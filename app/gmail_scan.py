"""Gmail (IMAP) scanner: finds tracking numbers in shipping emails.

Read-only: mailbox is opened with readonly=True and messages are fetched with
BODY.PEEK[], so nothing is marked as read or modified.
"""
import email
import email.header
import email.utils
import html
import imaplib
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, unquote, urlparse

# ------------------------------------------------------------------ validators
def usps_ok(n: str) -> bool:
    """USPS IMpb mod-10 check digit (20–22 digit numbers starting 92–95)."""
    if not n.isdigit() or not 20 <= len(n) <= 22:
        return False
    body, chk = n[:-1], int(n[-1])
    s = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - s % 10) % 10 == chk


def s10_ok(n: str) -> bool:
    """UPU S10 international (e.g. RR123456789CN) check digit."""
    m = re.fullmatch(r"[A-Z]{2}(\d{8})(\d)[A-Z]{2}", n)
    if not m:
        return False
    s = sum(int(d) * w for d, w in zip(m.group(1), (8, 6, 4, 2, 3, 5, 9, 7)))
    c = 11 - s % 11
    c = 0 if c == 10 else 5 if c == 11 else c
    return c == int(m.group(2))


def ups_ok(n: str) -> bool:
    m = re.fullmatch(r"1Z([0-9A-Z]{15})(\d)", n)
    if not m:
        return False
    total = 0
    for i, ch in enumerate(m.group(1)):
        v = int(ch) if ch.isdigit() else (ord(ch) - 63) % 10
        total += v * (2 if i % 2 else 1)
    return (10 - total % 10) % 10 == int(m.group(2))


# ------------------------------------------------------------------ patterns
# (regex, confidence, validator)  — "high" numbers are auto-added, "low" go to review.
STRONG = [
    (r"\b1Z[0-9A-Z]{16}\b", None),                       # UPS
    (r"\b(?:420\d{5})?(9[2-5]\d{18,20})\b", usps_ok),      # USPS (optionally ZIP-routed)
    (r"\b[A-Z]{2}\d{9}[A-Z]{2}\b", s10_ok),                # International postal (China Post, Yanwen, etc.)
    (r"\bLP\d{14}\b", None),                               # AliExpress / Cainiao
    (r"\bYT\d{16}\b", None),                               # YunExpress
    (r"\bTBA\d{12}\b", None),                              # Amazon Logistics
    (r"\b1LS[A-Z0-9]{12,20}\b", None),                     # LaserShip / OnTrac
    (r"\bGM\d{16,20}\b", None),                            # DHL eCommerce
    (r"\bUUS[A-Z0-9]{13,20}\b", None),                     # Cainiao US
    (r"\bCNUS[A-Z0-9]{10,20}\b", None),                    # Cainiao US
]
# A number written right after a "tracking number" label.
LABELED = re.compile(
    r"(?:tracking|track(?:ing)?\s*(?:no\.?|number|num|#|id|code)|waybill|air\s*waybill|AWB|shipment\s*(?:no\.?|number|id))"
    r"\s*(?:number|no\.?|#|id|code)?\s*[:#]?\s*(?:is\s*)?[:#]?\s*([A-Z0-9][A-Z0-9\- ]{7,34}[A-Z0-9])",
    re.I,
)
NUMERIC_CTX = re.compile(r"\b(\d{10}|\d{12}|\d{15}|\d{20})\b")   # FedEx / DHL — only near "track"
TRACK_WORD = re.compile(r"track|waybill|shipment|fedex|dhl", re.I)
NEG_CTX = re.compile(r"order|invoice|phone|tel|call|account|member|confirmation|reference|receipt|zip|sku|item", re.I)

URL_PARAM = re.compile(
    r"^(?:track(?:ing)?(?:_?num(?:ber)?s?|_?id|_?no)?|tracknum(?:bers)?|trknbr|tlabels|nums?|inquirynumber\d?|awb|"
    r"tracking_numbers?|shipmentnumber|mailno(?:list)?|logisticsno|number|id|q|trackingid)$",
    re.I,
)
CARRIER_HOSTS = (
    "ups.com", "usps.com", "fedex.com", "dhl.", "17track", "aftership", "parcelsapp", "cainiao", "aliexpress",
    "ontrac", "lasership", "track.", "tracking.", "narvar", "route.com", "shipup", "parcel", "yunexpress",
    "4px", "yanwen", "canadapost", "royalmail", "amazon", "shipstation", "pirateship", "trackingmore",
)
STOP_WORDS = {"TRACKING", "NUMBER", "SHIPMENT", "AVAILABLE", "PROVIDED", "UNAVAILABLE"}


def _norm(s: str) -> str:
    return re.sub(r"[\s\-]", "", s).upper()


def _looks_like_tracking(n: str) -> bool:
    return 8 <= len(n) <= 35 and sum(c.isdigit() for c in n) >= 6 and n not in STOP_WORDS


def extract(text: str, urls: list[str]) -> dict[str, str]:
    """Return {number: 'high'|'low'}."""
    found: dict[str, str] = {}

    def put(n, conf):
        if found.get(n) != "high":
            found[n] = conf

    up = text.upper()
    for pat, check in STRONG:
        for m in re.finditer(pat, up):
            n = m.group(1) if m.groups() else m.group(0)
            if check is None or check(n):
                put(n, "high")
            elif TRACK_WORD.search(text[max(0, m.start() - 120): m.start()]):
                put(n, "low")

    for m in LABELED.finditer(text):
        n = _norm(m.group(1).split("  ")[0])
        n = re.split(r"[^A-Z0-9]", n)[0]
        if _looks_like_tracking(n):
            put(n, "high" if sum(c.isdigit() for c in n) >= 8 else "low")

    for m in NUMERIC_CTX.finditer(text):
        before = text[max(0, m.start() - 80): m.start()]
        if TRACK_WORD.search(before) and not NEG_CTX.search(before[-30:]):
            put(m.group(1), "low")

    for u in urls:
        try:
            p = urlparse(html.unescape(u))
        except ValueError:
            continue
        host = p.netloc.lower()
        known = any(h in host for h in CARRIER_HOSTS)
        cands = [v for k, v in parse_qsl(p.query) if URL_PARAM.match(k)]
        seg = re.search(r"/(?:track|tracking|trackings|package|parcel)s?/(?:[a-z\-]+/)?([A-Za-z0-9]{8,35})(?:[/?#]|$)", p.path, re.I)
        if seg:
            cands.append(seg.group(1))
        for v in cands:
            for part in re.split(r"[,\s;|]+", unquote(v)):
                n = _norm(part)
                if _looks_like_tracking(n) and re.fullmatch(r"[A-Z0-9]+", n):
                    put(n, "high" if known else "low")
    return found


# ------------------------------------------------------------------ email parsing
def _dec(s) -> str:
    if not s:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(s)))
    except Exception:
        return str(s)


def message_text(msg) -> tuple[str, list[str]]:
    texts, urls = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/html":
            urls += re.findall(r'href\s*=\s*["\']([^"\']+)["\']', body, re.I)
            body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
            body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|td|li|h\d)>", "\n", body)
            body = html.unescape(re.sub(r"<[^>]+>", " ", body))
        urls += re.findall(r"https?://[^\s<>\"')\]]+", body)
        texts.append(body)
    text = re.sub(r"[ \t ​]+", " ", "\n".join(texts))
    return text, urls


SEARCH = ('{tracking shipped shipment "out for delivery" "on its way" "has shipped" dispatched '
          '"delivery update" "track your" "tracking number" waybill} -in:chats -in:spam -in:trash')


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def scan(host: str, user: str, password: str, lookback_days: int, seen: set[str], max_msgs: int = 150) -> dict:
    """Blocking. Returns {'messages': [{key, subject, sender, date, numbers{n:conf}}], 'checked': int}."""
    imap = imaplib.IMAP4_SSL(host, 993, timeout=60)
    try:
        imap.login(user, password)
        mailbox = "INBOX"
        typ, boxes = imap.list()
        for b in boxes or []:
            line = b.decode(errors="replace") if isinstance(b, bytes) else str(b)
            if "\\All" in line:
                mailbox = line.rsplit(' "/" ', 1)[-1].strip()
                break
        if not mailbox.startswith('"'):
            mailbox = _q(mailbox)
        typ, _ = imap.select(mailbox, readonly=True)
        if typ != "OK":
            imap.select("INBOX", readonly=True)
        uidv = (imap.response("UIDVALIDITY")[1] or [b"0"])[0]
        uidv = uidv.decode() if isinstance(uidv, bytes) else str(uidv)

        query = f"newer_than:{lookback_days}d {SEARCH}"
        try:
            typ, data = imap.uid("SEARCH", "X-GM-RAW", _q(query))
        except imaplib.IMAP4.error:  # not Gmail: plain IMAP fallback
            since = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
            typ, data = imap.uid("SEARCH", "SINCE", since)
        uids = (data[0] or b"").split() if data else []
        uids = [u.decode() for u in uids][::-1]  # newest first
        todo = [u for u in uids if f"{uidv}:{u}" not in seen][:max_msgs]

        out = []
        for uid in todo:
            typ, msgdata = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((x[1] for x in msgdata or [] if isinstance(x, tuple)), None)
            key = f"{uidv}:{uid}"
            if not raw:
                out.append({"key": key, "numbers": {}})
                continue
            msg = email.message_from_bytes(raw)
            subject = _dec(msg.get("Subject"))
            name, addr = email.utils.parseaddr(_dec(msg.get("From")))
            if subject.startswith("📦"):  # our own notifications
                out.append({"key": key, "numbers": {}})
                continue
            text, urls = message_text(msg)
            nums = extract(subject + "\n" + text, urls)
            out.append({
                "key": key, "subject": subject[:200], "sender": name or addr.split("@")[0], "from": addr,
                "date": msg.get("Date", ""), "numbers": nums,
            })
        return {"messages": out, "checked": len(todo), "matched": len(uids)}
    finally:
        try:
            imap.logout()
        except Exception:
            pass
