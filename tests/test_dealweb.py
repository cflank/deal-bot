import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ["DEALWEB_DEV"] = "1"   # no real pushes, no Secure cookie

from dealbot import runner, sources as bot_sources
from dealbot.matching import Offer, assess
from dealbot.sources import source_shopify
from dealweb import auth, db, server, worker
from dealweb.__main__ import import_legacy

BARK = "https://api.day.app/abcdefgh12345678"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DEALWEB_DB"] = str(Path(self.tmp.name) / "t.db")
        self.con = db.connect(init=True)
        self.codes = {}
        self._send = auth.send_code
        auth.send_code = lambda bark, code: self.codes.__setitem__(bark, code) or []
        self._bg = server.check_in_background
        server.check_in_background = lambda uid: None

    def tearDown(self):
        auth.send_code = self._send
        server.check_in_background = self._bg
        self.con.close()
        self.tmp.cleanup()

    def register(self, phone, kind="invite"):
        invite = auth.new_invite(self.con, kind)
        auth.start(self.con, phone, invite, BARK)
        return auth.verify(self.con, phone, self.codes[BARK])


class AuthTests(Base):
    def test_register_then_login(self):
        token = self.register("416-555-0101")
        user = auth.session_user(self.con, token)
        self.assertEqual(user["phone"], "14165550101")
        self.con.execute("DELETE FROM otp_sends")
        self.assertEqual(auth.start(self.con, "(416) 555 0101"), "login")
        self.assertTrue(auth.verify(self.con, "4165550101", self.codes[BARK]))

    def test_invite_checked_before_any_push_and_single_use(self):
        with self.assertRaises(auth.AuthError):
            auth.start(self.con, "4165550102", "nope", BARK)
        self.assertEqual(self.codes, {})
        invite = auth.new_invite(self.con)
        auth.start(self.con, "4165550102", invite, BARK)
        auth.verify(self.con, "4165550102", self.codes[BARK])
        with self.assertRaises(auth.AuthError):
            auth.start(self.con, "4165550103", invite, BARK)

    def test_unknown_phone_without_invite(self):
        with self.assertRaises(auth.AuthError):
            auth.start(self.con, "4165550104")

    def test_bark_host_allow_list(self):
        for bad in ("http://127.0.0.1:8080/abcdefgh", "https://evil.example/abcdefgh", ""):
            with self.assertRaises(auth.AuthError):
                auth.check_bark(bad)
        self.assertEqual(auth.check_bark(BARK + "/这里改成你自己的推送内容"), BARK)

    def test_wrong_code_attempts_are_limited(self):
        invite = auth.new_invite(self.con)
        auth.start(self.con, "4165550105", invite, BARK)
        for _ in range(auth.OTP_MAX_ATTEMPTS):
            with self.assertRaisesRegex(auth.AuthError, "不对"):
                auth.verify(self.con, "4165550105", "000000" if self.codes[BARK] != "000000" else "111111")
        with self.assertRaisesRegex(auth.AuthError, "太多"):
            auth.verify(self.con, "4165550105", self.codes[BARK])

    def test_send_rate_limit(self):
        self.register("4165550106")
        with self.assertRaisesRegex(auth.AuthError, "一分钟"):
            auth.start(self.con, "4165550106")

    def test_rebind_replaces_bark_and_logs_out(self):
        token = self.register("4165550107")
        code = auth.new_invite(self.con, "rebind", "14165550107")
        self.con.execute("DELETE FROM otp_sends")
        new = "https://api.day.app/zzzzzzzz99999999"
        auth.start(self.con, "4165550107", code, new)
        auth.verify(self.con, "4165550107", self.codes[new])
        self.assertIsNone(auth.session_user(self.con, token))
        self.assertEqual(self.con.execute("SELECT bark FROM users").fetchone()["bark"], new)


class CoreTests(unittest.TestCase):
    def test_condition_filter(self):
        st = runner.settings({})
        product = {"id": "p1", "name": "Coach bag", "conditions": ["used"]}
        self.assertFalse(runner._keep(Offer("S", "Coach bag", 100, "u"), product, {"id": "s"}, st))
        self.assertTrue(runner._keep(Offer("S", "Coach bag", 100, "u"), product, {"id": "s", "condition": "used"}, st))
        self.assertTrue(runner._keep(Offer("S", "Coach bag", 100, "u"), {"id": "p1", "name": "Coach bag"}, {"id": "s"}, st))

    def test_alert_below_applies_to_any_condition(self):
        product = {"id": "p1", "name": "x", "alert_below": 200}
        self.assertTrue(assess(Offer("S", "x", 199, "u", condition="used"), product).qualifies)
        self.assertFalse(assess(Offer("S", "x", 201, "u", condition="used"), product).qualifies)

    def test_percent_of_reference_price_rule(self):
        cfg = server.clean_product({"name": "x", "msrp": {"default": "1000"},
                                    "max_pct": {"used": "70", "new": "", "refurb": "999", "bogus": "5"}})
        self.assertEqual(cfg["max_pct"], {"used": 70})
        self.assertTrue(assess(Offer("S", "x", 700, "u", condition="used"), cfg).qualifies)
        self.assertFalse(assess(Offer("S", "x", 701, "u", condition="used"), cfg).qualifies)
        self.assertFalse(assess(Offer("S", "x", 700, "u", condition="new"), cfg).qualifies)

    def test_reference_price_found_automatically(self):
        srcs = [{"name": "BB", "reference": "product"}, {"name": "Brand", "reference": "offer"}, {"name": "K"}]

        def offer(source, title, price, cond="new", **kw):
            return Offer(source, title, price, "u", condition=cond, product="p1", **kw)
        mine = [offer("BB", "Phone 256GB", 1599.99),
                offer("BB", "Phone 256GB", 1249.99, regular_price=1699.99, extra={"seller": "第三方卖家"}),  # made-up list price
                offer("BB", "Phone 512GB", 1700, regular_price=1899.99),
                offer("BB", "Phone 256GB", 1000, "openbox", regular_price=1300)]
        used, bag = offer("K", "Phone 256GB", 800, "used"), offer("Brand", "Bag", 80, regular_price=100)
        ref = worker.auto_reference(mine + [used, bag], srcs, set(), {"p1": {"1024": 2499}})
        self.assertEqual(ref, {"p1": {"256": 1599.99, "512": 1899.99, "1024": 2499}})   # 1TB remembered from before
        product = {"id": "p1", "msrp": ref["p1"], "max_pct": {"used": 70}}
        a = assess(used, product)
        self.assertEqual((a.msrp, round(a.pct_of_msrp), a.qualifies), (1599.99, 50, True))
        self.assertIsNone(assess(offer("K", "Phone 128GB", 500, "used"), product).pct_of_msrp)   # no price for that size
        self.assertEqual(assess(offer("K", "Phone", 500, "used"), product).msrp, 1599.99)        # size unknown: cheapest
        self.assertEqual(assess(bag, {"id": "p1"}).msrp, 100)            # a brand store's own list price
        hand = offer("Brand", "Bag", 80, regular_price=100)
        self.assertEqual(worker.auto_reference(mine + [hand], srcs, {"p1"}, {}), {})   # filled in by hand: left alone
        self.assertNotIn("list_price", hand.extra)

    def test_product_only_searched_on_its_sources(self):
        calls = []
        old = dict(runner.OFFER_SOURCES)
        runner.OFFER_SOURCES["page"] = lambda cfg: calls.append(cfg["id"]) or []
        try:
            cfg = {"products": [{"id": "p1", "name": "bag", "sources": ["a"]}],
                   "sources": [{"id": "a", "name": "A", "type": "page", "url": "https://a/?q={query}"},
                               {"id": "b", "name": "B", "type": "page", "url": "https://b/?q={query}"}]}
            _, _, health = runner.collect(cfg, pause=0)
        finally:
            runner.OFFER_SOURCES.update(old)
        self.assertEqual(calls, ["a"])
        self.assertEqual(list(health), ["A"])

    def test_shopify_source(self):
        payload = {"resources": {"results": {"products": [
            {"title": "BRAVE Backpack", "price": "115.50", "compare_at_price_min": "165.00", "compare_at_price_max": "175.00", "available": True,
             "url": "/products/brave?_pos=1&_psq=backpack"},
            {"title": "Free thing", "price": "0.00", "url": "/products/free"}]}}}
        old, bot_sources.fetch = bot_sources.fetch, lambda url, **kw: json.dumps(payload)
        try:
            offers = source_shopify({"name": "M&N", "domain": "example.com", "query": "back pack"})
        finally:
            bot_sources.fetch = old
        self.assertEqual(len(offers), 1)
        self.assertEqual((offers[0].price, offers[0].regular_price, offers[0].url),
                         (115.5, 165.0, "https://example.com/products/brave"))


class WorkerTests(Base):
    def test_cache_shares_requests_between_users_and_isolates_data(self):
        self.register("4165550201")
        self.register("4165550202")
        for uid, alert in ((1, 500), (2, 50)):
            cfg = server.clean_product({"name": "Coach bag", "category": "other", "alert_below": alert})
            self.con.execute("INSERT INTO products(user_id, cfg, created_at) VALUES(?,?,0)", (uid, json.dumps(cfg)))
        self.con.commit()
        hits = []
        page = json.dumps({"products": [{"name": "Coach bag tote", "price": 120, "url": "/p/1"}]})

        def fake(url, **kw):
            hits.append(url)
            return page
        sources = [{"id": "w", "name": "W", "type": "page", "url": "https://w.example/s?q={query}", "categories": ["other"]}]
        old, bot_sources.fetch = bot_sources.fetch, fake
        try:
            with worker.with_cache(self.con, sources):
                users = self.con.execute("SELECT * FROM users ORDER BY id").fetchall()
                first = worker.run_user(self.con, users[0], sources)
                second = worker.run_user(self.con, users[1], sources)
        finally:
            bot_sources.fetch = old
        self.assertEqual(len(hits), 1)                       # same query fetched once for both users
        self.assertEqual([o["product"] for o in first["offers"]], ["p1"])
        self.assertEqual([o["product"] for o in second["offers"]], ["p2"])
        self.assertTrue(first["offers"][0]["qualifies"])     # alert_below 500
        self.assertFalse(second["offers"][0]["qualifies"])   # alert_below 50

    def test_used_only_product_still_learns_the_list_price(self):
        self.register("4165550204")
        cfg = server.clean_product({"name": "Phone", "conditions": ["used"], "max_price": 1000, "max_pct": {"used": 70}})
        self.con.execute("INSERT INTO products(user_id, cfg, created_at) VALUES(1,?,0)", (json.dumps(cfg),))
        self.con.commit()
        page = json.dumps({"products": [{"name": "Phone 256GB", "price": 1599.99, "url": "/p/new"},
                                        {"name": "Phone 256GB used", "price": 800, "url": "/p/used"}]})
        sources = [{"id": "s", "name": "S", "type": "page", "url": "https://s.example/?q={query}",
                    "reference": "product", "categories": ["other"]}]
        old, bot_sources.fetch = bot_sources.fetch, lambda url, **kw: page
        try:
            user = self.con.execute("SELECT * FROM users").fetchone()
            latest = worker.run_user(self.con, user, sources)
        finally:
            bot_sources.fetch = old
        self.assertEqual([(o["url"], o["msrp"], o["qualifies"]) for o in latest["offers"]],
                         [("https://s.example/p/used", 1599.99, True)])     # the new listing only taught the price
        state = json.loads(self.con.execute("SELECT state FROM user_state").fetchone()["state"])
        self.assertEqual(state["auto_msrp"], {"p1": {"256": 1599.99}})

    def test_import_legacy_keeps_alert_state(self):
        self.register("4165550203", kind="admin")
        d = Path(self.tmp.name)
        (d / "config.json").write_text(json.dumps({"products": [{"id": "iphone-air", "name": "iPhone Air"}]}), "utf-8")
        (d / "state.json").write_text(json.dumps({
            "offers": {"iphone-air|BB|u": {"product": "iphone-air", "alerted_price": 900}},
            "started_products": ["iphone-air"], "history": {"iphone-air": {"2026-11-12": {"256GB": 900}}}}), "utf-8")
        import_legacy(self.con, str(d / "config.json"), str(d / "state.json"))
        state = json.loads(self.con.execute("SELECT state FROM user_state").fetchone()["state"])
        self.assertEqual(state["started_products"], ["p1"])
        self.assertEqual(state["offers"]["p1|BB|u"]["alerted_price"], 900)
        self.assertIn("p1", state["history"])


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        self.httpd = server.serve("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def call(self, method, path, body=None, cookie=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {}),
                                              **({"Cookie": cookie} if cookie else {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read()), resp.headers.get("Set-Cookie")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), None

    def signup(self, phone):
        invite = auth.new_invite(self.con)
        self.assertEqual(self.call("POST", "/api/auth/start", {"phone": phone, "invite": invite, "bark": BARK})[0], 200)
        status, _, cookie = self.call("POST", "/api/auth/verify", {"phone": phone, "code": self.codes[BARK]})
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", cookie)
        return cookie.split(";")[0]

    def test_flow_and_isolation(self):
        self.assertEqual(self.call("GET", "/api/me")[0], 401)
        a = self.signup("4165550301")
        b = self.signup("4165550302")
        status, product, _ = self.call("POST", "/api/products", {
            "name": "Coach Tabby", "category": "fashion", "conditions": ["used", "bogus"],
            "include": "tabby, coach tabby", "alert_below": "250", "sources": ["turnabout", "nope"]}, a)
        self.assertEqual(status, 200)
        self.assertEqual((product["conditions"], product["include"], product["alert_below"], product["sources"]),
                         (["used"], ["tabby", "coach tabby"], 250.0, ["turnabout"]))
        self.assertEqual(len(self.call("GET", "/api/products", cookie=a)[1]["products"]), 1)
        self.assertEqual(self.call("GET", "/api/products", cookie=b)[1]["products"], [])
        self.assertEqual(self.call("PUT", f"/api/products/{product['id']}", {"name": "x"}, b)[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/products/{product['id']}", {}, b)[0], 404)
        self.assertEqual(self.call("GET", "/api/admin", cookie=a)[0], 403)
        me = self.call("GET", "/api/me", cookie=a)[1]
        self.assertNotIn("bark", json.dumps(me))             # the Bark address is never sent back
        self.assertEqual(self.call("POST", "/api/auth/logout", {}, a)[0], 200)
        self.assertEqual(self.call("GET", "/api/me", cookie=a)[0], 401)

    def test_unread_body_cannot_leak_into_next_request(self):
        import socket
        host, port = self.httpd.server_address
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 18\r\n\r\nGET /api/me HTTP/1")
            data = b""
            try:
                sock.sendall(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
                while chunk := sock.recv(65536):   # server must close after the first response
                    data += chunk
            except ConnectionError:
                pass   # closing with the body unread may reset the connection instead of ending it cleanly
        self.assertLessEqual(data.count(b"HTTP/1."), 1)       # the smuggled second request is never answered
        if data:
            self.assertTrue(data.startswith(b"HTTP/1.0 405"))

    def test_deleted_product_id_is_not_reused(self):
        a = self.signup("4165550304")
        first = self.call("POST", "/api/products", {"name": "a"}, a)[1]["id"]
        self.call("DELETE", f"/api/products/{first}", {}, a)
        self.assertNotEqual(self.call("POST", "/api/products", {"name": "b"}, a)[1]["id"], first)

    def test_product_cap_origin_and_static(self):
        a = self.signup("4165550303")
        os.environ["DEALWEB_MAX_PRODUCTS"] = "1"
        try:
            self.assertEqual(self.call("POST", "/api/products", {"name": "a"}, a)[0], 200)
            self.assertEqual(self.call("POST", "/api/products", {"name": "b"}, a)[0], 400)
        finally:
            del os.environ["DEALWEB_MAX_PRODUCTS"]
        self.assertEqual(self.call("POST", "/api/products", {"name": "c"}, a, {"Origin": "https://evil.example"})[0], 403)
        for path in ("/static/../db.py", "/static/.env", "/nope"):
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(self.base + path)


if __name__ == "__main__":
    unittest.main()
