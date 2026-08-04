"""
Runner de SHADOW del us30_trader — cablea los componentes reales y procesa
tick(s) de validación, logueando el trade que HARÍA sin enviar nada.

  señal:  AlertsSignalSource  (engine de us30_alerts, datos OANDA, solo lectura)
  broker: CapitalClient       (Capital.com demo, allow_execution=False → no ejecuta)
  state:  JsonStateStore      (dedup en disco)

Uso (con el venv que tiene las deps de us30_alerts):
  cd us30_trader
  ../us30_alerts/.venv/bin/python run_shadow.py --once      # un solo tick (prueba)
  ../us30_alerts/.venv/bin/python run_shadow.py             # loop en cada cierre 1H

NADA se ejecuta: allow_execution=False. Este runner es para validar la cadena
completa en vivo y correr semanas comparando contra las alertas.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.capital_client import CapitalClient, CapitalConfig
from execution.config import load_trader_config
from execution.executor import MODE_SHADOW, Executor
from execution.signal_source import AlertsSignalSource
from execution.state_store import JsonStateStore

HERE = Path(__file__).resolve().parent
ALERTS_DIR = HERE.parent / "us30_alerts"


def build_executor() -> tuple[Executor, CapitalClient, AlertsSignalSource]:
    log = logging.getLogger("us30_trader")
    tcfg = load_trader_config(HERE / "config.yaml")
    if tcfg.mode != MODE_SHADOW:
        log.warning("config.mode=%s pero run_shadow FUERZA shadow (allow_execution=False). "
                    "Para ejecutar de verdad usá el runner live.", tcfg.mode)
    # run_shadow SIEMPRE es shadow: broker sin permiso de ejecución + executor en shadow.
    exec_cfg = tcfg.executor_config(mode_override=MODE_SHADOW)
    log.info("config: epic=%s risk_pct=%.2f%% rr=%.1f value_per_point=%.4f mode=%s",
             exec_cfg.epic, exec_cfg.risk_pct, exec_cfg.rr_target,
             exec_cfg.spec.value_per_point, exec_cfg.mode)

    signal = AlertsSignalSource(ALERTS_DIR)
    broker = CapitalClient(CapitalConfig.from_env(HERE / ".env", allow_execution=False))
    broker.login()
    store = JsonStateStore(HERE / "state" / "trader_state.json")
    ex = Executor(signal, broker, store, exec_cfg)
    return ex, broker, signal


def run_once(ex: Executor, broker: CapitalClient, signal: AlertsSignalSource) -> None:
    bal = broker.balance()
    price = broker.current_price("US30")
    log = logging.getLogger("us30_trader")
    log.info("bias=%s | balance=$%s disp=$%s | US30 bid/offer=%s/%s spread=%.1f",
             signal.current_bias(), bal.get("balance"), bal.get("available"),
             price.get("bid"), price.get("offer"), price.get("spread") or 0.0)
    decisions = ex.process_once()
    if not decisions:
        log.info("sin setup validado en el último cierre (normal entre setups).")
    for d in decisions:
        log.info("decision: action=%s dir=%s reason=%s", d.action, d.direction, d.reason or "-")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="procesar un solo tick y salir")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ex, broker, signal = build_executor()
    try:
        if args.once:
            run_once(ex, broker, signal)
            return
        # loop: procesar poco después de cada cierre 1H (:01)
        while True:
            now = datetime.now(timezone.utc)
            nxt = (now.replace(minute=1, second=0, microsecond=0))
            if nxt <= now:
                nxt += timedelta(hours=1)
            time.sleep(max(1.0, (nxt - now).total_seconds()))
            run_once(ex, broker, signal)
    finally:
        signal.close()


if __name__ == "__main__":
    main()
