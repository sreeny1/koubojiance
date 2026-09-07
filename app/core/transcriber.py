"""faster-whisper 转写引擎封装。

稳定性设计：
- GPU 不可用 / 显存不足时自动降级：float16 → int8_float16 → int8(CPU)，
  保证程序在任何机器上都能跑起来；
- 模型下载支持 HuggingFace 官方源 → hf-mirror 镜像自动回退（国内网络友好）；
- CUDA 运行库（cuBLAS/cuDNN）从 .venv 内的 nvidia pip 包定位加载，
  不依赖系统环境；
- 转写过程支持进度回调与取消检查。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

from .config import MODELS_DIR

log = logging.getLogger("transcriber")


def _preload_onnxruntime() -> None:
    """规避系统目录中被劫持的 onnxruntime.dll。

    部分机器的 C:\\Windows\\System32 下存在旧版 onnxruntime.dll（全局安装
    的其他软件写入），Windows DLL 搜索顺序中 System32 优先于包目录，导致
    faster-whisper 的 VAD 依赖初始化失败（DLL 初始化例程失败，错误 1114）。

    解法：import onnxruntime 之前，用 ctypes 按完整路径把 venv 内正确的
    onnxruntime.dll 预先加载进进程——后续加载器按模块名直接复用它。
    """
    try:
        import ctypes
        import importlib.util

        spec = importlib.util.find_spec("onnxruntime")
        if spec is None or not spec.origin:
            return
        dll = Path(spec.origin).parent / "capi" / "onnxruntime.dll"
        if dll.is_file():
            ctypes.WinDLL(str(dll))
            log.info("已预加载 onnxruntime.dll: %s", dll)
    except Exception as e:  # noqa: BLE001  预加载失败不阻断启动，VAD 报错时再暴露
        log.warning("onnxruntime 预加载失败（忽略）: %s", e)


_preload_onnxruntime()


class TranscriptionCanceled(Exception):
    """用户取消了转写任务。"""


class WhisperEngine:
    """单例转写引擎：懒加载模型，线程安全。"""

    def __init__(self, settings: dict) -> None:
        self._settings = settings
        self._model = None
        self._lock = threading.Lock()
        # 转写全局互斥：同一时间内只允许一个视频做原生推理。
        # ctranslate2/faster-whisper 并发调用同一模型实例可能原生崩溃（无法被 try/except 捕获，
        # 会把整个服务进程带崩），尤其在 GPU 上多个任务并行时。串行化是最稳妥的保障。
        self._transcribe_lock = threading.Lock()
        # 模型下载协调：任意时刻至多一个线程在下载；同目标等待共享，不同目标串行
        self._dl_cond = threading.Condition()
        self._dl_target: str | None = None
        # 实际生效的配置（可能与请求配置不同，例如 GPU 降级后）
        self.effective = {"model": None, "device": None, "compute_type": None}
        self._setup_hf_endpoint()
        self._setup_cuda_dlls()

    @property
    def downloading(self) -> dict | None:
        """当前是否正在下载模型（供前端展示）。"""
        with self._dl_cond:
            if self._dl_target is None:
                # 有 .part 残留但未在下载也算"未就绪"（由调用方兜底续传）
                return None
            return {"name": self._dl_target, "active": True}

    # ------------------------------------------------------------------
    def _setup_hf_endpoint(self) -> None:
        ep = (self._settings.get("hf_endpoint") or "").strip()
        if ep:
            os.environ["HF_ENDPOINT"] = ep
            if "hf-mirror.com" in ep:
                self._bypass_system_proxy()

    @staticmethod
    def _bypass_system_proxy() -> None:
        """hf-mirror 是国内直连源；VPN/系统代理反而会劫持 TLS 导致下载失败。

        httpx 通过 urllib.request.getproxies() 读取 Windows 系统代理，
        此处将其替换为空实现，使模型下载强制直连。
        """
        import urllib.request

        urllib.request.getproxies = lambda: {}  # type: ignore[assignment]

    @staticmethod
    def _setup_cuda_dlls() -> None:
        """把 .venv 内 nvidia pip 包的 CUDA DLL 目录注册进搜索路径。

        两手准备：
        1. os.add_dll_directory：覆盖带 LOAD_LIBRARY_SEARCH_USER_DIRS 标志的加载；
        2. 前置到进程 PATH：cudnn64_9.dll 内部会用默认搜索路径加载其子组件
           （cudnn_ops64_9.dll 等），只有 PATH 方式对它生效。
        """
        try:
            import nvidia  # noqa: F401  由 nvidia-cublas-cu12 / nvidia-cudnn-cu12 提供
        except ImportError:
            return
        # nvidia 是命名空间包（__file__ 为 None），从 __path__ 取实际目录
        prepend: list[str] = []
        for pkg_path in getattr(nvidia, "__path__", []):
            base = Path(pkg_path)
            for sub in base.iterdir():
                for bin_name in ("bin", "lib"):
                    d = sub / bin_name
                    if d.is_dir():
                        os.add_dll_directory(str(d))
                        prepend.append(str(d))
        if prepend:
            os.environ["PATH"] = ";".join(prepend) + ";" + os.environ.get("PATH", "")

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    def _load_model_once(self, model_name: str, device: str, compute: str):
        """带 HF 镜像回退的模型加载。

        只有在模型需"在线下载"时才做镜像回退；本地已下载模型的加载失败
        （如词表缺失）属于模型/文件问题，切镜像重试同一本地路径毫无意义，还误导日志。
        """
        from faster_whisper import WhisperModel

        # model_name 是已解析结果：本地目录路径 / 在线模型名
        is_local = Path(model_name).is_dir()
        try:
            return WhisperModel(
                model_name, device=device, compute_type=compute,
                download_root=str(MODELS_DIR),
            )
        except Exception as e:  # noqa: BLE001
            if not is_local and self._switch_to_mirror():
                log.warning("官方源下载失败，已切换 hf-mirror 镜像重试: %s", e)
                return WhisperModel(
                    model_name, device=device, compute_type=compute,
                    download_root=str(MODELS_DIR),
                )
            raise

    def _switch_to_mirror(self) -> bool:
        """把 huggingface_hub 端点切换到国内镜像（并绕过系统代理直连）。"""
        if os.environ.get("HF_ENDPOINT") == "https://hf-mirror.com":
            return False
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        self._bypass_system_proxy()
        try:
            import huggingface_hub.constants as hc

            hc.ENDPOINT = "https://hf-mirror.com"
        except Exception:  # noqa: BLE001
            pass
        return True

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_model_path(name: str) -> str:
        """优先使用本地已下载的模型目录 data/models/local/<name>。

        模型缺失时由 ensure_model() 自动从国内源下载到该目录；
        不存在（下载未就绪）时返回原名，交给 huggingface_hub 在线下载（带镜像回退）。
        """
        from .model_downloader import local_model_path

        local = local_model_path(name)
        if (local / "model.bin").is_file():
            return str(local)
        return name

    # ------------------------------------------------------------------
    def is_model_ready(self, name: str | None = None) -> bool:
        """本地模型是否已就绪（首次使用前引擎检查）。"""
        from .model_downloader import is_model_ready as _rdy

        return _rdy(name or self._settings.get("model", "large-v3"))

    def ensure_model(self, name: str | None = None,
                     progress: Callable[[float], None] | None = None,
                     cancel_check: Callable[[], bool] | None = None,
                     state_cb: Callable[[dict], None] | None = None) -> None:
        """确保模型已下载到本地（全自动，国内源优先）。

        - 已就绪：直接返回；- 尚未下载：自动断点续传下载；
        - 并发安全：至多一个线程在下载；同目标等待共享，不同目标串行等待。
        - state_cb：可选，接收"每文件详情 + 进度"的状态字典，供前端展示下载界面。
        """
        from .model_downloader import download_model

        target = name or self._settings.get("model", "large-v3")
        if self.is_model_ready(target):
            if progress:
                progress(1.0)
            if state_cb:
                state_cb({"name": target, "ready": True})
            return

        with self._dl_cond:
            # 等其他线程完成手上的下载（无论目标是否相同）
            while self._dl_target is not None:
                self._dl_cond.wait()
            # 等待期间可能已被别人下载完成
            if self.is_model_ready(target):
                if progress:
                    progress(1.0)
                if state_cb:
                    state_cb({"name": target, "ready": True})
                return
            self._dl_target = target
        try:
            from .model_downloader import _DownloadCanceled

            download_model(target, progress=progress, cancel_check=cancel_check,
                           state_cb=state_cb)
        except _DownloadCanceled:
            raise TranscriptionCanceled() from None
        finally:
            with self._dl_cond:
                self._dl_target = None
                self._dl_cond.notify_all()

    def get_model(self):
        """获取（或首次加载）whisper 模型，带自动降级链。"""
        with self._lock:
            if self._model is not None:
                return self._model

            name = self._settings.get("model", "large-v3")
            model_path = self._resolve_model_path(name)
            device = self._settings.get("device", "auto")
            compute = self._settings.get("compute_type", "int8_float16")
            if device == "auto":
                device = "cuda" if self._cuda_available() else "cpu"

            # 降级链：请求配置 → 更保守的 GPU 配置 → CPU
            chain: list[tuple[str, str]] = []
            if device == "cuda":
                chain.append((device, compute))
                if compute != "int8_float16":
                    chain.append(("cuda", "int8_float16"))
                chain.append(("cuda", "int8"))
            chain.append(("cpu", "int8"))

            last_err: Exception | None = None
            for dev, comp in chain:
                try:
                    log.info("加载模型 %s (%s/%s)", name, dev, comp)
                    self._model = self._load_model_once(model_path, dev, comp)
                    self.effective = {
                        "model": name, "device": dev, "compute_type": comp,
                    }
                    return self._model
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    log.warning("模型加载失败 %s/%s: %s，尝试降级", dev, comp, e)
            raise RuntimeError(f"模型加载失败（已尝试全部降级组合）: {last_err}")

    def reload(self, settings: dict) -> None:
        """设置变更后重载模型。"""
        with self._lock:
            self._settings = settings
            self._model = None  # 下次 get_model 时按新配置加载
            self._setup_hf_endpoint()

    # ------------------------------------------------------------------
    def transcribe(
        self,
        media_path: str | Path,
        progress: Callable[[float], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[dict]:
        """转写单个媒体文件，返回字幕段列表。

        返回格式：[{"start": 秒, "end": 秒, "text": str}, ...]
        """
        from .media import extract_audio_wav, has_audio_stream, probe_duration_ms

        src = str(media_path)
        duration = probe_duration_ms(src)

        # 无音频轨的视频（如纯画面/无声音素材）无法转写语音，直接视为"无语音内容"，
        # 返回空字幕而非抛异常（界面会显示"未识别到语音内容"，而不是"转写失败"）。
        # 放在加载模型之前，避免为这类视频白白加载数 GB 模型。
        if not has_audio_stream(src):
            log.info("媒体无音轨，跳过转写：%s", src)
            return []

        with self._transcribe_lock:
            model = self.get_model()
            log.info("开始转写：%s（时长 %sms，设备 %s/%s）",
                     src, duration or 0, self.effective.get("device"),
                     self.effective.get("compute_type"))

            lang = self._settings.get("language", "zh")
            kwargs: dict = dict(
                language=None if lang == "auto" else lang,
                beam_size=5,
                vad_filter=True,                # 跳过静音/背景音乐段
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,  # 防止坏音频引起重复循环
            )

            try:
                segments_iter, info = model.transcribe(src, **kwargs)
            except Exception as e:  # noqa: BLE001
                # 个别格式 PyAV 解不开时，用 ffmpeg 抽轨后重试一次
                log.warning("直接转写失败(%s)，尝试 ffmpeg 抽音轨: %s", src, e)
                from .config import DATA_DIR

                wav = DATA_DIR / "media" / f"_tmp_{Path(src).stem[:50]}.wav"
                if not extract_audio_wav(src, wav):
                    raise RuntimeError(f"音轨解析失败: {e}")
                try:
                    segments_iter, info = model.transcribe(str(wav), **kwargs)
                finally:
                    wav.unlink(missing_ok=True)

            if duration is None:
                duration = int((info.duration or 0) * 1000)

            result: list[dict] = []
            for seg in segments_iter:  # 生成器：边转写边产出
                if cancel_check and cancel_check():
                    raise TranscriptionCanceled()
                result.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
                if progress and duration > 0:
                    progress(min(0.99, seg.end * 1000 / duration))
            if progress:
                progress(1.0)
            return split_oversized_segments(result)


# --------------------------------------------------------------------
# 字幕句级切分
# --------------------------------------------------------------------
# whisper 的 segment 粒度通常是 5~10 秒一段（含多个分句），偶发跑偏时
# 甚至整条视频一段。用户需要句级字幕（每句独立时间点，点击可跳转），
# 因此：超过 _SPLIT_MAX_SEC 秒且含 ≥2 个分句的段按标点切分，
# 子句时间戳按文字占比线性插值（段内误差通常 1~2 秒内）。
_SPLIT_MAX_SEC = 5.0            # 单段超过该时长且多句时触发切分
_MIN_SUB_LEN = 6                # 切出的子句最短字符数，防止切太碎
# 句末/逗号级标点。注意：whisper 跑偏时中文常输出半角标点（, .），
# 所以半角逗号/句号也必须算切分点，否则坏数据切不开。
_SENT_SPLIT_RE = r"(?<=[。！？；，,!?;…．.])"


def split_oversized_segments(segments: list[dict]) -> list[dict]:
    """对超长 segment 按标点二次切分，时间戳线性插值。"""
    out: list[dict] = []
    for seg in segments:
        dur = seg["end"] - seg["start"]
        if dur <= _SPLIT_MAX_SEC or len(seg["text"]) <= _MIN_SUB_LEN:
            out.append(seg)
            continue
        subs = _split_text_into_sentences(seg["text"])
        if len(subs) <= 1:
            out.append(seg)
            continue
        # 按各子句字符数占比分配时长
        total_chars = sum(len(s) for s in subs)
        t = seg["start"]
        for s in subs:
            frac = len(s) / total_chars
            out.append({
                "start": t,
                "end": min(seg["end"], t + dur * frac),
                "text": s,
            })
            t += dur * frac
    return out


def _split_text_into_sentences(text: str) -> list[str]:
    """把长文本按句末标点切成句子列表（无标点时按长度硬切）。"""
    import re

    # 句末标点后断开（保留标点在句尾）
    parts = re.split(_SENT_SPLIT_RE, text)
    # 过滤空串，合并过短的碎片到前一句
    merged: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if merged and len(merged[-1]) < _MIN_SUB_LEN:
            merged[-1] += p
        else:
            merged.append(p)
    # 无任何标点切不开时，按 ~30 字硬切
    if len(merged) == 1 and len(merged[0]) > 60:
        s = merged[0]
        merged = [s[i:i + 30] for i in range(0, len(s), 30)]
    return merged


# --------------------------------------------------------------------
def format_timestamp_srt(seconds: float) -> str:
    """秒 → SRT 时间戳 00:00:01,500"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(
            f"{format_timestamp_srt(seg['start'])} --> {format_timestamp_srt(seg['end'])}"
        )
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)
