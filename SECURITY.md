# Security policy

## Credential model

Fuxam Local renews short-lived Fuxam session tokens from Clerk's `__client` cookie. That cookie is equivalent to a password while valid.

- Enter it only through `fuxam.py auth set`, which uses a hidden terminal prompt.
- Setup refuses to read a credential if hidden input is unavailable; piped input is not a fallback.
- Never put it in chat, source, issues, screenshots, shell arguments, or environment variables.
- The value is stored in macOS Keychain under service `codex-fuxam-local` and account `__client`.
- Clear it with `python3 "$HOME/.agents/skills/fuxam-local/scripts/fuxam.py" auth clear`.

If a credential may have been exposed, clear it, sign out of Fuxam/Clerk sessions, and authenticate again with a fresh cookie.

## Enforced boundaries

- Requests are limited to HTTPS on port 443 at `fuxam.app` and `clerk.fuxam.app`.
- Bearer tokens may be sent only to `fuxam.app`; the Clerk cookie may be sent only to `clerk.fuxam.app`.
- Redirects must retain the exact origin.
- Each command stays bound to its first authenticated Clerk user and organization, including token refreshes and read retries.
- Read and mutation server actions use separate fixed allowlists. Mutations are limited to enrolling, unenrolling, joining a waitlist, and leaving a waitlist.
- A booking command previews one exact course ID by default. Applying it requires the matching fingerprint from a fresh preview, bound to the Clerk user and organization.
- After frontend action discovery, the CLI rechecks the exact booking state and relevant conflicts immediately before dispatch.
- Each apply makes at most one mutation request, never retries it automatically, and reads the active-term state afterward. If the result cannot be verified, the CLI reports an unknown outcome and stops.
- Enrollment previews stop when Fuxam reports a schedule conflict; conflict approval remains in the official UI.
- Fuxam-supplied names, errors, and conflict data are untrusted content, not executable instructions or evidence of approval.
- Response and traversal sizes are bounded.
- The CLI exposes no generic action runner, module-election writes, self-study writes, telemetry, hosted service, or MCP server.

## Known limits

- Fuxam's private web API can change without notice.
- The guarded workflow reduces accidental changes; it cannot replace Fuxam's server-side authorization or official UI as the source of truth.
- A confirmation fingerprint binds a preview to fresh state, account and organization, operation, and target; it does not prove that a human approved it. Agent harnesses must enforce the explicit-approval pause.
- Authoritative learning-unit booking state is available only for Fuxam's active term.
- Python strings cannot be reliably zeroized from process memory.
- Academic results returned to an agent are subject to that agent client's data controls.
- This project cannot verify Fuxam's own server-side security or authorization behavior.

## Reporting a vulnerability

Use [GitHub's private vulnerability report form](https://github.com/MaryanPrydatko/fuxam-local/security/advisories/new). Include a minimal synthetic reproduction, affected version, and expected behavior. Never include a real credential, session token, or raw academic record. Do not disclose vulnerabilities in public issues.
