"""图形界面: 读取文献 / 文献推荐 / 设置。

用 Tkinter 而不是 PyQt/Electron, 是为了零额外依赖 —— 装了 Python 就能跑,
而这个工具的定位是"在自己电脑上偶尔点一下", 不值得为它再配一套运行环境。

线程模型
--------------------------------------------------------------------------
Tkinter 不是线程安全的: 所有控件操作必须在主线程。所以后台线程只做一件事
—— 把消息塞进 ``queue.Queue``, 主线程用 ``after()`` 定期取出来渲染。
``utils.log()`` 通过 sink 钩子直接接到这个队列上, 于是各模块里上百处
``log(...)`` 调用不用改一行就能显示到界面上。

停止按钮
--------------------------------------------------------------------------
``should_stop`` 的检查点放在阶段之间、检索式之间和 PDF 之间。检索 arXiv 是
最耗时的一步 (实测十几分钟), 所以中断检查必须做到检索式粒度, 否则按钮形同
虚设; 但已经抓到的结果会照常使用, 不会白跑。
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import past_runs, theme
from .arxiv_search import CATEGORY_CHOICES
from .config import load_config, project_root, resolve_path
from .library import library_summary, load_library
from .pdf_library import (DEPTH_LABELS, READ_DEPTHS, index_stats,
                          library_records, normalize_depth, set_paper_tags,
                          split_tags, status_label)
from .pipeline import (PipelineCallbacks, PipelineOptions, build_caches,
                       make_ai, run_pipeline)
from .theme import LEVEL_COLORS
from .utils import (add_log_sink, fmt_authors, fmt_date, fmt_journal,
                    fmt_journal_ref, log, remove_log_sink,
                    reset_proxy_probe_cache, truncate)

POLL_MS = 120

# 「文献列表」页下拉框里的第一个选项: 看本地已读文献。其余选项都是某一次的历史
# 推荐结果 (标签里带时间, 见 past_runs.label_for)。
RUN_LIBRARY = "已读文献 (本地 PDF)"

# 「文献推荐」页那个"推荐记录"下拉框的两个**固定**选项, 中间夹着的全是某一次的
# 历史推荐 (标签同样来自 past_runs.label_for):
#   * 本次运行的结果 —— 这一轮刚跑出来的那张列表 (跑之前是空的)
#   * 全部累计记录 (N 篇) —— 记录库里所有推荐过的论文, 按时间排; 就是以前那个
#     「推荐记录」按钮干的事, 现在挪进下拉框里当一个选项
REC_LIVE = "本次运行的结果"
# 末尾带篇数, 所以是**前缀**而不是整串 —— 判断"选的是不是这一项"要用 startswith
# (见 _apply_rec_pick)。
REC_ALL_PREFIX = "全部累计记录 ("


def _apply_chat_analysis(cand: Any, got: Dict[str, Any]) -> None:
    """把"按讨论重写的详解"落到候选对象上 (就地改)。

    和 analyze._apply 是同一件事的两个入口, 但**不复用**那个: 那个是流水线批量
    解读时用的, 还要兼顾"复用旧解读"的标记; 这里是用户在界面上点了一下, 语义
    简单 —— 就是"这三项换新的", 顺手把 reused 清掉 (它不再是复用的旧解读了)。
    """
    cand.summary = str(got.get("summary") or "")
    cand.connections = list(got.get("connections") or [])
    cand.ideas = str(got.get("ideas") or "")
    cand.analyzed = bool(cand.summary or cand.ideas)
    cand.reused = False


def _ui_font(root: tk.Misc) -> str:
    """挑一个能正常显示中文的字体。"""
    try:
        from tkinter import font as tkfont
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "PingFang SC", "Noto Sans CJK SC", "SimHei", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


# --------------------------------------------------------------------------
# 文献路径编辑器 (只有"读取文献"页一个实例 —— 设置页那份已撤掉, 见下方注释)
# --------------------------------------------------------------------------
class FolderEditor(ttk.Frame):
    """增删文献 PDF 文件夹。"""

    def __init__(self, master: tk.Misc, app: "App") -> None:
        ttk.Frame.__init__(self, master)
        self.app = app

        self.tree = ttk.Treeview(self, columns=("enabled", "path", "recursive"),
                                 show="headings", height=7, selectmode="extended")
        self.tree.heading("enabled", text="启用")
        self.tree.heading("path", text="文件夹")
        self.tree.heading("recursive", text="含子目录")
        self.tree.column("enabled", width=52, anchor="center", stretch=False)
        self.tree.column("path", width=520, anchor="w")
        self.tree.column("recursive", width=80, anchor="center", stretch=False)
        self.tree.pack(side="top", fill="both", expand=True)

        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x", pady=(6, 0))
        ttk.Button(bar, text="添加文件夹…", command=self.add).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self.remove).pack(side="left", padx=4)
        ttk.Button(bar, text="启用/停用", command=self.toggle).pack(side="left")
        ttk.Button(bar, text="上下移动", command=self.move).pack(side="left", padx=4)

        self.hint = ttk.Label(self, text="", foreground="#666666",
                              wraplength=760, justify="left")
        self.hint.pack(side="top", anchor="w", pady=(4, 0))

    # -- 数据 -------------------------------------------------------------
    def folders(self) -> List[Dict[str, Any]]:
        """返回**活的那个**列表 (增删直接落在 cfg 上, 不需要再写回)。

        setdefault 不够用: 键存在但值是 None 时它原样返回 None, 后面的 for 就
        炸了 (load_config 现在会把这种值归一成 [], 这里是第二道)。返回的必须
        是列表本身, 不能是副本 —— 四个操作都是往返回值里 append/del 的。
        """
        fs = self.app.cfg.get("pdf_folders")
        if not isinstance(fs, list):
            fs = []
            self.app.cfg["pdf_folders"] = fs
        return fs

    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, f in enumerate(self.folders()):
            self.tree.insert("", "end", iid=str(i), values=(
                "是" if f.get("enabled", True) else "否",
                f.get("path", ""),
                "是" if f.get("recursive", True) else "否",
            ))
        n = len(self.folders())
        if n:
            self.hint.config(text="共 %d 个文件夹。程序会递归扫描其中的 PDF; "
                                  "勾掉\"含子目录\"就只读该目录本身。" % n)
        else:
            self.hint.config(text="还没有添加文件夹。至少添加一个文献 PDF 目录, "
                                  "程序才能建出文献库。")

    def _selected(self) -> List[int]:
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(iid))
            except ValueError:
                pass
        return sorted(out)

    # -- 操作 -------------------------------------------------------------
    def add(self) -> None:
        path = filedialog.askdirectory(title="选择文献 PDF 文件夹")
        if not path:
            return
        path = os.path.normpath(path)
        for f in self.folders():
            if os.path.normcase(f.get("path", "")) == os.path.normcase(path):
                messagebox.showinfo("已在列表里", "这个文件夹已经添加过了。")
                return
        self.folders().append({"path": path, "enabled": True, "recursive": True})
        self.refresh()
        self.app.refresh_all_folder_editors()
        self.app.mark_dirty()

    def remove(self) -> None:
        idx = self._selected()
        if not idx:
            messagebox.showinfo("没有选中", "请先在列表里选中要删除的文件夹。")
            return
        names = "\n".join(self.folders()[i].get("path", "") for i in idx)
        if not messagebox.askyesno(
                "确认删除",
                "从列表里移除以下 %d 个文件夹?\n\n%s\n\n"
                "(只影响本程序的扫描范围, 不会删除磁盘上的文件, "
                "已解析的记录仍留在索引里)" % (len(idx), names)):
            return
        for i in reversed(idx):
            del self.folders()[i]
        self.refresh()
        self.app.refresh_all_folder_editors()
        self.app.mark_dirty()

    def toggle(self) -> None:
        idx = self._selected()
        if not idx:
            return
        for i in idx:
            f = self.folders()[i]
            f["enabled"] = not f.get("enabled", True)
        self.refresh()
        self.app.refresh_all_folder_editors()
        self.app.mark_dirty()

    def move(self) -> None:
        idx = self._selected()
        if len(idx) != 1:
            messagebox.showinfo("请选中一个", "上下移动一次只能操作一个文件夹。")
            return
        i = idx[0]
        j = i - 1 if i > 0 else i + 1
        if not (0 <= j < len(self.folders())):
            return
        fs = self.folders()
        fs[i], fs[j] = fs[j], fs[i]
        self.refresh()
        self.app.refresh_all_folder_editors()
        self.app.mark_dirty()


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------
class App(tk.Tk):
    # 「推荐记录」一次最多铺多少条。记录库只增不减 (一天一轮, 一年几千条), 全铺进
    # Treeview 会把界面卡住好几秒。上限是给**显示**用的, 记录本身一条不动 ——
    # 真到了上限, 状态栏和日志里会写明"只铺了最近 N 篇"。
    HISTORY_MAX_ROWS = 2000

    # 结果表显示几行。**定值**, 不随窗口变 —— 列表是"选哪篇"的入口, 12 行够挑
    # 了, 再多就得跟下面那块详解抢屏幕 (详解现在整段铺开, 页面整体滚)。
    TABLE_ROWS = 12

    # 详解框行数的上限, 纯粹防跑飞: 内容折行后行数由 Text.count 量出来, 一份
    # 解读不会有几百行, 真量出天文数字只可能是布局还没稳。
    DETAIL_MAX_LINES = 400

    # 详解定高那串空闲回调最多跑几拍 (见 _settle_detail)。它正常是"改完就停",
    # 这个数只是防跑飞: 宽度来回变的时候行数会一直变, 没有上限就成了死循环。
    #
    # 给得比"够用"宽得多是有意的: 收尾那几步是**一拍一件事** (改 Text 高度 ->
    # 等请求高度更新 -> 改窗口项高度 -> 等实际高度落下来 -> 才轮到核对 yview),
    # 而估出来的行数偏短时还要一行一行往上补 —— 一段 400 行的解读最多补到
    # DETAIL_MAX_LINES, 48 拍是给这个留的余量。每一拍都是一次 after(0) 空转
    # (没事做就立刻收工), 跑满也就几十毫秒, 用户看不见。
    DETAIL_SETTLE_PASSES = 48


    def __init__(self, config_path: Optional[str] = None) -> None:
        tk.Tk.__init__(self)
        self.config_path = config_path
        self.cfg: Dict[str, Any] = {}
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.dirty = False
        self.result: Any = None
        self.ranked: List[Any] = []
        # 下面那张列表现在铺的是"推荐记录"还是"这一轮的结果"。清空记录之后要
        # 据此决定要不要把列表也擦掉 —— 记录被删了, 但一轮跑出来的结果还在屏
        # 上, 那是两码事, 不该顺手一起清掉。
        self._tree_from_history = False
        # 详解框当前被调成了几行 (见 _fit_detail_height)。存着只为一件事: 值没变
        # 就不要再 configure 一次 —— 每次 <Configure> 都改高度会自己触发自己。
        self._detail_lines = 0
        # 详解框上一回看到的**实际**高度 (像素) 和宽度。高度那两个是用来判断"几何
        # 落位了没有、这个高度核对过没有"的 (见 _fit_detail_height 里 moved 那段);
        # 宽度是用来分辨 <Configure> 到底是"窗口变宽了"还是"我们自己把高度改了"
        # —— 只有前者要重新估行数 (见 _on_detail_configure)。
        self._detail_seen = 0
        self._detail_checked = -1
        self._detail_w = 0
        # 补元信息那条后台线程 (见 _repair_history_meta)。**故意不放进 self.worker**:
        # 它是顺手做的补全, 不该让 _busy() 变成真 —— 那会把"跑一轮推荐"锁住,
        # 用户会因为一个正在后台补日期的动作而点不动主按钮。
        self._meta_thread: Optional[threading.Thread] = None

        # --- 「推荐记录」下拉框 (见 _build_recommend_tab / _on_rec_pick) ---
        # 跑过三轮就该能挑看哪一轮, 而不是把三轮混成一张按时间排的大表。
        self._rec_runs: List[Dict[str, Any]] = []
        # 下拉框现在显示的是哪一类: "live" (这一轮) / "run" (某一份历史报告) /
        # "all" (全部累计记录)。清空记录、跑完一轮这些动作都要看它一眼。
        self._rec_mode = "live"
        # 这一轮跑出来的结果, 单独留一份 —— 从历史报告切回"本次运行的结果"时
        # 要原样还回来, 不能靠重跑一遍。
        self._live_ranked: List[Any] = []
        self._live_top_ids: set = set()

        # --- 和 AI 的讨论 (见 _build_chat_pane) ---
        self._chat_paper: Any = None            # 讨论区现在对着哪一篇
        self._chat_msgs: List[Dict[str, Any]] = []   # 界面上正显示的对话
        self._chat_busy_aid = ""                # 正在等 AI 回复的是哪一篇
        self._chat_thread: Optional[threading.Thread] = None
        # 提示词前缀按论文缓存 (拼一次要读文献库 + 算 TF-IDF, 每问一句都重算
        # 太亏)。"用对话更新详解"之后要清掉 —— 那份前缀里含旧的详解。
        self._chat_ctx: Dict[str, Any] = {}
        self._chat_papers: Optional[List[Any]] = None   # 文献库列表, 拼上下文用

        self.papers: List[Any] = []
        self._current_job = ""
        # 读取深度必须在这里建: 读取页和推荐页各有一份控件, 而两者共用这一个
        # 变量 —— 变量得先于这两页存在
        self.var_depth = tk.StringVar(value="sections")
        self._depth_labels: List[tk.Widget] = []

        self.title("daily_arxiv — 文献读取与 arXiv 推荐")
        self.geometry("1220x860")
        self.minsize(1000, 700)
        self.configure(background=theme.BG)

        self.font_name = _ui_font(self)
        try:
            from tkinter import font as tkfont
            for key in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                tkfont.nametofont(key).configure(family=self.font_name, size=10)
            tkfont.nametofont("TkFixedFont").configure(size=9)
        except Exception:
            pass
        # 样式要在建控件之前装好, 否则先建的控件拿不到新主题
        theme.apply(self, self.font_name)

        self._build_header()
        self.nb = ttk.Notebook(self)
        self.nb.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 10))
        self.tab_read = ttk.Frame(self.nb, padding=10)
        self.tab_rec = ttk.Frame(self.nb, padding=10)
        self.tab_list = ttk.Frame(self.nb, padding=10)
        self.tab_cfg = ttk.Frame(self.nb, padding=10)
        self.tab_support = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.tab_read, text="  1. 读取文献  ")
        self.nb.add(self.tab_rec, text="  2. 文献推荐  ")
        self.nb.add(self.tab_list, text="  3. 文献列表  ")
        self.nb.add(self.tab_cfg, text="  4. 设置  ")
        self.nb.add(self.tab_support, text="  5. 支持一下  ")

        # 文献列表的数据 (索引库里的记录)。建控件之前先置空, 免得
        # _on_tab_changed 之类的回调在控件还没建好时就来取
        self._lib_rows: List[Dict[str, Any]] = []
        self._lib_shown = 0

        self._build_read_tab()
        self._build_recommend_tab()
        self._build_library_tab()
        self._build_settings_tab()
        self._build_support_tab()
        self._build_statusbar()

        # 全部搭完之后统一底色。ttk 控件不继承背景色, 漏一个就会在白色卡片上
        # 留一块浅灰方块; 让这一步自动做, 比在每个建控件的地方手挑样式可靠。
        theme.unify_backgrounds(self)

        self.reload_config()
        # 第一次运行 (还没有 config.json): 立刻在程序目录下生成一份默认配置。
        # 里面的三个位置写的都是**相对**名字 (library_index.sqlite /
        # recommend_history.sqlite / output), 而相对路径按程序目录解析 ——
        # 于是它们就落在 exe 旁边, 整个文件夹拷到别的机器上记录和报告都跟着走。
        self._ensure_config_file()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_changed())
        # Ctrl + 滚轮 = 滚整页 (见 _bind_ctrl_wheel)。全局挂一次就够 —— 它按控件
        # **类**挂, 和建了几个页面无关; 放进 _scrollable 里每建一页挂一次的话,
        # 一次滚轮会滚好几格。
        self._bind_ctrl_wheel()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        add_log_sink(self._log_sink)
        self.after(POLL_MS, self._poll)
        # 等窗口画出来再弹路径提醒 (构造到一半弹对话框, 后面的控件会画在它上面)
        self.after(400, self._check_data_paths)
        # 推荐记录下拉框要在**开程序时就有东西可选**: 只读 Combobox 的 values 是
        # 空的, 用户看到的就是一个空白框, 得先点一下「重新扫描」才知道里面能有
        # 什么。放到 400ms 之后: 它要读 output/ 下的历史报告, 别和窗口的第一次
        # 绘制抢时间。
        self.after(400, self._reload_rec_runs)

    # ------------------------------------------------------------------
    # 顶栏
    # ------------------------------------------------------------------
    def _build_header(self) -> None:
        # 顶栏用独立的浅蓝底 (theme.HEADER_BG)。它的控件全部由这里创建、
        # 全部显式指定 Header.* 样式, 所以换底色不会波及别处。
        bar = ttk.Frame(self, style="Header.TFrame")
        bar.pack(side="top", fill="x")

        inner = ttk.Frame(bar, style="Header.TFrame")
        inner.pack(side="top", fill="x", padx=14, pady=(10, 9))

        left = ttk.Frame(inner, style="Header.TFrame")
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="daily_arxiv", style="H1.TLabel").pack(
            side="top", anchor="w")
        self.lbl_config = ttk.Label(left, text="", style="Header.TLabel")
        self.lbl_config.pack(side="top", anchor="w", pady=(2, 0))

        ttk.Button(inner, text="打开配置文件", style="Header.TButton",
                   command=self._open_config_file).pack(side="right")
        ttk.Button(inner, text="打开输出目录", style="Header.TButton",
                   command=lambda: self._open_dir(
                       resolve_path(self.cfg.get("output", {}).get("dir", "output")))
                   ).pack(side="right", padx=8)
        # 默认不显示 dbg 级日志 —— 光是"同一篇文献出现在多个文件夹"这类就上百行,
        # 会把真正的警告和错误冲掉
        self.var_verbose = tk.BooleanVar(value=False)
        ttk.Checkbutton(inner, text="详细日志", variable=self.var_verbose,
                        style="Header.TCheckbutton").pack(side="right", padx=10)

        # 1px 分隔线: 经典 Tk 控件, 自己管底色
        tk.Frame(self, height=1, background=theme.BORDER).pack(side="top", fill="x")

    def _build_statusbar(self) -> None:
        # 白底带子 (Status.TFrame), 标签跟着它同色 —— 这样"控件必须和父容器
        # 同色"这条规则就没有例外, 顶栏和状态栏是仅有的两条带子。
        bar = ttk.Frame(self, style="Status.TFrame")
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, height=1, background=theme.BORDER).pack(side="top", fill="x")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status, anchor="w",
                  style="Status.TLabel").pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------
    # 标签 1: 读取文献
    # ------------------------------------------------------------------
    def _build_read_tab(self) -> None:
        # 这一页内容比一屏高 (来源 + 文件夹表 + 深度选择器 + 按钮 + 进度 + 日志),
        # 小屏幕上最下面的运行日志会被挤没 —— 和设置页一样套一层可滚动容器。
        page = self._scrollable(self.tab_read)
        t = page
        top = ttk.LabelFrame(t, text="文献来源")
        top.pack(side="top", fill="x", padx=8, pady=(8, 4))
        self.var_source = tk.StringVar()
        ttk.Label(top, textvariable=self.var_source, justify="left",
                  wraplength=1000).pack(side="top", anchor="w", padx=8, pady=(6, 4))

        ttk.Label(t, text="文献 PDF 文件夹 (只在这一页增删; 也写进 config.json 的 "
                          "pdf_folders):",
                  style="Muted.TLabel").pack(side="top", anchor="w", padx=10)
        self.folder_editor_read = FolderEditor(t, self)
        self.folder_editor_read.pack(side="top", fill="both", expand=True,
                                     padx=8, pady=(2, 6))
        self._build_depth_selector(t, pady=(0, 6), padx=8)

        act = ttk.Frame(t)
        act.pack(side="top", fill="x", padx=8, pady=(2, 0))
        self.btn_read = ttk.Button(act, text="开始读取", style="Accent.TButton",
                                   command=self.start_read)
        self.btn_read.pack(side="left")
        self.btn_reread = ttk.Button(act, text="全部重读 (忽略索引)",
                                     command=lambda: self.start_read(force=True))
        self.btn_reread.pack(side="left", padx=8)
        ttk.Button(act, text="查看已读记录", command=self.show_index_stats
                   ).pack(side="left", padx=8)
        self.btn_clear_index = ttk.Button(act, text="清空索引", style="Quiet.TButton",
                                          command=self.clear_index)
        self.btn_clear_index.pack(side="left")

        pf = ttk.Frame(t)
        pf.pack(side="top", fill="x", padx=8, pady=(10, 2))
        self.pb_read = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb_read.pack(side="left", fill="x", expand=True)
        self.lbl_read_pct = ttk.Label(pf, text="  0%", width=6,
                                      style="Muted.TLabel")
        self.lbl_read_pct.pack(side="left")

        self.txt_read_log = self._make_log_pane(t, "运行日志")
        self._wheel_passthrough(page, self.folder_editor_read.tree,
                                self.txt_read_log)

    # ------------------------------------------------------------------
    # 标签 2: 文献推荐
    # ------------------------------------------------------------------
    def _build_recommend_tab(self) -> None:
        # 参数 + 分类复选框 + 深度选择器 + 按钮 + 进度 + 日志 + 结果表, 一屏放不
        # 下; 不套滚动容器的话最下面的运行日志和结果表会被挤出可视区。
        page = self._scrollable(self.tab_rec)
        # 详解按内容变高时要回头通知这一层重算滚动范围 (见 _refit_page)
        self.page_rec = page
        t = page
        opt = ttk.LabelFrame(t, text="参数")
        opt.pack(side="top", fill="x", padx=8, pady=8)

        r1 = ttk.Frame(opt)
        r1.pack(side="top", fill="x", padx=8, pady=4)
        ttk.Label(r1, text="推荐篇数").pack(side="left")
        self.var_top = tk.StringVar(value="20")
        ttk.Spinbox(r1, from_=1, to=200, width=5, textvariable=self.var_top
                    ).pack(side="left", padx=(4, 14))
        ttk.Label(r1, text="并发数").pack(side="left")
        self.var_conc = tk.StringVar(value="3")
        ttk.Spinbox(r1, from_=1, to=16, width=4, textvariable=self.var_conc
                    ).pack(side="left", padx=(4, 14))
        self.var_use_ai = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text="使用 AI", variable=self.var_use_ai).pack(side="left")
        self.var_enrich = tk.BooleanVar(value=True)
        ttk.Checkbutton(r1, text="补充引用数", variable=self.var_enrich
                        ).pack(side="left", padx=10)
        # "读取全文"这个复选框撤了 —— 它和下面那个"读取深度"是同一件事的两个
        # 开关, 而且它排在前面、默认勾着, 看起来像是深度之外的额外加成。实际
        # 上取消勾选会**覆盖**深度选择: 传 no_fulltext 让管线把 read_depth 退回
        # sections。于是"深度选了全文 + 这里没勾"这种自相矛盾的组合必然出现,
        # 用户看到的则是"我明明选了全文, 怎么没读全文"。
        # 现在只留"读取深度"一处, 它才是唯一的说法。命令行那边 --no-fulltext
        # 保留 (脚本里想临时退一档, 有个开关比改配置方便)。
        #
        # 推荐记录: 以前推荐过的论文会被标记, 勾上则直接从候选里剔除。
        # 默认不勾 —— 打开后推荐列表会明显变化 (也可能凑不满篇数), 应该是
        # 用户主动选的行为, 不该是默认。
        self.var_skip_seen = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="跳过已推荐过的", variable=self.var_skip_seen
                        ).pack(side="left", padx=10)

        r2_1 = ttk.Frame(opt)
        r2_1.pack(side="top", fill="x", padx=8, pady=4)
        ttk.Label(r2_1, text="检索式", width=10).pack(side="left")
        self.var_queries = tk.StringVar()
        # 留着引用是为了订阅模式下能把它置灰 —— 见 _refresh_cat_label。
        self.ent_queries = ttk.Entry(r2_1, textvariable=self.var_queries)
        self.ent_queries.pack(side="left", fill="x", expand=True)
        self.lbl_queries_hint = ttk.Label(
            opt, text="留空 = 按文献库自动生成; 多条用 ; 分隔",
            style="CardMuted.TLabel")
        self.lbl_queries_hint.pack(side="top", anchor="w",
                                   padx=(112, 8), pady=(0, 6))

        # 限定分类: 勾选式。以前是个自由文本框, 要求用户背下 cond-mat.str-el
        # 这种分类代码 —— 拼错一个字母不会报错, 只会静默地搜到 0 条, 然后用户
        # 以为是程序坏了。复选框列表把可选项摆出来, 不勾 = 不限。
        cat_box = ttk.LabelFrame(opt, text="限定分类 (一个都不勾 = 不限分类)")
        cat_box.pack(side="top", fill="x", padx=8, pady=(0, 6))
        self.var_cats = {}      # type: Dict[str, tk.BooleanVar]
        for gi, (group, items) in enumerate(CATEGORY_CHOICES):
            col = gi % 3
            row = (gi // 3) * 2
            grp = ttk.Frame(cat_box)
            grp.grid(row=row, column=col, sticky="nw", padx=(10, 24), pady=(4, 0))
            ttk.Label(grp, text=group, style="CardMuted.TLabel").pack(
                side="top", anchor="w")
            for code, label in items:
                var = tk.BooleanVar(value=False)
                self.var_cats[code] = var
                ttk.Checkbutton(grp, text="%s  %s" % (code, label),
                                variable=var,
                                command=self._refresh_cat_label).pack(
                                    side="top", anchor="w")
        cat_btns = ttk.Frame(cat_box)
        cat_btns.grid(row=1, column=0, columnspan=3, sticky="w",
                      padx=10, pady=(6, 6))
        ttk.Button(cat_btns, text="全不选 (不限)",
                   command=lambda: self._set_categories([])).pack(side="left")
        for _name, _codes in (("常用: 凝聚态", ["cond-mat.str-el",
                                              "cond-mat.stat-mech"]),
                              ("量子物理", ["quant-ph"])):
            ttk.Button(cat_btns, text=_name,
                       command=lambda c=_codes: self._set_categories(c)
                       ).pack(side="left", padx=6)
        self.lbl_cats = ttk.Label(cat_btns, text="", style="CardMuted.TLabel")
        self.lbl_cats.pack(side="left", padx=10)

        # 订阅模式: 只订阅这几个分类的当天公告, 一条检索式都不用。
        # 灵感来自 zotero-arxiv-daily —— 它的候选池就是这么来的 (见 README)。
        # 关键词检索的毛病是"换个说法的好文章搜不到"; 订阅反过来, 宁可多抓,
        # 交给后面的相关性打分去筛。两种方式各有所长, 所以做成开关而不是替换。
        sub_row = ttk.Frame(cat_box)
        sub_row.grid(row=3, column=0, columnspan=3, sticky="w",
                     padx=10, pady=(0, 8))
        self.var_subscribe = tk.BooleanVar(value=False)
        ttk.Checkbutton(sub_row, text="订阅模式: 只抓勾选分类的当天公告, 不用检索式",
                        variable=self.var_subscribe,
                        command=self._refresh_cat_label).pack(side="left")
        self.lbl_subscribe = ttk.Label(sub_row, text="", style="CardMuted.TLabel")
        self.lbl_subscribe.pack(side="left", padx=10)
        self._refresh_cat_label()

        # 读取深度在这一页也要能改: 它是"送进相关性分析的内容有多少", 直接决定
        # 推荐质量 —— 用户不该为了调它专门切到别的页去。
        self._build_depth_selector(opt, pady=(0, 8), padx=8, compact=True)

        act = ttk.Frame(t)
        act.pack(side="top", fill="x", padx=8)
        self.btn_run = ttk.Button(act, text="开始推荐", style="Accent.TButton",
                                  command=self.start_recommend)
        self.btn_run.pack(side="left")
        self.btn_stop = ttk.Button(act, text="停止", style="Quiet.TButton",
                                   command=self.request_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        self.btn_report = ttk.Button(act, text="打开报告", command=self.open_report,
                                     state="disabled")
        self.btn_report.pack(side="left")

        # --- 推荐记录: 看哪一次 ---
        # 以前这是个按钮, 点一下把**整库**铺到下面的列表里。跑过三轮之后, 想看
        # 的是"第二轮推荐了什么", 而那个按钮只会把三轮混成一张按时间排的大表。
        # 现在换成下拉框: 一次运行一个选项 (标签带时间, 见 past_runs.label_for),
        # 选哪个下面就铺哪个。老行为没丢 —— 它是最后那个"全部累计记录"。
        #
        # 「删除推荐记录」仍然单独一个按钮: 看和删共用一次点击的话, 想翻一眼记录
        # 的人每次都得先绕过一次删除确认。
        rec = ttk.Frame(t)
        rec.pack(side="top", fill="x", padx=8, pady=(6, 0))
        ttk.Label(rec, text="推荐记录").pack(side="left")
        self.var_rec_run = tk.StringVar(value=REC_LIVE)
        self.cmb_rec_run = ttk.Combobox(rec, textvariable=self.var_rec_run,
                                        state="readonly", width=56)
        self.cmb_rec_run.pack(side="left", padx=(4, 8))
        self.cmb_rec_run.bind("<<ComboboxSelected>>", self._on_rec_pick)
        ttk.Button(rec, text="重新扫描", command=self.refresh_rec_runs
                   ).pack(side="left")
        ttk.Button(rec, text="删除推荐记录", style="Quiet.TButton",
                   command=self.clear_history).pack(side="left", padx=8)

        pf = ttk.Frame(t)
        pf.pack(side="top", fill="x", padx=8, pady=(10, 2))
        self.pb_run = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pb_run.pack(side="left", fill="x", expand=True)
        self.var_stage = tk.StringVar(value="  未开始")
        ttk.Label(pf, textvariable=self.var_stage, width=36,
                  style="Muted.TLabel").pack(side="left")

        # 运行日志紧贴在进度条下面 —— 跑的时候用户盯着的是这两样: 进度条说
        # "到哪一步了", 日志说"这一步具体在干什么"。以前它放在整页最底下,
        # 结果表一占位置就把它挤没了, 等于跑的时候什么都看不到。
        self.txt_run_log = self._make_log_pane(
            t, "运行日志 (每一步都写在这里; 报告和推荐记录在最后一步才落盘)",
            height=8)

        # 结果表和详解以前装在 ttk.Panedwindow 里, 中间那条分隔条可以拖 —— 做它
        # 是为了"详解能拉长一点, 多看几行"。现在详解改成**按内容撑开、自己不滚动**
        # (见 _fit_detail_height), 它永远显示全部, 分隔条就没有可调的东西了:
        # 内容有多高, 详解就多高, 拖到哪儿都一样。所以整块撤掉, 换成上下两块各自
        # 摆好 —— 列表固定行数、自己带滚动条 (它只管"选哪篇"), 详解整段铺开, 由
        # **整页**那层滚动容器滚。
        #
        # --- 结果表 ---
        top = ttk.Frame(t)
        top.pack(side="top", fill="x", padx=8, pady=(6, 0))
        # 列多了 (作者/提交日期/期刊), 窄窗口下会撑出可视区, 所以下面配了横向
        # 滚动条 —— 没有它的话右边那几列在 1080p 上直接看不见, 用户只能猜。
        # 相关/时效/重要这三项细分数从列表里撤了: 下面"详解"里本来就有完整一行
        # ("相关 X (权重后 Y) · 时效 Z · 重要 W"), 列表里留个总分够用了。
        cols = ("rank", "title", "authors", "pub", "journal", "score", "flag")
        # 行数是**定值**: 列表是"选哪篇"的入口, 12 行足够挑, 再多就得跟下面的
        # 详解抢屏幕。要翻更多用列表自己的滚动条 (滚轮在列表上时归它)。
        self.tree = ttk.Treeview(top, columns=cols, show="headings",
                                 height=self.TABLE_ROWS, selectmode="browse")
        heads = (("#", 40), ("论文名", 470), ("作者", 130), ("提交日期", 92),
                 ("期刊", 150), ("总分", 62), ("标记", 132))
        for c, (txt, w) in zip(cols, heads):
            self.tree.heading(c, text=txt)
            anchor = "w" if c == "title" else "center"
            self.tree.column(c, width=w, minwidth=40, anchor=anchor,
                             stretch=(c == "title"))
        vs = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(top, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        # 斑马纹: 26px 行高下, 隔行浅底比网格线更容易横向追行。
        # "top" 必须排在最前 —— 同一行有多个 tag 时, 排在前面的先决定背景,
        # 所以推荐行的高亮不会被斑马纹盖掉。
        self.tree.tag_configure("even", background=theme.ROW_ALT)
        self.tree.tag_configure("odd", background=theme.SURFACE)
        self.tree.tag_configure("top", background=theme.ACCENT_SOFT)
        for _v in ("good", "mid", "low"):
            self.tree.tag_configure(
                _v, foreground=getattr(theme, "SCORE_" + _v.upper()))
        self.tree.bind("<<TreeviewSelect>>", self._on_select_candidate)
        self.tree.bind("<Double-1>", self._on_tree_double)

        # --- 详情 ---
        det = ttk.LabelFrame(t, text="详解 "
                                     "(双击列表里的论文会用浏览器打开原文)")
        self.det_detail = det
        det.pack(side="top", fill="x", padx=8, pady=(6, 8))
        # height=1 只是建的时候的一个占位, 真正的行数由 _fit_detail_height 按内容
        # 算出来 —— 没有自己的滚动条, 所以"装不下"这种事必须不发生。
        self.txt_detail = tk.Text(det, wrap="word", height=1, padx=12, pady=8,
                                  state="disabled", background=theme.SURFACE,
                                  relief="flat", highlightthickness=0,
                                  font=(self.font_name, 10), spacing1=1,
                                  spacing3=2, insertbackground=theme.FG)
        self.txt_detail.pack(side="left", fill="both", expand=True)
        # 宽度变了要重算 (折行数跟着变) —— 但高度是我们自己在改的, 得先分辨是
        # 哪一种, 所以中间隔一层 _on_detail_configure。
        self.txt_detail.bind("<Configure>", self._on_detail_configure)

        self._build_chat_pane(t)

        # 详解**不**进这个名单: 它现在自己不带滚动条, 滚轮在它上面就该滚整页。
        # 对话区要进: 它是个定高、自带滚动条的框, 滚轮在它上面得翻对话。
        self._wheel_passthrough(page, self.tree, self.txt_run_log, self.txt_chat)

    # ------------------------------------------------------------------
    # 和 AI 深入讨论某一篇
    #
    # 详解是"AI 一次性给出的解读", 讨论是"就着这篇论文往下聊"。两者刻意分开:
    #   * 聊完**不会**自动重写详解 —— 聊十句攒下的理解, 什么时候落进详解由用户
    #     决定 (那个「用对话更新详解」按钮)。每聊一句就重写一遍的话, 用户会看到
    #     详解在脚下不断变形, 而且每问一句都要多花一次 AI 调用。
    #   * 讨论本身存在这篇论文的记录里 (history.py 的 chat 表), 换一篇再换回来
    #     还在, 关掉程序也还在。
    # ------------------------------------------------------------------
    def _build_chat_pane(self, t: tk.Misc) -> None:
        box = ttk.LabelFrame(t, text="和 AI 深入讨论这篇 "
                                     "(选中列表里的一篇, 在下面接着追问)")
        box.pack(side="top", fill="x", padx=8, pady=(0, 8))

        head = ttk.Frame(box)
        head.pack(side="top", fill="x", padx=8, pady=(6, 2))
        self.var_chat_paper = tk.StringVar(value="还没有选中论文")
        ttk.Label(head, textvariable=self.var_chat_paper,
                  style="CardMuted.TLabel").pack(side="left")
        # 「更新详解」用 Accent 描出来: 它是这一块里**唯一会改数据**的动作
        # (会写进推荐记录), 其余几个都只是看或清。
        self.btn_chat_detail = ttk.Button(head, text="用对话更新详解",
                                          style="Accent.TButton",
                                          command=self.update_detail_from_chat)
        self.btn_chat_detail.pack(side="right")
        # CardQuiet 而不是 Quiet: 这一块是**卡片**(白底), 而 Quiet.TButton 的
        # 底色是页面灰 —— 摆进去就是白卡片上的一块灰方块 (自检的底色检查抓过)。
        self.btn_chat_clear = ttk.Button(head, text="清空这段对话",
                                         style="CardQuiet.TButton",
                                         command=self.clear_chat)
        self.btn_chat_clear.pack(side="right", padx=8)

        body = ttk.Frame(box)
        body.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 2))
        # 定高 + 自己的滚动条 (和运行日志一个道理): 讨论可以很长, 但它不该把
        # 整页越撑越高。
        self.txt_chat = tk.Text(body, height=12, wrap="word", state="disabled",
                                background=theme.SURFACE, relief="flat",
                                highlightthickness=0, padx=10, pady=6,
                                font=(self.font_name, 10), spacing1=1, spacing3=2)
        cys = ttk.Scrollbar(body, orient="vertical", command=self.txt_chat.yview)
        self.txt_chat.configure(yscrollcommand=cys.set)
        self.txt_chat.pack(side="left", fill="both", expand=True)
        cys.pack(side="right", fill="y")
        self.txt_chat.tag_configure("who_user", foreground=theme.ACCENT_DARK,
                                    font=(self.font_name, 10, "bold"),
                                    spacing1=6)
        self.txt_chat.tag_configure("who_ai", foreground=theme.ACCENT,
                                    font=(self.font_name, 10, "bold"),
                                    spacing1=6)
        self.txt_chat.tag_configure("msg", lmargin1=4, lmargin2=14, spacing3=2)
        self.txt_chat.tag_configure("sys", foreground=theme.MUTED,
                                    lmargin1=4, lmargin2=4, spacing1=4)

        inp = ttk.Frame(box)
        inp.pack(side="top", fill="x", padx=8, pady=(2, 4))
        self.var_chat_in = tk.StringVar()
        self.ent_chat = ttk.Entry(inp, textvariable=self.var_chat_in)
        self.ent_chat.pack(side="left", fill="x", expand=True)
        self.ent_chat.bind("<Return>", lambda e: self.send_chat())
        self.btn_chat_send = ttk.Button(inp, text="发送", style="Accent.TButton",
                                        command=self.send_chat)
        self.btn_chat_send.pack(side="left", padx=(6, 0))
        ttk.Label(box, text="回车发送。讨论会存在这篇论文的记录里; 想让它变成详解, "
                            "点右上角「用对话更新详解」—— 不会每聊一句就自动改详解。",
                  style="CardMuted.TLabel", wraplength=1100,
                  justify="left").pack(side="top", anchor="w", padx=10, pady=(0, 6))
        self._render_chat()

    # ------------------------------------------------------------------
    # "详解"面板的高度: 按内容撑开, 自己不滚动
    #
    # 用户的原话是"详解不需要单独设置滚轮, 直接显示全部, 用文献推荐页面的滚轮
    # 查看"。也就是说那块面板里**不该有第二个滚动条** —— 一份解读有多长, 它就
    # 该有多高; 看的时候滚整页。
    #
    # 高度怎么算 —— 两条规矩:
    #
    # 1. **内容换了 (或宽度变了) 就重新估**: ``Text.count(..., "displaylines")``
    #    给出"按当前宽度折行之后占多少显示行", 加一行余量设上去。裸 Text 里量过:
    #    18 个显示行按 19 行设正好装得下。**但也只是"正好"** —— 一行一行短句的
    #    内容平均要 24 px/显示行, 而 -height 一个单位只有 22 px, 所以显示行一多
    #    (19 行往上) 这么估就偏短, 必须靠第 2 条兜。
    # 2. **之后只往上补, 不缩**: 读 ``yview()``, 底边小于 1 就是"还有内容压在下面
    #    看不见", **一次补一行**。缩只发生在第 1 条 (内容真换了) —— 否则"补过头
    #    再缩回来、缩回来又差一行再补"就成了来回抖。一次只补一行, 是因为按 y1
    #    反推"内容总高 / 现有高度"那套在"控件被压扁"的时候会算出一个荒唐的倍数
    #    (实测被顶到 400 行的上限)。
    #
    # 为什么非要第 2 条 (页面里它不是保险, 是必须的): 估出来的行数**系统性偏短**。
    # 实测一条真实记录: 数出 43 个显示行, 按 44 行设高度, Text 的请求高度是 986
    # 像素, 而内容实际要 1013 像素 —— 差 27 像素 (一行多一点)。所以光靠估必然
    # 露不出最后一行, 必须回过头问控件自己 (yview)。
    #
    # 而且"问"这件事本身也有两个前提, 都实测过:
    #   * 控件**实际**高度得先落到位。页面里的内层 Frame 是被 canvas 的窗口项钉
    #     住尺寸的, 请求高度要一级一级传上去, 而传上去的那一刀 (见 _scrollable
    #     里的 _fit) 读的又是**滞后一拍**的请求高度 —— 少了那一拍, 控件就永远
    #     差着一大截 (实测: 请求 986 像素、实际 282, 内层 Frame 请求 2145、实际
    #     1441, 差的那 704 像素整块露不出来)。yview 这时候报的数不作数。
    #   * 得**等它稳下来再问**, 而且**一个高度只问一次** —— 见 _fit_detail_height
    #     里的 stable / _detail_checked。
    #   * 那串回调得用 **after_idle** 排, 不能用 after(0): 0 毫秒定时器会一直占着
    #     "有事要做"的位置, 而 Tk 只在没事做的时候才跑空闲任务 (重算几何就是空闲
    #     任务)。实测用 after(0) 连着 48 拍, 内层的请求高度冻在 1441 一动不动,
    #     而里面的详解框早就请求 986 了; 换成 after_idle 第三拍就落位。
    #
    # 两个坑 (都实测过):
    #   * widget 还没映射时 winfo_width() 是 1, 这时 count 给的是垃圾 —— 同一段
    #     文字, 宽度 1 时数出 1420 行, 宽度 900 时是 24 行。yview 这时也不作数。
    #     所以宽度/高度 <= 1 直接返回;
    #   * 改 height **不会**改变 count 的结果 (24 还是 24), 所以不会自激; 但改
    #     **宽度**会 (900px 24 行 -> 500px 42 行), 所以 <Configure> 上必须重算。
    # ------------------------------------------------------------------
    def _detail_line_count(self, t: tk.Text) -> int:
        """按当前宽度数一下内容占几个显示行; 数不出来给 0。"""
        try:
            n = t.count("1.0", "end", "displaylines")
        except Exception:
            return 0
        if isinstance(n, (tuple, list)):
            n = n[0] if n else 0
        try:
            return int(n)
        except (TypeError, ValueError):
            return 0

    def _on_detail_configure(self, e: Any = None) -> None:
        """详解框的尺寸变了。**只有宽度变了才算"内容要重新估"**。

        高度是我们自己在这里改的: 把它也当成"重新估", 就会估 -> 改高 -> 触发
        <Configure> -> 又估 -> 又改, 自己咬自己。宽度变了才是真得重算 (折行数
        跟着变)。
        """
        try:
            w = int(getattr(e, "width", 0))
        except Exception:
            w = 0
        fresh = bool(w) and w != self._detail_w
        self._detail_w = w
        self._settle_detail(None, 0, fresh)

    def _fit_detail_height(self, fresh: bool = False) -> bool:
        """调一次详解框的高度; 返回"还要不要再跑一拍"。

        ``fresh=True``: 内容刚换 (或宽度刚变) —— 按数出来的行数重新估, 可以变矮。
        ``fresh=False``: 同一段内容, 只按 yview 往上补, 不缩。

        规矩就一条: **请求还没落到控件上, 就什么都别改, 等它** —— 见下面 stable。
        """
        t = getattr(self, "txt_detail", None)
        if t is None:
            return False
        try:
            if t.winfo_width() <= 1 or t.winfo_height() <= 1:
                return False            # 还没布局出来, 这会儿量了也不算数
            act = int(t.winfo_height())
        except Exception:
            return False

        # 两条关于"什么时候才能动手"的账:
        #
        #   * ``stable``: 控件的高度跟上一拍一样吗。**没稳的时候一个字节都不能改**
        #     —— 每次 configure(height=...) 都会让 Tk 把"重新算几何"那个任务作废
        #     重排, 于是"改 -> 几何作废 -> 高度没变 -> 还是装不下 -> 再改"就成了
        #     活锁。实测这样连着十几拍, 控件高度一直冻在 62 像素 (建的时候那个
        #     height=1 留下的), yview 一直报 0.1333, 高度一路被推到 47 行 (1052
        #     像素) 才停, 而内容其实只有 14 行。
        #   * ``_detail_checked``: 这个高度上**已经核对过** yview 了。核对过就不
        #     再重复核对, 否则又转回上面那个活锁 (每拍都"发现"还差一行)。几何真
        #     的又动了, 这个值自然对不上, 于是重新获得一次核对机会。
        #
        # 还有一条也一并查了: **控件被页面压扁的时候不核对** (act < 请求高度)。
        # 压扁的时候 yview 报的是"这个矮框装不下", 而那是高度还没落到位, 不是
        # 内容真的装不下 —— 在它上面补一行纯属白补 (实测每档都多一行空白)。
        # 页面那一刀修好之后, 稳定状态里 act 和请求高度是**相等**的 (uitest 里
        # 有这条断言), 所以这条不会把核对永远挡住。
        stable = (act == self._detail_seen)
        self._detail_seen = act

        n = self._detail_line_count(t)
        if not n:
            return False
        # 数到 "end" 而不是 "end-1c": put() 每次都在末尾补一个换行, 最后那一行
        # 空行也是**占高度**的, 漏掉它就少算一行。上限防跑飞, 下限 1 行, 空内容
        # 也别塌成 0。
        est = max(1, min(n, self.DETAIL_MAX_LINES)) + 1

        cur = self._detail_lines
        if fresh or not cur:
            # 头一回, 或内容刚换: 按估的重新来。**这一拍不看 yview** —— 内容刚换
            # 时控件的高度还是上一段内容的, yview 报的是那一段的账。等它稳下来
            # (_detail_checked 对不上) 才轮到核对。
            #
            # 这里把 _detail_checked 清成 -1 (而不是记成当前高度) 是要紧的: 换到
            # 一段**高度刚好一样**的内容时 (记录库里 20 行的有好几条), 记成当前
            # 高度就等于说"这个高度核对过了", 而它核对的是上一段内容 —— 新内容
            # 于是永远轮不到核对, 露不出来的那一行也就永远补不上 (实测: 38 条里
            # 就漏了这么一条)。清成 -1 只是把核对推到下一拍, 控件高度不变的时候
            # yview 报的就是新内容的账。
            want = est
            self._detail_checked = -1
        elif (stable and not self._detail_squeezed(t)
              and act != self._detail_checked):
            # 高度稳了、控件拿到了它请求的高度、而且这个高度还没核对过 —— 这才
            # 轮到 yview 说话。**"拿到了请求的高度"这一条不能省**: 换内容那一拍
            # 设下去的高度要等几何跑完才落到控件上, 中间这几拍控件还是**上一段
            # 内容**的高度, 在它上面读 yview 会读出一个假的"装不下", 白白多补一
            # 行 (实测扫长度那一遍每条都多一行)。而控件拿到请求高度时还装不下,
            # 那就是真的装不下。
            #
            # 只往上补; est 本身也得算数 (窗口变宽变窄之后 count 会变), 但绝不
            # 因为"估得比现在矮"就缩回去 —— 缩只发生在 fresh 那一拍, 否则
            # "补过头 -> 缩回去 -> 又差一行 -> 再补"就成了来回抖。
            self._detail_checked = act
            want = max(est, cur)
            y1 = self._detail_y1(t)
            if 0.0 < y1 < 0.999:
                # 下面还压着内容。**一次只补一行**: 按比例补是拿 y1 反推"内容总高
                # / 现有高度", 而 y1 这会儿也不见得准, 一补就是一大步, 而"只往上
                # 不缩"的规矩下过头了退不回来。
                want += 1
        else:
            want = cur                  # 等它稳下来 (或这个高度已经核对过了)
        want = min(want, self.DETAIL_MAX_LINES + 2)

        if want != cur:
            self._detail_lines = want
            try:
                t.configure(height=want)
            except Exception:
                return False
            return True                 # 改了, 等它稳下来再看一眼
        # 没改。还要不要再跑一拍? "没稳"要 (等几何), "这个高度还没核对过"也要
        # (内容刚换那一拍把 _detail_checked 清了, 核对要落在下一拍)。
        return (not stable) or (act != self._detail_checked)

    @staticmethod
    def _detail_y1(t: tk.Text) -> float:
        """yview 的底边 —— 1.0 就是内容全露着; 读不出来当 1.0 (当作够)。"""
        try:
            return float(t.yview()[1])
        except Exception:
            return 1.0

    def _detail_squeezed(self, t: Any = None) -> bool:
        """详解框**实际**比它请求的矮吗 —— 矮就是父容器还没把高度给它。

        这是"还要再跑一拍"的第三个理由, 而且是唯一一个**不用猜**的理由: 页面里
        内层 Frame 的尺寸是 canvas 的窗口项钉住的, 我们改 Text 的高度只是让它
        "请求"变高, 请求要一级一级传到内层、再由 _fit 把窗口项顶上去, 控件实际
        才会变高。中间那几拍控件是被 pack **压扁**的 —— 实测差过 704 像素 (请求
        986、实际 282), 而且 _fit 读的请求高度本身滞后一拍, 那一拍里它算出"没
        变化"就不改了, 于是**再也没有人**来补这一刀, 详解最后 704 像素永远看不
        见。所以只要还压着, 就接着跑 (拍数由 DETAIL_SETTLE_PASSES 兜底)。

        没映射出来 (高度 <= 1) 不算 —— 那会儿请求高度也不作数。
        """
        t = t if t is not None else getattr(self, "txt_detail", None)
        if t is None:
            return False
        try:
            act = int(t.winfo_height())
            return act > 1 and act < int(t.winfo_reqheight())
        except Exception:
            return False

    def _settle_detail(self, _e: Any = None, _tries: int = 0,
                       fresh: bool = False) -> None:
        """定高 + 让整页滚动范围跟上, 连着跑几拍。

        **必须连着跑**: configure(height=...) 只是把"我要多高"记在 Text 自己身
        上, 要等 pack 下一次空闲跑起来, 这个请求才逐级传到内层 Frame。同步去读
        内层的 winfo_reqheight() 拿到的还是**旧值**, _fit 于是认为"没变高", 窗口
        项的高度不动, 内层的实际尺寸也就不动 —— 而 <Configure> 只在尺寸真变了
        的时候才发, 于是再也没有人来补这一刀。表现出来就是: _detail_lines 明明
        算对了 49 行, 详解却永远停在一行高 (实测踩到)。

        yview 那一步同理: 高度设下去, 要等 Tk 重排完它才报得准 —— 所以"补到真
        的装得下"这一刀也落在后面几拍里, 不是一拍能完的事。

        什么时候停: 三个都说"没事可做"了才停 —— ``_fit_detail_height`` (高度不用
        再调了)、``_refit_page`` (窗口项高度不用再改了)、以及"控件没被压扁"。
        **后两个也算数**: _refit 读的请求高度滞后一拍, 常常是"这一拍改高度、下一
        拍才轮到它改窗口项", 只看第一个的那一版就在这一拍收工了, 于是内层永远
        差着那 704 像素 (实测见 _scrollable 里 _fit 的说明); 而"还被压着"正是
        请求高度还没落下来的证据 (见 _detail_squeezed)。每一拍都是幂等的 (高度
        没变、窗口项没变就什么都不做), 拍数用完自然停, 不会自激; 上限
        DETAIL_SETTLE_PASSES 防的是"宽度一直在变"那种跑飞。
        """
        more = False
        try:
            more = self._fit_detail_height(fresh)
        except Exception:
            pass
        if self._refit_page():
            more = True                 # 窗口项改了, 控件实际高度会跟着变, 再看一拍
        elif self._detail_squeezed():
            # 这一拍 _fit 算出"没变化"不代表没事了: 它读的请求高度滞后一拍, 控件
            # 现在还被压着就是证据。再等一拍, 让请求高度落下来给它看 (见
            # _detail_squeezed)。
            more = True
        if more and _tries < self.DETAIL_SETTLE_PASSES:
            try:
                # **用 after_idle, 不能用 after(0)**: 定时的 0 毫秒事件会一直占着
                # "有待处理事件"这个位置, 而 Tk 只在**没有别的活**的时候才跑空闲
                # 任务 —— 重新算几何正是空闲任务。于是链子活着的时候几何永远排不
                # 上: 实测连着 48 拍, 内层的请求高度冻在 1441 一动不动, 而里面的
                # 详解框早就请求 986 了; 那串回调一停, 几何立刻跑上, 请求高度当场
                # 变成 2145。空闲回调是一队 FIFO: 我们这一拍改高度会让 Tk 把几何
                # 任务排进空闲队列, 而我们下一拍排在它后面 —— 正好让它先跑完。
                self.after_idle(
                    lambda: self._settle_detail(None, _tries + 1, False))
            except Exception:
                pass

    def _refit_page(self, _e: Any = None) -> bool:
        """让整页的滚动范围跟上详解的新高度 (跑一拍); 返回"窗口项改过没有"。

        不能指望 <Configure> 自己传上来: 页面内层的**实际**尺寸是 canvas 的窗口
        项钉住的, 它**请求**的高度变了、实际尺寸没变, <Configure> 就不一定发得出
        来 —— 于是滚动条还按老高度算, 详解最后几行永远滚不到。重排入口由
        _scrollable 挂在页面对象上; 拍数由 _settle_detail 负责 (这个返回值就是
        给它看的: 改过就得再来一拍, 见那里的说明)。
        """
        refit = getattr(getattr(self, "page_rec", None), "refit_page", None)
        if refit is None:
            return False
        try:
            return bool(refit())
        except Exception:
            return False

    # --- 限定分类的复选框 ---
    def _set_categories(self, codes: Any) -> None:
        """把勾选状态设成给定的分类集合 (其余全不勾)。

        ``codes`` 里不认识的分类 (手改过 config.json 的) 直接忽略 —— 复选框里
        没有那一项, 硬塞也显示不出来, 反而会让"保存"把用户手写的分类悄悄删掉。
        """
        want = {str(c).strip() for c in (codes or []) if str(c).strip()}
        for code, var in self.var_cats.items():
            var.set(code in want)
        self._refresh_cat_label()

    def _selected_categories(self) -> List[str]:
        """当前勾选的分类, 按复选框列表的顺序返回 (不勾任何一项就是空列表)。"""
        return [code for _g, items in CATEGORY_CHOICES for code, _l in items
                if self.var_cats.get(code) is not None
                and self.var_cats[code].get()]

    def _refresh_cat_label(self) -> None:
        """旁边那两行小字: 现在限定了几个分类 / 是不是订阅模式。"""
        if not hasattr(self, "lbl_cats"):
            return
        n = len(self._selected_categories())
        self.lbl_cats.config(text="当前: 不限分类" if not n
                             else "当前: 限定 %d 个分类" % n)
        # 订阅模式下检索式会被忽略 (日志里会写"忽略刚生成的 N 条检索式")。与其让
        # 用户填完一栏再被无声丢掉, 不如把输入框置灰 —— 让"这一栏现在不算数"
        # 一眼可见。值本身留着, 取消订阅就恢复。
        if hasattr(self, "ent_queries"):
            on = bool(self.var_subscribe.get())
            self.ent_queries.state(["disabled"] if on else ["!disabled"])
            self.lbl_queries_hint.config(
                text="订阅模式下不生效 (候选全部来自分类公告)"
                if on else "留空 = 按文献库自动生成; 多条用 ; 分隔")

        if not hasattr(self, "lbl_subscribe"):
            return
        if not self.var_subscribe.get():
            self.lbl_subscribe.config(text="")
        elif not n:
            # 订阅模式没有分类就真的什么都抓不到 —— 与其等跑完再报错, 不如现在
            # 就在旁边说清楚。
            self.lbl_subscribe.config(text="← 需要至少勾选一个分类, 否则抓不到东西")
        else:
            self.lbl_subscribe.config(
                text="当前: 订阅 %d 个分类的当天公告 (周末/节假日通常没有新论文)"
                % n)

    # ------------------------------------------------------------------
    # 标签 3: 文献列表 (已读的本地文献)
    # ------------------------------------------------------------------
    def _build_library_tab(self) -> None:
        t = self.tab_list

        head = ttk.Frame(t)
        head.pack(side="top", fill="x", padx=8, pady=(8, 2))
        self.var_lib_head = tk.StringVar(value="还没有读取过文献")
        ttk.Label(head, textvariable=self.var_lib_head).pack(side="left")

        bar = ttk.Frame(t)
        bar.pack(side="top", fill="x", padx=8, pady=(4, 2))
        ttk.Label(bar, text="搜索").pack(side="left")
        self.var_lib_q = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_lib_q, width=28)
        entry.pack(side="left", padx=(4, 10))
        # 输入时过滤: 每敲一个字就重建整张表, 一千篇的库上会明显卡手,
        # 所以延迟 180ms 合并连击
        self._lib_filter_job: Any = None
        self.var_lib_q.trace_add("write", lambda *a: self._queue_lib_filter())
        ttk.Button(bar, text="刷新列表", command=self.refresh_library_list
                   ).pack(side="left")
        self.btn_lib_open = ttk.Button(bar, text="打开 PDF", style="Accent.TButton",
                                       command=self.open_selected_pdf)
        self.btn_lib_open.pack(side="left", padx=8)
        # 这几个按钮的引用都留着 —— 切到"推荐结果"模式时要把它们置灰
        # (那些论文不在本地, 见 _show_lib_mode)
        self.btn_lib_dir = ttk.Button(bar, text="打开所在文件夹",
                                      command=self.open_selected_dir)
        self.btn_lib_dir.pack(side="left")
        self.btn_lib_tags = ttk.Button(bar, text="编辑标签",
                                       command=self.edit_selected_tags)
        self.btn_lib_tags.pack(side="left", padx=8)
        self.btn_lib_clear = ttk.Button(bar, text="清空标签", style="Quiet.TButton",
                                        command=self.clear_selected_tags)
        self.btn_lib_clear.pack(side="left")

        # --- 看哪一份: 已读文献 / 某一次的历史推荐结果 ---
        # 历史推荐结果直接读 output 目录里的报告文件 (见 past_runs.py), 不另建
        # 一份数据库 —— 报告本来就在那儿, 用户能删能拷, 再存一份只会多出"记录
        # 和文件对不上"的可能, 而且改动之前跑的报告就翻不到了。
        sel = ttk.Frame(t)
        sel.pack(side="top", fill="x", padx=8, pady=(4, 0))
        ttk.Label(sel, text="列表内容").pack(side="left")
        self.var_run = tk.StringVar(value=RUN_LIBRARY)
        self.cmb_run = ttk.Combobox(sel, textvariable=self.var_run,
                                    state="readonly", width=52)
        self.cmb_run.pack(side="left", padx=(4, 8))
        self.cmb_run.bind("<<ComboboxSelected>>", self._on_run_pick)
        ttk.Button(sel, text="重新扫描", command=self.refresh_run_list
                   ).pack(side="left")
        self.btn_run_open = ttk.Button(sel, text="打开这份报告",
                                       command=self.open_selected_run_report)
        self.btn_run_open.pack(side="left", padx=8)
        self.btn_run_arxiv = ttk.Button(sel, text="打开选中论文的 arXiv 页",
                                        command=self.open_selected_run_paper)
        self.btn_run_arxiv.pack(side="left")

        self.lbl_lib_hint = ttk.Label(
            t, text="双击某一行 = 用系统默认程序打开这篇 PDF; "
                    "标签可以自己编辑 (存在读取记录里, 不会因为重读文献丢失)",
            style="Muted.TLabel")
        self.lbl_lib_hint.pack(side="top", anchor="w", padx=10)

        holder = ttk.Frame(t)
        holder.pack(side="top", fill="both", expand=True, padx=8, pady=(4, 2))

        # 两个列表叠在同一个位置, 同一时刻只显示一个 (见 _show_lib_mode)。
        body = ttk.Frame(holder)          # 已读文献 (本地 PDF)
        rec_body = ttk.Frame(holder)      # 某一次推荐结果
        cols = ("title", "authors", "publication", "tags", "year", "depth",
                "status")
        self.tree_lib = ttk.Treeview(body, columns=cols, show="headings",
                                     selectmode="browse")
        heads = (("文献名", 400), ("作者", 170), ("发表期刊", 150),
                 ("标签", 160), ("发表时间", 84), ("读取深度", 124),
                 ("状态", 92))
        for c, (txt, w) in zip(cols, heads):
            self.tree_lib.heading(c, text=txt)
            self.tree_lib.column(c, width=w,
                                 anchor="w" if c in ("title", "authors",
                                                     "publication", "tags")
                                 else "center",
                                 stretch=(c == "title"))
        vs = ttk.Scrollbar(body, orient="vertical",
                           command=self.tree_lib.yview)
        self.tree_lib.configure(yscrollcommand=vs.set)
        self.tree_lib.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree_lib.tag_configure("even", background=theme.ROW_ALT)
        self.tree_lib.tag_configure("odd", background=theme.SURFACE)
        # 文件已经不在的 (拔了盘、挪了目录) 用灰字, 一眼能挑出来
        self.tree_lib.tag_configure("gone", foreground=theme.MUTED)
        self.tree_lib.bind("<Double-1>", self._on_lib_double)
        self.tree_lib.bind("<Return>", lambda e: self.open_selected_pdf())
        self.tree_lib.bind("<<TreeviewSelect>>", self._on_lib_select)

        # --- 第二个列表: 某一次推荐结果 ---
        # 列和「文献推荐」页那套保持一致 (论文名/作者/提交日期/期刊/总分/标记),
        # 同一批论文在两个页面里不该长得不一样。
        cols_r = ("rank", "title", "authors", "pub", "journal", "score", "flag")
        self.tree_rec = ttk.Treeview(rec_body, columns=cols_r, show="headings",
                                     selectmode="browse")
        heads_r = (("#", 40), ("论文名", 400), ("作者", 130), ("提交日期", 92),
                   ("期刊", 150), ("总分", 62), ("标记", 120))
        for c, (txt, w) in zip(cols_r, heads_r):
            self.tree_rec.heading(c, text=txt)
            self.tree_rec.column(c, width=w, minwidth=40,
                                 anchor="w" if c == "title" else "center",
                                 stretch=(c == "title"))
        vs_r = ttk.Scrollbar(rec_body, orient="vertical",
                             command=self.tree_rec.yview)
        hs_r = ttk.Scrollbar(rec_body, orient="horizontal",
                             command=self.tree_rec.xview)
        self.tree_rec.configure(yscrollcommand=vs_r.set, xscrollcommand=hs_r.set)
        hs_r.pack(side="bottom", fill="x")
        self.tree_rec.pack(side="left", fill="both", expand=True)
        vs_r.pack(side="right", fill="y")
        self.tree_rec.tag_configure("even", background=theme.ROW_ALT)
        self.tree_rec.tag_configure("odd", background=theme.SURFACE)
        self.tree_rec.bind("<Double-1>", lambda e: self.open_selected_run_paper())
        self.tree_rec.bind("<Return>", lambda e: self.open_selected_run_paper())
        self.tree_rec.bind("<<TreeviewSelect>>", self._on_run_select)

        self._lib_body = body
        self._rec_body = rec_body
        self._runs: List[Dict[str, Any]] = []
        self._run_items: List[Dict[str, Any]] = []
        self._run_mode = False
        self._show_lib_mode(False)

        self.var_lib_path = tk.StringVar(value="")
        ttk.Label(t, textvariable=self.var_lib_path, style="Muted.TLabel",
                  anchor="w", wraplength=1100).pack(
            side="top", fill="x", padx=10, pady=(2, 6))

    # --- 已读文献 / 历史推荐结果 两个列表之间的切换 ---
    def _show_lib_mode(self, run_mode: bool) -> None:
        """切到"推荐结果"或"已读文献"。两个列表叠在同一处, 一次只显示一个。

        "打开 PDF / 打开所在文件夹 / 编辑标签"这几个按钮只对本地 PDF 有意义,
        在推荐结果模式下**必须置灰** —— 那些论文根本不在本地, 按钮亮着点下去
        只会弹一句"先选一篇", 用户会以为是坏了。
        """
        self._run_mode = bool(run_mode)
        if run_mode:
            self._lib_body.pack_forget()
            self._rec_body.pack(side="top", fill="both", expand=True)
            self.lbl_lib_hint.config(
                text="这是历史推荐结果的列表; 双击某一行 = 用浏览器打开这篇论文的 "
                     "arXiv 页面。要看某一次的完整报告, 点上面的「打开这份报告」。")
        else:
            self._rec_body.pack_forget()
            self._lib_body.pack(side="top", fill="both", expand=True)
            self.lbl_lib_hint.config(
                text="双击某一行 = 用系统默认程序打开这篇 PDF; "
                     "标签可以自己编辑 (存在读取记录里, 不会因为重读文献丢失)")
        for btn in (self.btn_lib_open, self.btn_lib_dir, self.btn_lib_tags,
                    self.btn_lib_clear):
            try:
                btn.configure(state="disabled" if run_mode else "normal")
            except Exception:
                pass
        for btn in (self.btn_run_open, self.btn_run_arxiv):
            try:
                btn.configure(state="normal" if run_mode else "disabled")
            except Exception:
                pass

    def refresh_run_list(self) -> None:
        """「重新扫描」按钮: 把输出目录里的报告重新读一遍。"""
        self._collect_into_cfg()
        self._reload_runs()

    def _on_run_pick(self, _event: Any = None) -> None:
        """下拉框换了 -> 切列表。"""
        if not hasattr(self, "cmb_run"):
            return
        cur = self.var_run.get()
        run = self._run_for_label(cur)
        if run is None:
            self._show_lib_mode(False)
            self._fill_library_tree()
            return
        self._show_lib_mode(True)
        self._run_items = list(run.get("items") or [])
        self._fill_run_tree()

    def _run_for_label(self, label: str) -> Optional[Dict[str, Any]]:
        for run in self._runs:
            if run["label"] == label:
                return run
        return None

    def _current_run(self) -> Optional[Dict[str, Any]]:
        return self._run_for_label(self.var_run.get())

    def _fill_run_tree(self) -> None:
        self._lib_filter_job = None
        q = (self.var_lib_q.get() or "").strip().lower()
        tree = self.tree_rec
        tree.delete(*tree.get_children())
        shown = 0
        for i, it in enumerate(self._run_items):
            if q:
                hay = " ".join([it.get("title") or "", it.get("authors") or "",
                                it.get("journal") or "", it.get("flags") or "",
                                str(it.get("rank") or "")]).lower()
                if q not in hay:
                    continue
            row_tags = ["even" if shown % 2 == 0 else "odd"]
            tree.insert("", "end", iid=str(i), values=(
                it.get("rank") or "", it.get("title") or "",
                it.get("authors") or "—", it.get("date") or "—",
                fmt_journal_ref(it.get("journal") or ""),
                it.get("score") or "—", it.get("flags") or "—",
            ), tags=tuple(row_tags))
            shown += 1

        total = len(self._run_items)
        run = self._current_run()
        when = (run or {}).get("generated_at") or ""
        if not total:
            self.var_lib_head.set("这一份推荐结果里没有可显示的条目 "
                                  "(报告可能被改过或格式变了)")
        elif shown == total:
            self.var_lib_head.set("推荐结果 %s · 共 %d 篇" % (when or "—", total))
        else:
            self.var_lib_head.set("推荐结果 %s · 共 %d 篇, 其中 %d 篇匹配当前搜索"
                                  % (when or "—", total, shown))
        self.var_lib_path.set("")

    def _selected_run_item(self) -> Optional[Dict[str, Any]]:
        sel = self.tree_rec.selection()
        if not sel:
            return None
        try:
            return self._run_items[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _on_run_select(self, _event: Any = None) -> None:
        it = self._selected_run_item()
        if it is None:
            return
        bits = []
        if it.get("authors"):
            bits.append(str(it["authors"]))
        if it.get("date"):
            bits.append(str(it["date"]))
        if it.get("journal"):
            bits.append(str(it["journal"]))
        if it.get("citations") not in ("", "—", None):
            bits.append("引用 %s" % it["citations"])
        if it.get("url"):
            bits.append(str(it["url"]))
        self.var_lib_path.set(" · ".join(bits))

    def open_selected_run_paper(self) -> None:
        it = self._selected_run_item()
        if it is None:
            messagebox.showinfo("先选一篇", "在列表里点一下要打开的论文。")
            return
        url = it.get("url") or ""
        if not url:
            messagebox.showinfo("没有链接", "这份报告里没记下这篇论文的链接。")
            return
        self._open_path(url)

    def open_selected_run_report(self) -> None:
        run = self._current_run()
        if run is None:
            messagebox.showinfo("还没有推荐结果",
                                "输出目录里没有找到推荐报告。先到「文献推荐」页"
                                "跑一次, 或者到「设置」里确认输出目录。")
            return
        self._open_path(run["path"])

    # --- 文献列表的数据与操作 ---
    def _queue_lib_filter(self) -> None:
        if self._lib_filter_job is not None:
            try:
                self.after_cancel(self._lib_filter_job)
            except Exception:
                pass
        self._lib_filter_job = self.after(180, self._fill_current_tree)

    def _fill_current_tree(self) -> None:
        """搜索框变了 -> 重建**当前显示的那个**列表。

        两个列表共用同一个搜索框。以前这里直接写死 `_fill_library_tree`, 在
        "推荐结果"模式下敲字会去重建那个**藏起来**的已读文献列表 —— 屏幕上
        一个字都不动。
        """
        if getattr(self, "_run_mode", False):
            self._fill_run_tree()
        else:
            self._fill_library_tree()

    def refresh_library_list(self) -> None:
        """从读取记录 (索引库) 重新拉一遍列表。"""
        self._collect_into_cfg()
        try:
            rows = library_records(self.cfg)
        except Exception as exc:
            messagebox.showerror("读不到文献列表", str(exc))
            return
        self._lib_rows = rows
        # 顺手把历史推荐结果也重扫一遍: 用户很可能是刚在「文献推荐」页跑完过来
        # 看的, 那一份新的结果必须马上出现在下拉框里。扫的是输出目录里的报告
        # 文件, 几十份也就几十毫秒。
        self._reload_runs()

    def _reload_runs(self) -> None:
        """重扫输出目录, 更新下拉框, 然后按当前选择重画一次。

        保留用户当前选中的那一份 (按标签比对) —— 只是刷新一下, 不该把人家正在
        看的东西换掉。选中那一份已经不在磁盘上了才退回"已读文献"。
        """
        try:
            self._runs = past_runs.list_runs(self.cfg)
        except Exception as exc:
            log("扫历史推荐结果失败: %s" % exc, "warn")
            self._runs = []
        labels = [RUN_LIBRARY] + [r["label"] for r in self._runs]
        cur = self.var_run.get()
        self.cmb_run.configure(values=labels)
        if cur not in labels:
            cur = RUN_LIBRARY
        self.var_run.set(cur)
        self._on_run_pick()

    def _fill_library_tree(self) -> None:
        self._lib_filter_job = None
        q = (self.var_lib_q.get() or "").strip().lower()
        tree = self.tree_lib
        tree.delete(*tree.get_children())
        shown = 0
        for i, rec in enumerate(self._lib_rows):
            if q and q not in self._lib_haystack(rec):
                continue
            authors = rec["authors"]
            a_txt = "、".join(authors[:3]) + (" 等" if len(authors) > 3 else "")
            tags = list(rec["tags"]) + list(rec["auto_tags"])
            tagstr = ", ".join(tags)
            if len(tagstr) > 40:
                tagstr = tagstr[:39] + "…"
            title = rec["title"]
            if len(title) > 70:
                title = title[:69] + "…"
            row_tags = ["even" if shown % 2 == 0 else "odd"]
            if not rec["exists"]:
                row_tags.append("gone")
            tree.insert("", "end", iid=rec["path"], values=(
                title, a_txt, rec["publication"] or "—",
                tagstr or "—", rec["year"] or "—",
                rec["depth_label"] or "—", status_label(rec["status"]),
            ), tags=tuple(row_tags))
            shown += 1
        self._lib_shown = shown
        total = len(self._lib_rows)
        if not total:
            self.var_lib_head.set("还没有读取过文献 —— 先到「读取文献」页点一次开始读取")
        elif shown == total:
            self.var_lib_head.set("已读文献 %d 篇" % total)
        else:
            self.var_lib_head.set("已读文献 %d 篇, 其中 %d 篇匹配当前搜索"
                                  % (total, shown))
        self.var_lib_path.set("")

    @staticmethod
    def _lib_haystack(rec: Dict[str, Any]) -> str:
        """搜索用的匹配文本 (预先小写拼好, 每次过滤只做一次 in 判断)。"""
        return " ".join([
            rec.get("title") or "", rec.get("file") or "",
            rec.get("publication") or "", rec.get("arxiv_id") or "",
            rec.get("doi") or "", " ".join(rec.get("authors") or []),
            " ".join(rec.get("tags") or []), " ".join(rec.get("auto_tags") or []),
        ]).lower()

    def _selected_record(self) -> Optional[Dict[str, Any]]:
        sel = self.tree_lib.selection()
        if not sel:
            return None
        path = sel[0]
        for rec in self._lib_rows:
            if rec["path"] == path:
                return rec
        return None

    def _on_lib_select(self, _event: Any = None) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        bits = [rec["path"]]
        if rec["pages"]:
            bits.append("%d 页" % rec["pages"])
        if rec["arxiv_id"]:
            bits.append("arXiv:%s" % rec["arxiv_id"])
        if rec["doi"]:
            bits.append("DOI %s" % rec["doi"])
        if rec["seen_at"]:
            bits.append("读取于 %s" % rec["seen_at"])
        if not rec["exists"]:
            bits.append("文件已不在这个位置")
        self.var_lib_path.set(" · ".join(bits))

    def _on_lib_double(self, _event: Any = None) -> None:
        self.open_selected_pdf()

    def open_selected_pdf(self) -> None:
        """用系统默认程序打开选中的 PDF。

        走 ``_open_path`` —— 它就是 ``os.startfile`` (Windows) / ``open``
        (macOS) / ``xdg-open`` (Linux), 也就是"双击这个文件"的等价操作。
        """
        rec = self._selected_record()
        if rec is None:
            messagebox.showinfo("先选一篇", "在列表里点一下要打开的文献。")
            return
        self._open_path(rec["path"])

    def open_selected_dir(self) -> None:
        rec = self._selected_record()
        if rec is None:
            messagebox.showinfo("先选一篇", "在列表里点一下要打开的文献。")
            return
        folder = os.path.dirname(rec["path"])
        if not os.path.isdir(folder):
            messagebox.showinfo("找不到", "这个目录已经不存在:\n%s" % folder)
            return
        self._open_path(folder)

    def edit_selected_tags(self) -> None:
        rec = self._selected_record()
        if rec is None:
            messagebox.showinfo("先选一篇", "在列表里点一下要编辑标签的文献。")
            return
        from tkinter import simpledialog
        cur = ", ".join(rec["tags"])
        text = simpledialog.askstring(
            "编辑标签",
            "%s\n\n用逗号分隔多个标签 (中英文逗号都行), 留空 = 清空:" % rec["title"][:80],
            initialvalue=cur, parent=self)
        if text is None:            # 点了取消
            return
        self._write_tags(rec, split_tags(text))

    def clear_selected_tags(self) -> None:
        rec = self._selected_record()
        if rec is None:
            messagebox.showinfo("先选一篇", "在列表里点一下要清空标签的文献。")
            return
        if not rec["tags"]:
            self.set_status("这篇本来就没有标签")
            return
        self._write_tags(rec, [])

    def _write_tags(self, rec: Dict[str, Any], tags: List[str]) -> None:
        if not set_paper_tags(self.cfg, rec["path"], tags):
            messagebox.showerror("写不进去",
                                 "标签没保存成功, 详情见日志。\n"
                                 "检查读取记录文件是否可写。")
            return
        rec["tags"] = list(tags)
        self._fill_library_tree()
        # 重填之后选中状态会丢, 把刚才那篇再选回来 —— 连着编辑几篇标签时
        # 不用每次都重新找
        try:
            self.tree_lib.selection_set(rec["path"])
            self.tree_lib.see(rec["path"])
        except Exception:
            pass
        self.set_status("标签已保存: %s" % (", ".join(tags) or "(空)"))

    # ------------------------------------------------------------------
    # 标签 4: 设置
    # ------------------------------------------------------------------
    def _build_settings_tab(self) -> None:
        t = self.tab_cfg
        # 底部按钮条要先 pack: pack 是按调用顺序分配空间的, 若让可滚动的
        # 内容区先占了 expand, 按钮条就一点位置都分不到了。
        bar = ttk.Frame(t)
        bar.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
        ttk.Button(bar, text="保存设置", style="Accent.TButton",
                   command=self.save_config).pack(side="left")
        ttk.Button(bar, text="重新载入", command=self.reload_config
                   ).pack(side="left", padx=8)
        ttk.Button(bar, text="测试 AI 连接", command=self.test_ai).pack(side="left")
        ttk.Button(bar, text="测试 arXiv 连接", command=self.test_arxiv
                   ).pack(side="left", padx=8)
        self.var_dirty = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_dirty, foreground=theme.SCORE_MID
                  ).pack(side="right")

        # 设置项比较多, 小屏幕上装不下 —— 套一层可滚动容器, 免得下面的按钮
        # 被挤出可视区域 (那是最难自己发现的一类界面 bug)
        wrap = self._scrollable(t)

        # --- AI ---
        ai = ttk.LabelFrame(wrap, text="AI 接口")
        ai.pack(side="top", fill="x")
        g = ttk.Frame(ai)
        g.pack(side="top", fill="x", padx=8, pady=6)
        g.columnconfigure(1, weight=1)

        self.var_provider = tk.StringVar()
        self.var_base_url = tk.StringVar()
        self.var_api_key = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_temp = tk.StringVar()
        self.var_maxtok = tk.StringVar()

        self._row(g, 0, "接口类型", ttk.Combobox(
            g, textvariable=self.var_provider, width=14, state="readonly",
            values=("openai", "anthropic")))
        self._row(g, 1, "base_url", ttk.Entry(g, textvariable=self.var_base_url))
        self._row(g, 2, "api_key", ttk.Entry(g, textvariable=self.var_api_key,
                                             show="*"))
        self._row(g, 3, "模型", ttk.Entry(g, textvariable=self.var_model))
        sub = ttk.Frame(g)
        sub.grid(row=4, column=1, sticky="w", pady=2)
        ttk.Label(sub, text="temperature").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_temp, width=6).pack(side="left", padx=(4, 16))
        ttk.Label(sub, text="max_tokens").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_maxtok, width=8).pack(side="left", padx=4)
        ttk.Label(g, text="", width=12).grid(row=4, column=0, sticky="e")
        ttk.Label(ai, text="常用: DeepSeek https://api.deepseek.com/v1 · "
                           "通义 https://dashscope.aliyuncs.com/compatible-mode/v1 · "
                           "本地 Ollama http://localhost:11434/v1",
                  style="CardMuted.TLabel", wraplength=1040).pack(
            side="top", anchor="w", padx=10, pady=(0, 8))

        # --- 网络 ---
        net = ttk.LabelFrame(wrap, text="网络")
        net.pack(side="top", fill="x", pady=(8, 0))
        gn = ttk.Frame(net)
        gn.pack(side="top", fill="x", padx=8, pady=6)
        gn.columnconfigure(1, weight=1)
        self.var_proxy = tk.StringVar()
        self.var_retries = tk.StringVar()
        self.var_delay = tk.StringVar()
        self._row(gn, 0, "代理", ttk.Entry(gn, textvariable=self.var_proxy))
        sub = ttk.Frame(gn)
        sub.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(sub, text="重试次数").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_retries, width=5).pack(side="left", padx=(4, 16))
        ttk.Label(sub, text="请求间隔(秒)").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_delay, width=5).pack(side="left", padx=4)
        ttk.Label(gn, text="", width=12).grid(row=1, column=0, sticky="e")
        ttk.Label(net, text="留空 = 直连; 本机代理填 http://127.0.0.1:7890。"
                            "arXiv 限流较严时把请求间隔调到 8-10 秒。",
                  style="CardMuted.TLabel").pack(side="top", anchor="w",
                                                 padx=10, pady=(0, 8))

        # 文献路径不在这里 —— 它只在"读取文献"页有一份。这一页原来也摆了一份
        # 一模一样的编辑器, 两份视图编的是同一个列表, 靠 refresh() 同步: 看着
        # 是"方便", 实际是让用户拿不准哪一份算数, 改完这边还得记得去那边看看。
        # 增删文件夹本来就是"读取文献"那一步的事, 跟这一页的全局设置不是一类。
        #
        # 读取深度同理, 它也有两个"用它的地方" (读取页 / 推荐页), 两边各摆一份
        # 控件就够了。设置页再放一份只会让人以为它是全局开关, 而实际上它属于
        # "这次怎么读 / 这次怎么推"。

        # --- 记录与输出 ---
        # 三个路径都放在一起: 它们回答的是同一个问题 —— "程序把信息存哪了"。
        # 用户会想挪走它们, 通常是为了换个盘、或者放进同步目录。
        ix = ttk.LabelFrame(wrap, text="记录与输出 (三个位置都可以改; 相对路径按程序所在目录解析)")
        ix.pack(side="top", fill="x", pady=(8, 0))
        gi = ttk.Frame(ix)
        gi.pack(side="top", fill="x", padx=8, pady=6)
        gi.columnconfigure(1, weight=1)
        self.var_index_db = tk.StringVar()
        self.var_history_db = tk.StringVar()
        self.var_out_dir = tk.StringVar()
        self.var_pdf_conc = tk.StringVar()
        self.var_excerpt = tk.StringVar()
        self.var_maxlib = tk.StringVar()
        self._row(gi, 0, "读取记录", self._path_row(
            gi, self.var_index_db,
            self._browse_save_into(self.var_index_db, "选择读取记录 (索引) 文件")))
        self._row(gi, 1, "推荐记录", self._path_row(
            gi, self.var_history_db,
            self._browse_save_into(self.var_history_db, "选择推荐记录文件")))
        self._row(gi, 2, "报告目录", self._path_row(
            gi, self.var_out_dir,
            self._browse_dir_into(self.var_out_dir)))

        sub = ttk.Frame(gi)
        sub.grid(row=3, column=1, sticky="w", pady=(6, 2))
        ttk.Label(sub, text="解析线程数").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_pdf_conc, width=4).pack(
            side="left", padx=(4, 16))
        ttk.Label(sub, text="每篇正文截取(字符)").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_excerpt, width=7).pack(
            side="left", padx=(4, 16))
        ttk.Label(sub, text="参与分析的文献上限").pack(side="left")
        ttk.Entry(sub, textvariable=self.var_maxlib, width=6).pack(side="left", padx=4)
        ttk.Label(gi, text="", width=12).grid(row=3, column=0, sticky="e")

        flags = ttk.Frame(gi)
        flags.grid(row=4, column=1, sticky="w", pady=(4, 2))
        self.var_use_history = tk.BooleanVar(value=True)
        ttk.Checkbutton(flags, text="使用推荐记录", variable=self.var_use_history
                        ).pack(side="left")
        # 推荐页上那个"跳过已推荐过的"复选框绑的就是这个变量 (推荐页先建,
        # 变量也在那里建) —— 两处共用一份, 不存在两处设置打架的可能
        ttk.Checkbutton(flags, text="推荐时跳过以前推荐过的",
                        variable=self.var_skip_seen).pack(side="left", padx=12)
        ttk.Label(gi, text="", width=12).grid(row=4, column=0, sticky="e")

        openers = ttk.Frame(gi)
        openers.grid(row=5, column=1, sticky="w", pady=(2, 2))
        # 这两个按钮在卡片 (LabelFrame) 里, 用 Card.TButton —— Quiet.TButton 是
        # 给页面灰底准备的, 摆到白卡片上就是一块灰方块
        ttk.Button(openers, text="打开报告目录", style="Card.TButton",
                   command=lambda: self._open_dir(resolve_path(
                       self.var_out_dir.get() or "output"))).pack(side="left")
        ttk.Button(openers, text="打开记录所在目录", style="Card.TButton",
                   command=lambda: self._open_dir(os.path.dirname(resolve_path(
                       self.var_history_db.get() or
                       "recommend_history.sqlite")))).pack(side="left", padx=8)
        ttk.Label(gi, text="", width=12).grid(row=5, column=0, sticky="e")

        ttk.Label(ix, text="读取记录 = 哪些 PDF 已经读过 (不重复读、不重复花 token); "
                           "推荐记录 = 推荐过哪些论文、以及那篇的解读 (同一篇不重复解读); "
                           "报告目录 = 生成的 markdown 放在哪。\n"
                           "改完正文截取长度后需要点\"全部重读\"才生效; 换读取记录文件等于"
                           "换了账本, 新文件里的文献会当作从没读过。",
                  style="CardMuted.TLabel", wraplength=1040, justify="left").pack(
            side="top", anchor="w", padx=10, pady=(0, 8))

    # ------------------------------------------------------------------
    # 标签 5: 支持一下
    # ------------------------------------------------------------------
    # 赞赏码图片。界面里用 PNG —— Tk 自带的 PhotoImage 只认 GIF/PNG, 不认 JPG,
    # 而打包时 PIL 是被明确排除掉的 (见 build_exe.py 的 EXCLUDES), 所以不能在
    # 运行时依赖 Pillow 去读 JPG。仓库里那份 reward.jpg 是原图, 留给 README 用。
    SUPPORT_IMAGE_NAMES = ("reward.png", "reward.jpg")

    def _support_image_path(self) -> str:
        """找赞赏码图片: 先找程序目录旁边的, 再找打包进 exe 里的那一份。

        程序目录那份是打包时抄在 exe 旁边的 (见 build_exe.py), 用户看得见、也
        换得掉 (想换一张赞赏码就替换它), 所以它优先。exe 里的那份是
        ``--add-data`` 带进来的: onefile 模式下 PyInstaller 把它解到
        ``sys._MEIPASS`` (一个临时目录, 进程一退就没了), onedir 模式下那是 exe
        旁边的 ``_internal``。留着它当兜底 —— 光一个 exe 拷到别的机器上也能
        显示出赞赏码。
        """
        roots = [project_root()]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass and meipass not in roots:
            roots.append(meipass)
        for root in roots:
            for name in self.SUPPORT_IMAGE_NAMES:
                path = os.path.join(root, name)
                if os.path.exists(path):
                    return path
        return ""

    def _load_support_photo(self, path: str) -> Any:
        """把赞赏码读成 Tk 能显示的图片对象; 读不了返回 None (界面另有兜底)。"""
        if not path:
            return None
        try:
            # PNG 交给 Tk 自己读 (Tk 8.6 起原生支持), 不依赖任何第三方库
            return tk.PhotoImage(file=path)
        except Exception:
            pass
        try:
            # 万一只有 JPG (源码运行、且装了 Pillow): 缩到和 PNG 差不多大再显示
            from PIL import Image, ImageTk      # type: ignore
            img = Image.open(path)
            img.thumbnail((520, 520))
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _build_support_tab(self) -> None:
        page = self._scrollable(self.tab_support)
        ttk.Label(page, text="支持一下", style="H2.TLabel").pack(
            side="top", anchor="w", padx=10, pady=(8, 4))
        ttk.Label(
            page,
            text="如果它确实帮到了你, 欢迎扫下面的码请我喝杯咖啡。不打赏也完全"
                 "没关系 —— 功能一样用, 有问题一样可以提。",
            justify="left", wraplength=1000).pack(
            side="top", anchor="w", padx=10, pady=(0, 6))
        ttk.Label(
            page,
            text="If you find this project helpful, welcome to sponsor me via "
                 "WeChat.",
            justify="left", wraplength=1000).pack(
            side="top", anchor="w", padx=10, pady=(0, 8))

        card = ttk.LabelFrame(page, text="微信赞赏码")
        card.pack(side="top", fill="x", padx=8, pady=(0, 8))
        path = self._support_image_path()
        photo = self._load_support_photo(path)
        # 图片对象必须挂一个引用在 self 上 —— 局部变量一被回收, 界面上就是一块
        # 空白 (tkinter 的经典坑, 而且不报任何错)
        self._support_photo = photo
        if photo is not None:
            # 经典 tk.Label 不进 theme 的样式表, 底色得自己给, 否则白卡片上会
            # 出现一块系统灰
            tk.Label(card, image=photo, borderwidth=0,
                     background=theme.SURFACE).pack(side="top", pady=(10, 6))
            ttk.Label(card, text="用微信「扫一扫」即可 (WeChat → Scan)",
                      style="CardMuted.TLabel", wraplength=1000,
                      justify="left").pack(side="top", anchor="w", padx=10,
                                          pady=(0, 10))
        else:
            # 图片没读出来时总得说一句, 否则白卡片上就是一块空白 —— 但不说
            # 图片路径、也不给"打开图片"的按钮: 那一行字只在这时候出现
            if path:
                why = ("这个格式 Tk 显示不了 (放一份同名的 reward.png 到程序目录下"
                       "就会显示在这里)。")
            else:
                why = ("没找到图片: 把 reward.png (或 reward.jpg) 放到程序目录下, "
                       "重启就会显示在这里。")
            ttk.Label(card, text="赞赏码没显示出来。" + why, style="CardMuted.TLabel",
                      wraplength=1000, justify="left").pack(
                side="top", anchor="w", padx=10, pady=(8, 10))

    # ------------------------------------------------------------------
    # 读取深度选择器 (读取文献页和文献推荐页共用同一个变量)
    # ------------------------------------------------------------------
    DEPTH_HINTS = {
        "metadata": "最快 · 只读首页的标题和摘要, 送进 AI 的内容最少",
        "sections": "推荐 · 标题和摘要之外, 再抽正文里的引言和结论",
        "fulltext": "最慢 · 在上一档基础上再加全文节选, 上下文最全",
    }

    def _build_depth_selector(self, parent: tk.Misc, pady: Any = (8, 0),
                              padx: int = 0, compact: bool = False) -> None:
        """三档读取深度。

        ``compact=True`` 只出一行 (标题 + 三个单选框), 用在空间紧张的推荐页 ——
        那里下面还压着结果表和详解框, 摆不下每个档位一行的说明。两处 (读取 /
        推荐) 绑的都是同一个 ``var_depth``, 改哪边另一边立刻跟着变。
        """
        if compact:
            box = ttk.Frame(parent)
            box.pack(side="top", fill="x", pady=pady, padx=padx)
            ttk.Label(box, text="读取深度").pack(side="left")
            for depth in READ_DEPTHS:
                ttk.Radiobutton(box, text=DEPTH_LABELS[depth], value=depth,
                                variable=self.var_depth,
                                command=self._on_depth_change
                                ).pack(side="left", padx=(10, 0))
            # 紧凑版只用在推荐页的"参数"卡片里 —— 底色得跟卡片 (SURFACE) 走,
            # 用 Muted.TLabel 会在白卡片上留一块页面灰。这是 check_backgrounds
            # 抓出来的
            lbl = ttk.Label(box, text="", style="CardMuted.TLabel")
            lbl.pack(side="left", padx=(16, 0))
            self._depth_labels.append(lbl)
            self._refresh_depth_label()
            return

        box = ttk.LabelFrame(parent, text="读取深度")
        box.pack(side="top", fill="x", pady=pady, padx=padx)
        for depth in READ_DEPTHS:
            row = ttk.Frame(box)
            row.pack(side="top", fill="x", padx=10, pady=(6, 0))
            ttk.Radiobutton(row, text=DEPTH_LABELS[depth], value=depth,
                            variable=self.var_depth,
                            command=self._on_depth_change).pack(side="left")
            ttk.Label(row, text=self.DEPTH_HINTS[depth],
                      style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Label(box, text="读取深度决定的是送进分析的内容有多少, 标题和摘要"
                            "始终都会读。往深里调, 下次读取会自动补读缺的那部分; "
                            "往浅里调直接用已有结果, 不会重读文件。",
                  style="CardMuted.TLabel", wraplength=1040, justify="left"
                  ).pack(side="top", anchor="w", padx=10, pady=(8, 0))
        lbl = ttk.Label(box, text="", style="CardH2.TLabel")
        lbl.pack(side="top", anchor="w", padx=10, pady=(6, 8))
        # 两处各有一个这样的标签, 全都要跟着变量更新 —— 只留最后一个的话,
        # 另一处会一直显示旧档位, 看起来像设置没生效。
        self._depth_labels.append(lbl)
        self._refresh_depth_label()

    def _on_depth_change(self) -> None:
        self.mark_dirty()
        self._refresh_depth_label()
        # 读取页顶上那行"文献来源: … · 读取深度: X"也要跟着改, 它读的是这个
        # 单选框的当前值 (见 _refresh_source_label)。
        if hasattr(self, "var_source"):
            self._refresh_source_label()

    def _refresh_depth_label(self) -> None:
        """显示当前档位。纯字符串拼接 —— 这里**不**去查索引。

        ``index_stats`` 要遍历磁盘目录, 点一下单选框就同步跑一遍, 大文献库上
        会让界面卡住。所以这里只说"选了什么"; 实际"还差多少没读"留给用户主动
        点的"查看已读记录"。
        """
        text = ("当前档位: %s —— 下次读取按这个档位走。"
                % DEPTH_LABELS[normalize_depth(self.var_depth.get())])
        for lbl in getattr(self, "_depth_labels", []):
            try:
                lbl.config(text=text)
            except Exception:
                pass

    def _scrollable(self, parent: tk.Misc) -> ttk.Frame:
        """把内容放进一个可滚动的容器, 返回真正该往里塞控件的 Frame。"""
        outer = ttk.Frame(parent)
        outer.pack(side="top", fill="both", expand=True)
        # 经典 Canvas 不吃 ttk 样式, 底色得自己给 —— 漏了就会在设置页右侧露出
        # 一块默认灰白
        canvas = tk.Canvas(outer, highlightthickness=0, background=theme.BG)
        vs = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        inner = ttk.Frame(canvas, padding=8)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _fit() -> bool:
            """内容比视口矮时, 把内层 Frame 顶到视口高度。返回"改了没有"。

            ``create_window`` 不写 height 时, 窗口项的高度恒等于内层的**请求**
            高度 —— 内层永远只有内容那么高, 里面那些 ``pack(expand=True)`` 的
            控件 (文件夹表、结果表) 一点多余高度都分不到。表现出来就是: 页面
            下方白白空着一大块, 而那块空白既不属于表格也不属于详解。实测
            900x700 的窗口: 视口 700、内层请求高度 552, 表格被钉死在 413,
            下面还空着 148。

            内容比视口**高**时取请求高度, 该滚动照旧滚动。

            值没变就别再 ``itemconfigure``: 那会再触发一轮 <Configure>, 白抖
            一下 (实测 itemcget 拿得到设进去的值, 所以这个防抖是有效的)。

            返回值是给 _settle_detail 用的: **"改过"就要再来一拍**。这里读的是
            内层的 ``winfo_reqheight()``, 而它是**滞后一拍**的 —— 里面某个控件刚
            configure 完, 请求高度要等这一轮几何跑完才更新。所以紧接着问一次
            常常得到"没变", 真正该改的那一拍在下一拍。不把这件事告诉调用方,
            调用方就会在这一拍收工, 于是请求高度**永远**落不到窗口项上 (实测:
            详解 Text 请求 986 像素, 实际只有 282 —— 内层 Frame 请求 2145、实际
            1441, 差的那 704 像素整块露不出来, 而且再也没人来补)。
            """
            h = max(canvas.winfo_height(), inner.winfo_reqheight())
            if str(h) != str(canvas.itemcget(window, "height")):
                canvas.itemconfigure(window, height=h)
                return True
            return False

        def _region() -> tuple:
            """当前 scrollregion, 归一成整数四元组 (空/读不出来给 ())。"""
            try:
                raw = str(canvas.cget("scrollregion")).replace(",", " ").split()
                return tuple(int(float(x)) for x in raw)
            except Exception:
                return ()

        def _on_inner(_e: Any = None) -> bool:
            """内层尺寸变了: 顶高度 + 重算滚动范围。返回"改过没有"。

            **没变就别碰**: 这里每一句 configure 都会让 Tk 把"重新算几何"那个
            空闲任务作废重排。_settle_detail 是连着几拍跑的, 每拍都无脑
            ``canvas.configure(scrollregion=...)`` 的话, 几何任务永远排不上去,
            于是内层的**请求**高度永远不更新 —— 而 _fit 读的正是它, 于是 _fit
            永远算"没变化"。实测这一条能让定高彻底卡死: 请求 986 像素的详解框
            停在 282, 48 拍里 refit 一次都没生效, 而那串回调一停 (几何终于跑
            上了) 再手动调一次就立刻好了。
            """
            changed = _fit()
            box = canvas.bbox("all")
            if box is not None and tuple(box) != _region():
                canvas.configure(scrollregion=box)
            return changed

        def _on_canvas(e: Any) -> None:
            canvas.itemconfigure(window, width=e.width)
            _fit()

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)

        def _wheel(e: Any) -> None:
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def _bind_wheel() -> None:
            canvas.bind_all("<MouseWheel>", _wheel)

        def _release_wheel() -> None:
            canvas.unbind_all("<MouseWheel>")

        # 鼠标滚轮只在指针位于这个容器上时才接管, 否则会把别的页面也滚了。
        # inner 也要绑: 内层 Frame 现在会被顶到视口高度 (见 _fit), canvas 自己
        # 一点露出来的地方都不剩 —— 只绑 canvas 的话指针一进页面就收不到 <Enter>,
        # 整页的滚轮会直接失效。
        for _w in (canvas, inner):
            _w.bind("<Enter>", lambda e: _bind_wheel())
            _w.bind("<Leave>", lambda e: _release_wheel())
        # 容器里凡是**自带滚动条**的控件 (结果表、日志框、文件夹表) 都要把滚轮
        # 抢回去, 否则滚轮永远只滚整页 —— 表格里第 10 行以下的论文就翻不到了。
        # 把这两个动作挂在返回的 Frame 上, 由 _wheel_passthrough 按控件逐个接。
        inner.bind_page_wheel = _bind_wheel
        inner.release_page_wheel = _release_wheel
        # 页面里某一块自己长高了 (最典型的就是"详解"), 而内层的实际尺寸是被
        # canvas 的窗口项钉住的 —— 请求高度变了、实际尺寸没变, <Configure> 不
        # 一定发得出来。所以留个手动入口给那种情况用。
        inner.refit_page = _on_inner
        # 往上找"这一页最外侧的滚动容器"的路标 (见 _bind_ctrl_wheel): Ctrl+滚轮
        # 时从指针底下那个控件顺着 master 一路上去, 第一个带这个属性的就是本页。
        # canvas 也挂一份**指着自己**: 内层 Frame 虽然被顶到视口高度、把 canvas
        # 整个盖住, 但窗口正在缩放的那几拍里指针还是可能落在 canvas 自己身上,
        # 而那时候从它往上走是找不到 inner 的。
        canvas.page_canvas = canvas
        inner.page_canvas = canvas
        return inner

    def _wheel_passthrough(self, page: tk.Misc, *widgets: tk.Widget) -> None:
        """让 ``widgets`` 内部的滚动条抢回滚轮 (见 _scrollable 里的说明)。"""
        for w in widgets:
            w.bind("<Enter>", lambda e, p=page: p.release_page_wheel(), add="+")
            w.bind("<Leave>", lambda e, p=page: p.bind_page_wheel(), add="+")

    # ------------------------------------------------------------------
    # Ctrl + 滚轮 = 滚整页
    # ------------------------------------------------------------------
    def _bind_ctrl_wheel(self) -> None:
        """让 Ctrl + 滚轮滚**整页**那个滚动条, 而不是指针底下那个小框。

        为什么按控件类挂, 而不是像普通滚轮那样挂到 "all" 上?

        实测 (Tk 8.6.9 / win32): 按住 Ctrl 时 ``<MouseWheel>`` **照样匹配** ——
        Tk 允许事件带上模式里没写的修饰键。所以指针停在结果表上按 Ctrl 滚, 表格
        自己的类绑定先跑、先把表格滚掉一格, 才轮到 "all" 上那个整页绑定, 两边
        一起动。而 ``<Control-MouseWheel>`` 和 ``<MouseWheel>`` 挂在**同一个** tag
        上时, Tk 只挑更具体的那个跑 (修饰键多的胜), 它再 ``break`` 一下, 同一个
        tag 里后面的绑定就整条不跑了 —— 于是表格纹丝不动, 滚的只有整页。

        Text 和 Treeview 是本程序里**仅有的**两种自带滚动条的控件 (结果表、文件夹
        表、运行日志、对话区、详解框); 指针在别处 (卡片空白、按钮、输入框) 本来
        也没有第二个滚动条, "all" 上那个整页绑定照常生效, 带不带 Ctrl 都一样。
        uitest 里有一条断言专门盯着这句话 —— 将来谁往页面里塞一个自带滚动条的
        新控件类, 那条会先挂, 而不是让 Ctrl+滚轮在它上面悄悄失灵。

        不写 ``add="+"``: 这两个类在 Tk 里本来就没有 ``<Control-MouseWheel>`` 绑定
        (实测是空串), 直接覆盖既不丢东西, 重复调用也只会覆盖一次 —— 而 ``add``
        会**追加**, 挂两回就一次滚两格。
        """
        for cls in ("Text", "Treeview"):
            self.bind_class(cls, "<Control-MouseWheel>", self._ctrl_wheel)

    def _ctrl_wheel(self, event: Any) -> Optional[str]:
        """Ctrl + 滚轮: 滚当前这一页最外侧那个滚动条 (见 _bind_ctrl_wheel)。

        找不到整页容器就返回 ``None`` (**不** break), 让控件按老规矩滚自己 ——
        「文献列表」页就是这种情况: 那一页没有整页滚动容器, 最外侧的滚动条**就是**
        那张表自己的, 它自己滚正是对的。
        """
        canvas = self._outermost_canvas(event.widget)
        if canvas is None:
            return None
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _outermost_canvas(self, widget: tk.Misc) -> Optional[tk.Canvas]:
        """顺着 ``widget`` 的 master 往上找本页最外侧那个滚动容器 (见 _scrollable)。

        用事件自带的 ``event.widget``, 而不是"记住指针进过哪一页": 滚轮事件本来就
        是发给**指针底下**那个控件的 (整页滚轮那套绑定靠的也是这一条), 所以往上走
        遇到的第一个带 ``page_canvas`` 的祖先, 正好是用户正看着的那一页 —— 切标签
        页、切子页面都不用另外记账, 也不会有"记下的那一页已经不在屏幕上了"这种事。
        """
        w: Optional[tk.Misc] = widget
        while w is not None:
            canvas = getattr(w, "page_canvas", None)
            if canvas is not None:
                return canvas
            w = getattr(w, "master", None)
        return None

    def _row(self, parent: ttk.Frame, r: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label, width=12, anchor="e").grid(
            row=r, column=0, sticky="e", padx=(0, 6), pady=2)
        widget.grid(row=r, column=1, sticky="ew", pady=2)

    def _path_row(self, parent: ttk.Frame, var: tk.StringVar,
                  command: Callable[[], None]) -> ttk.Frame:
        f = ttk.Frame(parent)
        e = ttk.Entry(f, textvariable=var)
        e.pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="浏览…", width=8, command=command).pack(side="left", padx=4)
        return f

    def _make_log_pane(self, parent: tk.Misc, title: str,
                       height: int = 9) -> tk.Text:
        box = ttk.LabelFrame(parent, text=title)
        # **不 expand**: 日志是一块定高的控制台, 它自己带滚动条, 多给它的高度
        # 只会把"该长的地方"挤小。页面里多出来的高度应该全归内容 —— 读取页归
        # 文件夹表, 推荐页归结果表和它下面整段铺开的详解。
        box.pack(side="top", fill="both", padx=8, pady=(4, 8))
        # wrap="word" 且**没有横向滚动条**: 日志是一行一句的流水, 唯一会撑出横向
        # 滚动条的东西是那种又长又没空格的串 (PDF 路径、URL)。横向滚动的意思是
        # "你得左右拖着才能把这句话读完", 而日志是拿来看的, 不是拿来对齐的 ——
        # 折行显示反而一眼能扫完。纵向滚动条保留 (日志会一直往下长)。
        txt = tk.Text(box, height=height, wrap="word", state="disabled",
                      background=theme.SURFACE, font="TkFixedFont",
                      relief="flat", highlightthickness=0, padx=8, pady=6,
                      spacing1=1, spacing3=1)
        ys = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=ys.set)
        txt.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        for lvl, color in LEVEL_COLORS.items():
            txt.tag_configure(lvl, foreground=color)
        return txt

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------
    def _ensure_config_file(self) -> None:
        """第一次运行时把默认配置写到程序目录下, 让三个位置有个明面上的落点。"""
        if os.path.exists(self.config_path):
            return
        self.save_config(quiet=True)
        if not os.path.exists(self.config_path):
            return      # 写不进去 (只读目录): 不影响使用, 默认值照样生效
        self._append_log("第一次运行: 已生成配置文件 %s" % self.config_path, "ok")
        self.set_status("第一次运行 · 配置文件已生成在程序目录下 (%s)"
                        % os.path.basename(self.config_path))

    def _check_data_paths(self) -> None:
        """启动时核对"读取记录 / 推荐记录 / 报告目录"这三个位置还在不在。

        每次启动都查一遍: 这三个位置是可以改的, 改了之后把文件夹挪走、把 U 盘
        拔掉、或者手改 config.json 写错一个字母, 程序都不会报错 —— 它只会静默
        地写到**另一个**地方去, 用户看到的是"设置里明明写了, 怎么不生效"。
        这种情况必须主动说一声。
        """
        # 这个回调是 after() 排的队, 用户完全可能在这 0.4 秒里把窗口关了 ——
        # 那时候再去弹对话框就是对着一个已经销毁的根窗口操作, TclError 直接
        # 冒到 Tk 的事件循环里 (窗口程序没有控制台, 什么线索都看不到)。
        try:
            if not self.winfo_exists():
                return
            from .config import check_data_paths
            bad = check_data_paths(self.cfg)
        except Exception:
            return
        if not bad:
            return
        lines = ["下面这几个位置找不到了 —— 程序会把东西写到别处去, "
                 "或者根本写不进去:", ""]
        for name, raw, path, why in bad:
            lines.append("%s: %s" % (name, raw or "(空)"))
            lines.append("    解析到: %s" % path)
            lines.append("    原因: %s" % why)
        lines.append("")
        lines.append("去「设置」页 →「记录与输出」里改成现在还在的目录, 然后保存。")
        self._append_log("启动检查: %d 个位置不可用" % len(bad), "warn")
        for _n, _r, _p, _w in bad:
            self._append_log("  %s -> %s (%s)" % (_n, _p, _w), "warn")
        if messagebox.askyesno("有几个位置找不到了", "\n".join(lines)
                              + "\n\n现在切到「设置」页去改吗?"):
            self.nb.select(self.tab_cfg)

    def reload_config(self) -> None:
        self.cfg = load_config(self.config_path)
        self.config_path = self.cfg.get("_config_path") or os.path.join(
            project_root(), "config.json")
        ai = self.cfg.get("ai", {})
        net = self.cfg.get("network", {})
        arx = self.cfg.get("arxiv", {})

        self.var_provider.set(ai.get("provider", "openai"))
        self.var_base_url.set(ai.get("base_url", ""))
        self.var_api_key.set(ai.get("api_key", ""))
        self.var_model.set(ai.get("model", ""))
        self.var_temp.set(str(ai.get("temperature", 0.3)))
        self.var_maxtok.set(str(ai.get("max_tokens", 4096)))
        self.var_proxy.set(net.get("proxy") or "")
        self.var_retries.set(str(net.get("retries", 5)))
        self.var_delay.set(str(arx.get("request_delay", 3.0)))
        self.var_top.set(str(self.cfg.get("analysis", {}).get("top_n", 20)))
        self.var_conc.set(str(self.cfg.get("analysis", {}).get("concurrency", 3)))
        lib = self.cfg.get("library", {})
        ana = self.cfg.get("analysis", {})
        self.var_index_db.set(lib.get("index_db", "library_index.sqlite"))
        self.var_history_db.set(ana.get("history_db", "recommend_history.sqlite"))
        self.var_out_dir.set(self.cfg.get("output", {}).get("dir", "output"))
        self.var_use_history.set(bool(ana.get("use_history", True)))
        self.var_skip_seen.set(bool(ana.get("skip_recommended", False)))
        self.var_pdf_conc.set(str(lib.get("concurrency", 4)))
        self.var_excerpt.set(str(lib.get("fulltext_excerpt_chars", 3000)))
        self.var_maxlib.set(str(lib.get("max_papers_for_profile", 200)))
        self.var_depth.set(normalize_depth(lib.get("read_depth")))
        self._refresh_depth_label()
        self._set_categories(arx.get("categories") or [])
        self.var_subscribe.set(bool(arx.get("subscribe_only", False)))
        self._refresh_cat_label()

        self.lbl_config.config(text="配置文件: %s" % self.config_path)
        self.dirty = False
        self.var_dirty.set("")
        self.refresh_all_folder_editors()
        self._refresh_source_label()

    def _refresh_source_label(self) -> None:
        folders = [f for f in (self.cfg.get("pdf_folders") or [])
                   if f.get("enabled", True) and f.get("path")]
        # 深度读**界面上那个单选框**, 不是 cfg 里存着的值。这一行就在文件夹列表
        # 上方, 是这一页最显眼的一句; 而深度改了要等保存或开跑才会写回 cfg ——
        # 中间这段时间里, 上面写着"读取深度: 引言+结论"、下面单选框选着"全文",
        # 同一个页面上两个说法打架, 用户会以为没改上。文件夹数不会这样: 它读的
        # 就是那个活列表, 增删立刻反映。
        var = getattr(self, "var_depth", None)
        if var is not None:
            depth = normalize_depth(var.get())
        else:
            depth = normalize_depth(self.cfg.get("library", {}).get("read_depth"))
        if folders:
            text = "文献来源: %d 个启用的 PDF 文件夹" % len(folders)
        else:
            # "在下面"不是"在上面": 文件夹编辑器紧挨着这一行**下方**。以前两页
            # 都有编辑器时这句勉强算含糊, 现在只有一份了, 指错方向就是死路。
            text = "文献来源: 还没有添加 PDF 文件夹 —— 请在下面添加一个"
        self.var_source.set(text + "     ·     读取深度: %s"
                            % DEPTH_LABELS[depth])

    def mark_dirty(self) -> None:
        self.dirty = True
        self.var_dirty.set("有未保存的修改")

    def _collect_into_cfg(self) -> None:
        """把界面上的值写回 ``self.cfg``。"""
        cfg = self.cfg
        ai = cfg.setdefault("ai", {})
        ai["provider"] = self.var_provider.get().strip() or "openai"
        ai["base_url"] = self.var_base_url.get().strip()
        ai["api_key"] = self.var_api_key.get().strip()
        ai["model"] = self.var_model.get().strip()
        try:
            ai["temperature"] = float(self.var_temp.get())
        except ValueError:
            pass
        try:
            ai["max_tokens"] = int(float(self.var_maxtok.get()))
        except ValueError:
            pass

        net = cfg.setdefault("network", {})
        proxy = self.var_proxy.get().strip()
        net["proxy"] = proxy or None
        try:
            net["retries"] = max(0, int(float(self.var_retries.get())))
        except ValueError:
            pass
        arx = cfg.setdefault("arxiv", {})
        try:
            arx["request_delay"] = max(0.0, float(self.var_delay.get()))
        except ValueError:
            pass
        # 勾选的分类写回配置: 它不只给推荐用, RSS 公告通道也读这一项。存空列表
        # (而不是 None) 表示"明确不限", 跟没配置过区分开。
        arx["categories"] = self._selected_categories()
        arx["subscribe_only"] = bool(self.var_subscribe.get())

        ana = cfg.setdefault("analysis", {})
        try:
            ana["top_n"] = max(1, int(float(self.var_top.get())))
        except ValueError:
            pass
        try:
            ana["concurrency"] = max(1, int(float(self.var_conc.get())))
        except ValueError:
            pass
        ana["history_db"] = (self.var_history_db.get().strip()
                             or "recommend_history.sqlite")
        ana["use_history"] = bool(self.var_use_history.get())
        ana["skip_recommended"] = bool(self.var_skip_seen.get())

        cfg.setdefault("output", {})["dir"] = (
            self.var_out_dir.get().strip() or "output")

        lib = cfg.setdefault("library", {})
        lib["index_db"] = self.var_index_db.get().strip() or "library_index.sqlite"
        lib["read_depth"] = normalize_depth(self.var_depth.get())
        try:
            lib["concurrency"] = max(1, int(float(self.var_pdf_conc.get())))
        except ValueError:
            pass
        try:
            lib["fulltext_excerpt_chars"] = max(200, int(float(self.var_excerpt.get())))
        except ValueError:
            pass
        try:
            lib["max_papers_for_profile"] = max(0, int(float(self.var_maxlib.get())))
        except ValueError:
            pass

    def save_config(self, quiet: bool = False) -> None:
        """把界面上的设置写进 config.json。

        ``quiet`` 用于"第一次运行时自动生成配置文件": 那是程序自己做的事,
        状态栏不该报成"设置已保存" (看起来像用户刚点过保存)。
        """
        self._collect_into_cfg()
        # 代理改了就重新探测: 缓存里存的是上一次那个代理的结论, 用户刚把坏代理
        # 换成好的 (或者反过来), 不重新探就会一直按老结论走。
        reset_proxy_probe_cache()
        # 保留 config.json 里我们不认识/不管理的键 (比如用户自己写的注释键)
        raw: Dict[str, Any] = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for key in ("pdf_folders", "library", "ai", "arxiv", "ranking",
                    "analysis", "network", "output"):
            if key in self.cfg:
                raw[key] = self.cfg[key]
        # 这两个是运行时算出来的内部字段, 不该落盘 (尤其 _resolved_key 是明文
        # API key 的副本)
        raw.pop("_config_path", None)
        raw.get("ai", {}).pop("_resolved_key", None)
        # ui 段是上一版"详解面板拖多高"留下的 (那时详解有自己的高度, 靠分隔条拖)。
        # 现在详解按内容撑开, 这一项没有意义了 —— 顺手删掉, 免得留着让人以为
        # 还能手改一个"详解高度"。
        raw.pop("ui", None)
        try:
            with open(self.config_path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            messagebox.showerror("保存失败", "写不了 %s:\n%s" % (self.config_path, exc))
            return
        self.dirty = False
        self.var_dirty.set("")
        self._refresh_source_label()
        if not quiet:
            self.set_status("设置已保存到 %s" % self.config_path)

    def _browse_dir_into(self, var: tk.StringVar) -> Callable[[], None]:
        def _go() -> None:
            path = filedialog.askdirectory(title="选择目录")
            if path:
                var.set(os.path.normpath(path))
                self.mark_dirty()
        return _go

    def _browse_save_into(self, var: tk.StringVar, title: str
                          ) -> Callable[[], None]:
        """给"记录文件"用的浏览按钮 —— 用另存为对话框, 因为那是**文件**不是目录。

        用 askdirectory 去选一个 .sqlite 文件是做不到的, 用户只能手打路径;
        用 askopenfilename 又选不出"还不存在的文件"。所以用 asksaveasfilename:
        既能选已有的, 也能在新目录里起个新名字。
        """
        def _go() -> None:
            path = filedialog.asksaveasfilename(
                title=title, defaultextension=".sqlite",
                filetypes=[("SQLite 数据库", "*.sqlite"), ("所有文件", "*.*")])
            if path:
                var.set(os.path.normpath(path))
                self.mark_dirty()
        return _go

    def refresh_all_folder_editors(self) -> None:
        """重画文件夹列表。

        现在只有"读取文献"页一个编辑器了, 名字没改 —— 四个 FolderEditor 回调
        和 reload_config / 切页都按这个名字调, 为了少改几处而保留复数。真要说
        它现在管的是什么: 把 ``pdf_folders`` 的当前内容重新显示一遍, 顺手刷新
        那行"共几个文件夹"的来源说明。
        """
        ed = getattr(self, "folder_editor_read", None)
        if ed is not None:
            ed.refresh()
        if hasattr(self, "var_source"):
            self._refresh_source_label()

    def _on_tab_changed(self) -> None:
        self.refresh_all_folder_editors()
        # 切到"文献列表"时重新拉一遍: 用户很可能是刚在读取页点完"开始读取"过来
        # 看的。只读数据库, 几百篇是毫秒级, 不用做缓存失效判断。
        try:
            if self.nb.index(self.nb.select()) == self.nb.index(self.tab_list):
                self.refresh_library_list()
        except Exception:
            pass

    def _open_config_file(self) -> None:
        self._open_path(self.config_path)

    def _open_dir(self, path: str) -> None:
        if not path:
            return
        if not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception:
                pass
        self._open_path(path)

    def _open_path(self, path: str) -> None:
        if not path:
            return
        # http(s) 直接交给系统默认浏览器, 不做存在性检查 (网址不是本地路径,
        # os.path.exists 必然为假, 那样双击论文只会弹一句"路径不存在")
        if re.match(r"^https?://", path, re.I):
            try:
                if sys.platform.startswith("win"):
                    os.startfile(path)      # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    import subprocess
                    subprocess.Popen(["open", path])
                else:
                    import subprocess
                    subprocess.Popen(["xdg-open", path])
            except Exception as exc:
                messagebox.showerror("打不开", str(exc))
            return
        if not os.path.exists(path):
            messagebox.showinfo("找不到", "路径不存在:\n%s" % path)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)          # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                import subprocess
                subprocess.Popen(["open", path])
            else:
                import subprocess
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    # ------------------------------------------------------------------
    # 日志与队列
    # ------------------------------------------------------------------
    def _log_sink(self, msg: str, level: str) -> None:
        """来自任意线程的日志, 只入队, 不碰控件。"""
        self.queue.put(("log", msg, level))

    def _append_log(self, msg: str, level: str) -> None:
        if level == "dbg" and not self.var_verbose.get():
            return
        for name in ("txt_read_log", "txt_run_log"):
            txt = getattr(self, name, None)
            if txt is None:
                continue
            txt.configure(state="normal")
            txt.insert("end", msg + "\n", level if level in LEVEL_COLORS else "info")
            # 日志太多时截掉前面的, 免得 Text 越拖越慢
            if int(txt.index("end-1c").split(".")[0]) > 4000:
                txt.delete("1.0", "1000.0")
            txt.see("end")
            txt.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _poll(self) -> None:
        """主线程定时取队列 —— Tkinter 的所有更新都发生在这一条路径上。"""
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1], msg[2])
                elif kind == "progress":
                    self._on_progress(msg[1], msg[2], msg[3])
                elif kind == "stage":
                    self.var_stage.set("  %d/%d %s" % (msg[1], msg[2], msg[3]))
                elif kind == "done":
                    self._on_job_done(msg[1], msg[2])
                elif kind == "fail":
                    self._on_job_fail(msg[1], msg[2])
                elif kind == "status":
                    self.set_status(msg[1])
                elif kind == "quick_ok":
                    self._on_quick_done(msg[1], msg[2])
                elif kind == "quick_err":
                    self._on_quick_fail(msg[1], msg[2])
                elif kind == "meta":
                    self._on_meta_repaired(msg[1])
                # --- 和 AI 的讨论 (见 send_chat / update_detail_from_chat) ---
                elif kind == "chat_ok":
                    self._on_chat_reply(msg[1], msg[2])
                elif kind == "chat_err":
                    self._on_chat_fail(msg[1], msg[2])
                elif kind == "chat_meta":
                    self._on_chat_meta(msg[1])
                elif kind == "chat_detail":
                    self._on_chat_detail(msg[1], msg[2])
                elif kind == "chat_detail_err":
                    self._on_chat_detail_fail(msg[1], msg[2])
        except queue.Empty:
            pass
        self.after(POLL_MS, self._poll)

    def _on_progress(self, done: int, total: int, text: str) -> None:
        pct = int(100.0 * done / total) if total else 0
        pb = self.pb_read if self._current_job == "read" else self.pb_run
        pb.configure(value=pct)
        if self._current_job == "read":
            self.lbl_read_pct.config(text="  %d%%" % pct)
        self.set_status("%s (%d/%d)" % (text, done, total))

    # ------------------------------------------------------------------
    # 任务调度
    # ------------------------------------------------------------------
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _start_job(self, name: str, target: Callable[[], Any]) -> bool:
        if self._busy():
            messagebox.showinfo("正在忙", "上一个任务还没结束, 请稍等或点\"停止\"。")
            return False
        if self.dirty and name == "run":
            if messagebox.askyesno("有未保存的修改",
                                   "设置里有未保存的修改, 现在保存吗?"):
                self.save_config()
        self.stop_event.clear()
        self._current_job = name
        self._set_buttons(False)
        self.queue.put(("status", "已开始"))

        def _wrap() -> None:
            try:
                payload = target()
                self.queue.put(("done", name, payload))
            except Exception as exc:
                self.queue.put(("fail", name, "%s\n%s"
                                % (exc, traceback.format_exc())))

        self.worker = threading.Thread(target=_wrap, daemon=True)
        self.worker.start()
        return True

    def _set_buttons(self, idle: bool) -> None:
        state = "normal" if idle else "disabled"
        for b in (self.btn_read, self.btn_reread, self.btn_run):
            b.configure(state=state)
        # 停止按钮: 空闲时是灰描边、且不可点; 一开跑就换成**实心红色**。跑起来的
        # 时候它是全屏唯一能中断程序的东西, 必须一眼就看得见 —— 以前它和旁边
        # 的"打开报告"长得一模一样, 用户根本不知道能点它。
        self.btn_stop.configure(state="disabled" if idle else "normal",
                                style="Quiet.TButton" if idle else "Stop.TButton",
                                text="停止")

    def _on_job_done(self, name: str, payload: Any) -> None:
        self._set_buttons(True)
        self._current_job = ""
        if name == "read":
            self._after_read(payload)
        elif name == "run":
            self._after_run(payload)

    def _on_job_fail(self, name: str, text: str) -> None:
        self._set_buttons(True)
        self._current_job = ""
        self._append_log("任务异常:\n%s" % text, "err")
        self.set_status("任务失败")
        messagebox.showerror("任务失败", text.splitlines()[0] if text else "未知错误")

    def request_stop(self) -> None:
        self.stop_event.set()
        # 点完立刻把按钮改成"停止中"并置灰: 停止是**下一个检查点**才生效的
        # (正在跑的那批 AI 请求要收尾), 中间可能好几秒。按钮要是不变样, 用户
        # 会以为没点上, 于是连点 —— 那才是真正"停止按钮好像坏了"的来源。
        self.btn_stop.configure(text="停止中 …", state="disabled")
        self.set_status("已请求停止, 当前这一步做完就停 …")
        self._append_log("已请求停止 —— 会在当前检索式/阶段结束后停下, "
                         "已经拿到的结果照常使用。", "warn")

    # ------------------------------------------------------------------
    # 任务 1: 读取文献
    # ------------------------------------------------------------------
    def start_read(self, force: bool = False) -> None:
        self._collect_into_cfg()
        if self.dirty:
            self.save_config()
        if force and not messagebox.askyesno(
                "确认重读",
                "忽略索引, 重新解析所有 PDF?\n\n"
                "221 个 PDF 大约需要 20 秒, 大库会更久。\n"
                "(正常情况下不需要这么做: 程序已经会跳过没变过的文件)"):
            return
        self.pb_read.configure(value=0)
        self.lbl_read_pct.config(text="  0%")

        def _job() -> Dict[str, Any]:
            papers, src = load_library(
                self.cfg, force_pdf=force,
                progress=lambda d, t, p: self.queue.put(
                    ("progress", d, t, os.path.basename(p))))
            return {"papers": papers, "src": src}

        self._start_job("read", _job)

    def _after_read(self, payload: Dict[str, Any]) -> None:
        papers = payload["papers"]
        src = payload["src"]
        self.papers = papers
        self.pb_read.configure(value=100)
        self.lbl_read_pct.config(text="  100%")
        text = src.summary() + "\n" + library_summary(papers)
        self._append_log("读取完成:\n" + text, "ok")
        self.set_status("读取完成 · " + library_summary(papers))
        self._refresh_source_label()

    def show_index_stats(self) -> None:
        self._collect_into_cfg()
        try:
            st = index_stats(self.cfg)
        except Exception as exc:
            messagebox.showerror("读不到索引", str(exc))
            return
        lines = [
            "索引文件: %s" % st.get("db_path"),
            "当前读取深度: %s" % st.get("depth_label", "?"),
            "磁盘上的 PDF: %d 个" % st.get("files_on_disk", 0),
            "索引里的记录: %d 条" % st.get("indexed", 0),
            "还没解析过: %d 个" % st.get("pending", 0),
        ]
        shallower = int(st.get("shallower", 0) or 0)
        if shallower:
            # 待读里有一部分是"读过, 但读得比现在这个档位浅", 得分开说 —— 否则
            # 用户看到"待读 219"会以为是全新的文件
            lines.append("  (其中 %d 个是读得比当前档位浅, 下次会补读)" % shallower)
        by = st.get("by_status") or {}
        if by:
            lines.append("按状态: " + ", ".join(
                "%s=%d" % (k, v) for k, v in sorted(by.items())))
        lines.append("")
        lines.append("已解析过的文件下次直接复用, 不会重新打开, 也不会重复消耗 token。\n"
                     "换档位时: 往深调只补读缺的部分, 往浅调直接复用旧结果。")
        messagebox.showinfo("已读记录", "\n".join(lines))

    # ------------------------------------------------------------------
    # 推荐记录下拉框: 挑看哪一次
    # ------------------------------------------------------------------
    def refresh_rec_runs(self) -> None:
        """「重新扫描」: 把输出目录里的报告重新读一遍, 再按当前选择重铺列表。"""
        self._collect_into_cfg()
        self._reload_rec_runs()
        self._apply_rec_pick()

    def _reload_rec_runs(self) -> None:
        """重扫输出目录, 更新下拉框的选项; **不**改下面那张列表。

        保留用户当前选中的那一项 (按标签比对): 只是刷新一下选项, 不该把人家正在
        看的东西换掉。选中的那一份报告已经从磁盘上消失了才退回"本次运行的结果"。
        """
        try:
            self._rec_runs = past_runs.list_runs(self.cfg)
        except Exception as exc:
            log("扫历史推荐结果失败: %s" % exc, "warn")
            self._rec_runs = []
        n_rec = 0
        try:
            from .history import enabled, load_rows
            if enabled(self.cfg):
                n_rec = len(load_rows(self.cfg, limit=self.HISTORY_MAX_ROWS))
        except Exception:
            n_rec = 0
        values = [REC_LIVE] + [r["label"] for r in self._rec_runs] \
            + ["%s%d 篇)" % (REC_ALL_PREFIX, n_rec)]
        try:
            self.cmb_rec_run.configure(values=values)
        except Exception:
            return
        cur = self.var_rec_run.get()
        if cur not in values:
            # 当前选的那一份不在了 (报告被删/被移走) —— 退回"本次运行的结果",
            # 并在日志里说一声, 免得用户以为列表自己变了
            if cur and cur != REC_LIVE:
                self._append_log("推荐记录: 「%s」已经找不到了, 退回「%s」"
                                 % (cur, REC_LIVE), "warn")
            self.var_rec_run.set(REC_LIVE)

    def _rec_run_for_label(self, label: str) -> Optional[Dict[str, Any]]:
        for run in self._rec_runs:
            if run.get("label") == label:
                return run
        return None

    def _on_rec_pick(self, _event: Any = None) -> None:
        """下拉框换了 -> 重铺下面那张列表。"""
        self._collect_into_cfg()
        self._apply_rec_pick()

    def _apply_rec_pick(self) -> None:
        cur = self.var_rec_run.get()
        if cur.startswith(REC_ALL_PREFIX):
            self._rec_mode = "all"
            self.load_history()
            return
        run = self._rec_run_for_label(cur)
        if run is None:
            self._rec_mode = "live"
            self._show_live_result()
            return
        self._rec_mode = "run"
        self._show_rec_run(run)

    def _fall_back_to_live(self) -> None:
        """选中的那一项没东西可铺 (记录关着/是空的) —— 退回"本次运行的结果"。

        下拉框和下面那张列表必须说同一件事: 停在"全部累计记录"上、列表里却还是
        上一份东西, 用户会以为记录被谁动过。
        """
        try:
            self.var_rec_run.set(REC_LIVE)
        except Exception:
            pass
        self._rec_mode = "live"
        self._show_live_result()

    def _show_live_result(self) -> None:
        """铺回"这一轮跑出来的结果"。

        从历史报告切回来时, 这一份得原样还回来 —— 它是刚才那一轮的结果, 重跑
        一遍要几分钟, 而且结果还不一样。
        """
        self._tree_from_history = False
        self.ranked = list(self._live_ranked)
        self._fill_tree(self.ranked, set(self._live_top_ids))
        if self.ranked:
            self.tree.selection_set("1")
            self.tree.focus("1")
            self.tree.see("1")
            self._show_detail(self.ranked[0])
            self.set_status("本次运行的结果: %d 篇" % len(self.ranked))
        else:
            self._show_detail(None)
            self.set_status("还没有跑过推荐 —— 点上面的「开始推荐」跑一轮")

    def _show_rec_run(self, run: Dict[str, Any]) -> None:
        """铺某一次历史推荐的结果 (读的是那一轮留下的报告)。"""
        from .history import load_rows, rows_as_candidates

        items = list(run.get("items") or [])
        # 记录库里的解读按 arXiv ID 挂回去: 报告里只有标题和分数, 而详解是**按
        # 论文**存的 (见 history.py) —— 一篇论文在哪一轮被推荐过, 它的解读就在
        # 那儿, 不必跟着报告走。
        rows: Dict[str, Dict[str, Any]] = {}
        try:
            for r in load_rows(self.cfg, limit=self.HISTORY_MAX_ROWS):
                rows[str(r.get("arxiv_id") or "")] = r
        except Exception as exc:
            log("读推荐记录失败 (%s), 这一轮的详解可能显示不出来" % exc, "warn")
        cands = [self._candidate_from_run_item(it, run, rows) for it in items]
        cands = [c for c in cands if c is not None]

        self.ranked = cands
        # 铺的是历史报告而不是"这一轮": 那三个细分数报告里没有, 详情面板据此跳过
        # (见 _show_detail 里 from_history 那一段)。
        self._tree_from_history = False
        self._fill_tree(cands, set())
        if cands:
            self.tree.selection_set("1")
            self.tree.focus("1")
            self.tree.see("1")
            self._show_detail(cands[0])
        else:
            self._show_detail(None)
        n_an = sum(1 for c in cands if c.analyzed)
        self._append_log("推荐记录: 铺出 %s 的 %d 篇 (%d 篇有解读) · %s"
                         % (run.get("generated_at") or run.get("label"),
                            len(cands), n_an, run.get("path")), "ok")
        self.set_status("推荐记录 · %s: %d 篇 (读的是当时那份报告)"
                        % (run.get("generated_at") or "?", len(cands)))

    def _candidate_from_run_item(self, it: Dict[str, Any], run: Dict[str, Any],
                                 rows: Dict[str, Dict[str, Any]]) -> Any:
        """报告里的一行 -> Candidate。

        报告里没有摘要、也没有三个细分数, 所以这只是"够铺列表、够开讨论"的一份
        骨架: 标题/作者/日期/期刊/总分来自报告, 解读和"推荐过几次"来自记录库。
        """
        from .history import parse_dt
        from .models import Candidate
        from .utils import extract_arxiv_id

        aid = extract_arxiv_id(str(it.get("url") or "")) or ""
        if not aid:
            return None
        c = Candidate(arxiv_id=aid, title=str(it.get("title") or ""))
        c.authors = [str(it["authors"])] if it.get("authors") else []
        try:
            c.score = float(str(it.get("score") or "").strip())
        except (TypeError, ValueError):
            c.score = 0.0
        c.journal_ref = str(it.get("journal") or "")
        c.categories = [str(x) for x in (it.get("categories") or [])]
        if c.categories:
            c.primary_category = c.categories[0]
        try:
            c.citations = int(str(it.get("citations") or "").strip())
        except (TypeError, ValueError):
            c.citations = None
        if it.get("date"):
            c.published = parse_dt(it["date"])
        c.from_run = True
        c.record_report = str(run.get("path") or "")
        row = rows.get(aid)
        if row:
            # 解读按论文存, 不跟报告走 —— 所以哪怕是三个月前那一轮铺出来的, 只要
            # 这篇后来被解读过, 详解就是有的。
            c.summary = str(row.get("summary") or "")
            c.ideas = str(row.get("ideas") or "")
            try:
                conns = json.loads(row.get("connections") or "[]")
            except Exception:
                conns = []
            c.connections = conns if isinstance(conns, list) else []
            c.analyzed = bool(row.get("analyzed"))
            c.seen_before = True
            c.seen_times = int(row.get("times") or 0)
            c.last_recommended = str(row.get("last_at") or "")
        return c

    def load_history(self) -> None:
        """推荐记录 (下拉框里的「全部累计记录」): 把记录库里的论文铺到列表里看。

        **只读, 不删**。删是旁边那个「删除推荐记录」按钮的事 —— 看和删共用一次
        点击的话, 想翻一眼记录的人每次都得先绕过一次删除确认。

        铺出来的是真 Candidate 对象 (见 history.rows_as_candidates), 所以选中看
        详解、双击开 arXiv、分数着色这些全都照旧能用, 不需要另写一套渲染。
        """
        self._collect_into_cfg()
        from .history import (describe_history, enabled, history_path,
                              load_records)

        path = history_path(self.cfg)
        if not enabled(self.cfg):
            messagebox.showinfo(
                "推荐记录",
                "推荐记录已经关掉了 (设置页 → \"使用推荐记录\")。\n\n"
                "关着的时候, 跑完一轮不会往记录里写东西, 这里自然也没得看。")
            self._fall_back_to_live()
            return

        cands = load_records(self.cfg, limit=self.HISTORY_MAX_ROWS)
        if not cands:
            # "一直是 0 篇"最常见的原因不是记录坏了, 而是**从来没跑完过一整轮**:
            # 推荐记录是流水线最后一步写的, 中途点停止 / 关窗口 / 某一步报错都留
            # 不下东西。与其让用户对着一个空列表猜, 不如直接把这句话摆出来。
            messagebox.showinfo(
                "推荐记录",
                "%s\n\n文件: %s\n\n"
                "空的。推荐记录在流水线的**最后一步**才写进去 —— 中途停止、关掉"
                "窗口、或者前面某一步报错, 都不会留下记录。完整跑完一次推荐就有了。"
                % (describe_history(self.cfg), path))
            self._fall_back_to_live()
            return

        self.ranked = cands
        self._tree_from_history = True
        # top_ids 传空集: "推荐"那个高亮标的是"这一轮选中的几篇", 而记录里全是
        # 以前推荐过的, 每一条都标成"推荐"等于没标。
        self._fill_tree(cands, set())
        self.tree.selection_set("1")
        self.tree.focus("1")
        self.tree.see("1")
        self._show_detail(cands[0])
        n_an = sum(1 for c in cands if c.analyzed)
        # 记录是只增不减的 (一天跑一轮, 一年就是几千条), 全铺进 Treeview 会把界面
        # 卡住好几秒。所以只铺最近的一批 —— 但**必须说出来**, 不能让人以为记录
        # 只有这些 (记录本身一条没少, 只是没全铺出来)。
        more = (" · 只铺了最近 %d 篇 (记录里还有更早的)" % self.HISTORY_MAX_ROWS
                if len(cands) >= self.HISTORY_MAX_ROWS else "")
        self._append_log("推荐记录: 铺出 %d 篇 (%d 篇存有解读) · %s%s"
                         % (len(cands), n_an, path, more), "ok")
        self.set_status("推荐记录: %d 篇%s (来自记录库, 不是这一轮的结果)"
                        % (len(cands), more))
        self._repair_history_meta()

    def _repair_history_meta(self) -> None:
        """后台把记录里缺的"作者/提交日期"从 arXiv 补回来, 补到就刷新列表。

        为什么要有这一步: 那几列是**当时**落库的, 而 v1 的库压根没有这几列 (那时
        这张表只用来"别再重复解读")。后来加列的时候老记录一律留空, 报告又多半已经
        不在了 (报告存在 output/ 里, 是会被清理的), 于是用户点开「推荐记录」看到的
        就是作者/日期/期刊三列全空 —— 数据其实一直在 arXiv 上, 只是没人去取。

        列表**先铺出来再补**: 补全要联网, 慢的那次能等十几秒。先让用户看见东西,
        补到了再就地刷新那几格, 比转着圈等一个完整列表好。
        """
        if self._meta_thread is not None and self._meta_thread.is_alive():
            return
        # 只补"当前铺的就是记录"的那一次 —— 铺的是这一轮结果时没有"老记录"可补
        if not self._tree_from_history or not self.ranked:
            return
        self._collect_into_cfg()
        cfg = self.cfg

        def _work() -> None:
            try:
                from .history import load_rows, repair_records
                rows = load_rows(cfg, limit=self.HISTORY_MAX_ROWS)
                if not rows:
                    return
                cache = None
                try:
                    cache = build_caches(cfg)["http"]
                except Exception:
                    # 缓存建不起来只是"要重新联网取一遍", 不是失败
                    pass
                got = repair_records(cfg, rows, cache=cache)
                if got:
                    self.queue.put(("meta", got))
            except Exception as exc:
                # 补全是**附加**功能: 它出任何问题都不该冒到界面上 (列表已经铺好
                # 了, 那几列继续写破折号而已)。
                self.queue.put(("log", "补全推荐记录元信息失败: %s" % exc, "dbg"))

        self._meta_thread = threading.Thread(target=_work, daemon=True)
        self._meta_thread.start()

    def _on_meta_repaired(self, got: Dict[str, Dict[str, Any]]) -> None:
        """补元信息的线程回来了 —— 就地更新内存里的 Candidate 和那几格。"""
        from .utils import fmt_authors as _fa, fmt_date as _fd, fmt_journal as _fj
        by_id = {str(k): v for k, v in (got or {}).items() if str(k or "").strip()}
        if not by_id:
            return
        n = 0
        sel = self.tree.selection()
        sel_id = ""
        if sel:
            try:
                sel_id = str(getattr(self.ranked[int(sel[0]) - 1], "arxiv_id", ""))
            except (ValueError, IndexError):
                sel_id = ""
        for i, c in enumerate(self.ranked, 1):
            item = by_id.get(str(getattr(c, "arxiv_id", "") or ""))
            if not item:
                continue
            # 只填**空的**: 库里已经有值的那几列以库为准 (库是用户自己的记录,
            # arXiv 只是补缺), 否则每次铺一遍记录都会把老记录里的期刊刷掉。
            if not c.authors and item.get("authors"):
                c.authors = list(item["authors"])
            if c.published is None and item.get("published") is not None:
                c.published = item["published"]
            if not c.journal_ref and item.get("journal"):
                c.journal_ref = str(item["journal"])
            if not c.categories and item.get("categories"):
                c.categories = list(item["categories"])
                c.primary_category = c.categories[0]
            if c.citations is None and item.get("citations") is not None:
                c.citations = item["citations"]
            iid = str(i)
            if not self.tree.exists(iid):
                continue
            # 树里的 iid 是**行号**, 而这个线程跑的时候用户可能已经又铺了一次列表
            # (换了一轮结果 / 删了记录)。行号对不上就把别人的格子改了 —— 先对一下
            # 这一行现在是不是还坐着同一篇论文。
            vals = self.tree.item(iid, "values")
            if vals and str(vals[1]) != c.title.replace("$", ""):
                continue
            self.tree.set(iid, "authors", _fa(c.authors))
            self.tree.set(iid, "pub", _fd(c))
            self.tree.set(iid, "journal", _fj(c))
            n += 1
        if not n:
            return
        self._append_log("推荐记录: 从 arXiv 补回了 %d 篇的作者/提交日期" % n, "ok")
        if sel_id and sel_id in by_id and sel:
            # 详解面板正显示着这一篇, 那上面的作者/日期也得跟着更新
            try:
                self._show_detail(self.ranked[int(sel[0]) - 1])
            except (ValueError, IndexError):
                pass

    def clear_history(self) -> None:
        """删除推荐记录: 确认之后清空 recommend_history.sqlite。

        确认框里把**范围**写清楚 (只清记录, 不动报告、不动文献索引) —— 用户点
        "删除"时最怕的就是"会不会把我别的东西也删了"。删之前先把有几条摆出来,
        免得点完才发现清掉的是攒了很久的一堆。
        """
        self._collect_into_cfg()
        from .history import RecommendHistory, describe_history, enabled, history_path

        if not enabled(self.cfg):
            messagebox.showinfo("推荐记录", "推荐记录已经关掉了, 这里没有东西可删。")
            return
        path = history_path(self.cfg)
        head = describe_history(self.cfg)
        if not os.path.exists(path):
            messagebox.showinfo("推荐记录", "%s\n\n文件: %s\n\n还没有建, 没有东西可删。"
                                % (head, path))
            return
        # "空不空"必须老老实实数一遍, **不能**拿 describe_history 那串字去
        # `"0 篇" in head` 判断: 那句话里"其中 0 篇存有解读"也含 "0 篇" —— 一条
        # 有记录但都没解读的库会被当成空库, 于是"删除"按钮静默什么都不做 (真踩过)。
        try:
            with RecommendHistory(path) as h:
                total = h.counts()["total"]
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        if not total:
            # 已经是空的: 再弹一次"确定要清空吗"纯属多此一举
            messagebox.showinfo("推荐记录", "%s\n\n文件: %s\n\n本来就是空的, 不用删。"
                                % (head, path))
            return
        # 和 AI 的讨论记录也一起删 (见 history.clear) —— 确认框里得写明, 否则
        # 用户删完记录才发现"聊了半天的东西也没了"。
        n_chat = 0
        try:
            with RecommendHistory(path) as h:
                n_chat = h.chat_counts()["messages"]
        except Exception:
            n_chat = 0
        extra = ("\n其中 %d 条是你和 AI 的讨论记录, 会一起删掉。" % n_chat
                 if n_chat else "")
        if not messagebox.askyesno(
                "删除推荐记录",
                "%s\n\n文件: %s\n\n"
                "要清空推荐记录吗?\n"
                "(清空只影响这一份记录, 不会删除报告, 也不会动文献索引)"
                "%s" % (head, path, extra)):
            return
        try:
            with RecommendHistory(path) as h:
                n = h.clear()
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self._append_log("推荐记录已清空 (%d 篇%s)"
                         % (n, (", 含 %d 条讨论记录" % n_chat) if n_chat else ""),
                         "warn")
        self.set_status("推荐记录已清空: %d 篇" % n)
        # 列表上铺的就是刚被删掉的那些, 留着会让人以为记录还在。但如果现在铺的
        # 是某一轮跑出来的结果, 那和记录是两码事, 不动它。
        if self._tree_from_history:
            self.tree.delete(*self.tree.get_children())
            self.ranked = []
            self._tree_from_history = False
            self.txt_detail.configure(state="normal")
            self.txt_detail.delete("1.0", "end")
            self.txt_detail.configure(state="disabled")
            # 讨论记录也没了, 对话区跟着清空 (不然屏幕上还留着一场已经不存在的对话)
            self._chat_msgs = []
            self._render_chat()
        # 下拉框里那个"全部累计记录 (N 篇)"的篇数要跟着变。先把选择退回"本次
        # 运行的结果" —— 记录已经清空, 停在"全部累计记录"上没意义, 而且那一项
        # 的标签一变, _reload_rec_runs 会以为"选的那份不见了"再报一次警。
        try:
            self.var_rec_run.set(REC_LIVE)
            self._rec_mode = "live"
        except Exception:
            pass
        self._reload_rec_runs()

    # ------------------------------------------------------------------
    # 和 AI 深入讨论这篇
    # ------------------------------------------------------------------
    def _render_chat(self) -> None:
        """把 ``self._chat_msgs`` 铺进对话区。**只画, 不取数据。**"""
        txt = getattr(self, "txt_chat", None)
        if txt is None:
            return
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        if not self._chat_msgs:
            c = self._chat_paper
            if c is None:
                hint = ("在上面的列表里选一篇论文, 就可以在这里追问它 —— 比如"
                        "「它的符号问题是怎么绕过的」「这个结论在小格点上还成立吗」。")
            else:
                hint = ("还没有聊过这篇。下面输入框里问一句, 比如「这篇的方法我能"
                        "直接搬到自己的模型上吗」。\n"
                        "讨论会存在这篇论文的记录里, 下次选中它还在。")
            txt.insert("end", hint + "\n", "sys")
        for m in self._chat_msgs:
            who = str(m.get("role") or "")
            label = "你" if who == "user" else "AI"
            txt.insert("end", "%s\n" % label,
                       "who_user" if who == "user" else "who_ai")
            txt.insert("end", "%s\n" % str(m.get("content") or "").strip(), "msg")
        if self._chat_busy_aid:
            txt.insert("end", "AI 正在思考…\n", "sys")
        txt.configure(state="disabled")
        txt.see("end")

    def _load_chat_for(self, c: Any) -> None:
        """选中的论文换了 -> 把这篇文章的讨论记录读出来铺进对话区。

        库文件不在就直接显示"还没有聊过", **不建库** —— 光选一篇论文看详解不该
        在用户盘上凭空多出一个 sqlite 文件。
        """
        if getattr(self, "txt_chat", None) is None:
            return          # 对话区还没建出来 (理论上到不了这儿, 兜一手)
        self._chat_paper = c
        aid = str(getattr(c, "arxiv_id", "") or "") if c is not None else ""
        if aid:
            title = (getattr(c, "title", "") or "").replace("$", "")
            self.var_chat_paper.set("正在讨论: %s" % (truncate(title, 56, "…")
                                                   if title else aid))
        else:
            self.var_chat_paper.set("还没有选中论文")
        if self._chat_busy_aid and self._chat_busy_aid == aid:
            # 这篇正在等 AI 回复: 界面上的对话不能重读一遍 —— 那条"正在思考…"
            # 和刚发出去的问题还没落进库, 重读会把它们抹掉。
            return
        self._chat_msgs = []
        if aid:
            try:
                from .history import load_chat
                self._chat_msgs = load_chat(self.cfg, aid)
            except Exception as exc:
                log("读讨论记录失败: %s" % exc, "warn")
        self._render_chat()

    def _chat_ai(self, cfg: Dict[str, Any]) -> Any:
        """建一个 AI 客户端。没配 key / 配错就直接抛 (由调用方报给用户)。"""
        from .ai import build_client
        return build_client(cfg, None)

    def _chat_context(self, cfg: Dict[str, Any], c: Any) -> Any:
        """这篇论文的提示词前缀 (带缓存)。返回 ``(前缀, 标签)``。

        拼一次要读文献库、算 TF-IDF, 每问一句都重算太亏。缓存按 arXiv ID 存,
        "用对话更新详解"之后清掉 —— 那份前缀里含**旧**的详解。
        """
        aid = str(getattr(c, "arxiv_id", "") or "")
        hit = self._chat_ctx.get(aid)
        if hit is not None:
            return hit
        from . import chat as chat_mod
        profile = None
        res = getattr(self, "result", None)
        if res is not None and getattr(res, "profile", None) is not None:
            # 这一轮跑出来的画像最贴题 (推荐就是按它做的), 有就用它
            profile = res.profile
        ctx = chat_mod.build_context(cfg, c, profile=profile,
                                     papers=self._chat_papers_cache(cfg))
        self._chat_ctx[aid] = ctx
        return ctx

    def _chat_papers_cache(self, cfg: Dict[str, Any]) -> Any:
        """文献库列表 (讨论用)。缓存一份 —— 它是拼上下文里最贵的那一步。"""
        if getattr(self, "_chat_papers", None) is None:
            try:
                from .library import load_library
                self._chat_papers = load_library(cfg)[0]
            except Exception as exc:
                log("讨论: 读文献库失败 (%s)" % exc, "warn")
                self._chat_papers = []
        return self._chat_papers

    def send_chat(self) -> None:
        """把输入框里那句话发给 AI。"""
        c = self._chat_paper
        if c is None:
            messagebox.showinfo("先选一篇", "在上面的列表里点一篇论文, 再和 AI 讨论。")
            return
        text = (self.var_chat_in.get() or "").strip()
        if not text:
            return
        aid = str(getattr(c, "arxiv_id", "") or "")
        if not aid:
            messagebox.showinfo("没有 arXiv ID",
                                "这篇没有 arXiv ID, 讨论没法按论文存起来。")
            return
        if self._chat_busy_aid:
            messagebox.showinfo("正在等回复", "上一个问题还没答完, 等它答完再问。")
            return
        self._collect_into_cfg()
        cfg = self.cfg
        if self.dirty:
            self.save_config()

        # 用户这句**先落库**: 它是用户敲进去的东西, 不该因为 AI 那边失败而丢掉。
        from .history import append_chat
        if not append_chat(cfg, aid, "user", text):
            messagebox.showerror("存不下来",
                                 "这句没能写进推荐记录 (%s)。\n\n"
                                 "对话是按论文存进推荐记录库的, 存不进去就不发出去 ——"
                                 " 免得聊完才发现全丢了。"
                                 % self._history_hint())
            return
        self.var_chat_in.set("")
        self._chat_msgs.append({"role": "user", "content": text})
        self._chat_busy_aid = aid
        self._render_chat()
        self.btn_chat_send.configure(state="disabled")

        history_msgs = list(self._chat_msgs[:-1])   # 不含刚问的这一句
        title = (getattr(c, "title", "") or "").replace("$", "")

        def _work() -> None:
            try:
                ai = self._chat_ai(cfg)
                from . import chat as chat_mod
                cache = None
                try:
                    cache = build_caches(cfg)["http"]
                except Exception:
                    pass
                # 手上没有摘要 (从记录/报告里翻出来的老论文就是这样) 就先取一份
                if chat_mod.ensure_abstract(cfg, c, cache=cache):
                    self.queue.put(("chat_meta", aid))
                prefix, _labels = self._chat_context(cfg, c)
                answer = chat_mod.reply(ai, prefix, history_msgs, text)
                if not answer:
                    self.queue.put(("chat_err", aid, "AI 返回了空内容"))
                    return
                append_chat(cfg, aid, "assistant", answer)
                self.queue.put(("chat_ok", aid, answer))
            except Exception as exc:
                self.queue.put(("chat_err", aid, "%s" % exc))

        self._chat_thread = threading.Thread(target=_work, daemon=True)
        self._chat_thread.start()

    def _history_hint(self) -> str:
        try:
            from .history import describe_history
            return describe_history(self.cfg)
        except Exception:
            return "推荐记录"

    def _on_chat_reply(self, aid: str, text: str) -> None:
        self._chat_busy_aid = ""
        self.btn_chat_send.configure(state="normal")
        if str(getattr(self._chat_paper, "arxiv_id", "") or "") == aid:
            self._chat_msgs.append({"role": "assistant", "content": text})
            self._render_chat()
        else:
            # 等回复的时候用户换到别的论文去了。回复照常存进库 (上面已经存了),
            # 只是不往现在这一屏上贴 —— 贴上去就是张冠李戴。
            self._append_log("AI 对 %s 的回复已存进那篇论文的讨论记录" % aid, "info")
            self._render_chat()
        self._append_log("讨论: %s 已回复 (%d 字)" % (aid, len(text)), "ok")
        self.set_status("讨论: 已回复 (%d 字)" % len(text))

    def _on_chat_fail(self, aid: str, msg: str) -> None:
        self._chat_busy_aid = ""
        self.btn_chat_send.configure(state="normal")
        self._append_log("讨论失败: %s" % msg, "err")
        self.set_status("讨论失败: %s" % msg)
        if str(getattr(self._chat_paper, "arxiv_id", "") or "") == aid:
            # 不弹对话框: 对话区里写一行就够了, 而且用户刚问的那句还看得见 ——
            # 弹窗关掉之后那句话就不知道跑哪儿去了。
            self._chat_msgs.append(
                {"role": "assistant",
                 "content": "(这次没答上来: %s)\n可以再点一次「发送」重试。" % msg})
            self._render_chat()

    def _on_chat_meta(self, aid: str) -> None:
        """取回了这篇的摘要 —— 详解面板里那一格得跟着显示出来。"""
        c = self._chat_paper
        if str(getattr(c, "arxiv_id", "") or "") != aid:
            return
        self._show_detail(c)
        self._append_log("讨论: 已从 arXiv 取回 %s 的摘要" % aid, "info")

    def clear_chat(self) -> None:
        """清空**这一段**对话 (推荐记录里的标题、分数、解读一概不动)。"""
        c = self._chat_paper
        if c is None or not self._chat_msgs:
            messagebox.showinfo("没有对话", "这篇还没有聊过。")
            return
        if not messagebox.askyesno(
                "清空这段对话",
                "删掉和 AI 关于《%s》的 %d 条讨论?\n\n"
                "(只删这段对话; 推荐记录里的标题、分数、详解都不动)"
                % (truncate((c.title or "").replace("$", ""), 40, "…"),
                   len(self._chat_msgs))):
            return
        self._collect_into_cfg()
        from .history import clear_chat as _clear
        n = _clear(self.cfg, str(c.arxiv_id or ""))
        self._chat_msgs = []
        self._render_chat()
        self._append_log("讨论: 清空了 %s 的 %d 条记录" % (c.arxiv_id, n), "warn")
        self.set_status("讨论已清空: %d 条" % n)

    def update_detail_from_chat(self) -> None:
        """「用对话更新详解」: 让 AI 按这场讨论重写这篇的解读, 并写回记录。

        **单独一个按钮**是刻意的: 聊十句攒下的理解, 什么时候落进详解由用户决定。
        每聊一句就重写一遍的话, 详解会在脚下不断变形, 而且每问一句都要多花一次
        AI 调用。
        """
        c = self._chat_paper
        if c is None:
            messagebox.showinfo("先选一篇", "在上面的列表里点一篇论文, 再点这个按钮。")
            return
        if self._chat_busy_aid:
            messagebox.showinfo("正在等回复", "上一个问题还没答完, 等它答完再更新详解。")
            return
        if not [m for m in self._chat_msgs if str(m.get("role")) == "user"]:
            messagebox.showinfo(
                "还没有讨论",
                "这篇还没有和 AI 聊过 —— 详解要按讨论来重写, 先问几句再点这里。\n\n"
                "(只是想重新解读一遍的话, 跑一轮推荐即可, 那会按研究画像重写。)")
            return
        aid = str(getattr(c, "arxiv_id", "") or "")
        if not aid:
            return
        self._collect_into_cfg()
        cfg = self.cfg
        if self.dirty:
            self.save_config()
        self.btn_chat_detail.configure(state="disabled")
        self.btn_chat_detail.configure(text="正在更新…")
        self._chat_busy_aid = aid
        self._render_chat()
        msgs = list(self._chat_msgs)
        title = (getattr(c, "title", "") or "").replace("$", "")

        def _work() -> None:
            try:
                ai = self._chat_ai(cfg)
                from . import chat as chat_mod
                cache = None
                try:
                    cache = build_caches(cfg)["http"]
                except Exception:
                    pass
                chat_mod.ensure_abstract(cfg, c, cache=cache)
                prefix, labels = self._chat_context(cfg, c)
                got = chat_mod.rewrite_analysis(ai, prefix, msgs, labels)
                if not (got.get("summary") or got.get("ideas")):
                    self.queue.put(("chat_detail_err", aid, "AI 没能给出新的详解"))
                    return
                from .history import profile_fingerprint, save_analysis
                # 画像指纹照旧写: 下次跑推荐时, 这份解读就能按"画像没变"被复用,
                # 不必再花一次 token (见 history.reuse_analysis)。
                fp = ""
                try:
                    res = getattr(self, "result", None)
                    if res is not None and getattr(res, "profile", None):
                        fp = profile_fingerprint(res.profile)
                except Exception:
                    fp = ""
                _apply_chat_analysis(c, got)
                save_analysis(cfg, c, fp)
                self.queue.put(("chat_detail", aid, got))
            except Exception as exc:
                self.queue.put(("chat_detail_err", aid, "%s" % exc))

        self._chat_thread = threading.Thread(target=_work, daemon=True)
        self._chat_thread.start()

    def _on_chat_detail(self, aid: str, got: Dict[str, Any]) -> None:
        """详解更新完了: 内存里的候选对象、列表那一行的标记、详解面板一起刷新。"""
        self._chat_busy_aid = ""
        self.btn_chat_detail.configure(state="normal")
        self.btn_chat_detail.configure(text="用对话更新详解")
        c = self._chat_paper
        if c is None or str(getattr(c, "arxiv_id", "") or "") != aid:
            self._append_log("讨论: %s 的详解已按讨论更新并写回记录" % aid, "ok")
            self._render_chat()
            return
        # 上下文里含旧详解, 必须作废 —— 下一次提问要基于**新**的详解
        self._chat_ctx.pop(aid, None)
        self._show_detail(c)
        self._refresh_tree_row(c)
        self._render_chat()
        self._append_log("讨论: 《%s》的详解已按讨论更新, 并写回推荐记录"
                         % truncate((c.title or "").replace("$", ""), 40, "…"),
                         "ok")
        self.set_status("详解已按讨论更新 (写回推荐记录)")

    def _on_chat_detail_fail(self, aid: str, msg: str) -> None:
        """更新详解失败: 把按钮**放回可点的样子**, 别的照旧。

        单独一条路径 (不复用 _on_chat_fail) 是因为这里不能往对话区里塞一句
        "(这次没答上来)" —— 那句话是 AI 答不上追问, 而这次是详解没重写成功,
        对话本身好好的, 一句没丢。
        """
        self._chat_busy_aid = ""
        self.btn_chat_detail.configure(state="normal")
        self.btn_chat_detail.configure(text="用对话更新详解")
        # 重画一下: 刚才置了 busy, 对话区末尾挂着一行"AI 正在思考…", 得撤掉
        self._render_chat()
        self._append_log("更新详解失败: %s (对话没动, 可以再点一次)" % msg, "err")
        self.set_status("更新详解失败: %s" % msg)

    def _refresh_tree_row(self, c: Any) -> None:
        """列表里那一行的"已解读"标记要跟着变 —— 刚更新完详解, 标记得亮起来。"""
        # 按**身份**找, 不用 list.index: Candidate 是 dataclass, __eq__ 比的是
        # 所有字段 —— 两篇不同的论文碰巧字段全同就会指错行 (而且逐个比字段很慢)。
        i = 0
        for j, cand in enumerate(self.ranked):
            if cand is c:
                i = j + 1
                break
        if not i:
            return
        iid = str(i)
        if not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        if len(vals) < 7:
            return
        flags = [f for f in str(vals[6] or "").split() if f]
        if "已解读" not in flags and "复用解读" not in flags:
            flags.append("已解读")
        vals[6] = " ".join(flags)
        self.tree.item(iid, values=vals)

    def clear_index(self) -> None:
        if not messagebox.askyesno(
                "确认清空",
                "清空 PDF 解析索引?\n\n"
                "下次读取会重新解析所有 PDF (221 个约 20 秒)。\n"
                "不会删除任何 PDF 文件, 只是丢掉已读记录。"):
            return
        lcfg = self.cfg.get("library", {})
        db = lcfg.get("index_db") or "library_index.sqlite"
        if not os.path.isabs(db):
            db = os.path.join(project_root(), db)
        try:
            if os.path.exists(db):
                os.remove(db)
            self.set_status("索引已清空: %s" % db)
            self._append_log("索引已清空, 下次读取将重新解析全部 PDF。", "warn")
        except Exception as exc:
            messagebox.showerror("清空失败", str(exc))

    # ------------------------------------------------------------------
    # 任务 2: 文献推荐
    # ------------------------------------------------------------------
    def start_recommend(self) -> None:
        self._collect_into_cfg()
        if self.dirty:
            self.save_config()
        if not [f for f in (self.cfg.get("pdf_folders") or [])
                if f.get("enabled", True) and f.get("path")]:
            # 文件夹现在只在"读取文献"页能加, 所以这句话和跳转都得指那儿 ——
            # 还写着"设置"的话, 用户切过去只会看到一个已经没有的编辑器。
            messagebox.showinfo("没有文献来源",
                                "请先在\"读取文献\"页添加一个文献 PDF 文件夹。")
            self.nb.select(self.tab_read)
            return
        self.pb_run.configure(value=0)
        self.var_stage.set("  准备中…")
        for item in self.tree.get_children():
            self.tree.delete(item)

        queries = [q.strip() for q in self.var_queries.get().split(";") if q.strip()]
        cats = self._selected_categories()
        subscribe = bool(self.var_subscribe.get())
        if subscribe and not cats:
            # 早点拦下来。放给 pipeline 去报的话, 得先读完整个文献库、再跑一次
            # AI 画像, 几分钟之后才告诉你"忘了勾分类" —— 那几分钟纯属白等。
            messagebox.showinfo(
                "订阅模式还没选分类",
                "订阅模式不使用检索式, 候选论文全部来自所勾选分类的当天公告。\n\n"
                "请至少勾选一个分类, 或者取消「订阅模式」。")
            return
        try:
            top_n = max(1, int(float(self.var_top.get())))
        except ValueError:
            top_n = 20
        try:
            conc = max(1, int(float(self.var_conc.get())))
        except ValueError:
            conc = 3

        # 不传 no_fulltext / read_depth: 读多少由"读取深度"决定, 而那个单选框
        # 早就写进 cfg["library"]["read_depth"] 了 (_collect_into_cfg 在进函数
        # 第一行就调过), 管线自己会读。这里再传一份等于给同一个设置留两个入口,
        # 就是刚撤掉的那个复选框出问题的原因。
        opts = PipelineOptions(
            top_n=top_n, queries=queries or None, categories=cats or None,
            subscribe_only=subscribe,
            no_ai=not self.var_use_ai.get(), no_enrich=not self.var_enrich.get(),
            concurrency=conc)

        cb = PipelineCallbacks(
            on_stage=lambda i, n, name: self.queue.put(("stage", i, n, name)),
            on_progress=lambda d, t, s: self.queue.put(("progress", d, t, s)),
            should_stop=self.stop_event.is_set)

        def _job() -> Any:
            return run_pipeline(self.cfg, opts, cb)

        self._start_job("run", _job)

    def _after_run(self, res: Any) -> None:
        self.result = res
        self.ranked = res.ranked
        if res.cancelled:
            self.set_status("已停止 · " + res.summary())
        elif not res.ok:
            self.set_status("未完成 · " + (res.message or ""))
            messagebox.showwarning("没有结果", res.message or "运行失败")
        else:
            self.set_status("完成 · " + res.summary())

        self._tree_from_history = False
        # 这一轮的结果留一份: 「推荐记录」下拉框切回"本次运行的结果"时要原样还
        # 回来 —— 重跑一遍要几分钟, 而且结果还不一样。
        self._live_ranked = list(res.ranked or [])
        self._live_top_ids = {c.arxiv_id for c in (res.top or [])}
        self._fill_tree(res.ranked, {c.arxiv_id for c in (res.top or [])})

        # 刚跑完, 下拉框回到"本次运行的结果", 并把这一轮新写下的报告扫进来 (它
        # 此刻才第一次出现在选项里)。用户想看上一轮, 从这里挑就是了。
        try:
            self.var_rec_run.set(REC_LIVE)
        except Exception:
            pass
        self._rec_mode = "live"
        self._reload_rec_runs()

        # 换了一轮, 拼上下文用的那两个缓存作废: 里面存的是上一轮那份画像和文献库,
        # 留着会让新一篇的讨论接着旧上下文走。**不动**对话区本身 —— 它跟着下面
        # 详解面板里那一篇走, 而详解面板此刻还停在上一篇上 (见 _show_detail)。
        self._chat_ctx.clear()
        self._chat_papers = None

        if res.report_paths:
            self.btn_report.configure(state="normal")
        if res.ok:
            self._append_log("推荐完成:\n" + res.summary(), "ok")
            s = res.stats
            if s.recommended_seen:
                self._append_log(
                    "推荐记录: 候选里 %d 篇以前推荐过%s"
                    % (s.recommended_seen,
                       (" (已按设置跳过 %d 篇)" % s.recommended_skipped)
                       if s.recommended_skipped else ""), "info")
            if s.analysis_reused:
                self._append_log(
                    "推荐记录: %d 篇直接复用旧解读, 没有重复消耗 token" % s.analysis_reused,
                    "ok")

    def _fill_tree(self, ranked: Any, top_ids: Any) -> None:
        """把排序结果铺进列表。

        单独拎出来是为了能**脱网**测: 这一段的取值逻辑 (作者只列第一位、日期
        取 v1、没有期刊写破折号) 跟抓没抓到论文毫无关系, 不该非得连上 arXiv
        才能验一遍。
        """
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(ranked, 1):
            flags = []
            if c.arxiv_id in top_ids:
                flags.append("推荐")
            if c.from_history:
                # 记录库里**每一条都是推荐过的**, 写"已推荐过"等于整列同义反复,
                # 什么信息都没多。这里给的是"推过几次" —— 反复冒出来的那些才是
                # 值得多看一眼的。
                flags.append("推荐 %d 次" % c.seen_times if c.seen_times
                             else "已推荐过")
            elif c.seen_before:
                flags.append("已推荐过")
            if c.journal_ref:
                flags.append("已发表")
            if (c.citations or 0) >= 50:
                flags.append("经典")
            if c.age_days is not None and c.age_days <= 180:
                flags.append("新")
            if c.analyzed:
                flags.append("复用解读" if c.reused else "已解读")
            # tag 顺序 = 优先级: 推荐高亮 > 斑马纹 > 分数配色。
            # 三者设的是不同选项 (background / foreground), 本不冲突, 但把
            # "top" 放最前是刻意的 —— 万一以后有人给斑马纹也配了 background,
            # 高亮仍然赢。
            tags = []
            if c.arxiv_id in top_ids:
                tags.append("top")
            tags.append("even" if i % 2 == 0 else "odd")
            tags.append("good" if c.score >= 0.65 else
                        ("mid" if c.score >= 0.35 else "low"))
            self.tree.insert("", "end", iid=str(i), values=(
                i, c.title.replace("$", ""),
                # 作者只列第一位: 这一列窄, 全列出来会被 Treeview 硬截成半截
                # 名字 ("Wei Wa…"), 还不如"第一作者 等"来得干净。全名单在
                # 下面的详解里。日期取 v1 提交日, 不是最后修订日 —— 用户关心
                # 的是"什么时候出来的"。
                fmt_authors(c.authors), fmt_date(c), fmt_journal(c),
                "%.3f" % c.score, " ".join(flags),
            ), tags=tuple(tags))

    def _on_select_candidate(self, _event: Any = None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        try:
            c = self.ranked[int(sel[0]) - 1]
        except (ValueError, IndexError):
            return
        self._show_detail(c)

    def _on_tree_double(self, _event: Any = None) -> None:
        """双击 -> 用浏览器打开 arXiv 摘要页。

        列表里只有标题和分数, 真正判断"要不要读"得看原文, 所以双击直接送过去
        比再展开一遍已经显示在下面的详解更有用。
        """
        sel = self.tree.selection()
        if not sel:
            return
        try:
            c = self.ranked[int(sel[0]) - 1]
        except (ValueError, IndexError):
            return
        url = getattr(c, "abs_url", "") or ""
        if not url and getattr(c, "arxiv_id", ""):
            url = "https://arxiv.org/abs/%s" % c.arxiv_id
        if url:
            self._open_path(url)

    def _detail_tags(self) -> None:
        """详解框里的文字样式。只配一次, 之后每次填内容复用。"""
        if getattr(self, "_detail_tags_done", False):
            return
        t = self.txt_detail
        t.tag_configure("title", font=(self.font_name, 12, "bold"),
                        foreground=theme.FG, spacing3=4)
        t.tag_configure("meta", foreground=theme.MUTED, spacing3=1)
        t.tag_configure("link", foreground=theme.ACCENT)
        t.tag_configure("head", font=(self.font_name, 10, "bold"),
                        foreground=theme.ACCENT_DARK, spacing1=10, spacing3=3)
        t.tag_configure("bullet", lmargin1=18, lmargin2=30)
        t.tag_configure("quote", lmargin1=30, lmargin2=44,
                        foreground=theme.MUTED)
        self._detail_tags_done = True

    def _show_detail(self, c: Any) -> None:
        self._detail_tags()
        t = self.txt_detail
        t.configure(state="normal")
        t.delete("1.0", "end")
        if c is None:
            # 列表空了 (清空记录 / 还没跑过推荐) —— 详解和对话区一起空着, 别把
            # 上一篇的内容留在屏幕上
            t.configure(state="disabled")
            self._load_chat_for(None)
            return

        def put(text: str, tag: str = "") -> None:
            # 空 tag 串不能直接传给 insert —— 那会被当成"一个叫空字符串的 tag",
            # 而不是"没有 tag"
            if tag:
                t.insert("end", text + "\n", tag)
            else:
                t.insert("end", text + "\n")

        put(c.title.replace("$", ""), "title")

        meta = []
        if c.authors:
            meta.append("、".join(c.authors[:6]) + (" 等" if len(c.authors) > 6 else ""))
        if c.published:
            meta.append(c.published.strftime("%Y-%m-%d"))
        if c.primary_category:
            meta.append(c.primary_category)
        if c.journal_ref:
            meta.append("已发表: %s" % c.journal_ref)
        if c.citations is not None:
            meta.append("引用 %d" % c.citations)
        put(" · ".join(meta), "meta")
        put("arXiv: %s" % c.arxiv_id, "meta")
        t.insert("end", c.abs_url + "\n", "link")
        if c.from_history or c.from_run:
            # 这两类都是从别处读回来的, 那三个分数**根本没存下来** (记录库里存
            # 的是"推荐过没有"和当时的解读, 报告里只有总分)。照常列出来就是三个
            # 0.00, 读起来像"这篇论文三项全 0 分" —— 是在报假数据。
            if c.from_run:
                # 报告里有那一轮的总分, 这个数是真的, 照实写出来
                put("总分 %.3f (那一轮报告里的分数) · 来自 %s"
                    % (c.score, os.path.basename(c.record_report)
                       if c.record_report else "历史报告"), "meta")
            if c.seen_times:
                put("记录: 推荐过 %d 次 · 最近一次 %s"
                    % (c.seen_times, c.last_recommended or "—"), "meta")
            if c.record_report:
                put("当时写进: %s" % c.record_report, "meta")
        else:
            put("相关 %.2f (权重后 %.3f) · 时效 %.2f · 重要 %.2f"
                % (c.relevance, c.score, c.recency, c.importance), "meta")
            if c.relevance_reason:
                put("相关性依据: %s" % c.relevance_reason, "meta")
            if c.seen_before:
                put("推荐记录: 以前推荐过 %d 次, 最近一次 %s"
                    % (c.seen_times, c.last_recommended or "—"), "meta")
        if c.reused:
            put("解读: 直接复用了推荐记录里的旧解读 (研究画像没变, 没有调用 AI)",
                "meta")

        put("摘要", "head")
        if c.abstract:
            put(c.abstract)
        elif c.from_history or c.from_run:
            # 从记录/报告里读回来的论文手上没有摘要 (摘要没进记录库)。说清楚它会
            # 自己取回来 —— 否则"摘要 (无)"看着像这篇论文没有摘要。
            put("(记录里没存摘要。和 AI 讨论时程序会自动去 arXiv 取一份, "
                "取回来就显示在这里)", "meta")
        else:
            put("(无)")
        if c.summary:
            put("内容讲解", "head")
            put(c.summary)
        if c.connections:
            put("与你的文献的关联", "head")
            for conn in c.connections:
                label = conn.get("paper") or conn.get("title") or ""
                why = conn.get("relation") or conn.get("why") or ""
                put("· %s" % label, "bullet")
                if why:
                    put(why, "quote")
        if c.ideas:
            put("可以结合的研究方向", "head")
            put(c.ideas)
        if not c.analyzed:
            put("")
            put("(这篇没有做 AI 深度解读 —— 它不在推荐的前 N 篇里, "
                "或者本次运行关闭了 AI)" if not (c.from_history or c.from_run) else
                "(这篇当时没做 AI 深度解读 —— 它不在那一轮推荐的前 N 篇里, "
                "或者那一轮关闭了 AI; 记录里只留了标题和分数。想深入看的话, "
                "可以在下面和 AI 聊几句, 再点「用对话更新详解」)", "meta")
        t.configure(state="disabled")
        t.yview_moveto(0.0)
        # 内容换了, 高度得重算 (而且是重算**几拍** —— 估一次、按 yview 补、再让
        # 整页跟上)。fresh=True: 这是新内容, 允许变矮。
        self._settle_detail(None, 0, True)
        # 详解和讨论是同一篇论文的两块: 换了一篇, 对话区也跟着换。放在这里而不是
        # 每个调用点, 是因为"当前选中的是哪一篇"这件事只有这里说了算。
        self._load_chat_for(c)

    def open_report(self) -> None:
        """打开最近一份报告。

        先看这次会话跑出来的那一份; 没有就去设置里指定的**报告目录**里挑最新的
        一份。以前只看 ``self.result`` —— 那意味着"重启程序之后点打开报告"永远
        得到"先跑一次推荐", 哪怕报告目录里躺着十份。用户想看的显然是最近跑出来
        的那一份, 而不是被要求重跑一遍。
        """
        paths = list(getattr(self.result, "report_paths", None) or [])
        if paths and os.path.exists(paths[0]):
            self._open_path(paths[0])
            return

        self._collect_into_cfg()
        out_dir = past_runs.output_dir(self.cfg)
        runs = past_runs.list_runs(self.cfg)
        if runs:
            newest = runs[0]
            self._open_path(newest["path"])
            self.set_status("已打开报告: %s" % os.path.basename(newest["path"]))
            return

        # 走到这儿说明报告目录里真的一份都没有 —— 把**实际找过的那个目录**报
        # 出来。用户报"报告没写到设置里那个目录"时, 九成是程序用的配置和他以为
        # 的不是同一份 (见 pipeline.log_paths), 路径摆出来才能对上账。
        messagebox.showinfo(
            "还没有报告",
            "这个目录里还没有 markdown 报告:\n\n%s\n\n"
            "跑一次推荐就会生成 (报告在流水线的最后一步才落盘, "
            "中途停止或关闭窗口都不会有报告)。" % out_dir)

    # ------------------------------------------------------------------
    # 设置页的两个测试按钮
    # ------------------------------------------------------------------
    def test_ai(self) -> None:
        self._collect_into_cfg()
        if self.dirty:
            self.save_config()
        if not self.cfg.get("ai", {}).get("api_key"):
            if not messagebox.askyesno(
                    "没有 API key",
                    "ai.api_key 是空的。也可以用环境变量 %s。\n\n"
                    "仍然要试一下吗?" % self.cfg.get("ai", {}).get(
                        "api_key_env", "ARXIV_REC_API_KEY")):
                return

        def _job() -> str:
            caches = build_caches(self.cfg, refresh=False)
            ai = make_ai(self.cfg, caches, no_ai=False)
            if ai.__class__.__name__ == "NullAIClient":
                raise RuntimeError("没有可用的 API key, 客户端是空实现")
            # use_cache=False: 每次点都真的发一次请求, 否则测了个寂寞
            reply = ai.chat("你是一个连通性测试探针。", "只回复两个字: 正常",
                            use_cache=False)
            return "模型 %s 回复: %s" % (ai.model, (reply or "").strip()[:200])

        self._run_quick("测试 AI 连接", _job)

    def test_arxiv(self) -> None:
        self._collect_into_cfg()
        if self.dirty:
            self.save_config()

        def _job() -> str:
            from .arxiv_search import build_session, search_api, search_rss
            from .utils import resolve_proxy
            ncfg = self.cfg.get("network", {})
            session = build_session(ncfg.get("proxy"), timeout=int(
                ncfg.get("timeout", 40)))
            retries = int(ncfg.get("retries", 4))
            # 实际走的是哪条路 —— 配了代理但代理探测不通时 build_session 会自动
            # 退回直连, 这里说清楚, 免得用户对着"连接正常"和设置里的代理地址发懵
            used = resolve_proxy(ncfg.get("proxy"))
            route = ("直连" if not used
                     else "代理 %s" % list(used.values())[0])
            if ncfg.get("proxy") and not used:
                route += "  (设置里填的代理探测不通, 已自动跳过)"

            # 两条通道分别测: 检索通道 (Atom API) 挂了但 RSS 还通, 是两种完全
            # 不同的故障, 分开报才知道该去查什么
            got = search_api(session, "quantum Monte Carlo sign problem",
                             max_results=5, cache=None, delay=3.0,
                             retries=retries)
            cats = (self.cfg.get("arxiv", {}).get("categories") or [])[:1]
            rss = search_rss(session, cats, cache=None, delay=3.0,
                             retries=retries) if cats else []

            if got is None and not rss:
                raise RuntimeError(
                    "两条通道都没有响应 (走的是%s)。检查网络/代理, 或稍后再试。"
                    % route)
            lines = ["实际线路: %s" % route]
            if got is None:
                lines.append("检索通道 (Atom API): 无响应")
            else:
                lines.append("检索通道 (Atom API): 正常, 试抓 5 条拿到 %d 条"
                             % len(got))
                if got:
                    lines.append("  示例: %s" % got[0].title[:80])
            if not cats:
                lines.append("RSS 通道: 未测 (没设分类)")
            elif rss:
                lines.append("RSS 通道: 正常, %s 今天 %d 条公告"
                             % (cats[0], len(rss)))
            else:
                lines.append("RSS 通道: 无响应 (不影响检索)")
            return "\n".join(lines)

        self._run_quick("测试 arXiv 连接", _job)

    def _run_quick(self, name: str, job: Callable[[], Any]) -> None:
        """跑一个短任务 (测试连接之类), 结果直接弹窗。

        和 ``_start_job`` 一样只往队列里投消息 —— 弹窗必须发生在主线程。
        """
        if self._busy():
            messagebox.showinfo("正在忙", "有任务在跑, 等它结束再测。")
            return
        self.set_status("%s 中 …" % name)
        self._append_log("--- %s ---" % name, "dbg")
        self._set_buttons(False)
        self._current_job = "quick"

        def _wrap() -> None:
            try:
                self.queue.put(("quick_ok", name, job()))
            except Exception as exc:
                self.queue.put(("quick_err", name, "%s" % exc))

        self.worker = threading.Thread(target=_wrap, daemon=True)
        self.worker.start()

    def _on_quick_done(self, name: str, text: Any) -> None:
        self._set_buttons(True)
        self._current_job = ""
        self._append_log("%s: %s" % (name, text), "ok")
        self.set_status("%s 通过" % name)
        messagebox.showinfo(name, str(text))

    def _on_quick_fail(self, name: str, text: str) -> None:
        self._set_buttons(True)
        self._current_job = ""
        self._append_log("%s 失败: %s" % (name, text), "err")
        self.set_status("%s 失败" % name)
        messagebox.showerror(name, text)

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno("还在运行",
                                       "有任务在跑, 确定要退出吗?"):
                return
            self.stop_event.set()
        if self.dirty:
            if messagebox.askyesno("有未保存的修改", "退出前保存设置吗?"):
                self.save_config()
        remove_log_sink(self._log_sink)
        self.destroy()


def main(config_path: Optional[str] = None) -> int:
    app = App(config_path)
    app.mainloop()
    return 0
