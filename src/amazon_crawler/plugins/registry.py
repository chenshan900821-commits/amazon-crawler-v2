from __future__ import annotations

from amazon_crawler.domain.errors import ValidationError
from amazon_crawler.domain.ports import CrawlPlugin


class PluginRegistry:
    def __init__(self, plugins: list[CrawlPlugin]) -> None:
        self._plugins = {plugin.kind: plugin for plugin in plugins}

    def get(self, kind: str) -> CrawlPlugin:
        plugin = self._plugins.get(kind)
        if not plugin:
            raise ValidationError(f"unsupported crawler kind: {kind}")
        return plugin

    def capabilities(self) -> list[dict[str, object]]:
        input_contracts = {
            "amazon.product": ["ASIN", "Amazon product URL"],
            "product": ["ASIN", "Amazon product URL"],
            "product_jp": ["ASIN", "Amazon product URL"],
            "product_hw": ["ASIN", "Amazon product URL"],
            "product_hw_jp": ["ASIN", "Amazon product URL"],
            "product_time": ["ASIN", "Amazon product URL"],
            "product_time_jp": ["ASIN", "Amazon product URL"],
            "search": ["keyword object"],
            "search_jp": ["keyword object"],
            "search_hour": ["hourly keyword object"],
            "search_hour_jp": ["hourly keyword object"],
            "reviews": ["ASIN object"],
            "category_asin_list": ["category/page object"],
            "asin_list_jp": ["category/page object"],
            "rank_list": ["allowlisted Amazon rank URL object"],
            "rank_list_jp": ["allowlisted Amazon rank URL object"],
            "merchant": ["seller object"],
            "merchant_home": ["seller discovery object"],
            "merchant_products": ["seller/page object"],
        }
        return [
            {
                "kind": kind,
                "inputs": input_contracts[kind],
                "execution_modes": ["standard", "overseas", "realtime"],
                "resume": True,
                "evidence": True,
            }
            for kind in sorted(self._plugins)
        ]
