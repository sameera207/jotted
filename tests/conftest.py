"""For every test: nothing outside the test's own folder is changed."""

import pytest

from jotted.integrations import claude_desktop


@pytest.fixture(autouse=True)
def _claude_desktop_config(tmp_path_factory, monkeypatch):
    """`jotted claude connect` edits a scratch file, never the real Claude Desktop's (subprocesses
    inherit the variable)."""
    folder = tmp_path_factory.mktemp("claude")
    monkeypatch.setenv(claude_desktop.PATH_VAR, str(folder / "claude_desktop_config.json"))
