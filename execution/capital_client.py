"""
Cliente REST de Capital.com para us30_trader.

- Session manager: la sesión de Capital.com caduca a los ~10 min de inactividad.
  Este cliente re-autentica proactivamente (antes de que expire) y también ante un
  401, reintentando la llamada una vez.
- Wrappers de LECTURA: accounts, market(epic), search, positions, confirm.
- Wrappers de ESCRITURA (open/update-stop/close): SOLO se ejecutan si
  `allow_execution=True`. En modo `shadow` el flag es False y cualquier intento de
  escritura levanta ExecutionDisabled → imposible mandar una orden por accidente.

Sin dependencias externas (urllib de la stdlib). Pensado para correr nativo en la
MacBook Air M5 y el VPS Linux, sin Windows.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("us30_trader.capital")

HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}

# Re-auth proactivo antes del límite de 10 min de inactividad.
_SESSION_REFRESH_AFTER = 540.0   # 9 min
# Rate limit de creación de posiciones: 1 cada 0.1 s (dejamos margen).
_MIN_ORDER_INTERVAL = 0.12
# Reintento de login ante rate-limit de creación de sesión (error.too-many.requests).
_LOGIN_BACKOFF = (15.0, 30.0, 60.0)   # segundos entre reintentos


class CapitalError(Exception):
    """Error de la API o del cliente."""

    def __init__(self, message: str, status: int | None = None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class ExecutionDisabled(CapitalError):
    """Se intentó una escritura con allow_execution=False (modo shadow)."""


@dataclass
class CapitalConfig:
    api_key: str
    identifier: str
    password: str
    environment: str = "demo"       # 'demo' | 'live'
    allow_execution: bool = False   # False = shadow (no manda órdenes)

    @classmethod
    def from_env(cls, env_path: str | Path, allow_execution: bool = False) -> "CapitalConfig":
        env = _load_env(Path(env_path))
        missing = [k for k in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_API_PASSWORD")
                   if not env.get(k) or env[k].startswith("replace_")]
        if missing:
            raise CapitalError(f"faltan credenciales en {env_path}: {', '.join(missing)}")
        environment = env.get("CAPITAL_ENVIRONMENT", "demo").lower()
        if environment not in HOSTS:
            raise CapitalError(f"CAPITAL_ENVIRONMENT invalido: {environment!r}")
        return cls(
            api_key=env["CAPITAL_API_KEY"],
            identifier=env["CAPITAL_IDENTIFIER"],
            password=env["CAPITAL_API_PASSWORD"],
            environment=environment,
            allow_execution=allow_execution,
        )


def _load_env(path: Path) -> dict:
    if not path.exists():
        raise CapitalError(f"no existe {path} (copia .env.example a .env)")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if " #" in val:                       # comentario inline
            val = val.split(" #", 1)[0].strip()
        env[key.strip()] = val
    return env


class CapitalClient:
    def __init__(self, config: CapitalConfig):
        self.cfg = config
        self.base = HOSTS[config.environment]
        self._cst: str | None = None
        self._xsec: str | None = None
        self._last_activity = 0.0
        self._last_order_ts = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ HTTP
    def _raw_http(self, method, url, headers, body=None, net_retries=2):
        """HTTP crudo. Reintenta ante errores de RED transitorios (TLS/timeout/EOF),
        no ante HTTP≥400 (esos se devuelven para que _request los maneje)."""
        data = json.dumps(body).encode() if body is not None else None
        last_err = None
        for attempt in range(net_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    payload = resp.read().decode()
                    parsed = json.loads(payload) if payload else {}
                    return resp.status, dict(resp.headers), parsed
            except urllib.error.HTTPError as e:
                payload = e.read().decode()
                try:
                    parsed = json.loads(payload)
                except Exception:
                    parsed = {"raw": payload}
                return e.code, dict(e.headers), parsed
            except urllib.error.URLError as e:
                last_err = e.reason
                if attempt < net_retries:
                    log.warning("error de red (%s) en %s %s — reintento %d/%d",
                                e.reason, method, url, attempt + 1, net_retries)
                    time.sleep(1.0 + attempt)
        raise CapitalError(f"error de red tras {net_retries + 1} intentos: {last_err}")

    # ----------------------------------------------------------- SESSION MGMT
    def login(self) -> None:
        with self._lock:
            last = (None, None, None)
            for attempt in range(len(_LOGIN_BACKOFF) + 1):
                status, hdrs, payload = self._raw_http(
                    "POST", f"{self.base}/api/v1/session",
                    headers={"X-CAP-API-KEY": self.cfg.api_key,
                             "Content-Type": "application/json"},
                    body={"identifier": self.cfg.identifier, "password": self.cfg.password},
                )
                if status == 200:
                    self._cst = hdrs.get("CST")
                    self._xsec = hdrs.get("X-SECURITY-TOKEN")
                    if not self._cst or not self._xsec:
                        raise CapitalError("login sin tokens CST/X-SECURITY-TOKEN", status, payload)
                    self._last_activity = time.monotonic()
                    log.info("Capital.com sesión iniciada (%s)", self.cfg.environment)
                    return
                code = payload.get("errorCode") if isinstance(payload, dict) else None
                last = (status, code, payload)
                # Rate-limit de sesión → esperar y reintentar (no es un error de credenciales).
                if (status == 429 or code == "error.too-many.requests") and attempt < len(_LOGIN_BACKOFF):
                    wait = _LOGIN_BACKOFF[attempt]
                    log.warning("login rate-limited (%s) → espera %.0fs y reintenta (%d/%d)",
                                code, wait, attempt + 1, len(_LOGIN_BACKOFF))
                    time.sleep(wait)
                    continue
                break
            status, code, payload = last
            raise CapitalError(f"login fallido: {code or payload}", status, payload)

    def ping(self) -> bool:
        """Mantiene viva la sesión sin re-crearla (evita el rate-limit de /session).
        GET liviano; resetea la inactividad tanto acá como del lado de Capital.com."""
        try:
            self._request("GET", "/api/v1/ping")
            return True
        except CapitalError as e:
            log.warning("ping falló: %s", e)
            return False

    def _ensure_session(self) -> None:
        if self._cst is None or (time.monotonic() - self._last_activity) > _SESSION_REFRESH_AFTER:
            self.login()

    def _auth_headers(self) -> dict:
        return {"CST": self._cst, "X-SECURITY-TOKEN": self._xsec,
                "Content-Type": "application/json"}

    def _request(self, method, path, body=None):
        """Request autenticado con re-auth proactivo + reintento único ante 401."""
        with self._lock:
            self._ensure_session()
            url = f"{self.base}{path}"
            status, _, payload = self._raw_http(method, url, self._auth_headers(), body)
            if status == 401:                         # sesión vencida → re-login y reintento
                log.warning("401 en %s %s → re-login y reintento", method, path)
                self.login()
                status, _, payload = self._raw_http(method, url, self._auth_headers(), body)
            if status >= 400:
                raise CapitalError(f"{method} {path} → HTTP {status}: "
                                   f"{payload.get('errorCode', payload)}", status, payload)
            self._last_activity = time.monotonic()
            return payload

    # ---------------------------------------------------------------- LECTURA
    def accounts(self) -> list:
        return self._request("GET", "/api/v1/accounts").get("accounts", [])

    def preferred_account(self) -> dict:
        accs = self.accounts()
        for a in accs:
            if a.get("preferred"):
                return a
        if not accs:
            raise CapitalError("la cuenta no tiene subcuentas")
        return accs[0]

    def balance(self) -> dict:
        """Devuelve el bloque balance {balance, available, deposit, profitLoss} de la preferida."""
        return self.preferred_account().get("balance", {})

    def search_markets(self, term: str) -> list:
        return self._request("GET", f"/api/v1/markets?searchTerm={term}").get("markets", [])

    def market(self, epic: str) -> dict:
        """Detalle completo de un instrumento (instrument, dealingRules, snapshot)."""
        return self._request("GET", f"/api/v1/markets/{epic}")

    def current_price(self, epic: str) -> dict:
        """{bid, offer, spread} del snapshot — para spread guard y logging shadow."""
        snap = self.market(epic).get("snapshot", {})
        bid, offer = snap.get("bid"), snap.get("offer")
        spread = (offer - bid) if (bid is not None and offer is not None) else None
        return {"bid": bid, "offer": offer, "spread": spread,
                "status": snap.get("marketStatus")}

    def positions(self) -> list:
        return self._request("GET", "/api/v1/positions").get("positions", [])

    def positions_for_epic(self, epic: str) -> list:
        """Posiciones abiertas de un epic (para reconciliación al arrancar)."""
        out = []
        for p in self.positions():
            mkt = p.get("market", {})
            if mkt.get("epic") == epic:
                out.append(p)
        return out

    def position(self, deal_id: str) -> dict:
        return self._request("GET", f"/api/v1/positions/{deal_id}")

    def confirm(self, deal_reference: str) -> dict:
        """Estado de una orden por su dealReference → trae dealId, status, level."""
        return self._request("GET", f"/api/v1/confirms/{deal_reference}")

    def activities(self, last_period_seconds: int = 3600) -> list:
        """Historial de actividad. Cada cierre de POSITION trae `source` (SL/TP/USER)."""
        return self._request(
            "GET", f"/api/v1/history/activity?lastPeriod={int(last_period_seconds)}"
        ).get("activities", [])

    def transactions(self, last_period_seconds: int = 3600) -> list:
        """Historial de transacciones → trae el P/L realizado por dealId."""
        return self._request(
            "GET", f"/api/v1/history/transactions?lastPeriod={int(last_period_seconds)}"
        ).get("transactions", [])

    # ---------------------------------------------------------------- ESCRITURA
    def _require_execution(self, action: str) -> None:
        if not self.cfg.allow_execution:
            raise ExecutionDisabled(
                f"'{action}' bloqueado: allow_execution=False (modo shadow). "
                f"No se envió ninguna orden."
            )

    def _throttle_order(self) -> None:
        delta = time.monotonic() - self._last_order_ts
        if delta < _MIN_ORDER_INTERVAL:
            time.sleep(_MIN_ORDER_INTERVAL - delta)
        self._last_order_ts = time.monotonic()

    def open_market_position(self, *, epic: str, direction: str, size: float,
                             stop_level: float, profit_level: float,
                             guaranteed_stop: bool = False) -> dict:
        """Market con SL y TP adjuntos. Devuelve {dealReference}. Requiere allow_execution."""
        self._require_execution("open_market_position")
        self._throttle_order()
        body = {
            "epic": epic,
            "direction": direction.upper(),
            "size": size,
            "stopLevel": stop_level,
            "profitLevel": profit_level,
            "guaranteedStop": guaranteed_stop,
        }
        return self._request("POST", "/api/v1/positions", body)

    def open_and_confirm(self, *, epic: str, direction: str, size: float,
                         stop_level: float, profit_level: float,
                         guaranteed_stop: bool = False, confirm_wait: float = 1.0) -> dict:
        """Abre y espera el confirm para devolver el dealId permanente + estado."""
        ref = self.open_market_position(
            epic=epic, direction=direction, size=size,
            stop_level=stop_level, profit_level=profit_level,
            guaranteed_stop=guaranteed_stop,
        ).get("dealReference")
        time.sleep(confirm_wait)
        return self.confirm(ref)

    def update_position_stop(self, deal_id: str, stop_level: float,
                             profit_level: float | None = None) -> dict:
        """Mueve el SL de una posición (implementa el Break Even). Requiere allow_execution."""
        self._require_execution("update_position_stop")
        body: dict = {"stopLevel": stop_level}
        if profit_level is not None:
            body["profitLevel"] = profit_level
        return self._request("PUT", f"/api/v1/positions/{deal_id}", body)

    def close_position(self, deal_id: str) -> dict:
        """Cierra una posición. Requiere allow_execution."""
        self._require_execution("close_position")
        return self._request("DELETE", f"/api/v1/positions/{deal_id}")


if __name__ == "__main__":
    # Smoke test de SOLO LECTURA (allow_execution=False por default).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    here = Path(__file__).resolve().parent.parent
    client = CapitalClient(CapitalConfig.from_env(here / ".env"))
    client.login()
    bal = client.balance()
    print("balance:", bal.get("balance"), "disponible:", bal.get("available"))
    print("US30 precio:", client.current_price("US30"))
    print("posiciones US30 abiertas:", len(client.positions_for_epic("US30")))
    # Prueba de la guarda de shadow:
    try:
        client.open_market_position(epic="US30", direction="BUY", size=0.001,
                                    stop_level=1, profit_level=1)
    except ExecutionDisabled as e:
        print("guarda shadow OK →", e)
