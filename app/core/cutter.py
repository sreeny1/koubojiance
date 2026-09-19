"""ffmpeg 精确去除片段（音画同步裁剪拼接）。

需求：用户勾选若干违禁词命中后，把对应时间片段的画面与声音一起剪掉，
剩余片段无缝拼接，输出参数与原视频保持一致（分辨率/帧率/像素格式/编码族/
音频采样率与声道数），文件名保持不变（调用方先备份再覆盖）。

实现方式：
- 用 PyAV（faster-whisper 自带依赖）探测原视频参数，不依赖 ffprobe；
- 用 ffmpeg 的 select/aselect 表达式一次性丢弃"删除区间"，无需逐段落盘再
  concat，速度快、能处理任意多个区间；
- 重编码：视频默认 libx264(CRF 保画质) + faststart，音频 aac 匹配原采样率/
  声道/码率。精确到秒必然要重编码（流复制只能切关键帧，误差大）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from .media import get_ffmpeg

log = logging.getLogger("cutter")


class CutCanceled(Exception):
    """用户取消了去词切割任务。"""


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------
def probe_media(path: str | Path) -> dict:
    """探测媒体参数，返回 dict（缺失字段为 None）。"""
    import av

    info: dict = {
        "duration_sec": None,
        "has_video": False,
        "has_audio": False,
        "width": None,
        "height": None,
        "fps": None,
        "pix_fmt": None,
        "video_codec": None,
        "video_bitrate": None,
        "audio_codec": None,
        "sample_rate": None,
        "channels": None,
        "audio_bitrate": None,
    }
    log.debug("探测媒体参数: %s", path)
    with av.open(str(path)) as c:
        if c.duration is not None:
            info["duration_sec"] = c.duration / 1_000_000  # av 时长单位：微秒
        v = next((s for s in c.streams if s.type == "video"), None)
        a = next((s for s in c.streams if s.type == "audio"), None)
        if v is not None:
            info["has_video"] = True
            info["width"] = v.width
            info["height"] = v.height
            rate = v.average_rate or v.guessed_rate or v.base_rate
            if rate is not None:
                try:
                    info["fps"] = float(rate)
                except (TypeError, ValueError):
                    info["fps"] = None
            info["pix_fmt"] = getattr(v, "pix_fmt", None)
            ctx = getattr(v, "codec_context", None)
            if ctx is not None:
                info["video_codec"] = getattr(ctx, "name", None) or getattr(v.codec, "name", None)
                info["video_bitrate"] = getattr(ctx, "bit_rate", None)
            else:
                info["video_codec"] = getattr(v.codec, "name", None)
        if a is not None:
            info["has_audio"] = True
            ctx = getattr(a, "codec_context", None)
            info["audio_codec"] = (getattr(ctx, "name", None) if ctx else None) or getattr(a.codec, "name", None)
            if ctx is not None:
                info["sample_rate"] = getattr(ctx, "sample_rate", None)
                info["channels"] = getattr(ctx, "channels", None)
                info["audio_bitrate"] = getattr(ctx, "bit_rate", None)
    log.debug("媒体参数: %s", info)
    return info


# ----------------------------------------------------------------------
# 区间处理
# ----------------------------------------------------------------------
def merge_ranges(ranges: list[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    """合并重叠/相邻的 [t0, t1] 区间（gap 秒以内也视为连续）。"""
    if not ranges:
        return []
    clean = []
    for t0, t1 in ranges:
        t0, t1 = float(t0), float(t1)
        if t1 <= t0:
            continue
        clean.append((min(t0, t1), max(t0, t1)))
    clean.sort()
    out: list[tuple[float, float]] = []
    for t0, t1 in clean:
        if out and t0 <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], t1))
        else:
            out.append((t0, t1))
    return out


def hits_to_remove_ranges(hits: list[dict], pad: float = 0.3) -> list[tuple[float, float]]:
    """把命中列表（含 start_ms/end_ms）转成要去除的秒级区间（前后加缓冲）。

    每个区间保证最短 0.2s：命中时间戳插值为零长（词在段首且单字）时，
    pad=0 的调用也不会产出无效区间（trim 同点无效果，等于没剪）。
    """
    pad = max(0.0, float(pad))
    ranges = []
    for h in hits:
        t0 = float(h["start_ms"]) / 1000 - pad
        t1 = float(h["end_ms"]) / 1000 + pad
        if t0 < 0:
            t0 = 0.0
        if t1 - t0 < 0.2:
            t1 = t0 + 0.2
        ranges.append((t0, t1))
    return merge_ranges(ranges)


def _keep_intervals(remove_ranges: list[tuple[float, float]], duration: float | None) -> list[tuple[float, float | None]]:
    """由去除区间反推保留区间（end 为 None 表示一直到结尾）。"""
    ranges = merge_ranges(remove_ranges)
    if not ranges:
        return [(0.0, None)]
    keep: list[tuple[float, float | None]] = []
    cursor = 0.0
    for t0, t1 in ranges:
        t0 = max(0.0, t0)
        if duration:
            t1 = min(duration, t1)
        if t0 > cursor + 1e-6:
            keep.append((cursor, t0))
        cursor = max(cursor, t1)
    if duration:
        if cursor < duration - 1e-6:
            keep.append((cursor, duration))
    else:
        keep.append((cursor, None))
    return keep


def _build_filtergraph(keep: list[tuple[float, float | None]], has_audio: bool) -> str:
    """用 trim/atrim + concat 精确拼接（确定性优于 select 布尔组合）。"""
    n = len(keep)
    if n == 1:
        s, e = keep[0]
        end = f":end={e:.3f}" if e is not None else ""
        chains = [f"[0:v]trim=start={s:.3f}{end},setpts=PTS-STARTPTS[vout]"]
        if has_audio:
            chains.append(f"[0:a]atrim=start={s:.3f}{end},asetpts=PTS-STARTPTS[aout]")
        return ";".join(chains)

    vchains: list[str] = []
    achains: list[str] = []
    for i, (s, e) in enumerate(keep):
        end = f":end={e:.3f}" if e is not None else ""
        vchains.append(f"[0:v]trim=start={s:.3f}{end},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            achains.append(f"[0:a]atrim=start={s:.3f}{end},asetpts=PTS-STARTPTS[a{i}]")

    chains = vchains + achains
    chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    if has_audio:
        chains.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[aout]")
    return ";".join(chains)


# ----------------------------------------------------------------------
# 切割
# ----------------------------------------------------------------------
def cut_remove_ranges(
    src: str | Path,
    remove_ranges: list[tuple[float, float]],
    dst: str | Path,
    progress: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    """把 src 中 remove_ranges 指定的区间剪掉，其余拼接写入 dst。

    返回探测到的源参数（调用方可用于日志/校验）。
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_file():
        raise FileNotFoundError(f"源文件不存在：{src}")
    ranges = merge_ranges(remove_ranges)
    if not ranges:
        raise ValueError("没有需要去除的时间区间")

    info = probe_media(src)
    if not info["has_video"]:
        raise ValueError("该文件没有视频轨道，无法去除画面")

    total = info.get("duration_sec") or 0.0
    keep = _keep_intervals(ranges, info.get("duration_sec"))
    if not keep:
        raise ValueError("去除区间覆盖了整段视频，没有可保留的内容")
    log.info("去词切割参数: 源时长=%.2fs，去除区间=%s，保留区间=%s",
             total, ranges, keep)

    ffmpeg = get_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", str(src)]
    fc = _build_filtergraph(keep, bool(info["has_audio"]))
    log.debug("filter_complex: %s", fc)
    maps: list[str] = ["-map", "[vout]"]
    if info["has_audio"]:
        maps += ["-map", "[aout]"]
    cmd += ["-filter_complex", fc]
    cmd += maps

    # 视频编码：与原编码族保持一致（mp4 绝大多数是 h264）
    vcodec = (info.get("video_codec") or "").lower()
    if vcodec in ("hevc", "h265", "libx265"):
        enc = "libx265"
        cmd += ["-c:v", enc, "-preset", "veryfast", "-crf", "23", "-tag:v", "hvc1"]
    else:
        enc = "libx264"
        cmd += ["-c:v", enc, "-preset", "veryfast", "-crf", "20"]
    if info.get("pix_fmt"):
        cmd += ["-pix_fmt", str(info["pix_fmt"])]
    # 输出帧率：仅在探测值合理（1~300fps）时固定，防止个别容器把
    # average_rate 探成 1000fps 之类异常值导致产物时长/帧数错乱
    fps = info.get("fps")
    if fps and 1 < fps < 300:
        cmd += ["-r", f"{fps:.6f}".rstrip("0").rstrip(".")]

    # 音频：aac 匹配原参数（无音频则跳过）
    if info["has_audio"]:
        ab = info.get("audio_bitrate") or 192_000
        cmd += ["-c:a", "aac", "-b:a", str(int(ab))]
        if info.get("sample_rate"):
            cmd += ["-ar", str(int(info["sample_rate"]))]
        if info.get("channels"):
            cmd += ["-ac", str(int(info["channels"]))]

    cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", "-loglevel", "error", str(dst)]

    log.info("ffmpeg 切割：%d 个去除区间（源时长 %.1fs）→ %s", len(ranges), total, dst)
    # 完整命令行记录：参数一致性出问题时可直接复现
    log.info("ffmpeg 命令: %s", subprocess.list2cmdline(cmd))
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    last_frac = 0.0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_check and cancel_check():
                raise CutCanceled()
            if line.startswith("out_time="):
                try:
                    t = _parse_out_time(line[len("out_time="):].strip())
                except ValueError:
                    continue
                if total > 0 and progress:
                    frac = min(1.0, max(0.0, t / total))
                    if frac - last_frac >= 0.01 or frac >= 1.0:
                        last_frac = frac
                        log.debug("ffmpeg 进度 %.0f%%（out_time=%s）", frac * 100, t)
                        progress(frac)
        # 读取剩余 stderr（避免管道写满阻塞）
        err = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
        log.info("ffmpeg 切割结束: 退出码=%s，耗时 %.1fs", code, time.monotonic() - t0)
    except CutCanceled:
        _terminate(proc)
        dst.unlink(missing_ok=True)
        raise
    except Exception:
        _terminate(proc)
        dst.unlink(missing_ok=True)
        raise

    if code != 0:
        dst.unlink(missing_ok=True)
        log.error("ffmpeg 切割失败（退出码 %s），stderr 尾部: %s", code, err[-800:])
        raise RuntimeError(f"ffmpeg 切割失败（退出码 {code}）：{err[-800:]}")

    if not dst.is_file() or dst.stat().st_size == 0:
        raise RuntimeError("ffmpeg 切割产物为空（可能所有区间均被去除）")

    # ---- 产物时长校验：ffmpeg 退出码 0 ≠ 剪辑生效 ----
    # 极端情况下（filter 未按预期工作/时间基异常）会产出与原片几乎等长的
    # "复制件"，直接覆盖原文件等于没剪。校验：产物时长 ≈ 原时长 - 去除总时长，
    # 偏差超过 1.5s 即判失败，绝不覆盖原文件。
    from .media import probe_duration_ms

    out_dur_ms = probe_duration_ms(dst)
    if total > 0 and out_dur_ms:
        total_remove = sum(t1 - t0 for t0, t1 in ranges)
        expected_sec = total - total_remove
        if abs(out_dur_ms / 1000 - expected_sec) > 1.5:
            dst.unlink(missing_ok=True)
            log.error("切割产物时长校验失败: 预期约 %.2fs，实际 %.2fs"
                      "（原 %.2fs，去除 %.2fs）",
                      expected_sec, out_dur_ms / 1000, total, total_remove)
            raise RuntimeError(
                f"切割产物时长异常（预期约 {expected_sec:.1f}s，"
                f"实际 {out_dur_ms / 1000:.1f}s），已中止且未改动原文件。"
                "请重试或反馈日志给开发者。"
            )

    out_size = dst.stat().st_size
    if progress:
        progress(1.0)
    log.info("ffmpeg 切割产物: %s（%.1f MB，原 %.1f MB）",
             dst, out_size / 1048576,
             src.stat().st_size / 1048576 if src.is_file() else 0)
    return info


def _parse_out_time(s: str) -> float:
    """'HH:MM:SS.micro' → 秒。"""
    h, m, rest = s.split(":")
    sec = float(rest)
    return int(h) * 3600 + int(m) * 60 + sec


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 替换目标文件（清只读 + 重试，规避 WinError 5 占用/只读）
# ----------------------------------------------------------------------
def replace_over_target(tmp: str | Path, dst: str | Path, attempts: int = 8) -> None:
    """把 tmp 原子替换为 dst（原名）。

    Windows 上 MoveFileEx 替换目标时若目标被其它进程占用（播放器/缩略图/
    杀毒/同步工具）或带只读属性，会抛 [WinError 5] 拒绝访问。此处：
    1. 先清只读属性；2. 退避重试（占用多为瞬时）；3. 仍失败给出可操作提示。
    """
    import stat
    import time

    tmp, dst = Path(tmp), Path(dst)

    def _clear_readonly(p: Path) -> None:
        try:
            if p.is_file() and (p.stat().st_mode & stat.S_IREAD):
                os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass

    _clear_readonly(dst)
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except (PermissionError, OSError) as e:  # noqa: BLE001  WinError5=PermissionError
            last = e
            _clear_readonly(dst)  # 每轮再清一次（防又被置回）
            time.sleep(0.3 * (i + 1))
    raise RuntimeError(
        f"替换原文件失败（可能被占用或只读）：{dst}\n"
        f"请关闭正在使用该视频的程序（内置播放器/预览/杀毒/同步软件）后重试。"
        f"\n原始错误：{last}"
    )


# ----------------------------------------------------------------------
# 备份
# ----------------------------------------------------------------------
def backup_original(path: str | Path) -> Path:
    """把原文件复制到同目录备份（时间戳后缀），返回备份路径。"""
    src = Path(path)
    if not src.is_file():
        raise FileNotFoundError(f"源文件不存在：{src}")
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = src.with_name(f"{src.stem}_去词前备份_{stamp}{src.suffix}")
    n = 1
    while backup.exists():
        backup = src.with_name(f"{src.stem}_去词前备份_{stamp}_{n}{src.suffix}")
        n += 1
    import shutil

    shutil.copy2(src, backup)
    log.info("已备份原文件: %s → %s（%.1f MB）",
             src, backup, backup.stat().st_size / 1048576)
    return backup
