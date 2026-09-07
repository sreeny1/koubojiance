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

import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable

import httpx

from .config import MODELS_DIR

log = logging.getLogger("model_download")

LOCAL_MODELS = MODELS_DIR / "local"

MS_BASE = "https://modelscope.cn/models/Systran/faster-whisper-{name}/resolve/master"
HF_BASE = "https://hf-mirror.com/Systran/faster-whisper-{name}/resolve/main"

# faster-whisper 模型仓库的候选文件清单（下载时按"源上实际存在"挑选）
# 关键：不同模型词表文件不同——large-v3 用 vocabulary.json，medium/small/base/tiny 用 vocabulary.txt！
MODEL_FILES = [
    "model.bin", "config.json", "tokenizer.json",
    "vocabulary.json", "vocabulary.txt",
    "preprocessor_config.json", "configuration.json",
]
# 加载必需的核心文件（ctranslate2 加载模型必须）
_CORE_FILES = ["model.bin", "config.json", "tokenizer.json"]
# 词表文件候选（不同模型必有其一；缺失会导致 "Cannot load the vocabulary from the model directory"）
_VOCAB_FILES = ["vocabulary.json", "vocabulary.txt"]
# 各模型仓库实际存在的"模型本体"文件（用于界面/手动引导精确展示；.gitattributes/README.md 之外）
_MODEL_FILES_EXPECTED = {
    "large-v3": ["model.bin", "config.json", "tokenizer.json", "vocabulary.json",
                 "preprocessor_config.json", "configuration.json"],
    "large-v3-turbo": ["model.bin", "config.json", "tokenizer.json", "vocabulary.json",
                       "preprocessor_config.json", "configuration.json"],  # large-v3 变体，同理
    "medium": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "configuration.json"],
    "small": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "configuration.json"],
    "base": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "configuration.json"],
    "tiny": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "configuration.json"],
}
# ModelScope 已官方镜像的模型尺寸（经 verify_model.py 逐文件核验过大模型）
MS_AVAILABLE = {"large-v3", "large-v3-turbo", "medium", "small", "base", "tiny"}


def local_model_path(name: str) -> Path:
    return LOCAL_MODELS / name


def is_model_ready(name: str) -> bool:
    """本地模型是否可加载（引擎加载前检查）。

    必须满足：3 个核心文件齐全 + 至少一个词表文件（vocabulary.json 或 vocabulary.txt）。
    不同模型词表形式不同，若只核对核心 3 文件会误判"就绪"而在加载时报
    "Cannot load the vocabulary from the model directory"。
    """
    d = local_model_path(name)
    if not all((d / f).is_file() for f in _CORE_FILES):
        return False
    return any((d / f).is_file() for f in _VOCAB_FILES)


def ms_exists(name: str) -> bool:
    """探测 ModelScope 是否有该模型（带重试；失败时明确警告并回退 hf-mirror）。"""
    if name not in MS_AVAILABLE:
        return False
    url = f"https://modelscope.cn/api/v1/models/Systran/faster-whisper-{name}"
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            r = httpx.get(url, timeout=20, trust_env=False)
            log.debug("ModelScope 探测 %s（第 %d 次）→ %s", name, attempt, r.status_code)
            if r.status_code == 200:
                return True
            if r.status_code == 404:
                log.info("ModelScope 无模型 %s（404），改用 hf-mirror", name)
                return False
            # 其它状态（如 429/5xx）继续重试
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("ModelScope 探测第 %d 次失败: %s", attempt, e)
        time.sleep(2)
    log.warning("ModelScope 探测失败（%s），回退 hf-mirror 慢速源", last_err)
    return False


def download_file(url: str, dst: Path, retries: int = 10,
                  progress: Callable[[float], None] | None = None,
                  cancel_check: Callable[[], bool] | None = None) -> None:
    """断点续传下载单文件（ModelScope/HF CDN 均支持 Range）。

    progress 回调：单个文件已下载比例(0~1)。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    t0 = time.monotonic()
    for attempt in range(1, retries + 1):
        pos = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={pos}-"} if pos else {}
        log.debug("下载文件 %s（第 %d 次尝试，断点 %s）", dst.name, attempt,
                  f"{pos / 1048576:.1f}MB" if pos else "0")
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
            log.warning("下载 %s 第 %d 次失败: %s（将重试）", dst.name, attempt, e)
            if attempt == retries:
                raise
            time.sleep(attempt * 3)  # 指数退避重试
    if tmp.exists():
        size = tmp.stat().st_size
        os.replace(tmp, dst)
        log.info("文件下载完成: %s（%.2f MB，耗时 %.1fs）",
                 dst.name, size / 1048576, time.monotonic() - t0)


class _DownloadCanceled(Exception):
    """用户取消了模型下载（与转写取消共用语义）。"""


def _source_candidates(name: str) -> list[tuple[str, str]]:
    """可用下载源列表（按优先级返回 label 与 base URL）。"""
    cands: list[tuple[str, str]] = []
    if name in MS_AVAILABLE:
        cands.append(("ModelScope（国内直连）", MS_BASE.format(name=name)))
    cands.append(("hf-mirror 镜像", HF_BASE.format(name=name)))
    return cands





def _pick_sources(name: str) -> list[tuple[str, str]]:
    """按优先级返回下载源：ModelScope（国内直连，通常最快）优先，hf-mirror 作备份。

    不再做"下载式带宽探测"——那会先下载 16MB 且期间不展示进度，导致用户看到卡在 0%。
    直接按已知最快的国内源优先，失败再自动切下一源，启动更快、进度更可感知。
    """
    return _source_candidates(name)


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
    files = _MODEL_FILES_EXPECTED.get(name, MODEL_FILES)
    return [
        {"name": f, "size": _estimate_size(name, f), "exists": (local_model_path(name) / f).is_file(),
         "status": "done" if (local_model_path(name) / f).is_file() else "pending"}
        for f in files
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


def _resolve_source_files(base: str, name: str) -> list[str]:
    """返回该模型在该源上"实际存在"的文件清单（HEAD 失败用 GET Range 兜底）。

    不同模型仓库的文件数不同：small/medium 通常缺少 preprocessor_config.json /
    vocabulary.json（404），不能按固定 5 文件硬下，否则会卡在永远找不到的文件上。
    """
    out: list[str] = []
    for f in MODEL_FILES:
        url = f"{base}/{f}"
        ok = False
        try:
            r = httpx.head(url, timeout=10, trust_env=False, follow_redirects=True)
            ok = r.status_code in (200, 206)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            try:
                with httpx.stream("GET", url, headers={"Range": "bytes=0-0"},
                                  timeout=10, trust_env=False, follow_redirects=True) as r:
                    ok = r.status_code in (200, 206)
            except Exception:  # noqa: BLE001
                ok = False
        log.debug("源文件探测 %s → %s=%s", f, "存在" if ok else "404",
                  url[:110])
        if ok:
            out.append(f)
    # 本机已存在的文件也纳入（避免源临时探测不到但本地已有）
    for f in MODEL_FILES:
        if f not in out and (local_model_path(name) / f).is_file():
            out.append(f)
    if not out:
        out = list(_CORE_FILES) + list(_VOCAB_FILES)  # 兜底：核心 + 词表
    return out


def _download_from(base: str, name: str, file_list: list[str], target_dir: Path,
                   progress: Callable[[float], None] | None,
                   cancel_check: Callable[[], bool] | None,
                   src_label: str,
                   state_cb: Callable[[dict], None] | None = None) -> None:
    """从单个源下载指定文件（断点续传）。file_list 为该源实际存在的文件。"""
    sizes = _fetch_file_sizes(base, name)
    files = []
    for fname in file_list:
        exists = (target_dir / fname).is_file()
        files.append({
            "name": fname,
            "size": sizes.get(fname) or _estimate_size(name, fname),
            "exists": exists,
            "status": "done" if exists else "pending",
        })
    n = len(file_list)
    log.info("使用源 [%s] 下载模型 %s：%d 个文件（%s）", src_label, name, n,
             ", ".join(f"{f['name']}({'已存在' if f['exists'] else f['size']})"
                       for f in [{"name": fn, "size": sizes.get(fn) or _estimate_size(name, fn), "exists": (target_dir / fn).is_file()} for fn in file_list]))
    state = {
        "name": name, "source": src_label, "overall": 0.0, "frac": 0.0,
        "current_file": None, "file_index": 0, "file_count": n,
        "file_progress": 0.0, "files": files, "downloaded": 0, "total": 0,
    }
    for i, fname in enumerate(file_list):
        if state["files"][i]["exists"]:
            state["files"][i]["status"] = "done"
            state["overall"] = (i + 1) / n
            state["frac"] = state["overall"]
            if state_cb:
                state_cb(dict(state))
            continue
        state["files"][i]["status"] = "downloading"
        state["current_file"] = fname
        state["file_index"] = i
        size = state["files"][i]["size"] or 0
        state["total"] = size
        url = f"{base}/{fname}"
        log.info("开始下载文件 %s/%s（%.2f MB）→ %s",
                 name, fname, (size or 0) / 1048576, target_dir / fname)

        def on_f(frac: float, _state=state, _i=i) -> None:
            _set_file(_state, frac, _i, name, progress, state_cb)

        download_file(url, target_dir / fname, progress=on_f, cancel_check=cancel_check)
        state["files"][i]["status"] = "done"
        state["files"][i]["exists"] = True
        state["overall"] = (i + 1) / n
        state["frac"] = state["overall"]
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
    log.info("模型 %s 本地未就绪，开始下载。源优先级: %s",
             name, " → ".join(label for label, _ in sources))
    last_err: Exception | None = None
    for label, base in sources:
        try:
            log.info("使用下载源: %s", label)
            file_list = _resolve_source_files(base, name)
            _download_from(base, name, file_list, target_dir, progress, cancel_check, label, state_cb)
            if is_model_ready(name):
                log.info("模型 %s 下载完成（源: %s），就绪校验通过", name, label)
                return
            log.warning("源 %s 下载结束但就绪校验未通过，尝试下一源", label)
        except _DownloadCanceled:
            log.info("模型 %s 下载被用户取消", name)
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("源 %s 下载失败（%s），尝试下一源", label, e)
    log.error("模型下载失败（已尝试全部源）: %s", last_err)
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
    state["frac"] = overall
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