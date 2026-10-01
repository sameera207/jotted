"""The web server's basics: open locally (no login), security headers, run pruning."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from rmtasks import config  # noqa: E402
from rmtasks.analysis import prune_runs  # noqa: E402

ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    text = config.EXAMPLE.read_text().replace('db   = "./data/rmtasks.db"', f'db = "{tmp_path}/db.sqlite"')
    path = tmp_path / "config.toml"
    path.write_text(text)
    monkeypatch.setenv(config.ENV_VAR, str(path))
    return config.load()


def client(cfg):
    from rmtasks.server import create_app

    return create_app(cfg, background=False).test_client()


def test_open_locally_without_login(cfg):
    c = client(cfg)
    assert c.get("/api/todo").status_code == 200
    assert c.get("/").status_code == 200


def test_security_headers(cfg):
    r = client(cfg).get("/")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_prune_runs_keeps_the_newest(tmp_path):
    for i in range(6):
        (tmp_path / f"20261001-12000{i}").mkdir()
    (tmp_path / "template").mkdir()  # not a run: left alone
    prune_runs(tmp_path, keep=3)
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["20261001-120004", "20261001-120005", "template"]  # room for the run about to be written
