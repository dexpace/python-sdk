# Copyright (c) 2026 dexpace and Omar Aljarrah.
# Licensed under the MIT License. See LICENSE.md in the repository root for details.

"""Typed-model codec sitting above ``Serde``.

``Serde`` is document-in / document-out: it turns wire bytes into plain
``dict`` / ``list`` / scalar documents and back. This module bridges that
plain-document layer and frozen dataclass models::

    wire bytes -> Serde.deserializer -> document -> Codec.decode -> dataclass
    dataclass  -> Codec.encode -> document -> Serde.serializer -> wire bytes

The codec never touches JSON (or any other) syntax, so it is format-agnostic
and reusable with any ``Serde``. It is deliberately validation-free: it
reconstructs declared types, handles aliases, ``Tristate`` fields, datetimes,
enums, containers and discriminated unions, but performs no schema checks or
scalar coercion. The model's own ``__post_init__`` invariants still run because
construction goes through the normal constructor.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime as _dt
import enum
import types
import typing
from typing import Final, Union, cast, get_args, get_origin, get_type_hints

from ..errors import DeserializationError, SerializationError
from .tristate import ABSENT, NULL, Present, Tristate

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Mapping

ALIAS_KEY: Final = "alias"
"""``field.metadata`` key naming the wire name for a dataclass field."""

DISCRIMINATOR_KEY: Final = "__codec_discriminator__"
"""Class attribute naming a discriminated union's tag/discriminator field."""

REGISTRY_KEY: Final = "__codec_registry__"
"""Class attribute holding a discriminated union's ``tag -> concrete`` map."""


class CodecError(DeserializationError):
    """A document could not be decoded into the requested typed model.

    Carries a wire-name breadcrumb so failures point at the offending location
    in the source document. Subclasses ``DeserializationError`` (hence
    ``ValueError`` and ``SdkError``) so existing handlers continue to catch it.

    Attributes:
        path: Wire-name breadcrumb to the offending location, e.g.
            ``("methods", "[0]", "last4")``.
        target_name: Name of the type that was being decoded, if known.
    """

    def __init__(
        self,
        reason: str,
        *,
        path: tuple[str, ...] = (),
        target_name: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Initialise the error.

        Args:
            reason: Human-readable failure description.
            path: Wire-name breadcrumb to the offending location.
            target_name: Name of the type being decoded, if known.
            error: Underlying cause, if any.
        """
        self.path = path
        self.target_name = target_name
        super().__init__(_render(reason, path, target_name), error=error)


def _render(reason: str, path: tuple[str, ...], target_name: str | None) -> str:
    """Render a codec error message with its path and target context."""
    rendered = _render_path(path)
    prefix = f"field path '{rendered}': " if rendered else ""
    suffix = f" (decoding {target_name})" if target_name else ""
    return f"{prefix}{reason}{suffix}"


def _render_path(path: tuple[str, ...]) -> str:
    """Join a breadcrumb into ``a.b[0].c`` form."""
    out = ""
    for part in path:
        if part.startswith("["):
            out += part
        else:
            out += f".{part}" if out else part
    return out


@dataclasses.dataclass(frozen=True, slots=True)
class _ModelInfo:
    """Cached per-model decode metadata."""

    hints: Mapping[str, object]
    field_to_wire: Mapping[str, str]
    wire_to_field: Mapping[str, str]


# Bounded by the (finite) set of model classes defined at import time. Models
# are not created dynamically at runtime, so this cache never grows unbounded
# in practice; the lack of an explicit size cap is acceptable for that reason.
_MODEL_CACHE: dict[type, _ModelInfo] = {}


def field_alias(
    wire_name: str,
    /,
    *,
    default: object = dataclasses.MISSING,
    default_factory: Callable[[], object] | None = None,
) -> object:
    """Declare a dataclass field whose wire name differs from its Python name.

    Sugar over ``dataclasses.field(metadata={ALIAS_KEY: wire_name}, ...)``.
    Raw ``field(metadata={"alias": ...})`` works identically.

    Args:
        wire_name: The key used in the wire document for this field.
        default: Optional default value (mutually exclusive with
            ``default_factory``).
        default_factory: Optional zero-arg factory producing the default.

    Returns:
        A ``dataclasses.Field`` carrying the alias metadata.
    """
    metadata = {ALIAS_KEY: wire_name}
    if default is not dataclasses.MISSING:
        return dataclasses.field(default=default, metadata=metadata)
    if default_factory is not None:
        return dataclasses.field(default_factory=default_factory, metadata=metadata)
    return dataclasses.field(metadata=metadata)


def discriminated[T](tag_field: str, /) -> Callable[[type[T]], type[T]]:
    """Mark a base/union class as a discriminated union.

    Attaches an empty variant registry and records which wire field carries the
    discriminator tag. Apply to the base type; register concrete variants with
    ``@variant``.

    Args:
        tag_field: Wire name of the field carrying the discriminator value.

    Returns:
        A class decorator returning the class unchanged.
    """

    def decorate(cls: type[T]) -> type[T]:
        setattr(cls, DISCRIMINATOR_KEY, tag_field)
        setattr(cls, REGISTRY_KEY, {})
        return cls

    return decorate


def variant[T](tag_value: str, /) -> Callable[[type[T]], type[T]]:
    """Register a concrete dataclass under ``tag_value`` in its base's registry.

    Walks the decorated class's MRO to find the nearest base carrying a registry
    (declared via ``@discriminated``) and registers the class there.

    Args:
        tag_value: The discriminator value selecting this variant.

    Returns:
        A class decorator returning the class unchanged.

    Raises:
        TypeError: If no ``@discriminated`` base is found in the MRO.
        ValueError: If ``tag_value`` is already registered under that base.
    """

    def decorate(cls: type[T]) -> type[T]:
        registry = _find_registry(cls)
        if tag_value in registry:
            raise ValueError(
                f"discriminator value {tag_value!r} already registered "
                f"(by {registry[tag_value].__name__})",
            )
        registry[tag_value] = cls
        return cls

    return decorate


def _find_registry(cls: type) -> dict[str, type]:
    """Return the variant registry owned by the nearest ``@discriminated`` base."""
    for base in cls.__mro__[1:]:
        registry = base.__dict__.get(REGISTRY_KEY)
        if registry is not None:
            return cast("dict[str, type]", registry)
    raise TypeError(
        f"{cls.__name__} has no @discriminated base; apply @discriminated to its union base first",
    )


class Codec:
    """Stateless engine converting between documents and typed models.

    Constructed once and reused. Effectively immutable after construction; its
    only mutable state is a shared module-level type-hint cache whose dict
    operations are atomic under CPython's GIL, so instances are safe to share
    across threads.
    """

    __slots__ = ("_tolerate_unknown",)

    def __init__(self, *, tolerate_unknown: bool = True) -> None:
        """Configure the codec.

        Args:
            tolerate_unknown: When ``True`` (default), wire keys not claimed by
                any field are silently dropped on decode, so a growing server
                payload does not break older clients. When ``False``, an
                unclaimed key raises ``CodecError``.
        """
        self._tolerate_unknown = tolerate_unknown

    def decode[T](self, data: object, target: type[T]) -> T:
        """Decode a plain document into an instance of ``target``.

        Args:
            data: A plain document (``dict`` / ``list`` / scalar) as produced by
                ``Serde.deserializer``.
            target: The type to reconstruct — a dataclass, a discriminated base,
                ``list[X]`` / ``dict[str, X]``, a datetime/enum, or a scalar.

        Returns:
            A fully constructed instance of ``target``.

        Raises:
            CodecError: On any structural mismatch or conversion failure, with a
                wire-name path pointing at the offending location.
        """
        return cast("T", _decode_value(data, target, (), self._tolerate_unknown))

    def encode(self, value: object) -> object:
        """Encode a typed value into a plain document.

        Args:
            value: A dataclass, container, datetime, enum, ``Tristate`` field
                value, or scalar.

        Returns:
            A plain document (``dict`` / ``list`` / scalar) ready for
            ``Serde.serializer``.

        Raises:
            SerializationError: If ``value`` cannot be turned into a document.
        """
        return _encode_value(value)


# --------------------------------------------------------------------------- #
# Decode                                                                       #
# --------------------------------------------------------------------------- #


def _decode_value(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode ``data`` into ``target``, dispatching on the type's shape."""
    if target is object or target is typing.Any:
        return data
    if _is_tristate(target):
        return _decode_tristate(data, target, path, tolerate_unknown)
    origin = get_origin(target)
    if origin is None:
        return _decode_atomic(data, target, path, tolerate_unknown)
    if origin in (Union, types.UnionType):
        return _decode_union(data, target, path, tolerate_unknown)
    return _decode_container(data, target, origin, path, tolerate_unknown)


def _decode_atomic(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode into a non-parametrised target (dataclass, datetime, enum, scalar)."""
    if isinstance(target, type):
        if REGISTRY_KEY in target.__dict__:
            return _dispatch_union(data, target, path, tolerate_unknown)
        if dataclasses.is_dataclass(target):
            return _decode_dataclass(data, target, path, tolerate_unknown)
        if issubclass(target, enum.Enum):
            return _decode_enum(data, target, path)
        if issubclass(target, (_dt.datetime, _dt.date, _dt.time)):
            return _decode_temporal(data, target, path)
    return data


def _decode_dataclass(
    data: object,
    target: type,
    path: tuple[str, ...],
    tolerate_unknown: bool,
    *,
    exempt_key: str | None = None,
) -> object:
    """Decode a mapping into a plain dataclass, field by field.

    Args:
        data: The wire mapping to decode.
        target: The dataclass type to construct.
        path: Wire-name breadcrumb to this location.
        tolerate_unknown: Whether unclaimed keys are dropped or rejected.
        exempt_key: A wire key always permitted under strict mode even when no
            field claims it — used for a discriminated union's tag, which is a
            structural key rather than a stray unknown one.
    """
    if not isinstance(data, cabc.Mapping):
        raise CodecError(
            f"expected an object, got {type(data).__name__}",
            path=path,
            target_name=target.__name__,
        )
    info = _resolve_info(target)
    kwargs = _decode_fields(data, target, info, path, tolerate_unknown)
    if not tolerate_unknown:
        _reject_unknown(data, info, path, target.__name__, exempt_key=exempt_key)
    try:
        return target(**kwargs)
    except (TypeError, ValueError) as err:
        raise CodecError(str(err), path=path, target_name=target.__name__, error=err) from err


def _decode_fields(
    data: Mapping[object, object],
    target: type,
    info: _ModelInfo,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> dict[str, object]:
    """Build the constructor kwargs for ``target`` from ``data``."""
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(target):
        wire = info.field_to_wire[f.name]
        hint = info.hints[f.name]
        if wire not in data:
            _require_present_or_default(f, wire, path, target.__name__)
            continue
        kwargs[f.name] = _decode_value(data[wire], hint, (*path, wire), tolerate_unknown)
    return kwargs


def _require_present_or_default(
    f: dataclasses.Field[object],
    wire: str,
    path: tuple[str, ...],
    target_name: str,
) -> None:
    """Ensure a missing field has a default; otherwise raise ``CodecError``."""
    has_default = f.default is not dataclasses.MISSING
    has_factory = f.default_factory is not dataclasses.MISSING
    if not (has_default or has_factory):
        raise CodecError(
            f"missing required field {f.name!r} (wire {wire!r})",
            path=path,
            target_name=target_name,
        )


def _reject_unknown(
    data: Mapping[object, object],
    info: _ModelInfo,
    path: tuple[str, ...],
    target_name: str,
    *,
    exempt_key: str | None = None,
) -> None:
    """Raise if ``data`` carries a key not claimed by any field.

    ``exempt_key`` names a wire key that is always permitted even when no field
    claims it (a discriminated union's tag), so strict mode does not punish the
    very key that drove variant dispatch.
    """
    for key in data:
        if key == exempt_key:
            continue
        if key not in info.wire_to_field:
            raise CodecError(
                f"unknown field {key!r}",
                path=path,
                target_name=target_name,
            )


def _decode_container(
    data: object,
    target: object,
    origin: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode a parametrised container (list/tuple/set/dict/mapping)."""
    args = get_args(target)
    if origin is dict or _is_mapping_origin(origin):
        return _decode_mapping(data, args, path, tolerate_unknown)
    if origin is tuple:
        return _decode_tuple(data, args, path, tolerate_unknown)
    if origin in (list, set, frozenset) or _is_sequence_origin(origin):
        return _decode_sequence(data, origin, args, path, tolerate_unknown)
    return data


def _decode_sequence(
    data: object,
    origin: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode a homogeneous sequence into list/set/frozenset (default list)."""
    if not isinstance(data, cabc.Iterable) or isinstance(data, (str, bytes, cabc.Mapping)):
        raise CodecError(f"expected an array, got {type(data).__name__}", path=path)
    elem = args[0] if args else object
    items = [
        _decode_value(item, elem, (*path, f"[{i}]"), tolerate_unknown)
        for i, item in enumerate(data)
    ]
    if origin in (set, cabc.Set, cabc.MutableSet):
        return set(items)
    if origin is frozenset:
        return frozenset(items)
    return items


def _decode_tuple(
    data: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode a homogeneous (``tuple[X, ...]``) or fixed-arity tuple."""
    if not isinstance(data, cabc.Iterable) or isinstance(data, (str, bytes, cabc.Mapping)):
        raise CodecError(f"expected an array, got {type(data).__name__}", path=path)
    seq = list(data)
    if len(args) == 2 and args[1] is Ellipsis:
        elem = args[0]
        return tuple(
            _decode_value(v, elem, (*path, f"[{i}]"), tolerate_unknown) for i, v in enumerate(seq)
        )
    arity = len(args)
    if len(seq) != arity:
        raise CodecError(
            f"expected an array of {arity} element(s), got {len(seq)}",
            path=path,
        )
    return tuple(
        _decode_value(v, t, (*path, f"[{i}]"), tolerate_unknown)
        for i, (v, t) in enumerate(zip(seq, args, strict=True))
    )


def _decode_mapping(
    data: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode a mapping, recovering each key and value through its declared type.

    Wire object keys are always strings, so a declared key type such as ``int``,
    an enum, or ``UUID`` is recovered by recursing on the key the same way the
    codec recurses on values; ``str`` and ``object`` key types pass through.
    """
    if not isinstance(data, cabc.Mapping):
        raise CodecError(f"expected an object, got {type(data).__name__}", path=path)
    key_type = args[0] if len(args) == 2 else object
    value_type = args[1] if len(args) == 2 else object
    return {
        _decode_value(key, key_type, (*path, str(key)), tolerate_unknown): _decode_value(
            val,
            value_type,
            (*path, str(key)),
            tolerate_unknown,
        )
        for key, val in data.items()
    }


def _decode_union(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode an ``X | None`` union: ``None`` passthrough, else decode ``X``.

    Only single-arm optionals (``X | None``) recover their inner type; ``None``
    is passed through only when ``NoneType`` is genuinely a union member, so a
    non-optional union such as ``int | str`` does not silently accept ``None``.
    Unions with two or more non-``None`` arms are tagless and cannot be resolved
    structurally, so their payload passes through untouched — use a discriminated
    union (``@discriminated`` / ``@variant``) when an arm must be reconstructed.
    """
    all_args = get_args(target)
    args = [a for a in all_args if a is not type(None)]
    if data is None and type(None) in all_args:
        return None
    if len(args) == 1:
        return _decode_value(data, args[0], path, tolerate_unknown)
    return data


def _decode_tristate(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Decode a present key into ``NULL`` or ``Present(inner)``.

    A missing key is handled upstream in ``_decode_fields`` (the kwarg is
    omitted so the field default applies); this path only runs for present keys.
    """
    if data is None:
        return NULL
    inner = _tristate_inner(target)
    return Present(_decode_value(data, inner, path, tolerate_unknown))


def _decode_enum(data: object, target: type[enum.Enum], path: tuple[str, ...]) -> object:
    """Decode a value into an enum member by value."""
    try:
        return target(data)
    except ValueError as err:
        raise CodecError(
            f"{data!r} is not a valid {target.__name__}",
            path=path,
            target_name=target.__name__,
            error=err,
        ) from err


def _decode_temporal(data: object, target: type, path: tuple[str, ...]) -> object:
    """Decode an ISO-8601 string into datetime/date/time."""
    if not isinstance(data, str):
        raise CodecError(
            f"expected an ISO-8601 string, got {type(data).__name__}",
            path=path,
            target_name=target.__name__,
        )
    try:
        return target.fromisoformat(data)  # type: ignore[attr-defined]
    except ValueError as err:
        raise CodecError(
            f"{data!r} is not a valid {target.__name__}",
            path=path,
            target_name=target.__name__,
            error=err,
        ) from err


def _dispatch_union(
    data: object,
    base: type,
    path: tuple[str, ...],
    tolerate_unknown: bool,
) -> object:
    """Resolve a discriminated union to a concrete variant and decode it."""
    if not isinstance(data, cabc.Mapping):
        raise CodecError(
            f"expected an object, got {type(data).__name__}",
            path=path,
            target_name=base.__name__,
        )
    tag_field: str = getattr(base, DISCRIMINATOR_KEY)
    registry = cast("dict[str, type]", getattr(base, REGISTRY_KEY))
    if tag_field not in data:
        raise CodecError(
            f"missing discriminator field {tag_field!r}",
            path=path,
            target_name=base.__name__,
        )
    tag = data[tag_field]
    concrete = registry.get(cast("str", tag))
    if concrete is None:
        known = sorted(registry)
        raise CodecError(
            f"unknown discriminator value {tag!r}; known: {known}",
            path=path,
            target_name=base.__name__,
        )
    return _decode_dataclass(
        data,
        concrete,
        path,
        tolerate_unknown,
        exempt_key=tag_field,
    )


# --------------------------------------------------------------------------- #
# Encode                                                                       #
# --------------------------------------------------------------------------- #


def _encode_value(value: object) -> object:
    """Encode a typed value into a plain document."""
    if isinstance(value, enum.Enum):
        # Checked before the scalar branch: ``StrEnum`` members are ``str`` and
        # ``IntEnum`` members are ``int``, so an earlier scalar check would
        # return the member itself instead of collapsing it to ``value.value``.
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Present):
        return _encode_value(value.value)
    if value is NULL or value is ABSENT:
        # Bare tristate sentinels have no enclosing key to fold against; both
        # collapse to ``None`` at the top level. The absent-vs-null distinction
        # is only observable when folding a dataclass field (see
        # ``_encode_dataclass``).
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _encode_dataclass(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as err:
            raise SerializationError("cannot encode non-UTF-8 bytes value") from err
    if isinstance(value, cabc.Mapping):
        return {_encode_key(k): _encode_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode_value(v) for v in value]
    raise SerializationError(f"cannot encode value of type {type(value).__name__}")


def _encode_dataclass(value: object) -> dict[str, object]:
    """Encode a dataclass into a document, folding tristate fields."""
    assert dataclasses.is_dataclass(value) and not isinstance(value, type)
    info = _resolve_info(type(value))
    out: dict[str, object] = {}
    for f in dataclasses.fields(value):
        wire = info.field_to_wire[f.name]
        attr = getattr(value, f.name)
        if attr is ABSENT:
            continue
        if attr is NULL:
            out[wire] = None
            continue
        out[wire] = _encode_value(attr)
    return out


def _encode_key(key: object) -> str | int | float | bool | None:
    """Collapse a mapping key into a document-legal scalar key.

    Runs the key through ``_encode_value`` (which folds an enum to its value and
    a datetime/date/time to its ISO string) then ensures the result is a JSON
    object key type. A leftover ``str`` / ``int`` / ``float`` / ``bool`` /
    ``None`` passes through; anything else is coerced to ``str(...)`` so the
    encoded document round-trips against ``_decode_mapping``.

    Args:
        key: The original mapping key.

    Returns:
        A ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` document key.

    Raises:
        SerializationError: If the collapsed key is a container or other type
            that has no meaningful scalar key form.
    """
    encoded = _encode_value(key)
    if encoded is None or isinstance(encoded, (str, int, float, bool)):
        return encoded
    if isinstance(encoded, (cabc.Mapping, list, tuple, set, frozenset)):
        raise SerializationError(
            f"cannot encode mapping key of type {type(key).__name__}",
        )
    return str(encoded)


# --------------------------------------------------------------------------- #
# Type introspection helpers                                                   #
# --------------------------------------------------------------------------- #


def _resolve_info(target: type) -> _ModelInfo:
    """Resolve and cache decode metadata for a dataclass ``target``."""
    info = _MODEL_CACHE.get(target)
    if info is not None:
        return info
    hints = get_type_hints(target, include_extras=True)
    field_to_wire: dict[str, str] = {}
    wire_to_field: dict[str, str] = {}
    for f in dataclasses.fields(target):
        wire = f.metadata.get(ALIAS_KEY, f.name)
        field_to_wire[f.name] = wire
        wire_to_field[wire] = f.name
    info = _ModelInfo(hints=hints, field_to_wire=field_to_wire, wire_to_field=wire_to_field)
    _MODEL_CACHE[target] = info
    return info


def _is_tristate(target: object) -> bool:
    """Return whether ``target`` is a ``Tristate[X]`` (or its expanded union).

    ``get_type_hints`` resolves a ``type`` alias such as ``Tristate[str]`` to a
    ``GenericAlias`` whose origin is the ``Tristate`` alias object itself, not a
    ``Union``. Older / expanded forms surface as a ``Present``-bearing union, so
    both shapes are recognised. A bare, non-parametrised ``Tristate`` field is
    treated as ``Tristate[object]`` (inner type ``object``).
    """
    if target is Tristate:
        return True
    if get_origin(target) is Tristate:
        return True
    if get_origin(target) in (Union, types.UnionType):
        return any(get_origin(arg) is Present or arg is Present for arg in get_args(target))
    return False


def _tristate_inner(target: object) -> object:
    """Recover ``X`` from a ``Tristate[X]`` (or its expanded union form)."""
    if get_origin(target) is Tristate:
        args = get_args(target)
        return args[0] if args else object
    for arg in get_args(target):
        if get_origin(arg) is Present:
            inner = get_args(arg)
            return inner[0] if inner else object
        if arg is Present:
            return object
    return object


def _is_mapping_origin(origin: object) -> bool:
    """Return whether ``origin`` is an abstract Mapping origin."""
    return origin in (cabc.Mapping, cabc.MutableMapping)


def _is_sequence_origin(origin: object) -> bool:
    """Return whether ``origin`` is an abstract Sequence/Set origin."""
    return origin in (
        cabc.Sequence,
        cabc.MutableSequence,
        cabc.Set,
        cabc.MutableSet,
        cabc.Iterable,
        cabc.Collection,
    )


__all__ = [
    "ALIAS_KEY",
    "DISCRIMINATOR_KEY",
    "REGISTRY_KEY",
    "Codec",
    "CodecError",
    "discriminated",
    "field_alias",
    "variant",
]
