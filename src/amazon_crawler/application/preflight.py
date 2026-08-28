from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from amazon_crawler.config import Settings


SECRET_LOCATION = (
    "项目根目录 .env 或部署平台 Secret；不要把真实值粘贴到 Agent 对话、"
    "任务参数或 Git 中。"
)


def _url_is_usable(value: str | None, schemes: set[str]) -> bool:
    if not value:
        return False
    try:
        parsed = urlparse(value)
        return parsed.scheme.lower() in schemes and bool(parsed.hostname)
    except ValueError:
        return False


def configuration_report(settings: Settings) -> dict[str, Any]:
    """Return a secret-free deployment configuration report.

    This only verifies that configuration is present and structurally plausible.
    It deliberately does not contact Amazon, Redis, or a proxy provider, and it
    cannot prove that a Worker process is currently alive.
    """

    blocking_issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    cookie_redis_configured = bool(settings.cookie_redis_url)
    static_cookie_configured = bool(settings.amazon_cookie)
    cookie_configured = cookie_redis_configured or static_cookie_configured
    if cookie_redis_configured:
        cookie_source = "redis_pool"
    elif static_cookie_configured:
        cookie_source = "static_cookie"
    else:
        cookie_source = "none"

    if settings.require_cookie and not cookie_configured:
        blocking_issues.append(
            {
                "code": "COOKIE_SOURCE_MISSING",
                "message": (
                    "当前 CRAWLER_REQUIRE_COOKIE=true，但没有配置可供普通抓取使用的 "
                    "Cookie 来源。"
                ),
                "configure_one_of": [
                    "CRAWLER_COOKIE_REDIS_URL",
                    "CRAWLER_AMAZON_COOKIE",
                ],
                "where": SECRET_LOCATION,
            }
        )

    dynamic_proxy = bool(settings.proxy_extract_url)
    fixed_proxy = bool(settings.http_proxy)
    if dynamic_proxy:
        proxy_mode = "dynamic_extraction_api"
    elif fixed_proxy:
        proxy_mode = "fixed_connection_url"
    else:
        proxy_mode = "direct"

    proxy_credentials_complete = bool(settings.proxy_username) == bool(
        settings.proxy_password
    )
    if dynamic_proxy and not proxy_credentials_complete:
        blocking_issues.append(
            {
                "code": "PROXY_CREDENTIALS_INCOMPLETE",
                "message": (
                    "动态代理隧道账号和密码只配置了一个；需要同时配置，或在 IP 白名单"
                    "模式下同时留空。"
                ),
                "configure_together": [
                    "CRAWLER_PROXY_USERNAME",
                    "CRAWLER_PROXY_PASSWORD",
                ],
                "where": SECRET_LOCATION,
            }
        )
    if dynamic_proxy and not _url_is_usable(
        settings.proxy_extract_url, {"http", "https"}
    ):
        blocking_issues.append(
            {
                "code": "PROXY_EXTRACT_URL_INVALID",
                "message": "CRAWLER_PROXY_EXTRACT_URL 必须是完整的 http(s) 提取接口链接。",
                "configure": ["CRAWLER_PROXY_EXTRACT_URL"],
                "where": SECRET_LOCATION,
            }
        )
    if fixed_proxy and not _url_is_usable(settings.http_proxy, {"http", "https"}):
        blocking_issues.append(
            {
                "code": "HTTP_PROXY_URL_INVALID",
                "message": "CRAWLER_HTTP_PROXY 必须是完整的 http(s) 代理连接 URL。",
                "configure": ["CRAWLER_HTTP_PROXY"],
                "where": SECRET_LOCATION,
            }
        )
    if dynamic_proxy and fixed_proxy:
        warnings.append(
            {
                "code": "DYNAMIC_PROXY_TAKES_PRECEDENCE",
                "message": (
                    "两种代理都已配置；运行时使用 CRAWLER_PROXY_EXTRACT_URL，"
                    "CRAWLER_HTTP_PROXY 不会作为失败回退。"
                ),
            }
        )
    if not dynamic_proxy and not fixed_proxy:
        warnings.append(
            {
                "code": "PROXY_NOT_CONFIGURED",
                "message": (
                    "未配置代理，普通抓取将尝试直连。若当前网络或授权方案要求代理，"
                    "请先配置一种代理；Cookie 生产默认要求代理。"
                ),
                "configure_one_of": [
                    "CRAWLER_PROXY_EXTRACT_URL",
                    "CRAWLER_HTTP_PROXY",
                ],
            }
        )

    next_steps: list[str] = []
    if blocking_issues:
        next_steps.extend(
            [
                "编辑项目根目录 .env，只填写检查结果列出的环境变量。",
                "在当前终端执行：set -a; source .env; set +a",
                "重新运行 amazon-crawler doctor；Agent Skill 则重新运行 doctor 后再 create。",
            ]
        )
    else:
        next_steps.extend(
            [
                "配置项检查已通过；这不代表 Redis、代理或 Amazon 已完成真实连通性验证。",
                "确认另一个进程正在运行 amazon-crawler serve 或 amazon-crawler worker。",
            ]
        )

    return {
        "ok": True,
        "configuration_ready": not blocking_issues,
        "checks": {
            "cookie": {
                "required": settings.require_cookie,
                "configured": cookie_configured,
                "selected_source": cookie_source,
                "redis_pool_configured": cookie_redis_configured,
                "static_cookie_configured": static_cookie_configured,
                "overseas_pool_configured": bool(
                    settings.cookie_redis_overseas_url
                ),
                "values_disclosed": False,
            },
            "proxy": {
                "required_for_ordinary_crawl": False,
                "required_for_cookie_harvest": settings.cookie_harvest_require_proxy,
                "configured": dynamic_proxy or fixed_proxy,
                "selected_mode": proxy_mode,
                "dynamic_extract_url_configured": dynamic_proxy,
                "fixed_connection_url_configured": fixed_proxy,
                "dynamic_tunnel_credentials": (
                    "configured"
                    if settings.proxy_username and settings.proxy_password
                    else "not_configured"
                    if not settings.proxy_username and not settings.proxy_password
                    else "incomplete"
                ),
                "precedence": "dynamic_extraction_api_over_fixed_connection_url",
                "values_disclosed": False,
            },
            "worker": {
                "runtime_status": "not_verified",
                "serve_will_start_embedded_worker": settings.worker_enabled,
                "message": (
                    "配置检查无法证明 Worker 进程正在运行；创建任务后必须用 show "
                    "确认状态离开 pending。"
                ),
            },
            "storage": {
                "state_database_configured": True,
                "canonical_sink": "sqlite",
                "values_disclosed": False,
            },
        },
        "blocking_issues": blocking_issues,
        "warnings": warnings,
        "next_steps": next_steps,
    }
