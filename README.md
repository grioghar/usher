# usher

**An N+1, load-aware director for Plex.** Point your viewers at one URL; usher
sends each of them to your *least-busy* Plex server — and quietly skips any
server that's down. The routing brain is written in **[Sigil](https://github.com/grioghar/sigil)**
and compiled to **WebAssembly**; the runtime is a tiny host that just does I/O.

Runs in **Docker** or an **LXC** (or any host with Python). No database, no
account of its own, ~150 lines of host code.

```
                         ┌──────────────────────────┐
 viewer → watch.you.tld →│  usher (this project)     │
                         │  • poll every PMS          │
                         │  • fold the Sigil/WASM     │──► 302 → least-busy PMS
                         │    core over them          │       (sticky per session)
                         │  • redirect to the winner  │
                         └──────────────────────────┘
        servers: [ plex-A ] [ plex-B ] [ plex-C ] …   (any N; down ones skipped)
```

---

## Why

If you run **more than one Plex server over the same library** (e.g. a beefy box
plus a small QuickSync/NVENC transcode node, all reading the same media), there's
no built-in way to spread viewers across them. usher gives you:

- **Load balancing** — new sessions go to whichever server has the lightest load
  (a transcode counts far more than a direct-play stream).
- **N+1 redundancy** — list N servers; if one falls over, viewers are routed to
  the healthy remainder automatically. Add a spare node and you have N+1.
- **A tiny, auditable core** — the decision policy is a contract-checked Sigil
  program compiled to a ~250-byte WASM module, not buried in glue code.

## What it is *not* (read this)

Plex servers each have their **own identity and their own database** (watch
history, "continue watching", metadata). Plex clients connect to a *specific*
server. usher therefore is a **director**, not a transparent proxy: it decides
*which* server you use and sends you there — it does **not** merge several
servers into one. For a seamless experience:

- Every listed server should serve the **same library set** (same media, same
  paths). The usual setup is one media store shared read-only (NFS/SMB) to each
  Plex node.
- Watch-state lives per server. If you want progress to match across nodes, run a
  one-way database mirror from your primary to the others (out of scope here).
- Hardware transcoding needs **Plex Pass** on the account, as usual.

## How the decision works

The Sigil core ([`src/plex_director.sg`](src/plex_director.sg)) scores each
server and the host folds it over all of them:

```
score = transcodes*100 + streams*30 + cpu/10     // a transcode ≫ a stream
```

- Lowest score wins; a **down** server (health check failed) can never win.
- On an exact tie the **earlier** server in your config wins — so config order is
  your tie-break priority.
- If every server is down, usher returns `503`.

Because host effects in Sigil compile to WASM *imports*, the module has no
ambient authority — it can only compute a number from the numbers it's given.

## Quick start — Docker

```bash
git clone https://github.com/grioghar/usher && cd usher
cp config/usher.example.yaml config/usher.yaml
$EDITOR config/usher.yaml                 # add your servers + machine_ids
export PLEX_TOKEN=xxxxxxxxxxxxxxxxxxxx     # an account X-Plex-Token
docker compose up -d --build
curl -s localhost:8099/status | jq        # see live load + the current pick
```

Then front `:8099` with your reverse proxy at e.g. `https://watch.you.tld` and
share that URL with your users.

## Quick start — LXC (or any Debian/Ubuntu host)

```bash
git clone https://github.com/grioghar/usher && cd usher
sudo lxc/install.sh                        # installs a systemd service in /opt/usher
sudo $EDITOR /etc/usher/usher.yaml
sudo systemctl edit usher                  # add: Environment=PLEX_TOKEN=xxxx
sudo systemctl restart usher
```

## Configuration

`config/usher.yaml` (see [`config/usher.example.yaml`](config/usher.example.yaml)):

| key | meaning |
|-----|---------|
| `servers[]` | your Plex servers: `name`, `poll_url` (internal `:32400`), `machine_id` |
| `redirect_mode` | `app` → `app.plex.tv/…/server/<machine_id>` (works remotely); `direct` → `<poll_url>/web` (LAN) |
| `poll_sec` | load re-measurement interval (default 5) |
| `sticky_sec` | how long a viewer stays pinned to their server (default 1800) |
| `port` | listen port (default 8099) |

Find a server's `machine_id`: `curl -s "http://<ip>:32400/identity?X-Plex-Token=TOKEN"`.

## Endpoints

- `GET /` → `302` redirect to the least-busy server (sets a sticky cookie).
- `GET /status` → JSON: each server's `up`/`streams`/`transcodes` and the current pick.
- `GET /health` → `ok`.

## Build the core from source (optional)

The prebuilt `src/plex_director.wasm` is committed, so you don't need the Sigil
compiler. To change the policy, edit `src/plex_director.sg` and rebuild:

```bash
CC0=/path/to/cc0 tools/build-wasm.sh       # needs cc0 from grioghar/sigil
```

## Layout

```
src/plex_director.sg     the routing policy, in Sigil  (the interesting part)
src/plex_director.wasm   prebuilt wasm32 module (committed)
host/director.py         thin runtime: poll → fold the wasm core → redirect
config/usher.example.yaml
Dockerfile · docker-compose.yml · lxc/install.sh · tools/build-wasm.sh
```

## License

MIT — see [LICENSE](LICENSE).

## Companion: `tools/plexsync.py` — keep watch-state in sync

A director spreads *sessions* across servers, but each Plex server keeps its own
watch history. `plexsync` reconciles that: an **API-level, bidirectional** diff
engine that reads watched + in-progress state from every server, matches items by
their **Plex GUID** (server-agnostic), and writes the **newer** state to the other
side (newest-wins, positive-state only). It never copies databases (that corrupts
a claimed server) and never marks anything unwatched.

```bash
PLEX_TOKEN=xxxx A_URL=http://a:32400 B_URL=http://b:32400 \
  python3 tools/plexsync.py            # DRY-RUN: shows the diff, writes nothing
  python3 tools/plexsync.py --apply --limit 250   # sync, capped per run
```

Run it on a timer (every ~15 min); writes are idempotent so it converges and then
does nothing until someone watches something new. Items a server doesn't have are
skipped. (Currently two-server oriented; extend the `plan()` fold for N.)

