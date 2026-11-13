#!/usr/bin/env bash
# 在 VPS 上安装/更新比价机器人网站（Debian 12 / Ubuntu 22.04+，以 root 运行，可重复运行）。
#   bash install-web.sh --domain 38-188-49-85.sslip.io [--branch web]
# 数据在 /var/lib/dealweb/app.db，代码在 /opt/dealweb。管理命令：dealweb invite | rebind 手机号 | import-legacy …
set -euo pipefail

REPO="https://github.com/cflank/deal-bot.git"
APP_DIR="/opt/dealweb"
DATA_DIR="/var/lib/dealweb"
SVC_USER="dealbot"
DOMAIN=""
BRANCH="main"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift ;;
    --branch) BRANCH="${2:-}"; shift ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
  shift
done
[[ $EUID -eq 0 ]] || { echo "请用 root 运行。" >&2; exit 1; }
[[ -n "$DOMAIN" ]] || { echo "请用 --domain 指定网站域名。" >&2; exit 1; }

echo "==> 1/5 安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 ca-certificates tzdata caddy >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "需要 Python 3.11 以上。" >&2; exit 1; }

echo "==> 2/5 代码和数据目录"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/dealbot --shell /usr/sbin/nologin "$SVC_USER"
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$APP_DIR"
fi
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"

cat > /usr/local/bin/dealweb <<WRAP
#!/bin/sh
# 管理命令入口：以服务账户身份、用正式数据库运行。
cd $APP_DIR && exec runuser -u $SVC_USER -- env DEALWEB_DB=$DATA_DIR/app.db PYTHONDONTWRITEBYTECODE=1 python3 -m dealweb "\$@"
WRAP
chmod 755 /usr/local/bin/dealweb

echo "==> 3/5 系统服务"
cat > /etc/systemd/system/dealweb.service <<UNIT
[Unit]
Description=比价机器人网站
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=DEALWEB_DB=$DATA_DIR/app.db PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m dealweb serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/dealweb-worker.service <<UNIT
[Unit]
Description=比价机器人网站：检查一轮
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SVC_USER
WorkingDirectory=$APP_DIR
Environment=DEALWEB_DB=$DATA_DIR/app.db PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 -m dealweb worker
TimeoutStartSec=25min
Nice=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR
UNIT
cat > /etc/systemd/system/dealweb-worker.timer <<'UNIT'
[Unit]
Description=比价机器人网站：每 30 分钟检查

[Timer]
OnCalendar=*:0/30
RandomizedDelaySec=60
Persistent=false

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable -q dealweb.service dealweb-worker.timer
systemctl restart dealweb.service
systemctl start dealweb-worker.timer

echo "==> 4/5 HTTPS（Caddy）"
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8787
}
CADDY
systemctl enable -q caddy
systemctl reload caddy || systemctl restart caddy

echo "==> 5/5 检查"
sleep 2
systemctl is-active dealweb.service caddy.service dealweb-worker.timer
curl -fsS -o /dev/null -w "本机服务：HTTP %{http_code}\n" http://127.0.0.1:8787/
echo "网站：https://$DOMAIN （首次签发证书可能要等一分钟）"
echo "生成管理员邀请码：dealweb invite --admin"
