"""Live sensor sampling: LibreHardwareMonitor (if running) + nvidia-smi + psutil.

Everything is boiled down to one flat dict of canonical metrics, e.g.
{"cpu.temp": 71.5, "gpu.load": 98, "gpu.thr_thermal": 0, ...}, so the rules
don't care where a number came from.
"""

from __future__ import annotations

import os
import shutil
import time
from statistics import mean

import httpx
import psutil

from .util import parse_number, run_cmd

LHM_URL = os.environ.get("LHM_URL", "http://127.0.0.1:8085/data.json")

psutil.cpu_percent(percpu=True)  # prime: first call always returns 0

_GROUP_TYPES = {
    "Temperatures": "Temperature", "Clocks": "Clock", "Load": "Load", "Powers": "Power",
    "Fans": "Fan", "Voltages": "Voltage", "Data": "Data", "SmallData": "SmallData",
    "Controls": "Control", "Throughput": "Throughput", "Levels": "Level", "Factors": "Factor",
    "Currents": "Current",
}


def _hw_kind(node: dict) -> str:
    hid = (node.get("HardwareId") or "").lower()
    img = (node.get("ImageURL") or "").lower()
    s = hid + " " + img
    if "cpu" in s:
        return "cpu"
    if "gpu" in s or "nvidia" in s or "ati.png" in s or "amd.png" in s:
        return "gpu"
    if "nvme" in s or "hdd" in s or "ssd" in s or "storage" in s:
        return "storage"
    if "ram" in s or "memory" in s:
        return "memory"
    if "mainboard" in s or "lpc" in s or "chip" in s:
        return "motherboard"
    return "other"


def parse_lhm(data: dict) -> list[dict]:
    """Flatten LHM's data.json tree into readings.

    Tree: root -> computer -> hardware -> (sub-hardware) -> sensor group -> sensor.
    """
    readings: list[dict] = []

    def walk(node: dict, hardware: dict | None, depth: int):
        children = node.get("Children") or []
        text = node.get("Text") or ""
        if depth >= 2 and text not in _GROUP_TYPES and children and hardware is None:
            hardware = {"name": text, "kind": _hw_kind(node)}
        if not children:
            value = parse_number(node.get("Value"))
            if value is None or hardware is None:
                return
            stype = node.get("Type") or node.get("_group_type")
            readings.append({
                "hardware": hardware["name"], "kind": hardware["kind"], "type": stype,
                "name": text, "value": value,
                "unit": (str(node.get("Value")).split(" ", 1)[1:] or [""])[0].strip(),
            })
            return
        group_type = _GROUP_TYPES.get(text)
        for c in children:
            if group_type:
                c = dict(c, _group_type=group_type)
            walk(c, hardware, depth + 1)

    walk(data, None, 0)
    return readings


def lhm_available() -> bool:
    try:
        return httpx.get(LHM_URL, timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def read_lhm() -> list[dict] | None:
    try:
        r = httpx.get(LHM_URL, timeout=1.5)
        if r.status_code != 200:
            return None
        return parse_lhm(r.json())
    except (httpx.HTTPError, ValueError):
        return None


def _pick(readings, kind, stype, names=(), contains=(), exclude=(), agg=max):
    vals = []
    for r in readings:
        if r["kind"] != kind or r["type"] != stype:
            continue
        n = r["name"]
        if any(x.lower() in n.lower() for x in exclude):
            continue
        if names and n in names:
            return r["value"]
        if contains and any(x.lower() in n.lower() for x in contains):
            vals.append(r["value"])
        elif not names and not contains:
            vals.append(r["value"])
    return agg(vals) if vals else None


def lhm_metrics(readings: list[dict]) -> dict:
    m: dict[str, float] = {}

    def put(key, v):
        if v is not None:
            m[key] = round(float(v), 1)

    gpus = {r["hardware"] for r in readings if r["kind"] == "gpu"}
    # Prefer a discrete GPU if an iGPU is also listed.
    gpu_name = next((g for g in gpus if "intel" not in g.lower()), next(iter(gpus), None))
    gr = [r for r in readings if r["kind"] != "gpu" or r["hardware"] == gpu_name]

    put("cpu.temp", _pick(gr, "cpu", "Temperature",
                          names=("Core (Tctl/Tdie)", "CPU Package", "Package", "Core Max"),
                          exclude=("Distance", "TjMax")))
    if "cpu.temp" not in m:
        put("cpu.temp", _pick(gr, "cpu", "Temperature", exclude=("Distance", "TjMax")))
    put("cpu.load", _pick(gr, "cpu", "Load", names=("CPU Total",)))
    put("cpu.core_load_max", _pick(gr, "cpu", "Load", contains=("Core",), exclude=("Total",)))
    put("cpu.clock", _pick(gr, "cpu", "Clock", contains=("Core #",), agg=mean))
    put("cpu.power", _pick(gr, "cpu", "Power", names=("Package", "CPU Package")))

    put("gpu.temp", _pick(gr, "gpu", "Temperature", names=("GPU Core",)))
    put("gpu.hotspot", _pick(gr, "gpu", "Temperature", contains=("Hot Spot",)))
    put("gpu.load", _pick(gr, "gpu", "Load", names=("GPU Core",)))
    put("gpu.clock", _pick(gr, "gpu", "Clock", names=("GPU Core",)))
    put("gpu.power", _pick(gr, "gpu", "Power", names=("GPU Package", "GPU Power")))
    put("gpu.fan_pct", _pick(gr, "gpu", "Control", contains=("Fan",)))
    put("gpu.mem_used", _pick(gr, "gpu", "SmallData", names=("GPU Memory Used",)))
    put("gpu.mem_total", _pick(gr, "gpu", "SmallData", names=("GPU Memory Total",)))

    put("ram.used_pct", _pick(gr, "memory", "Load", names=("Memory",)))
    put("storage.temp_max", _pick(gr, "storage", "Temperature"))
    return m


# --- nvidia-smi --------------------------------------------------------------

_NV_BASE = ["temperature.gpu", "utilization.gpu", "clocks.gr", "clocks.max.gr", "power.draw",
            "enforced.power.limit", "fan.speed", "memory.used", "memory.total", "pstate",
            "pcie.link.gen.current", "pcie.link.gen.max", "pcie.link.width.current", "pcie.link.width.max"]
_NV_REASONS = ["hw_thermal_slowdown", "sw_thermal_slowdown", "sw_power_cap", "hw_slowdown",
               "hw_power_brake_slowdown"]
_NV_KEYS = {
    "temperature.gpu": "gpu.temp", "utilization.gpu": "gpu.load", "clocks.gr": "gpu.clock",
    "clocks.max.gr": "gpu.clock_max", "power.draw": "gpu.power", "enforced.power.limit": "gpu.power_limit",
    "fan.speed": "gpu.fan_pct", "memory.used": "gpu.mem_used", "memory.total": "gpu.mem_total",
    "pstate": "gpu.pstate", "pcie.link.gen.current": "gpu.pcie_gen", "pcie.link.gen.max": "gpu.pcie_gen_max",
    "pcie.link.width.current": "gpu.pcie_width", "pcie.link.width.max": "gpu.pcie_width_max",
}
_nv_fieldset: list[str] | None = None


def _nv_query(fields: list[str]) -> list[str] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    code, out = run_cmd([exe, f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"], timeout=5)
    if code != 0 or not out.strip():
        return None
    return [p.strip() for p in out.strip().splitlines()[0].split(",")]


def nvidia_metrics() -> dict:
    """Query nvidia-smi; tries new (clocks_event_reasons) then old (clocks_throttle_reasons) names."""
    global _nv_fieldset
    candidates = [_nv_fieldset] if _nv_fieldset else [
        _NV_BASE + [f"clocks_event_reasons.{r}" for r in _NV_REASONS],
        _NV_BASE + [f"clocks_throttle_reasons.{r}" for r in _NV_REASONS],
        _NV_BASE,
    ]
    for fields in candidates:
        vals = _nv_query(fields)
        if vals and len(vals) == len(fields):
            _nv_fieldset = fields
            break
    else:
        return {}
    m: dict[str, float] = {}
    reasons = {}
    for f, v in zip(fields, vals):
        if "reasons." in f:
            reasons[f.split(".", 1)[1]] = 1 if v.lower() == "active" else 0
            continue
        if f == "pstate":
            n = parse_number(v)
            if n is not None:
                m["gpu.pstate"] = n
            continue
        n = parse_number(v)
        if n is not None:
            m[_NV_KEYS[f]] = n
    if reasons:
        m["gpu.thr_thermal"] = max(reasons.get("hw_thermal_slowdown", 0), reasons.get("sw_thermal_slowdown", 0))
        m["gpu.thr_power"] = reasons.get("sw_power_cap", 0)
        m["gpu.thr_hw"] = max(reasons.get("hw_slowdown", 0), reasons.get("hw_power_brake_slowdown", 0))
    return m


def psutil_metrics() -> dict:
    per_core = psutil.cpu_percent(percpu=True)
    vm = psutil.virtual_memory()
    m = {
        "cpu.load": round(mean(per_core), 1) if per_core else None,
        "cpu.core_load_max": max(per_core) if per_core else None,
        "ram.used_pct": vm.percent,
        "ram.used_gb": round(vm.used / 1024**3, 1),
    }
    freq = psutil.cpu_freq()
    if freq and freq.current:
        m["cpu.clock_reported"] = round(freq.current)
    return {k: v for k, v in m.items() if v is not None}


def sample(include_raw: bool = False) -> dict:
    sources = []
    metrics: dict[str, float] = {}
    ps = psutil_metrics()
    metrics.update(ps)
    sources.append("psutil")
    raw = read_lhm()
    if raw:
        metrics.update(lhm_metrics(raw))  # LHM beats psutil for CPU numbers
        sources.append("LibreHardwareMonitor")
    nv = nvidia_metrics()
    if nv:
        metrics.update(nv)  # nvidia-smi is the most exact source for NVIDIA cards
        sources.append("nvidia-smi")
    out = {"ts": time.time(), "metrics": metrics, "sources": sources}
    if include_raw and raw:
        out["raw"] = raw
    return out
