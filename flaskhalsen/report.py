"""Shareable report for forum help threads (BBCode for SweClockers, Markdown for Reddit/Discord)."""

from __future__ import annotations

from collections import Counter

REPO_URL = "https://github.com/ejleroth/flaskhalsen"
SEV_LABEL = {"kritisk": "KRITISK", "varning": "Varning", "info": "Info", "ok": "OK"}
CAT_LABEL = {"crash": "Oväntade avstängningar", "bsod": "Blåskärmar", "gpu_driver": "GPU-drivrutinskrascher",
             "whea": "Hårdvarufel (WHEA)", "disk": "Diskfel", "app_crash": "Programkrascher",
             "app_hang": "Programhängningar", "low_memory": "Slut på minne"}


class _Fmt:
    def __init__(self, kind: str):
        self.bb = kind == "bbcode"

    def b(self, s):
        return f"[b]{s}[/b]" if self.bb else f"**{s}**"

    def h(self, s):
        return f"[b]{s}[/b]" if self.bb else f"### {s}"

    def link(self, url, text):
        return f"[url={url}]{text}[/url]" if self.bb else f"[{text}]({url})"

    def items(self, rows):
        if not rows:
            return ""
        if self.bb:
            return "[list]\n" + "\n".join(f"[*]{r}" for r in rows) + "\n[/list]"
        return "\n".join(f"- {r}" for r in rows)

    def quote(self, s):
        return f"[quote]{s}[/quote]" if self.bb else "\n".join("> " + line for line in s.splitlines())


def _hw_rows(hw: dict) -> list[str]:
    g = hw.get("primary_gpu") or {}
    mem = hw.get("memory") or {}
    sticks = mem.get("sticks") or []
    s0 = sticks[0] if sticks else {}
    board = hw.get("board") or {}
    bios = hw.get("bios") or {}
    os_ = hw.get("os") or {}
    rows = [f"CPU: {(hw.get('cpu') or {}).get('name', '?')}",
            f"GPU: {g.get('name', '?')}" + (f" ({g['vram_gb']} GB, drivrutin {g.get('driver_version', '?')})" if g.get("vram_gb") else ""),
            f"RAM: {mem.get('total_gb', '?')} GB {s0.get('type', '')}, {len(sticks)} st, {s0.get('configured_speed', '?')} MT/s"
            + (f" (märkt {s0['rated_speed_guess']})" if s0.get("rated_speed_guess") else "")
            + (f", {s0.get('part_number')}" if s0.get("part_number") else ""),
            f"Moderkort: {board.get('manufacturer', '')} {board.get('model', '')}, BIOS {bios.get('version', '?')} ({bios.get('date', '?')})".strip(),
            f"Windows: {os_.get('name', '?')} build {os_.get('build', '?')}"]
    disks = hw.get("disks") or []
    if disks:
        rows.append("Lagring: " + ", ".join(f"{d.get('name')} ({d.get('size_gb')} GB)" for d in disks))
    return rows


def build(hw: dict, findings: list[dict], events: list[dict], ai_summary: str | None = None,
          kind: str = "bbcode", version: str = "") -> str:
    f = _Fmt(kind)
    parts = [f.h("Min dator")]
    parts.append(f.items(_hw_rows(hw)))

    problems = [x for x in findings if x.get("severity") != "ok"]
    parts.append(f.h(f"Hittade problem ({len(problems)})"))
    if problems:
        parts.append(f.items([f"{f.b(SEV_LABEL.get(x['severity'], x['severity']) + ':')} {x['title']}. {x.get('detail', '')}"
                              for x in problems]))
    else:
        parts.append("Inga problem hittades.")

    counts = Counter(e.get("cat") for e in events)
    if counts:
        parts.append(f.h("Windows-händelser senaste 30 dagarna"))
        parts.append(f.items([f"{CAT_LABEL.get(c, c)}: {n}" for c, n in counts.most_common()]))
        stop = [e for e in events if e.get("cat") == "bsod"][:3]
        if stop:
            parts.append("Senaste blåskärmar: " + "; ".join(e.get("text", "")[:120] for e in stop))

    if ai_summary:
        parts.append(f.h("AI-sammanfattning (lokal modell)"))
        parts.append(f.quote(ai_summary.strip()))

    parts.append(f"Rapport från {f.link(REPO_URL, 'Flaskhalsen')} {version}".strip()
                 + " (lokal AI-diagnos, inga personuppgifter eller serienummer ingår).")
    return "\n\n".join(p for p in parts if p)
