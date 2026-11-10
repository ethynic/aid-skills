---
name: feishu-doc
description: 读取、搜索、修改飞书（Feishu/Lark）云文档与电子表格，并按约定更新任务状态。只要用户提到飞书、飞书文档、知识库、Lark 文档、"读一下这个文档/链接里的需求"、"总结飞书文档里的开发任务"、"更新任务状态"、"把结果写回飞书"，就使用本技能——即使用户没有明说"飞书"二字，只要是文档链接来自 feishu.cn 或 larksuite.com 域名。基于飞书开放平台自建应用凭据调用官方 OpenAPI，配套零依赖 Python CLI，支持新版文档 docx、知识库 wiki 节点与电子表格 sheet（读取+改单元格）。
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
python "$CLI" search <关键词>                         # 按标题搜文档（需用户登录态）
python "$CLI" sheet-list <表格链接>                    # 列出电子表格的工作表
python "$CLI" sheet-read <表格链接> [--sheet 名称]     # 表格→Markdown（read 会自动识别表格链接）
python "$CLI" sheet-set <表格链接> --cell J7 --value 测试完成  # 修改单元格
python "$CLI" sheet-images <表格链接> --row 8          # 下载该行的用户截图到 ./feishu-images/
```

用户给的是**文档名**而不是链接时，按顺序定位：

1. 直接 `read "<文档名>"` —— pin 过的别名会直接命中（搜索不收录"节点级共享"的 wiki 表格，别名是唯一可靠的按名访问方式）
2. `search <关键词>` —— 按标题搜索（仅覆盖用户空间/共享文档）
3. `list` / `wiki-spaces` + `wiki-list` 逐层翻
4. 都失败 → 请用户贴一次链接，**立刻 `pin <文档名> <链接>` 收藏**，之后按名字直达

常用文档pin一次即可，跨会话生效（`pinned` 查看、`unpin <名字>` 删除）。

## 标准工作流：需求 → 开发 → 回写

用户说"读一下这个飞书文档/总结任务/开发完更新状态"时：

1. **读**：`read` 拿到全文 Markdown。大文档用 `--out` 落盘再分块阅读。
2. **看图**：任务行带「[图片]」标记时，`sheet-images <文档> --row <行号>` 下载截图，然后**逐张打开查看**再下结论——用户的截图里往往画了红框/标注，是需求的直接证据，跳过图片容易理解偏。
3. **总结**：把需求归纳为「目标 / 范围 / 验收标准 / 疑问点」呈现给用户，有歧义先问清，再开工开发。
4. **回写**（开发完成后）：
   - 任务在**电子表格**里（常见列如「修改进度」）：`sheet set <链接> --cell <状态列+行号> --value 测试完成`。行号看 `sheet read` 输出的「行」列，列号看表头字母顺序
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

- 支持**新版文档 docx**、**知识库 wiki** 节点、**电子表格 sheet**（读取 + 改单元格）；旧版 `docs`、多维表格 `base` 不支持（会明确报错）。
- `search` 命令按**标题**搜索且需要用户登录态（`read` 大文档建议 `--out` 落盘）。
- 未 `login` 且走应用身份时，应用必须被添加为文档协作者，否则任何文档都读不到——最高频问题，优先引导用户 `login`。
- `login` 的浏览器授权需要用户在场（本机回环地址回调）；纯无人值守环境用 tenant 身份并提前配好文档授权。
- 表格任务的状态在「修改进度」这类列里：用 `sheet set --cell` 改，不适用 `set-status`；docx 块表格里的状态用 `find` 定位文本块再 `update`。
