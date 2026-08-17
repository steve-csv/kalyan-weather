"""
Local web server - read the forecast on your phone, and refresh it from there.

Serves the generated dashboard over the home network so a phone on the same
WiFi can open it, and exposes a Refresh button that re-runs the daily build on
the PC. Nothing is exposed to the internet: the server binds the LAN interface
only, and the refresh endpoint is a plain POST from the page itself.

    python -m wxagent serve

Then open the printed http://<pc-ip>:8000 on the phone.

DESIGN NOTES
------------
* The rebuild runs in a background thread so the page stays responsive and the
  phone gets an immediate acknowledgement rather than a hung request.
* Only ONE rebuild can be in flight at a time. Without that guard, an impatient
  double-tap on a phone would start two full builds in parallel and burn twice
  the API budget - which is exactly how the daily quota was exhausted once
  already.
* A cooldown blocks refreshes that arrive too close together, for the same
  reason. The daily build is ~30 requests; the free tier is finite.
* The refresh runs the DAILY build only. The weekly is several times the
  request cost and is left to its Sunday schedule.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C

DEFAULT_PORT = 8000
REFRESH_COOLDOWN_S = 120        # minimum gap between accepted refreshes

_state_lock = threading.Lock()
_state = {
    "running": False,
    "last_started": 0.0,
    "last_finished": 0.0,
    "last_result": "",
    "last_error": "",
}


# --------------------------------------------------------------------------
# Keeping the machine awake
# --------------------------------------------------------------------------
# A sleeping laptop cannot serve: the CPU halts and the network adapter drops.
# There is no server-side fix for that. What CAN be done is ask Windows not to
# idle-sleep for as long as the server is running, which is the difference
# between "available while I'm at my desk" and "available all evening".
#
# This is scoped to the process: the request is released the moment the server
# stops, so nothing is left permanently changed on the machine.

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(on: bool) -> bool:
    """Ask Windows to hold off idle sleep. Returns True if the call landed."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED) if on else ES_CONTINUOUS
        # Display sleep is deliberately NOT blocked - the screen can go dark,
        # the machine just must not suspend.
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:                             # noqa: BLE001
        return False


def lid_close_action() -> str | None:
    """What Windows currently does when the lid closes, on mains power."""
    try:
        out = subprocess.run(
            ["powercfg", "/q", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION"],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:                             # noqa: BLE001
        return None
    m = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    if not m:
        return None
    return {"0": "do nothing", "1": "sleep",
            "2": "hibernate", "3": "shut down"}.get(str(int(m.group(1), 16)))


def lan_ip() -> str:
    """Best-guess LAN address. No traffic is actually sent to the probe host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _explain(proc: subprocess.CompletedProcess) -> str:
    """
    Pick the line that actually says what went wrong.

    Taking the last line of output gives whatever trailing advice was printed,
    not the cause - which is useless on a phone. Prefer the line stating the
    failure, and fall back to the last non-empty line only if nothing matches.
    """
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return f"exit code {proc.returncode}"

    for marker in ("Cannot fetch data:", "Cannot build", "Error", "error:"):
        for ln in lines:
            if marker.lower() in ln.lower():
                return ln.split(":", 1)[-1].strip() if ":" in ln else ln
    # Traceback: the final exception line is the informative one.
    for ln in reversed(lines):
        if "Error" in ln or "Exception" in ln:
            return ln
    return lines[-1]


def _run_daily() -> None:
    """Run the daily build, recording the outcome for the status endpoint."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "wxagent", "daily", "--no-notify", "--quiet"],
            cwd=str(C.ROOT), capture_output=True, text=True, timeout=900,
            # The child prints UTF-8; without this Windows decodes it as cp1252
            # and every em-dash arrives on the phone as mojibake.
            encoding="utf-8", errors="replace",
        )
        ok = proc.returncode == 0
        with _state_lock:
            _state["last_result"] = "ok" if ok else f"failed ({proc.returncode})"
            _state["last_error"] = "" if ok else _explain(proc)
    except subprocess.TimeoutExpired:
        with _state_lock:
            _state["last_result"] = "failed"
            _state["last_error"] = "build timed out"
    except Exception as exc:                      # noqa: BLE001
        with _state_lock:
            _state["last_result"] = "failed"
            _state["last_error"] = str(exc)
    finally:
        with _state_lock:
            _state["running"] = False
            _state["last_finished"] = time.time()


def _start_refresh() -> tuple[int, dict]:
    """Kick off a rebuild if one is not already running or too recent."""
    now = time.time()
    with _state_lock:
        if _state["running"]:
            return 409, {"status": "already-running",
                         "message": "A rebuild is already in progress."}
        since = now - _state["last_started"]
        if _state["last_started"] and since < REFRESH_COOLDOWN_S:
            wait = int(REFRESH_COOLDOWN_S - since)
            return 429, {"status": "cooldown", "waitSeconds": wait,
                         "message": (f"Last refresh was {int(since)}s ago. "
                                     f"Wait {wait}s — each build costs about "
                                     "30 API requests and the daily budget is "
                                     "finite.")}
        _state["running"] = True
        _state["last_started"] = now
        _state["last_error"] = ""
        _state["last_result"] = ""

    threading.Thread(target=_run_daily, daemon=True).start()
    return 202, {"status": "started",
                 "message": "Rebuilding — takes about a minute."}


def _status() -> dict:
    with _state_lock:
        st = dict(_state)
    page = C.FORECAST_DIR / "index.html"
    st["pageModified"] = (
        datetime.fromtimestamp(page.stat().st_mtime).strftime("%d %b %H:%M")
        if page.exists() else None)
    st["serverTime"] = datetime.now().strftime("%d %b %H:%M")
    return st


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves forecasts/ plus the refresh and status endpoints."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(C.FORECAST_DIR), **kwargs)

    def log_message(self, fmt, *args):            # quieter console
        if "/api/" not in (self.path or ""):
            return
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                             # noqa: N802
        if self.path.startswith("/api/status"):
            self._json(200, _status())
            return
        if self.path in ("/", ""):
            self.path = "/index.html"
        # Generated pages are rewritten in place; never let a phone cache them.
        super().do_GET()

    def do_POST(self):                            # noqa: N802
        if self.path.startswith("/api/refresh"):
            code, payload = _start_refresh()
            self._json(code, payload)
            return
        self.send_error(404)

    def guess_type(self, path):
        # The generated pages are UTF-8 and artifact.html carries no <meta
        # charset> (its head is supplied by the host). Without an explicit
        # charset here the browser falls back to latin-1 and every em-dash and
        # emoji renders as mojibake.
        ctype = super().guess_type(path)
        if ctype in ("text/html", "text/markdown", "text/plain"):
            return ctype + "; charset=utf-8"
        return ctype

    def end_headers(self):
        if self.path.endswith((".html", ".md")) or self.path in ("/", ""):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


class Server(socketserver.ThreadingTCPServer):
    # allow_reuse_address maps to SO_REUSEADDR, which on Windows does NOT mean
    # what it means on Unix: it lets a second socket bind a port that is
    # already actively listening, silently hijacking it. Two servers then hold
    # the same port and requests land on whichever the OS picks - a genuinely
    # confusing failure, and it also suppresses the "port already in use" error
    # this program relies on to tell the user a server is already running.
    # On Windows the safe setting is off; on Unix it stays on so a restart
    # after Ctrl+C is not blocked by TIME_WAIT.
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def _port_holder(port: int) -> int | None:
    """PID currently listening on `port`, if it can be determined."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=15, encoding="utf-8", errors="replace",
        ).stdout
    except Exception:                             # noqa: BLE001
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


def find_cloudflared() -> str | None:
    """
    Locate cloudflared.

    shutil.which alone is not enough: a freshly-installed program is on the
    machine PATH, but a process that started before the install inherited the
    old PATH and will never see it. That produced a confident "cloudflared is
    not installed" message minutes after installing it. So the standard install
    locations are checked directly as well.
    """
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\cloudflared\cloudflared.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def start_tunnel(port: int) -> subprocess.Popen | None:
    """
    Expose the local server publicly via a Cloudflare quick tunnel.

    This is what makes "can we do it as localhost" actually work for someone
    abroad: localhost and 192.168.x.x are unreachable from outside the house,
    but a tunnel gives this same server a public https address.

    Quick tunnels need no Cloudflare account. The trade-offs are real and
    printed for the user: the address changes every restart, and while it runs
    the page is reachable by anyone holding that address.
    """
    exe = find_cloudflared()
    if not exe:
        print("  ! cloudflared is not installed, so no public link.")
        print("    Install it once with:")
        print("        winget install --id Cloudflare.cloudflared")
        print("    then run this again with --tunnel.\n")
        return None

    print("  Starting public tunnel (Cloudflare)…")
    proc = subprocess.Popen(
        [exe, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    # cloudflared prints the assigned hostname to its log within a few seconds.
    url = None
    deadline = time.time() + 40
    while time.time() < deadline and proc.poll() is None:
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            continue
        m = re.search(r"https://[-\w]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break

    if url:
        print("\n" + "=" * 58)
        print("  PUBLIC LINK (share this one):")
        print(f"    {url}/")
        print("=" * 58)
        print("  Anyone with this address can open the forecast while this")
        print("  window stays open. It changes each time you restart.\n")
    else:
        print("  ! Tunnel did not report an address in time; it may still be")
        print("    starting. Check the cloudflared output above.\n")

    # Keep draining the pipe so cloudflared never blocks on a full buffer.
    threading.Thread(
        target=lambda: [None for _ in iter(proc.stdout.readline, "")],
        daemon=True,
    ).start()
    return proc


def _auto_stop(httpd, hours: float) -> threading.Thread:
    """
    Shut the server down after `hours`, releasing the sleep hold with it.

    Holding a laptop awake indefinitely to serve a page nobody is reading is a
    poor trade - it burns power and wear for no benefit. A bounded window means
    the machine goes back to normal on its own if you forget to close the
    window.
    """
    deadline = datetime.now() + timedelta(hours=hours)

    def run():
        # Poll a wall-clock DEADLINE rather than sleeping for the whole
        # duration in one call. A single long time.sleep() does not reliably
        # account for the machine suspending and resuming - a 4-hour limit set
        # this way was still running four days later, because the timer never
        # caught up with real time. Re-reading the clock is immune to that.
        while datetime.now() < deadline:
            time.sleep(min(30.0, max(1.0,
                       (deadline - datetime.now()).total_seconds())))
        print(f"\n{hours:g}-hour limit reached — shutting the server down so "
              "the machine can sleep normally.")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def serve(port: int = DEFAULT_PORT, *, tunnel: bool = False,
          allow_sleep: bool = False, hours: float = 2.0) -> int:
    ip = lan_ip()
    page = C.FORECAST_DIR / "index.html"
    if not page.exists():
        print("No dashboard built yet. Run this first:")
        print("    python -m wxagent daily")
        return 1

    print("Kalyan weather agent — local server\n")
    print(f"  On this PC : http://localhost:{port}/")
    print(f"  On phone   : http://{ip}:{port}/")
    print(f"  MMR week   : http://{ip}:{port}/mmr.html")
    print("\nThose two work on your home WiFi only.\n")

    awake = False
    if not allow_sleep:
        awake = keep_awake(True)
        if awake:
            print("  Sleep held off while this server runs (screen can still")
            print("  switch off; the machine just won't suspend).")
            lid = lid_close_action()
            if lid in ("sleep", "hibernate", "shut down"):
                print(f"  NOTE: closing the lid still makes it {lid}. To serve")
                print("  with the lid shut, change that in Windows: Power &")
                print("  sleep > Additional power settings > Choose what")
                print("  closing the lid does > 'Do nothing' (plugged in).")
        else:
            print("  ! Could not hold off sleep; the machine may suspend and")
            print("    the page will go offline until it wakes.")
        print()

    tunnel_proc = start_tunnel(port) if tunnel else None

    if hours and hours > 0:
        stop_at = datetime.now() + timedelta(hours=hours)
        print(f"Runs for {hours:g} hours, until about {stop_at:%H:%M}, then "
              "stops on its own")
        print("so the laptop can sleep normally. Ctrl+C to stop sooner.\n")
    else:
        print("Runs until you close this window. Ctrl+C to stop.\n")

    try:
        with Server(("0.0.0.0", port), Handler) as httpd:
            if hours and hours > 0:
                _auto_stop(httpd, hours)
            httpd.serve_forever()
            print("Server stopped.")
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as exc:
        print(f"\nCould not start on port {port}: {exc}\n")
        holder = _port_holder(port)
        if holder:
            print(f"Port {port} is already held by PID {holder}.")
            print("That is almost always this same server left running from")
            print("earlier. Close that window, or stop it with:")
            print(f"    powershell -Command \"Stop-Process -Id {holder} -Force\"")
        else:
            print("Another program may be using it.")
        print(f"\nOr just use a different port:  python -m wxagent serve --port 8080")
        return 1
    finally:
        # The tunnel outlives the server otherwise, leaving a public address
        # pointing at a dead port.
        if tunnel_proc is not None and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            print("Public tunnel closed.")
        if awake:
            keep_awake(False)
            print("Normal sleep behaviour restored.")
    return 0
