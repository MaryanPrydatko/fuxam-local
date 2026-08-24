# Repository constraints

- `.agents/skills/fuxam-local` is the canonical installable skill.
- Preserve the read-only boundary: never add booking, unbooking, or waitlist actions.
- Keep credentials in the hidden macOS Keychain flow; never accept them through arguments, environment variables, config files, logs, or chat.
- Preserve exact host, port, redirect, action, and response-size checks when changing networking code.
- Use synthetic fixtures only. Tests must not contact Fuxam or require a student credential.

Run before claiming completion:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
```
