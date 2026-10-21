"""Notifikasi WhatsApp lewat WAHA (WhatsApp HTTP API yang dipasang sendiri).

Dokumentasi WAHA: https://waha.devlike.pro/docs/how-to/send-messages/
Pengiriman berjalan di thread terpisah, sehingga WAHA yang lambat atau mati
tidak pernah menghambat bot Telegram.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("whatsapp")


def to_chat_id(target: str) -> str:
    """'0812-3456-789' / '+62812...' -> '62812...@c.us'; ID grup ('...@g.us') dipakai apa adanya."""
    target = target.strip()
    if "@" in target:
        return target
    digits = re.sub(r"\D", "", target)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return f"{digits}@c.us"


class WahaNotifier:
    def __init__(self, url: str, api_key: str, session: str, targets: list[str],
                 delay: float = 1.5, timeout: float = 10):
        self.url = url.strip().rstrip("/")
        self.session = session
        self.targets = [to_chat_id(t) for t in targets if t.strip()]
        self.delay = delay  # jeda antar-tujuan, agar tidak terlihat seperti spam
        self.timeout = timeout
        self.http = requests.Session()
        if api_key:
            self.http.headers["X-Api-Key"] = api_key
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="whatsapp") if self.enabled else None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.targets)

    def send_text(self, chat_id: str, text: str) -> bool:
        try:
            resp = self.http.post(
                f"{self.url}/api/sendText",
                json={"session": self.session, "chatId": chat_id, "text": text},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            log.warning("Gagal menghubungi WAHA di %s: %s", self.url, type(e).__name__)
            return False
        if resp.ok:
            return True
        log.warning("WAHA menolak pesan ke %s: HTTP %s %s", chat_id, resp.status_code, resp.text[:200])
        return False

    def broadcast(self, text: str) -> list[bool]:
        results = []
        for i, chat_id in enumerate(self.targets):
            if i:
                time.sleep(self.delay)
            results.append(self.send_text(chat_id, text))
        return results

    def notify(self, text: str):
        """Kirim ke semua tujuan di latar belakang (tidak menunggu hasil)."""
        if self.enabled:
            self.pool.submit(self.broadcast, text)

    def session_status(self) -> str | None:
        """Status sesi WAHA, mis. WORKING / SCAN_QR_CODE / STOPPED; None jika WAHA tidak bisa dihubungi."""
        try:
            resp = self.http.get(f"{self.url}/api/sessions/{self.session}", timeout=self.timeout)
            if resp.ok:
                return resp.json().get("status")
            log.warning("Gagal membaca status sesi WAHA: HTTP %s %s", resp.status_code, resp.text[:200])
        except (requests.RequestException, ValueError) as e:
            log.warning("Gagal menghubungi WAHA di %s: %s", self.url, type(e).__name__)
        return None

    def shutdown(self):
        if self.pool:
            self.pool.shutdown(wait=True)
