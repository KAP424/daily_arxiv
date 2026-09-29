"""研究画像构建。

把本地 PDF 文献库浓缩成:
  * 一段研究方向概述
  * 主要研究主题 / 方法 / 关键词
  * 一组用于 arXiv 检索的检索式

文献多时采用分层摘要 (先分组归纳, 再汇总), 避免单次请求超出上下文。
没有 AI 时退化为 TF-IDF 关键词抽取, 流程依然能跑通。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .ai import AIClient, NullAIClient
from .models import LibraryPaper, ResearchProfile
from .utils import (chunk, dedupe_preserve, log, strip_latex, truncate)

# 每篇文献在摘要里占的字符预算
PER_PAPER_CHARS = 300
# 单次请求送进去的文献数
GROUP_SIZE = 40
# 单次请求的字符上限 (粗略控制 token)
MAX_DIGEST_CHARS = 40000


SYSTEM_PROMPT = (
    "你是一位资深的学术文献分析助手, 服务对象是科研工作者。"
    "你的任务是从一个人的文献库里推断他的研究方向和兴趣, 并生成用于检索 arXiv 的检索式。"
    "请只输出 JSON, 不要任何解释文字。"
)


def _paper_line(idx: int, paper: LibraryPaper) -> str:
    """把一篇文献压成一行, 用于喂给 AI。

    每篇的字数上限 (``PER_PAPER_CHARS``) 是硬预算, 不随读取深度变 —— 深度决定
    的是**用什么内容填满这个预算**, 不是把它撑大。所以选"全文"档不会让画像
    那一步的 token 翻倍, 只是让每篇的字更有信息量 (摘要短的时候尤其明显)。
    """
    bits = ["%d. %s" % (idx, strip_latex(paper.title))]
    if paper.year:
        bits.append("(%d)" % paper.year)
    if paper.publication:
        bits.append("[%s]" % truncate(paper.publication, 40))
    body = paper.abstract or ""
    if paper.context:
        # 摘要之后接上按深度取的正文。摘要本身就长的论文, 上下文只占剩下那点
        # 余量; 没摘要的论文则由它填满整个预算。
        body = (body + " " + paper.context).strip() if body else paper.context
    if body:
        bits.append("— " + truncate(strip_latex(body), PER_PAPER_CHARS, ""))
    return " ".join(bits)


def _build_digest(papers: List[LibraryPaper], max_chars: int = MAX_DIGEST_CHARS) -> str:
    """构建文献摘要文本, 超出预算时按优先级截断。"""
    lines = []
    used = 0
    # 有摘要的优先, 其次按年份新的优先
    ordered = sorted(
        papers,
        key=lambda p: (0 if p.abstract else 1, -(p.year or 0)),
    )
    for idx, paper in enumerate(ordered, 1):
        line = _paper_line(idx, paper)
        if used + len(line) > max_chars and lines:
            break
        lines.append(line)
        used += len(line) + 1
    if len(lines) < len(ordered):
        log("画像摘要使用前 %d 篇文献 (共 %d 篇, 控制上下文长度)"
            % (len(lines), len(ordered)), "dbg")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# AI 路径
# --------------------------------------------------------------------------
_GROUP_SCHEMA_HINT = """{
  "topics": ["研究主题, 中文学术术语, 6-12 个"],
  "methods": ["使用的研究方法/技术, 6-12 个"],
  "keywords": ["英文专业关键词, 15-30 个, 用于检索"],
  "subfields": ["对应的 arXiv 分类代码, 如 cond-mat.str-el, 3-8 个"]
}"""


def _summarize_group(ai: AIClient, digest: str, group_idx: int) -> Dict[str, Any]:
    user = (
        "以下是一位研究者文献库的一部分 (编号. 标题 (年份) [期刊] — 摘要)。\n\n"
        "%s\n\n"
        "请归纳这批文献体现出的研究主题、方法、英文关键词和所属 arXiv 分类。\n"
        "严格按此 JSON 结构输出:\n%s" % (digest, _GROUP_SCHEMA_HINT)
    )
    result = ai.chat_json(SYSTEM_PROMPT, user, default={})
    if not isinstance(result, dict):
        return {}
    return result


_FINAL_SCHEMA_HINT = """{
  "summary": "一段 150-250 字的中文概述, 说明这个人的研究方向、关注的核心问题和常用手段",
  "topics": ["主要研究主题, 8-15 个中文术语"],
  "methods": ["常用方法/技术, 6-12 个"],
  "keywords": ["英文专业关键词, 20-40 个"],
  "queries": [
    "arXiv 检索式, 12-20 条, 纯英文, 每条 2-6 个词",
    "要同时包含宽泛的主题词和具体的专有名词, 覆盖不同侧面",
    "例如: measurement induced criticality / determinant quantum Monte Carlo / "
    "Gross-Neveu transition / entanglement entropy sign problem"
  ]
}"""


def _finalize_profile(ai: AIClient, merged: Dict[str, Any], digest: str,
                      n_papers: int, attempts: int = 3) -> Dict[str, Any]:
    user = (
        "这是一位研究者文献库的整体归纳结果 (来自分批分析):\n"
        "%s\n\n"
        "该文献库共 %d 篇文献。以下是其中一部分的原始条目, 供你把握具体方向:\n\n"
        "%s\n\n"
        "请综合以上信息, 生成最终的研究画像与 arXiv 检索式。\n"
        "检索式要能覆盖他研究的不同侧面 (核心方法、物理对象、交叉方向), "
        "既要有精确的专有名词, 也要有宽泛的主题词, 以便召回足够多的相关新文献。\n"
        "严格按此 JSON 结构输出:\n%s"
        % (json.dumps(merged, ensure_ascii=False, indent=1)[:6000],
           n_papers, truncate(digest, 16000), _FINAL_SCHEMA_HINT)
    )
    # 这一步是**整个流程的咽喉**: 没有检索式就没有候选, 整轮直接报废。而它偏偏
    # 是提示词最长、最容易被接口截断或返回空的一次调用 (实测真踩到过一次空回复,
    # 前面的分组归纳全都好好的, 就这一步空手而归, 于是整轮白跑)。
    # 所以这里单独重试: 空回复/解析失败就再来一次, 不指望一次就中。
    for attempt in range(1, max(1, attempts) + 1):
        result = ai.chat_json(SYSTEM_PROMPT, user, default={})
        if isinstance(result, dict) and (result.get("queries") or []):
            return result
        if attempt < attempts:
            log("  生成检索式没拿到结果 (第 %d/%d 次), 重试" % (attempt, attempts),
                "warn")
    log("生成检索式连续 %d 次都没拿到结果, 改用关键词拼检索式" % attempts, "warn")
    return result if isinstance(result, dict) else {}


def _queries_from_ai(ai: AIClient, papers: List[LibraryPaper],
                     n_queries: int) -> Dict[str, Any]:
    """分层摘要 -> 最终画像。"""
    digest = _build_digest(papers)
    groups = chunk(digest.split("\n"), GROUP_SIZE)
    log("画像分析: %d 篇文献分 %d 组归纳" % (len(papers), len(groups)))

    group_results = []
    for i, grp in enumerate(groups, 1):
        res = _summarize_group(ai, "\n".join(grp), i)
        if res:
            group_results.append(res)
            log("  第 %d/%d 组完成" % (i, len(groups)), "dbg")

    merged: Dict[str, Any] = {"topics": [], "methods": [], "keywords": [], "subfields": []}
    for res in group_results:
        for key in merged:
            vals = res.get(key) or []
            if isinstance(vals, list):
                merged[key].extend(str(v) for v in vals if v)
    for key in merged:
        merged[key] = dedupe_preserve(merged[key])

    final = _finalize_profile(ai, merged, digest, len(papers))
    if not final:
        final = merged
    final["queries"] = dedupe_preserve([str(q) for q in (final.get("queries") or [])])
    return final


# --------------------------------------------------------------------------
# 无 AI 的启发式路径
# --------------------------------------------------------------------------
_STOPWORDS = set("""
a an the of and or for in on to with by from at as is are was were be been being
this that these those it its we our us they their them he she his her not no nor
but if then than so such can could may might will would shall should do does did
have has had using used use new novel two three one via under over between within
results result show shows shown study studies paper present presents presented
however thus therefore also more most less least very much many few all any both
each other others into out up down about which who whom whose what when where why
""".split())


def _heuristic_profile(papers: List[LibraryPaper], n_queries: int) -> Dict[str, Any]:
    """无 AI 时: 用 TF-IDF 从标题+摘要里抽关键词, 拼成检索式。"""
    from sklearn.feature_extraction.text import TfidfVectorizer

    docs = []
    for p in papers:
        text = "%s %s" % (p.title or "", p.abstract or "")
        text = strip_latex(text).lower()
        docs.append(text)
    docs = [d for d in docs if d.strip()]
    if not docs:
        return {"summary": "", "topics": [], "methods": [], "keywords": [], "queries": []}

    terms: List[str] = []
    try:
        vec = TfidfVectorizer(
            stop_words=list(_STOPWORDS), ngram_range=(1, 2),
            max_features=400, min_df=2, sublinear_tf=True,
        )
        matrix = vec.fit_transform(docs)
        scores = matrix.sum(axis=0).A1
        names = vec.get_feature_names_out()
        ranked = sorted(zip(names, scores), key=lambda kv: -kv[1])
        # 优先保留双词短语 (检索效果更好), 单字词只补少量
        bigrams = [n for n, _ in ranked if " " in n]
        unigrams = [n for n, _ in ranked if " " not in n]
        terms = dedupe_preserve(bigrams[:n_queries] + unigrams[:n_queries // 2])
    except Exception as exc:
        log("TF-IDF 关键词抽取失败: %s" % exc, "warn")

    queries = terms[:n_queries]
    return {
        "summary": "（未配置 AI, 以下关键词由 TF-IDF 从文献库标题与摘要自动抽取）",
        "topics": [],
        "methods": [],
        "keywords": terms,
        "queries": queries,
    }


def _has_ascii_letter(s: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in s)


def _queries_from_terms(data: Dict[str, Any], n_queries: int) -> List[str]:
    """画像里已有主题/方法/关键词、但一条检索式都没有时, 用它们拼检索式。

    这种组合是会出现的: AI 归纳主题和关键词那几步都成功了, 唯独最后"生成检索式"
    那一步返回了空 (提示词最长, 最容易出岔子)。这时画像本身是好的, 没理由为了
    缺检索式就把整轮丢掉 —— 拿现成的词拼几条, 至少能跑出一个结果。

    **只挑英文词**, 中文的一个都不要。画像里的 ``topics`` / ``methods`` 按提示词的
    要求是**中文**的, 而 arXiv 的标题摘要全是英文 —— 拿"符号问题"去查是一条都
    返回不了的。按"主题在前、关键词在后"拼的话, 18 条预算会被中文词占满, 这一轮
    照样零候选, 只是失败得更晚、还白打了一堆限流很紧的请求。少几条好检索式,
    永远好过凑数凑出来的废检索式。

    一个英文词都没有时返回空列表, 由调用方走启发式路径 (从文献标题/摘要里抽英文
    关键词) —— 那条路才真的能救回来。
    """
    ascii_terms: List[str] = []
    for key in ("queries", "keywords", "methods", "topics"):
        for item in (data.get(key) or []):
            s = str(item).strip()
            if not s or not _has_ascii_letter(s):
                continue
            # 检索式是"2-6 个词的短语"; 太长的当句子处理, 截前 6 个词
            if len(s.split()) > 6:
                s = " ".join(s.split()[:6])
            ascii_terms.append(s)
    return [q for q in dedupe_preserve(ascii_terms) if q][:max(1, n_queries)]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def build_profile(cfg: Dict[str, Any], papers: List[LibraryPaper],
                  ai: Optional[AIClient] = None) -> ResearchProfile:
    """构建研究画像, 并生成检索式。"""
    acfg = cfg.get("arxiv", {})
    n_queries = int(acfg.get("auto_queries", 18))
    manual = [q for q in (acfg.get("queries") or []) if q and q.strip()]

    use_ai = ai is not None and not isinstance(ai, NullAIClient)
    data: Dict[str, Any] = {}
    generated_by = "heuristic"

    if use_ai:
        try:
            data = _queries_from_ai(ai, papers, n_queries)
            generated_by = "ai"
        except Exception as exc:
            log("AI 画像分析失败 (%s), 回退到启发式方法" % exc, "warn")
            data = {}
    if not data or not (data.get("queries") or data.get("keywords")):
        data = _heuristic_profile(papers, n_queries)
        generated_by = "heuristic"
    elif not (data.get("queries") or []):
        # 有主题/关键词却没有检索式 —— 上面 _finalize_profile 已经重试过几轮了。
        # 这时拿现成的词拼几条, 别让整轮卡在"没有检索式"上。
        data = dict(data)
        data["queries"] = _queries_from_terms(data, n_queries)
        if data["queries"]:
            generated_by += "+terms"
            log("AI 没给出检索式, 用画像里的关键词拼了 %d 条"
                % len(data["queries"]), "warn")
        else:
            # 画像里一个英文词都没有 (全是中文主题/方法)。中文检索式在 arXiv 上
            # 一条都命中不了, 所以别拿它们凑数 —— 改用启发式路径, 从文献的标题和
            # 摘要里抽英文关键词。这是唯一还救得回来的路。
            log("画像里没有可用的英文词, 改用文献标题/摘要抽关键词生成检索式", "warn")
            data = _heuristic_profile(papers, n_queries)
            generated_by = "heuristic"

    queries = [str(q) for q in (data.get("queries") or []) if q]
    if manual:
        log("使用配置中手动指定的 %d 条检索式 (忽略自动生成)" % len(manual), "warn")
        queries = manual
        generated_by += "+manual"

    queries = dedupe_preserve(queries)[:max(n_queries, len(manual))]

    profile = ResearchProfile(
        summary=str(data.get("summary") or ""),
        topics=[str(t) for t in (data.get("topics") or [])],
        methods=[str(t) for t in (data.get("methods") or [])],
        keywords=[str(t) for t in (data.get("keywords") or [])],
        queries=queries,
        generated_by=generated_by,
    )

    log("研究画像: %d 个主题, %d 个关键词, %d 条检索式 (来源: %s)"
        % (len(profile.topics), len(profile.keywords), len(profile.queries), generated_by))
    return profile


def profile_digest_for_scoring(profile: ResearchProfile) -> str:
    """把画像压成一段文本, 用于相关性打分的提示词。"""
    parts = []
    if profile.summary:
        parts.append("研究方向概述: %s" % profile.summary)
    if profile.topics:
        parts.append("主要主题: %s" % "、".join(profile.topics[:15]))
    if profile.methods:
        parts.append("常用方法: %s" % "、".join(profile.methods[:12]))
    if profile.keywords:
        parts.append("关键词: %s" % ", ".join(profile.keywords[:40]))
    return "\n".join(parts)
