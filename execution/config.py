"""
Loader del config.yaml del trader.

Separación a propósito:
  - `parse_trader_config(dict)`: PURO (stdlib), con defaults + validación → testeable
    sin pyyaml ni red.
  - `load_trader_config(path)`: lee el YAML (usa pyyaml, presente en el venv de alertas)
    y delega en el parser.

Devuelve un `TraderConfig` que sabe construir el `ExecutorConfig` + `InstrumentSpec`
que consume el executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .executor import MODE_LIVE, MODE_PRACTICE, MODE_SHADOW, ExecutorConfig
from .sizing import InstrumentSpec

_VALID_MODES = {MODE_SHADOW, MODE_PRACTICE, MODE_LIVE}


class ConfigError(ValueError):
    pass


@dataclass
class TraderConfig:
    broker: str
    environment: str
    epic: str
    mode: str
    allow_live_env: bool
    risk_pct: float
    rr_target: float
    sl_buffer_points: float
    be_enabled: bool
    be_trigger_r: float
    be_buffer_points: float
    max_spread_points: float
    max_slippage_points: float
    min_units_action: str
    max_concurrent_positions: int
    max_daily_loss_pct: float
    # timing de la validación post-cierre (segundos tras el cierre 1H)
    validation_offset_seconds: int
    validation_retry_seconds: int
    validation_max_wait_seconds: int
    # instrument spec
    value_per_point: float
    size_increment: float
    min_deal_size: float
    max_deal_size: float
    margin_factor: float
    price_step: float

    def instrument_spec(self) -> InstrumentSpec:
        return InstrumentSpec(
            value_per_point=self.value_per_point,
            size_increment=self.size_increment,
            min_deal_size=self.min_deal_size,
            max_deal_size=self.max_deal_size,
            margin_factor=self.margin_factor,
            price_step=self.price_step,
        )

    def executor_config(self, mode_override: str | None = None) -> ExecutorConfig:
        return ExecutorConfig(
            epic=self.epic,
            risk_pct=self.risk_pct,
            rr_target=self.rr_target,
            sl_buffer_points=self.sl_buffer_points,
            be_trigger_r=self.be_trigger_r,
            be_buffer_points=self.be_buffer_points,
            max_spread_points=self.max_spread_points,
            max_concurrent_positions=self.max_concurrent_positions,
            max_daily_loss_pct=self.max_daily_loss_pct,
            mode=mode_override or self.mode,
            spec=self.instrument_spec(),
        )


def _req(d: Dict[str, Any], key: str, path: str):
    if key not in d:
        raise ConfigError(f"falta '{path}.{key}' en el config")
    return d[key]


def _pos(value, name: str) -> float:
    v = float(value)
    if v <= 0:
        raise ConfigError(f"'{name}' debe ser > 0 (es {v})")
    return v


def parse_trader_config(raw: Dict[str, Any]) -> TraderConfig:
    """Valida y arma el TraderConfig desde el dict crudo del YAML."""
    if not isinstance(raw, dict):
        raise ConfigError("config vacío o mal formado")
    ex = raw.get("execution")
    if not isinstance(ex, dict):
        raise ConfigError("falta la sección 'execution'")

    mode = str(_req(ex, "mode", "execution")).lower()
    if mode not in _VALID_MODES:
        raise ConfigError(f"mode inválido: {mode!r} (usa {sorted(_VALID_MODES)})")

    risk_pct = _pos(ex.get("risk_pct", 1.0), "execution.risk_pct")
    if risk_pct > 100:
        raise ConfigError(f"execution.risk_pct absurdo: {risk_pct} (> 100%)")
    rr_target = _pos(ex.get("rr_target", 2.0), "execution.rr_target")
    sl_buffer_points = float(ex.get("sl_buffer_points", 10.0))
    if sl_buffer_points < 0:
        raise ConfigError("execution.sl_buffer_points no puede ser negativo")

    be = ex.get("break_even", {}) or {}
    inst = ex.get("instrument", {}) or {}

    max_conc = int(ex.get("max_concurrent_positions", 1))
    if max_conc < 1:
        raise ConfigError("execution.max_concurrent_positions debe ser >= 1")

    min_units_action = str(ex.get("min_units_action", "skip")).lower()
    if min_units_action not in ("skip", "min"):
        raise ConfigError(f"min_units_action inválido: {min_units_action!r} (skip|min)")

    return TraderConfig(
        broker=str(ex.get("broker", "capitalcom")),
        environment=str(ex.get("environment", "demo")).lower(),
        epic=str(ex.get("epic", "US30")),
        mode=mode,
        allow_live_env=bool(ex.get("allow_live_env", False)),
        risk_pct=risk_pct,
        rr_target=rr_target,
        sl_buffer_points=sl_buffer_points,
        be_enabled=bool(be.get("enabled", True)),
        be_trigger_r=_pos(be.get("trigger_r", 1.0), "break_even.trigger_r"),
        be_buffer_points=float(be.get("buffer_points", 5.0)),
        max_spread_points=float(ex.get("max_spread_points", 15.0)),
        max_slippage_points=float(ex.get("max_slippage_points", 10.0)),
        min_units_action=min_units_action,
        max_concurrent_positions=max_conc,
        max_daily_loss_pct=_pos(ex.get("max_daily_loss_pct", 3.0), "execution.max_daily_loss_pct"),
        validation_offset_seconds=int(ex.get("validation_offset_seconds", 20)),
        validation_retry_seconds=max(1, int(ex.get("validation_retry_seconds", 5))),
        validation_max_wait_seconds=int(ex.get("validation_max_wait_seconds", 90)),
        value_per_point=_pos(inst.get("value_per_point", 1.0), "instrument.value_per_point"),
        size_increment=_pos(inst.get("size_increment", 0.001), "instrument.size_increment"),
        min_deal_size=_pos(inst.get("min_deal_size", 0.001), "instrument.min_deal_size"),
        max_deal_size=_pos(inst.get("max_deal_size", 500.0), "instrument.max_deal_size"),
        margin_factor=_pos(inst.get("margin_factor", 0.05), "instrument.margin_factor"),
        price_step=_pos(inst.get("price_step", 0.1), "instrument.price_step"),
    )


def load_trader_config(path: str | Path) -> TraderConfig:
    """Lee el YAML del disco y lo parsea. Requiere pyyaml (venv de alertas)."""
    import yaml  # import perezoso: los tests del parser no lo necesitan

    p = Path(path)
    if not p.exists():
        raise ConfigError(f"no existe el config: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return parse_trader_config(raw)
