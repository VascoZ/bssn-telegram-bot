# Bot Helpdesk Telegram Percepatan BSSN

Bot Telegram yang menghubungkan user dari instansi (pemda) dengan admin BSSN. User mendaftar lewat bot dan memilih layanan. Admin lalu menerima notifikasi dan membalas percakapan langsung dari Telegram, tanpa perlu saling bertukar nomor. Notifikasi permintaan baru juga bisa diteruskan ke WhatsApp.

## Fitur

**Untuk user**
- Sambutan berisi katalog layanan dan jam layanan saat `/start`.
- Pendaftaran singkat: nama → instansi → pilih layanan (PTKKSS, TTIS, MANRISK, PEMDI, PENGADUAN). Setiap permintaan mendapat nomor tiket, misalnya `#0042`.
- Bisa mengirim teks, foto, dokumen, dan pesan suara ke admin.
- FAQ per layanan (`❓ FAQ` atau `/faq`), `/layanan` untuk melihat katalog lagi, `/batal` untuk membatalkan pendaftaran.
- Penilaian ⭐1–5 setelah percakapan selesai.

**Untuk admin**
- Notifikasi permintaan baru dengan tombol **✋ Ambil**. Notifikasi di admin lain otomatis ditandai "Diambil oleh …".
- Membalas user lewat tombol **💬 Balas**, *reply* ke pesan user, atau `/talk`. Setiap balasan diberi inisial admin, misalnya `-ao`.
- `/list` menampilkan user yang menunggu atau sedang ditangani, beserta perintah cepat yang bisa disentuh (`/talk_123…`, `/end_123…`).
- `/stats` (jumlah permintaan dan rata-rata penilaian per layanan) dan `/export` (rekap Excel).
- Pengingat otomatis untuk tiket yang belum diambil, hanya di jam layanan.
- Opsional: notifikasi hanya ke admin layanan tertentu, grup admin bersama, atau **satu topik forum per user** di grup admin.
- Opsional: notifikasi ke **WhatsApp** lewat [WAHA](https://waha.devlike.pro) yang dipasang sendiri.

## Struktur proyek

| File | Isi |
|---|---|
| `bot.py` | Seluruh logika bot: konfigurasi, alur user, perintah admin, relay pesan, pengingat, backup |
| `storage.py` | Penyimpanan SQLite (user, admin, tiket, notifikasi, pemetaan pesan untuk *reply*) |
| `whatsapp.py` | Pengirim notifikasi WhatsApp lewat WAHA |
| `faq.json` | Isi FAQ; bisa diubah tanpa restart bot |
| `docker-compose.yml`, `Dockerfile` | Menjalankan bot + WAHA dengan Docker |
| `.env.example` | Contoh semua pengaturan |
| `tests/` | Simulasi uji offline (tanpa koneksi Telegram/WhatsApp sungguhan) |

Daftar layanan beserta deskripsinya ada di `LAYANAN_KATALOG` di bagian atas `bot.py`.

## Menjalankan

### 1. Siapkan pengaturan
Salin `.env.example` menjadi `.env`, lalu isi minimal:

```
BOT_TOKEN=<token dari @BotFather>
ADMIN_IDS=<ID Telegram admin, pisahkan dengan koma>
```

`.env` berisi rahasia, jadi jangan pernah di-commit (sudah dikecualikan di `.gitignore`).

### 2a. Dengan Python
Butuh Python 3.11+.

```
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

### 2b. Dengan Docker
```
docker compose up -d --build
```
Perintah ini menjalankan bot dan WAHA sekaligus. Data tersimpan di volume Docker `bot-data`, sedangkan sesi WhatsApp di volume `waha-sessions`.

Jalankan bot **di satu tempat saja** (Python *atau* Docker). Dua bot dengan token yang sama akan saling berebut pesan.

## Pengaturan

Semua diatur lewat `.env`. Contoh lengkap dan penjelasannya ada di `.env.example`.

| Variabel | Default | Fungsi |
|---|---|---|
| `BOT_TOKEN` | – (wajib) | Token bot Telegram |
| `ADMIN_IDS` | – | ID Telegram admin umum |
| `LAYANAN_ADMINS` | – | Admin per layanan, mis. `TTIS=111,222; MANRISK=333` |
| `ADMIN_INITIALS` | huruf depan nama | Inisial di akhir balasan admin, mis. `1154578221=ao` |
| `ADMIN_GROUP_ID` | – | ID grup admin; notifikasi dikirim ke grup ini |
| `ADMIN_GROUP_TOPICS` | `false` | Satu topik forum per user di grup admin |
| `JAM_LAYANAN` / `HARI_LAYANAN` / `LIBUR` | `08:00-16:00` / `1-5` / – | Jam dan hari layanan (kosongkan `JAM_LAYANAN` untuk 24 jam) |
| `REMINDER_MINUTES` / `REMINDER_MAX` | `15` / `3` | Pengingat tiket yang belum diambil (`0` = mati) |
| `WAHA_URL`, `WAHA_API_KEY`, `WA_TARGETS` | – | Notifikasi WhatsApp (lihat di bawah) |
| `WA_DETAIL` / `WA_REMINDERS` | `false` / `false` | Sertakan nama & instansi / kirim juga pengingat ke WhatsApp |
| `BACKUP_KEEP` | `7` | Jumlah cadangan harian database (`0` = mati) |
| `CLEANUP_DAYS` | `30` | Hapus data user yang tidak pernah menyelesaikan pendaftaran |
| `TELEGRAM_PROXY` | – | Proxy bila jaringan memblokir Telegram |

## Perintah

| Perintah | Siapa | Fungsi |
|---|---|---|
| `/start` | semua | Menu dan katalog layanan |
| `/layanan`, `/faq`, `/batal` | user | Katalog, FAQ, batalkan pendaftaran |
| `/list [semua] [layanan]` | admin | Daftar user aktif (atau semua) |
| `/talk <id>` atau `/talk_<id>` | admin | Mulai/ambil alih percakapan |
| `/end [id]` atau `/end_<id>` | admin | Akhiri percakapan (user diminta menilai) |
| `/stats` | admin | Statistik per layanan |
| `/export [layanan]` | admin | Unduh rekap Excel |
| `/wa` | admin | Cek status WhatsApp dan kirim pesan tes |
| `/help` | admin | Daftar perintah admin |

## Notifikasi WhatsApp (WAHA)

1. Isi `WAHA_API_KEY`, `WAHA_DASHBOARD_PASSWORD`, dan `WA_TARGETS` (nomor `08…`/`62…` atau ID grup `…@g.us`) di `.env`. Bila bot dijalankan dengan Python, isi juga `WAHA_URL=http://localhost:3000`.
2. Jalankan WAHA: `docker compose up -d waha`
3. Buka `http://localhost:3000/dashboard` (login `admin` + `WAHA_DASHBOARD_PASSWORD`), mulai sesi `default`, lalu scan QR dari WhatsApp nomor pengirim (Perangkat tertaut).
4. Ketik `/wa` di bot untuk mengetes.

Catatan:
- WAHA bukan API resmi WhatsApp. Pakai **nomor khusus** sebagai pengirim, dan nomor itu harus berbeda dari nomor penerima.
- Image WAHA diambil dari `mirror.gcr.io` karena server unduhan Docker Hub (CloudFront) diblokir di sebagian jaringan. Isinya identik dengan `devlikeapro/waha:gows`.
- Bila WAHA mati, bot Telegram tetap berjalan normal; hanya notifikasi WhatsApp yang tidak terkirim.

## Data dan cadangan

- Database SQLite ada di `data/bot.db`. Cadangan harian disimpan di `data/backups/`.
- Data lama dari versi pertama (`data/state.json`) otomatis dipindahkan saat bot pertama kali dijalankan.
- Folder `data/` berisi data pribadi user (nama, instansi), jadi jangan diunggah ke repositori.

## Pengujian

```
python -m pytest tests -v
```

Simulasi menjalankan bot dengan Telegram dan WAHA tiruan (±300 pemeriksaan) dan tidak membaca `.env`, sehingga tidak pernah mengirim pesan sungguhan.
