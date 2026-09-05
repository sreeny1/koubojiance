# -*- coding: utf-8 -*-
"""检测器端到端逻辑测试：归一化层 + 拼音谐音层 + 时间内插。"""
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.database import Database  # noqa: E402
from core.detector import Detector  # noqa: E402

# 独立临时库，绝不污染正式 data/app.db（正式库由种子词库建库）
db = Database(Path(tempfile.mkdtemp(prefix="detector_test_")) / "app.db")
det = Detector(db)

cases = [
    # (字幕文本, 期望命中的词列表)
    ("我们的产品全网销量第一，绝对值得信赖。", ["第一", "绝对"]),
    ("这款面霜效果蕞好用，用完皮肤超好。", ["最好"]),          # 谐音：蕞→最
    ("想了解的宝子加微信私聊我哦。", ["加微信", "私聊"]),
    ("这个投资项目稳赚不赔，零风险躺赚。", ["稳赚不赔", "零风险", "躺赚"]),
    ("今天天气不错，我出门买了个西瓜。", []),                  # 无违禁词
    ("第 一 名，品质保证。", ["第一"]),                         # 分词规避
]

all_ok = True
for text, expected in cases:
    hits = det.scan_text(text, 10_000, 15_000, segment_id=0)
    got_words = sorted({h["word_text"] for h in hits})
    exp_words = sorted(expected)
    ok = all(w in got_words for w in exp_words)
    status = "✓" if ok else "✗"
    print(f"[{status}] {text[:24]}... → 命中: {got_words}")
    for h in hits:
        print(f"      - 词[{h['word_text']}] 原文[{h['matched_text']}] "
              f"分类[{h['category_name']}] 时间 {h['start_ms']}~{h['end_ms']}ms")
    if not ok:
        all_ok = False
        print(f"      期望包含: {exp_words}")

print()
print("检测器测试 " + ("全部通过 ✓" if all_ok else "存在失败 ✗"))
sys.exit(0 if all_ok else 1)
