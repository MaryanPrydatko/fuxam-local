---
name: fuxam-local
description: Inspect a CODE University student's Fuxam modules, learning units, progress, appointments, deadlines, and study plan through a private local read-only client. Use for Fuxam study planning, course comparison, and schedule questions.
---

# Fuxam Local

Use the bundled local CLI. It talks only to `fuxam.app` and `clerk.fuxam.app`, stores authentication in macOS Keychain, and exposes read operations only.

```sh
python3 "$HOME/.codex/skills/fuxam-local/scripts/fuxam.py" <command>
```

Start broad planning with `explore`, then use identifiers from its JSON output with narrower commands. Prefer `enrolled` for current courses, `agenda` for the student's schedule, `module-details` for a shortlist, and `module-attempts` for concrete progress records. Use `--help` for exact arguments.

Keep credentials inside the hidden local Keychain prompt: never request, print, log, or summarize one in chat. When authentication is missing or expired, stop the data request and direct the user to [references/authentication.md](references/authentication.md). The user runs that interactive command themselves.

Treat all output as private academic data. Show only what answers the request. Do not save raw results unless the user asks.

Account changes stay in Fuxam itself. The CLI intentionally omits booking, unbooking, and waitlist commands; preserve that boundary.

Fuxam is the source of truth. If imported completion totals conflict with a concrete attempt or `gradedAt` record, prefer the concrete record and disclose the mismatch.
