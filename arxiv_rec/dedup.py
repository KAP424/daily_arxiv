"""去重: 把候选论文里已经存在于本地文献库中的剔除。

三层判定, 由严到宽:
  1. arXiv ID 完全相同
  2. DOI 相同 (含 arXiv 自动分配的 10.48550/arXiv.*)
  3. 标题归一化后相同, 或高度相似 (覆盖"预印本 vs 正式发表版"的情况)
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import Candidate, LibraryPaper
from .utils import log, norm_doi

# 标题相似度阈值。0.90 能抓住"改标点/大小写差异"的同名论文,
# 又不会把真正不同的论文误判成重复。
TITLE_SIM_THRESHOLD = 0.90
# 短标题容易误判, 提高阈值
SHORT_TITLE_LEN = 40
# 包含式匹配的最短长度。"A review" 与 "A review of recent progress" 这类
# 标题+副标题的变体, 编辑距离相似度只有 ~0.83, 靠阈值抓不到, 但它们确实是
# 同一篇论文 (常见于预印本 vs 正式版)。长度够长时, 严格包含即可判定为同一篇。
CONTAINMENT_MIN_LEN = 40

# 连载论文的分集标记。长标题在短标题之后接着的是这种东西时, 两篇是**不同的论文**,
# 不是同一篇的"标题+副标题"变体。
#
# 实测踩到的坑: Hubbard 1963 年那组 "Electron correlations in narrow energy bands"
# 一共五篇 (I~V), 后四篇的标题都是第一篇 + ". II. The Degenerate Band Case" 这种
# 后缀。严格包含判断会把它们全部并进第一篇, 一次丢掉四篇真论文。
#
# 假阴性的代价只是"库里多留一篇" (去重没生效), 假阳性却是"真论文消失", 两者不对等,
# 所以这里宁可判得保守。罗马数字要求后面跟分隔符或结尾, 否则 "IV Characteristics"
# 这类正常副标题会被误伤。
_RE_SERIES_TAIL = re.compile(
    r"^[\s.,;:—\-–]*"
    r"(?:[IVXL]{1,6}\.?(?=[\s:.—\-–]|$)"
    r"|\d{1,2}\.?(?=[\s:.—\-–]|$)"
    r"|part\b|pt\.?\b)",
    re.IGNORECASE)


def _is_series_continuation(shorter: str, longer: str) -> bool:
    """``longer`` 是否只是 ``shorter`` 的**下一集** (而非同一篇的副标题变体)。"""
    if shorter not in longer:
        return False
    idx = longer.index(shorter)
    return bool(_RE_SERIES_TAIL.match(longer[idx + len(shorter):]))


def _significant_tokens(title: str) -> Set[str]:
    """取标题里较长的词, 用于快速预筛候选比对对象。"""
    return {t for t in title.split() if len(t) >= 5}


def build_index(papers: List[LibraryPaper]) -> Dict[str, Any]:
    """预先建好用于比对的索引。

    各张表都映射到文献在 ``papers`` 里的下标, 这样命中之后能直接拿到是
    哪一篇 —— 同一个文件出现在多个 PDF 文件夹里时, 要往那一篇里补字段。
    """
    arxiv_ids: Dict[str, int] = {}
    dois: Dict[str, int] = {}
    titles: Dict[str, int] = {}              # 归一化标题 -> 下标
    token_index: Dict[str, List[str]] = {}   # 词 -> 归一化标题列表
    ids: Dict[str, Tuple[str, str]] = {}     # 归一化标题 -> (arXiv ID, DOI)

    for i, p in enumerate(papers):
        if p.arxiv_id:
            arxiv_ids.setdefault(p.arxiv_id.lower(), i)
        if p.doi:
            dois.setdefault(norm_doi(p.doi), i)
        nt = p.norm_title
        if not nt:
            continue
        titles.setdefault(nt, i)
        ids.setdefault(nt, (p.arxiv_id.lower(), norm_doi(p.doi)))
        for tok in _significant_tokens(nt):
            token_index.setdefault(tok, []).append(nt)

    return {
        "papers": list(papers),
        "arxiv_ids": arxiv_ids,
        "dois": dois,
        "titles": titles,
        "token_index": token_index,
        "ids": ids,
    }


def index_one(index: Dict[str, Any], paper: LibraryPaper, pos: int) -> None:
    """把一篇新文献补进已有索引, 下标为 ``pos``。

    合并多个来源的文献库时要边加边查, 否则同一篇论文若在两个来源里都以
    "新面孔"出现, 会被收进来两次。
    """
    if paper.arxiv_id:
        index["arxiv_ids"].setdefault(paper.arxiv_id.lower(), pos)
    if paper.doi:
        index["dois"].setdefault(norm_doi(paper.doi), pos)
    nt = paper.norm_title
    if not nt:
        return
    index["titles"].setdefault(nt, pos)
    index["ids"].setdefault(nt, (paper.arxiv_id.lower(), norm_doi(paper.doi)))
    for tok in _significant_tokens(nt):
        index["token_index"].setdefault(tok, []).append(nt)


def _ident_conflict(cand: Candidate, other_ids: Tuple[str, str]) -> bool:
    """候选与库中这篇的**显式标识符**是否互相矛盾。

    arXiv ID 和 DOI 都是唯一标识。两边都给出了值却各不相同, 那就只能是两篇不同的
    论文 —— 标题再像也不能合并。实测有一对标题相似度 0.99 的
    ("Universal nonequilibrium..." / "Universal non-equilibrium..."), 但 arXiv ID
    分别是 1103.4662 和 1106.4078, 是一对孪生论文, 不是同一篇。

    这里只处理"双方都有值且不同"; 一边缺失时不算矛盾 —— 正式版 PDF 常常没有 arXiv
    戳记, 那正是要靠标题兜底的情形。
    """
    other_aid, other_doi = other_ids
    if cand.arxiv_id and other_aid and cand.arxiv_id.lower() != other_aid:
        return True
    cand_doi = norm_doi(cand.doi) if cand.doi else ""
    if cand_doi and other_doi and cand_doi != other_doi:
        return True
    return False


def find_in_library(cand: Candidate,
                    index: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """在库中查找候选论文, 返回 ``(命中的文献下标, 原因)``。

    没命中时返回 ``(None, "")``。``is_in_library`` 是它的布尔包装。
    """
    # 1) arXiv ID
    if cand.arxiv_id and cand.arxiv_id.lower() in index["arxiv_ids"]:
        return index["arxiv_ids"][cand.arxiv_id.lower()], "arXiv ID 相同"

    # 2) DOI (候选没有 DOI 时, 用 arXiv 自动 DOI 形式再试一次)
    doi_candidates = []
    if cand.doi:
        doi_candidates.append(norm_doi(cand.doi))
    if cand.arxiv_id:
        doi_candidates.append(norm_doi("10.48550/arXiv.%s" % cand.arxiv_id))
    for d in doi_candidates:
        if d and d in index["dois"]:
            return index["dois"][d], "DOI 相同"

    # 3) 标题
    nt = cand.norm_title
    if not nt:
        return None, ""
    if nt in index["titles"]:
        return index["titles"][nt], "标题相同"

    # 4) 标题模糊匹配 (只和共享长词的库内标题比, 避免 O(N*M) 全比)
    pool: Set[str] = set()
    for tok in _significant_tokens(nt):
        for other in index["token_index"].get(tok, []):
            pool.add(other)
    if not pool:
        return None, ""

    threshold = TITLE_SIM_THRESHOLD
    if len(nt) < SHORT_TITLE_LEN:
        threshold = 0.95

    # 先做包含式判断: 同一篇论文的"标题+副标题"变体, 相似度只有 ~0.8, 阈值抓不到,
    # 但短标题被长标题完整包含时基本可以确定是同一篇。
    for other in pool:
        shorter, longer = (nt, other) if len(nt) <= len(other) else (other, nt)
        if len(shorter) >= CONTAINMENT_MIN_LEN and shorter in longer:
            if _is_series_continuation(shorter, longer):
                continue
            if _ident_conflict(cand, index["ids"].get(other, ("", ""))):
                continue
            return index["titles"].get(other), "标题为包含关系 (副标题变体)"

    for other in pool:
        # 长度差太多直接跳过, 省时间
        if abs(len(other) - len(nt)) > max(20, 0.35 * len(nt)):
            continue
        sm = SequenceMatcher(None, nt, other)
        # quick_ratio 是 ratio 的**上界** (它只数字符出现次数, 不找匹配块),
        # 所以先拿它挡一道不会漏掉任何该命中的 —— 判定结果和不加它完全一致。
        # 实测这一步原来占整个去重的 90% (37865 次 ratio() 里绝大多数是
        # 明显不相干的标题), 加上它之后快一个量级。
        if sm.quick_ratio() < threshold:
            continue
        ratio = sm.ratio()
        if ratio >= threshold:
            if _ident_conflict(cand, index["ids"].get(other, ("", ""))):
                continue
            return index["titles"].get(other), "标题高度相似 (%.2f)" % ratio
    return None, ""


def is_in_library(cand: Candidate, index: Dict[str, Any]) -> Tuple[bool, str]:
    """判断单篇候选是否已在库中, 返回 (是否命中, 原因)。"""
    pos, reason = find_in_library(cand, index)
    return (pos is not None), reason


def filter_library(
    candidates: List[Candidate],
    papers: List[LibraryPaper],
) -> Tuple[List[Candidate], List[Tuple[Candidate, str]]]:
    """过滤掉库中已有的论文。

    返回 ``(保留的候选, [(被剔除的候选, 原因), ...])``。
    """
    index = build_index(papers)
    kept: List[Candidate] = []
    removed: List[Tuple[Candidate, str]] = []

    for cand in candidates:
        hit, reason = is_in_library(cand, index)
        if hit:
            removed.append((cand, reason))
        else:
            kept.append(cand)

    log("去重: 剔除库中已有 %d 篇, 保留 %d 篇" % (len(removed), len(kept)))
    return kept, removed


def dedup_candidates(candidates: List[Candidate]) -> List[Candidate]:
    """候选内部去重 (按 arXiv ID 与标题), 兜底用。"""
    seen_ids: Set[str] = set()
    seen_titles: Set[str] = set()
    out: List[Candidate] = []
    for cand in candidates:
        key = cand.arxiv_id.lower()
        nt = cand.norm_title
        if key and key in seen_ids:
            continue
        if nt and nt in seen_titles:
            continue
        if key:
            seen_ids.add(key)
        if nt:
            seen_titles.add(nt)
        out.append(cand)
    return out
