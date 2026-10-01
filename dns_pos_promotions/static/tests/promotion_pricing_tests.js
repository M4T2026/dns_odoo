/** @odoo-module **/

import {
    allocateCoveredUnits,
    allocateTarget,
    discountPercentFor,
    missingRewardProductIds,
    shortfallForMinimum,
    untaxedTotal,
    planBundleSavings,
    planProportionalSavings,
} from "@dns_pos_promotions/pos/promotion_engine";

/** Sum of the savings a plan produces, rounded to cents like a real currency would be. */
function total(savings) {
    const sum = [...savings.values()].reduce(
        (running, value) => running + (typeof value === "number" ? value : value.saving),
        0
    );
    return Math.round(sum * 100) / 100;
}

/** Saving for one entry, rounded to cents. */
function saved(plan, id) {
    const value = plan.get(id);
    const amount = value === undefined ? 0 : typeof value === "number" ? value : value.saving;
    return Math.round(amount * 100) / 100;
}

function entry(id, qty, unitPrice) {
    return { id, qty, unitPrice };
}

QUnit.module("DNS POS bundle pricing");

QUnit.test("only whole bundles are discounted; the remainder keeps its price", (assert) => {
    // 7 units at 5.00, any 3 for 10.00 -> two bundles cover 6 units (30.00 -> 20.00),
    // the 7th unit stays at 5.00, so the order totals 25.00.
    const savings = planBundleSavings([entry("a", 7, 5)], 3, 10);
    assert.strictEqual(total(savings), 10, "saving is 30 - 20");
    assert.strictEqual(7 * 5 - total(savings), 25, "order total");
});

QUnit.test("a basket below the required quantity is not discounted", (assert) => {
    assert.strictEqual(planBundleSavings([entry("a", 2, 5)], 3, 10).size, 0);
});

QUnit.test("a bundle price above the shelf price never adds a surcharge", (assert) => {
    // Three units at 2.00 cost 6.00; a 10.00 bundle would be worse for the customer.
    assert.strictEqual(planBundleSavings([entry("a", 3, 2)], 3, 10).size, 0);
});

QUnit.test("quantity pools across lines of the same pool", (assert) => {
    // Two lines of the same product, 2 + 1 units, make one bundle of 3.
    const savings = planBundleSavings([entry("a", 2, 5), entry("b", 1, 5)], 3, 10);
    assert.strictEqual(total(savings), 5, "15.00 of goods priced at 10.00");
    assert.strictEqual(savings.size, 2, "both lines carry part of the saving");
});

QUnit.test("the most expensive units go into the bundle", (assert) => {
    // "Any 3 for 20" over 2 x 15.00 and 2 x 5.00. The bundle should take both 15.00 units and
    // one 5.00 unit (35.00 -> 20.00 = 15.00 saved), leaving the cheaper unit at full price.
    const savings = planBundleSavings([entry("cheap", 2, 5), entry("dear", 2, 15)], 3, 20);
    assert.strictEqual(total(savings), 15);

    // The dear line contributed 30 of the 35 covered, so it takes 30/35 of the saving.
    assert.strictEqual(saved(savings, "dear"), 12.86);
    assert.strictEqual(saved(savings, "cheap"), 2.14);
});

QUnit.test("only the covered quantity is reported, so the rest can stay at full price", (assert) => {
    // "Any 3 for $5" with five $3 items on one merged line: three units are covered, two are not.
    // Reporting coveredQty is what lets the engine split the line instead of blending one
    // discount percentage across all five units.
    const plan = planBundleSavings([entry("a", 5, 3)], 3, 5);
    assert.strictEqual(plan.get("a").coveredQty, 3, "three units covered");
    assert.strictEqual(total(plan), 4, "9.00 of goods priced at 5.00");

    const coveredTotal = 3 * 3 - total(plan);
    const remainderTotal = 2 * 3;
    assert.strictEqual(coveredTotal, 5, "the bundle costs exactly the bundle price");
    assert.strictEqual(remainderTotal, 6, "the two spare units stay at full price");
    assert.strictEqual(coveredTotal + remainderTotal, 11, "order total");
});

QUnit.test("a sixth item forms a second bundle rather than deepening the discount", (assert) => {
    const five = planBundleSavings([entry("a", 5, 3)], 3, 5);
    const six = planBundleSavings([entry("a", 6, 3)], 3, 5);
    assert.strictEqual(five.get("a").coveredQty, 3, "five items: one bundle");
    assert.strictEqual(six.get("a").coveredQty, 6, "six items: two bundles");
    assert.strictEqual(total(five), 4);
    assert.strictEqual(total(six), 8, "the saving doubles only at the sixth item");
    assert.strictEqual(6 * 3 - total(six), 10, "two bundles cost 10.00");
});

QUnit.test("allocation stops once the covered quantity is met", (assert) => {
    const allocation = allocateCoveredUnits([entry("a", 5, 10), entry("b", 5, 1)], 3);
    assert.strictEqual(allocation.length, 1, "the dearer line alone covers it");
    assert.deepEqual(
        allocation.map((item) => [item.id, item.qty, item.base]),
        [["a", 3, 30]]
    );
});

QUnit.test("fractional quantities allocate without expanding into units", (assert) => {
    const savings = planBundleSavings([entry("a", 1.5, 10), entry("b", 1.5, 10)], 3, 20);
    assert.strictEqual(total(savings), 10, "30.00 of goods priced at 20.00");
});

QUnit.module("DNS POS proportional discounts");

QUnit.test("a fixed discount splits by value contributed", (assert) => {
    const savings = planProportionalSavings([entry("a", 1, 30), entry("b", 1, 10)], 8);
    assert.strictEqual(total(savings), 8);
    assert.strictEqual(savings.get("a"), 6, "75% of the basket value");
    assert.strictEqual(savings.get("b"), 2, "25% of the basket value");
});

QUnit.test("a fixed discount never exceeds the basket", (assert) => {
    const savings = planProportionalSavings([entry("a", 1, 10)], 500);
    assert.strictEqual(total(savings), 10, "capped at the line value, never negative");
});

QUnit.test("an empty or zero-value basket produces no savings", (assert) => {
    assert.strictEqual(planProportionalSavings([], 10).size, 0);
    assert.strictEqual(planProportionalSavings([entry("a", 1, 0)], 10).size, 0);
    assert.strictEqual(planProportionalSavings([entry("a", 1, 10)], 0).size, 0);
});

QUnit.module("DNS POS single_bundle versus mix_match");

QUnit.test("pooling per product and pooling across the set differ for the same basket", (assert) => {
    // Basket: 2 x product A at 5.00, 2 x product B at 5.00. Offer: any 3 for 10.00.
    const lineA = entry("a", 2, 5);
    const lineB = entry("b", 2, 5);

    // single_bundle pools per product: neither product reaches 3, so nothing is discounted.
    const singleA = planBundleSavings([lineA], 3, 10);
    const singleB = planBundleSavings([lineB], 3, 10);
    assert.strictEqual(total(singleA) + total(singleB), 0, "no single product reaches the quantity");

    // mix_match pools across the set: 4 units make one bundle of 3.
    const mixed = planBundleSavings([lineA, lineB], 3, 10);
    assert.strictEqual(total(mixed), 5, "15.00 of goods priced at 10.00");
});

QUnit.module("DNS POS bundle total is cent-exact");

/** Odoo rounds each line separately, so the allocated shares must sum to the target exactly. */
function bundleTotalFor(prices, target) {
    const rows = prices.map((price) => ({ base: price }));
    const nets = allocateTarget(rows, target);
    // Each line is rounded independently by Odoo; sum what the register would actually charge.
    return Math.round(nets.reduce((sum, net) => sum + Math.round(net * 100) / 100, 0) * 100) / 100;
}

QUnit.test("3 for $5 rings up as exactly $5.00, never $4.99", (assert) => {
    // Prices that previously drifted a cent once each line rounded on its own.
    for (const prices of [
        [2.99, 1.50, 3.00], [2.99, 2.99, 2.99], [1.99, 1.99, 1.99],
        [3.33, 3.33, 3.33], [0.99, 0.99, 7.99], [2.50, 2.50, 2.51],
        [1.11, 2.22, 3.33], [4.95, 0.05, 0.05], [2.99, 2.99, 3.01],
    ]) {
        assert.strictEqual(bundleTotalFor(prices, 5), 5, `3 for $5 over ${prices.join("/")}`);
    }
});

QUnit.test("the residual cent lands on the largest line", (assert) => {
    const rows = [{ base: 1 }, { base: 8 }, { base: 1 }];
    const nets = allocateTarget(rows, 5);
    assert.strictEqual(Math.round(nets.reduce((a, b) => a + b, 0) * 100) / 100, 5, "total is exact");
    assert.ok(nets[1] > nets[0] && nets[1] > nets[2], "largest row absorbs the rounding");
});

QUnit.test("a single covered line takes the whole target", (assert) => {
    assert.deepEqual(allocateTarget([{ base: 8.97 }], 5), [5]);
});

QUnit.test("a valueless pool allocates nothing", (assert) => {
    assert.deepEqual(allocateTarget([{ base: 0 }, { base: 0 }], 5), [0, 0]);
});

QUnit.module("DNS POS discount percentage");

/** What Odoo will actually bill for the line, given the percentage we write on it. */
function billed(base, percent, rounding = 0.01) {
    return Math.round((base * (1 - percent / 100)) / rounding) * rounding;
}

QUnit.test("the printed percentage is short enough to read", (assert) => {
    // 5 units at 4.99 with $10 off used to print "40.080160320641276% discount" on the receipt.
    assert.strictEqual(discountPercentFor(4.99 * 5, 10), 40.08);
    assert.strictEqual(discountPercentFor(3, 1), 33.33, "a repeating ratio still terminates");
    assert.strictEqual(discountPercentFor(19.99, 1.999), 10, "a clean 10% stays 10, not 10.000000002");
});

QUnit.test("shortening the percentage never changes what the customer pays", (assert) => {
    const cases = [
        [4.99 * 5, 10], [2.5 * 3, 2.5], [19.99, 1.999], [999.99, 0.01],
        [3, 1], [30.03, 10], [13.13, 7], [12.34, 12.34],
    ];
    for (const [base, saving] of cases) {
        const percent = discountPercentFor(base, saving);
        assert.strictEqual(
            billed(base, percent).toFixed(2),
            (Math.round((base - saving) * 100) / 100).toFixed(2),
            `${saving} off ${base} bills exactly`
        );
    }
});

QUnit.test("a tiny saving on a large line widens the precision rather than vanishing", (assert) => {
    // At two decimals this rounds to 0% and the cent would be silently lost.
    assert.strictEqual(discountPercentFor(999.99, 0.01), 0.001);
    assert.strictEqual(billed(999.99, 0.001).toFixed(2), "999.98");
});

QUnit.test("a fully discounted line reads as 100%", (assert) => {
    assert.strictEqual(discountPercentFor(12.34, 12.34), 100);
});

QUnit.module("DNS POS reward product loading");

/** Stand-in for pos.db: only these ids are in the till's product database. */
function loadedIds(...ids) {
    const set = new Set(ids);
    return (id) => (set.has(id) ? { id } : undefined);
}

QUnit.test("a giveaway the till never loaded is requested", (assert) => {
    // The catalogue is truncated at the till, and a reward product is never scanned, so nothing
    // else would pull it in - this is what stopped Buy X Get Y working at all.
    const promotions = [{ id: 1, reward_product_id: 133195 }];
    assert.deepEqual(missingRewardProductIds(promotions, loadedIds(7009)), [133195]);
});

QUnit.test("a giveaway already in the till is not re-fetched", (assert) => {
    const promotions = [{ id: 1, reward_product_id: 7009 }];
    assert.deepEqual(missingRewardProductIds(promotions, loadedIds(7009)), []);
});

QUnit.test("promotions sharing one giveaway ask for it once", (assert) => {
    const promotions = [
        { id: 1, reward_product_id: 133195 },
        { id: 2, reward_product_id: 133195 },
        { id: 3, reward_product_id: 99306 },
    ];
    assert.deepEqual(missingRewardProductIds(promotions, loadedIds()), [133195, 99306]);
});

QUnit.test("offers with no giveaway are ignored", (assert) => {
    const promotions = [
        { id: 1, reward_product_id: false },
        { id: 2 },
        { id: 3, reward_product_id: 0 },
    ];
    assert.deepEqual(missingRewardProductIds(promotions, loadedIds()), []);
    assert.deepEqual(missingRewardProductIds(undefined, loadedIds()), [], "no payload at all");
});

QUnit.module("DNS POS coupon minimum spend");

QUnit.test("a basket below the minimum reports what is still needed", (assert) => {
    // The code is accepted but dormant; the cashier is told the shortfall rather than being
    // shown "applied" over an order where nothing changed.
    assert.strictEqual(shortfallForMinimum(10, 20), 10);
    assert.strictEqual(shortfallForMinimum(19.99, 20), 0.01);
});

QUnit.test("a qualifying basket has no shortfall", (assert) => {
    assert.strictEqual(shortfallForMinimum(20, 20), 0, "exactly on the threshold qualifies");
    assert.strictEqual(shortfallForMinimum(25, 20), 0);
});

QUnit.test("no minimum means it always qualifies", (assert) => {
    assert.strictEqual(shortfallForMinimum(0, 0), 0);
    assert.strictEqual(shortfallForMinimum(5, undefined), 0);
});

QUnit.test("the basket total is measured at shelf price before discount", (assert) => {
    // Same expression the pricing loop uses for the minimum test, so the popup and the engine
    // cannot disagree about whether an order qualifies.
    const lines = [
        { product: { lst_price: 4.99 }, quantity: 2 },
        { product: { lst_price: 10 }, quantity: 1 },
    ];
    assert.strictEqual(untaxedTotal(lines), 19.98);
    assert.strictEqual(untaxedTotal([]), 0);
});
