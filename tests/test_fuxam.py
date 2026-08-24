from __future__ import annotations

import base64
import contextlib
import http.client
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".agents" / "skills" / "fuxam-local" / "scripts" / "fuxam.py"
SPEC = importlib.util.spec_from_file_location("fuxam_local_cli", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load the Fuxam CLI for tests.")
fuxam = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fuxam)


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

    def study_plan(self, focus_id: str | None, term_id: str | None) -> dict[str, str]:
        if focus_id is not None or term_id is not None:
            raise AssertionError("Smoke test must request the current plan.")
        return {"plan": "private-study-plan"}

    def agenda(
        self, direction: str, cursor: str | None, limit: int, past: bool
    ) -> list[dict[str, str]]:
        if (direction, cursor, limit, past) != ("initial", None, 1, False):
            raise AssertionError("Smoke test must use its bounded agenda request.")
        return [{"appointment": "private-appointment"}]

    def bookable(self, search: str, page: int, per_page: int) -> dict[str, object]:
        if (search, page, per_page) != ("", 1, 1):
            raise AssertionError("Smoke test must use its bounded catalog request.")
        return {"courses": [{"title": "private-bookable-course"}]}

    def term_courses(self) -> dict[str, object]:
        return synthetic_term_payload()


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
            b"2:ready\n"
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

        self.assertEqual(fuxam.parse_flight(body), {"items": [1, 2]})

    def test_truncated_flight_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "truncated"):
            fuxam.parse_flight(b'0:{"a":"$@1"}\n1:T20,{}')

    def test_duplicate_flight_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(fuxam.FuxamError, "malformed"):
            fuxam.parse_flight(b'0:{"a":"$@1"}\n0:{}')

    def test_jwt_claims_decodes_urlsafe_payload(self) -> None:
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "user", "exp": 2_000_000_000}).encode()
        ).rstrip(b"=")
        token = ".".join(("header", payload.decode(), "signature"))
        self.assertEqual(fuxam.jwt_claims(token), {"sub": "user", "exp": 2_000_000_000})

    def test_term_page_finds_one_authoritative_flight_payload(self) -> None:
        self.assertEqual(
            fuxam.parse_term_page(synthetic_term_html()), synthetic_term_payload()
        )

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

    def test_term_page_ignores_non_integer_flight_channels(self) -> None:
        record = "a:" + json.dumps(synthetic_term_payload()) + "\n"
        for channel in (True, 1.0):
            body = (
                "<script>self.__next_f.push("
                + json.dumps([channel, record])
                + ")</script>"
            ).encode()
            with (
                self.subTest(channel=channel),
                self.assertRaisesRegex(fuxam.FuxamError, "current-term"),
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
            mock.patch.object(fuxam, "MAX_RESPONSE_BYTES", 4),
            self.assertRaisesRegex(fuxam.FuxamError, "large"),
        ):
            fuxam.parse_term_page(b"12345")


class TerminalSummaryTests(unittest.TestCase):
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
        keychain = mock.Mock()
        keychain.get.return_value = "synthetic-client-cookie"

        with (
            mock.patch.object(fuxam, "Keychain", return_value=keychain),
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

    def test_explore_accepts_zero_page_count_as_an_empty_result(self) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "study_plan", return_value={}),
            mock.patch.object(
                client,
                "bookable",
                return_value={"pageCount": 0, "totalCount": 0, "courses": []},
            ) as bookable,
        ):
            result = client.explore("no matches")

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
                    return_value={"pageCount": page_count},
                ),
                self.assertRaisesRegex(fuxam.FuxamError, "page count"),
            ):
                client.explore("")

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
    def test_doctor_reports_credential_status_without_revealing_value(self) -> None:
        keychain = mock.Mock()
        keychain.get.return_value = "super-private-cookie"
        with (
            mock.patch.object(fuxam.sys, "platform", "darwin"),
            mock.patch.object(fuxam, "Keychain", return_value=keychain),
        ):
            result = fuxam.doctor_status()

        self.assertTrue(result["ok"])
        self.assertTrue(result["credential"]["configured"])
        self.assertNotIn("super-private-cookie", json.dumps(result))
        keychain.get.assert_called_once_with()

    def test_doctor_is_safe_on_an_unsupported_platform(self) -> None:
        with (
            mock.patch.object(fuxam.sys, "platform", "linux"),
            mock.patch.object(fuxam, "Keychain") as keychain,
        ):
            result = fuxam.doctor_status()

        self.assertFalse(result["ok"])
        self.assertIsNone(result["credential"]["configured"])
        keychain.assert_not_called()

    def test_doctor_redacts_keychain_failure_details(self) -> None:
        with (
            mock.patch.object(fuxam.sys, "platform", "darwin"),
            mock.patch.object(
                fuxam,
                "Keychain",
                side_effect=OSError("private-local-keychain-path"),
            ),
        ):
            result = fuxam.doctor_status()

        self.assertFalse(result["ok"])
        self.assertEqual(result["keychainError"], "KEYCHAIN_CHECK_FAILED")
        self.assertNotIn("private-local-keychain-path", json.dumps(result))

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
        imports = {
            line.split()[1].split(".")[0]
            for line in SCRIPT.read_text().splitlines()
            if line.startswith(("import ", "from "))
        }
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
            "json",
            "re",
            "sys",
            "time",
            "typing",
            "unicodedata",
            "urllib",
        }
        self.assertEqual(imports - allowed, set())

    def test_skill_bundles_no_mcp_server(self) -> None:
        scripts = SCRIPT.parent
        self.assertEqual(list(scripts.glob("*mcp*")), [])


if __name__ == "__main__":
    unittest.main()
