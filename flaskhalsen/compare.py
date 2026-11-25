"""Before/after: save a snapshot of the diagnosis, fix something, compare with now.

This is the "Gick inte. Nu går det." part: it turns a fix into visible proof.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .util import parse_date

# key -> (label, unit, higher_is_better)
METRICS = {
    "ram_speed": ("RAM-hastighet", "MT/s", True),
    "cpu.temp": ("CPU-temperatur", "°C", False),
    "gpu.temp": ("GPU-temperatur", "°C", False),
    "storage.temp_max": ("Varmaste NVMe-disk", "°C", False),
    "free_c_gb": ("Ledigt på C:", "GB", True),
}
STABILITY_CATS = ("crash", "bsod", "whea", "gpu_driver")
MIN_DAYS_FOR_STABILITY = 1
_CAT_LABEL = {"crash": "oväntade avstängningar", "bsod": "blåskärmar", "whea": "hårdvarufel (WHEA)",
              "gpu_driver": "grafikdrivrutinskrascher"}


def measure(hw: dict, live: dict | None) -> dict:
    """The numbers a fix is expected to move."""
    sticks = (hw.get("memory") or {}).get("sticks") or []
    speeds = [s.get("configured_speed") for s in sticks if s.get("configured_speed")]
    vols = hw.get("volumes") or []
    c = next((v for v in vols if str(v.get("mount", "")).upper().startswith("C")), None)
    m = {"ram_speed": min(speeds) if speeds else None,
         "free_c_gb": c.get("free_gb") if c else None,
         "power_scheme": (hw.get("power_scheme") or {}).get("name")}
    for k in ("cpu.temp", "gpu.temp", "storage.temp_max"):
        m[k] = (live or {}).get(k)
    return m


def make_snapshot(hw: dict, findings: list[dict], live: dict | None) -> dict:
    return {"findings": [{k: f.get(k) for k in ("id", "severity", "title")} for f in findings],
            "metrics": measure(hw, live)}


def _rate(events: list[dict], cats, start: datetime, end: datetime) -> dict:
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    out = {}
    for cat in cats:
        n = 0
        for e in events:
            t = parse_date(e.get("t"))
            if e.get("cat") == cat and t and start <= t < end:
                n += 1
        out[cat] = {"count": n, "days": round(days, 1), "per_week": round(n / days * 7, 1)}
    return out


def compare(before: dict, findings_now: list[dict], metrics_now: dict, events: list[dict],
            now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    taken = datetime.fromtimestamp(before["ts"], tz=timezone.utc)
    ids_before = {f["id"]: f for f in before["findings"]}
    ids_now = {f["id"]: f for f in findings_now}

    # Event-based findings look back 30 days, so a fixed crash cause keeps "finding" old crashes.
    # Stability is therefore judged on crash rate before vs. after the snapshot instead.
    stability = []
    before_rate = _rate(events, STABILITY_CATS, taken - timedelta(days=30), taken)
    after_rate = _rate(events, STABILITY_CATS, taken, now)
    # Zero crashes in two hours proves nothing: wait a day before calling it better.
    too_early = (now - taken) < timedelta(days=MIN_DAYS_FOR_STABILITY)
    for cat in STABILITY_CATS:
        b, a = before_rate[cat], after_rate[cat]
        if b["count"] or a["count"]:
            if a["count"] and a["per_week"] >= b["per_week"]:
                verdict = "worse"
            elif too_early:
                verdict = "too_early"
            else:
                verdict = "better" if a["per_week"] < b["per_week"] else "same"
            stability.append({"cat": cat, "label": _CAT_LABEL[cat], "before": b, "after": a, "verdict": verdict})

    metrics = []
    for key, (label, unit, higher) in METRICS.items():
        b, a = before["metrics"].get(key), metrics_now.get(key)
        if b is None or a is None:
            continue
        diff = a - b
        # Idle temperatures wander a few degrees minute to minute; only call real changes.
        tol = 0.5 if unit == "GB" else 5.0 if unit == "°C" else 50
        verdict = "same" if abs(diff) < tol else ("better" if (diff > 0) == higher else "worse")
        metrics.append({"key": key, "label": label, "unit": unit, "before": b, "after": a, "verdict": verdict})
    if before["metrics"].get("power_scheme") != metrics_now.get("power_scheme") and metrics_now.get("power_scheme"):
        metrics.append({"key": "power_scheme", "label": "Energischema", "unit": "",
                        "before": before["metrics"].get("power_scheme"), "after": metrics_now["power_scheme"],
                        "verdict": "changed"})

    event_ids = {"hard_shutdowns", "bsod", "whea", "gpu_tdr", "disk_errors", "app_crashes", "low_memory"}
    return {
        "snapshot": {"id": before["id"], "ts": before["ts"], "label": before["label"]},
        "fixed": [f for i, f in ids_before.items() if i not in ids_now],
        "new": [f for i, f in ids_now.items() if i not in ids_before and f.get("severity") != "ok"],
        "remaining": [dict(f, history=i in event_ids) for i, f in ids_now.items()
                      if i in ids_before and f.get("severity") != "ok"],
        "metrics": metrics,
        "stability": stability,
        "hours_since": round((now - taken).total_seconds() / 3600, 1),
    }
