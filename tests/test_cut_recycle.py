# -*- coding: utf-8 -*-
"""Integration test for cut flow: original goes to recycle bin after replacement.

Uses a mock recycle function (moves the backup file to a temp _recycle dir)
so the test does not pollute the real Windows Recycle Bin.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import imageio_ffmpeg  # noqa: E402
from core.database import Database  # noqa: E402
from core.media import probe_duration_ms  # noqa: E402
from server.cut_tasks import CutManager  # noqa: E402


def make_video(path: Path) -> None:
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ff, "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=10:r=25",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=10",
        "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cut_recycle_test_"))
    src = tmp / "source.mp4"
    make_video(src)
    original_ms = probe_duration_ms(src)
    assert original_ms and 9500 <= original_ms <= 10500, original_ms

    db = Database(tmp / "app.db")
    video_id = db.execute(
        "INSERT INTO videos(path,filename,duration_ms) VALUES(?,?,?)",
        (str(src), src.name, original_ms),
    )
    seg_id = db.execute(
        "INSERT INTO segments(video_id,idx,start_ms,end_ms,text) VALUES(?,?,?,?,?)",
        (video_id, 0, 2000, 4000, "banned words here"),
    )
    hit_id = db.execute(
        "INSERT INTO hits(video_id,segment_id,word_id,word_text,matched_text,start_ms,end_ms,sentence) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (video_id, seg_id, 1, "banned", "banned", 2000, 4000, "banned words here"),
    )

    recycle_dir = tmp / "_recycle"
    recycle_dir.mkdir()

    def fake_recycle(path):
        path = Path(path)
        target = recycle_dir / path.name
        shutil.move(str(path), str(target))
        return True

    mgr = CutManager(db)
    try:
        with patch("server.cut_tasks.send_to_recycle_bin", side_effect=fake_recycle), \
             patch("server.cut_tasks.CutManager._requeue_transcribe", lambda self, vid: None):
            job_id = mgr.submit(video_id, [hit_id], pad=0.0)
            deadline = time.time() + 60
            job = None
            while time.time() < deadline:
                job = db.query_one("SELECT * FROM cut_jobs WHERE id=?", (job_id,))
                if job and job["status"] in ("done", "error", "canceled"):
                    break
                time.sleep(0.2)
    finally:
        mgr.shutdown()
        db.close()

    assert job and job["status"] == "done", job
    out_ms = probe_duration_ms(src) or 0
    assert abs(out_ms - (original_ms - 2000)) <= 1500, (original_ms, out_ms)
    assert str(job["backup_path"]).startswith("回收站:"), job["backup_path"]
    assert list(recycle_dir.iterdir()), "fake recycle dir is empty"
    assert not list(tmp.glob("*去词前备份*")), "backup copy still in source dir"
    assert not list(tmp.glob("*.cut_tmp.mp4")), "temp cut file left behind"
    shutil.rmtree(tmp, ignore_errors=True)
    print("cut recycle integration test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
