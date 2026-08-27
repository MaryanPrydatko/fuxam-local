# Verification

Use the bundled CLI:

```sh
python3 "<skill-root>/scripts/fuxam.py" doctor
python3 "<skill-root>/scripts/fuxam.py" smoke-test
python3 "<skill-root>/scripts/fuxam.py" smoke-test --deep
```

On Windows, use `py -3` instead of `python3`.

`doctor` checks the runtime and OS credential store without contacting Fuxam. If authentication is unavailable, follow [authentication.md](authentication.md); the user unlocks the store or runs the hidden prompt locally.

Smoke checks are read-only and return pass/fail status and response types, not academic records. The deep check adds active-term parsing and one frontend-backed catalog page. Study-plan and catalog checks share their commands' schema validators.

A pass establishes compatibility for that account at that moment. It does not cover full pagination, every read command, agent consent handling, live writes, or future Fuxam changes.

For a requested live workflow check, `learning-units --format table` is read-only. A booking preview may check one eligible exact course ID; verify `mode: preview` and `changed: false`, then stop. Never pass `--apply` while testing.

Maintainers should also run the offline checks in the repository's `CONTRIBUTING.md`. Tests and CI use synthetic fixtures, never credentials or authenticated requests.
