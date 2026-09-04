"""Timing de la validación post-cierre: offset configurable + espera a que la vela cierre.

El trader despierta a HH:00 + offset (default 20s) y, si OANDA aún no marcó cerrada la vela
recién terminada, reintenta hasta que cierre (sin perder la entrada). Antes era fijo a :01.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from run_trader import _candle_readiness, _seconds_until_next_close_plus


def test_offset_apunta_a_HH00_mas_offset():
    for offset in (0, 20, 45):
        secs = _seconds_until_next_close_plus(offset)
        target = datetime.utcnow() + timedelta(seconds=secs)
        # el objetivo cae en el segundo = offset (mod 60) de algún minuto :00
        assert target.minute == 0
        assert abs(target.second - (offset % 60)) <= 1
        assert 0 < secs <= 3600 + offset


def test_offset_menor_a_60_es_mas_rapido_que_el_viejo_60():
    # 20s debe despertar antes que el viejo :01 (=60s) dentro de la misma hora.
    now = datetime.utcnow()
    if now.minute == 0 and now.second < 25:
        return  # borde raro justo en el cierre; el resto de la hora es representativo
    assert _seconds_until_next_close_plus(20) < _seconds_until_next_close_plus(60)


# --- readiness -------------------------------------------------------------
CLOSE = datetime(2026, 9, 4, 13, 0, 0)          # cierre de la vela de las 12:00
EXPECTED = CLOSE - timedelta(hours=1)            # = 12:00 (open de la vela que cerró)


def test_ready_cuando_la_vela_esperada_ya_cerro():
    assert _candle_readiness(EXPECTED, EXPECTED) == "ready"
    # una más nueva también es 'ready'
    assert _candle_readiness(EXPECTED + timedelta(hours=1), EXPECTED) == "ready"


def test_wait_cuando_todavia_no_cerro():
    # OANDA aún muestra como última cerrada la vela previa (11:00) → esperar
    assert _candle_readiness(EXPECTED - timedelta(hours=1), EXPECTED) == "wait"
    assert _candle_readiness(None, EXPECTED) == "wait"


def test_stale_cuando_el_candle_quedo_muy_viejo():
    # mercado cerrado (finde): la última cerrada es de hace días → no reintentar
    assert _candle_readiness(EXPECTED - timedelta(days=2), EXPECTED) == "stale"
