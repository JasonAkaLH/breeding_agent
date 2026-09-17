from __future__ import annotations

import json
import tempfile
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from .client import MCPProtocolError
from .protocol import MCPStreamEvent, json_rpc_message_kind
from .temporary_results import (
    MAX_DURABLE_MCP_RESULT_BYTES,
    MCPResultSink,
    MCPResultTooLargeError,
    MCPTemporaryResultRef,
    MCPTemporaryStorageExhaustedError,
)


MAX_CONTROL_RESULT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class MCPStreamingResponse:
    message: Mapping[str, Any]
    result_ref: MCPTemporaryResultRef | None = None
    event: MCPStreamEvent | None = None
    events: tuple[MCPStreamEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class _MCPResultTarget:
    response_id: str | int | None
    sink: MCPResultSink | None


class IncrementalJSONRPCResultParser:
    """Incremental JSON-RPC parser for transports that own their framing loop."""

    def __init__(
        self,
        sink: MCPResultSink | None = None,
        *,
        sink_selector: Callable[[Mapping[str, Any]], _MCPResultTarget | None] | None = None,
        max_envelope_bytes: int = 64 * 1024,
        max_provisional_result_bytes: int = MAX_DURABLE_MCP_RESULT_BYTES,
        require_response: bool = True,
        control_result_types: frozenset[str] = frozenset(),
    ) -> None:
        self._parser = _JSONRPCResultExtractor(
            sink,
            sink_selector=sink_selector,
            max_envelope_bytes=max_envelope_bytes,
            max_provisional_result_bytes=max_provisional_result_bytes,
            require_response=require_response,
            control_result_types=control_result_types,
        )

    async def feed(self, chunk: bytes) -> None:
        await self._parser.feed(chunk)

    async def finish(self) -> MCPStreamingResponse:
        return await self._parser.finish()

    async def abort(self) -> None:
        await self._parser.abort()


async def parse_json_rpc_byte_stream(
    chunks: AsyncIterable[bytes],
    sink: MCPResultSink,
    *,
    max_envelope_bytes: int = 64 * 1024,
    control_result_types: frozenset[str] = frozenset(),
) -> MCPStreamingResponse:
    parser = IncrementalJSONRPCResultParser(
        sink,
        max_envelope_bytes=max_envelope_bytes,
        control_result_types=control_result_types,
    )
    try:
        async for chunk in chunks:
            await parser.feed(bytes(chunk))
        return await parser.finish()
    except BaseException:
        await sink.abort()
        raise


async def parse_sse_json_rpc_byte_stream(
    chunks: AsyncIterable[bytes],
    sink: MCPResultSink,
    *,
    max_envelope_bytes: int = 64 * 1024,
    max_control_line_bytes: int = 16 * 1024,
    control_result_types: frozenset[str] = frozenset(),
) -> MCPStreamingResponse:
    parser = _SSEJSONRPCParser(
        sink,
        max_envelope_bytes=max_envelope_bytes,
        max_control_line_bytes=max_control_line_bytes,
        control_result_types=control_result_types,
    )
    try:
        async for chunk in chunks:
            result = await parser.feed(bytes(chunk))
            if result is not None:
                return result
        return await parser.finish()
    except BaseException:
        await sink.abort()
        raise


class _JSONRPCResultExtractor:
    def __init__(
        self,
        sink: MCPResultSink | None,
        *,
        sink_selector: Callable[[Mapping[str, Any]], _MCPResultTarget | None] | None = None,
        max_envelope_bytes: int,
        max_provisional_result_bytes: int = MAX_DURABLE_MCP_RESULT_BYTES,
        require_response: bool = True,
        control_result_types: frozenset[str] = frozenset(),
    ) -> None:
        if max_envelope_bytes <= 0:
            raise ValueError("max_envelope_bytes must be positive.")
        if max_provisional_result_bytes <= 0:
            raise ValueError("max_provisional_result_bytes must be positive.")
        self._sink = sink
        self._sink_selector = sink_selector
        self._result_target: _MCPResultTarget | None = None
        self._provisional_result = None
        self._provisional_result_size = 0
        self._max_provisional_result_bytes = max_provisional_result_bytes
        self._result_type = _TopLevelStringFieldDetector("resultType")
        self._control_result_types = control_result_types
        self._max_envelope_bytes = max_envelope_bytes
        self._require_response = require_response
        self._prefix = bytearray()
        self._suffix = bytearray()
        self._phase = "prefix"
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._unicode_remaining = 0
        self._capturing_key = False
        self._key = bytearray()
        self._current_key: str | None = None
        self._expecting_key = False
        self._awaiting_result = False
        self._result_kind: str | None = None
        self._result_depth = 0
        self._primitive = bytearray()
        self._result_complete = False

    async def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            if self._phase == "prefix":
                await self._feed_prefix(byte)
            elif self._phase == "result":
                await self._feed_result(byte)
            else:
                self._append_envelope(self._suffix, byte)

    async def finish(self) -> MCPStreamingResponse:
        if self._phase == "result" and self._result_kind == "primitive" and self._primitive:
            self._validate_primitive()
            self._result_complete = True
            self._phase = "suffix"
        if self._phase == "result" or self._awaiting_result:
            raise MCPProtocolError("MCP streaming JSON-RPC result ended before completion.")
        synthetic = bytes(self._prefix)
        if self._result_complete:
            synthetic += b"null"
        synthetic += bytes(self._suffix)
        try:
            payload = json.loads(synthetic)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MCPProtocolError("MCP streaming response must be valid JSON-RPC JSON.") from exc
        try:
            kind = json_rpc_message_kind(payload)
        except ValueError as exc:
            raise MCPProtocolError(str(exc)) from exc
        if self._require_response and kind != "response":
            raise MCPProtocolError("MCP streaming response must be a JSON-RPC response.")
        if self._sink_selector is not None and kind == "response":
            target = self._sink_selector(payload)
            if target is not None:
                self._result_target = target
                self._sink = target.sink
                if target.response_id is not None:
                    payload["id"] = target.response_id
        result_ref: MCPTemporaryResultRef | None = None
        if self._result_complete:
            if self._provisional_result is not None:
                await self._consume_provisional_result(payload)
            if self._sink is not None:
                assert self._sink is not None
                if self._result_type.value in self._control_result_types:
                    materialized = await self._sink.materialize(
                        max_bytes=MAX_CONTROL_RESULT_BYTES
                    )
                    await self._sink.abort()
                    try:
                        payload["result"] = json.loads(materialized)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise MCPProtocolError(
                            "MCP streaming control result is invalid JSON."
                        ) from exc
                else:
                    result_ref = await self._sink.finalize()
                    payload["result"] = {"_mcpResultRef": result_ref.as_payload()}
        elif self._sink is not None:
            await self._sink.abort()
        return MCPStreamingResponse(message=payload, result_ref=result_ref)

    async def abort(self) -> None:
        self._close_provisional_result()
        if self._sink is not None:
            await self._sink.abort()

    async def _feed_prefix(self, byte: int) -> None:
        if self._awaiting_result:
            if chr(byte).isspace():
                self._append_envelope(self._prefix, byte)
                return
            self._phase = "result"
            await self._start_result(byte)
            return

        self._append_envelope(self._prefix, byte)
        if self._in_string:
            if self._capturing_key:
                self._key.append(byte)
            if self._unicode_remaining:
                if chr(byte) not in "0123456789abcdefABCDEF":
                    raise MCPProtocolError("MCP streaming JSON contains an invalid unicode escape.")
                self._unicode_remaining -= 1
                return
            if self._escape:
                if byte == ord("u"):
                    self._unicode_remaining = 4
                elif byte not in b'"\\/bfnrt':
                    raise MCPProtocolError("MCP streaming JSON contains an invalid escape.")
                self._escape = False
                return
            if byte == ord("\\"):
                self._escape = True
                return
            if byte < 0x20:
                raise MCPProtocolError("MCP streaming JSON contains an unescaped control character.")
            if byte == ord('"'):
                self._in_string = False
                if self._capturing_key:
                    try:
                        self._current_key = json.loads(bytes(self._key))
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise MCPProtocolError("MCP streaming JSON object key is invalid.") from exc
                    self._capturing_key = False
                    self._expecting_key = False
                self._key.clear()
            return

        if byte == ord('"'):
            self._in_string = True
            if self._depth == 1 and self._expecting_key:
                self._capturing_key = True
                self._key = bytearray(b'"')
            return
        if byte == ord("{"):
            self._depth += 1
            if self._depth == 1:
                self._expecting_key = True
            return
        if byte == ord("["):
            self._depth += 1
            return
        if byte in (ord("}"), ord("]")):
            self._depth -= 1
            if self._depth < 0:
                raise MCPProtocolError("MCP streaming JSON has unbalanced delimiters.")
            return
        if self._depth == 1 and byte == ord(":"):
            if self._current_key == "result":
                self._awaiting_result = True
            self._current_key = None
            return
        if self._depth == 1 and byte == ord(","):
            self._expecting_key = True
            self._current_key = None

    async def _start_result(self, byte: int) -> None:
        self._awaiting_result = False
        if self._sink is None:
            if self._sink_selector is None:
                raise MCPProtocolError("MCP streaming result has no configured sink.")
            try:
                prefix_payload = json.loads(bytes(self._prefix) + b"null}")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise MCPProtocolError("MCP streaming JSON-RPC envelope metadata is invalid.") from exc
            target = self._sink_selector(prefix_payload)
            if target is not None:
                self._result_target = target
                self._sink = target.sink
            if self._sink is None:
                self._open_provisional_result()
        if byte in (ord("{"), ord("[")):
            self._result_kind = "container"
            self._result_depth = 1
        elif byte == ord('"'):
            self._result_kind = "string"
            self._in_string = True
        else:
            self._result_kind = "primitive"
            self._primitive.append(byte)
        await self._write_result(bytes((byte,)))

    async def _feed_result(self, byte: int) -> None:
        if self._result_kind == "primitive":
            if chr(byte).isspace() or byte in (ord(","), ord("}")):
                self._validate_primitive()
                self._complete_result()
                self._append_envelope(self._suffix, byte)
                return
            self._primitive.append(byte)
            if len(self._primitive) > self._max_envelope_bytes:
                raise MCPProtocolError("MCP streaming primitive result exceeded metadata limit.")
            await self._write_result(bytes((byte,)))
            return

        await self._write_result(bytes((byte,)))
        if self._in_string:
            if self._unicode_remaining:
                if chr(byte) not in "0123456789abcdefABCDEF":
                    raise MCPProtocolError("MCP streaming JSON contains an invalid unicode escape.")
                self._unicode_remaining -= 1
                return
            if self._escape:
                if byte == ord("u"):
                    self._unicode_remaining = 4
                elif byte not in b'"\\/bfnrt':
                    raise MCPProtocolError("MCP streaming JSON contains an invalid escape.")
                self._escape = False
                return
            if byte == ord("\\"):
                self._escape = True
                return
            if byte < 0x20:
                raise MCPProtocolError("MCP streaming JSON contains an unescaped control character.")
            if byte == ord('"'):
                self._in_string = False
                if self._result_kind == "string":
                    self._complete_result()
            return
        if byte == ord('"'):
            self._in_string = True
        elif byte in (ord("{"), ord("[")):
            self._result_depth += 1
        elif byte in (ord("}"), ord("]")):
            self._result_depth -= 1
            if self._result_depth < 0:
                raise MCPProtocolError("MCP streaming result has unbalanced delimiters.")
            if self._result_depth == 0:
                self._complete_result()

    def _complete_result(self) -> None:
        self._result_complete = True
        self._phase = "suffix"
        self._in_string = False
        self._escape = False
        self._unicode_remaining = 0

    def _validate_primitive(self) -> None:
        try:
            json.loads(bytes(self._primitive))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MCPProtocolError("MCP streaming primitive result is invalid JSON.") from exc

    def _append_envelope(self, target: bytearray, byte: int) -> None:
        target.append(byte)
        if len(self._prefix) + len(self._suffix) > self._max_envelope_bytes:
            raise MCPProtocolError("MCP streaming JSON-RPC envelope exceeded metadata limit.")

    async def _write_result(self, data: bytes) -> None:
        self._result_type.feed(data)
        if self._provisional_result is not None:
            self._write_provisional_result(data)
            return
        assert self._sink is not None
        await self._sink.write(data)

    def _open_provisional_result(self) -> None:
        try:
            self._provisional_result = tempfile.TemporaryFile()
        except OSError as exc:
            raise MCPTemporaryStorageExhaustedError() from exc

    def _write_provisional_result(self, data: bytes) -> None:
        if self._provisional_result_size + len(data) > self._max_provisional_result_bytes:
            self._close_provisional_result()
            raise MCPResultTooLargeError()
        try:
            self._provisional_result.write(data)
        except OSError as exc:
            self._close_provisional_result()
            raise MCPTemporaryStorageExhaustedError() from exc
        self._provisional_result_size += len(data)

    async def _consume_provisional_result(self, payload: dict[str, Any]) -> None:
        provisional = self._provisional_result
        assert provisional is not None
        try:
            provisional.seek(0)
            if self._sink is not None:
                while chunk := provisional.read(64 * 1024):
                    await self._sink.write(chunk)
            elif self._result_target is not None:
                try:
                    payload["result"] = json.loads(provisional.read())
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise MCPProtocolError("MCP streaming result is invalid JSON.") from exc
            else:
                payload["result"] = None
        except OSError as exc:
            raise MCPTemporaryStorageExhaustedError() from exc
        finally:
            self._close_provisional_result()

    def _close_provisional_result(self) -> None:
        provisional = self._provisional_result
        self._provisional_result = None
        self._provisional_result_size = 0
        if provisional is not None:
            try:
                provisional.close()
            except OSError:
                pass


class _TopLevelStringFieldDetector:
    """Find one top-level string field without buffering the JSON object."""

    def __init__(self, field_name: str) -> None:
        self._field_name = field_name
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._capture: str | None = None
        self._token = bytearray()
        self._expecting_key = False
        self._current_key: str | None = None
        self._awaiting_value = False
        self.value: str | None = None

    def feed(self, data: bytes) -> None:
        if self.value is not None:
            return
        for byte in data:
            if self._in_string:
                if self._capture is not None:
                    self._token.append(byte)
                if self._escape:
                    self._escape = False
                    continue
                if byte == ord("\\"):
                    self._escape = True
                    continue
                if byte != ord('"'):
                    continue
                self._in_string = False
                if self._capture is not None:
                    try:
                        decoded = json.loads(bytes(self._token))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        decoded = None
                    if self._capture == "key":
                        self._current_key = decoded if isinstance(decoded, str) else None
                        self._expecting_key = False
                    elif isinstance(decoded, str):
                        self.value = decoded
                    self._capture = None
                    self._token.clear()
                continue

            if byte == ord('"'):
                self._in_string = True
                if self._depth == 1 and self._expecting_key:
                    self._capture = "key"
                    self._token = bytearray(b'"')
                elif self._depth == 1 and self._awaiting_value:
                    self._capture = "value"
                    self._token = bytearray(b'"')
                    self._awaiting_value = False
                continue
            if chr(byte).isspace():
                continue
            if byte in (ord("{"), ord("[")):
                self._depth += 1
                if self._depth == 1 and byte == ord("{"):
                    self._expecting_key = True
                if self._awaiting_value:
                    self._awaiting_value = False
                continue
            if byte in (ord("}"), ord("]")):
                self._depth -= 1
                continue
            if self._depth == 1 and byte == ord(":"):
                self._awaiting_value = self._current_key == self._field_name
                continue
            if self._depth == 1 and byte == ord(","):
                self._expecting_key = True
                self._current_key = None
                self._awaiting_value = False


class _SSEJSONRPCParser:
    def __init__(
        self,
        sink: MCPResultSink,
        *,
        max_envelope_bytes: int,
        max_control_line_bytes: int,
        control_result_types: frozenset[str],
    ) -> None:
        self._sink = sink
        self._max_envelope_bytes = max_envelope_bytes
        self._control_result_types = control_result_types
        self._json = self._new_json_parser()
        self._events: list[MCPStreamEvent] = []
        self._max_control_line_bytes = max_control_line_bytes
        self._line_prefix = bytearray()
        self._field: str | None = None
        self._control_value = bytearray()
        self._data_started = False
        self._data_line_seen = False
        self._pending_data_newline = False
        self._skip_leading_space = False
        self._event_name: str | None = None
        self._event_id: str | None = None
        self._retry_ms: int | None = None
        self._saw_cr = False

    async def feed(self, chunk: bytes) -> MCPStreamingResponse | None:
        for byte in chunk:
            if self._saw_cr:
                self._saw_cr = False
                if byte == ord("\n"):
                    result = await self._end_line()
                    if result is not None:
                        return result
                    continue
                result = await self._end_line()
                if result is not None:
                    return result
            if byte == ord("\r"):
                self._saw_cr = True
                continue
            if byte == ord("\n"):
                result = await self._end_line()
                if result is not None:
                    return result
                continue
            await self._feed_line_byte(byte)
        return None

    async def finish(self) -> MCPStreamingResponse:
        if self._saw_cr or self._field is not None or self._line_prefix:
            result = await self._end_line()
            if result is not None:
                return result
        if not self._data_started:
            raise MCPProtocolError("MCP SSE stream ended before a JSON-RPC data event.")
        result = await self._finish_event()
        if result is None:
            raise MCPProtocolError("MCP SSE stream ended before a JSON-RPC response.")
        return result

    async def _feed_line_byte(self, byte: int) -> None:
        if self._field == "data":
            if self._skip_leading_space:
                self._skip_leading_space = False
                if byte == ord(" "):
                    return
            if self._pending_data_newline:
                await self._json.feed(b"\n")
                self._pending_data_newline = False
            self._data_started = True
            await self._json.feed(bytes((byte,)))
            return
        if self._field is not None:
            if self._skip_leading_space:
                self._skip_leading_space = False
                if byte == ord(" "):
                    return
            self._control_value.append(byte)
            if len(self._control_value) > self._max_control_line_bytes:
                raise MCPProtocolError("MCP SSE control line exceeded metadata limit.")
            return
        if byte == ord(":"):
            self._field = self._line_prefix.decode("ascii", errors="ignore")
            self._line_prefix.clear()
            self._skip_leading_space = True
            return
        self._line_prefix.append(byte)
        if len(self._line_prefix) > self._max_control_line_bytes:
            raise MCPProtocolError("MCP SSE field name exceeded metadata limit.")

    async def _end_line(self) -> MCPStreamingResponse | None:
        if self._field is None and not self._line_prefix:
            if self._data_started:
                return await self._finish_event()
            self._reset_line()
            return None
        if self._field == "data":
            self._data_line_seen = True
            if self._data_started:
                self._pending_data_newline = True
        elif self._field == "event":
            self._event_name = self._decode_control()
        elif self._field == "id":
            self._event_id = self._decode_control()
        elif self._field == "retry":
            try:
                self._retry_ms = int(self._decode_control())
            except ValueError:
                self._retry_ms = None
        self._reset_line()
        return None

    async def _finish_event(self) -> MCPStreamingResponse | None:
        parsed = await self._json.finish()
        event = MCPStreamEvent(
            event=self._event_name or "message",
            event_id=self._event_id,
            retry_ms=self._retry_ms,
            data="",
            message=parsed.message,
            is_priming=False,
        )
        self._events.append(event)
        try:
            kind = json_rpc_message_kind(parsed.message)
        except ValueError as exc:
            raise MCPProtocolError(str(exc)) from exc
        if kind == "response":
            if "result" not in parsed.message and parsed.result_ref is None:
                await self._sink.abort()
            return MCPStreamingResponse(
                message=parsed.message,
                result_ref=parsed.result_ref,
                event=event,
                events=tuple(self._events),
            )
        self._reset_event()
        return None

    def _new_json_parser(self) -> _JSONRPCResultExtractor:
        return _JSONRPCResultExtractor(
            None,
            sink_selector=lambda message: _MCPResultTarget(
                response_id=message.get("id"),
                sink=self._sink,
            ),
            max_envelope_bytes=self._max_envelope_bytes,
            require_response=False,
            control_result_types=self._control_result_types,
        )

    def _reset_event(self) -> None:
        self._json = self._new_json_parser()
        self._data_started = False
        self._data_line_seen = False
        self._pending_data_newline = False
        self._event_name = None
        self._event_id = None
        self._retry_ms = None

    def _decode_control(self) -> str:
        try:
            return self._control_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MCPProtocolError("MCP SSE control field must be UTF-8.") from exc

    def _reset_line(self) -> None:
        self._line_prefix.clear()
        self._field = None
        self._control_value.clear()
        self._skip_leading_space = False


__all__ = [
    "IncrementalJSONRPCResultParser",
    "MCPStreamingResponse",
    "parse_json_rpc_byte_stream",
    "parse_sse_json_rpc_byte_stream",
]
