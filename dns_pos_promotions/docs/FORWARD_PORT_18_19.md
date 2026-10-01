# Forward Port Guide: Odoo 18 and 19

Forward-port as a separate project; do not install this Odoo 17 package unchanged.

Review these boundaries in order:

1. Manifest dependencies, asset bundle names, security categories, and XML view attributes.
2. OWL imports, service contracts, client-action registry, ControlButtons extension points, and popup APIs.
3. POS data loading: session loader hooks, field serialization, order/line setup, JSON export/import, and local offline database behavior.
4. Bus channel subscription and notification payload shape.
5. ORM changes to constraints, advisory locking, company checks, and `search_read`/RPC signatures.
6. Tax/price helpers and refund creation so proportional allocation remains accounting-neutral.

Re-run every `SCN-PROMO-001` through `SCN-PROMO-017` scenario on the target version. Treat asset compilation, POS boot, offline expiry, live refresh, receipt, refund, and concurrent activation as mandatory gates rather than assuming API compatibility.

