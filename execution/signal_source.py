"""
Fuente de señal del trader = el MISMO motor de us30_alerts, reusado sin modificarlo.

Este adaptador importa el pipeline de `us30_alerts/src` (datasource OANDA +
bar_aggregator + Evaluator) y expone SOLO lo que el executor necesita: la lista de
setups que pasan la **validación estricta post-cierre** (`validate_last_closed_candle`),
ya mapeados a la forma que consume el sizing (BUY/SELL, entry, sl, tp).

Por qué reusar en vez de reimplementar:
  - El `AlertPayload` de us30_alerts ya fue diseñado forward-compatible para esto
    (ver su docstring: "everything an order executor (Phase 2) would need").
  - Mantiene la PARIDAD con el backtester y con las alertas (mismo evaluator, mismas
    reglas). No se toca el bot de producción.

Feed de datos: OANDA (lectura, permitida aun en la cuenta forex-only) — igual que las
alertas, para conservar paridad. La EJECUCIÓN va por Capital.com (capital_client). La
divergencia de feeds está anotada en HANDOFF_TRADER.md §3; en shadow es aceptable.

IMPORTANTE: correr con un intérprete que tenga las deps de us30_alerts (pandas,
pydantic, python-dotenv, requests). En shadow: `us30_alerts/.venv/bin/python`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ValidatedSetup:
    """Setup estricto listo para sizing. Direction ya mapeada a BUY/SELL."""
    direction: str            # "BUY" | "SELL"  (para Capital.com)
    signal_direction: str     # "LONG" | "SHORT" (original del engine)
    entry: float              # entry_estimated del payload
    sl: float                 # sl_estimated (SL estructural swing)
    tp: float                 # tp_estimated (1:target_r)
    risk_points: float
    target_r: float
    instrument: str
    alert_id: str             # dedup id determinístico del engine
    confirm_candle_open: datetime


_DIR_MAP = {"LONG": "BUY", "SHORT": "SELL"}


class AlertsSignalSource:
    def __init__(self, alerts_dir: str | Path,
                 config_name: str = "config.yaml",
                 env_name: str = ".env"):
        self.alerts_dir = Path(alerts_dir).resolve()
        if not (self.alerts_dir / "src").is_dir():
            raise FileNotFoundError(f"no se encontró src/ en {self.alerts_dir}")

        # Hacer importable `src` de us30_alerts (no depende del cwd).
        if str(self.alerts_dir) not in sys.path:
            sys.path.insert(0, str(self.alerts_dir))

        # Rutas ABSOLUTAS al config/.env del engine → sin os.chdir (thread-safe: el
        # runner combinado corre validación y BE en hilos distintos).
        cfg_path = self.alerts_dir / config_name
        env_path = self.alerts_dir / env_name

        from src.config import load_config
        from src.datasource.oanda import OandaDataSource
        from src.strategy.evaluator import Evaluator
        from src import bar_aggregator

        self._bar_aggregator = bar_aggregator
        self.cfg = load_config(config_path=str(cfg_path), env_path=str(env_path))
        self.ds = OandaDataSource(
            api_token=self.cfg.secrets.oanda_api_token,
            account_id=self.cfg.secrets.oanda_account_id,
            environment=self.cfg.secrets.oanda_environment,
            instrument=self.cfg.oanda_instrument,
            http_timeout=self.cfg.engine.http_timeout_seconds,
        )
        # news_provider=None: las noticias solo anotan la alerta, no cambian el setup.
        self.evaluator = Evaluator(self.cfg, news_provider=None)

        self._tz = ZoneInfo(self.cfg.operating_window.timezone)

    # ------------------------------------------------------------------
    def latest_validated_setups(self, now_ny: Optional[datetime] = None) -> List[ValidatedSetup]:
        """Corre el pipeline y devuelve los setups que pasan la validación estricta
        del ÚLTIMO candle cerrado. Lista vacía = no hay setup válido ahora (lo normal
        entre setups). NO envía ni ejecuta nada; es solo lectura de señal."""
        if now_ny is None:
            now_ny = datetime.now(self._tz)
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

        df_entry, _forming, _live_bias = self._bar_aggregator.fetch_and_prepare(self.ds, self.cfg)
        if df_entry is None or df_entry.empty or len(df_entry) < self.cfg.ma_length:
            return []
        results = self.evaluator.validate_last_closed_candle(df_entry, now_utc, now_ny)

        setups: List[ValidatedSetup] = []
        for _direction, payload in results.items():
            if payload is None:
                continue
            setups.append(ValidatedSetup(
                direction=_DIR_MAP[payload.direction],
                signal_direction=payload.direction,
                entry=float(payload.entry_estimated),
                sl=float(payload.sl_estimated),
                tp=float(payload.tp_estimated),
                risk_points=float(payload.risk_points),
                target_r=float(payload.target_r),
                instrument=payload.instrument,
                alert_id=payload.alert_id,
                confirm_candle_open=payload.confirm_candle_open,
            ))
        return setups

    def current_bias(self) -> str:
        """Bias que el engine busca ahora (para logging/contexto). LONG|SHORT|NONE."""
        df_entry, _forming, live_bias = self._bar_aggregator.fetch_and_prepare(self.ds, self.cfg)
        if df_entry is None or df_entry.empty:
            return "NONE"
        if self.cfg.filters.live_bias and live_bias in ("LONG", "SHORT"):
            return live_bias
        val = df_entry.iloc[-1].get("bias", "NONE")
        return str(val) if val in ("LONG", "SHORT", "NONE") else "NONE"

    def close(self) -> None:
        try:
            self.ds.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Prueba en vivo (SOLO LECTURA): arma la señal desde el engine de alertas.
    alerts = Path(__file__).resolve().parent.parent.parent / "us30_alerts"
    src = AlertsSignalSource(alerts)
    print(f"bias actual: {src.current_bias()}  (live_bias={src.cfg.filters.live_bias})")
    setups = src.latest_validated_setups()
    if not setups:
        print("sin setup válido en el último candle cerrado (normal entre setups).")
    for s in setups:
        print(f"SETUP {s.direction} ({s.signal_direction}) entry={s.entry} "
              f"sl={s.sl} tp={s.tp} risk={s.risk_points:.1f}pts id={s.alert_id[:12]}")
    src.close()
