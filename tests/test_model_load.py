# -*- coding: utf-8 -*-
"""模型加载冒烟测试：单独进程验证各 device/compute 组合是否可用。

用法：python tests\test_model_load.py [cuda|int8_float16 组合序号]
不带参数则依次尝试：cuda/int8_float16 → cuda/float16 → cuda/int8 → cpu/int8
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.transcriber import WhisperEngine  # noqa: E402

MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "models" / "local" / "large-v3"

COMBOS = [
    ("cuda", "int8_float16"),
    ("cuda", "float16"),
    ("cuda", "int8"),
    ("cpu", "int8"),
]


def main() -> int:
    if len(sys.argv) > 1:
        combos = [COMBOS[int(sys.argv[1])]]
    else:
        combos = COMBOS
    for dev, comp in combos:
        print(f"尝试 {dev}/{comp} ...", flush=True)
        eng = WhisperEngine({
            "model": "large-v3", "device": dev, "compute_type": comp,
        })
        m = eng.get_model()
        print(f"  ✓ {dev}/{comp} 加载成功", flush=True)
        # 试转写一小段
        segs = m.transcribe(
            str(Path(__file__).resolve().parent / "media" / "test_video_1.mp4"),
            language="zh", vad_filter=True,
        )[0]
        texts = [s.text for s in segs][:2]
        print(f"  转写采样: {texts}", flush=True)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
