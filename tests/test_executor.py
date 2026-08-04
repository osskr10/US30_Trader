"""
Tests del Executor en modo shadow (y live con broker fake). Sin red, sin pandas.

Corre standalone:  cd us30_trader && python3 tests/test_executor.py
o con pytest:      cd us30_trader && python3 -m pytest tests/ -q
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.executor import (  # noqa: E402
    Executor, ExecutorConfig, MODE_LIVE, MODE_SHADOW, resolve_position_deal_id,
)
from execution.state_store import InMemoryStateStore  # noqa: E402


# --------------------------------------------------------------------------- fakes
@dataclass
class FakeSetup:
    direction: str
    entry: float
    sl: float
    tp: float
    alert_id: str
    signal_direction: str = "LONG"
    risk_points: float = 150.0
    target_r: float = 2.0
    instrument: str = "US30"
    confirm_candle_open: Optional[datetime] = None


class FakeSignal:
    def __init__(self, setups: List[FakeSetup]):
        self._setups = setups

    def latest_validated_setups(self, now_ny=None):
        return list(self._setups)


class FakeBroker:
    def __init__(self, balance=1000.0, available=1000.0, spread=2.0,
                 status="TRADEABLE", open_positions=0):
        self._balance = balance
        self._available = available
        self._spread = spread
        self._status = status
        self._open_positions = open_positions
        self.opened = []   # registro de órdenes enviadas

    def balance(self):
        return {"balance": self._balance, "available": self._available}

    def current_price(self, epic):
        return {"bid": 53000.0, "offer": 53000.0 + self._spread,
                "spread": self._spread, "status": self._status}

    def positions_for_epic(self, epic):
        return [{"dealId": f"pos{i}"} for i in range(self._open_positions)]

    def open_and_confirm(self, *, epic, direction, size, stop_level, profit_level):
        self.opened.append({"epic": epic, "direction": direction, "size": size,
                            "stop_level": stop_level, "profit_level": profit_level})
        return {"dealId": "DEAL123", "dealStatus": "ACCEPTED"}


def _long_setup(aid="a1"):
    return FakeSetup(direction="BUY", entry=53000.0, sl=52850.0, tp=53300.0, alert_id=aid)


def _mk(signal, broker=None, store=None, mode=MODE_SHADOW, daily_loss=0.0, **cfg_kw):
    broker = broker or FakeBroker()
    store = store or InMemoryStateStore()
    cfg = ExecutorConfig(mode=mode, **cfg_kw)
    ex = Executor(signal, broker, store, cfg, daily_loss_pct_provider=lambda: daily_loss)
    return ex, broker, store


# --------------------------------------------------------------------------- tests
def test_shadow_would_open_no_ejecuta():
    ex, broker, store = _mk(FakeSignal([_long_setup()]))
    decisions = ex.process_once()
    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "shadow"
    assert d.plan is not None and d.plan.ok
    assert abs(d.plan.size - 0.066) < 1e-4
    assert broker.opened == []           # NADA enviado en shadow
    assert store.is_seen("a1")           # marcado para no repetir


def test_shadow_dedup_no_repite():
    store = InMemoryStateStore()
    ex, broker, _ = _mk(FakeSignal([_long_setup()]), store=store)
    first = ex.process_once()
    assert first[0].action == "shadow"
    # segundo tick con el mismo setup → duplicado
    second = ex.process_once()
    assert second[0].action == "skip"
    assert "duplicado" in second[0].reason


def test_guarda_spread():
    broker = FakeBroker(spread=20.0)   # > max_spread_points 15
    ex, _, store = _mk(FakeSignal([_long_setup()]), broker=broker)
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "spread" in d.reason
    assert not store.is_seen("a1")     # NO se marca en skip por guarda → puede reintentar


def test_guarda_mercado_no_operable():
    broker = FakeBroker(status="CLOSED")
    ex, _, _ = _mk(FakeSignal([_long_setup()]), broker=broker)
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "no operable" in d.reason


def test_guarda_max_posiciones():
    broker = FakeBroker(open_positions=1)   # ya hay 1, máx 1
    ex, _, _ = _mk(FakeSignal([_long_setup()]), broker=broker, max_concurrent_positions=1)
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "posición" in d.reason


def test_guarda_margen_insuficiente():
    broker = FakeBroker(available=50.0)   # margen ~175 > 50
    ex, _, _ = _mk(FakeSignal([_long_setup()]), broker=broker)
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "margen" in d.reason.lower()


def test_kill_switch_perdida_diaria():
    ex, _, _ = _mk(FakeSignal([_long_setup()]), daily_loss=5.0, max_daily_loss_pct=3.0)
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "kill switch" in d.reason


def test_sizing_skip_sl_muy_ancho():
    # SL a 20000 pts → size < minDealSize → skip
    wide = FakeSetup(direction="BUY", entry=53000.0, sl=33000.0, tp=93000.0, alert_id="wide")
    ex, _, _ = _mk(FakeSignal([wide]))
    d = ex.process_once()[0]
    assert d.action == "skip"
    assert "sizing" in d.reason


def test_modo_live_ejecuta_via_broker():
    broker = FakeBroker()
    store = InMemoryStateStore()
    ex, broker, store = _mk(FakeSignal([_long_setup()]), broker=broker, store=store, mode=MODE_LIVE)
    d = ex.process_once()[0]
    assert d.action == "open"
    assert len(broker.opened) == 1
    o = broker.opened[0]
    assert o["direction"] == "BUY"
    assert abs(o["size"] - 0.066) < 1e-4
    assert abs(o["stop_level"] - 52850.0) < 1e-4
    assert abs(o["profit_level"] - 53300.0) < 1e-4
    rec = store.get("a1")
    assert rec["deal_id"] == "DEAL123"
    assert rec["be_done"] is False


def test_resolve_position_deal_id_usa_affected_deals():
    # Como devuelve Capital.com: dealId de arriba = workingOrderId; el de la posición
    # está en affectedDeals con status OPENED.
    confirm = {
        "dealId": "WORKING-ORDER-ID",
        "affectedDeals": [{"dealId": "POSITION-ID", "status": "OPENED"}],
    }
    assert resolve_position_deal_id(confirm) == "POSITION-ID"


def test_resolve_position_deal_id_fallback_sin_affected():
    assert resolve_position_deal_id({"dealId": "X"}) == "X"
    assert resolve_position_deal_id({}) is None


def test_live_guarda_deal_id_de_la_posicion():
    class BrokerAffected(FakeBroker):
        def open_and_confirm(self, *, epic, direction, size, stop_level, profit_level):
            self.opened.append({"direction": direction, "size": size})
            return {"dealId": "WORKING", "dealStatus": "ACCEPTED",
                    "affectedDeals": [{"dealId": "POS-REAL", "status": "OPENED"}]}
    store = InMemoryStateStore()
    ex, broker, store = _mk(FakeSignal([_long_setup()]), broker=BrokerAffected(),
                            store=store, mode=MODE_LIVE)
    d = ex.process_once()[0]
    assert d.action == "open"
    assert store.get("a1")["deal_id"] == "POS-REAL"   # el de la posición, no WORKING


def test_short_setup_shadow():
    s = FakeSetup(direction="SELL", entry=53000.0, sl=53150.0, tp=52700.0,
                  alert_id="s1", signal_direction="SHORT")
    ex, broker, _ = _mk(FakeSignal([s]))
    d = ex.process_once()[0]
    assert d.action == "shadow"
    assert d.plan.direction == "SELL"
    assert abs(d.plan.tp - 52700.0) < 1e-4
    assert broker.opened == []


def test_sl_buffer_long_aleja_sl_y_recalcula():
    # SL estructural 52850; buffer 10 → SL efectivo 52840; distancia 160 (no 150).
    ex, broker, store = _mk(FakeSignal([_long_setup()]), sl_buffer_points=10.0)
    d = ex.process_once()[0]
    assert d.action == "shadow"
    assert abs(d.plan.sl - 52840.0) < 1e-4
    assert abs(d.plan.sl_distance - 160.0) < 1e-4
    assert abs(d.plan.tp - (53000.0 + 2 * 160.0)) < 1e-4   # TP 1:2 sobre la distancia real
    # size = 10 / 160 = 0.0625 → floor a 0.001 = 0.062 (menor que sin buffer)
    assert abs(d.plan.size - 0.062) < 1e-4


def test_sl_buffer_short_aleja_sl_hacia_arriba():
    s = FakeSetup(direction="SELL", entry=53000.0, sl=53150.0, tp=52700.0, alert_id="s1")
    ex, _, _ = _mk(FakeSignal([s]), sl_buffer_points=10.0)
    d = ex.process_once()[0]
    assert abs(d.plan.sl - 53160.0) < 1e-4       # SL hacia arriba
    assert abs(d.plan.sl_distance - 160.0) < 1e-4
    assert abs(d.plan.tp - (53000.0 - 2 * 160.0)) < 1e-4


def test_sl_buffer_cero_no_cambia_sl():
    ex, _, _ = _mk(FakeSignal([_long_setup()]), sl_buffer_points=0.0)
    d = ex.process_once()[0]
    assert abs(d.plan.sl - 52850.0) < 1e-4
    assert abs(d.plan.size - 0.066) < 1e-4


def test_interes_compuesto_escala_con_balance():
    # Mismo setup, distinto balance → el size escala proporcional al balance (riesgo %).
    setup = _long_setup()
    ex_low, _, _ = _mk(FakeSignal([setup]), broker=FakeBroker(balance=700.0, available=700.0))
    ex_high, _, _ = _mk(FakeSignal([_long_setup("a2")]),
                        broker=FakeBroker(balance=1200.0, available=1200.0))
    d_low = ex_low.process_once()[0]
    d_high = ex_high.process_once()[0]
    # riesgo = balance * 1% → 7 vs 12; size = riesgo/150 → 0.046 vs 0.08
    assert abs(d_low.plan.risk_amount - 7.0) < 1e-6
    assert abs(d_high.plan.risk_amount - 12.0) < 1e-6
    assert d_high.plan.size > d_low.plan.size
    assert abs(d_low.plan.size - 0.046) < 1e-4     # floor(7/150 /0.001)*0.001
    assert abs(d_high.plan.size - 0.08) < 1e-4     # floor(12/150 /0.001)*0.001


def test_dos_setups_en_un_tick():
    a = _long_setup("a1")
    b = FakeSetup(direction="SELL", entry=53000.0, sl=53150.0, tp=52700.0, alert_id="b1")
    ex, _, store = _mk(FakeSignal([a, b]))
    decisions = ex.process_once()
    assert len(decisions) == 2
    assert all(d.action == "shadow" for d in decisions)
    assert store.is_seen("a1") and store.is_seen("b1")


# --------------------------------------------------------------------------- runner
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
