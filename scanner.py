# -*- coding: utf-8 -*-
"""Word 超链接关键字扫描器 —— 核心逻辑（不依赖 GUI）

流程：解析 docx 内全部超链接 -> 并发抓取网页正文（可按「关注前几页」跟进网页
自身的分页）-> 每一页按「任一关键字命中」独立筛选 -> 导出。
docx 解析使用标准库 zipfile + ElementTree，覆盖正文 / 页眉页脚 / 脚注 / 尾注。
"""
from __future__ import annotations

import csv
import queue
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# 每个工作线程复用一个 requests.Session：同一站点多页 / 多详情可复用 TCP+TLS
# 连接，避免每页重新握手。Session 非线程安全，故用 thread-local 隔离。
_http_local = threading.local()


def _http_session():
    sess = getattr(_http_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        _http_local.session = sess
    return sess


# 浏览器 WAF 挑战成功后拿到的 cookie 共享池：渲染线程写入，HTTP 线程读取注入。
# 政务站多为「一次挑战设 cookie，之后同域 HTTP 直连」，可省掉每页浏览器渲染。
# key 为可匹配的域后缀（如 cdjinjiang.gov.cn），value 为 {name: value}。
_shared_cookies: dict[str, dict[str, str]] = {}
_cookie_lock = threading.Lock()


def _remember_cookies(cookies) -> None:
    """把 Playwright context.cookies() 的结果存入共享池（供 HTTP 请求复用）。"""
    if not cookies:
        return
    with _cookie_lock:
        for c in cookies:
            try:
                domain = str(c.get("domain") or "").lstrip(".").lower()
                name, value = str(c["name"]), str(c["value"])
                if not domain or not name:
                    continue
                _shared_cookies.setdefault(domain, {})[name] = value
            except Exception:
                continue


def _apply_cookies(host: str) -> dict[str, str]:
    """取出适用于 host 的 cookie（域后缀匹配），供本次 HTTP 请求注入。"""
    host = (host or "").lower().split(":")[0]
    out: dict[str, str] = {}
    if not host:
        return out
    with _cookie_lock:
        for domain, jar in _shared_cookies.items():
            if host == domain or host.endswith("." + domain):
                out.update(jar)
    return out

# 新增列一律追加在末尾，保持前 11 列位置 / 语义与旧版完全一致
FIELDS = ["序号", "状态", "命中关键字", "链接文字", "网页标题", "链接URL",
          "跳转后URL", "备注", "页码", "起始链接", "抓取方式", "来源层级"]
STATUS_RANK = {"命中": 0, "未命中": 1, "失败": 2}
MAX_PAGE_BYTES = 8 * 1024 * 1024
MIN_PAGES = 1
MAX_PAGES = 20

# 渲染模式：http 仅 requests（默认）；auto 遇 WAF 自动改用浏览器重试；browser 全量渲染
RENDER_MODES = ("auto", "http", "browser")
BROWSER_CHANNELS = ("auto", "msedge", "chrome", "chromium")
_CHANNEL_ORDER = ("msedge", "chrome", "chromium")
_CHANNEL_LABEL = {"msedge": "Edge 渲染", "chrome": "Chrome 渲染",
                  "chromium": "Chromium 渲染"}
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_WAF_STATUS = frozenset({403, 412, 429, 503})
_MIN_CHALLENGE_TEXT = 180
_RENDER_BUDGET = 15.0
_RENDER_POLL_MS = 300

# 「下一页」可见文本特征；仅接受短文本，避免把导航句子 / 广告词误判成分页
_NEXT_EXACT = frozenset({
    "next", "nextpage", "next>", "next›", ">", ">>", "›", "››", "»", "»»", "→",
    "下一页", "下页", "下一頁", "后一页", "後一頁", "下一张", "下一張", "往后", "往後",
})
_NEXT_WORD_RE = re.compile(
    r"(下一页|下页|下一頁|后一页|後一頁|下一张|下一張|\bnext\b)", re.I)
# 分页容器 class / id 特征
_PAGINATION_RE = re.compile(
    r"(pagination|pager|page[-_]?(nav|num|number|list|link|index|bar|box|div|wrap|area)"
    r"|\bpages?\b)", re.I)
# 明显的下载 / 非 HTML 资源
_BAD_HREF_EXT = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|gz|exe|msi|dmg|apk|mp3|mp4|avi|"
    r"jpe?g|png|gif|bmp|csv|xml|rss|json|txt)(?:$|[?#])", re.I)
# 登录 / 注册 / 下载 / 脚本类链接
_BAD_HREF_WORD = re.compile(
    r"(javascript:|mailto:|tel:|data:|/login|/signin|/sign-in|/signup|/register|"
    r"/logout|/download|/dl/|\bdownload\b)", re.I)
_PAGE_QUERY_KEYS = ("page", "pn", "p", "offset")
_PURIFY_TAGS = ("script", "style", "noscript", "svg", "iframe", "template")

# 每页最多跟进详情页条数的收敛范围
MIN_DETAILS = 0
MAX_DETAILS = 50
DEFAULT_MAX_DETAILS = 10
# 详情页路径特征：优先认定为新闻 / 文章详情
_DETAIL_PATH_RE = re.compile(
    r"(content_|content/|/info|article|art/|detail|show)", re.I)
# 详情页链接的可见文本为这些短导航词时排除
_NAV_TEXT = frozenset({
    "首页", "网站首页", "返回首页", "主页", "政务公开", "互动交流", "网站地图",
    "联系我们", "在线咨询", "办事指南", "更多", "更多>>", "more", "home",
    "首页>>", "next", "上一页", "下一页", "下页", "尾页", "末页",
})
_LEVEL_RANK = {"列表页": 0, "详情页": 1}
# JS 分页点击：分页容器内可接受的「下一页」文本与数字页码
_JS_NEXT_HREF_OK = ("javascript:", "#", "javascript:;")


@dataclass
class Link:
    url: str
    texts: list[str] = field(default_factory=list)
    order: int = 0


@dataclass
class PageData:
    """一个起始链接跟进到的单页抓取结果。

    迭代时按约定的 5 元组 (page_no, url, text, title, error) 输出，便于按位置
    解包复用；final_url / note 请用属性访问。error 非空表示该页抓取失败。
    fetch_via / render_note 为浏览器渲染新增的可选字段，默认保持旧版语义。
    html 为该页原始 HTML，供 scan 从列表页发现详情页链接。
    """

    page_no: int
    url: str
    text: str = ""
    title: str = ""
    error: str = ""
    final_url: str = ""
    note: str = ""
    fetch_via: str = "HTTP"
    render_note: str = ""
    html: str = ""

    def __iter__(self):
        return iter((self.page_no, self.url, self.text, self.title, self.error))


def html_unescape(s: str) -> str:
    from html import unescape
    return unescape(s)


def _is_web(url: str) -> bool:
    try:
        return urlsplit(url).scheme.lower() in ("http", "https")
    except Exception:
        return False


def _norm_key(url: str) -> str:
    """去 fragment 与末尾斜杠，用于去重，保留 query"""
    try:
        p = urlsplit(url)
        base = f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/')}"
        return f"{base}?{p.query}" if p.query else base
    except Exception:
        return url.strip().rstrip("/")


def _same_origin(a: str, b: str) -> bool:
    """同 scheme + netloc（含端口）才算同源，用于分页防跑飞。"""
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        return (pa.scheme.lower() == pb.scheme.lower()
                and pa.netloc.lower() == pb.netloc.lower())
    except Exception:
        return False


def _same_host(a: str, b: str) -> bool:
    """只比较 netloc（含端口），忽略 scheme —— 同主机 http↔https 视为同站。

    仅用于从列表页筛选详情页链接：政务站常见于列表页 https、详情页 http，
    用严格 _same_origin 会把这些详情链接全部漏掉。分页防跑飞仍用 _same_origin。
    """
    try:
        pa, pb = urlsplit(a), urlsplit(b)
        return bool(pa.netloc) and pa.netloc.lower() == pb.netloc.lower()
    except Exception:
        return False


def _synth_page_url(url: str, page_no: int) -> str:
    """AJAX 换页（URL 不变）时合成带 query 的展示 URL，供去重与展示。

    _norm_key 会去掉 fragment，故必须用 query 参数 __wls_page=N 区分不同页；
    保留原 query 的其它参数，仅替换 __wls_page。
    """
    try:
        p = urlsplit(url)
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k != "__wls_page"]
        pairs.append(("__wls_page", str(page_no)))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), p.fragment))
    except Exception:
        return url


def _clamp_pages(value) -> int:
    """把「关注前几页」限制在 1..20，非法输入回退为 1。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = MIN_PAGES
    return max(MIN_PAGES, min(MAX_PAGES, n))


def extract_links(docx_path: str) -> tuple[list[Link], int]:
    """返回 (去重后的 http/https 超链接列表, 被过滤的非网页链接数)"""
    links: dict[str, Link] = {}
    skipped = 0
    order = 0

    def add(url: str | None, text: str) -> None:
        nonlocal skipped, order
        if not url:
            return
        url = html_unescape(url.strip())
        if not _is_web(url):
            skipped += 1
            return
        key = _norm_key(url)
        if key not in links:
            links[key] = Link(url=url, order=order)
            order += 1
        t = (text or "").strip()[:200]
        if t and t not in links[key].texts:
            links[key].texts.append(t)

    with zipfile.ZipFile(docx_path) as z:
        names = set(z.namelist())
        # 遍历所有可能含正文的 word 部件：document / header / footer / footnotes / endnotes
        # 不能只遍历 rels 文件——老式域代码超链接的 rels 里根本没有 hyperlink 关系
        parts = sorted(n for n in names
                       if n.startswith("word/") and n.endswith(".xml")
                       and not n.startswith("word/_rels/")
                       and not n.startswith("word/theme/"))
        for part_name in parts:
            rels: dict[str, str] = {}
            rels_name = "word/_rels/" + part_name[len("word/"):] + ".rels"
            if rels_name in names:
                try:
                    rel_root = ET.fromstring(z.read(rels_name))
                except ET.ParseError:
                    rel_root = None
                if rel_root is not None:
                    for rel in rel_root.iter(PKG_REL + "Relationship"):
                        if rel.get("Type", "").lower().endswith("/hyperlink"):
                            rid, target = rel.get("Id"), rel.get("Target")
                            if rid and target:
                                rels[rid] = target
            try:
                root = ET.fromstring(z.read(part_name))
            except (ET.ParseError, KeyError):
                continue

            # ① 标准 w:hyperlink（目标 URL 在 relationship 表里）
            if rels:
                for hl in root.iter(W + "hyperlink"):
                    rid = hl.get(R + "id")
                    if rid not in rels:
                        continue
                    text = "".join(t.text or "" for t in hl.iter(W + "t"))
                    add(rels[rid], text)

            # ② 老式 HYPERLINK 域代码：URL 直接写在 w:instrText，不依赖 rels。
            #    按段落合并（域代码可能被拆成多个 instrText），同时取该段落显示文字。
            for para in root.iter(W + "p"):
                # 域代码可能被 Word 拆成多个 instrText，必须无分隔拼接，
                # 否则 URL 中间会被插入空格；正则用 \s* 兼容无空白的片段边界。
                instr = "".join(t.text or "" for t in para.iter(W + "instrText"))
                if not instr or "HYPERLINK" not in instr.upper():
                    continue
                visible = "".join(t.text or "" for t in para.iter(W + "t"))
                quoted = list(re.finditer(
                    r'HYPERLINK\s*(?:"([^"]+)"|\'([^\']+)\')', instr, re.I))
                if quoted:
                    for m in quoted:
                        add(m.group(1) or m.group(2), visible)
                else:
                    # 少数编辑器导出时不带引号，兜底抓裸 URL
                    for m in re.finditer(r'https?://[^\s"\'<>]+', instr, re.I):
                        add(m.group(0), visible)

            # ③ w:fldSimple：域代码写在 w:instr 属性上
            for fld in root.iter(W + "fldSimple"):
                instr = fld.get(W + "instr") or ""
                for m in re.finditer(r'HYPERLINK\s*(?:"([^"]+)"|\'([^\']+)\')', instr, re.I):
                    visible = "".join(t.text or "" for t in fld.iter(W + "t"))
                    add(m.group(1) or m.group(2), visible)
    return sorted(links.values(), key=lambda x: x.order), skipped


def parse_keywords(raw: str) -> list[str]:
    """支持换行 / 中英文逗号 / 分号 / 顿号分隔，去重保序"""
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[\r\n,，;；、]+", raw or ""):
        k = part.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def find_matches(text: str, keywords: list[str], ignore_case: bool = True) -> list[str]:
    hay = text.lower() if ignore_case else text
    return [k for k in keywords if (k.lower() if ignore_case else k) in hay]


def _fetch_html(url: str, timeout: int = 10, verify_ssl: bool = True, *,
                with_cookies: bool = False, tolerate_waf_status: bool = False) -> tuple[str, str]:
    """请求网页并返回 (解码后的 HTML, 最终 URL)。

    超时 / SSL / UA / Content-Type 校验 / 编码回退集中于此，保证每一页行为一致。
    with_cookies=True 时注入浏览器挑战后共享的 cookie；tolerate_waf_status=True
    时对带 cookie 仍返回 WAF 状态（如 520）但正文完整的页面按成功处理——只在
    渲染加速路径里启用，普通抓取仍严格 raise_for_status 保持失败语义。
    """
    sess = _http_session()
    if with_cookies:
        try:
            host = urlsplit(url).netloc
            cks = _apply_cookies(host)
            if cks:
                sess.cookies.update(cks)
        except Exception:
            pass
    resp = sess.get(url, timeout=(min(5, timeout), timeout),
                    allow_redirects=True, verify=verify_ssl)
    if not (tolerate_waf_status and resp.status_code in _WAF_STATUS):
        resp.raise_for_status()
    ctype = resp.headers.get("Content-Type", "").lower()
    if ctype and not any(x in ctype for x in ("html", "text", "xml", "json")):
        raise ValueError(f"非网页内容（{ctype.split(';')[0]}），可能是 PDF 或下载文件")
    clen = resp.headers.get("Content-Length", "")
    if clen.isdigit() and int(clen) > MAX_PAGE_BYTES:
        raise ValueError("页面超过 8MB，已跳过")
    raw = resp.content
    if len(raw) > MAX_PAGE_BYTES:
        raise ValueError("页面超过 8MB，已跳过")
    # 编码优先级：响应头声明 -> UTF-8 严格解码 -> 探测编码 -> GB18030 -> 容错兜底
    declared = (resp.encoding or "").lower()
    candidates: list[str] = []
    if declared and declared not in ("iso-8859-1", "latin-1", "windows-1252"):
        candidates.append(declared)
    candidates.append("utf-8")
    try:
        detected = resp.apparent_encoding
    except Exception:
        detected = None
    if detected and detected.lower() not in [c.lower() for c in candidates]:
        candidates.append(detected)
    candidates.append("gb18030")
    html = None
    for enc in candidates:
        try:
            html = raw.decode(enc, errors="strict")
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if html is None:
        html = raw.decode("utf-8", errors="ignore")
    return html, resp.url


def _html_to_parts(html: str) -> tuple[str, str]:
    """把 HTML 拆成 (正文纯文本, 标题)，并净化脚本 / 样式等噪声节点。"""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(list(_PURIFY_TAGS)):
        tag.decompose()
    body = soup.body or soup
    return body.get_text(" ", strip=True), title


def fetch_page(url: str, timeout: int = 10, verify_ssl: bool = True) -> tuple[str, str, str]:
    """抓取网页并返回 (正文纯文本, 最终URL, 标题)。保持旧版公开签名与语义。"""
    html, final_url = _fetch_html(url, timeout, verify_ssl)
    text, title = _html_to_parts(html)
    return text, final_url, title


def _fetch_page_with_html(url: str, timeout: int = 10,
                          verify_ssl: bool = True) -> tuple[str, str, str, str]:
    """同 fetch_page，额外把原始 HTML 作为第 4 项返回，供分页链接发现使用。"""
    html, final_url = _fetch_html(url, timeout, verify_ssl)
    text, title = _html_to_parts(html)
    return text, final_url, title, html


class _RenderFetchError(Exception):
    """浏览器渲染链路的统一失败异常，携带中文原因与抓取方式标注。"""

    def __init__(self, message: str, via: str = "", render_note: str = "",
                 count_failed: bool = True):
        super().__init__(message)
        self.message = message
        self.fetch_via = via
        self.render_note = render_note
        self.count_failed = count_failed


def _describe_playwright_error(exc: BaseException) -> str:
    """把 Playwright / 浏览器异常转成简短中文原因（按类名与文案识别，不硬 import）。"""
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    if "timeout" in name.lower() or "timed out" in low or "timeout" in low:
        return "浏览器渲染超时"
    if ("executable doesn't exist" in low or "please run the following command" in low
            or "browsertype.launch" in low or "no such file" in low):
        return "未找到浏览器内核"
    if "target closed" in low or "crashed" in low or "page closed" in low:
        return "浏览器页面崩溃或已关闭"
    if "net::" in low or "err_aborted" in low:
        return f"浏览器网络错误：{msg[:50]}"
    return f"浏览器渲染失败：{msg[:60]}"


def _describe_error(exc: BaseException) -> str:
    """把抓取异常统一转成简短中文原因，写入备注 / PageData.error。"""
    if isinstance(exc, _RenderFetchError):
        return exc.message
    if isinstance(exc, requests.exceptions.Timeout):
        return "请求超时"
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSL 证书错误（可勾选忽略证书）"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "重定向次数过多"
    if isinstance(exc, requests.exceptions.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        return f"HTTP {code}"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"网络错误：{type(exc).__name__}"
    # Playwright 异常：按模块名 / 类名识别，http 模式下无需 import playwright
    mod = type(exc).__module__ or ""
    name = type(exc).__name__
    if ("playwright" in mod or name in ("PlaywrightTimeoutError", "PlaywrightError")
            or "playwright" in str(exc)[:160].lower()):
        return _describe_playwright_error(exc)
    return f"{type(exc).__name__}: {str(exc)[:70]}"


# ---------- 分页链接发现 ----------

def _clean_href(href: object) -> str:
    """过滤锚点 / 脚本 / 下载 / 登录类 href，返回可拼接的地址。"""
    if not href:
        return ""
    raw = html_unescape(str(href)).strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return ""
    if _BAD_HREF_EXT.search(raw) or _BAD_HREF_WORD.search(low):
        return ""
    return raw


def _is_next_label(label: str) -> bool:
    """判断 a 标签可见文本是否像「下一页 / next / › / » / >」。"""
    s = re.sub(r"\s+", "", label).strip().lower()
    if not s or len(s) > 12:
        return False
    if s in _NEXT_EXACT:
        return True
    return bool(_NEXT_WORD_RE.search(s))


def _is_pagination_container(tag) -> bool:
    cls = tag.get("class") or []
    if isinstance(cls, str):
        cls = cls.split()
    ident = " ".join([str(c) for c in cls] + [str(tag.get("id") or "")]).lower()
    return bool(_PAGINATION_RE.search(ident))


def _tag_rel(tag) -> list[str]:
    rel = tag.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return [str(r).lower() for r in rel]


def _next_by_query(url: str, visited: set[str]) -> str | None:
    """兜底：URL 中 page / pn / p / offset 参数为纯数字时 +1 构造下一页。"""
    try:
        p = urlsplit(url)
    except Exception:
        return None
    if not p.query:
        return None
    try:
        pairs = parse_qsl(p.query, keep_blank_values=True)
    except Exception:
        return None
    for key in _PAGE_QUERY_KEYS:
        for i, (k, v) in enumerate(pairs):
            if k.lower() != key or not v.isdigit():
                continue
            new_pairs = list(pairs)
            new_pairs[i] = (k, str(int(v) + 1))
            try:
                candidate = urlunsplit((p.scheme, p.netloc, p.path,
                                        urlencode(new_pairs), ""))
            except Exception:
                return None
            if _is_web(candidate) and _norm_key(candidate) not in visited:
                return candidate
            return None
    return None


def _find_next_url(html: str, base_url: str, start_url: str,
                   visited: set[str], current_page: int) -> tuple[str | None, bool]:
    """从已抓页面中挑出下一页 URL。

    返回 (下一页 URL 或 None, 该链接是否由规则④ query 递增构造)。
    识别优先级：① rel=next ② 「下一页」短文本 ③ 分页容器内页码 ④ query 参数递增。
    ④ 仅在页面确有分页迹象时启用，避免给无分页的普通链接误造出后续页。
    """
    if not html:
        return None, False
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return None, False

    def accept(href: object) -> str | None:
        clean = _clean_href(href)
        if not clean:
            return None
        try:
            full = urljoin(base_url, clean)
        except Exception:
            return None
        if not _is_web(full) or not _same_origin(start_url, full):
            return None
        if _norm_key(full) in visited:
            return None
        return full

    # ① <link rel="next"> / <a rel="next">
    for tag in soup.find_all(["link", "a"]):
        if "next" in _tag_rel(tag):
            found = accept(tag.get("href"))
            if found:
                return found, False

    # ② a 标签可见文本为「下一页 / 下页 / next / › / » / >」等
    next_like_seen = False
    digit_link_count = 0
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if _is_next_label(label):
            next_like_seen = True
            found = accept(a.get("href"))
            if found:
                return found, False
        if re.fullmatch(r"\d{1,4}", re.sub(r"\s+", "", label)):
            digit_link_count += 1

    # ③ 常见分页容器（class/id 含 page / pagination / pager）内的页码链接
    containers = [t for t in soup.find_all(
        ["div", "ul", "ol", "nav", "section", "table", "span", "p"])
        if _is_pagination_container(t)]
    for box in containers:
        numbered: list[tuple[int, str]] = []
        plain: list[tuple[str, str]] = []
        for a in box.find_all("a", href=True):
            found = accept(a.get("href"))
            if not found:
                continue
            label = re.sub(r"\s+", "", a.get_text(" ", strip=True))
            plain.append((label, found))
            if label.isdigit():
                numbered.append((int(label), found))
        want = str(current_page + 1)
        for label, found in plain:
            if label == want or label == f"[{want}]":
                return found, False
        bigger = [(n, u) for n, u in numbered if n > current_page]
        if bigger:
            return min(bigger, key=lambda x: x[0])[1], False
        for label, found in plain:
            if _is_next_label(label):
                return found, False

    # ④ 仅在确有分页迹象时，用 URL 参数递增兜底
    if (containers or next_like_seen or digit_link_count >= 2) \
            and _same_origin(start_url, base_url):
        return _next_by_query(base_url, visited), True
    return None, False


def _clamp_details(value) -> int:
    """把「每页最多跟进详情页」限制在 0..50，非法输入回退为 10。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DETAILS
    return max(MIN_DETAILS, min(MAX_DETAILS, n))


def _is_nav_text(label: str) -> bool:
    """链接可见文本是否为短导航词（含 首页>> / 下一页 之类变体）。"""
    s = re.sub(r"\s+", "", label).strip().lower()
    if not s:
        return False
    if s in _NAV_TEXT:
        return True
    # 「首页 >>」「更多 »」这类导航，文本仍以导航词结尾且很短
    return len(s) <= 10 and any(
        s.endswith(w) for w in ("首页", "政务公开", "互动交流", "网站地图",
                                "联系我们", "更多", "more", "home"))


def _has_js_pagination(html: str) -> bool:
    """当前页 HTML 是否含「JS 触发的分页按钮」（无真实 href 的下一页 / 页码）。"""
    if not html:
        return False
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return False
    containers = [t for t in soup.find_all(
        ["div", "ul", "ol", "nav", "section", "table", "span", "p"])
        if _is_pagination_container(t)]
    for box in containers:
        for a in box.find_all("a"):
            label = a.get_text(" ", strip=True)
            if not (_is_next_label(label)
                    or re.fullmatch(r"\d{1,4}", re.sub(r"\s+", "", label))):
                continue
            href = html_unescape(str(a.get("href") or "")).strip().lower()
            if not href or href.startswith(_JS_NEXT_HREF_OK):
                return True
    return False


def extract_detail_links(html: str, base_url: str, start_url: str,
                         limit: int, exclude: set[str]) -> list[str]:
    """从列表页 HTML 提取值得跟进的详情页链接。

    规则：同站 http(s)、规范化去重、排除 exclude（已抓过）、过滤脚本 / 下载 /
    登录类链接、排除分页容器内链接与短导航词；优先路径含 content_ / article /
    detail 等详情特征或可见文本足够长的链接；按页面出现顺序返回，最多 limit 条。
    """
    out: list[str] = []
    if not html or limit <= 0:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out

    # 分页容器内的 a 全部排除（否则会把「下一页」当成详情）
    pager_ids: set[int] = set()
    for t in soup.find_all(
            ["div", "ul", "ol", "nav", "section", "table", "span", "p"]):
        if _is_pagination_container(t):
            for a in t.find_all("a"):
                pager_ids.add(id(a))

    seen: set[str] = set()
    normal: list[str] = []
    preferred: list[str] = []
    for a in soup.find_all("a", href=True):
        if id(a) in pager_ids:
            continue
        clean = _clean_href(a.get("href"))
        if not clean:
            continue
        try:
            full = urljoin(base_url, clean)
        except Exception:
            continue
        # 详情链接只按主机判定同站（列表页 https / 详情页 http 均放行）；
        # 分页防跑飞仍用严格的 _same_origin，两者互不影响
        if not _is_web(full) or not _same_host(start_url, full):
            continue
        key = _norm_key(full)
        if key in seen or key in exclude:
            continue
        label = a.get_text(" ", strip=True)
        if _is_nav_text(label):
            continue
        seen.add(key)
        path = urlsplit(full).path
        is_detail = bool(_DETAIL_PATH_RE.search(path)) or len(label.strip()) >= 6
        (preferred if is_detail else normal).append(full)
    # 详情页特征优先，同组保持页面出现顺序
    for url in preferred + normal:
        out.append(url)
        if len(out) >= limit:
            break
    return out


# ---------- 浏览器渲染（Playwright，仅 auto / browser 模式延迟导入） ----------

def _clamp_render_wait(value) -> float:
    """render_wait 收敛到 0..15，非法值（含 NaN/inf）回退 2.0。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 2.0
    if v != v or v in (float("inf"), float("-inf")):
        return 2.0
    return max(0.0, min(15.0, v))


def _clamp_slots(value) -> int:
    """browser_slots 收敛到 1..8，非法值回退 2。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 2
    return max(1, min(8, n))


def _looks_like_js_challenge(html: str) -> bool:
    """判断 HTML 是否像 JS 挑战页（短正文 + cookie/reload/meta 挑战脚本）。"""
    if not html:
        return False
    low = html.lower()
    if "<script" not in low and "<meta" not in low:
        return False
    signatures = (
        "document.cookie", "location.reload", "location.href", "window.location",
        "settimeout(", "eval(", "document.write(", "challenge", "cf-chl",
        "_cf_chl", "jschl", "anti-bot", "http-equiv=\"refresh",
        "http-equiv=refresh", "waf", "cookie=", "set-cookie",
    )
    return any(s in low for s in signatures)


def browser_diagnose() -> dict:
    """快速探测本机浏览器通道（仅 launch + close，不联网），任何情况都不抛异常。

    返回 {"available": [channel...], "checked": {channel: 中文说明}, "hint": 中文提示}。
    """
    checked: dict[str, str] = {ch: "" for ch in _CHANNEL_ORDER}
    available: list[str] = []
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        detail = _describe_playwright_error(exc)
        for ch in _CHANNEL_ORDER:
            checked[ch] = f"不可用：Playwright 无法加载（{detail}）"
        return {"available": [], "checked": checked,
                "hint": "未安装或无法加载 Playwright，请运行 pip install playwright 后重试"}

    try:
        pw = sync_playwright().start()
    except Exception as exc:
        detail = _describe_playwright_error(exc)
        for ch in _CHANNEL_ORDER:
            checked[ch] = f"不可用：浏览器驱动启动失败（{detail}）"
        return {"available": [], "checked": checked,
                "hint": f"浏览器驱动启动失败（{detail}），请重新安装 playwright"}

    try:
        for ch in _CHANNEL_ORDER:
            browser = None
            try:
                kwargs = {"headless": True, "args": list(_LAUNCH_ARGS)}
                if ch != "chromium":
                    kwargs["channel"] = ch
                browser = pw.chromium.launch(**kwargs)
                checked[ch] = f"可用（{browser.version}）"
                available.append(ch)
            except Exception as exc:
                checked[ch] = f"不可用：{_describe_playwright_error(exc)}"
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    finally:
        try:
            pw.stop()
        except Exception:
            pass

    if available:
        hint = "已检测到可用浏览器：" + "、".join(_CHANNEL_LABEL[c] for c in available)
    else:
        hint = ("本机未检测到 Edge/Chrome，可运行 playwright install chromium "
                "安装自带内核后使用「Chromium 渲染」")
    return {"available": available, "checked": checked, "hint": hint}


class _BrowserSession:
    """管理 scan() 期间的浏览器渲染资源。

    Playwright sync API 不可跨线程共享，故用 threading.local() 让每个 worker 线程
    懒创建并复用自己的 driver + browser + context；每个 work() 任务在 finally 中
    调用 release_thread() 关闭 driver，避免 node.exe / 浏览器进程泄漏。
    threading.Semaphore 全局限制同时进行的渲染数。
    """

    def __init__(self, channel: str, render_wait: float, slots: int, should_stop=None):
        self._candidates: list[str] = (list(_CHANNEL_ORDER) if channel == "auto"
                                       else [channel])
        self._render_wait = _clamp_render_wait(render_wait)
        self._sem = threading.Semaphore(_clamp_slots(slots))
        self._should_stop = should_stop
        self._local = threading.local()
        self._lock = threading.Lock()
        self._preferred: str | None = None
        self.used = 0
        self.failed = 0

    # ---------- 线程内生命周期 ----------
    def release_thread(self) -> None:
        """关闭并清空当前线程的 context / browser / driver，可重复调用。"""
        st = self._local
        ctx = getattr(st, "context", None)
        browser = getattr(st, "browser", None)
        pw = getattr(st, "pw", None)
        st.context = None
        st.browser = None
        st.pw = None
        st.channel = None
        st.label = ""
        for obj in (ctx, browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def _stopped(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception:
            return False

    def _candidate_order(self) -> list[str]:
        with self._lock:
            pref = self._preferred
        if pref and pref in self._candidates:
            return [pref] + [c for c in self._candidates if c != pref]
        return list(self._candidates)

    def _ensure_browser(self, verify_ssl: bool):
        """在当前线程懒创建 driver + browser + context，返回线程状态对象。"""
        st = self._local
        if getattr(st, "pw", None) is not None and getattr(st, "browser", None) is not None:
            return st
        # 延迟导入：render_mode="http" 或显式注入 fetcher 时绝不会执行到这里
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise _RenderFetchError(
                "未安装 Playwright，无法使用浏览器渲染；请运行 pip install playwright",
                count_failed=True) from exc

        last_exc: BaseException | None = None
        for ch in self._candidate_order():
            pw = None
            browser = None
            try:
                pw = sync_playwright().start()
                kwargs = {"headless": True, "args": list(_LAUNCH_ARGS)}
                if ch != "chromium":
                    kwargs["channel"] = ch
                browser = pw.chromium.launch(**kwargs)
                ctx = browser.new_context(
                    locale="zh-CN",
                    user_agent=HEADERS["User-Agent"],
                    ignore_https_errors=not verify_ssl,
                    viewport={"width": 1366, "height": 900},
                )
                st.pw, st.browser, st.context = pw, browser, ctx
                st.channel, st.label = ch, _CHANNEL_LABEL[ch]
                with self._lock:
                    if self._preferred is None:
                        self._preferred = ch
                return st
            except Exception as exc:
                last_exc = exc
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if pw is not None:
                    try:
                        pw.stop()
                    except Exception:
                        pass
                continue
        raise _RenderFetchError(self._launch_failure_message(last_exc),
                                count_failed=True)

    def _launch_failure_message(self, exc: BaseException | None) -> str:
        detail = _describe_playwright_error(exc) if exc is not None else "未知原因"
        if len(self._candidates) == 1:
            return (f"无法启动 {_CHANNEL_LABEL[self._candidates[0]]}：{detail}；"
                    "可运行 playwright install chromium 安装内核，或改用「自动」通道")
        return (f"本机未检测到可用的 Edge/Chrome/Chromium 浏览器：{detail}；"
                "可运行 playwright install chromium 安装自带内核")

    # ---------- 单页渲染 ----------
    @staticmethod
    def _body_text(page) -> str:
        try:
            return page.locator("body").inner_text(timeout=2000) or ""
        except Exception:
            try:
                return page.evaluate(
                    "() => document.body ? document.body.innerText : ''") or ""
            except Exception:
                return ""

    def _render_page(self, st, url: str, timeout: int) -> tuple:
        """渲染单页，返回 (净化正文, 最终URL, 标题, 渲染后HTML, page)。

        page 由调用方负责关闭；渲染中途异常时本方法自行关闭，避免泄漏。
        """
        page = None
        ok = False
        doc_status: dict[str, int | None] = {"last": None}
        try:
            page = st.context.new_page()

            def _on_response(resp) -> None:
                try:
                    if resp.request.resource_type == "document":
                        doc_status["last"] = resp.status
                except Exception:
                    pass

            page.on("response", _on_response)
            try:
                goto_ms = max(1000, int(float(timeout)) * 1000)
            except (TypeError, ValueError):
                goto_ms = 10000
            page.goto(url, wait_until="domcontentloaded", timeout=goto_ms)

            t0 = time.time()
            while True:
                if self._stopped():
                    raise _RenderFetchError("已手动停止，终止浏览器渲染",
                                            count_failed=False)
                text = self._body_text(page)
                elapsed = time.time() - t0
                # 至少额外等待 render_wait 秒（WAF 的 JS 挑战要 set-cookie + reload），
                # 正文超过 180 字即认为挑战完成；总预算 15s 兜底
                if elapsed >= self._render_wait and len(text.strip()) > _MIN_CHALLENGE_TEXT:
                    break
                # 明确的错误页（404 等，排除 WAF 常用的 403/412/429/503）无需空等预算
                status_now = doc_status["last"]
                if (status_now is not None and status_now >= 400
                        and status_now not in _WAF_STATUS
                        and elapsed >= self._render_wait):
                    break
                if elapsed >= _RENDER_BUDGET:
                    break
                page.wait_for_timeout(_RENDER_POLL_MS)

            status = doc_status["last"]
            if status is not None and status >= 400:
                raise _RenderFetchError(f"HTTP {status}（浏览器渲染后仍为错误页）",
                                        count_failed=True)

            final_url = page.url
            # 跨主机重定向按失败处理；仅比较 netloc（忽略 scheme），
            # 避免把常见的 http→https 同主机跳转误判成外站
            try:
                a, b = urlsplit(url), urlsplit(final_url)
                cross_host = a.netloc.lower() != b.netloc.lower()
            except Exception:
                cross_host = False
            if cross_host:
                raise _RenderFetchError(
                    f"已重定向到外站（{final_url[:70]}），停止抓取",
                    count_failed=True)
            html = page.content()      # 渲染后 DOM，让分页发现吃到 JS 生成的「下一页」
            title = page.title()
            clean_text, clean_title = _html_to_parts(html)
            ok = True
            return clean_text, final_url, clean_title or title, html, page
        finally:
            if page is not None and not ok:
                try:
                    page.close()
                except Exception:
                    pass

    def render(self, url: str, timeout: int, verify_ssl: bool, *,
               via_prefix: str = "", reason: str = "",
               with_js_next: bool = False, start_url: str = "") -> tuple:
        """用浏览器渲染一页，返回 6 元组供 fetch_pages 消费。

        第 5 项为 fetch_via，第 6 项为需要追加进备注的可读说明。
        with_js_next=True 且页面确有 JS 分页时，追加第 7 项无参 callable js_next。
        """
        label = ""
        page = None
        try:
            if self._stopped():
                raise _RenderFetchError("已手动停止，跳过浏览器渲染",
                                        count_failed=False)
            with self._sem:
                st = self._ensure_browser(verify_ssl)
                label = st.label
                text, final_url, title, html, page = self._render_page(
                    st, url, timeout)
                if self._stopped():
                    raise _RenderFetchError("已手动停止，丢弃本页渲染结果",
                                            count_failed=False)
                # WAF 挑战成功后把 cookie 存入共享池：同域后续页可走 HTTP 直连，
                # 省掉每页浏览器渲染（详情页提速关键）。失败时不导出，避免毒化池。
                try:
                    _remember_cookies(st.context.cookies())
                except Exception:
                    pass
            with self._lock:
                self.used += 1
            via = via_prefix + label
            note = f"{reason}，已自动改用 {label}" if reason else ""
            result = [text, final_url, title, html, via, note]
            if with_js_next and page is not None and _has_js_pagination(html):
                origin = start_url or url
                seen = {_norm_key(origin), _norm_key(final_url)}
                result.append(self._make_js_next(page, timeout, via, origin, seen))
                page = None  # 生命周期交给 js_next 闭包，work 结束时 release_thread 兜底
            return tuple(result)
        except _RenderFetchError as exc:
            if not exc.fetch_via:
                exc.fetch_via = via_prefix + (label or "浏览器渲染")
            if exc.count_failed:
                with self._lock:
                    self.failed += 1
            raise
        except Exception as exc:
            with self._lock:
                self.failed += 1
            raise _RenderFetchError(
                f"浏览器渲染失败：{_describe_playwright_error(exc)}",
                via=via_prefix + (label or "浏览器渲染"),
                count_failed=True) from exc
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    @staticmethod
    def _find_js_next_anchor(page):
        """在当前页面的分页容器内定位「下一页」或「当前页+1」数字的 a 标签。"""
        try:
            anchors = page.locator("a")
            count = min(anchors.count(), 400)
        except Exception:
            return None
        nexts: list = []
        digits: list[tuple[int, object]] = []
        for i in range(count):
            a = anchors.nth(i)
            try:
                text = a.inner_text(timeout=600) or ""
            except Exception:
                continue
            label = re.sub(r"\s+", "", text).strip()
            if not label:
                continue
            # 仅在分页容器内查找，杜绝点到导航菜单 / 正文里的「下一页」广告
            try:
                anc = a.evaluate(
                    "(el)=>{let o=[],p=el;for(let i=0;i<6&&p;i++,p=p.parentElement)"
                    "{o.push(((p.className||'')+' '+(p.id||'')).trim())}"
                    "return o.join(' ')}")
            except Exception:
                continue
            if not _PAGINATION_RE.search(anc or ""):
                continue
            href = html_unescape(str(a.get_attribute("href") or "")).strip().lower()
            if _BAD_HREF_EXT.search(href) or _BAD_HREF_WORD.search(href) \
                    and not href.startswith("javascript:"):
                continue
            if _is_next_label(text):
                nexts.append(a)
            elif label.isdigit() and len(label) <= 4:
                digits.append((int(label), a))
        if nexts:
            return nexts[0]
        if digits:
            current = None
            for n, a in digits:
                try:
                    cls = (a.get_attribute("class") or "").lower()
                    aria = (a.get_attribute("aria-current") or "").lower()
                except Exception:
                    cls, aria = "", ""
                if ("active" in cls or "current" in cls or "cur" in cls
                        or "selected" in cls or aria == "page"):
                    current = n
                    break
            target = current + 1 if current is not None else None
            if target is not None:
                for n, a in digits:
                    if n == target:
                        return a
            for n, a in digits:
                if n > 1:
                    return a
        return None

    def _make_js_next(self, page, timeout: int, via: str, start_url: str,
                      seen: set[str], ajax_page: int = 1):
        """构造无参 js_next：点击分页「下一页」，返回新页 6 元组（可再带第 7 项）。

        点击后轮询 URL / 正文变化，做同源校验与 URL 规范化去重；到头或失败返回 None。
        ajax_page 为当前展示页序号（当前页算 1）：AJAX 换页时 URL 不变，用
        __wls_page=N 合成 query 区分不同页，避免被 seen / visited 当成重复页丢弃。
        """
        def _js_next() -> tuple | None:
            if self._stopped():
                return None
            anchor = self._find_js_next_anchor(page)
            if anchor is None:
                return None
            before_url = page.url
            before_text = self._body_text(page)
            try:
                click_ms = max(1000, int(float(timeout)) * 1000)
                anchor.click(timeout=min(click_ms, 5000))
            except Exception:
                return None
            try:
                budget = min(max(float(timeout), 1.0), 10.0)
            except (TypeError, ValueError):
                budget = 5.0
            t0 = time.time()
            changed = False
            url_changed = False
            while True:
                if self._stopped():
                    return None
                cur_url = page.url
                cur_text = self._body_text(page)
                url_changed = cur_url != before_url
                if url_changed or (cur_text and cur_text != before_text):
                    changed = True
                    break
                if time.time() - t0 >= budget:
                    break
                page.wait_for_timeout(_RENDER_POLL_MS)
            if not changed:
                return None  # 点击后 URL 与正文都没变化：到头，平静停止
            try:
                page.wait_for_timeout(int(min(self._render_wait, 3.0) * 1000))
            except Exception:
                pass
            raw_url = page.url
            if not _same_origin(start_url, raw_url):
                return None
            next_page = ajax_page + 1
            if url_changed:
                # 真实 URL 变化（普通分页）：沿用原 URL 与去重逻辑
                final_url = raw_url
            else:
                # AJAX 换页：URL 不变但正文变了，合成带 query 的展示 URL，
                # 让 seen / fetch_pages 的 visited 能区分不同页，避免被当重复丢弃
                final_url = _synth_page_url(raw_url, next_page)
            key = _norm_key(final_url)
            if key in seen:
                return None
            seen.add(key)
            try:
                html = page.content()
                title = page.title()
            except Exception:
                return None
            clean_text, clean_title = _html_to_parts(html)
            result = (clean_text, final_url, clean_title or title, html, via, "")
            if _has_js_pagination(html):
                result += (self._make_js_next(
                    page, timeout, via, start_url, seen, next_page),)
            return result

        return _js_next

    # ---------- auto 模式：先 HTTP，命中 WAF 再浏览器重试 ----------
    def auto_fetch(self, url: str, timeout: int, verify_ssl: bool, *,
                   with_js_next: bool = False, start_url: str = "") -> tuple:
        try:
            html, final_url = _fetch_html(url, timeout, verify_ssl)
        except requests.exceptions.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            if code in _WAF_STATUS:
                return self.render(url, timeout, verify_ssl, via_prefix="HTTP→",
                                   reason=f"HTTP {code}",
                                   with_js_next=with_js_next, start_url=start_url)
            raise
        text, title = _html_to_parts(html)
        if len(text.strip()) < _MIN_CHALLENGE_TEXT and _looks_like_js_challenge(html):
            return self.render(url, timeout, verify_ssl, via_prefix="HTTP→",
                               reason="疑似 JS 挑战（正文过短）",
                               with_js_next=with_js_next, start_url=start_url)
        return text, final_url, title, html, "HTTP", ""

    def make_fetcher(self, mode: str):
        """构造供 fetch_pages 注入的 fetcher，签名 (url, timeout, verify_ssl)。

        渲染模式下额外返回第 7 项 js_next callable，供纯 JS 分页点击跟进。
        """
        def _fetch(url: str, timeout: int, verify_ssl: bool) -> tuple:
            if mode == "browser":
                return self.render(url, timeout, verify_ssl,
                                   with_js_next=True, start_url=url)
            return self.auto_fetch(url, timeout, verify_ssl,
                                   with_js_next=True, start_url=url)
        return _fetch

    def make_detail_fetcher(self, mode: str):
        """详情页专用 fetcher：同域已有浏览器 cookie 时先走 HTTP 直连，
        正文合格（足够长且非 JS 挑战）就直接采用，省去每篇的浏览器渲染；
        否则回退到与列表页相同的渲染逻辑，保证结果语义不变。"""
        base = self.make_fetcher(mode)

        def _fetch(url: str, timeout: int, verify_ssl: bool) -> tuple:
            try:
                host = urlsplit(url).netloc
                if _apply_cookies(host):
                    html, final = _fetch_html(url, timeout, verify_ssl,
                                              with_cookies=True,
                                              tolerate_waf_status=True)
                    text, title = _html_to_parts(html)
                    if len(text.strip()) >= _MIN_CHALLENGE_TEXT and not _looks_like_js_challenge(html):
                        return text, final, title, html, "HTTP（复用渲染会话）", ""
            except Exception:
                pass  # 带 cookie 的 HTTP 不可用则回退渲染
            return base(url, timeout, verify_ssl)
        return _fetch


def fetch_pages(start_url: str, max_pages: int = 1, *, timeout: int = 10,
                verify_ssl: bool = True, fetcher=None,
                should_stop=None) -> list[PageData]:
    """抓取一个起始链接及其自身分页的前 N 页，返回逐页结果列表。

    max_pages 被限制在 1..20；默认 1 即不翻页（与旧版行为一致）。
    fetcher 可选，签名 (url, timeout, verify_ssl)，返回 (text, final_url, title)
    或 (text, final_url, title, html)；只有提供第 4 项 HTML 时才能发现 DOM 分页。
    内部渲染 fetcher 另返回可选第 5 项 fetch_via / 第 6 项渲染说明，缺省时
    一律按 "HTTP" 处理，不改变 3 / 4 元组注入的公开契约。
    渲染 fetcher 可再追加第 7 项无参 callable js_next：当真实 href 找不到且
    当前页有 JS 分页特征时调用它，直接把结果作为下一页（不重复 fetch）。
    失败页会作为一条 error 非空的 PageData 返回，不会被静默丢弃。
    """
    limit = _clamp_pages(max_pages)
    fetch = fetcher or _fetch_page_with_html
    pages: list[PageData] = []
    visited: set[str] = {_norm_key(start_url)}
    current = start_url
    query_mode = False  # 本链是否已进入「URL 参数递增」兜底模式
    pending: tuple | None = None  # js_next 预取的下一页结果，下一轮直接使用

    while len(pages) < limit:
        if should_stop and should_stop():
            break
        try:
            if pending is not None:
                result = pending          # 点击已抓到下一页，不重复请求
                pending = None
            else:
                result = tuple(fetch(current, timeout, verify_ssl))
            if len(result) < 3:
                raise ValueError("fetcher 至少需返回 (正文, 最终URL, 标题)")
            text = str(result[0] or "")
            final = str(result[1] or current)
            title = str(result[2] or "")
            html = str(result[3]) if len(result) > 3 and result[3] else ""
            via = str(result[4]) if len(result) > 4 and result[4] else "HTTP"
            render_note = str(result[5]) if len(result) > 5 and result[5] else ""
            js_next = result[6] if len(result) > 6 and callable(result[6]) else None
        except Exception as exc:
            # 失败页也要占一行，便于排查；但无法从失败页继续安全地跟进分页
            pages.append(PageData(page_no=len(pages) + 1, url=current,
                                  error=_describe_error(exc),
                                  fetch_via=getattr(exc, "fetch_via", "") or "HTTP",
                                  render_note=getattr(exc, "render_note", "") or ""))
            break

        visited.add(_norm_key(final))
        note = ""
        nxt: str | None = None
        if len(pages) + 1 < limit:
            if not _same_origin(start_url, final):
                note = "已重定向到外站，停止跟进分页"
            elif not (should_stop and should_stop()):
                dom_next, via_query = _find_next_url(
                    html, final, start_url, visited, len(pages) + 1)
                if dom_next:
                    nxt = dom_next
                    query_mode = via_query
                elif query_mode and _same_origin(start_url, final):
                    # 已进入参数递增模式：后续页即使没有分页条，也继续按参数
                    # 递增到 N 页 / 失败 / 去重为止，仍受同源与 N≤20 约束
                    nxt = _next_by_query(final, visited)
                elif js_next is not None and _has_js_pagination(html):
                    # 无真实 href 的纯 JS 分页：点击下一页按钮直接取得数据
                    try:
                        following = js_next()
                    except Exception:
                        following = None
                    if following and len(tuple(following)) >= 3:
                        cand = tuple(following)
                        cand_final = str(cand[1] or "")
                        if (_same_origin(start_url, cand_final)
                                and _norm_key(cand_final) not in visited):
                            pending = cand
                            query_mode = False
                            note = "；".join(
                                x for x in (note, "通过 JS 分页按钮翻页") if x)

        pages.append(PageData(page_no=len(pages) + 1, url=current, text=text,
                              title=title, final_url=final, note=note,
                              fetch_via=via, render_note=render_note, html=html))
        if pending is not None:
            # 下一页数据已就绪：更新 current 供下一轮行展示，随后直接消费
            current = str(pending[1] or current)
            visited.add(_norm_key(current))
            continue
        if not nxt or _norm_key(nxt) in visited:
            break
        visited.add(_norm_key(nxt))
        current = nxt
    return pages


def scan(docx_path: str, keywords: list[str], *, workers: int = 8, timeout: int = 10,
         ignore_case: bool = True, verify_ssl: bool = True, max_pages: int = 1,
         on_progress=None, should_stop=None, fetcher=None,
         render_mode: str = "http", browser_channel: str = "auto",
         render_wait: float = 2.0, browser_slots: int = 2,
         follow_details: bool = False, max_details: int = 10) -> dict:
    """扫描全部链接，每个链接可按 max_pages 跟进网页自身分页。

    render_mode：http（默认，仅 requests，与旧版完全一致）/ auto（遇 WAF 状态码
    或 JS 挑战短正文时自动改用浏览器重试该页一次）/ browser（全量浏览器渲染）。
    显式传入 fetcher 时永远以注入为准，渲染模式不会干扰测试注入。
    follow_details=True 时，对抓取成功的列表页再跟进其中的详情页（每页最多
    max_details 条，全局去重），详情页正文同样做关键字匹配并单独成行；
    详情页在第二阶段按 workers 并发抓取，浏览器会话在线程内复用，显著提速。
    on_progress(done, total, row) 按「页」推进：total 为预估页面任务数
    (起始链接数 × max_pages，详情页任务不计入分母)；should_stop() 返回 True 则尽快停止。
    返回字典中 total 仍为「起始链接数」，pages 为「实际产出的页面行数」，
    detail_pages / detail_matched 为详情页行数 / 其中命中数。
    """
    mode = render_mode if render_mode in RENDER_MODES else "http"
    channel = browser_channel if browser_channel in BROWSER_CHANNELS else "auto"
    slots = _clamp_slots(browser_slots)
    # 每次扫描重置共享 cookie 池，避免上一次扫描的会话 cookie 污染本次结果
    with _cookie_lock:
        _shared_cookies.clear()

    links, skipped = extract_links(docx_path)
    total_links = len(links)
    limit = _clamp_pages(max_pages)
    total_tasks = total_links * limit
    rows: list[dict] = []

    session = None
    if fetcher is None and mode in ("auto", "browser"):
        session = _BrowserSession(channel=channel, render_wait=render_wait,
                                  slots=slots, should_stop=should_stop)
        effective_fetcher = session.make_fetcher(mode)
    else:
        # None 时 fetch_pages 用默认 _fetch_page_with_html，零 Playwright 开销
        effective_fetcher = fetcher

    # 真正启用浏览器渲染时，线程数必须收敛到浏览器槽位：每个渲染线程都会
    # thread-local 懒启动一套 Playwright driver + Edge，并持有到线程任务结束
    # 才释放。若线程数 > slots，就会启动 N 套浏览器常驻抢内存/CPU（卡死根因）。
    # 注入 fetcher / 纯 HTTP 模式没有浏览器开销，沿用 workers 并发。
    if session is not None:
        pool_workers = slots
    else:
        pool_workers = max(1, min(int(workers), 32))

    # 详情页跟进：显式注入 fetcher 时同样生效；全局去重避免多个栏目页重复抓同一篇
    detail_limit = _clamp_details(max_details) if follow_details else 0
    detail_visited: set[str] = set()
    detail_lock = threading.Lock()
    # 详情页专用 fetcher：渲染模式下先试「带 WAF cookie 的 HTTP 直连」，命中正文
    # 就免渲染；注入 fetcher / 纯 HTTP 模式保持原样。
    page_fetcher = (session.make_detail_fetcher(mode) if session is not None
                    else (effective_fetcher or _fetch_page_with_html))

    # 详情页候选在第二阶段并发抓取（原实现是列表页线程内逐个串行，页面一多就很慢）。
    # 任务元组：(idx, page_no, page_count, docx起始链接, 父列表页, 链接文字, 详情URL, 序号)
    detail_tasks: list[tuple] = []

    def build_list_rows(idx: int, link: Link) -> tuple[list[dict], list[tuple]]:
        pages = fetch_pages(link.url, limit, timeout=timeout,
                            verify_ssl=verify_ssl, fetcher=effective_fetcher,
                            should_stop=should_stop)
        count = len(pages) if pages else 1
        out: list[dict] = []
        pending: list[tuple] = []
        detail_pos = 0
        for p in pages:
            note = "；".join(x for x in (p.note, p.render_note, p.error) if x)
            row = {"idx": idx, "page_no": p.page_no, "page_count": count,
                   "parent_url": link.url, "url": p.url, "final_url": p.final_url,
                   "texts": " / ".join(link.texts), "title": (p.title or "")[:200],
                   "matched": [], "status": "", "note": note,
                   "fetch_via": p.fetch_via or "HTTP",
                   "level": "列表页", "detail_of": ""}
            if p.error:
                row["status"] = "失败"
            else:
                hits = find_matches(p.title + " " + p.text, keywords, ignore_case)
                row["matched"] = hits
                row["status"] = "命中" if hits else "未命中"
            out.append(row)

            # 只对抓取成功且有 HTML 的列表页收集详情候选；详情页自身分页不跟进
            if detail_limit <= 0 or p.error or not p.html:
                continue
            if should_stop and should_stop():
                continue
            parent = p.final_url or p.url
            candidates = extract_detail_links(
                p.html, parent, link.url, detail_limit, detail_visited)
            for d_url in candidates:
                d_key = _norm_key(d_url)
                with detail_lock:
                    if d_key in detail_visited:
                        continue
                    detail_visited.add(d_key)
                detail_pos += 1
                pending.append((idx, p.page_no, count, link.url, parent,
                                " / ".join(link.texts), d_url, detail_pos))
        return out, pending

    def make_detail_row(task: tuple) -> dict:
        idx, page_no, count, root_url, parent, texts, d_url, detail_pos = task
        d_text, d_final, d_title, d_via, d_err = "", "", "", "HTTP", ""
        try:
            d_result = tuple(page_fetcher(d_url, timeout, verify_ssl))
            if len(d_result) < 3:
                raise ValueError("详情页 fetcher 返回格式不正确")
            d_text = str(d_result[0] or "")
            d_final = str(d_result[1] or d_url)
            d_title = str(d_result[2] or "")
            if len(d_result) > 4 and d_result[4]:
                d_via = str(d_result[4])
        except Exception as exc:
            d_err = _describe_error(exc)
            d_via = getattr(exc, "fetch_via", "") or d_via
        d_note = f"详情页来源：{parent}"
        if d_err:
            d_note = f"{d_note}；{d_err}"
        row = {"idx": idx, "page_no": page_no, "page_count": count,
               "parent_url": root_url, "url": d_url, "final_url": d_final,
               "texts": texts, "title": (d_title or "")[:200],
               "matched": [], "status": "", "note": d_note,
               "fetch_via": d_via, "level": "详情页", "detail_of": parent,
               "detail_order": detail_pos}
        if d_err:
            row["status"] = "失败"
        else:
            hits = find_matches(d_title + " " + d_text, keywords, ignore_case)
            row["matched"] = hits
            row["status"] = "命中" if hits else "未命中"
        return row

    def work(idx: int, link: Link) -> tuple[list[dict], list[tuple]]:
        try:
            return build_list_rows(idx, link)
        except Exception as exc:
            # 兜底：单个起始链接整体异常也不能让扫描崩溃，且必须留下一行可排查
            return ([{"idx": idx, "page_no": 1, "page_count": 1,
                      "parent_url": link.url, "url": link.url, "final_url": "",
                      "texts": " / ".join(link.texts), "title": "",
                      "matched": [], "status": "失败", "note": _describe_error(exc),
                      "fetch_via": getattr(exc, "fetch_via", "") or "HTTP",
                      "level": "列表页", "detail_of": ""}], [])
        finally:
            # 列表页任务结束即关闭本线程 driver，避免 node.exe / 浏览器进程泄漏
            if session is not None:
                session.release_thread()

    def run_detail_tasks(tasks: list[tuple], base_done: int, base_total: int) -> None:
        """并发抓取详情页：每个线程从队列循环取任务，线程内复用浏览器，
        整批结束才 release_thread（避免每篇详情都重新启动一次 Playwright）。
        详情页也计入进度，分母 = 列表页实际完成数 + 详情页任务总数。"""
        if not tasks:
            return
        n = max(1, min(len(tasks), pool_workers))
        q: queue.Queue = queue.Queue()
        for t in tasks:
            q.put(t)
        for _ in range(n):
            q.put(None)  # 每个线程一个结束哨兵
        append_lock = threading.Lock()
        detail_done = [0]

        def loop():
            try:
                while True:
                    if should_stop and should_stop():
                        break
                    try:
                        task = q.get_nowait()
                    except queue.Empty:
                        break
                    if task is None:
                        break
                    try:
                        row = make_detail_row(task)
                    except Exception as exc:
                        row = {"idx": task[0], "page_no": task[1], "page_count": task[2],
                               "parent_url": task[3], "url": task[6], "final_url": "",
                               "texts": task[5], "title": "", "matched": [],
                               "status": "失败", "note": _describe_error(exc),
                               "fetch_via": getattr(exc, "fetch_via", "") or "HTTP",
                               "level": "详情页", "detail_of": task[4],
                               "detail_order": task[7]}
                    with append_lock:
                        rows.append(row)
                        detail_done[0] += 1
                        if on_progress:
                            on_progress(base_done + detail_done[0], base_total, row)
            finally:
                if session is not None:
                    session.release_thread()

        dex = ThreadPoolExecutor(max_workers=n)
        try:
            futures = [dex.submit(loop) for _ in range(n)]
            for fut in futures:
                fut.result()
        finally:
            dex.shutdown(wait=True)

    ex = ThreadPoolExecutor(max_workers=pool_workers)
    stopped = False
    done = 0
    try:
        futures = [ex.submit(work, i, l) for i, l in enumerate(links)]
        for fut in as_completed(futures):
            list_rows, pending = fut.result()
            for row in list_rows:
                row.setdefault("fetch_via", "HTTP")
                row.setdefault("level", "列表页")
                row.setdefault("detail_of", "")
                rows.append(row)
                done += 1
                if on_progress:
                    on_progress(done, total_tasks, row)
            detail_tasks.extend(pending)
            if should_stop and should_stop():
                stopped = True
                break
    finally:
        ex.shutdown(wait=not stopped, cancel_futures=stopped)

    # 第二阶段：详情页并发抓取；成功与否都各占一行，排序/统计口径与旧版一致。
    # 分母从「链接数×页数」修正为「已抓列表页 + 详情任务数」，进度延伸到详情页，
    # 避免停在中间看起来像卡死。
    if not stopped and detail_tasks:
        detail_total = done + len(detail_tasks)
        if on_progress:
            on_progress(done, detail_total, None)  # 仅更新分母，不插入行
        run_detail_tasks(detail_tasks, done, detail_total)

    rows.sort(key=lambda r: (STATUS_RANK.get(r["status"], 9), r["idx"], r["page_no"],
                             _LEVEL_RANK.get(r.get("level", "列表页"), 0),
                             r.get("detail_order", 0)))
    return {
        "rows": rows,
        "total": total_links,
        "pages": len(rows),
        "matched": sum(1 for r in rows if r["status"] == "命中"),
        "failed": sum(1 for r in rows if r["status"] == "失败"),
        "skipped": skipped,
        "stopped": stopped,
        "page_limit": limit,
        "browser_used": session.used if session is not None else 0,
        "browser_failed": session.failed if session is not None else 0,
        "render_mode": mode,
        "detail_pages": sum(1 for r in rows if r.get("level") == "详情页"),
        "detail_matched": sum(1 for r in rows if r.get("level") == "详情页"
                              and r["status"] == "命中"),
    }


def _row_values(i: int, r: dict) -> list:
    pn, pc = r.get("page_no"), r.get("page_count")
    page_label = f"{pn}/{pc}" if pn is not None and pc is not None else ""
    return [i, r.get("status", ""), "、".join(r.get("matched", [])), r.get("texts", ""),
            r.get("title", ""), r.get("url", ""), r.get("final_url", ""), r.get("note", ""),
            page_label, r.get("parent_url", ""), r.get("fetch_via", "HTTP"),
            r.get("level", "列表页")]


def export_xlsx(path: str, rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "链接汇总"
    ws.append(FIELDS)
    fill = PatternFill("solid", fgColor="DCE6F1")
    for c in ws[1]:
        c.font = Font(bold=True, color="1F3864")
        c.fill = fill
        c.alignment = Alignment(vertical="center")
    for i, r in enumerate(rows, 1):
        ws.append(_row_values(i, r))
        # 第 6 列「链接URL」始终指向该行对应的具体页面 URL（分页时为第 k 页）
        if r.get("url", "").startswith("http"):
            cell = ws.cell(row=i + 1, column=6)
            cell.hyperlink = r["url"]
            cell.font = Font(color="0563C1", underline="single")
        if r.get("status") == "命中":
            ws.cell(row=i + 1, column=2).font = Font(bold=True, color="0B6B3A")
    for idx, width in enumerate([6, 10, 22, 32, 32, 52, 52, 42, 10, 52, 16, 12], 1):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = f"A1:L{len(rows) + 1}"
    wb.save(path)


def export_csv(path: str, rows: list[dict]) -> None:
    # utf-8-sig 保证 Excel 直接打开中文不乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for i, r in enumerate(rows, 1):
            w.writerow(_row_values(i, r))
