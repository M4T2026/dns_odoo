"""Read-only POS payload benchmark for the disposable scale fixture."""
import json
from statistics import median
from time import perf_counter

from odoo import fields


Promotion = env["dns.pos.promotion"]
config = env["pos.config"].search([], limit=1)
domain = [("state", "in", ["active", "scheduled"]), ("date_end", ">", fields.Datetime.now())]
samples = []
payload = []
for _iteration in range(7):
    started = perf_counter()
    records = Promotion.search(domain, order="priority, id").filtered(lambda promotion: config in promotion._resolved_pos_configs())
    payload = [record.pos_payload(config) for record in records]
    samples.append((perf_counter() - started) * 1000)
encoded = json.dumps(payload, default=str).encode()
print({
    "config_id": config.id,
    "promotion_count": len(payload),
    "payload_bytes": len(encoded),
    "median_query_and_serialize_ms": round(median(samples), 2),
    "runs_ms": [round(value, 2) for value in samples],
})
