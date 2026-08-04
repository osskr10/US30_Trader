"""Cierra CUALQUIER posición abierta de US30 en la demo (limpieza del test) y
vuelca los datos crudos de cada posición para leer size/level/upl/contractSize."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from execution.capital_client import CapitalClient, CapitalConfig

HERE = Path(__file__).resolve().parent
EPIC = "US30"
log = logging.getLogger("cleanup")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    broker = CapitalClient(CapitalConfig.from_env(HERE / ".env", allow_execution=True))
    broker.login()

    positions = broker.positions_for_epic(EPIC)
    log.info("posiciones abiertas de %s: %d", EPIC, len(positions))
    for p in positions:
        print(json.dumps(p, indent=2, default=str))

    for p in positions:
        inner = p.get("position", p)
        deal_id = inner.get("dealId")
        if not deal_id:
            continue
        try:
            res = broker.close_position(deal_id)
            log.info("cerrada %s → %s", deal_id, res.get("dealStatus") or res)
        except Exception as e:  # noqa: BLE001
            log.error("no se pudo cerrar %s: %s", deal_id, e)

    remaining = broker.positions_for_epic(EPIC)
    log.info("posiciones restantes de %s tras limpieza: %d", EPIC, len(remaining))


if __name__ == "__main__":
    main()
