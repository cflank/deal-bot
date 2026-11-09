"""Push notifications via Bark (iOS, https://bark.day.app). Falls back to stdout without a key."""
from __future__ import annotations

import json
import os
import re
import urllib.request

DEFAULT_SERVER = "https://api.day.app"


def parse_key(raw: str, server: str = "") -> tuple[str, str]:
    """Accept a bare device key or the URL the Bark app shows (https://api.day.app/KEY/...)."""
    raw = (raw or "").strip()
    m = re.match(r"^(https?://[^/]+)/([^/?#]+)", raw)
    if m:
        return m.group(2), (server or m.group(1))
    return raw, server


class Notifier:
    def __init__(self, key: str | None = None, server: str | None = None, dry_run: bool = False):
        raw = key if key is not None else os.environ.get("BARK_KEY", "")
        srv = server if server is not None else os.environ.get("BARK_SERVER", "").strip()
        self.key, srv = parse_key(raw, srv)
        self.server = (srv or DEFAULT_SERVER).rstrip("/")
        self.dry_run = dry_run or not self.key
        self.sent: list[dict] = []
        self.errors: list[str] = []

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def send(self, title: str, message: str, *, click: str | None = None,
             priority: int = 3, group: str = "比价机器人") -> None:
        # priority 5 breaks through Focus/Do Not Disturb; 2 arrives silently.
        level = "timeSensitive" if priority >= 5 else "passive" if priority <= 2 else "active"
        payload = {"device_key": self.key, "title": title, "body": message,
                   "group": group, "level": level, "isArchive": 1}
        if click:
            payload["url"] = click
        self.sent.append(payload)
        if self.dry_run:
            print(f"\n[通知预览] {title}\n{message}\n{click or ''}")
            return
        req = urllib.request.Request(f"{self.server}/push", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
            if body.get("code", 200) != 200:
                raise RuntimeError(body.get("message") or f"code {body.get('code')}")
        except Exception as exc:  # one failed push must not abort the whole run
            self.errors.append(f"{title[:40]}: {type(exc).__name__}: {exc}")
            print(f"推送失败：{self.errors[-1]}")
