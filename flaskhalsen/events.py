"""Windows event log: crashes, BSODs, GPU driver resets, WHEA hardware errors, disk errors."""

from __future__ import annotations

import re
from datetime import datetime

from .util import as_list, run_powershell_json

_SPECS = [
    # (log, provider, ids or None, category)
    ("System", "Microsoft-Windows-Kernel-Power", [41], "crash"),
    ("System", "EventLog", [6008], "crash"),
    ("System", "Microsoft-Windows-WER-SystemErrorReporting", [1001], "bsod"),
    ("System", "Display", [4101], "gpu_driver"),
    ("System", "nvlddmkm", None, "gpu_driver"),
    ("System", "amdkmdag", None, "gpu_driver"),
    ("System", "Microsoft-Windows-WHEA-Logger", None, "whea"),
    ("System", "disk", [7, 51, 153], "disk"),
    ("System", "Ntfs", [55, 98], "disk"),
    ("System", "stornvme", None, "disk"),
    ("System", "Microsoft-Windows-Resource-Exhaustion-Detector", [2004], "low_memory"),
    ("Application", "Application Error", [1000], "app_crash"),
    ("Application", "Application Hang", [1002], "app_hang"),
]

_SCRIPT = r"""
$since = (Get-Date).AddDays(-__DAYS__)
$specs = @(__SPECS__)
$out = foreach ($s in $specs) {
  $f = @{ LogName = $s[0]; ProviderName = $s[1]; StartTime = $since }
  if ($s[2]) { $f.Id = $s[2] }
  try {
    Get-WinEvent -FilterHashtable $f -MaxEvents 150 -ErrorAction Stop | ForEach-Object {
      $e = $_
      $msg = ''
      if ($e.Message) { $msg = $e.Message.Substring(0, [Math]::Min(280, $e.Message.Length)) }
      $props = @($e.Properties | Select-Object -First 8 | ForEach-Object { "$($_.Value)" })
      [pscustomobject]@{ t = $e.TimeCreated.ToString('o'); id = $e.Id; provider = $e.ProviderName; cat = $s[3]; level = $e.Level; props = $props; msg = $msg }
    }
  } catch {}
}
ConvertTo-Json -InputObject @($out) -Depth 4 -Compress
"""

# Common stop codes, explained for humans.
BUGCHECKS = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL", "Ofta en drivrutin eller instabilt RAM/överklockning."),
    0x1A: ("MEMORY_MANAGEMENT", "Pekar starkt på RAM: instabil XMP/EXPO eller trasig sticka."),
    0x3B: ("SYSTEM_SERVICE_EXCEPTION", "Oftast en drivrutin (grafik, antivirus, anti-cheat)."),
    0x50: ("PAGE_FAULT_IN_NONPAGED_AREA", "Drivrutin eller RAM."),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "Drivrutin som kraschat."),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", "Drivrutin som strular vid vila/strömsparläge."),
    0xD1: ("DRIVER_IRQL_NOT_LESS_OR_EQUAL", "Drivrutin - ofta nätverk eller grafik."),
    0xEF: ("CRITICAL_PROCESS_DIED", "Korrupta systemfiler eller disk-problem."),
    0x101: ("CLOCK_WATCHDOG_TIMEOUT", "CPU-instabilitet: överklockning/undervolt (t.ex. Curve Optimizer)."),
    0x116: ("VIDEO_TDR_FAILURE", "Grafikkortet slutade svara: drivrutin, överklock eller strömförsörjning."),
    0x117: ("VIDEO_TDR_TIMEOUT_DETECTED", "Grafikkortet slutade svara."),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "Hårdvarufel: oftast CPU-spänning/överklock, ibland RAM eller PSU."),
    0x133: ("DPC_WATCHDOG_VIOLATION", "Drivrutin eller SSD-firmware."),
    0x139: ("KERNEL_SECURITY_CHECK_FAILURE", "Drivrutin eller RAM."),
    0x154: ("UNEXPECTED_STORE_EXCEPTION", "Disk/SSD-problem."),
}

WHEA_IDS = {
    1: "Allmänt hårdvarufel",
    17: "Korrigerat PCIe-fel (ofta grafikkort/NVMe/riser-kabel)",
    18: "Fatalt hårdvarufel (CPU) - orsakade troligen en krasch",
    19: "Korrigerat CPU-fel (ofta instabil överklock/undervolt)",
    20: "Fatalt hårdvarufel",
    47: "Korrigerat minnesfel (RAM)",
}

_HEX = re.compile(r"0x([0-9a-fA-F]+)")


def bugcheck_code(event: dict) -> int | None:
    """Extract the stop code from a BugCheck 1001 or Kernel-Power 41 event."""
    props = event.get("props") or []
    if event.get("id") == 41 and props:
        try:
            return int(props[0]) or None
        except ValueError:
            return None
    for text in list(props[:1]) + [event.get("msg") or ""]:
        m = _HEX.search(str(text))
        if m:
            code = int(m.group(1), 16)
            if code:
                return code
    return None


def describe(event: dict) -> str:
    cat = event.get("cat")
    eid = event.get("id")
    if cat == "bsod" or (cat == "crash" and eid == 41):
        code = bugcheck_code(event)
        if code:
            name, why = BUGCHECKS.get(code, (f"0x{code:X}", "Okänd stoppkod."))
            return f"Blåskärm {name} (0x{code:X}): {why}"
        if eid == 41:
            return "Datorn stängdes av oväntat (strömavbrott, frysning eller hård omstart)."
    if cat == "crash" and eid == 6008:
        return "Föregående avstängning var oväntad."
    if cat == "whea":
        return f"WHEA {eid}: {WHEA_IDS.get(eid, 'Hårdvarufel')}"
    if cat == "gpu_driver":
        return "Grafikdrivrutinen slutade svara och återställdes (TDR)."
    if cat == "disk":
        return f"Diskfel från {event.get('provider')} (id {eid})."
    if cat == "low_memory":
        return "Windows fick slut på minne (RAM + växlingsfil)."
    if cat == "app_crash":
        return f"Programkrasch: {app_name(event)}"
    if cat == "app_hang":
        return f"Programmet hängde sig: {app_name(event)}"
    return event.get("msg") or ""


def app_name(event: dict) -> str:
    props = event.get("props") or []
    return props[0] if props else "okänt program"


def collect(days: int = 14) -> list[dict]:
    specs = ",".join(
        "@('{}','{}',{},'{}')".format(log, prov, "@(" + ",".join(map(str, ids)) + ")" if ids else "$null", cat)
        for log, prov, ids, cat in _SPECS
    )
    script = _SCRIPT.replace("__DAYS__", str(int(days))).replace("__SPECS__", specs)
    events = as_list(run_powershell_json(script, timeout=60))
    for e in events:
        e["props"] = as_list(e.get("props"))
        e["text"] = describe(e)
    events = drop_duplicate_shutdowns(events)
    events.sort(key=lambda e: e.get("t") or "", reverse=True)
    return events


def drop_duplicate_shutdowns(events: list[dict]) -> list[dict]:
    """EventLog 6008 is logged at boot right next to Kernel-Power 41 for the same incident.

    Keep 6008 only when there is no 41 within 5 minutes of it.
    """
    def ts(e):
        try:
            return datetime.fromisoformat(e["t"]).timestamp()
        except (KeyError, ValueError):
            return None

    k41 = [(ts(e), e) for e in events if e.get("id") == 41 and e.get("cat") == "crash"]
    k41 = [(t, e) for t, e in k41 if t]
    out = []
    for e in events:
        if e.get("id") == 6008 and e.get("cat") == "crash":
            t = ts(e)
            twin = next((k for x, k in k41 if t and abs(t - x) < 300), None)
            if twin is not None:
                died = died_at(e)
                if died:  # 6008 knows when the PC actually died; 41 only knows when it came back
                    twin["died_at"] = died
                    twin["text"] += f" Datorn dog {died}."
                continue
        out.append(e)
    return out


def died_at(event: dict) -> str | None:
    """EventLog 6008 props are [time, date] of the unexpected shutdown, with invisible LTR marks."""
    props = [str(p).replace("‎", "").replace("‏", "").strip() for p in event.get("props") or []]
    if len(props) >= 2 and props[0] and props[1]:
        return f"{props[1]} {props[0][:5]}"
    return None
