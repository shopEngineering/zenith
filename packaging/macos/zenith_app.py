#!/usr/bin/env python3
"""PyInstaller entry point for the ZENITH.app bundle.

Starts the stdlib server in a background thread, waits until the port is actually
listening, opens the default browser, then blocks forever (the server owns the
process lifetime). All the frozen-vs-source path handling lives in server.py and
is gated on sys.frozen; this launcher stays deliberately tiny.
"""
import os
import socket
import sys
import threading
import time
import webbrowser

# Force UTF-8 stdio: Windows defaults to cp1252, which can't encode the server's
# banner/statusline glyphs (→, █) and would crash the process. Guarded (windowed
# builds may have no console → streams are None).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import server

PORT = int(os.environ.get("ZENITH_PORT", "8777"))
HOST = "127.0.0.1"


def _wait_until_listening(timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex((HOST, PORT)) == 0:
                return True
        time.sleep(0.15)
    return False


def _run_server():
    try:
        server.main()
    except Exception:
        # Windowed builds have no console; persist the traceback so startup
        # failures are diagnosable (and visible in CI).
        import traceback
        from pathlib import Path
        try:
            d = Path.home() / ".zenith"
            d.mkdir(parents=True, exist_ok=True)
            (d / "startup-error.log").write_text(traceback.format_exc())
        except Exception:
            pass
        raise


def main():
    # server.main() blocks on serve_forever(); run it in a daemon thread so we can
    # wait for the socket and open the browser from the main thread.
    threading.Thread(target=_run_server, daemon=True).start()
    # Open the browser once the port is up — unless suppressed (ZENITH_NO_BROWSER=1,
    # e.g. headless CI). A browser-open failure must never take the server down.
    if _wait_until_listening() and os.environ.get("ZENITH_NO_BROWSER") != "1":
        try:
            webbrowser.open(f"http://{HOST}:{PORT}")
        except Exception:
            pass
    # Keep the process alive while the daemon server thread serves requests.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
