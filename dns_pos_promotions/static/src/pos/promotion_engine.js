/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { Order, Orderline } from "@point_of_sale/app/store/models";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { Component } from "@odoo/owl";
import { TextInputPopup } from "@point_of_sale/app/utils/input_popups/text_input_popup";

const PROMOTION_FIELDS = [
    "dns_promotion_id", "dns_promotion_name", "dns_promotion_rule", "dns_base_price",
    "dns_discount_allocation", "dns_saved_amount", "dns_promotion_snapshot",
];

function nowUtcComparable() {
    return luxon.DateTime.utc();
}

function isCurrentlyValid(promotion) {
    const now = nowUtcComparable();
    return now >= luxon.DateTime.fromSQL(promotion.date_start, { zone: "utc" }) &&
        now < luxon.DateTime.fromSQL(promotion.date_end, { zone: "utc" });
}

const ORDER_WIDE_TYPES = new Set(["order_percent", "order_fixed", "coupon"]);

/*
 * The pricing arithmetic below is pure: it works on plain `{ id, qty, unitPrice }` entries rather
 * than Orderline objects, so it can be unit-tested without a POS environment. The Order methods
 * map lines onto these entries and turn the resulting savings into line discounts.
 *
 * Two invariants hold throughout:
 *   1. A promotion only ever sets a line *discount*. It never writes the unit price, so a
 *      pricelist price or a price the cashier set by hand survives a promotion being applied
 *      and then removed.
 *   2. Bundles cover whole sets only. Uncovered units are moved to their own full-price line
 *      rather than sharing a blended discount percentage with the covered ones.
 */

/**
 * Allocate `coveredQty` units across entries, most expensive first, so a mixed-price bundle
 * resolves in the customer's favour.
 *
 * @returns {Array<{id: *, qty: number, base: number}>}
 */
export function allocateCoveredUnits(entries, coveredQty) {
    const allocation = [];
    let remaining = coveredQty;
    for (const entry of [...entries].sort((a, b) => b.unitPrice - a.unitPrice)) {
        if (remaining <= 0) {
            break;
        }
        const qty = Math.min(remaining, entry.qty);
        if (qty <= 0) {
            continue;
        }
        allocation.push({ id: entry.id, qty, base: qty * entry.unitPrice });
        remaining -= qty;
    }
    return allocation;
}

/**
 * Spread one absolute discount across entries in proportion to what each contributes.
 *
 * @returns {Map<*, number>} saving per entry id
 */
export function planProportionalSavings(entries, totalDiscount) {
    const savings = new Map();
    const baseTotal = entries.reduce((sum, entry) => sum + entry.qty * entry.unitPrice, 0);
    if (baseTotal <= 0 || totalDiscount <= 0) {
        return savings;
    }
    const discount = Math.min(totalDiscount, baseTotal);
    for (const entry of entries) {
        const share = (entry.qty * entry.unitPrice) / baseTotal;
        savings.set(entry.id, (savings.get(entry.id) || 0) + discount * share);
    }
    return savings;
}

/**
 * Price a pool at `bundleTotal` per `requiredQty` units. Whole bundles only.
 * Reports covered quantity so the caller can split partly-covered lines.
 *
 * @returns {Map<*, {saving: number, coveredQty: number}>}
 */
export function planBundleSavings(entries, requiredQty, bundleTotal) {
    const plan = new Map();
    if (!(requiredQty > 0) || !(bundleTotal > 0)) {
        return plan;
    }
    const totalQty = entries.reduce((sum, entry) => sum + entry.qty, 0);
    const bundles = Math.floor(totalQty / requiredQty);
    if (bundles < 1) {
        return plan;
    }
    return planFromAllocation(allocateCoveredUnits(entries, bundles * requiredQty), bundleTotal * bundles);
}

/**
 * Split `target` across rows in proportion to their base, in whole currency units.
 *
 * Odoo rounds every order line independently (`base = round_pr(price_unit * quantity)`), so a
 * bundle spread over three lines can each land a fraction of a cent out and ring up as $4.99
 * instead of $5.00. Rounding each share and handing the residual to the largest row makes the
 * total exact.
 *
 * @param {Array<{base: number}>} rows
 * @returns {number[]} net amount per row, summing exactly to `target`
 */
export function allocateTarget(rows, target, rounding = 0.01) {
    const round = (value) => Math.round(value / rounding) * rounding;
    const coveredBase = rows.reduce((sum, row) => sum + row.base, 0);
    if (coveredBase <= 0) {
        return rows.map(() => 0);
    }
    const nets = rows.map((row) => round(target * (row.base / coveredBase)));
    const residual = round(target - nets.reduce((sum, value) => sum + value, 0));
    if (residual) {
        let largest = 0;
        for (let i = 1; i < rows.length; i++) {
            if (rows[i].base > rows[largest].base) largest = i;
        }
        nets[largest] = round(nets[largest] + residual);
    }
    return nets;
}

/** Turn a covered-unit allocation plus a target price into per-entry savings. */
export function planFromAllocation(allocation, targetTotal) {
    const plan = new Map();
    const coveredBase = allocation.reduce((sum, entry) => sum + entry.base, 0);
    const saving = coveredBase - targetTotal;
    if (saving <= 0) {
        return plan;
    }
    for (const entry of allocation) {
        const current = plan.get(entry.id) || { saving: 0, coveredQty: 0 };
        current.saving += saving * (entry.base / coveredBase);
        current.coveredQty += entry.qty;
        plan.set(entry.id, current);
    }
    return plan;
}

/**
 * The percentage to write on a line: the coarsest precision that still bills the exact
 * cent-rounded saving.
 *
 * Odoo prints `line.discount` verbatim on the product screen and on the receipt, so handing it a
 * raw ratio shows the customer "40.080160320641276% discount". Rounding blindly would break the
 * invariant the fixed and bundle paths work to establish - that $10 off takes exactly $10 off -
 * so widen the precision only until the money lines up again. Two decimals covers almost every
 * case; an awkward base occasionally needs three or four.
 */
export function discountPercentFor(base, saving, rounding = 0.01) {
    // Compare in whole currency units: rounding to a float leaves dust that breaks ===.
    const units = (value) => Math.round(value / rounding);
    const target = units(base - saving);
    const exact = (saving / base) * 100;
    for (let decimals = 2; decimals <= 8; decimals += 1) {
        const candidate = Number(exact.toFixed(decimals));
        if (units(base * (1 - candidate / 100)) === target) return candidate;
    }
    return exact;
}

/**
 * Basket value an order-wide offer is measured against, at shelf price before any discount.
 *
 * Shared by the pricing loop and the promo-code popup: if the popup measured the shortfall
 * differently it could accept a code the engine then refuses to apply, or vice versa.
 */
export function untaxedTotal(lines) {
    return lines.reduce((sum, line) => sum + line.product.lst_price * line.quantity, 0);
}

/**
 * How much more the customer must spend before an order-wide offer takes effect. 0 means it
 * already qualifies.
 */
export function shortfallForMinimum(basketTotal, minimumOrderAmount) {
    const minimum = minimumOrderAmount || 0;
    return basketTotal >= minimum ? 0 : minimum - basketTotal;
}

/**
 * Reward product ids a payload needs that the till does not have.
 *
 * `isLoaded` takes a product id and returns the POS record, or undefined. Deduped, because
 * several promotions can share one giveaway.
 */
export function missingRewardProductIds(promotions, isLoaded) {
    const wanted = new Set();
    for (const promotion of promotions || []) {
        const id = promotion.reward_product_id;
        if (id && !isLoaded(id)) wanted.add(id);
    }
    return [...wanted];
}

export function buildPromotionIndexes(promotions) {
    const byId = new Map();
    const byProduct = new Map();
    const byCoupon = new Map();
    const orderWide = [];
    for (const promotion of [...promotions].sort((a, b) => a.priority - b.priority || a.id - b.id)) {
        byId.set(promotion.id, promotion);
        // An all-products offer ships no ID list, so its exclusions are the only thing standing
        // between "every product" and the SKUs an operator pulled out. Scope-backed offers had
        // theirs filtered out server-side and arrive with an empty list.
        promotion.excludedProductIds = new Set(promotion.excluded_product_ids || []);
        if (promotion.coupon_code) byCoupon.set(promotion.coupon_code.toUpperCase(), promotion);
        // Order-wide offers and all-products offers match every line without an ID list (R1/R2/R7).
        if (ORDER_WIDE_TYPES.has(promotion.offer_type) || promotion.all_products) {
            orderWide.push(promotion);
            continue;
        }
        for (const productId of promotion.product_ids || []) {
            const current = byProduct.get(productId) || [];
            current.push(promotion);
            byProduct.set(productId, current);
        }
    }
    return { byId, byProduct, byCoupon, orderWide };
}

patch(PosStore.prototype, {
    async _processData(loadedData) {
        await super._processData(...arguments);
        this.dnsLoadPromotions(loadedData["dns.pos.promotion"] || []);
        this.dnsSetupPromotionBus();
    },

    dnsLoadPromotions(promotions) {
        const indexes = buildPromotionIndexes(promotions);
        this.dnsPromotionsById = indexes.byId;
        this.dnsPromotionsByProduct = indexes.byProduct;
        this.dnsPromotionsByCoupon = indexes.byCoupon;
        this.dnsPromotionsOrderWide = indexes.orderWide;
        this.dnsPromotionVersion = Math.max(0, ...promotions.map((p) => p.version || 0));
    },

    dnsSetupPromotionBus() {
        if (this.dnsPromotionBusReady || !this.config?.id) return;
        this.dnsPromotionBusReady = true;
        const bus = this.env.services.bus_service;
        bus.addChannel(`dns_pos_promotions:${this.config.id}`);
        bus.addEventListener("notification", ({ detail }) => {
            for (const notification of detail) {
                if (notification.type !== "dns_pos_promotion_refresh") continue;
                const order = this.get_order();
                if (!order || !order.get_orderlines().length) {
                    this.dnsRefreshPromotionPayload();
                } else {
                    this.dnsPromotionRefreshPending = true;
                    this.env.services.notification.add(
                        _t("Promotion changes are ready and will load with the next order."),
                        { type: "info", sticky: true }
                    );
                }
            }
        });
    },

    async dnsRefreshPromotionPayload() {
        const payload = await this.env.services.orm.call("pos.session", "get_dns_promotion_payload", [[this.pos_session.id]]);
        await this.dnsEnsureRewardProducts(payload);
        this.dnsLoadPromotions(payload);
        this.dnsPromotionRefreshPending = false;
        this.get_order()?.dnsRecomputePromotions();
    },

    /**
     * Pull in any giveaway product this session has never loaded.
     *
     * The session load ships reward products alongside the catalogue, but a promotion validated
     * while the till is open arrives over the bus instead - so its reward product can still be
     * absent. Done here, awaited, because dnsRecomputePromotions is synchronous: fetching from
     * inside the pricing path would miss the first pass and the free line would show up late.
     */
    async dnsEnsureRewardProducts(promotions) {
        const missing = missingRewardProductIds(promotions, (id) => this.db.get_product_by_id(id));
        if (!missing.length) return;
        // setAvailable false: never write available_in_pos back onto the product.
        await this._addProducts(missing, false);
    },

    add_new_order() {
        const order = super.add_new_order(...arguments);
        if (this.dnsPromotionRefreshPending) this.dnsRefreshPromotionPayload();
        return order;
    },
});

patch(Orderline.prototype, {
    setup() {
        super.setup(...arguments);
        const json = arguments[1]?.json || {};
        for (const field of PROMOTION_FIELDS) this[field] = json[field] || false;
        this.dns_is_reward_line = json.dns_is_reward_line || false;
        this.dns_split_remainder = json.dns_split_remainder || false;
    },

    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        for (const field of PROMOTION_FIELDS) json[field] = this[field] || false;
        json.dns_is_reward_line = this.dns_is_reward_line || false;
        // Client-side only; the server drops unknown keys. Persisted so a reloaded draft order
        // can fold its split lines back together before repricing.
        json.dns_split_remainder = this.dns_split_remainder || false;
        return json;
    },

    getDisplayData() {
        return {
            ...super.getDisplayData(...arguments),
            dnsPromotionName: this.dns_promotion_name || false,
            dnsSavedAmount: this.dns_saved_amount || 0,
            dnsPromotionLocked: this.dnsIsPromotionLocked(),
            dnsDiscountLabel: this.dnsDiscountLabel(),
        };
    },

    /**
     * How the discount reads on the line, replacing core's "With a <pct>% discount".
     *
     * A percentage offer states its own headline rate, so show that and drop the decimals: the
     * cashier set up "10% off", not "10.08% off". Every other kind of promotion is an amount
     * rather than a rate - the percentage is only how the saving gets expressed to Odoo - so a
     * bundle or a fixed discount reads as the money saved instead of a ratio nobody chose.
     */
    dnsDiscountLabel() {
        if (!this.dns_promotion_id || this.dnsIsRefundLine()) return false;
        const promotion = this.pos.dnsPromotionsById?.get(this.dns_promotion_id);
        if (this.dns_promotion_rule === "percentage") {
            const percent = promotion?.percent_discount ?? this.get_discount();
            return `${Math.round(percent)}% off`;
        }
        if (!this.dns_saved_amount) return false;
        return `${this.env.utils.formatCurrency(this.dns_saved_amount)} off`;
    },

    /** Unit price before any promotional discount — the pricelist price, not the list price. */
    dnsUnitPrice() {
        return this.get_unit_price();
    },

    /**
     * A refund line carries the pricing of the original sale and is never re-evaluated:
     * the promotion behind it may have ended or changed since.
     */
    dnsIsRefundLine() {
        return Boolean(this.refunded_orderline_id);
    },

    /** A line priced by a promotion may not be edited by hand. */
    dnsIsPromotionLocked() {
        return Boolean(this.dns_promotion_id) && !this.dnsIsRefundLine();
    },

    /** True when the cashier deliberately overrode price or discount on this line. */
    dnsIsManuallyPriced() {
        if (this.dns_promotion_id || this.dns_is_reward_line) {
            return false;
        }
        return this.price_type === "manual" || this.get_discount() > 0;
    },

    set_quantity(quantity, keepPrice) {
        const result = super.set_quantity(...arguments);
        if (!this.order?.dnsPromotionUpdating) this.order?.dnsSchedulePromotionRecompute();
        return result;
    },
});

patch(Order.prototype, {
    setup() {
        super.setup(...arguments);
        this.dnsAppliedCoupon = this.dnsAppliedCoupon || false;
        this.dnsPromotionUpdating = false;
        this.dnsPromotionTimer = null;
    },

    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.dnsAppliedCoupon = this.dnsAppliedCoupon || false;
        return json;
    },

    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.dnsAppliedCoupon = json.dnsAppliedCoupon || false;
    },

    add_product(product, options) {
        const result = super.add_product(...arguments);
        if (!this.dnsPromotionUpdating) this.dnsSchedulePromotionRecompute();
        return result;
    },

    removeOrderline(line) {
        const result = super.removeOrderline(...arguments);
        if (!this.dnsPromotionUpdating) this.dnsSchedulePromotionRecompute();
        return result;
    },

    // Kept for call-site compatibility, but repricing is synchronous: the previous 250ms timer
    // let a cashier reach the payment screen before the discount had been applied.
    dnsSchedulePromotionRecompute() {
        this.dnsRecomputePromotions();
    },

    /** Lines a promotion may price. Refunds, reward lines and hand-priced lines are excluded. */
    dnsNormalLines() {
        return this.get_orderlines().filter(
            (line) => !line.dns_is_reward_line && !line.dnsIsRefundLine() &&
                !line.dnsIsManuallyPriced() && line.get_quantity() > 0
        );
    },

    dnsClearPromotionState() {
        for (const line of [...this.get_orderlines()]) {
            // Refund lines keep the promotion data of the sale they reverse.
            if (line.dnsIsRefundLine()) continue;
            if (line.dns_is_reward_line) {
                this.removeOrderline(line);
                continue;
            }
            if (line.dns_promotion_id) line.set_discount(0);
            for (const field of PROMOTION_FIELDS) line[field] = false;
        }
    },

    /**
     * Fold previously split remainders back into their parent line, so every recompute starts
     * from the raw basket instead of splitting an already-split line.
     */
    dnsConsolidateSplitLines() {
        for (const remainder of this.get_orderlines()) {
            if (!remainder.dns_split_remainder || remainder.dnsIsRefundLine()) continue;
            const parent = this.get_orderlines().find(
                (line) => line !== remainder && !line.dns_split_remainder && !line.dnsIsRefundLine() &&
                    !line.dns_is_reward_line && line.get_product().id === remainder.get_product().id &&
                    line.dnsUnitPrice() === remainder.dnsUnitPrice()
            );
            if (parent) {
                parent.set_quantity(parent.get_quantity() + remainder.get_quantity());
                this.removeOrderline(remainder);
            } else {
                remainder.dns_split_remainder = false;
            }
        }
    },

    /** Move the uncovered part of a line onto its own full-price line. */
    dnsSplitCoveredQuantity(line, coveredQty) {
        const remainder = line.get_quantity() - coveredQty;
        // Quantities can be fractional, so compare with a tolerance.
        if (remainder <= 0.00001) return null;
        const remainderLine = new Orderline(
            { env: this.env },
            { pos: this.pos, order: this, product: line.get_product() }
        );
        remainderLine.set_quantity(remainder);
        remainderLine.set_unit_price(line.dnsUnitPrice());
        remainderLine.price_type = line.price_type;
        remainderLine.set_full_product_name();
        remainderLine.dns_split_remainder = true;
        line.set_quantity(coveredQty);
        this.orderlines.add(remainderLine);
        return remainderLine;
    },

    dnsMarkLine(line, promotion, basePrice, saved, rule) {
        line.dns_promotion_id = promotion.id;
        line.dns_promotion_name = promotion.name;
        line.dns_promotion_rule = rule || promotion.offer_type;
        line.dns_base_price = basePrice;
        line.dns_discount_allocation = saved;
        line.dns_saved_amount = saved;
        line.dns_promotion_snapshot = {
            id: promotion.id, name: promotion.name, type: promotion.offer_type,
            version: promotion.version, date_start: promotion.date_start, date_end: promotion.date_end,
        };
    },

    /** Map order lines onto the plain entries the pricing functions work with. */
    dnsLineEntries(lines) {
        return lines.map((line) => ({ id: line, qty: line.get_quantity(), unitPrice: line.dnsUnitPrice() }));
    },

    /**
     * Express a saving as a line discount. Promotions never write the unit price, so a pricelist
     * or manually set price survives a promotion being applied and then removed.
     */
    dnsApplySaving(promotion, line, saving, rule) {
        const base = line.dnsUnitPrice() * line.get_quantity();
        if (base <= 0 || saving <= 0) return false;
        const capped = Math.min(saving, base);
        line.set_discount(this.dnsDiscountPercent(base, capped), { fromPromo: true });
        this.dnsMarkLine(line, promotion, line.dnsUnitPrice(), capped, rule);
        return true;
    },

    dnsDiscountPercent(base, saving) {
        return discountPercentFor(base, saving, this.pos.currency?.rounding || 0.01);
    },

    dnsApplyPercent(promotion, lines, percent = promotion.percent_discount) {
        let applied = false;
        for (const line of lines) {
            const base = line.dnsUnitPrice() * line.get_quantity();
            applied = this.dnsApplySaving(promotion, line, (base * percent) / 100, "percentage") || applied;
        }
        return applied;
    },

    dnsApplyFixed(promotion, lines, totalDiscount = promotion.fixed_discount, rule = "fixed") {
        const savings = planProportionalSavings(this.dnsLineEntries(lines), totalDiscount);
        if (!savings.size) return false;
        // Quantize to cents and hand the residual to the largest line, so a $5 discount spread
        // over several lines takes exactly $5 off the order.
        const rows = [...savings].map(([line, saving]) => ({ line, saving: this.dnsCurrencyRound(saving) }));
        const wanted = this.dnsCurrencyRound([...savings.values()].reduce((sum, value) => sum + value, 0));
        const residual = this.dnsCurrencyRound(wanted - rows.reduce((sum, row) => sum + row.saving, 0));
        if (residual) {
            rows.sort((a, b) => b.saving - a.saving);
            rows[0].saving = this.dnsCurrencyRound(rows[0].saving + residual);
        }
        let applied = false;
        for (const row of rows) {
            applied = this.dnsApplySaving(promotion, row.line, row.saving, rule) || applied;
        }
        return applied;
    },

    /** Round to the register's currency, e.g. to the nearest cent. */
    dnsCurrencyRound(value) {
        const rounding = this.pos.currency?.rounding || 0.01;
        return Math.round(value / rounding) * rounding;
    },

    /**
     * Apply a covered-unit plan, splitting any line the bundles only partly cover.
     *
     * The bundle total is allocated in whole cents rather than as a raw percentage. Odoo rounds
     * every line independently (`base = round_pr(price_unit * quantity)`), so three lines each
     * off by a fraction of a cent leave "3 for $5" ringing up as $4.99. Allocating the target and
     * handing the residual cent to the largest line makes the bundle total exact.
     */
    dnsApplyPlan(promotion, plan, rule) {
        const rows = [];
        let coveredBase = 0;
        let totalSaving = 0;
        for (const [line, { saving, coveredQty }] of plan) {
            this.dnsSplitCoveredQuantity(line, coveredQty);
            const base = this.dnsCurrencyRound(line.dnsUnitPrice() * line.get_quantity());
            rows.push({ line, base });
            coveredBase += base;
            totalSaving += saving;
        }
        if (!rows.length || coveredBase <= 0) return false;

        const target = this.dnsCurrencyRound(coveredBase - totalSaving);
        const nets = allocateTarget(rows, target, this.pos.currency?.rounding || 0.01);

        let applied = false;
        rows.forEach((row, index) => {
            applied = this.dnsApplySaving(promotion, row.line, row.base - nets[index], rule) || applied;
        });
        return applied;
    },

    dnsApplyBundle(promotion, lines) {
        // single_bundle pools quantity per product; mix_match pools across the whole eligible set.
        const bundleTotal = promotion.bundle_total > 0
            ? promotion.bundle_total
            : promotion.bundle_unit_price * promotion.required_qty;
        if (promotion.offer_type !== "single_bundle") {
            return this.dnsApplyPlan(
                promotion,
                planBundleSavings(this.dnsLineEntries(lines), promotion.required_qty, bundleTotal),
                "mix_match"
            );
        }
        const byProduct = new Map();
        for (const line of lines) {
            const key = line.get_product().id;
            byProduct.has(key) ? byProduct.get(key).push(line) : byProduct.set(key, [line]);
        }
        let applied = false;
        for (const productLines of byProduct.values()) {
            const plan = planBundleSavings(this.dnsLineEntries(productLines), promotion.required_qty, bundleTotal);
            applied = this.dnsApplyPlan(promotion, plan, "single_bundle") || applied;
        }
        return applied;
    },

    dnsApplyMultiGroup(promotion, lines) {
        // Every group must contribute its required quantity for a bundle to form.
        const groupStats = promotion.groups.map((group) => {
            const eligible = lines.filter((line) => group.product_ids.includes(line.get_product().id));
            return { group, eligible, qty: eligible.reduce((sum, line) => sum + line.get_quantity(), 0) };
        });
        if (!groupStats.length) return false;
        const bundles = Math.min(...groupStats.map((item) => Math.floor(item.qty / item.group.required_qty)));
        if (!Number.isFinite(bundles) || bundles <= 0) return false;
        // Take the covered units group by group so each group contributes exactly its share.
        const allocation = groupStats.flatMap((item) =>
            allocateCoveredUnits(this.dnsLineEntries(item.eligible), item.group.required_qty * bundles)
        );
        return this.dnsApplyPlan(promotion, planFromAllocation(allocation, promotion.bundle_total * bundles), "multi_group");
    },

    dnsApplyBuyXGetY(promotion, lines) {
        const quantity = lines.reduce((sum, line) => sum + line.get_quantity(), 0);
        const rewards = Math.floor(quantity / promotion.required_qty) * promotion.reward_qty;
        if (rewards <= 0) return false;
        const product = this.pos.db.get_product_by_id(promotion.reward_product_id);
        if (!product) {
            // Should be unreachable: the session load ships reward products and the bus refresh
            // tops them up. Say so loudly rather than returning false - an offer that quietly
            // does nothing at the till is near-impossible to diagnose from the shop floor.
            console.warn(
                `[dns_pos_promotions] "${promotion.name}" (id ${promotion.id}) cannot give away ` +
                `product ${promotion.reward_product_id}: it is not loaded in this session. ` +
                `No free line will be added.`
            );
            return false;
        }
        // Built synchronously: core add_product is async, so assigning to its return value would
        // set properties on a Promise and the reward line could never be found or removed again.
        const rewardLine = new Orderline({ env: this.env }, { pos: this.pos, order: this, product });
        rewardLine.set_quantity(rewards);
        rewardLine.set_unit_price(0);
        rewardLine.price_type = "manual";
        rewardLine.set_discount(0);
        rewardLine.set_full_product_name();
        rewardLine.dns_is_reward_line = true;
        this.orderlines.add(rewardLine);
        this.dnsMarkLine(rewardLine, promotion, product.lst_price, product.lst_price * rewards, "free_reward");
        return true;
    },

    dnsRecomputePromotions() {
        if (this.dnsPromotionUpdating || !this.pos.dnsPromotionsById) return;
        this.dnsPromotionUpdating = true;
        try {
            this.dnsClearPromotionState();
            this.dnsConsolidateSplitLines();
            const lines = this.dnsNormalLines();
            const usedLines = new Set();
            // R1: build the candidate set from the byProduct index (O(lines + matched))
            // instead of scanning every promotion with Array.includes (O(promotions x lines x scope)).
            const candidateMap = new Map();
            for (const line of lines) {
                for (const promotion of this.pos.dnsPromotionsByProduct.get(line.product.id) || []) {
                    if (!candidateMap.has(promotion.id)) candidateMap.set(promotion.id, { promotion, lines: [] });
                    candidateMap.get(promotion.id).lines.push(line);
                }
            }
            const candidates = [...candidateMap.values()].sort(
                (a, b) => a.promotion.priority - b.promotion.priority || a.promotion.id - b.promotion.id
            );
            for (const promotion of this.pos.dnsPromotionsOrderWide || []) {
                candidates.push({ promotion, lines, orderWide: true });
            }
            for (const { promotion, lines: matchedLines, orderWide } of candidates) {
                if (!isCurrentlyValid(promotion)) continue;
                if (promotion.offer_type === "coupon" && this.dnsAppliedCoupon !== promotion.coupon_code?.toUpperCase()) continue;
                // Excluded lines drop out of both the discount and the minimum-spend total: a
                // promotion either covers a line or it does not, and letting an excluded line
                // push an order over the threshold it is then not part of reads as a bug at the till.
                const excluded = promotion.excludedProductIds || new Set();
                const eligible = orderWide || promotion.all_products
                    ? lines.filter((line) => !usedLines.has(line) && !excluded.has(line.product.id))
                    : matchedLines.filter((line) => !usedLines.has(line));
                const orderEligible = lines.filter((line) => !usedLines.has(line) && !excluded.has(line.product.id));
                if (ORDER_WIDE_TYPES.has(promotion.offer_type)) {
                    if (untaxedTotal(orderEligible) < promotion.minimum_order_amount) continue;
                } else if (!eligible.length) continue;
                if (promotion.offer_type === "product_percent") this.dnsApplyPercent(promotion, eligible);
                else if (promotion.offer_type === "product_fixed") this.dnsApplyFixed(promotion, eligible, promotion.fixed_discount * eligible.reduce((s, l) => s + l.quantity, 0));
                else if (["single_bundle", "mix_match"].includes(promotion.offer_type)) this.dnsApplyBundle(promotion, eligible);
                else if (promotion.offer_type === "multi_group") this.dnsApplyMultiGroup(promotion, eligible);
                else if (promotion.offer_type === "buy_x_get_y") this.dnsApplyBuyXGetY(promotion, eligible);
                else if (promotion.offer_type === "order_percent") this.dnsApplyPercent(promotion, orderEligible);
                else if (promotion.offer_type === "order_fixed") this.dnsApplyFixed(promotion, orderEligible);
                else if (promotion.offer_type === "coupon") promotion.percent_discount ? this.dnsApplyPercent(promotion, orderEligible) : this.dnsApplyFixed(promotion, orderEligible);
                for (const line of [...eligible, ...(["order_percent", "order_fixed", "coupon"].includes(promotion.offer_type) ? orderEligible : [])]) {
                    if (line.dns_promotion_id) usedLines.add(line);
                }
            }
        } finally {
            this.dnsPromotionUpdating = false;
        }
    },
});

export class DnsPromoCodeButton extends Component {
    static template = "dns_pos_promotions.PromoCodeButton";

    async clickDnsCoupon() {
        const pos = this.env.services.pos;
        const notification = this.env.services.notification;
        const order = pos.get_order();
        if (!order) return;

        const applied = order.dnsAppliedCoupon || "";
        const { confirmed, payload } = await this.env.services.popup.add(TextInputPopup, {
            title: applied ? _t("Change promotion code") : _t("Apply promotion code"),
            // Clearing the field is the way off an order: a wrong code otherwise survives into
            // the saved draft and the whole order has to be voided to shake it.
            body: applied ? _t("Clear the field to remove the code from this order.") : "",
            startingValue: applied,
            placeholder: _t("Scan or enter code"),
        });
        if (!confirmed) return;

        const code = String(payload || "").trim().toUpperCase();
        if (!code) {
            if (applied) {
                order.dnsAppliedCoupon = false;
                order.dnsRecomputePromotions();
                notification.add(_t("Promotion code removed."), { type: "info" });
            }
            return;
        }

        const promotion = pos.dnsPromotionsByCoupon?.get(code);
        if (!promotion || !isCurrentlyValid(promotion)) {
            notification.add(_t("This promotion code is invalid or expired."), { type: "danger" });
            return;
        }

        order.dnsAppliedCoupon = code;
        order.dnsRecomputePromotions();

        // Accepted but dormant: the basket is under the offer's minimum. Say so, rather than
        // report success over an order where nothing changed - the discount appears by itself
        // once enough is scanned.
        const shortfall = shortfallForMinimum(
            untaxedTotal(order.dnsNormalLines()), promotion.minimum_order_amount
        );
        if (shortfall > 0) {
            notification.add(
                _t("Code applied - spend %s more for it to take effect.",
                   this.env.utils.formatCurrency(shortfall)),
                { type: "warning" }
            );
            return;
        }
        notification.add(_t("Promotion code applied."), { type: "success" });
    }
}

ProductScreen.addControlButton({
    component: DnsPromoCodeButton,
    condition: function () {
        return true;
    },
});
