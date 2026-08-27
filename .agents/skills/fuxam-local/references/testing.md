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

4. Verify the active-term page and dynamically resolved read action too. This deeper check may take longer:

   ```sh
   python3 "<skill-root>/scripts/fuxam.py" smoke-test --deep
   ```

The report contains only pass/fail status and response shapes. A passing test proves current compatibility for that account and moment; it cannot guarantee that Fuxam's private API will never change.

Study-plan and catalog checks use the same schema validation as their summary commands. A deep check exercises one catalog page, not a full pagination run, every read command, or any live write.

For a user-requested live workflow check, `learning-units --format table` is read-only. A `booking <operation> <course-id>` command without `--apply` may verify preview behavior for one eligible exact course ID; stop after confirming `mode: preview` and `changed: false`.

Repository maintainers should also run the credential-free offline suite documented in the repository README. Never run authenticated checks in CI, and never pass `--apply` while testing.
