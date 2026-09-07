"""全局配置与路径管理。

所有运行时数据（数据库、模型、字幕、导出文件、缓存）统一收在项目内
data/ 目录下，保证不向项目外写入任何文件。运行日志单独收在软件根目录
logs/ 下（日志是排查问题用的，必须容易找、不受 data 清理影响）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

# 项目根目录 = app/ 的上一级（打包后即"软件根目录"）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"            # 日志统一放这里（软件根目录/logs）
MODELS_DIR = DATA_DIR / "models"        # whisper 模型缓存
RUNTIME_DIR = DATA_DIR / "runtime"      # 首启自动下载的运行时（CUDA 库 wheel/解压目录/清单）
MEDIA_DIR = DATA_DIR / "media"          # 网页拖拽上传的视频落地目录
SUBTITLES_DIR = DATA_DIR / "subtitles"  # 导出的 SRT 字幕
EXPORTS_DIR = DATA_DIR / "exports"      # 导出的 Excel 报告
DB_PATH = DATA_DIR / "app.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

# ---- 应用版本（每次发布更新此号；界面/日志/状态接口统一读取）----
APP_NAME = "口播违禁词检测"
APP_VERSION = "1.6.0"

# ---- 支持的转写模型（v1.6.0 起仅保留 large-v3，其它模型弃用）----
ALLOWED_MODELS: tuple[str, ...] = ("large-v3",)
DEFAULT_MODEL = "large-v3"

DEFAULT_SETTINGS: dict[str, Any] = {
    # 转写模型：v1.6.0 起仅支持 large-v3（其它模型已弃用）
    "model": "large-v3",
    # device: auto(自动检测 GPU) / cuda / cpu
    "device": "auto",
    # compute_type: int8_float16(默认，省显存) / float16 / int8
    "compute_type": "int8_float16",
    # 转写语言（zh=中文）。auto 为自动检测
    "language": "zh",
    # 同时转写的任务数。GPU 显存有限，默认 1；纯 CPU 可调大
    "max_workers": 1,
    # HuggingFace 模型下载源。留空=官方源；国内网络不通时可填 https://hf-mirror.com
    "hf_endpoint": "",
    # 日志级别：info(默认) / debug(详细日志模式，记录每文件/每段/命令等调试细节)
    "log_level": "info",
    # 界面主题：system(跟随系统) / light(浅色) / dark(深色)
    "theme": "system",
    # 同时显示/检索的默认违禁词分类
    "ui": {
        "accent": "#4f6ef7",
    },
}

log = logging.getLogger("config")


def ensure_dirs() -> None:
    """创建所有数据/日志目录（幂等）。"""
    for d in (DATA_DIR, MODELS_DIR, MEDIA_DIR, SUBTITLES_DIR, EXPORTS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict[str, Any]:
    """读取设置。文件损坏时自动备份并回退到默认值（鲁棒性）。"""
    ensure_dirs()
    if not SETTINGS_PATH.exists():
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings root is not an object")
        # 以默认值为底合并，保证新增字段有值
        merged = _deep_merge(DEFAULT_SETTINGS, data)
        return _normalize_settings(merged)
    except Exception as e:  # noqa: BLE001
        backup = SETTINGS_PATH.with_suffix(f".corrupt-{int(time.time())}.bak")
        try:
            shutil.copy2(SETTINGS_PATH, backup)
            log.warning("设置文件损坏，已备份到 %s 并回退默认值: %s", backup, e)
        except Exception:  # noqa: BLE001
            log.warning("设置文件损坏且备份失败，回退默认值: %s", e)
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict[str, Any]) -> None:
    """原子写入设置文件（先写临时文件再替换，避免写一半损坏）。"""
    ensure_dirs()
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    settings = _normalize_settings(dict(settings))
    tmp.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, SETTINGS_PATH)
    log.info("设置已保存到 %s（%d 个字段，log_level=%s）",
             SETTINGS_PATH, len(settings), settings.get("log_level"))
    return settings


def _normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """设置归一化：v1.6.0 起仅允许 large-v3 模型，其它模型强制回退。"""
    if settings.get("model") not in ALLOWED_MODELS:
        old = settings.get("model")
        settings["model"] = DEFAULT_MODEL
        if old and old != DEFAULT_MODEL:
            log.warning("模型 %s 已弃用，强制回退为 %s", old, DEFAULT_MODEL)
    return settings


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
