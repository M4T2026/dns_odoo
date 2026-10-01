# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductsSeasons(models.Model):
    _name = "products.seasons"
    _description = "Product Season"
    _order = "name"

    name = fields.Char(required=True, index=True)
    code = fields.Char(index=True)
    active = fields.Boolean(default=True)


class ProductsSubcategory(models.Model):
    _name = "products.subcategory"
    _description = "Product Subcategory / Size"
    _order = "name"

    name = fields.Char(required=True, index=True)
    code = fields.Char(index=True)
    active = fields.Boolean(default=True)


class ProductMultiBarcode(models.Model):
    _name = "product.multi.barcode"
    _description = "Product Multi Barcode"
    _order = "multi_barcode"

    name = fields.Char(string="Barcode Description")
    multi_barcode = fields.Char(string="Barcode", required=True, index=True)
    product_id = fields.Many2one("product.product", string="Product Variant", ondelete="cascade", required=True, index=True)
    product_tmpl_id = fields.Many2one("product.template", string="Product Template", related="product_id.product_tmpl_id", store=True, index=True)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    vendor_id = fields.Many2one("res.partner", string="Vendor", domain="[('supplier_rank', '>', 0)]")
    product_season = fields.Many2one("products.seasons", string="Season")
    product_subcategory = fields.Many2one("products.subcategory", string="Subcategory / Size")
    product_tag_ids = fields.Many2many(
        "product.tag", relation="product_template_product_tag_rel",
        column1="product_template_id", column2="product_tag_id", string="Product Tags"
    )
    multi_barcode_ids = fields.One2many("product.multi.barcode", "product_tmpl_id", string="Multi Barcodes")


class ProductProduct(models.Model):
    _inherit = "product.product"

    allow_multi_barcodes = fields.Boolean(string="Allow Multi Barcodes", default=True)
    multi_barcode_ids = fields.One2many("product.multi.barcode", "product_id", string="Multi Barcodes")
