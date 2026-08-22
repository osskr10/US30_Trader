"""
Tests de execution/capital_client.py que NO tocan la red:
parseo de .env, guarda de shadow (allow_execution) y validación de credenciales.

Corre standalone:  cd us30_trader && python3 tests/test_capital_client.py
o con pytest:      cd us30_trader && python3 -m pytest tests/ -q
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution.capital_client as cc  # noqa: E402
from execution.capital_client import (  # noqa: E402
    CapitalClient, CapitalConfig, CapitalError, ExecutionDisabled, _load_env,
)


def _write_tmp(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".env")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def test_load_env_parsea_y_saca_comentarios_inline():
    path = _write_tmp(
        "# comentario\n"
        "CAPITAL_API_KEY=abc123\n"
        "CAPITAL_IDENTIFIER=user@mail.com\n"
        "CAPITAL_API_PASSWORD=secret\n"
        "CAPITAL_ENVIRONMENT=demo   # 'demo' o 'live'\n"
        "\n"
        "VACIA=\n"
    )
    try:
        env = _load_env_path(path)
    finally:
        os.remove(path)
    assert env["CAPITAL_API_KEY"] == "abc123"
    assert env["CAPITAL_ENVIRONMENT"] == "demo"   # comentario inline removido
    assert env["VACIA"] == ""


def _load_env_path(path):
    from pathlib import Path
    return _load_env(Path(path))


def test_from_env_falla_si_faltan_credenciales():
    path = _write_tmp("CAPITAL_API_KEY=replace_me_placeholder\n")
    try:
        raised = False
        try:
            CapitalConfig.from_env(path)
        except CapitalError as e:
            raised = True
            assert "faltan credenciales" in str(e)
        assert raised
    finally:
        os.remove(path)


def test_from_env_rechaza_environment_invalido():
    path = _write_tmp(
        "CAPITAL_API_KEY=abc\nCAPITAL_IDENTIFIER=u@m.com\n"
        "CAPITAL_API_PASSWORD=p\nCAPITAL_ENVIRONMENT=produccion\n"
    )
    try:
        raised = False
        try:
            CapitalConfig.from_env(path)
        except CapitalError as e:
            raised = True
            assert "invalido" in str(e)
        assert raised
    finally:
        os.remove(path)


def _shadow_client():
    cfg = CapitalConfig(api_key="k", identifier="i", password="p",
                        environment="demo", allow_execution=False)
    return CapitalClient(cfg)


def test_guarda_shadow_bloquea_open():
    c = _shadow_client()
    raised = False
    try:
        c.open_market_position(epic="US30", direction="BUY", size=0.01,
                               stop_level=1.0, profit_level=2.0)
    except ExecutionDisabled:
        raised = True
    assert raised, "open_market_position debía bloquearse en shadow"


def test_guarda_shadow_bloquea_update_stop():
    c = _shadow_client()
    raised = False
    try:
        c.update_position_stop("deal123", stop_level=50000.0)
    except ExecutionDisabled:
        raised = True
    assert raised, "update_position_stop debía bloquearse en shadow"


def test_guarda_shadow_bloquea_close():
    c = _shadow_client()
    raised = False
    try:
        c.close_position("deal123")
    except ExecutionDisabled:
        raised = True
    assert raised, "close_position debía bloquearse en shadow"


def test_execution_habilitada_no_bloquea_por_guarda():
    # Con allow_execution=True la guarda NO debe disparar ExecutionDisabled.
    # (No hay red en el test → el error, si llega, será de conexión, no de guarda.)
    cfg = CapitalConfig(api_key="k", identifier="i", password="p",
                        environment="demo", allow_execution=True)
    c = CapitalClient(cfg)
    got_execution_disabled = False
    try:
        c.update_position_stop("deal123", stop_level=50000.0)
    except ExecutionDisabled:
        got_execution_disabled = True
    except Exception:
        pass  # error de red esperado: la guarda ya pasó
    assert not got_execution_disabled


def _client():
    cfg = CapitalConfig(api_key="k", identifier="i", password="p", environment="demo")
    return CapitalClient(cfg)


def test_login_reintenta_ante_too_many_requests():
    c = _client()
    calls = {"n": 0}

    def fake_raw(method, url, headers, body=None, net_retries=2):
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, {}, {"errorCode": "error.too-many.requests"}
        return 200, {"CST": "C", "X-SECURITY-TOKEN": "X"}, {}

    c._raw_http = fake_raw
    orig = cc.time.sleep
    cc.time.sleep = lambda *a, **k: None   # no esperar de verdad en el test
    try:
        c.login()
    finally:
        cc.time.sleep = orig
    assert c._cst == "C" and c._xsec == "X"
    assert calls["n"] == 2                  # falló 1, reintentó y funcionó


def test_login_agota_reintentos_y_falla():
    c = _client()

    def always_429(method, url, headers, body=None, net_retries=2):
        return 429, {}, {"errorCode": "error.too-many.requests"}

    c._raw_http = always_429
    orig = cc.time.sleep
    cc.time.sleep = lambda *a, **k: None
    raised = False
    try:
        c.login()
    except CapitalError as e:
        raised = True
        assert "too-many" in str(e)
    finally:
        cc.time.sleep = orig
    assert raised


def test_login_credenciales_malas_no_reintenta():
    c = _client()
    calls = {"n": 0}

    def bad_creds(method, url, headers, body=None, net_retries=2):
        calls["n"] += 1
        return 400, {}, {"errorCode": "error.invalid.details"}

    c._raw_http = bad_creds
    raised = False
    try:
        c.login()
    except CapitalError:
        raised = True
    assert raised and calls["n"] == 1        # error de credenciales → un solo intento


def test_open_and_confirm_reintenta_confirm_404_luego_ok():
    c = _client()
    c.open_market_position = lambda **k: {"dealReference": "o_x"}
    calls = {"n": 0}

    def fake_confirm(ref):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise CapitalError("nf", status=404, payload={"errorCode": "error.not-found.dealReference"})
        return {"dealStatus": "ACCEPTED", "affectedDeals": [{"dealId": "POS", "status": "OPENED"}]}

    c.confirm = fake_confirm
    orig = cc.time.sleep
    cc.time.sleep = lambda *a, **k: None
    try:
        r = c.open_and_confirm(epic="US100", direction="SELL", size=0.126,
                               stop_level=1, profit_level=2)
    finally:
        cc.time.sleep = orig
    assert r.get("dealStatus") == "ACCEPTED"
    assert calls["n"] == 3          # falló 2 veces, al 3er intento OK


def test_open_and_confirm_recupera_por_posiciones():
    from execution.executor import resolve_position_deal_id
    c = _client()
    c.open_market_position = lambda **k: {"dealReference": "o_x"}

    def always_404(ref):
        raise CapitalError("nf", status=404, payload={})

    c.confirm = always_404
    c.positions_for_epic = lambda epic: [
        {"position": {"direction": "SELL", "size": 0.126, "dealId": "RECO"}}]
    orig = cc.time.sleep
    cc.time.sleep = lambda *a, **k: None
    try:
        r = c.open_and_confirm(epic="US100", direction="SELL", size=0.126,
                               stop_level=1, profit_level=2)
    finally:
        cc.time.sleep = orig
    assert r.get("recovered") is True
    assert resolve_position_deal_id(r) == "RECO"


def test_open_and_confirm_unknown_si_no_recupera():
    c = _client()
    c.open_market_position = lambda **k: {"dealReference": "o_x"}

    def always_404(ref):
        raise CapitalError("nf", status=404, payload={})

    c.confirm = always_404
    c.positions_for_epic = lambda epic: []       # no hay posición que coincida
    orig = cc.time.sleep
    cc.time.sleep = lambda *a, **k: None
    try:
        r = c.open_and_confirm(epic="US100", direction="SELL", size=0.126,
                               stop_level=1, profit_level=2)
    finally:
        cc.time.sleep = orig
    assert r.get("dealStatus") == "UNKNOWN"


def test_recover_position_ignora_size_distinto():
    c = _client()
    c.positions_for_epic = lambda epic: [
        {"position": {"direction": "SELL", "size": 0.999, "dealId": "OTRA"}}]
    assert c._recover_position("US100", "SELL", 0.126) is None   # size no coincide


def test_ping_ok():
    c = _client()
    c._cst = "C"; c._xsec = "X"; c._last_activity = time.monotonic()
    c._raw_http = lambda *a, **k: (200, {}, {"status": "OK"})
    assert c.ping() is True


def test_ping_falla_devuelve_false():
    c = _client()
    c._cst = "C"; c._xsec = "X"; c._last_activity = time.monotonic()
    c._raw_http = lambda *a, **k: (500, {}, {"errorCode": "boom"})
    assert c.ping() is False


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
    print(f"\n{len(tests) - failed}/{len(tests)} tests OK")
    sys.exit(1 if failed else 0)
