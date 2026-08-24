# Fuxam Local

Private, local, read-only Fuxam access for agent harnesses—implemented as an open [Agent Skill](https://agentskills.io/specification) and a dependency-free Python CLI.

This is an unofficial student-built client. It is not affiliated with or endorsed by CODE University, Fuxam, Clerk, or CodeCampus. Fuxam remains the source of truth for academic records and availability.

## Compatibility

| Interface | Works with | Notes |
| --- | --- | --- |
| Agent Skill | Codex, Claude, and hosts implementing the Agent Skills standard | Automatic discovery when installed in a supported skill directory |
| JSON CLI | Any local agent allowed to run Python commands | The universal fallback; no daemon or protocol server |

“Any harness” here means a **local macOS harness** that can load an Agent Skill or execute a command. A hosted agent with no access to your Mac cannot reach its Keychain or Fuxam session. Supporting that would require a remote credential-bearing service, which this project intentionally does not provide.

## Why no MCP server?

This integration does not need one. A skill tells the agent when and how to invoke the local CLI, and the CLI starts only when Fuxam data is requested. That means:

- no daemon or MCP handshake;
- no extra tool catalog in every prompt;
- no Node.js process or runtime package download;
- no hosted intermediary or analytics SDK;
- the same script remains callable by agents without skill support.

MCP is useful for capabilities that genuinely need a long-lived cross-client tool server. It would add machinery here without unlocking the private local Keychain for hosted agents.

## Security and privacy

The CLI communicates directly with `fuxam.app` and `clerk.fuxam.app`. The Clerk `__client` credential is entered through a hidden prompt and stored in macOS Keychain—not in source, shell history, environment variables, agent configuration, or chat.

The implementation additionally enforces:

- exact HTTPS host and standard-port allowlists;
- same-origin redirects, preventing credential forwarding to another origin;
- a fixed allowlist of read-only Fuxam actions;
- bounded cookies, HTTP responses, catalog pages, scripts, and CLI integers;
- no booking, unbooking, waitlist, analytics, telemetry, or third-party runtime dependencies.

Fuxam results still pass through whichever agent client answers the question, so that client's data controls apply. See [SECURITY.md](SECURITY.md) for the full boundary.

## Requirements

- macOS with an unlocked login Keychain
- Python 3.10 or newer
- a CODE University Fuxam account
- a local agent harness or terminal

The runtime has no package dependencies. Repository maintainers additionally need [`uv`](https://docs.astral.sh/uv/) for pinned lint and format tools.

## Install for multiple harnesses

Clone or download this repository, then run from its root:

```sh
python3 install.py --dry-run
python3 install.py
```

For an existing Codex or Claude install, preview and perform the upgrade explicitly:

```sh
python3 install.py --replace --dry-run
python3 install.py --replace
```

The installer copies the canonical skill to `~/.agents/skills/fuxam-local` and creates compatibility aliases for `~/.claude/skills/fuxam-local` and `~/.codex/skills/fuxam-local`. It refuses to overwrite an existing path unless `--replace` is set; every replaced path is preserved as a timestamped backup.

Open a new agent task after installing or updating a skill. Other local agents can call the installed `scripts/fuxam.py` directly even if they do not implement Agent Skills.

The `.agents/skills` location follows the open Agent Skills layout used by [Codex skills](https://developers.openai.com/codex/skills). Claude also supports Agent Skills from its [skills directory](https://code.claude.com/docs/en/skills), which the installer aliases to the same canonical copy.

## Authenticate locally

Treat the Clerk cookie like a password. Never paste it into an AI chat, screenshot, command argument, issue, or agent configuration.

1. Sign in at <https://fuxam.app>.
2. Open <https://clerk.fuxam.app/v1/client?__clerk_api_version=2026-05-12&_clerk_js_version=6.29.2> in another tab.
3. On that Clerk tab, open Developer Tools → Application/Storage → Cookies → `https://clerk.fuxam.app`.
4. Copy only the value of the `__client` cookie.
5. Run the hidden local prompt:

   ```sh
   python3 "$HOME/.agents/skills/fuxam-local/scripts/fuxam.py" auth set
   ```

6. Paste the value, press Return, and clear the clipboard.

If the session expires, repeat `auth set`. Remove the stored credential with `auth clear`.

## Test it

Start with the dependency-free offline suite. It uses synthetic fixtures, makes no Fuxam requests, and needs no credential:

```sh
python3 -m compileall -q .agents/skills/fuxam-local/scripts
python3 -m unittest discover -s tests -v
```

Repository maintainers should also run the pinned lint and portability checks. `uvx` may download these tools from PyPI when they are not already cached:

```sh
uvx ruff check .
uvx ruff format --check .
uvx --from skills-ref==0.1.1 agentskills validate .agents/skills/fuxam-local
```

Then test the installed copy on your Mac:

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"
python3 "$FUXAM" doctor
python3 "$FUXAM" smoke-test
python3 "$FUXAM" smoke-test --deep
```

- `doctor` is offline and reports only runtime, platform, and credential readiness.
- `smoke-test` exercises representative read-only JSON endpoints.
- `smoke-test --deep` also verifies the dynamically resolved frontend server-action path. It may take longer.
- Both smoke modes return only pass/fail and response shapes—never academic record values.

A passing deep smoke test is the strongest current compatibility check. It cannot guarantee future compatibility because Fuxam exposes no supported public API for this project.

## Use it

Ask a skill-aware agent naturally:

```text
Use $fuxam-local to show my current modules and learning units.
Use $fuxam-local to compare these modules with my progress.
Use $fuxam-local to check this proposed course set for conflicts.
Use $fuxam-local to summarize my appointments and deadlines next month.
```

Or run the JSON CLI directly from any local harness:

```sh
FUXAM="$HOME/.agents/skills/fuxam-local/scripts/fuxam.py"
python3 "$FUXAM" --help
python3 "$FUXAM" explore
python3 "$FUXAM" enrolled
python3 "$FUXAM" agenda --limit 25
```

Its read-only commands cover catalog exploration, enrolled and bookable courses, the study plan, search, appointments, deadlines, module details and attempts, exam details, conflict checks, pinned courses, todos, layer paths, and excluded dates.

## Stability

Fuxam exposes no supported public API for this project. Endpoint or frontend changes can break the client. Failures are surfaced explicitly, and the client never treats its output as more authoritative than Fuxam itself.

## License

MIT. See [LICENSE](LICENSE), [NOTICE.md](NOTICE.md), and [CHANGELOG.md](CHANGELOG.md).
