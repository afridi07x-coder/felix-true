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
API_ID         = int(os.environ.get("API_ID", "34635054"))
API_HASH       = os.environ.get("API_HASH", "b8e93ca4f3abdcba65cc020504f82f08")
STRING_SESSION = os.environ.get("STRING_SESSION", "1BVtsOIgBu12ALQ5jHpcN975uTNN-3e4-m1LVKaluRzhGEko7U22nA3_Uh1gF2gzl4IIO_5PgNBdYVhvYs50gnTjC606BtzNiKN9iZA43ndO7sRrE_yBZalC_SaWJGqR9EGPH8gFmR-UX7oVSTKLSHDwOPLgEuO9KpAsY7GDMQYECVb1_Gv88LWLTnKGbJthJocrsP0QJqaqty8676paxdOo1IBIRK4yI8Wpy0PNJ_EcJfgM-SM47PoW6a1rrYgm6joBCzYDHWcYBj2xn7CW0Gu2eSLGAothtogAgHDeZLoJ1n8RS6qpAgZfCAGYeapBzQvnJ59TTze42NfZokGqbAkCkR1hJku4=")
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
    """Returns True if key is valid and not expired."""
    if not key: return False
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key=? AND active=1", (key,)
    ).fetchone()
    conn.close()
    if not row: return False
    if row["expiry"]:
        if datetime.now() > datetime.fromisoformat(row["expiry"]):
            return False
    # Increment use count
    conn = get_db()
    conn.execute("UPDATE api_keys SET uses=uses+1 WHERE key=?", (key,))
    conn.commit()
    conn.close()
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

async def refresh_access(link, session_id, orig_number):
    print(f"[REFRESH] Starting for {orig_number}, link={link}")
    try:
        _, ac = acc_manager.next_client()
        if not ac:
            print("[REFRESH] No client available")
            return False

        # Step 1: Nick Bot ko link bhejo — timestamp note karo pehle
        sent_ts = int(time.time())
        await ac.send_message(NICK_BOT, link)
        print(f"[REFRESH] Sent link to Nick Bot at ts={sent_ts}, waiting...")
        await asyncio.sleep(10)

        # Step 2: Nick Bot ki SIRF NAYI messages se t.me bypass link nikalo
        # sent_ts ke baad aaye messages hi valid hain
        msgs = await ac.get_messages(NICK_BOT, limit=5)
        bypass = None

        for msg in msgs:
            # Purani message skip karo
            if msg.date and int(msg.date.timestamp()) < sent_ts:
                print(f"[REFRESH] Skipping old msg id={msg.id} ts={msg.date}")
                continue
            # Text mein t.me/Truecaller link dhundo (priority)
            if msg.text:
                m = re.search(r'https://t\.me/[^\s\)]+', msg.text)
                if m:
                    bypass = m.group(0).rstrip('.')
                    print(f"[REFRESH] Found bypass in text: {bypass}")
                    break
            # Button mein dhundo (fallback)
            bl = btn_link(msg)
            if bl and "t.me" in bl:
                bypass = bl
                print(f"[REFRESH] Found bypass in button: {bypass}")
                break

        if not bypass:
            print(f"[REFRESH] No t.me bypass link found in Nick Bot response")
            return False

        # Step 3: start= payload nikalo
        start_payload = None
        m = re.search(r'[?&]start=([^&\s]+)', bypass, re.IGNORECASE)
        if m:
            start_payload = m.group(1).strip()

        if not start_payload:
            print(f"[REFRESH] No start payload in bypass link")
            return False

        # Step 4: Telethon StartBot request se open karo (proper deep link trigger)
        # Ye actual Telegram protocol use karta hai — sirf send_message nahi
        from telethon.tl.functions.messages import StartBotRequest
        from telethon.tl.types import InputPeerUser
        try:
            tc_bot_entity = await ac.get_entity(TRUECALLER_BOT)
            await ac(StartBotRequest(
                bot=tc_bot_entity,
                peer=tc_bot_entity,
                start_param=start_payload
            ))
            print(f"[REFRESH] StartBotRequest sent with param={start_payload}")
        except Exception as sbe:
            print(f"[REFRESH] StartBotRequest failed: {sbe}, fallback to send_message")
            await ac.send_message(TRUECALLER_BOT, f"/start {start_payload}", parse_mode=None)

        # Step 5: Access grant hone ka wait karo
        await asyncio.sleep(5)

        # Step 6: Number dobara search karo
        await ac.send_message(TRUECALLER_BOT, clean_num(orig_number))
        print(f"[REFRESH] Re-sent number {orig_number} to Truecaller bot")
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
            asyncio.create_task(refresh_access(link, req["session_id"], req["number"]))
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
FELIX_API_KEY = "daddyfelix"

def run_async(coro):
    """Run async function from sync Flask context."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

async def resolve_username(username):
    """Use userbot to resolve username → {name, username, tg_id}"""
    username = username.lstrip('@')
    _, ac = acc_manager.next_client()
    if not ac: return None
    try:
        entity = await ac.get_entity(username)
        name = " ".join(filter(None, [
            getattr(entity, 'first_name', '') or '',
            getattr(entity, 'last_name', '')  or ''
        ])).strip() or getattr(entity, 'title', '') or username
        uname = getattr(entity, 'username', None)
        tg_id = str(entity.id)
        return {
            "name":        name,
            "username":    f"@{uname}" if uname else f"@{username}",
            "telegram_id": tg_id
        }
    except Exception as e:
        return None

async def resolve_userid(user_id):
    """Use userbot to resolve user_id → {name, username, tg_id}"""
    uid = int(user_id)
    name, uname = None, None

    # Try 1: get_entity (works if user is in cache/contacts)
    _, ac = acc_manager.next_client()
    try:
        entity = await ac.get_entity(uid) if ac else None
        if not entity: raise Exception("No client")
        name = " ".join(filter(None, [
            getattr(entity, 'first_name', '') or '',
            getattr(entity, 'last_name', '')  or ''
        ])).strip() or getattr(entity, 'title', '') or None
        uname = getattr(entity, 'username', None)
        print(f"[TG] get_entity OK: {name} @{uname}")
    except Exception as e:
        print(f"[TG] get_entity failed: {e}")

    # Try 2: inline query resolve (works for public users)
    if not name:
        try:
            result = await ac(GetFullUserRequest(uid)) if ac else None
            if not result: raise Exception("No client")
            u = result.users[0] if result.users else None
            if u:
                name = " ".join(filter(None, [
                    getattr(u, 'first_name', '') or '',
                    getattr(u, 'last_name', '') or ''
                ])).strip() or None
                uname = getattr(u, 'username', None)
                print(f"[TG] GetFullUser OK: {name}")
        except Exception as e:
            print(f"[TG] GetFullUser failed: {e}")

    # Even if name unknown, return ID so phone APIs can still work
    return {
        "name":        name or f"User {user_id}",
        "username":    f"@{uname}" if uname else None,
        "telegram_id": str(uid)
    }

def fetch_phone_from_apis(tg_id):
    """Try both APIs to get phone number from TG ID. Returns {country, country_code, phone_number} or None"""
    import requests as _req

    # API 1: felixapi
    try:
        r = _req.get(f"{FELIX_API}?key={FELIX_API_KEY}&tg={tg_id}", timeout=10)
        if r.status_code == 200:
            d = r.json()
            inner = d.get("result", {}).get("result", {})
            if inner.get("success") and inner.get("number"):
                return {
                    "country":      inner.get("country", "Unknown"),
                    "country_code": inner.get("country_code", ""),
                    "phone_number": inner.get("number", "")
                }
    except Exception as e:
        print(f"[TG] felixapi failed: {e}")

    # API 2: username-usrid-to-num
    try:
        r = _req.get(
            f"{USERID_API}/userid={tg_id}",
            params={"key": "c797993aa04e03df3b6d597c001be4f3"},
            timeout=10
        )
        if r.status_code == 200:
            d = r.json()
            # Try common response fields
            phone = d.get("phone") or d.get("number") or d.get("phone_number")
            if phone:
                return {
                    "country":      d.get("country", "Unknown"),
                    "country_code": d.get("country_code", ""),
                    "phone_number": str(phone)
                }
    except Exception as e:
        print(f"[TG] userid-api failed: {e}")

    return None

@app.route('/api', methods=['GET'])
def api_tg_check():
    """Single /api entry point — routes to tg_lookup or num_lookup"""
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    tg = request.args.get('tg', '').strip()
    if tg:
        return tg_lookup()
    return num_lookup()

@app.route('/api/tg', methods=['GET'])
def api_tg_direct():
    """Direct TG endpoint: /api/tg?key=felix&tg=userid_or_username"""
    key = request.args.get('key', '')
    if key != API_KEY:
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

    # Step 2: Fetch phone number
    location = fetch_phone_from_apis(tg_id)
    if not location:
        location = {"country": "Unknown", "country_code": "", "phone_number": "Not found"}

    # Step 3: Build response
    result = {
        "username_info": {
            "name":        tg_info["name"],
            "username":    tg_info.get("username") or "N/A",
            "telegram_id": tg_id
        },
        "location": {
            "country":      location["country"],
            "country_code": location["country_code"],
            "phone_number": location["phone_number"]
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
    return jsonify({
        "api":       "Truecaller Userbot API — Felix",
        "developer": "@alfaXdevloper",
        "endpoints": {
            "number_lookup": "/api?key=YOUR_KEY&num=NUMBER",
            "tg_lookup":     "/api?key=YOUR_KEY&tg=USERID_OR_USERNAME",
            "health":        "/api/health",
            "admin":         "/admin (POST, requires admin key)"
        },
        "examples":  {
            "indian":   "/api?key=YOUR_KEY&num=8373838566",
            "pakistan": "/api?key=YOUR_KEY&num=3323312487",
            "tg_id":    "/api?key=YOUR_KEY&tg=123456789",
            "username": "/api?key=YOUR_KEY&tg=ItsVivekOP1"
        }
    })

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
