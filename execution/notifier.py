"""
Notificaciones del trader (Telegram / Pushover). Bot SEPARADO del de alertas.

Reglas:
  - Nunca rompe el trading: cada envío atrapa sus errores y devuelve False.
  - Se arma desde el .env del trader. Si no hay credenciales, queda un dispatcher
    vacío (no-op) y el bot funciona igual, solo sin avisar.
  - Formatters centralizados para los eventos: orden abierta, break even, error, info.

stdlib puro (urllib). Correr con cualquier Python 3.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("us30_trader.notifier")

PREFIX = "🤖 US30 TRADER"


def _post(url: str, data: dict, timeout: float) -> bool:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        log.warning("notificación falló (%s): %s", url.split("/")[2], e)
        return False


class TelegramChannel:
    def __init__(self, bot_token: str, chat_id: str, http_timeout: float = 10.0):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.http_timeout = http_timeout

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        return _post(url, {"chat_id": self.chat_id, "text": text,
                           "disable_web_page_preview": "true"}, self.http_timeout)


class PushoverChannel:
    def __init__(self, user_key: str, app_token: str, http_timeout: float = 10.0):
        self.user_key = user_key
        self.app_token = app_token
        self.http_timeout = http_timeout

    def send(self, text: str) -> bool:
        return _post("https://api.pushover.net/1/messages.json",
                     {"token": self.app_token, "user": self.user_key, "message": text},
                     self.http_timeout)


class Notifier:
    """Dispatcher: manda a todos los canales configurados. 0 canales = no-op."""

    def __init__(self, channels: Optional[List] = None):
        self.channels = channels or []

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    def send(self, text: str) -> bool:
        ok = False
        for ch in self.channels:
            try:
                ok = ch.send(text) or ok
            except Exception as e:  # noqa: BLE001 — jamás romper el trading
                log.warning("canal %s explotó: %s", type(ch).__name__, e)
        return ok

    # -------- formatters de eventos --------
    def notify_open(self, *, direction: str, epic: str, size: float, entry: float,
                    sl: float, tp: float, risk_amount: float, risk_pct: float,
                    balance: float) -> bool:
        return self.send(
            f"✅ {PREFIX} — ORDEN ABIERTA\n"
            f"{epic} {direction}  size {size:.3f}\n"
            f"entry {entry:.1f} · SL {sl:.1f} · TP {tp:.1f}\n"
            f"riesgo ${risk_amount:.2f} ({risk_pct:.2f}% de ${balance:.2f})"
        )

    def notify_be(self, *, direction: str, epic: str, be_stop: float,
                  deal_id: str = "") -> bool:
        return self.send(
            f"🟡 {PREFIX} — BREAK EVEN\n{epic} {direction}\nSL movido a {be_stop:.1f}"
        )

    def notify_outcome(self, *, direction: str, epic: str, kind: str,
                       pnl: float | None = None,
                       balance: float | None = None,
                       others: list | None = None) -> bool:
        """Resultado del cierre. kind ∈ TP | SL | BE | MANUAL | UNKNOWN.

        `balance`: balance de la cuenta tras el cierre (opcional).
        `others`: lista de (epic, direction, upl) de OTRAS operaciones abiertas en la
        cuenta (compartida entre pares). Se listan para saber qué queda en curso."""
        icons = {"TP": "🎯", "SL": "🛑", "BE": "🟡", "MANUAL": "⚪", "UNKNOWN": "⚪"}
        labels = {"TP": "TP alcanzado", "SL": "SL alcanzado",
                  "BE": "cerrado en breakeven", "MANUAL": "cerrado manualmente",
                  "UNKNOWN": "posición cerrada"}
        icon = icons.get(kind, "⚪")
        label = labels.get(kind, "posición cerrada")
        pnltxt = "" if pnl is None else f" ({'+' if pnl >= 0 else '-'}${abs(pnl):.2f})"
        lines = [f"{icon} {PREFIX} — {label}", f"{epic} {direction}{pnltxt}"]
        if balance is not None:
            lines.append(f"Balance: ${balance:.2f}")
        if others is not None:
            if others:
                lines.append("En curso:")
                for oepic, odir, oupl in others:
                    upltxt = "" if oupl is None else f" ({'+' if oupl >= 0 else '-'}${abs(oupl):.2f})"
                    lines.append(f"  • {oepic} {odir}{upltxt}")
            else:
                lines.append("En curso: ninguna otra operación abierta")
        return self.send("\n".join(lines))

    def notify_error(self, text: str) -> bool:
        return self.send(f"🔴 {PREFIX} — ERROR\n{text}")

    def notify_info(self, text: str) -> bool:
        return self.send(f"ℹ️ {PREFIX} — {text}")


def _load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        env[k.strip()] = v
    return env


def build_notifier_from_env(env_path, http_timeout: float = 10.0) -> Notifier:
    """Arma el Notifier desde el .env del trader. Canales activos solo si hay creds."""
    env = _load_env(Path(env_path))
    channels: List = []
    tg_token, tg_chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        channels.append(TelegramChannel(tg_token, tg_chat, http_timeout))
        log.info("notificaciones: Telegram activo")
    po_user, po_token = env.get("PUSHOVER_USER_KEY"), env.get("PUSHOVER_APP_TOKEN")
    if po_user and po_token:
        channels.append(PushoverChannel(po_user, po_token, http_timeout))
        log.info("notificaciones: Pushover activo")
    if not channels:
        log.info("notificaciones: ningún canal configurado (bot corre igual, sin avisar)")
    return Notifier(channels)
