"""数据模型定义。

本模块是各子模块之间的唯一接口契约, 所有字段含义在此固定。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# 用户文献库侧
# --------------------------------------------------------------------------
@dataclass
class LibraryPaper:
    """用户文献库里的一篇文献。"""

    item_id: int
    key: str = ""                       # 稳定的短标识
    title: str = ""
    abstract: str = ""
    date: str = ""                      # 原始日期字符串
    year: Optional[int] = None
    doi: str = ""
    url: str = ""
    authors: List[str] = field(default_factory=list)
    publication: str = ""               # journal / repository
    item_type: str = ""                 # journalArticle / preprint / ...
    arxiv_id: str = ""                  # 规范化后的 arXiv ID (无版本号)
    fulltext: str = ""                  # 全文节选 (按 library.read_depth 抽取)
    # 送进提示词的补充上下文, 由 library.read_depth 决定内容:
    #   metadata -> ""            (只送标题 + 摘要)
    #   sections -> 引言 + 结论
    #   fulltext -> 全文节选
    # 和 fulltext 分开是因为两者的用途不同: fulltext 是"抽出来了多少"的
    # 事实记录 (统计要用), context 是"这次要送多少"的策略。
    context: str = ""
    tags: List[str] = field(default_factory=list)

    @property
    def norm_title(self) -> str:
        from .utils import normalize_title
        return normalize_title(self.title)

    @property
    def short_label(self) -> str:
        """报告中引用该文献时用的短标签: 第一作者姓 + 年份。"""
        first = ""
        if self.authors:
            first = self.authors[0].split()[-1] if self.authors[0].split() else ""
        if first and self.year:
            return "%s %d" % (first, self.year)
        if first:
            return first
        return (self.title or "Untitled")[:40]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # 报告里不需要全文和提示词上下文, 避免体积爆炸
        d.pop("fulltext", None)
        d.pop("context", None)
        return d


@dataclass
class ResearchProfile:
    """由用户文献库归纳出的研究画像, 用于生成检索式与相关性打分。"""

    summary: str = ""                                   # 一段话概括研究方向
    topics: List[str] = field(default_factory=list)     # 主要研究主题
    methods: List[str] = field(default_factory=list)    # 常用方法/技术
    keywords: List[str] = field(default_factory=list)   # 高频关键词
    queries: List[str] = field(default_factory=list)    # 生成的 arXiv 检索式
    generated_by: str = ""                              # "ai" 或 "heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# arXiv 侧
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    """一篇候选 arXiv 论文, 贯穿检索 -> 排序 -> 解读全流程。"""

    arxiv_id: str                       # 规范化 (无版本号), 主键
    title: str = ""
    abstract: str = ""
    authors: List[str] = field(default_factory=list)
    published: Optional[datetime] = None    # v1 提交时间
    updated: Optional[datetime] = None      # 最新版本时间
    announced: str = ""                     # 原始 "Submitted ..." 文本
    primary_category: str = ""
    categories: List[str] = field(default_factory=list)
    comments: str = ""
    journal_ref: str = ""
    doi: str = ""
    version: str = ""
    source_queries: List[str] = field(default_factory=list)  # 由哪些检索式命中

    # --- 富化字段 (OpenAlex 等) ---
    citations: Optional[int] = None
    influential_citations: Optional[int] = None
    concepts: List[str] = field(default_factory=list)
    # "这一轮不用再为它去查引用了" —— 成功查到、查过但没数据、以及因为排得太靠后
    # 而压根没去查的, 都置 True。它标记的是"尝试过了", 不是"拿到了数据"。
    enriched: bool = False

    # --- 评分字段 (0~1) ---
    relevance: float = 0.0
    relevance_reason: str = ""
    recency: float = 0.0
    importance: float = 0.0
    score: float = 0.0

    # --- AI 解读 ---
    summary: str = ""                   # 大致内容讲解
    connections: List[Dict[str, str]] = field(default_factory=list)
    ideas: str = ""                     # 可结合的研究方向
    analyzed: bool = False
    reused: bool = False                # 解读是从推荐记录里复用的, 没有调 AI

    # --- 推荐记录 ---
    seen_before: bool = False           # 以前推荐过这篇
    seen_times: int = 0                 # 加上这次, 一共推荐过几次
    last_recommended: str = ""          # 上次推荐的时间
    # 这一条是从记录库**读回来**的 (选「全部累计记录」铺出来的那张列表), 不是这
    # 一轮跑出来的。区别在于: 相关度/时效/重要性那三个分数没有存进库里, 拿回来
    # 一律是 0.00 —— 详解面板要是照常列出来, 就成了"这篇论文三项全 0 分", 是在
    # 报假数据。所以详情里看到这个标志就跳过那一段。
    from_history: bool = False
    record_report: str = ""             # 这条记录当时写进了哪份报告
    # 这一条是从**某一轮的报告**里读回来的 (「文献推荐」页那个"推荐记录"下拉框),
    # 同样不是这一轮跑出来的。和 from_history 的区别: 报告里写着那一轮的总分,
    # 所以详情里能写出分数 —— 缺的只是相关/时效/重要这三项细分。
    from_run: bool = False

    @property
    def norm_title(self) -> str:
        from .utils import normalize_title
        return normalize_title(self.title)

    @property
    def abs_url(self) -> str:
        return "https://arxiv.org/abs/%s" % self.arxiv_id if self.arxiv_id else ""

    @property
    def pdf_url(self) -> str:
        return "https://arxiv.org/pdf/%s" % self.arxiv_id if self.arxiv_id else ""

    @property
    def uid(self) -> str:
        """稳定唯一标识, 用于缓存 key。"""
        raw = (self.arxiv_id or self.norm_title).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:16]

    @property
    def age_days(self) -> Optional[float]:
        if self.published is None:
            return None
        delta = datetime.now() - self.published
        return delta.total_seconds() / 86400.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k in ("published", "updated"):
            if d.get(k) is not None:
                d[k] = d[k].isoformat()
        return d


@dataclass
class RunStats:
    """一次运行的统计信息, 写入报告头部。"""

    library_total: int = 0
    library_with_abstract: int = 0
    library_with_fulltext: int = 0
    queries_used: int = 0
    candidates_raw: int = 0
    candidates_after_dedup: int = 0
    candidates_enriched: int = 0
    candidates_analyzed: int = 0
    ai_calls: int = 0
    ai_tokens: int = 0
    started_at: str = ""
    elapsed_sec: float = 0.0

    # --- 推荐记录 (见 history.py) ---
    recommended_seen: int = 0           # 候选里以前推荐过的篇数
    recommended_skipped: int = 0        # 其中被跳过的篇数
    analysis_reused: int = 0            # 直接复用旧解读、没花 token 的篇数
    analysis_new: int = 0               # 这次真正调 AI 解读的篇数

    # 各阶段耗时 (秒), 键是 pipeline.STAGES。跑得慢的时候一眼能看出卡在哪
    stage_sec: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
