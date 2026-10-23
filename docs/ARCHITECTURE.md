# Architecture

`zcode-session-exporter` is a local-only web application with no runtime third-party dependencies.

## Components

```text
app.py
  HTTP server, token auth, static files, JSON API, export job control

exporter.py
  read-only SQLite access, session statistics, paged preview,
  readable/raw formatting, streaming export, rotating writer

static/
  index.html, app.js, styles.css
  browser UI: session list, message viewer, preview modal, export panel

demo/demo.sqlite
  synthetic data used by screenshots and UI smoke tests

scripts/
  make_demo_db.py, package.py, secret_scan.py

test/
  test_exporter.py, test_server.py, ui_smoke.py
```

## Data flow

```text
ZCode / ZCodium SQLite
        |
        |  file:...?mode=ro, PRAGMA query_only=1
        v
Source.connect()
        |
        +--> session_stats()  one grouped scan for counts/chars
        +--> get_session()    metadata + part type distribution
        +--> get_messages()   one page of messages + parts
        +--> preview()        one page rendered with the real formatter
        +--> export job       streaming cursor -> rotating writer -> zip
        |
        v
HTTP JSON API (127.0.0.1 + token)
        |
        v
Browser UI
```

## Large-session design

- Session statistics use one grouped scan instead of per-session subqueries.
- Message preview uses `LIMIT/OFFSET` on `message` before joining `part`.
- Preview text is capped; tool output is capped more aggressively.
- Export uses a single streaming cursor and writes each message as it is read.
- Split files rotate at message boundaries.
- The browser never receives the full session unless the user exports it.

## Cross-platform behavior

- Windows: `os.startfile` opens the export folder.
- macOS: `open`.
- Linux: `xdg-open`.
- `start.cmd` and `start.sh` are both provided.
- SQLite paths are percent-encoded before building a `file:` URI.
- If a WAL database cannot be opened with `mode=ro`, the tool retries with
  `mode=ro&immutable=1` (recent WAL-only rows may be unavailable in that fallback).

## SQLite compatibility

`session_stats()` and `get_session()` prefer SQLite JSON1 functions. If
`json_valid()` is unavailable, both fall back to parsing `part.data` in Python.
This keeps the tool usable on older SQLite builds at the cost of a slower first
statistics scan.

## Security model

- Bind address is forced to loopback.
- Every `/api/*` request requires a random token generated at startup.
- Source databases are opened read-only.
- Static file and download paths are checked against their allowed roots.
- Message content is never written to the process log.
- No telemetry, no remote API calls, no upload.
