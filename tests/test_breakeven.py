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
    def __init__(self, bid=53000.0, offer=53002.0, status="TRADEABLE", raise_on_update=False,
                 open_ids=None, activities=None, transactions=None, raise_on_positions=False):
        self._bid = bid
        self._offer = offer
        self._status = status
        self._raise = raise_on_update
        self._open_ids = list(open_ids) if open_ids is not None else []
        self._activities = activities or []
        self._transactions = transactions or []
        self._raise_positions = raise_on_positions
        self.updates = []   # (deal_id, stop_level, profit_level)

    def current_price(self, epic):
        return {"bid": self._bid, "offer": self._offer, "spread": self._offer - self._bid,
                "status": self._status}

    def update_position_stop(self, deal_id, stop_level, profit_level=None):
        if self._raise:
            raise RuntimeError("broker caído")
        self.updates.append((deal_id, stop_level, profit_level))
        return {"dealId": deal_id, "dealStatus": "ACCEPTED"}

    def positions_for_epic(self, epic):
        if self._raise_positions:
            raise RuntimeError("no se pudo leer posiciones")
        return [{"position": {"dealId": d}} for d in self._open_ids]

    def balance(self):
        return {"balance": 1000.0, "available": 900.0}

    def positions(self):
        return [{"position": {"dealId": d, "direction": "BUY", "upl": 1.5},
                 "market": {"epic": "OTHER"}} for d in self._open_ids]

    def activities(self, last_period_seconds=3600):
        return self._activities

    def transactions(self, last_period_seconds=3600):
        return self._transactions


class FakeNotifier:
    def __init__(self):
        self.outcomes = []

    def notify_outcome(self, *, direction, epic, kind, pnl=None, balance=None, others=None):
        self.outcomes.append((direction, epic, kind, pnl))
        self.last_balance = balance
        self.last_others = others
        return True


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
    assert br.updates == [("DEAL1", 53005.0, 53300.0)]
    assert st.get("a1")["be_done"] is True
    assert "be_ts_utc" in st.get("a1")


def test_be_reenvia_tp_para_no_borrarlo():
    """Regresión: el PUT /positions de Capital.com reemplaza los niveles; el BE debe
    reenviar el TP original o la posición queda SIN take-profit (pierde el objetivo 1:2)."""
    st = _store_with(_open_record(direction="BUY", be_trigger=53150.0, deal_id="DEAL1"))
    br = FakeBroker()
    mon = BreakEvenMonitor(br, st, "US30")
    mon.on_price(bid=53150.0, offer=53152.0)
    assert len(br.updates) == 1
    deal_id, stop_level, profit_level = br.updates[0]
    assert profit_level == 53300.0            # el tp del record se reenvía, no se pierde


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
    assert br.updates == [("DEALS", 52995.0, 53300.0)]
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
    assert br.updates == [("DEAL1", 53005.0, 53300.0)]


def test_poll_once_mercado_no_operable_no_aplica():
    st = _store_with(_open_record(be_trigger=53150.0))
    br = FakeBroker(bid=53150.0, offer=53152.0, status="CLOSED")
    mon = BreakEvenMonitor(br, st, "US30")
    assert mon.poll_once() == []
    assert br.updates == []


# --------------------------------------------------------------------------- cierres
def _act(deal_id, source):
    return {"dealId": deal_id, "type": "POSITION", "status": "ACCEPTED", "source": source}


def _tx(deal_id, size):
    return {"dealId": deal_id, "transactionType": "TRADE", "size": size}


def test_cierre_por_SL_notifica_perdida():
    st = _store_with(_open_record(deal_id="D1", be_done=False))
    br = FakeBroker(open_ids=[], activities=[_act("D1", "SL")], transactions=[_tx("D1", "-7.02")])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    closed = mon.check_closures()
    assert closed == ["a1"]
    assert nt.outcomes == [("BUY", "US30", "SL", -7.02)]
    rec = st.get("a1")
    assert rec["action"] == "closed" and rec["outcome"] == "SL" and rec["closed_notified"] is True


def test_cierre_por_TP_notifica_ganancia():
    st = _store_with(_open_record(deal_id="D1"))
    br = FakeBroker(open_ids=[], activities=[_act("D1", "TP")], transactions=[_tx("D1", "+14.00")])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == ["a1"]
    assert nt.outcomes == [("BUY", "US30", "TP", 14.0)]


def test_cierre_en_BE_si_ya_estaba_en_be():
    st = _store_with(_open_record(deal_id="D1", be_done=True))
    br = FakeBroker(open_ids=[], activities=[_act("D1", "SL")], transactions=[_tx("D1", "0.50")])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == ["a1"]
    assert nt.outcomes[0][2] == "BE"          # source SL + be_done → breakeven


def test_cierre_manual():
    st = _store_with(_open_record(deal_id="D1"))
    br = FakeBroker(open_ids=[], activities=[_act("D1", "USER")], transactions=[_tx("D1", "-1.0")])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == ["a1"]
    assert nt.outcomes[0][2] == "MANUAL"


def test_posicion_sigue_abierta_no_cierra():
    st = _store_with(_open_record(deal_id="D1"))
    br = FakeBroker(open_ids=["D1"])           # sigue abierta
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == []
    assert nt.outcomes == []
    assert st.get("a1")["action"] == "open"


def test_no_leer_posiciones_no_concluye_cierre():
    st = _store_with(_open_record(deal_id="D1"))
    br = FakeBroker(raise_on_positions=True)   # no se pudo leer → NO concluir cierre
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == []
    assert st.get("a1")["action"] == "open"    # sigue abierta, no se marca cerrada


def test_cierre_otro_epic_se_ignora():
    st = _store_with(_open_record(aid="x", deal_id="D9", epic="US100"))
    br = FakeBroker(open_ids=[])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == []          # no es de este epic
    assert nt.outcomes == []


def test_cierre_no_re_notifica():
    st = _store_with(_open_record(deal_id="D1"))
    br = FakeBroker(open_ids=[], activities=[_act("D1", "SL")], transactions=[_tx("D1", "-5")])
    nt = FakeNotifier()
    mon = BreakEvenMonitor(br, st, "US30", notifier=nt)
    assert mon.check_closures() == ["a1"]
    assert mon.check_closures() == []          # ya marcada closed_notified → no repite
    assert len(nt.outcomes) == 1


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
