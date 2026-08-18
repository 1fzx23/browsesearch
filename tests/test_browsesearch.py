#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browsesearch 的测试。

- 解析测试：直接对本地 fixture 的 HTML 跑解析逻辑（不需要网络）。
- 真实浏览器管线测试：用本地 http 服务器把 fixture 起起来，再让真实
  Chromium 去「打开」它，验证「浏览器渲染 → 取回 HTML → 解析」整条链路
  （同样不依赖外网，但确实走了真实浏览器）。

运行方式：
    python tests/test_browsesearch.py        # 零依赖直接跑
    python -m pytest tests/                  # 用 pytest 跑
"""

import json as _json
import os
import sys
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

# 让 import browsesearch 能找到仓库根目录的模块
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import browsesearch as bs  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    with open(os.path.join(FIX, name), "r", encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------------------
# 纯解析 / 工具函数测试（不需要浏览器）
# --------------------------------------------------------------------------
def test_parse_ddg():
    root = bs.parse_html(_fixture("duckduckgo.html"))
    results = bs.parse_ddg(root)
    assert len(results) == 3
    assert results[0]["title"] == "Python 异步编程完全指南"
    assert results[0]["url"] == "https://example.com/python-async"
    assert "asyncio" in results[0]["snippet"]
    # 第二条是官方文档
    assert results[1]["url"] == "https://docs.python.org/3/library/asyncio.html"


def test_parse_bing():
    root = bs.parse_html(_fixture("bing.html"))
    results = bs.parse_bing(root)
    assert len(results) == 3
    assert results[0]["title"] == "2026 年最佳机械键盘推荐"
    assert results[0]["url"] == "https://example.com/bing-best-keyboard"
    assert "性价比" in results[0]["snippet"]


def test_parse_google():
    root = bs.parse_html(_fixture("google.html"))
    results = bs.parse_google(root)
    assert len(results) == 3
    assert results[0]["title"] == "gRPC 是什么？一篇搞懂"
    assert results[0]["url"] == "https://example.com/grpc-intro"
    assert "Protobuf" in results[0]["snippet"]


def test_build_url():
    u = bs._build_ddg("python 教程")
    assert "q=" in u and "python" in u and "%20" in u or "+" in u
    assert urllib.parse.quote_plus("python 教程") in u
    # bing / google 也应正确编码
    assert "python" in bs._build_bing("python")
    assert "python" in bs._build_google("python")


def test_ddg_real_url():
    assert bs._ddg_real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com") == "https://example.com"
    assert bs._ddg_real_url("https://example.com/direct") == "https://example.com/direct"
    assert bs._ddg_real_url("") == ""


# --------------------------------------------------------------------------
# 真实浏览器管线测试（用本地 fixture 走 Chromium）
# --------------------------------------------------------------------------
def _serve_fixture_once(filename):
    """起一个只服务 fixtures 目录的本地服务器，返回 base url，并用事件控制退出。"""
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=FIX, **k)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}/{filename}"


def test_browser_pipeline_ddg():
    if not bs._browser_available():
        print("[skip] Playwright 不可用，跳过真实浏览器管线测试")
        return
    httpd, url = _serve_fixture_once("duckduckgo.html")
    try:
        markup = bs.fetch_browser(url, headless=True, timeout_ms=30000)
        results = bs.parse_ddg(bs.parse_html(markup))
        assert len(results) == 3
        assert results[0]["url"] == "https://example.com/python-async"
    finally:
        httpd.shutdown()


def test_browser_pipeline_google():
    if not bs._browser_available():
        print("[skip] Playwright 不可用，跳过真实浏览器管线测试")
        return
    httpd, url = _serve_fixture_once("google.html")
    try:
        markup = bs.fetch_browser(url, headless=True, timeout_ms=30000)
        results = bs.parse_google(bs.parse_html(markup))
        assert len(results) == 3
        assert results[0]["url"] == "https://example.com/grpc-intro"
    finally:
        httpd.shutdown()


def test_relevance_top_match():
    """相关度排序：最匹配查询的结果应排第一，且 score 更高。"""
    results = [
        {"title": "完全无关的话题", "url": "u1", "snippet": "和 Python 没关系"},
        {"title": "Python 异步编程入门", "url": "u2", "snippet": "讲解 asyncio 异步编程"},
    ]
    ordered = bs.rank_results(results, "python 异步编程", sort="relevance")
    assert ordered[0]["url"] == "u2"
    assert ordered[0]["score"] > ordered[1]["score"]
    # 每条都应带 score / rank
    assert all("score" in r and "rank" in r for r in ordered)


def test_parse_many_ddg():
    """30+ 条 fixture：能解析出至少 30 条，且标题齐全不重复。"""
    root = bs.parse_html(_fixture("duckduckgo_many.html"))
    results = bs.parse_ddg(root)
    assert len(results) >= 30, f"只解析出 {len(results)} 条，需 ≥30"
    titles = [r["title"] for r in results]
    assert all(titles), "存在空标题"
    assert len(set(titles)) == len(titles), "标题有重复"
    assert all(r["url"].startswith("http") for r in results)


def test_relevance_on_many_fixture():
    """在 40 条 fixture 上按相关度排序，最匹配的应排在最前。"""
    root = bs.parse_html(_fixture("duckduckgo_many.html"))
    results = bs.parse_ddg(root)
    ordered = bs.rank_results(results, "python 异步编程", sort="relevance")
    assert ordered[0]["title"].startswith("Python 异步编程")
    assert ordered[0]["rank"] == 1


def test_http_server():
    """本地 HTTP API：GET/POST /search 返回结构化 JSON（含 rank/score）。"""
    fake = [
        {"title": "Python 异步编程指南", "url": "https://x.com/1", "snippet": "asyncio", "score": 8.0, "rank": 1},
        {"title": "无关结果", "url": "https://x.com/2", "snippet": "随便", "score": 0.0, "rank": 2},
    ]
    orig = bs.search
    bs.search = lambda *a, **k: fake  # 避免真实联网
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), bs._APIHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # GET
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/search?q=python&limit=10"
        ) as r:
            data = _json.loads(r.read())
        assert data["count"] == 2
        assert data["query"] == "python"
        assert data["top_match"]["url"] == "https://x.com/1"
        assert "rank" in data["results"][0] and "score" in data["results"][0]

        # POST
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/search",
            data=_json.dumps({"query": "python", "limit": 5}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            data2 = _json.loads(r.read())
        assert data2["count"] == 2
    finally:
        bs.search = orig
        httpd.shutdown()


# --------------------------------------------------------------------------
# 轻量运行器（无 pytest 时也能跑）
# --------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {fn.__name__}: {e!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
