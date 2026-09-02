# 多用户账号体系实现规格书（ADR-0010）

- 日期：2026-08-31
- 依据：ADR-0010（决策与取舍）、CONTEXT.md「账号与权限」章节（术语）
- 范围：登录/注册、多用户隔离、凭证按用户、语雀令牌改库、管理后台、数据迁移、部署形态
- 硬约束：**纯 Python 标准库，零新增依赖**；前端仍是单文件 `weibo_web.html`；打包结构不变（`weibo_archive.spec` 无需加文件）

---

## 1. 数据库变更

### 1.1 新表

```sql
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,          -- 3–16 位：中文/英文/数字/下划线
  pass_hash TEXT NOT NULL,                -- PBKDF2-HMAC-SHA256 hex（100k 轮）
  pass_salt TEXT NOT NULL,                -- secrets.token_hex(16)
  role TEXT NOT NULL DEFAULT 'user',      -- 'admin' | 'user'
  disabled INTEGER NOT NULL DEFAULT 0,    -- 管理员强制停用
  deactivated_at INTEGER DEFAULT 0,       -- 注销时间戳；0=未注销
  can_archive INTEGER NOT NULL DEFAULT 0, -- 语雀 AI 归档权限；admin 建号即 1
  created_at TEXT, last_login_at TEXT
);

CREATE TABLE sessions (
  token TEXT PRIMARY KEY,                 -- secrets.token_hex(32)
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL             -- unix ts，30 天滑动
);

CREATE TABLE user_kv (                    -- 按用户设置（原全局 kv 的用户级部分）
  user_id INTEGER NOT NULL, k TEXT NOT NULL, v TEXT,
  PRIMARY KEY (user_id, k)
);
-- 键：cookie / cookie_status / sched_on / sched_minutes / sched_last / yuque_token
```

全局 `kv` 表保留，只放全局项：`invite_code`、`schema_version`。

### 1.2 存量表重建（SQLite 不能改主键，走「建新表→拷数据→改名」）

| 表 | 变更 |
|---|---|
| `bloggers` | 加列 `user_id INTEGER NOT NULL`；主键改 `(user_id, uid)` |
| `posts` | 加列 `user_id INTEGER NOT NULL`；主键改 `(user_id, id)` |
| `pull_seen` | 加列 `user_id`；主键改 `(user_id, uid, id)` |

索引重建：`idx_posts_uid_ts` → `(user_id, uid, created_ts DESC)`；`idx_posts_ts` → `(user_id, created_ts DESC)`。

### 1.3 迁移流程 `migrate_multiuser()`（在 `init_db()` 内、建表前执行）

触发条件：`kv.schema_version` 不存在。回滚保障已有——`main()` 里 `backup_db()`（weibo_server.py:1779）在迁移前自动留备份。步骤：

1. 建 `users` / `sessions` / `user_kv`
2. 插入 admin：密码取环境变量 `AUTH_ADMIN_PASSWORD`；未设置则 `secrets.token_urlsafe(12)` 生成，打印一次（控制台；frozen 无控制台时经现有机制进 `weibo_server.log`，weibo_server.py:1822-1828）。`role='admin'`、`can_archive=1`
3. 全局 `kv` 的 `cookie` / `cookie_status` / `sched_on` / `sched_minutes` / `sched_last` 搬入 `user_kv`（admin），并从 `kv` 删除
4. 调现有 `load_yuque_token()`（weibo_server.py:1488）一次性导入本机 claude 配置里的语雀令牌 → admin 的 `user_kv.yuque_token`；导入后该函数只剩此用途
5. 生成邀请码 `secrets.token_urlsafe(8)` → `kv.invite_code`
6. 按 1.2 重建三张存量表，存量行 `user_id` = admin 的 id
7. `kv.schema_version = '2'`

---

## 2. 认证模块（新代码，全部标准库：`hashlib`/`secrets`/`http.cookies`）

### 2.1 基础函数

- `hash_password(pw, salt)` = `hashlib.pbkdf2_hmac('sha256', pw, salt, 100_000)` hex
- `new_session(user_id)`：token 落库，30 天过期
- `touch_session(token)`：每次鉴权命中续期（滑动）
- `user_from_request(handler)`：解析 `Cookie: wb_session=…` → 查库 → 校验未过期 → 返回 user 行；命中即续期

### 2.2 会话 cookie 属性

`wb_session=<token>; Path=/; HttpOnly; SameSite=Lax`；当请求头 `X-Forwarded-Proto: https`（nginx 转发）时追加 `Secure`。SameSite=Lax 已阻断跨站 POST，不再另做 CSRF token。

### 2.3 登录限流

内存字典 `{username: deque(失败时间戳)}`：15 分钟内失败满 5 次 → 之后 15 分钟拒绝该用户名登录（返回等待提示）。进程重启即清零，可接受。

### 2.4 登录状态机（`POST /api/auth/login`）

| 账号状态 | 响应 |
|---|---|
| 不存在 / 密码错 | 统一 `用户名或密码错误`（防用户名枚举） |
| `disabled=1` | `账号已被停用，请联系管理员` |
| 已注销且未满 7 天 | `{ok:1, deactivated:true, days_left:N}`，不发会话；前端展示注销提示 + 【取消注销】 |
| 已注销且超 7 天 | 视同不存在（清除任务兜底） |
| 正常 | 发会话，更新 `last_login_at` |

### 2.5 注销与清除

- `POST /api/auth/deactivate`（登录态内）：置 `deactivated_at=now`，删除该用户全部会话；进行中和排队中的该用户任务全部清出队列（见 §5.3）
- `POST /api/auth/cancel_deactivate`：用户名+密码再验证 → 清 `deactivated_at` → 发会话
- 清除任务：启动时 + 每 24 小时执行——`deactivated_at < now-7d` 的用户：删除其 `bloggers`/`posts`/`pull_seen`/`user_kv`/`sessions`，再删 user 行（用户名随之释放）；顺带清理过期会话行

---

## 3. API 变更

### 3.1 鉴权接入方式

`do_GET`/`do_POST`（weibo_server.py:1703/1726）开头统一取会话：

- **免鉴权**：`GET /`（SPA 壳）、`/api/auth/login`、`/api/auth/register`
- **其余全部 `/api/*` 与 `/img`**：无有效会话 → `401 {"ok":false,"error":"unauthorized"}`；`/img` 必须设卡，否则公网部署会变成开放代理
- 鉴权通过后，所有 `api_*` 函数签名统一增加首参 `user`（user 行），查询一律带 `user_id = user['id']` 过滤——**这是防越权的核心纪律，逐函数核对，不得遗漏**

### 3.2 新增端点

| 端点 | 说明 |
|---|---|
| `POST /api/auth/login` `…/register` `…/logout` | 注册校验：邀请码 == `kv.invite_code`；用户名正则 `^[\w\u4e00-\u9fa5]{3,16}$`；密码 ≥8 位；成功自动登录。注册时管理员不在场也无妨 |
| `GET /api/auth/me` | 当前用户：username/role/can_archive/是否配置 cookie/令牌（脱敏） |
| `POST /api/me/password` | `{old,new}`，验证旧密码后换哈希 |
| `POST /api/me/yuque_token` | `{token}`，写 `user_kv.yuque_token`；空串 = 清除 |
| `GET /api/template` | 只读返回 `yuque-sync-template.md` 文本（用户可看不可改） |
| `POST /api/auth/deactivate` `…/cancel_deactivate` | 见 2.5 |
| `GET /api/admin/users` | 仅 `role='admin'`：列表（用户名/注册时间/最近登录/状态/注销倒计时/can_archive） |
| `POST /api/admin/user` | `{id, action}`：`disable`/`enable`/`reset_password`（随机密码仅返回一次）/`can_archive`(0/1)/`purge`（注销账号提前清除或直删正常用户，连带数据，双重确认在前端做） |
| `GET /api/admin/invite_code` `POST /api/admin/invite_code/regenerate` | 查看 / 换码 |

管理端点自身也必须是 admin 会话，双重校验（会话有效 + `role='admin'`）。

### 3.3 存量端点的用户化改造清单（逐项核对）

- `/api/cookie`（weibo_server.py:1143）→ 写 `user_kv`；`MSession`（190 行 `kv_get('cookie')`）改为 `MSession(user)`，构造时读该用户 cookie——`MSession` 当前按全局单例用，需改为每任务/每请求按用户构造
- `/api/state` → 只返回该用户的博主/任务/进度 + `can_archive` + cookie 状态
- `/api/posts`（1591）、`/api/blogger/*`、`/api/batch/*`、`/api/refull`、`/api/sync*`、`/api/pause`、`/api/cancel`、`/api/schedule`、`/api/update/cancel` → 全部加 `user_id` 过滤；入参里的 `uid`/`id` 需先校验属于当前用户，不属于视同不存在
- `/api/yuque/*` → 同上，另加归档权限闸门（§5.4）
- `sched_on`/`sched_minutes`/`sched_last` 的所有读写点（含 `sched_cfg()`，708 行）→ 改 `user_kv`

---

## 4. 语雀链路多用户化

### 4.1 归档：`_spawn_claude(prompt, token)`（现 872 行）

签名加 `token`。每次调用生成临时 `--mcp-config` 文件：

```json
{"mcpServers": {"yuque": {
  "command": "npx", "args": ["-y", "yuque-mcp"],
  "env": {"YUQUE_PERSONAL_TOKEN": "<该用户令牌>"}
}}}
```

命令行变为 `[exe, '-p', '-', '--output-format', 'json', '--mcp-config', cfg_path, '--allowedTools', 'mcp__yuque__*']`。临时文件写在系统临时目录、仅当前用户可读（POSIX `os.chmod 0o600`），**无论成功/超时/重试都要删除**（finally）。重试逻辑（MCP 未就绪，910 行）保留。实现后按 ADR-0008 惯例用临时知识库 e2e 实测。

### 4.2 删除：`api_yuque_delete`（1559 行）

`load_yuque_token()` → `user_kv.yuque_token`；1567 行的错误提示改为「请在个人设置中配置语雀令牌」。`load_yuque_token()` 本身只保留给迁移导入用。

### 4.3 归档入口闸门

`/api/yuque/sync`（含批量）入口处：`user.can_archive != 1` → 拒绝；`yuque_token` 为空 → 提示先配置。前端卡片按钮同步按 `can_archive` 显隐（`/api/state` 下发）。

---

## 5. 后台任务改造

### 5.1 队列带用户上下文

- `TASKQ`（450 行）：元素 `(uid, mode)` → `(user_id, uid, mode)`
- `REFRESH_QUEUE`：id 列表 → `(user_id, ids)`
- `SYNC_QUEUE`：同样带 `user_id`
- 3 个 worker 池保持全局共享（2 拉取 + 1 更新 + 1 归档，不变），取出任务后按 `user_id` 构造 `MSession` / 取令牌；该用户无 cookie → 任务以「请先配置微博 cookie」失败落 `note`

### 5.2 `schedule_worker`（731 行）与 `cookie_watcher`（686 行）

- 调度：遍历所有 `sched_on=1` 且非禁用/注销的用户，各自比对 `sched_last` + `sched_minutes`，按自己的一键全部拉取规则入队；错过补偿逻辑按用户各算各的
- cookie 复验：对每个配置了 cookie 的活跃用户各复验一次（每用户每 300 秒 1 次请求，量级可接受），结果写各自 `cookie_status`

### 5.3 用户禁用/注销/删除时的任务清理

新增 `purge_user_tasks(user_id)`：从三个队列中剔除该用户的排队项；正在执行的任务不强杀，但其后续落库对用户已不存在的行是空操作。删除用户时先调此函数再删数据。

---

## 6. 前端改造（`weibo_web.html`，沿用现有显隐切换模式）

1. **登录视图 `#authView`**：登录/注册两栏切换；注册含邀请码输入；「已注销」提示态 + 【取消注销】按钮（调 2.5 流程）。启动时 `GET /api/auth/me`：401 显示本视图；**所有 fetch 统一处理 401 → 回登录视图**
2. **顶栏**：用户名 + 【个人设置】【退出登录】；`role='admin'` 多一个【管理后台】入口
3. **个人设置弹窗**：修改密码；小号 cookie（现 `#banner` 粘贴区 302-307 行的交互保留，保存目标改为当前用户）；语雀令牌（脱敏显示末 4 位 + 编辑/清除）；【查看同步模板】只读弹窗
4. **管理后台弹窗**：用户表格（用户名/注册时间/最近登录/状态含注销倒计时/归档开关）+ 行操作（禁用/启用、重置密码、删除【双重确认弹窗】）；邀请码区（显示/复制/重新生成）
5. **归档按钮**：`can_archive=0` 时卡片不显示【同步】；批量归档入口同理
6. `#banner` 的 cookie 状态提示（红灯等）行为不变，数据源改为当前用户

---

## 7. 部署形态

- **监听不变**：`127.0.0.1:8766`（1831 行保持回环）；公网访问一律经 nginx 反代 + TLS，nginx 加 `proxy_set_header X-Forwarded-Proto $scheme`
- **systemd**：专用用户（如 `weibo`）运行；`Environment=AUTH_ADMIN_PASSWORD=...` 仅首次迁移需要（之后可移除）；数据目录 `chown weibo:weibo; chmod 700`
- **该用户环境**：Node.js、claude CLI（`npm i -g @anthropic-ai/claude-code`）、claude 登录态；**不要**在 `~/.claude.json` 注册任何 yuque MCP（归档走 `--mcp-config` 逐次注入）
- **本地开发与测试**：不受影响，直接 `http://localhost:8766` 登录 admin；无需 nginx
- 已知风险备案：服务器上 claude 登录态是高价值凭证；HTTPS 未就绪前密码明文传输（域名与 HTTPS 同步上线）

---

## 8. 测试计划

1. **迁移**：真实 `weibo.db` 副本上跑迁移——数据条数前后一致、博主/博文/归档状态/断点（`next_page`/`pull_seen`）完整归 admin、全局设置搬家正确、`weibo-backup-*` 备份存在
2. **认证**：注册（邀请码对/错）、登录、30 天会话、5 次锁定、防枚举（错用户名与错密码同文案）、注销→7 天内登录提示→取消注销→数据完好→（改时间戳模拟）7 天后清除、禁用即时踢会话
3. **隔离**：双用户（admin + 注册用户）各加同一博主——各拉各的、互不可见；互访对方的 `uid`/`id` 参数返回不存在；无会话访问任意 `/api/*` 得 401；普通用户调 `/api/admin/*` 得 403/404
4. **语雀**：临时知识库 e2e（真实令牌）——`--mcp-config` 临时文件注入后归档成功、临时文件已删；删除链路走库内令牌成功；`can_archive=0` 的用户被拒；无令牌提示正确
5. **任务**：拉取中禁用用户 → 队列清理；定时拉取按用户各自生效；一个用户 432 不影响另一用户

---

## 9. 不在本次范围

审计日志、按用户自定义同步模板、拉取/归档配额、邮件/找回密码、多机部署、`cryptography` 加密落库（备份需带离服务器时再议）。

---

## 附：实现时顺手核对的两处现状

- weibo_server.py:1842 端口占用分支里 `webbrowser.open(url)` 引用的 `url` 在该分支尚未定义（1844 行才赋值），是潜在 NameError——实现时顺手修正
- `backup_db()`（1779 行）保留最近 3 份启动备份，迁移回滚点天然存在，不另做备份逻辑
