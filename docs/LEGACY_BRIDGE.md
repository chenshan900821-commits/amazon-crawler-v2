# 旧 MySQL / Redis 兼容桥

兼容桥默认关闭。只有环境变量或显式的安全旧配置加载开关提供连接时，服务才注册旧 MySQL/Redis sink；构建应用不会主动发起网络连接。

## 安全复用旧配置

`CRAWLER_USE_LEGACY_CONFIG=1` 让 V2 以 AST 解析 `CRAWLER_LEGACY_CONFIG_PATH`。它只求值字符串、数字、容器、简单拼接和受限随机数据表达式，不 import 旧模块，也不执行任意函数。环境变量优先于旧值。

MySQL 和结果 Redis 必须解析为 localhost/loopback，否则启动前拒绝。公网 Cookie Redis 默认不加载，只有 `CRAWLER_ALLOW_EXTERNAL_COOKIE_READ=1` 才允许作为只读 Cookie 来源；外部代理 API 需要独立的 `CRAWLER_ALLOW_EXTERNAL_PROXY_API=1`。健康接口只给 `loaded` 与 target scope，不返回主机、端口、用户名、密码或 URL。

## 导入任务

`legacy-import` 只读取旧任务表中 `state = 0` 的行，并以 `legacy:{task}:{id}` 作为 V2 幂等键；重复导入不会创建重复 Job。`product_hw_jp` 会保留为 overseas 模式，`product_time_jp` 会保留为 realtime 高优先级模式。重试预算也按旧 Funboost 配置迁移：普通任务是首次执行加 4 次重试，`search_hour_jp` 是首次执行加 10 次重试。它不会抢占或修改旧任务状态，因此 Shadow 阶段不能让旧 Worker 与 V2 同时处理同一批任务。

## 写出结果

`legacy-export` 可把 V2 结果投影成旧表字段后写到旧 Redis 压缩缓冲，或直接参数化批量写入旧 MySQL。商品主结果和维度结果会拆分，商家首页会投影成分页子任务，带 `source_task_id` 的商家商品会投影出商品详情任务。

手工写出命令必须带 `--confirm-external-write`。普通 Job 也可选择 `legacy_redis` 或 `legacy_mysql` 作为命名 result sink；结果和 outbox 先在 SQLite 原子提交，再由独立 delivery lease 分发。旧 Redis 的 delivery marker 与所有 `RPUSH` 在一个 Lua 脚本内完成；旧 MySQL 使用旧唯一键与 `INSERT IGNORE`，仍需把没有业务唯一键的表视为 at-least-once。

旧 DataSaver 和商家商品详情任务扩展使用 `INSERT IGNORE`。V2 的 MySQL writer 对 MySQL/MariaDB 同样生成 `INSERT IGNORE`，使重复投递保持旧唯一键去重语义；命令返回数量表示尝试发布的行数，真实新增数量仍须由目标表对账确认。

## 状态映射

状态不复制数字，而由显式规则转换：成功 `1`、待处理/运行/暂停 `0`、页面不存在或普通搜索无结果 `-3`、Cookie 缺失/不可解析的业务类型 `-2`、搜索只有一页以及无评论/类目无商品/榜单无商品 `-4`、取消 `-5`、其他失败 `-1`。`legacy-sync-state` 需要指定唯一旧任务 ID 并显式确认外部写入；命令会同时验证 V2 kind 与目标旧任务匹配，并确认该 ID 等于导入任务项中保存的旧 ID。

## 秘密边界

数据库和 Redis URL 只能通过运行时环境变量或上述内存态 AST 旧配置适配器提供。命令输出、任务数据库、outbox、结果、证据和健康接口均不返回 URL、Cookie、代理或认证信息。

可用 `scripts/run_legacy_bridge_roundtrip.py` 做 loopback 隔离验收：MySQL 使用会话级 TEMPORARY TABLE，Redis 使用带 TTL 的专用探针键并在返回前删除。报告只包含 scope、布尔结果、配置哈希和缺失表名。若旧库缺少源码已声明的评论任务/结果表，`scripts/ensure_legacy_review_tables.py` 只会在 loopback MySQL 上、同时给出 `--apply --confirm-loopback-write` 时补齐这两个兼容表。

`scripts/run_result_sink_roundtrip.py` 会让一条合成评论结果真正穿过 V2 canonical SQLite、事务 outbox、旧 MySQL 和旧 Redis sink，并逐一验证投递状态；结束前按唯一哨兵删除测试行、Redis 列表元素和 delivery marker。它验证的是桥接机制，不替代 33 个真实新旧业务样本的字段对照。

`legacy-export` 同样拒绝把不匹配的 V2 kind 写入目标旧表。MySQL 写入会拒绝没有任何目标列的行，并按实际列集合分组，避免异构行批量参数错位。
