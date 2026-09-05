# -*- coding: utf-8 -*-
"""查询 ModelScope 模型文件的官方大小与 SHA256，并核对本地文件。"""
import hashlib
from pathlib import Path

import httpx

LOCAL = Path(__file__).resolve().parent.parent / "data" / "models" / "local" / "large-v3"

r = httpx.get(
    "https://modelscope.cn/api/v1/models/Systran/faster-whisper-large-v3/repo/files",
    params={"Revision": "master", "Root": ""},
    timeout=20, trust_env=False,
)
print("status:", r.status_code)
data = r.json()
files = [f for f in data["Data"]["Files"] if f["Type"] == "blob"]
for f in files:
    local = LOCAL / f["Path"]
    if local.exists():
        h = hashlib.sha256()
        with open(local, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        digest = h.hexdigest()
        size_ok = local.stat().st_size == f["Size"]
        hash_ok = digest == f.get("Sha256")
        print(f"{f['Path']}: 官方 {f['Size']}B / 本地 {local.stat().st_size}B "
              f"大小{'✓' if size_ok else '✗'} 哈希{'✓' if hash_ok else '✗'} "
              f"(本地sha256={digest[:16]}…)")
    else:
        print(f"{f['Path']}: 本地缺失（官方 {f['Size']}B, sha256={f.get('Sha256', '')[:16]}…）")
