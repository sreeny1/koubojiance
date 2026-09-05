# -*- coding: utf-8 -*-
"""onnxruntime 加载顺序冲突排查。"""
import subprocess
import sys

CASES = {
    "单独导入": "import onnxruntime; print('OK', onnxruntime.__version__)",
    "ctranslate2先导入": "import ctranslate2; import onnxruntime; print('OK', onnxruntime.__version__)",
    "onnxruntime先导入": "import onnxruntime; import ctranslate2; print('OK', onnxruntime.__version__)",
    "faster_whisper先导入": "import faster_whisper; import onnxruntime; print('OK', onnxruntime.__version__)",
}

for name, code in CASES.items():
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout + r.stderr).decode("utf-8", "ignore").strip().splitlines()
    tail = out[-1] if out else "(无输出)"
    print(f"[{r.returncode}] {name}: {tail[:100]}")
