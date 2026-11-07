"""联网搜索模块（Search-RAG 检索层）

检索链路（LLM friendly 优先，逐级降级）：
1. 首选 Tavily Search API（专为 LLM/RAG 设计，需 API Key，未配置自动跳过）
2. 次选 s.jina.ai（免费免 Key 的 LLM 友好搜索清洗器，部分网络环境不可达时自动跳过）
3. 兜底 cn.bing.com 网页搜索（免 Key，抓取结果块的标题+摘要）

本地清洗（对三个来源通用）：剥元信息 → 废话黑名单 → 切句 → 关键词命中筛选 → 硬性截断。
任何失败（网络/限流/空结果）一律返回 None，调用方降级为"无资料正常回复"，不阻塞主流程。
"""
import asyncio
import html
import re
import time
from urllib.parse import quote

import aiohttp

from part_record import log

TAVILY_URL = "https://api.tavily.com/search"
JINA_URL = "https://s.jina.ai/{}"
BING_URL = "https://cn.bing.com/search?q={}"
DEFAULT_MAX_CHARS = 300
TIMEOUT = 20
# 整次检索的总时长预算（秒）：降级查询链 × 多来源的重试轮次多，无总超时会拖住消息主循环
TOTAL_TIMEOUT = 45
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
_JINA_HEADERS = {"Accept": "text/plain", "User-Agent": "QQ-BOT/1.0"}

# 搜索摘要高频废话（SEO/导航/免责声明类），命中整句剔除
_STOP_PHRASES = (
    "为您提供", "欢迎访问", "欢迎光临", "点击进入", "点击查看", "点击下载",
    "ICP备", "免责声明", "版权所有", "扫一扫", "关注公众号", "下载APP",
    "下载App", "手机客户端", "未经许可", "不得转载", "cookie", "Cookie",
    "您的位置",
)

# 必应摘要开头的相对/绝对日期前缀，如 "2 天之前 · "、"2021年10月11日 · "
_DATE_PREFIX = re.compile(
    r"^(\d+\s*(天|小时|分钟|周|个月)之前|\d{4}年\d{1,2}月\d{1,2}日)\s*[·\-—]\s*"
)

# 查询词里的功能性尾段（对搜索引擎无区分度，整段剔除）
_GENERIC_SEGMENTS = {"名称", "时间", "日期", "介绍", "一览", "是什么", "是谁", "怎么样", "多少"}
# 词段开头的修饰词（必应遇冷僻组合直接崩成单字字典页，剥掉后重试）
_LEADING_QUALIFIERS = ("首次", "第一次", "最新", "最近", "近期", "全新")


def _fallback_queries(query):
    """生成降级查询链：原词 → 去功能尾段 → 段内剥修饰词 → 只留前两段。去重保序。
    必应抓取接口对 3 段以上或含冷僻修饰词的查询会退化成单字字典页，必须简化重试。"""
    segs = [s for s in re.split(r"\s+", query.strip()) if s]
    candidates = [" ".join(segs)] if segs else []
    core = [s for s in segs if s not in _GENERIC_SEGMENTS]
    if core and core != segs:
        candidates.append(" ".join(core))
    base = core if core else segs
    stripped = []
    for s in base:
        for q in _LEADING_QUALIFIERS:
            if s.startswith(q) and len(s) > len(q) + 1:
                s = s[len(q):]
                break
        stripped.append(s)
    if stripped and stripped != base:
        candidates.append(" ".join(stripped))
    if len(stripped) > 2:
        candidates.append(" ".join(stripped[:2]))
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _query_keywords(query):
    """返回词段列表，每段是 {整段, 二字滑窗...} 的变体集合。英文/数字整段算一个特征。全小写。
    命中按"段"计：同一词段的多个滑窗命中只算 1 次，防止"终末地"一个词堆出 3 个命中蒙混过关。"""
    pieces = []
    for tok in re.split(r"[\s，。？！?!,.、：:;；\"'“”]+", query):
        for piece in re.findall(r"[0-9a-zA-Z]+|[一-鿿]+", tok):
            if len(piece) < 2:
                continue
            piece = piece.lower()
            variants = {piece}
            if not piece.isascii() and len(piece) >= 3:
                variants.update(piece[i:i + 2] for i in range(len(piece) - 1))
            pieces.append(variants)
    return pieces


def _strip_html(x):
    return html.unescape(re.sub(r"<[^>]+>", "", x or "")).strip()


async def _tavily_search(query, api_key, timeout):
    """Tavily Search API：LLM 友好的托管搜索，content 为正文级摘录，信息密度远高于网页 snippet。"""
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                TAVILY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
    except Exception:
        return None
    parts = []
    for r in data.get("results", []):
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        if title:
            parts.append(title + "。")
        if content:
            parts.append(content)
    return "\n".join(parts) or None


def _bing_text(body, max_blocks=5):
    """解析必应搜索结果块：标题 + 摘要（去日期前缀），拼成待清洗文本。"""
    blocks = re.findall(r'<li class="b_algo".*?</li>', body, re.S)
    parts = []
    for b in blocks[:max_blocks]:
        t = re.search(r"<h2>.*?>(.*?)</a>", b, re.S)
        p = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        title = _strip_html(t.group(1) if t else "")
        snippet = _strip_html(p.group(1) if p else "")
        snippet = _DATE_PREFIX.sub("", snippet)
        if title:
            parts.append(title + "。")
        if snippet:
            parts.append(snippet)
    return "\n".join(parts)


def _jina_text(body):
    """剥掉 Jina 返回里的标题/链接/时间等元信息行，只留正文。"""
    keep = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(\[\d+\]\s*)?(Title|URL|Url|Published|Markdown|Description)\b", line):
            continue
        if line.startswith(("http://", "https://")):
            continue
        keep.append(line)
    return "\n".join(keep)


async def _fetch(url, headers, timeout):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.text()
    except Exception:
        return None


def _clean_and_pick(text, query, max_chars):
    """切句 → 废话过滤 → 关键词命中筛选 → 硬性截断；全落空时降级取前几句非废话。"""
    sentences = [re.sub(r"\s+", " ", s.strip())
                 for s in re.split(r"(?<=[。！？!?\n])", text) if s.strip()]
    fresh = []
    for s in sentences:
        if len(s) < 8:
            continue
        if any(p in s for p in _STOP_PHRASES):
            continue
        fresh.append(s)

    pieces = _query_keywords(query)

    def hits(s):
        low = s.lower().replace(" ", "")
        # 按词段计命中：一段内任意变体命中即算该段命中 1 次
        return sum(1 for variants in pieces if any(v in low for v in variants))

    # 多词段查询要求至少命中 2 个不同词段，防止"2026"这类泛词或单个词段放进整页无关内容；
    # 全落空时返回空串（宁可无资料降级，也不喂误导性内容给主模型）
    threshold = 2 if len(pieces) >= 2 else 1
    picked = [s for s in fresh if hits(s) >= threshold]
    if not picked:
        return ""

    out, total = [], 0
    for s in picked:
        if total + len(s) > max_chars:
            remain = max_chars - total
            if remain >= 30:
                out.append(s[:remain] + "…")
            break
        out.append(s)
        total += len(s)
    return "".join(out)


async def search_web(query, max_chars=DEFAULT_MAX_CHARS, timeout=TIMEOUT, tavily_key="", total_timeout=TOTAL_TIMEOUT):
    """返回清洗后的参考资料纯文本；失败返回 None（调用方降级为无资料回复）。
    每个降级查询词按 Tavily（有 Key 时）→ Jina → 必应 顺序逐源尝试：
    源返回空、或清洗后判定为无关内容，都继续尝试下一来源，而不是拿到非空正文就定死。
    total_timeout 是整次检索的总预算（含所有降级查询词与来源），单次请求超时会被剩余预算截断。"""
    query = (query or "").strip()
    if not query:
        return None
    tavily_key = (tavily_key or "").strip()
    deadline = time.monotonic() + total_timeout
    for q in _fallback_queries(query):
        for source in ("Tavily", "Jina", "必应"):
            if source == "Tavily" and not tavily_key:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log(f"[搜索] 检索超出总预算 {total_timeout:g}s，降级为无资料回复")
                return None
            per_timeout = max(1, min(timeout, remaining))
            if source == "Tavily":
                body = await _tavily_search(q, tavily_key, per_timeout) or ""
            elif source == "Jina":
                raw = await _fetch(JINA_URL.format(quote(q)), _JINA_HEADERS, per_timeout)
                body = _jina_text(raw) if raw else ""
            else:
                raw = await _fetch(BING_URL.format(quote(q)), _UA, per_timeout)
                body = _bing_text(raw) if raw else ""
            if not body.strip():
                continue
            result = _clean_and_pick(body, q, max_chars)
            if result:
                if q != query:
                    log(f"[搜索] 原查询无有效结果，已改用简化词检索: {q}")
                log(f"[搜索] 检索来源: {source}")
                return result
            log(f"[搜索] {source} 内容清洗后无关，尝试下一来源")
    return None
