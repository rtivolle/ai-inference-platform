"""Administrator login with Argon2 passwords and revocable opaque sessions."""

import asyncio
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from typing import Annotated
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

COOKIE_NAME = "inference_session"
PASSWORD_HASHER = PasswordHasher()
security_bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
router = APIRouter(prefix="/auth", tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024, json_schema_extra={"writeOnly": True})


class SessionStore:
    def __init__(self, path: str = ":memory:"):
        self.lock = threading.RLock()
        if path != ":memory:":
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL)")
            if "credential_version" not in {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}:
                self.db.execute("ALTER TABLE sessions ADD COLUMN credential_version TEXT NOT NULL DEFAULT ''")
            self.db.execute("CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, failures INTEGER, started REAL)")
            self.db.commit()

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, username: str, lifetime: int, credential_version: str) -> tuple[str, dict]:
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE expires <= ?", (time.time(),))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            expires = time.time() + lifetime
            self.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (self.key(token), username, csrf, expires, credential_version))
            self.db.commit()
            return token, {"username": username, "role": "admin", "csrf_token": csrf, "expires_at": expires}

    def get(self, token: str) -> dict | None:
        if len(token) > 256:
            return None
        with self.lock:
            row = self.db.execute("SELECT username, csrf, expires, credential_version FROM sessions WHERE id = ?", (self.key(token),)).fetchone()
            if not row or row[2] <= time.time():
                return None
            return {"username": row[0], "role": "admin", "csrf_token": row[1], "expires_at": row[2], "credential_version": row[3]}

    def revoke(self, token: str):
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE id = ?", (self.key(token),))
            self.db.commit()

    def check_rate_limit(self, key: str):
        with self.lock:
            self.db.execute("DELETE FROM attempts WHERE started <= ?", (time.time() - 300,))
            row = self.db.execute("SELECT failures FROM attempts WHERE key = ?", (key,)).fetchone()
            if row and row[0] >= 5:
                raise HTTPException(429, "Too many login attempts. Try again in five minutes.", headers={"Retry-After": "300"})

    def failed_login(self, key: str):
        with self.lock:
            self.db.execute(
                "INSERT INTO attempts VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET failures = failures + 1",
                (key, time.time()),
            )
            self.db.commit()

    def reset_attempts(self, key: str):
        with self.lock:
            self.db.execute("DELETE FROM attempts WHERE key = ?", (key,))
            self.db.commit()


class Authentication:
    def __init__(self):
        self.store = SessionStore(os.getenv("CONTROL_PLANE_SESSION_DB", ":memory:"))
        self._verification_slots = asyncio.Semaphore(2)
        self._dummy_hash = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))

    @property
    def mock_mode(self) -> bool:
        return os.getenv("CONTROL_PLANE_MOCK", "0").lower() in ("1", "true")

    @property
    def configured(self) -> bool:
        return bool(os.getenv("CONTROL_PLANE_ADMIN_USERNAME") and os.getenv("CONTROL_PLANE_ADMIN_PASSWORD_HASH"))

    @property
    def secure_cookie(self) -> bool:
        explicit = os.getenv("CONTROL_PLANE_COOKIE_SECURE")
        return explicit.lower() not in ("0", "false") if explicit is not None else not self.mock_mode

    def api_token(self) -> str:
        return os.getenv("CONTROL_PLANE_API_TOKEN", "test-token" if self.mock_mode else "")

    @property
    def credential_version(self) -> str:
        return hashlib.sha256((os.getenv("CONTROL_PLANE_ADMIN_USERNAME", "") + "\0" + os.getenv("CONTROL_PLANE_ADMIN_PASSWORD_HASH", "")).encode()).hexdigest()

    def check_origin(self, request: Request):
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            public_url = os.getenv("CONTROL_PLANE_PUBLIC_URL") or str(request.base_url).rstrip("/")
            parsed = urlsplit(origin)
            origin_base = f"{parsed.scheme}://{parsed.netloc}"
            if parsed.scheme not in ("http", "https") or origin_base.rstrip("/") != public_url.rstrip("/"):
                raise HTTPException(403, "Request origin was rejected.")

    def set_session(self, response: Response, username: str, previous: str | None = None) -> dict:
        if previous:
            self.store.revoke(previous)
        lifetime = min(86400, max(300, int(os.getenv("CONTROL_PLANE_SESSION_SECONDS", "3600"))))
        token, identity = self.store.create(username, lifetime, self.credential_version)
        response.set_cookie(COOKIE_NAME, token, max_age=lifetime, httponly=True, secure=self.secure_cookie, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return identity

    async def verify(self, request: Request, credentials: HTTPAuthorizationCredentials | None = None) -> dict:
        # API bearer credentials are independent of browser cookie sessions. Cloudflare
        # Access may provide an outer gate, but arbitrary headers never authenticate here.
        if credentials:
            configured = self.api_token()
            if configured and hmac.compare_digest(credentials.credentials.encode(), configured.encode()):
                return {"username": "api-client", "role": "admin", "auth": "bearer"}
            raise HTTPException(401, "Unauthorized: valid Bearer token or administrator session required.", headers={"WWW-Authenticate": "Bearer"})
        token = request.cookies.get(COOKIE_NAME, "")
        identity = self.store.get(token) if token else None
        if identity and identity.pop("credential_version") != self.credential_version:
            self.store.revoke(token)
            identity = None
        if not identity:
            raise HTTPException(401, "Unauthorized: sign in to continue.", headers={"WWW-Authenticate": "Bearer"})
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            self.check_origin(request)
            csrf = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf.encode(), identity["csrf_token"].encode()):
                raise HTTPException(403, "Session CSRF verification failed.")
        return identity


auth = Authentication()


async def verify_auth(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security_bearer)] = None):
    return await auth.verify(request, credentials)


@router.get("/config")
async def authentication_config():
    return {"login_enabled": auth.configured, "mock_token_enabled": auth.mock_mode, "session_seconds": int(os.getenv("CONTROL_PLANE_SESSION_SECONDS", "3600"))}


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    auth.check_origin(request)
    if not auth.configured:
        raise HTTPException(503, "Administrator login is not configured.")
    client_ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    key = hashlib.sha256(client_ip.encode()).hexdigest()
    async with auth._verification_slots:
        auth.store.check_rate_limit(key)
        expected_username = os.environ["CONTROL_PLANE_ADMIN_USERNAME"]
        username_valid = hmac.compare_digest(payload.username.encode(), expected_username.encode())
        password_hash = os.environ["CONTROL_PLANE_ADMIN_PASSWORD_HASH"] if username_valid else auth._dummy_hash
        try:
            password_valid: bool = await run_in_threadpool(PASSWORD_HASHER.verify, password_hash, payload.password)
        except (VerificationError, InvalidHashError):
            password_valid = False
        if not username_valid or not password_valid:
            auth.store.failed_login(key)
            raise HTTPException(401, "Invalid username or password.")
        auth.store.reset_attempts(key)
    return auth.set_session(response, expected_username, request.cookies.get(COOKIE_NAME))


@router.get("/session")
async def current_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await auth.verify(request)


@router.post("/logout")
async def logout(request: Request, response: Response, identity: Annotated[dict, Depends(verify_auth)]):
    auth.store.revoke(request.cookies.get(COOKIE_NAME, ""))
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=auth.secure_cookie, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"status": "signed_out"}


if __name__ == "__main__":
    import getpass
    import sys

    if sys.argv[1:] != ["hash-password"]:
        raise SystemExit("Usage: python -m api.auth hash-password")
    password = getpass.getpass("Administrator password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters.")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    print(PASSWORD_HASHER.hash(password))
