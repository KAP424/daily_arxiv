"""界面配色与 ttk 样式。

为什么用 ``clam`` 而不是 Windows 默认的 ``vista``
--------------------------------------------------------------------------
``vista`` 好看, 但它把颜色、内边距、边框全部交给系统主题绘制, 大部分
``style.configure`` 会被直接忽略 —— 想调个按钮颜色都做不到。``clam`` 是
ttk 自带的、完全可配置的主题, 而且**跨平台表现一致**: 打包成 exe 之后拿到别人
机器上跑, 界面不会因为系统主题不同而变形。

两个背景, 以及为什么必须成对配置
--------------------------------------------------------------------------
界面只有两种底色: 页面 (``BG``) 和卡片 (``SURFACE``, 白色)。ttk 控件**不会**
从父容器继承背景色 —— 一个 ``TLabel`` 放在白色卡片里, 如果不显式换成
``Card.TLabel``, 它会带着页面的浅灰底色画出一个方块, 看起来就是"界面坏了"。

所以每个控件类都配了 ``Page.*`` 和 ``Card.*`` 两个变体, 由
``unify_backgrounds()`` 在界面搭好之后统一扫一遍: 进 ``TLabelframe`` 就切到
``Card``, 其余沿用父级。这样新增控件时不用记得手动挑样式 —— 漏挑会被
``uitest.py`` 里的背景一致性检查抓出来 (见 ``check_backgrounds``)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# --------------------------------------------------------------------------
# 配色
# --------------------------------------------------------------------------
BG = "#eef1f6"            # 页面底色
SURFACE = "#ffffff"       # 卡片 / 数据区底色
HEADER_BG = "#e8eefc"     # 顶栏 (淡蓝, 和主色调同族)
ROW_ALT = "#f7f9fc"       # 表格斑马纹
BORDER = "#d5dae2"
FG = "#1f2937"            # 主文字
MUTED = "#6b7280"         # 次要说明文字
ACCENT = "#2563eb"        # 主色: 主要动作按钮
ACCENT_DARK = "#1d4ed8"   # 主色深: 标题、悬停
ACCENT_SOFT = "#dbe7ff"   # 主色淡: 选中行、高亮行
DANGER = "#dc2626"        # 危险色: 运行中的"停止"按钮
DANGER_DARK = "#b91c1c"
DISABLED_BG = "#e6e9ee"
DISABLED_FG = "#a3a9b3"

# 日志级别 -> 颜色。dbg 用浅灰, 免得"同一篇出现在多个文件夹"这类上百行的
# 调试信息把真正的警告和错误盖掉。
LEVEL_COLORS = {
    "dbg": "#8a8a8a",
    "info": FG,
    "warn": "#b45309",
    "err": "#b91c1c",
    "ok": "#15803d",
}

# 分数分档配色 (表格里的相关性/总分列)。三档而不是连续取色: 连续取色在
# 26px 行高下几乎看不出差别, 分档反而一眼能扫出哪些值得看。
SCORE_GOOD = "#15803d"
SCORE_MID = "#b45309"
SCORE_LOW = "#6b7280"

# 这里曾经有一套"把 ttk.Panedwindow 的分隔条画粗、画上抓手"的样式: 那时结果表
# 和详解装在 panedwindow 里, 详解的高度靠拖分隔条来调。后来详解改成按内容撑开
# (一份解读多长就多高, 自己不滚动, 由整页那层滚动容器滚), 分隔条没有可调的
# 东西了, panedwindow 整块撤掉 —— 样式也就跟着撤了。


def score_color(value: float) -> str:
    """按分值挑一个颜色。0.65 以上算好, 0.35 以下算低。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return SCORE_LOW
    if v >= 0.65:
        return SCORE_GOOD
    if v >= 0.35:
        return SCORE_MID
    return SCORE_LOW


# --------------------------------------------------------------------------
# 控件类 -> 样式名后缀。unify_backgrounds 靠这张表决定该换哪个样式。
# --------------------------------------------------------------------------
_STYLE_SUFFIX: Dict[str, str] = {
    "TFrame": "TFrame",
    "TLabel": "TLabel",
    "TCheckbutton": "TCheckbutton",
    "TRadiobutton": "TRadiobutton",
    "TLabelframe": "TLabelframe",
    "TButton": "TButton",
    "TLabelFrame": "TLabelframe",       # 少数 Tk 版本报这个名字
}

# 进到这些容器里就切到卡片底色
_CARD_CLASSES = ("TLabelframe", "TLabelFrame")

# 控件没被显式指定样式时, ttk 拿**类名**当样式名用 (TLabel / TFrame / ...)。
# 只有这些"默认样式"才该被自动换成 Page./Card. 变体: 已经被显式指定过的样式
# (Accent.TButton / H1.TLabel / Header.* ...) 是刻意挑的, 改了就是 bug。
_DEFAULT_STYLES: Dict[str, str] = {
    "TFrame": "TFrame",
    "TLabel": "TLabel",
    "TButton": "TButton",
    "TCheckbutton": "TCheckbutton",
    "TRadiobutton": "TRadiobutton",
    "TLabelframe": "TLabelframe",
    "TLabelFrame": "TLabelframe",
}

# 故意和父容器不同底色的"带子"。它们不只是自己换个颜色, 整条子树的底色都跟着
# 走 —— 所以 check_backgrounds 遇到它们时要把期望底色改成它们的实际底色,
# 而不是报"不一致"。这是唯一允许的底色例外, 除此之外一切控件都必须和父容器同色。
_BAND_STYLES = ("Header.TFrame", "Status.TFrame")

# 实心按钮: 底色就是它的"脸", **本来就该**和容器不一样 —— 这是设计, 不是漏配。
# 只有这几个, 而且它们都是叶子 (不会有需要跟着同色的子控件), 所以单独列出来,
# 而不是把整个 TButton 类都放过: 描边式的 Page./Card.TButton 仍然要接受检查,
# 那种"白卡片上摆一个灰按钮"才是真问题。
_SOLID_STYLES = ("Accent.TButton", "Stop.TButton")


def apply(root: Any, family: str, base_size: int = 10) -> None:
    """把整套样式装到 ``root`` 上。必须在建控件**之前**调用。"""
    from tkinter import ttk

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        # 没有 clam (理论上不会) 就退回默认主题 —— 颜色会丢, 但界面仍可用
        pass

    f = (family, base_size)
    fb = (family, base_size, "bold")
    fh1 = (family, base_size + 5, "bold")
    fmono = "TkFixedFont"

    def both(name: str, **kw: Any) -> None:
        """同名样式配 Page 和 Card 两份, 底色不同、其余一致。"""
        for prefix, bg in (("Page", BG), ("Card", SURFACE)):
            full = "%s.%s" % (prefix, name) if name else prefix
            style.configure(full, background=bg, **kw)

    # --- 基类: 没被 unify 扫到的控件也落到一个明确的样子上 ---
    style.configure(".", background=BG, foreground=FG, font=f)
    both("TFrame")
    both("TLabel", foreground=FG)
    both("TCheckbutton", foreground=FG)
    both("TRadiobutton", foreground=FG)
    both("TButton")

    # --- 文字 ---
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("CardMuted.TLabel", background=SURFACE, foreground=MUTED)
    style.configure("H1.TLabel", background=HEADER_BG, foreground=ACCENT_DARK,
                    font=fh1)
    style.configure("H2.TLabel", background=BG, foreground=ACCENT_DARK, font=fb)
    style.configure("CardH2.TLabel", background=SURFACE, foreground=ACCENT_DARK,
                    font=fb)
    style.configure("CardMono.TLabel", background=SURFACE, foreground=MUTED,
                    font=fmono)

    # --- 按钮 ---
    # 普通按钮: 描边式, 白底 + 细边框, 不抢主按钮的注意力
    for prefix, bg in (("Page", BG), ("Card", SURFACE)):
        name = "%s.TButton" % prefix
        style.configure(name, background=bg, foreground=FG, bordercolor=BORDER,
                        lightcolor=bg, darkcolor=bg, relief="solid",
                        borderwidth=1, focusthickness=0, padding=(12, 6))
        style.map(name,
                  background=[("pressed", DISABLED_BG), ("active", ACCENT_SOFT),
                              ("disabled", bg)],
                  foreground=[("disabled", DISABLED_FG)],
                  bordercolor=[("active", ACCENT)])
    # 主按钮: 实心主色。整个界面只有"开始读取"和"开始推荐"用它,
    # 让"下一步该点哪个"一眼可见。
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
                    relief="solid", borderwidth=1, focusthickness=0,
                    font=fb, padding=(18, 8))
    style.map("Accent.TButton",
              background=[("pressed", ACCENT_DARK), ("active", ACCENT_DARK),
                          ("disabled", "#a9bce8")],
              foreground=[("disabled", "#eef2ff")],
              bordercolor=[("disabled", "#a9bce8")])
    # 运行中的"停止": 实心红色。平时它是灰的描边按钮 (Quiet.TButton), 一点
    # "开始推荐"就换成这一身 —— 运行期间**唯一**能中断程序的那个按钮必须是
    # 全屏最扎眼的东西, 而不是和"打开报告"长一个样。
    style.configure("Stop.TButton", background=DANGER, foreground="#ffffff",
                    bordercolor=DANGER, lightcolor=DANGER, darkcolor=DANGER,
                    relief="solid", borderwidth=1, focusthickness=0,
                    font=fb, padding=(18, 8))
    style.map("Stop.TButton",
              background=[("pressed", DANGER_DARK), ("active", DANGER_DARK),
                          ("disabled", "#e8b4b4")],
              foreground=[("disabled", "#fdf2f2")],
              bordercolor=[("disabled", "#e8b4b4")])
    # 危险/次要动作 (停止、清空索引)
    style.configure("Quiet.TButton", background=BG, foreground=MUTED,
                    bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                    relief="solid", borderwidth=1, focusthickness=0,
                    padding=(12, 6))
    style.map("Quiet.TButton",
              background=[("active", "#f3f4f6"), ("disabled", BG)],
              foreground=[("disabled", DISABLED_FG)])
    # 顶栏那条带子自己也要配 —— 漏了它, lookup 会一路退到基类 "." 的页面灰,
    # 顶栏就根本不变色 (而且顶栏上的控件全都配了 HEADER_BG, 反而变成
    # "浅蓝控件摆在灰底上", 比不美化还难看)。这个漏配是 check_backgrounds
    # 抓出来的: 它发现顶栏里的标签底色 #e8eefc 和所在容器的 #eef1f6 对不上。
    style.configure("Header.TFrame", background=HEADER_BG)
    # 顶栏上的按钮: 底色跟着顶栏走
    style.configure("Header.TButton", background=HEADER_BG, foreground=FG,
                    bordercolor="#c3d2f2", lightcolor=HEADER_BG,
                    darkcolor=HEADER_BG, relief="solid", borderwidth=1,
                    focusthickness=0, padding=(10, 5))
    style.map("Header.TButton",
              background=[("active", "#d6e2fa"), ("disabled", HEADER_BG)],
              foreground=[("disabled", DISABLED_FG)])
    style.configure("Header.TCheckbutton", background=HEADER_BG, foreground=FG)
    style.map("Header.TCheckbutton", background=[("active", HEADER_BG)])
    style.configure("Header.TLabel", background=HEADER_BG, foreground=MUTED)

    # --- 卡片框 ---
    for prefix, bg in (("Page", BG), ("Card", SURFACE)):
        name = "%s.TLabelframe" % prefix
        style.configure(name, background=bg, bordercolor=BORDER, relief="solid",
                        borderwidth=1, labelmargins=(10, 2, 10, 2))
        style.configure("%s.Label" % name, background=bg, foreground=ACCENT_DARK,
                        font=fb, padding=(2, 0))

    # --- 输入类: 一律白底, 聚焦时边框变主色 ---
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, fieldbackground=SURFACE, background=SURFACE,
                        foreground=FG, bordercolor=BORDER, lightcolor=BORDER,
                        darkcolor=BORDER, insertcolor=FG, padding=5,
                        arrowcolor=MUTED, relief="solid", borderwidth=1)
        style.map(name,
                  bordercolor=[("focus", ACCENT), ("hover", ACCENT)],
                  lightcolor=[("focus", ACCENT), ("hover", ACCENT)],
                  darkcolor=[("focus", ACCENT), ("hover", ACCENT)],
                  fieldbackground=[("readonly", SURFACE)],
                  arrowcolor=[("active", ACCENT)])
    # 下拉列表是经典 Tk Listbox, 走 option 数据库而不是 style
    for opt, val in (("*TCombobox*Listbox.background", SURFACE),
                     ("*TCombobox*Listbox.foreground", FG),
                     ("*TCombobox*Listbox.selectBackground", ACCENT),
                     ("*TCombobox*Listbox.selectForeground", "#ffffff"),
                     ("*TCombobox*Listbox.font", f)):
        try:
            root.option_add(opt, val)
        except Exception:
            pass

    # --- 标签页 ---
    style.configure("TNotebook", background=BG, borderwidth=0,
                    tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background="#e2e6ee", foreground=MUTED,
                    font=f, padding=(20, 9), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", SURFACE), ("active", "#eaeff8")],
              foreground=[("selected", ACCENT_DARK), ("active", FG)],
              # 选中的页签向下多探 2px, 和内容区连成一片, 看不出接缝
              expand=[("selected", (0, 0, 0, 2))])

    # --- 表格 ---
    style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE,
                    foreground=FG, bordercolor=BORDER, borderwidth=1,
                    relief="solid", rowheight=26, font=f)
    style.configure("Treeview.Heading", background="#e6eaf1", foreground="#374151",
                    font=fb, relief="flat", borderwidth=0, padding=(6, 7))
    style.map("Treeview.Heading", background=[("active", "#dbe1ea")])
    style.map("Treeview",
              background=[("selected", ACCENT_SOFT)],
              foreground=[("selected", FG)])

    # --- 进度条 ---
    style.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor="#e2e6ee", bordercolor="#e2e6ee",
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=16)
    style.configure("Accent.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor="#e2e6ee", bordercolor="#e2e6ee",
                    lightcolor=ACCENT, darkcolor=ACCENT, thickness=16)

    # --- 滚动条 ---
    style.configure("Vertical.TScrollbar", background="#c8cdd6",
                    troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                    lightcolor="#c8cdd6", darkcolor="#c8cdd6", relief="flat")
    style.configure("Horizontal.TScrollbar", background="#c8cdd6",
                    troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                    lightcolor="#c8cdd6", darkcolor="#c8cdd6", relief="flat")
    style.map("Vertical.TScrollbar", background=[("active", "#aab1bc")])
    style.map("Horizontal.TScrollbar", background=[("active", "#aab1bc")])

    # 这里原本还有 "Sash" 和 "TPanedwindow" 两条样式 —— 给结果表和详解之间那条
    # 可拖动分隔条加粗、画抓手用的。分隔条随 panedwindow 一起撤了 (见文件头那段
    # 说明), 两条样式没人再用, 一并删掉。

    # --- 状态栏 ---
    # 状态栏自成一条白底带子 (Status.TFrame), 标签跟着带子同色。
    # 这样"子控件必须和父容器同色"这条规则就没有例外了, check_backgrounds
    # 只需要认识 _BAND_STYLES 里这几条带子。
    style.configure("Status.TFrame", background=SURFACE)
    style.configure("Status.TLabel", background=SURFACE, foreground=MUTED,
                    padding=(10, 5))


def unify_backgrounds(widget: Any, bg_key: str = "Page",
                      touched: Any = None) -> List[Any]:
    """给**没挑过样式**的 ttk 控件补上与所在容器匹配的那一个。

    进 ``TLabelframe`` 就切 ``Card`` (白底), 其余沿用父级。``touched`` 传入一个
    list 时顺便收集被改过的控件, 便于排查。

    **只补默认样式, 绝不覆盖已显式指定的样式。** 这一点是踩过坑的: 早先这里
    无条件 ``child.configure(style=...)``, 于是界面搭好之后跑一遍, 把所有刻意
    挑过的样式全洗掉了 —— ``Accent.TButton`` 变成 ``Page.TButton``、
    ``H1.TLabel`` 变成 ``Page.TLabel``、整条顶栏的 ``Header.*`` 也没了。
    结果界面不报任何错, 只是"美化"凭空消失, 而且越晚调用越彻底。
    判断依据用 ``_DEFAULT_STYLES``: 控件没设过样式时 ttk 报的正是类名。

    必须在界面**全部搭好之后**调用 —— 之后新建的控件不会自动被扫到。
    """
    if touched is None:
        touched = []
    for child in widget.winfo_children():
        try:
            cls = child.winfo_class()
        except Exception:
            continue
        suffix = _STYLE_SUFFIX.get(cls)
        if suffix is not None:
            try:
                cur = str(child.cget("style") or "")
            except Exception:
                cur = ""
            # 空串 = 从没设过; 等于类名 = ttk 的默认样式。两种都属于"没挑过",
            # 可以补。其余情况一律放过。
            if cur == "" or cur == _DEFAULT_STYLES.get(cls, suffix):
                name = "%s.%s" % (bg_key, suffix)
                try:
                    child.configure(style=name)
                    touched.append((child, name))
                except Exception:
                    # 有些控件类不支持 style 选项 (例如 ttk.Separator 的某些版本),
                    # 跳过即可 —— 它本来就没有背景要统一
                    pass
        nxt = "Card" if cls in _CARD_CLASSES else bg_key
        unify_backgrounds(child, nxt, touched)
    return touched


def check_backgrounds(root: Any, family: str = "") -> List[Tuple[str, str, str, str]]:
    """找出底色和父容器对不上的控件。

    返回 ``[(控件类, 实际样式, 实际底色, 期望底色), ...]``。空列表表示一致。

    这是给自检用的: ttk 控件不继承背景色, 漏配一个 ``Card.TLabel`` 就会在白色
    卡片上画出一块浅灰方块。肉眼要盯着看才能发现, 而这里一秒钟就能扫完整个
    控件树 —— 界面测试里挂上它, 以后加控件漏了样式会被立刻拦下。

    **比的是解析出来的底色, 不是样式名。** 早先这里只比对样式名 (期望
    ``Card.TLabel`` 之类), 结果是: 一遍 ``unify_backgrounds`` 把所有刻意挑过的
    样式洗成默认样式之后, 两边"名字"仍然对得上, 检查报 0 问题, 而界面上的
    主色按钮、顶栏、状态栏全没了。名字一致不代表颜色一致 —— 只有真去
    ``style.lookup`` 出底色来比, 才既抓得住"忘了统一", 也抓得住"统一时改错了"。

    唯一允许的例外是 ``_BAND_STYLES`` 里那几条带子 (顶栏、状态栏): 它们本来
    就该和父容器不同色, 而且整条子树跟着它们走。
    """
    from tkinter import ttk

    style = ttk.Style(root)
    problems: List[Tuple[str, str, str, str]] = []

    def effective(child: Any, cls: str) -> Tuple[str, str]:
        """返回 (样式名, 该样式解析出来的底色)。样式名为空时用类名兜底。"""
        suffix = _STYLE_SUFFIX.get(cls, "")
        try:
            name = str(child.cget("style") or "")
        except Exception:
            name = ""
        if not name:
            name = _DEFAULT_STYLES.get(cls, suffix)
        try:
            bg = style.lookup(name, "background")
        except Exception:
            bg = ""
        if isinstance(bg, (tuple, list)):
            bg = bg[0] if bg else ""
        return name, str(bg or "")

    def walk(w: Any, want: str) -> None:
        for child in w.winfo_children():
            try:
                cls = child.winfo_class()
            except Exception:
                continue
            mine = want
            if cls in _STYLE_SUFFIX:
                name, bg = effective(child, cls)
                if name in _BAND_STYLES:
                    # 带子: 自己换底色合法, 且后代跟着它走
                    mine = bg or want
                elif name not in _SOLID_STYLES:
                    if bg and want and bg.lower() != want.lower():
                        problems.append((cls, name, bg, want))
                if cls in _CARD_CLASSES:
                    # 卡片内部一律白底 —— 卡片自己的边框/标题栏由 Labelframe
                    # 样式画, 和它的孩子无关
                    mine = SURFACE
            walk(child, mine)

    walk(root, BG)
    return problems
