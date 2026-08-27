# Changelog

## 0.4.1 — 2026-08-27

- Refuse credential entry when a terminal cannot hide input, and stop commands if the signed-in user or organization changes.
- Reconcile unexpected booking-response and verification failures without retrying a mutation.
- Decode nested React Flight model references and literal text correctly, with bounded depth and expansion.
- Reject malformed study-plan and catalog data instead of reporting false empty or complete results; use those same validators in smoke tests.
- Support shared, symlinked skill directories while preserving backups and rollback.
- Add a working private vulnerability-reporting channel.

## 0.4.0 — 2026-08-25

- Added authoritative active-term learning-unit enrollment, waitlist, self-study, and availability data from Fuxam's My Learning Units page.
- Changed `enrolled --term` to report confirmed current-term enrollments instead of offering-tag overlaps.
- Added guarded preview-and-confirm commands for enrolling, unenrolling, joining a waitlist, and leaving a waitlist.
- Bound each change to one exact course, Clerk context, and a final fresh-state check; disabled mutation retries and added read-after-write verification.

## 0.3.2 — 2026-08-24

- Moved installer backups outside skill-discovery directories so old copies cannot appear as duplicate skills.

## 0.3.1 — 2026-08-24

- Stopped presenting `ACTIVE` learning-unit records with a matching offering tag as term enrollment or current workload.
- Marked term enrollment, workload, and completion as unknown in offering-overlap results.
- Tightened the skill's evidence rules for formal elections, learning-unit records, and concrete progress.

## 0.3.0 — 2026-08-24

- Added term-filtered learning-unit summaries with compact terminal tables.
- Added a `modules` command for formal study-plan elections.
- Labeled explicit module associations separately from title-only code mentions.
- Surfaced ambiguous or incomplete records instead of presenting them as definitive empty results.
- Sanitized terminal tables against control-character injection.
- Treated an empty catalog search as a valid result instead of a pagination error.

## 0.2.1 — 2026-08-24

- Fixed Clerk token renewal for accounts without an active organization.
- Added a regression test for the null-organization session shape.

## 0.2.0 — 2026-08-24

- Made the Agent Skill host-neutral and aligned it with the open Agent Skills layout.
- Added a safe installer for shared `.agents`, Claude, and Codex skill locations.
- Added offline readiness diagnostics and privacy-preserving live smoke tests.
- Kept the universal interface as a dependency-free CLI with no MCP server or daemon.

## 0.1.0 — 2026-08-24

- Added the private local Codex skill and dependency-free Python CLI.
- Added read-only catalog, study-plan, progress, schedule, deadline, and conflict access.
- Added macOS Keychain credential storage and strict request-origin controls.
- Added synthetic unit tests, Ruff checks, and GitHub Actions CI.
- Published under the MIT License with no telemetry or hosted component.
