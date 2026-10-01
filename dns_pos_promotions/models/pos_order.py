from odoo import api, fields, models


class PosOrderLine(models.Model):
    _inherit = "pos.order.line"

    dns_promotion_id = fields.Many2one("dns.pos.promotion", index=True, ondelete="set null")
    dns_promotion_name = fields.Char()
    dns_promotion_rule = fields.Char()
    dns_base_price = fields.Float(digits="Product Price")
    dns_discount_allocation = fields.Float(digits="Product Price")
    dns_saved_amount = fields.Float(digits="Product Price")
    dns_promotion_snapshot = fields.Json()

    def _export_for_ui(self, orderline):
        # Without this the promotion fields never travel back to the browser, so a refund or a
        # reprint loses the promotion name, the base price and the recorded saving.
        result = super()._export_for_ui(orderline)
        result.update({
            "dns_promotion_id": orderline.dns_promotion_id.id or False,
            "dns_promotion_name": orderline.dns_promotion_name or False,
            "dns_promotion_rule": orderline.dns_promotion_rule or False,
            "dns_base_price": orderline.dns_base_price or 0.0,
            "dns_discount_allocation": orderline.dns_discount_allocation or 0.0,
            "dns_saved_amount": orderline.dns_saved_amount or 0.0,
            "dns_promotion_snapshot": orderline.dns_promotion_snapshot or {},
        })
        return result


class PosOrder(models.Model):
    _inherit = "pos.order"

    @api.model
    def create_from_ui(self, orders, draft=False):
        # R5: a till may hold a stale promotion payload (cache, restore,
        # re-migration). Never let an unknown dns_promotion_id kill the whole
        # sale - null unknown references (audit fields stay on the line).
        promo_ids = {
            (line[2].get("dns_promotion_id") or 0)
            for order in orders
            for line in (order.get("data", {}).get("lines") or [])
            if isinstance(line, (list, tuple)) and len(line) > 2 and isinstance(line[2], dict)
            and line[2].get("dns_promotion_id")
        }
        if promo_ids:
            valid_ids = set(self.env["dns.pos.promotion"].sudo().search([("id", "in", list(promo_ids))]).ids)
            for order in orders:
                for line in (order.get("data", {}).get("lines") or []):
                    if isinstance(line, (list, tuple)) and len(line) > 2 and isinstance(line[2], dict):
                        pid = line[2].get("dns_promotion_id")
                        if pid and pid not in valid_ids:
                            line[2]["dns_promotion_id"] = False
        return super().create_from_ui(orders, draft=draft)

    # No _order_line_fields override: that hook lives on pos.order.line, not pos.order, so an
    # override here never runs. Core also returns a list rather than a dict, so calling .update()
    # on it would raise. The dns_* keys are persisted anyway because core's _is_field_accepted
    # passes through any key that is a real field on pos.order.line.

