"""ParcelDeck — self-hosted universal package tracker.

Backed by the 17TRACK Tracking API (v2.2), which covers 3,000+ carriers
including AliExpress / Cainiao, China Post, Yanwen, 4PX, USPS, UPS, FedEx,
DHL, Amazon, OnTrac, LaserShip, Canada Post, Royal Mail, etc.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from html import escape as html_escape
from urllib.parse import parse_qsl, quote
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- config
API_KEY = os.getenv("TRACK17_API_KEY", "").strip()
API_BASE = os.getenv("TRACK17_API_BASE", "https://api.17track.net/track/v2.2").rstrip("/")
CARRIER_LIST_URL = os.getenv(
    "TRACK17_CARRIER_LIST_URL",
    "https://res.17track.net/asset/carrier/info/apicarrier.all.json",
)
DB_PATH = os.getenv("DB_PATH", "/data/parceldeck.db")
POLL_MINUTES = max(5, int(os.getenv("POLL_MINUTES", "60")))
APP_USERNAME = os.getenv("APP_USERNAME", "admin").strip() or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_DAYS = max(1, int(os.getenv("SESSION_DAYS", "30")))
MAX_FAILS, LOCK_SECONDS = 5, 15 * 60
APPRISE_URLS = [u for u in re.split(r"[,\s]+", os.getenv("APPRISE_URLS", "")) if u]
NOTIFY_ON = {
    s.strip()
    for s in os.getenv(
        "NOTIFY_ON",
        "InTransit,AvailableForPickup,OutForDelivery,Delivered,DeliveryFailure,Exception",
    ).split(",")
    if s.strip()
}
WEBHOOK_VERIFY = os.getenv("WEBHOOK_VERIFY", "true").lower() in ("1", "true", "yes")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# Gmail / IMAP import
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = re.sub(r"\s+", "", os.getenv("GMAIL_APP_PASSWORD", ""))
IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
EMAIL_POLL_MINUTES = max(5, int(os.getenv("EMAIL_POLL_MINUTES", "15")))
EMAIL_LOOKBACK_DAYS = max(1, int(os.getenv("EMAIL_LOOKBACK_DAYS", "7")))
# auto = add confident matches, queue the rest for review; review = queue everything; off = disabled
EMAIL_MODE = os.getenv("EMAIL_MODE", "auto").lower()
EMAIL_ENABLED = bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD) and EMAIL_MODE != "off"
EMAIL_STATE = {"last_scan": "", "last_error": "", "last_checked": 0, "last_added": 0, "running": False}

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
log = logging.getLogger("parceldeck")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

STATUS_LABELS = {
    "NotFound": "Not found yet",
    "InfoReceived": "Label created",
    "InTransit": "In transit",
    "Expired": "Expired",
    "AvailableForPickup": "Ready for pickup",
    "OutForDelivery": "Out for delivery",
    "DeliveryFailure": "Delivery failed",
    "Delivered": "Delivered",
    "Exception": "Exception",
    "Pending": "Pending",
}
FINAL_STATUSES = {"Delivered", "Expired"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- database
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL UNIQUE,
                label TEXT DEFAULT '',
                carrier_code INTEGER,
                carrier_name TEXT DEFAULT '',
                status TEXT DEFAULT 'Pending',
                sub_status TEXT DEFAULT '',
                last_event TEXT DEFAULT '',
                last_location TEXT DEFAULT '',
                last_event_time TEXT DEFAULT '',
                eta_from TEXT DEFAULT '',
                eta_to TEXT DEFAULT '',
                events_json TEXT DEFAULT '[]',
                providers_json TEXT DEFAULT '[]',
                registered INTEGER DEFAULT 0,
                register_error TEXT DEFAULT '',
                archived INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT,
                last_checked TEXT DEFAULT ''
            )"""
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(packages)")}
        if "source" not in cols:
            c.execute("ALTER TABLE packages ADD COLUMN source TEXT DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS email_seen (key TEXT PRIMARY KEY, seen_at TEXT)")
        c.execute(
            """CREATE TABLE IF NOT EXISTS suggestions (
                number TEXT PRIMARY KEY, label TEXT, sender TEXT, subject TEXT, email_date TEXT,
                confidence TEXT, status TEXT DEFAULT 'pending', created_at TEXT)"""
        )


def row_to_dict(r: sqlite3.Row, full: bool = False) -> dict:
    d = dict(r)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    d["events"] = json.loads(d.pop("events_json") or "[]")
    d["providers"] = json.loads(d.pop("providers_json") or "[]")
    if not full:
        d["event_count"] = len(d["events"])
        d["events"] = d["events"][:1]
    return d


# ---------------------------------------------------------------- 17TRACK client
class Track17Error(Exception):
    pass


async def t17(path: str, payload) -> dict:
    if not API_KEY:
        raise Track17Error("TRACK17_API_KEY is not set")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{API_BASE}/{path}",
            headers={"17token": API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise Track17Error(f"17TRACK HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    if data.get("code") not in (0, None):
        raise Track17Error(f"17TRACK error {data.get('code')}: {json.dumps(data.get('data'))[:300]}")
    return data.get("data") or {}


def _rejection_msg(rej: dict) -> str:
    err = rej.get("error") or {}
    return f"{err.get('message') or 'rejected'} ({err.get('code')})"


def _already_registered(rej: dict) -> bool:
    err = rej.get("error") or {}
    return err.get("code") == -18019901 or "already" in str(err.get("message", "")).lower()


# ---------------------------------------------------------------- carriers
CARRIERS: dict[int, str] = {}


async def load_carriers():
    global CARRIERS
    cache = os.path.join(os.path.dirname(DB_PATH) or ".", "carriers.json")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(CARRIER_LIST_URL)
            r.raise_for_status()
            items = r.json()
        with open(cache, "w") as f:
            json.dump(items, f)
    except Exception as e:  # fall back to cache
        log.warning("Could not download carrier list (%s); using cache if present", e)
        if not os.path.exists(cache):
            return
        with open(cache) as f:
            items = json.load(f)
    out = {}
    for it in items:
        key = it.get("key") or it.get("code")
        name = it.get("_name") or it.get("name") or ""
        if key is not None and name:
            out[int(key)] = name
    CARRIERS = out
    log.info("Loaded %d carriers", len(CARRIERS))


# ---------------------------------------------------------------- tracking parse / store
def parse_track(item: dict) -> dict:
    """Turn a 17TRACK accepted item / webhook data into our columns."""
    ti = item.get("track_info") or {}
    latest = ti.get("latest_status") or {}
    ev = ti.get("latest_event") or {}
    eta = ((ti.get("time_metrics") or {}).get("estimated_delivery_date")) or {}
    providers_out, events = [], []
    for p in ((ti.get("tracking") or {}).get("providers") or []):
        prov = p.get("provider") or {}
        pname = prov.get("name") or prov.get("alias") or CARRIERS.get(prov.get("key") or 0, "")
        providers_out.append({"key": prov.get("key"), "name": pname, "homepage": prov.get("homepage"),
                              "tel": prov.get("tel"), "country": prov.get("country")})
        for e in p.get("events") or []:
            events.append({
                "time": e.get("time_iso") or e.get("time_utc") or "",
                "time_utc": e.get("time_utc") or "",
                "description": e.get("description") or "",
                "location": e.get("location") or "",
                "stage": e.get("stage") or "",
                "provider": pname,
            })
    events.sort(key=lambda e: e["time_utc"] or e["time"], reverse=True)
    carrier_code = item.get("carrier")
    names = [p["name"] for p in reversed(providers_out) if p["name"]]  # first-mile -> last-mile
    carrier_name = " → ".join(dict.fromkeys(names)) or CARRIERS.get(carrier_code or 0, "")
    return {
        "carrier_code": carrier_code,
        "carrier_name": carrier_name,
        "status": latest.get("status") or "NotFound",
        "sub_status": latest.get("sub_status") or "",
        "last_event": ev.get("description") or (events[0]["description"] if events else ""),
        "last_location": ev.get("location") or (events[0]["location"] if events else ""),
        "last_event_time": ev.get("time_iso") or (events[0]["time"] if events else ""),
        "eta_from": eta.get("from") or "",
        "eta_to": eta.get("to") or "",
        "events_json": json.dumps(events),
        "providers_json": json.dumps(providers_out),
    }


async def store_track(item: dict):
    number = item.get("number")
    if not number:
        return
    parsed = parse_track(item)
    with db() as c:
        row = c.execute("SELECT * FROM packages WHERE number=?", (number,)).fetchone()
        if not row:
            return
        old_status = row["status"]
        old_count = len(json.loads(row["events_json"] or "[]"))
        if not json.loads(parsed["events_json"]) and old_count:
            # Partial payload (no event history) — keep what we already have.
            for k in ("events_json", "providers_json", "carrier_name"):
                parsed[k] = row[k]
        sets = ", ".join(f"{k}=?" for k in parsed)
        c.execute(
            f"UPDATE packages SET {sets}, registered=1, register_error='', last_checked=?, updated_at=? WHERE number=?",
            (*parsed.values(), now_iso(), now_iso(), number),
        )
    new_count = len(json.loads(parsed["events_json"]))
    changed = parsed["status"] != old_status and parsed["status"] in NOTIFY_ON
    # On the very first fetch of a new package, only alert for "action" statuses, not routine "In transit".
    first_fetch_noise = old_status == "Pending" and parsed["status"] in ("InTransit", "InfoReceived", "NotFound")
    if changed and not first_fetch_noise:
        name = row["label"] or number
        body = f"{STATUS_LABELS.get(parsed['status'], parsed['status'])}: {parsed['last_event']}"
        if parsed["last_location"]:
            body += f" — {parsed['last_location']}"
        await notify(f"📦 {name}", body)
    elif new_count > old_count:
        log.info("%s: %d new event(s)", number, new_count - old_count)


async def register_numbers(numbers: list[tuple[str, Optional[int]]]):
    """Register with 17TRACK (each new number uses 1 quota)."""
    for i in range(0, len(numbers), 40):
        batch = numbers[i : i + 40]
        payload = [{"number": n, **({"carrier": c} if c else {})} for n, c in batch]
        try:
            data = await t17("register", payload)
        except Track17Error as e:
            with db() as c:
                for n, _ in batch:
                    c.execute("UPDATE packages SET register_error=? WHERE number=?", (str(e), n))
            continue
        with db() as c:
            for a in data.get("accepted") or []:
                c.execute(
                    "UPDATE packages SET registered=1, register_error='', carrier_code=COALESCE(?, carrier_code) WHERE number=?",
                    (a.get("carrier"), a.get("number")),
                )
            for rj in data.get("rejected") or []:
                if _already_registered(rj):
                    c.execute("UPDATE packages SET registered=1, register_error='' WHERE number=?", (rj.get("number"),))
                else:
                    c.execute("UPDATE packages SET register_error=? WHERE number=?", (_rejection_msg(rj), rj.get("number")))


async def refresh_numbers(numbers: list[str]):
    """Pull latest info (does not consume quota)."""
    for i in range(0, len(numbers), 40):
        batch = numbers[i : i + 40]
        try:
            data = await t17("gettrackinfo", [{"number": n} for n in batch])
        except Track17Error as e:
            log.warning("refresh failed: %s", e)
            continue
        for item in data.get("accepted") or []:
            await store_track(item)
        with db() as c:
            for rj in data.get("rejected") or []:
                c.execute("UPDATE packages SET last_checked=? WHERE number=?", (now_iso(), rj.get("number")))


async def poll_once():
    with db() as c:
        rows = c.execute(
            "SELECT number, registered FROM packages WHERE archived=0 AND status NOT IN ('Delivered','Expired')"
        ).fetchall()
    unreg = [(r["number"], None) for r in rows if not r["registered"]]
    if unreg:
        await register_numbers(unreg)
    nums = [r["number"] for r in rows]
    if nums:
        await refresh_numbers(nums)
        log.info("Polled %d package(s)", len(nums))


async def poller():
    await asyncio.sleep(5)
    while True:
        try:
            await poll_once()
        except Exception:
            log.exception("poll error")
        await asyncio.sleep(POLL_MINUTES * 60)


# ---------------------------------------------------------------- notifications
async def notify(title: str, body: str) -> bool:
    if not APPRISE_URLS:
        return False
    def _send():
        import apprise
        a = apprise.Apprise()
        for u in APPRISE_URLS:
            a.add(u)
        return a.notify(title=title, body=body)
    try:
        return await asyncio.to_thread(_send)
    except Exception:
        log.exception("notify failed")
        return False


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if APP_PASSWORD:
        _secret()  # create persistent session key on first run
        log.info("Login enabled for user '%s'", APP_USERNAME)
    else:
        log.warning("APP_PASSWORD is empty — ParcelDeck is NOT password protected")
    asyncio.create_task(load_carriers())
    tasks = [asyncio.create_task(poller())]
    if EMAIL_ENABLED:
        tasks.append(asyncio.create_task(email_poller()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ParcelDeck", lifespan=lifespan)


# ---------------------------------------------------------------- auth
AUTH_ENABLED = bool(APP_PASSWORD)
COOKIE = "pd_session"
OPEN_PATHS = ("/login", "/logout", "/api/webhook/", "/healthz", "/manifest.webmanifest", "/icon.svg")
FAILS: dict[str, list[float]] = {}  # ip -> failure timestamps


def _secret() -> bytes:
    if os.getenv("SESSION_SECRET"):
        return os.environ["SESSION_SECRET"].encode()
    path = os.path.join(os.path.dirname(DB_PATH) or ".", ".session_secret")
    try:
        with open(path) as f:
            return f.read().strip().encode()
    except FileNotFoundError:
        s = secrets.token_hex(32)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(s)
        os.chmod(path, 0o600)
        return s.encode()


def _sign(user: str, exp: int) -> str:
    # Password fingerprint in the signature: changing APP_PASSWORD logs out every session.
    pw_fp = hashlib.sha256(APP_PASSWORD.encode()).hexdigest()[:16]
    return hmac.new(_secret(), f"{user}|{exp}|{pw_fp}".encode(), hashlib.sha256).hexdigest()


def make_session(user: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{user}|{exp}|{_sign(user, exp)}"


def valid_session(tok: str) -> bool:
    try:
        user, exp, sig = tok.rsplit("|", 2)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(user, int(exp)))
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _locked(ip: str) -> int:
    """Seconds remaining in lockout (5 failures within 15 min → 15 min lock)."""
    now = time.time()
    recent = [t for t in FAILS.get(ip, []) if now - t < LOCK_SECONDS]
    FAILS[ip] = recent
    return int(LOCK_SECONDS - (now - recent[-MAX_FAILS])) if len(recent) >= MAX_FAILS else 0


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    p = request.url.path
    if AUTH_ENABLED and not p.startswith(OPEN_PATHS) and not valid_session(request.cookies.get(COOKIE, "")):
        if p.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        nxt = quote(p + (("?" + request.url.query) if request.url.query else ""), safe="")
        return RedirectResponse(f"/login?next={nxt}", status_code=303)
    return await call_next(request)


def _safe_next(n: str) -> str:
    return n if n.startswith("/") and not n.startswith("//") else "/"


@app.get("/login")
async def login_page(request: Request, next: str = "/", error: str = ""):
    if not AUTH_ENABLED or valid_session(request.cookies.get(COOKIE, "")):
        return RedirectResponse(_safe_next(next), status_code=303)
    with open(os.path.join(STATIC_DIR, "login.html")) as f:
        page = f.read()
    msg = {"bad": "Wrong username or password.",
           "locked": "Too many attempts — try again in a few minutes."}.get(error, "")
    page = page.replace("{{NEXT}}", html_escape(_safe_next(next))).replace("{{ERROR}}", html_escape(msg))
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login_submit(request: Request):
    form = dict(parse_qsl((await request.body()).decode(errors="replace")))
    nxt = _safe_next(form.get("next", "/"))
    ip = _client_ip(request)
    if _locked(ip):
        log.warning("login locked for %s", ip)
        return RedirectResponse(f"/login?error=locked&next={quote(nxt, safe='')}", status_code=303)
    user_ok = hmac.compare_digest(form.get("username", "").strip().lower(), APP_USERNAME.lower())
    pw_ok = hmac.compare_digest(form.get("password", ""), APP_PASSWORD)
    if not (user_ok and pw_ok):
        FAILS.setdefault(ip, []).append(time.time())
        log.warning("failed login from %s", ip)
        await asyncio.sleep(1)  # slow down guessing
        return RedirectResponse(f"/login?error=bad&next={quote(nxt, safe='')}", status_code=303)
    FAILS.pop(ip, None)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, make_session(APP_USERNAME), httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", max_age=SESSION_DAYS * 86400)
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/config")
async def config():
    return {
        "api_key_set": bool(API_KEY),
        "notifications": len(APPRISE_URLS),
        "poll_minutes": POLL_MINUTES,
        "webhook_url": (PUBLIC_URL + "/api/webhook/17track") if PUBLIC_URL else "",
        "auth": AUTH_ENABLED,
        "user": APP_USERNAME if AUTH_ENABLED else "",
        "email": {
            "enabled": EMAIL_ENABLED, "address": GMAIL_ADDRESS, "mode": EMAIL_MODE,
            "every": EMAIL_POLL_MINUTES, **{k: v for k, v in EMAIL_STATE.items()},
        },
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}


class AddIn(BaseModel):
    number: Optional[str] = None
    numbers: Optional[str] = None  # bulk: newline / comma separated, optional "NUMBER label"
    label: Optional[str] = ""
    carrier: Optional[int] = None


NUM_RE = re.compile(r"^[A-Za-z0-9\-]{6,40}$")


async def add_entries(entries: list[tuple[str, str]], carrier: Optional[int] = None, source: str = "manual",
                      unarchive: bool = True) -> dict:
    added, skipped, invalid = [], [], []
    with db() as c:
        for raw, label in entries:
            n = re.sub(r"\s+", "", raw).upper()
            if not NUM_RE.match(n):
                invalid.append(raw)
                continue
            try:
                c.execute(
                    "INSERT INTO packages (number,label,carrier_code,source,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (n, label, carrier, source, now_iso(), now_iso()),
                )
                added.append(n)
            except sqlite3.IntegrityError:
                if unarchive:
                    c.execute("UPDATE packages SET archived=0 WHERE number=?", (n,))
                skipped.append(n)
            c.execute("UPDATE suggestions SET status='added' WHERE number=?", (n,))
    if added:
        await register_numbers([(n, carrier) for n in added])
        await asyncio.sleep(1.5)  # give 17TRACK a moment before first pull
        await refresh_numbers(added)
    return {"added": added, "existing": skipped, "invalid": invalid}


@app.post("/api/packages")
async def add_packages(body: AddIn):
    entries: list[tuple[str, str]] = []
    if body.number:
        entries.append((body.number, body.label or ""))
    if body.numbers:
        for line in re.split(r"[\n;]+", body.numbers):
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"[\s,\t]+", line, maxsplit=1)
            entries.append((parts[0], parts[1].strip() if len(parts) > 1 else ""))
    return await add_entries(entries, body.carrier)


# ---------------------------------------------------------------- Gmail import
def _email_label(sender: str, subject: str) -> str:
    s = re.sub(r"(?i)^(re|fwd?):\s*", "", subject or "").strip()
    s = re.sub(r"\s+", " ", s)
    return (f"{sender}: {s}" if sender and sender.lower() not in s.lower() else s or sender)[:90]


async def email_scan_once() -> dict:
    from . import gmail_scan
    if not EMAIL_ENABLED:
        return {"error": "Gmail import is not configured"}
    if EMAIL_STATE["running"]:
        return {"error": "scan already running"}
    EMAIL_STATE["running"] = True
    try:
        with db() as c:
            seen = {r[0] for r in c.execute("SELECT key FROM email_seen")}
            known = {r[0] for r in c.execute("SELECT number FROM packages")}
            known |= {r[0] for r in c.execute("SELECT number FROM suggestions")}
        res = await asyncio.to_thread(
            gmail_scan.scan, IMAP_HOST, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, EMAIL_LOOKBACK_DAYS, seen
        )
        to_add: list[tuple[str, str]] = []
        queued = 0
        with db() as c:
            for m in res["messages"]:
                c.execute("INSERT OR IGNORE INTO email_seen VALUES (?,?)", (m["key"], now_iso()))
                for num, conf in (m.get("numbers") or {}).items():
                    if num in known:
                        continue
                    known.add(num)
                    label = _email_label(m.get("sender", ""), m.get("subject", ""))
                    if EMAIL_MODE == "auto" and conf == "high":
                        to_add.append((num, label))
                        status = "added"
                    else:
                        status = "pending"
                        queued += 1
                    c.execute(
                        "INSERT OR IGNORE INTO suggestions VALUES (?,?,?,?,?,?,?,?)",
                        (num, label, m.get("sender", ""), m.get("subject", ""), m.get("date", ""), conf, status, now_iso()),
                    )
        added = (await add_entries(to_add, source="email"))["added"] if to_add else []
        if added:
            await notify("📦 New packages from email", f"Now tracking {len(added)}: " + ", ".join(l for n, l in to_add if n in added)[:300])
        EMAIL_STATE.update(last_scan=now_iso(), last_error="", last_checked=res["checked"], last_added=len(added))
        log.info("Email scan: %d new message(s), %d added, %d queued for review", res["checked"], len(added), queued)
        return {"checked": res["checked"], "added": added, "queued": queued}
    except Exception as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "Invalid credentials" in msg:
            msg = "Gmail login failed — check GMAIL_ADDRESS and the 16-character app password"
        EMAIL_STATE.update(last_scan=now_iso(), last_error=msg)
        log.warning("Email scan failed: %s", e)
        return {"error": msg}
    finally:
        EMAIL_STATE["running"] = False


async def email_poller():
    await asyncio.sleep(20)
    while True:
        await email_scan_once()
        await asyncio.sleep(EMAIL_POLL_MINUTES * 60)


@app.post("/api/email/scan")
async def email_scan_now():
    r = await email_scan_once()
    if "error" in r:
        raise HTTPException(400, r["error"])
    return r


@app.get("/api/suggestions")
async def suggestions():
    with db() as c:
        rows = c.execute("SELECT * FROM suggestions WHERE status='pending' ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/suggestions/{number}/add")
async def suggestion_add(number: str):
    with db() as c:
        r = c.execute("SELECT * FROM suggestions WHERE number=?", (number,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return await add_entries([(r["number"], r["label"])], source="email")


@app.post("/api/suggestions/{number}/dismiss")
async def suggestion_dismiss(number: str):
    with db() as c:
        c.execute("UPDATE suggestions SET status='dismissed' WHERE number=?", (number,))
    return {"ok": True}


@app.get("/api/packages")
async def list_packages(view: str = "active"):
    where = {
        "active": "archived=0 AND status NOT IN ('Delivered')",
        "delivered": "archived=0 AND status='Delivered'",
        "archived": "archived=1",
        "all": "1=1",
    }.get(view, "archived=0")
    with db() as c:
        rows = c.execute(f"SELECT * FROM packages WHERE {where} ORDER BY COALESCE(NULLIF(last_event_time,''), created_at) DESC").fetchall()
        counts = dict(
            active=c.execute("SELECT COUNT(*) FROM packages WHERE archived=0 AND status!='Delivered'").fetchone()[0],
            delivered=c.execute("SELECT COUNT(*) FROM packages WHERE archived=0 AND status='Delivered'").fetchone()[0],
            archived=c.execute("SELECT COUNT(*) FROM packages WHERE archived=1").fetchone()[0],
        )
    return {"packages": [row_to_dict(r) for r in rows], "counts": counts}


def _get(pid: int) -> sqlite3.Row:
    with db() as c:
        r = c.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    return r


@app.get("/api/packages/{pid}")
async def get_package(pid: int):
    return row_to_dict(_get(pid), full=True)


class PatchIn(BaseModel):
    label: Optional[str] = None
    archived: Optional[bool] = None
    carrier: Optional[int] = None


@app.patch("/api/packages/{pid}")
async def patch_package(pid: int, body: PatchIn):
    r = _get(pid)
    with db() as c:
        if body.label is not None:
            c.execute("UPDATE packages SET label=?, updated_at=? WHERE id=?", (body.label, now_iso(), pid))
        if body.archived is not None:
            c.execute("UPDATE packages SET archived=?, updated_at=? WHERE id=?", (int(body.archived), now_iso(), pid))
    if body.carrier:
        if r["registered"]:
            await t17("changecarrier", [{"number": r["number"], "carrier_old": r["carrier_code"], "carrier_new": body.carrier}])
        with db() as c:
            c.execute("UPDATE packages SET carrier_code=? WHERE id=?", (body.carrier, pid))
        if not r["registered"]:
            await register_numbers([(r["number"], body.carrier)])
        await asyncio.sleep(1.5)
        await refresh_numbers([r["number"]])
    return row_to_dict(_get(pid), full=True)


@app.post("/api/packages/{pid}/refresh")
async def refresh_package(pid: int):
    r = _get(pid)
    if not r["registered"]:
        await register_numbers([(r["number"], r["carrier_code"])])
    elif r["status"] in FINAL_STATUSES or r["status"] == "NotFound":
        try:  # restart tracking if 17TRACK stopped it
            await t17("retrack", [{"number": r["number"]}])
        except Track17Error:
            pass
    await refresh_numbers([r["number"]])
    return row_to_dict(_get(pid), full=True)


@app.delete("/api/packages/{pid}")
async def delete_package(pid: int):
    r = _get(pid)
    if r["registered"]:
        try:
            await t17("deletetrack", [{"number": r["number"]}])
        except Track17Error as e:
            log.warning("deletetrack: %s", e)
    with db() as c:
        c.execute("DELETE FROM packages WHERE id=?", (pid,))
    return {"ok": True}


@app.post("/api/refresh-all")
async def refresh_all():
    await poll_once()
    return {"ok": True}


@app.get("/api/quota")
async def quota():
    try:
        return await t17("getquota", {})
    except Track17Error as e:
        return {"error": str(e)}


@app.get("/api/carriers")
async def carriers(q: str = "", limit: int = 30):
    ql = q.lower().strip()
    res = [{"code": k, "name": v} for k, v in CARRIERS.items() if not ql or ql in v.lower() or ql == str(k)]
    res.sort(key=lambda x: (not x["name"].lower().startswith(ql), len(x["name"])))
    return res[:limit]


@app.post("/api/notify/test")
async def notify_test():
    if not APPRISE_URLS:
        raise HTTPException(400, "APPRISE_URLS is not set")
    ok = await notify("📦 ParcelDeck", "Test notification — notifications are working.")
    return {"ok": ok}


@app.post("/api/webhook/17track")
async def webhook(request: Request):
    raw = await request.body()
    if WEBHOOK_VERIFY:
        sign = request.headers.get("sign", "")
        expected = hashlib.sha256((raw.decode("utf-8") + "/" + API_KEY).encode()).hexdigest()
        if not hmac.compare_digest(sign.lower(), expected):
            log.warning("webhook signature mismatch")
            raise HTTPException(401, "bad signature")
    payload = json.loads(raw or b"{}")
    event = payload.get("event")
    data = payload.get("data") or {}
    if event == "TRACKING_UPDATED" and data:
        await store_track(data)
    log.info("webhook %s %s", event, data.get("number"))
    return {"ok": True}


# ---------------------------------------------------------------- static UI
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({
        "name": "ParcelDeck", "short_name": "Parcels", "start_url": "/", "display": "standalone",
        "background_color": "#0f1115", "theme_color": "#0f1115",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }, media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon():
    return FileResponse(os.path.join(STATIC_DIR, "icon.svg"), media_type="image/svg+xml")
