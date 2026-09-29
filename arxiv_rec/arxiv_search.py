"""arXiv 检索: 官方 Atom API + RSS 公告, 解析成 Candidate 列表。

只用 arXiv 的官方接口, 一个网页都不抓:

* **Atom API** (``export.arxiv.org/api/query``) —— 唯一的检索通道。一条检索式
  **一次请求**就能拿回最多 2000 条 (实测 cond-mat 一次 3.8MB / 2000 条), 所以这里
  根本不翻页。翻页是 HTTP 429 的主要来源: 20 条检索式 × 每页 200 × 3 页 = 60 次
  背靠背请求, 每次都在 arXiv 的限流窗口里再挤一下。
* **RSS** (``rss.arxiv.org/rss/<category>``) —— 每个分类当天的新公告, 一次请求拿全,
  连检索式都不需要。用来兜住"今天刚出的论文" —— 检索式是按你已有文献生成的,
  没覆盖到的新方向不会出现在结果里。

**不再抓网页搜索页** (``arxiv.org/search``)。三条理由: 那是未公开的页面结构, 改了
就得跟着改正则; arXiv 明确请求不要爬; 而且它对共享出口 IP 的限流比 API 更严, 还
不认 ``cat:`` 前缀 (分类过滤会静默失效, 见 ``_api_search_query``)。详情页
(``/abs/``) 同样不抓 —— 同样的信息 Atom API 用 ``id_list=`` 一次能批量拿一批。

请求间隔由 ``utils.GLOBAL_LIMITER`` 统一控制, 这里**不再自己 sleep**: 每个通道各睡
各的挡不住并发, 加起来照样能在限流窗口里打出七八个请求。
"""

from __future__ import annotations

import html as html_mod
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import Candidate
from .utils import (DiskCache, build_session, clean_text, dedupe_preserve,
                    extract_arxiv_id, http_get, log, norm_doi, probe_route,
                    set_global_interval, truncate)

EXPORT_URL = "http://export.arxiv.org/api/query"
RSS_URL = "https://rss.arxiv.org/rss/%s"
ABS_URL = "https://arxiv.org/abs/%s"

# arXiv API 的硬上限: max_results 超过 2000 会被拒绝 (或静默截断), 所以钳住。
MAX_API_RESULTS = 2000

# id_list 批量查询一次带多少个 ID。官方没给硬限制, 但 URL 长度和响应体积都要顾,
# 100 是稳妥值 (实测 100 个 ID 的响应约 700KB)。
ID_BATCH = 100


# --------------------------------------------------------------------------
# 通道 1: 官方 Atom API (唯一的检索通道)
# --------------------------------------------------------------------------
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def parse_atom(xml_text: str, query: str = "") -> List[Candidate]:
    """解析 export.arxiv.org 的 Atom 响应。"""
    out: List[Candidate] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        log("Atom 解析失败: %s" % exc, "warn")
        return out

    for entry in root.findall("a:entry", _ATOM_NS):
        def _text(tag: str, ns: str = "a") -> str:
            node = entry.find("%s:%s" % (ns, tag), _ATOM_NS)
            return clean_text(node.text or "") if node is not None else ""

        raw_id = _text("id")
        arxiv_id = extract_arxiv_id(raw_id)
        title = _text("title")
        if not arxiv_id or not title:
            continue

        authors = []
        for a in entry.findall("a:author", _ATOM_NS):
            nm = a.find("a:name", _ATOM_NS)
            if nm is not None and nm.text:
                authors.append(clean_text(nm.text))

        published = updated = None
        for tag, target in (("published", "published"), ("updated", "updated")):
            node = entry.find("a:%s" % tag, _ATOM_NS)
            if node is not None and node.text:
                try:
                    dt = datetime.strptime(node.text.strip()[:19], "%Y-%m-%dT%H:%M:%S")
                    if target == "published":
                        published = dt
                    else:
                        updated = dt
                except Exception:
                    pass

        cats = []
        prim = entry.find("arxiv:primary_category", _ATOM_NS)
        if prim is not None:
            term = prim.get("term")
            if term:
                cats.append(term)
        for c in entry.findall("a:category", _ATOM_NS):
            term = c.get("term")
            if term and term not in cats:
                cats.append(term)

        comment_node = entry.find("arxiv:comment", _ATOM_NS)
        journal_node = entry.find("arxiv:journal_ref", _ATOM_NS)
        doi_node = entry.find("arxiv:doi", _ATOM_NS)

        out.append(Candidate(
            arxiv_id=arxiv_id,
            title=title,
            abstract=_text("summary"),
            authors=authors,
            published=published,
            updated=updated,
            primary_category=cats[0] if cats else "",
            categories=cats,
            comments=clean_text(comment_node.text) if comment_node is not None and comment_node.text else "",
            journal_ref=clean_text(journal_node.text) if journal_node is not None and journal_node.text else "",
            doi=norm_doi(doi_node.text) if doi_node is not None and doi_node.text else "",
            source_queries=[query] if query else [],
        ))
    return out


_RE_API_TOKEN = re.compile(r'"[^"]*"|\S+')
_RE_API_FIELD = re.compile(
    r"^(?:all|ti|abs|au|cat|co|jr|rn|id|doi|abstract|title|author|comment|"
    r"journal_ref|report_no|acm_class|msc_class|primary_category):", re.I)
_API_OPS = ("AND", "OR", "ANDNOT")

# 界面「限定分类」那组复选框的候选。按大类分组, 每组内部按常用程度排。
#
# 为什么是**固定**列表而不是从文献库里推: 索引里只存了标题/作者/摘要/DOI 这些,
# 没有存 arXiv 分类 (读 PDF 本来也读不出分类), 想推也推不出来。而且这里列的
# 是 arXiv 的**投稿分类**, 是给检索用的; 用户库里的论文属于哪个分类, 跟"想找
# 哪个分类的新论文"本来就是两件事。
CATEGORY_CHOICES: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("凝聚态 / 统计物理", [
        ("cond-mat.str-el", "强关联电子"),
        ("cond-mat.stat-mech", "统计力学"),
        ("cond-mat.supr-con", "超导"),
        ("cond-mat.quant-gas", "冷原子气体"),
        ("cond-mat.mes-hall", "介观与拓扑"),
        ("cond-mat.dis-nn", "无序系统"),
        ("cond-mat.mtrl-sci", "材料科学"),
    ]),
    ("量子 / 高能 / 数学物理", [
        ("quant-ph", "量子物理"),
        ("hep-th", "高能理论"),
        ("hep-lat", "格点场论"),
        ("nucl-th", "核理论"),
        ("math-ph", "数学物理"),
    ]),
    ("计算 / 机器学习", [
        ("physics.comp-ph", "计算物理"),
        ("cs.LG", "机器学习"),
        ("cs.AI", "人工智能"),
    ]),
]

# 平铺出来的全集, 校验配置里存的分类用
ALL_CATEGORIES: List[str] = [code for _g, items in CATEGORY_CHOICES
                             for code, _label in items]


def _api_search_query(query: str,
                      categories: Optional[List[str]] = None) -> str:
    """把一条检索式转成 arXiv API 的 ``search_query``。

    关键是**每个词都要显式加字段前缀, 并且显式写 AND**。实测:

      * ``all:sign problem AND cat:cond-mat.str-el`` -> 67 万条, 前 6 条里 5 条
        根本不在该分类 —— 裸词串后面跟的 ``cat:`` 几乎不生效, 括号也救不了
        (加不加括号结果一模一样, 所以这不是括号的锅);
      * ``all:sign AND all:problem AND (cat:cond-mat.str-el OR cat:quant-ph)``
        -> 1082 条, 命中率 100%。

    也就是说 arXiv 的解析器遇到"字段前缀 + 一串裸词"时会退化成宽松匹配。所以
    这里逐词展开, 顺带让多个分类的 OR 真正起作用 (不展开的话 AND/OR 的优先级
    会把分类条件吃掉)。

    已经带字段前缀的词、以及 ``"引号短语"`` 原样保留; AND/OR/ANDNOT 照抄,
    缺运算符的地方补 AND。
    """
    parts: List[str] = []
    prev_is_term = False
    for tok in _RE_API_TOKEN.findall(query or ""):
        up = tok.upper()
        if up in _API_OPS:
            if prev_is_term:
                parts.append(up)
                prev_is_term = False
            continue
        if prev_is_term:
            parts.append("AND")
        parts.append(tok if _RE_API_FIELD.match(tok) else "all:%s" % tok)
        prev_is_term = True
    while parts and parts[-1] in _API_OPS:   # 收尾的悬空运算符会让整个检索式报错
        parts.pop()
    expr = " ".join(parts)
    if categories:
        cat_expr = " OR ".join("cat:%s" % c for c in categories)
        # 整个检索式加括号: 否则 OR 的优先级会让分类条件只作用到最后一个分支
        # (实测 ti:x OR abs:y AND (cat:...) 比加括号多出 15% 的越界结果)
        expr = "(%s) AND (%s)" % (expr, cat_expr) if expr else cat_expr
    return expr


def search_api(
    session: Any,
    query: str,
    max_results: int = 200,
    cache: Optional[DiskCache] = None,
    delay: float = 3.0,
    retries: int = 3,
    categories: Optional[List[str]] = None,
    sort_by: str = "relevance",
) -> Optional[List[Candidate]]:
    """用官方 Atom API 跑一条检索式。返回 None 表示请求失败。

    **一次请求拿完, 不翻页。** 这是这次改造的核心: 翻页每多一页就多一次背靠背
    请求, 而 ``max_results`` 官方允许到 2000, 一条检索式要的结果远没那么多。

    ``delay`` 只作为重试的退避基数; 请求之间的间隔由全局闸门统一保证。
    """
    want = max(1, min(int(max_results), MAX_API_RESULTS))
    search_query = _api_search_query(query, categories)
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": want,
        "sortBy": sort_by,
        "sortOrder": "descending",
    }
    try:
        xml_text = http_get(session, EXPORT_URL, params=params, cache=cache,
                            retries=retries, base_delay=delay)
    except Exception as exc:
        log("Atom API 检索失败 '%s': %s" % (truncate(query, 40), exc), "warn")
        return None
    return parse_atom(xml_text, query)


def search_by_ids(
    session: Any,
    arxiv_ids: List[str],
    cache: Optional[DiskCache] = None,
    delay: float = 3.0,
    retries: int = 3,
) -> List[Candidate]:
    """按 arXiv ID 批量补全元数据 (替代原来抓 ``/abs/`` 详情页的做法)。

    一次请求带 ``ID_BATCH`` 个 ID, 比逐个抓详情页少两个数量级的请求数 —— 逐个抓
    正是限流的另一个来源。失败的批次跳过, 返回能拿到的部分 (这是补全, 不是主流程)。
    """
    ids = dedupe_preserve([i for i in (arxiv_ids or []) if i])
    if not ids:
        return []
    out: List[Candidate] = []
    for start in range(0, len(ids), ID_BATCH):
        batch = ids[start:start + ID_BATCH]
        params = {"id_list": ",".join(batch), "max_results": len(batch)}
        try:
            xml_text = http_get(session, EXPORT_URL, params=params, cache=cache,
                                retries=retries, base_delay=delay)
        except Exception as exc:
            log("批量补全失败 (%d 个 ID): %s" % (len(batch), exc), "warn")
            continue
        out.extend(parse_atom(xml_text, ""))
    return out


# --------------------------------------------------------------------------
# 通道 2: RSS 公告 (每天每个分类一次请求, 不需要检索式)
# --------------------------------------------------------------------------
# RSS 2.0 自己的标签没有命名空间, arxiv:/dc: 两个前缀必须走这个映射表查。
# 注意 ``item.find(path)`` **不传**这张表的话, 带前缀的路径一律返回 None ——
# 不会报错, 只会静默变成空字符串, 所以 _rss_text 里那个 namespaces 参数不能省。
_RSS_NS = {
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# description 长这样:
#   "arXiv:2502.18929v1 Announce Type: new \nAbstract: We study ..."
# 前半段是 arXiv 自己塞的公告信息, 不是摘要, 必须剥掉, 否则摘要字段里全是噪声。
_RE_RSS_PREFIX = re.compile(
    r"^\s*arXiv:\s*\S+\s*Announce\s+Type:\s*\S+\s*", re.I)


def _rss_text(item: ET.Element, tag: str, ns: str = "") -> str:
    path = "%s:%s" % (ns, tag) if ns else tag
    node = item.find(path, _RSS_NS)
    return clean_text(node.text or "") if node is not None else ""


def _parse_rss_date(raw: str) -> Optional[datetime]:
    """解析 RSS 的 ``pubDate`` (RFC 822, 如 'Mon, 24 Feb 2025 00:00:00 -0500')。"""
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except Exception:
        return None
    # 统一成 naive 本地时间: 候选池里的时间来自好几条通道, 带 tzinfo 的和不带
    # 的混在一起比较会直接抛 TypeError
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def parse_rss(xml_text: str, category: str = "",
              announce_types: Optional[Any] = None) -> List[Candidate]:
    """解析 ``rss.arxiv.org/rss/<category>`` 的响应。

    RSS 的字段比 Atom 少 (没有 DOI / journal_ref / comments), 但**有当天全部新公告**,
    这正是检索式补不到的那一块。

    ``announce_types`` 给定时只保留这些公告类型 (见下面对 new/cross/replace 的说明);
    传 ``None`` 表示不过滤。
    """
    out: List[Candidate] = []
    skipped = 0
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        log("RSS 解析失败 (%s): %s" % (category or "?", exc), "warn")
        return out

    for item in root.iter("item"):
        link = _rss_text(item, "link")
        arxiv_id = extract_arxiv_id(link) or extract_arxiv_id(_rss_text(item, "guid"))
        title = _rss_text(item, "title")
        if not arxiv_id or not title:
            continue

        desc = html_mod.unescape(_rss_text(item, "description"))
        desc = _RE_RSS_PREFIX.sub("", desc)
        abstract = re.sub(r"^Abstract\s*:\s*", "", desc, flags=re.I).strip()

        # dc:creator 是**每个作者一个节点** (不是逗号分隔的一串), 所以必须
        # findall —— 用 find 只会拿到第一作者
        authors = [clean_text(n.text or "") for n in item.findall("dc:creator", _RSS_NS)]
        authors = [a for a in authors if a]

        prim = item.find("arxiv:primary_category", _RSS_NS)
        primary = (prim.get("term") or "").strip() if prim is not None else ""
        # **feed 自己的分类必须补进去。** RSS 的 <category> 只写主分类, 交叉列表
        # 的论文在别的分类的 feed 里也照样只写自己的主分类 —— 实测 cond-mat.str-el
        # 的 41 条公告里有 23 条是 cross/replace-cross, <category> 写的是 hep-th、
        # quant-ph 之类。但"出现在 cond-mat.str-el 的 feed 里"这件事本身就说明它被
        # 公告到了该分类。不补的话按分类过滤时这 23 条会被当成越界结果丢掉。
        cats = dedupe_preserve([primary, category, _rss_text(item, "category")])

        pub_raw = _rss_text(item, "pubDate")
        announce = _rss_text(item, "announce_type", "arxiv")

        # 公告类型过滤。arXiv 的 feed 里混着四种公告: new (新论文) / cross
        # (新论文被交叉列表到本分类) / replace (**旧论文**的新版本) /
        # replace-cross。后两种不是"新论文", 而是已有论文的修订 —— 订阅模式
        # 只想要当天的新东西, 所以默认把它们滤掉 (zotero-arxiv-daily 也是这么
        # 做的, 它的 include_cross_list 默认关)。announce_types 为 None 时
        # 不过滤, 保持老行为。
        if announce_types is not None and announce not in announce_types:
            skipped += 1
            continue

        out.append(Candidate(
            arxiv_id=arxiv_id,
            title=title,
            abstract=abstract,
            authors=authors,
            published=_parse_rss_date(pub_raw),
            primary_category=primary or (cats[0] if cats else ""),
            categories=cats,
            announced=pub_raw,
            comments=("公告类型: %s" % announce) if announce else "",
            source_queries=["<RSS> %s 最新公告" % (category or "?")],
        ))
    if skipped:
        log("  RSS %s: 另有 %d 条是旧论文的新版本 (replace), 已按公告类型滤掉"
            % (category or "?", skipped), "dbg")
    return out


def search_rss(
    session: Any,
    categories: List[str],
    cache: Optional[DiskCache] = None,
    delay: float = 3.0,
    retries: int = 3,
    max_per_category: int = 0,
    announce_types: Optional[Any] = None,
) -> List[Candidate]:
    """抓取若干分类的当天公告。失败就跳过该分类 (这是补充通道, 不该拖垮主流程)。"""
    out: List[Candidate] = []
    for cat in dedupe_preserve(categories or []):
        try:
            xml_text = http_get(session, RSS_URL % cat, cache=cache,
                                retries=retries, base_delay=delay)
        except Exception as exc:
            log("RSS 抓取失败 (%s): %s" % (cat, exc), "warn")
            continue
        items = parse_rss(xml_text, cat, announce_types=announce_types)
        if max_per_category and len(items) > max_per_category:
            items = items[:max_per_category]
        log("  RSS %s: %d 条公告" % (cat, len(items)), "dbg")
        out.extend(items)
    return out


# --------------------------------------------------------------------------
# 候选池汇总
# --------------------------------------------------------------------------
def merge_candidates(pools: Iterable[List[Candidate]]) -> List[Candidate]:
    """合并多路检索结果, 按 arXiv ID 去重并保留命中的检索式。

    先出现的通道优先保留其字段值 (只补空字段), 所以调用方要把**信息更全的通道
    排在前面** —— ``collect`` 就是先放 Atom API, 再放 RSS。
    """
    merged: Dict[str, Candidate] = {}
    for pool in pools:
        for cand in pool:
            key = cand.arxiv_id.lower()
            if not key:
                continue
            if key in merged:
                existing = merged[key]
                for q in cand.source_queries:
                    if q not in existing.source_queries:
                        existing.source_queries.append(q)
                # 摘要更完整的优先保留
                if len(cand.abstract) > len(existing.abstract):
                    existing.abstract = cand.abstract
                if not existing.comments and cand.comments:
                    existing.comments = cand.comments
                if not existing.journal_ref and cand.journal_ref:
                    existing.journal_ref = cand.journal_ref
                if not existing.doi and cand.doi:
                    existing.doi = cand.doi
                if not existing.published and cand.published:
                    existing.published = cand.published
                if not existing.authors and cand.authors:
                    existing.authors = cand.authors
                if not existing.categories and cand.categories:
                    existing.categories = cand.categories
                    existing.primary_category = cand.primary_category
            else:
                merged[key] = cand
    return list(merged.values())


def _fill_missing_abstracts(session: Any, merged: List[Candidate],
                            cache: Optional[DiskCache], delay: float,
                            retries: int) -> None:
    """给缺摘要的候选 (主要是 RSS 来的) 批量补全, 就地改。"""
    need = [c.arxiv_id for c in merged if not c.abstract]
    if not need:
        return
    log("补全 %d 篇缺摘要的候选 (id_list 批量查询)" % len(need))
    by_id = {c.arxiv_id: c for c in search_by_ids(session, need, cache=cache,
                                                  delay=delay, retries=retries)}
    filled = 0
    for cand in merged:
        got = by_id.get(cand.arxiv_id)
        if got is None:
            continue
        if got.abstract and not cand.abstract:
            cand.abstract = got.abstract
            filled += 1
        if not cand.doi and got.doi:
            cand.doi = got.doi
        if not cand.journal_ref and got.journal_ref:
            cand.journal_ref = got.journal_ref
        if not cand.comments and got.comments:
            cand.comments = got.comments
        if not cand.published and got.published:
            cand.published = got.published
        if not cand.authors and got.authors:
            cand.authors = got.authors
    log("  补全了 %d 篇的摘要" % filled, "dbg")


def collect(
    cfg: Dict[str, Any],
    queries: List[str],
    cache: Optional[DiskCache] = None,
    per_query: Optional[int] = None,
    max_candidates: Optional[int] = None,
    categories: Optional[List[str]] = None,
    delay: Optional[float] = None,
    progress: Optional[Any] = None,
    should_stop: Optional[Any] = None,
) -> List[Candidate]:
    """按检索式列表抓取候选论文。

    流程: 每条检索式一次 Atom API 请求 (不翻页) -> 再补一遍各分类的当天 RSS 公告
    -> 合并去重 -> 按需补全缺摘要的。

    ``progress`` 是可选回调 ``fn(done, total, query)``; ``should_stop`` 是可选
    回调 ``fn() -> bool``, 返回 True 时在检索式之间提前收工 (UI 的"停止"按钮)。
    """
    acfg = cfg.get("arxiv", {})
    ncfg = cfg.get("network", {})
    per_query = per_query or int(acfg.get("per_query", 200))
    max_candidates = max_candidates or int(acfg.get("max_candidates", 800))
    categories = categories if categories is not None else (acfg.get("categories") or [])
    delay = delay if delay is not None else float(acfg.get("request_delay", 3.0))
    use_rss = bool(acfg.get("use_rss", True))
    rss_categories = acfg.get("rss_categories") or categories
    # 订阅模式 (一条检索式都没有): RSS 是**唯一**的候选来源。这时 use_rss=False
    # 不能再照字面执行 —— 那会让整个候选池静默地空掉, 然后用户看到的是"没有抓到
    # 任何候选论文", 完全猜不到是配置里一个跟订阅无关的开关干的。
    subscribe = not queries
    if subscribe:
        # 订阅模式认的是用户在界面上勾的那几个分类, 不是 rss_categories ——
        # 后者是"检索模式下额外抓哪些分类"的老配置项, 拿它覆盖勾选结果会让人
        # 对着一排勾选框猜"到底订的是什么"。
        rss_categories = categories
        if not use_rss:
            log("订阅模式: RSS 是唯一的候选来源, 忽略 use_rss=False", "warn")
            use_rss = True
    if not use_rss:
        rss_categories = []
    announce_types = acfg.get("subscribe_announce_types") if subscribe else None

    # 请求间隔交给全局闸门。放这里设置是因为它同时管住了检索、补全、RSS 和
    # OpenAlex 富化 —— 每条通道各自 sleep 是挡不住并发的。
    set_global_interval(delay)

    session = build_session(ncfg.get("proxy"), timeout=int(ncfg.get("timeout", 40)))
    retries = int(ncfg.get("retries", 4))

    pools: List[List[Candidate]] = []
    total_raw = 0
    failed_queries: List[str] = []

    # 每条检索式实际抓多少篇。按 max_candidates 摊到各条检索式上, 不再每条都抓
    # per_query 篇 —— 18 条检索式各抓 200 篇 = 3600 条, 而候选池上限只有 800,
    # 也就是说七成的抓取 (以及它们的请求、限流等待) 最后会被整段丢掉。
    # 更糟的是合并时是"一条检索式接一条"地排, 截断后排在后面的十几条检索式
    # 连一篇都没进排序 —— 白抓, 而且白得毫无意义。摊薄之后每条都进得来。
    budget = per_query
    if max_candidates and queries:
        spread = int(math.ceil(max_candidates * float(acfg.get(
            "per_query_slack", 1.35)) / len(queries)))
        budget = max(int(acfg.get("per_query_min", 50)),
                     min(per_query, spread))
        if budget < per_query:
            log("按候选池上限 %d 摊薄: 每条检索式抓 %d 篇 (原 %d 篇)"
                % (max_candidates, budget, per_query), "dbg")

    def _run(query: str, idx: int, total: int) -> Optional[List[Candidate]]:
        log("[%d/%d] 检索: %s" % (idx, total, truncate(query, 70)))
        got = search_api(session, query, max_results=budget, cache=cache,
                         delay=delay, retries=retries,
                         categories=categories or None)
        if got is None:
            log("  请求失败, 稍后重试", "warn")
            return None
        log("  命中 %d 篇" % len(got), "dbg")
        return got

    stopped = False
    # 连着几条检索式一条都没抓到时, 先探一下网络, 不通就别再往下试了。
    #
    # 为什么要探而不是直接收工: 每失败一条要等 5 次重试 (退避加起来约 60 秒),
    # 18 条检索式外加后面那轮整体重试 = 36 次 × 60 秒, 网络真断了的话用户要干等
    # 半个多小时才看到一句"没有抓到任何候选论文"。但"连续失败"本身**不能**当作
    # 网络断了的证据 —— arXiv 限流时也会这样, 而限流是间歇性的, 第 4 条很可能
    # 就好了, 直接收工等于把本来能拿到的结果白白丢掉。所以这里花 8 秒打一次
    # 探测请求, 用事实区分这两种情况。
    DEAD_AFTER = 3
    consec_fail = 0
    dead = False
    for idx, query in enumerate(queries, 1):
        if should_stop is not None and should_stop():
            log("收到停止请求, 已抓到的结果照常使用", "warn")
            stopped = True
            break
        got = _run(query, idx, len(queries))
        if got is None:
            failed_queries.append(query)
            consec_fail += 1
            if consec_fail >= DEAD_AFTER and total_raw == 0:
                route = dict(getattr(session, "proxies", None) or {}) or None
                if probe_route(route, timeout=8.0):
                    log("连续 %d 条检索式失败, 但网络是通的 —— 应该是 arXiv 在"
                        "限流, 继续尝试后面的检索式" % consec_fail, "warn")
                    consec_fail = 0     # 重置, 免得后面每条都探一次
                else:
                    log("连续 %d 条检索式全部失败, 探测也确认网络不通, 不再尝试"
                        "后面的 %d 条" % (consec_fail, len(queries) - idx), "err")
                    dead = True
                    break
        else:
            pools.append(got)
            total_raw += len(got)
            consec_fail = 0
        if progress is not None:
            try:
                progress(idx, len(queries), query)
            except Exception:
                pass

    if dead:
        log("请检查设置页的「代理」: 本机代理 (如 127.0.0.1:7890) 挂掉时, 端口"
            "可能还在监听, 但每个请求都返回 502。点「测试 arXiv 连接」按钮可以"
            "看到实际走的是哪条线路; 也可以把代理清空直接走直连再试。", "err")

    # 失败的检索式整体再试一轮 —— arXiv 的限流是间歇性的, 隔一会儿常常就好了。
    # 间隔靠闸门自己退避, 这里不再额外 sleep。网络已经判定不通时跳过这轮, 免得
    # 把刚才省下的时间又原样赔进去。
    if failed_queries and not stopped and not dead:
        log("有 %d 条检索式失败, 重试一轮" % len(failed_queries), "warn")
        still_failed = []
        for idx, query in enumerate(failed_queries, 1):
            got = _run(query, idx, len(failed_queries))
            if got is None:
                still_failed.append(query)
            else:
                pools.append(got)
                total_raw += len(got)
        if still_failed:
            log("仍有 %d 条检索式未成功: %s"
                % (len(still_failed), "; ".join(truncate(q, 40) for q in still_failed)),
                "warn")
            log("提示: 这是 arXiv 对该网络出口的间歇性限流。可以提高 "
                "arxiv.request_delay (全局请求间隔, 单位秒), 换用其他代理, "
                "或稍后重跑 (已抓到的结果有缓存, 重跑不会重复请求)。", "warn")

    # 补充通道: 各分类当天的全部公告。放在检索之后, 这样合并时 API 的完整元数据优先。
    if rss_categories and not stopped:
        if should_stop is not None and should_stop():
            log("收到停止请求, 跳过 RSS 公告", "warn")
        else:
            log("抓取 RSS 公告: %s" % ", ".join(rss_categories))
            rss = search_rss(session, list(rss_categories), cache=cache,
                             delay=delay, retries=retries,
                             announce_types=announce_types)
            if rss:
                pools.append(rss)
                total_raw += len(rss)
            else:
                log("  RSS 没拿到公告 (不影响主结果)", "warn")

    merged = merge_candidates(pools)
    log("候选池: 原始 %d 条 -> 去重后 %d 篇" % (total_raw, len(merged)))

    if acfg.get("fetch_details"):
        _fill_missing_abstracts(session, merged, cache, delay, retries)

    if max_candidates and len(merged) > max_candidates:
        log("候选数超过上限 %d, 截断" % max_candidates, "warn")
        merged = merged[:max_candidates]
    return merged
