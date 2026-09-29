"""通用工具: 文本规范化、arXiv ID 提取、HTTP 会话与重试、磁盘缓存、JSON 容错解析。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


# --------------------------------------------------------------------------
# 控制台
# --------------------------------------------------------------------------
def setup_console() -> None:
    """Windows 控制台默认 GBK, 输出中文/希腊字母会崩。强制切到 UTF-8。

    同时打开行缓冲: 输出被重定向到文件时 (python run.py > log.txt) 默认是块缓冲,
    进度会一直看不到, 直到进程结束才一次性刷出来。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace",
                            line_buffering=True)
            except TypeError:
                # 少数替代实现不支持 line_buffering
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            except Exception:
                pass


_LEVEL_PREFIX = {"info": "[*]", "ok": "[+]", "warn": "[!]", "err": "[x]", "dbg": "[.]"}

# 日志接收器。GUI 把自己的队列投递函数注册进来, 就能实时接住管线日志,
# 而不需要去改上百个 log() 调用点。CLI 下这个列表始终为空, 行为不变。
_log_sinks: List[Callable[[str, str], None]] = []


def add_log_sink(fn: Callable[[str, str], None]) -> None:
    """注册一个日志接收器 ``fn(msg, level)``。"""
    if fn not in _log_sinks:
        _log_sinks.append(fn)


def remove_log_sink(fn: Callable[[str, str], None]) -> None:
    """注销日志接收器 (UI 关闭时调用, 避免往已销毁的控件投递)。"""
    try:
        _log_sinks.remove(fn)
    except ValueError:
        pass


def log(msg: str, level: str = "info") -> None:
    """带级别前缀的日志输出。

    输出到 stdout 的同时分发给所有已注册的接收器。接收器抛异常不能影响
    主流程 —— 日志失败绝不该让一次跑了几分钟的任务崩掉。
    """
    prefix = _LEVEL_PREFIX.get(level, "[*]")
    line = "%s %s" % (prefix, msg)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # 极端情况下退化为 ASCII。注意: 打包成窗口程序 (--windowed) 之后
        # sys.stdout 是 None, 这时 print 本身是空操作, 走不到这个分支;
        # 但真走到了也不能因为 stdout 为 None 再抛一次。
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(line.encode(enc, "replace").decode(enc), flush=True)
        except Exception:
            pass
    except Exception:
        # 输出管道被关掉 (BrokenPipeError) 之类。日志写不出去不该让一次跑了
        # 几分钟的任务崩在这里 —— 下面还有 UI 接收器接着, 界面里照样看得到。
        pass

    for fn in list(_log_sinks):
        try:
            fn(msg, level)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 文本规范化
# --------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_LATEX_RE = re.compile(r"\$[^$]*\$|\\(?:[a-zA-Z]+|.)")


def strip_latex(text: str) -> str:
    """去掉 LaTeX 数学与命令, 保留可读文本。

    用于标题归一化与展示; 摘要中的公式会被替换为空格, 避免污染相似度计算。
    """
    if not text:
        return ""
    text = _LATEX_RE.sub(" ", text)
    text = text.replace("{", " ").replace("}", " ").replace("\\", " ")
    return text


def normalize_title(title: str) -> str:
    """标题归一化: 用于去重比对。

    小写 -> 去 LaTeX -> Unicode NFKD -> 去标点 -> 折叠空白。
    """
    if not title:
        return ""
    t = strip_latex(title)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = t.lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def clean_text(text: str) -> str:
    """清理 arXiv 页面抓下来的文本: 折叠空白, 去掉不可见字符。"""
    if not text:
        return ""
    text = text.replace(" ", " ").replace("​", "")
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int, ellipsis: str = " …") -> str:
    """按字符数截断, 尽量在句子边界断开。"""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    for sep in (". ", "。", "; ", ", "):
        idx = cut.rfind(sep)
        if idx > limit * 0.6:
            return cut[: idx + len(sep)].rstrip() + ellipsis
    return cut.rstrip() + ellipsis


# --------------------------------------------------------------------------
# 候选论文的显示格式
#
# 这几件事**界面和报告都要做**, 所以放在这里当唯一出处。以前报告里自己有一份
# `_fmt_date`, 界面要是再抄一份, 两边迟早会漂 —— 同一篇论文在列表里写 2026-09-25、
# 在报告里写 2026/9/25 这种事, 没人会当成 bug 去修, 但它就是错的。
# --------------------------------------------------------------------------
def fmt_date(cand: Any) -> str:
    """提交日期 (v1); 没有就退回最新版本时间, 都没有给个破折号。"""
    ref = getattr(cand, "published", None) or getattr(cand, "updated", None)
    try:
        return ref.strftime("%Y-%m-%d") if ref else "—"
    except Exception:
        return "—"


def fmt_authors(authors: Any, limit: int = 1) -> str:
    """作者串: 只列前 ``limit`` 位, 还有别人就缀一个"等"。

    列表里那一列很窄, 全列出来会被 Treeview 硬截成半截名字 —— "Wei Wa…" 这种
    还不如老老实实写"第一作者 等"。要看全部作者请点开详解或看报告。
    """
    names = [str(a).strip() for a in (authors or []) if str(a).strip()]
    if not names:
        return "—"
    if len(names) <= limit:
        return ", ".join(names)
    return "%s 等" % ", ".join(names[:limit])


def fmt_journal_ref(ref: Any, limit: int = 24) -> str:
    """期刊/会议简称。没发表过就写"—"(而不是留空 —— 空格子和"没数据"分不清)。"""
    text = str(ref or "").strip()
    if not text:
        return "—"
    return truncate(text, limit, "…")


def fmt_journal(cand: Any, limit: int = 24) -> str:
    """同上, 但直接从候选对象上取。"""
    return fmt_journal_ref(getattr(cand, "journal_ref", ""), limit)


def norm_doi(doi: str) -> str:
    """DOI 归一化: 去掉 URL 前缀与大小写差异。"""
    if not doi:
        return ""
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi:", "doi "):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d.strip().strip(".")


# --------------------------------------------------------------------------
# arXiv ID
# --------------------------------------------------------------------------
# 新式: 2401.12345 / 2401.12345v2 ; 旧式: cond-mat/0701001 / hep-th/9901001v3
_ARXIV_NEW_RE = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")
_ARXIV_OLD_RE = re.compile(
    r"\b([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b", re.IGNORECASE
)


def extract_arxiv_id(text: str) -> str:
    """从任意文本 (URL / archiveID / extra 字段) 中提取规范化 arXiv ID。

    返回不带版本号的形式; 旧式 ID 原样保留 (小写)。找不到返回 ""。

    注意一个易踩的坑: 形如 ``2024.12345`` 的裸数字既是 arXiv 新式 ID, 也长得像
    普通网址里的日期/编号。若不加限制, ``https://example.com/2024.12345`` 会被
    解析出一个伪 arXiv ID, 进而污染去重索引、把真正的那篇论文误判为"已在库中"。
    因此: 含 URL 时只认 arxiv.org 域名, 裸数字形式仅在没有外部 URL 时接受。
    """
    if not text:
        return ""

    # 1) 显式指向 arxiv.org 的链接 —— 最可靠
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", text, re.IGNORECASE)
    if m:
        tail = re.sub(r"\.pdf$", "", m.group(1), flags=re.IGNORECASE)
        m2 = _ARXIV_NEW_RE.search(tail) or _ARXIV_OLD_RE.search(tail)
        if m2:
            return m2.group(1).lower()

    # 2) 含非 arxiv 的 URL: 只有在文本另外明确提到 arXiv 时才继续
    has_foreign_url = bool(re.search(r"[a-zA-Z][a-zA-Z0-9+.\-]*://", text))
    if has_foreign_url and not re.search(r"arxiv", text, re.IGNORECASE):
        return ""

    # 3) 旧式 ID (带分类前缀, 如 cond-mat/0701001) —— 不会与日期混淆
    m = _ARXIV_OLD_RE.search(text)
    if m:
        return m.group(1).lower()

    # 4) 新式 ID
    m = _ARXIV_NEW_RE.search(text)
    if m:
        return m.group(1)
    return ""


def extract_arxiv_version(text: str) -> str:
    """提取版本号, 如 'v2'; 无则返回 ''。"""
    if not text:
        return ""
    m = _ARXIV_NEW_RE.search(text) or _ARXIV_OLD_RE.search(text)
    if m and m.group(2):
        return m.group(2)
    return ""


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------
_YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_year(date_str: str) -> Optional[int]:
    """从任意日期字符串中解析年份。"""
    if not date_str:
        return None
    m = _YEAR_RE.search(str(date_str))
    if not m:
        return None
    year = int(m.group(1))
    if 1800 <= year <= datetime.now().year + 2:
        return year
    return None


def parse_arxiv_date(text: str) -> Optional[datetime]:
    """解析 arXiv 页面上的 'Submitted 25 September, 2026' 之类文本。"""
    if not text:
        return None
    low = text.lower()
    ym = _YEAR_RE.search(low)
    if not ym:
        return None
    year = int(ym.group(1))
    month = 1
    for name, num in _MONTHS.items():
        if re.search(r"\b%s\b" % name, low):
            month = num
            break
    dm = re.search(r"\b(\d{1,2})\s+(?:%s)" % "|".join(_MONTHS.keys()), low)
    day = int(dm.group(1)) if dm else 1
    try:
        return datetime(year, month, min(max(day, 1), 28))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# 磁盘缓存
# --------------------------------------------------------------------------
class DiskCache:
    """极简磁盘缓存: 网络请求与 AI 响应都走这里, 便于反复调试不重复打接口。"""

    def __init__(self, root: str, namespace: str = "default", enabled: bool = True):
        self.dir = os.path.join(root, namespace)
        self.enabled = enabled
        if self.enabled:
            os.makedirs(self.dir, exist_ok=True)

    @staticmethod
    def _key(key: str) -> str:
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def path(self, key: str) -> str:
        return os.path.join(self.dir, self._key(key) + ".json")

    def get(self, key: str) -> Optional[Any]:
        if not self.enabled:
            return None
        p = self.path(key)
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload.get("value")
        except Exception:
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        try:
            tmp = self.path(key) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "value": value}, fh, ensure_ascii=False)
            os.replace(tmp, self.path(key))
        except Exception:
            pass


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def normalize_proxy(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """把配置里的 proxy 转成 requests 的 proxies 字典。

    重要: Windows 系统代理常被写成 ``https://127.0.0.1:7890``, 这会让
    requests 对代理本身发起 TLS 握手而失败。这里统一把本机代理的 scheme
    规范化成 ``http://``。

    ``None`` / ``""`` -> 不走代理; ``"auto"`` -> 读系统设置 (同样会规范化)。
    """
    if proxy is None or proxy == "":
        return None
    if str(proxy).lower() == "auto":
        try:
            from urllib.request import getproxies
            sysp = getproxies()
        except Exception:
            return None
        out = {}
        for scheme in ("http", "https"):
            val = sysp.get(scheme)
            if val:
                out[scheme] = _fix_proxy_scheme(val)
        return out or None
    fixed = _fix_proxy_scheme(str(proxy))
    return {"http": fixed, "https": fixed}


def _fix_proxy_scheme(url: str) -> str:
    """本机回环地址上的代理一律用 http:// (代理本身通常不是 TLS 端点)。"""
    u = url.strip()
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            host = u[len(prefix):]
            if host.startswith("127.0.0.1") or host.startswith("localhost"):
                return "http://" + host
            return u
    return "http://" + u


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def is_loopback_url(url: str) -> bool:
    """判断 URL 是否指向本机。

    本机服务 (本地 Ollama / vLLM / 中转) 必须绕过代理, 否则代理会去连它自己,
    请求直接失败。requests 只在 trust_env=True 时才读 NO_PROXY, 而我们刻意关掉了
    trust_env, 所以这里自己判断。
    """
    if not url:
        return False
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/:?#]+)", url)
    if not m:
        return False
    host = m.group(1).strip("[]").lower()
    if host in _LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


def proxies_for(session: "requests.Session", url: str) -> Dict[str, str]:
    """按目标地址决定本次请求用不用代理。"""
    if is_loopback_url(url):
        return {}
    return dict(getattr(session, "proxies", None) or {})


# 探测用的地址: 就用真正要访问的 arXiv API, 抓 1 条 —— 我们只关心"这条路通不通",
# 不需要真实数据。
#
# ``max_results=0`` 看着更省, 但 arXiv 不接受: 它会回一个 **HTTP 500**, 于是
# "路是通的"被读成"路断了"。踩过这个坑之后老老实实要 1 条。
PROBE_URL = ("http://export.arxiv.org/api/query"
             "?search_query=all:electron&max_results=1")

# 探测结果按 proxy 配置串缓存。一次进程只探一回: 每条检索式都探一次的话,
# 光探测就要等 18 次超时。
_PROXY_PROBE_CACHE: Dict[str, Optional[Dict[str, str]]] = {}
_PROXY_PROBE_LOCK = threading.Lock()


def _proxy_is_loopback(proxies: Dict[str, str]) -> bool:
    """代理地址是不是本机 (Clash / v2ray 之类挂在 127.0.0.1:7890 的那种)。"""
    for val in proxies.values():
        host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", str(val))
        host = host.split("@")[-1].split("/")[0].split(":")[0]
        if host.strip("[]").lower() in _LOOPBACK_HOSTS or host.startswith("127."):
            return True
    return False


def probe_route(proxies: Optional[Dict[str, str]], timeout: float = 8.0) -> bool:
    """带着指定的线路 (``None`` = 直连) 打一次探测地址, 通了返回 True。

    "通"的判定故意宽松: 只要不是 5xx 就算通。特别是 **429 必须算通** ——
    arXiv 限流说明路是好的, 只是让我们慢点, 这跟代理挂掉 (502) 是两回事,
    不能因为限流就把代理甩掉, 也不能因为限流就判定"网络断了"而收工。

    **必须自己建 ``trust_env=False`` 的会话, 不能用裸 ``requests.get``**:
    裸调用会去读 Windows 注册表里的系统代理设置, 而"直连"要的恰恰是不碰它。
    真踩过 —— 系统代理里写着 127.0.0.1:7890 (Clash 关掉之后的残留), 于是
    "直连探测"被系统代理悄悄接管, 拿到 502, 程序据此得出"直连也不通"的结论,
    把一个本来能自动修好的问题判成了绝症。
    """
    if requests is None:
        return False
    try:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": DEFAULT_UA})
        # 显式传 {} (而不是 None): None 的含义是"你自己看着办", {} 才是"不走代理"
        resp = session.get(PROBE_URL, proxies=dict(proxies or {}),
                           timeout=timeout)
        return resp.status_code < 500 or resp.status_code == 429
    except Exception:
        return False


def resolve_proxy(cfg_proxy: Optional[str], timeout: float = 8.0) -> Optional[Dict[str, str]]:
    """把配置里的代理解析成**实际能用**的 proxies 字典 (None = 直连)。

    只对本机代理做这件事, 原因是实践中踩到的坑: 用户在设置里填了
    ``http://127.0.0.1:7890`` (Clash 之类), 后来那个客户端关掉了或者上游挂了,
    端口还在监听 —— 于是每一次 arXiv 请求都拿到 502, 重试五遍、失败, 18 条检索式
    全灭, 最后只看到一句"没有抓到任何候选论文", 完全猜不到是代理的问题。

    本机代理挂掉时**直连往往正是通的** (那个代理本来就是为别的用途开的),
    所以这里探一下: 通了就照用, 不通就退回直连并明确记一条日志。非本机的代理
    (公司/学校统一出口) 不做这个猜测 —— 那种代理通常是真的必须走, 而且拿直连
    兜底会静默地改变网络行为, 比报错更难查。
    """
    key = repr(cfg_proxy)
    with _PROXY_PROBE_LOCK:
        if key in _PROXY_PROBE_CACHE:
            return _PROXY_PROBE_CACHE[key]

    proxies = normalize_proxy(cfg_proxy)
    resolved = proxies
    if proxies is not None and _proxy_is_loopback(proxies):
        if probe_route(proxies, timeout):
            log("代理 %s 可用" % list(proxies.values())[0], "dbg")
        else:
            resolved = None
            if probe_route(None, timeout):
                log("代理 %s 连不上 arXiv, 已自动改用直连 (直连是通的)。"
                    "要恢复走代理请检查代理客户端是否在运行。"
                    % list(proxies.values())[0], "warn")
            else:
                # 两条都不通: 退回原配置, 让后面正常的报错流程去说"网络有问题"
                resolved = proxies
                log("代理 %s 和直连都连不上 arXiv, 按原配置继续 (多半是网络断了)"
                    % list(proxies.values())[0], "warn")

    with _PROXY_PROBE_LOCK:
        _PROXY_PROBE_CACHE[key] = resolved
    return resolved


def reset_proxy_probe_cache() -> None:
    """清掉探测缓存 (改完设置后重新探测用)。"""
    with _PROXY_PROBE_LOCK:
        _PROXY_PROBE_CACHE.clear()


def build_session(cfg_proxy: Optional[str] = None, timeout: int = 40) -> "requests.Session":
    """构建 requests 会话。

    ``trust_env=False`` 是刻意的: 避免 requests 自动读取那份 scheme 写错的
    系统代理设置, 代理只由配置显式决定。

    代理走 ``resolve_proxy``: 本机代理探测不通时自动退回直连 (见那里的说明)。
    探测结果在一个进程内只算一次, 所以这里不用怕被反复调用。
    """
    if requests is None:
        raise RuntimeError("缺少 requests 库, 请先 pip install requests")
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": DEFAULT_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    proxies = resolve_proxy(cfg_proxy)
    if proxies:
        session.proxies.update(proxies)
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__("HTTP %d for %s" % (status, url))
        self.status = status
        self.url = url
        self.body = body


# 退避上限。没有这个上限时, base_delay=5 且重试 5 次会退化成 5/10/20/40/80 秒,
# 单条请求就要等两分半, 整体跑起来像卡死。
MAX_BACKOFF_SEC = 20.0


class RateLimiter:
    """全局请求节流器: 保证同一个域名上任意两次请求至少间隔 ``min_interval`` 秒。

    为什么要做成全局的: 限流看的是"这个 IP 在短时间里发了多少请求", 而发起请求的
    地方散在好几个模块里 (检索、翻页、重试、补充详情), 每个地方各自 ``sleep`` 只
    能保证自己那一段不密集, 加起来照样能在一秒里打出七八个请求 —— 这正是 HTTP 429
    的来源。把闸门放在唯一的出口上, 任何调用点都不可能绕过。

    线程安全 (深度解读是并发的); 退避时会把"下一次允许的时间"往后推, 于是所有
    线程一起等, 而不是各等各的。
    """

    def __init__(self, min_interval: float = 3.0) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._next_at = 0.0
        self._lock = threading.Lock()
        self.waits = 0          # 统计: 一共被动等了多少次
        self.waited_sec = 0.0

    def wait(self) -> float:
        """睡到轮到自己, 返回实际睡了多久。"""
        with self._lock:
            now = time.time()
            # 在锁里把时间片"预定"下来再出锁睡, 于是并发线程拿到的是 t+3、t+6、
            # t+9 … 各自错开, 而不是一起醒来抢同一个窗口
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.min_interval
        if delay > 0:
            self.waits += 1
            self.waited_sec += delay
            time.sleep(delay)
        return delay

    def penalize(self, seconds: float) -> None:
        """被限流了: 把下一次允许的时间往后推, 让所有线程一起冷静。"""
        with self._lock:
            self._next_at = max(self._next_at, time.time() + max(0.0, seconds))

    def stats(self) -> Dict[str, Any]:
        return {"min_interval": self.min_interval, "waits": self.waits,
                "waited_sec": round(self.waited_sec, 1)}


# 进程级共享。默认 3 秒 —— arXiv 官方文档明说"每 3 秒不要超过 1 个请求"。
GLOBAL_LIMITER = RateLimiter(3.0)


def set_global_interval(seconds: float) -> None:
    """配置加载后调用一次, 覆盖默认的 3 秒。"""
    GLOBAL_LIMITER.min_interval = max(0.0, float(seconds))


def http_get(
    session: "requests.Session",
    url: str,
    params: Optional[Dict[str, Any]] = None,
    cache: Optional[DiskCache] = None,
    retries: int = 4,
    base_delay: float = 3.0,
    timeout: Optional[int] = None,
    allow_status: Iterable[int] = (),
    limiter: Optional["RateLimiter"] = None,
) -> str:
    """带磁盘缓存与指数退避的 GET, 返回响应文本。

    对 429/5xx 做指数退避重试; 429 的退避更激进 (arXiv 限流恢复较慢)。

    **每次尝试前都要先过全局节流闸门** (``GLOBAL_LIMITER``, 默认 3 秒)。这是全流程
    唯一的 HTTP 出口, 把闸门放在这里, 检索/翻页/重试/补充详情这些调用点就都不可能
    绕过它各自打请求 —— 各睡各的加起来照样能在一秒里发七八个请求, 那正是 429 的来源。

    命中缓存时**不**过闸门: 没有发出网络请求, 没有理由等。
    """
    cache_key = url + "?" + json.dumps(params or {}, sort_keys=True)
    if cache is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            return hit

    timeout = timeout or getattr(session, "request_timeout", 40)
    limiter = limiter or GLOBAL_LIMITER
    last_exc = None  # type: Optional[Exception]
    for attempt in range(retries):
        limiter.wait()
        try:
            resp = session.get(url, params=params, timeout=timeout,
                               proxies=proxies_for(session, url))
            if resp.status_code == 200:
                text = resp.text
                if cache is not None:
                    cache.set(cache_key, text)
                return text
            if resp.status_code in tuple(allow_status):
                return resp.text
            if resp.status_code in (429, 500, 502, 503, 504):
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1.5)
                if resp.status_code == 429:
                    delay *= 2.0
                # 先夹到上限再推闸门: 否则 base_delay=3 时第 4 次重试会把整个进程
                # 的闸门推到 48 秒后, 界面上看就是"卡死了"
                delay = min(delay, MAX_BACKOFF_SEC)
                if resp.status_code == 429:
                    # 被限流了, 把闸门整体往后推。只让当前线程睡的话, 别的线程
                    # 会立刻补上一个新请求, 刚退避完又被限流, 退避等于白做。
                    limiter.penalize(delay)
                last_exc = HttpError(resp.status_code, url, resp.text[:200])
                if attempt + 1 < retries:
                    log("HTTP %d (第 %d/%d 次), %.1fs 后重试: %s"
                        % (resp.status_code, attempt + 1, retries, delay, url),
                        "warn")
                    time.sleep(delay)
                else:
                    # 没有下一次了, 别睡 —— retries=1 的调用 (富化) 会白等好几秒,
                    # 只为了在醒来之后直接抛异常
                    log("HTTP %d (第 %d/%d 次), 不再重试: %s"
                        % (resp.status_code, attempt + 1, retries, url), "warn")
                continue
            raise HttpError(resp.status_code, url, resp.text[:400])
        except HttpError:
            raise
        except Exception as exc:  # 网络层异常
            last_exc = exc
            delay = min(base_delay * (2 ** attempt) + random.uniform(0, 1.5),
                        MAX_BACKOFF_SEC)
            if attempt + 1 < retries:
                log("请求失败 (%s), %.1fs 后重试: %s"
                    % (type(exc).__name__, delay, url), "warn")
                time.sleep(delay)
            else:
                log("请求失败 (%s), 不再重试: %s"
                    % (type(exc).__name__, url), "warn")
    raise RuntimeError("重试 %d 次后仍失败: %s (%s)" % (retries, url, last_exc))


# --------------------------------------------------------------------------
# LLM 输出解析
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def safe_json_loads(text: str, default: Any = None) -> Any:
    """从 LLM 回复里稳健地抽出 JSON。

    依次尝试: 直接解析 -> 去 markdown 围栏 -> 截取最外层 {} 或 [] -> 修尾逗号。
    全部失败返回 ``default``。
    """
    if not text:
        return default
    text = text.strip()

    candidates: List[str] = [text]

    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])

    for cand in candidates:
        for attempt in (cand, _repair_json(cand)):
            if not attempt:
                continue
            try:
                return json.loads(attempt)
            except Exception:
                continue

    # 最后再试一次: 是不是被 max_tokens 截断了 (见 _salvage_truncated)
    salvaged = _salvage_truncated(text)
    if salvaged is not None:
        return salvaged
    return default


def _salvage_truncated(text: str) -> Any:
    """抢救被截断的 JSON: 保住已经完整的部分, 丢掉残缺的尾巴。

    模型输出撞上 ``max_tokens`` 时会断在半截字符串里:

        {"scores": [{"id": "a", "score": 95, "reason": "…"},
                    {"id": "b", "score": 45, "reason": "…"},
                    {"id": "c", "score": 9

    严格解析只能整个丢掉 —— 一批 8 篇的分数全没了, 退化成启发式打分, 排序结果
    跟着变。可前面那些对象**本身是完整的**, 拿来用就是了: 少几个分数, 总比一个
    都没有强。实测 300 篇的打分里, 这样丢掉的批次会让 8~16 篇退化成关键词重叠。

    做法: 找出所有"可能刚好结束了一个值"的右括号位置, 从后往前逐个试着补上闭合
    符号再解析, 第一个能解析通的就是最长的合法前缀 (越靠后保留的数据越多)。
    只有前面所有严格尝试都失败时才会走到这里, 所以它不可能让原本能解析的结果变差。

    调用方拿到的是**可能缺字段**的对象, 但三个调用点 (rank/analyze/profile) 都会
    逐字段校验, 缺的字段各自有默认值 —— 所以这里不用替它们操心完整性。
    """
    start = -1
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start < 0:
        return None

    # 字符串感知地扫一遍, 记下每个右括号的位置 (引号里的括号不算数)
    depth = 0
    in_str = False
    esc = False
    cuts: List[int] = []
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            cuts.append(i)

    head_all = text[start:]
    for i in reversed(cuts):
        head = head_all[:i - start + 1]
        # 补哪种闭合符号取决于截在哪一层, 三种都试一遍最省心
        for tail in ("]}", "]", "}"):
            try:
                return json.loads(head + tail)
            except Exception:
                continue
    return None


def _repair_json(text: str) -> str:
    """修掉 LLM 常见的 JSON 小毛病: 尾逗号、中文引号、单引号包裹的 key。"""
    if not text:
        return text
    t = text
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("：", ":")           # 全角冒号
    t = t.replace("，", ",")           # 全角逗号
    t = re.sub(r",\s*([}\]])", r"\1", t)   # 尾逗号
    return t


def chunk(items: List[Any], size: int) -> List[List[Any]]:
    """把列表切成固定大小的块。"""
    if size <= 0:
        return [items]
    return [items[i:i + size] for i in range(0, len(items), size)]


def dedupe_preserve(items: Iterable[str]) -> List[str]:
    """去重且保持原顺序 (大小写不敏感)。"""
    seen = set()
    out = []
    for it in items:
        if not it:
            continue
        key = it.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it.strip())
    return out


def retry_call(
    fn: Callable[[], Any],
    retries: int = 3,
    base_delay: float = 2.0,
    label: str = "call",
) -> Any:
    """对任意可调用对象做指数退避重试。"""
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt == retries - 1:
                break
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1.0)
            log("%s 失败 (%s), %.1fs 后重试" % (label, type(exc).__name__, delay), "warn")
            time.sleep(delay)
    raise last  # type: ignore[misc]


def fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return "%.1f 秒" % seconds
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return "%d 分 %d 秒" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %d 分" % (hours, minutes)
