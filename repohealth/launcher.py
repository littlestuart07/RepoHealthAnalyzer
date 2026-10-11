"""Opening the app in a window rather than a browser tab.

The server is an ordinary local web server, so the simplest "desktop app" is a
Chromium window started in app mode: no address bar, no tabs, its own taskbar
entry. That needs no extra dependency, since any machine running the app
already has a browser.

Three routes are tried in order, each falling back to the next:
  1. pywebview, if the user installed it -- a genuine native window
  2. Chrome or Edge in --app mode -- chromeless, indistinguishable in practice
  3. the default browser -- always works
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_SIZE = (1280, 860)

_WINDOWS_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]
_POSIX_NAMES = ["google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser", "microsoft-edge"]


def find_chromium() -> str | None:
    """Path to a Chromium-family browser, or None."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = _WINDOWS_CANDIDATES + (
            [str(Path(local) / "Google/Chrome/Application/chrome.exe")] if local else []
        )
    elif sys.platform == "darwin":
        candidates = _MAC_CANDIDATES
    else:
        candidates = []
        for name in _POSIX_NAMES:
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def open_window(url: str, title: str = "RepoHealth") -> str:
    """Open `url` in the most app-like window available.

    Returns which route was taken: "pywebview", "chrome" or "browser".
    Blocks only for pywebview, which must own the main thread.
    """
    w, h = WINDOW_SIZE

    try:
        import webview  # type: ignore
    except ImportError:
        pass
    else:
        webview.create_window(title, url, width=w, height=h, min_size=(900, 600))
        webview.start()
        return "pywebview"

    exe = find_chromium()
    if exe:
        # A separate user-data-dir keeps the app window out of the user's
        # ordinary browser session -- no shared tabs, no profile pollution.
        profile = Path.home() / ".repohealth" / "window"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                [exe, f"--app={url}", f"--window-size={w},{h}",
                 f"--user-data-dir={profile}", "--no-first-run",
                 "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return "chrome"
        except OSError:
            pass

    webbrowser.open(url)
    return "browser"
