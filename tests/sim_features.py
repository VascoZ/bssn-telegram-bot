"""Simulasi fitur lanjutan (tiket, FAQ, penilaian, topik, pengingat). Usage: python tests/sim_features.py <dm|topics> <workdir>"""
import io, json, os, sys, time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

MODE, WORK = sys.argv[1], sys.argv[2]
os.makedirs(WORK, exist_ok=True)
for f in os.listdir(WORK):
    os.remove(os.path.join(WORK, f))
GROUP = -100500
FAQ = f"{WORK}/faq.json"
json.dump({"_petunjuk": "x", "UMUM": [{"q": "Apa <b>itu</b>?", "a": "Jawab & <script>"}], "TTIS": [{"q": "T?", "a": "A"}], "PEMDI": []},
          open(FAQ, "w", encoding="utf-8"))
os.environ["BOT_NO_DOTENV"] = "1"
os.environ.update(BOT_TOKEN="123:dummy", ADMIN_IDS="1, 3", DB_FILE=f"{WORK}/bot.db", BOT_WORKERS="1",
                  FAQ_FILE=FAQ, JAM_LAYANAN="08:00-16:00", HARI_LAYANAN="1-5", LIBUR="2026-12-25",
                  REMINDER_MINUTES="15", REMINDER_MAX="3", ADMIN_INITIALS="1=ao; 3=")
if MODE == "topics":
    os.environ.update(ADMIN_GROUP_ID=str(GROUP), ADMIN_GROUP_TOPICS="true")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telebot.apihelper import ApiTelegramException
import bot as b

failed = False
def check(cond, label):
    global failed
    print(("PASS " if cond else "FAIL ") + f"[{MODE}] " + label)
    failed |= not cond

log, next_mid, next_topic = [], [1000], [70]
DELETED_THREADS = set()
class Resp: status_code = 400; reason = "Bad Request"
def new_msg():
    next_mid[0] += 1
    return SimpleNamespace(message_id=next_mid[0])
def fake_send(chat_id, text, **kw):
    if kw.get("message_thread_id") in DELETED_THREADS:
        raise ApiTelegramException("x", Resp(), {"error_code": 400, "description": "Bad Request: message thread not found"})
    m = new_msg(); log.append(("send", chat_id, text, kw, m.message_id)); return m
def fake_copy(chat_id, from_chat, mid, **kw):
    m = new_msg(); log.append(("copy", chat_id, f"<copy {mid}>", kw, m.message_id)); return m
def fake_edit(text, chat_id=None, message_id=None, **kw):
    log.append(("edit", chat_id, text, kw, message_id)); return True
def fake_answer(cb_id, text=None, show_alert=None, **kw):
    log.append(("answer", cb_id, text, {"show_alert": show_alert}, None)); return True
def fake_create_topic(chat_id, name, **kw):
    next_topic[0] += 1; log.append(("topic", chat_id, name, kw, next_topic[0]))
    return SimpleNamespace(message_thread_id=next_topic[0])
def fake_edit_topic(chat_id, thread_id, name=None, **kw):
    log.append(("rename", chat_id, name, {"thread": thread_id}, None)); return True
b.bot.send_message, b.bot.copy_message, b.bot.edit_message_text = fake_send, fake_copy, fake_edit
b.bot.answer_callback_query, b.bot.create_forum_topic, b.bot.edit_forum_topic = fake_answer, fake_create_topic, fake_edit_topic
b.time.sleep = lambda s: None
REAL_IS_OPEN = b.is_open
b.is_open = lambda now=None: True  # jam layanan diuji terpisah

uid = [0]
def update(payload):
    uid[0] += 1
    log.clear()
    b.bot.process_new_updates([b.Update.de_json({"update_id": uid[0], **payload})])
    return list(log)

def msg(chat_id, text=None, *, sender=None, photo=False, reply_to=None, thread=None):
    sender = sender or chat_id
    m = {"message_id": 5000 + uid[0], "date": int(time.time()),
         "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "is_forum": thread is not None},
         "from": {"id": sender, "is_bot": False, "first_name": "x", "username": f"adm{sender}" if sender in (1, 3) else None}}
    if photo:
        m["photo"] = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    else:
        m["text"] = text
        if text.startswith("/"):
            m["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if thread is not None:  # pesan di topik forum: Telegram menandainya sebagai reply ke pesan pembuka topik
        m["message_thread_id"] = thread
        m["is_topic_message"] = True
        m["reply_to_message"] = {"message_id": thread, "date": 0, "chat": m["chat"],
                                 "from": {"id": 9, "is_bot": True, "first_name": "bot"},
                                 "forum_topic_created": {"name": "t", "icon_color": 1}}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to, "date": 0, "chat": m["chat"],
                                 "from": {"id": 9, "is_bot": True, "first_name": "bot"}, "text": "x"}
    return update({"message": m})

def click(sender, data, message_id=1):
    return update({"callback_query": {"id": f"cb{uid[0]}", "from": {"id": sender, "is_bot": False, "first_name": "x", "username": f"adm{sender}"},
                                      "chat_instance": "ci", "data": data,
                                      "message": {"message_id": message_id, "date": 0, "chat": {"id": sender, "type": "private"}, "text": "n"}}})

def sends(out, chat=None):
    return [e for e in out if e[0] in ("send", "copy") and (chat is None or e[1] == chat)]
def answers(out):
    return [e for e in out if e[0] == "answer"]
def markup_json(entry):
    m = entry[3].get("reply_markup")
    return json.dumps(json.loads(m.to_json()), ensure_ascii=False) if m else ""

def register(user, nama, instansi, layanan):
    msg(user, "/start"); msg(user, "Menghubungi Admin"); msg(user, nama); msg(user, instansi)
    return msg(user, layanan)

U, V, W = 100, 200, 300
TOPICS = MODE == "topics"
ADMIN_CHAT = GROUP if TOPICS else 1

# ---- 6. jam layanan ----
check("Senin–Jumat, 08.00–16.00 WIB" in b.WELCOME_TEXT, "welcome shows service hours")
at = lambda *a: REAL_IS_OPEN(datetime(*a, tzinfo=b.TIMEZONE))
check(at(2026, 10, 19, 9, 0) and at(2026, 10, 23, 15, 59), "open Mon 09:00 and Fri 15:59")
check(not at(2026, 10, 19, 16, 0) and not at(2026, 10, 19, 7, 59), "closed at 16:00 and before 08:00")
check(not at(2026, 10, 24, 10, 0) and not at(2026, 10, 25, 10, 0), "closed on weekend")
check(not at(2026, 12, 25, 10, 0), "closed on LIBUR date")

# ---- 9. FAQ & 10. /batal ----
out = msg(U, "/start")
check("❓ FAQ" in markup_json(out[0]), "menu shows FAQ button when faq.json has entries")
out = msg(U, "❓ FAQ")
mk = markup_json(out[0])
check("faq:UMUM" in mk and "faq:TTIS" in mk and "PEMDI" not in mk and "_petunjuk" not in mk, "FAQ lists only non-empty sections")
out = click(U, "faq:UMUM")
faq_msg = sends(out, U)[0]
check("Apa &lt;b&gt;itu&lt;/b&gt;?" in faq_msg[2] and "&amp; &lt;script&gt;" in faq_msg[2] and faq_msg[3]["parse_mode"] == "HTML", "FAQ answer HTML-escaped")
out = msg(U, "/faq"); check("Pilih topik FAQ" in out[0][2], "/faq command works")
json.dump({"UMUM": [{"q": "Baru?", "a": "Ya"}]}, open(FAQ, "w", encoding="utf-8"))
out = click(U, "faq:UMUM"); check("Baru?" in sends(out, U)[0][2], "faq.json changes apply without restart")
out = click(U, "faq:TTIS"); check(answers(out) and "tidak ditemukan" in answers(out)[0][2], "removed FAQ section handled")
os.rename(FAQ, FAQ + ".off")
out = msg(U, "/start"); check("FAQ" not in markup_json(out[0]), "no FAQ file -> no FAQ button")
out = msg(U, "/faq"); check("belum tersedia" in out[0][2], "no FAQ file -> /faq says unavailable")
os.rename(FAQ + ".off", FAQ)

out = msg(U, "Menghubungi Admin"); check("/batal" in out[0][2], "registration prompt mentions /batal")
msg(U, "Budi")
out = msg(U, "/batal"); check("dibatalkan" in out[0][2] and b.db.get_step(U) is None, "/batal cancels registration")
out = msg(U, "/batal"); check("Tidak ada proses" in out[0][2], "/batal without registration")

# ---- 8. tiket & 13. topik ----
out = register(U, "Budi", "Kota Padang", "TTIS")
conf = sends(out, U)[-1]
check("Nomor tiket Anda: *#0001*" in conf[2], "user gets ticket number")
notice = [e for e in sends(out, ADMIN_CHAT) if "Permintaan Kontak Baru #0001" in e[2]]
check(len(notice) == 1, "admin notice includes ticket")
if TOPICS:
    topics = [e for e in out if e[0] == "topic"]
    check(len(topics) == 1 and topics[0][2] == "Budi · Kota Padang · TTIS", "topic created with user name")
    TOPIC_U = topics[0][4]
    check(notice[0][3].get("message_thread_id") == TOPIC_U, "notice posted inside user's topic")
    check("ketik langsung di topik" in notice[0][2], "notice explains typing in topic")
notice_mid = notice[0][4]

# ---- 11. tombol Balas & 12. penanda ----
out = msg(U, "halo")
fwd = sends(out, ADMIN_CHAT)[0]
if TOPICS:
    check(fwd[3].get("message_thread_id") == TOPIC_U and "reply:" not in markup_json(fwd), "user message goes to topic without Balas button")
    out = msg(GROUP, "siap membantu", sender=3, thread=TOPIC_U)
    check(any(e[1] == U and e[2] == "[Admin]: siap membantu\n\n-x" for e in sends(out)), "admin typing in topic reaches user")
    check(b.db.get_user(U)["admin_id"] == 3, "first admin typing in topic takes user")
    check(any(e[0] == "edit" and e[4] == notice_mid and "Diambil oleh adm3" in e[2] for e in out), "notice marked taken from topic")
    out = msg(GROUP, "saya bantu juga", sender=1, thread=TOPIC_U)
    check(any(e[1] == U for e in sends(out)) and b.db.get_user(U)["admin_id"] == 3, "other admin can also reply in topic; owner unchanged")
    out = msg(U, "terima kasih")
    check(sends(out) and all(e[1] == GROUP and e[3].get("message_thread_id") == TOPIC_U for e in sends(out)), "assigned user's messages still go to topic (not DM)")
    out = msg(GROUP, "/unknowncmd", sender=3, thread=TOPIC_U); check(not sends(out, U), "unknown command in topic not relayed")
    out = msg(GROUP, "bukan admin", sender=555, thread=TOPIC_U); check(not sends(out, U), "non-admin in topic ignored")
    # topik dihapus admin -> dibuat ulang
    DELETED_THREADS.add(TOPIC_U)
    out = msg(U, "masih ada?")
    new_topics = [e for e in out if e[0] == "topic"]
    check(len(new_topics) == 1 and any(e[3].get("message_thread_id") == new_topics[0][4] for e in sends(out, GROUP)), "deleted topic is recreated")
    TOPIC_U = new_topics[0][4]
    check(b.db.topic_user(TOPIC_U) == U, "new topic mapped to user")
    out = msg(GROUP, "/end", sender=3, thread=TOPIC_U)
    check(any(e[1] == GROUP and "diakhiri" in e[2] and e[3].get("message_thread_id") == TOPIC_U for e in sends(out)), "/end inside topic ends that user, reply in same topic")
    rating_prompt = [e for e in sends(out, U) if "beri nilai" in e[2]]
    out = register(U, "Budi", "Kota Bukittinggi", "PEMDI")
    check(any(e[0] == "rename" and e[2] == "Budi · Kota Bukittinggi · PEMDI" for e in out), "topic renamed on re-registration")
else:
    check(f"reply:{U}" in markup_json(fwd), "forwarded user message has 💬 Balas button")
    out = click(1, f"reply:{U}")
    check(b.db.get_user(U)["admin_id"] == 1 and b.db.active_user(1) == U, "Balas takes user and makes them active")
    ind = [e for e in sends(out, 1) if "Sekarang bicara dengan: Budi (Kota Padang · TTIS · #0001)" in e[2]]
    check(len(ind) == 1 and "/end_100" in ind[0][2], "active indicator shown after Balas")
    check(any(e[0] == "edit" and e[4] == notice_mid for e in out), "Balas marks notice taken")
    out = click(1, f"reply:{U}"); check("sudah bicara" in answers(out)[0][2] and not sends(out, 1), "Balas again: no duplicate indicator")
    out = click(3, f"reply:{U}"); check(answers(out)[0][3]["show_alert"] and "/talk_100" in answers(out)[0][2], "Balas by other admin refused with takeover hint")
    out = click(55, f"reply:{U}"); check("tidak diizinkan" in answers(out)[0][2], "Balas by non-admin refused")
    out = msg(1, "jawaban"); check(sends(out, U)[0][2] == "[Admin]: jawaban\n\n-ao", "after Balas plain text goes to user, signed with configured initials")
    def media(kind, extra):
        m = {"message_id": 9000 + uid[0], "date": int(time.time()), "chat": {"id": 1, "type": "private"},
             "from": {"id": 1, "is_bot": False, "first_name": "Alfido", "last_name": "Putra"}, **extra}
        return update({"message": m})
    photo = [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}]
    out = media("photo", {"photo": photo, "caption": "lihat ini", "caption_entities": [{"type": "bold", "offset": 0, "length": 5}]})
    c = sends(out, U)[0]
    check(c[0] == "copy" and c[3]["caption"] == "lihat ini\n\n-ao" and c[3]["caption_entities"][0].type == "bold", "photo caption signed, formatting kept")
    out = media("photo", {"photo": photo}); check(sends(out, U)[0][3]["caption"] == "-ao", "photo without caption gets initials as caption")
    out = media("photo", {"photo": photo, "caption": "x" * 1023}); check(sends(out, U)[0][3]["caption"] == "x" * 1023, "caption at limit sent without initials")
    out = media("sticker", {"sticker": {"file_id": "s", "file_unique_id": "s", "type": "regular", "width": 1, "height": 1, "is_animated": False, "is_video": False}})
    check(sends(out, U) and "caption" not in sends(out, U)[0][3], "sticker forwarded without caption")
    class Fake: pass
    a = Fake(); a.id, a.first_name, a.last_name, a.username = 99, "Budi Santoso", "Wijaya", None
    check(b.admin_initials(a) == "bsw", "auto initials from Telegram name")
    a.first_name, a.last_name, a.username = "", None, "Rina_x"
    check(b.admin_initials(a) == "ri", "auto initials fall back to username")
    a.id = 3; a.first_name = "Zed"
    check(b.admin_initials(a) == "z", "empty configured initials fall back to name")
    register(V, "Sari", "Kab Agam", "PEMDI")
    fwdV = sends(msg(V, "tolong"), 1)[0]
    out = click(1, f"reply:{V}")
    ind = [e for e in sends(out, 1) if "Sekarang bicara dengan: Sari" in e[2]]
    check(ind and "Budi | 100" in ind[0][2], "indicator mentions previous user when switching")
    out = msg(1, f"/talk {U}"); check(any("Sekarang bicara dengan: Budi" in e[2] for e in sends(out, 1)), "/talk shows indicator")
    out = msg(1, "/end")
    rating_prompt = [e for e in sends(out, U) if "beri nilai" in e[2]]

# ---- 7. penilaian ----
check(len(rating_prompt) == 1 and "#0001" in rating_prompt[0][2] and "rate:1:5" in markup_json(rating_prompt[0]), "rating prompt after /end")
check(b.db.get_request(1)["ended_at"] is not None, "request marked ended")
out = click(V, "rate:1:2"); check("sudah tercatat" in answers(out)[0][2] and b.db.get_request(1)["rating"] is None, "other user cannot rate someone else's ticket")
out = click(U, "rate:1:4")
check(b.db.get_request(1)["rating"] == 4, "rating saved")
check(any(e[0] == "edit" and "⭐⭐⭐⭐☆" in e[2] for e in out), "rating message updated with stars")
taker = 3 if TOPICS else 1
check(any(e[1] == taker and "nilai 4/5" in e[2] for e in sends(out)), "handling admin informed of rating")
out = click(U, "rate:1:1"); check("sudah tercatat" in answers(out)[0][2] and b.db.get_request(1)["rating"] == 4, "cannot rate twice")
out = msg(1, "/stats"); check("⭐ 4.0 dari 1 penilaian" in out[0][2], "/stats shows average rating")
out = msg(1, "/list semua"); check("Tiket: #0001" in out[0][2] or "Tiket: #0002" in out[0][2], "/list shows ticket")

# ---- 6. di luar jam layanan ----
b.is_open = lambda now=None: False
out = register(W, "Andi", "Kab Solok", "MANRISK")
check(b.CLOSED_NOTICE in sends(out, W)[-1][2], "out-of-hours notice on registration")
check(any("Permintaan Kontak Baru" in e[2] for e in sends(out)), "admins still notified out of hours")

# ---- 5. pengingat ----
b.is_open = REAL_IS_OPEN
w_req = b.db.latest_request(W)
monday_10 = int(datetime(2026, 10, 19, 10, 0, tzinfo=b.TIMEZONE).timestamp())
b.db.conn.execute("UPDATE requests SET created_at = ? WHERE id = ?", (monday_10 - 20 * 60, w_req["id"]))
b.db.conn.execute("UPDATE requests SET taken_by = 1 WHERE id != ?", (w_req["id"],))  # isolasi permintaan yang diuji
log.clear(); n = b.send_reminders(monday_10)
rem = [e for e in sends(log) if "Pengingat" in e[2]]
check(n == 1 and len(rem) >= 1 and b.ticket(w_req["id"]) in rem[0][2], "reminder sent for request untaken >15 min")
check("20 menit" in rem[0][2] and f"take:{W}" in markup_json(rem[0]), "reminder shows wait time and Ambil button")
check({e[1] for e in rem} == ({GROUP} if TOPICS else {1, 3}) and "message_thread_id" not in rem[0][3], "reminder goes to layanan admins / group general")
log.clear(); check(b.send_reminders(monday_10 + 60) == 0, "no repeat reminder within interval")
check(b.send_reminders(monday_10 + 16 * 60) == 1 and b.send_reminders(monday_10 + 32 * 60) == 1, "repeat reminders after interval")
check(b.send_reminders(monday_10 + 48 * 60) == 0, "max 3 reminders")
saturday = int(datetime(2026, 10, 24, 10, 0, tzinfo=b.TIMEZONE).timestamp())
b.db.conn.execute("UPDATE requests SET reminders = 0, reminded_at = NULL WHERE id = ?", (w_req["id"],))
check(b.send_reminders(saturday) == 0, "no reminders outside service hours")
log.clear(); b.send_reminders(monday_10)
rem_mid = [e for e in sends(log) if "Pengingat" in e[2]][0][4]
out = click(1, f"take:{W}")
check(any(e[0] == "edit" and e[4] == rem_mid and "Diambil oleh adm1" in e[2] for e in out), "taking user marks reminder as taken too")
check(b.send_reminders(monday_10 + 3600) == 0, "no reminders once taken")

# ---- format durasi ----
check(b.format_duration(65 * 60) == "1 jam 5 menit" and b.format_duration(26 * 3600) == "1 hari 2 jam" and b.format_duration(30) == "0 menit", "duration formatting")


# ---- daftar ulang: tiket lama ditutup (kasus "nyangkut") ----
b.is_open = lambda now=None: True
X = 400
out = register(X, "testing", "kota padang", "TTIS")
first = b.db.latest_request(X)["id"]
first_notice = [e for e in sends(out) if "Permintaan Kontak Baru" in e[2]]
out = register(X, "keit", "padang", "PTKKSS")
second = b.db.latest_request(X)["id"]
check(b.db.get_request(first)["cancelled_at"] is not None and b.db.get_request(second)["cancelled_at"] is None, "re-registration closes previous untaken ticket")
check(any(e[0] == "edit" and e[4] == first_notice[0][4] and f"Digantikan oleh tiket {b.ticket(second)}" in e[2] for e in out), "old notice marked as superseded")
b.db.conn.execute("UPDATE requests SET created_at = ?, reminders = 0, reminded_at = NULL WHERE user_id = ?", (monday_10 - 20 * 60, X))
log.clear(); b.send_reminders(monday_10)
rem = [e[2] for e in sends(log) if "Pengingat" in e[2]]
check(any(b.ticket(second) in r for r in rem) and not any(b.ticket(first) in r for r in rem), "only latest ticket is reminded")
stats = {r["layanan"]: r for r in b.db.stats()}
check(stats.get("TTIS") is None or all(r["user_id"] != X for r in b.db.export_rows("TTIS")), "superseded ticket excluded from stats/export")
out = click(1, f"take:{X}")
check(b.db.get_request(second)["taken_by"] == 1 and b.db.get_request(first)["taken_by"] is None, "taking user only takes the open ticket")

# ---- akun admin tidak bisa membuat tiket ----
out = msg(1, "Menghubungi Admin")
check("terdaftar sebagai admin" in out[0][2] and b.db.get_step(1) is None, "admin cannot start registration")
b.db.touch(3, 0); b.db.update_user(3, nama="adm", instansi="x", layanan="TTIS")
b.db.conn.execute("INSERT INTO requests (user_id, nama, layanan, created_at) VALUES (3, 'adm', 'TTIS', 1)")
check(b.db.cancel_open_requests(b.ADMINS, 5) == 1, "startup closes open tickets of admin accounts")
out = msg(1, "/list semua"); check("ID: 3 " not in out[0][2], "admins hidden from /list")

# ---- perbaikan data lama saat startup ----
import sqlite3 as _sq
from pathlib import Path as _P
legacy = f"{WORK}/legacy.db"
c = _sq.connect(legacy); c.executescript("""
CREATE TABLE requests (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, nama TEXT, instansi TEXT, layanan TEXT, created_at INTEGER NOT NULL, taken_by INTEGER, taken_at INTEGER);
INSERT INTO requests (user_id, nama, layanan, created_at) VALUES (7, 'a', 'TTIS', 1), (7, 'b', 'PTKKSS', 2), (7, 'c', 'PEMDI', 3), (8, 'd', 'TTIS', 4);
"""); c.commit(); c.close()
st = b.Storage(_P(legacy))
open_ids = [r["id"] for r in st.conn.execute("SELECT id FROM requests WHERE cancelled_at IS NULL")]
check(open_ids == [3, 4], "startup closes stale duplicate tickets from older versions")

# ---- pengingat gagal tidak dicatat & tidak dikirim ulang ----
import requests as _rq
calls = []
orig = b.bot.send_message
def flaky(chat_id, text, **kw):
    calls.append(chat_id)
    raise _rq.exceptions.ReadTimeout("timeout")
Y = 500
register(Y, "Nina", "Kab Z", "MANRISK")
b.db.conn.execute("UPDATE requests SET created_at = ? WHERE user_id = ?", (monday_10 - 20 * 60, Y))
b.db.conn.execute("UPDATE requests SET taken_by = 1 WHERE user_id != ?", (Y,))
b.bot.send_message = flaky
b.send_reminders(monday_10)
b.bot.send_message = orig
check(len(calls) == len(set(calls)), "reminder not re-sent on timeout (no duplicate)")
check(b.db.latest_request(Y)["reminders"] == 0, "failed reminder not counted; retried next round")
log.clear(); b.send_reminders(monday_10 + 60)
check(any("Pengingat" in e[2] for e in sends(log)), "reminder delivered on next round")


# ---- cadangan database harian ----
import sqlite3 as _sq2
b.BACKUP_DIR = Path(WORK) / "backups"
b.BACKUP_KEEP = 3
for d in ("20260101", "20260102", "20260103", "20260104"):
    (b.BACKUP_DIR).mkdir(parents=True, exist_ok=True)
    (b.BACKUP_DIR / f"bot-{d}.db").write_bytes(b"old")
b.backup_db()
files = sorted(f.name for f in b.BACKUP_DIR.glob("bot-*.db"))
today = f"bot-{datetime.now(b.TIMEZONE):%Y%m%d}.db"
check(today in files and len(files) == 3 and "bot-20260101.db" not in files, "backup created and only newest BACKUP_KEEP kept")
c = _sq2.connect(b.BACKUP_DIR / today)
check(c.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == b.db.conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], "backup is a complete, readable copy")
c.close()
b.backup_db(); check(len(list(b.BACKUP_DIR.glob("bot-*.db"))) == 3 and not list(b.BACKUP_DIR.glob("*.tmp")), "same-day backup overwritten, no temp files left")

# ---- berhenti bersih (docker stop / SIGTERM) ----
try:
    b.handle_sigterm(15, None); stopped = False
except SystemExit as e:
    stopped = e.code == 0
check(stopped, "SIGTERM turns into clean exit")

# ---- cache FAQ ----
reads = []
_orig_read = Path.read_text
def counting_read(self, *a, **k):
    if self == b.FAQ_FILE: reads.append(1)
    return _orig_read(self, *a, **k)
Path.read_text = counting_read
b.load_faq(); b.load_faq(); b.load_faq()
check(len(reads) == 0, "FAQ not re-read while file unchanged")
json.dump({"UMUM": [{"q": "Cache?", "a": "Diperbarui"}], "TTIS": [{"q": "x", "a": "y"}]}, open(FAQ, "w", encoding="utf-8"))
faq = b.load_faq()
check(len(reads) == 1 and faq["UMUM"][0]["q"] == "Cache?", "FAQ re-read after file changes")
Path.read_text = _orig_read

sys.exit(1 if failed else 0)
