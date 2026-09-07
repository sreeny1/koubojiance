"""转写任务队列。

稳定性设计：
- 后台工作线程从队列取任务逐个处理，单视频失败不影响整批；
- 任务状态持久化在 SQLite，服务重启后自动把未完成任务重新入队（断点恢复）；
- 支持取消（逐字幕段检查）与失败重试；
- 进度按"已转写时长/总时长"实时上报，供前端轮询。
"""
from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Any

from core.config import SUBTITLES_DIR, load_settings
from core.database import Database, get_db
from core.detector import Detector
from core.transcriber import TranscriptionCanceled, WhisperEngine, segments_to_srt

log = logging.getLogger("tasks")

# 支持的媒体扩展名（视频 + 音频）
VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".ts", ".m4v",
    ".wmv", ".mpg", ".mpeg", ".3gp",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma",
}


class TaskManager:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()
        self.settings = load_settings()
        self.engine = WhisperEngine(self.settings)
        self.detector = Detector(self.db)
        self._queue: queue.Queue[int] = queue.Queue()
        self._cancel_flags: dict[int, threading.Event] = {}
        self._progress: dict[int, float] = {}
        self._workers: list[threading.Thread] = []
        self._shutdown = threading.Event()
        # 模型自动下载状态（供前端展示）
        self._dl_state: dict = {"active": False, "name": None, "frac": 0.0}
        self._dl_error: str | None = None
        self._recover_orphans()
        self._start_model_predownload()
        self._start_workers()

    # ------------------------------------------------------------------
    def _start_model_predownload(self) -> None:
        """首次启动自动准备运行环境（全自动，无需用户操作）：

        1. NVIDIA 机器先自动下载 CUDA 运行库（data/runtime/nvidia，约1.36GB；
           AMD/Intel/无独显机器完全跳过）；
        2. 再下载/校验当前选择的 whisper 模型（data/models/local/<name>）。
        下载源均为国内高速源；断点续传 + 文件清单（大小/SHA256）校验 + 就绪判定，
        保证大文件/小文件都完整、不遗漏。
        """
        name = self.settings.get("model", "large-v3")
        need_cuda = False
        try:
            from core.cuda_setup import is_cuda_runtime_ready, need_cuda_runtime

            need_cuda = need_cuda_runtime() and not is_cuda_runtime_ready()
        except Exception:  # noqa: BLE001
            log.debug("CUDA 判定失败（按不需要处理）", exc_info=True)
        need_model = not self.engine.is_model_ready(name)
        if not need_cuda and not need_model:
            return

        # 初始状态：先展示 CUDA 阶段（如需要），否则直接模型清单
        try:
            from core.model_downloader import _initial_files
            files = _initial_files(name)
        except Exception:  # noqa: BLE001
            files = []
        stage = "cuda" if need_cuda else "model"
        self._dl_state = {
            "active": True, "stage": stage,
            "name": ("nvidia-cuda-runtime" if need_cuda else name),
            "frac": 0.0, "overall": 0.0,
            "current_file": None, "file_index": -1,
            "file_count": 3 if need_cuda else len(files), "file_progress": 0.0,
            "files": [] if need_cuda else files, "source": None, "cuda_error": None,
        }

        def _cuda_ready() -> bool:
            try:
                from core.cuda_setup import is_cuda_runtime_ready

                return is_cuda_runtime_ready()
            except Exception:  # noqa: BLE001
                return False

        def _run() -> None:
            def on_state(st: dict) -> None:
                # 合并"活跃/错误"字段，其余用下载器下发的每文件详情
                st.setdefault("active", True)
                st.setdefault("overall", st.get("frac", 0.0))
                st.setdefault("frac", st.get("overall", 0.0))
                if "cuda_error" not in st:
                    st["cuda_error"] = self._dl_state.get("cuda_error")
                self._dl_state = st

            def on_p(frac: float) -> None:
                self._dl_state["frac"] = frac
                self._dl_state["overall"] = frac

            try:
                if need_cuda:
                    log.info("检测到 NVIDIA 显卡，开始自动下载 CUDA 运行库"
                             "（国内 PyPI 镜像，断点续传 + SHA256，约1.36GB）")
                    from core.cuda_setup import download_cuda_runtime

                    download_cuda_runtime(progress=on_p, state_cb=on_state)
                    log.info("CUDA 运行库下载/校验完成")
                    self._dl_state["stage"] = "model"
                    self._dl_state["name"] = name
                    try:
                        from core.model_downloader import _initial_files
                        self._dl_state["files"] = _initial_files(name)
                    except Exception:  # noqa: BLE001
                        pass
                    self._dl_state["overall"] = 0.0
                    self._dl_state["frac"] = 0.0
                # 模型阶段（CUDA 下载失败不阻断模型；CUDA 成功后模型下载前重判 device）
                if not self.engine.is_model_ready(name):
                    log.info("本地缺少模型 %s，开始自动下载"
                             "（国内源，断点续传 + 清单/SHA256 校验）", name)
                    self.engine.ensure_model(name, progress=on_p, state_cb=on_state)
                self._dl_state["frac"] = 1.0
                self._dl_state["overall"] = 1.0
                log.info("首启运行环境就绪: 模型=%s, CUDA=%s",
                         name, "就绪" if need_cuda and _cuda_ready()
                         else ("不需要" if not need_cuda else "失败(将用CPU)"))
            except Exception as e:  # noqa: BLE001
                if need_cuda and not _cuda_ready():
                    self._dl_state["cuda_error"] = str(e)[:300]
                    # CUDA 下载失败不阻断模型：继续尝试模型（防止首启完全不可用）
                    if not self.engine.is_model_ready(name):
                        try:
                            log.warning("CUDA 下载失败（%s），继续下载模型（将使用 CPU 模式）", e)
                            self._dl_state["stage"] = "model"
                            self._dl_state["name"] = name
                            self.engine.ensure_model(name, progress=on_p, state_cb=on_state)
                            self._dl_state["frac"] = 1.0
                            self._dl_state["overall"] = 1.0
                        except Exception as e2:  # noqa: BLE001
                            self._dl_error = str(e2)[:300]
                            log.exception("模型自动下载失败")
                else:
                    self._dl_error = str(e)[:300]
                    log.exception("首启运行环境准备失败")
            finally:
                self._dl_state["active"] = False

        threading.Thread(target=_run, name="runtime-predownload", daemon=True).start()

    @property
    def download_state(self) -> dict:
        d = dict(self._dl_state)
        d["error"] = self._dl_error
        return d

    # ------------------------------------------------------------------
    def _recover_orphans(self) -> None:
        """把上次进程退出时遗留的未完成任务重新入队。"""
        rows = self.db.query(
            "SELECT id FROM tasks WHERE status IN ('queued','running')"
        )
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE tasks SET status='queued' WHERE status IN ('queued','running')"
            )
        for r in rows:
            self._queue.put(r["id"])
        if rows:
            log.info("断点恢复：重新入队 %d 个任务", len(rows))

    def _start_workers(self) -> None:
        n = max(1, int(self.settings.get("max_workers", 1)))
        for i in range(n):
            t = threading.Thread(target=self._worker_loop, name=f"whisper-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                task_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_task(task_id)
            except Exception:  # noqa: BLE001  兜底：任何异常都不能杀死工作线程
                log.exception("任务 %s 处理异常", task_id)

    # ------------------------------------------------------------------
    def _run_task(self, task_id: int) -> None:
        task = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or task["status"] not in ("queued", "running"):
            return
        video = self.db.query_one(
            "SELECT * FROM videos WHERE id=?", (task["video_id"],)
        )
        if not video:
            self._finish(task_id, "error", "视频记录不存在")
            return
        if not Path(video["path"]).exists():
            self._finish(task_id, "error", f"文件不存在：{video['path']}")
            return

        cancel = threading.Event()
        self._cancel_flags[task_id] = cancel
        self._progress[task_id] = 0.0
        self.db.execute(
            "UPDATE tasks SET status='running', error=NULL WHERE id=?", (task_id,)
        )
        log.info("任务 #%s 开始处理: %s（视频 #%s, %s）",
                 task_id, video["filename"], video["id"], video["path"])

        try:
            def on_progress(frac: float) -> None:
                self._progress[task_id] = frac

            # 模型未就绪时自动等待后台下载完成（下载阶段映射到进度条 0~90%）
            self.engine.ensure_model(
                progress=lambda f: self._progress.__setitem__(task_id, f * 0.9),
                cancel_check=cancel.is_set,
            )

            segments = self.engine.transcribe(
                video["path"],
                progress=on_progress,
                cancel_check=cancel.is_set,
            )

            duration_ms = (
                int(segments[-1]["end"] * 1000) if segments else None
            )
            with self.db.tx() as conn:
                conn.execute("DELETE FROM segments WHERE video_id=?", (video["id"],))
                conn.execute("DELETE FROM hits WHERE video_id=?", (video["id"],))
                conn.executemany(
                    "INSERT INTO segments(video_id,idx,start_ms,end_ms,text) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (video["id"], i, int(s["start"] * 1000),
                         int(s["end"] * 1000), s["text"])
                        for i, s in enumerate(segments)
                    ],
                )
                conn.execute(
                    "UPDATE videos SET duration_ms=?, transcribed_at="
                    "datetime('now','localtime') WHERE id=?",
                    (duration_ms, video["id"]),
                )
            # 导出 SRT（数据以数据库为准，SRT 是给人看/复用的副产品）
            if segments:
                srt_path = SUBTITLES_DIR / f"{video['id']}_{video['filename']}.srt"
                srt_path.write_text(segments_to_srt(segments), encoding="utf-8")

            hits = self.detector.scan_video(video["id"])
            self._progress[task_id] = 1.0
            self._finish(task_id, "done", None)
            log.info(
                "任务 #%s 完成: %s（%d 段字幕，%d 处命中，SRT=%s）",
                task_id, video["filename"], len(segments), hits,
                SUBTITLES_DIR / f"{video['id']}_{video['filename']}.srt",
            )
        except TranscriptionCanceled:
            log.info("任务 #%s 被用户取消: %s", task_id, video["filename"])
            self._finish(task_id, "canceled", "用户取消")
        except Exception as e:  # noqa: BLE001
            log.exception("任务 #%s 失败: %s", task_id, video["filename"])
            self._finish(task_id, "error", str(e)[:500])
        finally:
            self._cancel_flags.pop(task_id, None)
            self._progress.pop(task_id, None)

    def _finish(self, task_id: int, status: str, error: str | None) -> None:
        self.db.execute(
            "UPDATE tasks SET status=?, error=?, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (status, error, task_id),
        )

    # ------------------------------------------------------------------
    # 供 API 调用的管理接口
    # ------------------------------------------------------------------
    def submit_paths(self, paths: list[str]) -> dict[str, int]:
        """展开目录、过滤媒体文件、建视频记录并入队。"""
        files: list[Path] = []
        for p in paths:
            path = Path(p)
            if path.is_dir():
                files.extend(
                    f for f in sorted(path.rglob("*"))
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS
                )
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTS:
                files.append(path)
        # 去重（同一文件可能被拖入多次）
        uniq = {f.resolve() for f in files}

        added = 0
        new_task_ids: list[int] = []
        for f in sorted(uniq):
            video = self.db.query_one(
                "SELECT id FROM videos WHERE path=?", (str(f),)
            )
            if video:
                vid = video["id"]
                # 已有排队/进行中的任务则不重复建（避免同一视频重复转写）
                pending = self.db.query_one(
                    "SELECT id FROM tasks WHERE video_id=? "
                    "AND status IN ('queued','running') LIMIT 1", (vid,)
                )
                if pending:
                    log.debug("视频已在任务队列中，跳过: %s", f)
                    continue
            else:
                vid = self.db.execute(
                    "INSERT INTO videos(path,filename) VALUES(?,?)",
                    (str(f), f.name),
                )
                log.debug("新建视频记录 #%s: %s", vid, f)
            tid = self.db.execute(
                "INSERT INTO tasks(video_id) VALUES(?)", (vid,)
            )
            new_task_ids.append(tid)
            added += 1
        for tid in new_task_ids:
            self._queue.put(tid)
        log.info("提交路径完成: 输入 %s 个路径 → 展开 %d 个媒体文件（去重后）→ 新任务 %d 个",
                 len(paths), len(uniq), added)
        return {"files": len(uniq), "tasks": added}

    def retry(self, task_id: int) -> bool:
        task = self.db.query_one(
            "SELECT * FROM tasks WHERE id=? AND status IN ('error','canceled')",
            (task_id,),
        )
        if not task:
            return False
        self.db.execute(
            "UPDATE tasks SET status='queued', error=NULL, finished_at=NULL "
            "WHERE id=?", (task_id,)
        )
        self._queue.put(task_id)
        log.info("任务 #%s 已重试入队", task_id)
        return True

    def cancel(self, task_id: int) -> bool:
        task = self.db.query_one(
            "SELECT * FROM tasks WHERE id=? AND status IN ('queued','running')",
            (task_id,),
        )
        if not task:
            return False
        flag = self._cancel_flags.get(task_id)
        if flag:
            flag.set()
        if task["status"] == "queued":
            self._finish(task_id, "canceled", "用户取消")
        log.info("任务 #%s 已请求取消", task_id)
        return True

    def progress_map(self) -> dict[int, float]:
        return dict(self._progress)

    def reload_wordbank(self) -> None:
        """词库变更后：重载内存索引并对全部已转写视频重新检测。

        同时顺带修复历史坏数据：whisper 偶发跑偏产生的超长字幕段
        （整条视频一整段、无内部时间戳）在此按标点二次切分。
        """
        self.detector.reload()
        self._repair_oversized_segments()
        return self.detector.scan_all()

    def _repair_oversized_segments(self) -> None:
        """把库中超长段切分回写（分钟级耗时，纯本地计算，不重新转写）。"""
        from core.transcriber import split_oversized_segments

        rows = self.db.query(
            "SELECT video_id, idx, start_ms, end_ms, text FROM segments "
            "ORDER BY video_id, idx"
        )
        # 按视频分组找出含超长段的视频，只重写这些视频
        by_video: dict[int, list[dict]] = {}
        for r in rows:
            by_video.setdefault(r["video_id"], []).append(r)

        for vid, segs in by_video.items():
            as_dicts = [
                {"start": s["start_ms"] / 1000, "end": s["end_ms"] / 1000,
                 "text": s["text"]}
                for s in segs
            ]
            fixed = split_oversized_segments(as_dicts)
            if len(fixed) == len(as_dicts):
                continue  # 无超长段，跳过
            with self.db.tx() as conn:
                conn.execute("DELETE FROM segments WHERE video_id=?", (vid,))
                conn.executemany(
                    "INSERT INTO segments(video_id,idx,start_ms,end_ms,text) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (vid, i, int(s["start"] * 1000),
                         int(s["end"] * 1000), s["text"])
                        for i, s in enumerate(fixed)
                    ],
                )
            log.info("已修复视频 %s 的超长字幕段：%d → %d 段",
                     vid, len(as_dicts), len(fixed))

    def reload_settings(self, settings: dict) -> None:
        self.settings = settings
        self.engine.reload(settings)

    def shutdown(self) -> None:
        self._shutdown.set()
        for t in self._workers:
            t.join(timeout=3)

    def clear_all(self) -> dict:
        """一键清除全部检测记录（任务/视频/字幕/命中），保留词库与设置。"""
        import os
        import queue
        import time

        # 1. 取消所有 queued / running 的任务：发 cancel 信号 + 把 queued 的直接置 canceled
        rows = self.db.query(
            "SELECT id, status FROM tasks WHERE status IN ('queued','running')"
        )
        for r in rows:
            flag = self._cancel_flags.get(r["id"])
            if flag:
                flag.set()
            if r["status"] == "queued":
                self._finish(r["id"], "canceled", "已清除")

        # 2. 清空内存队列
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        # 3. 给 running 中的任务最多 10 秒优雅退出（cancel 信号生效后 transcriber 会抛 TranscriptionCanceled）
        deadline = time.time() + 10
        while time.time() < deadline:
            remaining = self.db.query_one(
                "SELECT COUNT(*) c FROM tasks WHERE status='running'"
            )["c"]
            if remaining == 0:
                break
            time.sleep(0.2)

        # 4. 统计 + 清库（按依赖序删，ON DELETE CASCADE 会自动级联）
        stat = self.db.query_one(
            "SELECT COUNT(*) videos, "
            "(SELECT COUNT(*) FROM tasks) tasks, "
            "(SELECT COUNT(*) FROM segments) segments, "
            "(SELECT COUNT(*) FROM hits) hits FROM videos"
        )

        with self.db.tx() as conn:
            conn.execute("DELETE FROM hits")
            conn.execute("DELETE FROM segments")
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM videos")

        # 5. 清内存状态
        self._progress.clear()
        self._cancel_flags.clear()

        # 6. 清磁盘：所有 SRT 字幕文件 + data/media 下的上传副本
        deleted_srt = 0
        if SUBTITLES_DIR.exists():
            for f in SUBTITLES_DIR.iterdir():
                if f.suffix.lower() == ".srt":
                    try:
                        f.unlink()
                        deleted_srt += 1
                    except OSError:
                        pass

        from core.config import MEDIA_DIR
        deleted_media = 0
        if MEDIA_DIR.exists():
            for f in MEDIA_DIR.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        deleted_media += 1
                    except OSError:
                        pass

        log.info(
            "一键清除：%d 视频 / %d 任务 / %d 字幕段 / %d 命中 → 磁盘清 %d SRT / %d media",
            stat["videos"], stat["tasks"], stat["segments"], stat["hits"],
            deleted_srt, deleted_media,
        )
        return {
            "ok": True,
            "videos": stat["videos"],
            "tasks": stat["tasks"],
            "segments": stat["segments"],
            "hits": stat["hits"],
            "srt_files": deleted_srt,
            "media_files": deleted_media,
        }


# 模块级单例
_manager: TaskManager | None = None


def init_manager() -> TaskManager:
    global _manager
    if _manager is None:
        _manager = TaskManager()
    return _manager


def get_manager() -> TaskManager:
    assert _manager is not None, "TaskManager 未初始化"
    return _manager
