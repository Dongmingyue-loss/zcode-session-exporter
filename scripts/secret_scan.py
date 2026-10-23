#!/usr/bin/env python3
"""Blocking secret scan for publishable files."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "dist", "exports", "artifacts", "__pycache__", "test/tmp"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".sqlite", ".db", ".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm", ".zip"}

PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b"),
]

ALLOW = [
    re.compile(r"^sk-test-"),
    re.compile(r"^sk-demo-"),
    re.compile(r"^test-token-"),
    re.compile(r"^\[REDACTED\]$"),
]


def collect_secrets() -> set[str]:
    secrets: set[str] = set()
    home = Path.home()
    candidates = [
        home / ".zcode" / "v2" / "config.json",
        home / ".zcode" / "cli" / "config.json",
        home / ".zcodium" / "v2" / "config.json",
        home / ".zcodium" / "cli" / "config.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, str) and re.search(
                        r"api[-_]?key|authorization|token|secret|password", key, re.I
                    ):
                        if len(child.strip()) >= 8:
                            secrets.add(child.strip())
                            bearer = re.match(r"^Bearer\s+(.+)$", child.strip(), re.I)
                            if bearer:
                                secrets.add(bearer.group(1).strip())
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(item, list):
                stack.extend(item)
    return secrets


def walk() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return files


def main() -> int:
    secrets = collect_secrets()
    findings = []
    for path in walk():
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        text = data.decode("utf-8", "replace")
        hits = 0
        for secret in secrets:
            if secret and secret in text:
                hits += 1
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                value = match.group(0)
                if not any(rule.search(value.replace("Bearer ", "", 1)) for rule in ALLOW):
                    hits += 1
        if hits:
            findings.append((path.relative_to(ROOT).as_posix(), hits))

    print(f"Scanned {len(walk())} files against {len(secrets)} local secret value(s).")
    if findings:
        print("BLOCKED: potential secret matches:")
        for path, count in findings:
            print(f"  {path}: {count}")
        return 1
    print("PASS: no secret values found in publishable files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
