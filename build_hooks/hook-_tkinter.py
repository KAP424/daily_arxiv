# -*- coding: utf-8 -*-
"""顶掉 PyInstaller 自带的 ``hook-_tkinter.py``: 只收 Tcl/Tk 里用得着的那些数据文件。

这个文件由 ``build_exe.py`` 用 ``--additional-hooks-dir`` 挂进去。

为什么要顶掉它
--------------------------------------------------------------------------
自带那份只有一行 ``hook_api.add_datas(tcltk_info.data_files)`` —— 把 Tcl/Tk
的整个库目录原样收进来。本机实测一共 912 个文件, 其中

    _tcl_data/tzdata     606 个   Tcl 的时区数据库
    _tcl_data/msgs       127 个   Tcl 的本地化消息 (几十种语言各一份)
    _tk_data/msgs         16 个   Tk 的同上

这三样占掉 749 个。单文件 exe **每次启动都要把这些文件解到临时目录**, 而本程序
一个都用不到:

* ``tzdata`` 只有 Tcl 的 ``clock`` 命令按**名字**取时区时才读 (``clock format
  ... -timezone Asia/Shanghai``)。本程序的日期全是 Python ``datetime`` 自己算
  的, 从不碰 Tcl 的 clock, 本地时区也是 C 运行库给的。缺了它最坏情况是"某个
  时区名查不到", 不会静默算错。
* ``msgs`` 是 Tcl/Tk 弹错误框时查的翻译表; 缺了只是退回英文原文, 功能不变。

省下来的字节只有 0.9 MB 左右, 但省下来的**文件数**是 749 个 —— 全部 1318 个
条目里的一大半。单文件启动那几秒里, 相当一部分就是解这些小文件的开销
(实测: 源码方式 ``ui.py --check`` 0.80 秒, exe 3.72 秒, 差的 2.9 秒几乎全是
解包)。

``_tcl_data/encoding`` (78 个) 和 ``_tk_data/images`` (13 个) **留着**: 前者是
Tcl 读写非 UTF-8 文本时要查的编码表, 后者是 ttk 主题和图标用的, 都不值得为
几十 KB 去赌。

怎么生效
--------------------------------------------------------------------------
``--additional-hooks-dir`` 给的目录优先级**高于** PyInstaller 自带 hooks
(``PyInstaller/depend/analysis.py`` 里 HOOK_PRIORITY_USER_HOOKS), 而且同一个
模块只保留优先级最高的那一份 hook —— 所以这里的同名 ``hook-_tkinter.py`` 会把
自带那份整个顶掉, 不是叠加。

**别把这个文件删掉**: 不在的话构建照样能过, 只是悄悄退回自带 hook —— exe 大
回去、启动慢回去, 而且不会有任何提示。

数据文件元组的形状是 ``(目标相对路径, 源文件路径, 'DATA')`` —— **目标在前**,
见 ``PyInstaller/utils/hooks/tcl_tk.py`` 的 ``_collect_files_from_directory``。
"""
from PyInstaller import log as logging
from PyInstaller.utils.hooks.tcl_tk import tcltk_info

# 库目录下的一级子目录名 -> 整个不要
SKIP_DIRS = ("tzdata", "msgs")


def _wanted(dest: str) -> bool:
    """``dest`` 形如 ``_tcl_data\\tzdata\\Africa\\Abidjan`` 或 ``_tcl_data\\auto.tcl``。"""
    parts = dest.replace("\\", "/").split("/")
    # parts[0] 是 _tcl_data / _tk_data, parts[1] 才是库目录下的一级子目录
    return not (len(parts) > 1 and parts[1] in SKIP_DIRS)


def hook(hook_api):
    kept = [entry for entry in tcltk_info.data_files if _wanted(entry[0])]
    dropped = len(tcltk_info.data_files) - len(kept)
    hook_api.add_datas(kept)
    # 打一行日志, 免得"到底砍掉多少"只能靠翻产物去猜
    logger = logging.getLogger(__name__)
    logger.info("Tcl/Tk 数据文件: 收 %d 个, 砍掉 %d 个 (tzdata / msgs)",
                len(kept), dropped)
