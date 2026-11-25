"""One-click Ollama install via winget (Ollama's own signed installer, per-user, no admin)."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import ollama

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
state: dict = {"running": False, "step": None, "error": None, "done": False}


def _app_exe() -> Path | None:
    p = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    return p if p.exists() else None


def _start_ollama() -> None:
    exe = _app_exe()
    if exe:
        subprocess.Popen([str(exe)], creationflags=_NO_WINDOW, close_fds=True)


def _wait_ready(seconds: int = 90) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if ollama.version():
            return True
        time.sleep(2)
    return False


def _work() -> None:
    try:
        if not _app_exe():
            winget = shutil.which("winget")
            if not winget:
                raise RuntimeError("winget saknas på den här datorn. Installera Ollama manuellt från ollama.com/download.")
            state["step"] = "Laddar ner och installerar Ollama (ca 1 GB, några minuter)…"
            r = subprocess.run([winget, "install", "--id", "Ollama.Ollama", "-e", "--silent",
                                "--accept-source-agreements", "--accept-package-agreements"],
                               capture_output=True, timeout=1800, creationflags=_NO_WINDOW)
            if not _app_exe():
                tail = r.stdout.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
                raise RuntimeError(f"Installationen misslyckades (winget {r.returncode}). {tail[0]}")
        if not ollama.version():
            state["step"] = "Startar Ollama…"
            _start_ollama()
            if not _wait_ready():
                raise RuntimeError("Ollama installerades men startade inte. Starta Ollama från Start-menyn.")
        state["step"] = "Klart"
    except Exception as e:  # noqa: BLE001 - shown in the UI
        state["error"] = str(e)
    finally:
        state["running"] = False
        state["done"] = True


def install() -> dict:
    if state["running"]:
        return state
    state.update(running=True, step="Startar…", error=None, done=False)
    threading.Thread(target=_work, daemon=True).start()
    return state
