from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestDnsPromotionEngine(TransactionCase):
    @classmethod
    def _pos_configs(cls, count=1):
        """Existing POS configs in the current company.

        Deliberately not `env.ref("point_of_sale.pos_config_main")`: that XML ID only exists in
        databases loaded with POS demo data, and a real deployment has its own configs. Existing
        records are reused rather than created, because third-party modules on this database hook
        `pos.config.create` and call out to external payment services, which the test runner blocks.
        """
        configs = cls.env["pos.config"].search([("company_id", "=", cls.env.company.id)], limit=count)
        if len(configs) < count:
            raise cls.skipException(f"Needs {count} POS config(s) in {cls.env.company.name}.")
        return configs

    def _session(self, config):
        """A usable session, reusing an open one: Odoo forbids two open sessions per till."""
        existing = self.env["pos.session"].search(
            [("config_id", "=", config.id), ("state", "!=", "closed")], limit=1
        )
        return existing or self.env["pos.session"].create({"config_id": config.id})

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        configs = cls._pos_configs(2)
        cls.config_a, cls.config_b = configs[0], configs[1]
        cls.product_a = cls.env["product.product"].create({
            "name": "Promotion Test Product A", "lst_price": 10, "available_in_pos": True, "barcode": "DNS-PROMO-A",
        })
        cls.product_b = cls.env["product.product"].create({
            "name": "Promotion Test Product B", "lst_price": 20, "available_in_pos": True, "barcode": "DNS-PROMO-B",
        })
        cls.start = fields.Datetime.add(fields.Datetime.now(), hours=-1)
        cls.end = fields.Datetime.add(fields.Datetime.now(), days=2)

    def _promotion(self, **overrides):
        values = {
            "name": "Test Promotion",
            "offer_type": "product_percent",
            "percent_discount": 10,
            "date_start": self.start,
            "date_end": self.end,
            "pos_config_ids": [(6, 0, [self.config_a.id])],
            "selector_ids": [(0, 0, {"name": "Products", "product_ids": [(6, 0, [self.product_a.id])]})],
        }
        values.update(overrides)
        return self.env["dns.pos.promotion"].create(values)

    def test_many_promotions_with_same_priority_remain_distinct(self):
        promotions = self.env["dns.pos.promotion"]
        for index in range(150):
            promotions |= self._promotion(
                name=f"Scale Promotion {index}",
                priority=100,
                pos_config_ids=[(6, 0, [self.config_a.id if index % 2 else self.config_b.id])],
                date_start=fields.Datetime.add(self.start, days=index * 3),
                date_end=fields.Datetime.add(self.end, days=index * 3),
            )
        self.assertEqual(len(promotions), 150)
        self.assertEqual(len(set(promotions.ids)), 150)

    def test_materialization_and_store_payload(self):
        promotion = self._promotion()
        promotion.action_validate()
        self.assertIn(promotion.state, ("active", "scheduled"))
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)
        self.assertTrue(promotion.pos_payload(self.config_a)["product_ids"])
        self.assertNotIn(self.config_b, promotion._resolved_pos_configs())

    def test_conflicting_product_store_and_dates_are_blocked(self):
        first = self._promotion(name="First")
        first.action_validate()
        second = self._promotion(name="Second")
        second.action_validate()
        self.assertEqual(second.state, "conflict")
        self.assertTrue(second.conflict_ids)
        self.assertEqual(second.conflict_ids.product_id, self.product_a)

    def test_non_overlapping_store_is_allowed(self):
        first = self._promotion(name="Store A")
        first.action_validate()
        second = self._promotion(name="Store B", pos_config_ids=[(6, 0, [self.config_b.id])])
        second.action_validate()
        self.assertFalse(second.conflict_ids)

    def test_non_overlapping_dates_are_allowed(self):
        first = self._promotion(name="Current")
        first.action_validate()
        second = self._promotion(
            name="Future",
            date_start=fields.Datetime.add(self.end, hours=1),
            date_end=fields.Datetime.add(self.end, days=2),
        )
        second.action_validate()
        self.assertEqual(second.state, "scheduled")
        self.assertFalse(second.conflict_ids)

    def test_multi_barcode_selector(self):
        self.product_b.write({"allow_multi_barcodes": True})
        self.env["product.multi.barcode"].create({"multi_barcode": "ALT-DNS-B", "product_id": self.product_b.id})
        promotion = self._promotion(
            name="Barcode offer",
            selector_ids=[(0, 0, {"name": "Barcode", "barcode_terms": "ALT-DNS-B"})],
        )
        promotion.action_validate()
        self.assertEqual(promotion.scope_ids.product_id, self.product_b)
        lookup = self.env["dns.pos.promotion"].product_promotion_lookup("ALT-DNS-B")
        self.assertEqual(lookup[0]["id"], self.product_b.id)

    def test_invalid_reward_and_date_are_rejected(self):
        with self.assertRaises(ValidationError):
            self._promotion(percent_discount=101)
        with self.assertRaises(Exception):
            self._promotion(date_start=self.end, date_end=self.start)

    def test_multi_group_materialization(self):
        promotion = self._promotion(
            name="Drink and snack",
            offer_type="multi_group",
            percent_discount=0,
            bundle_total=25,
            selector_ids=[],
            assortment_group_ids=[
                (0, 0, {"name": "Group A", "required_qty": 2, "selector_ids": [(0, 0, {"name": "A", "product_ids": [(6, 0, [self.product_a.id])]})]}),
                (0, 0, {"name": "Group B", "required_qty": 1, "selector_ids": [(0, 0, {"name": "B", "product_ids": [(6, 0, [self.product_b.id])]})]}),
            ],
        )
        promotion.action_validate()
        self.assertEqual(set(promotion.scope_ids.product_id.ids), {self.product_a.id, self.product_b.id})
        self.assertEqual(len(promotion.pos_payload(self.config_a)["groups"]), 2)

    # ---------------------------------------------------------------- POS integration

    def test_pos_loader_works_for_a_plain_cashier(self):
        """A cashier with no promotion group must still be able to open a session.

        The loader runs as the cashier inside load_pos_data. Promotion configuration is
        manager-only, and pos.config may be restricted per user, so the loader must sudo.
        """
        promotion = self._promotion(name="Cashier visible")
        promotion.action_validate()
        cashier = self.env["res.users"].create({
            "name": "DNS Test Cashier",
            "login": "dns_test_cashier_prod",
            "groups_id": [(6, 0, [self.env.ref("point_of_sale.group_pos_user").id])],
        })
        session = self._session(self.config_a)
        payload = session.with_user(cashier)._get_pos_ui_dns_pos_promotion({})
        self.assertTrue(any(entry["id"] == promotion.id for entry in payload))

    def test_loader_respects_the_pos_config_switch(self):
        self._promotion(name="Switchable").action_validate()
        session = self._session(self.config_a)
        self.assertTrue(session._get_pos_ui_dns_pos_promotion({}))
        self.config_a.dns_promotions_enabled = False
        self.assertEqual(session._get_pos_ui_dns_pos_promotion({}), [])

    def test_payload_cache_notices_a_lower_version_promotion_changing(self):
        """The cache fingerprint must cover every promotion, not just the highest version.

        With promotions at v5 and v3, editing the second to v4 leaves the maximum at 5; a
        max-based cache key would keep serving the stale payload.
        """
        first = self._promotion(name="Cache A")
        first.action_validate()
        second = self._promotion(name="Cache B", pos_config_ids=[(6, 0, [self.config_a.id])],
                                 selector_ids=[(0, 0, {"name": "B", "product_ids": [(6, 0, [self.product_b.id])]})])
        second.action_validate()
        for _ in range(3):
            first.with_context(dns_system_write=True).write({"version": first.version + 1})

        session = self._session(self.config_a)
        before = session._get_pos_ui_dns_pos_promotion({})
        self.assertTrue(before)
        second.write({"percent_discount": 42})
        second.action_validate()
        after = session._get_pos_ui_dns_pos_promotion({})
        changed = [row for row in after if row["id"] == second.id]
        self.assertTrue(changed, "the edited promotion is still in the payload")
        self.assertEqual(changed[0]["percent_discount"], 42, "cache served a stale payload")

    def test_order_line_fields_round_trip_to_the_ui(self):
        """Without _export_for_ui, refunds and reprints lose the promotion entirely."""
        promotion = self._promotion(name="Persisted")
        promotion.action_validate()
        session = self._session(self.config_a)
        order = self.env["pos.order"].create({
            "session_id": session.id,
            "company_id": self.env.company.id,
            "amount_total": 9, "amount_tax": 0, "amount_paid": 0, "amount_return": 0,
            "lines": [(0, 0, {
                "product_id": self.product_a.id,
                "qty": 1, "price_unit": 10, "discount": 10,
                "price_subtotal": 9, "price_subtotal_incl": 9,
                "dns_promotion_id": promotion.id,
                "dns_promotion_name": promotion.name,
                "dns_promotion_rule": "percentage",
                "dns_base_price": 10,
                "dns_saved_amount": 1,
            })],
        })
        exported = order.lines._export_for_ui(order.lines)
        self.assertEqual(exported["dns_promotion_id"], promotion.id)
        self.assertEqual(exported["dns_saved_amount"], 1)

    def test_promotion_products_workspace_paginates_and_filters(self):
        promotion = self._promotion(
            name="Product list",
            selector_ids=[(0, 0, {"name": "Both", "product_ids": [(6, 0, [self.product_a.id, self.product_b.id])]})],
        )
        promotion.action_validate()
        page = self.env["dns.pos.promotion"].promotion_products_workspace(promotion.id, limit=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["records"]), 1)
        self.assertIn("barcode", page["records"][0])
        filtered = self.env["dns.pos.promotion"].promotion_products_workspace(promotion.id, query="DNS-PROMO-B")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["records"][0]["product_id"], self.product_b.id)

    # --- Editing an existing promotion, and wall-clock scheduling -------------------------

    def _now_window(self):
        """The class's live date window as builder wall-clock strings, so a promotion built from
        `_workspace_payload` can be asserted to land on `active` rather than `scheduled`."""
        model = self.env["dns.pos.promotion"]
        tz = "Australia/Brisbane"
        return {
            "date_start": model._utc_to_wall(self.start, tz),
            "date_end": model._utc_to_wall(self.end, tz),
            "timezone": tz,
        }

    def _workspace_payload(self, **overrides):
        payload = {
            "name": "Workspace Promotion",
            "offer_type": "product_percent",
            "percent_discount": 15,
            "date_start": "2026-09-01 09:00:00",
            "date_end": "2026-09-30 21:00:00",
            "timezone": "Australia/Brisbane",
            "pos_config_ids": [self.config_a.id],
            "selector": {"barcode_terms": "DNS-PROMO-A"},
        }
        payload.update(overrides)
        return payload

    def test_editing_shows_and_edits_an_explicit_product_list(self):
        """Migrated promotions hold products as a list; the builder used to reopen them empty."""
        promotion = self._promotion(
            name="Migrated explicit",
            selector_ids=[(0, 0, {"name": "Migrated explicit products",
                                  "product_ids": [(6, 0, (self.product_a | self.product_b).ids)]})],
        )
        model = self.env["dns.pos.promotion"]
        editable = model.workspace_detail(promotion.id)["editable"]
        self.assertEqual(sorted(editable["selector"]["product_ids"]), sorted((self.product_a | self.product_b).ids))
        self.assertEqual({row["barcode"] for row in editable["selector"]["products"]}, {"DNS-PROMO-A", "DNS-PROMO-B"})

        # What the builder sends back after the operator removes product B.
        selector = {key: value for key, value in editable["selector"].items() if key != "products"}
        selector["product_ids"] = self.product_a.ids
        payload = dict(editable, selector=selector)
        payload.pop("manual_products")
        model.update_from_workspace(promotion.id, payload, validate=True)
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)

    def test_wall_clock_dates_convert_to_utc(self):
        """A start typed as 9am is 9am in the promotion's zone, not 9am UTC."""
        model = self.env["dns.pos.promotion"]
        created = model.create_from_workspace(self._workspace_payload())
        promotion = model.browse(created["id"])
        # Brisbane is UTC+10 year round, so 09:00 local is 23:00 the previous day in UTC.
        self.assertEqual(fields.Datetime.to_string(promotion.date_start), "2026-08-31 23:00:00")
        self.assertEqual(fields.Datetime.to_string(promotion.date_end), "2026-09-30 11:00:00")
        detail = model.workspace_detail(promotion.id)
        self.assertEqual(detail["date_start"], "2026-09-01 09:00:00")
        self.assertEqual(detail["editable"]["date_end"], "2026-09-30 21:00:00")

    def test_wall_clock_respects_a_dst_observing_zone(self):
        model = self.env["dns.pos.promotion"]
        created = model.create_from_workspace(self._workspace_payload(
            timezone="Australia/Sydney",
            date_start="2026-01-15 09:00:00",  # AEDT, UTC+11
            date_end="2026-07-15 09:00:00",    # AEST, UTC+10
        ))
        promotion = model.browse(created["id"])
        self.assertEqual(fields.Datetime.to_string(promotion.date_start), "2026-01-14 22:00:00")
        self.assertEqual(fields.Datetime.to_string(promotion.date_end), "2026-07-14 23:00:00")

    def test_update_from_workspace_edits_in_place(self):
        """Editing must not require archive-and-recreate: same record, same id."""
        model = self.env["dns.pos.promotion"]
        promotion = self._promotion(name="Editable")
        promotion.action_validate()
        self.assertEqual(promotion.state, "active")

        result = model.update_from_workspace(promotion.id, self._workspace_payload(
            name="Editable (revised)",
            percent_discount=25,
            date_start=promotion._utc_to_wall(self.start, promotion.timezone),
            date_end=promotion._utc_to_wall(self.end, promotion.timezone),
            selector={"product_ids": [self.product_a.id]},
        ))
        self.assertEqual(result["id"], promotion.id)
        self.assertEqual(promotion.name, "Editable (revised)")
        self.assertEqual(promotion.percent_discount, 25)
        # A bare save takes it off the tills until someone revalidates.
        self.assertEqual(promotion.state, "draft")

        revalidated = model.update_from_workspace(promotion.id, self._workspace_payload(
            name="Editable (revised)",
            percent_discount=30,
            date_start=promotion._utc_to_wall(self.start, promotion.timezone),
            date_end=promotion._utc_to_wall(self.end, promotion.timezone),
            selector={"product_ids": [self.product_a.id]},
        ), validate=True)
        self.assertEqual(revalidated["state"], "active")
        self.assertEqual(promotion.percent_discount, 30)
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)

    def test_update_reuses_the_builder_selector(self):
        model = self.env["dns.pos.promotion"]
        promotion = self._promotion(name="Selector reuse")
        selector_id = promotion.selector_ids.id
        model.update_from_workspace(promotion.id, self._workspace_payload(
            name="Selector reuse",
            date_start=promotion._utc_to_wall(self.start, promotion.timezone),
            date_end=promotion._utc_to_wall(self.end, promotion.timezone),
            selector={"barcode_terms": "DNS-PROMO-B"},
        ))
        self.assertEqual(promotion.selector_ids.ids, [selector_id])
        self.assertEqual(promotion.selector_ids.barcode_terms, "DNS-PROMO-B")

    def test_update_leaves_assortment_group_offers_alone(self):
        """The flat builder selector cannot express groups, so it must not be written onto one."""
        promotion = self._promotion(
            name="Grouped",
            offer_type="multi_group",
            bundle_total=25,
            selector_ids=[(5, 0, 0)],
            assortment_group_ids=[(0, 0, {
                "name": "Tops", "required_qty": 1,
                "selector_ids": [(0, 0, {"name": "Tops", "product_ids": [(6, 0, [self.product_a.id])]})],
            })],
        )
        self.env["dns.pos.promotion"].update_from_workspace(promotion.id, self._workspace_payload(
            name="Grouped",
            offer_type="multi_group",
            bundle_total=25,
            date_start=promotion._utc_to_wall(self.start, promotion.timezone),
            date_end=promotion._utc_to_wall(self.end, promotion.timezone),
            selector={"barcode_terms": "DNS-PROMO-B"},
        ))
        # The group's own selector is linked to the promotion too, so the check is that no
        # ungrouped selector appeared and the group's selection is untouched.
        self.assertFalse(promotion._builder_selector())
        self.assertEqual(promotion.selector_ids, promotion.assortment_group_ids.selector_ids)
        self.assertEqual(promotion.assortment_group_ids.selector_ids.product_ids, self.product_a)
        self.assertFalse(promotion.assortment_group_ids.selector_ids.barcode_terms)

    # --- Only the buildable offer types are offered -----------------------------------------

    def test_workspace_options_offer_only_the_buildable_types(self):
        options = self.env["dns.pos.promotion"].workspace_options()
        self.assertEqual(
            [item["value"] for item in options["offer_types"]],
            ["product_percent", "product_fixed", "mix_match", "buy_x_get_y", "order_percent"],
        )
        # Every label still ships so a promotion on a retired type is named, not shown as a key.
        self.assertEqual(options["offer_labels"]["single_bundle"], "Single Product Bundle Price")
        self.assertEqual(options["offer_labels"]["order_fixed"], "Order Total Fixed Discount")
        self.assertEqual(options["offer_labels"]["coupon"], "Coupon / Code Offer")

    def test_retired_offer_types_cannot_be_built(self):
        model = self.env["dns.pos.promotion"]
        for retired in ("single_bundle", "multi_group", "order_fixed", "coupon"):
            with self.assertRaises(ValidationError, msg=retired):
                model.create_from_workspace(self._workspace_payload(offer_type=retired))

    def test_order_wide_offers_are_buildable(self):
        model = self.env["dns.pos.promotion"]
        percent = model.browse(model.create_from_workspace(self._workspace_payload(
            offer_type="order_percent", percent_discount=5, minimum_order_amount=50,
        ))["id"])
        self.assertEqual(percent.offer_type, "order_percent")
        self.assertEqual(percent.minimum_order_amount, 50)

    def test_a_legacy_promotion_keeps_its_retired_type_when_edited(self):
        """Editing a single_bundle promotion's dates must not force it onto another type."""
        model = self.env["dns.pos.promotion"]
        promotion = self._promotion(
            name="Legacy", offer_type="single_bundle", required_qty=2, bundle_total=5,
        )
        model.update_from_workspace(promotion.id, self._workspace_payload(
            name="Legacy renamed",
            offer_type="single_bundle",
            required_qty=2,
            bundle_total=5,
            date_start=promotion._utc_to_wall(self.start, promotion.timezone),
            date_end=promotion._utc_to_wall(self.end, promotion.timezone),
        ))
        self.assertEqual(promotion.name, "Legacy renamed")
        self.assertEqual(promotion.offer_type, "single_bundle")
        # ...but it cannot be moved sideways onto a different retired type.
        with self.assertRaises(ValidationError):
            model.update_from_workspace(promotion.id, self._workspace_payload(
                name="Legacy renamed",
                offer_type="multi_group",
                date_start=promotion._utc_to_wall(self.start, promotion.timezone),
                date_end=promotion._utc_to_wall(self.end, promotion.timezone),
            ))

    def test_buy_x_get_y_is_buildable_end_to_end(self):
        """The builder had no reward-product input, so this type could never validate."""
        model = self.env["dns.pos.promotion"]
        hits = model.reward_product_lookup("DNS-PROMO-B")
        self.assertEqual([hit["id"] for hit in hits], [self.product_b.id])
        created = model.create_from_workspace(self._workspace_payload(
            offer_type="buy_x_get_y",
            required_qty=2,
            reward_product_id=hits[0]["id"],
            reward_qty=1,
        ))
        promotion = model.browse(created["id"])
        self.assertEqual(promotion.reward_product_id, self.product_b)
        self.assertEqual(promotion.reward_qty, 1)
        self.assertEqual(model.workspace_detail(promotion.id)["editable"]["reward_product_name"],
                         self.product_b.display_name)

    def test_tag_selector_resolves_through_the_template(self):
        """product_tag_ids is stored on product.template, not on the variant."""
        tag = self.env["product.tag"].create({"name": "DNS Promo Tag"})
        self.product_b.product_tmpl_id.product_tag_ids = [(6, 0, tag.ids)]
        promotion = self._promotion(
            name="Tagged",
            selector_ids=[(0, 0, {"name": "By tag", "tag_ids": [(6, 0, tag.ids)]})],
        )
        promotion.action_validate()
        self.assertEqual(promotion.scope_ids.product_id, self.product_b)

    def test_season_and_subcategory_selectors(self):
        season = self.env["products.seasons"].create({"name": "Summer 2026"})
        subcategory = self.env["products.subcategory"].create({"name": "Large"})
        vendor = self.env["res.partner"].create({"name": "Acme Supplier", "supplier_rank": 1})
        self.product_b.product_tmpl_id.write({
            "product_season": season.id,
            "product_subcategory": subcategory.id,
            "vendor_id": vendor.id,
        })
        promotion = self._promotion(
            name="Season and Size offer",
            selector_ids=[(0, 0, {
                "name": "Filters",
                "season_ids": [(6, 0, season.ids)],
                "size_ids": [(6, 0, subcategory.ids)],
                "vendor_ids": [(6, 0, vendor.ids)],
            })],
        )
        promotion.action_validate()
        self.assertEqual(promotion.scope_ids.product_id, self.product_b)

    def test_workspace_options_ship_tags_not_brands(self):
        options = self.env["dns.pos.promotion"].workspace_options()
        self.assertIn("tags", options)
        self.assertNotIn("brands", options)

    def test_tag_selector_round_trips_through_the_builder(self):
        model = self.env["dns.pos.promotion"]
        tag = self.env["product.tag"].create({"name": "DNS Builder Tag"})
        self.product_a.product_tmpl_id.product_tag_ids = [(6, 0, tag.ids)]
        created = model.create_from_workspace(self._workspace_payload(
            selector={"tag_ids": tag.ids},
        ))
        promotion = model.browse(created["id"])
        self.assertEqual(promotion.selector_ids.tag_ids, tag)
        detail = model.workspace_detail(promotion.id)
        self.assertEqual(detail["editable"]["selector"]["tag_ids"], tag.ids)
        self.assertEqual(detail["selectors"][0]["tags"], [tag.name])

    def test_tag_preview_counts_products(self):
        tag = self.env["product.tag"].create({"name": "DNS Preview Tag"})
        for product in (self.product_a, self.product_b):
            product.product_tmpl_id.product_tag_ids = [(4, tag.id)]
        preview = self.env["dns.pos.promotion"].preview_workspace_selector({"tag_ids": tag.ids})
        self.assertEqual(preview["count"], 2)

    # --- The giveaway product has to reach the till ----------------------------------------

    def test_reward_product_ships_even_when_the_catalogue_is_truncated(self):
        """Odoo caps the POS product load; the reward product is never scanned, so nothing else
        would pull it in and Buy X Get Y would silently do nothing."""
        promotion = self._promotion(
            name="Giveaway",
            offer_type="buy_x_get_y",
            required_qty=2,
            reward_product_id=self.product_b.id,
            reward_qty=1,
            selector_ids=[(0, 0, {"name": "Trigger", "product_ids": [(6, 0, [self.product_a.id])]})],
        )
        promotion.action_validate()
        self.assertEqual(promotion.state, "active")

        # Force the truncation instead of hoping the catalogue is big enough.
        self.env["ir.config_parameter"].sudo().set_param("point_of_sale.limited_product_count", "1")
        session = self._session(self.config_a)
        params = session._loader_params_product_product()

        truncated = {row["id"] for row in self.config_a.get_limited_products_loading(["id"])}
        self.assertNotIn(self.product_b.id, truncated, "core would drop the reward product")

        loaded = session._get_pos_ui_product_product(params)
        by_id = {row["id"]: row for row in loaded}
        self.assertIn(self.product_b.id, by_id, "the reward product must be shipped anyway")
        self.assertEqual(len(loaded), len({row["id"] for row in loaded}), "no duplicate rows")

    def test_an_injected_reward_product_is_shaped_like_a_normal_one(self):
        """The till reads these rows through the same code path as any other product."""
        promotion = self._promotion(
            name="Giveaway shape",
            offer_type="buy_x_get_y",
            required_qty=2,
            reward_product_id=self.product_b.id,
            reward_qty=1,
            selector_ids=[(0, 0, {"name": "Trigger", "product_ids": [(6, 0, [self.product_a.id])]})],
        )
        promotion.action_validate()
        self.env["ir.config_parameter"].sudo().set_param("point_of_sale.limited_product_count", "1")
        session = self._session(self.config_a)
        rows = {row["id"]: row for row in session._get_pos_ui_product_product(
            session._loader_params_product_product())}

        injected = rows[self.product_b.id]
        reference = rows[next(pid for pid in rows if pid != self.product_b.id)]
        self.assertEqual(set(injected), set(reference), "same keys as a loader-produced row")
        self.assertIn("categ", injected, "_process_pos_ui_product_product ran on it")
        self.assertIs(type(injected["image_128"]), bool, "image flattened to a bool like core does")

    def test_a_promotion_without_a_reward_product_loads_nothing_extra(self):
        promotion = self._promotion(name="No giveaway")
        promotion.action_validate()
        session = self._session(self.config_a)
        params = session._loader_params_product_product()
        self.assertEqual(
            len(session._get_pos_ui_product_product(params)),
            len(super(type(session), session)._get_pos_ui_product_product(params)),
        )

    def test_the_two_pos_loaders_agree_on_what_applies(self):
        """The product loader and the payload builder must not drift on 'applies here'."""
        promotion = self._promotion(
            name="Shared definition",
            offer_type="buy_x_get_y",
            required_qty=2,
            reward_product_id=self.product_b.id,
            reward_qty=1,
            selector_ids=[(0, 0, {"name": "Trigger", "product_ids": [(6, 0, [self.product_a.id])]})],
        )
        promotion.action_validate()
        session = self._session(self.config_a)
        applicable = session._dns_applicable_promotions()
        self.assertIn(promotion, applicable)
        payload_ids = {row["id"] for row in session.get_dns_promotion_payload()}
        self.assertEqual(payload_ids, set(applicable.ids))

    def test_applicable_promotions_is_empty_when_the_till_has_promotions_off(self):
        self.config_a.dns_promotions_enabled = False
        try:
            session = self._session(self.config_a)
            self.assertFalse(session._dns_applicable_promotions())
        finally:
            self.config_a.dns_promotions_enabled = True

    # --- Coupon / code offers ---------------------------------------------------------------

    def _coupon(self, code, **overrides):
        values = {
            "name": f"Coupon {code}",
            "offer_type": "coupon",
            "coupon_code": code,
            "percent_discount": 10,
            "selector_ids": [(5, 0, 0)],
        }
        values.update(overrides)
        return self._promotion(**values)

    def test_a_coupon_without_selectors_is_order_wide(self):
        """A coupon prices the whole basket; demanding a product list made it unvalidatable."""
        promotion = self._coupon("DNSCOUPON1", minimum_order_amount=20)
        promotion.action_validate()
        self.assertEqual(promotion.state, "active")
        self.assertTrue(promotion.all_products)
        self.assertFalse(promotion.scope_ids, "an order-wide offer writes no scope rows")

        payload = promotion.pos_payload(self.config_a)
        self.assertEqual(payload["coupon_code"], "DNSCOUPON1")
        self.assertEqual(payload["minimum_order_amount"], 20)
        self.assertEqual(payload["percent_discount"], 10)
        self.assertTrue(payload["all_products"])

    def test_a_coupon_with_selectors_stays_scoped(self):
        promotion = self._coupon(
            "DNSCOUPON2",
            selector_ids=[(0, 0, {"name": "Just A", "product_ids": [(6, 0, [self.product_a.id])]})],
        )
        promotion.action_validate()
        self.assertFalse(promotion.all_products)
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)

    def test_a_coupon_with_no_discount_is_rejected(self):
        """Otherwise it goes active, the till accepts the code, and nothing comes off."""
        with self.assertRaises(ValidationError):
            self._coupon("DNSCOUPON3", percent_discount=0, fixed_discount=0)

    def test_a_coupon_may_be_a_percentage_or_an_amount(self):
        percent = self._coupon("DNSCOUPON4", percent_discount=15, fixed_discount=0)
        percent.action_validate()
        self.assertEqual(percent.state, "active")
        amount = self._coupon("DNSCOUPON5", percent_discount=0, fixed_discount=7.5)
        amount.action_validate()
        self.assertEqual(amount.pos_payload(self.config_a)["fixed_discount"], 7.5)

    def test_a_coupon_without_a_code_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._coupon(False)

    def test_a_coupon_percentage_over_100_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._coupon("DNSCOUPON6", percent_discount=150)

    def test_a_duplicate_code_names_the_promotion_already_using_it(self):
        first = self._coupon("DNSDUPE")
        with self.assertRaises(ValidationError) as caught:
            self._coupon("DNSDUPE", name="Second")
        self.assertIn(first.name, str(caught.exception))
        self.assertIn("DNSDUPE", str(caught.exception))

    def test_a_code_is_not_freed_by_cancelling_the_promotion(self):
        """Codes are permanent by design: an old flyer must never revive against a new offer."""
        first = self._coupon("DNSRETIRED")
        first.action_cancel()
        self.assertEqual(first.state, "cancelled")
        with self.assertRaises(ValidationError):
            self._coupon("DNSRETIRED", name="Reused")

    def test_a_coupon_can_no_longer_be_built_from_the_workspace(self):
        """Code offers are retired: stored ones keep pricing, but none can be created."""
        model = self.env["dns.pos.promotion"]
        with self.assertRaises(ValidationError) as caught:
            model.create_from_workspace(self._workspace_payload(
                name="Workspace coupon",
                offer_type="coupon",
                coupon_code="DNSWORKSPACE",
                percent_discount=10,
                minimum_order_amount=20,
                selector={},
                **self._now_window(),
            ))
        self.assertIn("Coupon / Code Offer", str(caught.exception))

    def test_an_untouched_product_step_leaves_an_order_wide_offer_order_wide(self):
        """The builder used to attach an empty selector, which read as 'scoped, matches nothing'
        and made every order-wide offer built in the workspace impossible to validate."""
        model = self.env["dns.pos.promotion"]
        for offer_type, extra in (
            ("order_percent", {"percent_discount": 5}),
        ):
            created = model.create_from_workspace(self._workspace_payload(
                name=f"Order wide {offer_type}", offer_type=offer_type, selector={},
                **self._now_window(), **extra,
            ))
            promotion = model.browse(created["id"])
            self.assertFalse(promotion.selector_ids, f"{offer_type}: no empty selector created")
            promotion.action_validate()
            self.assertEqual(promotion.state, "active", offer_type)
            self.assertTrue(promotion.all_products, offer_type)

    def test_a_selector_that_narrows_nothing_is_ignored(self):
        """Covers the selector being emptied by a later edit, not just never filled in."""
        promotion = self._promotion(
            name="Emptied",
            offer_type="order_fixed",
            fixed_discount=5,
            selector_ids=[(0, 0, {"name": "Left blank"})],
        )
        self.assertTrue(promotion.selector_ids)
        self.assertFalse(promotion.selector_ids._has_criteria())
        promotion.action_validate()
        self.assertTrue(promotion.all_products)

    def test_an_empty_selector_does_not_make_a_product_offer_universal(self):
        """The escape hatch must not silently widen a product-scoped offer to everything."""
        promotion = self._promotion(
            name="Product offer with no products",
            selector_ids=[(0, 0, {"name": "Left blank"})],
        )
        with self.assertRaises(ValidationError):
            promotion.action_validate()
        self.assertFalse(promotion.all_products)

    def test_reward_lookup_ignores_an_empty_query(self):
        self.assertEqual(self.env["dns.pos.promotion"].reward_product_lookup("  "), [])

    def test_archived_promotions_cannot_be_edited(self):
        promotion = self._promotion(name="Archived")
        promotion.action_archive()
        with self.assertRaises(UserError):
            self.env["dns.pos.promotion"].update_from_workspace(promotion.id, self._workspace_payload())

    def test_add_and_remove_single_product(self):
        model = self.env["dns.pos.promotion"]
        promotion = self._promotion(name="Incremental")
        promotion.action_validate()
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)

        added = model.add_promotion_products(promotion.id, "DNS-PROMO-B\nNOT-A-REAL-CODE", validate=True)
        self.assertEqual(added["added"], 1)
        self.assertEqual(added["unmatched"], ["NOT-A-REAL-CODE"])
        self.assertEqual(promotion.state, "active")
        self.assertEqual(promotion.scope_ids.product_id, self.product_a | self.product_b)

        removed = model.remove_promotion_products(promotion.id, [self.product_a.id], validate=True)
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(promotion.excluded_product_ids, self.product_a)
        self.assertEqual(promotion.scope_ids.product_id, self.product_b)

    def test_re_adding_an_excluded_product_clears_the_exclusion(self):
        model = self.env["dns.pos.promotion"]
        promotion = self._promotion(
            name="Re-add",
            selector_ids=[(0, 0, {"name": "Both", "product_ids": [(6, 0, [self.product_a.id, self.product_b.id])]})],
        )
        promotion.action_validate()
        model.remove_promotion_products(promotion.id, [self.product_b.id], validate=True)
        self.assertEqual(promotion.scope_ids.product_id, self.product_a)
        model.add_promotion_products(promotion.id, "DNS-PROMO-B", validate=True)
        self.assertFalse(promotion.excluded_product_ids)
        self.assertEqual(promotion.scope_ids.product_id, self.product_a | self.product_b)

    def test_excluding_everything_is_rejected(self):
        promotion = self._promotion(name="Fully excluded")
        promotion.write({"excluded_product_ids": [(6, 0, [self.product_a.id])]})
        with self.assertRaises(ValidationError):
            promotion.action_validate()

    def test_exclusion_survives_an_all_products_offer(self):
        """An order-wide offer ships no product ids, so its exclusions must reach the till."""
        promotion = self._promotion(
            name="Order wide",
            offer_type="order_percent",
            selector_ids=[(5, 0, 0)],
            excluded_product_ids=[(6, 0, [self.product_b.id])],
        )
        promotion.action_validate()
        payload = promotion.pos_payload(self.config_a)
        self.assertTrue(payload["all_products"])
        self.assertEqual(payload["excluded_product_ids"], [self.product_b.id])

    def test_scope_backed_offers_ship_no_exclusion_list(self):
        promotion = self._promotion(
            name="Scoped",
            selector_ids=[(0, 0, {"name": "Both", "product_ids": [(6, 0, [self.product_a.id, self.product_b.id])]})],
            excluded_product_ids=[(6, 0, [self.product_b.id])],
        )
        promotion.action_validate()
        payload = promotion.pos_payload(self.config_a)
        # Already filtered out server-side, so the till needs no runtime check.
        self.assertEqual(payload["excluded_product_ids"], [])
        self.assertEqual(payload["product_ids"], [self.product_a.id])

    # --- Store groups -------------------------------------------------------------------------

    def _store_group(self, configs):
        model = self.env["dns.pos.promotion"]
        saved = model.save_store_group_workspace({"name": "Test Group", "pos_config_ids": configs.ids})
        return self.env["dns.promotion.store.group"].browse(saved["id"])

    def test_store_group_editor_reopens_with_its_tills(self):
        """The editor used to match warehouse names against till names, so it ticked nothing."""
        group = self._store_group(self.config_a | self.config_b)
        row = next(r for r in self.env["dns.pos.promotion"].store_groups_workspace() if r["id"] == group.id)
        self.assertEqual(set(row["pos_config_ids"]), {self.config_a.id, self.config_b.id})

    def test_store_group_change_reaches_live_promotions(self):
        group = self._store_group(self.config_a)
        promotion = self._promotion(name="Grouped", pos_config_ids=[(5, 0, 0)], store_group_ids=[(6, 0, group.ids)])
        promotion.action_validate()
        self.assertEqual(promotion.store_scope_ids.config_id, self.config_a)
        version = promotion.version

        result = self.env["dns.pos.promotion"].save_store_group_workspace(
            {"id": group.id, "name": group.name, "pos_config_ids": (self.config_a | self.config_b).ids}
        )
        self.assertEqual(result["revalidated"], 1)
        self.assertIn(promotion.state, ("active", "scheduled"))
        self.assertEqual(promotion.store_scope_ids.config_id, self.config_a | self.config_b)
        self.assertGreater(promotion.version, version, "tills rebuild their cache on a version change")

        payload = self._session(self.config_b)._get_pos_ui_dns_pos_promotion({})
        self.assertTrue(any(entry["id"] == promotion.id for entry in payload))

    def test_renaming_a_store_group_does_not_revalidate(self):
        group = self._store_group(self.config_a)
        promotion = self._promotion(name="Renamed", pos_config_ids=[(5, 0, 0)], store_group_ids=[(6, 0, group.ids)])
        promotion.action_validate()
        version = promotion.version
        result = self.env["dns.pos.promotion"].save_store_group_workspace(
            {"id": group.id, "name": "Renamed Group", "pos_config_ids": self.config_a.ids}
        )
        self.assertEqual(result["revalidated"], 0)
        self.assertEqual(promotion.version, version)

    def test_store_group_change_that_breaks_a_live_promotion_is_refused(self):
        group = self._store_group(self.config_a)
        promotion = self._promotion(name="Only coverage", pos_config_ids=[(5, 0, 0)], store_group_ids=[(6, 0, group.ids)])
        promotion.action_validate()
        with self.assertRaises(ValidationError):
            group.unlink()

    def test_store_group_change_into_a_conflict_is_refused(self):
        other = self._promotion(name="Already on B", pos_config_ids=[(6, 0, self.config_b.ids)])
        other.action_validate()
        group = self._store_group(self.config_a)
        promotion = self._promotion(name="Grouped on A", pos_config_ids=[(5, 0, 0)], store_group_ids=[(6, 0, group.ids)])
        promotion.action_validate()
        self.assertIn(promotion.state, ("active", "scheduled"))
        with self.assertRaises(ValidationError):
            group.write({"pos_config_ids": [(4, self.config_b.id)]})

    # --- Promotion viewers --------------------------------------------------------------------

    def _viewer(self, configs):
        return self.env["res.users"].create({
            "name": "DNS Test Promotion Viewer",
            "login": "dns_test_promotion_viewer",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("dns_pos_promotions.group_dns_promotion_user").id,
            ])],
            "pos_config_ids": [(6, 0, configs.ids)],
        })

    def test_viewer_sees_only_promotions_on_their_allowed_pos(self):
        on_a = self._promotion(name="Viewer A")
        on_a.action_validate()
        on_b = self._promotion(name="Viewer B", pos_config_ids=[(6, 0, self.config_b.ids)])
        on_b.action_validate()
        draft_on_a = self._promotion(name="Viewer draft", selector_ids=[(0, 0, {"name": "B", "product_ids": [(6, 0, self.product_b.ids)]})])

        model = self.env["dns.pos.promotion"].with_user(self._viewer(self.config_a))
        visible = model.search([])
        self.assertIn(on_a, visible)
        self.assertNotIn(on_b, visible)
        self.assertNotIn(draft_on_a, visible)
        with self.assertRaises(AccessError):
            model.promotion_products_workspace(on_b.id)
        self.assertEqual(model.workspace_detail(on_a.id)["stores"], [self.config_a.name])

    def test_viewer_sees_a_store_group_promotion_but_not_the_group(self):
        group = self._store_group(self.config_a | self.config_b)
        promotion = self._promotion(name="Grouped for viewer", pos_config_ids=[(5, 0, 0)], store_group_ids=[(6, 0, group.ids)])
        promotion.action_validate()

        model = self.env["dns.pos.promotion"].with_user(self._viewer(self.config_a))
        self.assertIn(promotion, model.search([]))
        detail = model.workspace_detail(promotion.id)
        self.assertEqual(detail["store_groups"], [])
        self.assertEqual(detail["stores"], [self.config_a.name], "tills outside Allowed POS stay hidden")

        options = model.workspace_options()
        self.assertFalse(options["is_manager"])
        self.assertEqual(options["store_groups"], [])
        with self.assertRaises(AccessError):
            model.store_groups_workspace()
        with self.assertRaises(AccessError):
            self.env["dns.promotion.report"].with_user(model.env.user).search([])

    # --- Promotion back office ----------------------------------------------------------------

    def _backoffice(self, configs):
        return self.env["res.users"].create({
            "name": "DNS Test Promotion Backoffice",
            "login": "dns_test_promotion_backoffice",
            "groups_id": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("dns_pos_promotions.group_dns_promotion_backoffice").id,
            ])],
            "pos_config_ids": [(6, 0, configs.ids)],
        })

    def _backoffice_payload(self, **overrides):
        payload = {
            "name": "Back office promotion",
            "offer_type": "product_percent",
            "percent_discount": 15,
            "date_start": fields.Datetime.to_string(self.start),
            "date_end": fields.Datetime.to_string(self.end),
            "timezone": "UTC",
            "pos_config_ids": self.config_a.ids,
            "selector": {"barcode_terms": self.product_b.barcode},
        }
        payload.update(overrides)
        return payload

    def test_backoffice_builds_and_validates_on_their_own_tills(self):
        model = self.env["dns.pos.promotion"].with_user(self._backoffice(self.config_a))
        options = model.workspace_options()
        self.assertTrue(options["can_build"])
        self.assertFalse(options["is_manager"])
        self.assertEqual(options["store_groups"], [])

        created = model.create_from_workspace(self._backoffice_payload())
        saved = model.update_from_workspace(created["id"], self._backoffice_payload(percent_discount=20), validate=True)
        self.assertIn(saved["state"], ("active", "scheduled"))
        self.assertTrue(model.workspace_detail(created["id"])["can_edit"])

    def test_backoffice_cannot_reach_beyond_their_tills(self):
        model = self.env["dns.pos.promotion"].with_user(self._backoffice(self.config_a))
        for payload in (
            self._backoffice_payload(pos_config_ids=(self.config_a | self.config_b).ids),
            self._backoffice_payload(pos_config_ids=[], all_stores=True),
            self._backoffice_payload(pos_config_ids=[], store_group_ids=self._store_group(self.config_a).ids),
        ):
            with self.assertRaises(AccessError):
                model.create_from_workspace(payload)

        own = model.create_from_workspace(self._backoffice_payload())
        with self.assertRaises(AccessError, msg="widening an own promotion is refused too"):
            model.update_from_workspace(own["id"], self._backoffice_payload(pos_config_ids=(self.config_a | self.config_b).ids))

    def test_backoffice_only_reads_network_promotions(self):
        network = self._promotion(name="Network", pos_config_ids=[(6, 0, (self.config_a | self.config_b).ids)])
        network.action_validate()
        model = self.env["dns.pos.promotion"].with_user(self._backoffice(self.config_a))

        self.assertIn(network, model.search([]))
        self.assertFalse(model.workspace_detail(network.id)["can_edit"])
        rows = model.search_workspace(query="Network")["records"]
        self.assertFalse(next(row for row in rows if row["id"] == network.id)["can_edit"])
        with self.assertRaises(AccessError):
            model.update_from_workspace(network.id, self._backoffice_payload())
        with self.assertRaises(AccessError):
            model.add_promotion_products(network.id, self.product_b.barcode)
        with self.assertRaises(AccessError):
            model.browse(network.id).action_validate()
        with self.assertRaises(AccessError):
            network.selector_ids.with_user(model.env.user).write({"name": "Hijacked"})
        self.assertIn(network.state, ("active", "scheduled"))

    def test_backoffice_has_no_store_groups_or_analysis(self):
        model = self.env["dns.pos.promotion"].with_user(self._backoffice(self.config_a))
        with self.assertRaises(AccessError):
            model.store_groups_workspace()
        with self.assertRaises(AccessError):
            model.save_store_group_workspace({"name": "Mine", "pos_config_ids": self.config_a.ids})
        with self.assertRaises(AccessError):
            self.env["dns.promotion.report"].with_user(model.env.user).search([])

    def test_backoffice_launch_still_conflicts_with_a_network_promotion(self):
        network = self._promotion(name="Network on A+B", pos_config_ids=[(6, 0, (self.config_a | self.config_b).ids)])
        network.action_validate()
        model = self.env["dns.pos.promotion"].with_user(self._backoffice(self.config_a))
        created = model.create_from_workspace(self._backoffice_payload(selector={"barcode_terms": self.product_a.barcode}))
        saved = model.update_from_workspace(created["id"], self._backoffice_payload(selector={"barcode_terms": self.product_a.barcode}), validate=True)
        self.assertEqual(saved["state"], "conflict")

    def test_ending_soon_tile_lists_what_it_counts(self):
        Promotion = self.env["dns.pos.promotion"]
        soon = self._promotion(name="EndingSoonTest soon")
        later = self._promotion(
            name="EndingSoonTest later", date_end=fields.Datetime.add(self.end, days=20),
            selector_ids=[(0, 0, {"name": "Products", "product_ids": [(6, 0, [self.product_b.id])]})],
        )
        self._promotion(name="EndingSoonTest draft")
        soon.action_validate()
        later.action_validate()
        self.assertEqual((soon.state, later.state), ("active", "active"))

        result = Promotion.search_workspace(query="EndingSoonTest", state="ending_soon")
        self.assertEqual([row["id"] for row in result["records"]], [soon.id])
        # The tile counts exactly what clicking it lists.
        self.assertEqual(
            Promotion.dashboard_data()["counts"]["ending_soon"],
            Promotion.search_count(Promotion._ending_soon_domain()),
        )
        self.assertIn(soon, Promotion.search(Promotion._ending_soon_domain()))
