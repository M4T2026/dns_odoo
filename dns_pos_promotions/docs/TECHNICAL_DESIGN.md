# DNS POS Promotions — Technical Design

## Architecture

`dns.pos.promotion` owns lifecycle, reward, schedule, and store assignment. `dns.promotion.selector` expresses reusable eligibility rules. Validation resolves those rules into indexed `dns.promotion.product.scope` rows. POS sessions load only Active or Scheduled records intersecting their configured store; browser maps are keyed by immutable database ID, with priority used only for deterministic ordering.

The evaluation path is:

```text
Manager configuration → transactional validation → materialized product/store scope
→ store-specific POS payload → ID-indexed browser lookup → order-line allocation/snapshot
```

Conflict detection takes a PostgreSQL advisory transaction lock before comparing Scheduled/Active records. An overlap requires intersecting time, resolved store, and resolved product. All-product and automatic order-total rules use the complete POS product universe. Stacking and overrides are intentionally rejected in v1.

`pos.order.line` persists promotion ID/name/rule, base price, allocation, saved amount, and version so payment, reload, receipt, and refund do not depend on later rule edits. `_export_for_ui` returns those fields, which is what lets a refund or a reprint keep the original pricing.

## Pricing invariants

Four rules the engine must keep. Each one exists because breaking it produced a real defect.

1. **A promotion only ever sets a line discount — never the unit price.** Reward allocation starts from the line's own `get_unit_price()`, not `product.lst_price`. Writing the unit price destroys a pricelist price or a price the cashier set by hand, and it cannot be restored when the promotion is removed.
2. **Bundles cover whole sets, and the line is split on that boundary.** The POS merges repeat scans into one line, and a line carries only one discount percentage — so "any 3 for $5" with five items would show as one line at a blended 26.7% and every unit would look discounted. `dnsSplitCoveredQuantity` moves the uncovered units to their own full-price line; `dnsConsolidateSplitLines` folds them back at the start of every recompute so the order never grows a line per keystroke.
3. **Bundle totals are allocated in whole cents.** Odoo rounds each line independently (`base = round_pr(price_unit * quantity)`), so a bundle spread over three lines drifts and "3 for $5" rings up as $4.99. `allocateTarget` splits the target by line value, rounds each share, and gives the residual cent to the largest line.
4. **Repricing is synchronous.** It runs inside `set_quantity` / `add_product` / `removeOrderline` and again in `PaymentScreen.validateOrder`. The earlier `setTimeout(…, 250)` let a cashier reach the payment screen before the discount had been applied.

Refund lines, reward lines and hand-priced lines are excluded from evaluation and are never cleared.

Promotion changes publish a store/version event through Odoo bus. Empty orders refresh immediately; non-empty orders retain their snapshot and defer refresh until the next order. Client-side start/end checks provide the offline expiry boundary.

## Important boundaries

- Odoo 17 only; no dependency on Loyalty evaluation.
- No promotional `product.pricelist.item` is created or consulted.
- Product attribute or selector changes require validation before a new scope becomes active.
- Multi-barcode support uses `product.multi.barcode` supplied by `sale_pos_multi_barcodes_app`.
- Historical legacy order fields are not removed when the old module is retired.

## Main files

- `models/promotion.py`: schema, validation, conflicts, lookup, bus versioning.
- `models/pos_session.py`: store-specific POS payload and refresh RPC.
- `models/pos_order.py`: persisted audit snapshot.
- `static/src/admin`: OWL administration workspace.
- `static/src/pos`: POS evaluation and cashier controls.
- `wizards`: legacy conversion and CSV import.

