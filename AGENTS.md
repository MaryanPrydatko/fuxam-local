# Repository constraints

- `.agents/skills/fuxam-local` is the canonical installable skill.
- Keep saved credentials in the hidden terminal flow and native OS store. Temporary `FUXAM_COOKIE` authentication requires explicit `--auth env`; never fall back between sources or load `.env` files. Never accept credential values through arguments, logs, or chat.
- Preserve exact host, port, redirect, action, and response-size checks when changing networking code.
- Keep account changes inside the guarded `booking` workflow: one exact course ID, a fresh preview, explicit approval of its fingerprint, one mutation request with no automatic retry, and read-after-write verification.
- The mutation allowlist is limited to enrolling, unenrolling, joining a waitlist, and leaving a waitlist. Keep generic server actions, module elections, self-study changes, telemetry, hosted services, and MCP out of scope.
- Use synthetic fixtures only. Tests and CI must not contact Fuxam, require a student credential, or perform a live mutation.

Run before claiming completion:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
uvx ruff==0.12.11 check .
uvx ruff==0.12.11 format --check .
uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
```
