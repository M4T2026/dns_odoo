# Odoo 17 Validation Report

Environment: official `odoo:17.0` container, PostgreSQL 15, disposable database `dns_promotions_17_test`, HTTP port 17069.

Completed validation:

- Python and JavaScript syntax checks and XML parsing.
- Clean Odoo registry installation with all declared dependencies.
- Eight Odoo post-install model/transaction tests: 0 failures, 0 errors.
- Coverage includes 150 equal-priority records, ID uniqueness, primary/alternate barcode resolution, product/store/date conflicts, non-overlap, assortment requirements, and reward constraints.
- Behave RPC harness includes 160-record scale, store isolation, barcode lookup, conflict queue, and visible-browser workspace scenarios.

Production acceptance still requires execution against a disposable restored client backup, full 45-store data volumes, and signed results for payment/receipt/refund and physical POS peripherals. The repository stack intentionally does not mutate the source backup.

