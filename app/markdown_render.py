from __future__ import annotations

import re

import markdown


_MD = markdown.Markdown(extensions=["extra", "tables", "fenced_code"])


def render_markdown(md_text: str, org: str, name: str) -> str:
    """Render markdown and rewrite relative _assets links to marketplace asset URLs."""
    # Rewrite common relative asset paths used in plugin READMEs.
    base = f"/api/v1/plugins/{org}/{name}/_assets/"

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        url = url.lstrip("./")
        if url.startswith("_assets/"):
            url = url[len("_assets/") :]
        return f"({base}{url})"

    md_text = re.sub(r"\((?:\./)?(_assets/[^)]+)\)", repl, md_text)
    md_text = re.sub(r"\((?:\./)?(assets/[^)]+)\)", repl, md_text)

    # Reset parser state per render.
    _MD.reset()
    return _MD.convert(md_text)
