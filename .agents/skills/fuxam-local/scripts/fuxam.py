#!/usr/bin/env python3
"""Fuxam study data and course bookings."""

from __future__ import annotations

import argparse
import ctypes
import getpass
import hashlib
import hmac
import http.client
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections.abc import Callable
from typing import Any

from fuxam_errors import FuxamError
from fuxam_protocol import (
    MAX_RESPONSE_BYTES,
    as_object,
    jwt_claims,
    parse_flight,
    parse_term_page,
)

BASE_URL = "https://fuxam.app"
CLERK_URL = "https://clerk.fuxam.app"
CLERK_QUERY = "__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2"
VERSION = "0.4.2"
USER_AGENT = f"fuxam-local/{VERSION}"
KEYCHAIN_SERVICE = b"codex-fuxam-local"
KEYCHAIN_ACCOUNT = b"__client"
NOT_FOUND = -25300
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
MUTATION_ACTIONS = frozenset(
    {
        "bookCoursesAction",
        "unbookCourseAction",
        "joinWaitlistAction",
        "leaveWaitlistAction",
    }
)
BOOKING_ACTIONS: dict[str, tuple[str, str]] = {
    "enroll": ("bookCoursesAction", "ENROLLED"),
    "unenroll": ("unbookCourseAction", "NOT_ENROLLED"),
    "join-waitlist": ("joinWaitlistAction", "WAITLISTED"),
    "leave-waitlist": ("leaveWaitlistAction", "NOT_WAITLISTED"),
}
TERM_CATEGORY_STATES = {
    "myCourses": "ENROLLED",
    "waitlist": "WAITLISTED",
    "selfStudy": "SELF_STUDY",
}
MAX_COURSE_ID_BYTES = 200


class MutationOutcomeUnknown(FuxamError):
    """A mutation may have reached Fuxam; callers must reconcile by reading."""

    def __init__(self) -> None:
        super().__init__("The Fuxam mutation outcome is unknown.")


class MutationPreconditionChanged(FuxamError):
    """A preview-bound account or build changed before the mutation request."""


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
        "access": "read with guarded booking writes",
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
            lambda value: validate_study_plan(value) and "object",
        ),
        (
            "agenda",
            lambda: client.agenda("initial", None, 1, False),
            validate_smoke_response,
        ),
    ]
    if deep:
        checks.extend(
            [
                (
                    "active-term-bookings",
                    client.term_courses,
                    lambda value: (summarize_term_courses(value) and "object"),
                ),
                (
                    "bookable-server-action",
                    lambda: client.bookable("", 1, 1),
                    lambda value: validate_bookable_page(value) and "object",
                ),
            ]
        )

    reports: list[dict[str, Any]] = []
    for name, check, validate in checks:
        try:
            value = check()
            response_type = validate(value)
            reports.append({"name": name, "ok": True, "shape": {"type": response_type}})
        except MutationPreconditionChanged:
            raise
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
    target = canonical_term(term) if term is not None else None
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
                "termRelationship": (
                    "offering-tag-match-only" if target else "not-filtered-by-term"
                ),
                "termEnrollmentStatus": "unknown",
                "currentWorkloadStatus": "unknown",
                "completionStatus": "not-checked",
                "progressChecked": False,
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
        "kind": (
            "active-learning-unit-offering-matches"
            if target
            else "active-learning-unit-records"
        ),
        "term": target,
        "total": len(learning_units),
        "learningUnits": learning_units,
        "termEnrollmentStatus": "unknown",
        "currentWorkloadStatus": "unknown",
        "completionStatus": "not-checked",
        "progressChecked": False,
        "confirmedClaims": (
            [
                "learning-unit record is ACTIVE",
                *([f"catalog offering tag matches {target}"] if target else []),
            ]
            if learning_units
            else []
        ),
        "unconfirmedClaims": (
            [
                f"enrolled in {target}",
                f"taking in {target}",
                f"needed in {target}",
                "not previously completed",
            ]
            if target
            else ["current workload", "still needed", "not previously completed"]
        ),
        "complete": not schema_issues,
        "warnings": warnings,
        "evidence": {
            "recordStatus": "Fuxam reports ACTIVE; this can persist after completion",
            "term": "catalog offering tag only",
            "completion": "not checked",
            "explicitModuleAssociations": "learning-unit association, not election",
            "titleOnlyModuleCodes": "title mention only",
        },
    }


def _exact_optional_int(
    value: Any, label: str, *, positive: bool = False
) -> int | None:
    if value is None:
        return None
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        raise FuxamError(f"Fuxam returned unsupported {label} data.")
    return value


def summarize_term_courses(
    value: Any, requested_term: str | None = None
) -> dict[str, Any]:
    """Summarize booking states from Fuxam's current-term page."""
    response = as_object(value, "current-term booking")
    active_term = as_object(response.get("activeTerm"), "active term")
    term_id = active_term.get("id")
    term_name = active_term.get("name")
    if not isinstance(term_id, str) or not term_id:
        raise FuxamError("Fuxam returned no active term ID.")
    if not isinstance(term_name, str) or not term_name:
        raise FuxamError("Fuxam returned no active term name.")
    active_code = canonical_term(term_name)
    if requested_term is not None and canonical_term(requested_term) != active_code:
        raise FuxamError(
            f"TERM_NOT_ACTIVE: Fuxam exposes live booking state for {active_code}, "
            f"not {canonical_term(requested_term)}."
        )
    can_book = response.get("canBookCourses")
    waitlist_enabled = response.get("waitlistEnabled")
    if type(can_book) is not bool or type(waitlist_enabled) is not bool:
        raise FuxamError("Fuxam returned unsupported booking-policy data.")
    categories = as_object(response.get("coursesByCategory"), "course categories")
    expected_categories = ("myCourses", "waitlist", "selfStudy", "bookable")
    if any(not isinstance(categories.get(name), list) for name in expected_categories):
        raise FuxamError("Fuxam returned unsupported current-term course categories.")
    if set(categories) != set(expected_categories):
        raise FuxamError("Fuxam returned changed current-term course categories.")

    units: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    counts = {
        "enrolled": 0,
        "waitlisted": 0,
        "selfStudy": 0,
        "bookable": 0,
        "full": 0,
    }
    for category in expected_categories:
        for raw_course in categories[category]:
            course = as_object(raw_course, "current-term course")
            course_id = course.get("id")
            name = course.get("name")
            if not isinstance(course_id, str) or not course_id:
                raise FuxamError("Fuxam returned a course without an ID.")
            if course_id in seen_ids:
                raise FuxamError("Fuxam returned a duplicate current-term course ID.")
            seen_ids.add(course_id)
            if not isinstance(name, str) or not name:
                raise FuxamError("Fuxam returned a course without a name.")
            if (
                type(course.get("isEnrolled")) is not bool
                or type(course.get("isSelfStudy")) is not bool
            ):
                raise FuxamError("Fuxam returned unsupported course-state data.")
            booking = as_object(course.get("booking"), "course booking")
            is_full = booking.get("isFull")
            can_unbook = booking.get("canUnbook")
            if type(is_full) is not bool or type(can_unbook) is not bool:
                raise FuxamError("Fuxam returned unsupported course-booking flags.")
            waitlist_position = _exact_optional_int(
                booking.get("waitlistPosition"),
                "waitlist position",
                positive=True,
            )
            capacity = _exact_optional_int(booking.get("capacity"), "course capacity")
            enrolled_count = _exact_optional_int(
                booking.get("enrolledCount"), "enrolled count"
            )
            if category == "bookable":
                state = "FULL" if is_full else "BOOKABLE"
            else:
                state = TERM_CATEGORY_STATES[category]
            is_enrolled = course["isEnrolled"]
            is_self_study = course["isSelfStudy"]
            if category == "myCourses" and not is_enrolled:
                raise FuxamError("Fuxam returned an inconsistent enrolled course.")
            if category in {"waitlist", "bookable"} and (is_enrolled or is_self_study):
                raise FuxamError("Fuxam returned an inconsistent booking category.")
            if category == "selfStudy" and not is_self_study:
                raise FuxamError("Fuxam returned an inconsistent self-study course.")
            if state == "WAITLISTED" and waitlist_position is None:
                raise FuxamError("Fuxam returned a waitlist entry without a position.")
            if state != "WAITLISTED" and waitlist_position is not None:
                raise FuxamError("Fuxam returned an unexpected waitlist position.")
            count_key = {
                "ENROLLED": "enrolled",
                "WAITLISTED": "waitlisted",
                "SELF_STUDY": "selfStudy",
                "BOOKABLE": "bookable",
                "FULL": "full",
            }[state]
            counts[count_key] += 1
            layer_id = course.get("layerId")
            if layer_id is not None and not isinstance(layer_id, str):
                raise FuxamError("Fuxam returned unsupported course layer data.")
            enrollment_origin = booking.get("enrollmentOrigin")
            if enrollment_origin is not None and not isinstance(enrollment_origin, str):
                raise FuxamError("Fuxam returned unsupported enrollment-origin data.")
            units.append(
                {
                    "courseId": course_id,
                    "layerId": layer_id,
                    "name": name,
                    "state": state,
                    "category": category,
                    "capacity": capacity,
                    "enrolledCount": enrolled_count,
                    "isFull": is_full,
                    "canUnenroll": can_unbook,
                    "waitlistPosition": waitlist_position,
                    "enrollmentOrigin": enrollment_origin,
                }
            )
    state_order = {
        "ENROLLED": 0,
        "WAITLISTED": 1,
        "SELF_STUDY": 2,
        "BOOKABLE": 3,
        "FULL": 4,
    }
    units.sort(key=lambda item: (state_order[item["state"]], item["name"].casefold()))
    return {
        "kind": "active-term-learning-unit-bookings",
        "term": active_code,
        "termId": term_id,
        "termName": term_name,
        "total": len(units),
        "counts": counts,
        "canBookCourses": can_book,
        "waitlistEnabled": waitlist_enabled,
        "learningUnits": units,
        "complete": True,
        "warnings": [],
        "evidence": "Fuxam My Learning Units active-term booking categories",
    }


def summarize_term_enrolled(
    value: Any, requested_term: str | None = None
) -> dict[str, Any]:
    """Return only confirmed enrollments, keeping waitlist entries separate."""
    summary = summarize_term_courses(value, requested_term)
    enrolled = [
        item for item in summary["learningUnits"] if item["state"] == "ENROLLED"
    ]
    waitlisted = [
        item for item in summary["learningUnits"] if item["state"] == "WAITLISTED"
    ]
    return {
        **summary,
        "kind": "active-term-learning-unit-enrollments",
        "total": len(enrolled),
        "learningUnits": enrolled,
        "waitlistedTotal": len(waitlisted),
        "waitlistedLearningUnits": waitlisted,
        "confirmedClaims": [f"listed learning units are enrolled in {summary['term']}"],
        "unconfirmedClaims": ["completion status", "still needed"],
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


def validate_study_plan(value: Any) -> dict[str, Any]:
    response = as_object(value, "study plan")
    if (
        "error" in response
        or not isinstance(response.get("availableTerms"), list)
        or not isinstance(response.get("electiveGroups"), list)
    ):
        raise FuxamError("Fuxam returned unsupported study-plan data.")
    term_names_by_id: dict[str, str] = {}
    for term in response["availableTerms"]:
        if (
            not isinstance(term, dict)
            or not isinstance(term.get("id"), str)
            or not term["id"]
            or not isinstance(term.get("name"), str)
            or not term["name"]
        ):
            raise FuxamError("Fuxam returned unsupported study-plan term data.")
        name = _term_or_none(term["name"]) or term["name"]
        if term["id"] in term_names_by_id and term_names_by_id[term["id"]] != name:
            raise FuxamError("Fuxam returned conflicting study-plan term data.")
        term_names_by_id[term["id"]] = name
    for group in response["electiveGroups"]:
        if not isinstance(group, dict) or not isinstance(
            group.get("availableStudyPlanItems"), list
        ):
            raise FuxamError("Fuxam returned unsupported study-plan group data.")
        for item in group["availableStudyPlanItems"]:
            if not isinstance(item, dict) or type(item.get("isElected")) is not bool:
                raise FuxamError("Fuxam returned unsupported study-plan election data.")
    return response


def validate_bookable_page(value: Any) -> dict[str, Any]:
    response = as_object(value, "bookable courses")
    page_count = response.get("pageCount")
    if type(page_count) is not int or page_count < 0:
        raise FuxamError("Fuxam returned an invalid catalog page count.")
    fields = [field for field in ("courses", "learningUnits") if field in response]
    if "error" in response or not fields:
        raise FuxamError("Fuxam returned unsupported catalog data.")
    for field in fields:
        values = response[field]
        if not isinstance(values, list) or any(
            not isinstance(course, dict)
            or not isinstance(course.get("id"), str)
            or not course["id"].strip()
            for course in values
        ):
            raise FuxamError("Fuxam returned unsupported catalog course data.")
    courses = response[fields[0]]
    if any(response[field] != courses for field in fields[1:]):
        raise FuxamError("Fuxam returned conflicting catalog collections.")
    total_count = response.get("totalCount")
    if "totalCount" in response and (
        type(total_count) is not int or total_count < len(courses)
    ):
        raise FuxamError("Fuxam returned an invalid catalog total count.")
    if page_count == 0 and (courses or total_count not in (None, 0)):
        raise FuxamError("Fuxam returned contradictory empty catalog data.")
    if (
        page_count == 1
        and total_count is not None
        and total_count != len({course["id"] for course in courses})
    ):
        raise FuxamError("Fuxam returned an inconsistent catalog total count.")
    return {"pageCount": page_count, "totalCount": total_count, "courses": courses}


def summarize_modules(value: Any, term: str | None = None) -> dict[str, Any]:
    """Return formal study-plan elections, optionally narrowed to one term."""
    target = canonical_term(term) if term is not None else None
    response = validate_study_plan(value)
    term_names_by_id: dict[str, str] = {}
    available_terms: list[str] = []
    schema_issues = 0
    for available in response["availableTerms"]:
        code = _term_or_none(available["name"])
        if code:
            term_names_by_id[available["id"]] = code
            if code not in available_terms:
                available_terms.append(code)
        else:
            schema_issues += 1
    modules: list[dict[str, Any]] = []
    conflicts = 0
    unresolved_terms = 0
    unreadable_elections = 0
    for group in response["electiveGroups"]:
        for item in group["availableStudyPlanItems"]:
            if not item["isElected"]:
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
    if command in {"enrolled", "learning-units"} and result.get("kind") in {
        "active-term-learning-unit-enrollments",
        "active-term-learning-unit-bookings",
    }:
        units = result.get("learningUnits", [])

        def rows(values: list[dict[str, Any]]) -> list[list[Any]]:
            return [
                [
                    item.get("state"),
                    item.get("name"),
                    item.get("courseId"),
                    (
                        f"{item.get('enrolledCount')}/{item.get('capacity')}"
                        if item.get("capacity") is not None
                        and item.get("enrolledCount") is not None
                        else None
                    ),
                    item.get("waitlistPosition"),
                ]
                for item in values
            ]

        heading = (
            f"Confirmed enrollments for {term}:"
            if result["kind"] == "active-term-learning-unit-enrollments"
            else f"Active-term learning units for {term}:"
        )
        output = heading
        if units:
            output += "\n\n" + _render_table(
                ["State", "Learning unit", "Course ID", "Seats", "Waitlist"],
                rows(units),
            )
        else:
            output += "\n\nNone."
        waitlisted = result.get("waitlistedLearningUnits", [])
        if result["kind"] == "active-term-learning-unit-enrollments" and waitlisted:
            output += "\n\nWaitlisted (not enrolled):\n\n" + _render_table(
                ["State", "Learning unit", "Course ID", "Seats", "Waitlist"],
                rows(waitlisted),
            )
        return _append_warnings(output, result)
    if command == "enrolled":
        units = result.get("learningUnits", [])
        if not units:
            if result.get("term"):
                qualifier = "" if result.get("complete", True) else " confirmed"
                output = (
                    f"No{qualifier} ACTIVE learning-unit records carry an {term} "
                    "offering tag. This does not establish term enrollment or "
                    "current workload."
                )
            else:
                qualifier = "" if result.get("complete", True) else " confirmed"
                output = f"No{qualifier} ACTIVE learning-unit records found."
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
            f"ACTIVE learning-unit records with an {term} offering tag:"
            if result.get("term")
            else "ACTIVE learning-unit records:"
        )
        modules_command = f"modules --term {term}" if result.get("term") else "modules"
        caveat = (
            "An offering tag only shows availability; term enrollment and current "
            "workload are unknown. ACTIVE records may persist after completion.\n\n"
            if result.get("term")
            else "ACTIVE is a record status, not proof of unfinished work.\n\n"
        )
        output = f"{heading}\n\n{caveat}{table}\n\n"
        output += (
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


class FuxamClient:
    def __init__(self) -> None:
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.user_id: str | None = None
        self.organization_id = ""
        self.context_cache: dict[str, str] | None = None
        self.build_id: str | None = None
        self.actions: dict[tuple[str, str], str] = {}

    def _require_account(self, expected_account: str) -> None:
        if not self.user_id or not hmac.compare_digest(
            account_fingerprint(self.user_id, self.organization_id), expected_account
        ):
            raise MutationPreconditionChanged(
                "ACCOUNT_CHANGED: the signed-in Fuxam account changed."
            )

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
        expected_account: str | None = None,
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
            token = self._session_token()
            if expected_account is not None:
                self._require_account(expected_account)
            request_headers["authorization"] = f"Bearer {token}"
            if require_context:
                self.context()
        elif expected_account is not None:
            raise FuxamError("Refused an account-bound unauthenticated request.")
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
                    expected_account=expected_account,
                )
            raise FuxamError(f"Fuxam request failed with HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise FuxamError("Could not reach Fuxam.") from exc
        except http.client.HTTPException as exc:
            raise FuxamError("Fuxam returned an incomplete response.") from exc
        except OSError as exc:
            raise FuxamError("Could not reach Fuxam.") from exc

    def _json(
        self,
        path: str,
        query: dict[str, Any] | None = None,
        *,
        require_context: bool = True,
        expected_account: str | None = None,
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
            expected_account=expected_account,
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
        raw_organization_id = active.get("last_active_organization_id")
        if raw_organization_id is None:
            organization_id = ""
        elif isinstance(raw_organization_id, str):
            organization_id = raw_organization_id
        else:
            raise FuxamError("Clerk returned an invalid organization context.")
        form = urllib.parse.urlencode(
            {
                "organization_id": organization_id,
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
        expiry = claims.get("exp")
        if not isinstance(claims.get("sub"), str) or type(expiry) not in (int, float):
            raise FuxamError("The Clerk token is missing identity or expiry data.")
        try:
            token_expires_at = float(expiry)
        except OverflowError as exc:
            raise FuxamError("The Clerk token has an invalid expiry.") from exc
        if not math.isfinite(token_expires_at):
            raise FuxamError("The Clerk token has an invalid expiry.")
        if self.user_id and (
            self.user_id != claims["sub"] or self.organization_id != organization_id
        ):
            raise MutationPreconditionChanged(
                "ACCOUNT_CHANGED: the signed-in Fuxam account changed. "
                "Start a new command for the intended account."
            )
        self.user_id = claims["sub"]
        self.organization_id = organization_id
        self.token = token
        self.token_expires_at = token_expires_at
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

    def account_fingerprint(self, *, force: bool = False) -> str:
        if force:
            self._session_token(force=True)
        self.context()
        if not self.user_id:
            raise FuxamError("The authenticated session has no user ID.")
        return account_fingerprint(self.user_id, self.organization_id)

    def enrolled(self, search: str = "") -> Any:
        return self._json("/api/widgets/get-courses", {"search": search})

    def term_courses(self, *, expected_account: str | None = None) -> dict[str, Any]:
        dashboard_url = self.context()["dashboardUrl"]
        suffix = "/my-courses"
        if not dashboard_url.endswith(suffix):
            raise FuxamError("Could not derive Fuxam's current-term page safely.")
        term_url = dashboard_url[: -len(suffix)] + "/my-term"
        body = self._open(
            term_url,
            headers={"accept": "text/html"},
            require_context=False,
            expected_account=expected_account,
        )
        return parse_term_page(body)

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

    def _action_sources(
        self, page_url: str, *, expected_account: str | None = None
    ) -> dict[str, set[str]]:
        try:
            html = self._open(
                page_url,
                headers={"accept": "text/html"},
                require_context=False,
                expected_account=expected_account,
            ).decode()
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
                script = self._open(
                    source,
                    require_context=False,
                    expected_account=expected_account,
                ).decode()
            except UnicodeDecodeError as exc:
                raise FuxamError("Fuxam returned malformed script data.") from exc
            for action_id, name in pattern.findall(script):
                actions.setdefault(name, set()).add(action_id)
        return actions

    def current_build_id(self, *, expected_account: str | None = None) -> str:
        build = as_object(
            self._json(
                "/api/build-id",
                require_context=False,
                expected_account=expected_account,
            ),
            "build",
        )
        current_build = build.get("buildId")
        if not isinstance(current_build, str) or not current_build:
            raise FuxamError("Fuxam returned no build ID.")
        if current_build != self.build_id:
            self.build_id = current_build
            self.actions.clear()
        return current_build

    def _resolve_action(
        self,
        name: str,
        page_url: str,
        *,
        expected_build: str | None = None,
        expected_account: str | None = None,
    ) -> str:
        current_build = self.current_build_id(expected_account=expected_account)
        if expected_build is not None and not hmac.compare_digest(
            current_build, expected_build
        ):
            raise MutationPreconditionChanged(
                "BUILD_CHANGED: Fuxam changed after the preview."
            )
        cache_key = (page_url, name)
        if cache_key not in self.actions:
            action_sources = self._action_sources(
                page_url, expected_account=expected_account
            )
            allowed_actions = READ_ONLY_ACTIONS | MUTATION_ACTIONS
            for action_name, candidates in action_sources.items():
                if action_name in allowed_actions and len(candidates) == 1:
                    self.actions[(page_url, action_name)] = next(iter(candidates))
            if cache_key not in self.actions:
                raise FuxamError(
                    f"Could not uniquely resolve Fuxam action {name}; the frontend may have changed."
                )
        return self.actions[cache_key]

    def _invoke_action(
        self,
        action_id: str,
        args: list[Any],
        page_url: str,
        *,
        retry: bool,
        expected_account: str | None = None,
    ) -> Any:
        encoded = json.dumps(args, separators=(",", ":")).encode()
        if len(encoded) > 64 * 1024:
            raise FuxamError("Refused an unexpectedly large Fuxam action request.")
        body = self._open(
            page_url,
            method="POST",
            headers={
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": action_id,
            },
            data=encoded,
            retry=retry,
            require_context=False,
            expected_account=expected_account,
        )
        return parse_flight(body)

    def _action(
        self,
        name: str,
        args: list[Any],
        page_url: str | None = None,
        *,
        expected_account: str | None = None,
    ) -> Any:
        if name not in READ_ONLY_ACTIONS:
            raise FuxamError("Refused a non-read-only Fuxam action.")
        target = page_url or self.context()["dashboardUrl"]
        action_id = self._resolve_action(
            name, target, expected_account=expected_account
        )
        return self._invoke_action(
            action_id,
            args,
            target,
            retry=True,
            expected_account=expected_account,
        )

    def _mutation_action(
        self,
        name: str,
        args: list[Any],
        *,
        expected_account: str,
        expected_build: str,
        final_precondition: Callable[[], None] | None = None,
    ) -> Any:
        if name not in MUTATION_ACTIONS:
            raise FuxamError("Refused an unsupported mutation action.")
        self._session_token(force=True)
        self._require_account(expected_account)
        target = self.context()["dashboardUrl"]
        action_id = self._resolve_action(
            name,
            target,
            expected_build=expected_build,
            expected_account=expected_account,
        )
        if not hmac.compare_digest(
            self.current_build_id(expected_account=expected_account), expected_build
        ):
            raise MutationPreconditionChanged(
                "BUILD_CHANGED: Fuxam changed after the preview."
            )
        self._require_account(expected_account)
        if final_precondition is not None:
            final_precondition()
            self._require_account(expected_account)
        try:
            return self._invoke_action(
                action_id,
                args,
                target,
                retry=False,
                expected_account=expected_account,
            )
        except MutationPreconditionChanged:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            # Response/decoder failures do not prove the write was rejected.
            raise MutationOutcomeUnknown() from exc

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

    def booking_conflicts(
        self,
        course_id: str,
        enrolled_ids: list[str],
        *,
        expected_account: str | None = None,
    ) -> Any:
        course_ids = list(dict.fromkeys([course_id, *enrolled_ids]))
        if len(course_ids) < 2:
            return []
        return self._action(
            "checkCourseConflictsAction",
            [
                {
                    "courseIds": course_ids,
                    "includeAppointmentsForCourseIds": [course_id],
                }
            ],
            expected_account=expected_account,
        )

    def mutate_booking(
        self,
        operation: str,
        course_id: str,
        *,
        expected_account: str,
        expected_build: str,
        final_precondition: Callable[[], None] | None = None,
    ) -> Any:
        action = BOOKING_ACTIONS.get(operation)
        if action is None:
            raise FuxamError("Refused an unsupported booking operation.")
        action_name, _ = action
        args = [[course_id]] if operation == "enroll" else [course_id]
        return self._mutation_action(
            action_name,
            args,
            expected_account=expected_account,
            expected_build=expected_build,
            final_precondition=final_precondition,
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
            except MutationPreconditionChanged:
                raise
            except FuxamError:
                sections[section] = None
                failures.append(section)
        try:
            sections["attempts"] = self.module_attempts(module_version_id)
        except MutationPreconditionChanged:
            raise
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
            study_plan = validate_study_plan(self.study_plan(None, None))
        except MutationPreconditionChanged:
            raise
        except FuxamError:
            warnings.append("studyPlan unavailable")
        first = validate_bookable_page(self.bookable(query, 1, 100))
        page_count = first["pageCount"]
        if page_count > MAX_EXPLORE_PAGES:
            raise FuxamError("Fuxam returned an invalid catalog page count.")
        pages = [first] if page_count else []
        for page in range(2, page_count + 1):
            current = validate_bookable_page(self.bookable(query, page, 100))
            if (current["pageCount"], current["totalCount"]) != (
                page_count,
                first["totalCount"],
            ):
                raise FuxamError(
                    "Fuxam catalog pagination changed during the read; run a new query."
                )
            pages.append(current)
        courses: dict[str, Any] = {}
        for page in pages:
            for course in page["courses"]:
                courses[course["id"]] = course
        if first["totalCount"] is not None and len(courses) != first["totalCount"]:
            raise FuxamError(
                "Fuxam returned an inconsistent catalog total count; run a new query."
            )
        return {
            "studyPlan": study_plan,
            "learningUnits": list(courses.values()),
            "query": query,
            "partial": bool(warnings),
            "warnings": warnings,
        }


def account_fingerprint(user_id: str, organization_id: str = "") -> str:
    if not isinstance(user_id, str) or not user_id:
        raise FuxamError("The authenticated session has no user ID.")
    if not isinstance(organization_id, str):
        raise FuxamError("The authenticated session has an invalid organization ID.")
    encoded = (
        b"fuxam-local-account-v2\0"
        + user_id.encode("utf-8")
        + b"\0"
        + organization_id.encode("utf-8")
    )
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_course_id(value: str) -> str:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise FuxamError(
            "INVALID_COURSE_ID: use the exact ID from learning-units."
        ) from exc
    if (
        not encoded
        or len(encoded) > MAX_COURSE_ID_BYTES
        or re.fullmatch(rb"[A-Za-z0-9_-]+", encoded) is None
    ):
        raise FuxamError("INVALID_COURSE_ID: use the exact ID from learning-units.")
    return value


def _booking_target(summary: dict[str, Any], course_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in summary.get("learningUnits", [])
        if isinstance(item, dict) and item.get("courseId") == course_id
    ]
    if len(matches) != 1:
        raise FuxamError("TARGET_NOT_FOUND: no unique active-term course has that ID.")
    return matches[0]


def _booking_postcondition(operation: str, state: str) -> bool:
    if operation == "enroll":
        return state == "ENROLLED"
    if operation == "unenroll":
        return state in {"BOOKABLE", "FULL"}
    if operation == "join-waitlist":
        return state == "WAITLISTED"
    if operation == "leave-waitlist":
        return state in {"BOOKABLE", "FULL"}
    return False


def _booking_eligibility(
    operation: str, target: dict[str, Any], summary: dict[str, Any]
) -> None:
    state = target["state"]
    operation_eligible = {
        "enroll": state == "BOOKABLE",
        "unenroll": state == "ENROLLED" and target["canUnenroll"],
        "join-waitlist": state == "FULL" and summary["waitlistEnabled"],
        "leave-waitlist": state == "WAITLISTED",
    }[operation]
    eligible = summary["canBookCourses"] and operation_eligible
    if not eligible:
        raise FuxamError(
            f"NOT_ELIGIBLE: {operation} is unavailable from state {state}."
        )


def _booking_fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _booking_conflict_snapshot(value: Any, target_course_id: str) -> dict[str, Any]:
    if not isinstance(value, list):
        raise FuxamError("Fuxam returned unsupported conflict-check data.")
    relevant: list[dict[str, Any]] = []
    for conflict in value:
        if not isinstance(conflict, dict):
            raise FuxamError("Fuxam returned unsupported conflict-check data.")
        course_a = conflict.get("courseA")
        course_b = conflict.get("courseB")
        if not isinstance(course_a, dict) or not isinstance(course_b, dict):
            raise FuxamError("Fuxam returned unsupported conflict-check data.")
        course_a_id = course_a.get("id")
        course_b_id = course_b.get("id")
        if (
            not isinstance(course_a_id, str)
            or not course_a_id
            or not isinstance(course_b_id, str)
            or not course_b_id
        ):
            raise FuxamError("Fuxam returned unsupported conflict-check data.")
        if target_course_id in {course_a_id, course_b_id}:
            relevant.append(conflict)
    return {
        "checked": True,
        "hasConflicts": bool(relevant),
        "count": len(relevant),
    }


def _waitlist_conflict_warning(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    warning = value.get("hasConflicts")
    return warning if type(warning) is bool else None


def _booking_snapshot(
    client: FuxamClient,
    operation: str,
    course_id: str,
    expected_account: str,
) -> dict[str, Any]:
    summary = summarize_term_courses(
        client.term_courses(expected_account=expected_account)
    )
    target = _booking_target(summary, course_id)
    desired_state = BOOKING_ACTIONS[operation][1]
    if _booking_postcondition(operation, target["state"]):
        return {
            "alreadyApplied": True,
            "summary": summary,
            "target": target,
            "desiredState": desired_state,
        }
    _booking_eligibility(operation, target, summary)

    conflict_check: dict[str, Any] = {"checked": False}
    if operation == "enroll":
        enrolled_ids = [
            item["courseId"]
            for item in summary["learningUnits"]
            if item["state"] == "ENROLLED"
        ]
        if enrolled_ids:
            conflict_check = _booking_conflict_snapshot(
                client.booking_conflicts(
                    course_id,
                    enrolled_ids,
                    expected_account=expected_account,
                ),
                course_id,
            )
        if conflict_check.get("hasConflicts"):
            raise FuxamError(
                "SCHEDULE_CONFLICTS: Fuxam found a schedule conflict; inspect and "
                "confirm it in the official UI."
            )
    build_id = client.current_build_id(expected_account=expected_account)
    basis = {
        "schema": 1,
        "operation": operation,
        "courseId": course_id,
        "courseName": target["name"],
        "termId": summary["termId"],
        "term": summary["term"],
        "buildId": build_id,
        "accountFingerprint": expected_account,
        "observedState": target["state"],
        "desiredState": desired_state,
        "booking": {
            "capacity": target["capacity"],
            "enrolledCount": target["enrolledCount"],
            "isFull": target["isFull"],
            "canUnenroll": target["canUnenroll"],
            "waitlistPosition": target["waitlistPosition"],
            "canBookCourses": summary["canBookCourses"],
            "waitlistEnabled": summary["waitlistEnabled"],
        },
        "conflictCheck": conflict_check,
    }
    fingerprint = _booking_fingerprint(basis)
    return {
        "alreadyApplied": False,
        "summary": summary,
        "target": target,
        "desiredState": desired_state,
        "buildId": build_id,
        "fingerprint": fingerprint,
        "preview": {
            "ok": True,
            "mode": "preview",
            "operation": operation,
            "target": {
                "courseId": course_id,
                "name": target["name"],
                "termId": summary["termId"],
                "term": summary["term"],
                "termName": summary["termName"],
            },
            "observedState": target["state"],
            "desiredState": desired_state,
            "booking": basis["booking"],
            "conflictCheck": conflict_check,
            "confirmationRequired": True,
            "confirmationFingerprint": fingerprint,
            "changed": False,
        },
    }


def booking_workflow(
    client: FuxamClient,
    operation: str,
    course_id: str,
    *,
    apply: bool,
    confirmation: str | None,
) -> dict[str, Any]:
    """Preview or safely apply one exact active-term booking transition."""
    if operation not in BOOKING_ACTIONS:
        raise FuxamError("Refused an unsupported booking operation.")
    course_id = _validate_course_id(course_id)
    expected_account = client.account_fingerprint(force=True)
    snapshot = _booking_snapshot(client, operation, course_id, expected_account)
    summary = snapshot["summary"]
    target = snapshot["target"]
    desired_state = snapshot["desiredState"]
    if snapshot["alreadyApplied"]:
        return {
            "ok": True,
            "mode": "apply" if apply else "preview",
            "operation": operation,
            "target": {
                "courseId": course_id,
                "name": target["name"],
                "termId": summary["termId"],
                "term": summary["term"],
                "termName": summary["termName"],
            },
            "observedState": target["state"],
            "desiredState": desired_state,
            "confirmationRequired": False,
            "changed": False,
            "result": "already-applied",
        }
    preview = snapshot["preview"]
    fingerprint = snapshot["fingerprint"]
    build_id = snapshot["buildId"]
    if not apply:
        return preview
    if not confirmation:
        raise FuxamError(
            "CONFIRMATION_REQUIRED: preview first, then pass --apply --confirm "
            "with its exact fingerprint."
        )
    if not confirmation.isascii() or not hmac.compare_digest(confirmation, fingerprint):
        raise FuxamError(
            "STALE_PREVIEW: the confirmation does not match fresh Fuxam state."
        )

    def final_precondition() -> None:
        fresh = _booking_snapshot(client, operation, course_id, expected_account)
        if fresh["alreadyApplied"] or not hmac.compare_digest(
            confirmation, fresh["fingerprint"]
        ):
            raise MutationPreconditionChanged(
                "STALE_PREVIEW: Fuxam state changed before the mutation request."
            )

    ambiguous = False
    mutation_result: Any = None
    try:
        mutation_result = client.mutate_booking(
            operation,
            course_id,
            expected_account=expected_account,
            expected_build=build_id,
            final_precondition=final_precondition,
        )
    except MutationOutcomeUnknown:
        ambiguous = True
    try:
        verified_summary = summarize_term_courses(
            client.term_courses(expected_account=expected_account)
        )
        if verified_summary["termId"] != summary["termId"]:
            raise FuxamError("The active Fuxam term changed after the mutation.")
        verified_target = _booking_target(verified_summary, course_id)
    except (Exception, KeyboardInterrupt) as exc:
        raise FuxamError(
            "OUTCOME_UNKNOWN: Fuxam state could not be verified; inspect the official "
            "UI before trying again."
        ) from exc
    if not _booking_postcondition(operation, verified_target["state"]):
        code = "OUTCOME_UNKNOWN" if ambiguous else "POSTCONDITION_FAILED"
        raise FuxamError(
            f"{code}: the expected state was not observed; inspect the official UI "
            "before trying again."
        )
    result = {
        **preview,
        "mode": "apply",
        "confirmationRequired": False,
        "changed": True,
        "result": "reconciled-success" if ambiguous else "verified-success",
        "verifiedState": verified_target["state"],
    }
    if operation == "join-waitlist":
        conflict_warning = _waitlist_conflict_warning(mutation_result)
        result["scheduleConflictWarning"] = conflict_warning
        result["requiresUiInspection"] = conflict_warning is not False
        if conflict_warning is True:
            result["warning"] = (
                "SCHEDULE_CONFLICT_WARNING: inspect the official Fuxam UI for "
                "the conflict details."
            )
        elif conflict_warning is None:
            result["warning"] = (
                "WAITLIST_CONFLICT_STATUS_UNKNOWN: Fuxam's warning response could "
                "not be verified; inspect the official UI."
            )
    return result


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


def bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
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
        help="also check the active-term page and bookable catalog action",
    )
    auth = commands.add_parser("auth", help="Manage the local Keychain credential")
    auth.add_argument("operation", choices=("set", "status", "clear"))
    commands.add_parser("context", help="Verify the CODE study context")
    enrolled = commands.add_parser(
        "enrolled", help="List raw records or confirmed active-term enrollments"
    )
    enrolled.add_argument(
        "--search", default="", help="search older raw records (without --term)"
    )
    enrolled.add_argument(
        "--term",
        help="show confirmed enrollments when this is the active Fuxam term",
    )
    add_output_format_argument(enrolled)
    learning_units = commands.add_parser(
        "learning-units",
        help="Show active-term enrolled, waitlisted, self-study, and bookable units",
    )
    learning_units.add_argument(
        "--term", help="require this semester to be Fuxam's active term"
    )
    add_output_format_argument(learning_units)
    booking = commands.add_parser(
        "booking", help="Preview or apply one active-term learning-unit change"
    )
    booking_operations = booking.add_subparsers(dest="operation", required=True)
    for operation in BOOKING_ACTIONS:
        booking_operation = booking_operations.add_parser(operation)
        booking_operation.add_argument(
            "course_id", help="exact course ID from learning-units"
        )
        booking_operation.add_argument(
            "--apply",
            action="store_true",
            help="submit the change after a matching preview",
        )
        booking_operation.add_argument(
            "--confirm",
            dest="confirmation",
            help="exact sha256 fingerprint returned by the preview",
        )
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
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = normalize_client_cookie(
                    getpass.getpass("Fuxam __client value (hidden): ")
                )
        except getpass.GetPassWarning as exc:
            raise FuxamError(
                "Credential entry requires an interactive terminal with hidden "
                "input. Run `fuxam.py auth set` directly in your terminal."
            ) from exc
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
        if args.term is not None:
            if args.search:
                raise FuxamError("--search and --term cannot be combined.")
            return summarize_term_enrolled(client.term_courses(), args.term)
        enrolled = client.enrolled(args.search)
        return (
            summarize_enrolled(enrolled) if args.output_format == "table" else enrolled
        )
    if args.command == "learning-units":
        return summarize_term_courses(client.term_courses(), args.term)
    if args.command == "booking":
        return booking_workflow(
            client,
            args.operation,
            args.course_id,
            apply=args.apply,
            confirmation=args.confirmation,
        )
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
