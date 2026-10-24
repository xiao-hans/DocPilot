from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.platform.config import DATABASE_URL
from app.platform.db import Base

# 全表注册点：`Base.metadata` 的唯一消费者在这里，所以上下文自己的表也必须在此导入。
# 若放进 `app/models/__init__.py`，模型层就会反向依赖业务上下文（docs/12 §7.1 R5）。
from app.contexts.automation import domain as automation_domain  # noqa: F401
from app.contexts.processing import domain as processing_domain  # noqa: F401
from app.contexts.quota import domain as quota_domain  # noqa: F401
from app.contexts.review import domain as review_domain  # noqa: F401
from app.contexts.search import domain as search_domain  # noqa: F401
from app.contexts.templates import domain as templates_domain  # noqa: F401
from app.contexts.webhooks import domain as webhooks_domain  # noqa: F401
from app.models import access, account, dispatch, document, outbox, persistent_job  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
