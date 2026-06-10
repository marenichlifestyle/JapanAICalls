from __future__ import annotations

from bs4 import BeautifulSoup


def clean_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def compact_html_fragments(html: str, limit: int = 12000) -> str:
    soup = BeautifulSoup(html, "html.parser")
    fragments = []
    for selector in ["script[type='application/ld+json']", "meta", "table", "section", "div"]:
        for node in soup.select(selector)[:25]:
            fragments.append(str(node)[:500])
            if sum(len(x) for x in fragments) > limit:
                break
        if sum(len(x) for x in fragments) > limit:
            break
    result = "\n".join(fragments)
    return result[:limit]
