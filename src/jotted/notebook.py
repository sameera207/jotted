"""Reading a downloaded reMarkable document's page list."""

from __future__ import annotations


def page_order(content: dict) -> list[str]:
    """Page ids in reading order, from a .content file."""
    cpages = (content.get("cPages") or {}).get("pages")
    if cpages:
        live = [p for p in cpages if "deleted" not in p]
        live.sort(key=lambda p: (p.get("idx") or {}).get("value", ""))
        return [p["id"] for p in live]
    return list(content.get("pages") or [])
