from __future__ import annotations

import re

from amazon_crawler.domain.models import CrawlFailure


BLOCKED_MARKERS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "sentimos las molestias. necesitamos asegurarnos de que no eres un robot",
    "desculpe pelo inconveniente. para continuar realizando suas compras",
    "lo sentimos, tenemos que asegurarnos de que eres una persona",
    "wir bitten um ihr verständnis und wollen uns sicher sein dass sie kein bot sind",
    "désolés, il faut que nous nous assurions que vous n'êtes pas un robot",
    "questa operazione è utile per capire se l'utente è un robot",
    "sorry, we moeten er alleen voor zorgen dat je geen robot bent",
    "przepraszamy, musimy tylko upewnić się, że nie jesteś robotem",
    "jag är ledsen, vi måste bara säkerställa att du inte är en robot",
    "our servers are getting hit pretty hard right now",
    "üzgünüz, sadece robot olmadığınızdan emin olmalıyız",
    "in order to continue, we need to verify that you're not a robot",
    "automated access to amazon data",
    'action="/errors/validatecaptcha"',
    "bm-verify=",
)
TEMPORARY_MARKERS = (
    "we're sorry, an error has occurred. please reload this page and try again",
    "request was throttled. please wait a moment and refresh the page",
    '"isempty": true',
)
PRODUCT_PAGE_NOT_FOUND_MARKERS = (
    "sorry! we couldn't find that page. try searching or go to amazon's home page.",
    "desculpe! não conseguimos encontrar esta página. pesquise novamente ou volte para a página inicial",
    "lo sentimos! no pudimos encontrar la página que buscabas. trata de usar la barra de búsqueda o visita la página principal",
    "we’re sorry. the web address you entered is not a functioning page on our site.",
    "we're sorry. the web address you've entered is not a functioning page on our site.",
    "üzgünüz. girdiğiniz web adresi, sitemizde işlev gösteren bir sayfaya karşılık gelmiyor.",
    "przepraszamy. wyszukiwana strona nie istnieje.",
    "nous sommes désolés. l'adresse web que vous avez saisie n'est pas une page fonctionnelle de notre site.",
    "siamo spiacenti. l'indirizzo web inserito non è una pagina funzionante sul nostro sito.",
    "vi ber om ursäkt. webbadressen du har angett är inte en fungerande sida på vår webbplats.",
    "lo sentimos. la dirección web que has especificado no es una página activa de nuestro sitio.",
    "this page is unavailable, usually because you're not a customer in the country or region where we have rights for this event.",
)
SEARCH_NO_RESULT_MARKERS = (
    "no results for",
    "no hay resultados para",
    "nenhum resultado para",
    "daha az anahtar kelime",
    "nie znaleziono wyników dla",
)
MERCHANT_NO_RESULT_MARKERS = (
    "no results for",
    "no hay resultados para",
    "nenhum resultado para",
    "brak wyników dla",
    "arama sorgunuz için",
)
RANK_NO_RESULT_MARKERS = (
    *PRODUCT_PAGE_NOT_FOUND_MARKERS,
    "sorry, there are no best sellers available in this category",
    "desculpe, no momento não temos uma lista de mais vendidos nessa categoria",
    "lo sentimos, no hay productos más vendidos disponibles en esta categoría",
    "niestety, w tej kategorii nie ma bestsellerów",
    "üzgünüz, bu kategoride hiç çok satan ürün yok",
)
MERCHANT_DETAIL_NOT_FOUND_MARKERS = (
    "sorry! we couldn't find that page. try searching or go to amazon's home page.",
    "welcome to amazon customer service",
)


def _terminal_not_found(code: str) -> CrawlFailure:
    return CrawlFailure(
        code,
        "Amazon returned an unavailable or empty page",
        retryable=False,
    )


def classify_amazon_page(
    source: str,
    *,
    not_found_code: str,
    purpose: str,
) -> CrawlFailure | None:
    """Classify legacy Amazon interstitials without exposing response content."""

    sample = source[:200_000].lower()
    if any(marker in sample for marker in BLOCKED_MARKERS):
        return CrawlFailure(
            "blocked_page",
            "Amazon returned a verification page",
            retryable=True,
        )
    # The old validators do not share one global no-result policy.  Product and
    # review pages check page/address failures before temporary markers; rank
    # pages do the same for empty-list copy.  Search/category/merchant list
    # parsers check temporary markers first and must therefore retry a response
    # that happens to contain both signals.
    if purpose in {"product", "reviews"} and any(
        marker in sample for marker in PRODUCT_PAGE_NOT_FOUND_MARKERS
    ):
        return _terminal_not_found(not_found_code)
    if purpose == "merchant" and any(
        marker in sample for marker in MERCHANT_DETAIL_NOT_FOUND_MARKERS
    ):
        return _terminal_not_found(not_found_code)
    if purpose == "rank_list" and any(
        marker in sample for marker in RANK_NO_RESULT_MARKERS
    ):
        return _terminal_not_found(not_found_code)
    if any(marker in sample for marker in TEMPORARY_MARKERS):
        return CrawlFailure(
            "upstream_incomplete",
            "Amazon returned a throttled or incomplete page",
            retryable=True,
        )
    if purpose in {"search", "search_hour", "category_asin_list"} and any(
        marker in sample for marker in SEARCH_NO_RESULT_MARKERS
    ):
        return _terminal_not_found(not_found_code)
    if purpose in {"merchant_home", "merchant_products"} and any(
        marker in sample for marker in MERCHANT_NO_RESULT_MARKERS
    ):
        return _terminal_not_found(not_found_code)
    return None


def classify_product_completeness(source: str) -> CrawlFailure | None:
    """Reproduce the old product/review partial-page acceptance boundary."""

    has_histogram = (
        'id="cm_cr_dp_d_rating_histogram"' in source
        or "id='cm_cr_dp_d_rating_histogram'" in source
    )
    if not has_histogram and len(source) < 350_000:
        return CrawlFailure(
            "upstream_incomplete",
            "Amazon response omitted the review histogram on a short detail page",
            retryable=True,
        )
    if "jQuery.parseJSON" not in source:
        return CrawlFailure(
            "upstream_incomplete",
            "Amazon response omitted required detail-page state",
            retryable=True,
        )
    page_type = re.search(r'page:\{pageType: "(.*?)", subPageType:', source)
    if page_type and page_type.group(1) != "Detail":
        return CrawlFailure(
            "unsupported_product_type",
            "Amazon response is not a detail page",
            retryable=False,
        )
    if page_type and re.search(
        r'"pageModel":\{"pageTitle":"(.*?)","pageDescription"', source
    ):
        return CrawlFailure(
            "unsupported_product_type",
            "Amazon response is an unsupported restricted detail type",
            retryable=False,
        )
    return None


def classify_collection_completeness(
    kind: str,
    source: str,
    *,
    page: int = 1,
) -> CrawlFailure | None:
    """Reject structurally partial pages that the old parsers retried."""

    if kind in {"search", "search_hour"}:
        reversed_range = re.search(
            r"(\d+)\s*(?:-|–|\s+a\s+)\s*(\d+)\s+(?:of|de|z|/)",
            source,
            re.IGNORECASE,
        )
        if (
            page > 1
            and reversed_range
            and int(reversed_range.group(1)) > int(reversed_range.group(2))
        ):
            return CrawlFailure(
                "one_page_only",
                "Amazon indicates the search has only one page",
                retryable=False,
            )
        asins = re.findall(
            r'(?:\\"|\")listitem(?:\\"|\") data-asin=(?:\\"|\")([A-Z0-9]{10})(?:\\"|\")',
            source,
        )
        has_total = bool(
            re.search(r'(?:\\?"?)totalResultCount(?:\\?")?\s*:\s*[1-9]\d*', source)
            or re.search(
                r'<span>.*?\d+\s+(?:results|resultados|wyników|sonuç).*?</span>',
                source,
                re.IGNORECASE,
            )
        )
        if not asins or not has_total:
            return CrawlFailure(
                "upstream_incomplete",
                "Amazon search stream omitted result identity or result totals",
                retryable=True,
            )
    elif kind == "category_asin_list":
        has_rows = bool(re.search(r'role=["\']listitem["\'][^>]*data-asin=', source))
        has_total_or_node = (
            'id="apb-desktop-browse-search-see-all"' in source
            or "id='apb-desktop-browse-search-see-all'" in source
            or bool(
                re.search(
                    r'<span>.*?\d+\s+(?:results|resultados|wyników|sonuç).*?</span>',
                    source,
                    re.IGNORECASE,
                )
            )
        )
        if not has_rows or not has_total_or_node:
            return CrawlFailure(
                "upstream_incomplete",
                "Amazon category page omitted result rows or totals",
                retryable=True,
            )
    elif kind == "rank_list":
        if not re.search(r'data-client-recs-list=["\'][^"\']+', source):
            return CrawlFailure(
                "upstream_incomplete",
                "Amazon rank page omitted ranking metadata",
                retryable=True,
            )
    elif kind in {"merchant_home", "merchant_products"}:
        total = re.search(
            r'(?:\\?"?)totalResultCount(?:\\?")?\s*:\s*(\d+)', source
        )
        if not total:
            return CrawlFailure(
                "upstream_incomplete",
                "Amazon merchant page omitted product totals",
                retryable=True,
            )
        if int(total.group(1)) == 0:
            return CrawlFailure(
                "merchant_has_no_products"
                if kind == "merchant_home"
                else "merchant_page_has_no_products",
                "merchant has no products",
                retryable=False,
            )
    return None
