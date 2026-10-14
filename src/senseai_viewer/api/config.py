# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""Configuration from environment variables only."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["ConfigError", "Settings"]


class ConfigError(RuntimeError):
    """The environment does not describe a runnable server."""


@dataclass(frozen=True)
class Settings:
    """Server settings. Build them with :meth:`from_env`."""

    s3_bucket: str | None = None
    s3_prefix: str = "viewer"
    aws_region: str | None = None
    cache_ttl_s: float = 60.0
    password: str | None = None
    local_summaries: str | None = None
    production: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Read ``S3_BUCKET``, ``S3_PREFIX``, ``AWS_REGION``, ``CACHE_TTL_S``,
        ``VIEWER_PASSWORD``, ``LOCAL_SUMMARIES`` and ``RAILWAY_ENVIRONMENT``.
        """  # noqa: D205
        e = os.environ if env is None else env
        try:
            ttl = float(e.get("CACHE_TTL_S") or 60)
        except ValueError as exc:
            raise ConfigError(f"CACHE_TTL_S must be a number, got {e['CACHE_TTL_S']!r}") from exc
        return cls(
            s3_bucket=e.get("S3_BUCKET") or None,
            s3_prefix=(e.get("S3_PREFIX") or "viewer").strip("/"),
            aws_region=e.get("AWS_REGION") or None,
            cache_ttl_s=max(0.0, ttl),
            password=e.get("VIEWER_PASSWORD") or None,
            local_summaries=e.get("LOCAL_SUMMARIES") or None,
            production=bool(e.get("RAILWAY_ENVIRONMENT")),
        )

    def validate(self) -> None:
        """Raise :class:`ConfigError` if the server must not start with these settings."""
        if self.production and not self.password:
            raise ConfigError(
                "VIEWER_PASSWORD is not set: refusing to start in production "
                "(RAILWAY_ENVIRONMENT is set)"
            )
        if not self.local_summaries and not self.s3_bucket:
            raise ConfigError("set S3_BUCKET (or LOCAL_SUMMARIES=<folder> for local use)")
