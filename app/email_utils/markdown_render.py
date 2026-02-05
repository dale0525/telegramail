from markdown_it import MarkdownIt


_md = MarkdownIt("commonmark", {"breaks": True, "html": False})


def render_markdown_to_html(markdown_text: str) -> str:
    rendered = _md.render(markdown_text or "")
    return f"<html><body>{rendered}</body></html>"

