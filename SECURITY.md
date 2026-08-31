# Security policy

## Supported version

Security fixes are applied to the current `main` branch. This repository currently targets local development and controlled single-instance deployment; the ordinary HTTP management API is not a public multi-tenant boundary.

## Report a vulnerability

Use GitHub private vulnerability reporting when it is enabled for this repository. Do not open a public issue containing a Cookie, Token, proxy URL, Redis/MySQL connection, raw Amazon response, local path, account identifier, or exploit details. If private reporting is unavailable, open a public issue containing only a non-sensitive request for a private contact channel.

Include the affected commit, interface, expected boundary, minimal reproduction and impact. Remove all real credentials and customer data.

## Secret handling

- Keep runtime secrets only in `.env` or the deployment Secret manager.
- Never place secrets in MCP Tool arguments, task inputs, logs, screenshots, evidence, examples or Agent conversations.
- Run `python scripts/audit_public_artifacts.py` before publishing documentation or demo evidence.
- If a secret is exposed, stop affected workers, rotate or revoke it, remove the public artifact, and review Git history before resuming.
