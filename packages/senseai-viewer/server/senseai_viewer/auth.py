# Copyright 2026 SenseAI Institute
# SPDX-License-Identifier: Apache-2.0
"""HTTP Basic auth for the whole app when ``VIEWER_PASSWORD`` is set.

The username is ignored; the password must match. Health checks stay open so Railway and
load balancers can probe the service. Written as plain ASGI middleware so streaming
responses (the runner's Server-Sent Events) pass through unbuffered.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

OPEN_PATHS = {"/api/health", "/healthz"}


def password_ok(header: str | None, password: str) -> bool:
    """Whether an ``Authorization: Basic …`` header carries ``password``."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    _, _, given = decoded.partition(":")
    return secrets.compare_digest(given.encode(), password.encode())


class BasicAuth:
    """Require the shared password on every HTTP request except :data:`OPEN_PATHS`."""

    def __init__(self, app: ASGIApp, password: str) -> None:
        self.app = app
        self.password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on if it is open or carries the password; else answer 401."""
        if scope["type"] != "http" or scope.get("path") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        auth = headers.get("authorization", "")
        token_route = (
            scope.get("method") in ("PUT", "DELETE")
            and scope.get("path", "").startswith("/api/runs")
        ) or (scope.get("method") == "POST" and scope.get("path") == "/api/runs/import")
        if token_route and auth.lower().startswith("bearer "):
            await self.app(
                scope, receive, send
            )  # upload/import/delete check UPLOAD_TOKEN themselves
            return
        if password_ok(auth, self.password):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="SenseAI viewer", charset="UTF-8"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Password required."})
