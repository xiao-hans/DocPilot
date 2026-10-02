"""应用数据模型；导入所有模型与上下文领域对象以注册 SQLAlchemy metadata。"""

from app.contexts.processing import domain as processing_domain  # noqa: F401
from app.contexts.review import domain as review_domain  # noqa: F401
from app.contexts.templates import domain as templates_domain  # noqa: F401
from app.models import access, account, document, outbox, persistent_job  # noqa: F401
