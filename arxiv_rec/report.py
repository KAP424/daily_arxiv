"""报告生成: 输出 Markdown 报告 (可选同时输出 JSON)。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import Candidate, LibraryPaper, ResearchProfile, RunStats
from .rank import is_famous, is_new
from .utils import fmt_date, log, truncate


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
# 界面和报告必须写同一个日期。以前这里自己算一遍, 界面上要是再抄一遍, 同一篇
# 论文就会出现列表里 2026-09-25、报告里 2026/9/25 这种事 —— 没人会当 bug 修,
# 但它就是错的。所以统一走 utils.fmt_date。
_fmt_date = fmt_date


def _fmt_citations(cand: Candidate) -> str:
    if cand.citations is None:
        return "—"
    return str(cand.citations)


def _flags(cand: Candidate, rcfg: Dict[str, Any]) -> str:
    marks = []
    if is_new(cand):
        marks.append("🆕新")
    if is_famous(cand, rcfg):
        marks.append("⭐经典")
    if cand.journal_ref:
        marks.append("📖已发表")
    return " ".join(marks) if marks else "—"


def _bar(value: float, width: int = 10) -> str:
    """把 0~1 的分值画成字符条, 便于肉眼比较。"""
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "█" * filled + "░" * (width - filled)


def _escape_md_cell(text: str) -> str:
    """转义会破坏 Markdown 表格的字符。"""
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _clean_title(title: str) -> str:
    """标题里的 LaTeX 符号在 Markdown 里可读性差, 做轻度清理。"""
    return title.replace("$", "").strip() if title else ""


# --------------------------------------------------------------------------
# 各区块
# --------------------------------------------------------------------------
def _header(stats: RunStats, profile: ResearchProfile,
            cfg: Dict[str, Any]) -> List[str]:
    lines = [
        "# arXiv 相关文献推荐报告",
        "",
        "> 由你的本地文献库自动分析生成 · 生成时间 %s" % stats.started_at,
        "",
        "## 一、本次运行概览",
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        "| 文献库总量 | %d 篇 |" % stats.library_total,
        "| 其中有摘要 / 全文 | %d / %d 篇 |" % (stats.library_with_abstract,
                                                  stats.library_with_fulltext),
        "| 使用的检索式 | %d 条 |" % stats.queries_used,
        "| 抓取候选论文 | %d 篇 |" % stats.candidates_raw,
        "| 排除库中已有后 | %d 篇 |" % stats.candidates_after_dedup,
        "| 获得引用数据 | %d 篇 |" % stats.candidates_enriched,
        "| 完成 AI 解读 | %d 篇 |" % stats.candidates_analyzed,
        "| 耗时 | %s |" % ("%.1f 秒" % stats.elapsed_sec),
        "",
    ]
    if stats.ai_calls:
        lines.append("| AI 调用 / tokens | %d 次 / %d |" % (stats.ai_calls,
                                                              stats.ai_tokens))
        lines.append("")
    return lines


def _profile_section(profile: ResearchProfile) -> List[str]:
    lines = ["## 二、识别出的研究画像", ""]
    if profile.summary:
        lines += [profile.summary, ""]
    if profile.topics:
        lines += ["**主要研究主题**: " + "、".join(profile.topics[:15]), ""]
    if profile.methods:
        lines += ["**常用方法**: " + "、".join(profile.methods[:12]), ""]
    if profile.keywords:
        lines += ["**关键词**: " + ", ".join(profile.keywords[:35]), ""]
    lines += ["<sub>画像来源: %s</sub>" % profile.generated_by, ""]
    return lines


def _table(top: List[Candidate], rcfg: Dict[str, Any]) -> List[str]:
    lines = [
        "## 三、推荐总览",
        "",
        "评分 = 相关性 ×%.2f + 时效性 ×%.2f + 重要程度 ×%.2f"
        % (rcfg.get("weight_relevance", 0.6), rcfg.get("weight_recency", 0.2),
           rcfg.get("weight_importance", 0.2)),
        "",
        "| # | 论文 | 提交日期 | 相关性 | 时效性 | 重要性 | 总分 | 引用 | 标记 |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for i, cand in enumerate(top, 1):
        title = _escape_md_cell(_clean_title(cand.title))
        link = "[%s](%s)" % (truncate(title, 78, "…"), cand.abs_url)
        lines.append(
            "| %d | %s | %s | %.2f | %.2f | %.2f | **%.3f** | %s | %s |"
            % (i, link, _fmt_date(cand), cand.relevance, cand.recency,
               cand.importance, cand.score, _fmt_citations(cand),
               _flags(cand, rcfg))
        )
    lines.append("")
    return lines


def _detail_section(cand: Candidate, rank: int, rcfg: Dict[str, Any]) -> List[str]:
    lines = [
        "---",
        "",
        "### %d. %s" % (rank, _clean_title(cand.title)),
        "",
        "**arXiv**: [%s](%s) · **PDF**: [下载](%s)"
        % (cand.arxiv_id, cand.abs_url, cand.pdf_url),
        "",
    ]

    meta = []
    if cand.authors:
        authors = ", ".join(cand.authors)
        meta.append("**作者**: %s" % truncate(authors, 200, " 等"))
    meta.append("**提交**: %s" % _fmt_date(cand))
    if cand.updated and cand.published and cand.updated != cand.published:
        meta.append("**最近更新**: %s" % cand.updated.strftime("%Y-%m-%d"))
    if cand.categories:
        meta.append("**分类**: %s" % ", ".join(cand.categories[:5]))
    if cand.journal_ref:
        meta.append("**期刊**: %s" % truncate(_clean_title(cand.journal_ref), 120, ""))
    if cand.comments:
        meta.append("**备注**: %s" % truncate(_clean_title(cand.comments), 160, ""))
    if cand.citations is not None:
        meta.append("**引用数**: %d" % cand.citations)
    lines += [" · ".join(meta[:3]), ""]
    for m in meta[3:]:
        lines += [m, ""]

    lines += [
        "**评分**",
        "",
        "```",
        "相关性   %.2f  %s" % (cand.relevance, _bar(cand.relevance)),
        "时效性   %.2f  %s" % (cand.recency, _bar(cand.recency)),
        "重要程度 %.2f  %s" % (cand.importance, _bar(cand.importance)),
        "综合得分 %.3f" % cand.score,
        "```",
        "",
    ]
    if cand.relevance_reason:
        lines += ["*相关性判断依据: %s*" % cand.relevance_reason, ""]

    if cand.summary:
        lines += ["#### 📄 内容讲解", "", cand.summary, ""]
    else:
        lines += ["#### 📄 内容讲解", "",
                  "> 未生成 (未配置 AI, 或该篇解读失败)。以下是原始摘要:", ""]
        lines += ["> " + truncate(cand.abstract or "（无摘要）", 900, "").replace("\n", " "), ""]

    if cand.connections:
        lines += ["#### 🔗 与你的文献的关联", ""]
        for conn in cand.connections:
            lines.append("- **%s** — %s"
                         % (_escape_md_cell(conn.get("paper", "")),
                            conn.get("relation", "")))
        lines.append("")

    if cand.ideas:
        lines += ["#### 💡 可以结合的研究方向", "", cand.ideas, ""]

    if cand.abstract and cand.summary:
        lines += ["<details><summary>展开原始摘要</summary>", "",
                  _clean_title(cand.abstract), "", "</details>", ""]

    return lines


def _appendix(stats: RunStats, profile: ResearchProfile,
              candidates: List[Candidate], removed: List[Tuple[Candidate, str]],
              rcfg: Dict[str, Any]) -> List[str]:
    lines = ["---", "", "## 附录", ""]

    lines += ["### 使用的检索式", ""]
    for q in profile.queries:
        lines.append("- `%s`" % q)
    lines.append("")

    if removed:
        lines += ["### 已排除的库内文献 (%d 篇)" % len(removed), "",
                  "这些论文已经在你的文献库里, 因此不再推荐:", ""]
        for cand, reason in removed[:60]:
            lines.append("- %s — *%s*" % (truncate(_clean_title(cand.title), 90, "…"),
                                          reason))
        if len(removed) > 60:
            lines.append("- … 另有 %d 篇" % (len(removed) - 60))
        lines.append("")

    # 落选的候选里挑几篇"最近但相关性略低"的, 供参考
    rest = candidates
    if rest:
        lines += ["### 其他值得一看的候选 (按相关性)", ""]
        for cand in sorted(rest, key=lambda c: -c.relevance)[:15]:
            lines.append("- [%s](%s) — 相关性 %.2f · %s · %s"
                         % (truncate(_clean_title(cand.title), 80, "…"),
                            cand.abs_url, cand.relevance, _fmt_date(cand),
                            _flags(cand, rcfg)))
        lines.append("")

    return lines


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def build_report(
    cfg: Dict[str, Any],
    profile: ResearchProfile,
    top: List[Candidate],
    all_candidates: List[Candidate],
    removed: List[Tuple[Candidate, str]],
    stats: RunStats,
) -> str:
    """拼装完整的 Markdown 报告。"""
    rcfg = cfg.get("ranking", {})
    lines: List[str] = []
    lines += _header(stats, profile, cfg)
    lines += _profile_section(profile)
    lines += _table(top, rcfg)

    lines += ["## 四、逐篇详解", ""]
    if not top:
        lines += ["> 没有找到符合条件的推荐论文。可以尝试: 放宽 `arxiv.min_relevance`, "
                  "或增加 `arxiv.queries` 里的检索式。", ""]
    for i, cand in enumerate(top, 1):
        lines += _detail_section(cand, i, rcfg)

    # 附录里排除掉已详解的, 避免重复
    top_ids = {c.arxiv_id for c in top}
    rest = [c for c in all_candidates if c.arxiv_id not in top_ids]
    lines += _appendix(stats, profile, rest, removed, rcfg)

    lines += ["", "---", "",
              "<sub>由 daily_arxiv 生成 · 相关性评分与解读由 AI 完成, "
              "建议结合原文判断</sub>", ""]
    return "\n".join(lines)


def write_report(cfg: Dict[str, Any], markdown: str,
                 candidates: List[Candidate], profile: ResearchProfile,
                 stats: RunStats, tag: str = "") -> List[str]:
    """把报告写到磁盘, 返回生成的文件路径列表。"""
    ocfg = cfg.get("output", {})
    out_dir = ocfg.get("dir", "output")
    if not os.path.isabs(out_dir):
        from .config import project_root
        out_dir = os.path.join(project_root(), out_dir)
    os.makedirs(out_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ("_" + tag) if tag else ""
    written = []

    formats = [f.lower() for f in (ocfg.get("formats") or ["markdown"])]

    if "markdown" in formats or "md" in formats:
        md_path = os.path.join(out_dir, "arxiv_recommend_%s%s.md" % (stamp, suffix))
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        written.append(md_path)

    if "json" in formats:
        json_path = os.path.join(out_dir, "arxiv_recommend_%s%s.json" % (stamp, suffix))
        payload = {
            "generated_at": stats.started_at,
            "stats": stats.to_dict(),
            "profile": profile.to_dict(),
            "recommendations": [c.to_dict() for c in candidates],
        }
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        written.append(json_path)

    return written
