from werkzeug.utils import redirect

from odoo import http
from odoo.http import request


class DnsPromotionWebApp(http.Controller):
    @http.route("/dns/promotions", type="http", auth="user", methods=["GET"])
    def promotion_webapp(self, **_kwargs):
        """Open the authenticated OWL workspace as a chrome-free separate webapp."""
        action = request.env.ref("dns_pos_promotions.action_dns_promotion_workspace")
        menu = request.env.ref("dns_pos_promotions.menu_dns_promotions_root")
        return redirect(f"/web?dns_promo_app=1#action={action.id}&menu_id={menu.id}&cids={request.env.company.id}")
