# Local authentication

Saved login is the default: macOS Keychain, Linux Secret Service, or Windows Credential Manager. Temporary login is an explicit alternative; neither mode loads `.env` files or falls back to the other.

`<skill-root>` means the directory containing `SKILL.md`. On native Windows, use `py -3` instead of `python3`.

## Find the cookie

The user does these steps in their own browser. Never paste the cookie into chat or tool arguments.

1. Sign in at <https://fuxam.app>.
2. Open <https://clerk.fuxam.app/v1/client?__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2> in a separate tab.
3. On that Clerk tab, open Developer Tools → Application/Storage → Cookies → `https://clerk.fuxam.app`.
4. Copy only the value of the `__client` cookie. Keep it out of AI chats, screenshots, shell arguments, and clipboard history where possible.

Then choose saved or temporary login below and clear the clipboard after pasting.

## Saved login

Linux needs `secret-tool` and a running, unlocked Secret Service provider such as GNOME Keyring. Install the CLI through your distribution:

| Distribution | Command |
| --- | --- |
| Debian / Ubuntu | `sudo apt install libsecret-tools` |
| Fedora | `sudo dnf install libsecret` |
| Arch | `sudo pacman -S libsecret` |

The package alone does not set up or unlock a keyring. Use the same desktop session as the keyring; automatic login may leave it locked. WSL uses the Linux backend, not Windows Credential Manager. For SSH or headless use without a reachable keyring, choose temporary login.

Run directly in an interactive terminal and paste into the hidden prompt:

```sh
python3 "<skill-root>/scripts/fuxam.py" auth set
```

After the prompt finishes, check the connection:

```sh
python3 "<skill-root>/scripts/fuxam.py" doctor
python3 "<skill-root>/scripts/fuxam.py" smoke-test
```

If input cannot be hidden, `auth set` refuses to read it. Do not pipe a cookie into it. When the session expires, repeat `auth set` with a fresh cookie. To remove the saved copy:

```sh
python3 "<skill-root>/scripts/fuxam.py" auth clear
```

On Linux, an inaccessible credential may be missing or locked. `auth status` reports `configured: null` when it cannot tell; this is not a confirmed login. Unlock the keyring before replacing the cookie. A failed `auth clear` does not confirm removal; inspect the Fuxam Local entry in the keyring.

Storage limits are 2,560 bytes on Windows and 8,191 bytes on Linux. Oversized cookies are rejected, not truncated. macOS keeps the existing 16 KiB limit and stored credentials.

## Temporary login

No keyring or `secret-tool` is needed. Supply `FUXAM_COOKIE` in the process environment and put `--auth env` before every command. It is never saved by this CLI. It remains readable by programs that inherit your shell's environment, so use a trusted terminal and unset it when done. Do not put it in `.env`, a shell profile, command history, or chat.

To enter it without putting its value in shell history, use the block for your shell directly in an interactive terminal:

Bash (usual Linux default):

```bash
unset FUXAM_COOKIE
read -rs -p 'Fuxam __client (hidden): ' FUXAM_COOKIE && export FUXAM_COOKIE
```

Zsh (macOS default):

```zsh
unset FUXAM_COOKIE
read -rs 'FUXAM_COOKIE?Fuxam __client (hidden): ' && export FUXAM_COOKIE
```

Then run:

```sh
python3 "<skill-root>/scripts/fuxam.py" --auth env doctor
python3 "<skill-root>/scripts/fuxam.py" --auth env smoke-test
python3 "<skill-root>/scripts/fuxam.py" --auth env learning-units --format table
unset FUXAM_COOKIE
```

Windows PowerShell:

```powershell
$env:FUXAM_COOKIE = [System.Net.NetworkCredential]::new('', (Read-Host 'Fuxam __client' -AsSecureString)).Password
try {
    py -3 "<skill-root>/scripts/fuxam.py" --auth env doctor
    py -3 "<skill-root>/scripts/fuxam.py" --auth env learning-units --format table
} finally {
    Remove-Item Env:FUXAM_COOKIE -ErrorAction SilentlyContinue
}
```

`auth status` works with `--auth env`; `auth set` and `auth clear` only manage saved login and are rejected in this mode. A missing or malformed variable stops the request. If the variable is set without `--auth env`, the CLI stops rather than choosing an account for you. Unset it to return to saved login.

For agents, launch the agent from the same terminal before unsetting the variable. In PowerShell, put the agent's launch command inside the `try` block above. Tell it to use `--auth env` for every Fuxam command, including booking previews and applies. An already-running desktop app will not inherit a newly set variable. Never ask an agent to print or inspect the value.

Temporary mode uses the same account checks and booking approvals. It is not an encrypted store or protection from other processes running as you.
