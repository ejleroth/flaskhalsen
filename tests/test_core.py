from datetime import datetime, timedelta, timezone

from flaskhalsen import hardware, models, rules, sensors
from flaskhalsen.events import bugcheck_code, describe
from flaskhalsen.util import parse_date, parse_number

CFG = {
    "headroom_gb": 1.5,
    "tiers": [
        {"id": "tiny", "label": "t", "min_vram_gb": 0, "models": ["qwen3:4b"]},
        {"id": "small", "label": "s", "min_vram_gb": 5.5, "models": ["qwen3:8b"]},
        {"id": "medium", "label": "m", "min_vram_gb": 10.5, "models": ["qwen3:14b"]},
        {"id": "large", "label": "l", "min_vram_gb": 20, "models": ["qwen3:30b"]},
    ],
    "cpu_only": {"small_min_ram_gb": 24},
}


def hw_with_gpu(vram, integrated=False, ram=32):
    return {"primary_gpu": {"name": "GPU", "vram_gb": vram, "integrated": integrated},
            "memory": {"total_gb": ram}}


# --- utils ------------------------------------------------------------------------

def test_parse_number_handles_swedish_decimal_comma():
    assert parse_number("45,5 °C") == 45.5
    assert parse_number("1234.0 MHz") == 1234.0
    assert parse_number("n/a") is None


def test_parse_date_formats():
    assert parse_date("/Date(1700000000000)/").year == 2023
    assert parse_date("2026-09-11T00:00:00.0000000+02:00").month == 9


# --- model picking ----------------------------------------------------------------

def test_tier_by_vram():
    assert models.recommend(hw_with_gpu(4), [], CFG)["tier"]["id"] == "tiny"
    assert models.recommend(hw_with_gpu(8), [], CFG)["tier"]["id"] == "small"
    assert models.recommend(hw_with_gpu(10), [], CFG)["tier"]["id"] == "small"  # 10 - 1.5 headroom
    assert models.recommend(hw_with_gpu(12), [], CFG)["tier"]["id"] == "medium"
    assert models.recommend(hw_with_gpu(24), [], CFG)["tier"]["id"] == "large"


def test_cpu_only_uses_ram():
    r = models.recommend({"primary_gpu": None, "memory": {"total_gb": 16}}, [], CFG)
    assert r["cpu_only"] and r["tier"]["id"] == "tiny"
    r = models.recommend({"primary_gpu": None, "memory": {"total_gb": 32}}, [], CFG)
    assert r["tier"]["id"] == "small"
    r = models.recommend(hw_with_gpu(1, integrated=True, ram=16), [], CFG)
    assert r["cpu_only"]


def test_installed_match_and_fallback():
    installed = [{"name": "qwen3:8b", "size": 5.2e9}]
    r = models.recommend(hw_with_gpu(10), installed, CFG)
    assert r["installed_match"] == "qwen3:8b" and r["suggested_download"] is None
    # 24 GB card wants qwen3:30b; falls back to biggest installed model that fits
    r = models.recommend(hw_with_gpu(24), installed, CFG)
    assert r["suggested_download"] == "qwen3:30b" and r["auto_choice"] == "qwen3:8b"
    # model too big for card is not chosen
    r = models.recommend(hw_with_gpu(6), [{"name": "qwen3:14b", "size": 9.3e9}], CFG)
    assert r["auto_choice"] is None


# --- hardware helpers -------------------------------------------------------------

def test_rated_speed_from_part_number():
    assert hardware.parse_rated_speed("CMW32GX4M2C3200C16") == 3200
    assert hardware.parse_rated_speed("CMK32GX5M2B6000C36") == 6000
    assert hardware.parse_rated_speed("F4-3600C16D-16GVKC") == 3600
    assert hardware.parse_rated_speed("KF432C16BB/16") == 3200
    assert hardware.parse_rated_speed("") is None


def test_gpu_list_prefers_registry_over_capped_wmi():
    raw = {"vc": [{"Name": "AMD Radeon RX 7800 XT", "AdapterRAM": 4293918720, "DriverVersion": "1"}],
           "reg": [{"DriverDesc": "AMD Radeon RX 7800 XT", "Qw": 17163091968}]}
    gpus = hardware.build_gpu_list(raw, [])
    assert gpus[0]["vram_gb"] == 16.0 and gpus[0]["vram_source"] == "registry"


# --- LHM parsing ------------------------------------------------------------------

LHM = {"Text": "Sensor", "Children": [{"Text": "PC", "Children": [
    {"Text": "AMD Ryzen 7 5800X3D", "ImageURL": "images_icon/cpu.png", "Children": [
        {"Text": "Temperatures", "Children": [
            {"Text": "Core (Tctl/Tdie)", "Value": "71,3 °C", "Children": []}]},
        {"Text": "Load", "Children": [
            {"Text": "CPU Total", "Value": "40,0 %", "Children": []},
            {"Text": "CPU Core #1", "Value": "99,0 %", "Children": []}]}]},
    {"Text": "NVIDIA GeForce RTX 3080", "ImageURL": "images_icon/nvidia.png", "Children": [
        {"Text": "Temperatures", "Children": [
            {"Text": "GPU Core", "Value": "65,0 °C", "Children": []},
            {"Text": "GPU Hot Spot", "Value": "80,0 °C", "Children": []}]}]}]}]}


def test_lhm_parse_and_metrics():
    readings = sensors.parse_lhm(LHM)
    m = sensors.lhm_metrics(readings)
    assert m["cpu.temp"] == 71.3
    assert m["cpu.load"] == 40.0 and m["cpu.core_load_max"] == 99.0
    assert m["gpu.temp"] == 65.0 and m["gpu.hotspot"] == 80.0


# --- events -----------------------------------------------------------------------

def test_shutdown_dedupe_keeps_death_time():
    from flaskhalsen.events import drop_duplicate_shutdowns
    evs = [{"t": "2026-11-24T08:05:49+01:00", "id": 41, "cat": "crash", "props": ["0"], "text": "x."},
           {"t": "2026-11-24T08:05:53+01:00", "id": 6008, "cat": "crash",
            "props": ["01:12:35", "‎2026-‎11-‎24"], "text": "y"}]
    out = drop_duplicate_shutdowns(evs)
    assert len(out) == 1 and out[0]["died_at"] == "2026-11-24 01:12"


def test_bugcheck_parsing():
    assert bugcheck_code({"id": 1001, "props": ["0x00000124 (0x0, 0x1)"]}) == 0x124
    assert bugcheck_code({"id": 41, "props": ["292", "0"]}) == 292
    assert bugcheck_code({"id": 41, "props": ["0", "0"]}) is None
    assert "WHEA_UNCORRECTABLE" in describe({"cat": "bsod", "id": 1001, "props": ["0x00000124"]})


# --- rules ------------------------------------------------------------------------

def ids(findings):
    return {f["id"] for f in findings}


def test_xmp_off_detected():
    hw = {"memory": {"sticks": [{"configured_speed": 2133, "rated_speed_guess": 3200, "type": "DDR4",
                                 "part_number": "X", "size_gb": 16}] * 2}}
    assert "ram_xmp_off" in ids(rules.diagnose(hw, []))
    hw["memory"]["sticks"] = [dict(hw["memory"]["sticks"][0], configured_speed=3200)] * 2
    assert "ram_xmp_off" not in ids(rules.diagnose(hw, []))


def test_single_channel():
    hw = {"memory": {"sticks": [{"configured_speed": 3200, "type": "DDR4", "size_gb": 16}]}}
    assert "ram_single_channel" in ids(rules.diagnose(hw, []))


def _ev(cat, eid, props=(), days_ago=1):
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"t": t, "cat": cat, "id": eid, "provider": "x", "props": list(props)}


def test_event_rules():
    evs = [_ev("crash", 41, ["0"]), _ev("crash", 41, ["0"]), _ev("crash", 41, ["0"]),
           _ev("whea", 19), _ev("bsod", 1001, ["0x00000124"]), _ev("crash", 41, ["0"], days_ago=40)]
    f = {x["id"]: x for x in rules.diagnose({}, evs)}
    assert f["hard_shutdowns"]["evidence"]["count"] == 3  # 40-day-old one excluded
    assert f["hard_shutdowns"]["severity"] == "kritisk"
    assert "whea" in f and "bsod" in f


def _samples(n, **kw):
    return [dict({"ts": i}, **kw) for i in range(n)]


def test_bottleneck_cpu_vs_gpu():
    cpu = _samples(20, **{"gpu.load": 60, "cpu.load": 55, "cpu.core_load_max": 99})
    assert "bottleneck_cpu" in ids(rules.diagnose({}, [], cpu))
    gpu = _samples(20, **{"gpu.load": 99, "cpu.load": 30, "cpu.core_load_max": 60})
    assert "bottleneck_gpu" in ids(rules.diagnose({}, [], gpu))


def test_gpu_throttle_pcie_vram():
    s = _samples(20, **{"gpu.load": 99, "gpu.temp": 88, "gpu.thr_thermal": 1, "gpu.pcie_width": 8,
                        "gpu.pcie_width_max": 16, "gpu.mem_used": 9900, "gpu.mem_total": 10240})
    found = ids(rules.diagnose({}, [], s))
    assert {"gpu_thermal", "pcie_width", "vram_full"} <= found


def test_pcie_ignored_at_idle():
    s = _samples(20, **{"gpu.load": 5, "gpu.pcie_width": 16, "gpu.pcie_width_max": 16,
                        "gpu.pcie_gen": 1, "gpu.pcie_gen_max": 4})
    assert "pcie_gen" not in ids(rules.diagnose({}, [], s))
