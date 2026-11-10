"""Gateway: agrega los microservicios de dominio, sirve el dashboard y permite inyectar anomalías."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from common import DOMAINS, INJECT, STATS, mqtt_client

DEFAULT = ",".join(f"{d}=http://{d}:8000" for d in DOMAINS)
URLS = dict(item.split("=", 1) for item in os.getenv("DOMAIN_URLS", DEFAULT).split(","))
edge_stats, mqtt = {}, None


def on_message(client, userdata, msg):
    try:
        edge_stats.update(json.loads(msg.payload))
    except ValueError:
        pass


@asynccontextmanager
async def lifespan(app):
    global mqtt
    mqtt = mqtt_client("gateway", [STATS], on_message)
    yield
    mqtt.loop_stop()


app = FastAPI(title="Gateway ciudad inteligente", lifespan=lifespan)


async def fetch(client, domain, base):
    try:
        s, ser, al = await asyncio.gather(client.get(f"{base}/summary"), client.get(f"{base}/series"),
                                          client.get(f"{base}/alerts"))
        for r in (s, ser, al):
            r.raise_for_status()
        return {"ok": True, **s.json(), "series": ser.json(), "alerts": al.json()}
    except Exception as exc:  # servicio caído o sin respuesta
        return {"ok": False, "domain": domain, "error": str(exc)}


@app.get("/api/overview")
async def overview():
    async with httpx.AsyncClient(timeout=2) as client:
        results = await asyncio.gather(*(fetch(client, d, u) for d, u in URLS.items()))
    domains = {r["domain"]: r for r in results}
    alerts = sorted((a for r in results if r["ok"] for a in r["alerts"]), key=lambda a: -a["ts"])[:15]
    return {"domains": domains, "alerts": alerts, "edge": edge_stats or None}


@app.post("/api/inject")
def inject(domain: str):
    if domain not in DOMAINS:
        raise HTTPException(400, f"domain debe ser uno de {DOMAINS}")
    mqtt.publish(INJECT, json.dumps({"domain": domain}))
    return {"sent": domain}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
