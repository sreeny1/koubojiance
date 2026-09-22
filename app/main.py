"""启动入口：python app/main.py

流程：初始化日志与数据库 → 启动任务管理器 → 起 FastAPI 服务 →
自动打开浏览器。端口默认 8765，被占用时自动向后寻找可用端口。

日志：统一写入软件根目录 logs/app.log（详细日志模式，轮转保留多份），
控制台同步输出；未捕获异常（主线程/子线程）也会记录，方便排查问题。
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent
# 允许 `python app/main.py` 与 `python -m app.main` 两种方式运行：
# 统一把 app/ 目录加入模块搜索路径，内部一律绝对导入（core.xxx / server.xxx）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 最早阶段应用上次暂存的在线更新（只覆盖 app/，失败不影响本次启动）。
# 辅助脚本通常已经在重启前完成；这里是双重保险。
try:
    from core import updater as _updater

    _applied = _updater.apply_pending_update()
    if _applied:
        print(f"[updater] 已应用暂存更新 v{_applied.get('version')}")
except Exception as _e:  # noqa: BLE001
    print(f"[updater] 启动时应用暂存更新失败（已忽略）：{_e}")

from core.config import (  # noqa: E402
    APP_NAME, APP_VERSION, BASE_DIR, DATA_DIR, LOGS_DIR, MODELS_DIR,
    ensure_dirs, load_settings,
)

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from core.database import init_db  # noqa: E402
from server.api import router  # noqa: E402
from server.cut_tasks import init_cut_manager  # noqa: E402
from server.tasks import init_manager  # noqa: E402

# 详细日志格式：毫秒时间戳 + 级别 + 线程名 + 模块.函数:行号，可精确定位每一行来自哪里
_FMT = ("%(asctime)s.%(msecs)03d | %(levelname)-7s | %(threadName)-18s | "
        "%(name)s.%(funcName)s:%(lineno)d | %(message)s")
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "info") -> None:
    """统一日志配置：控制台 + 软件根目录 logs/app.log（详细模式）。

    - 文件轮转：单文件上限 20MB，保留最近 10 份，长跑不膨胀；
    - 捕获未处理异常（sys.excepthook + 子线程 threading.excepthook）与 warnings；
    - 日志级别：info（默认）或在设置中开启「详细日志模式」后为 debug。
    """
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if level == "debug" else logging.INFO)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)
    # 重复调用（如测试）先清掉已有 handler，避免重复输出
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    fh = RotatingFileHandler(
        LOGS_DIR / "app.log",
        maxBytes=20 * 1024 * 1024, backupCount=10, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG)
    root.addHandler(sh)

    # 第三方库降噪：httpx 自身日志很吵，保留到 WARNING
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn 日志统一走根 logger（log_config=None），全部进同一个文件
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

    # warnings 模块（DeprecationWarning 等）也进日志文件
    logging.captureWarnings(True)

    def _excepthook(tp, val, tb) -> None:
        logging.getLogger("uncaught").critical(
            "未捕获异常（sys.excepthook）", exc_info=(tp, val, tb))

    def _thread_excepthook(args) -> None:
        tname = args.thread.name if args.thread else "?"
        logging.getLogger("uncaught").critical(
            "线程 %s 未捕获异常", tname,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    log = logging.getLogger("main")
    log.info("日志系统就绪：%s（级别 %s）",
             LOGS_DIR / "app.log", logging.getLevelName(root.level))


def find_free_port(start: int = 8765, tries: int = 50) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"从 {start} 起连续 {tries} 个端口均被占用")


def _log_startup_banner(settings: dict) -> None:
    """启动横幅：版本/环境/路径/显卡/系统门槛/设置，一次写全，方便远程排查。"""
    import platform

    log = logging.getLogger("main")
    log.info("=" * 70)
    log.info("%s v%s 启动日志", APP_NAME, APP_VERSION)
    log.info("Python %s | 系统 %s %s | 机子 %s",
             sys.version.split()[0], platform.system(), platform.release(),
             platform.machine())
    log.info("项目根目录 : %s", BASE_DIR)
    log.info("数据目录   : %s", DATA_DIR)
    log.info("模型目录   : %s", MODELS_DIR)
    log.info("日志目录   : %s（日志文件: %s/app.log）", LOGS_DIR, LOGS_DIR)
    log.info("配置文件   : %s", DATA_DIR / "settings.json")
    try:
        import shutil

        du = shutil.disk_usage(str(DATA_DIR))
        log.info("磁盘空间   : 总 %.1f GB / 可用 %.1f GB",
                 du.total / 1024 ** 3, du.free / 1024 ** 3)
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.syscheck import system_info

        si = system_info(DATA_DIR)
        gpu = si.get("gpu") or {}
        req = si.get("requirements") or {}
        log.info("显卡识别   : vendor=%s name=%s cuda=%s → 推荐设备 %s",
                 gpu.get("vendor"), gpu.get("name"), gpu.get("cuda"),
                 gpu.get("recommended_device"))
        log.info("系统门槛   : ok=%s 内存=%sGB 磁盘=%sGB avx2=%s 问题=%s",
                 req.get("ok"), req.get("ram_gb"), req.get("free_disk_gb"),
                 req.get("avx2"), req.get("problems"))
    except Exception as e:  # noqa: BLE001
        log.warning("启动系统信息采集失败（忽略）: %s", e)
    log.info("当前设置   : %s", json.dumps(settings, ensure_ascii=False))
    log.info("=" * 70)


def create_app() -> FastAPI:
    app = FastAPI(title="口播违禁词检测", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """API 请求级日志：方法/路径/状态码/耗时，失败时带完整异常栈。"""
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            logging.getLogger("api").exception(
                "请求处理异常：%s %s", request.method, request.url.path)
            raise
        dur_ms = (time.perf_counter() - start) * 1000
        if request.url.path.startswith("/api"):
            logging.getLogger("api").info(
                "HTTP %s %s -> %s（%.1fms）",
                request.method, request.url.path, response.status_code, dur_ms)
        return response

    app.include_router(router)
    static_dir = BASE / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index() -> HTMLResponse:
        """首页：注入版本号做静态资源缓存击穿（OTA 覆盖 app/ 后，
        ?v=版本号 变化强制 WebView2/浏览器重新拉取 JS/CSS，
        根治"更新成功但界面还是旧布局"的缓存问题）。
        页面本身禁缓存（no-store），保证版本注入始终生效。"""
        html = (BASE / "web" / "index.html").read_text(encoding="utf-8")
        html = html.replace("{{APP_VERSION}}", APP_VERSION)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

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
    if settings.get("log_level") == "debug":
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("main").info("已开启「详细日志模式」(DEBUG)")

    if settings.get("hf_endpoint"):
        os.environ["HF_ENDPOINT"] = str(settings["hf_endpoint"])

    _log_startup_banner(settings)

    init_db()
    init_manager()      # 启动转写工作线程（会恢复上次未完成任务）
    init_cut_manager()  # 启动去词切割工作线程

    app = create_app()
    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    log = logging.getLogger("main")
    log.info("%s v%s 启动：%s", APP_NAME, APP_VERSION, url)
    log.info("按 Ctrl+C 或调用 /api/shutdown 优雅退出")

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # log_config=None：uvicorn 不接管日志配置，访问日志/错误日志全部汇入根 logger
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=True)


if __name__ == "__main__":
    main()
