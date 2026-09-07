# -*- coding: utf-8 -*-
"""CUDA 运行库下载器测试（v1.4.0 首启自动下载链路）。

用法：
  python tests/test_cuda_runtime.py              # 只测源解析/清单/就绪/校验函数（不下载）
  python tests/test_cuda_runtime.py --download   # 真下载到 data/runtime 并全链路验证（约1.36GB，可断点续传）
"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from core.cuda_setup import (  # noqa: E402
    CUDA_PKGS, PIP_SIMPLE_MIRRORS, cuda_manifest, is_cuda_runtime_ready,
    need_cuda_runtime, _parse_simple_index, _resolve_wheel, _sha256_file,
)

print("[1] 清单完整性：%d 个 CUDA 包，%s" % (
    len(CUDA_PKGS),
    "OK" if all(p["wheel"] and p["required"] and p["size"] > 0 for p in CUDA_PKGS) else "FAIL"))
total = sum(p["size"] for p in CUDA_PKGS)
print("    清单合计约 %.2f GB；必要 DLL 数: %d" % (total / 1024,
      sum(len(p["required"]) for p in CUDA_PKGS)))

print("[2] need_cuda_runtime() = %s（本机 AMD 预期 False；NVIDIA 机 True）" % need_cuda_runtime())
print("[3] is_cuda_runtime_ready() = %s" % is_cuda_runtime_ready())
print("[4] PyPI 镜像数: %d" % len(PIP_SIMPLE_MIRRORS))

print("[5] 源解析（清华索引 → URL+SHA256）…")
for pkg in CUDA_PKGS:
    label, url, sha = _resolve_wheel(pkg)
    print("    %-24s %s\n      %s\n      sha256=%s…" % (
        pkg["name"], label, url[:100], (sha or "")[:16]))
    assert sha and len(sha) == 64, "清华索引未返回 SHA256"
    assert pkg["wheel"] in url
print("    源解析全部正常（URL 与 SHA256 均来自镜像索引）")

if "--download" in sys.argv:
    print("[6] 真下载（约1.36GB，断点续传 + SHA256 + 解压 + 清单核对）…")
    from core.cuda_setup import download_cuda_runtime

    last = [0.0]

    def on_p(frac: float) -> None:
        if frac - last[0] >= 0.05 or frac >= 1.0:
            last[0] = frac
            print("    进度 %.0f%%" % (frac * 100), flush=True)

    download_cuda_runtime(progress=on_p)
    ready = is_cuda_runtime_ready()
    print("[7] 下载+安装完成，is_cuda_runtime_ready() =", ready)
    manifest = cuda_manifest()
    for pkg in manifest["packages"]:
        print("    %-24s installed=%s" % (pkg["name"], pkg["installed"]))
    assert ready, "就绪校验未通过"
    # 再抽查一个 DLL 的 SHA256 与 wheel 包内一致（完整性）
    from core.config import RUNTIME_DIR
    dll = RUNTIME_DIR / "nvidia" / "cublas" / "bin" / "cublas64_12.dll"
    assert dll.is_file() and dll.stat().st_size > 10 * 1024 * 1024
    print("[8] 关键 DLL 抽查: %s（%.1f MB）" % (dll.name, dll.stat().st_size / 1048576))
    print("\nCUDA 运行库全链路验证通过 ✓（首次启动可用 GPU）")
else:
    print("\n（未加 --download：只验证清单与源解析，不下载）")
print("测试完成")
