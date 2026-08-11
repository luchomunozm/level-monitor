"""
LATAM fare monitor — matriz de precios minimos por dia (solo ida, por tramo)

Rutas monitoreadas (ambas direcciones de cada par) y meses configurables.
Cada corrida:
  1. Intenta el calendario bulk de LATAM (searchbox/v1/calendar) por mes.
     Si viene con datos, llena el mes entero con 1 llamada (barato).
  2. Para lo que falte, consulta ofertas solo-ida dia por dia en modo
     refresco rotativo: actualiza las celdas mas antiguas primero, con un
     tope de llamadas por corrida (MAX_DAY_CALLS). La matriz completa se
     refresca cada ~4-5 horas.
  3. Publica la matriz completa en LATAM_STATUS.md + Job Summary, con la
     antiguedad de cada dato.
  4. Alerta por correo si min(ida) + min(vuelta) de un par baja del umbral,
     o si un tramo suelto baja de PER_LEG_ALERT.

Correo OPCIONAL (mismos secrets de Gmail del monitor LEVEL).
"""

import json
import os
import random
import smtplib
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

# ============================ CONFIG ==================================

# Pares de ciudades (se monitorean ambas direcciones de cada par)
PAIRS = [
    {"name": "BCN-SCL", "a": "BCN", "b": "SCL", "max_rt": 1000},
    {"name": "MAD-SCL", "a": "MAD", "b": "SCL", "max_rt": 1000},
]

# Meses a monitorear (formato "YYYY-MM"). Edita a gusto.
MONTHS = ["2026-12", "2027-01", "2027-02", "2027-03"]

PER_LEG_ALERT = 450       # avisa si un tramo suelto (OW) <= este valor EUR
MAX_DAY_CALLS = 36        # tope de consultas dia-a-dia por corrida
STALE_HOURS = 6           # una celda mas vieja que esto se considera vencida
RETRIES = 2

# ----------------------------------------------------------------------

CAL_URL = ("https://www.latamairlines.com/bff/web-products-searchbox/"
           "v1/calendar")
SEARCH_URL = "https://www.latamairlines.com/bff/air-offers/v2/offers/search"
STATE_FILE = Path("latam_state.json")
STATUS_FILE = Path("LATAM_STATUS.md")

UAS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
]


def latam_headers():
    return {
        "User-Agent": random.choice(UAS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": "https://www.latamairlines.com/es/es",
        "x-latam-application-country": "es",
        "x-latam-application-lang": "es",
        "x-latam-application-oc": "es",
        "x-latam-application-name": "web-air-offers",
        "x-latam-client-name": "web-air-offers",
        "x-latam-request-id": str(uuid.uuid4()),
        "x-latam-track-id": str(uuid.uuid4()),
        "x-latam-app-session-id": str(uuid.uuid4()),
    }


# ============================ HELPERS =================================


def looks_blocked(text):
    t = (text or "")[:400].lower()
    return "<!doctype html" in t or "<html" in t or "access denied" in t


def get_json(session, url, params, label):
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, headers=latam_headers(),
                            timeout=30)
        except requests.RequestException as exc:
            print(f"[WARN] red {label} {attempt}/{RETRIES}: {exc}")
            time.sleep(1.5 * attempt)
            continue
        if r.status_code == 200 and not looks_blocked(r.text):
            try:
                return r.json()
            except ValueError:
                print(f"[WARN] no-JSON {label}")
        else:
            print(f"[WARN] HTTP {r.status_code}"
                  f"{' bloqueado' if looks_blocked(r.text) else ''} {label}")
        time.sleep(1.5 * attempt + random.uniform(0, 1))
    return None


def days_of_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    d = date(y, m, 1)
    out = []
    while d.month == m:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def try_bulk_calendar(session, origin, dest, ym):
    """Intenta el calendario bulk. Devuelve {fecha: precio} o {}."""
    y, m = ym[:4], ym[5:7]
    data = get_json(session, CAL_URL,
                    {"origin": origin, "destination": dest,
                     "month": m, "year": y, "isRoundTrip": "false"},
                    f"cal {origin}->{dest} {ym}")
    result = {}
    if not data:
        return result

    def walk(obj):
        # busca objetos con fecha + monto, estructura defensiva
        if isinstance(obj, dict):
            dt = None
            amt = None
            for k, v in obj.items():
                lk = k.lower()
                if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-" \
                        and ("date" in lk or "day" in lk):
                    dt = v[:10]
                if isinstance(v, (int, float)) and v > 0 and \
                        ("price" in lk or "amount" in lk or
                         "minimum" in lk or "value" in lk):
                    amt = float(v)
            if dt and amt and dt[:7] == ym:
                if dt not in result or amt < result[dt]:
                    result[dt] = round(amt, 2)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return result


def fetch_day_min(session, origin, dest, day):
    """Minimo del dia via ofertas solo-ida. Devuelve precio o None/'nf'."""
    params = {"origin": origin, "destination": dest, "outFrom": day,
              "adult": 1, "cabinType": "Economy", "sort": "PRICE,asc",
              "redemption": "false"}
    data = get_json(session, SEARCH_URL, params,
                    f"ow {origin}->{dest} {day}")
    if data is None:
        return None
    content = data.get("content") or []
    best = None
    for o in content:
        try:
            amt = o["summary"]["lowestPrice"]["amount"]
        except (KeyError, TypeError):
            continue
        if amt and (best is None or amt < best):
            best = float(amt)
    return round(best, 2) if best is not None else "nf"   # nf = sin vuelos


def flights_link(origin, dest, out_day, in_day=None):
    base = ("https://www.latamairlines.com/es-es/flights"
            f"?origin={origin}&destination={dest}"
            f"&outbound={out_day}T00%3A00%3A00.000Z"
            "&adt=1&chd=0&inf=0&cabin=Economy")
    if in_day:
        return base + f"&inbound={in_day}T00%3A00%3A00.000Z&trip=RT"
    return base + "&trip=OW"


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


def age_str(ts_iso, now):
    try:
        ts = datetime.fromisoformat(ts_iso)
    except ValueError:
        return "?"
    mins = int((now - ts).total_seconds() // 60)
    return f"{mins}m" if mins < 120 else f"{mins // 60}h"


# ============================== MAIN ==================================


def main():
    now = datetime.now(timezone.utc)
    state = (json.loads(STATE_FILE.read_text())
             if STATE_FILE.exists() else {})
    fares = state.get("fares", {})        # {"BCN->SCL": {"YYYY-MM-DD": {p, ts}}}
    alerted = set(state.get("alerted", []))

    directions = []
    for p in PAIRS:
        directions.append((f"{p['a']}->{p['b']}", p["a"], p["b"]))
        directions.append((f"{p['b']}->{p['a']}", p["b"], p["a"]))

    s = requests.Session()
    try:
        s.get("https://www.latamairlines.com/es/es",
              headers=latam_headers(), timeout=30)
    except requests.RequestException:
        pass

    calls = 0
    blocked = 0

    # 1) Intento bulk por calendario (1 llamada por direccion-mes)
    for dkey, o, d in directions:
        fares.setdefault(dkey, {})
        for ym in MONTHS:
            month_cells = {day: c for day, c in fares[dkey].items()
                           if day[:7] == ym}
            fresh = [c for c in month_cells.values()
                     if (now - datetime.fromisoformat(c["ts"]))
                     .total_seconds() < STALE_HOURS * 3600]
            # si el mes ya esta mayormente fresco, no gastar la llamada
            if len(fresh) >= 20:
                continue
            got = try_bulk_calendar(s, o, d, ym)
            calls += 1
            if got:
                for day, price in got.items():
                    fares[dkey][day] = {"p": price, "ts": now.isoformat()}
                print(f"[OK] bulk {dkey} {ym}: {len(got)} dias")
            time.sleep(random.uniform(0.5, 1.2))

    # 2) Refresco rotativo dia-a-dia de las celdas mas viejas
    queue = []
    for dkey, o, d in directions:
        for ym in MONTHS:
            for day in days_of_month(ym):
                if day <= now.date().isoformat():
                    continue
                cell = fares[dkey].get(day)
                ts = (datetime.fromisoformat(cell["ts"])
                      if cell else datetime(2000, 1, 1, tzinfo=timezone.utc))
                queue.append((ts, dkey, o, d, day))
    queue.sort(key=lambda x: x[0])   # mas viejo primero

    for ts, dkey, o, d, day in queue[:MAX_DAY_CALLS]:
        val = fetch_day_min(s, o, d, day)
        calls += 1
        if val is None:
            blocked += 1
        else:
            fares[dkey][day] = {"p": val, "ts": now.isoformat()}
        time.sleep(random.uniform(0.6, 1.4))

    reliable = blocked <= max(2, calls // 3)

    # 3) Status: matriz por direccion y mes
    md = [f"## LATAM monitor — {now.strftime('%Y-%m-%d %H:%M UTC')}\n",
          f"Meses: {', '.join(MONTHS)} · llamadas: {calls} · "
          f"bloqueos: {blocked} · confiable: {'sí' if reliable else 'no'}\n"]
    if not reliable:
        md.append("> ⚠️ Muchos bloqueos esta corrida: no se envían correos.\n")

    new_alerts = []
    for p in PAIRS:
        ka, kb = f"{p['a']}->{p['b']}", f"{p['b']}->{p['a']}"
        md.append(f"### {p['name']} (umbral RT €{p['max_rt']})\n")
        mins = {}
        for dkey in (ka, kb):
            md.append(f"**{dkey}**\n")
            best = None
            for ym in MONTHS:
                cells = sorted((day, c) for day, c in fares[dkey].items()
                               if day[:7] == ym and c["p"] != "nf")
                if not cells:
                    md.append(f"- {ym}: _sin datos aún_")
                    continue
                mn_day, mn = min(cells, key=lambda x: x[1]["p"])
                line = f"- {ym}: min **€{mn['p']}** el {mn_day} " \
                       f"({age_str(mn['ts'], now)})"
                cheap = [f"{d[8:]}=€{c['p']}" for d, c in cells
                         if c["p"] <= mn["p"] * 1.10][:6]
                if len(cheap) > 1:
                    line += f" · baratos: {', '.join(cheap)}"
                md.append(line)
                if best is None or mn["p"] < best[1]:
                    best = (mn_day, mn["p"])
            md.append("")
            if best:
                mins[dkey] = best
            # tramos sueltos bajo PER_LEG_ALERT
            for day, c in sorted(fares[dkey].items()):
                if c["p"] != "nf" and isinstance(c["p"], (int, float)) \
                        and c["p"] <= PER_LEG_ALERT:
                    key = f"LEG|{dkey}|{day}|{int(c['p'] // 25)}"
                    new_alerts.append(
                        (key, f"TRAMO {dkey} {day}: €{c['p']}\n"
                              f"  {flights_link(dkey[:3], dkey[-3:], day)}"))

        # combo minimo del par (vuelta DEBE ser posterior a la ida)
        outs = sorted((day, c["p"]) for day, c in fares[ka].items()
                      if isinstance(c["p"], (int, float)))
        ins = sorted((day, c["p"]) for day, c in fares[kb].items()
                     if isinstance(c["p"], (int, float)))
        best_combo = None
        if outs and ins:
            # sufijo-minimo de vueltas por fecha para busqueda eficiente
            suff = []
            mn = None
            for day, pr in reversed(ins):
                if mn is None or pr < mn[1]:
                    mn = (day, pr)
                suff.append((day, mn))
            suff_by_day = dict(suff)
            ins_days = [d for d, _ in ins]
            import bisect
            for od, op in outs:
                i = bisect.bisect_right(ins_days, od)   # vuelta > ida
                if i >= len(ins_days):
                    continue
                rd, (rbd, rbp) = ins_days[i], suff_by_day[ins_days[i]]
                total = round(op + rbp, 2)
                if best_combo is None or total < best_combo[0]:
                    best_combo = (total, od, op, rbd, rbp)
        if best_combo:
            total, od, op, idy, ip = best_combo
            flag = "✅" if total < p["max_rt"] else ""
            md.append(f"**Mejor combo {p['name']}** {flag}: "
                      f"€{total} — ida {od} (€{op}) + vuelta {idy} (€{ip})  \n"
                      f"[Ver en LATAM]({flights_link(p['a'], p['b'], od, idy)})\n")
            if total < p["max_rt"]:
                key = f"RT|{p['name']}|{od}|{idy}|{int(total // 50)}"
                new_alerts.append(
                    (key, f"[{p['name']}] COMBO €{total} "
                          f"(umbral €{p['max_rt']})\n"
                          f"  IDA {od}: €{op} | VUELTA {idy}: €{ip}\n"
                          f"  {flights_link(p['a'], p['b'], od, idy)}\n"
                          f"  OJO: suma de tramos solo-ida; el precio RT "
                          f"real puede variar (a veces es menor)."))

    fresh_alerts = [(k, t) for k, t in new_alerts if k not in alerted]
    if fresh_alerts and reliable:
        md.insert(2, f"> 🔥 **{len(fresh_alerts)} alerta(s) nueva(s)**\n")
    md.append(f"\n---\n_correo: {'activo' if email_on() else 'no configurado'} "
              f"· celdas en matriz: "
              f"{sum(len(v) for v in fares.values())}_")
    write_status("\n".join(md))
    print(f"[INFO] llamadas={calls} bloqueos={blocked} "
          f"alertas_nuevas={len(fresh_alerts)} confiable={reliable}")

    sent = False
    if fresh_alerts and reliable and email_on():
        body = ("ALERTAS LATAM\n\n"
                + "\n\n".join(t for _, t in fresh_alerts))
        send_email(f"LATAM: {len(fresh_alerts)} alerta(s) de vuelos", body)
        sent = True
    if sent:
        for k, _ in fresh_alerts:
            alerted.add(k)

    state.update({"fares": fares, "alerted": sorted(alerted),
                  "last_run": now.isoformat(),
                  "last_calls": calls, "last_blocked": blocked})
    STATE_FILE.write_text(json.dumps(state))

    if calls and blocked >= calls:
        print("[ERROR] todo bloqueado")
        sys.exit(1)


if __name__ == "__main__":
    main()
