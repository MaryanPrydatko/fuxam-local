# Fuxam Local

Private, local, read-only Fuxam access for Codex—implemented as a Codex skill and a dependency-free Python CLI.

This is an unofficial student-built client. It is not affiliated with or endorsed by CODE University, Fuxam, Clerk, or CodeCampus. Fuxam remains the source of truth for academic records and availability.

## Why a skill instead of an MCP server?

For one person using Codex on one Mac, a skill is the smaller interface:

- no daemon or MCP protocol session;
- no Node.js or package download at runtime;
- no separately registered tool catalog in every prompt;
- no analytics SDK or hosted intermediary;
- one local Python process only when Fuxam data is requested.

MCP is useful when the same tools must work across many clients or need a long-lived process. It is not inherently bad or always slower; it is simply more machinery than this personal integration needs.

## Security and privacy

The CLI communicates directly with `fuxam.app` and `clerk.fuxam.app`. The Clerk `__client` credential is entered through a hidden prompt and stored in macOS Keychain—not in source, shell history, environment variables, or Codex configuration.

The implementation additionally enforces:

- exact HTTPS host and standard-port allowlists;
- same-origin redirects, preventing credential forwarding to another origin;
- a fixed allowlist of read-only Fuxam actions;
- bounded cookies, HTTP responses, catalog pages, scripts, and CLI integers;
- no booking, unbooking, or waitlist code;
- no telemetry or third-party runtime dependencies.

Fuxam results still pass through the Codex client when the skill answers a question, so Codex's own data controls apply. See [SECURITY.md](SECURITY.md) for the full boundary.

## Requirements

- macOS with an unlocked login Keychain
- Python 3.10 or newer
- a CODE University Fuxam account
- Codex Desktop or Codex CLI

## Install in the Codex skill library

Clone or download this repository, then run from its root:

```sh
mkdir -p "$HOME/.codex/skills/fuxam-local"
ditto .agents/skills/fuxam-local "$HOME/.codex/skills/fuxam-local"
```

Open a new Codex task after installing or updating the skill. The repository also uses the standard `.agents/skills` layout, so Codex can discover it while working inside this repository.

## Authenticate locally

Treat the Clerk cookie like a password. Never paste it into an AI chat, screenshot, command argument, or issue.

1. Sign in at <https://fuxam.app>.
2. Open <https://clerk.fuxam.app/v1/client?__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2> in another tab.
3. On that Clerk tab, open Developer Tools → Application/Storage → Cookies → `https://clerk.fuxam.app`.
4. Copy only the value of the `__client` cookie.
5. Run the hidden local prompt:

   ```sh
   python3 "$HOME/.codex/skills/fuxam-local/scripts/fuxam.py" auth set
   ```

6. Paste the value, press Return, and clear the clipboard.

Verify configuration without displaying the credential:

```sh
python3 "$HOME/.codex/skills/fuxam-local/scripts/fuxam.py" auth status
python3 "$HOME/.codex/skills/fuxam-local/scripts/fuxam.py" context
```

If the session expires, repeat `auth set`. Remove the stored credential with `auth clear`.

## Use it

Ask Codex naturally:

```text
Use $fuxam-local to show my current modules and learning units.
Use $fuxam-local to compare these modules with my progress.
Use $fuxam-local to check this proposed course set for conflicts.
Use $fuxam-local to summarize my appointments and deadlines next month.
```

The bundled CLI is also usable directly:

```sh
FUXAM="$HOME/.codex/skills/fuxam-local/scripts/fuxam.py"
python3 "$FUXAM" --help
python3 "$FUXAM" explore
python3 "$FUXAM" enrolled
python3 "$FUXAM" agenda --limit 25
```

Its read-only commands cover catalog exploration, enrolled and bookable courses, the study plan, search, appointments, deadlines, module details and attempts, exam details, conflict checks, pinned courses, todos, layer paths, and excluded dates.

## Development

There are no runtime dependencies. From the repository root:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
uvx ruff check .
uvx ruff format --check .
```

Tests use synthetic data and mocked networking. They never need a student credential or contact Fuxam.

## Stability

Fuxam exposes no supported public API for this project. Endpoint or frontend changes can break the client. Failures are surfaced explicitly, and the client never treats its output as more authoritative than Fuxam itself.

## License

MIT. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and [CHANGELOG.md](CHANGELOG.md).
