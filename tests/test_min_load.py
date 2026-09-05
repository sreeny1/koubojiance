# -*- coding: utf-8 -*-
"""最小化验证：经 core.transcriber（含 onnxruntime 预加载）加载模型并转写。

同时回归测试中文路径（项目目录含中文）下模型加载是否正常。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.transcriber import WhisperEngine  # noqa: E402  触发 onnxruntime 预加载

MODEL_DIR = (Path(__file__).resolve().parent.parent
             / "data" / "models" / "local" / "large-v3")
AUDIO = Path(__file__).resolve().parent / "media" / "test_video_1.mp4"

print("[1] 经引擎加载模型（cuda/int8_float16，项目中文路径）...", flush=True)
t0 = time.time()
eng = WhisperEngine({
    "model": "large-v3", "device": "auto",
    "compute_type": "int8_float16", "language": "zh",
})
segs = eng.transcribe(AUDIO)
dt = time.time() - t0
print(f"[2] 实际生效: {eng.effective}", flush=True)
print(f"[3] 转写完成（{dt:.1f}s，{len(segs)} 段）:", flush=True)
for s in segs:
    print(f"  [{s['start']:6.1f} - {s['end']:6.1f}] {s['text']}", flush=True)
print("[4] 全部成功 ✓", flush=True)
