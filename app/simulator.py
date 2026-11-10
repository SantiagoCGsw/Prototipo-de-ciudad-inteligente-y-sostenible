"""Capa IoT simulada: publica lecturas de sensores de energía, movilidad y residuos por MQTT."""
import json
import math
import os
import random
import threading
import time

from common import INJECT, RAW, UNITS, mqtt_client

SPEED = float(os.getenv("SIM_SPEED", "120"))        # 1 s real = 120 s simulados (un día ≈ 12 min)
INTERVAL = float(os.getenv("SIM_INTERVAL", "1"))    # segundos entre lecturas
ANOMALY_P = float(os.getenv("ANOMALY_PROB", "0.002"))
GLITCH_P = 0.03                                     # lecturas corruptas que el Edge debe descartar
T0 = time.time()

ENERGY = {"energia-norte": 1.0, "energia-centro": 1.5, "energia-sur": 0.8}
MOBILITY = {"mov-av-principal": 1.0, "mov-centro": 1.3, "mov-terminal": 0.8}
WASTE = {"res-parque": 300, "res-mercado": 200, "res-colegio": 420, "res-hospital": 260}  # s para llenarse
level = {d: random.uniform(5, 50) for d in WASTE}
spike, lock = {}, threading.Lock()


def sim_hour():
    return (6 + (time.time() - T0) * SPEED / 3600) % 24


def bump(h, c, w):
    return math.exp(-(((h - c) / w) ** 2))


def on_message(client, userdata, msg):
    """Inyección manual de anomalías desde el dashboard (topic city/control/inject)."""
    try:
        domain = json.loads(msg.payload)["domain"]
    except (ValueError, KeyError, TypeError):
        return
    with lock:
        if domain == "waste":
            d = random.choice(list(WASTE))
            level[d] = min(100, level[d] + 35)
        elif domain in ("energy", "mobility"):
            spike[random.choice(list(ENERGY if domain == "energy" else MOBILITY))] = 10


def readings():
    h = sim_hour()
    with lock:
        for d, k in ENERGY.items():
            yield "energy", d, k * (60 + 25 * math.sin(2 * math.pi * (h - 14) / 24)), True
        for d, k in MOBILITY.items():
            yield "mobility", d, k * (15 + 45 * bump(h, 8, 1.6) + 55 * bump(h, 18, 1.8)), True
        for d, secs in WASTE.items():
            level[d] += 100 / secs * INTERVAL * random.uniform(0.5, 1.5)
            if level[d] >= 100:
                yield "waste", d, 100.0, False
                level[d] = random.uniform(0, 5)  # recolección
            else:
                yield "waste", d, level[d], False


def main():
    client = mqtt_client("simulator", [INJECT], on_message)
    while True:
        for domain, dev, value, can_spike in list(readings()):
            if can_spike:
                with lock:
                    if random.random() < ANOMALY_P:
                        spike[dev] = 10
                    if spike.get(dev, 0) > 0:
                        value *= 1.9
                        spike[dev] -= 1
                value *= random.gauss(1, 0.02)
            if random.random() < GLITCH_P:
                value = random.choice([-999, 9999])
            payload = {"device_id": dev, "domain": domain, "ts": time.time(),
                       "value": round(value, 2), "unit": UNITS[domain]}
            client.publish(f"{RAW}/{domain}/{dev}", json.dumps(payload))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
