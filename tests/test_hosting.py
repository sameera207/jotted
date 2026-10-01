"""Hosting: password login, the public-address guard, the health check, run pruning."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from rmtasks import cli, config  # noqa: E402
from rmtasks.analysis import prune_runs  # noqa: E402

ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    text = (ROOT / "config.example.toml").read_text().replace('db   = "./data/rmtasks.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return config.load()


def client(cfg):
    from rmtasks.server import create_app

    return create_app(cfg, background=False).test_client()


def test_no_password_means_open_locally(cfg, monkeypatch):
    monkeypatch.delenv("RMTASKS_PASSWORD", raising=False)
    c = client(cfg)
    assert c.get("/api/todo").status_code == 200
    assert c.get("/healthz").get_json() == {"ok": True}


def test_password_protects_pages_and_api(cfg, monkeypatch):
    monkeypatch.setenv("RMTASKS_PASSWORD", "correct horse")
    c = client(cfg)
    assert c.get("/healthz").status_code == 200  # the platform's health check needs no login
    assert c.get("/api/todo").status_code == 401
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert b"Password" in c.get("/login").data

    monkeypatch.setattr("time.sleep", lambda s: None)
    assert c.post("/login", data={"password": "wrong"}).status_code == 401
    assert c.get("/api/todo").status_code == 401

    r = c.post("/login", data={"password": "correct horse"})
    assert r.status_code == 302
    assert c.get("/api/todo").get_json()["auth"] is True

    c.post("/logout")
    assert c.get("/api/todo").status_code == 401


def test_session_cookie_is_hardened(cfg, monkeypatch):
    monkeypatch.setenv("RMTASKS_PASSWORD", "pw")
    monkeypatch.setenv("RMTASKS_SECURE_COOKIES", "1")
    c = client(cfg)
    r = c.post("/login", data={"password": "pw"})
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie


def test_serve_refuses_public_address_without_password(cfg, monkeypatch):
    monkeypatch.delenv("RMTASKS_PASSWORD", raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "0"]) == 2


def test_prune_runs_keeps_the_newest(tmp_path):
    for i in range(6):
        (tmp_path / f"20261001-12000{i}").mkdir()
    (tmp_path / "template").mkdir()  # not a run: left alone
    prune_runs(tmp_path, keep=3)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["20261001-120004", "20261001-120005", "template"]  # room for the run about to be written


def test_railway_config_is_valid_and_on_the_volume(monkeypatch):
    cfg = config.load(ROOT / "config.railway.toml")
    for p in (cfg.paths.cache_dir, cfg.paths.output_dir, cfg.rmapi.token_file, cfg.server.db):
        assert str(p).startswith("/data/")
    assert cfg.server.host == "0.0.0.0"


# ---------------------------------------------------------------- Sign in with Google

from authlib.integrations.flask_client.apps import FlaskOAuth2App  # noqa: E402
from flask import redirect as flask_redirect  # noqa: E402

GOOGLE_ENV = {"GOOGLE_CLIENT_ID": "id.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "secret",
              "RMTASKS_ALLOWED_EMAILS": "Sam@Example.com, other@example.com"}


@pytest.fixture
def google(cfg, monkeypatch):
    for k, v in GOOGLE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RMTASKS_PASSWORD", raising=False)
    seen = {}

    def fake_redirect(self, redirect_uri=None, **kw):  # no network: Google's metadata isn't fetched
        seen["redirect_uri"], seen["kw"] = redirect_uri, kw
        return flask_redirect("https://accounts.google.com/o/oauth2/v2/auth?fake=1")

    monkeypatch.setattr(FlaskOAuth2App, "authorize_redirect", fake_redirect)
    return seen


def userinfo(monkeypatch, info=None, error=None):
    def fake_token(self, **kw):
        if error:
            raise error
        return {"access_token": "x", "userinfo": info}

    monkeypatch.setattr(FlaskOAuth2App, "authorize_access_token", fake_token)


def test_google_sign_in_for_an_allowed_email(cfg, google, monkeypatch):
    c = client(cfg)
    page = c.get("/login").data
    assert b"Sign in with Google" in page and b"Password" not in page  # Google only: no password form
    r = c.get("/auth/google")
    assert r.status_code == 302 and google["kw"] == {"prompt": "select_account"}
    assert google["redirect_uri"].endswith("/auth/google/callback")

    userinfo(monkeypatch, {"email": "sam@example.com", "email_verified": True})
    r = c.get("/auth/google/callback?code=c&state=s")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert c.get("/api/me").get_json() == {"email": "sam@example.com", "method": "google"}
    assert c.get("/api/todo").get_json()["user"] == "sam@example.com"


@pytest.mark.parametrize("info, error_code", [
    ({"email": "stranger@example.com", "email_verified": True}, "not_allowed"),
    ({"email": "sam@example.com", "email_verified": False}, "unverified"),
    ({}, "unverified"),
])
def test_google_sign_in_refused(cfg, google, monkeypatch, info, error_code):
    c = client(cfg)
    userinfo(monkeypatch, info)
    r = c.get("/auth/google/callback?code=c&state=s")
    assert r.headers["Location"] == f"/login?error={error_code}"
    assert c.get("/api/todo").status_code == 401
    assert b"allowed to use this app" in c.get("/login?error=not_allowed").data


def test_google_errors_and_forged_callbacks_do_not_sign_in(cfg, google, monkeypatch):
    from authlib.integrations.base_client import OAuthError

    c = client(cfg)
    userinfo(monkeypatch, error=OAuthError(error="access_denied"))
    assert c.get("/auth/google/callback?error=access_denied").headers["Location"] == "/login?error=google"
    assert c.get("/api/todo").status_code == 401
    # Unknown error codes are never echoed into the page.
    assert b"<script>" not in c.get("/login?error=<script>alert(1)</script>").data


def test_google_without_allowed_emails_refuses_to_start(cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.delenv("RMTASKS_ALLOWED_EMAILS", raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "0"]) == 2
    with pytest.raises(RuntimeError, match="ALLOWED_EMAILS"):
        client(cfg)


def test_half_configured_google_refuses_to_start(cfg, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "id")
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert cli.main(["serve", "--port", "0"]) == 2


def test_redirect_uri_behind_railways_proxy(cfg, google, monkeypatch):
    monkeypatch.setenv("RMTASKS_BEHIND_PROXY", "1")
    c = client(cfg)
    c.get("/auth/google", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "rmtasks.up.railway.app"})
    assert google["redirect_uri"] == "https://rmtasks.up.railway.app/auth/google/callback"
    monkeypatch.setenv("RMTASKS_PUBLIC_URL", "https://tasks.example.com/")
    client(cfg).get("/auth/google")
    assert google["redirect_uri"] == "https://tasks.example.com/auth/google/callback"


def test_password_and_google_together(cfg, google, monkeypatch):
    monkeypatch.setenv("RMTASKS_PASSWORD", "pw")
    page = client(cfg).get("/login").data
    assert b"Sign in with Google" in page and b"Password" in page and b">or<" in page


def test_security_headers(cfg, monkeypatch):
    monkeypatch.setenv("RMTASKS_SECURE_COOKIES", "1")
    r = client(cfg).get("/healthz")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "max-age" in r.headers["Strict-Transport-Security"]
