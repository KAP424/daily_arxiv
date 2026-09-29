# -*- coding: utf-8 -*-
"""历史推荐结果: 把 output 目录里的报告读回来, 在「文献列表」页翻看。

为什么不另建一份"推荐结果数据库"?

  * 报告文件本来就在那里。用户看到的、发给别人的、删掉的, 都是这些 .md ——
    再维护一份平行的记录, 只会多出一处"文件和记录对不上"的可能。
  * 早先跑出来的报告同样能翻。另建数据库的话, 只有改动之后新跑的几轮才有得看。
  * 报告里该有的都有: 标题、作者、提交日期、期刊、总分、引用数、标记。

代价是得**解析 Markdown**。所以这里的解析写得比较宽: 认不出来的一律留空, 而不是
抛异常 —— 一份手改过的、或者将来格式变了的报告, 顶多是某一列显示成破折号, 不该
让整个「文献列表」页打不开。
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from .config import project_root, resolve_path
from .utils import log

# arxiv_recommend_20260929_105755.md / arxiv_recommend_20260929_105755_demo.md
_RUN_FILE = re.compile(r"^arxiv_recommend_(\d{8})_(\d{6})(?:_(.*))?\.md$")
# 表格数据行: "| 1 | [标题](url) | 2026-09-25 | 0.98 | 0.00 | 1.00 | **0.788** | 440 | ⭐经典 |"
_ROW = re.compile(r"^\|\s*(\d+)\s*\|")
# 详解小标题: "### 1. 论文标题"
_DETAIL = re.compile(r"^###\s+(\d+)\.\s*(.+?)\s*$")
# "**作者**: A, B · **提交**: 2010-01-13 · **最近更新**: ..."
_PAIR = re.compile(r"\*\*([^*]+)\*\*\s*[:：]\s*(.*)")
_LINK = re.compile(r"\[(.*?)\]\((https?://[^)]+)\)")
_STAMP = re.compile(r"生成时间\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# 报告里标记列用的表情, 换成和「文献推荐」页一致的纯文字, 免得同一批论文在
# 两个页面里长得不一样。
_FLAG_TEXT = {"🆕新": "新", "⭐经典": "经典", "📖已发表": "已发表"}


def output_dir(cfg: Dict[str, Any]) -> str:
    """报告所在目录 (和 write_report 用同一套解析规则)。"""
    return resolve_path((cfg.get("output") or {}).get("dir", "output")
                        or "output", project_root())


def _clean_flag(raw: str) -> str:
    """把报告里的标记列转成纯文字。"""
    out = []
    for tok in (raw or "").split():
        out.append(_FLAG_TEXT.get(tok, tok))
    text = " ".join(out).strip()
    return "" if text == "—" else text


def _split_pairs(line: str) -> Dict[str, str]:
    """把一行里用 " · " 拼起来的 ``**键**: 值`` 拆开。

    只认 ``·`` **后面紧跟 ``**``** 的分隔符 —— 备注那类自由文本里也可能有
    " · ", 一刀切会把它们拦腰截断。
    """
    out: Dict[str, str] = {}
    for part in re.split(r"\s+·\s+(?=\*\*)", line or ""):
        m = _PAIR.match(part.strip())
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _first_author(authors: str) -> str:
    """报告里的作者串是 "A, B, C 等", 列表那一列窄, 只留第一位。"""
    text = (authors or "").strip()
    if not text or text == "—":
        return "—"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0]
    return "%s 等" % parts[0]


def _split_cats(raw: str) -> List[str]:
    """报告的 ``**分类**: cond-mat.str-el, hep-th`` 一行拆成列表。"""
    text = (raw or "").strip()
    if not text or text == "—":
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def parse_report(text: str) -> Dict[str, Any]:
    """把一份报告的正文解析成 ``{"generated_at", "items"}``。

    ``items`` 里每项是一个 dict, 字段和报告表格/详解能对上的那些。认不出来的
    字段留空串, 调用方按"没有这项"显示破折号。
    """
    lines = (text or "").splitlines()
    out: Dict[str, Any] = {"generated_at": "", "items": []}
    m = _STAMP.search(text or "")
    if m:
        out["generated_at"] = m.group(1)

    # --- 第一遍: 表格 (名次 -> 分数/引用/标记/链接/日期) ---
    rows: Dict[int, Dict[str, str]] = {}
    in_table = False
    for line in lines:
        if line.startswith("| # |") or line.startswith("|  #  |"):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            # 表格结束 (分隔行之后遇到空行或标题)
            if rows:
                break
            continue
        if set(line) <= set("|-: "):
            continue                      # "| ---: | --- | ..." 分隔行
        rm = _ROW.match(line)
        if not rm:
            continue
        rank = int(rm.group(1))
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        link = _LINK.search(cells[1] if len(cells) > 1 else "")
        rows[rank] = {
            "title": (link.group(1) if link else
                      (cells[1] if len(cells) > 1 else "")).replace("\\|", "|"),
            "url": link.group(2) if link else "",
            "date": cells[2] if len(cells) > 2 else "",
            "score": (cells[6] if len(cells) > 6 else "").replace("*", ""),
            "citations": cells[7] if len(cells) > 7 else "",
            "flags": _clean_flag(cells[8] if len(cells) > 8 else ""),
        }

    # --- 第二遍: 详解段 (作者/期刊/更完整的标题) ---
    details: Dict[int, Dict[str, str]] = {}
    cur: Optional[Dict[str, str]] = None
    cur_rank = 0
    for line in lines:
        dm = _DETAIL.match(line)
        if dm:
            cur_rank = int(dm.group(1))
            cur = {"title": dm.group(2).strip()}
            details[cur_rank] = cur
            continue
        if cur is None:
            continue
        # 下一篇论文的小标题 (#### 之类) 之后就不用再看了
        if line.startswith("#### "):
            continue
        if line.startswith("### "):
            cur = None
            continue
        for key, val in _split_pairs(line).items():
            if key in ("作者", "提交", "期刊", "引用数", "分类"):
                cur[key] = val

    # --- 合并 ---
    items: List[Dict[str, Any]] = []
    for rank in sorted(set(rows) | set(details)):
        row = rows.get(rank, {})
        det = details.get(rank, {})
        date = det.get("提交") or row.get("date") or ""
        journal = det.get("期刊") or ""
        items.append({
            "rank": rank,
            # 详解里的标题是完整的, 表格里的被截到 78 字
            "title": (det.get("title") or row.get("title") or "").strip(),
            "url": row.get("url") or "",
            "authors": _first_author(det.get("作者") or ""),
            "date": "" if date == "—" else date,
            "journal": "" if journal == "—" else journal,
            # 分类解析成列表 (报告里写的是 "cond-mat.str-el, hep-th" 这种一行)。
            # 「推荐记录」按钮要把记录铺成列表, 而老记录里没存分类 —— 有这一项,
            # 那些行才能从当时的报告里把主分类补出来, 详解里不会缺一格。
            "categories": _split_cats(det.get("分类") or ""),
            "score": row.get("score") or "",
            "citations": row.get("citations") or "",
            "flags": row.get("flags") or "",
        })
    out["items"] = items
    return out


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception as exc:
        log("读不到报告 %s: %s" % (path, exc), "warn")
        return ""


def load_run(path: str) -> Dict[str, Any]:
    """读一份报告, 返回 ``{"path", "label", "generated_at", "items"}``。"""
    parsed = parse_report(_read(path))
    return {
        "path": path,
        "label": label_for(path, parsed.get("generated_at", ""),
                           len(parsed["items"])),
        "generated_at": parsed.get("generated_at", ""),
        "items": parsed["items"],
    }


def label_for(path: str, generated_at: str, n: int) -> str:
    """下拉框里那一行字: 时间 + 篇数 + 文件名后缀。

    时间优先用报告正文里写的"生成时间", 读不出来就退回文件名里的时间戳 ——
    文件可能是从别处拷来的, 但文件名的时间戳至少还是它自己的。
    """
    when = generated_at
    if not when:
        m = _RUN_FILE.match(os.path.basename(path))
        if m:
            try:
                when = datetime.strptime(m.group(1) + m.group(2),
                                         "%Y%m%d%H%M%S").strftime(
                    "%Y-%m-%d %H:%M:%S")
            except ValueError:
                when = ""
    tag = ""
    m = _RUN_FILE.match(os.path.basename(path))
    if m and m.group(3):
        tag = " · %s" % m.group(3)
    return "%s · %d 篇%s" % (when or os.path.basename(path), n, tag)


def list_runs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """扫一遍输出目录, 返回所有历史推荐结果 (新的在前)。

    每份都要完整读一遍才能知道推荐了几篇 —— 报告是几十 KB 的文本, 本地盘上
    几十份也就几十毫秒。真放在网络盘上会慢一些, 所以界面里是**按需刷新**
    (切到这一页时扫一次), 不是每次敲键盘都扫。
    """
    out_dir = output_dir(cfg)
    if not out_dir or not os.path.isdir(out_dir):
        return []

    found: List[Dict[str, Any]] = []
    try:
        names = os.listdir(out_dir)
    except Exception as exc:
        log("扫不了输出目录 %s: %s" % (out_dir, exc), "warn")
        return []

    for name in names:
        if not _RUN_FILE.match(name):
            continue
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        parsed = parse_report(_read(path))
        found.append({
            "path": path,
            "generated_at": parsed.get("generated_at", ""),
            "label": label_for(path, parsed.get("generated_at", ""),
                               len(parsed["items"])),
            "items": parsed["items"],
        })

    # 报告正文里的生成时间是最准的排序键; 没有就用文件修改时间
    def _key(run: Dict[str, Any]) -> str:
        when = run.get("generated_at") or ""
        if when:
            return when
        try:
            return datetime.fromtimestamp(
                os.path.getmtime(run["path"])).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    found.sort(key=_key, reverse=True)
    return found
