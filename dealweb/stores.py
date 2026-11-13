"""Storewide sale watch.

A few times a day, read each brand store's public product list (Shopify /products.json) and tell
users when a large share of the store got cheaper since the last look. Only listed prices are
visible: a discount applied at checkout or by a code does not show up here.
"""
from __future__ import annotations

import json
import statistics
import time

from dealbot.notify import Notifier

from . import auth, db

SCAN_HOURS = 6        # how often one store is re-read
MAX_PAGES = 4         # 250 products per page; ponytail: bigger stores are only watched by their first 1,000
PER_CYCLE = 3         # stores read per 30-minute cycle; 3 keeps 29 stores inside SCAN_HOURS
PAUSE = 1.5           # seconds between two pages of the same store
DROP = 0.9            # a product counts as marked down when its price fell by 10% or more
MIN_COUNT, MIN_SHARE = 10, 0.15   # ...and a sale is at least 10 such products and 15% of the store
KEEP_DAYS = 14


def snapshot(domain: str, fetch) -> dict[str, float]:
    """{product id: its cheapest variant's price} for up to MAX_PAGES pages."""
    prices: dict[str, float] = {}
    for page in range(1, MAX_PAGES + 1):
        if page > 1:
            time.sleep(PAUSE)
        products = json.loads(fetch(f"https://{domain}/products.json?limit=250&page={page}",
                                    accept="application/json")).get("products", [])
        for p in products:
            found = [float(v["price"]) for v in p.get("variants", []) if _positive(v.get("price"))]
            if found:
                prices[str(p["id"])] = min(found)
        if len(products) < 250:
            break
    return prices


def _positive(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False


def compare(old: dict, new: dict) -> dict | None:
    """A sale, if enough of the products seen both times got cheaper; otherwise None."""
    both = [k for k in new if k in old]
    ratios = [new[k] / old[k] for k in both if new[k] <= old[k] * DROP]
    if len(ratios) < max(MIN_COUNT, MIN_SHARE * len(both)):
        return None
    return {"dropped": len(ratios), "of": len(both), "ratio": round(statistics.median(ratios), 2)}


def scan_due(con, sources: list[dict], fetch, *, now: float | None = None) -> list[dict]:
    """Re-read the stores that are due (oldest first, PER_CYCLE at most) and return the sales found."""
    now = now or time.time()
    seen = {r["store"]: r for r in con.execute("SELECT store, scanned_at, prices FROM store_prices")}
    stores = [s for s in sources if s.get("reference") == "offer" and s.get("type") == "shopify"]
    due = sorted((s for s in stores if now - (seen[s["id"]]["scanned_at"] if s["id"] in seen else 0) >= SCAN_HOURS * 3600),
                 key=lambda s: seen[s["id"]]["scanned_at"] if s["id"] in seen else 0)[:PER_CYCLE]
    events = []
    for s in due:
        old = json.loads(seen[s["id"]]["prices"]) if s["id"] in seen else {}
        try:
            new = snapshot(s["domain"], fetch)
        except Exception as exc:   # keep the old prices and try again in SCAN_HOURS
            print(f"[整店] {s['name']} 读取失败：{type(exc).__name__}: {str(exc)[:120]}")
            new = None
        with con:
            con.execute("INSERT OR REPLACE INTO store_prices(store, scanned_at, prices) VALUES(?,?,?)",
                        (s["id"], now, json.dumps(new if new else old)))
        sale = compare(old, new) if new and old else None
        print(f"[整店] {s['name']} {len(new or {})} 件" + (f"，其中 {sale['dropped']} 件降价" if sale else ""))
        if sale:
            events.append({**sale, "store": s["id"], "name": s["name"], "url": f"https://{s['domain']}", "at": now})
    if events:
        with con:
            recent = [e for e in db.get_meta(con, "store_sales", []) if now - e["at"] < KEEP_DAYS * 86400]
            db.set_meta(con, "store_sales", events + recent)
    return events


def describe(e: dict) -> str:
    return (f"查到的 {e['of']} 件商品里有 {e['dropped']} 件降价，"
            f"其中一半降到了之前价格的 {round(e['ratio'] * 100)}% 或更低。")


def notify(con, events: list[dict]) -> None:
    """Push each sale to every user who has not turned storewide alerts off."""
    for user in con.execute("SELECT * FROM users ORDER BY id").fetchall():
        if json.loads(user["settings"] or "{}").get("store_sales", True) is False:
            continue
        n = Notifier(key=user["bark"], server="", dry_run=auth.dev_mode())
        for e in events:
            n.send(f"品牌活动：{e['name']}", describe(e) + "\n结账时才减的折扣这里看不到，以店里为准。",
                   click=e["url"], priority=4, group="品牌活动")
