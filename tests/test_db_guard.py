"""一次性数据库命名守卫的回归测试。

清库与 downgrade 是不可逆操作，因此守卫必须只看名字形状而不是子串：
`latest` 含有 `test`，但它是一个正常业务库名。
"""

from __future__ import annotations

import pytest

from app.platform.db import assert_disposable_database


def _url(database: str) -> str:
    return f"postgresql+psycopg://user:password@127.0.0.1:5432/{database}"


def _sqlite_url(path: str) -> str:
    return f"sqlite:///{path}"


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


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/tmp/test_replay.sqlite3", "test_replay"),
        ("C:/tmp/replay_test.db", "replay_test"),
        ("/tmp/docpilot_test.sqlite3", "docpilot_test"),
    ],
)
def test_sqlite_urls_are_judged_by_file_name(path: str, expected: str) -> None:
    """SQLite 的 `database` 是路径，守卫必须按文件名判。

    维护轮 M11 实测：修之前 SQLite 目标**一律被拒**——整条路径拿去匹配名字形状永远不成立。
    方向安全（不会误清库），但让"在 SQLite 上跑清库/演练"变成不可能，
    而 `--replay` 这类工具正需要它。所以这条用例把两种方言的行为都钉住。
    """
    assert assert_disposable_database(_sqlite_url(path)) == expected


@pytest.mark.parametrize("path", ["C:/tmp/docpilot.sqlite3", "/tmp/data.sqlite3", "app.platform.db"])
def test_sqlite_non_disposable_paths_are_rejected(path: str) -> None:
    """开发库与普通文件名仍要拒：修的时候不能把守卫放宽成"路径里有 test 就行"。"""
    with pytest.raises(RuntimeError, match="一次性库命名"):
        assert_disposable_database(_sqlite_url(path))


def test_in_memory_sqlite_is_rejected() -> None:
    """`:memory:` 没有名字形状可核，fail-closed：拒绝，由调用方换文件库。"""
    with pytest.raises(RuntimeError, match="一次性库命名"):
        assert_disposable_database("sqlite:///:memory:")
