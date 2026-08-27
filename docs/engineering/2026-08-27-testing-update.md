# v0.4.1 verification

Recorded on 27 August 2026 for [v0.4.1](https://github.com/MaryanPrydatko/fuxam-local/releases/tag/v0.4.1).

- 153 unit tests passed on Python 3.10.14, 3.13.9, and 3.14.5.
- Compilation, Ruff lint/format, and skill validation passed.
- Read-only release checks passed: `doctor`, quick smoke (4/4), and deep smoke (6/6). The deep check repeats the four quick checks.

Separate, one-off stress runs recorded 444,426 parser calls and 8,148 booking scenarios across two seeds and three runtimes. Those counts include repetition; no distinct-input count was measured. The temporary harness was not shipped, so these runs are not reproducible from this repository. The checked-in regression suite is the maintained test evidence.

All automated booking tests used synthetic data. The live checks covered one account's authentication and selected reads at that moment. No live enrollment change or end-to-end agent approval flow was tested. Fuxam's private protocol can change.

A remaining compatibility observation: the JWT decoder accepts lone surrogate code points. These tests do not establish that every malformed response is handled.

For reproducible offline checks, see [CONTRIBUTING.md](../../CONTRIBUTING.md).

Release CLI SHA-256:

```text
527cd9e03cbd6f2b79d7fe1de0e8d5f06b6b8ecf03ab34873250f2a39dbd75c7
```
