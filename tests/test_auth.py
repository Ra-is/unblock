from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from unblock.api import app
from unblock.config import Settings


@pytest.fixture
def signing(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cfg = Settings(
        app_env="dev", unblock_user_pool_id="eu-west-2_test", unblock_client_id="client-test"
    )
    monkeypatch.setattr("unblock.auth.settings", lambda: cfg)
    monkeypatch.setattr(
        "unblock.auth.jwks",
        lambda url: SimpleNamespace(
            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key())
        ),
    )
    claims = {
        "sub": "user-123",
        "custom:tenant": "org-one",
        "aud": "client-test",
        "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_test",
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        "token_use": "id",
        "cognito:groups": ["reviewer"],
    }
    return private, claims


def test_valid_identity(signing):
    private, claims = signing
    token = jwt.encode(claims, private, algorithm="RS256")
    response = TestClient(app).get("/api/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    assert response.json() == {
        "tenant": "org-one",
        "actor": "user-123",
        "reviewer": True,
        "demo": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "different-client"},
        {"token_use": "access"},
        {"iss": "https://attacker.invalid"},
        {"exp": 0},
        {"custom:tenant": "../other"},
        {"custom:tenant": ""},
    ],
)
def test_reject_invalid_claims(signing, change):
    private, claims = signing
    token = jwt.encode(claims | change, private, algorithm="RS256")
    assert (
        TestClient(app).get("/api/me", headers={"Authorization": "Bearer " + token}).status_code
        == 401
    )


def test_demo_identity_is_read_only_and_never_a_reviewer():
    import pytest
    from fastapi import HTTPException

    from unblock.auth import Identity, writer

    demo = Identity(tenant="demo", actor="visitor", reviewer=True, demo=True)
    # A demo account may read, but every write path must refuse it.
    with pytest.raises(HTTPException) as raised:
        writer(demo)
    assert raised.value.status_code == 403
    assert writer(Identity(tenant="org-a", actor="member")).tenant == "org-a"


def test_demo_group_membership_strips_reviewer_rights(monkeypatch):
    import jwt

    from unblock.auth import identity
    from unblock.config import Settings

    monkeypatch.setattr(
        "unblock.auth.settings",
        lambda: Settings(app_env="dev", unblock_user_pool_id="pool", unblock_client_id="client"),
    )
    claims = {
        "custom:tenant": "demo",
        "sub": "visitor",
        "token_use": "id",
        "cognito:groups": ["reviewer", "demo"],
    }
    monkeypatch.setattr(jwt, "decode", lambda *a, **k: claims)
    monkeypatch.setattr(
        "unblock.auth.jwks",
        lambda url: type(
            "K", (), {"get_signing_key_from_jwt": lambda self, t: type("S", (), {"key": "k"})()}
        )(),
    )
    result = identity("Bearer token")
    assert result.demo is True and result.reviewer is False
