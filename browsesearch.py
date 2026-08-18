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

零运行时依赖（HTTP 模式只用 Python 标准库）；真实浏览器模式需要
`pip install "browsesearch[browser]"`（会装上 playwright 并下载 Chromium）。

用法示例
--------
    # 默认引擎 duckduckgo，真实浏览器模式（自动）
    python browsesearch.py "python 异步编程"

    # 指定引擎 + 输出 JSON
    python browsesearch.py "最佳机械键盘" -e bing -f json

    # 强制纯 HTTP 模式（不需要浏览器）
    python browsesearch.py "rust vs go" --mode http

    # 直接打开第一条结果（你的真实默认浏览器）
    python browsesearch.py "天气" --open

作为库使用
----------
    from browsesearch import search
    for r in search("python", engine="duckduckgo", limit=5):
        print(r["title"], r["url"])
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser

__version__ = "1.0.0"

# 一个尽量「像真人桌面浏览器」的 UA，避免被搜索引擎当成裸脚本直接挡掉。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 20000  # 毫秒（浏览器）/ 秒会被转换


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


def _build_ddg(query: str) -> str:
    return "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)


def _build_bing(query: str) -> str:
    return "https://www.bing.com/search?q=" + urllib.parse.quote_plus(query)


def _build_google(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query) + "&hl=en"


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


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------
def search(
    query: str,
    engine: str = "duckduckgo",
    limit: int = 10,
    mode: str = "auto",
    headless: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
):
    """
    执行一次搜索，返回结果列表（每个元素含 title/url/snippet）。

    mode:
      "auto"    → 有 Playwright 就用真实浏览器，否则回退 HTTP（默认）
      "browser" → 强制真实浏览器（没有就抛错）
      "http"    → 强制纯 HTTP 回退
    """
    if engine not in ENGINES:
        raise ValueError(f"未知引擎 {engine!r}，可选：{', '.join(ENGINES)}")
    eng = ENGINES[engine]
    url = eng["build_url"](query)

    resolved = mode
    if mode == "auto":
        resolved = "browser" if _browser_available() else "http"

    if resolved == "browser":
        try:
            markup = fetch_browser(url, headless=headless, timeout_ms=timeout)
        except Exception as exc:  # 浏览器起不来？优雅回退到 HTTP
            if mode == "auto":
                sys.stderr.write(
                    f"[browsesearch] 浏览器模式失败（{exc!r}），回退到 HTTP 模式。\n"
                )
                markup = fetch_http(url, timeout // 1000 or 20)
            else:
                raise
    else:
        markup = fetch_http(url, timeout // 1000 or 20)

    results = eng["parse"](parse_html(markup))
    return results[:limit]


# ---------------------------------------------------------------------------
# 输出格式化
# ---------------------------------------------------------------------------
def _fmt_text(results):
    if not results:
        return "（没有搜到结果）"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        lines.append(f"   {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _fmt_markdown(results):
    if not results:
        return "_（没有搜到结果）_"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['title']}]({r['url']})")
        if r.get("snippet"):
            lines.append(f"   > {r['snippet']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    parser = argparse.ArgumentParser(
        prog="browsesearch",
        description="真正从浏览器里联网搜索：默认驱动真实 Chromium 渲染结果。",
    )
    parser.add_argument("query", nargs="?", help="搜索关键词")
    parser.add_argument(
        "-e", "--engine", default="duckduckgo",
        choices=list(ENGINES), help="搜索引擎（默认 duckduckgo）",
    )
    parser.add_argument("-n", "--limit", type=int, default=10, help="返回条数（默认 10）")
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
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_engines:
        for key, eng in ENGINES.items():
            print(f"  {key:<10} {eng['label']}"
                  f"{'  (需 JS 渲染)' if eng['needs_js'] else ''}")
        return 0

    if not args.query:
        parser.error("请提供搜索关键词，例如：browsesearch \"python 教程\"")

    results = search(
        args.query,
        engine=args.engine,
        limit=args.limit,
        mode=args.mode,
        headless=not args.no_headless,
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
