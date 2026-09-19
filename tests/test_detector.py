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

# ---- 词级时间戳：命中时间应取词的精确时间而非线性内插 ----
# 场景：60 秒段中"第一"出现在文本中段；线性内插会把时间估到 ~35s，
# 而词级时间戳给出真实发音时间 12.0~12.8s
seg_text = "今天给大家介绍一款产品" + " blah" * 20 + "，全网销量第一，值得关注。"
seg_words = []
pos = 0
for token in ["今天给大家介绍一款产品"] + ["blah"] * 20 + ["，全网销量第一，值得关注。"]:
    seg_words.append({"cs": pos, "ce": pos + len(token), "s": 10.0, "e": 60.0})
    pos += len(token)
# 目标词"第一"的字符区间：精确词时间覆盖内插时间
target_cs = seg_text.find("第一")
seg_words.append({
    "cs": target_cs, "ce": target_cs + 2, "s": 12.0, "e": 12.8,
})

hits = det.scan_text(seg_text, 10_000, 60_000, segment_id=1, words=seg_words)
hit_first = next((h for h in hits if h["word_text"] == "第一"), None)
if hit_first:
    got = (hit_first["start_ms"], hit_first["end_ms"])
    print(f"[词级时间] '第一' 命中时间 {got[0]}~{got[1]}ms（词级真实 12000~12800ms）")
    if 11_000 <= got[0] <= 13_000 and 12_000 <= got[1] <= 13_500:
        print("[✓] 词级时间戳生效：命中时间取自词数据而非内插")
    else:
        all_ok = False
        print("[✗] 词级时间戳未生效，命中时间疑似走了内插")
else:
    all_ok = False
    print("[✗] 词级用例未命中'第一'")

# 无词数据回退：同一文本不给 words，"第一"位于文本后部（~89% 处），
# 内插时间应接近段尾（~55s+）——与词级时间 12s 相差 45s，
# 正是"内插剪偏、词级时间戳修复"的直观对比
hits2 = det.scan_text(seg_text, 10_000, 60_000, segment_id=2)
hit2 = next((h for h in hits2 if h["word_text"] == "第一"), None)
if hit2:
    print(f"[回退内插] 无词数据时'第一'命中时间 {hit2['start_ms']}~{hit2['end_ms']}ms"
          f"（预期 ~55000ms 附近，与词级 12000ms 相差 40s+ —— 内插误差示例）")
    if 50_000 <= hit2["start_ms"] <= 60_000:
        print("[✓] 无词数据正确回退线性内插（且凸显了内插的大误差）")
    else:
        all_ok = False
        print("[✗] 回退内插时间异常")

print()
print("检测器测试 " + ("全部通过 ✓" if all_ok else "存在失败 ✗"))
sys.exit(0 if all_ok else 1)
