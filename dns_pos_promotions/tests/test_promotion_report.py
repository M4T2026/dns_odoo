from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDnsPromotionReport(TransactionCase):
    """The redemption analysis view.

    Same conventions as TestDnsPromotionEngine: reuse the database's existing POS configs rather
    than creating one, because third-party modules on this database hook `pos.config.create` and
    call out to external services the test runner blocks.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        configs = cls.env["pos.config"].search([("company_id", "=", cls.env.company.id)], limit=2)
        if len(configs) < 2:
            raise cls.skipException(f"Needs 2 POS configs in {cls.env.company.name}.")
        cls.config_a, cls.config_b = configs[0], configs[1]
        cls.product_a = cls.env["product.product"].create({
            "name": "Report Product A", "lst_price": 10, "available_in_pos": True,
        })
        cls.product_b = cls.env["product.product"].create({
            "name": "Report Product B", "lst_price": 20, "available_in_pos": True,
        })
        cls.promotion = cls.env["dns.pos.promotion"].create({
            "name": "Report Promotion",
            "offer_type": "product_percent",
            "percent_discount": 10,
            "date_start": fields.Datetime.add(fields.Datetime.now(), hours=-1),
            "date_end": fields.Datetime.add(fields.Datetime.now(), days=2),
            "pos_config_ids": [(6, 0, [cls.config_a.id])],
            "selector_ids": [(0, 0, {"name": "A", "product_ids": [(6, 0, [cls.product_a.id])]})],
        })

    def _session(self, config):
        existing = self.env["pos.session"].search(
            [("config_id", "=", config.id), ("state", "!=", "closed")], limit=1
        )
        return existing or self.env["pos.session"].create({"config_id": config.id})

    def _order(self, config, lines, state="paid"):
        """A POS order carrying promotion columns, shaped like one the engine would have priced."""
        order = self.env["pos.order"].create({
            "session_id": self._session(config).id,
            "company_id": self.env.company.id,
            "amount_total": 0, "amount_tax": 0, "amount_paid": 0, "amount_return": 0,
            "lines": [(0, 0, values) for values in lines],
        })
        order.state = state
        # The view reads a real SQL table, so pending ORM writes have to be on disk first.
        self.env.flush_all()
        return order

    def _line(self, product, qty=1, price=10, saved=1.0, promotion=None, rule="percentage"):
        values = {
            "product_id": product.id,
            "qty": qty,
            "price_unit": price,
            "price_subtotal": price * qty - saved,
            "price_subtotal_incl": price * qty - saved,
            "dns_base_price": price,
            "dns_saved_amount": saved,
            "dns_promotion_rule": rule,
        }
        if promotion is not None:
            values["dns_promotion_id"] = promotion.id
            values["dns_promotion_name"] = promotion.name
        return values

    def _rows(self, order):
        return self.env["dns.promotion.report"].search([("order_id", "=", order.id)])

    # -----------------------------------------------------------------------------------

    def test_a_discounted_line_produces_one_report_row(self):
        order = self._order(self.config_a, [
            self._line(self.product_a, qty=2, price=10, saved=2, promotion=self.promotion),
        ])
        rows = self._rows(order)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.promotion_id, self.promotion)
        self.assertEqual(row.promotion_name, self.promotion.name)
        self.assertEqual(row.product_id, self.product_a)
        self.assertEqual(row.config_id, self.config_a)
        self.assertEqual(row.quantity, 2)
        self.assertEqual(row.base_amount, 20, "pre-promotion unit price times quantity")
        self.assertEqual(row.saved_amount, 2)
        self.assertEqual(row.net_amount, 18)
        self.assertEqual(row.offer_type, "product_percent")
        self.assertEqual(row.rule, "percentage")

    def test_lines_without_a_promotion_are_excluded(self):
        order = self._order(self.config_a, [
            self._line(self.product_a, saved=1, promotion=self.promotion),
            # An ordinary full-price line on the same receipt.
            {"product_id": self.product_b.id, "qty": 1, "price_unit": 20,
             "price_subtotal": 20, "price_subtotal_incl": 20},
        ])
        rows = self._rows(order)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows.product_id, self.product_a)

    def test_the_three_breakdowns_agree_on_the_total(self):
        """Promotion-wise, product-wise and store-wise must all sum to the same money."""
        order_a = self._order(self.config_a, [
            self._line(self.product_a, saved=2, promotion=self.promotion),
            self._line(self.product_b, saved=3, promotion=self.promotion),
        ])
        order_b = self._order(self.config_b, [
            self._line(self.product_a, saved=5, promotion=self.promotion),
        ])
        domain = [("order_id", "in", (order_a | order_b).ids)]
        report = self.env["dns.promotion.report"]

        def totals(group):
            return {
                row[group] if isinstance(row[group], str) else (row[group] or [None])[0]: row["saved_amount"]
                for row in report.read_group(domain, ["saved_amount:sum"], [group])
            }

        by_promotion = totals("promotion_name")
        by_product = totals("product_id")
        by_store = totals("config_id")

        self.assertEqual(sum(by_promotion.values()), 10)
        self.assertEqual(sum(by_product.values()), 10)
        self.assertEqual(sum(by_store.values()), 10)
        self.assertEqual(by_product[self.product_a.id], 7, "2 + 5 across both stores")
        self.assertEqual(by_product[self.product_b.id], 3)
        self.assertEqual(by_store[self.config_a.id], 5)
        self.assertEqual(by_store[self.config_b.id], 5)

    def test_a_store_rolls_up_its_tills(self):
        """pos.config is a till, not a store: one store runs several, and grouping by config
        would split a single shop's takings across three rows."""
        warehouse = self.config_a.picking_type_id.warehouse_id
        if not warehouse:
            self.skipTest("This POS config has no warehouse to report a store from.")
        siblings = self.env["pos.config"].search([
            ("picking_type_id.warehouse_id", "=", warehouse.id),
        ])
        if len(siblings) < 2:
            self.skipTest("Needs a store with more than one till.")

        first, second = siblings[0], siblings[1]
        order_a = self._order(first, [self._line(self.product_a, saved=3, promotion=self.promotion)])
        order_b = self._order(second, [self._line(self.product_a, saved=7, promotion=self.promotion)])

        domain = [("order_id", "in", (order_a | order_b).ids)]
        report = self.env["dns.promotion.report"]

        by_till = report.read_group(domain, ["saved_amount:sum"], ["config_id"])
        by_store = report.read_group(domain, ["saved_amount:sum"], ["warehouse_id"])

        self.assertEqual(len(by_till), 2, "two tills report separately")
        self.assertEqual(len(by_store), 1, "and roll up into one store")
        self.assertEqual(by_store[0]["warehouse_id"][0], warehouse.id)
        self.assertEqual(by_store[0]["saved_amount"], 10)
        self.assertEqual(sum(row["saved_amount"] for row in by_till), 10, "no money invented or lost")

    def test_every_row_reports_a_store(self):
        order = self._order(self.config_a, [
            self._line(self.product_a, saved=1, promotion=self.promotion),
        ])
        row = self._rows(order)
        self.assertEqual(row.config_id, self.config_a)
        self.assertEqual(row.warehouse_id, self.config_a.picking_type_id.warehouse_id)

    def test_unpaid_orders_do_not_inflate_the_takings(self):
        for state in ("draft", "cancel"):
            order = self._order(
                self.config_a,
                [self._line(self.product_a, saved=99, promotion=self.promotion)],
                state=state,
            )
            self.assertFalse(self._rows(order), f"a {state} order must not report savings")

    def test_a_deleted_promotion_keeps_its_name_in_history(self):
        """dns_promotion_id is ondelete='set null', so the takings outlive the campaign."""
        doomed = self.env["dns.pos.promotion"].create({
            "name": "Deleted Campaign",
            "offer_type": "product_percent",
            "percent_discount": 5,
            "date_start": fields.Datetime.add(fields.Datetime.now(), hours=-1),
            "date_end": fields.Datetime.add(fields.Datetime.now(), days=2),
            "pos_config_ids": [(6, 0, [self.config_a.id])],
            "selector_ids": [(0, 0, {"name": "A", "product_ids": [(6, 0, [self.product_a.id])]})],
        })
        order = self._order(self.config_a, [
            self._line(self.product_a, saved=4, promotion=doomed),
        ])
        doomed.unlink()
        self.env.flush_all()

        rows = self._rows(order)
        self.assertEqual(len(rows), 1, "the redemption survives the campaign")
        self.assertFalse(rows.promotion_id)
        self.assertEqual(rows.promotion_name, "Deleted Campaign")
        self.assertEqual(rows.saved_amount, 4)

    def test_the_discount_percentage_is_derived_not_summed(self):
        order = self._order(self.config_a, [
            self._line(self.product_a, qty=2, price=10, saved=4, promotion=self.promotion),
        ])
        self.assertEqual(self._rows(order).discount_pct, 20, "4 saved on a 20 base")

    def test_the_report_is_read_only(self):
        # A SQL view has no storage to write to; the manager ACL says 1,0,0,0 (viewers have none).
        access = self.env["ir.model.access"].search([
            ("model_id.model", "=", "dns.promotion.report"),
        ])
        self.assertTrue(access, "the report must be reachable at all")
        self.assertTrue(all(rule.perm_read for rule in access))
        self.assertFalse(any(rule.perm_write or rule.perm_create or rule.perm_unlink for rule in access))
