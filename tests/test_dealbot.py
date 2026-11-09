import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dealbot import runner
from dealbot.matching import Offer, assess, detect_condition, detect_storage, matches_product
from dealbot.notify import Notifier
from dealbot.sources import Post, extract_json_blobs, parse_amazon, parse_feed, parse_price, walk_products

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text("utf-8"))
AIR = CFG["products"][0]
INC, EXC = AIR["include"], AIR["exclude"]
PODS = {"id": "pods", "name": "AirPods Pro 3", "query": "airpods pro 3", "include": ["airpods pro 3"],
        "exclude": ["case for"], "min_price": 150, "msrp": {"default": 329}, "max_pct": {"new": 80}}
PS5 = {"id": "ps5", "name": "PS5 Pro", "include": ["ps5 pro"], "target_price": {"default": 849}}


def offer(title, price, url="u", product="iphone-air", **kw):
    return Offer("BB", title, price, url, product=product, **kw)


class MatchingTests(unittest.TestCase):
    def test_product_match(self):
        self.assertTrue(matches_product("Apple iPhone Air 256GB - Sky Blue - Unlocked", INC, EXC))
        self.assertTrue(matches_product("iPhone 17 Air 512 GB", INC, EXC))
        self.assertTrue(matches_product("iPhoneAir 1TB used, with case", INC, EXC))
        self.assertFalse(matches_product("MacBook Air 13 M4", INC, EXC))
        self.assertFalse(matches_product("Clear Case for iPhone Air with MagSafe", INC, EXC))
        self.assertFalse(matches_product("WTB iPhone Air", INC, EXC))

    def test_storage_condition(self):
        tiers = {256: 1, 512: 1, 1024: 1}
        self.assertEqual(detect_storage("iPhone Air 1TB Space Black", tiers), 1024)
        self.assertEqual(detect_storage("iPhone Air 512 GB", tiers), 512)
        self.assertIsNone(detect_storage("iPhone Air 12GB RAM", tiers))
        self.assertEqual(detect_condition("Refurbished iPhone Air 256GB"), "refurb")
        self.assertEqual(detect_condition("Open Box - Apple iPhone Air"), "openbox")
        self.assertEqual(detect_condition("Apple iPhone Air (Renewed)"), "refurb")
        self.assertIsNone(detect_condition("Apple iPhone Air 256GB"))

    def test_assess_tiered(self):
        a = assess(offer("iPhone Air 256GB", 1199), AIR)
        self.assertTrue(a.qualifies)
        self.assertEqual(a.label, "256GB")
        self.assertFalse(assess(offer("iPhone Air 256GB", 1399), AIR).qualifies)
        self.assertTrue(assess(offer("iPhone Air 512GB", 1399), AIR).qualifies)
        a = assess(offer("iPhone Air Sky Blue", 1500), AIR)
        self.assertTrue(a.storage_guessed)
        self.assertEqual(a.tier, 256)
        self.assertFalse(assess(offer("iPhone Air 256GB", 1100, condition="used"), AIR).qualifies)
        self.assertTrue(assess(offer("iPhone Air 256GB", 950, condition="used"), AIR).qualifies)
        self.assertEqual(assess(offer("iPhone Air 1TB", 1999), AIR).label, "1TB")

    def test_assess_single_price_and_target_only(self):
        a = assess(offer("AirPods Pro 3", 259, product="pods"), PODS)
        self.assertTrue(a.qualifies)
        self.assertEqual(a.label, "")
        self.assertFalse(assess(offer("AirPods Pro 3", 299, product="pods"), PODS).qualifies)
        a = assess(offer("PS5 Pro", 829, product="ps5"), PS5)
        self.assertTrue(a.qualifies)
        self.assertIsNone(a.pct_of_msrp)
        self.assertFalse(assess(offer("PS5 Pro", 899, product="ps5"), PS5).qualifies)


class ParserTests(unittest.TestCase):
    def test_parse_price(self):
        self.assertEqual(parse_price("$1,299.99"), 1299.99)
        self.assertEqual(parse_price({"currentPrice": {"amount": "$1,099.00", "raw_amount": "1099.00"}}), 1099.0)
        self.assertEqual(parse_price([{"price": 1200}, {"price": 1100}]), 1100)
        self.assertIsNone(parse_price("Call for price"))

    def test_apple_refurb_bootstrap(self):
        html = """<script>window.REFURB_GRID_BOOTSTRAP = {"tiles":[
          {"title":"Refurbished iPhone Air 256GB - Sky Blue (Unlocked)",
           "price":{"currentPrice":{"amount":"$1,229.00","raw_amount":"1229.00"}},
           "productDetailsUrl":"/ca/shop/product/FXYZ/refurbished-iphone-air"}]};</script>"""
        found = [t for b in extract_json_blobs(html) for t in walk_products(b, "https://www.apple.com/ca/shop/refurbished/iphone")]
        self.assertEqual(found, [("Refurbished iPhone Air 256GB - Sky Blue (Unlocked)", 1229.0,
                                  "https://www.apple.com/ca/shop/product/FXYZ/refurbished-iphone-air", None)])

    def test_next_data_and_ld_json_with_stock(self):
        next_data = {"props": {"pageProps": {"listings": [
            {"title": "iPhone Air 256GB like new", "price": {"amount": 95000}, "url": "/v-cell-phone/x/1"},
            {"title": "iPhone Air 512GB", "price": 1300, "url": "/p/2", "availabilityStatus": "OUT_OF_STOCK"},
            {"name": "Toronto", "id": 3}]}}}
        ld = {"@context": "https://schema.org", "@type": "Product", "name": "Apple iPhone Air 512GB",
              "offers": [{"@type": "Offer", "price": "1499.99", "availability": "https://schema.org/OutOfStock"},
                         {"@type": "Offer", "price": "1599.99", "availability": "https://schema.org/InStock"}]}
        html = (f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
                f'<script type="application/ld+json">{json.dumps(ld)}</script>')
        found = [t for b in extract_json_blobs(html) for t in walk_products(b, "https://www.kijiji.ca/b/")]
        self.assertIn(("iPhone Air 256GB like new", 95000.0, "https://www.kijiji.ca/v-cell-phone/x/1", None), found)
        self.assertIn(("iPhone Air 512GB", 1300.0, "https://www.kijiji.ca/p/2", False), found)
        # in-stock offer wins over the cheaper sold-out one
        self.assertIn(("Apple iPhone Air 512GB", 1599.99, "https://www.kijiji.ca/b/", True), found)
        self.assertEqual(len(found), 3)

    def test_shopify_suggest_json(self):
        body = json.dumps({"resources": {"results": {"products": [
            {"title": "Apple iPhone Air 256GB", "price": "1449.99", "url": "/products/iphone-air", "available": True}]}}})
        found = walk_products(extract_json_blobs(body)[0], "https://www.staples.ca/search")
        self.assertEqual(found, [("Apple iPhone Air 256GB", 1449.99, "https://www.staples.ca/products/iphone-air", True)])

    def test_amazon(self):
        html = """<div data-asin="B0ABCDEF12" data-index="1" data-component-type="s-search-result">
          <h2 aria-label="x"><span>Apple iPhone Air, 256GB, Space Black - Unlocked (Renewed)</span></h2>
          <span class="a-price"><span class="a-offscreen">$1,049.00</span></span></div>
          <div data-asin="B0ZZZZZZZZ" data-component-type="s-search-result"><h2><span>No price</span></h2></div>"""
        offers = parse_amazon(html, "Amazon.ca")
        self.assertEqual(len(offers), 1)
        self.assertEqual((offers[0].price, offers[0].url), (1049.0, "https://www.amazon.ca/dp/B0ABCDEF12"))

    def test_feeds(self):
        rss = """<?xml version="1.0"?><rss version="2.0"><channel><item><title>[Costco] iPhone Air 256GB $1,099</title>
          <link>https://forums.redflagdeals.com/x-1/</link><guid>t1</guid></item></channel></rss>"""
        atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Rogers iPhone Air $0 down</title>
          <link href="https://forums.redflagdeals.com/y-2/"/><id>t2</id></entry></feed>"""
        self.assertEqual(parse_feed(rss, "RFD")[0].link, "https://forums.redflagdeals.com/x-1/")
        self.assertEqual(parse_feed(atom, "RFD")[0].link, "https://forums.redflagdeals.com/y-2/")


class CollectTests(unittest.TestCase):
    def test_templating_matching_and_stock(self):
        calls = []

        def fake_page(src):
            calls.append(src["url"])
            if "airpods" in src["url"]:
                return [Offer(src["name"], "Apple AirPods Pro 3", 259, "https://w/pods"),
                        Offer(src["name"], "iPhone Air 256GB", 1199, "https://w/air-wrong-product")]
            return [Offer(src["name"], "Apple iPhone Air 256GB", 1199, "https://w/air"),
                    Offer(src["name"], "Apple iPhone Air 512GB", 1299, "https://w/air512", in_stock=False),
                    Offer(src["name"], "Clear case for iPhone Air", 999, "https://w/case")]

        def fake_rfd(src):
            return [Post("RFD", "t1", "AirPods Pro 3 $249 at Costco", "l1"), Post("RFD", "t2", "Random TV", "l2")]

        cfg = {"settings": {}, "products": [AIR, PODS],
               "sources": [{"name": "W", "type": "page", "url": "https://w/s?q={query}"},
                           {"name": "RFD", "type": "rfd", "url": "feed"}]}
        orig_o, orig_p = dict(runner.OFFER_SOURCES), dict(runner.POST_SOURCES)
        runner.OFFER_SOURCES["page"], runner.POST_SOURCES["rfd"] = fake_page, fake_rfd
        try:
            offers, posts, health = runner.collect(cfg, pause=0)
        finally:
            runner.OFFER_SOURCES.update(orig_o)
            runner.POST_SOURCES.update(orig_p)
        self.assertEqual(calls, ["https://w/s?q=iphone+air", "https://w/s?q=airpods+pro+3"])
        self.assertEqual(sorted((o.product, o.url) for o in offers),
                         [("iphone-air", "https://w/air"), ("pods", "https://w/pods")])
        self.assertEqual([(p.id, p.product) for p in posts], [("t1", "pods")])
        self.assertTrue(health["W"]["ok"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"settings": {}, "products": [AIR, PODS], "sources": []}
        self.morning = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # 07:00 Toronto

    def fetched(self, offers, posts=(), ok=True):
        health = {"BB": {"ok": True, "raw": 5, "matched": len(offers)}}
        if not ok:
            health["AMZ"] = {"ok": False, "raw": 0, "matched": 0, "error": "HTTP 503"}
        return list(offers), list(posts), health

    def test_first_run_then_alerts(self):
        state, n = {}, Notifier(topic="", dry_run=True)
        deal = offer("Apple iPhone Air 256GB Unlocked", 1199, "https://bb/1")
        meh = offer("Apple iPhone Air 512GB Unlocked", 1749, "https://bb/2")
        post = Post("RFD", "t1", "iPhone Air $999 at Costco", "https://rfd/1", product="iphone-air")
        runner.run(self.cfg, state, n, now=self.morning, fetched=self.fetched([deal, meh], [post]))
        self.assertEqual(len(n.sent), 1)
        self.assertIn("开始监控", n.sent[0]["title"])
        self.assertEqual(state["started_products"], ["iphone-air", "pods"])

        n.sent.clear()
        runner.run(self.cfg, state, n, now=self.morning, fetched=self.fetched([deal, meh], [post]))
        self.assertEqual(n.sent, [])

        n.sent.clear()
        runner.run(self.cfg, state, n, now=self.morning,
                   fetched=self.fetched([offer(deal.title, 1180, deal.url), meh]))
        self.assertEqual(n.sent, [])
        post2 = Post("RFD", "t2", "AirPods Pro 3 $229", "https://rfd/2", product="pods")
        pods = Offer("BB", "AirPods Pro 3", 249, "https://bb/pods", product="pods")
        runner.run(self.cfg, state, n, now=self.morning,
                   fetched=self.fetched([offer(deal.title, 1099, deal.url), meh, pods], [post2]))
        titles = [m["title"] for m in n.sent]
        self.assertEqual(len(titles), 3, titles)
        self.assertTrue(any(t.startswith("RFD") for t in titles))
        self.assertIn("iPhone Air 256GB $1,099 (-24%)", titles)
        self.assertIn("AirPods Pro 3 $249 (-24%)", titles)

    def test_new_product_later_gets_its_own_summary(self):
        state, n = {}, Notifier(topic="", dry_run=True)
        cfg1 = {"settings": {}, "products": [AIR], "sources": []}
        runner.run(cfg1, state, n, now=self.morning, fetched=self.fetched([offer("iPhone Air 256GB", 1199)]))
        n.sent.clear()
        pods = Offer("BB", "AirPods Pro 3", 199, "https://bb/pods", product="pods")
        runner.run(self.cfg, state, n, now=self.morning, fetched=self.fetched([offer("iPhone Air 256GB", 1199), pods]))
        self.assertEqual([m["title"] for m in n.sent], ["🤖 开始监控：AirPods Pro 3"])

    def test_digest_history_and_outputs(self):
        state, n = {"started_products": ["iphone-air", "pods"]}, Notifier(topic="", dry_run=True)
        later = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)  # 10:00 Toronto
        offers = [offer("Apple iPhone Air 256GB", 1449, "https://bb/1"),
                  offer("Apple iPhone Air 256GB", 1100, "https://kj/1", condition="used")]
        runner.run(self.cfg, state, n, now=later, fetched=self.fetched(offers, ok=False))
        self.assertEqual([m["title"] for m in n.sent], ["📊 每日行情"])
        self.assertIn("AMZ", n.sent[0]["message"])
        self.assertEqual(state["history"]["iphone-air"]["2026-11-20"], {"256GB": 1449, "256GB 二手": 1100})
        latest = runner.latest_json(self.cfg, state)
        self.assertEqual(len(latest["offers"]), 2)
        self.assertEqual(latest["products"][1]["name"], "AirPods Pro 3")
        json.dumps(latest)
        n.sent.clear()
        runner.run(self.cfg, state, n, now=later, fetched=self.fetched(offers))
        self.assertEqual(n.sent, [])


class CliTests(unittest.TestCase):
    def test_interval_skip(self):
        from dealbot.__main__ import _due
        cfg = {"settings": {"interval_minutes": 30}}
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(_due(cfg, {}, now))
        self.assertFalse(_due(cfg, {"last_run": "2026-11-20T11:45:00+00:00"}, now))
        self.assertTrue(_due(cfg, {"last_run": "2026-11-20T11:29:00+00:00"}, now))


if __name__ == "__main__":
    unittest.main()
