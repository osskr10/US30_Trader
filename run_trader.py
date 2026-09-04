"""
Runner COMBINADO del us30_trader: validación + Break Even en un solo proceso.

  - Hilo VALIDACIÓN: poco después de cada cierre 1H (:01) corre el executor →
    si hay setup validado, EJECUTA la orden (market + SL + TP) en Capital.com.
  - Hilo BREAK EVEN: polea el precio y mueve el SL a 1R cuando corresponde.

Modo de ejecución (config.yaml → execution.mode):
  shadow            → no envía órdenes (loguea lo que haría)
  practice | live   → ENVÍA órdenes reales al broker

⚠️ El HOST lo fija CAPITAL_ENVIRONMENT en .env:
  demo  → cuenta demo (sin dinero real)
  live  → cuenta real
Guarda dura: si mode ejecuta y el .env NO es 'demo', se aborta salvo que el config
tenga allow_live_env: true (opt-in explícito para dinero real).

Uso (con el venv que tiene las deps de us30_alerts):
  cd us30_trader
  ../us30_alerts/.venv/bin/python run_trader.py            # loop 24/7
  ../us30_alerts/.venv/bin/python run_trader.py --once     # una validación + un poll de BE
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.breakeven import BreakEvenMonitor
from execution.capital_client import CapitalClient, CapitalConfig
from execution.config import load_trader_config
from execution.executor import MODE_SHADOW, Executor
from execution.notifier import build_notifier_from_env
from execution.signal_source import AlertsSignalSource
from execution.state_store import JsonStateStore

HERE = Path(__file__).resolve().parent
ALERTS_DIR = HERE.parent / "us30_alerts"
log = logging.getLogger("us30_trader")


def _extract_deal_id(pos: dict):
    """Saca el dealId de una entrada de GET /positions (defensivo)."""
    if not isinstance(pos, dict):
        return None
    inner = pos.get("position") if isinstance(pos.get("position"), dict) else pos
    return inner.get("dealId")


def reconcile(broker: CapitalClient, store: JsonStateStore, epic: str) -> None:
    """Al arrancar: sincroniza el state con las posiciones reales del broker.
      - registros 'open' cuyo dealId ya NO está abierto → marcar 'closed'
        (tocaron SL/TP mientras el proceso estaba caído).
      - posiciones abiertas en el broker que NO están en el state → avisar (ajenas/manual).
    """
    try:
        open_positions = broker.positions_for_epic(epic)
    except Exception as e:  # noqa: BLE001
        log.warning("reconciliación: no se pudieron leer posiciones (%s) — sigo igual", e)
        return
    broker_deal_ids = {d for d in (_extract_deal_id(p) for p in open_positions) if d}

    our_deal_ids = set()
    for aid, rec in store.all_records().items():
        if rec.get("epic") != epic:
            continue
        did = rec.get("deal_id")
        if rec.get("action") == "open" and did:
            our_deal_ids.add(did)
            if did not in broker_deal_ids:
                closed = dict(rec)
                closed["action"] = "closed"
                closed["closed_detected_utc"] = datetime.now(timezone.utc).isoformat()
                store.mark(aid, closed)
                log.info("reconciliación: posición %s (id=%s) ya no está abierta → cerrada",
                         did, aid[:12])

    foreign = broker_deal_ids - our_deal_ids
    if foreign:
        log.warning("reconciliación: %d posición(es) en %s NO están en el state (¿manual/ajena?): %s",
                    len(foreign), epic, sorted(foreign))
    log.info("reconciliación OK: %d abiertas en broker, %d propias en state",
             len(broker_deal_ids), len(our_deal_ids))


def build(cfg_path: Path):
    tcfg = load_trader_config(cfg_path)
    executes = tcfg.mode != MODE_SHADOW
    allow_execution = executes

    broker_cfg = CapitalConfig.from_env(HERE / ".env", allow_execution=allow_execution)

    # --- Guarda de entorno: no golpear cuenta real por accidente ---
    if executes and broker_cfg.environment != "demo" and not tcfg.allow_live_env:
        raise SystemExit(
            f"ABORTADO: mode='{tcfg.mode}' ejecuta órdenes y CAPITAL_ENVIRONMENT="
            f"'{broker_cfg.environment}' NO es 'demo'. Para operar en real, poné "
            f"allow_live_env: true en config.yaml (opt-in explícito)."
        )

    if executes:
        log.warning("=== MODO %s: SE ENVIARÁN ÓRDENES REALES a la cuenta '%s' de Capital.com ===",
                    tcfg.mode.upper(), broker_cfg.environment)
    else:
        log.info("=== MODO SHADOW: no se envía ninguna orden ===")

    exec_cfg = tcfg.executor_config()
    log.info("config: epic=%s risk_pct=%.2f%% (compuesto s/balance) rr=%.1f "
             "sl_buffer=%.0fpts BE@%.1fR/buf=%.0f value_per_point=%.4f mode=%s",
             exec_cfg.epic, exec_cfg.risk_pct, exec_cfg.rr_target, exec_cfg.sl_buffer_points,
             exec_cfg.be_trigger_r, exec_cfg.be_buffer_points, exec_cfg.spec.value_per_point,
             exec_cfg.mode)

    notifier = build_notifier_from_env(HERE / ".env")
    signal_source = AlertsSignalSource(ALERTS_DIR)
    broker = CapitalClient(broker_cfg)
    broker.login()
    store = JsonStateStore(HERE / "state" / "trader_state.json")
    executor = Executor(signal_source, broker, store, exec_cfg, notifier=notifier)
    be_monitor = BreakEvenMonitor(broker, store, exec_cfg.epic, notifier=notifier)
    return tcfg, signal_source, broker, store, executor, be_monitor, notifier


def do_validation_cycle(executor: Executor, broker: CapitalClient,
                        signal_source: AlertsSignalSource) -> None:
    bal = broker.balance()
    price = broker.current_price(executor.cfg.epic)
    log.info("VALIDACIÓN | bias=%s | balance=$%s disp=$%s | %s bid/offer=%s/%s spread=%.1f",
             signal_source.current_bias(), bal.get("balance"), bal.get("available"),
             executor.cfg.epic, price.get("bid"), price.get("offer"), price.get("spread") or 0.0)
    decisions = executor.process_once()
    if not decisions:
        log.info("sin setup validado en el último cierre.")
    for d in decisions:
        log.info("decision: action=%s dir=%s reason=%s", d.action, d.direction, d.reason or "-")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _interruptible_sleep(seconds: float, stop: dict) -> None:
    slept = 0.0
    while slept < seconds and not stop["flag"]:
        time.sleep(min(1.0, seconds - slept))
        slept += 1.0


def _seconds_until_next_close_plus(offset_seconds: int) -> float:
    """Segundos hasta el próximo cierre 1H + offset (HH:00:00 + offset_seconds)."""
    now = _now_utc()
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=offset_seconds)
    if target <= now:
        target += timedelta(hours=1)
    return max(1.0, (target - now).total_seconds())


def _candle_readiness(last_open, expected_open) -> str:
    """'ready' | 'stale' | 'wait'. Ver run_trader de xauusd1h para el detalle."""
    if last_open is None:
        return "wait"
    if last_open >= expected_open:
        return "ready"
    if expected_open - last_open > timedelta(hours=1):
        return "stale"
    return "wait"


def _wait_for_closed_candle(signal_source: AlertsSignalSource, retry_seconds: int,
                            max_wait_seconds: int, stop: dict) -> bool:
    """Al despertar (~HH:00:offset), espera a que OANDA marque CERRADA la vela recién
    terminada antes de validar. Reintenta cada retry_seconds hasta max_wait_seconds.
    Devuelve True cuando está lista (o el candle quedó viejo = mercado cerrado)."""
    close_time = _now_utc().replace(minute=0, second=0, microsecond=0)
    expected_open = close_time - timedelta(hours=1)
    deadline = close_time + timedelta(seconds=max_wait_seconds)
    while not stop["flag"]:
        try:
            last = signal_source.last_closed_open_utc()
        except Exception as e:  # noqa: BLE001
            log.warning("readiness: fallo leyendo el último candle: %s", e)
            last = None
        last_open = last.replace(tzinfo=None) if last is not None else None
        if _candle_readiness(last_open, expected_open) in ("ready", "stale"):
            return True
        if _now_utc() >= deadline:
            log.warning("readiness: la vela esperada (%s) no cerró tras %ds — valido igual",
                        expected_open.isoformat(), max_wait_seconds)
            return False
        _interruptible_sleep(retry_seconds, stop)
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="una validación + un poll de BE, y salir")
    ap.add_argument("--be-interval", type=float, default=2.0,
                    help="segundos entre polls de BE (default 2)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tcfg, signal_source, broker, store, executor, be_monitor, notifier = build(HERE / "config.yaml")

    reconcile(broker, store, executor.cfg.epic)

    if args.once:
        do_validation_cycle(executor, broker, signal_source)
        applied = be_monitor.poll_once()
        if applied:
            log.info("BE aplicado a: %s", applied)
        closed = be_monitor.check_closures()
        if closed:
            log.info("cierres detectados: %s", closed)
        signal_source.close()
        return

    bal = broker.balance()
    notifier.notify_info(
        f"arrancado mode={tcfg.mode} env={broker.cfg.environment} "
        f"balance=${bal.get('balance')} risk={executor.cfg.risk_pct}% sl_buffer={executor.cfg.sl_buffer_points}pts")

    stop = {"flag": False}

    def _shutdown(signum, _frame):
        log.info("señal %s → apagando", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Hilo de BE (daemon): polea el precio y mueve SL a 1R.
    be_thread = threading.Thread(
        target=be_monitor.run,
        kwargs={"interval_seconds": args.be_interval, "stop_flag": lambda: stop["flag"]},
        name="breakeven", daemon=True,
    )
    be_thread.start()

    # Hilo principal: validación tras cada cierre 1H. Despierta a HH:00 + offset (default
    # 20s) y espera a que la vela esté cerrada antes de validar (reintento).
    offset_s = tcfg.validation_offset_seconds
    retry_s = tcfg.validation_retry_seconds
    max_wait_s = tcfg.validation_max_wait_seconds
    log.info("runner combinado arrancado — validación a HH:00+%ds (reintento cada %ds hasta %ds)",
             offset_s, retry_s, max_wait_s)
    try:
        while not stop["flag"]:
            _interruptible_sleep(_seconds_until_next_close_plus(offset_s), stop)
            if stop["flag"]:
                break
            _wait_for_closed_candle(signal_source, retry_s, max_wait_s, stop)
            if stop["flag"]:
                break
            # Reintenta el ciclo ante errores TRANSITORIOS (p.ej. blip de OANDA
            # "Insufficient authorization" que se recupera solo en segundos). Solo
            # manda la alerta ruidosa a Telegram si fallan TODOS los intentos.
            # Seguro de reintentar: process_once deduplica por alert_id (no doble-abre)
            # y current_bias es solo lectura.
            attempts, backoff = 3, 15
            for attempt in range(1, attempts + 1):
                try:
                    do_validation_cycle(executor, broker, signal_source)
                    break
                except Exception as e:  # noqa: BLE001 — el loop no debe morir
                    if attempt < attempts and not stop["flag"]:
                        log.warning("ciclo de validación falló (intento %d/%d): %s — reintento en %ds",
                                    attempt, attempts, e, backoff)
                        slept2 = 0.0
                        while slept2 < backoff and not stop["flag"]:
                            time.sleep(min(1.0, backoff - slept2))
                            slept2 += 1.0
                    else:
                        log.exception("ciclo de validación falló tras %d intentos: %s", attempt, e)
                        notifier.notify_error(f"ciclo de validación falló tras {attempt} intentos: {e}")
    finally:
        be_thread.join(timeout=5)
        signal_source.close()
        log.info("=== us30_trader detenido ===")


if __name__ == "__main__":
    main()
