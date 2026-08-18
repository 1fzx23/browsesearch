# 🔎 browsesearch

> 真正从浏览器里联网搜索的小工具 —— 默认驱动一个真实的 Chromium 无头浏览器去打开搜索引擎、等 JS 渲染出结果，再读取 DOM 抽取标题/链接/摘要。没有 API key，不被轻易反爬挡掉。

这是「每两周一个小工具」系列的 **第 4 个**，难度 **L4（专家）**：它涉及真实浏览器编排、多引擎 HTML 解析与自动回退，比前三个工具更硬核一些。

---

## 为什么不是「调搜索 API」？

很多「搜索工具」只是封装了一个搜索服务商的 HTTP API —— 需要 key、有配额、还要联网授权。
`browsesearch` 走另一条路：

- **真实渲染**：用 [Playwright](https://playwright.dev/) 驱动 Chromium 打开搜索页，JavaScript 完整执行，拿到的 DOM 才是真人看到的那份。
- **绕过反爬**：带真实桌面 UA + 视口，比裸脚本/HTTP 请求更难被人机校验拦截（Google 这类反爬严格的尤其明显）。
- **零 Key**：不依赖任何搜索服务商 API key。
- **自动回退**：环境里没装 Playwright？自动回退到纯 Python 标准库 HTTP 模式，照样能搜。

---

## 安装

```bash
# 真实浏览器模式（默认），会顺带下载 Chromium
pip install "browsesearch[browser]"

# 只想用纯标准库 HTTP 回退模式
pip install browsesearch
```

> 也可以不安装，直接 `python browsesearch.py "关键词"` 运行单文件。

---

## 命令行用法

```bash
# 默认 DuckDuckGo + 真实浏览器（自动）
python browsesearch.py "python 异步编程"

# 指定引擎
python browsesearch.py "最佳机械键盘" -e bing
python browsesearch.py "rust vs go"   -e google

# 输出为 JSON / Markdown
python browsesearch.py "gRPC 教程" -f json
python browsesearch.py "gRPC 教程" -f md

# 限制条数
python browsesearch.py "天气" -n 5

# 强制纯 HTTP 模式（不需要浏览器）
python browsesearch.py "新闻" --mode http

# 用系统默认浏览器打开第一条结果
python browsesearch.py "天气" --open

# 让浏览器以窗口形式出现（非无头）
python browsesearch.py "python" --no-headless

# 查看支持的引擎
python browsesearch.py --list-engines
```

### 参数一览

| 参数 | 说明 |
| --- | --- |
| `query` | 搜索关键词（位置参数） |
| `-e, --engine` | `duckduckgo`(默认) / `bing` / `google` |
| `-n, --limit` | 返回条数，默认 10 |
| `-f, --format` | `text`(默认) / `json` / `md` |
| `--mode` | `auto`(默认) / `browser`(真实浏览器) / `http`(纯标准库) |
| `--no-headless` | 浏览器显示窗口 |
| `--open` | 用系统默认浏览器打开结果 |
| `--list-engines` | 列出引擎后退出 |

---

## 作为库使用

```python
from browsesearch import search

results = search("python", engine="duckduckgo", limit=5)
for r in results:
    print(r["title"])
    print(r["url"])
    print(r["snippet"])
```

`search()` 返回的每个元素是 `{"title": str, "url": str, "snippet": str}`。

---

## 支持的搜索引擎

| 引擎 | 说明 |
| --- | --- |
| **DuckDuckGo**（默认） | HTML 版服务端渲染，对无头浏览器最友好 |
| **Bing** | 结构清晰，渲染后能稳定抽取 |
| **Google** | 反爬最严，真实浏览器加成最大；DOM 偶尔变动，解析为尽力而为 |

---

## 工作原理（简图）

```
关键词
  │
  ▼
┌─────────────── 真实浏览器模式（默认，需 Playwright）──────────────┐
│  Chromium 无头打开搜索引擎页 → 等 JS 渲染 → 取回完整 HTML        │
└────────────────────────────────────────────────────────────────┘
  │  或
  ▼  HTTP 回退模式（纯标准库 urllib，真实桌面 UA）
搜索引擎返回 HTML
  │
  ▼
轻量 DOM 解析（纯标准库 HTMLParser）→ [{title, url, snippet}, ...]
```

解析层用了一个零依赖的迷你 DOM（`html.parser` 之上），没有任何第三方解析库。

---

## 测试

```bash
python tests/test_browsesearch.py     # 零依赖直接跑
# 或
python -m pytest tests/
```

测试覆盖：三个引擎的 HTML 解析、URL 编码、DuckDuckGo 重定向链接解码，
以及 **真实 Chromium 管线** —— 起一个本地服务器把 fixture 喂给无头浏览器，验证「渲染 → 取回 → 解析」整条链路。

---

## 注意 & 礼仪

- 本工具用于个人学习/效率场景，请遵守目标网站的 `robots.txt` 与服务条款，不要高频滥用。
- 真实浏览器模式需要本机能联网（它真的要去访问搜索引擎）。
- Google 的 DOM 结构变动较频繁，若解析失效，优先切到 DuckDuckGo / Bing。

---

## License

[MIT](./LICENSE) © 1fzx23
