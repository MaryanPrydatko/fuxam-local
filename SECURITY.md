# Security policy

## Credential model

Fuxam Local renews short-lived Fuxam session tokens from Clerk's `__client` cookie. That cookie is equivalent to a password while valid.

- Enter it only through `fuxam.py auth set`, which uses a hidden terminal prompt.
- Never put it in chat, source, issues, screenshots, shell arguments, or environment variables.
- The value is stored in macOS Keychain under service `codex-fuxam-local` and account `__client`.
- Clear it with `python3 "$HOME/.agents/skills/fuxam-local/scripts/fuxam.py" auth clear`.

If a credential may have been exposed, clear it, sign out of Fuxam/Clerk sessions, and authenticate again with a fresh cookie.

## Enforced boundaries

- Requests are limited to HTTPS on port 443 at `fuxam.app` and `clerk.fuxam.app`.
- Bearer tokens may be sent only to `fuxam.app`; the Clerk cookie may be sent only to `clerk.fuxam.app`.
- Redirects must retain the exact origin.
- React server actions must appear in a fixed read-only allowlist.
- Response and traversal sizes are bounded.
- The CLI contains no mutation commands and no telemetry.

## Known limits

- Fuxam's private web API can change without notice.
- Python strings cannot be reliably zeroized from process memory.
- Academic results returned to an agent are subject to that agent client's data controls.
- This project cannot verify Fuxam's own server-side security or authorization behavior.

## Reporting a vulnerability

Do not include a real credential, session token, or raw academic record in a report. If this repository is published on GitHub, enable private vulnerability reporting and use that channel. Until then, contact the maintainer privately and provide a minimal synthetic reproduction.
