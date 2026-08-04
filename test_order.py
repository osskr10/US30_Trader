"""
Prueba de ejecución de punta a punta en DEMO (size mínimo 0.001):
  open → confirm → leer posición → MOVER SL (mecanismo del Break Even) → cerrar.

Verifica exactamente las 4 llamadas de escritura que hará el bot, incluyendo el PUT
del BE con el dealId CORRECTO (el de affectedDeals, no el workingOrderId).

Uso:  cd us30_trader && ../us30_alerts/.venv/bin/python test_order.py
(stdlib puro; cualquier Python 3 sirve)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from execution.capital_client import CapitalClient, CapitalConfig
from execution.executor import resolve_position_deal_id

HERE = Path(__file__).resolve().parent
EPIC = "US30"
SIZE = 0.001
log = logging.getLogger("test_order")


def _position(broker, epic, deal_id):
    for p in broker.positions_for_epic(epic):
        inner = p.get("position", p)
        if inner.get("dealId") == deal_id:
            return inner
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = CapitalConfig.from_env(HERE / ".env", allow_execution=True)
    if cfg.environment != "demo":
        raise SystemExit(f"ABORTADO: CAPITAL_ENVIRONMENT={cfg.environment!r} no es 'demo'.")
    broker = CapitalClient(cfg)
    broker.login()

    price = broker.current_price(EPIC)
    bid, offer = float(price["bid"]), float(price["offer"])
    stop_level = round(bid - 100.0, 1)
    profit_level = round(offer + 200.0, 1)
    log.info("1) OPEN %s BUY %.3f  SL=%.1f TP=%.1f (bid/offer=%.1f/%.1f)",
             EPIC, SIZE, stop_level, profit_level, bid, offer)

    confirm = broker.open_and_confirm(epic=EPIC, direction="BUY", size=SIZE,
                                      stop_level=stop_level, profit_level=profit_level,
                                      confirm_wait=1.5)
    deal_id = resolve_position_deal_id(confirm)
    status = confirm.get("dealStatus") or confirm.get("status")
    log.info("2) CONFIRM status=%s position_dealId=%s", status, deal_id)
    if status not in ("ACCEPTED", "OPEN") or not deal_id:
        raise SystemExit(f"orden NO aceptada (status={status}).")

    time.sleep(1.5)
    pos = _position(broker, EPIC, deal_id)
    if pos:
        log.info("   posición: level=%s size=%s SL=%s TP=%s leverage=%s upl=%s",
                 pos.get("level"), pos.get("size"), pos.get("stopLevel"),
                 pos.get("profitLevel"), pos.get("leverage"), pos.get("upl"))

    # 3) Mover el SL (mismo PUT que usa el BE). Lo tightening a bid-50 (válido).
    new_stop = round(bid - 50.0, 1)
    log.info("3) BE-PUT mover SL %.1f → %.1f (dealId=%s)", stop_level, new_stop, deal_id)
    upd = broker.update_position_stop(deal_id, stop_level=new_stop)
    log.info("   update status=%s", upd.get("dealStatus") or upd)
    time.sleep(1.5)
    pos2 = _position(broker, EPIC, deal_id)
    if pos2:
        log.info("   SL ahora = %s (esperado ≈ %.1f)", pos2.get("stopLevel"), new_stop)

    # 4) Cerrar
    log.info("4) CLOSE dealId=%s", deal_id)
    close = broker.close_position(deal_id)
    log.info("   close=%s", json.dumps(close, default=str))
    remaining = len(broker.positions_for_epic(EPIC))
    log.info("=== path completo OK: open→confirm→BE-put→close | posiciones restantes=%d ===",
             remaining)


if __name__ == "__main__":
    main()
