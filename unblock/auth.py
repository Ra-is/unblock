import os
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel

from unblock.config import settings


class Identity(BaseModel):
    tenant: str
    actor: str
    reviewer: bool = False
    # A shared public account. It may read its own tenant and change nothing.
    demo: bool = False


@lru_cache
def jwks(url):
    return jwt.PyJWKClient(url, cache_keys=True)


def identity(authorization: str | None = Header(default=None)) -> Identity:
    cfg = settings()
    if (
        cfg.app_env == "local"
        and not os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        and not cfg.unblock_user_pool_id
    ):
        return Identity(tenant="local", actor="local-reviewer", reviewer=True)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to access your workspace.")
    if not cfg.unblock_user_pool_id:
        raise HTTPException(503, "Authentication is not configured.")
    issuer = (
        f"https://cognito-idp.{cfg.aws_default_region}.amazonaws.com/{cfg.unblock_user_pool_id}"
    )
    token = authorization[7:]
    try:
        signing_key = jwks(issuer + "/.well-known/jwks.json").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=cfg.unblock_client_id,
            issuer=issuer,
            options={"require": ["exp", "iat", "sub", "token_use"]},
        )
        if claims["token_use"] != "id":
            raise ValueError("ID token required")
        tenant = claims.get("custom:tenant")
        import re

        if not tenant or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", tenant):
            raise ValueError("Organization membership required")
        groups = claims.get("cognito:groups", [])
        demo = "demo" in groups or tenant == "demo"
        return Identity(
            tenant=tenant,
            actor=claims["sub"],
            # A demo account is never a reviewer, whatever its group membership says.
            reviewer="reviewer" in groups and not demo,
            demo=demo,
        )
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(401, "Your session is invalid or expired.") from None


def writer(user: Annotated[Identity, Depends(identity)]) -> Identity:
    if user.demo:
        raise HTTPException(
            403, "This is a read-only demo. Deploy your own workspace to make changes."
        )
    return user
