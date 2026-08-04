#!/usr/bin/env python3
"""
Verificación de la cuenta Capital.com para us30_trader (§11 de PLAN_CAPITALCOM.md).

SOLO LECTURA: autentica y consulta. NO envía ninguna orden.

Chequea:
  1. POST /session          -> autenticacion (CST + X-SECURITY-TOKEN)
  2. GET  /accounts         -> balance / disponible (para sizing y margen)
  3. GET  /markets?search   -> encontrar el epic real de US30 y si es operable
  4. GET  /markets/{epic}   -> minDealSize, contractSize, precision, marginFactor

Uso:
    cd "/Users/osskr10/Claude TEST/us30_trader"
    python3 verify_capital.py

Lee credenciales de ./.env  (nunca hardcodeadas). No imprime secrets.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).with_name(".env")

HOSTS = {
    "demo": "https://demo-api-capital.backend-capital.com",
    "live": "https://api-capital.backend-capital.com",
}


def load_env(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"ERROR: no existe {path}. Copia .env.example a .env y completalo.")
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        # sacar comentario inline tipo:  demo   # 'demo' o 'live'
        if " #" in val:
            val = val.split(" #", 1)[0].strip()
        env[key.strip()] = val
    return env


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
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
        sys.exit(f"ERROR de red: {e.reason}")


def main():
    env = load_env(ENV_PATH)

    api_key = env.get("CAPITAL_API_KEY", "")
    identifier = env.get("CAPITAL_IDENTIFIER", "")
    password = env.get("CAPITAL_API_PASSWORD", "")
    environment = env.get("CAPITAL_ENVIRONMENT", "demo").lower()

    missing = [k for k, v in {
        "CAPITAL_API_KEY": api_key,
        "CAPITAL_IDENTIFIER": identifier,
        "CAPITAL_API_PASSWORD": password,
    }.items() if not v or v.startswith("replace_")]
    if missing:
        sys.exit(f"ERROR: faltan credenciales en .env: {', '.join(missing)}")

    if environment not in HOSTS:
        sys.exit(f"ERROR: CAPITAL_ENVIRONMENT invalido: {environment!r} (usa 'demo' o 'live')")

    base = HOSTS[environment]
    print(f"== Verificacion Capital.com ({environment}) ==")
    print(f"Host: {base}\n")

    # ---- 1) AUTENTICACION ----
    print("[1/4] POST /session ...")
    status, hdrs, payload = http(
        "POST", f"{base}/api/v1/session",
        headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json"},
        body={"identifier": identifier, "password": password},
    )
    if status != 200:
        err = payload.get("errorCode") or payload
        sys.exit(f"  FALLO auth (HTTP {status}): {err}\n"
                 f"  Revisa API_KEY / IDENTIFIER / API_PASSWORD y que la key sea del entorno {environment}.")
    cst = hdrs.get("CST")
    xsec = hdrs.get("X-SECURITY-TOKEN")
    if not cst or not xsec:
        sys.exit("  FALLO: no llegaron los tokens CST / X-SECURITY-TOKEN.")
    print("  OK autenticado (tokens recibidos, no se imprimen).")
    cur = payload.get("currencyIsoCode") or payload.get("currency") or "?"
    print(f"  Moneda de la cuenta: {cur}\n")

    auth = {"CST": cst, "X-SECURITY-TOKEN": xsec, "Content-Type": "application/json"}

    # ---- 2) CUENTA / BALANCE ----
    print("[2/4] GET /accounts ...")
    status, _, payload = http("GET", f"{base}/api/v1/accounts", headers=auth)
    if status != 200:
        print(f"  aviso: HTTP {status}: {payload}")
    else:
        for acc in payload.get("accounts", []):
            bal = acc.get("balance", {})
            flag = " (preferida)" if acc.get("preferred") else ""
            print(f"  Cuenta {acc.get('accountId')}{flag} [{acc.get('currency')}]: "
                  f"balance={bal.get('balance')} disponible={bal.get('available')}")
    print()

    # ---- 3) BUSCAR US30 ----
    print("[3/4] GET /markets?searchTerm=US30 ...")
    status, _, payload = http("GET", f"{base}/api/v1/markets?searchTerm=US30", headers=auth)
    if status != 200:
        sys.exit(f"  FALLO busqueda (HTTP {status}): {payload}")
    markets = payload.get("markets", [])
    if not markets:
        sys.exit("  No se encontro ningun mercado para 'US30'. Proba otro searchTerm en la app.")
    print(f"  {len(markets)} resultado(s):")
    epic = None
    for m in markets:
        e = m.get("epic")
        name = m.get("instrumentName")
        st = m.get("marketStatus")
        print(f"    - epic={e!r:14} {name!r}  status={st}")
        # heuristica: preferir el que se llame US30 / Wall Street 30
        if epic is None and e:
            low = (name or "").lower()
            if "us30" in e.lower() or "wall street 30" in low or "us wall street" in low:
                epic = e
    if epic is None:
        epic = markets[0].get("epic")
    print(f"  -> epic elegido para specs: {epic!r}\n")

    # ---- 4) SPECS DEL INSTRUMENTO ----
    print(f"[4/4] GET /markets/{epic} ...")
    status, _, payload = http("GET", f"{base}/api/v1/markets/{epic}", headers=auth)
    if status != 200:
        sys.exit(f"  FALLO specs (HTTP {status}): {payload}")
    instr = payload.get("instrument", {})
    rules = payload.get("dealingRules", {})
    snap = payload.get("snapshot", {})

    def rule(name):
        r = rules.get(name, {})
        return f"{r.get('value')} ({r.get('unit')})" if isinstance(r, dict) else r

    print(f"  Nombre:            {instr.get('name')}")
    print(f"  Tipo:              {instr.get('type')}")
    print(f"  Operable (status): {snap.get('marketStatus')}")
    print(f"  contractSize:      {instr.get('contractSize')}")
    print(f"  minDealSize:       {rule('minDealSize')}")
    print(f"  maxDealSize:       {rule('maxDealSize')}")
    print(f"  minStopDistance:   {rule('minStopOrProfitDistance')}")
    print(f"  precision size:    {instr.get('lotSize')}  (lotSize)")
    mr = instr.get("marginFactor")
    print(f"  marginFactor:      {mr} {instr.get('marginFactorUnit') or ''}")
    print(f"  bid/offer actual:  {snap.get('bid')} / {snap.get('offer')}")

    # ---- volcado crudo para encontrar el valor por punto (no es sensible) ----
    print("\n--- RAW instrument (campos para sizing) ---")
    for k in sorted(instr.keys()):
        print(f"  {k}: {instr[k]}")
    print("--- RAW dealingRules ---")
    print(json.dumps(rules, indent=2))

    print("\n== OK: verificacion de LECTURA completa. No se envio ninguna orden. ==")
    print("Pasa esta salida (no tiene secrets) para ajustar el sizing y scaffoldear en shadow.")


if __name__ == "__main__":
    main()
