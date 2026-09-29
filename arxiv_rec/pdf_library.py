"""从 PDF 文件夹读取文献库, 带增量索引和三档读取深度。

这是唯一的文献来源 —— 不读 Zotero 数据库。元数据 (标题/作者/DOI/年份) 全靠
从 PDF 内容里推断, 推断不出来时才退到文件名。

--------------------------------------------------------------------------
增量读取
--------------------------------------------------------------------------
用户的核心诉求是"不要重复读取已读过的文件", 因为重复读取会一路传导到
AI 调用上, 白烧 token。所以索引做在**文件内容**这一层:

  1. 先按路径查索引, 若 ``(size, mtime_ns)`` 都没变 -> 直接复用, 连文件都不打开;
  2. 变了 -> 算 sha1。若 sha1 命中索引里已有记录 -> 说明只是**移动/改名**,
     把原来的提取结果搬过来即可, 同样不需要重新解析 PDF;
  3. 只有内容确实是新的 -> 才真正解析 PDF。

mtime 用纳秒精度 (``st_mtime_ns``): "大小相同 + mtime 纳秒级相同 + 内容不同"
在实践中不会发生, 所以不必每次扫描都读整个文件去算哈希。

提取结果落在 SQLite (``library.index_db``), 它是可重建的派生数据, 但故意
不放在 ``cache/`` 下 —— ``--refresh`` 会清空 cache, 不该顺手把用户几十分钟
的 PDF 解析成果也清掉。

--------------------------------------------------------------------------
三档读取深度
--------------------------------------------------------------------------
``library.read_depth`` 决定每篇文献抽到哪一层:

  * ``metadata``  标题 + 摘要                          最快, 只读前几页
  * ``sections``  标题 + 摘要 + 引言/结论               (默认)
  * ``fulltext``  标题 + 摘要 + 引言/结论 + 全文节选

档位是**单调包含**的: 深档位包含浅档位的一切。所以索引里记了每篇是按哪档读
的 (``depth`` 列), 换档位时只需要重读"读得不够深"的那些文件 —— 往浅里调
一个文件都不用重读, 往深里调只补读缺的部分。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import LibraryPaper
from .utils import (clean_text, log, norm_doi, parse_year,
                    strip_latex, truncate)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
# 提取逻辑改动后要 +1: 索引里存的是旧代码的解析结果, 不升版本就不会重解析。
# 7 -> 8: 引言/结论的标题正则改成允许"标题与正文并成一行", 结论改为逐级放宽
#         窗口查找。不升版本的话老索引里的空 conclusion 会被当成"这篇没有结论"
#         一直复用下去。
# 8 -> 9: arXiv ID / DOI 不再从全文里捞 (改成只认首页、且必须早于参考文献区)。
#         必须升版本重解析: 老索引里存着一批**从引文里抄来的假 ID**, 而 arXiv ID
#         是去重的第一优先级, 那些假 ID 会让真正的新论文被当成"已在库中"丢掉。
SCHEMA_VERSION = 9

# 解析全文时最多读多少页 (摘要和引言都在前几页, 后面是参考文献)
MAX_PAGES_FOR_TEXT = 12

# 找结论时从文末往前读的页数窗口, 由小到大逐个试。
#
# 为什么要"渐进"而不是一次读很多: 参考文献动辄占掉三四页, 结论常常不在最后
# 6 页里 (实测漏掉的那些里, 一半是"文末 6 页全是参考文献")。但一上来就读
# 40 页又太贵。所以先读 6 页, 没找到再放宽 —— 多数论文第一轮就命中, 平均
# 代价还是"读 6 页", 只有少数长参考文献的论文才多读。
TAIL_PAGE_WINDOWS = (6, 16, 40)

# 引言/结论各自最多留多少字符。这两段是给 AI 当补充上下文的, 不是全文,
# 所以给个上限, 免得一篇 5000 字的引言把整个提示词预算吃光。
MAX_SECTION_CHARS = 1200

# --------------------------------------------------------------------------
# 读取深度
# --------------------------------------------------------------------------
# 三档, 从浅到深。**顺序不能乱**: 索引里记着上次是按哪一档读的, 只有
# "已读的档 >= 这次要的档"才允许复用。少了这个判断, 用户从"标题+摘要"
# 切到"全文"时会一个文件都不重读, 新加的全文永远是空的 —— 而且没有任何报错,
# 只是结果看起来"怎么还是老样子"。
READ_DEPTHS = ("metadata", "sections", "fulltext")
DEPTH_ORDER = {"metadata": 0, "sections": 1, "fulltext": 2}
DEPTH_LABELS = {
    "metadata": "标题 + 摘要",
    "sections": "标题 + 摘要 + 引言/结论",
    "fulltext": "全文",
}


def normalize_depth(depth: str) -> str:
    """把配置里的深度名收敛到 ``READ_DEPTHS`` 里的一个。

    认不出来时取 ``sections`` 而不是 ``metadata``: 用户写了个拼错的档位,
    多做一点比默默少做一点好 —— 少做的话结果看起来"正常", 没人会发现。
    """
    d = str(depth or "").strip().lower()
    return d if d in DEPTH_ORDER else "sections"


def _depth_covers(stored: str, wanted: str) -> bool:
    """索引里已读的档够不够深。没记档位的老记录 (空串) 一律当作不够。"""
    return DEPTH_ORDER.get(str(stored or ""), -1) >= DEPTH_ORDER[wanted]

# 判为"不是一篇独立论文"的文件名特征 —— 补充材料、审稿回复之类。
# 它们也有 PDF 正文, 但提取出来的"标题"是垃圾, 会污染研究画像。
DEFAULT_SKIP_PATTERNS = [
    r"supplement", r"supporting[\s_-]*information", r"\bsi\b", r"\bsm\b",
    r"response[\s_-]*to[\s_-]*referee", r"referee[\s_-]*report",
    r"cover[\s_-]*letter", r"reviewer[\s_-]*comment",
    r"appendix[\s_-]*only", r"erratum[\s_-]*only",
]

# 标题行的排除特征: arXiv 页边戳、期刊页眉、页码、日期
_RE_ARXIV_STAMP = re.compile(
    r"arXiv:\s*\d{4}\.\d{4,5}(v\d+)?\s*(\[[^\]]*\])?\s*(\d{1,2}\s+\w+\s+\d{4})?",
    re.I)
_RE_JOURNAL_HEADER = re.compile(
    r"^(phys(ical)?\.?\s*rev(iew)?\.?|nature|science|journal of|j\.\s|"
    r"vol(ume)?\.?\s*\d|no\.?\s*\d|pp\.?\s*\d|\d{1,3}\s*\(\d{4}\))", re.I)
_RE_MOSTLY_SYMBOL = re.compile(r"^[^\w一-鿿]{0,3}$")
_RE_PAGE_NUMBER = re.compile(r"^\s*\d{1,4}\s*$")
_RE_DATE_LIKE = re.compile(r"^\s*\d{1,2}\s+\w+\s+\d{4}\s*$")
_RE_DOWNLOAD_COVER = re.compile(
    r"JSTOR|jstor\.org|This content downloaded|All use subject to|"
    r"Terms and Conditions of Use|Your use of the \w+ archive|"
    r"is collaborating with JSTOR|not-for-profit service", re.I)
# 控制字符: 标题里出现就说明字体编码是坏的
_RE_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 句末标点后接大写 —— 正文段落的标志, 标题里几乎不出现
_RE_SENTENCE_END = re.compile(r"[.!?]\s+[A-Z]")

# "Abstract" 之后到哪里为止
_RE_ABS_STOP = re.compile(
    r"(?:^|\n)\s*(?:\d+\.?\s*)?(?:I\.?\s+)?"
    r"(?:Introduction|INTRODUCTION|Contents|CONTENTS|"
    r"Keywords|KEYWORDS|PACS|Pacs|"
    r"\d+\.\s+[A-Z])",
)

# DOI / arXiv ID 在正文里的形态
_RE_DOI_IN_TEXT = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.I)

# 首页上"文献区开始"的标志。参考文献里的 DOI / arXiv 号是**别人的**, 一律不能要。
_RE_BIB_HEAD = re.compile(
    r"(?m)^[ \t]*(?:REFERENCES|References|Bibliography|BIBLIOGRAPHY|"
    r"参考\s*文献|引用文献)[ \t]*$")

# arXiv 戳记 —— 必须带字面 "arXiv" 前缀, 这是它和"正文里随便一个四位点五位数字"
# 的唯一可靠区别。
_RE_ARXIV_STAMP_ID = re.compile(
    r"arXiv[:\s]*(\d{4}\.\d{4,5}|[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?",
    re.IGNORECASE)


def _before_bibliography(text: str) -> str:
    """砍掉 ``text`` 里参考文献区之后的部分。

    首页上也常有参考文献 —— 短论文、讲稿、会议摘要的文献表第一页就开始了。
    那些条目里的 DOI 和 arXiv 号指向的是**被引论文**, 拿它们当本文档的标识符,
    会让多篇不同的论文认领同一个 ID。
    """
    m = _RE_BIB_HEAD.search(text or "")
    return text[:m.start()] if m else (text or "")


def _ident_from_head(head: str) -> Tuple[str, str]:
    """从**首页**抽 (arXiv ID, DOI)。抽不到就给空串。

    这里刻意**不用** ``utils.extract_arxiv_id``。那个函数是为 Zotero 元数据 / API
    返回写的, 只要文本里出现形如 ``2307.10602`` 的裸数字就认 —— 可 PDF 正文、
    公式编号、参考文献里这种数字遍地都是。实测 219 篇真实文献:

      * 74 篇首页带 arXiv 戳记 —— 可靠;
      * 71 篇"戳记"其实出现在参考文献区 —— 全是别人的;
      * 结果一个 ``cond-mat/0403055`` 被 6 篇毫不相干的论文同时认领,
        ``2302.11742`` 被 4 篇认领。

    这个错误比"抽不到 ID"严重得多: arXiv ID 是去重的**第一优先级**, 假 ID 会让
    一篇真正的新论文在推荐阶段被判定成"已在库中"直接丢掉 —— 而推荐新论文正是
    这个程序存在的意义。所以宁可空着, 不可认错。同一份实测里"戳记在首页"
    的有 74 篇, 而"只在第 2 页之后出现戳记"的有 **0** 篇, 说明往深里找除了
    捞到引文之外没有任何收益。

    顺带: 同样的道理, DOI 也只认首页 (且必须早于参考文献区)。
    """
    body = _before_bibliography(head)
    aid = ""
    m = _RE_ARXIV_STAMP_ID.search(body)
    if m:
        aid = m.group(1).lower()
    doi = ""
    m = _RE_DOI_IN_TEXT.search(body)
    if m:
        doi = m.group(0)
    return aid, doi

# --------------------------------------------------------------------------
# 小节定位 (引言 / 结论)
# --------------------------------------------------------------------------
# 小节标题的编号前缀: "2. " / "II. " / "B. " / 无
_SEC_NUM = r"(?:\d+\.?[ \t]*|[IVX]{1,4}\.[ \t]*|[A-Z]\.[ \t]*)?"

# 标题后面允许接什么。**行首锚定是刻意的**: 正文里 "as discussed in the
# introduction of Ref. [3]" 这种句子遍地都是, 不锚定行首就会把句子当小节标题,
# 抽出来的"引言"是一句参考文献指引。
#
# 但**行尾不能锚死**: PDF 抽出来的文本块经常把标题和正文第一句并成一行 ——
# 实测 "Discussion and conclusion Overall, we realize a practical scheme..."
# 就是这种情况。所以标题词后面允许三种情况:
#   1. 直接换行                       ("Conclusions")
#   2. 跟一个大写字母/括号/冒号/数字    (正文或 "结论与展望" 这类标题)
#   3. 跟一个连接词再接上面那种        ("Introduction to Quantum Monte Carlo")
# 唯独不接受**直接跟小写词**, 那样 "Conclusions and outlook are discussed in
# Sec. 5" 这种正文句子会被误判成小节标题。
_SEC_TAIL = (r"[ \t]*(?:$|[ \t]+(?:(?i:and|to|of|for|in|on|with|与|和)\b[ \t]+)?"
             r"[A-Z(（\[:：0-9])")

# 少数论文的小节标题是 "Introduction and motivation" 这种复合形式, 中间的小写
# 词不在 _SEC_TAIL 的允许范围里。单列一条只收几个常见词的分支 —— 白名单窄,
# 而且就算误判, 代价也只是多带一小段正文进上下文, 不是抽错文献。
_RE_INTRO_HEAD = re.compile(
    r"(?m)^[ \t]*" + _SEC_NUM +
    r"(?:(?:INTRODUCTIONS?|Introductions?|引言|前言)" + _SEC_TAIL +
    r"|(?:Introduction|引言|前言)[ \t]+(?i:and|与|和)[ \t]+"
    r"(?i:motivation|background|overview|summary|scope|notation|methods?)"
    + _SEC_TAIL + r")")

# 结论的写法比引言杂得多。物理期刊常用 "Summary and outlook",
# PRL 常用 "Conclusions", 也有 "Concluding remarks" / "Discussion and conclusions"。
#
# 刻意**不**收光秃秃的 "Discussion" 和 "Summary": 这两个词在小节标题里出现得
# 很早 (第 3、4 节), 收进来会把中段的讨论当成结论。带尾巴的复合形式才收。
_RE_CONCL_HEAD = re.compile(
    r"(?m)^[ \t]*" + _SEC_NUM +
    r"(?:CONCLUSIONS?|Conclusions?|"
    r"CONCLUDING\s+REMARKS|Concluding\s+[Rr]emarks|"
    r"CONCLUDING\s+DISCUSSION|Concluding\s+[Dd]iscussion|"
    r"FINAL\s+REMARKS|Final\s+[Rr]emarks|"
    r"SUMMARY\s+AND\s+(?:OUTLOOK|CONCLUSIONS?|DISCUSSION|PERSPECTIVES)|"
    r"Summary\s+and\s+(?:outlook|conclusions?|discussion|perspectives)|"
    r"CONCLUSIONS?\s+AND\s+(?:OUTLOOK|PERSPECTIVES?|SUMMARY|DISCUSSION)|"
    r"Conclusions?\s+and\s+(?:outlook|perspectives?|summary|discussion|"
    r"future\s+(?:work|directions|prospects))|"
    r"DISCUSSION\s+AND\s+CONCLUSIONS?|Discussion\s+and\s+conclusions?|"
    r"OUTLOOK|Outlook|"
    r"结论与展望|总结与展望|结论|总结)" + _SEC_TAIL)

# 结论的第二档 (兜底)。很多论文**根本没有** "Conclusion" 小节 —— 物理期刊习惯
# 用光秃秃的 "Discussion" / "Summary" / "Perspectives" 收尾, 综述尤其如此。
# 实测 219 篇真实文献: 第一档抽到 27%, 补上这一档到 43%, 而人工看抽出来的内容
# 基本都是对的 (文末那一节的正文)。
#
# 这一档**只在第一档完全没命中时才用**, 且取最后一个匹配 —— 中段那个
# "III. DISCUSSION" 因此不会被选中。所以光秃秃的 "Discussion" 收在这里是安全的,
# 放进第一档才危险 (第一档只要有匹配就赢, 中段的讨论会被当成结论)。
_RE_CONCL_HEAD_WIDE = re.compile(
    r"(?m)^[ \t]*" + _SEC_NUM +
    r"(?:SUMMARY|Summary|DISCUSSION|Discussion|OUTLOOK|Outlook|"
    r"CONCLUDING|Concluding|REMARKS|Remarks|PERSPECTIVES|Perspectives|"
    r"总结|讨论|展望)" + _SEC_TAIL)

# 引言/结论到哪里为止。结论后面通常是致谢和参考文献, 这两块不是正文。
_RE_SEC_STOP = re.compile(
    r"(?m)^[ \t]*" + _SEC_NUM +
    r"(?:ACKNOWLEDG\w*|Acknowledg\w*|REFERENCES|References|"
    r"BIBLIOGRAPHY|Bibliography|APPENDIX\w*|Appendix\w*|"
    r"SUPPLEMENTAL\s+MATERIAL|Supplemental\s+[Mm]aterial|"
    r"致谢|参考文献|附录)")

# 参考文献条目的特征: 年份后面跟着期刊缩写, 或者以 "[12]" 编号开头。
# 结论抽到一半撞进参考文献时, 用这个兜住 (有些 PDF 的 "References" 标题
# 被抽成了页眉, 行锚定匹配不到)。
_RE_BIB_LINE = re.compile(
    r"(?m)^[ \t]*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}\.[ \t])"
    r"[^\n]{0,80}?\b(?:19|20)\d{2}\b")


def _clean_section(text: str) -> str:
    """小节正文收尾: 接回断词, 去掉多余空白, 压到上限。"""
    body = _fix_hyphenation(clean_text(text or ""))
    if len(body) < 80:
        return ""
    return truncate(body, MAX_SECTION_CHARS, ellipsis=" …")


def _find_intro(text: str) -> str:
    """抽引言正文。

    从第一个 "Introduction" 小节标题往后取, 直到撞上致谢/参考文献, 或者
    取满 ``MAX_SECTION_CHARS``。

    这里**不**去精确判断"下一节标题从哪开始": 猜小节标题要引入一套很松的
    正则, 猜错了会把正文切碎; 而引言的用途只是给 AI 当上下文, 多带几十个
    字进下一节完全无害。宁可多带, 不要切错。
    """
    if not text:
        return ""
    m = _RE_INTRO_HEAD.search(text)
    if not m:
        return ""
    body = text[m.end():]
    stop = _RE_SEC_STOP.search(body)
    if stop and stop.start() > 120:
        body = body[:stop.start()]
    return _clean_section(body)


def _concl_body(text: str, pattern: Any) -> str:
    """在 ``text`` 里按 ``pattern`` 找最后一个标题, 取它后面那节正文。

    取**最后一个**匹配: 讨论和结论分开写时 ("4. Discussion" / "5. Conclusions")
    最后那个才是总结。而且正文里提到 "our conclusions" 的行不会以标题形态出现,
    行锚定已经把这类噪声挡掉了。
    """
    if not text:
        return ""
    matches = list(pattern.finditer(text))
    if not matches:
        return ""
    body = text[matches[-1].end():]
    stop = _RE_SEC_STOP.search(body)
    if stop:
        body = body[:stop.start()]
    else:
        # 没有致谢/参考文献标题兜底时, 再用参考文献条目形态切一刀
        bib = _RE_BIB_LINE.search(body)
        if bib and bib.start() > 120:
            body = body[:bib.start()]
    return _clean_section(body)


def _find_conclusion(text: str) -> str:
    """抽结论正文 (第一档: 明确写着 Conclusion 的小节)。"""
    return _concl_body(text, _RE_CONCL_HEAD)


def _find_conclusion_wide(text: str) -> str:
    """抽结论正文 (第二档兜底: 用 Discussion / Summary / Perspectives 收尾的论文)。

    只在第一档完全没命中时才该调用, 否则中段的 "Discussion" 会顶掉真正的
    "Conclusions"。
    """
    return _concl_body(text, _RE_CONCL_HEAD_WIDE)

# 作者行特征: 含逗号分隔的姓名、上标符号、"and"
_RE_AUTHOR_HINT = re.compile(
    r"[A-Z][a-z]+(?:\s+[A-Z]\.?)*\s+[A-Z][a-z]+|"
    r"\band\b|[\*†‡§¶]|\b[A-Z]\.[\s\-]*[A-Z]", re.UNICODE)


# --------------------------------------------------------------------------
# PDF 后端
# --------------------------------------------------------------------------
class PdfBackendUnavailable(RuntimeError):
    """一个 PDF 解析库都没装。"""


def available_backend() -> str:
    """返回当前可用的 PDF 后端名。"""
    for mod, name in (("fitz", "pymupdf"), ("pdfminer.high_level", "pdfminer"),
                      ("pypdf", "pypdf"), ("PyPDF2", "pypdf2")):
        try:
            __import__(mod)
            return name
        except Exception:
            continue
    return ""


def _open_document(path: str) -> Tuple[Any, str]:
    """打开 PDF, 返回 ``(文档对象, 后端名)``。"""
    try:
        import fitz  # PyMuPDF
        return fitz.open(path), "pymupdf"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError("PyMuPDF 打不开: %s" % exc)

    try:
        from pdfminer.high_level import extract_text  # noqa: F401
        return _PdfMinerDoc(path), "pdfminer"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError("pdfminer 打不开: %s" % exc)

    try:
        import pypdf
        return _PypdfDoc(pypdf.PdfReader(path)), "pypdf"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError("pypdf 打不开: %s" % exc)

    try:
        import PyPDF2
        return _PypdfDoc(PyPDF2.PdfReader(path)), "pypdf2"
    except ImportError:
        pass
    except Exception as exc:
        raise RuntimeError("PyPDF2 打不开: %s" % exc)

    raise PdfBackendUnavailable(
        "没有可用的 PDF 解析库。请安装 PyMuPDF: pip install pymupdf"
    )


class _PdfMinerDoc:
    """pdfminer 的适配层, 只实现本模块用到的那几个方法。"""

    def __init__(self, path: str):
        self.path = path
        self.metadata: Dict[str, str] = {}
        self.page_count = 0
        try:
            from pdfminer.pdfparser import PDFParser
            from pdfminer.pdfdocument import PDFDocument
            from pdfminer.pdfpage import PDFPage
            with open(path, "rb") as fh:
                doc = PDFDocument(PDFParser(fh))
                info = (doc.info or [{}])[0] if doc.info else {}
                for k, v in (info or {}).items():
                    if isinstance(v, bytes):
                        try:
                            v = v.decode("utf-8", "replace")
                        except Exception:
                            v = str(v)
                    self.metadata[str(k)] = str(v)
                # 页数必须自己数出来 —— _text_tail 要靠它定位文末几页。
                # create_pages 只走页对象, 不解码内容流, 所以这一步很便宜。
                self.page_count = sum(1 for _ in PDFPage.create_pages(doc))
        except Exception:
            pass

    def _text(self, max_pages: int) -> str:
        from pdfminer.high_level import extract_text
        try:
            return extract_text(self.path, maxpages=max_pages) or ""
        except Exception:
            return ""

    def _text_tail(self, max_pages: int) -> str:
        """只读最后 ``max_pages`` 页 (结论在文末)。"""
        if not self.page_count:
            return ""
        from pdfminer.high_level import extract_text
        start = max(0, self.page_count - max_pages)
        try:
            return extract_text(self.path,
                                page_numbers=list(range(start, self.page_count))) or ""
        except Exception:
            return ""

    def __enter__(self) -> "_PdfMinerDoc":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _PypdfDoc:
    """pypdf / PyPDF2 的适配层。"""

    def __init__(self, reader: Any):
        self.reader = reader
        self.metadata = {}
        self.page_count = len(getattr(reader, "pages", []) or [])
        try:
            meta = reader.metadata or {}
            for k, v in dict(meta).items():
                self.metadata[str(k).lstrip("/")] = str(v)
        except Exception:
            pass

    def _text(self, max_pages: int) -> str:
        parts = []
        try:
            for page in list(self.reader.pages)[:max_pages]:
                parts.append(page.extract_text() or "")
        except Exception:
            pass
        return "\n".join(parts)

    def _text_tail(self, max_pages: int) -> str:
        """只读最后 ``max_pages`` 页 (结论在文末)。"""
        try:
            pages = list(self.reader.pages)
        except Exception:
            return ""
        start = max(0, len(pages) - max_pages)
        parts = []
        try:
            for page in pages[start:]:
                parts.append(page.extract_text() or "")
        except Exception:
            pass
        return "\n".join(parts)

    def __enter__(self) -> "_PypdfDoc":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _read_tail(doc: Any, backend: str, page_count: int, window: int = 6,
               page_cache: Optional[Dict[int, str]] = None) -> str:
    """读文末 ``window`` 页的文本, 供找结论用。

    PyMuPDF 走页级接口, ``page_cache`` 按页缓存 —— 逐级放宽窗口时只抽新增的那
    几页, 不会把已经抽过的重抽一遍。其余后端只能走适配层上的 ``_text_tail``,
    整段重抽 (pdfminer/pypdf 本来也没有便宜的按页接口)。

    拿不到就返回空串: 结论抽不到只是少一段上下文, 不该让整篇解析失败。
    """
    if backend == "pymupdf":
        try:
            n = int(page_count or getattr(doc, "page_count", 0) or 0)
        except Exception:
            n = 0
        if n <= 0:
            return ""
        parts = []
        for i in range(max(0, n - int(window)), n):
            if page_cache is not None and i in page_cache:
                parts.append(page_cache[i])
                continue
            try:
                txt = "\n".join(b["text"] for b in _page_blocks(doc.load_page(i)))
            except Exception:
                txt = ""
            if page_cache is not None:
                page_cache[i] = txt
            parts.append(txt)
        return "\n".join(parts)
    getter = getattr(doc, "_text_tail", None)
    if getter is None:
        return ""
    try:
        return getter(int(window)) or ""
    except Exception:
        return ""


def _find_conclusion_in_doc(doc: Any, backend: str, page_count: int) -> str:
    """在文末**逐级放宽**的窗口里找结论, 两档标题依次兜底。

    逐级放宽的原因: 参考文献动辄占掉三四页, 结论常常不在最后 6 页里 (实测漏掉
    的那些里, 一半是"文末 6 页全是参考文献")。但一上来就读 40 页又太贵。所以先
    读 6 页, 没找到再放宽到 16、40 —— 多数论文第一轮就命中, 平均代价仍然是
    "读 6 页", 只有少数长参考文献的论文才多读。实测 219 篇里 41 篇在第 6 页窗口
    命中, 19 篇靠放宽到 16/40 页才命中。

    两档标题: 明确写 "Conclusions" 的优先; 全都没写 (综述和不少 PRB 就是这样,
    用 "Discussion" / "Summary" 收尾) 才退到第二档。第一档一旦命中就立刻返回,
    所以中段那个 "III. DISCUSSION" 不会顶掉后面的 "V. Conclusions"。

    只有真要抽正文小节时才该调用 (``read_depth == "metadata"`` 时别调)。
    """
    cache = {}  # type: Dict[int, str]
    try:
        total = int(page_count or 0)
    except Exception:
        total = 0
    wide = ""
    for window in TAIL_PAGE_WINDOWS:
        tail = _read_tail(doc, backend, total, window, cache)
        if tail:
            got = _find_conclusion(tail)
            if got:
                return got
            # 第一档没命中就记下第二档的结果, 但**继续放宽**: 后面某一级可能
            # 就冒出真正的 "Conclusions" 了, 那比现在的 "Discussion" 更该用。
            w = _find_conclusion_wide(tail)
            if w:
                wide = w
        # 窗口已经盖住整篇了 (论文比窗口还短), 再放宽读到的还是同一段文本
        if total and window >= total:
            break
    return wide


# --------------------------------------------------------------------------
# 文本抽取
# --------------------------------------------------------------------------
def _page_blocks(page: Any) -> List[Dict[str, Any]]:
    """取一页的文本块, 按阅读顺序排好。

    两栏排版的页面直接按 y 排序会把左右栏交错混在一起, 所以先按 y 分行
    (20pt 容差), 同一行内再按 x 排。
    """
    try:
        raw = page.get_text("blocks") or []
    except Exception:
        return []
    blocks = []
    for b in raw:
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not isinstance(text, str):
            continue
        text = clean_text(text)
        if text:
            blocks.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text})
    blocks.sort(key=lambda b: (round(b["y0"] / 20.0), b["x0"]))
    return blocks


def _page_spans(page: Any) -> List[Dict[str, Any]]:
    """取一页所有 span 及其字号, 用于按字号猜标题。"""
    try:
        data = page.get_text("dict") or {}
    except Exception:
        return []
    spans: List[Dict[str, Any]] = []
    for block in data.get("blocks") or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                text = clean_text(span.get("text") or "")
                if not text:
                    continue
                spans.append({
                    "text": text,
                    "size": float(span.get("size") or 0.0),
                    "font": str(span.get("font") or ""),
                    "y0": float((span.get("bbox") or [0, 0, 0, 0])[1]),
                    "x0": float((span.get("bbox") or [0, 0, 0, 0])[0]),
                })
    return spans


def _join_lines(lines: Sequence[str]) -> str:
    """把标题的若干行拼起来。

    标题换行时, 若上一行以连字符结尾要接上 (LaTeX 生成的 PDF 常有
    ``sign-problem-`` / ``resilient`` 这种断词), 否则补空格。
    """
    out = ""
    for raw in lines:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
        elif out.endswith("-") and not out.endswith("--"):
            out = out[:-1] + piece
        else:
            out = out + " " + piece
    return clean_text(out)


def _is_noise_line(text: str) -> bool:
    """判断一行是不是页眉/页码/arXiv 戳之类的噪声。"""
    t = text.strip()
    if not t or len(t) < 2:
        return True
    if _RE_PAGE_NUMBER.match(t) or _RE_DATE_LIKE.match(t):
        return True
    if _RE_MOSTLY_SYMBOL.match(t):
        return True
    if _RE_ARXIV_STAMP.search(t) and len(t) < 120:
        return True
    if _RE_JOURNAL_HEADER.match(t) and len(t) < 90:
        return True
    # 全是 URL / 邮箱
    if re.match(r"^[\w.+-]+@[\w.-]+$", t) or t.lower().startswith("http"):
        return True
    # 数据库下载封面页 (JSTOR 之类)。这类页面第一页整页都是版权说明,
    # 真正的论文在后面, 与其把版权声明当摘要, 不如老实承认没抽到。
    if _RE_DOWNLOAD_COVER.search(t):
        return True
    return False


def _guess_title_from_spans(spans: List[Dict[str, Any]]) -> str:
    """按字号找标题: 取页面最大字号的那批连续行。"""
    if not spans:
        return ""
    # 只看页面上半部分的 span (标题一定在上面), 并排除噪声
    usable = [s for s in spans if not _is_noise_line(s["text"])]
    if not usable:
        return ""
    max_y = max(s["y0"] for s in usable)
    upper = [s for s in usable if s["y0"] <= max_y * 0.55] or usable

    max_size = max(s["size"] for s in upper)
    if max_size <= 0:
        return ""
    # 字号差 0.5 以内算同一级 (PDF 里同一行不同 span 字号会轻微抖动)
    title_spans = [s for s in upper if s["size"] >= max_size - 0.5]
    if not title_spans:
        return ""
    title_spans.sort(key=lambda s: (round(s["y0"] / 6.0), s["x0"]))

    # 只保留"从最大字号那一行开始"的连续行, 遇到明显更小的字号就停
    lines: List[str] = []
    last_y = None
    for s in title_spans:
        if last_y is not None and s["y0"] - last_y > 60:
            break        # 中间隔了太远, 不是同一段标题
        lines.append(s["text"])
        last_y = s["y0"]
        if len(_join_lines(lines)) > 400:
            break
    return _join_lines(lines)


def _looks_like_title(text: str) -> bool:
    """判断一段文本像不像论文标题。"""
    t = clean_text(text)
    if len(t) < 12 or len(t) > 400:
        return False
    if _is_noise_line(t):
        return False
    # 标题不会以句号结尾, 也不该是一整句
    if t.endswith(".") and len(t) > 60:
        return False
    # 至少要有一个较长的词, 排除 "Untitled" / "Microsoft Word - xxx.doc"
    if not re.search(r"[A-Za-z一-鿿]{4,}", t):
        return False
    if re.match(r"^(untitled|microsoft word|document\d*|pdf|scan|img|"
                r"-\s*\d|new doc)", t, re.I):
        return False
    # 文件名形态 (含 .pdf/.doc/.tex 或大量下划线)
    if re.search(r"\.(pdf|docx?|tex|dvi)$", t, re.I) or t.count("_") >= 3:
        return False
    return True


# 明显不是论文正文的标题 —— 补充材料说明、审稿意见、回复信、报错输出
_RE_NON_PAPER_TITLE = re.compile(
    r"^(?:file\s*name\s*:|description\s*:|"
    r"supplementary\s+(?:information|material|note)|supporting\s+information|"
    r"comment\s*:|"
    # "Comments on the manuscript" 是审稿意见, 但 "Comment on 'X'" 是 PRL 正文。
    # 所以冒号形式照抓, 而 on/to 形式必须限定后面跟的是**文档**而不是论文标题。
    r"comments?\s+(?:on|to)\s+(?:the\s+)?(?:manuscript|paper|draft|submission|"
    r"article|thesis|referee|reviewers?|review|revision|resubmission|authors)\b|"
    r"(?:response|reply|answer|rebuttal)\s+to\s+(?:the\s+)?(?:referee|reviewers?)s?\b|"
    # 撇号在 s 前面 ("Reviewer's comments"), 所以不能用 reviewers?'?
    r"referees?['’]?s?\s+report|reviewers?['’]?s?\s+comments?|"
    r"to\s+cite\s+this\s+article|traceback\s*\(most\s+recent\s+call|"
    r"report\s+on\s+the\s+manuscript)", re.I)


def classify_title(title: str) -> Tuple[str, str]:
    """判断抽出来的标题像不像一篇论文正文, 返回 ``(结论, 原因)``。

    结论是 ``"paper"`` 或 ``"junk"``。四条规则按"越具体越先判"排:

      1. 乱码 —— 字体缺编码表, 抽出来的是符号汤;
      2. 正文段落 —— 首页没能定位标题, 抓了整段 abstract/introduction;
      3. 非论文文档 —— 补充材料、审稿意见、回复信、报错输出 (靠关键字表);
      4. 页眉栏目名 —— "LETTERS of" 这类。

    顺序不能换: "LETTERS of" 既像页眉也像非论文标题, 但 "Comment: ..." 只有
    关键字表认得出来, 所以 3 必须排在 4 前面。

    这个判定原来写在 ``extract_pdf`` 里的一串 elif 中, 拆出来是为了能直接测 ——
    漏杀会让垃圾文档混进文献库, 误杀会丢掉真论文, 两个方向都得有回归测试。
    """
    if _is_garbage_title(title):
        return "junk", "标题提取为乱码 (PDF 内嵌字体缺少编码表)"
    if _looks_like_prose_block(title):
        return "junk", "首页无法定位标题 (抓到的是正文段落)"
    if _RE_NON_PAPER_TITLE.search(title or ""):
        return "junk", "不是论文正文 (补充材料/审稿意见/回复信等)"
    if _is_column_header(title):
        return "junk", "标题是期刊栏目名或页眉"
    return "paper", ""


def _is_garbage_title(t: str) -> bool:
    """标题是不是提取失败后的乱码。

    内嵌字体没有 ToUnicode 表时, PyMuPDF 抽出来的是 CID 码位映射成的
    ``"!$#"%'&`` 这类字符串 —— 字母被符号切碎、连不成词。三个判据:
      * 出现控制字符 —— 正常标题里不可能有 ``\\x00`` 这种字节;
      * 长到不像标题 —— 乱码时往往把整页都当成一个块抽出来;
      * "处在长度 >= 3 的连续词里的字母占比"过低。
    """
    t = clean_text(t)
    if len(t) < 8:
        # "Cjr" / "相变" 这种短到不可能是标题
        return True
    if _RE_CONTROL_CHAR.search(t):
        return True
    alnum = sum(1 for c in t if c.isalnum())
    if alnum == 0:
        return True
    in_words = sum(len(m.group(0))
                   for m in re.finditer(r"[A-Za-z一-鿿]{3,}", t))
    return in_words < 0.6 * alnum


# 标题块里混进作者/单位/摘要时, 从这些位置切开
_RE_TITLE_CUT = re.compile(
    r"(?:\s+(?:Authors?\s*:|AUTHORS\s*:)|"
    r"\s+(?:ABSTRACT|Abstract|ABSTRACT\s*:)\s|"
    r"\s+\d+\s*(?:Department|Institute|Institut|Laborator|Centre|Center|"
    r"College|School|Universit|Academy|State\s+Key)\b|"
    # "Ke-Jun Xu *, Qinda Guo *" —— 逗号分隔的人名列表
    r"\s+[A-Z][A-Za-z'\-]*(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z'\-]+\s*[*†‡§¶∗]\s*,)")


def _trim_title_block(t: str) -> str:
    """标题块后面粘上了作者/单位/摘要时, 在第一个分隔标志处截断。

    按字号定位标题时会连带把下面的块一起抓进来 (尤其是标题和正文同字号的
    PDF), 结果是"标题 + 作者 + 单位"一长串。截断比整块丢弃好 —— 标题本身
    是能用的。
    """
    t = clean_text(t)
    if not t:
        return ""
    m = _RE_TITLE_CUT.search(t)
    if m and m.start() >= 15:
        t = t[:m.start()].rstrip(" ,;:-")
    return t


def _looks_like_prose_block(t: str) -> bool:
    """整段是不是连续正文 —— 说明标题定位跑偏, 抓到的其实是段落。

    判据是"够长 + 至少两处句末标点后接大写字母"。标题极少出现这种形态,
    正文段落则遍地都是。
    """
    t = clean_text(t)
    if len(t) < 150:
        return False
    return len(_RE_SENTENCE_END.findall(t)) >= 2


def _is_column_header(t: str) -> bool:
    """标题是不是期刊栏目名/页眉, 例如 "PREVIEW"、"LETTERS of"、"ARTICLES"。

    判据是"很短 + 字母几乎全大写" —— 论文标题很少全大写, 栏目名则基本都是。
    中文没有大小写之分, 必须先排除, 否则任何短中文标题都会被误判。
    """
    t = clean_text(t)
    if not t or len(t) > 30:
        return False
    letters = "".join(c for c in t if c.isalpha())
    if len(letters) < 4:
        return False
    if any("一" <= c <= "鿿" for c in letters):
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / float(len(letters)) >= 0.7


# 像人名的词: 首字母大写 (可带缩写点、连字符、撇号)
_RE_NAME_TOKEN = re.compile(r"^(?:[A-Z][\w'’\-]*|[A-Z]\.)$")
# 正文/参考文献行里常见的小写虚词, 作者行里极少
_RE_LOWER_WORD = re.compile(r"\b[a-z]{3,}\b")


def _author_block_score(text: str) -> float:
    """给一段文本打"像不像作者行"的分。

    两个判据缺一不可:
      * 人名词占比高  —— 挡掉 "Electronic structure, spin excitations" 这种
        参考文献条目;
      * 小写虚词占比低 —— 挡掉 "it is deﬁned as follows:" 和
        "To cite this article: Markus Heyl Rep. Prog. Phys" 这种正文/页眉。
    """
    t = clean_text(text)
    if not t or len(t) > 400:
        return 0.0
    # 全大写的短块是 arXiv 分类标签 / 栏目名 ("CONDENSED MATTER"), 不是作者
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 6 and t.upper() == t:
        return 0.0
    words = [w for w in re.split(r"[\s,;]+", t) if w]
    if not words:
        return 0.0
    # 去掉上标编号与符号后缀: "Gu1,2†" -> "Gu"
    stripped = [re.sub(r"[\d\*†‡§¶∗]+$", "", w).strip(".,;") for w in words]
    stripped = [w for w in stripped if w]
    if not stripped:
        return 0.0
    name_like = sum(1 for w in stripped if _RE_NAME_TOKEN.match(w))
    name_ratio = name_like / float(len(stripped))

    lower = len(_RE_LOWER_WORD.findall(t))
    lower_ratio = lower / float(max(1, len(stripped)))

    if lower_ratio > 0.25:
        return 0.0
    return name_ratio


def _guess_authors_from_blocks(blocks: List[Dict[str, Any]], title: str) -> List[str]:
    """在标题之后、摘要之前找作者行。"""
    if not blocks:
        return []
    # 找到标题所在块的位置
    title_key = strip_latex(title).lower()[:40]
    start = 0
    for i, b in enumerate(blocks):
        if title_key and title_key[:25] in strip_latex(b["text"]).lower():
            start = i + 1
            break

    names: List[str] = []
    for b in blocks[start:start + 4]:
        text = b["text"]
        if re.match(r"^\s*(abstract|ABSTRACT|摘要)", text):
            break
        if _is_noise_line(text) or _RE_FRONTMATTER.search(text):
            continue
        if _author_block_score(text) < 0.60:
            continue
        cleaned = _strip_affiliations(text)
        for part in re.split(r",|;|\band\b|&", cleaned):
            part = clean_text(re.sub(r"[\*†‡§¶∗\d]", "", part))
            part = part.strip(" .;,")
            if 3 <= len(part) <= 60 and re.search(r"[A-Za-z一-鿿]{2,}", part):
                if part not in names:
                    names.append(part)
        if names:
            break
    return names[:25]


def _strip_affiliations(text: str) -> str:
    """去掉作者行里的单位/邮箱部分。"""
    t = text
    t = re.sub(r"[\w.+-]+@[\w.-]+", " ", t)
    t = re.sub(r"\b(?:University|Institute|Department|Laboratory|Lab|Center|"
               r"Centre|College|School|Academy|Group|Physics|Physical)\b.*",
               " ", t, flags=re.I)
    return t


def _extract_abstract(pages_text: List[str],
                      first_blocks: List[Dict[str, Any]]) -> str:
    """从正文里抽摘要。

    两条路:
      1. 找显式的 "Abstract" 字样 —— LaTeX 模板排出来的 PDF 大多有;
      2. 没有这个字样时按版式推断 —— 大量物理期刊 (PRX/NJP/RPP) 和部分
         arXiv 投稿根本不印 "Abstract", 摘要就是标题/作者/单位之后的
         第一段正文。只认字面量会漏掉一大半文献。
    """
    for text in pages_text[:2]:
        if not text:
            continue
        m = re.search(r"(?:^|\n)\s*(?:Abstract|ABSTRACT|摘要)\s*[:：.\-—]?\s*",
                      text)
        if not m:
            continue
        body = text[m.end():]
        stop = _RE_ABS_STOP.search(body)
        if stop and stop.start() > 80:
            body = body[:stop.start()]
        body = _fix_hyphenation(clean_text(body))
        # 摘要是连续散文, 太短说明抓错了
        if len(body) >= 80:
            return truncate(body, 2200, ellipsis=" …")

    return _abstract_from_layout(first_blocks)


# 摘要结束的标志: 正文小标题、版权行、关键词
_RE_ABS_STOP_BLOCK = re.compile(
    r"^\s*(?:[IVX]{1,4}\.|[0-9]{1,2}\.?)?\s*(?:INTRODUCTION|Introduction|"
    r"Published by the American Physical Society|PACS|Keywords|KEYWORDS|"
    r"Contents|CONTENTS|©|Copyright|DOI:|https?://)", re.I)

# 作者/单位/脚注这类"正文之前"的内容特征。
#
# 只收**强**信号: 机构名不能单独作为判据 —— 摘要正文里出现 "physics"、
# "science"、"institute" 是家常便饭 (例如 "Researchers in physical science
# aim to uncover..."), 拿机构词去过滤会把摘要本身误杀。
_RE_FRONTMATTER = re.compile(
    r"@|"
    r"^\s*\d+\s*(?:Department|Institute|Institut|Laborator|Centre|Center|"
    r"College|School|Universit|Academy|State Key|Beijing|Shanghai)|"
    r"^\s*\d+\s*[A-Z]|"                        # "2ByteDance Seed" 这种单位行
    r"^\s*(?:Received|Accepted|Published|Submitted)\b|"
    r"^\s*[†‡§¶∗*]\s*(?:These authors|Corresponding|Electronic)|"
    r"contributed equally|"
    r"^\s*[0-9]+(?:,[0-9]+)*\s*$", re.I)


def _is_prose(text: str) -> bool:
    """判断一段文本是不是连续散文 (摘要的特征)。"""
    t = clean_text(text)
    if len(t) < 150:
        return False
    # 至少两个句号结尾的句子
    if len(re.findall(r"\.\s|\.$", t)) < 2:
        return False
    # 散文里小写虚词的比例高; 作者/单位块里几乎没有
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(words) < 20:
        return False
    common = sum(1 for w in words if w.lower() in
                 ("the", "of", "and", "in", "to", "a", "is", "are", "we",
                  "for", "that", "with", "by", "as", "on", "this", "be"))
    return common / float(len(words)) >= 0.12


def _abstract_from_layout(blocks: List[Dict[str, Any]]) -> str:
    """按版式推断摘要: 标题之后的第一段散文。

    不用"跳过机构名"来找起点 —— 作者块、单位块、邮箱、脚注、arXiv 戳都
    不是散文, 自然会被 ``_is_prose`` 挡掉; 反过来, 摘要正文里出现
    "physical science" 之类的词也不会被误伤。
    """
    if not blocks:
        return ""
    start = 0
    # 跳过标题所在块 (标题也短, 但要防止它被当成摘要起点)
    for i, b in enumerate(blocks[:4]):
        if len(b["text"]) >= 12 and not _is_noise_line(b["text"]):
            start = i + 1
            break

    chunks: List[str] = []
    for b in blocks[start:]:
        text = b["text"]
        if _RE_ABS_STOP_BLOCK.match(text):
            break
        if _is_noise_line(text):
            if chunks:
                break
            continue

        if not chunks:
            # 起点: 第一段散文
            if _is_prose(text):
                chunks.append(text)
            continue

        # 已经在收集了
        if _is_prose(text):
            chunks.append(text)
            if sum(len(c) for c in chunks) > 2600:
                break
            continue
        # 不是散文: 只有上一段明显没写完时才当作续行
        prev = chunks[-1].rstrip()
        if not prev.endswith((".", "!", "?", "。")) and len(text) >= 40:
            chunks.append(text)
            continue
        break

    body = _fix_hyphenation(clean_text(" ".join(chunks)))
    if len(body) < 120:
        return ""
    return truncate(body, 2200, ellipsis=" …")


def _extract_year(text: str, arxiv_id: str, meta: Dict[str, str],
                  filename: str) -> Optional[int]:
    """推断年份。"""
    # 1) arXiv ID 里的 YYMM 最可靠
    m = re.match(r"^(\d{2})(\d{2})\.\d{4,5}$", arxiv_id or "")
    if m:
        yy = int(m.group(1))
        year = 1900 + yy if yy >= 91 else 2000 + yy
        if 1991 <= year <= datetime.now().year + 1:
            return year
    # 2) PDF 元数据的 creationDate, 形如 D:20230815123456
    for key in ("creationDate", "CreationDate", "modDate", "ModDate"):
        raw = str(meta.get(key) or "")
        m = re.search(r"(19|20)\d{2}", raw)
        if m:
            year = int(m.group(0))
            if 1990 <= year <= datetime.now().year + 1:
                return year
    # 3) 正文里出现的年份 (取最早的那个, 参考文献里的年份都更大)
    years = [int(y) for y in re.findall(r"\b(19[89]\d|20[0-4]\d)\b", text or "")]
    years = [y for y in years if 1990 <= y <= datetime.now().year + 1]
    if years:
        return min(years)
    # 4) 文件名里的年份 (Zotero 这类管理器导出的命名是 "作者 - 年份 - 标题.pdf")
    m = re.search(r"\b(19[89]\d|20[0-4]\d)\b", os.path.basename(filename or ""))
    if m:
        year = int(m.group(0))
        if 1990 <= year <= datetime.now().year + 1:
            return year
    return None


# --------------------------------------------------------------------------
# 单个 PDF 的解析
# --------------------------------------------------------------------------
class PdfExtract:
    """一个 PDF 的解析结果。"""

    __slots__ = ("status", "error", "title", "authors", "year", "abstract",
                 "doi", "arxiv_id", "publication", "fulltext", "intro",
                 "conclusion", "depth", "pages", "backend", "kind")

    def __init__(self) -> None:
        self.status = "ok"          # ok | notext | error | skipped
        self.error = ""
        self.title = ""
        self.authors: List[str] = []
        self.year: Optional[int] = None
        self.abstract = ""
        self.doi = ""
        self.arxiv_id = ""
        self.publication = ""
        self.fulltext = ""
        self.intro = ""             # 引言节选 (depth >= sections 才有)
        self.conclusion = ""        # 结论节选 (depth >= sections 才有)
        self.depth = ""             # 这份结果是按哪一档抽的
        self.pages = 0
        self.backend = ""
        self.kind = "paper"         # paper | supplement | unknown

    def as_row(self) -> Dict[str, Any]:
        return {
            "status": self.status, "error": self.error, "title": self.title,
            "authors": json.dumps(self.authors, ensure_ascii=False),
            "year": self.year, "abstract": self.abstract, "doi": self.doi,
            "arxiv_id": self.arxiv_id, "publication": self.publication,
            "fulltext": self.fulltext, "intro": self.intro,
            "conclusion": self.conclusion, "depth": self.depth,
            "pages": self.pages,
            "backend": self.backend, "kind": self.kind,
        }


def context_for(depth: str, intro: str, conclusion: str,
                fulltext: str) -> str:
    """按读取深度拼出"送进 AI 的补充上下文"。

    这是三档深度真正影响 token 的地方 —— 抽出来的东西存在索引里是免费的,
    送进提示词才要钱。所以:

      * ``metadata``  -> 空串 (只送标题 + 摘要);
      * ``sections``  -> 引言 + 结论;
      * ``fulltext``  -> 全文节选 (前几页, 里面已经含了摘要和引言)。
    """
    if depth == "fulltext":
        return fulltext or ""
    if depth == "sections":
        parts = []
        if intro:
            parts.append("【引言】%s" % intro)
        if conclusion:
            parts.append("【结论】%s" % conclusion)
        return "\n".join(parts)
    return ""


def extract_pdf(path: str, excerpt_chars: int = 3000,
                depth: str = "sections") -> PdfExtract:
    """解析一个 PDF, 提取标题/作者/年份/摘要/DOI/arXiv ID, 以及按深度要的正文。

    ``depth`` 见 ``READ_DEPTHS``: ``metadata`` 只到摘要为止 (最快, 也最省);
    ``sections`` 再抽引言和结论; ``fulltext`` 再抽全文节选。三档是**包含**
    关系, 所以 ``fulltext`` 会把引言结论也抽出来 —— 这样从深档切回浅档时
    索引里的结果可以直接复用。
    """
    depth = normalize_depth(depth)
    out = PdfExtract()
    out.depth = depth
    try:
        doc, backend = _open_document(path)
    except PdfBackendUnavailable:
        raise
    except Exception as exc:
        out.status = "error"
        out.error = str(exc)[:300]
        return out

    out.backend = backend
    try:
        with doc:
            meta = dict(getattr(doc, "metadata", {}) or {})
            out.pages = int(getattr(doc, "page_count", 0) or 0)

            page_texts: List[str] = []
            first_blocks: List[Dict[str, Any]] = []
            first_spans: List[Dict[str, Any]] = []

            if backend == "pymupdf":
                for i in range(min(out.pages or MAX_PAGES_FOR_TEXT,
                                   MAX_PAGES_FOR_TEXT)):
                    page = doc.load_page(i)
                    blocks = _page_blocks(page)
                    if i == 0:
                        first_blocks = blocks
                        first_spans = _page_spans(page)
                    page_texts.append("\n".join(b["text"] for b in blocks))
            else:
                whole = doc._text(MAX_PAGES_FOR_TEXT)
                page_texts = _split_pages(whole)

            joined = "\n".join(page_texts)
            head = "\n".join(page_texts[:2])

            if not clean_text(joined):
                out.status = "notext"
                out.error = "PDF 没有文本层 (可能是扫描件, 需要 OCR)"
                _fill_from_meta_only(out, meta, path)
                return out

            # --- 标题: 元数据优先, 但元数据常常是垃圾, 所以要做可信度检查 ---
            meta_title = clean_text(str(meta.get("title") or ""))
            guessed = _guess_title_from_spans(first_spans) if first_spans else ""
            if _looks_like_title(meta_title) and (
                    not guessed or _title_similar(meta_title, guessed)):
                out.title = meta_title
            elif guessed:
                out.title = guessed
            elif _looks_like_title(meta_title):
                out.title = meta_title
            else:
                # 最后退到文件名 (去掉扩展名和 "作者 - 年份 - " 前缀)
                out.title = _title_from_filename(path)

            # 标题块粘上了作者/单位就先截断; 截完还是正文段落的话, 说明这一页
            # 的标题定位失败了 —— 此时文件名往往反而可靠 (Zotero 这类管理器
            # 导出的命名是"作者 - 年份 - 标题", 比乱抓的正文段落强得多)。
            out.title = _trim_title_block(out.title)
            if _is_garbage_title(out.title) or _looks_like_prose_block(out.title):
                from_file = _title_from_filename(path)
                if (_looks_like_title(from_file) and not _is_garbage_title(from_file)
                        and not _looks_like_prose_block(from_file)):
                    out.title = from_file

            # --- 作者 ---
            out.authors = _guess_authors_from_blocks(first_blocks, out.title)
            if not out.authors:
                meta_author = clean_text(str(meta.get("author") or ""))
                if meta_author and len(meta_author) < 300:
                    out.authors = [a.strip() for a in
                                   re.split(r";|,|\band\b", meta_author)
                                   if a.strip()][:25]

            # --- 标识符: 只认首页 ---
            # 不要退回全文找。全文里找到的 ID/DOI 几乎必然是参考文献里别人的,
            # 详见 _ident_from_head 的注释。
            aid, doi = _ident_from_head(head)
            out.arxiv_id = aid
            if doi:
                out.doi = norm_doi(doi)

            # --- 年份 ---
            out.year = _extract_year(head, out.arxiv_id, meta, path)

            # --- 摘要 ---
            out.abstract = _extract_abstract(page_texts, first_blocks)

            # --- 期刊信息 (arXiv 注释 / journal ref 常见于首页页脚) ---
            m = re.search(r"(?:Phys(?:ical)?\.?\s*Rev|Nature|Science|"
                          r"J\.?\s*High\s*Energy|New\s*J\.?\s*Phys)"
                          r"[^\n]{0,80}\(?20\d\d\)?", head)
            if m:
                out.publication = clean_text(m.group(0))[:120]

            # --- 正文: 按读取深度决定抽到哪一层 ---
            if depth != "metadata":
                out.intro = _find_intro(joined)
                out.conclusion = _find_conclusion_in_doc(doc, backend, out.pages)
            if depth == "fulltext":
                out.fulltext = _make_excerpt(joined, excerpt_chars)

            # --- 可用性判定: 这份 PDF 到底算不算一篇文献 ---
            if not out.title or not _looks_like_title(out.title):
                out.kind = "unknown"
            verdict, why = classify_title(out.title)
            if verdict == "junk":
                out.kind = "junk"
                out.error = why
            elif not out.abstract and not out.arxiv_id and not out.doi:
                # 没摘要也没标识符, 大概率不是一篇正常论文
                out.kind = "supplement" if out.kind == "unknown" else out.kind
    except Exception as exc:
        out.status = "error"
        out.error = str(exc)[:300]
    return out


def _split_pages(text: str) -> List[str]:
    """把整篇文本粗略切成页 (pdfminer/pypdf 没有页级接口时用)。"""
    if not text:
        return []
    parts = re.split(r"\f|\n{4,}", text)
    return [p for p in parts if p.strip()] or [text]


def _fill_from_meta_only(out: PdfExtract, meta: Dict[str, str], path: str) -> None:
    """扫描件: 没有文本层, 只能靠元数据和文件名。"""
    meta_title = clean_text(str(meta.get("title") or ""))
    out.title = meta_title if _looks_like_title(meta_title) else _title_from_filename(path)
    meta_author = clean_text(str(meta.get("author") or ""))
    if meta_author and len(meta_author) < 300:
        out.authors = [a.strip() for a in re.split(r";|,|\band\b", meta_author) if a.strip()][:25]
    out.year = _extract_year("", "", meta, path)


def _title_from_filename(path: str) -> str:
    """从文件名兜底推断标题。"""
    base = os.path.splitext(os.path.basename(path))[0]
    # 文献管理器的命名: "Murciano 等 - 2023 - Measurement-Altered Ising Quantum Criticality"
    m = re.match(r"^.*?\s+-\s+(?:19|20)\d\d\s+-\s+(.+)$", base)
    if m:
        base = m.group(1)
    base = re.sub(r"[_]+", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -_")
    return base[:300]


def _title_similar(a: str, b: str) -> bool:
    """两个标题是否指的是同一篇 (元数据标题 vs 按字号猜的标题)。"""
    from difflib import SequenceMatcher
    na = re.sub(r"[^a-z0-9]+", "", a.lower())
    nb = re.sub(r"[^a-z0-9]+", "", b.lower())
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.80


# 换行连字符修复: 排在一行末尾的连字符会把单词切断 ("de- scribed")。
# 但不能一律拼接 —— "sign-problem" 恰好断在连字符处时拼成 "signproblem"
# 更糟。所以只在前缀不是常见复合词前缀时才拼接。
_HYPHEN_PREFIXES = set("""
non self anti pre post multi semi quasi co re bi tri two three four many few
one high low long short strong weak spin charge lattice field sign free
state band wave gauge flux mean real time space energy density
""".split())
_RE_SOFT_HYPHEN = re.compile(r"\b([A-Za-z]{3,})-\s+([a-z]{3,})\b")


def _fix_hyphenation(text: str) -> str:
    """把 PDF 换行断开的单词接回去。"""
    def _join(m: "re.Match") -> str:
        head = m.group(1)
        if head.lower() in _HYPHEN_PREFIXES:
            return m.group(0)
        return head + m.group(2)
    return _RE_SOFT_HYPHEN.sub(_join, text)


def _make_excerpt(text: str, limit: int) -> str:
    """截取全文里信息量最大的开头部分。"""
    if not text or limit <= 0:
        return ""
    body = _fix_hyphenation(clean_text(text))
    for marker in ("\nReferences\n", "\nREFERENCES\n", "\nBibliography\n",
                   "\nAcknowledg", "\nACKNOWLEDG"):
        idx = body.find(marker)
        if idx > limit:
            body = body[:idx]
            break
    return truncate(body, limit, ellipsis=" …")


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,   -- 规范化路径 (Windows 下大小写无关), 作为键
    disp_path   TEXT NOT NULL,      -- 原始大小写路径, 用于展示
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    sha1        TEXT,
    status      TEXT NOT NULL,
    error       TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    authors     TEXT DEFAULT '[]',
    year        INTEGER,
    abstract    TEXT DEFAULT '',
    doi         TEXT DEFAULT '',
    arxiv_id    TEXT DEFAULT '',
    publication TEXT DEFAULT '',
    fulltext    TEXT DEFAULT '',
    intro       TEXT DEFAULT '',
    conclusion  TEXT DEFAULT '',
    depth       TEXT DEFAULT '',    -- 上次按哪一档读的; 决定能不能复用
    pages       INTEGER DEFAULT 0,
    backend     TEXT DEFAULT '',
    kind        TEXT DEFAULT 'paper',
    seen_at     TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_sha1 ON files(sha1);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- 用户自己给文献打的标签 ("文献列表"页可以编辑)。
-- 单独一张表而不是往 files 里加一列, 有两个原因: 一是标签是**用户数据**,
-- 不是解析结果 —— 重建索引时不该跟着一起没; 二是 files 加列会触发整表作废
-- (见 _check_schema), 用户只是想要个标签, 不该为此把 200 多篇 PDF 重读一遍。
CREATE TABLE IF NOT EXISTS tags (
    path TEXT PRIMARY KEY,          -- 与 files.path 同一套规范化规则
    tags TEXT NOT NULL DEFAULT ''   -- 逗号分隔
);
"""


def _ddl_columns() -> List[str]:
    """从 ``_INDEX_DDL`` 里解析出 files 表的列名。

    让"该有哪些列"只有一个来源: 往 DDL 里加一列, 升级检查自动跟上, 不会出现
    "DDL 改了但升级检查漏了"这种最难查的不一致。
    """
    m = re.search(r"CREATE TABLE IF NOT EXISTS files\s*\((.*?)\n\);",
                  _INDEX_DDL, re.S)
    if not m:
        return []
    cols = []
    for line in m.group(1).splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        if line:
            cols.append(line.split()[0])
    return cols


# files 表应有的全部列。老版本建的表列更少, 升级时要靠这个判断出来。
_FILES_COLUMNS = _ddl_columns()


def _norm_key(path: str) -> str:
    """规范化路径作为索引主键 (Windows 下大小写不敏感)。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def split_tags(raw: str) -> List[str]:
    """把用户输入的标签串切成列表。

    中英文逗号都认 (中文输入法下打出来的多半是"，"), 去重且保持输入顺序 ——
    用 set 会变成随机顺序, 用户编辑完再看发现顺序变了会以为存错了。
    """
    out: List[str] = []
    for part in re.split(r"[,，;；]", raw or ""):
        tag = part.strip()
        if tag and tag not in out:
            out.append(tag[:40])
    return out[:20]


class PdfIndex:
    """PDF 解析结果的增量索引 (SQLite)。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_INDEX_DDL)
        self.invalidated = self._check_schema()
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(k, v) VALUES('schema', ?)",
            (str(SCHEMA_VERSION),))
        self.conn.commit()

    def _table_columns(self) -> List[str]:
        try:
            rows = self.conn.execute("PRAGMA table_info(files)").fetchall()
        except sqlite3.Error:
            return []
        return [r["name"] for r in rows]

    def _check_schema(self) -> int:
        """解析逻辑升级或表结构变了 -> 旧索引整表作废并重建。

        索引只按 (size, mtime) 判断"要不要重解析", 它并不知道提取代码改了。
        不在这里清掉的话, 换了提取逻辑也仍旧复用旧结果, 白改。

        **必须 DROP 再建, 不能只 DELETE 行**: 老版本的 files 表少几列
        (intro/conclusion/depth), 而 ``CREATE TABLE IF NOT EXISTS`` 遇到已存在
        的表是直接跳过的 —— 于是旧表结构原样留着, 删完行再插就撞上
        "table files has no column named intro", 整个文献库一篇都读不出来。
        索引是可重建的派生数据, 整表丢掉没有任何损失。

        返回作废掉的记录数。
        """
        row = self.conn.execute("SELECT v FROM meta WHERE k = 'schema'").fetchone()
        old = "?"
        if row is not None:
            old = row["v"]
        try:
            same_version = int(old) == SCHEMA_VERSION
        except (TypeError, ValueError):
            same_version = False

        have = set(self._table_columns())
        missing = [c for c in _FILES_COLUMNS if c not in have]
        n = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM files").fetchone()["n"])
        if same_version and not missing:
            return 0
        if not missing and not n:
            # 全新的空库: 表刚由 _INDEX_DDL 按当前结构建好, 不用动它
            return 0

        self.conn.execute("DROP TABLE IF EXISTS files")
        self.conn.executescript(_INDEX_DDL)
        log("PDF 索引格式从 v%s 升到 v%d%s, %d 条旧记录作废, 将重新解析"
            % (old, SCHEMA_VERSION,
               (" (旧表缺列: %s)" % ", ".join(missing)) if missing else "", n),
            "warn")
        return n

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PdfIndex":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- 读 ---------------------------------------------------------------
    def get(self, path: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM files WHERE path = ?",
                                (_norm_key(path),))
        return cur.fetchone()

    def by_sha1(self, sha1: str) -> Optional[sqlite3.Row]:
        if not sha1:
            return None
        cur = self.conn.execute(
            "SELECT * FROM files WHERE sha1 = ? AND status IN ('ok','notext') "
            "ORDER BY seen_at DESC LIMIT 1", (sha1,))
        return cur.fetchone()

    def rows(self) -> List[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM files"))

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM files GROUP BY status"):
            out[row["status"]] = row["n"]
        return out

    def all_tags(self) -> Dict[str, List[str]]:
        """一次读出全部标签: ``{规范化路径: [标签, ...]}``。"""
        out: Dict[str, List[str]] = {}
        try:
            for row in self.conn.execute("SELECT path, tags FROM tags"):
                out[row["path"]] = split_tags(row["tags"])
        except Exception as exc:
            log("读取文献标签失败: %s" % exc, "warn")
        return out

    def set_tags(self, path: str, tags: Sequence[str]) -> None:
        """写一篇文献的标签。空标签 = 删除这一行。"""
        key = _norm_key(path)
        clean = split_tags(",".join(tags))
        if not clean:
            self.conn.execute("DELETE FROM tags WHERE path = ?", (key,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO tags(path, tags) VALUES(?, ?)",
                (key, ",".join(clean)))
        self.conn.commit()

    # -- 写 ---------------------------------------------------------------
    def upsert(self, path: str, size: int, mtime_ns: int, sha1: str,
               ext: PdfExtract) -> None:
        row = ext.as_row()
        self.conn.execute(
            """INSERT OR REPLACE INTO files
               (path, disp_path, size, mtime_ns, sha1, status, error, title,
                authors, year, abstract, doi, arxiv_id, publication, fulltext,
                intro, conclusion, depth, pages, backend, kind, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_norm_key(path), os.path.abspath(path), size, mtime_ns, sha1,
             row["status"], row["error"], row["title"], row["authors"],
             row["year"], row["abstract"], row["doi"], row["arxiv_id"],
             row["publication"], row["fulltext"], row["intro"],
             row["conclusion"], row["depth"], row["pages"],
             row["backend"], row["kind"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def touch(self, path: str, size: int, mtime_ns: int) -> None:
        """只更新 seen_at (文件没变, 只是又见到一次)。"""
        self.conn.execute(
            "UPDATE files SET seen_at = ?, size = ?, mtime_ns = ? WHERE path = ?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), size, mtime_ns,
             _norm_key(path)))

    def reuse(self, path: str, size: int, mtime_ns: int, sha1: str,
              src: sqlite3.Row) -> None:
        """内容相同的文件换了位置: 直接搬运解析结果, 不重新解析。"""
        self.conn.execute(
            """INSERT OR REPLACE INTO files
               (path, disp_path, size, mtime_ns, sha1, status, error, title,
                authors, year, abstract, doi, arxiv_id, publication, fulltext,
                intro, conclusion, depth, pages, backend, kind, seen_at)
               SELECT ?,?,?,?,?,status,error,title,authors,year,abstract,doi,
                      arxiv_id,publication,fulltext,intro,conclusion,depth,
                      pages,backend,kind,?
               FROM files WHERE path = ?""",
            (_norm_key(path), os.path.abspath(path), size, mtime_ns, sha1,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), src["path"]))

    def mark_missing(self, alive_keys: Iterable[str]) -> int:
        """把这次扫描没见到的记录标成 missing (不删除, 可能只是临时拔了盘)。"""
        alive = set(alive_keys)
        n = 0
        for row in list(self.conn.execute("SELECT path FROM files")):
            if row["path"] not in alive:
                self.conn.execute(
                    "UPDATE files SET status = 'missing' WHERE path = ?",
                    (row["path"],))
                n += 1
        return n

    def commit(self) -> None:
        self.conn.commit()

    def forget(self, paths: Optional[Sequence[str]] = None) -> int:
        """删掉索引记录, 强制下次重新解析。"""
        if paths is None:
            cur = self.conn.execute("DELETE FROM files")
        else:
            cur = self.conn.execute(
                "DELETE FROM files WHERE path IN (%s)"
                % ",".join("?" for _ in paths),
                [_norm_key(p) for p in paths])
        self.conn.commit()
        return cur.rowcount


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def scan_pdfs(folders: Sequence[Dict[str, Any]],
              skip_patterns: Optional[Sequence[str]] = None) -> List[str]:
    """递归扫描配置的文件夹, 返回 PDF 绝对路径列表。

    用 realpath 去重, 避免符号链接/junction 造成的无限递归与重复。
    """
    found: List[str] = []
    seen: set = set()
    skip_re = ([re.compile(p, re.I) for p in skip_patterns]
               if skip_patterns else [])

    for folder in folders:
        if not isinstance(folder, dict):
            continue
        if not folder.get("enabled", True):
            continue
        root = str(folder.get("path") or "").strip()
        if not root:
            continue
        if not os.path.isdir(root):
            log("PDF 目录不存在, 跳过: %s" % root, "warn")
            continue
        recursive = bool(folder.get("recursive", True))

        if recursive:
            walker = os.walk(root, followlinks=False)
        else:
            try:
                entries = sorted(os.listdir(root))
            except Exception as exc:
                log("无法列出目录 %s: %s" % (root, exc), "warn")
                continue
            walker = [(root, [], entries)]

        for dirpath, dirnames, filenames in walker:
            # 跳过隐藏目录和常见的无关目录
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in
                           (".git", "node_modules", "__pycache__", ".trash")]
            for name in filenames:
                if not name.lower().endswith(".pdf"):
                    continue
                if any(rx.search(name) for rx in skip_re):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    real = os.path.realpath(full)
                except Exception:
                    real = full
                key = os.path.normcase(real)
                if key in seen:
                    continue
                seen.add(key)
                found.append(full)

    found.sort()
    return found


def _sha1_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            h.update(data)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
class ScanStats:
    """一次扫描的统计, 用于向用户交代"这次到底干了多少活"。"""

    def __init__(self) -> None:
        self.total = 0
        self.reused = 0        # 索引里已有, 没重新解析
        self.moved = 0         # 内容没变只是换了路径
        self.extracted = 0     # 真正解析了
        self.failed = 0
        self.notext = 0
        self.skipped_kind = 0
        self.junk = 0          # 乱码/补充材料/审稿意见, 不进文献库
        self.missing = 0
        self.deepened = 0      # 上次读得比这次浅, 为补内容而重读
        self.depth = ""        # 这次用的读取深度

    def to_dict(self) -> Dict[str, int]:
        return {
            "total": self.total, "reused": self.reused, "moved": self.moved,
            "extracted": self.extracted, "failed": self.failed,
            "notext": self.notext, "skipped_kind": self.skipped_kind,
            "junk": self.junk, "missing": self.missing,
            "deepened": self.deepened, "depth": self.depth,
        }

    def summary(self) -> str:
        out = ("共 %d 个 PDF: 复用已有解析 %d, 内容搬家 %d, 新解析 %d, "
               "无文本层 %d, 失败 %d"
               % (self.total, self.reused, self.moved, self.extracted,
                  self.notext, self.failed))
        if self.deepened:
            out += ", 因深度加深而重读 %d" % self.deepened
        if self.junk:
            out += ", 非论文 (已忽略) %d" % self.junk
        return out


def read_pdf_library(cfg: Dict[str, Any], force: bool = False,
                     progress: Optional[Any] = None) -> Tuple[List[LibraryPaper], ScanStats]:
    """读取 PDF 文件夹文献库。

    ``force=True`` 时忽略索引, 全部重新解析。
    ``progress`` 是个可选回调 ``fn(done, total, path)``, 供 UI 显示进度。
    """
    lcfg = cfg.get("library", {})
    folders = cfg.get("pdf_folders") or []
    excerpt_chars = int(lcfg.get("fulltext_excerpt_chars", 3000))
    skip_patterns = lcfg.get("skip_patterns") or DEFAULT_SKIP_PATTERNS
    concurrency = max(1, int(lcfg.get("concurrency", 4)))
    depth = normalize_depth(lcfg.get("read_depth", "sections"))

    stats = ScanStats()
    stats.depth = depth
    files = scan_pdfs(folders, skip_patterns)
    stats.total = len(files)
    log("读取深度: %s" % DEPTH_LABELS[depth])

    db_path = _index_db_path(cfg)

    # 一个 PDF 都没扫到时不能直接返回 —— 目录被移除或磁盘没挂上时, 索引里
    # 的旧记录还得照常标成 missing, 否则 UI 会一直显示"已读"。
    if not files:
        log("PDF 文件夹里没有找到 PDF (检查 pdf_folders 配置)", "warn")
        if os.path.exists(db_path):
            with PdfIndex(db_path) as index:
                stats.missing = index.mark_missing([])
                index.commit()
        return [], stats

    log("扫描到 %d 个 PDF" % len(files))

    papers: List[LibraryPaper] = []
    todo: List[Tuple[str, int, int]] = []      # 需要算 sha1 / 解析的

    with PdfIndex(db_path) as index:
        if force:
            n = index.forget()
            log("已清空 PDF 索引 (%d 条), 全部重新解析" % n, "warn")

        # --- 第 1 步: 快速路径, 没变的直接复用 ---
        for path in files:
            try:
                st = os.stat(path)
            except OSError as exc:
                log("无法读取文件属性 %s: %s" % (path, exc), "warn")
                stats.failed += 1
                continue
            size, mtime_ns = int(st.st_size), int(st.st_mtime_ns)
            row = index.get(path)
            if (row is not None and not force
                    and row["size"] == size and row["mtime_ns"] == mtime_ns
                    and row["status"] in ("ok", "notext", "skipped")):
                # 文件没变, 但上次读得比这次浅 -> 得重读补上缺的那部分。
                # 不判这一条的话, 用户从"标题+摘要"切到"全文"会一个文件都不
                # 重读, 索引里 fulltext 全是空串, 而界面显示"全部已读"。
                if not _depth_covers(row["depth"], depth):
                    stats.deepened += 1
                    todo.append((path, size, mtime_ns))
                    continue
                stats.reused += 1
                index.touch(path, size, mtime_ns)
                _row_to_paper(row, path, papers, depth)
            else:
                todo.append((path, size, mtime_ns))

        index.commit()
        log("增量: %d 个文件未变动直接复用, %d 个需要处理"
            % (stats.reused, len(todo)))

        # --- 第 2 步: 算 sha1, 识别"内容搬家" ---
        still_todo: List[Tuple[str, int, int, str]] = []
        if todo:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(_sha1_file, p): (p, s, m)
                           for p, s, m in todo}
                for fut in as_completed(futures):
                    path, size, mtime_ns = futures[fut]
                    try:
                        digest = fut.result()
                    except Exception as exc:
                        log("计算哈希失败 %s: %s" % (path, exc), "warn")
                        stats.failed += 1
                        continue
                    src = index.by_sha1(digest)
                    if (src is not None and not force
                            and _depth_covers(src["depth"], depth)):
                        index.reuse(path, size, mtime_ns, digest, src)
                        stats.moved += 1
                        log("内容已解析过, 复用: %s" % os.path.basename(path), "dbg")
                        _row_to_paper(src, path, papers, depth)
                    else:
                        still_todo.append((path, size, mtime_ns, digest))
            index.commit()

        if stats.moved:
            log("有 %d 个文件内容与已解析过的相同 (移动/改名), 未重复解析"
                % stats.moved)

        # --- 第 3 步: 真正解析 ---
        if still_todo:
            if stats.deepened:
                log("其中 %d 个是因为读取深度加深而重读 (其余是新增/变动)"
                    % stats.deepened)
            log("需要解析 %d 个 PDF ..." % len(still_todo))
            done = 0
            results: List[Tuple[str, int, int, str, PdfExtract]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(extract_pdf, p, excerpt_chars, depth): (p, s, m, d)
                    for p, s, m, d in still_todo
                }
                for fut in as_completed(futures):
                    path, size, mtime_ns, digest = futures[fut]
                    done += 1
                    try:
                        ext = fut.result()
                    except PdfBackendUnavailable:
                        raise
                    except Exception as exc:
                        ext = PdfExtract()
                        ext.status = "error"
                        ext.error = str(exc)[:300]
                    results.append((path, size, mtime_ns, digest, ext))
                    if progress is not None:
                        try:
                            progress(done, len(still_todo), path)
                        except Exception:
                            pass
                    if done % 20 == 0 or done == len(still_todo):
                        log("  解析进度 %d/%d" % (done, len(still_todo)), "dbg")

            for path, size, mtime_ns, digest, ext in results:
                if ext.status == "error":
                    stats.failed += 1
                    log("解析失败 %s: %s" % (os.path.basename(path), ext.error), "warn")
                elif ext.status == "notext":
                    stats.notext += 1
                elif ext.kind == "junk":
                    stats.junk += 1
                    log("不是论文, 已忽略 %s: %s"
                        % (os.path.basename(path), ext.error), "dbg")
                elif ext.kind == "supplement":
                    stats.skipped_kind += 1
                else:
                    stats.extracted += 1
                index.upsert(path, size, mtime_ns, digest, ext)
                _ext_to_paper(ext, path, papers, depth)
            index.commit()

        # --- 第 4 步: 标记消失的文件 ---
        stats.missing = index.mark_missing(_norm_key(p) for p in files)
        index.commit()
        if stats.missing:
            log("有 %d 条索引记录对应的文件已不在扫描范围内 (标为 missing, 未删除)"
                % stats.missing)

    # item_id 用负数: 这个字段的历史来源是 Zotero 的 itemID (正整数), 负数
    # 保证不会和任何外部编号撞上, 也让日志里一眼能看出"这是 PDF 来源"。
    # 注意 library.load_library 之后会把它重编成连续正整数, 所以它只在
    # read_pdf_library 这一层有区分来源的意义。
    papers.sort(key=lambda p: (p.year or 0), reverse=True)
    for i, p in enumerate(papers, 1):
        p.item_id = -i

    log("PDF 文献库: %s" % stats.summary())
    return papers, stats


def _row_to_paper(row: Any, path: str, out: List[LibraryPaper],
                  depth: str = "sections") -> None:
    """把索引行还原成 LibraryPaper。

    ``context`` 按**这次配置的**深度拼, 不是按存的时候那一档 —— 索引里可能
    存着比这次要的更全的内容 (深档切浅档时复用), 该送多少由当前配置说了算。
    """
    if (row["kind"] or "") == "junk":
        return
    try:
        authors = json.loads(row["authors"] or "[]")
        if not isinstance(authors, list):
            authors = []
    except Exception:
        authors = []
    keys = set(row.keys())
    out.append(LibraryPaper(
        item_id=0,
        key=hashlib.sha1(_norm_key(path).encode("utf-8")).hexdigest()[:8],
        title=row["title"] or "",
        abstract=row["abstract"] or "",
        date=str(row["year"] or ""),
        year=row["year"],
        doi=row["doi"] or "",
        url="",
        authors=[str(a) for a in authors],
        publication=row["publication"] or "",
        item_type="pdf",
        arxiv_id=row["arxiv_id"] or "",
        fulltext=row["fulltext"] or "",
        context=context_for(depth,
                            (row["intro"] if "intro" in keys else "") or "",
                            (row["conclusion"] if "conclusion" in keys else "") or "",
                            row["fulltext"] or ""),
        tags=[],
    ))


def _ext_to_paper(ext: PdfExtract, path: str, out: List[LibraryPaper],
                  depth: str = "sections") -> None:
    """把新解析的结果变成 LibraryPaper。

    乱码标题 / 审稿意见这类 ``kind == "junk"`` 的文件不进文献库 —— 它们会被
    当成"你已经有的论文"参与去重, 还会把垃圾词带进研究画像。
    """
    if ext.status == "error" or ext.kind == "junk":
        return
    out.append(LibraryPaper(
        item_id=0,
        key=hashlib.sha1(_norm_key(path).encode("utf-8")).hexdigest()[:8],
        title=ext.title or "",
        abstract=ext.abstract or "",
        date=str(ext.year or ""),
        year=ext.year,
        doi=ext.doi or "",
        url="",
        authors=list(ext.authors),
        publication=ext.publication or "",
        item_type="pdf",
        arxiv_id=ext.arxiv_id or "",
        fulltext=ext.fulltext or "",
        context=context_for(depth, ext.intro, ext.conclusion, ext.fulltext),
        tags=[],
    ))


def index_stats(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """给 UI 用: 报告索引现状 (已读多少、待读多少、按哪个深度读的)。

    ``pending`` 里含**因为深度不够而需要重读**的文件 —— 用户把档位从
    "标题+摘要" 调到 "全文" 之后, 界面上待读数应该立刻变大, 否则他会以为
    改设置没生效。
    """
    lcfg = cfg.get("library", {})
    db_path = _index_db_path(cfg)

    depth = normalize_depth(lcfg.get("read_depth", "sections"))
    files = scan_pdfs(cfg.get("pdf_folders") or [],
                      lcfg.get("skip_patterns") or DEFAULT_SKIP_PATTERNS)
    out: Dict[str, Any] = {
        "db_path": db_path, "db_exists": os.path.exists(db_path),
        "files_on_disk": len(files), "indexed": 0, "pending": 0,
        "by_status": {}, "depth": depth, "depth_label": DEPTH_LABELS[depth],
        "shallower": 0,
    }
    if not out["db_exists"]:
        out["pending"] = len(files)
        return out

    try:
        with PdfIndex(db_path) as index:
            by_status = index.counts()
            out["by_status"] = by_status
            out["indexed"] = sum(
                v for k, v in by_status.items()
                if k in ("ok", "notext", "skipped"))
            known = {row["path"]: (row["size"], row["mtime_ns"], row["depth"])
                     for row in index.rows()}
    except Exception as exc:
        log("读取 PDF 索引失败: %s" % exc, "warn")
        out["pending"] = len(files)
        return out

    pending = 0
    shallow = 0
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = _norm_key(path)
        rec = known.get(key)
        if rec is None or rec[:2] != (int(st.st_size), int(st.st_mtime_ns)):
            pending += 1
        elif not _depth_covers(rec[2], depth):
            pending += 1
            shallow += 1
    out["pending"] = pending
    out["shallower"] = shallow
    return out


# --------------------------------------------------------------------------
# 文献列表 (界面第 4 页)
# --------------------------------------------------------------------------
def _index_db_path(cfg: Dict[str, Any]) -> str:
    """索引库的绝对路径。"""
    db_path = (cfg.get("library", {}).get("index_db")
               or "library_index.sqlite")
    if not os.path.isabs(db_path):
        from .config import project_root
        db_path = os.path.join(project_root(), db_path)
    return db_path


def _auto_tags(status: str, publication: str, arxiv_id: str,
               exists: bool) -> List[str]:
    """从索引里已有的字段推出几个"事实型"标签。

    **刻意不从目录名推标签**: 本程序的典型文献库是 Zotero 的 storage 目录,
    每篇论文一个以 8 位大写字母数字命名的文件夹 —— 那种"标签"对用户没有任何
    意义, 只会把标签这一列占满噪声。真正想分类就自己编辑标签 (存 tags 表),
    这里只放"程序确知的事实"。
    """
    out: List[str] = []
    if publication:
        out.append("已发表")
    elif arxiv_id:
        out.append("arXiv")
    if status == "notext":
        out.append("无文本层")
    elif status == "error":
        out.append("解析失败")
    if not exists or status == "missing":
        out.append("文件已不在")
    return out


def library_records(cfg: Dict[str, Any],
                    progress: Optional[Any] = None) -> List[Dict[str, Any]]:
    """给"文献列表"页用: 索引里已读的文献, 一条一条带齐展示字段。

    只读数据库, 外加对每条路径一次 ``os.path.exists`` —— **不遍历目录**。
    文献库放在网络盘或移动硬盘上时, 走一遍目录要几秒到几十秒, 而这一页是
    随点随开的; 缺文件与否用 exists 判断就够了。

    返回按 (年份倒序, 标题) 排好的 dict 列表, 排序键和 ``library.order_papers``
    保持一致, 免得同一批文献在"读取"和"列表"两处显示成不同顺序。
    """
    db_path = _index_db_path(cfg)
    out: List[Dict[str, Any]] = []
    if not os.path.exists(db_path):
        return out

    try:
        with PdfIndex(db_path) as index:
            rows = index.rows()
            tags = index.all_tags()
    except Exception as exc:
        log("读文献列表失败: %s" % exc, "warn")
        return out

    for i, row in enumerate(rows):
        keys = set(row.keys())
        status = row["status"] or ""
        # 乱码/审稿意见这类不是论文的文件本来就不进文献库, 列表里也不该出现
        if (row["kind"] if "kind" in keys else "paper") == "junk":
            continue
        path = row["disp_path"] or row["path"]
        try:
            authors = json.loads(row["authors"] or "[]")
            if not isinstance(authors, list):
                authors = []
        except Exception:
            authors = []
        depth = (row["depth"] if "depth" in keys else "") or ""
        publication = row["publication"] or ""
        arxiv_id = row["arxiv_id"] or ""
        exists = os.path.exists(path)
        if progress is not None and i % 25 == 0:
            try:
                progress(i + 1, len(rows), os.path.basename(path))
            except Exception:
                pass

        user_tags = list(tags.get(_norm_key(path), []))
        auto = _auto_tags(status, publication, arxiv_id, exists)

        out.append({
            "path": path,
            "file": os.path.basename(path),
            "title": (row["title"] or "").strip() or os.path.splitext(
                os.path.basename(path))[0],
            "authors": [str(a) for a in authors],
            "publication": publication,
            "year": row["year"],
            "arxiv_id": arxiv_id,
            "doi": row["doi"] or "",
            "tags": user_tags,
            "auto_tags": auto,
            "status": status,
            "depth": depth,
            "depth_label": DEPTH_LABELS.get(normalize_depth(depth), "")
            if depth else "",
            "pages": int((row["pages"] if "pages" in keys else 0) or 0),
            "seen_at": (row["seen_at"] if "seen_at" in keys else "") or "",
            "exists": exists,
        })

    def _key(rec: Dict[str, Any]) -> Any:
        try:
            y = int(rec.get("year") or 0)
        except (TypeError, ValueError):
            y = 0
        return (-y, (rec.get("title") or "").lower())

    out.sort(key=_key)
    log("文献列表: %d 篇已读" % len(out))
    return out


def set_paper_tags(cfg: Dict[str, Any], path: str,
                   tags: Sequence[str]) -> bool:
    """写一篇文献的标签 (界面"文献列表"页用)。返回是否成功。

    单独开一次连接写一行就关 —— 这个操作是用户手动点的, 频率极低, 为它长期
    持有一个连接反而要处理"文献库正在被读取时"的并发问题。
    """
    db_path = _index_db_path(cfg)
    try:
        with PdfIndex(db_path) as index:
            index.set_tags(path, tags)
        return True
    except Exception as exc:
        log("写标签失败 (%s): %s" % (path, exc), "warn")
        return False


def status_label(status: str) -> str:
    """索引状态 -> 界面上给人看的字。"""
    return {
        "ok": "已读",
        "notext": "无文本层",
        "error": "解析失败",
        "missing": "文件已不在",
        "skipped": "已跳过",
    }.get(status or "", status or "")
