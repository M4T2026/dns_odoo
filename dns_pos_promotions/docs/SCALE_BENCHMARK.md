# Odoo 17 promotion scale benchmark

Date: 1 August 2026  
Runtime: Odoo Enterprise `17.0+e-20260801`, PostgreSQL 15

## Dataset

The test is isolated in disposable databases. It does not alter the source dump.

- 151 POS configurations
- 2,000 synthetic POS products
- 500 simultaneously active promotions on one deliberately worst-case POS
- Four unique eligible products per promotion
- Immutable database IDs with duplicate priority values

`tests/seed_scale.py` creates the fixture. It disables Linkly on copied stores so a
benchmark can never pair dummy configurations with external payment terminals.

## Results

| Database | Promotions returned | Payload | Cold query + serialization | Warm median |
|---|---:|---:|---:|---:|
| Synthetic Odoo 17 | 500 | 248,832 bytes | 150.92 ms | 53.35 ms |
| Restored live copy | 500 | 253,392 bytes | 210.80 ms | 51.88 ms |

The promotion payload is comfortably below 300 KB and warmed server processing is
about 52 ms even when a single store receives all 500 offers. Normal store-specific
loading should return substantially fewer records.

Two POS sessions were opened successfully in the restored live copy. Full browser
startup did not complete within four minutes because the standard Odoo
`pos.session/load_pos_data` request was calculating stock/product data across the
live catalogue. PostgreSQL showed active stock-move queries while the promotion-only
payload had already benchmarked at approximately 52 ms. This is not evidence of a
promotion-engine bottleneck.

## Environment limitation

The supplied `.dump.gz` is a PostgreSQL dump only. It contains attachment metadata
but not the matching Odoo filestore. Backend/POS requests therefore log missing
attachment files and first-time asset generation is unusually slow. A production
filestore copy is required for a valid end-to-end startup baseline and visual checks
of existing product images and company branding.

## Architecture decision

Keep the current store-filtered, materialized-scope architecture for v1. The measured
promotion payload and evaluation inputs do not justify a separate service or browser
database yet. Before production sign-off:

1. Restore the matching filestore and rerun full POS startup.
2. Profile the standard/custom product and stock loaders that dominate startup.
3. Set an acceptance budget for total POS load and for the promotion RPC separately.
4. If promotion payloads later exceed roughly 1–2 MB per store, add a versioned,
   precomputed store payload with ETag/delta refresh; do not load a 150-store global
   promotion catalogue into every till.
