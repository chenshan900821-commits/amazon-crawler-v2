# Amazon Crawler V2 架构决策

## 1. 边界

V2 只位于 `amazon_crawler_v2`，不导入或修改旧 Spider。旧 `settings/config.py` 仅在显式开关下通过 AST 读取字面量；读取配置不会执行旧模块。旧 MySQL 和结果 Redis 只允许 loopback，外部 Cookie Redis 与代理 API 必须分别授权。

V2 借鉴 MediaCrawler 类项目中“采集器、资源提供器、存储器可替换”的思路，但没有复制旧爬虫的全局单例和抓取后直接写目标库的耦合方式。MediaCrawler 当前公开指南列出 CSV、JSON、JSONL、Excel、SQLite、MySQL、PostgreSQL 等可选存储；V2 取其“多后端可选择”的优点，再增加 canonical state、事务 outbox、投递租约与 dead-letter，把可靠任务状态、抓取插件和结果投递拆成三个独立边界。

MediaCrawler 当前仓库声明为非商业学习许可，因此本项目只做独立架构研究，不复制其代码、选择器或商业受限资产。未来 SaaS 发布仍需逐项复核自身依赖许可证和 Amazon 数据使用边界。

## 2. 分层

1. `interfaces`：HTTP API、管理界面、CLI 和 Agent Skill，只接收业务输入与命名能力，不接收连接串或秘密。
2. `application`：Worker、租约、重试、暂停/恢复/取消、父子 Job 和投递 Worker。
3. `plugins`：11 类 Amazon 任务的输入规范化、请求形态、响应分类和结果解析。
4. `domain/ports.py`：`CookieProvider`、`ProxyProvider`、`ResultSink` 等稳定端口。
5. `infra`：SQLite 状态库、HTTP/TLS、旧配置适配器、Cookie/代理、旧 MySQL/Redis 投影和各结果 sink。

插件只返回带 `schema_version` 的 `CrawlResult` 或结构化 `CrawlFailure`。它不知道最终结果写到 JSONL、MySQL、Redis 还是未来的 ClickHouse/对象存储。

## 3. 为什么不把所有数据压成一张统一宽表

商品、搜索、评论、商家、榜单和类目字段不同，强制做统一宽表会产生大量空列，并让一个任务的字段变化影响所有消费者。V2 只统一稳定信封：

- `kind`：数据类型；
- `schema_version`：该类型的版本；
- `job_id/item_id/result_id`：来源与幂等身份；
- `collected_at`：采集时间；
- `data`：该类型自己的结构化字段；
- `evidence`：脱敏响应证据。

旧 MySQL 表需要固定列时，由 allowlist projection 在 sink 边界转换。将来接 ClickHouse、PostgreSQL、Parquet 或对象存储，也应各自在 sink 内按 `kind + schema_version` 建模，而不是改采集插件。

## 4. 多存储写入模型

`sqlite` 始终是任务、断点、结果和 outbox 的主记录。任务成功时，主结果与所选二级 sink 的投递命令在同一 SQLite 事务提交：

| Sink | 当前用途 | 投递语义 |
| --- | --- | --- |
| `sqlite` | 主结果、任务状态、断点和证据 | 与任务结果同事务 |
| `jsonl` | 本地增量导出和下游处理 | outbox、稳定 delivery ID、文件回执去重 |
| `legacy_redis` | 旧压缩结果缓冲 | outbox、Lua 原子 delivery marker + RPUSH |
| `legacy_mysql` | 旧业务表兼容 | outbox、参数化投影、旧 `INSERT IGNORE` 语义 |

因此“抓取成功”和“二级存储已送达”是两个状态。MySQL 或 Redis 临时不可用不会抹掉抓取结果，也不会让 Amazon 再被重复请求；投递 Worker 用独立租约、重试和 dead-letter 处理目标故障。

跨 SQLite 与外部数据库没有分布式事务。Redis sink 通过 delivery marker 去重；MySQL 的最终去重仍依赖旧表业务唯一键，没有唯一键的旧表只能声明为 at-least-once，不能声称 exactly-once。

## 5. 新增一个存储后端

新增后端只需要：

1. 实现 `ResultSink.name` 与 `publish(ClaimedDelivery)`；
2. 使用 `delivery.id` 作为幂等键；
3. 在启动时用部署秘密构造并注册 sink；
4. 让 `/capabilities` 只公开名称和能力说明；
5. 增加成功、重复投递、超时、租约恢复、秘密扫描和 dead-letter 测试。

API、UI 和 Agent 只能选择 `/capabilities` 已返回的名称。目标 URL、用户名、密码、表名、Redis key 和输出目录不得进入 Job payload。

## 6. 断点与恢复

断点不是写在内存或日志里。每个输入项有独立状态、尝试次数和带 owner 的租约；分页任务的连续页与派生任务也持久化。Worker 崩溃后，过期租约可回收，已成功项不重抓，旧 Worker 的迟到结果因 owner 不匹配而被拒绝。父结果与派生 Job 在同一事务提交，避免只写一半。

## 7. Cookie、代理与秘密

Cookie 消费与 Cookie 生产分离。普通服务只按站点/邮编从 Provider 取租约；维护任务在显式确认后才做地址切换、二次页面确认和 TTL 写入。失败 Cookie/代理进入隔离期，不会把原值写进状态库、日志、API、界面、证据或 Agent 输出。

Agent Skill 只能创建、查看、暂停、恢复、经确认取消任务，以及查询结果、事件和投递状态。它不能读取配置秘密、刷新外部 Cookie、任意选择连接串或直接发布生产结果。

## 8. 当前完成边界

11 类任务、Cookie/代理、旧 MySQL/Redis 桥、管理界面、Agent Skill、断点和多 sink 代码路径已经实现并通过自动测试；本机旧 MySQL/Redis 真实回环也已通过。旧 `54b5…` 指纹的实时同响应矩阵曾达到 23/33、0 差异，但外围审计发现相同 Worker ID 被误复用时缺少唯一租约世代；任务和 outbox 均已增加 lease token 校验。当前 `f637…` 指纹下的可晋级矩阵因此为 0/33，旧批次仅作历史回归证据。新矩阵未到 33/33 且 US/JP/双邮编覆盖未同时满足前，不得把整体状态标为完成。
