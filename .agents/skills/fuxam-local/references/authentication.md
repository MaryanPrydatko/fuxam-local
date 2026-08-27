# Local authentication

The CLI stores the Fuxam Clerk `__client` cookie in macOS Keychain, Linux Secret Service, or Windows Credential Manager. It never stores the cookie in the skill, a `.env` file, shell environment, command history, or a hosted service.

Linux requires `secret-tool` (Ubuntu/Debian: `libsecret-tools`) and an unlocked Secret Service keyring, such as GNOME Keyring. Run in the same desktop session as the keyring. WSL, SSH, and headless sessions need a reachable Secret Service; they do not use the Windows store or fall back to files.

On native Windows, use `py -3` instead of `python3` in these commands.

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

On Linux, an inaccessible credential may be missing or locked. `auth status` reports `configured: null` when it cannot tell; this is not a confirmed login. Unlock the keyring before replacing the cookie. A failed `auth clear` does not confirm removal; inspect the Fuxam Local entry in the keyring.

Storage limits are 2,560 bytes on Windows and 8,191 bytes on Linux. Oversized cookies are rejected, not truncated. macOS keeps the existing 16 KiB limit and stored credentials.

`<skill-root>` means the directory containing the skill's `SKILL.md` file.
