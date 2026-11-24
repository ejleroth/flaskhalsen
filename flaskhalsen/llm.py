"""The AI part: a local model that explains findings and answers questions via tools."""

from __future__ import annotations

import json
from typing import Callable

from . import ollama, store

SYSTEM_PROMPT = """Du är Flaskhalsen, en lugn och kunnig svensk PC-tekniker som körs helt lokalt på användarens dator.

Regler:
- Svara alltid på svenska, kort och konkret. Använd punktlistor för åtgärder.
- Basera ALLT på data från dina verktyg. Hitta aldrig på värden, modeller eller händelser.
  Saknas data: säg det rakt ut och föreslå hur man får fram det (t.ex. "gör en inspelning medan du spelar").
- Regelmotorns fynd (get_findings) är fakta du ska förklara - motsäg dem inte.
- Ordna åtgärder från gratis/enkelt till dyrt. Föreslå aldrig att köpa nytt om en inställning kan lösa det.
- Förklara facktermer kort första gången (t.ex. XMP, TDR, WHEA).
- Var ärlig om osäkerhet.
- Ställ inte motfrågor om sådant du kan ta reda på själv. Du har redan en sammanfattning av datorn
  och regelmotorns fynd nedan; använd verktygen när du behöver detaljer (händelser, sensorer)."""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_system_info",
        "description": "Datorns hårdvara: CPU, GPU, RAM (stickor, hastighet), moderkort, BIOS, diskar, energischema.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_findings",
        "description": "Regelmotorns diagnos: lista med fynd (allvarlighet, förklaring, åtgärd, bevis).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_recent_events",
        "description": "Händelser från Windows-loggen: krascher, blåskärmar, drivrutinskrascher, hårdvarufel, diskfel, programkrascher.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "enum": ["all", "crash", "bsod", "gpu_driver", "whea", "disk", "app_crash", "app_hang", "low_memory"]},
            "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "get_sensor_summary",
        "description": "Sammanfattning (min/snitt/max) av senaste sensorinspelningen: temperaturer, last, klockor, effekt, strypning.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_live_sensors",
        "description": "Sensorvärden just nu (ett ögonblick).",
        "parameters": {"type": "object", "properties": {}}}},
]


def summarize_samples(samples: list[dict]) -> dict:
    if not samples:
        return {"error": "Ingen inspelning finns. Be användaren starta en inspelning medan problemet uppstår."}
    keys = sorted({k for s in samples for k in s if k != "ts"})
    out = {"samples": len(samples), "seconds": round(samples[-1]["ts"] - samples[0]["ts"])}
    for k in keys:
        vals = [s[k] for s in samples if isinstance(s.get(k), (int, float))]
        if vals:
            out[k] = {"min": round(min(vals), 1), "avg": round(sum(vals) / len(vals), 1), "max": round(max(vals), 1)}
    return out


def _record(model: str, resp: dict, purpose: str) -> dict:
    p = resp.get("prompt_eval_count") or 0
    e = resp.get("eval_count") or 0
    secs = (resp.get("eval_duration") or 0) / 1e9
    store.add_tokens(model, p, e, secs, purpose)
    return {"prompt_tokens": p, "eval_tokens": e, "tokens_per_second": round(e / secs, 1) if secs else None}


def run_chat(model: str, history: list[dict], tool_impl: dict[str, Callable[[dict], object]],
             context: dict | None = None, purpose: str = "chat", max_rounds: int = 6) -> dict:
    """Run a tool-calling loop. Returns {answer, tool_calls, usage}.

    `context` (computer summary + findings) goes straight into the system prompt: an 8B model
    often answers with follow-up questions instead of calling tools, so give it the basics up front.
    """
    system = SYSTEM_PROMPT
    if context:
        system += "\n\nKÄND DATA OM DATORN:\n" + json.dumps(context, ensure_ascii=False, default=str)
    messages = [{"role": "system", "content": system}] + history
    calls_made = []
    usage = {"prompt_tokens": 0, "eval_tokens": 0, "tokens_per_second": None}
    for _ in range(max_rounds):
        resp = ollama.chat(model, messages, tools=TOOLS)
        u = _record(model, resp, purpose)
        usage["prompt_tokens"] += u["prompt_tokens"]
        usage["eval_tokens"] += u["eval_tokens"]
        usage["tokens_per_second"] = u["tokens_per_second"] or usage["tokens_per_second"]
        msg = resp.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"answer": (msg.get("content") or "").strip(), "tool_calls": calls_made, "usage": usage}
        messages.append(msg)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            impl = tool_impl.get(name)
            result = impl(args) if impl else {"error": f"okänt verktyg {name}"}
            calls_made.append(name)
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:5000]})
    return {"answer": "Jag fastnade i för många verktygsanrop. Försök ställa frågan mer specifikt.",
            "tool_calls": calls_made, "usage": usage}


def explain_findings(model: str, findings: list[dict], hw_brief: dict) -> dict:
    """One-shot: turn the rule findings into a short prioritized Swedish summary."""
    prompt = (
        "Här är datorn och regelmotorns fynd. Skriv en kort diagnos (max ~200 ord):\n"
        "1. En mening om helhetsläget.\n2. De viktigaste problemen i prioritetsordning, med varför de spelar roll.\n"
        "3. En numrerad åtgärdslista, enklast först.\n\n"
        f"DATOR:\n{json.dumps(hw_brief, ensure_ascii=False)}\n\nFYND:\n{json.dumps(findings, ensure_ascii=False)}"
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    resp = ollama.chat(model, messages)
    usage = _record(model, resp, "explain")
    return {"answer": ((resp.get("message") or {}).get("content") or "").strip(), "usage": usage}


def benchmark(model: str) -> dict:
    messages = [{"role": "user", "content": "Skriv tre meningar om varför man ska damma av sin dator."}]
    resp = ollama.chat(model, messages)
    return _record(model, resp, "benchmark")
