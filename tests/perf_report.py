# -*- coding: utf-8 -*-
"""性能检测报告：检查程序运行期占用与残留，辅助性能优化验收。

用法：
  1) 程序正常运行时执行一次；
  2) 观察"空闲状态"与"转写任务中"两组数据对比；
  3) 下方每项都给出建议阈值，超出即需要关注。

纯标准库实现（不依赖 psutil），输出为表格文本。
"""
from __future__ import annotations

import ctypes
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def process_mem(pid: int) -> dict:
    """Windows: 获取进程工作集（物理内存）与私有字节（含虚拟）。"""
    class PEB(ctypes.Structure):
        # 与 Windows PROCESS_MEMORY_COUNTERS 完全一致（注意 PageFaultCount 不可省略）
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    info = PEB()
    info.cb = ctypes.sizeof(PEB)
    h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_LIMITED | QUERY_INFORMATION
    if not h:
        return {}
    try:
        ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(info), ctypes.sizeof(info))
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    return {
        "working_mb": info.WorkingSetSize / 1048576,
        "private_mb": info.PrivateUsage / 1048576,
    }


def main() -> int:
    print("=" * 56)
    print("口播违禁词检测 · 性能检测报告")
    print("=" * 56)

    # 1. 服务进程占用
    print("\n[1] 本程序进程（python.exe 运行 app/main.py）")
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -match 'main.py' } | "
         "ForEach-Object { $_.ProcessId }"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).decode("utf-8", "ignore").split()
    for pid_str in out:
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        mem = process_mem(pid)
        if not mem:
            continue
        print(f"    PID {pid}: 物理内存 {mem['working_mb']:.0f} MB "
              f"(私有 {mem['private_mb']:.0f} MB)")

    # 2. 任务队列
    print("\n[2] 任务队列（/api/tasks 统计）")
    try:
        import httpx
        r = httpx.get("http://127.0.0.1:8765/api/tasks", timeout=3, trust_env=False)
        counts = r.json()["counts"]
        print(f"    排队 {counts.get('queued', 0)} · 进行中 {counts.get('running', 0)} · "
              f"完成 {counts.get('done', 0)} · 失败 {counts.get('error', 0)}")
    except Exception as e:  # noqa: BLE001
        print(f"    服务未在 8765 响应：{e}")

    # 3. 数据/日志体积（残留排查）
    print("\n[3] data 目录体积")
    total = 0
    rows = []
    for d in sorted(p.name for p in DATA.iterdir() if p.is_dir()):
        size = sum(f.stat().st_size for f in (DATA / d).rglob("*") if f.is_file())
        rows.append((d, size))
        total += size
    for d, size in rows:
        flag = " ⚠" if size > 200 * 1048576 and d not in ("models",) else ""
        print(f"    {d:<12} {size / 1048576:8.1f} MB{flag}")
    print(f"    {'合计':<12} {total / 1048576:8.1f} MB")

    print("\n    logs/app.log 与导出文件：")
    for p in sorted((DATA.parent / "logs").glob("*.log*")) + sorted((DATA / "exports").glob("*"))[-5:]:
        mb = p.stat().st_size / 1048576
        print(f"    {p.name:<50} {mb:6.2f} MB")

    # 4. 临时/残留
    print("\n[4] 残留检查")
    tmp = list((DATA / "media").glob("_tmp_*"))
    part = list((DATA / "models" / "local").rglob("*.part"))
    print(f"    临时音轨残留 _tmp_* ：{len(tmp)}"
          + (f"（{sum(f.stat().st_size for f in tmp)/1048576:.0f} MB）" if tmp else ""))
    print(f"    未完成的模型下载 .part：{len(part)}")

    # 5. 数据库体积
    try:
        db = sqlite3.connect(DATA / "app.db")
        n_v = db.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        n_w = db.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        n_h = db.execute("SELECT COUNT(*) FROM hits").fetchone()[0]
        db.close()
        print(f"\n[5] 数据库：{n_v} 视频 / {n_h} 命中，词库 {n_w} 词 "
              f"（app.db {(DATA/'app.db').stat().st_size/1048576:.1f} MB）")
    except Exception as e:  # noqa: BLE001
        print(f"\n[5] 数据库读取失败：{e}")

    print("\n阈值参考（超过建议处理）：")
    print("  · 空闲时 python 物理内存 > 模型体积 + 300MB → 检查泄漏")
    print("  · 无任务时 /api/tasks 每 30s 轮询（前端已节流）→ 正常")
    print("  · data 体积：models 为大头且必要；media/subtitles 可手动清理")
    print("  · logs/app.log > 20MB 会自动轮转（保留 10 份）→ 正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())