# 旧系统迁移边界

## 已实现

- 旧 11 类任务表到 V2 canonical kind 的显式映射。
- `state = 0` 任务只读导入，幂等键为 `legacy:{task}:{id}`。
- 商品主结果/维度结果、搜索、评论、类目、榜单、商家、商家分页和商品详情派生任务的旧表投影。
- 旧 Redis `amazon_{task}_items_buffer`、维度 buffer 和 zlib JSON 格式。
- V2 状态到旧数字状态的显式转换，包括旧 `-4` 的搜索只有一页、无评论、类目无商品和榜单无商品语义。
- 写回前强制校验 V2 kind 与旧任务目标一致；状态同步还绑定导入任务项中的旧 ID，拒绝错表和错行。

## 仍然禁止自动发生

- 未配置旧桥时，普通服务不连接旧 MySQL、旧结果 Redis 或生产队列；只注册运行时明确配置的 sink。
- Shadow 阶段不允许 V2 与旧 Worker 抢同一批任务。
- 未经 `--confirm-external-write` 不写旧 Redis/MySQL，也不回写旧任务状态。
- 不把旧代码中的 Cookie、代理或数据库凭证复制到 V2 文件、任务、日志或输出。显式启用时只通过不执行旧代码的 AST 适配器在内存中复用，并对写目标强制 loopback。

## 交付顺序

1. 运行全部离线测试和 manifest 结构门禁。
2. 在隔离环境安装完整依赖，确认健康接口为 `curl_cffi` 且 TLS impersonation 已启用。
3. 只读导入一小批授权任务，关闭旧 Worker 对该批次的消费，V2 结果只写本地；旧任务与旧结果必须有精确 `task_id` 血缘，按 ASIN/站点/邮编找到的历史行只能诊断，不能证明对等。
4. 优先让旧解析器与 V2 解析同一份响应，再对比字段、行数、页数、父子任务和失败分类；不同时间页面的价格、排名、配送差异不得包装成解析器差异或通过证据。
5. 用 `run_legacy_bridge_roundtrip.py` 验证 loopback MySQL TEMPORARY TABLE 与 Redis 专用探针键，再用 `run_result_sink_roundtrip.py` 验证实际 outbox → MySQL/Redis 投递、幂等和清理；最后验证 dead-letter。
6. 单一 marketplace/单一 kind 灰度，保留旧路由回退。
7. 证据齐全后才更新 parity manifest 为 `complete`。

结果与 outbox 在 SQLite 内原子提交。Redis sink 通过 Lua delivery marker 防重复；MySQL sink 仍依赖旧表业务唯一键与 `INSERT IGNORE`。若旧 MySQL 表没有唯一键，目标写成功但确认前崩溃时必须对账，不能把自动重试描述为 exactly-once。
