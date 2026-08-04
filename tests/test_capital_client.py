"""
Tests de execution/capital_client.py que NO tocan la red:
parseo de .env, guarda de shadow (allow_execution) y validación de credenciales.

Corre standalone:  cd us30_trader && python3 tests/test_capital_client.py
o con pytest:      cd us30_trader && python3 -m pytest tests/ -q
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
