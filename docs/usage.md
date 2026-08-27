# Usage

Set the installed CLI path once per terminal:

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"
```

`doctor` checks local readiness without contacting Fuxam. `smoke-test` exercises
read-only paths and returns only check metadata; `smoke-test --deep` also checks
the active-term page and one catalog action. A passing check reflects that
account and moment, not a permanent compatibility guarantee.

```sh
python3 "$FUXAM" doctor
python3 "$FUXAM" smoke-test
python3 "$FUXAM" smoke-test --deep
```

## Reading records

Use `learning-units` for Fuxam's active-term booking view. It separates
enrolled, waitlisted, self-study, bookable, and full learning units.
`enrolled --term TERM` returns confirmed active-term enrollments and keeps
waitlisted entries separate. This client cannot verify historical booking state.
Commands return JSON by default; these summaries also support `--format table`.

```sh
python3 "$FUXAM" learning-units --format table
python3 "$FUXAM" enrolled --term "Fall 2026" --format table
python3 "$FUXAM" modules --term "Fall 2026" --format table
python3 "$FUXAM" module-attempts MODULE_VERSION_ID
```

Keep the evidence distinct:

- `isElected=true` with an unambiguous term is a formal module election.
- An active-term `ENROLLED` item is current enrollment; a waitlist item is not.
- An older `ACTIVE` record can remain after completion.
- An offered-in-term tag is catalog availability, not attendance or enrollment.
- A published `PASSED` attempt is progress evidence. Its absence does not prove
  that work is incomplete.

A learning-unit association or a code in its title is not a formal election.
Incomplete results or schema errors are not evidence of zero records.

## Booking

Bookings affect the active term only. Pick the exact ID from `learning-units`,
then preview before applying. Never choose by title or fuzzy match.

```sh
python3 "$FUXAM" booking enroll COURSE_ID
python3 "$FUXAM" booking enroll COURSE_ID --apply --confirm 'sha256:...'
```

| Operation | Required state | Verified final state |
| --- | --- | --- |
| `enroll` | `BOOKABLE` | `ENROLLED` |
| `unenroll` | `ENROLLED`, with unbooking allowed | `BOOKABLE` or `FULL` |
| `join-waitlist` | `FULL`, with waitlists enabled | `WAITLISTED` |
| `leave-waitlist` | `WAITLISTED` | `BOOKABLE` or `FULL` |

All operations also need Fuxam's booking window to be open. The preview includes
the course, term, state, booking details, conflict result, and confirmation
fingerprint. An agent must show it and obtain explicit approval of that exact
fingerprint before the `--apply` command.

Before dispatch the CLI checks the account, current state, booking policy,
frontend build, and relevant enrollment conflicts. It sends no more than one
mutation request and then reads the active-term state back. An enrollment conflict
stops with `SCHEDULE_CONFLICTS`; resolve it in Fuxam's official UI.

- `NOT_ELIGIBLE`: Fuxam does not allow the transition from the current state.
- `STALE_PREVIEW`: obtain a new preview and approval.
- `ACCOUNT_CHANGED`: confirm the account, then start a new command. Do not mix results.
- `OUTCOME_UNKNOWN` or `POSTCONDITION_FAILED`: the request may have reached Fuxam.
  Inspect its UI; do not retry blindly.

A verified `join-waitlist` result still needs UI inspection
unless `scheduleConflictWarning` is explicitly `false`; `true` or `null` means
Fuxam reported or could not verify its warning. Both set `requiresUiInspection`
to `true`; an unknown warning also reports `WAITLIST_CONFLICT_STATUS_UNKNOWN`.

Module-election and self-study changes remain in Fuxam's official UI. More
security and credential details are in [SECURITY.md](../SECURITY.md).
