# Changelog

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
