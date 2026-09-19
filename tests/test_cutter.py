# -*- coding: utf-8 -*-
"""cutter 单元测试：区间合并 + 真实 ffmpeg 音画切割。

生成 10 秒测试视频（蓝色画面 + 1kHz 正弦音），去除 [2,4] 与 [6,8] 两段，
校验输出时长约 6 秒、产物存在。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.cutter import cut_remove_ranges, merge_ranges, hits_to_remove_ranges  # noqa: E402
from core.media import probe_duration_ms  # noqa: E402


def test_merge() -> None:
    assert merge_ranges([(2, 4), (3, 5), (8, 9)]) == [(2, 5), (8, 9)]
    assert merge_ranges([(1, 2), (2, 3)], gap=0.1) == [(1, 3)]
    assert merge_ranges([]) == []
    print("[1] 区间合并 OK")


def test_hits_to_ranges() -> None:
    hits = [{"start_ms": 2000, "end_ms": 3000}, {"start_ms": 2800, "end_ms": 3500}]
    ranges = hits_to_remove_ranges(hits, pad=0.3)
    assert ranges == [(1.7, 3.8)]
    tail = hits_to_remove_ranges([{"start_ms": 9500, "end_ms": 10000}], pad=0.3, duration=10.0)
    assert tail and all(0.0 <= a < b <= 10.0 for a, b in tail), tail
    print("[2] hits_to_ranges OK (tail clamped)")


def test_cut() -> None:
    import imageio_ffmpeg

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = Path(tempfile.mkdtemp(prefix="cutter_test_"))
    src = tmp / "src.mp4"
    out = tmp / "out.mp4"
    gen = [
        ff, "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=10:r=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=10",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(src),
    ]
    r = subprocess.run(gen, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]

    info = cut_remove_ranges(src, [(2.0, 4.0), (6.0, 8.0)], out)
    assert out.is_file() and out.stat().st_size > 0
    d = probe_duration_ms(out)
    assert d is not None and 5500 <= d <= 6500, f"输出时长异常：{d}ms"
    print(f"[3] 切割 OK：源参数 {info['width']}x{info['height']}@{info['fps']:.0f}fps "
          f"{info['video_codec']}/{info['audio_codec']}，输出时长 {d}ms")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_merge()
    test_hits_to_ranges()
    test_cut()
    print("\ncutter 测试全部通过 ✓")
