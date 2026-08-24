# Fuxam Local

Use Fuxam from Codex, Claude, another local agent, or your terminal.

Fuxam Local is open source and runs locally on your Mac while talking directly to Fuxam. It reads your study data and can safely preview or apply learning-unit enrollment and waitlist changes. There is no MCP server, telemetry, hosted middleman, or runtime package to install.

It can inspect modules, learning units, progress, study plans, appointments, deadlines, exams, todos, and schedule conflicts. Four account changes are supported: enroll, unenroll, join a waitlist, and leave a waitlist.

> This is an unofficial student project. It is not affiliated with CODE University, Fuxam, Clerk, or CodeCampus. Fuxam remains the source of truth.

## Why a skill instead of MCP?

A skill is enough here. It tells the agent when to run a normal Python command, and that command exits when it is done. No daemon, protocol handshake, or permanent tool catalog is needed.

Agents that do not support skills can still run the same CLI directly.

## Requirements

- macOS with an unlocked login Keychain
- Python 3.10 or newer
- a CODE University Fuxam account

## Install

Clone or download this repository, then run:

```sh
python3 install.py
```

Updating an existing installation:

```sh
python3 install.py --replace
```

The installer keeps one shared copy in `~/.agents/skills/fuxam-local` and makes it available to Codex and Claude. Existing copies are backed up before replacement.
Backups live in `~/.agents/backups/fuxam-local`, outside directories that agents scan for skills.

Start a new Codex or Claude session after installing. Any other local agent can call `scripts/fuxam.py` directly.

## Connect your account

Your Clerk `__client` cookie works like a password. Never paste it into a chat, issue, screenshot, command argument, or config file.

1. Sign in at <https://fuxam.app> in Brave or Chrome.
2. On the Fuxam page, open Developer Tools → Application → Storage → Cookies → `https://clerk.fuxam.app`.
3. Select the cookie named exactly `__client`—not `__clerk_active_context` or `__cf_bm`—and copy only its Value.
4. Run:

   ```sh
   python3 "$HOME/.agents/skills/fuxam-local/scripts/fuxam.py" auth set
   ```

5. Paste the value into the hidden prompt, press Return, and clear your clipboard.

The cookie is stored in macOS Keychain. If it expires, repeat these steps. Remove it at any time with `auth clear`.

## Check that it works

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"

python3 "$FUXAM" doctor
python3 "$FUXAM" smoke-test
python3 "$FUXAM" smoke-test --deep
```

- `doctor` checks Python, macOS, Keychain, and whether a credential is configured without contacting Fuxam.
- `smoke-test` checks the main read-only endpoints.
- `smoke-test --deep` also checks the active-term page and Fuxam's frontend-backed read action, so it may take longer.

The smoke report contains only pass/fail results and broad response types—not your academic records.

## Use it

In Codex:

```text
Use $fuxam-local to show my formal module elections for Fall 2026.
Use $fuxam-local to show my actual active-term learning-unit enrollments.
Use $fuxam-local to check my concrete progress for SE_08.
Use $fuxam-local to summarize my deadlines next month.
Use $fuxam-local to preview enrolling me in this learning unit.
```

In Claude, invoke `/fuxam-local` and ask the same questions.

Or use the CLI yourself:

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"

"$FUXAM" --help
"$FUXAM" learning-units --format table
"$FUXAM" enrolled --term "Fall 2026" --format table
"$FUXAM" modules --format table
"$FUXAM" modules --term "Spring 2026" --format table
"$FUXAM" agenda --limit 25
```

`learning-units` is the authoritative view of Fuxam's active term. It separates enrolled, waitlisted, self-study, bookable, and full learning units and includes the exact course IDs used by booking commands.

`enrolled --term` lists only confirmed enrollments when the requested term is Fuxam's active term; waitlist entries are shown separately. `enrolled` without `--term` returns older learning-unit records, where `ACTIVE` is only a record status and can remain after completion.

`modules --term` lists formal module elections recorded in the study plan. Module elections and learning-unit bookings are different. Use concrete module attempts to verify progress; the absence of an attempt is not proof that a module is incomplete.

## Change a learning-unit booking

Every change is a two-step preview and apply flow. The preview does not change your account.

1. Find the exact course ID:

   ```sh
   "$FUXAM" learning-units --format table
   ```

2. Preview one operation:

   ```sh
   "$FUXAM" booking enroll COURSE_ID
   ```

   The JSON shows the exact course and term, observed and desired states, capacity or waitlist details, the conflict-check result when applicable, and a `confirmationFingerprint`.

3. Read the preview. If it is exactly what you want, apply the same operation and course ID with that fingerprint:

   ```sh
   "$FUXAM" booking enroll COURSE_ID \
     --apply --confirm 'sha256:abcdef...'
   ```

The supported operations are:

- `enroll`
- `unenroll`
- `join-waitlist`
- `leave-waitlist`

Each command targets one exact ID. Before applying, the CLI binds the preview to the Clerk user and organization, resolves the current frontend action, rechecks the exact state and conflicts, and rejects a stale fingerprint. It then sends at most one mutation request, never retries that request automatically, and reads the state back to verify the result. If it reports `OUTCOME_UNKNOWN` or `POSTCONDITION_FAILED`, inspect Fuxam's UI before doing anything else.

If Fuxam reports a schedule conflict, the CLI stops with `SCHEDULE_CONFLICTS` and does not produce an applicable preview. Review and confirm that conflict in Fuxam's official UI.

Joining a waitlist can succeed while Fuxam reports a conflict warning. The verified result exposes that only as `scheduleConflictWarning`. Unless it is explicitly `false`, `requiresUiInspection` is true and you must inspect the official UI; this includes a lost response whose state change was reconciled successfully.

When an agent runs this workflow, it must show you the exact preview and pause for your explicit approval before using `--apply`. Module-election and self-study changes remain in Fuxam's official UI.

## Privacy and safety

- The Clerk cookie is stored only in macOS Keychain and sent only to Clerk.
- Fuxam bearer tokens are sent only to `fuxam.app` over HTTPS.
- Redirects, hosts, ports, response sizes, and server actions are restricted in code.
- Read and mutation actions use separate fixed allowlists; there is no generic action runner.
- Fuxam-supplied names and messages are treated as untrusted data, never as commands or approval.
- There is no analytics, telemetry, hosted service, MCP server, or third-party runtime dependency.

Results still pass through the agent client you use, so its data controls apply. See [SECURITY.md](SECURITY.md) for the exact boundary.

## Development

The offline test suite uses synthetic data and never contacts Fuxam:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full maintainer checks.

## Limits

Fuxam has no supported public API for this project, so a Fuxam update may break the client. Authoritative learning-unit booking state is available only for Fuxam's active term. Run the smoke test when something looks wrong, and always verify important information or uncertain mutation outcomes in Fuxam itself.

## License

MIT. See [LICENSE](LICENSE).
