"""Push notifications via Bark (iOS, https://bark.day.app). Falls back to stdout without a key.

BARK_KEY may hold several devices, separated by newlines, commas or spaces. Each entry is a
bare device key or the URL the Bark app shows (https://api.day.app/KEY/...); a URL entry keeps
its own server, so phones on different (self-hosted) Bark servers can be mixed.
"""
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


def parse_devices(raw: str, server: str = "") -> list[tuple[str, str]]:
    """Split BARK_KEY into unique (key, server) pairs."""
    devices: list[tuple[str, str]] = []
    for part in re.split(r"[\s,;，；]+", raw or ""):
        if not part:
            continue
        key, srv = parse_key(part, server)
        pair = (key, (srv or DEFAULT_SERVER).rstrip("/"))
        if key and pair not in devices:
            devices.append(pair)
    return devices


class Notifier:
    def __init__(self, key: str | None = None, server: str | None = None, dry_run: bool = False):
        raw = key if key is not None else os.environ.get("BARK_KEY", "")
        srv = server if server is not None else os.environ.get("BARK_SERVER", "").strip()
        self.devices = parse_devices(raw, srv)
        self.dry_run = dry_run or not self.devices
        self.sent: list[dict] = []
        self.errors: list[str] = []

    @property
    def configured(self) -> bool:
        return bool(self.devices)

    def send(self, title: str, message: str, *, click: str | None = None,
             priority: int = 3, group: str = "比价机器人") -> None:
        # priority 5 breaks through Focus/Do Not Disturb; 2 arrives silently.
        level = "timeSensitive" if priority >= 5 else "passive" if priority <= 2 else "active"
        payload = {"title": title, "body": message, "group": group, "level": level, "isArchive": 1}
        if click:
            payload["url"] = click
        self.sent.append(payload)
        if self.dry_run:
            print(f"\n[通知预览] {title}\n{message}\n{click or ''}")
            return
        for n, (key, server) in enumerate(self.devices, 1):
            body = json.dumps({**payload, "device_key": key}).encode("utf-8")
            req = urllib.request.Request(f"{server}/push", data=body, method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    result = json.loads(resp.read().decode("utf-8") or "{}")
                if result.get("code", 200) != 200:
                    raise RuntimeError(result.get("message") or f"code {result.get('code')}")
            except Exception as exc:  # one phone failing must not stop the others or the run
                # Never log the key itself: identify the phone by its position in BARK_KEY.
                self.errors.append(f"第 {n} 台手机｜{title[:40]}：{type(exc).__name__}: {exc}")
                print(f"推送失败：{self.errors[-1]}")
