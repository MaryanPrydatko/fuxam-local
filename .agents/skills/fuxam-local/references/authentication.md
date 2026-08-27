# Local authentication

The CLI stores the Fuxam Clerk `__client` cookie in the user's macOS login Keychain under service `codex-fuxam-local`. It never stores the cookie in the skill, shell environment, command history, or a hosted service.

The user must complete setup themselves in a local terminal:

1. Sign in at <https://fuxam.app>.
2. Open <https://clerk.fuxam.app/v1/client?__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2> in a separate tab.
3. On that Clerk tab, open Developer Tools → Application/Storage → Cookies → `https://clerk.fuxam.app`.
4. Copy only the value of the `__client` cookie. Keep it out of AI chats, screenshots, shell arguments, and clipboard history where possible.
5. Run:

   ```sh
   python3 "<skill-root>/scripts/fuxam.py" auth set
   ```

6. Paste only the `__client` value into the hidden terminal prompt, then clear the clipboard.

Run `auth set` directly in an interactive terminal. If the terminal cannot hide input, the CLI refuses to read the credential. Do not work around this with a pipe, argument, or environment variable.

Verify without revealing the credential:

```sh
python3 "<skill-root>/scripts/fuxam.py" doctor
python3 "<skill-root>/scripts/fuxam.py" context
```

If the session expires, repeat `auth set` with a fresh cookie. To remove it:

```sh
python3 "<skill-root>/scripts/fuxam.py" auth clear
```

`<skill-root>` means the directory containing the skill's `SKILL.md` file.
