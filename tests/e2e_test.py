# -*- coding: utf-8 -*-
"""端到端测试：通过 HTTP API 提交测试视频 → 等待转写完成 → 校验检测结果。

前置条件：服务已启动（启动.bat 或 python app/main.py）、
模型已下载（tests/download_model.py）、测试视频已生成（tests/make_test_media.py）。
"""
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8765"
MEDIA = Path(__file__).resolve().parent / "media"

# 预期命中词（与 make_test_media.py 的口播文案对应）
EXPECTED = {
    "test_video_1.mp4": {"最好", "第一", "绝对", "根治", "加微信", "私聊"},
    "test_video_2.mp4": {"零风险", "稳赚不赔", "躺赚"},
}


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=60)

    # 1. 提交两个测试视频
    paths = [str(MEDIA / "test_video_1.mp4"), str(MEDIA / "test_video_2.mp4")]
    r = c.post("/api/scan", json={"paths": paths})
    r.raise_for_status()
    print("[1] 提交检测:", r.json())

    # 2. 轮询等待任务完成（首跑含模型加载，最长等 10 分钟）
    deadline = time.time() + 600
    while time.time() < deadline:
        tasks = c.get("/api/tasks").raise_for_status().json()
        counts = tasks["counts"]
        active = counts.get("queued", 0) + counts.get("running", 0)
        running = next(
            (t for t in tasks["tasks"] if t["status"] == "running"), None)
        prog = f" 进度{running['progress']:.0%}" if running else ""
        print(f"    排队{counts.get('queued', 0)} 进行中{counts.get('running', 0)}"
              f" 完成{counts.get('done', 0)} 失败{counts.get('error', 0)}{prog}",
              end="\r")
        if active == 0:
            break
        time.sleep(3)
    print()

    errors = [t for t in c.get("/api/tasks").json()["tasks"]
              if t["status"] == "error"]
    if errors:
        print("[✗] 有失败任务：")
        for t in errors:
            print(f"    {t['filename']}: {t['error']}")
        return 1

    # 3. 校验检测结果
    results = c.get("/api/results").raise_for_status().json()
    print(f"[2] 统计: {results['stats']}")
    all_ok = True
    for v in results["videos"]:
        got = {h["word_text"] for h in v["hits"]}
        exp = EXPECTED.get(v["filename"], set())
        missing = exp - got
        ok = not missing
        all_ok &= ok
        print(f"[{'✓' if ok else '✗'}] {v['filename']}: 命中 {sorted(got)}"
              + (f" 缺失 {sorted(missing)}" if missing else ""))
        for h in v["hits"]:
            t0 = h["start_ms"] / 1000
            print(f"      {t0:7.2f}s [{h['category']}] {h['word_text']}"
                  f" ← “{h['sentence'][:40]}”")
        # 顺带验证字幕接口
        subs = c.get(f"/api/videos/{v['id']}/subtitles").raise_for_status().json()
        print(f"      字幕 {len(subs['segments'])} 段")

    # 4. 验证导出
    r = c.post("/api/export", json={})
    r.raise_for_status()
    print(f"[3] Excel 导出: {len(r.content)} 字节, 文件名正常"
          if "spreadsheetml" in r.headers.get("content-type", "") else "[✗] 导出异常")

    print("\n端到端测试 " + ("全部通过 ✓" if all_ok else "存在失败 ✗"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
