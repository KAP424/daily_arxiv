# -*- coding: utf-8 -*-
"""UI 冒烟测试: 建界面、点按钮、等线程跑完、检查结果。

不碰用户的 config.json —— 复制一份到临时目录再用。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

FAIL = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name +
          (("  <- " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAIL.append(name)


def has_flag(tree, row, flag):
    """结果表的"标记"列里**有没有**这个标记。

    必须按空格切开逐个比, 不能用 ``"推荐" in 整行`` —— 标记串里还有
    "已推荐过"、"复用解读" 这些同样含"推荐"二字的词, 子串匹配会把它们一起
    算进去 (真踩过: top_n=3 的那次被数成 5)。

    列下标按列名现查, 不写死数字 —— 列集合改过一次 (加作者/日期/期刊), 写死的
    那个 7 就指到"标记"右边去了, 而它不会报错, 只会让一堆断言莫名其妙地挂掉。
    """
    try:
        idx = list(tree["columns"]).index("flag")
    except (ValueError, TypeError):
        return False
    vals = tree.item(row, "values")
    try:
        return flag in str(vals[idx]).split()
    except Exception:
        return False


def shown(w):
    """这个控件现在是不是"摆出来了"。

    用 ``winfo_manager()`` 而不是 ``winfo_ismapped()``: 后者要等窗口真正映射
    到屏幕上才为真, 而"切标签页 -> 立刻检查"这种时序下它偶尔还是假 —— 于是
    同一份代码十次里有一次报错 (真踩过: 文献列表页那三条断言莫名挂了一次)。
    两个表是靠 pack / pack_forget 切的, 管理器名字就是确定性的答案。
    """
    try:
        return w.winfo_manager() == "pack"
    except Exception:
        return False


def cell(tree, row, col):
    """取某一列的值 (按列名)。列名不存在就返回空串。"""
    try:
        idx = list(tree["columns"]).index(col)
    except (ValueError, TypeError):
        return ""
    try:
        return tree.item(row, "values")[idx]
    except Exception:
        return ""


# ---- 准备一份隔离的配置 ----
tmpdir = tempfile.mkdtemp(prefix="daily_arxiv_uitest_")
cfg_src = os.path.join(ROOT, "config.json")
cfg_dst = os.path.join(tmpdir, "config.json")
shutil.copy(cfg_src, cfg_dst)
raw = json.load(io.open(cfg_dst, encoding="utf-8"))
raw.setdefault("output", {})["dir"] = os.path.join(tmpdir, "output")
raw.setdefault("library", {})["index_db"] = os.path.join(tmpdir, "idx.sqlite")
# 推荐记录也要指到临时目录: 相对路径按**程序所在目录**解析, 不写绝对路径的话
# 这个测试会往用户真正的推荐记录库里写 (真踩过 —— 推荐次数被测试刷到十几)
raw.setdefault("analysis", {})["history_db"] = os.path.join(tmpdir, "history.sqlite")
json.dump(raw, io.open(cfg_dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

import tkinter as tk
from tkinter import ttk
from arxiv_rec.ui import App, REC_LIVE, REC_ALL_PREFIX
from arxiv_rec.utils import setup_console

setup_console()

print("=" * 70)
print("A) 界面构建")
print("=" * 70)
app = App(cfg_dst)
app.update()

tabs = [app.nb.tab(i, "text").strip() for i in range(app.nb.index("end"))]
check("五个标签页", len(tabs) == 5, tabs)
check("标签页名称",
      tabs == ["1. 读取文献", "2. 文献推荐", "3. 文献列表", "4. 设置",
               "5. 支持一下"], tabs)


def all_widgets(w):
    out = [w]
    for c in w.winfo_children():
        out.extend(all_widgets(c))
    return out


def labels_of(w):
    out = []
    for x in all_widgets(w):
        try:
            t = x.cget("text")
        except Exception:
            continue
        if t:
            out.append(str(t).strip())
    return out


read_labels = labels_of(app.tab_read)
rec_labels = labels_of(app.tab_rec)
list_labels = labels_of(app.tab_list)
cfg_labels = labels_of(app.tab_cfg)
sup_labels = labels_of(app.tab_support)
hdr_labels = labels_of(app)

# 用户点名要的三件事
check("1.读取文献: 有'开始读取'", "开始读取" in read_labels)
check("1.读取文献: 有'全部重读 (忽略索引)'", "全部重读 (忽略索引)" in read_labels)
check("1.读取文献: 有'查看已读记录'", "查看已读记录" in read_labels)
check("1.读取文献: 有'清空索引'", "清空索引" in read_labels)
check("1.读取文献: 有 PDF 文件夹编辑器",
      any("添加文件夹" in s for s in read_labels))
check("2.文献推荐: 有'开始推荐'", "开始推荐" in rec_labels)
check("2.文献推荐: 有'停止'", "停止" in rec_labels)
check("2.文献推荐: 有'打开报告'", "打开报告" in rec_labels)
check("2.文献推荐: 也有'读取深度'选择器", "读取深度" in rec_labels)
check("3.文献列表: 有搜索框", "搜索" in list_labels)
check("3.文献列表: 有'刷新列表'", "刷新列表" in list_labels)
check("3.文献列表: 有'打开 PDF'", "打开 PDF" in list_labels)
check("3.文献列表: 有'打开所在文件夹'", "打开所在文件夹" in list_labels)
check("3.文献列表: 有'编辑标签'", "编辑标签" in list_labels)
check("3.文献列表: 有'清空标签'", "清空标签" in list_labels)
check("4.设置: 有'保存设置'", "保存设置" in cfg_labels)
check("4.设置: 有'重新载入'", "重新载入" in cfg_labels)
check("4.设置: 有'测试 AI 连接'", "测试 AI 连接" in cfg_labels)
check("4.设置: 有'测试 arXiv 连接'", "测试 arXiv 连接" in cfg_labels)
# 文献路径只在"读取文献"页有一份。设置页原来也摆了一份一模一样的编辑器,
# 两份编的是同一个列表, 用户拿不准哪份算数 —— 已撤掉。
check("1.读取文献: 有文献路径编辑器",
      any("添加文件夹" in s for s in read_labels))
check("4.设置: 不再有文献路径编辑器 (只在读取页)",
      not any("添加文件夹" in s for s in cfg_labels),
      [s for s in cfg_labels if "文件夹" in s])
# 用户点名要能配的三个路径: 读取记录 / 推荐记录 / 报告输出目录
check("4.设置: 有'记录与输出'分组",
      any("记录与输出" in s for s in cfg_labels), cfg_labels)
for _lbl in ("读取记录", "推荐记录", "报告目录"):
    check("4.设置: 有 %s 路径行" % _lbl, _lbl in cfg_labels)
check("4.设置: 有'使用推荐记录'开关", "使用推荐记录" in cfg_labels)
check("4.设置: 有'推荐时跳过以前推荐过的'开关",
      "推荐时跳过以前推荐过的" in cfg_labels)
check("4.设置: 有'打开报告目录'", "打开报告目录" in cfg_labels)
check("4.设置: 有'打开记录所在目录'", "打开记录所在目录" in cfg_labels)
check("标题栏: 有'详细日志'", "详细日志" in hdr_labels)

# 三档读取深度: 只有读取页和推荐页各一份, 都绑在 app.var_depth 上。
# 设置页**不该**再有 —— 它是"这次怎么读/怎么推", 不是全局开关, 摆两份以上
# 反而让人以为是独立设置。
from arxiv_rec.pdf_library import DEPTH_LABELS
check("1.读取文献: 有'读取深度'选择器", "读取深度" in read_labels)
check("4.设置: 不再有'读取深度'选择器 (已挪走)",
      "读取深度" not in cfg_labels, [s for s in cfg_labels if "深度" in s])
for _lbl in DEPTH_LABELS.values():
    check("读取页列出档位 %s" % _lbl, _lbl in read_labels)
    check("推荐页列出档位 %s" % _lbl, _lbl in rec_labels)
check("两处共用同一个变量", app.var_depth.get() in DEPTH_LABELS,
      app.var_depth.get())
check("两个档位说明标签都建好了", len(app._depth_labels) == 2,
      len(app._depth_labels))

# 文件夹编辑器只剩一个了。设置页那个属性**不该**再存在 —— 留着它 (哪怕不
# pack) 就等于留了个没人刷新的死视图, 以后谁再往里写点东西会静默失效。
check("读取页那个文件夹编辑器在, 且能看到配置里的列表",
      app.folder_editor_read.folders() is app.cfg.get("pdf_folders"),
      len(app.folder_editor_read.folders()))
check("设置页那个文件夹编辑器已经不存在了",
      not hasattr(app, "folder_editor_cfg"))

# "读取全文"复选框撤了 —— 它和"读取深度"是同一件事的两个开关, 取消勾选会
# 覆盖深度选择。推荐页只留深度单选框。
#
# 上面两条和下面这条都是**否定断言** (断言某个字符串不在列表里), 这种断言最容易
# 变成"永远为真": 只要 labels_of 因为任何原因没收集到那一行, 它就自动通过, 而
# 控件其实还在。所以先把**同一行里还在的那几个复选框**断言一遍 —— 它们能被收集
# 到, 说明收集机制确实看得见那一行, "读取全文不在里面"才是句有约束力的话。
# (设置页那条同理, 它的对照组是上面已经断言过的 "保存设置" in cfg_labels。)
for _sib in ("使用 AI", "补充引用数", "跳过已推荐过的"):
    check("推荐页参数行里的 %s 能被收集到 (于是否定断言才有约束力)" % _sib,
          _sib in rec_labels, rec_labels)
check("2.文献推荐: 不再有'读取全文'复选框 (深度单选框才是唯一说法)",
      "读取全文" not in rec_labels,
      [s for s in rec_labels if "全文" in s])
check("推荐页那个 var_fulltext 变量也没了", not hasattr(app, "var_fulltext"))

# --- 5. 支持一下 ---------------------------------------------------------
# 用户点名要的: 一页写"如果这个项目帮到了你, 欢迎通过微信赞赏我", 并放上赞赏码。
# 图片这一格最容易**假通过**: 页面搭好了、文字都在, 只是图片没显示出来 (Tk 读
# 不了 JPG、或者图片文件没跟着 exe 走), 而程序不报任何错 —— 所以这里既查
# "加载成功", 也查"真的挂到控件上了", 两件事是分开的。
check("5.支持一下: 有英文那句原文",
      any("If you find this project helpful" in s for s in sup_labels), sup_labels)
check("5.支持一下: 有'微信赞赏码'卡片", "微信赞赏码" in sup_labels, sup_labels)
check("5.支持一下: 有'用微信「扫一扫」即可'那行说明",
      any("用微信「扫一扫」即可" in s for s in sup_labels), sup_labels)
# 下面三条是**否定断言** (断言某个东西不在页面上), 这种断言最容易变成"永远为真":
# 只要 labels_of 因为任何原因没收集到那一行, 它就自动通过。上面那三条正着断言的
# 文字 (英文那句 / 卡片标题 / 扫一扫那行) 就是对照组 —— 它们能被收集到, 说明收集
# 机制确实看得见这一页, "按钮不在里面"才是句有约束力的话。
check("5.支持一下: 没有'打开赞赏码'按钮 (用户要求去掉)",
      not any("打开赞赏码" in s for s in sup_labels),
      [s for s in sup_labels if "赞赏码" in s])
check("5.支持一下: 没有'打开程序目录'按钮 (用户要求去掉)",
      not any("打开程序目录" in s for s in sup_labels),
      [s for s in sup_labels if "程序目录" in s])
check("5.支持一下: 不显示图片路径 (用户要求去掉)",
      not any(s.startswith("图片:") for s in sup_labels),
      [s for s in sup_labels if "图片" in s])
check("5.支持一下: 没有'业余时间写的…免费、开源'那段 (用户要求去掉)",
      not any("业余时间写的" in s for s in sup_labels),
      [s for s in sup_labels if "业余" in s or "开源" in s])


def _image_of(w):
    """控件上挂的图片名 (没有就是空串)。取不到一律当没有。"""
    try:
        return str(w.cget("image") or "")
    except Exception:
        return ""


_sup_img = getattr(app, "_support_photo", None)
check("5.支持一下: 赞赏码图片加载成功 (Tk 只认 PNG/GIF, 所以要有一份 PNG)",
      _sup_img is not None, app._support_image_path())
if _sup_img is not None:
    check("5.支持一下: 图片有实际尺寸 (不是 0x0 的空图)",
          _sup_img.width() > 100 and _sup_img.height() > 100,
          "%sx%s" % (_sup_img.width(), _sup_img.height()))
check("5.支持一下: 图片挂在控件上真的显示出来 (不是只加载了没放上去)",
      any(_image_of(w) for w in all_widgets(app.tab_support)),
      [w.winfo_class() for w in all_widgets(app.tab_support)])
check("赞赏码 PNG 在仓库里 (打包时要带上它)",
      os.path.exists(os.path.join(
          os.path.dirname(os.path.abspath(__file__)), "reward.png")),
      app._support_image_path())

# cfg 里 pdf_folders 被手改成一个非列表 (最常见的是 null) 时, folders() 必须
# 兜底成列表。以前它是 `setdefault(...)`, 而 setdefault **不替换**已存在的
# None —— refresh() 里的 for 直接 TypeError, 界面起不来。
_saved_folders = app.cfg["pdf_folders"]
try:
    app.cfg["pdf_folders"] = None
    _got = app.folder_editor_read.folders()
    check("cfg 里 pdf_folders 是 None 时 folders() 兜底成列表, 不炸",
          isinstance(_got, list) and app.cfg["pdf_folders"] is _got, repr(_got))
finally:
    app.cfg["pdf_folders"] = _saved_folders
check("还原之后 folders() 还是原来那个活列表",
      app.folder_editor_read.folders() is _saved_folders)

# 读取页顶上那行"文献来源: … · 读取深度: X"读的是**单选框的当前值**, 不是 cfg
# 里存着的旧值 —— 否则改了深度要等保存才更新, 同一页上两个说法打架。
from arxiv_rec.pdf_library import normalize_depth
_d0 = app.var_depth.get()
app.var_depth.set("fulltext")
app._on_depth_change()
check("改了深度, 读取页顶上的来源行立刻跟着变 (不用等保存)",
      app.var_source.get().rstrip().endswith("读取深度: " + DEPTH_LABELS["fulltext"]),
      app.var_source.get())
app.var_depth.set(_d0)
app._on_depth_change()
check("改回原档位, 那行也跟着回去",
      app.var_source.get().rstrip().endswith(
          "读取深度: " + DEPTH_LABELS[normalize_depth(_d0)]),
      app.var_source.get())

print()
print("=" * 70)
print("A2) 配色 / 样式")
print("=" * 70)
from arxiv_rec import theme

# 1) 每个控件的底色都必须和所在容器一致。
#    ttk 控件不继承背景色, 漏配一个 Card.TLabel 就会在白色卡片上画出一块
#    浅灰方块 —— 这种错肉眼要盯着看才发现, 这里一秒扫完整个控件树。
#    注意它比的是 **解析出来的底色**, 不是样式名: 只比名字的话,
#    一遍"把样式统一成默认值"就能让名字全部对上而颜色全错 (真踩过)。
_probs = theme.check_backgrounds(app)
check("所有控件底色和容器一致", not _probs,
      "; ".join("%s/%s %s!=%s" % p for p in _probs[:6]))

# 2) 刻意挑过的样式不能被 unify_backgrounds 洗掉。
#    同理, 这是"美化整体消失但程序不报错"的唯一预警 —— 少了它, 主色按钮、
#    顶栏、状态栏全都会静默退回默认灰, 测试却依然全绿。
_styles_used = set()
for _w in all_widgets(app):
    try:
        _s = str(_w.cget("style") or "")
    except Exception:
        continue
    if _s:
        _styles_used.add(_s)
for _need in ("Accent.TButton", "Quiet.TButton", "H1.TLabel", "Header.TFrame",
              "Header.TLabel", "Header.TButton", "Status.TFrame", "Status.TLabel",
              "Muted.TLabel", "CardMuted.TLabel", "CardH2.TLabel"):
    check("样式 %s 还在用" % _need, _need in _styles_used)

# 3) 两条"带子"必须真的配了底色。漏配 Header.TFrame 会让 lookup 退到基类的
#    页面灰, 顶栏根本不变色, 而顶栏里的控件全是 HEADER_BG —— 浅蓝控件摆在
#    灰底上, 比不美化还难看。
_st = ttk.Style(app)
check("顶栏底色 = theme.HEADER_BG",
      str(_st.lookup("Header.TFrame", "background")).lower() == theme.HEADER_BG.lower(),
      _st.lookup("Header.TFrame", "background"))
check("状态栏底色 = theme.SURFACE",
      str(_st.lookup("Status.TFrame", "background")).lower() == theme.SURFACE.lower(),
      _st.lookup("Status.TFrame", "background"))
check("用的是可配置的 clam 主题 (vista 会吃掉大部分配色)",
      str(_st.theme_use()) == "clam", _st.theme_use())

print()
print("=" * 70)
print("A3) 推荐结果表: 论文名 / 作者 / 提交日期 / 期刊 / 总分 / 标记")
print("=" * 70)
_cols = list(app.tree["columns"])
for _c, _name in (("title", "论文名"), ("authors", "作者"), ("pub", "提交日期"),
                  ("journal", "期刊"), ("score", "总分"), ("flag", "标记")):
    check("结果表有 %s 列" % _name, _c in _cols, _cols)
_heads = [app.tree.heading(c, "text") for c in _cols]
check("表头依次是 # / 论文名 / 作者 / 提交日期 / 期刊 / 总分 / 标记",
      _heads == ["#", "论文名", "作者", "提交日期", "期刊", "总分", "标记"],
      _heads)
# 列变多了, 窄窗口下右边几列会出可视区 —— 没有横向滚动条就只能靠拖边框猜
_hbars = [w for w in app.tree.master.winfo_children()
          if isinstance(w, ttk.Scrollbar) and str(w.cget("orient")) == "horizontal"]
check("结果表配了横向滚动条", len(_hbars) == 1, len(_hbars))

# 铺行的逻辑 (_fill_tree) 跟抓没抓到论文无关, 所以这里造两篇假论文离线验。
from datetime import datetime as _dt
from arxiv_rec.models import Candidate as _Cand


def _mk(aid, title, authors, pub, journal, score, **kw):
    c = _Cand(arxiv_id=aid, title=title, authors=authors, score=score)
    c.published = pub
    c.journal_ref = journal
    for k, v in kw.items():
        setattr(c, k, v)
    return c


_off = [
    _mk("2401.00001", "Majorana Positivity and the Sign Problem",
        ["Wei Wang", "Li Chen"], _dt(2026, 9, 25), "Phys. Rev. Lett. 137, 010601",
        0.788),
    _mk("2401.00002", "Reformulating the Pfaffian Sign", ["Bo Zhang"],
        _dt(2026, 9, 24), "", 0.770, seen_before=True),
    _mk("2401.00003", "A Paper With No Date", [], None, "", 0.1),
]
app._fill_tree(_off, {"2401.00001"})
_rows = app.tree.get_children()
check("_fill_tree 铺出 3 行", len(_rows) == 3, len(_rows))
# Treeview 取回来的值一律是字符串, 跟 "1" 比而不是跟 1 比
check("第一列是序号", cell(app.tree, _rows[0], "rank") == "1",
      repr(cell(app.tree, _rows[0], "rank")))
check("论文名列就是标题",
      "Majorana Positivity" in str(cell(app.tree, _rows[0], "title")),
      cell(app.tree, _rows[0], "title"))
check("作者列只列第一位 + 等", cell(app.tree, _rows[0], "authors") == "Wei Wang 等",
      cell(app.tree, _rows[0], "authors"))
check("单作者不加'等'", cell(app.tree, _rows[1], "authors") == "Bo Zhang",
      cell(app.tree, _rows[1], "authors"))
check("提交日期列是 v1 提交日 (不是最后修订日)",
      cell(app.tree, _rows[0], "pub") == "2026-09-25",
      cell(app.tree, _rows[0], "pub"))
check("没有日期时写破折号而不是空白",
      cell(app.tree, _rows[2], "pub") == "—", repr(cell(app.tree, _rows[2], "pub")))
check("期刊列显示期刊简称",
      "Phys. Rev. Lett." in str(cell(app.tree, _rows[0], "journal")),
      cell(app.tree, _rows[0], "journal"))
check("没发表过的期刊列写破折号",
      cell(app.tree, _rows[1], "journal") == "—",
      repr(cell(app.tree, _rows[1], "journal")))
check("总分列保留三位小数", cell(app.tree, _rows[0], "score") == "0.788",
      cell(app.tree, _rows[0], "score"))
check("标记列照旧: 推荐", has_flag(app.tree, _rows[0], "推荐"))
check("标记列照旧: 已推荐过", has_flag(app.tree, _rows[1], "已推荐过"))
check("没被推荐的没有'推荐'标记", not has_flag(app.tree, _rows[1], "推荐"))
check("再铺一次是重建不是追加",
      len(app.tree.get_children()) == 3, len(app.tree.get_children()))
app._fill_tree([], {})
check("_fill_tree 传空列表能把表清干净",
      len(app.tree.get_children()) == 0, len(app.tree.get_children()))

print()
print("=" * 70)
print("A4) 推荐记录: 铺列表 (只读), 删除是另一个按钮")
print("=" * 70)

# 这两件事刻意分开: 「推荐记录」下拉框 (这里走的是"全部累计记录"那条路) = 把库里的
# 记录铺到下面那张表里看; 「删除推荐记录」= 清空。以前它们挤在同一个按钮上 (看完
# 弹窗, 关掉前问一句要不要清空) —— 想翻一眼记录的人每次都得绕过一次删除确认。
# 下拉框本身 (选哪一轮) 由 A6 那一组管。
for _sib in ("开始推荐", "打开报告"):
    check("推荐页按钮行里的 %s 能被收集到 (于是下面两条有约束力)" % _sib,
          _sib in rec_labels, rec_labels)
check("推荐页有'推荐记录'按钮", "推荐记录" in rec_labels)
check("推荐页另有独立的'删除推荐记录'按钮", "删除推荐记录" in rec_labels)
check("有 load_history 方法 (铺列表那件事)", hasattr(app, "load_history"))
check("有 clear_history 方法 (删除那件事)", hasattr(app, "clear_history"))
check("旧的 show_history 已经不存在 (它自带删除确认, 正是要去掉的行为)",
      not hasattr(app, "show_history"))

# 造一份 **v1 结构** 的库 (没有 authors/published/journal/categories/citations
# 这五列) —— 用户手上那份就是这个结构, 顺带在界面这条路上验一遍迁移。
_hdb = app.var_history_db.get()
check("测试用的推荐记录库在临时目录 (不碰用户那份)",
      os.path.abspath(_hdb).startswith(os.path.abspath(tmpdir)), _hdb)
import sqlite3 as _sq
_con = _sq.connect(_hdb)
_con.executescript("""
CREATE TABLE recommended (
    arxiv_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    first_at TEXT NOT NULL DEFAULT '', last_at TEXT NOT NULL DEFAULT '',
    times INTEGER NOT NULL DEFAULT 0, best_score REAL NOT NULL DEFAULT 0,
    report TEXT NOT NULL DEFAULT '', profile_fp TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '', connections TEXT NOT NULL DEFAULT '[]',
    ideas TEXT NOT NULL DEFAULT '', analyzed INTEGER NOT NULL DEFAULT 0);
""")
_con.executemany(
    "INSERT INTO recommended (arxiv_id, title, first_at, last_at, times,"
    " best_score, report, profile_fp, summary, connections, ideas, analyzed)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
    [("2401.11111", "记录里的第一篇", "2026-09-20 10:00:00",
      "2026-09-29 14:59:16", 4, 0.9, "", "fp",
      "这一篇的内容讲解还在记录里。", "[]", "想法", 1),
     ("2401.22222", "记录里的第二篇", "2026-09-21 10:00:00",
      "2026-09-29 14:59:16", 1, 0.7, "", "", "", "[]", "", 0),
     ("2401.33333", "记录里的第三篇", "2026-09-22 10:00:00",
      "2026-09-28 09:00:00", 2, 0.5, "", "fp", "讲解", "[]", "", 1)])
_con.commit()
_con.close()

# 弹窗全部拦下来: 既不让测试卡在模态框上, 也顺便验证"点这个按钮到底问了没有"。
from arxiv_rec import ui as _ui
_real_ask = _ui.messagebox.askyesno
_real_info = _ui.messagebox.showinfo
_real_err = _ui.messagebox.showerror
_dlg = {"ask": 0, "info": 0, "err": 0, "answer": False, "text": "", "info_text": ""}


def _fake_ask(title, msg, *a, **k):
    _dlg["ask"] += 1
    _dlg["text"] = "%s\n%s" % (title, msg)
    return _dlg["answer"]


def _fake_info(title, msg, *a, **k):
    _dlg["info"] += 1
    _dlg["info_text"] = "%s\n%s" % (title, msg)


def _fake_err(title, msg, *a, **k):
    _dlg["err"] += 1


_ui.messagebox.askyesno = _fake_ask
_ui.messagebox.showinfo = _fake_info
_ui.messagebox.showerror = _fake_err


def _db_count():
    c = _sq.connect(_hdb)
    try:
        return c.execute("SELECT COUNT(*) FROM recommended").fetchone()[0]
    finally:
        c.close()


try:
    app.var_use_history.set(True)
    _dlg["ask"] = 0
    app.load_history()
    check("'推荐记录'点一下就问了 0 次确认 (打开记录不等于删除)", _dlg["ask"] == 0,
          _dlg["ask"])
    check("库里的 3 条一条没少", _db_count() == 3, _db_count())
    _rows = app.tree.get_children()
    check("记录被铺进了下面那张列表", len(_rows) == 3, len(_rows))
    check("列表内容来自记录 (不是这一轮的结果)",
          "记录里的第一篇" in str(cell(app.tree, _rows[0], "title")),
          cell(app.tree, _rows[0], "title"))
    check("铺的是真 Candidate (选中/双击/配色才能照旧用)",
          len(app.ranked) == 3 and all(c.from_history for c in app.ranked),
          len(app.ranked))
    check("列表标记成了'这一张表来自记录库'",
          app._tree_from_history is True)
    check("分数列显示的是记录里的 best_score",
          cell(app.tree, _rows[0], "score") == "0.900",
          cell(app.tree, _rows[0], "score"))
    # 记录库里每一条都是推荐过的, 全写"已推荐过"等于整列同义反复 —— 这里给的是
    # 次数。注意"推荐 4 次"中间有空格, has_flag 是按空格切的, 所以直接比文本。
    check("标记列写的是推荐过几次 (不是清一色的'已推荐过')",
          "推荐 4 次" in cell(app.tree, _rows[0], "flag"),
          cell(app.tree, _rows[0], "flag"))
    check("'已推荐过'那套说法只在实时结果里用",
          "已推荐过" not in cell(app.tree, _rows[0], "flag"),
          cell(app.tree, _rows[0], "flag"))
    check("第一条被自动选中并填进详解", app.tree.selection() == (_rows[0],),
          app.tree.selection())
    _det = app.txt_detail.get("1.0", "end")
    check("详解里说明了这是记录 (推荐过几次)", "记录: 推荐过 4 次" in _det,
          _det[:200])
    # 那三个分数**没有存进记录库**, 照原样列出来就是三个 0.00, 等于报假数据。
    # 同样是否定断言 —— 先把同一段里还在的那行摆出来当对照, 免得它自动通过。
    check("详解里仍然给出了内容讲解 (于是下面那条否定断言有约束力)",
          "内容讲解" in _det and "这一篇的内容讲解还在记录里。" in _det)
    check("详解里不再列'相关 0.00 / 时效 / 重要' (那三个分数没存进库)",
          "相关 0.00" not in _det and "重要 0.00" not in _det,
          [ln for ln in _det.splitlines() if "相关" in ln])
    check("状态栏说明这张表是记录库来的",
          "记录库" in app.var_status.get(), app.var_status.get())

    # 记录库只增不减 (一天一轮, 一年几千条), 所以铺的时候有个上限 —— 但截断了
    # 必须**说出来**, 不能让人以为记录只有这些。
    _cap0 = app.HISTORY_MAX_ROWS
    try:
        app.HISTORY_MAX_ROWS = 2
        app.load_history()
        check("记录超过上限时只铺最近的一批",
              len(app.tree.get_children()) == 2, len(app.tree.get_children()))
        check("而且明确写出'只铺了最近 N 篇' (不是悄悄截断)",
              "只铺了最近 2 篇" in app.var_status.get(), app.var_status.get())
        check("上限只管显示, 库里的 3 条一条没少", _db_count() == 3, _db_count())
    finally:
        app.HISTORY_MAX_ROWS = _cap0
        app.load_history()
    check("恢复上限之后又铺满 3 条", len(app.tree.get_children()) == 3,
          len(app.tree.get_children()))

    # --- 删除: 先答"否" ---
    _dlg["answer"] = False
    _dlg["ask"] = 0
    app.clear_history()
    check("'删除推荐记录'先弹确认", _dlg["ask"] == 1, _dlg["ask"])
    check("确认框标题是'删除推荐记录'",
          _dlg["text"].startswith("删除推荐记录"), _dlg["text"][:40])
    check("确认框写清了范围 (不动报告)", "不会删除报告" in _dlg["text"])
    check("确认框写清了范围 (不动文献索引)", "文献索引" in _dlg["text"])
    check("答'否'时一条都没删", _db_count() == 3, _db_count())
    check("答'否'时列表也还在", len(app.tree.get_children()) == 3,
          len(app.tree.get_children()))

    # --- 删除: 答"是" ---
    _dlg["answer"] = True
    _dlg["ask"] = 0
    app.clear_history()
    check("答'是'之后库被清空", _db_count() == 0, _db_count())
    check("清空之后列表也擦掉了 (留着会让人以为记录还在)",
          len(app.tree.get_children()) == 0, len(app.tree.get_children()))
    check("清空之后 app.ranked 也清了", app.ranked == [], len(app.ranked))
    check("清空之后详解框也空了",
          app.txt_detail.get("1.0", "end").strip() == "",
          repr(app.txt_detail.get("1.0", "end")[:60]))

    # 一条"有记录但都没解读"的库: describe_history 会说"1 篇 (其中 0 篇存有解读)",
    # 那句话里**含 "0 篇"**。要是拿这个子串去判空, 这种库会被当成空库 —— 于是点
    # 「删除推荐记录」静默什么都不做, 而用户以为删掉了。这条就是钉住那个坑。
    from arxiv_rec.history import RecommendHistory as _RH
    from arxiv_rec.models import Candidate as _C2
    from arxiv_rec.history import describe_history as _dh
    with _RH(_hdb) as _h:
        _h.record([_C2(arxiv_id="2401.77777", title="有记录但没解读")],
                  profile_fp="")
    check("这种库的说明文字里确实含 '0 篇' (否则下面那条测不到真东西)",
          "0 篇" in _dh(app.cfg), _dh(app.cfg))
    _dlg["answer"] = True
    _dlg["ask"] = 0
    app.clear_history()
    check("有记录但都没解读时, 删除按钮照样弹确认并真的删掉",
          _dlg["ask"] == 1 and _db_count() == 0, (_dlg["ask"], _db_count()))

    # 清空的是"记录", 不是"屏幕上这一轮的结果" —— 两码事。
    # 库里先放一条, 否则会走"本来就是空的"那个提前返回, 这条断言就成了空断言。
    with _RH(_hdb) as _h:
        _h.record([_C2(arxiv_id="2401.44444", title="清空之前记的")],
                  profile_fp="fp")
    app._tree_from_history = False
    app.ranked = _off
    app._fill_tree(_off, {"2401.00001"})
    _dlg["answer"] = True
    _dlg["ask"] = 0
    app.clear_history()
    check("列表上铺的是这一轮结果时, 清空记录照常执行 (库里确实有东西可删)",
          _dlg["ask"] == 1 and _db_count() == 0, (_dlg["ask"], _db_count()))
    check("但不会把屏幕上这一轮的结果也擦掉",
          len(app.tree.get_children()) == 3, len(app.tree.get_children()))
    app._fill_tree([], {})
    app.ranked = []
    app._tree_from_history = False

    # --- 空库 / 关掉时 ---
    _dlg["info"] = 0
    _dlg["info_text"] = ""
    app.load_history()
    check("空库时给一句解释 (而不是铺一张空表让人猜)", _dlg["info"] == 1)
    check("那句解释点出了'最后一步才写记录'", "最后一步" in _dlg["info_text"],
          _dlg["info_text"][:200])
    check("空库时不铺列表", len(app.tree.get_children()) == 0)
    # 空库时下拉框会退回「本次运行的结果」(不然它停在一个已经没东西可铺的选项
    # 上, 而下面那张表是别的来源)。状态栏说的是**屏幕上这张表**的状态, 所以它
    # 讲的是"这一轮还没跑过", 而不是"推荐记录是空的" —— 后者那句在弹窗里。
    check("空库时退回'本次运行的结果'",
          app.var_rec_run.get() == REC_LIVE, app.var_rec_run.get())
    check("空库时状态栏讲的是这一轮的结果 (屏幕上铺的就是它)",
          "还没有跑过推荐" in app.var_status.get(), app.var_status.get())

    # 空库时点删除: 不该再弹一次"确定要清空吗" (本来就空, 问了也白问)
    _dlg["info"] = 0
    _dlg["ask"] = 0
    app.clear_history()
    check("库是空的时点删除: 直接说'本来就是空的', 不弹确认",
          _dlg["info"] == 1 and _dlg["ask"] == 0, (_dlg["info"], _dlg["ask"]))

    # 看记录必须是**只读**的: 库文件不存在时, 点一下不该在盘上凭空建出一个来
    _nowhere = os.path.join(tmpdir, "还没有建的库.sqlite")
    from arxiv_rec.history import load_records as _lr0
    _empty0 = _lr0({"analysis": {"history_db": _nowhere, "use_history": True}})
    check("库文件不存在时读记录给空列表", _empty0 == [], _empty0)
    check("而且不会顺手把库文件建出来 (看记录是只读的)",
          not os.path.exists(_nowhere))

    app.var_use_history.set(False)
    _dlg["info"] = 0
    _dlg["info_text"] = ""
    app.load_history()
    check("关掉'使用推荐记录'时点它: 说明为什么没得看", _dlg["info"] == 1)
    check("那句话指向设置页那个开关", "使用推荐记录" in _dlg["info_text"],
          _dlg["info_text"][:200])
    _dlg["info"] = 0
    _dlg["ask"] = 0
    app.clear_history()
    check("关掉时点删除: 只说没东西可删, 不会真去删",
          _dlg["info"] == 1 and _dlg["ask"] == 0 and _db_count() == 0,
          (_dlg["info"], _dlg["ask"]))
    app.var_use_history.set(True)

    # 删完之后还能接着记 (clear 不该把库搞成只读/损坏)
    from arxiv_rec.history import load_records as _lr
    with _RH(_hdb) as _h:
        _h.record([_C2(arxiv_id="2401.44444", title="清空之后新记的")],
                  profile_fp="fp")
    _after = _lr({"analysis": {"history_db": _hdb, "use_history": True}})
    check("清空之后还能接着记 (库没被搞坏)",
          len(_after) == 1 and _after[0].title == "清空之后新记的",
          [c.title for c in _after])
finally:
    _ui.messagebox.askyesno = _real_ask
    _ui.messagebox.showinfo = _real_info
    _ui.messagebox.showerror = _real_err
    # 后面几节 (B/C/D) 还要用这份库: 留一条干净可用的记录, 别把状态带过去
    _c = _sq.connect(_hdb)
    _c.execute("DELETE FROM recommended")
    _c.commit()
    _c.close()
    app.ranked = []
    app._tree_from_history = False
    app._fill_tree([], {})

print()
print("=" * 70)
print("A5) 详解整段铺开 (自己不滚), 日志不横滚, 滚轮归整页, Ctrl+滚轮归最外侧")
print("=" * 70)

# 用户的原话: "运行日志那不需要左右滚轮" + "详解那不需要单独设置滚轮, 直接显示
# 全部, 用文献推荐页面的滚轮进行查看"。这一节验的就是这三件事。
#
# **必须先切到推荐页再量**: 没被选中的标签页里, 控件是未映射的, winfo_height()
# 一律报 1, 而 Text.count("displaylines") 在宽度为 1 时给的是垃圾值 —— 量出来的
# "详解几行"完全没有意义 (真踩过)。
app.nb.select(app.tab_rec)
for _ in range(6):
    app.update()

# --- 1. 运行日志: 没有横向滚动条, 而且真的会折行 ---
check("运行日志框 wrap=word (长路径会折行, 而不是撑出一条横条)",
      str(app.txt_run_log.cget("wrap")) == "word",
      app.txt_run_log.cget("wrap"))
check("运行日志框没有横向滚动条",
      not any(isinstance(w, ttk.Scrollbar) and str(w.cget("orient")) == "horizontal"
              for w in app.txt_run_log.master.winfo_children()),
      [str(w) for w in app.txt_run_log.master.winfo_children()])
check("运行日志框保留了纵向滚动条 (日志会一直往下长)",
      any(isinstance(w, ttk.Scrollbar) and str(w.cget("orient")) == "vertical"
          for w in app.txt_run_log.master.winfo_children()))

# --- 2. 详解: 自己不滚动, 高度按内容撑开 ---
check("详解框 wrap=word", str(app.txt_detail.cget("wrap")) == "word",
      app.txt_detail.cget("wrap"))
check("详解框没有自己的滚动条 (滚它就该滚整页)",
      not any(isinstance(w, (ttk.Scrollbar, tk.Scrollbar))
              for w in app.det_detail.winfo_children()),
      [str(w) for w in app.det_detail.winfo_children()])
check("详解的标题不再写'可以拖' (没有分隔条了)",
      "拖" not in str(app.det_detail.cget("text")), app.det_detail.cget("text"))

_cv5 = None
for _w5 in all_widgets(app.tab_rec):
    if isinstance(_w5, tk.Canvas):
        _cv5 = _w5
        break
check("推荐页里有滚动容器", _cv5 is not None)
_inner5 = None
if _cv5 is not None:
    _inner5 = _cv5.nametowidget(_cv5.itemcget(_cv5.find_all()[0], "window"))
    check("内层 Frame 被顶到视口高度 (多余高度才分得下去)",
          _inner5.winfo_height() >= _cv5.winfo_height() - 2,
          "inner=%d canvas=%d" % (_inner5.winfo_height(), _cv5.winfo_height()))
check("日志框不再抢这块高度 (expand=0)",
      int(app.txt_run_log.master.pack_info().get("expand", 0)) == 0,
      app.txt_run_log.master.pack_info().get("expand"))

# 造一篇"解读很长"的论文铺进去 —— 长度可控, 才能拿高度做比较
from arxiv_rec.models import Candidate as _Cand5

def _put_detail(para_lines, words=12):
    """铺一篇详解长度可控的论文。``words`` 是每个段落的"词数" —— 段落越长,
    窗口变窄时折出来的行数才越多, 拿它验"宽度变了要重算行数"。"""
    c = _Cand5(arxiv_id="2609.00001", title="测试: 详解自动撑高")
    c.authors = ["Zhang San", "Li Si"]
    c.abstract = ("摘要段落 " * words + "\n") * max(1, para_lines // 2)
    c.summary = ("内容讲解 " * words + "\n") * max(1, para_lines // 2)
    c.analyzed = True
    c.relevance, c.recency, c.importance = 0.5, 0.5, 0.5
    app.ranked = [c]
    app._tree_from_history = False
    app._fill_tree(app.ranked, set())
    app.tree.selection_set("1")
    app._show_detail(c)
    # 定高不是一拍的事, 顺序是: 估高度 -> 请求高度落到内层 -> 顶高窗口项 -> 控件
    # 实际变高 -> 才轮到按 yview 补。每一拍都是一次 after_idle 空闲回调, 一次
    # update() 正好跑一拍。给 12 拍是留余量: 6 拍在老写法下够, 但中间那几步是
    # "一拍只做一件事", 少了就会量到半路的高度 (量出来比真实值矮, 断言会误报)。
    for _ in range(12):
        app.update()
    return c

_c5 = _put_detail(40)
_h5a = app.txt_detail.winfo_height()
_v5 = app.txt_detail.yview()
check("详解整段可见 (yview 是 0.0~1.0, 没有需要滚的余量)",
      abs(float(_v5[0])) < 1e-6 and abs(float(_v5[1]) - 1.0) < 1e-6, _v5)
check("详解确实被撑高了 (不再是一行的高度)",
      _h5a > 200, _h5a)
if _cv5 is not None:
    _box5 = _cv5.bbox("all")
    check("整页的滚动范围装得下详解整块 (用整页滚轮才看得到全部)",
          _box5 is not None and _box5[3] >= app.det_detail.winfo_y()
          + app.det_detail.winfo_height(),
          "scrollregion=%s, 详解底边=%d"
          % (_box5, app.det_detail.winfo_y() + app.det_detail.winfo_height()))

# 内容变短 -> 高度要跟着缩回去 (说明是"按内容", 不是"撑开就不管了")
_put_detail(6)
_h5b = app.txt_detail.winfo_height()
check("内容变短时高度跟着缩回去",
      _h5b < _h5a, "%d -> %d" % (_h5a, _h5b))

# 长度扫一遍, 每一档都必须"整段可见", 而且**没被页面压扁**。
#
# 这条钉的是哪一步 (都实测过):
#   * 把"让整页跟上"那一刀 (_refit_page) 摘掉再跑这一遍 -> 9 档全挂 (高度一律
#     停在 40 像素, yview 报 0.07~0.01)。也就是说这条确实能抓住"页面那一刀没
#     生效"这个回归, 而它正是真正卡死人的那个: 内层的请求高度滞后一拍, _fit
#     在那一拍算出"没变化"就不改了, 而 <Configure> 只在实际尺寸变了才发 ——
#     于是控件永远被压扁, 详解最后几百像素永远看不见。
#   * 只把"按 yview 核实"那一步摘掉 (仍按行数估 + 多给一行) -> 这一遍**全绿**,
#     抓不到。合成内容量出来是 22 px/显示行左右, 估算刚好够; 真实记录里有
#     24 px/显示行的 (一行一行短句, 每行要多花 spacing1+spacing3), 那种会差
#     十几到几十像素。所以"整段可见"的硬保证靠的是定高那一串 (见 ui.py 的
#     _settle_detail), 不是这条断言 —— 这里如实写清楚, 免得下次有人以为它是
#     万能保险。
_bad5 = []
_squashed5 = []
for _n5 in (2, 3, 5, 8, 12, 18, 25, 33, 40):
    _put_detail(_n5)
    _y5 = float(app.txt_detail.yview()[1])
    if _y5 < 0.999:
        _bad5.append("%d 段 -> yview1=%.4f" % (_n5, _y5))
    # 控件拿到了它请求的高度吗 —— 被压扁的话下面的 yview 也不作数
    _r5, _a5 = app.txt_detail.winfo_reqheight(), app.txt_detail.winfo_height()
    if _a5 < _r5:
        _squashed5.append("%d 段 -> 请求 %d, 实际 %d" % (_n5, _r5, _a5))
check("从短到长各档都整段可见",
      not _bad5, _bad5)
check("从短到长各档都没被页面压扁 (实际高度 >= 请求高度)",
      not _squashed5, _squashed5)

# 窗口变窄 -> 同样的字要折更多行 -> 高度要重新算
# 段落得**足够长**才验得出来。第一版用 200 字的段落, 1124 和 904 宽下都恰好折
# 3 行 (200 个字约 2680 像素, 除以 904 是 2.96) —— 行数一样、高度一样, 那条断言
# 就成了"反正都一样"的空转, 挂了还以为代码有问题。400 字就有明显落差 (5 行 -> 6 行)。
_put_detail(40, words=80)
_w5a = app.txt_detail.winfo_width()
_h5c = app.txt_detail.winfo_height()
app.geometry("1000x860")
for _ in range(8):
    app.update()
check("窗口变窄后详解重新算过行数 (高度变大)",
      app.txt_detail.winfo_height() > _h5c,
      "宽 %d -> %d, 高 %d -> %d"
      % (_w5a, app.txt_detail.winfo_width(), _h5c, app.txt_detail.winfo_height()))
check("变窄之后仍然整段可见 (重算没有把内容挤出框外)",
      float(app.txt_detail.yview()[1]) >= 0.999, app.txt_detail.yview())
app.geometry("1220x860")
for _ in range(8):
    app.update()

# --- 3. 滚轮: 在详解上滚 = 滚整页; 在结果表上滚 = 滚表格自己 ---
check("详解框没被登记成'自带滚动条的控件' (没绑 <Enter>, 滚轮不会被它抢走)",
      app.txt_detail.bind("<Enter>") == "", app.txt_detail.bind("<Enter>"))
app.tree.event_generate("<Enter>")
app.update()
check("指针进结果表 -> 整页滚轮让给表格",
      app.tk.call("bind", "all", "<MouseWheel>") == "",
      repr(app.tk.call("bind", "all", "<MouseWheel>")))
app.tree.event_generate("<Leave>")
app.update()
check("指针离开结果表 -> 滚轮还给整页 (详解就在这片区域里)",
      app.tk.call("bind", "all", "<MouseWheel>") != "",
      repr(app.tk.call("bind", "all", "<MouseWheel>")))
if _cv5 is not None:
    _cv5.yview_moveto(0.0)
    app.update()
    _cv5.event_generate("<MouseWheel>", delta=-120)
    app.update()
    check("整页滚轮真的能往下滚 (滚到底就能看到详解末尾)",
          float(_cv5.yview()[0]) > 0.0, _cv5.yview())

# --- 4. Ctrl + 滚轮: 滚的是整页最外侧那个滚动条 ---
# 上面刚验过"指针在结果表上滚的是表格自己"。要滚整页就得按住 Ctrl, 而这件事
# 只能靠**按控件类**挂 <Control-MouseWheel> 来做 (见 ui.py 的 _bind_ctrl_wheel):
# 实测 Tk 8.6.9 下按住 Ctrl 时 <MouseWheel> 照样匹配, 挂到 "all" 上的话表格会
# 跟着一起滚 —— 两个滚动条一起动, 正是这个功能要消掉的东西。
check("Text 类挂上了 <Control-MouseWheel>",
      bool(app.bind_class("Text", "<Control-MouseWheel>")),
      repr(app.bind_class("Text", "<Control-MouseWheel>")))
check("Treeview 类挂上了 <Control-MouseWheel>",
      bool(app.bind_class("Treeview", "<Control-MouseWheel>")),
      repr(app.bind_class("Treeview", "<Control-MouseWheel>")))

# 上面那两行的前提是"页面里自带滚动条的控件只有这两种"。将来谁往页面里塞一个
# Listbox 之类, 这一条会先挂 —— 而不是让 Ctrl+滚轮在它上面悄悄失灵。
_self_scroll6 = []
for _w6 in all_widgets(app):
    try:
        if str(_w6.cget("yscrollcommand")):
            _self_scroll6.append(_w6)
    except Exception:
        pass


def _ok_scroller(w):
    """自带滚动条的控件是不是"按类挂上就行"的那几种。

    整页容器 (canvas) 也在名单里: 它自己的滚动条**就是**最外侧那条, 不用抢。
    """
    return (isinstance(w, (tk.Text, ttk.Treeview))
            or getattr(w, "page_canvas", None) is w)


check("自带竖向滚动条的控件只有 Text / Treeview (整页容器除外)",
      all(_ok_scroller(w) for w in _self_scroll6),
      [(str(w), type(w).__name__) for w in _self_scroll6
       if not _ok_scroller(w)])
check("确实找到了自带滚动条的控件 (上面那条不是空话)",
      len(_self_scroll6) >= 5, len(_self_scroll6))

check("从结果表往上找到的是整页 canvas (不是它自己)",
      app._outermost_canvas(app.tree) is _cv5, app._outermost_canvas(app.tree))
check("从日志框往上找到的也是整页 canvas",
      app._outermost_canvas(app.txt_run_log) is _cv5,
      app._outermost_canvas(app.txt_run_log))
check("canvas 自己也认得自己 (指针落在它身上时也找得到)",
      app._outermost_canvas(_cv5) is _cv5, app._outermost_canvas(_cv5))
# 「文献列表」页没有整页容器: 那一页最外侧的滚动条**就是**表格自己的, 所以这里
# 必须返回 None (也就是不 break), 让表格照旧自己滚。
check("「文献列表」页找不到整页容器 (Ctrl+滚轮不抢它的滚轮)",
      app._outermost_canvas(app.tree_lib) is None,
      app._outermost_canvas(app.tree_lib))

# 结果表和日志框都得**真有得滚**, 否则"它自己没动"那句话是空的
for _i6 in range(60):
    app.tree.insert("", "end", values=(_i6 + 1, "占位论文 %d" % _i6, "作者",
                                       "2026-09-01", "期刊", "0.500", ""))
app.txt_run_log.configure(state="normal")
app.txt_run_log.insert("end", "\n".join("滚轮测试行 %d" % _i6
                                        for _i6 in range(40)))
app.txt_run_log.configure(state="disabled")
for _ in range(3):
    app.update()
check("结果表自己滚得动 (不是空表)", float(app.tree.yview()[1]) < 0.999,
      app.tree.yview())
check("日志框自己滚得动 (不是空框)", float(app.txt_run_log.yview()[1]) < 0.999,
      app.txt_run_log.yview())

if _cv5 is not None:
    _cv5.yview_moveto(0.0)
    app.tree.yview_moveto(0.0)
    app.txt_run_log.yview_moveto(0.0)
    app.update()
    _tree6 = app.tree.yview()[0]
    app.tree.event_generate("<Control-MouseWheel>", delta=-120)
    app.update()
    check("Ctrl+滚轮 停在结果表上 -> 滚的是整页", float(_cv5.yview()[0]) > 0.0,
          _cv5.yview())
    check("Ctrl+滚轮 停在结果表上 -> 表格自己纹丝不动",
          app.tree.yview()[0] == _tree6, (app.tree.yview(), _tree6))

    _cv5.yview_moveto(0.0)
    app.txt_run_log.yview_moveto(0.0)
    app.update()
    _log6 = app.txt_run_log.yview()[0]
    app.txt_run_log.event_generate("<Control-MouseWheel>", delta=-120)
    app.update()
    check("Ctrl+滚轮 停在日志框上 -> 滚的是整页", float(_cv5.yview()[0]) > 0.0,
          _cv5.yview())
    check("Ctrl+滚轮 停在日志框上 -> 日志框自己纹丝不动",
          app.txt_run_log.yview()[0] == _log6,
          (app.txt_run_log.yview(), _log6))

    # 往上滚也认 (方向来自 delta 的正负, 和普通滚轮那套一致)
    _cv5.yview_moveto(0.5)
    app.update()
    _mid6 = float(_cv5.yview()[0])
    app.tree.event_generate("<Control-MouseWheel>", delta=120)
    app.update()
    check("Ctrl+往上滚 -> 整页往回滚", float(_cv5.yview()[0]) < _mid6,
          (_mid6, _cv5.yview()))

# 不带 Ctrl 时老规矩一个字不改: 指针在表格上, 滚的就是表格自己
app.tree.yview_moveto(0.0)
if _cv5 is not None:
    _cv5.yview_moveto(0.0)
app.tree.event_generate("<Enter>")      # 指针进表格 -> 整页滚轮让给表格
app.update()
app.tree.event_generate("<MouseWheel>", delta=-120)
app.update()
check("不带 Ctrl 滚结果表 -> 表格自己滚 (老行为没被改坏)",
      float(app.tree.yview()[0]) > 0.0, app.tree.yview())
check("不带 Ctrl 滚结果表 -> 整页没动",
      _cv5 is None or float(_cv5.yview()[0]) == 0.0,
      _cv5.yview() if _cv5 is not None else None)
app.tree.event_generate("<Leave>")
app.update()

# 收尾: 把为这一节塞进去的占位行清掉, 别带给后面几节
for _item6 in app.tree.get_children()[1:]:
    app.tree.delete(_item6)
for _ in range(2):
    app.update()

# --- 5. 老 config.json 里残留的 ui 段: 点一次"保存设置"就该自己消失 ---
# 上一版把"详解被拖到多高"写在 ui.detail_height 里。现在没有这一项了, 但那一段
# 还躺在用户的 config.json 里 —— 保存一次之后不能再留着 (否则年年留一段谁都
# 不认识、谁都不敢删的配置)。
with io.open(cfg_dst, encoding="utf-8") as _fh5c:
    _raw5c = json.load(_fh5c)
_raw5c["ui"] = {"detail_height": 420}
with io.open(cfg_dst, "w", encoding="utf-8") as _fh5c:
    json.dump(_raw5c, _fh5c, ensure_ascii=False, indent=2)
app.reload_config()
app.save_config()
with io.open(cfg_dst, encoding="utf-8") as _fh5c:
    _after5c = json.load(_fh5c)
check("保存设置后残留的 ui 段被丢掉 (白名单里已经没有它)",
      "ui" not in _after5c, sorted(_after5c))
check("丢掉 ui 段没有顺手丢掉别的段",
      set(_after5c.keys()) >= set(_raw5c.keys()) - {"ui"},
      "%s -> %s" % (sorted(_raw5c), sorted(_after5c)))

# 收尾: 别把这一节的论文带给后面几节
app.ranked = []
app._tree_from_history = False
app._fill_tree([], {})
for _ in range(2):
    app.update()

print()
print("=" * 70)
print("A6) 「推荐记录」下拉框: 挑看哪一轮 · 和 AI 深入讨论这一篇")
print("=" * 70)

# 用户的原话: "比如我总共开始推荐了3次, 推荐记录那里应该可以选择用哪次的推荐记录,
# 选择后下面的列表就显示那次的推荐结果" + "在这个界面再添加一个 ai 进一步对话的
# 窗口…交流的结果也存在这个文献的记忆里…更新文献的详解需要单独加一个按钮"。
#
# 这一节把两件事都走一遍。造两份报告当"跑过的两轮" —— 不依赖真跑推荐, 也不依赖
# arXiv 通不通; 真跑那两轮在 D/E 节。
from arxiv_rec.history import RecommendHistory as _RH6
from arxiv_rec.history import load_chat as _lc6
from arxiv_rec.history import load_rows as _lrows6
from arxiv_rec.models import Candidate as _C6
from arxiv_rec.models import LibraryPaper

# A4 那一节的 finally 已经把弹窗还原成真的了, 这里再拦一次 (后面要问"到底弹了
# 没有"), 结束时照样还原。
_real_ask6 = _ui.messagebox.askyesno
_real_info6 = _ui.messagebox.showinfo
_real_err6 = _ui.messagebox.showerror
_ui.messagebox.askyesno = _fake_ask
_ui.messagebox.showinfo = _fake_info
_ui.messagebox.showerror = _fake_err
_out_dir = raw["output"]["dir"]
os.makedirs(_out_dir, exist_ok=True)

_RUN_HEAD = u"""# arXiv 相关文献推荐报告

> 由你的本地文献库自动分析生成 · 生成时间 %s

## 三、推荐总览

| # | 论文 | 提交日期 | 相关性 | 时效性 | 重要性 | 总分 | 引用 | 标记 |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
"""


def _mk_run(path, stamp, rows):
    """按报告的真实版式造一份: rows = [(标题, id, 日期, 总分, 作者, 分类)]。"""
    body = [_RUN_HEAD % stamp]
    for i, (title, aid, date, score, author, cat) in enumerate(rows, 1):
        body.append("| %d | [%s](https://arxiv.org/abs/%s) | %s | 0.90 | 0.90 | "
                    "0.30 | **%s** | 7 |  |\n" % (i, title, aid, date, score))
    body.append("\n")
    for i, (title, aid, date, score, author, cat) in enumerate(rows, 1):
        body.append("\n### %d. %s\n\n**arXiv**: [%s](https://arxiv.org/abs/%s)"
                    " · **PDF**: [下载](https://arxiv.org/pdf/%s)\n\n"
                    "**作者**: %s · **提交**: %s\n\n**分类**: %s\n"
                    % (i, title, aid, aid, aid, author, date, cat))
    with io.open(path, "w", encoding="utf-8") as _fh:
        _fh.write("".join(body))


_RUN_A = os.path.join(_out_dir, "arxiv_recommend_20260928_090000.md")
_RUN_B = os.path.join(_out_dir, "arxiv_recommend_20260929_105537_second.md")
_mk_run(_RUN_A, "2026-09-28 09:00:00",
        [("第一轮的第一篇", "2401.50001", "2026-09-20", "0.801",
          "Wei Wang, Li Chen", "cond-mat.str-el"),
         ("第一轮的第二篇", "2401.50002", "2026-09-19", "0.602",
          "Bo Zhang", "cond-mat.str-el")])
_mk_run(_RUN_B, "2026-09-29 10:55:37",
        [("第二轮的那一篇", "2401.50003", "2026-09-25", "0.700",
          "Qiang Liu", "cond-mat.str-el")])

# 记录库里给第一轮那两篇各留一条记录: 一篇有解读, 一篇没有。报告里**没有**解读,
# 它得从记录库里按 arXiv ID 挂回来 —— 这一条要验的就是那个挂接。
with _RH6(_hdb) as _h6:
    _c6a = _C6(arxiv_id="2401.50001", title="第一轮的第一篇")
    _c6a.summary = "记录库里存着的讲解。"
    _c6a.ideas = "记录库里存着的方向。"
    _c6a.analyzed = True
    _h6.record([_c6a, _C6(arxiv_id="2401.50002", title="第一轮的第二篇")],
               profile_fp="fp6")

_dlg["info"] = 0
_dlg["info_text"] = ""
app.var_use_history.set(True)
app.refresh_rec_runs()
app.update()

_vals6 = list(app.cmb_rec_run.cget("values"))
check("下拉框第一项是'本次运行的结果'", _vals6[0] == REC_LIVE, _vals6)
check("下拉框列出了两轮历史推荐 (新的在前)",
      any("2026-09-29 10:55:37" in v for v in _vals6)
      and any("2026-09-28 09:00:00" in v for v in _vals6), _vals6)
check("选项里写清了那一轮推荐了几篇",
      any(v.startswith("2026-09-28 09:00:00") and "2 篇" in v for v in _vals6), _vals6)
check("文件名后缀也带进标签了 (同一秒跑两轮也分得清)",
      any(v.endswith("second") for v in _vals6), _vals6)
check("最后一项是'全部累计记录 (N 篇)' (以前那个按钮的行为没丢)",
      _vals6[-1].startswith(REC_ALL_PREFIX) and "2 篇" in _vals6[-1], _vals6[-1])

_label_a = [v for v in _vals6 if v.startswith("2026-09-28")][0]
_label_b = [v for v in _vals6 if v.startswith("2026-09-29")][0]

# --- 选第一轮: 下面那张表必须换成那一轮的结果 ---
app.var_rec_run.set(_label_a)
app._on_rec_pick()
app.update()
_rows_a = app.tree.get_children()
check("选第一轮后铺出 2 篇", len(_rows_a) == 2, len(_rows_a))
check("铺的确实是**那一轮**的论文",
      "第一轮的第一篇" in str(cell(app.tree, _rows_a[0], "title")),
      cell(app.tree, _rows_a[0], "title"))
check("第二轮那篇不在这一轮里",
      all("第二轮" not in str(cell(app.tree, r, "title")) for r in _rows_a))
check("总分用的是那一轮报告里的数",
      cell(app.tree, _rows_a[0], "score") == "0.801",
      cell(app.tree, _rows_a[0], "score"))
check("这些是'从某一轮报告读回来的' (不是这一轮跑出来的)",
      all(c.from_run for c in app.ranked), [c.from_run for c in app.ranked])
check("模式记成了 run", app._rec_mode == "run", app._rec_mode)
check("第一条自动选中", app.tree.selection() == (_rows_a[0],), app.tree.selection())

_det6 = app.txt_detail.get("1.0", "end")
check("详解里写出了那一轮的总分 (报告里有这个数, 照实写)",
      "总分 0.801" in _det6, [ln for ln in _det6.splitlines() if "总分" in ln])
check("详解里说明了这个分数是哪来的 (不是这一轮算的)",
      "那一轮报告里的分数" in _det6, _det6[:200])
check("报告里没有的三个细分数不列出来 (列出来就是三个 0.00)",
      "相关 0.00" not in _det6, [ln for ln in _det6.splitlines() if "相关" in ln])
check("解读从记录库里按 arXiv ID 挂回来了 (报告里没有解读)",
      "记录库里存着的讲解。" in _det6, _det6[:300])
check("没有解读的那篇直说'记录里只留了标题和分数'",
      app.ranked[1].analyzed is False)

# --- 选第二轮: 列表跟着换 (这就是用户要的那件事) ---
app.var_rec_run.set(_label_b)
app._on_rec_pick()
app.update()
_rows_b = app.tree.get_children()
check("换到第二轮后列表变成那一轮的 1 篇", len(_rows_b) == 1, len(_rows_b))
check("列表里是第一轮**没有**的那篇",
      "第二轮的那一篇" in str(cell(app.tree, _rows_b[0], "title")),
      cell(app.tree, _rows_b[0], "title"))
check("上一轮的论文没有留在列表里",
      all("第一轮" not in str(cell(app.tree, r, "title")) for r in _rows_b))

# --- 切回'本次运行的结果': 原样还回来, 不重跑 ---
_live6 = _C6(arxiv_id="2401.60001", title="这一轮跑出来的那篇")
_live6.score = 0.9
app._live_ranked = [_live6]
app._live_top_ids = {"2401.60001"}
app.var_rec_run.set(REC_LIVE)
app._on_rec_pick()
app.update()
check("切回'本次运行的结果'后铺的是这一轮那份 (不是最后一次看的历史轮)",
      [str(cell(app.tree, r, "title")) for r in app.tree.get_children()]
      == ["这一轮跑出来的那篇"],
      [str(cell(app.tree, r, "title")) for r in app.tree.get_children()])
check("'推荐'标记也回来了 (top_ids 一起还的)",
      has_flag(app.tree, app.tree.get_children()[0], "推荐"),
      cell(app.tree, app.tree.get_children()[0], "flag"))
check("模式记回了 live", app._rec_mode == "live", app._rec_mode)

# --- 最后那一项 = 老的"推荐记录"按钮: 整库铺出来 ---
app.var_rec_run.set(_vals6[-1])
app._on_rec_pick()
app.update()
check("选'全部累计记录'铺的是整库 (两轮混在一起, 按记录排)",
      len(app.tree.get_children()) == 2, len(app.tree.get_children()))
check("模式记成了 all", app._rec_mode == "all", app._rec_mode)
check("这一张表标成了'来自记录库'", app._tree_from_history is True)
check("记录里每一条都是推荐过的, 不重复写'已推荐过'",
      all(c.from_history for c in app.ranked))

# --- 和 AI 深入讨论 ---
from arxiv_rec import chat as _chat6

_ai6 = {"client": None, "ensure": []}
_orig_chat_ai = app._chat_ai
_orig_ensure = _chat6.ensure_abstract
app._chat_ai = lambda cfg: _ai6["client"]


def _fake_ensure(cfg, cand, cache=None):
    """假装去 arXiv 取摘要 —— 记一笔, 不联网。

    取回来的摘要直接塞进候选对象 (和真实现一样), 顺带验"取回来之后详解面板
    会跟着显示"这条链路。
    """
    _ai6["ensure"].append(str(getattr(cand, "arxiv_id", "")))
    if not str(getattr(cand, "abstract", "") or ""):
        cand.abstract = "从 arXiv 取回来的摘要。"
        return True
    return False


_chat6.ensure_abstract = _fake_ensure

# 文献库也换掉: 拼讨论上下文要用它, 而**真读一遍是 219 个 PDF 的全文解析**
# (这台机器上四分钟起步)。讨论这一节要验的是"聊天怎么走、存哪、怎么写回详解",
# 不是 PDF 解析 —— 那个在 C 节验。给几篇假的, 既快又可控。
_orig_papers = app._chat_papers_cache
_papers6 = [LibraryPaper(item_id=1, key="k1", title="Sign problem in QMC",
                         abstract="Majorana positivity", year=2020,
                         authors=["Wei Wang"], arxiv_id="2001.00001"),
            LibraryPaper(item_id=2, key="k2", title="Pfaffian sign",
                         abstract="Reformulating the Pfaffian sign", year=2019,
                         authors=["Li Chen"], arxiv_id="1901.00001")]
app._chat_papers_cache = lambda cfg: _papers6


class _FakeChatAI:
    """假 AI: 聊天给一段文字, 重写详解给一份 JSON。不打网络。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.answer = "它把符号问题转成了一个可解的本征值问题。"
        self.data = {
            "summary": "按讨论重写的讲解。",
            "connections": [{"paper": "编出来的标签 2099", "relation": "编的"}],
            "ideas": "按讨论重写的新方向。",
        }

    def chat(self, system, user, json_mode=False, use_cache=True):
        self.calls.append(("chat", system, user))
        if self.fail:
            raise RuntimeError("假的: 接口挂了")
        return self.answer

    def chat_json(self, system, user, default=None, use_cache=True):
        self.calls.append(("json", system, user))
        if self.fail:
            raise RuntimeError("假的: 接口挂了")
        return self.data


def _pump(cond, secs=20.0):
    """转事件循环 (主线程取队列全靠 after, 不转就不会有反应) 直到 cond 为真。"""
    _t0 = time.time()
    while time.time() - _t0 < secs:
        app.update()
        if cond():
            return True
        time.sleep(0.03)
    return bool(cond())


try:
    # 铺出第一轮那两篇 —— 挑第一轮而不是第二轮: 这两篇**记录库里有**, 后面
    # "更新详解只碰解读那几列"才有得比 (第二轮那篇是报告里新冒出来的, 记录里没有)。
    app.var_rec_run.set(_label_a)
    app._on_rec_pick()
    app.update()

    check("有对话区那个框 (和详解同一页)", hasattr(app, "txt_chat"))
    check("有'用对话更新详解'按钮 (单独一个, 不是每聊一句就改)",
          "更新详解" in str(app.btn_chat_detail.cget("text")),
          app.btn_chat_detail.cget("text"))
    check("有'清空这段对话'按钮", hasattr(app, "btn_chat_clear"))
    check("有发送按钮", hasattr(app, "btn_chat_send"))

    _aid6 = str(app.ranked[0].arxiv_id)
    check("选中一篇之后, 对话区标题换成了这一篇",
          _aid6 in app.var_chat_paper.get() or "正在讨论" in app.var_chat_paper.get(),
          app.var_chat_paper.get())
    check("没聊过时对话区给的是提示 (不是一片空白)",
          "还没有聊过" in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[:80])

    # --- 发一句 ---
    _ai6["client"] = _FakeChatAI()
    app.var_chat_in.set("它的符号问题是怎么绕过去的?")
    app.send_chat()
    check("发出去之后输入框清空了", app.var_chat_in.get() == "",
          app.var_chat_in.get())
    check("等回复期间发送按钮置灰 (防连点)",
          str(app.btn_chat_send.cget("state")) == "disabled",
          app.btn_chat_send.cget("state"))
    check("刚问的那句立刻显示出来了 (不等 AI)",
          "它的符号问题是怎么绕过去的?" in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[:200])
    check("先落库再发: 用户那句在等回复时就已经存进记录了",
          [m["content"] for m in
           _lc6(app.cfg, _aid6)]
          == ["它的符号问题是怎么绕过去的?"],
          _lc6(app.cfg, _aid6))
    check("发之前先补了摘要 (从报告里翻出来的论文手上没有摘要)",
          _ai6["ensure"] and _ai6["ensure"][-1] == _aid6, _ai6["ensure"])

    check("等到了 AI 的回复", _pump(lambda: app._chat_busy_aid == ""), "超时")
    _chat_txt6 = app.txt_chat.get("1.0", "end")
    check("回复显示在对话区里", "它把符号问题转成了一个" in _chat_txt6,
          _chat_txt6[:400])
    check("回复也存进了这篇论文的记录",
          [m["content"] for m in
           _lc6(app.cfg, _aid6)][-1]
          == "它把符号问题转成了一个可解的本征值问题。",
          _lc6(app.cfg, _aid6))
    check("答完之后发送按钮恢复可点",
          str(app.btn_chat_send.cget("state")) == "normal",
          app.btn_chat_send.cget("state"))
    check("取回摘要之后详解面板里也跟着显示出来了 (chat_meta 那条链路)",
          "从 arXiv 取回来的摘要。" in app.txt_detail.get("1.0", "end"),
          app.txt_detail.get("1.0", "end")[:400])
    check("AI 拿到的上下文里有这篇论文的标题 (不是空手聊)",
          any("第一轮的第一篇" in c[2] for c in _ai6["client"].calls),
          [c[2][:60] for c in _ai6["client"].calls])

    # --- 换一篇再换回来: 讨论还在 (这就是"存在这篇文献的记忆里") ---
    # 这里**不动**列表的选中项: selection_set 会发出 <<TreeviewSelect>>, 而它
    # 要等到 update() 才处理 —— 那一拍会把下面这次手动 _show_detail 覆盖掉。
    app._show_detail(_C6(arxiv_id="2401.50009", title="换一篇看看"))
    app.update()
    check("换到别的论文时对话区跟着换 (不显示上一篇的讨论)",
          "它的符号问题是怎么绕过去的?" not in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[:120])
    app.tree.selection_set("1")
    app._on_select_candidate()
    app.update()
    check("换回来讨论还在 (关掉程序也还在, 它存在记录库里)",
          "它把符号问题转成了一个" in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[:300])

    # --- 「用对话更新详解」: 用户点才更新 ---
    _row6_before = {r["arxiv_id"]: r for r in
                    _lrows6(app.cfg)}[_aid6]
    check("聊完**没有**自动改详解 (详解还是记录里那份)",
          "按讨论重写的讲解。" not in app.txt_detail.get("1.0", "end"),
          app.txt_detail.get("1.0", "end")[:200])

    app.update_detail_from_chat()
    check("点下去之后按钮先置灰并写着'正在更新…'",
          str(app.btn_chat_detail.cget("state")) == "disabled",
          app.btn_chat_detail.cget("state"))
    check("更新完了", _pump(lambda: app._chat_busy_aid == ""), "超时")
    check("按钮恢复原样", str(app.btn_chat_detail.cget("state")) == "normal"
          and str(app.btn_chat_detail.cget("text")) == "用对话更新详解",
          (app.btn_chat_detail.cget("state"), app.btn_chat_detail.cget("text")))

    _rows6_after = {r["arxiv_id"]: r for r in
                    _lrows6(app.cfg)}
    _row6 = _rows6_after[_aid6]
    check("重写详解时把整场讨论都给了 AI (不然它只能凭摘要重写一遍)",
          any(c[0] == "json" and "它的符号问题是怎么绕过去的?" in c[2]
              and "它把符号问题转成了一个" in c[2]
              for c in _ai6["client"].calls),
          [(c[0], c[2][:80]) for c in _ai6["client"].calls])
    check("新的详解写回了记录库", _row6["summary"] == "按讨论重写的讲解。",
          _row6["summary"])
    check("新的研究方向也写回去了", _row6["ideas"] == "按讨论重写的新方向。",
          _row6["ideas"])
    check("编出来的关联标签被丢掉了 (留下的关联必须真在文献库里)",
          _row6["connections"] == "[]", _row6["connections"])
    check("详解面板上显示的就是新那份",
          "按讨论重写的讲解。" in app.txt_detail.get("1.0", "end"),
          app.txt_detail.get("1.0", "end")[:300])
    check("列表那一行的标记跟着亮起来 (不用重新铺表)",
          has_flag(app.tree, app.tree.get_children()[0], "已解读"),
          cell(app.tree, app.tree.get_children()[0], "flag"))
    # 用户只点了"更新详解", 没跑推荐 —— 次数/分数/时间一个都不该动
    check("推荐次数没被动过 (聊两句不该让次数涨)",
          _row6["times"] == _row6_before["times"],
          (_row6_before["times"], _row6["times"]))
    check("最后推荐时间没被动过", _row6["last_at"] == _row6_before["last_at"],
          (_row6_before["last_at"], _row6["last_at"]))
    check("讨论本身没被这次更新清掉",
          len(_lc6(app.cfg, _aid6)) == 2,
          _lc6(app.cfg, _aid6))
    check("上下文缓存作废了 (下一次提问要基于新的详解, 不是旧的)",
          _aid6 not in app._chat_ctx, list(app._chat_ctx))

    # --- 更新失败: 按钮必须回到可点的样子 (卡在'正在更新…'就再也点不动了) ---
    _ai6["client"] = _FakeChatAI(fail=True)
    app.update_detail_from_chat()
    check("失败也算结束", _pump(lambda: app._chat_busy_aid == ""), "超时")
    check("失败之后按钮仍然可点 (不能卡在'正在更新…')",
          str(app.btn_chat_detail.cget("state")) == "normal"
          and str(app.btn_chat_detail.cget("text")) == "用对话更新详解",
          (app.btn_chat_detail.cget("state"), app.btn_chat_detail.cget("text")))
    check("失败不会把好端端的详解改坏",
          {r["arxiv_id"]: r for r in _lrows6(app.cfg)}[_aid6]["summary"] == "按讨论重写的讲解。")

    # --- 追问失败: 对话区里写一行, 不弹对话框 (弹窗关掉用户那句就找不着了) ---
    _dlg["err"] = 0
    app.var_chat_in.set("再问一句")
    app.send_chat()
    check("追问失败也算结束", _pump(lambda: app._chat_busy_aid == ""), "超时")
    check("失败写在对话区里 (不弹对话框)", _dlg["err"] == 0, _dlg["err"])
    check("写明了这次没答上来",
          "没答上来" in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[-300:])
    check("用户问的那句还看得见 (没有跟着一起消失)",
          "再问一句" in app.txt_chat.get("1.0", "end"))
    check("失败时用户那句仍然存着 (敲进去的字不该丢)",
          any(m["content"] == "再问一句" for m in _lc6(app.cfg, _aid6)),
          _lc6(app.cfg, _aid6))
    check("失败不会往记录里塞一条假的 AI 回复",
          not any(m["role"] == "assistant" and "没答上来" in m["content"]
                  for m in _lc6(app.cfg, _aid6)))

    # --- 还没聊过就点'更新详解': 说清楚, 别花冤枉钱 ---
    # 换一个**干净**的假客户端: 上一条用的是 fail=True 那个, 它的 calls 本来就是空的,
    # 拿它验"没调 AI"等于没验。
    _ai6["client"] = _FakeChatAI()
    app._show_detail(_C6(arxiv_id="2401.50077", title="没聊过的"))
    app.update()
    _dlg["info"] = 0
    _dlg["info_text"] = ""
    app.update_detail_from_chat()
    check("没聊过就点'更新详解': 提示一句, 一次 AI 都没调",
          _dlg["info"] == 1 and _ai6["client"].calls == [],
          (_dlg["info"], _ai6["client"].calls))
    check("提示里说清了要先聊几句", "先问几句" in _dlg["info_text"],
          _dlg["info_text"][:120])

    # --- 清空这一段对话 ---
    app.tree.selection_set("1")
    app._on_select_candidate()
    app.update()
    _dlg["answer"] = True
    _dlg["ask"] = 0
    app.clear_chat()
    check("清空前先确认", _dlg["ask"] == 1, _dlg["ask"])
    check("对话区空了", "再问一句" not in app.txt_chat.get("1.0", "end"),
          app.txt_chat.get("1.0", "end")[:120])
    check("记录库里的讨论也清了",
          _lc6(app.cfg, _aid6) == [])
    check("清讨论**不动**推荐记录 (详解还在)",
          {r["arxiv_id"]: r for r in _lrows6(app.cfg)}[_aid6]["summary"] == "按讨论重写的讲解。")

    # --- 没选论文就发 ---
    app._show_detail(None)
    app.update()
    _dlg["info"] = 0
    _dlg["info_text"] = ""
    app.var_chat_in.set("对着空气说话")
    app.send_chat()
    check("没选论文就发: 提示先选一篇", _dlg["info"] == 1, _dlg["info"])
    check("提示里说清了要选一篇", "选一篇" in _dlg["info_text"],
          _dlg["info_text"][:80])
finally:
    app._chat_ai = _orig_chat_ai
    app._chat_papers_cache = _orig_papers
    _chat6.ensure_abstract = _orig_ensure
    _ui.messagebox.askyesno = _real_ask6
    _ui.messagebox.showinfo = _real_info6
    _ui.messagebox.showerror = _real_err6

# --- 报告被删掉之后, 下拉框里那一项要消失, 并退回'本次运行的结果' ---
app.var_rec_run.set(_label_a)
app._on_rec_pick()
app.update()
check("(前置) 现在停在第一轮上", app._rec_mode == "run", app._rec_mode)
os.remove(_RUN_A)
app.refresh_rec_runs()
app.update()
check("报告被删后下拉框里那一项也没了",
      not any(v.startswith("2026-09-28") for v in app.cmb_rec_run.cget("values")),
      app.cmb_rec_run.cget("values"))
check("当前选中的那份不见了就退回'本次运行的结果'",
      app.var_rec_run.get() == REC_LIVE, app.var_rec_run.get())
check("退回时在日志里说了一声'找不到了' (不是悄悄换掉)",
      "找不到了" in app.txt_run_log.get("1.0", "end"),
      app.txt_run_log.get("1.0", "end")[-200:])

# 收尾: 把这一节造的报告删掉, 别影响后面几节 (E2 会去挑"最新的一份报告")
for _p6 in (_RUN_A, _RUN_B):
    if os.path.exists(_p6):
        os.remove(_p6)
app._live_ranked = []
app._live_top_ids = set()
app.ranked = []
app._tree_from_history = False
app._fill_tree([], {})
app._show_detail(None)
app.refresh_rec_runs()
for _ in range(3):
    app.update()

print()
print("=" * 70)
print("B) 设置读写往返")
print("=" * 70)
before = json.load(io.open(cfg_dst, encoding="utf-8"))
app.var_base_url.set("http://example.invalid/v1")
app.var_api_key.set("sk-test-roundtrip")
app.var_model.set("test-model")
app.save_config()
after = json.load(io.open(cfg_dst, encoding="utf-8"))
check("base_url 写回", after["ai"]["base_url"] == "http://example.invalid/v1",
      after["ai"].get("base_url"))
check("api_key 写回", after["ai"]["api_key"] == "sk-test-roundtrip")
check("model 写回", after["ai"]["model"] == "test-model")
check("未知字段没被抹掉",
      set(after.keys()) >= set(before.keys()),
      "%s -> %s" % (sorted(before), sorted(after)))
check("pdf_folders 保留", "pdf_folders" in after and after["pdf_folders"])

# 读取深度也要能存下来 (它现在存在 library.read_depth 里)
app.var_depth.set("fulltext")
app.save_config()
after2 = json.load(io.open(cfg_dst, encoding="utf-8"))
check("读取深度写回 library.read_depth",
      after2["library"]["read_depth"] == "fulltext",
      after2.get("library", {}).get("read_depth"))
app.var_depth.set("sections")
app.save_config()

# 三个可配置路径也要能存下来 (用户点名要的三件事)
_paths = {
    "library.index_db": (app.var_index_db, os.path.join(tmpdir, "idx2.sqlite")),
    "analysis.history_db": (app.var_history_db,
                            os.path.join(tmpdir, "hist.sqlite")),
    "output.dir": (app.var_out_dir, os.path.join(tmpdir, "out2")),
}
for _var, _val in _paths.values():
    _var.set(_val)
app.save_config()
after3 = json.load(io.open(cfg_dst, encoding="utf-8"))
for _key, (_var, _val) in _paths.items():
    _sec, _name = _key.split(".")
    check("%s 写回" % _key, after3.get(_sec, {}).get(_name) == _val,
          after3.get(_sec, {}).get(_name))
# 换回去, 后面的读取/推荐还得用原路径
app.var_index_db.set(after2["library"]["index_db"])
app.var_history_db.set(after2["analysis"]["history_db"])
app.var_out_dir.set(after2["output"]["dir"])
app.save_config()

# 改回去, 别影响后面的推荐测试
app.var_api_key.set("")
app.var_base_url.set(before["ai"]["base_url"])
app.var_model.set(before["ai"]["model"])
app.save_config()

print()
print("=" * 70)
print("C) 点'开始读取' -> 等线程跑完")
print("=" * 70)
app.nb.select(app.tab_read)
app.update()
t0 = time.time()
app.start_read()
deadline = t0 + 300
while app._current_job and time.time() < deadline:
    app.update()
    time.sleep(0.05)
check("读取任务结束 (没卡死)", not app._current_job, app._current_job)
check("读到了文献", len(app.papers) > 0, len(app.papers))
# 读取路径不走 PipelineResult, 结果直接落在 self.papers 上 (self.result 是推荐用的)
# 只要求"读到了一批", 不钉死具体篇数 —— 文献库会变, 钉死了每次加文件都要改测试
check("papers 已填充", len(app.papers) > 50, len(app.papers))
# 按 "sections" 档读出来的应该带上下文 (引言/结论), 而且统计里要报告当前档位
check("读取深度是 sections", app.cfg["library"]["read_depth"] == "sections",
      app.cfg["library"].get("read_depth"))
check("有文献带上了引言/结论上下文",
      sum(1 for p in app.papers if getattr(p, "context", "")) > 10,
      sum(1 for p in app.papers if getattr(p, "context", "")))
log_text = app.txt_read_log.get("1.0", "end").strip()
check("日志面板收到了内容", len(log_text) > 0, len(log_text))
check("日志里有'读取完成'", "读取完成" in log_text, log_text[:80])
print("     耗时 %.1fs, %d 篇" % (time.time() - t0, len(app.papers)))

print()
print("=" * 70)
print("C2) 文献列表页: 列出的必须是刚读过的那些 PDF")
print("=" * 70)
app.nb.select(app.tab_list)
app.update()
app.refresh_library_list()      # 同步的: 拉完索引直接建表, 不用等线程
rows_lib = app.tree_lib.get_children()
check("列表页有行", len(rows_lib) > 50, len(rows_lib))
# 列表列的是**索引里的文件**(204 个), papers 是去重后的文献(183 篇) ——
# 同一篇 PDF 存在两个文件夹时索引里是两行。所以这里不比总数, 比"每篇读到的
# 文献都能在列表里找到自己那一条", 这才是用户点得开的前提。
# 按规范化标题比而不是按路径: LibraryPaper 上没有 path 字段, 索引里的
# disp_path 和读取时用的路径也未必逐字相同。标题取 _lib_rows 里的原文 ——
# 表格里那列超过 70 字会被截断加省略号, 拿它比会把长标题全判成"找不到"。
from arxiv_rec.utils import normalize_title
_lib_titles = set(normalize_title(r["title"]) for r in app._lib_rows)
_missing = [p for p in app.papers if normalize_title(p.title) not in _lib_titles]
check("读到的每篇文献都在列表里能找到", not _missing,
      "%d 篇找不到, 例: %s" % (len(_missing),
                               [_missing[0].title[:60]] if _missing else ""))
if rows_lib:
    vals = app.tree_lib.item(rows_lib[0], "values")
    check("列齐了 (名/作者/期刊/标签/时间/深度/状态)", len(vals) == 7, len(vals))
    check("文献名非空", str(vals[0]).strip() != "", vals)
    check("发表时间列是年份或空",
          str(vals[4]).strip() == "—" or str(vals[4]).strip().isdigit(), vals[4])
    check("读取深度列 = 当前档位 (sections)",
          str(vals[5]).strip() in ("", "—", DEPTH_LABELS["sections"]), vals[5])
    # 选中 -> 底部显示完整路径 (点"打开 PDF"用的就是它)
    app.tree_lib.selection_set(rows_lib[0])
    app.update()
    # 详情串是 "路径 · N 页 · arXiv:xxx · 读取于 ...", 路径是第一段
    _shown_path = app.var_lib_path.get().split(" · ")[0].strip()
    check("选中后底部显示真实存在的文件路径", os.path.isfile(_shown_path),
          app.var_lib_path.get()[:120])
    # 搜索过滤
    app.var_lib_q.set("the")
    app._fill_library_tree()
    _n = len(app.tree_lib.get_children())
    check("搜索能过滤列表", _n < len(rows_lib), _n)
    app.var_lib_q.set("")
    app._fill_library_tree()
    check("清空搜索后恢复全部", len(app.tree_lib.get_children()) == len(rows_lib),
          len(app.tree_lib.get_children()))
    # 标签要能存下来并显示在 tag 列 (用户点名要这一列)
    from arxiv_rec.pdf_library import set_paper_tags
    set_paper_tags(app.cfg, rows_lib[0], ["uitest-tag"])
    app.refresh_library_list()
    _tag_cell = str(app.tree_lib.item(rows_lib[0], "values")[3])
    check("标签写进读取记录后显示在 tag 列", "uitest-tag" in _tag_cell, _tag_cell)
    set_paper_tags(app.cfg, rows_lib[0], [])
    app.refresh_library_list()
    check("清空标签后 tag 列不再有它",
          "uitest-tag" not in str(app.tree_lib.item(rows_lib[0], "values")[3]),
          app.tree_lib.item(rows_lib[0], "values")[3])

print()
print("=" * 70)
print("C3) 限定分类: 复选框 (一个都不勾 = 不限)")
print("=" * 70)
from arxiv_rec.arxiv_search import ALL_CATEGORIES

check("每个候选分类都有一个复选框", set(app.var_cats) == set(ALL_CATEGORIES),
      set(ALL_CATEGORIES) ^ set(app.var_cats))
check("复选框都是 BooleanVar (能勾)", all(
    hasattr(v, "set") and hasattr(v, "get") for v in app.var_cats.values()))

app._set_categories([])
check("全不选 -> 不限分类", app._selected_categories() == [],
      app._selected_categories())
check("全不选时旁边小字写'不限'", "不限" in app.lbl_cats.cget("text"),
      app.lbl_cats.cget("text"))

app._set_categories(["quant-ph", "cs.LG"])
check("勾两个就报两个", app._selected_categories() == ["quant-ph", "cs.LG"],
      app._selected_categories())

# 界面顺序固定, 不受勾选先后影响 —— 否则同一个配置每次生成的检索条件都不一样,
# 缓存和结果对比都会跟着飘
app._set_categories(["cs.LG", "quant-ph"])
check("返回顺序跟界面一致 (不受勾选先后影响)",
      app._selected_categories() == ["quant-ph", "cs.LG"],
      app._selected_categories())

# 手改过 config.json 的人可能写了个不在列表里的分类: 要忽略, 不能崩
app._set_categories(["cond-mat.str-el", "这不是个分类"])
check("不认识的分类被忽略而不是崩",
      app._selected_categories() == ["cond-mat.str-el"],
      app._selected_categories())

# 存进配置再读回来
app._set_categories(["hep-th"])
app.save_config()
app.reload_config()
check("勾选写进 config.json 并读得回来", app._selected_categories() == ["hep-th"],
      app._selected_categories())

app._set_categories([])
app.save_config()
app.reload_config()
check("全不选也存得住 (存的是空列表, 不是把这个键删掉)",
      app._selected_categories() == [], app._selected_categories())

print()
print("=" * 70)
print("C4) 订阅模式开关")
print("=" * 70)

check("订阅模式复选框默认不勾", app.var_subscribe.get() is False,
      app.var_subscribe.get())
check("不勾订阅时旁边没有提示字", app.lbl_subscribe.cget("text") == "",
      app.lbl_subscribe.cget("text"))

# 订阅模式 + 没勾分类 -> 必须当场警告 (不能等跑几分钟才报错)
app._set_categories([])
app.var_subscribe.set(True)
app._refresh_cat_label()
check("订阅模式没勾分类时提示要勾分类",
      "分类" in app.lbl_subscribe.cget("text"),
      app.lbl_subscribe.cget("text"))

app._set_categories(["cond-mat.str-el", "quant-ph"])
app._refresh_cat_label()
check("订阅模式勾了分类后提示变成订阅几个分类",
      "订阅 2 个分类" in app.lbl_subscribe.cget("text"),
      app.lbl_subscribe.cget("text"))

# 检索式框要置灰: 订阅模式下它填了也不算数, 得让这件事看得见
check("订阅模式下检索式框被置灰", app.ent_queries.instate(["disabled"]),
      app.ent_queries.state())
check("订阅模式下检索式提示改说明",
      "不生效" in app.lbl_queries_hint.cget("text"),
      app.lbl_queries_hint.cget("text"))

# 值不能被清掉 —— 取消订阅要能接着用
app.var_queries.set("all:electron")
app.var_subscribe.set(False)
app._refresh_cat_label()
check("取消订阅后检索式框恢复可编辑",
      not app.ent_queries.instate(["disabled"]), app.ent_queries.state())
check("取消订阅后之前填的检索式还在",
      app.var_queries.get() == "all:electron", app.var_queries.get())
check("取消订阅后检索式提示恢复原样",
      "自动生成" in app.lbl_queries_hint.cget("text"),
      app.lbl_queries_hint.cget("text"))
app.var_queries.set("")
app.var_subscribe.set(True)
app._refresh_cat_label()

# 存进配置再读回来
app.save_config()
app.reload_config()
check("订阅模式存进 config.json 并读得回来",
      app.var_subscribe.get() is True, app.var_subscribe.get())
check("订阅模式读回来后分类也在",
      app._selected_categories() == ["cond-mat.str-el", "quant-ph"],
      app._selected_categories())

app.var_subscribe.set(False)
app.save_config()
app.reload_config()
check("取消订阅模式也存得住", app.var_subscribe.get() is False,
      app.var_subscribe.get())

app._set_categories([])
app.save_config()
app.reload_config()

print()
print("=" * 70)
print("C5) 文献列表页: 选一份历史推荐结果, 显示那一次的推荐列表")
print("=" * 70)
from arxiv_rec.ui import RUN_LIBRARY

# 造一份报告放进临时输出目录 —— 不依赖真跑一轮, 也不依赖 arXiv 通不通。
_out_dir = raw["output"]["dir"]
os.makedirs(_out_dir, exist_ok=True)
_FAKE_REPORT = u"""# arXiv 相关文献推荐报告

> 由你的本地文献库自动分析生成 · 生成时间 2026-09-29 10:55:37

## 三、推荐总览

| # | 论文 | 提交日期 | 相关性 | 时效性 | 重要性 | 总分 | 引用 | 标记 |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | [Majorana Positivity and the Sign Problem](https://arxiv.org/abs/2401.00001) | 2026-09-25 | 0.98 | 0.99 | 0.30 | **0.788** | 12 | 🆕新 |
| 2 | [Reformulating the Pfaffian Sign](https://arxiv.org/abs/2401.00002) | 2026-09-24 | 0.95 | 0.98 | 0.90 | **0.770** | 440 | ⭐经典 📖已发表 |

### 1. Majorana Positivity and the Sign Problem

**arXiv**: [2401.00001](https://arxiv.org/abs/2401.00001) · **PDF**: [下载](https://arxiv.org/pdf/2401.00001)

**作者**: Wei Wang, Li Chen, Bo Zhang · **提交**: 2026-09-25

**分类**: cond-mat.str-el

### 2. Reformulating the Pfaffian Sign

**arXiv**: [2401.00002](https://arxiv.org/abs/2401.00002) · **PDF**: [下载](https://arxiv.org/pdf/2401.00002)

**作者**: Bo Zhang · **提交**: 2026-09-24

**期刊**: Phys. Rev. Lett. 104, 157201 (2010)
"""
with io.open(os.path.join(_out_dir, "arxiv_recommend_20260929_105537.md"), "w",
             encoding="utf-8") as _fh:
    _fh.write(_FAKE_REPORT)

app.nb.select(app.tab_list)
app.update()
app.refresh_library_list()
app.update()

check("下拉框第一项是'已读文献'", app.var_run.get() == RUN_LIBRARY,
      app.var_run.get())
check("下拉框列出了刚造的那份报告",
      any("2026-09-29 10:55:37" in v and "2 篇" in v
          for v in app.cmb_run.cget("values")),
      app.cmb_run.cget("values"))
check("'已读文献'模式下显示的是本地 PDF 那个表",
      shown(app._lib_body) and not shown(app._rec_body),
      (app._lib_body.winfo_manager(), app._rec_body.winfo_manager()))
check("'已读文献'模式下 PDF 按钮可用",
      str(app.btn_lib_open.cget("state")) == "normal",
      app.btn_lib_open.cget("state"))

_run_label = [v for v in app.cmb_run.cget("values") if v != RUN_LIBRARY][0]
app.var_run.set(_run_label)
app._on_run_pick()
app.update()

check("选中一份推荐结果后切到推荐结果表",
      shown(app._rec_body) and not shown(app._lib_body),
      (app._lib_body.winfo_manager(), app._rec_body.winfo_manager()))
check("推荐结果模式下 PDF 按钮被置灰 (那些论文不在本地)",
      str(app.btn_lib_open.cget("state")) == "disabled",
      app.btn_lib_open.cget("state"))
check("推荐结果模式下'打开这份报告'可用",
      str(app.btn_run_open.cget("state")) == "normal",
      app.btn_run_open.cget("state"))
_rows_r = app.tree_rec.get_children()
check("推荐结果表列出 2 篇", len(_rows_r) == 2, len(_rows_r))
check("第一行是论文名",
      "Majorana Positivity" in str(cell(app.tree_rec, _rows_r[0], "title")),
      cell(app.tree_rec, _rows_r[0], "title"))
check("作者列只留第一位 + 等",
      cell(app.tree_rec, _rows_r[0], "authors") == "Wei Wang 等",
      cell(app.tree_rec, _rows_r[0], "authors"))
check("单作者不加'等'", cell(app.tree_rec, _rows_r[1], "authors") == "Bo Zhang",
      cell(app.tree_rec, _rows_r[1], "authors"))
check("提交日期列解析出来了",
      cell(app.tree_rec, _rows_r[0], "pub") == "2026-09-25",
      cell(app.tree_rec, _rows_r[0], "pub"))
check("期刊列解析出来了",
      "Phys. Rev. Lett." in str(cell(app.tree_rec, _rows_r[1], "journal")),
      cell(app.tree_rec, _rows_r[1], "journal"))
check("没写期刊的那篇显示破折号",
      cell(app.tree_rec, _rows_r[0], "journal") == "—",
      repr(cell(app.tree_rec, _rows_r[0], "journal")))
check("总分列去掉了加粗星号",
      cell(app.tree_rec, _rows_r[0], "score") == "0.788",
      repr(cell(app.tree_rec, _rows_r[0], "score")))
check("标记列的表情换成了纯文字",
      has_flag(app.tree_rec, _rows_r[1], "经典") and
      has_flag(app.tree_rec, _rows_r[1], "已发表"),
      cell(app.tree_rec, _rows_r[1], "flag"))
check("标题用的是详解里的完整标题 (不是被截到 78 字的表格版)",
      str(cell(app.tree_rec, _rows_r[0], "title")).endswith("Sign Problem"),
      cell(app.tree_rec, _rows_r[0], "title"))
check("页眉写了是哪一份、共几篇",
      "共 2 篇" in app.var_lib_head.get(), app.var_lib_head.get())

# 搜索框在推荐结果模式下必须过滤**推荐结果**那张表 (以前会去重建藏起来的
# 已读文献表, 屏幕上一点反应都没有)
app.var_lib_q.set("Pfaffian")
app._fill_current_tree()
app.update()
check("搜索框在推荐结果模式下过滤的是推荐结果表",
      len(app.tree_rec.get_children()) == 1, len(app.tree_rec.get_children()))
check("过滤后页眉说明匹配了几篇",
      "其中 1 篇匹配当前搜索" in app.var_lib_head.get(), app.var_lib_head.get())
app.var_lib_q.set("")
app._fill_current_tree()

# 双击 = 打开 arXiv 页 (这里把 _open_path 换掉, 免得真弹浏览器)
_opened = []
_orig_open_path = app._open_path
app._open_path = lambda p: _opened.append(p)
try:
    app.tree_rec.selection_set(_rows_r[0])
    app.open_selected_run_paper()
    check("双击/回车打开的是这篇的 arXiv 页",
          _opened == ["https://arxiv.org/abs/2401.00001"], _opened)
finally:
    app._open_path = _orig_open_path

# 切回"已读文献" -> 按钮状态要跟着还原
app.var_run.set(RUN_LIBRARY)
app._on_run_pick()
app.update()
check("切回已读文献后 PDF 按钮恢复可用",
      str(app.btn_lib_open.cget("state")) == "normal",
      app.btn_lib_open.cget("state"))
check("切回已读文献后显示的是本地 PDF 表",
      shown(app._lib_body) and not shown(app._rec_body),
      (app._lib_body.winfo_manager(), app._rec_body.winfo_manager()))

# 报告文件被删掉之后, 下拉框里那一项要消失, 且不能卡在"选中一个不存在的"
os.remove(os.path.join(_out_dir, "arxiv_recommend_20260929_105537.md"))
app.refresh_run_list()
app.update()
check("报告被删后下拉框里那一项也没了",
      not any(v != RUN_LIBRARY for v in app.cmb_run.cget("values")),
      app.cmb_run.cget("values"))
check("报告被删后自动退回'已读文献'", app.var_run.get() == RUN_LIBRARY,
      app.var_run.get())

print()
print("=" * 70)
print("D) 点'开始推荐' (限定分类, 不调 AI) -> 验证分类过滤在界面里也生效")
print("=" * 70)
app.nb.select(app.tab_rec)
app.var_use_ai.set(False)
app.var_enrich.set(False)
# 这一段以前靠取消勾选"读取全文"把深度压回 sections (那个复选框会覆盖深度
# 选择)。复选框撤掉之后, 唯一能压深度的就是深度单选框本身 —— B) 段刚把它设
# 成 fulltext 测过写回, 这里得显式压回来, 否则这一轮会按全文档去解析整个
# 文献库: 结论不受影响 (这段只验分类过滤), 但白等很久。
app.var_depth.set("sections")
app._on_depth_change()
app.var_queries.set("sign problem; hubbard model")
# 限定分类现在是复选框: 勾 cond-mat.str-el 这一个
app._set_categories(["cond-mat.str-el"])
check("勾选后 _selected_categories 只报勾上的那个",
      app._selected_categories() == ["cond-mat.str-el"],
      app._selected_categories())
check("勾选后旁边的小字跟着变", "限定 1 个分类" in app.lbl_cats.cget("text"),
      app.lbl_cats.cget("text"))
app.var_top.set(5)
app.update()

t0 = time.time()
app.start_recommend()
deadline = t0 + 900
while app._current_job and time.time() < deadline:
    app.update()
    time.sleep(0.05)
check("推荐任务结束 (没卡死)", not app._current_job, app._current_job)
res = app.result
check("拿到结果", res is not None)
if res is not None:
    print("     code=%s ok=%s msg=%s" % (res.code, res.ok, res.message))
    print("     候选 %d -> 去重后 %d, 排序 %d"
          % (res.stats.candidates_raw, res.stats.candidates_after_dedup,
             len(res.ranked)))
    check("抓到了候选", res.stats.candidates_raw > 0, res.stats.candidates_raw)
    if res.ranked:
        off = [c for c in res.ranked if "cond-mat.str-el" not in c.categories]
        check("界面里分类过滤生效 (无越界)", not off,
              "%d 篇越界, 例: %s" % (len(off), off[0].title if off else ""))
        check("结果列表被填充", len(app.ranked) == len(res.ranked),
              "%d vs %d" % (len(app.ranked), len(res.ranked)))
        rows = app.tree.get_children()
        check("Treeview 有行", len(rows) > 0, len(rows))
        # 列表故意列出**全部**排序结果, 只有前 top_n 篇打"推荐"标记并高亮
        check("Treeview 列出全部排序结果",
              len(rows) == len(res.ranked), "%d vs %d" % (len(rows), len(res.ranked)))
        flagged = [r for r in rows if has_flag(app.tree, r, "推荐")]
        check("恰好 top_n 篇带'推荐'标记", len(flagged) == 5, len(flagged))
        tagged = [r for r in rows if "top" in app.tree.item(r, "tags")]
        check("高亮行 = 推荐行", len(tagged) == 5, len(tagged))
        check("第一行就是推荐篇", has_flag(app.tree, rows[0], "推荐"))
        # 点第一行, 看详解框有没有内容
        app.tree.selection_set(rows[0])
        app.tree.event_generate("<<TreeviewSelect>>")
        app.update()
        detail = app.txt_detail.get("1.0", "end").strip()
        check("双击/选中后详解框有内容", len(detail) > 0, repr(detail[:60]))
    else:
        check("有排序结果", False, "ranked 为空")
print("     耗时 %.1fs" % (time.time() - t0))

print()
print("=" * 70)
print("E) 再跑一次推荐 (验证结果列表会先清空, 不会撞 iid)")
print("=" * 70)
app.var_top.set(3)
app.update()
t0 = time.time()
app.start_recommend()
deadline = t0 + 900
while app._current_job and time.time() < deadline:
    app.update()
    time.sleep(0.05)
check("第二次推荐也没卡死", not app._current_job, app._current_job)
res2 = app.result
if res2 is not None:
    rows2 = app.tree.get_children()
    check("列表被重建而不是追加", len(rows2) == len(res2.ranked),
          "%d vs %d" % (len(rows2), len(res2.ranked)))
    flagged2 = [r for r in rows2 if has_flag(app.tree, r, "推荐")]
    check("top_n 改成 3 后标记也跟着变", len(flagged2) == 3, len(flagged2))
    # 推荐记录: 第二轮跑的是同一批检索式, 所以第一轮的推荐必然在记录里 ——
    # 候选里应该出现"已推荐过", 且其中那些解读过的会复用
    if app.result is not None:
        seen2 = [r for r in rows2 if has_flag(app.tree, r, "已推荐过")]
        check("第二轮能认出'已推荐过'的候选 (推荐记录生效)", len(seen2) > 0,
              len(seen2))
print("     耗时 %.1fs" % (time.time() - t0))

print()
print("=" * 70)
print("E2) 滚动容器 / 停止按钮高亮 / 打开报告的兜底")
print("=" * 70)
from arxiv_rec import ui as ui_mod

out_dir = raw["output"]["dir"]


def page_frame(tab):
    """找出这个标签页里那个可滚动容器返回的 Frame (它身上挂着滚轮绑定)。"""
    for c in all_widgets(tab):
        if not isinstance(c, tk.Canvas):
            continue
        for ch in c.winfo_children():
            if hasattr(ch, "bind_page_wheel"):
                return ch
    return None


for _name, _tab in (("读取文献", app.tab_read), ("文献推荐", app.tab_rec)):
    _page = page_frame(_tab)
    check("%s 页套了可滚动容器 (内容再多也够得着)" % _name, _page is not None)
    _bars = [x for x in all_widgets(_tab) if isinstance(x, ttk.Scrollbar)
             and str(x.cget("orient")) == "vertical"]
    check("%s 页有竖向滚动条" % _name, len(_bars) >= 1, len(_bars))
    # 表格/日志框自己要能抢回滚轮, 否则指针停在结果表上滚的还是整页 ——
    # 表格里第 10 行以下的论文就永远翻不到
    _txt = app.txt_run_log if _name == "文献推荐" else app.txt_read_log
    check("%s 页的日志框接上了滚轮穿透" % _name, bool(_txt.bind("<Enter>")),
          repr(_txt.bind("<Enter>")))

# 运行日志必须在**进度条下面**, 而且排在结果表前面 —— 跑的时候这两样才是
# 用户盯着看的东西 (以前日志在整页最底下, 结果表一占位就把它挤没了)
_page_rec = page_frame(app.tab_rec)
_slaves = _page_rec.pack_slaves() if _page_rec is not None else []


def _idx_of(widget):
    for i, s in enumerate(_slaves):
        if widget in all_widgets(s):
            return i
    return -1


_i_pb, _i_log, _i_tree = (_idx_of(app.pb_run), _idx_of(app.txt_run_log),
                          _idx_of(app.tree))
check("运行日志排在进度条下面", _i_pb >= 0 and _i_log > _i_pb, (_i_pb, _i_log))
check("结果表排在运行日志下面 (日志不会把表格挤走)",
      _i_tree > _i_log >= 0, (_i_log, _i_tree))

# --- 停止按钮: 开跑后必须高亮 ---
app._set_buttons(False)
check("开跑后停止按钮换成实心红 (Stop.TButton)",
      app.btn_stop.cget("style") == "Stop.TButton", app.btn_stop.cget("style"))
check("开跑后停止按钮可点",
      str(app.btn_stop.cget("state")) == "normal", app.btn_stop.cget("state"))
check("开跑后'开始推荐'被置灰",
      str(app.btn_run.cget("state")) == "disabled", app.btn_run.cget("state"))
app.request_stop()
check("点了停止后按钮立刻变成'停止中'",
      "停止中" in app.btn_stop.cget("text"), app.btn_stop.cget("text"))
check("点了停止后按钮自己置灰 (防连点)",
      str(app.btn_stop.cget("state")) == "disabled", app.btn_stop.cget("state"))
app._set_buttons(True)
check("跑完恢复成灰色描边 + 不可点",
      app.btn_stop.cget("style") == "Quiet.TButton"
      and str(app.btn_stop.cget("state")) == "disabled",
      (app.btn_stop.cget("style"), app.btn_stop.cget("state")))
check("跑完按钮文字复位", app.btn_stop.cget("text") == "停止",
      app.btn_stop.cget("text"))
app.stop_event.clear()

# --- 打开报告: 重启之后也要能打开 (去报告目录里挑最新的一份) ---
_opened = []
_orig_open_path = app._open_path
_orig_info = ui_mod.messagebox.showinfo
app._open_path = lambda p: _opened.append(p)
try:
    app.result = None       # 模拟"重启程序之后再点打开报告"
    app.open_report()
    check("重启后点'打开报告'打开的是报告目录里最新那份",
          len(_opened) == 1
          and os.path.basename(_opened[0]).startswith("arxiv_recommend_"),
          _opened)
    check("打开的那份确实在设置里指定的报告目录下",
          bool(_opened) and os.path.dirname(os.path.abspath(_opened[0]))
          == os.path.abspath(out_dir), _opened)

    # 报告目录被清空 -> 提示里必须带上"实际找过的那个目录", 用户才能对上账
    for _f in os.listdir(out_dir):
        if _f.startswith("arxiv_recommend_"):
            os.remove(os.path.join(out_dir, _f))
    _msgs = []
    ui_mod.messagebox.showinfo = lambda t, m, **kw: _msgs.append((t, m))
    _before = len(_opened)
    app.open_report()
    check("报告目录为空时不打开任何东西",
          len(_opened) == _before, _opened)
    check("提示里写着实际找过的报告目录 (能对上账)",
          bool(_msgs) and out_dir in _msgs[0][1], _msgs)
finally:
    ui_mod.messagebox.showinfo = _orig_info
    app._open_path = _orig_open_path

print()
print("=" * 70)
print("E3) 第一次运行: 配置自动生成在程序目录下, 三个位置都落在它旁边")
print("=" * 70)
# 这一条必须**另起一个进程**跑: 它要把 project_root() 假装成另一个目录, 而
# 当前进程里那个 App 正用着真目录; 同一个进程里再建第二个 Tk 窗口也会互相干扰。
_fresh = os.path.join(tmpdir, "fresh_program_dir")
os.makedirs(_fresh)
_fresh_cfg = os.path.join(_fresh, "config.json")
_probe = os.path.join(tmpdir, "first_run_probe.py")
with io.open(_probe, "w", encoding="utf-8") as _fh:
    _fh.write(
        "# -*- coding: utf-8 -*-\n"
        "import io, json, os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from arxiv_rec import config as C\n"
        "# 假装程序就装在 _fresh 里 (打包后 project_root() 返回 exe 所在目录)\n"
        "C.project_root = lambda: %r\n"
        "from arxiv_rec import ui as U\n"
        "U.project_root = C.project_root\n"
        "app = U.App(%r)\n"
        "app.update()\n"
        "p = %r\n"
        "print('EXISTS', os.path.exists(p))\n"
        "if os.path.exists(p):\n"
        "    c = json.load(io.open(p, encoding='utf-8'))\n"
        "    print('INDEX', c['library']['index_db'])\n"
        "    print('HISTORY', c['analysis']['history_db'])\n"
        "    print('OUTPUT', c['output']['dir'])\n"
        "print('BAD', C.check_data_paths(app.cfg))\n"
        "print('ROOT', C.project_root())\n"
        "app.destroy()\n" % (ROOT, _fresh, _fresh_cfg, _fresh_cfg))
import subprocess

_p = subprocess.run([sys.executable, _probe], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, timeout=180)
_out = _p.stdout.decode("utf-8", "replace")
_lines = dict(l.split(" ", 1) for l in _out.splitlines() if " " in l
              and l.split(" ", 1)[0] in ("EXISTS", "INDEX", "HISTORY", "OUTPUT",
                                         "BAD", "ROOT"))
check("第一次运行就在程序目录下生成了 config.json",
      _lines.get("EXISTS") == "True", _out[-400:])
check("读取记录默认是相对名字 (于是落在 exe 旁边)",
      _lines.get("INDEX") == "library_index.sqlite", _lines.get("INDEX"))
check("推荐记录默认是相对名字",
      _lines.get("HISTORY") == "recommend_history.sqlite", _lines.get("HISTORY"))
check("报告目录默认是相对名字",
      _lines.get("OUTPUT") == "output", _lines.get("OUTPUT"))
check("第一次运行不弹'位置找不到'的假警报",
      _lines.get("BAD") == "[]", _lines.get("BAD"))
check("程序目录就是假装的那个 (project_root 跟随 exe)",
      (_lines.get("ROOT") or "").strip().lower() == _fresh.lower(),
      _lines.get("ROOT"))
check("探针进程正常退出", _p.returncode == 0, _p.returncode)

print()
print("=" * 70)
print("F) 关闭")
print("=" * 70)
try:
    app._on_close()
    check("能正常关闭", True)
except Exception as exc:
    check("能正常关闭", False, exc)

shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("=" * 70)
if FAIL:
    print("失败 %d 项:" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
