# 架构与恢复语义

## 请求与状态路径

```text
UI / CLI / Agent Skill / Legacy Import
                 │
                 ▼
        CrawlerService
 normalize + validate + idempotency
                 │
                 ▼
 SQLite jobs / items / events / results / result_outbox
        │ atomic claim + lease
        ▼
      Worker ── PluginRegistry ── 11 task plugins
        │              │
        │       RequestContextFactory
        │       cookie + proxy + fingerprint
        │              │
        │     curl_cffi TLS transport
        │              │
        └──── result + evidence + follow-up jobs + sink deliveries
                           same transaction
                                      │
                                      ▼
                              DeliveryWorker
                          ┌───────┼──────────┐
                          ▼       ▼          ▼
                        JSONL  Legacy Redis  Legacy MySQL
```

领域层只定义 Job、Item、Result、FollowupJob、ClaimedDelivery 和资源端口；插件负责输入归一化、请求构造和解析；基础设施负责 SQLite、HTTP/TLS、Cookie、代理、证据、结果 sink 和旧系统桥；API、CLI、UI 与 Agent Skill 共用同一个 Service。

## 断点不是一个内存页码

每个输入对应独立 `job_items` 行。Worker 在事务中把待执行项改为 `running`，写入 `lease_owner` 和 `lease_expires_at`，执行期间续租。进程崩溃后，过期租约恢复为 `pending`；成功结果与 Item 完成状态同事务提交。旧 Worker 的迟到结果因租约所有者不匹配被拒绝。

`checkpoint_seq` 只是从第一个输入起连续处于终态的最大序号；真正恢复依据是逐项状态。搜索、类目、榜单和商家分页把 page 放在持久化输入中，所以重启不会依赖进程内游标。

## 原子派生任务

`merchant_home` 解析店铺商品总量后产生最多三页 `merchant_products` Job；带 `source_task_id` 的 `merchant_products` 会产生商品详情 Job。派生 Job、父结果、父 Item 成功状态和 `job.followup_created` 事件在同一 SQLite 事务提交：要么一起存在，要么一起回滚。

父 Job 和子 Job 独立暂停、恢复、失败和重试。子 Job 的 `options` 只保存非秘密的父 Job/Item ID、派生原因，并继承父任务选择的结果 sink 名称。

## 状态库与结果存储分离

`sqlite` 不是可拔掉的普通结果插件，而是 P0 的任务控制面与 canonical result：Item 成功、结果、派生任务和 `result_outbox` 必须在一个事务内提交。任务参数只允许保存 `result_sinks` 名称，连接串、账号、Cookie、代理和输出目录只能存在于运行时配置。

`DeliveryWorker` 独立领取 outbox 行并续租。目标失败只改变 delivery 状态，不回滚抓取；过期 delivery 租约可恢复，迟到确认会被拒绝。JSONL 使用稳定 delivery ID、文件锁与回执防重；旧 Redis 使用 Lua 把投递标记和 `RPUSH` 原子化；旧 MySQL 使用 allowlist 投影与 `INSERT IGNORE`，其最终去重能力取决于旧表唯一键。

CSV、Excel 是结果导出格式，不承担任务状态；ClickHouse、PostgreSQL、对象存储和 Webhook 可以实现同一个 `ResultSink` Port，再注册为名称，不能让用户在 Job 中提交任意 endpoint。

这保留了 MediaCrawler 一类项目“按配置选择多种保存方式”的优点，但把可靠性边界做得更严格：每个 sink 只实现 `name + publish(ClaimedDelivery)`，由启动时注册表注入；一个 Job 可同时选择多个已注册名称，不能动态上传连接串。新增 ClickHouse/S3/PostgreSQL 时不改 Parser、Plugin 或 Job schema，只增加适配器和部署配置；投递失败、重试、dead-letter、去重回执仍由统一 outbox 管理。SQLite canonical 不能被这些分析/导出目标替代。

## 暂停、恢复与取消

- 暂停进入 `pause_requested`，停止领取新项；在途请求完成后进入 `paused`。
- 恢复只重新开放未完成项，不重置成功项。
- 取消标记未开始项并等待在途项结算；已完成结果保留。

## Cookie、代理与传输

`RequestContextFactory` 并发取得 Cookie、代理和指纹。Cookie 可来自固定运行时注入或旧 Redis 池；代理可来自固定运行时注入或动态提取服务。健康接口只返回数量、分组、隔离数和匿名状态。

`CRAWLER_HTTP_TRANSPORT=auto` 在 `curl_cffi` 可用时启用真实 TLS/JA3/HTTP2 浏览器画像，并把 FingerprintProfile 的 `impersonate` 传给传输层；显式 `httpx` 是诊断降级通道，不声称具备 TLS 仿真。跨主机重定向会被拒绝。

验证码、限流、临时不完整页和多语言不存在页通过统一策略分类：验证码/限流可重试并反馈资源失效，不存在/无结果是业务终态。系统不破解验证码。

## 扩展边界

SQLite 是单机开发、离线验收和小规模运行默认控制面。生产多实例应实现 PostgreSQL、Durable Object 或其他具备一致性条件更新的 StateStore，但不能把 ClickHouse 当任务锁，也不能改变租约所有权、迟到结果拒绝、父子/outbox 原子提交和幂等语义。Cloudflare 队列、SP-API、Ads API、第三方 API 和文件上传应实现 Source Adapter 或 Plugin，不把外部字段强行塞入 Amazon 商品 schema。
