import base64
import csv
import io
import json
import logging
from collections import defaultdict

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression

_logger = logging.getLogger(__name__)


def _tz_selection():
    return [(tz, tz) for tz in pytz.all_timezones]


PROMOTION_STATES = [
    ("draft", "Draft"),
    ("conflict", "Conflict"),
    ("scheduled", "Scheduled"),
    ("active", "Active"),
    ("ended", "Ended"),
    ("cancelled", "Cancelled"),
    ("archived", "Archived"),
]

OFFER_TYPES = [
    ("product_percent", "Product Percentage Discount"),
    ("product_fixed", "Product Fixed Amount Discount"),
    ("single_bundle", "Single Product Bundle Price"),
    ("buy_x_get_y", "Buy X Get Y Free"),
    ("order_percent", "Order Total Percentage Discount"),
    ("order_fixed", "Order Total Fixed Discount"),
    ("coupon", "Coupon / Code Offer"),
    ("mix_match", "Mix and Match Bundle"),
    ("multi_group", "Multi-group Assortment Bundle"),
]

# The offer types operators are allowed to build. OFFER_TYPES above stays complete so promotions
# already stored on a retired type keep reading and pricing correctly - and so the legacy
# migration wizard can still land them - but nothing new can be created outside this list.
SELECTABLE_OFFER_TYPES = [
    "product_percent",
    "product_fixed",
    "mix_match",
    "buy_x_get_y",
    "order_percent",
]

# Offers that apply to the whole basket rather than to a resolved product list. Kept in one place
# because the server (which decides whether to materialize scope rows) and the POS engine (whose
# ORDER_WIDE_TYPES mirrors this) have to agree, or a promotion validates here and does nothing there.
ORDER_WIDE_OFFER_TYPES = ("order_percent", "order_fixed", "coupon")


class DnsPromotionStoreGroup(models.Model):
    _name = "dns.promotion.store.group"
    _description = "Promotion Store Group"
    _order = "name"

    name = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    warehouse_ids = fields.Many2many(
        "stock.warehouse", relation="dns_promotion_store_group_warehouse_rel",
        column1="group_id", column2="warehouse_id", string="Warehouses"
    )
    pos_config_ids = fields.Many2many(
        "pos.config", relation="dns_promotion_store_group_pos_config_rel",
        column1="group_id", column2="config_id", string="Point of Sale Configurations"
    )
    store_count = fields.Integer(string="Tills", compute="_compute_store_count")
    warehouse_count = fields.Integer(string="Stores", compute="_compute_store_count")

    @api.depends("warehouse_ids", "pos_config_ids")
    def _compute_store_count(self):
        for record in self:
            configs = record._resolved_pos_configs()
            record.store_count = len(configs)
            record.warehouse_count = len(configs._dns_warehouses())

    def _resolved_pos_configs(self):
        self.ensure_one()
        configs = self.pos_config_ids
        if self.warehouse_ids:
            configs |= self.env["pos.config"].search([
                ("picking_type_id.warehouse_id", "in", self.warehouse_ids.ids),
            ])
        return configs

    # Store coverage is materialized into store_scope_ids when a promotion validates, and the
    # tills read nothing else. Without re-materializing here, a till added to a group never
    # receives the group's running promotions and a till taken out keeps pricing them.
    def write(self, vals):
        if not {"pos_config_ids", "warehouse_ids"} & set(vals):
            return super().write(vals)
        before = {group.id: group._resolved_pos_configs() for group in self}
        result = super().write(vals)
        changed = self.filtered(lambda group: group._resolved_pos_configs() != before[group.id])
        changed._dns_live_promotions()._dns_revalidate_store_coverage()
        return result

    def unlink(self):
        promotions = self._dns_live_promotions()
        result = super().unlink()
        promotions.invalidate_recordset(["store_group_ids"])
        promotions._dns_revalidate_store_coverage()
        return result

    def _dns_live_promotions(self):
        if not self:
            return self.env["dns.pos.promotion"]
        return self.env["dns.pos.promotion"].search([
            ("store_group_ids", "in", self.ids), ("state", "in", ["active", "scheduled"]),
        ])


class DnsPromotionStoreScope(models.Model):
    _name = "dns.promotion.store.scope"
    _description = "Resolved store assignment for a promotion (materialized at validation)"

    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    config_id = fields.Many2one("pos.config", required=True, index=True)

    _sql_constraints = [
        ("dns_store_scope_uniq", "unique(promotion_id, config_id)", "Store scope entries must be unique."),
    ]


class DnsPromotionSelector(models.Model):
    _name = "dns.promotion.selector"
    _description = "Promotion Product Selector"
    _order = "sequence, id"

    name = fields.Char(required=True, default=lambda self: _("Eligible Products"))
    sequence = fields.Integer(default=10)
    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    group_id = fields.Many2one("dns.promotion.assortment.group", ondelete="cascade", index=True)
    all_products = fields.Boolean()
    # Marks the single selector that ad-hoc "add this product too" edits append to. Flagged rather
    # than matched by name: the name is translatable and operators can rename it.
    is_manual = fields.Boolean(copy=False, help="Holds products added one at a time from the workspace.")
    product_ids = fields.Many2many(
        "product.product", relation="dns_promotion_selector_product_rel",
        column1="selector_id", column2="product_id", string="Products"
    )
    category_ids = fields.Many2many(
        "product.category", relation="dns_promotion_selector_category_rel",
        column1="selector_id", column2="category_id", string="Product Categories"
    )
    tag_ids = fields.Many2many(
        "product.tag", relation="dns_promotion_selector_tag_rel",
        column1="selector_id", column2="tag_id", string="Tags"
    )
    season_ids = fields.Many2many(
        "products.seasons", relation="dns_promotion_selector_season_rel",
        column1="selector_id", column2="season_id", string="Seasons"
    )
    size_ids = fields.Many2many(
        "products.subcategory", relation="dns_promotion_selector_size_rel",
        column1="selector_id", column2="size_id", string="Sizes"
    )
    colour = fields.Char()
    vendor_ids = fields.Many2many(
        "res.partner", relation="dns_promotion_selector_vendor_rel",
        column1="selector_id", column2="vendor_id", string="Vendors"
    )
    barcode_terms = fields.Text(help="One primary or alternate barcode per line. Commas are also accepted.")
    resolved_count = fields.Integer(compute="_compute_resolved_count")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("group_id") and not vals.get("promotion_id"):
                vals["promotion_id"] = self.env["dns.promotion.assortment.group"].browse(vals["group_id"]).promotion_id.id
        records = super().create(vals_list)
        records.mapped("promotion_id")._selector_configuration_changed()
        return records

    def write(self, vals):
        promotions = self.mapped("promotion_id")
        result = super().write(vals)
        (promotions | self.mapped("promotion_id"))._selector_configuration_changed()
        return result

    def unlink(self):
        promotions = self.mapped("promotion_id")
        result = super().unlink()
        promotions._selector_configuration_changed()
        return result

    @api.depends("promotion_id.scope_ids", "group_id")
    def _compute_resolved_count(self):
        for selector in self:
            selector.resolved_count = self.env["dns.promotion.product.scope"].search_count([
                ("promotion_id", "=", selector.promotion_id.id),
                ("selector_id", "=", selector.id),
            ])

    def _barcode_values(self):
        self.ensure_one()
        raw = (self.barcode_terms or "").replace(",", "\n")
        return list(dict.fromkeys(value.strip() for value in raw.splitlines() if value.strip()))

    def _has_criteria(self):
        """True when this selector actually narrows anything.

        The builder attaches a selector to every promotion it creates, so leaving the product
        step untouched produces an empty one. An empty selector resolves nothing *and* makes the
        promotion look product-scoped, which is why an order-wide offer built in the workspace
        could never validate: _materialize_scope saw a selector, skipped the all-products flag,
        then failed for having resolved no products.
        """
        self.ensure_one()
        return bool(
            self.all_products or self.product_ids or self.category_ids or self.tag_ids
            or self.season_ids or self.size_ids or (self.colour or "").strip()
            or self.vendor_ids or self._barcode_values()
        )

    def _product_domain(self):
        self.ensure_one()
        domain = [("available_in_pos", "=", True), ("active", "=", True)]
        clauses = []
        if self.product_ids:
            clauses.append([("id", "in", self.product_ids.ids)])
        if self.category_ids:
            clauses.append([("categ_id", "child_of", self.category_ids.ids)])
        if self.tag_ids:
            # product_tag_ids is stored on product.template in standard Odoo core; search goes through product_tmpl_id.
            tag_field = "product_tmpl_id.product_tag_ids" if "product_tag_ids" in self.env["product.template"]._fields else "product_tmpl_id.sh_product_tag_ids"
            clauses.append([(tag_field, "in", self.tag_ids.ids)])
        if self.season_ids:
            clauses.append([("product_tmpl_id.product_season", "in", self.season_ids.ids)])
        if self.size_ids:
            clauses.append([("product_tmpl_id.product_subcategory", "in", self.size_ids.ids)])
        if self.colour:
            clauses.append([("product_tmpl_id.colour", "=ilike", self.colour.strip())])
        if self.vendor_ids:
            if "vendor_id" in self.env["product.template"]._fields:
                clauses.append([("product_tmpl_id.vendor_id", "in", self.vendor_ids.ids)])
            else:
                clauses.append([("product_tmpl_id.seller_ids.partner_id", "in", self.vendor_ids.ids)])
        barcodes = self._barcode_values()
        if barcodes:
            if "product.multi.barcode" in self.env:
                alternate = self.env["product.multi.barcode"].search([
                    ("multi_barcode", "in", barcodes),
                ])
                alternate_products = alternate.mapped("product_id") | alternate.mapped("product_product")
                clauses.append(expression.OR([
                    [("barcode", "in", barcodes)],
                    [("id", "in", alternate_products.ids)],
                ]))
            else:
                clauses.append([("barcode", "in", barcodes)])
        if not self.all_products and not clauses:
            return expression.AND([domain, [("id", "=", 0)]])
        for clause in clauses:
            domain = expression.AND([domain, clause])
        return domain

    def resolve_products(self):
        self.ensure_one()
        return self.env["product.product"].search(self._product_domain())


class DnsPromotionAssortmentGroup(models.Model):
    _name = "dns.promotion.assortment.group"
    _description = "Promotion Assortment Group"
    _order = "sequence, id"

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    required_qty = fields.Float(required=True, default=1)
    selector_ids = fields.One2many("dns.promotion.selector", "group_id", string="Product Selectors")

    @api.constrains("required_qty")
    def _check_required_qty(self):
        if any(group.required_qty <= 0 for group in self):
            raise ValidationError(_("Assortment group quantity must be greater than zero."))

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.mapped("promotion_id")._selector_configuration_changed()
        return records

    def write(self, vals):
        promotions = self.mapped("promotion_id")
        result = super().write(vals)
        (promotions | self.mapped("promotion_id"))._selector_configuration_changed()
        return result

    def unlink(self):
        promotions = self.mapped("promotion_id")
        result = super().unlink()
        promotions._selector_configuration_changed()
        return result


class DnsPromotionProductScope(models.Model):
    _name = "dns.promotion.product.scope"
    _description = "Resolved Promotion Product"
    _order = "promotion_id, product_id"

    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    selector_id = fields.Many2one("dns.promotion.selector", ondelete="cascade", index=True)
    group_id = fields.Many2one("dns.promotion.assortment.group", ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", required=True, ondelete="cascade", index=True)
    barcode = fields.Char(related="product_id.barcode", store=True, index=True)

    _sql_constraints = [
        ("scope_unique", "unique(promotion_id, selector_id, group_id, product_id)", "Resolved product is duplicated."),
    ]

    def init(self):
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS dns_promotion_scope_promotion_product_idx
            ON dns_promotion_product_scope (promotion_id, product_id)
        """)


class DnsPromotionConflict(models.Model):
    _name = "dns.promotion.conflict"
    _description = "Promotion Conflict"
    _order = "store_id, product_id, id"

    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    conflicting_promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    product_id = fields.Many2one("product.product", required=True, index=True)
    store_id = fields.Many2one("pos.config", required=True, index=True)
    overlap_start = fields.Datetime()
    overlap_end = fields.Datetime()
    barcode = fields.Char(related="product_id.barcode")


class DnsPromotionAudit(models.Model):
    _name = "dns.promotion.audit"
    _description = "Promotion Audit Event"
    _order = "create_date desc, id desc"

    promotion_id = fields.Many2one("dns.pos.promotion", required=True, ondelete="cascade", index=True)
    event_type = fields.Selection([
        ("created", "Created"), ("validated", "Validated"), ("conflict", "Conflict"),
        ("activated", "Activated"), ("ended", "Ended"), ("cancelled", "Cancelled"),
        ("archived", "Archived"), ("migrated", "Migrated"), ("changed", "Changed"),
    ], required=True, index=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    message = fields.Text(required=True)
    payload = fields.Json()


class DnsPosPromotion(models.Model):
    _name = "dns.pos.promotion"
    _description = "Odoo POS Promotion"
    _order = "priority, id"
    _inherit = ["mail.thread", "mail.activity.mixin"]

    name = fields.Char(required=True, tracking=True, index=True)
    active = fields.Boolean(default=True)
    state = fields.Selection(PROMOTION_STATES, default="draft", required=True, tracking=True, index=True)
    priority = fields.Integer(default=100, required=True, index=True)
    offer_type = fields.Selection(OFFER_TYPES, required=True, tracking=True, index=True)
    automatic = fields.Boolean(default=True, help="Automatic offers are evaluated without cashier input.")
    coupon_code = fields.Char(index=True, copy=False)
    date_start = fields.Datetime(required=True, tracking=True, index=True)
    date_end = fields.Datetime(required=True, tracking=True, index=True)
    timezone = fields.Selection(
        selection=lambda self: _tz_selection(), default="Australia/Brisbane", required=True,
        help="Wall-clock timezone the start/end times are entered in. Stored in UTC.",
    )
    all_stores = fields.Boolean()
    store_group_ids = fields.Many2many(
        "dns.promotion.store.group", relation="dns_pos_promotion_store_group_rel",
        column1="promotion_id", column2="group_id", string="Store Groups"
    )
    pos_config_ids = fields.Many2many(
        "pos.config", relation="dns_pos_promotion_pos_config_rel",
        column1="promotion_id", column2="config_id", string="Individual Stores"
    )
    selector_ids = fields.One2many("dns.promotion.selector", "promotion_id", string="Product Selectors", copy=True)
    assortment_group_ids = fields.One2many("dns.promotion.assortment.group", "promotion_id", string="Assortment Groups", copy=True)
    # Selectors are additive AND-filters, so there is otherwise no way to drop one SKU out of a
    # category- or tag-wide selection short of enumerating the category by hand.
    excluded_product_ids = fields.Many2many(
        "product.product", relation="dns_promotion_excluded_product_rel",
        column1="promotion_id", column2="product_id", string="Excluded Products",
        help="Products removed from this promotion even when a selector resolves them.",
    )
    scope_ids = fields.One2many("dns.promotion.product.scope", "promotion_id", readonly=True)
    all_products = fields.Boolean(readonly=True, help="Offer resolves to the full POS product universe; evaluated without an ID list (no scope rows written).")
    store_scope_ids = fields.One2many("dns.promotion.store.scope", "promotion_id", readonly=True)
    conflict_ids = fields.One2many("dns.promotion.conflict", "promotion_id", readonly=True)
    audit_ids = fields.One2many("dns.promotion.audit", "promotion_id", readonly=True)
    percent_discount = fields.Float()
    fixed_discount = fields.Monetary()
    required_qty = fields.Float(default=1)
    bundle_total = fields.Monetary()
    bundle_unit_price = fields.Monetary()
    reward_product_id = fields.Many2one("product.product", domain=[("available_in_pos", "=", True)])
    reward_qty = fields.Float(default=1)
    minimum_order_amount = fields.Monetary()
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id, required=True)
    conflict_count = fields.Integer(compute="_compute_counts")
    product_count = fields.Integer(compute="_compute_counts")
    store_count = fields.Integer(string="Tills", compute="_compute_counts")
    # A store runs several tills, so "65 tills" and "25 stores" describe the same coverage.
    # Reporting only the till count overstates how many shops a campaign reaches.
    warehouse_count = fields.Integer(string="Stores", compute="_compute_counts")
    version = fields.Integer(default=1, readonly=True)
    migrated_from_legacy_id = fields.Integer(copy=False, index=True)
    migration_note = fields.Text(copy=False)

    _sql_constraints = [
        ("date_order", "CHECK(date_end > date_start)", "End date must be later than start date."),
        ("coupon_code_unique", "unique(coupon_code)", "Coupon code must be unique."),
    ]

    @api.model
    def _assert_coupon_code_free(self, code, exclude_id=False):
        """Reject a reused coupon code with a sentence rather than a constraint violation.

        Checked here, before the write reaches Postgres, because @api.constrains only runs after
        the INSERT - the SQL constraint would always win and the operator would see the raw
        database error. That constraint stays as the real guarantee; this is the readable path to
        the same answer.

        Deliberately spans every state, archived and cancelled included: a code printed on an old
        flyer must never resurrect against a new offer.
        """
        if not code:
            return
        domain = [("coupon_code", "=", code)]
        if exclude_id:
            domain.append(("id", "!=", exclude_id))
        clash = self.sudo().with_context(active_test=False).search(domain, limit=1)
        if clash:
            raise ValidationError(_(
                'Coupon code %(code)s is already used by "%(name)s" (%(state)s). '
                "Codes are never reused, even once a promotion ends - pick another."
            ) % {"code": code, "name": clash.name, "state": clash.state})

    @api.model_create_multi
    def create(self, vals_list):
        seen = set()
        for vals in vals_list:
            code = vals.get("coupon_code")
            if not code:
                continue
            if code in seen:
                raise ValidationError(_("Coupon code %s is used twice in the same batch.") % code)
            seen.add(code)
            self._assert_coupon_code_free(code)
        records = super().create(vals_list)
        records._dns_check_backoffice_scope()
        for record in records:
            record._audit("created", _("Promotion created."))
        return records

    def write(self, vals):
        if vals.get("coupon_code"):
            if len(self) > 1:
                raise ValidationError(_("A coupon code cannot be given to several promotions."))
            self._assert_coupon_code_free(vals["coupon_code"], exclude_id=self.id)
        # `all_products` and `store_scope_ids` are written by _materialize_scope during validation;
        # treating them as a configuration change audits every validation as an edit.
        protected = {"state", "scope_ids", "conflict_ids", "version", "all_products", "store_scope_ids"}
        configuration_changed = bool(set(vals) - protected)
        if not self.env.context.get("dns_system_write") and configuration_changed and any(record.state in ("active", "scheduled") for record in self):
            vals = dict(vals, state="draft")
        result = super().write(vals)
        self._dns_check_backoffice_scope()
        if configuration_changed:
            for record in self:
                record._audit("changed", _("Promotion configuration changed; validation is required."))
        return result

    def _selector_configuration_changed(self):
        """Invalidate materialized membership whenever eligibility changes."""
        for record in self.exists():
            record.scope_ids.unlink()
            record.conflict_ids.unlink()
            if record.state in ("active", "scheduled", "conflict"):
                record.with_context(dns_system_write=True).write({"state": "draft"})
            record._audit("changed", _("Product eligibility changed; validation is required."))

    @api.constrains("offer_type", "percent_discount", "fixed_discount", "required_qty", "bundle_total", "bundle_unit_price", "reward_qty", "coupon_code")
    def _check_rewards(self):
        for record in self:
            if record.offer_type in ("product_percent", "order_percent") and not 0 < record.percent_discount <= 100:
                raise ValidationError(_("Percentage discount must be between 0 and 100."))
            if record.offer_type in ("product_fixed", "order_fixed") and record.fixed_discount <= 0:
                raise ValidationError(_("Fixed discount must be greater than zero."))
            if record.offer_type in ("single_bundle", "mix_match", "multi_group") and record.required_qty <= 0:
                raise ValidationError(_("Required quantity must be greater than zero."))
            if record.offer_type in ("single_bundle", "mix_match", "multi_group") and record.bundle_total <= 0 and record.bundle_unit_price <= 0:
                raise ValidationError(_("A bundle total or unit price is required."))
            if record.offer_type == "buy_x_get_y" and (not record.reward_product_id or record.reward_qty <= 0):
                raise ValidationError(_("Buy X Get Y requires a reward product and positive reward quantity."))
            if record.offer_type == "coupon":
                if not record.coupon_code:
                    raise ValidationError(_("Coupon offers require a coupon code."))
                # Without this a coupon validates, goes active, and the till accepts the code -
                # then takes nothing off. The engine picks percentage over amount when both are
                # set, so exactly one of them being present is the useful configuration.
                if record.percent_discount <= 0 and record.fixed_discount <= 0:
                    raise ValidationError(_(
                        "Coupon %s needs a discount: set either a percentage or a fixed amount."
                    ) % record.coupon_code)
                if record.percent_discount and not 0 < record.percent_discount <= 100:
                    raise ValidationError(_("Percentage discount must be between 0 and 100."))

    @api.depends("conflict_ids", "scope_ids", "store_group_ids", "pos_config_ids", "all_stores")
    def _compute_counts(self):
        for record in self:
            record.conflict_count = len(record.conflict_ids)
            record.product_count = len(record.scope_ids.mapped("product_id"))
            configs = record._resolved_pos_configs()
            record.store_count = len(configs)
            record.warehouse_count = len(configs._dns_warehouses())

    def _resolved_pos_configs(self):
        self.ensure_one()
        if self.all_stores:
            return self.env["pos.config"].search([])
        configs = self.pos_config_ids
        # sudo: promotion viewers have no access to store groups, yet a group still decides
        # which of their tills a promotion covers. The tills are read back through the caller's
        # own pos.config rules, so a viewer never learns of one outside their Allowed POS.
        grouped = self.env["pos.config"]
        for group in self.sudo().store_group_ids:
            grouped |= group._resolved_pos_configs()
        if grouped:
            configs |= self.env["pos.config"].with_context(active_test=False).search([("id", "in", grouped.ids)])
        return configs

    @api.model
    def _dns_is_manager(self):
        return self.env.user.has_group("dns_pos_promotions.group_dns_promotion_manager")

    @api.model
    def _dns_is_backoffice(self):
        """Back office only: may build promotions, but just for the tills in their Allowed POS."""
        return (self.env.user.has_group("dns_pos_promotions.group_dns_promotion_backoffice")
                and not self._dns_is_manager())

    def _dns_outside_allowed_pos(self):
        """Why a back office user may not own this promotion, or "" when they may.

        Mirrors rule_dns_promotion_backoffice. sudo: pos.config rules would otherwise hide exactly
        the tills this is looking for.
        """
        self.ensure_one()
        promotion = self.sudo()
        if promotion.all_stores:
            return _("All Stores is reserved for Promotion Managers.")
        if promotion.store_group_ids:
            return _("Store groups are reserved for Promotion Managers; pick your tills individually.")
        if not promotion.pos_config_ids:
            return _("Pick at least one of your tills.")
        outside = promotion.pos_config_ids - self.env.user.pos_config_ids
        if outside:
            return _("%s is not one of your Allowed POS.") % ", ".join(outside.mapped("name"))
        return ""

    def _dns_can_edit(self):
        self.ensure_one()
        if self._dns_is_manager():
            return True
        return self._dns_is_backoffice() and not self._dns_outside_allowed_pos()

    def _dns_check_backoffice_scope(self):
        """Refuse a back office create/write that reaches beyond the user's Allowed POS.

        The record rule is only checked against the record as it was before a write, so on its
        own it would let a back office user widen their own promotion to All Stores.
        """
        if self.env.su or not self._dns_is_backoffice():
            return
        for record in self:
            problem = record._dns_outside_allowed_pos()
            if problem:
                raise AccessError(_('You can only run "%(name)s" on your own stores. %(problem)s') % {
                    "name": record.name, "problem": problem,
                })

    def _dns_revalidate_store_coverage(self):
        """Revalidate live promotions after a store group they use changed its tills."""
        for promotion in self:
            before = promotion.store_scope_ids.config_id
            promotion._audit("changed", _("A store group this promotion uses changed its tills; store coverage revalidated."))
            try:
                promotion.action_validate()
            except (UserError, ValidationError) as error:
                raise ValidationError(_(
                    'Live promotion "%(name)s" uses this store group and would no longer be valid: %(error)s'
                ) % {"name": promotion.name, "error": error.args[0]}) from error
            # Refused rather than let a store group edit quietly take a running campaign off
            # every till; raising rolls the group change back with it.
            if promotion.state == "conflict":
                clash = promotion.conflict_ids[:1]
                raise ValidationError(_(
                    'This change would put live promotion "%(name)s" in conflict with "%(other)s" '
                    "(%(product)s at %(store)s). Resolve that overlap first."
                ) % {
                    "name": promotion.name, "other": clash.conflicting_promotion_id.name,
                    "product": clash.product_id.display_name, "store": clash.store_id.name,
                })
            # action_validate refreshed the tills now in scope; the ones that dropped out also
            # have to hear about it, or they keep pricing the promotion until they reload.
            promotion._notify_pos_refresh(configs=before - promotion.store_scope_ids.config_id)

    def _audit(self, event_type, message, payload=None):
        self.ensure_one()
        self.env["dns.promotion.audit"].sudo().create({
            "promotion_id": self.id,
            "event_type": event_type,
            "message": message,
            "payload": payload or {},
        })

    def _materialize_scope(self):
        self.ensure_one()
        self.scope_ids.unlink()
        self.store_scope_ids.unlink()
        values = []
        # A selector that narrows nothing is treated as absent, not as "matches nothing".
        selectors = (self.selector_ids | self.assortment_group_ids.selector_ids).filtered(
            lambda selector: selector._has_criteria()
        )
        # R7: all-product scopes (order-wide offers without selectors, or an
        # all_products selector) are flagged instead of materializing 40K+ rows.
        all_products = False
        if self.offer_type in ORDER_WIDE_OFFER_TYPES and not selectors:
            all_products = True
        if any(selector.all_products for selector in selectors):
            all_products = True
            selectors = selectors.filtered(lambda sel: not sel.all_products)
        excluded = set(self.excluded_product_ids.ids)
        for selector in selectors:
            for product in selector.resolve_products():
                if product.id in excluded:
                    continue
                values.append({
                    "promotion_id": self.id,
                    "selector_id": selector.id,
                    "group_id": selector.group_id.id,
                    "product_id": product.id,
                })
        # A product may be selected by multiple clauses; retain its group distinction but not duplicate rows.
        deduped = list({(v["promotion_id"], v.get("selector_id"), v.get("group_id"), v["product_id"]): v for v in values}.values())
        if deduped:
            self.env["dns.promotion.product.scope"].create(deduped)
        # R4: store assignment is materialized here (validation-time) so session
        # loads use one indexed join instead of per-promotion searches.
        configs = self._resolved_pos_configs()
        if configs:
            self.env["dns.promotion.store.scope"].create(
                [{"promotion_id": self.id, "config_id": config.id} for config in configs]
            )
        self.with_context(dns_system_write=True).write({"all_products": all_products})
        if not all_products and not self.scope_ids:
            if excluded:
                raise ValidationError(_(
                    "Every product this promotion resolves to has been excluded. "
                    "Remove some exclusions or widen the product selectors."
                ))
            raise ValidationError(_("The promotion does not resolve to any active Point of Sale products."))

    # Datetimes are stored naive-UTC like every other Odoo datetime. The promotion's own
    # `timezone` - not the reader's - is authoritative for the wall clock an operator typed:
    # a campaign that starts "9am" starts at 9am in the store's zone regardless of who edits it.
    @api.model
    def _wall_to_utc(self, value, tz_name):
        """Wall-clock ``value`` in ``tz_name`` -> naive UTC datetime for storage."""
        if not value:
            return False
        naive = fields.Datetime.to_datetime(value).replace(tzinfo=None)
        # Default is_dst=False rather than None: None raises on the ambiguous hour a DST-observing
        # zone repeats every autumn, which would make the form unsubmittable twice a year.
        localised = pytz.timezone(tz_name or "UTC").localize(naive)
        return localised.astimezone(pytz.utc).replace(tzinfo=None)

    @api.model
    def _utc_to_wall(self, value, tz_name):
        """Naive UTC ``value`` -> ``YYYY-MM-DD HH:MM:SS`` wall clock in ``tz_name``."""
        if not value:
            return ""
        value = fields.Datetime.to_datetime(value)
        local = pytz.utc.localize(value.replace(tzinfo=None)).astimezone(pytz.timezone(tz_name or "UTC"))
        return local.strftime("%Y-%m-%d %H:%M:%S")

    def _dates_overlap(self, other):
        self.ensure_one()
        return self.date_start < other.date_end and other.date_start < self.date_end

    def _build_conflicts(self):
        self.ensure_one()
        self.conflict_ids.unlink()
        own_stores = set(self._resolved_pos_configs().ids)
        if not own_stores:
            raise ValidationError(_("Select at least one store, a store group, or All Stores."))
        # sudo: a back office user must still collide with promotions they cannot read, or two
        # stores' offers would stack on the same till.
        candidates = self.sudo().search([
            ("id", "!=", self.id),
            ("state", "in", ["scheduled", "active"]),
            ("date_start", "<", self.date_end),
            ("date_end", ">", self.date_start),
        ])
        own_products = set(self.scope_ids.product_id.ids)
        values = []
        for other in candidates:
            stores = own_stores.intersection(other._resolved_pos_configs().ids)
            if not stores:
                continue
            products = own_products.intersection(other.scope_ids.product_id.ids)
            for store_id in stores:
                for product_id in products:
                    values.append({
                        "promotion_id": self.id,
                        "conflicting_promotion_id": other.id,
                        "product_id": product_id,
                        "store_id": store_id,
                        "overlap_start": max(self.date_start, other.date_start),
                        "overlap_end": min(self.date_end, other.date_end),
                    })
        if values:
            self.env["dns.promotion.conflict"].create(values)
        return bool(values)

    def action_validate(self):
        # Validation writes as the caller, so this is where back office meets its record rule;
        # checked up front for one clear error instead of one from a half-rebuilt scope.
        self.check_access_rule("write")
        for record in self:
            if record.state in ("cancelled", "archived"):
                raise UserError(_("Cancelled or archived promotions cannot be validated."))
            # Transaction-level lock serializes validation/activation across workers.
            self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", (17917001,))
            record._materialize_scope()
            has_conflicts = record._build_conflicts()
            if has_conflicts:
                record.with_context(dns_system_write=True).write({"state": "conflict"})
                record._audit("conflict", _("Validation found %s product/store conflicts.") % len(record.conflict_ids))
                continue
            now = fields.Datetime.now()
            state = "active" if record.date_start <= now < record.date_end else "scheduled" if record.date_start > now else "ended"
            record.with_context(dns_system_write=True).write({"state": state, "version": record.version + 1})
            record._audit("validated", _("Promotion validated and moved to %s.") % state)
            record._notify_pos_refresh()
        return True

    def action_cancel(self):
        for record in self:
            record.with_context(dns_system_write=True).write({"state": "cancelled", "version": record.version + 1})
            record._audit("cancelled", _("Promotion cancelled."))
            record._notify_pos_refresh()

    def action_archive(self):
        for record in self:
            record.with_context(dns_system_write=True).write({"state": "archived", "active": False, "version": record.version + 1})
            record._audit("archived", _("Promotion archived."))
            record._notify_pos_refresh()

    def action_clone(self):
        self.ensure_one()
        clone = self.copy({"name": _("%s (Copy)") % self.name, "state": "draft", "active": True, "version": 1})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": clone.id, "view_mode": "form"}

    def action_view_scope(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Resolved Products"), "res_model": "dns.promotion.product.scope", "view_mode": "tree", "domain": [("promotion_id", "=", self.id)]}

    def action_view_conflicts(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Promotion Conflicts"), "res_model": "dns.promotion.conflict", "view_mode": "tree", "domain": [("promotion_id", "=", self.id)]}

    def action_import_products_csv(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Import Promotion Products"), "res_model": "dns.promotion.product.csv", "view_mode": "form", "target": "new", "context": {"default_promotion_id": self.id}}

    def action_export_products_csv(self):
        self.ensure_one()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["product_id", "name", "barcode", "promotion", "group"])
        for scope in self.scope_ids:
            writer.writerow([scope.product_id.id, scope.product_id.display_name, scope.product_id.barcode or "", self.name, scope.group_id.name or ""])
        attachment = self.env["ir.attachment"].create({
            "name": f"promotion_{self.id}_products.csv",
            "type": "binary",
            "datas": base64.b64encode(output.getvalue().encode("utf-8")),
            "mimetype": "text/csv",
            "res_model": self._name,
            "res_id": self.id,
        })
        return {"type": "ir.actions.act_url", "url": f"/web/content/{attachment.id}?download=true", "target": "self"}

    @api.model
    def bulk_assign_store_group(self, promotion_ids, store_group_id):
        promotions = self.browse(promotion_ids).exists()
        promotions.write({"store_group_ids": [(4, store_group_id)], "state": "draft"})
        return len(promotions)

    def _notify_pos_refresh(self, configs=None):
        for promotion in self:
            targets = configs if configs is not None else (
                promotion.store_scope_ids.mapped("config_id") or promotion._resolved_pos_configs()
            )
            for config in targets:
                channel = f"dns_pos_promotions:{config.id}"
                self.env["bus.bus"]._sendone(channel, "dns_pos_promotion_refresh", {
                    "promotion_id": promotion.id,
                    "version": promotion.version,
                })

    @api.model
    def cron_update_states(self):
        now = fields.Datetime.now()
        activating = self.search([("state", "=", "scheduled"), ("date_start", "<=", now), ("date_end", ">", now)])
        ending = self.search([("state", "in", ["active", "scheduled"]), ("date_end", "<=", now)])
        for record in activating:
            record.with_context(dns_system_write=True).write({"state": "active", "version": record.version + 1})
            record._audit("activated", _("Promotion activated by schedule."))
            record._notify_pos_refresh()
        for record in ending:
            record.with_context(dns_system_write=True).write({"state": "ended", "version": record.version + 1})
            record._audit("ended", _("Promotion ended by schedule."))
            record._notify_pos_refresh()

    @api.model
    def _ending_soon_domain(self):
        """Live promotions that end within the next 7 days: the dashboard's Ending Soon tile."""
        now = fields.Datetime.now()
        return [("state", "=", "active"), ("date_end", ">", now), ("date_end", "<=", fields.Datetime.add(now, days=7))]

    @api.model
    def dashboard_data(self, filters=None):
        counts = {state: self.search_count([("state", "=", state)]) for state, _label in PROMOTION_STATES}
        counts["ending_soon"] = self.search_count(self._ending_soon_domain())
        return {"counts": counts, "version": max(self.search([]).mapped("version") or [0])}

    @api.model
    def search_workspace(self, query="", state=None, store_id=None, limit=80, offset=0):
        domain = []
        # Not a real state: the Ending Soon tile, listed soonest first.
        order = "date_end asc, id" if state == "ending_soon" else None
        if state == "ending_soon":
            domain = self._ending_soon_domain()
        elif state:
            domain.append(("state", "=", state))
        if store_id:
            # sudo: viewers cannot search store groups, and the path below would make them.
            groups = self.env["dns.promotion.store.group"].sudo().search([("pos_config_ids", "in", [store_id])])
            domain = expression.AND([domain, expression.OR([
                [("all_stores", "=", True)],
                [("pos_config_ids", "in", [store_id])],
                [("store_group_ids", "in", groups.ids)],
            ])])
        if query:
            product_domain = [
                [("name", "ilike", query)], [("default_code", "ilike", query)], [("barcode", "ilike", query)],
            ]
            if "multi_barcode_ids" in self.env["product.product"]._fields:
                product_domain.extend([
                    [("multi_barcode_ids.multi_barcode", "ilike", query)],
                    [("product_tmpl_id.multi_barcode_ids.multi_barcode", "ilike", query)],
                ])
            products = self.env["product.product"].search(expression.OR(product_domain), limit=200)
            domain = expression.AND([domain, expression.OR([
                [("name", "ilike", query)], [("coupon_code", "ilike", query)],
                [("scope_ids.product_id", "in", products.ids)],
            ])])
        limit = min(max(int(limit or 25), 1), 100)
        offset = max(int(offset or 0), 0)
        records = self.search(domain, limit=limit, offset=offset, order=order)
        rows = [{
            "id": record.id,
            "name": record.name,
            "state": record.state,
            "offer_type": record.offer_type,
            "priority": record.priority,
            "date_start": record._utc_to_wall(record.date_start, record.timezone),
            "date_end": record._utc_to_wall(record.date_end, record.timezone),
            "timezone": record.timezone,
            "product_count": record.product_count,
            "store_count": record.store_count,
            "warehouse_count": record.warehouse_count,
            "conflict_count": record.conflict_count,
            "can_edit": record._dns_can_edit(),
        } for record in records]
        return {"records": rows, "total": self.search_count(domain), "limit": limit, "offset": offset}

    @api.model
    def workspace_options(self):
        """Small reference payload for the custom workspace; never ships product catalogues."""
        # Viewers see only their Allowed POS (pos.config rules) and no store groups at all.
        is_manager = self._dns_is_manager()
        stores = self.env["pos.config"].search([], order="name")
        groups = self.env["dns.promotion.store.group"].search([], order="name") if is_manager else []
        categories = self.env["product.category"].search([], order="complete_name", limit=500)
        tags = self.env["product.tag"].search([], order="name", limit=500) if "product.tag" in self.env else (
            self.env["sh.product.tag"].search([], order="sequence, name", limit=500) if "sh.product.tag" in self.env else self.env["product.category"]
        )
        vendors = self.env["res.partner"].search([("supplier_rank", ">", 0)], order="name", limit=500)
        # The user's own zone first so the common case is the default pick, then the rest of the
        # common list - a chain can run a campaign on another region's clock.
        user_tz = self.env.user.tz or "Australia/Brisbane"
        timezones = [user_tz] + [tz for tz in pytz.common_timezones if tz != user_tz]
        labels = dict(OFFER_TYPES)
        return {
            "is_manager": is_manager,
            # Managers and back office build promotions; back office only on their Allowed POS,
            # which is already all `stores` holds for them (pos.config rules).
            "can_build": is_manager or self._dns_is_backoffice(),
            # Only the buildable types are offered in the picker...
            "offer_types": [{"value": value, "label": labels[value]} for value in SELECTABLE_OFFER_TYPES],
            # ...but every label is shipped so a promotion still on a retired type is named
            # correctly in the library and detail views instead of showing its raw key.
            "offer_labels": labels,
            "timezones": timezones,
            "default_timezone": user_tz,
            # warehouse travels with each till so the builder can group its picker by store.
            # sudo on the till only for the warehouse hop - see pos.config._dns_warehouses.
            "stores": [{"id": rec.id, "name": rec.name,
                        "warehouse_id": rec.sudo().picking_type_id.warehouse_id.id or False,
                        "warehouse": rec.sudo().picking_type_id.warehouse_id.name or _("Unassigned")}
                       for rec in stores],
            "store_groups": [{"id": rec.id, "name": rec.name, "count": rec.store_count,
                              "warehouse_count": rec.warehouse_count} for rec in groups],
            "categories": [{"id": rec.id, "name": rec.complete_name} for rec in categories],
            "tags": [{"id": rec.id, "name": rec.name} for rec in tags],
            "vendors": [{"id": rec.id, "name": rec.name} for rec in vendors],
        }

    @api.model
    def preview_workspace_selector(self, selector):
        """Resolve a builder selector without creating persistent rows."""
        selector = selector or {}
        codes = selector.get("barcode_terms") or ""
        values = [value.strip() for value in codes.replace(",", "\n").splitlines() if value.strip()]
        if len(values) > 10000:
            raise ValidationError(_("A single paste is limited to 10,000 SKU/barcode values."))
        transient = self.env["dns.promotion.selector"].new({
            "all_products": bool(selector.get("all_products")),
            "barcode_terms": "\n".join(values),
            "category_ids": [(6, 0, selector.get("category_ids") or [])],
            "tag_ids": [(6, 0, selector.get("tag_ids") or [])],
            "vendor_ids": [(6, 0, selector.get("vendor_ids") or [])],
            "product_ids": [(6, 0, selector.get("product_ids") or [])],
        })
        products = self.env["product.product"].search(transient._product_domain(), limit=51)
        count = self.env["product.product"].search_count(transient._product_domain())
        found_codes = set(products.mapped("barcode"))
        alternate = self.env["product.multi.barcode"].search([("multi_barcode", "in", values)]).mapped("multi_barcode") if (values and "product.multi.barcode" in self.env) else []
        found_codes.update(alternate)
        return {
            "count": count,
            "sample": [{"id": p.id, "name": p.display_name, "sku": p.default_code or "", "barcode": p.barcode or ""} for p in products[:50]],
            "unmatched": [value for value in values if value not in found_codes][:100],
            "truncated": count > 50,
        }

    @api.model
    def _workspace_vals(self, payload, promotion=None):
        """Map a builder payload onto promotion field values. Shared by create and update."""
        required = ["name", "offer_type", "date_start", "date_end"]
        missing = [field for field in required if not payload.get(field)]
        if missing:
            raise ValidationError(_("Complete the required fields: %s") % ", ".join(missing))
        # A promotion already on a retired type may keep it - editing its dates or products must
        # not force a repricing - but it can never be moved onto another retired type.
        allowed = set(SELECTABLE_OFFER_TYPES)
        if promotion:
            allowed.add(promotion.offer_type)
        if payload["offer_type"] not in allowed:
            raise ValidationError(_("%s is no longer an available offer type.") % (
                dict(OFFER_TYPES).get(payload["offer_type"]) or payload["offer_type"]
            ))
        timezone = payload.get("timezone") or "Australia/Brisbane"
        return {
            "name": payload["name"].strip(),
            "offer_type": payload["offer_type"],
            "priority": int(payload.get("priority") or 100),
            "automatic": bool(payload.get("automatic", True)),
            "coupon_code": payload.get("coupon_code") or False,
            # The builder sends the wall clock the operator typed; the promotion's timezone
            # decides what instant that is.
            "date_start": self._wall_to_utc(payload["date_start"], timezone),
            "date_end": self._wall_to_utc(payload["date_end"], timezone),
            "timezone": timezone,
            "all_stores": bool(payload.get("all_stores")),
            "store_group_ids": [(6, 0, payload.get("store_group_ids") or [])],
            "pos_config_ids": [(6, 0, payload.get("pos_config_ids") or [])],
            "percent_discount": float(payload.get("percent_discount") or 0),
            "fixed_discount": float(payload.get("fixed_discount") or 0),
            "required_qty": float(payload.get("required_qty") or 1),
            "bundle_total": float(payload.get("bundle_total") or 0),
            "bundle_unit_price": float(payload.get("bundle_unit_price") or 0),
            "reward_product_id": payload.get("reward_product_id") or False,
            "reward_qty": float(payload.get("reward_qty") or 1),
            "minimum_order_amount": float(payload.get("minimum_order_amount") or 0),
        }

    @api.model
    def _workspace_selector_has_criteria(self, selector):
        """Whether a builder payload's product step narrows anything at all."""
        selector = selector or {}
        return bool(
            selector.get("all_products")
            or (selector.get("barcode_terms") or "").strip()
            or selector.get("product_ids") or selector.get("category_ids")
            or selector.get("tag_ids") or selector.get("vendor_ids")
        )

    @api.model
    def _workspace_selector_vals(self, selector):
        selector = selector or {}
        vals = {
            "name": selector.get("name") or _("Builder product selection"),
            "all_products": bool(selector.get("all_products")),
            "barcode_terms": selector.get("barcode_terms") or False,
            "category_ids": [(6, 0, selector.get("category_ids") or [])],
            "tag_ids": [(6, 0, selector.get("tag_ids") or [])],
            "vendor_ids": [(6, 0, selector.get("vendor_ids") or [])],
        }
        # Only touched when the caller says so: the builder cannot express an explicit product
        # list, and an omitted key must not wipe one set from the backend form or a CSV import.
        if "product_ids" in selector:
            vals["product_ids"] = [(6, 0, selector.get("product_ids") or [])]
        return vals

    @api.model
    def reward_product_lookup(self, query, limit=20):
        """Find a single POS product to give away, for the Buy X Get Y reward picker.

        Deliberately not product_promotion_lookup: that one walks every promotion each product
        belongs to, which is wasted work when all the builder needs is a name to pin to a field.
        """
        term = (query or "").strip()
        if not term:
            return []
        search_domain = [
            [("name", "ilike", term)],
            [("default_code", "ilike", term)],
            [("barcode", "ilike", term)],
        ]
        if "multi_barcode_ids" in self.env["product.product"]._fields:
            search_domain.append([("multi_barcode_ids.multi_barcode", "ilike", term)])
        products = self.env["product.product"].search(expression.AND([
            [("available_in_pos", "=", True)],
            expression.OR(search_domain),
        ]), limit=min(int(limit or 20), 50))
        return [{
            "id": product.id, "name": product.display_name,
            "sku": product.default_code or "", "barcode": product.barcode or "",
            "price": product.lst_price,
        } for product in products]

    @api.model
    def create_from_workspace(self, payload):
        """Create one draft promotion atomically from the high-volume builder."""
        payload = payload or {}
        vals = self._workspace_vals(payload)
        # No selector at all when the product step was left empty, rather than an empty one:
        # an order-wide offer is defined by having no product criteria.
        selector = payload.get("selector") or {}
        if self._workspace_selector_has_criteria(selector):
            vals["selector_ids"] = [(0, 0, self._workspace_selector_vals(selector))]
        promotion = self.create(vals)
        return {"id": promotion.id, "name": promotion.name, "state": promotion.state}

    @api.model
    def update_from_workspace(self, promotion_id, payload, validate=False):
        """Edit an existing promotion in place.

        Any configuration change drops the promotion back to draft (see `write`), which takes a
        live offer off the tills until someone revalidates. `validate=True` closes that window in
        the same request, so changing one product on a running campaign is a single action rather
        than the archive-and-rebuild it used to be.
        """
        promotion = self._workspace_promotion(promotion_id, mode="write")
        if promotion.state == "archived":
            raise UserError(_("Archived promotions cannot be edited. Clone it instead."))
        payload = payload or {}
        promotion.write(self._workspace_vals(payload, promotion=promotion))

        # Update the builder selector in place instead of unlink+recreate: one write means
        # _selector_configuration_changed runs once and the selector keeps its id, so anything
        # pointing at it (resolved-product rows, the selector browser) stays coherent.
        #
        # Skipped entirely for assortment-group offers: the flat builder selector cannot express
        # groups, and writing one would put products in scope with no group, silently breaking
        # the multi-group arithmetic at the till.
        if payload.get("selector") is not None and not promotion.assortment_group_ids:
            builder = promotion._builder_selector()
            vals = self._workspace_selector_vals(payload["selector"])
            if builder:
                builder.write(vals)
            else:
                promotion.write({"selector_ids": [(0, 0, vals)]})

        if validate:
            promotion.action_validate()
        return {
            "id": promotion.id, "name": promotion.name, "state": promotion.state,
            "conflict_count": promotion.conflict_count,
        }

    def _builder_selector(self):
        """The single selector the step-3 product builder owns, if any.

        Assortment-group selectors carry `promotion_id` as well as `group_id` (see
        DnsPromotionSelector.create), so they show up in `selector_ids` and have to be filtered
        out explicitly - overwriting one from the flat builder would silently regroup products.
        """
        self.ensure_one()
        return self.selector_ids.filtered(lambda sel: not sel.is_manual and not sel.group_id)[:1]

    @api.model
    def _workspace_promotion(self, promotion_id, mode="read"):
        promotion = self.browse(int(promotion_id)).exists()
        if not promotion:
            raise UserError(_("Promotion no longer exists."))
        # exists() bypasses record rules; without this a viewer could page through the products
        # of a promotion outside their Allowed POS just by guessing its id.
        promotion.check_access_rule("read")
        if mode == "write":
            # Up front, before any selector is touched: back office can read network-wide
            # promotions on their tills but edit only the ones confined to them.
            promotion.check_access_rights("write")
            promotion.check_access_rule("write")
        return promotion

    @api.model
    def _resolve_product_terms(self, terms):
        """Resolve pasted SKU/barcode lines to products, matching alternate barcodes too.

        Mirrors the CSV importer in wizards/product_csv.py so a paste and an upload agree on
        what a code means.
        """
        raw = terms if isinstance(terms, str) else "\n".join(terms or [])
        values = list(dict.fromkeys(
            value.strip() for value in raw.replace(",", "\n").splitlines() if value.strip()
        ))
        if not values:
            return self.env["product.product"], []
        if len(values) > 10000:
            raise ValidationError(_("A single paste is limited to 10,000 SKU/barcode values."))
        products = self.env["product.product"].search([
            ("available_in_pos", "=", True), ("active", "=", True),
            "|", ("barcode", "in", values), ("default_code", "in", values),
        ])
        if "product.multi.barcode" in self.env:
            alternate = self.env["product.multi.barcode"].search([("multi_barcode", "in", values)])
            products |= (alternate.mapped("product_id") | alternate.mapped("product_product")).filtered(
                lambda product: product.available_in_pos and product.active
            )
            found = set(products.mapped("barcode")) | set(products.mapped("default_code")) | set(alternate.mapped("multi_barcode"))
        else:
            found = set(products.mapped("barcode")) | set(products.mapped("default_code"))
        return products, [value for value in values if value not in found]

    @api.model
    def add_promotion_products(self, promotion_id, terms, validate=False):
        """Append individual products to a promotion without rebuilding its selectors."""
        promotion = self._workspace_promotion(promotion_id, mode="write")
        if promotion.state == "archived":
            raise UserError(_("Archived promotions cannot be edited. Clone it instead."))
        products, unmatched = self._resolve_product_terms(terms)
        if not products:
            raise UserError(_("None of those codes matched a Point of Sale product."))
        manual = promotion.selector_ids.filtered("is_manual")[:1]
        if not manual:
            manual = self.env["dns.promotion.selector"].create({
                "promotion_id": promotion.id,
                "name": _("Manual additions"),
                "is_manual": True,
            })
        added = products - manual.product_ids
        manual.write({"product_ids": [(4, product.id) for product in products]})
        # A product being added back is no longer excluded, whichever way it was removed before.
        if promotion.excluded_product_ids & products:
            promotion.write({"excluded_product_ids": [(3, product.id) for product in products]})
        promotion._audit("changed", _("%s product(s) added from the workspace.") % len(added))
        if validate:
            promotion.action_validate()
        return {
            "added": len(added), "unmatched": unmatched[:100],
            "state": promotion.state, "product_count": promotion.product_count,
        }

    @api.model
    def remove_promotion_products(self, promotion_id, product_ids, validate=False):
        """Exclude individual products from a promotion.

        Selectors only ever add, so dropping one SKU out of a category-wide selection has to be
        recorded as an explicit exclusion rather than by editing the selector.
        """
        promotion = self._workspace_promotion(promotion_id, mode="write")
        if promotion.state == "archived":
            raise UserError(_("Archived promotions cannot be edited. Clone it instead."))
        product_ids = [int(value) for value in (product_ids or [])]
        if not product_ids:
            raise UserError(_("Select at least one product to remove."))
        vals = {"excluded_product_ids": [(4, product_id) for product_id in product_ids]}
        manual = promotion.selector_ids.filtered("is_manual")[:1]
        if manual:
            manual.write({"product_ids": [(3, product_id) for product_id in product_ids]})
        promotion.write(vals)
        promotion._audit("changed", _("%s product(s) excluded from the workspace.") % len(product_ids))
        if validate:
            promotion.action_validate()
        return {
            "removed": len(product_ids), "state": promotion.state,
            "product_count": promotion.product_count,
        }

    @api.model
    def conflict_queue_data(self, limit=50, offset=0):
        domain = [("promotion_id.state", "=", "conflict")]
        conflicts = self.env["dns.promotion.conflict"].search(domain, limit=min(int(limit), 100), offset=max(int(offset), 0))
        return {"total": self.env["dns.promotion.conflict"].search_count(domain), "records": [{
            "id": conflict.id, "promotion_id": conflict.promotion_id.id,
            "promotion": conflict.promotion_id.name, "existing": conflict.conflicting_promotion_id.name,
            "product": conflict.product_id.display_name, "barcode": conflict.barcode or "",
            "store": conflict.store_id.name,
            "start": self._utc_to_wall(conflict.overlap_start, conflict.promotion_id.timezone),
            "end": self._utc_to_wall(conflict.overlap_end, conflict.promotion_id.timezone),
            "timezone": conflict.promotion_id.timezone,
        } for conflict in conflicts]}

    def _workspace_store_tills(self):
        self.ensure_one()
        stores = defaultdict(list)
        for config in self._resolved_pos_configs().sorted("name"):
            # sudo only for the warehouse hop - see pos.config._dns_warehouses.
            stores[config.sudo().picking_type_id.warehouse_id.name or _("Unassigned")].append(config.name)
        return [{"store": store, "tills": tills} for store, tills in sorted(stores.items())]

    @api.model
    def _workspace_product_rows(self, products):
        return [{"id": product.id, "name": product.display_name, "sku": product.default_code or "",
                 "barcode": product.barcode or ""} for product in products]

    @api.model
    def workspace_detail(self, promotion_id):
        promotion = self._workspace_promotion(promotion_id)
        tz = promotion.timezone
        builder = promotion._builder_selector()
        return {
            "id": promotion.id, "name": promotion.name, "state": promotion.state,
            "can_edit": promotion._dns_can_edit(),
            "offer_type": promotion.offer_type, "offer_label": dict(OFFER_TYPES).get(promotion.offer_type),
            "priority": promotion.priority, "automatic": promotion.automatic,
            "coupon_code": promotion.coupon_code or "",
            "date_start": promotion._utc_to_wall(promotion.date_start, tz),
            "date_end": promotion._utc_to_wall(promotion.date_end, tz),
            "timezone": tz,
            "percent_discount": promotion.percent_discount, "fixed_discount": promotion.fixed_discount,
            "required_qty": promotion.required_qty, "bundle_total": promotion.bundle_total,
            "bundle_unit_price": promotion.bundle_unit_price,
            "reward_product": promotion.reward_product_id.display_name or "",
            "reward_qty": promotion.reward_qty,
            "minimum_order_amount": promotion.minimum_order_amount,
            "all_stores": promotion.all_stores,
            "all_products": promotion.all_products,
            # Tills grouped under their store, the way the builder picker shows them. Read through
            # the caller's own pos.config rules, so a viewer only sees their Allowed POS.
            "store_tills": promotion._workspace_store_tills(),
            "excluded_products": self._workspace_product_rows(promotion.excluded_product_ids[:500]),
            "product_count": promotion.product_count, "store_count": promotion.store_count,
            "warehouse_count": promotion.warehouse_count,
            "warehouses": promotion._resolved_pos_configs()._dns_warehouses().mapped("name"),
            "conflict_count": promotion.conflict_count, "version": promotion.version,
            "excluded_count": len(promotion.excluded_product_ids),
            "stores": promotion._resolved_pos_configs().mapped("name"),
            "store_groups": promotion.store_group_ids.mapped("name") if self._dns_is_manager() else [],
            "selectors": [{"id": row.id, "name": row.name, "resolved_count": row.resolved_count,
                           "all_products": row.all_products, "barcode_count": len(row._barcode_values()),
                           "is_manual": row.is_manual,
                           "product_list_count": len(row.product_ids),
                           "categories": row.category_ids.mapped("complete_name"),
                           "tags": row.tag_ids.mapped("name"),
                           "vendors": row.vendor_ids.mapped("name"),
                           "group": row.group_id.name or ""} for row in promotion.selector_ids],
            "assortment_groups": [{"id": group.id, "name": group.name, "required_qty": group.required_qty}
                                  for group in promotion.assortment_group_ids],
            "audits": [{"id": row.id, "event": row.event_type, "message": row.message,
                        "user": row.user_id.name, "date": row.create_date} for row in promotion.audit_ids[:100]],
            # Everything the builder needs to reopen this promotion for editing. Raw ids, not
            # display names: the builder pickers are keyed by id.
            "editable": {
                "id": promotion.id,
                "name": promotion.name,
                "offer_type": promotion.offer_type,
                "priority": promotion.priority,
                "automatic": promotion.automatic,
                "coupon_code": promotion.coupon_code or "",
                "date_start": promotion._utc_to_wall(promotion.date_start, tz),
                "date_end": promotion._utc_to_wall(promotion.date_end, tz),
                "timezone": tz,
                "all_stores": promotion.all_stores,
                "store_group_ids": promotion.store_group_ids.ids,
                "pos_config_ids": promotion.pos_config_ids.ids,
                "percent_discount": promotion.percent_discount,
                "fixed_discount": promotion.fixed_discount,
                "required_qty": promotion.required_qty,
                "bundle_total": promotion.bundle_total,
                "bundle_unit_price": promotion.bundle_unit_price,
                "reward_product_id": promotion.reward_product_id.id or False,
                "reward_product_name": promotion.reward_product_id.display_name or "",
                "reward_qty": promotion.reward_qty,
                "minimum_order_amount": promotion.minimum_order_amount,
                "selector": {
                    "all_products": builder.all_products,
                    "barcode_terms": builder.barcode_terms or "",
                    "category_ids": builder.category_ids.ids,
                    "tag_ids": builder.tag_ids.ids,
                    "vendor_ids": builder.vendor_ids.ids,
                    # Migrated and CSV-imported promotions keep their products as an explicit
                    # list, not as pasted codes. Without it the builder reopened them with an
                    # empty product step and refused to save.
                    "product_ids": builder.product_ids.ids,
                    "products": self._workspace_product_rows(builder.product_ids),
                },
                # Added one at a time from the product panel. Saved on their own selector, which
                # the builder never rewrites, so they are shown but not edited here.
                "manual_products": self._workspace_product_rows(
                    promotion.selector_ids.filtered("is_manual").product_ids
                ),
            },
        }

    @api.model
    def promotion_products_workspace(self, promotion_id, query="", limit=50, offset=0):
        """Resolved products for one promotion, paginated.

        A promotion can resolve to thousands of SKUs, so this is never returned inline with
        workspace_detail; the workspace asks for a page at a time when the operator opens the
        product list.
        """
        promotion = self._workspace_promotion(promotion_id)
        domain = [("promotion_id", "=", promotion.id)]
        term = (query or "").strip()
        if term:
            search_domain = [
                [("name", "ilike", term)],
                [("default_code", "ilike", term)],
                [("barcode", "ilike", term)],
            ]
            if "multi_barcode_ids" in self.env["product.product"]._fields:
                search_domain.append([("multi_barcode_ids.multi_barcode", "ilike", term)])
            products = self.env["product.product"].search(expression.OR(search_domain))
            domain = expression.AND([domain, [("product_id", "in", products.ids)]])

        scope = self.env["dns.promotion.product.scope"]
        rows = scope.search(domain, limit=limit, offset=offset, order="id")
        return {
            "total": scope.search_count(domain),
            "records": [{
                "id": row.id,
                "product_id": row.product_id.id,
                "name": row.product_id.display_name,
                "sku": row.product_id.default_code or "",
                "barcode": row.barcode or row.product_id.barcode or "",
                "price": row.product_id.lst_price,
                "selector": row.selector_id.name or "",
                "group": row.group_id.name or "",
            } for row in rows],
        }

    @api.model
    def clone_from_workspace(self, promotion_id):
        promotion = self._workspace_promotion(promotion_id)
        clone = promotion.copy({"name": _("%s (Copy)") % promotion.name, "state": "draft", "active": True, "version": 1})
        return {"id": clone.id, "name": clone.name}

    @api.model
    def store_groups_workspace(self):
        groups = self.env["dns.promotion.store.group"].search([], order="name")
        return [{"id": group.id, "name": group.name, "active": group.active,
                 "store_count": group.store_count, "warehouse_count": group.warehouse_count,
                 "stores": group._resolved_pos_configs()._dns_warehouses().mapped("name"),
                 "tills": group._resolved_pos_configs().mapped("name"),
                 # The editor is keyed by till id. It used to match `stores` (warehouse names)
                 # against till names, so reopening a group ticked nothing and saving emptied it.
                 "pos_config_ids": group.pos_config_ids.ids,
                 "promotion_count": self.search_count([("store_group_ids", "in", group.ids)])} for group in groups]

    @api.model
    def save_store_group_workspace(self, payload):
        payload = payload or {}
        if not (payload.get("name") or "").strip():
            raise ValidationError(_("Store group name is required."))
        if not payload.get("pos_config_ids"):
            raise ValidationError(_("Pick at least one till for the store group."))
        values = {"name": payload["name"].strip(), "pos_config_ids": [(6, 0, payload["pos_config_ids"])]}
        group = self.env["dns.promotion.store.group"].browse(payload.get("id") or []).exists()
        revalidated = self.browse()
        if group:
            live = group._dns_live_promotions()
            before = group._resolved_pos_configs()
            group.write(values)
            if group._resolved_pos_configs() != before:
                revalidated = live
        else:
            group = self.env["dns.promotion.store.group"].create(values)
        return {
            "id": group.id, "name": group.name, "store_count": group.store_count,
            # Live promotions the write revalidated (see DnsPromotionStoreGroup.write), so the
            # operator knows the tills changed.
            "revalidated": len(revalidated),
        }

    @api.model
    def selector_groups_workspace(self, query="", limit=100):
        domain = [("promotion_id.active", "=", True)]
        if query:
            domain.append(("name", "ilike", query))
        selectors = self.env["dns.promotion.selector"].search(domain, order="write_date desc", limit=min(int(limit), 200))
        return [{"id": selector.id, "name": selector.name, "promotion_id": selector.promotion_id.id,
                 "promotion": selector.promotion_id.name, "state": selector.promotion_id.state,
                 "resolved_count": selector.resolved_count, "all_products": selector.all_products,
                 "barcode_count": len(selector._barcode_values()),
                 "categories": selector.category_ids.mapped("complete_name"),
                 "tags": selector.tag_ids.mapped("name"),
                 "vendors": selector.vendor_ids.mapped("name")} for selector in selectors]

    @api.model
    def product_promotion_lookup(self, query, limit=50):
        search_domain = [
            [("name", "ilike", query)], [("default_code", "ilike", query)], [("barcode", "ilike", query)],
        ]
        if "multi_barcode_ids" in self.env["product.product"]._fields:
            search_domain.extend([
                [("multi_barcode_ids.multi_barcode", "ilike", query)],
                [("product_tmpl_id.multi_barcode_ids.multi_barcode", "ilike", query)],
            ])
        domain = expression.OR(search_domain)
        products = self.env["product.product"].search(domain, limit=limit)
        result = []
        for product in products:
            promotions = self.search([("scope_ids.product_id", "=", product.id)])
            alternate = (product.multi_barcode_ids.mapped("multi_barcode") + product.product_tmpl_id.multi_barcode_ids.mapped("multi_barcode")) if "multi_barcode_ids" in product._fields else []
            result.append({
                "id": product.id,
                "name": product.display_name,
                "barcode": product.barcode,
                "alternate_barcodes": list(dict.fromkeys(alternate)),
                "promotions": [{"id": p.id, "name": p.name, "state": p.state, "date_start": p.date_start, "date_end": p.date_end} for p in promotions],
            })
        return result

    def pos_payload(self, config):
        self.ensure_one()
        groups = defaultdict(list)
        product_ids = []
        if not self.all_products:
            for scope in self.scope_ids:
                product_ids.append(scope.product_id.id)
                if scope.group_id:
                    groups[scope.group_id.id].append(scope.product_id.id)
        return {
            "all_products": self.all_products,
            "id": self.id,
            "name": self.name,
            "priority": self.priority,
            "offer_type": self.offer_type,
            "automatic": self.automatic,
            "coupon_code": self.coupon_code,
            "date_start": fields.Datetime.to_string(self.date_start),
            "date_end": fields.Datetime.to_string(self.date_end),
            "timezone": self.timezone,
            "version": self.version,
            "product_ids": list(dict.fromkeys(product_ids)),
            # An all_products offer writes no scope rows, so its exclusions can only be honoured
            # at the till. Scope-backed offers already had them filtered out server-side.
            "excluded_product_ids": self.excluded_product_ids.ids if self.all_products else [],
            "groups": [{"id": group.id, "name": group.name, "required_qty": group.required_qty, "product_ids": groups[group.id]} for group in self.assortment_group_ids],
            "percent_discount": self.percent_discount,
            "fixed_discount": self.fixed_discount,
            "required_qty": self.required_qty,
            "bundle_total": self.bundle_total,
            "bundle_unit_price": self.bundle_unit_price,
            "reward_product_id": self.reward_product_id.id,
            "reward_qty": self.reward_qty,
            "minimum_order_amount": self.minimum_order_amount,
        }
