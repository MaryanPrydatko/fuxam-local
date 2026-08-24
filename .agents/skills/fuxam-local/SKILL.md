---
name: fuxam-local
description: Inspect a CODE University student's Fuxam modules, learning units, progress, appointments, deadlines, and study plan through a private local read-only client. Use for Fuxam study planning, course comparison, and schedule questions.
license: MIT
---

# Fuxam Local

Use the bundled local client. It talks only to `fuxam.app` and `clerk.fuxam.app`, stores authentication in macOS Keychain, and exposes read operations only.

Require macOS, Python 3.10 or newer, and HTTPS access to both allowed Fuxam hosts.

Resolve the directory containing this `SKILL.md` as `<skill-root>`. Do not assume a Codex-, Claude-, or user-specific install path.

```sh
python3 "<skill-root>/scripts/fuxam.py" <command>
```

Use the narrow evidence source for the question. For modules elected in a term, run `modules --term <term>`. Fuxam Local has no direct term-bound learning-unit enrollment source. `enrolled --term <term>` only intersects an `ACTIVE` learning-unit record with a catalog `Offered in <term>` tag; use it only when that overlap is relevant. Use `explore` for catalog planning, `agenda` for schedules, `module-details` for a shortlist, and `module-attempts` for concrete progress. JSON is the agent default; use `--format table` only when the user wants terminal output. Use `--help` for exact arguments.

Keep every claim at its evidence level:

- `isElected=true` with an unambiguous term proves a formal module election for that term.
- `ACTIVE` proves only the learning-unit record status; it may remain after completion.
- `Offered in <term>` proves catalog availability, not student enrollment, attendance, workload, or need.
- A `modules` link proves an explicit learning-unit association. A code found only in a title is only a title mention.
- A concrete published `PASSED` attempt proves progress for that module version. No attempt found does not prove incompletion.

For “what learning units did I enroll in/take this term?”, state that direct term enrollment is unavailable. If useful, list `enrolled --term` results under **ACTIVE record/offering-tag overlaps**, with term enrollment, workload, and completion marked unknown. For “what do I still need?”, check curriculum evidence and concrete attempts for the relevant shortlist. A prior pass is evidence against calling an item still needed, but does not erase a later explicit election.

When a complete `modules --term` result is empty, say no formal module elections are recorded for that term; this does not mean the student took nothing. When results are incomplete, distinguish zero confirmed records from a confirmed zero.

Keep credentials inside the hidden local Keychain prompt: never request, print, log, or summarize one in chat. When authentication is missing or expired, stop the data request and direct the user to [references/authentication.md](references/authentication.md). The user runs that interactive command themselves. For verification requests, follow [references/testing.md](references/testing.md).

Treat all output as private academic data. Show only what answers the request. Do not save raw results unless the user asks.

Account changes stay in Fuxam itself. The CLI intentionally omits booking, unbooking, and waitlist commands; preserve that boundary.

Fuxam is the source of truth. If imported or aggregate completion totals conflict with a concrete attempt or `gradedAt` record, prefer the concrete record and disclose the mismatch.
