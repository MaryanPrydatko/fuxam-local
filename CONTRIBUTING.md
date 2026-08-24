# Contributing

Contributions are welcome when they preserve the project's privacy and read-only boundary.

Before submitting a change:

1. Do not add booking, unbooking, waitlist, analytics, or hosted-service behavior.
2. Do not record or commit credentials or real student data; use synthetic fixtures.
3. Keep the Python CLI dependency-free unless a dependency has a compelling security or maintenance benefit.
4. Run:

   ```sh
   python3 -m compileall -q .agents/skills/fuxam-local/scripts
   python3 -m unittest discover -s tests -v
   uvx ruff==0.12.11 check .
   uvx ruff==0.12.11 format --check .
   uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
   ```

When Fuxam changes, explain the observed protocol behavior without attaching private responses. Keep commits focused, and document non-obvious security decisions in the code or pull request.
