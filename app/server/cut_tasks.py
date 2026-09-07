"""去词切割任务队列（与转写任务解耦，单 worker 串行）。

职责：接收"勾选的违禁词命中 → 去词"请求，后台执行：
备份原文件 → ffmpeg 精确剪掉命中区间 → 覆盖原名 → 清空陈旧字幕/命中并
自动重新入队转写（保证界面结果与新的干净视频一致）。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from pathlib import Path

from core.cutter import (
    CutCanceled,
    backup_original,
    cut_remove_ranges,
    hits_to_remove_ranges,
)
from core.database import Database, get_db
from core.media import probe_duration_ms

log = logging.getLogger("cut_tasks")


class CutManager:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()
        self._queue: queue.Queue[int] = queue.Queue()
        self._progress: dict[int, float] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._shutdown = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="cut-worker", daemon=True)
        self._worker.start()
        self._recover()

    # ------------------------------------------------------------------
    def _recover(self) -> None:
        """重启后把上次遗留的未完成去词任务重新入队。"""
        rows = self.db.query(
            "SELECT id FROM cut_jobs WHERE status IN ('queued','running')"
        )
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE cut_jobs SET status='queued' "
                "WHERE status IN ('queued','running')"
            )
        for r in rows:
            self._queue.put(r["id"])
        if rows:
            log.info("去词任务断点恢复：%d 个", len(rows))

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001
                log.exception("去词任务 %s 处理异常", job_id)

    # ------------------------------------------------------------------
    def submit(self, video_id: int, hit_ids: list[int], pad: float = 0.3) -> int:
        job_id = self.db.execute(
            "INSERT INTO cut_jobs(video_id, hit_ids, pad) VALUES(?,?,?)",
            (video_id, json.dumps(hit_ids), float(pad)),
        )
        self._queue.put(job_id)
        log.info("去词任务 #%s 已提交: 视频 #%s, %d 个命中, pad=%.2fs",
                 job_id, video_id, len(hit_ids), pad)
        return job_id

    def _run(self, job_id: int) -> None:
        job = self.db.query_one("SELECT * FROM cut_jobs WHERE id=?", (job_id,))
        if not job or job["status"] not in ("queued", "running"):
            return
        video = self.db.query_one(
            "SELECT * FROM videos WHERE id=?", (job["video_id"],)
        )
        if not video:
            self._finish(job_id, "error", "视频记录不存在")
            return
        src = Path(video["path"])
        if not src.is_file():
            self._finish(job_id, "error", f"文件不存在：{video['path']}")
            return
        if src.suffix.lower() != ".mp4":
            self._finish(job_id, "error", "仅支持 mp4 格式去词")
            return

        try:
            hit_ids = json.loads(job["hit_ids"] or "[]")
        except (TypeError, ValueError):
            self._finish(job_id, "error", "命中数据损坏")
            return
        if not hit_ids:
            self._finish(job_id, "error", "没有选中要去除的违禁词")
            return

        ph = ",".join("?" * len(hit_ids))
        hits = self.db.query(
            f"SELECT id, start_ms, end_ms FROM hits "
            f"WHERE id IN ({ph}) AND video_id=?",
            (*hit_ids, job["video_id"]),
        )
        if not hits:
            self._finish(job_id, "error", "选中的命中已不存在（可能已被删除）")
            return

        pad = float(job["pad"] or 0.3)
        ranges = hits_to_remove_ranges(hits, pad)
        duration_ms = probe_duration_ms(src)
        if duration_ms:
            total_remove = sum(t1 - t0 for t0, t1 in ranges)
            if total_remove >= duration_ms / 1000 - 0.5:
                self._finish(job_id, "error", "去除范围过大（将清空整段视频），已取消")
                return

        cancel = threading.Event()
        self._cancel[job_id] = cancel
        self._progress[job_id] = 0.0
        self.db.execute(
            "UPDATE cut_jobs SET status='running', error=NULL, progress=0 "
            "WHERE id=?", (job_id,)
        )
        log.info("去词任务 #%s 开始处理: 视频 #%s %s，%d 个去除区间",
                 job_id, job["video_id"], video["filename"], len(ranges))

        tmp = src.with_name(src.stem + ".cut_tmp.mp4")
        try:
            def on_p(frac: float) -> None:
                self._progress[job_id] = frac

            backup = backup_original(src)
            self.db.execute(
                "UPDATE cut_jobs SET backup_path=? WHERE id=?",
                (str(backup), job_id),
            )
            cut_remove_ranges(
                src, ranges, tmp, progress=on_p, cancel_check=cancel.is_set
            )
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise RuntimeError("切割产物为空")
            os.replace(tmp, src)  # 覆盖原名（同目录原子替换）
            self._progress[job_id] = 1.0
            self._finish(job_id, "done", None)
            self._requeue_transcribe(job["video_id"])
            log.info(
                "去词任务 #%s 完成：%s（去除 %d 个区间，备份 %s）",
                job_id, video["filename"], len(ranges), backup,
            )
        except CutCanceled:
            tmp.unlink(missing_ok=True)
            log.info("去词任务 #%s 被用户取消: %s", job_id, video["filename"])
            self._finish(job_id, "canceled", "用户取消")
        except Exception as e:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            log.exception("去词任务 #%s 失败: %s", job_id, video["filename"])
            self._finish(job_id, "error", str(e)[:500])
        finally:
            self._cancel.pop(job_id, None)
            self._progress.pop(job_id, None)

    def _requeue_transcribe(self, video_id: int) -> None:
        """去词后原字幕/命中时间已失效：清空并重新入队转写。"""
        try:
            from server.tasks import get_manager

            with self.db.tx() as conn:
                conn.execute("DELETE FROM hits WHERE video_id=?", (video_id,))
                conn.execute("DELETE FROM segments WHERE video_id=?", (video_id,))
                conn.execute("DELETE FROM tasks WHERE video_id=?", (video_id,))
            video = self.db.query_one(
                "SELECT path FROM videos WHERE id=?", (video_id,)
            )
            if video:
                get_manager().submit_paths([video["path"]])
        except Exception:  # noqa: BLE001
            log.exception("去词后重新转写入队失败（不影响去词结果）")

    def _finish(self, job_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE cut_jobs SET status=?, error=?, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (status, error, job_id),
        )

    # ------------------------------------------------------------------
    def cancel(self, job_id: int) -> bool:
        job = self.db.query_one(
            "SELECT * FROM cut_jobs WHERE id=? AND status IN ('queued','running')",
            (job_id,),
        )
        if not job:
            return False
        flag = self._cancel.get(job_id)
        if flag:
            flag.set()
        if job["status"] == "queued":
            self._finish(job_id, "canceled", "用户取消")
        return True

    def progress_map(self) -> dict[int, float]:
        return dict(self._progress)

    def shutdown(self) -> None:
        self._shutdown.set()
        self._worker.join(timeout=3)


# 模块级单例
_manager: CutManager | None = None


def init_cut_manager() -> CutManager:
    global _manager
    if _manager is None:
        _manager = CutManager()
    return _manager


def get_cut_manager() -> CutManager:
    assert _manager is not None, "CutManager 未初始化"
    return _manager
