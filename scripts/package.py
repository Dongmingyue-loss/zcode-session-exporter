#!/usr/bin/env python3
"""Build a publishable source zip for zcode-session-exporter."""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "1.0.0"
DIST = ROOT / "dist"
STAGING = DIST / f"zcode-session-exporter-v{VERSION}"
ZIP_PATH = DIST / f"zcode-session-exporter-v{VERSION}.zip"

ALLOWLIST = [
    "app.py",
    "exporter.py",
    "static",
    "test",
    "scripts",
    "demo",
    "docs",
    "start.cmd",
    "start.sh",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "requirements.txt",
    "requirements-dev.txt",
    ".gitignore",
    ".github",
]

FORBIDDEN_DIRS = {
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "exports",
    "artifacts",
    "test/tmp",
}


def is_forbidden(relative: Path) -> bool:
    text = relative.as_posix()
    if any(part in FORBIDDEN_DIRS for part in relative.parts):
        return True
    if text.startswith("test/tmp"):
        return True
    if relative.suffix in {".pyc", ".pyo", ".log", ".zip"}:
        return True
    if relative.suffix in {".sqlite", ".db", ".sqlite-wal", ".sqlite-shm", ".db-wal", ".db-shm"}:
        return text != "demo/demo.sqlite"
    return False


def copy_tree(source: Path, target: Path) -> None:
    if source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(ROOT)
        if is_forbidden(relative):
            continue
        destination = STAGING / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def add_file(archive: zipfile.ZipFile, path: Path, arcname: str) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname)
    mode = 0o755 if path.name == "start.sh" else 0o644
    info.external_attr = mode << 16
    with path.open("rb") as handle:
        archive.writestr(info, handle.read())


def main() -> None:
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)
    for item in ALLOWLIST:
        source = ROOT / item
        if not source.exists():
            raise FileNotFoundError(f"missing publishable item: {item}")
        copy_tree(source, STAGING / item)

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(STAGING.rglob("*")):
            if path.is_file():
                add_file(archive, path, path.relative_to(STAGING).as_posix())

    data = ZIP_PATH.read_bytes()
    print(f"Package: {ZIP_PATH}")
    print(f"Size: {len(data)} bytes")
    print(f"SHA256: {hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    main()
