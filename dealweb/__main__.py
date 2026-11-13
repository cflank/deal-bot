"""CLI: python3 -m dealweb serve | worker | invite [--admin] | rebind PHONE | import-legacy | probe-shop DOMAIN"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import auth, db, worker


def import_legacy(con, config_path: str, state_path: str) -> str:
    """Attach the single-user config.json/state.json to the first admin account."""
    user = con.execute("SELECT * FROM users WHERE is_admin=1 ORDER BY id LIMIT 1").fetchone()
    if not user:
        return "还没有管理员账户：先用 `invite --admin` 生成的邀请码注册。"
    if con.execute("SELECT 1 FROM products WHERE user_id=?", (user["id"],)).fetchone():
        return "管理员账户里已经有商品，不重复导入。"
    cfg = json.loads(Path(config_path).read_text("utf-8"))
    state = json.loads(Path(state_path).read_text("utf-8")) if Path(state_path).exists() else {}
    renamed = {}
    with con:
        for p in cfg.get("products", []):
            old_id = p.pop("id")
            p.setdefault("category", "electronics")
            new_id = con.execute("INSERT INTO products(user_id, cfg, created_at) VALUES(?,?,?)",
                                 (user["id"], json.dumps(p, ensure_ascii=False), time.time())).lastrowid
            renamed[old_id] = f"p{new_id}"
        # Re-key the state so already-alerted offers are not pushed again.
        offers = {}
        for key, rec in state.get("offers", {}).items():
            pid, rest = key.split("|", 1)
            if pid in renamed:
                offers[f"{renamed[pid]}|{rest}"] = {**rec, "product": renamed[pid]}
        state["offers"] = offers
        state["started_products"] = [renamed[p] for p in state.get("started_products", []) if p in renamed]
        state["history"] = {renamed[p]: v for p, v in state.get("history", {}).items() if p in renamed}
        state["recent_posts"] = [{**p, "product": renamed[p["product"]]}
                                 for p in state.get("recent_posts", []) if p.get("product") in renamed]
        state.pop("health", None)
        con.execute("INSERT OR REPLACE INTO user_state(user_id, state, latest) VALUES(?,?,'{}')",
                    (user["id"], json.dumps(state, ensure_ascii=False)))
    return f"已导入 {len(renamed)} 个商品和 {len(offers)} 条报价记录。"


def probe_shop(domain: str, query: str) -> str:
    from dealbot.sources import source_shopify
    offers = source_shopify({"name": domain, "domain": domain, "query": query})
    lines = [f"{domain}：搜 “{query}” 返回 {len(offers)} 条"]
    lines += [f"  {o.title[:50]}  ${o.price:g}" for o in offers[:5]]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="dealweb", description="比价机器人网站")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="启动网页服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--dev", action="store_true", help="开发模式：验证码和推送只打印，不真发")
    sub.add_parser("worker", help="检查一轮（由定时器调用）")
    s = sub.add_parser("invite", help="生成邀请码")
    s.add_argument("--admin", action="store_true", help="用这个邀请码注册的账户是管理员")
    s = sub.add_parser("rebind", help="给换了手机的用户生成一次性换绑码")
    s.add_argument("phone")
    s = sub.add_parser("import-legacy", help="把单人版的设置和状态导入管理员账户")
    s.add_argument("--config", default="config.json")
    s.add_argument("--state", default="data/state.json")
    s = sub.add_parser("probe-shop", help="试一个 Shopify 店铺域名能不能用")
    s.add_argument("domain")
    s.add_argument("query", nargs="?", default="bag")
    args = ap.parse_args(argv)

    if args.cmd == "probe-shop":
        print(probe_shop(args.domain, args.query))
        return 0
    if args.cmd == "serve":
        if args.dev:
            os.environ["DEALWEB_DEV"] = "1"
        from .server import serve
        httpd = serve(args.host, args.port)
        print(f"dealweb 运行在 http://{args.host}:{args.port}" + ("（开发模式：验证码打印在这里）" if auth.dev_mode() else ""))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    con = db.connect(init=True)
    if args.cmd == "worker":
        if not db.try_lock(con, "cycle"):
            print("上一轮还没跑完，跳过。")
            return 0
        try:
            worker.run_cycle(con)
        finally:
            db.unlock(con, "cycle")
    elif args.cmd == "invite":
        print(auth.new_invite(con, "admin" if args.admin else "invite"))
    elif args.cmd == "rebind":
        phone = auth.norm_phone(args.phone)
        if not con.execute("SELECT 1 FROM users WHERE phone=?", (phone,)).fetchone():
            print("没有这个手机号的账户。")
            return 1
        print(auth.new_invite(con, "rebind", phone))
    elif args.cmd == "import-legacy":
        print(import_legacy(con, args.config, args.state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
