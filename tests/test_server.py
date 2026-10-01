"""The web server's basics: open locally (no login), security headers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from jotted import config  # noqa: E402

ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/jotted.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return config.load()


def client(cfg):
    from jotted.server import create_app

    return create_app(cfg, background=False).test_client()


def test_open_locally_without_login(cfg):
    c = client(cfg)
    assert c.get("/api/todo").status_code == 200
    assert c.get("/").status_code == 200


def test_security_headers(cfg):
    r = client(cfg).get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"

