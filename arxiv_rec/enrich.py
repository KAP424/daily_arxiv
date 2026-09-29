"""候选论文富化: 用 OpenAlex 补充引用数等"重要程度"信号。

OpenAlex 免费且无需 key, 支持按 DOI 批量查询。arXiv 预印本都有自动分配的
DOI (``10.48550/arXiv.XXXX.XXXXX``), 因此即便候选本身没写 DOI 也能查。

这个接口在共享出口 IP 下容易被限流 (429), 所以:
  * 批量查询, 减少请求数;
  * 失败不致命, 只是重要性评分退化为启发式。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .models import Candidate
from .utils import (DiskCache, RateLimiter, build_session, chunk, http_get, log,
                    norm_doi, truncate)

OPENALEX_URL = "https://api.openalex.org/works"
# OpenAlex 单次 OR 过滤的取值数量上限较宽松, 25 是个稳妥值
BATCH_SIZE = 25
# 连续几批整批失败就认定这个出口被限流了, 不再往下试。见 enrich_candidates。
MAX_CONSECUTIVE_FAILURES = 3
# 礼貌池标识, 能拿到更稳定的配额
POLITE_MAILTO = "arxiv-recommender@example.com"
# OpenAlex 单独一道闸门, 不和 arXiv 共用。
#
# 共用是有代价的: 全局闸门是按 arXiv 的"每 3 秒 1 个请求"定的, 而 OpenAlex 在
# 礼貌池里允许每秒 10 个 —— 富化几十批本来几秒钟的事, 排在 arXiv 那条 3 秒一格的
# 队里要等上一分钟。反过来, OpenAlex 偶尔的 429 还会把闸门整体往后推, 连累后面
# 的 arXiv 请求一起干等。
# 2 个/秒: 比 OpenAlex 的上限保守得多 (不至于因为我们被限流), 又比 3 秒一格快得多。
OPENALEX_LIMITER = RateLimiter(0.5)


def _candidate_doi(cand: Candidate) -> str:
    """取候选的查询用 DOI: 优先真实 DOI, 否则用 arXiv 自动 DOI。"""
    if cand.doi:
        return norm_doi(cand.doi)
    if cand.arxiv_id:
        return norm_doi("10.48550/arXiv.%s" % cand.arxiv_id)
    return ""


def _query_batch(session: Any, dois: List[str], cache: Optional[DiskCache],
                 timeout: int, retries: int,
                 ) -> Tuple[Optional[Dict[str, Dict[str, Any]]], bool]:
    """批量查一批 DOI。

    返回 ``(结果, 是否被限流)``。**整批失败时结果是 None 而不是空字典** —— 调用方
    要靠它区分"这批问失败了"和"这批问成功但 OpenAlex 上就是没有这几篇"。分不出来
    的话, 一个被限流的出口会傻乎乎地把剩下二十几批全部问完: 实测有一轮 32 批全
    429, 白等 8 分钟, 最后一篇引用数都没拿到。

    第二个返回值单独标出 429, 因为那是**确定性**的 —— 出口 IP 被限流了, 下一批
    马上再问也还是 429, 没必要凑满"连续 3 批"才熔断。
    """
    params = {
        "filter": "doi:" + "|".join(dois),
        "per-page": str(len(dois)),
        "select": "doi,cited_by_count,publication_date,type,referenced_works_count,"
                  "concepts,primary_location,ids",
        "mailto": POLITE_MAILTO,
    }
    try:
        text = http_get(session, OPENALEX_URL, params=params, cache=cache,
                        retries=retries, base_delay=2.0, timeout=timeout,
                        limiter=OPENALEX_LIMITER)
    except Exception as exc:
        msg = str(exc)
        log("OpenAlex 查询失败: %s" % truncate(msg, 120), "warn")
        return None, ("429" in msg or "Too Many" in msg)

    try:
        data = json.loads(text)
    except Exception:
        log("OpenAlex 返回的不是 JSON", "warn")
        return None, False

    out: Dict[str, Dict[str, Any]] = {}
    for work in data.get("results") or []:
        doi = norm_doi(work.get("doi") or "")
        if doi:
            out[doi] = work
        # OpenAlex 返回的 DOI 可能带 https://doi.org/ 前缀, 已由 norm_doi 处理
    return out, False


def enrich_candidates(
    cfg: Dict[str, Any],
    candidates: List[Candidate],
    cache: Optional[DiskCache] = None,
    heur: Optional[Dict[str, float]] = None,
    limit: int = 0,
    should_stop: Optional[Any] = None,
) -> int:
    """为候选补充引用数等元数据, 返回成功富化的篇数。

    就地修改 ``candidates`` 里的对象。

    ``should_stop`` 是可选回调 ``fn() -> bool``。整轮 32 批是流水线里最慢的几步
    之一, 只在阶段边界检查"停止"的话, 用户点完按钮还要干等好几分钟, 看起来就
    跟按钮坏了一样。检查点放在**批之间**, 已经拿到的引用数照常保留。

    ``heur`` 是 ``{arxiv_id: 相关性}``。给了 ``limit`` (>0) 且候选多于 ``limit``
    时, 只去查最相关的那 ``limit`` 篇 —— 排在后面的候选本来就进不了 top_n, 为它们
    各查一次 OpenAlex 纯属浪费配额 (实测一轮 793 篇要 32 批, 是整轮最慢的一步)。
    被跳过的那些照样置 ``enriched``, 免得下游以为"还没查过"。
    """
    ncfg = cfg.get("network", {})
    timeout = int(ncfg.get("timeout", 40))
    # 只试一次。富化是"锦上添花", 而且失败模式基本就是被限流 —— 那种情况下
    # 重试只是把 429 再听一遍, 每次还要退避好几秒。真挂了几批, 下面的熔断会
    # 直接把整段收掉, 所以重试在这里没有价值。
    retries = 1

    targets = [c for c in candidates if not c.enriched]
    if not targets:
        return 0

    session = build_session(ncfg.get("proxy"), timeout=timeout)
    todo = len(targets)
    dropped = 0
    if limit > 0 and len(targets) > limit:
        # 没有相关性分数时按原顺序取 (至少是"检索命中的先后"), 有分数就按分数排
        if heur:
            targets.sort(key=lambda c: -heur.get(c.arxiv_id, 0.0))
        dropped = len(targets) - limit
        for cand in targets[limit:]:
            cand.enriched = True
        targets = targets[:limit]

    # 建立 doi -> [candidate] 的映射 (多篇候选可能共享 DOI)
    by_doi: Dict[str, List[Candidate]] = {}
    no_doi: List[Candidate] = []
    for cand in targets:
        doi = _candidate_doi(cand)
        if doi:
            by_doi.setdefault(doi, []).append(cand)
        else:
            no_doi.append(cand)

    all_dois = list(by_doi.keys())
    log("富化: %d 篇候选待查询 (其中 %d 篇无 DOI)" % (todo, len(no_doi)))
    if dropped:
        log("富化: 按相关性只查最靠前的 %d 篇, 其余 %d 篇按无引用数据计分"
            % (limit, dropped))

    enriched = 0
    batches = chunk(all_dois, BATCH_SIZE)
    dead = 0          # 连续整批失败次数
    for i, batch in enumerate(batches, 1):
        if should_stop is not None and should_stop():
            log("收到停止请求, 富化在第 %d/%d 批停下 (已拿到的引用数据照常使用)"
                % (i - 1, len(batches)), "warn")
            break
        works, throttled = _query_batch(session, batch, cache, timeout, retries)
        if works is None:
            dead += 1
            # 熔断: OpenAlex 在共享出口上被限流时是**持续**的, 不是偶发。实测
            # 一轮 32 批全部 429, 每批还各自退避重试两次, 白等 8 分钟、最后一篇
            # 引用数都没拿到。连挂几批就说明这个出口现在查不动了, 直接收工 ——
            # 引用数本来就只是"重要程度"里的一项, 缺了不影响出推荐。
            # 已经看到 429 就不用再凑满 3 批: 那说明 IP 正在被限流, 下一批
            # 立刻再问必然还是 429。
            if throttled or dead >= MAX_CONSECUTIVE_FAILURES:
                log("OpenAlex 被限流 (429), 剩余 %d 批不再尝试" % (len(batches) - i)
                    if throttled else
                    "OpenAlex 连续 %d 批失败, 剩余 %d 批不再尝试"
                    % (dead, len(batches) - i), "warn")
                log("  重要性评分将只用 arXiv 元数据 (期刊/顶刊加成); "
                    "等一会儿重跑可拿到引用数", "warn")
                break
            continue
        dead = 0
        for doi, cands in ((d, by_doi[d]) for d in batch):
            work = works.get(doi)
            if not work:
                continue
            for cand in cands:
                _apply(cand, work)
                enriched += 1
        if i % 5 == 0 or i == len(batches):
            log("  富化进度 %d/%d 批, 已命中 %d 篇"
                % (i, len(batches), enriched), "dbg")

    # 无 DOI 的候选标记为已处理, 避免重复尝试
    for cand in no_doi:
        cand.enriched = True

    # 中途熔断时, 后面那几批压根没问过。照样标成"处理过" —— 这个标记的含义是
    # "这一轮不用再为它查引用了", 而它们确实不会再被查。留着不标只会让下游
    # (或下一次调用) 以为"还没试过", 平白多出一层需要解释的状态。
    for cand in targets:
        cand.enriched = True

    log("富化完成: %d/%d 篇拿到引用数据" % (enriched, len(targets)))
    if dropped:
        log("  (另有 %d 篇排在后面, 这一轮没有去查引用数)" % dropped, "dbg")
    return enriched


def _apply(cand: Candidate, work: Dict[str, Any]) -> None:
    """把 OpenAlex 的一条记录写回候选。"""
    cited = work.get("cited_by_count")
    if isinstance(cited, int):
        cand.citations = cited

    concepts = work.get("concepts") or []
    if isinstance(concepts, list):
        names = []
        for c in concepts[:6]:
            if isinstance(c, dict) and c.get("display_name"):
                # OpenAlex 的 concepts 带 score, 只保留较相关的
                if float(c.get("score") or 0) >= 0.3:
                    names.append(str(c["display_name"]))
        cand.concepts = names

    if not cand.journal_ref:
        loc = work.get("primary_location") or {}
        source = loc.get("source") or {}
        name = str(source.get("display_name") or "").strip()
        # OpenAlex 会把 arXiv 本身当作"来源", 名字形如 "arXiv (Cornell University)"。
        # 这既不是正式发表, 也不能当作期刊信息 —— 写进去会让报告显示一个假的
        # "期刊", 还会让 importance_score 给纯预印本白送 journal_ref_bonus。
        if name and not _is_preprint_source(name):
            cand.journal_ref = name

    cand.enriched = True


# 预印本仓库: 它们的"来源名"不代表正式发表
_PREPRINT_MARKERS = (
    "arxiv", "biorxiv", "medrxiv", "chemrxiv", "ssrn", "research square",
    "preprints.org", "hal ", "osf preprint", "techrxiv", "eartharxiv",
)


def _is_preprint_source(name: str) -> bool:
    """判断 OpenAlex 的来源名是否只是一个预印本仓库。"""
    low = name.lower()
    return any(marker in low for marker in _PREPRINT_MARKERS)
