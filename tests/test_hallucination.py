# -*- coding: utf-8 -*-
"""幻觉字幕过滤单元测试：纯函数级（不依赖模型）。"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.transcriber import _is_hallucination  # noqa: E402

# (文本, no_speech_prob, 是否应为幻觉)
cases = [
    # ---- 用户实测截图中的真实幻觉（必须过滤）----
    ("字幕由 Amara.org 社群提供", 0.0, True),
    ("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目", 0.0, True),
    ("中文字幕提供", 0.0, True),
    ("MING PAO CANADA // MING PAO TORONTO", 0.0, True),
    ("字幕由Amara.org社群提供", 0.0, True),          # 无空格变体
    ("请不吝点赞订阅转发打赏支持明镜与点点栏目", 0.0, True),
    ("Subtitles by the Amara.org community", 0.0, True),
    ("Thanks for watching!", 0.0, True),
    ("字幕製作", 0.0, True),                            # 繁体
    # ---- 极短 + 高无语音概率（音乐段幻觉信号）----
    ("嗯嗯嗯嗯", 0.95, True),
    ("啊。", 0.92, True),
    # ---- 真实口播（绝不能误删）----
    ("在老弟直播间价格便宜,而且还包邮给您。", 0.0, False),
    ("姐姐我们家的新鲜紫皮独头蒜成熟了,生吃炒菜腌腊八蒜都很香。", 0.0, False),
    ("今天天气不错，我出门买了个西瓜。", 0.0, False),
    ("感谢大家观看我们的节目", 0.0, False),            # 日常用语，非水印
    ("这款产品的价格特别划算", 0.5, False),
    # 长句即使包含关键词也不删（真实口播引用水印文本的极端情况）
    ("我们这个视频的字幕是由我们团队自己一个字一个字打出来的，绝对不是机器生成的", 0.0, False),
    # 短句但 no_speech_prob 不高 → 保留
    ("好的。", 0.3, False),
    # 空文本
    ("", 0.0, True),
    ("   ", 0.0, True),
]

all_ok = True
for text, prob, expect in cases:
    got = _is_hallucination(text, prob)
    mark = "✓" if got == expect else "✗"
    if got != expect:
        all_ok = False
    print(f"[{mark}] {_is_hallucination.__name__}({text[:28]!r}, {prob}) = {got}"
          f"（期望 {expect}）")

print()
print("幻觉过滤测试 " + ("全部通过 ✓" if all_ok else "存在失败 ✗"))
sys.exit(0 if all_ok else 1)
