"""Small helpers: running PowerShell/commands and parsing their output."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Force UTF-8 so Swedish characters (å, ä, ö) in localized output survive.
_PS_PREFIX = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$ErrorActionPreference = 'SilentlyContinue'; "
)


def run_cmd(args: list[str], timeout: float = 15) -> tuple[int, str]:
    """Run a command, return (returncode, stdout). Never raises for missing exe."""
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return -1, ""
    return r.returncode, r.stdout.decode("utf-8", errors="replace")


def run_powershell_json(script: str, timeout: float = 45):
    """Run a PowerShell script whose output is JSON; return parsed value or None."""
    code, out = run_cmd(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", _PS_PREFIX + script],
        timeout=timeout,
    )
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


_MS_DATE = re.compile(r"/Date\((-?\d+)\)/")


def parse_date(value) -> datetime | None:
    """Parse the date formats PowerShell/WMI/registry hand back."""
    if value is None or value == "":
        return None
    if isinstance(value, dict) and "value" in value:  # PS DateTime with DisplayHint
        value = value["value"]
    s = str(value)
    m = _MS_DATE.search(s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%m-%d-%Y", "%Y%m%d"):
        try:
            dt = datetime.strptime(s[:32], fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def as_list(value) -> list:
    """PowerShell's ConvertTo-Json turns 1-item arrays into objects; undo that."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_number(text) -> float | None:
    """Parse '45,0 °C' or '1234.5 MHz' (either decimal separator)."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _NUM.search(str(text).replace(" ", " "))
    if not m:
        return None
    return float(m.group(0).replace(",", "."))
