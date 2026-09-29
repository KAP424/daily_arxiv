#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""daily_arxiv 图形界面入口。

    python ui.py                       # 用默认 config.json
    python ui.py --config my.json      # 指定配置文件
    python ui.py --check               # 不建窗口, 只打印路径与依赖自检
    python ui.py --run --top 10        # 不建窗口, 直接跑一轮 (参数同 run.py)

界面分四页:
  1. 读取文献  — 扫描本地 PDF 文件夹, 按选定深度解析, 建好可复用的索引
  2. 文献推荐  — 抓 arXiv、做相关性分析、写推荐与建议
  3. 文献列表  — 已读过的文献, 点一行用系统默认程序打开它的 PDF
  4. 设置      — API key / base_url / 网络 / 三个存放位置 / 读取深度

不想开界面就用 ``python run.py``, 两者跑的是同一条流水线 —— ``ui.py --run``
和 ``run.py`` 最终都调 ``arxiv_rec.cli``, 免得同一套参数在两处各写一遍然后慢慢跑偏。
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import threading
import traceback
from typing import List, Optional

# 打包成 exe 之后 __file__ 在 PyInstaller 的临时解包目录里, 把它加进 sys.path
# 没有意义 (模块已经在 bundle 里了); 而且那个目录退出就删, 当不了根目录。
# 非冻结时才需要这一行 —— 直接 python ui.py 时保证能 import 到 arxiv_rec。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ui.py", description="daily_arxiv 图形界面")
    p.add_argument("--config", default=None, help="配置文件路径 (默认 ./config.json)")
    p.add_argument("--check", action="store_true",
                   help="不打开界面, 只打印路径与依赖自检 (打包后排查问题用)")
    p.add_argument("--run", action="store_true",
                   help="不打开界面, 直接跑一轮推荐 (后面可以再跟 run.py 的参数)")
    return p.parse_args(argv)


def _extract_run_argv(argv: List[str]) -> Optional[List[str]]:
    """命令行里有 ``--run`` 就返回去掉它的其余参数, 否则返回 None。

    这里不用 argparse: ``--run`` 后面跟的是**另一套**参数 (``run.py`` 的), 塞进
    同一个 parser 会和 ``ui.py`` 自己的 ``--config`` / ``--check`` 打架。手工摘掉
    ``--run`` 再把剩下的原样转交, 两边各管各的参数, 谁也不认识谁。
    """
    if "--run" not in argv:
        return None
    return [a for a in argv if a != "--run"]


def _run_cli(argv: List[str]) -> int:
    """把 ``--run`` 后面那串参数交给命令行管线。

    **不需要**在这里操心"窗口版 exe 没有控制台, 输出会消失": ``cli.main()`` 一进去
    就用 ``_Tee`` 把 stdout/stderr 接到 ``<输出目录>/run_<时间>.log``, 而 ``_Tee``
    对 ``real=None`` (没有控制台时 ``sys.stdout`` 就是 None) 已经处理好了。
    这里再套一层重定向只会多写出第二个日志文件。
    """
    from arxiv_rec.cli import run as cli_run

    try:
        return cli_run(argv)
    except KeyboardInterrupt:
        return 130


def _self_check(config_path: Optional[str]) -> int:
    """不建窗口, 只打印"程序现在到底从哪读、往哪写、缺什么"。

    打包成 exe 之后最容易出的一类问题是**路径**: 配置没读到、索引建到临时
    目录里、报告写完就没了。这些都不报错, 只是"看起来没生效"。所以留一个
    不依赖图形界面的自检入口 —— 双击 exe 出不来窗口的时候, 在命令行里跑

        daily_arxiv.exe --check

    就能看到实际用的路径和缺失的依赖。
    """
    import platform

    from arxiv_rec.config import (DEFAULT_CONFIG, is_frozen, load_config,
                                  project_root, resolve_path)

    ok = True
    lines: List[str] = []

    def w(s: str = "") -> None:
        lines.append(s)

    root = project_root()
    w("=" * 66)
    w("daily_arxiv 自检")
    w("=" * 66)
    w("运行方式   : %s" % ("打包的 exe" if is_frozen() else "源码"))
    w("Python     : %s (%d 位)"
      % (platform.python_version(), 64 if sys.maxsize > 2**32 else 32))
    w("数据根目录 : %s" % root)
    if not os.path.isdir(root):
        w("             [!] 这个目录不存在")
        ok = False
    if not os.access(root, os.W_OK):
        w("             [!] 这个目录不可写 —— 配置、索引、报告都存不下来。")
        w("                 换个位置放 exe (别放在 Program Files 下)。")
        ok = False

    # --- 配置 ---
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        w("")
        w("配置加载失败: %s" % exc)
        _emit_check(lines)
        return 1
    used = cfg.get("_config_path") or ""
    w("")
    w("配置文件   : %s" % (used or "<没找到, 用内置默认值>"))
    if not used:
        w("             (在「设置」页保存一次, 就会在数据根目录下生成 config.json)")
    ai = cfg.get("ai", {})
    key = (ai.get("_resolved_key") or "")
    w("AI 接口    : %s / %s"
      % (ai.get("provider", "?"), ai.get("base_url") or "<未填>"))
    w("AI 密钥    : %s"
      % ("已配置 (%d 字符)" % len(key) if key else
         "没配置 —— 只能走启发式排序, 没有 AI 解读"))

    # --- 索引 / 输出 ---
    lib = cfg.get("library", {})
    db = resolve_path(lib.get("index_db") or DEFAULT_CONFIG["library"]["index_db"])
    w("")
    w("文献索引   : %s" % db)
    if os.path.exists(db):
        try:
            import sqlite3
            con = sqlite3.connect(db)
            # 表名是 files 不是 papers; status 里有 ok / notext / error 等
            rows = dict(con.execute(
                "SELECT status, COUNT(*) FROM files GROUP BY status"))
            con.close()
            total = sum(rows.values())
            w("             已记录 %d 个文件 (读过的 PDF 不会重读)" % total)
            if rows:
                w("             %s" % ", ".join(
                    "%s %d" % (k, v) for k, v in sorted(rows.items())))
        except Exception as exc:
            w("             [!] 打不开: %s" % exc)
            ok = False
    else:
        w("             还没建 —— 到「读取文献」页点一次开始读取")
    w("报告目录   : %s" % resolve_path(cfg.get("output", {}).get("dir", "output")))
    # 推荐记录这条以前没打出来 —— 而"推荐记录一直是 0 篇"有一半是它指到了别的
    # 地方去 (相对路径按数据根目录解析, 或者手改 config.json 时写错一个字母)。
    # 顺手把三个位置能不能用也判一遍, 和界面启动时那个提醒用的是同一份判断。
    from arxiv_rec.config import check_data_paths, data_paths
    w("推荐记录   : %s" % data_paths(cfg)[1][2])
    for _name, _raw, _path, _why in check_data_paths(cfg):
        w("             [!] %s 这个位置用不了 (%s)" % (_name, _why))
        ok = False

    folders = [f for f in (cfg.get("pdf_folders") or []) if f.get("enabled", True)]
    w("PDF 文件夹 : %d 个" % len(folders))
    for f in folders:
        p = resolve_path(f.get("path", ""))
        exists = os.path.isdir(p)
        if not exists:
            ok = False
        w("             %s %s" % ("OK " if exists else "[!]", p))
    w("读取深度   : %s" % lib.get("read_depth", "sections"))

    # --- 依赖 ---
    # 三档: req 缺了就干不了活; warn 缺了还能跑但**结果明显变差**, 不能因为
    # "没报错"就说一切正常; opt 纯粹是锦上添花。
    w("")
    w("依赖:")
    warnings: List[str] = []
    for mod, why, level in (
            ("tkinter", "图形界面", "req"),
            ("requests", "联网抓 arXiv", "req"),
            ("fitz", "解析 PDF (PyMuPDF)", "req"),
            ("numpy", "数值计算", "warn"),
            ("sklearn", "TF-IDF 相关性", "warn")):
        try:
            __import__(mod)
            w("  [OK ] %-9s %s" % (mod, why))
        except Exception as exc:
            mark = {"req": "!!", "warn": "~~", "opt": "--"}[level]
            w("  [%s] %-9s %s  (%s)" % (mark, mod, why, exc))
            if level == "req":
                ok = False
            elif level == "warn":
                warnings.append("%s 不可用: %s" % (mod, exc))

    w("")
    if warnings:
        w("注意 (能跑, 但结果会变差):")
        for msg in warnings:
            w("  - %s" % msg)
        w("  sklearn 不可用时, 相关性排序会退化成关键词重叠, 分数普遍挤在一起;")
        w("  这种情况通常是打包时把某个模块排除掉了, 用 --venv 重新打包试试。")
        w("")
    w("=" * 66)
    if not ok:
        w("结论: 有问题, 见上面的 [!]")
    elif warnings:
        w("结论: 能跑, 但有降级项 (见上面的 [~~])")
    else:
        w("结论: 一切正常")
    w("=" * 66)
    _emit_check(lines)
    return 0 if ok else 1


def _emit_check(lines: List[str]) -> None:
    """把自检结果送到用户能看见的地方。

    窗口版 exe (``--windowed``) 没有控制台, ``sys.stdout`` 是 ``None`` ——
    直接 ``sys.stdout.write`` 会在最需要它的时候 (界面起不来) 崩掉。所以:
    有 stdout 就打印; 同时**总是**写一份文件到数据根目录; 两者都没有就弹窗。
    """
    text = "\n".join(lines)

    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.write(text + "\n")
            stream.flush()
        except Exception:
            stream = None

    path = ""
    try:
        from arxiv_rec.config import project_root
        path = os.path.join(project_root(), "check_report.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        path = ""

    if stream is None:
        try:
            import tkinter.messagebox as mb
            mb.showinfo("daily_arxiv 自检",
                        text if len(text) < 1500 else
                        text[:1500] + "\n...\n\n完整内容见:\n" + (path or "(写不进去)"))
        except Exception:
            pass


def _log_crash(text: str) -> str:
    """把崩溃信息追加到 exe 旁边的 crash.log, 返回文件路径 (写不进去就返回空)。"""
    try:
        from arxiv_rec.config import project_root
        path = os.path.join(project_root(), "crash.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 64 + "\n")
            fh.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            fh.write(text)
        return path
    except Exception:
        return ""


def _install_crash_handler() -> None:
    """兜住未捕获的异常: 写 crash.log, 主线程的再弹个窗。

    打包成窗口程序 (``--windowed``) 之后**没有控制台**, 未捕获的异常会让进程
    直接静默退出: 双击 exe, 窗口闪一下 (或者干脆不出现), 用户完全不知道发生了
    什么, 也没有任何线索可以反馈。所以这里必须兜住 —— 尤其 ``main()`` 里
    "建窗口"这一步, 那正是最容易出问题 (缺配置、缺依赖、字体异常) 的地方。

    工作线程的异常只写文件、不弹窗: Tk 不是线程安全的, 从子线程弹模态框
    可能把界面搞死, 那是比崩溃更难查的问题。
    """
    def make_hook(show_dialog: bool, where: str = ""):
        def hook(exc_type, exc, tb) -> None:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            if where:
                text = "[%s] %s" % (where, text)
            path = _log_crash(text)
            # 只写文件是不够的: 工作线程崩了不弹窗 (Tk 不是线程安全的), 用户看到
            # 的只是"某个结果莫名其妙没出来", 完全不知道出过错 —— 上次那个崩溃
            # 就是因为没人看见, 最后只留下一句读不懂的 TypeError。推进日志面板,
            # 界面上至少有个红字。log() 只往队列里塞、不碰控件, 子线程调是安全的。
            try:
                from arxiv_rec.utils import log as _log
                _log("后台出错了: %s: %s (完整信息见 %s)"
                     % (exc_type.__name__, exc, path or "crash.log"), "err")
            except Exception:
                pass
            stream = getattr(sys, "stderr", None)
            if stream is not None:
                try:
                    stream.write(text)
                except Exception:
                    pass
            if not show_dialog:
                return
            try:
                import tkinter.messagebox as mb
                mb.showerror(
                    "daily_arxiv 出错了",
                    "%s: %s\n\n详细信息已写入:\n%s"
                    % (exc_type.__name__, exc, path or "(日志也写不进去)"))
            except Exception:
                # 连弹窗都起不来 (比如 Tk 根本没装上) —— 已经写了 crash.log,
                # 别再因为兜底逻辑本身再抛一次
                pass
        return hook

    sys.excepthook = make_hook(True)

    # threading.excepthook 是 3.8 才有的, 老版本没有就算了。
    #
    # 注意签名**不一样**: sys.excepthook 收 (type, value, tb) 三个参数, 而
    # threading.excepthook 收的是**一个** ExceptHookArgs 具名元组。之前这里
    # 图省事把同一个三参数闭包挂到两边, 结果是: 工作线程一抛异常, 兜底逻辑
    # 自己先抛 TypeError, 真正的异常反而没了 —— crash.log 里只剩
    # "hook() missing 2 required positional arguments: 'exc' and 'tb'",
    # 完全看不出哪里出的错, 比不写日志还误导。
    if hasattr(threading, "excepthook"):
        def thread_hook(args) -> None:
            exc_type = getattr(args, "exc_type", None)
            if exc_type is None:      # 拿到个不认识的对象: 别猜, 原样记下来
                _log_crash("threading.excepthook 收到意外参数: %r" % (args,))
                return
            name = getattr(getattr(args, "thread", None), "name", None) or "?"
            make_hook(False, "工作线程 %r" % name)(
                exc_type, getattr(args, "exc_value", None),
                getattr(args, "exc_tb", None))

        threading.excepthook = thread_hook


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # --run 走命令行管线, 一样不需要 tkinter。放在最前面: 定时任务里跑 exe
    # (或者干脆没有图形环境的机器上) 不该因为 tkinter 起不来就整个失败。
    run_argv = _extract_run_argv(raw)
    if run_argv is not None:
        return _run_cli(run_argv)

    args = parse_args(raw)
    if args.check:
        # 自检走纯文本, 不需要 tkinter —— 界面起不来的情况下更要能跑
        return _self_check(args.config)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        msg = ("没有找到 tkinter。\n"
               "Windows/macOS 的官方 Python 自带; Linux 上装一下 python3-tk。\n"
               "也可以直接用命令行: python run.py\n")
        stream = getattr(sys, "stderr", None)
        if stream is not None:
            stream.write(msg)
        else:
            # 窗口程序没有 stderr, 只能弹窗 —— 否则用户看到的只是"什么都没发生"
            try:
                import tkinter.messagebox as mb
                mb.showerror("缺少 tkinter", msg)
            except Exception:
                pass
        return 1

    from arxiv_rec.ui import App
    from arxiv_rec.utils import setup_console
    setup_console()
    _install_crash_handler()
    App(args.config).mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
