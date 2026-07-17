# Security Policy

logmon is an evidence-integrity tool; security issues are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 0.0.x (pre-release) | Best effort |

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Report privately to the maintainer via GitHub Security Advisories on
<https://github.com/OnyxOmega/logmon> ("Report a vulnerability"), or by direct
contact with the repository owner (YASDC / Kevin Perryman).

Please include:
- A description of the issue and its impact.
- Steps to reproduce, or a proof of concept.
- Affected version(s) and environment.

You can expect an acknowledgement within a reasonable period and coordinated
disclosure once a fix is available.

## Scope notes

logmon deliberately **never** alters OS Event Log configuration and runs its
alert watcher unprivileged. Findings that involve privilege boundaries
(service vs GUI vs tray), the alert-store ACLs, manifest hashing/signing, or the
tamper-detection watermark logic are especially in scope.
