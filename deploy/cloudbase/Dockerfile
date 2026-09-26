# 微博存档 · 云托管镜像（CloudBase CloudRun，ADR-0011）
# 构建上下文 = pack.sh --cloudbase 产出的 cloudbase-dist/（3 个运行文件 + 本 Dockerfile）
# 注意：只准打包进运行文件，weibo.db 绝不进镜像（数据走挂载盘快照，见 deploy/cloudbase/README.md）
FROM python:3.12-slim

WORKDIR /app
COPY weibo_server.py weibo_web.html yuque-sync-template.md ./
RUN mkdir -p /app/data

# 云托管形态（环境变量语义见 weibo_server.py 顶部）：
#   无头 + 监听全部接口；运行库放容器本地盘（真 SQLite 语义）；
#   快照落挂载盘 /mnt/weibo（对象存储挂载，控制台「存储设置」配置）；
#   WEIBO_BASE_PATH=/weibo：与同域名下其他服务共用备案域名时，挂在 /weibo 路由下
#   （云开发控制台「自定义域名」按路径路由），
#   将来换独立子域名根路径部署时把该变量置空即可，无需改代码。
ENV WEIBO_NO_BROWSER=1 \
    WEIBO_BIND=0.0.0.0 \
    WEIBO_DB=/app/data/weibo.db \
    WEIBO_COS_DIR=/mnt/weibo \
    WEIBO_BASE_PATH=/weibo \
    TZ=Asia/Shanghai

EXPOSE 8766
CMD ["python", "weibo_server.py"]
