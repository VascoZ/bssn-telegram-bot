"""Penyimpanan SQLite untuk bot helpdesk.

Semua akses lewat satu koneksi yang dijaga lock, sehingga aman dipakai
oleh beberapa worker thread sekaligus.
"""
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    nama        TEXT,
    instansi    TEXT,
    layanan     TEXT,
    step        TEXT,               -- langkah pengisian data yang sedang ditunggu
    admin_id    INTEGER,            -- admin yang sedang menangani
    last_active INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins (
    admin_id    INTEGER PRIMARY KEY,
    username    TEXT,
    active_user INTEGER             -- tujuan pesan admin yang bukan reply
);
CREATE TABLE IF NOT EXISTS requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    nama       TEXT,
    instansi   TEXT,
    layanan    TEXT,
    created_at INTEGER NOT NULL,
    taken_by   INTEGER,
    taken_at   INTEGER
);
CREATE TABLE IF NOT EXISTS notices (  -- pesan notifikasi permintaan, untuk ditandai saat diambil
    request_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relay (    -- pesan di sisi admin -> user asalnya, untuk fitur reply
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_notices_request ON notices(request_id);
CREATE INDEX IF NOT EXISTS idx_relay_created ON relay(created_at);
"""

# Kolom yang ditambahkan setelah versi pertama; ditambahkan otomatis ke database lama.
ADDED_COLUMNS = {
    "users": {"topic_id": "INTEGER"},              # topik forum user di grup admin
    "requests": {
        "reminders": "INTEGER NOT NULL DEFAULT 0",  # jumlah pengingat yang sudah dikirim
        "reminded_at": "INTEGER",
        "ended_at": "INTEGER",
        "rating": "INTEGER",                       # penilaian user 1-5
        "cancelled_at": "INTEGER",                 # ditutup tanpa diambil (mis. digantikan pendaftaran baru)
    },
}

USER_FIELDS = {"nama", "instansi", "layanan", "step"}

# Permintaan yang masih menunggu admin (alias tabel: r).
OPEN = "r.taken_by IS NULL AND r.cancelled_at IS NULL"


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # aman dengan WAL, tulis jauh lebih cepat
        self.conn.execute("PRAGMA busy_timeout=5000")   # tunggu, bukan gagal, bila file sedang dikunci
        self.conn.executescript(SCHEMA)
        for table, columns in ADDED_COLUMNS.items():
            existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_users_topic ON users(topic_id)")
        # Tutup tiket lama yang tertinggal karena user mendaftar ulang (data dari versi sebelumnya).
        self.conn.execute(
            f"UPDATE requests AS r SET cancelled_at = created_at WHERE {OPEN} "
            "AND EXISTS (SELECT 1 FROM requests n WHERE n.user_id = r.user_id AND n.id > r.id)"
        )

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def _one(self, sql: str, *params):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, *params) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def _exec(self, sql: str, *params):
        with self.lock:
            self.conn.execute(sql, params)

    # === USER ===

    def touch(self, user_id: int, ts: int):
        self._exec(
            "INSERT INTO users (user_id, last_active) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_active = excluded.last_active",
            user_id, ts,
        )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE user_id = ?", user_id)

    def get_step(self, user_id: int) -> str | None:
        row = self._one("SELECT step FROM users WHERE user_id = ?", user_id)
        return row["step"] if row else None

    def update_user(self, user_id: int, **fields):
        assert fields.keys() <= USER_FIELDS, fields
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE users SET {cols} WHERE user_id = ?", *fields.values(), user_id)

    def complete_request(self, user_id: int, layanan: str, now: int) -> tuple[int, list[sqlite3.Row]]:
        """Simpan layanan, selesaikan pengisian data, dan catat permintaan baru.

        Permintaan lama user yang belum diambil ditutup (digantikan permintaan baru);
        notifikasinya dikembalikan agar bisa ditandai.
        """
        with self.tx() as c:
            c.execute("UPDATE users SET layanan = ?, step = NULL WHERE user_id = ?", (layanan, user_id))
            superseded = c.execute(
                f"SELECT n.chat_id, n.message_id, n.text FROM notices n JOIN requests r ON r.id = n.request_id "
                f"WHERE r.user_id = ? AND {OPEN}",
                (user_id,),
            ).fetchall()
            c.execute(f"UPDATE requests AS r SET cancelled_at = ? WHERE r.user_id = ? AND {OPEN}", (now, user_id))
            cur = c.execute(
                "INSERT INTO requests (user_id, nama, instansi, layanan, created_at) "
                "SELECT user_id, nama, instansi, layanan, ? FROM users WHERE user_id = ?",
                (now, user_id),
            )
            return cur.lastrowid, superseded

    def cancel_open_requests(self, user_ids, now: int) -> int:
        """Tutup permintaan terbuka milik user tertentu (dipakai untuk akun admin)."""
        ids = list(user_ids)
        if not ids:
            return 0
        with self.tx() as c:
            return c.execute(
                f"UPDATE requests AS r SET cancelled_at = ? WHERE {OPEN} AND r.user_id IN ({','.join('?' * len(ids))})",
                (now, *ids),
            ).rowcount

    def list_users(self, layanan: str | None = None, only_active: bool = True) -> list[sqlite3.Row]:
        """User terdaftar. Aktif = sedang ditangani admin atau punya permintaan yang belum diambil."""
        sql = (
            "SELECT * FROM (SELECT u.*, a.username AS admin_username, "
            f"EXISTS (SELECT 1 FROM requests r WHERE r.user_id = u.user_id AND {OPEN}) AS menunggu, "
            "(SELECT MAX(id) FROM requests r WHERE r.user_id = u.user_id) AS tiket "
            "FROM users u LEFT JOIN admins a ON a.admin_id = u.admin_id WHERE u.layanan IS NOT NULL)"
        )
        conditions, params = [], []
        if only_active:
            conditions.append("(admin_id IS NOT NULL OR menunggu)")
        if layanan:
            conditions.append("layanan = ?")
            params.append(layanan)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        return self._all(sql + " ORDER BY menunggu DESC, last_active DESC", *params)

    def set_topic(self, user_id: int, topic_id: int | None):
        self._exec("UPDATE users SET topic_id = ? WHERE user_id = ?", topic_id, user_id)

    def topic_user(self, topic_id: int) -> int | None:
        row = self._one("SELECT user_id FROM users WHERE topic_id = ?", topic_id)
        return row["user_id"] if row else None

    # === PERMINTAAN (TIKET) ===

    def get_request(self, request_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM requests WHERE id = ?", request_id)

    def latest_request(self, user_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT 1", user_id)

    def end_request(self, user_id: int, now: int) -> sqlite3.Row | None:
        """Tandai permintaan terakhir user selesai; mengembalikan permintaan tersebut."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE requests SET ended_at = ? WHERE id = ?", (now, row["id"]))
            return c.execute("SELECT * FROM requests WHERE id = ?", (row["id"],)).fetchone()

    def set_rating(self, request_id: int, user_id: int, rating: int) -> bool:
        """Simpan penilaian sekali saja, dan hanya oleh pemilik permintaan."""
        with self.tx() as c:
            return c.execute(
                "UPDATE requests SET rating = ? WHERE id = ? AND user_id = ? AND rating IS NULL",
                (rating, request_id, user_id),
            ).rowcount == 1

    def due_reminders(self, cutoff: int, oldest: int, max_reminders: int) -> list[sqlite3.Row]:
        """Permintaan yang belum diambil dan pengingat terakhirnya (atau waktu dibuat) sebelum cutoff."""
        return self._all(
            "SELECT r.*, u.topic_id FROM requests r JOIN users u ON u.user_id = r.user_id "
            f"WHERE {OPEN} AND r.reminders < ? AND r.created_at >= ? "
            "AND COALESCE(r.reminded_at, r.created_at) <= ? ORDER BY r.id",
            max_reminders, oldest, cutoff,
        )

    def mark_reminded(self, request_id: int, now: int):
        self._exec("UPDATE requests SET reminders = reminders + 1, reminded_at = ? WHERE id = ?", now, request_id)

    # === ADMIN ===

    def set_admin_name(self, admin_id: int, username: str):
        self._exec(
            "INSERT INTO admins (admin_id, username) VALUES (?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET username = excluded.username",
            admin_id, username,
        )

    def admin_name(self, admin_id: int) -> str:
        row = self._one("SELECT username FROM admins WHERE admin_id = ?", admin_id)
        return row["username"] if row and row["username"] else f"ID:{admin_id}"

    def active_user(self, admin_id: int) -> int | None:
        row = self._one("SELECT active_user FROM admins WHERE admin_id = ?", admin_id)
        return row["active_user"] if row else None

    def assign(self, user_id: int, admin_id: int, now: int, make_active: bool = True) -> dict:
        """Tetapkan admin untuk user dan tandai permintaan terbuka sebagai diambil.

        Mengembalikan admin sebelumnya, user aktif sebelumnya milik admin ini, dan
        notifikasi yang perlu ditandai "diambil".
        """
        with self.tx() as c:
            prev_admin = c.execute("SELECT admin_id FROM users WHERE user_id = ?", (user_id,)).fetchone()["admin_id"]
            c.execute("INSERT OR IGNORE INTO admins (admin_id) VALUES (?)", (admin_id,))
            prev_active = c.execute("SELECT active_user FROM admins WHERE admin_id = ?", (admin_id,)).fetchone()["active_user"]

            c.execute("UPDATE users SET admin_id = ? WHERE user_id = ?", (admin_id, user_id))
            if make_active:
                c.execute("UPDATE admins SET active_user = ? WHERE admin_id = ?", (user_id, admin_id))
            if prev_admin is not None and prev_admin != admin_id:
                c.execute("UPDATE admins SET active_user = NULL WHERE admin_id = ? AND active_user = ?", (prev_admin, user_id))

            notices = c.execute(
                "SELECT n.chat_id, n.message_id, n.text FROM notices n "
                f"JOIN requests r ON r.id = n.request_id WHERE r.user_id = ? AND {OPEN}",
                (user_id,),
            ).fetchall()
            c.execute(
                f"UPDATE requests AS r SET taken_by = ?, taken_at = ? WHERE r.user_id = ? AND {OPEN}",
                (admin_id, now, user_id),
            )
        return {
            "prev_admin": prev_admin,
            "prev_active": prev_active if make_active and prev_active != user_id else None,
            "notices": notices,
        }

    def release(self, user_id: int):
        with self.tx() as c:
            c.execute("UPDATE users SET admin_id = NULL WHERE user_id = ?", (user_id,))
            c.execute("UPDATE admins SET active_user = NULL WHERE active_user = ?", (user_id,))

    # === NOTIFIKASI & RELAY ===

    def add_notice(self, request_id: int, chat_id: int, message_id: int, text: str):
        self._exec("INSERT INTO notices VALUES (?, ?, ?, ?)", request_id, chat_id, message_id, text)

    def add_relay(self, chat_id: int, message_id: int, user_id: int, now: int):
        self._exec("INSERT OR REPLACE INTO relay VALUES (?, ?, ?, ?)", chat_id, message_id, user_id, now)

    def relay_user(self, chat_id: int, message_id: int) -> int | None:
        row = self._one("SELECT user_id FROM relay WHERE chat_id = ? AND message_id = ?", chat_id, message_id)
        return row["user_id"] if row else None

    # === LAPORAN ===

    def stats(self) -> list[sqlite3.Row]:
        return self._all(
            f"SELECT layanan, COUNT(*) AS total, SUM({OPEN}) AS terbuka, "
            "COUNT(DISTINCT user_id) AS user, AVG(rating) AS rating, COUNT(rating) AS dinilai "
            "FROM requests r WHERE r.cancelled_at IS NULL GROUP BY layanan ORDER BY total DESC"
        )

    def export_rows(self, layanan: str | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT r.*, a.username AS admin_username FROM requests r "
            "LEFT JOIN admins a ON a.admin_id = r.taken_by WHERE r.cancelled_at IS NULL"
        )
        if layanan:
            return self._all(sql + " AND r.layanan = ? ORDER BY r.created_at", layanan)
        return self._all(sql + " ORDER BY r.created_at")

    # === PEMELIHARAAN ===

    def cleanup(self, cutoff: int, admin_ids: frozenset[int]) -> dict:
        """Hapus user yang tidak pernah menyelesaikan pengisian data dan data relay lama."""
        with self.tx() as c:
            placeholders = ",".join("?" * len(admin_ids)) or "NULL"
            users = c.execute(
                "DELETE FROM users WHERE layanan IS NULL AND admin_id IS NULL AND last_active < ? "
                f"AND user_id NOT IN ({placeholders})",
                (cutoff, *admin_ids),
            ).rowcount
            relay = c.execute("DELETE FROM relay WHERE created_at < ?", (cutoff,)).rowcount
            notices = c.execute(
                "DELETE FROM notices WHERE request_id IN "
                "(SELECT id FROM requests WHERE taken_by IS NOT NULL OR cancelled_at IS NOT NULL OR created_at < ?)",
                (cutoff,),
            ).rowcount
        return {"users": users, "relay": relay, "notices": notices}

    def backup(self, dest: Path):
        """Salin database ke dest secara konsisten (aman walau bot sedang berjalan)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        target = sqlite3.connect(tmp)
        try:
            with self.lock:
                self.conn.backup(target)
        finally:
            target.close()
        tmp.replace(dest)

    def migrate_json(self, path: Path):
        """Impor state.json dari versi lama (sekali saja), lalu ganti namanya."""
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("Gagal membaca %s, migrasi dilewati", path)
            return
        with self.tx() as c:
            for uid, u in data.get("users", {}).items():
                # User yang mendaftar sebelum ada pilihan layanan tetap dianggap terdaftar.
                layanan = u.get("layanan") or ("-" if u.get("instansi") else None)
                c.execute(
                    "INSERT OR IGNORE INTO users (user_id, nama, instansi, layanan, last_active) VALUES (?, ?, ?, ?, ?)",
                    (int(uid), u.get("nama"), u.get("instansi"), layanan, u.get("last_active", 0)),
                )
            for aid, name in data.get("admin_names", {}).items():
                c.execute("INSERT OR IGNORE INTO admins (admin_id, username) VALUES (?, ?)", (int(aid), name))
        path.rename(path.with_suffix(".json.migrated"))
        log.info("Data lama dari %s sudah dipindahkan ke database", path)
