"""兼容入口：营业日台账。规范实现见 app.reconcile.daily。"""

from app.reconcile.daily import *  # noqa: F401,F403
from app.reconcile.daily import __getattr__  # noqa: F401
