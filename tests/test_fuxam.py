from __future__ import annotations

import argparse
import base64
import contextlib
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

    def test_enrolled_cli_labels_term_match_without_claiming_workload(self) -> None:
        payload = {
            "courses": [
                {
                    "name": "Clean Code (SE_08)",
                    "status": "ACTIVE",
                    "courseTags": [{"tag": {"name": "Offered in FS26"}}],
                    "modules": [],
                }
            ]
        }

        def invoke(output_format: str) -> tuple[int, str, str]:
            client = mock.Mock()
            client.enrolled.return_value = payload
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
        self.assertEqual(result["kind"], "active-learning-unit-offering-matches")
        self.assertEqual(result["termEnrollmentStatus"], "unknown")
        self.assertEqual(result["currentWorkloadStatus"], "unknown")
        self.assertFalse(result["progressChecked"])
        self.assertEqual(
            result["confirmedClaims"],
            [
                "learning-unit record is ACTIVE",
                "catalog offering tag matches FS26",
            ],
        )
        self.assertEqual(
            result["unconfirmedClaims"],
            [
                "enrolled in FS26",
                "taking in FS26",
                "needed in FS26",
                "not previously completed",
            ],
        )
        self.assertEqual(
            result["learningUnits"][0]["termRelationship"],
            "offering-tag-match-only",
        )
        self.assertNotEqual(result["kind"], "active-learning-unit-enrollments")
        self.assertNotIn("enrollment", result["evidence"])
        self.assertEqual(
            result["evidence"]["recordStatus"],
            "Fuxam reports ACTIVE; this can persist after completion",
        )

        table_code, table_output, table_error = invoke("table")
        self.assertEqual(table_code, 0)
        self.assertEqual(table_error, "")
        self.assertIn("Clean Code (SE_08)", table_output)
        self.assertIn(
            "An offering tag only shows availability; term enrollment and current "
            "workload are unknown.",
            table_output,
        )
        self.assertNotIn("Current learning units tagged as offered", table_output)

    def test_empty_term_match_makes_no_affirmative_record_claims(self) -> None:
        client = mock.Mock()
        client.enrolled.return_value = {"courses": []}
        stdout = io.StringIO()
        stderr = io.StringIO()
        argv = [
            str(SCRIPT),
            "enrolled",
            "--term",
            "FS26",
            "--format",
            "json",
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
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["confirmedClaims"], [])

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


class ReadOnlyContractTests(unittest.TestCase):
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

    def test_cli_exposes_no_account_mutation_commands(self) -> None:
        parser = fuxam.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        forbidden = {"book", "unbook", "join-waitlist", "leave-waitlist"}
        self.assertTrue(forbidden.isdisjoint(subparsers.choices))

    def test_parser_bounds_page_numbers(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            fuxam.build_parser().parse_args(["bookable", "--page", "0"])

    def test_source_contains_no_telemetry_or_mutation_action_identifiers(self) -> None:
        source = SCRIPT.read_text().lower()
        forbidden = (
            "posthog",
            "analytics",
            "bookcourseaction",
            "unbookcourseaction",
            "joinwaitlistaction",
            "leavewaitlistaction",
        )
        for value in forbidden:
            with self.subTest(value=value):
                self.assertNotIn(value, source)


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
        self.assertEqual(len(result["checks"]), 5)
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
            "ctypes",
            "getpass",
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
