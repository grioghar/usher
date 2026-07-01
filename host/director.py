#!/usr/bin/env python3
"""usher — N+1 load director for Plex (thin host runtime).

The routing *policy* is the Sigil-compiled WASM core (plex_director.wasm). This
host only does I/O: it polls every configured Plex server, folds the Sigil core
over them to pick the least-busy healthy one, and 302-redirects each viewer
there (sticky per session). Any number of servers; a down server is skipped
(that is the N+1 property). Zero policy logic lives here.

Config (USHER_CONFIG, default /config/usher.yaml):

    plex_token: "xxxxx"          # or set PLEX_TOKEN env
    poll_sec: 5
    port: 8099
    redirect_mode: app           # app -> app.plex.tv/server/<machine_id>
                                 # direct -> <poll_url>/web
    sticky_sec: 1800
    servers:
      - name: plhoenix
        poll_url: http://10.0.0.5:32400
        machine_id: ae4ca1f0...
      - name: quicksync
        poll_url: http://10.0.0.6:32400
        machine_id: 4cb92d7a...
"""
import json, os, time, threading, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import wasmtime

def _load_config():
    path = os.environ.get("USHER_CONFIG", "/config/usher.yaml")
    raw = open(path, "r", encoding="utf-8").read()
    try:
        import yaml
        return yaml.safe_load(raw)
    except ImportError:
        return json.loads(raw)          # a JSON config also works

CFG           = _load_config()
TOKEN         = os.environ.get("PLEX_TOKEN") or CFG.get("plex_token", "")
SERVERS       = CFG["servers"]          # [{name, poll_url, machine_id}]
POLL_SEC      = int(CFG.get("poll_sec", 5))
PORT          = int(CFG.get("port", 8099))
REDIRECT_MODE = CFG.get("redirect_mode", "app")
STICKY_SEC    = int(CFG.get("sticky_sec", 1800))
def _find_wasm():
    if os.environ.get("USHER_WASM"):
        return os.environ["USHER_WASM"]
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "plex_director.wasm"),                 # Docker/LXC (flattened)
              os.path.join(here, "..", "src", "plex_director.wasm")):   # repo layout
        if os.path.exists(p):
            return p
    return os.path.join(here, "plex_director.wasm")
WASM          = _find_wasm()

# --- Sigil decision core (WASM) -------------------------------------------
_ENGINE = wasmtime.Engine()
_MODULE = wasmtime.Module(_ENGINE, open(WASM, "rb").read())
_SENTINEL = 9_000_000 * 1000 + 999      # seed: huge score, "none" index

def _step(packed, up, streams, transcodes, cpu, idx):
    store = wasmtime.Store(_ENGINE)
    externs = [wasmtime.Func(store, i.type, lambda *a, _r=i.type.results: tuple(0 for _ in _r))
               for i in _MODULE.imports if isinstance(i.type, wasmtime.FuncType)]
    inst = wasmtime.Instance(store, _MODULE, externs)
    return int(inst.exports(store)["main"](store, packed, up, streams, transcodes,
                                           min(100, max(0, cpu)), idx))

# --- Plex polling ----------------------------------------------------------
def _poll(url):
    """(up, streams, transcodes) for one PMS."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/status/sessions",
                                     headers={"X-Plex-Token": TOKEN, "Accept": "application/json"})
        mc = json.load(urllib.request.urlopen(req, timeout=4)).get("MediaContainer", {})
        streams = int(mc.get("size", 0))
        transcodes = sum(1 for m in (mc.get("Metadata", []) or []) if m.get("TranscodeSession"))
        return (1, streams, transcodes)
    except Exception:
        return (0, 0, 0)

STATE = {"servers": [], "chosen": None, "chosen_idx": None, "ts": 0}

def refresh():
    packed, rows = _SENTINEL, []
    for i, s in enumerate(SERVERS):
        up, streams, tc = _poll(s["poll_url"])
        rows.append({"name": s.get("name", f"server{i}"), "up": up,
                     "streams": streams, "transcodes": tc})
        packed = _step(packed, up, streams, tc, 0, i)   # cpu=0: streams/transcodes dominate
    idx = packed % 1000
    chosen_idx = None if idx == 999 else idx
    STATE.update({"servers": rows, "chosen_idx": chosen_idx, "ts": int(time.time()),
                  "chosen": None if chosen_idx is None else SERVERS[chosen_idx].get("name")})
    return chosen_idx

def _bg():
    while True:
        try: refresh()
        except Exception: pass
        time.sleep(POLL_SEC)

def _target(i):
    s = SERVERS[i]
    if REDIRECT_MODE == "direct":
        return s["poll_url"].rstrip("/") + "/web"
    return "https://app.plex.tv/desktop/#!/server/" + s["machine_id"]

# --- HTTP entry ------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            return self._send(200, json.dumps(STATE, indent=2).encode(), "application/json")
        if self.path.startswith("/health"):
            return self._send(200, b"ok")
        idx = STATE["chosen_idx"]
        if idx is None:
            return self._send(503, b"No Plex server available right now.")
        self._send(302, b"", extra={"Location": _target(idx),
                                    "Set-Cookie": f"usher_route={idx}; Max-Age={STICKY_SEC}; Path=/"})
    def log_message(self, *a): pass

if __name__ == "__main__":
    refresh()
    threading.Thread(target=_bg, daemon=True).start()
    print(f"usher on :{PORT}  servers={[s.get('name') for s in SERVERS]}  mode={REDIRECT_MODE}", flush=True)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
