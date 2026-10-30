"""Provider backends."""

from __future__ import annotations

from pathlib import Path

import httpx

from taxameter.config import Config, ConfigError
from taxameter.providers.base import Provider

PROVIDERS = ("vast",)


def get_provider(cfg: Config, transport: httpx.BaseTransport | None = None) -> Provider:
    name = cfg.provider.name
    if name == "vast":
        from taxameter.providers.vast import VastProvider

        key = cfg.provider.api_key_file
        return VastProvider(Path(key).expanduser() if key else None, transport)
    raise ConfigError(f"unknown provider {name!r} in [provider] (known: {', '.join(PROVIDERS)})")
