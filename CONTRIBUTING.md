# Contributing

Contributions are welcome when they preserve the project's privacy and guarded-mutation boundary.

Before submitting a change:

1. Keep booking changes to one exact course ID and one of four fixed operations: enroll, unenroll, join a waitlist, or leave a waitlist.
2. Preserve the preview fingerprint, fresh-state check, explicit confirmation, single non-retried mutation, and read-after-write verification. An ambiguous outcome must stop for manual inspection.
3. Keep generic server actions, module-election writes, self-study writes, analytics, telemetry, hosted services, and MCP out of scope.
4. Never record or commit credentials or real student data. Tests and CI use synthetic fixtures and never perform live Fuxam mutations.
5. Keep the Python CLI dependency-free unless a dependency has a compelling security or maintenance benefit.
6. Run:

   ```sh
   python3 -m compileall -q .agents/skills/fuxam-local/scripts
   python3 -m unittest discover -s tests -v
   uvx ruff==0.12.11 check .
   uvx ruff==0.12.11 format --check .
   uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
   ```

When Fuxam changes, explain the observed protocol behavior without attaching private responses. Keep commits focused, and document non-obvious security decisions in the code or pull request.
