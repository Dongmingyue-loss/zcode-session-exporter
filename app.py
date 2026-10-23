#!/usr/bin/env python3
"""Local web GUI for exporting ZCode / ZCodium session messages to TXT."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse

from exporter import ExportOptions, Exporter, Source, VERSION

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def default_output_dir() -> Path:
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / "ZCode-Session-Exports"


def _source_candidates(base_name: str, env_name: str, explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    env_value = os.environ.get(env_name)
    if env_value:
        return [Path(env_value)]
    home = Path.home()
    names = [base_name, base_name.lower()]
    candidates = [home / f".{base_name.lower()}" / "cli" / "db" / "db.sqlite"]
    for name in names:
        candidates.extend(
            [
                home / "Library" / "Application Support" / name / "cli" / "db" / "db.sqlite",
                home / ".config" / name / "cli" / "db" / "db.sqlite",
                home / ".local" / "share" / name / "cli" / "db" / "db.sqlite",
            ]
        )
    return candidates


def _first_existing(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def build_sources(args: argparse.Namespace) -> dict[str, Source]:
    demo_db = ROOT / "demo" / "demo.sqlite"
    if getattr(args, "demo", False):
        if not demo_db.exists():
            raise FileNotFoundError(
                f"demo database not found: {demo_db}; run scripts/make_demo_db.py"
            )
        return {"demo": Source("demo", "Demo", demo_db)}

    zcode_db = _first_existing(
        _source_candidates("ZCode", "ZCODE_DB", args.zcode_db)
    )
    zcodium_db = _first_existing(
        _source_candidates("ZCodium", "ZCODIUM_DB", args.zcodium_db)
    )
    sources = {
        "zcode": Source("zcode", "ZCode", zcode_db),
        "zcodium": Source("zcodium", "ZCodium", zcodium_db),
    }
    if demo_db.exists():
        sources["demo"] = Source("demo", "Demo", demo_db)
    return sources


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    opener = shutil.which("xdg-open")
    if not opener:
        raise OSError("xdg-open not found; open the directory manually")
    subprocess.Popen([opener, str(path)])


class Handler(BaseHTTPRequestHandler):
    server_version = f"ZCodeSessionExporter/{VERSION}"

    # -- helpers ----------------------------------------------------------
    @property
    def service(self) -> SimpleNamespace:
        return self.server.service  # type: ignore[attr-defined]

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _path(self) -> str:
        return urlparse(self.path).path

    def _authorized(self) -> bool:
        token = self.headers.get("X-Auth-Token")
        if not token:
            token = self._query().get("token", [None])[0]
        if not token:
            return False
        try:
            return secrets.compare_digest(
                token.encode("utf-8"), self.service.token.encode("utf-8")
            )
        except (TypeError, UnicodeEncodeError):
            return False

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _source(self, source_id: str) -> Source:
        source = self.service.sources.get(source_id)
        if not source:
            raise ValueError(f"unknown source: {source_id}")
        if not source.db_path.exists():
            raise FileNotFoundError(f"database not found: {source.db_path}")
        info = source.validate()
        if info.error:
            raise ValueError(info.error)
        return source

    def _serve_static(self, relative: str) -> None:
        target = (STATIC / relative).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            self._error("not found", 404)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        content_type = content_types.get(target.suffix.lower(), "application/octet-stream")
        body = target.read_bytes()
        headers = {}
        if target.suffix.lower() == ".html":
            headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            )
        self._send(200, body, content_type, headers)

    def _serve_download(self, job_id: str) -> None:
        job = self.service.exporter.get(job_id)
        if not job or not job.download_path:
            self._error("download not ready", 404)
            return
        path = Path(job.download_path).resolve()
        allowed = [Path(job.output_dir).resolve()] if job.output_dir else []
        if not allowed or not any(path.is_relative_to(root) for root in allowed):
            self._error("invalid download path", 403)
            return
        if not path.is_file():
            self._error("file not found", 404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(path.name)}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- routing ----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self._path()
        query = self._query()
        try:
            if path in {"/", "/index.html"}:
                self._serve_static("index.html")
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/") :])
                return
            if path == "/favicon.ico":
                self._serve_static("favicon.svg")
                return

            if path == "/api/health":
                self._json({"ok": True, "time": time.time()})
                return

            if path.startswith("/api/") and not self._authorized():
                self._error("unauthorized", 401)
                return

            if path == "/api/sources":
                infos = [source.validate().to_dict() for source in self.service.sources.values()]
                self._json(
                    {
                        "sources": infos,
                        "default_output_dir": str(self.service.exporter.default_output_dir),
                    }
                )
                return

            if path == "/api/sessions":
                source = self._source(query.get("source", ["zcode"])[0])
                q = query.get("q", [""])[0]
                sort = query.get("sort", ["updated"])[0]
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = max(1, min(1000, int(query.get("limit", ["500"])[0])))
                sessions, total = source.list_sessions(q, sort, offset, limit)
                self._json({"sessions": sessions, "total": total, "source": source.id})
                return

            if path == "/api/session":
                source = self._source(query.get("source", ["zcode"])[0])
                session_id = query.get("id", [""])[0]
                session = source.get_session(session_id)
                if not session:
                    self._error("session not found", 404)
                    return
                self._json({"session": session})
                return

            if path == "/api/messages":
                source = self._source(query.get("source", ["zcode"])[0])
                session_id = query.get("id", [""])[0]
                offset = max(0, int(query.get("offset", ["0"])[0]))
                limit = max(1, min(100, int(query.get("limit", ["20"])[0])))
                max_chars = max(0, min(200_000, int(query.get("max_chars", ["8000"])[0])))
                result = source.get_messages(session_id, offset, limit, max_chars)
                self._json(result)
                return

            if path == "/api/part":
                source = self._source(query.get("source", ["zcode"])[0])
                part_id = query.get("id", [""])[0]
                max_chars = max(0, min(5_000_000, int(query.get("max_chars", ["2000000"])[0])))
                part = source.get_part(part_id, max_chars)
                if not part:
                    self._error("part not found", 404)
                    return
                self._json({"part": part})
                return

            if path == "/api/jobs":
                jobs = sorted(
                    self.service.exporter.jobs.values(),
                    key=lambda item: item.created_at,
                    reverse=True,
                )[:20]
                self._json({"jobs": [job.to_dict() for job in jobs]})
                return

            if path == "/api/job":
                job = self.service.exporter.get(query.get("id", [""])[0])
                if not job:
                    self._error("job not found", 404)
                    return
                self._json({"job": job.to_dict()})
                return

            if path == "/api/job/download":
                self._serve_download(query.get("id", [""])[0])
                return

            self._error("not found", 404)
        except FileNotFoundError as exc:
            self._error(str(exc), 404)
        except (ValueError, sqlite3.Error) as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001 - local diagnostic
            self._error(f"internal error: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self._path()
        try:
            if path.startswith("/api/") and not self._authorized():
                self._error("unauthorized", 401)
                return

            if path == "/api/export":
                data = self._read_json()
                options = ExportOptions.from_dict(data)
                job = self.service.exporter.start(options)
                self._json({"job": job.to_dict()})
                return

            if path == "/api/preview":
                data = self._read_json()
                options = ExportOptions.from_dict(data)
                session_id = str(data.get("session_id") or "")
                if not session_id:
                    raise ValueError("session_id is required")
                preview = self.service.exporter.preview(
                    options,
                    session_id,
                    int(data.get("preview_messages") or 30),
                    int(data.get("preview_chars") or 200_000),
                    int(data.get("preview_offset") or 0),
                )
                self._json(preview)
                return

            if path == "/api/job/cancel":
                data = self._read_json()
                job_id = str(data.get("id") or "")
                ok = self.service.exporter.cancel(job_id)
                self._json({"ok": ok})
                return

            if path == "/api/reveal":
                data = self._read_json()
                raw_path = Path(str(data.get("path") or "")).resolve()
                roots = [self.service.exporter.default_output_dir.resolve()]
                roots += [
                    Path(job.output_dir).resolve()
                    for job in self.service.exporter.jobs.values()
                    if job.output_dir
                ]
                if not any(raw_path.is_relative_to(root) for root in roots):
                    self._error("path is outside the export directory", 403)
                    return
                if not raw_path.exists():
                    self._error("path not found", 404)
                    return
                open_path(raw_path)
                self._json({"ok": True})
                return

            self._error("not found", 404)
        except (ValueError, OSError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            self._error(f"internal error: {exc}", 500)

    def log_message(self, fmt: str, *args) -> None:
        # Never log query strings or message content.
        safe_path = self._path()
        sys.stderr.write(f"{self.command} {safe_path} {args[1] if len(args) > 1 else ''}\n")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        # HTTPServer.server_bind() calls socket.getfqdn(), which can block for
        # tens of seconds on macOS/CI when reverse DNS is slow. The bind address
        # is always loopback, so the hostname is already known.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZCode / ZCodium session message exporter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3220)
    parser.add_argument("--zcode-db", default=None)
    parser.add_argument("--zcodium-db", default=None)
    parser.add_argument("--demo", action="store_true", help="Use only the bundled demo database")
    parser.add_argument(
        "--output-dir",
        default=str(default_output_dir()),
    )
    parser.add_argument("--token", default=None)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--version", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.version:
        print(f"zcode-session-exporter {VERSION}")
        return 0
    if args.host not in {"127.0.0.1", "localhost"}:
        print("Only loopback binding is allowed (127.0.0.1).", file=sys.stderr)
        return 2

    sources = build_sources(args)
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    exporter = Exporter(sources, output_dir)
    token = args.token or secrets.token_urlsafe(24)

    server = None
    for port in range(args.port, args.port + 12):
        try:
            server = Server((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print(f"Could not bind a port in {args.port}-{args.port + 11}.", file=sys.stderr)
        return 2

    server.service = SimpleNamespace(  # type: ignore[attr-defined]
        sources=sources,
        exporter=exporter,
        token=token,
    )

    url = f"http://127.0.0.1:{server.server_port}/?token={token}"
    print("ZCode / ZCodium Session Exporter", flush=True)
    for source in sources.values():
        print(f"  {source.label}: {source.db_path}", flush=True)
    print(f"  Output: {output_dir}", flush=True)
    print(f"  URL: {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
