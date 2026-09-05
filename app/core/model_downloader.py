"""模型自动下载模块（国内网络友好）。

优先从 ModelScope（阿里魔搭，国内直连，已验证 5.7MB/s）下载
Systran/faster-whisper-* 模型，落到 data/models/local/<name>/；
ModelScope 不支持该模型时回退 HuggingFace hf-mirror 直连。

核心设计（与 tests/download_model.py 同源，供引擎首次启动复用）：
- 断点续传：.part 临时文件 + Range 请求，中断自动重试；
- 全自动：无任何交互，引擎在缺少模型时自动调用；
- 进度回调：可供前端展示下载百分比。
"""
from __future__ import annotations

import os
import time
import urllib.request
from pathlib import Path
from typing import Callable

import httpx

from .config import MODELS_DIR

LOCAL_MODELS = MODELS_DIR / "local"

MS_BASE = "https://modelscope.cn/models/Systran/faster-whisper-{name}/resolve/master"
HF_BASE = "https://hf-mirror.com/Systran/faster-whisper-{name}/resolve/main"

# faster-whisper 模型仓库的固定文件清单
MODEL_FILES = [
    "model.bin", "config.json", "preprocessor_config.json",
    "tokenizer.json", "vocabulary.json",
]
# ModelScope 已官方镜像的模型尺寸（经 verify_model.py 逐文件核验过大模型）
MS_AVAILABLE = {"large-v3", "large-v3-turbo", "medium", "small", "base", "tiny"}


def local_model_path(name: str) -> Path:
    return LOCAL_MODELS / name


def is_model_ready(name: str) -> bool:
    """本地模型是否完整（引擎加载前检查）。"""
    return (local_model_path(name) / "model.bin").is_file()


def ms_exists(name: str) -> bool:
    """探测 ModelScope 是否有该模型（带重试；失败时明确警告并回退 hf-mirror）。"""
    if name not in MS_AVAILABLE:
        return False
    url = f"https://modelscope.cn/api/v1/models/Systran/faster-whisper-{name}"
    last_err: Exception | None = None
    for _ in range(3):
        try:
            r = httpx.get(url, timeout=20, trust_env=False)
            if r.status_code == 200:
                return True
            if r.status_code == 404:
                return False
            # 其它状态（如 429/5xx）继续重试
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(2)
    print(f"  [警告] ModelScope 探测失败（{last_err}），回退 hf-mirror 慢速源", flush=True)
    return False


def download_file(url: str, dst: Path, retries: int = 10,
                  progress: Callable[[float], None] | None = None,
                  cancel_check: Callable[[], bool] | None = None) -> None:
    """断点续传下载单文件（ModelScope/HF CDN 均支持 Range）。

    progress 回调：单个文件已下载比例(0~1)。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    for attempt in range(1, retries + 1):
        pos = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        try:
            with httpx.stream("GET", url, headers=headers, timeout=30,
                              trust_env=False, follow_redirects=True) as r:
                if r.status_code == 416:  # Range 超出 → 文件已下载完整
                    break
                r.raise_for_status()
                mode = "ab" if pos and r.status_code == 206 else "wb"
                total = int(r.headers.get("content-length", 0)) + pos
                done = pos
                with open(tmp, mode) as f:
                    for chunk in r.iter_bytes(1024 * 1024):
                        if cancel_check and cancel_check():
                            raise _DownloadCanceled()
                        f.write(chunk)
                        done += len(chunk)
                        if total and progress:
                            progress(done / total)
                if total and done < total:
                    raise IOError(f"下载不完整 {done}/{total}")
                break
        except _DownloadCanceled:
            raise
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(attempt * 3)  # 指数退避重试
    if tmp.exists():
        os.replace(tmp, dst)


class _DownloadCanceled(Exception):
    """用户取消了模型下载（与转写取消共用语义）。"""


def download_model(name: str,
                   progress: Callable[[float], None] | None = None,
                   cancel_check: Callable[[], bool] | None = None) -> None:
    """下载完整模型到 data/models/local/<name>/（自动选择源与断点续传）。"""
    target_dir = local_model_path(name)
    if is_model_ready(name):
        if progress:
            progress(1.0)
        return

    if ms_exists(name):
        base = MS_BASE.format(name=name)
        src = "ModelScope(国内直连)"
    else:
        base = HF_BASE.format(name=name)
        src = "hf-mirror(直连)"
        # hf-mirror 国内直连：绕过系统代理，避免 VPN 劫持 TLS
        urllib.request.getproxies = lambda: {}  # type: ignore[assignment]

    file_progress = [0.0]  # 当前文件已下载比例（回调线程内更新）
    n_files = len(MODEL_FILES)
    for i, fname in enumerate(MODEL_FILES):
        if (target_dir / fname).exists():
            file_progress[0] = 1.0
        else:
            file_progress[0] = 0.0
        url = f"{base}/{fname}"
        try:
            download_file(
                url, target_dir / fname,
                progress=lambda f: _set_file(file_progress, f, i, n_files, name, progress),
                cancel_check=cancel_check,
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"模型下载失败（{src}）：{fname} — {e}\n"
                f"请检查网络后重试（已支持断点续传，再次启动会从断点继续）"
            ) from e
        file_progress[0] = 1.0
        if progress:
            progress((i + 1) / n_files)


def _set_file(file_progress: list[float], f: float, i: int, n_files: int,
              name: str, progress: Callable[[float], None] | None) -> None:
    """合并单文件进度到总体进度：总进度 = 已完文件 + 当前文件比例。"""
    if progress:
        overall = (i + f) / n_files
        file_progress[0] = f
        progress(overall)


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "large-v3"
    print(f"下载模型 {model} …", flush=True)
    download_model(model, progress=lambda p: print(f"  {p:.0%}") if p < 1.0 else print("  完成"))
    print("完成 ✓")