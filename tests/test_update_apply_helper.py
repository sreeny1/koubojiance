# -*- coding: utf-8 -*-
"""Test the real PowerShell update helper: extract app zip, copy files, run restart target."""
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core import config, updater  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="update_helper_test_"))
    root = tmp / "root"
    app_dir = root / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "old.txt").write_text("old", encoding="utf-8")
    bat = root / "restart.bat"
    bat.write_text("@echo off\r\n> \"%~dp0restart_marker.txt\" echo restarted\r\n", encoding="utf-8")

    update_dir = root / "data" / "updates"
    update_dir.mkdir(parents=True)
    pkg = update_dir / "app.zip"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/old.txt", "new")
        zf.writestr("app/new.txt", "new-file")
    digest = updater.sha256_file(pkg)
    pending = update_dir / "pending.json"
    pending.write_text(json.dumps({
        "version": "9.9.9", "zip": str(pkg), "sha256": digest
    }), encoding="utf-8")

    with patch.multiple(
        config,
        BASE_DIR=root,
        UPDATE_DIR=update_dir,
        UPDATE_STAGING_DIR=update_dir / "staging",
        UPDATE_PENDING_PATH=pending,
        UPDATE_APPLIED_PATH=update_dir / "applied.json",
        UPDATE_HELPER_PATH=update_dir / "apply_update.ps1",
    ), patch.object(updater.os, "getpid", return_value=999999):
        updater.launch_apply_helper()

    marker = root / "restart_marker.txt"
    deadline = time.time() + 30
    while time.time() < deadline:
        if marker.exists() and (app_dir / "new.txt").exists() and not pending.exists():
            break
        time.sleep(0.3)

    assert (app_dir / "old.txt").read_text(encoding="utf-8") == "new", "app files not replaced"
    assert (app_dir / "new.txt").exists(), "new file missing"
    assert marker.exists(), "restart bat did not run"
    assert not pending.exists(), "pending.json not removed"
    print("update helper integration test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
