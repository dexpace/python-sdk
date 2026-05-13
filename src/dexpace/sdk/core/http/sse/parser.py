"""Server-Sent Events parser per the WHATWG spec.

Consumes an iterator of ``bytes`` chunks (typically from
``ResponseBody.iter_bytes`` / ``AsyncResponseBody.aiter_bytes``) and emits
``SseEvent`` records. The parser:

- Buffers across chunk boundaries (lines may be split mid-byte).
- Handles ``LF`` / ``CR`` / ``CRLF`` line terminators interchangeably per
  the spec.
- Joins multi-line ``data:`` fields with a single ``\n``.
- Skips comment lines (those beginning with ``:``).
- Treats blank lines as event terminators; empty events are not emitted.
- Decodes payloads as UTF-8 (the spec mandates UTF-8 for ``text/event-stream``).

Per-event fields:
- ``data``: the (possibly multi-line) message payload.
- ``event``: the event name (defaults to ``"message"``).
- ``id``: the last-event-id (sticky across the parser's lifetime per spec).
- ``retry``: reconnect time in milliseconds, when the server sends one.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Final

from dexpace.sdk.core.errors import StreamingError

_LF: Final[int] = 0x0A
_CR: Final[int] = 0x0D
_COLON: Final[int] = 0x3A


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One parsed Server-Sent Event.

    Attributes:
        data: The event payload (with newlines between concatenated
            ``data:`` lines).
        event: The event name (``"message"`` if the stream omitted ``event:``).
        id: The most recent ``id:`` value seen by the parser, sticky across
            events per the spec.
        retry: The most recent ``retry:`` value seen by the parser, sticky
            like ``id`` once observed. Although carried on each ``SseEvent``,
            this field is **not** per-event: the WHATWG spec defines
            ``retry`` as a connection-level reconnection-time setting that
            persists until the server sends a new value, so subsequent
            events repeat the last observed integer (milliseconds) until
            superseded. ``None`` means no ``retry:`` line has been seen
            yet on this stream.
    """

    data: str
    event: str = "message"
    id: str | None = None
    retry: int | None = None


@dataclass(slots=True)
class SseParser:
    """Stateful WHATWG SSE parser.

    Feed bytes via :meth:`feed`; finished events arrive on the internal
    queue and are yielded by :meth:`drain`. Callers typically use the
    free functions :func:`parse_events` / :func:`parse_async_events` rather
    than holding a parser directly.
    """

    _buffer: bytearray = field(default_factory=bytearray)
    _data_lines: list[str] = field(default_factory=list)
    _event: str = "message"
    _last_id: str | None = None
    _retry: int | None = None
    _pending: deque[SseEvent] = field(default_factory=deque)
    max_line_bytes: int = 1 << 20  # 1 MiB

    def feed(self, chunk: bytes) -> None:
        """Append ``chunk`` to the parser buffer and consume completed lines.

        Raises:
            StreamingError: If the buffered prefix exceeds ``max_line_bytes``
                without a line terminator.
        """
        if not chunk:
            return
        self._buffer.extend(chunk)
        while True:
            line, consumed = _read_line(self._buffer)
            if line is None:
                if len(self._buffer) > self.max_line_bytes:
                    raise StreamingError(f"SSE line exceeded {self.max_line_bytes} bytes")
                return
            del self._buffer[:consumed]
            self._process_line(line)

    def drain(self) -> Iterator[SseEvent]:
        """Yield (and clear) every event accumulated by ``feed`` so far."""
        while self._pending:
            yield self._pending.popleft()

    def end(self) -> Iterator[SseEvent]:
        """Flush any final event (no trailing blank line) before EOS.

        Raises:
            StreamingError: If the trailing buffer ends mid-codepoint and
                cannot be decoded as UTF-8.
        """
        if self._buffer:
            try:
                line = self._buffer.decode("utf-8")
            except UnicodeDecodeError as err:
                raise StreamingError("Stream ended mid-codepoint") from err
            self._buffer.clear()
            self._process_line(line)
        if self._data_lines:
            self._dispatch()
        yield from self.drain()

    def _process_line(self, line: str) -> None:
        if not line:
            self._dispatch()
            return
        if line.startswith(":"):
            return  # comment
        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        match field_name:
            case "data":
                self._data_lines.append(value)
            case "event":
                self._event = value
            case "id":
                if "\0" not in value:  # spec: ignore IDs containing NULL
                    self._last_id = value
            case "retry":
                if value.isdigit():
                    self._retry = int(value)
            case _:
                # Unknown field — spec says ignore.
                pass

    def _dispatch(self) -> None:
        if not self._data_lines:
            # Spec: blank line with no data buffered ⇒ no event emitted,
            # but event name and retry reset.
            self._event = "message"
            return
        event = SseEvent(
            data="\n".join(self._data_lines),
            event=self._event,
            id=self._last_id,
            retry=self._retry,
        )
        self._pending.append(event)
        self._data_lines = []
        self._event = "message"


def _read_line(buffer: bytearray) -> tuple[str | None, int]:
    """Find the next complete line in ``buffer``.

    Returns ``(line, consumed)`` where ``line`` is the decoded text without
    its terminator and ``consumed`` is the number of bytes (including the
    terminator) to drop from the buffer. ``(None, 0)`` indicates the buffer
    does not yet contain a complete line.
    """
    for index, byte in enumerate(buffer):
        if byte == _LF:
            return buffer[:index].decode("utf-8"), index + 1
        if byte == _CR:
            # CR or CRLF — peek at the next byte if present.
            if index + 1 < len(buffer) and buffer[index + 1] == _LF:
                return buffer[:index].decode("utf-8"), index + 2
            return buffer[:index].decode("utf-8"), index + 1
    return None, 0


def parse_events(chunks: Iterable[bytes]) -> Iterator[SseEvent]:
    """Drive an ``SseParser`` from a sync iterable of byte chunks.

    Args:
        chunks: Iterable of byte chunks (typically ``response.body.iter_bytes()``).

    Yields:
        ``SseEvent`` records as they become complete.
    """
    parser = SseParser()
    for chunk in chunks:
        parser.feed(chunk)
        yield from parser.drain()
    yield from parser.end()


class AsyncSseStream:
    """Async iterator that drives an ``SseParser`` from an async byte stream.

    Construct via :func:`parse_async_events` or directly. Use as
    ``async for event in stream``.
    """

    __slots__ = ("_chunks", "_parser", "_pending")

    def __init__(self, chunks: AsyncIterable[bytes]) -> None:
        self._chunks = aiter(chunks)
        self._parser = SseParser()
        self._pending: Iterator[SseEvent] = iter(())

    def __aiter__(self) -> AsyncSseStream:
        return self

    async def __anext__(self) -> SseEvent:
        while True:
            try:
                return next(self._pending)
            except StopIteration:
                pass
            try:
                chunk = await self._chunks.__anext__()
            except StopAsyncIteration:
                self._pending = self._parser.end()
                try:
                    return next(self._pending)
                except StopIteration as err:
                    raise StopAsyncIteration from err
            self._parser.feed(chunk)
            self._pending = self._parser.drain()


def parse_async_events(chunks: AsyncIterable[bytes]) -> AsyncSseStream:
    """Build an async SSE event stream from an async byte iterable.

    Args:
        chunks: Async iterable of byte chunks (typically
            ``response.body.aiter_bytes()``).

    Returns:
        An async iterator of ``SseEvent`` records.
    """
    return AsyncSseStream(chunks)


__all__ = [
    "AsyncSseStream",
    "SseEvent",
    "SseParser",
    "parse_async_events",
    "parse_events",
]
