import base64
import csv
import io

from odoo import fields, models, _
from odoo.exceptions import UserError


class DnsPromotionProductCsv(models.TransientModel):
    _name = "dns.promotion.product.csv"
    _description = "Import Promotion Products"

    promotion_id = fields.Many2one("dns.pos.promotion", required=True)
    file = fields.Binary(required=True)
    filename = fields.Char()
    result = fields.Text(readonly=True)

    def action_import(self):
        self.ensure_one()
        try:
            content = base64.b64decode(self.file).decode("utf-8-sig")
        except Exception as exc:
            raise UserError(_("The CSV must be UTF-8 encoded: %s") % exc) from exc
        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames or "barcode" not in [name.strip().lower() for name in reader.fieldnames]:
            raise UserError(_("CSV must contain a barcode column."))
        rows = list(reader)
        barcodes = [str(row.get("barcode") or row.get("Barcode") or "").strip() for row in rows]
        barcodes = list(dict.fromkeys(value for value in barcodes if value))
        products = self.env["product.product"].search([("barcode", "in", barcodes)])
        if "product.multi.barcode" in self.env:
            alternate = self.env["product.multi.barcode"].search([("multi_barcode", "in", barcodes)])
            products |= alternate.mapped("product_id") | alternate.mapped("product_product")
            found = set(products.mapped("barcode")) | set(alternate.mapped("multi_barcode"))
        else:
            found = set(products.mapped("barcode"))
        missing = [value for value in barcodes if value not in found]
        self.env["dns.promotion.selector"].create({
            "promotion_id": self.promotion_id.id,
            "name": _("CSV import: %s") % (self.filename or _("products")),
            "product_ids": [(6, 0, products.ids)],
        })
        self.promotion_id.with_context(dns_system_write=True).write({"state": "draft"})
        self.result = _("Imported %s products. Missing barcodes: %s") % (len(products), ", ".join(missing) or _("none"))
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id, "view_mode": "form", "target": "new"}

