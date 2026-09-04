# 微博存档工具 · 服务器部署

把微博存档工具部署到 Linux 云服务器（Ubuntu / Debian）。**架构按 ADR-0010**：服务只监听
回环 `127.0.0.1:8766`，公网一律经 nginx 反代；数据目录 `/opt/weibo`（专用用户 `weibo`，
700/600 权限）；环境变量走 `/etc/weibo.env`（仅 root 可读）。

> 安全提示（当前快跑版）：走的是**裸 IP + HTTP 明文**，登录密码与会话在公网明文传输。
> 上线后请尽快：① 改强管理员密码；② 给服务器限来源 IP 或上域名 + HTTPS（certbot 一步）。
> 工具代码已有多用户鉴权（邀请码注册 + PBKDF2 + HttpOnly 会话），但仍不要公网裸奔长期使用。

> ⚠️ **给执行部署的 AI 的铁律**
> 运行代码与数据**只准用服务器上现成的部署包** `weibo-deploy.tar.gz`（解包出 `weibo-deploy/`）。
> **禁止 git clone、禁止从 GitHub / 任何远程仓库联网拉取代码或数据**——本仓库里的文档仅供
> 阅读，仓库里没有 `weibo.db`（它不进 git），从仓库拿代码 = 全新空库 = 用户数据丢失。
> 开工前先确认包在位：`test -f /tmp/weibo-deploy.tar.gz`；不在就请用户上传，不要自己去拉。

## 流程总览

```
本机（Windows）                云服务器
─────────────                 ─────────
bash deploy/pack.sh      →    （上传 weibo-deploy.tar.gz）
tar xzf weibo-deploy.tar.gz
bash weibo-deploy/deploy/install.sh
浏览器打开 http://<服务器IP>/ 登录
```

---

## 先分清：全新部署还是更新已有服务器

- **第一次装 / 换新服务器**（服务器上还没有 `weibo.db`）→ 走下方「第 1~3 步」的**带库包**
  （`pack.sh` 默认带库）。
- **已部署过、只是改功能代码**（服务器 `/opt/weibo/weibo.db` 已存在）→ 直接看下面的
  「更新已有服务器（非首次部署）」，用 **`pack.sh --no-db`**。

## 更新已有服务器（非首次部署，只改功能代码）

服务器上线后，数据（账号、博主/微博、cookie、语雀令牌与同步记录）以服务器上的
`/opt/weibo/weibo.db` 为准，**本地那份会越来越旧**。日常升级**只传代码、不背本地库**：

```bash
# 本机（仓库根，git-bash）——不必停本地服务（不带库的包不检查 weibo.db-wal）
bash deploy/pack.sh --no-db
scp weibo-deploy.tar.gz ubuntu@服务器IP:/tmp/
```

```bash
# 服务器
cd /tmp && tar xzf weibo-deploy.tar.gz
sudo bash weibo-deploy/deploy/install.sh      # 别加 FORCE_DB / ALLOW_FRESH
```

install.sh 只覆盖 3 个运行文件、**复用服务器已有库**。第 69 行的防呆只在
「服务器没库 **且** 包里也没库」时触发；已部署的服务器库存在，所以**无需 `ALLOW_FRESH`**。
（`pack.sh --no-db` 结尾提示加 ALLOW_FRESH 是给全新空库的通用话术，本场景请忽略。）

### 只改单文件时更省（可选轻量路径）

服务器运行时读的是磁盘上的**单文件**（如 `/opt/weibo/weibo_web.html`，服务进程从磁盘读）。
只改一个运行文件时，可不必打整包、连 `--no-db` 都省：

```bash
# 本机
scp weibo_web.html ubuntu@服务器IP:/tmp/

# 服务器
sudo ls -l /opt/weibo/            # 先确认 weibo_web.html 确实在这层
sudo install -o weibo -g weibo -m 0644 /tmp/weibo_web.html /opt/weibo/weibo_web.html
sudo systemctl restart weibo
# 验证：新版页面里应有移动端标记（旧版没有）
curl -s http://127.0.0.1:8766/ | grep -c bloggerStrip    # ≥1 = 已生效
```

其余两个运行文件 `weibo_server.py`、`yuque-sync-template.md` 同理可按完整路径替换。该轻量路径
同样只动运行文件、**不碰库**，但只适合替换某个已知单文件；它仍要守铁律——只传工作区里那一个
文件即可，**不要 git clone / 同步整个仓库**（仓库无 db，拉了 = 空库）。

更新后核对：

```bash
sudo -u weibo ls -lh /opt/weibo/weibo.db      # 大小不变 = 库没被动
curl -sI http://服务器IP/ | head -1            # 出口 200
curl -s http://127.0.0.1:8766/ | grep -c bloggerStrip   # ≥1 = 新版页面已生效
systemctl is-active weibo                      # active
```

**回滚到上一版**：把上一次的 `weibo-deploy.tar.gz`（或解压后的 `weibo-deploy/`）
重跑一遍上面的 install.sh 即可——代码覆盖回去，库仍不动。

**什么时候才要带库打包**：只有整套迁移（换机器 / 服务器重建）才用带库包，且带的是
**服务器上的库**而不是本地旧库——先 `sudo systemctl stop weibo` 再拷
`/opt/weibo/weibo.db`，做法见文末「备份数据 · 退服务器」。

---

## 第 1 步 本机打包（首次部署；已在开发机做好的话可跳过）

在仓库根目录（git-bash）：

```bash
bash deploy/pack.sh              # 连带本地 weibo.db 一起打包（迁移现有数据，推荐）
# bash deploy/pack.sh --no-db    # 只要代码（服务器全新空库；装时需加 ALLOW_FRESH=1）
```

- 打包前**必须停本地微博服务**（`weibo_stop.vbs`），否则 `weibo.db-wal` 未回收、包内库不完整；
  pack.sh 检测到会拒绝。
- 产出 `weibo-deploy.tar.gz`（含代码 + weibo.db，约 140MB）。

## 第 2 步 上传到服务器

你的云服务器 ssh 账号是 `ubuntu`（非 root，云厂商默认禁 root 直登），上传时**不要传去
`/root`**（非 root 写不进），传到 `/tmp` 即可：

```bash
# 本机执行
scp weibo-deploy.tar.gz ubuntu@服务器IP:/tmp/

# 也可以走厂商对象存储 / 控制台上传，再把文件放到服务器任意可写目录（如 /tmp）
```

## 第 3 步 在服务器安装（一次性）

以 `ubuntu` 账号 ssh 登录，用 `sudo` 执行 install.sh（脚本内部要求 root，本身会自检）：

```bash
ssh ubuntu@服务器IP

# 先确认部署包在位（不在就让用户上传，别去 git/仓库拉代码）
test -f /tmp/weibo-deploy.tar.gz && echo "包在位" || echo "!! 包不在，请先上传"

cd /tmp
tar xzf weibo-deploy.tar.gz        # ubuntu 即可解压（/tmp 可写），无需 sudo
sudo bash weibo-deploy/deploy/install.sh
```

install.sh **会自动新建系统用户 `weibo` 和目录 `/opt/weibo`，无需你手动 useradd**。它幂等、可重复跑
（重跑 = 用 bundle 覆盖代码、不碰已有库）。自动完成：

1. 建专用系统用户 `weibo`，数据目录 `/opt/weibo`（700）
2. 拷入 3 个运行文件；有 `weibo.db` 则导入（**已有库绝不静默覆盖**）
3. 生成 `/etc/weibo.env`：
   - **迁移模式**（带了库）：沿用原库 admin 密码，不生成新密码
   - **空库模式**：随机生成 admin 初始密码，打印 + 存 `/root/weibo-admin-password.txt`
4. 装成 systemd 服务 `weibo` 并启动，健康检查通过为止
5. 安装 nginx 反代（`SKIP_NGINX=1` 可跳过），监听 80 → 127.0.0.1:8766

**为什么进程要跑在专用用户 `weibo` 下，而不是 ubuntu？**——最小权限。这是公网 HTTP 服务，且
`weibo.db` 明文存着各用户微博 cookie / 语雀 token（ADR-0010 的信任边界 = 专用用户 + 数据目录
700/600）。若用 `ubuntu`（有 sudo、近乎 root）跑服务，网页一旦被攻破 = 攻击者能读 `~/.ssh`
私钥 = 整机沦陷；改用 `weibo`（nologin、无 sudo、只能摸 `/opt/weibo`）跑，最坏情况被限制在一个
数据目录里。隔离由 install.sh 免费顺带建立，日常使用也不需要跟该用户打交道（全程
`sudo systemctl` / `sudo -u weibo`）。**不要改成 `User=ubuntu`。**

可调变量：

| 变量 | 作用 |
| --- | --- |
| `AUTH_ADMIN_PASSWORD=xxx` | 空库首启指定 admin 初始密码（不设则随机） |
| `SKIP_NGINX=1` | 跳过 nginx 安装与反代配置 |
| `FORCE_DB=1` | 允许用 bundle 库覆盖 `/opt/weibo/weibo.db` 已有数据 |
| `ALLOW_FRESH=1` | 允许在 bundle 没有 `weibo.db` 时全新空库安装（默认拒绝，防误用仓库纯代码） |

## 部署成功后的回报项（给用户）

install.sh 结尾会打印，也请人工核对：

- `WEIBO_HTTP=http://<服务器公网IP>/` —— 访问入口
- 登录账号：`admin`；密码＝迁移模式用**原本地密码** / 空库模式见脚本输出或
  `/root/weibo-admin-password.txt`
- 防火墙 / 云厂商安全组需放行 **80/tcp**（ssh 保持）
- 日志：`journalctl -u weibo -f`；服务状态：`systemctl status weibo`
- 数据在 `/opt/weibo/weibo.db`，别拿进 git

## 验证

```bash
curl -sI http://127.0.0.1:8766/ | head -1        # 期望 HTTP/1.0 200 OK
curl -sI http://<服务器IP>/ | head -1            # 经 nginx 也是 200
systemctl is-active weibo                        # active
# 数据必须真迁移过来（本地库 ~90MB，太小 = 装成了空库）
sudo -u weibo ls -lh /opt/weibo/weibo.db
```

浏览器打开 `http://<服务器IP>/`，用 admin 登录后：① 个人设置改密码；② 贴微博小号 Cookie
（或迁移模式已带）；③ 找一条博主点增量拉取，看是否正常。

## 万一 AI 从仓库装出了空库，怎么把数据放回去

现象：`/opt/weibo/weibo.db` 只有几 KB~几 MB（全新空库），本应先从包导入你的 ~90MB 库。修正：

```bash
sudo systemctl stop weibo
# 用服务器上 /tmp/weibo-deploy/ 里的 weibo.db 覆盖空库（先确认它确实在）
sudo ls -lh /tmp/weibo-deploy/weibo.db
sudo cp /tmp/weibo-deploy/weibo.db /opt/weibo/weibo.db
sudo chown weibo:weibo /opt/weibo/weibo.db
sudo chmod 600 /opt/weibo/weibo.db
sudo systemctl start weibo
sudo -u weibo ls -lh /opt/weibo/weibo.db        # 应恢复到 ~90MB
```

放回去后 admin 密码 = 你本地原密码；若 `/tmp/weibo-deploy/` 已被删，让用户重新上传包再解压。

## 排障

| 现象 | 处理 |
| --- | --- |
| 80 打不开 | 服务器安全组放行 80；ufw 则 `sudo ufw allow 80/tcp` |
| `curl 127.0.0.1:8766` 不通 | `journalctl -u weibo -n 50` 看报错；端口被占改 `weibo.service` 里 ExecStart 端口 |
| 打开是 nginx 欢迎页 | install.sh 已删 default 站点，手动 `rm /etc/nginx/sites-enabled/default && systemctl reload nginx` |
| 拉到一半 432 暂停 | 正常反爬保护，换小号或等退避；见 CONTEXT.md |
| 想升级代码 | 已部署服务器走上文「更新已有服务器」：`pack.sh --no-db` → 上传 → 重跑 `install.sh`（覆盖代码，复用库） |

## 备份数据 · 退服务器

全部数据 = 单文件 `/opt/weibo/weibo.db`（账号、微博 cookie、语雀 token、博主与博文都在里面）。
备份 = **干净停服后拷走这一个文件**；以后换新服务器，把它当本地库重新打包上传即可原样续用。

```bash
# 服务器上：先干净停服（systemctl stop 会把 WAL 收进主库；别用 kill，否则丢最近写入）
sudo systemctl stop weibo
sudo cp /opt/weibo/weibo.db /tmp/weibo-backup.db
sudo chown ubuntu:ubuntu /tmp/weibo-backup.db
ls -lh /tmp/weibo-backup.db          # 核对 ~90MB
```

```bash
# 本机拉回
scp ubuntu@服务器IP:/tmp/weibo-backup.db ./
```

核对无误后，删掉服务器临时文件再退服务器：

```bash
sudo rm /tmp/weibo-backup.db
```

拉回想接着在本地用：先停本地服务，把该文件改名覆盖本地 `weibo.db`，再 `weibo_start.vbs`。
**提醒**：`weibo.db` 明文存各用户 cookie / 语雀 token，属敏感文件——别进 git、别随手传网盘。

## （可选）在服务器开启语雀归档：claude + Node + 自定义 key

默认部署不含 claude；不装工具照常用——拉取/查询/定时、语雀【删档】（走 OpenAPI）都不依赖它，
只有点【同步】把微博 AI 总结成语雀文档时才需要（会提示「本机没找到 claude」）。数据迁到服务器后，
在服务器网页点【同步】就是在这台服务器上跑，所以要归档就得装。

几条铁律先记住：

- 服务进程以系统用户 `weibo` 跑，它 spawn 的 claude / npx **必须装到系统路径 `/usr/local`**，别装在
  ubuntu 家目录 / nvm 里（weibo 无权访问，会 Permission denied）。
- 国内服务器连不上 Anthropic，用**自定义 key（中转）**，配在 `/etc/weibo.env`，重启 weibo 后由
  子进程继承。**自定义 key 不需要交互式 claude 登录。**
- npx 拉 `yuque-mcp` 看的是 weibo 用户自己的 npm 配置，镜像要设给 weibo。

### 1) 装 Node 到 /usr/local（国内直连 npmmirror 下载，通用）

```bash
cd /tmp
VER=$(curl -fsSL https://registry.npmmirror.com/-/binary/node/latest-v22.x/ | grep -oE 'node-v22\.[0-9]+\.[0-9]+-linux-x64\.tar\.xz' | sort -V | tail -1)
[ -n "$VER" ] || { echo "版本列表拉不到，稍后再试或改用 latest-v20.x"; exit 1; }
curl -fsSL -o "$VER" "https://registry.npmmirror.com/-/binary/node/latest-v22.x/$VER"
sudo rm -rf /usr/local/bin/node /usr/local/bin/npm /usr/local/bin/npx /usr/local/include/node /usr/local/lib/node_modules /usr/local/share/doc/node
sudo tar -xJf "$VER" -C /usr/local --strip-components=1 && rm -f "$VER"
node -v && npm -v
```

> 若 node 是 **nvm** 装的（在 `/home/xxx/.nvm` 下）也一样处理——别去软链它，直接跑上面这条重装一份到
> /usr/local。装完确认 `/usr/local/bin/npm` 是**指向内部的软链**（`ls -l` 显示
> `npm -> ../lib/node_modules/npm/bin/npm-cli.js`），把它 -L 复制成真文件会报 cli.js 找不到。

### 2) 换镜像 + 装 claude 到 /usr/local

```bash
sudo npm config set registry https://registry.npmmirror.com
sudo npm i -g --prefix /usr/local @anthropic-ai/claude-code
/usr/local/bin/claude --version
# 确认 weibo 用户也能搜到（三条都要有输出）
sudo -u weibo which node npm claude
```

### 3) 配自定义 key 并试通（把地址和 key 换成你的中转）

```bash
export ANTHROPIC_BASE_URL=https://你的中转地址
export ANTHROPIC_AUTH_TOKEN=sk-你的key
export ANTHROPIC_MODEL=你的中转支持的模型名     # 试通用到就必配，并原样带进第 4 步
claude -p "回两个字：通了"
```

能回字即通。个别中转认 `x-api-key`（用 `ANTHROPIC_API_KEY=sk-...`）而非 AUTH_TOKEN，报 401 就换变量。

### 4) 写进服务环境并重启（让归档的 claude 子进程继承）

第 3 步试通用到几个变量，这里就原样写几个（**含模型**，你的中转认模型就必须带上）。采用**整体覆盖
写入**，幂等、不会残留脏行；别用 heredoc（粘贴时结尾 `EOF` 容易丢，内容会混进文件）：

```bash
# 把下面几行改成第 3 步试通时用的真实值
printf '%s\n' \
  'WEIBO_NO_BROWSER=1' \
  'ANTHROPIC_BASE_URL=https://你的中转地址' \
  'ANTHROPIC_AUTH_TOKEN=sk-你的key' \
  'ANTHROPIC_MODEL=你的中转支持的模型名' | sudo tee /etc/weibo.env >/dev/null
sudo chmod 600 /etc/weibo.env
sudo systemctl restart weibo
sudo cat /etc/weibo.env    # 核对：应正好这几行、无占位符
```

`WEIBO_NO_BROWSER=1` 必须保留（服务无头运行靠它）。以后要加别的变量（如代理 `HTTPS_PROXY`），把对应行并进上面的 printf 清单再跑一次即可。

### 5) 给 weibo 的 npx 换镜像 + 模拟服务调用验证

```bash
sudo -u weibo npm config set registry https://registry.npmmirror.com
sudo -u weibo bash -c 'ANTHROPIC_BASE_URL=https://你的中转地址 ANTHROPIC_AUTH_TOKEN=sk-你的key claude -p "回两个字：通了"'
```

能回字，网页里点【同步】即可。语雀令牌不用配：迁移的库已按用户带着，用户也能在「个人设置」里重贴
（ADR-0010，只认库中令牌，不入 git）。

## 想从裸 IP 快跑升级到 HTTPS

有域名后：改 `deploy/nginx-weibo.conf` 的 `server_name`，再

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

即可。工具本身无需改动（反代已转发 `X-Forwarded-Proto`）。

## 文件清单

```
deploy/
├── README.md            # 本文档（部署 AI 从这里读）
├── pack.sh              # 本机打包（Windows git-bash）
├── install.sh           # 服务器一键安装（幂等）
├── weibo.service        # systemd 服务单元
└── nginx-weibo.conf     # nginx 反代配置
```
