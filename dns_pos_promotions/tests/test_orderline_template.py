import os

from lxml import etree
from lxml.builder import E

from odoo.modules.module import get_module_path
from odoo.tests import TransactionCase, tagged
from odoo.tools.template_inheritance import apply_inheritance_specs


@tagged("post_install", "-at_install")
class TestOrderlineTemplateInheritance(TransactionCase):
    """The POS orderline template is patched by xpath, and a bad xpath is expensive.

    Owl templates are stitched together when the POS asset bundle is built, not when the module
    installs, so a mismatched expression passes `-u` cleanly and only fails at the till. Worse, an
    over-broad expression silently *matches the wrong node*: `//li[.//t[@t-esc='line.discount']]`
    selects the outer orderline element, and replacing it wipes out the product name, the price,
    the notes and the lot lines. These tests do what the bundler does, in advance.
    """

    @classmethod
    def _template(cls, path, name):
        root = etree.parse(path).getroot()
        matches = [node for node in root.iter() if node.get("t-name") == name]
        assert matches, f"{name} not found in {path}"
        return matches[0]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        core = os.path.join(
            get_module_path("point_of_sale"),
            "static/src/app/generic_components/orderline/orderline.xml",
        )
        ours = os.path.join(
            get_module_path("dns_pos_promotions"), "static/src/pos/promotion_ui.xml"
        )
        parent = cls._template(core, "point_of_sale.Orderline")
        spec = cls._template(ours, "dns_pos_promotions.Orderline")
        # Raises if any xpath fails to match, exactly as the asset bundler would.
        cls.result = apply_inheritance_specs(parent, E.data(*list(spec)))
        cls.rendered = etree.tostring(cls.result).decode()

    def test_the_orderline_structure_survives(self):
        self.assertIn('class="orderline', self.rendered, "the outer orderline element")
        self.assertIn('class="info-list', self.rendered, "the detail list")
        self.assertIn("line.productName", self.rendered, "the product name")
        self.assertIn("line.customerNote", self.rendered, "the customer note")
        self.assertIn("line.internalNote", self.rendered, "the internal note")
        self.assertIn("line.attributes", self.rendered, "the variant attributes")
        self.assertIn('t-slot="default"', self.rendered, "the slot other modules extend through")

    def test_both_discount_variants_are_siblings_in_the_info_list(self):
        info_list = [
            node for node in self.result.iter("li")
            if node.getparent() is not None and "info-list" in (node.getparent().get("class") or "")
        ]
        conditions = [node.get("t-if") or "" for node in info_list]
        promo = [c for c in conditions if "dnsDiscountLabel" in c and "line.discount" not in c]
        manual = [c for c in conditions if "line.discount" in c and "!line.dnsDiscountLabel" in c]
        self.assertEqual(len(promo), 1, "one promotion discount line")
        self.assertEqual(len(manual), 1, "one untouched manual-discount line")

    def test_a_manual_discount_still_renders_the_core_way(self):
        # The two conditions must be mutually exclusive, or a promotion line prints twice.
        self.assertIn("!line.dnsDiscountLabel", self.rendered)

    def test_the_promotion_chip_is_still_injected(self):
        self.assertIn("dns-promotion-chip", self.rendered)
