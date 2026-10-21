"""Simulasi integrasi WAHA dengan server HTTP tiruan. Usage: python tests/sim_whatsapp.py <workdir>"""
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

WORK = sys.argv[1]
os.makedirs(WORK, exist_ok=True)
for f in os.listdir(WORK):
    os.remove(os.path.join(WORK, f))

# ---- WAHA tiruan ----
received, state = [], {"status": "WORKING", "fail": False}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append({"path": self.path, "key": self.headers.get("X-Api-Key"), "body": body, "t": time.time()})
        if self.headers.get("X-Api-Key") != "rahasia":
            return self._reply(401, {"message": "Unauthorized"})
        self._reply(500 if state["fail"] else 201, {"id": "x"})
    def do_GET(self):
        if self.headers.get("X-Api-Key") != "rahasia":
            return self._reply(401, {})
        self._reply(200, {"name": "default", "status": state["status"]})
server = HTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]

os.environ["BOT_NO_DOTENV"] = "1"
os.environ.update(BOT_TOKEN="123:dummy", ADMIN_IDS="1", DB_FILE=f"{WORK}/bot.db", BOT_WORKERS="1", JAM_LAYANAN="",
                  WAHA_URL=f"http://127.0.0.1:{PORT}/", WAHA_API_KEY="rahasia",
                  WA_TARGETS="0812-3456-789, +62 811 222 333, 120363012345@g.us")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot as b
import whatsapp

failed = False
def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + label)
    failed |= not cond

log, mid = [], [1000]
def fake_send(chat_id, text, **kw):
    mid[0] += 1; log.append((chat_id, text)); return SimpleNamespace(message_id=mid[0])
b.bot.send_message = fake_send
b.bot.edit_message_text = lambda *a, **k: True
b.wa.delay = 0.05

uid = [0]
def msg(chat_id, text):
    uid[0] += 1; log.clear()
    m = {"message_id": uid[0], "date": int(time.time()), "chat": {"id": chat_id, "type": "private"},
         "from": {"id": chat_id, "is_bot": False, "first_name": "x"}, "text": text}
    if text.startswith("/"):
        m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    b.bot.process_new_updates([b.Update.de_json({"update_id": uid[0], "message": m})])
    return list(log)
def wait_wa(n, timeout=5):
    end = time.time() + timeout
    while len(received) < n and time.time() < end:
        time.sleep(0.02)
def register(user, nama, instansi, layanan):
    for t in ("/start", "Menghubungi Admin", nama, instansi):
        msg(user, t)
    return msg(user, layanan)

# ---- format tujuan ----
check(whatsapp.to_chat_id("0812-3456-789") == "628123456789@c.us", "08xx number -> 62xx@c.us")
check(whatsapp.to_chat_id("+62 811 222 333") == "62811222333@c.us", "+62 number with spaces")
check(whatsapp.to_chat_id("120363012345@g.us") == "120363012345@g.us", "group id kept")
check(b.wa.enabled and b.wa.url == f"http://127.0.0.1:{PORT}", "notifier enabled, trailing slash removed")

# ---- permintaan baru ----
b.BOT_USERNAME = "KSSPemdaBSSN_bot"
out = register(100, "Budi", "Kota Padang", "TTIS")
check(any("Terima kasih" in t for _, t in out), "user confirmation not delayed by WhatsApp")
wait_wa(3)
check(len(received) == 3 and {r["body"]["chatId"] for r in received} == {"628123456789@c.us", "62811222333@c.us", "120363012345@g.us"},
      "notification sent to all WA targets")
r0 = received[0]
check(r0["path"] == "/api/sendText" and r0["key"] == "rahasia" and r0["body"]["session"] == "default", "WAHA request format: path, X-Api-Key, session")
text = r0["body"]["text"]
check("Permintaan baru #0001" in text and "TTIS" in text and "https://t.me/KSSPemdaBSSN_bot" in text, "WA text has ticket, layanan, bot link")
check("Budi" not in text and "Kota Padang" not in text, "user details not sent to WA by default")
gaps = [received[i]["t"] - received[i - 1]["t"] for i in range(1, 3)]
check(all(g >= 0.04 for g in gaps), "delay between WA targets")

# ---- detail & di luar jam layanan ----
received.clear()
b.WA_DETAIL = True
b.is_open = lambda now=None: False
register(200, "Sari", "Kab Agam", "PEMDI"); wait_wa(3)
t2 = received[0]["body"]["text"]
check("Sari – Kab Agam" in t2 and "luar jam layanan" in t2, "WA_DETAIL adds name/instansi; out-of-hours noted")
b.WA_DETAIL = False
b.is_open = lambda now=None: True

# ---- WAHA error tidak mengganggu bot ----
received.clear(); state["fail"] = True
out = register(300, "Andi", "Kab Solok", "MANRISK")
check(any("Terima kasih" in t for _, t in out), "WAHA returning 500 does not break registration")
wait_wa(3); state["fail"] = False

down = whatsapp.WahaNotifier("http://127.0.0.1:1", "x", "default", ["0811"], timeout=1)
check(down.send_text("62811@c.us", "x") is False and down.session_status() is None, "WAHA down handled (no exception)")
wrong_key = whatsapp.WahaNotifier(f"http://127.0.0.1:{PORT}", "salah", "default", ["0811"])
check(wrong_key.send_text("62811@c.us", "x") is False, "wrong API key reported as failure")

# ---- pengingat ke WA ----
received.clear()
b.WA_REMINDERS = True
from datetime import datetime
now = int(datetime(2026, 10, 19, 10, 0, tzinfo=b.TIMEZONE).timestamp())
b.db.conn.execute("UPDATE requests SET created_at = ?, taken_by = NULL", (now - 20 * 60,))
b.db.conn.execute("UPDATE requests SET taken_by = 1 WHERE user_id != 100")
b.is_open = lambda now=None: True
b.send_reminders(now); wait_wa(3)
check(received and "belum diambil selama 20 menit" in received[0]["body"]["text"], "WA_REMINDERS sends reminder to WA")
received.clear(); b.WA_REMINDERS = False
b.db.conn.execute("UPDATE requests SET reminders = 0, reminded_at = NULL")
b.send_reminders(now); time.sleep(0.3)
check(not received, "no WA reminder when WA_REMINDERS off")

# ---- /wa ----
received.clear()
out = msg(1, "/wa")
check("Sesi WAHA 'default': WORKING" in out[0][1] and "3/3 tujuan" in out[0][1] and len(received) == 3, "/wa reports status and sends test")
state["status"] = "SCAN_QR_CODE"
out = msg(1, "/wa"); check("scan QR" in out[0][1], "/wa hints QR scan when session not ready")
out = msg(100, "/wa"); check(out[0][1] == "Anda tidak diizinkan.", "/wa admin only")

b.wa.shutdown()
server.shutdown()
sys.exit(1 if failed else 0)
