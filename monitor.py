"""
LEVEL fare monitor — BCN<->SCL

Por cada corrida:
  - Escanea el calendario de precios (ambas direcciones) en los proximos
    GLOBAL_MONTHS meses (incluido el actual; el rango se mueve solo).
  - Por cada VENTANA de viaje: busca el dia mas barato de ida y vuelta y
    VALIDA el combo real en el buscador (precio verdadero + asientos).
    Solo alerta con precio validado; si el buscador no responde, lo marca
    como no confiable y NO manda correo (evita falsos positivos por lag).
  - Avisa si cualquier tramo suelto <= PER_LEG_ALERT en cualquier fecha.
  - Reintenta ante bloqueos de Akamai. Si la corrida sale poco confiable
    (muchos bloqueos), suprime correos y lo deja anotado en el status.
  - Escribe status con todos los precios por tramo (Job Summary + STATUS.md).

Correo OPCIONAL (secrets de Gmail). Sin ellos, solo genera el status.
"""

import json
import os
import random
import smtplib
import sys
import time
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
PROMO_LEG = 70          # <= 70 EUR => promo, alerta destacada
GLOBAL_MONTHS = 10      # proximos N meses incluyendo el actual (se mueve solo)

# --- Anti-bloqueo / confiabilidad ---
RETRIES = 3             # intentos por consulta ante bloqueo Akamai
RELIABLE_MAX_BLOCK = 0.34   # si se bloquea > 34% de las consultas, no alerta

# ----------------------------------------------------------------------

CAL_URL = "https://www.flylevel.com/nwe/flights/api/calendar/"
FLIGHTS_URL = "https://www.flylevel.com/nwe/api/flights/"
STATE_FILE = Path("state.json")
STATUS_FILE = Path("STATUS.md")

UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
     "Gecko/20100101 Firefox/125.0"),
]


def base_headers():
    return {
        "User-Agent": random.choice(UAS),
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
    y, m = start_ym
    out = []
    for _ in range(n):
        out.append((m, y))
        y, m = y + (m == 12), (m % 12) + 1
    return out


def months_spanning(d1, d2):
    a, b = date.fromisoformat(d1), date.fromisoformat(d2)
    return month_list((a.year, a.month), (b.year, b.month))


def looks_blocked(text):
    t = (text or "")[:400].lower()
    return "<!doctype html" in t or "<html" in t


def get_with_retry(session, url, params, label):
    """GET con reintentos ante bloqueo Akamai. Devuelve dict JSON o None."""
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, headers=base_headers(),
                            timeout=30)
        except requests.RequestException as exc:
            print(f"[WARN] red {label} intento {attempt}/{RETRIES}: {exc}")
            time.sleep(2 * attempt + random.uniform(0, 1))
            continue
        if r.status_code == 200 and not looks_blocked(r.text):
            try:
                return r.json()
            except ValueError:
                print(f"[WARN] no-JSON {label} intento {attempt}/{RETRIES}")
        elif looks_blocked(r.text):
            print(f"[WARN] bloqueo Akamai {label} "
                  f"intento {attempt}/{RETRIES}")
        else:
            print(f"[WARN] HTTP {r.status_code} {label} "
                  f"intento {attempt}/{RETRIES}")
        time.sleep(2 * attempt + random.uniform(0, 1.5))
    return None


def fetch_calendar(session, origin, dest, month, year):
    params = {"triptype": "RT", "origin": origin, "destination": dest,
              "month": f"{month:02d}", "year": str(year),
              "currencyCode": "EUR", "originType": "flights"}
    data = get_with_retry(session, CAL_URL, params,
                          f"cal {origin}->{dest} {month}/{year}")
    if not data:
        return None
    try:
        return data["data"]["dayPrices"]
    except (KeyError, TypeError):
        return None


def leg_min(fares_economy):
    best = None
    for f in fares_economy:
        if f.get("totalPrice") is None:
            continue
        if best is None or f["totalPrice"] < best["totalPrice"]:
            best = f
    if not best:
        return None
    return (round(best["totalPrice"], 2), best.get("availability"),
            best.get("tags") or [])


def fetch_flights(session, o, d, dd1, dd2):
    params = {"o1": o, "d1": d, "dd1": dd1, "dd2": dd2,
              "ADT": 1, "CHD": 0, "INL": 0, "r": "true", "mm": "true",
              "forcedCurrency": "EUR", "forcedCulture": "es-ES",
              "newecom": "true", "originType": "flights"}
    data = get_with_retry(session, FLIGHTS_URL, params,
                          f"flights {o}->{d} {dd1}/{dd2}")
    if not data:
        return None
    try:
        fi = data["flightsInfo"]
    except (KeyError, TypeError):
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
        s.get("https://www.flylevel.com/", headers=base_headers(), timeout=30)
    except requests.RequestException:
        pass

    # 1) Calendario: proximos N meses + meses de ventanas (por si caen fuera)
    today = datetime.now(timezone.utc).date()
    scan_months = next_months((today.year, today.month), GLOBAL_MONTHS)
    scan_set = set(scan_months)
    fetch_months = list(scan_months)
    for w in WINDOWS:
        for my in (months_spanning(w["out_from"], w["out_to"])
                   + months_spanning(w["ret_from"], w["ret_to"])):
            if my not in scan_set and my not in fetch_months:
                fetch_months.append(my)

    cal = {}
    blocked = 0
    attempts = 0
    for direction, (o, d) in {"BCN->SCL": ("BCN", "SCL"),
                              "SCL->BCN": ("SCL", "BCN")}.items():
        for m, y in fetch_months:
            attempts += 1
            dp = fetch_calendar(s, o, d, m, y)
            if dp is None:
                blocked += 1
                continue
            for p in dp:
                if p.get("price"):
                    cal[(direction, p["date"])] = {
                        "price": p["price"], "tags": p.get("tags") or []}
            time.sleep(random.uniform(0.4, 1.0))   # politeness entre llamadas

    block_ratio = blocked / max(1, attempts)
    reliable = block_ratio <= RELIABLE_MAX_BLOCK

    def dir_days(direction):
        return [{"date": k[1], **v} for k, v in cal.items() if k[0] == direction]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sm0, smN = scan_months[0], scan_months[-1]
    md = [f"## LEVEL monitor — {now}\n",
          f"Ruta BCN↔SCL · escaneo {sm0[1]}-{sm0[0]:02d} a "
          f"{smN[1]}-{smN[0]:02d} ({GLOBAL_MONTHS} meses) · "
          f"bloqueos: {blocked}/{attempts}\n"]
    if not reliable:
        md.append(f"> ⚠️ **Corrida poco confiable** ({blocked}/{attempts} "
                  f"consultas bloqueadas por Level). No se envían correos "
                  f"esta vez para evitar falsos positivos.\n")
    new_alerts = []

    # 2) Ventanas: SOLO alerta con combo validado por el buscador
    md.append("### Tus ventanas (total ida+vuelta)\n")
    for w in WINDOWS:
        out_c = cheapest_day(dir_days("BCN->SCL"), w["out_from"], w["out_to"])
        in_c = cheapest_day(dir_days("SCL->BCN"), w["ret_from"], w["ret_to"])
        if not out_c or not in_c:
            md.append(f"**{w['name']}** — sin datos de calendario "
                      f"(bloqueo o sin vuelos)\n")
            continue

        real = fetch_flights(s, "BCN", "SCL", out_c["date"], in_c["date"])
        link = deep_link(out_c["date"], in_c["date"])

        if not real:
            # Buscador no validó: mostramos estimado pero NO alertamos
            est = round(out_c["price"] + in_c["price"], 2)
            md.append(
                f"**{w['name']}** ⚠️ — estimado €{est} "
                f"(buscador no respondió, *no se alerta*)  \n"
                f"&nbsp;&nbsp;IDA {out_c['date']}: €{out_c['price']} · "
                f"VUELTA {in_c['date']}: €{in_c['price']}  \n"
                f"&nbsp;&nbsp;[Buscar]({link})\n")
            continue

        total = real["total"]
        o_pr, i_pr = real["out_price"], real["in_price"]
        o_se, i_se = real["out_seats"], real["in_seats"]
        promo = (o_pr <= PROMO_LEG or i_pr <= PROMO_LEG)
        hit = total < w["max_total"] or promo
        flag = "🔥" if promo else ("✅" if hit else "")
        seats = lambda x: f" ({x} asientos)" if x is not None else ""
        md.append(
            f"**{w['name']}** {flag} — total **€{total}** "
            f"(límite €{w['max_total']}) · _combo real_  \n"
            f"&nbsp;&nbsp;IDA {out_c['date']}: €{o_pr}{seats(o_se)} · "
            f"VUELTA {in_c['date']}: €{i_pr}{seats(i_se)}  \n"
            f"&nbsp;&nbsp;[Reservar]({link})\n")

        if hit:
            key = f"WIN|{w['name']}|{out_c['date']}|{in_c['date']}|{int(total//50)}"
            txt = (f"[{w['name']}] TOTAL €{total} "
                   f"{'(PROMO!)' if promo else ''}\n"
                   f"  IDA {out_c['date']}: €{o_pr}{seats(o_se)}\n"
                   f"  VUELTA {in_c['date']}: €{i_pr}{seats(i_se)}\n"
                   f"  Reservar: {link}")
            new_alerts.append((key, txt))

    # 3) Tramos sueltos baratos (del calendario que SÍ respondió)
    md.append("\n### Tramos sueltos baratos (cualquier fecha)\n")
    cheap = []
    for direction in ("BCN->SCL", "SCL->BCN"):
        for d in sorted(dir_days(direction), key=lambda x: x["date"]):
            yy, mm = int(d["date"][:4]), int(d["date"][5:7])
            if (mm, yy) not in scan_set:
                continue
            if d["price"] <= PER_LEG_ALERT:
                cheap.append((direction, d["date"], d["price"]))
    if cheap:
        md.append("| Sentido | Fecha | Precio | |")
        md.append("|---|---|---|---|")
        for direction, dt, pr in cheap:
            mark = "🔥promo" if pr <= PROMO_LEG else "✅"
            md.append(f"| {direction} | {dt} | €{pr} | {mark} |")
            key = f"LEG|{direction}|{dt}|{int(pr//20)}"
            new_alerts.append(
                (key, f"TRAMO SUELTO {direction} {dt}: €{pr} "
                      f"{'(PROMO!)' if pr <= PROMO_LEG else ''}"))
    else:
        md.append(f"_ningún tramo ≤ €{PER_LEG_ALERT} en el rango._\n")

    # 4) Filtrar a solo alertas NUEVAS
    fresh = [(k, t) for (k, t) in new_alerts if k not in alerted]
    if fresh and reliable:
        md.insert(2, f"> 🔥 **{len(fresh)} alerta(s) nueva(s) — ver abajo**\n")

    md.append(f"\n---\n_correo: {'activo' if email_on() else 'no configurado'} "
              f"· confiable: {'sí' if reliable else 'no'}_")
    write_status("\n".join(md))
    print(f"[INFO] {now} | alertas_nuevas={len(fresh)} "
          f"bloqueos={blocked}/{attempts} confiable={reliable}")

    # 5) Correo SOLO si la corrida es confiable
    sent = False
    if fresh and reliable and email_on():
        body = "ALERTAS LEVEL\n\n" + "\n\n".join(t for _, t in fresh)
        body += ("\n\n---\nOjo: el calendario puede tener lag. Confirma en el "
                 "buscador y compra al toque. Asientos = por bucket de tarifa, "
                 "no del avión entero.")
        send_email(f"LEVEL: {len(fresh)} alerta(s) de vuelos baratos", body)
        sent = True

    # Solo marcamos como 'ya avisado' lo que de verdad se envió
    if sent:
        for k, _ in fresh:
            alerted.add(k)

    state.update({"alerted": sorted(alerted), "last_run": now,
                  "last_alerts": len(fresh), "last_blocked": blocked,
                  "last_attempts": attempts, "reliable": reliable})
    STATE_FILE.write_text(json.dumps(state, indent=1))

    # Rojo SOLO si se bloqueó TODO (señal de revisar Plan B)
    if attempts and blocked == attempts:
        print("[ERROR] todas las consultas bloqueadas — ver Plan B (local)")
        sys.exit(1)


if __name__ == "__main__":
    main()
