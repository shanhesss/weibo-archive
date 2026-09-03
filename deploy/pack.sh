#!/usr/bin/env bash
# ============================================================
# 微博存档工具 —— 本机打包脚本（在 Windows 的 git-bash / WSL / Mac / Linux 均可跑）
# 在仓库根目录运行：bash deploy/pack.sh
#
# 产出：仓库根目录 weibo-deploy.tar.gz
#   weibo-deploy/
#     weibo_server.py  weibo_web.html  yuque-sync-template.md   # 运行文件
#     weibo.db                                                  # 数据（可选）
#     deploy/install.sh  deploy/README.md  ...                  # 部署脚本与文档
#
# 参数：
#   --no-db    只打代码，服务器全新空库（不带本地 weibo.db）
#   --force    本地服务未停也强行打包（不推荐：拷出的库可能丢最近数据）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."        # 切到仓库根

OUT="weibo-deploy.tar.gz"
STAGE=".deploy_stage"
INC_DB=1
FORCE=0
for a in "$@"; do
  case "$a" in
    --no-db)  INC_DB=0 ;;
    --force)  FORCE=1  ;;
    *) echo "未知参数: $a（支持 --no-db / --force）"; exit 1 ;;
  esac
done

need() { [ -f "$1" ] || { echo "错误：仓库根缺 $1"; exit 1; }; }
need weibo_server.py
need weibo_web.html
need yuque-sync-template.md

if [ "$INC_DB" = 1 ]; then
  need weibo.db
  if [ -s weibo.db-wal ] && [ "$FORCE" != 1 ]; then
    echo "错误：存在未回收的 weibo.db-wal（$(stat -c%s weibo.db-wal) 字节），本地服务多半还在运行，"
    echo "      直接打包会丢最近写入的数据。请先停本地服务（双击 weibo_stop.vbs 或 taskkill），"
    echo "      确认 weibo.db-wal 消失后再跑；确要强打加 --force。"
    exit 1
  fi
fi

rm -rf "$STAGE"
mkdir -p "$STAGE/weibo-deploy/deploy"
cp deploy/install.sh deploy/README.md deploy/weibo.service deploy/nginx-weibo.conf \
   "$STAGE/weibo-deploy/deploy/"
cp weibo_server.py weibo_web.html yuque-sync-template.md "$STAGE/weibo-deploy/"
if [ "$INC_DB" = 1 ]; then cp weibo.db "$STAGE/weibo-deploy/"; fi

tar -czf "$OUT" -C "$STAGE" weibo-deploy
rm -rf "$STAGE"

echo "已生成 $OUT"
ls -lh "$OUT"
if [ "$INC_DB" = 1 ]; then
  echo "内容含 weibo.db（连带本地数据迁移）。上传到服务器后执行（账号为 ubuntu，传到 /tmp）："
else
  echo "内容仅代码（空库；install.sh 需加 ALLOW_FRESH=1 才放行）。上传到服务器后执行："
fi
echo "  scp weibo-deploy.tar.gz ubuntu@服务器IP:/tmp/"
if [ "$INC_DB" = 1 ]; then
  echo "  ssh ubuntu@服务器IP 'cd /tmp && tar xzf weibo-deploy.tar.gz && sudo bash weibo-deploy/deploy/install.sh'"
else
  echo "  ssh ubuntu@服务器IP 'cd /tmp && tar xzf weibo-deploy.tar.gz && sudo ALLOW_FRESH=1 bash weibo-deploy/deploy/install.sh'"
fi
