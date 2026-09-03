#!/usr/bin/env bash
# ============================================================
# 微博存档工具 —— Linux 服务器一键安装脚本（幂等，可重复执行）
# 目标系统：Ubuntu / Debian，需以 root 运行
#
# 用法（在解包后的 bundle 目录里执行）：
#     bash weibo-deploy/deploy/install.sh
#
# 可调环境变量：
#     AUTH_ADMIN_PASSWORD=xxx   空库首启的管理员密码；不设则随机生成并保存
#     SKIP_NGINX=1              跳过 nginx 反代安装（默认安装并在 80 监听）
#     FORCE_DB=1                允许用 bundle 里的 weibo.db 覆盖 /opt/weibo 已有库
#     ALLOW_FRESH=1             允许在没有 weibo.db 的情况下全新空库安装（防误用仓库代码）
# ============================================================
set -euo pipefail

# ---- 定位 bundle 根（本脚本上一级目录） ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(dirname "$SCRIPT_DIR")"
APP_DIR="/opt/weibo"
ENV_FILE="/etc/weibo.env"
SVC="/etc/systemd/system/weibo.service"

echo "==> bundle 目录: $BUNDLE_DIR"
[ "$(id -u)" -eq 0 ] || { echo "错误：请用 root 运行（sudo bash ...）"; exit 1; }

# ---- 必备运行文件检查（缺任一则中止） ----
for f in weibo_server.py weibo_web.html yuque-sync-template.md; do
  [ -f "$BUNDLE_DIR/$f" ] || { echo "错误：bundle 里缺 $f，请用本机 deploy/pack.sh 重新打包"; exit 1; }
done

# ---- Python 3 与 curl（Ubuntu 自带，缺则装） ----
NEED_APT=0
if ! command -v python3 >/dev/null; then
  echo "==> 缺 python3"; NEED_APT=1
fi
if ! command -v curl >/dev/null; then
  echo "==> 缺 curl"; NEED_APT=1
fi
if [ "$NEED_APT" = 1 ]; then
  apt-get update -y && apt-get install -y python3 curl
fi

# ---- 专用系统用户与目录（ADR-0010：数据目录 700） ----
if ! id weibo >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin weibo
fi
install -d -o weibo -g weibo -m 700 "$APP_DIR"

# ---- 拷贝运行文件（代码始终覆盖，可用来升级） ----
for f in weibo_server.py weibo_web.html yuque-sync-template.md; do
  install -o weibo -g weibo -m 644 "$BUNDLE_DIR/$f" "$APP_DIR/"
done

# ---- 数据文件：有 bundle 库则导入，绝不静默覆盖已有库 ----
DB="$APP_DIR/weibo.db"
HAVE_DB=0
if [ -f "$BUNDLE_DIR/weibo.db" ]; then
  if [ -f "$DB" ] && [ "${FORCE_DB:-0}" != 1 ]; then
    echo "==> $APP_DIR 已有 weibo.db，跳过 bundle 库导入（要覆盖删掉旧库或用 FORCE_DB=1）"
  else
    install -o weibo -g weibo -m 600 "$BUNDLE_DIR/weibo.db" "$DB"
    echo "==> 已导入 weibo.db（迁移现有数据）"
  fi
fi
[ -f "$DB" ] && HAVE_DB=1

# ---- 防呆：没有数据文件就默认拒绝（防止 AI 拿 git 仓库的纯代码装出空库） ----
if [ ! -f "$DB" ] && [ ! -f "$BUNDLE_DIR/weibo.db" ] && [ "${ALLOW_FRESH:-0}" != 1 ]; then
  echo "!! 错误：bundle 里没有 weibo.db（这通常是没经 deploy/pack.sh 打包、直接从仓库拉的代码）。"
  echo "   从仓库拿代码装出来 = 全新空库 = 用户数据丢失。请用本机 deploy/pack.sh 打的包重跑；"
  echo "   确要全新空库安装，再加 ALLOW_FRESH=1。"
  exit 1
fi

# ---- 运行环境变量 ----
# 全新空库：首启会自动建表、建 admin（密码用 AUTH_ADMIN_PASSWORD，未设则随机）
# 已有库（迁移）：admin 已存在，密码沿用原库，不写 AUTH_ADMIN_PASSWORD
PW_TXT="/root/weibo-admin-password.txt"
rm -f "$ENV_FILE"
if [ "$HAVE_DB" = 1 ]; then
  {
    echo "WEIBO_NO_BROWSER=1"
  } > "$ENV_FILE"
  echo "==> 已检测到现有库：管理员沿用原库账号（admin / 你原来在用的密码）"
else
  ADMIN_PW="${AUTH_ADMIN_PASSWORD:-}"
  if [ -z "$ADMIN_PW" ]; then
    ADMIN_PW="$(openssl rand -hex 12 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(12))')"
  fi
  {
    echo "WEIBO_NO_BROWSER=1"
    echo "AUTH_ADMIN_PASSWORD=$ADMIN_PW"
  } > "$ENV_FILE"
  echo "WEIBO_URL=http://<服务器IP>/"
  echo "WEIBO_ADMIN_USER=admin"
  echo "WEIBO_ADMIN_PASSWORD=$ADMIN_PW" > "$PW_TXT"
  echo "WEIBO_ADMIN_PASSWORD=$ADMIN_PW"
  echo "（密码也已存 $PW_TXT，仅 root 可读；登录后请立即在个人设置里修改）"
fi
chmod 600 "$ENV_FILE"

# ---- 安装并启动 systemd 服务 ----
install -m 644 "$SCRIPT_DIR/weibo.service" "$SVC"
systemctl daemon-reload
systemctl enable --now weibo >/dev/null
echo "==> 等待服务就绪…"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:8766/"; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  echo "!! 服务 30 秒内未就绪，最近日志："
  journalctl -u weibo -n 40 --no-pager || true
  exit 1
fi
echo "==> 微博存档服务已运行（127.0.0.1:8766）"

# ---- nginx 反代（SKIP_NGINX=1 可跳过） ----
if [ "${SKIP_NGINX:-0}" != 1 ]; then
  if ! command -v nginx >/dev/null; then
    echo "==> 安装 nginx"
    apt-get update -y && apt-get install -y nginx
  fi
  install -m 644 "$SCRIPT_DIR/nginx-weibo.conf" /etc/nginx/sites-available/weibo
  rm -f /etc/nginx/sites-enabled/default          # 关掉占用 80 的默认站点
  ln -sf /etc/nginx/sites-available/weibo /etc/nginx/sites-enabled/weibo
  nginx -t >/dev/null
  systemctl enable nginx >/dev/null 2>&1 || true
  systemctl restart nginx
  echo "==> nginx 已监听 80 并反代到本服务"
else
  echo "==> 已跳过 nginx（SKIP_NGINX=1）"
fi

# ---- 安装完成，回报项 ----
echo
echo "==================== 部署完成 ===================="
echo "WEIBO_HTTP=http://<服务器公网IP>/"
echo "访问入口 = http://<服务器公网IP>/   （HTTP 明文，建议尽快上 HTTPS / 限来源 IP）"
if [ "$HAVE_DB" = 1 ]; then
  echo "登录 = admin / 你本地原来在用的管理员密码（库是原样迁移的）"
else
  echo "登录 = admin / $ADMIN_PW（见 /root/weibo-admin-password.txt）"
fi
echo "管理 = 登录后右上角管理员入口，可改邀请码/重置密码"
echo "数据 = $APP_DIR（用户 weibo，目录 700）"
echo "日志 = journalctl -u weibo -f"
echo "=================================================="
