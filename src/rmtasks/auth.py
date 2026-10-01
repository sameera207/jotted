"""Sign-in for the web app: Google (limited to listed emails) and/or a password.

Configured by environment variables, never by config.toml:

| Variable | Meaning |
| --- | --- |
| GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET | Turn on "Sign in with Google" |
| RMTASKS_ALLOWED_EMAILS | Comma-separated emails allowed in. Required with Google, or any Google account could sign in |
| RMTASKS_PUBLIC_URL | The app's public URL (e.g. https://rmtasks.up.railway.app); used for Google's redirect |
| RMTASKS_PASSWORD | Optional password sign-in (a fallback, or the only method) |
| RMTASKS_SECRET_KEY | Signs the session cookie |
| RMTASKS_SECURE_COOKIES=1 | HTTPS-only cookies and HSTS (set in the Docker image) |
| RMTASKS_BEHIND_PROXY=1 | Trust the proxy's X-Forwarded-* headers (set in the Docker image) |

With no method configured, the app is open: fine on 127.0.0.1, refused by
`rmtasks serve` on any public address.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from html import escape

from flask import Flask, Response, jsonify, redirect, request, session

log = logging.getLogger("rmtasks.auth")

GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
OPEN_PATHS = ("/login", "/healthz", "/auth/google", "/auth/google/callback")

ERRORS = {  # login page messages, by code; never echo request text into the page
    "password": "Wrong password.",
    "not_allowed": "That Google account isn't allowed to use this app.",
    "unverified": "Google hasn't verified that account's email address.",
    "google": "Google sign-in failed. Try again.",
}


@dataclass(frozen=True)
class AuthConfig:
    password: str
    google_id: str
    google_secret: str
    allowed: frozenset[str]
    public_url: str

    @classmethod
    def from_env(cls) -> "AuthConfig":
        allowed = {e.strip().lower() for e in os.environ.get("RMTASKS_ALLOWED_EMAILS", "").split(",") if e.strip()}
        return cls(
            password=os.environ.get("RMTASKS_PASSWORD", ""),
            google_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            google_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            allowed=frozenset(allowed),
            public_url=os.environ.get("RMTASKS_PUBLIC_URL", "").rstrip("/"),
        )

    @property
    def google(self) -> bool:
        return bool(self.google_id and self.google_secret)

    @property
    def enabled(self) -> bool:
        return bool(self.password) or self.google

    def problems(self) -> list[str]:
        """Configuration mistakes that must stop the server from starting."""
        out = []
        if bool(self.google_id) != bool(self.google_secret):
            out.append("set both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or neither")
        if self.google and not self.allowed:
            out.append("Google sign-in needs RMTASKS_ALLOWED_EMAILS; without it any Google account could sign in")
        return out


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>rmtasks · sign in</title>
<style>
:root { --bg:#f6f5f1; --panel:#fff; --ink:#1d1d1b; --muted:#75736c; --line:#e4e2da; --accent:#2f6f4f; --danger:#b3261e; }
@media (prefers-color-scheme: dark) { :root { --bg:#161614; --panel:#1f1f1c; --ink:#ecebe6; --muted:#9c9a92;
  --line:#33322e; --accent:#6fbf94; --danger:#f08a82; } }
body { margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--ink);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
main { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:24px; width:min(340px, 90vw); }
h1 { font-size:20px; margin:0 0 16px; }
input { width:100%; box-sizing:border-box; font:inherit; padding:9px 12px; border-radius:8px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); }
button, .google { display:block; box-sizing:border-box; margin-top:12px; width:100%; font:inherit; padding:9px;
  border-radius:8px; border:0; background:var(--accent); color:#fff; cursor:pointer; text-align:center; text-decoration:none; }
.google { background:var(--panel); color:var(--ink); border:1px solid var(--line); margin-top:0; }
.google:hover { border-color:var(--muted); }
.or { color:var(--muted); font-size:12px; text-align:center; margin:16px 0 4px; }
.err { color:var(--danger); font-size:14px; margin-bottom:12px; }
</style></head><body><main><h1>rmtasks</h1>{error}{google}{or}{password}</main></body></html>"""

PASSWORD_FORM = """<form method="post" action="/login">
<label for="pw" style="display:block;margin-bottom:6px">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" {autofocus} required>
<button type="submit">Sign in</button></form>"""


def login_page(cfg: AuthConfig, error: str = "", status: int = 200) -> Response:
    html = (LOGIN_PAGE
            .replace("{error}", f'<div class="err">{escape(ERRORS[error])}</div>' if error in ERRORS else "")
            .replace("{google}", '<a class="google" href="/auth/google">Sign in with Google</a>' if cfg.google else "")
            .replace("{or}", '<div class="or">or</div>' if cfg.google and cfg.password else "")
            .replace("{password}", PASSWORD_FORM.replace("{autofocus}", "" if cfg.google else "autofocus")
                     if cfg.password else ""))
    return Response(html, status=status, mimetype="text/html")


def setup(flask: Flask, cfg: AuthConfig | None = None) -> AuthConfig:
    cfg = cfg or AuthConfig.from_env()
    if cfg.problems():
        raise RuntimeError("; ".join(cfg.problems()))
    secure = os.environ.get("RMTASKS_SECURE_COOKIES") == "1"
    seed = f"rmtasks:{cfg.password}:{cfg.google_secret}"
    flask.config.update(
        SECRET_KEY=os.environ.get("RMTASKS_SECRET_KEY") or hashlib.sha256(seed.encode()).hexdigest(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    if os.environ.get("RMTASKS_BEHIND_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix

        flask.wsgi_app = ProxyFix(flask.wsgi_app, x_for=1, x_proto=1, x_host=1)

    @flask.after_request
    def security_headers(resp: Response) -> Response:
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        if secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @flask.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    if not cfg.enabled:
        return cfg

    @flask.before_request
    def require_login():
        if request.path in OPEN_PATHS or session.get("signed_in"):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="Signed out: reload the page to sign in"), 401
        return redirect("/login")

    def sign_in(method: str, email: str | None = None):
        session.clear()  # a fresh session: nothing from before sign-in carries over
        session["signed_in"] = True
        session["method"] = method
        if email:
            session["email"] = email
        session.permanent = True
        return redirect("/")

    @flask.get("/login")
    def login_form():
        return login_page(cfg, request.args.get("error", ""))

    @flask.post("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @flask.get("/api/me")
    def me():
        return jsonify(email=session.get("email"), method=session.get("method"))

    if cfg.password:
        @flask.post("/login")
        def login():
            if hmac.compare_digest(request.form.get("password", "").encode(), cfg.password.encode()):
                return sign_in("password")
            time.sleep(1)  # slow down guessing
            log.warning("failed password sign-in from %s", request.remote_addr)
            return login_page(cfg, "password", status=401)

    if cfg.google:
        from authlib.integrations.base_client import OAuthError
        from authlib.integrations.flask_client import OAuth

        oauth = OAuth(flask)
        google = oauth.register(
            name="google",
            client_id=cfg.google_id,
            client_secret=cfg.google_secret,
            server_metadata_url=GOOGLE_METADATA,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )

        def callback_url() -> str:
            base = cfg.public_url or request.url_root.rstrip("/")
            return f"{base}/auth/google/callback"

        @flask.get("/auth/google")
        def google_start():
            # prompt=select_account: always let you pick, so a wrong default account can't get stuck
            return google.authorize_redirect(callback_url(), prompt="select_account")

        @flask.get("/auth/google/callback")
        def google_callback():
            try:
                token = google.authorize_access_token()
            except (OAuthError, ValueError, KeyError) as e:  # bad state, denied consent, invalid ID token
                log.warning("Google sign-in failed: %s", e)
                return redirect("/login?error=google")
            info = token.get("userinfo") or {}  # from the verified ID token (issuer, audience, expiry, nonce)
            email = (info.get("email") or "").lower()
            if not info.get("email_verified"):
                return redirect("/login?error=unverified")
            if email not in cfg.allowed:
                log.warning("Google sign-in refused for %s", email)
                return redirect("/login?error=not_allowed")
            return sign_in("google", email)

    return cfg
