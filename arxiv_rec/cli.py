"""命令行入口的实现。

以前这份代码在 ``run.py`` 里, 而 ``ui.py`` 打成的 exe 想要一个"不开界面也能
跑一次"的入口时, 就得把同一套参数再抄一遍 —— 两条命令行迟早会跑偏 (一边加了
``--depth``, 另一边忘了), 所以统一放到包里:

    python run.py                 # 源码方式
    python ui.py --run            # 同上, 走图形界面的入口
    daily_arxiv.exe --run         # 打包之后

三处调用的都是这里的 ``main()``。

打包成窗口程序 (``--windowed``) 之后 ``sys.stdout`` 是 ``None`` —— 那时所有
输出只写进 ``<输出目录>/run_<时间>.log``, 见 ``_tee_output``。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from typing import Any, List, Optional

from . import __version__
from .config import describe, load_config, project_root, resolve_path
from .library import library_summary
from .pdf_library import READ_DEPTHS
from .pipeline import STAGES, PipelineOptions, run_pipeline, stage_index
from .rank import explain_weights
from .utils import fmt_elapsed, log, setup_console, truncate


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="daily_arxiv",
        description="根据本地 PDF 文献库推荐 arXiv 相关文献",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=None, help="配置文件路径 (默认 ./config.json)")
    p.add_argument("--top", type=int, default=None, help="推荐篇数 (默认取配置里的 top_n)")
    p.add_argument("--queries", nargs="+", default=None,
                   help="手动指定 arXiv 检索式 (覆盖自动生成)")
    p.add_argument("--categories", nargs="+", default=None,
                   help="限定 arXiv 分类, 如 cond-mat.str-el quant-ph")
    p.add_argument("--subscribe", action="store_true",
                   help="订阅模式: 只用 --categories 的当天公告, 不生成检索式")
    p.add_argument("--no-ai", action="store_true", help="不调用 AI (纯启发式)")
    p.add_argument("--no-enrich", action="store_true", help="跳过 OpenAlex 引用富化")
    p.add_argument("--no-fulltext", action="store_true",
                   help="不读全文节选 (等同于 --depth sections)")
    p.add_argument("--depth", choices=list(READ_DEPTHS), default="",
                   help="PDF 读取深度: metadata=标题+摘要, "
                        "sections=再加引言/结论 (默认), fulltext=再加全文节选")
    p.add_argument("--stage", choices=STAGES, default="report",
                   help="只跑到指定阶段就停下 (调试用)")
    p.add_argument("--concurrency", type=int, default=None,
                   help="AI 调用并发数 (相关性打分 + 深度解读); 1 为串行")
    p.add_argument("--refresh", action="store_true", help="清空 HTTP/AI 缓存后重新抓取")
    p.add_argument("--refresh-pdf", action="store_true",
                   help="忽略 PDF 索引, 重新解析所有 PDF (提取逻辑升级后用)")
    p.add_argument("--tag", default="", help="输出文件名后缀, 便于区分多次运行")
    p.add_argument("--no-history", action="store_true",
                   help="这次不读也不写推荐记录 (每次都当全新的一轮)")
    p.add_argument("--version", action="version", version="daily_arxiv %s" % __version__)
    return p.parse_args(argv)


class _Tee:
    """同时写到原 stdout 和日志文件。

    窗口版 exe 没有控制台 (``sys.stdout`` 是 ``None``), 一次跑完什么都不留下 ——
    出了问题用户既看不到也没法反馈。所以 ``--run`` 一律留一份日志文件。
    """

    def __init__(self, path: str, real: Any) -> None:
        self.real = real
        self.path = path
        try:
            self.fh = open(path, "w", encoding="utf-8")
        except Exception:
            self.fh = None

    def write(self, s: str) -> int:
        if self.fh is not None:
            try:
                self.fh.write(s)
            except Exception:
                pass
        if self.real is not None:
            try:
                self.real.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self) -> None:
        for stream in (self.fh, self.real):
            if stream is not None:
                try:
                    stream.flush()
                except Exception:
                    pass

    def close(self) -> None:
        if self.fh is not None:
            try:
                self.fh.close()
            except Exception:
                pass


def _tee_output(cfg: dict) -> Optional[_Tee]:
    """把 stdout/stderr 接到 ``<输出目录>/run_<时间>.log``。"""
    out_dir = resolve_path((cfg.get("output") or {}).get("dir") or "output")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        out_dir = project_root()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tee = _Tee(os.path.join(out_dir, "run_%s.log" % stamp),
               getattr(sys, "stdout", None))
    sys.stdout = tee            # type: ignore[assignment]
    sys.stderr = tee            # type: ignore[assignment]
    return tee


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    # 顺序要紧: 先让**真正的** stdout 切成 UTF-8 (Windows 控制台默认 GBK, 中文
    # 一打印就 UnicodeEncodeError), 再套日志文件。反过来的话 reconfigure 落在
    # _Tee 上 —— 它没有 reconfigure, setup_console 静默跳过, 控制台依旧是 GBK。
    setup_console()
    cfg = load_config(args.config)
    tee = _tee_output(cfg)

    print("=" * 72)
    print("  daily_arxiv v%s — 基于文献库的 arXiv 推荐" % __version__)
    print("=" * 72)
    print(describe(cfg))
    print()
    print(explain_weights(cfg))
    print()

    opts = PipelineOptions(
        top_n=args.top, queries=args.queries, categories=args.categories,
        subscribe_only=args.subscribe,
        no_ai=args.no_ai, no_enrich=args.no_enrich, no_fulltext=args.no_fulltext,
        read_depth=args.depth,
        concurrency=args.concurrency, refresh=args.refresh,
        refresh_pdf=args.refresh_pdf, tag=args.tag, stop_at=args.stage,
        no_history=args.no_history,
    )
    res = run_pipeline(cfg, opts)

    if res.source_stats is not None:
        print(res.source_stats.summary())
    if res.papers:
        print("  " + library_summary(res.papers))
    print()
    if res.profile is not None:
        if res.profile.queries:
            print("  生成的检索式:")
            for q in res.profile.queries:
                print("    - %s" % truncate(q, 76))
        else:
            # 订阅模式下这里是空的, 而且**是正常的** —— 别让它看起来像出了故障。
            cats = (cfg.get("arxiv") or {}).get("categories") or []
            print("  订阅模式: 不使用检索式, 候选来自 %d 个分类的当天公告%s"
                  % (len(cats),
                     (" (%s)" % ", ".join(cats)) if cats else ""))
        print()
    # 只有在真的排过序之后才打这张表 —— --stage 停在 rank 之前时 res.ranked 还是
    # 未打分的候选池, 打出来会是一列 0.000 却标着"排名"
    if res.ranked and stage_index(args.stage) >= STAGES.index("rank"):
        print("  排名前 10 预览:")
        print("    %-4s %-58s %6s %6s %6s %7s"
              % ("#", "标题", "相关", "时效", "重要", "总分"))
        for i, c in enumerate(res.ranked[:10], 1):
            print("    %-4d %-58s %6.2f %6.2f %6.2f %7.3f"
                  % (i, truncate(c.title.replace("$", ""), 56, "…"),
                     c.relevance, c.recency, c.importance, c.score))
        print()
    elif res.ranked:
        print("  候选 %d 篇 (还没排序, --stage %s 停在 rank 之前)"
              % (len(res.ranked), args.stage))
        print()

    if not res.ok:
        if res.cancelled:
            log("已中断。", "warn")
            _finish_log(tee)
            return 130
        log(res.message or "运行失败", "err")
        _finish_log(tee)
        return res.code or 1

    _finish(res)
    _finish_log(tee)
    return 0


def _finish_log(tee: Optional[_Tee]) -> None:
    if tee is not None:
        print("日志: %s" % tee.path)
        tee.flush()
        tee.close()


def _finish(res: Any) -> None:
    """收尾统计输出。"""
    print()
    print("-" * 72)
    print("完成 · 耗时 %s" % fmt_elapsed(res.elapsed_sec))
    if res.stats.ai_calls:
        print("AI 调用 %d 次, 约 %d tokens"
              % (res.stats.ai_calls, res.stats.ai_tokens))
    elif res.stats.analysis_reused:
        print("这次没有调用 AI —— %d 篇的解读直接复用了推荐记录"
              % res.stats.analysis_reused)
    if res.top:
        print("推荐 %d 篇, 其中前 3 篇:" % len(res.top))
        for i, c in enumerate(res.top[:3], 1):
            print("  %d. %s" % (i, truncate(c.title.replace("$", ""), 66, "…")))
    for path in res.report_paths:
        print("报告: %s" % path)
    print("-" * 72)


def run(argv: Optional[List[str]] = None) -> int:
    """``run.py`` 用这个: 额外兜住 Ctrl-C 和意外异常。"""
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:
        setup_console()
        log("运行出错: %s" % exc, "err")
        import traceback
        traceback.print_exc()
        return 1
