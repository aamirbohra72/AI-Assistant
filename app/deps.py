import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.config import get_settings


def _check(expected: str, provided: str | None) -> None:
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing credentials")


async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    _check(get_settings().ADMIN_API_KEY, x_api_key)


async def require_internal_token(x_internal_token: Annotated[str | None, Header()] = None) -> None:
    _check(get_settings().INTERNAL_API_TOKEN, x_internal_token)
