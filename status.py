"""Estado del trader para el 'Estado Trader Bot.command': cuenta, posiciones y
última operación. Imprime líneas KEY=VALUE parseables. Solo lectura."""

from __future__ import annotations

import json
from pathlib import Path

from execution.capital_client import CapitalClient, CapitalConfig

HERE = Path(__file__).resolve().parent


def _epic() -> str:
    """Lee el epic del config del trader (no hardcodeado → sirve para otros pares)."""
    try:
        from execution.config import load_trader_config
        return load_trader_config(HERE / "config.yaml").epic
    except Exception:  # noqa: BLE001
        return "US30"


def main() -> None:
    epic = _epic()
    print(f"EPIC={epic}")
    try:
        broker = CapitalClient(CapitalConfig.from_env(HERE / ".env", allow_execution=False))
        broker.login()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR={e}")
        return

    try:
        acc = broker.preferred_account()
        bal = acc.get("balance", {}) or {}
        balance = float(bal.get("balance", 0) or 0)
        available = float(bal.get("available", 0) or 0)
        pnl = float(bal.get("profitLoss", 0) or 0)
        print(f"BALANCE={balance:.2f}")
        print(f"AVAILABLE={available:.2f}")
        print(f"PNL={pnl:.2f}")
        print(f"EQUITY={balance + pnl:.2f}")
        print(f"CURRENCY={acc.get('currency', '?')}")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR=account:{e}")

    try:
        positions = broker.positions_for_epic(epic)
        print(f"OPENCOUNT={len(positions)}")
        for p in positions:
            pos = p.get("position", p)
            print("OPENPOS={}|{}|{}|{}|{}|{}".format(
                pos.get("direction"), pos.get("size"), pos.get("level"),
                pos.get("stopLevel"), pos.get("profitLevel"), pos.get("upl")))
    except Exception as e:  # noqa: BLE001
        print(f"ERROR=positions:{e}")

    try:
        sp = HERE / "state" / "trader_state.json"
        recs = []
        if sp.exists():
            data = json.loads(sp.read_text(encoding="utf-8") or "{}")
            recs = [r for r in data.values() if r.get("ts_utc")]
        if recs:
            r = max(recs, key=lambda x: x.get("ts_utc", ""))
            print("LASTOP={}|{}|{}|{}|{}|{}|{}".format(
                r.get("action"), r.get("direction"), r.get("entry"),
                r.get("sl"), r.get("tp"), r.get("size"), r.get("ts_utc")))
        else:
            print("LASTOP=none")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR=state:{e}")


if __name__ == "__main__":
    main()
