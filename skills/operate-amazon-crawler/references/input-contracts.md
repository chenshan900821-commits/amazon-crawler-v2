# Supported input contracts

Pass product ASINs or recognized product URLs as positional inputs. Pass every other task as one `--input-json` object. Use only fields supplied by the user or stable defaults shown here.

| Kind | Required fields | Optional fields |
|---|---|---|
| `product`, `product_hw`, `product_time` | positional ASIN/URL and marketplace for an ASIN | postal code |
| `search`, `search_hour` | `keyword`, `market_id`, `turn_page` | `post_code`, `frequent`, `data_hour`, `add_date` |
| `reviews` | `asin`, `market_id` | `post_code`, `add_date` |
| `category_asin_list` | `category_id`, `market_id`, `page` | `post_code`, `add_date` |
| `rank_list` | Amazon `url`, `url_type`, `category_id`, `market_id`, `page` | none |
| `merchant` | `seller_id`, `market_id` | none |
| `merchant_home` | `seller_id`, `market_id` | `post_code`, `add_date`, Amazon `object_url`, `source_task_id` |
| `merchant_products` | `seller_id`, `market_id`, `page` | `post_code`, `add_date`, `source_task_id` |

Use `url_type` value `bestsellers` or `new-releases`. Rank URLs and merchant `object_url` values must be HTTPS URLs on the selected Amazon marketplace. Never add cookies, proxies, headers, credentials, SQL, or parser code to task input.

Leave `--max-attempts` unset unless the user explicitly requests a different retry budget. The crawler then preserves legacy behavior: 5 total attempts for ordinary tasks and 11 for `search_hour`.

## Result sink option

Pass one or more `--result-sink NAME` flags only with names returned by `capabilities`. `sqlite` is always included. `jsonl` is a local export. `legacy_mysql` and `legacy_redis` write configured legacy destinations and require explicit user confirmation through the Skill wrapper. Never pass endpoints, credentials, table names, Redis keys, or output paths.
