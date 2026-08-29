# 真实运行视频与证据清单

本页记录 README 中四条演示视频的来源、执行入口和成功证据。演示于 2026-08-29 在受控本机环境完成，使用项目 `.env` 中已授权且未提交的 Cookie/代理配置，请求公开 Amazon US 商品页 `B07FZ8S74R`。

## 证据边界

- 四种入口分别创建独立 Job，不用同一任务结果冒充四种调用方式。
- 视频只包含公开商品字段、任务状态、响应大小和哈希；不包含 Cookie、代理、Redis URL、Token 或签名密钥。
- 终端视频是对真实运行输出的脱敏、压缩时间实录：保留命令、重试、终态、结果和证据，剪掉无信息的等待时间。
- Web 视频由真实页面操作中的首页、已填写表单、执行中和结果详情关键画面组成。
- Amazon 页面会变化。标题、评分、响应大小和响应哈希是当次事实，不是长期固定断言。
- 这些演示证明四条受控本机路径在该样本上跑通，不替代全任务矩阵、长期稳定性或公网生产验收。

## 四条独立真实任务

| 入口 | Job ID | 类型 / 模式 | 尝试 | 终态 | 结果 | HTTP / 字节 | Amazon 响应 SHA-256 |
|---|---|---|---:|---|---:|---|---|
| Agent Skill | `job_faf30b929a1645f49902681865d2a3c7` | `amazon.product / standard` | 2 | `succeeded` | 1 | `200 / 1,564,297` | `d422cbfc849eeb8beac26383d103048c8334a392871a9f2c84aedd6eac156b92` |
| MCP | `job_4d077f808a2143d7bb069c34609c26ca` | `product / standard` | 2 | `succeeded` | 1 | `200 / 1,563,695` | `923ea9cbdda731ddfa1c0d7bece8a9b1141eb49e99b408bf22bded1fa789ee58` |
| Web | `job_d02afb53ad1b4017828b959f3818ccab` | `product_time / realtime` | 2 | `succeeded` | 1 | `200 / 1,563,679` | `ce4f8389cef0571f79629974dfd7443d20ab70f316067a20be52ba1a20d8e97e` |
| CLI | `job_398ea5ba01844401a4736cb12ae86f04` | `product_time / standard` | 3 | `succeeded` | 1 | `200 / 1,563,292` | `d022cd6a914335f3d3cba4fa52d4141d7a39c9a1752227645d979c222aff542d` |

四条结果都返回：

```json
{
  "asin": "B07FZ8S74R",
  "title": "Echo Dot (3rd Gen, 2018 release) - Smart speaker with Alexa - Charcoal",
  "rating": "4.7 out of 5 stars"
}
```

不同时间读取到的评论数量等易变字段可能不同，因此未将其设为验收常量。

## 复现入口

先完成 README 的安装与 `.env` 配置。真实秘密只能进入本机 `.env` 或部署平台 Secret。

### Agent Skill

在 Codex 中：

```text
$operate-amazon-crawler 采集 US 站商品 B07FZ8S74R，配送邮编使用 10001；等待任务完成，并报告任务状态、结果数量、标题、评分和证据哈希。
```

不依赖 Agent 界面时，下面的命令复现 Skill 在 MCP 不可用时采用的确定性、安全后备路径：

```bash
python skills/operate-amazon-crawler/scripts/crawler_cli.py run B07FZ8S74R \
  --marketplace US \
  --postal-code 10001 \
  --max-attempts 3 \
  --idempotency-key YOUR_REQUEST_ID \
  --timeout-seconds 240
```

### MCP

```bash
amazon-crawler-mcp-client smoke
amazon-crawler-mcp-client call crawler_run_job \
  --arguments '{"inputs":["B07FZ8S74R"],"kind":"product","marketplace_id":"US","postal_code":"10001","max_attempts":3,"idempotency_key":"YOUR_REQUEST_ID","timeout_seconds":240}'
```

握手成功只证明 MCP 链路可用。还必须检查 `configuration_ready=true`、`result.is_error=false`、业务终态为 `succeeded`，以及 `results` 非空。

### Web

```bash
amazon-crawler serve --host 127.0.0.1 --port 3000
```

打开 <http://127.0.0.1:3000>，选择“商品实时观测”、`Realtime`、US、邮编 `10001`，填写 ASIN 后创建任务。演示中的页面按“待执行 → 执行中 → 已完成”变化，详情最终显示 `1 / 1`、`TRY 2/3`、商品标题、评分、覆盖率、响应哈希、SQLite 已提交和重试事件。

### CLI

```bash
amazon-crawler run B07FZ8S74R \
  --kind product_time \
  --marketplace US \
  --postal-code 10001 \
  --max-attempts 3 \
  --idempotency-key YOUR_REQUEST_ID \
  --timeout-seconds 240
```

CLI 返回中的 `runner.mode=job_scoped_in_process_worker`、`runner.consumed_other_jobs=false` 和 `runner.stopped_reason=terminal` 证明它启动的是任务范围内的临时 Worker。

## 视频文件完整性

| 视频 | 时长 | 分辨率 | 文件 SHA-256 |
|---|---:|---|---|
| [`agent-skill.mp4`](assets/demos/agent-skill.mp4) | 26 秒 | 1280×720 | `ce4d8d3833d65ab8a6173511557ff27204e09780f313772239e531cc01a2a86b` |
| [`mcp.mp4`](assets/demos/mcp.mp4) | 27 秒 | 1280×720 | `c4f454ae3a23ccfadd4ca559db1f5de0d17106b345f7ee373bc7db879a17ec46` |
| [`web-control-plane.mp4`](assets/demos/web-control-plane.mp4) | 17 秒 | 1280×720 | `4ab634ece02581b146ef5f9f0f60e3e7cadcc6cf8e0af79e7031587a9f7e5f95` |
| [`cli.mp4`](assets/demos/cli.mp4) | 27 秒 | 1280×720 | `4d748d49841b3042652bda2de2b7f3ccb4e32bfeaea6135345b5669af15ae697` |

同目录的 GIF 是 README 轻量预览，PNG 是视频封面；`frames/` 保留 Web 演示的四个真实关键画面，`raw/*.ass` 保留终端演示的脱敏字幕时间线，便于审阅媒体中展示了什么。
