"""Start Flaskhalsen: python run.py [--port 8765] [--no-browser]

Also the entry point for the portable build (Flaskhalsen.exe).
"""

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

FROZEN = getattr(sys, "frozen", False)
ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent  # noqa: SLF001
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from flaskhalsen import __version__  # noqa: E402
from flaskhalsen.app import app  # noqa: E402

def open_browser(url: str) -> None:
    """Open the UI. When we run elevated (the exe asks for admin), a plain webbrowser.open
    would start the browser as administrator too. explorer.exe hands the URL to the
    normal, non-elevated desktop session instead."""
    if os.name == "nt":
        try:
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                import subprocess
                subprocess.Popen(["explorer.exe", url])
                return
        except OSError:
            pass
    webbrowser.open(url)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://127.0.0.1:{a.port}"
    if not a.no_browser:
        threading.Timer(1.5, lambda: open_browser(url)).start()
    print(f"Flaskhalsen {__version__} körs på {url}")
    print("Stäng det här fönstret för att avsluta.")
    # Pass the app object (not an import string) so it also works inside the frozen exe.
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
