"""启动入口：python app/main.py

流程：初始化日志与数据库 → 启动任务管理器 → 起 FastAPI 服务 →
自动打开浏览器。端口默认 8765，被占用时自动向后寻找可用端口。
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 允许 `python app/main.py` 与 `python -m app.main` 两种方式运行：
# 统一把 app/ 目录加入模块搜索路径，内部一律绝对导入（core.xxx / server.xxx）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.config import APP_NAME, APP_VERSION, ensure_dirs, load_settings  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from core.database import init_db  # noqa: E402
from server.api import router  # noqa: E402
from server.cut_tasks import init_cut_manager  # noqa: E402
from server.tasks import init_manager  # noqa: E402


def setup_logging() -> None:
    ensure_dirs()
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    # 大小轮转：单文件上限 5MB、保留最近 3 份，避免长时间运行日志无限膨胀
    from logging.handlers import RotatingFileHandler

    fh = RotatingFileHandler(
        BASE.parent / "data" / "app.log",
        maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)


def find_free_port(start: int = 8765, tries: int = 50) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"从 {start} 起连续 {tries} 个端口均被占用")


def create_app() -> FastAPI:
    app = FastAPI(title="口播违禁词检测", docs_url=None, redoc_url=None)
    app.include_router(router)
    static_dir = BASE / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(BASE / "web" / "index.html"))

    return app


def main() -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="口播违禁词检测 本地服务")
    ap.add_argument(
        "--no-browser", action="store_true",
        help="不自动打开系统浏览器（启动器 WebView2 模式使用）",
    )
    args, _ = ap.parse_known_args()

    setup_logging()
    settings = load_settings()

    if settings.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = str(settings["hf_endpoint"])

    init_db()
    init_manager()      # 启动转写工作线程（会恢复上次未完成任务）
    init_cut_manager()  # 启动去词切割工作线程

    app = create_app()
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    logging.getLogger("main").info("%s v%s 启动：%s", APP_NAME, APP_VERSION, url)

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
