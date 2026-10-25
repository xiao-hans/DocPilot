# DocPilot

通用型、多租户文档处理平台。租户上传 PDF 或图片，平台按模板提取字段，人工复核后导出 XLSX 或 CSV。

当前形态是**本机开发验证**：只在本机 Docker Compose 上跑，不部署线上服务器。本地读数不构成生产容量或 SLA。

## 十秒概览

- 一套能跑通"上传 → 解析 → 提取 → 复核 → 检索 → 导出"的完整链路，本机一条命令起栈。
- 多租户隔离在路径、服务、数据库三层同时强制；平台管理员不获得任何租户数据访问权。
- 执行正确性不依赖消息只投递一次：执行权由数据库条件更新判定单一胜者。
- 工程基线是 30 份文档：需求、概要/详细设计、测试计划与用例、需求追踪矩阵、风险登记册、实施记录、4 篇 ADR。
- **边界写在明处**：哪些已批准、哪些是待验证目标、哪些结论不能签，见下文[交付边界](#交付边界)。本项目不声称达到生产可用。

先读什么：想跑起来看[快速开始](#快速开始本机开发)；想理解设计看[架构要点](#架构要点)；想核验质量看[测试与门禁](#测试与门禁)。

## 当前状态

| 项 | 值 |
|---|---|
| 交付阶段 | 阶段 A–F 于 2026-10-02 交付；阶段 G0–G28 交付至 2026-10-07；维护轮 M1–M30 交付至 2026-10-24（见 `docs/09 §39`–`§69`） |
| 后端用例 | 444 项（PostgreSQL 与 SQLite 基础套件各 444 项，0 失败 0 跳过） |
| 浏览器用例 | 17 项（Playwright，系统 Chrome；含 2 项可访问性全量扫描） |
| 迁移 head | `e1f4c7a29d63`（审计请求关联 ID），单线性 12 步 |
| 默认基线 | `BROKER_URL` 空、`STORAGE_BACKEND=local`、`EXTRACTOR_MODE=mock`、Webhook 关闭 |

## 核心流程

登录 → 建模板（字段定义）→ 上传 PDF 或图片 → 解析（PyMuPDF / RapidOCR）→ 提取（mock 或 LLM）→ 人工复核 → 检索 → 导出 XLSX 或 CSV。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11、FastAPI、Uvicorn、SQLAlchemy 2.x + Alembic、Pydantic v2 |
| 数据库 | SQLite（仅 `APP_ENV=development`）或 PostgreSQL 17 |
| 队列 | RabbitMQ 4 + `pika` 1.3 薄层（见 `docs/adr/0004`） |
| 对象存储 | `ObjectStorage` 契约 → `LocalStorage` 或 `S3Storage`（boto3）；本机模拟器是 SeaweedFS 4.22 |
| 解析 | PyMuPDF、RapidOCR（onnxruntime）、Pillow |
| 导出 | openpyxl 与自建 `csv_builder`（XLSX 与 CSV 同源） |
| 前端 | Vue 3、TypeScript、Vite 6、TailwindCSS 3 |
| 端到端 | Playwright 1.62，系统 Chrome，单 worker |

## 目录结构

```
app/
  main.py            FastAPI 入口，装配根：uvicorn app.main:app
  dispatcher.py      装配根：python -m app.dispatcher
  worker.py          装配根：python -m app.worker
  webhooks.py        装配根：python -m app.webhooks
  platform/          与业务无关的运行时基础设施
    config.py          全部配置来自环境变量，带范围钳制
    db.py              Engine、SessionLocal、一次性库命名守卫
    errors.py          HTTP 错误映射
    security.py        PBKDF2-SHA256（60 万轮）、令牌生成与摘要
    request_context.py 请求 ID 的 ContextVar 与日志过滤器
    http/dependencies.py 认证与租户授权依赖
  shared/            被多个上下文复用的纯逻辑（不依赖上下文与适配器）
    audit.py  outbox.py  timestamps.py  text_safety.py  export_files.py
  adapters/          外部世界的技术实现（不依赖上下文）
    storage/           对象存储与文档落盘
    parsing/           PDF 文本、扫描件 OCR、解析调度
    extraction/        LLM 与 mock 提取器、提示词
    broker/amqp.py     RabbitMQ 传输
    exports/           XLSX 与 CSV 生成
  models/            只装表定义（ORM / dataclass / Pydantic schema）
  contexts/          10 个限界上下文：automation、documents、identity、
                     operations、processing、quota、review、search、
                     templates、webhooks
migrations/versions/ 12 个 Alembic 修订
tests/               38 个测试文件，444 项用例
scripts/             23 个门禁、演练、回填与核对脚本
frontend/            Vue 3 前端与 Playwright 用例
docs/                30 个 Markdown：编号文档 01–24、4 篇 ADR、1 份索引、1 份实施计划
deploy/postgres/     首次初始化数据卷时创建测试库
```

`app/models/` 只装表定义，不导入任何上下文。全表注册发生在 `migrations/env.py`——它是
`Base.metadata` 的唯一消费者，所以上下文自己的表也在那里导入（`docs/12 §7`）。

规模参考（按磁盘统计，不含 `__pycache__`）：`app/` 110 个 Python 文件、11,755 行；`tests/` 39 个文件、11,653 行；`scripts/` 23 个（17 py + 6 sh）；`docs/` 30 个 Markdown（含 4 篇 ADR）。

## 快速开始（本机开发）

1. 复制配置示例。

   ```
   cp .env.example .env
   ```

2. 生成高熵引导密钥，写入 `.env` 的 `BOOTSTRAP_TOKEN`。

   ```
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

3. 安装依赖。

   ```
   python -m venv .venv
   .venv/Scripts/python.exe -m pip install -r requirements.txt
   ```

   若本机 pip 默认源是清华镜像，`pika` 未同步，单独补装。

   ```
   .venv/Scripts/python.exe -m pip install "pika>=1.3,<2.0" -i https://pypi.org/simple
   ```

4. 启动 API。`APP_ENV=development` 时启动会自动应用待执行的迁移。

   ```
   .venv/Scripts/python.exe -m app.main
   ```

5. 打开 `http://127.0.0.1:8000`。首次使用点"初始化平台"，或向 `POST /api/v1/auth/bootstrap` 发送 `X-Bootstrap-Token` 请求头与 `email`、`password`、`tenant_name`。该入口成功一次后失效。

6. 构建前端（可选）。未构建时 `/` 返回 503 并给出构建命令。

   ```
   cd frontend && npm install && npm run build
   ```

## 本机依赖栈

`docker` CLI 只存在于本机 WSL 的 Ubuntu-24.04 内，Windows 侧没有。Compose 命令应在 WSL 里执行。

```bash
# 基础设施三件套：PostgreSQL、RabbitMQ、SeaweedFS
docker compose up -d

# 整栈：加 API、派发器、Worker、Webhook 投递进程
docker compose --profile app up -d
```

端口只绑 `127.0.0.1`：PostgreSQL 5432、RabbitMQ 5672 与 15672、SeaweedFS 8333。

WSL 会在空闲后回收，容器随之停止。跑门禁或演练前先 `docker compose ps` 确认 `healthy`，并保留一个常驻会话防空闲回收。

```
wsl -d Ubuntu-24.04 -- tail -f /dev/null
```

注意"容器 `healthy`"与"Windows 侧连得上"是两件事：实测出现过容器显示 `healthy`、`Test-NetConnection 127.0.0.1 5432` 却返回 `False` 的情况，那属于 WSL 转发失效，重跑 `docker compose up -d` 即可。

### 队列形态

设置 `BROKER_URL` 后需要三个进程：API、`python -m app.dispatcher`、`python -m app.worker`。`BROKER_URL` 为空时后两者直接退出并说明原因，任务由 API 进程内执行；一旦设置，API 不再自己跑任务，应保证有 Worker 在消费，否则任务停在 `queued`（风险 R-017）。

### 切换到 S3 后端

顺序固定，先回填再切换：

```
.venv/Scripts/python.exe scripts/storage_backfill_to_object_store.py   # 退出码 0
```

回填确认后再把 `STORAGE_BACKEND` 改为 `s3`。端点在 `127.0.0.1` 时，若环境里设了 `HTTP(S)_PROXY`，应让 `NO_PROXY` 覆盖 `127.0.0.1`。

## 测试与门禁

用例按标记分层。基础套件不应设置 `BROKER_URL`。全部门禁应串行执行。

```bash
# 基础套件（SQLite）
.venv/Scripts/python.exe -m pytest -m "not broker and not s3 and not clamd"

# 基础套件（PostgreSQL，先设 TEST_DATABASE_URL）
.venv/Scripts/python.exe -m pytest -m "not broker and not s3 and not clamd"

# 真实 SeaweedFS 端点
.venv/Scripts/python.exe -m pytest -m s3

# 真实 RabbitMQ
.venv/Scripts/python.exe -m pytest -m broker

# 真实 clamd（默认 127.0.0.1:3310；连不上整组跳过）
.venv/Scripts/python.exe -m pytest -m clamd

# PostgreSQL 迁移门禁：空库 upgrade → downgrade -1 → upgrade → alembic check
.venv/Scripts/python.exe scripts/migration_gate.py

# 浏览器端到端（默认形态：依赖栈 + 主机应用进程）
cd frontend && E2E_COMPOSE=1 npx playwright test

# 文档交叉引用与用例编号核对
.venv/Scripts/python.exe scripts/doc_ref_check.py

# 文档与代码是否还对得上（引用的路径、用例名、marker 门禁命令）
.venv/Scripts/python.exe scripts/doc_sync_check.py

# 改了产品函数时：抽出被改的函数，真跑覆盖它的用例
.venv/Scripts/python.exe scripts/assertion_sync_check.py --rev WORKTREE --run
```

### 提交前（可选）

装了这条 hook 之后，每次提交都会跑上面最后那条检查：抽改动里的产品函数，静态找出调用它们的用例，再真跑一遍。**只有覆盖用例真的失败才阻断提交**；纯提示不阻断。

```bash
# 安装（装成 .git/hooks/pre-commit；重复执行是幂等的）
bash scripts/install_git_hooks.sh
# 卸载
rm .git/hooks/pre-commit
```

它拦得住"改了产品却忘了看测试"，**拦不住"看了但没改对"**——静态那一层只能看见测试文件被动过，看不见断言内容是否仍然成立（`docs/06` TC-FUNC-070 是这条边界的真实来历）。所以它是复核入口，不是"断言必然同步"的证明。

最近一次门禁读数（2026-10-24，串行；后端套件与 clamd 套件为维护轮 M32 读数，浏览器端到端为 M31 读数）：

```
迁移门禁              退出码 0
基础套件 PostgreSQL   446 项  0 失败 0 错误 0 跳过
基础套件 SQLite       446 项  0 失败 0 错误 0 跳过
-m s3                 8 项    0 失败 0 错误 0 跳过
-m broker             4 项    0 失败 0 错误 0 跳过（须显式给 BROKER_URL）
-m clamd              6 项    0 失败 0 错误 0 跳过（真实 clamd 在 127.0.0.1:3310）
浏览器端到端 默认形态  18 项   0 失败（含 2 项可访问性全量扫描）
浏览器端到端 容器形态  17 项   0 失败，1.6 分钟（E2E_COMPOSE=1 E2E_APP_CONTAINERS=1；M27 读数，未随本轮复跑）
Webhook 写路径形态     17 项   0 失败，1.4 分钟（E2E_WEBHOOK=1，见 docs/06 §5.33；M27 读数，未随本轮复跑）
文档交叉引用核对        scripts/doc_ref_check.py PASS（30 个文档，0 问题）
文档与代码同步核对      scripts/doc_sync_check.py PASS（26 个文档，0 问题）
断言同步提示            scripts/assertion_sync_check.py --rev WORKTREE --run
```

**读数应同记 tests / failures / errors / skipped 四列。** `-m broker` 在 `BROKER_URL` 未设置时按设计**跳过**而不是失败；只记 `failures=0` 会把"没跑"读成"全通过"（维护轮 M20 实测差点这样记错，见 `docs/09 §59`）。

浏览器端到端在默认形态（依赖栈 + 主机应用进程）下跑；`quota.spec.ts` 现在单跑也成立，不再依赖套件顺序。

**门禁应串行执行。** 同机并行会造成假红：实测基础套件与集成套件并行时报 3 failed 加 11 errors，串行重跑全绿。并行的红不能当作代码结论。

设置 `TEST_DATABASE_URL` 时整轮改用 PostgreSQL，并在会话开始前重置该库的 `public` schema。脚本拒绝指向名称不匹配 `^(?:[\w.-]*_test|test_[\w.-]+)$` 的库，以免误清开发数据。

## 架构要点

### 模块化单体加独立 Worker

`app/contexts/` 下的 10 个限界上下文按 DDD 战略设计划分，每个上下文分 `domain`、`repository`、`service`、`api` 四层（按需存在）。技术设施按层归位：`app/platform/`（运行时基础设施）、`app/shared/`（多上下文复用的纯逻辑）、`app/adapters/`（外部世界的技术实现）、`app/models/`（只装表定义）。分层规则见 `docs/12 §7`，由 `scripts/arch_check.py` 自动校验。

三条硬规则：

1. 仓储查询应显式携带 `tenant_id`。
2. 无租户上下文的方法只允许命名 `*_internal`，且仅供 Worker 调用。
3. 授权只写在 API 层的依赖声明里。

### 多租户隔离在三层同时强制

1. **路径层**：`get_tenant_context` 校验成员关系为 `active`、租户为 `active`、用户匹配，否则 403。
2. **服务层**：仓储方法显式接收 `tenant_id`。
3. **数据库层**：复合外键与部分唯一索引，例如 `(tenant_id, template_version_id)` 引用 `template_versions(tenant_id, id)`。

平台管理员不获得任何租户数据访问权。平台视图只输出计数与租户标识。每读一次聚合，为被列出的每个租户各写一条 `platform.usage_viewed` 审计。

### 执行正确性不依赖"只投递一次"

消息只带 `dispatch_id`、`job_id`、`tenant_id`、`attempt_no`、`queue` 五个字段。执行权由 `job_dispatches` 上的条件更新判定单一胜者。租约令牌随机生成，续租与收口都应匹配当前令牌；被接管的旧 Worker 收口时整体回滚并返回 `False`。

### 快照退役

`processing_jobs.snapshot` 曾与规范化列双真相。六个修订各自可独立回滚：R1 扩容、R2 双写、R3 回填、R4 对账、R5 切读、R6 删列。R1–R5 已于 2026-10-04 合入，读路径不再依赖快照。R6 删列未做，门禁见 `docs/20 §6`。

## 文档导航

入口是 [`docs/文档索引.md`](docs/文档索引.md)。常用文档：

| 文档 | 用途 |
|---|---|
| [`docs/03-软件需求规格说明.md`](docs/03-软件需求规格说明.md) | 功能与质量需求，FR 与 NFR 编号 |
| [`docs/04-概要设计说明.md`](docs/04-概要设计说明.md) | 系统上下文、模块、数据流、部署视图 |
| [`docs/06-测试计划与测试用例.md`](docs/06-测试计划与测试用例.md) | 测试策略、环境、TC 编号与执行记录 |
| [`docs/07-需求追踪矩阵.md`](docs/07-需求追踪矩阵.md) | 需求到设计、测试的追踪 |
| [`docs/08-路线图与风险登记册.md`](docs/08-路线图与风险登记册.md) | 分阶段交付、假设、风险 R-001 至 R-018 |
| [`docs/09-工程实施记录.md`](docs/09-工程实施记录.md) | 各阶段实际落地内容与实施边界 |
| [`docs/17-本地可靠性与规模验证详细设计.md`](docs/17-本地可靠性与规模验证详细设计.md) | 本地集成环境、容量口径、未决事项台账 |
| [`docs/19-运行决策提案.md`](docs/19-运行决策提案.md) | 已批运行决策与仍未给值的项 |
| [`docs/20-快照退役方案.md`](docs/20-快照退役方案.md) | R1–R6 修订、删列门禁、硬边界 |
| [`docs/21-出站Webhook与签名投递详细设计.md`](docs/21-出站Webhook与签名投递详细设计.md) | Webhook 端点、签名、轮换、SSRF 规则 |
| [`docs/24-恶意文件检测详细设计.md`](docs/24-恶意文件检测详细设计.md) | 扫描器契约、三档判定、默认关闭与未扫描的历史行 |

## 交付边界

以下结论已批准，引用时不应越界。

1. **只做本机 Docker Compose 开发验证，不部署线上服务器。** 本地数字不构成生产容量或 SLA。
2. 容量规划目标是 100 个租户、约 1TB 文件、最多 100 个并发任务。这是需验证的产品目标，不代表已满足。
3. `RPO ≤ 24 小时 / RTO ≤ 4 小时` 是**待验证目标**，不是达成。
4. "对象数据已有可恢复备份"这句结论**不能签**。能签的是"有能力发现某次备份不可恢复"。卷级冷副本 12 轮 3 次失败，根因未归因；逻辑级备份 5/5 通过，但只测过本机 6 个对象、6.3 MB。
5. 出站 Webhook **机制已交付，默认关闭**：无 `WEBHOOK_MASTER_KEY` 时建端点返回 409，投递进程拒绝启动。
6. 保留期已批**不等于**自动删除授权。当前系统里没有任何路径会删租户数据。
7. **R6 删列未批。** 批准需要三样同时成立：`docs/20 §6` 四条门禁、观察窗口内 `job.rebuild_incomplete` 审计事件新增 0 条、一次带数据的恢复演练。
8. 孤儿对象审计**只报告不删除**。彻底删除政策未批到定时任务那一步。
9. 恶意文件检测**机制已交付，默认关闭**：`MALWARE_SCAN_BACKEND` 取 `none`（默认）或空串时上传不扫描、`document_versions.malware_status` 保持 `NULL`；取 `clamav` 时走本机 `clamd`；未知取值在工厂里抛 `RuntimeError`，不静默退回关闭。处置口径是**仅标记不阻断**，检出也不拦上传。ClamAV 适配器已用**真容器端到端验过**（`-m clamd` 6 条，本地 `clamav/clamav:stable` + 自造签名，`docs/24 §6.2`），但**本产品未附任何 clamd 部署**——容器与签名库都是验证环境手搭的，运维要自己提供。
10. 链路阈值已批可判：列表与检索 P95 ≤ 200 ms，调度延迟 P95 ≤ 2 s，请求路径禁止整对象读，积压在一个探测周期内可发现。**处理阈值未设定**，因为没有可判的分子：真实 OCR 与 LLM 未接入。

## 仍未给值的事项

计费单价（FR-042）、通知渠道与审批流（FR-040）、SSO 与自助注册、生产供应商与拓扑（G-DEC-04）。R6 删列同样未批，等待书面确认。

恶意文件检测的引擎与处置口径已不再是未给值项：2026-10-17 已批为"可替换契约 + 本地 ClamAV 适配器、默认关闭"与"仅标记不阻断"，2026-10-24 已按此落地（`docs/24`）。仍未定的是**是否在任何部署里开启扫描**——那与 Webhook 主密钥同性质，属运维动作，需单独确认。

## 工程参考

本项目采用适合小团队的迭代式裁剪流程。参考 ISO/IEC/IEEE 12207:2026 生命周期过程、29148:2018 需求工程、42010:2022 架构描述、29119-2:2021 与 29119-3:2021 测试过程与文档。当前文档是项目级工程基线草案，不代表已通过或声明符合这些标准。
