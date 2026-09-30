"""Regresión 2026-09-29: sesiones largas de Capital.com devolvían balance 0 de forma
intermitente y el executor salteaba entradas válidas ("sizing: balance <= 0").
CapitalClient.balance() ahora re-loguea y relee UNA vez si la lectura viene vacía o <= 0."""
import execution.capital_client as cc
from execution.capital_client import CapitalClient, CapitalConfig

cc._BALANCE_RETRY_SLEEP = 0.0   # tests sin esperas


class _Client(CapitalClient):
    def __init__(self, reads):
        super().__init__(CapitalConfig(api_key="k", identifier="i", password="p", environment="demo"))
        self._reads = list(reads)
        self.logins = 0

    def preferred_account(self):
        return {"balance": self._reads.pop(0)}

    def login(self):
        self.logins += 1


def test_balance_ok_no_relogin():
    c = _Client([{"balance": 1123.83, "available": 1100.0}])
    assert c.balance()["balance"] == 1123.83
    assert c.logins == 0


def test_balance_cero_relogin_y_relee():
    c = _Client([{"balance": 0, "available": 0}, {"balance": 1123.83, "available": 1100.0}])
    assert c.balance()["balance"] == 1123.83
    assert c.logins == 1


def test_balance_vacio_relogin_y_relee():
    c = _Client([{}, {"balance": 500.0}])
    assert c.balance()["balance"] == 500.0
    assert c.logins == 1


def test_balance_sigue_en_cero_tras_3_reintentos():
    c = _Client([{"balance": 0}] * 4)
    assert float(c.balance()["balance"]) == 0
    assert c.logins == 3


def test_balance_se_recupera_al_segundo_reintento():
    c = _Client([{"balance": 0}, {"balance": 0}, {"balance": 1123.83}])
    assert c.balance()["balance"] == 1123.83
    assert c.logins == 2
