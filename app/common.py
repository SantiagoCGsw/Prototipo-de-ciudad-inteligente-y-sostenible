"""Configuración compartida, cliente MQTT y lógica pura (validación, agregación, IA ligera)."""
import logging
import os
from collections import deque
from statistics import mean, pstdev

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
DOMAINS = ("energy", "mobility", "waste")
UNITS = {"energy": "kW", "mobility": "veh/min", "waste": "%"}
RANGES = {"energy": (0, 500), "mobility": (0, 300), "waste": (0, 100)}
RAW, AGG = "city/raw", "city/agg"
STATS, INJECT = "city/edge/stats", "city/control/inject"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


def mqtt_client(name, topics=(), on_message=None):
    """Cliente MQTT con reconexión automática; se suscribe a `topics` al conectar."""
    log = logging.getLogger(name)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=name)

    def on_connect(cl, userdata, flags, reason_code, properties=None):
        log.info("conectado al broker MQTT (%s)", reason_code)
        for t in topics:
            cl.subscribe(t)

    client.on_connect = on_connect
    if on_message:
        client.on_message = on_message
    client.reconnect_delay_set(1, 10)
    client.connect_async(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    return client


def is_valid(domain, value):
    """Filtro de borde: descarta lecturas no numéricas, NaN o fuera de rango físico."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return False
    lo, hi = RANGES[domain]
    return lo <= value <= hi


def aggregate(values):
    return {"avg": round(mean(values), 2), "min": round(min(values), 2),
            "max": round(max(values), 2), "n": len(values)}


class AnomalyDetector:
    """Detecta saltos bruscos: z-score de la variación entre lecturas consecutivas."""

    def __init__(self, window=20, threshold=3.5, min_samples=8):
        self.diffs, self.last = deque(maxlen=window), None
        self.threshold, self.min_samples = threshold, min_samples

    def update(self, value):
        z = None
        if self.last is not None:
            d = value - self.last
            if len(self.diffs) >= self.min_samples:
                s = max(pstdev(self.diffs), 0.02 * abs(self.last), 1e-6)
                score = (d - mean(self.diffs)) / s
                if abs(score) > self.threshold:
                    z = score
            self.diffs.append(d)
        self.last = value
        return z


def eta_to_full(points, full=100.0, window=8, min_points=4):
    """Regresión lineal sobre (ts, nivel): segundos estimados hasta llegar a `full`."""
    pts = points[-window:]
    start = 0
    for i in range(1, len(pts)):  # ignora datos previos a una recolección (caída de nivel)
        if pts[i][1] < pts[i - 1][1] - 5:
            start = i
    pts = pts[start:]
    if len(pts) < min_points:
        return None
    mx = mean(p[0] for p in pts)
    my = mean(p[1] for p in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / den
    if slope <= 1e-6:
        return None
    return max(full - pts[-1][1], 0) / slope
