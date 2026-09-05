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
    """本地模型是否完整（引擎加载前检查）。

    要求全部固定文件齐全——若只有 model.bin 却缺 tokenizer/vocabulary 等小文件，
    会误判为就绪导致加载失败，因此必须逐个核对。
    """
    d = local_model_path(name)
    return all((d / f).is_file() for f in MODEL_FILES)


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


def _source_candidates(name: str) -> list[tuple[str, str]]:
    """可用下载源列表（按优先级返回 label 与 base URL）。"""
    cands: list[tuple[str, str]] = []
    if name in MS_AVAILABLE:
        cands.append(("ModelScope（国内直连）", MS_BASE.format(name=name)))
    cands.append(("hf-mirror 镜像", HF_BASE.format(name=name)))
    return cands


def _probe_speed(base: str, name: str) -> float:
    """探测某源的真实下载带宽（字节/秒）。用 model.bin 前 8MB 的 Range 请求实测。

    旧实现用小文件 config.json 只能反映连接延迟（虚低），无法区分快慢源；
    改测大文件块才能真正反映 CDN 带宽，帮助"多源竞速"选到最快源。
    """
    url = f"{base}/model.bin"
    headers = {"Range": "bytes=0-8388607"}
    t0 = time.time()
    got = 0
    try:
        with httpx.stream("GET", url, headers=headers, timeout=25, trust_env=False,
                          follow_redirects=True) as r:
            if r.status_code not in (200, 206):
                r.raise_for_status()
            for chunk in r.iter_bytes(1024 * 1024):
                got += len(chunk)
                if got >= 8 * 1024 * 1024:  # 读满 8MB 即可估速
                    break
    except Exception:  # noqa: BLE001  个别 CDN 不支持 Range，退回小文件估延迟
        return _probe_speed_latency(base)
    dt = time.time() - t0
    return got / dt if dt > 0 else 0.0


def _probe_speed_latency(base: str) -> float:
    """兜底：用小文件 config.json 估延迟（仅当实测失败时）。"""
    url = f"{base}/config.json"
    t0 = time.time()
    got = 0
    try:
        with httpx.stream("GET", url, timeout=15, trust_env=False,
                          follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(256 * 1024):
                got += len(chunk)
                if got > 2 * 1024 * 1024:
                    break
    except Exception:  # noqa: BLE001
        return 0.0
    dt = time.time() - t0
    return got / dt if dt > 0 else 0.0


def _pick_sources(name: str) -> list[tuple[str, str]]:
    """并发探测各源速度，返回按"最快优先"排序的源列表。"""
    cands = _source_candidates(name)
    scored: list[tuple[float, str, str]] = []
    for label, base in cands:
        scored.append((_probe_speed(base, name), label, base))
    scored.sort(key=lambda x: -x[0])
    return [(label, base) for _, label, base in scored]


def manual_download_guide(name: str) -> dict:
    """下载失败时给用户的手动引导信息（前端展示可点击链接与目标目录）。"""
    return {
        "name": name,
        "target_dir": str(local_model_path(name)),
        "files": _initial_files(name),
        "sources": [
            {"label": label, "base": base}
            for label, base in _source_candidates(name)
        ],
    }


def _initial_files(name: str) -> list[dict]:
    """构造模型文件清单（大小用估算值，存在标记按本地判断）。"""
    return [
        {"name": f, "size": _estimate_size(name, f), "exists": (local_model_path(name) / f).is_file(),
         "status": "done" if (local_model_path(name) / f).is_file() else "pending"}
        for f in MODEL_FILES
    ]


def any_model_ready() -> bool:
    """本地是否已有任意一个完整模型（用于区分"首次使用"与"切换模型"）。"""
    if not LOCAL_MODELS.is_dir():
        return False
    return bool(available_models())


def available_models() -> list[str]:
    """本地已"完整"就绪的模型名列表（用于界面标注"已下载"，避免残缺模型误标）。"""
    if not LOCAL_MODELS.is_dir():
        return []
    return sorted(
        d.name for d in LOCAL_MODELS.iterdir()
        if d.is_dir() and is_model_ready(d.name)
    )


def _fetch_file_sizes(base: str, name: str) -> dict[str, int]:
    """从源拉取模型文件大小（HEAD 请求，失败回退估算）。返回 {fname: bytes}。"""
    sizes: dict[str, int] = {}
    for fname in MODEL_FILES:
        url = f"{base}/{fname}"
        try:
            r = httpx.head(url, timeout=10, trust_env=False, follow_redirects=True)
            if r.status_code == 200 and r.headers.get("content-length"):
                sizes[fname] = int(r.headers["content-length"])
        except Exception:  # noqa: BLE001  个别 CDN 不支持 HEAD
            pass
    return sizes


def _estimate_size(name: str, fname: str) -> int | None:
    """按模型名估算单文件大小（主要用于大文件 model.bin 展示 '约 3GB'）。"""
    est = {
        "large-v3": {"model.bin": 3_087_284_237, "tokenizer.json": 2_480_617,
                     "vocabulary.json": 1_068_114},
        "large-v3-turbo": {"model.bin": 1_600_000_000},
        "medium": {"model.bin": 1_500_000_000},
        "small": {"model.bin": 480_000_000},
        "base": {"model.bin": 140_000_000},
        "tiny": {"model.bin": 75_000_000},
    }
    return est.get(name, {}).get(fname)


def _download_from(base: str, name: str, target_dir: Path,
                   progress: Callable[[float], None] | None,
                   cancel_check: Callable[[], bool] | None,
                   src_label: str,
                   state_cb: Callable[[dict], None] | None = None) -> None:
    """从单个源下载全部模型文件（断点续传）。失败抛异常。"""
    sizes = _fetch_file_sizes(base, name)
    files = []
    for fname in MODEL_FILES:
        exists = (target_dir / fname).is_file()
        files.append({
            "name": fname,
            "size": sizes.get(fname) or _estimate_size(name, fname),
            "exists": exists,
            "status": "done" if exists else "pending",
        })
    state = {
        "name": name, "source": src_label, "overall": 0.0,
        "current_file": None, "file_index": 0, "file_count": len(MODEL_FILES),
        "file_progress": 0.0, "files": files, "downloaded": 0, "total": 0,
    }
    n = len(MODEL_FILES)
    for i, fname in enumerate(MODEL_FILES):
        if state["files"][i]["exists"]:
            state["files"][i]["status"] = "done"
            state["overall"] = (i + 1) / n
            if state_cb:
                state_cb(dict(state))
            continue
        state["files"][i]["status"] = "downloading"
        state["current_file"] = fname
        state["file_index"] = i
        size = state["files"][i]["size"] or 0
        state["total"] = size
        url = f"{base}/{fname}"

        def on_f(frac: float, _state=state, _i=i) -> None:
            _set_file(_state, frac, _i, name, progress, state_cb)

        download_file(url, target_dir / fname, progress=on_f, cancel_check=cancel_check)
        state["files"][i]["status"] = "done"
        state["files"][i]["exists"] = True
        state["overall"] = (i + 1) / n
        state["downloaded"] = size
        if state_cb:
            state_cb(dict(state))


def download_model(name: str,
                   progress: Callable[[float], None] | None = None,
                   cancel_check: Callable[[], bool] | None = None,
                   state_cb: Callable[[dict], None] | None = None) -> None:
    """下载完整模型到 data/models/local/<name>/（多源竞速 + 断点续传 + 失败回退）。"""
    target_dir = local_model_path(name)
    if is_model_ready(name):
        if progress:
            progress(1.0)
        if state_cb:
            state_cb({"name": name, "ready": True})
        return

    sources = _pick_sources(name)
    last_err: Exception | None = None
    for label, base in sources:
        try:
            print(f"  使用下载源：{label}", flush=True)
            _download_from(base, name, target_dir, progress, cancel_check, label, state_cb)
            if is_model_ready(name):
                return
        except _DownloadCanceled:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  [警告] 源 {label} 下载失败（{e}），尝试下一源", flush=True)
    raise RuntimeError(
        f"模型下载失败（已尝试全部源）：{last_err}\n"
        f"请检查网络后重试，或按界面提示手动下载后放入 {target_dir}"
    )


def _set_file(state: dict, f: float, i: int,
              name: str, progress: Callable[[float], None] | None,
              state_cb: Callable[[dict], None] | None) -> None:
    """更新总体/当前文件进度，并回调（供前端展示每文件详情）。"""
    n = state["file_count"]
    overall = (i + f) / n
    state["overall"] = overall
    state["file_index"] = i
    state["current_file"] = state["files"][i]["name"]
    state["file_progress"] = f
    state["downloaded"] = int(f * (state["files"][i]["size"] or 0))
    if progress:
        progress(overall)
    if state_cb:
        state_cb(dict(state))


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "large-v3"
    print(f"下载模型 {model} …", flush=True)
    download_model(model, progress=lambda p: print(f"  {p:.0%}") if p < 1.0 else print("  完成"))
    print("完成 ✓")