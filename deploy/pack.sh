#!/usr/bin/env bash
# ============================================================
# 微博存档工具 —— 云托管打包脚本（cloudbase-cloudrun 产品线，ADR-0011）
# 在仓库根目录运行：bash deploy/pack.sh
#
# 产出：cloudbase-dist/（运行代码 + Dockerfile）
# 铁律：绝不带 weibo.db —— 数据走对象存储挂载盘快照通道，
#       部署与验证流程见 deploy/cloudbase/README.md
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."        # 切到仓库根

DIST="cloudbase-dist"
need() { [ -f "$1" ] || { echo "错误：仓库根缺 $1"; exit 1; }; }
need weibo_server.py
need weibo_web.html
need yuque-sync-template.md
need deploy/cloudbase/Dockerfile

rm -rf "$DIST"
mkdir -p "$DIST"
cp weibo_server.py weibo_web.html yuque-sync-template.md "$DIST/"
cp deploy/cloudbase/Dockerfile "$DIST/"

echo "已生成云托管部署目录 $DIST/（只含代码 + Dockerfile，无 weibo.db）"
ls -lh "$DIST"
echo "下一步（本机已装并登录 cloudbase CLI；prod-xxxxxxxxxxxxxxxx 换成你的环境 ID）："
echo "  printf 'n\ny\n' | cloudbase cloudrun deploy -e prod-xxxxxxxxxxxxxxxx -s weibo-archive \\"
echo "    --source $DIST --port 8766 --min-num 1 --max-num 1 --force --wait"
