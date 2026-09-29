"""推荐记录: 记住推荐过哪些论文, 下次不再重复解读。

和 AI 缓存的分工
--------------------------------------------------------------------------
``network.cache_dir`` 那层缓存按**提示词原文**做键, 提示词里含研究画像和论文
摘要 —— 改一次读取深度、换一条检索式, 画像就变了, 同一篇论文的解读会被重新
调用一次。这层缓存省的是"一模一样地重跑一遍"。

推荐记录按 **arXiv ID** 记, 不依赖提示词是否逐字相同, 所以能跨画像改动复用。
但也正因如此, 复用时**必须核对画像指纹**: 拿"按旧画像写的解读"冒充新画像的
结论, 比重新调用一次更糟 —— 报告里会写出一堆和当前研究方向对不上的关联。

所以这里的规则是:
  * 推荐过没有 —— 只看 ID (用来标记"已推荐"、可选地跳过)
  * 解读能不能复用 —— 必须 ID 相同**且**画像指纹一致

记录的是"信息"而不是"原始数据": 标题、分数、时间、报告路径、以及那篇的解读
正文。全部存在一个 sqlite 文件里, 路径可配置 (设置页的「推荐记录」)。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import project_root, resolve_path
from .utils import extract_arxiv_id, log

SCHEMA_VERSION = 2

# 2 -> 1 之后加的五列 (authors/published/journal/categories/citations):
# 「推荐记录」按钮现在把这张表**当成列表铺出来** (见 rows_as_candidates), 而
# 只有标题、分数、时间的话, 那一排"作者/提交日期/期刊"全是破折号 —— 看着像坏
# 了。这五列补上之后, 记录本身就能独立拼出列表, 不必再去翻当时的报告 (报告是
# 会被删的)。
#
# 迁移是**加列**而不是重建: 这张表存的是用户积累下来的推荐历史, 和索引那种
# "删了重扫就有"的缓存不是一回事 —— 绝对不能照搬 pdf_library 里"版本变了就
# DROP TABLE"的做法, 那等于把记录清空。见 _migrate。
_ADDED_COLUMNS = (
    ("authors", "TEXT NOT NULL DEFAULT ''"),     # JSON 数组
    ("published", "TEXT NOT NULL DEFAULT ''"),   # v1 提交时间 (ISO)
    ("journal", "TEXT NOT NULL DEFAULT ''"),
    ("categories", "TEXT NOT NULL DEFAULT ''"),  # JSON 数组
    ("citations", "INTEGER"),
)

_DDL = """
CREATE TABLE IF NOT EXISTS recommended (
    arxiv_id    TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    first_at    TEXT NOT NULL DEFAULT '',   -- 第一次被推荐的时间
    last_at     TEXT NOT NULL DEFAULT '',   -- 最近一次被推荐的时间
    times       INTEGER NOT NULL DEFAULT 0,
    best_score  REAL NOT NULL DEFAULT 0,
    report      TEXT NOT NULL DEFAULT '',   -- 最近一次把它写进去的报告
    profile_fp  TEXT NOT NULL DEFAULT '',   -- 存解读时用的画像指纹
    summary     TEXT NOT NULL DEFAULT '',   -- 内容讲解
    connections TEXT NOT NULL DEFAULT '[]', -- 与你文献的关联
    ideas       TEXT NOT NULL DEFAULT '',   -- 可结合的研究方向
    analyzed    INTEGER NOT NULL DEFAULT 0,
    authors     TEXT NOT NULL DEFAULT '',
    published   TEXT NOT NULL DEFAULT '',
    journal     TEXT NOT NULL DEFAULT '',
    categories  TEXT NOT NULL DEFAULT '',
    citations   INTEGER
);
CREATE INDEX IF NOT EXISTS recommended_last ON recommended(last_at);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def history_path(cfg: Dict[str, Any]) -> str:
    """推荐记录库的路径 (相对路径按数据根目录解析)。"""
    raw = ((cfg.get("analysis") or {}).get("history_db")
           or "recommend_history.sqlite")
    return resolve_path(raw)


def enabled(cfg: Dict[str, Any]) -> bool:
    return bool((cfg.get("analysis") or {}).get("use_history", True))


def report_dir(cfg: Dict[str, Any]) -> str:
    """报告目录 —— 和 write_report / past_runs.output_dir 同一套解析规则。

    这里自己算一遍而不是调 past_runs.output_dir: 那个模块是**界面**那一侧的东西
    (翻看历史报告), 而记录库这一侧不该为了拿一个目录去依赖它。
    """
    return resolve_path((cfg.get("output") or {}).get("dir", "output")
                        or "output", project_root())


def _json_list(text: Any) -> List[str]:
    """存进去的 JSON 数组读回来。读不出来就当空 —— 一份手改过的库不该让列表打不开。"""
    try:
        val = json.loads(text or "[]")
    except Exception:
        return []
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if str(x).strip()]


def _parse_dt(text: Any) -> Optional[datetime]:
    """``published`` 那一列读回成 datetime。认不出来返回 None (不是报错)。"""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _iso_text(value: Any) -> str:
    """提交日期 -> 库里存的字符串。datetime 走 ISO, 别的原样转字符串。

    ``record`` 和 ``update_meta`` 两条写路径都过这里 —— 同一个字段两种写法的话,
    读回来就会出现"有的行认得出日期、有的认不出"。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def _meta_columns(cand: Any) -> Tuple[str, str, str, str, Optional[int]]:
    """要落库的那五列元信息: (作者, 提交日期, 期刊, 分类, 引用数)。

    作者和分类存 JSON 数组, 不存逗号分隔的字符串: 作者名本身就带逗号的
    ("Smith, Jr.") 会被拆错, 而且读回来还得再猜一次分隔符。
    """
    pub = _iso_text(getattr(cand, "published", None))
    cites = getattr(cand, "citations", None)
    try:
        cites = None if cites is None else int(cites)
    except (TypeError, ValueError):
        cites = None
    return (
        json.dumps([str(a) for a in (getattr(cand, "authors", None) or [])],
                   ensure_ascii=False),
        pub,
        str(getattr(cand, "journal_ref", "") or ""),
        json.dumps([str(c) for c in (getattr(cand, "categories", None) or [])],
                   ensure_ascii=False),
        cites,
    )


def profile_fingerprint(profile: Any) -> str:
    """画像指纹: 只看真正会改变解读内容的那几项。

    刻意**不**包含 generated_by (模型名) —— 换个模型重跑, 已有的解读仍然
    是针对同一个研究方向的, 没必要重来。
    """
    if profile is None:
        return ""
    payload = {
        "q": list(getattr(profile, "queries", []) or []),
        "t": list(getattr(profile, "topics", []) or []),
        "m": list(getattr(profile, "methods", []) or []),
        "k": list(getattr(profile, "keywords", []) or []),
        "s": (getattr(profile, "summary", "") or "").strip(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


class RecommendHistory:
    """推荐记录库。用 ``with`` 打开, 任何失败都只记日志、不影响这一轮运行。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_DDL)
        self._migrate()
        self.conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('schema', ?)",
                          (str(SCHEMA_VERSION),))
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列。**只加列, 绝不删行**。

        ``CREATE TABLE IF NOT EXISTS`` 遇到已存在的表直接跳过, 所以 v1 的库
        (没有 authors/published/journal/categories/citations) 光靠 _DDL 是补不上
        新列的, 一读就 "no such column: authors"。这里按 PRAGMA 挨个补齐。

        为什么不学 pdf_library 那套"版本不一致就 DROP TABLE 重建": 那张表是
        缓存, 删了重扫一遍就有; 这张表是用户攒下来的推荐历史, 删了就真没了 ——
        宁可少几列也不能清空。
        """
        try:
            have = set(r["name"] for r in
                       self.conn.execute("PRAGMA table_info(recommended)"))
        except Exception as exc:                # 表都读不出来就别硬改了
            log("读不出推荐记录表结构: %s" % exc, "warn")
            return
        for name, decl in _ADDED_COLUMNS:
            if name in have:
                continue
            try:
                self.conn.execute("ALTER TABLE recommended ADD COLUMN %s %s"
                                  % (name, decl))
                log("推荐记录库补了一列: %s" % name)
            except Exception as exc:
                # 并发打开时另一个进程可能刚好补过, 报错不致命
                log("给推荐记录库补列 %s 失败: %s" % (name, exc), "warn")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "RecommendHistory":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 读 ---------------------------------------------------------------
    def known(self) -> Dict[str, Dict[str, Any]]:
        """一次读出全部记录, 返回 ``{arxiv_id: {...}}``。

        刻意**一次性读成普通 dict**: 解读阶段是多线程跑的, sqlite 连接默认
        不允许跨线程使用, 而且每篇都去查一次库在这个规模上纯属浪费。读完之后
        线程里只碰内存里的 dict。
        """
        out: Dict[str, Dict[str, Any]] = {}
        try:
            for row in self.conn.execute("SELECT * FROM recommended"):
                out[row["arxiv_id"]] = dict(row)
        except Exception as exc:
            log("读取推荐记录失败: %s" % exc, "warn")
        return out

    def counts(self) -> Dict[str, int]:
        out = {"total": 0, "analyzed": 0}
        try:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n, SUM(analyzed) AS a FROM recommended"
            ).fetchone()
            out["total"] = int(row["n"] or 0)
            out["analyzed"] = int(row["a"] or 0)
        except Exception:
            pass
        return out

    def last_run(self) -> str:
        try:
            row = self.conn.execute(
                "SELECT MAX(last_at) AS t FROM recommended").fetchone()
            return str(row["t"] or "")
        except Exception:
            return ""

    def recent(self, limit: int = 8) -> List[Dict[str, Any]]:
        """最近推荐过的几篇。"""
        return self.rows(limit=limit)

    def rows(self, limit: int = 0) -> List[Dict[str, Any]]:
        """全部记录, 最近推荐的排前面。

        排序必须给**三级**: 同一轮里写进去的所有行, ``last_at`` 是同一个时间戳
        (整轮只在结尾写一次), 只按它排的话, 同一份记录每次点开顺序都可能不一样
        —— 用户会以为记录被谁动过。所以再按分数、最后按 ID 兜底, 让顺序稳定。
        """
        sql = ("SELECT * FROM recommended "
               "ORDER BY last_at DESC, best_score DESC, arxiv_id")
        args: Tuple[Any, ...] = ()
        if limit and limit > 0:
            sql += " LIMIT ?"
            args = (int(limit),)
        try:
            return [dict(r) for r in self.conn.execute(sql, args)]
        except Exception as exc:
            log("读取推荐记录失败: %s" % exc, "warn")
            return []

    # -- 写 ---------------------------------------------------------------
    def record(self, candidates: Iterable[Any], report_path: str = "",
               profile_fp: str = "") -> Tuple[int, int]:
        """把这一轮推荐的论文写进记录, 返回 (新增, 更新)。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new = upd = 0
        for cand in candidates:
            aid = (getattr(cand, "arxiv_id", "") or "").strip()
            if not aid:
                continue
            try:
                row = self.conn.execute(
                    "SELECT times, best_score, analyzed FROM recommended "
                    "WHERE arxiv_id = ?", (aid,)).fetchone()
                analyzed = 1 if getattr(cand, "analyzed", False) else 0
                # 元信息 (作者/提交日期/期刊/分类/引用数) 每次都刷新: 它跟画像
                # 无关, 拿到的就是更全的那份, 没必要像解读那样"有旧的就留着"。
                meta = _meta_columns(cand)
                if row is None:
                    new += 1
                    times = 1
                    best = float(getattr(cand, "score", 0.0) or 0.0)
                    self.conn.execute(
                        """INSERT INTO recommended
                           (arxiv_id, title, first_at, last_at, times, best_score,
                            report, profile_fp, summary, connections, ideas,
                            analyzed, authors, published, journal, categories,
                            citations)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (aid, getattr(cand, "title", "") or "", now, now, times,
                         best, report_path,
                         profile_fp if analyzed else "",
                         getattr(cand, "summary", "") or "",
                         json.dumps(getattr(cand, "connections", []) or [],
                                    ensure_ascii=False),
                         getattr(cand, "ideas", "") or "", analyzed) + meta)
                else:
                    upd += 1
                    times = int(row["times"] or 0) + 1
                    best = max(float(row["best_score"] or 0.0),
                               float(getattr(cand, "score", 0.0) or 0.0))
                    # 只有这次真的解读了才覆盖解读内容; 没解读就保留旧的 ——
                    # 否则一次 --no-ai 的运行会把之前存好的解读清成空串
                    if analyzed:
                        self.conn.execute(
                            """UPDATE recommended SET title=?, last_at=?, times=?,
                               best_score=?, report=?, profile_fp=?, summary=?,
                               connections=?, ideas=?, analyzed=1,
                               authors=?, published=?, journal=?, categories=?,
                               citations=?
                               WHERE arxiv_id=?""",
                            (getattr(cand, "title", "") or "", now, times, best,
                             report_path, profile_fp,
                             getattr(cand, "summary", "") or "",
                             json.dumps(getattr(cand, "connections", []) or [],
                                        ensure_ascii=False),
                             getattr(cand, "ideas", "") or "") + meta + (aid,))
                    else:
                        self.conn.execute(
                            """UPDATE recommended SET last_at=?, times=?,
                               best_score=?, report=?, authors=?, published=?,
                               journal=?, categories=?, citations=?
                               WHERE arxiv_id=?""",
                            (now, times, best, report_path) + meta + (aid,))
            except Exception as exc:
                log("写推荐记录失败 (%s): %s" % (aid, exc), "warn")
        try:
            self.conn.commit()
        except Exception:
            pass
        return new, upd

    def update_meta(self, items: Dict[str, Dict[str, Any]]) -> int:
        """把补出来的元信息写回库, 返回改了几行。

        **只动那五列** (作者/提交日期/期刊/分类/引用数)。标题、分数、解读、报告
        路径这些一个字都不碰 —— 补元信息这件事的授权范围就到这儿。

        和 ``record`` 里那句"元信息每次都刷新"是同一个道理: 这几列跟研究画像无关,
        拿到更全的就该覆盖, 不像解读那样得"有旧的就留着"。
        """
        n = 0
        for aid, m in (items or {}).items():
            aid = str(aid or "").strip()
            if not aid or not isinstance(m, dict):
                continue
            try:
                cur = self.conn.execute(
                    "UPDATE recommended SET authors=?, published=?, journal=?, "
                    "categories=?, citations=? WHERE arxiv_id=?",
                    (json.dumps([str(a) for a in (m.get("authors") or [])],
                                ensure_ascii=False),
                     _iso_text(m.get("published")),
                     str(m.get("journal") or ""),
                     json.dumps([str(c) for c in (m.get("categories") or [])],
                                ensure_ascii=False),
                     _int_or_none(m.get("citations")),
                     aid))
                n += max(0, int(cur.rowcount or 0))
            except Exception as exc:
                log("写回推荐记录元信息失败 (%s): %s" % (aid, exc), "warn")
        try:
            self.conn.commit()
        except Exception:
            pass
        return n

    def forget(self, ids: Optional[List[str]] = None) -> int:
        cur = (self.conn.execute("DELETE FROM recommended") if not ids else
               self.conn.execute(
                   "DELETE FROM recommended WHERE arxiv_id IN (%s)"
                   % ",".join("?" for _ in ids), list(ids)))
        self.conn.commit()
        return cur.rowcount

    def clear(self) -> int:
        """清空全部推荐记录, 返回清掉了多少条。

        删完补一次 VACUUM: 不清的话 sqlite 会把空页留在文件里, 界面上写着"已
        重置", 硬盘上那个文件却还是原来那么大 —— 用户按体积判断的时候会对不上。

        只删记录, 不删报告、不动文献索引: 报告是用户自己的产物, 索引删了还得
        重扫。界面上那句确认写的就是这个范围。
        """
        n = self.forget()
        try:
            self.conn.execute("VACUUM")
            self.conn.commit()
        except Exception as exc:               # VACUUM 失败不影响"记录已清空"
            log("整理推荐记录库失败: %s" % exc, "warn")
        return n

    # -- 复用 -------------------------------------------------------------
    @staticmethod
    def reuse_analysis(cand: Any, row: Optional[Dict[str, Any]],
                       profile_fp: str) -> bool:
        """把记录里存的解读套回候选对象。返回是否真的复用了。

        **画像指纹不一致时一律不复用** —— 见模块开头的说明。
        """
        if not row:
            return False
        if not row.get("analyzed") or not (row.get("summary") or "").strip():
            return False
        if (row.get("profile_fp") or "") != profile_fp:
            return False
        try:
            conns = json.loads(row.get("connections") or "[]")
        except Exception:
            conns = []
        cand.summary = row.get("summary") or ""
        cand.connections = conns if isinstance(conns, list) else []
        cand.ideas = row.get("ideas") or ""
        cand.analyzed = True
        cand.reused = True
        return True


def _report_paths(rows: List[Dict[str, Any]],
                  search_dirs: Optional[List[str]] = None) -> List[str]:
    """这些记录该去读哪几份报告 —— 记下的绝对路径, 外加按文件名再找一遍。

    第二遍不是多余的: 记录里存的是**绝对路径**, 而用户把整个程序目录拷到别处
    (或者改了「报告目录」) 之后, 那个路径就全失效了 —— 报告文件往往还在, 只是
    换了个地方。只认绝对路径的话, 这些行会白白显示成一排破折号。
    """
    out: List[str] = []
    for r in rows:
        p = str(r.get("report") or "").strip()
        if not p:
            continue
        if p not in out:
            out.append(p)
        if os.path.isfile(p):
            continue
        name = os.path.basename(p)
        for d in (search_dirs or []):
            alt = os.path.join(d, name)
            if os.path.isfile(alt) and alt not in out:
                out.append(alt)
    return out


def _report_enrichment(rows: List[Dict[str, Any]],
                       search_dirs: Optional[List[str]] = None
                       ) -> Dict[str, Dict[str, Any]]:
    """从记录指向的报告里, 把老记录缺的那几项补出来 —— ``{arxiv_id: item}``。

    v1 的库没存作者/提交日期/期刊 (那时这张表只用来"别再重复解读", 列表是后来
    才要铺的)。这些行的 ``report`` 列指向当时的报告, 报告里这几项都写着 —— 读
    一遍补上, 用户现有的记录就不会一排破折号。

    只在**内存里**补, 不写回库: 点一下"推荐记录"就去改用户的数据文件, 不合适;
    而且报告是可以被删的, 补出来的东西本来就不该当成记录的一部分存起来。
    (真正"该存进库"的元信息走的是另一条路 —— 见 repair_records。)
    """
    out: Dict[str, Dict[str, Any]] = {}
    paths = _report_paths(rows, search_dirs)
    if not paths:
        return out
    from .past_runs import parse_report          # 循环导入, 用到才拉
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                parsed = parse_report(fh.read())
        except Exception as exc:
            log("读记录指向的报告失败 %s: %s" % (p, exc), "warn")
            continue
        for item in parsed.get("items") or []:
            aid = extract_arxiv_id(str(item.get("url") or ""))
            if aid:
                # 同一篇在多份报告里出现时, 留第一份就够了 (列表是新的在前)
                out.setdefault(aid, item)
    return out


def _int_or_none(text: Any) -> Optional[int]:
    """尽量弄成一个 int, 弄不出来给 None (不是 0 —— "没有这项"和"0 次引用"是两回事)。"""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text
    if isinstance(text, float):
        return int(text) if text == int(text) else None
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def rows_as_candidates(rows: List[Dict[str, Any]],
                       search_dirs: Optional[List[str]] = None) -> List[Any]:
    """记录行 -> ``Candidate`` 列表, 好让「文献推荐」页那张列表直接铺出来。

    复用 Candidate 而不是另写一套渲染: 列表的选择、双击打开、斑马纹、分数着色、
    下方详解面板, 全都是照 Candidate 写的。转成真对象, 这些一行代码都不用动。

    ``search_dirs`` 是"报告可能还在这几个目录里", 见 _report_paths。
    """
    from .models import Candidate                 # 循环导入, 用到才拉

    enrich = _report_enrichment(rows, search_dirs)
    out: List[Any] = []
    for r in rows:
        aid = str(r.get("arxiv_id") or "").strip()
        c = Candidate(arxiv_id=aid, title=str(r.get("title") or ""))
        c.score = float(r.get("best_score") or 0.0)
        c.summary = str(r.get("summary") or "")
        c.ideas = str(r.get("ideas") or "")
        c.connections = _conns(r.get("connections"))
        c.analyzed = bool(r.get("analyzed"))
        c.authors = _json_list(r.get("authors"))
        c.published = _parse_dt(r.get("published"))
        c.journal_ref = str(r.get("journal") or "")
        c.categories = _json_list(r.get("categories"))
        if c.categories:
            c.primary_category = c.categories[0]
        c.citations = _int_or_none(r.get("citations"))
        # 这一条是"读回来的", 不是"这轮跑出来的" —— 详解面板据此跳过三个分数
        c.from_history = True
        c.record_report = str(r.get("report") or "")
        c.seen_before = True
        c.seen_times = int(r.get("times") or 0)
        c.last_recommended = str(r.get("last_at") or "")
        # 老记录缺的几项, 拿当时那份报告补 (补不补得上都不影响别的字段)
        item = enrich.get(aid)
        if item:
            if not c.authors and item.get("authors"):
                c.authors = [str(item["authors"])]
            if c.published is None:
                c.published = _parse_dt(item.get("date"))
            if not c.journal_ref and item.get("journal"):
                c.journal_ref = str(item["journal"])
            if not c.categories and item.get("categories"):
                c.categories = [str(x) for x in item["categories"]]
                c.primary_category = c.categories[0]
            if not c.title and item.get("title"):
                c.title = str(item["title"])
            if c.citations is None:
                # 报告里没有引用数时写的是 "—", _int_or_none 会给出 None ——
                # 正是想要的: 列表那边按"没有这项"显示, 而不是显示成 0
                c.citations = _int_or_none(item.get("citations"))
        out.append(c)
    return out


def missing_meta_ids(rows: List[Dict[str, Any]]) -> List[str]:
    """哪些记录缺"作者/提交日期" —— 这两项 arXiv 一定有, 空着就说明确实缺。

    只看这两项, **不看期刊**: 一篇没发表过的论文本来就没有期刊, 拿它当"缺"会
    让每次点「推荐记录」都去问一遍 arXiv, 还永远补不上。
    """
    out: List[str] = []
    for r in rows:
        aid = str(r.get("arxiv_id") or "").strip()
        if not aid:
            continue
        if not str(r.get("authors") or "").strip() \
                or not str(r.get("published") or "").strip():
            out.append(aid)
    return out


def fill_missing_meta(cfg: Dict[str, Any], arxiv_ids: List[str],
                      cache: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """按 arXiv ID 把元信息取回来 —— ``{arxiv_id: {...}}``。

    为什么要联网: 记录里那几列是**当时**落库的, 而 v1 的库根本没有这几列 (那时
    这张表只用来"别再重复解读")。报告也靠不住 —— 报告是会被删的 (见 build_exe
    里那句"重建会连 output/ 一起删"), 而记录里存的又是报告的绝对路径。补不回来
    的时候, 用户看到的就是一排"——", 完全猜不到数据本来就在 arXiv 上。

    用的是抓候选时那套 ``search_by_ids`` (一次请求带 100 个 ID), 所以 20 条记录
    只有一个请求, 而且走同一层磁盘缓存 —— 第二次点「推荐记录」根本不发请求。
    """
    ids = [str(i).strip() for i in (arxiv_ids or []) if str(i or "").strip()]
    if not ids:
        return {}
    from .arxiv_search import search_by_ids        # 循环导入, 用到才拉
    from .utils import build_session, set_global_interval

    ncfg = cfg.get("network") or {}
    acfg = cfg.get("arxiv") or {}
    delay = float(acfg.get("request_delay", 3.0))
    # 全局节流闸门也要设: 补元信息不该绕过"每条请求之间歇 3 秒"这条规矩, 否则
    # 用户点一下「推荐记录」就成了绕过限流的后门。
    set_global_interval(delay)
    session = build_session(ncfg.get("proxy"),
                            timeout=int(ncfg.get("timeout", 40)))
    # 这是补全, 不是主流程: 重试次数比抓候选时少 (用户正等着看列表)。
    got = search_by_ids(session, ids, cache=cache, delay=delay, retries=2)
    out: Dict[str, Dict[str, Any]] = {}
    for cand in got:
        aid = str(getattr(cand, "arxiv_id", "") or "").strip()
        if not aid:
            continue
        out[aid] = {
            "authors": list(getattr(cand, "authors", None) or []),
            "published": getattr(cand, "published", None),
            "journal": str(getattr(cand, "journal_ref", "") or ""),
            "categories": list(getattr(cand, "categories", None) or []),
            # arXiv 不提供引用数, 留 None: "没有这项"和"0 次引用"是两回事
            "citations": None,
        }
    return out


def repair_records(cfg: Dict[str, Any], rows: List[Dict[str, Any]],
                   cache: Optional[Any] = None) -> Dict[str, Dict[str, Any]]:
    """把记录里缺的作者/提交日期从 arXiv 补回来, 并写回库。

    返回补到的 ``{arxiv_id: 元信息}`` (没补到的不在里面)。**任何失败都不抛**:
    补不齐只是列表里那几列继续写着"——", 不该因此让「推荐记录」打不开。

    这一步会**改用户的数据文件** (只改那五列), 所以日志里把做了什么写清楚 ——
    "点一下看一眼"顺手改了盘上的东西, 用户有权知道。
    """
    ids = missing_meta_ids(rows)
    if not ids:
        return {}
    log("推荐记录: %d 篇缺作者/提交日期, 按 arXiv ID 补一遍" % len(ids))
    try:
        got = fill_missing_meta(cfg, ids, cache=cache)
    except Exception as exc:
        log("按 ID 补全推荐记录失败: %s" % exc, "warn")
        return {}
    if not got:
        log("没补到任何元信息 —— 多半是网络不通 (列表里那几列还是破折号)",
            "warn")
        return {}
    path = history_path(cfg)
    if os.path.exists(path):
        try:
            with RecommendHistory(path) as h:
                n = h.update_meta(got)
            log("推荐记录: 补全了 %d 篇, 已写回 %s" % (n, path), "ok")
        except Exception as exc:
            log("补出来的元信息没能写回记录库 (%s); 这次列表上照常显示。" % exc,
                "warn")
    return got


def _conns(text: Any) -> List[Dict[str, str]]:
    """``connections`` 列读回来。形状不对就丢掉 —— 详情面板要按 dict 取键。"""
    try:
        val = json.loads(text or "[]")
    except Exception:
        return []
    if not isinstance(val, list):
        return []
    return [x for x in val if isinstance(x, dict)]


def load_rows(cfg: Dict[str, Any], limit: int = 0) -> List[Dict[str, Any]]:
    """按配置读出**原始记录行** (还没转成 Candidate)。

    补元信息那条后台线程要的就是原始行 (``repair_records`` 按列判断缺什么、按列
    写回去), 而界面铺列表要的是 Candidate。两条路都从这儿拿数据, 省得"读库"这件
    事有两个写法。

    库打不开、被关掉、或者一条记录都没有, 都返回空列表。**文件不存在时直接返回,
    不打开**: ``RecommendHistory()`` 会顺手把库建出来 (建表、补列), 于是"点一下
    「推荐记录」看一眼"就会在用户盘上凭空多出一个文件 —— 看记录这件事从头到尾都
    该是只读的。
    """
    if not enabled(cfg):
        return []
    path = history_path(cfg)
    if not os.path.exists(path):
        return []
    try:
        with RecommendHistory(path) as h:
            return h.rows(limit=limit)
    except Exception as exc:
        log("读推荐记录失败: %s" % exc, "warn")
        return []


def load_records(cfg: Dict[str, Any], limit: int = 0) -> List[Any]:
    """按配置读出全部推荐记录, 直接给界面用的 Candidate 列表。"""
    rows = load_rows(cfg, limit)
    if not rows:
        return []
    return rows_as_candidates(rows, [report_dir(cfg)])


def open_history(cfg: Dict[str, Any],
                 use: bool = True) -> Optional[RecommendHistory]:
    """按配置打开推荐记录库。关掉或打不开时返回 None (不影响这一轮运行)。"""
    if not use or not enabled(cfg):
        return None
    path = history_path(cfg)
    try:
        return RecommendHistory(path)
    except Exception as exc:
        log("打不开推荐记录库 %s: %s (这一轮不做记录)" % (path, exc), "warn")
        return None


def describe_history(cfg: Dict[str, Any]) -> str:
    """给界面/自检用的一句话。"""
    if not enabled(cfg):
        return "推荐记录: 已关闭"
    path = history_path(cfg)
    if not os.path.exists(path):
        return "推荐记录: %s (还没建)" % path
    try:
        with RecommendHistory(path) as h:
            c = h.counts()
            return ("推荐记录: %d 篇 (其中 %d 篇存有解读) · 最近 %s"
                    % (c["total"], c["analyzed"], h.last_run() or "—"))
    except Exception as exc:
        return "推荐记录: 打不开 (%s)" % exc
