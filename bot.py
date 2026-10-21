"""Bot Telegram helpdesk Percepatan BSSN.

Meneruskan percakapan antara user dan admin.
Konfigurasi lewat environment variable (lihat .env.example).
"""
import html
import io
import json
import logging
import os
import re
import signal
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import telebot
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font
from telebot import apihelper
from telebot.apihelper import ApiTelegramException
from telebot.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, KeyboardButton,
    Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)

from storage import Storage
from whatsapp import WahaNotifier

if not os.getenv("BOT_NO_DOTENV"):  # pengujian mematikan .env agar tidak memakai token/nomor WA sungguhan
    load_dotenv()

# Katalog layanan yang ditampilkan saat /start. Urutan di sini = urutan tombol pilihan layanan.
# TODO: periksa kembali deskripsi berikut sebelum dipakai.
LAYANAN_KATALOG = {
    "PTKKSS": "Pengukuran Tingkat Kematangan Keamanan Siber dan Sandi di instansi Anda.",
    "TTIS": "Pembentukan dan pendampingan Tim Tanggap Insiden Siber (TTIS).",
    "MANRISK": "Pendampingan penerapan manajemen risiko keamanan informasi.",
    "PEMDI": "Konsultasi keamanan siber untuk penyelenggaraan pemerintahan digital.",
    "PENGADUAN": "Penyampaian pengaduan, kendala, atau masukan terkait layanan.",
}
LAYANAN = list(LAYANAN_KATALOG)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Environment variable {name} wajib diisi (lihat .env.example).")
    return value


def parse_ids(text: str) -> set[int]:
    return {int(x) for x in text.replace(",", " ").split()}


def parse_layanan_admins(text: str) -> dict[str, frozenset[int]]:
    """Format: "TTIS=111,222; MANRISK=333"."""
    result = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        name, _, ids = part.partition("=")
        name = name.strip().upper()
        if name not in LAYANAN:
            raise SystemExit(f"LAYANAN_ADMINS: layanan '{name}' tidak dikenal (pilihan: {', '.join(LAYANAN)}).")
        result[name] = frozenset(parse_ids(ids))
    return result


def parse_initials(text: str) -> dict[int, str]:
    """Format: "1154578221=ao; 28193590=bs"."""
    result = {}
    for part in text.split(";"):
        if not part.strip():
            continue
        admin_id, _, initials = part.partition("=")
        try:
            result[int(admin_id)] = initials.strip()
        except ValueError:
            raise SystemExit(f"ADMIN_INITIALS tidak valid: '{part.strip()}' (contoh: 1154578221=ao).")
    return result


BOT_TOKEN = require_env("BOT_TOKEN")
ADMIN_INITIALS = parse_initials(os.getenv("ADMIN_INITIALS", ""))
LAYANAN_ADMINS = parse_layanan_admins(os.getenv("LAYANAN_ADMINS", ""))
GENERAL_ADMINS = frozenset(parse_ids(os.getenv("ADMIN_IDS", "")))
ADMINS = GENERAL_ADMINS.union(*LAYANAN_ADMINS.values())
if not ADMINS:
    raise SystemExit("Isi ADMIN_IDS dan/atau LAYANAN_ADMINS (lihat .env.example).")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID") or 0) or None
DB_FILE = Path(os.getenv("DB_FILE", "data/bot.db"))
TIMEZONE = ZoneInfo(os.getenv("BOT_TIMEZONE", "Asia/Jakarta"))
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()  # mis. http://proxy.kantor:8080
WORKERS = int(os.getenv("BOT_WORKERS", "4"))
CLEANUP_DAYS = int(os.getenv("CLEANUP_DAYS", "30"))  # 0 = nonaktif
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "7"))     # jumlah cadangan harian yang disimpan; 0 = nonaktif
BACKUP_DIR = DB_FILE.parent / "backups"
ADMIN_GROUP_TOPICS = os.getenv("ADMIN_GROUP_TOPICS", "").strip().lower() in ("1", "true", "ya", "yes")
if ADMIN_GROUP_TOPICS and not ADMIN_GROUP_ID:
    raise SystemExit("ADMIN_GROUP_TOPICS membutuhkan ADMIN_GROUP_ID.")
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "15"))  # 0 = nonaktif
REMINDER_MAX = int(os.getenv("REMINDER_MAX", "3"))
FAQ_FILE = Path(os.getenv("FAQ_FILE") or Path(__file__).with_name("faq.json"))


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    return default if not value else value in ("1", "true", "ya", "yes")


# Notifikasi WhatsApp lewat WAHA (opsional; aktif bila WAHA_URL dan WA_TARGETS diisi).
wa = WahaNotifier(
    url=os.getenv("WAHA_URL", ""),
    api_key=os.getenv("WAHA_API_KEY", "").strip(),
    session=os.getenv("WAHA_SESSION", "default").strip() or "default",
    targets=os.getenv("WA_TARGETS", "").split(","),
)
if os.getenv("WAHA_URL", "").strip() and not wa.targets:
    raise SystemExit("WA_TARGETS wajib diisi jika WAHA_URL diisi (lihat .env.example).")
WA_DETAIL = env_flag("WA_DETAIL")        # sertakan nama & instansi user di pesan WhatsApp
WA_REMINDERS = env_flag("WA_REMINDERS")  # kirim juga pengingat tiket yang belum diambil
BOT_USERNAME: str | None = None          # diisi saat bot berjalan, untuk tautan t.me di pesan WhatsApp


def parse_jam(text: str) -> tuple[dtime, dtime] | None:
    """Format "08:00-16:00"; kosong = layanan 24 jam."""
    if not text.strip():
        return None
    start, _, end = text.partition("-")
    try:
        return dtime.fromisoformat(start.strip()), dtime.fromisoformat(end.strip())
    except ValueError:
        raise SystemExit(f"JAM_LAYANAN tidak valid: '{text}' (contoh: 08:00-16:00).")


def parse_hari(text: str) -> frozenset[int]:
    """Format "1-5" atau "1,2,3,4,5" (1 = Senin ... 7 = Minggu)."""
    days = set()
    for part in text.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            days.update(range(int(a), int(b) + 1))
        elif part:
            days.add(int(part))
    if not days or not days <= set(range(1, 8)):
        raise SystemExit(f"HARI_LAYANAN tidak valid: '{text}' (contoh: 1-5).")
    return frozenset(days)


JAM_LAYANAN = parse_jam(os.getenv("JAM_LAYANAN", "08:00-16:00"))
HARI_LAYANAN = parse_hari(os.getenv("HARI_LAYANAN", "1-5"))
LIBUR = frozenset(date.fromisoformat(d.strip()) for d in os.getenv("LIBUR", "").split(",") if d.strip())
NAMA_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

NETWORK_ATTEMPTS = 2
POLLING_RETRY_DELAY = 5
CLEANUP_INTERVAL = 24 * 3600
BACKUP_INTERVAL = 6 * 3600  # dicek tiap 6 jam; tetap satu file per hari (file hari ini ditimpa)
BTN_ADMIN = "Menghubungi Admin"
BTN_FAQ = "❓ FAQ"
REMINDER_INTERVAL = 60
REMINDER_MAX_AGE = 7 * 86400  # permintaan lebih lama dari ini tidak lagi diingatkan
MAX_MESSAGE_LEN = 4000  # batas Telegram 4096, sisakan ruang
MAX_CAPTION_LEN = 1024
CAPTION_TYPES = {"photo", "video", "document", "audio", "voice", "animation"}
RELAY_TYPES = [
    "text", "photo", "document", "video", "audio", "voice",
    "video_note", "sticker", "animation", "location", "contact",
]
ADMIN_HELP = (
    "Perintah admin:\n"
    "/list [layanan] - user yang sedang aktif\n"
    "/list semua [layanan] - semua user yang pernah mendaftar\n"
    "/stats - jumlah permintaan per layanan\n"
    "/export [layanan] - unduh rekap Excel\n"
    "/wa - cek & tes notifikasi WhatsApp\n"
    "/talk <user_id> - mulai percakapan dengan user\n"
    "/end [user_id] - akhiri percakapan\n\n"
    "Tips:\n"
    "• Tekan 💬 Balas di pesan user, lalu ketik balasan seperti chat biasa.\n"
    "• Reply pesan dari user untuk langsung membalas user tersebut.\n"
    "• Di /list, sentuh /talk_… untuk langsung mengambil user."
    + ("\n• Di grup admin, ketik langsung di topik user untuk membalas; /end di topik mengakhiri percakapan."
       if ADMIN_GROUP_TOPICS else "")
)
WARN_UNDELIVERED = "⚠️ Pesan tidak terkirim ke user (mungkin user memblokir bot)."


def jam_description() -> str | None:
    if JAM_LAYANAN is None:
        return None
    days = sorted(HARI_LAYANAN)
    if len(days) > 1 and days == list(range(days[0], days[-1] + 1)):
        hari = f"{NAMA_HARI[days[0] - 1]}–{NAMA_HARI[days[-1] - 1]}"
    else:
        hari = ", ".join(NAMA_HARI[d - 1] for d in days)
    start, end = JAM_LAYANAN
    return f"{hari}, {start:%H.%M}–{end:%H.%M} {datetime.now(TIMEZONE).tzname()}"


def is_open(now: datetime | None = None) -> bool:
    """Apakah sekarang termasuk jam layanan."""
    if JAM_LAYANAN is None:
        return True
    now = now or datetime.now(TIMEZONE)
    return (now.isoweekday() in HARI_LAYANAN and now.date() not in LIBUR
            and JAM_LAYANAN[0] <= now.time() < JAM_LAYANAN[1])


KATALOG_TEXT = (
    "📚 <b>Katalog Layanan</b>\n\n"
    + "\n\n".join(f"<b>{i}. {name}</b>\n{desc}" for i, (name, desc) in enumerate(LAYANAN_KATALOG.items(), 1))
)
WELCOME_TEXT = (
    "Selamat datang di layanan helpdesk Percepatan BSSN 👋\n\n"
    f"{KATALOG_TEXT}\n\n"
    + (f"🕗 <b>Jam layanan:</b> {jam_description()}\n\n" if JAM_LAYANAN else "")
    + f"Untuk berkonsultasi, tekan tombol <b>{BTN_ADMIN}</b> di bawah. "
    f"Pertanyaan umum bisa dilihat lewat tombol <b>{BTN_FAQ}</b>. "
    "Ketik /layanan untuk melihat katalog ini kembali."
)
CLOSED_NOTICE = (
    f"🕗 Saat ini di luar jam layanan ({jam_description()}). "
    "Permintaan Anda sudah tercatat dan akan ditanggapi pada jam layanan berikutnya."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("percepatan")
telebot.logger.propagate = False  # TeleBot punya handler sendiri; cegah log tercetak dua kali

apihelper.ENABLE_MIDDLEWARE = True
apihelper.CONNECT_TIMEOUT = 10  # default telebot 15/30 detik membuat bot terasa macet saat jaringan lambat
apihelper.READ_TIMEOUT = 15     # long polling punya timeout sendiri, tidak terpengaruh
if TELEGRAM_PROXY:
    apihelper.proxy = {"https": TELEGRAM_PROXY}

db = Storage(DB_FILE)
db.migrate_json(DB_FILE.with_name("state.json"))
db.cancel_open_requests(ADMINS, int(time.time()))  # tiket dari akun admin tidak bisa diambil siapa pun


class ChatDispatcher:
    """Proses update secara paralel antar-chat, tetapi tetap berurutan dalam satu chat."""

    def __init__(self, workers: int):
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="worker") if workers > 1 else None
        self.lock = threading.Lock()
        self.queues: dict[int, deque] = {}

    def submit(self, key: int, task):
        if self.pool is None:
            self._run(task)
            return
        with self.lock:
            queue = self.queues.get(key)
            if queue is not None:
                queue.append(task)
                return
            self.queues[key] = deque([task])
        self.pool.submit(self._drain, key)

    def _drain(self, key: int):
        while True:
            with self.lock:
                queue = self.queues[key]
                if not queue:
                    del self.queues[key]
                    return
                task = queue.popleft()
            self._run(task)

    @staticmethod
    def _run(task):
        try:
            task()
        except Exception:
            log.exception("Error saat memproses update")

    def shutdown(self):
        if self.pool:
            self.pool.shutdown(wait=True)


def is_allowed_chat(chat) -> bool:
    return chat.type == "private" or chat.id == ADMIN_GROUP_ID


class HelpdeskBot(telebot.TeleBot):
    def process_new_updates(self, updates: list[Update]):
        for update in updates:
            # Geser offset di sini; telebot baru melakukannya saat update diproses di worker.
            self.last_update_id = max(self.last_update_id, update.update_id)
            if update.message:
                if not is_allowed_chat(update.message.chat):
                    continue
                key = update.message.chat.id
            elif update.callback_query:
                key = update.callback_query.from_user.id
            else:
                continue
            dispatcher.submit(key, partial(super().process_new_updates, [update]))


dispatcher = ChatDispatcher(WORKERS)
fanout = ThreadPoolExecutor(8, thread_name_prefix="fanout")
bot = HelpdeskBot(BOT_TOKEN, threaded=False)


# === HELPERS ===

def call_api(target, fn, retry: bool = True):
    """Panggil API Telegram; kegagalan dicatat dan tidak menghentikan handler.

    Gangguan jaringan dicoba ulang sekali (bisa menyebabkan pesan ganda bila
    percobaan pertama ternyata sampai, lebih baik daripada pesan hilang).
    retry=False untuk pesan yang lebih baik ditunda daripada terkirim ganda.
    """
    for attempt in range(1, (NETWORK_ATTEMPTS if retry else 1) + 1):
        try:
            return fn()
        except ApiTelegramException as e:
            log.warning("Gagal kirim ke %s: %s", target, e.description)
            return None
        except requests.RequestException as e:
            log.warning("Gangguan jaringan saat kirim ke %s (percobaan %d): %s", target, attempt, type(e).__name__)
            if retry and attempt < NETWORK_ATTEMPTS:
                time.sleep(2)
    return None


def send(chat_id: int, text: str, retry: bool = True, **kwargs) -> Message | None:
    return call_api(chat_id, lambda: bot.send_message(chat_id, text, **kwargs), retry=retry)


def copy(chat_id: int, message: Message, **kwargs):
    return call_api(chat_id, lambda: bot.copy_message(chat_id, message.chat.id, message.message_id, **kwargs))


def broadcast(targets, fn) -> list:
    """Jalankan fn(target) untuk banyak target sekaligus."""
    targets = list(targets)
    if len(targets) <= 1:
        return [fn(t) for t in targets]
    return list(fanout.map(fn, targets))


def send_chunked(chat_id: int, header: str, entries: list[str]):
    chunk = header
    for entry in entries:
        if len(chunk) + len(entry) + 2 > MAX_MESSAGE_LEN:
            send(chat_id, chunk)
            chunk = ""
        chunk = f"{chunk}\n\n{entry}" if chunk else entry
    send(chat_id, chunk)


def menu_markup() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    buttons = [KeyboardButton(BTN_ADMIN)]
    if load_faq():
        buttons.append(KeyboardButton(BTN_FAQ))
    markup.add(*buttons)
    return markup


def inline_button(text: str, data: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton(text, callback_data=data))
    return markup


def ticket(request_id: int | None) -> str:
    return f"#{request_id:04d}" if request_id else "-"


_faq_cache: tuple[tuple, dict] = ((), {})


def load_faq() -> dict[str, list[dict]]:
    """Isi faq.json; dibaca ulang hanya bila file berubah, jadi perubahan FAQ tetap berlaku tanpa restart."""
    global _faq_cache
    try:
        stat = FAQ_FILE.stat()
    except OSError:
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    if _faq_cache[0] == key:
        return _faq_cache[1]
    try:
        data = json.loads(FAQ_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("Gagal membaca %s", FAQ_FILE)
        return {}
    order = ["UMUM", *LAYANAN]
    sections = {k.upper(): v for k, v in data.items() if not k.startswith("_") and v}
    faq = {k: sections[k] for k in sorted(sections, key=lambda k: order.index(k) if k in order else len(order))}
    _faq_cache = (key, faq)
    return faq


def topic_name(user) -> str:
    parts = [user["nama"] or f"User {user['user_id']}", user["instansi"], user["layanan"]]
    return " · ".join(p for p in parts if p)[:128]


topic_lock = threading.Lock()


def ensure_topic(user_id: int, recreate: bool = False) -> int | None:
    """Topik forum milik user di grup admin; dibuat bila belum ada."""
    with topic_lock:
        user = db.get_user(user_id)
        if user["topic_id"] and not recreate:
            return user["topic_id"]
        topic = call_api(ADMIN_GROUP_ID, lambda: bot.create_forum_topic(ADMIN_GROUP_ID, topic_name(user)))
        if topic is None:
            return None
        db.set_topic(user_id, topic.message_thread_id)
        return topic.message_thread_id


def via_topic(target: int, user_id: int, fn) -> bool:
    """Jalankan fn(thread_id) di topik user bila target adalah grup forum; selain itu fn(None).

    Jika topik terhapus, topik dibuat ulang sekali; jika tetap gagal, kirim ke grup tanpa topik.
    """
    if not (ADMIN_GROUP_TOPICS and target == ADMIN_GROUP_ID):
        return fn(None)
    for recreate in (False, True):
        thread_id = ensure_topic(user_id, recreate=recreate)
        if thread_id is not None and fn(thread_id):
            return True
    return fn(None)


def thread_kwargs(thread_id: int | None) -> dict:
    return {"message_thread_id": thread_id} if thread_id else {}


def same_thread(message: Message) -> dict:
    """Agar balasan bot di grup forum muncul di topik yang sama dengan pesan admin."""
    if ADMIN_GROUP_TOPICS and message.chat.id == ADMIN_GROUP_ID and message.is_topic_message:
        return thread_kwargs(message.message_thread_id)
    return {}


def topic_user_of(message: Message) -> int | None:
    if ADMIN_GROUP_TOPICS and message.chat.id == ADMIN_GROUP_ID and message.is_topic_message:
        return db.topic_user(message.message_thread_id)
    return None


def active_indicator(user_id: int, previous: int | None = None) -> str:
    user = db.get_user(user_id)
    request = db.latest_request(user_id)
    detail = " · ".join(p for p in [user["instansi"], user["layanan"], request and ticket(request["id"])] if p)
    text = (
        f"💬 Sekarang bicara dengan: {user['nama'] or 'User'} ({detail})\n"
        f"Pesan biasa Anda akan dikirim ke user ini. Akhiri: /end_{user_id}"
    )
    if previous is not None:
        text += f"\n(User sebelumnya, {user_label(previous)}, tetap bisa dibalas lewat reply atau 💬 Balas.)"
    return text


def layanan_markup() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*(KeyboardButton(name) for name in LAYANAN))
    return markup


def show_menu(chat_id: int):
    send(chat_id, "Silakan pilih menu berikut:", reply_markup=menu_markup())


def format_time(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def user_label(user_id: int) -> str:
    user = db.get_user(user_id)
    nama = user["nama"] if user and user["nama"] else "User"
    return f"{nama} | {user_id}"


def command_arg(text: str) -> str | None:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else None


def parse_layanan_arg(message: Message) -> tuple[bool, str | None]:
    arg = command_arg(message.text)
    if arg is None:
        return True, None
    if arg.upper() not in LAYANAN:
        send(message.chat.id, f"Layanan tidak dikenal. Pilihan: {', '.join(LAYANAN)}")
        return False, None
    return True, arg.upper()


def admin_targets(layanan: str | None) -> list[int]:
    return sorted(LAYANAN_ADMINS.get(layanan) or GENERAL_ADMINS or ADMINS)


def target_candidates(layanan: str | None) -> list[list[int]]:
    """Tujuan pesan ke tim admin, berurutan: grup admin dulu, lalu admin per layanan."""
    candidates = [[ADMIN_GROUP_ID]] if ADMIN_GROUP_ID else []
    return candidates + [admin_targets(layanan)]


def deliver_to_admins(candidates: list[list[int]], fn) -> bool:
    """Coba tiap daftar tujuan sampai ada yang berhasil menerima."""
    return any(any(broadcast(targets, fn)) for targets in candidates)


def admin_only(handler):
    def wrapper(message: Message):
        if message.from_user.id not in ADMINS:
            if message.chat.type == "private":
                send(message.chat.id, "Anda tidak diizinkan.")
            return
        handler(message)
    return wrapper


@bot.middleware_handler(update_types=["message"])
def track_activity(bot_instance, message: Message):
    sender = message.from_user
    if sender and sender.id in ADMINS:
        db.set_admin_name(sender.id, sender.username or f"ID:{sender.id}")
    if message.chat.type == "private":
        db.touch(message.chat.id, message.date)


# === MENGAMBIL USER ===

def mark_notices(notices, status: str):
    """Tambahkan status di bawah notifikasi/pengingat dan hilangkan tombolnya."""
    def edit(notice):
        return call_api(notice["chat_id"], lambda: bot.edit_message_text(
            f"{notice['text']}\n\n{status}",
            chat_id=notice["chat_id"], message_id=notice["message_id"],
        ))
    broadcast(notices, edit)


def take_user(admin_id: int, user_id: int, *, takeover: bool, make_active: bool = True) -> tuple[bool, str]:
    """Tetapkan admin untuk user. Mengembalikan (berhasil, pesan untuk admin)."""
    user = db.get_user(user_id)
    if user is None or user_id in ADMINS:
        return False, "User ID tidak valid."
    current = user["admin_id"]
    if current not in (None, admin_id) and not takeover:
        return False, f"User ini sudah ditangani {db.admin_name(current)}. Gunakan /talk_{user_id} untuk mengambil alih."

    result = db.assign(user_id, admin_id, int(time.time()), make_active=make_active)
    admin_name = db.admin_name(admin_id)
    prev_admin = result["prev_admin"]
    if prev_admin not in (None, admin_id):
        send(prev_admin, f"Percakapan dengan user {user_label(user_id)} diambil alih oleh {admin_name}.")
    mark_notices(result["notices"], f"✅ Diambil oleh {admin_name}")
    if prev_admin != admin_id and not send(user_id, "Admin telah menghubungi Anda."):
        send(admin_id, WARN_UNDELIVERED)
    return True, active_indicator(user_id, previous=result["prev_active"])


def answer(call: CallbackQuery, text: str, alert: bool = False):
    call_api(call.from_user.id, lambda: bot.answer_callback_query(call.id, text[:200], show_alert=alert))


def admin_callback(handler):
    def wrapper(call: CallbackQuery):
        admin_id = call.from_user.id
        if admin_id not in ADMINS:
            answer(call, "Anda tidak diizinkan.")
            return
        db.set_admin_name(admin_id, call.from_user.username or f"ID:{admin_id}")
        handler(call, admin_id, int(call.data.split(":", 1)[1]))
    return wrapper


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("take:"))
@admin_callback
def on_take(call: CallbackQuery, admin_id: int, user_id: int):
    ok, text = take_user(admin_id, user_id, takeover=False)
    answer(call, "Berhasil diambil." if ok else text, alert=not ok)
    if ok:
        send(admin_id, text)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("reply:"))
@admin_callback
def on_reply_button(call: CallbackQuery, admin_id: int, user_id: int):
    if db.active_user(admin_id) == user_id:
        answer(call, "Anda sudah bicara dengan user ini. Ketik balasan Anda di chat pribadi dengan bot.")
        return
    ok, text = take_user(admin_id, user_id, takeover=False)
    if not ok:
        answer(call, text, alert=True)
        return
    answer(call, "Ketik balasan Anda di chat pribadi dengan bot.")
    send(admin_id, text)


# === PERINTAH ===

@bot.message_handler(commands=["start"], chat_types=["private"])
def cmd_start(message: Message):
    if db.get_step(message.chat.id):
        db.update_user(message.chat.id, step=None)
    send(message.chat.id, WELCOME_TEXT, parse_mode="HTML", reply_markup=menu_markup())
    if message.chat.id in ADMINS:
        send(message.chat.id, ADMIN_HELP)


@bot.message_handler(commands=["layanan"], chat_types=["private"])
def cmd_layanan(message: Message):
    send(message.chat.id, KATALOG_TEXT, parse_mode="HTML")


@bot.message_handler(commands=["batal"], chat_types=["private"])
def cmd_batal(message: Message):
    if db.get_step(message.chat.id):
        db.update_user(message.chat.id, step=None)
        send(message.chat.id, "Pengisian data dibatalkan.", reply_markup=menu_markup())
    else:
        send(message.chat.id, "Tidak ada proses yang sedang berjalan.", reply_markup=menu_markup())


# === FAQ ===

@bot.message_handler(commands=["faq"], chat_types=["private"])
@bot.message_handler(func=lambda m: m.text == BTN_FAQ, chat_types=["private"])
def cmd_faq(message: Message):
    faq = load_faq()
    if not faq:
        send(message.chat.id, "FAQ belum tersedia.")
        return
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(*(InlineKeyboardButton(section.title() if section == "UMUM" else section, callback_data=f"faq:{section}")
                 for section in faq))
    send(message.chat.id, "❓ Pilih topik FAQ:", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("faq:"))
def on_faq(call: CallbackQuery):
    section = call.data.split(":", 1)[1]
    items = load_faq().get(section)
    answer(call, "" if items else "FAQ tidak ditemukan.")
    if not items:
        return
    title = "Umum" if section == "UMUM" else section
    body = "\n\n".join(
        f"<b>{i}. {html.escape(item.get('q', ''))}</b>\n{html.escape(item.get('a', ''))}" for i, item in enumerate(items, 1)
    )
    send(call.from_user.id,
         f"❓ <b>FAQ {html.escape(title)}</b>\n\n{body}\n\nBelum terjawab? Tekan <b>{BTN_ADMIN}</b>.",
         parse_mode="HTML")


# === PENILAIAN ===

def rating_markup(request_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=5)
    markup.add(*(InlineKeyboardButton(f"{n}⭐", callback_data=f"rate:{request_id}:{n}") for n in range(1, 6)))
    return markup


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("rate:"))
def on_rate(call: CallbackQuery):
    _, request_id, rating = call.data.split(":")
    request_id, rating = int(request_id), int(rating)
    if not 1 <= rating <= 5 or not db.set_rating(request_id, call.from_user.id, rating):
        answer(call, "Penilaian sudah tercatat sebelumnya.")
        return
    stars = "⭐" * rating + "☆" * (5 - rating)
    answer(call, "Terima kasih!")
    call_api(call.from_user.id, lambda: bot.edit_message_text(
        f"Terima kasih atas penilaian Anda untuk tiket {ticket(request_id)}: {stars}",
        chat_id=call.message.chat.id, message_id=call.message.message_id,
    ))
    request = db.get_request(request_id)
    if request["taken_by"]:
        send(request["taken_by"], f"⭐ {request['nama']} memberi nilai {rating}/5 untuk tiket {ticket(request_id)}.")


@bot.message_handler(commands=["help"])
@admin_only
def cmd_help(message: Message):
    send(message.chat.id, ADMIN_HELP)


@bot.message_handler(commands=["list"])
@admin_only
def cmd_list(message: Message):
    args = (command_arg(message.text) or "").upper().split()
    show_all = "SEMUA" in args
    layanan = next((a for a in args if a != "SEMUA"), None)
    if layanan is not None and layanan not in LAYANAN:
        send(message.chat.id, f"Layanan tidak dikenal. Pilihan: {', '.join(LAYANAN)}")
        return

    users = [u for u in db.list_users(layanan, only_active=not show_all) if u["user_id"] not in ADMINS]
    if not users:
        send(message.chat.id, "Belum ada user." if show_all else
             "Tidak ada user yang sedang aktif. Ketik /list semua untuk melihat semua user.")
        return

    me = message.from_user.id
    entries = []
    for u in users:
        uid = u["user_id"]
        if u["admin_id"] == me:
            status, action = "💬 Ditangani Anda", f"Bicara: /talk_{uid}   Akhiri: /end_{uid}"
        elif u["admin_id"]:
            status = f"💬 Ditangani {u['admin_username'] or 'ID:' + str(u['admin_id'])}"
            action = f"Ambil alih: /talk_{uid}"
        elif u["menunggu"]:
            status, action = "⏳ Menunggu admin", f"Ambil: /talk_{uid}"
        else:
            status, action = "✅ Selesai", f"Hubungi lagi: /talk_{uid}"
        entries.append(
            f"ID: {uid}   Tiket: {ticket(u['tiket'])}\n"
            f"Nama: {u['nama']}\n"
            f"Instansi: {u['instansi'] or '-'}\n"
            f"Layanan: {u['layanan']}\n"
            f"Last Active: {format_time(u['last_active'])}\n"
            f"Status: {status}\n"
            f"👉 {action}"
        )
    title = "Semua User" if show_all else "User Aktif"
    header = f"📋 {title}{f' {layanan}' if layanan else ''} ({len(users)}):"
    send_chunked(message.chat.id, header, entries)


@bot.message_handler(commands=["stats"])
@admin_only
def cmd_stats(message: Message):
    rows = db.stats()
    if not rows:
        send(message.chat.id, "Belum ada permintaan.")
        return
    def rating_text(avg, count) -> str:
        return f"⭐ {avg:.1f} dari {count} penilaian" if count else "belum ada penilaian"

    lines = [
        f"• {r['layanan']}: {r['total']} permintaan, {r['terbuka']} belum diambil, {r['user']} user, "
        f"{rating_text(r['rating'], r['dinilai'])}"
        for r in rows
    ]
    total = sum(r["total"] for r in rows)
    terbuka = sum(r["terbuka"] for r in rows)
    dinilai = sum(r["dinilai"] for r in rows)
    avg = sum((r["rating"] or 0) * r["dinilai"] for r in rows) / dinilai if dinilai else 0
    send(message.chat.id, "📊 Statistik Permintaan:\n" + "\n".join(lines)
         + f"\n\nTotal: {total} permintaan, {terbuka} belum diambil, {rating_text(avg, dinilai)}")


def build_export(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Rekap"
    ws.append(["No", "Tiket", "Tanggal", "Nama", "Instansi", "Layanan", "User ID",
               "Diambil oleh", "Waktu diambil", "Selesai", "Penilaian"])
    for i, r in enumerate(rows, 1):
        taken_by = (r["admin_username"] or f"ID:{r['taken_by']}") if r["taken_by"] else "-"
        ws.append([i, ticket(r["id"]), format_time(r["created_at"]), r["nama"], r["instansi"], r["layanan"],
                   r["user_id"], taken_by, format_time(r["taken_at"]), format_time(r["ended_at"]),
                   r["rating"] if r["rating"] else "-"])
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type == "f":  # isian user yang diawali "=" jangan dibaca Excel sebagai rumus
                cell.data_type = "s"
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for column, width in zip("ABCDEFGHIJK", [5, 8, 20, 25, 35, 12, 14, 18, 20, 20, 10]):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@bot.message_handler(commands=["wa"])
@admin_only
def cmd_wa(message: Message):
    if not wa.enabled:
        send(message.chat.id, "Notifikasi WhatsApp belum aktif. Isi WAHA_URL dan WA_TARGETS di .env.")
        return
    status = wa.session_status()
    results = wa.broadcast("✅ Tes notifikasi WhatsApp dari bot helpdesk Percepatan BSSN.")
    lines = [f"Sesi WAHA '{wa.session}': {status or 'tidak bisa dihubungi'}",
             f"Pesan tes terkirim ke {sum(results)}/{len(results)} tujuan."]
    if status and status != "WORKING":
        lines.append("Buka dashboard WAHA lalu mulai sesi dan scan QR dengan WhatsApp.")
    send(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["export"])
@admin_only
def cmd_export(message: Message):
    ok, layanan = parse_layanan_arg(message)
    if not ok:
        return
    rows = db.export_rows(layanan)
    if not rows:
        send(message.chat.id, "Belum ada permintaan.")
        return
    data = build_export(rows)
    name = f"rekap_{layanan.lower() + '_' if layanan else ''}{datetime.now(TIMEZONE):%Y%m%d_%H%M}.xlsx"
    chat_id = message.chat.id
    call_api(chat_id, lambda: bot.send_document(
        chat_id, InputFile(io.BytesIO(data), file_name=name), caption=f"Rekap {len(rows)} permintaan",
    ))


def user_command(name: str):
    """Filter untuk /name, /name <id>, dan bentuk yang bisa disentuh /name_<id> (opsional @NamaBot)."""
    pattern = re.compile(rf"^/{name}(_\d+)?(@\w+)?(\s|$)")
    return lambda m: bool(m.text and pattern.match(m.text))


def user_command_arg(text: str) -> str | None:
    head, _, rest = text.partition(" ")
    head = head.split("@", 1)[0]
    if "_" in head:
        return head.split("_", 1)[1]
    return rest.strip() or None


@bot.message_handler(func=user_command("talk"))
@admin_only
def cmd_talk(message: Message):
    arg = user_command_arg(message.text)
    if arg is None or len(arg.split()) != 1:
        send(message.chat.id, "Gunakan format: /talk <user_id>")
        return
    try:
        user_id = int(arg)
    except ValueError:
        send(message.chat.id, "User ID tidak valid.")
        return
    _, text = take_user(message.from_user.id, user_id, takeover=True)
    send(message.chat.id, text)


@bot.message_handler(func=user_command("end"))
@admin_only
def cmd_end(message: Message):
    admin_id = message.from_user.id
    arg = user_command_arg(message.text)
    if arg is None:
        user_id = topic_user_of(message) or db.active_user(admin_id)
        if user_id is None:
            send(message.chat.id, "Anda tidak sedang dalam percakapan. Gunakan /end <user_id>.")
            return
    else:
        try:
            user_id = int(arg)
        except ValueError:
            send(message.chat.id, "User ID tidak valid.")
            return

    user = db.get_user(user_id)
    if user is None or user["admin_id"] != admin_id:
        send(message.chat.id, "Anda tidak sedang menangani user ini.", **same_thread(message))
        return
    db.release(user_id)
    request = db.end_request(user_id, int(time.time()))
    send(message.chat.id, f"Percakapan dengan {user_label(user_id)} diakhiri.", **same_thread(message))
    send(user_id, "Percakapan dengan admin telah berakhir.", reply_markup=menu_markup())
    if request is not None and request["rating"] is None:
        send(user_id, f"Bagaimana pelayanan kami untuk tiket {ticket(request['id'])}? Silakan beri nilai:",
             reply_markup=rating_markup(request["id"]))


# === MENU USER ===

@bot.message_handler(func=lambda m: m.text == BTN_ADMIN, chat_types=["private"])
def menu_hubungi_admin(message: Message):
    if message.chat.id in ADMINS:
        # Admin tidak bisa mengambil tiketnya sendiri, jadi tiket dari akun admin akan menggantung.
        send(message.chat.id, "Anda terdaftar sebagai admin, jadi tidak bisa membuat permintaan. "
                              "Untuk mencoba alur user, gunakan akun Telegram lain.\n\n" + ADMIN_HELP)
        return
    db.update_user(message.chat.id, step="nama")
    send(message.chat.id, "Silakan masukkan *Nama Anda:*\n(ketik /batal untuk membatalkan)",
         parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())


@bot.message_handler(func=lambda m: db.get_step(m.chat.id) is not None, chat_types=["private"])
def handle_data(message: Message):
    user_id = message.chat.id
    step = db.get_step(user_id)
    text = message.text.strip()

    if step == "nama":
        db.update_user(user_id, nama=text, step="instansi")
        send(user_id, "Silakan masukkan *Instansi Anda* (cth: Kota Padang, Sumatera Barat) :", parse_mode="Markdown")
        return

    if step == "instansi":
        db.update_user(user_id, instansi=text, step="layanan")
        send(user_id, "Silakan pilih *Layanan* yang Anda butuhkan:", parse_mode="Markdown", reply_markup=layanan_markup())
        return

    layanan = text.upper()
    if layanan not in LAYANAN:
        send(user_id, "Layanan tidak dikenal. Silakan pilih salah satu tombol di bawah:", reply_markup=layanan_markup())
        return

    request_id, superseded = db.complete_request(user_id, layanan, int(time.time()))
    mark_notices(superseded, f"↪️ Digantikan oleh tiket {ticket(request_id)}")
    notify_request(request_id, db.get_user(user_id))
    reply = f"Terima kasih! Data Anda sudah diteruskan ke admin.\nNomor tiket Anda: *{ticket(request_id)}*"
    if not is_open():
        reply += f"\n\n{CLOSED_NOTICE}"
    send(user_id, reply, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())


def notify_request(request_id: int, user):
    user_id = user["user_id"]
    text = (
        f"🔔 Permintaan Kontak Baru {ticket(request_id)}\n"
        f"Nama: {user['nama']}\nInstansi: {user['instansi']}\nLayanan: {user['layanan']}\nUserID: {user_id}\n\n"
        + ("Tekan Ambil, atau ketik langsung di topik ini untuk membalas." if ADMIN_GROUP_TOPICS
           else "Tekan Ambil, atau reply pesan ini untuk membalas.")
    )
    markup = inline_button("✋ Ambil", f"take:{user_id}")
    now = int(time.time())
    if ADMIN_GROUP_TOPICS and user["topic_id"]:
        # Data user bisa berubah saat mendaftar ulang; samakan nama topiknya.
        call_api(ADMIN_GROUP_ID, lambda: bot.edit_forum_topic(ADMIN_GROUP_ID, user["topic_id"], name=topic_name(user)))

    def notify(target: int) -> bool:
        def in_thread(thread_id):
            sent = send(target, text, reply_markup=markup, **thread_kwargs(thread_id))
            if sent is None:
                return False
            db.add_notice(request_id, target, sent.message_id, text)
            db.add_relay(target, sent.message_id, user_id, now)
            return True
        return via_topic(target, user_id, in_thread)

    if not deliver_to_admins(target_candidates(user["layanan"]), notify):
        log.error("Notifikasi permintaan %s tidak terkirim ke admin mana pun", ticket(request_id))
    wa.notify(wa_request_text(request_id, user))


def wa_link() -> str:
    return f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "bot helpdesk di Telegram"


def wa_request_text(request_id: int, user) -> str:
    """Pesan WhatsApp sengaja singkat: detail user tetap di Telegram kecuali WA_DETAIL diaktifkan."""
    lines = [f"🔔 *Permintaan baru {ticket(request_id)}* (layanan {user['layanan']})"]
    if WA_DETAIL:
        lines.append(f"{user['nama']} – {user['instansi']}")
    if not is_open():
        lines.append("Masuk di luar jam layanan.")
    lines.append(f"Silakan cek {wa_link()}")
    return "\n".join(lines)


# === RELAY PESAN ===

@bot.message_handler(content_types=RELAY_TYPES)
def relay(message: Message):
    is_admin = message.from_user.id in ADMINS
    if message.chat.type != "private":
        # Di grup admin, bot hanya menanggapi pesan admin di topik user atau reply ke pesan user.
        if not is_admin or (message.text or "").startswith("/"):
            return
        topic_user = topic_user_of(message)
        if topic_user is not None:
            relay_from_admin(message, topic_user=topic_user)
        elif message.reply_to_message:
            relay_from_admin(message)
        return
    if message.text and message.text.startswith("/"):
        send(message.chat.id, ADMIN_HELP if is_admin else "Perintah tidak dikenal. Ketik /start untuk menu.")
        return
    if is_admin:
        relay_from_admin(message)
    else:
        relay_from_user(message)


def relay_from_admin(message: Message, topic_user: int | None = None):
    admin_id = message.from_user.id
    chat_id = message.chat.id
    in_group = message.chat.type != "private"

    if topic_user is not None:
        # Topik adalah ruang bersama tim: admin mana pun boleh membalas.
        # User yang belum ditangani otomatis menjadi milik admin yang pertama membalas.
        user_id = topic_user
        if db.get_user(user_id)["admin_id"] is None:
            take_user(admin_id, user_id, takeover=False, make_active=False)
    elif message.reply_to_message:
        user_id = db.relay_user(chat_id, message.reply_to_message.message_id)
        if user_id is None:
            if not in_group:
                send(chat_id, "Pesan yang Anda reply bukan pesan dari user.")
            return
        user = db.get_user(user_id)
        if user is None:
            send(chat_id, "User tidak ditemukan (data mungkin sudah dibersihkan).")
            return
        if user["admin_id"] != admin_id:
            ok, text = take_user(admin_id, user_id, takeover=False, make_active=db.active_user(admin_id) is None)
            if not ok:
                send(chat_id, text)
                return
    else:
        user_id = db.active_user(admin_id)
        if user_id is None:
            send(chat_id, "Tekan 💬 Balas di pesan user, reply pesan user, atau gunakan /talk <user_id> untuk membalas.")
            return

    sign = signature(message.from_user)
    if message.content_type == "text":
        delivered = send(user_id, f"[Admin]: {message.text}{sign}") is not None
    elif message.content_type in CAPTION_TYPES:
        caption = f"{message.caption}{sign}" if message.caption else sign.strip()
        if len(caption) > MAX_CAPTION_LEN:
            caption = message.caption
        delivered = copy(user_id, message, caption=caption, caption_entities=message.caption_entities) is not None
    else:
        delivered = copy(user_id, message) is not None  # stiker, lokasi, dll. tidak punya keterangan
    if not delivered:
        send(chat_id, WARN_UNDELIVERED, **same_thread(message))


def admin_initials(user) -> str:
    """Inisial dari ADMIN_INITIALS; jika belum diatur, dari huruf depan nama Telegram admin."""
    if ADMIN_INITIALS.get(user.id):
        return ADMIN_INITIALS[user.id]
    words = f"{user.first_name or ''} {user.last_name or ''}".split()
    letters = "".join(w[0] for w in words if w[0].isalpha())[:3].lower()
    return letters or (user.username or "")[:2].lower() or "admin"


def signature(user) -> str:
    return f"\n\n-{admin_initials(user)}"


def relay_from_user(message: Message):
    user_id = message.chat.id
    user = db.get_user(user_id)
    if user is not None and user["step"]:
        send(user_id, "Silakan jawab pertanyaan sebelumnya dengan teks, atau ketik /batal.")
        return
    if user is None or (user["admin_id"] is None and user["layanan"] is None):
        show_menu(user_id)
        return

    candidates = target_candidates(user["layanan"])
    if user["admin_id"] is not None and not ADMIN_GROUP_TOPICS:
        # Di mode topik semua pesan user tetap masuk ke topiknya agar tim bisa melihat.
        candidates.insert(0, [user["admin_id"]])

    label = f"[{user['nama'] or 'User'} | {user_id}]"
    now = int(time.time())

    def forward(target: int) -> bool:
        def in_thread(thread_id):
            extra = thread_kwargs(thread_id)
            # Di topik, admin cukup mengetik langsung; tombol Balas hanya untuk chat pribadi / grup biasa.
            button = {} if thread_id else {"reply_markup": inline_button("💬 Balas", f"reply:{user_id}")}
            if message.content_type == "text":
                sent = [send(target, f"{label}: {message.text}", **extra, **button)]
            else:
                header = send(target, f"{label} mengirim {message.content_type}:", **extra, **button)
                sent = [header, copy(target, message, **extra) if header else None]
            for msg in sent:
                if msg is not None:
                    db.add_relay(target, msg.message_id, user_id, now)
            return sent[0] is not None
        return via_topic(target, user_id, in_thread)

    if not deliver_to_admins(candidates, forward):
        log.error("Pesan dari user %s tidak terkirim ke admin mana pun", user_id)


# === TUGAS LATAR ===

def format_duration(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    days, hours = divmod(hours, 24)
    parts = [f"{days} hari" if days else "", f"{hours} jam" if hours else "",
             f"{minutes} menit" if minutes or not (days or hours) else ""]
    return " ".join(p for p in parts if p)


def send_reminders(now: int | None = None) -> int:
    """Ingatkan admin tentang permintaan yang belum diambil (hanya di jam layanan)."""
    now = now or int(time.time())
    if not is_open(datetime.fromtimestamp(now, TIMEZONE)):
        return 0
    due = db.due_reminders(now - REMINDER_MINUTES * 60, now - REMINDER_MAX_AGE, REMINDER_MAX)
    for req in due:
        text = (
            f"⏰ Pengingat: tiket {ticket(req['id'])} dari {req['nama']} ({req['instansi']} · {req['layanan']}) "
            f"belum diambil selama {format_duration(now - req['created_at'])}."
        )
        markup = inline_button("✋ Ambil", f"take:{req['user_id']}")

        def remind(target: int, req=req, text=text, markup=markup) -> bool:
            # Tanpa kirim ulang: pengingat yang gagal dicoba lagi pada putaran berikutnya,
            # sedangkan kirim ulang saat koneksi lambat bisa membuat pengingat ganda.
            sent = send(target, text, retry=False, reply_markup=markup)
            if sent is None:
                return False
            db.add_notice(req["id"], target, sent.message_id, text)
            db.add_relay(target, sent.message_id, req["user_id"], now)
            return True

        if deliver_to_admins(target_candidates(req["layanan"]), remind):
            db.mark_reminded(req["id"], now)
            if WA_REMINDERS:
                wa.notify(f"⏰ Tiket {ticket(req['id'])} (layanan {req['layanan']}) belum diambil selama "
                          f"{format_duration(now - req['created_at'])}. Silakan cek {wa_link()}")
    return len(due)


def periodic(name: str, interval: int, fn):
    def loop():
        while True:
            try:
                fn()
            except Exception:
                log.exception("Tugas %s gagal", name)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name=name).start()


def cleanup():
    counts = db.cleanup(int(time.time()) - CLEANUP_DAYS * 86400, ADMINS)
    if any(counts.values()):
        log.info("Pembersihan data lama: %s", counts)


def backup_db():
    """Cadangan harian database (satu file per hari), simpan BACKUP_KEEP file terbaru."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    db.backup(BACKUP_DIR / f"bot-{datetime.now(TIMEZONE):%Y%m%d}.db")
    for old in sorted(BACKUP_DIR.glob("bot-*.db"))[:-BACKUP_KEEP]:
        old.unlink()


def handle_sigterm(signum, frame):
    # `docker stop` mengirim SIGTERM; ubah jadi keluar normal agar blok finally sempat berjalan.
    log.info("Menerima sinyal berhenti, menutup bot...")
    raise SystemExit(0)


# === MAIN ===

def run_polling():
    """Polling tanpa henti; gangguan jaringan dicatat satu baris lalu dicoba lagi."""
    while True:
        try:
            bot.polling(non_stop=True, allowed_updates=["message", "callback_query"], logger_level=None)
            return  # polling hanya selesai normal saat dihentikan (Ctrl+C)
        except requests.RequestException as e:
            log.warning("Koneksi ke Telegram terputus (%s), mencoba lagi dalam %d detik", type(e).__name__, POLLING_RETRY_DELAY)
        except Exception:
            log.exception("Error tak terduga, mencoba lagi dalam %d detik", POLLING_RETRY_DELAY)
        time.sleep(POLLING_RETRY_DELAY)


def check_forum_group():
    chat = call_api(ADMIN_GROUP_ID, lambda: bot.get_chat(ADMIN_GROUP_ID))
    if chat is not None and not chat.is_forum:
        log.error("ADMIN_GROUP_TOPICS aktif, tetapi grup %s belum mengaktifkan fitur Topik. "
                  "Pesan akan dikirim ke grup tanpa topik.", ADMIN_GROUP_ID)


if __name__ == "__main__":
    log.info(
        "Bot berjalan: %d admin, %d worker, grup admin %s%s, routing layanan %s, jam layanan %s, database %s%s",
        len(ADMINS), WORKERS, ADMIN_GROUP_ID or "-", " (topik per user)" if ADMIN_GROUP_TOPICS else "",
        ", ".join(LAYANAN_ADMINS) or "-", jam_description() or "24 jam", DB_FILE,
        " (via proxy)" if TELEGRAM_PROXY else "",
    )
    if ADMIN_GROUP_TOPICS:
        check_forum_group()
    me = call_api("getMe", bot.get_me)
    BOT_USERNAME = me.username if me else None
    if wa.enabled:
        status = wa.session_status()
        log.info("Notifikasi WhatsApp aktif ke %d tujuan, sesi WAHA: %s", len(wa.targets), status or "tidak bisa dihubungi")
        if status != "WORKING":
            log.warning("Sesi WAHA belum siap; buka dashboard WAHA, mulai sesi '%s' lalu scan QR.", wa.session)
    if CLEANUP_DAYS > 0:
        periodic("cleanup", CLEANUP_INTERVAL, cleanup)
    if REMINDER_MINUTES > 0:
        periodic("reminder", REMINDER_INTERVAL, send_reminders)
    if BACKUP_KEEP > 0:
        periodic("backup", BACKUP_INTERVAL, backup_db)
    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        run_polling()
    finally:
        dispatcher.shutdown()
        fanout.shutdown()
        wa.shutdown()
        log.info("Bot berhenti.")
