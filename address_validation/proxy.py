from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass
class ProxySettings:
    http: str | None = None
    https: str | None = None
    no_proxy: str | None = None
    source: str = "none"

    @property
    def enabled(self) -> bool:
        return bool(self.http or self.https)

    def as_httpx_proxy(self) -> str | None:
        # httpx 0.28 accepts a single proxy URL. Prefer HTTPS proxy for public APIs.
        return self.https or self.http

    def redacted_summary(self) -> str:
        if not self.enabled:
            return "disabled"
        parts = []
        if self.http:
            parts.append(f"http={_redact_proxy_url(self.http)}")
        if self.https:
            parts.append(f"https={_redact_proxy_url(self.https)}")
        if self.no_proxy:
            parts.append(f"no_proxy={self.no_proxy}")
        return f"enabled via {self.source} ({', '.join(parts)})"


def get_proxy_settings(config: dict[str, Any] | None = None) -> ProxySettings:
    """
    Resolve proxy settings without requiring secrets in committed config.

    Priority:
    1. Environment variables
       - ADDRESS_VALIDATION_HTTP_PROXY / ADDRESS_VALIDATION_HTTPS_PROXY
       - HTTP_PROXY / HTTPS_PROXY / ALL_PROXY
       - NO_PROXY / no_proxy
    2. Local-only config keys under proxy: (intended for config.local.yaml)
    """
    env_http = (
        os.environ.get("ADDRESS_VALIDATION_HTTP_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    env_https = (
        os.environ.get("ADDRESS_VALIDATION_HTTPS_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or env_http
    )
    env_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")

    if env_http or env_https:
        return ProxySettings(
            http=env_http,
            https=env_https,
            no_proxy=env_no_proxy,
            source="environment",
        )

    proxy_config = (config or {}).get("proxy") or {}
    local_http = proxy_config.get("http") or proxy_config.get("url")
    local_https = proxy_config.get("https") or local_http
    local_no_proxy = proxy_config.get("no_proxy")
    if local_http or local_https:
        return ProxySettings(
            http=local_http,
            https=local_https,
            no_proxy=local_no_proxy,
            source="local config",
        )

    return ProxySettings()


def apply_no_proxy_env(settings: ProxySettings) -> None:
    """Ensure NO_PROXY is visible to httpx trust_env handling when set locally."""
    if settings.no_proxy:
        os.environ.setdefault("NO_PROXY", settings.no_proxy)
        os.environ.setdefault("no_proxy", settings.no_proxy)


def _redact_proxy_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = "***@" if parts.username else ""
    netloc = f"{user}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
