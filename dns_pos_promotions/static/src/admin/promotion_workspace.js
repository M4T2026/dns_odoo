/** @odoo-module **/

import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

/**
 * The message a server error actually carries.
 *
 * An Odoo RPC failure arrives as an RPCError whose `.message` is the generic envelope label
 * ("Odoo Server Error"); the sentence the model raised - "Complete the required fields:
 * date_start, date_end" - is on `.data.message`. Reading `.message` alone turns every
 * ValidationError into an unexplained toast.
 */
function dnsErrorMessage(error) {
    return error?.data?.message || error?.message || "";
}

// Offers that price the whole basket instead of a resolved product list. Mirrors
// ORDER_WIDE_OFFER_TYPES in models/promotion.py - the two must agree or the builder demands
// products the server does not want.
const ORDER_WIDE_OFFER_TYPES = ["order_percent", "order_fixed", "coupon"];

// `id` is false for a new campaign and set when the builder is reopened to edit an existing one.
const emptyBuilder = () => ({
    id: false,
    step: 1, name: "", offer_type: "product_percent", priority: 100, automatic: true,
    all_stores: false, store_group_ids: [], pos_config_ids: [],
    selector: { all_products: false, barcode_terms: "", category_ids: [], tag_ids: [], vendor_ids: [], product_ids: [], products: [] },
    manual_products: [],
    percent_discount: 10, fixed_discount: 0, required_qty: 1, bundle_total: 0,
    bundle_unit_price: 0, reward_product_id: false, reward_product_name: "", reward_qty: 1,
    coupon_code: "", minimum_order_amount: 0,
    date_start: "", date_end: "", timezone: "Australia/Brisbane",
});

export class DnsPromotionWorkspace extends Component {
    static template = "dns_pos_promotions.Workspace";
    static props = { action: Object, actionId: { type: Number, optional: true }, className: { type: String, optional: true } };

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: true, view: "dashboard", query: "", status: "", dashboard: { counts: {} },
            promotions: [], total: 0, offset: 0, limit: 25, products: [], conflicts: [], conflictTotal: 0,
            options: { offer_types: [], timezones: [], stores: [], store_groups: [], categories: [], tags: [], vendors: [] },
            selected: new Set(), builder: emptyBuilder(), preview: null, previewLoading: false, saving: false,
            detail: null, storeGroups: [], selectors: [], mobileNavOpen: false,
            groupEditor: { id: false, name: "", pos_config_ids: [] },
            rewardSearch: { query: "", results: [], loading: false },
            // Resolved SKUs for the open promotion. Loaded on demand and a page at a time:
            // a promotion can cover thousands of products.
            productPanel: { open: false, loading: false, query: "", records: [], total: 0, offset: 0, limit: 50 },
        });
        onWillStart(async () => {
            this.state.options = await this.orm.call("dns.pos.promotion", "workspace_options", []);
            this.state.builder.timezone = this.state.options.default_timezone || this.state.builder.timezone;
            await this.load();
        });
        onMounted(() => {
            const standalone = new URLSearchParams(location.search).has("dns_promo_app");
            document.body.classList.toggle("dns-promo-standalone", standalone);
            if (standalone) document.querySelector(".o_main_navbar")?.style.setProperty("display", "none", "important");
            this.onPopState = (event) => this.restoreHistory(event.state);
            window.addEventListener("popstate", this.onPopState);
            history.replaceState({ ...(history.state || {}), dnsPromotionWorkspace: true, view: "dashboard" }, "");
        });
        onWillUnmount(() => {
            document.body.classList.remove("dns-promo-standalone");
            document.querySelector(".o_main_navbar")?.style.removeProperty("display");
            window.removeEventListener("popstate", this.onPopState);
        });
    }

    async load() {
        this.state.loading = true;
        try {
            const [dashboard, result] = await Promise.all([
                this.orm.call("dns.pos.promotion", "dashboard_data", []),
                this.orm.call("dns.pos.promotion", "search_workspace", [], {
                    query: this.state.query, state: this.state.status || false,
                    limit: this.state.limit, offset: this.state.offset,
                }),
            ]);
            this.state.dashboard = dashboard; this.state.promotions = result.records;
            this.state.total = result.total;
        } finally { this.state.loading = false; }
    }

    /** Promotion Managers build and edit; viewers only browse the promotions on their Allowed POS. */
    get isManager() { return Boolean(this.state.options.is_manager); }
    /** Managers, and back office users for promotions confined to their own Allowed POS. */
    get canBuild() { return Boolean(this.state.options.can_build); }

    async navigate(view, { push = true } = {}) {
        // Also reachable through browser history, so the menu hiding them is not enough.
        if ((!this.canBuild && view === "builder") || (!this.isManager && view === "stores")) view = "dashboard";
        if (push && this.state.view !== view) history.pushState({ dnsPromotionWorkspace: true, view }, "");
        this.state.view = view;
        this.state.mobileNavOpen = false;
        if (view === "products") await this.searchProducts();
        if (view === "conflicts") await this.loadConflicts();
        if (view === "stores") await this.loadStoreGroups();
        if (view === "selectors") await this.loadSelectors();
        if (["dashboard", "promotions"].includes(view)) await this.load();
    }
    async restoreHistory(entry) {
        const view = entry?.dnsPromotionWorkspace ? entry.view : "dashboard";
        if (view === "detail" && entry.promotionId) {
            this.state.loading = true; this.state.view = "detail";
            this.resetProductPanel();
            try { this.state.detail = await this.orm.call("dns.pos.promotion", "workspace_detail", [entry.promotionId]); }
            finally { this.state.loading = false; }
            return this.showDetailProducts();
        }
        await this.navigate(view || "dashboard", { push: false });
    }
    goBack() { if (this.state.view !== "dashboard") history.back(); }

    /**
     * Open the backend redemption report.
     *
     * The Analysis menu lives in the Odoo navbar, which this workspace hides when it runs as the
     * chrome-free webapp (`dns_promo_app=1`) - so from in here the report is unreachable. Leaving
     * the component restores the navbar via onWillUnmount, which is also how the operator gets
     * back afterwards.
     */
    openAnalysis() {
        if (!this.isManager) return;
        this.state.mobileNavOpen = false;
        return this.action.doAction("dns_pos_promotions.action_dns_promotion_report");
    }
    toggleMobileNav() { this.state.mobileNavOpen = !this.state.mobileNavOpen; }
    startBuilder() {
        this.state.builder = emptyBuilder();
        this.state.builder.timezone = this.state.options.default_timezone || this.state.builder.timezone;
        this.state.preview = null;
        return this.navigate("builder");
    }
    /** Edit straight from the promotion library, without opening the detail page first. */
    async editPromotionById(id) {
        this.state.loading = true;
        try { this.state.detail = await this.orm.call("dns.pos.promotion", "workspace_detail", [id]); }
        finally { this.state.loading = false; }
        return this.editPromotion();
    }
    /** Reopen the currently displayed promotion in the builder, prefilled. */
    editPromotion() {
        const editable = this.state.detail?.editable;
        if (!editable) return;
        this.state.builder = {
            ...emptyBuilder(),
            ...editable,
            selector: { ...emptyBuilder().selector, ...(editable.selector || {}) },
            date_start: this.toInputValue(editable.date_start),
            date_end: this.toInputValue(editable.date_end),
            step: 1,
        };
        this.state.preview = null;
        return this.navigate("builder");
    }
    setStep(step) { this.state.builder.step = Math.max(1, Math.min(6, step)); }

    /**
     * What still stops this promotion being saved, in the operator's words.
     *
     * The wizard used to let you walk to Review with nothing filled in and only find out on
     * submit, via whatever the server raised. A datetime-local input is the sharp edge: leave the
     * minutes unset and it reports its value as an empty string, so a field that looks filled in
     * silently isn't.
     */
    builderProblems() {
        const builder = this.state.builder;
        const problems = [];
        if (!builder.name.trim()) problems.push("Name the campaign in step 1.");
        if (!builder.all_stores && !builder.pos_config_ids.length && !builder.store_group_ids.length) {
            problems.push(this.isManager
                ? "Pick at least one store, a store group, or All Stores in step 2."
                : "Pick at least one of your tills in step 2.");
        }
        const selector = builder.selector;
        const hasSegment = selector.all_products || selector.barcode_terms.trim()
            || selector.category_ids.length || selector.tag_ids.length || selector.vendor_ids.length
            || selector.product_ids.length || builder.manual_products.length;
        // Order-wide offers price the whole basket, so an empty product step is the normal case
        // for them rather than an omission.
        if (!hasSegment && !ORDER_WIDE_OFFER_TYPES.includes(builder.offer_type)) {
            problems.push("Choose the products this applies to in step 3.");
        }
        if (builder.offer_type === "buy_x_get_y" && !builder.reward_product_id) {
            problems.push("Pick the free product in step 4.");
        }
        if (builder.offer_type === "coupon") {
            if (!builder.coupon_code.trim()) problems.push("Enter the coupon code in step 4.");
            if (!builder.percent_discount && !builder.fixed_discount) {
                problems.push("Give the coupon a discount in step 4 - a percentage or a fixed amount.");
            }
        }
        if (!builder.date_start || !builder.date_end) {
            problems.push("Set a complete start and end date - including the time - in step 5.");
        } else if (builder.date_end <= builder.date_start) {
            problems.push("The end date must be later than the start date.");
        }
        return problems;
    }
    nextStep() { this.setStep(this.state.builder.step + 1); }
    previousStep() { this.setStep(this.state.builder.step - 1); }
    coverageCount() {
        if (this.state.builder.all_stores) return this.state.options.stores.length;
        const ids = new Set(this.state.builder.pos_config_ids);
        for (const group of this.state.options.store_groups.filter((item) => this.state.builder.store_group_ids.includes(item.id))) {
            // Groups can overlap; this is deliberately labelled estimated until server validation.
            for (let i = 0; i < group.count; i++) ids.add(`group-${group.id}-${i}`);
        }
        return ids.size;
    }
    /** Distinct stores behind the selected tills - a shop runs several, so the till count
     *  on its own reads as far wider coverage than it is. */
    coverageStoreCount() {
        const byId = new Map(this.state.options.stores.map((store) => [store.id, store]));
        if (this.state.builder.all_stores) {
            return new Set(this.state.options.stores.map((store) => store.warehouse_id)).size;
        }
        const warehouses = new Set(
            this.state.builder.pos_config_ids.map((id) => byId.get(id)?.warehouse_id).filter(Boolean)
        );
        // Groups resolve server-side, so their stores can only be estimated here.
        for (const group of this.state.options.store_groups.filter((item) => this.state.builder.store_group_ids.includes(item.id))) {
            for (let i = 0; i < (group.warehouse_count || 0); i++) warehouses.add(`group-${group.id}-${i}`);
        }
        return warehouses.size;
    }
    /** Tills grouped under their store, so the picker reads as shops rather than a flat list. */
    storesWithTills() {
        const byStore = new Map();
        for (const till of this.state.options.stores) {
            const key = till.warehouse || "Unassigned";
            if (!byStore.has(key)) byStore.set(key, { name: key, tills: [] });
            byStore.get(key).tills.push(till);
        }
        return [...byStore.values()].sort((a, b) => a.name.localeCompare(b.name));
    }
    toggleStoreTills(store) {
        const selected = new Set(this.state.builder.pos_config_ids);
        const ids = store.tills.map((till) => till.id);
        const allOn = ids.every((id) => selected.has(id));
        ids.forEach((id) => (allOn ? selected.delete(id) : selected.add(id)));
        this.state.builder.pos_config_ids = [...selected];
    }
    storeIsFullySelected(store) {
        return store.tills.every((till) => this.state.builder.pos_config_ids.includes(till.id));
    }
    toggleArray(field, id, nested = false) {
        const target = nested ? this.state.builder.selector : this.state.builder;
        const values = new Set(target[field]); values.has(id) ? values.delete(id) : values.add(id); target[field] = [...values];
    }
    toggleStoreGroup(id) { this.toggleArray("store_group_ids", id); }
    toggleStore(id) { this.toggleArray("pos_config_ids", id); }
    /** Drop one product from the explicit list the promotion was saved with. */
    removeBuilderProduct(id) {
        const selector = this.state.builder.selector;
        selector.product_ids = selector.product_ids.filter((productId) => productId !== id);
        selector.products = selector.products.filter((product) => product.id !== id);
        this.state.preview = null;
    }
    setMulti(event, field) { this.state.builder.selector[field] = [...event.target.selectedOptions].map((option) => Number(option.value)); }

    /* Buy X Get Y needs one specific giveaway product. The catalogue is far too large for a
       <select>, so this is a search-and-pin box rather than a dropdown. */
    async searchRewardProducts() {
        const search = this.state.rewardSearch;
        if (!search.query.trim()) { search.results = []; return; }
        search.loading = true;
        try { search.results = await this.orm.call("dns.pos.promotion", "reward_product_lookup", [search.query.trim()]); }
        catch (error) { this.notification.add(dnsErrorMessage(error) || "Product search failed.", { type: "danger" }); }
        finally { search.loading = false; }
    }
    onRewardSearchKeydown(event) { if (event.key === "Enter") return this.searchRewardProducts(); }
    pickRewardProduct(product) {
        this.state.builder.reward_product_id = product.id;
        this.state.builder.reward_product_name = product.name;
        this.state.rewardSearch = { query: "", results: [], loading: false };
    }
    clearRewardProduct() {
        this.state.builder.reward_product_id = false;
        this.state.builder.reward_product_name = "";
    }

    async previewProducts() {
        this.state.previewLoading = true;
        try { this.state.preview = await this.orm.call("dns.pos.promotion", "preview_workspace_selector", [this.state.builder.selector]); }
        catch (error) { this.notification.add(dnsErrorMessage(error) || "Product preview failed.", { type: "danger" }); }
        finally { this.state.previewLoading = false; }
    }
    // The <input type="datetime-local"> value is a wall clock with no zone. It is sent as-is and
    // the server interprets it in the promotion's own timezone, so 9am means 9am in the store.
    dateValue(value) { return value ? `${value.replace("T", " ")}:00` : false; }
    toInputValue(sqlWallClock) { return sqlWallClock ? sqlWallClock.slice(0, 16).replace(" ", "T") : ""; }
    async savePromotion(validate = false) {
        this.state.saving = true;
        try {
            const payload = { ...this.state.builder, date_start: this.dateValue(this.state.builder.date_start), date_end: this.dateValue(this.state.builder.date_end) };
            // Display-only rows; the server only needs the ids.
            const { products: _products, ...selector } = payload.selector;
            payload.selector = selector;
            delete payload.step;
            delete payload.manual_products;
            const editing = Boolean(payload.id);
            const saved = editing
                ? await this.orm.call("dns.pos.promotion", "update_from_workspace", [payload.id, payload], { validate })
                : await this.orm.call("dns.pos.promotion", "create_from_workspace", [payload]);
            if (!editing && validate) await this.orm.call("dns.pos.promotion", "action_validate", [[saved.id]]);
            this.notification.add(this.saveMessage(editing, validate, saved), {
                type: saved.state === "conflict" ? "warning" : "success",
            });
            if (editing) return this.openPromotion(saved.id);
            this.state.view = "promotions"; this.state.offset = 0; await this.load();
        } catch (error) { this.notification.add(dnsErrorMessage(error) || "Promotion could not be saved.", { type: "danger", sticky: true }); }
        finally { this.state.saving = false; }
    }
    saveMessage(editing, validate, saved) {
        if (saved.state === "conflict") return `Saved, but validation found ${saved.conflict_count} conflict(s). Review the conflict queue.`;
        if (!editing) return validate ? "Promotion created and validated." : "Promotion saved as draft.";
        // Any edit drops a live promotion to draft server-side, so say so rather than let a
        // campaign quietly stop applying at the tills.
        return validate ? `Promotion updated and back to ${this.stateLabel(saved.state).toLowerCase()}.` : "Promotion saved as draft — validate to push it to the tills.";
    }
    saveDraft() { return this.savePromotion(false); }
    launchPromotion() { return this.savePromotion(true); }

    async searchProducts() {
        if (!this.state.query.trim()) { this.state.products = []; return; }
        this.state.loading = true;
        try { this.state.products = await this.orm.call("dns.pos.promotion", "product_promotion_lookup", [this.state.query.trim()]); }
        finally { this.state.loading = false; }
    }
    async loadConflicts() {
        this.state.loading = true;
        try { const result = await this.orm.call("dns.pos.promotion", "conflict_queue_data", []); this.state.conflicts = result.records; this.state.conflictTotal = result.total; }
        finally { this.state.loading = false; }
    }
    async loadStoreGroups() {
        this.state.loading = true;
        try { this.state.storeGroups = await this.orm.call("dns.pos.promotion", "store_groups_workspace", []); }
        finally { this.state.loading = false; }
    }
    async loadSelectors() {
        this.state.loading = true;
        try { this.state.selectors = await this.orm.call("dns.pos.promotion", "selector_groups_workspace", [], { query: this.state.query }); }
        finally { this.state.loading = false; }
    }
    editStoreGroup(group = null) {
        this.state.groupEditor = { id: group?.id || false, name: group?.name || "", pos_config_ids: [...(group?.pos_config_ids || [])] };
    }
    toggleGroupEditorStore(id) {
        const values = new Set(this.state.groupEditor.pos_config_ids); values.has(id) ? values.delete(id) : values.add(id);
        this.state.groupEditor.pos_config_ids = [...values];
    }
    toggleGroupEditorStoreTills(store) {
        const selected = new Set(this.state.groupEditor.pos_config_ids);
        const ids = store.tills.map((till) => till.id);
        const allOn = ids.every((id) => selected.has(id));
        ids.forEach((id) => (allOn ? selected.delete(id) : selected.add(id)));
        this.state.groupEditor.pos_config_ids = [...selected];
    }
    groupEditorStoreSelected(store) {
        return store.tills.every((till) => this.state.groupEditor.pos_config_ids.includes(till.id));
    }
    async saveStoreGroup() {
        if (this.state.saving) return;
        this.state.saving = true;
        try {
            const saved = await this.orm.call("dns.pos.promotion", "save_store_group_workspace", [this.state.groupEditor]);
            const pushed = saved.revalidated ? ` ${saved.revalidated} live promotion(s) revalidated for the new tills.` : "";
            this.notification.add(`${saved.name} saved.${pushed}`, { type: "success" });
            this.editStoreGroup(); await this.loadStoreGroups();
            this.state.options = await this.orm.call("dns.pos.promotion", "workspace_options", []);
        } catch (error) {
            this.notification.add(dnsErrorMessage(error) || "Store group could not be saved.", { type: "danger", sticky: true });
        } finally { this.state.saving = false; }
    }
    onSearchKeydown(event) { if (event.key === "Enter") return this.state.view === "products" ? this.searchProducts() : this.load(); }
    applyState(state) { this.state.status = state; this.state.offset = 0; this.state.view = "promotions"; return this.load(); }
    nextPage() { if (this.state.offset + this.state.limit < this.state.total) { this.state.offset += this.state.limit; return this.load(); } }
    previousPage() { if (this.state.offset) { this.state.offset = Math.max(0, this.state.offset - this.state.limit); return this.load(); } }
    async openPromotion(id) {
        history.pushState({ dnsPromotionWorkspace: true, view: "detail", promotionId: id }, "");
        this.state.loading = true; this.state.view = "detail"; this.state.mobileNavOpen = false;
        this.resetProductPanel();
        try { this.state.detail = await this.orm.call("dns.pos.promotion", "workspace_detail", [id]); }
        catch (error) {
            // Never leave the operator on a blank detail page with no idea why.
            this.notification.add(dnsErrorMessage(error) || "Could not open this promotion.", { type: "danger", sticky: true });
            this.state.view = "promotions";
            return;
        }
        finally { this.state.loading = false; }
        return this.showDetailProducts();
    }
    /** The product list is what people open a promotion to check, so it starts open. */
    showDetailProducts() {
        if (!this.state.detail || this.state.detail.all_products) return;
        this.state.productPanel.open = true;
        return this.loadDetailProducts(0);
    }
    /** What the customer gets, in words, for every offer type - not just percentage/amount/bundle. */
    rewardSummary(detail) {
        const money = (value) => `$${Number(value || 0).toFixed(2)}`;
        switch (detail.offer_type) {
            case "product_percent": case "order_percent": return `${detail.percent_discount}% off`;
            case "product_fixed": case "order_fixed": return `${money(detail.fixed_discount)} off`;
            case "coupon": return `Code ${detail.coupon_code}: ${detail.percent_discount ? `${detail.percent_discount}% off` : `${money(detail.fixed_discount)} off`}`;
            case "buy_x_get_y": return `Buy ${detail.required_qty}, get ${detail.reward_qty} × ${detail.reward_product || "(no reward product)"} free`;
            case "single_bundle": case "mix_match": case "multi_group":
                return detail.bundle_total
                    ? `Any ${detail.required_qty} for ${money(detail.bundle_total)}`
                    : `Any ${detail.required_qty} at ${money(detail.bundle_unit_price)} each`;
            default: return "—";
        }
    }
    resetProductPanel() {
        this.state.productPanel = { open: false, loading: false, query: "", records: [], total: 0, offset: 0, limit: 50, addTerms: "", busy: false };
    }
    toggleDetailProducts() {
        const panel = this.state.productPanel;
        if (panel.open) { panel.open = false; return; }
        panel.open = true;
        return this.loadDetailProducts(0);
    }
    async loadDetailProducts(offset = this.state.productPanel.offset) {
        const panel = this.state.productPanel;
        panel.loading = true; panel.offset = offset;
        try {
            const result = await this.orm.call("dns.pos.promotion", "promotion_products_workspace", [this.state.detail.id], {
                query: panel.query, limit: panel.limit, offset,
            });
            panel.records = result.records; panel.total = result.total;
        } catch (error) {
            this.notification.add(dnsErrorMessage(error) || "Could not load products.", { type: "danger" });
        } finally { panel.loading = false; }
    }
    onProductPanelKeydown(event) { if (event.key === "Enter") return this.loadDetailProducts(0); }
    nextProductPage() {
        const panel = this.state.productPanel;
        if (panel.offset + panel.limit < panel.total) return this.loadDetailProducts(panel.offset + panel.limit);
    }
    previousProductPage() {
        const panel = this.state.productPanel;
        if (panel.offset) return this.loadDetailProducts(Math.max(0, panel.offset - panel.limit));
    }

    /* Single-product edits. Both revalidate when the promotion had already been validated: the
       underlying write drops it to draft, and a manager adding one SKU to a live campaign does
       not expect to have taken the whole campaign off the tills. A promotion still being drafted
       is left alone - it may not be complete enough to validate yet. */
    autoValidate() { return this.state.detail?.state !== "draft"; }
    async addProducts() {
        const panel = this.state.productPanel;
        if (!panel.addTerms.trim() || panel.busy) return;
        panel.busy = true;
        try {
            const result = await this.orm.call("dns.pos.promotion", "add_promotion_products",
                [this.state.detail.id, panel.addTerms], { validate: this.autoValidate() });
            panel.addTerms = "";
            const unmatched = result.unmatched.length ? ` ${result.unmatched.length} code(s) matched nothing: ${result.unmatched.slice(0, 5).join(", ")}` : "";
            this.notification.add(`${result.added} product(s) added.${unmatched}`, { type: unmatched ? "warning" : "success" });
            await this.refreshDetail();
        } catch (error) {
            this.notification.add(dnsErrorMessage(error) || "Products could not be added.", { type: "danger", sticky: true });
        } finally { panel.busy = false; }
    }
    async removeProduct(productId) {
        const panel = this.state.productPanel;
        if (panel.busy) return;
        panel.busy = true;
        try {
            await this.orm.call("dns.pos.promotion", "remove_promotion_products",
                [this.state.detail.id, [productId]], { validate: this.autoValidate() });
            this.notification.add("Product removed from this promotion.", { type: "success" });
            await this.refreshDetail();
        } catch (error) {
            this.notification.add(dnsErrorMessage(error) || "Product could not be removed.", { type: "danger", sticky: true });
        } finally { panel.busy = false; }
    }
    async refreshDetail() {
        this.state.detail = await this.orm.call("dns.pos.promotion", "workspace_detail", [this.state.detail.id]);
        if (this.state.productPanel.open) await this.loadDetailProducts(this.state.productPanel.offset);
    }

    async clonePromotion() { const clone = await this.orm.call("dns.pos.promotion", "clone_from_workspace", [this.state.detail.id]); this.notification.add(`${clone.name} created.`, { type: "success" }); await this.openPromotion(clone.id); }
    async validatePromotion(id) { await this.orm.call("dns.pos.promotion", "action_validate", [[id]]); this.notification.add("Validation completed.", { type: "success" }); await this.load(); }
    async bulkArchive() { const ids = [...this.state.selected]; if (!ids.length) return; await this.orm.call("dns.pos.promotion", "action_archive", [ids]); this.state.selected = new Set(); await this.load(); }
    toggleSelection(id) { const selected = new Set(this.state.selected); selected.has(id) ? selected.delete(id) : selected.add(id); this.state.selected = selected; }
    stateLabel(value) { return ({ draft: "Draft", conflict: "Conflict", scheduled: "Scheduled", active: "Active", ended: "Ended", cancelled: "Cancelled", archived: "Archived" })[value] || value; }
    offerLabel(value) {
        return this.state.options.offer_labels?.[value]
            || this.state.options.offer_types.find((item) => item.value === value)?.label
            || value;
    }
    /** Picker contents: the buildable types, plus this promotion's own if it predates the list.
     *  Without the extra entry the <select> would silently fall back to its first option and a
     *  save would retype the promotion. */
    builderOfferTypes() {
        const types = this.state.options.offer_types || [];
        const current = this.state.builder.offer_type;
        if (!current || types.some((item) => item.value === current)) return types;
        return [...types, { value: current, label: `${this.offerLabel(current)} (retired)` }];
    }
}

registry.category("actions").add("dns_pos_promotions.workspace", DnsPromotionWorkspace);
