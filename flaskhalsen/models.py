"""Pick a local model that fits the user's hardware, based on config/models.json."""

from __future__ import annotations

import json

from .util import CONFIG_DIR


def load_config() -> dict:
    with open(CONFIG_DIR / "models.json", encoding="utf-8") as f:
        return json.load(f)


def _base(tag: str) -> str:
    """'qwen3:8b-q4_K_M' -> 'qwen3:8b'; 'qwen3' -> 'qwen3:latest'."""
    name, _, variant = tag.partition(":")
    if not variant:
        return f"{name}:latest"
    return f"{name}:{variant.split('-')[0]}"


def usable_memory(hw: dict, cfg: dict) -> tuple[float, str, bool]:
    """Return (usable_gb, human-readable basis, cpu_only)."""
    gpu = hw.get("primary_gpu")
    if gpu and not gpu.get("integrated") and (gpu.get("vram_gb") or 0) >= 3:
        vram = gpu["vram_gb"]
        return max(0.0, vram - cfg.get("headroom_gb", 1.5)), f"{gpu['name']} ({vram:g} GB VRAM)", False
    ram = (hw.get("memory") or {}).get("total_gb") or 0
    return ram, f"Ingen lämplig GPU - kör på CPU ({ram:g} GB RAM)", True


def pick_tier(hw: dict, cfg: dict) -> tuple[dict, float, str, bool]:
    usable, basis, cpu_only = usable_memory(hw, cfg)
    tiers = sorted(cfg["tiers"], key=lambda t: t["min_vram_gb"])
    if cpu_only:
        tier = tiers[0]
        small_ram = cfg.get("cpu_only", {}).get("small_min_ram_gb", 24)
        if usable >= small_ram and len(tiers) > 1:
            tier = tiers[1]
        return tier, usable, basis, True
    tier = tiers[0]
    for t in tiers:
        if usable >= t["min_vram_gb"]:
            tier = t
    return tier, usable, basis, False


def recommend(hw: dict, installed: list[dict], cfg: dict | None = None) -> dict:
    """installed: Ollama /api/tags 'models' entries (name, size in bytes)."""
    cfg = cfg or load_config()
    tier, usable, basis, cpu_only = pick_tier(hw, cfg)
    installed_names = [m["name"] for m in installed]
    installed_bases = {_base(n): n for n in installed_names}

    match = next((installed_bases[_base(m)] for m in tier["models"] if _base(m) in installed_bases), None)

    fitting = []
    for m in installed:
        size_gb = (m.get("size") or 0) / 1e9
        fits = cpu_only or size_gb + 0.8 <= usable
        fitting.append({"name": m["name"], "size_gb": round(size_gb, 1), "fits": fits})

    # If the ideal tier model isn't installed, use the biggest installed model that fits.
    fallback = None
    if not match:
        fits = [f for f in fitting if f["fits"]]
        if fits:
            fallback = max(fits, key=lambda f: f["size_gb"])["name"]

    return {
        "basis": basis,
        "cpu_only": cpu_only,
        "usable_gb": round(usable, 1),
        "tier": {"id": tier["id"], "label": tier["label"], "approx_size_gb": tier.get("approx_size_gb")},
        "suggested_download": None if match else tier["models"][0],
        "installed_match": match,
        "auto_choice": match or fallback,
        "installed": fitting,
    }
