"""FastAPI 路由层。

只做参数校验与数据组装，业务逻辑全部在 core/ 与 TaskManager 中。
所有视频文件访问都通过数据库中登记的 video_id 间接进行，不暴露
任意路径读取，保证本地服务的安全性。
"""
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from core import config
from core.database import get_db
from core.exporter import export_hits
from server.cut_tasks import get_cut_manager
from server.tasks import VIDEO_EXTS, get_manager

log = logging.getLogger("api")

router = APIRouter(prefix="/api")

_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
}


# ----------------------------------------------------------------------
# 任务提交与管理
# ----------------------------------------------------------------------
def _ensure_model_ready() -> None:
    """模型未就绪时拒绝提交转写，避免用户在软件完整可用前拖入视频。"""
    m = get_manager()
    if not m.engine.is_model_ready():
        raise HTTPException(
            409,
            "识别模型尚未下载完成，暂时还不能开始检测。"
            "请等待模型下载完成（或按界面提示手动放置模型）后再拖入视频。"
        )


@router.post("/scan")
def scan(payload: dict = Body(...)):
    """提交本地路径（文件或目录）进行转写检测。模型未就绪时拦截。"""
    _ensure_model_ready()
    paths = payload.get("paths") or []
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "paths 不能为空")
    paths = [str(p) for p in paths if str(p).strip()]
    if not paths:
        raise HTTPException(400, "paths 不能为空")
    for p in paths:
        if not Path(p).exists():
            raise HTTPException(400, f"路径不存在：{p}")
    return get_manager().submit_paths(paths)


@router.post("/upload")
async def upload(file: UploadFile = File(...)):
    """网页拖拽上传的文件落地到 data/media 并直接入队。模型未就绪时拦截。"""
    _ensure_model_ready()
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in VIDEO_EXTS:
        raise HTTPException(400, f"不支持的文件类型：{suffix}")
    config.ensure_dirs()
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    dst = config.MEDIA_DIR / safe_name
    try:
        with dst.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        raise HTTPException(500, f"保存上传文件失败：{e}") from e
    return get_manager().submit_paths([str(dst)])


@router.get("/tasks")
def list_tasks(status: str | None = None, limit: int = 500):
    """任务列表（含实时进度）。"""
    db = get_db()
    sql = """
        SELECT t.id, t.status, t.error, t.created_at, t.finished_at,
               v.id AS video_id, v.filename, v.path, v.duration_ms
        FROM tasks t JOIN videos v ON v.id = t.video_id
    """
    params: list = []
    if status:
        sql += " WHERE t.status=?"
        params.append(status)
    sql += " ORDER BY t.id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, params)
    prog = get_manager().progress_map()
    for r in rows:
        r["progress"] = prog.get(r["id"], 1.0 if r["status"] == "done" else 0.0)
    counts = {c["status"]: c["c"] for c in db.query(
        "SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
    return {"tasks": rows, "counts": counts}


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int):
    if not get_manager().retry(task_id):
        raise HTTPException(400, "任务不存在或当前状态不可重试")
    return {"ok": True}


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int):
    if not get_manager().cancel(task_id):
        raise HTTPException(400, "任务不存在或已结束")
    return {"ok": True}


# ----------------------------------------------------------------------
# 检测结果
# ----------------------------------------------------------------------
@router.get("/results")
def results():
    """全部视频的检测结果总览（含每视频命中明细）。"""
    db = get_db()
    videos = db.query(
        """
        SELECT v.id, v.filename, v.path, v.duration_ms, v.created_at,
               v.transcribed_at,
               (SELECT t.status FROM tasks t WHERE t.video_id = v.id
                ORDER BY t.id DESC LIMIT 1) AS task_status,
               (SELECT COUNT(*) FROM hits h WHERE h.video_id = v.id) AS hit_count,
               (SELECT COUNT(*) FROM segments s WHERE s.video_id = v.id) AS seg_count
        FROM videos v
        ORDER BY v.id DESC
        """
    )
    hits = db.query(
        """
        SELECT h.video_id, h.id, h.word_text, h.matched_text, h.start_ms,
               h.end_ms, h.sentence, c.name AS category, c.color AS color
        FROM hits h
        JOIN words w ON w.id = h.word_id
        JOIN categories c ON c.id = w.category_id
        ORDER BY h.video_id, h.start_ms
        """
    )
    by_video: dict[int, list] = {}
    for h in hits:
        by_video.setdefault(h["video_id"], []).append(h)
    for v in videos:
        v["hits"] = by_video.get(v["id"], [])
    stats = {
        "videos": len(videos),
        "transcribed": sum(1 for v in videos if v["seg_count"] > 0),
        "hits": len(hits),
    }
    return {"videos": videos, "stats": stats}


@router.get("/videos/{video_id}/subtitles")
def video_subtitles(video_id: int):
    """单视频完整字幕（含每段的命中标注，用于详情与播放器联动）。"""
    db = get_db()
    video = db.query_one("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        raise HTTPException(404, "视频不存在")
    segments = db.query(
        "SELECT id, idx, start_ms, end_ms, text FROM segments "
        "WHERE video_id=? ORDER BY idx", (video_id,)
    )
    hits = db.query(
        """
        SELECT h.segment_id, h.word_text, h.matched_text, h.start_ms, h.end_ms,
               h.sentence, c.name AS category, c.color AS color
        FROM hits h
        JOIN words w ON w.id = h.word_id
        JOIN categories c ON c.id = w.category_id
        WHERE h.video_id=? ORDER BY h.start_ms
        """, (video_id,)
    )
    by_seg: dict[int, list] = {}
    for h in hits:
        by_seg.setdefault(h["segment_id"], []).append(h)
    for s in segments:
        s["hits"] = by_seg.get(s["id"], [])
    return {"video": dict(video), "segments": segments}


@router.get("/videos/{video_id}/stream")
def video_stream(video_id: int):
    """流式播放视频（Starlette FileResponse 支持 Range 断点请求）。"""
    db = get_db()
    video = db.query_one("SELECT path FROM videos WHERE id=?", (video_id,))
    if not video:
        raise HTTPException(404, "视频不存在")
    p = Path(video["path"])
    if not p.exists():
        raise HTTPException(410, "文件已被移动或删除")
    return FileResponse(
        str(p), media_type=_MEDIA_TYPES.get(p.suffix.lower(), "application/octet-stream")
    )


@router.get("/videos/{video_id}/srt")
def video_srt(video_id: int):
    """下载该视频的 SRT 字幕。"""
    db = get_db()
    video = db.query_one("SELECT * FROM videos WHERE id=?", (video_id,))
    if not video:
        raise HTTPException(404, "视频不存在")
    srt = config.SUBTITLES_DIR / f"{video_id}_{video['filename']}.srt"
    if not srt.exists():
        raise HTTPException(404, "暂无字幕文件")
    return FileResponse(
        str(srt), media_type="text/plain",
        filename=f"{Path(video['filename']).stem}.srt",
    )


@router.delete("/videos/{video_id}")
def delete_video(video_id: int, remove_file: bool = False):
    """删除某条检测记录；remove_file=True 时把磁盘上的原始文件移入回收站。"""
    db = get_db()
    video = db.query_one("SELECT id, path FROM videos WHERE id=?", (video_id,))
    if not video:
        raise HTTPException(404, "视频不存在")

    removed_file = False
    if remove_file:
        from core.media import send_to_recycle_bin
        p = Path(video["path"])
        if not p.exists():
            removed_file = True  # 文件早已不在磁盘，视为已删除
        elif send_to_recycle_bin(p):
            removed_file = True
        else:
            raise HTTPException(
                409, f"文件正被占用或删除失败，已取消操作：{p.name}"
            )

    with db.tx() as conn:
        conn.execute("DELETE FROM hits WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM tasks WHERE video_id=?", (video_id,))
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
    return {"ok": True, "removed_file": removed_file}


# ----------------------------------------------------------------------
# 去词切割（自定义去除选中的违禁词）
# ----------------------------------------------------------------------
@router.post("/videos/{video_id}/cut")
def submit_cut(video_id: int, payload: dict = Body(...)):
    """提交去词任务：hit_ids 为选中的命中 id 列表，pad 为命中前后缓冲秒。"""
    db = get_db()
    video = db.query_one("SELECT id, path FROM videos WHERE id=?", (video_id,))
    if not video:
        raise HTTPException(404, "视频不存在")
    if Path(video["path"]).suffix.lower() != ".mp4":
        raise HTTPException(400, "仅支持 mp4 格式去词")
    hit_ids = payload.get("hit_ids") or []
    if not isinstance(hit_ids, list) or not hit_ids:
        raise HTTPException(400, "请至少选择一条命中")
    try:
        hit_ids = [int(x) for x in hit_ids]
    except (TypeError, ValueError):
        raise HTTPException(400, "hit_ids 必须是整数数组") from None
    try:
        pad = max(0.0, min(2.0, float(payload.get("pad", 0.3))))
    except (TypeError, ValueError):
        pad = 0.3
    job_id = get_cut_manager().submit(video_id, hit_ids, pad)
    return {"job_id": job_id}


@router.get("/videos/{video_id}/cut-jobs")
def list_cut_jobs(video_id: int):
    """该视频的去词任务列表（含实时进度）。"""
    rows = get_db().query(
        "SELECT * FROM cut_jobs WHERE video_id=? ORDER BY id DESC LIMIT 20",
        (video_id,),
    )
    prog = get_cut_manager().progress_map()
    for r in rows:
        r["progress"] = prog.get(r["id"], r["progress"] or 0.0)
    return {"jobs": rows}


@router.post("/cut-jobs/{job_id}/cancel")
def cancel_cut_job(job_id: int):
    if not get_cut_manager().cancel(job_id):
        raise HTTPException(400, "任务不存在或已结束")
    return {"ok": True}


@router.delete("/data")
def clear_all_data():
    """一键清除全部检测数据（任务/视频/字幕/命中），词库与设置不动。"""
    return get_manager().clear_all()


# ----------------------------------------------------------------------
# 词库管理
# ----------------------------------------------------------------------
@router.get("/words")
def list_words():
    db = get_db()
    categories = db.query("SELECT * FROM categories ORDER BY id")
    words = db.query(
        """
        SELECT w.*, c.name AS category, c.color AS color
        FROM words w JOIN categories c ON c.id = w.category_id
        ORDER BY w.category_id, w.id
        """
    )
    return {"categories": categories, "words": words}


@router.get("/categories")
def list_categories():
    return {"categories": get_db().query("SELECT * FROM categories ORDER BY id")}


@router.post("/categories")
def create_category(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    color = (payload.get("color") or "#e5484d").strip()
    if not name:
        raise HTTPException(400, "分类名不能为空")
    try:
        cid = get_db().execute(
            "INSERT INTO categories(name,color) VALUES(?,?)", (name, color)
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"创建失败（可能重名）：{e}") from e
    return {"id": cid}


@router.post("/words")
def create_word(payload: dict = Body(...)):
    word = (payload.get("word") or "").strip()
    category_id = payload.get("category_id")
    note = (payload.get("note") or "").strip()
    if not word:
        raise HTTPException(400, "词不能为空")
    if not category_id:
        raise HTTPException(400, "缺少分类")
    try:
        wid = get_db().execute(
            "INSERT INTO words(category_id,word,note) VALUES(?,?,?)",
            (category_id, word, note),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"添加失败（该分类下可能已存在）：{e}") from e
    return {"id": wid}


@router.post("/words/bulk")
def bulk_create_words(payload: dict = Body(...)):
    """批量导入：按行拆分文本。"""
    text = payload.get("text") or ""
    category_id = payload.get("category_id")
    if not category_id:
        raise HTTPException(400, "缺少分类")
    words = [w.strip() for w in text.replace("\r", "\n").split("\n") if w.strip()]
    if not words:
        raise HTTPException(400, "没有可导入的词")
    db = get_db()
    added = 0
    with db.tx() as conn:
        for w in words:
            cur = conn.execute(
                "INSERT OR IGNORE INTO words(category_id,word) VALUES(?,?)",
                (category_id, w),
            )
            added += cur.rowcount
    return {"added": added, "skipped": len(words) - added}


@router.put("/words/{word_id}")
def update_word(word_id: int, payload: dict = Body(...)):
    db = get_db()
    row = db.query_one("SELECT id FROM words WHERE id=?", (word_id,))
    if not row:
        raise HTTPException(404, "词不存在")
    sets, params = [], []
    for key in ("word", "note", "enabled"):
        if key in payload:
            sets.append(f"{key}=?")
            params.append(payload[key])
    if "category_id" in payload and payload["category_id"]:
        sets.append("category_id=?")
        params.append(payload["category_id"])
    if not sets:
        raise HTTPException(400, "没有需要更新的字段")
    params.append(word_id)
    with db.tx() as conn:
        conn.execute(f"UPDATE words SET {', '.join(sets)} WHERE id=?", params)
    return {"ok": True}


@router.delete("/words/{word_id}")
def delete_word(word_id: int):
    get_db().execute("DELETE FROM words WHERE id=?", (word_id,))
    return {"ok": True}


@router.post("/redetect")
def redetect():
    """词库变更后，对全部已转写视频重新检测（无需重新转写）。"""
    result = get_manager().reload_wordbank()
    return result


# ----------------------------------------------------------------------
# 报告导出
# ----------------------------------------------------------------------
@router.post("/export")
def export(payload: dict = Body(default={})):
    video_ids = payload.get("video_ids") or None
    try:
        path = export_hits(video_ids)
    except Exception as e:  # noqa: BLE001
        log.exception("导出失败")
        raise HTTPException(500, f"导出失败：{e}") from e
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


# ----------------------------------------------------------------------
# 设置与状态
# ----------------------------------------------------------------------
@router.get("/settings")
def get_settings():
    return config.load_settings()


@router.post("/settings")
def save_settings(payload: dict = Body(...)):
    merged = config.load_settings()
    # 白名单字段合并，防止前端塞入垃圾键
    for key in config.DEFAULT_SETTINGS:
        if key in payload:
            merged[key] = payload[key]
    try:
        merged["max_workers"] = max(1, min(8, int(merged["max_workers"])))
    except (TypeError, ValueError):
        merged["max_workers"] = 1
    config.save_settings(merged)
    get_manager().reload_settings(merged)
    return merged


@router.get("/status")
def status():
    m = get_manager()
    db = get_db()
    counts = {c["status"]: c["c"] for c in db.query(
        "SELECT status, COUNT(*) c FROM tasks GROUP BY status")}
    try:
        from core.syscheck import system_info
        sys_info = system_info(config.DATA_DIR)
    except Exception:  # noqa: BLE001  系统信息获取失败不影响状态接口
        sys_info = {}
    guide = None
    model_ready = True
    try:
        from core.model_downloader import is_model_ready, manual_download_guide
        model_name = m.settings.get("model", "large-v3")
        model_ready = is_model_ready(model_name)
        if not model_ready:
            guide = manual_download_guide(model_name)
    except Exception:  # noqa: BLE001
        guide = None
    return {
        "effective": m.engine.effective,   # 实际生效的模型/设备（未加载时为 null）
        "settings": m.settings,
        "task_counts": counts,
        "model_download": m.download_state,
        "model_ready": model_ready,        # 模型是否已就绪（首启拦截拖入的依据）
        "system": sys_info,
        "model_guide": guide,              # 模型缺失时的手动下载引导（含链接与目标目录）
        "version": config.APP_VERSION,
    }


@router.post("/shutdown")
def shutdown():
    """本地服务优雅退出（供启动器托盘退出调用）。

    只接受 127.0.0.1 本机访问（uvicorn 已绑定 loopback）。
    流程：停止任务队列/工作线程 → 清理 → 结束进程；
    由启动器配合进程树强杀做双保险，确保零残留。
    """
    import os
    import time

    def _graceful_stop() -> None:
        time.sleep(0.4)  # 让响应先返回给调用方
        try:
            get_manager().shutdown()  # 停转写 worker 线程（最多等 3s）
        except Exception:  # noqa: BLE001
            logging.getLogger("tasks").exception("优雅关停异常（忽略）")
        try:
            get_cut_manager().shutdown()  # 停去词 worker 线程
        except Exception:  # noqa: BLE001
            logging.getLogger("cut_tasks").exception("去词任务关停异常（忽略）")
        try:
            get_db().close()
        except Exception:  # noqa: BLE001
            pass
        os._exit(0)  # normal 结束可释放句柄；ffmpeg 子进程由启动器树杀兜底

    threading.Thread(target=_graceful_stop, daemon=True).start()
    return {"ok": True}


# ----------------------------------------------------------------------
# 系统文件/目录选择对话框（本地应用专属能力）
# ----------------------------------------------------------------------
_pick_lock = threading.Lock()

# 独立进程运行的 tkinter 选择器：即使 Windows 输入法组件（wetype_tip.dll）
# 在对话框弹出时崩溃（实测 0xC0000005 会把服务进程一起带走），也只死这个
# 子进程，主服务不受影响，用户重试即可。
_PICK_HELPER = r"""\
import json, sys

def main() -> int:
    # 关键：子进程与父进程统一用 UTF-8 交换数据。
    # Windows 下管道 stdout 默认按系统代码页(GBK)编码，中文路径会被父进程
    # 按 UTF-8 解码成乱码（实测"今天素材"->"ز"类怪字符），必须显式指定。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    mode = sys.argv[1]
    exts = sys.argv[2:]
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"tkinter 不可用: {e}"}, ensure_ascii=False))
        return 1
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if mode == "files":
            chosen = filedialog.askopenfilenames(
                title="选择视频/音频文件（可多选）",
                filetypes=[("媒体文件", " ".join(exts)), ("所有文件", "*.*")],
            )
            paths = list(chosen)
        else:
            chosen = filedialog.askdirectory(title="选择文件夹（将扫描其中全部媒体文件）")
            paths = [chosen] if chosen else []
    finally:
        root.destroy()
    print(json.dumps({"paths": paths}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
"""


@router.post("/pick")
def pick_files(payload: dict = Body(...)):
    """弹出系统文件/目录选择框（独立子进程运行 tkinter）。

    浏览器出于安全不提供本地绝对路径，此接口用于"浏览"按钮，
    选择结果直接走 /api/scan，避免大文件经 HTTP 上传复制一遍。
    """
    import json
    import subprocess
    import sys

    mode = payload.get("mode", "files")
    if mode not in ("files", "folder"):
        raise HTTPException(400, "mode 必须是 files 或 folder")

    with _pick_lock:
        try:
            r = subprocess.run(
                [sys.executable, "-c", _PICK_HELPER, mode, *sorted(VIDEO_EXTS)],
                capture_output=True, timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "选择对话框超时，请重试") from None

    out = (r.stdout or b"").decode("utf-8", "ignore").strip()
    data = None
    try:
        data = json.loads(out) if out else None
    except ValueError:
        data = None

    if data is None or r.returncode != 0:
        detail = (data and data.get("error")) or "选择框异常退出（可能被系统组件干扰），请重试"
        raise HTTPException(500, f"无法完成文件选择：{detail}")
    return {"paths": data["paths"]}
