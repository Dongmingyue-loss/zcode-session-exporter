#!/usr/bin/env python3
"""Automated tests for the ZCode session exporter."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from exporter import ExportOptions, Exporter, Source  # noqa: E402


def make_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            title TEXT,
            directory TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            task_type TEXT,
            parent_id TEXT,
            slug TEXT
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY,
            message_id TEXT,
            session_id TEXT,
            time_created INTEGER,
            time_updated INTEGER,
            data TEXT,
            sequence INTEGER
        );
        """
    )

    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
        ("sess_test", "测试会话", r"C:\tmp", 1000, 2000, "interactive", None, "test"),
    )
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?)",
        ("sess_split", "拆分测试", r"C:\tmp", 3000, 4000, "interactive", None, "split"),
    )

    assistant = {
        "role": "assistant",
        "time": {"created": 1000, "completed": 1100},
        "modelID": "model-x",
        "providerID": "provider-x",
        "mode": "plan",
        "agent": "zcode-agent",
        "tokens": {"input": 10, "output": 20, "reasoning": 5},
        "finish": "stop",
    }
    user = {"role": "user", "time": {"created": 1200}, "agent": "zcode-agent"}

    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?,?)",
        ("m0", "sess_test", 1000, 1000, json.dumps(assistant, ensure_ascii=False), 0),
    )
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?,?)",
        ("m1", "sess_test", 1200, 1200, json.dumps(user, ensure_ascii=False), 1),
    )

    parts = [
        ("p0", "m0", 0, {"type": "text", "text": "你好，这是助手回复。"}),
        ("p1", "m0", 1, {"type": "reasoning", "text": "这是推理过程。"}),
        (
            "p2",
            "m0",
            2,
            {
                "type": "tool",
                "tool": "Bash",
                "callID": "call_1",
                "state": {
                    "status": "completed",
                    "input": {"cmd": "echo hi"},
                    "output": "hi\n",
                    "metadata": {"base64": "QUJD"},
                },
            },
        ),
        (
            "p3",
            "m0",
            3,
            {
                "type": "file",
                "filename": "a.txt",
                "mime": "text/plain",
                "url": r"C:\a.txt",
                "metadata": {"preview": {"text": "file preview"}, "sizeBytes": 12},
            },
        ),
        (
            "p4",
            "m0",
            4,
            {
                "type": "timeline",
                "timelineType": "model_change",
                "fromModel": {"label": "p/old"},
                "toModel": {"label": "p/new"},
                "status": "completed",
            },
        ),
        (
            "p5",
            "m0",
            5,
            {
                "type": "compaction",
                "trigger": "auto",
                "compactReason": "context_limit",
                "phase": "mid_turn",
                "preCompactTokenCount": 1000,
                "timelineStatus": "completed",
            },
        ),
        ("p6", "m0", 6, {"type": "step-start"}),
        (
            "p7",
            "m0",
            7,
            {
                "type": "step-finish",
                "reason": "stop",
                "tokens": {"input": 10, "output": 20},
            },
        ),
        ("p8", "m1", 0, {"type": "text", "text": "继续"}),
    ]
    for part_id, message_id, sequence, data in parts:
        con.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (
                part_id,
                message_id,
                "sess_test",
                1000 + sequence,
                1000 + sequence,
                json.dumps(data, ensure_ascii=False),
                sequence,
            ),
        )

    # Several large messages so the splitter can rotate at message boundaries.
    for index in range(8):
        message_id = f"sm{index}"
        con.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?)",
            (
                message_id,
                "sess_split",
                3000 + index,
                3000 + index,
                json.dumps(assistant, ensure_ascii=False),
                index,
            ),
        )
        con.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (
                f"sp{index}",
                message_id,
                "sess_split",
                3000 + index,
                3000 + index,
                json.dumps({"type": "text", "text": "x" * 500_000}, ensure_ascii=False),
                0,
            ),
        )

    con.commit()
    con.close()


def wait_job(job, timeout: float = 60.0):
    deadline = time.time() + timeout
    while job.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
    if job.status in {"queued", "running"}:
        raise TimeoutError("export job did not finish")
    if job.status == "error":
        raise RuntimeError(job.error)
    return job


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zcode-exporter-test-")
        self.root = Path(self.temp.name)
        self.db = self.root / "db.sqlite"
        make_fixture(self.db)
        self.source = Source("zcode", "ZCode", self.db)
        self.exporter = Exporter({"zcode": self.source}, self.root / "out")

    def tearDown(self):
        self.temp.cleanup()

    def export(self, session_ids, **overrides):
        options = ExportOptions(
            source_id="zcode",
            session_ids=session_ids,
            output_dir=str(self.root / "out"),
            **overrides,
        )
        job = self.exporter.start(options)
        wait_job(job)
        return job

    def test_stats_and_search(self):
        stats = self.source.session_stats()
        self.assertEqual(len(stats), 2)
        item = next(row for row in stats if row["id"] == "sess_test")
        self.assertEqual(item["message_count"], 2)
        self.assertEqual(item["part_count"], 9)
        self.assertEqual(item["tool_count"], 1)
        self.assertEqual(item["reasoning_count"], 1)
        sessions, total = self.source.list_sessions("测试会话")
        self.assertEqual(total, 1)
        self.assertEqual(sessions[0]["id"], "sess_test")

    def test_preview(self):
        result = self.source.get_messages("sess_test", 0, 10, 100)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["messages"]), 2)
        types = [part["type"] for part in result["messages"][0]["parts"]]
        self.assertIn("text", types)
        self.assertIn("tool", types)
        part = self.source.get_part("p2")
        self.assertIn("hi", part["output"])

    def test_readable_export(self):
        job = self.export(
            ["sess_test"],
            format="readable",
            split_mb=0,
            include_step_events=True,
        )
        self.assertEqual(len(job.files), 1)
        text = Path(job.files[0]).read_text(encoding="utf-8")
        self.assertIn("SESSION: 测试会话", text)
        self.assertIn("[000000] ASSISTANT", text)
        self.assertIn("[reasoning]", text)
        self.assertIn("[tool] Bash", text)
        self.assertIn("output:\nhi", text)
        self.assertIn("[file] a.txt", text)
        self.assertIn("[timeline] model_change", text)
        self.assertIn("[compaction]", text)
        self.assertIn("[step] start", text)
        self.assertIn("[step] finish", text)
        self.assertIn("继续", text)
        self.assertNotIn("QUJD", text)
        self.assertFalse(Path(job.files[0]).name.endswith(".part001.txt"))

    def test_raw_export_strips_binary(self):
        job = self.export(["sess_test"], format="raw", split_mb=0, include_binary=False)
        lines = Path(job.files[0]).read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(first["type"], "session")
        self.assertEqual(second["type"], "message")
        self.assertEqual(second["sequence"], 0)
        self.assertTrue(second["parts"][0]["id"])
        self.assertIn("data", second["parts"][0])
        self.assertNotIn("QUJD", "\n".join(lines))

    def test_split_export(self):
        job = self.export(["sess_split"], format="readable", split_mb=1)
        self.assertGreater(len(job.files), 1)
        total = sum(Path(path).stat().st_size for path in job.files)
        self.assertGreater(total, 3_000_000)
        for path in job.files:
            self.assertTrue(Path(path).name.endswith(".txt"))

    def test_preview_export(self):
        options = ExportOptions(
            source_id="zcode",
            session_ids=["sess_test"],
            output_dir=str(self.root / "out"),
            format="readable",
            include_step_events=True,
        )
        result = self.exporter.preview(options, "sess_test", limit_messages=1, max_chars=50_000)
        self.assertEqual(result["shown_messages"], 1)
        self.assertEqual(result["total_messages"], 2)
        self.assertIn("SESSION: 测试会话", result["text"])
        self.assertIn("[000000] ASSISTANT", result["text"])
        self.assertNotIn("[000001] USER", result["text"])
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["pages"], 2)

        page2 = self.exporter.preview(
            options,
            "sess_test",
            limit_messages=1,
            max_chars=50_000,
            offset_messages=1,
        )
        self.assertEqual(page2["shown_messages"], 1)
        self.assertEqual(page2["page"], 2)
        self.assertEqual(page2["pages"], 2)
        self.assertFalse(page2["has_more"])
        self.assertIn("[000001] USER", page2["text"])
        self.assertNotIn("[000000] ASSISTANT", page2["text"])
        self.assertNotIn("SESSION:", page2["text"])

        raw_options = ExportOptions(
            source_id="zcode",
            session_ids=["sess_test"],
            output_dir=str(self.root / "out"),
            format="raw",
        )
        raw = self.exporter.preview(raw_options, "sess_test", limit_messages=1, max_chars=50_000)
        lines = raw["text"].splitlines()
        self.assertEqual(json.loads(lines[0])["type"], "session")
        self.assertEqual(json.loads(lines[1])["type"], "message")
        self.assertEqual(raw["shown_messages"], 1)

    def test_custom_role_labels(self):
        job = self.export(
            ["sess_test"],
            format="readable",
            split_mb=0,
            user_label="我",
            agent_label="助手",
        )
        text = Path(job.files[0]).read_text(encoding="utf-8")
        self.assertIn("[000000] 助手", text)
        self.assertIn("[000001] 我", text)
        self.assertNotIn("[000000] ASSISTANT", text)

        raw_job = self.export(
            ["sess_test"],
            format="raw",
            split_mb=0,
            user_label="我",
            agent_label="助手",
        )
        lines = Path(raw_job.files[0]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[1])["message"]["role"], "assistant")

        options = ExportOptions(
            source_id="zcode",
            session_ids=["sess_test"],
            output_dir=str(self.root / "out"),
            format="readable",
            user_label="我",
            agent_label="助手",
        )
        preview = self.exporter.preview(options, "sess_test", limit_messages=2, max_chars=50_000)
        self.assertIn("[000000] 助手", preview["text"])
        self.assertIn("[000001] 我", preview["text"])

    def test_json1_fallback(self):
        self.source._json1 = False
        stats = self.source.session_stats(force=True)
        item = next(row for row in stats if row["id"] == "sess_test")
        self.assertEqual(item["tool_count"], 1)
        self.assertEqual(item["reasoning_count"], 1)
        session = self.source.get_session("sess_test")
        types = {entry["type"]: entry["count"] for entry in session["part_types"]}
        self.assertEqual(types.get("tool"), 1)
        self.assertEqual(types.get("reasoning"), 1)

    def test_read_only(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.export(["sess_test"], format="readable", split_mb=0)
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_single_split_file_is_collapsed(self):
        job = self.export(["sess_test"], format="readable", split_mb=100)
        self.assertEqual(len(job.files), 1)
        self.assertFalse(Path(job.files[0]).name.endswith(".part001.txt"))


if __name__ == "__main__":
    unittest.main()
