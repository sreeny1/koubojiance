# -*- coding: utf-8 -*-
"""词级时间戳 + 去词闭环真机验证（需要本机 large-v3 模型，纯本地不联网）。

流程：
1. 真实转写 tests/media/test_video_1.mp4（统一 16kHz 预抽轨 + VAD + 词级时间戳）；
2. 校验 words 数据完整性（非空、时间单调、落在段内）；
3. 临时库跑检测器，校验命中时间与已知 SRT 时间戳接近（词级 ±1.5s）；
4. 复制视频到临时目录 → 按命中区间真实去词 → 校验产物时长 ≈ 原时长 − 去除总长；
5. 重新转写产物，确认违禁词已消失（去词真实生效，不是"复制了一份"）。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.config import load_settings  # noqa: E402
from core.cutter import cut_remove_ranges, hits_to_remove_ranges  # noqa: E402
from core.database import Database  # noqa: E402
from core.detector import Detector  # noqa: E402
from core.media import probe_duration_ms  # noqa: E402
from core.transcriber import WhisperEngine  # noqa: E402

MEDIA = Path(__file__).resolve().parent / "media" / "test_video_1.mp4"

# make_test_media.py 口播文案的真实时间参考（旧 SRT），容差 ±1.5s
EXPECTED_ROUGH = {
    "最好": (2.0, 5.5),
    "第一": (8.0, 12.0),
    "绝对": (13.0, 17.0),
    "根治": (15.0, 19.0),
    "加微信": (21.0, 26.5),
    "私聊": (22.0, 27.0),
}


def main() -> int:
    assert MEDIA.is_file(), f"测试视频不存在: {MEDIA}"
    settings = load_settings()
    engine = WhisperEngine(settings)
    ok = True

    # ---- 1. 真实转写 ----
    print(f"[1] 转写 {MEDIA.name}（large-v3，{engine._settings.get('device')}）…")
    segs = engine.transcribe(MEDIA)
    print(f"    转写 {len(segs)} 段:")
    for s in segs:
        print(f"    [{s['start']:6.2f} - {s['end']:6.2f}] {s['text']}")
    assert segs, "转写结果为空（VAD 重试也未产出）"

    # ---- 2. words 完整性 ----
    with_words = [s for s in segs if s.get("words")]
    print(f"[2] 携带词级时间戳的段: {len(with_words)}/{len(segs)}")
    if len(with_words) < len(segs) * 0.5:
        print("[✗] 词级时间戳覆盖率过低")
        ok = False
    for s in with_words:
        ws = s["words"]
        for a, b in zip(ws, ws[1:]):
            if b["s"] < a["s"] - 0.01:
                print(f"[✗] 词时间不单调: {s['text'][:20]}")
                ok = False
                break
        for w in ws:
            if not (s["start"] - 0.5 <= w["s"] <= s["end"] + 0.5):
                print(f"[✗] 词时间越界: {w} 不在段 [{s['start']},{s['end']}]")
                ok = False
                break
    if ok:
        print("    词时间单调、未越界: OK")
        sample = with_words[0]["words"][:6]
        print("    首段词样例:",
              [(w["cs"], round(w["s"], 2), round(w["e"], 2)) for w in sample])

    # ---- 3. 检测器：命中时间 vs 已知参考 ----
    db = Database(Path(tempfile.mkdtemp(prefix="wordts_e2e_")) / "app.db")
    det = Detector(db)
    hits_all = []
    for i, s in enumerate(segs):
        for h in det.scan_text(s["text"], int(s["start"] * 1000),
                               int(s["end"] * 1000), segment_id=i,
                               words=s.get("words")):
            hits_all.append(h)
    print(f"[3] 命中 {len(hits_all)} 处:")
    for h in hits_all:
        t0, t1 = h["start_ms"] / 1000, h["end_ms"] / 1000
        lo, hi = EXPECTED_ROUGH.get(h["word_text"], (0, 999))
        near = lo - 0.8 <= t0 <= hi + 0.8
        mark = "✓" if near else "✗"
        print(f"    [{mark}] {t0:6.2f}~{t1:6.2f}s {h['word_text']}"
              f" ← “{h['matched_text']}”")
        if not near:
            ok = False
            print(f"         期望区间 {lo}~{hi}s（词级时间应非常接近真实发音）")

    # ---- 4. 真实去词（临时目录，不动测试素材） ----
    tmp = Path(tempfile.mkdtemp(prefix="wordts_cut_"))
    src = tmp / MEDIA.name
    shutil.copy2(MEDIA, src)
    src_dur = probe_duration_ms(src) or 0
    ranges = hits_to_remove_ranges(hits_all, pad=0.3)
    total_remove = sum(t1 - t0 for t0, t1 in ranges)
    out = tmp / "cut_out.mp4"
    print(f"[4] 去词切割: {len(ranges)} 个区间（去除 {total_remove:.2f}s / 原 {src_dur/1000:.2f}s）…")
    cut_remove_ranges(src, ranges, out)
    out_dur = probe_duration_ms(out) or 0
    expected = src_dur / 1000 - total_remove
    print(f"    产物时长 {out_dur/1000:.2f}s（期望 ≈{expected:.2f}s）")
    if abs(out_dur / 1000 - expected) > 1.5:
        print("[✗] 产物时长校验失败（切割未生效）")
        ok = False
    else:
        print("    时长校验: OK")

    # ---- 5. 重转写产物：违禁词应消失 ----
    print("[5] 重新转写去词产物，验证违禁词已移除…")
    segs2 = engine.transcribe(out)
    text2 = " ".join(s["text"] for s in segs2)
    print(f"    产物字幕 {len(segs2)} 段: {text2[:120]}")
    banned = {"最好", "第一", "绝对", "根治", "加微信", "私聊"}
    det2 = Detector(db)
    remain = []
    for i, s in enumerate(segs2):
        for h in det2.scan_text(s["text"], int(s["start"] * 1000),
                                int(s["end"] * 1000), segment_id=1000 + i,
                                words=s.get("words")):
            if h["word_text"] in banned:
                remain.append((h["word_text"], round(h["start_ms"] / 1000, 2)))
    if remain:
        print(f"[✗] 去词后仍残留违禁词: {remain}")
        ok = False
    else:
        print("    违禁词已全部移除: OK（去词真实生效）")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("词级时间戳端到端验证 " + ("全部通过 ✓" if ok else "存在失败 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
