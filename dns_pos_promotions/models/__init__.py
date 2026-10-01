from . import res_users
from . import product_extensions
from . import promotion
from . import pos_session
# After pos_order: Registry.init_models() walks models in import order, calling _auto_init()
# then init() on each. dns.promotion.report.init() builds a SQL view over the dns_* columns
# pos_order.py adds to pos_order_line, so those columns must already exist - importing the
# report first makes a fresh install die on "column line.dns_promotion_id does not exist".
from . import pos_order
from . import promotion_report
