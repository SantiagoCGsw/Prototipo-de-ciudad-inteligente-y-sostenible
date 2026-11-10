"""Microservicio de dominio (DOMAIN=energy|mobility|waste): persiste, analiza y expone una API REST."""
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from statistics import mean

from fastapi import FastAPI

from common import AGG, DOMAINS, UNITS, AnomalyDetector, eta_to_full, mqtt_client

DOMAIN = os.environ["DOMAIN"]
assert DOMAIN in DOMAINS, f"DOMAIN debe ser uno de {DOMAINS}"
DB_PATH = os.getenv("DB_PATH", f"/data/{DOMAIN}.db")
COOLDOWN, HISTORY = 20, 60
lock = threading.RLock()

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS readings(device_id TEXT, ts REAL, avg REAL, min REAL, max REAL, n INT);
CREATE INDEX IF NOT EXISTS ix_readings ON readings(device_id, ts);
CREATE TABLE IF NOT EXISTS alerts(ts REAL, device_id TEXT, kind TEXT, message TEXT);
""")
latest, last_alert = {}, {}
history = defaultdict(lambda: deque(maxlen=HISTORY))
detectors = defaultdict(AnomalyDetector)
for (dev,) in db.execute("SELECT DISTINCT device_id FROM readings").fetchall():
    rows = db.execute("SELECT ts, avg FROM readings WHERE device_id=? ORDER BY ts DESC LIMIT ?", (dev, HISTORY))
    history[dev].extend(reversed(rows.fetchall()))


def alert(dev, kind, message):
    now = time.time()
    if now - last_alert.get((dev, kind), 0) < COOLDOWN:
        return
    last_alert[(dev, kind)] = now
    db.execute("INSERT INTO alerts VALUES (?,?,?,?)", (now, dev, kind, message))


def handle(p):
    dev, ts, avg = p["device_id"], p["ts"], p["avg"]
    with lock:
        latest[dev] = {k: p[k] for k in ("ts", "avg", "min", "max", "n")}
        history[dev].append((ts, avg))
        db.execute("INSERT INTO readings VALUES (?,?,?,?,?,?)", (dev, ts, avg, p["min"], p["max"], p["n"]))
        db.execute("DELETE FROM readings WHERE ts < ?", (ts - 3600,))
        if DOMAIN == "waste":
            if avg >= 80:
                alert(dev, "llenado", f"{dev} al {avg:.0f} %: programar recolección")
        else:
            z = detectors[dev].update(avg)
            if z is not None:
                alert(dev, "anomalia", f"{dev}: {avg:.1f} {UNITS[DOMAIN]} (variación atípica, z={z:+.1f})")
        db.commit()


def on_message(client, userdata, msg):
    try:
        handle(json.loads(msg.payload))
    except (ValueError, KeyError):
        pass


@asynccontextmanager
async def lifespan(app):
    client = mqtt_client(f"domain-{DOMAIN}", [f"{AGG}/{DOMAIN}/#"], on_message)
    yield
    client.loop_stop()


app = FastAPI(title=f"Servicio {DOMAIN}", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "domain": DOMAIN}


@app.get("/summary")
def summary():
    now = time.time()
    with lock:
        devices = []
        for dev, r in sorted(latest.items()):
            item = {"device_id": dev, **r, "stale": now - r["ts"] > 30}
            if DOMAIN == "waste":
                item["eta_s"] = eta_to_full(list(history[dev]))
            devices.append(item)
        recent = db.execute("SELECT COUNT(*) FROM alerts WHERE ts > ?", (now - 3600,)).fetchone()[0]
    vals = [d["avg"] for d in devices]
    if DOMAIN == "waste":
        kpis = [("Llenado medio", mean(vals) if vals else 0, "%"), ("Contenedores sobre 80 %", sum(v >= 80 for v in vals), "")]
    else:
        kpis = [("Total", sum(vals), UNITS[DOMAIN])]
    return {"domain": DOMAIN, "unit": UNITS[DOMAIN], "devices": devices, "alerts_1h": recent,
            "kpis": [{"label": l, "value": round(v, 1), "unit": u} for l, v, u in kpis]}


@app.get("/series")
def series():
    with lock:
        return {dev: [{"ts": t, "v": v} for t, v in pts] for dev, pts in history.items()}


@app.get("/alerts")
def alerts(limit: int = 20):
    with lock:
        rows = db.execute("SELECT ts, device_id, kind, message FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": t, "device_id": d, "kind": k, "message": m, "domain": DOMAIN} for t, d, k, m in rows]
