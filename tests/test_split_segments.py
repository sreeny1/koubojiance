# -*- coding: utf-8 -*-
"""测试超长段切分函数 split_oversized_segments。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.transcriber import split_oversized_segments  # noqa: E402

# 用例1：用户实际遇到的坏数据（整条视频一大段，半角标点）
bad = [{
    "start": 0.0, "end": 60.0,
    "text": ("这就是华为全新发布的这只手表,华为的Watch4 Pro,"
             "手表48毫米的蓝宝石镜面,钛金属的包边,原厂的钛金属表带,"
             "看起来颜值太棒了."),
}]
fixed = split_oversized_segments(bad)
print("用例1 坏数据修复:")
for s in fixed:
    print(f"  [{s['start']:6.2f} - {s['end']:6.2f}] {s['text']}")
print("段数:", len(fixed))
assert len(fixed) > 1, "坏数据未被切分"
print()

# 用例2：短段（<5秒）不受影响
normal = [{"start": 0.0, "end": 4.9, "text": "正常短段"}]
assert split_oversized_segments(normal) == normal, "短段被误改"
print("用例2 短段不受影响: OK")
print()

# 用例2b：10 秒多句段按句切分（用户实际场景：句级分割）
seg10 = [{
    "start": 0.0, "end": 10.7,
    "text": ("这就是华为全新发布的这只手表,华为的Watch4 Pro,"
             "手表48毫米的蓝宝石镜面,钛金属的包边,原厂的钛金属表带,"
             "看起来颜值太棒了."),
}]
fixed2b = split_oversized_segments(seg10)
print(f"用例2b 10秒多句段切分: {len(fixed2b)} 段")
for s in fixed2b:
    print(f"  [{s['start']:5.2f} - {s['end']:5.2f}] {s['text']}")
assert len(fixed2b) >= 5, "10秒多句段未按句切分"
for a, b in zip(fixed2b, fixed2b[1:]):
    assert abs(a["end"] - b["start"]) < 1e-9, "时间戳不连续"
print("  时间戳连续性: OK")
print()

# 用例2c：长段但单句（无标点、文字<60）不切
single = [{"start": 0.0, "end": 30.0, "text": "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"}]
assert split_oversized_segments(single) == single, "单句段被误切"
print("用例2c 单句段不切: OK")
print()

# 用例3：超长段但无标点（硬切兜底）
nopunc = [{
    "start": 10.0, "end": 70.0,
    "text": "今天我们来讲一讲这个产品它有三大优点第一点是续航长"
            "第二点是屏幕好第三点是价格实惠大家听懂了吗" * 2,
}]
fixed3 = split_oversized_segments(nopunc)
print(f"用例3 无标点硬切: {len(fixed3)} 段")
assert all(s["end"] <= 70.0001 for s in fixed3), "时间越界"
assert fixed3[0]["start"] == 10.0 and fixed3[-1]["end"] == 70.0, "首尾时间戳错误"
print("  时间戳边界: OK")
print()

# 用例4：时间戳连续性（后一段 start == 前一段 end）
for a, b in zip(fixed, fixed[1:]):
    assert abs(a["end"] - b["start"]) < 1e-9, "时间戳不连续"
print("用例4 时间戳连续性: OK")
print()

# 用例5：正常段与坏段混合，只切坏段
mixed = [
    {"start": 0.0, "end": 8.0, "text": "开头正常段"},
    {"start": 8.0, "end": 80.0, "text": "这一段是超长的一段,包含多个句子.第一句.第二句.第三句.第四句.第五句."},
    {"start": 80.0, "end": 90.0, "text": "结尾正常段"},
]
fixed5 = split_oversized_segments(mixed)
assert fixed5[0] == mixed[0], "开头正常段被改"
assert fixed5[-1] == mixed[-1], "结尾正常段被改"
assert len(fixed5) > 3, "中间超长段未切分"
print(f"用例5 混合场景: OK（{len(fixed5)} 段，正常段原样保留）")

# 用例6：词级时间戳——切分后子句时间取词时间、词正确归属子句
wseg = [{
    "start": 0.0, "end": 12.0,
    "text": "这是第一句话，这是第二句话。",
    "words": [
        {"cs": 0, "ce": 6, "s": 0.5, "e": 2.9},     # 这是第一句话，
        {"cs": 7, "ce": 13, "s": 3.5, "e": 5.8},    # 这是第二句话。
    ],
}]
fixed6 = split_oversized_segments(wseg)
print("用例6 词级时间切分:")
for s in fixed6:
    print(f"  [{s['start']:5.2f} - {s['end']:5.2f}] {s['text']}  words={s['words']}")
assert len(fixed6) == 2, "词级用例未按句切分"
assert abs(fixed6[0]["start"] - 0.5) < 0.01 and abs(fixed6[0]["end"] - 2.9) < 0.01, \
    "第一子句时间未取词级时间"
assert abs(fixed6[1]["start"] - 3.5) < 0.01 and abs(fixed6[1]["end"] - 5.8) < 0.01, \
    "第二子句时间未取词级时间"
assert fixed6[0]["words"] and fixed6[0]["words"][0]["cs"] == 0, "词未归属到第一子句"
assert fixed6[1]["words"] and fixed6[1]["words"][0]["cs"] == 0, "词未归属到第二子句（本地坐标）"
print("  词级时间戳切分: OK")

print()
print("全部用例通过")
