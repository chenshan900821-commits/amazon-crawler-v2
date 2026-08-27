# 旧系统功能对等验收矩阵

这份文档是 V2 是否完成迁移的验收入口。文件存在或接口能调用不算完成；真实状态以 `contracts/legacy_parity.v1.json` 的证据为准。

## 当前结论

整体状态：**11 类任务已完成代码迁移和离线回放，生产对等仍待受控联调。**

| V2 kind | 旧任务名 | 已证明 | 最终放行前仍需证明 |
|---|---|---|---|
| `search` | `search_jp` | 字段契约、流式片段、页输入、价格口径；爱尔兰保存页 48 行回放 | 授权页面与旧结果 shadow 对比 |
| `search_hour` | `search_hour_jp` | 小时字段、完整列表契约；逐项幂等键包含 `data_hour`；旧版 11 次总尝试预算 | 整点调度与真实站点对比 |
| `product` | `product_jp` | 旧主字段、变体维度、旧表投影；19 份真实保存页逐字段回放 | 多站点授权样本与真实 Cookie/代理 |
| `product_hw` | `product_hw_jp` | 独立 kind、同商品/维度契约；旧任务导入保留 overseas 模式 | 部署路由容量和隔离策略 |
| `product_time` | `product_time_jp` | 实时观测 schema、高优先级语义；旧任务导入保留 realtime 模式；无时间参数每次新建观测，显式幂等键可安全重试 | 真实时间表唯一键和重复观测对账 |
| `reviews` | `reviews` | 评论字段、视频、旧默认值和页面完整性门禁 | 无评论/多语言页面 shadow 对比 |
| `merchant` | `merchant` | 商家详情字段 | 不同站点企业信息样本 |
| `merchant_home` | `merchant_home` | 三页上限、原子派生分页 Job；5 份旧不完整页均保持可重试拒绝、不假成功 | 真实成功店铺页数和父子链路联调 |
| `merchant_products` | `merchant_products` | 列表字段、原子派生商品 Job；有 `source_task_id` 时按旧规则去重，JP 派生 `product_hw`，其他站派生 `product` | 旧商品详情任务唯一键对账 |
| `category_asin_list` | `asin_list_jp` | 类目字段、页断点；爱尔兰保存页 24 行回放 | 多站点类目 URL/语言验证 |
| `rank_list` | `rank_list_jp` | 榜单字段、ACP 续传、排名稳定、续页并发度 3；爱尔兰保存页 50 行及类目树回放 | 真实 ACP 响应和分页 shadow 对比 |

## 已通过的公共证据

- `scripts/audit_legacy_source_contracts.py` 直接读取旧 `settings/config.py` 与 11 个 Spider 源文件，核对任务名、任务/结果表、Redis buffer、Cookie 路由、动态代理、业务状态数字、总尝试次数及榜单续页并发度；当前 11/11 通过，审计过程不导入旧配置也不输出秘密值。
- `scripts/audit_legacy_platform_contracts.py` 继续只读核对旧发布器、活跃登记、僵尸恢复、zlib 结果缓冲、批量状态回写、Cookie 缓存/穿透、代理轮换和框架管理页，并要求 V2 的租约、transactional outbox、旧桥、Cookie 生产/消费、代理隔离和任务管理界面均有对应实现；审计不导入或执行旧代码。
- 11 类 canonical task 均经过插件级离线执行，不只调用 Parser。
- 商品、搜索/类目/评论/榜单、商家字段均有合成 fixture 语义断言。
- `scripts/audit_archived_product_parity.py` 不信任历史 JSON 的版本，把当前旧 `product_parser_utils.py` 与 V2 同时运行在 19 份相同 HTML 上；FR、IE、NL、SE、TR、US 共 19/19 结果字段和维度行一致。报告只保存样本/页面/结果哈希和差异路径。另检测到 2 份历史 JSON 已落后于当前旧源码，证明历史数据库或旧快照不能替代同响应对照。该审计需安装 `.[legacy-parity]` 可选依赖，只读取旧源码与配置中的 30 个解析常量，不执行旧配置。
- `scripts/audit_legacy_collection_snapshots.py` 对旧爱尔兰 smoke 证据回放：search 48 行、asin-list 24 行、rank-list 50 行，首 ASIN 和榜单类目树均一致。
- `scripts/audit_legacy_merchant_snapshots.py` 重放 5 份旧商家首页异常快照；V2 均判定 `upstream_incomplete` 且可重试，与旧版“商品数量异常”拒绝语义一致。
- 搜索 `&&&` 流式片段和榜单 ACP 续传有独立回放；榜单续页保持旧版并发度 3，但任一分片失败时 V2 会重试整项，不保留旧版可能静默漏行的行为。
- 商品/评论缺少评分模块或 `jQuery.parseJSON`、搜索/类目缺少结果身份或总数、榜单缺少排名元数据时均拒绝假成功。
- 验证码、HTTP 异常、页面不完整、解析失败和空结果均在失败事件中保留响应 SHA；启用证据采集后，解析/页面策略失败按内容哈希分别保存，重试不会覆盖旧证据；详情经白名单过滤。
- 成功响应只持久化内容类型/语言/长度三项头；`Set-Cookie`、其余响应头以及 URL 查询参数/fragment 均不会进入数据库、API、界面或 Agent 输出。
- Worker 重启、租约过期、迟到结果、暂停/恢复/取消和幂等创建已测试。
- 旧版未预期解析异常的重试语义已对照保留；API、CLI、界面新建与旧任务导入都默认采用普通任务 5 次、小时搜索 11 次总尝试，并允许显式覆盖。
- 商家两级派生 Job 与父结果在同一 SQLite 事务提交。
- 旧任务表、结果表、Redis key、压缩格式、状态数字和结果投影均为显式映射。
- 旧库写回校验 V2 kind 与目标旧任务，状态同步再绑定旧任务 ID，拒绝错表/错行写入。
- 运行时代码、界面和 Agent Skill 的凭证字面量扫描以及 `.env.example` 空值约束纳入自动测试。
- Cookie 冷启动并发（含真实空池）、带负缓存的池穿透、站点/邮编选择、隔离恢复、地址切换后二次页面确认、TTL 写入和定时维护已测试；代理提取失败也采用单飞冷却，部分资源获取失败会释放已取得的租约。
- 最终门禁会单独校验 Cookie 生产链的受控收据；当前尚缺 US/JP、至少两个邮编的真实创建、地址确认、TTL 与消费端可见证据。单元测试或现有 Cookie key 数量不能替代这份证据。
- JSONL 多存储投递拒绝结果文件、回执目录和回执文件符号链接，目录/文件固定为 `0700`/`0600`，公开回执不包含绝对路径。
- TLS 指纹传输层通过假传输契约证明 `impersonate`、代理和秘密遮蔽；真实网络指纹仍属于受控联调。
- 全任务管理界面已经本地交互验证；Agent Skill 有项目内结构、命令白名单、取消确认与秘密输出回归测试。
- 使用授权的旧 Cookie 池、动态代理和 `curl_cffi` 已得到一个真实商品成功响应；由于本机旧库没有可按 `task_id` 精确关联的对应结果，只能按 ASIN/站点/邮编找到历史行，动态价格、排名、配送等不同，因此该批明确记为失败诊断，不计入 33 场景通过数。
- 源码指纹门禁前的旧解析器/V2 同响应累计采集曾达到 **23/33、0 差异**，但旧批次未绑定源码，只能作为初步证据。后续绑定 `54b5b6cfb0c58d2036268ad1900e07f306843bf4dfa8f66b50ca02b7958fe02b` 的实时页面再次取得 **23/33 通过、0 差异**，并发现、修复 `fbt[].price` 缺失值和空 `merchant.seller_name` 两处旧语义差异。外围完成性审计随后发现同名 Worker 重新领取时缺少唯一租约世代，任务和 outbox 均已增加 lease token 校验；旧配置适配器又补齐 4 条代理线路的显式白名单选择，当前冻结指纹更新为 `8ca5b378a056e7327ecf6f4236d8b83ac29046b2d9ea273e3ba09350d9068cb1`。因此旧 23 份批次现在只作历史回归证据：它们彼此 `source_binding_complete: true`，但相对当前源码 `source_matches_current: false`，当前可晋级进度为 **0/33**。新矩阵仍必须覆盖 US、JP 和至少两个邮编。
- 当前旧源码和 V2 在同一合成响应上的 11 类 no-result 与 11 类 throttle 状态对照已 22/22 通过。实时对照累计发现并修复：`is_video` 旧投影漏列、流式搜索的原始文本替换语义、类目排名递增、404 目的归属过宽、以及不同旧解析器对无结果文案的检查顺序和范围。
- `merchant_products` 旧解析器在显式无结果时会先写 `./html`再抛 `NoPageError`；受控回放用内存丢弃 sink 代替该调试写入，既保留旧业务终态，又不持久化原始 HTML。
- 旧 `54b5…` 矩阵当时剩余 10 个自然 throttle：`category_asin_list`、`merchant`、`merchant_home`、`merchant_products`、`product`、`product_hw`、`product_time`、`rank_list`、`reviews`、`search_hour`。租约与旧代理线路选择修复后，当前 `8ca5…` 指纹必须重采全部 33 个身份，而不是只补这 10 个；最终矩阵还必须包含 JP。海外 Cookie 池已有 JP 邮编分组，但此前 JP 商品/搜索均在收到上游响应前返回网络错误。网络错误、正常成功或无结果都不能冒充 throttle，也不会为取得 throttle 主动施压。

## 统一完成门槛

每个任务只有同时满足以下条件，才能把 `current_status` 改成 `complete`：

1. 输入和输出可与旧表无损映射。
2. 脱敏/合成 fixture 的字段语义断言通过。
3. 页/项断点恢复且成功项不重复。
4. 崩溃、租约过期和迟到结果测试通过。
5. Cookie、代理和指纹由统一请求上下文提供，秘密不进入 DB、日志、UI、API、证据或 Agent 输出。
6. 使用授权资源完成离线回放之外的 33 场景 controlled shadow validation；租约 token 修复使旧 23 份批次指纹过期，当前冻结源码下必须重新采集 33/33，并取得 JP 真实覆盖。
7. 使用明确授权的隔离资源生成 Cookie 生产收据，覆盖 US/JP 与至少两个邮编，并证明地址确认、TTL、消费端可见及 Cookie 值脱敏。
8. 在隔离旧 MySQL/Redis 上验证 schema、唯一键、压缩缓冲和状态回写。
9. `python scripts/verify_parity_manifest.py --require-complete` 返回成功。

验证码只检测、隔离和报告，不实现破解；这不是缺口，而是明确的安全边界。

## 验证命令

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python scripts/verify_parity_manifest.py
```

受控联调步骤见 `docs/CONTROLLED_ACCEPTANCE.md`。在证据齐全前，最终门禁必须失败。
