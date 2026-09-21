"""在线更新核心。

设计目标：应用代码和网页资源的“小包更新”，不重复下载约 150MB 的完整绿色包。
- latest.json 放在 GitHub main 分支根目录，作为版本清单；
- 更新包（app/ 目录压缩）放 GitHub Releases；
- 下载后校验 SHA256，再交给 PowerShell 辅助脚本在程序退出后覆盖 app/ 并重启；
- 如果辅助脚本没有跑起来，下次启动时 _bootstrap.py 也会应用 pending 更新。

安全边界：只允许更新 app/ 目录；data/、logs/、python 运行时、exe 均不会被本模块覆盖。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from core import config

log = logging.getLogger("updater")

ProgressCb = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """在线更新失败。"""


# ----------------------------------------------------------------------
# 版本比较与清单
# ----------------------------------------------------------------------
def _version_key(version: str) -> tuple:
    """把 1.7.0 / v1.7.0 / 1.7.0-beta 转成可比较的元组。"""
    text = str(version or "").strip().lstrip("vV")
    parts = re.split(r"[.\-+]", text)
    key: list[Any] = []
    for p in parts:
        if p.isdigit():
            key.append(int(p))
        else:
            # 非数字后缀（如 beta）排在正式版之前，保持简单稳定
            key.append(-1)
            key.append(p.lower())
    return tuple(key)


def is_newer(remote: str, local: str) -> bool:
    return _version_key(remote) > _version_key(local)


def _candidate_urls(url: str, use_mirror: bool | None = None) -> list[str]:
    """返回下载候选顺序：原始 URL，然后镜像前缀 URL。"""
    urls = [url]
    if use_mirror is None:
        settings = config.load_settings()
        use_mirror = bool(settings.get("update_use_mirror", True))
    if use_mirror:
        for prefix in config.UPDATE_MIRROR_PREFIXES:
            urls.append(prefix.rstrip("/") + "/" + url.lstrip("/"))
    # 去重保序
    out: list[str] = []
    for u in urls:
        if u not in out:
            out.append(u)
    return out


def _http_get_json(url: str, timeout: float = 12.0) -> dict:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise UpdateError("latest.json 格式错误：根节点不是对象")
        return data


def fetch_manifest(force_mirror: bool | None = None) -> dict:
    """获取最新版清单。先直连，失败后按设置尝试镜像。"""
    errors: list[str] = []
    for url in _candidate_urls(config.UPDATE_MANIFEST_URL, force_mirror):
        try:
            log.info("检查更新清单: %s", url)
            return _http_get_json(url)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url} -> {e}")
            log.warning("获取更新清单失败: %s -> %s", url, e)
    raise UpdateError("无法获取更新清单：\n" + "\n".join(errors[:5]))


def _pick_asset(manifest: dict) -> dict:
    assets = manifest.get("assets")
    if isinstance(assets, dict) and assets:
        # 优先 app/source 这种小包；full 包暂时只提示、不自动应用
        for key in ("app", "source"):
            if isinstance(assets.get(key), dict):
                asset = dict(assets[key])
                if asset.get("url"):
                    return asset
        for asset in assets.values():
            if isinstance(asset, dict) and asset.get("url"):
                return dict(asset)
    # 兼容顶层简写
    if manifest.get("url"):
        return {
            "name": manifest.get("asset_name") or "update.zip",
            "url": manifest["url"],
            "size": manifest.get("size"),
            "sha256": manifest.get("sha256"),
        }
    raise UpdateError("latest.json 中没有可用的更新包")


def check_for_update() -> dict:
    """检查更新，返回给前端的状态字典。网络异常不抛出，返回 error 字段。"""
    result: dict[str, Any] = {
        "current_version": config.APP_VERSION,
        "update_available": False,
        "error": None,
    }
    try:
        manifest = fetch_manifest()
        remote = str(manifest.get("version") or "").strip()
        if not remote:
            raise UpdateError("latest.json 缺少 version 字段")
        need = is_newer(remote, config.APP_VERSION)
        result.update({
            "latest_version": remote,
            "notes": manifest.get("notes") or "",
            "published_at": manifest.get("published_at") or "",
            "asset": _pick_asset(manifest) if need else {},
            "update_available": need,
        })
        log.info("更新检查：当前=%s 最新=%s 可更新=%s",
                 config.APP_VERSION, remote, result["update_available"])
    except Exception as e:  # noqa: BLE001
        log.warning("检查更新失败: %s", e)
        result["update_available"] = False
        result["error"] = str(e)
    return result


# ----------------------------------------------------------------------
# 下载与校验
# ----------------------------------------------------------------------
def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _asset_target(asset: dict) -> Path:
    name = Path(str(asset.get("name") or "update.zip")).name
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return config.UPDATE_DIR / name


def _asset_hash_ok(path: Path, asset: dict) -> bool:
    want = str(asset.get("sha256") or "").strip().lower()
    if not want:
        log.warning("更新包未提供 sha256，跳过校验（不推荐）")
        return True
    got = sha256_file(path).lower()
    if got != want:
        log.error("更新包 SHA256 不匹配：want=%s got=%s", want, got)
        return False
    return True


def _download_one(url: str, dest: Path, progress_cb: ProgressCb | None) -> None:
    """下载到 .part，支持服务端断点续传。"""
    part = dest.with_suffix(dest.suffix + ".part")
    pos = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={pos}-"} if pos > 0 else {}
    mode = "ab" if pos > 0 else "wb"
    log.info("下载更新包: %s（已续传 %d 字节）", url, pos)

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", url, headers=headers) as r:
            if r.status_code == 416 and pos > 0:
                # 本地 part 已超过服务端文件长度，删掉重来
                part.unlink(missing_ok=True)
                raise UpdateError("断点文件异常，已清理，请重试")
            if r.status_code == 206 and pos > 0:
                mode = "ab"
            elif r.status_code == 200:
                mode = "wb"
                pos = 0
            else:
                r.raise_for_status()
            total = 0
            cl = r.headers.get("content-length")
            if cl and cl.isdigit():
                total = int(cl) + pos
            done = pos
            with part.open(mode) as f:
                for chunk in r.iter_bytes(1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb:
                        progress_cb(done, total)
    os.replace(part, dest)


def download_asset(asset: dict, progress_cb: ProgressCb | None = None) -> Path:
    """下载更新包并校验 SHA256；已下载且校验通过则直接复用。"""
    if not asset or not asset.get("url"):
        raise UpdateError("更新包地址为空")
    dest = _asset_target(asset)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and _asset_hash_ok(dest, asset):
        log.info("复用已下载更新包: %s", dest)
        return dest

    errors: list[str] = []
    for url in _candidate_urls(str(asset["url"])):
        try:
            _download_one(url, dest, progress_cb)
            if _asset_hash_ok(dest, asset):
                log.info("更新包下载完成并校验通过: %s", dest)
                return dest
            raise UpdateError("SHA256 校验失败")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url} -> {e}")
            log.warning("更新包下载失败: %s -> %s", url, e)
            # 清掉可能损坏的文件，下一源重新下
            dest.unlink(missing_ok=True)
            dest.with_suffix(dest.suffix + ".part").unlink(missing_ok=True)
    raise UpdateError("更新包下载失败：\n" + "\n".join(errors[:5]))


# ----------------------------------------------------------------------
# 暂存与启动时应用
# ----------------------------------------------------------------------
def stage_update(asset: dict, version: str) -> dict:
    """下载并写入 pending.json；真正的文件覆盖在重启时执行。"""
    zip_path = download_asset(asset)
    payload = {
        "version": version,
        "zip": str(zip_path),
        "sha256": str(asset.get("sha256") or ""),
        "staged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    config.UPDATE_PENDING_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("更新已暂存：v%s -> %s", version, zip_path)
    return payload


def load_pending() -> dict | None:
    p = config.UPDATE_PENDING_PATH
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("zip"):
            return data
    except Exception as e:  # noqa: BLE001
        log.warning("pending.json 无法解析，忽略: %s", e)
    return None


def _copy_tree_overwrite(src: Path, dst: Path, retries: int = 8) -> None:
    """把 src 内容覆盖复制到 dst，Windows 下对短暂占用做退避重试。"""
    last: Exception | None = None
    for i in range(retries):
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            return
        except PermissionError as e:
            last = e
            log.warning("覆盖文件被占用，%.1fs 后重试（%d/%d）: %s",
                       0.4 * (i + 1), i + 1, retries, e)
            time.sleep(0.4 * (i + 1))
    raise UpdateError(f"覆盖 app/ 失败，文件可能被占用: {last}")


def apply_pending_update() -> dict | None:
    """启动时应用 pending 更新（辅助脚本未执行成功时的兜底）。

    只更新 app/ 目录，不碰 data/、logs/、python/、exe。
    """
    pending = load_pending()
    if not pending:
        return None
    zip_path = Path(str(pending.get("zip") or ""))
    if not zip_path.exists():
        log.warning("pending 更新包不存在，清理 pending: %s", zip_path)
        config.UPDATE_PENDING_PATH.unlink(missing_ok=True)
        return None

    want = str(pending.get("sha256") or "").strip().lower()
    if want:
        got = sha256_file(zip_path).lower()
        if got != want:
            log.error("pending 更新包 SHA256 不匹配，已清理: want=%s got=%s", want, got)
            config.UPDATE_PENDING_PATH.unlink(missing_ok=True)
            zip_path.unlink(missing_ok=True)
            return None

    staging = config.UPDATE_STAGING_DIR / f"startup_{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(staging)
        src_app = staging / "app"
        if not src_app.is_dir():
            raise UpdateError("更新包内缺少 app/ 目录")
        _copy_tree_overwrite(src_app, config.BASE_DIR / "app")
        applied = {
            "version": pending.get("version"),
            "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "startup fallback",
        }
        config.UPDATE_APPLIED_PATH.write_text(
            json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        config.UPDATE_PENDING_PATH.unlink(missing_ok=True)
        log.info("启动时已应用待安装更新：v%s", pending.get("version"))
        return applied
    except Exception as e:  # noqa: BLE001
        log.error("启动时应用更新失败: %s", e, exc_info=True)
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ----------------------------------------------------------------------
# 退出后由 PowerShell 辅助脚本覆盖并重启
# ----------------------------------------------------------------------
_HELPER_PS1 = r'''param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$Pending,
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [string]$RestartExe = "",
    [string]$RestartBat = ""
)
$ErrorActionPreference = "Stop"
$UpdateDir = Join-Path $Root "data\updates"
$Log = Join-Path $Root "logs\update.log"
function Write-UpLog([string]$m) {
    $dir = Split-Path $Log -Parent
    if (!(Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Add-Content -Path $Log -Encoding UTF8 -Value ((Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff") + " | " + $m)
}
try {
    Write-UpLog "helper start; waitPid=$WaitPid"
    try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Milliseconds 800

    # 关闭仍在项目目录内运行的旧进程（主要是 WebView2 启动器；当前 PowerShell 不在项目目录内）
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessId -ne $PID -and $_.ExecutablePath -and $_.ExecutablePath.ToLower().StartsWith($Root.ToLower()) } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
    Start-Sleep -Milliseconds 600

    $p = Get-Content -LiteralPath $Pending -Raw -Encoding UTF8 | ConvertFrom-Json
    $zip = [string]$p.zip
    if (!(Test-Path -LiteralPath $zip)) { throw "update zip not found: $zip" }

    $tmp = Join-Path $UpdateDir ("helper_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $srcApp = Join-Path $tmp "app"
    if (!(Test-Path $srcApp)) { throw "update zip missing app/" }
    Copy-Item -Path (Join-Path $srcApp "*") -Destination (Join-Path $Root "app") -Recurse -Force
    Remove-Item -LiteralPath $Pending -Force -ErrorAction SilentlyContinue
    Write-UpLog "apply OK; version=$($p.version)"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

    if ($RestartExe -and (Test-Path -LiteralPath $RestartExe)) {
        Start-Process -FilePath $RestartExe -WorkingDirectory $Root
        Write-UpLog "restart exe: $RestartExe"
    } elseif ($RestartBat -and (Test-Path -LiteralPath $RestartBat)) {
        Start-Process -FilePath $RestartBat -WorkingDirectory $Root
        Write-UpLog "restart bat: $RestartBat"
    } else {
        Write-UpLog "no restart target"
    }
} catch {
    Write-UpLog ("apply failed: " + $_.Exception.Message)
    exit 1
}
'''


def _find_restart_targets() -> tuple[str, str]:
    exe = ""
    bat = ""
    try:
        for p in config.BASE_DIR.iterdir():
            if p.is_file() and p.suffix.lower() == ".exe" and "webview2" not in p.name.lower():
                exe = str(p)
                break
        for p in config.BASE_DIR.iterdir():
            if p.is_file() and p.suffix.lower() == ".bat":
                bat = str(p)
                break
    except Exception:  # noqa: BLE001
        pass
    return exe, bat


def launch_apply_helper() -> None:
    """写入并启动 apply_update.ps1；调用后 API 应尽快让服务退出。"""
    pending = load_pending()
    if not pending:
        raise UpdateError("没有待安装的更新，请先下载更新")
    config.UPDATE_HELPER_PATH.write_text(_HELPER_PS1, encoding="utf-8-sig")
    exe, bat = _find_restart_targets()
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(config.UPDATE_HELPER_PATH),
        "-Root", str(config.BASE_DIR),
        "-Pending", str(config.UPDATE_PENDING_PATH),
        "-WaitPid", str(os.getpid()),
    ]
    # 只传非空可选参数；PowerShell -File 显式收到空字符串会在写日志前退出
    if exe:
        cmd += ["-RestartExe", exe]
    if bat:
        cmd += ["-RestartBat", bat]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    log.info("启动更新辅助脚本: %s", cmd)
    subprocess.Popen(cmd, close_fds=True, creationflags=creationflags)
    log.info("更新辅助脚本已启动，服务即将退出以完成替换并重启")
