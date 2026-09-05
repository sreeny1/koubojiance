# -*- coding: utf-8 -*-
"""快速自检：模块导入 + 检测器归一化/拼音逻辑单元测试（不依赖数据库）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

# 模块导入自检
import main  # noqa: F401
print("[1] main.py 导入成功")
from core import config, database, detector, exporter, media, transcriber  # noqa: F401
from server import api, tasks  # noqa: F401
print("[2] 全部模块导入成功")

# 检测器逻辑自检
from core.detector import normalize, _char_pinyin_tokens

n, m = normalize("第 一 名，蕞好用！")
print("[3] 归一化结果:", repr(n), "映射:", m)
assert n == "第一名蕞好用", f"归一化异常: {n}"
print("[4] 拼音token:", _char_pinyin_tokens(n))

# 繁体 + 全角
n2, _ = normalize("最強 ＮＯ．１")
print("[5] 繁体/全角归一化:", repr(n2))
assert "最强no1" in n2, f"归一化异常: {n2}"

print("\n全部自检通过 ✓")
