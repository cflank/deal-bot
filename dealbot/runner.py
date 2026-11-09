"""One monitoring pass: fetch all sources for all products, compare with state, push alerts."""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from .matching import CONDITION_LABELS, Assessment, Offer, assess, detect_condition, matches_product
from .notify import Notifier
from .sources import OFFER_SOURCES, POST_SOURCES, Post

try:
    from zoneinfo import ZoneInfo
    TORONTO = ZoneInfo("America/Toronto")
except Exception:  # tzdata missing
    TORONTO = timezone(timedelta(hours=-5))

HISTORY_DAYS = 120


def money(v: float) -> str:
    return f"${v:,.2f}" if v % 1 else f"${v:,.0f}"


def enabled_products(cfg: dict) -> list[dict]:
    return [p for p in cfg.get("products", []) if p.get("enabled", True) and p.get("id")]


def settings(cfg: dict) -> dict:
    defaults = {"interval_minutes": 30, "daily_digest_hour": 9, "hst_pct": 13, "price_drop_cad": 30,
                "urgent_discount_pct": 20, "max_pushes_per_run": 6, "skip_out_of_stock": True}
    return {**defaults, **(cfg.get("settings") or {})}


# ------------------------------------------------------------------ messages

def describe(o: Offer, a: Assessment, product: dict, hst_pct: float) -> str:
    cond = CONDITION_LABELS.get(o.condition, o.condition)
    lines = [f"来源：{o.source}｜{cond}", o.title]
    if a.msrp:
        saving = a.msrp - o.price
        if saving > 0:
            lines.append(f"价格：{money(o.price)}（原价 {money(a.msrp)}，省 {money(saving)} / {a.discount_pct:.0f}%）")
        else:
            lines.append(f"价格：{money(o.price)}（原价 {money(a.msrp)}）")
    else:
        lines.append(f"价格：{money(o.price)}" + (f"（目标价 {money(a.target)}）" if a.target else ""))
    if o.regular_price:
        lines.append(f"商家原标价：{money(o.regular_price)}")
    if o.condition == "used":
        lines.append("私人交易一般不另收税；当面验货")
    elif hst_pct:
        lines.append(f"含 {hst_pct:g}% HST 约 {money(o.price * (1 + hst_pct / 100))}")
    if a.carrier:
        lines.append(f"⚠️ 标题含 {a.carrier}，可能需绑定运营商/套餐")
    if o.extra.get("seller"):
        lines.append(f"⚠️ {o.extra['seller']}")
    if a.storage_guessed:
        lines.append(f"⚠️ 未识别容量，按 {a.label} 比较")
    return "\n".join(lines)


def headline(o: Offer, a: Assessment, product: dict) -> str:
    off = f" (-{a.discount_pct:.0f}%)" if a.discount_pct is not None and a.discount_pct >= 1 else ""
    cond = "" if o.condition == "new" else f"{CONDITION_LABELS.get(o.condition, '')} "
    label = f"{a.label} " if a.label else ""
    return f"{product['name']} {cond}{label}{money(o.price)}{off}"


def short_line(i: int, o: Offer, a: Assessment) -> str:
    cond = CONDITION_LABELS.get(o.condition, o.condition)
    label = f" {a.label}" if a.label else ""
    rel = f"（原价的 {a.pct_of_msrp:.0f}%）" if a.pct_of_msrp is not None else ""
    return f"{i}. {money(o.price)}{label} {cond} · {o.source}{rel}"


# ------------------------------------------------------------------ fetching

def _fill(template: str, product: dict) -> str:
    q = product.get("query") or product["name"]
    return (template.replace("{query}", urllib.parse.quote_plus(q))
                    .replace("{query_slug}", urllib.parse.quote("-".join(q.lower().split()))))


def _per_product(src: dict) -> bool:
    return src["type"] == "bestbuy" or "{query" in src.get("url", "")


def _keep(o: Offer, product: dict, src: dict, st: dict) -> bool:
    if not matches_product(o.title, product.get("include") or [product["name"]], product.get("exclude") or []):
        return False
    lo = product.get("min_price") or 0
    hi = product.get("max_price") or 10**9
    if not lo <= o.price <= hi:
        return False
    if st["skip_out_of_stock"] and o.in_stock is False:
        return False
    o.product = product["id"]
    o.condition = src.get("condition") or detect_condition(o.title) or "new"
    return True


def collect(cfg: dict, pause: float = 1.5) -> tuple[list[Offer], list[Post], dict]:
    products = enabled_products(cfg)
    st = settings(cfg)
    offers: list[Offer] = []
    posts: list[Post] = []
    health: dict = {}
    for src in cfg.get("sources", []):
        if not src.get("enabled", True) or not products:
            continue
        name, typ = src["name"], src["type"]
        raw = matched = 0
        errors: list[str] = []
        jobs = [(p, {**src, "url": _fill(src.get("url", ""), p), "query": p.get("query") or p["name"]})
                for p in products] if _per_product(src) else [(None, src)]
        for i, (only, job) in enumerate(jobs):
            if i:
                time.sleep(pause)
            try:
                if typ in POST_SOURCES:
                    items = POST_SOURCES[typ](job)
                    raw += len(items)
                    for post in items:
                        for p in products:
                            if matches_product(post.title, p.get("include") or [p["name"]], p.get("exclude") or []):
                                post.product = p["id"]
                                posts.append(post)
                                matched += 1
                                break
                elif typ in OFFER_SOURCES:
                    items = OFFER_SOURCES[typ](job)
                    raw += len(items)
                    for o in items:
                        for p in ([only] if only else products):
                            if _keep(o, p, src, st):
                                offers.append(o)
                                matched += 1
                                break
                else:
                    raise ValueError(f"未知数据源类型 {typ}")
            except Exception as exc:
                who = f"{only['name']}: " if only else ""
                errors.append(f"{who}{type(exc).__name__}: {exc}"[:160])
        health[name] = {"ok": not errors, "raw": raw, "matched": matched}
        if errors:
            health[name]["error"] = "；".join(errors)[:300]
        print(f"[{name}] {health[name]}")
    return offers, posts, health


# ---------------------------------------------------------------------- run

def run(cfg: dict, state: dict, notifier: Notifier, *, now: datetime | None = None,
        force_digest: bool = False, fetched=None) -> dict:
    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    st = settings(cfg)
    products = {p["id"]: p for p in enabled_products(cfg)}
    max_push = int(st["max_pushes_per_run"])

    offers, posts, health = fetched if fetched is not None else collect(cfg)
    known = state.setdefault("offers", {})
    seen_posts = state.setdefault("posts", {})
    started = set(state.get("started_products", []))

    best_by_key: dict[str, Offer] = {}
    for o in offers:
        if o.product in products and (o.key not in best_by_key or o.price < best_by_key[o.key].price):
            best_by_key[o.key] = o

    assessed: dict[str, list[tuple[Offer, Assessment]]] = {pid: [] for pid in products}
    to_alert: list[tuple[Offer, Assessment]] = []
    for key, o in best_by_key.items():
        a = assess(o, products[o.product])
        assessed[o.product].append((o, a))
        rec = known.setdefault(key, {"first_seen": now_iso})
        rec.update(title=o.title, price=o.price, url=o.url, source=o.source, product=o.product,
                   condition=o.condition, last_seen=now_iso, lowest=min(o.price, rec.get("lowest", o.price)))
        alerted = rec.get("alerted_price")
        if a.qualifies and (alerted is None or o.price <= alerted - st["price_drop_cad"]):
            rec["alerted_price"] = o.price
            if o.product in started:
                to_alert.append((o, a))
    for lst in assessed.values():
        lst.sort(key=lambda t: t[1].sort_key(t[0].price))
    to_alert.sort(key=lambda t: t[1].sort_key(t[0].price))

    new_posts = [p for p in posts if p.id not in seen_posts and p.product in products]
    for p in posts:
        seen_posts.setdefault(p.id, now_iso)

    # Products monitored for the first time: one summary instead of a burst of alerts.
    fresh = [pid for pid in products if pid not in started]
    if fresh:
        ok = sum(1 for h in health.values() if h["ok"])
        lines = [f"数据源 {ok}/{len(health)} 正常。"]
        for pid in fresh:
            items = assessed[pid]
            qual = sum(1 for _, a in items if a.qualifies)
            lines.append(f"\n【{products[pid]['name']}】找到 {len(items)} 个报价" + (f"，{qual} 个已达提醒条件" if qual else ""))
            lines += [short_line(i, o, a) for i, (o, a) in enumerate(items[:3], 1)]
            lines += [f"· RFD：{p.title}" for p in new_posts if p.product == pid][:2]
        failed = [n for n, h in health.items() if not h["ok"]]
        if failed:
            lines.append("\n暂时抓取失败：" + "、".join(failed))
        names = "、".join(products[pid]["name"] for pid in fresh)
        first = next((assessed[pid][0][0].url for pid in fresh if assessed[pid]), None)
        notifier.send(f"🤖 开始监控：{names}"[:200], "\n".join(lines), click=first)
        started.update(fresh)

    pushes = 0
    for p in new_posts:
        if p.product in fresh or pushes >= max_push:
            continue
        notifier.send(f"RFD 新帖：{p.title}"[:200], f"{products[p.product]['name']}\n{p.title}\n{p.link}",
                      click=p.link or None, priority=4, group=products[p.product]["name"])
        pushes += 1
    rest = []
    for o, a in to_alert:
        if pushes >= max_push:
            rest.append((o, a))
            continue
        urgent = a.discount_pct is not None and a.discount_pct >= st["urgent_discount_pct"]
        notifier.send(headline(o, a, products[o.product]), describe(o, a, products[o.product], st["hst_pct"]),
                      click=o.url, priority=5 if urgent else 4, group=products[o.product]["name"])
        pushes += 1
    if rest:
        notifier.send(f"另有 {len(rest)} 个报价达到提醒条件",
                      "\n".join(f"{products[o.product]['name']} " + short_line(i, o, a)
                                for i, (o, a) in enumerate(rest[:10], 1)))

    prev_health = state.get("health", {})
    for name, h in health.items():
        h["fails"] = 0 if h["ok"] else prev_health.get(name, {}).get("fails", 0) + 1
    state["health"] = health

    local = now.astimezone(TORONTO)
    today = local.date().isoformat()
    hour = int(st["daily_digest_hour"])
    if force_digest or (not fresh and hour >= 0 and local.hour >= hour and state.get("last_digest") != today):
        send_digest(products, assessed, health, notifier)
        state["last_digest"] = today

    _record_history(state, assessed, today, now)

    cutoff_offer = (now - timedelta(days=14)).isoformat(timespec="seconds")
    cutoff_post = (now - timedelta(days=60)).isoformat(timespec="seconds")
    state["offers"] = {k: v for k, v in known.items() if v.get("last_seen", "") >= cutoff_offer}
    state["posts"] = {k: v for k, v in seen_posts.items() if v >= cutoff_post}
    state["started_products"] = sorted(started)
    state["recent_posts"] = (
        [{"product": p.product, "title": p.title, "link": p.link, "first_seen": now_iso} for p in new_posts]
        + [p for p in state.get("recent_posts", []) if p.get("first_seen", "") >= cutoff_post]
    )[:50]
    state["last_run"] = now_iso
    state["_assessed"] = assessed
    return state


def _series(o: Offer, a: Assessment) -> str:
    base = a.label or "价格"
    return f"{base} 二手" if o.condition == "used" else base


def _record_history(state: dict, assessed: dict, today: str, now: datetime) -> None:
    hist = state.setdefault("history", {})
    for pid, items in assessed.items():
        day = hist.setdefault(pid, {}).setdefault(today, {})
        for o, a in items:
            s = _series(o, a)
            day[s] = min(day.get(s, o.price), o.price)
    cutoff = (now - timedelta(days=HISTORY_DAYS)).date().isoformat()
    for pid in list(hist):
        hist[pid] = {d: v for d, v in hist[pid].items() if d >= cutoff}
        if not hist[pid]:
            del hist[pid]


def send_digest(products, assessed, health, notifier: Notifier) -> None:
    lines = []
    for pid, product in products.items():
        items = assessed.get(pid, [])
        lines.append(f"【{product['name']}】" + ("" if items else "暂无报价"))
        lines += [short_line(i, o, a) for i, (o, a) in enumerate(items[:4], 1)]
    ok = [n for n, h in health.items() if h["ok"]]
    bad = [f"{n}（连续 {h['fails']} 次）" for n, h in health.items() if not h["ok"]]
    lines.append(f"\n数据源正常 {len(ok)}/{len(health)}" + ("；失败：" + "、".join(bad) if bad else ""))
    first = next((items[0][0].url for items in assessed.values() if items), None)
    notifier.send("📊 每日行情", "\n".join(lines), click=first, priority=2)


# ------------------------------------------------------------------ outputs

def latest_json(cfg: dict, state: dict) -> dict:
    products = enabled_products(cfg)
    offers = []
    for pid, items in state.get("_assessed", {}).items():
        for o, a in items:
            rec = state["offers"].get(o.key, {})
            offers.append({
                "product": pid, "source": o.source, "title": o.title, "price": o.price, "url": o.url,
                "condition": o.condition, "tier": a.label, "msrp": a.msrp,
                "pct": None if a.pct_of_msrp is None else round(a.pct_of_msrp, 1),
                "qualifies": a.qualifies, "carrier": a.carrier, "storage_guessed": a.storage_guessed,
                "in_stock": o.in_stock, "regular_price": o.regular_price, "seller": o.extra.get("seller"),
                "first_seen": rec.get("first_seen"), "lowest": rec.get("lowest"),
            })
    return {
        "generated_at": state.get("last_run"),
        "interval_minutes": settings(cfg)["interval_minutes"],
        "products": [{"id": p["id"], "name": p["name"]} for p in products],
        "offers": offers,
        "posts": state.get("recent_posts", []),
        "health": state.get("health", {}),
    }


def step_summary(cfg: dict, state: dict) -> str:
    out = ["## 比价结果", ""]
    names = {p["id"]: p["name"] for p in enabled_products(cfg)}
    for pid, items in state.get("_assessed", {}).items():
        out += [f"### {names.get(pid, pid)}", "", "| # | 价格 | 规格 | 成色 | 原价% | 来源 | 商品 |", "|---|---|---|---|---|---|---|"]
        for i, (o, a) in enumerate(items[:15], 1):
            pct = f"{a.pct_of_msrp:.0f}%" if a.pct_of_msrp is not None else "-"
            title = o.title.replace("|", "/")[:70]
            out.append(f"| {i} | {money(o.price)} | {a.label or '-'} | {CONDITION_LABELS.get(o.condition, o.condition)}"
                       f" | {pct} | {o.source} | [{title}]({o.url}) |")
        out.append("")
    out += ["### 数据源", ""]
    for name, h in state.get("health", {}).items():
        status = f"✅ 匹配 {h['matched']} / 原始 {h['raw']}" if h["ok"] else f"❌ {h.get('error', '')}"
        out.append(f"- {name}：{status}")
    return "\n".join(out) + "\n"
