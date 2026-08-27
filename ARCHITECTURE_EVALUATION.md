# 架构评估：旧实现、MediaCrawlerPro 与 MediaCrawler

## 结论

MediaCrawlerPro 值得学习的是工程方向，而不是源码：请求驱动、断点持久化、账号/代理资源池、签名与主抓取流程解耦、CLI 可包装为 Agent Skill。开源 MediaCrawler 当前用 `AbstractStore` 与各平台 StoreFactory 支持 JSONL、CSV、Excel、SQLite、MySQL、PostgreSQL、MongoDB 等目标，但其 `NON-COMMERCIAL LEARNING LICENSE 1.1` 明确禁止未经许可的商业用途。因此本项目不复制任何实现，只做清洁室设计：采用通用 Port/Registry 思路，并额外加入服务化所需的 canonical state + transactional outbox。

公开依据：

- [MediaCrawlerPro 组织说明](https://github.com/MediaCrawlerPro)列出了断点续爬、账号与 IP 池、去 Playwright 主链路、签名服务解耦和 CLI Agent Skill。
- [开源 MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)展示了 `media_platform`、`proxy`、`store`、`cache`、`webui` 等模块边界，仍以多平台采集为核心。

## 当前旧实现的问题

1. 商品标准、海外和实时 Spider 大量复制同一生命周期代码，差异主要是队列名、Cookie 池和少量落库行为。
2. 任务真相分散在 MySQL 数字状态、Redis 活跃集合、Funboost Hook、结果缓冲和僵尸任务修复器中；恢复行为无法由单一事务证明。
3. 抓取、解析、重试、状态更新、队列确认和落库混在 Spider 中，新增 UI 或 Agent 接口只能穿透内部实现。
4. 没有业务任务界面；框架自带管理器看不到 ASIN 级进度、断点、证据和失败原因。
5. 凭证配置与代码边界不清。新实现允许部署环境注入；若迁移期明确授权复用旧配置，只做不执行代码的 AST 数据读取，且秘密永不持久化。

## 采用与不采用

| 方向 | 决策 | Amazon 场景补充 |
|---|---|---|
| 请求驱动 | 采用 | 默认 HTTP 客户端；浏览器只能作为未来独立适配器 |
| 断点续爬 | 强化采用 | 逐项状态 + 连续序号断点 + Worker 租约，而不是单一页码 |
| Cookie/代理池 | 采用并收口权限 | 兼容旧 Redis Cookie key、动态代理和健康反馈；凭证仅由部署环境提供 |
| 签名服务 | 暂不需要 | Amazon 商品页 P0 不依赖平台签名；未来特殊接口独立为能力服务 |
| 多平台大基类 | 不采用 | 用小型 Plugin Port，避免继承树和生命周期复制 |
| CLI Agent Skill | 采用并加固 | JSON 输出、允许命令清单、取消确认、禁止凭证和任意 URL |
| 多结果存储 | 采用并强化 | 借鉴 Store Port/Registry；用 SQLite canonical result + outbox 隔离外部失败，而不是在抓取事务中直接调用任意 Store |
| Pro 源码/协议 | 不采用 | 未公开且有商业限制 |
| MediaCrawler 源码 | 不复制 | 当前许可证限制非商业学习；仅参考公开架构概念 |

## 新架构决策

- `application` 只负责用例与 Worker 编排。
- `domain` 定义状态、结果、失败和端口，不依赖 HTTP/SQLite/UI。
- `plugins` 负责 11 类任务的输入归一化、请求和解析；执行策略是数据，不复制 Spider 生命周期。
- `infra` 实现 SQLite、Cookie/代理资源层、`curl_cffi` TLS 指纹传输、速率限制、证据和旧系统桥。
- `interfaces` 暴露同一套 Service 的 CLI、API 和 UI。
- `skills` 是受约束的 Agent 操作面，不允许 Agent 修改抓取器或读取秘密。
