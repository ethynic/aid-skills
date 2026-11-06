# 飞书 OpenAPI 参考速查

本技能 CLI 实际使用的接口、错误处理与扩展指引。仅在某条命令持续报错、或要扩展新命令时再读。

## 已用接口

| 命令 | 接口 | 所需权限 |
| --- | --- | --- |
| tenant 凭据 | `POST /auth/v3/tenant_access_token/internal` | 无（app_id/secret 换 token，约 2h 有效） |
| login（授权页） | `GET https://accounts.feishu.cn/open-apis/authen/v1/authorize?client_id&response_type=code&redirect_uri&scope&state&prompt=consent` | scope 需已在应用后台开通 |
| login（换 token） | `POST https://accounts.feishu.cn/oauth/v3/token`（v2 兜底 `/authen/v2/oauth/token`），grant_type=authorization_code/refresh_token | 无 |
| whoami | `GET /authen/v1/user_info`（Bearer user_access_token） | 用户已登录 |
| wiki 链接解析 | `GET /wiki/v2/spaces/get_node?token=` | `wiki:wiki:readonly` |
| read（Markdown/JSON） | `GET /docx/v1/documents/{id}`、`GET /docx/v1/documents/{id}/blocks`（分页 500/页） | `docx:document:readonly` |
| read --raw | `GET /docx/v1/documents/{id}/raw_content` | `docx:document:readonly` |
| find / update / set-status | 同 read；写入用 `PATCH /docx/v1/documents/{id}/blocks/{block_id}`（body: `update_text_elements`） | `docx:document` |
| append | `POST /docx/v1/documents/{id}/blocks/{block_id}/children`（body: `children` 数组，可选 `index`） | `docx:document` |
| list | `GET /drive/v1/files?folder_token=` | `drive:drive:readonly` |
| wiki-spaces | `GET /wiki/v2/spaces` | `wiki:wiki:readonly` |
| wiki-list | `GET /wiki/v2/spaces/{space_id}/nodes` | `wiki:wiki:readonly` |

## 双身份鉴权说明

- 默认 `auto`：有用户登录态（`~/.config/feishu-doc/user_token.json`）用 user_access_token，否则用 tenant_access_token；`--auth user|tenant` 强制指定。
- user_access_token 以用户身份生效：只要用户对文档有权限即可读写，无需把应用添加为文档协作者（这是与 tenant 模式的核心差异）。
- refresh_token 是一次性的：每次刷新都会返回新的 refresh_token，CLI 已即时落盘；约 7 天不使用会过期，需重新 `login`。
- 授权 scope 默认：`offline_access docx:document drive:drive:readonly wiki:wiki:readonly`（space 分隔；`offline_access` 才会返回 refresh_token）。
- OAuth 常见错误码：20027 scope 未在应用后台开通；20010 用户不在应用可用范围；20071 redirect_uri 与后台配置不一致；20002 client_secret 错误；20003/20065 授权码无效或已使用（5 分钟有效期、仅一次）。

## 关键 block_type

| 值 | 类型 | 值 | 类型 |
| --- | --- | --- | --- |
| 1 | page（根） | 13 | ordered |
| 2 | text | 14 | code |
| 3–11 | heading1–9 | 15 | quote |
| 12 | bullet | 17 | todo |
| 19 | callout | 31 / 32 | table / table_cell |
| 22 | divider | 34 | quote_container |

只有 `TEXT_KEY_BY_BT` 中的类型能用 `update_text_elements` 整块替换文本；表格单元格是独立的 text 块，可被 `find` + `update` 直接改。

## 错误处理约定

CLI 已自动处理：token 过期（刷新重试一次）、限流 code `99991400` / HTTP 429（退避重试）、GET 网络错误重试。写操作不自动重试，避免重复追加。

其余错误原样输出 `code` + `msg` 给用户，并附排查提示。常见错误码速查：

- `99991663` / `99991672` 等 999916xx：token 无效 → 检查 app_id/secret、应用是否发布
- 权限类（permission denied / `131006` 等）：权限未开通、版本未发布、或应用未加为文档协作者/知识库成员
- `1770001` / `1770002` 类（document not exist）：文档 ID 错误，或应用对该文档无可见性

## 扩展指引（做新命令时参考）

- **创建文档**：`POST /docx/v1/documents`（body: `title`，可选 `folder_token`）。注意：不传 folder 时文档创建在应用空间根目录，用户可能看不到——建议先让用户分享一个文件夹给应用，用它的 folder_token。
- **批量插入**：`PATCH /docx/v1/documents/{id}/blocks/{block_id}/descendant` 可一次插入多块，替代循环 `children`。
- **多维表格/电子表格**：分别走 `/bitable/v1/apps/...` 与 `/sheets/v3/...`，建议做成独立技能而不是塞进本技能。
- **全文搜索**：需要 user_access_token（OAuth），自建应用 tenant token 不支持；维持「文件夹 + 知识库树」定位即可。

## 安全红线

- 任何情况下不把 `app_secret`、`user_access_token`、`refresh_token` 打印到输出、写入文档或写进代码仓库。
- 凭据与登录态都保存在本机（env / `~/.config/feishu-doc/` 下）；更换应用时删除 `tenant_token_cache.json` 与 `user_token.json`。
