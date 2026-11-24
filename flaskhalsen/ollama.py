"""Minimal Ollama HTTP client."""

from __future__ import annotations

import json
import os
from typing import Callable

import httpx

BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not BASE.startswith("http"):
    BASE = "http://" + BASE


# Lesson learned: with a game running, num_ctx=8192 made a 1.2k-token prompt take >3 min
# (the bigger KV cache got paged out of VRAM), while 4096 took 1.5 s. Keep it modest
# and keep the prompts compact instead. Same value for every call, or Ollama reloads the model.
NUM_CTX = int(os.environ.get("FLASKHALSEN_NUM_CTX", "4096"))


class OllamaError(RuntimeError):
    pass


def version() -> str | None:
    try:
        return httpx.get(f"{BASE}/api/version", timeout=2).json().get("version")
    except (httpx.HTTPError, ValueError):
        return None


def list_models() -> list[dict]:
    try:
        return httpx.get(f"{BASE}/api/tags", timeout=5).json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []


def pull(name: str, on_progress: Callable[[dict], None]) -> None:
    with httpx.stream("POST", f"{BASE}/api/pull", json={"name": name, "stream": True}, timeout=None) as r:
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if "error" in msg:
                raise OllamaError(msg["error"])
            on_progress(msg)


def chat(model: str, messages: list[dict], tools: list[dict] | None = None,
         think: bool = False, timeout: float = 180, max_tokens: int = 900) -> dict:
    # num_predict caps the answer: without it a small model can ramble for minutes,
    # and Ollama keeps generating even after the client gave up, blocking later requests.
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0.3, "num_ctx": NUM_CTX, "num_predict": max_tokens}}
    if tools:
        body["tools"] = tools
    body["think"] = think
    try:
        r = httpx.post(f"{BASE}/api/chat", json=body, timeout=timeout)
        if r.status_code == 400 and "think" in r.text:
            body.pop("think")  # model/server without thinking support
            r = httpx.post(f"{BASE}/api/chat", json=body, timeout=timeout)
    except httpx.TimeoutException as e:
        raise OllamaError("AI-modellen svarade inte i tid. Kör ett spel samtidigt? Då får modellen "
                          "dela grafikminnet och blir mycket långsammare.") from e
    except httpx.HTTPError as e:
        raise OllamaError(f"Kunde inte nå Ollama: {e}") from e
    if r.status_code != 200:
        raise OllamaError(f"Ollama svarade {r.status_code}: {r.text[:300]}")
    return r.json()
