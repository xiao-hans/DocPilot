# 21. 出站 Webhook 与签名投递详细设计（FR-041，阶段 G14）

状态：设计已成文并据其实施（2026-10-04）。评审人：待业主。风险级别：中高（首次引入**服务端主动外联**
与**跨边界密钥**），因此本文件明确写了不做什么与失败口径。

## 1. 为什么现在做这一件

`docs/07` 把 FR-041 记为"部分实现"：`/api/v1` 路由、版本化与鉴权都在，缺的是**出站 Webhook、签名与密钥
轮换**。同一张矩阵里 FR-040 的缺口是"通知只登记、不实际投递"。这两条其实共用一个未建的能力：把已经
与业务变更同事务落库的领域事件（`domain_events`，DOC-DD-004 §4.5）安全地送出去。

它不需要业主先拍板：投递目标由租户自己配置，签名方案是行业通用形状（时间戳 + HMAC-SHA256），重试与
失效口径是工程默认值且可配置。需要拍板的是**通知渠道里的邮件/短信**与**审批流语义**，本次不碰。

## 2. 范围

做：
- 租户级 Webhook 端点：登记（URL + 订阅事件类型）、暂停/恢复、删除、轮换密钥。
- 投递：从 `domain_events` 扇出到每个端点的投递行，带租约的条件更新认领、指数退避、终态成功/失败。
- 签名：`X-DocPilot-Signature: v1=<hex>` 覆盖 `<timestamp>.<body>`，另发 `X-DocPilot-Timestamp`、
  `X-DocPilot-Delivery`、`X-DocPilot-Event`、`X-DocPilot-Secret-Version`（接收方按版本号选密钥）。
- 密钥轮换：签名密钥由服务端主密钥派生，轮换即推进版本号；宽限窗口内额外发一个
  `X-DocPilot-Signature-Previous: v1=<旧密钥签名>`，让接收方无停机切换；窗口外该头消失。
- 失败下钻：投递列表与逐条错误（状态码/错误类别/尝试次数），供运维与 FR-042 的失败明细口径复用。

不做（写清楚，避免被当成已交付）：
- 不改 `NotificationRequest` 的渠道语义，不实现邮件/短信/App 推送，不做审批流。
- 不做 mTLS、IP 白名单、出站代理配置、批量重放（redrive）UI。
- 不承诺投递顺序与"恰好一次"。给的是**至少一次 + 幂等键**：接收方按 `X-DocPilot-Delivery` 去重。
- 不做事件载荷的租户级过滤规则（载荷就是 `domain_events` 里那份，已经由 outbox 写入器拒收绝对路径）。

## 3. 数据

`webhook_endpoints`
- `id`、`tenant_id`、`created_by`、`url`、`description`
- `event_types`（JSON 数组；空数组 = 不订阅任何类型，创建时必须至少一个）
- `status` ∈ `active` / `paused`
- `secret_version`（整数，从 1 起）、`rotated_at`、`grace_seconds`
- `created_at`、`updated_at`
- 唯一约束：无（同租户可多个端点指向同一 URL，用途不同）

签名密钥**不落库**：`secret = HMAC_SHA256(WEBHOOK_MASTER_KEY, f"{endpoint_id}|{secret_version}")`。
因此数据库单独被读走也拿不到签名密钥；轮换只是把 `secret_version` +1 并记 `rotated_at`。创建/轮换时
明文密钥返回一次，列表接口只回末 6 位指纹。

`webhook_deliveries`
- `id`、`endpoint_id`、`tenant_id`、`event_id`、`event_type`
- `status` ∈ `pending` / `inflight` / `succeeded` / `failed`
- `attempts`、`next_attempt_at`、`claimed_until`（租约到期）、`lease_token`
- `response_status`、`error_kind` ∈ `timeout` / `connect` / `http_status` / `rejected` / `blocked_target`
- `last_error`（截断，不含响应体正文）、`created_at`、`completed_at`
- 唯一约束：(`endpoint_id`, `event_id`) —— 扇出的幂等键，重复扫描不会产生第二条

扇出条件：端点 `active` 且 `event_types` 命中，事件 `occurred_at >= 端点 created_at`，且尚无投递行。
**不使用 `domain_events.published_at`**：那个字段现在的含义是"事件规则引擎已消费过"（`automation/service.py`
在跑完规则时写它），Webhook 是第二个独立消费者，借用它会互相吞掉。这一点在代码注释与用例里都点名。

## 4. 投递语义

- 认领沿用阶段 G2 的形状：条件更新把 `pending` 且 `next_attempt_at <= now` 的行改成 `inflight` 并写
  `claimed_until = now + WEBHOOK_LEASE_SECONDS`、新 `lease_token`；只有持有者能收尾。两个进程同时投递
  同一行时只有一个胜者，与 Worker 租约同构（`docs/adr/0004`）。
- 成功判定：HTTP 2xx。4xx/5xx 与网络错误都算失败并按退避重试；`410 Gone` 视为端点已被接收方注销，
  直接终态 `failed` 不再重试。
- 退避：`WEBHOOK_BACKOFF_SECONDS * 2**(attempts-1)`，上限 `WEBHOOK_BACKOFF_MAX_SECONDS`；尝试到
  `WEBHOOK_MAX_ATTEMPTS` 后进 `failed`。
- 租约回收：`inflight` 且 `claimed_until < now` 的行退回 `pending`（进程被杀不会永久卡住）。
- 载荷：`{"id": event_id, "type": event_type, "occurred_at": ..., "tenant_id": ..., "aggregate_type": ...,
  "aggregate_id": ..., "data": <payload>}`，序列化成稳定字节（`sort_keys=True`、`separators=(",", ":")`），
  签名与发送用**同一份字节**，避免任何 re-serialize 差异导致验签失败。
- 时间戳防重放：签名里带 Unix 秒时间戳，接收方应拒绝超过 5 分钟的老投递（文档口径，服务端不代替接收方判定）。

## 5. 出站目标校验（SSRF）

服务端主动外联是本设计新引入的能力，默认按最小权限处理：
- 只允许 `https`；`http` 仅当 `WEBHOOK_ALLOW_INSECURE_TARGETS=true`（本机测试用）且主机名是回环地址。
- 解析后的 IP 落在 `127.0.0.0/8`、`::1`、`10/8`、`172.16/12`、`192.168/16`、`169.254/16`、`0.0.0.0`
  之内时拒绝，除非 `WEBHOOK_ALLOW_PRIVATE_TARGETS=true`。被拒的行走终态 `failed` + `error_kind=blocked_target`，
  不进入重试（配置没改对，重试一万次也不会变）。
- 主机名解析失败 → `connect` 类错误，按退避重试（DNS 可能是瞬时故障）。
- 3xx 重定向**不跟随**：跟随等于把签名交给一个未经校验的第三方地址。

## 6. 权限与审计

- 路由挂在租户上下文下，创建/轮换/删除要求 `owner` 或 `admin`；列表与投递明细允许 `member` 读（排障需要），
  `reviewer` 不开放。
- 审计动作登记进 `app/services/audit.py` 的白名单：`webhook.endpoint_created` / `_rotated` / `_paused` /
  `_resumed` / `_deleted`，只记 `endpoint_id`、`url_host`（不记完整 URL 的 path 与 query，那可能带 token）、
  `secret_version`、`event_types` 数量。**投递本身不写审计**：一次事件 × N 个端点 × 重试会产生大量行，
  审计面会被噪声淹没；明细以 `webhook_deliveries` 为准。

## 7. 进程形态

`python -m app.webhooks` 独立循环，与派发器同构但不依赖 `BROKER_URL`（Webhook 与队列是两条独立链路，
未启用队列的部署照样应该有出站通知）。阶段 G13 已有 `profile: app`，这里加第五个服务
`webhooks`，容器形态下与 api/dispatcher/worker 一起起，测试里用同一份契约。

## 8. 验收口径

用例必须能失败，所以每条正向都配了反向：
1. 签上去 → 本地桩服务用同一派生密钥**验证通过**；篡改 body 任一字节 → 验证失败。
2. 轮换 → 宽限窗口内新旧两个签名都能验过；窗口外只剩新签名；旧密钥不再被签出。
3. 扇出幂等 → 连续三轮扫描只产生一条投递行；两个进程并发各跑一轮 → 同一行只被投递一次（另一家认领失败）。
4. 退避与终态 → 桩服务返回 500 时按退避推进 `next_attempt_at`，到 `WEBHOOK_MAX_ATTEMPTS` 落 `failed`；
   返回 410 直接终态；租约过期能被回收再投。
5. SSRF → `http://127.0.0.1`（默认配置）与 `https://example.com` 指向私有 IP 的解析结果都被拒，
   且 `error_kind=blocked_target`；打开开关后同一条能通过（证明拒的是配置而非代码写死）。
6. 权限矩阵 → `reviewer` 读列表 403、`member` 创建 403、`admin` 全流程可走；跨租户取别的租户端点 404。
7. 双方言：以上全部在 PostgreSQL 与 SQLite 各跑一遍（迁移也走 `alembic check` 无漂移）。
8. 浏览器验收：加界面才做，本轮不承诺（与 G10→G12 的口径一致）。

## 9. 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `WEBHOOK_MASTER_KEY` | 空 | 空即禁用出站投递并让 `app.webhooks` 拒绝启动（不静默降级） |
| `WEBHOOK_TIMEOUT_SECONDS` | 5 | 单次连接+读超时 |
| `WEBHOOK_MAX_ATTEMPTS` | 6 | 到数落 `failed` |
| `WEBHOOK_BACKOFF_SECONDS` | 15 | 退避基数 |
| `WEBHOOK_BACKOFF_MAX_SECONDS` | 1800 | 退避上限 |
| `WEBHOOK_LEASE_SECONDS` | 120 | 投递租约 |
| `WEBHOOK_ROTATION_GRACE_SECONDS` | 3600 | 双签名窗口 |
| `WEBHOOK_ALLOW_PRIVATE_TARGETS` | false | 本机测试才开 |
| `WEBHOOK_ALLOW_INSECURE_TARGETS` | false | 本机测试才开 |

`WEBHOOK_MASTER_KEY` 缺失时的口径与 `BOOTSTRAP_TOKEN` 一样是"配置不全就不做这件事"，绝不退化成
"用一个内置默认密钥签" —— 那会让所有部署共用同一个可预测的密钥。
