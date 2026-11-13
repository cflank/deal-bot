"""HTTP server: JSON API + static files. Meant to sit behind Caddy (HTTPS) on 127.0.0.1."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dealbot.notify import Notifier, parse_devices

from . import auth, db, worker

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
         ".ico": "image/x-icon", ".webmanifest": "application/manifest+json", ".woff2": "font/woff2"}
CATEGORIES = {"electronics": "电子数码", "fashion": "服饰箱包", "other": "其它", "deals": "只盯优惠帖子"}
CONDITIONS = ("new", "openbox", "refurb", "used")
MAX_BODY = 64 * 1024
START_PER_IP_HOUR = 20
_ip_hits: dict[str, list[float]] = {}
_check_lock = threading.Lock()   # on-demand checks patch a module global; one at a time


def max_products() -> int:
    return int(os.environ.get("DEALWEB_MAX_PRODUCTS", "10"))


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ validation

def _text(v, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def _words(v, max_items: int, limit: int = 40) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[,，\n]+", v)
    out = []
    for item in v or []:
        w = _text(item, limit)
        if w and w not in out:
            out.append(w)
    return out[:max_items]


def _price(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if 0 < f < 10**7 else None


def clean_product(data: dict, old: dict | None = None) -> dict:
    """Validated product config. Keys the form doesn't know (imported legacy rules) are kept."""
    name = _text(data.get("name"), 60)
    if not name:
        raise ApiError("请填写商品名称")
    cfg = dict(old or {})
    cfg.update(
        name=name,
        query=_text(data.get("query"), 80) or name,
        category=data.get("category") if data.get("category") in CATEGORIES else "other",
        include=_words(data.get("include"), 10) or [name],
        exclude=_words(data.get("exclude"), 40),
        conditions=[c for c in CONDITIONS if c in (data.get("conditions") or [])],
        enabled=bool(data.get("enabled", True)),
    )
    known = {s["id"] for s in db.load_sources()}
    cfg["sources"] = [s for s in data.get("sources") or [] if s in known]
    for key in ("min_price", "max_price", "alert_below"):
        cfg[key] = _price(data.get(key))
    if cfg["min_price"] and cfg["max_price"] and cfg["min_price"] > cfg["max_price"]:
        raise ApiError("最低价不能高于最高价")
    msrp = data.get("msrp") or {}
    tiers = {str(int(k)): _price(v) for k, v in msrp.items() if str(k).isdigit() and _price(v)}
    cfg["msrp"] = dict(list(tiers.items())[:8]) or ({"default": _price(msrp.get("default"))}
                                                     if _price(msrp.get("default")) else {})
    # Per-condition "alert at or below N% of the reference price" (dealbot.matching.assess).
    pcts = data.get("max_pct") or {}
    cfg["max_pct"] = {c: p for c in CONDITIONS if (p := _price(pcts.get(c))) and p <= 150}
    return cfg


def public_user(con, user) -> dict:
    return {
        "phone": user["phone"], "is_admin": bool(user["is_admin"]),
        "devices": len(parse_devices(user["bark"])),
        "settings": {"daily_digest_hour": 9, "store_sales": True, **json.loads(user["settings"] or "{}")},
        "max_products": max_products(),
        "categories": CATEGORIES,
        "sources": [{"id": s["id"], "name": s["name"], "categories": s.get("categories", []),
                     "condition": s.get("condition")} for s in db.load_sources()],
    }


def check_in_background(user_id: int) -> None:
    """Check one user right after they change their list, so the board isn't empty for 30 minutes."""
    def job():
        with _check_lock:
            con = db.connect()
            try:
                worker.run_cycle(con, only_user=user_id)
            except Exception as exc:
                print(f"即时检查失败：{type(exc).__name__}: {exc}")
            finally:
                con.close()
    threading.Thread(target=job, daemon=True).start()


# ---------------------------------------------------------------------- routes

def r_start(h, con, user, m, body):
    now = time.time()
    hits = [t for t in _ip_hits.get(h.client_ip, []) if now - t < 3600]
    if len(hits) >= START_PER_IP_HOUR:
        raise ApiError("请求太频繁，请稍后再试", 429)
    _ip_hits[h.client_ip] = hits + [now]
    mode = auth.start(con, body.get("phone", ""), body.get("invite", ""), body.get("bark", ""))
    return {"ok": True, "mode": mode}


def r_verify(h, con, user, m, body):
    token = auth.verify(con, body.get("phone", ""), body.get("code", ""))
    h.set_session = token
    return {"ok": True}


def r_logout(h, con, user, m, body):
    auth.logout(con, h.session_token)
    h.set_session = ""
    return {"ok": True}


def r_me(h, con, user, m, body):
    return public_user(con, user)


def r_products(h, con, user, m, body):
    row = con.execute("SELECT state FROM user_state WHERE user_id=?", (user["id"],)).fetchone()
    auto = json.loads(row["state"]).get("auto_msrp", {}) if row else {}
    # auto_msrp: the reference price the worker found by itself, shown in the form (read-only).
    return {"products": [{**p, "auto_msrp": auto.get(p["id"], {})} for p in db.user_products(con, user["id"])]}


def r_product_create(h, con, user, m, body):
    count = con.execute("SELECT COUNT(*) FROM products WHERE user_id=?", (user["id"],)).fetchone()[0]
    if count >= max_products():
        raise ApiError(f"每个账户最多 {max_products()} 个商品")
    cfg = clean_product(body)
    with con:
        pid = con.execute("INSERT INTO products(user_id, cfg, created_at) VALUES(?,?,?)",
                          (user["id"], json.dumps(cfg, ensure_ascii=False), time.time())).lastrowid
    check_in_background(user["id"])
    return {**cfg, "id": f"p{pid}"}


def _own_product(con, user, m):
    row = con.execute("SELECT id, cfg FROM products WHERE id=? AND user_id=?", (int(m.group(1)), user["id"])).fetchone()
    if not row:
        raise ApiError("找不到这个商品", 404)
    return row


def r_product_update(h, con, user, m, body):
    row = _own_product(con, user, m)
    cfg = clean_product(body, json.loads(row["cfg"]))
    with con:
        con.execute("UPDATE products SET cfg=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), row["id"]))
    check_in_background(user["id"])
    return {**cfg, "id": f"p{row['id']}"}


def r_product_delete(h, con, user, m, body):
    row = _own_product(con, user, m)
    with con:
        con.execute("DELETE FROM products WHERE id=?", (row["id"],))
    check_in_background(user["id"])
    return {"ok": True}


def r_dashboard(h, con, user, m, body):
    row = con.execute("SELECT latest FROM user_state WHERE user_id=?", (user["id"],)).fetchone()
    latest = json.loads(row["latest"]) if row else {}
    week = time.time() - 7 * 86400
    latest["store_sales"] = [e for e in db.get_meta(con, "store_sales", []) if e["at"] >= week]
    return latest


def r_settings(h, con, user, m, body):
    settings = json.loads(user["settings"] or "{}")
    if "daily_digest_hour" in body:
        hour = int(body["daily_digest_hour"])
        if not -1 <= hour <= 23:
            raise ApiError("每日行情时间不对")
        settings["daily_digest_hour"] = hour
    if "store_sales" in body:
        settings["store_sales"] = bool(body["store_sales"])
    bark = user["bark"]
    if "bark" in body:
        bark = auth.check_bark(body["bark"])
    with con:
        con.execute("UPDATE users SET settings=?, bark=? WHERE id=?", (json.dumps(settings), bark, user["id"]))
    return public_user(con, con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())


def r_test_push(h, con, user, m, body):
    n = Notifier(key=user["bark"], server="", dry_run=auth.dev_mode())
    n.send("✅ 比价机器人测试推送", f"能看到这条说明推送已配好（共 {len(n.devices)} 台手机）。", priority=5)
    if n.errors:
        raise ApiError("有手机没收到：" + "；".join(n.errors), 502)
    return {"ok": True, "devices": len(n.devices)}


def r_admin(h, con, user, m, body):
    users = con.execute(
        "SELECT u.phone, u.is_admin, u.created_at, COUNT(p.id) AS products FROM users u "
        "LEFT JOIN products p ON p.user_id = u.id GROUP BY u.id ORDER BY u.id").fetchall()
    invites = con.execute("SELECT code, kind, phone, created_at FROM invites WHERE used_at IS NULL "
                          "ORDER BY created_at DESC").fetchall()
    return {"users": [dict(u) for u in users], "invites": [dict(i) for i in invites],
            "health": db.get_meta(con, "health", {})}


def r_admin_invite(h, con, user, m, body):
    return {"code": auth.new_invite(con)}


# (method, path regex, handler, access)   access: "public" | "user" | "admin"
ROUTES = [
    ("POST", r"/api/auth/start", r_start, "public"),
    ("POST", r"/api/auth/verify", r_verify, "public"),
    ("POST", r"/api/auth/logout", r_logout, "public"),
    ("GET", r"/api/me", r_me, "user"),
    ("GET", r"/api/products", r_products, "user"),
    ("POST", r"/api/products", r_product_create, "user"),
    ("PUT", r"/api/products/p(\d+)", r_product_update, "user"),
    ("DELETE", r"/api/products/p(\d+)", r_product_delete, "user"),
    ("GET", r"/api/dashboard", r_dashboard, "user"),
    ("PUT", r"/api/settings", r_settings, "user"),
    ("POST", r"/api/test-push", r_test_push, "user"),
    ("GET", r"/api/admin", r_admin, "admin"),
    ("POST", r"/api/admin/invites", r_admin_invite, "admin"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "dealweb"
    # HTTP/1.0 on purpose: every response closes the connection, so a request body we chose not
    # to read (404/405/415…) can never be parsed as the next request on a pooled proxy connection.

    def log_message(self, fmt, *args):   # never log bodies, cookies or Bark addresses
        print(f"{self.client_ip} {self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}")

    @property
    def client_ip(self) -> str:
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    @property
    def session_token(self) -> str | None:
        morsel = SimpleCookie(self.headers.get("Cookie") or "").get("sid")
        return morsel.value if morsel else None

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        extra = {"Cache-Control": "no-store"}
        token = getattr(self, "set_session", None)
        if token is not None:
            secure = "" if auth.dev_mode() else "; Secure"
            age = auth.SESSION_TTL if token else 0
            extra["Set-Cookie"] = f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}"
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        file = STATIC / name
        # Only plain files directly inside static/ are served.
        if "/" in name or "\\" in name or name.startswith(".") or not file.is_file() or file.suffix not in TYPES:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        cache = "no-cache"
        self._send(200, file.read_bytes(), TYPES[file.suffix], {"Cache-Control": cache})

    def _handle(self) -> None:
        self.set_session = None
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/"):
            if self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                self._send(405, b"Method not allowed", "text/plain; charset=utf-8")
            return
        for method, pattern, func, access in ROUTES:
            m = re.fullmatch(pattern, path)
            if m and method == self.command:
                break
        else:
            self._json(404, {"error": "没有这个接口"})
            return
        con = db.connect()
        try:
            body = {}
            if self.command != "GET":
                # CSRF: browsers can't send a cross-site JSON body without a preflight, and the
                # Origin (when present) must be this site.
                origin = self.headers.get("Origin")
                if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                    raise ApiError("来源不对", 403)
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    raise ApiError("需要 JSON 请求", 415)
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise ApiError("请求太大", 413)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ApiError("请求格式不对")
            user = auth.session_user(con, self.session_token)
            if access != "public" and not user:
                raise ApiError("请先登录", 401)
            if access == "admin" and not user["is_admin"]:
                raise ApiError("没有权限", 403)
            self._json(200, func(self, con, user, m, body))
        except (ApiError, auth.AuthError) as exc:
            self._json(getattr(exc, "status", 400), {"error": str(exc)})
        except (ValueError, TypeError, KeyError):
            self._json(400, {"error": "请求格式不对"})
        except Exception as exc:
            print(f"服务器错误 {path}: {type(exc).__name__}: {exc}")
            self._json(500, {"error": "服务器出错了，请稍后再试"})
        finally:
            con.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle


def serve(host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    db.connect(init=True).close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
