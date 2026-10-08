"""Reuso de la descarga de OANDA dentro del mismo tick (readiness → bias → validación)."""
from types import SimpleNamespace

import pandas as pd

import execution.signal_source as ss
from execution.signal_source import AlertsSignalSource


class _Agg:
    def __init__(self):
        self.calls = 0

    def fetch_and_prepare(self, ds, cfg):
        self.calls += 1
        df = pd.DataFrame({"datetime": [pd.Timestamp("2026-10-08 15:00")], "bias": ["SHORT"]})
        return df, None, "SHORT"


def _src():
    s = object.__new__(AlertsSignalSource)          # sin OANDA ni config reales
    s._bar_aggregator = _Agg()
    s.ds = None
    s.cfg = SimpleNamespace(filters=SimpleNamespace(live_bias=True))
    s._snapshot = None
    return s


def test_readiness_siempre_fresco_y_bias_reusa():
    s = _src()
    s.last_closed_open_utc()
    s.last_closed_open_utc()                        # cada intento de readiness descarga
    assert s._bar_aggregator.calls == 2
    assert s.current_bias() == "SHORT"
    assert s._fetch(reuse=True)[2] == "SHORT"       # lo que usa la validación
    assert s._bar_aggregator.calls == 2             # bias + validación: 0 descargas extra


def test_sin_snapshot_o_invalidado_descarga():
    s = _src()
    s.current_bias()
    assert s._bar_aggregator.calls == 1             # no había nada guardado
    s.invalidate_snapshot()
    s.current_bias()
    assert s._bar_aggregator.calls == 2


def test_snapshot_vencido_descarga(monkeypatch):
    s = _src()
    s.last_closed_open_utc()
    t0 = s._snapshot[0]
    monkeypatch.setattr(ss.time, "monotonic", lambda: t0 + ss._SNAPSHOT_TTL_SECONDS + 1)
    s.current_bias()
    assert s._bar_aggregator.calls == 2
