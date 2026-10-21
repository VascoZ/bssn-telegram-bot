"""Menjalankan semua simulasi bot (tanpa koneksi Telegram/WhatsApp sungguhan).

Setiap simulasi butuh proses Python sendiri karena konfigurasi bot dibaca saat import,
jadi pytest menjalankannya sebagai subprocess. Jalankan: python -m pytest tests -v
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SUITES = [
    ("sim_core.py", "dm"),
    ("sim_core.py", "group"),
    ("sim_features.py", "dm"),
    ("sim_features.py", "topics"),
    ("sim_whatsapp.py", None),
]


@pytest.mark.parametrize("script,mode", SUITES, ids=[f"{s[:-3]}-{m or 'default'}" for s, m in SUITES])
def test_suite(script, mode, tmp_path):
    args = [sys.executable, str(HERE / script), *([mode] if mode else []), str(tmp_path)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "BOT_NO_DOTENV": "1"}
    result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    failures = [line for line in result.stdout.splitlines() if line.startswith("FAIL")]
    passed = sum(line.startswith("PASS") for line in result.stdout.splitlines())
    assert result.returncode == 0 and not failures and passed > 0, (
        "\n".join(failures) or result.stdout[-3000:]) + "\n" + result.stderr[-3000:]
