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
