"""Static hardware/system detection: CPU, GPU (+ real VRAM), RAM, OS, BIOS, disks."""

from __future__ import annotations

import re
import shutil
import time

import psutil

from .util import as_list, parse_date, run_cmd, run_powershell_json

_PS_SCRIPT = r"""
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,Manufacturer
$cs = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,TotalPhysicalMemory,AutomaticManagedPagefile
$os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,@{n='LastBoot';e={$_.LastBootUpTime.ToString('o')}}
$bios = Get-CimInstance Win32_BIOS | Select-Object SMBIOSBIOSVersion,Manufacturer,@{n='ReleaseDate';e={$_.ReleaseDate.ToString('o')}}
$board = Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product
$mem = @(Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,SMBIOSMemoryType,DeviceLocator,BankLabel)
$vc = @(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,@{n='DriverDate';e={$_.DriverDate.ToString('o')}},AdapterRAM,PNPDeviceID)
$classKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
$reg = @(Get-ChildItem $classKey | Where-Object { $_.PSChildName -match '^\d{4}$' } | ForEach-Object {
  $p = Get-ItemProperty $_.PSPath
  $ms = $p.'HardwareInformation.MemorySize'
  if ($ms -is [byte[]]) { $ms = [BitConverter]::ToUInt32($ms, 0) }
  [pscustomobject]@{ DriverDesc = $p.DriverDesc; Qw = $p.'HardwareInformation.qwMemorySize'; Mem = $ms }
})
$disks = @(Get-PhysicalDisk | ForEach-Object { [pscustomobject]@{ Name = $_.FriendlyName; Media = "$($_.MediaType)"; Bus = "$($_.BusType)"; Health = "$($_.HealthStatus)"; Size = $_.Size } })
$pf = @(Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize)
$scheme = (powercfg /getactivescheme) -join ' '
[pscustomobject]@{ cpu=$cpu; cs=$cs; os=$os; bios=$bios; board=$board; mem=$mem; vc=$vc; reg=$reg; disks=$disks; pagefile=$pf; powerScheme=$scheme } | ConvertTo-Json -Depth 4 -Compress
"""

POWER_SCHEMES = {
    "a1841308-3541-4fab-bc81-f71556f20b4a": "Energisparläge",
    "381b4222-f694-41f0-9685-ff5bb260df2e": "Balanserad",
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "Hög prestanda",
    "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimat prestanda",
}

MEMORY_TYPES = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 34: "DDR5", 35: "LPDDR5"}

_VIRTUAL_GPU = re.compile(r"basic display|remote|virtual|parsec|meta|citrix|vmware|hyper-v|idd", re.I)
_INTEGRATED = re.compile(r"intel.*(uhd|iris|hd graphics|arc\(tm\) graphics)|radeon\(tm\) graphics|radeon graphics|vega \d+ graphics", re.I)

NVIDIA_SMI_STATIC = "name,memory.total,driver_version,pcie.link.gen.max,pcie.link.width.max"


def _nvidia_smi() -> str | None:
    return shutil.which("nvidia-smi")


def detect_nvidia() -> list[dict]:
    exe = _nvidia_smi()
    if not exe:
        return []
    code, out = run_cmd([exe, f"--query-gpu={NVIDIA_SMI_STATIC}", "--format=csv,noheader,nounits"])
    if code != 0:
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            vram_gb = round(float(parts[1]) / 1024, 1)
        except ValueError:
            vram_gb = None
        gpus.append({
            "name": parts[0], "vendor": "NVIDIA", "vram_gb": vram_gb, "vram_source": "nvidia-smi",
            "driver_version": parts[2], "integrated": False,
        })
    return gpus


def _vram_from_registry(name: str, reg: list[dict]) -> float | None:
    for r in reg:
        if (r.get("DriverDesc") or "").strip().lower() == name.strip().lower():
            for key in ("Qw", "Mem"):
                v = r.get(key)
                if isinstance(v, (int, float)) and v > 0:
                    return round(v / 1024**3, 1)
    return None


def _vendor(name: str) -> str:
    n = name.lower()
    if "nvidia" in n or "geforce" in n or "rtx" in n:
        return "NVIDIA"
    if "amd" in n or "radeon" in n:
        return "AMD"
    if "intel" in n or "arc" in n:
        return "Intel"
    return "Okänd"


def build_gpu_list(raw: dict, nvidia: list[dict]) -> list[dict]:
    """Combine nvidia-smi (exact) with WMI + registry (AMD/Intel) into one GPU list.

    Note: Win32_VideoController.AdapterRAM is a 32-bit field and caps at 4 GB,
    so we only use it as a last resort and mark it as capped.
    """
    reg = as_list(raw.get("reg"))
    gpus: list[dict] = []
    nv_names = {g["name"].lower() for g in nvidia}
    for vc in as_list(raw.get("vc")):
        name = (vc.get("Name") or "").strip()
        if not name or _VIRTUAL_GPU.search(name):
            continue
        drv_date = parse_date(vc.get("DriverDate"))
        match = next((g for g in nvidia if g["name"].lower() == name.lower()
                      or g["name"].lower() in name.lower()), None)
        if match:
            gpu = dict(match)
        else:
            if name.lower() in nv_names:
                continue
            vram = _vram_from_registry(name, reg)
            source = "registry"
            if vram is None and vc.get("AdapterRAM"):
                vram = round(vc["AdapterRAM"] / 1024**3, 1)
                source = "wmi (max 4 GB)"
            gpu = {"name": name, "vendor": _vendor(name), "vram_gb": vram, "vram_source": source,
                   "driver_version": vc.get("DriverVersion"),
                   "integrated": bool(_INTEGRATED.search(name)) or (vram is not None and vram < 2)}
        gpu["driver_date"] = drv_date.date().isoformat() if drv_date else None
        gpus.append(gpu)
    # nvidia-smi GPUs that WMI somehow didn't list
    for g in nvidia:
        if not any(x["name"] == g["name"] for x in gpus):
            gpus.append(dict(g, driver_date=None))
    return gpus


def primary_gpu(gpus: list[dict]) -> dict | None:
    discrete = [g for g in gpus if not g.get("integrated")]
    pool = discrete or gpus
    if not pool:
        return None
    return max(pool, key=lambda g: g.get("vram_gb") or 0)


def parse_rated_speed(part_number: str | None) -> int | None:
    """Guess a RAM kit's rated (XMP/EXPO) speed from its part number.

    E.g. 'CMK32GX5M2B6000C36' -> 6000, 'F4-3600C16-16GVKC' -> 3600, 'KF432C16BB/16' -> 3200.
    """
    if not part_number:
        return None
    pn = part_number.strip().upper()
    m = re.search(r"(?<!\d)(2[4-9]\d{2}|[3-8]\d{3})(?!\d)", pn)
    if m:
        return int(m.group(1))
    m = re.search(r"KF[45]?(\d{2})C", pn)  # Kingston Fury short code: KF432 -> 3200
    if m:
        return int(m.group(1)) * 100
    return None


def build_memory(raw: dict) -> dict:
    sticks = []
    for m in as_list(raw.get("mem")):
        sticks.append({
            "slot": m.get("DeviceLocator") or m.get("BankLabel"),
            "size_gb": round((m.get("Capacity") or 0) / 1024**3),
            "speed": m.get("Speed"),
            "configured_speed": m.get("ConfiguredClockSpeed"),
            "manufacturer": (m.get("Manufacturer") or "").strip(),
            "part_number": (m.get("PartNumber") or "").strip(),
            "type": MEMORY_TYPES.get(m.get("SMBIOSMemoryType"), None),
            "rated_speed_guess": parse_rated_speed(m.get("PartNumber")),
        })
    total = (raw.get("cs") or {}).get("TotalPhysicalMemory") or psutil.virtual_memory().total
    return {"total_gb": round(total / 1024**3, 1), "sticks": sticks}


def _disk_media(value: str) -> str:
    v = (value or "").strip()
    return {"3": "HDD", "4": "SSD", "5": "SCM", "0": "Okänd"}.get(v, v or "Okänd")


def _disk_bus(value: str) -> str:
    v = (value or "").strip()
    return {"17": "NVMe", "11": "SATA", "7": "USB", "8": "RAID", "10": "SAS"}.get(v, v or "Okänd")


def build_disks(raw: dict) -> list[dict]:
    disks = []
    for d in as_list(raw.get("disks")):
        disks.append({"name": (d.get("Name") or "").strip(), "media": _disk_media(d.get("Media")),
                      "bus": _disk_bus(d.get("Bus")), "health": d.get("Health"),
                      "size_gb": round((d.get("Size") or 0) / 1e9)})
    volumes = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        volumes.append({"mount": p.mountpoint, "total_gb": round(u.total / 1e9),
                        "free_gb": round(u.free / 1e9, 1), "free_pct": round(100 - u.percent, 1)})
    return disks, volumes


def detect() -> dict:
    t0 = time.time()
    raw = run_powershell_json(_PS_SCRIPT) or {}
    nvidia = detect_nvidia()
    gpus = build_gpu_list(raw, nvidia)
    cpu = raw.get("cpu") or {}
    os_ = raw.get("os") or {}
    bios = raw.get("bios") or {}
    board = raw.get("board") or {}
    cs = raw.get("cs") or {}
    disks, volumes = build_disks(raw)
    scheme_text = (raw.get("powerScheme") or "").lower()
    scheme_guid = next((g for g in POWER_SCHEMES if g in scheme_text), None)
    scheme_name_m = re.search(r"\(([^)]+)\)\s*$", raw.get("powerScheme") or "")
    bios_date = parse_date(bios.get("ReleaseDate"))
    boot = parse_date(os_.get("LastBoot"))
    return {
        "cpu": {
            "name": (cpu.get("Name") or "").strip() or None,
            "cores": cpu.get("NumberOfCores") or psutil.cpu_count(logical=False),
            "threads": cpu.get("NumberOfLogicalProcessors") or psutil.cpu_count(),
            "max_mhz": cpu.get("MaxClockSpeed"),
        },
        "gpus": gpus,
        "primary_gpu": primary_gpu(gpus),
        "memory": build_memory(raw),
        "os": {"name": os_.get("Caption"), "build": os_.get("BuildNumber"),
               "last_boot": boot.isoformat() if boot else None},
        "board": {"manufacturer": board.get("Manufacturer"), "model": board.get("Product"),
                  "system": f"{cs.get('Manufacturer') or ''} {cs.get('Model') or ''}".strip()},
        "bios": {"version": bios.get("SMBIOSBIOSVersion"),
                 "date": bios_date.date().isoformat() if bios_date else None},
        "disks": disks,
        "volumes": volumes,
        "pagefile": {"auto": cs.get("AutomaticManagedPagefile"),
                     "files": [p.get("Name") for p in as_list(raw.get("pagefile"))]},
        "power_scheme": {"guid": scheme_guid,
                         "name": POWER_SCHEMES.get(scheme_guid) or (scheme_name_m.group(1) if scheme_name_m else None)},
        "detect_seconds": round(time.time() - t0, 2),
    }
