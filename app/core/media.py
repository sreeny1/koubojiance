"""媒体处理工具：定位 ffmpeg、探测时长、必要时抽取音轨。

faster-whisper 内置 PyAV 可直接读视频容器中的音轨，绝大多数情况无需
预先抽轨；仅当个别格式 PyAV 解不开时，用 ffmpeg 转 16k 单声道 WAV 兜底。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_FFMPEG: str | None = None


def get_ffmpeg() -> str:
    """返回项目内可用的 ffmpeg 可执行文件路径。

    优先级：环境变量 FFMPEG_PATH > imageio-ffmpeg 自带二进制 > 系统 PATH。
    imageio-ffmpeg 的二进制安装在项目 .venv 内，满足"文件不出项目"的约束。
    """
    global _FFMPEG
    if _FFMPEG:
        return _FFMPEG
    import os

    if os.environ.get("FFMPEG_PATH") and Path(os.environ["FFMPEG_PATH"]).exists():
        _FFMPEG = os.environ["FFMPEG_PATH"]
        return _FFMPEG
    try:
        import imageio_ffmpeg

        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception:  # noqa: BLE001
        # 最后退回系统 PATH 中的 ffmpeg（若存在）
        import shutil

        sys_ff = shutil.which("ffmpeg")
        if sys_ff:
            _FFMPEG = sys_ff
            return _FFMPEG
    raise RuntimeError(
        "未找到 ffmpeg：请确认 .venv 中已安装 imageio-ffmpeg，"
        "或设置环境变量 FFMPEG_PATH 指向 ffmpeg.exe"
    )


def probe_duration_ms(path: str | Path) -> int | None:
    """用 PyAV 快速探测媒体时长（毫秒）。失败返回 None（不抛异常）。"""
    try:
        import av  # faster-whisper 自带依赖

        with av.open(str(path)) as container:
            if container.duration is not None:
                return int(container.duration / 1000)  # av 时长单位是微秒
        return None
    except Exception:  # noqa: BLE001
        return None


def has_audio_stream(path: str | Path) -> bool:
    """媒体是否含音频轨。失败时保守返回 True（不阻断，交由后续流程报错）。"""
    try:
        import av

        with av.open(str(path)) as container:
            return any(s.type == "audio" for s in container.streams)
    except Exception:  # noqa: BLE001
        return True


def extract_audio_wav(src: str | Path, dst: str | Path) -> bool:
    """用 ffmpeg 抽取 16kHz 单声道 WAV。成功返回 True。

    仅作为 PyAV 解不开特殊格式时的兜底手段。
    """
    cmd = [
        get_ffmpeg(), "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le", str(dst),
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, timeout=1800,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0 and Path(dst).exists()
    except Exception:  # noqa: BLE001
        return False


def send_to_recycle_bin(path: str | Path) -> bool:
    """将文件移动到 Windows 回收站（可恢复），成功返回 True。

    通过 Shell 的 SHFileOperationW（FO_DELETE + FOF_ALLOWUNDO）实现，
    不依赖第三方库；文件被占用或删除失败时返回 False，绝不永久删除。
    """
    import ctypes
    from ctypes import wintypes

    # SHFILEOPSTRUCTW 结构体（32/64 位通用）
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", ctypes.c_uint),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    p = Path(path)
    if not p.exists():
        return True  # 文件已不存在，视为成功（无需再删）

    # 多路径用 \0 分隔，整体以双 \0 结尾
    p_from = str(p) + "\0\0"
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                     # FO_DELETE
    op.pFrom = p_from
    # FOF_ALLOWUNDO(进回收站) | FOF_SILENT(不显示进度) |
    # FOF_NOCONFIRMATION(不弹确认) | FOF_NOERRORUI(不弹错误框)
    op.fFlags = 0x40 | 0x04 | 0x10 | 0x400

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted
