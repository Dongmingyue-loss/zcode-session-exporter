#!/usr/bin/env python3
"""HTTP API smoke tests for the local web GUI."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import Handler, Server  # noqa: E402
from exporter import Exporter, Source  # noqa: E402
from test_exporter import make_fixture  # noqa: E402


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zcode-server-test-")
        self.root = Path(self.temp.name)
        self.db = self.root / "db.sqlite"
        make_fixture(self.db)
        self.source = Source("zcode", "ZCode", self.db)
        self.exporter = Exporter({"zcode": self.source}, self.root / "out")
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.service = SimpleNamespace(
            sources={"zcode": self.source},
            exporter=self.exporter,
            token="testtoken",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None, token="testtoken"):
        data = None
        headers = {}
        if token:
            headers["X-Auth-Token"] = token
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        with self.opener.open(request, timeout=15) as response:
            body = response.read()
            return response.status, response.headers, body

    def test_health_and_sources(self):
        status, _headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, _headers, body = self.request("/api/sources")
        self.assertEqual(status, 200)
        sources = json.loads(body)["sources"]
        self.assertEqual(sources[0]["id"], "zcode")
        self.assertTrue(sources[0]["exists"])

    def test_sessions_and_messages(self):
        status, _headers, body = self.request("/api/sessions?source=zcode")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 2)

        status, _headers, body = self.request(
            "/api/messages?source=zcode&id=sess_test&offset=0&limit=2"
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["messages"]), 2)

    def test_preview(self):
        status, _headers, body = self.request(
            "/api/preview",
            method="POST",
            payload={
                "source": "zcode",
                "session_id": "sess_test",
                "format": "readable",
                "preview_messages": 1,
                "preview_offset": 0,
                "preview_chars": 50000,
                "user_label": "我",
                "agent_label": "助手",
            },
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["shown_messages"], 1)
        self.assertIn("[000000] 助手", data["text"])

    def test_export_and_download(self):
        status, _headers, body = self.request(
            "/api/export",
            method="POST",
            payload={
                "source": "zcode",
                "session_ids": ["sess_test"],
                "format": "readable",
                "output_dir": str(self.root / "out"),
                "split_mb": 0,
                "utf8_bom": False,
            },
        )
        self.assertEqual(status, 200)
        job = json.loads(body)["job"]
        deadline = time.time() + 20
        while time.time() < deadline:
            status, _headers, body = self.request(f"/api/job?id={job['id']}")
            self.assertEqual(status, 200)
            current = json.loads(body)["job"]
            if current["status"] in {"done", "error", "cancelled"}:
                break
            time.sleep(0.05)
        self.assertEqual(current["status"], "done")
        self.assertTrue(current["download_path"])

        status, headers, body = self.request(
            f"/api/job/download?id={job['id']}&token=testtoken", token=None
        )
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn(b"SESSION: \xe6\xb5\x8b\xe8\xaf\x95\xe4\xbc\x9a\xe8\xaf\x9d", body)

    def test_static_assets(self):
        for path, marker in [
            ("/", "ZCode \u4f1a\u8bdd\u5bfc\u51fa\u5668"),
            ("/static/app.js", "previewExport"),
            ("/static/styles.css", "--cyan"),
        ]:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(marker.encode("utf-8"), body)


if __name__ == "__main__":
    unittest.main()
