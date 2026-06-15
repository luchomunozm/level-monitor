"""
LEVEL fare monitor — BCN<->SCL

Por cada corrida:
  - Escanea el calendario de precios (ambas direcciones) en un rango amplio.
  - Para cada VENTANA de viaje: busca el dia mas barato de ida y de vuelta,
    valida el combo real en el buscador (total verdadero + asientos) y avisa
    si el TOTAL ida+vuelta baja del presupuesto de esa ventana.
  - ADEMAS: avisa si CUALQUIER tramo suelto baja de PER_LEG_ALERT en
    cualquier fecha del rango (util para mover planes si algo sale regalado).
  - Escribe un status con TODOS los precios por tramo (visibilidad total) en
    el Job Summary de Actions y en STATUS.md.

El correo es OPCIONAL (secrets de Gmail). Sin ellos, solo genera el status.
"""

import json
import os
import smtplib
import sys
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ============================ CONFIG ==================================

# --- Ventanas de viaje: total ida+vuelta (EUR) ---
WINDOWS = [
    {"name": "Matri de Benja",
     "out_from": "2027-01-29", "out_to": "2027-01-29",   # ida fija
     "ret_from": "2027-02-01", "ret_to": "2027-02-01",   # vuelta fija
     "max_total": 1000},
    {"name": "Navidad con mama",
     "out_from": "2026-12-15", "out_to": "2026-12-24",
     "ret_from": "2026-12-26", "ret_to": "2027-01-05",
     "max_total": 900},
    {"name": "Marzo break + cumple",
     "out_from": "2027-03-20", "out_to": "2027-03-26",
     "ret_from": "2027-03-23", "ret_to": "2027-03-28",
     "max_total": 600},
]

# --- Escaneo global: cualquier tramo barato, cualquier fecha ---
PER_LEG_ALERT = 99      # avisa si un tramo (un sentido) <= 99 EUR
PROMO_LEG = 70          # <= 70 EUR => promo 9EUR, alerta destacada
GLOBAL_MONTHS = 10      # escaneo = proximos N meses INCLUYENDO el actual.
                        # Level publica ~10 meses; el rango se mueve solo
                        # en cada corrida (no hay que editar fechas nunca).

# ----------------------------------------------------------------------

CAL_URL = "https://www.flylevel.com/nwe/flights/api/calendar/"
FLIGHTS_URL = "https://www.flylevel.com/nwe/api/flights/"
STATE_FILE = Path("state.json")
STATUS_FILE = Path("STATUS.md")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/137.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9",
    "Referer": "https://www.flylevel.com/Flight/Select/",
}

# ============================ HELPERS =================================


def month_list(ym_from, ym_to):
    (ya, ma), (yb, mb) = ym_from, ym_to
    out, y, m = [], ya, ma
    while (y, m) <= (yb, mb):
        out.append((m, y))
        y, m = y + (m == 12), (m % 12) + 1
    return out


def next_months(start_ym, n):
    """n meses (m, y) empezando en start_ym=(anio, mes), incluido."""
    y, m = start_ym
    out = []
    for _ in range(n):
        out.append((m, y))
        y, m = y + (m == 12), (m % 12) + 1
    return out


def months_spanning(d1, d2):
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return month_list((a.year, a.month), (b.year, b.month))


def fetch_calendar(session, origin, dest, month, year):
    params = {"triptype": "RT", "origin": origin, "destination": dest,
              "month": f"{month:02d}", "year": str(year),
              "currencyCode": "EUR", "originType": "flights"}
    try:
        r = session.get(CAL_URL, params=params, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"[WARN] red cal {origin}->{dest} {month}/{year}: {exc}")
        return None
    if r.status_code != 200:
        print(f"[WARN] HTTP {r.status_code} cal {origin}->{dest} "
              f"{month}/{year} (posible Akamai)")
        return None
    try:
        return r.json()["data"]["dayPrices"]
    except (ValueError, KeyError):
        print(f"[WARN] no-JSON cal {origin}->{dest}: {r.text[:80].strip()}")
        return None


def leg_min(fares_economy):
    """(precio_total_min, asientos_de_esa_tarifa, tags) de una lista de fares."""
    best = None
    for f in fares_economy:
        if f.get("totalPrice") is None:
            continue
        if best is None or f["totalPrice"] < best["totalPrice"]:
            best = f
    if not best:
        return None
    return (round(best["totalPrice"], 2),
            best.get("availability"),
            best.get("tags") or [])


def fetch_flights(session, o, d, dd1, dd2):
    """Combo real ida+vuelta -> dict con totales, asientos y tags, o None."""
    params = {"o1": o, "d1": d, "dd1": dd1, "dd2": dd2,
              "ADT": 1, "CHD": 0, "INL": 0, "r": "true", "mm": "true",
              "forcedCurrency": "EUR", "forcedCulture": "es-ES",
              "newecom": "true", "originType": "flights"}
    try:
        r = session.get(FLIGHTS_URL, params=params, headers=HEADERS, timeout=30)
    except requests.RequestException as exc:
        print(f"[WARN] red flights {o}->{d} {dd1}/{dd2}: {exc}")
        return None
    if r.status_code != 200:
        print(f"[WARN] HTTP {r.status_code} flights {o}->{d} (posible Akamai)")
        return None
    try:
        fi = r.json()["flightsInfo"]
    except (ValueError, KeyError):
        return None

    def best_of(journeys):
        cand = []
        for j in journeys or []:
            eco = (j.get("fares") or {}).get("Economy") or []
            lm = leg_min(eco)
            if lm:
                cand.append(lm)
        return min(cand, key=lambda x: x[0]) if cand else None

    out = best_of(fi.get("outboundJourneys"))
    inb = best_of(fi.get("inboundJourneys"))
    if not out or not inb:
        return None
    return {"out_price": out[0], "out_seats": out[1], "out_tags": out[2],
            "in_price": inb[0], "in_seats": inb[1], "in_tags": inb[2],
            "total": round(out[0] + inb[0], 2)}


def cheapest_day(day_prices, dfrom, dto):
    cands = [d for d in (day_prices or [])
             if d.get("price") and dfrom <= d["date"] <= dto]
    return min(cands, key=lambda x: x["price"]) if cands else None


def deep_link(out_date, ret_date):
    return ("https://www.flylevel.com/Flight/Select/"
            f"?o1=BCN&d1=SCL&dd1={out_date}&dd2={ret_date}"
            "&ADT=1&CHD=0&INL=0&r=true&mm=true&forcedCurrency=EUR&newecom=true")


def email_on():
    return all(os.environ.get(k) for k in
               ("GMAIL_USER", "GMAIL_APP_PASSWORD", "EMAIL_TO"))


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = (
        subject, os.environ["GMAIL_USER"], os.environ["EMAIL_TO"])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)
    print(f"[OK] correo enviado a {os.environ['EMAIL_TO']}")


def write_status(md):
    STATUS_FILE.write_text(md, encoding="utf-8")
    sp = os.environ.get("GITHUB_STEP_SUMMARY")
    if sp:
        with open(sp, "a", encoding="utf-8") as fh:
            fh.write(md)


# ============================== MAIN ==================================


def main():
    state = (json.loads(STATE_FILE.read_text())
             if STATE_FILE.exists() else {"alerted": []})
    alerted = set(state["alerted"])

    s = requests.Session()
    try:
        s.get("https://www.flylevel.com/", headers=HEADERS, timeout=30)
    except requests.RequestException:
        pass

    # 1) Calendario: escaneo global = proximos N meses desde el mes actual.
    today = datetime.now(timezone.utc).date()
    scan_months = next_months((today.year, today.month), GLOBAL_MONTHS)
    scan_set = set(scan_months)
    # Aseguramos consultar tambien los meses de las ventanas (por si alguna
    # cae fuera del rango de escaneo).
    fetch_months = list(scan_months)
    for w in WINDOWS:
        for my in (months_spanning(w["out_from"], w["out_to"])
                   + months_spanning(w["ret_from"], w["ret_to"])):
            if my not in scan_set and my not in fetch_months:
                fetch_months.append(my)

    cal = {}        # (dir, "YYYY-MM-DD") -> {price, tags}
    blocked = 0
    for direction, (o, d) in {"BCN->SCL": ("BCN", "SCL"),
                              "SCL->BCN": ("SCL", "BCN")}.items():
        for m, y in fetch_months:
            dp = fetch_calendar(s, o, d, m, y)
            if dp is None:
                blocked += 1
                continue
            for p in dp:
                if p.get("price"):
                    cal[(direction, p["date"])] = {
                        "price": p["price"], "tags": p.get("tags") or []}

    def dir_days(direction):
        return [{"date": k[1], **v} for k, v in cal.items() if k[0] == direction]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sm0, smN = scan_months[0], scan_months[-1]
    md = [f"## LEVEL monitor — {now}\n",
          f"Ruta BCN↔SCL · escaneo {sm0[1]}-{sm0[0]:02d} a "
          f"{smN[1]}-{smN[0]:02d} ({GLOBAL_MONTHS} meses) · "
          f"bloqueos: {blocked}\n"]
    new_alerts = []   # (key, texto)

    # 2) Ventanas: combo real ida+vuelta
    md.append("### Tus ventanas (total ida+vuelta)\n")
    for w in WINDOWS:
        out_c = cheapest_day(dir_days("BCN->SCL"), w["out_from"], w["out_to"])
        in_c = cheapest_day(dir_days("SCL->BCN"), w["ret_from"], w["ret_to"])
        if not out_c or not in_c:
            md.append(f"**{w['name']}** — sin datos de calendario en el rango\n")
            continue

        real = fetch_flights(s, "BCN", "SCL", out_c["date"], in_c["date"])
        if real:
            total = real["total"]
            o_pr, i_pr = real["out_price"], real["in_price"]
            o_se, i_se = real["out_seats"], real["in_seats"]
            tags = set(real["out_tags"]) | set(real["in_tags"])
            src = "combo real"
        else:
            total = out_c["price"] + in_c["price"]
            o_pr, i_pr = out_c["price"], in_c["price"]
            o_se = i_se = None
            tags = set(out_c["tags"]) | set(in_c["tags"])
            src = "estimado calendario (buscador no respondio)"

        promo = (o_pr <= PROMO_LEG or i_pr <= PROMO_LEG)
        hit = total < w["max_total"] or promo
        flag = "🔥" if promo else ("✅" if hit else "")
        link = deep_link(out_c["date"], in_c["date"])

        seats = lambda x: f" ({x} asientos)" if x is not None else ""
        md.append(
            f"**{w['name']}** {flag} — total **€{total}** "
            f"(límite €{w['max_total']}) · _{src}_  \n"
            f"&nbsp;&nbsp;IDA {out_c['date']}: €{o_pr}{seats(o_se)} · "
            f"VUELTA {in_c['date']}: €{i_pr}{seats(i_se)}  \n"
            f"&nbsp;&nbsp;[Reservar]({link})\n")

        if hit:
            key = f"WIN|{w['name']}|{out_c['date']}|{in_c['date']}|{int(total//50)}"
            if key not in alerted:
                txt = (f"[{w['name']}] TOTAL €{total} "
                       f"{'(PROMO 9EUR!)' if promo else ''}\n"
                       f"  IDA {out_c['date']}: €{o_pr}{seats(o_se)}\n"
                       f"  VUELTA {in_c['date']}: €{i_pr}{seats(i_se)}\n"
                       f"  Reservar: {link}")
                new_alerts.append((key, txt))

    # 3) Escaneo global: tramos sueltos baratos
    md.append("\n### Tramos sueltos baratos (cualquier fecha)\n")
    cheap_legs = []
    for direction in ("BCN->SCL", "SCL->BCN"):
        for d in sorted(dir_days(direction), key=lambda x: x["date"]):
            yy, mm = int(d["date"][:4]), int(d["date"][5:7])
            if (mm, yy) not in scan_set:
                continue   # solo el rango de 10 meses, no meses extra de ventana
            if d["price"] <= PER_LEG_ALERT:
                cheap_legs.append((direction, d["date"], d["price"], d["tags"]))
    if cheap_legs:
        md.append("| Sentido | Fecha | Precio | |")
        md.append("|---|---|---|---|")
        for direction, dt, pr, tg in cheap_legs:
            mark = "🔥9€" if pr <= PROMO_LEG else "✅"
            md.append(f"| {direction} | {dt} | €{pr} | {mark} |")
            key = f"LEG|{direction}|{dt}|{int(pr//20)}"
            if key not in alerted:
                new_alerts.append(
                    (key, f"TRAMO SUELTO {direction} {dt}: €{pr} "
                          f"{'(PROMO 9EUR!)' if pr <= PROMO_LEG else ''}"))
    else:
        md.append(f"_ningún tramo ≤ €{PER_LEG_ALERT} en el rango escaneado._\n")

    # 4) Status + correo
    if new_alerts:
        md.insert(2, f"> 🔥 **{len(new_alerts)} alerta(s) nueva(s) — ver abajo**\n")
    md.append(f"\n---\n_correo: {'activo' if email_on() else 'no configurado'}_")
    write_status("\n".join(md))
    print(f"[INFO] {now} | alertas_nuevas={len(new_alerts)} bloqueos={blocked}")

    if new_alerts and email_on():
        body = "ALERTAS LEVEL\n\n" + "\n\n".join(t for _, t in new_alerts)
        body += ("\n\n---\nOjo: el calendario puede tener lag. Confirma en el "
                 "buscador y compra al tiro. Los asientos son por bucket de "
                 "tarifa, no del avion entero.")
        n = len(new_alerts)
        send_email(f"LEVEL: {n} alerta(s) de vuelos baratos", body)

    for key, _ in new_alerts:
        alerted.add(key)
    state.update({"alerted": sorted(alerted), "last_run": now,
                  "last_alerts": len(new_alerts), "last_blocked": blocked})
    STATE_FILE.write_text(json.dumps(state, indent=1))

    if blocked >= len(fetch_months) and blocked > 0:
        print("[ERROR] todo bloqueado — ver Plan B (correr local)")
        sys.exit(1)


if __name__ == "__main__":
    main()
