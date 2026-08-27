"""Decode Clerk tokens and Fuxam's React Flight responses."""

from __future__ import annotations

import base64
import json
import math
import re
from html.parser import HTMLParser
from typing import Any

from fuxam_errors import FuxamError

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_FLIGHT_DEPTH = 64
MAX_FLIGHT_NODES = 200_000
MAX_FLIGHT_STRING_BYTES = MAX_RESPONSE_BYTES


def as_object(value: Any, label: str = "response") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FuxamError(f"Fuxam returned an unsupported {label} shape.")
    return value


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON numbers are unsupported.")
    return number


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return as_object(
            json.loads(
                base64.urlsafe_b64decode(segment),
                parse_float=_finite_json_float,
                parse_constant=_finite_json_float,
            ),
            "token",
        )
    except (IndexError, ValueError, RecursionError) as exc:
        raise FuxamError("Clerk returned an invalid session token.") from exc


def _flight_records(body: bytes) -> dict[str, tuple[str, str]]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise FuxamError("Fuxam returned unexpectedly large action data.")
    records: dict[str, tuple[str, str]] = {}
    header_pattern = re.compile(rb"([0-9a-f]+):")
    text_pattern = re.compile(rb"T([0-9a-f]+),")
    offset = 0
    try:
        while offset < len(body):
            while offset < len(body) and body[offset] in (10, 13):
                offset += 1
            if offset >= len(body):
                break
            header = header_pattern.match(body, offset)
            if header is None:
                raise FuxamError("Fuxam returned malformed action data.")
            record_id = header.group(1).decode("ascii")
            if record_id in records:
                raise FuxamError("Fuxam returned malformed action data.")
            if len(records) >= MAX_FLIGHT_NODES:
                raise FuxamError("Fuxam returned unexpectedly complex action data.")
            offset = header.end()
            if offset < len(body) and body[offset] == ord("T"):
                text_header = text_pattern.match(body, offset)
                if text_header is None:
                    raise FuxamError("Fuxam returned malformed action data.")
                byte_length = int(text_header.group(1), 16)
                start = text_header.end()
                if byte_length > len(body) - start:
                    raise FuxamError("Fuxam returned truncated action data.")
                end = start + byte_length
                records[record_id] = ("text", body[start:end].decode("utf-8"))
                offset = end
            else:
                # Binary rows need length framing; never scan them as model JSON.
                if offset < len(body) and body[offset] in b"AOoUSsLlGgMmV":
                    raise FuxamError("Fuxam returned unsupported binary action data.")
                newline = body.find(b"\n", offset)
                end = len(body) if newline < 0 else newline
                records[record_id] = (
                    "model",
                    body[offset:end].decode("utf-8").rstrip("\r"),
                )
                offset = len(body) if newline < 0 else newline + 1
    except (UnicodeDecodeError, ValueError) as exc:
        raise FuxamError("Fuxam returned malformed action data.") from exc
    return records


def parse_flight(body: bytes) -> Any:
    """Decode the JSON/text subset of React Flight used by Fuxam actions."""
    records = _flight_records(body)
    decoded: dict[str, Any] = {}
    active: set[str] = set()
    nodes = 0
    string_bytes = 0

    def spend(depth: int, text: str | None = None) -> None:
        nonlocal nodes, string_bytes
        nodes += 1
        if text is not None:
            string_bytes += len(text.encode("utf-8"))
        if (
            depth > MAX_FLIGHT_DEPTH
            or nodes > MAX_FLIGHT_NODES
            or string_bytes > MAX_FLIGHT_STRING_BYTES
        ):
            raise FuxamError("Fuxam returned unexpectedly complex action data.")

    def model(record_id: str) -> Any:
        if record_id not in records or records[record_id][0] != "model":
            raise FuxamError("Fuxam returned unsupported action data.")
        if record_id not in decoded:
            payload = records[record_id][1]
            if payload.startswith("E"):
                raise FuxamError("Fuxam rejected the action request.")
            decoded[record_id] = json.loads(
                payload,
                parse_float=_finite_json_float,
                parse_constant=_finite_json_float,
            )
        return decoded[record_id]

    def resolve_record(record_id: str, depth: int) -> Any:
        if record_id not in records or record_id in active:
            raise FuxamError("Fuxam returned an unresolved or cyclic action reference.")
        if depth > MAX_FLIGHT_DEPTH:
            raise FuxamError("Fuxam returned unexpectedly complex action data.")
        kind, payload = records[record_id]
        if kind == "text":
            spend(depth, payload)
            return payload
        active.add(record_id)
        try:
            # Expand each occurrence: caching resolved trees hides explosive DAGs.
            return resolve(model(record_id), depth)
        finally:
            active.remove(record_id)

    def resolve(item: Any, depth: int) -> Any:
        spend(depth, item if isinstance(item, str) else None)
        if isinstance(item, str):
            if item.startswith("$$"):
                return item[1:]
            if item == "$undefined":
                return None
            match = re.fullmatch(r"\$([0-9a-f]+)", item)
            if match:
                return resolve_record(match.group(1), depth + 1)
            if item.startswith("$"):
                raise FuxamError("Fuxam returned an unsupported action value.")
            return item
        if isinstance(item, list):
            return [resolve(child, depth + 1) for child in item]
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                spend(depth + 1, key)
                result[key] = resolve(child, depth + 1)
            return result
        return item

    try:
        root = as_object(model("0"), "action")
        action = root.get("a")
        match = (
            re.fullmatch(r"\$@([0-9a-f]+)", action) if isinstance(action, str) else None
        )
        if match is None:
            raise FuxamError("Fuxam returned unsupported action data.")
        return resolve_record(match.group(1), 0)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FuxamError(
            "Fuxam returned unsupported or overly complex action data."
        ) from exc


class _FlightScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._in_script = False
        self._chunks: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "script":
            self._in_script = True
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._in_script:
            self.scripts.append("".join(self._chunks))
            self._in_script = False
            self._chunks = []


def _find_term_payloads(value: Any) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > 200_000:
            raise FuxamError("Fuxam returned unexpectedly complex term data.")
        if isinstance(item, dict):
            if "coursesByCategory" in item:
                matches.append(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return matches


def parse_term_page(body: bytes) -> dict[str, Any]:
    """Extract exactly one current-term booking payload from a Next.js page."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise FuxamError("Fuxam returned an unexpectedly large term page.")
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FuxamError("Fuxam returned malformed term page data.") from exc
    parser = _FlightScriptParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise FuxamError("Fuxam returned malformed term page data.") from exc

    if len(parser.scripts) > 10_000:
        raise FuxamError("Fuxam returned unexpectedly complex term page data.")
    flight_chunks: list[str] = []
    push_marker = "self.__next_f.push("
    decoder = json.JSONDecoder(
        parse_float=_finite_json_float, parse_constant=_finite_json_float
    )
    push_count = 0
    for script in parser.scripts:
        cursor = 0
        while cursor < len(script) and script[cursor].isspace():
            cursor += 1
        if not script.startswith(push_marker, cursor):
            continue
        while True:
            value_start = cursor + len(push_marker)
            while value_start < len(script) and script[value_start].isspace():
                value_start += 1
            try:
                push, value_end = decoder.raw_decode(script, value_start)
            except (ValueError, RecursionError) as exc:
                raise FuxamError("Fuxam returned malformed term page data.") from exc
            while value_end < len(script) and script[value_end].isspace():
                value_end += 1
            if value_end >= len(script) or script[value_end] != ")":
                raise FuxamError("Fuxam returned malformed term page data.")
            cursor = value_end + 1
            push_count += 1
            if push_count > 10_000:
                raise FuxamError("Fuxam returned unexpectedly complex term page data.")
            if not isinstance(push, list) or not push:
                raise FuxamError("Fuxam returned malformed term page data.")
            if type(push[0]) is int and push[0] == 1:
                if len(push) != 2 or not isinstance(push[1], str):
                    raise FuxamError("Fuxam returned malformed term page data.")
                flight_chunks.append(push[1])
            while cursor < len(script) and script[cursor].isspace():
                cursor += 1
            if cursor < len(script) and script[cursor] == ";":
                cursor += 1
                while cursor < len(script) and script[cursor].isspace():
                    cursor += 1
            if cursor == len(script):
                break
            if not script.startswith(push_marker, cursor):
                raise FuxamError("Fuxam returned malformed term page data.")

    try:
        stream = "".join(flight_chunks).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FuxamError("Fuxam returned malformed term page data.") from exc
    matches: list[dict[str, Any]] = []
    offset = 0
    # Next page streams include empty-ID control lines; unlike action responses,
    # this scanner must skip those lines while keeping byte-length framing strict.
    try:
        while offset < len(stream):
            while offset < len(stream) and stream[offset] in (10, 13):
                offset += 1
            if offset >= len(stream):
                break
            newline = stream.find(b"\n", offset)
            line_end = len(stream) if newline < 0 else newline
            colon = stream.find(b":", offset, line_end)
            if colon < 0 or colon == offset:
                offset = len(stream) if newline < 0 else newline + 1
                continue
            encoded_start = colon + 1
            if encoded_start < len(stream) and stream[encoded_start] == ord("T"):
                length_match = re.match(rb"T([0-9a-fA-F]+),", stream[encoded_start:])
                if not length_match:
                    raise FuxamError("Fuxam returned malformed term page data.")
                byte_length = int(length_match.group(1), 16)
                start = encoded_start + length_match.end()
                end = start + byte_length
                if end > len(stream):
                    raise FuxamError("Fuxam returned truncated term page data.")
                offset = end
                continue
            encoded = stream[encoded_start:line_end]
            offset = len(stream) if newline < 0 else newline + 1
            if b"coursesByCategory" not in encoded:
                continue
            stripped = encoded.lstrip()
            if not stripped.startswith((b"{", b"[")):
                continue
            try:
                value = json.loads(
                    stripped,
                    parse_float=_finite_json_float,
                    parse_constant=_finite_json_float,
                )
            except json.JSONDecodeError as exc:
                raise FuxamError("Fuxam returned malformed term page data.") from exc
            matches.extend(_find_term_payloads(value))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise FuxamError("Fuxam returned malformed term page data.") from exc
    if len(matches) != 1:
        raise FuxamError(
            "Fuxam returned no unique current-term booking payload; "
            "the frontend may have changed."
        )
    return matches[0]
