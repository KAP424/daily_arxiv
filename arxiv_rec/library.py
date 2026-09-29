"""统一文献库: 把配置里所有 PDF 文件夹读成一个 ``LibraryPaper`` 列表。

只认本地 PDF 文件 —— 不读 Zotero 数据库, 也不依赖 Zotero 的任何东西。文件名
里带不带 "作者 - 年份 - 标题" 这种 Zotero 导出格式都无所谓, 那只是标题定位
失败时的兜底线索 (见 ``pdf_library._title_from_filename``)。

多个文件夹之间会互相去重: 同一篇文献在两个目录里各存一份时只算一篇, 缺的
字段互相补齐。

下游 (画像 / 去重 / 排序 / 报告) 只认 ``LibraryPaper`` 列表。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .dedup import build_index, find_in_library, index_one
from .models import Candidate, LibraryPaper
from .utils import log, truncate


class SourceStats:
    """这次读取的统计, 供界面和日志显示。"""

    def __init__(self) -> None:
        self.pdf_total = 0
        self.pdf_stats: Dict[str, Any] = {}
        self.pdf_error = ""
        self.merged_duplicates = 0      # 跨文件夹重复, 只补信息
        self.final_total = 0
        self.capped = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pdf_total": self.pdf_total,
            "pdf_stats": self.pdf_stats,
            "pdf_error": self.pdf_error,
            "merged_duplicates": self.merged_duplicates,
            "final_total": self.final_total,
            "capped": self.capped,
        }

    def summary(self) -> str:
        lines = []
        if self.pdf_error:
            lines.append("  PDF 目录   : 读取失败 (%s)" % self.pdf_error)
        else:
            lines.append("  PDF 目录   : %d 篇" % self.pdf_total)
            if self.merged_duplicates:
                lines.append("  其中跨文件夹重复, 仅合并信息: %d 篇"
                             % self.merged_duplicates)
        lines.append("  合并后总计 : %d 篇" % self.final_total)
        if self.capped:
            lines.append("  超出上限被截断: %d 篇 (调 library.max_papers_for_profile)"
                         % self.capped)
        return "\n".join(lines)


def _absorb(base: LibraryPaper, extra: LibraryPaper) -> List[str]:
    """把 ``extra`` 里 base 缺失的信息补进去, 返回补了哪些字段。"""
    filled = []
    pairs = (
        ("abstract", "摘要"), ("fulltext", "全文"), ("doi", "DOI"),
        ("arxiv_id", "arXiv ID"), ("publication", "期刊"),
        # context 必须补。引言/结论是按读取深度抽出来的, 同一个文件在两个文件夹
        # 里各有一份时, 先读到的那份可能是浅档位、后读到的才是深档位; 漏掉这一
        # 项的话合并后 context 就丢了, 看起来像"读取深度设置没生效"。
        ("context", "引言/结论"),
    )
    for attr, label in pairs:
        if not getattr(base, attr, "") and getattr(extra, attr, ""):
            setattr(base, attr, getattr(extra, attr))
            filled.append(label)
    if not base.authors and extra.authors:
        base.authors = list(extra.authors)
        filled.append("作者")
    if not base.year and extra.year:
        base.year = extra.year
        filled.append("年份")
    if not base.title and extra.title:
        base.title = extra.title
        filled.append("标题")
    return filled


def order_papers(papers: List[LibraryPaper],
                 max_papers: int = 0) -> List[LibraryPaper]:
    """按"年份倒序 + 标题"排序, 再按上限截断。返回新列表。

    次级键**必须是内容派生的** (这里用标题), 不能依赖输入顺序。只按年份排是
    稳定排序, 并列的那些会保持原顺序 —— 而原顺序在"新建索引"和"复用索引"
    两条路径上并不一样 (遍历文件夹 vs 读数据库)。实测同一份文献库 (183 篇
    截到 60 篇, 大量同年): 两次运行排出来的顺序不同, 内容一模一样只是次序换了。

    后果不只是"看着别扭": 研究画像的提示词是按这个顺序拼的, 顺序一变提示词就变,
    而 AI 缓存正是拿提示词当 key —— 第二次运行的画像调用会**全部落空**, 白花
    token, 同一份输入也复现不出同一份报告。所以这里刻意做成可单独测试的纯函数。
    """
    def _key(p: LibraryPaper) -> Any:
        try:
            y = int(p.year or 0)
        except (TypeError, ValueError):
            y = 0
        return (-y, (p.title or "").lower())

    out = sorted(papers, key=_key)
    if max_papers > 0 and len(out) > max_papers:
        out = out[:max_papers]
    return out


def load_library(
    cfg: Dict[str, Any],
    force_pdf: bool = False,
    progress: Optional[Any] = None,
) -> Tuple[List[LibraryPaper], SourceStats]:
    """读取并合并配置里所有 PDF 文件夹。

    ``progress`` 是可选回调 ``fn(done, total, path)``, 供 UI 显示解析进度。

    ``force_pdf`` 忽略索引重新解析全部文件。
    """
    stats = SourceStats()
    papers: List[LibraryPaper] = []

    folders = [f for f in (cfg.get("pdf_folders") or [])
               if isinstance(f, dict) and f.get("enabled", True)
               and str(f.get("path") or "").strip()]
    if not folders:
        log("没有配置任何文献文件夹, 文献库为空。请在\"设置\"里添加 PDF 目录。", "warn")
        return papers, stats

    from .pdf_library import PdfBackendUnavailable, read_pdf_library
    try:
        papers, pdf_stats = read_pdf_library(
            cfg, force=force_pdf, progress=progress)
        stats.pdf_total = len(papers)
        stats.pdf_stats = pdf_stats.to_dict()
    except PdfBackendUnavailable as exc:
        stats.pdf_error = str(exc)[:200]
        log(str(exc), "err")
        return [], stats
    except Exception as exc:
        stats.pdf_error = str(exc)[:200]
        log("读取 PDF 目录失败: %s" % exc, "err")
        return [], stats

    # ---------------- 跨文件夹去重 ----------------
    merged: List[LibraryPaper] = []
    index = build_index(merged)
    for p in papers:
        cand = Candidate(arxiv_id=p.arxiv_id, title=p.title, doi=p.doi)
        pos, _ = find_in_library(cand, index)
        if pos is None:
            merged.append(p)
            index_one(index, p, len(merged) - 1)
        else:
            filled = _absorb(merged[pos], p)
            stats.merged_duplicates += 1
            if filled:
                log("同一篇文献出现在多个文件夹, 补入 %s: %s"
                    % ("/".join(filled), truncate(merged[pos].title, 50, "…")),
                    "dbg")
    papers = merged
    if stats.merged_duplicates:
        log("去重: %d 篇在多个文件夹里重复, 未重复计入"
            % stats.merged_duplicates)

    # ---------------- 上限与排序 ----------------
    max_papers = int(cfg.get("library", {}).get("max_papers_for_profile", 200))
    # 截断已经在 order_papers 里做了, 这里拿截断**前**的篇数来报"砍掉了多少" ——
    # 用截断后的 len(papers) 判断的话条件永远不成立, capped 会一直是 0,
    # 上限生效了却什么都不提示。
    before = len(papers)
    papers = order_papers(papers, max_papers)
    if max_papers > 0 and before > max_papers:
        stats.capped = before - max_papers
        log("文献数 %d 超过上限 %d, 按年份保留较新的 %d 篇用于画像"
            % (before, max_papers, max_papers), "warn")

    # 重新编号: 保证 item_id 唯一
    for i, p in enumerate(papers):
        p.item_id = i + 1

    stats.final_total = len(papers)
    log("文献库共 %d 篇" % len(papers))
    return papers, stats


def library_summary(papers: List[LibraryPaper]) -> str:
    """给 UI 用的一句话摘要。"""
    n = len(papers)
    if not n:
        return "文献库为空"
    n_abs = sum(1 for p in papers if p.abstract)
    n_ctx = sum(1 for p in papers if p.context)
    n_id = sum(1 for p in papers if p.arxiv_id or p.doi)
    years = [p.year for p in papers if p.year]
    span = "%d-%d" % (min(years), max(years)) if years else "年份未知"
    return ("共 %d 篇 · 有摘要 %d · 有引言/结论 %d · 有 arXiv ID 或 DOI %d · %s"
            % (n, n_abs, n_ctx, n_id, span))
