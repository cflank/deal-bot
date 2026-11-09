"""Push notifications via ntfy (https://ntfy.sh). Falls back to stdout without a topic."""
from __future__ import annotations

import json
import os
import urllib.request


class Notifier:
    def __init__(self, server: str | None = None, topic: str | None = None,
                 token: str | None = None, dry_run: bool = False):
        self.server = (server or os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
        self.topic = topic if topic is not None else os.environ.get("NTFY_TOPIC", "").strip()
        self.token = token if token is not None else os.environ.get("NTFY_TOKEN", "").strip()
        self.dry_run = dry_run or not self.topic
        self.sent: list[dict] = []

    def send(self, title: str, message: str, *, click: str | None = None,
             priority: int = 3, tags: list[str] | None = None) -> None:
        payload = {"topic": self.topic, "title": title, "message": message,
                   "priority": priority, "tags": tags or []}
        if click:
            payload["click"] = click
        self.sent.append(payload)
        if self.dry_run:
            print(f"\n[通知预览] {title}\n{message}\n{click or ''}")
            return
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.server, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
