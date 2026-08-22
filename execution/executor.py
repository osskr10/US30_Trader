"""
Executor del us30_trader — une señal + sizing + broker.

En el tick de validación estricta post-cierre:
  1. pide los setups validados a la fuente de señal (signal_source),
  2. por cada setup: dedup → guardas → sizing,
  3. en modo `shadow` LOGUEA la orden que haría (sin enviar);
     en modo `live`/`practice` ejecuta vía el broker (Capital.com).

Diseño por INYECCIÓN DE DEPENDENCIAS: recibe `signal_source`, `broker` y `store`
como objetos con una interfaz mínima. Por eso este módulo es stdlib puro y se testea
con fakes, sin necesitar pandas ni red.

Interfaces esperadas:
  signal_source.latest_validated_setups(now_ny=None) -> list[ValidatedSetup-like]
      cada setup: .direction ('BUY'|'SELL'), .signal_direction, .entry, .sl, .tp,
      .risk_points, .target_r, .instrument, .alert_id, .confirm_candle_open
  broker.balance() -> {'balance', 'available', ...}
  broker.current_price(epic) -> {'bid','offer','spread','status'}
  broker.positions_for_epic(epic) -> list
  broker.open_and_confirm(epic, direction, size, stop_level, profit_level) -> dict   (solo live)
  store.is_seen(alert_id) / store.mark(alert_id, record)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

from .sizing import InstrumentSpec, TradePlan, compute_trade_plan

log = logging.getLogger("us30_trader.executor")

MODE_SHADOW = "shadow"
MODE_LIVE = "live"        # también acepta 'practice' como sinónimo de live-en-demo
MODE_PRACTICE = "practice"


def resolve_position_deal_id(confirm: dict):
    """El dealId de la POSICIÓN (para BE/cierre) NO es el 'dealId' de arriba del confirm
    (ese es el workingOrderId). Está en affectedDeals con status 'OPENED'. Verificado
    contra la API 2026-08-03. Fallback al dealId de arriba si no hay affectedDeals."""
    if not isinstance(confirm, dict):
        return None
    for d in confirm.get("affectedDeals", []) or []:
        if d.get("status") == "OPENED" and d.get("dealId"):
            return d["dealId"]
    return confirm.get("dealId")


@dataclass
class ExecutorConfig:
    epic: str = "US30"
    risk_pct: float = 1.0
    rr_target: float = 2.0
    sl_buffer_points: float = 0.0     # respiro del SL: lo aleja del entry (spread guard)
    be_trigger_r: float = 1.0
    be_buffer_points: float = 5.0
    max_spread_points: float = 15.0
    max_concurrent_positions: int = 1
    max_daily_loss_pct: float = 3.0
    mode: str = MODE_SHADOW
    spec: InstrumentSpec = field(default_factory=InstrumentSpec)


@dataclass
class Decision:
    """Resultado de procesar un setup. `action` = 'open' | 'shadow' | 'skip'."""
    action: str
    reason: str
    alert_id: str
    direction: str
    mode: str
    plan: Optional[TradePlan] = None
    broker_result: Optional[dict] = None

    @property
    def acted(self) -> bool:
        return self.action in ("open", "shadow")


class Executor:
    def __init__(self, signal_source, broker, store, cfg: ExecutorConfig,
                 daily_loss_pct_provider: Optional[Callable[[], float]] = None,
                 notifier=None):
        self.signal = signal_source
        self.broker = broker
        self.store = store
        self.cfg = cfg
        self.notifier = notifier
        # Provider del % de pérdida diaria acumulada para el kill switch. Default 0.
        self._daily_loss = daily_loss_pct_provider or (lambda: 0.0)

    # ------------------------------------------------------------------
    def process_once(self, now_ny: Optional[datetime] = None) -> List[Decision]:
        """Un tick de validación: procesa todos los setups validados."""
        setups = self.signal.latest_validated_setups(now_ny)
        decisions: List[Decision] = []
        for setup in setups:
            decisions.append(self._handle_setup(setup))
        return decisions

    # ------------------------------------------------------------------
    def _handle_setup(self, setup) -> Decision:
        aid = setup.alert_id
        direction = setup.direction

        def skip(reason: str, plan: Optional[TradePlan] = None) -> Decision:
            log.info("SKIP %s %s — %s (id=%s)", direction, self.cfg.epic, reason, aid[:12])
            return Decision(action="skip", reason=reason, alert_id=aid,
                            direction=direction, mode=self.cfg.mode, plan=plan)

        # 1) dedup — no re-actuar sobre el mismo setup ya procesado
        if self.store.is_seen(aid):
            return skip("duplicado (ya procesado)")

        # 2) kill switch de pérdida diaria
        daily_loss = self._daily_loss()
        if daily_loss >= self.cfg.max_daily_loss_pct:
            return skip(f"kill switch: pérdida diaria {daily_loss:.2f}% >= "
                        f"{self.cfg.max_daily_loss_pct:.2f}%")

        # 3) sanity: rr del engine vs config del trader
        if abs(getattr(setup, "target_r", self.cfg.rr_target) - self.cfg.rr_target) > 1e-9:
            log.warning("target_r del setup (%.2f) != rr_target del trader (%.2f) — "
                        "uso el del trader", setup.target_r, self.cfg.rr_target)

        # 4) precio + guardas de mercado
        price = self.broker.current_price(self.cfg.epic)
        if price.get("status") != "TRADEABLE":
            return skip(f"mercado no operable (status={price.get('status')})")
        spread = price.get("spread")
        if spread is not None and spread > self.cfg.max_spread_points:
            return skip(f"spread {spread:.1f} > máx {self.cfg.max_spread_points:.1f}")

        # 5) máximo de posiciones concurrentes
        open_positions = self.broker.positions_for_epic(self.cfg.epic)
        if len(open_positions) >= self.cfg.max_concurrent_positions:
            return skip(f"ya hay {len(open_positions)} posición(es) abiertas "
                        f"(máx {self.cfg.max_concurrent_positions})")

        # 6) respiro del SL: alejarlo del entry para que el spread/mecha no lo toque.
        #    El size se recalcula sobre la distancia REAL → el riesgo sigue siendo risk_pct.
        eff_sl = self._sl_with_buffer(setup.sl, direction)

        # 7) sizing — riesgo = balance ACTUAL * risk_pct (interés compuesto: se re-lee el
        #    balance en cada validación). Incluye guarda de margen contra 'available'.
        bal = self.broker.balance()
        balance = float(bal.get("balance", 0.0))
        available = float(bal.get("available", 0.0))
        plan = compute_trade_plan(
            balance=balance, risk_pct=self.cfg.risk_pct,
            entry=setup.entry, sl=eff_sl, direction=direction,
            spec=self.cfg.spec, rr_target=self.cfg.rr_target,
            be_trigger_r=self.cfg.be_trigger_r, be_buffer_points=self.cfg.be_buffer_points,
            available_margin=available,
        )
        if not plan.ok:
            return skip(f"sizing: {plan.reason}", plan=plan)

        # 8) actuar según modo
        if self.cfg.mode == MODE_SHADOW:
            return self._shadow(setup, plan, balance)
        return self._execute(setup, plan, balance)

    def _sl_with_buffer(self, sl: float, direction: str) -> float:
        """Aleja el SL del entry en sl_buffer_points (LONG: abajo, SHORT: arriba)."""
        buf = self.cfg.sl_buffer_points
        if buf <= 0:
            return sl
        if direction == "BUY":
            return sl - buf
        if direction == "SELL":
            return sl + buf
        return sl

    # ------------------------------------------------------------------
    def _shadow(self, setup, plan: TradePlan, balance: float = 0.0) -> Decision:
        log.info(
            "SHADOW WOULD-OPEN %s %s size=%.3f entry=%.2f SL=%.2f TP=%.2f "
            "| BE: activa @%.2f → SL a %.2f | riesgo=$%.2f (%.2f%% de $%.2f) margen=$%.2f (id=%s)",
            plan.direction, self.cfg.epic, plan.size, plan.entry, plan.sl, plan.tp,
            plan.be_trigger, plan.be_stop, plan.risk_realized, self.cfg.risk_pct, balance,
            plan.margin_required, setup.alert_id[:12],
        )
        self.store.mark(setup.alert_id, self._record(setup, plan, action="shadow"))
        return Decision(action="shadow", reason="", alert_id=setup.alert_id,
                        direction=plan.direction, mode=self.cfg.mode, plan=plan)

    def _execute(self, setup, plan: TradePlan, balance: float = 0.0) -> Decision:
        aid = setup.alert_id
        log.info("LIVE OPEN %s %s size=%.3f entry=%.2f SL=%.2f TP=%.2f "
                 "| riesgo=$%.2f (%.2f%% de $%.2f) (id=%s)",
                 plan.direction, self.cfg.epic, plan.size, plan.entry, plan.sl, plan.tp,
                 plan.risk_realized, self.cfg.risk_pct, balance, aid[:12])

        # Falla → marcamos 'error' (seen) para NO re-intentar y evitar doble orden;
        # avisamos fuerte para revisar a mano. La reconciliación al reiniciar ayuda.
        def fail(reason: str) -> Decision:
            log.error("ejecución fallida %s %s: %s (id=%s)",
                      plan.direction, self.cfg.epic, reason, aid[:12])
            rec = self._record(setup, plan, action="error")
            rec["error"] = reason
            self.store.mark(aid, rec)
            if self.notifier:
                self.notifier.notify_error(
                    f"fallo al abrir {self.cfg.epic} {plan.direction} "
                    f"(size {plan.size:.3f}): {reason}. REVISAR manualmente.")
            return Decision(action="skip", reason=f"error de ejecución: {reason}",
                            alert_id=aid, direction=plan.direction, mode=self.cfg.mode, plan=plan)

        try:
            result = self.broker.open_and_confirm(
                epic=self.cfg.epic, direction=plan.direction, size=plan.size,
                stop_level=plan.sl, profit_level=plan.tp,
            )
        except Exception as e:  # noqa: BLE001
            return fail(str(e))

        status = (result or {}).get("dealStatus") or (result or {}).get("status")
        deal_id = resolve_position_deal_id(result)
        if status not in ("ACCEPTED", "OPEN") or not deal_id:
            return fail(f"orden no aceptada (status={status}, dealId={deal_id})")

        recovered = bool(result.get("recovered"))
        record = self._record(setup, plan, action="open")
        record["broker_result"] = result
        record["deal_id"] = deal_id
        record["be_done"] = False
        record["recovered"] = recovered
        self.store.mark(aid, record)
        if recovered:
            log.warning("posición %s recuperada por fallback (confirm 404) (id=%s)", deal_id, aid[:12])
        if self.notifier:
            self.notifier.notify_open(
                direction=plan.direction, epic=self.cfg.epic, size=plan.size,
                entry=plan.entry, sl=plan.sl, tp=plan.tp,
                risk_amount=plan.risk_realized, risk_pct=self.cfg.risk_pct, balance=balance)
            if recovered:
                self.notifier.notify_info(
                    f"{self.cfg.epic} {plan.direction}: el confirm tardó (404), "
                    f"posición recuperada y en seguimiento normal.")
        return Decision(action="open", reason="", alert_id=aid,
                        direction=plan.direction, mode=self.cfg.mode, plan=plan,
                        broker_result=result)

    def _record(self, setup, plan: TradePlan, action: str) -> dict:
        return {
            "alert_id": setup.alert_id,
            "action": action,
            "direction": plan.direction,
            "signal_direction": getattr(setup, "signal_direction", None),
            "epic": self.cfg.epic,
            "entry": plan.entry,
            "sl": plan.sl,
            "tp": plan.tp,
            "size": plan.size,
            "be_trigger": plan.be_trigger,
            "be_stop": plan.be_stop,
            "risk_realized": plan.risk_realized,
            "margin_required": plan.margin_required,
            "confirm_candle_open": getattr(setup, "confirm_candle_open", None),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
