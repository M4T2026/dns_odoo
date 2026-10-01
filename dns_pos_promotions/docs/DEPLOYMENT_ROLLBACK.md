# Deployment and Rollback Runbook

## Safe deployment

1. Take and verify a PostgreSQL dump and filestore backup. Restore both to the disposable Odoo 17 stack first.
2. Run `dns_promotion_infra/start.ps1`, then `dns_promotion_infra/init.ps1`.
3. Install `dns_pos_promotions`; do not uninstall `pos_promotional_discounts` yet.
4. Run the legacy migration wizard in dry-run mode, export its report, correct conflicts/ambiguities, then execute migration.
5. Disable the legacy POS asset bundle and legacy rule loader before enabling the replacement in a store. Restart Odoo and rebuild assets.
6. Pilot one store, then representative store groups, then all stores. Verify POS payload, price/tax allocation, receipts, refunds, and bus refresh.
7. Keep legacy records read-only through acceptance. After sign-off, archive them; uninstall the old module only after historical orders open and refund correctly.

Never run migration tests against the source backup. All schedules must be reviewed in Australia/Brisbane time even though database values are UTC.

## Rollback

1. Stop new promotion activation and finish or close open POS orders.
2. Disable `dns_pos_promotions` POS assets/loading and re-enable the legacy assets/loader from the same release package.
3. Restore the pre-deployment database and filestore if any migrated data must be reversed; do not attempt partial SQL deletion.
4. Restart all Odoo workers, clear asset attachments if required, and validate one POS per store group.
5. Preserve exported migration/conflict reports and application logs for diagnosis.

