# feishu-doc — 飞书文档读写技能

让智能体（ZCode / Claude Code / Codex 等）读取、总结、修改你的飞书文档，并把任务状态回写。基于**飞书开放平台自建应用**，走官方 OpenAPI。

## 两种鉴权方式

| | 方式 A：用户授权（`login`，推荐） | 方式 B：应用身份（tenant token） |
| --- | --- | --- |
| 体验 | 浏览器点一次「同意」，约 7 天内用过一次就自动续期 | 无浏览器交互，配好即用 |
| 文档授权 | **无需逐篇授权**——能访问你自己有权限的所有文档 | 每篇文档/知识库要手动添加应用为协作者 |
| 适用 | 个人日常开发（你的场景） | 无人值守 CI、服务器定时任务 |

两种方式可同时配置，命令默认 `auto`：有登录态走用户身份，否则走应用身份；`--auth tenant/user` 可强制指定。

## 它能做什么

| 能力 | 命令 |
| --- | --- |
| 读取文档为 Markdown（总结需求、提取任务） | `read` |
| 按内容定位文档块 | `find` |
| 修改指定块的文本（更新状态、改正描述） | `update` / `set-status` |
| 在文末追加内容（开发完成记录等） | `append` |
| 浏览云空间文件夹、知识库文档树 | `list` / `wiki-spaces` / `wiki-list` |
| 浏览器授权登录 / 查看登录用户 | `login` / `logout` / `whoami` |

仅支持**新版文档（docx）**与**知识库（wiki）节点**。旧版 docs、电子表格、多维表格暂不支持。

---

## 一次性配置（约 5 分钟，之后全程免手动）

### 1. 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn)（国际版 Lark 用 <https://open.larksuite.com>）→ **开发者后台**
2. **创建企业自建应用**，起名（如「AI 开发助手」），创建后记录 **App ID**（`cli_` 开头）和 **App Secret**

> 说明：飞书**没有**「通过 API 创建应用」的接口，这一步对所有工具（包括 WorkBuddy 等连接器类产品）都是必做的，好在只需一次。

### 2. 开通权限

进入应用的 **权限管理**，开通以下权限（OAuth 授权也基于它们）：

| 权限名称 | 权限标识 | 用途 |
| --- | --- | --- |
| 获取用户的 refresh_token（离线访问） | `offline_access` | `login` 后长期免重登 |
| 查看、评论、编辑和管理版式文档 | `docx:document` | 读 + 改 + 追加文档 |
| 查看、评论和下载云空间中所有文件 | `drive:drive:readonly` | `list` 列出文件夹 |
| 查看知识库 | `wiki:wiki:readonly` | 知识库浏览、wiki 链接解析 |

### 3. 配置重定向 URL（方式 A 必需）

应用 **安全设置 → 重定向 URL** → 添加：

```text
http://127.0.0.1:5178/callback
```

端口与 `login --port` 一致（默认 5178）。

### 4. 配置凭据（三选一）

**方式 A：环境变量（推荐）**

```bash
# macOS / Linux / Git Bash（写进 ~/.bashrc 永久生效）
export FEISHU_APP_ID=cli_xxxxxxx
export FEISHU_APP_SECRET=xxxxxxxx
```

```powershell
# Windows PowerShell（setx 永久生效，重开终端后可用）
setx FEISHU_APP_ID "cli_xxxxxxx"
setx FEISHU_APP_SECRET "xxxxxxxx"
```

**方式 B：配置文件** `~/.config/feishu-doc/config.json`（Windows 是 `C:\Users\<你>\.config\...`）：

```json
{ "app_id": "cli_xxxxxxx", "app_secret": "xxxxxxxx" }
```

**方式 C：命令行参数**（临时用）：`--app-id cli_xxx --app-secret xxx`

### 5. 登录（方式 A）或发布应用（方式 B）

```bash
python scripts/feishu_cli.py login      # 拉起浏览器 → 点「同意」→ 完成
python scripts/feishu_cli.py whoami     # 确认登录身份
```

之后 `read` / `update` / `set-status` 等命令直接可用，访问你在飞书里有权限的任何 docx/wiki 文档，**不再需要逐篇添加应用协作者**。

方式 B（tenant，无人值守场景）还需两步：
1. **版本管理与发布** → 创建版本 → 发布（管理员审核通过后生效）
2. 给目标文档授权：文档右上角「···」→「添加文档应用」（知识库则把应用加为成员）

### 6. 验证

```bash
python scripts/feishu_cli.py setup                  # 报告登录态与应用凭据
python scripts/feishu_cli.py read <你的文档链接>     # 输出 Markdown
```

---

## 安装到智能体

把 `feishu-doc` 整个目录复制（或软链）到对应智能体的技能目录，见仓库根 `README.md` 的安装表。装好后直接对智能体说：

- 「读一下这个飞书文档，总结我要开发的任务：<链接>」
- 「开发完成了，把这篇文档的状态改成已完成，并追加一条完成记录」

## 常见问题

| 现象 | 原因与解决 |
| --- | --- |
| `login` 报 20027 | 授权的 scope 没在应用后台开通 → 权限管理里申请（第 2 步），重新 `login` |
| `login` 报 20010 | 你不在应用可用范围 → 发布时可用范围包含你（或全体成员） |
| `login` 报 20071 | 重定向 URL 与后台配置不一致 → 核对第 3 步（含端口） |
| 登录隔了很久失效 | refresh_token 约 7 天不用会过期 → 重新 `login`（经常使用则永不过期） |
| tenant 模式报无权限 | 应用没发布、或没把应用加为文档协作者/知识库成员 |
| `read` 能读，`update` 报权限 | tenant 模式下协作者权限选了「可阅读」；改成「可编辑」，或改用 `login` 模式 |
| 报 `app_secret` 错误 | 检查是否复制完整；重置 Secret 后要同步更新配置 |

## 安全说明

- 凭据只保存在你的机器（环境变量/配置文件），不会进入对话上下文；CLI 不打印 Secret。
- 登录态（含 refresh_token）保存在 `~/.config/feishu-doc/user_token.json`，仅本机使用；`logout` 可随时清除。
- 智能体对文档的每次修改都是真实 API 调用，建议先在测试文档上试一轮再用于正式任务文档。
