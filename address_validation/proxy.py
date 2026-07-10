from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit


DEFAULT_NO_PROXY = "ase.testingaddress.com,.testingaddress.com,localhost,127.0.0.1"


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


def merge_no_proxy(*values: str | None) -> str:
    entries: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for item in value.split(","):
            cleaned = item.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(cleaned)
    return ",".join(entries)


def host_bypasses_proxy(url: str, no_proxy: str | None) -> bool:
    """
    Return True when the URL host should skip the company proxy.

    Chrome PAC files usually bypass intranet hosts; Python must do this explicitly
    when a proxy is configured, otherwise intranet calls often return HTTP 504.
    """
    if not no_proxy:
        return False

    hostname = (urlsplit(url).hostname or "").lower()
    if not hostname:
        return False

    for entry in no_proxy.split(","):
        pattern = entry.strip().lower()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if pattern.startswith("."):
            if hostname.endswith(pattern) or hostname == pattern.lstrip("."):
                return True
        elif hostname == pattern or hostname.endswith("." + pattern):
            return True
    return False


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

    proxy_config = (config or {}).get("proxy") or {}
    local_http = proxy_config.get("http") or proxy_config.get("url")
    local_https = proxy_config.get("https") or local_http
    local_no_proxy = proxy_config.get("no_proxy")

    no_proxy = merge_no_proxy(DEFAULT_NO_PROXY, env_no_proxy, local_no_proxy)

    if env_http or env_https:
        return ProxySettings(
            http=env_http,
            https=env_https,
            no_proxy=no_proxy,
            source="environment",
        )

    if local_http or local_https:
        return ProxySettings(
            http=local_http,
            https=local_https,
            no_proxy=no_proxy,
            source="local config",
        )

    return ProxySettings(no_proxy=no_proxy)


def apply_no_proxy_env(settings: ProxySettings) -> None:
    """Ensure NO_PROXY is visible to httpx trust_env handling when set locally."""
    if settings.no_proxy:
        # Always merge defaults so intranet hosts are not forced through proxy.
        merged = merge_no_proxy(DEFAULT_NO_PROXY, os.environ.get("NO_PROXY"), settings.no_proxy)
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged


def _redact_proxy_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    user = "***@" if parts.username else ""
    netloc = f"{user}{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
