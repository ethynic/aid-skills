#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feishu_cli.py — 飞书(Lark)云文档读写命令行工具（Agent Skill 配套）。

零依赖：仅使用 Python 3.8+ 标准库，任何能跑 Python 的智能体环境开箱即用。

凭据优先级：命令行参数(--app-id/--app-secret) > 环境变量
(FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_API_BASE) > 配置文件
~/.config/feishu-doc/config.json（内容: {"app_id": "cli_xxx", "app_secret": "xxx"}）

主要命令：
  setup        验证凭据与登录态
  login        浏览器 OAuth 授权登录（推荐；以用户身份访问文档，免逐篇授权）
  logout       清除本机登录态
  whoami       查看当前登录用户
  read         读取文档（Markdown / 纯文本 / 块 JSON；表格链接自动识别）
  find         按内容查找块，拿到 block_id
  append       在文档末尾（或指定父块下）追加块
  update       替换某个块的文本
  set-status   修改「状态: xxx」约定行（任务状态回写）
  search       按标题关键词搜索文档（需用户登录态）
  list         列出云空间某文件夹下的文件
  wiki-spaces  列出知识库空间
  wiki-list    列出知识库节点（文档树）
  sheet-list   列出电子表格的工作表
  sheet-read   读取电子表格为 Markdown
  sheet-set    修改电子表格单元格
  selftest     本地自测（不访问网络）

支持文档类型：新版文档 docx、知识库 wiki 节点、电子表格 sheet（读取+改单元格）。
不支持：旧版 docs、多维表格 base。
"""

import argparse
import http.server
import json
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

API_BASE_DEFAULT = "https://open.feishu.cn/open-apis"
UA = "feishu-doc-skill/1.0"
CONFIG_DIR = Path.home() / ".config" / "feishu-doc"
CONFIG_FILE = CONFIG_DIR / "config.json"
TOKEN_CACHE = CONFIG_DIR / "tenant_token_cache.json"
ALIASES_FILE = CONFIG_DIR / "aliases.json"
USER_TOKEN_FILE = CONFIG_DIR / "user_token.json"
DEFAULT_LOGIN_PORT = 5178
DEFAULT_SCOPES = "offline_access docx:document drive:drive:readonly wiki:wiki:readonly"

TOKEN_ERR_CODES = {99991661, 99991663, 99991668, 99991672}
RATE_LIMIT_CODE = 99991400

HINTS = [
    "排查提示：",
    "  1) 推荐先运行 `python feishu_cli.py login` 浏览器授权：以你自己的身份访问你有权限的文档，无需逐篇添加应用协作者",
    "  2) 应用权限未开通或版本未发布 → 开发者后台「权限管理」开通后到「版本管理与发布」发布（详见技能目录 README.md）",
    "  3) 应用身份(tenant)访问时：应用必须被添加为该文档的协作者 → 文档右上角「···」→「添加文档应用」",
    "  4) 知识库(wiki)文档 → 应用身份需把应用添加为知识库成员；用户身份只要你自己是成员即可",
    "  5) 文档类型不支持 → 当前仅支持新版文档(docx)与知识库(wiki)节点",
]

# ---------------------------------------------------------------- 常量表

# 可创建的块类型: 名称 -> (block_type, 载荷键名)
BLOCK_TYPES = {
    "text": (2, "text"),
    "h1": (3, "heading1"), "h2": (4, "heading2"), "h3": (5, "heading3"),
    "h4": (6, "heading4"), "h5": (7, "heading5"), "h6": (8, "heading6"),
    "bullet": (12, "bullet"), "ordered": (13, "ordered"), "code": (14, "code"),
    "quote": (15, "quote"), "todo": (17, "todo"),
}

# 可读文本的块类型: block_type -> 载荷键名（也可用于 update）
TEXT_KEY_BY_BT = {
    2: "text",
    3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
    7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9",
    12: "bullet", 13: "ordered", 14: "code", 15: "quote", 17: "todo",
}
HEADING_BT = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 11: 9}

BLOCK_TYPE_NAMES = {1: "page", 19: "callout", 22: "divider", 27: "image",
                    31: "table", 32: "table_cell", 34: "quote_container"}
BLOCK_TYPE_NAMES.update({bt: key for bt, key in TEXT_KEY_BY_BT.items()})

DOC_URL_RE = re.compile(
    r"https?://[^\s]+?/(?P<kind>docx|docs|wiki|base|sheets|file|folder)/(?P<token>[0-9A-Za-z]+)")


class FeishuError(Exception):
    """带用户可读信息的错误；message 会原样展示给用户/智能体。"""


# ---------------------------------------------------------------- 基础设施

def _setup_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass


def load_config():
    if not CONFIG_FILE.exists():
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:  # utf-8-sig 兼容 PowerShell 写入的 BOM
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print("警告：配置文件 %s 解析失败：%s" % (CONFIG_FILE, e), file=sys.stderr)
        return None


def resolve_creds(args):
    app_id = getattr(args, "app_id", None) or os.environ.get("FEISHU_APP_ID")
    app_secret = getattr(args, "app_secret", None) or os.environ.get("FEISHU_APP_SECRET")
    base = getattr(args, "base_url", None) or os.environ.get("FEISHU_API_BASE")
    cfg = load_config()
    if cfg:
        app_id = app_id or cfg.get("app_id")
        app_secret = app_secret or cfg.get("app_secret")
        base = base or cfg.get("base_url")
    if not app_id or not app_secret:
        raise FeishuError(
            "缺少飞书应用凭据。三选一配置：\n"
            "  1) 环境变量: export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx\n"
            "     (Windows PowerShell: $env:FEISHU_APP_ID=\"cli_xxx\"; 或 setx 永久生效)\n"
            "  2) 配置文件: %s\n"
            "     内容: {\"app_id\": \"cli_xxx\", \"app_secret\": \"xxx\"}\n"
            "  3) 命令行: --app-id cli_xxx --app-secret xxx\n"
            "应用创建与授权步骤见技能目录 README.md" % CONFIG_FILE)
    return {"app_id": app_id, "app_secret": app_secret,
            "base": (base or API_BASE_DEFAULT).rstrip("/")}


def raw_http(method, url, body=None, headers=None, timeout=30):
    """底层 HTTP，返回 (status, raw_bytes)。GET 类网络错误自动重试，写操作不重试。"""
    data = None
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    attempts = 4 if method in ("GET", "HEAD") else 1
    last_err = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            except Exception:
                return e.code, b""
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 4))
    raise FeishuError("网络错误: %s [%s %s]" % (last_err, method, url))


def get_token(creds, force=False):
    """获取并缓存 tenant_access_token（有效期约 2h，提前 5 分钟刷新）。"""
    if not force and TOKEN_CACHE.exists():
        try:
            with open(TOKEN_CACHE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if (c.get("app_id") == creds["app_id"] and c.get("base") == creds["base"]
                    and float(c.get("expire_at", 0)) > time.time() + 300):
                return c["token"]
        except Exception:
            pass
    status, raw = raw_http(
        "POST", creds["base"] + "/auth/v3/tenant_access_token/internal",
        body={"app_id": creds["app_id"], "app_secret": creds["app_secret"]})
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        obj = None
    if not isinstance(obj, dict) or obj.get("code") != 0 or not obj.get("tenant_access_token"):
        code = obj.get("code") if isinstance(obj, dict) else "HTTP %s" % status
        msg = obj.get("msg") if isinstance(obj, dict) else "非 JSON 响应"
        raise FeishuError(
            "获取 tenant_access_token 失败: code=%s msg=%s\n"
            "请检查 app_id / app_secret 是否正确、应用是否已创建并发布。" % (code, msg))
    token = obj["tenant_access_token"]
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "app_id": creds["app_id"], "base": creds["base"],
                       "expire_at": time.time() + int(obj.get("expire", 7200)) - 300}, f)
    except Exception:
        pass
    return token


# ---------------------------------------------------------------- 用户授权 (OAuth)

def _accounts_origin(creds):
    m = re.match(r"^(https?://[^/]+)", creds["base"])
    origin = m.group(1) if m else "https://open.feishu.cn"
    return (origin.replace("open.feishu.cn", "accounts.feishu.cn")
                  .replace("open.larksuite.com", "accounts.larksuite.com"))


def build_authorize_url(creds, port, state, scope=None):
    # 授权页在 accounts 域名下，但路径保留 /open-apis 前缀（见官方文档）
    return _accounts_origin(creds) + "/open-apis/authen/v1/authorize?" + urllib.parse.urlencode({
        "client_id": creds["app_id"], "response_type": "code",
        "redirect_uri": "http://127.0.0.1:%d/callback" % port,
        "scope": scope or DEFAULT_SCOPES, "state": state, "prompt": "consent"})


def save_user_token(rec):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(USER_TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(rec, f)


def load_user_token_record():
    if not USER_TOKEN_FILE.exists():
        return None
    try:
        with open(USER_TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _drop_user_token():
    try:
        USER_TOKEN_FILE.unlink()
    except OSError:
        pass


def oauth_token_request(creds, payload):
    """v3 令牌端点优先（官方推荐），v2 兜底；返回解析后的 JSON 对象。"""
    body = {"client_id": creds["app_id"], "client_secret": creds["app_secret"]}
    body.update(payload)
    obj = None
    for url in (_accounts_origin(creds) + "/oauth/v3/token",
                creds["base"] + "/authen/v2/oauth/token"):
        status, raw = raw_http("POST", url, body=body)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            obj = None
        if obj is None or status in (404, 405):
            continue  # 该版本端点不存在，换下一个
        return obj
    return obj or {}


def store_user_tokens(creds, obj):
    now = time.time()
    rec = {
        "app_id": creds["app_id"], "base": creds["base"],
        "access_token": obj.get("access_token"),
        "expire_at": now + int(obj.get("expires_in", 7200)) - 120,
        "scope": obj.get("scope", ""),
        "refresh_token": obj.get("refresh_token"),
        "refresh_expire_at": (now + int(obj["refresh_token_expires_in"]) - 3600)
                             if obj.get("refresh_token_expires_in") else None,
    }
    save_user_token(rec)
    return rec["access_token"]


def get_user_token(creds):
    """返回可用的 user_access_token；未登录返回 None。过期时用 refresh_token 续期。"""
    rec = load_user_token_record()
    if not rec or rec.get("app_id") != creds["app_id"] or not rec.get("access_token"):
        return None
    if rec.get("expire_at", 0) > time.time() + 60:
        return rec["access_token"]
    if not rec.get("refresh_token"):
        return None
    if rec.get("refresh_expire_at") and rec["refresh_expire_at"] < time.time():
        _drop_user_token()
        return None
    obj = oauth_token_request(creds, {"grant_type": "refresh_token",
                                      "refresh_token": rec["refresh_token"]})
    if not isinstance(obj, dict) or obj.get("code") != 0 or not obj.get("access_token"):
        _drop_user_token()
        desc = (obj or {}).get("error_description") or (obj or {}).get("msg") or "未知错误"
        raise FeishuError("user_access_token 刷新失败（%s）。登录已过期，请重新运行 login 授权。"
                          "\n提示：refresh_token 有有效期（约 7 天），期间用过一次即自动续期；"
                          "长期不用会过期。" % desc)
    return store_user_tokens(creds, obj)


def require_user_token(creds):
    token = get_user_token(creds)
    if not token:
        raise FeishuError(
            "尚未登录或登录已失效。请运行: python feishu_cli.py login\n"
            "（会打开浏览器完成一次授权；之后以你的身份访问你有权限的文档，"
            "无需逐篇添加应用协作者）")
    return token


def call_api(method, path, creds, params=None, body=None, auth="auto"):
    """调用飞书 OpenAPI，返回 data 部分。

    auth: auto=优先用户登录态、无则应用身份; user=仅用户身份; tenant=仅应用身份。
    自动处理 token 过期（各身份各重试一次）与限流退避重试。
    """
    last = (None, None, None)
    retried = set()
    for attempt in range(4):
        if auth == "user":
            kind, token = "user", require_user_token(creds)
        elif auth == "tenant":
            kind, token = "tenant", get_token(creds)
        else:
            token = get_user_token(creds)
            kind, token = ("user", token) if token else ("tenant", get_token(creds))
        url = creds["base"] + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, raw = raw_http(method, url, body=body,
                               headers={"Authorization": "Bearer " + token})
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            obj = None
        if not isinstance(obj, dict):
            raise FeishuError("HTTP %s，响应不是 JSON: %s [%s %s]"
                              % (status, raw[:200].decode("utf-8", "replace"), method, path))
        code = obj.get("code", -1)
        msg = obj.get("msg", "")
        if code == 0:
            return obj.get("data", {})
        last = (code, msg, status)
        if code in TOKEN_ERR_CODES and kind not in retried:
            retried.add(kind)
            if kind == "user":
                # 强制走一次 refresh_token 续期后再试
                rec = load_user_token_record()
                if rec:
                    rec["expire_at"] = 0
                    save_user_token(rec)
                if not get_user_token(creds):
                    raise FeishuError("登录态已失效，请重新运行 login 授权。")
            else:
                get_token(creds, force=True)
            continue
        if code == RATE_LIMIT_CODE or status == 429:
            time.sleep(1 + attempt)
            continue
        break
    code, msg, status = last
    extra = (" HTTP=%s" % status) if (status and status != 200) else ""
    raise FeishuError("飞书 API 调用失败: code=%s msg=%s [%s %s%s]\n%s"
                      % (code, msg, method, path, extra, "\n".join(HINTS)))


# ---------------------------------------------------------------- 文档定位

def alias_load():
    try:
        with open(CONFIG_DIR / "aliases.json", "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def alias_save(mapping):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_DIR / "aliases.json", "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def alias_lookup(name):
    if not name:
        return None
    return alias_load().get(name.strip())


def parse_doc_ref(ref, prefer_wiki=False, _depth=0):
    """解析文档链接或裸 token，返回 (类型, token)。支持 pin 过的文档名（别名）。"""
    ref = ref.strip().strip("\"'")
    m = DOC_URL_RE.search(ref)
    if m:
        kind, token = m.group("kind"), m.group("token")
        if kind == "docx":
            return "docx", token
        if kind == "wiki":
            return "wiki", token
        if kind == "docs":
            raise FeishuError("这是旧版文档(docs)链接，旧版接口已下线。"
                              "请使用新版文档(docx)或知识库(wiki)链接。")
        raise FeishuError("暂不支持类型「%s」：当前支持新版文档(docx)与知识库(wiki)。" % kind)
    if re.fullmatch(r"[0-9A-Za-z]{8,64}", ref):
        if prefer_wiki or ref.lower().startswith("wik"):
            return "wiki", ref
        return "docx", ref
    if _depth == 0:  # 不是链接/token → 试文档名别名
        aliased = alias_lookup(ref)
        if aliased:
            return parse_doc_ref(aliased, prefer_wiki, _depth=1)
    raise FeishuError(
        "无法识别的文档链接或 token: %s\n"
        "若是文档名：先让用户贴一次链接，然后 pin \"%s\" <链接> 收藏，之后即可用名字直接访问；"
        "也可先 search <关键词> 搜索。" % (ref, ref))


def _wiki_node(token, creds):
    data = call_api("GET", "/wiki/v2/spaces/get_node", creds, params={"token": token})
    return data.get("node") or {}


def _non_docx_hint(obj_type, title):
    if obj_type == "sheet":
        return ("该链接是电子表格「%s」。可直接 read 读取，或用 sheet read / sheet set "
                "精确操作（sheet list 查看工作表）。" % (title or "未命名"))
    if obj_type == "bitable":
        return "该链接是多维表格（bitable），暂不支持。"
    return "该知识库节点指向 %s 类型（当前支持 docx 文档与电子表格 sheet）: %s" % (obj_type or "?", title)


def _url_worksheet_hint(ref):
    """从 URL 的 sheet= 查询参数中提取工作表提示。"""
    if "sheet=" not in ref:
        return None
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(ref.strip().strip("\"'")).query)
        vals = qs.get("sheet") or []
        return vals[0] if vals else None
    except Exception:
        return None


def resolve_node(ref, creds, prefer_wiki=False):
    """解析为 (类型, 对象token, 工作表提示)。类型: docx / sheet / bitable / ..."""
    s = ref.strip().strip("\"'")
    if "://" not in s and not re.fullmatch(r"[0-9A-Za-z]{8,64}", s):
        aliased = alias_lookup(s)
        if aliased:
            ref = aliased  # 用收藏的完整链接，保留 sheet= 等上下文
    kind, token = parse_doc_ref(ref, prefer_wiki)
    hint = _url_worksheet_hint(ref)
    if kind == "docx":
        return "docx", token, hint
    node = _wiki_node(token, creds)
    obj_type = (node.get("obj_type") or "unknown").lower()
    return obj_type, node.get("obj_token"), hint


def resolve_document(ref, creds, prefer_wiki=False):
    """把链接/token 解析为 docx 的 document_id；其他类型给出明确指引。裸 token 失败时自动换一种解释重试。"""
    kind, token = parse_doc_ref(ref, prefer_wiki)
    bare = "://" not in ref

    def _as_docx(k, t):
        if k == "docx":
            return t
        node = _wiki_node(t, creds)
        ot = (node.get("obj_type") or "").lower()
        if ot != "docx":
            raise FeishuError(_non_docx_hint(ot, node.get("title") or ""))
        return node.get("obj_token")

    try:
        return _as_docx(kind, token)
    except FeishuError as e:
        if not bare:
            raise
        try:
            return _as_docx("docx" if kind == "wiki" else "wiki", token)
        except FeishuError:
            raise e


# ---------------------------------------------------------------- 电子表格 (sheet)

def resolve_spreadsheet(ref, creds):
    """解析链接/token 为电子表格 token，返回 (spreadsheet_token, 工作表提示)。"""
    kind, token, hint = resolve_node(ref, creds)
    if kind == "docx":
        raise FeishuError("该链接是 docx 文档，直接用 read 读取即可。")
    if kind != "sheet":
        raise FeishuError(_non_docx_hint(kind, ""))
    return token, hint


def get_worksheets(ss_token, creds):
    data = call_api("GET", "/sheets/v3/spreadsheets/%s/sheets/query" % ss_token, creds)
    return data.get("sheets") or []


def pick_worksheet(sheets, sel):
    if not sel:
        return sheets[0]
    for s in sheets:
        if sel in (s.get("sheet_id"), s.get("title")) or str(s.get("index")) == sel:
            return s
    raise FeishuError("找不到工作表「%s」。可用工作表: %s"
                      % (sel, "; ".join("%s(id=%s)" % (s.get("title"), s.get("sheet_id"))
                                        for s in sheets)))


def col_letter(n):
    """1-based 列号转字母：1→A, 26→Z, 27→AA, 52→AZ。"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def sheet_values(ss_token, creds, rng):
    data = call_api("GET", "/sheets/v2/spreadsheets/%s/values/%s"
                    % (ss_token, urllib.parse.quote(rng, safe="!:")),
                    creds, params={"valueRenderOption": "ToString",
                                   "dateTimeRenderOption": "FormattedString"})
    d = data.get("valueRange") or data
    return d.get("values") or []


def sheet_values_raw(ss_token, creds, rng):
    """读取原始值（不带渲染选项）：图片单元格返回 embed-image 元素列表。"""
    data = call_api("GET", "/sheets/v2/spreadsheets/%s/values/%s"
                    % (ss_token, urllib.parse.quote(rng, safe="!:")), creds)
    d = data.get("valueRange") or data
    return d.get("values") or []


def _parse_range_anchors(rng):
    """解析 工作表ID!A3:F20，返回 (sheet_id, 起始列号, 起始行, 结束列号, 结束行)。"""
    m = re.fullmatch(r"([0-9A-Za-z]+)!([A-Z]+)(\d+):([A-Z]+)(\d+)", rng)
    if not m:
        return None

    def col_idx(letters):
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - 64)
        return n

    return (m.group(1), col_idx(m.group(2)), int(m.group(3)),
            col_idx(m.group(4)), int(m.group(5)))


_IMG_MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8", ".jpg"), (b"GIF8", ".gif"))


def _sniff_image_ext(data):
    for magic, ext in _IMG_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


_EMBED_IMG_RE = re.compile(r"\[\{'id': \d+, 'type': 'embed-image'\}\]")


def values_to_markdown(values, max_cell=300):
    if not values:
        return "(空表)"
    width = max(len(r) for r in values)
    norm = [list(r) + [""] * (width - len(r)) for r in values]

    def clean(v):
        s = _EMBED_IMG_RE.sub("[图片]", str(v if v is not None else ""))
        return s.replace("\n", "<br>").replace("|", "\\|")[:max_cell]

    lines = ["| 行 | " + " | ".join(clean(c) for c in norm[0]) + " |",
             "| " + " | ".join(["---"] * (width + 1)) + " |"]
    for i, r in enumerate(norm[1:], start=2):
        lines.append("| %d | " % i + " | ".join(clean(c) for c in r) + " |")
    lines.append("")
    lines.append("说明: 「行」列为电子表格实际行号，第 1 行视为表头。修改单元格: "
                 "sheet set <链接> --cell <列+行号，如 C5> --value <新值>。"
                 "「[图片]」为单元格图片，用 sheet-images <链接> --row <行号> 下载后查看。")
    return "\n".join(lines)


def spreadsheet_to_text(ss_token, creds, sel=None, rng=None):
    sheets = get_worksheets(ss_token, creds)
    if not sheets:
        raise FeishuError("该电子表格没有工作表")
    ws = pick_worksheet(sheets, sel)
    gp = ws.get("grid_properties") or {}
    rows = int(gp.get("row_count") or 0)
    cols = int(gp.get("column_count") or 0)
    if not rng:
        rng = "%s!A1:%s%d" % (ws.get("sheet_id"), col_letter(min(cols, 52) or 26),
                              min(rows, 2000) or 200)
    values = sheet_values(ss_token, creds, rng)
    note = ""
    if len(values) > 400:
        values = values[:400]
        note = "\n（仅显示前 400 行，用 --range <工作表ID!A200:Z400> 继续读取）"
    header = "工作表「%s」(sheet_id=%s) 共%s行×%s列，读取范围 %s" % (
        ws.get("title"), ws.get("sheet_id"), rows, cols, rng)
    return header + "\n\n" + values_to_markdown(values) + note


# ---------------------------------------------------------------- 块处理

def get_doc_meta(doc_id, creds):
    data = call_api("GET", "/docx/v1/documents/%s" % doc_id, creds)
    return data.get("document") or {}


def get_blocks(doc_id, creds):
    items = []
    page_token = None
    for _ in range(200):
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = call_api("GET", "/docx/v1/documents/%s/blocks" % doc_id, creds, params=params)
        items.extend(data.get("items") or [])
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    return items


def get_raw_content(doc_id, creds):
    data = call_api("GET", "/docx/v1/documents/%s/raw_content" % doc_id, creds)
    return data.get("content") or ""


def elements_to_text(elements):
    parts = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        if "text_run" in el:
            parts.append((el["text_run"] or {}).get("content", ""))
        elif "mention_user" in el:
            parts.append("@成员")
        elif "mention_doc" in el:
            parts.append("@文档")
        elif "url" in el:
            parts.append((el["url"] or {}).get("url", ""))
        elif "emotion" in el:
            parts.append("[%s]" % ((el["emotion"] or {}).get("emoji_type") or "表情"))
    return "".join(parts)


def block_text(block):
    key = TEXT_KEY_BY_BT.get(block.get("block_type"))
    if not key:
        return ""
    obj = block.get(key)
    if not isinstance(obj, dict):
        return ""
    return elements_to_text(obj.get("elements"))


def flatten_blocks(blocks):
    """按文档顺序展平块，返回 [(block, depth), ...] 与 {block_id: block}。"""
    by_id = {b["block_id"]: b for b in blocks}
    page = next((b for b in blocks if b.get("block_type") == 1), None)
    out = []

    def walk(b, depth):
        out.append((b, depth))
        for cid in b.get("children") or []:
            c = by_id.get(cid)
            if c is not None:
                walk(c, depth + 1)

    if page is not None:
        walk(page, 0)
    seen = {b["block_id"] for b, _ in out}
    for b in blocks:  # 孤儿块兜底
        if b["block_id"] not in seen:
            out.append((b, 0))
    return out, by_id


def _render_table(block, by_id):
    tb = block.get("table") or {}
    cells = tb.get("cells") or []
    prop = tb.get("property") or {}
    cols = prop.get("column_size") or 1

    def cell_text(cell_id):
        cb = by_id.get(cell_id) or {}
        parts = []
        stack = list(cb.get("children") or [])
        while stack:
            x = by_id.get(stack.pop(0))
            if not x:
                continue
            t = block_text(x)
            if t:
                parts.append(t)
            stack = list(x.get("children") or []) + stack
        return " ".join(parts)

    rows = []
    for i in range(0, len(cells), cols):
        row = [cell_text(c).replace("|", "\\|").replace("\n", "<br>").strip()
               for c in cells[i:i + cols]]
        rows.append(row)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    for r in rows:
        r.extend([""] * (width - len(r)))
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return out


def blocks_to_markdown(blocks):
    by_id = {b["block_id"]: b for b in blocks}
    lines = []

    def render_children(parent, indent, quote):
        prev_bt = None
        ord_n = 0
        for cid in parent.get("children") or []:
            b = by_id.get(cid)
            if b is None:
                continue
            bt = b.get("block_type")
            if bt == 13:
                ord_n = ord_n + 1 if prev_bt == 13 else 1
            else:
                ord_n = 0
            render_block(b, indent, quote, ord_n)
            prev_bt = bt

    def render_block(b, indent, quote, ord_n=0):
        bt = b.get("block_type")
        pad = " " * indent
        t = block_text(b)

        def out(s):
            lines.append((quote + pad + s).rstrip())

        if bt == 1:  # page
            pass
        elif bt in HEADING_BT:
            out("#" * HEADING_BT[bt] + " " + t)
        elif bt == 2:
            out(t)
        elif bt == 12:
            out("- " + t)
        elif bt == 13:
            out("%d. %s" % (ord_n, t))
        elif bt == 17:
            done = bool((((b.get("todo") or {}).get("style")) or {}).get("done"))
            out(("- [x] " if done else "- [ ] ") + t)
        elif bt == 14:
            out("```")
            for ln in (t or "").split("\n"):
                lines.append(quote + ln)
            out("```")
        elif bt == 15:
            out("> " + t)
        elif bt in (19, 34):  # callout / quote_container：子块加引用前缀
            pass
        elif bt == 31:
            for ln in _render_table(b, by_id):
                out(ln)
        elif bt == 22:
            out("---")
        elif bt == 27:
            out("*[图片 %s]*" % b.get("block_id"))
        else:
            if t:
                out(t)
            elif not (b.get("children") or []):
                out("*[%s %s]*" % (BLOCK_TYPE_NAMES.get(bt, "block"), b.get("block_id")))

        child_indent = indent + 2 if bt in (12, 13, 17) else indent
        child_quote = quote + "> " if bt in (19, 34) else quote
        render_children(b, child_indent, child_quote)

    page = next((b for b in blocks if b.get("block_type") == 1), None)
    if page is not None:
        render_children(page, 0, "")
    else:
        # 兜底：没有 page 块时渲染所有顶层块（父块不在集合中的块）
        for b in blocks:
            if b.get("parent_id") not in by_id and b.get("block_type") != 32:
                render_block(b, 0, "")
    return "\n".join(lines)


def build_block(type_key, content):
    bt, key = BLOCK_TYPES[type_key]
    return {"block_type": bt, key: {"elements": [{"text_run": {"content": content}}]}}


def update_block_text(creds, doc_id, block, new_text):
    bt = block.get("block_type")
    if bt not in TEXT_KEY_BY_BT:
        raise FeishuError("块类型(block_type=%s %s)不支持文本替换。"
                          "建议用 find 查看相邻文本块定位，或改用 append 追加说明。"
                          % (bt, BLOCK_TYPE_NAMES.get(bt, "?")))
    key = TEXT_KEY_BY_BT[bt]
    payload = {"update_text_elements": {"elements": [{"text_run": {"content": new_text}}]}}
    style = ((block.get(key) or {}).get("style"))
    if isinstance(style, dict) and style:
        payload["update_text_elements"]["style"] = style
    call_api("PATCH", "/docx/v1/documents/%s/blocks/%s" % (doc_id, block["block_id"]),
             creds, body=payload)


# ---------------------------------------------------------------- 命令

def cmd_setup(args, creds):
    rec = load_user_token_record()
    if rec and rec.get("app_id") == creds["app_id"] and rec.get("access_token"):
        print("✓ 用户登录态: 已登录（scope: %s）" % (rec.get("scope") or "?"))
        print("  推荐日常使用此身份；无需逐篇文档添加应用协作者")
    else:
        print("○ 用户登录态: 未登录（可运行 login 完成浏览器授权，推荐）")
    try:
        t0 = time.time()
        get_token(creds, force=True)
        print("✓ 应用凭据: 有效，tenant_access_token 获取成功（%.1fs）" % (time.time() - t0))
    except FeishuError as e:
        print("○ 应用凭据: 不可用（不影响已登录用户身份的使用）\n  %s" % str(e).split("\n")[0])
    print("  app_id : %s" % creds["app_id"])
    print("  api    : %s" % creds["base"])


def cmd_login(args, creds):
    port = args.port
    redirect_uri = args.redirect_uri or ("http://127.0.0.1:%d/callback" % port)
    state = secrets.token_hex(16)
    auth_url = build_authorize_url(creds, port, state, scope=args.scope)
    result = {"code": None, "error": None, "state": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = urllib.parse.parse_qs(u.query)
            for k in result:
                result[k] = (q.get(k) or [None])[0]
            body = ("<meta charset='utf-8'><body style='font-family:sans-serif;"
                    "text-align:center;padding-top:60px'><h2>✓ 授权完成</h2>"
                    "<p>请回到终端继续，本页面可以关闭。</p></body>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        httpd = http.server.HTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise FeishuError(
            "本地端口 %d 监听失败（%s）。请换端口：login --port <端口>，"
            "并同步在开发者后台「安全设置 → 重定向 URL」里加上新地址 "
            "http://127.0.0.1:<端口>/callback" % (port, e))
    print("授权链接（已尝试自动打开浏览器；未打开请手动复制到浏览器）：\n  %s" % auth_url)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass
    print("等待授权回调 127.0.0.1:%d …（约 5 分钟内有效，Ctrl+C 取消）" % port)
    deadline = time.time() + 300
    try:
        while time.time() < deadline:
            httpd.timeout = 2
            httpd.handle_request()
            if result["code"] or result["error"]:
                break
    except KeyboardInterrupt:
        print("\n已取消")
        return 1
    finally:
        httpd.server_close()

    if result["error"]:
        raise FeishuError("你在浏览器中拒绝了授权（%s），未做任何更改。" % result["error"])
    if not result["code"]:
        raise FeishuError("等待授权超时，请重新运行 login。")
    if result["state"] != state:
        raise FeishuError("state 校验不一致，为安全起见已中止，请重新 login。")
    obj = oauth_token_request(creds, {"grant_type": "authorization_code",
                                      "code": result["code"],
                                      "redirect_uri": redirect_uri})
    if not isinstance(obj, dict) or obj.get("code") != 0 or not obj.get("access_token"):
        code = (obj or {}).get("code")
        desc = (obj or {}).get("error_description") or (obj or {}).get("msg") or ""
        raise FeishuError(
            "换取 user_access_token 失败: code=%s %s\n常见原因:\n"
            "  - 20027: 授权 scope 未在应用后台开通（权限管理里申请 offline_access/docx/drive/wiki）\n"
            "  - 20010: 你不在应用可用范围内（发布时可用范围需包含你）\n"
            "  - 20071: 重定向 URL 与后台配置不一致（应为 %s）"
            % (code, desc, redirect_uri))
    store_user_tokens(creds, obj)
    print("✓ 登录成功，登录态已保存到 %s" % USER_TOKEN_FILE)
    print("  scope: %s" % obj.get("scope", "?"))
    if obj.get("refresh_token"):
        print("  后续自动续期：约 7 天内用过一次即一直有效；长期不用需重新 login")
    else:
        print("  注意：未返回 refresh_token，请确认应用已开通 offline_access 权限后重新 login")
    return 0


def cmd_logout(args, creds):
    if USER_TOKEN_FILE.exists():
        _drop_user_token()
        print("✓ 已清除本机登录态")
    else:
        print("本机没有登录态")


def cmd_pin(args, creds):
    link = args.link.strip().strip("\"'")
    kind, token = parse_doc_ref(link)  # 能解析才收
    mapping = alias_load()
    mapping[args.name.strip()] = link
    alias_save(mapping)
    print("✓ 已收藏: %s → [%s] %s" % (args.name.strip(), kind, token))
    print("  之后可直接 read / find / sheet-read \"%s\" 按名访问" % args.name.strip())
    return 0


def cmd_unpin(args, creds):
    mapping = alias_load()
    if mapping.pop(args.name.strip(), None) is None:
        print("没有这个收藏: %s" % args.name)
        return 1
    alias_save(mapping)
    print("✓ 已删除收藏: %s" % args.name.strip())
    return 0


def cmd_pinned(args, creds):
    mapping = alias_load()
    if not mapping:
        print("（还没有收藏。用 pin <文档名> <链接> 添加，之后可用文档名直接访问）")
        return 0
    for k, v in mapping.items():
        print("%s\n      %s" % (k, v))
    return 0


def cmd_whoami(args, creds):
    data = call_api("GET", "/authen/v1/user_info", creds, auth="user")
    rec = load_user_token_record() or {}
    print("name    : %s" % (data.get("name") or "?"))
    print("open_id : %s" % (data.get("open_id") or "?"))
    print("scope   : %s" % (rec.get("scope") or "?"))
    if rec.get("expire_at"):
        print("token   : %.1f 小时后到期（到期自动用 refresh_token 续期）"
              % (max(rec["expire_at"] - time.time(), 0) / 3600.0))
    return 0


def cmd_read(args, creds):
    kind, obj_id, hint = resolve_node(args.doc, creds, prefer_wiki=getattr(args, "wiki", False))
    if kind == "sheet":
        if args.json or args.raw:
            print("提示: 电子表格不支持 --json/--raw，按 Markdown 输出。", file=sys.stderr)
        text = spreadsheet_to_text(obj_id, creds, sel=hint)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print("✓ 已写入 %s（%d 字符）" % (args.out, len(text)))
        else:
            print(text)
        return 0
    if kind != "docx":
        raise FeishuError(_non_docx_hint(kind, ""))
    doc_id = obj_id
    if args.json:
        text = json.dumps({"document_id": doc_id, "blocks": get_blocks(doc_id, creds)},
                          ensure_ascii=False, indent=2)
    elif args.raw:
        text = get_raw_content(doc_id, creds)
    else:
        meta = get_doc_meta(doc_id, creds)
        body = blocks_to_markdown(get_blocks(doc_id, creds))
        title = meta.get("title") or ""
        text = (("# " + title + "\n\n") if title else "") + body
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("✓ 已写入 %s（%d 字符）" % (args.out, len(text)))
    else:
        print(text)
    return 0


def cmd_find(args, creds):
    doc_id = resolve_document(args.doc, creds, prefer_wiki=getattr(args, "wiki", False))
    flat, _ = flatten_blocks(get_blocks(doc_id, creds))
    hits = 0
    for b, _depth in flat:
        t = block_text(b)
        if not t:
            continue
        if args.regex:
            ok = bool(re.search(args.query, t))
        else:
            ok = args.query.lower() in t.lower()
        if ok:
            hits += 1
            print("%s  [%s]  %s" % (b.get("block_id"),
                                    BLOCK_TYPE_NAMES.get(b.get("block_type"), "?"),
                                    t[:300].replace("\n", " ")))
    if hits:
        print("---\n共 %d 处匹配。用 update <文档> <block_id> --text 替换，或 append 追加。" % hits)
    else:
        print("（无匹配）")
    return 0


def cmd_append(args, creds):
    doc_id = resolve_document(args.doc, creds, prefer_wiki=getattr(args, "wiki", False))
    parent = args.parent or doc_id
    raw_lines = (args.text or "").replace("\\n", "\n").split("\n")
    lines = [ln.strip("\r") for ln in raw_lines if ln.strip()]
    if not lines:
        raise FeishuError("没有可追加的文本内容。")
    created = []
    index = args.index
    for ln in lines:
        payload = {"children": [build_block(args.type, ln)]}
        if index is not None:
            payload["index"] = index
            index += 1
        data = call_api("POST", "/docx/v1/documents/%s/blocks/%s/children" % (doc_id, parent),
                        creds, body=payload)
        created.extend(data.get("children") or [])
    print("✓ 已追加 %d 个块:" % len(created))
    for i in created:
        print("  %s" % i)
    return 0


def cmd_update(args, creds):
    doc_id = resolve_document(args.doc, creds, prefer_wiki=getattr(args, "wiki", False))
    target = next((b for b in get_blocks(doc_id, creds)
                   if b.get("block_id") == args.block_id), None)
    if target is None:
        raise FeishuError("未找到块 %s。先用 find <文档> <关键词> 定位块 ID。" % args.block_id)
    old = block_text(target)
    update_block_text(creds, doc_id, target, args.text)
    print("✓ 块已更新 %s" % args.block_id)
    print("  旧: %s" % old[:200])
    print("  新: %s" % args.text[:200])
    return 0


def cmd_set_status(args, creds):
    doc_id = resolve_document(args.doc, creds, prefer_wiki=getattr(args, "wiki", False))
    flat, _ = flatten_blocks(get_blocks(doc_id, creds))
    pat = re.compile(r"^\s*" + re.escape(args.field) + r"\s*[:：]")
    for b, _depth in flat:
        if b.get("block_type") not in TEXT_KEY_BY_BT:
            continue
        t = block_text(b)
        if pat.match(t):
            new_text = "%s: %s" % (args.field, args.value)
            update_block_text(creds, doc_id, b, new_text)
            print("✓ 状态已更新: 「%s」→「%s」" % (t.strip(), new_text))
            print("  块: %s" % b["block_id"])
            return 0
    raise FeishuError(
        "未找到状态行（匹配「%s:」开头的文本块）。\n"
        "方案 A: 让文档作者加一行「%s: 待开发」作为状态约定行；\n"
        "方案 B: 用 find <文档> 状态 定位实际写法，再用 update 替换。" % (args.field, args.field))


def cmd_list(args, creds):
    params = {"page_size": 200, "order_by": "CreatedTime", "direction": "DESC"}
    if args.folder:
        params["folder_token"] = args.folder
    data = call_api("GET", "/drive/v1/files", creds, params=params)
    files = data.get("files") or []
    shown = 0
    for f in files:
        if args.type != "all" and f.get("type") != args.type:
            continue
        shown += 1
        print("[%s] %s" % (f.get("type"), f.get("name")))
        print("      %s" % f.get("url"))
    print("---\n共 %d 个（过滤后 %d 个）。拿到的 docx/wiki 链接可直接 read。" % (len(files), shown))
    return 0


def cmd_wiki_spaces(args, creds):
    items = []
    page_token = None
    for _ in range(50):
        params = {"page_size": 50}
        if page_token:
            params["page_token"] = page_token
        data = call_api("GET", "/wiki/v2/spaces", creds, params=params)
        items.extend(data.get("items") or [])
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    for s in items:
        print("%s  %s" % (s.get("space_id"), s.get("name") or ""))
    print("---\n共 %d 个知识库空间。用 wiki-list <space_id> 浏览文档树。" % len(items))
    return 0


def cmd_wiki_list(args, creds):
    items = []
    page_token = None
    for _ in range(50):
        params = {"page_size": 50}
        if args.parent:
            params["parent_node_token"] = args.parent
        if page_token:
            params["page_token"] = page_token
        data = call_api("GET", "/wiki/v2/spaces/%s/nodes" % args.space, creds, params=params)
        items.extend(data.get("items") or [])
        page_token = data.get("page_token")
        if not data.get("has_more"):
            break
    for n in items:
        print("[%s] %s" % (n.get("obj_type"), n.get("title") or ""))
        print("      node_token=%s  obj_token=%s%s" % (
            n.get("node_token"), n.get("obj_token"),
            "  (有子节点)" if n.get("has_child") else ""))
    print("---\n共 %d 个节点。wiki 链接形如 https://xxx.feishu.cn/wiki/<node_token>，可直接 read。" % len(items))
    return 0


def cmd_search(args, creds):
    """按标题关键词搜索文档（需用户登录态）。"""
    data = call_api("POST", "/suite/docs-api/search/object", creds,
                    body={"search_key": args.query,
                          "page_size": min(max(args.limit, 1), 50), "offset": 0})
    ents = (data.get("docs_entities") or []) if isinstance(data, dict) else []
    if not ents:
        print("（无匹配）")
        return 0
    m = re.match(r"^(https?://[^/]+)", creds["base"])
    host = m.group(1) if m else "https://open.feishu.cn".replace("open.", "")
    path_by_type = {"docx": "/docx/", "doc": "/docs/", "sheet": "/sheets/", "bitable": "/base/"}
    for it in ents:
        t = it.get("docs_type")
        tok = it.get("docs_token")
        path = path_by_type.get(t)
        url = (host + path + tok) if path else "(%s: %s)" % (t, tok)
        print("[%s] %s" % (t, it.get("title")))
        print("      %s" % url)
    print("---\n共 %d 条。docx 链接可直接 read；电子表格用 sheet read / sheet set。" % len(ents))
    return 0


def cmd_sheet_list(args, creds):
    ss, _hint = resolve_spreadsheet(args.doc, creds)
    sheets = get_worksheets(ss, creds)
    for s in sheets:
        gp = s.get("grid_properties") or {}
        print("%s  「%s」  (%s行×%s列)" % (s.get("sheet_id"), s.get("title"),
                                          gp.get("row_count"), gp.get("column_count")))
    print("---\n用 sheet read <链接> --sheet <名称或ID> 读取；read <链接> 默认读第一个工作表。")
    return 0


def cmd_sheet_read(args, creds):
    ss, hint = resolve_spreadsheet(args.doc, creds)
    text = spreadsheet_to_text(ss, creds, sel=args.sheet or hint, rng=args.range)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print("✓ 已写入 %s（%d 字符）" % (args.out, len(text)))
    else:
        print(text)
    return 0


def cmd_sheet_set(args, creds):
    ss, hint = resolve_spreadsheet(args.doc, creds)
    sheets = get_worksheets(ss, creds)
    ws = pick_worksheet(sheets, args.sheet or hint)
    cell = args.cell.strip().upper()
    if not re.fullmatch(r"[A-Z]+[0-9]+", cell):
        raise FeishuError("单元格格式不正确（示例: C5）。--cell 列号+行号，如 C5")
    rng = "%s!%s:%s" % (ws.get("sheet_id"), cell, cell)  # 飞书 v2 端点不接受单锚点范围（如 I8），必须双锚点
    old = sheet_values(ss, creds, rng)
    old_v = old[0][0] if old and old[0] else ""
    call_api("PUT", "/sheets/v2/spreadsheets/%s/values" % ss, creds,
             body={"valueRange": {"range": rng, "values": [[args.value]]}})
    print("✓ 单元格已更新 「%s」!%s" % (ws.get("title"), cell))
    print("  旧: %s" % old_v)
    print("  新: %s" % args.value)
    return 0


def cmd_sheet_images(args, creds):
    """扫描范围内的单元格图片并下载到本地，供智能体查看。"""
    ss, hint = resolve_spreadsheet(args.doc, creds)
    sheets = get_worksheets(ss, creds)
    ws = pick_worksheet(sheets, args.sheet or hint)
    sid = ws.get("sheet_id")
    gp = ws.get("grid_properties") or {}
    cols = int(gp.get("column_count") or 26)
    if args.row:
        rng = "%s!A%d:%s%d" % (sid, args.row, col_letter(min(cols, 52)), args.row)
        start_row, start_col = args.row, 1
    elif args.range:
        rng = args.range
        anchors = _parse_range_anchors(rng.upper())
        if not anchors:
            raise FeishuError("范围格式应为 工作表ID!A1:F20（需带工作表ID前缀，sheet-list 可查）")
        _, start_col, start_row, _, _ = anchors
    else:
        raise FeishuError("请指定 --row <行号>（取该行图片）或 --range <工作表ID!A1:F20>（扫一片）")
    values = sheet_values_raw(ss, creds, rng)
    found = []
    for i, row_vals in enumerate(values):
        for j, cell in enumerate(row_vals):
            # 图片单元格：裸返回时是 dict，多图时可能是 list
            elements = cell if isinstance(cell, list) else [cell]
            for k, el in enumerate(elements):
                if isinstance(el, dict) and el.get("type") == "embed-image" and el.get("fileToken"):
                    found.append((start_row + i, col_letter(start_col + j), k, el["fileToken"]))
    if not found:
        print("该范围没有发现单元格图片")
        return 0
    token = get_user_token(creds) or get_token(creds)
    out_dir = Path(args.out or "feishu-images")
    out_dir.mkdir(parents=True, exist_ok=True)
    saved, failed = [], 0
    for row_no, col_l, k, ft in found[:max(args.limit, 1)]:
        status, data = raw_http("GET", "%s/drive/v1/medias/%s/download" % (creds["base"], ft),
                                headers={"Authorization": "Bearer " + token})
        if status != 200 or not data:
            failed += 1
            print("行%d 列%s  %s  ✗ 下载失败 (HTTP %s)" % (row_no, col_l, ft, status))
            continue
        path = out_dir / ("r%d_%s_%d_%s%s" % (row_no, col_l, k + 1, ft[:8], _sniff_image_ext(data)))
        path.write_bytes(data)
        saved.append(path)
        print("行%d 列%s  %s  ->  %s (%d bytes)" % (row_no, col_l, ft, path, len(data)))
    print("---\n发现 %d 张，已下载 %d 张%s到 %s；用图片查看工具(Read)逐张打开即可看到内容。"
          % (len(found), len(saved), ("，失败 %d 张" % failed) if failed else "", out_dir))
    if len(found) > max(args.limit, 1):
        print("（超出 --limit %d 的 %d 张未下载，可加大 --limit 或缩小范围）" % (args.limit, len(found) - max(args.limit, 1)))
    return 0


# ---------------------------------------------------------------- 自测

def run_selftest():
    failures = []

    def check(name, cond):
        print(("PASS  " if cond else "FAIL  ") + name)
        if not cond:
            failures.append(name)

    check("url docx", parse_doc_ref("https://aa.feishu.cn/docx/doxcnABC123?from=x") == ("docx", "doxcnABC123"))
    check("url wiki", parse_doc_ref("https://aa.feishu.cn/wiki/wikcnXYZ789") == ("wiki", "wikcnXYZ789"))
    try:
        parse_doc_ref("https://aa.feishu.cn/docs/old123")
        check("url docs 拒绝", False)
    except FeishuError:
        check("url docs 拒绝", True)
    check("bare docx", parse_doc_ref("doxcnABCDEFGHIJ") == ("docx", "doxcnABCDEFGHIJ"))
    check("bare wiki", parse_doc_ref("wikcnABCDEFGHIJ") == ("wiki", "wikcnABCDEFGHIJ"))

    blocks = [
        {"block_id": "p", "block_type": 1, "parent_id": "", "children": ["h", "t", "b1", "b2", "tb"]},
        {"block_id": "h", "block_type": 4, "parent_id": "p",
         "heading2": {"elements": [{"text_run": {"content": "任务目标"}}]}},
        {"block_id": "t", "block_type": 2, "parent_id": "p",
         "text": {"elements": [{"text_run": {"content": "完成登录功能"}}]}},
        {"block_id": "b1", "block_type": 12, "parent_id": "p",
         "bullet": {"elements": [{"text_run": {"content": "支持手机号"}}]}},
        {"block_id": "b2", "block_type": 17, "parent_id": "p",
         "todo": {"elements": [{"text_run": {"content": "联调"}}], "style": {"done": True}}},
        {"block_id": "tb", "block_type": 31, "parent_id": "p", "children": ["tc1", "tc2"],
         "table": {"cells": ["tc1", "tc2"], "property": {"row_size": 1, "column_size": 2}}},
        {"block_id": "tc1", "block_type": 32, "parent_id": "tb", "children": ["cc1"]},
        {"block_id": "tc2", "block_type": 32, "parent_id": "tb", "children": ["cc2"]},
        {"block_id": "cc1", "block_type": 2, "parent_id": "tc1",
         "text": {"elements": [{"text_run": {"content": "字段"}}]}},
        {"block_id": "cc2", "block_type": 2, "parent_id": "tc2",
         "text": {"elements": [{"text_run": {"content": "说明"}}]}},
    ]
    md = blocks_to_markdown(blocks)
    for frag in ["## 任务目标", "完成登录功能", "- 支持手机号", "- [x] 联调",
                 "| 字段 | 说明 |", "| --- | --- |"]:
        check("render: %s" % frag, frag in md)

    u = build_authorize_url({"app_id": "cli_x", "base": API_BASE_DEFAULT}, 5178, "st123")
    check("authorize url 域名", u.startswith("https://accounts.feishu.cn/open-apis/authen/v1/authorize"))
    check("authorize url 参数", ("client_id=cli_x" in u) and ("offline_access" in u)
          and ("127.0.0.1%3A5178" in u))
    check("col_letter", col_letter(1) == "A" and col_letter(26) == "Z"
          and col_letter(27) == "AA" and col_letter(52) == "AZ")
    smd = values_to_markdown([["任务", "状态"], ["登录", "待开发"], ["支付", "已完成"]])
    for frag in ["| 行 | 任务 | 状态 |", "| 2 | 登录 | 待开发 |", "| 3 | 支付 | 已完成 |"]:
        check("sheet render: %s" % frag, frag in smd)
    imd = values_to_markdown([["备注"], ["[{'id': 1, 'type': 'embed-image'}]"]])
    check("sheet render 图片占位符", "[图片]" in imd and "embed-image" not in imd)
    check("图片类型嗅探", _sniff_image_ext(b"\x89PNG\r\n") == ".png"
          and _sniff_image_ext(b"\xff\xd8\xff") == ".jpg")

    global CONFIG_DIR
    _orig_dir = CONFIG_DIR
    try:  # 别名机制（隔离到临时目录，不碰真实收藏）
        CONFIG_DIR = Path(tempfile.mkdtemp(prefix="feishu_alias_test_"))
        alias_save({"测试文档": "https://aa.feishu.cn/wiki/wikcnTEST1"})
        check("alias 存取", alias_lookup("测试文档") == "https://aa.feishu.cn/wiki/wikcnTEST1")
        check("alias 解析", parse_doc_ref("测试文档") == ("wiki", "wikcnTEST1"))
        check("alias 未命中", alias_lookup("不存在的文档") is None)
    finally:
        CONFIG_DIR = _orig_dir

    if failures:
        print("\n%d 项自测失败" % len(failures))
        return 1
    print("\n全部自测通过")
    return 0


# ---------------------------------------------------------------- CLI

EXAMPLES = """示例:
  python feishu_cli.py setup                 # 检查凭据与登录态
  python feishu_cli.py login                 # 浏览器授权登录（推荐，一次性确认）
  python feishu_cli.py whoami                # 查看当前登录用户
  python feishu_cli.py read https://xxx.feishu.cn/docx/doxcnXXX
  python feishu_cli.py read https://xxx.feishu.cn/wiki/wikcnXXX --raw
  python feishu_cli.py find <文档链接> 状态
  python feishu_cli.py update <文档链接> <block_id> --text "状态: 已完成"
  python feishu_cli.py append <文档链接> --text "## 完成记录\\n2026-01-01 已完成开发与自测"
  python feishu_cli.py set-status <文档链接> 已完成
  python feishu_cli.py list
  python feishu_cli.py wiki-spaces
  python feishu_cli.py wiki-list <space_id>
  python feishu_cli.py search 修改清单                       # 按标题搜文档（需登录态）
  python feishu_cli.py sheet-read <表格链接> [--sheet 工作表名]
  python feishu_cli.py sheet-set <表格链接> --cell C5 --value 已完成
  python feishu_cli.py sheet-images <表格链接> --row 8          # 下载该行用户上传的截图
"""


def build_parser():
    # 全局凭据参数挂在顶层与每个子命令上，前后位置均可；SUPPRESS 避免子命令默认值覆盖顶层取值
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--app-id", default=argparse.SUPPRESS,
                        help="飞书应用 App ID（默认读 FEISHU_APP_ID 或配置文件）")
    common.add_argument("--app-secret", default=argparse.SUPPRESS,
                        help="飞书应用 App Secret（默认读 FEISHU_APP_SECRET 或配置文件）")
    common.add_argument("--base-url", default=argparse.SUPPRESS,
                        help="API 基地址（Lark 国际版用 https://open.larksuite.com/open-apis）")
    common.add_argument("--wiki", action="store_true", default=argparse.SUPPRESS,
                        help="裸 token 按 wiki 节点解析（URL 会自动识别，无需此参数）")
    common.add_argument("--auth", choices=["auto", "user", "tenant"], default="auto",
                        help="鉴权方式：auto=优先用户登录态，否则应用身份（默认）")

    p = argparse.ArgumentParser(
        prog="feishu_cli.py",
        description="飞书云文档读写 CLI（Agent Skill 配套，零依赖，支持 docx 与 wiki）",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common])
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("setup", parents=[common], help="验证凭据与登录态")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("login", parents=[common], help="浏览器授权登录（推荐，免逐篇文档授权）")
    sp.add_argument("--port", type=int, default=DEFAULT_LOGIN_PORT,
                    help="本地回调端口（默认 %d，需与应用后台重定向 URL 一致）" % DEFAULT_LOGIN_PORT)
    sp.add_argument("--redirect-uri",
                    help="覆盖回调地址（默认 http://127.0.0.1:<port>/callback；"
                         "若后台只允许 localhost 形式，用 --redirect-uri http://localhost:<port>/callback 保持两端一致）")
    sp.add_argument("--scope", help="覆盖授权 scope（默认: %s）" % DEFAULT_SCOPES)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("logout", help="清除本机登录态")
    sp.set_defaults(func=cmd_logout)

    sp = sub.add_parser("whoami", parents=[common], help="查看当前登录用户")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("read", parents=[common], help="读取文档为 Markdown")
    sp.add_argument("doc", help="文档链接或 token")
    sp.add_argument("--raw", action="store_true", help="取纯文本（更快，无结构）")
    sp.add_argument("--json", action="store_true", help="输出块 JSON（供程序处理）")
    sp.add_argument("--out", help="写入文件而不是标准输出")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("find", parents=[common], help="按内容查找块（拿 block_id）")
    sp.add_argument("doc", help="文档链接或 token")
    sp.add_argument("query", help="关键词（或 --regex 正则）")
    sp.add_argument("--regex", action="store_true", help="按正则匹配")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("append", parents=[common], help="在文末（或父块下）追加块")
    sp.add_argument("doc", help="文档链接或 token")
    sp.add_argument("--text", required=True, help="追加内容；多行用字面 \\n 分隔")
    sp.add_argument("--type", default="text", choices=sorted(BLOCK_TYPES.keys()),
                    help="块类型（默认 text）")
    sp.add_argument("--parent", help="父块 ID（默认文档根，即追加到文末）")
    sp.add_argument("--index", type=int, help="插入位置下标（默认末尾）")
    sp.set_defaults(func=cmd_append)

    sp = sub.add_parser("update", parents=[common], help="替换某个块的文本")
    sp.add_argument("doc", help="文档链接或 token")
    sp.add_argument("block_id", help="目标块 ID（find 获得）")
    sp.add_argument("--text", required=True, help="替换后的完整文本")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("set-status", parents=[common], help="更新「状态: xxx」约定行")
    sp.add_argument("doc", help="文档链接或 token")
    sp.add_argument("value", help="新的状态值，如：已完成")
    sp.add_argument("--field", default="状态", help="状态字段名（默认「状态」）")
    sp.set_defaults(func=cmd_set_status)

    sp = sub.add_parser("list", parents=[common], help="列出云空间文件")
    sp.add_argument("folder", nargs="?", help="文件夹 token（默认根目录）")
    sp.add_argument("--type", default="all",
                    choices=["all", "docx", "doc", "sheet", "bitable", "file", "folder"],
                    help="按类型过滤")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("wiki-spaces", parents=[common], help="列出知识库空间")
    sp.set_defaults(func=cmd_wiki_spaces)

    sp = sub.add_parser("wiki-list", parents=[common], help="列出知识库节点")
    sp.add_argument("space", help="知识库 space_id（wiki-spaces 获得）")
    sp.add_argument("--parent", help="父节点 node_token（默认顶层）")
    sp.set_defaults(func=cmd_wiki_list)

    sp = sub.add_parser("search", parents=[common], help="按标题关键词搜索文档（需用户登录态）")
    sp.add_argument("query", help="标题关键词")
    sp.add_argument("--limit", type=int, default=20, help="返回条数（默认 20，最多 50）")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("pin", help="收藏文档别名（之后可用文档名直接访问）")
    sp.add_argument("name", help="文档名/别名")
    sp.add_argument("link", help="文档链接或 token")
    sp.set_defaults(func=cmd_pin)

    sp = sub.add_parser("unpin", help="删除文档别名收藏")
    sp.add_argument("name", help="文档名/别名")
    sp.set_defaults(func=cmd_unpin)

    sp = sub.add_parser("pinned", help="列出全部文档别名收藏")
    sp.set_defaults(func=cmd_pinned)

    sp = sub.add_parser("sheet-list", parents=[common], help="列出电子表格的工作表")
    sp.add_argument("doc", help="表格链接或 token")
    sp.set_defaults(func=cmd_sheet_list)

    sp = sub.add_parser("sheet-read", parents=[common], help="读取电子表格为 Markdown")
    sp.add_argument("doc", help="表格链接或 token")
    sp.add_argument("--sheet", help="工作表名称或 sheet_id（默认 URL 指定的或第一个）")
    sp.add_argument("--range", help="范围（如 aTxhcc!A1:H50，默认有效区域）")
    sp.add_argument("--out", help="写入文件而不是标准输出")
    sp.set_defaults(func=cmd_sheet_read)

    sp = sub.add_parser("sheet-set", parents=[common], help="修改电子表格单元格")
    sp.add_argument("doc", help="表格链接或 token")
    sp.add_argument("--cell", required=True, help="单元格（列+行号，如 C5）")
    sp.add_argument("--value", required=True, help="新值")
    sp.add_argument("--sheet", help="工作表名称或 sheet_id（默认 URL 指定的或第一个）")
    sp.set_defaults(func=cmd_sheet_set)

    sp = sub.add_parser("sheet-images", parents=[common], help="下载单元格图片到本地（供查看）")
    sp.add_argument("doc", help="表格链接、token 或已 pin 的文档名")
    sp.add_argument("--row", type=int, help="只取该行的图片")
    sp.add_argument("--range", help="扫描范围（工作表ID!A1:F20）")
    sp.add_argument("--sheet", help="工作表名称或 sheet_id")
    sp.add_argument("--out", default="feishu-images", help="保存目录（默认 ./feishu-images）")
    sp.add_argument("--limit", type=int, default=20, help="最多下载张数（默认 20）")
    sp.set_defaults(func=cmd_sheet_images)

    sp = sub.add_parser("selftest", help="本地自测（不访问网络）")
    sp.set_defaults(func=lambda a, c: run_selftest())

    return p


def main(argv=None):
    _setup_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "selftest":
            return run_selftest()
        if args.command in ("logout", "pin", "unpin", "pinned"):
            return args.func(args, None)
        creds = resolve_creds(args)
        return args.func(args, creds)
    except FeishuError as e:
        print(str(e), file=sys.stderr)
        return 1
    except BrokenPipeError:  # 输出被 head 等管道截断时安静退出
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
