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

Use the narrow evidence source for the question. For term-specific enrollment, run `enrolled --term <term>` for current learning units and `modules --term <term>` for formal study-plan elections. Use `explore` for catalog planning, `agenda` for schedules, `module-details` for a shortlist, and `module-attempts` for concrete progress. JSON is the agent default; use `--format table` only when the user wants terminal output. Use `--help` for exact arguments.

Separate enrollment, offering, association, and election evidence. An enrolled record proves current learning-unit enrollment; a term tag proves only that the unit is offered in that term; a `modules` link proves an explicit learning-unit association; and a code present only in a title is a title mention. Only an elected study-plan record proves formal module election. For term-specific module questions, lead with formal elections and list term-tagged learning units separately. State unknowns instead of promoting weaker evidence.

Keep credentials inside the hidden local Keychain prompt: never request, print, log, or summarize one in chat. When authentication is missing or expired, stop the data request and direct the user to [references/authentication.md](references/authentication.md). The user runs that interactive command themselves. For verification requests, follow [references/testing.md](references/testing.md).

Treat all output as private academic data. Show only what answers the request. Do not save raw results unless the user asks.

Account changes stay in Fuxam itself. The CLI intentionally omits booking, unbooking, and waitlist commands; preserve that boundary.

Fuxam is the source of truth. If imported completion totals conflict with a concrete attempt or `gradedAt` record, prefer the concrete record and disclose the mismatch.
