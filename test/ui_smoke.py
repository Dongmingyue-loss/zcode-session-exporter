#!/usr/bin/env python3
"""Headless browser smoke test for the local web GUI.

Usage:
  python scripts/make_demo_db.py
  python test/ui_smoke.py

On Windows, the test uses the installed Edge browser unless
UI_BROWSER_CHANNEL is set. On macOS/Linux, it uses Playwright Chromium.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright is not installed. Run: python -m pip install -r requirements-dev.txt")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "testtoken"


def wait_for_health(base: str, timeout: float = 30.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(f"{base}/api/health", timeout=2) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server did not become healthy: {base}")


def main() -> int:
    demo = ROOT / "demo" / "demo.sqlite"
    if not demo.exists():
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "make_demo_db.py")])

    output_dir = Path(tempfile.mkdtemp(prefix="zcode-ui-smoke-"))
    artifact_dir = ROOT / "artifacts" / f"ui-smoke-{platform.system().lower()}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-u",
        str(ROOT / "app.py"),
        "--demo",
        "--no-open",
        "--token",
        TOKEN,
        "--port",
        "0",
        "--output-dir",
        str(output_dir),
    ]
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    server_lines: list[str] = []
    base = None
    full_url = None
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            server_lines.append(line.rstrip())
            if "URL:" in line:
                full_url = line.split("URL:", 1)[1].strip()
                base = full_url.split("?", 1)[0]
                break
        if not base or not full_url:
            raise RuntimeError("server did not print a URL:\n" + "\n".join(server_lines))
        wait_for_health(base)

        channel = os.environ.get("UI_BROWSER_CHANNEL")
        if not channel and sys.platform == "win32":
            channel = "msedge"

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                channel=channel or None,
            )
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.goto(full_url, wait_until="domcontentloaded")
            page.wait_for_selector(".source-tab", timeout=15000)
            page.wait_for_selector(".session-item", timeout=15000)
            page.screenshot(path=str(artifact_dir / "01-home.png"), full_page=True)

            page.locator(".session-item").filter(has_text="Demo: 长会话分页").click()
            page.wait_for_selector(".message", timeout=15000)
            page.screenshot(path=str(artifact_dir / "02-session.png"), full_page=True)

            page.get_by_role("button", name="预览导出").click()
            page.wait_for_selector("#preview-modal:not(.hidden)", timeout=15000)
            text = page.locator("#preview-text").text_content() or ""
            assert "SESSION:" in text, "preview is missing session header"
            page.locator("#preview-next").click()
            page.wait_for_function(
                "() => document.querySelector('#preview-page-info')?.textContent.includes('2 /')",
                timeout=15000,
            )
            page.screenshot(path=str(artifact_dir / "03-preview-page2.png"), full_page=True)

            page.locator("#preview-close").click()
            page.locator("#user-label").fill("我")
            page.locator("#agent-label").fill("助手")
            page.get_by_role("button", name="预览导出").click()
            page.wait_for_selector("#preview-modal:not(.hidden)", timeout=15000)
            text = page.locator("#preview-text").text_content() or ""
            assert "[000000] 我" in text, "custom user label is missing"
            assert "[000001] 助手" in text, "custom agent label is missing"
            page.screenshot(path=str(artifact_dir / "04-preview-labels.png"), full_page=True)

            page.locator("#preview-confirm").click()
            page.wait_for_selector("#job-card:not(.hidden)", timeout=15000)
            page.wait_for_function(
                "() => document.querySelector('#job-status')?.textContent.includes('已完成')",
                timeout=60000,
            )
            assert page.locator("#job-download").is_visible()
            page.screenshot(path=str(artifact_dir / "05-export-done.png"), full_page=True)

            browser.close()

        print("UI smoke test passed")
        print(f"screenshots: {artifact_dir}")
        return 0
    except Exception as exc:
        print(f"UI smoke test failed: {exc}")
        if server_lines:
            print("--- server output ---")
            print("\n".join(server_lines[-40:]))
        return 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
