{
    "name": "Odoo POS Promotions",
    "summary": "Scalable store-aware promotions for Odoo Point of Sale",
    "version": "17.0.2.2.1",
    "category": "Point of Sale",
    "license": "LGPL-3",
    "author": "DNS",
    "depends": [
        "point_of_sale",
        "mail",
        "bus",
    ],
    "data": [
        "security/dns_pos_promotion_security.xml",
        "security/ir.model.access.csv",
        "data/dns_pos_promotion_cron.xml",
        "views/dns_pos_promotion_views.xml",
        "views/dns_promotion_report_views.xml",
        "views/dns_pos_promotion_menus.xml",
        "views/product_views.xml",
        "wizards/dns_legacy_promotion_migration_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "dns_pos_promotions/static/src/admin/**/*.js",
            "dns_pos_promotions/static/src/admin/**/*.xml",
            "dns_pos_promotions/static/src/admin/**/*.scss",
        ],
        "point_of_sale._assets_pos": [
            "dns_pos_promotions/static/src/pos/**/*.js",
            "dns_pos_promotions/static/src/pos/**/*.xml",
            "dns_pos_promotions/static/src/pos/**/*.scss",
        ],
        # The POS unit-test bundle, not web.qunit_suite_tests: these tests import
        # @point_of_sale/... and @dns_pos_promotions/pos/..., neither of which resolves in the
        # backend bundle, so the files silently fail to load there.
        "point_of_sale.assets_qunit_tests": [
            "dns_pos_promotions/static/tests/**/*.js",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": True,
}
