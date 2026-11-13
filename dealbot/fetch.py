"""HTTP fetching with browser-like headers (stdlib only)."""
from __future__ import annotations

import gzip
import time
import urllib.error
import urllib.request
import zlib

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def fetch(url: str, *, accept: str | None = None, timeout: int = 25, retries: int = 1) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": accept or "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    data = gzip.decompress(data)
                elif enc == "deflate":
                    data = zlib.decompress(data)
                charset = resp.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            last = RuntimeError(f"HTTP {exc.code}")
            if exc.code in (401, 403, 404, 429):
                break  # retrying a block/404 won't help, and retrying a rate limit makes it worse
        except Exception as exc:  # network errors, timeouts
            last = exc
        if attempt < retries:
            time.sleep(3)
    assert last is not None
    raise last
