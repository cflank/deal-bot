"""Invite codes, Bark-delivered login codes and sessions. There are no passwords."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time

from dealbot.notify import Notifier, parse_devices

OTP_TTL = 300            # seconds a code stays valid
OTP_MAX_ATTEMPTS = 5
SEND_PER_MINUTE = 1
SEND_PER_HOUR = 5
SESSION_TTL = 30 * 86400
MAX_DEVICES = 5
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class AuthError(Exception):
    """Message is shown to the user as-is."""


def dev_mode() -> bool:
    return os.environ.get("DEALWEB_DEV") == "1"


def bark_servers() -> set[str]:
    raw = os.environ.get("DEALWEB_BARK_SERVERS", "https://api.day.app")
    return {s.strip().rstrip("/") for s in raw.split(",") if s.strip()}


def norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    # ponytail: 10 digits are assumed North American (+1); other numbers are kept as typed.
    # The phone number is only an account name here, so no real E.164 parsing.
    if len(digits) == 10:
        digits = "1" + digits
    if not 7 <= len(digits) <= 15:
        raise AuthError("手机号格式不对")
    return digits


def check_bark(raw: str) -> str:
    """Validate Bark addresses and return them normalised, one per line.

    The server POSTs to whatever host is given here, so hosts are allow-listed.
    """
    devices = parse_devices(raw or "")
    if not devices:
        raise AuthError("请填写 Bark 推送地址")
    if len(devices) > MAX_DEVICES:
        raise AuthError(f"最多 {MAX_DEVICES} 台手机")
    allowed = bark_servers()
    for key, server in devices:
        if server not in allowed:
            raise AuthError("只支持 Bark 官方服务器（https://api.day.app/…）的地址")
        if not _KEY_RE.match(key):
            raise AuthError("Bark 地址格式不对，请从 Bark App 首页复制")
    return "\n".join(f"{server}/{key}" for key, server in devices)


def send_code(bark: str, code: str) -> list[str]:
    """Push a login code; returns error strings (empty when every phone got it)."""
    n = Notifier(key=bark, server="", dry_run=dev_mode())
    n.send("比价机器人验证码", f"{code}（5 分钟内有效）", priority=5)
    return n.errors


def _hash(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def new_invite(con, kind: str = "invite", phone: str | None = None) -> str:
    code = secrets.token_urlsafe(6)
    with con:
        con.execute("INSERT INTO invites(code, kind, phone, created_at) VALUES(?,?,?,?)",
                    (code, kind, phone, time.time()))
    return code


def start(con, phone: str, invite: str = "", bark: str = "") -> str:
    """Send a code. Returns "login" or "register"."""
    phone = norm_phone(phone)
    invite = (invite or "").strip()
    user = con.execute("SELECT id, bark FROM users WHERE phone=?", (phone,)).fetchone()
    pending = None
    if invite:
        # The invite is checked before anything is pushed anywhere.
        inv = con.execute("SELECT kind, phone FROM invites WHERE code=? AND used_at IS NULL", (invite,)).fetchone()
        if not inv:
            raise AuthError("邀请码无效或已经用过")
        target = check_bark(bark)
        if inv["kind"] == "rebind":
            if not user or inv["phone"] != phone:
                raise AuthError("换绑码和手机号对不上")
        elif user:
            raise AuthError("这个手机号已经注册，直接登录即可")
        pending = {"invite": invite, "bark": target}
    elif user:
        target = user["bark"]
    else:
        raise AuthError("这个手机号还没注册，请填写邀请码和 Bark 地址")

    now = time.time()
    with con:
        con.execute("DELETE FROM otp_sends WHERE at < ?", (now - 3600,))
        recent = [r["at"] for r in con.execute("SELECT at FROM otp_sends WHERE phone=?", (phone,))]
        if sum(1 for t in recent if now - t < 60) >= SEND_PER_MINUTE:
            raise AuthError("验证码刚发过，请一分钟后再试")
        if len(recent) >= SEND_PER_HOUR:
            raise AuthError("验证码发送太频繁，请一小时后再试")
        con.execute("INSERT INTO otp_sends(phone, at) VALUES(?,?)", (phone, now))

    code = f"{secrets.randbelow(10**6):06d}"
    if send_code(target, code):
        raise AuthError("验证码没发出去，请检查 Bark 地址是否正确")
    with con:
        con.execute("INSERT OR REPLACE INTO otps(phone, code_hash, expires_at, attempts, pending) VALUES(?,?,?,0,?)",
                    (phone, _hash(phone, code), now + OTP_TTL, json.dumps(pending) if pending else None))
    return "register" if pending else "login"


def verify(con, phone: str, code: str) -> str:
    """Check a code, create the account if this was a registration, return a session token."""
    phone = norm_phone(phone)
    now = time.time()
    with con:
        row = con.execute("SELECT * FROM otps WHERE phone=?", (phone,)).fetchone()
        if not row or row["expires_at"] < now:
            raise AuthError("验证码已过期，请重新获取")
        if row["attempts"] >= OTP_MAX_ATTEMPTS:
            con.execute("DELETE FROM otps WHERE phone=?", (phone,))
            con.commit()
            raise AuthError("输错次数太多，请重新获取验证码")
        if not secrets.compare_digest(row["code_hash"], _hash(phone, re.sub(r"\D", "", code or ""))):
            con.execute("UPDATE otps SET attempts=attempts+1 WHERE phone=?", (phone,))
            con.commit()
            raise AuthError("验证码不对")
        con.execute("DELETE FROM otps WHERE phone=?", (phone,))

        user = con.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        if row["pending"]:
            pending = json.loads(row["pending"])
            inv = con.execute("SELECT kind FROM invites WHERE code=? AND used_at IS NULL",
                              (pending["invite"],)).fetchone()
            if not inv:
                raise AuthError("邀请码已被使用")
            if inv["kind"] == "rebind":
                con.execute("UPDATE users SET bark=? WHERE id=?", (pending["bark"], user["id"]))
                con.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
                uid = user["id"]
            else:
                uid = con.execute("INSERT INTO users(phone, bark, is_admin, created_at) VALUES(?,?,?,?)",
                                  (phone, pending["bark"], int(inv["kind"] == "admin"), now)).lastrowid
            con.execute("UPDATE invites SET used_at=?, used_by=? WHERE code=?", (now, uid, pending["invite"]))
        elif user:
            uid = user["id"]
        else:
            raise AuthError("账户不存在")

        token = secrets.token_urlsafe(32)
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        con.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES(?,?,?)",
                    (_hash(token), uid, now + SESSION_TTL))
    return token


def session_user(con, token: str | None):
    if not token:
        return None
    return con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires_at > ?",
        (_hash(token), time.time())).fetchone()


def logout(con, token: str | None) -> None:
    if token:
        with con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))
