# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | Yes |

## Reporting a vulnerability

Please report security issues through GitHub Security Advisories:

<https://github.com/Dongmingyue-loss/zcode-session-exporter/security/advisories/new>

Do not open a public issue for a vulnerability that could expose session data, credentials, or local files.

## Scope

This tool is a single-user local application:

- It binds only to `127.0.0.1`.
- API requests require a random token generated at startup.
- Source databases are opened read-only with `PRAGMA query_only = 1`.
- It does not send session content to any remote service and has no telemetry.

## Data handling

Exports can contain raw conversation text, tool output, file paths, and other sensitive information. Treat exported TXT/JSONL files as sensitive data. Do not upload them to public services without review.

The tool never stores the source database contents in logs. Error messages may include file paths but not message bodies.
