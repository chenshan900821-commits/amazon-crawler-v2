from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse


@dataclass(frozen=True, slots=True)
class Marketplace:
    id: str
    name: str
    domain: str
    amazon_marketplace_id: str


MARKETPLACES = {
    market.id: market
    for market in [
        Marketplace("US", "United States", "www.amazon.com", "ATVPDKIKX0DER"),
        Marketplace("CA", "Canada", "www.amazon.ca", "A2EUQ1WTGCTBG2"),
        Marketplace("MX", "Mexico", "www.amazon.com.mx", "A1AM78C64UM0Y8"),
        Marketplace("BR", "Brazil", "www.amazon.com.br", "A2Q3Y263D00KWC"),
        Marketplace("UK", "United Kingdom", "www.amazon.co.uk", "A1F83G8C2ARO7P"),
        Marketplace("DE", "Germany", "www.amazon.de", "A1PA6795UKMFR9"),
        Marketplace("FR", "France", "www.amazon.fr", "A13V1IB3VIYZZH"),
        Marketplace("IT", "Italy", "www.amazon.it", "APJ6JRA9NG5V4"),
        Marketplace("ES", "Spain", "www.amazon.es", "A1RKKUPIHCS9HS"),
        Marketplace("NL", "Netherlands", "www.amazon.nl", "A1805IZSGTT6HS"),
        Marketplace("SE", "Sweden", "www.amazon.se", "A2NODRKZP88ZB9"),
        Marketplace("PL", "Poland", "www.amazon.pl", "A1C3SOZRARQ6R3"),
        Marketplace("BE", "Belgium", "www.amazon.com.be", "AMEN7PMS3EDWL"),
        Marketplace("IE", "Ireland", "www.amazon.ie", "A28R8C7NBKEWEA"),
        Marketplace("ZA", "South Africa", "www.amazon.co.za", "AE08WJ6YKNBMC"),
        Marketplace("EG", "Egypt", "www.amazon.eg", "ARBP9OOSHTCHU"),
        Marketplace("JP", "Japan", "www.amazon.co.jp", "A1VC38T7YXB528"),
        Marketplace("AU", "Australia", "www.amazon.com.au", "A39IBJ37TRP1C6"),
        Marketplace("IN", "India", "www.amazon.in", "A21TJRUUN4KGV"),
        Marketplace("SG", "Singapore", "www.amazon.sg", "A19VAU5U5O7RUS"),
        Marketplace("AE", "United Arab Emirates", "www.amazon.ae", "A2VIGQ35RCS4UG"),
        Marketplace("SA", "Saudi Arabia", "www.amazon.sa", "A17E79C6D8DWNP"),
        Marketplace("TR", "Turkey", "www.amazon.com.tr", "A33AVAJ2PDY3EV"),
    ]
}

DOMAIN_TO_MARKETPLACE = {
    market.domain.removeprefix("www."): market for market in MARKETPLACES.values()
}

AMAZON_ID_TO_MARKETPLACE = {
    market.amazon_marketplace_id: market for market in MARKETPLACES.values()
}

MARKETPLACE_COOKIE_ALIASES = {
    market.id: market.amazon_marketplace_id for market in MARKETPLACES.values()
}

# One deterministic delivery region per marketplace for the operator-facing
# Cookie acquisition flow. These values preserve the first operational choice
# from the existing site configuration. Marketplaces without a configured,
# exercised value are intentionally omitted instead of receiving a guessed one.
MARKETPLACE_DEFAULT_POSTAL_CODES = {
    "US": "10001",
    "CA": "J0N 1P0",
    "MX": "45645",
    "BR": "18680-693",
    "UK": "EC1A 1HQ",
    "DE": "16515",
    "FR": "75000",
    "IT": "10040",
    "ES": "28001",
    "NL": "1079",
    "SE": "413 08",
    "PL": "21-030",
    "BE": "1050",
    "IE": "D02 R5Y3",
    "ZA": "Johannesburg",
    "JP": "140-0001",
    "AU": "2000",
    "SG": "699010",
    "AE": "Dubai",
    "SA": "Riyadh",
    "TR": "34000",
}

MARKETPLACE_LANGUAGES = {
    "CA": "en_CA",
    "US": "en_US",
    "MX": "",
    "BR": "",
    "IE": "en_IE",
    "ES": "en_GB",
    "UK": "en_GB",
    "FR": "en_GB",
    "BE": "en_GB",
    "NL": "en_GB",
    "DE": "en_GB",
    "IT": "en_GB",
    "SE": "en_GB",
    "ZA": "",
    "PL": "",
    "EG": "en_AE",
    "TR": "",
    "SA": "en_AE",
    "AE": "en_AE",
    "IN": "en_US",
    "SG": "",
    "AU": "",
    "JP": "en_US",
}

SENSITIVE_URL_KEYS = {
    "auth",
    "authorization",
    "credential",
    "key",
    "password",
    "secret",
    "session",
    "signature",
    "token",
}


def url_contains_sensitive_material(url: str) -> bool:
    """Reject URLs whose persisted query keys look credential-bearing."""

    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.fragment:
        return True
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = "".join(character for character in key.lower() if character.isalnum())
        if normalized in SENSITIVE_URL_KEYS or normalized.endswith(
            (
                "authorization",
                "credential",
                "password",
                "secret",
                "signature",
                "token",
                "apikey",
                "accesskey",
            )
        ):
            return True
    return False


def marketplace_language(marketplace_code: str) -> str:
    return MARKETPLACE_LANGUAGES.get(marketplace_code, "")


def merchant_language(marketplace_code: str) -> str:
    return "en_AE" if marketplace_code in {"EG", "AE"} else "en"


def marketplace_default_postal_code(marketplace_id: str) -> str | None:
    normalized = marketplace_id.strip().upper()
    market = MARKETPLACES.get(normalized) or AMAZON_ID_TO_MARKETPLACE.get(normalized)
    if market is None:
        return None
    return MARKETPLACE_DEFAULT_POSTAL_CODES.get(market.id)


def public_marketplaces() -> list[dict[str, str | None]]:
    return [
        {
            "id": market.id,
            "name": market.name,
            "domain": market.domain,
            "amazon_marketplace_id": market.amazon_marketplace_id,
            "default_postal_code": MARKETPLACE_DEFAULT_POSTAL_CODES.get(market.id),
        }
        for market in MARKETPLACES.values()
    ]
