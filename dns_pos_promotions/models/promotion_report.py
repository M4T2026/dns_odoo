from odoo import api, fields, models, tools

from .promotion import OFFER_TYPES


class DnsPromotionReport(models.Model):
    """What promotions actually did at the tills, by promotion, product and store.

    A SQL view over the promotion columns the engine writes onto pos.order.line, built the way
    point_of_sale's own pos.order.report is (`_auto = False` plus `init()`). One model carrying
    every dimension answers all three breakdowns through group-by, rather than three
    near-identical models that could drift apart.
    """

    _name = "dns.promotion.report"
    _description = "Promotion Redemption Analysis"
    _auto = False
    _rec_name = "promotion_name"
    _order = "date desc"

    # --- dimensions -------------------------------------------------------------------
    date = fields.Datetime(string="Order Date", readonly=True)
    promotion_id = fields.Many2one("dns.pos.promotion", string="Promotion", readonly=True)
    # Read from the order line, not only the promotion: dns_promotion_id is ondelete="set null",
    # so a deleted campaign leaves its redemptions with a null link but an intact name. Without
    # the fallback, historical takings would collapse into one nameless bucket.
    promotion_name = fields.Char(string="Campaign", readonly=True)
    offer_type = fields.Selection(OFFER_TYPES, string="Offer Type", readonly=True)
    rule = fields.Char(string="Applied Rule", readonly=True,
                       help="How the engine priced the line: percentage, fixed, a bundle, or a free reward.")
    product_id = fields.Many2one("product.product", string="Product", readonly=True)
    product_tmpl_id = fields.Many2one("product.template", string="Product Template", readonly=True)
    categ_id = fields.Many2one("product.category", string="Product Category", readonly=True)
    # A physical store runs several tills - AirlieBeach alone has three - so pos.config is the
    # till, not the store. The store is the warehouse behind it, which is the same definition
    # dns.promotion.store.group already resolves coverage through.
    warehouse_id = fields.Many2one("stock.warehouse", string="Store", readonly=True)
    config_id = fields.Many2one("pos.config", string="Till", readonly=True)
    session_id = fields.Many2one("pos.session", string="Session", readonly=True)
    order_id = fields.Many2one("pos.order", string="Order", readonly=True)
    company_id = fields.Many2one("res.company", string="Company", readonly=True)
    currency_id = fields.Many2one("res.currency", string="Currency", readonly=True)

    # --- measures ---------------------------------------------------------------------
    quantity = fields.Float(string="Quantity", readonly=True)
    base_amount = fields.Float(string="Before Discount", readonly=True, digits="Product Price",
                               help="What the line would have cost at its pre-promotion unit price.")
    saved_amount = fields.Float(string="Customer Saved", readonly=True, digits="Product Price")
    net_amount = fields.Float(string="Net Sold", readonly=True, digits="Product Price")
    line_count = fields.Integer(string="Lines", readonly=True)
    discount_pct = fields.Float(string="Discount %", compute="_compute_discount_pct",
                                readonly=True, digits=(5, 2))

    @api.depends("base_amount", "saved_amount")
    def _compute_discount_pct(self):
        # Computed rather than selected in the view so it stays right once a pivot aggregates
        # rows: summing per-line percentages would be meaningless.
        for row in self:
            row.discount_pct = (row.saved_amount / row.base_amount * 100) if row.base_amount else 0.0

    def _query(self):
        # No parameters and no interpolation: every value below is a literal, so this stays a
        # static string rather than something assembled from data.
        return """
            SELECT
                line.id                                              AS id,
                pos_order.date_order                                 AS date,
                line.dns_promotion_id                                AS promotion_id,
                COALESCE(promotion.name, line.dns_promotion_name)    AS promotion_name,
                promotion.offer_type                                 AS offer_type,
                line.dns_promotion_rule                              AS rule,
                line.product_id                                      AS product_id,
                product.product_tmpl_id                              AS product_tmpl_id,
                template.categ_id                                    AS categ_id,
                pos_order.config_id                                  AS config_id,
                picking_type.warehouse_id                            AS warehouse_id,
                pos_order.session_id                                 AS session_id,
                pos_order.id                                         AS order_id,
                pos_order.company_id                                 AS company_id,
                -- pos.config.currency_id is computed, not a column; the company's currency is
                -- the only stored one, and a till always sells in it.
                company.currency_id                                  AS currency_id,
                line.qty                                             AS quantity,
                COALESCE(line.dns_base_price, 0) * line.qty          AS base_amount,
                COALESCE(line.dns_saved_amount, 0)                   AS saved_amount,
                line.price_subtotal                                  AS net_amount,
                1                                                    AS line_count
            FROM pos_order_line line
            JOIN pos_order ON pos_order.id = line.order_id
            JOIN product_product product ON product.id = line.product_id
            JOIN product_template template ON template.id = product.product_tmpl_id
            LEFT JOIN dns_pos_promotion promotion ON promotion.id = line.dns_promotion_id
            LEFT JOIN res_company company ON company.id = pos_order.company_id
            -- pos.config -> picking type -> warehouse is how this module already resolves a
            -- store group's coverage, so the analysis groups by the same notion of "store".
            LEFT JOIN pos_config config ON config.id = pos_order.config_id
            LEFT JOIN stock_picking_type picking_type ON picking_type.id = config.picking_type_id
            -- Only money actually taken: a parked or cancelled order must never inflate a
            -- campaign's reported savings.
            WHERE pos_order.state IN ('paid', 'done', 'invoiced')
              -- Matches dns_promotion_report_line_idx exactly, so Postgres can use the partial
              -- index instead of scanning every order line ever rung up. dns_promotion_name is
              -- what makes it a single condition: the engine always writes it alongside the id,
              -- and unlike the id it survives the campaign being deleted.
              AND line.dns_promotion_name IS NOT NULL
        """

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(
            "CREATE OR REPLACE VIEW %s AS (%s)" % (self._table, self._query())
        )
        # pos_order_line runs to millions of rows and only a handful carry a promotion. Without
        # this the pivot plans a parallel sequential scan of the whole table and takes tens of
        # seconds. The predicate is character-for-character the view's, which is what lets
        # Postgres prove the partial index applies; order_id is included so the join to pos_order
        # becomes a nested loop over ~10 rows rather than a hash join over every paid order.
        # Dropped first, not CREATE IF NOT EXISTS: the definition has changed once already, and
        # IF NOT EXISTS matches on name alone - it would silently keep an index built for the old
        # predicate, which then cannot satisfy the query and the scan goes back to sequential.
        self.env.cr.execute("DROP INDEX IF EXISTS dns_promotion_report_line_idx")
        self.env.cr.execute("""
            CREATE INDEX dns_promotion_report_line_idx
            ON pos_order_line (order_id, product_id)
            WHERE dns_promotion_name IS NOT NULL
        """)
        # The promotion columns are recent, so the planner has no statistics for them and
        # estimates millions of matching rows where there are a handful - enough to make it
        # prefer a sequential scan over the index above. Sampled, so it costs seconds even here.
        self.env.cr.execute("ANALYZE pos_order_line (dns_promotion_name)")
