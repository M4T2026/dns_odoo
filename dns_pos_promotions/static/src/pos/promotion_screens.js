/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";

patch(ProductScreen.prototype, {
    /**
     * A promotional line is priced by the engine, so its price and discount are not the
     * cashier's to edit: any manual value would be silently recomputed away on the next
     * quantity change. Quantity edits stay allowed and simply re-trigger the engine.
     */
    _setValue(val) {
        const line = this.currentOrder?.get_selected_orderline();
        const editsPrice = ["discount", "price"].includes(this.pos.numpadMode);
        if (line?.dnsIsPromotionLocked?.() && editsPrice) {
            this.numberBuffer.reset();
            this.notification.add(
                _t("%s is priced by a promotion. Change the quantity, or remove the line.",
                   line.dns_promotion_name || _t("This line")),
                { type: "warning" }
            );
            return;
        }
        return super._setValue(...arguments);
    },
});

patch(PaymentScreen.prototype, {
    /**
     * Last-chance reprice before tendering. The engine is synchronous, but an order restored
     * from a saved draft, or one whose promotions were refreshed mid-session, may not have been
     * evaluated against the payload currently loaded.
     */
    async validateOrder(isForceValidate) {
        this.currentOrder?.dnsRecomputePromotions?.();
        return super.validateOrder(...arguments);
    },
});
