# 8. 语雀归档：无头 Claude CLI + yuque MCP

- 日期：2026-08-25
- 状态：已接受（2026-08-25 经 /grill-with-docs 共识并实现，临时库 e2e 实测通过：真实建文档并落到目标目录）

## 背景

用户要在每张微博卡片博主头像右侧加【同步】按钮，一键把微博「AI 总结 + 同步到语雀文档」。
微博工具（weibo_server.py）是纯标准库本地服务器 + PyInstaller 打包（ADR-0001），
而 AI 总结与语雀 MCP 都活在 Claude Code 生态里，需要一个桥把两者连起来。

## 决策

- **执行引擎 = 无头 claude CLI 子进程**：weibo_server 新端点 spawn `claude -p`
  （提示词内含微博内容与同步模板），AI 总结 + 语雀 MCP 建文档一体完成；结果经
  `--output-format json` 解析出文档 URL。weibo_server 保持纯标准库、零新增 Python 依赖，打包结构不变。
- **无头调用细节**：`claude -p -`（stdin 传提示词，规避 Windows 命令行长度限制）+
  `--allowedTools "mcp__yuque__*"`（无头模式权限层不放行 MCP 工具，必须显式授权）+
  `--output-format json`。提示词要求模型先 `WaitForMcpServers`——yuque MCP 是 npx 冷启动、
  可能比模型慢；`_spawn_claude` 对「MCP 未就绪」类输出自动重试一次（重试时 npx 已缓存变热）。
- **语雀接入 = 官方 yuque-mcp**（`npx -y yuque-mcp` + 个人 token）：已注册在用户全局
  `~/.claude.json` 的 mcpServers，无头 claude 自动加载；token 不写入 weibo 仓库任何文件。
- **文档组织 = 一博主一目录、一微博一文档**：博主行配置「归档目录」链接
  （`https://www.yuque.com/{账号}/{知识库}[/{层级}]/{目录}`），文档建在该知识库、并挂到
  目录节点下（`yuque_update_toc` + `appendChild`，节点是目录还是普通文档都行，语雀允许文档挂文档）；
  目录缺失不拦截归档，找不到目录节点就跳过移动。
- **幂等 = 本地归档状态**：posts 表记录 archived / yuque_doc_url / archived_at；
  已归档微博可再同步——按最新模板用 `yuque_update_doc` 更新已有文档（doc 由库中 URL 定位），
  不新建、不重复；若原文档已被删除则重建。
- **触发 = 仅手动**：单卡按钮 + 批量（不限条数、批内 2 路并发、进度提示、可随时取消）；单卡与批量互不拦截——
  已在同步中的微博自动跳过，其余追加到等待队列，顶部计数累加。转发微博不支持同步。
- **内容 = 模板驱动**：`weibo/yuque-sync-template.md` 定义标题与正文格式，每次同步前读取；
  不放入完整正文（要查原文点原文链接）。

## 后果

- 运行环境要求变高：本机需有可调用的 claude CLI（PATH 或 VSCode 扩展内置二进制），
  且全局配置了 yuque MCP；语雀 MCP 有每日配额（约 50 次/天）。
- 每次同步消耗一次 Claude 调用，约 30-45 秒/条；批量 6 条、批内 2 路并发约 1.5-2.5 分钟。
- 工具自身零新依赖，打包产物结构不变，仅需在 spec 的 datas 里带上同步模板文件。
