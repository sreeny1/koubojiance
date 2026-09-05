"""系统能力检测：显卡识别 + 最低配置门槛。

用于两处：
1. 启动器/服务在启动前检查最低配置（内存/磁盘/AVX2），不足则友好提示；
2. /api/status 下发显卡与配置信息，前端据此显示"检测到 AMD 显卡，使用 CPU 模式"。

显卡策略（与 ctranslate2 生态能力一致）：
- NVIDIA 且 CUDA 可用 → GPU 加速；
- AMD / Intel / 无独显 → CPU int8 兜底（ctranslate2 在 Windows 无 ROCm/OpenCL 后端）。
"""
from __future__ import annotations

import shutil
from pathlib import Path

MIN_RAM_GB = 8
MIN_FREE_DISK_GB = 8


def _cpu_avx2() -> bool:
    """CPU 是否支持 AVX2（ctranslate2 CPU 推理硬性要求）。检测失败不阻断。"""
    try:
        import ctypes

        # PF_AVX2_INSTRUCTIONS_AVAILABLE == 40
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
    except Exception:  # noqa: BLE001
        return True


def _ram_gb() -> float | None:
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return m.ullTotalPhys / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return None


def _free_disk_gb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except Exception:  # noqa: BLE001
        return None


def _win_gpu_names() -> list[str]:
    """枚举 Windows 显示适配器名称（含虚拟显示），尽力而为。"""
    names: list[str] = []
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}",
        )
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
                with winreg.OpenKey(key, sub) as k:
                    names.append(str(winreg.QueryValueEx(k, "DriverDesc")[0]))
            except OSError:
                break
            i += 1
    except Exception:  # noqa: BLE001
        pass
    return names


def detect_gpu() -> dict:
    """识别当前可用加速设备。返回 {vendor, name, cuda, recommended_device}。"""
    info: dict = {"vendor": "unknown", "name": "", "cuda": False, "recommended_device": "cpu"}

    cuda = False
    try:
        import ctranslate2

        cuda = ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        cuda = False
    info["cuda"] = cuda

    names = _win_gpu_names()
    # 取第一个非虚拟显示适配器作为主显卡名
    for n in names:
        low = n.lower()
        if "virtual" in low or "basic display" in low or "microsoft" in low:
            continue
        info["name"] = n
        break
    if not info["name"] and names:
        info["name"] = names[0]

    if cuda:
        info["vendor"] = "nvidia"
        info["recommended_device"] = "cuda"
    else:
        joined = " ".join(names).lower()
        if "amd" in joined or "radeon" in joined:
            info["vendor"] = "amd"
        elif "intel" in joined:
            info["vendor"] = "intel"
        else:
            info["vendor"] = "none" if not names else "unknown"
        info["recommended_device"] = "cpu"
    return info


def check_requirements(data_dir: Path | None = None) -> dict:
    """最低配置门槛检查。返回 {ok, problems, ram_gb, free_disk_gb, avx2}。"""
    problems: list[str] = []
    ram = _ram_gb()
    if ram is not None and ram < MIN_RAM_GB:
        problems.append(f"物理内存不足：{ram:.1f}GB（最低要求 {MIN_RAM_GB}GB）")
    free = _free_disk_gb(data_dir or Path.cwd())
    if free is not None and free < MIN_FREE_DISK_GB:
        problems.append(f"磁盘剩余空间不足：{free:.1f}GB（最低要求 {MIN_FREE_DISK_GB}GB）")
    avx2 = _cpu_avx2()
    if not avx2:
        problems.append("CPU 不支持 AVX2 指令集，本地语音转写引擎无法运行")
    return {
        "ok": not problems,
        "problems": problems,
        "ram_gb": round(ram, 1) if ram is not None else None,
        "free_disk_gb": round(free, 1) if free is not None else None,
        "avx2": avx2,
    }


def system_info(data_dir: Path | None = None) -> dict:
    """供 /api/status 下发的完整系统信息。"""
    return {"gpu": detect_gpu(), "requirements": check_requirements(data_dir)}
