#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browsesearch — 真正从浏览器里联网搜索的小工具
=============================================

和那些「调一个搜索 API」的工具不同，browsesearch 默认直接驱动一个
**真实的 Chromium 无头浏览器**去打开搜索引擎页面：让 JavaScript 把结果
渲染出来（顺带绕过大部分反爬/人机校验），再读取渲染后的 DOM 抽取标题、
链接与摘要。

如果环境里没有装 Playwright（或不想用浏览器），它会自动回退到「纯标准库
HTTP 请求」模式：用真实桌面浏览器的 User-Agent 去抓取搜索页 HTML，再用
同一套解析逻辑抽取结果。

额外能力（AI 友好 / 相关度排序）：
- 一次抓满 **30 条**（默认，多页自动翻页去重），返回「所有结果」的标题。
- 给每条结果算一个 **匹配度分数（score）**，并按相关度排序，一键找出
  **最匹配的那一条（top_match）**。
- 内置 **零依赖本地 HTTP API**（`--serve`）：任何 AI / 脚本用 HTTP 调
  `/search` 即可拿到结构化 JSON 返回值。

零运行时依赖（HTTP 模式只用 Python 标准库）；真实浏览器模式需要
`pip install "browsesearch[browser]"`（会装上 playwright 并下载 Chromium）。

用法示例
--------
    # 默认引擎 duckduckgo，真实浏览器模式，抓 30 条并按相关度排序
    python browsesearch.py "python 异步编程"

    # 只取最匹配的 3 条（按 score 排序）
    python browsesearch.py "python 异步编程" --top 3

    # 输出为 JSON（含 rank / score），方便 AI 读取
    python browsesearch.py "最佳机械键盘" -f json

    # 启动本地 HTTP API，让 AI 直接调用
    python browsesearch.py --serve --port 8731

作为库使用
----------
    from browsesearch import search

    results = search("python", engine="duckduckgo", limit=30)
    for r in results:
        print(r["rank"], r["score"], r["title"], r["url"])
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "1.1.0"

# 一个尽量「像真人桌面浏览器」的 UA，避免被搜索引擎当成裸脚本直接挡掉。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20000  # 毫秒（浏览器）/ 秒会被转换
DEFAULT_LIMIT = 30       # 默认一次抓满 30 条
MAX_PAGES = 5            # 多页翻页上限


# ---------------------------------------------------------------------------
# 轻量 DOM（纯标准库）
# ---------------------------------------------------------------------------
class Node:
    """极简 DOM 节点，仅保留我们抽取结果需要的信息。"""

    __slots__ = ("tag", "attrs", "children", "text", "parent")

    def __init__(self, tag, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []
        self.text = ""  # 该节点「自己」的文本（在子节点之前）
        self.parent = None

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    @property
    def href(self):
        return self.attrs.get("href")

    def all_text(self):
        """递归汇总整棵子树的文本。"""
        parts = [self.text]
        for c in self.children:
            parts.append(c.all_text())
        return "".join(parts)

    def find(self, tag=None, cls=None):
        """深度优先返回第一个匹配的子孙节点。"""
        for c in self.children:
            if (tag is None or c.tag == tag) and (cls is None or cls in c.classes):
                return c
            r = c.find(tag, cls)
            if r is not None:
                return r
        return None

    def find_all(self, tag=None, cls=None):
        """返回所有匹配的子孙节点（文档顺序 / 前序遍历）。"""
        out = []

        def walk(node):
            for c in node.children:
                if (tag is None or c.tag == tag) and (cls is None or cls in c.classes):
                    out.append(c)
                walk(c)

        walk(self)
        return out


class _DOMParser(HTMLParser):
    """把 HTML 解析成一棵 Node 树。"""

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        # 向后找到最近的同名开始标签，把它（连同未闭合的子节点）一起弹栈
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                break

    def handle_data(self, data):
        self.stack[-1].text += data


def parse_html(markup: str) -> Node:
    """把 HTML 字符串解析成根 Node。"""
    p = _DOMParser()
    p.feed(markup)
    p.close()
    return p.root


def _clean(text: str) -> str:
    """去掉多余空白并把 HTML 实体反转义。"""
    return html.unescape(" ".join(text.split())).strip()


# ---------------------------------------------------------------------------
# 相关度评分
# ---------------------------------------------------------------------------
def _tokenize(query: str):
    """把查询拆成「词」。中英文混排都能用：按空白/常见标点切分。"""
    q = (query or "").lower().strip()
    if not q:
        return []
    tokens = re.split(r"[\s,;，；。、]+", q)
    return [t for t in tokens if t]


def relevance_score(query: str, result: dict) -> float:
    """
    给单条结果算一个「和查询的匹配度分数」。

    规则（朴素但够用，纯标准库无依赖）：
      - 整句查询命中标题：+5；命中摘要：+1
      - 每个查询词命中标题：+2；命中摘要：+0.5
      - 查询词覆盖率（命中词数 / 总词数）：+1 * 覆盖率
      - 有摘要（信息更完整）：+0.1
    返回的 score 越高代表越相关。
    """
    tokens = _tokenize(query)
    if not tokens:
        return 0.0
    title = (result.get("title") or "").lower()
    snippet = (result.get("snippet") or "").lower()
    ql = (query or "").lower()

    score = 0.0
    matched = 0
    if ql in title:
        score += 5.0
        matched += 1
    if ql in snippet:
        score += 1.0
    for t in tokens:
        if t in title:
            score += 2.0
            matched += 1
        elif t in snippet:
            score += 0.5
            matched += 1
    # 覆盖率：避免「只命中一个词却排很高」
    score += (matched / len(tokens)) * 1.0
    if snippet:
        score += 0.1
    return round(score, 4)


def rank_results(results, query: str, sort: str = "relevance"):
    """
    给结果列表算分并排序。

    sort="relevance" → 按 score 降序（最匹配在前），并写入 rank。
    sort="engine"    → 保持搜索引擎原始顺序，rank 仍按原顺序编号。
    返回新列表（不修改入参顺序以外的引用内容，但会就地补 score/rank）。
    """
    for r in results:
        r["score"] = relevance_score(query, r)
    if sort == "relevance":
        ordered = sorted(results, key=lambda r: r.get("score", 0.0), reverse=True)
    else:
        ordered = list(results)
    for i, r in enumerate(ordered, 1):
        r["rank"] = i
    return ordered


# ---------------------------------------------------------------------------
# 搜索引擎定义
# ---------------------------------------------------------------------------
def _ddg_real_url(href: str) -> str:
    """DuckDuckGo 的结果链接经常是 //duckduckgo.com/l/?uddg=ENCODED 重定向，
    这里把真正的目标 URL 还原出来。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        q = urllib.parse.urlparse(href).query
        val = urllib.parse.parse_qs(q).get("uddg", [None])[0]
        if val:
            return urllib.parse.unquote(val)
    if href.startswith("http"):
        return href
    return ""


def _build_ddg(query: str, page: int = 0) -> str:
    # DuckDuckGo HTML 版单页即返回约 30 条；翻页需 POST，这里单页足够
    return "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)


def _build_bing(query: str, page: int = 0) -> str:
    first = 1 + 10 * page
    return "https://www.bing.com/search?q=" + urllib.parse.quote_plus(query) + f"&first={first}"


def _build_google(query: str, page: int = 0) -> str:
    start = 10 * page
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query) + f"&hl=en&start={start}"


def parse_ddg(root: Node):
    results = []
    links = root.find_all("a", "result__a")
    snippets = root.find_all(cls="result__snippet")
    for i, link in enumerate(links):
        title = _clean(link.all_text())
        url = _ddg_real_url(link.href or "")
        snippet = _clean(snippets[i].all_text()) if i < len(snippets) else ""
        if url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def parse_bing(root: Node):
    results = []
    for li in root.find_all("li", "b_algo"):
        a = li.find("a")
        if not a or not a.href or not a.href.startswith("http"):
            continue
        title = _clean(a.all_text())
        p = li.find("p")
        snippet = _clean(p.all_text()) if p else ""
        results.append({"title": title, "url": a.href, "snippet": snippet})
    return results


def parse_google(root: Node):
    results = []
    for block in root.find_all(cls="g"):
        h3 = block.find("h3")
        if not h3:
            continue
        a = block.find("a")
        if not a or not a.href or not a.href.startswith("http"):
            continue
        title = _clean(h3.all_text())
        snip = block.find(cls="VwiC3b") or block.find(cls="IsZvec")
        snippet = _clean(snip.all_text()) if snip else ""
        results.append({"title": title, "url": a.href, "snippet": snippet})
    return results


ENGINES = {
    "duckduckgo": {
        "label": "DuckDuckGo",
        "build_url": _build_ddg,
        "parse": parse_ddg,
        "needs_js": False,  # HTML 版是服务端渲染，对无头更友好
    },
    "bing": {
        "label": "Bing",
        "build_url": _build_bing,
        "parse": parse_bing,
        "needs_js": True,
    },
    "google": {
        "label": "Google",
        "build_url": _build_google,
        "parse": parse_google,
        "needs_js": True,  # 强反爬，真实浏览器收益最大
    },
}


# ---------------------------------------------------------------------------
# 抓取层：真实浏览器 / 纯 HTTP
# ---------------------------------------------------------------------------
def fetch_browser(url: str, headless: bool = True, timeout_ms: int = DEFAULT_TIMEOUT) -> str:
    """用真实（无头）Chromium 打开页面，等 JS 渲染后取回完整 HTML。"""
    from playwright.sync_api import sync_playwright  # 延迟导入，核心模式不依赖它

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent=BROWSER_UA,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            page = context.new_page()
            # domcontentloaded 比 networkidle 更稳，不会因某个跟踪请求卡死
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            # 给结果列表的 JS 一点渲染时间
            page.wait_for_timeout(900)
            return page.content()
        finally:
            browser.close()


def fetch_http(url: str, timeout_s: int = 20) -> str:
    """用标准库直接抓取搜索页 HTML（回退模式）。"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


def _browser_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _fetch(url: str, mode: str, headless: bool, timeout: int) -> str:
    """按 mode 解析策略抓取一页 HTML，browser 失败在 auto 下优雅回退 http。"""
    resolved = mode
    if mode == "auto":
        resolved = "browser" if _browser_available() else "http"
    if resolved == "browser":
        try:
            return fetch_browser(url, headless=headless, timeout_ms=timeout)
        except Exception as exc:  # 浏览器起不来？优雅回退到 HTTP
            if mode == "auto":
                sys.stderr.write(
                    f"[browsesearch] 浏览器模式失败（{exc!r}），回退到 HTTP 模式。\n"
                )
                return fetch_http(url, timeout // 1000 or 20)
            raise
    return fetch_http(url, timeout // 1000 or 20)


def _collect(query, engine, limit, mode, headless, timeout, max_pages=MAX_PAGES):
    """跨页抓取并去重，直到凑够 limit 条（或翻完 max_pages / 没有更多）。"""
    eng = ENGINES[engine]
    collected = []
    seen = set()
    page = 0
    while len(collected) < limit and page < max_pages:
        url = eng["build_url"](query, page)
        try:
            markup = _fetch(url, mode, headless, timeout)
        except Exception as exc:
            sys.stderr.write(f"[browsesearch] 第 {page + 1} 页抓取失败：{exc!r}\n")
            break
        page_results = eng["parse"](parse_html(markup))
        if not page_results:
            break  # 没有更多结果
        for r in page_results:
            u = r.get("url")
            if u and u not in seen:
                seen.add(u)
                collected.append(r)
        page += 1
    return collected


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def search(
    query: str,
    engine: str = "duckduckgo",
    limit: int = DEFAULT_LIMIT,
    mode: str = "auto",
    headless: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    sort: str = "relevance",
    top: int = None,
    max_pages: int = MAX_PAGES,
):
    """
    执行一次搜索。

    返回结果列表（每个元素含 title/url/snippet/score/rank）。

    limit : 想要的结果条数（默认 30；会跨页翻页去重凑够）。
    mode  : "auto"(默认) / "browser"(真实浏览器) / "http"(纯标准库)
    sort  : "relevance"(按匹配度降序，默认) / "engine"(搜索引擎原顺序)
    top   : 若给出，则无视 sort，按相关度取前 N 条（最匹配的 N 条）。
    """
    if engine not in ENGINES:
        raise ValueError(f"未知引擎 {engine!r}，可选：{', '.join(ENGINES)}")
    if not query or not query.strip():
        raise ValueError("query 不能为空")

    # top 需要比 limit 更多的候选才能挑出最优，故按 max 收集
    collect_limit = limit
    if top is not None and top > limit:
        collect_limit = top

    collected = _collect(query, engine, collect_limit, mode, headless, timeout, max_pages)

    # top 永远按相关度排序
    ordered = rank_results(collected, query, sort="relevance" if top else sort)

    if top is not None:
        ordered = ordered[:top]
    elif limit:
        ordered = ordered[:limit]
    return ordered


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------
def _fmt_text(results):
    if not results:
        return "（没有搜到结果）"
    lines = []
    for r in results:
        rank = r.get("rank", 0)
        score = r.get("score")
        head = f"{rank}. {r['title']}"
        if score is not None:
            head += f"  (匹配度 {score})"
        lines.append(head)
        lines.append(f"   {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_markdown(results):
    if not results:
        return "_（没有搜到结果）_"
    lines = []
    for r in results:
        rank = r.get("rank", 0)
        score = r.get("score")
        title = f"{rank}. [{r['title']}]({r['url']})"
        if score is not None:
            title += f" — 匹配度 {score}"
        lines.append(title)
        if r.get("snippet"):
            lines.append(f"   > {r['snippet']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 本地 HTTP API（零依赖，AI 可直接调用）
# ---------------------------------------------------------------------------
def _api_search(
    query,
    engine="duckduckgo",
    limit=DEFAULT_LIMIT,
    sort="relevance",
    top=None,
    mode="auto",
    headless=True,
):
    """供 HTTP 服务调用的内部封装：返回带元信息的 dict。"""
    if not query or not str(query).strip():
        raise ValueError("缺少 query 参数")
    results = search(
        str(query),
        engine=engine,
        limit=int(limit) if limit is not None else DEFAULT_LIMIT,
        mode=mode,
        headless=bool(headless),
        sort=sort,
        top=int(top) if top is not None else None,
    )
    return {
        "query": str(query),
        "engine": engine,
        "sort": sort,
        "count": len(results),
        "top_match": results[0] if results else None,
        "results": results,
    }


class _APIHandler(BaseHTTPRequestHandler):
    """极简 JSON API 处理器。"""

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/health"):
            self._send(200, {
                "status": "ok",
                "service": "browsesearch",
                "version": __version__,
                "engines": list(ENGINES),
            })
            return
        if path == "/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def g(k, d=None):
                v = qs.get(k)
                return v[0] if v else d

            try:
                out = _api_search(
                    query=g("q") or g("query"),
                    engine=g("engine", "duckduckgo"),
                    limit=g("limit", DEFAULT_LIMIT),
                    sort=g("sort", "relevance"),
                    top=g("top"),
                    mode=g("mode", "auto"),
                    headless=(g("headless", "1") not in ("0", "false", "no")),
                )
                self._send(200, out)
            except Exception as e:  # noqa: BLE001
                self._send(400, {"error": str(e)})
            return
        self._send(404, {"error": "not found", "hint": "GET /search?q=... 或 /health"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/search":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": "invalid json: " + str(e)})
            return
        try:
            out = _api_search(
                query=data.get("query") or data.get("q"),
                engine=data.get("engine", "duckduckgo"),
                limit=data.get("limit", DEFAULT_LIMIT),
                sort=data.get("sort", "relevance"),
                top=data.get("top"),
                mode=data.get("mode", "auto"),
                headless=bool(data.get("headless", True)),
            )
            self._send(200, out)
        except Exception as e:  # noqa: BLE001
            self._send(400, {"error": str(e)})

    def log_message(self, *a):  # 静默日志
        pass


def serve(host: str = "127.0.0.1", port: int = 8731):
    """启动本地 HTTP API 服务（阻塞运行，Ctrl+C 退出）。"""
    httpd = ThreadingHTTPServer((host, port), _APIHandler)
    actual_port = httpd.server_address[1]
    print(f"[browsesearch] HTTP API 已启动 → http://{host}:{actual_port}")
    print(f"[browsesearch] 示例： curl 'http://{host}:{actual_port}/search?q=python&limit=30'")
    print("[browsesearch] 按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return actual_port


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="browsesearch",
        description="真正从浏览器里联网搜索：默认驱动真实 Chromium 渲染结果，按匹配度排序。",
    )
    parser.add_argument("query", nargs="?", help="搜索关键词")
    parser.add_argument(
        "-e", "--engine", default="duckduckgo",
        choices=list(ENGINES), help="搜索引擎（默认 duckduckgo）",
    )
    parser.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"返回条数（默认 {DEFAULT_LIMIT}，会跨页去重凑够）")
    parser.add_argument(
        "-f", "--format", default="text",
        choices=["text", "json", "md"], help="输出格式",
    )
    parser.add_argument(
        "--mode", default="auto", choices=["auto", "browser", "http"],
        help="抓取模式：auto(默认) / browser(真实浏览器) / http(纯标准库)",
    )
    parser.add_argument("--no-headless", action="store_true", help="浏览器显示窗口（非无头）")
    parser.add_argument("--open", action="store_true", help="用系统默认浏览器打开结果")
    parser.add_argument("--list-engines", action="store_true", help="列出支持的引擎后退出")
    parser.add_argument("--version", action="version", version=f"browsesearch {__version__}")
    # —— AI 友好 / 相关度 ——
    parser.add_argument("--sort", default="relevance",
                        choices=["relevance", "engine"],
                        help="排序：relevance(按匹配度降序,默认) / engine(原始顺序)")
    parser.add_argument("--top", type=int, default=None,
                        help="只返回最匹配的前 N 条（按相关度）")
    parser.add_argument("--all", action="store_true",
                        help="不限制条数，尽量多抓（最多翻 MAX_PAGES 页）")
    # —— 本地 HTTP API ——
    parser.add_argument("--serve", action="store_true",
                        help="启动本地 HTTP API 服务（AI 调用入口）")
    parser.add_argument("--port", type=int, default=8731, help="serve 模式端口")
    parser.add_argument("--host", default="127.0.0.1", help="serve 模式监听地址")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_engines:
        for key, eng in ENGINES.items():
            print(f"  {key:<10} {eng['label']}"
                  f"{'  (需 JS 渲染)' if eng['needs_js'] else ''}")
        return 0

    # —— 启动 HTTP API 服务 ——
    if args.serve:
        serve(host=args.host, port=args.port)
        return 0

    if not args.query:
        parser.error("请提供搜索关键词，例如：browsesearch \"python 教程\"")

    limit = 9999 if args.all else args.limit
    results = search(
        args.query,
        engine=args.engine,
        limit=limit,
        mode=args.mode,
        headless=not args.no_headless,
        sort=args.sort,
        top=args.top,
    )

    if args.format == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif args.format == "md":
        print(_fmt_markdown(results))
    else:
        print(_fmt_text(results))

    if args.open and results:
        target = results[0]["url"]
        sys.stderr.write(f"[browsesearch] 正在用默认浏览器打开：{target}\n")
        webbrowser.open(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
