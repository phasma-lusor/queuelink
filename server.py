#!/usr/bin/env python3
"""
QueueLink — desktop bridge for the League companion app.

Connects to the League client's local API (LCU), relays queue / ready-check /
champ-select state to phones over Socket.IO, and runs the actions they send back.

    pip install -r requirements.txt
    python server.py

Then scan the QR code (or open the printed URL) on a phone on the same Wi-Fi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import secrets
import socket
import sys
import webbrowser
from pathlib import Path

import aiohttp
import socketio
from aiohttp import web

try:  # optional: reads credentials straight from the running client process
    import psutil
except ImportError:
    psutil = None

try:  # draws the pairing QR code
    import qrcode
    import qrcode.image.svg
except ImportError:
    qrcode = None

FROZEN = getattr(sys, "frozen", False)  # True when running as the packaged .exe
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
INDEX_FILE = BUNDLE_DIR / "index.html"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / "QueueLink"
PORT = int(os.environ.get("QUEUELINK_PORT", "8080"))


def load_pin() -> str:
    """Keep the same PIN across launches so phones only pair once."""
    if os.environ.get("QUEUELINK_PIN"):
        return os.environ["QUEUELINK_PIN"]
    cfg = CONFIG_DIR / "config.json"
    try:
        pin = str(json.loads(cfg.read_text())["pin"])
        if pin.isdigit() and len(pin) == 6:
            return pin
    except (OSError, ValueError, KeyError, TypeError):
        pass
    pin = f"{secrets.randbelow(10**6):06d}"
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"pin": pin}))
    except OSError:
        pass
    return pin


PAIR_TOKEN = load_pin()
READY_CHECK_SECONDS = 12   # length of the in-client accept window
AUTO_ACCEPT_DELAY = 0.4    # seconds; avoids racing the client's own popup

# LCU events we relay. Each becomes a WAMP subscription "OnJsonApiEvent_<uri with / -> _>".
SUBSCRIPTIONS = (
    "/lol-gameflow/v1/gameflow-phase",
    "/lol-matchmaking/v1/search",
    "/lol-matchmaking/v1/ready-check",
    "/lol-champ-select/v1/session",
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("queuelink")


# ─── Finding the League client ────────────────────────────────────────────────

def _lockfile_paths() -> list[Path]:
    paths = []
    if os.environ.get("LEAGUE_PATH"):
        paths.append(Path(os.environ["LEAGUE_PATH"]) / "lockfile")
    if platform.system() == "Darwin":
        paths.append(Path("/Applications/League of Legends.app/Contents/LoL/lockfile"))
    else:
        paths += [Path(f"{d}:/Riot Games/League of Legends/lockfile") for d in "CDEFG"]
    return paths


def find_credentials() -> tuple[int, str] | None:
    """Return (port, password) for the running League client, or None."""
    if psutil:
        for proc in psutil.process_iter(["name", "cmdline"]):
            try:
                if not (proc.info["name"] or "").startswith("LeagueClientUx"):
                    continue
                args = dict(
                    a[2:].split("=", 1)
                    for a in (proc.info["cmdline"] or [])
                    if a.startswith("--") and "=" in a
                )
            except (psutil.Error, ValueError):
                continue
            if "app-port" in args and "remoting-auth-token" in args:
                return int(args["app-port"]), args["remoting-auth-token"]

    # Fallback: lockfile is "LeagueClient:<pid>:<port>:<password>:https"
    for path in _lockfile_paths():
        try:
            _name, _pid, port, password, _proto = path.read_text().strip().split(":")
            return int(port), password
        except (OSError, ValueError):
            continue
    return None


# ─── LCU connection (REST + WebSocket) ───────────────────────────────────────

class LCU:
    """Minimal LCU client. Reconnects forever; never blocks the event loop."""

    def __init__(self, on_event, on_connect, on_disconnect):
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self.http: aiohttp.ClientSession | None = None
        self.base = ""
        self.connected = False

    async def request(self, method: str, path: str, body=None):
        """Return (status, parsed_json_or_None). Never raises for network errors."""
        if not self.http:
            return 503, {"message": "League client is not running"}
        kwargs = {"ssl": False, "timeout": aiohttp.ClientTimeout(total=10)}
        if body is not None:
            kwargs["json"] = body
        try:
            async with self.http.request(method, self.base + path, **kwargs) as r:
                raw = await r.read()
                try:
                    data = json.loads(raw) if raw else None
                except ValueError:
                    data = None
                return r.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return 503, {"message": f"League client unreachable: {e}"}

    async def get(self, path: str):
        status, data = await self.request("GET", path)
        return data if status < 400 else None

    async def fetch_raw(self, path: str):
        if not self.http:
            return 503, "text/plain", b""
        async with self.http.get(self.base + path, ssl=False,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
            return r.status, r.content_type, await r.read()

    async def run_forever(self):
        waiting_logged = False
        while True:
            creds = await asyncio.to_thread(find_credentials)
            if creds:
                waiting_logged = False
                try:
                    await self._session(*creds)
                except Exception as e:  # client booting, closed, stale lockfile…
                    log.debug("LCU connection attempt failed: %s", e)
                finally:
                    await self._close()
            elif not waiting_logged:
                log.info("Waiting for the League client to start…")
                waiting_logged = True
            await asyncio.sleep(2)

    async def _session(self, port: int, password: str):
        self.base = f"https://127.0.0.1:{port}"
        self.http = aiohttp.ClientSession(auth=aiohttp.BasicAuth("riot", password))
        async with self.http.ws_connect(f"wss://127.0.0.1:{port}/", ssl=False, heartbeat=30) as ws:
            for uri in SUBSCRIPTIONS:
                await ws.send_json([5, "OnJsonApiEvent" + uri.replace("/", "_")])
            self.connected = True
            log.info("Connected to League client (port %s)", port)
            try:
                await self._on_connect()
            except Exception:
                log.exception("Initial state sync failed")

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                    continue
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                # Event frames look like: [8, "OnJsonApiEvent_…", {"uri", "eventType", "data"}]
                if isinstance(frame, list) and len(frame) == 3 and frame[0] == 8:
                    ev = frame[2] or {}
                    try:
                        await self._on_event(ev.get("uri"), ev.get("eventType"), ev.get("data"))
                    except Exception:
                        log.exception("Error handling %s", ev.get("uri"))

    async def _close(self):
        was_connected = self.connected
        self.connected = False
        if self.http:
            await self.http.close()
            self.http = None
        if was_connected:
            log.info("League client disconnected")
            await self._on_disconnect()


# ─── Bridge: state, derived views, actions ───────────────────────────────────

def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


class Bridge:
    def __init__(self, sio: socketio.AsyncServer):
        self.sio = sio
        self.lcu = LCU(self.on_lcu_event, self.on_lcu_connect, self.on_lcu_disconnect)
        self.auto_accept = False
        self._accepting = False
        self.champions: list[dict] = []
        self.spells: list[dict] = []
        self._reset_game_state()

    def _reset_game_state(self):
        self.phase = "None"
        self.queue = None
        self.ready_check = None
        self.session = None
        self.pickable: set[int] = set()
        self.bannable: set[int] = set()

    async def emit(self, event: str, data):
        await self.sio.emit(event, data)

    def snapshot(self) -> dict:
        return {
            "lcu": self.lcu.connected,
            "autoAccept": self.auto_accept,
            "phase": self.phase,
            "queue": self.queue,
            "readyCheck": self.ready_check,
            "champSelect": self.champ_select_view(),
        }

    # LCU lifecycle ------------------------------------------------------------

    async def on_lcu_connect(self):
        await self.emit("lcu_status", {"connected": True})
        # Pull current state so phones are correct even if we connected mid-lobby.
        for uri in SUBSCRIPTIONS:
            status, data = await self.lcu.request("GET", uri)
            ok = status < 400 and data is not None
            await self.on_lcu_event(uri, "Update" if ok else "Delete", data if ok else None)

    async def on_lcu_disconnect(self):
        self._reset_game_state()
        self.champions, self.spells = [], []  # may change after a patch
        await self.emit("lcu_status", {"connected": False})
        await self.emit("snapshot", self.snapshot())

    async def on_lcu_event(self, uri, event_type, data):
        deleted = event_type == "Delete" or data is None

        if uri == "/lol-gameflow/v1/gameflow-phase":
            self.phase = "None" if deleted else str(data)
            await self.emit("gameflow", {"phase": self.phase})

        elif uri == "/lol-matchmaking/v1/search":
            self.queue = None if deleted else {
                "searchState": data.get("searchState"),
                "timeInQueue": data.get("timeInQueue") or 0,
                "estimatedQueueTime": data.get("estimatedQueueTime"),
            }
            await self.emit("queue", self.queue)

        elif uri == "/lol-matchmaking/v1/ready-check":
            self.ready_check = None if deleted else {
                "state": data.get("state"),
                "playerResponse": data.get("playerResponse"),
                "timer": data.get("timer") or 0,
                "total": READY_CHECK_SECONDS,
            }
            await self.emit("ready_check", self.ready_check)
            self._maybe_auto_accept()

        elif uri == "/lol-champ-select/v1/session":
            if deleted:
                self.session = None
                self.pickable, self.bannable = set(), set()
            else:
                starting = self.session is None
                self.session = data
                if starting or not self.pickable:
                    await self._load_champion_pool()
            await self.emit("champ_select", self.champ_select_view())

    async def _load_champion_pool(self):
        self.pickable = set(await self.lcu.get("/lol-champ-select/v1/pickable-champion-ids") or [])
        self.bannable = set(await self.lcu.get("/lol-champ-select/v1/bannable-champion-ids") or [])

    # Auto-accept (runs on the PC so it works even when the phone is asleep) ---

    def _maybe_auto_accept(self):
        rc = self.ready_check
        if (self.auto_accept and not self._accepting and rc
                and rc["state"] == "InProgress" and rc["playerResponse"] == "None"):
            asyncio.create_task(self._auto_accept())

    async def _auto_accept(self):
        self._accepting = True
        try:
            await asyncio.sleep(AUTO_ACCEPT_DELAY)
            if self.auto_accept:
                result = await self.call("POST", "/lol-matchmaking/v1/ready-check/accept")
                if result["ok"]:
                    log.info("Auto-accepted match")
                    await self.emit("notice", {"message": "Match auto-accepted"})
        finally:
            self._accepting = False

    # Derived champ-select view sent to phones ----------------------------------

    def champ_select_view(self) -> dict | None:
        s = self.session
        if not s:
            return None
        me = s.get("localPlayerCellId")
        actions = [a for turn in (s.get("actions") or []) for a in turn]
        mine = [a for a in actions if a.get("actorCellId") == me and not a.get("completed")]
        current = next((a for a in mine if a.get("isInProgress")), None)
        # If it's not our turn, target our next pick so the phone can pre-hover it.
        target = current or next((a for a in mine if a.get("type") == "pick"), None)

        locked = {a["championId"] for a in actions if a.get("completed") and a.get("championId")}
        bans = {"ally": [], "enemy": []}
        for a in actions:
            if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
                bans["ally" if a.get("isAllyAction") else "enemy"].append(a["championId"])

        def member(m):
            return {
                "cellId": m.get("cellId"),
                "championId": m.get("championId") or 0,
                "pickIntent": m.get("championPickIntent") or 0,
                "position": m.get("assignedPosition") or "",
                "spell1Id": m.get("spell1Id"),
                "spell2Id": m.get("spell2Id"),
                "isMe": m.get("cellId") == me,
            }

        timer = s.get("timer") or {}
        return {
            "phase": timer.get("phase"),
            "timeLeftMs": timer.get("adjustedTimeLeftInPhase") or 0,
            "totalMs": timer.get("totalTimeInPhase") or 0,
            "action": {k: target.get(k) for k in ("id", "type", "championId", "isInProgress")} if target else None,
            "myTeam": [member(m) for m in s.get("myTeam") or []],
            "theirTeam": [member(m) for m in s.get("theirTeam") or []],
            "bans": bans,
            "unavailable": sorted(locked),
            "pickable": sorted(self.pickable) or None,   # None = unknown, allow all
            "bannable": sorted(self.bannable) or None,
        }

    # Static game data -----------------------------------------------------------

    async def load_champions(self) -> list[dict]:
        if not self.champions:
            data = await self.lcu.get("/lol-game-data/assets/v1/champion-summary.json") or []
            self.champions = sorted(
                ({"id": c["id"], "name": c.get("name"), "alias": c.get("alias"),
                  "roles": c.get("roles") or [], "icon": c.get("squarePortraitPath")}
                 for c in data if c.get("id", -1) > 0),
                key=lambda c: c["name"] or "",
            )
        return self.champions

    async def load_spells(self) -> list[dict]:
        if not self.spells:
            data = await self.lcu.get("/lol-game-data/assets/v1/summoner-spells.json") or []
            self.spells = [
                {"id": s["id"], "name": s.get("name"), "icon": s.get("iconPath"),
                 "modes": s.get("gameModes") or []}
                for s in data if s.get("id", 0) > 0 and s.get("gameModes")
            ]
        return self.spells

    async def resolve_champion(self, query: str) -> dict | None:
        q = _norm(query)
        if not q:
            return None
        champs = await self.load_champions()
        for test in (lambda n: n == q, lambda n: n.startswith(q), lambda n: q in n):
            for c in champs:
                if test(_norm(c["name"])) or test(_norm(c["alias"])):
                    return c
        return None

    # Actions ---------------------------------------------------------------------

    async def call(self, method: str, path: str, body=None) -> dict:
        status, data = await self.lcu.request(method, path, body)
        if status < 400:
            return {"ok": True}
        msg = (data or {}).get("message") if isinstance(data, dict) else None
        return _err(msg or f"League client rejected the request ({status})")

    async def search_and_lock_champion(self, payload: dict) -> dict:
        if not self.lcu.connected:
            return _err("League client is not running")
        lock = bool(payload.get("lock", True))
        cid = payload.get("championId")
        if not cid and payload.get("query"):
            champ = await self.resolve_champion(str(payload["query"]))
            if not champ:
                return _err(f'No champion matches "{payload["query"]}"')
            cid = champ["id"]
        if not cid:
            return _err("No champion given")
        cid = int(cid)

        # Re-read the live session so we never act on stale state.
        status, session = await self.lcu.request("GET", "/lol-champ-select/v1/session")
        if status >= 400 or not session:
            return _err("You're not in champ select")
        self.session = session
        view = self.champ_select_view()
        action = view["action"]
        if not action:
            return _err("Nothing left for you to pick or ban")
        if cid in view["unavailable"]:
            return _err("That champion is already picked or banned")
        pool = view["bannable"] if action["type"] == "ban" else view["pickable"]
        if pool and cid not in pool:
            return _err("That champion isn't available to you")

        note = None
        if lock and not action["isInProgress"]:
            lock, note = False, "Not your turn yet, so it's hovered instead"

        result = await self.call(
            "PATCH", f"/lol-champ-select/v1/session/actions/{action['id']}",
            {"championId": cid, "completed": lock},
        )
        if not result["ok"]:
            return result
        return {"ok": True, "championId": cid, "locked": lock, "type": action["type"], "message": note}

    async def set_spells(self, payload: dict) -> dict:
        if not self.session:
            return _err("Spells can only be changed in champ select")
        body = {k: int(payload[k]) for k in ("spell1Id", "spell2Id") if payload.get(k)}
        if not body:
            return _err("No spells given")
        return await self.call("PATCH", "/lol-champ-select/v1/session/my-selection", body)

    async def get_loadout(self) -> dict:
        pages = await self.lcu.get("/lol-perks/v1/pages") or []
        current = await self.lcu.get("/lol-perks/v1/currentpage") or {}
        return {
            "ok": True,
            "pages": [
                {"id": p["id"], "name": p.get("name"), "current": p["id"] == current.get("id"),
                 "primaryStyleId": p.get("primaryStyleId"), "subStyleId": p.get("subStyleId")}
                for p in pages if p.get("isValid", True)
            ],
            "spells": await self.load_spells(),
        }

    async def select_rune_page(self, payload: dict) -> dict:
        if not payload.get("id"):
            return _err("No rune page given")
        return await self.call("PUT", "/lol-perks/v1/currentpage", int(payload["id"]))

    async def set_auto_accept(self, payload: dict) -> dict:
        self.auto_accept = bool(payload.get("enabled"))
        log.info("Auto-accept %s", "ON" if self.auto_accept else "OFF")
        await self.emit("auto_accept", {"enabled": self.auto_accept})
        self._maybe_auto_accept()
        return {"ok": True, "enabled": self.auto_accept}


# ─── Socket.IO ───────────────────────────────────────────────────────────────

sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")
bridge = Bridge(sio)
phones: set[str] = set()


@sio.event
async def connect(sid, environ, auth):
    if not isinstance(auth, dict) or str(auth.get("token", "")) != PAIR_TOKEN:
        log.warning("Rejected a connection with a wrong PIN (%s)", environ.get("REMOTE_ADDR"))
        raise socketio.exceptions.ConnectionRefusedError("bad_token")
    phones.add(sid)
    log.info("Phone connected (%s)", environ.get("REMOTE_ADDR"))


@sio.event
async def disconnect(sid, *args):
    phones.discard(sid)
    log.info("Phone disconnected")


def action(name):
    """Register a Socket.IO event whose return value is sent back as the ack."""
    def deco(fn):
        async def handler(sid, data=None):
            try:
                return await fn(data if isinstance(data, dict) else {})
            except Exception as e:
                log.exception("Action %s failed", name)
                return _err(str(e))
        sio.on(name, handler)
        return fn
    return deco


@action("get_snapshot")
async def _snapshot(d):
    return {"ok": True, **bridge.snapshot()}

@action("accept_match")
async def _accept(d):
    return await bridge.call("POST", "/lol-matchmaking/v1/ready-check/accept")

@action("decline_match")
async def _decline(d):
    return await bridge.call("POST", "/lol-matchmaking/v1/ready-check/decline")

@action("start_queue")
async def _start_queue(d):
    return await bridge.call("POST", "/lol-lobby/v2/lobby/matchmaking/search")

@action("cancel_queue")
async def _cancel_queue(d):
    return await bridge.call("DELETE", "/lol-lobby/v2/lobby/matchmaking/search")

@action("search_and_lock_champion")
async def _pick(d):
    return await bridge.search_and_lock_champion(d)

@action("set_auto_accept")
async def _auto(d):
    return await bridge.set_auto_accept(d)

@action("set_spells")
async def _spells(d):
    return await bridge.set_spells(d)

@action("get_loadout")
async def _loadout(d):
    return await bridge.get_loadout()

@action("select_rune_page")
async def _runes(d):
    return await bridge.select_rune_page(d)


# ─── HTTP: phone UI, static data, icon proxy ─────────────────────────────────

@web.middleware
async def cors(request, handler):
    # Only our own JSON/asset routes; Socket.IO handles its own CORS.
    if not request.path.startswith(("/api/", "/lol-game-data/")):
        return await handler(request)
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _require_pin(request):
    if request.query.get("t") != PAIR_TOKEN:
        raise web.HTTPUnauthorized(text="Wrong or missing PIN")
    if not bridge.lcu.connected:
        raise web.HTTPServiceUnavailable(text="League client is not running")


async def index(request):
    if not INDEX_FILE.exists():
        return web.Response(text="index.html not found next to server.py", status=404)
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


async def api_champions(request):
    _require_pin(request)
    return web.json_response(await bridge.load_champions())


async def api_spells(request):
    _require_pin(request)
    return web.json_response(await bridge.load_spells())


async def game_asset(request):
    """Proxy champion/spell icons from the client so the phone needs no internet for them."""
    if not bridge.lcu.connected:
        raise web.HTTPServiceUnavailable()
    try:
        status, ctype, body = await bridge.lcu.fetch_raw(request.path)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise web.HTTPBadGateway()
    return web.Response(body=body, status=status, content_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})


PAIR_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>QueueLink</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
  font-family:"Segoe UI",system-ui,sans-serif;color:#f6ecff;
  background:radial-gradient(120% 60% at 50% -10%,#4a0d86,transparent 60%),#0b0120}
.card{background:rgba(32,10,72,.7);border:1px solid rgba(255,42,109,.35);border-radius:20px;
  padding:32px;text-align:center;max-width:420px;margin:16px;box-shadow:0 0 40px rgba(255,42,109,.2)}
h1{margin:0 0 6px;letter-spacing:.15em;font-size:22px} h1 b{color:#ff2a6d;text-shadow:0 0 12px #ff2a6d}
.qr{background:#fff;border-radius:14px;padding:10px;width:260px;height:260px;margin:20px auto}
.qr img{width:100%;height:100%;display:block}
.pin{font-size:34px;letter-spacing:.3em;font-weight:700;color:#05d9e8;text-shadow:0 0 10px rgba(5,217,232,.6)}
.url{font-family:Consolas,monospace;font-size:15px;color:#f6ecff;word-break:break-all}
.status{display:flex;gap:10px;justify-content:center;margin-top:18px;font-size:14px;flex-wrap:wrap}
.dot{padding:5px 12px;border-radius:99px;border:1px solid rgba(157,78,221,.4);color:#a898cc}
.dot.on{color:#fff;border-color:#05d9e8;box-shadow:0 0 10px rgba(5,217,232,.4)}
.muted{color:#a898cc;font-size:14px;line-height:1.5}
</style></head><body><div class="card">
<h1>QUEUE<b>LINK</b></h1>
<div class="muted">Scan with your phone's camera. Your phone must be on the same Wi-Fi.</div>
<div class="qr"><img src="/pair/qr.svg" alt="Pairing QR code"></div>
<div class="muted">Or type this into your phone's browser</div>
<div class="url">__BASE__</div>
<div class="muted" style="margin-top:12px">PIN</div><div class="pin">__PIN__</div>
<div class="status"><span id="lcu" class="dot">Waiting for League</span><span id="ph" class="dot">Phones: 0</span></div>
<p class="muted">Leave the QueueLink window open while you play. You can close this tab.</p>
</div><script>
const lcu=document.getElementById('lcu'),ph=document.getElementById('ph');
async function poll(){try{const d=await (await fetch('/pair/status')).json();
lcu.classList.toggle('on',d.lcu);lcu.textContent=d.lcu?'League connected':'Waiting for League';
ph.classList.toggle('on',d.phones>0);ph.textContent='Phones: '+d.phones;}
catch(e){lcu.classList.remove('on');lcu.textContent='QueueLink is closed';}}
poll();setInterval(poll,2000);
</script></body></html>"""


def _local_only(request):
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="The pairing page only opens on the PC running QueueLink")


async def pair_page(request):
    _local_only(request)
    html = PAIR_PAGE.replace("__BASE__", request.app["phone_base"]).replace("__PIN__", PAIR_TOKEN)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def pair_qr(request):
    _local_only(request)
    if not qrcode:
        raise web.HTTPNotFound()
    img = qrcode.make(request.app["phone_url"], image_factory=qrcode.image.svg.SvgPathFillImage, border=2)
    return web.Response(text=img.to_string(encoding="unicode"), content_type="image/svg+xml")


async def pair_status(request):
    _local_only(request)
    return web.json_response({"lcu": bridge.lcu.connected, "phones": len(phones)})


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def main():
    app = web.Application(middlewares=[cors])
    sio.attach(app)
    app.router.add_get("/", index)
    app.router.add_get("/api/champions", api_champions)
    app.router.add_get("/api/spells", api_spells)
    app.router.add_get("/lol-game-data/assets/{tail:.*}", game_asset)
    app.router.add_get("/pair", pair_page)
    app.router.add_get("/pair/qr.svg", pair_qr)
    app.router.add_get("/pair/status", pair_status)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()

    # Use the preferred port, or the next free one if something else has it.
    port = None
    for candidate in range(PORT, PORT + 10):
        try:
            await web.TCPSite(runner, "0.0.0.0", candidate).start()
            port = candidate
            break
        except OSError:
            continue
    if port is None:
        raise RuntimeError(f"Ports {PORT}-{PORT + 9} are all in use")

    ip = lan_ip()
    app["phone_base"] = f"http://{ip}:{port}"
    app["phone_url"] = f"http://{ip}:{port}/?t={PAIR_TOKEN}"

    print("\n  QueueLink is running. Leave this window open while you play.")
    print(f"  Phone address:  {app['phone_base']}")
    print(f"  PIN:            {PAIR_TOKEN}")
    print(f"  Pairing page:   http://localhost:{port}/pair\n")

    if not os.environ.get("QUEUELINK_NO_BROWSER"):
        webbrowser.open(f"http://localhost:{port}/pair")

    try:
        await bridge.lcu.run_forever()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    if FROZEN and sys.platform == "win32":
        os.system("title QueueLink")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nQueueLink stopped.")
    except Exception as e:  # keep the window open so friends can read the error
        log.exception("QueueLink crashed")
        if FROZEN:
            input(f"\nSomething went wrong: {e}\nPress Enter to close…")
        sys.exit(1)
