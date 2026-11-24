"""FastAPI app: serves the UI and the JSON API."""

from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, events, hardware, llm, models, ollama, rules, sensors, store

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Flaskhalsen", version=__version__)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def cached(key: str, ttl: float, fn, refresh: bool = False):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def get_hw(refresh=False):
    return cached("hw", 600, hardware.detect, refresh)


def get_events(refresh=False):
    return cached("events", 120, lambda: events.collect(30), refresh)


def latest_session_samples() -> list[dict]:
    done = [s for s in store.sessions() if s["n"] > 0]
    return store.samples(done[0]["id"]) if done else []


def current_model() -> str | None:
    chosen = store.get_setting("model")
    installed = ollama.list_models()
    names = [m["name"] for m in installed]
    if chosen and chosen in names:
        return chosen
    rec = models.recommend(get_hw(), installed)
    return rec["auto_choice"]


def hw_brief(hw: dict) -> dict:
    g = hw.get("primary_gpu") or {}
    mem = hw.get("memory") or {}
    sticks = mem.get("sticks") or []
    return {"cpu": hw["cpu"].get("name"), "gpu": g.get("name"), "vram_gb": g.get("vram_gb"),
            "ram_gb": mem.get("total_gb"), "ram_sticks": len(sticks),
            "ram_speed": sticks[0].get("configured_speed") if sticks else None,
            "board": (hw.get("board") or {}).get("model"), "os": (hw.get("os") or {}).get("name")}


# --- status / hardware / models ----------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"version": __version__, "ollama": ollama.version(), "lhm": sensors.lhm_available(),
            "nvidia_smi": bool(hardware._nvidia_smi()), "model": current_model(),
            "recording": _recorder.status()}


@app.get("/api/hardware")
def api_hardware(refresh: bool = False):
    return get_hw(refresh)


@app.get("/api/models")
def api_models():
    installed = ollama.list_models()
    rec = models.recommend(get_hw(), installed)
    rec["selected"] = current_model()
    rec["manual"] = store.get_setting("model")
    rec["benchmark"] = store.get_setting("benchmark")
    rec["pull"] = _pull_state
    return rec


class ModelIn(BaseModel):
    name: str | None = None


@app.post("/api/models/select")
def api_select(body: ModelIn):
    store.set_setting("model", body.name)  # None = automatic
    return {"selected": current_model()}


_pull_state: dict = {"name": None, "status": None, "completed": 0, "total": 0, "error": None, "done": True}


@app.post("/api/models/pull")
def api_pull(body: ModelIn):
    if not body.name:
        raise HTTPException(400, "name krävs")
    if not _pull_state["done"]:
        raise HTTPException(409, "En nedladdning pågår redan")
    _pull_state.update(name=body.name, status="startar", completed=0, total=0, error=None, done=False)

    def work():
        try:
            def prog(m):
                _pull_state["status"] = m.get("status")
                if m.get("total"):
                    _pull_state["total"] = m["total"]
                    _pull_state["completed"] = m.get("completed", 0)
            ollama.pull(body.name, prog)
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            _pull_state["error"] = str(e)
        finally:
            _pull_state["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return _pull_state


@app.post("/api/models/benchmark")
def api_benchmark():
    model = current_model()
    if not model:
        raise HTTPException(400, "Ingen modell installerad")
    res = llm.benchmark(model)
    res["model"] = model
    store.set_setting("benchmark", res)
    return res


# --- sensors / recording ----------------------------------------------------------

class Recorder:
    def __init__(self):
        self.sid = None
        self.until = 0.0
        self.started = 0.0
        self.label = ""
        self.stop_flag = threading.Event()

    def status(self):
        if self.sid is None:
            return None
        return {"session_id": self.sid, "label": self.label, "elapsed": round(time.time() - self.started),
                "remaining": max(0, round(self.until - time.time()))}

    def start(self, seconds: int, label: str):
        if self.sid is not None:
            raise HTTPException(409, "En inspelning pågår redan")
        self.sid = store.new_session(label, seconds)
        self.started = time.time()
        self.until = self.started + seconds
        self.label = label
        self.stop_flag.clear()
        threading.Thread(target=self._run, daemon=True).start()
        return self.status()

    def _run(self):
        sid = self.sid
        try:
            while time.time() < self.until and not self.stop_flag.is_set():
                t0 = time.time()
                s = sensors.sample()
                store.add_sample(sid, s["ts"], s["metrics"])
                self.stop_flag.wait(max(0.0, 1.0 - (time.time() - t0)))
        finally:
            store.end_session(sid)
            self.sid = None

    def stop(self):
        self.stop_flag.set()


_recorder = Recorder()


@app.get("/api/live")
def api_live():
    return sensors.sample()


class RecordIn(BaseModel):
    seconds: int = 120
    label: str = "Inspelning"


@app.post("/api/record/start")
def api_record_start(body: RecordIn):
    return _recorder.start(max(10, min(body.seconds, 3600)), body.label[:80])


@app.post("/api/record/stop")
def api_record_stop():
    _recorder.stop()
    return {"ok": True}


@app.get("/api/sessions")
def api_sessions():
    return store.sessions()


@app.get("/api/sessions/{sid}")
def api_session(sid: int):
    samples = store.samples(sid)
    return {"samples": samples, "summary": llm.summarize_samples(samples),
            "findings": [asdict(f) for f in rules.rule_samples(samples)]}


# --- diagnosis / events / AI ------------------------------------------------------

@app.get("/api/events")
def api_events(refresh: bool = False):
    return get_events(refresh)


def all_findings(session_id: int | None = None):
    samples = store.samples(session_id) if session_id else latest_session_samples()
    found = rules.diagnose(get_hw(), get_events(), samples)
    if not any(f["id"] == "nvme_hot" for f in found):
        # Disk heat shows up at idle too, so check a live reading even without a recording.
        try:
            live = [asdict(f) for f in rules.rule_storage_temp([sensors.sample()["metrics"]])]
        except Exception:  # noqa: BLE001 - sensors are best effort
            live = []
        found = sorted(found + live, key=lambda f: rules.SEVERITY_ORDER.get(f["severity"], 9))
    return found


@app.get("/api/diagnose")
def api_diagnose(session_id: int | None = None):
    return all_findings(session_id)


def _need_model() -> str:
    model = current_model()
    if not model:
        raise HTTPException(400, "Ingen AI-modell installerad. Ladda ner den rekommenderade modellen först.")
    if not ollama.version():
        raise HTTPException(503, "Ollama körs inte. Starta Ollama och försök igen.")
    return model


@app.post("/api/diagnose/explain")
def api_explain():
    model = _need_model()
    findings = all_findings()
    slim = [{k: f[k] for k in ("severity", "title", "detail", "fix", "evidence")} for f in findings]
    try:
        res = llm.explain_findings(model, slim, hw_brief(get_hw()))
    except ollama.OllamaError as e:
        raise HTTPException(502, str(e)) from e
    res["model"] = model
    return res


class ChatIn(BaseModel):
    messages: list[dict]


def _tool_impl():
    def events_tool(args):
        cat = args.get("category") or "all"
        limit = int(args.get("limit") or 25)
        ev = [e for e in get_events() if cat == "all" or e.get("cat") == cat]
        return [{"tid": e["t"][:16], "typ": e["cat"], "id": e["id"], "beskrivning": e["text"]} for e in ev[:limit]]

    def system_tool(args):
        hw = get_hw()
        mem = hw.get("memory") or {}
        return {**hw_brief(hw),
                "ram_moduler": [{k: s.get(k) for k in ("slot", "size_gb", "configured_speed", "rated_speed_guess", "part_number")}
                                for s in mem.get("sticks") or []],
                "bios": hw.get("bios"), "diskar": hw.get("disks"), "volymer": hw.get("volumes"),
                "energischema": (hw.get("power_scheme") or {}).get("name"),
                "gpu_drivrutin": (hw.get("primary_gpu") or {}).get("driver_version")}

    def findings_tool(args):
        return [{k: f[k] for k in ("severity", "title", "detail", "fix", "evidence")} for f in all_findings()]

    return {
        "get_system_info": system_tool,
        "get_findings": findings_tool,
        "get_recent_events": events_tool,
        "get_sensor_summary": lambda a: llm.summarize_samples(latest_session_samples()),
        "get_live_sensors": lambda a: sensors.sample()["metrics"],
    }


@app.post("/api/chat")
def api_chat(body: ChatIn):
    model = _need_model()
    history = [{"role": m["role"], "content": m["content"]} for m in body.messages[-12:]
               if m.get("role") in ("user", "assistant") and m.get("content")]
    context = {"dator": hw_brief(get_hw()),
               "fynd": [{k: f[k] for k in ("severity", "title", "detail", "fix")} for f in all_findings()],
               "händelser_30_dagar": dict(Counter(e["cat"] for e in get_events()))}
    try:
        res = llm.run_chat(model, history, _tool_impl(), context=context)
    except ollama.OllamaError as e:
        raise HTTPException(502, str(e)) from e
    res["model"] = model
    return res


@app.get("/api/tokens")
def api_tokens():
    return store.token_totals()
