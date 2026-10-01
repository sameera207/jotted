"""The web server's basics: open locally (no login), security headers, and the AI settings."""

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
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
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



def test_jev_is_a_plugin_turned_on_and_off_by_its_key(cfg, monkeypatch):
    import os

    from jotted import keys
    from jotted.adapters.typesafe_judge import TypeSafeJudge
    from jotted.llm import ModelError

    def verify(self):
        if not os.environ[self.cfg.api_key_env].startswith("good"):
            raise ModelError("TypeSafe rejected this key")

    monkeypatch.setattr(TypeSafeJudge, "verify", verify)
    monkeypatch.setenv(cfg.llm.api_key_env, "sk-ant-from-the-shell-1234")
    c = client(cfg)

    ai = c.get("/api/ai").get_json()
    assert ai["llm"]["label"] == "Anthropic" and ai["llm"]["model"] == cfg.llm.model
    assert ai["llm"]["key"] == {"set": True, "source": "environment", "hint": "…1234"}
    assert [p["id"] for p in ai["llm"]["providers"]] == ["anthropic"]
    assert not ai["jev"]["enabled"] and ai["judge"] == "llm"

    r = c.put("/api/ai/jev/key", json={"key": "bad-key"})
    assert r.status_code == 400 and "rejected" in r.get_json()["error"]
    assert "TYPESAFE_API_KEY" not in os.environ and not keys.saved(cfg)

    ai = c.put("/api/ai/jev/key", json={"key": "good-jev-key-9876"}).get_json()
    assert ai["jev"]["enabled"] and ai["judge"] == "jev" and ai["jev"]["key"]["source"] == "saved"
    assert keys.saved(cfg) == {"TYPESAFE_API_KEY": "good-jev-key-9876"}
    assert "good-jev-key-9876" not in c.get("/api/ai").get_data(as_text=True)  # never sent back

    assert c.delete("/api/ai/llm/key").status_code == 400  # nothing works without the LLM
    ai = c.delete("/api/ai/jev/key").get_json()
    assert not ai["jev"]["enabled"] and ai["judge"] == "llm" and not keys.saved(cfg)

    monkeypatch.setenv("TYPESAFE_API_KEY", "exported-in-the-shell")
    assert c.delete("/api/ai/jev/key").status_code == 409  # outside Jotted's reach
    assert c.put("/api/ai/nope/key", json={"key": "x"}).status_code == 404
    assert c.put("/api/ai/jev/key", json={}).status_code == 400
