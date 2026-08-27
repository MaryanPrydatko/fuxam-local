# Changelog

## 0.5.0 — 2026-08-27

- Add explicit temporary login through `--auth env` and `FUXAM_COOKIE`.
- Explain missing Linux keyring dependencies during installation.
- Add `--version` and document manual updates.
- Add Linux Secret Service and Windows Credential Manager storage.
- Install Windows agent copies without symlink privileges.
- Write UTF-8 output on Windows and add cross-platform checks.

## 0.4.2 — 2026-08-27

- Separate response decoding from the CLI.
- Test installed commands through shared and aliased skill paths.
- Simplify Flight channel handling and test ignored and malformed pushes.
- Shorten help and documentation; credit the project that inspired this one.
- Remove unused testing graphics.

## 0.4.1 — 2026-08-27

- Require hidden terminal input for credentials.
- Stop commands if the signed-in user or organization changes.
- Check uncertain booking results without retrying writes.
- Decode nested React Flight references and literal text, with size and depth limits.
- Reject malformed study-plan and catalog responses, including in smoke tests.
- Fix installation through shared, symlinked skill directories.
- Enable private vulnerability reporting.

## 0.4.0 — 2026-08-25

- Read active-term enrollments, waitlists, self-study, and availability from My Learning Units.
- Make `enrolled --term` use confirmed enrollments, not offering-tag overlaps.
- Add preview-and-confirm commands for enrollment and waitlist changes.
- Require an exact course, account, organization, and fresh state for each write.
- Verify writes by reading back the result; never retry a mutation.

## 0.3.2 — 2026-08-24

- Store installer backups outside skill-discovery directories.
- Migrate older backups during replacement, with rollback on failure.

## 0.3.1 — 2026-08-24

- Stop treating `ACTIVE` records with matching offering tags as term enrollment.
- Mark unchecked enrollment, workload, and completion as unknown.
- Use study-plan elections for module questions and attempts for progress.

## 0.3.0 — 2026-08-24

- Add offering-term filters and terminal tables for learning-unit records.
- Add `modules` for formal study-plan elections.
- Accept semester names such as `Fall 2026` as well as term codes.
- Distinguish explicit module associations from codes mentioned in titles.
- Report ambiguous or incomplete records.
- Strip terminal control characters from tables.
- Accept empty catalog searches without a pagination error.

## 0.2.1 — 2026-08-24

- Fix token renewal for accounts without an active organization.

## 0.2.0 — 2026-08-24

- Adopt the Agent Skills layout for use across local agents.
- Add a shared installer with Codex and Claude aliases.
- Add offline diagnostics and read-only live smoke tests.

## 0.1.0 — 2026-08-24

- Add a read-only CLI for courses, study plans, progress, and schedules.
- Store credentials in macOS Keychain and restrict request origins.
- Add offline tests, lint checks, and CI.
- Release under the MIT license.
