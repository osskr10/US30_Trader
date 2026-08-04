"""
Sizing y niveles para us30_trader sobre Capital.com.

Lógica PURA (sin API, sin red): a partir del balance, el riesgo %, y el par
entry/SL estructural, calcula:
  - `size` en contratos (redondeado a minSizeIncrement, clamp a min/max deal size)
  - TP a 1:R (rr_target, default 2.0)
  - nivel de disparo del Break Even (1R) y el nuevo SL cuando se dispara (entry + buffer)
  - margen requerido (guarda)

Valores reales de US30 (demo, verificado 2026-08-03):
  minDealSize = minSizeIncrement = 0.001, marginFactor = 5%, cuenta e instrumento en USD.
  `value_per_point` = USD por punto para size=1.0 → asumido 1.0 (USD/USD), A CONFIRMAR
  empíricamente con una orden mínima en demo. Es parámetro, no hardcode.

Convención de dirección:
  BUY  → SL por debajo del entry (sl < entry)
  SELL → SL por encima del entry (sl > entry)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class InstrumentSpec:
    """Specs del instrumento (defaults = US30 demo Capital.com, 2026-08-03)."""
    value_per_point: float = 1.0     # USD por punto para size=1.0 (A CONFIRMAR en demo)
    size_increment: float = 0.001    # minSizeIncrement
    min_deal_size: float = 0.001     # minDealSize
    max_deal_size: float = 500.0     # maxDealSize
    margin_factor: float = 0.05      # 5%
    price_step: float = 0.1          # minStepDistance (redondeo de niveles de precio)


@dataclass(frozen=True)
class TradePlan:
    """Resultado del sizing. Si `ok` es False, `reason` explica por qué se saltea."""
    ok: bool
    reason: str
    direction: str          # "BUY" | "SELL"
    entry: float
    sl: float
    tp: float
    size: float
    risk_amount: float      # riesgo objetivo en USD (balance * risk_pct/100)
    risk_realized: float    # riesgo real con el size redondeado (size * dist * value_per_point)
    sl_distance: float      # en puntos
    be_trigger: float       # precio al que se activa el BE (1R a favor)
    be_stop: float          # nuevo SL cuando se dispara el BE (entry ± buffer)
    margin_required: float


def _floor_to_increment(value: float, increment: float) -> float:
    v = Decimal(str(value))
    inc = Decimal(str(increment))
    n = (v / inc).to_integral_value(rounding=ROUND_DOWN)
    return float(n * inc)


def _round_to_step(value: float, step: float) -> float:
    v = Decimal(str(value))
    st = Decimal(str(step))
    n = (v / st).to_integral_value(rounding=ROUND_HALF_UP)
    return float(n * st)


def compute_trade_plan(
    *,
    balance: float,
    risk_pct: float,               # en porcentaje: 1.0 = 1%
    entry: float,
    sl: float,
    direction: str,                # "BUY" | "SELL"
    spec: InstrumentSpec = InstrumentSpec(),
    rr_target: float = 2.0,
    be_trigger_r: float = 1.0,
    be_buffer_points: float = 5.0,
    available_margin: float | None = None,
) -> TradePlan:
    """Calcula el plan de trade. No envía nada; solo aritmética."""
    direction = direction.upper()

    def skip(reason: str) -> TradePlan:
        return TradePlan(
            ok=False, reason=reason, direction=direction, entry=entry, sl=sl,
            tp=0.0, size=0.0, risk_amount=0.0, risk_realized=0.0,
            sl_distance=0.0, be_trigger=0.0, be_stop=0.0, margin_required=0.0,
        )

    if direction not in ("BUY", "SELL"):
        return skip(f"direccion invalida: {direction!r}")
    if balance <= 0:
        return skip("balance <= 0")
    if risk_pct <= 0:
        return skip("risk_pct <= 0")

    # Coherencia dirección vs SL
    if direction == "BUY" and not sl < entry:
        return skip("BUY requiere sl < entry")
    if direction == "SELL" and not sl > entry:
        return skip("SELL requiere sl > entry")

    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return skip("distancia de SL nula")

    risk_amount = balance * (risk_pct / 100.0)
    per_point = sl_distance * spec.value_per_point
    if per_point <= 0:
        return skip("value_per_point invalido")

    raw_size = risk_amount / per_point
    size = _floor_to_increment(raw_size, spec.size_increment)

    if size < spec.min_deal_size:
        return skip(
            f"size {raw_size:.6f} < minDealSize {spec.min_deal_size} "
            f"(SL demasiado ancho para el riesgo) -> skip"
        )
    if size > spec.max_deal_size:
        size = _floor_to_increment(spec.max_deal_size, spec.size_increment)

    sign = 1 if direction == "BUY" else -1
    tp = _round_to_step(entry + sign * rr_target * sl_distance, spec.price_step)
    be_trigger = _round_to_step(entry + sign * be_trigger_r * sl_distance, spec.price_step)
    be_stop = _round_to_step(entry + sign * be_buffer_points, spec.price_step)
    sl_rounded = _round_to_step(sl, spec.price_step)

    risk_realized = size * sl_distance * spec.value_per_point
    margin_required = size * entry * spec.margin_factor

    if available_margin is not None and margin_required > available_margin:
        return skip(
            f"margen insuficiente: requiere {margin_required:.2f} > "
            f"disponible {available_margin:.2f}"
        )

    return TradePlan(
        ok=True, reason="", direction=direction, entry=entry, sl=sl_rounded,
        tp=tp, size=size, risk_amount=risk_amount, risk_realized=risk_realized,
        sl_distance=sl_distance, be_trigger=be_trigger, be_stop=be_stop,
        margin_required=margin_required,
    )


if __name__ == "__main__":
    # Demo con el ejemplo del PLAN_CAPITALCOM.md §5
    plan = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0,
        direction="BUY", available_margin=1000.0,
    )
    print(plan)
