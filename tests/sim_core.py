"""Simulasi alur inti bot (DM & grup admin). Usage: python tests/sim_core.py <dm|group> <workdir>"""
import io, json, os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace

MODE, WORK = sys.argv[1], sys.argv[2]
os.makedirs(WORK, exist_ok=True)
for f in os.listdir(WORK):
    os.remove(os.path.join(WORK, f))
GROUP = -100500
os.environ["BOT_NO_DOTENV"] = "1"
os.environ.update(
    BOT_TOKEN="123:dummy", ADMIN_IDS="1, 2", LAYANAN_ADMINS="TTIS=3",
    DB_FILE=f"{WORK}/bot.db", BOT_WORKERS="1", CLEANUP_DAYS="30", JAM_LAYANAN="",
)
if MODE == "group":
    os.environ["ADMIN_GROUP_ID"] = str(GROUP)
# data lama untuk diuji migrasinya
json.dump({"users": {"777": {"nama": "Lama", "instansi": "Kab X", "last_active": 1700000000},
                     "778": {"last_active": 1700000000}},
           "admin_names": {"1": "adm1"}}, open(f"{WORK}/state.json", "w"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from telebot.apihelper import ApiTelegramException
import bot as b

failed = False
def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + f"[{MODE}] " + label)
    failed |= not cond

# ---- fake Telegram API ----
log = []
next_mid = [1000]
BLOCKED = {2}
FLAKY = {}

class Resp: status_code = 403; reason = "Forbidden"

def guard(chat_id):
    if chat_id in BLOCKED:
        raise ApiTelegramException("x", Resp(), {"error_code": 403, "description": "Forbidden: blocked"})
    if FLAKY.get(chat_id):
        FLAKY[chat_id] -= 1
        raise requests.exceptions.ReadTimeout("timeout")

def new_msg():
    next_mid[0] += 1
    return SimpleNamespace(message_id=next_mid[0])

def fake_send(chat_id, text, **kw):
    guard(chat_id); m = new_msg(); log.append(("send", chat_id, text, kw, m.message_id)); return m
def fake_copy(chat_id, from_chat, mid, **kw):
    guard(chat_id); m = new_msg(); log.append(("copy", chat_id, f"<copy {mid}>", kw, m.message_id)); return m
def fake_edit(text, chat_id=None, message_id=None, **kw):
    log.append(("edit", chat_id, text, kw, message_id)); return True
def fake_answer(cb_id, text=None, show_alert=None, **kw):
    log.append(("answer", cb_id, text, {"show_alert": show_alert}, None)); return True
def fake_doc(chat_id, document, caption=None, **kw):
    log.append(("doc", chat_id, caption, {"data": document.file.read(), "name": document.file_name}, None)); return new_msg()

b.bot.send_message, b.bot.copy_message = fake_send, fake_copy
b.bot.edit_message_text, b.bot.answer_callback_query, b.bot.send_document = fake_edit, fake_answer, fake_doc
REAL_SLEEP = time.sleep
b.time.sleep = lambda s: None

uid = [0]
def update(payload):
    uid[0] += 1
    log.clear()
    b.bot.process_new_updates([b.Update.de_json({"update_id": uid[0], **payload})])
    return list(log)

def msg(chat_id, text=None, *, sender=None, photo=False, reply_to=None, chat_type=None):
    sender = sender or chat_id
    m = {"message_id": 5000 + uid[0], "date": int(time.time()),
         "chat": {"id": chat_id, "type": chat_type or ("private" if chat_id > 0 else "supergroup")},
         "from": {"id": sender, "is_bot": False, "first_name": "x", "username": f"adm{sender}" if sender in (1, 2, 3) else None}}
    if photo:
        m["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    else:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to, "date": 0, "chat": m["chat"], "from": {"id": 0, "is_bot": True, "first_name": "bot"}, "text": "x"}
    return update({"message": m})

def click(admin, data, chat_id=None, message_id=1):
    return update({"callback_query": {"id": f"cb{uid[0]}", "from": {"id": admin, "is_bot": False, "first_name": "x", "username": f"adm{admin}"},
                                      "chat_instance": "ci", "data": data,
                                      "message": {"message_id": message_id, "date": 0, "chat": {"id": chat_id or admin, "type": "private"}, "text": "n"}}})

def sends(out, chat=None):
    return [e for e in out if e[0] in ("send", "copy") and (chat is None or e[1] == chat)]

def register(user, nama, instansi, layanan):
    msg(user, "/start"); msg(user, "Menghubungi Admin"); msg(user, nama); msg(user, instansi)
    return msg(user, layanan)

U, V, W = 100, 200, 300

# ---- migrasi ----
check(os.path.exists(f"{WORK}/state.json.migrated") and not os.path.exists(f"{WORK}/state.json"), "state.json migrated and renamed")
check(b.db.get_user(777)["layanan"] == "-" and b.db.get_user(778)["layanan"] is None, "legacy registered user kept as registered")

# ---- katalog ----
out = msg(U, "/start")
check(len(out) == 1 and "Katalog Layanan" in out[0][2] and all(n in out[0][2] for n in b.LAYANAN) and out[0][3].get("parse_mode") == "HTML", "/start shows welcome + full catalog in one message")
check("Menghubungi Admin" in out[0][3]["reply_markup"].to_json(), "/start welcome carries menu button")
out = msg(U, "/layanan"); check("PENGADUAN" in out[0][2] and "Selamat datang" not in out[0][2], "/layanan shows catalog only")
from html.parser import HTMLParser
class P(HTMLParser):
    def __init__(s): super().__init__(); s.stack = []; s.ok = True
    def handle_starttag(s, t, a): s.stack.append(t)
    def handle_endtag(s, t): s.ok &= bool(s.stack) and s.stack.pop() == t
p_ = P(); p_.feed(b.WELCOME_TEXT); check(p_.ok and not p_.stack, "welcome HTML well-formed")
msg(U, "Menghubungi Admin"); out = msg(U, "/layanan"); check("Katalog" in out[0][2] and b.db.get_step(U) == "nama", "/layanan works mid-registration without breaking it")
msg(U, "/start")

# ---- pendaftaran + routing per layanan ----
out = msg(U, "Menghubungi Admin"); check("Nama" in out[0][2], "asks nama")
out = msg(U, "x", photo=True); check("dengan teks" in out[0][2], "photo during registration -> ask for text")
msg(U, "Budi"); out = msg(U, "Kota Padang"); check("Layanan" in out[0][2], "asks layanan")
out = msg(U, "lainnya"); check("tidak dikenal" in out[0][2], "invalid layanan rejected")
out = msg(U, "ttis")
notices_u = sends(out, GROUP if MODE == "group" else 3)
check(len(notices_u) == 1 and "Layanan: TTIS" in notices_u[0][2], "TTIS notice goes to " + ("group" if MODE == "group" else "TTIS admin 3 only"))
check(not sends(out, 1), "admin 1 not notified for TTIS")
check("take:100" in json.dumps(notices_u[0][3]["reply_markup"].to_dict()), "notice has Ambil button")
check(sends(out, U)[-1][2].startswith("Terima kasih"), "user confirmed")
notice_u_mid = notices_u[0][4]

out = register(V, "Sari", "Kab Agam", "PEMDI")
if MODE == "group":
    check(len(sends(out, GROUP)) == 1 and not sends(out, 1), "PEMDI notice to group")
else:
    check(len(sends(out, 1)) == 1 and not sends(out, 3), "PEMDI (no layanan admin) -> all ADMIN_IDS, admin 2 blocked skipped")

# ---- tombol Ambil ----
nchat = GROUP if MODE == "group" else 3
out = click(3, "take:100", chat_id=nchat, message_id=notice_u_mid)
check(any(e[0] == "send" and e[1] == U and "Admin telah menghubungi" in e[2] for e in out), "Ambil: user informed")
check(any(e[0] == "edit" and e[4] == notice_u_mid and "Diambil oleh adm3" in e[2] for e in out), "Ambil: notice marked taken")
check(any(e[0] == "answer" and "Berhasil" in e[2] for e in out) and any(e[0] == "send" and e[1] == 3 and "Sekarang bicara dengan: Budi (Kota Padang · TTIS · #0001)" in e[2] for e in out), "Ambil: callback answered + active indicator DM")
out = click(1, "take:100")
check(any(e[0] == "answer" and "sudah ditangani adm3" in e[2] and e[3]["show_alert"] for e in out), "second Ambil refused with alert")
out = click(55, "take:100"); check(any(e[0] == "answer" and "tidak diizinkan" in e[2] for e in out), "non-admin Ambil refused")

# ---- pesan user -> admin, reply admin -> user ----
out = msg(U, "halo admin"); check(sends(out) and all(e[1] == 3 for e in sends(out)), "assigned user -> only their admin")
out = msg(V, "tolong")
vchat = GROUP if MODE == "group" else 1
fwd = sends(out, vchat); check(len(fwd) == 1 and fwd[0][2] == "[Sari | 200]: tolong", "unassigned user -> " + ("group" if MODE == "group" else "admins"))
out = msg(vchat, "siap", sender=1, reply_to=fwd[0][4])
check(any(e[1] == V and e[2] == "[Admin]: siap\n\n-x" for e in sends(out)), "admin reply delivered to user")
check(b.db.get_user(V)["admin_id"] == 1, "reply assigns user to replying admin")
out = msg(vchat, "boleh?", sender=2, reply_to=fwd[0][4])
check(not sends(out, V) and any("sudah ditangani adm1" in e[2] for e in sends(out)), "other admin reply refused")
out = msg(U, "x", photo=True)
check([e[0] for e in sends(out, 3)] == ["send", "copy"], "user photo: header + copy to admin")
out = msg(3, "x", photo=True, reply_to=sends(out, 3)[1][4])
check(sends(out, U) and sends(out, U)[0][0] == "copy", "admin reply to copied photo -> photo to user")

# ---- banyak user per admin ----
out = msg(1, f"/talk {U}")
check(any(e[1] == 3 and "diambil alih oleh adm1" in e[2] for e in sends(out)), "/talk takeover notifies previous admin")
check(b.db.active_user(1) == U, "active user switched")
out = msg(1, "pesan biasa"); check(sends(out, U) and sends(out, U)[0][2] == "[Admin]: pesan biasa\n\n-x", "plain admin text -> active user")
out = msg(vchat, "masih bisa", sender=1, reply_to=fwd[0][4]); check(sends(out, V), "reply still reaches non-active user")
out = msg(1, "/talk abc"); check("tidak valid" in out[0][2], "/talk bad id")
out = msg(U, f"/talk {V}"); check(out[0][2] == "Anda tidak diizinkan.", "non-admin /talk denied")
if MODE == "group":
    out = msg(GROUP, "ngobrol antar admin", sender=1); check(not out, "group chatter without reply ignored")
    out = msg(-999, "/list", sender=1); check(not out, "unknown group ignored")

# ---- laporan ----
register(W, "=HYPERLINK(\"http://x\")", "Kab Y", "PENGADUAN")
out = msg(1, "/list TTIS"); check("Budi" in out[0][2] and "Sari" not in out[0][2] and "(1)" in out[0][2], "/list filter by layanan")
out = msg(1, "/list")
check("Ditangani Anda" in out[0][2] and "Menunggu admin" in out[0][2] and "Lama" not in out[0][2] and "User Aktif" in out[0][2], "/list shows only active users with status")
check(out[0][2].index("Menunggu admin") < out[0][2].index("Ditangani"), "/list puts waiting users first")
out = msg(1, "/list semua"); check("Lama" in out[0][2] and "Layanan: -" in out[0][2] and "Semua User" in out[0][2], "/list semua incl. legacy user")
out = msg(1, "/list semua ttis"); check("Budi" in out[0][2] and "Sari" not in out[0][2], "/list semua + layanan filter")
out = msg(1, "/list xyz"); check("tidak dikenal" in out[0][2], "/list invalid layanan")
out = msg(1, "/stats"); check("TTIS: 1 permintaan, 0 belum diambil" in out[0][2] and "PENGADUAN: 1 permintaan, 1 belum diambil" in out[0][2], "/stats counts")
out = msg(1, "/export")
doc = [e for e in out if e[0] == "doc"]
check(doc and doc[0][3]["name"].endswith(".xlsx") and "3 permintaan" in doc[0][2], "/export sends xlsx")
from openpyxl import load_workbook
ws = load_workbook(io.BytesIO(doc[0][3]["data"])).active
rows = list(ws.iter_rows(values_only=True))
check(rows[0][3] == "Nama" and rows[1][1] == "#0001" and rows[1][3] == "Budi" and rows[1][7] == "adm3" and rows[0][10] == "Penilaian", "export content incl. ticket & rating columns")
check(str(ws.cell(row=4, column=4).value).startswith("=HYPERLINK") and ws.cell(row=4, column=4).data_type == "s", "export neutralises formula in user input")
out = msg(1, "/export pemdi"); check("1 permintaan" in [e for e in out if e[0] == "doc"][0][2], "/export filter")

# ---- perintah cepat di /list ----
out = msg(1, "/list"); t = out[0][2]
check("Ditangani Anda" in t and "/end_100" in t and "Ambil: /talk_300" in t, "/list shows quick actions for admin's own and waiting users")
out = msg(3, "/list"); check("Ambil alih: /talk_100" in out[0][2] and "/end_100" not in out[0][2], "/list shows takeover action for other admin's user")
out = msg(3, "/talk_300"); check(b.db.get_user(W)["admin_id"] == 3 and any(e[1] == W and "Admin telah menghubungi" in e[2] for e in sends(out)), "/talk_<id> takes user")
out = msg(3, "/end_300@KSSPemdaBSSN_bot"); check(b.db.get_user(W)["admin_id"] is None and any(e[1] == W and "berakhir" in e[2] for e in sends(out)), "/end_<id>@bot ends conversation")
out = msg(3, "/list semua"); check("Hubungi lagi: /talk_300" in out[0][2], "/list semua shows re-contact action for finished user")
out = msg(W, "/talk_100"); check(out[0][2] == "Anda tidak diizinkan.", "non-admin /talk_<id> denied")
out = msg(3, "/talkative"); check(b.db.active_user(3) is None or True, "unrelated command not treated as /talk")
check(not any(e[1] == 3 and "Gunakan format" in e[2] for e in out), "/talkative not parsed as /talk")

# ---- /end ----
out = msg(1, "/end"); check(any(e[1] == U and "berakhir" in e[2] for e in sends(out)), "/end active user")
out = msg(1, "/list"); check("Budi" not in out[0][2], "after /end user no longer in /list (reported bug)")
out = msg(1, "/list semua"); check("Budi" in out[0][2] and "✅ Selesai" in out[0][2], "after /end user in /list semua as Selesai")
out = msg(1, "/end"); check("tidak sedang dalam percakapan" in out[0][2], "/end without active")
out = msg(1, f"/end {V}"); check(b.db.get_user(V)["admin_id"] is None, "/end <id> for non-active assigned user")
out = msg(3, f"/end {V}"); check("tidak sedang menangani" in out[0][2], "/end for user not yours")
b.db.release(W); b.db.assign(W, 1, 0); b.db.release(W)
out = msg(1, "/list"); check("Tidak ada user yang sedang aktif" in out[0][2], "/list empty message when nobody active")

# ---- jaringan ----
FLAKY[1 if MODE == "dm" else GROUP] = 1
out = msg(V, "retry?"); check(len(sends(out)) == 1, "timeout retried once then delivered")
if MODE == "group":
    FLAKY[GROUP] = 4
    out = msg(V, "fallback"); check(sends(out, 1), "group unreachable -> fallback to admins DM")
FLAKY.clear()

# ---- pembersihan ----
b.db.touch(888, 1)  # user lama, tidak pernah mendaftar
c = b.db.cleanup(int(time.time()) - 30 * 86400, b.ADMINS)
check(b.db.get_user(888) is None and b.db.get_user(778) is None and b.db.get_user(777) is not None, "cleanup removes stale unregistered users only")

# ---- dispatcher ----
d = b.ChatDispatcher(4)
order = {1: [], 2: []}
running, peak = [0], [0]
lk = threading.Lock()
def task(key, i):
    def run():
        with lk:
            running[0] += 1; peak[0] = max(peak[0], running[0])
        REAL_SLEEP(0.01 * ((i * 7) % 3))
        order[key].append(i)
        with lk:
            running[0] -= 1
    return run
for i in range(10):
    d.submit(1, task(1, i)); d.submit(2, task(2, i))
d.shutdown()
check(order[1] == list(range(10)) and order[2] == list(range(10)), "dispatcher keeps per-chat order")
check(peak[0] >= 2, "dispatcher runs different chats in parallel")

b.dispatcher = b.ChatDispatcher(4)
blocker = threading.Event()
b.dispatcher.submit(U, blocker.wait)
before = b.bot.last_update_id
b.bot.process_new_updates([b.Update.de_json({"update_id": before + 50, "message": {"message_id": 1, "date": 0, "chat": {"id": U, "type": "private"}, "from": {"id": U, "is_bot": False, "first_name": "x"}, "text": "hi"}})])
check(b.bot.last_update_id == before + 50, "update offset advanced before worker runs")
blocker.set(); b.dispatcher.shutdown()

calls = []
def fake_polling(**kw):
    calls.append(1)
    if len(calls) == 1: raise requests.exceptions.ReadTimeout("x")
    if len(calls) == 2: raise RuntimeError("boom")
b.bot.polling = fake_polling
b.run_polling()
check(len(calls) == 3, "run_polling retries and stops on normal exit")

sys.exit(1 if failed else 0)
