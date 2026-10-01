import json

from odoo import fields, models


class PosSession(models.Model):
    _inherit = "pos.session"

    def _pos_ui_models_to_load(self):
        models_to_load = super()._pos_ui_models_to_load()
        if "dns.pos.promotion" not in models_to_load:
            models_to_load.append("dns.pos.promotion")
        return models_to_load

    def _loader_params_dns_pos_promotion(self):
        return {"search_params": {"domain": [("id", "=", 0)], "fields": ["id"]}}

    def _dns_promotion_cache_key(self):
        self.ensure_one()
        # sudo: a cashier may not be allowed to read pos.config directly (ct_pos_user_restriction).
        return f"dns_pos_promotions.payload.{self.config_id.sudo().id}"

    def _dns_applicable_promotions(self):
        """Promotions this till should price with.

        Shared by the promotion payload and the product loader below, so the two cannot drift on
        what "applies here" means.

        sudo throughout: this runs inside load_pos_data as the cashier. Promotion configuration
        is manager-only data, and pos.config itself may be restricted per user by
        ct_pos_user_restriction - but a till cannot price an order without either.
        """
        self.ensure_one()
        config = self.config_id.sudo()
        if not config.dns_promotions_enabled:
            return self.env["dns.pos.promotion"].sudo()
        now = fields.Datetime.now()
        # R4: applicable promotions via the materialized store scope - one
        # indexed read instead of per-promotion searches.
        scope_rows = self.env["dns.promotion.store.scope"].sudo().search([("config_id", "=", config.id)])
        promotion_ids = set(scope_rows.mapped("promotion_id").ids)
        candidates = self.env["dns.pos.promotion"].sudo().search([
            ("state", "in", ["active", "scheduled"]),
            ("date_end", ">", now),
        ], order="priority, id")
        return candidates.filtered(
            lambda p: p.id in promotion_ids or not p.store_scope_ids and config in p._resolved_pos_configs()
        )

    def _get_pos_ui_product_product(self, params):
        """Guarantee every promotion's giveaway product reaches the till.

        Odoo caps the POS product load at `point_of_sale.limited_product_count` (20,000 by
        default) - on this database that is roughly a quarter of the catalogue. A trigger product
        survives that: scanning an unloaded barcode makes core fetch it via find_product_by_barcode.
        A Buy X Get Y reward product is never scanned, so nothing pulls it in, and
        dnsApplyBuyXGetY's `pos.db.get_product_by_id` returns undefined and gives up without a
        word. Appending them here is what makes the offer work at all.
        """
        products = super()._get_pos_ui_product_product(params)
        rewards = self._dns_applicable_promotions().mapped("reward_product_id")
        if not rewards:
            return products
        loaded = {row["id"] for row in products}
        missing = rewards.filtered(lambda product: product.id not in loaded)
        if not missing:
            return products
        # Read and post-process with core's own machinery so the injected rows are shaped exactly
        # like loader output (categ, image_128 as a bool, currency-converted lst_price).
        loader = self._loader_params_product_product()
        extra = missing.sudo().with_context(**loader["context"]).read(loader["search_params"]["fields"])
        self._process_pos_ui_product_product(extra)
        return products + extra

    def _get_pos_ui_dns_pos_promotion(self, params):
        self.ensure_one()
        config = self.config_id.sudo()
        if not config.dns_promotions_enabled:
            return []
        applicable = self._dns_applicable_promotions()

        # R3: the rendered payload is cached per (config, fingerprint). The fingerprint lists every
        # applicable promotion with its version, not just the highest version: with promotions at
        # v5 and v3, editing the second to v4 leaves the maximum at 5, and a max-based key would
        # serve a stale payload. Membership changes move the fingerprint too.
        version = ",".join(f"{p.id}:{p.version}" for p in applicable)
        ICP = self.env["ir.config_parameter"].sudo()
        key = self._dns_promotion_cache_key()
        try:
            cached = json.loads(ICP.get_param(key) or "{}")
        except (TypeError, ValueError):
            cached = {}
        if cached.get("version") == version:
            return cached.get("payload", [])

        payload = [promotion.pos_payload(config) for promotion in applicable]
        ICP.set_param(key, json.dumps({"version": version, "payload": payload}))
        return payload

    def get_dns_promotion_payload(self):
        self.ensure_one()
        return self._get_pos_ui_dns_pos_promotion({})


class PosConfig(models.Model):
    _inherit = "pos.config"

    dns_promotions_enabled = fields.Boolean(
        string="Odoo Promotions", default=True,
        help="Evaluate Odoo promotions on this Point of Sale. Turn off to sell at list price only.",
    )
    dns_promotion_version = fields.Integer(default=0, readonly=True)

    def _dns_warehouses(self):
        """The physical stores behind these tills.

        A store runs several POS configs - AirlieBeach has three, Coolalinga four - so counting
        configs overstates how many shops a promotion actually reaches. This is the same
        config -> picking type -> warehouse hop that store groups already resolve through.
        """
        # sudo: a promotion viewer need not hold Inventory rights, and stock.picking.type is
        # unreadable without them. Only the stores behind tills the caller already sees.
        return self.sudo().mapped("picking_type_id.warehouse_id")


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # Without this the per-till switch above cannot be reached from the UI.
    dns_promotions_enabled = fields.Boolean(
        related="pos_config_id.dns_promotions_enabled", readonly=False,
    )
