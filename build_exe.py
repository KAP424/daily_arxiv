#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 daily_arxiv 打包成一个 exe。

    python build_exe.py                # 单文件, 无控制台窗口 (默认)
    python build_exe.py --onedir       # 打成一个文件夹, 启动快得多
    python build_exe.py --console      # 保留控制台窗口, 排查启动问题用
    python build_exe.py --no-config    # 不把 config.json 复制到 exe 旁边
    python build_exe.py --dist-dir out # 产物换个目录 (见下)

产物在 ``dist/`` 下。

上一次的 exe 还开着怎么办
--------------------------------------------------------------------------
Windows 不允许覆盖正在运行的程序。所以打包前会先检查 ``dist/daily_arxiv.exe``
有没有被占用, 被占用了就明说"关掉它再打包", 而不是让 PyInstaller 在后面抛一个
和真实原因隔着好几层的 ``PermissionError``。

不想关也行 —— ``--dist-dir out`` 换个目录打包, 产物在 ``out/``, 互不干扰。

关于"程序把数据放哪"
--------------------------------------------------------------------------
程序把 **exe 自己所在的目录** 当作根目录 (见 ``config.project_root``):
config.json、library_index.sqlite、output/、cache/ 全在那里。所以整个
dist 文件夹拷到别的机器上就能直接用, 文献库索引和设置一起带走 —— 不需要
再装 Python, 也不需要重新读一遍 PDF。

PyInstaller 单文件模式启动时会先把内容解到临时目录, 所以 ``__file__``
指向的是一个进程退出就消失的路径; 早期版本拿它当根目录, 结果是配置读不到、
索引每次重建、报告转身就没。现在冻结时改认 ``sys.executable`` 的目录。

单文件 vs 文件夹
--------------------------------------------------------------------------
单文件就是一个 exe, 好拷贝, 但每次启动都要解包 (numpy + sklearn + PyMuPDF
加起来不小), 首次启动大概几秒。``--onedir`` 出的是一整个文件夹, 启动快,
适合长期放在自己机器上用。默认给单文件, 因为"一个能双击的 exe"最省事。

体积: 用 ``--venv`` 能让 exe 小一大截
--------------------------------------------------------------------------
当前解释器是 conda 环境时, 打出来的 exe 会非常大 (700 MB 量级), 几乎全部
是 ``mkl_*.dll``: conda 的 numpy / scipy 链的是 Intel MKL, 而 MKL 要为每一代
CPU 各带一套内核 (avx512 / avx2 / avx / mc / ...), 加起来 600 MB 上下。

    python build_exe.py --venv

会建一个只装了运行依赖的干净虚拟环境, 用 PyPI 的 wheel (链的是 OpenBLAS,
一个 30 MB 的 dll) 来打包, exe 通常能降到 200 MB 以内。本程序只做 TF-IDF
和余弦相似度, 这点矩阵运算用哪个 BLAS 毫无区别。

**不要**手工去删那些 mkl_*.dll: MKL 靠它们适配 CPU, 删掉在你机器上能跑,
换台机器就会 "DLL load failed"。要么留着, 要么用 ``--venv`` 从根上换掉。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join(ROOT, "ui.py")
NAME = "daily_arxiv"

# 这些包在这台机器上装着, 但本程序一行都没用到。不排除的话 PyInstaller 有
# 可能顺着某个间接引用把它们整包拖进来 —— 光 matplotlib + pandas 就是上百 MB。
#
# **不要**把 unittest 加进来。曾经加过, 结果是打出来的 exe 里 sklearn 直接
# 报 "No module named 'unittest'" —— sklearn 内部有模块 import unittest。
# 而 sklearn 一挂, 相关性排序就退化成关键词重叠 (rank.py 里给所有候选 0.5 分),
# 推荐质量掉一大截, 而且**表面上一切正常**, 不报错, 只是结果变差。
# 排除清单里每一条都要确认"真的没人 import", 这类标准库尤其危险。
EXCLUDES = [
    "matplotlib", "pandas", "PIL", "IPython", "notebook", "jupyter",
    "pytest", "tornado", "zmq", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "wx",
    # 这些是明确的测试包, 运行时不会被 import
    "tkinter.test", "sklearn.tests", "scipy.tests", "numpy.tests",
]


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def human(nbytes: int) -> str:
    v = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return "%.1f %s" % (v, unit)
        v /= 1024.0
    return "%.1f GB" % v


def dir_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(base, fn))
            except OSError:
                pass
    return total


def check_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        log("没有装 PyInstaller。装一下:")
        log("")
        log("    python -m pip install pyinstaller")
        log("")
        log("(如果 pip 卡在 SSL/超时, 加上代理: "
            "python -m pip install --proxy http://127.0.0.1:7890 pyinstaller)")
        return False


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="build_exe.py", description="打包成 exe")
    p.add_argument("--onedir", action="store_true",
                   help="打成文件夹而不是单文件 (启动更快)")
    p.add_argument("--console", action="store_true",
                   help="保留控制台窗口 (默认无窗口; 排查启动问题时可加)")
    p.add_argument("--no-config", action="store_true",
                   help="不把 config.json 复制到 exe 旁边")
    p.add_argument("--keep-build", action="store_true",
                   help="保留 build/ 中间目录")
    p.add_argument("--venv", action="store_true",
                   help="用一个干净的虚拟环境打包, exe 会小很多 (见下)")
    p.add_argument("--refresh-venv", action="store_true",
                   help="配合 --venv: 重建虚拟环境")
    p.add_argument("--proxy", default=None,
                   help="装依赖时用的代理; 不填就取 config.json 里的 network.proxy")
    p.add_argument("--dist-dir", default=None,
                   help="产物目录 (默认 dist/)。上一次的 exe 还开着的时候, 用这个"
                        "换一个目录打包, 免得跟正在运行的那个打架")
    return p.parse_args(argv)


def _looks_like_conda() -> bool:
    """当前解释器是不是 conda 环境 (它的 numpy/scipy 链的是 Intel MKL)。"""
    if os.environ.get("CONDA_PREFIX"):
        return True
    exe = (sys.executable or "").replace("\\", "/").lower()
    return "/envs/" in exe or "/conda" in exe


def warn_bloated_build(packer: str) -> None:
    """没用 --venv 且解释器像是 conda 时, 提前说清楚 exe 会大 3 倍多。

    这不是"提醒一下"就完事的: 实测同一个项目, ``--venv`` 出来 76.9 MB,
    直接拿 conda 环境打是 283 MB —— 差在 ``mkl_*.dll`` 上, 它要为每一代
    CPU (avx512/avx2/avx/mc/...) 各带一套内核。功能完全一样, 本程序只做
    TF-IDF 和余弦相似度, 用哪个 BLAS 都行。不知道这回事的人会以为
    "这程序就这么大", 然后莫名其妙多传 200 MB。
    """
    if not _looks_like_conda():
        return
    log("")
    log("!" * 68)
    log("提示: 现在用的是 conda 环境里的解释器:")
    log("    %s" % packer)
    log("")
    log("conda 的 numpy/scipy 链的是 Intel MKL, 打出来的 exe 会大 3 倍多")
    log("(实测 283 MB, 而 --venv 出来是 77 MB)。功能没有任何区别。")
    log("")
    log("想要小体积, 加一个 --venv:")
    log("")
    log("    python build_exe.py --venv")
    log("")
    log("(第一次会建一个干净的构建环境, 只装运行依赖, 多花一两分钟。)")
    log("!" * 68)
    log("")


def config_proxy() -> str:
    """从 config.json 里读代理 —— 用户已经为抓 arXiv 配过一次, 不用再填。"""
    try:
        import json
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
            return ((json.load(fh).get("network") or {}).get("proxy") or "").strip()
    except Exception:
        return ""


def venv_python(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def ensure_venv(venv_dir: str, proxy: str, refresh: bool) -> str:
    """准备好一个只装了运行依赖的干净环境, 返回它的 python 路径。

    为什么值得多这一步
    ------------------------------------------------------------------
    conda 环境里的 numpy / scipy 链的是 Intel MKL, 光 ``mkl_*.dll`` 就有
    600 MB 上下 (它要为每代 CPU 都带一套内核), 打出来的 exe 会到 700 MB+。
    PyPI 上的 wheel 用的是 OpenBLAS, 一个 30 MB 左右的 dll 搞定 ——
    功能上对本程序 (TF-IDF + 余弦相似度, 这点矩阵运算) 没有任何区别,
    但 exe 能小到四分之一。

    另外这也让产物更"干净": 不会因为 conda 环境里恰好装了什么而被打进去。
    """
    py = venv_python(venv_dir)
    if refresh and os.path.isdir(venv_dir):
        log("删除旧的构建环境 %s" % venv_dir)
        shutil.rmtree(venv_dir, ignore_errors=True)
        py = venv_python(venv_dir)
    if not os.path.exists(py):
        log("创建干净的构建环境: %s" % venv_dir)
        rc = subprocess.call([sys.executable, "-m", "venv", venv_dir])
        if rc != 0:
            raise RuntimeError("创建虚拟环境失败 (退出码 %d)" % rc)
    else:
        log("复用已有的构建环境: %s" % venv_dir)

    pkgs = [
        "requests>=2.25",
        "PyMuPDF>=1.23",
        "numpy>=1.19",
        "scipy>=1.5",            # sklearn 依赖
        "scikit-learn>=0.24",
        "pyinstaller>=6.0,<6.12",
    ]
    cmd = [py, "-m", "pip", "install", "--disable-pip-version-check",
           "--quiet", "--upgrade"] + pkgs
    if proxy:
        cmd += ["--proxy", proxy]
        log("走代理: %s" % proxy)
    log("安装运行依赖 (第一次要下载一会儿, 之后就快了)...")
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        raise RuntimeError(
            "装依赖失败 (退出码 %d)。如果是 SSL/超时, 试试:\n"
            "    python build_exe.py --venv --proxy http://127.0.0.1:7890" % rc)
    return py


def _is_locked(path: str) -> bool:
    """这个文件现在被别的进程占着吗 (Windows 上正跑着的 exe 就是这种状态)。"""
    if not os.path.exists(path):
        return False
    try:
        # 以"可写"方式打开: 被占用的 exe 会直接 PermissionError
        with open(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _remove_dir_safely(path: str) -> str:
    """删掉一个目录: **先改名, 再删**。返回 "" 表示成功, 否则是失败原因 (人话)。

    为什么不能直接 ``rmtree``: 它是**边走边删**的。目录里有东西占着的时候, 已经
    删掉的那部分不会回来 —— 实测踩过两次, 第二次连 ``dist`` 本身都没删掉, 而里面
    的 ``daily_arxiv.exe``、``config.json``、索引已经没了。用户看到的是一句
    "删不掉旧目录", 而他刚才还在用的那个 exe 已经不在磁盘上了。

    最阴的是**占着目录的往往是 exe 自己**: 双击启动的程序, 工作目录就是它所在的
    目录, 于是整个 ``dist`` 都删不掉 —— 而 ``_check_not_running`` 查的是"exe 文件
    本身锁没锁", 在 Windows 上删一个正在运行的 exe 是允许的 (文件被摘掉, 进程照跑),
    所以那道检查会放行, 拦不住这种。

    改名是**一步到位**的: 成了, 说明整个目录没人占着, 接下来慢慢删都行; 失败了
    (WinError 32), 说明有东西占着, 而这时目录**一个字节都没动** —— 正好可以干净
    地退出, 告诉用户"关掉它再打包"。
    """
    if not os.path.isdir(path):
        return ""
    doomed = path + ".delete_me"
    if os.path.isdir(doomed):       # 上一次改名成功但没删干净留下的
        shutil.rmtree(doomed, ignore_errors=True)
    try:
        os.rename(path, doomed)
    except OSError as exc:
        return str(exc)
    shutil.rmtree(doomed, ignore_errors=True)
    if os.path.isdir(doomed):
        # 改名成功 = 目录能换名字, 剩下删不掉的是里面某个**文件** (例如 exe 还在
        # 跑)。不影响这次打包 (新产物写进全新的目录), 所以只提醒一句。
        log("[!] %s 改成了 %s, 但里面还有文件删不掉 —— 打包不受影响, "
            "回头手动删掉那个目录即可。" % (path, os.path.basename(doomed)))
    return ""


def _check_not_running(target_dir: str, name: str) -> bool:
    """打包前先确认上一次的 exe 没在跑, 否则给一句人话再退出。

    不查这一步的话, 失败会以这个面目出现:

        PermissionError: [WinError 5] 拒绝访问: '...\\dist\\daily_arxiv.exe'
          File "...\\PyInstaller\\building\\api.py", line 752, in assemble
            os.remove(self.name)

    而且上面那句 rmtree(ignore_errors=True) 会**默默吞掉**真正的失败原因 (删不掉
    旧目录), 于是 PyInstaller 后面才撞上, 报出一个和真实原因隔着好几层的错。
    用户看到的是 PyInstaller 内部栈, 完全想不到"哦我那个 exe 还开着"。
    """
    exe = os.path.join(target_dir, name + ".exe")
    if not _is_locked(exe):
        return True
    log("")
    log("打包失败: 上一次的 exe 正在运行 ——")
    log("    %s" % exe)
    log("")
    log("Windows 不允许覆盖正在运行的程序。请先关掉它再打包:")
    log("  * 关掉那个窗口 / 在任务管理器里结束 daily_arxiv.exe")
    log("  * 如果是命令行跑的 (daily_arxiv.exe --run), 等它跑完或按 Ctrl-C")
    log("  * 另外, 资源管理器里正预览着这个文件也会锁住它, 换个目录再看")
    log("")
    log("(单文件 exe 运行时会有两个同名进程: 一个是解包器, 一个是真正的程序。"
        "两个都要结束。)")
    log("")
    return False


# 重建会整个删掉产物目录, 但那个目录同时是**用户的数据目录** —— 这几样必须
# 先收起来再删, 构建完放回去。少收一样就是一次静默的数据丢失:
#   config.json            设置页里选过的一切 (分类 / 读取深度 / 三个存放位置)
#   library_index.sqlite   "哪些 PDF 已经读过"的账本 (丢了要重读 200 多篇)
#   recommend_history.sqlite 推荐过哪些论文 + 那篇的解读 (丢了要重花 token)
_DATA_FILES = ("config.json", "library_index.sqlite", "recommend_history.sqlite")
# 目录也得一起收起来。它们装的是**用户的产物**, 不是构建产物:
#   output/  历次报告和 --run 的运行日志
#   cache/   arXiv 与 AI 两层缓存
#
# 以前只收上面那三个文件, 于是每重新打一次包, dist/output/ 里的报告就被下面
# 那句 rmtree 顺手删光了。后果不只是"报告没了": 推荐记录库里存的是报告的**绝对
# 路径**, 「推荐记录」里那几行"作者/提交日期/期刊"在老记录上正是从报告里兜底
# 读出来的 (见 history._report_enrichment) —— 报告一没, 那几列就永远补不回来,
# 用户看到的是"记录里的元信息凭空消失"。
_DATA_DIRS = ("output", "cache")


def _stash_data(dist: str):
    """把产物目录里的用户数据收到临时目录, 返回 ``(临时目录, {名字: 备份路径})``。

    文件是**抄**、目录是**移**。抄是为了构建中途失败时产物目录半毁也还有备份;
    目录只能移 —— cache/ 可能是几百兆, 抄一份既慢又占地方。两者都不会丢:
    构建失败时备份还在原处, 日志里会写出它在哪。
    """
    if not os.path.isdir(dist):
        return "", {}
    mapping = {}
    stash = ""
    for name in _DATA_FILES + _DATA_DIRS:
        src = os.path.join(dist, name)
        is_dir = name in _DATA_DIRS
        if not (os.path.isdir(src) if is_dir else os.path.isfile(src)):
            continue
        if not stash:
            stash = tempfile.mkdtemp(prefix="daily_arxiv_build_")
        try:
            dst = os.path.join(stash, name)
            if is_dir:
                shutil.move(src, dst)
            else:
                shutil.copyfile(src, dst)
            mapping[name] = dst
        except Exception as exc:
            log("备份 %s 失败 (%s), 它会丢 —— 构建前先手动抄一份。" % (name, exc))
    return stash, mapping


def _restore_data(saved, target_dir: str):
    """把备份放回产物目录, 返回成功放回的名字列表。"""
    done = []
    for name, src in (saved or {}).items():
        dst = os.path.join(target_dir, name)
        try:
            if os.path.isdir(src):
                # 目标位置理论上刚被 rmtree 过, 不该有东西; 真要有就只能是这次
                # 构建自己造出来的空壳, 清掉让用户的目录原样落回去。
                if os.path.isdir(dst):
                    shutil.rmtree(dst, ignore_errors=True)
                shutil.move(src, dst)
            else:
                shutil.copyfile(src, dst)
            done.append(name)
        except Exception as exc:
            log("放回 %s 失败 (%s); 备份还在 %s, 手动抄过去即可。"
                % (name, exc, src))
    return done


def main(argv=None) -> int:
    args = parse_args(argv)

    dist = os.path.abspath(args.dist_dir) if args.dist_dir \
        else os.path.join(ROOT, "dist")
    # 中间目录跟着产物目录走。用同一个 build/ 的话, 两次打包会互相踩 ——
    # 一次 --dist-dir 的构建会把默认构建的中间产物覆盖掉。
    build = (dist + "_build") if args.dist_dir else os.path.join(ROOT, "build")

    # 打包用的解释器: 默认就是当前这个, --venv 时换成干净环境里的那个
    packer = sys.executable
    if args.venv:
        proxy = args.proxy or config_proxy()
        try:
            packer = ensure_venv(os.path.join(ROOT, ".build_venv"), proxy,
                                 args.refresh_venv)
        except Exception as exc:
            log("")
            log(str(exc))
            return 1
        log("")
    else:
        if not check_pyinstaller():
            return 1
        warn_bloated_build(packer)

    # 先确认旧 exe 没在跑 —— 否则下面 rmtree 会默默失败, 一路错到 PyInstaller
    # 内部才炸出一个和真实原因毫无关系的栈。
    if not _check_not_running(dist, NAME):
        return 1

    # 每次全量重建: PyInstaller 对"改了依赖之后"的增量缓存经常认错, 出现
    # "明明改了代码, exe 里还是旧的"这种问题, 排查起来比多等一会儿贵得多。
    #
    # 但 dist 目录里不只是构建产物 —— 它同时是**用户的数据目录** (config.json /
    # library_index.sqlite / recommend_history.sqlite)。全量重建会连它们一起删掉,
    # 于是"重新打了个包"顺手把用户设置好的分类、读取深度、三个存放位置和读过
    # 哪些 PDF 的账本一起清零。所以先把这几样抄到临时目录, 构建完再放回去。
    stash_dir, saved = _stash_data(dist)
    if saved:
        log("产物目录里已有的用户数据先收起来了: %s" % ", ".join(sorted(saved)))
    for d in (dist, build):
        why = _remove_dir_safely(d)
        if not why:
            continue
        # 删不干净就直说。吞掉它的话, 后面 PyInstaller 会在一个半新半旧的目录上
        # 干活, 报出来的错完全指不到这里。
        log("")
        log("删不掉旧目录 %s: %s" % (d, why))
        log("")
        log("多半是有程序正占着它。最常见的就是上一次的 exe 还开着 —— 双击启动的")
        log("程序, 工作目录就是它自己所在的目录, 所以整个 dist 都删不掉。请先关掉它")
        log("(任务管理器里结束 daily_arxiv.exe; 单文件 exe 运行时有两个同名进程, "
            "两个都要结束)。")
        log("资源管理器里正开着这个文件夹也会占着它, 换个目录再看。")
        log("")
        # 目录本身**一个字节都没动** (见 _remove_dir_safely), 但上面已经把 output/
        # 和 cache/ **挪**进备份目录了 —— 不放回去的话, "打包失败"会顺带把用户的
        # 报告和缓存搬走, 而日志只说了一句"删不掉"。
        back = _restore_data(saved, dist)
        if back:
            log("已把 %s 放回原处 —— 产物目录和你打包前一样, 什么都没丢。"
                % ", ".join(sorted(back)))
        if stash_dir:
            if len(back) == len(saved):
                shutil.rmtree(stash_dir, ignore_errors=True)
            else:
                log("有东西没放回去, 备份留在 %s。" % stash_dir)
        return 1

    cmd = [packer, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--name", NAME,
           "--onedir" if args.onedir else "--onefile",
           "--console" if args.console else "--windowed",
           # 让 PyInstaller 能找到 arxiv_rec 包 (脚本在项目根目录)
           "--paths", ROOT,
           "--distpath", dist,
           "--workpath", build,
           "--specpath", build]
    for mod in EXCLUDES:
        cmd += ["--exclude-module", mod]
    # 赞赏码 ("支持一下"页显示的那张)。打进 exe 里, 这样光一个 exe 拷到别的
    # 机器上也能显示出来 —— 不带的话那页会是一句"没找到图片"。
    # 下面还会再抄一份到 exe 旁边, 让用户看得见、也能自己换一张。
    reward_src = os.path.join(ROOT, "reward.png")
    if os.path.exists(reward_src):
        cmd += ["--add-data", reward_src + os.pathsep + "."]
    cmd.append(ENTRY)

    log("=" * 68)
    log("打包 %s (%s, %s)" % (NAME,
                              "文件夹" if args.onedir else "单文件",
                              "带控制台" if args.console else "无控制台"))
    log("=" * 68)
    log(" ".join(cmd))
    log("")
    rc = subprocess.call(cmd, cwd=ROOT)
    if rc != 0:
        log("")
        log("打包失败 (退出码 %d)。上面应该有 PyInstaller 的具体报错。" % rc)
        return rc

    # --- 产物 ---
    if args.onedir:
        exe = os.path.join(dist, NAME, NAME + ".exe")
        target_dir = os.path.join(dist, NAME)
    else:
        exe = os.path.join(dist, NAME + ".exe")
        target_dir = dist
    if not os.path.exists(exe):
        log("没找到产物 %s —— PyInstaller 应该报过错了。" % exe)
        return 1

    # --- 赞赏码也放一份在 exe 旁边 ---
    # 它已经打进 exe 里了 (见上面 --add-data), 这里再放一份是为了两件事: 界面
    # 上会显示这个文件的实际路径, 以及"想换一张赞赏码"的人可以直接替换它。
    if os.path.exists(reward_src):
        try:
            shutil.copyfile(reward_src, os.path.join(target_dir, "reward.png"))
            log("")
            log("已把 reward.png 放到 %s (「支持一下」页显示的那张)。" % target_dir)
        except Exception as exc:
            log("复制 reward.png 失败 (%s); exe 里还带着一份, 不影响显示。" % exc)

    # --- 把配置和已有索引放到 exe 旁边 ---
    # 索引一起带过去很关键: 它是"哪些 PDF 已经读过了"的账本, 不带的话
    # exe 第一次跑「读取文献」要把 200 多篇 PDF 从头解析一遍 (几十秒到几分钟);
    # 带过去就只做增量检查, 秒级完成。
    if not args.no_config:
        # 先把构建前收起来的那几样放回去 —— 它们才是"用户正在用的"。
        restored = _restore_data(saved, target_dir)
        if restored:
            log("")
            log("已放回原有的 %s。" % ", ".join(sorted(restored)))
            log("    (要重置成仓库根目录那份配置: 先删掉 %s 再打一次)"
                % os.path.join(target_dir, "config.json"))

        src = os.path.join(ROOT, "config.json")
        dst = os.path.join(target_dir, "config.json")
        if "config.json" in restored:
            pass
        elif os.path.exists(src):
            try:
                shutil.copyfile(src, dst)
                log("")
                log("[!] 已把 config.json 复制到 %s" % target_dir)
                log("[!] 里面有你的 API key —— 把这个 exe 发给别人之前记得删掉它,")
                log("    或者用 --no-config 重新打包。")
            except Exception as exc:
                log("复制 config.json 失败 (%s), 不影响 exe 本身。" % exc)
        else:
            log("")
            log("没找到 config.json, 跳过。首次运行后在「设置」页填好再保存,")
            log("会在 exe 旁边生成一个。")

        idx_src = os.path.join(ROOT, "library_index.sqlite")
        if "library_index.sqlite" not in restored and os.path.exists(idx_src):
            try:
                shutil.copyfile(idx_src,
                                os.path.join(target_dir, "library_index.sqlite"))
                log("已把 library_index.sqlite 复制过去 —— 之前读过的 PDF 不用重读。")
            except Exception as exc:
                log("复制索引失败 (%s), exe 第一次读取文献时会重建。" % exc)
    if stash_dir:
        if saved and args.no_config:
            # --no-config 是"打一份干净的、能发给别人的包", 所以收起来的东西
            # 不放回去。但**必须说出来** —— 沉默地删掉别人的报告和索引, 用户
            # 只会觉得"打了个包, 我的东西没了"。
            log("")
            log("[!] --no-config: 产物目录里原有的 %s 没有放回 (干净的包不带用户数据)。"
                % ", ".join(sorted(saved)))
            log("    要留着它们就别加 --no-config; 现在去 %s 里手动抄。" % stash_dir)
        else:
            shutil.rmtree(stash_dir, ignore_errors=True)

    if not args.keep_build:
        shutil.rmtree(build, ignore_errors=True)

    size = (dir_size(target_dir) if args.onedir
            else os.path.getsize(exe))
    log("")
    log("=" * 68)
    log("完成: %s" % exe)
    log("大小: %s" % human(size))
    log("=" * 68)
    log("")
    log("双击就能用。程序把数据放在 exe 所在的目录:")
    log("    config.json            设置 (第一次保存后生成)")
    log("    library_index.sqlite   文献索引 (读过的 PDF 不用再读)")
    log("    recommend_history.sqlite  推荐记录 (推荐过什么、解读都在这)")
    log("    output/                生成的报告")
    log("    output/run_<时间>.log   每次 --run 的完整日志 (没有控制台时看这个)")
    log("    crash.log              万一崩了, 原因写在这里")
    log("    reward.png             「支持一下」页的赞赏码 (想换一张就替换它)")
    log("")
    log("整个 dist 文件夹拷到别的机器上照样能跑, 不需要装 Python。")
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    sys.exit(main())
