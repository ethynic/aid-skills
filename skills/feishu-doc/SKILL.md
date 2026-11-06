---
name: feishu-doc
description: 读取、搜索、修改飞书（Feishu/Lark）云文档，并按约定更新文档中的任务状态。只要用户提到飞书、飞书文档、知识库、Lark 文档、"读一下这个文档/链接里的需求"、"总结飞书文档里的开发任务"、"更新任务状态"、"把结果写回飞书"，就使用本技能——即使用户没有明说"飞书"二字，只要是文档链接来自 feishu.cn 或 larksuite.com 域名。基于飞书开放平台自建应用凭据调用官方 OpenAPI，配套零依赖 Python CLI，支持新版文档 docx 与知识库 wiki 节点。
---

# feishu-doc — 飞书文档读写

## 前置条件（一次性配置）

1. 运行环境需要 Python 3.8+（脚本只用标准库，无需 pip 安装任何依赖）。
2. 用户需要一次性完成：创建飞书「企业自建应用」（无 API 可替代，所有工具都必须做）→ 开通权限 → 配置重定向 URL → 配置凭据。**完整图文步骤见本目录 `README.md`** —— 首次使用、或遇到任何权限类报错时，先读它。
3. 凭据按优先级：命令行 `--app-id/--app-secret` > 环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` > 配置文件 `~/.config/feishu-doc/config.json`。

首次使用先自检，再按提示引导用户：

```bash
python "<本技能目录>/scripts/feishu_cli.py" setup
```

**鉴权优先级（默认 `auto`）**：用户登录态（`login` 浏览器授权，能访问用户自己有权限的所有文档，免逐篇授权）→ 应用身份（tenant token，要求应用被添加为文档协作者）。`--auth user/tenant` 可强制指定。用户遇到「无权限读文档」时，优先引导运行 `login` 而不是去逐篇添加应用协作者。

## 命令速查

以下 `<文档>` 均支持完整 URL（docx/wiki 链接自动识别）或裸 token：

```bash
CLI="<本技能目录>/scripts/feishu_cli.py"

python "$CLI" setup                                   # 检查凭据与登录态
python "$CLI" login                                   # 浏览器授权登录（一次确认，推荐）
python "$CLI" whoami                                  # 查看当前登录用户
python "$CLI" read <文档>                             # 读取为 Markdown
python "$CLI" read <文档> --raw                       # 纯文本（更快，适合大文档摘要）
python "$CLI" read <文档> --json                      # 块 JSON（结构化处理时用）
python "$CLI" read <文档> --out doc.md                # 写入文件（大文档推荐，避免刷屏）
python "$CLI" find <文档> <关键词> [--regex]          # 定位内容块 → 拿到 block_id
python "$CLI" update <文档> <block_id> --text "新文本" # 替换某个块的文本
python "$CLI" append <文档> --text "第一行\n第二行" [--type todo|bullet|h2|code|...]  # 文末追加
python "$CLI" set-status <文档> 已完成 [--field 状态]  # 更新「状态: xxx」行
python "$CLI" list [folder_token] [--type docx]       # 列出云空间文件
python "$CLI" wiki-spaces                             # 列出知识库空间
python "$CLI" wiki-list <space_id> [--parent node]    # 浏览知识库文档树
```

找不到目标文档时：用 `list` / `wiki-spaces` + `wiki-list` 逐层定位，拿到链接后再 `read`。

## 标准工作流：需求 → 开发 → 回写

用户说"读一下这个飞书文档/总结任务/开发完更新状态"时：

1. **读**：`read` 拿到全文 Markdown。大文档用 `--out` 落盘再分块阅读。
2. **总结**：把需求归纳为「目标 / 范围 / 验收标准 / 疑问点」呈现给用户，有歧义先问清，再开工开发。
3. **回写**（开发完成后）：
   - 文档里有「状态: xxx」行 → `set-status <文档> 已完成`
   - 用 `append` 追加一段完成记录，格式与文档既有风格一致，例如：
     `append <文档> --type h2 --text "完成记录"`，再 `append <文档> --text "2026-01-01：已完成开发与自测，改动见 <PR/提交链接>"`

## 编辑规则（重要）

- **修改前必须先看到原文**：用 `read` 或 `find` 确认目标块当前内容，禁止凭记忆盲改。
- 优先用 `set-status` 和精确块的 `update`；不要清空、重写用户写的内容。
- `update` 是整块替换——若原行还有额外信息（负责人、日期等），把新文本拼完整再提交。
- 用户文档没有状态行时，不要擅自造约定，先问用户；用户同意后可以 `append` 一行「状态: xxx」作为约定。
- 出错时把 CLI 输出的错误原文（含 code/msg）如实转告用户，排查思路见 `references/feishu-openapi.md`。

## 已知限制

- 仅支持**新版文档 docx** 与**知识库 wiki** 节点；旧版 `docs`、电子表格 `sheets`、多维表格 `base` 不支持（会明确报错）。
- 全文关键词搜索需要用户身份授权，本技能不做全文搜索；用「文件夹列表 + 知识库树」定位文档。
- 未 `login` 且走应用身份时，应用必须被添加为文档协作者，否则任何文档都读不到——最高频问题，优先引导用户 `login`。
- `login` 的浏览器授权需要用户在场（本机回环地址回调）；纯无人值守环境用 tenant 身份并提前配好文档授权。
- 状态行如果在**表格**里，`set-status` 找不到；用 `find` 定位单元格里的文本块再 `update`。
