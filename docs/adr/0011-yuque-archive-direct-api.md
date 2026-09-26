# 11. 语雀归档去 claude 化：直调 AI 接口 + 语雀 OpenAPI，AI 配置改全局落库

- 日期：2026-09-26
- 状态：已接受（取代 ADR-0008 的「无头 claude + yuque MCP」链路；与云托管线 ADR-0012 同结论，两条产品线各持一份）

## 背景

原归档链路（ADR-0008）在服务器上 spawn 无头 `claude -p`，靠 yuque MCP 让 AI 自主
建语雀文档。这条路的运行要求太重：本机需有可调用的 claude CLI 和 npx 冷启动的
MCP，agentic 循环非确定性——慢、易被截断、失败原因难归类，打包成 exe 分发的
本地形态更不该绑死 Claude Code 生态。AI 配置此前隐含在 claude 的环境与全局
`~/.claude.json` 里，改配置要动本机文件。

## 决策

### 归档拆成两次确定性 HTTP 调用（全走标准库 urllib）

- **第一步 · AI 总结**：`_ai_complete(prompt, cfg)` 直接 POST 到
  Anthropic 兼容中转的 `/v1/messages`（头：`x-api-key` + `authorization: Bearer` +
  `anthropic-version: 2023-06-01`，`max_tokens=4000`）。prompt 仍由 `yuque-sync-template.md`
  模板驱动，输出契约 `TITLE:...` / `BODY:...`，解析失败自动重试一次。
- **第二步 · 写语雀**：`yuque_create_doc` / `yuque_update_doc`（OpenAPI，复用
  ADR-0009 已有的 `_yuque_api`），归档目录挂载走 `yuque_mount_doc`（GET toc → 匹配
  slug → PUT appendNode，失败只记日志不阻断归档）。已归档再同步 = 更新既有文档；
  文档被远端删了则回落为重建。
- 转发微博不支持归档、正文原样成章、私有可见性、一微博一文档等语义与 ADR-0008 一致。

### AI 中转配置：全局一份、管理员后台填、落 kv 表

- `kv` 三键 `ai_base_url` / `ai_key` / `ai_model`；`ai_cfg()` 三项齐备才返回配置，
  否则 None。不设环境变量兜底（配置落库，后台改一次全实例生效）。
- 管理员界面「AI 归档服务」一行读写：`GET/POST /api/admin/ai_config`（仅管理员）。
  读取只回掩码（`_mask_token`，末 4 位），永不回显 key 原文；保存时 key 留空 = 保持原值，
  `clear_key=1` 才清除。
- 未配齐时用户点【同步】、批量归档入口统一拦：提示「AI 归档功能还没开通，
  请联系管理员在管理后台设置里配置」（零术语，不出现中转/模型等字眼）。

## 备选方案

- **保留 claude + MCP**：本机需装 Claude Code 与 npx，agentic 非确定、失败难归类，
  与「打包即可用」的本地形态冲突，否决。
- **换 OpenAI 兼容 `/chat/completions`**：手上的中转是 Anthropic 兼容口，`/v1/messages`
  直连零改动；将来要接别的网关再在 `_ai_complete` 一处适配即可。
- **AI 配置按用户隔离**：消耗的是同一个服务器侧中转配额，按用户配反而要每人贴 key，
  与「管理员默认放行归档权限」的隔离设计冲突，故全局一份。

## 后果

- 归档不再依赖本机 claude / npx / MCP，纯标准库链路，打包后即可用；
  中转地址/密钥/模型名换成任意 Anthropic 兼容网关也只需后台改配置。
- AI 总结质量与输出契约绑定更紧：`TITLE:/BODY:` 解析失败会重试并明确报错，
  不再被 agent 自主行为掩盖。
- `ai_key` 明文存 `kv` 表，信任边界不变（ADR-0010：专用运行环境 + 界面/日志全程掩码）。
- 语雀 token 按用户存 `user_kv` 不变；存量库首次启动仍从 `~/.claude/settings.json`
  一次性导入令牌（此后只认库）。
- 文档更新：AGENTS.md 归档链路一句、CONTEXT.md「AI 归档服务」术语随之改写，
  ADR-0008 标记被取代。
