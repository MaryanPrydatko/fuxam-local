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

    def test_catalog_page_count_is_bounded(self) -> None:
        client = fuxam.FuxamClient()
        with (
            mock.patch.object(client, "study_plan", return_value={}),
            mock.patch.object(
                client,
                "bookable",
                return_value={"pageCount": fuxam.MAX_EXPLORE_PAGES + 1},
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
            "urllib",
        }
        self.assertEqual(imports - allowed, set())

    def test_skill_bundles_no_mcp_server(self) -> None:
        scripts = SCRIPT.parent
        self.assertEqual(list(scripts.glob("*mcp*")), [])


if __name__ == "__main__":
    unittest.main()
