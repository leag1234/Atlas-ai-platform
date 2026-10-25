"""Render targeted shopping retrieval without depending on model URL copying."""

import re
from urllib.parse import urlsplit

from services.orchestrator.link_requests import unavailable
from services.orchestrator.loop import Message
from services.orchestrator.shopping_prices import MONEY


def render_shopping(searches: dict[str, list[Message]], language: str) -> str | None:
    sections: list[str] = []
    found_shop = False
    for query, rows in searches.items():
        product, separator, _ = query.partition(" (site:")
        if not separator:
            continue
        identity = re.sub(r"[^a-z0-9]", "", product.casefold())
        shops: dict[str, Message] = {}
        for row in rows:
            url = str(row.get("link", ""))
            host = urlsplit(url).hostname or ""
            # These are shop domains, not permission to invent their product URLs.
            shop = next(
                (
                    name
                    for name in ("thomann", "woodbrass", "boulanger", "fnac")
                    if re.fullmatch(r"(?:www\.)?" + name + r"\.[a-z.]+", host)
                ),
                None,
            )
            subject = str(row.get("title", "")) + " " + url
            if shop and identity in re.sub(r"[^a-z0-9]", "", subject.casefold()):
                shops.setdefault(shop, row)
        found_shop |= bool(shops)
        lines = ["### " + product]
        for shop, row in list(shops.items())[:3]:
            line = "- [" + shop.capitalize() + "](" + str(row["link"]) + ")"
            # Multiple amounts could be an accessory or instalment offer. Keep
            # those ambiguous prices out; the link is useful on its own.
            prices = list(MONEY.finditer(str(row.get("snippet", ""))))
            if len(prices) == 1:
                line += " — " + prices[0][0]
            lines.append(line)
        if not shops:
            lines.append(unavailable(product, language))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) if found_shop else None
