"""
Break Even a 1R — mueve el SL a entry(+buffer) cuando el precio toca 1R a favor.

Lee las posiciones del state store (las que abrió el executor, con `be_trigger` /
`be_stop` / `deal_id` / `be_done`), y en cada tick de precio:
  - LONG (BUY):  dispara si  bid   >= be_trigger   (bid = precio de cierre de un long)
  - SHORT (SELL): dispara si offer <= be_trigger   (offer = precio de cierre de un short)
Al disparar: `PUT /positions/{dealId}` moviendo el SL a `be_stop`, y marca `be_done`.

Elección conservadora del lado del precio (bid para long, offer para short): así el BE
no se activa antes de que el trade esté realmente 1R a favor en el precio de cierre.

v1 = POLLING REST (broker.current_price). Sin dependencia nueva, respeta el rate limit
(10 req/s). El WebSocket de Capital.com puede reemplazar el poll más adelante para
reaccionar más rápido sin polear — la lógica (`on_price`) no cambia.

Diseño testeable: `on_price(bid, offer)` es lógica pura; el poll y el loop solo la
alimentan. En shadow (posiciones sin `deal_id`) se LOGUEA el movimiento sin llamar al
broker, para ver el comportamiento del BE sin ejecutar.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

log = logging.getLogger("us30_trader.breakeven")


class BreakEvenMonitor:
    def __init__(self, broker, store, epic: str, notifier=None):
        self.broker = broker
        self.store = store
        self.epic = epic
        self.notifier = notifier

    # ------------------------------------------------------------------
    def pending(self) -> List[dict]:
        """Posiciones del epic que todavía no llegaron a BE."""
        out = []
        for rec in self.store.all_records().values():
            if rec.get("epic") != self.epic:
                continue
            if rec.get("be_done"):
                continue
            if rec.get("be_trigger") is None or rec.get("be_stop") is None:
                continue
            if rec.get("action") not in ("open", "shadow"):
                continue
            out.append(rec)
        return out

    # ------------------------------------------------------------------
    def on_price(self, bid: float, offer: float) -> List[str]:
        """Procesa un tick de precio. Devuelve los alert_id a los que se les aplicó BE."""
        applied: List[str] = []
        for rec in self.pending():
            direction = rec.get("direction")
            trigger = rec.get("be_trigger")
            if direction == "BUY":
                reached = bid >= trigger
            elif direction == "SELL":
                reached = offer <= trigger
            else:
                log.warning("registro con direction inválida: %r (id=%s)",
                            direction, str(rec.get("alert_id"))[:12])
                continue
            if reached and self._apply_be(rec, bid, offer):
                applied.append(rec.get("alert_id"))
        return applied

    def _apply_be(self, rec: dict, bid: float, offer: float) -> bool:
        aid = rec.get("alert_id")
        be_stop = rec.get("be_stop")
        deal_id = rec.get("deal_id")
        try:
            if deal_id:
                # Reenviar el TP original: el PUT /positions de Capital.com reemplaza los
                # niveles, así que si mandamos solo el stop, BORRA el profitLevel (TP) y el
                # trade pierde su objetivo 1:2. rec["tp"] lo preserva (None en shadow → no-op).
                self.broker.update_position_stop(deal_id, stop_level=be_stop,
                                                 profit_level=rec.get("tp"))
                log.info("BE aplicado %s %s → SL a %.2f (deal=%s id=%s)",
                         rec.get("direction"), self.epic, be_stop, deal_id, str(aid)[:12])
                if self.notifier:
                    self.notifier.notify_be(direction=rec.get("direction"), epic=self.epic,
                                            be_stop=be_stop, deal_id=deal_id)
            else:
                # shadow: no hay posición real → solo se loguea.
                log.info("SHADOW WOULD-MOVE-BE %s %s → SL a %.2f (id=%s)",
                         rec.get("direction"), self.epic, be_stop, str(aid)[:12])
        except Exception as e:  # noqa: BLE001 — no marcar be_done para reintentar
            log.error("fallo moviendo BE (id=%s deal=%s): %s — se reintenta",
                      str(aid)[:12], deal_id, e)
            if self.notifier:
                self.notifier.notify_error(
                    f"fallo moviendo BE de {self.epic} (deal={deal_id}): {e}. REVISAR.")
            return False

        updated = dict(rec)
        updated["be_done"] = True
        updated["be_ts_utc"] = datetime.now(timezone.utc).isoformat()
        self.store.mark(aid, updated)
        return True

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _our_open_records(self) -> List[dict]:
        """Registros de posiciones REALES nuestras aún no cerradas (para detectar cierre)."""
        out = []
        for rec in self.store.all_records().values():
            if rec.get("epic") != self.epic:
                continue
            if rec.get("action") != "open" or rec.get("closed_notified"):
                continue
            out.append(rec)
        return out

    def _open_deal_ids(self):
        """dealIds abiertos AHORA en el broker (None si no se pudo leer → no concluir cierres)."""
        try:
            ids = set()
            for p in self.broker.positions_for_epic(self.epic):
                inner = p.get("position", p) if isinstance(p, dict) else {}
                did = inner.get("dealId")
                if did:
                    ids.add(did)
            return ids
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudieron leer posiciones para detectar cierres: %s", e)
            return None

    def _resolve_outcome(self, deal_id, be_done: bool):
        """Devuelve (kind, pnl). kind: TP|SL|BE|MANUAL|UNKNOWN. Nunca lanza."""
        source = None
        try:
            for a in self.broker.activities(3600):
                if (a.get("dealId") == deal_id and a.get("type") == "POSITION"
                        and a.get("source") in ("SL", "TP", "USER")):
                    source = a.get("source")
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudo leer activity para %s: %s", deal_id, e)
        pnl = None
        try:
            for t in self.broker.transactions(3600):
                if t.get("dealId") == deal_id:
                    raw = str(t.get("size", "")).replace("+", "").strip()
                    pnl = float(raw) if raw not in ("", "None") else None
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudo leer transactions para %s: %s", deal_id, e)

        if source == "TP":
            kind = "TP"
        elif source == "SL":
            kind = "BE" if be_done else "SL"   # si ya estaba en BE, el stop es breakeven
        elif source == "USER":
            kind = "MANUAL"
        else:
            kind = "UNKNOWN"
        return kind, pnl

    def _handle_closure(self, rec: dict) -> None:
        aid = rec.get("alert_id")
        kind, pnl = self._resolve_outcome(rec.get("deal_id"), bool(rec.get("be_done")))
        log.info("CIERRE %s %s → %s pnl=%s (id=%s)",
                 rec.get("direction"), self.epic, kind, pnl, str(aid)[:12])
        if self.notifier:
            self.notifier.notify_outcome(direction=rec.get("direction"), epic=self.epic,
                                         kind=kind, pnl=pnl)
        updated = dict(rec)
        updated["action"] = "closed"
        updated["outcome"] = kind
        updated["pnl"] = pnl
        updated["closed_notified"] = True
        updated["closed_ts_utc"] = datetime.now(timezone.utc).isoformat()
        self.store.mark(aid, updated)

    def check_closures(self) -> List[str]:
        """Detecta posiciones nuestras que se cerraron (SL/TP/manual), notifica el
        resultado y las marca cerradas. Devuelve los alert_id cerrados este ciclo."""
        open_recs = self._our_open_records()
        if not open_recs:
            return []
        open_ids = self._open_deal_ids()
        if open_ids is None:            # no pudimos leer → no concluir nada este ciclo
            return []
        closed = []
        for r in open_recs:
            did = r.get("deal_id")
            if did and did not in open_ids:
                self._handle_closure(r)
                closed.append(r.get("alert_id"))
        return closed

    def poll_once(self) -> List[str]:
        """Un ciclo de poll: lee el precio por REST y aplica BE si corresponde.
        Devuelve [] si no hay pendientes (evita pegarle a la API de gusto)."""
        if not self.pending():
            return []
        price = self.broker.current_price(self.epic)
        if price.get("status") != "TRADEABLE":
            return []
        bid, offer = price.get("bid"), price.get("offer")
        if bid is None or offer is None:
            return []
        return self.on_price(float(bid), float(offer))

    def run(self, interval_seconds: float = 2.0, stop_flag=None,
            keepalive_seconds: float = 480.0) -> None:
        """Loop de BE. `stop_flag` opcional: callable que devuelve True para cortar.
        Cada `keepalive_seconds` (8 min < los 10 de expiración) pinguea para mantener
        UNA sola sesión viva todo el día, en vez de re-loguear en cada validación
        horaria (lo que dispara el rate-limit de /session de Capital.com)."""
        log.info("BE monitor arrancado (epic=%s, poll=%.1fs, keepalive=%.0fs)",
                 self.epic, interval_seconds, keepalive_seconds)
        last_ka = time.monotonic()
        while True:
            if stop_flag is not None and stop_flag():
                break
            try:
                self.poll_once()          # BE a 1R
                self.check_closures()      # detecta SL/TP/manual y notifica el resultado
                if hasattr(self.broker, "ping") and (time.monotonic() - last_ka) >= keepalive_seconds:
                    self.broker.ping()
                    last_ka = time.monotonic()
            except Exception as e:  # noqa: BLE001 — el loop nunca debe morir
                log.exception("poll_once/check_closures/keepalive falló: %s", e)
            time.sleep(interval_seconds)
