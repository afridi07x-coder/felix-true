"""
Truecaller Userbot API — Felix
- Clean flat JSON output
- All records returned (multi-row)
- Auto access refresh via Nick_Bypass_Bot
- Multi-account round-robin system
- API Key management with expiry
"""

from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.tl.functions.users import GetFullUserRequest
from telethon.sessions import StringSession
import asyncio, threading, re, os, time, logging, requests
import json, uuid, sqlite3
from datetime import datetime, timedelta
from functools import wraps

# ==================== CREDENTIALS ====================
API_ID         = int(os.environ.get("API_ID", "30507352"))
API_HASH       = os.environ.get("API_HASH", "5cf21ea9611af88bd05c851248d4aca1")
STRING_SESSION = os.environ.get("STRING_SESSION", "1BVtsOIcBux4pCEHF8INCOH2xoqiyvzNngdtXTIHeWnW4uz1fNogbVWSqcSVAFyhsGg-CwK2RS2vdPVTNw0LCCkE51t3rcgUeuCe1CTvNlpFq_6rpOy18uDhh7KSQMBeMdCdCu1oUDLTRpehq0Ao8PyCzgdiv2b5U7dIprEpsQe7FpsN_Y3UKQ986VP4C52Nhc6VWZDX8y9_bxen0Xc6JRtEgCw_y7ImI_sYtA9IY2pK-lSl0sGkioHEraPhJ45a7csDN3-FYj9L8UNaQSXDgyWkDUYnrT4a0jH2KwCSuouT5z7Y8Pw5u7IhY-L3g0pnxuegoxWlfG7YrpwxycM76YqG3gGHFd2M=")
API_KEY        = os.environ.get("API_KEY", "felix")

# ==================== CONFIG ====================
TRUECALLER_BOT = "@Truecaller_redbot"
NICK_BOT       = "@Nick_Bypass_Bot"
ADMIN_KEY      = "felix_admin"   # Master key for /admin endpoints

logging.basicConfig(level=logging.INFO)
logging.getLogger('telethon').setLevel(logging.WARNING)

app    = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
loop   = None

pending = {}
stats   = {"total": 0, "success": 0, "failed": 0}

# ==================== DATABASE ====================

DB_PATH = os.environ.get("DB_PATH", "felix.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    # API Keys table
    c.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            key     TEXT UNIQUE NOT NULL,
            name    TEXT NOT NULL,
            created TEXT NOT NULL,
            expiry  TEXT,
            active  INTEGER DEFAULT 1,
            uses    INTEGER DEFAULT 0
        )
    """)
    # Accounts table
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            api_id         TEXT NOT NULL,
            api_hash       TEXT NOT NULL,
            session_string TEXT NOT NULL,
            active         INTEGER DEFAULT 1,
            created        TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] Initialized")

# ==================== API KEY AUTH ====================

def check_api_key(key):
    """Returns True if key is valid, not expired, and within daily limit."""
    if not key: return False
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key=? AND active=1", (key,)
    ).fetchone()
    conn.close()
    if not row: return False
    # Expiry check
    if row["expiry"]:
        if datetime.now() > datetime.fromisoformat(row["expiry"]):
            return False
    # Daily limit check
    try:
        daily_limit = row["daily_limit"] if "daily_limit" in row.keys() else 0
        if daily_limit > 0:
            today = datetime.now().strftime("%Y-%m-%d")
            last_reset = row["last_reset"] if "last_reset" in row.keys() else None
            daily_uses  = row["daily_uses"]  if "daily_uses"  in row.keys() else 0
            # Reset if new day
            if last_reset != today:
                daily_uses = 0
            if daily_uses >= daily_limit:
                return False  # Daily limit exceeded
            # Increment daily_uses
            conn = get_db()
            conn.execute(
                "UPDATE api_keys SET uses=uses+1, daily_uses=?, last_reset=? WHERE key=?",
                (daily_uses + 1, today, key)
            )
            conn.commit(); conn.close()
            return True
    except Exception:
        pass
    # No daily limit — just increment total uses
    conn = get_db()
    conn.execute("UPDATE api_keys SET uses=uses+1 WHERE key=?", (key,))
    conn.commit(); conn.close()
    return True

def require_key(f):
    """Decorator — validates API key from ?key= param."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.args.get("key", "")
        if not check_api_key(key):
            return jsonify({"success": False, "error": "Invalid or expired API key"}), 401
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    """Decorator — validates admin master key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.args.get("key", "") or request.json.get("key", "") if request.is_json else request.args.get("key", "")
        if key != ADMIN_KEY:
            return jsonify({"success": False, "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return decorated

# ==================== MULTI-ACCOUNT MANAGER ====================

class AccountManager:
    def __init__(self):
        self._clients  = {}   # id -> TelegramClient
        self._rr_index = 0
        self._lock     = threading.Lock()

    def get_active_ids(self):
        conn = get_db()
        rows = conn.execute("SELECT id FROM accounts WHERE active=1").fetchall()
        conn.close()
        return [r["id"] for r in rows]

    def get_client(self, acc_id):
        return self._clients.get(acc_id)

    def set_client(self, acc_id, client):
        self._clients[acc_id] = client

    def remove_client(self, acc_id):
        c = self._clients.pop(acc_id, None)
        return c

    def next_client(self):
        """Round-robin: returns (acc_id, client) of next active connected account."""
        with self._lock:
            active_ids = [
                aid for aid in self.get_active_ids()
                if aid in self._clients and self._clients[aid].is_connected()
            ]
            if not active_ids:
                return None, None
            idx = self._rr_index % len(active_ids)
            self._rr_index = (self._rr_index + 1) % len(active_ids)
            aid = active_ids[idx]
            return aid, self._clients[aid]

acc_manager = AccountManager()

# ==================== UTILS ====================

def clean_num(n):
    s = str(n).strip()
    digits = re.sub(r'[^\d]', '', s)
    if digits:
        if len(digits) == 12 and digits[:2] in ('91', '92'): digits = digits[2:]
        return digits
    return s  # non-numeric query — return as-is

def valid_num(n):
    # No validation — accept anything, detect country best-effort
    c = clean_num(n) if re.search(r'\d', str(n)) else str(n).strip()
    if not c: c = str(n).strip()
    country = "Pakistan" if (len(c) == 11 and c.startswith('03')) else "India"
    return True, c if c else str(n).strip(), country

def find_link(text):
    m = re.search(r'https?://\S+', text or "")
    return m.group(0) if m else None

def btn_link(msg):
    if msg and msg.buttons:
        for row in msg.buttons:
            for b in row:
                if hasattr(b, 'url') and b.url: return b.url
    return None

def extract_field(line, *keywords):
    """
    Strip all emojis/symbols from start of line,
    then extract value after 'keyword:' or 'keyword -'
    """
    # Remove leading emoji/symbols
    clean = re.sub(r'^[\W]+', '', line).strip()
    for kw in keywords:
        m = re.search(rf'{re.escape(kw)}\s*[:\-]\s*(.+)', clean, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            # Remove junk but keep & for circle names like VODA BHR&JHR
            val = re.sub(r'[`\'\"\\]', '', val)          # remove backticks/quotes
            val = re.sub(r'[^\w\s,.\-@/&]', '', val)
            val = re.sub(r'\s+', ' ', val).strip()
            if val: return val
    return None

# ==================== PARSER ====================

def parse_response(text, number):
    if not text:
        return None

    tl = text.lower()

    # Access issues
    if "access has been expired" in tl or "don't have access" in tl:
        return {"_status": "ACCESS_EXPIRED", "link": find_link(text)}
    if "click the button" in tl or "get 1 hour access" in text:
        return {"_status": "ACCESS_NEEDED", "link": find_link(text)}
    if "unlocked 1-hour" in tl or "congrats" in tl:
        return {"_status": "ACCESS_GRANTED"}

    lines = [l.strip() for l in text.split('\n')]

    # ── Step 1: Split into record blocks by "Record N:" ──
    # Find all "Record N:" positions
    record_starts = []
    for i, line in enumerate(lines):
        if re.match(r'^[^\w]*Record\s+\d+\s*:', line, re.IGNORECASE):
            record_starts.append(i)

    records = []

    if record_starts:
        # Parse each block separately
        for idx, start in enumerate(record_starts):
            end = record_starts[idx + 1] if idx + 1 < len(record_starts) else len(lines)
            block = lines[start:end]
            rec = parse_block(block, number)
            if rec:
                records.append(rec)
    else:
        # No "Record N:" separators — treat whole message as one record
        rec = parse_block(lines, number)
        if rec:
            records.append(rec)

    # Get total_results from footer
    total_results = len(records)
    m = re.search(r'Total\s+Results?\s*[:\-]\s*(\d+)', text, re.IGNORECASE)
    if m:
        total_results = int(m.group(1))

    country = "Pakistan" if len(clean_num(number)) == 11 else "India"

    return {
        "_status":       "OK",
        "success":       True,
        "country":       country,
        "number":        number,
        "total_records": len(records),
        "total_results": total_results,
        "records":       records,
        "made_by":       "@felix_bhai"
    }


def parse_block(lines, default_number):
    """Parse a single record block into a dict."""
    rec      = {}
    addr_lines = []
    in_addr  = False

    for line in lines:
        if not line: continue

        # Skip separator lines
        if re.match(r'^[━─\-=\s]+$', line): continue

        # Skip "Record N:" header itself
        if re.match(r'^[^\w]*Record\s+\d+\s*:', line, re.IGNORECASE):
            continue

        # Skip footer/header lines
        ll = line.lower()
        if any(x in ll for x in [
            'search results', 'total records', 'total results',
            'made by', 'india mobile', 'pakistan mobile'
        ]):
            in_addr = False
            continue

        # Skip pure flag emoji lines
        if re.match(r'^[\U0001F1E0-\U0001F1FF\s]+$', line):
            continue

        # ── Field: Number ──
        if re.search(r'number\s*[:\-]', ll) and 'alt' not in ll:
            in_addr = False
            v = extract_field(line, 'Number')
            if v:
                rec['number'] = clean_num(v)
            continue

        # ── Field: Name ──
        if re.search(r'\bname\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'Name')
            if v: rec['name'] = v
            continue

        # ── Field: Father ──
        if re.search(r'father\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'Father')
            if v: rec['father_name'] = v
            continue

        # ── Field: Alt Number ──
        if re.search(r'alt.*number\s*[:\-]', ll) or re.search(r'alt_number\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'Alt Number', 'Alt_Number', 'Alt')
            if v: rec['alt_number'] = clean_num(v)
            continue

        # ── Field: Address (start) ──
        if re.search(r'\baddress\s*[:\-]', ll):
            in_addr = True
            addr_lines = []
            # Value on same line as "Address:"
            v = extract_field(line, 'Address')
            if v: addr_lines.append(v)
            continue


        # ── Field: Mobile (Pakistan uses Mobile: instead of Number:) ──
        if re.search(r'mobile\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'Mobile')
            if v and 'number' not in rec:
                rec['number'] = clean_num(v)
            continue

        # ── Field: CNIC ──
        if re.search(r'cnic\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'CNIC')
            if v: rec['cnic'] = v
            continue

        # ── Field: Circle / Operator ──
        if re.search(r'circle\s*[:\-]', ll) or re.search(r'operator\s*[:\-]', ll):
            if addr_lines:
                rec['address'] = ' '.join(addr_lines)
                addr_lines = []
            in_addr = False
            v = extract_field(line, 'Circle', 'Operator')
            if v: rec['circle'] = v
            continue

        # ── Field: ID ──
        if re.search(r'\bid\s*[:\-]', ll) and 'aid' not in ll:
            if addr_lines:
                rec['address'] = ' '.join(addr_lines)
                addr_lines = []
            in_addr = False
            v = extract_field(line, 'ID')
            if v: rec['id'] = v
            continue

        # ── Field: Email ──
        if re.search(r'email\s*[:\-]', ll):
            in_addr = False
            v = extract_field(line, 'Email')
            if v: rec['email'] = v
            continue

        # ── Address continuation ──
        if in_addr:
            # Strip leading bullet/pin emoji
            cl = re.sub(r'^[-•*📍🏠]\s*', '', line).strip()
            if cl and re.search(r'[a-zA-Z0-9]', cl):
                addr_lines.append(cl)

    # Flush remaining address
    if addr_lines:
        rec['address'] = ' '.join(addr_lines)

    # Must have at least name or id to be valid
    if not (rec.get('name') or rec.get('id') or rec.get('number')):
        return None

    if 'number' not in rec:
        rec['number'] = default_number

    # Return in clean field order
    ordered = {}
    for key in ['number', 'name', 'father_name', 'alt_number', 'cnic', 'address', 'circle', 'id', 'email']:
        if key in rec:
            ordered[key] = rec[key]

    return ordered

# ==================== ACCESS REFRESH ====================

async def refresh_access(link, session_id, orig_number, acc_id=None):
    """
    Flow:
    1. Nick Bot ko original shortener link bhejo
    2. Nick Bot bypassed link deta hai: https://t.me/Truecaller_redbot?start=PAYLOAD
    3. Payload extract karo: PAYLOAD
    4. Truecaller bot ko /start PAYLOAD bhejo (exactly is format mein)
    5. Access milta hai — number dobara bhejo
    """
    print(f"[REFRESH] Starting for {orig_number}, link={link}")
    try:
        # Same account use karo jo original request ne use ki thi
        if acc_id is not None:
            ac = acc_manager.get_client(acc_id)
            if not ac or not ac.is_connected():
                print(f"[REFRESH] Original acc_id={acc_id} not available, using next")
                _, ac = acc_manager.next_client()
        else:
            _, ac = acc_manager.next_client()
        if not ac:
            print("[REFRESH] No client available")
            return False

        # Step 1: Nick Bot ko shortener link bhejo
        sent_ts = int(time.time())
        await ac.send_message(NICK_BOT, link)
        print(f"[REFRESH] Sent to Nick Bot: {link}")
        await asyncio.sleep(5)

        # Step 2: Nick Bot ka response lo — nayi message mein bypass link dhundo
        bypass = None
        for attempt in range(3):
            msgs = await ac.get_messages(NICK_BOT, limit=8)
            for msg in msgs:
                if msg.date and int(msg.date.timestamp()) < sent_ts:
                    continue
                # Text mein https://t.me/... link dhundo
                if msg.text:
                    m = re.search(r'https://t\.me/[^\s\)\]]+', msg.text)
                    if m:
                        bypass = m.group(0).rstrip('.')
                        print(f"[REFRESH] Bypass link found in text: {bypass}")
                        break
                # Button mein dhundo
                bl = btn_link(msg)
                if bl and "t.me" in bl:
                    bypass = bl.rstrip('.')
                    print(f"[REFRESH] Bypass link found in button: {bypass}")
                    break
            if bypass:
                break
            if attempt < 2:
                print(f"[REFRESH] Waiting for Nick Bot response... attempt {attempt+1}")
                await asyncio.sleep(2)

        if not bypass:
            print("[REFRESH] Nick Bot ne bypass link nahi diya")
            return False

        # Step 3: Payload extract karo
        # bypass = https://t.me/Truecaller_redbot?start=291HKO7G4K
        # payload = 291HKO7G4K
        m = re.search(r'[?&]start=([^&\s\)\]]+)', bypass, re.IGNORECASE)
        if not m:
            print(f"[REFRESH] start= payload nahi mila in: {bypass}")
            return False

        # Trailing garbage strip karo — ., *, _, ~, ) etc Telegram markdown chars
        start_payload = re.sub(r'[.*_~)`\'"]+$', '', m.group(1).strip())
        print(f"[REFRESH] Payload: {start_payload}")

        # Step 4: Truecaller bot ko /start PAYLOAD bhejo
        # Format exactly: /start 291HKO7G4K
        start_msg = f"/start {start_payload}"
        await ac.send_message(TRUECALLER_BOT, start_msg)
        print(f"[REFRESH] Sent to Truecaller bot: {start_msg}")

        # Step 5: Access grant hone do — Truecaller responds instantly
        await asyncio.sleep(2)

        # Step 6: Number dobara bhejo
        await ac.send_message(TRUECALLER_BOT, clean_num(orig_number))
        print(f"[REFRESH] Re-sent number: {orig_number}")
        return True

    except Exception as e:
        print(f"[REFRESH ERROR] {e}")
        return False

# ==================== EVENT HANDLER ====================

@events.register(events.NewMessage)
async def on_message(event):
    msg = event.message
    if not msg or not msg.text: return

    sender = await event.get_sender()
    uname  = (getattr(sender, 'username', '') or "").lower()
    if 'truecaller_redbot' not in uname: return

    print(f"[TC] Response received ({len(msg.text)} chars)")

    # Find oldest matching pending request
    matched_id = None
    oldest_ts  = float('inf')
    for rid, req in list(pending.items()):
        if req.get("done"): continue
        age = time.time() - req["ts"]
        if age > 180:
            pending.pop(rid, None)
            continue
        if req["ts"] < oldest_ts:
            oldest_ts  = req["ts"]
            matched_id = rid

    if not matched_id: return

    req    = pending[matched_id]
    result = parse_response(msg.text, req["number"])
    if not result: return

    status = result.get("_status", "OK")

    if status in ("ACCESS_EXPIRED", "ACCESS_NEEDED"):
        # Button se link pehle nikalo (btn_link priority) — text mein link nahi hota
        link = btn_link(msg) or result.get("link")
        if link:
            print(f"[TC] Access issue — link={link} — refreshing via Nick Bot")
            # Timestamp refresh karo taaki 90s timeout na ho during refresh
            pending[matched_id]["ts"] = time.time()
            asyncio.create_task(refresh_access(link, req["session_id"], req["number"], req.get("acc_id")))
        else:
            print(f"[TC] Access issue but no link found in msg or buttons")
        return

    if status == "ACCESS_GRANTED":
        print(f"[TC] Access granted — waiting for retry result")
        # Timestamp refresh karo
        if matched_id in pending:
            pending[matched_id]["ts"] = time.time()
        return

    pending[matched_id]["result"] = result
    pending[matched_id]["done"]   = True
    print(f"[TC] {result.get('total_records', 0)} records found for {req['number']}")

# ==================== FLASK API ====================

def num_lookup():
    number = request.args.get('num') or request.args.get('number', '')
    if not number:
        return jsonify({"success": False, "error": "Missing number"}), 400

    _, num_c, country = valid_num(number)
    if not num_c:
        return jsonify({"success": False, "error": "Empty number"}), 400

    stats["total"] += 1
    session_id = f"s_{int(time.time()*1000)}"
    req_id     = f"{session_id}_{num_c}"

    pending[req_id] = {
        "session_id": session_id,
        "number":     num_c,
        "ts":         time.time(),
        "done":       False,
        "result":     None
    }

    acc_id, acc_client = acc_manager.next_client()
    if not acc_client:
        pending.pop(req_id, None)
        stats["failed"] += 1
        return jsonify({"success": False, "error": "No active Telegram accounts"}), 503

    # Store which account handled this request
    pending[req_id]["acc_id"] = acc_id

    async def _send():
        await acc_client.send_message(TRUECALLER_BOT, num_c)

    try:
        asyncio.run_coroutine_threadsafe(_send(), loop).result(timeout=10)
    except Exception as e:
        pending.pop(req_id, None)
        stats["failed"] += 1
        return jsonify({"success": False, "error": f"Send failed: {e}"}), 500

    # Poll for result — max 90s
    deadline = time.time() + 90
    while time.time() < deadline:
        req = pending.get(req_id, {})
        if req.get("done"):
            result = req["result"]
            pending.pop(req_id, None)

            if result and result.get("success"):
                stats["success"] += 1
                import json as _json
                from flask import Response as _Resp
                data = {
                    "country":       result.get("country", country),
                    "number":        num_c,
                    "total_records": result.get("total_records", 0),
                    "records":       result.get("records", []),
                    "total_results": result.get("total_results", 0),
                    "made_by":       "@felix_bhai"
                }
                return _Resp(_json.dumps(data, ensure_ascii=False), mimetype='application/json')
            else:
                stats["failed"] += 1
                return jsonify({"success": False, "error": result.get("error", "No data")}), 500
        time.sleep(0.3)

    pending.pop(req_id, None)
    stats["failed"] += 1
    return jsonify({"success": False, "error": "Timeout — bot didn't respond in 90s"}), 504

# ==================== TG LOOKUP ====================

FELIX_API     = "https://felixapi.onrender.com/api"
USERID_API    = "https://username-usrid-to-num.onrender.com"
USERID_API_KEY = "c797993aa04e03df3b6d597c001be4f3"
FELIX_API_KEY = "daddyfelix"

def run_async(coro):
    """Run async function from sync Flask context."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

async def _parse_user(entity, fallback_id=None):
    """Parse any user/entity into info dict"""
    name = " ".join(filter(None, [
        getattr(entity, 'first_name', '') or '',
        getattr(entity, 'last_name', '')  or ''
    ])).strip() or getattr(entity, 'title', '') or (f"User {fallback_id}" if fallback_id else "Unknown")
    uname = getattr(entity, 'username', None)
    tg_id = str(entity.id)
    phone = getattr(entity, 'phone', None)
    return {
        "name":         name,
        "username":     f"@{uname}" if uname else None,
        "telegram_id":  tg_id,
        "public_phone": str(phone) if phone else None
    }

async def resolve_username(username):
    """Resolve username → info dict"""
    uname_clean = username.lstrip('@')
    _, ac = acc_manager.next_client()
    if not ac: return None
    try:
        entity = await ac.get_entity(uname_clean)
        info = await _parse_user(entity, uname_clean)
        # Ensure username is set even if entity has different username
        if not info["username"]:
            info["username"] = f"@{uname_clean}"
        return info
    except Exception as e:
        print(f"[TG] resolve_username failed: {e}")
        return None

async def resolve_userid(user_id):
    """Resolve user_id → info dict"""
    uid = int(user_id)
    _, ac = acc_manager.next_client()
    if not ac:
        return {"name": f"User {user_id}", "username": None, "telegram_id": str(uid), "public_phone": None}

    # Try 1: get_entity (fastest)
    try:
        entity = await ac.get_entity(uid)
        info = await _parse_user(entity, uid)
        print(f"[TG] get_entity OK: {info['name']} {info['username']}")
        return info
    except Exception as e:
        print(f"[TG] get_entity failed: {e}")

    # Try 2: GetFullUserRequest
    try:
        full = await ac(GetFullUserRequest(uid))
        u = full.users[0] if full and full.users else None
        if u:
            info = await _parse_user(u, uid)
            print(f"[TG] GetFullUser OK: {info['name']} {info['username']}")
            return info
    except Exception as e:
        print(f"[TG] GetFullUser failed: {e}")

    return {"name": f"User {user_id}", "username": None, "telegram_id": str(uid), "public_phone": None}

def fetch_phone_from_apis(tg_id):
    """Try APIs to get phone. Priority: userid-to-num → felixapi"""
    import requests as _req

    # API 1: username-usrid-to-num (priority)
    try:
        r = _req.get(
            f"{USERID_API}/userid={tg_id}",
            params={"key": USERID_API_KEY},
            timeout=12
        )
        print(f"[TG] userid-api status={r.status_code}")
        if r.status_code == 200:
            d = r.json()
            if d.get("status"):
                for src_key, src_val in d.get("data", {}).items():
                    for rec in src_val.get("records", []):
                        phone = str(rec.get("phone", "")).strip()
                        if phone and phone not in ("None", "", "null"):
                            print(f"[TG] userid-api found: {phone} ({rec.get('country')})")
                            return {
                                "country":      rec.get("country", "Unknown"),
                                "country_code": rec.get("country_code", ""),
                                "phone_number": phone
                            }
    except Exception as e:
        print(f"[TG] userid-api failed: {e}")

    # API 2: felixapi (fallback)
    try:
        r = _req.get(f"{FELIX_API}?key={FELIX_API_KEY}&tg={tg_id}", timeout=10)
        print(f"[TG] felixapi status={r.status_code}")
        if r.status_code == 200:
            d = r.json()
            inner = d.get("result", {}).get("result", {})
            if inner.get("success") and inner.get("number"):
                print(f"[TG] felixapi found: {inner.get('number')}")
                return {
                    "country":      inner.get("country", "Unknown"),
                    "country_code": inner.get("country_code", ""),
                    "phone_number": inner.get("number", "")
                }
    except Exception as e:
        print(f"[TG] felixapi failed: {e}")

    return None

@app.route('/api', methods=['GET'])
def api_tg_check():
    """Single /api entry point — routes to tg_lookup or num_lookup"""
    key = request.args.get('key', '')
    # Accept both hardcoded master key AND DB-managed keys
    if key != API_KEY and not check_api_key(key):
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    tg = request.args.get('tg', '').strip()
    if tg:
        return tg_lookup()
    return num_lookup()

@app.route('/api/tg', methods=['GET'])
def api_tg_direct():
    """Direct TG endpoint: /api/tg?key=felix&tg=userid_or_username"""
    key = request.args.get('key', '')
    if key != API_KEY and not check_api_key(key):
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    return tg_lookup()

def tg_lookup():
    import json as _json
    from flask import Response as _Resp

    tg = request.args.get('tg', '').strip()
    if not tg:
        return jsonify({"success": False, "error": "Missing tg param"}), 400

    # Step 1: Resolve to TG info
    is_username = not tg.lstrip('@').isdigit()

    if is_username:
        tg_info = run_async(resolve_username(tg))
        if not tg_info:
            return jsonify({"success": False, "error": "Could not resolve username"}), 404
    else:
        # For numeric ID — always return something so phone APIs can run
        tg_info = run_async(resolve_userid(tg))
        if not tg_info:
            tg_info = {"name": f"User {tg}", "username": None, "telegram_id": tg}

    tg_id = tg_info["telegram_id"]
    print(f"[TG] Resolved: {tg_info}")

    # Step 2: Fetch phone — API 1 → API 2 → public profile fallback
    location = fetch_phone_from_apis(tg_id)
    if not location:
        pub = tg_info.get("public_phone")
        if pub:
            print(f"[TG] Using public profile phone: {pub}")
            location = {"country": "Unknown", "country_code": "", "phone_number": pub}
        else:
            location = {"country": "Unknown", "country_code": "", "phone_number": "Not found"}

    # Step 3: info_key based on input type
    info_key = "username_info" if is_username else "userid_info"

    # Step 4: Fix public phone — extract country_code if missing
    phone_num = location["phone_number"]
    country_code = location["country_code"]
    country = location["country"]

    # If public phone and no country_code — try to detect from known prefixes
    if phone_num and phone_num != "Not found" and not country_code:
        PHONE_CC = [
            ("+880", "Bangladesh"), ("+977", "Nepal"), ("+94", "Sri Lanka"),
            ("+971", "UAE"), ("+966", "Saudi Arabia"), ("+92", "Pakistan"),
            ("+91", "India"), ("+1", "USA"), ("+44", "UK"),
            ("+98", "Iran"), ("+90", "Turkey"), ("+7", "Russia"),
            ("+86", "China"), ("+81", "Japan"), ("+82", "South Korea"),
            ("+49", "Germany"), ("+33", "France"), ("+39", "Italy"),
            ("+55", "Brazil"), ("+61", "Australia"), ("+40", "Romania"),
        ]
        for cc, cname in PHONE_CC:
            digits = cc.replace("+", "")
            if phone_num.startswith(digits):
                country_code = cc
                if country == "Unknown":
                    country = cname
                # Strip country code prefix from phone number
                phone_num = phone_num[len(digits):]
                break

    # Step 5: Build response
    result = {
        info_key: {
            "name":        tg_info["name"],
            "username":    tg_info.get("username") or "N/A",
            "telegram_id": tg_id
        },
        "location": {
            "country":      country,
            "country_code": country_code,
            "phone_number": phone_num
        },
        "made_by": "@Felix_Bhai"
    }
    return _Resp(_json.dumps(result, ensure_ascii=False), mimetype='application/json')

@app.route('/api/health')
def health():
    active_ids = acc_manager.get_active_ids()
    connected  = [
        aid for aid in active_ids
        if acc_manager.get_client(aid) and acc_manager.get_client(aid).is_connected()
    ]
    return jsonify({
        "status":            "ok",
        "accounts_active":   len(active_ids),
        "accounts_connected": len(connected),
        "pending":           len(pending),
        "stats":             stats
    })

@app.route('/')
def home():
    from flask import Response as _R
    return _R(
        '{"status":true,"name":"FELIX API","version":"2.0","developer":"@felix_bhai","maintenance":true,"maintenance_message":"system"}',
        mimetype='application/json'
    )

# ==================== ADMIN PANEL ====================

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Felix API — Admin</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --pu:#7c5cfc;--pu2:#5b3fd8;--pu-glow:rgba(124,92,252,.25);
    --bg:#0d0d0f;--bg2:#13131a;--bg3:#1a1a24;--bg4:#1f1f2e;
    --brd:#252535;--brd2:#2e2e42;
    --gr:#22c55e;--rd:#ef4444;--yw:#f59e0b;
    --tx:#8888aa;--tx2:#c0c0d8;
  }
  body{background:var(--bg);color:#e2e2f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}

  /* ── HEADER ── */
  header{
    background:linear-gradient(135deg,#110d2e 0%,#0d0d0f 60%);
    border-bottom:1px solid var(--brd);padding:16px 28px;
    display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:99;
    backdrop-filter:blur(10px);
  }
  .logo{width:36px;height:36px;background:linear-gradient(135deg,var(--pu),#a855f7);
    border-radius:10px;display:flex;align-items:center;justify-content:center;
    box-shadow:0 0 16px var(--pu-glow);font-size:1.1rem}
  header h1{font-size:1.2rem;font-weight:700;letter-spacing:.3px;flex:1}
  .hbadge{font-size:.7rem;color:var(--tx);background:var(--bg3);border:1px solid var(--brd2);
    padding:3px 10px;border-radius:20px}
  .pulse{width:8px;height:8px;border-radius:50%;background:#22c55e;
    box-shadow:0 0 0 2px rgba(34,197,94,.3);animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 2px rgba(34,197,94,.3)}50%{box-shadow:0 0 0 5px rgba(34,197,94,.1)}}

  /* ── LAYOUT ── */
  .wrap{max-width:960px;margin:0 auto;padding:24px 16px}

  /* ── STATS ── */
  .stats-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
  .stat{background:var(--bg2);border:1px solid var(--brd);border-radius:14px;padding:18px 14px;
    text-align:center;position:relative;overflow:hidden;transition:border .2s,transform .15s}
  .stat:hover{border-color:var(--brd2);transform:translateY(-2px)}
  .stat::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--pu-glow),transparent);opacity:0;transition:opacity .3s}
  .stat:hover::before{opacity:1}
  .stat .val{font-size:1.8rem;font-weight:800;background:linear-gradient(135deg,#a78bfa,#7c5cfc);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .stat .lbl{font-size:.68rem;color:var(--tx);margin-top:5px;text-transform:uppercase;letter-spacing:1px}
  .stat .ico{font-size:1.4rem;margin-bottom:6px}

  /* ── CARD ── */
  .card{background:var(--bg2);border:1px solid var(--brd);border-radius:16px;
    padding:24px;margin-bottom:20px;transition:border .2s}
  .card:hover{border-color:var(--brd2)}
  .card-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
  .card-hd h2{font-size:.75rem;font-weight:700;letter-spacing:1.8px;color:var(--tx);
    text-transform:uppercase;display:flex;align-items:center;gap:8px}
  .card-hd h2 .dot{width:6px;height:6px;border-radius:50%;background:var(--pu)}

  /* ── FORM ── */
  label{display:block;font-size:.71rem;color:var(--tx);margin-bottom:6px;margin-top:14px;
    text-transform:uppercase;letter-spacing:.9px;font-weight:600}
  label:first-of-type{margin-top:0}
  input,textarea{
    width:100%;background:var(--bg3);border:1px solid var(--brd2);border-radius:10px;
    padding:11px 14px;color:#e2e2f0;font-size:.87rem;outline:none;
    transition:border .2s,box-shadow .2s;
  }
  input:focus,textarea:focus{border-color:var(--pu);box-shadow:0 0 0 3px var(--pu-glow)}
  textarea{resize:vertical;min-height:80px;font-family:monospace;font-size:.78rem}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}

  /* ── BUTTONS ── */
  .btn-row{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;align-items:center}
  button{background:linear-gradient(135deg,var(--pu),var(--pu2));color:#fff;border:none;
    border-radius:10px;padding:10px 20px;font-size:.84rem;font-weight:600;cursor:pointer;
    display:inline-flex;align-items:center;gap:7px;transition:all .2s;
    box-shadow:0 4px 12px rgba(92,63,216,.3)}
  button:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(92,63,216,.45)}
  button:active{transform:scale(.97);box-shadow:none}
  button.danger{background:linear-gradient(135deg,#dc2626,#991b1b);box-shadow:0 4px 12px rgba(220,38,38,.25)}
  button.danger:hover{box-shadow:0 6px 18px rgba(220,38,38,.4)}
  button.success{background:linear-gradient(135deg,#16a34a,#15803d);box-shadow:0 4px 12px rgba(22,163,74,.25)}
  button.success:hover{box-shadow:0 6px 18px rgba(22,163,74,.4)}
  button.ghost{background:transparent;border:1px solid var(--brd2);color:var(--tx2);box-shadow:none}
  button.ghost:hover{border-color:var(--pu);color:#e2e2f0;background:var(--bg3)}
  button.sm{padding:6px 12px;font-size:.75rem;border-radius:7px}

  /* ── TOAST ── */
  .toast{
    position:fixed;top:20px;right:20px;z-index:999;padding:12px 18px;border-radius:12px;
    font-size:.85rem;font-weight:600;display:flex;align-items:center;gap:10px;
    transform:translateX(120%);transition:transform .35s cubic-bezier(.34,1.56,.64,1);
    max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.5);
  }
  .toast.show{transform:translateX(0)}
  .toast.ok{background:#14532d;color:#4ade80;border:1px solid #166534}
  .toast.err{background:#450a0a;color:#f87171;border:1px solid #7f1d1d}
  .toast.info{background:#1e1b4b;color:#a78bfa;border:1px solid #3730a3}

  /* ── GENERATED KEY BOX ── */
  .key-result{
    background:var(--bg4);border:1px solid var(--pu);border-radius:12px;
    padding:16px 18px;margin-top:16px;display:none;
    animation:fadeIn .3s ease;
  }
  .key-result.show{display:block}
  .key-label{font-size:.68rem;color:var(--tx);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px}
  .key-value{
    font-family:'Courier New',monospace;font-size:.95rem;color:#a78bfa;
    word-break:break-all;background:var(--bg3);border:1px solid var(--brd2);
    border-radius:8px;padding:10px 14px;margin-bottom:10px;letter-spacing:.5px;
  }
  .key-meta{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px}
  .key-meta span{font-size:.72rem;color:var(--tx2);background:var(--bg3);
    border:1px solid var(--brd2);padding:3px 10px;border-radius:20px}
  .copy-btn{background:linear-gradient(135deg,#1d4ed8,#1e40af);font-size:.78rem;padding:7px 14px;border-radius:8px}
  .copy-btn:hover{background:linear-gradient(135deg,#2563eb,#1d4ed8)}
  @keyframes fadeIn{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}

  /* ── TABLE ── */
  .tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid var(--brd)}
  table{width:100%;border-collapse:collapse;font-size:.82rem;min-width:560px}
  th{color:var(--tx);font-weight:700;text-transform:uppercase;font-size:.67rem;
    letter-spacing:1px;padding:10px 14px;border-bottom:1px solid var(--brd);
    text-align:left;background:var(--bg3)}
  td{padding:11px 14px;border-bottom:1px solid var(--brd);vertical-align:middle;
    color:var(--tx2)}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(124,92,252,.05)}
  .badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;
    border-radius:20px;font-size:.68rem;font-weight:700}
  .badge.green{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.2)}
  .badge.red{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.2)}
  .badge.blue{background:rgba(96,165,250,.12);color:#60a5fa;border:1px solid rgba(96,165,250,.2)}
  .badge.yw{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.2)}

  /* Key cell with copy */
  .key-cell{display:flex;align-items:center;gap:8px}
  .key-code{font-family:monospace;font-size:.78rem;color:#a78bfa;cursor:pointer;
    background:var(--bg3);border:1px solid var(--brd2);padding:3px 8px;
    border-radius:6px;transition:border .2s;max-width:160px;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .key-code:hover{border-color:var(--pu)}
  .copy-icon{cursor:pointer;opacity:.5;font-size:.85rem;transition:opacity .2s;flex-shrink:0}
  .copy-icon:hover{opacity:1}

  /* ── EMPTY STATE ── */
  .empty{text-align:center;padding:32px;color:var(--tx)}
  .empty .ei{font-size:2rem;margin-bottom:8px}
  .empty p{font-size:.82rem}

  /* ── DIVIDER ── */
  .divider{border:none;border-top:1px solid var(--brd);margin:20px 0}

  @media(max-width:640px){
    .stats-row{grid-template-columns:1fr 1fr}
    .row2,.row3{grid-template-columns:1fr}
    .wrap{padding:16px 12px}
    header{padding:14px 16px}
  }
</style>
</head>
<body>

<!-- Toast -->
<div class="toast" id="toast"></div>

<header>
  <div class="logo">⚡</div>
  <h1>Felix API</h1>
  <div class="hbadge">Admin Panel</div>
  <div class="pulse" title="Server Online"></div>
</header>

<div class="wrap">

  <!-- Stats -->
  <div class="stats-row">
    <div class="stat"><div class="ico">🔑</div><div class="val" id="st-keys">—</div><div class="lbl">API Keys</div></div>
    <div class="stat"><div class="ico">👤</div><div class="val" id="st-accs">—</div><div class="lbl">Accounts</div></div>
    <div class="stat"><div class="ico">⏳</div><div class="val" id="st-pend">—</div><div class="lbl">Pending</div></div>
    <div class="stat"><div class="ico">📊</div><div class="val" id="st-uptime">Live</div><div class="lbl">Status</div></div>
  </div>

  <!-- Generate Key -->
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Generate API Key</h2>
    </div>
    <div class="row3">
      <div>
        <label>Key Name / Owner</label>
        <input id="kName" placeholder="e.g. My App"/>
      </div>
      <div>
        <label>Expiry Days (0 = Forever)</label>
        <input id="kDays" type="number" value="30" min="0"/>
      </div>
      <div>
        <label>Daily Limit (0 = Unlimited)</label>
        <input id="kLimit" type="number" value="100" min="0"/>
      </div>
    </div>
    <div class="btn-row">
      <button onclick="genKey()">⚡ Generate Key</button>
    </div>

    <!-- Generated Key Result Box -->
    <div class="key-result" id="keyResult">
      <div class="key-label">✅ Key Generated Successfully</div>
      <div class="key-value" id="keyDisplay">—</div>
      <div class="key-meta" id="keyMeta"></div>
      <div class="btn-row">
        <button class="copy-btn" onclick="copyKey()">📋 Copy Key</button>
        <button class="ghost sm" onclick="document.getElementById('keyResult').classList.remove('show')">✕ Close</button>
      </div>
    </div>
  </div>

  <!-- Keys Table -->
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Active API Keys</h2>
      <button class="ghost sm" onclick="loadKeys()">↻ Refresh</button>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>API Key</th><th>Name</th><th>Expiry</th>
          <th>Daily Limit</th><th>Uses</th><th>Status</th><th>Action</th>
        </tr></thead>
        <tbody id="keysTbl">
          <tr><td colspan="7"><div class="empty"><div class="ei">⏳</div><p>Loading keys...</p></div></td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <hr class="divider"/>

  <!-- Add Account -->
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Add Telegram Account</h2>
    </div>
    <div class="row2">
      <div>
        <label>Account Name</label>
        <input id="aName" placeholder="e.g. Account 2"/>
      </div>
      <div>
        <label>API ID</label>
        <input id="aApiId" placeholder="12345678"/>
      </div>
    </div>
    <label>API Hash</label>
    <input id="aApiHash" placeholder="32 character hash"/>
    <label>Session String (Telethon)</label>
    <textarea id="aSession" placeholder="Paste Telethon StringSession here..."></textarea>
    <div class="btn-row">
      <button class="success" onclick="addAccount()">➕ Add Account</button>
    </div>
  </div>

  <!-- Accounts Table -->
  <div class="card">
    <div class="card-hd">
      <h2><span class="dot"></span>Telegram Accounts</h2>
      <button class="ghost sm" onclick="loadAccounts()">↻ Refresh</button>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Name</th><th>API ID</th><th>Status</th><th>Mode</th><th>Actions</th>
        </tr></thead>
        <tbody id="accsTbl">
          <tr><td colspan="5"><div class="empty"><div class="ei">⏳</div><p>Loading accounts...</p></div></td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>

<script>
const ADM = new URLSearchParams(location.search).get('key') || '';
let _lastKey = '';

// ── Toast ──
function toast(msg, type='ok'){
  const el = document.getElementById('toast');
  el.className = 'toast ' + type;
  el.innerHTML = (type==='ok'?'✅':type==='err'?'❌':'ℹ️') + ' <span>' + msg + '</span>';
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'), 3500);
}

// ── API ──
async function api(path, method='GET', body=null){
  const sep = path.includes('?') ? '&' : '?';
  try {
    const r = await fetch(path + sep + 'key=' + ADM, {
      method, headers:{'Content-Type':'application/json'},
      body: body ? JSON.stringify(body) : null
    });
    return r.json();
  } catch(e) {
    return {success:false, error:'Network error'};
  }
}

// ── Copy Helper ──
function copyText(text, label){
  navigator.clipboard.writeText(text).then(()=>{
    toast((label||'Text') + ' copied!', 'ok');
  }).catch(()=>{
    // fallback
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    toast((label||'Text') + ' copied!', 'ok');
  });
}

function copyKey(){
  if(_lastKey) copyText(_lastKey, 'API Key');
}

// ── Stats ──
async function loadStats(){
  const d = await api('/admin/stats');
  if(!d.success) return;
  document.getElementById('st-keys').textContent = d.total_keys ?? '—';
  document.getElementById('st-accs').textContent = d.total_accounts ?? '—';
  document.getElementById('st-pend').textContent = d.pending ?? '—';
  document.getElementById('st-uptime').textContent = '✅';
}

// ── Keys ──
async function loadKeys(){
  const d = await api('/admin/keys');
  const tb = document.getElementById('keysTbl');
  if(!d.success){
    tb.innerHTML='<tr><td colspan="7"><div class="empty"><div class="ei">🔐</div><p>Auth failed</p></div></td></tr>';
    return;
  }
  if(!d.keys.length){
    tb.innerHTML='<tr><td colspan="7"><div class="empty"><div class="ei">🗝️</div><p>No API keys yet</p></div></td></tr>';
    return;
  }
  tb.innerHTML = d.keys.map(k=>{
    const expiry = k.expiry ? k.expiry.split('T')[0] : '<span style="color:#4ade80">Forever</span>';
    const limit  = k.daily_limit > 0 ? k.daily_limit : '<span style="color:#4ade80">∞</span>';
    const shortKey = k.key.slice(0,8)+'...'+k.key.slice(-4);
    return `<tr>
      <td>
        <div class="key-cell">
          <span class="key-code" title="${k.key}" onclick="copyText('${k.key}','API Key')">${shortKey}</span>
          <span class="copy-icon" onclick="copyText('${k.key}','API Key')" title="Copy full key">📋</span>
        </div>
      </td>
      <td><b style="color:#e2e2f0">${k.name}</b></td>
      <td>${expiry}</td>
      <td>${limit}</td>
      <td><span class="badge blue">${k.uses}</span></td>
      <td><span class="badge ${k.active?'green':'red'}">${k.active?'● Active':'● Disabled'}</span></td>
      <td>
        <button class="danger sm" onclick="delKey('${k.key}')">Revoke</button>
      </td>
    </tr>`;
  }).join('');
}

async function genKey(){
  const name = document.getElementById('kName').value.trim();
  const days = parseInt(document.getElementById('kDays').value)||0;
  const limit = parseInt(document.getElementById('kLimit').value)||0;
  if(!name){ toast('Enter a key name', 'err'); return; }
  const btn = event.target.closest('button');
  btn.disabled = true; btn.textContent = '⏳ Generating...';
  const d = await api('/admin/keys/create','POST',{name,days,daily_limit:limit});
  btn.disabled = false; btn.innerHTML = '⚡ Generate Key';
  if(d.success){
    _lastKey = d.key;
    document.getElementById('keyDisplay').textContent = d.key;
    const expTxt = d.expiry ? 'Expires: '+d.expiry.split('T')[0] : 'Expires: Never';
    const limTxt = limit > 0 ? 'Limit: '+limit+'/day' : 'Limit: Unlimited';
    document.getElementById('keyMeta').innerHTML =
      `<span>👤 ${name}</span><span>📅 ${expTxt}</span><span>🔢 ${limTxt}</span>`;
    document.getElementById('keyResult').classList.add('show');
    toast('Key generated for '+name, 'ok');
    loadKeys(); loadStats();
  } else {
    toast(d.error||'Failed to generate key', 'err');
  }
}

async function delKey(key){
  if(!confirm('Revoke this API key? This cannot be undone.')) return;
  const d = await api('/admin/keys/revoke','POST',{key});
  if(d.success){ toast('Key revoked', 'ok'); loadKeys(); loadStats(); }
  else toast(d.error||'Failed', 'err');
}

// ── Accounts ──
async function loadAccounts(){
  const d = await api('/admin/accounts');
  const tb = document.getElementById('accsTbl');
  if(!d.success){
    tb.innerHTML='<tr><td colspan="5"><div class="empty"><div class="ei">🔐</div><p>Auth failed</p></div></td></tr>';
    return;
  }
  if(!d.accounts.length){
    tb.innerHTML='<tr><td colspan="5"><div class="empty"><div class="ei">👤</div><p>No accounts added</p></div></td></tr>';
    return;
  }
  tb.innerHTML = d.accounts.map((a,i)=>`
    <tr>
      <td><b style="color:#e2e2f0">${a.name}</b></td>
      <td><span class="badge blue">${a.api_id}</span></td>
      <td><span class="badge ${a.active?'green':'red'}">${a.active?'● Active':'● Disabled'}</span></td>
      <td><span class="badge ${i===0?'yw':''}">${i===0?'⚡ Current':'Standby'}</span></td>
      <td style="display:flex;gap:6px;flex-wrap:wrap">
        <button class="success sm" onclick="startAcc(${a.id})" title="Start/Reconnect this account">▶ Start</button>
        <button class="danger sm" onclick="delAcc(${a.id})">✕ Remove</button>
      </td>
    </tr>`).join('');
}

async function addAccount(){
  const name           = document.getElementById('aName').value.trim();
  const api_id         = document.getElementById('aApiId').value.trim();
  const api_hash       = document.getElementById('aApiHash').value.trim();
  const session_string = document.getElementById('aSession').value.trim();
  if(!name||!api_id||!api_hash||!session_string){ toast('Fill all fields', 'err'); return; }
  const btn = event.target.closest('button');
  btn.disabled = true; btn.textContent = '⏳ Adding...';
  const d = await api('/admin/accounts/add','POST',{name,api_id,api_hash,session_string});
  btn.disabled = false; btn.innerHTML = '➕ Add Account';
  if(d.success){
    toast('Account added! Restart bot to activate.', 'ok');
    loadAccounts(); loadStats();
    document.getElementById('aName').value='';
    document.getElementById('aApiId').value='';
    document.getElementById('aApiHash').value='';
    document.getElementById('aSession').value='';
  } else {
    toast(d.error||'Failed', 'err');
  }
}

async function startAcc(id){
  // Call restart/start endpoint
  const d = await api('/admin/accounts/start','POST',{id});
  if(d && d.success) toast('Account start signal sent!', 'info');
  else toast('Start: ' + (d&&d.error ? d.error : 'Signal sent (restart bot if needed)'), 'info');
}

async function delAcc(id){
  if(!confirm('Remove this account?')) return;
  const d = await api('/admin/accounts/remove','POST',{id});
  if(d.success){ toast('Account removed', 'ok'); loadAccounts(); loadStats(); }
  else toast(d.error||'Failed', 'err');
}

loadStats(); loadKeys(); loadAccounts();
setInterval(loadStats, 10000);
</script>
</body>
</html>"""

@app.route('/admin')
def admin_panel():
    from flask import Response as _R
    key = request.args.get('key','')
    if key != ADMIN_KEY:
        return _R('{"error":"Admin access required"}', status=403, mimetype='application/json')
    return _R(ADMIN_HTML, mimetype='text/html')

@app.route('/admin/stats')
def admin_stats():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    conn = get_db()
    total_keys = conn.execute("SELECT COUNT(*) FROM api_keys WHERE active=1").fetchone()[0]
    total_accs = conn.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    conn.close()
    return jsonify({"success":True,"total_keys":total_keys,"total_accounts":total_accs,"pending":len(pending)})

@app.route('/admin/keys')
def admin_keys():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    conn = get_db()
    rows = conn.execute("SELECT * FROM api_keys ORDER BY id DESC").fetchall()
    conn.close()
    keys = []
    for r in rows:
        keys.append({"key":r["key"],"name":r["name"],"expiry":r["expiry"],"active":r["active"],"uses":r["uses"],"daily_limit":r["daily_limit"] if "daily_limit" in r.keys() else 0})
    return jsonify({"success":True,"keys":keys})

@app.route('/admin/keys/create', methods=['POST'])
def admin_keys_create():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    data = request.get_json() or {}
    name = data.get('name','').strip()
    days = int(data.get('days', 30))
    daily_limit = int(data.get('daily_limit', 0))
    if not name: return jsonify({"success":False,"error":"Name required"}), 400
    new_key = uuid.uuid4().hex[:20]
    expiry = (datetime.now() + timedelta(days=days)).isoformat() if days > 0 else None
    conn = get_db()
    # Add daily_limit column if not exists
    try: conn.execute("ALTER TABLE api_keys ADD COLUMN daily_limit INTEGER DEFAULT 0")
    except: pass
    try: conn.execute("ALTER TABLE api_keys ADD COLUMN daily_uses INTEGER DEFAULT 0")
    except: pass
    try: conn.execute("ALTER TABLE api_keys ADD COLUMN last_reset TEXT")
    except: pass
    conn.execute("INSERT INTO api_keys (key,name,created,expiry,active,uses,daily_limit) VALUES (?,?,?,?,1,0,?)",
                 (new_key, name, datetime.now().isoformat(), expiry, daily_limit))
    conn.commit(); conn.close()
    return jsonify({"success":True,"key":new_key,"expiry":expiry,"daily_limit":daily_limit})

@app.route('/admin/keys/revoke', methods=['POST'])
def admin_keys_revoke():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    data = request.get_json() or {}
    target = data.get('key','')
    conn = get_db()
    conn.execute("UPDATE api_keys SET active=0 WHERE key=?", (target,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route('/admin/accounts')
def admin_accounts():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    conn = get_db()
    rows = conn.execute("SELECT id,name,api_id,active FROM accounts ORDER BY id").fetchall()
    conn.close()
    return jsonify({"success":True,"accounts":[{"id":r["id"],"name":r["name"],"api_id":r["api_id"],"active":r["active"]} for r in rows]})

@app.route('/admin/accounts/add', methods=['POST'])
def admin_accounts_add():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    data = request.get_json() or {}
    name = data.get('name','').strip()
    api_id = data.get('api_id','').strip()
    api_hash = data.get('api_hash','').strip()
    session_string = data.get('session_string','').strip()
    if not all([name, api_id, api_hash, session_string]):
        return jsonify({"success":False,"error":"All fields required"}), 400
    conn = get_db()
    conn.execute("INSERT INTO accounts (name,api_id,api_hash,session_string,active,created) VALUES (?,?,?,?,1,?)",
                 (name, api_id, api_hash, session_string, datetime.now().isoformat()))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route('/admin/accounts/remove', methods=['POST'])
def admin_accounts_remove():
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    data = request.get_json() or {}
    acc_id = int(data.get('id', 0))
    conn = get_db()
    conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route('/admin/accounts/start', methods=['POST'])
def admin_accounts_start():
    """Check/reconnect status of an account."""
    key = request.args.get('key','')
    if key != ADMIN_KEY: return jsonify({"success":False,"error":"Unauthorized"}), 403
    data = request.get_json() or {}
    acc_id = int(data.get('id', 0))
    existing = acc_manager.get_client(acc_id)
    if existing:
        try:
            connected = existing.is_connected()
            return jsonify({"success":True,"message":f"Account {acc_id} is {'connected ✅' if connected else 'disconnected — restart bot'}"})
        except Exception as e:
            return jsonify({"success":False,"error":str(e)})
    return jsonify({"success":True,"message":"Account not loaded yet — restart bot to activate"})

# ==================== MAIN ====================

def run_flask():
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

async def main():
    global loop, client
    loop = asyncio.get_running_loop()

    # DB init + auto-insert default account if not exists
    init_db()
    conn = get_db()
    existing = conn.execute("SELECT id FROM accounts WHERE active=1").fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO accounts (name, api_id, api_hash, session_string, active, created)
            VALUES (?, ?, ?, ?, 1, ?)
        """, ("default", str(API_ID), API_HASH, STRING_SESSION, datetime.now().isoformat()))
        conn.commit()
        print("[DB] Default account inserted")
    conn.close()

    # Start Telegram client
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    client.add_event_handler(on_message)

    # Register client in acc_manager
    conn = get_db()
    row = conn.execute("SELECT id FROM accounts WHERE active=1 LIMIT 1").fetchone()
    conn.close()
    if row:
        acc_manager.set_client(row["id"], client)
        print(f"[ACC] Registered account id={row['id']}")

    print("=" * 55)
    print("✅ TRUECALLER API READY")
    print(f"👤 {me.first_name} | 📱 {getattr(me, 'phone', 'N/A')}")
    print("📡 http://localhost:5000/api?key=felix&num=NUMBER")
    print("=" * 55)

    await client.run_until_disconnected()

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
