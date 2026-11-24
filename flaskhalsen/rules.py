"""Deterministic diagnosis rules.

The LLM never judges raw numbers on its own - it explains what these rules found.
Each rule returns zero or more Findings with the evidence that triggered it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from .events import BUGCHECKS, app_name, bugcheck_code

SEVERITY_ORDER = {"kritisk": 0, "varning": 1, "info": 2, "ok": 3}

# JEDEC base speeds: running at one of these while the kit is rated higher = XMP/EXPO off.
JEDEC = {"DDR4": {2133, 2400, 2666, 2933, 3200}, "DDR5": {4000, 4400, 4800, 5200, 5600}}


@dataclass
class Finding:
    id: str
    severity: str  # kritisk | varning | info | ok
    category: str  # minne | gpu | cpu | stabilitet | lagring | system | flaskhals
    title: str
    detail: str
    fix: str = ""
    evidence: dict = field(default_factory=dict)


# --- static rules (hardware info + event log) --------------------------------------

def rule_ram(hw: dict) -> list[Finding]:
    out = []
    sticks = (hw.get("memory") or {}).get("sticks") or []
    if not sticks:
        return out
    s0 = sticks[0]
    rated = max((s.get("rated_speed_guess") or 0) for s in sticks)
    running = min((s.get("configured_speed") or 0) for s in sticks)
    mtype = s0.get("type")
    if rated and running and rated > running + 100 and running in JEDEC.get(mtype, {running}):
        out.append(Finding(
            "ram_xmp_off", "varning", "minne", "RAM går inte i sin märkta hastighet",
            f"Minnet körs i {running} MT/s men kitet ({s0.get('part_number')}) är märkt för {rated} MT/s. "
            "XMP/DOCP/EXPO verkar vara avstängt i BIOS.",
            "Starta om till BIOS och aktivera XMP (Intel), DOCP (ASUS AMD) eller EXPO (DDR5 AMD). "
            "Det ger ofta 5-15 % högre FPS i CPU-tunga spel.",
            {"running_mts": running, "rated_mts": rated, "part_number": s0.get("part_number")}))
    if len(sticks) == 1:
        out.append(Finding(
            "ram_single_channel", "varning", "minne", "Bara en RAM-sticka (single channel)",
            f"Endast en sticka på {s0.get('size_gb')} GB hittades. Single channel halverar minnesbandbredden.",
            "Använd två likadana stickor i de slotar moderkortsmanualen anger (oftast A2 + B2).",
            {"sticks": 1}))
    parts = {s.get("part_number") for s in sticks}
    sizes = {s.get("size_gb") for s in sticks}
    if len(parts) > 1 or len(sizes) > 1:
        out.append(Finding(
            "ram_mixed", "info", "minne", "Blandade RAM-kit",
            f"Olika stickor hittades ({', '.join(sorted(p or '?' for p in parts))}). "
            "Blandade kit kan vara instabila med XMP.",
            "Om du har krascher: testa med bara ett matchat kit.",
            {"part_numbers": sorted(p or "?" for p in parts), "sizes_gb": sorted(sizes)}))
    if len(sticks) == 4 and rated and mtype == "DDR5":
        out.append(Finding(
            "ram_4x_ddr5", "info", "minne", "Fyra DDR5-stickor",
            "Fyra DDR5-stickor är svårt för minneskontrollern och klarar ofta inte märkt hastighet.",
            "Vid instabilitet: sänk hastigheten ett steg eller kör två stickor.", {"sticks": 4}))
    return out


def rule_power_plan(hw: dict) -> list[Finding]:
    ps = hw.get("power_scheme") or {}
    if ps.get("guid") == "a1841308-3541-4fab-bc81-f71556f20b4a":
        return [Finding("power_saver", "varning", "system", "Energisparläge är aktivt",
                        "Windows energischema är Energisparläge, vilket kan strypa CPU:n.",
                        "Byt till Balanserad eller Hög prestanda under Inställningar > System > Ström.",
                        {"scheme": ps.get("name")})]
    return []


def rule_disk_space(hw: dict) -> list[Finding]:
    out = []
    for v in hw.get("volumes") or []:
        if v["total_gb"] >= 20 and (v["free_pct"] < 10 or v["free_gb"] < 15):
            sev = "varning" if v["mount"].upper().startswith("C") else "info"
            out.append(Finding(f"disk_full_{v['mount'][0]}", sev, "lagring",
                               f"Lite ledigt utrymme på {v['mount']}",
                               f"{v['free_gb']} GB ({v['free_pct']} %) ledigt. Full disk ger sämre SSD-prestanda "
                               "och kan stoppa Windows-uppdateringar och spelpatchar.",
                               "Rensa med Diskrensning / Lagringsinställningar, eller flytta spel.", v))
    for d in hw.get("disks") or []:
        if d.get("health") and d["health"] not in ("Healthy", "0", "Felfri"):
            out.append(Finding(f"disk_health_{d['name']}", "kritisk", "lagring",
                               f"Disken {d['name']} rapporterar dålig hälsa",
                               f"Windows anger hälsostatus '{d['health']}'.",
                               "Säkerhetskopiera nu och kontrollera SMART med CrystalDiskInfo.", d))
    return out


def rule_drivers_bios(hw: dict, today: date | None = None) -> list[Finding]:
    today = today or date.today()
    out = []
    gpu = hw.get("primary_gpu") or {}
    if gpu.get("driver_date"):
        age = (today - date.fromisoformat(gpu["driver_date"])).days
        if age > 365:
            out.append(Finding("gpu_driver_old", "info", "gpu", "Gammal grafikdrivrutin",
                               f"Drivrutinen för {gpu['name']} är från {gpu['driver_date']} ({age // 30} månader).",
                               "Uppdatera från tillverkarens sida (NVIDIA App / AMD Adrenalin / Intel).",
                               {"driver": gpu.get("driver_version"), "date": gpu["driver_date"]}))
    bios = hw.get("bios") or {}
    if bios.get("date"):
        age = (today - date.fromisoformat(bios["date"])).days
        if age > 730:
            out.append(Finding("bios_old", "info", "system", "Gammalt BIOS",
                               f"BIOS {bios.get('version')} är från {bios['date']}. Nyare BIOS kan förbättra "
                               "minneskompatibilitet och stabilitet.",
                               "Kolla moderkortstillverkarens supportsida. Uppdatera bara om du har problem.",
                               bios))
    pf = hw.get("pagefile")
    ram = (hw.get("memory") or {}).get("total_gb") or 0
    if pf is not None and not pf.get("auto") and not pf.get("files") and ram <= 32:
        out.append(Finding("pagefile_off", "varning", "system", "Växlingsfilen är avstängd",
                           "Utan växlingsfil kan spel krascha när RAM tar slut.",
                           "Slå på 'Hantera växlingsfilens storlek automatiskt' i Systemegenskaper.", pf))
    return out


def _within(events, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in events:
        try:
            t = datetime.fromisoformat(e["t"])
        except (KeyError, ValueError):
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if t >= cutoff:
            out.append(e)
    return out


def rule_events(events: list[dict], days: int = 14) -> list[Finding]:
    out = []
    ev = _within(events, days)
    by_cat: dict[str, list] = {}
    for e in ev:
        by_cat.setdefault(e.get("cat"), []).append(e)

    whea = by_cat.get("whea", [])
    if whea:
        ids = Counter(e["id"] for e in whea)
        fatal = ids.get(18, 0) + ids.get(20, 0)
        out.append(Finding("whea", "kritisk" if fatal or len(whea) >= 5 else "varning", "stabilitet",
                           f"{len(whea)} WHEA-hårdvarufel senaste {days} dagarna",
                           "WHEA-fel betyder att hårdvaran själv rapporterat fel. "
                           + ("Fatala CPU-fel (id 18) orsakar krascher. " if fatal else "")
                           + ("Id 19 brukar betyda instabil överklock/undervolt. " if 19 in ids else "")
                           + ("Id 17 är PCIe-fel - kolla grafikkort, NVMe och riser-kablar. " if 17 in ids else ""),
                           "Återställ överklockning/Curve Optimizer/PBO till standard och testa igen.",
                           {"count_by_id": dict(ids)}))

    bsods = [e for e in by_cat.get("bsod", [])]
    k41 = [e for e in by_cat.get("crash", []) if e["id"] == 41]
    codes = Counter(c for c in (bugcheck_code(e) for e in bsods + k41) if c)
    if codes:
        named = [f"{BUGCHECKS.get(c, (hex(c),))[0]} ×{n}" for c, n in codes.most_common()]
        top = codes.most_common(1)[0][0]
        out.append(Finding("bsod", "kritisk", "stabilitet", f"{sum(codes.values())} blåskärmar senaste {days} dagarna",
                           f"Stoppkoder: {', '.join(named)}. Vanligast: {BUGCHECKS.get(top, ('', 'okänd'))[1]}",
                           "Se stoppkodens förklaring. Börja med att stänga av överklockning/XMP och testa.",
                           {"stop_codes": {hex(c): n for c, n in codes.items()}}))
    silent = [e for e in k41 if not bugcheck_code(e)]
    if silent:
        died = [e["died_at"] for e in silent if e.get("died_at")]
        hours = [int(d[-5:-3]) for d in died if d[-5:-3].isdigit()]
        night = sum(1 for h in hours if h >= 23 or h < 7)
        pattern = ""
        if hours and night / len(hours) >= 0.5:
            pattern = (f" {night} av {len(hours)} skedde mellan 23 och 07, när datorn troligen stod i vila - "
                       "det pekar mot vila/viloläge snarare än last.")
        elif hours:
            pattern = f" Tidpunkter då datorn dog: {', '.join(died[:6])}."
        out.append(Finding("hard_shutdowns", "kritisk" if len(silent) >= 3 else "varning", "stabilitet",
                           f"{len(silent)} oväntade avstängningar utan blåskärm",
                           "Kernel-Power 41 med stoppkod 0 betyder att datorn dog utan att hinna skriva en "
                           "blåskärm: strömavbrott, nätaggregat som slår ifrån, frysning som krävde reset, "
                           "eller att du höll in strömknappen." + pattern,
                           "Om det sker under spel: misstänk nätaggregatet (transienta toppar från grafikkortet) "
                           "eller instabil överklock. Om det sker i vila: stäng av snabbstart och testa att "
                           "stänga av djupa C-states/ErP i BIOS.",
                           {"count": len(silent), "died_at": died[:10],
                            "restarted": [e["t"][:16] for e in silent[:5]]}))

    tdr = by_cat.get("gpu_driver", [])
    if tdr:
        out.append(Finding("gpu_tdr", "varning" if len(tdr) < 3 else "kritisk", "gpu",
                           f"Grafikdrivrutinen har kraschat {len(tdr)} gånger",
                           "Drivrutinen slutade svara och återställdes (TDR). Vanliga orsaker: GPU-överklock, "
                           "för lite ström, trasig drivrutinsinstallation eller överhettning.",
                           "Ta bort GPU-överklock, avinstallera drivrutinen med DDU och installera om, "
                           "kontrollera att PCIe-strömkablarna är separata kablar (inte en daisy-chain).",
                           {"count": len(tdr)}))

    disk = by_cat.get("disk", [])
    if disk:
        out.append(Finding("disk_errors", "kritisk" if len(disk) >= 3 else "varning", "lagring",
                           f"{len(disk)} diskfel i händelseloggen",
                           "Windows har loggat fel från disk/filsystem.",
                           "Säkerhetskopiera, kör CrystalDiskInfo och kontrollera kablar/M.2-montering.",
                           {"count": len(disk), "providers": dict(Counter(e["provider"] for e in disk))}))

    apps = Counter(app_name(e) for e in by_cat.get("app_crash", []))
    frequent = {a: n for a, n in apps.items() if n >= 3}
    if frequent:
        out.append(Finding("app_crashes", "info", "stabilitet", "Program som kraschar ofta",
                           ", ".join(f"{a} ({n} ggr)" for a, n in sorted(frequent.items(), key=lambda x: -x[1])),
                           "Verifiera spelfilerna i Steam/launchern. Kraschar flera olika spel: misstänk "
                           "drivrutin, RAM eller överklock.", {"apps": frequent}))
    if by_cat.get("low_memory"):
        out.append(Finding("low_memory", "varning", "minne", "Windows har fått slut på minne",
                           f"{len(by_cat['low_memory'])} varningar om att RAM + växlingsfil tog slut.",
                           "Stäng program i bakgrunden, kontrollera växlingsfilen eller uppgradera RAM.",
                           {"count": len(by_cat["low_memory"])}))
    return out


# --- dynamic rules (recorded sensor samples) ---------------------------------------

def _series(samples, key):
    return [s[key] for s in samples if s.get(key) is not None]


def _frac(samples, pred) -> float:
    return sum(1 for s in samples if pred(s)) / len(samples) if samples else 0.0


def _load(s):
    return (s.get("gpu.load") or 0) >= 50 or (s.get("cpu.load") or 0) >= 50


NVME_WARN_C = 75  # most NVMe drives start throttling somewhere between ~75 and 85 °C
NVME_CRIT_C = 85


def rule_storage_temp(samples: list[dict]) -> list[Finding]:
    """Hot NVMe drive. Works on a single live sample too (no recording needed)."""
    t = _series(samples, "storage.temp_max")
    if not t or max(t) < NVME_WARN_C:
        return []
    peak = max(t)
    crit = peak >= NVME_CRIT_C
    return [Finding("nvme_hot", "kritisk" if crit else "varning", "lagring",
                    "NVMe-disken är för varm" if crit else "NVMe-disken är varm",
                    f"Disktemperaturen nådde {peak:.0f} °C. De flesta NVMe-diskar stryper sin hastighet runt "
                    f"75-85 °C, vilket ger långsamma laddningar och hack när spel läser in data.",
                    "Montera moderkortets M.2-kylfläns (eller köp en), se till att den sitter med skyddsplasten "
                    "borttagen, och att grafikkortet inte blåser varm luft rakt på disken. Ökat luftflöde i chassit hjälper.",
                    {"max_temp": peak})]


def rule_samples(samples: list[dict]) -> list[Finding]:
    out = []
    if len(samples) < 5:
        return out
    loaded = [s for s in samples if _load(s)] or samples

    # GPU thermal throttling
    thr = _frac(loaded, lambda s: s.get("gpu.thr_thermal") == 1)
    gtemp = _series(loaded, "gpu.temp")
    if thr > 0.05 or (gtemp and max(gtemp) >= 87):
        out.append(Finding("gpu_thermal", "varning", "gpu", "Grafikkortet stryps av värme",
                           f"Termisk strypning aktiv {thr:.0%} av tiden under last. Max {max(gtemp, default=0):.0f} °C.",
                           "Rensa damm, öka fläktkurvan, förbättra luftflödet i chassit. Vid hög hotspot: "
                           "byt kylpasta/termokuddar.", {"throttle_fraction": round(thr, 2),
                                                          "max_temp": max(gtemp, default=None)}))

    hot = [(s["gpu.hotspot"] - s["gpu.temp"]) for s in loaded
           if s.get("gpu.hotspot") is not None and s.get("gpu.temp") is not None]
    if hot and max(hot) >= 25:
        out.append(Finding("gpu_hotspot_delta", "varning", "gpu", "Stor skillnad mellan GPU-kärna och hotspot",
                           f"Hotspot är upp till {max(hot):.0f} °C varmare än kärnan. Över ~20-25 °C tyder på "
                           "dålig kontakt mellan kylare och kärna.",
                           "Kylpastan kan ha torkat eller pumpat ut. Repasta eller kontakta garantin.",
                           {"max_delta": round(max(hot), 1)}))

    pw = _frac(loaded, lambda s: s.get("gpu.thr_power") == 1)
    if pw > 0.5:
        out.append(Finding("gpu_power_limit", "info", "gpu", "Grafikkortet slår i effektgränsen",
                           f"Kortet begränsas av sin effektgräns {pw:.0%} av tiden under last. Det är normalt "
                           "för moderna kort och inget fel.",
                           "Vill du ha mer prestanda per watt: prova en undervolt (MSI Afterburner).",
                           {"power_limit_fraction": round(pw, 2)}))

    if _frac(loaded, lambda s: s.get("gpu.thr_hw") == 1) > 0.02:
        out.append(Finding("gpu_hw_slowdown", "kritisk", "gpu", "Grafikkortet gör nödstrypning (HW slowdown)",
                           "Hårdvarustrypning triggas av för hög temperatur eller att strömförsörjningen inte räcker.",
                           "Kontrollera PCIe-strömkablarna (12VHPWR helt intryckt!) och nätaggregatets kapacitet.",
                           {}))

    # PCIe link: only judge under GPU load (it downclocks at idle to save power)
    gpu_busy = [s for s in samples if (s.get("gpu.load") or 0) >= 60 and s.get("gpu.pcie_width")]
    if len(gpu_busy) >= 3:
        w = min(s["gpu.pcie_width"] for s in gpu_busy)
        wmax = max(s.get("gpu.pcie_width_max") or 0 for s in gpu_busy)
        g = min(s.get("gpu.pcie_gen") or 0 for s in gpu_busy)
        gmax = max(s.get("gpu.pcie_gen_max") or 0 for s in gpu_busy)
        if wmax and w < wmax:
            out.append(Finding("pcie_width", "varning", "gpu", f"Grafikkortet kör PCIe x{w:.0f} i stället för x{wmax:.0f}",
                               "Kortet sitter troligen i fel slot, eller så delar slotten banor med en M.2-disk.",
                               "Sätt kortet i översta x16-slotten och kolla moderkortsmanualen om M.2-delning.",
                               {"width": w, "width_max": wmax}))
        elif gmax and g and g < gmax:
            out.append(Finding("pcie_gen", "info", "gpu", f"PCIe gen {g:.0f} i stället för gen {gmax:.0f} under last",
                               "Länken går i lägre generation än kortet stödjer. Kan bero på BIOS-inställning, "
                               "riser-kabel eller att moderkortet/CPU:n inte stödjer högre.",
                               "Kontrollera PCIe-inställningen i BIOS (Auto) och eventuell riser-kabel.",
                               {"gen": g, "gen_max": gmax}))

    # VRAM / RAM pressure
    vram = [s["gpu.mem_used"] / s["gpu.mem_total"] for s in samples
            if s.get("gpu.mem_used") and s.get("gpu.mem_total")]
    if vram and max(vram) >= 0.95:
        out.append(Finding("vram_full", "varning", "flaskhals", "Grafikminnet (VRAM) är fullt",
                           f"VRAM-användningen nådde {max(vram):.0%}. När VRAM tar slut flyttas data till RAM, "
                           "vilket ger hack och stutter.",
                           "Sänk texturkvalitet eller upplösning ett steg.", {"max_fraction": round(max(vram), 2)}))
    ram = _series(samples, "ram.used_pct")
    if ram and max(ram) >= 90:
        out.append(Finding("ram_full", "varning", "flaskhals", "RAM-minnet är nästan fullt",
                           f"RAM-användningen nådde {max(ram):.0f} %.",
                           "Stäng webbläsare och program i bakgrunden, eller uppgradera RAM.", {"max_pct": max(ram)}))

    # CPU temperature
    ct = _series(samples, "cpu.temp")
    if ct and max(ct) >= 95:
        out.append(Finding("cpu_hot", "varning", "cpu", "Processorn är mycket varm",
                           f"CPU-temperaturen nådde {max(ct):.0f} °C. Många CPU:er stryper runt 95-100 °C.",
                           "Kontrollera att kylaren sitter rätt, att fläktar/pump snurrar och att kylpastan är OK.",
                           {"max_temp": max(ct)}))

    out += rule_storage_temp(samples)

    # The namesake: what is the bottleneck?
    game = [s for s in samples if (s.get("gpu.load") is not None) and _load(s)]
    if len(game) >= 5:
        gpu_bound = _frac(game, lambda s: s["gpu.load"] >= 95)
        cpu_bound = _frac(game, lambda s: s["gpu.load"] < 80 and (s.get("cpu.core_load_max") or 0) >= 90)
        avg_gpu = sum(s["gpu.load"] for s in game) / len(game)
        if cpu_bound >= 0.3:
            out.append(Finding("bottleneck_cpu", "info", "flaskhals", "Flaskhalsen är processorn",
                               f"GPU-lasten låg i snitt på {avg_gpu:.0f} % medan minst en CPU-kärna gick på max "
                               f"{cpu_bound:.0%} av tiden. Grafikkortet väntar på processorn.",
                               "Höj grafikinställningarna (gratis kvalitet), begränsa FPS, aktivera XMP eller "
                               "överväg en snabbare CPU.", {"cpu_bound_fraction": round(cpu_bound, 2),
                                                             "avg_gpu_load": round(avg_gpu)}))
        elif gpu_bound >= 0.6:
            out.append(Finding("bottleneck_gpu", "ok", "flaskhals", "Flaskhalsen är grafikkortet (det är bra)",
                               f"GPU-lasten var ≥95 % {gpu_bound:.0%} av tiden. Grafikkortet jobbar för fullt, "
                               "vilket är precis vad man vill i spel.",
                               "Vill du ha mer FPS: sänk inställningar, använd DLSS/FSR eller uppgradera GPU.",
                               {"gpu_bound_fraction": round(gpu_bound, 2), "avg_gpu_load": round(avg_gpu)}))
    return out


def diagnose(hw: dict, events: list[dict], samples: list[dict] | None = None) -> list[dict]:
    findings: list[Finding] = []
    findings += rule_ram(hw)
    findings += rule_power_plan(hw)
    findings += rule_disk_space(hw)
    findings += rule_drivers_bios(hw)
    findings += rule_events(events)
    if samples:
        findings += rule_samples(samples)
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.severity, 9))
    return [asdict(f) for f in findings]
