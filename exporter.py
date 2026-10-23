#!/usr/bin/env python3
"""Read-only ZCode / ZCodium session reader and streaming exporter."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

VERSION = "1.0.0"


# --------------------------------------------------------------------------- utils

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fmt_time(ms: Optional[int]) -> str:
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "-"


def safe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
    return value


def truncate(value: str, limit: int) -> tuple[str, bool, int]:
    if limit <= 0 or len(value) <= limit:
        return value, False, len(value)
    return value[:limit], True, len(value)


def pretty(value: Any, limit: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(value)
    if limit > 0 and len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
    return text


def sanitize_filename(value: str, fallback: str = "session") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "").strip().strip(".")
    text = re.sub(r"\s+", " ", text)
    if not text:
        text = fallback
    text = text[:80]
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *[f"COM{i}" for i in range(1, 10)],
        *[f"LPT{i}" for i in range(1, 10)],
    }
    if text.split(".")[0].upper() in reserved:
        text = f"_{text}"
    return text


def looks_binary(value: str) -> bool:
    if len(value) < 4096:
        return False
    sample = value[:4096]
    if sample.startswith("data:"):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", sample))


def strip_binary(value: Any) -> Any:
    """Remove inline base64 blobs from raw exports while keeping references."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in {"base64", "base64Data", "imageBase64", "audioBase64", "dataUrl"}:
                result[key] = "[binary omitted]"
                continue
            if key == "data" and isinstance(child, str) and looks_binary(child):
                result[key] = "[binary omitted]"
                continue
            result[key] = strip_binary(child)
        return result
    if isinstance(value, list):
        return [strip_binary(item) for item in value]
    if isinstance(value, str) and looks_binary(value):
        return "[binary omitted]"
    return value


# --------------------------------------------------------------------------- sources

@dataclass
class SourceInfo:
    id: str
    label: str
    db_path: Path
    exists: bool = False
    size: int = 0
    mtime: float = 0.0
    session_count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "db_path": str(self.db_path),
            "exists": self.exists,
            "size": self.size,
            "mtime": self.mtime,
            "session_count": self.session_count,
            "error": self.error,
        }


class Source:
    def __init__(self, source_id: str, label: str, db_path: Path):
        self.id = source_id
        self.label = label
        self.db_path = Path(db_path)
        self._stats_cache: Optional[list[dict]] = None
        self._stats_key: Optional[tuple] = None
        self._json1: Optional[bool] = None
        self._immutable_fallback = False
        self._lock = threading.Lock()

    # -- connection -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise FileNotFoundError(f"database not found: {self.db_path}")
        base_uri = self.db_path.resolve().as_uri()
        last_error: Optional[Exception] = None
        for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
            try:
                con = sqlite3.connect(f"{base_uri}{suffix}", uri=True, timeout=30)
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only = 1")
                con.execute("PRAGMA busy_timeout = 30000")
                if suffix.endswith("immutable=1"):
                    self._immutable_fallback = True
                return con
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "unable to open database file" not in str(exc):
                    raise
        raise last_error or sqlite3.OperationalError("unable to open database file")

    def json1_available(self) -> bool:
        if self._json1 is not None:
            return self._json1
        con = self.connect()
        try:
            con.execute("SELECT json_valid('{}')").fetchone()
            self._json1 = True
        except sqlite3.Error:
            self._json1 = False
        finally:
            con.close()
        return self._json1

    def validate(self) -> SourceInfo:
        info = SourceInfo(self.id, self.label, self.db_path)
        if not self.db_path.exists():
            info.error = "database not found"
            return info
        stat = self.db_path.stat()
        info.exists = True
        info.size = stat.st_size
        info.mtime = stat.st_mtime
        try:
            con = self.connect()
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                required = {"session", "message", "part"}
                missing = required - tables
                if missing:
                    info.error = f"missing tables: {', '.join(sorted(missing))}"
                    return info
                info.session_count = con.execute(
                    "SELECT COUNT(*) FROM session"
                ).fetchone()[0]
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            info.error = str(exc)
        return info

    # -- stats ------------------------------------------------------------
    def _stats_key_now(self) -> tuple:
        stat = self.db_path.stat()
        wal = Path(f"{self.db_path}-wal")
        if wal.exists():
            wal_stat = wal.stat()
            wal_key = (wal_stat.st_mtime_ns, wal_stat.st_size)
        else:
            wal_key = (0, 0)
        return (stat.st_mtime_ns, stat.st_size, wal_key)

    def session_stats(self, force: bool = False) -> list[dict]:
        key = self._stats_key_now()
        with self._lock:
            if not force and self._stats_cache is not None and self._stats_key == key:
                return self._stats_cache

        session_query = """
        SELECT s.id, s.title, s.directory, s.time_created, s.time_updated,
               s.task_type, s.parent_id, s.slug
        FROM session s
        """
        message_query = """
        SELECT session_id, COUNT(*) AS message_count,
               COALESCE(SUM(LENGTH(data)), 0) AS message_chars
        FROM message GROUP BY session_id
        """
        part_query = """
        SELECT session_id, COUNT(*) AS part_count,
               COALESCE(SUM(LENGTH(data)), 0) AS part_chars,
               COALESCE(SUM(CASE WHEN json_valid(data)
                   AND json_extract(data, '$.type') = 'tool' THEN 1 ELSE 0 END), 0) AS tool_count,
               COALESCE(SUM(CASE WHEN json_valid(data)
                   AND json_extract(data, '$.type') = 'reasoning' THEN 1 ELSE 0 END), 0) AS reasoning_count
        FROM part GROUP BY session_id
        """
        con = self.connect()
        try:
            session_rows = con.execute(session_query).fetchall()
            message_stats = {row["session_id"]: dict(row) for row in con.execute(message_query)}
            if self.json1_available():
                part_stats = {
                    row["session_id"]: dict(row)
                    for row in con.execute(part_query)
                }
            else:
                part_stats = {}
                for row in con.execute("SELECT session_id, data FROM part"):
                    sid = row["session_id"]
                    entry = part_stats.setdefault(
                        sid,
                        {
                            "part_count": 0,
                            "part_chars": 0,
                            "tool_count": 0,
                            "reasoning_count": 0,
                        },
                    )
                    raw = row["data"] or ""
                    entry["part_count"] += 1
                    entry["part_chars"] += len(raw)
                    parsed = safe_json(raw)
                    if isinstance(parsed, dict):
                        part_type = parsed.get("type")
                        if part_type == "tool":
                            entry["tool_count"] += 1
                        elif part_type == "reasoning":
                            entry["reasoning_count"] += 1
        finally:
            con.close()

        stats = []
        for row in session_rows:
            item = dict(row)
            message = message_stats.get(item["id"], {})
            part = part_stats.get(item["id"], {})
            item["message_count"] = int(message.get("message_count") or 0)
            item["message_chars"] = int(message.get("message_chars") or 0)
            item["part_count"] = int(part.get("part_count") or 0)
            item["part_chars"] = int(part.get("part_chars") or 0)
            item["tool_count"] = int(part.get("tool_count") or 0)
            item["reasoning_count"] = int(part.get("reasoning_count") or 0)
            item["title"] = item.get("title") or "(untitled)"
            item["directory"] = item.get("directory") or ""
            item["estimated_tokens"] = max(
                1, (item["message_chars"] + item["part_chars"]) // 2
            )
            stats.append(item)

        with self._lock:
            self._stats_cache = stats
            self._stats_key = key
        return stats

    def list_sessions(
        self,
        query: str = "",
        sort: str = "updated",
        offset: int = 0,
        limit: int = 500,
    ) -> tuple[list[dict], int]:
        stats = self.session_stats()
        if query:
            needle = query.lower()
            stats = [
                item
                for item in stats
                if needle in str(item.get("title", "")).lower()
                or needle in str(item.get("directory", "")).lower()
                or needle in str(item.get("id", "")).lower()
            ]

        sorters = {
            "updated": lambda item: item.get("time_updated") or 0,
            "created": lambda item: item.get("time_created") or 0,
            "messages": lambda item: item.get("message_count") or 0,
            "chars": lambda item: (item.get("part_chars") or 0)
            + (item.get("message_chars") or 0),
            "title": lambda item: str(item.get("title", "")).lower(),
        }
        stats.sort(key=sorters.get(sort, sorters["updated"]), reverse=sort != "title")
        total = len(stats)
        return stats[offset : offset + limit], total

    def get_session(self, session_id: str) -> Optional[dict]:
        for item in self.session_stats():
            if item["id"] == session_id:
                result = dict(item)
                break
        else:
            return None

        con = self.connect()
        try:
            if self.json1_available():
                part_types = []
                for row in con.execute(
                    """
                    SELECT CASE WHEN json_valid(data) THEN json_extract(data, '$.type')
                                ELSE 'invalid-json' END AS type,
                           COUNT(*) AS count,
                           COALESCE(SUM(LENGTH(data)), 0) AS chars
                    FROM part WHERE session_id = ?
                    GROUP BY type ORDER BY chars DESC
                    """,
                    (session_id,),
                ):
                    part_types.append(dict(row))
            else:
                counts: dict[str, dict] = {}
                for row in con.execute(
                    "SELECT data FROM part WHERE session_id = ?", (session_id,)
                ):
                    raw = row["data"] or ""
                    parsed = safe_json(raw)
                    part_type = (
                        parsed.get("type")
                        if isinstance(parsed, dict)
                        else "invalid-json"
                    )
                    entry = counts.setdefault(
                        part_type or "unknown",
                        {"type": part_type or "unknown", "count": 0, "chars": 0},
                    )
                    entry["count"] += 1
                    entry["chars"] += len(raw)
                part_types = sorted(
                    counts.values(), key=lambda item: item["chars"], reverse=True
                )
            result["part_types"] = part_types

            row = con.execute(
                """
                SELECT MIN(time_created) AS first_message_at,
                       MAX(time_created) AS last_message_at
                FROM message WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            result["first_message_at"] = row["first_message_at"] if row else None
            result["last_message_at"] = row["last_message_at"] if row else None
        finally:
            con.close()
        return result

    # -- preview ----------------------------------------------------------
    def get_messages(
        self,
        session_id: str,
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 8000,
    ) -> dict:
        limit = max(1, min(limit, 100))
        max_chars = max(0, min(max_chars, 200_000))
        con = self.connect()
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT id, sequence, time_created, time_updated, data
                FROM message WHERE session_id = ?
                ORDER BY sequence, id LIMIT ? OFFSET ?
                """,
                (session_id, limit, offset),
            ).fetchall()

            message_ids = [row["id"] for row in rows]
            parts: dict[str, list[dict]] = {message_id: [] for message_id in message_ids}
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                for part in con.execute(
                    f"""
                    SELECT id, message_id, sequence, time_created, data
                    FROM part WHERE message_id IN ({placeholders})
                    ORDER BY sequence, id
                    """,
                    message_ids,
                ):
                    parts.setdefault(part["message_id"], []).append(
                        preview_part(part, max_chars)
                    )

            messages = []
            for row in rows:
                data = safe_json(row["data"]) or {}
                messages.append(
                    {
                        "id": row["id"],
                        "sequence": row["sequence"],
                        "time_created": row["time_created"],
                        "time_updated": row["time_updated"],
                        "role": data.get("role") or "unknown",
                        "modelID": data.get("modelID"),
                        "providerID": data.get("providerID"),
                        "variant": data.get("variant"),
                        "mode": data.get("mode"),
                        "agent": data.get("agent"),
                        "tokens": data.get("tokens"),
                        "finish": data.get("finish"),
                        "summary": preview_summary(data.get("summary"), max_chars),
                        "parts": parts.get(row["id"], []),
                    }
                )
        finally:
            con.close()

        return {
            "messages": messages,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(messages) < total,
        }

    def get_part(self, part_id: str, max_chars: int = 2_000_000) -> Optional[dict]:
        max_chars = max(0, min(max_chars, 5_000_000))
        con = self.connect()
        try:
            row = con.execute(
                "SELECT id, message_id, sequence, time_created, data FROM part WHERE id = ?",
                (part_id,),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return preview_part(row, max_chars)


def preview_summary(summary: Any, max_chars: int) -> Optional[dict]:
    if not isinstance(summary, dict):
        return None
    body = str(summary.get("body") or "")
    text, truncated, full_len = truncate(body, max_chars)
    return {
        "title": summary.get("title") or "Summary",
        "body": text,
        "truncated": truncated,
        "full_length": full_len,
    }


def preview_part(row: sqlite3.Row, max_chars: int) -> dict:
    raw = row["data"]
    data = safe_json(raw)
    base = {
        "id": row["id"],
        "message_id": row["message_id"],
        "sequence": row["sequence"],
        "time_created": row["time_created"],
    }
    if not isinstance(data, dict):
        text, truncated, full_len = truncate(str(raw or ""), max_chars)
        return {
            **base,
            "type": "raw",
            "text": text,
            "truncated": truncated,
            "full_length": full_len,
        }

    part_type = data.get("type") or "unknown"
    if part_type in {"text", "reasoning"}:
        text, truncated, full_len = truncate(str(data.get("text") or ""), max_chars)
        return {
            **base,
            "type": part_type,
            "text": text,
            "truncated": truncated,
            "full_length": full_len,
        }

    if part_type == "tool":
        state = data.get("state") if isinstance(data.get("state"), dict) else {}
        input_text = pretty(state.get("input"), max_chars)
        output = state.get("output")
        output_text = pretty(output, max_chars)
        error_text = str(state.get("error") or "")
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        return {
            **base,
            "type": "tool",
            "tool": data.get("tool") or "unknown",
            "callID": data.get("callID"),
            "status": state.get("status"),
            "title": state.get("title"),
            "input": input_text,
            "output": output_text,
            "error": error_text,
            "metadata_keys": sorted(metadata.keys()),
            "truncated": bool(
                max_chars > 0
                and (len(input_text) >= max_chars or len(output_text) >= max_chars)
            ),
        }

    if part_type == "file":
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        preview = ""
        if isinstance(metadata.get("preview"), dict):
            preview = str(metadata["preview"].get("text") or "")
        preview, truncated, full_len = truncate(preview, max_chars)
        return {
            **base,
            "type": "file",
            "filename": data.get("filename") or "",
            "mime": data.get("mime") or "",
            "url": data.get("url") or "",
            "sizeBytes": metadata.get("sizeBytes"),
            "preview": preview,
            "truncated": truncated,
            "full_length": full_len,
        }

    if part_type == "timeline":
        return {
            **base,
            "type": "timeline",
            "timelineType": data.get("timelineType"),
            "from": (data.get("fromModel") or {}).get("label"),
            "to": (data.get("toModel") or {}).get("label"),
            "status": data.get("status"),
        }

    if part_type == "compaction":
        return {
            **base,
            "type": "compaction",
            "trigger": data.get("trigger"),
            "phase": data.get("phase"),
            "compactReason": data.get("compactReason"),
            "preCompactTokenCount": data.get("preCompactTokenCount"),
            "status": data.get("timelineStatus"),
        }

    if part_type == "step-finish":
        return {
            **base,
            "type": "step-finish",
            "reason": data.get("reason"),
            "tokens": data.get("tokens"),
        }

    raw_text = pretty(data, max_chars)
    return {
        **base,
        "type": part_type,
        "raw": raw_text,
    }


# --------------------------------------------------------------------------- export

@dataclass
class ExportOptions:
    source_id: str
    session_ids: list[str]
    output_dir: str
    format: str = "readable"
    include_reasoning: bool = True
    include_tool_input: bool = True
    include_tool_output: bool = True
    include_tool_metadata: bool = False
    include_file_preview: bool = True
    include_step_events: bool = False
    include_timeline: bool = True
    include_compaction: bool = True
    include_summary: bool = True
    include_message_meta: bool = True
    include_binary: bool = False
    split_mb: int = 100
    utf8_bom: bool = False
    max_tool_output_chars: int = 0
    user_label: str = "USER"
    agent_label: str = "ASSISTANT"

    @classmethod
    def from_dict(cls, data: dict) -> "ExportOptions":
        return cls(
            source_id=str(data.get("source", "zcode")),
            session_ids=[str(item) for item in (data.get("session_ids") or [])],
            output_dir=str(data.get("output_dir") or ""),
            format=str(data.get("format") or "readable"),
            include_reasoning=bool(data.get("include_reasoning", True)),
            include_tool_input=bool(data.get("include_tool_input", True)),
            include_tool_output=bool(data.get("include_tool_output", True)),
            include_tool_metadata=bool(data.get("include_tool_metadata", False)),
            include_file_preview=bool(data.get("include_file_preview", True)),
            include_step_events=bool(data.get("include_step_events", False)),
            include_timeline=bool(data.get("include_timeline", True)),
            include_compaction=bool(data.get("include_compaction", True)),
            include_summary=bool(data.get("include_summary", True)),
            include_message_meta=bool(data.get("include_message_meta", True)),
            include_binary=bool(data.get("include_binary", False)),
            split_mb=max(0, int(data.get("split_mb") or 0)),
            utf8_bom=bool(data.get("utf8_bom", False)),
            max_tool_output_chars=max(0, int(data.get("max_tool_output_chars") or 0)),
            user_label=str(data.get("user_label") or "USER").strip() or "USER",
            agent_label=str(data.get("agent_label") or "ASSISTANT").strip() or "ASSISTANT",
        )


class RotatingWriter:
    def __init__(self, base_path: Path, extension: str, split_bytes: int, bom: bool):
        self.base_path = base_path
        self.extension = extension
        self.split_bytes = max(0, split_bytes)
        self.bom = bom
        self.index = 0
        self.fp = None
        self.current_path: Optional[Path] = None
        self.current_size = 0
        self.total_bytes = 0
        self.files: list[Path] = []
        self.open_next()

    def open_next(self) -> None:
        if self.fp:
            self.fp.close()
        self.index += 1
        if self.split_bytes:
            name = f"{self.base_path.name}.part{self.index:03d}{self.extension}"
        else:
            name = f"{self.base_path.name}{self.extension}"
        self.current_path = self.base_path.with_name(name)
        self.fp = open(self.current_path, "wb")
        self.current_size = 0
        self.files.append(self.current_path)
        if self.bom:
            self.fp.write(b"\xef\xbb\xbf")
            self.current_size += 3
            self.total_bytes += 3

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        if (
            self.split_bytes
            and self.current_size > 0
            and self.current_size + len(data) > self.split_bytes
        ):
            self.open_next()
        self.fp.write(data)
        self.current_size += len(data)
        self.total_bytes += len(data)

    def close(self) -> None:
        if self.fp:
            self.fp.close()
            self.fp = None

    def finalize(self) -> None:
        """Close and collapse a single split part back to the plain filename."""
        self.close()
        if self.split_bytes and len(self.files) == 1 and self.current_path:
            plain = self.base_path.with_name(f"{self.base_path.name}{self.extension}")
            if self.current_path != plain and not plain.exists():
                try:
                    self.current_path.rename(plain)
                    self.files[0] = plain
                    self.current_path = plain
                except OSError:
                    pass


@dataclass
class ExportJob:
    id: str
    options: ExportOptions
    status: str = "queued"
    created_at: str = field(default_factory=now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    output_dir: Optional[str] = None
    download_path: Optional[str] = None
    files: list[str] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "output_dir": self.output_dir,
            "download_path": self.download_path,
            "files": self.files,
            "progress": self.progress,
            "options": {
                "source": self.options.source_id,
                "format": self.options.format,
                "session_count": len(self.options.session_ids),
                "split_mb": self.options.split_mb,
            },
        }


class Exporter:
    def __init__(self, sources: dict[str, Source], default_output_dir: Path):
        self.sources = sources
        self.default_output_dir = Path(default_output_dir)
        self.jobs: dict[str, ExportJob] = {}
        self._lock = threading.Lock()

    def start(self, options: ExportOptions) -> ExportJob:
        if options.format not in {"readable", "raw"}:
            raise ValueError("format must be readable or raw")
        if options.source_id not in self.sources:
            raise ValueError(f"unknown source: {options.source_id}")
        source = self.sources[options.source_id]
        if not source.db_path.exists():
            raise ValueError(f"database not found: {source.db_path}")

        if not options.output_dir:
            options.output_dir = str(self.default_output_dir)
        output_root = Path(options.output_dir).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        if not options.session_ids:
            options.session_ids = [item["id"] for item in source.session_stats()]
        if not options.session_ids:
            raise ValueError("no sessions selected")

        job = ExportJob(id=f"job_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}", options=options)
        job.progress = {
            "session_index": 0,
            "session_total": len(options.session_ids),
            "message_done": 0,
            "message_total": 0,
            "bytes_written": 0,
            "current_file": None,
            "current_session": None,
        }
        with self._lock:
            self.jobs[job.id] = job

        thread = threading.Thread(target=self._run, args=(job, source), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> Optional[ExportJob]:
        with self._lock:
            return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.cancel_event.set()
        return True

    def preview(
        self,
        options: ExportOptions,
        session_id: str,
        limit_messages: int = 30,
        max_chars: int = 200_000,
        offset_messages: int = 0,
    ) -> dict:
        """Render one page of messages with the real export formatter."""
        source = self.sources.get(options.source_id)
        if not source:
            raise ValueError(f"unknown source: {options.source_id}")
        session = source.get_session(session_id)
        if not session:
            raise ValueError("session not found")

        limit_messages = max(1, min(int(limit_messages or 30), 200))
        max_chars = max(1000, min(int(max_chars or 200_000), 1_000_000))
        offset_messages = max(0, int(offset_messages or 0))

        preview_options = copy(options)
        preview_options.include_binary = False
        if preview_options.max_tool_output_chars <= 0:
            preview_options.max_tool_output_chars = 4000
        else:
            preview_options.max_tool_output_chars = min(
                preview_options.max_tool_output_chars, 4000
            )

        con = source.connect()
        try:
            total = con.execute(
                "SELECT COUNT(*) FROM message WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT m.id AS m_id, m.sequence AS m_seq,
                       m.time_created AS m_time, m.data AS m_data,
                       p.id AS p_id, p.sequence AS p_seq,
                       p.time_created AS p_time, p.data AS p_data
                FROM (
                    SELECT id, sequence, time_created, data
                    FROM message
                    WHERE session_id = ?
                    ORDER BY sequence, id
                    LIMIT ? OFFSET ?
                ) m
                LEFT JOIN part p ON p.message_id = m.id
                ORDER BY m.sequence, m.id, p.sequence, p.id
                """,
                (session_id, limit_messages, offset_messages),
            )

            chunks: list[str] = []
            if offset_messages == 0:
                if preview_options.format == "readable":
                    chunks.append(format_session_header(session, source.label))
                else:
                    chunks.append(
                        json.dumps(
                            {"type": "session", "session": session_meta(session, source.label)},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

            current_id = None
            current_seq = None
            current_message = None
            current_parts: list[dict] = []
            shown = 0

            def flush() -> None:
                nonlocal shown
                if current_message is None:
                    return
                message_data = safe_json(current_message) or {}
                if preview_options.format == "readable":
                    chunks.append(
                        format_message_readable(
                            current_message,
                            current_parts,
                            preview_options,
                            sequence=current_seq,
                        )
                    )
                else:
                    part_data = []
                    for part_meta in current_parts:
                        parsed = safe_json(part_meta["data"])
                        part_data.append(
                            {
                                "id": part_meta["id"],
                                "sequence": part_meta["sequence"],
                                "time_created": part_meta["time_created"],
                                "data": parsed
                                if parsed is not None
                                else {"type": "raw", "text": part_meta["data"]},
                            }
                        )
                    chunks.append(
                        json.dumps(
                            {
                                "type": "message",
                                "session": session_meta(session, source.label),
                                "sequence": current_seq,
                                "message": strip_binary(message_data),
                                "parts": [strip_binary(item) for item in part_data],
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                shown += 1

            for row in rows:
                if row["m_id"] != current_id:
                    if current_id is not None:
                        flush()
                        if shown >= limit_messages:
                            break
                    current_id = row["m_id"]
                    current_seq = row["m_seq"]
                    current_message = row["m_data"]
                    current_parts = []
                if row["p_id"]:
                    current_parts.append(
                        {
                            "id": row["p_id"],
                            "sequence": row["p_seq"],
                            "time_created": row["p_time"],
                            "data": row["p_data"],
                        }
                    )
            if current_id is not None and shown < limit_messages:
                flush()
        finally:
            con.close()

        text = "".join(chunks)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + "\n... [preview truncated]"
        pages = max(1, (total + limit_messages - 1) // limit_messages)
        page = min(pages, offset_messages // limit_messages + 1)
        return {
            "text": text,
            "shown_messages": shown,
            "total_messages": total,
            "offset_messages": offset_messages,
            "limit_messages": limit_messages,
            "page": page,
            "pages": pages,
            "has_more": offset_messages + shown < total,
            "truncated": truncated,
            "format": preview_options.format,
            "bytes": len(text.encode("utf-8")),
        }

    def _run(self, job: ExportJob, source: Source) -> None:
        job.status = "running"
        job.started_at = now_iso()
        out_dir = Path(job.options.output_dir) / job.id
        out_dir.mkdir(parents=True, exist_ok=True)
        job.output_dir = str(out_dir)

        try:
            counts = {}
            for session_id in job.options.session_ids:
                con = source.connect()
                try:
                    counts[session_id] = con.execute(
                        "SELECT COUNT(*) FROM message WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                finally:
                    con.close()
            total_messages = sum(counts.values())
            job.progress["message_total"] = total_messages

            manifest_sessions = []
            done_messages = 0
            started = time.time()

            for session_index, session_id in enumerate(job.options.session_ids, 1):
                if job.cancel_event.is_set():
                    break

                session = source.get_session(session_id)
                if not session:
                    manifest_sessions.append({"id": session_id, "error": "session not found"})
                    continue

                job.progress["session_index"] = session_index
                job.progress["current_session"] = session.get("title")

                base = sanitize_filename(
                    f"{session.get('title') or 'untitled'}_{session_id[-8:]}",
                    fallback=session_id,
                )
                extension = ".txt" if job.options.format == "readable" else ".jsonl"
                writer = RotatingWriter(
                    out_dir / base,
                    extension,
                    job.options.split_mb * 1024 * 1024,
                    job.options.utf8_bom,
                )

                if job.options.format == "readable":
                    writer.write(format_session_header(session, source.label))
                else:
                    writer.write(
                        json.dumps(
                            {"type": "session", "session": session_meta(session, source.label)},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

                con = source.connect()
                try:
                    rows = con.execute(
                        """
                        SELECT m.id AS m_id, m.sequence AS m_seq,
                               m.time_created AS m_time, m.data AS m_data,
                               p.id AS p_id, p.sequence AS p_seq,
                               p.time_created AS p_time, p.data AS p_data
                        FROM message m
                        LEFT JOIN part p ON p.message_id = m.id
                        WHERE m.session_id = ?
                        ORDER BY m.sequence, m.id, p.sequence, p.id
                        """,
                        (session_id,),
                    )
                    current_id = None
                    current_seq = None
                    current_message = None
                    current_parts: list[dict] = []

                    def flush() -> None:
                        nonlocal current_id, current_seq, current_message, current_parts, done_messages
                        if current_message is None:
                            return
                        message_data = safe_json(current_message) or {}
                        part_data = []
                        for part_meta in current_parts:
                            parsed = safe_json(part_meta["data"])
                            part_data.append(
                                {
                                    "id": part_meta["id"],
                                    "sequence": part_meta["sequence"],
                                    "time_created": part_meta["time_created"],
                                    "data": parsed if parsed is not None else {"type": "raw", "text": part_meta["data"]},
                                }
                            )
                        if job.options.format == "readable":
                            writer.write(
                                format_message_readable(
                                    current_message,
                                    current_parts,
                                    job.options,
                                    sequence=current_seq,
                                )
                            )
                        else:
                            writer.write(
                                json.dumps(
                                    {
                                        "type": "message",
                                        "session": session_meta(session, source.label),
                                        "sequence": current_seq,
                                        "message": (
                                            message_data
                                            if job.options.include_binary
                                            else strip_binary(message_data)
                                        ),
                                        "parts": (
                                            part_data
                                            if job.options.include_binary
                                            else [strip_binary(item) for item in part_data]
                                        ),
                                    },
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            )
                        done_messages += 1
                        job.progress["message_done"] = done_messages
                        job.progress["bytes_written"] = writer.total_bytes
                        job.progress["current_file"] = (
                            writer.current_path.name if writer.current_path else None
                        )

                    for row in rows:
                        if job.cancel_event.is_set():
                            break
                        if row["m_id"] != current_id:
                            flush()
                            current_id = row["m_id"]
                            current_seq = row["m_seq"]
                            current_message = row["m_data"]
                            current_parts = []
                        if row["p_id"]:
                            current_parts.append(
                                {
                                    "id": row["p_id"],
                                    "sequence": row["p_seq"],
                                    "time_created": row["p_time"],
                                    "data": row["p_data"],
                                }
                            )
                    flush()
                finally:
                    con.close()

                writer.finalize()
                manifest_sessions.append(
                    {
                        "id": session_id,
                        "title": session.get("title"),
                        "directory": session.get("directory"),
                        "messages": counts.get(session_id, 0),
                        "files": [str(path) for path in writer.files],
                        "bytes": writer.total_bytes,
                    }
                )
                job.files.extend(str(path) for path in writer.files)

            if job.cancel_event.is_set():
                final_status = "cancelled"
            else:
                final_status = "done"

            manifest = {
                "job_id": job.id,
                "created_at": job.created_at,
                "finished_at": now_iso(),
                "source": job.options.source_id,
                "format": job.options.format,
                "options": job.options.__dict__,
                "sessions": manifest_sessions,
                "files": job.files,
                "duration_seconds": round(time.time() - started, 2),
            }
            manifest_path = out_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if final_status == "done":
                if len(job.files) == 1:
                    job.download_path = job.files[0]
                elif len(job.files) > 1:
                    zip_path = out_dir / "export.zip"
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                        for file_path in job.files:
                            archive.write(file_path, arcname=Path(file_path).name)
                    job.download_path = str(zip_path)
            job.status = final_status
            job.finished_at = now_iso()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            job.status = "error"
            job.error = str(exc)
            job.finished_at = now_iso()


def session_meta(session: dict, source_label: str) -> dict:
    return {
        "id": session.get("id"),
        "title": session.get("title"),
        "directory": session.get("directory"),
        "task_type": session.get("task_type"),
        "parent_id": session.get("parent_id"),
        "time_created": session.get("time_created"),
        "time_updated": session.get("time_updated"),
        "source": source_label,
    }


def format_session_header(session: dict, source_label: str) -> str:
    return (
        "=" * 80
        + "\n"
        + f"SESSION: {session.get('title') or '(untitled)'}\n"
        + f"ID: {session.get('id')}\n"
        + f"Source: {source_label}\n"
        + f"Workspace: {session.get('directory') or '-'}\n"
        + f"Task type: {session.get('task_type') or '-'}\n"
        + f"Created: {fmt_time(session.get('time_created'))}\n"
        + f"Updated: {fmt_time(session.get('time_updated'))}\n"
        + f"Messages: {session.get('message_count')}\n"
        + f"Parts: {session.get('part_count')}\n"
        + f"Chars: {session.get('part_chars')}\n"
        + "=" * 80
        + "\n\n"
    )


def format_message_readable(
    message_raw: str,
    parts: Iterable[dict],
    options: ExportOptions,
    sequence: Optional[int] = None,
) -> str:
    data = safe_json(message_raw) or {}
    raw_role = str(data.get("role") or "unknown")
    if raw_role == "user":
        role = options.user_label
    elif raw_role == "assistant":
        role = options.agent_label
    else:
        role = raw_role.upper()
    time_value = (data.get("time") or {}).get("created")
    seq_label = f"{int(sequence):06d}" if isinstance(sequence, int) else "------"
    lines = [f"[{seq_label}] {role}  {fmt_time(time_value)}", "-" * 80]

    meta = []
    if options.include_message_meta:
        if data.get("modelID"):
            meta.append(f"model={data.get('providerID')}/{data.get('modelID')}")
        if data.get("mode"):
            meta.append(f"mode={data.get('mode')}")
        if data.get("agent"):
            meta.append(f"agent={data.get('agent')}")
        tokens = data.get("tokens") or {}
        if tokens:
            meta.append(
                "tokens="
                f"in:{tokens.get('input', 0)} "
                f"out:{tokens.get('output', 0)} "
                f"reasoning:{tokens.get('reasoning', 0)}"
            )
        if data.get("finish"):
            meta.append(f"finish={data.get('finish')}")
    if meta:
        lines.append("[meta] " + " | ".join(meta))

    summary = data.get("summary")
    if options.include_summary and isinstance(summary, dict):
        lines.append(f"[summary] {summary.get('title') or 'Summary'}")
        body = str(summary.get("body") or "")
        if body:
            lines.append(body)

    for part_meta in parts:
        raw = part_meta.get("data") if isinstance(part_meta, dict) else part_meta
        part = safe_json(raw)
        if not isinstance(part, dict):
            lines.append(f"[part:raw]\n{raw}")
            continue
        part_type = part.get("type") or "unknown"

        if part_type == "text":
            text = str(part.get("text") or "")
            if text:
                lines.append(text)
        elif part_type == "reasoning":
            if options.include_reasoning:
                text = str(part.get("text") or "")
                if text:
                    lines.append("[reasoning]")
                    lines.append(text)
                    lines.append("[/reasoning]")
        elif part_type == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            lines.append(
                f"[tool] {part.get('tool') or 'unknown'} "
                f"({part.get('callID') or '-'}) status={state.get('status') or '-'}"
            )
            if options.include_tool_input:
                input_text = pretty(state.get("input"))
                if input_text:
                    lines.append("input:")
                    lines.append(input_text)
            if options.include_tool_output:
                output_text = pretty(state.get("output"), options.max_tool_output_chars)
                if output_text:
                    lines.append("output:")
                    lines.append(output_text)
            if state.get("error"):
                lines.append("error:")
                lines.append(str(state.get("error")))
            if options.include_tool_metadata and state.get("metadata"):
                lines.append("metadata:")
                lines.append(pretty(state.get("metadata")))
        elif part_type == "file":
            metadata = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
            lines.append(
                f"[file] {part.get('filename') or '-'} ({part.get('mime') or '-'})"
            )
            if part.get("url"):
                lines.append(f"path: {part.get('url')}")
            if options.include_file_preview:
                preview = ""
                if isinstance(metadata.get("preview"), dict):
                    preview = str(metadata["preview"].get("text") or "")
                if preview:
                    lines.append("preview:")
                    lines.append(preview)
        elif part_type == "timeline":
            if options.include_timeline:
                from_label = (part.get("fromModel") or {}).get("label")
                to_label = (part.get("toModel") or {}).get("label")
                lines.append(
                    f"[timeline] {part.get('timelineType') or 'event'}: "
                    f"{from_label or '-'} -> {to_label or '-'}"
                )
        elif part_type == "compaction":
            if options.include_compaction:
                lines.append(
                    "[compaction] "
                    f"trigger={part.get('trigger') or '-'} "
                    f"reason={part.get('compactReason') or '-'} "
                    f"phase={part.get('phase') or '-'} "
                    f"pre_tokens={part.get('preCompactTokenCount') or '-'} "
                    f"status={part.get('timelineStatus') or '-'}"
                )
        elif part_type == "step-finish":
            if options.include_step_events:
                lines.append(
                    f"[step] finish reason={part.get('reason') or '-'} "
                    f"tokens={pretty(part.get('tokens') or {})}"
                )
        elif part_type == "step-start":
            if options.include_step_events:
                lines.append("[step] start")
        else:
            lines.append(f"[part:{part_type}]")
            lines.append(pretty(part, options.max_tool_output_chars))

    return "\n".join(lines).rstrip() + "\n\n"
