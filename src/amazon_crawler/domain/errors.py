class CrawlerError(Exception):
    """Base exception for controlled crawler failures."""


class ValidationError(CrawlerError):
    pass


class NotFoundError(CrawlerError):
    pass


class ConflictError(CrawlerError):
    pass
