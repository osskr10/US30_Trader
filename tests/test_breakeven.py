"""
Tests del BreakEvenMonitor — lógica pura con precio inyectado, sin red.

Corre standalone:  cd us30_trader && python3 tests/test_breakeven.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.breakeven import BreakEvenMonitor  # noqa: E402
from execution.state_store import InMemoryStateStore  # noqa: E402


class FakeBroker:
    def __init__(self, bid=53000.0, offer=53002.0, status="TRADEABLE", raise_on_update=False):
        self._bid = bid
        self._offer = offer
        self._status = status
        self._raise = raise_on_update
        self.updates = []   # (deal_id, stop_level)

    def current_price(self, epic):
        return {"bid": self._bid, "offer": self._offer, "spread": self._offer - self._bid,
                "status": self._status}

    def update_position_stop(self, deal_id, stop_level, profit_level=None):
        if self._raise:
            raise RuntimeError("broker caído")
        self.updates.append((deal_id, stop_level))
        return {"dealId": deal_id, "dealStatus": "ACCEPTED"}


def _open_record(aid="a1", direction="BUY", be_trigger=53150.0, be_stop=53005.0,
                 deal_id="DEAL1", be_done=False, epic="US30"):
    return {
        "alert_id": aid, "action": "open", "direction": direction, "epic": epic,
        "entry": 53000.0, "sl": 52850.0, "tp": 53300.0, "size": 0.066,
        "be_trigger": be_trigger, "be_stop": be_stop,
        "deal_id": deal_id, "be_done": be_done,
    }


def _store_with(*records):
    st = InMemoryStateStore()
    for r in records:
        st.mark(r["alert_id"], r)
    return st


# --------------------------------------------------------------------------- LONG
def test_long_dispara_cuando_bid_alcanza_trigger():
    st = _store_with(_open_record(direction="BUY", be_trigger=53150.0))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=53150.0, offer=53152.0)
    assert applied == ["a1"]
    assert br.updates == [("DEAL1", 53005.0)]
    assert st.get("a1")["be_done"] is True
    assert "be_ts_utc" in st.get("a1")


def test_long_no_dispara_si_bid_por_debajo():
    st = _store_with(_open_record(direction="BUY", be_trigger=53150.0))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=53149.9, offer=53151.9)
    assert applied == []
    assert br.updates == []
    assert st.get("a1")["be_done"] is False


# --------------------------------------------------------------------------- SHORT
def test_short_dispara_cuando_offer_baja_a_trigger():
    st = _store_with(_open_record(aid="s1", direction="SELL",
                                  be_trigger=52850.0, be_stop=52995.0, deal_id="DEALS"))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=52847.0, offer=52850.0)
    assert applied == ["s1"]
    assert br.updates == [("DEALS", 52995.0)]
    assert st.get("s1")["be_done"] is True


def test_short_no_dispara_si_offer_por_encima():
    st = _store_with(_open_record(aid="s1", direction="SELL",
                                  be_trigger=52850.0, be_stop=52995.0, deal_id="DEALS"))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=52850.1, offer=52852.1)
    assert applied == []
    assert br.updates == []


# --------------------------------------------------------------------------- estados
def test_ya_en_be_no_re_dispara():
    st = _store_with(_open_record(be_done=True))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=53200.0, offer=53202.0)
    assert applied == []
    assert br.updates == []


def test_shadow_sin_deal_id_loguea_y_marca_sin_llamar_broker():
    rec = _open_record(deal_id=None)
    rec["action"] = "shadow"
    st = _store_with(rec)
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=53150.0, offer=53152.0)
    assert applied == ["a1"]
    assert br.updates == []                    # NO se llamó al broker
    assert st.get("a1")["be_done"] is True


def test_error_broker_no_marca_be_done_para_reintentar():
    st = _store_with(_open_record())
    br = FakeBroker(raise_on_update=True)
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=53150.0, offer=53152.0)
    assert applied == []
    assert st.get("a1")["be_done"] is False    # queda pendiente para el próximo tick


def test_filtra_por_epic():
    st = _store_with(_open_record(aid="otro", epic="US100"))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.on_price(bid=99999.0, offer=99999.0)
    assert applied == []
    assert br.updates == []


# --------------------------------------------------------------------------- poll
def test_poll_once_sin_pendientes_no_pega_a_la_api():
    st = _store_with(_open_record(be_done=True))
    # broker que explota si le piden precio → prueba que ni se lo pide
    class Boom(FakeBroker):
        def current_price(self, epic):
            raise AssertionError("no debería consultarse el precio sin pendientes")
    mon = BreakEvenMonitor(Boom(), st, "US30")
    assert mon.poll_once() == []


def test_poll_once_aplica_be_via_rest():
    st = _store_with(_open_record(be_trigger=53150.0))
    br = FakeBroker(bid=53150.0, offer=53152.0)
    mon = BreakEvenMonitor(br, st, "US30")
    applied = mon.poll_once()
    assert applied == ["a1"]
    assert br.updates == [("DEAL1", 53005.0)]


def test_poll_once_mercado_no_operable_no_aplica():
    st = _store_with(_open_record(be_trigger=53150.0))
    br = FakeBroker(bid=53150.0, offer=53152.0, status="CLOSED")
    mon = BreakEvenMonitor(br, st, "US30")
    assert mon.poll_once() == []
    assert br.updates == []


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
