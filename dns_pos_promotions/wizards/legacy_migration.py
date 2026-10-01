import logging

from odoo import fields, models, _

_logger = logging.getLogger(__name__)


class DnsLegacyPromotionMigration(models.TransientModel):
    _name = "dns.legacy.promotion.migration"
    _description = "Migrate Legacy POS Promotions"

    dry_run = fields.Boolean(default=True)
    migrate_inactive = fields.Boolean()
    result = fields.Text(readonly=True)

    def action_migrate(self):
        self.ensure_one()
        if "pos.promotions" not in self.env.registry.models:
            self.result = _("Legacy model pos.promotions is not installed.")
            return self._reopen()
        legacy = self.env["pos.promotions"].with_context(active_test=False).search([])
        if not self.migrate_inactive:
            legacy = legacy.filtered("active")
        lines = []
        created = self.env["dns.pos.promotion"]
        for source in legacy:
            existing = self.env["dns.pos.promotion"].search([("migrated_from_legacy_id", "=", source.id)], limit=1)
            if existing:
                lines.append(_("SKIP %s: already migrated to %s") % (source.display_name, existing.display_name))
                continue
            values, selector_commands, notes = self._map_legacy(source)
            if self.dry_run:
                lines.append(_("DRY RUN %s: %s") % (source.display_name, "; ".join(notes) or _("ready")))
                continue
            try:
                with self.env.cr.savepoint():
                    target = self.env["dns.pos.promotion"].create(values)
                    for selector_values in selector_commands:
                        selector_values["promotion_id"] = target.id
                        self.env["dns.promotion.selector"].create(selector_values)
                    target._audit("migrated", _("Migrated from legacy promotion %s.") % source.display_name, {"legacy_id": source.id, "notes": notes})
                    target.action_validate()
            except Exception as exc:  # Migration must report individual failures and continue.
                notes.append(str(exc))
                target = self.env["dns.pos.promotion"].create(dict(values, state="draft", migration_note="; ".join(notes)))
                for selector_values in selector_commands:
                    selector_values["promotion_id"] = target.id
                    self.env["dns.promotion.selector"].create(selector_values)
            created |= target
            lines.append(_("MIGRATED %s: %s") % (source.display_name, "; ".join(notes) or _("validated")))
        self.result = "\n".join(lines) or _("No legacy promotions found.")
        return self._reopen()

    def _map_legacy(self, source):
        offer_map = {
            "discount_on_products": "product_percent",
            "buy_x_get_y_qty_at_z_price": "single_bundle",
            "buy_x_products_get_y_qty_at_z_price": "mix_match",
            "buy_x_get_y": "buy_x_get_y",
            "get_x_discount_on_sale_total": "order_percent",
        }
        now = fields.Datetime.now()
        end = source.end_date or fields.Datetime.add(now, days=30)
        notes = []
        if not source.end_date:
            notes.append(_("missing end date defaulted to 30 days"))
        values = {
            "name": source.name,
            "offer_type": offer_map.get(source.offer_type, "product_percent"),
            "state": "draft",
            "priority": source.sequence or 100,
            "date_start": now,
            "date_end": end if end > now else fields.Datetime.add(now, hours=1),
            "all_stores": not bool(source.pos_ids),
            "pos_config_ids": [(6, 0, source.pos_ids.ids)],
            "percent_discount": source.discount_rate or 0,
            "required_qty": 1,
            "bundle_total": 0,
            "migrated_from_legacy_id": source.id,
            "migration_note": "; ".join(notes),
        }
        products = source.discounted_ids.mapped("product_id")
        selectors = [{"name": _("Migrated explicit products"), "product_ids": [(6, 0, products.ids)]}]
        if source.offer_type == "buy_x_get_y_qty_at_z_price" and source.buy_x_get_y_qty_at_z_price_ids:
            first = source.buy_x_get_y_qty_at_z_price_ids[0]
            products = source.buy_x_get_y_qty_at_z_price_ids.mapped("product_x_id")
            selectors = [{"name": _("Migrated bundle products"), "product_ids": [(6, 0, products.ids)]}]
            values.update(required_qty=first.y_qty, bundle_unit_price=first.z_price, bundle_total=first.total_price)
            if len(set(source.buy_x_get_y_qty_at_z_price_ids.mapped("y_qty"))) > 1:
                notes.append(_("different legacy quantities require review"))
        if source.offer_type == "buy_x_products_get_y_qty_at_z_price" and source.buy_x_products_get_y_qty_at_z_price_ids:
            first = source.buy_x_products_get_y_qty_at_z_price_ids[0]
            selectors = [{"name": _("Migrated assortment"), "product_ids": [(6, 0, first.product_x_ids.ids)]}]
            values.update(required_qty=first.y_qty, bundle_unit_price=first.z_price, bundle_total=first.total_price)
        return values, selectors, notes

    def _reopen(self):
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form", "target": "new"}
