"""排序: 把候选论文按 相关性 / 时效性 / 重要程度 联合打分。

    score = w_rel * relevance + w_rec * recency + w_imp * importance

相关性优先用 AI 打分 (能理解语义, 而不是只匹配关键词); 没有 AI 时用 TF-IDF
余弦相似度兜底。时效性用指数衰减, 重要程度用引用数取对数再加期刊/顶刊加成。

AI 打分很贵, 所以先用启发式相似度做预筛, 只把最有希望的若干篇送去 AI 精评。
"""

from __future__ import annotations

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .ai import AIClient, NullAIClient
from .models import Candidate, ResearchProfile
from .profile import profile_digest_for_scoring
from .utils import chunk, log, strip_latex, truncate

# --------------------------------------------------------------------------
# 时效性
# --------------------------------------------------------------------------
def recency_score(cand: Candidate, tau_days: float, hard_days: float,
                  now: Optional[datetime] = None) -> float:
    """指数衰减的时效性得分 (0~1)。

    用 v1 提交时间 (``published``); 没有则退回最后更新时间的近似。
    """
    now = now or datetime.now()
    ref = cand.published or cand.updated
    if ref is None:
        return 0.3   # 日期未知, 给个中性偏低的值
    age = (now - ref).total_seconds() / 86400.0
    if age < 0:
        age = 0.0
    if hard_days > 0 and age > hard_days:
        return 0.0
    if tau_days <= 0:
        return 1.0
    return float(math.exp(-age / tau_days))


# --------------------------------------------------------------------------
# 重要程度
# --------------------------------------------------------------------------
def importance_score(cand: Candidate, rcfg: Dict[str, Any]) -> float:
    """重要程度得分 (0~1): 引用数 + 正式发表 + 顶刊关键词。"""
    scale = float(rcfg.get("citation_scale", 200.0)) or 200.0
    score = 0.0

    if cand.citations is not None and cand.citations > 0:
        # log1p 压缩长尾, 再按 citation_scale 归一化并截断到 1
        score = math.log1p(cand.citations) / math.log1p(scale)
        score = min(1.0, score)

    if cand.journal_ref:
        score += float(rcfg.get("journal_ref_bonus", 0.15))

    venue_keywords = [str(k).lower() for k in (rcfg.get("venue_keywords") or [])]
    haystack = "%s %s" % (cand.journal_ref or "", cand.comments or "")
    haystack = haystack.lower()
    if any(kw in haystack for kw in venue_keywords):
        score += float(rcfg.get("venue_bonus", 0.10))

    return min(1.0, score)


def is_famous(cand: Candidate, rcfg: Dict[str, Any]) -> bool:
    threshold = int(rcfg.get("famous_min_citations", 50))
    return cand.citations is not None and cand.citations >= threshold


def is_new(cand: Candidate, days: int = 120, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    ref = cand.published or cand.updated
    if ref is None:
        return False
    return (now - ref).total_seconds() / 86400.0 <= days


# --------------------------------------------------------------------------
# 相关性: 启发式 (TF-IDF)
# --------------------------------------------------------------------------
def _candidate_text(cand: Candidate) -> str:
    return strip_latex("%s %s" % (cand.title or "", cand.abstract or "")).lower()


def heuristic_relevance(
    profile: ResearchProfile,
    candidates: List[Candidate],
) -> Dict[str, float]:
    """用 TF-IDF 余弦相似度算相关性, 返回 {arxiv_id: 0~1}。

    把画像文本当成一个"查询文档", 与每篇候选做余弦相似度, 再归一化到 0~1。
    """
    if not candidates:
        return {}

    profile_text = strip_latex(" ".join(
        [profile.summary] + profile.topics + profile.methods + profile.keywords
    )).lower()

    docs = [_candidate_text(c) for c in candidates]
    if not profile_text.strip():
        return {c.arxiv_id: 0.5 for c in candidates}

    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        log("sklearn 不可用, 启发式相关性全部给 0.5", "warn")
        return {c.arxiv_id: 0.5 for c in candidates}

    try:
        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2),
                              sublinear_tf=True, min_df=1)
        matrix = vec.fit_transform([profile_text] + docs)
        sims = cosine_similarity(matrix[0:1], matrix[1:]).ravel()
    except Exception as exc:
        log("TF-IDF 相关性计算失败: %s" % exc, "warn")
        return {c.arxiv_id: 0.5 for c in candidates}

    # 余弦相似度绝对值通常集中在 0~0.4, 做一次拉伸让区分度更明显
    max_sim = float(sims.max()) if len(sims) else 0.0
    if max_sim <= 0:
        return {c.arxiv_id: 0.0 for c in candidates}

    out: Dict[str, float] = {}
    for cand, sim in zip(candidates, sims):
        val = float(sim) / max_sim          # 相对最强匹配归一化
        # 轻微压缩, 避免绝大多数挤在 0 附近
        out[cand.arxiv_id] = round(min(1.0, val ** 0.7), 4)
    return out


# --------------------------------------------------------------------------
# 相关性: AI 打分
# --------------------------------------------------------------------------
SCORE_SYSTEM = (
    "你是学术文献相关性评审专家。给定一位研究者的研究画像和若干候选论文, "
    "你需要判断每篇候选与该研究者当前工作的相关程度。"
    "评分要拉开档次: 真正同一细分方向、可能直接启发的给高分; "
    "只是大领域相同、方法或对象不同的给中等分; 边缘相关的给低分。"
    "不要因为都是物理就都给高分。只输出 JSON。"
)


def _score_prompt(profile_text: str, batch: List[Candidate]) -> str:
    lines = []
    for cand in batch:
        lines.append(json.dumps({
            "id": cand.arxiv_id,
            "title": strip_latex(cand.title),
            "abstract": truncate(strip_latex(cand.abstract or ""), 700, ""),
            "categories": cand.categories[:3],
            "date": cand.published.strftime("%Y-%m") if cand.published else "",
        }, ensure_ascii=False))
    return (
        "研究者画像:\n%s\n\n"
        "候选论文 (每行一个 JSON):\n%s\n\n"
        "请为每篇候选打一个 0-100 的相关性分数, 并给出一句不超过 40 字的中文理由, "
        "说明它和研究者方向的关联点 (或为什么不够相关)。\n"
        "严格按此 JSON 输出:\n"
        '{"scores": [{"id": "论文id", "score": 85, "reason": "理由"}, ...]}\n'
        "必须为上面每一篇候选都给出分数。"
        % (profile_text, "\n".join(lines))
    )


def ai_relevance(
    ai: AIClient,
    profile: ResearchProfile,
    candidates: List[Candidate],
    batch_size: int = 8,
    concurrency: int = 1,
    should_stop: Optional[Any] = None,
) -> Dict[str, Tuple[float, str]]:
    """用 AI 给候选打相关性分, 返回 {arxiv_id: (0~1, 理由)}。

    单批失败不影响整体: 失败的批次留空, 由调用方回退到启发式分数。

    ``should_stop`` 是可选回调 ``fn() -> bool``。这是整条流水线里最慢的一步
    (300 篇 / 每批 8 篇 = 38 次调用, 串行要 380 秒), 只在阶段边界查"停止"的话,
    用户点完按钮要干等好几分钟 —— 看起来就是按钮坏了。已经在飞的那几批让它跑完
    (结果仍然有用), 还没开打的直接不打。

    批次之间并发跑 (``concurrency`` > 1)。这是排序阶段里最贵的一步 —— 300 篇
    按每批 8 篇是 38 次调用, 串着跑实测要 380 秒, 而各批之间毫无依赖。写结果的
    时候加锁, 因为多个线程会往同一个 ``out`` 里塞。
    """
    profile_text = profile_digest_for_scoring(profile)
    if not profile_text.strip():
        return {}

    out: Dict[str, Tuple[float, str]] = {}
    lock = threading.Lock()
    batches = chunk(candidates, max(1, batch_size))
    done_batches = [0]

    def _parse_batch(batch: List[Candidate], result: Any) -> Dict[str, Tuple[float, str]]:
        got: Dict[str, Tuple[float, str]] = {}
        if not isinstance(result, dict):
            return got
        scores = result.get("scores")
        if not isinstance(scores, list):
            return got
        valid_ids = {c.arxiv_id for c in batch}
        for item in scores:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id") or "").strip()
            if rid not in valid_ids:
                continue
            try:
                raw = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            got[rid] = (max(0.0, min(1.0, raw / 100.0)),
                        str(item.get("reason") or "")[:120])
        return got

    stopped_at = [0]

    def _run_batch(i: int, batch: List[Candidate]) -> None:
        # 并发路径下所有 future 是一次性提交的, 排在队列里的那些要等到轮到自己
        # 才知道该不该跑。所以这个检查必须放在**批的开头**, 光在提交前查是不够的
        # —— 提交那个循环几乎是瞬间跑完的, 用户根本来不及点。
        if should_stop is not None and should_stop():
            stopped_at[0] = stopped_at[0] or i
            return
        prompt = _score_prompt(profile_text, batch)
        got: Dict[str, Tuple[float, str]] = {}
        # 失败重试一次。AI 接口偶尔会返回一个**空的 200**, 或者把回复**截断**在
        # 半截字符串上 —— 两种都不是异常, 所以 AIClient 里那层 retry_call (只兜
        # 异常) 不会管它; 也不会有缓存 (chat 只在 text 非空时才 set), 所以重试
        # 是真的会再打一次接口。
        #
        # 判定标准是"够不够用", 不是"有没有": 截断的回复里 safe_json_loads 能救回
        # 完整的那些, 但只救回 1/8 篇的话, 剩下 7 篇照样退化成关键词分数 —— 而
        # 它们本来可能是高分候选, 排序结果会跟着变。所以拿到不足一半就再打一次。
        enough = max(1, len(batch) // 2)
        for attempt in (1, 2):
            result = ai.chat_json(SCORE_SYSTEM, prompt, default=None)
            got = _parse_batch(batch, result)
            if len(got) >= enough:
                break
            if attempt == 1:
                log("  相关性打分第 %d/%d 批只拿到 %d/%d 篇, 重试一次"
                    % (i, len(batches), len(got), len(batch)), "warn")
        if not got:
            log("相关性打分第 %d/%d 批解析失败 (重试后仍为空)" % (i, len(batches)),
                "warn")
        elif len(got) < len(batch):
            log("相关性打分第 %d/%d 批有 %d/%d 篇没拿到分数, 退回启发式"
                % (i, len(batches), len(batch) - len(got), len(batch)), "dbg")
        with lock:
            out.update(got)
            done_batches[0] += 1
            n = done_batches[0]
        if n % 5 == 0 or n == len(batches):
            log("  相关性打分进度 %d/%d 批, 已评 %d 篇"
                % (n, len(batches), len(out)), "dbg")

    if concurrency and concurrency > 1 and len(batches) > 1:
        workers = max(1, min(int(concurrency), len(batches)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_batch, i, b)
                       for i, b in enumerate(batches, 1)]
            for fut in as_completed(futures):
                # 单批异常不能带走整轮: _run_batch 内部已经吞掉解析错误, 这里
                # 再兜一层网络层的意外
                exc = fut.exception()
                if exc is not None:
                    log("相关性打分有一批异常: %s" % exc, "warn")
    else:
        for i, b in enumerate(batches, 1):
            _run_batch(i, b)
            if stopped_at[0]:
                break

    if stopped_at[0]:
        log("收到停止请求, 相关性打分在第 %d/%d 批停下 (已评 %d 篇, "
            "其余退回启发式分数)" % (stopped_at[0], len(batches), len(out)), "warn")
    return out


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def rank_candidates(
    cfg: Dict[str, Any],
    profile: ResearchProfile,
    candidates: List[Candidate],
    ai: Optional[AIClient] = None,
    heur: Optional[Dict[str, float]] = None,
    should_stop: Optional[Any] = None,
) -> List[Candidate]:
    """给全部候选打分并排序, 返回按 score 降序的列表。

    ``heur`` 可以由调用方算好传进来 —— 富化阶段也要用同一份相关性来挑"值得查
    引用数的那批", TF-IDF 没必要对同一批文档算两遍。
    """
    rcfg = cfg.get("ranking", {})
    acfg = cfg.get("analysis", {})
    tau = float(rcfg.get("recency_tau_days", 365.0))
    hard = float(rcfg.get("recency_hard_days", 1460.0))
    now = datetime.now()

    if not candidates:
        return []

    # --- 1) 启发式相关性 (同时用于预筛) ---
    if heur is None:
        heur = heuristic_relevance(profile, candidates)

    # --- 2) 选一批送 AI 精评 ---
    max_for_ai = int(acfg.get("max_for_ai_scoring", 300))
    use_ai = ai is not None and not isinstance(ai, NullAIClient)
    ai_scores: Dict[str, Tuple[float, str]] = {}
    if use_ai:
        pre = sorted(candidates, key=lambda c: -heur.get(c.arxiv_id, 0.0))[:max_for_ai]
        if len(pre) < len(candidates):
            log("AI 精评前 %d 篇 (共 %d 篇, 其余用启发式分数)"
                % (len(pre), len(candidates)))
        ai_scores = ai_relevance(
            ai, profile, pre,
            batch_size=int(acfg.get("score_batch_size", 8)),
            concurrency=int(acfg.get("concurrency", 1) or 1),
            should_stop=should_stop,
        )
        log("AI 相关性打分完成: %d/%d 篇" % (len(ai_scores), len(pre)))
    else:
        log("未启用 AI, 相关性使用 TF-IDF 启发式分数", "warn")

    # --- 3) 合并三项得分 ---
    w_rel = float(rcfg.get("weight_relevance", 0.60))
    w_rec = float(rcfg.get("weight_recency", 0.20))
    w_imp = float(rcfg.get("weight_importance", 0.20))
    total_w = w_rel + w_rec + w_imp
    if total_w <= 0:
        w_rel, w_rec, w_imp, total_w = 0.6, 0.2, 0.2, 1.0

    for cand in candidates:
        if cand.arxiv_id in ai_scores:
            cand.relevance, cand.relevance_reason = ai_scores[cand.arxiv_id]
        else:
            cand.relevance = heur.get(cand.arxiv_id, 0.0)
            cand.relevance_reason = "（启发式 TF-IDF 相似度）"

        cand.recency = recency_score(cand, tau, hard, now)
        cand.importance = importance_score(cand, rcfg)
        cand.score = (w_rel * cand.relevance
                      + w_rec * cand.recency
                      + w_imp * cand.importance) / total_w

    candidates.sort(key=lambda c: (-c.score, -c.relevance,
                                   -(c.citations or 0)))
    return candidates


def apply_relevance_floor(candidates: List[Candidate],
                          min_relevance: float) -> List[Candidate]:
    """丢掉相关性过低的候选。"""
    if min_relevance <= 0:
        return candidates
    kept = [c for c in candidates if c.relevance >= min_relevance]
    if len(kept) < len(candidates):
        log("相关性低于 %.2f 的 %d 篇已丢弃"
            % (min_relevance, len(candidates) - len(kept)))
    return kept


def explain_weights(cfg: Dict[str, Any]) -> str:
    r = cfg.get("ranking", {})
    return ("权重: 相关性 %.2f / 时效性 %.2f / 重要性 %.2f | 时效衰减 τ=%.0f 天"
            % (r.get("weight_relevance", 0.6), r.get("weight_recency", 0.2),
               r.get("weight_importance", 0.2), r.get("recency_tau_days", 365)))
