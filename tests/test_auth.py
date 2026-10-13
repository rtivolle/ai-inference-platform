import time

import pytest
from api import auth as authentication

PASSWORD = "test-only-administrator-password"


@pytest.fixture
def auth_client(api_client, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("CONTROL_PLANE_ADMIN_PASSWORD_HASH", authentication.PASSWORD_HASHER.hash(PASSWORD))
    monkeypatch.setattr(authentication, "auth", authentication.Authentication())
    return api_client


def sign_in(client):
    response = client.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    return response


def test_login_session_csrf_logout_and_stolen_cookie_revocation(auth_client):
    response = sign_in(auth_client)
    cookie = auth_client.cookies.get(authentication.COOKIE_NAME)
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert PASSWORD not in response.text
    assert auth_client.get("/api/v1/cluster/gpus").status_code == 200
    assert auth_client.get("/auth/session").json()["username"] == "admin"
    assert auth_client.post("/auth/logout").status_code == 403
    csrf = response.json()["csrf_token"]
    assert auth_client.post("/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://attacker.invalid"}).status_code == 403
    assert auth_client.post("/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert auth_client.get("/api/v1/cluster/gpus", headers={"Cookie": f"{authentication.COOKIE_NAME}={cookie}"}).status_code == 401


def test_expired_session_is_rejected(auth_client):
    sign_in(auth_client)
    authentication.auth.store.db.execute("UPDATE sessions SET expires = ?", (time.time() - 1,))
    authentication.auth.store.db.commit()
    assert auth_client.get("/auth/session").status_code == 401
    assert auth_client.get("/api/v1/models").status_code == 401


def test_login_rotates_previous_session(auth_client):
    sign_in(auth_client)
    first_cookie = auth_client.cookies.get(authentication.COOKIE_NAME)
    sign_in(auth_client)
    assert auth_client.cookies.get(authentication.COOKIE_NAME) != first_cookie
    assert auth_client.get("/auth/session", headers={"Cookie": f"{authentication.COOKIE_NAME}={first_cookie}"}).status_code == 401


def test_login_rate_limit_and_generic_invalid_credentials(auth_client):
    for username in ["unknown", "admin", "unknown", "admin", "admin"]:
        response = auth_client.post("/auth/login", json={"username": username, "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid username or password."
    response = auth_client.post("/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "300"


@pytest.mark.parametrize("headers", [
    {"Cf-Access-Jwt-Assertion": "unverified"},
    {"CF-Access-Client-Id": "anything", "CF-Access-Client-Secret": "anything"},
    {"Authorization": "Bearer invalid"},
])
def test_arbitrary_credentials_never_authenticate(api_client, headers):
    assert api_client.get("/api/v1/cluster/gpus", headers=headers).status_code == 401


def test_secure_cookie_when_running_live(auth_client, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_MOCK", "0")
    response = sign_in(auth_client)
    assert "secure" in response.headers["set-cookie"].lower()


def test_csrf_required_for_all_session_mutations(auth_client):
    response = sign_in(auth_client)
    payload = {"model_id": "meta-llama/Meta-Llama-3-8B-Instruct"}
    assert auth_client.post("/api/v1/models/deploy", json=payload).status_code == 403
    assert auth_client.post("/api/v1/models/deploy", json=payload, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert auth_client.post("/api/v1/models/deploy", json=payload, headers={"X-CSRF-Token": response.json()["csrf_token"]}).status_code == 200
