# -*- coding: utf-8 -*-
"""模型下载命令行工具（复用 app.core.model_downloader 自动下载逻辑）。

用法：python tests/download_model.py [large-v3|large-v3-turbo|medium|small|...]
默认 large-v3。优先 ModelScope 国内直连，断点续传、失败自动重试。
生产环境无需手动运行——程序首次启动发现缺模型会自动调用本逻辑。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.model_downloader import download_model  # noqa: E402


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "large-v3"
    print(f"下载模型 {name} …", flush=True)
    download_model(name, progress=lambda p: print(f"  {p:.0%}"))
    print("完成 ✓ 引擎将自动优先使用本地模型目录")


if __name__ == "__main__":
    main()