"""科技战略协作基础服务的服务端基础包。"""

from .quota import QuotaService
from .service import DomainService

__all__ = ["DomainService", "QuotaService"]
