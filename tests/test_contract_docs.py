"""The documents downstream agents and apps read (docs/cli-contract.md, docs/schema.json)
match the CLI. If this fails, run: uv run python scripts/contract_docs.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import contract_docs  # noqa: E402


def test_the_contract_documents_are_up_to_date():
    stale = [str(p.relative_to(ROOT)) for p in contract_docs.stale()]
    assert not stale, f"out of date: {stale}. Run: uv run python scripts/contract_docs.py"


def test_the_guide_covers_every_command():
    guide = contract_docs.GUIDE.read_text()
    for c in contract_docs.schema_data()["commands"]:
        assert f"### `jotted {c['command']}`" in guide, c["command"]
