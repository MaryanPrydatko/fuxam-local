from __future__ import annotations

import ast
import base64
import contextlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import unittest
import urllib.error
import urllib.parse
import urllib.request
from types import ModuleType
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents" / "skills" / "fuxam-local" / "scripts" / "fuxam.py"


def load_script_module(name: str, path: pathlib.Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load bundled module {name}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCRIPTS = SCRIPT.parent
load_script_module("fuxam_errors", SCRIPTS / "fuxam_errors.py")
credentials = load_script_module("fuxam_credentials", SCRIPTS / "fuxam_credentials.py")
protocol = load_script_module("fuxam_protocol", SCRIPTS / "fuxam_protocol.py")
fuxam = load_script_module("fuxam_local_cli", SCRIPT)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: int) -> FakeResponse:
        self.requests.append(request)
        if timeout != 30:
            raise AssertionError("Unexpected network timeout.")
        return FakeResponse(self.body)


class FakeSmokeClient:
    def context(self) -> dict[str, str]:
        return {
            "userId": "private-student-id",
            "cohortId": "private-cohort-id",
            "studyProgramVersionId": "private-program-id",
        }

    def enrolled(self, search: str) -> list[dict[str, str]]:
        if search:
            raise AssertionError("Smoke test must use an empty search.")
        return [{"title": "private-course-title"}]

    def study_plan(
        self, focus_id: str | None, term_id: str | None
    ) -> dict[str, object]:
        if focus_id is not None or term_id is not None:
            raise AssertionError("Smoke test must request the current plan.")
        return synthetic_study_plan()

    def agenda(
        self, direction: str, cursor: str | None, limit: int, past: bool
    ) -> list[dict[str, str]]:
        if (direction, cursor, limit, past) != ("initial", None, 1, False):
            raise AssertionError("Smoke test must use its bounded agenda request.")
        return [{"appointment": "private-appointment"}]

    def bookable(self, search: str, page: int, per_page: int) -> dict[str, object]:
        if (search, page, per_page) != ("", 1, 1):
            raise AssertionError("Smoke test must use its bounded catalog request.")
        return {
            "pageCount": 250,
            "totalCount": 250,
            "courses": [{"id": "course-synthetic", "title": "private-bookable-course"}],
        }

    def term_courses(self) -> dict[str, object]:
        return synthetic_term_payload()


def synthetic_study_plan() -> dict[str, object]:
    return {
        "availableTerms": [{"id": "term-fs26", "name": "Fall 2026"}],
        "electiveGroups": [
            {"name": "private-study-plan", "availableStudyPlanItems": []}
        ],
    }


def malformed_study_plans() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = [
        {},
        {"availableTerms": []},
        {"electiveGroups": []},
        {"availableTerms": None, "electiveGroups": []},
        {"availableTerms": [], "electiveGroups": {}},
        {"availableTerms": [], "electiveGroups": [None]},
        {"availableTerms": [], "electiveGroups": [{}]},
        {"availableTerms": [], "electiveGroups": [{"availableStudyPlanItems": None}]},
        {"availableTerms": [], "electiveGroups": [{"availableStudyPlanItems": [None]}]},
    ]
    terms = [None, [], 1, "private-invalid-term", {}, {"id": "private-term-id"}]
    terms.append({"name": "Fall 2026"})
    terms.extend(
        {"id": "private-term-id", "name": "Fall 2026", field: value}
        for field in ("id", "name")
        for value in ("", None, 0, False, [], {})
    )
    cases.extend({"availableTerms": [term], "electiveGroups": []} for term in terms)
    items = [{}] + [
        {"isElected": flag} for flag in (None, 0, 1, "true", "false", {}, [])
    ]
    cases.extend(
        {
            "availableTerms": [],
            "electiveGroups": [{"availableStudyPlanItems": [item]}],
        }
        for item in items
    )
    return cases


def malformed_catalog_pages() -> list[dict[str, object]]:
    cases: list[dict[str, object]] = [
        {},
        {"pageCount": 1},
        {"courses": []},
        {"pageCount": 0, "courses": [{"id": "course-synthetic"}]},
        {"pageCount": 0, "courses": [], "totalCount": 1},
        {"pageCount": 1, "courses": [], "learningUnits": [{"id": "other"}]},
        {
            "pageCount": 1,
            "courses": [{"id": "private-course-id"}],
            "totalCount": 0,
        },
        {"pageCount": 1, "courses": [], "totalCount": 1},
        {"pageCount": 1, "courses": [{"id": "course-a"}], "totalCount": 2},
        {"pageCount": 1, "courses": [{"id": "course-a"}] * 2, "totalCount": 2},
    ]
    cases.extend(
        {"pageCount": 1, "courses": value}
        for value in (
            None,
            {},
            "invalid",
            [None],
            [{}],
            [{"id": ""}],
            [{"id": "   "}],
            [{"id": 1}],
        )
    )
    cases.extend(
        {"pageCount": value, "courses": []}
        for value in (
            None,
            True,
            False,
            -1,
            1.5,
            "broken",
            {},
        )
    )
    cases.extend(
        {"pageCount": 1, "courses": [], "totalCount": value}
        for value in (None, True, -1, 1.5, "broken", {})
    )
    return cases


def synthetic_session(
    user_id: str = "student-synthetic",
    organization_id: str | None = None,
    *,
    expiry: object = 2_000_000_000,
) -> tuple[FakeResponse, FakeResponse]:
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": user_id, "exp": expiry}).encode()
    ).rstrip(b"=")
    token = ".".join(("header", payload.decode(), "signature"))
    client = {
        "response": {
            "last_active_session_id": "sess_synthetic",
            "sessions": [
                {
                    "id": "sess_synthetic",
                    "status": "active",
                    "last_active_organization_id": organization_id,
                    "last_active_token": {"jwt": "synthetic-renewal-token"},
                }
            ],
        }
    }
    return (
        FakeResponse(json.dumps(client).encode()),
        FakeResponse(json.dumps({"jwt": token}).encode()),
    )


def synthetic_course(
    course_id: str,
    name: str,
    *,
    full: bool = False,
    can_unbook: bool = False,
    waitlist_position: int | None = None,
) -> dict[str, object]:
    return {
        "id": course_id,
        "name": name,
        "layerId": f"layer-{course_id}",
        "institutionId": "institution-synthetic",
        "isEnrolled": can_unbook,
        "isSelfStudy": False,
        "booking": {
            "capacity": 20,
            "enrolledCount": 20 if full else 10,
            "isFull": full,
            "canUnbook": can_unbook,
            "waitlistPosition": waitlist_position,
            "enrollmentOrigin": "SELF_BOOKED" if can_unbook else None,
        },
    }


def synthetic_term_payload() -> dict[str, object]:
    return {
        "activeTerm": {"id": "term-fs26", "name": "Fall 2026"},
        "canBookCourses": True,
        "waitlistEnabled": True,
        "coursesByCategory": {
            "myCourses": [
                synthetic_course(
                    "course-enrolled",
                    "Synthetic Enrolled LU",
                    can_unbook=True,
                )
            ],
            "waitlist": [
                synthetic_course(
                    "course-waitlisted",
                    "Synthetic Waitlisted LU",
                    full=True,
                    waitlist_position=3,
                )
            ],
            "selfStudy": [],
            "bookable": [
                synthetic_course("course-open", "Synthetic Open LU"),
                synthetic_course(
                    "course-full",
                    "Synthetic Full LU",
                    full=True,
                ),
            ],
        },
    }


def synthetic_term_html(payload: dict[str, object] | None = None) -> bytes:
    flight = "a:" + json.dumps(payload or synthetic_term_payload()) + "\n"
    push = json.dumps([1, flight])
    return f"<html><script>self.__next_f.push({push})</script></html>".encode()


def synthetic_term_after(operation: str) -> dict[str, object]:
    payload = json.loads(json.dumps(synthetic_term_payload()))
    categories = payload["coursesByCategory"]
    transitions = {
        "enroll": ("bookable", "myCourses", "course-open"),
        "unenroll": ("myCourses", "bookable", "course-enrolled"),
        "join-waitlist": ("bookable", "waitlist", "course-full"),
        "leave-waitlist": ("waitlist", "bookable", "course-waitlisted"),
    }
    source, destination, course_id = transitions[operation]
    course = next(item for item in categories[source] if item["id"] == course_id)
    categories[source].remove(course)
    if operation == "enroll":
        course["isEnrolled"] = True
        course["booking"]["canUnbook"] = True
    elif operation == "unenroll":
        course["isEnrolled"] = False
        course["booking"]["canUnbook"] = False
    elif operation == "join-waitlist":
        course["booking"]["waitlistPosition"] = 4
    else:
        course["booking"]["waitlistPosition"] = None
    categories[destination].append(course)
    return payload


class ParsingTests(unittest.TestCase):
    def test_inline_flight_records_are_resolved(self) -> None:
        body = (
            b'0:{"a":"$@1"}\n'
            b'1:{"ok":true,"message":"$2","missing":"$undefined"}\n'
            b'2:"ready"\n'
        )

        self.assertEqual(
            fuxam.parse_flight(body),
            {"ok": True, "message": "ready", "missing": None},
        )

    def test_length_prefixed_flight_record_is_parsed(self) -> None:
        payload = b'{"items":[1,2]}'
        body = (
            b'0:{"a":"$@1"}\n' + b"1:T" + f"{len(payload):x}".encode() + b"," + payload
        )

        self.assertEqual(fuxam.parse_flight(body), payload.decode())

    def test_flight_resolves_model_records_recursively(self) -> None:
        body = (
            b'0:{"a":"$@1","unrelated":"$L9"}\n'
            b'1:{"object":"$2","list":"$3","again":"$2"}\n'
            b'2:{"nested":true,"value":"$4"}\n'
            b'3:["$4",null,4]\n4:"ready"\n'
        )
        self.assertEqual(
            fuxam.parse_flight(body),
            {
                "object": {"nested": True, "value": "ready"},
                "list": ["ready", None, 4],
                "again": {"nested": True, "value": "ready"},
            },
        )

    def test_flight_unescapes_model_strings_exactly_once(self) -> None:
        body = (
            b'0:{"a":"$@1"}\n'
            b'1:["$$2","$$undefined","$$$2","$undefined","","ordinary"]\n'
        )
        self.assertEqual(
            fuxam.parse_flight(body),
            ["$2", "$undefined", "$$2", None, "", "ordinary"],
        )

    def test_flight_text_uses_utf8_byte_lengths_and_stays_literal(self) -> None:
        value = '$$2\n0:{"a":"fake"}\né'
        encoded = value.encode()
        body = (
            b'0:{"a":"$@1"}\n1:{"text":"$2","after":"$3"}\n'
            + f"2:T{len(encoded):x},".encode()
            + encoded
            + b"3:true\n"
        )
        self.assertEqual(fuxam.parse_flight(body), {"text": value, "after": True})

    def test_flight_rejects_dangling_cycles_and_unsupported_tags(self) -> None:
        cases = (
            b'1:{"value":"$2"}\n',
            b'1:{"value":"$1"}\n',
            b'1:"$2"\n2:"$1"\n',
            b'1:{"value":"$Q2"}\n2:[]\n',
            b'1:{"value":"$2:key"}\n2:{"key":true}\n',
            b'1:{"value":"$D123"}\nd123:true\n',
            b'1:{"value":"$@2"}\n2:true\n',
            b'1:"$2"\n2:ready\n',
            b'1:E{"message":"private-action-detail"}\n',
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(fuxam.FuxamError) as raised:
                    fuxam.parse_flight(b'0:{"a":"$@1"}\n' + rows)
                self.assertNotIn("private-action-detail", str(raised.exception))

    def test_flight_rejects_unsupported_encoded_scalars(self) -> None:
        for scalar in (
            "$D2026-08-27T12:00:00.000Z",
            "$n12345678901234567890",
            "$Infinity",
            "$-Infinity",
            "$NaN",
            "$-0",
        ):
            body = b'0:{"a":"$@1"}\n1:' + json.dumps({"value": scalar}).encode()
            with self.subTest(scalar=scalar), self.assertRaises(fuxam.FuxamError):
                fuxam.parse_flight(body)

    def test_flight_rejects_nonfinite_json_numbers(self) -> None:
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e309", b"-1e309"):
            for rows in (
                b"1:" + number,
                b'1:{"private-number":[' + number + b"]}",
                b'1:"$2"\n2:' + number,
            ):
                with self.subTest(number=number, rows=rows):
                    with self.assertRaises(fuxam.FuxamError) as raised:
                        fuxam.parse_flight(b'0:{"a":"$@1"}\n' + rows)
                    self.assertNotIn("private-number", str(raised.exception))

    def test_flight_preserves_finite_numbers_and_literal_constants(self) -> None:
        body = b'0:{"a":"$@1"}\n1:[9007199254740993,-1.5,1e308,5e-324,"NaN","Infinity"]'
        self.assertEqual(
            fuxam.parse_flight(body),
            [9007199254740993, -1.5, 1e308, 5e-324, "NaN", "Infinity"],
        )

    def test_flight_rejects_invalid_headers_roots_and_text_framing(self) -> None:
        cases = (
            b'0:{"a":"$@not-hex"}\nnot-hex:true\n',
            b'0:{"a":"$@A"}\nA:true\n',
            b'0:{"a":1}\n1:true\n',
            b'0:{"a":"$@1"}\n1:T-1,x',
            b'0:{"a":"$@1"}\n1:T,',
            b'0:{"a":"$@1"}\n1:T1,\xc3',
            b'0:{"a":"$@1"}\n1:A1,\x00',
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(fuxam.FuxamError):
                fuxam.parse_flight(body)

    def test_flight_rejects_oversized_body_before_parsing(self) -> None:
        with mock.patch.object(protocol, "MAX_RESPONSE_BYTES", 4):
            with self.assertRaisesRegex(fuxam.FuxamError, "large"):
                fuxam.parse_flight(b'0:{"a":"$@1"}\n1:true\n')

    def test_flight_bounds_nested_models_and_reference_depth(self) -> None:
        nested = b'0:{"a":"$@1"}\n1:' + b"[" * 120 + b"0" + b"]" * 120
        chain = (
            b'0:{"a":"$@1"}\n'
            + b"".join(
                f'{index:x}:"${index + 1:x}"\n'.encode() for index in range(1, 120)
            )
            + b"78:true\n"
        )
        for body in (nested, chain):
            with self.subTest(kind="nested" if body is nested else "chain"):
                with self.assertRaises(fuxam.FuxamError):
                    fuxam.parse_flight(body)

    def test_flight_bounds_expanded_nodes_not_just_unique_records(self) -> None:
        body = (
            b'0:{"a":"$@1"}\n1:["$2","$2"]\n2:["$3","$3"]\n'
            b'3:["$4","$4"]\n4:[true,false]\n'
        )
        with mock.patch.object(protocol, "MAX_FLIGHT_NODES", 16):
            with self.assertRaisesRegex(fuxam.FuxamError, "complex"):
                fuxam.parse_flight(body)

    def test_flight_bounds_repeated_text_expansion(self) -> None:
        body = b'0:{"a":"$@1"}\n1:["$2","$2","$2","$2"]\n2:T20,' + b"x" * 32
        with mock.patch.object(protocol, "MAX_FLIGHT_STRING_BYTES", 96):
            with self.assertRaisesRegex(fuxam.FuxamError, "complex"):
                fuxam.parse_flight(body)

    def test_truncated_flight_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "truncated"):
            fuxam.parse_flight(b'0:{"a":"$@1"}\n1:T20,{}')

    def test_duplicate_flight_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "malformed"):
            fuxam.parse_flight(b'0:{"a":"$@1"}\n0:{}')

    def test_jwt_claims_decodes_urlsafe_payload(self) -> None:
        expected = {
            "sub": "user",
            "exp": 2_000_000_000.5,
            "large": 1e308,
            "small": 5e-324,
            "literal": "Infinity",
        }
        payload = base64.urlsafe_b64encode(json.dumps(expected).encode()).rstrip(b"=")
        token = ".".join(("header", payload.decode(), "signature"))
        self.assertEqual(fuxam.jwt_claims(token), expected)

    def test_jwt_claims_rejects_nonfinite_json_numbers(self) -> None:
        for number in (b"NaN", b"Infinity", b"-Infinity", b"1e309", b"-1e309"):
            for field in (b'"exp":' + number, b'"data":[' + number + b"]"):
                raw = b'{"sub":"private-user",' + field + b"}"
                payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
                token = f"header.{payload}.signature"
                with self.subTest(number=number, field=field):
                    with self.assertRaises(fuxam.FuxamError) as raised:
                        fuxam.jwt_claims(token)
                    self.assertNotIn("private-user", str(raised.exception))
                    self.assertNotIn(payload, str(raised.exception))

    def test_jwt_claims_handles_deep_json_without_a_traceback(self) -> None:
        depth = 1500
        raw = b'{"data":' + b"[" * depth + b"0" + b"]" * depth + b"}"
        payload = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        try:
            claims = fuxam.jwt_claims(f"header.{payload}.signature")
        except fuxam.FuxamError as exc:
            self.assertEqual(str(exc), "Clerk returned an invalid session token.")
        else:
            # Interpreter decoders differ in their nesting limit. Successful
            # decoding is allowed; an interpreter limit must be a FuxamError.
            value = claims["data"]
            for _ in range(depth):
                self.assertIsInstance(value, list)
                self.assertEqual(len(value), 1)
                value = value[0]
            self.assertEqual(value, 0)

    def test_term_page_finds_one_authoritative_flight_payload(self) -> None:
        expected = synthetic_term_payload()
        expected["sample"] = {"large": 1e308, "small": 5e-324, "literal": "NaN"}
        self.assertEqual(fuxam.parse_term_page(synthetic_term_html(expected)), expected)

    def test_term_page_reassembles_a_record_split_across_push_frames(self) -> None:
        record = "a:" + json.dumps(synthetic_term_payload()) + "\n"
        midpoint = len(record) // 2
        body = (
            "<script>self.__next_f.push("
            + json.dumps([1, record[:midpoint]])
            + ")</script>"
            + "<script>self.__next_f.push("
            + json.dumps([1, record[midpoint:]])
            + ")</script>"
        ).encode()

        self.assertEqual(fuxam.parse_term_page(body), synthetic_term_payload())

    def test_term_page_handles_multiple_pushes_in_one_script(self) -> None:
        record = "a:" + json.dumps(synthetic_term_payload()) + "\n"
        body = (
            "<script>self.__next_f.push([0]); self.__next_f.push("
            + json.dumps([1, record])
            + ");</script>"
        ).encode()

        self.assertEqual(fuxam.parse_term_page(body), synthetic_term_payload())

    def test_term_page_ignores_push_markers_in_strings_and_comments(self) -> None:
        stale = "a:" + json.dumps({"coursesByCategory": {}}) + "\n"
        decoys = (
            '<script>const marker="self.__next_f.push(";</script>',
            "<script>/* self.__next_f.push(" + json.dumps([1, stale]) + ") */</script>",
        )
        for decoy in decoys:
            body = (decoy + synthetic_term_html().decode()).encode()
            with self.subTest(decoy=decoy[:30]):
                self.assertEqual(fuxam.parse_term_page(body), synthetic_term_payload())

    def test_term_page_rejects_missing_duplicate_or_malformed_payloads(self) -> None:
        payload = synthetic_term_payload()
        one = synthetic_term_html(payload).decode()
        duplicate = (one + one).encode()
        cases = (
            b'<html><script>self.__next_f.push([1,"a:{}\\n"])</script></html>',
            duplicate,
            b'<script>self.__next_f.push([1,"a:{\\"coursesByCategory\\":]\\n"])</script>',
        )
        for body in cases:
            with self.subTest(body=body[:40]), self.assertRaises(fuxam.FuxamError):
                fuxam.parse_term_page(body)

    def test_term_page_rejects_unpaired_surrogate_pushes(self) -> None:
        for codepoint in (b"d800", b"dfff"):
            body = (
                b'<script>self.__next_f.push([1,"private-marker\\u'
                + codepoint
                + b'"])</script>'
            )
            with self.subTest(codepoint=codepoint):
                with self.assertRaises(fuxam.FuxamError) as raised:
                    fuxam.parse_term_page(body)
                self.assertNotIn("private-marker", str(raised.exception))

    def test_term_page_contains_push_json_decoder_errors(self) -> None:
        for push in (
            b"[1," + b"9" * 5000 + b"]",
            b"[1," + b"[" * 1500 + b"0" + b"]" * 1500 + b"]",
        ):
            body = b"<script>self.__next_f.push(" + push + b")</script>"
            with self.subTest(size=len(push)):
                with self.assertRaises(fuxam.FuxamError) as raised:
                    fuxam.parse_term_page(body)
                self.assertNotIn(push.decode(), str(raised.exception))

    def test_term_page_handles_deep_models_without_a_traceback(self) -> None:
        expected = synthetic_term_payload()
        record = "a:" + "[" * 1500 + json.dumps(expected) + "]" * 1500 + "\n"
        body = (
            "<script>self.__next_f.push(" + json.dumps([1, record]) + ")</script>"
        ).encode()
        try:
            result = fuxam.parse_term_page(body)
        except fuxam.FuxamError as exc:
            self.assertEqual(str(exc), "Fuxam returned malformed term page data.")
        else:
            self.assertEqual(result, expected)

    def test_term_page_rejects_nonfinite_json_numbers(self) -> None:
        for number in ("NaN", "Infinity", "-Infinity", "1e309", "-1e309"):
            record = 'a:{"coursesByCategory":{},"private-number":[' + number + "]}\n"
            model = (
                "<script>self.__next_f.push(" + json.dumps([1, record]) + ")</script>"
            ).encode()
            outer = (
                "<script>self.__next_f.push([0," + number + "])</script>"
            ).encode() + synthetic_term_html()
            for label, body in (("model", model), ("outer", outer)):
                with self.subTest(number=number, label=label):
                    with self.assertRaises(fuxam.FuxamError) as raised:
                        fuxam.parse_term_page(body)
                    self.assertNotIn("private-number", str(raised.exception))

    def test_term_page_ignores_other_flight_channels(self) -> None:
        record = "a:" + json.dumps(synthetic_term_payload()) + "\n"
        for channel in (0, 2, -1, True, False, 1.0, "1", None, {}, []):
            ignored = "self.__next_f.push(" + json.dumps([channel, record]) + ");"
            body = ("<script>" + ignored + "</script>").encode()
            with (
                self.subTest(channel=channel),
                self.assertRaisesRegex(fuxam.FuxamError, "current-term"),
            ):
                fuxam.parse_term_page(body)

            body = (
                "<script>"
                + ignored
                + "self.__next_f.push("
                + json.dumps([1, record])
                + ");</script>"
            ).encode()
            with self.subTest(channel=channel, followed_by_data=True):
                self.assertEqual(fuxam.parse_term_page(body), synthetic_term_payload())

    def test_term_page_rejects_malformed_data_pushes(self) -> None:
        for push in ([1], [1, None], [1, 0], [1, []], [1, {}], [1, "", "extra"]):
            body = (
                "<script>self.__next_f.push(" + json.dumps(push) + ")</script>"
            ).encode() + synthetic_term_html()
            with (
                self.subTest(push=push),
                self.assertRaisesRegex(fuxam.FuxamError, "malformed term page"),
            ):
                fuxam.parse_term_page(body)

    def test_term_page_ignores_length_prefixed_decoy_payload(self) -> None:
        decoy = ("ignore\na:" + json.dumps({"coursesByCategory": {}})).encode()
        flight = (
            f"9:T{len(decoy):x}," + decoy.decode() + "\n"
            "a:" + json.dumps(synthetic_term_payload()) + "\n"
        )
        body = (
            "<script>self.__next_f.push(" + json.dumps([1, flight]) + ")</script>"
        ).encode()

        self.assertEqual(fuxam.parse_term_page(body), synthetic_term_payload())

    def test_term_page_rejects_oversized_input_before_parsing(self) -> None:
        with (
            mock.patch.object(protocol, "MAX_RESPONSE_BYTES", 4),
            self.assertRaisesRegex(fuxam.FuxamError, "large"),
        ):
            fuxam.parse_term_page(b"12345")


class TerminalSummaryTests(unittest.TestCase):
    def test_modules_reject_missing_collections_or_non_boolean_elections(self) -> None:
        for value in malformed_study_plans():
            with self.subTest(value=value):
                with self.assertRaises(fuxam.FuxamError) as raised:
                    fuxam.summarize_modules(value, "FS26")
                self.assertNotIn("private-", str(raised.exception))

    def test_modules_reject_conflicting_term_ids_in_either_order(self) -> None:
        for names in (
            ("Spring 2026", "Fall 2026"),
            ("Fall 2026", "Spring 2026"),
        ):
            payload = {
                "availableTerms": [
                    {"id": "term-synthetic", "name": name} for name in names
                ],
                "electiveGroups": [
                    {
                        "availableStudyPlanItems": [
                            {
                                "isElected": True,
                                "electedTermIds": ["term-synthetic"],
                                "moduleVersion": {
                                    "courseModule": {"name": "SE_01: Synthetic Module"}
                                },
                            }
                        ]
                    }
                ],
            }
            for term in ("FS26", "SS26", None):
                with (
                    self.subTest(names=names, term=term),
                    self.assertRaisesRegex(
                        fuxam.FuxamError, "conflicting study-plan term"
                    ),
                ):
                    fuxam.summarize_modules(payload, term)

    def test_modules_accept_equivalent_names_for_the_same_term_id(self) -> None:
        payload = synthetic_study_plan()
        payload["availableTerms"] = [
            {"id": "term-fs26", "name": name}
            for name in ("Fall 2026", "FS26", "Fall Semester 2026")
        ]

        summary = fuxam.summarize_modules(payload, "FS26")

        self.assertTrue(summary["complete"])
        self.assertEqual(summary["availableTerms"], ["FS26"])

    def test_modules_accept_explicit_empty_and_unelected_records(self) -> None:
        for groups in (
            [],
            [{"availableStudyPlanItems": []}],
            [{"availableStudyPlanItems": [{"isElected": False}]}],
        ):
            with self.subTest(groups=groups):
                summary = fuxam.summarize_modules(
                    {
                        "availableTerms": [],
                        "electiveGroups": groups,
                    },
                    "FS26",
                )
                self.assertTrue(summary["complete"])
                self.assertEqual(summary["total"], 0)

    def test_term_aliases_are_canonicalized(self) -> None:
        cases = {
            "FS26": "FS26",
            "fs 2026": "FS26",
            "Fall 2026": "FS26",
            "Fall Semester 2026": "FS26",
            "SS26": "SS26",
            "Spring 2026": "SS26",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(fuxam.canonical_term(value), expected)
        with self.assertRaisesRegex(fuxam.FuxamError, "unambiguous term"):
            fuxam.canonical_term("2026")

    def test_enrolled_summary_keeps_evidence_separate(self) -> None:
        payload = {
            "courses": [
                {
                    "id": "private-learning-unit-a",
                    "name": "Synthetic Bound Unit (SE_99)",
                    "status": "ACTIVE",
                    "courseTags": [{"tag": {"name": "Offered in FS26"}}],
                    "modules": [
                        {
                            "id": "private-module-a",
                            "code": "SE_01",
                            "name": "SE_01: Synthetic Explicit Module",
                        }
                    ],
                },
                {
                    "id": "private-learning-unit-b",
                    "name": "Synthetic Reading Group (STS_02, STS_03)",
                    "status": "ACTIVE",
                    "courseTags": [{"tag": {"name": "Offered in FS26"}}],
                    "modules": [],
                },
                {
                    "id": "private-learning-unit-c",
                    "name": "Synthetic Spring Unit (SE_42)",
                    "status": "ACTIVE",
                    "courseTags": [{"tag": {"name": "Offered in SS26"}}],
                    "modules": [],
                },
                {
                    "id": "private-learning-unit-d",
                    "name": "Synthetic Inactive Unit (SE_77)",
                    "status": "ARCHIVED",
                    "courseTags": [{"tag": {"name": "Offered in FS26"}}],
                    "modules": [],
                },
            ]
        }

        summary = fuxam.summarize_enrolled(payload, "Fall 2026")

        self.assertEqual(summary["term"], "FS26")
        self.assertEqual(summary["total"], 2)
        bound, advertised = summary["learningUnits"]
        self.assertEqual(
            bound["explicitModuleAssociations"],
            [{"code": "SE_01", "name": "SE_01: Synthetic Explicit Module"}],
        )
        self.assertEqual(bound["titleOnlyModuleCodes"], ["SE_99"])
        self.assertEqual(advertised["explicitModuleAssociations"], [])
        self.assertEqual(advertised["titleOnlyModuleCodes"], ["STS_02", "STS_03"])
        serialized = json.dumps(summary)
        self.assertNotIn("private-learning-unit", serialized)
        self.assertNotIn("private-module", serialized)

    def test_term_summary_reports_authoritative_booking_categories(self) -> None:
        result = fuxam.summarize_term_courses(synthetic_term_payload(), "FS26")

        self.assertEqual(result["kind"], "active-term-learning-unit-bookings")
        self.assertEqual(result["term"], "FS26")
        self.assertEqual(result["termName"], "Fall 2026")
        self.assertEqual(result["counts"]["enrolled"], 1)
        self.assertEqual(result["counts"]["waitlisted"], 1)
        self.assertEqual(result["counts"]["bookable"], 1)
        self.assertEqual(result["counts"]["full"], 1)
        states = {item["courseId"]: item["state"] for item in result["learningUnits"]}
        self.assertEqual(states["course-enrolled"], "ENROLLED")
        self.assertEqual(states["course-waitlisted"], "WAITLISTED")
        self.assertEqual(states["course-open"], "BOOKABLE")
        self.assertEqual(states["course-full"], "FULL")
        self.assertTrue(result["complete"])

    def test_term_summary_reports_valid_self_study_courses(self) -> None:
        payload = synthetic_term_payload()
        self_study = synthetic_course(
            "course-self-study",
            "Synthetic Self-Study LU",
        )
        self_study["isSelfStudy"] = True
        payload["coursesByCategory"]["selfStudy"].append(self_study)

        result = fuxam.summarize_term_courses(payload, "FS26")

        self.assertEqual(result["counts"]["selfStudy"], 1)
        states = {item["courseId"]: item["state"] for item in result["learningUnits"]}
        self.assertEqual(states["course-self-study"], "SELF_STUDY")

    def test_term_summary_rejects_a_non_active_requested_term(self) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "TERM_NOT_ACTIVE"):
            fuxam.summarize_term_courses(synthetic_term_payload(), "SS26")

    def test_explicit_empty_term_is_rejected_instead_of_changing_data_source(
        self,
    ) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "unambiguous term"):
            fuxam.summarize_term_courses(synthetic_term_payload(), "")
        with self.assertRaisesRegex(fuxam.FuxamError, "unambiguous term"):
            fuxam.summarize_enrolled({"courses": []}, "")
        with self.assertRaisesRegex(fuxam.FuxamError, "unambiguous term"):
            fuxam.summarize_modules({}, "")

        client = mock.Mock()
        client.term_courses.return_value = synthetic_term_payload()
        args = fuxam.build_parser().parse_args(["enrolled", "--term", ""])
        with (
            mock.patch.object(fuxam, "FuxamClient", return_value=client),
            self.assertRaisesRegex(fuxam.FuxamError, "unambiguous term"),
        ):
            fuxam.run(args)

        client.term_courses.assert_called_once_with()
        client.enrolled.assert_not_called()

    def test_term_summary_rejects_duplicate_ids_and_schema_drift(self) -> None:
        duplicate = synthetic_term_payload()
        duplicate["coursesByCategory"]["bookable"].append(
            synthetic_course("course-enrolled", "Duplicate")
        )
        bad_bool = synthetic_term_payload()
        bad_bool["canBookCourses"] = 1
        bad_count = synthetic_term_payload()
        bad_count["coursesByCategory"]["bookable"][0]["booking"]["capacity"] = True
        unknown_category = synthetic_term_payload()
        unknown_category["coursesByCategory"]["newCategory"] = []
        misclassified = synthetic_term_payload()
        misclassified["coursesByCategory"]["myCourses"][0]["isEnrolled"] = False
        malformed_self_study = synthetic_term_payload()
        malformed_self_study["coursesByCategory"]["selfStudy"].append(
            synthetic_course("course-self-study", "Malformed Self-Study LU")
        )
        missing_waitlist_position = synthetic_term_payload()
        missing_waitlist_position["coursesByCategory"]["waitlist"][0]["booking"][
            "waitlistPosition"
        ] = None
        unexpected_waitlist_position = synthetic_term_payload()
        unexpected_waitlist_position["coursesByCategory"]["bookable"][0]["booking"][
            "waitlistPosition"
        ] = 1

        for payload in (
            duplicate,
            bad_bool,
            bad_count,
            unknown_category,
            misclassified,
            malformed_self_study,
            missing_waitlist_position,
            unexpected_waitlist_position,
        ):
            with self.subTest(payload=payload), self.assertRaises(fuxam.FuxamError):
                fuxam.summarize_term_courses(payload)

    def test_enrolled_term_cli_uses_authoritative_term_page(self) -> None:
        payload = synthetic_term_payload()

        def invoke(output_format: str) -> tuple[int, str, str]:
            client = mock.Mock()
            client.term_courses.return_value = payload
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                str(SCRIPT),
                "enrolled",
                "--term",
                "Fall 2026",
                "--format",
                output_format,
            ]
            with (
                mock.patch.object(fuxam, "FuxamClient", return_value=client),
                mock.patch.object(fuxam.sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = fuxam.main()
            return code, stdout.getvalue(), stderr.getvalue()

        json_code, json_output, json_error = invoke("json")
        result = json.loads(json_output)
        self.assertEqual(json_code, 0)
        self.assertEqual(json_error, "")
        self.assertEqual(result["kind"], "active-term-learning-unit-enrollments")
        self.assertEqual(result["counts"]["enrolled"], 1)

        table_code, table_output, table_error = invoke("table")
        self.assertEqual(table_code, 0)
        self.assertEqual(table_error, "")
        self.assertIn("Enrolled", table_output)
        self.assertIn("Synthetic Enrolled LU", table_output)
        self.assertIn("Waitlisted", table_output)
        self.assertNotIn("offering tag", table_output)

    def test_modules_summary_filters_formal_elections_by_term(self) -> None:
        def item(
            code: str,
            *,
            elected: bool,
            term: str | None = None,
            term_ids: list[str] | None = None,
        ) -> dict[str, object]:
            return {
                "isElected": elected,
                "electedTermIds": term_ids or [],
                "organizationTermName": term,
                "isCoreModule": code == "SE_01",
                "moduleVersion": {
                    "name": f"{code}: Synthetic Version",
                    "ectsPoints": 5,
                    "courseModule": {"name": f"{code}: Synthetic Module"},
                },
            }

        payload = {
            "availableTerms": [
                {"id": "term-fs26", "name": "Fall Semester 2026"},
                {"id": "term-ss26", "name": "Spring Semester 2026"},
            ],
            "electiveGroups": [
                {
                    "availableStudyPlanItems": [
                        item("SE_01", elected=True, term="Fall Semester 2026"),
                        item("SE_02", elected=False, term="Fall Semester 2026"),
                        item("SE_03", elected=True, term="Spring Semester 2026"),
                        item("PM_24/BM_24", elected=True, term_ids=["term-fs26"]),
                        item("SE_05", elected=True, term_ids=["unknown-term"]),
                    ]
                }
            ],
        }

        summary = fuxam.summarize_modules(payload, "FS26")

        self.assertEqual(summary["term"], "FS26")
        self.assertEqual(summary["total"], 2)
        self.assertEqual(
            [module["code"] for module in summary["modules"]],
            ["PM_24/BM_24", "SE_01"],
        )
        self.assertEqual(summary["modules"][0]["codes"], ["PM_24", "BM_24"])
        self.assertTrue(all(module["formalElection"] for module in summary["modules"]))
        self.assertEqual(summary["unresolvedTermRecords"], 1)
        self.assertFalse(summary["complete"])

    def test_modules_do_not_confirm_ambiguous_or_unreadable_elections(self) -> None:
        def version(code: str) -> dict[str, object]:
            return {
                "name": f"{code}: Synthetic Version",
                "ectsPoints": 5,
                "courseModule": {"name": f"{code}: Synthetic Module"},
            }

        payload = {
            "availableTerms": [
                {"id": "term-fs26", "name": "Fall Semester 2026"},
                {"id": "term-ss26", "name": "Spring Semester 2026"},
            ],
            "electiveGroups": [
                {
                    "availableStudyPlanItems": [
                        {
                            "isElected": True,
                            "organizationTermName": "Fall Semester 2026",
                            "organizationTerm": {"name": "Spring Semester 2026"},
                            "electedTermIds": ["term-fs26", "term-ss26"],
                            "moduleVersion": version("SE_06"),
                        },
                        {
                            "isElected": True,
                            "electedTermIds": ["unknown-term"],
                            "moduleVersion": version("SE_07"),
                        },
                        {
                            "isElected": True,
                            "organizationTermName": "Fall Semester 2026",
                            "electedTermIds": [],
                            "moduleVersion": None,
                        },
                    ]
                }
            ],
        }

        summary = fuxam.summarize_modules(payload, "FS26")

        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["unresolvedTermRecords"], 2)
        self.assertEqual(summary["unreadableElectionRecords"], 1)
        self.assertFalse(summary["complete"])
        output = fuxam.render_terminal_result("modules", summary)
        self.assertIn("No confirmed formal module elections", output)
        self.assertIn("Warning:", output)
        self.assertIn("conflicting term sources", output)

    def test_enrolled_schema_drift_is_reported(self) -> None:
        payload = {
            "courses": [
                "unsupported",
                {"status": "ACTIVE", "name": None},
                {
                    "status": "ACTIVE",
                    "name": "Synthetic Unit",
                    "courseTags": "unsupported",
                    "modules": "unsupported",
                },
            ]
        }

        summary = fuxam.summarize_enrolled(payload)

        self.assertEqual(summary["total"], 1)
        self.assertFalse(summary["complete"])
        self.assertTrue(summary["warnings"])
        output = fuxam.render_terminal_result("enrolled", summary)
        self.assertIn("results may be incomplete", output)

    def test_terminal_tables_are_clear_and_sanitize_cells(self) -> None:
        enrolled = {
            "term": "FS26",
            "total": 1,
            "learningUnits": [
                {
                    "name": "Synthetic\nUnit\tName",
                    "status": "ACTIVE",
                    "offeringTerms": ["FS26"],
                    "explicitModuleAssociations": [
                        {"code": "SE_01", "name": "Synthetic"}
                    ],
                    "titleOnlyModuleCodes": ["SE_02"],
                }
            ],
        }

        table = fuxam.render_terminal_result("enrolled", enrolled)

        self.assertIn("Learning unit", table)
        self.assertIn("Explicit associations", table)
        self.assertIn("Title-only codes", table)
        self.assertIn("Synthetic Unit Name", table)
        self.assertNotIn("Synthetic\nUnit", table)
        self.assertIn("not formal elections", table)

        hostile = fuxam._clean_cell("Course\x1b]52;c;SECRET\x07\u0085\u202e|Name")
        for control in ("\x1b", "\x07", "\u0085", "\u202e", "|"):
            self.assertNotIn(control, hostile)
        self.assertIn("¦", hostile)

        empty = fuxam.render_terminal_result(
            "modules", {"term": "FS26", "total": 0, "modules": []}
        )
        self.assertIn("No formal module elections found for FS26.", empty)

        all_terms = fuxam.render_terminal_result(
            "enrolled",
            {"term": None, "total": 1, "learningUnits": enrolled["learningUnits"]},
        )
        self.assertIn("ACTIVE learning-unit records:", all_terms)
        self.assertIn("not proof of unfinished work", all_terms)
        self.assertIn("Use `modules`", all_terms)
        self.assertNotIn("--term all terms", all_terms)

    def test_enrolled_without_summary_options_returns_raw_payload(self) -> None:
        payload = {"courses": [{"private": "unchanged"}], "total": 1}
        client = mock.Mock()
        client.enrolled.return_value = payload
        args = fuxam.build_parser().parse_args(["enrolled"])

        with mock.patch.object(fuxam, "FuxamClient", return_value=client):
            result = fuxam.run(args)

        self.assertIs(result, payload)
        client.enrolled.assert_called_once_with("")

    def test_enrolled_and_modules_expose_json_or_table_formats(self) -> None:
        parser = fuxam.build_parser()
        enrolled = parser.parse_args(
            ["enrolled", "--term", "Fall 2026", "--format", "table"]
        )
        modules = parser.parse_args(["modules", "--term", "FS26", "--format", "json"])

        self.assertEqual(enrolled.term, "Fall 2026")
        self.assertEqual(enrolled.output_format, "table")
        self.assertEqual(modules.term, "FS26")
        self.assertEqual(modules.output_format, "json")

    def test_enrolled_term_rejects_the_raw_record_search_option(self) -> None:
        args = fuxam.build_parser().parse_args(
            ["enrolled", "--term", "FS26", "--search", "needle"]
        )
        client = mock.Mock()

        with (
            mock.patch.object(fuxam, "FuxamClient", return_value=client),
            self.assertRaisesRegex(fuxam.FuxamError, "cannot be combined"),
        ):
            fuxam.run(args)

        client.term_courses.assert_not_called()


class CredentialAndNetworkTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Requires POSIX terminal semantics.")
    def test_auth_set_real_getpass_rejects_a_process_without_a_controlling_tty(
        self,
    ) -> None:
        code = textwrap.dedent(
            """
            import os
            import pathlib
            import sys
            from unittest import mock

            try:
                tty = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            except OSError:
                pass
            else:
                os.close(tty)
                raise AssertionError("The child still has a controlling terminal.")

            sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
            import fuxam as cli
            keychain = mock.Mock(storage="macOS Keychain")
            cli.credential_store = mock.Mock(return_value=keychain)
            sys.argv = [sys.argv[1], "auth", "set"]
            status = cli.main()
            keychain.set.assert_not_called()
            if sys.stdin.read() != "synthetic-private-cookie\\n":
                raise AssertionError("Credential entry consumed visible stdin.")
            raise SystemExit(status)
            """
        )
        result = subprocess.run(  # noqa: S603 - isolated Python, synthetic input only.
            [sys.executable, "-c", code, str(SCRIPT)],
            input="synthetic-private-cookie\n",
            text=True,
            capture_output=True,
            start_new_session=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("interactive", json.loads(result.stderr)["error"])
        self.assertNotIn("synthetic-private-cookie", result.stderr)

    def test_auth_set_rejects_visible_stdin_fallback_before_reading(self) -> None:
        keychain = mock.Mock(storage="macOS Keychain")
        stdin = io.StringIO("synthetic-private-cookie\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(fuxam, "credential_store", return_value=keychain),
            mock.patch.object(fuxam.sys, "argv", [str(SCRIPT), "auth", "set"]),
            mock.patch.object(fuxam.sys, "stdin", stdin),
            mock.patch.object(
                fuxam.getpass, "getpass", side_effect=fuxam.getpass.fallback_getpass
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = fuxam.main()

        self.assertEqual(code, 1)
        self.assertEqual(stdin.tell(), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("interactive", json.loads(stderr.getvalue())["error"])
        self.assertNotIn("synthetic-private-cookie", stderr.getvalue())
        keychain.set.assert_not_called()

    def test_auth_set_stores_only_the_hidden_normalized_value(self) -> None:
        keychain = mock.Mock(storage="macOS Keychain")
        with (
            mock.patch.object(fuxam, "credential_store", return_value=keychain),
            mock.patch.object(
                fuxam.getpass, "getpass", return_value="__client=synthetic-cookie"
            ),
        ):
            result = fuxam.run(fuxam.build_parser().parse_args(["auth", "set"]))

        keychain.set.assert_called_once_with("synthetic-cookie")
        self.assertTrue(result["configured"])
        self.assertNotIn("synthetic-cookie", json.dumps(result))

    def test_cookie_normalization_accepts_value_or_cookie_prefix(self) -> None:
        self.assertEqual(fuxam.normalize_client_cookie("abc_DEF-123"), "abc_DEF-123")
        self.assertEqual(
            fuxam.normalize_client_cookie("__client=abc_DEF-123; Path=/"),
            "abc_DEF-123",
        )

    def test_cookie_normalization_rejects_control_characters(self) -> None:
        with self.assertRaises(fuxam.FuxamError):
            fuxam.normalize_client_cookie("abc\ndef")

    def test_origin_rejects_http_userinfo_and_invalid_ports(self) -> None:
        bad_urls = (
            "http://fuxam.app/path",
            "https://user@fuxam.app/path",
            "https://:pass@fuxam.app/path",
            "https://fuxam.app:not-a-port/path",
        )
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(fuxam.FuxamError):
                fuxam.origin(url)

    def test_cross_origin_redirect_is_rejected(self) -> None:
        request = urllib.request.Request("https://fuxam.app/start")
        handler = fuxam.SameOriginRedirectHandler()

        with self.assertRaisesRegex(fuxam.FuxamError, "cross-origin"):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://example.com/steal",
            )

    def test_open_rejects_hosts_ports_and_misrouted_credentials(self) -> None:
        client = fuxam.FuxamClient()
        calls = (
            lambda: client._open("https://example.com", authenticated=False),
            lambda: client._open("https://fuxam.app:444", authenticated=False),
            lambda: client._open("https://clerk.fuxam.app", authenticated=True),
            lambda: client._open(
                "https://fuxam.app", headers={"cookie": "opaque"}, authenticated=False
            ),
        )
        for call in calls:
            with self.subTest(call=call), self.assertRaises(fuxam.FuxamError):
                call()

    def test_bearer_token_is_attached_only_after_policy_checks(self) -> None:
        opener = FakeOpener()
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "_session_token", return_value="opaque-value"),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
        ):
            client._open("https://fuxam.app/api/test", require_context=False)

        headers = {
            name.lower(): value for name, value in opener.requests[0].header_items()
        }
        self.assertEqual(headers["authorization"], "Bearer opaque-value")
        self.assertNotIn("cookie", headers)

    def test_session_token_encodes_a_missing_organization_as_blank(self) -> None:
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "synthetic-user", "exp": 2_000_000_000}).encode()
        ).rstrip(b"=")
        session_token = ".".join(("header", payload.decode(), "signature"))
        clerk_client = {
            "response": {
                "last_active_session_id": "sess_synthetic",
                "sessions": [
                    {
                        "id": "sess_synthetic",
                        "status": "active",
                        "last_active_organization_id": None,
                        "last_active_token": {"jwt": "synthetic-renewal-token"},
                    }
                ],
            }
        }
        opener = mock.Mock()
        opener.open.side_effect = (
            FakeResponse(json.dumps(clerk_client).encode()),
            FakeResponse(json.dumps({"jwt": session_token}).encode()),
        )
        keychain = mock.Mock(storage="macOS Keychain")
        keychain.get.return_value = "synthetic-client-cookie"

        with (
            mock.patch.object(fuxam, "credential_store", return_value=keychain),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
        ):
            self.assertEqual(fuxam.FuxamClient()._session_token(), session_token)

        token_request = opener.open.call_args_list[1].args[0]
        form = urllib.parse.parse_qs(
            token_request.data.decode(), keep_blank_values=True
        )
        self.assertEqual(form["organization_id"], [""])
        self.assertEqual(form["tab_state"], ["focused"])
        self.assertEqual(form["token"], ["synthetic-renewal-token"])

    def test_session_token_rejects_invalid_expiry_before_caching(self) -> None:
        for expiry in (
            True,
            False,
            None,
            "2000000000",
            float("nan"),
            float("inf"),
            float("-inf"),
            10**400,
            -(10**400),
        ):
            client = fuxam.FuxamClient()
            opener = mock.Mock()
            opener.open.side_effect = synthetic_session("private-user", expiry=expiry)
            keychain = mock.Mock(storage="macOS Keychain")
            keychain.get.return_value = "synthetic-client-cookie"
            with (
                self.subTest(expiry=expiry),
                mock.patch.object(fuxam, "credential_store", return_value=keychain),
                mock.patch.object(
                    fuxam.urllib.request, "build_opener", return_value=opener
                ),
            ):
                with self.assertRaises(fuxam.FuxamError) as raised:
                    client._session_token()
                self.assertNotIn("private-user", str(raised.exception))
                self.assertIsNone(client.token)
                self.assertIsNone(client.user_id)
                self.assertEqual(client.token_expires_at, 0.0)

    def test_oversized_response_is_rejected(self) -> None:
        opener = FakeOpener(b"12345")
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(fuxam, "MAX_RESPONSE_BYTES", 4),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaisesRegex(fuxam.FuxamError, "large response"),
        ):
            client._open("https://fuxam.app/api/test", authenticated=False)

    def test_authenticated_401_is_not_retried_when_retry_is_disabled(self) -> None:
        opener = mock.Mock()
        error = urllib.error.HTTPError(
            "https://fuxam.app/action", 401, "Unauthorized", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        opener.open.side_effect = error
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "_session_token", return_value="opaque"),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaises(fuxam.FuxamError),
        ):
            client._open(
                "https://fuxam.app/action",
                method="POST",
                data=b"[]",
                require_context=False,
                retry=False,
            )

        self.assertEqual(opener.open.call_count, 1)

    def test_bound_read_401_account_switch_stops_before_retry_request(self) -> None:
        private_account_a = "private-student-a"
        private_account_b = "private-student-b"
        private_token_a = "private-token-a"
        private_token_b = "private-token-b"
        client = fuxam.FuxamClient()
        client.user_id = private_account_a
        expected_account = fuxam.account_fingerprint(private_account_a)
        opener = mock.Mock()
        error = urllib.error.HTTPError(
            "https://fuxam.app/api/build-id",
            401,
            "Unauthorized",
            {},
            io.BytesIO(),
        )
        self.addCleanup(error.close)
        opener.open.side_effect = error

        def session_token(force: bool = False) -> str:
            if force:
                client.user_id = private_account_b
                return private_token_b
            return (
                private_token_b
                if client.user_id == private_account_b
                else private_token_a
            )

        with (
            mock.patch.object(client, "_session_token", side_effect=session_token),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ) as build_opener,
            self.assertRaisesRegex(
                fuxam.MutationPreconditionChanged, "ACCOUNT_CHANGED"
            ) as raised,
        ):
            client._open(
                "https://fuxam.app/api/build-id",
                headers={"accept": "application/json"},
                require_context=False,
                expected_account=expected_account,
            )

        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(build_opener.call_count, 1)
        serialized_error = json.dumps({"error": str(raised.exception)})
        for private_value in (
            private_account_a,
            private_account_b,
            private_token_a,
            private_token_b,
        ):
            self.assertNotIn(private_value, serialized_error)

    def test_reads_pin_user_and_organization_across_session_renewal(self) -> None:
        identities = (
            ("student-other", "organization-original"),
            ("student-original", "organization-other"),
        )
        for mode in ("401", "expiry", "context"):
            for next_user, next_org in identities:
                with self.subTest(mode=mode, user=next_user, organization=next_org):
                    client = fuxam.FuxamClient()
                    error = urllib.error.HTTPError(
                        "https://fuxam.app/api/synthetic",
                        401,
                        "Unauthorized",
                        {},
                        io.BytesIO(),
                    )
                    self.addCleanup(error.close)
                    first_data = [{"slug": "code"}] if mode == "context" else {}
                    second_data = [
                        {"id": "cohort-other", "studyProgramVersionId": "program-other"}
                    ]
                    opener = mock.Mock()
                    opener.open.side_effect = [
                        *synthetic_session("student-original", "organization-original"),
                        FakeResponse(json.dumps(first_data).encode()),
                        *([] if mode == "expiry" else [error]),
                        *synthetic_session(next_user, next_org),
                        FakeResponse(json.dumps(second_data).encode()),
                    ]
                    keychain = mock.Mock(storage="macOS Keychain")
                    keychain.get.return_value = "synthetic-cookie"
                    with (
                        mock.patch.object(
                            fuxam, "credential_store", return_value=keychain
                        ),
                        mock.patch.object(
                            fuxam.urllib.request, "build_opener", return_value=opener
                        ),
                    ):
                        if mode != "context":
                            client._json("/api/first", require_context=False)
                            if mode == "expiry":
                                client.token_expires_at = 0
                        with self.assertRaisesRegex(
                            fuxam.FuxamError, "ACCOUNT_CHANGED"
                        ) as raised:
                            if mode == "context":
                                client.context()
                            else:
                                client._json("/api/second", require_context=False)

                    requests = [call.args[0] for call in opener.open.call_args_list]
                    fuxam_requests = [
                        request
                        for request in requests
                        if urllib.parse.urlsplit(request.full_url).hostname
                        == "fuxam.app"
                    ]
                    self.assertEqual(len(fuxam_requests), 1 if mode == "expiry" else 2)
                    self.assertEqual(client.user_id, "student-original")
                    self.assertEqual(client.organization_id, "organization-original")
                    self.assertIsNone(client.context_cache)
                    for private_value in (next_user, next_org, "synthetic-cookie"):
                        self.assertNotIn(private_value, str(raised.exception))

    def test_same_account_read_401_can_refresh_and_retry_once(self) -> None:
        error = urllib.error.HTTPError(
            "https://fuxam.app/api/synthetic", 401, "Unauthorized", {}, io.BytesIO()
        )
        self.addCleanup(error.close)
        opener = mock.Mock()
        opener.open.side_effect = [
            *synthetic_session(),
            error,
            *synthetic_session(),
            FakeResponse(b'{"ok":true}'),
        ]
        keychain = mock.Mock(storage="macOS Keychain")
        keychain.get.return_value = "synthetic-cookie"
        with (
            mock.patch.object(fuxam, "credential_store", return_value=keychain),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
        ):
            result = fuxam.FuxamClient()._json("/api/synthetic", require_context=False)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.open.call_count, 6)

    def test_term_courses_derives_only_the_exact_my_term_sibling(self) -> None:
        client = fuxam.FuxamClient()
        dashboard = "https://fuxam.app/en/dashboard/home/user/my-courses"
        with (
            mock.patch.object(
                client, "context", return_value={"dashboardUrl": dashboard}
            ),
            mock.patch.object(
                client, "_open", return_value=synthetic_term_html()
            ) as open_request,
        ):
            result = client.term_courses()

        self.assertEqual(result, synthetic_term_payload())
        open_request.assert_called_once_with(
            "https://fuxam.app/en/dashboard/home/user/my-term",
            headers={"accept": "text/html"},
            require_context=False,
            expected_account=None,
        )

    def test_http_error_does_not_expose_dynamic_request_path(self) -> None:
        private_url = "https://clerk.fuxam.app/v1/sessions/sess_PRIVATE/tokens"
        opener = mock.Mock()
        http_error = urllib.error.HTTPError(
            private_url, 403, "Forbidden", {}, io.BytesIO()
        )
        self.addCleanup(http_error.close)
        opener.open.side_effect = http_error
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaises(fuxam.FuxamError) as raised,
        ):
            client._open(private_url, authenticated=False)

        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("sess_PRIVATE", str(raised.exception))


class ServerActionContractTests(unittest.TestCase):
    def test_empty_build_id_is_rejected(self) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "_json", return_value={"buildId": ""}),
            self.assertRaisesRegex(fuxam.FuxamError, "no build ID"),
        ):
            client.current_build_id()

    def test_action_resolution_caches_all_unique_allowlisted_actions(self) -> None:
        client = fuxam.FuxamClient()
        page = "https://fuxam.app/en/dashboard/home/student/my-courses"
        sources = {
            "checkCourseConflictsAction": {"a" * 40},
            "bookCoursesAction": {"b" * 40},
            "unrelatedAction": {"c" * 40},
        }
        with (
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(
                client, "_action_sources", return_value=sources
            ) as action_sources,
        ):
            conflict = client._resolve_action("checkCourseConflictsAction", page)
            booking = client._resolve_action("bookCoursesAction", page)

        self.assertEqual(conflict, "a" * 40)
        self.assertEqual(booking, "b" * 40)
        self.assertNotIn((page, "unrelatedAction"), client.actions)
        action_sources.assert_called_once_with(page, expected_account=None)

    def test_non_read_only_server_action_is_rejected_before_context_lookup(
        self,
    ) -> None:
        client = fuxam.FuxamClient()
        with self.assertRaisesRegex(fuxam.FuxamError, "non-read-only"):
            client._action("bookCourseAction", [])

    def test_conflict_ids_are_deduplicated_without_reordering(self) -> None:
        client = fuxam.FuxamClient()
        with mock.patch.object(client, "_action", return_value=[]) as action:
            client.conflicts(["a", "b", "a"])

        expected = [
            {"courseIds": ["a", "b"], "includeAppointmentsForCourseIds": ["a", "b"]}
        ]
        action.assert_called_once_with("checkCourseConflictsAction", expected)

    def test_explore_accepts_empty_catalogs_with_optional_totals(self) -> None:
        for field in ("courses", "learningUnits"):
            for page_count in (0, 1):
                for totals in ({}, {"totalCount": 0}):
                    client = fuxam.FuxamClient()
                    value = {"pageCount": page_count, field: [], **totals}
                    with (
                        self.subTest(value=value),
                        mock.patch.object(
                            client, "study_plan", return_value=synthetic_study_plan()
                        ),
                        mock.patch.object(
                            client, "bookable", return_value=value
                        ) as bookable,
                    ):
                        result = client.explore("no matches")
                        self.assertFalse(result["partial"])
                        self.assertEqual(result["learningUnits"], [])
                        bookable.assert_called_once_with("no matches", 1, 100)

    def test_catalog_page_count_is_bounded(self) -> None:
        client = fuxam.FuxamClient()
        bad_values = (
            -1,
            fuxam.MAX_EXPLORE_PAGES + 1,
            "0",
            0.0,
            True,
            None,
        )
        for page_count in bad_values:
            with (
                self.subTest(page_count=page_count),
                mock.patch.object(client, "study_plan", return_value={}),
                mock.patch.object(
                    client,
                    "bookable",
                    return_value={"pageCount": page_count, "courses": []},
                ) as bookable,
                self.assertRaisesRegex(fuxam.FuxamError, "page count"),
            ):
                client.explore("")
            bookable.assert_called_once_with("", 1, 100)

    def test_explore_rejects_malformed_catalog_pages(self) -> None:
        client = fuxam.FuxamClient()
        for value in malformed_catalog_pages():
            with (
                self.subTest(value=value),
                mock.patch.object(
                    client, "study_plan", return_value=synthetic_study_plan()
                ),
                mock.patch.object(client, "bookable", return_value=value),
                self.assertRaises(fuxam.FuxamError),
            ):
                client.explore("")

    def test_explore_validates_every_page_and_consistent_metadata(self) -> None:
        first = {"pageCount": 2, "totalCount": 2, "courses": [{"id": "course-a"}]}
        for second in (
            {"pageCount": 2},
            {"pageCount": 3, "totalCount": 2, "courses": [{"id": "course-b"}]},
            {"pageCount": 2, "totalCount": 3, "courses": [{"id": "course-b"}]},
        ):
            client = fuxam.FuxamClient()
            with (
                self.subTest(second=second),
                mock.patch.object(
                    client, "study_plan", return_value=synthetic_study_plan()
                ),
                mock.patch.object(
                    client, "bookable", side_effect=[first, second]
                ) as bookable,
                self.assertRaises(fuxam.FuxamError),
            ):
                client.explore("")
            self.assertEqual(bookable.call_count, 2)

    def test_explore_rejects_total_count_presence_changes_between_pages(self) -> None:
        for totals in (({}, {"totalCount": 2}), ({"totalCount": 2}, {})):
            client = fuxam.FuxamClient()
            pages = [
                {"pageCount": 2, "courses": [{"id": course_id}], **metadata}
                for course_id, metadata in zip(
                    ("course-a", "course-b"), totals, strict=True
                )
            ]
            with (
                self.subTest(totals=totals),
                mock.patch.object(
                    client, "study_plan", return_value=synthetic_study_plan()
                ),
                mock.patch.object(client, "bookable", side_effect=pages) as bookable,
                self.assertRaisesRegex(fuxam.FuxamError, "pagination changed"),
            ):
                client.explore("")
            self.assertEqual(bookable.call_count, 2)

    def test_explore_accepts_complete_catalogs_with_optional_totals(self) -> None:
        rows = [{"id": "course-a"}, {"id": "course-b"}]
        for fields in (("courses",), ("learningUnits",), ("courses", "learningUnits")):
            for totals in ({}, {"totalCount": 2}):
                for page_rows in ([rows], [[rows[0]], [rows[1]]]):
                    pages = [
                        {
                            "pageCount": len(page_rows),
                            **totals,
                            **dict.fromkeys(fields, courses),
                        }
                        for courses in page_rows
                    ]
                    client = fuxam.FuxamClient()
                    with (
                        self.subTest(fields=fields, pages=pages),
                        mock.patch.object(
                            client, "study_plan", return_value=synthetic_study_plan()
                        ),
                        mock.patch.object(
                            client, "bookable", side_effect=pages
                        ) as bookable,
                    ):
                        result = client.explore("")
                        self.assertFalse(result["partial"])
                        self.assertEqual(result["learningUnits"], rows)
                        self.assertEqual(bookable.call_count, len(pages))

    def test_explore_fetches_all_pages_and_deduplicates_both_supported_collections(
        self,
    ) -> None:
        for field in ("courses", "learningUnits"):
            client = fuxam.FuxamClient()
            pages = [
                {"pageCount": 3, field: courses}
                for courses in (
                    [{"id": "course-a"}],
                    [{"id": "course-a"}, {"id": "course-b"}],
                    [{"id": "course-c"}],
                )
            ]
            with (
                self.subTest(field=field),
                mock.patch.object(
                    client, "study_plan", return_value=synthetic_study_plan()
                ),
                mock.patch.object(client, "bookable", side_effect=pages) as bookable,
            ):
                result = client.explore("synthetic search")
            self.assertFalse(result["partial"])
            self.assertEqual(
                [course["id"] for course in result["learningUnits"]],
                ["course-a", "course-b", "course-c"],
            )
            self.assertEqual(
                bookable.call_args_list,
                [mock.call("synthetic search", page, 100) for page in (1, 2, 3)],
            )

    def test_explore_rejects_incomplete_catalogs_despite_stable_totals(self) -> None:
        for field in ("courses", "learningUnits"):
            for total, first_ids, second_ids in (
                (3, ["course-a", "course-b"], ["course-a"]),
                (3, ["course-a"], ["course-b"]),
                (3, ["course-a", "course-b"], []),
                (1, ["course-a"], ["course-b"]),
            ):
                pages = [
                    {
                        "pageCount": 2,
                        "totalCount": total,
                        field: [{"id": course_id} for course_id in course_ids],
                    }
                    for course_ids in (first_ids, second_ids)
                ]
                client = fuxam.FuxamClient()
                with (
                    self.subTest(field=field, pages=pages),
                    mock.patch.object(
                        client, "study_plan", return_value=synthetic_study_plan()
                    ),
                    mock.patch.object(client, "bookable", side_effect=pages),
                    self.assertRaisesRegex(fuxam.FuxamError, "catalog total"),
                ):
                    client.explore("")

    def test_explore_marks_an_unusable_study_plan_partial(self) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "study_plan", return_value={}),
            mock.patch.object(
                client, "bookable", return_value={"pageCount": 0, "courses": []}
            ),
        ):
            result = client.explore("")
        self.assertTrue(result["partial"])
        self.assertIsNone(result["studyPlan"])

    def test_aggregate_reads_do_not_hide_account_changes_as_partial_results(
        self,
    ) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(
                client,
                "study_plan",
                side_effect=fuxam.MutationPreconditionChanged("ACCOUNT_CHANGED"),
            ),
            mock.patch.object(client, "bookable") as bookable,
            self.assertRaisesRegex(fuxam.FuxamError, "ACCOUNT_CHANGED"),
        ):
            client.explore("")
        bookable.assert_not_called()
        with (
            mock.patch.object(
                client,
                "context",
                return_value={
                    "moduleCatalogUrl": "https://fuxam.app/synthetic/module-catalog"
                },
            ),
            mock.patch.object(
                client,
                "_action",
                side_effect=fuxam.MutationPreconditionChanged("ACCOUNT_CHANGED"),
            ) as action,
            mock.patch.object(client, "module_attempts") as attempts,
            self.assertRaisesRegex(fuxam.FuxamError, "ACCOUNT_CHANGED"),
        ):
            client.module_details("module-synthetic", "version-synthetic", None)
        action.assert_called_once()
        attempts.assert_not_called()

    def test_cli_exposes_guarded_booking_commands(self) -> None:
        parser = fuxam.build_parser()
        preview = parser.parse_args(["booking", "enroll", "course-synthetic"])
        apply = parser.parse_args(
            [
                "booking",
                "leave-waitlist",
                "course-synthetic",
                "--apply",
                "--confirm",
                "sha256:synthetic",
            ]
        )

        self.assertEqual(preview.operation, "enroll")
        self.assertFalse(preview.apply)
        self.assertEqual(apply.operation, "leave-waitlist")
        self.assertTrue(apply.apply)
        self.assertEqual(apply.confirmation, "sha256:synthetic")

    def test_parser_bounds_page_numbers(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            fuxam.build_parser().parse_args(["bookable", "--page", "0"])

    def test_source_contains_no_telemetry_or_generic_raw_action_command(self) -> None:
        source = SCRIPT.read_text().lower()
        forbidden = ("posthog", "analytics", "raw-action")
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, source)


class BookingWorkflowTests(unittest.TestCase):
    def booking_client(self, *term_payloads: dict[str, object]) -> mock.Mock:
        client = mock.Mock()
        client.term_courses.side_effect = term_payloads or (synthetic_term_payload(),)
        client.current_build_id.return_value = "build-synthetic"
        client.account_fingerprint.return_value = "sha256:account-synthetic"
        client.booking_conflicts.return_value = []
        client.mutate_booking.return_value = {"ok": True}
        return client

    def test_preview_never_invokes_mutation_and_returns_confirmation(self) -> None:
        client = self.booking_client(synthetic_term_payload())

        result = fuxam.booking_workflow(
            client,
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )

        self.assertEqual(result["mode"], "preview")
        self.assertEqual(result["observedState"], "BOOKABLE")
        self.assertEqual(result["desiredState"], "ENROLLED")
        self.assertTrue(result["confirmationRequired"])
        self.assertRegex(result["confirmationFingerprint"], r"^sha256:[0-9a-f]{64}$")
        client.mutate_booking.assert_not_called()

    def test_public_cli_defaults_to_preview_and_never_mutates(self) -> None:
        client = self.booking_client(synthetic_term_payload())
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(SCRIPT),
            "booking",
            "join-waitlist",
            "course-full",
        ]

        with (
            mock.patch.object(fuxam, "FuxamClient", return_value=client),
            mock.patch.object(fuxam.sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = fuxam.main()

        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(result["mode"], "preview")
        self.assertFalse(result["changed"])
        client.mutate_booking.assert_not_called()

    def test_public_cli_apply_without_confirmation_exits_nonzero(self) -> None:
        client = self.booking_client(synthetic_term_payload())
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(SCRIPT),
            "booking",
            "join-waitlist",
            "course-full",
            "--apply",
        ]

        with (
            mock.patch.object(fuxam, "FuxamClient", return_value=client),
            mock.patch.object(fuxam.sys, "argv", argv),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = fuxam.main()

        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("CONFIRMATION_REQUIRED", stderr.getvalue())
        client.mutate_booking.assert_not_called()

    def test_conflict_details_are_redacted_and_enrollment_is_blocked(self) -> None:
        client = self.booking_client(synthetic_term_payload())
        client.booking_conflicts.return_value = [
            {
                "courseA": {"id": "course-open", "name": "private-secret"},
                "courseB": {"id": "course-enrolled"},
            }
        ]

        with self.assertRaisesRegex(fuxam.FuxamError, "SCHEDULE_CONFLICTS") as raised:
            fuxam.booking_workflow(
                client,
                "enroll",
                "course-open",
                apply=False,
                confirmation=None,
            )

        self.assertNotIn("private-secret", str(raised.exception))
        client.mutate_booking.assert_not_called()

    def test_unrelated_conflicts_do_not_block_target_enrollment(self) -> None:
        client = self.booking_client(synthetic_term_payload())
        client.booking_conflicts.return_value = [
            {
                "courseA": {"id": "already-enrolled-a"},
                "courseB": {"id": "already-enrolled-b"},
            }
        ]

        result = fuxam.booking_workflow(
            client,
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )

        self.assertFalse(result["conflictCheck"]["hasConflicts"])
        self.assertEqual(result["conflictCheck"]["count"], 0)
        client.mutate_booking.assert_not_called()

    def test_malformed_conflict_data_fails_closed(self) -> None:
        client = self.booking_client(synthetic_term_payload())
        client.booking_conflicts.return_value = [
            {"courseA": {"id": "course-open"}, "courseB": "malformed"}
        ]

        with self.assertRaisesRegex(fuxam.FuxamError, "unsupported conflict"):
            fuxam.booking_workflow(
                client,
                "enroll",
                "course-open",
                apply=False,
                confirmation=None,
            )

        client.mutate_booking.assert_not_called()

    def test_first_enrollment_skips_the_inapplicable_conflict_action(self) -> None:
        payload = synthetic_term_payload()
        payload["coursesByCategory"]["myCourses"] = []
        client = self.booking_client(payload)

        result = fuxam.booking_workflow(
            client,
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )

        self.assertEqual(result["conflictCheck"], {"checked": False})
        client.booking_conflicts.assert_not_called()
        client.mutate_booking.assert_not_called()

    def test_confirmation_is_bound_to_the_authenticated_account(self) -> None:
        account_a = self.booking_client(synthetic_term_payload())
        account_a.account_fingerprint.return_value = "sha256:account-a"
        account_b = self.booking_client(synthetic_term_payload())
        account_b.account_fingerprint.return_value = "sha256:account-b"

        preview_a = fuxam.booking_workflow(
            account_a,
            "join-waitlist",
            "course-full",
            apply=False,
            confirmation=None,
        )
        preview_b = fuxam.booking_workflow(
            account_b,
            "join-waitlist",
            "course-full",
            apply=False,
            confirmation=None,
        )

        self.assertNotEqual(
            preview_a["confirmationFingerprint"],
            preview_b["confirmationFingerprint"],
        )

    def test_confirmation_is_bound_to_operation_and_exact_course_id(self) -> None:
        enroll = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        join_waitlist = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "join-waitlist",
            "course-full",
            apply=False,
            confirmation=None,
        )
        twin_payload = synthetic_term_payload()
        twin_payload["coursesByCategory"]["bookable"].append(
            synthetic_course("course-open-twin", "Synthetic Open LU")
        )
        exact_id_twin = fuxam.booking_workflow(
            self.booking_client(twin_payload),
            "enroll",
            "course-open-twin",
            apply=False,
            confirmation=None,
        )

        self.assertNotEqual(
            enroll["confirmationFingerprint"],
            join_waitlist["confirmationFingerprint"],
        )
        self.assertNotEqual(
            enroll["confirmationFingerprint"],
            exact_id_twin["confirmationFingerprint"],
        )

    def test_apply_requires_matching_fresh_preview_fingerprint(self) -> None:
        preview_client = self.booking_client(synthetic_term_payload())
        preview = fuxam.booking_workflow(
            preview_client,
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        changed = synthetic_term_payload()
        changed["coursesByCategory"]["bookable"][0]["booking"]["enrolledCount"] = 11
        apply_client = self.booking_client(changed)

        with self.assertRaisesRegex(fuxam.FuxamError, "STALE_PREVIEW"):
            fuxam.booking_workflow(
                apply_client,
                "enroll",
                "course-open",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        apply_client.mutate_booking.assert_not_called()

    def test_non_ascii_confirmation_is_rejected_without_mutation(self) -> None:
        client = self.booking_client(synthetic_term_payload())

        with self.assertRaisesRegex(fuxam.FuxamError, "STALE_PREVIEW"):
            fuxam.booking_workflow(
                client,
                "enroll",
                "course-open",
                apply=True,
                confirmation="sha256:" + "é" * 64,
            )

        client.mutate_booking.assert_not_called()

    def test_final_precondition_rejects_state_change_before_dispatch(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        changed = synthetic_term_payload()
        changed["coursesByCategory"]["bookable"][0]["booking"]["enrolledCount"] = 11
        client = self.booking_client(synthetic_term_payload(), changed)

        def run_final_precondition(
            _operation: str,
            _course_id: str,
            **kwargs: object,
        ) -> object:
            final_precondition = kwargs["final_precondition"]
            self.assertTrue(callable(final_precondition))
            final_precondition()
            self.fail("A changed snapshot must stop before mutation dispatch.")

        client.mutate_booking.side_effect = run_final_precondition

        with self.assertRaisesRegex(fuxam.FuxamError, "STALE_PREVIEW"):
            fuxam.booking_workflow(
                client,
                "enroll",
                "course-open",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        self.assertEqual(client.term_courses.call_count, 2)

    def test_apply_without_confirmation_never_mutates(self) -> None:
        client = self.booking_client(synthetic_term_payload())

        with self.assertRaisesRegex(fuxam.FuxamError, "CONFIRMATION_REQUIRED"):
            fuxam.booking_workflow(
                client,
                "join-waitlist",
                "course-full",
                apply=True,
                confirmation=None,
            )

        client.mutate_booking.assert_not_called()

    def test_invalid_id_is_rejected_before_any_live_read(self) -> None:
        client = self.booking_client(synthetic_term_payload())

        with self.assertRaisesRegex(fuxam.FuxamError, "INVALID_COURSE_ID"):
            fuxam.booking_workflow(
                client,
                "enroll",
                "Synthetic Course Name",
                apply=False,
                confirmation=None,
            )

        client.term_courses.assert_not_called()

    def test_wrong_transition_is_rejected_without_mutation(self) -> None:
        client = self.booking_client(synthetic_term_payload())

        with self.assertRaisesRegex(fuxam.FuxamError, "NOT_ELIGIBLE"):
            fuxam.booking_workflow(
                client,
                "unenroll",
                "course-waitlisted",
                apply=False,
                confirmation=None,
            )

        client.mutate_booking.assert_not_called()

    def test_server_policy_denials_never_reach_mutation(self) -> None:
        booking_disabled = synthetic_term_payload()
        booking_disabled["canBookCourses"] = False
        waitlist_disabled = synthetic_term_payload()
        waitlist_disabled["waitlistEnabled"] = False
        unenroll_disabled = synthetic_term_payload()
        unenroll_disabled["coursesByCategory"]["myCourses"][0]["booking"][
            "canUnbook"
        ] = False
        cases = (
            ("enroll", "course-open", booking_disabled),
            ("unenroll", "course-enrolled", booking_disabled),
            ("join-waitlist", "course-full", booking_disabled),
            ("leave-waitlist", "course-waitlisted", booking_disabled),
            ("join-waitlist", "course-full", waitlist_disabled),
            ("unenroll", "course-enrolled", unenroll_disabled),
        )

        for operation, course_id, payload in cases:
            with self.subTest(operation=operation):
                client = self.booking_client(payload)

                with self.assertRaisesRegex(fuxam.FuxamError, "NOT_ELIGIBLE"):
                    fuxam.booking_workflow(
                        client,
                        operation,
                        course_id,
                        apply=False,
                        confirmation=None,
                    )

                client.mutate_booking.assert_not_called()

    def test_each_valid_apply_mutates_once_and_verifies_postcondition(self) -> None:
        cases = (
            ("enroll", "course-open", "ENROLLED"),
            ("unenroll", "course-enrolled", "BOOKABLE"),
            ("join-waitlist", "course-full", "WAITLISTED"),
            ("leave-waitlist", "course-waitlisted", "FULL"),
        )
        for operation, course_id, verified_state in cases:
            with self.subTest(operation=operation):
                preview = fuxam.booking_workflow(
                    self.booking_client(synthetic_term_payload()),
                    operation,
                    course_id,
                    apply=False,
                    confirmation=None,
                )
                apply_client = self.booking_client(
                    synthetic_term_payload(), synthetic_term_after(operation)
                )
                if operation == "join-waitlist":
                    apply_client.mutate_booking.return_value = {"hasConflicts": False}

                result = fuxam.booking_workflow(
                    apply_client,
                    operation,
                    course_id,
                    apply=True,
                    confirmation=preview["confirmationFingerprint"],
                )

                self.assertTrue(result["changed"])
                self.assertEqual(result["result"], "verified-success")
                self.assertEqual(result["verifiedState"], verified_state)
                apply_client.mutate_booking.assert_called_once_with(
                    operation,
                    course_id,
                    expected_account="sha256:account-synthetic",
                    expected_build="build-synthetic",
                    final_precondition=mock.ANY,
                )
                self.assertEqual(apply_client.term_courses.call_count, 2)
                if operation == "join-waitlist":
                    self.assertFalse(result["scheduleConflictWarning"])
                    self.assertFalse(result["requiresUiInspection"])

    def test_join_waitlist_reports_only_the_server_conflict_boolean(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "join-waitlist",
            "course-full",
            apply=False,
            confirmation=None,
        )
        client = self.booking_client(
            synthetic_term_payload(), synthetic_term_after("join-waitlist")
        )
        client.mutate_booking.return_value = {
            "hasConflicts": True,
            "privateDetail": "private-conflict-detail",
        }

        result = fuxam.booking_workflow(
            client,
            "join-waitlist",
            "course-full",
            apply=True,
            confirmation=preview["confirmationFingerprint"],
        )

        self.assertTrue(result["scheduleConflictWarning"])
        self.assertTrue(result["requiresUiInspection"])
        self.assertIn("SCHEDULE_CONFLICT_WARNING", result["warning"])
        self.assertNotIn("private-conflict-detail", json.dumps(result))

    def test_nominal_response_without_postcondition_is_a_failure(self) -> None:
        preview_client = self.booking_client(synthetic_term_payload())
        preview = fuxam.booking_workflow(
            preview_client,
            "unenroll",
            "course-enrolled",
            apply=False,
            confirmation=None,
        )
        client = self.booking_client(synthetic_term_payload(), synthetic_term_payload())

        with self.assertRaisesRegex(fuxam.FuxamError, "POSTCONDITION_FAILED"):
            fuxam.booking_workflow(
                client,
                "unenroll",
                "course-enrolled",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        client.mutate_booking.assert_called_once()

    def test_all_already_desired_states_are_idempotent_without_mutation(self) -> None:
        cases = (
            ("enroll", "course-enrolled", "ENROLLED"),
            ("unenroll", "course-open", "BOOKABLE"),
            ("unenroll", "course-full", "FULL"),
            ("join-waitlist", "course-waitlisted", "WAITLISTED"),
            ("leave-waitlist", "course-open", "BOOKABLE"),
            ("leave-waitlist", "course-full", "FULL"),
        )
        for operation, course_id, observed_state in cases:
            with self.subTest(operation=operation, state=observed_state):
                client = self.booking_client(synthetic_term_payload())

                result = fuxam.booking_workflow(
                    client,
                    operation,
                    course_id,
                    apply=True,
                    confirmation=None,
                )

                self.assertFalse(result["changed"])
                self.assertEqual(result["observedState"], observed_state)
                self.assertEqual(result["result"], "already-applied")
                client.mutate_booking.assert_not_called()

    def test_ambiguous_failure_reconciles_without_a_second_mutation(self) -> None:
        preview_client = self.booking_client(synthetic_term_payload())
        preview = fuxam.booking_workflow(
            preview_client,
            "join-waitlist",
            "course-full",
            apply=False,
            confirmation=None,
        )
        client = self.booking_client(
            synthetic_term_payload(), synthetic_term_after("join-waitlist")
        )
        client.mutate_booking.side_effect = fuxam.MutationOutcomeUnknown()

        result = fuxam.booking_workflow(
            client,
            "join-waitlist",
            "course-full",
            apply=True,
            confirmation=preview["confirmationFingerprint"],
        )

        self.assertEqual(result["result"], "reconciled-success")
        self.assertIsNone(result["scheduleConflictWarning"])
        self.assertTrue(result["requiresUiInspection"])
        self.assertIn("WAITLIST_CONFLICT_STATUS_UNKNOWN", result["warning"])
        client.mutate_booking.assert_called_once_with(
            "join-waitlist",
            "course-full",
            expected_account="sha256:account-synthetic",
            expected_build="build-synthetic",
            final_precondition=mock.ANY,
        )

    def test_ambiguous_failure_with_unmet_postcondition_stays_unknown(self) -> None:
        preview_client = self.booking_client(synthetic_term_payload())
        preview = fuxam.booking_workflow(
            preview_client,
            "leave-waitlist",
            "course-waitlisted",
            apply=False,
            confirmation=None,
        )
        client = self.booking_client(synthetic_term_payload(), synthetic_term_payload())
        client.mutate_booking.side_effect = fuxam.MutationOutcomeUnknown()

        with self.assertRaisesRegex(fuxam.FuxamError, "OUTCOME_UNKNOWN"):
            fuxam.booking_workflow(
                client,
                "leave-waitlist",
                "course-waitlisted",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        client.mutate_booking.assert_called_once()

    def test_parser_failure_after_dispatch_reconciles_without_a_second_write(
        self,
    ) -> None:
        nested_body = b'0:{"a":"$@1"}\n1:' + b"[" * 1500 + b"0" + b"]" * 1500 + b"\n"
        for fault in (None, RuntimeError("private-parser-detail")):
            for applied in (True, False):
                with self.subTest(fault=type(fault).__name__, applied=applied):
                    client = fuxam.FuxamClient()
                    client.user_id = "student-synthetic"
                    opener = FakeOpener(nested_body)
                    final_payload = (
                        synthetic_term_after("enroll")
                        if applied
                        else synthetic_term_payload()
                    )
                    with (
                        mock.patch.object(
                            client, "_session_token", return_value="token"
                        ),
                        mock.patch.object(
                            client,
                            "context",
                            return_value={
                                "dashboardUrl": "https://fuxam.app/synthetic/my-courses"
                            },
                        ),
                        mock.patch.object(
                            client, "current_build_id", return_value="build-synthetic"
                        ),
                        mock.patch.object(client, "booking_conflicts", return_value=[]),
                        mock.patch.object(
                            client, "_resolve_action", return_value="a" * 40
                        ),
                        mock.patch.object(
                            client,
                            "term_courses",
                            side_effect=[
                                synthetic_term_payload(),
                                synthetic_term_payload(),
                                synthetic_term_payload(),
                                final_payload,
                            ],
                        ) as term_courses,
                        mock.patch.object(
                            fuxam.urllib.request, "build_opener", return_value=opener
                        ),
                        mock.patch.object(
                            fuxam,
                            "parse_flight",
                            wraps=fuxam.parse_flight,
                            side_effect=fault,
                        ),
                    ):
                        preview = fuxam.booking_workflow(
                            client,
                            "enroll",
                            "course-open",
                            apply=False,
                            confirmation=None,
                        )
                        if applied:
                            result = fuxam.booking_workflow(
                                client,
                                "enroll",
                                "course-open",
                                apply=True,
                                confirmation=preview["confirmationFingerprint"],
                            )
                            self.assertEqual(result["result"], "reconciled-success")
                        else:
                            with self.assertRaisesRegex(
                                fuxam.FuxamError, "OUTCOME_UNKNOWN"
                            ) as raised:
                                fuxam.booking_workflow(
                                    client,
                                    "enroll",
                                    "course-open",
                                    apply=True,
                                    confirmation=preview["confirmationFingerprint"],
                                )
                            self.assertNotIn(
                                "private-parser-detail", str(raised.exception)
                            )

                    self.assertEqual(len(opener.requests), 1)
                    self.assertEqual(opener.requests[0].get_method(), "POST")
                    self.assertEqual(term_courses.call_count, 4)

    def test_unexpected_post_write_verification_errors_remain_unknown(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(),
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        for error in (RecursionError("private-depth"), RuntimeError("private-detail")):
            with self.subTest(error=type(error).__name__):
                client = self.booking_client()
                client.term_courses.side_effect = (synthetic_term_payload(), error)
                with self.assertRaisesRegex(
                    fuxam.FuxamError, "OUTCOME_UNKNOWN"
                ) as raised:
                    fuxam.booking_workflow(
                        client,
                        "enroll",
                        "course-open",
                        apply=True,
                        confirmation=preview["confirmationFingerprint"],
                    )
                self.assertNotIn("private-", str(raised.exception))
                client.mutate_booking.assert_called_once()
                self.assertEqual(client.term_courses.call_count, 2)

    def test_account_change_during_post_write_read_requires_ui_inspection(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(),
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        for ambiguous in (False, True):
            client = self.booking_client()
            client.term_courses.side_effect = (
                synthetic_term_payload(),
                fuxam.MutationPreconditionChanged("ACCOUNT_CHANGED: private-account"),
            )
            if ambiguous:
                client.mutate_booking.side_effect = fuxam.MutationOutcomeUnknown()
            with self.subTest(ambiguous=ambiguous):
                with self.assertRaisesRegex(
                    fuxam.FuxamError, "OUTCOME_UNKNOWN"
                ) as raised:
                    fuxam.booking_workflow(
                        client,
                        "enroll",
                        "course-open",
                        apply=True,
                        confirmation=preview["confirmationFingerprint"],
                    )
                self.assertIn(
                    "inspect the official UI before trying again.",
                    str(raised.exception),
                )
                self.assertNotIn("ACCOUNT_CHANGED", str(raised.exception))
                self.assertNotIn("private-account", str(raised.exception))
                client.mutate_booking.assert_called_once()
                self.assertEqual(client.term_courses.call_count, 2)

    def test_post_write_verification_failure_is_unknown_without_private_detail(
        self,
    ) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "unenroll",
            "course-enrolled",
            apply=False,
            confirmation=None,
        )
        private_detail = "private-verification-detail"
        client = self.booking_client(synthetic_term_payload())
        client.term_courses.side_effect = (
            synthetic_term_payload(),
            fuxam.FuxamError(private_detail),
        )

        with self.assertRaisesRegex(fuxam.FuxamError, "OUTCOME_UNKNOWN") as raised:
            fuxam.booking_workflow(
                client,
                "unenroll",
                "course-enrolled",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        self.assertNotIn(private_detail, str(raised.exception))
        client.mutate_booking.assert_called_once_with(
            "unenroll",
            "course-enrolled",
            expected_account="sha256:account-synthetic",
            expected_build="build-synthetic",
            final_precondition=mock.ANY,
        )
        self.assertEqual(client.term_courses.call_count, 2)

    def test_post_write_verification_interrupt_is_reported_as_unknown(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "leave-waitlist",
            "course-waitlisted",
            apply=False,
            confirmation=None,
        )
        client = self.booking_client(synthetic_term_payload())
        client.term_courses.side_effect = (
            synthetic_term_payload(),
            KeyboardInterrupt(),
        )

        with self.assertRaisesRegex(fuxam.FuxamError, "OUTCOME_UNKNOWN"):
            fuxam.booking_workflow(
                client,
                "leave-waitlist",
                "course-waitlisted",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        client.mutate_booking.assert_called_once_with(
            "leave-waitlist",
            "course-waitlisted",
            expected_account="sha256:account-synthetic",
            expected_build="build-synthetic",
            final_precondition=mock.ANY,
        )
        self.assertEqual(client.term_courses.call_count, 2)

    def test_active_term_rollover_cannot_verify_a_mutation(self) -> None:
        preview = fuxam.booking_workflow(
            self.booking_client(synthetic_term_payload()),
            "enroll",
            "course-open",
            apply=False,
            confirmation=None,
        )
        rolled_over = synthetic_term_after("enroll")
        rolled_over["activeTerm"] = {
            "id": "term-fs27",
            "name": "Fall 2027",
        }
        client = self.booking_client(synthetic_term_payload(), rolled_over)

        with self.assertRaisesRegex(fuxam.FuxamError, "OUTCOME_UNKNOWN"):
            fuxam.booking_workflow(
                client,
                "enroll",
                "course-open",
                apply=True,
                confirmation=preview["confirmationFingerprint"],
            )

        client.mutate_booking.assert_called_once_with(
            "enroll",
            "course-open",
            expected_account="sha256:account-synthetic",
            expected_build="build-synthetic",
            final_precondition=mock.ANY,
        )
        self.assertEqual(client.term_courses.call_count, 2)

    def test_each_operation_uses_only_its_fixed_action_and_argument_shape(self) -> None:
        cases = (
            ("enroll", "bookCoursesAction", [["course-synthetic"]]),
            ("unenroll", "unbookCourseAction", ["course-synthetic"]),
            ("join-waitlist", "joinWaitlistAction", ["course-synthetic"]),
            ("leave-waitlist", "leaveWaitlistAction", ["course-synthetic"]),
        )
        for operation, action_name, expected_args in cases:
            with self.subTest(operation=operation):
                client = fuxam.FuxamClient()
                with mock.patch.object(
                    client, "_mutation_action", return_value={"ok": True}
                ) as action:
                    client.mutate_booking(
                        operation,
                        "course-synthetic",
                        expected_account="sha256:account-synthetic",
                        expected_build="build-synthetic",
                    )
                action.assert_called_once_with(
                    action_name,
                    expected_args,
                    expected_account="sha256:account-synthetic",
                    expected_build="build-synthetic",
                    final_precondition=None,
                )

    def test_mutation_transport_is_never_retried(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        expected_account = fuxam.account_fingerprint("student-synthetic")
        with (
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(client, "_resolve_action", return_value="a" * 40),
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(client, "_session_token") as session_token,
            mock.patch.object(
                client, "_open", side_effect=fuxam.FuxamError("network failed")
            ) as open_request,
            self.assertRaises(fuxam.MutationOutcomeUnknown),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

        session_token.assert_called_once_with(force=True)
        self.assertEqual(open_request.call_count, 1)
        self.assertFalse(open_request.call_args.kwargs["retry"])

    def test_truncated_mutation_response_is_ambiguous_and_not_retried(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        expected_account = fuxam.account_fingerprint("student-synthetic")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = http.client.IncompleteRead(b"partial", 10)
        opener = mock.Mock()
        opener.open.return_value = response
        with (
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(client, "_session_token", return_value="token"),
            mock.patch.object(client, "_resolve_action", return_value="a" * 40),
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaises(fuxam.MutationOutcomeUnknown),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

        self.assertEqual(opener.open.call_count, 1)

    def test_interrupt_during_mutation_dispatch_becomes_ambiguous(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        expected_account = fuxam.account_fingerprint("student-synthetic")
        with (
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(client, "_session_token"),
            mock.patch.object(client, "_resolve_action", return_value="a" * 40),
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(client, "_invoke_action", side_effect=KeyboardInterrupt),
            self.assertRaises(fuxam.MutationOutcomeUnknown),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

    def test_unknown_mutation_action_is_rejected_before_resolution(self) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "_resolve_action") as resolve,
            self.assertRaisesRegex(fuxam.FuxamError, "unsupported mutation"),
        ):
            client._mutation_action(
                "deleteEverythingAction",
                [],
                expected_account="sha256:account-synthetic",
                expected_build="build-synthetic",
            )
        resolve.assert_not_called()

    def test_account_switch_after_preview_blocks_before_action_resolution(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-a"
        expected_account = fuxam.account_fingerprint("student-a")

        def switch_account(*, force: bool = False) -> str:
            self.assertTrue(force)
            client.user_id = "student-b"
            return "token-b"

        with (
            mock.patch.object(client, "_session_token", side_effect=switch_account),
            mock.patch.object(client, "context") as context,
            mock.patch.object(client, "_resolve_action") as resolve,
            self.assertRaisesRegex(fuxam.FuxamError, "ACCOUNT_CHANGED"),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

        context.assert_not_called()
        resolve.assert_not_called()

    def test_organization_switch_blocks_before_action_resolution(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-a"
        client.organization_id = "organization-a"
        expected_account = fuxam.account_fingerprint("student-a", "organization-a")

        def switch_organization(*, force: bool = False) -> str:
            self.assertTrue(force)
            client.organization_id = "organization-b"
            return "token-b"

        with (
            mock.patch.object(
                client, "_session_token", side_effect=switch_organization
            ),
            mock.patch.object(client, "context") as context,
            mock.patch.object(client, "_resolve_action") as resolve,
            self.assertRaisesRegex(fuxam.FuxamError, "ACCOUNT_CHANGED"),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

        context.assert_not_called()
        resolve.assert_not_called()

    def test_post_boundary_rechecks_account_after_obtaining_token(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-a"
        expected_account = fuxam.account_fingerprint("student-a")
        opener = mock.Mock()

        def switch_account() -> str:
            client.user_id = "student-b"
            return "token-b"

        with (
            mock.patch.object(client, "_session_token", side_effect=switch_account),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaisesRegex(fuxam.FuxamError, "ACCOUNT_CHANGED"),
        ):
            client._invoke_action(
                "a" * 40,
                ["course-synthetic"],
                "https://fuxam.app/synthetic/my-courses",
                retry=False,
                expected_account=expected_account,
            )

        opener.open.assert_not_called()

    def test_pre_dispatch_account_change_is_not_reported_as_unknown(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        opener = FakeOpener()
        changed = fuxam.MutationPreconditionChanged("ACCOUNT_CHANGED")
        with (
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(client, "_resolve_action", return_value="a" * 40),
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(
                client, "_session_token", side_effect=["synthetic-token", changed]
            ),
            mock.patch.object(
                fuxam.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaisesRegex(
                fuxam.MutationPreconditionChanged, "ACCOUNT_CHANGED"
            ) as raised,
        ):
            client.mutate_booking(
                "unenroll",
                "course-synthetic",
                expected_account=fuxam.account_fingerprint("student-synthetic"),
                expected_build="build-synthetic",
            )

        self.assertIs(raised.exception, changed)
        self.assertEqual(opener.requests, [])

    def test_build_change_after_preview_blocks_before_post(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        expected_account = fuxam.account_fingerprint("student-synthetic")
        with (
            mock.patch.object(client, "_session_token"),
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(
                client, "_resolve_action", side_effect=fuxam.FuxamError("BUILD_CHANGED")
            ) as resolve,
            mock.patch.object(client, "_invoke_action") as invoke,
            self.assertRaisesRegex(fuxam.FuxamError, "BUILD_CHANGED"),
        ):
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
            )

        resolve.assert_called_once_with(
            "unbookCourseAction",
            "https://fuxam.app/synthetic/my-courses",
            expected_build="build-synthetic",
            expected_account=expected_account,
        )
        invoke.assert_not_called()

    def test_final_precondition_runs_after_resolution_and_before_post(self) -> None:
        client = fuxam.FuxamClient()
        client.user_id = "student-synthetic"
        expected_account = fuxam.account_fingerprint("student-synthetic")
        timeline = mock.Mock()
        final_precondition = mock.Mock()
        with (
            mock.patch.object(client, "_session_token"),
            mock.patch.object(
                client,
                "context",
                return_value={"dashboardUrl": "https://fuxam.app/synthetic/my-courses"},
            ),
            mock.patch.object(
                client, "_resolve_action", return_value="a" * 40
            ) as resolve,
            mock.patch.object(
                client, "current_build_id", return_value="build-synthetic"
            ),
            mock.patch.object(
                client, "_invoke_action", return_value={"ok": True}
            ) as invoke,
        ):
            timeline.attach_mock(resolve, "resolve")
            timeline.attach_mock(final_precondition, "final")
            timeline.attach_mock(invoke, "invoke")
            client._mutation_action(
                "unbookCourseAction",
                ["course-synthetic"],
                expected_account=expected_account,
                expected_build="build-synthetic",
                final_precondition=final_precondition,
            )

        names = [call[0] for call in timeline.mock_calls]
        self.assertLess(names.index("resolve"), names.index("final"))
        self.assertLess(names.index("final"), names.index("invoke"))


class DiagnosticsTests(unittest.TestCase):
    def test_windows_cli_writes_utf8_when_output_is_redirected(self) -> None:
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        stderr = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with (
            mock.patch.object(fuxam.sys, "platform", "win32"),
            mock.patch.object(fuxam.sys, "argv", [str(SCRIPT), "context"]),
            mock.patch.object(fuxam.sys, "stdout", stdout),
            mock.patch.object(fuxam.sys, "stderr", stderr),
            mock.patch.object(fuxam, "run", return_value={"name": "Grüße ✓"}),
        ):
            status = fuxam.main()
        stdout.flush()
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.buffer.getvalue()), {"name": "Grüße ✓"})

    def test_deep_smoke_accepts_large_catalog_with_a_single_row_probe(self) -> None:
        client = FakeSmokeClient()
        with mock.patch.object(client, "bookable", wraps=client.bookable) as bookable:
            result = fuxam.smoke_test(client, deep=True)

        self.assertTrue(result["ok"])
        bookable.assert_called_once_with("", 1, 1)

    def test_deep_smoke_rejects_incomplete_single_page_catalogs(self) -> None:
        client = FakeSmokeClient()
        for field in ("courses", "learningUnits"):
            for rows in (
                [],
                [{"id": "private-catalog-course"}],
                [{"id": "private-catalog-course"}] * 2,
            ):
                with (
                    self.subTest(field=field, rows=rows),
                    mock.patch.object(
                        client,
                        "bookable",
                        return_value={"pageCount": 1, "totalCount": 2, field: rows},
                    ),
                ):
                    result = fuxam.smoke_test(client, deep=True)
                    self.assertFalse(result["ok"])
                    check = result["checks"][-1]
                    self.assertEqual(check["name"], "bookable-server-action")
                    self.assertEqual(check["error"], "FUXAM_CHECK_FAILED")
                    self.assertNotIn("private-catalog-course", json.dumps(result))

    def test_smoke_uses_the_same_study_plan_and_catalog_validation_as_commands(
        self,
    ) -> None:
        client = FakeSmokeClient()
        for method, cases, name in (
            ("study_plan", malformed_study_plans(), "study-plan"),
            ("bookable", malformed_catalog_pages(), "bookable-server-action"),
        ):
            for value in cases:
                with self.subTest(method=method, value=value):
                    with mock.patch.object(client, method, return_value=value):
                        result = fuxam.smoke_test(client, deep=True)
                    self.assertFalse(result["ok"])
                    check = next(
                        item for item in result["checks"] if item["name"] == name
                    )
                    self.assertEqual(check["error"], "FUXAM_CHECK_FAILED")
                    self.assertNotIn("private-", json.dumps(result))

    def test_doctor_reports_credential_status_without_revealing_value(self) -> None:
        for platform, storage in (
            ("darwin", "macOS Keychain"),
            ("linux", "Linux Secret Service"),
            ("win32", "Windows Credential Manager"),
        ):
            keychain = mock.Mock(storage=storage)
            keychain.get.return_value = "super-private-cookie"
            with (
                self.subTest(platform=platform),
                mock.patch.object(fuxam.sys, "platform", platform),
                mock.patch.object(fuxam, "credential_store", return_value=keychain),
            ):
                result = fuxam.doctor_status()

                self.assertTrue(result["ok"])
                self.assertTrue(result["platform"]["supported"])
                self.assertTrue(result["credential"]["configured"])
                self.assertEqual(result["credential"]["storage"], storage)
                self.assertNotIn("super-private-cookie", json.dumps(result))
                keychain.get.assert_called_once_with()

    def test_doctor_is_safe_on_an_unsupported_platform(self) -> None:
        with (
            mock.patch.object(fuxam.sys, "platform", "freebsd"),
            mock.patch.object(fuxam, "credential_store") as keychain,
        ):
            result = fuxam.doctor_status()

        self.assertFalse(result["ok"])
        self.assertIsNone(result["credential"]["configured"])
        keychain.assert_not_called()

    def test_linux_auth_status_reports_missing_or_locked_credentials(self) -> None:
        with (
            mock.patch.object(fuxam.sys, "platform", "linux"),
            mock.patch.object(
                credentials.shutil, "which", return_value="/usr/bin/secret-tool"
            ),
            mock.patch.object(
                credentials.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, b"", b"private-keyring-detail"
                ),
            ),
        ):
            result = fuxam.run(fuxam.build_parser().parse_args(["auth", "status"]))
            self.assertIsNone(result["configured"])
            self.assertEqual(result["storage"], "Linux Secret Service")
            self.assertIn("No readable credential", result["note"])
            self.assertNotIn("private-keyring-detail", json.dumps(result))

            result = fuxam.doctor_status()
            self.assertFalse(result["ok"])
            self.assertIsNone(result["credential"]["configured"])
            self.assertEqual(result["credentialError"], "CREDENTIAL_UNREADABLE")
            self.assertNotIn("Install", result["credential"]["hint"])
            self.assertNotIn("private-keyring-detail", json.dumps(result))

    def test_doctor_redacts_credential_store_failure_details(self) -> None:
        for platform, storage in (
            ("darwin", "macOS Keychain"),
            ("linux", "Linux Secret Service"),
            ("win32", "Windows Credential Manager"),
        ):
            for failure in (OSError, fuxam.FuxamError, UnicodeError):
                for stage in ("open", "read"):
                    with (
                        self.subTest(platform=platform, failure=failure, stage=stage),
                        mock.patch.object(fuxam.sys, "platform", platform),
                        mock.patch.object(fuxam, "credential_store") as factory,
                    ):
                        store = factory.return_value
                        store.storage = storage
                        operation = factory if stage == "open" else store.get
                        operation.side_effect = failure("private-local-store-detail")
                        result = fuxam.doctor_status()

                        self.assertFalse(result["ok"])
                        self.assertIsNone(result["credential"]["configured"])
                        self.assertEqual(
                            result["credential"]["storage"],
                            None if stage == "open" else storage,
                        )
                        self.assertEqual(
                            result["credentialError"],
                            "CREDENTIAL_STORE_UNAVAILABLE"
                            if stage == "open"
                            else "CREDENTIAL_UNREADABLE",
                        )
                        self.assertEqual(
                            "keychainError" in result, platform == "darwin"
                        )
                        if platform == "darwin":
                            self.assertEqual(
                                result["keychainError"], "KEYCHAIN_CHECK_FAILED"
                            )
                        hint = "Unlock your OS credential store and run auth set in a local terminal."
                        if platform == "linux":
                            hint = (
                                "Install secret-tool and unlock your desktop keyring, then run auth set."
                                if stage == "open"
                                else "Unlock your desktop keyring or run auth set locally."
                            )
                        self.assertEqual(result["credential"]["hint"], hint)
                        self.assertNotIn(
                            "private-local-store-detail", json.dumps(result)
                        )

    def test_deep_smoke_test_returns_only_shapes(self) -> None:
        result = fuxam.smoke_test(FakeSmokeClient(), deep=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "deep")
        self.assertEqual(len(result["checks"]), 6)
        serialized = json.dumps(result)
        self.assertNotIn("itemCount", serialized)
        self.assertNotIn("topLevelFieldCount", serialized)
        self.assertNotIn('"length"', serialized)
        for private_value in (
            "private-student-id",
            "private-course-title",
            "private-study-plan",
            "private-appointment",
            "private-bookable-course",
        ):
            self.assertNotIn(private_value, serialized)

    def test_smoke_test_reports_known_failures_and_continues(self) -> None:
        client = FakeSmokeClient()
        with mock.patch.object(
            client,
            "enrolled",
            side_effect=fuxam.FuxamError(
                "HTTP path contained sess_PRIVATE and student_PRIVATE"
            ),
        ):
            result = fuxam.smoke_test(client)

        self.assertFalse(result["ok"])
        enrolled = next(
            check for check in result["checks"] if check["name"] == "enrolled"
        )
        self.assertEqual(enrolled["error"], "FUXAM_CHECK_FAILED")
        self.assertNotIn("sess_PRIVATE", json.dumps(result))
        self.assertNotIn("student_PRIVATE", json.dumps(result))
        self.assertTrue(result["checks"][-1]["ok"])

    def test_smoke_test_stops_remaining_checks_when_the_account_changes(self) -> None:
        for deep in (False, True):
            client = FakeSmokeClient()
            with (
                self.subTest(deep=deep),
                mock.patch.object(
                    client,
                    "study_plan",
                    side_effect=fuxam.MutationPreconditionChanged("ACCOUNT_CHANGED"),
                ),
                mock.patch.object(client, "agenda") as agenda,
                mock.patch.object(client, "term_courses") as term_courses,
                mock.patch.object(client, "bookable") as bookable,
                self.assertRaisesRegex(
                    fuxam.MutationPreconditionChanged, "ACCOUNT_CHANGED"
                ),
            ):
                fuxam.smoke_test(client, deep=deep)
            agenda.assert_not_called()
            term_courses.assert_not_called()
            bookable.assert_not_called()

    def test_smoke_test_redacts_unexpected_exception_details(self) -> None:
        client = FakeSmokeClient()
        with mock.patch.object(
            client,
            "enrolled",
            side_effect=RuntimeError("private-record-in-exception"),
        ):
            result = fuxam.smoke_test(client)

        serialized = json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertIn("LOCAL_CHECK_FAILED", serialized)
        self.assertNotIn("private-record-in-exception", serialized)

    def test_smoke_test_rejects_unusable_success_shapes(self) -> None:
        cases = (
            ("context", {}),
            ("enrolled", None),
            ("study_plan", "not-json-structure"),
            ("agenda", {"error": "private-upstream-detail"}),
        )
        for method, bad_value in cases:
            with self.subTest(method=method):
                client = FakeSmokeClient()
                with mock.patch.object(client, method, return_value=bad_value):
                    result = fuxam.smoke_test(client)
                self.assertFalse(result["ok"])
                self.assertNotIn("private-upstream-detail", json.dumps(result))

        client = FakeSmokeClient()
        with mock.patch.object(client, "bookable", return_value={}):
            deep_result = fuxam.smoke_test(client, deep=True)
        self.assertFalse(deep_result["ok"])

    def test_failed_smoke_test_has_nonzero_cli_exit(self) -> None:
        output = io.StringIO()
        report = {"ok": False, "checks": []}
        with (
            mock.patch.object(fuxam.sys, "argv", [str(SCRIPT), "smoke-test"]),
            mock.patch.object(fuxam, "smoke_test", return_value=report),
            contextlib.redirect_stdout(output),
        ):
            code = fuxam.main()

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()), report)


class SkillMetadataTests(unittest.TestCase):
    def test_skill_has_minimal_valid_frontmatter(self) -> None:
        skill = (ROOT / ".agents" / "skills" / "fuxam-local" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\n"))
        frontmatter = skill.split("---", 2)[1]
        self.assertIn("\nname: fuxam-local\n", frontmatter)
        self.assertIn("\ndescription:", frontmatter)

    def test_runtime_has_no_third_party_imports(self) -> None:
        scripts = SCRIPT.parent
        local_modules = {path.stem for path in scripts.glob("*.py")}
        imports: set[str] = set()
        for path in scripts.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif (
                    isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
                ):
                    imports.add(node.module.split(".")[0])
        allowed = {
            "__future__",
            "argparse",
            "base64",
            "collections",
            "ctypes",
            "getpass",
            "hashlib",
            "hmac",
            "html",
            "http",
            "io",
            "json",
            "math",
            "re",
            "shutil",
            "subprocess",
            "sys",
            "time",
            "typing",
            "unicodedata",
            "urllib",
            "warnings",
        }
        self.assertEqual(imports - allowed - local_modules, set())

    def test_skill_bundles_no_mcp_server(self) -> None:
        scripts = SCRIPT.parent
        self.assertEqual(list(scripts.glob("*mcp*")), [])


if __name__ == "__main__":
    unittest.main()
