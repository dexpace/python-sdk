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
``UUID``s, enums, containers and discriminated unions, but performs no schema
checks or scalar coercion. The model's own ``__post_init__`` invariants still run because
construction goes through the normal constructor.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses
import datetime as _dt
import enum
import types
import typing
import uuid
from typing import Annotated, Final, Union, cast, get_args, get_origin, get_type_hints

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

_MAX_DEPTH: Final = 200
"""Recursion ceiling for ``_decode_value``.

A hostile, deeply nested document (e.g. ``[[[[...]]]]`` thousands of levels
deep) would otherwise exhaust the interpreter stack and surface a bare
``RecursionError``, escaping the codec's ``CodecError`` contract. The guard
trips well before CPython's default limit so the failure is a clean
``CodecError`` instead.
"""


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

    Raises:
        ValueError: If both ``default`` and ``default_factory`` are supplied.
    """
    metadata = {ALIAS_KEY: wire_name}
    if default is not dataclasses.MISSING and default_factory is not None:
        # ``dataclasses.field`` rejects this combo too, but only at class-body
        # evaluation; catching it here keeps the failure local to the call and
        # avoids silently ignoring the factory.
        raise ValueError("field_alias: pass at most one of default / default_factory")
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
    only mutable state is a shared, append-only module-level type-hint cache.
    A cache entry is computed independently per model and never mutated once
    stored, so a concurrent miss merely recomputes the same value and the last
    write wins with an identical result. The codec therefore does not rely on
    the GIL or atomic dict operations for correctness and is safe to share
    across threads, including under free-threaded CPython.
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
                ``list[X]`` / ``dict[str, X]``, a datetime/UUID/enum, or a
                scalar.

        Returns:
            A fully constructed instance of ``target``.

        Raises:
            CodecError: On any structural mismatch or conversion failure, with a
                wire-name path pointing at the offending location.
        """
        return cast("T", _decode_value(data, target, (), self._tolerate_unknown, 0))

    def encode(self, value: object) -> object:
        """Encode a typed value into a plain document.

        Args:
            value: A dataclass, container, datetime, ``UUID``, enum,
                ``Tristate`` field value, or scalar.

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
    depth: int,
) -> object:
    """Decode ``data`` into ``target``, dispatching on the type's shape."""
    if depth > _MAX_DEPTH:
        raise CodecError(
            f"maximum decode depth {_MAX_DEPTH} exceeded (input nested too deeply)",
            path=path,
        )
    if get_origin(target) is Annotated:
        # ``Annotated[X, ...]`` carries no decode meaning here; strip the
        # metadata and decode as the underlying ``X`` (without this the value
        # would fall through to the container branch and be returned undecoded).
        target = get_args(target)[0]
    if target is object or target is typing.Any:
        return data
    if _is_tristate(target):
        return _decode_tristate(data, target, path, tolerate_unknown, depth)
    origin = get_origin(target)
    if origin is None:
        return _decode_atomic(data, target, path, tolerate_unknown, depth)
    if origin in (Union, types.UnionType):
        return _decode_union(data, target, path, tolerate_unknown, depth)
    if isinstance(origin, type) and dataclasses.is_dataclass(origin):
        # A parametrised generic dataclass target (``Box[int]``) has a real
        # dataclass origin; decode it as that dataclass rather than letting it
        # fall through the container branch and return the raw dict undecoded.
        # The type arguments are mapped onto the class's type parameters so
        # generic fields decode against their concrete substitution.
        return _decode_dataclass(
            data, origin, path, tolerate_unknown, depth, type_args=_type_arg_map(origin, target)
        )
    return _decode_container(data, target, origin, path, tolerate_unknown, depth)


def _type_arg_map(origin: type, target: object) -> Mapping[object, object]:
    """Map a generic dataclass's type parameters onto the supplied arguments.

    For ``Box[int]`` with ``class Box[T]`` this returns ``{T: int}``. Mismatched
    counts (or a non-generic origin) yield an empty map, leaving each field's
    declared hint untouched.

    Args:
        origin: The dataclass origin of the parametrised target.
        target: The parametrised generic alias (e.g. ``Box[int]``).

    Returns:
        A mapping from each type parameter to its concrete argument.
    """
    params = getattr(origin, "__type_params__", ()) or getattr(origin, "__parameters__", ())
    args = get_args(target)
    if not params or len(params) != len(args):
        return {}
    return dict(zip(params, args, strict=True))


def _decode_atomic(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode into a non-parametrised target (dataclass, datetime, enum, scalar)."""
    if isinstance(target, type):
        if REGISTRY_KEY in target.__dict__:
            return _dispatch_union(data, target, path, tolerate_unknown, depth)
        if dataclasses.is_dataclass(target):
            return _decode_dataclass(data, target, path, tolerate_unknown, depth)
        if issubclass(target, enum.Enum):
            return _decode_enum(data, target, path)
        if issubclass(target, (_dt.datetime, _dt.date, _dt.time)):
            return _decode_temporal(data, target, path)
        if issubclass(target, uuid.UUID):
            return _decode_uuid(data, target, path)
    return data


def _decode_dataclass(
    data: object,
    target: type,
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
    *,
    exempt_key: str | None = None,
    type_args: Mapping[object, object] | None = None,
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
        type_args: For a parametrised generic dataclass, a map from the class's
            type parameters to their concrete arguments, applied to each field's
            declared hint before decoding.
    """
    if not isinstance(data, cabc.Mapping):
        raise CodecError(
            f"expected an object, got {type(data).__name__}",
            path=path,
            target_name=target.__name__,
        )
    info = _resolve_info(target)
    kwargs = _decode_fields(data, target, info, path, tolerate_unknown, depth, type_args)
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
    depth: int,
    type_args: Mapping[object, object] | None = None,
) -> dict[str, object]:
    """Build the constructor kwargs for ``target`` from ``data``."""
    kwargs: dict[str, object] = {}
    for f in dataclasses.fields(target):
        wire = info.field_to_wire[f.name]
        hint = info.hints[f.name]
        if type_args:
            hint = _substitute_type_vars(hint, type_args)
        if wire not in data:
            if (default := _missing_field_value(f, hint, wire, path, target.__name__)) is not _OMIT:
                kwargs[f.name] = default
            continue
        kwargs[f.name] = _decode_value(data[wire], hint, (*path, wire), tolerate_unknown, depth + 1)
    return kwargs


def _substitute_type_vars(hint: object, type_args: Mapping[object, object]) -> object:
    """Replace any type parameters in ``hint`` with their concrete arguments.

    Recurses through parametrised generics so a nested ``list[T]`` resolves to
    ``list[int]``. A bare type parameter is substituted directly; anything with
    no parameter to replace is returned unchanged.

    Args:
        hint: A resolved type hint, possibly mentioning a type parameter.
        type_args: Map from type parameters to their concrete arguments.

    Returns:
        ``hint`` with every known type parameter substituted.
    """
    if hint in type_args:
        return type_args[hint]
    args = get_args(hint)
    if not args:
        return hint
    origin = get_origin(hint)
    new_args = tuple(_substitute_type_vars(a, type_args) for a in args)
    if new_args == args or origin is None:
        return hint
    return origin[new_args]


_OMIT: Final = object()
"""Sentinel meaning "supply no kwarg; let the constructor's own default apply"."""


def _missing_field_value(
    f: dataclasses.Field[object],
    hint: object,
    wire: str,
    path: tuple[str, ...],
    target_name: str,
) -> object:
    """Resolve the value for a field whose wire key is absent.

    A field carrying its own default or default-factory is left for the
    constructor to fill (signalled by returning ``_OMIT``). A ``Tristate`` field
    with no declared default still has a meaningful "absent" value — ``ABSENT``
    is exactly the type's omitted-key inhabitant — so it is supplied rather than
    treated as a missing required field. Any other defaultless field is a
    genuine omission and raises.

    Args:
        f: The dataclass field whose key was absent from the document.
        hint: The field's resolved type hint.
        wire: The wire name that was looked up and not found.
        path: Wire-name breadcrumb to this location.
        target_name: Name of the dataclass being decoded.

    Returns:
        ``ABSENT`` for a defaultless ``Tristate`` field, otherwise ``_OMIT``.

    Raises:
        CodecError: If the field has neither a default nor ``Tristate`` typing.
    """
    if f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING:
        return _OMIT
    if _is_tristate(hint):
        return ABSENT
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
    depth: int,
) -> object:
    """Decode a parametrised container (list/tuple/set/dict/mapping)."""
    args = get_args(target)
    if origin is dict or _is_mapping_origin(origin):
        return _decode_mapping(data, args, path, tolerate_unknown, depth)
    if origin is tuple:
        return _decode_tuple(data, args, path, tolerate_unknown, depth)
    if origin in (list, set, frozenset) or _is_sequence_origin(origin):
        return _decode_sequence(data, origin, args, path, tolerate_unknown, depth)
    return data


def _decode_sequence(
    data: object,
    origin: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode a homogeneous sequence into list/set/frozenset (default list)."""
    if not isinstance(data, cabc.Iterable) or isinstance(data, (str, bytes, cabc.Mapping)):
        raise CodecError(f"expected an array, got {type(data).__name__}", path=path)
    elem = args[0] if args else object
    items = [
        _decode_value(item, elem, (*path, f"[{i}]"), tolerate_unknown, depth + 1)
        for i, item in enumerate(data)
    ]
    if origin in (set, cabc.Set, cabc.MutableSet):
        return _build_hashed(set, items, path)
    if origin is frozenset:
        return _build_hashed(frozenset, items, path)
    return items


def _build_hashed[C](
    factory: Callable[[list[object]], C],
    items: list[object],
    path: tuple[str, ...],
) -> C:
    """Build a ``set`` / ``frozenset``, mapping unhashable elements to ``CodecError``.

    A decoded element may be unhashable (e.g. a ``list`` decoded under a
    ``set[object]`` field), in which case ``set()`` / ``frozenset()`` raises a
    bare ``TypeError`` that would escape the codec's ``CodecError`` contract.

    Args:
        factory: ``set`` or ``frozenset``.
        items: The decoded elements to collect.
        path: Wire-name breadcrumb to this location.

    Returns:
        The constructed set or frozenset.

    Raises:
        CodecError: If any element is unhashable.
    """
    try:
        return factory(items)
    except TypeError as err:
        raise CodecError(f"unhashable element in {factory.__name__}", path=path, error=err) from err


def _decode_tuple(
    data: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode a homogeneous (``tuple[X, ...]``) or fixed-arity tuple."""
    if not isinstance(data, cabc.Iterable) or isinstance(data, (str, bytes, cabc.Mapping)):
        raise CodecError(f"expected an array, got {type(data).__name__}", path=path)
    seq = list(data)
    if len(args) == 2 and args[1] is Ellipsis:
        elem = args[0]
        return tuple(
            _decode_value(v, elem, (*path, f"[{i}]"), tolerate_unknown, depth + 1)
            for i, v in enumerate(seq)
        )
    arity = len(args)
    if len(seq) != arity:
        raise CodecError(
            f"expected an array of {arity} element(s), got {len(seq)}",
            path=path,
        )
    return tuple(
        _decode_value(v, t, (*path, f"[{i}]"), tolerate_unknown, depth + 1)
        for i, (v, t) in enumerate(zip(seq, args, strict=True))
    )


def _decode_mapping(
    data: object,
    args: tuple[object, ...],
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode a mapping, recovering each key and value through its declared type.

    Wire object keys are always strings. A declared key type that has a
    dedicated reconstruction branch — an enum, a datetime/date/time, or a
    ``UUID`` — is recovered by recursing on the key the same way the codec
    recurses on values. ``str`` and ``object`` keys pass through, and a bare
    scalar key type (``int`` / ``float`` / ``bool``) follows the codec's
    no-coercion rule, so its wire string is returned unchanged.
    """
    if not isinstance(data, cabc.Mapping):
        raise CodecError(f"expected an object, got {type(data).__name__}", path=path)
    key_type = args[0] if len(args) == 2 else object
    value_type = args[1] if len(args) == 2 else object
    return {
        _decode_value(key, key_type, (*path, str(key)), tolerate_unknown, depth + 1): _decode_value(
            val,
            value_type,
            (*path, str(key)),
            tolerate_unknown,
            depth + 1,
        )
        for key, val in data.items()
    }


def _decode_union(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode a union, recovering the inner type only for single-arm optionals.

    A single-arm optional (``X | None``) recovers ``X`` for a non-``None``
    payload and yields ``None`` for a ``None`` payload. Any union with two or
    more non-``None`` arms (``int | str``, ``A | B | None``) is tagless and
    cannot be resolved structurally, so its payload passes through untouched —
    including a ``None`` payload, which is returned as-is rather than rejected.
    Use a discriminated union (``@discriminated`` / ``@variant``) when an arm
    must be reconstructed.
    """
    all_args = get_args(target)
    args = [a for a in all_args if a is not type(None)]
    if data is None and type(None) in all_args:
        return None
    if len(args) == 1:
        return _decode_value(data, args[0], path, tolerate_unknown, depth + 1)
    return data


def _decode_tristate(
    data: object,
    target: object,
    path: tuple[str, ...],
    tolerate_unknown: bool,
    depth: int,
) -> object:
    """Decode a present key into ``NULL`` or ``Present(inner)``.

    A missing key is handled upstream in ``_decode_fields`` (the kwarg is
    omitted so the field default applies); this path only runs for present keys.
    """
    if data is None:
        return NULL
    inner = _tristate_inner(target)
    return Present(_decode_value(data, inner, path, tolerate_unknown, depth + 1))


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


def _decode_uuid(data: object, target: type, path: tuple[str, ...]) -> object:
    """Decode a string into a ``UUID`` (the canonical form ``_encode_value`` emits)."""
    if not isinstance(data, str):
        raise CodecError(
            f"expected a UUID string, got {type(data).__name__}",
            path=path,
            target_name=target.__name__,
        )
    try:
        return target(data)
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
    depth: int,
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
        depth,
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
    if isinstance(value, uuid.UUID):
        return str(value)
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

    Runs the key through ``_encode_value`` (which folds an enum to its value, a
    datetime/date/time to its ISO string, and a ``UUID`` to its canonical
    string) then ensures the result is a JSON object key type. A leftover
    ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` passes through; anything
    else is coerced to ``str(...)``.

    Round-trip note: enum, temporal, and ``UUID`` keys reconstruct to their
    declared type on decode because ``_decode_mapping`` recurses through a
    branch that rebuilds them. A bare scalar key (``int`` / ``float`` / ``bool``)
    follows the codec's no-coercion rule instead — JSON renders it as a string
    and decode returns that string unchanged, so such a key does not survive a
    full wire round-trip as its original type.

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
    # A PEP 695 generic dataclass (``class Box[T]``) annotates fields with its
    # type parameters (``item: T``). Python 3.13+ resolves those automatically,
    # but 3.12's ``get_type_hints`` does not see them and raises ``NameError``;
    # supply them via ``localns`` so resolution works on every supported version.
    type_params = getattr(target, "__type_params__", ())
    localns = {tp.__name__: tp for tp in type_params} or None
    try:
        hints = get_type_hints(target, include_extras=True, localns=localns)
    except NameError as err:
        # An unresolvable forward reference (a string annotation whose name is
        # not in scope) surfaces as a bare ``NameError`` from ``get_type_hints``;
        # wrap it so the codec keeps its ``CodecError`` contract.
        raise CodecError(
            f"cannot resolve a type hint on {target.__name__}: {err}",
            target_name=target.__name__,
            error=err,
        ) from err
    field_to_wire: dict[str, str] = {}
    wire_to_field: dict[str, str] = {}
    for f in dataclasses.fields(target):
        wire = f.metadata.get(ALIAS_KEY, f.name)
        if wire in wire_to_field:
            # Two fields claiming the same wire alias would silently shadow each
            # other (last-wins on decode, double-write on encode); reject it.
            raise CodecError(
                f"fields {wire_to_field[wire]!r} and {f.name!r} both map to wire name {wire!r}",
                target_name=target.__name__,
            )
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
    if get_origin(target) is Annotated:
        # An ``Annotated[Tristate[X], ...]`` field is still a Tristate; unwrap
        # so a defaultless annotated Tristate resolves to ABSENT on an omitted
        # key (matching the bare ``Tristate[X]`` contract).
        target = get_args(target)[0]
    if target is Tristate:
        return True
    if get_origin(target) is Tristate:
        return True
    if get_origin(target) in (Union, types.UnionType):
        return any(get_origin(arg) is Present or arg is Present for arg in get_args(target))
    return False


def _tristate_inner(target: object) -> object:
    """Recover ``X`` from a ``Tristate[X]`` (or its expanded union form)."""
    if get_origin(target) is Annotated:
        target = get_args(target)[0]
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
