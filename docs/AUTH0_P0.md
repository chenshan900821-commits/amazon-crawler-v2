# Auth0 P0：MCP 对外认证配置

P0 的目标是让公网 MCP Server 具备真实的登录与授权边界，同时保持当前架构简单：Auth0 负责登录、同意和发放 Token；本项目只作为 OAuth Resource Server 验证 Access Token、scope 和租户。服务端不保存 Auth0 Client Secret、Refresh Token 或用户密码。

## 1. 在 Auth0 创建 MCP API

在 Auth0 Dashboard 的 **Applications > APIs** 创建 API：

- Name：`Amazon Crawler MCP`
- Identifier：公网 MCP URL，例如 `https://crawler.example.com/mcp`
- Signing Algorithm：`RS256`
- Access Token Profile / Token Dialect：`RFC 9068`，并让 Token 包含 permissions（Management API 中对应 `rfc9068_profile_authz`）
- RBAC：开启；同时开启“把 Permissions 加入 Access Token”

Identifier 会成为 Access Token 的 `aud`，必须与 `CRAWLER_MCP_OAUTH_AUDIENCE` 完全一致，包括路径，不能填管理页面地址。

配置四个业务权限：

| Permission | 用途 |
| --- | --- |
| `crawler:read` | 预检、能力、任务、结果和事件读取 |
| `crawler:run` | 创建任务和执行任务 |
| `crawler:control` | 暂停和恢复任务 |
| `crawler:cancel` | 取消任务，生产环境还要一次性审批回执 |

建议创建三个角色：Reader 只授予 `crawler:read`；Operator 增加 `crawler:run` 和 `crawler:control`；Admin 才增加 `crawler:cancel`。不要给生产用户 `*`。MCP 指标和审计不属于 Auth0 业务 API 权限，使用受网络限制的独立内部运维入口。

也可以用 Auth0 CLI 创建 API，先把示例 URL 改成真实公网 MCP URL：

```bash
auth0 api post resource-servers --data '{
  "name": "Amazon Crawler MCP",
  "identifier": "https://crawler.example.com/mcp",
  "signing_alg": "RS256",
  "token_dialect": "rfc9068_profile_authz",
  "enforce_policies": true,
  "scopes": [
    {"value": "crawler:read", "description": "Read crawl state and results"},
    {"value": "crawler:run", "description": "Create and run crawl jobs"},
    {"value": "crawler:control", "description": "Pause and resume crawl jobs"},
    {"value": "crawler:cancel", "description": "Cancel crawl jobs"}
  ]
}'
```

## 2. 打开 MCP 兼容开关

在 Auth0 Tenant Settings 中确认：

1. **Resource Parameter Compatibility Profile** 已开启，使 Auth0 能识别 MCP OAuth 请求中的 `resource` 参数。
2. 公共 MCP Client 需要动态注册时，开启 **Client ID Metadata Document Registration**。
3. 数据库或社交登录 Connection 可供第三方 MCP Client 使用；如果 Auth0 要求，将 Connection 提升为 domain-level。

这些开关不能从公开 Discovery/JWKS 数据中证明，必须在 Dashboard 人工复核。

## 3. 配置服务端 `.env`

```dotenv
CRAWLER_MCP_PRODUCTION=true
CRAWLER_MCP_AUTH_MODE=oauth
CRAWLER_MCP_OAUTH_PROVIDER=auth0

# Auth0 Dashboard 显示的 Domain；不带 https://，不带路径
CRAWLER_MCP_AUTH0_DOMAIN=your-tenant.us.auth0.com

# 部署后的真实公网 MCP URL，也是 Auth0 API Identifier / Token audience
CRAWLER_MCP_RESOURCE_SERVER_URL=https://crawler.example.com/mcp
CRAWLER_MCP_OAUTH_AUDIENCE=https://crawler.example.com/mcp

# Auth0 P0 固定要求
CRAWLER_MCP_OAUTH_ALGORITHMS=RS256
CRAWLER_MCP_OAUTH_REQUIRE_AT_JWT=true
CRAWLER_MCP_OAUTH_SCOPE_CLAIM=scope
CRAWLER_MCP_OAUTH_PERMISSIONS_CLAIM=permissions

# P0：每个 Auth0 身份拥有自己的数据空间
CRAWLER_MCP_OAUTH_TENANT_MODE=subject

# 换成实际域名；Origin 是允许调用服务的网页或网关来源
CRAWLER_MCP_ALLOWED_HOSTS=crawler.example.com
CRAWLER_MCP_ALLOWED_ORIGINS=https://console.example.com

# 至少 32 字节的随机值，只保存在部署平台 Secret 中
CRAWLER_MCP_APPROVAL_SIGNING_KEY=replace-with-a-random-secret-at-least-32-bytes
```

Auth0 模式会根据 Domain 自动得到：

- Issuer：`https://your-tenant.us.auth0.com/`（末尾斜杠也是签发方的一部分）
- JWKS：`https://your-tenant.us.auth0.com/.well-known/jwks.json`

一般不要再填写 `CRAWLER_MCP_ISSUER_URL` 和 `CRAWLER_MCP_OAUTH_JWKS_URL`；如果显式填写，Issuer 必须与 Auth0 Domain 精确匹配。

## 4. 先做公开配置预检

```bash
amazon-crawler-mcp-auth0-check
```

这个命令只读取 Auth0 的公开 Discovery 和 JWKS，检查 Issuer、JWKS 地址、HTTPS 授权/Token 端点、PKCE S256 和 RS256 公钥。它不会登录，也不会读取任何 Client Secret。

输出里的 `ok: true` 只代表公开配置通过；`manual_dashboard_checks` 仍要逐项确认。

## 5. 启动并做真实 Token 联调

```bash
amazon-crawler-mcp \
  --transport streamable-http \
  --host 0.0.0.0 \
  --port 8000 \
  --path /mcp \
  --json-response \
  --stateless-http
```

优先让支持 MCP OAuth 的 Host 访问公网 URL并完成 Auth0 登录。若要用仓库命令行 Client 做 Token 验证，可从 Auth0 API 的 **Test** 页面或经批准的测试 Client 获得短期 API Access Token，然后只放在当前终端环境中：

```bash
export CRAWLER_MCP_ACCESS_TOKEN='实际的短期 API Access Token'
amazon-crawler-mcp-client --url https://crawler.example.com/mcp smoke
```

必须使用目标 API 的 **Access Token**，不能用 ID Token。成功输出应同时证明：OAuth 握手成功、必要 Tool 全部可见、`crawler_doctor` 与 `crawler_capabilities` 可调用。

## 6. 后续团队租户模式

只有在需要“一个公司多成员共享任务和数据”时才切换到 Auth0 Organization：

```dotenv
CRAWLER_MCP_OAUTH_TENANT_MODE=claim
CRAWLER_MCP_OAUTH_TENANT_CLAIM=org_id
CRAWLER_MCP_OAUTH_ALLOWED_TENANTS=org_actual_id_1,org_actual_id_2
```

生产 claim 模式强制要求允许列表，避免任意 `org_id` 自动变成可信租户。切换前还要补成员邀请、离职回收、跨组织访问和历史数据迁移测试。

## 验收边界

- 自动化测试可证明 JWT 签名、Issuer、Audience、RFC 9068 `typ`、scope/permissions 合并和租户隔离逻辑。
- 公开预检可证明 Auth0 Discovery 与 JWKS 可访问且形状正确。
- 只有拿真实 Auth0 Access Token 完成公网 MCP `smoke`，才算真实 OAuth 联调通过。
- 本地测试或 `ok: true` 不代表已经允许部署、发布或开放商业访问。

实现依据：Auth0 的 [MCP Server 授权快速开始](https://auth0.com/ai/docs/mcp/get-started/authorization-for-your-mcp-server)、[Access Token Profiles](https://auth0.com/docs/secure/tokens/access-tokens/access-token-profiles)、[JWT Access Token 验证](https://auth0.com/docs/secure/tokens/access-tokens/validate-access-tokens)，以及 MCP 的 [Authorization 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)。
