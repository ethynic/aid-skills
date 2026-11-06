# aid-skills — 智能体技能仓库

自研 Agent Skills 仓库：每个技能一个独立目录，遵循 [Agent Skills](https://agentskills.io) 开放规范（`SKILL.md` + 可选 `scripts/`、`references/`、`assets/`），目标是 **ZCode / Claude Code / Codex / 其他兼容智能体开箱即用**。

## 目录结构

```text
aid-skills/
├── README.md            # 本文件
└── skills/
    ├── feishu-doc/      # 飞书文档读写（读需求、总结任务、回写状态）
    │   ├── SKILL.md     # 技能入口（智能体加载的部分）
    │   ├── README.md    # 用户配置指南（飞书应用创建/授权）
    │   ├── references/  # 按需阅读的参考文档
    │   └── scripts/     # 可执行脚本（零依赖）
    ├── dev-workflow/    # 三智能体开发流程（纯流程指令，单文件）
    │   └── SKILL.md
    └── <your-skill>/    # 未来的新技能照此结构添加
```

## 技能列表

| 技能 | 用途 | 依赖 |
| --- | --- | --- |
| [feishu-doc](skills/feishu-doc/) | 读取/搜索/修改飞书文档；总结开发任务；更新任务状态 | Python 3.8+（仅标准库）+ 飞书自建应用凭据 |
| [dev-workflow](skills/dev-workflow/) | 三智能体开发流程：开发 → 测试 → CodeReview，worktree 功能分支上阶段即提交，合并主分支前人工把关 | 无（纯流程指令） |

## 安装

### 从 GitHub 安装（推荐）

```bash
git clone https://github.com/ethynic/aid-skills.git
cp -r aid-skills/skills/<技能名> <智能体的技能发现目录>/
```

也可以直接把下面这句话发给任意具备执行能力的智能体，让它自己完成安装（`<技能名>` 换成实际技能）：

> 请 clone <https://github.com/ethynic/aid-skills>，参照该仓库 README 的安装说明，把 `skills/<技能名>` 安装到你所在环境的用户级技能发现目录（常见智能体的目录见下表），安装完成后列出可用技能确认生效。

### 安装位置

每个智能体都从自己的技能发现目录加载 `SKILL.md`。把技能目录复制或软链到对应位置即可：

| 智能体 | 用户级（全局） | 项目级 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `<项目>/.claude/skills/` |
| ZCode | `~/.agents/skills/`（或 `~/.zcode/skills/`） | `<项目>/.agents/skills/`（或 `.zcode/skills/`） |
| Codex CLI | `~/.codex/skills/` | `<项目>/.codex/skills/`（若版本支持） |
| 其他兼容智能体 | 参照其文档的 skills 目录 | 同左 |

```bash
# 复制安装（Git Bash / macOS / Linux）
mkdir -p ~/.agents/skills
cp -r skills/feishu-doc ~/.agents/skills/

# 推荐：软链安装，仓库更新即时生效
# macOS / Linux:
ln -s "$(pwd)/skills/feishu-doc" ~/.agents/skills/feishu-doc
# Windows（开发者模式或管理员 CMD，目录联接）:
# mklink /J "%USERPROFILE%\.agents\skills\feishu-doc" "C:\repos\aid-skills\skills\feishu-doc"
```

同一个技能安装到多个智能体时，凭据是**共享**的（都读 `FEISHU_APP_ID` 等环境变量或 `~/.config/feishu-doc/config.json`），配置一次即可。

## 新增技能约定

1. 一个技能一个目录，放在 `skills/<name>/`，目录名 = SKILL.md 里的 `name`（小写中划线）。
2. `SKILL.md` 保持精简（触发条件写进 description；细节放 `references/`，可执行逻辑放 `scripts/`）。
3. 脚本优先**零依赖**（Python 标准库或 Node 内置模块），保证任意智能体沙箱可运行；确需依赖时在技能 README 里写清安装步骤。
4. 每个脚本提供 `--help` 与可离线运行的 `selftest`，改完先跑自测。
5. 涉及凭据的技能：凭据一律走环境变量/用户配置文件，禁止写进仓库；提供 `setup` 命令验证。
