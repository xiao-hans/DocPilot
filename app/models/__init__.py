"""应用数据模型；导入所有模型以注册 SQLAlchemy metadata。"""

from app.models import access, account, document, persistent_job  # noqa: F401
