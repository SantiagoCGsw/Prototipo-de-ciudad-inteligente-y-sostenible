"""Capa Edge: valida, filtra y agrega lecturas; solo envía resúmenes hacia los servicios."""
import json
import os
import threading
import time
from collections import defaultdict

from common import AGG, DOMAINS, RAW, STATS, aggregate, is_valid, mqtt_client

WINDOW = float(os.getenv("EDGE_WINDOW", "5"))
buffers, lock = defaultdict(list), threading.Lock()
stats = {"received": 0, "discarded": 0, "sent": 0}


def on_message(client, userdata, msg):
    try:
        _, _, domain, device = msg.topic.split("/")
        p = json.loads(msg.payload)
    except ValueError:
        return
    if domain not in DOMAINS:
        return
    with lock:
        stats["received"] += 1
        if is_valid(domain, p.get("value")):
            buffers[(domain, device)].append(p["value"])
        else:
            stats["discarded"] += 1


def main():
    client = mqtt_client("edge", [f"{RAW}/#"], on_message)
    while True:
        time.sleep(WINDOW)
        with lock:
            batch = {k: v for k, v in buffers.items() if v}
            buffers.clear()
            stats["sent"] += len(batch)
            snapshot = dict(stats)
        for (domain, device), values in batch.items():
            msg = {"device_id": device, "domain": domain, "ts": time.time(), **aggregate(values)}
            client.publish(f"{AGG}/{domain}/{device}", json.dumps(msg))
        snapshot["reduction_pct"] = round(100 * (1 - snapshot["sent"] / max(snapshot["received"], 1)), 1)
        snapshot["window_s"] = WINDOW
        client.publish(STATS, json.dumps(snapshot), retain=True)


if __name__ == "__main__":
    main()
