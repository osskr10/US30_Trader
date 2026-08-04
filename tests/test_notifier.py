"""
Tests del notifier — dispatcher + formatters + build_from_env, sin red.

Corre standalone:  cd us30_trader && python3 tests/test_notifier.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.notifier import Notifier, build_notifier_from_env  # noqa: E402


class FakeChannel:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return self.ok


class BoomChannel:
    def send(self, text):
        raise RuntimeError("canal roto")


def test_dispatcher_vacio_es_noop():
    n = Notifier([])
    assert n.enabled is False
    assert n.send("hola") is False


def test_envia_a_todos_los_canales():
    a, b = FakeChannel(), FakeChannel()
    n = Notifier([a, b])
    assert n.enabled is True
    assert n.send("hola") is True
    assert a.messages == ["hola"] and b.messages == ["hola"]


def test_ok_si_al_menos_uno_anda():
    ok, bad = FakeChannel(ok=True), FakeChannel(ok=False)
    n = Notifier([bad, ok])
    assert n.send("x") is True


def test_canal_que_explota_no_rompe():
    good = FakeChannel()
    n = Notifier([BoomChannel(), good])
    assert n.send("x") is True          # el bueno igual recibió
    assert good.messages == ["x"]


def test_formatters_incluyen_datos_clave():
    ch = FakeChannel()
    n = Notifier([ch])
    n.notify_open(direction="BUY", epic="US30", size=0.062, entry=53000.0,
                  sl=52840.0, tp=53320.0, risk_amount=10.0, risk_pct=1.0, balance=1000.0)
    n.notify_be(direction="BUY", epic="US30", be_stop=53005.0, deal_id="D1")
    n.notify_error("algo falló")
    n.notify_info("arrancado")
    joined = "\n".join(ch.messages)
    assert "ORDEN ABIERTA" in joined and "US30 BUY" in joined and "52840" in joined
    assert "1.00% de $1000.00" in joined
    assert "BREAK EVEN" in joined and "53005" in joined
    assert "ERROR" in joined and "algo falló" in joined


def _write(content):
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_build_sin_credenciales_es_noop():
    path = _write("CAPITAL_API_KEY=x\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")
    try:
        n = build_notifier_from_env(path)
    finally:
        os.remove(path)
    assert n.enabled is False


def test_build_con_telegram_activa_canal():
    path = _write("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n")
    try:
        n = build_notifier_from_env(path)
    finally:
        os.remove(path)
    assert n.enabled is True
    assert len(n.channels) == 1


def test_build_telegram_y_pushover():
    path = _write("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=999\n"
                  "PUSHOVER_USER_KEY=u\nPUSHOVER_APP_TOKEN=t\n")
    try:
        n = build_notifier_from_env(path)
    finally:
        os.remove(path)
    assert len(n.channels) == 2


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
