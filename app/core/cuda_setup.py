"""CUDA 运行库首启自动下载与本地安装（仅 NVIDIA 机器需要）。

绿色免安装包为控制体积不再封装 NVIDIA CUDA 库（wheel 合计约 1.36GB，
解压后约 2GB DLL）：首次启动检测到 NVIDIA 显卡时，从国内 PyPI 镜像
（清华 → 阿里云 → 腾讯云，均支持断点续传）自动下载 3 个固定版本 wheel，
做「大小 + SHA256」双重校验后解压到 data/runtime/nvidia/，并按
关键文件清单核对就绪；就绪后 ctranslate2 才可启用 GPU，否则自动走 CPU。

清单即契约：
- CUDA_PKGS 每个包固定版本 / wheel 文件名 / 关键文件（required），
  升级 NVIDIA 库时必须同步更新本清单与 wheel 文件名；
- 下载完成后写 data/runtime/manifests/cuda.json（文件清单留档，
  供校验器 / 人工核对，保证不遗漏文件）。
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx

from .config import DATA_DIR, RUNTIME_DIR
from .model_downloader import _DownloadCanceled, download_file

log = logging.getLogger("cuda_setup")

# 解压后的运行库根目录（wheel 解压出来的 nvidia/ 放这里）
NVIDIA_DIR = RUNTIME_DIR / "nvidia"
WHEELS_DIR = RUNTIME_DIR / "wheels"
MANIFEST_PATH = RUNTIME_DIR / "manifests" / "cuda.json"

# ---- CUDA 运行库清单（固定版本，与 requirements.txt 的 GPU 依赖一致）----
# size 为 wheel 下载体积（十进制 MB，仅用于界面展示/估算）；完整性以 SHA256（镜像索引官方哈希）为准；
# required 为解压后的关键文件（相对 NVIDIA_DIR），全部存在才算就绪
CUDA_PKGS: list[dict] = [
    {
        "name": "nvidia-cublas-cu12",
        "version": "12.9.2.10",
        "wheel": "nvidia_cublas_cu12-12.9.2.10-py3-none-win_amd64.whl",
        "size": 553,
        "required": [
            "cublas/bin/cublas64_12.dll",
            "cublas/bin/cublasLt64_12.dll",
            "cublas/bin/nvblas64_12.dll",
        ],
    },
    {
        "name": "nvidia-cudnn-cu12",
        "version": "9.25.1.1",
        "wheel": "nvidia_cudnn_cu12-9.25.1.1-py3-none-win_amd64.whl",
        "size": 732,
        "required": [
            "cudnn/bin/cudnn64_9.dll",
            "cudnn/bin/cudnn_ops64_9.dll",
            "cudnn/bin/cudnn_cnn64_9.dll",
            "cudnn/bin/cudnn_adv64_9.dll",
            "cudnn/bin/cudnn_graph64_9.dll",
            "cudnn/bin/cudnn_engines_precompiled64_9.dll",
        ],
    },
    {
        "name": "nvidia-cuda-nvrtc-cu12",
        "version": "12.9.86",
        "wheel": "nvidia_cuda_nvrtc_cu12-12.9.86-py3-none-win_amd64.whl",
        "size": 76,
        "required": [
            "cuda_nvrtc/bin/nvrtc64_120_0.dll",
            "cuda_nvrtc/bin/nvrtc64_120_0.alt.dll",
        ],
    },
]

# 国内可用的 PyPI 镜像（按优先级；simple 索引解析 wheel 链接，天然支持断点续传）
PIP_SIMPLE_MIRRORS: list[tuple[str, str]] = [
    ("清华 PyPI 镜像", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云 PyPI 镜像", "https://mirrors.aliyun.com/pypi/simple"),
    ("腾讯云 PyPI 镜像", "https://mirrors.cloud.tencent.com/pypi/simple"),
]


# ----------------------------------------------------------------------
# 显卡判定与就绪
# ----------------------------------------------------------------------
def need_cuda_runtime() -> bool:
    """是否需要 CUDA 运行库：仅当机器有 NVIDIA 显卡（注册表枚举，不依赖已装库）。"""
    try:
        from .syscheck import _win_gpu_names

        names = " ".join(_win_gpu_names()).lower()
    except Exception:  # noqa: BLE001
        return False
    keywords = ("nvidia", "geforce", "gforce", "quadro", "tesla", "rtx", "gtx")
    return any(k in names for k in keywords)


def is_cuda_runtime_ready() -> bool:
    """按关键文件清单核对：每个包的 required 文件都必须存在。"""
    if not NVIDIA_DIR.is_dir():
        return False
    return all((NVIDIA_DIR / rel).is_file()
               for pkg in CUDA_PKGS for rel in pkg["required"])


def cuda_bin_dirs() -> list[str]:
    """返回已安装运行库的 bin 目录（供 transcriber 注册 DLL 搜索路径）。"""
    if not is_cuda_runtime_ready():
        return []
    dirs: list[str] = []
    for d in sorted(NVIDIA_DIR.glob("*/bin")):
        if d.is_dir() and any(d.glob("*.dll")):
            dirs.append(str(d))
    return dirs


def cuda_manifest() -> dict:
    """文件清单（供界面展示 / 人工核对）：每个包的 wheel 与关键文件。"""
    return {
        "name": "nvidia-cuda-runtime",
        "nvidia_dir": str(NVIDIA_DIR),
        "packages": [
            {
                "name": p["name"], "version": p["version"],
                "wheel": p["wheel"], "size_mb": p["size"],
                "required": p["required"],
                "installed": all((NVIDIA_DIR / r).is_file() for r in p["required"]),
            }
            for p in CUDA_PKGS
        ],
    }


def save_cuda_manifest() -> None:
    """把下载/校验结果写入清单文件（留档核对）。"""
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = cuda_manifest()
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    MANIFEST_PATH.write_text(
        __import__("json").dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
# wheel 源解析（simple 索引 → 下载 URL + 官方 SHA256）
# ----------------------------------------------------------------------
def _parse_simple_index(simple_base: str, pkg: dict) -> tuple[str, str] | None:
    """从镜像 simple 索引解析指定版本 wheel 的 (下载URL, sha256)。

    注意：simple 索引 URL 使用规范包名（连字符，如 nvidia-cublas-cu12），
    而 wheel 文件名是下划线（nvidia_cublas_cu12-...whl）。
    """
    simple_url = f"{simple_base.rstrip('/')}/{pkg['name']}/"
    r = httpx.get(simple_url, timeout=20, trust_env=False, follow_redirects=True)
    if r.status_code != 200:
        raise RuntimeError(f"镜像索引返回 {r.status_code}: {simple_url}")
    for m in re.finditer(r'<a[^>]+href="([^"]+)"', r.text):
        href = html.unescape(m.group(1))
        target = f"{pkg['wheel']}"
        if target not in href:
            continue
        url = href.split("#")[0]
        sha = None
        if "#sha256=" in href:
            sha = href.split("#sha256=")[1].split("#")[0].strip()
        return urljoin(simple_url, url), sha
    raise RuntimeError(f"镜像 {simple_base} 未找到 {pkg['wheel']}")


def _resolve_wheel(pkg: dict) -> tuple[str, str, str]:
    """按镜像优先级解析 wheel，返回 (源label, 下载URL, sha256)。"""
    last_err: Exception | None = None
    for label, base in PIP_SIMPLE_MIRRORS:
        try:
            url, sha = _parse_simple_index(base, pkg)
            log.info("CUDA wheel 源解析成功 [%s]: %s", label, pkg["wheel"])
            return label, url, sha
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("源 [%s] 解析 %s 失败: %s", label, pkg["wheel"], e)
    raise RuntimeError(f"所有 PyPI 镜像均无法解析 {pkg['wheel']}: {last_err}")


# ----------------------------------------------------------------------
# 校验与安装
# ----------------------------------------------------------------------
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_wheel(whl: Path) -> bool:
    """wheel 大小校验（下全、非 0；SHA256 在下载完成时校验过，这里看 .ok 标记）。"""
    if not whl.is_file() or whl.stat().st_size == 0:
        return False
    ok = whl.with_suffix(".ok")
    return ok.is_file() and ok.read_text(encoding="utf-8", errors="ignore").strip() != ""


def _mark_wheel(whl: Path, sha: str) -> None:
    (whl.with_suffix(".ok")).write_text(sha or "sha256-verified", encoding="utf-8")


def _install_wheel(whl: Path, nvidia_dir: Path) -> None:
    """把 wheel（即 zip）安全解压到运行库目录。

    wheel 内成员通常带 nvidia/ 前缀（如 nvidia/cublas/bin/...），手动写入时
    剥离该前缀，使最终结构为 data/runtime/nvidia/<pkg>/bin/*.dll，
    避免出现 nvidia/nvidia 双层目录；同时逐成员做路径安全校验。
    """
    nvidia_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(whl)) as zf:
        for member in zf.infolist():
            src_name = member.filename.replace("\\", "/")
            # 路径安全校验（防 zip 炸弹路径穿越）
            if src_name.startswith(("/", "\\")) or ".." in src_name.split("/") or ":" in src_name:
                raise RuntimeError(f"wheel 含不安全路径，已拒绝: {src_name}")
            target_name = src_name
            if target_name.startswith("nvidia/"):
                target_name = target_name[len("nvidia/"):]
            target = nvidia_dir / target_name
            if src_name.endswith("/") or target_name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        log.info("解压 wheel: %s（%d 个成员）→ %s", whl.name, len(zf.infolist()), nvidia_dir)


def _pkg_installed(pkg: dict) -> bool:
    return all((NVIDIA_DIR / rel).is_file() for rel in pkg["required"])


# ----------------------------------------------------------------------
# 主流程：下载 → 校验 → 解压 → 清单核对
# ----------------------------------------------------------------------
def download_cuda_runtime(
    progress: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    state_cb: Callable[[dict], None] | None = None,
) -> None:
    """下载并安装 CUDA 运行库（国内镜像 + 断点续传 + SHA256 + 清单核对）。

    已就绪直接返回；每个包下载后立即校验 SHA256（来源索引携带官方哈希），
    解压后核对 required 文件，全部通过才写 manifest。
    """
    if is_cuda_runtime_ready():
        log.info("CUDA 运行库已就绪，跳过下载")
        return
    WHEELS_DIR.mkdir(parents=True, exist_ok=True)
    n = len(CUDA_PKGS)
    files = [
        {"name": p["name"], "size": p["size"] * 1_000_000,
         "status": "done" if _pkg_installed(p) else "pending"}
        for p in CUDA_PKGS
    ]
    state = {
        "stage": "cuda", "name": "nvidia-cuda-runtime",
        "overall": 0.0, "frac": 0.0, "source": None,
        "current_file": None, "file_index": -1, "file_count": n,
        "file_progress": 0.0, "files": files, "downloaded": 0, "total": 0,
        "verify": True,
    }

    def _emit() -> None:
        if state_cb:
            state_cb(dict(state))

    _emit()
    t_all = time.monotonic()
    for i, pkg in enumerate(CUDA_PKGS):
        if _pkg_installed(pkg):
            state["files"][i]["status"] = "done"
            state["overall"] = (i + 1) / n
            state["frac"] = state["overall"]
            log.info("CUDA 包已安装，跳过: %s", pkg["name"])
            _emit()
            continue

        whl = WHEELS_DIR / pkg["wheel"]
        state["source"] = None
        try:
            label, url, sha = _resolve_wheel(pkg)
            # 已下载的 wheel 先复用：非空并按官方 SHA256 复核，不符才删除重下
            if whl.is_file() and whl.stat().st_size >= 10 * 1024 * 1024:
                actual = _sha256_file(whl)
                if not sha or actual == sha:
                    _mark_wheel(whl, sha or actual)
                    log.info("CUDA wheel 已存在且校验通过，复用: %s（%.1f MB）",
                             pkg["name"], whl.stat().st_size / 1_000_000)
                else:
                    whl.unlink(missing_ok=True)
                    whl.with_suffix(".ok").unlink(missing_ok=True)
                    log.warning("CUDA wheel 已存在但 SHA256 不符，删除重下: %s",
                                pkg["name"])
            if not _verify_wheel(whl):
                state["source"] = label
                state["current_file"] = pkg["wheel"]
                state["file_index"] = i
                state["files"][i]["status"] = "downloading"
                state["total"] = pkg["size"] * 1_000_000
                state["downloaded"] = whl.stat().st_size if whl.exists() else 0
                _emit()

                def on_f(frac: float, _i=i) -> None:
                    overall = (_i + frac) / n
                    state["overall"] = overall
                    state["frac"] = overall
                    state["file_progress"] = frac
                    state["downloaded"] = int(frac * pkg["size"] * 1_000_000)
                    if progress:
                        progress(overall)
                    _emit()

                log.info("开始下载 CUDA 运行库 %s（约 %d MB）: %s",
                         pkg["name"], pkg["size"], url)
                download_file(url, whl, retries=8, progress=on_f,
                              cancel_check=cancel_check)
                # 完整性校验：非空 + SHA256（镜像索引官方哈希，权威判定），
                # 任一不符即删除损坏文件并整体失败（外层重试/切源）
                if whl.stat().st_size < 10 * 1024 * 1024:  # 最小体积防线（防截断为0）
                    whl.unlink(missing_ok=True)
                    whl.with_suffix(".ok").unlink(missing_ok=True)
                    raise RuntimeError(f"wheel 文件过小（疑似截断）: {whl.name}")
                actual = _sha256_file(whl)
                if sha and actual != sha:
                    whl.unlink(missing_ok=True)
                    whl.with_suffix(".ok").unlink(missing_ok=True)
                    raise RuntimeError(f"SHA256 校验失败: {pkg['name']}")
                log.info("CUDA wheel 校验通过: %s（%.1f MB，sha256=%s…）",
                         pkg["name"], whl.stat().st_size / 1_000_000, actual[:16])
                _mark_wheel(whl, sha or actual)

            _install_wheel(whl, NVIDIA_DIR)
            missing = [rel for rel in pkg["required"]
                       if not (NVIDIA_DIR / rel).is_file()]
            if missing:
                raise RuntimeError(
                    f"{pkg['name']} 解压后缺关键文件: {missing}")
            state["files"][i]["status"] = "done"
            state["overall"] = (i + 1) / n
            state["frac"] = state["overall"]
            log.info("CUDA 包就绪: %s（%d/%d）", pkg["name"], i + 1, n)
        except _DownloadCanceled:
            log.info("CUDA 运行库下载被用户取消")
            raise
        except Exception as e:  # noqa: BLE001
            log.error("CUDA 包 %s 下载/安装失败: %s", pkg["name"], e)
            raise RuntimeError(f"{pkg['name']} 下载失败: {e}") from e
        _emit()

    if not is_cuda_runtime_ready():
        raise RuntimeError("CUDA 运行库安装完成但就绪校验未通过（关键文件缺失）")
    save_cuda_manifest()
    log.info("CUDA 运行库全部就绪（共 3 个包，耗时 %.1fs）",
             time.monotonic() - t_all)
