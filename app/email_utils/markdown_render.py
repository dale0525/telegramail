from markdown_it import MarkdownIt


# Match the Mini App's markdown-it preset so preview and the delivered HTML
# support the same headings, tables, lists, links, and strikethrough syntax.
# Raw HTML stays disabled: user-authored tags must be escaped in both views.
_md = MarkdownIt("default", {"breaks": True, "html": False})


def render_markdown_to_html(markdown_text: str) -> str:
    rendered = _md.render(markdown_text or "")
    return f"<html><body>{rendered}</body></html>"
