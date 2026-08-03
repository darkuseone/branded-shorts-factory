"""Provider registry, ordered by the project's free-source priority."""

from __future__ import annotations

from ...config import FREE_STOCK_PRIORITY, Settings
from .base import StockProvider
from .internet_archive import InternetArchiveProvider
from .mixkit import MixkitProvider
from .nasa import NasaProvider
from .pexels import PexelsProvider
from .pixabay import PixabayProvider
from .pond5 import Pond5FreeProvider
from .wikimedia import WikimediaProvider

PROVIDER_CLASSES: dict[str, type[StockProvider]] = {
    PexelsProvider.name: PexelsProvider,
    PixabayProvider.name: PixabayProvider,
    MixkitProvider.name: MixkitProvider,
    NasaProvider.name: NasaProvider,
    WikimediaProvider.name: WikimediaProvider,
    InternetArchiveProvider.name: InternetArchiveProvider,
    Pond5FreeProvider.name: Pond5FreeProvider,
}


def build_providers(settings: Settings, only: list[str] | None = None) -> list[StockProvider]:
    """Instantiate providers in priority order, optionally filtered by name."""
    names = [name for name in FREE_STOCK_PRIORITY if name in PROVIDER_CLASSES]
    if only:
        wanted = {name.strip().lower() for name in only}
        names = [name for name in names if name in wanted]
    return [PROVIDER_CLASSES[name](settings) for name in names]


__all__ = [
    "InternetArchiveProvider",
    "MixkitProvider",
    "NasaProvider",
    "PROVIDER_CLASSES",
    "PexelsProvider",
    "PixabayProvider",
    "Pond5FreeProvider",
    "StockProvider",
    "WikimediaProvider",
    "build_providers",
]
