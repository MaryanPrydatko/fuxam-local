# Testing Fuxam Local

Use the script relative to this skill directory. Never put a real credential in a command, test fixture, log, or chat.

1. Check the local runtime and Keychain without contacting Fuxam:

   ```sh
   python3 "<skill-root>/scripts/fuxam.py" doctor
   ```

2. If authentication is missing, follow [authentication.md](authentication.md). The user must run the hidden `auth set` prompt locally.

3. Verify the ordinary read-only endpoints without returning academic data:

   ```sh
   python3 "<skill-root>/scripts/fuxam.py" smoke-test
   ```

4. Verify the dynamically resolved frontend action too. This deeper check may take longer:

   ```sh
   python3 "<skill-root>/scripts/fuxam.py" smoke-test --deep
   ```

The report contains only pass/fail status and response shapes. A passing test proves current compatibility for that account and moment; it cannot guarantee that Fuxam's private API will never change.

Repository maintainers should also run the credential-free offline suite documented in the repository README. Never run authenticated live checks in CI.
