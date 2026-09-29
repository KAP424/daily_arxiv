"""AI 深度解读: 为每篇入选论文生成

  1. 大致内容讲解 (它在做什么、怎么做、结论是什么)
  2. 与你文献库中哪些工作相关 (逐条对应到具体文献)
  3. 可以怎样结合你的工作做新研究 (具体可操作的方向)

关键设计: 每篇候选只把**最相关的若干篇你自己的文献**喂进去 (TF-IDF 预筛),
既省 token, 又让 AI 的"关联"有的放矢, 而不是泛泛而谈。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from .ai import AIClient, NullAIClient
from .history import RecommendHistory
from .models import Candidate, LibraryPaper, ResearchProfile
from .profile import profile_digest_for_scoring
from .utils import log, strip_latex, truncate

# 每篇候选配多少篇"你自己的相关文献"作为上下文
RELATED_K = 8
# 关联文献在提示词里的摘要长度
RELATED_ABSTRACT_CHARS = 400

ANALYZE_SYSTEM = (
    "你是一位既懂物理又熟悉学术前沿的资深合作者, 正在帮一位研究者快速判断"
    "一篇新论文值不值得读、以及能和他的工作怎么结合。"
    "你的分析必须具体: 说清楚论文的核心机制、用了什么方法、结论是什么; "
    "指出的关联要落到具体文献上; 提的研究想法要可操作, 不能是空话套话。"
    "只输出 JSON。"
)

ANALYZE_SCHEMA = """{
  "summary": "这篇论文的内容讲解, 200-320 字中文。说清楚: 研究什么问题、用什么方法、主要结论是什么、为什么有意思。允许出现必要的物理名词和公式符号。",
  "connections": [
    {
      "paper": "必须是下面给出的候选文献标签之一, 原样照抄",
      "relation": "这篇新论文和该文献的具体关联, 60-120 字。要说明是方法相同、问题相通、结论互补, 还是可以互相验证。"
    }
  ],
  "ideas": "可以怎样把这篇论文和上面这些工作结合起来做新研究, 150-260 字中文。给出 1-2 个具体方向: 要做什么、用什么方法、预期能得到什么。要具体到能动手的程度。"
}"""


# --------------------------------------------------------------------------
# 相关文献预筛
# --------------------------------------------------------------------------
class LibraryIndex:
    """基于 TF-IDF 的文献库检索, 用于给候选论文找"最相关的已有工作"。"""

    def __init__(self, papers: List[LibraryPaper]):
        self.papers = papers
        self._vectorizer = None
        self._matrix = None
        self._build()

    def _paper_text(self, p: LibraryPaper) -> str:
        """TF-IDF 用的文本。

        这里把 ``context`` (按深度取的引言/结论/全文) **无上限**地全放进去 ——
        它只参与本地打分, 不进提示词, 所以多加不花一分钱, 反而能让"最相关的
        几篇"选得更准。
        """
        parts = [p.title or ""]
        if p.abstract:
            parts.append(p.abstract)
        if p.context:
            parts.append(p.context)
        if p.tags:
            parts.append(" ".join(p.tags))
        return strip_latex(" ".join(parts)).lower()

    def _build(self) -> None:
        docs = [self._paper_text(p) for p in self.papers]
        if not docs:
            return
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            self._vectorizer = TfidfVectorizer(
                stop_words="english", ngram_range=(1, 2),
                sublinear_tf=True, min_df=1, max_features=60000,
            )
            self._matrix = self._vectorizer.fit_transform(docs)
        except Exception as exc:
            log("相关文献索引构建失败 (%s), 退回关键词重叠匹配" % exc, "warn")
            self._vectorizer = None

    def related(self, cand: Candidate, k: int = RELATED_K) -> List[LibraryPaper]:
        """找出与候选论文最相关的 k 篇库内文献。"""
        if not self.papers:
            return []
        if self._vectorizer is None or self._matrix is None:
            return self._keyword_related(cand, k)

        try:
            from sklearn.metrics.pairwise import cosine_similarity
            query = self._vectorizer.transform(
                [strip_latex("%s %s" % (cand.title or "",
                                        cand.abstract or "")).lower()]
            )
            sims = cosine_similarity(query, self._matrix).ravel()
            order = sims.argsort()[::-1][:k]
            return [self.papers[i] for i in order if sims[i] > 0]
        except Exception as exc:
            log("相关文献检索失败 (%s), 退回关键词匹配" % exc, "warn")
            return self._keyword_related(cand, k)

    def _keyword_related(self, cand: Candidate, k: int) -> List[LibraryPaper]:
        """无 sklearn 时的兜底: 按标题/摘要的词重叠数排序。"""
        target = set(strip_latex("%s %s" % (cand.title or "",
                                            cand.abstract or "")).lower().split())
        target = {t for t in target if len(t) >= 4}
        scored = []
        for p in self.papers:
            words = set(strip_latex("%s %s" % (p.title or "",
                                               p.abstract or "")).lower().split())
            overlap = len(target & {w for w in words if len(w) >= 4})
            if overlap:
                scored.append((overlap, p))
        scored.sort(key=lambda kv: -kv[0])
        return [p for _, p in scored[:k]]


# --------------------------------------------------------------------------
# 单篇解读
# --------------------------------------------------------------------------
def _candidate_block(cand: Candidate) -> str:
    """候选论文在提示词里的呈现。"""
    lines = [
        "标题: %s" % strip_latex(cand.title),
        "arXiv: %s" % cand.arxiv_id,
    ]
    if cand.authors:
        lines.append("作者: %s" % ", ".join(cand.authors[:8]))
    if cand.published:
        lines.append("提交时间: %s" % cand.published.strftime("%Y-%m-%d"))
    if cand.categories:
        lines.append("分类: %s" % ", ".join(cand.categories[:4]))
    if cand.comments:
        lines.append("备注: %s" % truncate(strip_latex(cand.comments), 200, ""))
    if cand.journal_ref:
        lines.append("期刊: %s" % truncate(strip_latex(cand.journal_ref), 120, ""))
    if cand.citations is not None:
        lines.append("引用数: %d" % cand.citations)
    if cand.concepts:
        lines.append("OpenAlex 主题: %s" % ", ".join(cand.concepts[:6]))
    lines.append("摘要: %s" % truncate(strip_latex(cand.abstract or ""), 2200, ""))
    return "\n".join(lines)


def _library_block(papers: List[LibraryPaper]) -> Tuple[str, List[str]]:
    """你自己的文献在提示词里的呈现, 返回 (文本, 可用标签列表)。"""
    blocks = []
    labels = []
    for p in papers:
        label = p.short_label
        # 标签可能撞车, 加序号保证唯一
        if label in labels:
            label = "%s #%d" % (label, labels.count(label) + 1)
        labels.append(label)
        # 摘要优先, 余量用按深度取的正文补上 (metadata 档 context 是空串,
        # 行为和以前完全一致)
        body = p.abstract or ""
        if p.context:
            body = (body + " " + p.context).strip() if body else p.context
        blocks.append(
            "[%s] %s (%s)\n    %s" % (
                label,
                truncate(strip_latex(p.title), 180, ""),
                p.year or "n.d.",
                truncate(strip_latex(body), RELATED_ABSTRACT_CHARS, ""),
            )
        )
    return "\n".join(blocks), labels


def analyze_one(
    ai: AIClient,
    cand: Candidate,
    related: List[LibraryPaper],
    profile: Optional[ResearchProfile] = None,
) -> Dict[str, Any]:
    """解读单篇候选论文。失败返回 {}。"""
    lib_block, labels = _library_block(related)

    parts = []
    if profile is not None:
        digest = profile_digest_for_scoring(profile)
        if digest:
            parts.append("研究者的研究方向:\n%s" % digest)
    if lib_block:
        parts.append("研究者已读的相关文献 (方括号内是引用标签):\n%s" % lib_block)
    else:
        parts.append("（研究者文献库中没有检索到明显相关的已有工作, "
                     "connections 可以返回空数组）")
    parts.append("需要解读的新论文:\n%s" % _candidate_block(cand))
    parts.append(
        "请完成三件事:\n"
        "1. 讲解这篇论文的内容 (summary);\n"
        "2. 指出它和上面哪些已读文献相关, paper 字段必须原样使用方括号里的标签 (connections);\n"
        "3. 提出可以结合的研究方向 (ideas)。\n"
        "严格按此 JSON 结构输出:\n%s" % ANALYZE_SCHEMA
    )

    result = ai.chat_json(ANALYZE_SYSTEM, "\n\n".join(parts), default=None)
    if not isinstance(result, dict):
        return {}

    # 校验标签, 防止 AI 编造不存在的文献
    valid = set(labels)
    connections = []
    for item in (result.get("connections") or []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("paper") or "").strip()
        relation = str(item.get("relation") or "").strip()
        if not label or not relation:
            continue
        # 允许 AI 漏掉方括号, 做一次宽松匹配
        if label not in valid:
            for v in valid:
                if v and (v in label or label in v):
                    label = v
                    break
        if label in valid:
            connections.append({"paper": label, "relation": relation})

    return {
        "summary": str(result.get("summary") or "").strip(),
        "connections": connections,
        "ideas": str(result.get("ideas") or "").strip(),
    }


# --------------------------------------------------------------------------
# 批量解读
# --------------------------------------------------------------------------
def analyze_top(
    cfg: Dict[str, Any],
    candidates: List[Candidate],
    papers: List[LibraryPaper],
    ai: Optional[AIClient],
    profile: Optional[ResearchProfile] = None,
    top_n: int = 20,
    concurrency: int = 1,
    history: Optional[Dict[str, Dict[str, Any]]] = None,
    profile_fp: str = "",
    should_stop: Optional[Any] = None,
) -> Tuple[int, int]:
    """对排名前 ``top_n`` 的候选做深度解读, 就地写回候选对象。

    ``history`` 是推荐记录库里已有的记录 (``{arxiv_id: 行}``), 见 history.py。
    命中且画像指纹一致的那几篇**不调 AI**, 直接把上次的解读套回去 —— 这是
    "同一篇论文不要重复花 token" 的那一半 (另一半是 AI 缓存, 它只认逐字相同的
    提示词)。

    返回 ``(这次真正调 AI 解读的篇数, 复用旧解读的篇数)``。
    """
    targets = candidates[:top_n]
    if not targets:
        return 0, 0

    # 复用判定要在**调用 AI 之前**做完: 命中复用的那几篇根本不需要相关文献
    # 预筛 (TF-IDF), 那一部分计算也一并省掉。
    reused = 0
    todo: List[Candidate] = []
    for cand in targets:
        row = (history or {}).get(cand.arxiv_id or "")
        if RecommendHistory.reuse_analysis(cand, row, profile_fp):
            reused += 1
            log("  [复用] %s 推荐记录里已有同一画像下的解读, 跳过 AI 调用"
                % cand.arxiv_id, "dbg")
        else:
            todo.append(cand)
    if reused:
        log("推荐记录命中: %d 篇直接复用旧解读, 不消耗 token" % reused)

    if ai is None or isinstance(ai, NullAIClient):
        if not reused:
            log("未启用 AI, 跳过深度解读 (报告里只有摘要与评分)", "warn")
        return 0, reused
    if not todo:
        log("全部 %d 篇都命中了推荐记录, 这次一次 AI 都不用调" % reused)
        return 0, reused

    acfg = cfg.get("analysis", {})
    k = int(acfg.get("related_papers_k", RELATED_K))
    index = LibraryIndex(papers)
    log("深度解读 %d 篇论文 (每篇配 %d 篇你的相关文献)" % (len(todo), k))

    stopped_at = [0]

    def _work(item: Tuple[int, Candidate]) -> Tuple[int, Candidate, Dict[str, Any]]:
        idx, cand = item
        # 20 篇逐篇解读要跑好几分钟。并发路径下 future 是一次性全提交的, 队列里
        # 的那些得等到轮到自己才知道该不该跑 —— 所以检查必须放在**篇的开头**。
        if should_stop is not None and should_stop():
            stopped_at[0] = stopped_at[0] or idx
            return idx, cand, {}
        related = index.related(cand, k)
        try:
            res = analyze_one(ai, cand, related, profile)
        except Exception as exc:
            log("解读 %s 失败: %s" % (cand.arxiv_id, exc), "warn")
            res = {}
        return idx, cand, res

    done = 0
    if concurrency and concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_work, it) for it in enumerate(todo, 1)]
            for fut in as_completed(futures):
                idx, cand, res = fut.result()
                if _apply(cand, res):
                    done += 1
                log("  [%d/%d] %s %s" % (idx, len(todo), cand.arxiv_id,
                                         "完成" if res else "跳过"), "dbg")
    else:
        for item in enumerate(todo, 1):
            idx, cand, res = _work(item)
            if _apply(cand, res):
                done += 1
            log("  [%d/%d] %s %s" % (idx, len(todo), cand.arxiv_id,
                                     "完成" if res else "跳过"))
            if stopped_at[0]:
                break

    if stopped_at[0]:
        log("收到停止请求, 深度解读在第 %d/%d 篇停下 (已经解读完的照常写进报告)"
            % (stopped_at[0] - 1, len(todo)), "warn")

    log("深度解读完成: %d/%d 篇新解读%s"
        % (done, len(todo),
           (", 另有 %d 篇复用推荐记录" % reused) if reused else ""))
    return done, reused


def _apply(cand: Candidate, res: Dict[str, Any]) -> bool:
    if not res:
        return False
    cand.summary = res.get("summary", "")
    cand.connections = res.get("connections", [])
    cand.ideas = res.get("ideas", "")
    cand.analyzed = bool(cand.summary or cand.ideas)
    return cand.analyzed
