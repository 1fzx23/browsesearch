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

import os
import sys
import threading
import urllib.parse
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
