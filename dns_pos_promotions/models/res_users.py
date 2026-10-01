# -*- coding: utf-8 -*-
from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    pos_config_ids = fields.Many2many(
        "pos.config",
        relation="res_users_pos_config_rel",
        column1="user_id",
        column2="config_id",
        string="Allowed POS Configurations",
        help="Allowed Point of Sale configurations for this user.",
    )
