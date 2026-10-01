/** @odoo-module **/

import { buildPromotionIndexes } from "@dns_pos_promotions/pos/promotion_engine";

QUnit.module("DNS POS promotion indexes");

QUnit.test("duplicate priorities never overwrite promotions", (assert) => {
    const promotions = Array.from({ length: 250 }, (_, index) => ({
        id: index + 1,
        priority: 100,
        product_ids: [42],
        coupon_code: false,
    }));
    const indexes = buildPromotionIndexes(promotions);
    assert.strictEqual(indexes.byId.size, 250);
    assert.strictEqual(indexes.byProduct.get(42).length, 250);
    assert.deepEqual([...indexes.byId.keys()].slice(0, 3), [1, 2, 3]);
});

QUnit.test("coupon lookup is normalized", (assert) => {
    const indexes = buildPromotionIndexes([{ id: 7, priority: 1, product_ids: [], coupon_code: "winter10" }]);
    assert.strictEqual(indexes.byCoupon.get("WINTER10").id, 7);
});

QUnit.test("an all-products offer carries its exclusions to the till", (assert) => {
    const promotion = {
        id: 9, priority: 1, offer_type: "product_percent", all_products: true,
        product_ids: [], coupon_code: false, excluded_product_ids: [11, 12],
    };
    const indexes = buildPromotionIndexes([promotion]);
    // No ID list to filter server-side, so the set has to survive onto the promotion itself.
    assert.deepEqual(indexes.orderWide, [promotion]);
    assert.ok(promotion.excludedProductIds.has(11));
    assert.notOk(promotion.excludedProductIds.has(13));
});

QUnit.test("a scope-backed offer indexes an empty exclusion set", (assert) => {
    const promotion = { id: 10, priority: 1, offer_type: "product_percent", product_ids: [5], coupon_code: false };
    buildPromotionIndexes([promotion]);
    assert.strictEqual(promotion.excludedProductIds.size, 0);
});

