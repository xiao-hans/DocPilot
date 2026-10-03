"""一次性数据库命名守卫的回归测试。

清库与 downgrade 是不可逆操作，因此守卫必须只看名字形状而不是子串：
`latest` 含有 `test`，但它是一个正常业务库名。
"""

from __future__ import annotations

import pytest

from app.db import assert_disposable_database


def _url(database: str) -> str:
    return f"postgresql+psycopg://user:password@127.0.0.1:5432/{database}"


@pytest.mark.parametrize("database", ["docpilot_test", "test_docpilot", "a_b_test"])
def test_accepts_disposable_database_names(database: str) -> None:
    assert assert_disposable_database(_url(database)) == database


@pytest.mark.parametrize(
    "database",
    [
        "latest",  # 子串含 test，但不是测试库
        "contest",
        "docpilot",
        "docpilot_restore_drill",
        "postgres",
        "template1",
        "mytest",
        "test",
    ],
)
def test_rejects_other_database_names(database: str) -> None:
    with pytest.raises(RuntimeError, match="一次性库命名"):
        assert_disposable_database(_url(database))
