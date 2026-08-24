# Fuxam Local

Let Codex, Claude, or another local coding agent read your Fuxam study data.

Fuxam Local is open source, read-only, and runs locally on your Mac while talking directly to Fuxam. There is no MCP server, telemetry, hosted middleman, or runtime package to install.

It can inspect your modules, learning units, progress, study plan, appointments, deadlines, exams, todos, and schedule conflicts. It cannot book, unbook, or change anything in your account.

> This is an unofficial student project. It is not affiliated with CODE University, Fuxam, Clerk, or CodeCampus. Fuxam remains the source of truth.

## Why a skill instead of MCP?

A skill is enough here. It tells the agent when to run a normal Python command, and that command exits when it is done. No daemon, protocol handshake, or permanent tool catalog is needed.

Agents that do not support skills can still run the same CLI directly.

## Requirements

- macOS with an unlocked login Keychain
- Python 3.10 or newer
- a CODE University Fuxam account

## Install

Clone or download this repository, then run:

```sh
python3 install.py
```

Updating an existing installation:

```sh
python3 install.py --replace
```

The installer keeps one shared copy in `~/.agents/skills/fuxam-local` and makes it available to Codex and Claude. Existing copies are backed up before replacement.

Start a new Codex or Claude session after installing. Any other local agent can call `scripts/fuxam.py` directly.

## Connect your account

Your Clerk `__client` cookie works like a password. Never paste it into a chat, issue, screenshot, command argument, or config file.

1. Sign in at <https://fuxam.app>.
2. Open the [Clerk client page](https://clerk.fuxam.app/v1/client?__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2).
3. On that page, open Developer Tools → Application/Storage → Cookies → `https://clerk.fuxam.app`.
4. Copy only the value of the `__client` cookie.
5. Run:

   ```sh
   python3 "$HOME/.agents/skills/fuxam-local/scripts/fuxam.py" auth set
   ```

6. Paste the value into the hidden prompt, press Return, and clear your clipboard.

The cookie is stored in macOS Keychain. If it expires, repeat these steps. Remove it at any time with `auth clear`.

## Check that it works

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"

python3 "$FUXAM" doctor
python3 "$FUXAM" smoke-test
python3 "$FUXAM" smoke-test --deep
```

- `doctor` checks Python, macOS, Keychain, and authentication without contacting Fuxam.
- `smoke-test` checks the main read-only endpoints.
- `smoke-test --deep` also checks Fuxam's frontend-backed course lookup and may take longer.

The smoke report contains only pass/fail results and broad response types—not your academic records.

## Use it

In Codex:

```text
Use $fuxam-local to show my enrolled modules.
Use $fuxam-local to summarize my deadlines next month.
Use $fuxam-local to check these courses for schedule conflicts.
```

In Claude, invoke `/fuxam-local` and ask the same questions.

Or use the CLI yourself:

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"

python3 "$FUXAM" --help
python3 "$FUXAM" explore
python3 "$FUXAM" enrolled
python3 "$FUXAM" agenda --limit 25
```

## Privacy and safety

- The Clerk cookie is stored only in macOS Keychain and sent only to Clerk.
- Fuxam bearer tokens are sent only to `fuxam.app` over HTTPS.
- Redirects, hosts, ports, response sizes, and server actions are restricted in code.
- There are no write actions, analytics, telemetry, hosted services, or third-party runtime dependencies.

Results still pass through the agent client you use, so its data controls apply. See [SECURITY.md](SECURITY.md) for the exact boundary.

## Development

The offline test suite uses synthetic data and never contacts Fuxam:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full maintainer checks.

## Limits

Fuxam has no supported public API for this project, so a Fuxam update may break the client. Run the smoke test when something looks wrong, and always verify important information in Fuxam itself.

## License

MIT. See [LICENSE](LICENSE).
