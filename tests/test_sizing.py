"""
Tests de execution/sizing.py — lógica pura, sin API.

Corre con pytest:   cd us30_trader && python3 -m pytest tests/ -q
o standalone:       cd us30_trader && python3 tests/test_sizing.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.sizing import InstrumentSpec, compute_trade_plan  # noqa: E402

TOL = 1e-6


def approx(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(a - b) <= tol


# ----------------------------------------------------------------------------
# Caso base LONG (ejemplo del PLAN §5)
# ----------------------------------------------------------------------------
def test_long_basico():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0,
        direction="BUY", available_margin=1000.0,
    )
    assert p.ok, p.reason
    assert p.direction == "BUY"
    assert approx(p.sl_distance, 150.0)
    assert approx(p.risk_amount, 10.0)
    # raw = 10/150 = 0.0666..., floor a 0.001 -> 0.066
    assert approx(p.size, 0.066)
    assert approx(p.tp, 53300.0)          # entry + 2*150
    assert approx(p.be_trigger, 53150.0)  # entry + 1*150
    assert approx(p.be_stop, 53005.0)     # entry + buffer 5
    assert approx(p.margin_required, 0.066 * 53000 * 0.05)  # ~174.9
    # el riesgo real nunca supera el objetivo (por el floor)
    assert p.risk_realized <= p.risk_amount + TOL
    assert approx(p.risk_realized, 9.9)


# ----------------------------------------------------------------------------
# Caso base SHORT (simétrico)
# ----------------------------------------------------------------------------
def test_short_basico():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=53150.0,
        direction="SELL", available_margin=1000.0,
    )
    assert p.ok, p.reason
    assert p.direction == "SELL"
    assert approx(p.sl_distance, 150.0)
    assert approx(p.size, 0.066)
    assert approx(p.tp, 52700.0)          # entry - 2*150
    assert approx(p.be_trigger, 52850.0)  # entry - 1*150
    assert approx(p.be_stop, 52995.0)     # entry - buffer 5


# ----------------------------------------------------------------------------
# Coherencia dirección vs SL
# ----------------------------------------------------------------------------
def test_buy_con_sl_arriba_se_saltea():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=53100.0, direction="BUY",
    )
    assert not p.ok
    assert "BUY requiere sl < entry" in p.reason


def test_sell_con_sl_abajo_se_saltea():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52900.0, direction="SELL",
    )
    assert not p.ok
    assert "SELL requiere sl > entry" in p.reason


def test_direccion_invalida():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52900.0, direction="HOLD",
    )
    assert not p.ok
    assert "direccion invalida" in p.reason


# ----------------------------------------------------------------------------
# Size por debajo del mínimo -> skip (SL demasiado ancho para el riesgo)
# ----------------------------------------------------------------------------
def test_size_debajo_del_minimo_se_saltea():
    # riesgo $10, SL a 20000 pts -> raw = 0.0005 < minDealSize 0.001
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=33000.0, direction="BUY",
    )
    assert not p.ok
    assert "minDealSize" in p.reason


# ----------------------------------------------------------------------------
# Clamp al máximo deal size
# ----------------------------------------------------------------------------
def test_clamp_al_max_deal_size():
    # riesgo enorme y SL muy chico -> raw >> 500 -> clamp a 500
    p = compute_trade_plan(
        balance=100_000.0, risk_pct=100.0, entry=53000.0, sl=52999.0,
        direction="BUY", available_margin=None,
    )
    assert p.ok, p.reason
    assert approx(p.size, 500.0)


# ----------------------------------------------------------------------------
# Guarda de margen
# ----------------------------------------------------------------------------
def test_margen_insuficiente_se_saltea():
    # size 0.066 @ 53000 * 5% ~= 174.9 de margen; disponible 100 -> skip
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0,
        direction="BUY", available_margin=100.0,
    )
    assert not p.ok
    assert "margen insuficiente" in p.reason


def test_margen_suficiente_pasa():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0,
        direction="BUY", available_margin=200.0,
    )
    assert p.ok, p.reason


# ----------------------------------------------------------------------------
# Redondeo del size al incremento (0.001), siempre hacia abajo
# ----------------------------------------------------------------------------
def test_size_redondea_al_incremento():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0, direction="BUY",
    )
    # 0.066 es múltiplo exacto de 0.001
    ratio = round(p.size / 0.001, 6)
    assert approx(ratio, round(ratio))  # entero
    assert p.risk_realized <= p.risk_amount + TOL  # el floor nunca sube el riesgo


# ----------------------------------------------------------------------------
# rr_target y buffer configurables
# ----------------------------------------------------------------------------
def test_rr_y_buffer_configurables():
    p = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52800.0, direction="BUY",
        rr_target=3.0, be_trigger_r=1.5, be_buffer_points=10.0,
    )
    assert p.ok, p.reason
    assert approx(p.sl_distance, 200.0)
    assert approx(p.tp, 53600.0)          # entry + 3*200
    assert approx(p.be_trigger, 53300.0)  # entry + 1.5*200
    assert approx(p.be_stop, 53010.0)     # entry + 10


# ----------------------------------------------------------------------------
# value_per_point distinto cambia el size proporcionalmente
# ----------------------------------------------------------------------------
def test_value_per_point_afecta_size():
    spec2 = InstrumentSpec(value_per_point=2.0)
    p1 = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0, direction="BUY",
    )
    p2 = compute_trade_plan(
        balance=1000.0, risk_pct=1.0, entry=53000.0, sl=52850.0, direction="BUY",
        spec=spec2,
    )
    # el doble de valor por punto -> la mitad de size (aprox, por redondeo)
    assert p2.size < p1.size
    assert approx(p2.size, 0.033)


# ----------------------------------------------------------------------------
# Runner standalone (sin pytest)
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    sys.exit(1 if failed else 0)
