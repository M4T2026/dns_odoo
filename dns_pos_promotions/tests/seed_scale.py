"""Disposable scale fixture for an Odoo shell (never run against production).

Creates a deliberately heavy single-store case: 500 simultaneously active offers,
each with four unique products, plus 151 POS configurations in total.
"""
from datetime import timedelta
from time import perf_counter

from odoo import Command, fields


PREFIX = "SCALE-"
started = perf_counter()
Promotion = env["dns.pos.promotion"].with_context(tracking_disable=True, mail_create_nolog=True)
Product = env["product.product"].with_context(tracking_disable=True)
Config = env["pos.config"].with_context(tracking_disable=True)

base_config = Config.search([], limit=1)
if not base_config:
    raise RuntimeError("Create one working POS configuration before seeding scale data.")

missing_stores = max(0, 151 - Config.search_count([]))
for number in range(missing_stores):
    # Never copy production payment-terminal credentials or pair external Linkly devices.
    base_config.copy({
        "name": f"{PREFIX}STORE-{number + 1:03d}",
        "is_linkly": False,
        "linkly_username": False,
        "linkly_password": False,
        "linkly_paircode": False,
        "linkly_secret_key": False,
    })

products = Product.search([("default_code", "like", PREFIX)], order="id")
for number in range(len(products), 2000):
    Product.create({
        "name": f"Scale Test Product {number + 1:04d}",
        "default_code": f"{PREFIX}{number + 1:05d}",
        "barcode": f"9900{number + 1:09d}",
        "list_price": 5 + (number % 50),
        "available_in_pos": True,
    })
products = Product.search([("default_code", "like", PREFIX)], order="id", limit=2000)

existing = Promotion.search_count([("name", "like", f"{PREFIX}PROMO-")])
now = fields.Datetime.now()
for number in range(existing, 500):
    batch = products[number * 4:(number + 1) * 4]
    promotion = Promotion.create({
        "name": f"{PREFIX}PROMO-{number + 1:03d}",
        "offer_type": "product_percent",
        "percent_discount": 10,
        "priority": 100,
        "date_start": now - timedelta(days=1),
        "date_end": now + timedelta(days=30),
        "pos_config_ids": [Command.set(base_config.ids)],
        "selector_ids": [Command.create({"name": "Scale products", "product_ids": [Command.set(batch.ids)]})],
    })
    # Scope is intentionally materialized directly: every batch is disjoint, so
    # conflict validation would only add benchmark setup time without coverage.
    env["dns.promotion.product.scope"].create([
        {"promotion_id": promotion.id, "selector_id": promotion.selector_ids.id, "product_id": product.id}
        for product in batch
    ])
    promotion.with_context(dns_system_write=True).write({"state": "active"})

env.cr.commit()
elapsed = perf_counter() - started
print({
    "elapsed_seconds": round(elapsed, 2),
    "stores": Config.search_count([]),
    "products": Product.search_count([("default_code", "like", PREFIX)]),
    "promotions": Promotion.search_count([("name", "like", f"{PREFIX}PROMO-")]),
    "target_pos_config_id": base_config.id,
})
