#!/usr/bin/env bash
# 在 VPS 上安装比价机器人（Ubuntu 22.04+/Debian 12+，以 root 运行）。可重复运行：已有的配置会保留。
#   curl -fsSL https://raw.githubusercontent.com/cflank/deal-bot/main/deploy/install.sh | sudo bash
# 重新填写 Bark Key：在上面的命令末尾加 `-s -- --reset-bark`
set -euo pipefail

REPO_HTTPS="https://github.com/cflank/deal-bot.git"
REPO_SSH="git@github.com:cflank/deal-bot.git"
APP_DIR="/opt/deal-bot"
CONF_DIR="/etc/dealbot"
SVC_USER="dealbot"
RESET_BARK=0
[[ "${1:-}" == "--reset-bark" ]] && RESET_BARK=1

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local __v; read -r -p "$1" __v </dev/tty || true; printf '%s' "$__v"; }
as_user() { runuser -u "$SVC_USER" -- env GIT_SSH_COMMAND="ssh -i $CONF_DIR/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" "$@"; }

[[ $EUID -eq 0 ]] || die "请用 root 运行（或在命令前加 sudo）。"
[[ -r /dev/tty ]] || die "需要在交互式终端里运行（脚本会提问）。"

say "1/7 安装依赖"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq git python3 openssh-client ca-certificates tzdata >/dev/null
else
  command -v git >/dev/null && command -v python3 >/dev/null || die "请先安装 git 和 python3（≥3.11）。"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 版本 $(python3 -V 2>&1) 太旧，需要 3.11 以上（建议 Ubuntu 24.04 或 Debian 12）。"
ok "$(python3 -V)，$(git --version)"

say "2/7 创建运行账户和代码目录"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/dealbot --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" /var/lib/dealbot/.ssh
if [[ -d "$APP_DIR/.git" ]]; then
  runuser -u "$SVC_USER" -- git -C "$APP_DIR" fetch -q origin main
  runuser -u "$SVC_USER" -- git -C "$APP_DIR" reset -q --hard origin/main
else
  install -d -o "$SVC_USER" -g "$SVC_USER" "$APP_DIR"
  runuser -u "$SVC_USER" -- git clone -q "$REPO_HTTPS" "$APP_DIR"
fi
ok "代码在 $APP_DIR"

say "3/7 Bark 推送 Key"
install -d -m 750 -o root -g "$SVC_USER" "$CONF_DIR"
if [[ -f "$CONF_DIR/env" && $RESET_BARK -eq 0 ]]; then
  ok "已存在 $CONF_DIR/env，沿用（要重填请加 --reset-bark）"
else
  echo "打开 iPhone 上的 Bark，复制首页的推送网址（https://api.day.app/…），粘贴到下面。"
  echo "多台手机就一台一台粘贴，全部填完后直接按回车。输入内容不会显示在屏幕上。"
  keys=()
  while :; do
    read -r -s -p "第 $(( ${#keys[@]} + 1 )) 台手机的 Bark 网址或 Key（直接回车结束）：" k </dev/tty || true
    echo
    k="${k//[[:space:]\"\'\$\`\\]/}"
    [[ -z "$k" ]] && break
    # 只保留 https://服务器/Key，去掉 Bark 示例网址后面的中文提示文字
    if [[ "$k" =~ ^(https?://[^/]+/[^/?#]+) ]]; then k="${BASH_REMATCH[1]}"; fi
    keys+=("$k")
  done
  [[ ${#keys[@]} -gt 0 ]] || die "至少需要一个 Bark Key。"
  umask 027
  printf 'BARK_KEY="%s"\n' "$(IFS=,; echo "${keys[*]}")" > "$CONF_DIR/env"
  chown root:"$SVC_USER" "$CONF_DIR/env"; chmod 640 "$CONF_DIR/env"
  ok "已保存 ${#keys[@]} 台手机（$CONF_DIR/env，只有 root 和 $SVC_USER 可读）"
fi

say "4/7 GitHub 部署密钥（用于把看板数据推到 data 分支）"
if [[ ! -f "$CONF_DIR/deploy_key" ]]; then
  ssh-keygen -q -t ed25519 -N "" -C "deal-bot-vps@$(hostname)" -f "$CONF_DIR/deploy_key"
fi
chown "$SVC_USER":"$SVC_USER" "$CONF_DIR/deploy_key" "$CONF_DIR/deploy_key.pub"
chmod 600 "$CONF_DIR/deploy_key"
push_test() {
  local tmp; tmp="$(runuser -u "$SVC_USER" -- mktemp -d)"
  as_user git -C "$tmp" init -q -b vps-push-test &&
  as_user git -C "$tmp" -c user.name=t -c user.email=t@localhost commit -q --allow-empty -m test &&
  as_user git -C "$tmp" push -q "$REPO_SSH" vps-push-test >/dev/null 2>&1 &&
  as_user git -C "$tmp" push -q "$REPO_SSH" :vps-push-test >/dev/null 2>&1
  local rc=$?; rm -rf "$tmp"; return $rc
}
if push_test; then
  ok "部署密钥已可写入仓库"
else
  echo
  echo "请在浏览器打开：https://github.com/cflank/deal-bot/settings/keys/new"
  echo "  Title 填：VPS $(hostname)"
  echo "  Key 粘贴下面这一整行，并勾选 “Allow write access”，然后点 Add key："
  echo
  cat "$CONF_DIR/deploy_key.pub"
  echo
  for attempt in 1 2 3 4 5; do
    ask "添加好后按回车测试（第 $attempt 次）…" >/dev/null
    if push_test; then ok "部署密钥已可写入仓库"; break; fi
    echo "还不能写入：确认已勾选 “Allow write access”，并且整行公钥都粘贴了。"
    [[ $attempt -eq 5 ]] && die "部署密钥一直无法写入，先停在这里。重新运行本脚本即可继续。"
  done
fi

say "5/7 安装定时任务（每 5 分钟唤醒；实际检查间隔按网页设置）"
cat > /etc/systemd/system/dealbot.service <<EOF
[Unit]
Description=比价机器人：检查一次
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$SVC_USER
EnvironmentFile=$CONF_DIR/env
ExecStart=$APP_DIR/deploy/run.sh run
TimeoutStartSec=15min
Nice=10
EOF
cat > /etc/systemd/system/dealbot.timer <<'EOF'
[Unit]
Description=比价机器人：每 5 分钟唤醒

[Timer]
OnCalendar=*:0/5
RandomizedDelaySec=30
Persistent=false

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
ok "已安装 dealbot.service 和 dealbot.timer"

say "6/7 测试推送"
if runuser -u "$SVC_USER" -- bash -c "set -a; . '$CONF_DIR/env'; set +a; cd '$APP_DIR' && python3 -m dealbot --mode test-notify"; then
  ok "测试推送已发出，请看手机是否收到“✅ 比价机器人测试推送”"
else
  echo "测试推送失败（原因见上方）。Key 填错的话，重新运行本脚本并加 --reset-bark。"
fi

say "7/7 立即检查一次并启动定时器"
if as_user env TRIGGER=manual bash -c "set -a; . '$CONF_DIR/env'; set +a; '$APP_DIR/deploy/run.sh' run"; then
  ok "首次检查完成，看板（https://cflank.github.io/deal-bot/ ）点“刷新”应显示“刚刚”"
else
  die "首次检查失败（原因见上方）。修好后重新运行本脚本即可。"
fi
systemctl enable --now dealbot.timer >/dev/null
ok "定时器已启动：$(systemctl show dealbot.timer -p NextElapseUSecRealtime --value 2>/dev/null || true)"

cat <<EOF

────────────────────────────────────────────
部署完成。最后一步：到 GitHub 关闭那边的定时运行，避免重复推送：
  https://github.com/cflank/deal-bot/settings/variables/actions/new
  Name 填 GH_SCHEDULE，Value 填 off
（网页上的“立即检查”“测试推送”按钮仍然由 GitHub 执行，不受影响。）

常用命令：
  看最近日志      journalctl -u dealbot -n 100 --no-pager
  立即检查一次    systemctl start dealbot.service
  下次运行时间    systemctl list-timers dealbot.timer
  暂停 / 恢复     systemctl disable --now dealbot.timer   /   systemctl enable --now dealbot.timer
  重填 Bark Key   重新运行安装命令并加 --reset-bark
  卸载           systemctl disable --now dealbot.timer; rm -rf /etc/systemd/system/dealbot.* $APP_DIR $CONF_DIR; userdel -r $SVC_USER
────────────────────────────────────────────
EOF
