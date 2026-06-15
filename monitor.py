"""
LEVEL 9-euro fare monitor — BCN<->SCL
Consulta el API de calendario de flylevel.com, busca dias con tag "campaign"
(o precio bajo el umbral) dentro de tus ventanas de viaje, y manda correo
con el deep link listo para comprar.

Se ejecuta via GitHub Actions (ver .github/workflows/monitor.yml).
Secrets requeridos: GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO
"""

import json
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ----------------------------- CONFIG ---------------------------------

# Umbral: precio por tramo (EUR, base + tasas) para considerarlo "promo".
# La tarifa 9EUR aparece como ~30 EUR con tasas. Dejamos margen.
PRICE_THRESHOLD = 60

# Tus ventanas de viaje. outbound = BCN->SCL, return = SCL->BCN.
WINDOWS = [
    {"name": "Navidad con mama",
     "out_from": "2026-12-15", "out_to": "2026-12-24",
     "ret_from": "2027-01-02", "ret_to": "2027-01-08"},
    {"name": "Matrimonio + cumple",
     "out_from": "2027-01-27", "out_to": "2027-01-30",
     "ret_from": "2027-02-01", "ret_to": "2027-02-05"},
    {"name": "Marzo",
     "out_from": "2027-03-18", "out_to": "2027-03-23",
     "ret_from": "2027-03-25", "ret_to": "2027-03-30"},
]

CALENDAR_URL = "https://www.flylevel.com/nwe/flights/api/calendar/"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/137.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.flylevel.com/Flight/Select/",
}

# ----------------------------- HELPERS --------------------------------


def months_between(d1: str, d2: str):
    """Lista de (mes, anio) que cubren el rango d1..d2."""
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    months, cur = [], date(a.year, a.month, 1)
    while cur <= b:
        months.append((cur.month, cur.year))
        cur = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
    return months


def fetch_calendar(session, origin, dest, month, year):
    """Devuelve dayPrices o None si hubo bloqueo/error."""
    params = {
        "triptype": "RT", "origin": origin, "destination": dest,
        "month": f"{month:02d}", "year": str(year),
        "currencyCode": "EUR", "originType": "flights",
    }
    try:
        r = session.get(CALENDAR_URL, params=params,
                        headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"[WARN] red: {origin}->{dest} {month}/{year}: {exc}")
        return None
    if r.status_code != 200:
        print(f"[WARN] HTTP {r.status_code} en {origin}->{dest} "
              f"{month}/{year} (posible bloqueo Akamai)")
        return None
    try:
        return r.json()["data"]["dayPrices"]
    except (ValueError, KeyError):
        snippet = r.text[:120].replace("\n", " ")
        print(f"[WARN] respuesta no-JSON (posible interstitial Akamai): "
              f"{snippet}")
        return None


def find_promo_days(day_prices, date_from, date_to):
    """Dias dentro del rango con tag campaign/discounted o precio bajo."""
    hits = []
    for d in day_prices or []:
        if not d.get("price"):
            continue
        if not (date_from <= d["date"] <= date_to):
            continue
        tags = set(d.get("tags") or [])
        is_promo = bool(tags & {"campaign", "discounted_price"})
        if is_promo or d["price"] <= PRICE_THRESHOLD:
            hits.append({"date": d["date"], "price": d["price"],
                         "tags": sorted(tags)})
    return hits


def deep_link(out_date, ret_date):
    return ("https://www.flylevel.com/Flight/Select/"
            f"?o1=BCN&d1=SCL&dd1={out_date}&dd2={ret_date}"
            "&ADT=1&CHD=0&INL=0&r=true&mm=true"
            "&forcedCurrency=EUR&newecom=true")


def send_email(subject, body):
    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ["EMAIL_TO"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, pwd)
        smtp.send_message(msg)
    print(f"[OK] correo enviado a {to}")


# ------------------------------ MAIN -----------------------------------


def main():
    state = (json.loads(STATE_FILE.read_text())
             if STATE_FILE.exists() else {"alerted": []})
    alerted = set(state["alerted"])

    session = requests.Session()
    # Visita inicial al home para obtener cookies basicas
    try:
        session.get("https://www.flylevel.com/", headers=HEADERS, timeout=30)
    except requests.RequestException:
        pass

    findings, blocked = [], 0
    for w in WINDOWS:
        # Tramo ida BCN->SCL
        for m, y in months_between(w["out_from"], w["out_to"]):
            dp = fetch_calendar(session, "BCN", "SCL", m, y)
            if dp is None:
                blocked += 1
                continue
            for hit in find_promo_days(dp, w["out_from"], w["out_to"]):
                findings.append({**hit, "leg": "IDA BCN->SCL",
                                 "window": w["name"],
                                 "link": deep_link(hit["date"],
                                                   w["ret_to"])})
        # Tramo vuelta SCL->BCN (calendario de la ruta inversa)
        for m, y in months_between(w["ret_from"], w["ret_to"]):
            dp = fetch_calendar(session, "SCL", "BCN", m, y)
            if dp is None:
                blocked += 1
                continue
            for hit in find_promo_days(dp, w["ret_from"], w["ret_to"]):
                findings.append({**hit, "leg": "VUELTA SCL->BCN",
                                 "window": w["name"],
                                 "link": deep_link(w["out_from"],
                                                   hit["date"])})

    print(f"[INFO] {datetime.utcnow().isoformat()}Z | "
          f"hallazgos={len(findings)} bloqueos={blocked}")

    # Solo alertar hallazgos nuevos (evita spam cada 5 min)
    new = [f for f in findings
           if f"{f['leg']}|{f['date']}|{f['price']}" not in alerted]

    if new:
        lines = ["PROMO DETECTADA EN TUS FECHAS\n"]
        for f in new:
            lines.append(f"[{f['window']}] {f['leg']}")
            lines.append(f"  Fecha: {f['date']}  |  EUR {f['price']}  "
                         f"|  tags: {', '.join(f['tags'])}")
            lines.append(f"  Reservar: {f['link']}\n")
        lines.append("Ojo: el calendario puede tener lag. Confirma el precio "
                     "en el buscador y compra al tiro si esta.")
        send_email(
            f"LEVEL 9EUR: {len(new)} fecha(s) en promo - corre a comprar",
            "\n".join(lines))
        for f in new:
            alerted.add(f"{f['leg']}|{f['date']}|{f['price']}")

    state["alerted"] = sorted(alerted)
    state["last_run"] = datetime.utcnow().isoformat() + "Z"
    state["last_findings"] = len(findings)
    state["last_blocked"] = blocked
    STATE_FILE.write_text(json.dumps(state, indent=1))

    # Si TODO vino bloqueado, falla el job para que se note en Actions
    if blocked > 0 and not findings and blocked >= 6:
        print("[ERROR] Todas las consultas bloqueadas. "
              "Revisar plan B (correr local).")
        sys.exit(1)


if __name__ == "__main__":
    main()
