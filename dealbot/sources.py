"""Deal sources. Each source returns raw Offer/Post lists; filtering happens in the runner.

Retail sites change their markup and block bots without notice, so parsers are
deliberately generic (embedded JSON / JSON-LD walking) and every source fails
independently.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .fetch import fetch
from .matching import Offer

# ---------------------------------------------------------------- price parsing

_PRICE_KEY_ORDER = (
    "raw_amount", "amount", "value", "price", "currentPrice", "salePrice",
    "lowPrice", "current", "priceInfo", "offers", "minPrice",
)
_TITLE_KEYS = ("title", "name", "productName", "displayName")
_URL_KEYS = ("productDetailsUrl", "productUrl", "canonicalUrl", "url", "seoUrl", "link", "href")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price(value, depth: int = 0) -> float | None:
    if depth > 4 or value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if isinstance(value, str):
        m = _NUM_RE.search(value.replace("\xa0", " "))
        if not m:
            return None
        num = float(m.group().replace(",", ""))
        return num if num > 0 else None
    if isinstance(value, dict):
        for k in _PRICE_KEY_ORDER:
            if k in value:
                p = parse_price(value[k], depth + 1)
                if p:
                    return p
        return None
    if isinstance(value, list):
        prices = [p for p in (parse_price(v, depth + 1) for v in value) if p]
        return min(prices) if prices else None
    return None


def _first_str(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _dict_price(d: dict) -> float | None:
    for k in _PRICE_KEY_ORDER:
        if k in d and k not in ("value",):  # bare "value" at item level is too ambiguous
            p = parse_price(d[k])
            if p:
                return p
    return None


# ------------------------------------------------------------- embedded JSON

_SCRIPT_RE = re.compile(r"<script([^>]*)>(.*?)</script>", re.S | re.I)
_ASSIGN_RE = re.compile(r"window\.[A-Z0-9_]*BOOTSTRAP[A-Z0-9_]*\s*=\s*", re.I)


def extract_json_blobs(text: str) -> list:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return [json.loads(stripped)]
        except ValueError:
            pass
    blobs: list = []
    for attrs, body in _SCRIPT_RE.findall(text):
        a = attrs.lower()
        if "application/ld+json" in a or "application/json" in a or "__next_data__" in a:
            try:
                blobs.append(json.loads(body.strip()))
            except ValueError:
                continue
    decoder = json.JSONDecoder()
    for m in _ASSIGN_RE.finditer(text):
        try:
            obj, _ = decoder.raw_decode(text, m.end())
            blobs.append(obj)
        except ValueError:
            continue
    return blobs


_OUT_WORDS = ("outofstock", "soldout", "out_of_stock", "sold_out", "discontinued")
_IN_WORDS = ("instock", "in_stock", "limitedavailability", "preorder", "backorder", "onlineonly", "instoreonly")


def stock_status(d: dict) -> bool | None:
    """True/False when a product/offer dict states its availability, else None."""
    for k in ("isInStock", "inStock", "available", "isAvailable"):
        if isinstance(d.get(k), bool):
            return d[k]
    for k in ("soldOut", "isSoldOut", "outOfStock", "isOutOfStock"):
        if isinstance(d.get(k), bool):
            return not d[k]
    for k in ("availability", "availabilityStatus", "stockStatus"):
        v = d.get(k)
        if isinstance(v, str):
            low = v.lower().replace(" ", "").replace("-", "")
            if any(w in low for w in _OUT_WORDS):
                return False
            if any(w in low for w in _IN_WORDS):
                return True
    return None


def _offers_stock(offers) -> bool | None:
    items = offers if isinstance(offers, list) else [offers]
    states = [stock_status(o) for o in items if isinstance(o, dict)]
    if any(s is True for s in states):
        return True
    if states and all(s is False for s in states):
        return False
    return None


def _ld_price(offers) -> float | None:
    """Lowest price among in-stock offers, falling back to any offer."""
    items = offers if isinstance(offers, list) else [offers]
    in_stock = [o for o in items if isinstance(o, dict) and stock_status(o) is not False]
    return parse_price(in_stock or items)


def walk_products(obj, base_url: str, out: list | None = None, depth: int = 0):
    """Find (title, price, url, in_stock) tuples anywhere inside a JSON structure."""
    if out is None:
        out = []
    if depth > 40:
        return out
    if isinstance(obj, dict):
        types = obj.get("@type")
        is_ld_product = types == "Product" or (isinstance(types, list) and "Product" in types)
        title = _first_str(obj, _TITLE_KEYS)
        if title:
            offers = obj.get("offers")
            price = _ld_price(offers) if is_ld_product else _dict_price(obj)
            url = _first_str(obj, _URL_KEYS)
            if not url and is_ld_product and isinstance(offers, dict):
                url = offers.get("url")
            if price and (url or is_ld_product):
                stock = _offers_stock(offers) if is_ld_product and offers else stock_status(obj)
                out.append((htmlmod.unescape(title), price, urllib.parse.urljoin(base_url, url or ""), stock))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_products(v, base_url, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_products(v, base_url, out, depth + 1)
    return out


# --------------------------------------------------------------------- sources

def source_page(cfg: dict) -> list[Offer]:
    """Generic page/JSON endpoint: walks embedded JSON and JSON-LD for products."""
    url = cfg["url"]
    text = fetch(url)
    offers: dict[str, Offer] = {}
    cents = cfg.get("cents")  # e.g. Kijiji reports every price in cents
    for blob in extract_json_blobs(text):
        for title, price, link, stock in walk_products(blob, url):
            if cents:
                price = price / 100.0
            key = link or title
            if key not in offers or price < offers[key].price:
                offers[key] = Offer(cfg["name"], title, round(price, 2), link or url, in_stock=stock)
    return list(offers.values())


def source_bestbuy(cfg: dict) -> list[Offer]:
    q = urllib.parse.quote(cfg["query"])
    url = (
        "https://www.bestbuy.ca/api/v2/json/search?lang=en-CA"
        f"&query={q}&page=1&pageSize=48&sortBy=relevance&sortDir=desc"
    )
    data = json.loads(fetch(url, accept="application/json"))
    offers = []
    for p in data.get("products", []):
        sale = parse_price(p.get("salePrice"))
        regular = parse_price(p.get("regularPrice"))
        price = sale or regular
        if not price or not p.get("name"):
            continue
        link = urllib.parse.urljoin("https://www.bestbuy.ca", p.get("productUrl") or "")
        seller = "第三方卖家" if p.get("isMarketplace") else ""
        offers.append(Offer(cfg["name"], p["name"], price, link,
                            regular_price=regular if regular and regular > price else None,
                            in_stock=stock_status(p),
                            extra={"seller": seller} if seller else {}))
    return offers


_AMZ_SPLIT = re.compile(r'data-component-type="s-search-result"')
_AMZ_ASIN = re.compile(r'data-asin="([A-Z0-9]{10})"')
_AMZ_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
_AMZ_PRICE = re.compile(r'<span class="a-offscreen">([^<]+)</span>')
_TAG_RE = re.compile(r"<[^>]+>")


def parse_amazon(text: str, source_name: str) -> list[Offer]:
    offers = []
    chunks = _AMZ_SPLIT.split(text)
    for prev, chunk in zip(chunks, chunks[1:]):
        asin_m = _AMZ_ASIN.findall(prev[-600:])
        title_m = _AMZ_TITLE.search(chunk)
        price_m = _AMZ_PRICE.search(chunk)
        if not (asin_m and title_m and price_m):
            continue
        title = htmlmod.unescape(_TAG_RE.sub(" ", title_m.group(1)))
        title = " ".join(title.split())
        price = parse_price(price_m.group(1))
        if price:
            offers.append(Offer(source_name, title, price, f"https://www.amazon.ca/dp/{asin_m[-1]}"))
    return offers


def source_amazon(cfg: dict) -> list[Offer]:
    text = fetch(cfg["url"])
    if "captcha" in text.lower() and "s-search-result" not in text:
        raise RuntimeError("Amazon 返回验证码页面（反爬）")
    return parse_amazon(text, cfg["name"])


@dataclass
class Post:
    source: str
    id: str
    title: str
    link: str
    product: str = ""


def parse_feed(text: str, source_name: str) -> list[Post]:
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    posts = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {}
        for child in el:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "link":
                fields.setdefault("link", child.get("href") or (child.text or "").strip())
            else:
                fields.setdefault(ctag, (child.text or "").strip())
        title = htmlmod.unescape(fields.get("title", ""))
        link = fields.get("link", "")
        pid = fields.get("guid") or fields.get("id") or link
        if title and pid:
            posts.append(Post(source_name, pid, title, link))
    return posts


def source_rfd(cfg: dict) -> list[Post]:
    return parse_feed(fetch(cfg["url"], accept="application/rss+xml,application/atom+xml,text/xml"), cfg["name"])


OFFER_SOURCES = {"page": source_page, "bestbuy": source_bestbuy, "amazon": source_amazon}
POST_SOURCES = {"rfd": source_rfd}
