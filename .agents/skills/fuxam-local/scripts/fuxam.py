#!/usr/bin/env python3
"""Read-only local CLI for a CODE University student's Fuxam account."""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE_URL = "https://fuxam.app"
CLERK_URL = "https://clerk.fuxam.app"
CLERK_QUERY = "__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2"
VERSION = "0.3.0"
USER_AGENT = f"fuxam-local/{VERSION}"
KEYCHAIN_SERVICE = b"codex-fuxam-local"
KEYCHAIN_ACCOUNT = b"__client"
NOT_FOUND = -25300
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_COOKIE_BYTES = 16 * 1024
MAX_EXPLORE_PAGES = 100
MAX_SCRIPT_SOURCES = 128
READ_ONLY_ACTIONS = frozenset(
    {
        "checkCourseConflictsAction",
        "getAssociatedLearningUnits",
        "getBookableCoursesAction",
        "getModuleCatalogDetails",
        "getModuleInfo",
    }
)


class FuxamError(RuntimeError):
    pass


def doctor_status() -> dict[str, Any]:
    """Report local readiness without contacting Fuxam or exposing credentials."""
    python_supported = sys.version_info >= (3, 10)
    platform_supported = sys.platform == "darwin"
    configured: bool | None = None
    keychain_failed = False
    if platform_supported:
        try:
            configured = Keychain().get() is not None
        except (FuxamError, OSError, UnicodeError):
            keychain_failed = True

    result: dict[str, Any] = {
        "ok": (
            python_supported
            and platform_supported
            and configured is True
            and not keychain_failed
        ),
        "version": VERSION,
        "python": {
            "version": ".".join(str(part) for part in sys.version_info[:3]),
            "supported": python_supported,
        },
        "platform": {"name": sys.platform, "supported": platform_supported},
        "credential": {
            "storage": "macOS Keychain",
            "configured": configured,
        },
        "network": {
            "tested": False,
            "allowedOrigins": [BASE_URL, CLERK_URL],
        },
        "access": "read-only",
        "telemetry": False,
    }
    if keychain_failed:
        result["keychainError"] = "KEYCHAIN_CHECK_FAILED"
    return result


def validate_smoke_response(
    value: Any,
    *,
    require_object: bool = False,
    required_fields: tuple[str, ...] = (),
    any_field: tuple[str, ...] = (),
) -> str:
    if require_object:
        if not isinstance(value, dict):
            raise FuxamError("Smoke response was not an object.")
    elif not isinstance(value, dict | list):
        raise FuxamError("Smoke response was not structured data.")
    if isinstance(value, dict):
        if "error" in value:
            raise FuxamError("Smoke response contained an error envelope.")
        if any(
            not isinstance(value.get(field), str) or not value[field]
            for field in required_fields
        ):
            raise FuxamError("Smoke response omitted required fields.")
        if any_field and not any(field in value for field in any_field):
            raise FuxamError("Smoke response omitted its expected payload fields.")
    return "object" if isinstance(value, dict) else "array"


def smoke_test(client: FuxamClient, *, deep: bool = False) -> dict[str, Any]:
    """Exercise live read-only paths while returning metadata only."""
    checks: list[tuple[str, Any, Any]] = [
        (
            "context",
            client.context,
            lambda value: validate_smoke_response(
                value,
                require_object=True,
                required_fields=(
                    "userId",
                    "cohortId",
                    "studyProgramVersionId",
                ),
            ),
        ),
        ("enrolled", lambda: client.enrolled(""), validate_smoke_response),
        (
            "study-plan",
            lambda: client.study_plan(None, None),
            validate_smoke_response,
        ),
        (
            "agenda",
            lambda: client.agenda("initial", None, 1, False),
            validate_smoke_response,
        ),
    ]
    if deep:
        checks.append(
            (
                "bookable-server-action",
                lambda: client.bookable("", 1, 1),
                lambda value: validate_smoke_response(
                    value,
                    require_object=True,
                    any_field=("courses", "learningUnits", "pageCount"),
                ),
            )
        )

    reports: list[dict[str, Any]] = []
    for name, check, validate in checks:
        try:
            value = check()
            response_type = validate(value)
            reports.append({"name": name, "ok": True, "shape": {"type": response_type}})
        except (FuxamError, json.JSONDecodeError):
            reports.append({"name": name, "ok": False, "error": "FUXAM_CHECK_FAILED"})
        except Exception:
            reports.append(
                {
                    "name": name,
                    "ok": False,
                    "error": "LOCAL_CHECK_FAILED",
                }
            )
    return {
        "ok": all(report["ok"] for report in reports),
        "mode": "deep" if deep else "quick",
        "privacy": "No academic records are included in this report.",
        "checks": reports,
    }


def origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise FuxamError("Refused a malformed endpoint.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise FuxamError("Refused a non-HTTPS or malformed endpoint.")
    return parsed.scheme, parsed.hostname.lower(), port


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urljoin(req.full_url, newurl)
        if origin(req.full_url) != origin(target):
            raise FuxamError("Refused a cross-origin redirect.")
        return super().redirect_request(req, fp, code, msg, headers, target)


def normalize_client_cookie(value: str) -> str:
    normalized = value.removeprefix("__client=").split(";", 1)[0].strip()
    try:
        encoded = normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FuxamError("The credential was malformed.") from exc
    if (
        not encoded
        or len(encoded) > MAX_COOKIE_BYTES
        or any(byte < 0x21 or byte > 0x7E or byte == 0x3B for byte in encoded)
    ):
        raise FuxamError("The credential was empty or malformed.")
    return normalized


class Keychain:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise FuxamError("This skill currently requires macOS Keychain.")
        self.lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.core = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self.lib.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainItemFreeContent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.lib.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.lib.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.lib.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.lib.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.lib.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.lib.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self.lib.SecKeychainItemDelete.restype = ctypes.c_int32
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.core.CFRelease.restype = None

    def _find(self) -> tuple[int, bytes | None, ctypes.c_void_p]:
        length = ctypes.c_uint32()
        data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self.lib.SecKeychainFindGenericPassword(
            None,
            len(KEYCHAIN_SERVICE),
            KEYCHAIN_SERVICE,
            len(KEYCHAIN_ACCOUNT),
            KEYCHAIN_ACCOUNT,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status == NOT_FOUND:
            return status, None, item
        if status != 0:
            raise FuxamError(f"Keychain lookup failed with status {status}.")
        try:
            value = ctypes.string_at(data, length.value)
        finally:
            self.lib.SecKeychainItemFreeContent(None, data)
        return status, value, item

    def get(self) -> str | None:
        _, value, item = self._find()
        try:
            return value.decode("utf-8") if value is not None else None
        finally:
            if item.value:
                self.core.CFRelease(item)

    def set(self, value: str) -> None:
        encoded = value.encode("utf-8")
        status, _, item = self._find()
        try:
            if status == NOT_FOUND:
                result = self.lib.SecKeychainAddGenericPassword(
                    None,
                    len(KEYCHAIN_SERVICE),
                    KEYCHAIN_SERVICE,
                    len(KEYCHAIN_ACCOUNT),
                    KEYCHAIN_ACCOUNT,
                    len(encoded),
                    encoded,
                    None,
                )
            else:
                result = self.lib.SecKeychainItemModifyAttributesAndData(
                    item, None, len(encoded), encoded
                )
            if result != 0:
                raise FuxamError(f"Keychain update failed with status {result}.")
        finally:
            if item.value:
                self.core.CFRelease(item)

    def clear(self) -> bool:
        status, _, item = self._find()
        if status == NOT_FOUND:
            return False
        try:
            result = self.lib.SecKeychainItemDelete(item)
            if result != 0:
                raise FuxamError(f"Keychain deletion failed with status {result}.")
            return True
        finally:
            if item.value:
                self.core.CFRelease(item)


def as_object(value: Any, label: str = "response") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FuxamError(f"Fuxam returned an unsupported {label} shape.")
    return value


def canonical_term(value: str) -> str:
    """Normalize human semester names and CODE term codes."""
    normalized = re.sub(r"\s+", " ", value).strip()
    code_match = re.fullmatch(r"(?i)(FS|SS)[ _-]?(\d{2}|\d{4})", normalized)
    if code_match:
        year = code_match.group(2)[-2:]
        return f"{code_match.group(1).upper()}{year}"
    name_match = re.fullmatch(r"(?i)(fall|spring)(?: semester)?\s+(\d{4})", normalized)
    if name_match:
        prefix = "FS" if name_match.group(1).lower() == "fall" else "SS"
        return f"{prefix}{name_match.group(2)[-2:]}"
    raise FuxamError(
        "Use an unambiguous term such as FS26, Fall 2026, SS26, or Spring 2026."
    )


def _term_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return canonical_term(value)
    except FuxamError:
        return None


def _module_codes(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    matches = re.findall(
        r"(?<![A-Z0-9])([A-Z]{2,5}_\d{2,3})(?![A-Z0-9])", value.upper()
    )
    return list(dict.fromkeys(matches))


def _offering_terms(course: dict[str, Any]) -> tuple[list[str], bool]:
    terms: list[str] = []
    tags = course.get("courseTags", [])
    if not isinstance(tags, list):
        return terms, True
    malformed = False
    for link in tags:
        if not isinstance(link, dict):
            malformed = True
            continue
        tag = link.get("tag")
        if not isinstance(tag, dict):
            malformed = True
            continue
        name = tag.get("name")
        if not isinstance(name, str):
            malformed = True
            continue
        match = re.fullmatch(r"(?i)Offered in\s+(.+)", name)
        term = _term_or_none(match.group(1)) if match else None
        if match and not term:
            malformed = True
        if term and term not in terms:
            terms.append(term)
    return terms, malformed


def summarize_enrolled(value: Any, term: str | None = None) -> dict[str, Any]:
    """Return a small enrollment view with explicit evidence labels."""
    response = as_object(value, "enrolled courses")
    courses = response.get("courses")
    if not isinstance(courses, list):
        raise FuxamError("Fuxam returned unsupported enrolled-course data.")
    target = canonical_term(term) if term else None
    learning_units: list[dict[str, Any]] = []
    schema_issues = 0
    for course in courses:
        if not isinstance(course, dict):
            schema_issues += 1
            continue
        status = course.get("status")
        if not isinstance(status, str):
            schema_issues += 1
            continue
        if status != "ACTIVE":
            continue
        name = course.get("name")
        if not isinstance(name, str) or not name:
            schema_issues += 1
            continue
        offering_terms, malformed_tags = _offering_terms(course)
        if malformed_tags:
            schema_issues += 1
        if target and target not in offering_terms:
            continue
        explicit: list[dict[str, str | None]] = []
        explicit_codes: set[str] = set()
        modules = course.get("modules", [])
        if isinstance(modules, list):
            for module in modules:
                if not isinstance(module, dict):
                    schema_issues += 1
                    continue
                module_name = module.get("name")
                code = module.get("code")
                if not isinstance(code, str) or not code:
                    codes = _module_codes(module_name)
                    code = codes[0] if codes else None
                if code:
                    explicit_codes.add(code.upper())
                if code or isinstance(module_name, str):
                    explicit.append(
                        {
                            "code": code,
                            "name": module_name
                            if isinstance(module_name, str)
                            else None,
                        }
                    )
                else:
                    schema_issues += 1
        else:
            schema_issues += 1
        title_only = [
            code for code in _module_codes(name) if code.upper() not in explicit_codes
        ]
        learning_units.append(
            {
                "name": name,
                "status": status,
                "offeringTerms": offering_terms,
                "explicitModuleAssociations": explicit,
                "titleOnlyModuleCodes": title_only,
            }
        )
    learning_units.sort(key=lambda item: item["name"].casefold())
    warnings: list[str] = []
    if schema_issues:
        warnings.append(
            f"{schema_issues} enrolled-data field(s) used an unsupported shape; "
            "results may be incomplete."
        )
    return {
        "kind": "active-learning-unit-enrollments",
        "term": target,
        "total": len(learning_units),
        "learningUnits": learning_units,
        "complete": not schema_issues,
        "warnings": warnings,
        "evidence": {
            "enrollment": "current learning-unit enrollment",
            "term": "offering tag only",
            "explicitModuleAssociations": "learning-unit association, not election",
            "titleOnlyModuleCodes": "title mention only",
        },
    }


def _study_item_terms(
    item: dict[str, Any], term_names_by_id: dict[str, str]
) -> tuple[list[str], bool, bool]:
    terms: list[str] = []
    unresolved = False
    raw_direct = item.get("organizationTermName")
    direct = _term_or_none(raw_direct)
    if raw_direct not in (None, "") and not direct:
        unresolved = True
    if direct:
        terms.append(direct)
    organization_term = item.get("organizationTerm")
    nested = (
        _term_or_none(organization_term.get("name"))
        if isinstance(organization_term, dict)
        else None
    )
    if organization_term not in (None, {}) and not isinstance(organization_term, dict):
        unresolved = True
    elif isinstance(organization_term, dict):
        raw_nested = organization_term.get("name")
        if raw_nested not in (None, "") and not nested:
            unresolved = True
    if nested and nested not in terms:
        terms.append(nested)
    mapped: list[str] = []
    term_ids = item.get("electedTermIds", [])
    if isinstance(term_ids, list):
        for term_id in term_ids:
            term = term_names_by_id.get(term_id) if isinstance(term_id, str) else None
            if not term:
                unresolved = True
            if term and term not in terms:
                terms.append(term)
            if term and term not in mapped:
                mapped.append(term)
    else:
        unresolved = True
    sources = [{term} for term in (direct, nested) if term]
    if mapped:
        sources.append(set(mapped))
    conflict = len(sources) > 1 and any(source != sources[0] for source in sources[1:])
    return terms, conflict, unresolved


def summarize_modules(value: Any, term: str | None = None) -> dict[str, Any]:
    """Return formal study-plan elections, optionally narrowed to one term."""
    response = as_object(value, "study plan")
    target = canonical_term(term) if term else None
    term_names_by_id: dict[str, str] = {}
    available_terms: list[str] = []
    schema_issues = 0
    terms = response.get("availableTerms", [])
    if isinstance(terms, list):
        for available in terms:
            if not isinstance(available, dict):
                schema_issues += 1
                continue
            term_id = available.get("id")
            code = _term_or_none(available.get("name"))
            if isinstance(term_id, str) and code:
                term_names_by_id[term_id] = code
            elif term_id is not None or available.get("name") is not None:
                schema_issues += 1
            if code and code not in available_terms:
                available_terms.append(code)
    else:
        schema_issues += 1
    modules: list[dict[str, Any]] = []
    conflicts = 0
    unresolved_terms = 0
    unreadable_elections = 0
    groups = response.get("electiveGroups", [])
    if not isinstance(groups, list):
        raise FuxamError("Fuxam returned unsupported study-plan data.")
    for group in groups:
        if not isinstance(group, dict):
            schema_issues += 1
            continue
        items = group.get("availableStudyPlanItems", [])
        if not isinstance(items, list):
            schema_issues += 1
            continue
        for item in items:
            if not isinstance(item, dict):
                schema_issues += 1
                continue
            if item.get("isElected") is not True:
                continue
            item_terms, conflict, unresolved = _study_item_terms(item, term_names_by_id)
            if conflict:
                conflicts += 1
            term_ambiguous = conflict or unresolved or not item_terms
            if term_ambiguous:
                unresolved_terms += 1
            if target and (term_ambiguous or target not in item_terms):
                continue
            version = item.get("moduleVersion")
            if not isinstance(version, dict):
                unreadable_elections += 1
                continue
            course_module = version.get("courseModule")
            module_name = (
                course_module.get("name") if isinstance(course_module, dict) else None
            )
            if not isinstance(module_name, str) or not module_name:
                unreadable_elections += 1
                continue
            codes = _module_codes(module_name)
            modules.append(
                {
                    "code": "/".join(codes) if codes else None,
                    "codes": codes,
                    "name": module_name,
                    "version": (
                        version.get("name")
                        if isinstance(version.get("name"), str)
                        else None
                    ),
                    "ectsPoints": version.get("ectsPoints"),
                    "isCoreModule": (
                        item.get("isCoreModule")
                        if isinstance(item.get("isCoreModule"), bool)
                        else None
                    ),
                    "terms": item_terms,
                    "formalElection": True,
                    "termConflict": conflict,
                    "termStatus": "ambiguous" if term_ambiguous else "confirmed",
                }
            )
    modules.sort(key=lambda item: ((item["code"] or ""), item["name"].casefold()))
    warnings: list[str] = []
    if conflicts:
        warnings.append("Some election records contain conflicting term sources.")
    if unresolved_terms:
        if target:
            warnings.append(
                f"{unresolved_terms} elected record(s) could not be assigned "
                f"confidently while filtering for {target}."
            )
        else:
            warnings.append(
                f"{unresolved_terms} elected record(s) had unknown or conflicting "
                "term evidence."
            )
    if unreadable_elections:
        warnings.append(
            f"{unreadable_elections} elected record(s) could not be displayed because "
            "module details were incomplete."
        )
    if schema_issues:
        warnings.append(
            f"{schema_issues} study-plan field(s) used an unsupported shape; results "
            "may be incomplete."
        )
    return {
        "kind": "formal-module-elections",
        "term": target,
        "total": len(modules),
        "modules": modules,
        "availableTerms": sorted(available_terms),
        "complete": not (unresolved_terms or unreadable_elections or schema_issues),
        "unresolvedTermRecords": unresolved_terms,
        "unreadableElectionRecords": unreadable_elections,
        "warnings": warnings,
        "evidence": "study-plan records with isElected=true",
    }


def _clean_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    safe = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in str(value)
    ).replace("|", "¦")
    return " ".join(safe.split())


def _render_table(headers: list[str], rows: list[list[Any]]) -> str:
    cleaned = [[_clean_cell(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cleaned:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(
        " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        for row in cleaned
    )
    return "\n".join(lines)


def _append_warnings(output: str, result: dict[str, Any]) -> str:
    warnings = result.get("warnings", [])
    if not isinstance(warnings, list) or not warnings:
        return output
    return output + "\n\nWarning: " + " ".join(_clean_cell(item) for item in warnings)


def render_terminal_result(command: str, result: dict[str, Any]) -> str:
    term = result.get("term") or "all terms"
    if command == "enrolled":
        units = result.get("learningUnits", [])
        if not units:
            if result.get("term"):
                qualifier = "" if result.get("complete", True) else " confirmed"
                output = (
                    f"No{qualifier} active learning units tagged as offered in {term}."
                )
            else:
                qualifier = "" if result.get("complete", True) else " confirmed"
                output = f"No{qualifier} active learning units found."
            return _append_warnings(output, result)
        rows = []
        for unit in units:
            explicit = ", ".join(
                module.get("code") or module.get("name") or "unknown"
                for module in unit.get("explicitModuleAssociations", [])
            )
            advertised = ", ".join(unit.get("titleOnlyModuleCodes", []))
            rows.append([unit.get("name"), explicit, advertised, unit.get("status")])
        table = _render_table(
            ["Learning unit", "Explicit associations", "Title-only codes", "Status"],
            rows,
        )
        heading = (
            f"Current learning units tagged as offered in {term}:"
            if result.get("term")
            else "Current active learning units:"
        )
        modules_command = f"modules --term {term}" if result.get("term") else "modules"
        output = (
            f"{heading}\n\n{table}\n\n"
            "Explicit associations and title-only codes are not formal elections. "
            f"Use `{modules_command}` for study-plan elections."
        )
        return _append_warnings(output, result)
    if command == "modules":
        modules = result.get("modules", [])
        if not modules:
            qualifier = "" if result.get("complete", True) else " confirmed"
            return _append_warnings(
                f"No{qualifier} formal module elections found for {term}.", result
            )

        def module_type(value: Any) -> str:
            if value is True:
                return "core"
            if value is False:
                return "elective"
            return "unknown"

        rows = [
            [
                module.get("code"),
                module.get("name"),
                module.get("ectsPoints"),
                module_type(module.get("isCoreModule")),
                (
                    (", ".join(module.get("terms", [])) or "unknown")
                    + (
                        " (ambiguous)"
                        if module.get("termStatus") == "ambiguous"
                        else ""
                    )
                ),
            ]
            for module in modules
        ]
        output = f"Formal module elections for {term}:\n\n" + _render_table(
            ["Module", "Name", "ECTS", "Type", "Elected term"], rows
        )
        return _append_warnings(output, result)
    raise FuxamError("Table output is unavailable for this command.")


def jwt_claims(token: str) -> dict[str, Any]:
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return as_object(json.loads(base64.urlsafe_b64decode(segment)), "token")
    except (IndexError, ValueError, json.JSONDecodeError) as exc:
        raise FuxamError("Clerk returned an invalid session token.") from exc


def parse_flight(body: bytes) -> Any:
    records: dict[str, str] = {}
    offset = 0
    try:
        while offset < len(body):
            while offset < len(body) and body[offset] in (10, 13):
                offset += 1
            if offset >= len(body):
                break
            colon = body.find(b":", offset)
            if colon < 0:
                raise FuxamError("Fuxam returned malformed action data.")
            record_id = body[offset:colon].decode("ascii")
            if not record_id or record_id in records:
                raise FuxamError("Fuxam returned malformed action data.")
            offset = colon + 1
            if offset < len(body) and body[offset] == ord("T"):
                comma = body.find(b",", offset + 1)
                if comma < 0:
                    raise FuxamError("Fuxam returned malformed action data.")
                byte_length = int(body[offset + 1 : comma], 16)
                start = comma + 1
                if byte_length > len(body) - start:
                    raise FuxamError("Fuxam returned truncated action data.")
                end = start + byte_length
                records[record_id] = body[start:end].decode("utf-8")
                offset = end
            else:
                newline = body.find(b"\n", offset)
                end = len(body) if newline < 0 else newline
                records[record_id] = body[offset:end].decode("utf-8").rstrip("\r")
                offset = len(body) if newline < 0 else newline + 1
    except (UnicodeDecodeError, ValueError) as exc:
        raise FuxamError("Fuxam returned malformed action data.") from exc
    try:
        root = as_object(json.loads(records["0"]), "action")
        match = re.fullmatch(r"\$@(.+)", str(root.get("a", "")))
        if not match or match.group(1) not in records:
            raise KeyError
        payload = records[match.group(1)]
        if payload.startswith("E"):
            raise FuxamError("Fuxam rejected the action request.")
        value = json.loads(payload)
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise FuxamError("Fuxam returned unsupported action data.") from exc

    def resolve(item: Any) -> Any:
        if item == "$undefined":
            return None
        if isinstance(item, str):
            match = re.fullmatch(r"\$([0-9a-f]+)", item)
            return records.get(match.group(1), item) if match else item
        if isinstance(item, list):
            return [resolve(child) for child in item]
        if isinstance(item, dict):
            return {key: resolve(child) for key, child in item.items()}
        return item

    return resolve(value)


class FuxamClient:
    def __init__(self) -> None:
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.user_id: str | None = None
        self.context_cache: dict[str, str] | None = None
        self.build_id: str | None = None
        self.actions: dict[str, str] = {}

    def _open(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        authenticated: bool = True,
        require_context: bool = True,
        retry: bool = True,
    ) -> bytes:
        request_headers = {"user-agent": USER_AGENT, **(headers or {})}
        _, hostname, port = origin(url)
        if port != 443:
            raise FuxamError("Refused a non-standard HTTPS port.")
        if hostname not in {"fuxam.app", "clerk.fuxam.app"}:
            raise FuxamError("Refused an endpoint outside the Fuxam allowlist.")
        if authenticated:
            if hostname != "fuxam.app":
                raise FuxamError("Refused to send a Fuxam token to another host.")
            request_headers["authorization"] = f"Bearer {self._session_token()}"
            if require_context:
                self.context()
        elif (
            any(name.lower() == "cookie" for name in request_headers)
            and hostname != "clerk.fuxam.app"
        ):
            raise FuxamError("Refused to send a Clerk cookie to another host.")
        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )
        try:
            opener = urllib.request.build_opener(SameOriginRedirectHandler())
            with opener.open(request, timeout=30) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise FuxamError("Fuxam returned an unexpectedly large response.")
                return body
        except urllib.error.HTTPError as exc:
            if authenticated and exc.code == 401 and retry:
                self._session_token(force=True)
                return self._open(
                    url,
                    method=method,
                    headers=headers,
                    data=data,
                    authenticated=authenticated,
                    require_context=require_context,
                    retry=False,
                )
            raise FuxamError(f"Fuxam request failed with HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise FuxamError("Could not reach Fuxam.") from exc

    def _json(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        *,
        require_context: bool = True,
    ) -> Any:
        url = urllib.parse.urljoin(BASE_URL, path)
        filtered = {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in (query or {}).items()
            if value is not None
        }
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)
        body = self._open(
            url,
            headers={"accept": "application/json"},
            require_context=require_context,
        )
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise FuxamError(f"Fuxam returned non-JSON data for {path}.") from exc

    def _session_token(self, force: bool = False) -> str:
        if not force and self.token and time.time() < self.token_expires_at - 10:
            return self.token
        cookie = Keychain().get()
        if not cookie:
            raise FuxamError(
                "Authentication is not configured. Run `fuxam.py auth set` locally."
            )
        cookie = normalize_client_cookie(cookie)
        clerk_headers = {
            "cookie": f"__client={cookie}",
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/",
            "user-agent": USER_AGENT,
        }
        client_url = f"{CLERK_URL}/v1/client?{CLERK_QUERY}"
        client = as_object(
            json.loads(
                self._open(client_url, headers=clerk_headers, authenticated=False)
            ),
            "Clerk client",
        )
        client = as_object(client.get("response", client), "Clerk client")
        sessions = [
            session
            for session in client.get("sessions", [])
            if isinstance(session, dict) and session.get("status") == "active"
        ]
        active_id = client.get("last_active_session_id")
        active = next((s for s in sessions if s.get("id") == active_id), None)
        if active is None and len(sessions) == 1:
            active = sessions[0]
        if active is None:
            raise FuxamError("No unambiguous active Fuxam session exists in Clerk.")
        last_token = as_object(active.get("last_active_token"), "Clerk token")
        if not isinstance(last_token.get("jwt"), str):
            raise FuxamError("The active Clerk session cannot be renewed.")
        form = urllib.parse.urlencode(
            {
                "organization_id": active.get("last_active_organization_id") or "",
                "tab_state": "focused",
                "token": last_token["jwt"],
            }
        ).encode()
        token_url = (
            f"{CLERK_URL}/v1/client/sessions/"
            f"{urllib.parse.quote(str(active['id']), safe='')}/tokens?{CLERK_QUERY}"
        )
        token_result = as_object(
            json.loads(
                self._open(
                    token_url,
                    method="POST",
                    headers={
                        **clerk_headers,
                        "content-type": "application/x-www-form-urlencoded",
                    },
                    data=form,
                    authenticated=False,
                )
            ),
            "Clerk token",
        )
        token = token_result.get("jwt")
        if not isinstance(token, str):
            raise FuxamError("Clerk returned no session token.")
        claims = jwt_claims(token)
        if not isinstance(claims.get("sub"), str) or not isinstance(
            claims.get("exp"), int | float
        ):
            raise FuxamError("The Clerk token is missing identity or expiry data.")
        if self.user_id and self.user_id != claims["sub"]:
            self.context_cache = None
            self.actions.clear()
        self.user_id = claims["sub"]
        self.token = token
        self.token_expires_at = float(claims["exp"])
        return token

    def context(self) -> dict[str, str]:
        if self.context_cache is not None:
            return self.context_cache
        self._session_token()
        institutions = self._json(
            "/api/user/get-institutions-user-has-access-to", require_context=False
        )
        cohorts = self._json(
            "/api/calendar/cohorts-for-filters",
            {"take": 1000},
            require_context=False,
        )
        supported = (
            [
                item
                for item in institutions
                if isinstance(item, dict) and item.get("slug") == "code"
            ]
            if isinstance(institutions, list)
            else []
        )
        contexts = (
            [
                item
                for item in cohorts
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("studyProgramVersionId"), str)
            ]
            if isinstance(cohorts, list)
            else []
        )
        if len(supported) != 1:
            raise FuxamError(
                "Expected exactly one CODE University institution context."
            )
        if len(contexts) != 1:
            raise FuxamError("Expected exactly one cohort and study-program context.")
        if not self.user_id:
            raise FuxamError("The authenticated session has no user ID.")
        user_path = f"/en/dashboard/home/{urllib.parse.quote(self.user_id, safe='')}"
        self.context_cache = {
            "userId": self.user_id,
            "cohortId": contexts[0]["id"],
            "studyProgramVersionId": contexts[0]["studyProgramVersionId"],
            "dashboardUrl": f"{BASE_URL}{user_path}/my-courses",
            "moduleCatalogUrl": f"{BASE_URL}{user_path}/module-catalog",
        }
        return self.context_cache

    def enrolled(self, search: str = "") -> Any:
        return self._json("/api/widgets/get-courses", {"search": search})

    def study_plan(self, focus_id: str | None, term_id: str | None) -> Any:
        context = self.context()
        return self._json(
            "/api/user-study-plan/elections",
            {
                "userId": context["userId"],
                "studyProgramVersionId": context["studyProgramVersionId"],
                "focusId": focus_id,
                "organizationTermId": term_id,
            },
        )

    def search(self, query: str, types: str, limit: int, offset: int) -> Any:
        return self._json(
            "/api/global-search",
            {"q": query, "types": types, "limit": limit, "offset": offset},
        )

    def agenda(self, direction: str, cursor: str | None, limit: int, past: bool) -> Any:
        return self._json(
            "/api/user/paginated-agenda",
            {
                "organizerFilter": "all",
                "timeRange": "all_upcoming",
                "showPastAppointments": past,
                "direction": direction,
                "cursor": cursor,
                "limit": limit,
            },
        )

    def course_appointments(
        self, layer_id: str, direction: str, cursor: str | None, limit: int
    ) -> Any:
        return self._json(
            f"/api/course/{urllib.parse.quote(layer_id, safe='')}/paginated-agenda",
            {"direction": direction, "cursor": cursor, "limit": limit},
        )

    def course_deadlines(
        self, layer_id: str, language: str, cursor: str | None, limit: int
    ) -> Any:
        return self._json(
            f"/api/course/{urllib.parse.quote(layer_id, safe='')}/dashboard/deadlines",
            {"lang": language, "cursor": cursor, "limit": limit},
        )

    def module_attempts(self, module_version_id: str) -> Any:
        return self._json(
            "/api/user-study-plan/module-exam-attempts",
            {"userId": self.context()["userId"], "moduleVersionId": module_version_id},
        )

    def pinned(self) -> Any:
        return self._json("/api/sidebar/get-pinned-courses")

    def todos(self, time_period: str) -> Any:
        return self._json(
            "/api/widgets/get-todo-content-blocks",
            {
                "timePeriod": time_period,
                "showNotStarted": True,
                "showInProgress": True,
                "showCompleted": True,
                "showReviewed": True,
                "showLocked": True,
            },
        )

    def layer_path(self, layer_id: str) -> Any:
        encoded = urllib.parse.quote(layer_id, safe="")
        return self._json(f"/api/structure/get-layer-path/{encoded}")

    def excluded_dates(self, start: str, end: str) -> Any:
        return self._json(
            "/api/global-excluded-dates/applicable",
            {
                "from": start,
                "to": end,
                "scope": "user",
                "userId": self.context()["userId"],
            },
        )

    def exam_details(
        self,
        exam_id: str,
        module_version_id: str,
        term_id: str,
        attempt_number: int,
    ) -> Any:
        context = self.context()
        return self._json(
            "/api/user-curriculum/exam-details",
            {
                "examId": exam_id,
                "moduleVersionId": module_version_id,
                "organizationTermId": term_id,
                "attemptNumber": attempt_number,
                "cohortId": context["cohortId"],
                "userId": context["userId"],
            },
        )

    def _action_sources(self, page_url: str) -> dict[str, set[str]]:
        try:
            html = self._open(page_url, headers={"accept": "text/html"}).decode()
        except UnicodeDecodeError as exc:
            raise FuxamError("Fuxam returned malformed page data.") from exc
        sources = {
            urllib.parse.urljoin(page_url, match)
            for match in re.findall(
                r'<script[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', html
            )
            if "/_next/" in match
        }
        if len(sources) > MAX_SCRIPT_SOURCES:
            raise FuxamError("Fuxam returned unexpectedly many script sources.")
        actions: dict[str, set[str]] = {}
        pattern = re.compile(
            r'createServerReference\)\("([0-9a-f]{40,64})",[^)]{0,240},"([A-Za-z_$][\w$]*)"\)'
        )
        for source in sources:
            try:
                script = self._open(source, require_context=False).decode()
            except UnicodeDecodeError as exc:
                raise FuxamError("Fuxam returned malformed script data.") from exc
            for action_id, name in pattern.findall(script):
                actions.setdefault(name, set()).add(action_id)
        return actions

    def _action(self, name: str, args: list[Any], page_url: str | None = None) -> Any:
        if name not in READ_ONLY_ACTIONS:
            raise FuxamError("Refused a non-read-only Fuxam action.")
        context = self.context()
        target = page_url or context["dashboardUrl"]
        build = as_object(self._json("/api/build-id"), "build")
        current_build = build.get("buildId")
        if not isinstance(current_build, str):
            raise FuxamError("Fuxam returned no build ID.")
        if current_build != self.build_id:
            self.build_id = current_build
            self.actions.clear()
        if name not in self.actions:
            candidates = self._action_sources(target).get(name, set())
            if len(candidates) != 1:
                raise FuxamError(
                    f"Could not uniquely resolve Fuxam action {name}; the frontend may have changed."
                )
            self.actions[name] = next(iter(candidates))
        body = self._open(
            target,
            method="POST",
            headers={
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": self.actions[name],
            },
            data=json.dumps(args, separators=(",", ":")).encode(),
        )
        return parse_flight(body)

    def bookable(self, search: str, page: int, per_page: int) -> Any:
        return self._action(
            "getBookableCoursesAction",
            [{"search": search, "page": page, "perPage": per_page}],
        )

    def conflicts(self, course_ids: list[str]) -> Any:
        unique_ids = list(dict.fromkeys(course_ids))
        return self._action(
            "checkCourseConflictsAction",
            [{"courseIds": unique_ids, "includeAppointmentsForCourseIds": unique_ids}],
        )

    def module_details(
        self,
        course_module_id: str,
        module_version_id: str,
        term_id: str | None,
    ) -> Any:
        context = self.context()
        page = (
            f"{context['moduleCatalogUrl']}/"
            f"{urllib.parse.quote(course_module_id, safe='')}/"
            f"{urllib.parse.quote(module_version_id, safe='')}"
        )
        sections: dict[str, Any] = {}
        failures: list[str] = []
        calls = (
            (
                "catalogDetails",
                "getModuleCatalogDetails",
                [course_module_id, module_version_id],
            ),
            ("moduleInfo", "getModuleInfo", [module_version_id]),
            (
                "associatedLearningUnits",
                "getAssociatedLearningUnits",
                [module_version_id, term_id],
            ),
        )
        for section, action, arguments in calls:
            try:
                sections[section] = self._action(action, arguments, page)
            except FuxamError:
                sections[section] = None
                failures.append(section)
        try:
            sections["attempts"] = self.module_attempts(module_version_id)
        except FuxamError:
            sections["attempts"] = None
            failures.append("attempts")
        return {
            "courseModuleId": course_module_id,
            "moduleVersionId": module_version_id,
            "organizationTermId": term_id,
            **sections,
            "partial": bool(failures),
            "unavailableSections": failures,
        }

    def explore(self, query: str) -> Any:
        study_plan: Any = None
        warnings: list[str] = []
        try:
            study_plan = self.study_plan(None, None)
        except FuxamError:
            warnings.append("studyPlan unavailable")
        first = as_object(self.bookable(query, 1, 100), "bookable courses")
        page_count = first.get("pageCount", 1)
        if type(page_count) is not int or not 0 <= page_count <= MAX_EXPLORE_PAGES:
            raise FuxamError("Fuxam returned an invalid catalog page count.")
        pages = [first] if page_count else []
        for page in range(2, page_count + 1):
            pages.append(as_object(self.bookable(query, page, 100), "bookable courses"))
        courses: dict[str, Any] = {}
        for page in pages:
            values = page.get("courses", page.get("learningUnits", []))
            if isinstance(values, list):
                for course in values:
                    if isinstance(course, dict) and isinstance(course.get("id"), str):
                        courses[course["id"]] = course
        return {
            "studyPlan": study_plan,
            "learningUnits": list(courses.values()),
            "query": query,
            "partial": bool(warnings),
            "warnings": warnings,
        }


def add_common_page_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--direction", choices=("initial", "past", "future"), default="initial"
    )
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=bounded_int(1, 1000), default=100)


def add_output_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--format",
        choices=("json", "table"),
        default="json",
        dest="output_format",
        help="output structured JSON or a compact terminal table",
    )


def bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"expected an integer from {minimum} through {maximum}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check local readiness without contacting Fuxam")
    smoke = commands.add_parser(
        "smoke-test", help="Run live read-only checks without returning academic data"
    )
    smoke.add_argument(
        "--deep",
        action="store_true",
        help="also verify the frontend-backed bookable-course action",
    )
    auth = commands.add_parser("auth", help="Manage the local Keychain credential")
    auth.add_argument("operation", choices=("set", "status", "clear"))
    commands.add_parser("context", help="Verify the CODE study context")
    enrolled = commands.add_parser("enrolled", help="List enrolled courses")
    enrolled.add_argument("--search", default="")
    enrolled.add_argument(
        "--term",
        help="filter offering tags by FS26, Fall 2026, SS26, or Spring 2026",
    )
    add_output_format_argument(enrolled)
    modules = commands.add_parser(
        "modules", help="List formally elected study-plan modules"
    )
    modules.add_argument("--term", help="filter formal elections by semester")
    add_output_format_argument(modules)
    bookable = commands.add_parser(
        "bookable", help="List one page of bookable learning units"
    )
    bookable.add_argument("--search", default="")
    bookable.add_argument("--page", type=bounded_int(1, 10000), default=1)
    bookable.add_argument("--per-page", type=bounded_int(1, 100), default=100)
    explore = commands.add_parser(
        "explore", help="Get the study plan and all matching learning units"
    )
    explore.add_argument("--query", default="")
    plan = commands.add_parser("study-plan", help="Get the current study plan")
    plan.add_argument("--focus-id")
    plan.add_argument("--term-id")
    search = commands.add_parser("search", help="Search Fuxam entities")
    search.add_argument("query")
    search.add_argument("--types", default="course,module,appointment,exam,term")
    search.add_argument("--limit", type=bounded_int(1, 1000), default=25)
    search.add_argument("--offset", type=bounded_int(0, 1000000), default=0)
    agenda = commands.add_parser("agenda", help="List student appointments")
    add_common_page_arguments(agenda)
    agenda.add_argument("--past", action="store_true")
    appointments = commands.add_parser(
        "course-appointments", help="List appointments for a course layer"
    )
    appointments.add_argument("layer_id")
    add_common_page_arguments(appointments)
    deadlines = commands.add_parser(
        "course-deadlines", help="List deadlines for a course layer"
    )
    deadlines.add_argument("layer_id")
    deadlines.add_argument("--language", choices=("en", "de"), default="en")
    deadlines.add_argument("--cursor")
    deadlines.add_argument("--limit", type=bounded_int(1, 1000), default=100)
    attempts = commands.add_parser(
        "module-attempts", help="Get concrete exam attempts for a module version"
    )
    attempts.add_argument("module_version_id")
    module = commands.add_parser(
        "module-details", help="Inspect one module and its learning units"
    )
    module.add_argument("course_module_id")
    module.add_argument("module_version_id")
    module.add_argument("--term-id")
    conflicts = commands.add_parser(
        "conflicts", help="Check a proposed course set for schedule conflicts"
    )
    conflicts.add_argument("course_ids", nargs="+")
    commands.add_parser("pinned", help="List pinned courses")
    todos = commands.add_parser("todos", help="List Fuxam todo items")
    todos.add_argument("--time-period", default="all")
    layer = commands.add_parser("layer-path", help="Resolve a course layer path")
    layer.add_argument("layer_id")
    excluded = commands.add_parser(
        "excluded-dates", help="List applicable excluded dates"
    )
    excluded.add_argument("start", help="ISO date or datetime")
    excluded.add_argument("end", help="ISO date or datetime")
    exam = commands.add_parser("exam-details", help="Get details for one exam attempt")
    exam.add_argument("exam_id")
    exam.add_argument("module_version_id")
    exam.add_argument("term_id")
    exam.add_argument("attempt_number", type=bounded_int(1, 100))
    return root


def run(args: argparse.Namespace) -> Any:
    if args.command == "doctor":
        return doctor_status()

    if args.command == "auth":
        keychain = Keychain()
        if args.operation == "status":
            return {
                "configured": keychain.get() is not None,
                "storage": "macOS Keychain",
            }
        if args.operation == "clear":
            return {"removed": keychain.clear()}
        value = normalize_client_cookie(
            getpass.getpass("Fuxam __client value (hidden): ")
        )
        keychain.set(value)
        return {"configured": True, "storage": "macOS Keychain"}

    client = FuxamClient()
    if args.command == "smoke-test":
        return smoke_test(client, deep=args.deep)
    if args.command == "context":
        context = client.context().copy()
        context.pop("dashboardUrl", None)
        context.pop("moduleCatalogUrl", None)
        return context
    if args.command == "enrolled":
        enrolled = client.enrolled(args.search)
        if args.term or args.output_format == "table":
            return summarize_enrolled(enrolled, args.term)
        return enrolled
    if args.command == "modules":
        return summarize_modules(client.study_plan(None, None), args.term)
    if args.command == "bookable":
        return client.bookable(args.search, args.page, args.per_page)
    if args.command == "explore":
        return client.explore(args.query)
    if args.command == "study-plan":
        return client.study_plan(args.focus_id, args.term_id)
    if args.command == "search":
        return client.search(args.query, args.types, args.limit, args.offset)
    if args.command == "agenda":
        return client.agenda(args.direction, args.cursor, args.limit, args.past)
    if args.command == "course-appointments":
        return client.course_appointments(
            args.layer_id, args.direction, args.cursor, args.limit
        )
    if args.command == "course-deadlines":
        return client.course_deadlines(
            args.layer_id, args.language, args.cursor, args.limit
        )
    if args.command == "module-attempts":
        return client.module_attempts(args.module_version_id)
    if args.command == "module-details":
        return client.module_details(
            args.course_module_id, args.module_version_id, args.term_id
        )
    if args.command == "conflicts":
        return client.conflicts(args.course_ids)
    if args.command == "pinned":
        return client.pinned()
    if args.command == "todos":
        return client.todos(args.time_period)
    if args.command == "layer-path":
        return client.layer_path(args.layer_id)
    if args.command == "excluded-dates":
        return client.excluded_dates(args.start, args.end)
    if args.command == "exam-details":
        return client.exam_details(
            args.exam_id,
            args.module_version_id,
            args.term_id,
            args.attempt_number,
        )
    raise AssertionError("Unhandled command")


def main() -> int:
    try:
        args = build_parser().parse_args()
        result = run(args)
        if getattr(args, "output_format", "json") == "table":
            sys.stdout.write(render_terminal_result(args.command, result) + "\n")
        else:
            json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        if args.command in {"doctor", "smoke-test"} and not result.get("ok", False):
            return 1
        return 0
    except (FuxamError, json.JSONDecodeError) as exc:
        json.dump({"error": str(exc)}, sys.stderr, ensure_ascii=False)
        sys.stderr.write("\n")
        return 1
    except KeyboardInterrupt:
        json.dump({"error": "Cancelled."}, sys.stderr)
        sys.stderr.write("\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
