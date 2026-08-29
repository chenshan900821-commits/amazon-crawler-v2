# 受控联调与最终放行

这份 Runbook 只用于授权、隔离环境。不得把生产凭证写入文件、命令历史、任务输入或测试产物。

## 1. 构建门禁

1. 在干净环境执行 `python -m pip install .`。
2. 运行全部单元、状态机、离线回放和 Skill 校验。
3. 启动 API 后检查 `/api/v1/health`：抓取端 `transport` 以及所有已配置的 `cookie_harvesters` 都必须是 `curl_cffi` 且 `tls_impersonation` 为 `true`；Cookie/代理只显示数量。
4. 使用仓库扫描确认没有 Cookie、代理认证、数据库 URL 或旧配置秘密进入新目录源码。

## 2. 资源联调

1. 仅通过部署秘密注入隔离 Redis、代理服务和测试数据库 URL。
2. 对两个代表站点（至少 US 与 JP）和两个邮编运行 `cookie-fill --pool default|overseas --confirm-external-write`；另外验证 `product_hw` 始终选择 overseas、普通 JP 任务选择 overseas、merchant/rank 选择独立静态 Cookie。
3. 验证 Cookie 数量增加、TTL 正确、地址切换后重新加载页面能看到目标邮编、消费端刷新可见、验证码/失效 Cookie 被隔离且任何输出不含 Cookie 值。
4. 验证代理轮换、失败剔除、恢复时间和 TLS 指纹 backend；不得以绕过验证码作为验收目标。

资源联调完成后，原始收据必须保存在项目目录外并经过 `compile_cookie_production_evidence.py` 校验后，才能写入仓库内固定路径 `evidence/controlled/cookie-production.json`。收据不得包含 Cookie 值、Redis key、URL、邮编明文或连接信息；最终门禁要求它覆盖 US/JP、至少两个脱敏邮编身份，并证明真实创建、地址确认、TTL、消费端刷新可见和一次维护调度执行。

推荐由受限采集器完成并生成原始收据：

```bash
CONTROLLED_COOKIE_PRODUCTION_AUTHORIZATION=approved-change-reference \
PYTHONPATH=src:. python scripts/collect_cookie_production_evidence.py \
  --us-postal-code 10001 --jp-postal-code 140-0001 \
  --authorization-reference approved-change-reference \
  --max-attempts-per-cookie 3 \
  --output /secure/path/raw-cookie-production-receipt.json \
  --confirm-authorized-cookie-production

PYTHONPATH=src:. python scripts/compile_cookie_production_evidence.py \
  /secure/path/raw-cookie-production-receipt.json
```

采集器只允许 default/US 后接 overseas/JP，固定单并发；每个 Cookie 默认最多一次生产尝试，可通过 `--max-attempts-per-cookie` 显式提高但硬限制为 1–5 次，任一成功立即停止。两个目标起点至少间隔 10 秒（默认 25 秒），且强制原始收据写在项目外。授权引用必须与 `CONTROLLED_COOKIE_PRODUCTION_AUTHORIZATION` 完全一致。

## 3. 11 类任务 Shadow 样本

每类至少准备成功、无结果/不存在、临时限流三个授权样本。分页任务至少覆盖第一页、后续页和中断恢复；商品覆盖父子变体；商家覆盖父任务、分页子任务和商品详情派生任务。

对同一输入保存旧结果与 V2 结果的脱敏副本，逐项比较：

- 输入站点、邮编、日期、页码和来源任务关系；
- 结果行数、ASIN/卖家主键和所有声明字段；
- 价格小数口径、排名、评论、维度、时间观测语义；
- 成功、无结果、不可解析、限流、验证码和重试耗尽状态；
- 父子 Job 数量、幂等键以及重跑后是否重复。

任何声明字段缺失、主键错配、成功页重复或秘密泄露均为阻断项。

### 3.1 先做同响应离线解析对照

旧目录已有成对保存的商品 HTML/解析结果时，先直接执行**当前旧解析器源码**与 V2 的同响应回放，避免把历史 JSON 或历史库状态误当成当前契约：

```bash
python -m pip install '.[legacy-parity]'
PYTHONPATH=src:. python scripts/audit_archived_product_parity.py \
  ../debug_html \
  --legacy-source-root .. \
  --output /secure/path/current-source-product-parity.json
```

该工具只读取旧源码；旧 `settings/config.py` 不会被导入或执行，只用 AST 读取 36 个解析所需字面量。旧日志模块被无副作用适配器替代；`merchant_products` 在无结果分支中的 `./html` 调试写入被内存丢弃 sink 代替，不让原始响应离开进程。输出强制位于项目外，只包含源码、样本、响应和结果哈希、站点码、数量及差异路径。`final_controlled_matrix_satisfied` 永远为 `false`，因为离线保存页不能证明当前 Cookie/代理、失败分类和运行时恢复。

### 3.2 增量采集但不降低最终门槛

33 个场景可以分批采集。批计划使用 `controlled-shadow-batch-plan.v1`，每批包含 1–33 个不重复场景；采集器输出 `controlled-shadow-batch.v1`。先查看累计状态：

```bash
PYTHONPATH=src:. python scripts/merge_controlled_shadow_batches.py \
  /secure/path/batch-*.json --status-only
```

只有 33 个唯一场景全部存在，且每个批次都绑定同一个当前 V2 运行时源码指纹时，合并器才会生成最终 `controlled-shadow-bundle.v1`：

```bash
PYTHONPATH=src:. python scripts/merge_controlled_shadow_batches.py \
  /secure/path/batch-*.json \
  --output /secure/path/controlled-shadow-bundle.json
```

状态汇总还会提前返回 `coverage_ready`、`source_matches_current` 与只含站点列表、不同邮编数量的 `coverage`；不暴露邮编值。即使场景达到 33/33，只要没有同时覆盖 US、JP 和至少两个邮编，或批次指纹不等于当前运行时源码，`merge_eligible` 仍为 `false`，合并命令也会拒绝生成最终 bundle。

批次不会被 `build_controlled_run_report.py` 接受，缺一项、重复 kind/case、run/授权/检查收据不一致都会阻断合并。采集器会把 `src/amazon_crawler` 与 `skills/operate-amazon-crawler` 的确定性 SHA-256 写入 `runtime.v2_source_sha256`；旧批次没有该字段、批次间指纹不一致、或合并后指纹与 Builder/Compiler/最终门禁执行时源码不一致时，均不得进入最终证据。任何运行时代码或 Skill 变更都会要求在代码冻结后重新采集最终矩阵。

`export_legacy_product_shadow_batch_plan.py` 默认只接受旧任务与结果的精确 `task_id` 关联；`--allow-historical-fallback` 只供诊断，按 ASIN/站点/邮编拼到的历史行不是同响应证据，不能通过动态字段严格比较，也不得计入完成度。

当旧库没有可精确关联的当前结果时，成功场景分别使用 `collect_product_dual_shadow.py`、`collect_collection_dual_shadow.py`、`collect_merchant_dual_shadow.py`：只请求一次，把响应保留在内存，同时运行当前旧源码与 V2。失败场景使用 `collect_failure_dual_shadow.py`；HTTP transport observer 会在 404/403/429 分类前提供同一份内存响应，批次只保存哈希。计划禁止携带预计算旧结果；多响应榜单还必须让旧证据、V2 证据的 response-set SHA 完全一致。`throttle` 只接收自然出现的可重试响应，不得主动制造压力或验证码。

候选样本可能返回正常商品、其他失败类型或请求级网络错误。可额外传入 `--diagnostic-output /secure/path/candidate-diagnostic.json`；只有候选与计划 case 不匹配时才写项目外诊断。该工件固定为 `promotable: false`，只含输入/响应哈希、HTTP 状态、字节数、结果字段名和数量，不含 HTML、结果值、URL、Cookie 或代理。它只用于判断下一个低频候选，合并器和最终门禁都不会接受它。

源码指纹门禁加入前曾采集 **23/33、0 个已接受差异**，但这些旧批次只能作为初步证据，不能进入最终 bundle，也不会通过补字段追认。

旧指纹真实联调期间先后发现 FBT 缺失价格的 `null`/空字符串差异，以及 V2 对空 `merchant.seller_name` 新增了旧系统不存在的失败规则；两处都已恢复旧语义并加入回归测试。绑定 `54b5b6cfb0c58d2036268ad1900e07f306843bf4dfa8f66b50ca02b7958fe02b` 的批次曾达到 **23/33、0 个差异**。外围完成性审计随后发现租约只按 owner 校验，同名 Worker 被误复用时不能证明旧执行一定被拒绝；任务和 outbox 均已增加每次领取唯一的 lease token，并覆盖旧 SQLite schema 的安全迁移。旧配置适配器又补齐 4 条代理线路的显式白名单选择，当前冻结指纹更新为 `8ca5b378a056e7327ecf6f4236d8b83ac29046b2d9ea273e3ba09350d9068cb1`，所以旧 23 份批次现在 `source_matches_current: false`，只作历史回归证据；当前可晋级进度重新为 **0/33**。必须在新指纹上重采完整成功、no-result、自然 throttle 矩阵及 JP 覆盖。没有上游响应的网络错误、非限流白名单的解析错误或成功页面都不能冒充 throttle；不会主动制造压力或验证码。

此外，同一合成响应上直接运行当前旧源码与 V2 的 22 个 no-result/throttle 状态回归已全部通过，但不替代剩余真实网络样本。最终放行仍要求以同一个 `v2_source_sha256` 补齐完整 33 场景。

## 4. 崩溃与恢复

在授权样本上分别中止 Worker 于请求前、请求后/提交前和父结果派生子 Job 时。重启后验证：成功项不重抓、过期租约恢复、迟到结果拒绝、父结果与子 Job 不会只存在一半。

## 5. 旧系统桥

1. `legacy-import` 只连接隔离只读账号；重复导入不得重复创建 Job。
2. `legacy-export` 先写隔离 Redis，解压随机样本验证 key 和 JSON；再写隔离 MySQL，验证真实 schema 类型和唯一键。
3. 人为模拟“目标已写、命令未回执”，执行对账后再决定是否重试。
4. `legacy-sync-state` 逐一核对成功、无结果、不可解析、取消和网络失败数字。

## 6. 放行证据

每个 kind 必须在 `contracts/legacy_parity.v1.json` 留下 contract test、offline replay、checkpoint recovery、migration mapping 和 controlled integration 的路径/回执。只有所有阻断项关闭、代码测试通过、真实证据可复核时，才能把任务与 `overall_status` 改为 `complete` 并运行最终门禁。

Controlled integration 不能只放一份说明文本。收据必须是 JSON，并包含：

- `schema_version: controlled-integration-evidence.v1`、对应 `kind`、`status: passed`、`environment: isolated`；
- 非空 `validated_at` 与 `authorization_reference`；
- 至少三个脱敏样本的 SHA-256（成功、无结果/不存在、临时限流）；
- `authorized_samples`、`legacy_shadow_match`、`cookie_proxy_redaction`、`checkpoint_recovery`、`legacy_bridge_roundtrip`、`tls_impersonation` 六项布尔检查；
- `runtime.transport_backend: curl_cffi` 与 `runtime.tls_impersonation: true`；
- `runtime.v2_source_sha256` 非空、绑定全部 33 个样本，且与 Builder、Compiler 和最终门禁当前执行源码一致；
- 对比报告的仓库内相对路径和真实 SHA-256。报告不得包含 Cookie、代理或数据库凭证。

`scripts/verify_parity_manifest.py --require-complete` 会校验这些内容与报告摘要，空文件、错误任务类型、缺失检查、伪造路径或不匹配哈希均无法放行。收据只证明所列受控样本，不自动证明无限制生产可靠性。

## 7. 自动生成脱敏对比报告

不要手工填写 `field_contract_match`、`outcome_match` 或 `differences`。先按 `contracts/controlled-shadow-plan.schema.json`，把隔离旧系统的 state 和四类结果投影写入项目外的 `controlled-shadow-plan.v1`。计划必须恰好包含 11 kind × `success/no_result/throttle` 共 33 个授权样本，并携带 checkpoint recovery、legacy bridge roundtrip、Cookie/代理脱敏三份同批次检查收据。JSON Schema 描述字段形态；采集器还会执行 Schema 无法表达的 kind/case 唯一矩阵、US/JP、两个邮编、收据批次一致和秘密字段检查。

Checkpoint recovery 与秘密边界收据可由本地自检器生成。它使用临时 SQLite 实际验证连续 checkpoint、租约过期恢复、迟到结果拒绝和重启状态，并使用随机秘密哨兵扫描公开 payload 与 SQLite 文件；不访问网络或外部系统：

```bash
PYTHONPATH=src:. python scripts/generate_controlled_local_checks.py \
  --run-id isolated-run-id \
  --authorization-reference change-ticket-reference \
  --output-dir /secure/path/local-checks
```

该命令只生成 `checkpoint_recovery` 与 `cookie_proxy_redaction` 两份收据及其底层工件，且强制写到项目目录之外。`legacy_bridge_roundtrip` 不允许由本地模拟替代，仍必须来自隔离 MySQL/Redis 的真实往返。可先运行 `scripts/run_legacy_bridge_roundtrip.py` 验证旧表覆盖和本机 Redis 原子回环，再运行 `scripts/run_result_sink_roundtrip.py` 让一条合成结果真正穿过 V2 outbox、旧 MySQL/Redis sink；后者会按精确哨兵清理测试行和 Redis 元素。

配置隔离 Cookie Redis、overseas Cookie Redis、merchant/rank Cookie、动态代理和明确的 `curl_cffi` 运行时后，执行 V2 真实采集：

先做纯本地计划校验；该命令不会加载 Cookie/代理、不会创建运行时，也不会发送网络请求：

```bash
PYTHONPATH=src:. python scripts/collect_controlled_v2_shadow.py \
  /secure/path/controlled-shadow-plan.json --validate-only
```

校验通过后再执行真实采集：

```bash
CONTROLLED_ACCEPTANCE_AUTHORIZATION=change-ticket-reference \
PYTHONPATH=src:. python scripts/collect_controlled_v2_shadow.py \
  /secure/path/controlled-shadow-plan.json \
  --output /secure/path/controlled-shadow-bundle.json \
  --concurrency 2 \
  --confirm-authorized-network
```

采集器会重新走正式 Plugin normalize、Cookie/代理/指纹和页面分类链路，并从真实成功结果或失败 evidence 读取响应 SHA。它拒绝未收到上游响应的 Cookie/网络/配置错误样本，不会把这类本地故障冒充成 `no_result` 或 `throttle`。实际 transport 必须是 `curl_cffi`，TLS 收据由真实后端和 33 个响应哈希生成。输出含原始业务结果，工具会强制要求写到项目目录之外；没有显式网络确认或环境授权引用不一致时不会发送请求。

失败候选具有时变性：原计划寻找 no-result 时可能自然收到 Amazon 限流页。`collect_failure_dual_shadow.py --accept-observed-throttle` 只在操作者显式启用、已收到可哈希的上游响应、V2 判为白名单内的可重试 throttle，且当前旧源码对同一响应给出一致失败状态时，才把实际观察记录为 `throttle`；成功、无上游响应的网络错误和非白名单解析错误仍会拒绝。该选项不会重试或增加请求量。

多类自然限流候选应使用 `run_controlled_failure_campaign.py`。它要求每个计划恰好一个场景，启动前一次性校验全部计划及输出冲突，随后强制串行且两次请求起点至少间隔 10 秒；默认 15 秒。单个候选不匹配时只写不可晋级的脱敏诊断并继续下一个，已通过证据立即独立落盘，不会因后续候选失败而丢失。该工具对每个计划只调用一次采集器，没有自动重试、并发或诱导限流逻辑：

```bash
CONTROLLED_ACCEPTANCE_AUTHORIZATION=change-ticket-reference \
PYTHONPATH=src:. python scripts/run_controlled_failure_campaign.py \
  /secure/path/product-throttle-plan.json \
  /secure/path/search-throttle-plan.json \
  --legacy-source-root .. \
  --output-dir /secure/path/campaign-round-1 \
  --prefix round-1 \
  --minimum-interval-seconds 15 \
  --confirm-authorized-network
```

若复用已经授权的 no-result 输入来等待偶发自然限流，必须同时传入 `--accept-observed-throttle --require-observed-case throttle`。这样只有同响应旧新状态一致的 throttle 才生成可晋级 bundle；正常 no-result 会转换为只含输入哈希、响应集哈希、源码指纹和失败状态的不可晋级诊断，不会形成重复 no-result 证据。

最终矩阵还必须真实覆盖 US、JP 和至少两个邮编。`build_failure_dual_plan.py --marketplace-code JP --postal-code 100-0001` 会同步设置顶层 marketplace/postal 与旧任务输入中的 Amazon marketplace ID/post_code；生成器拒绝未知站点。JP 样本仍要经过正式 normalize、对应 Cookie 路由、代理、当前旧源码同响应回放和源码指纹门禁，不能只修改报告中的站点标签。

如果目标邮编没有可用 Cookie，可先运行 `report_cookie_pool_coverage.py --pool overseas --marketplace JP --confirm-external-read`。它只对 `cookie:<marketplace>:<postal>:<id>` key 做 SCAN 并按邮编计数，不调用 GET/MGET，不读取 Cookie value，也不输出 key 末尾标识、Redis URL 或凭证；没有显式只读确认时拒绝连接。

采集完成后，把 bundle 转为只含哈希的报告：

```bash
PYTHONPATH=src:. python scripts/build_controlled_run_report.py \
  /secure/path/controlled-shadow-bundle.json \
  --output /secure/path/redacted-controlled-run.json
```

Builder 会执行以下独立校验：

- 每个输入重新经过对应 V2 kind 的正式 normalize 契约，拒绝无效站点、URL、ASIN、卖家、页码或未知形态；
- 成功样本的 V2 响应 SHA 必须与结果 evidence SHA 一致；
- V2 结果通过正式 `project_legacy_result` 投影成旧表四类行，再与隔离旧结果逐字段比较；
- `created_at`、`updated_at`、抓取时刻 `crawl_date` 只比较是否为合法时间；普通搜索的 `data_hour` 是抓取时刻而按易变字段处理，`search_hour.data_hour` 是业务维度并严格比较；其他声明字段和值均比较，列表顺序不会被排序或隐藏；
- 无结果与限流样本通过正式 `legacy_state_for` 重新计算旧状态，不能由操作者自行声明一致；
- 输出只包含输入、邮编、响应和结果的 SHA-256，不包含原始输入、邮编或旧/V2 结果；
- 四项全局检查必须分别提供 `controlled-check-receipt.v1`，包含同一 run、授权引用、隔离环境和底层工件 SHA，单独的布尔值不能通过门禁。

原始 shadow bundle 可能包含业务数据，只能保存在受控路径，不得提交仓库。Builder 会拒绝 Cookie、代理/数据库 URL、密码、令牌及带认证信息的 URI 字段，并以原子方式写出报告。

## 8. 编译脱敏收据

真实联调先汇总为 `controlled-run-report.v1`，再运行：

```bash
PYTHONPATH=src:. python scripts/compile_controlled_evidence.py \
  /path/to/redacted-controlled-run.json
```

编译器不访问网络、不写旧系统，也不修改对等清单。它只会在以下条件全部满足时，向 `evidence/controlled/` 生成每个 kind 的 comparison report 和 receipt：

- 11 个 kind 各有 `success`、`no_result`、`throttle` 三种且仅三种授权样本，共 33 个；
- 总体至少覆盖 US、JP 和两个不同邮编；邮编在报告中只保存 SHA-256；
- 每个样本都有输入、响应、旧结果、V2 结果和最终样本的 SHA-256；
- `field_contract_match`、`outcome_match` 为真且 `differences` 为空；
- 断点恢复、隔离旧桥往返、Cookie/代理脱敏和 `curl_cffi` TLS impersonation 均通过；
- 报告中不存在 Cookie、代理/数据库 URL、密码、令牌或带认证信息的 URI 字段。

编译器生成收据仍不等于完成。操作者必须复核 comparison report，再把对应 `controlled_integration` 路径加入清单并运行最终门禁。不得把单元测试构造的报告、示例 JSON 或手工设置的通过标志作为真实联调证据。

复核 11 份 comparison report 后，可用显式确认命令完成清单晋级：

```bash
PYTHONPATH=src:. python scripts/promote_parity_manifest.py --confirm-reviewed
```

晋级前还必须把项目外的脱敏 Cookie 生产回执编译到固定路径：

```bash
PYTHONPATH=src:. python scripts/compile_cookie_production_evidence.py \
  /secure/path/raw-cookie-production-evidence.json
```

编译器只接受 `controlled-cookie-production-evidence.v1`：至少包含 US 与 JP 两次真实创建操作、两个邮编哈希、地址二次确认、Redis TTL、消费端刷新可见、`curl_cffi` TLS 指纹、外部写授权和维护任务回执。原始回执必须位于项目外且不能是 symlink；输出固定为 `evidence/controlled/cookie-production.json`。最终晋级器会自动校验并挂载这份共享能力证据，缺失、篡改或源码指纹过期都会原子失败。

晋级器会再次核对每个 receipt 与 comparison 的 kind、运行批次、授权引用、时间、运行时和三个样本哈希是否一一对应，验证报告文件 SHA，并确认原有 contract/offline/checkpoint/migration 证据仍完整。全部检查通过后才会原子替换清单；它不执行任何网络或旧库写入。缺少 `--confirm-reviewed`、任一文件被篡改、存在秘密字段或最终门禁失败时均保持原清单不变。
