"""把八个阶段串成一次可中断、可回报进度的运行。

``run.py`` (命令行) 和 ``ui.py`` (图形界面) 都走这里, 免得同一条流水线维护
两份 —— 界面里少一个阶段、命令行里多一个参数, 是最容易悄悄跑偏的那类 bug。

与命令行版本的唯一区别是这里多了两个钩子:
  * ``on_progress``  -- 阶段内部的进度 (第几条检索式 / 第几个 PDF)
  * ``should_stop``  -- 用户点了"停止"。检查点放在阶段之间, 以及检索式之间
    和 PDF 之间这两个最耗时的循环里, 不用等整轮跑完。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .ai import NullAIClient, build_client
from .analyze import analyze_top
from .arxiv_search import collect
from .config import has_ai, project_root, resolve_path
from .dedup import filter_library
from .enrich import enrich_candidates
from .history import history_path, open_history, profile_fingerprint
from .library import load_library
from .models import Candidate, LibraryPaper, ResearchProfile, RunStats
from .profile import build_profile
from .rank import (apply_relevance_floor, heuristic_relevance,
                   rank_candidates)
from .report import build_report, write_report
from .utils import DiskCache, log, truncate

STAGES = ("library", "profile", "search", "dedup", "enrich", "rank",
          "analyze", "report")

STAGE_NAMES = {
    "library": "读取文献库",
    "profile": "构建研究画像",
    "search": "检索 arXiv",
    "dedup": "排除已有文献",
    "enrich": "补充引用数据",
    "rank": "排序",
    "analyze": "AI 深度解读",
    "report": "生成报告",
}


class Cancelled(Exception):
    """用户中途点了停止。"""


class PipelineOptions:
    """一次运行的全部可调项。字段与命令行参数一一对应。"""

    def __init__(self, top_n: Optional[int] = None,
                 queries: Optional[List[str]] = None,
                 categories: Optional[List[str]] = None,
                 no_ai: bool = False,
                 no_enrich: bool = False,
                 no_fulltext: bool = False,
                 read_depth: str = "",
                 concurrency: Optional[int] = None,
                 refresh: bool = False,
                 refresh_pdf: bool = False,
                 tag: str = "",
                 stop_at: str = "report",
                 no_history: bool = False,
                 subscribe_only: bool = False) -> None:
        self.top_n = top_n
        self.queries = queries
        self.categories = categories
        self.subscribe_only = subscribe_only
        self.no_ai = no_ai
        self.no_enrich = no_enrich
        self.no_fulltext = no_fulltext
        self.read_depth = read_depth
        self.concurrency = concurrency
        self.refresh = refresh
        self.refresh_pdf = refresh_pdf
        self.tag = tag
        self.stop_at = stop_at
        self.no_history = no_history

    def apply_to(self, cfg: Dict[str, Any]) -> None:
        """把选项覆盖到配置上 (就地修改)。"""
        if self.subscribe_only:
            # 订阅模式要**显式清空**检索式。空列表在这里是个有含义的取值 (就是
            # "没有检索式"), 所以不能沿用下面 `if self.queries:` 那套"非空才覆盖"
            # 的写法 —— 那样订阅模式根本表达不出来, 生成的检索式会照旧生效。
            cfg["arxiv"]["subscribe_only"] = True
            cfg["arxiv"]["queries"] = []
        elif self.queries:
            cfg["arxiv"]["queries"] = list(self.queries)
        if self.categories:
            cfg["arxiv"]["categories"] = list(self.categories)
        if self.concurrency is not None:
            cfg["analysis"]["concurrency"] = self.concurrency
        if self.no_fulltext:
            # 以前这个开关关的是 Zotero 的全文缓存。现在没有 Zotero 了, 它对应
            # 的是读取深度: 退到 "sections" (标题+摘要+引言/结论), 不读全文节选。
            cfg.setdefault("library", {})["read_depth"] = "sections"
        if self.read_depth:
            cfg.setdefault("library", {})["read_depth"] = self.read_depth
        if self.top_n is not None:
            cfg["analysis"]["top_n"] = self.top_n


class PipelineCallbacks:
    """界面接进度的地方。三个钩子都是可选的。"""

    def __init__(self,
                 on_stage: Optional[Callable[[int, int, str], None]] = None,
                 on_progress: Optional[Callable[[int, int, str], None]] = None,
                 should_stop: Optional[Callable[[], bool]] = None) -> None:
        self.on_stage = on_stage
        self.on_progress = on_progress
        self.should_stop = should_stop

    def stage(self, idx: int, name: str) -> None:
        if self.on_stage is not None:
            try:
                self.on_stage(idx, len(STAGES), name)
            except Exception:
                pass

    def progress(self, done: int, total: int, text: str) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(done, total, text)
            except Exception:
                pass

    def stopped(self) -> bool:
        if self.should_stop is None:
            return False
        try:
            return bool(self.should_stop())
        except Exception:
            return False

    def check(self) -> None:
        """在阶段边界检查停止请求。"""
        if self.stopped():
            raise Cancelled()


class PipelineResult:
    """一次运行的产物。失败时 ``ok=False``, ``message`` 说明原因。"""

    def __init__(self) -> None:
        self.ok = False
        self.code = 0
        self.message = ""
        self.cancelled = False
        self.report_paths: List[str] = []
        self.stats = RunStats()
        self.papers: List[LibraryPaper] = []
        self.profile: Optional[ResearchProfile] = None
        self.ranked: List[Candidate] = []
        self.top: List[Candidate] = []
        self.removed: List[Tuple[Candidate, str]] = []
        self.source_stats: Any = None
        self.elapsed_sec = 0.0

    def summary(self) -> str:
        s = self.stats
        return ("文献库 %d 篇 · 候选 %d 篇 (去重后 %d) · 解读 %d 篇 · "
                "耗时 %.0fs"
                % (s.library_total, s.candidates_raw, s.candidates_after_dedup,
                   s.candidates_analyzed, self.elapsed_sec))


def build_caches(cfg: Dict[str, Any], refresh: bool = False) -> Dict[str, DiskCache]:
    """建好 HTTP / AI 两层磁盘缓存。"""
    ncfg = cfg.get("network", {})
    enabled = bool(ncfg.get("cache", True))
    root = ncfg.get("cache_dir", "cache")
    if not os.path.isabs(root):
        root = os.path.join(project_root(), root)
    if refresh and os.path.isdir(root):
        import shutil
        log("清空缓存目录: %s" % root, "warn")
        shutil.rmtree(root, ignore_errors=True)
    return {
        "http": DiskCache(root, "http", enabled),
        "ai": DiskCache(root, "ai", enabled),
    }


def make_ai(cfg: Dict[str, Any], caches: Dict[str, DiskCache],
            no_ai: bool) -> Any:
    """按配置挑一个 AI 客户端, 任何一步不满足就降级为 NullAIClient。"""
    if no_ai:
        log("已指定不使用 AI, 全程走启发式", "warn")
        return NullAIClient(cfg)
    if not has_ai(cfg):
        log("未配置 API key, 降级为纯启发式模式 (在设置里填 API key 可启用 AI)",
            "warn")
        return NullAIClient(cfg)
    try:
        ai = build_client(cfg, caches["ai"])
        log("AI 客户端就绪: %s / %s" % (ai.provider, ai.model))
        return ai
    except Exception as exc:
        log("AI 客户端初始化失败 (%s), 降级为纯启发式模式" % exc, "warn")
        return NullAIClient(cfg)


def stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return len(STAGES) - 1


def run_pipeline(cfg: Dict[str, Any], opts: Optional[PipelineOptions] = None,
                 cb: Optional[PipelineCallbacks] = None) -> PipelineResult:
    """跑完整条流水线。所有失败都以 ``PipelineResult`` 返回, 不抛异常。"""
    opts = opts or PipelineOptions()
    cb = cb or PipelineCallbacks()
    opts.apply_to(cfg)

    res = PipelineResult()
    t0 = time.time()
    res.stats.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stop_at = stage_index(opts.stop_at)
    top_n = int(cfg["analysis"].get("top_n", 20))

    def reached(stage: str) -> bool:
        return stage_index(stage) >= stop_at

    # --- 阶段计时 ---
    # "这次怎么这么慢" 光看总耗时回答不了 (总耗时里 90% 常常是某一两个阶段)。
    # 每个阶段边界结算一次, 最后连同报告一起给出, 见 _log_timings。
    stage_t0 = [time.time()]
    stage_cur = [""]

    def begin(idx: int, name: str) -> None:
        """进入一个阶段: 结算上一个的耗时, 回报进度。"""
        if stage_cur[0]:
            res.stats.stage_sec[stage_cur[0]] = round(
                time.time() - stage_t0[0], 2)
        stage_t0[0] = time.time()
        stage_cur[0] = name
        cb.stage(idx, name)
        extra = {"library": " (本地 PDF 文件夹)"}.get(name, "")
        log("步骤 %d/8: %s%s" % (idx, STAGE_NAMES[name], extra))

    hist = None
    log_paths(cfg)
    try:
        # ---------------- 1) 读取文献库 ----------------
        begin(1, "library")
        papers, src = load_library(
            cfg, force_pdf=opts.refresh_pdf,
            progress=lambda d, t, p: cb.progress(d, t, os.path.basename(p)))
        res.papers = papers
        res.source_stats = src
        if not papers:
            res.code = 2
            res.message = ("文献库为空, 无法构建研究画像。"
                           "请在设置里添加文献 PDF 文件夹。")
            return res
        res.stats.library_total = len(papers)
        res.stats.library_with_abstract = sum(1 for p in papers if p.abstract)
        res.stats.library_with_fulltext = sum(1 for p in papers if p.fulltext)
        log("文献库就绪: %d 篇 (有摘要 %d, 有全文 %d)"
            % (len(papers), res.stats.library_with_abstract,
               res.stats.library_with_fulltext))
        if reached("library"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 2) AI 客户端 ----------------
        caches = build_caches(cfg, opts.refresh)
        ai = make_ai(cfg, caches, opts.no_ai)

        # ---------------- 3) 研究画像 ----------------
        begin(2, "profile")
        profile = build_profile(cfg, papers, ai)
        res.profile = profile
        acfg = cfg.get("arxiv", {})
        subscribe = bool(acfg.get("subscribe_only", False))
        if subscribe:
            # 订阅模式 (照抄 zotero-arxiv-daily 的思路): 不做关键词检索, 候选池
            # 完全来自所勾选分类的 arXiv 当天公告。适合"我就想看我这几个方向
            # 今天有什么新的" —— 关键词检索会漏掉那些换了说法的新论文。
            #
            # 画像照旧要建: 它不只用来出检索式, 后面的相关性打分和深度解读都
            # 靠它。所以这里只是把检索式丢掉, 不是跳过这一步。
            cats = acfg.get("categories") or []
            if not cats:
                res.code = 3
                res.message = ("订阅模式需要至少勾选一个分类: 这个模式不生成"
                               "检索式, 候选论文全部来自所选分类的 arXiv 当天"
                               "公告。请在「文献推荐」页勾选分类, 或取消订阅模式。")
                return res
            if profile.queries:
                log("订阅模式: 忽略刚生成的 %d 条检索式, 只用分类公告"
                    % len(profile.queries), "warn")
            profile.queries = []
            log("订阅模式: 候选论文来自 %d 个分类的当天公告 (%s)"
                % (len(cats), ", ".join(cats)))
        elif not profile.queries:
            res.code = 3
            res.message = "未能生成任何检索式, 请在设置里手动指定检索式。"
            return res
        res.stats.queries_used = len(profile.queries)
        if profile.queries:
            log("生成 %d 条检索式" % len(profile.queries))
            for q in profile.queries:
                log("  - %s" % truncate(q, 76), "dbg")
        if reached("profile"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 4) arXiv 检索 ----------------
        begin(3, "search")
        candidates = collect(cfg, profile.queries, cache=caches["http"],
                             progress=lambda d, t, q: cb.progress(
                                 d, t, "检索式 %d/%d" % (d, t)),
                             should_stop=cb.stopped)
        res.stats.candidates_raw = len(candidates)
        if not candidates:
            res.code = 4
            res.message = ("没有抓到任何候选论文。多半是网络问题 —— 到「设置」页"
                           "点「测试 arXiv 连接」看看哪条通道不通; 如果设了本机"
                           "代理 (127.0.0.1:xxxx), 确认那个代理软件开着。"
                           "也可能是检索式太窄, 或者限定的分类太少。")
            if subscribe:
                res.message += ("另外订阅模式只抓**当天**的公告: 周末、节假日"
                                "和 arXiv 不发布公告的日子本来就没有新论文, "
                                "这是正常的, 隔一天再跑即可。")
            return res
        res.ranked = candidates
        if reached("search"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 5) 去重 ----------------
        begin(4, "dedup")
        candidates, removed = filter_library(candidates, papers)
        res.removed = removed
        res.stats.candidates_after_dedup = len(candidates)
        if not candidates:
            res.code = 5
            res.message = "抓到的论文都在你的库里了。试试放宽检索式。"
            return res
        if reached("dedup"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 5.5) 推荐记录 ----------------
        # 放在富化之前: 要跳过的那几篇连引用数都不用去查, 省一次网络往返。
        # 也放在画像之后: 复用旧解读必须核对画像指纹, 而那要画像先算出来。
        hist = open_history(cfg, use=not opts.no_history)
        hist_rows: Dict[str, Dict[str, Any]] = {}
        profile_fp = profile_fingerprint(profile)
        if hist is not None:
            hist_rows = hist.known()
            for c in candidates:
                row = hist_rows.get(c.arxiv_id or "")
                if row:
                    c.seen_before = True
                    c.seen_times = int(row.get("times") or 0)
                    c.last_recommended = str(row.get("last_at") or "")
            res.stats.recommended_seen = sum(
                1 for c in candidates if c.seen_before)
            log("推荐记录: 库中已有 %d 篇, 本次候选命中 %d 篇"
                % (len(hist_rows), res.stats.recommended_seen))
            if (res.stats.recommended_seen
                    and bool((cfg.get("analysis") or {}).get(
                        "skip_recommended", False))):
                keep = [c for c in candidates if not c.seen_before]
                if len(keep) >= top_n:
                    res.stats.recommended_skipped = len(candidates) - len(keep)
                    log("按设置跳过 %d 篇以前推荐过的论文"
                        % res.stats.recommended_skipped)
                    candidates = keep
                else:
                    # 跳过之后连推荐篇数都凑不满, 那就别跳了 —— 悄悄给出一个
                    # 只有三篇的推荐列表, 比给出几篇旧的更让人摸不着头脑
                    log("跳过以前推荐过的之后只剩 %d 篇 (不足 %d 篇), "
                        "这次仍然保留它们" % (len(keep), top_n), "warn")
            res.stats.candidates_after_dedup = len(candidates)

        # ---------------- 6) 富化 ----------------
        begin(5, "enrich")
        heur: Optional[Dict[str, float]] = None
        if opts.no_enrich:
            log("跳过引用富化", "warn")
        else:
            enrich_max = int(cfg["analysis"].get("enrich_max", 400) or 0)
            if enrich_max > 0 and len(candidates) > enrich_max:
                # 先算一遍相关性, 拿它挑"值得去查引用数"的那批。这份分数接着
                # 传给排序阶段复用 —— TF-IDF 没必要对同一批文档算两遍。
                heur = heuristic_relevance(profile, candidates)
            try:
                res.stats.candidates_enriched = enrich_candidates(
                    cfg, candidates, caches["http"], heur=heur,
                    limit=enrich_max, should_stop=cb.stopped)
            except Exception as exc:
                log("引用富化失败 (不影响后续): %s" % exc, "warn")
        if reached("enrich"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 7) 排序 ----------------
        begin(6, "rank")
        log("相关性 / 时效性 / 重要性联合排序")
        ranked = rank_candidates(cfg, profile, candidates, ai, heur=heur,
                                 should_stop=cb.stopped)
        ranked = apply_relevance_floor(
            ranked, float(cfg["arxiv"].get("min_relevance", 0.0)))
        res.ranked = ranked
        if not ranked:
            res.code = 6
            res.message = "排序后没有剩下任何论文 (相关性阈值可能太高)。"
            return res
        log("排序完成, 前 3 篇:")
        for i, c in enumerate(ranked[:3], 1):
            log("  %d. %s (%.3f)" % (i, truncate(c.title.replace("$", ""), 60, "…"),
                                     c.score), "dbg")
        if reached("rank"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 8) AI 深度解读 ----------------
        top = ranked[:top_n]
        begin(7, "analyze")
        log("排名前 %d 篇" % len(top))
        new_n, reused_n = analyze_top(
            cfg, ranked, papers, ai, profile, top_n=top_n,
            concurrency=int(cfg["analysis"].get("concurrency", 1)),
            history=hist_rows, profile_fp=profile_fp,
            should_stop=cb.stopped)
        res.stats.analysis_new = new_n
        res.stats.analysis_reused = reused_n
        res.stats.candidates_analyzed = new_n + reused_n
        res.top = top
        if reached("analyze"):
            res.ok = True
            return res
        cb.check()

        # ---------------- 9) 报告 ----------------
        begin(8, "report")
        res.stats.ai_calls = getattr(ai, "calls", 0)
        res.stats.ai_tokens = getattr(ai, "total_tokens", 0)
        res.stats.elapsed_sec = time.time() - t0
        markdown = build_report(cfg, profile, top, ranked, removed, res.stats)
        res.report_paths = write_report(cfg, markdown, top, profile,
                                        res.stats, tag=opts.tag)
        for path in res.report_paths:
            log("报告已写入: %s" % path, "ok")
        if hist is not None:
            added, upd = hist.record(
                top, res.report_paths[0] if res.report_paths else "", profile_fp)
            log("推荐记录已更新: 新增 %d 篇, 更新 %d 篇 (%s)"
                % (added, upd, hist.db_path))
        res.ok = True
        return res

    except Cancelled:
        res.cancelled = True
        res.message = "已停止"
        log("已按请求停止", "warn")
        return res
    except Exception as exc:
        res.code = 1
        res.message = str(exc)
        log("运行出错: %s" % exc, "err")
        import traceback
        for line in traceback.format_exc().splitlines()[-6:]:
            log(line, "dbg")
        return res
    finally:
        if stage_cur[0]:
            res.stats.stage_sec[stage_cur[0]] = round(
                time.time() - stage_t0[0], 2)
        res.elapsed_sec = time.time() - t0
        if res.papers:
            res.stats.library_total = res.stats.library_total or len(res.papers)
        if hist is not None:
            hist.close()
        log_timings(res)


def log_paths(cfg: Dict[str, Any]) -> None:
    """把"这次要往哪儿写"的三条绝对路径打进运行日志。

    用户报"报告没放进设置里那个目录""推荐记录一直是 0 篇"时, 真正的原因几乎
    都是**程序读到的配置不是他以为的那一份** —— 手改过 config.json, 或者从
    别的目录启动、于是 ``./config.json`` 命中了另一份。日志里先摆出实际要用
    的三个绝对路径, 这类问题看一眼就定位了, 不用猜。
    """
    # 用 info 级而不是 dbg: dbg 默认不显示 (标题栏那个"详细日志"勾上才显示),
    # 而这四五行恰恰是排"东西写哪儿去了"时最需要看到的。一次运行几百行日志,
    # 多这五行不算什么。
    try:
        out_dir = resolve_path((cfg.get("output") or {}).get("dir") or "output")
        index_db = resolve_path((cfg.get("library") or {}).get("index_db")
                                or "library_index.sqlite")
        log("程序目录: %s" % project_root())
        log("报告目录: %s" % out_dir)
        log("读取记录: %s" % index_db)
        log("推荐记录: %s" % history_path(cfg))
        if (cfg.get("_config_path") or ""):
            log("配置文件: %s" % cfg["_config_path"])
    except Exception as exc:                # 打日志本身绝不能弄挂一次运行
        log("打不出路径信息: %s" % exc, "warn")


def log_timings(res: "PipelineResult") -> None:
    """按从大到小打出各阶段耗时。

    总耗时对不上直觉时 (比如"这次怎么跑了两分钟"), 这一行直接指出卡在哪 ——
    检索受网络和限流影响最大, AI 解读受并发和 token 数影响, 两者要调的参数
    完全不同, 光看总耗时是分不出来的。
    """
    items = [(v, k) for k, v in (res.stats.stage_sec or {}).items()]
    if not items:
        return
    items.sort(reverse=True)
    log("各阶段耗时 (从大到小): "
        + " · ".join("%s %.1fs" % (STAGE_NAMES.get(k, k), v) for v, k in items))
    search_sec = float(res.stats.stage_sec.get("search", 0.0))
    if search_sec > 20:
        log("检索占了大头 —— 这是 arXiv 的请求间隔 (arxiv.request_delay) 决定的;"
            " 命中缓存时不会重新发请求, 同一批检索式再跑一次会快很多。", "dbg")
