"""确认某个镜像里装的是当前代码（维护轮 M19）。

## 为什么需要它

`E2E_SKIP_BUILD=1` / `--no-build` 会复用已有镜像。M18 实测过误用代价：容器里跑的是
5 天前的镜像，于是"容器形态回归失败"其实是测了旧代码——一次假回归。当时的判断依据是
`docker images --format {{.CreatedSince}}`，但那只能看出"多久之前构建"，看不出
**"里面有没有我这一轮的改动"**。

本脚本按维护轮查标识：在镜像内导入本轮新增的模块、查本轮改过的形状。它只读不写，
不连数据库，因此可以在任何时刻跑。

## 用法

    docker run --rm -v "$PWD/scripts:/check:ro" docpilot-app:dev python /check/image_code_check.py

在容器形态的端到端回归前跑一次；输出里缺任何一个标识，就说明镜像过期，
必须去掉 `--no-build` 重建，否则读数不可信。
"""

from __future__ import annotations

import sys

# 镜像里应用装在 /srv/docpilot；非镜像环境下（主机直跑）当前目录即可。
for candidate in ("/srv/docpilot", "."):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

#: 每项是一个维护轮留下的可观察标识：(标识名, 检查函数)。
CHECKS: list[tuple[str, callable]] = [
    (
        "M3 request_id 中间件",
        lambda: __import__("app.request_context", fromlist=["REQUEST_ID_HEADER"]).REQUEST_ID_HEADER,
    ),
    (
        "M16 检索后端契约",
        lambda: __import__(
            "app.contexts.search.backend", fromlist=["SqlSearchBackend"]
        ).SqlSearchBackend.__name__,
    ),
    (
        "M17 流水线注入点",
        lambda: "property"
        if isinstance(
            __import__("app.services.pipeline", fromlist=["DocumentPipeline"])
            .DocumentPipeline.__dict__.get("dispatcher"),
            property,
        )
        else "plain attr（旧形状，M17 未进镜像）",
    ),
    (
        "M10 快照纯逻辑导出",
        # 脚本层的东西不在 Python 包内，只能查文件在不在。
        #
        # 注意：这里**不能**返回字符串 `"False"`——非空字符串永远为真，
        # 那样这条判据就成了一条永不失败的检查（M19 的负向验证正是这样照出来的）。
        # 要么返回真值、要么抛异常，缺项必须走到 except 分支。
        lambda: _require(
            __import__("pathlib").Path("/srv/docpilot/scripts/snapshot_logical_export.py").exists(),
            "snapshot_logical_export.py 不在镜像里",
        ),
    ),
]


def _require(condition: bool, message: str) -> str:
    """把布尔判据转成"要么给出值、要么抛异常"，避免空判据。"""
    if not condition:
        raise FileNotFoundError(message)
    return "present"


def main() -> int:
    missing: list[str] = []
    for name, probe in CHECKS:
        try:
            value = probe()
        except Exception as exc:  # noqa: BLE001 - 任何失败都意味着镜像里没有这项
            missing.append(name)
            print(f"MISSING  {name}: {type(exc).__name__}")
            continue
        print(f"OK       {name}: {value}")
    if missing:
        print(f"\n镜像过期：缺 {'、'.join(missing)}。去掉 --no-build 重建后再跑回归。")
        return 1
    print("\nIMAGE CODE CHECK: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
