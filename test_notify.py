"""Manda un mensaje de prueba por los canales configurados en el .env del trader.
Uso:  cd us30_trader && python3 test_notify.py"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from execution.notifier import build_notifier_from_env

HERE = Path(__file__).resolve().parent


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    n = build_notifier_from_env(HERE / ".env")
    if not n.enabled:
        raise SystemExit("No hay canal configurado. Completá TELEGRAM_BOT_TOKEN y "
                         "TELEGRAM_CHAT_ID en .env")
    ok = n.notify_info(f"prueba de notificaciones OK — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("enviado:", ok)
    if not ok:
        raise SystemExit("El envío falló. Revisá el token y el chat_id.")


if __name__ == "__main__":
    main()
