"""
Tests del parser de config del trader (puros, sin pyyaml ni red).

Corre standalone:  cd us30_trader && python3 tests/test_config.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.config import ConfigError, parse_trader_config  # noqa: E402


def _base() -> dict:
    return {
        "execution": {
            "broker": "capitalcom", "environment": "demo", "epic": "US30",
            "mode": "shadow", "risk_pct": 1.0, "rr_target": 2.0,
            "break_even": {"enabled": True, "trigger_r": 1.0, "buffer_points": 5},
            "max_spread_points": 15, "max_slippage_points": 10,
            "min_units_action": "skip", "max_concurrent_positions": 1,
            "max_daily_loss_pct": 3.0,
            "instrument": {"value_per_point": 1.0, "size_increment": 0.001,
                           "min_deal_size": 0.001, "max_deal_size": 500.0,
                           "margin_factor": 0.05, "price_step": 0.1},
        }
    }


def test_parse_base_ok():
    c = parse_trader_config(_base())
    assert c.mode == "shadow"
    assert c.epic == "US30"
    assert c.risk_pct == 1.0
    assert c.be_trigger_r == 1.0


def test_construye_executor_config_e_instrument_spec():
    c = parse_trader_config(_base())
    ec = c.executor_config()
    assert ec.mode == "shadow"
    assert ec.risk_pct == 1.0
    assert ec.spec.value_per_point == 1.0
    assert ec.spec.min_deal_size == 0.001
    spec = c.instrument_spec()
    assert spec.margin_factor == 0.05


def test_mode_override():
    c = parse_trader_config(_base())
    ec = c.executor_config(mode_override="live")
    assert ec.mode == "live"


def test_falta_execution():
    raised = False
    try:
        parse_trader_config({"foo": 1})
    except ConfigError as e:
        raised = True
        assert "execution" in str(e)
    assert raised


def test_mode_invalido():
    raw = _base()
    raw["execution"]["mode"] = "produccion"
    raised = False
    try:
        parse_trader_config(raw)
    except ConfigError as e:
        raised = True
        assert "mode" in str(e)
    assert raised


def test_risk_pct_negativo():
    raw = _base()
    raw["execution"]["risk_pct"] = -1
    raised = False
    try:
        parse_trader_config(raw)
    except ConfigError:
        raised = True
    assert raised


def test_risk_pct_absurdo():
    raw = _base()
    raw["execution"]["risk_pct"] = 150
    raised = False
    try:
        parse_trader_config(raw)
    except ConfigError as e:
        raised = True
        assert "100" in str(e)
    assert raised


def test_max_concurrent_menor_a_uno():
    raw = _base()
    raw["execution"]["max_concurrent_positions"] = 0
    raised = False
    try:
        parse_trader_config(raw)
    except ConfigError:
        raised = True
    assert raised


def test_min_units_action_invalido():
    raw = _base()
    raw["execution"]["min_units_action"] = "forzar"
    raised = False
    try:
        parse_trader_config(raw)
    except ConfigError:
        raised = True
    assert raised


def test_defaults_cuando_faltan_opcionales():
    # solo lo mínimo: execution con mode
    c = parse_trader_config({"execution": {"mode": "shadow"}})
    assert c.risk_pct == 1.0
    assert c.rr_target == 2.0
    assert c.value_per_point == 1.0
    assert c.max_concurrent_positions == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    sys.exit(1 if failed else 0)
