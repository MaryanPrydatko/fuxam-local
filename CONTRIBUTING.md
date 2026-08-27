# Contributing

Keep patches focused and include tests for changed behavior. Use the standard
library unless a dependency has a clear security or maintenance benefit.

Tests and CI use synthetic fixtures only: no credentials, student records, or
requests to Fuxam. Describe protocol changes without attaching private responses.

CI runs the offline suite on macOS, Linux, and Windows. Separate Linux/Windows
jobs exercise native stores using disposable, synthetic entries, never a Fuxam
credential. `tests/native_credentials.py` is restricted to CI runners.

Keep writes inside the four booking operations. Preserve exact-ID previews,
explicit confirmation, fresh-state checks, one non-retried request, and
read-after-write verification. Uncertain outcomes require UI inspection.
Read [SECURITY.md](SECURITY.md) before changing authentication or requests.

Run from the repository root:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
uvx ruff==0.12.11 check .
uvx ruff==0.12.11 format --check .
uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
```

On Windows, use `py -3` instead of `python3`.
