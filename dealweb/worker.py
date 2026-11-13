"""One check cycle: run dealbot's runner once per user, sharing fetched pages between users."""
from __future__ import annotations

import json
import statistics
import time
import urllib.parse
from datetime import datetime, timezone

from dealbot import runner, sources as bot_sources
from dealbot.matching import storage_sizes
from dealbot.notify import Notifier

from . import auth, db, stores

DEFAULT_TTL = 25 * 60     # a 30-minute cycle re-fetches; users in the same cycle share one request
ERROR_TTL = 25 * 60       # a failed request is not retried for other users in the same cycle
HOST_PAUSE = 1.5          # seconds between two real requests to the same host
SETTINGS = {"interval_minutes": 30, "max_pushes_per_run": 6}
USER_SETTINGS = ("daily_digest_hour",)


class CachedFetch:
    """Drop-in for dealbot.fetch.fetch backed by the http_cache table.

    One request per URL per TTL no matter how many users watch the same thing; the pause
    only applies to real network requests.
    """

    def __init__(self, con, real, ttl_by_host: dict[str, float]):
        self.con, self.real, self.ttl_by_host = con, real, ttl_by_host
        self.last_hit: dict[str, float] = {}
        self.requests = 0

    def __call__(self, url: str, **kw) -> str:
        host = urllib.parse.urlsplit(url).hostname or ""
        now = time.time()
        row = self.con.execute("SELECT body, error, fetched_at FROM http_cache WHERE url=?", (url,)).fetchone()
        if row:
            ttl = ERROR_TTL if row["error"] else self.ttl_by_host.get(host, DEFAULT_TTL)
            if now - row["fetched_at"] < ttl:
                if row["error"]:
                    raise RuntimeError(row["error"])
                return row["body"]
        wait = self.last_hit.get(host, 0) + HOST_PAUSE - now
        if wait > 0:
            time.sleep(wait)
        self.last_hit[host] = time.time()
        self.requests += 1
        body = error = None
        try:
            body = self.real(url, **kw)
            return body
        except Exception as exc:
            error = str(exc)[:200] or type(exc).__name__
            raise RuntimeError(error) from None
        finally:
            with self.con:
                self.con.execute("INSERT OR REPLACE INTO http_cache(url, body, error, fetched_at) VALUES(?,?,?,?)",
                                 (url, body, error, time.time()))


def ttl_by_host(sources: list[dict]) -> dict[str, float]:
    out = {}
    for s in sources:
        host = s.get("domain") or urllib.parse.urlsplit(s.get("url", "")).hostname
        if host and s.get("ttl_minutes"):
            out[host] = s["ttl_minutes"] * 60 - 300   # a little under, so the cycle that is due does fetch
    return out


def product_sources(product: dict, sources: list[dict]) -> list[str]:
    """Stores the user ticked, otherwise every source tagged with the product's category."""
    known = {s["id"] for s in sources}
    picked = [s for s in product.get("sources") or [] if s in known]
    if picked:
        return picked
    cat = product.get("category") or "other"
    return [s["id"] for s in sources if cat in s.get("categories", [])]


def auto_reference(offers, sources: list[dict], manual: set[str], remembered: dict) -> dict:
    """Reference prices read from the shops, for products whose owner left "参考原价" empty.

    Source field "reference":
    - "product" (Best Buy): the shop's own new listings give the list price per storage size, and
      every other offer of that product is compared against it. Marketplace sellers make up their
      "regular price", so their listings are ignored.
    - "offer" (brand stores): each item is compared against its own list price only.
    Returns {product_id: msrp_cfg}. Prices seen in earlier rounds are kept while the shop has no stock.
    """
    kind = {s["name"]: s.get("reference") for s in sources}
    seen: dict[str, dict[str, list[float]]] = {}
    for o in offers:
        if o.product in manual or o.condition != "new" or o.extra.get("seller"):
            continue
        price = o.regular_price or o.price
        if kind.get(o.source) == "offer":
            o.extra["list_price"] = price
        elif kind.get(o.source) == "product":
            # ponytail: the largest size in a title is taken as its storage, so "16GB RAM 512GB" is 512;
            # wrong for titles that only mention RAM. Fill in 参考原价 by hand for those.
            sizes = storage_sizes(o.title)
            seen.setdefault(o.product, {}).setdefault(str(max(sizes)) if sizes else "default", []).append(price)
    out = {}
    for pid in {*remembered, *seen} - manual:
        # median_low: a real listed price, and one stray accessory or bundle can't drag it.
        ref = {**remembered.get(pid, {}), **{t: statistics.median_low(v) for t, v in seen.get(pid, {}).items()}}
        if ref:
            out[pid] = ref
    return out


def run_user(con, user, sources: list[dict], *, now: datetime | None = None, force_digest: bool = False) -> dict:
    """Run one user's check with whatever fetch function dealbot.sources currently has."""
    if not db.try_lock(con, f"user:{user['id']}"):
        return {}
    try:
        products = [{**p, "sources": product_sources(p, sources)}
                    for p in db.user_products(con, user["id"]) if p.get("enabled", True)]
        row = con.execute("SELECT state FROM user_state WHERE user_id=?", (user["id"],)).fetchone()
        state = json.loads(row["state"]) if row else {}
        user_settings = {k: v for k, v in json.loads(user["settings"] or "{}").items() if k in USER_SETTINGS}
        cfg = {"settings": {**SETTINGS, **user_settings}, "products": products, "sources": sources}
        if products:
            notifier = Notifier(key=user["bark"], server="", dry_run=auth.dev_mode())
            # Collect without the owner's condition / top price / stock limits: a "used only" product still
            # needs the shop's new listings to learn the list price. The limits are applied right after.
            # ponytail: an offer two products both match goes to the first one even if its limits then drop it.
            wide = {**cfg, "settings": {**cfg["settings"], "skip_out_of_stock": False},
                    "products": [{**p, "conditions": [], "max_price": None} for p in products]}
            offers, posts, health = runner.collect(wide, pause=0)
            # A remembered price only counts while the search words it came from are unchanged.
            sig = {p["id"]: json.dumps([p.get("query"), p.get("include"), p.get("exclude")], ensure_ascii=False)
                   for p in products}
            old_sig = state.get("auto_msrp_sig", {})
            auto = auto_reference(offers, sources, {p["id"] for p in products if p.get("msrp")},
                                  {k: v for k, v in state.get("auto_msrp", {}).items() if old_sig.get(k) == sig.get(k)})
            by_id = {p["id"]: p for p in products}
            skip_oos = runner.settings(cfg)["skip_out_of_stock"]

            def wanted(o) -> bool:
                p = by_id[o.product]
                return ((not p.get("conditions") or o.condition in p["conditions"])
                        and o.price <= (p.get("max_price") or 10**9)
                        and not (skip_oos and o.in_stock is False))
            for p in products:
                p["msrp"] = p.get("msrp") or auto.get(p["id"], {})
            state = runner.run(cfg, state, notifier, now=now, force_digest=force_digest,
                               fetched=([o for o in offers if wanted(o)], posts, health))
            state["auto_msrp"], state["auto_msrp_sig"] = auto, {k: sig[k] for k in auto}
            latest = runner.latest_json(cfg, state)
            latest["push_errors"] = notifier.errors[:3]
        else:
            state["last_run"] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
            latest = {"generated_at": state["last_run"], "products": [], "offers": [], "posts": [], "health": {}}
        latest["history"] = state.get("history", {})
        state.pop("_assessed", None)
        with con:
            con.execute("INSERT OR REPLACE INTO user_state(user_id, state, latest) VALUES(?,?,?)",
                        (user["id"], json.dumps(state, ensure_ascii=False), json.dumps(latest, ensure_ascii=False)))
        return latest
    finally:
        db.unlock(con, f"user:{user['id']}")


def with_cache(con, sources: list[dict]):
    """Context manager: route dealbot's fetches through the shared cache."""
    class _Ctx:
        def __enter__(self):
            self.real = bot_sources.fetch
            self.fetch = bot_sources.fetch = CachedFetch(con, self.real, ttl_by_host(sources))
            return self.fetch

        def __exit__(self, *exc):
            bot_sources.fetch = self.real
    return _Ctx()


def run_cycle(con, *, only_user: int | None = None) -> None:
    sources = db.load_sources()
    health: dict = {}
    with with_cache(con, sources) as fetch:
        q = "SELECT * FROM users" + (" WHERE id=?" if only_user else "") + " ORDER BY id"
        for user in con.execute(q, (only_user,) if only_user else ()).fetchall():
            try:
                latest = run_user(con, user, sources)
            except Exception as exc:   # one user's failure must not stop the others
                print(f"用户 {user['id']} 检查失败：{type(exc).__name__}: {exc}")
                continue
            for name, h in (latest.get("health") or {}).items():
                agg = health.setdefault(name, {"ok": True, "raw": 0, "matched": 0})
                agg["ok"] = agg["ok"] and h["ok"]
                agg["raw"] += h.get("raw", 0)
                agg["matched"] += h.get("matched", 0)
                if h.get("error"):
                    agg["error"] = h["error"]
        print(f"本轮真实请求 {fetch.requests} 次。")
        if not only_user:
            try:   # uncached on purpose: a store's product list is megabytes and only read every few hours
                stores.notify(con, stores.scan_due(con, sources, fetch.real))
            except Exception as exc:
                print(f"整店检查失败：{type(exc).__name__}: {exc}")
    if not only_user:
        with con:
            db.set_meta(con, "health", {"at": time.time(), "sources": health})
            con.execute("DELETE FROM http_cache WHERE fetched_at < ?", (time.time() - 86400,))
