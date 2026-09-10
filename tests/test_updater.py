# -*- coding: utf-8 -*-
"""在线更新核心测试：版本比较、镜像候选、pending 更新应用（临时目录，不碰正式数据）。"""
import http.server
import json
import socketserver
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core import config, updater  # noqa: E402


def test_version_compare():
    assert updater.is_newer("1.7.0", "1.6.9")
    assert updater.is_newer("v1.10.0", "1.7.0")
    assert not updater.is_newer("1.7.0", "1.7.0")
    assert not updater.is_newer("1.6.9", "1.7.0")
    print("[1] 版本比较 OK")


def test_candidate_urls():
    with patch.object(config, "load_settings", return_value={"update_use_mirror": True}):
        urls = updater._candidate_urls("https://github.com/a/b/releases/download/v1/a.zip")
    assert urls[0].startswith("https://github.com/")
    assert any("ghproxy" in u for u in urls)
    print("[2] 镜像候选 URL OK")


def test_apply_pending_update():
    tmp = Path(tempfile.mkdtemp(prefix="updater_test_"))
    root = tmp / "root"
    app_dir = root / "app"
    app_dir.mkdir(parents=True)
    (app_dir / "old.txt").write_text("old", encoding="utf-8")

    update_dir = root / "data" / "updates"
    staging = update_dir / "staging"
    update_dir.mkdir(parents=True)
    staging.mkdir(parents=True)

    # 构造一个 app/ 更新包
    pkg = update_dir / "app-update.zip"
    with zipfile.ZipFile(pkg, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("app/old.txt", "new")
        zf.writestr("app/core/updater.py", "# new updater module\n")
    digest = updater.sha256_file(pkg)

    pending = update_dir / "pending.json"
    pending.write_text(json.dumps({
        "version": "9.9.9",
        "zip": str(pkg),
        "sha256": digest,
    }, ensure_ascii=False), encoding="utf-8")

    with patch.multiple(
        config,
        BASE_DIR=root,
        UPDATE_DIR=update_dir,
        UPDATE_STAGING_DIR=staging,
        UPDATE_PENDING_PATH=pending,
        UPDATE_APPLIED_PATH=update_dir / "applied.json",
        UPDATE_HELPER_PATH=update_dir / "apply_update.ps1",
    ):
        applied = updater.apply_pending_update()

    assert applied and applied.get("version") == "9.9.9"
    assert (app_dir / "old.txt").read_text(encoding="utf-8") == "new"
    assert (app_dir / "core" / "updater.py").exists()
    assert not pending.exists()
    assert (update_dir / "applied.json").exists()
    print("[3] pending 更新应用 OK")


def test_local_http_check_and_download():
    """用本机临时 HTTP 服务验证 latest.json -> 下载 zip -> pending.json 全流程。"""
    tmp = Path(tempfile.mkdtemp(prefix="updater_http_"))
    pkg = tmp / "koubo-app-v9.9.9.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("app/main.py", "# fake update\n")
    digest = updater.sha256_file(pkg)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(tmp), **kw)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    manifest = {
        "version": "9.9.9",
        "published_at": "2026-09-10T00:00:00+08:00",
        "notes": "local test",
        "assets": {
            "app": {
                "name": pkg.name,
                "url": f"http://127.0.0.1:{port}/{pkg.name}",
                "size": pkg.stat().st_size,
                "sha256": digest,
            }
        },
    }
    (tmp / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    update_dir = tmp / "updates"
    update_dir.mkdir()
    try:
        with patch.multiple(
            config,
            APP_VERSION="1.7.0",
            UPDATE_MANIFEST_URL=f"http://127.0.0.1:{port}/latest.json",
            UPDATE_DIR=update_dir,
            UPDATE_PENDING_PATH=update_dir / "pending.json",
        ):
            result = updater.check_for_update()
            assert result["update_available"] is True
            assert result["latest_version"] == "9.9.9"
            pending = updater.stage_update(result["asset"], result["latest_version"])
            assert Path(pending["zip"]).exists()
            assert (update_dir / "pending.json").exists()
    finally:
        srv.shutdown()
        srv.server_close()
    print("[4] 本地 HTTP 检查+下载 OK")


if __name__ == "__main__":
    test_version_compare()
    test_candidate_urls()
    test_apply_pending_update()
    test_local_http_check_and_download()
    print("\nupdater 测试全部通过 ✓")
