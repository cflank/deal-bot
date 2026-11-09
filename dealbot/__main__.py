"""CLI: python3 -m dealbot [--config config.json] [--data data] [--mode run|digest|test-notify]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .notify import Notifier
from .runner import latest_json, run, settings, step_summary


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def _due(cfg: dict, state: dict, now: datetime) -> bool:
    """Scheduled runs fire often; only do real work once per configured interval."""
    last = state.get("last_run")
    if not last:
        return True
    elapsed = (now - datetime.fromisoformat(last)).total_seconds() / 60
    return elapsed >= float(settings(cfg)["interval_minutes"]) - 2


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="比价监控机器人")
    ap.add_argument("--config", default=str(here / "config.json"))
    ap.add_argument("--data", default=str(here / "data"), help="状态和看板数据目录")
    ap.add_argument("--mode", default="run", choices=["run", "digest", "test-notify"])
    ap.add_argument("--trigger", default="manual", help="schedule 时按设置的间隔跳过")
    ap.add_argument("--dry-run", action="store_true", help="只打印通知，不推送、不写文件")
    args = ap.parse_args(argv)

    notifier = Notifier(dry_run=args.dry_run)
    if not notifier.topic:
        print("未设置 NTFY_TOPIC：通知只打印到终端。")

    if args.mode == "test-notify":
        notifier.send("✅ 比价机器人测试推送", "能看到这条说明手机推送已配好。", tags=["white_check_mark"])
        _set_output("ran", "false")
        return 0

    cfg = json.loads(Path(args.config).read_text("utf-8"))
    data = Path(args.data)
    state_path = data / "state.json"
    state = json.loads(state_path.read_text("utf-8")) if state_path.exists() else {}

    now = datetime.now(timezone.utc)
    if args.trigger == "schedule" and args.mode == "run" and not _due(cfg, state, now):
        print(f"距上次运行不足 {settings(cfg)['interval_minutes']} 分钟，跳过。")
        _set_output("ran", "false")
        return 0

    state = run(cfg, state, notifier, now=now, force_digest=args.mode == "digest")

    summary = step_summary(cfg, state)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)

    latest = latest_json(cfg, state)
    history = state.get("history", {})
    state.pop("_assessed", None)
    if not args.dry_run:
        data.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        (data / "latest.json").write_text(json.dumps(latest, ensure_ascii=False), "utf-8")
        (data / "history.json").write_text(json.dumps(history, ensure_ascii=False), "utf-8")
    _set_output("ran", "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
