# Amazon Crawler V2

这是在全新目录中重构的 Amazon 爬虫，不修改旧 Spider、Publisher、Saver 或配置。V2 已迁移 11 类旧任务，并用统一的任务状态机承载断点、租约、幂等、暂停/恢复/取消、派生任务、事件证据、API、管理界面和 AI Agent Skill。

## 当前状态

代码与合成/脱敏 fixture 的离线验收已通过；当前旧商品解析器源码与 V2 已在 19 份同响应保存页上逐字段 19/19 一致，覆盖 FR、IE、NL、SE、TR、US；本机旧 MySQL/Redis 也已完成真实桥接和 outbox sink 回环，未留下探针数据。源码指纹门禁前曾累计采集 **23/33** 个真实同响应场景、0 个差异，但这些批次只保留为历史诊断。当前旧源码与 V2 在同一合成响应上的 11 类 no-result + 11 类 throttle 状态回归已 22/22 通过；可进入最终门禁的当前源码绑定进度以下一段为准。Cookie 生产链虽已实现并通过隔离测试，但最终门禁还要求 US/JP、双邮编的真实生产收据，证明地址确认、TTL、消费端可见与全程脱敏；在获得明确外部写入授权前不会生成该收据。

上述 23 个旧批次采集于源码指纹门禁加入之前，只能作为初步同响应证据，最终合并器不会接收。后续真实页面又发现并修复了两处旧语义差异：FBT 缺失价格必须保留为 `""` 而非 `null`；`merchant` 即使 Amazon 新版页面没有旧 `seller_name` 节点，旧爬虫仍保存空字符串记录并判成功，V2 不得额外拒绝。每次修复都按设计使旧指纹批次失效。之后绑定 `54b5b6cfb0c58d2036268ad1900e07f306843bf4dfa8f66b50ca02b7958fe02b` 的 **23/33 个场景均通过、0 个差异**，覆盖 11 类成功、11 类 no-result 和 `search/throttle`，但外围完成性审计发现租约只按 owner 校验，无法在相同 Worker ID 被误复用时拒绝旧执行；任务与 outbox 现均增加每次领取唯一的 lease token。旧配置适配器随后补齐 4 条代理线路的显式白名单选择，当前冻结运行时源码指纹更新为 `8ca5b378a056e7327ecf6f4236d8b83ac29046b2d9ea273e3ba09350d9068cb1`。旧 23 份仅保留为历史回归证据，状态为 `source_binding_complete: true` 但 `source_matches_current: false`；当前可晋级进度重新为 **0/33**。最终必须基于新指纹重新采集完整矩阵，并同时覆盖 US、JP 和至少两个邮编；否则 `merge_eligible` 仍为 `false`，不能宣布生产功能对等，也不能直接替换旧系统。唯一进度真相见 `contracts/legacy_parity.v1.json`、`docs/PARITY_MATRIX.md` 和项目外受控批次目录。

已实现的 canonical kind：

- `product`、`product_hw`、`product_time`
- `search`、`search_hour`、`reviews`
- `category_asin_list`、`rank_list`
- `merchant`、`merchant_home`、`merchant_products`

迁移期仍注册 `product_jp`、`product_hw_jp`、`product_time_jp`、`search_jp`、`search_hour_jp`、`asin_list_jp`、`rank_list_jp` 等旧别名。

## 核心能力

- SQLite WAL 保存 Job、逐项状态、租约、连续断点、事件和结果；进程重启不重复成功项，过期租约可恢复，迟到 Worker 结果会被拒绝。成功结果与所选二级存储的 outbox 在同一事务提交，MySQL、Redis 或 JSONL 暂时不可用不会丢失本地结果或把抓取误判为失败。
- 结果存储已抽象为命名适配器：`sqlite` 是始终启用的控制面/结果原本，`jsonl` 是本地增量数据流，`legacy_mysql` 与 `legacy_redis` 复用旧投影。一个任务可同时选择多个去向；投递有独立租约、心跳、指数退避、去重回执和 dead-letter 状态。JSONL 目录与文件分别固定为 `0700`/`0600`，拒绝符号链接目标，回执只暴露文件名而非绝对路径。
- 成功与已收到响应的失败都留下 SHA-256；启用证据采集后，解析/页面策略失败按内容哈希分别落盘，重试不会覆盖前一份证据；事件只保存白名单字段。
- 受控迁移采集可在 HTTP 状态分类之前把响应临时交给内存 observer，因此 404/403/429 也能让当前旧解析器与 V2 处理同一响应；原始 HTML 不会进入批次文件。observer 异常不会改变正式抓取结果。
- 每个新受控批次绑定当前 `src/amazon_crawler` 与 Agent Skill 的统一源码 SHA-256；未绑定、绑定到不同版本或与报告生成时源码不一致的批次都不能最终合并或晋级。
- 成功响应证据不会原样保存上游响应头：仅保留内容类型、内容语言和内容长度，`Set-Cookie` 等其余头全部丢弃；证据 URL 会移除查询参数和 fragment，避免秘密进入 SQLite、API、界面或 Agent 输出。
- 商品详情覆盖旧主结果字段与 `amazon_dimensions_detail` 维度投影；实时任务保留独立观测时间。
- `product_time` 未指定 `add_date` 时，每次提交都会创建新的“现在”观测；调用方可传显式幂等键保证网络重试不重复，带旧观测时间的导入任务仍按观测时间稳定去重。
- `search_hour` 的逐项幂等身份包含 `data_hour`，同一批次中相同关键词与页码的不同小时观测不会互相覆盖。
- 新建与旧任务导入均默认保留旧重试预算：普通任务共 5 次尝试，`search_hour`/`search_hour_jp` 共 11 次尝试；API、CLI 和界面可显式覆盖。未预期的解析异常继续按旧语义重试，明确的无结果/不存在业务终态不重试。
- 搜索流式片段、榜单 ACP 续传、类目、评论和商家结果均有字段契约与离线回放。榜单续页保持旧版并发度 3；任一续页失败会重试整项，避免旧版静默漏行。
- `merchant_home` 的分页 Job 与父结果在同一事务创建；带 `source_task_id` 的 `merchant_products` 会原子创建商品详情 Job。
- Cookie 消费兼容旧 `cookie:{marketplace}:{postal}:*` Redis key，支持缓存刷新、冷启动并发收敛、带短时负缓存的池穿透查询、按站点/邮编选择、隔离和健康统计；即使池确实为空，并发 Worker 也不会重复全量扫描 Redis。资源路由保持旧语义：`product_hw` 始终使用 overseas 池，其余池任务仅 JP 使用 overseas 池；`merchant` 与 `rank_list` 可使用独立静态 Cookie。
- Cookie 生产链使用与抓取端一致的 `curl_cffi` TLS impersonation，建立首页会话、设置配送地址并重新加载页面确认目标邮编已生效，再补充 locale/currency 后按 TTL 写入；只在部署者显式确认时运行。定时维护按池/站点/邮编隔离失败，一个目标异常不会停止其余目标或下一轮，报告只给错误类型。
- 动态代理支持获取、轮换、失败隔离，以及提取服务失败时的单飞刷新与冷却恢复；不会因为代理服务失败而偷偷降级为无代理直连。默认 `auto` 传输在依赖可用时使用 `curl_cffi` 做真实 TLS/JA3/HTTP2 浏览器画像；默认 Chrome 131 TLS profile 与对应 User-Agent/平台头保持一致，并区分页面导航、搜索分页和榜单 ACP 请求头。`httpx` 仅为显式降级通道。健康接口会如实返回当前 backend 和是否启用 TLS impersonation。
- 旧 MySQL 任务可只读导入，结果可投影到旧 Redis 压缩缓冲或旧 MySQL，状态数字通过显式规则转换。旧配置只在显式开关下以 AST 读取数据，不导入旧模块或执行旧配置代码；MySQL/结果 Redis 写目标强制为 loopback，公网 Cookie 池和代理 API 需要各自单独授权。
- Agent Skill 只开放创建、查看、暂停、恢复、经确认取消、结果、事件和投递状态；只能选择能力接口返回的存储名称。选择旧 MySQL/Redis 时还需显式确认外部结果写入，Agent 永远不能提供连接串、Cookie、代理或文件路径。

## 安装与启动

```bash
cd amazon_crawler_v2
python -m pip install -e .
python -m amazon_crawler init-db
python -m amazon_crawler serve --port 3000
```

生产部署建议 API 与 Worker 分开：

```bash
CRAWLER_WORKER_ENABLED=false python -m amazon_crawler serve --host 0.0.0.0 --port 3000
python -m amazon_crawler worker
```

当前 HTTP 服务是开发控制面，没有登录、租户隔离和公网限流。增加身份认证、租户授权、审计、配额和结果访问控制前，不得直接暴露到公网。

## 创建任务

商品任务可直接传 ASIN 或受支持 Amazon 商品 URL：

```bash
python -m amazon_crawler create B0XXXXXXXX --kind product --marketplace US
python -m amazon_crawler create B0XXXXXXXX --kind product_time --marketplace US --mode realtime
python -m amazon_crawler create B0XXXXXXXX --kind product --marketplace US \
  --result-sink sqlite --result-sink jsonl
```

其他任务使用结构化 JSON；完整契约见 `skills/operate-amazon-crawler/references/input-contracts.md`：

```bash
python -m amazon_crawler create --kind search \
  --input-json '{"keyword":"wireless mouse","market_id":"US","post_code":"10001","turn_page":1,"frequent":0}'
```

所有命令输出 JSON，便于 Agent 和自动化程序稳定解析。

## Cookie 与旧系统桥

Cookie 生产不会随普通服务启动而访问外部站点。只有配置运行时 Redis/代理并显式确认后才会执行：

```bash
python -m amazon_crawler cookie-fill \
  --marketplace US --postal-code 10001 --target 10 \
  --confirm-external-write
```

为 `product_hw` 或 JP 链路补充旧 overseas 池时使用 `--pool overseas`。目标计划支持 `default`、`overseas` 两个顶层池；旧的无池名格式仍按 `default` 读取。

最终受控验收使用独立采集器。它固定按 default/US、overseas/JP 严格串行，各创建至少一个新 Cookie，两个目标起点默认间隔至少 25 秒；只输出邮编哈希、数量、TTL 和布尔检查，不输出 Cookie 值、Redis key、URL 或连接信息：

```bash
CONTROLLED_COOKIE_PRODUCTION_AUTHORIZATION=approved-change-reference \
PYTHONPATH=src:. python scripts/collect_cookie_production_evidence.py \
  --us-postal-code 10001 --jp-postal-code 140-0001 \
  --authorization-reference approved-change-reference \
  --max-attempts-per-cookie 3 \
  --output /secure/path/raw-cookie-production-receipt.json \
  --confirm-authorized-cookie-production
```

该命令会向 Amazon 发送请求并向两个 Cookie Redis 池新增带 TTL 的 Cookie；必须先得到覆盖这些具体外部副作用的明确授权。它不会写旧 MySQL、结果 Redis 或 V2 结果库。

旧系统桥默认关闭。授权在本机复用旧配置时，无需把秘密复制到 `.env`：

```bash
CRAWLER_USE_LEGACY_CONFIG=1 \
CRAWLER_LEGACY_CONFIG_PATH=../settings/config.py \
python -m amazon_crawler capabilities
```

该模式只自动采用 loopback MySQL/结果 Redis。当前旧 Cookie Redis 是公网目标，只有额外设置 `CRAWLER_ALLOW_EXTERNAL_COOKIE_READ=1` 才作为只读 Cookie 来源；代理 API 同理需要 `CRAWLER_ALLOW_EXTERNAL_PROXY_API=1`。`CRAWLER_LEGACY_PROXY_ROUTE` 只接受 `qg`、`qghw`、`qgal`、`jlal`，默认 `qg`；未知线路直接拒绝，不会静默回退。配置值不会写入 V2 数据库、日志、健康接口或 Agent 输出。

`legacy-import` 只读，并将 `product_hw_jp` 保留为 overseas 模式、`product_time_jp` 保留为 realtime 高优先级模式；手工 `legacy-export` 和 `legacy-sync-state` 必须显式确认外部写入。新任务也可通过 `options.result_sinks` 选择可靠 outbox 写出。Redis sink 用 Lua 原子投递标记防止重复；MySQL/MariaDB 保留旧 `INSERT IGNORE` 唯一键去重行为。跨 SQLite 与旧 MySQL 仍不是分布式事务，因此 MySQL sink 的精确去重仍依赖旧表业务唯一键。

## 验证

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src:. python scripts/audit_legacy_source_contracts.py
PYTHONPATH=src:. python scripts/audit_legacy_platform_contracts.py
python -m pip install -e '.[legacy-parity]'
PYTHONPATH=src:. python scripts/audit_archived_product_parity.py \
  ../debug_html --legacy-source-root .. \
  --output /secure/path/current-source-product-parity.json
python scripts/verify_parity_manifest.py
PYTHONPATH=src:. python scripts/generate_controlled_local_checks.py \
  --run-id isolated-run-id \
  --authorization-reference change-ticket-reference \
  --output-dir /secure/path/local-checks
PYTHONPATH=src:. python scripts/run_legacy_bridge_roundtrip.py \
  --legacy-config ../settings/config.py \
  --output /secure/path/legacy-bridge-roundtrip.json \
  --confirm-loopback-write
PYTHONPATH=src:. python scripts/run_result_sink_roundtrip.py \
  --legacy-config ../settings/config.py \
  --output /secure/path/result-sink-roundtrip.json \
  --confirm-loopback-write
CONTROLLED_ACCEPTANCE_AUTHORIZATION=change-ticket-reference \
PYTHONPATH=src:. python scripts/collect_controlled_v2_shadow.py \
  /secure/path/controlled-shadow-plan.json \
  --output /secure/path/controlled-shadow-bundle.json \
  --confirm-authorized-network
PYTHONPATH=src:. python scripts/build_controlled_run_report.py \
  /path/to/controlled-shadow-bundle.json --output /path/to/redacted-run-report.json
PYTHONPATH=src:. python scripts/compile_controlled_evidence.py /path/to/redacted-controlled-run.json
PYTHONPATH=src:. python scripts/compile_cookie_production_evidence.py \
  /path/to/redacted-cookie-production-receipt.json
PYTHONPATH=src:. python scripts/promote_parity_manifest.py --confirm-reviewed
python /Users/shanchen/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  skills/operate-amazon-crawler
node --check src/amazon_crawler/interfaces/static/app.js
```

只有完成 `docs/CONTROLLED_ACCEPTANCE.md` 的受控联调并补齐真实证据后，才允许运行并通过：

```bash
python scripts/verify_parity_manifest.py --require-complete
```

## 文档

- `ARCHITECTURE_EVALUATION.md`：旧实现与参考项目的取舍
- `ARCHITECTURE.md`：状态机、资源层、派生任务和恢复语义
- `docs/PARITY_MATRIX.md`：旧新功能对等矩阵
- `docs/CONTROLLED_ACCEPTANCE.md`：最终受控联调门禁
- `contracts/controlled-shadow-plan.schema.json`：33 场景真实联调计划的机器契约
- `contracts/controlled-shadow-batch-plan.schema.json`：可增量采集、但不可单独晋级的真实联调批次契约
- `contracts/controlled-product-dual-plan.schema.json`、`controlled-collection-dual-plan.schema.json`、`controlled-merchant-dual-plan.schema.json`：成功场景的当前旧源码同响应计划
- `contracts/controlled-failure-dual-plan.schema.json`：no-result 与自然出现的 throttle/retryable 同响应计划；不会主动诱发限流
- `scripts/report_cookie_pool_coverage.py`：只读 Cookie Redis key 元数据，按站点/邮编计数，不读取 Cookie value 或输出 key ID/连接信息
- `docs/LEGACY_BRIDGE.md`：旧 MySQL/Redis 兼容桥
- `docs/ARCHITECTURE.md`：分层、断点、多存储与 MediaCrawler 借鉴边界
- `docs/API.md`：HTTP API
- `CONTROLLED_EVOLUTION.md`：证据驱动的受控演进
