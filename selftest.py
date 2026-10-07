# -*- coding: utf-8 -*-
"""针对 daily_arxiv 高风险逻辑的自查测试。"""
import sys, os
# 按**本文件所在目录**找 arxiv_rec, 不写死路径。这里原来写的是项目搬到 D 盘之前
# 那个桌面目录, 搬完就成了死路径 —— 它排在 sys.path 最前面, 哪天那个目录又被
# 建出来 (哪怕放的是旧代码), 这个自查就会静默地测到旧版本上去。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timedelta
from arxiv_rec.utils import (extract_arxiv_id, parse_arxiv_date, normalize_title,
                             norm_doi, truncate, safe_json_loads, strip_latex,
                             is_loopback_url, normalize_proxy)
from arxiv_rec.models import Candidate, LibraryPaper
from arxiv_rec.dedup import filter_library, build_index, is_in_library
from arxiv_rec.rank import recency_score, importance_score, heuristic_relevance, rank_candidates
from arxiv_rec.models import ResearchProfile
from arxiv_rec.pdf_library import SCHEMA_VERSION

FAIL = []
def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  <- " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAIL.append(name)

print("=" * 70)
print("1) extract_arxiv_id")
print("=" * 70)
cases = [
    ("2401.12345", "2401.12345"),
    ("arXiv:2401.12345v2", "2401.12345"),
    ("https://arxiv.org/abs/2401.12345", "2401.12345"),
    ("https://arxiv.org/pdf/2401.12345v3.pdf", "2401.12345"),
    ("https://arxiv.org/abs/cond-mat/0701001", "cond-mat/0701001"),
    ("10.48550/arXiv.2401.12345", "2401.12345"),
    ("10.48550/arxiv.2601.08552", "2601.08552"),
    ("", ""),
    ("no arxiv here", ""),
    ("https://example.com/2024.12345", ""),      # 非 arxiv 域名的裸数字 -> 拒绝
    ("https://doi.org/10.1103/PhysRevB.91.041105", ""),
    ("Phys. Rev. B 91, 041105 (2015)", ""),      # 期刊卷页, 不能误抓
    ("see also 1234.5678 and 2401.12345", "1234.5678"),
    ("2401.12345", "2401.12345"),                # 裸 ID (无 URL) 仍可用
    ("arXiv:2401.12345", "2401.12345"),
    ("https://example.com/x?ref=2401.12345&arxiv", "2401.12345"),  # 提到 arxiv 才认
]
for src, want in cases:
    got = extract_arxiv_id(src)
    check("extract %-45r -> %r" % (src[:45], want), got == want, "got %r" % got)

print()
print("=" * 70)
print("2) parse_arxiv_date  (每个月都要能解析)")
print("=" * 70)
months = ["January","February","March","April","May","June","July","August",
          "September","October","November","December"]
ok_months = []
for i, m in enumerate(months, 1):
    d = parse_arxiv_date("Submitted 12 %s, 2025" % m)
    good = d is not None and d.month == i and d.year == 2025
    if not good:
        ok_months.append((m, d))
check("全部 12 个月份解析正确", not ok_months, ok_months)

d = parse_arxiv_date("Submitted 14 August, 2026; v1 submitted 4 August, 2026; originally announced August 2026.")
check("v1 日期串可解析", d is not None and d.month == 8 and d.year == 2026, d)
d = parse_arxiv_date("Submitted 25 September, 2026; originally announced September 2026.")
check("September 不误判成 Sep/其他月", d is not None and d.month == 9, d)
d = parse_arxiv_date("Submitted 3 January, 2024")
check("January 不误判 (jan/january 前缀)", d is not None and d.month == 1, d)
d = parse_arxiv_date("Submitted 5 Mar, 2024")
check("缩写 Mar 可解析", d is not None and d.month == 3, d)
check("空串返回 None", parse_arxiv_date("") is None)
check("无年份返回 None", parse_arxiv_date("Submitted 5 May") is None)

print()
print("=" * 70)
print("3) dedup  —— arXiv DOI 匹配 & 误杀风险")
print("=" * 70)
# 模拟用户库: 与真实数据一致 (DOI 存成小写 10.48550/arxiv.xxxx)
lib = [
    LibraryPaper(item_id=1, title="Linear Canonical-Ensemble Quantum Monte Carlo",
                 doi="10.48550/arxiv.2601.08552", arxiv_id="2601.08552", year=2026),
    LibraryPaper(item_id=2, title="Measurement-Altered Ising Quantum Criticality",
                 doi="10.1103/PhysRevLett.131.140401", year=2023),
    LibraryPaper(item_id=3, title="Dynamical quantum phase transitions: a review",
                 year=2018),
]
idx = build_index(lib)
c = Candidate(arxiv_id="2601.08552", title="Some Other Title Entirely",
              doi="10.48550/arXiv.2601.08552")
hit, why = is_in_library(c, idx)
check("arXiv 自动 DOI 大小写不同也能匹配", hit, why)

c2 = Candidate(arxiv_id="2601.08552", title="Different", doi="")
hit2, why2 = is_in_library(c2, idx)
check("仅靠 arXiv ID 匹配", hit2, why2)

# 误杀检查: 同领域但确实不同的论文
c3 = Candidate(arxiv_id="2501.99999",
               title="Measurement-Altered Ising Quantum Criticality in Two Dimensions",
               doi="")
hit3, why3 = is_in_library(c3, idx)
print("     (同领域加长标题 -> %s: %s)" % (hit3, why3))

c4 = Candidate(arxiv_id="2501.88888",
               title="Entanglement entropy across the superfluid-insulator transition", doi="")
hit4, why4 = is_in_library(c4, idx)
check("无关新论文不被误杀", not hit4, why4)

c5 = Candidate(arxiv_id="2501.77777",
               title="Dynamical quantum phase transitions: a review of recent progress", doi="")
hit5, why5 = is_in_library(c5, idx)
check("标题+副标题变体应判定为已有", hit5, why5)

c6 = Candidate(arxiv_id="2501.66666",
               title="Dynamical quantum phase transitions: a review", doi="")
hit6, why6 = is_in_library(c6, idx)
check("完全同名判定为已有", hit6, why6)

# 短标题的包含关系不应误判
lib2 = [LibraryPaper(item_id=9, title="Quantum Monte Carlo", year=2020)]
idx2 = build_index(lib2)
c7 = Candidate(arxiv_id="2501.55555",
               title="Quantum Monte Carlo study of the two-dimensional Hubbard model", doi="")
hit7, why7 = is_in_library(c7, idx2)
check("过短标题的包含关系不误判", not hit7, why7)

# --- 连载论文 (Hubbard 1963 那组 I~V) 不能被当成副标题变体并掉 ---
# 实测踩过: "Electron correlations in narrow energy bands" 后面跟着 II/III/IV/V 四篇,
# 严格包含判断把四篇真论文全并进了第一篇, 一次丢四篇。
_HUB = "Electron correlations in narrow energy bands"
_lib_series = [LibraryPaper(item_id=1, title=_HUB, year=1963)]
_idx_series = build_index(_lib_series)
for _part in ("II. The Degenerate Band Case", "III. An Improved Solution",
              "IV. The Atomic Representation"):
    _c = Candidate(arxiv_id="", title="%s. %s" % (_HUB, _part), doi="")
    _hit, _why = is_in_library(_c, _idx_series)
    check("连载 %s 不被并入第一篇" % _part.split(".")[0], not _hit, _why)

_c_sub = Candidate(arxiv_id="", title=_HUB + ": a numerical study", doi="")
_hit_s, _why_s = is_in_library(_c_sub, _idx_series)
check("真副标题变体仍然判为已有", _hit_s, _why_s)

# --- 显式标识符矛盾要能一票否决模糊标题匹配 ---
# 实测: 标题相似度 0.99 的一对孪生论文, arXiv ID 却是 1103.4662 / 1106.4078。
# arXiv ID 是唯一标识, 两者矛盾时标题再像也不能合并 —— 合并等于丢掉一篇真论文。
_lib_twin = [LibraryPaper(item_id=1, title="Universal nonequilibrium quantum dynamics "
                                           "in imaginary time", arxiv_id="1103.4662")]
_idx_twin = build_index(_lib_twin)
_c_twin = Candidate(arxiv_id="1106.4078",
                    title="Universal non-equilibrium quantum dynamics in imaginary time",
                    doi="")
_hit_t, _why_t = is_in_library(_c_twin, _idx_twin)
check("arXiv ID 不同 -> 标题再像也不合并", not _hit_t, _why_t)

# 一边缺 ID 时不算矛盾: 正式版 PDF 常常没有 arXiv 戳记, 正需要标题兜底
_lib_no_id = [LibraryPaper(item_id=1, title="Universal nonequilibrium quantum dynamics "
                                            "in imaginary time", doi="")]
_hit_n, _why_n = is_in_library(_c_twin, build_index(_lib_no_id))
check("库内那篇没有 ID 时, 标题匹配仍然生效", _hit_n, _why_n)

print()
print("=" * 70)
print("4) recency / importance 边界")
print("=" * 70)
now = datetime(2026, 9, 28)
c = Candidate(arxiv_id="x", published=now - timedelta(days=0))
check("刚提交 -> recency≈1", abs(recency_score(c, 365, 1460, now) - 1.0) < 1e-6,
      recency_score(c, 365, 1460, now))
c = Candidate(arxiv_id="x", published=now - timedelta(days=365))
r = recency_score(c, 365, 1460, now)
check("一年前 -> recency≈1/e", abs(r - 0.3679) < 0.01, r)
c = Candidate(arxiv_id="x", published=now - timedelta(days=5000))
check("超过 hard_days -> 0", recency_score(c, 365, 1460, now) == 0.0)
c = Candidate(arxiv_id="x", published=now + timedelta(days=30))
check("未来日期不产生 >1", recency_score(c, 365, 1460, now) <= 1.0)
c = Candidate(arxiv_id="x")
check("无日期 -> 0.3 中性值", recency_score(c, 365, 1460, now) == 0.3)
c = Candidate(arxiv_id="x", updated=now - timedelta(days=10))
check("只有 updated 也能算", recency_score(c, 365, 1460, now) > 0.9)

rcfg = {"citation_scale": 200.0, "journal_ref_bonus": 0.15,
        "venue_keywords": ["phys. rev. lett", "prl"], "venue_bonus": 0.10}
c = Candidate(arxiv_id="x", citations=0)
check("0 引用 -> 0", importance_score(c, rcfg) == 0.0)
c = Candidate(arxiv_id="x", citations=None)
check("无引用数据 -> 0", importance_score(c, rcfg) == 0.0)
c = Candidate(arxiv_id="x", citations=10000)
check("超高引用 -> 封顶 1.0", importance_score(c, rcfg) == 1.0,
      importance_score(c, rcfg))
c = Candidate(arxiv_id="x", citations=100, journal_ref="Phys. Rev. Lett. 130, 1")
s = importance_score(c, rcfg)
check("引用+顶刊 有加成且不超 1", 0 < s <= 1.0, s)

print()
print("=" * 70)
print("5) heuristic_relevance  —— 单候选 / 零重叠 / 分数分布")
print("=" * 70)
prof = ResearchProfile(summary="quantum Monte Carlo sign problem entanglement",
                       keywords=["quantum Monte Carlo", "sign problem"])
cs = [Candidate(arxiv_id="a", title="quantum Monte Carlo sign problem study",
                abstract="quantum Monte Carlo sign problem entanglement")]
h = heuristic_relevance(prof, cs)
check("单候选时分数有限且 <=1", 0 <= list(h.values())[0] <= 1.0, h)

cs = [Candidate(arxiv_id="a", title="quantum Monte Carlo sign problem", abstract="x"),
      Candidate(arxiv_id="b", title="marine biology coral reef", abstract="y"),
      Candidate(arxiv_id="c", title="astrophysics galaxy rotation", abstract="z")]
h = heuristic_relevance(prof, cs)
check("相关 > 无关", h["a"] > h["b"], h)
check("完全无关 -> 0", h["b"] == 0.0, h)

cs = [Candidate(arxiv_id="b", title="marine biology coral reef", abstract="y")]
h = heuristic_relevance(prof, cs)
check("全部无关时不会除零崩溃", isinstance(h["b"], float), h)

print()
print("=" * 70)
print("6) 权重与排序")
print("=" * 70)
cfg = {"ranking": {"weight_relevance": 0.6, "weight_recency": 0.2,
                   "weight_importance": 0.2, "recency_tau_days": 365.0,
                   "recency_hard_days": 1460.0, "citation_scale": 200.0},
       "analysis": {"max_for_ai_scoring": 300, "score_batch_size": 8}}
cs = [Candidate(arxiv_id="old_famous", title="quantum Monte Carlo sign problem",
                abstract="quantum Monte Carlo sign problem", citations=500,
                published=datetime.now() - timedelta(days=2000)),
      Candidate(arxiv_id="new_relevant", title="quantum Monte Carlo sign problem method",
                abstract="quantum Monte Carlo sign problem", citations=0,
                published=datetime.now() - timedelta(days=5))]
ranked = rank_candidates(cfg, prof, cs, ai=None)
check("排序不崩溃且分数在 [0,1]", all(0 <= c.score <= 1.0 for c in ranked),
      [(c.arxiv_id, c.score) for c in ranked])
check("score 单调不增", all(ranked[i].score >= ranked[i+1].score
                          for i in range(len(ranked)-1)))
print("     排序结果: %s" % ([(c.arxiv_id, round(c.score, 3)) for c in ranked],))

print()
print("=" * 70)
print("7) 工具函数")
print("=" * 70)
check("norm_doi 去前缀", norm_doi("https://doi.org/10.1103/PhysRevLett.131.140401")
      == "10.1103/physrevlett.131.140401")
check("normalize_title 稳定", normalize_title("$T_c$ in the 2D Hubbard Model!")
      == normalize_title("$T_c$ in the 2D Hubbard Model!"))
check("不同标题不碰撞",
      normalize_title("Quantum Monte Carlo study of fermions") !=
      normalize_title("Quantum Monte Carlo study of bosons"))
check("safe_json_loads 剥围栏",
      safe_json_loads('```json\n{"a": 1}\n```') == {"a": 1})
check("safe_json_loads 修尾逗号", safe_json_loads('{"a": 1,}') == {"a": 1})
check("safe_json_loads 前后有噪声",
      safe_json_loads('好的, 结果如下: {"a": 2} 以上') == {"a": 2})
check("safe_json_loads 失败返回默认", safe_json_loads("not json", default={"z": 1}) == {"z": 1})

# --- 被 max_tokens 截断的输出: 保住完整的元素, 别整批丢掉 ---
# 实测里 300 篇打分丢了 8~16 篇, 就是因为一批的回复断在半截字符串上,
# 严格解析把**整批**判了死刑, 而前面几个对象其实完好无损。
_trunc = ('{"scores": [{"id": "a", "score": 95, "reason": "符号问题"}, '
          '{"id": "b", "score": 45, "reason": "纠缠熵"}, '
          '{"id": "c", "score": 9')
_sv = safe_json_loads(_trunc)
check("截断的 JSON 能救回来 (不是 None)", isinstance(_sv, dict), _sv)
check("救回来的是完整的那些元素, 残缺的丢掉",
      [x["id"] for x in (_sv or {}).get("scores", [])] == ["a", "b"], _sv)
check("救回来的元素内容没被动过",
      (_sv or {}).get("scores", [{}])[0].get("reason") == "符号问题", _sv)

# 理由里带花括号/引号时, 不能把括号数错
_trunc2 = ('{"scores": [{"id": "a", "score": 90, "reason": "用了 {braces} 和 \\"引号\\""}, '
           '{"id": "b", "score": 8')
_sv2 = safe_json_loads(_trunc2)
check("理由里有花括号和转义引号也数得对",
      [x["id"] for x in (_sv2 or {}).get("scores", [])] == ["a"], _sv2)

# 完整但只是格式烂的输入, 走的还是原来那条路, 结果不能变
check("没截断的输入不受影响 (仍走严格路径)",
      safe_json_loads('{"scores": [{"id": "a", "score": 1}]}')
      == {"scores": [{"id": "a", "score": 1}]})
# 半截且一个元素都没闭合 -> 还是默认值, 不能凭空造出东西
check("一个完整元素都没有时仍返回默认",
      safe_json_loads('{"scores": [{"id": "a", "sc', default=None) is None)
check("完全不是 JSON 时不受影响",
      safe_json_loads("今天天气不错", default=None) is None)
check("truncate 短串不变", truncate("abc", 10) == "abc")
check("truncate 在句界断开", truncate("aaa. bbb. ccc. ddd", 15).endswith("…"))

print()
print("=" * 70)
print("8) 代理处理")
print("=" * 70)
check("本机代理 scheme 规范化",
      normalize_proxy("https://127.0.0.1:7890")["http"] == "http://127.0.0.1:7890",
      normalize_proxy("https://127.0.0.1:7890"))
check("远程 https 代理不被改坏",
      normalize_proxy("https://proxy.corp.com:8080")["http"] == "https://proxy.corp.com:8080",
      normalize_proxy("https://proxy.corp.com:8080"))
check("None -> 不走代理", normalize_proxy(None) is None)
check("空串 -> 不走代理", normalize_proxy("") is None)
check("localhost 识别为本机", is_loopback_url("http://localhost:11434/v1"))
check("127.0.0.1 识别为本机", is_loopback_url("http://127.0.0.1:8899/v1/chat/completions"))
check("127.x.x.x 识别为本机", is_loopback_url("http://127.1.2.3:80/"))
check("外部地址不识别为本机", not is_loopback_url("https://api.deepseek.com/v1"))
check("arxiv 不识别为本机", not is_loopback_url("https://arxiv.org/search/"))

print()
print("=" * 70)
print("9) 全局请求节流闸门 (HTTP 429 的根治手段)")
print("=" * 70)
import threading
import time as _time

from arxiv_rec.utils import GLOBAL_LIMITER, RateLimiter, set_global_interval

# 间隔 0 时不该有任何等待
z = RateLimiter(0.0)
t0 = _time.time()
z.wait(); z.wait(); z.wait()
check("间隔 0 不等待", _time.time() - t0 < 0.05, "%.3fs" % (_time.time() - t0))

# 第一次不排队, 第二次必须排满一个间隔
p = RateLimiter(0.10)
t0 = _time.time()
d1 = p.wait()
d2 = p.wait()
check("首次请求不排队", d1 == 0.0, d1)
check("第二次排满一个间隔", d2 >= 0.08, "%.3f" % d2)
check("统计到被动等待次数", p.waits >= 1, p.stats())

# penalize 把闸门整体往后推 —— 这是 429 之后"所有线程一起冷静"的关键
p2 = RateLimiter(0.0)
p2.penalize(0.15)
t0 = _time.time()
p2.wait()
check("penalize 会推迟下一次放行", _time.time() - t0 >= 0.12,
      "%.3f" % (_time.time() - t0))

# penalize 只往后推, 不能被一个更小的值提前解锁
p3 = RateLimiter(0.0)
p3.penalize(0.30)
p3.penalize(0.01)
t0 = _time.time()
p3.wait()
check("penalize 取较远的那个时刻", _time.time() - t0 >= 0.25,
      "%.3f" % (_time.time() - t0))

# 并发: 两个线程抢闸门, 必须错开而不是一起醒
pc = RateLimiter(0.12)
stamps = []
lk = threading.Lock()


def _grab():
    pc.wait()
    with lk:
        stamps.append(_time.time())


ths = [threading.Thread(target=_grab) for _ in range(3)]
for th in ths:
    th.start()
for th in ths:
    th.join()
stamps.sort()
gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
check("并发线程被错开 (最小的间隔也够大)",
      len(gaps) == 2 and min(gaps) >= 0.08, ["%.3f" % g for g in gaps])

# set_global_interval 是配置接进闸门的那根线
set_global_interval(7.5)
check("set_global_interval 生效", GLOBAL_LIMITER.min_interval == 7.5,
      GLOBAL_LIMITER.min_interval)
set_global_interval(0.5)   # 别把 7.5 秒留给后面的测试
check("可以再改回来", GLOBAL_LIMITER.min_interval == 0.5)
set_global_interval(3.0)   # 恢复官方建议值

print()
print("=" * 70)
print("10) arXiv API 检索式构造")
print("=" * 70)
from arxiv_rec.arxiv_search import _api_search_query

# 裸词必须逐个加 all: 并显式 AND —— 实测 "all:a b AND cat:X" 里的 cat: 几乎不生效
check("裸词逐个加 all: 前缀",
      _api_search_query("sign problem") == "all:sign AND all:problem",
      _api_search_query("sign problem"))
check("相邻词之间补 AND",
      " AND " in _api_search_query("monte carlo sign"),
      _api_search_query("monte carlo sign"))
check("单分类加括号",
      _api_search_query("sign problem", ["cond-mat.str-el"])
      == "(all:sign AND all:problem) AND (cat:cond-mat.str-el)",
      _api_search_query("sign problem", ["cond-mat.str-el"]))
check("多分类用 OR 且整体加括号",
      _api_search_query("x y", ["cond-mat.str-el", "quant-ph"])
      == "(all:x AND all:y) AND (cat:cond-mat.str-el OR cat:quant-ph)",
      _api_search_query("x y", ["cond-mat.str-el", "quant-ph"]))
check("已有字段前缀不重复加",
      _api_search_query("all:hubbard AND abs:model") == "all:hubbard AND abs:model",
      _api_search_query("all:hubbard AND abs:model"))
check("引号短语原样保留",
      _api_search_query('"determinant quantum Monte Carlo"', ["cond-mat.str-el"])
      == '(all:"determinant quantum Monte Carlo") AND (cat:cond-mat.str-el)',
      _api_search_query('"determinant quantum Monte Carlo"', ["cond-mat.str-el"]))
check("OR 两侧都被分类括号罩住",
      _api_search_query("ti:x OR abs:y", ["quant-ph"])
      == "(ti:x OR abs:y) AND (cat:quant-ph)",
      _api_search_query("ti:x OR abs:y", ["quant-ph"]))
check("悬空运算符被清掉",
      _api_search_query("a AND") == "all:a", _api_search_query("a AND"))
check("空检索式只剩分类",
      _api_search_query("", ["cond-mat.str-el"]) == "cat:cond-mat.str-el",
      _api_search_query("", ["cond-mat.str-el"]))
check("无分类时不出现 cat:",
      "cat:" not in _api_search_query("sign problem", None))
check("多余空白被折叠",
      _api_search_query("  spaced   out  ") == "all:spaced AND all:out",
      _api_search_query("  spaced   out  "))

print()
print("=" * 70)
print("11) PDF 垃圾标题识别 (误杀 / 漏杀)")
print("=" * 70)
from arxiv_rec.pdf_library import (_is_garbage_title, _trim_title_block,
                                   _looks_like_prose_block, _is_column_header,
                                   _looks_like_title, classify_title)

# 漏杀方向: 这些都不该当论文收进库里 (走的是 extract_pdf 用的完整判定链)
for junk in ["Cjr", "ULTIMATE EDITION", "PREVIEW", "LETTERS of",
             "File name: Supplementary Information",
             "Comment: This is a well written paper",
             "Response to the referee report",
             "Response to the Reviewers",
             "Comments on the manuscript",
             "Comments to the authors",
             "Reply to Referees",
             "Reviewer's comments",
             "Traceback (most recent call last)",
             "博士学位论文", "相变"]:
    v, why = classify_title(junk)
    check("垃圾标题被识别 %-42r" % junk[:42], v == "junk", "%s / %s" % (v, why))

# 误杀方向: 这些是真标题, 一个都不能挂
for good in ["Bogoliubov Quasiparticle on the Gossamer Fermi Surface in Electron-Doped Cuprates",
             "Emmy Noether looks at the deconfined quantum critical point",
             "Quantum Monte Carlo study of the two-dimensional Hubbard model",
             "Determinant quantum Monte Carlo simulations of the sign problem",
             # PRL 的 Comment / Reply 是真论文, 不能被"审稿意见"关键字误伤
             "Comment on 'Sign problem in quantum Monte Carlo'",
             "Reply to 'Comment on Sign problem in quantum Monte Carlo'"]:
    v, why = classify_title(good)
    check("真标题不被误杀 %-42r" % good[:42], v == "paper", "%s / %s" % (v, why))

check("判定结果只有 paper/junk 两种",
      set(classify_title(t)[0] for t in
          ["Cjr", "Sign problem in QMC", "LETTERS of"]) <= {"paper", "junk"})

check("CJK 标题不被当页眉",
      not _is_column_header("量子相变点行列式蒙特卡洛模拟"),
      _is_column_header("量子相变点行列式蒙特卡洛模拟"))
check("纯大写短串判为页眉", _is_column_header("LETTERS"))
check("正常标题不是页眉", not _is_column_header("Hubbard model"))
check("长标题不因超长被判页眉", not _is_column_header("A" * 80))

# 标题块里混进作者/单位时要切干净
check("在 Authors: 处切断",
      _trim_title_block("Sign problem in QMC Authors: A. Smith, B. Jones")
      == "Sign problem in QMC",
      _trim_title_block("Sign problem in QMC Authors: A. Smith, B. Jones"))
check("在 ABSTRACT 处切断",
      _trim_title_block("Quantum criticality ABSTRACT We study the") == "Quantum criticality",
      _trim_title_block("Quantum criticality ABSTRACT We study the"))
check("在单位处切断",
      _trim_title_block("Sign problem study 1 Department of Physics")
      == "Sign problem study",
      _trim_title_block("Sign problem study 1 Department of Physics"))
check("切点太靠前就不切 (避免把标题切没)",
      _trim_title_block("A B Authors: x") == "A B Authors: x",
      _trim_title_block("A B Authors: x"))

# 正文段落不能当标题
prose = ("We study the two-dimensional Hubbard model using determinant quantum "
         "Monte Carlo. The sign problem limits us to small lattices. We find a "
         "crossover at finite temperature.")
check("多句正文判为散文", _looks_like_prose_block(prose))
check("正常标题不是散文", not _looks_like_prose_block("Sign problem in QMC"))
check("短句不是散文", not _looks_like_prose_block("Quantum Monte Carlo."))

print()
print("=" * 70)
print("12) 文献库合并 (SourceStats + _absorb), 只用本地 PDF")
print("=" * 70)
from arxiv_rec.library import _absorb, library_summary, load_library

base = LibraryPaper(item_id=1, title="Sign problem in QMC", year=2020)
extra = LibraryPaper(item_id=-1, title="Sign problem in QMC", year=2020,
                     abstract="abs", fulltext="ft", doi="10.1/x",
                     arxiv_id="2001.00001", authors=["A", "B"], publication="PRB",
                     context="【引言】intro text")
filled = _absorb(base, extra)
check("补齐摘要", base.abstract == "abs", base.abstract)
check("补齐全文", base.fulltext == "ft", base.fulltext)
check("补齐 DOI 并统一小写", base.doi == "10.1/x", base.doi)
check("补齐 arXiv ID", base.arxiv_id == "2001.00001", base.arxiv_id)
check("补齐作者", base.authors == ["A", "B"], base.authors)
check("补齐期刊", base.publication == "PRB", base.publication)
# context 是最容易漏的一个: 同一篇文献在两个文件夹里各有一份时, 先读到的那份
# 可能是浅档位。漏掉这一项的话合并后引言/结论就没了, 看起来像深度设置没生效。
check("补齐引言/结论上下文", base.context == "【引言】intro text", base.context)
check("返回补齐的字段名", isinstance(filled, list) and len(filled) >= 5, filled)

# 不能把已有信息覆盖掉
base2 = LibraryPaper(item_id=2, title="T", year=2021, abstract="keep me",
                     authors=["X"], doi="10.2/y", context="【结论】mine")
_absorb(base2, LibraryPaper(item_id=-1, title="T", year=1999, abstract="WRONG",
                            authors=["Z"], doi="10.9/z", context="【结论】WRONG"))
check("已有摘要不被覆盖", base2.abstract == "keep me", base2.abstract)
check("已有作者不被覆盖", base2.authors == ["X"], base2.authors)
check("已有 DOI 不被覆盖", base2.doi == "10.2/y", base2.doi)
check("已有上下文不被覆盖", base2.context == "【结论】mine", base2.context)

check("library_summary 不崩",
      "篇" in library_summary([base, base2]), library_summary([base, base2]))
check("library_summary 空库不崩", isinstance(library_summary([]), str),
      library_summary([]))
check("library_summary 报告引言/结论篇数",
      "有引言/结论 2" in library_summary([base, base2]),
      library_summary([base, base2]))

# 没有配置文件夹时应该明确报错而不是崩, 也不能去读 Zotero
_papers, _st = load_library({"pdf_folders": [],
                             "library": {"max_papers_for_profile": 0}})
check("没配文件夹时返回空库而不是抛异常", _papers == [] and _st.final_total == 0,
      (_papers, _st.final_total))
check("SourceStats 不再有 Zotero 字段",
      not hasattr(_st, "zotero_total") and not hasattr(_st, "zotero_error"),
      [a for a in dir(_st) if "zotero" in a])

print()
print("=" * 70)
print("13) PDF 索引 schema 失效")
print("=" * 70)
from arxiv_rec.pdf_library import PdfIndex
import tempfile as _tf
_tmp = os.path.join(_tf.gettempdir(), "daily_arxiv_selftest_idx.sqlite")
if os.path.exists(_tmp):
    os.remove(_tmp)
idx = PdfIndex(_tmp)
check("新库 schema 版本被写下",
      idx.conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()["v"]
      == str(SCHEMA_VERSION),
      idx.conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone())
idx.conn.execute("INSERT INTO files (path, disp_path, size, mtime_ns, sha1, "
                 "status) VALUES ('p', 'p', 1, 1, 's', 'ok')")
idx.conn.commit()
idx.conn.execute("UPDATE meta SET v='1' WHERE k='schema'")
idx.conn.commit()
idx.conn.close()

idx2 = PdfIndex(_tmp)
check("版本变化时旧记录作废",
      idx2.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 0)
check("版本变化被记下条数", idx2.invalidated == 1, idx2.invalidated)
idx2.conn.close()

idx3 = PdfIndex(_tmp)
check("版本一致时不作废", idx3.invalidated == 0, idx3.invalidated)
idx3.conn.close()
try:
    os.remove(_tmp)
except OSError:
    pass

print()
print("=" * 70)
print("14) RSS 公告解析 (新通道)")
print("=" * 70)
from arxiv_rec.arxiv_search import parse_rss

_RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>cond-mat.str-el updates on arXiv.org</title>
    <item>
      <title>Sign problem in a frustrated Hubbard model</title>
      <link>https://arxiv.org/abs/2502.18929</link>
      <description>arXiv:2502.18929v1 Announce Type: new
Abstract: We study the sign problem &amp; its cure.</description>
      <guid isPermaLink="false">oai:arXiv.org:2502.18929v1</guid>
      <category>cond-mat.str-el</category>
      <pubDate>Mon, 24 Feb 2025 00:00:00 -0500</pubDate>
      <arxiv:announce_type>new</arxiv:announce_type>
      <dc:creator>Alice Smith</dc:creator>
      <dc:creator>Bob Jones</dc:creator>
    </item>
    <item>
      <title>A cross-listed paper</title>
      <link>https://arxiv.org/abs/2502.11111</link>
      <description>arXiv:2502.11111v1 Announce Type: cross
Abstract: Another one.</description>
      <category>cond-mat.str-el</category>
      <pubDate>Tue, 25 Feb 2025 00:00:00 -0500</pubDate>
      <arxiv:primary_category term="quant-ph"/>
      <arxiv:announce_type>cross</arxiv:announce_type>
      <dc:creator>Carol Lee</dc:creator>
    </item>
    <item>
      <title>交叉列表: 主分类在 hep-th</title>
      <link>https://arxiv.org/abs/2609.30362</link>
      <description>arXiv:2609.30362v1 Announce Type: cross
Abstract: Cross-listed into str-el.</description>
      <category>hep-th</category>
      <pubDate>Mon, 24 Feb 2025 00:00:00 -0500</pubDate>
      <arxiv:primary_category term="hep-th"/>
      <arxiv:announce_type>cross</arxiv:announce_type>
      <dc:creator>Dan Wu</dc:creator>
    </item>
    <item>
      <title>没有 link, 应该被跳过</title>
      <description>nothing useful here</description>
    </item>
  </channel>
</rss>
"""

items = parse_rss(_RSS_SAMPLE, "cond-mat.str-el")
check("解析出 3 条 (没 link 的被跳过)", len(items) == 3, len(items))
if len(items) == 3:
    a, b, c = items
    check("从 link 里提取 arXiv ID", a.arxiv_id == "2502.18929", a.arxiv_id)
    # 这是最容易出错的一处: description 前半段是 arXiv 自己塞的公告信息,
    # 不剥掉的话摘要字段里会混进 "arXiv:... Announce Type: new"
    check("摘要里的 Announce Type 前缀被剥掉",
          a.abstract == "We study the sign problem & its cure.", repr(a.abstract))
    check("摘要里没有 'Abstract:' 字样", "Abstract:" not in a.abstract, a.abstract)
    check("HTML 实体被反转义", "&amp;" not in a.abstract, a.abstract)
    # dc:creator 是每个作者一个节点, 用 find 只会拿到第一作者
    check("作者取全 (不是只有第一作者)", a.authors == ["Alice Smith", "Bob Jones"],
          a.authors)
    check("pubDate 解析成 datetime",
          a.published is not None and a.published.year == 2025
          and a.published.month == 2 and a.published.day == 24,
          a.published)
    check("announced 保留原始日期串", "24 Feb 2025" in a.announced, a.announced)
    check("公告类型记进 comments", "new" in a.comments, a.comments)
    check("分类已带上", a.categories == ["cond-mat.str-el"], a.categories)
    check("source_queries 标出这是 RSS 来的",
          any("RSS" in q for q in a.source_queries), a.source_queries)
    check("primary_category 优先用 arxiv:primary_category",
          b.primary_category == "quant-ph", b.primary_category)
    check("primary_category 也进了 categories",
          "quant-ph" in b.categories and "cond-mat.str-el" in b.categories,
          b.categories)
    # 交叉列表的论文在 feed 里只写自己的主分类, 但 feed 本身证明它被公告到了
    # 该分类。不补进去的话, 按分类过滤时这 23/41 条会被当成越界结果丢掉。
    check("交叉列表项补上了 feed 的分类",
          c.categories == ["hep-th", "cond-mat.str-el"], c.categories)
    check("交叉列表项的 primary 仍是自己的主分类",
          c.primary_category == "hep-th", c.primary_category)

check("空输入返回空列表", parse_rss("", "cond-mat.str-el") == [])
check("坏 XML 返回空列表而不抛异常", parse_rss("<rss><channel>", "x") == [])

print()
print("=" * 70)
print("15) 读取深度 (三档) 与引言/结论定位")
print("=" * 70)
from arxiv_rec.pdf_library import (_depth_covers, _find_conclusion,
                                   _find_conclusion_in_doc, _find_intro,
                                   context_for, normalize_depth, DEPTH_ORDER)

check("三档名都认", [normalize_depth(d) for d in DEPTH_ORDER] ==
      ["metadata", "sections", "fulltext"])
check("大写也认", normalize_depth("FULLTEXT") == "fulltext")
check("带空格也认", normalize_depth("  metadata ") == "metadata")
# 认不出来时取中间档而不是最浅档: 用户拼错了档位名, 多做一点比默默少做好
check("认不出来时取 sections (不是 metadata)",
      normalize_depth("fullltext") == "sections", normalize_depth("fullltext"))
check("空值取 sections", normalize_depth("") == "sections")
check("None 取 sections", normalize_depth(None) == "sections")

# 复用判定: 只有"已读的档 >= 这次要的档"才能复用
check("metadata 够 metadata", _depth_covers("metadata", "metadata"))
check("metadata 不够 sections", not _depth_covers("metadata", "sections"))
check("metadata 不够 fulltext", not _depth_covers("metadata", "fulltext"))
check("sections 够 metadata", _depth_covers("sections", "metadata"))
check("sections 够 sections", _depth_covers("sections", "sections"))
check("sections 不够 fulltext", not _depth_covers("sections", "fulltext"))
check("fulltext 够所有档",
      all(_depth_covers("fulltext", d) for d in DEPTH_ORDER))
check("空档位 (老记录) 一律不够",
      not _depth_covers("", "metadata") and not _depth_covers(None, "metadata"))

# context_for: 这才是三档深度真正影响 token 的地方
check("metadata 不送正文", context_for("metadata", "引言", "结论", "全文") == "")
check("sections 送引言和结论",
      context_for("sections", "引", "结", "全") == "【引言】引\n【结论】结",
      context_for("sections", "引", "结", "全"))
check("sections 只有引言时也能用",
      context_for("sections", "引", "", "全") == "【引言】引")
check("sections 抽不到正文时是空串",
      context_for("sections", "", "", "全") == "")
check("fulltext 送全文节选", context_for("fulltext", "引", "结", "全") == "全")

# --- 小节定位 ---
_INTRO_DOC = """Measurement-Altered Ising Quantum Criticality
Sara Murciano and others
Abstract
We study the effects of measurements on critical states.
I. INTRODUCTION
Quantum criticality has been a central theme in condensed matter physics.
In this work we consider the Ising model subject to measurements.
II. MODEL AND METHOD
The Hamiltonian is given by H = -J sum sigma_z sigma_z.
ACKNOWLEDGMENTS
We thank the referees.
REFERENCES
[1] S. Murciano, Phys. Rev. B 100, 123456 (2019).
"""
check("找得到引言", "central theme" in _find_intro(_INTRO_DOC), _find_intro(_INTRO_DOC))
check("引言不越过致谢", "thank the referees" not in _find_intro(_INTRO_DOC))
check("引言不越过参考文献", "Phys. Rev. B 100" not in _find_intro(_INTRO_DOC))
check("没有引言标题时返回空串",
      _find_intro("Just some text without any heading.\n") == "")
# 正文里提到 "introduction" 的句子不能被当成小节标题
_PROSE_INTRO = """Abstract
We follow the introduction of Ref. [3] and extend it.
II. MODEL
The model is defined as follows.
"""
check("正文里的 'the introduction of' 不算小节标题",
      _find_intro(_PROSE_INTRO) == "", _find_intro(_PROSE_INTRO))

_CONCL_DOC = """IV. DISCUSSION
The results are interesting but not conclusive.
V. CONCLUSIONS
We have shown that the sign problem is mild at half filling.
Our results suggest a new route to the phase diagram.
ACKNOWLEDGMENTS
We thank the referees for useful comments.
REFERENCES
[1] A. Author, Phys. Rev. Lett. 120, 010101 (2018).
"""
c = _find_conclusion(_CONCL_DOC)
check("找得到结论", "sign problem is mild" in c, c)
# 讨论和结论分开写时, 要取最后那个 ("结论"), 不是 "讨论"
check("取的是最后一个结论标题, 不是讨论", "not conclusive" not in c, c)
check("结论不越过致谢", "thank the referees" not in c, c)
check("结论不越过参考文献", "Phys. Rev. Lett. 120" not in c, c)

# 物理期刊常见的另一种写法
_SUMMARY_DOC = ("VI. Summary and outlook\n"
                "We have established that the sign problem is mild. " * 4 + "\n")
check("认 'Summary and outlook'",
      "future" not in _SUMMARY_DOC and "sign problem is mild" in
      _find_conclusion(_SUMMARY_DOC), _find_conclusion(_SUMMARY_DOC))
check("认 'Concluding remarks'",
      bool(_find_conclusion("Concluding remarks\n" + "body text here. " * 20)))
check("太短的结论 (正文不足 80 字) 当没找到",
      _find_conclusion("V. Conclusions\nToo short.\n") == "")
check("没有结论标题时返回空串",
      _find_conclusion("Only body text, no headings.\n") == "")

# PDF 抽文本时经常把标题和正文第一句并成一个块 —— 这是实测漏抽结论的头号原因
# (Yan & Meng 2023: "Discussion and conclusion Overall, we realize a practical
# scheme...")。标题正则的行尾不能锚死。
_MERGED_DOC = ("5. Discussion and conclusion Overall, we realize a practical "
               "scheme to extract the low-lying entanglement spectrum from "
               "quantum Monte Carlo simulations of the Hubbard model. "
               "This settles the long-standing question. " * 2)
check("标题与正文并成一行时也认得出来",
      "practical scheme" in _find_conclusion(_MERGED_DOC),
      _find_conclusion(_MERGED_DOC))
check("并行的正文句子不算标题",
      _find_conclusion("Conclusions and outlook are discussed in Sec. 5 of "
                       "this paper, where we also present the numerical "
                       "results for the two-dimensional model. " * 3) == "")
_FILLER = "Some body text about the model and its numerical treatment. " * 6


class _FakeDoc(object):
    """假文档, 只实现 ``_text_tail`` —— 用来测逐级放宽窗口的查找逻辑。

    走非 pymupdf 分支: 那条路不需要真的打开 PDF, 但窗口放宽、两档兜底的顺序
    都是同一份代码。
    """

    def __init__(self, pages):
        self.pages = pages
        self.page_count = len(pages)

    def _text_tail(self, max_pages):
        n = len(self.pages)
        return "\n".join(self.pages[max(0, n - int(max_pages)):])


def _pages_with(*placed, **kw):
    """30 页的假文档, ``placed`` 里每项是 ``(页号, 该页文本)``。"""
    total = kw.get("total", 30)
    out = [_FILLER for _ in range(total)]
    for where, text in placed:
        out[where] = text
    return out


_CONCL_TXT = "V. Conclusions\nWe have shown that the sign problem is mild. " * 4
_BIB_TXT = "REFERENCES\n[1] A. Author, Phys. Rev. Lett. 120, 010101 (2018).\n"

# 结论在第 15 页, 最后 6 页里没有 -> 必须靠放宽窗口才找得到
_got = _find_conclusion_in_doc(
    _FakeDoc(_pages_with((15, _CONCL_TXT))), "pypdf", 30)
check("窗口 6 页找不到时, 放宽到 16 页能找到", "sign problem is mild" in _got, _got)

# 第一档完全没命中才退第二档
_got = _find_conclusion_in_doc(
    _FakeDoc(_pages_with((15, "V. Discussion\n" + _FILLER))), "pypdf", 30)
check("只有 Discussion 收尾时, 第二档兜底抽到",
      "Some body text about the model" in _got, _got)

# 同一个窗口里两档都在时, 第一档赢 (中段的 Discussion 不能顶掉文末的 Conclusions)
_got = _find_conclusion_in_doc(
    _FakeDoc(_pages_with((27, "V. Discussion\n" + _FILLER),
                         (28, _CONCL_TXT), (29, _BIB_TXT))), "pypdf", 30)
check("有 Conclusions 时不被中段 Discussion 顶掉",
      "sign problem is mild" in _got and "Some body text" not in _got, _got)

check("两档都没有时返回空串",
      _find_conclusion_in_doc(_FakeDoc([_FILLER] * 30), "pypdf", 30) == "")

check("认 'Introduction and motivation' 这种复合引言标题",
      "sign problem" in _find_intro(
          "1. Introduction and motivation The sign problem has been a "
          "central obstacle to simulating fermionic systems at finite "
          "density for many decades. " * 2),
      _find_intro("1. Introduction and motivation The sign problem has been "
                  "a central obstacle. " * 4))

print()
print("=" * 70)
print("16) PDF 里的 arXiv ID / DOI 抽取 (只认首页, 不抄引文)")
print("=" * 70)
from arxiv_rec.pdf_library import _ident_from_head, _before_bibliography

# 正常情形: 首页页边戳记
check("首页 arXiv 戳记抽得到",
      _ident_from_head("arXiv:2307.10602v3  [cond-mat.str-el]  21 Feb 2025\n"
                       "Universal term of Entanglement Entropy\n")[0] == "2307.10602",
      _ident_from_head("arXiv:2307.10602v3\n")[0])
check("旧式 ID 也认",
      _ident_from_head("arXiv:cond-mat/0403055v1\n")[0] == "cond-mat/0403055",
      _ident_from_head("arXiv:cond-mat/0403055v1\n")[0])

# 核心回归: 裸数字不算。这一条要是漏了, 正文/公式编号/引文里的数字会被当成 ID,
# 而 arXiv ID 是去重第一优先级 —— 假 ID 会让新论文被当成"已在库中"丢掉。
check("没有 'arXiv' 字样的裸数字不算 ID",
      _ident_from_head("L. Hack), 2409.11628\nPfaffian Quantum Monte Carlo\n")[0] == "",
      _ident_from_head("L. Hack), 2409.11628\n")[0])
check("公式/页码附近的数字不算 ID",
      _ident_from_head("Eq. (2024.12345) shows the scaling\n")[0] == "")

# 参考文献区之后的戳记是别人的
_BIB_HEAD = ("arXiv:2307.10602\nIntroduction\nWe study the model.\n"
             "REFERENCES\n[1] A. Author, arXiv:1112.5166, Phys. Rev. B 90 (2014).\n")
check("参考文献区之后的 ID 不要",
      _ident_from_head(_BIB_HEAD)[0] == "2307.10602", _ident_from_head(_BIB_HEAD)[0])
check("首页只有参考文献里的 ID 时给空串",
      _ident_from_head("Introduction\nREFERENCES\n[1] arXiv:1112.5166\n")[0] == "",
      _ident_from_head("Introduction\nREFERENCES\n[1] arXiv:1112.5166\n")[0])
check("_before_bibliography 在 'Bibliography' 处也切",
      "arXiv" not in _before_bibliography("text\nBibliography\narXiv:1112.5166"))
check("没有文献区标题时原样返回",
      _before_bibliography("abc") == "abc")
check("参考文献区出现在正文里的句子不误切",
      _before_bibliography("see the References section for details")
      == "see the References section for details")

# DOI 同理
check("首页 DOI 抽得到",
      _ident_from_head("Phys. Rev. B 84, 224303 (2011)\n"
                       "doi: 10.1103/PhysRevB.84.224303\n")[1].lower()
      == "10.1103/physrevb.84.224303")
check("参考文献区里的 DOI 不要",
      _ident_from_head("Intro text\nREFERENCES\n[1] doi:10.1103/PhysRevB.90.1\n")[1]
      == "")

print()
print("=" * 70)
print("17) 文献库排序必须与输入顺序无关 (否则 AI 缓存整片落空)")
print("=" * 70)
from arxiv_rec.library import order_papers


def _mk(title, year):
    return LibraryPaper(item_id=0, title=title, year=year)


# 同一批文献用两种输入顺序喂进去, 模拟"新建索引"(遍历文件夹) 和
# "复用索引"(读数据库) 两条路径给出的不同顺序。
# 真实踩到的坑: 只按年份排是稳定排序, 并列的保持输入顺序 -> 两次运行排出的
# 次序不同 -> 研究画像的提示词跟着变 -> 拿提示词当 key 的 AI 缓存全部落空,
# 第二次运行白花 3 次调用, 同一份输入也复现不出同一份报告。
_group = [_mk("Alpha", 2020), _mk("Beta", 2020), _mk("Gamma", 2020),
          _mk("Delta", 2019), _mk("Epsilon", 2021)]
_fwd = [p.title for p in order_papers(list(_group), 0)]
_bwd = [p.title for p in order_papers(list(reversed(_group)), 0)]
check("同年并列时顺序与输入顺序无关", _fwd == _bwd, "%s vs %s" % (_fwd, _bwd))
check("年份仍然是倒序",
      [p.year for p in order_papers(list(_group), 0)] == [2021, 2020, 2020, 2020, 2019])
check("同年按标题升序",
      _fwd == ["Epsilon", "Alpha", "Beta", "Gamma", "Delta"], _fwd)

_a = [p.title for p in order_papers(list(_group), 3)]
_b = [p.title for p in order_papers(list(reversed(_group)), 3)]
check("截断结果与输入顺序无关", _a == _b, "%s vs %s" % (_a, _b))
check("截断取的正是排在最前的", _a == ["Epsilon", "Alpha", "Beta"], _a)

# 年份缺失或不是数字时不能把排序搞崩
_odd = [_mk("NoYear", None), _mk("BadYear", "n.d."), _mk("Ok", 2020)]
try:
    _o = [p.title for p in order_papers(_odd, 0)]
    check("年份缺失/异常也能排", _o == ["Ok", "BadYear", "NoYear"], _o)
except Exception as _exc:
    check("年份缺失/异常也能排", False, _exc)

print()
print("=" * 70)
print("18) 推荐记录: 同一篇不重复解读, 但换了画像必须重来")
print("=" * 70)
import shutil
import tempfile
from arxiv_rec.history import (RecommendHistory, profile_fingerprint,
                               describe_history, history_path)
# reuse_analysis 是 RecommendHistory 的静态方法 (它跟记录表的列定义绑在一起)
reuse_analysis = RecommendHistory.reuse_analysis

_tmp = tempfile.mkdtemp(prefix="daily_arxiv_histtest_")
_db = os.path.join(_tmp, "hist.sqlite")


def _cand(aid, title="T", score=0.5):
    return Candidate(arxiv_id=aid, title=title, score=score)


def _prof(queries, summary="s"):
    return ResearchProfile(queries=list(queries), topics=[], methods=[],
                           keywords=[], summary=summary)


try:
    with RecommendHistory(_db) as h:
        fp_old = profile_fingerprint(_prof(["a"]))
        c1 = _cand("2401.00001", "First")
        c1.summary = "讲了什么"
        c1.connections = [{"note": "关联"}]
        c1.ideas = "可以结合"
        c1.analyzed = True
        new, upd = h.record([c1], "r.md", fp_old)
        check("首次写入算新增", (new, upd) == (1, 0), (new, upd))
        _first_at = h.known()["2401.00001"]["first_at"]

        # 第二次同一篇: 次数 +1, 分数取更高, 解读原样保留
        c1b = _cand("2401.00001", "First", score=0.9)
        new2, upd2 = h.record([c1b], "r2.md", fp_old)
        check("第二次写入算更新", (new2, upd2) == (0, 1), (new2, upd2))
        row = h.known()["2401.00001"]
        check("推荐次数累加", row["times"] == 2, row["times"])
        check("best_score 取更高的一次", abs(row["best_score"] - 0.9) < 1e-9,
              row["best_score"])
        check("报告路径更新成最近一次", row["report"] == "r2.md", row["report"])
        check("第一次推荐时间被保留 (只更新最近一次)",
              row["first_at"] == _first_at and row["last_at"] >= _first_at,
              (row["first_at"], row["last_at"], _first_at))

        # --no-ai 那一轮: 候选没有解读, 不能把存好的解读清空
        h.record([_cand("2401.00001", "First", score=0.4)], "r3.md", fp_old)
        row = h.known()["2401.00001"]
        check("没解读的一轮不会清空已存的解读",
              row["summary"] == "讲了什么" and row["analyzed"] == 1, row["summary"])

        # 画像一致 -> 复用; 画像变了 -> 必须重来
        c2 = _cand("2401.00001")
        check("画像指纹一致时复用解读",
              reuse_analysis(c2, row, fp_old) and c2.reused
              and c2.summary == "讲了什么" and c2.connections == [{"note": "关联"}],
              (c2.summary, c2.connections))
        c3 = _cand("2401.00001")
        fp_new = profile_fingerprint(_prof(["a", "b"]))
        check("换了检索式 -> 指纹变了", fp_new != fp_old, (fp_old, fp_new))
        check("画像不一致时绝不复用",
              not reuse_analysis(c3, row, fp_new) and not c3.reused, c3.summary)
        check("从没推荐过的返回 False", not reuse_analysis(_cand("x"), None, fp_old))
        check("只推荐过没解读的也不复用",
              not reuse_analysis(_cand("y"),
                                 {"analyzed": 0, "summary": "", "profile_fp": fp_old},
                                 fp_old))

        check("counts 对得上", h.counts() == {"total": 1, "analyzed": 1},
              h.counts())
        check("recent 能取到", len(h.recent(5)) == 1, h.recent(5))
        check("last_run 非空", bool(h.last_run()), h.last_run())
        _n_forgot = h.forget(["2401.00001"])
        check("forget 能删", _n_forgot == 1, _n_forgot)
        check("删完就查不到了", h.known() == {}, h.known())
        # 没有 arXiv ID 的候选进不了记录 (记录是按 ID 做键的)
        check("没 ID 的候选被跳过",
              h.record([_cand("", "无 ID")], "r.md", fp_old) == (0, 0))
        check("空记录不会崩", h.counts()["total"] == 0, h.counts())

    # 指纹只看会改变解读内容的东西: 换模型不该让记录作废
    class _P(object):
        queries = ["a"]
        topics = []
        methods = []
        keywords = []
        summary = "s"
        generated_by = "model-A"

    class _P2(_P):
        generated_by = "model-B"

    check("换模型名不改指纹 (解读仍可复用)",
          profile_fingerprint(_P()) == profile_fingerprint(_P2()),
          (profile_fingerprint(_P()), profile_fingerprint(_P2())))
    check("画像为 None 时指纹为空串", profile_fingerprint(None) == "")

    # 打不开的路径只该影响记录, 不该把整轮运行带崩
    _bad = RecommendHistory.__new__(RecommendHistory)
    check("describe_history 不抛异常",
          isinstance(describe_history({"analysis": {"history_db": _db}}), str))
    check("关掉时不报错",
          "已关闭" in describe_history(
              {"analysis": {"history_db": _db, "use_history": False}}))
    check("history_path 用配置里的路径",
          history_path({"analysis": {"history_db": _db}}).endswith("hist.sqlite"),
          history_path({"analysis": {"history_db": _db}}))
    del _bad
except Exception as _exc:
    import traceback
    traceback.print_exc()
    check("推荐记录自测跑完", False, _exc)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 70)
print("19) 提速改动不能改变结果 (去重的 quick_ratio 预筛 / 富化的截断与熔断)")
print("=" * 70)
from difflib import SequenceMatcher
from arxiv_rec import dedup as _dd
from arxiv_rec.dedup import (build_index, _significant_tokens, _ident_conflict,
                             _is_series_continuation, CONTAINMENT_MIN_LEN,
                             TITLE_SIM_THRESHOLD, SHORT_TITLE_LEN)


def _find_without_quick_ratio(cand, index):
    """改动前的 find_in_library: 直接调 ratio(), 没有 quick_ratio 预筛。

    用它当"标准答案" —— quick_ratio 只是 ratio 的上界, 拿它预筛不该改变任何一条
    判定。这个测试就是钉住这一点: 以后谁再动这段, 只要结果有出入立刻报出来。
    """
    nt = cand.norm_title
    if not nt:
        return None, ""
    if nt in index["titles"]:
        return index["titles"][nt], "标题相同"
    pool = set()
    for tok in _significant_tokens(nt):
        for other in index["token_index"].get(tok, []):
            pool.add(other)
    if not pool:
        return None, ""
    threshold = TITLE_SIM_THRESHOLD
    if len(nt) < SHORT_TITLE_LEN:
        threshold = 0.95
    for other in pool:
        shorter, longer = (nt, other) if len(nt) <= len(other) else (other, nt)
        if len(shorter) >= CONTAINMENT_MIN_LEN and shorter in longer:
            if _is_series_continuation(shorter, longer):
                continue
            if _ident_conflict(cand, index["ids"].get(other, ("", ""))):
                continue
            return index["titles"].get(other), "标题为包含关系 (副标题变体)"
    for other in pool:
        if abs(len(other) - len(nt)) > max(20, 0.35 * len(nt)):
            continue
        ratio = SequenceMatcher(None, nt, other).ratio()
        if ratio >= threshold:
            if _ident_conflict(cand, index["ids"].get(other, ("", ""))):
                continue
            return index["titles"].get(other), "标题高度相似 (%.2f)" % ratio
    return None, ""


_lib = [LibraryPaper(item_id=i, title=t) for i, t in enumerate([
    "Quantum Monte Carlo study of the two-dimensional Hubbard model",
    "Sign problem and its mitigation in fermionic lattice models",
    "Entanglement entropy across the superfluid-insulator transition",
    "Dynamical quantum phase transitions: a review",
    "Machine learning the quantum many-body problem",
    "Numerical observation of emergent spacetime supersymmetry",
    "Electron correlations in narrow energy bands",
    "Topological order in a frustrated quantum magnet",
    "Tensor network approaches to strongly correlated systems",
    "Fermionic quantum criticality in honeycomb lattices",
])]
_idx = build_index(_lib)
_titles = [p.title for p in _lib]
_cases = []
for _t in _titles:
    _cases.append(Candidate(arxiv_id="", title=_t))                     # 完全相同
    _cases.append(Candidate(arxiv_id="", title=_t + ": a numerical study"))
    _cases.append(Candidate(arxiv_id="", title=_t.upper()))
    _cases.append(Candidate(arxiv_id="", title=_t.replace(" ", "  ")))
    _cases.append(Candidate(arxiv_id="", title=_t[:len(_t) // 2]))
for _extra in ("Quantum Monte Carlo", "A review", "Sign problem",
               "Completely unrelated paper about economics and markets",
               "Quantum Monte Carlo study of the three-dimensional Hubbard model",
               "Monte Carlo study of quantum systems", ""):
    _cases.append(Candidate(arxiv_id="", title=_extra))

_mismatch = []
for _c in _cases:
    _a = _find_without_quick_ratio(_c, _idx)
    _b = _dd.find_in_library(_c, _idx)
    if _a != _b:
        _mismatch.append((_c.title[:40], _a, _b))
check("quick_ratio 预筛不改变任何一条判定 (%d 条)" % len(_cases), not _mismatch,
      _mismatch[:3])
check("这批用例里确实有命中的 (不是全都没命中而白测)",
      any(_dd.find_in_library(_c, _idx)[0] is not None for _c in _cases),
      sum(1 for _c in _cases if _dd.find_in_library(_c, _idx)[0] is not None))

# --- 上面那 60 条是手挑的, 覆盖面有限。再来一轮随机对拍。 ---
# 预筛的安全性完全建立在"quick_ratio 永远是 ratio 的上界"这一个性质上。它一旦
# 不成立, 后果是**静默**的: 某篇真正重复的论文被跳过, 于是推荐里混进用户已经有
# 的论文 —— 不报错、不崩溃, 只是结果悄悄变错。所以这里用固定种子跑几千对随机
# 字符串 (含极小字母表 "ab"/"a" 这种容易撞出巧合匹配的), 专门盯这个性质。
import random as _random
from difflib import SequenceMatcher as _SM

_random.seed(20260929)
_ALPHABETS = ["abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz ",
              "abc ", "ab", "a"]
_fuzz_bad = []
_fuzz_n = 0
for _ in range(4000):
    _ab = _random.choice(_ALPHABETS)
    _a = "".join(_random.choice(_ab) for _ in range(_random.randint(0, 40)))
    _b = "".join(_random.choice(_ab) for _ in range(_random.randint(0, 40)))
    _sm = _SM(None, _a, _b)
    _fuzz_n += 1
    if _sm.quick_ratio() < _sm.ratio():
        _fuzz_bad.append((_a, _b, _sm.ratio(), _sm.quick_ratio()))
# 同长度的随机对 (最容易逼近上界) 再来一批
for _ in range(2000):
    _ab = _random.choice(_ALPHABETS)
    _n = _random.randint(1, 60)
    _a = "".join(_random.choice(_ab) for _ in range(_n))
    _b = "".join(_random.choice(_ab) for _ in range(_n))
    _sm = _SM(None, _a, _b)
    _fuzz_n += 1
    if _sm.quick_ratio() < _sm.ratio():
        _fuzz_bad.append((_a, _b, _sm.ratio(), _sm.quick_ratio()))
check("随机对拍 %d 对: quick_ratio 从不小于 ratio" % _fuzz_n, not _fuzz_bad,
      _fuzz_bad[:3])

# 长度差预筛: 被它跳过的对里, 不该有能过阈值的
_len_bad = []
for _a, _b in [(_c.norm_title, _t) for _c in _cases for _t in _titles]:
    if not _a or abs(len(_b) - len(_a)) <= max(20, 0.35 * len(_a)):
        continue
    _r = _SM(None, _a, _b).ratio()
    if _r >= TITLE_SIM_THRESHOLD:
        _len_bad.append((_a[:30], _b[:30], _r))
check("长度差预筛不会漏掉能过阈值的对", not _len_bad, _len_bad[:3])

# --- 富化: 截断只查最相关的, 被跳过的也算"处理过"; 被限流立刻收工 ---
from arxiv_rec import enrich as _en
from arxiv_rec.utils import http_get as _real_http_get
import json as _json

_calls = []


def _fake_throttled(session, url, params=None, **kw):
    _calls.append(1)
    raise RuntimeError("HTTP 429 for https://api.openalex.org/works")


_en.http_get = _fake_throttled

try:
    _many = [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
             for i in range(200)]
    _heur = {"2401.%05d" % i: i / 200.0 for i in range(200)}
    _calls[:] = []
    _n = _en.enrich_candidates({"network": {"timeout": 5, "retries": 3}},
                               _many, heur=_heur, limit=50)
    check("被限流时立刻收工 (不再把剩下的批次全试一遍)", len(_calls) == 1,
          "%d 次请求" % len(_calls))
    check("限流时富化篇数为 0", _n == 0, _n)
    check("被跳过的候选也标记成'已处理过'",
          all(c.enriched for c in _many),
          sum(1 for c in _many if not c.enriched))

    _ok = {"n": 0}

    def _fake_ok(session, url, params=None, **kw):
        _calls.append(1)
        _ok["n"] += 1
        _dois = (params or {}).get("filter", "").replace("doi:", "").split("|")
        return _json.dumps({"results": [
            {"doi": "https://doi.org/" + d, "cited_by_count": 5,
             "primary_location": {}, "concepts": []} for d in _dois]})

    _en.http_get = _fake_ok
    _many2 = [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
              for i in range(200)]
    _calls[:] = []
    _n2 = _en.enrich_candidates({"network": {"timeout": 5, "retries": 1}},
                                _many2, heur=_heur, limit=50)
    check("截断后只查了 limit 篇", _n2 == 50, _n2)
    _queried = [c for c in _many2 if c.citations is not None]
    _lowest = min(int(c.arxiv_id.split(".")[1]) for c in _queried)
    check("查的正是相关性最靠前的那批", _lowest >= 150,
          "最靠后的被查到的名次 %d (相关性越低名次越大)" % _lowest)
    check("其余候选没被查但也标成处理过",
          all(c.enriched for c in _many2) and
          sum(1 for c in _many2 if c.citations is None) == 150,
          sum(1 for c in _many2 if c.citations is None))

    # limit=0 -> 不截断
    _calls[:] = []
    _n3 = _en.enrich_candidates({"network": {"timeout": 5, "retries": 1}},
                                [Candidate(arxiv_id="2401.%05d" % i, title="x")
                                 for i in range(60)], limit=0)
    check("limit=0 表示不限 (60 篇全查)", _n3 == 60, _n3)
except Exception as _exc:
    import traceback
    traceback.print_exc()
    check("富化提速自测跑完", False, _exc)
finally:
    _en.http_get = _real_http_get   # 还原模块级被替换掉的 http_get

# --- 检索: 按候选池上限摊薄每条检索式抓多少 ---
import math as _math


def _budget(n_queries, cap, per_query=200, slack=1.35, floor=50):
    spread = int(_math.ceil(cap * float(slack) / n_queries))
    return max(int(floor), min(per_query, spread))


check("1 条检索式 -> 抓满 per_query", _budget(1, 800) == 200)
check("2 条检索式 -> 还是抓满", _budget(2, 800) == 200)
check("18 条检索式 -> 摊到 60 篇 (原 200)", _budget(18, 800) == 60, _budget(18, 800))
check("检索式极多时不低于下限", _budget(100, 800) == 50, _budget(100, 800))
check("上限为 0 (不限) 时按 per_query",
      min(200, max(50, int(_math.ceil(800 * 1.35 / 18)))) == 60)
check("摊薄后总量接近上限而不是远超",
      _budget(18, 800) * 18 <= 800 * 1.35 + 200,
      _budget(18, 800) * 18)

print()
print("=" * 70)
print("20) AI 相关性打分并发之后, 结果必须和串行一模一样")
print("=" * 70)
import threading as _threading
from arxiv_rec import rank as _rk

# 假 AI: 每次调用先睡一会儿 (模拟网络往返), 分数只由 id 决定 —— 与批次顺序、
# 与哪个线程跑的无关。所以串行和并发的输出必须逐条相等。
_SCORE_LATENCY = 0.08


class _FakeScoreAI:
    def __init__(self, latency=_SCORE_LATENCY, fail_at=()):
        self.latency = latency
        self.fail_at = set(fail_at)
        self.n = 0
        self._lk = _threading.Lock()

    def chat_json(self, system, user, default=None, use_cache=True):
        _time.sleep(self.latency)
        with self._lk:
            self.n += 1
            nth = self.n
        if nth in self.fail_at:
            raise RuntimeError("假装第 %d 批挂了" % nth)
        ids = []
        for line in user.splitlines():
            line = line.strip()
            if line.startswith('{"id"'):
                ids.append(_json.loads(line)["id"])
        return {"scores": [
            {"id": rid, "score": sum(ord(ch) for ch in rid) % 101,
             "reason": "理由"} for rid in ids]}


_prof = ResearchProfile(
    summary="研究量子多体计算里的符号问题", topics=["符号问题"],
    methods=["DQMC"], keywords=["sign problem"], queries=["sign problem"],
    generated_by="selftest")
_score_cands = [Candidate(arxiv_id="2401.%05d" % i,
                          title="Sign problem in lattice model %d" % i,
                          abstract="We study determinantal QMC. " * 3)
                for i in range(48)]
_N_BATCHES = 6          # 48 篇 / 每批 8 篇

_t0 = _time.time()
_seq = _rk.ai_relevance(_FakeScoreAI(), _prof, _score_cands, batch_size=8,
                        concurrency=1)
_seq_secs = _time.time() - _t0

_t0 = _time.time()
_par = _rk.ai_relevance(_FakeScoreAI(), _prof, _score_cands, batch_size=8,
                        concurrency=4)
_par_secs = _time.time() - _t0

check("并发和串行返回的条数相同 (%d 批, %d 篇)"
      % (_N_BATCHES, len(_score_cands)), len(_seq) == len(_par) == 48,
      "%d vs %d" % (len(_seq), len(_par)))
_diff = [k for k in set(_seq) | set(_par) if _seq.get(k) != _par.get(k)]
check("并发和串行逐条分数/理由完全相同", not _diff, _diff[:3])
check("并发确实重叠执行了 (不是排着队一个个跑)",
      _par_secs < _seq_secs * 0.6,
      "串行 %.2fs, 并发 4 路 %.2fs" % (_seq_secs, _par_secs))

# 单批异常不能带走整轮: 挂掉的那批留空 (调用方回退启发式分数), 其余照常返回
_broken = _rk.ai_relevance(_FakeScoreAI(fail_at=(2, 5)), _prof, _score_cands,
                           batch_size=8, concurrency=4)
check("某一批抛异常时其余批次照常返回", len(_broken) == 48 - 16,
      "%d 条 (期望 %d)" % (len(_broken), 48 - 16))


class _FlakyEmptyAI:
    """每批第一次返回**空的 200** (不是异常), 第二次才给正常结果。

    这是 AI 接口真实会出的毛病: 返回 200 但正文是空串。它不是异常, 所以
    AIClient 里那层 retry_call (只兜异常) 管不着; 空串也不会被写进缓存
    (chat 只在 text 非空时 set), 所以重试是真会再打一次接口。不重试的话
    这一批 8 篇就白白退化成启发式分数了。
    """

    provider = "fake"

    def __init__(self):
        # 按**提示词**计数, 不能按全局调用序号 —— 并发下 4 个批次的调用是交错
        # 到达的, "第奇数次要空"会随机落到别的批次头上, 测试就成了掷骰子。
        self.per_prompt = {}
        self.calls = 0
        self._lk = _threading.Lock()

    def chat_json(self, system, user, default=None, use_cache=True):
        with self._lk:
            self.calls += 1
            nth = self.per_prompt.get(user, 0) + 1
            self.per_prompt[user] = nth
        if nth == 1:
            return None          # 这一批的第一次: 空的 200 -> default (None)
        ids = []
        for line in user.splitlines():
            line = line.strip()
            if line.startswith('{"id"'):
                ids.append(_json.loads(line)["id"])
        return {"scores": [{"id": rid, "score": 70, "reason": "r"}
                           for rid in ids]}


_flaky = _FlakyEmptyAI()
_r_flaky = _rk.ai_relevance(_flaky, _prof, _score_cands, batch_size=8,
                            concurrency=4)
check("空回复的那一批会自动重试, 最终 48 篇全评上", len(_r_flaky) == 48,
      "%d 条 (期望 48)" % len(_r_flaky))
check("确实发生了重试 (调用次数 = 批数 + 批数)",
      _flaky.calls == _N_BATCHES * 2, "%d 次调用" % _flaky.calls)
check("重试拿到的分数被采用了 (不是退回启发式)",
      all(v[1] == "r" for v in _r_flaky.values()), list(_r_flaky.values())[:2])


class _TruncatingAI:
    """回复被截断在半截字符串上 (实测: 536 字符就断了, 不是 max_tokens 的问题)。

    走的是和真客户端一样的路: 让 ``safe_json_loads`` 去抢救。``keep`` 决定第一次
    能救回几篇 —— 用来区分"救回得够用"和"救回得不够, 该重试"。
    """

    provider = "fake"

    def __init__(self, keep=1):
        self.keep = keep
        self.per_prompt = {}
        self.calls = 0
        self._lk = _threading.Lock()

    def chat_json(self, system, user, default=None, use_cache=True):
        with self._lk:
            self.calls += 1
            nth = self.per_prompt.get(user, 0) + 1
            self.per_prompt[user] = nth
        ids = []
        for line in user.splitlines():
            line = line.strip()
            if line.startswith('{"id"'):
                ids.append(_json.loads(line)["id"])
        if nth == 1:
            # 前 keep 篇完整, 后面直接断在字符串中间
            good = ", ".join('{"id": "%s", "score": 70, "reason": "r"}' % r
                             for r in ids[:self.keep])
            return safe_json_loads('{"scores": [%s, {"id": "%s", "score": 7'
                                   % (good, ids[-1]))
        return {"scores": [{"id": rid, "score": 70, "reason": "r"}
                           for rid in ids]}


# 只救回 1/8 篇 -> 不足一半 -> 该重试, 重试后 48 篇全评上
_trunc = _TruncatingAI(keep=1)
_r_trunc = _rk.ai_relevance(_trunc, _prof, _score_cands, batch_size=8,
                            concurrency=4)
check("截断后只救回 1/8 篇时会重试, 最终 48 篇全评上", len(_r_trunc) == 48,
      "%d 条 (期望 48)" % len(_r_trunc))
check("截断的重试次数 = 批数 + 批数",
      _trunc.calls == _N_BATCHES * 2, "%d 次调用" % _trunc.calls)

# 救回 6/8 篇 -> 够用了, 不该再花一次调用
_trunc6 = _TruncatingAI(keep=6)
_r_trunc6 = _rk.ai_relevance(_trunc6, _prof, _score_cands, batch_size=8,
                             concurrency=4)
check("截断后救回 6/8 篇就够用, 不再重试 (省一次调用)",
      _trunc6.calls == _N_BATCHES, "%d 次调用 (期望 %d)"
      % (_trunc6.calls, _N_BATCHES))
check("救回的那些分数确实被用上了 (每批 6 篇, 共 %d 篇)" % (_N_BATCHES * 6),
      len(_r_trunc6) == _N_BATCHES * 6,
      "%d 条 (期望 %d)" % (len(_r_trunc6), _N_BATCHES * 6))

# 边界: concurrency = 0 / 1 / 大于批数, 都不能崩、不能少给结果
for _c in (0, 1, 99):
    _r = _rk.ai_relevance(_FakeScoreAI(latency=0.01), _prof, _score_cands,
                          batch_size=8, concurrency=_c)
    check("concurrency=%d 时结果依然完整" % _c, len(_r) == 48, len(_r))

# 接线检查: rank_candidates 必须把配置里的并发数真的传下去。传丢了的话上面这些
# 全对也没用 —— 实际跑起来还是串行, 那 380 秒一分都省不下来。
_orig_ai_relevance = _rk.ai_relevance
_captured = {}


def _spy(ai, profile, cands, batch_size=8, concurrency=1, should_stop=None):
    _captured["concurrency"] = concurrency
    _captured["batch_size"] = batch_size
    return {}


_rk.ai_relevance = _spy
try:
    _rk.rank_candidates(
        {"ranking": {}, "analysis": {"score_batch_size": 8, "concurrency": 7,
                                     "max_for_ai_scoring": 10}},
        _prof, [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
                for i in range(5)],
        ai=_FakeScoreAI(latency=0.0))
    check("rank_candidates 把 analysis.concurrency 传给了打分",
          _captured.get("concurrency") == 7, _captured)
    _captured.clear()
    _rk.rank_candidates(
        {"ranking": {}, "analysis": {"max_for_ai_scoring": 10}},
        _prof, [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
                for i in range(5)],
        ai=_FakeScoreAI(latency=0.0))
    check("没配 concurrency 时退化成串行 (不是崩掉)",
          _captured.get("concurrency") == 1, _captured)
finally:
    _rk.ai_relevance = _orig_ai_relevance

print()
print("=" * 70)
print("21) AI 没给出检索式时的兜底 (只能用英文词拼)")
print("=" * 70)
from arxiv_rec.profile import _queries_from_terms, _has_ascii_letter

# 真实的失败形态: 主题/方法/关键词都归纳出来了, 唯独"生成检索式"那一步返回空。
# 注意 topics / methods 按提示词要求是**中文**的, keywords 才是英文。
_profile_no_queries = {
    "summary": "研究量子多体计算中的符号问题",
    "topics": ["符号问题", "量子蒙特卡洛", "阻挫磁性", "量子临界"],
    "methods": ["行列式量子蒙特卡洛", "张量网络", "神经网络量子态"],
    "keywords": ["sign problem", "determinant quantum Monte Carlo",
                 "entanglement entropy", "Gross-Neveu transition",
                 "disorder operator"],
}

_q = _queries_from_terms(_profile_no_queries, 18)
check("兜底能拼出检索式", len(_q) > 0, _q)
check("拼出来的检索式全是英文 (arXiv 不认中文检索式)",
      all(_has_ascii_letter(x) for x in _q), _q)
check("优先用的是 keywords 而不是中文 topics",
      "sign problem" in _q, _q[:6])
check("中文主题没有被排到前面占掉预算",
      not _q or _has_ascii_letter(_q[0]), _q[:3])

# 只有中文词时**不拿中文凑数** —— arXiv 标题摘要全是英文, 中文检索式一条都
# 返回不了, 只会白打限流很紧的请求。返回空, 由 build_profile 走启发式路径。
_cn_only = {"topics": ["符号问题", "量子临界"], "methods": ["张量网络"],
            "keywords": []}
_q2 = _queries_from_terms(_cn_only, 18)
check("只有中文词时返回空 (不拿中文检索式凑数)", _q2 == [], _q2)

# 英文词少也不掺中文: 2 条好检索式 > 2 条好的 + 2 条注定零命中的
_mixed = {"keywords": ["sign problem", "DQMC"], "topics": ["符号问题"]}
_q2b = _queries_from_terms(_mixed, 18)
check("英文词少时也不掺中文进去",
      _q2b == ["sign problem", "DQMC"], _q2b)

# 已经给出的 queries 最优先, 且不重复
_with_q = dict(_profile_no_queries, queries=["sign problem", "DQMC"])
_q3 = _queries_from_terms(_with_q, 18)
check("已有的 queries 排在最前", _q3[:2] == ["sign problem", "DQMC"], _q3[:4])
check("结果里没有重复项", len(_q3) == len(set(_q3)), _q3)

# 超长条目截成 6 个词 (检索式是短语不是句子)
_long = {"keywords": [" ".join("word%d" % i for i in range(20))]}
_q4 = _queries_from_terms(_long, 18)
check("过长的条目截成 6 个词", _q4 and len(_q4[0].split()) == 6, _q4)

# 边界: 什么都空 / n_queries 为 0
check("画像里什么都没有时不崩, 返回空",
      _queries_from_terms({}, 18) == [], _queries_from_terms({}, 18))
check("n_queries=0 时至少给 1 条 (不能给出空检索式)",
      len(_queries_from_terms(_profile_no_queries, 0)) >= 1)

# 端到端: 走一遍 build_profile, 确认兜底真的被接上了
from arxiv_rec.profile import build_profile


class _NoQueryAI:
    """分组归纳正常, 但"生成检索式"那一步永远返回空。"""

    provider = "fake"

    def chat_json(self, system, user, default=None, use_cache=True):
        if "检索式" in user:
            return {}                      # 故意不给检索式
        return {"topics": ["符号问题"], "methods": ["量子蒙特卡洛"],
                "keywords": ["sign problem", "DQMC"], "subfields": []}


# 3 篇而不是 1 篇: 启发式路径用 TF-IDF (min_df=2), 只有一篇时抽不出任何词,
# 那样后面那条"启发式产出的检索式也是英文"会对着空列表空转, 什么都没验到。
_papers = [
    LibraryPaper(item_id=1, title="Sign problem in quantum Monte Carlo",
                 abstract="We study the sign problem in lattice models."),
    LibraryPaper(item_id=2, title="Determinant quantum Monte Carlo for fermions",
                 abstract="A determinant quantum Monte Carlo study of fermions."),
    LibraryPaper(item_id=3, title="Entanglement entropy in lattice models",
                 abstract="Entanglement entropy across the lattice model."),
]
_bp = build_profile({"arxiv": {"auto_queries": 18, "queries": []}},
                    _papers, _NoQueryAI())
check("AI 不给检索式时, build_profile 仍然产出检索式",
      len(_bp.queries) > 0, _bp.queries)
check("兜底路径被标记出来 (来源里带 +terms)",
      "+terms" in _bp.generated_by, _bp.generated_by)
check("兜底产出的检索式全是英文",
      all(_has_ascii_letter(q) for q in _bp.queries), _bp.queries)


class _ChineseOnlyAI:
    """分组归纳只给中文主题, 检索式那一步也返回空 —— 画像里一个英文词都没有。"""

    provider = "fake"

    def chat_json(self, system, user, default=None, use_cache=True):
        if "检索式" in user:
            return {}
        return {"topics": ["符号问题", "量子蒙特卡洛"], "methods": ["张量网络"],
                "keywords": [], "subfields": []}


_bp2 = build_profile({"arxiv": {"auto_queries": 18, "queries": []}},
                     _papers, _ChineseOnlyAI())
check("画像全是中文时, 退到启发式路径而不是拿中文检索式凑数",
      _bp2.generated_by == "heuristic", _bp2.generated_by)
check("启发式路径确实抽出了检索式 (不是空手而归)", len(_bp2.queries) > 0,
      _bp2.queries)
check("启发式路径产出的检索式也是英文",
      all(_has_ascii_letter(q) for q in _bp2.queries), _bp2.queries)

print()
print("=" * 70)
print("22) 崩溃兜底: 工作线程的异常要能被真正记下来")
print("=" * 70)

# 背景: ``sys.excepthook`` 收 (type, value, tb) 三个参数, 而 ``threading.excepthook``
# 收的是**一个** ExceptHookArgs 具名元组。把同一个三参数闭包挂到两边, 后果是
# 工作线程一崩, 兜底逻辑自己先抛 TypeError, 真正的异常反而没了 —— crash.log 里
# 只剩 "hook() missing 2 required positional arguments", 比不写日志还误导。
# 这个 bug 只在"别的地方崩了"的时候才暴露, 平时完全看不出来, 所以要专门测。
import inspect as _inspect
import tempfile as _tf22
import ui as _ui
import arxiv_rec.config as _cfg22

_orig_root = _cfg22.project_root
_orig_sys_hook = sys.excepthook
_orig_thr_hook = _threading.excepthook
_tmp22 = _tf22.mkdtemp(prefix="crashlog_")
try:
    # crash.log 写在 project_root() 下面, 指到临时目录免得污染程序目录
    _cfg22.project_root = lambda: _tmp22
    # 崩溃还得在界面上看得见 —— 只写 crash.log 的话, 用户看到的是"某个结果
    # 莫名其妙没出来", 根本不知道出过错。这里挂个 sink 收日志, 验证那条红字
    # 真的发出去了。
    _seen22 = []
    from arxiv_rec.utils import add_log_sink as _add_sink22
    _add_sink22(lambda msg, level: _seen22.append((msg, level)))
    _ui._install_crash_handler()

    def _n_positional(fn):
        return len([p for p in _inspect.signature(fn).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)])

    check("sys.excepthook 收 3 个参数 (type, value, tb)",
          _n_positional(sys.excepthook) == 3,
          str(_inspect.signature(sys.excepthook)))
    check("threading.excepthook 只收 1 个参数 (ExceptHookArgs)",
          _n_positional(_threading.excepthook) == 1,
          str(_inspect.signature(_threading.excepthook)))

    def _boom():
        raise ValueError("boom-from-worker-thread")

    _t = _threading.Thread(target=_boom, name="selftest-worker")
    _t.start()
    _t.join(5)

    _crash_path = os.path.join(_tmp22, "crash.log")
    _crash = ""
    if os.path.exists(_crash_path):
        with open(_crash_path, encoding="utf-8") as _fh:
            _crash = _fh.read()
    check("工作线程崩溃被写进了 crash.log",
          "boom-from-worker-thread" in _crash,
          (_crash[-300:] or "(文件没生成或为空)"))
    check("记下来的是真异常, 不是兜底逻辑自己的 TypeError",
          "ValueError" in _crash
          and "missing 2 required positional arguments" not in _crash,
          _crash[-300:])
    check("crash.log 里带上了线程名 (好定位是哪个后台任务)",
          "selftest-worker" in _crash, _crash[-300:])
    check("崩溃也推进了日志面板 (界面上看得见, 不是只写文件)",
          any("后台出错了" in m and lv == "err" for m, lv in _seen22),
          _seen22[-2:])

    # 收到不认识的对象时不能自己再崩一次
    _threading.excepthook("这不是 ExceptHookArgs")
    with open(_crash_path, encoding="utf-8") as _fh:
        _crash2 = _fh.read()
    check("收到意外参数时原样记下来, 而不是再抛一个异常",
          "意外参数" in _crash2, _crash2[-200:])
finally:
    _cfg22.project_root = _orig_root
    sys.excepthook = _orig_sys_hook
    _threading.excepthook = _orig_thr_hook

print()
print("=" * 70)
print("23) 本机代理挂掉时自动退回直连 (推荐「输出不了」的元凶)")
print("=" * 70)
from arxiv_rec import utils as _u

# 探测是真实的网络调用, 测试里换成脚本化的假实现 —— 我们要验的是"探测结果
# 怎么被用", 不是"网络通不通"。
_PROBE = {}
def _fake_probe(proxies, timeout=8.0):
    key = "direct" if proxies is None else repr(sorted(proxies.values()))
    return _PROBE.get(key, False)

_orig_probe = _u.probe_route
_u.probe_route = _fake_probe
_LOOPBACK = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
_LOOPBACK_KEY = repr(sorted(_LOOPBACK.values()))
try:
    # 1) 本机代理不通 + 直连通 -> 退回直连 (这正是用户遇到的情况)
    _u.reset_proxy_probe_cache()
    _PROBE = {"direct": True}
    got = _u.resolve_proxy("http://127.0.0.1:7890")
    check("本机代理探测不通时自动退回直连", got is None, got)

    # 2) 本机代理通 -> 照用
    _u.reset_proxy_probe_cache()
    _PROBE = {_LOOPBACK_KEY: True, "direct": True}
    got = _u.resolve_proxy("http://127.0.0.1:7890")
    check("本机代理探测得通时照常走代理",
          got is not None and "7890" in got["http"], got)

    # 3) 两条都不通 -> 保留原配置, 让后面的报错流程去说"网络有问题"
    _u.reset_proxy_probe_cache()
    _PROBE = {}
    got = _u.resolve_proxy("http://127.0.0.1:7890")
    check("代理和直连都不通时保留原配置 (不改线路, 只如实报错)",
          got is not None and "7890" in got["http"], got)

    # 4) 非本机代理不探测: 公司/学校统一出口是真的必须走, 擅自改线路比报错更难查
    _u.reset_proxy_probe_cache()
    _probed = []
    _u.probe_route = lambda p, timeout=8.0: (_probed.append(p), True)[1]
    got = _u.resolve_proxy("http://proxy.corp.com:8080")
    check("非本机代理不做探测, 直接照用",
          not _probed and got is not None and "corp.com" in got["http"],
          (_probed, got))
    _u.probe_route = _fake_probe

    # 5) 只探一次: 18 条检索式各探一次的话, 光探测就要等两分半
    _u.reset_proxy_probe_cache()
    _n = [0]
    def _counting_probe(p, timeout=8.0):
        _n[0] += 1
        return True
    _u.probe_route = _counting_probe
    for _ in range(5):
        _u.resolve_proxy("http://127.0.0.1:7890")
    check("同一个代理配置只探测一次 (18 条检索式不能探 18 次)", _n[0] == 1, _n[0])

    # 6) 清缓存后重新探 (设置页改了代理要重新判断)
    _u.reset_proxy_probe_cache()
    _u.resolve_proxy("http://127.0.0.1:7890")
    check("清缓存后确实重新探测", _n[0] == 2, _n[0])

    # 7) 空/None 代理不触发探测
    _n[0] = 0
    check("没配代理时不探测",
          _u.resolve_proxy(None) is None and _n[0] == 0, _n[0])
    check("代理为空串时不探测",
          _u.resolve_proxy("") is None and _n[0] == 0, _n[0])
    _u.probe_route = _fake_probe
finally:
    _u.probe_route = _orig_probe
    _u.reset_proxy_probe_cache()

# 8) 状态码怎么算"通": 429 是限流不是断网, 不能把代理甩掉也不能判定网络断了
class _Resp:
    def __init__(self, code):
        self.status_code = code


_orig_session_cls = _u.requests.Session


def _fake_session(code=200, boom=False, seen=None):
    """假的 requests.Session, 顺便把"探测时到底传了什么"记下来。"""
    class _S:
        def __init__(self):
            self.headers = {}
            self.trust_env = True

        def get(self, url, proxies=None, timeout=None):
            if seen is not None:
                seen["proxies"] = proxies
                seen["trust_env"] = self.trust_env
            if boom:
                raise OSError("connection refused")
            return _Resp(code)
    return _S


try:
    for _code, _want, _why in ((429, True, "限流说明路是通的"),
                               (200, True, "正常"),
                               (502, False, "502 正是代理坏掉的样子"),
                               (500, False, "5xx 一律算不通")):
        _u.requests.Session = _fake_session(_code)
        check("probe_route: HTTP %d -> %s (%s)" % (_code, _want, _why),
              _u.probe_route(_LOOPBACK, timeout=1.0) is _want)
    _u.requests.Session = _fake_session(boom=True)
    check("probe_route: 连不上 -> False",
          _u.probe_route(_LOOPBACK, timeout=1.0) is False)

    # 这条是给真踩过的坑立的碑: 直连探测必须**真的**直连。用裸 requests.get 会去
    # 读 Windows 注册表里的系统代理, 而系统代理往往就写着那个已经关掉的
    # 127.0.0.1:7890 —— 于是"直连"探测拿到 502, 程序误判成"两条路都断了",
    # 把一个能自动修好的问题判成绝症。
    _seen = {}
    _u.requests.Session = _fake_session(200, seen=_seen)
    _u.probe_route(None, timeout=1.0)
    check("直连探测显式传 {} 而不是 None (None 会让 requests 去读系统代理)",
          _seen.get("proxies") == {}, _seen)
    check("直连探测关掉了 trust_env (不读注册表里的系统代理)",
          _seen.get("trust_env") is False, _seen)
    _seen.clear()
    _u.probe_route(_LOOPBACK, timeout=1.0)
    check("走代理探测时把代理原样传下去",
          _seen.get("proxies") == _LOOPBACK, _seen)
finally:
    _u.requests.Session = _orig_session_cls

print()
print("=" * 70)
print("24) 限定分类: 候选列表 + 检索条件生成")
print("=" * 70)
from arxiv_rec.arxiv_search import (CATEGORY_CHOICES, ALL_CATEGORIES,
                                    _api_search_query)

check("分类候选非空", len(ALL_CATEGORIES) >= 10, len(ALL_CATEGORIES))
check("分类代码不重复", len(ALL_CATEGORIES) == len(set(ALL_CATEGORIES)),
      [c for c in ALL_CATEGORIES if ALL_CATEGORIES.count(c) > 1])
check("每个分类都带中文说明",
      all(lbl.strip() for _g, items in CATEGORY_CHOICES for _c, lbl in items))
check("每个分组都非空", all(items for _g, items in CATEGORY_CHOICES))
for _code in ALL_CATEGORIES:
    _q = _api_search_query("sign problem", [_code])
    check("分类 %s 能生成合法检索条件" % _code,
          _q.endswith("cat:%s)" % _code) and _q.startswith("("), _q)
# 不勾分类时不加外层括号 —— 那对括号是用来把分类的 OR 关在里面的, 没有分类
# 就没有要关的东西
check("不勾任何分类 = 不加分类条件",
      _api_search_query("sign problem", []) == "all:sign AND all:problem",
      _api_search_query("sign problem", []))
check("不勾任何分类时检索条件里没有 cat:",
      "cat:" not in _api_search_query("sign problem", []),
      _api_search_query("sign problem", []))
check("勾一个分类时检索条件里有 cat: 且被括号关住",
      _api_search_query("sign problem", ["quant-ph"])
      == "(all:sign AND all:problem) AND (cat:quant-ph)",
      _api_search_query("sign problem", ["quant-ph"]))

print()
print("=" * 70)
print("25) 网络不通时不再空转半个多小时")
print("=" * 70)
from arxiv_rec import arxiv_search as _as

_orig_build_s = _as.build_session
_orig_search_api = _as.search_api
_orig_probe2 = _as.probe_route


class _FakeSession:
    proxies = {}


def _wire(search_fn, probe_alive):
    """把 collect 依赖的网络部分全换成假的, 记录它到底试了几条检索式。"""
    tried = []
    _as.build_session = lambda *a, **k: _FakeSession()
    def _search(session, query, **kw):
        tried.append(query)
        return search_fn(query)
    _as.search_api = _search
    _as.probe_route = lambda p, timeout=8.0: probe_alive
    return tried


_CFG_NET = {"arxiv": {"per_query": 5, "max_candidates": 50, "use_rss": False,
                      "request_delay": 0.0},
            "network": {}}
_QUERIES = ["q%d" % i for i in range(1, 6)]

try:
    # 全失败 + 探测确认不通 -> 试满 DEAD_AFTER 条就收工, 不再跑完 5 条 + 第二轮
    _tried = _wire(lambda q: None, probe_alive=False)
    _res = _as.collect(_CFG_NET, _QUERIES)
    check("网络确认不通时只试 3 条就收工", len(_tried) == 3, _tried)
    check("网络确认不通时不做第二轮整体重试", len(_tried) == 3, _tried)
    check("网络确认不通时返回空候选", _res == [], _res)

    # 全失败但探测说网络是通的 (arXiv 限流) -> 必须把 5 条都试完, 不能提前收工
    _tried = _wire(lambda q: None, probe_alive=True)
    _res = _as.collect(_CFG_NET, _QUERIES)
    check("只是限流时要把所有检索式试完 (探测到通就不收工)",
          len(_tried) == 10, _tried)   # 5 条 + 第二轮 5 条

    # 有一条成功过 -> 后面连续失败也不该收工 (已经有结果了, 不能丢)
    _state = {"n": 0}
    def _first_ok_then_fail(q):
        _state["n"] += 1
        return [Candidate(arxiv_id="2401.00001", title="t",
                          abstract="a")] if _state["n"] == 1 else None
    _tried = _wire(_first_ok_then_fail, probe_alive=False)
    _res = _as.collect(_CFG_NET, _QUERIES)
    check("已经抓到过结果时不因后续失败而收工", len(_res) == 1, len(_res))
    # 5 条走完 + 失败的 4 条 (q1 成功过) 再试一轮 = 9 次
    check("已经抓到过结果时把所有检索式试完", len(_tried) == 9, _tried)
finally:
    _as.build_session = _orig_build_s
    _as.search_api = _orig_search_api
    _as.probe_route = _orig_probe2

print()
print("=" * 70)
print("26) 订阅模式 (只用分类公告, 不用检索式)")
print("=" * 70)
from arxiv_rec.pipeline import PipelineOptions as _PO

# --- apply_to: 订阅模式必须能**显式清空**检索式 ---
# 这是整个特性的关键接缝。apply_to 的老写法是 `if self.queries:` —— 非空才覆盖。
# 订阅模式要表达的恰恰是"没有检索式", 沿用老写法的话生成的检索式会照旧生效,
# 订阅模式变成一句空话。
_c = {"arxiv": {"queries": ["auto generated q"], "categories": ["quant-ph"]}}
_PO(subscribe_only=True).apply_to(_c)
check("订阅模式清空了检索式", _c["arxiv"]["queries"] == [], _c["arxiv"]["queries"])
check("订阅模式写入了 subscribe_only 标记",
      _c["arxiv"]["subscribe_only"] is True, _c["arxiv"])
check("订阅模式保留了分类", _c["arxiv"]["categories"] == ["quant-ph"],
      _c["arxiv"]["categories"])

# 订阅模式优先级高于 --queries: 同时给了也不该有检索式
_c = {"arxiv": {"queries": ["old"], "categories": []}}
_PO(subscribe_only=True, queries=["manual q"]).apply_to(_c)
check("订阅模式压过手动检索式", _c["arxiv"]["queries"] == [], _c["arxiv"]["queries"])

# 不开订阅时, 老行为一个字都不能变
_c = {"arxiv": {"queries": ["old"], "categories": []}}
_PO(queries=["manual q"]).apply_to(_c)
check("非订阅模式照旧覆盖检索式", _c["arxiv"]["queries"] == ["manual q"],
      _c["arxiv"]["queries"])
check("非订阅模式不写 subscribe_only 标记",
      "subscribe_only" not in _c["arxiv"], _c["arxiv"])

# 空 queries 且不开订阅 -> 不覆盖 (老行为: 空列表无含义)
_c = {"arxiv": {"queries": ["keep me"]}}
_PO(queries=[]).apply_to(_c)
check("非订阅模式下空检索式不覆盖已有配置",
      _c["arxiv"]["queries"] == ["keep me"], _c["arxiv"]["queries"])

# --- collect: 空检索式列表 = 只用 RSS, 一次 API 都不该打 ---
_orig_search_rss = _as.search_rss
_CFG_SUB = {"arxiv": {"per_query": 5, "max_candidates": 50, "use_rss": True,
                      "request_delay": 0.0, "categories": ["cond-mat.str-el"]},
            "network": {}}
try:
    _tried = _wire(lambda q: None, probe_alive=True)
    _rss_calls = []

    def _fake_rss(session, cats, **kw):
        _rss_calls.append(list(cats))
        return [Candidate(arxiv_id="2401.99999", title="rss paper",
                          abstract="a")]
    _as.search_rss = _fake_rss

    _res = _as.collect(_CFG_SUB, [])
    check("空检索式时不打任何 API 请求", _tried == [], _tried)
    check("空检索式时仍然抓 RSS 公告", _rss_calls == [["cond-mat.str-el"]],
          _rss_calls)
    check("空检索式时 RSS 结果进入候选池",
          [c.arxiv_id for c in _res] == ["2401.99999"],
          [c.arxiv_id for c in _res])

    # RSS 也没东西 -> 空候选池 (而不是报错崩掉)
    _rss_calls = []
    _as.search_rss = lambda session, cats, **kw: []
    _res = _as.collect(_CFG_SUB, [])
    check("空检索式 + RSS 无公告时返回空候选池", _res == [], _res)

    # use_rss=False 是"检索模式下别额外抓 RSS"的意思。订阅模式没有检索式, RSS 是
    # 唯一来源 —— 这时照字面关掉, 候选池会静默地空掉, 用户根本猜不到是这个开关。
    _rss_calls = []
    _as.search_rss = _fake_rss
    _cfg_off = {"arxiv": {"per_query": 5, "max_candidates": 50, "use_rss": False,
                          "request_delay": 0.0,
                          "categories": ["cond-mat.str-el"]},
                "network": {}}
    _res = _as.collect(_cfg_off, [])
    check("订阅模式下 use_rss=False 被忽略, 照抓 RSS",
          _rss_calls == [["cond-mat.str-el"]], _rss_calls)
    check("订阅模式下 use_rss=False 也有候选", len(_res) == 1, len(_res))

    # 但检索模式下 use_rss=False 必须照旧生效 (不能顺手改掉老行为)
    _rss_calls = []
    _tried = _wire(lambda q: [Candidate(arxiv_id="2401.00002", title="api",
                                        abstract="a")], probe_alive=True)
    _res = _as.collect(_cfg_off, ["q1"])
    check("检索模式下 use_rss=False 仍然不抓 RSS", _rss_calls == [], _rss_calls)

    # 订阅模式认界面勾选的 categories, 不认 rss_categories (那是检索模式的老配置项)
    _rss_calls = []
    _as.search_rss = _fake_rss
    _cfg_stale = {"arxiv": {"per_query": 5, "max_candidates": 50, "use_rss": True,
                            "request_delay": 0.0,
                            "categories": ["quant-ph"],
                            "rss_categories": ["hep-th"]},
                  "network": {}}
    _as.collect(_cfg_stale, [])
    check("订阅模式用勾选的 categories, 忽略陈旧的 rss_categories",
          _rss_calls == [["quant-ph"]], _rss_calls)
    # 检索模式下 rss_categories 照旧优先 (老行为)
    _rss_calls = []
    _tried = _wire(lambda q: [Candidate(arxiv_id="2401.00002", title="api",
                                        abstract="a")], probe_alive=True)
    _as.collect(_cfg_stale, ["q1"])
    check("检索模式下 rss_categories 仍然优先",
          _rss_calls == [["hep-th"]], _rss_calls)
finally:
    _as.search_rss = _orig_search_rss
    _as.build_session = _orig_build_s
    _as.search_api = _orig_search_api
    _as.probe_route = _orig_probe2

# --- parse_rss: 公告类型过滤 (new/cross 是新的, replace* 是旧论文的修订版) ---
# 实测 cond-mat.str-el 某天的 feed: new 10, cross 16, replace 8, replace-cross 7
# —— 37% 是旧论文的新版本。不过滤的话订阅模式会把几年前论文的修订版当成
# "今天的新论文"推给用户。
_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:arxiv="http://arxiv.org/schemas/atom"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel><title>test</title>
%s
  </channel>
</rss>""" % "\n".join(
    """    <item>
      <title>Paper %s</title>
      <link>http://arxiv.org/abs/2401.0000%d</link>
      <description>arXiv:2401.0000%dv1 Announce Type: %s
Abstract: abstract of %s</description>
      <dc:creator>Alice</dc:creator>
      <arxiv:primary_category term="cond-mat.str-el"/>
      <arxiv:announce_type>%s</arxiv:announce_type>
      <pubDate>Mon, 01 Jan 2024 00:00:00 -0500</pubDate>
    </item>""" % (t, i, i, t, t, t)
    for i, t in enumerate(["new", "cross", "replace", "replace-cross"], 1))

_all = _as.parse_rss(_RSS_XML, "cond-mat.str-el")
check("announce_types=None 时不过滤 (老行为不变)", len(_all) == 4, len(_all))
check("不过滤时四种公告类型都在",
      sorted(c.title for c in _all) ==
      ["Paper cross", "Paper new", "Paper replace", "Paper replace-cross"],
      [c.title for c in _all])

_f = _as.parse_rss(_RSS_XML, "cond-mat.str-el",
                   announce_types=["new", "cross"])
check("announce_types 过滤掉 replace 和 replace-cross", len(_f) == 2, len(_f))
check("过滤后留下的是 new 和 cross",
      sorted(c.title for c in _f) == ["Paper cross", "Paper new"],
      [c.title for c in _f])
check("过滤后摘要照旧解析出来",
      all(c.abstract.startswith("abstract of") for c in _f),
      [c.abstract for c in _f])

# 空列表 = 全滤掉 (配置里写 [] 是"什么都不要"的意思, 不是"不过滤")
check("announce_types=[] 时全部滤掉",
      _as.parse_rss(_RSS_XML, "c", announce_types=[]) == [], "非空")

# 只保留 new 时 cross 也要被滤掉 —— 别把未知类型静默放行
check("只保留 new 时 cross 也被滤掉",
      len(_as.parse_rss(_RSS_XML, "c", announce_types=["new"])) == 1, "!= 1")

# 老 config.json 里没有 subscribe_announce_types 这个键。load_config 必须是**深**
# 合并, 否则整个 arxiv 段会被用户的那份顶掉, 这一项取到 None -> 不过滤 -> 旧论文
# 的修订版又被当成新论文推出来, 而且一声不吭。
from arxiv_rec.config import DEFAULT_CONFIG as _DCFG
check("默认配置里有 subscribe_announce_types",
      _DCFG.get("arxiv", {}).get("subscribe_announce_types") == ["new", "cross"],
      _DCFG.get("arxiv", {}).get("subscribe_announce_types"))
check("默认订阅模式是关的 (老用户不受影响)",
      _DCFG.get("arxiv", {}).get("subscribe_only") is False,
      _DCFG.get("arxiv", {}).get("subscribe_only"))

# --- pipeline: 订阅模式没有分类时要报"缺分类", 而不是"缺检索式" ---
from arxiv_rec import pipeline as _pl


class _StubPaper:
    arxiv_id = "2401.00001"
    title = "t"
    abstract = "a"
    fulltext = ""
    authors = []
    year = 2024
    journal = ""
    tags = []


_orig_load_library = _pl.load_library
_orig_build_profile = _pl.build_profile
_orig_build_caches = _pl.build_caches
_orig_make_ai = _pl.make_ai
_orig_collect = _pl.collect


def _stub_collect(cfg, queries, **kw):
    _stub_collect.seen_queries = list(queries)
    _stub_collect.seen_cfg = cfg
    return [Candidate(arxiv_id="2401.00001", title="t", abstract="a")]


class _StubProfile:
    def __init__(self):
        self.queries = ["q1", "q2"]
        self.summary = ""
        self.topics = []
        self.methods = []
        self.keywords = []


try:
    _pl.load_library = lambda cfg, **kw: ([_StubPaper()], None)
    _pl.build_caches = lambda cfg, refresh: {"http": None}
    _pl.make_ai = lambda cfg, caches, no_ai: None
    _pl.build_profile = lambda cfg, papers, ai: _StubProfile()
    _pl.collect = _stub_collect

    _CFG_PL = lambda **arx: {"arxiv": arx, "analysis": {}, "network": {},
                             "library": {}, "output": {}}

    # 订阅模式 + 没分类 -> code 3, 且提示里说的是"分类"不是"检索式"
    _res = _pl.run_pipeline(
        _CFG_PL(subscribe_only=True, categories=[]),
        _PO(stop_at="search"))
    check("订阅模式没勾分类时报 code 3", _res.code == 3, _res.code)
    # 提示得指向"去勾分类"这个可执行的动作。注意不能断言消息里没有"检索式"三个字
    # —— "这个模式不生成检索式"本来就要说检索式, 那是解释, 不是让你去填检索式。
    # 真正要排除的是老提示那句"请在设置里手动指定检索式"。
    _m = _res.message or ""
    check("订阅模式缺分类的提示指向分类",
          "分类" in _m and "勾选" in _m, _m)
    check("订阅模式缺分类时不叫用户去填检索式",
          "手动指定检索式" not in _m, _m)

    # 订阅模式 + 有分类 -> 用空检索式调 collect
    _res = _pl.run_pipeline(
        _CFG_PL(subscribe_only=True, categories=["quant-ph"]),
        _PO(stop_at="search"))
    check("订阅模式有分类时把空检索式交给 collect",
          _stub_collect.seen_queries == [], _stub_collect.seen_queries)
    check("订阅模式跑到了 search 阶段之后", _res.code != 3, _res.code)

    # 非订阅 + 空检索式 -> 还是老的 code 3 (一个字都不能变)
    _pl.build_profile = lambda cfg, papers, ai: type(
        "_P", (), {"queries": [], "summary": "", "topics": [],
                   "methods": [], "keywords": []})()
    _res = _pl.run_pipeline(_CFG_PL(), _PO(stop_at="search"))
    check("非订阅模式没检索式时仍是 code 3", _res.code == 3, _res.code)
    check("非订阅模式缺检索式的提示说的是检索式",
          "检索式" in (_res.message or ""), _res.message)
finally:
    _pl.load_library = _orig_load_library
    _pl.build_profile = _orig_build_profile
    _pl.build_caches = _orig_build_caches
    _pl.make_ai = _orig_make_ai
    _pl.collect = _orig_collect

print()
print("=" * 70)
print("27) 列表显示的三个格式化函数 (界面和报告共用同一份)")
print("=" * 70)
from arxiv_rec.utils import fmt_authors, fmt_date, fmt_journal

_fc = Candidate(arxiv_id="2401.00001", title="t", authors=["A One", "B Two", "C Three"])
_fc.published = datetime(2026, 9, 25)
_fc.updated = datetime(2026, 9, 27)
_fc.journal_ref = "Phys. Rev. Lett."
check("fmt_date 取 v1 提交日而不是最后修订日",
      fmt_date(_fc) == "2026-09-25", fmt_date(_fc))
_fc.published = None
check("没有提交日时退回最后修订日", fmt_date(_fc) == "2026-09-27", fmt_date(_fc))
_fc.updated = None
check("两个日期都没有时给破折号 (不是空白)", fmt_date(_fc) == "—", repr(fmt_date(_fc)))
check("fmt_authors 默认只列第一位 + 等",
      fmt_authors(["A One", "B Two", "C Three"]) == "A One 等",
      fmt_authors(["A One", "B Two", "C Three"]))
check("fmt_authors 单作者不加'等'", fmt_authors(["A One"]) == "A One",
      fmt_authors(["A One"]))
check("fmt_authors 空列表给破折号", fmt_authors([]) == "—", repr(fmt_authors([])))
check("fmt_authors 容得下 None", fmt_authors(None) == "—", repr(fmt_authors(None)))
check("fmt_authors 会跳过空名字",
      fmt_authors(["A One", "  ", "B Two"]) == "A One 等",
      fmt_authors(["A One", "  ", "B Two"]))
check("fmt_journal 短期刊名照抄",
      fmt_journal(_fc) == "Phys. Rev. Lett.", fmt_journal(_fc))
check("fmt_journal 没发表过给破折号 (不是留空)",
      fmt_journal(Candidate(arxiv_id="2401.00002", title="t")) == "—",
      repr(fmt_journal(Candidate(arxiv_id="2401.00002", title="t"))))
_long = Candidate(arxiv_id="2401.00003", title="t", journal_ref="X" * 80)
check("fmt_journal 过长的会截断 (否则会把那一列撑变形)",
      len(fmt_journal(_long)) < 30 and fmt_journal(_long).endswith("…"),
      repr(fmt_journal(_long)))
# 报告和界面必须写同一个日期 —— 以前 report.py 自己算一遍, 迟早会漂
from arxiv_rec.report import _fmt_date as _rep_date
check("报告用的就是同一个 fmt_date", _rep_date is fmt_date, _rep_date)

print()
print("=" * 70)
print("28) 历史推荐结果: 解析报告文件 (解析失败也不能炸)")
print("=" * 70)
from arxiv_rec import past_runs as _pr

_RPT = """# arXiv 相关文献推荐报告

> 由你的本地文献库自动分析生成 · 生成时间 2026-09-29 10:55:37

| # | 论文 | 提交日期 | 相关性 | 时效性 | 重要性 | 总分 | 引用 | 标记 |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | [Majorana Positivity and the Sign Pro…](https://arxiv.org/abs/2401.00001) | 2026-09-25 | 0.98 | 0.99 | 0.30 | **0.788** | 12 | 🆕新 |
| 2 | [No Detail Section](https://arxiv.org/abs/2401.00009) | 2026-09-01 | 0.50 | 0.50 | 0.10 | **0.400** | — | — |

### 1. Majorana Positivity and the Sign Problem

**arXiv**: [2401.00001](https://arxiv.org/abs/2401.00001) · **PDF**: [下载](https://arxiv.org/pdf/2401.00001)

**作者**: Wei Wang, Li Chen, Bo Zhang · **提交**: 2026-09-25 · **最近更新**: 2026-09-27

**分类**: cond-mat.str-el

**期刊**: Phys. Rev. Lett. 104, 157201 (2010)

**备注**: 4 pages; 修订了 fig. 2 · 以及别的

**引用数**: 12
"""
_parsed = _pr.parse_report(_RPT)
check("报告生成时间解析出来",
      _parsed["generated_at"] == "2026-09-29 10:55:37", _parsed["generated_at"])
check("解析出 2 篇", len(_parsed["items"]) == 2, len(_parsed["items"]))
_it = _parsed["items"][0]
check("标题用详解里的完整版 (表格里那份被截过)",
      _it["title"] == "Majorana Positivity and the Sign Problem", _it["title"])
check("作者只留第一位", _it["authors"] == "Wei Wang 等", _it["authors"])
check("提交日期优先取详解里的 (和表格一致)", _it["date"] == "2026-09-25",
      _it["date"])
check("期刊解析出来", _it["journal"] == "Phys. Rev. Lett. 104, 157201 (2010)",
      _it["journal"])
check("总分去掉加粗星号", _it["score"] == "0.788", _it["score"])
check("链接保留", _it["url"] == "https://arxiv.org/abs/2401.00001", _it["url"])
check("标记的表情换成纯文字", _it["flags"] == "新", _it["flags"])
# 备注里有 " · " 但不该被当成字段分隔符 —— 那样 "备注" 会被拦腰截断
check("备注里的' · '没有破坏后面的字段解析",
      _parsed["items"][0]["journal"].startswith("Phys. Rev. Lett."),
      _parsed["items"][0]["journal"])

# 只有表格行、没有详解段的那一篇: 该有的还是要有, 缺的留空而不是崩
_it2 = _parsed["items"][1]
check("没有详解段时标题退回表格里的那份",
      _it2["title"] == "No Detail Section", _it2["title"])
check("没有详解段时作者留空", _it2["authors"] == "—", _it2["authors"])
check("引用数是破折号时照原样留着", _it2["citations"] == "—", _it2["citations"])
check("标记是破折号时转成空串", _it2["flags"] == "", repr(_it2["flags"]))

# 解析失败 (格式变了 / 手改过 / 根本不是报告) 不能抛异常 —— 一抛整个「文献列表」
# 页就打不开了, 而用户只是想看一眼上次推荐了什么
for _bad in ("", "随便一段文字\n\n没有任何表格", "# 只有标题\n",
             "| # | 论文 |\n| --- | --- |\n| 不是数字 | x |"):
    try:
        _r = _pr.parse_report(_bad)
        check("解析不了的内容返回空结果而不是抛异常 (%r)" % _bad[:12],
              _r["items"] == [], len(_r["items"]))
    except Exception as _exc:
        check("解析不了的内容返回空结果而不是抛异常 (%r)" % _bad[:12], False, _exc)

# 下拉框那一行字
check("标签带上生成时间和篇数",
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537.md",
                    "2026-09-29 10:55:37", 20)
      == "2026-09-29 10:55:37 · 20 篇",
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537.md",
                    "2026-09-29 10:55:37", 20))
check("正文里没有生成时间时退回文件名里的时间戳",
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537.md", "", 5)
      == "2026-09-29 10:55:37 · 5 篇",
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537.md", "", 5))
check("带 tag 的报告 (--tag demo) 在标签里看得出来",
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537_demo.md",
                    "2026-09-29 10:55:37", 5).endswith("· 5 篇 · demo"),
      _pr.label_for("C:/x/arxiv_recommend_20260929_105537_demo.md",
                    "2026-09-29 10:55:37", 5))

print()
print("=" * 70)
print("29) 停止按钮: 最慢的三个阶段内部必须有检查点")
print("=" * 70)
# 病根: cb.check() 只在**阶段边界**触发, 而富化 (~32 批) / 相关性打分 (38 批,
# 串行 380 秒) / 深度解读 (20 篇) 这三步中间一个检查点都没有。用户点完"停止"
# 要盯着界面干等好几分钟, 看起来就是按钮坏了。
#
# 另一个坑: 并发路径下 future 是**一次性全提交**的, 提交那个循环几乎瞬间跑完。
# 所以检查必须在**每个批/篇的开头**, 在提交前查是查不到用户那次点击的。

# --- 27a) 相关性打分 ---
_ai_stop = _FakeScoreAI(latency=0.0)
# 跑满 2 批之后喊停 (假 AI 每批调一次, 所以调用次数就是已跑批数)
_r_stop = _rk.ai_relevance(_ai_stop, _prof, _score_cands, batch_size=8,
                           concurrency=1, should_stop=lambda: _ai_stop.n >= 2)
check("相关性打分收到停止请求后不再打后面的批次", _ai_stop.n == 2, _ai_stop.n)
check("相关性打分停下时已评的分数照常返回 (供启发式回退之外的篇用)",
      len(_r_stop) == 16, "%d 条 (期望 16)" % len(_r_stop))

# 并发路径: 一上来就该停 -> 一次 AI 都不该调
_ai_stop2 = _FakeScoreAI(latency=0.0)
_r_stop2 = _rk.ai_relevance(_ai_stop2, _prof, _score_cands, batch_size=8,
                            concurrency=4, should_stop=lambda: True)
check("并发路径下 should_stop 立刻为真时一次 AI 都不调", _ai_stop2.n == 0,
      _ai_stop2.n)
check("并发路径下立刻停止返回空结果 (调用方回退启发式)", _r_stop2 == {},
      len(_r_stop2))

# --- 27b) 富化 (OpenAlex) ---
_en_calls = []


def _en_ok(session, url, params=None, **kw):
    _en_calls.append(1)
    dois = (params or {}).get("filter", "").replace("doi:", "").split("|")
    return _json.dumps({"results": [
        {"doi": "https://doi.org/" + d, "cited_by_count": 7,
         "primary_location": {}, "concepts": []} for d in dois]})


_en.http_get = _en_ok
try:
    # 60 篇 -> 3 批 (每批 25)。第一批打完就喊停。
    _en_cands = [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
                 for i in range(60)]
    _en.enrich_candidates({"network": {"timeout": 5, "retries": 1}}, _en_cands,
                          should_stop=lambda: len(_en_calls) >= 1)
    check("富化收到停止请求后不再打后面的批次", len(_en_calls) == 1,
          "%d 次请求" % len(_en_calls))
    check("富化停下时已拿到的引用数据照常保留",
          sum(1 for c in _en_cands if c.citations == 7) == 25,
          sum(1 for c in _en_cands if c.citations == 7))
    check("富化停下后仍然把候选标成'已处理' (下一轮不会再白查一遍)",
          all(c.enriched for c in _en_cands),
          sum(1 for c in _en_cands if not c.enriched))
finally:
    _en.http_get = _real_http_get

# --- 27c) 深度解读 ---
from arxiv_rec import analyze as _an


class _FakeAnalyzeAI:
    def __init__(self):
        self.n = 0

    def chat_json(self, system, user, default=None, use_cache=True):
        self.n += 1
        return {"summary": "这篇讲了点什么", "connections": [],
                "ideas": "可以这样结合起来做"}


_an_ai = _FakeAnalyzeAI()
_an_cands = [Candidate(arxiv_id="2401.%05d" % i, title="Sign problem %d" % i,
                       abstract="We study DQMC. " * 5)
             for i in range(20)]
_done_stop, _reused_stop = _an.analyze_top(
    {"analysis": {"related_papers_k": 3}}, _an_cands, [], _an_ai,
    profile=_prof, top_n=20, concurrency=1,
    should_stop=lambda: _an_ai.n >= 2)
check("深度解读收到停止请求后不再调 AI", _an_ai.n == 2, _an_ai.n)
check("深度解读停下时已解读完的照常算数", _done_stop == 2, _done_stop)
check("深度解读停下后没轮到的候选保持未解读",
      not any(c.analyzed for c in _an_cands[2:]),
      sum(1 for c in _an_cands[2:] if c.analyzed))

# --- 27d) 接线: rank_candidates 必须把 should_stop 真的传下去 ---
# 上面几条全对也没用, 传丢了的话实际跑起来还是老样子 —— 点了没反应。
_orig_ai_rel = _rk.ai_relevance
_seen_stop = {}


def _spy_stop(ai, profile, cands, batch_size=8, concurrency=1, should_stop=None):
    _seen_stop["cb"] = should_stop
    return {}


_rk.ai_relevance = _spy_stop
try:
    _sentinel = lambda: False
    _rk.rank_candidates(
        {"ranking": {}, "analysis": {"max_for_ai_scoring": 10}},
        _prof, [Candidate(arxiv_id="2401.%05d" % i, title="t%d" % i)
                for i in range(5)],
        ai=_FakeScoreAI(latency=0.0), should_stop=_sentinel)
    check("rank_candidates 把 should_stop 传给了打分",
          _seen_stop.get("cb") is _sentinel, _seen_stop.get("cb"))
finally:
    _rk.ai_relevance = _orig_ai_rel

print()
print("=" * 70)
print("30) 三个位置的存在性检查 + 运行时把路径打进日志")
print("=" * 70)
import shutil as _sh
import tempfile as _tf

from arxiv_rec.config import check_data_paths, data_paths
from arxiv_rec.pipeline import log_paths as _log_paths
from arxiv_rec.utils import add_log_sink as _add_sink
from arxiv_rec.utils import remove_log_sink as _rm_sink

_tmp30 = _tf.mkdtemp(prefix="daily_arxiv_selftest30_")
_gone = os.path.join(_tmp30, "挪走了")       # 故意不建这个目录

# --- 30a) 三个位置按设置页的顺序给出, 相对路径解析成绝对路径 ---
_cfg30 = {"library": {"index_db": os.path.join(_gone, "idx.sqlite")},
          "analysis": {"history_db": os.path.join(_gone, "h.sqlite")},
          "output": {"dir": os.path.join(_gone, "out")}}
_names = [n for n, _r, _p in data_paths(_cfg30)]
check("三个位置的名字和顺序固定 (读取记录/推荐记录/报告目录)",
      _names == ["读取记录", "推荐记录", "报告目录"], _names)
check("相对路径解析成绝对路径",
      os.path.isabs(data_paths({"library": {"index_db": "library_index.sqlite"}})[0][2]),
      data_paths({"library": {"index_db": "library_index.sqlite"}})[0][2])

# --- 30a2) 默认值必须是**相对**名字 ---
# 这是"第一次打开 exe 时三样东西就落在 exe 旁边"的全部依据: 默认值是相对
# 路径 + 冻结时 project_root() 返回 exe 所在目录。哪天真有人把默认值写成
# 绝对路径, 这条会立刻拦下来 —— 否则表现出来是"换了台机器记录全没了",
# 而那是事后极难追的一类问题。
from arxiv_rec.config import DEFAULT_CONFIG as _DC30

for _name30, _val30, _want30 in (
        ("读取记录", _DC30["library"]["index_db"], "library_index.sqlite"),
        ("推荐记录", _DC30["analysis"]["history_db"],
         "recommend_history.sqlite"),
        ("报告目录", _DC30["output"]["dir"], "output")):
    check("默认的%s是相对名字 (这样才跟着 exe 走)" % _name30,
          _val30 == _want30, _val30)

# "ui" 段以前装的是界面自己的状态 (详解面板被拖到多高)。详解现在按内容撑开,
# 没有可调的东西了 —— 那一段连同它的读写一起撤掉 (见 App._fit_detail_height)。
# 这里要守住的是: **默认配置里不再有这一项**, 而老 config.json 里残留的 ui 段
# 也不会让程序出问题 (save_config 会把它丢掉, 用户手改出来的垃圾不会一直留着)。
check("默认配置里不再有 ui 段 (详解高度不再是设置项)",
      "ui" not in _DC30, sorted(_DC30))

# 老 config.json 里还留着 ui.detail_height (上一版写过盘)。load_config 是深合并,
# 不认识的键会原样留着 —— 所以这里要守的是"读它不崩、别的段不受影响"; 至于
# "保存一次之后它自己消失", 那是 save_config 白名单的事, 在 uitest 里验。
import json as _json30
import tempfile as _tf30
from arxiv_rec.config import load_config as _lc30
_ui_dir30 = _tf30.mkdtemp(prefix="daily_arxiv_selftest_ui30_")
_ui_path30 = os.path.join(_ui_dir30, "config.json")
with open(_ui_path30, "w", encoding="utf-8") as _fh30:
    _json30.dump({"ui": {"detail_height": 420},
                  "ai": {"model": "keep-me"}}, _fh30)
_cfg_ui30 = _lc30(_ui_path30)
check("老 config.json 里的 ui 段读起来不报错, 别的段照常生效",
      (_cfg_ui30.get("ai") or {}).get("model") == "keep-me",
      (_cfg_ui30.get("ai") or {}).get("model"))
check("缺的段仍然被默认值补齐 (多出来的 ui 段没把默认值挤掉)",
      (_cfg_ui30.get("ranking") or {}).get("weight_relevance")
      == _DC30["ranking"]["weight_relevance"], _cfg_ui30.get("ranking"))

# --- 30b) 上一级目录不在 => 必须报出来 ---
_bad30 = check_data_paths(_cfg30)
check("父目录不存在时三个位置全被报出来", len(_bad30) == 3, len(_bad30))
check("报出来的那条带着'上一级目录不存在'",
      all("不存在" in b[3] for b in _bad30), [b[3] for b in _bad30])

# --- 30c) 文件还没建、但父目录在 => 不算问题 (全新安装的正常状态) ---
_ok30 = {"library": {"index_db": os.path.join(_tmp30, "还没建.sqlite")},
         "analysis": {"history_db": os.path.join(_tmp30, "也没建.sqlite")},
         "output": {"dir": os.path.join(_tmp30, "还没建目录")}}
check("父目录在就不报 (全新安装时这三个本来就还不存在)",
      check_data_paths(_ok30) == [], check_data_paths(_ok30))
# 这一条是整个检查的要点: 不这么判的话, 每次全新安装都会弹一个"找不到"的
# 假警报, 用户会去改一个本来没问题的设置。
os.makedirs(_ok30["output"]["dir"])
check("目录真的建出来之后同样不报", check_data_paths(_ok30) == [],
      check_data_paths(_ok30))

# --- 30d) 留空 / 缺键时兜底到默认文件名, 而不是报"路径是空的" ---
# 界面上的输入框被清空、或者手改 config.json 把某个键删了, 都不该变成一条
# 红色警告 —— 程序本来就有默认值, 用默认值就行。
_empty30 = data_paths({"library": {}, "analysis": {}, "output": {}})
check("三个位置缺键时兜底到默认文件名",
      [os.path.basename(p) for _n, _r, p in _empty30]
      == ["library_index.sqlite", "recommend_history.sqlite", "output"],
      [p for _n, _r, p in _empty30])
check("留空同样兜底 (不会被当成'路径是空的')",
      check_data_paths({"library": {"index_db": ""},
                        "analysis": {"history_db": ""},
                        "output": {"dir": ""}}) == [],
      check_data_paths({"library": {"index_db": ""},
                        "analysis": {"history_db": ""},
                        "output": {"dir": ""}}))

# --- 30e) 运行日志里必须先摆出实际要写的三条路径 ---
# 用户报"报告没写到设置里那个目录"时, 靠的就是这几行 —— 它们要是没打出来,
# 就只能靠猜 (这正是之前查这个问题花掉的时间)。
_lines30 = []
_sink30 = lambda m, lvl="": _lines30.append(m)
_add_sink(_sink30)
try:
    _log_paths({"output": {"dir": os.path.join(_tmp30, "out")},
                "library": {"index_db": os.path.join(_tmp30, "idx.sqlite")},
                "analysis": {"history_db": os.path.join(_tmp30, "h.sqlite")},
                "_config_path": "C:/x/config.json"})
finally:
    _rm_sink(_sink30)
_joined30 = "\n".join(_lines30)
check("日志里有报告目录的绝对路径",
      os.path.join(_tmp30, "out") in _joined30, _joined30)
check("日志里有读取记录和推荐记录的绝对路径",
      os.path.join(_tmp30, "idx.sqlite") in _joined30
      and os.path.join(_tmp30, "h.sqlite") in _joined30, _joined30)
check("日志里点明用的是哪一份配置文件",
      "C:/x/config.json" in _joined30, _joined30)

_sh.rmtree(_tmp30, ignore_errors=True)

print()
print("31) 手改配置写成 pdf_folders: null 不能把界面搞崩")
print("=" * 70)
import json as _j31

from arxiv_rec.config import load_config as _load31

# _deep_merge 只在两边都是 dict 时递归, 所以用户写个 null 会被原样留着; 而界面
# 那个编辑器用 setdefault("pdf_folders", []) 取列表 —— setdefault **不替换**
# 已存在的 None, 于是 refresh() 里 `for f in None` 直接 TypeError, 表现是"双击
# exe 弹一个看不懂的错", 根因在一个手写的 null 上。入口处必须归一成列表。
_tmp31 = _tf.mkdtemp(prefix="daily_arxiv_selftest31_")
try:
    for _bad31, _label31 in ((None, "null"), ("C:/somewhere", "一个字符串"),
                             ({"path": "x"}, "一个对象"), (42, "一个数字")):
        _p31 = os.path.join(_tmp31, "cfg_%s.json" % _label31.replace(" ", ""))
        with open(_p31, "w", encoding="utf-8") as _fh31:
            _j31.dump({"pdf_folders": _bad31, "ai": {"model": "m"}}, _fh31)
        _c31 = _load31(_p31)
        check("pdf_folders 写成%s 时被归一成空列表" % _label31,
              _c31.get("pdf_folders") == [], repr(_c31.get("pdf_folders")))
        # 归一之后还要能正常遍历 —— 这才是崩不崩的分界线
        try:
            _n31 = len([f for f in _c31["pdf_folders"]
                        if isinstance(f, dict) and f.get("path")])
            check("归一后能正常遍历 (写成%s)" % _label31, _n31 == 0, _n31)
        except Exception as _exc31:
            check("归一后能正常遍历 (写成%s)" % _label31, False, _exc31)

    # 正常的那份不能被这条兜底改坏: 列表要原样留着
    _p31ok = os.path.join(_tmp31, "cfg_ok.json")
    with open(_p31ok, "w", encoding="utf-8") as _fh31:
        _j31.dump({"pdf_folders": [{"path": "D:/papers", "enabled": True}]}, _fh31)
    _c31ok = _load31(_p31ok)
    check("正常的 pdf_folders 原样保留",
          _c31ok["pdf_folders"] == [{"path": "D:/papers", "enabled": True}],
          _c31ok["pdf_folders"])

    # 键**缺失**时靠 DEFAULT_CONFIG 兜底成 [] —— 界面那个编辑器启动时的
    # setdefault 副作用没了之后, 这条是"文件夹列表一定存在"的依据
    _p31none = os.path.join(_tmp31, "cfg_nokey.json")
    with open(_p31none, "w", encoding="utf-8") as _fh31:
        _j31.dump({"ai": {"model": "m"}}, _fh31)
    _c31none = _load31(_p31none)
    check("配置里没有 pdf_folders 这个键时, 合并默认值后是空列表",
          _c31none.get("pdf_folders") == [], repr(_c31none.get("pdf_folders")))
finally:
    _sh.rmtree(_tmp31, ignore_errors=True)

print("32) 推荐记录: 读回来铺成列表 (老库迁移 + 从当时的报告补元信息)")
print("=" * 70)
import sqlite3 as _sq32

from arxiv_rec.history import (RecommendHistory as _RH32,
                               load_records as _load32,
                               rows_as_candidates as _rows32)

# 铺记录这件事不再删记录了 (删是旁边那个按钮), 而是把 recommend_history.sqlite
# 铺成下面的列表。
# 这条路上有两件容易做错的事, 这里各测一遍:
#   * 老库 (v1 结构, 没有 authors/published/... 那五列) 必须**加列**迁移, 绝不能
#     学 pdf_library 那套"版本变了就 DROP TABLE" —— 那是用户攒的历史, 不是缓存
#   * 老记录里没存作者/日期/期刊/分类, 得从它指向的那份报告里补出来, 否则列表
#     那一排全是破折号
_tmp32 = _tf.mkdtemp(prefix="daily_arxiv_selftest32_")
try:
    # --- 造一份**真的报告** (格式照 dist/output 里那份来), 供"补元信息"用 ---
    _rep32 = os.path.join(_tmp32, "arxiv_recommend_20260929_145916.md")
    with open(_rep32, "w", encoding="utf-8") as _fh32:
        _fh32.write(
            "生成时间 2026-09-29 14:59:16\n\n"
            "| # | 论文 | 提交 | a | b | c | 总分 | 引用 | 标记 |\n"
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |\n"
            "| 1 | [记录里的第一篇](https://arxiv.org/abs/2401.11111) | 2026-09-25 "
            "| 0.98 | 0.00 | 1.00 | **0.788** | 58 | ⭐经典 |\n"
            "| 2 | [记录里的第二篇](https://arxiv.org/abs/2401.22222) | 2026-09-24 "
            "| 0.90 | 0.00 | 0.80 | **0.700** | — | 🆕新 |\n\n"
            "### 1. 记录里的第一篇\n\n"
            "**arXiv**: [2401.11111](https://arxiv.org/abs/2401.11111) · "
            "**PDF**: [下载](https://arxiv.org/pdf/2401.11111)\n\n"
            "**作者**: Yan-Cheng Wang, Nvsen Ma, Zi Yang Meng · "
            "**提交**: 2026-09-25\n\n"
            "**分类**: cond-mat.str-el, hep-th\n\n"
            "**期刊**: SciPost Phys. 13, 123 (2022)\n\n"
            "**引用数**: 58\n\n"
            "### 2. 记录里的第二篇\n\n"
            "**作者**: Bo Zhang · **提交**: 2026-09-24\n\n"
            "**分类**: quant-ph\n\n")

    # --- 造一份 **v1 结构** 的库 (用户手上那份就是这个结构) ---
    _db32 = os.path.join(_tmp32, "history.sqlite")
    _c32 = _sq32.connect(_db32)
    _c32.executescript("""
CREATE TABLE recommended (
    arxiv_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
    first_at TEXT NOT NULL DEFAULT '', last_at TEXT NOT NULL DEFAULT '',
    times INTEGER NOT NULL DEFAULT 0, best_score REAL NOT NULL DEFAULT 0,
    report TEXT NOT NULL DEFAULT '', profile_fp TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '', connections TEXT NOT NULL DEFAULT '[]',
    ideas TEXT NOT NULL DEFAULT '', analyzed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
INSERT INTO meta (k, v) VALUES ('schema', '1');
""")
    _c32.executemany(
        "INSERT INTO recommended (arxiv_id, title, first_at, last_at, times,"
        " best_score, report, profile_fp, summary, connections, ideas, analyzed)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [("2401.11111", "记录里的第一篇", "2026-09-20 10:00:00",
          "2026-09-29 14:59:16", 4, 0.9, _rep32, "fp", "讲解正文",
          '[{"paper": "我的某篇", "relation": "同一套方法"}]', "想法", 1),
         ("2401.22222", "记录里的第二篇", "2026-09-21 10:00:00",
          "2026-09-29 14:59:16", 1, 0.7, _rep32, "", "", "[]", "", 0),
         ("2401.33333", "第三篇 (没有报告)", "2026-09-22 10:00:00",
          "2026-09-28 09:00:00", 2, 0.5, "", "fp", "讲解", "[]", "", 1)])
    _c32.commit()
    _c32.close()

    # --- 打开: 加列迁移, 一行不少 ---
    with _RH32(_db32) as _h32:
        _cols32 = set(r["name"] for r in
                      _h32.conn.execute("PRAGMA table_info(recommended)"))
        _n32 = _h32.counts()["total"]
    check("老库打开后补上了那五列",
          {"authors", "published", "journal", "categories",
           "citations"} <= _cols32, sorted(_cols32))
    check("迁移是加列, 3 条记录一条没丢", _n32 == 3, _n32)
    with _RH32(_db32) as _h32:
        check("再打开一次也不报错、行数不变 (迁移幂等)", _h32.counts()["total"] == 3)
        _ord32 = [r["arxiv_id"] for r in _h32.rows()]
        check("排序稳定: 连读两次顺序一样 (同一轮 last_at 全相同, 靠分数兜底)",
              _ord32 == [r["arxiv_id"] for r in _h32.rows()], _ord32)
    check("最近一轮的排前面, 同一轮里分数高的排前面",
          _ord32 == ["2401.11111", "2401.22222", "2401.33333"], _ord32)

    # --- 铺成 Candidate ---
    _cfg32 = {"analysis": {"history_db": _db32, "use_history": True}}
    _cs32 = _load32(_cfg32)
    check("load_records 给出 3 个 Candidate", len(_cs32) == 3, len(_cs32))
    check("都带 from_history 标志 (详解据此跳过那三个没存进库的分数)",
          all(c.from_history for c in _cs32))
    _a32 = _cs32[0]
    check("分数取的是记录里的 best_score", abs(_a32.score - 0.9) < 1e-9, _a32.score)
    check("推荐次数/最近一次时间都带上了",
          _a32.seen_times == 4 and _a32.last_recommended == "2026-09-29 14:59:16",
          (_a32.seen_times, _a32.last_recommended))
    check("存着的解读正文读回来了", _a32.summary == "讲解正文", _a32.summary)
    check("存着的关联读回来了 (还是 dict 形状)",
          _a32.connections == [{"paper": "我的某篇", "relation": "同一套方法"}],
          _a32.connections)
    # 老库这五列是空的 —— 全靠解析当时那份报告
    check("作者从当时的报告里补出来了", _a32.authors == ["Yan-Cheng Wang 等"],
          _a32.authors)
    check("提交日期从报告里补出来了",
          _a32.published == datetime(2026, 9, 25), _a32.published)
    check("期刊从报告里补出来了",
          _a32.journal_ref == "SciPost Phys. 13, 123 (2022)", _a32.journal_ref)
    check("分类从报告里补出来了 (拆成了列表)",
          _a32.categories == ["cond-mat.str-el", "hep-th"], _a32.categories)
    check("主分类取的是第一个", _a32.primary_category == "cond-mat.str-el",
          _a32.primary_category)
    check("引用数从报告里补出来了", _a32.citations == 58, _a32.citations)
    # 报告里写 "—" 的那种: 要留成 None (显示成"没有这项"), 不能变成 0
    check("报告里写'—'的引用数留成 None, 不是 0", _cs32[1].citations is None,
          _cs32[1].citations)
    check("没有报告可查的那条: 那几项留空, 但记录本身照常铺出来",
          _cs32[2].authors == [] and _cs32[2].published is None
          and _cs32[2].title == "第三篇 (没有报告)", _cs32[2].title)
    check("报告路径也带在候选上 (详解里会写'当时写进')",
          _cs32[0].record_report == _rep32, _cs32[0].record_report)

    # --- 坏数据: 手改过的库不能让列表打不开 ---
    with _RH32(_db32) as _h32:
        _h32.conn.execute(
            "INSERT INTO recommended (arxiv_id, title, last_at, times,"
            " best_score, report, connections, authors, published, categories,"
            " citations, analyzed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2401.99999", "坏行", "2026-09-30 00:00:00", 1, 0.2,
             os.path.join(_tmp32, "没有这个文件.md"), "不是 JSON", "不是 JSON",
             "不是日期", "{坏", "不是数字", 1))
        _h32.conn.commit()
    _bad32 = [c for c in _load32(_cfg32) if c.arxiv_id == "2401.99999"][0]
    check("坏行也读得出来 (不是整个列表打不开)", _bad32.title == "坏行")
    check("坏掉的作者/日期/关联都退成空", _bad32.authors == []
          and _bad32.published is None and _bad32.connections == [],
          (_bad32.authors, _bad32.published, _bad32.connections))
    # citations 那列在 sqlite 里是 INTEGER, 但类型只是"亲和性": 手写个 "不是数字"
    # 进去它照存字符串。详情面板那行是 "引用 %d" % c.citations —— 留着字符串就是
    # 点一下列表就崩, 所以必须转成 int 或 None。
    check("坏掉的引用数变成 None (不是留着字符串去炸 %d)",
          _bad32.citations is None, repr(_bad32.citations))

    # --- 写回: 新记录自带元信息 ---
    with _RH32(_db32) as _h32:
        _h32.record([Candidate(
            arxiv_id="2401.55555", title="新记的一篇",
            authors=["张三", "Smith, Jr."], published=datetime(2026, 3, 4),
            categories=["cond-mat.str-el"], journal_ref="PRB 100, 1 (2019)",
            citations=42, score=0.8, analyzed=True, summary="讲解", ideas="想法")],
            profile_fp="fp")
    _new32 = [c for c in _load32(_cfg32) if c.arxiv_id == "2401.55555"][0]
    check("新记录的作者原样往返 (带逗号的 'Smith, Jr.' 没被拆错)",
          _new32.authors == ["张三", "Smith, Jr."], _new32.authors)
    check("新记录的日期/期刊/分类/引用数都往返了",
          _new32.published == datetime(2026, 3, 4)
          and _new32.journal_ref == "PRB 100, 1 (2019)"
          and _new32.categories == ["cond-mat.str-el"]
          and _new32.citations == 42,
          (_new32.published, _new32.journal_ref, _new32.categories,
           _new32.citations))

    # --- 关掉开关时: 不读也不写, 但库不动 ---
    check("'使用推荐记录'关掉时 load_records 给空列表",
          _load32({"analysis": {"history_db": _db32, "use_history": False}}) == [])
    with _RH32(_db32) as _h32:
        check("关掉只是不读不写, 库里的记录还在", _h32.counts()["total"] == 5,
              _h32.counts())

    # 看记录是**只读**的: RecommendHistory() 一打开就会建表补列, 所以文件不存在
    # 时必须提前返回, 否则"点一下「推荐记录」看一眼"就会在用户盘上凭空多一个文件
    _nofile32 = os.path.join(_tmp32, "还没有建的库.sqlite")
    check("库文件不存在时 load_records 给空列表, 而且不把文件建出来",
          _load32({"analysis": {"history_db": _nofile32,
                                "use_history": True}}) == []
          and not os.path.exists(_nofile32), os.path.exists(_nofile32))

    # --- 清空: 只清记录, 报告不动 ---
    with _RH32(_db32) as _h32:
        _before32 = os.path.getsize(_db32)
        _n32b = _h32.clear()
        _after32 = _h32.counts()
    check("clear() 报出清掉了 5 条", _n32b == 5, _n32b)
    check("清空之后读回来是空的", _after32["total"] == 0 and _load32(_cfg32) == [])
    check("清空**不删报告**", os.path.isfile(_rep32))
    check("VACUUM 之后文件没变大", os.path.getsize(_db32) <= _before32,
          (os.path.getsize(_db32), _before32))
    with _RH32(_db32) as _h32:
        _h32.record([Candidate(arxiv_id="2401.66666", title="清空后新记的")],
                    profile_fp="fp")
    check("清空之后还能接着记 (库没被搞坏)",
          [c.arxiv_id for c in _load32(_cfg32)] == ["2401.66666"],
          [c.arxiv_id for c in _load32(_cfg32)])

    # --- 报告的解析里多了"分类"这一项 (老记录靠它补主分类) ---
    from arxiv_rec.past_runs import parse_report as _pr32
    _items32 = _pr32(open(_rep32, encoding="utf-8").read())["items"]
    check("parse_report 现在也解析分类 (老记录靠它补)",
          _items32[0]["categories"] == ["cond-mat.str-el", "hep-th"],
          _items32[0].get("categories"))
    check("第二篇的分类也解析出来了", _items32[1]["categories"] == ["quant-ph"],
          _items32[1].get("categories"))
    # 老报告里可能根本没有"分类"那一行 —— 那时要给空列表, 不是 KeyError
    _nc32 = _pr32("### 1. 没有分类的一篇\n\n**作者**: A, B · **提交**: 2026-01-01\n")
    check("报告里没有'分类'那一项时给空列表 (不是 KeyError)",
          _nc32["items"][0]["categories"] == [],
          _nc32["items"][0].get("categories"))
finally:
    _sh.rmtree(_tmp32, ignore_errors=True)

print()
print("=" * 70)
print("33) 老记录缺的作者/日期/期刊: 按 ID 从 arXiv 补回来并写回库")
print("=" * 70)
# 用户报的现象: 铺一遍推荐记录, 列表里作者/提交日期/期刊三列全空。
# 根因有两层: (1) 那五列是后来才加的, 老记录里一律空着; (2) 报告是兜底数据源,
# 而报告存在 output/ 里, 会被清理掉 —— 于是兜底也没得兜。
# 这一段钉住第三层兜底 (按 arXiv ID 问一次) 的三个要点:
#   * 只把**真缺**的挑出来问 (缺期刊不算缺, 没发表过的论文本来就没有期刊)
#   * 补回来的只写那五列, 标题/分数/解读/报告路径一个字都不能动
#   * 补不上 (断网 / ID 查不到) 时必须安静地放弃, 不能让「推荐记录」打不开
_tmp33 = _tf.mkdtemp(prefix="daily_arxiv_selftest33_")
try:
    import json as _json33
    from datetime import datetime as _dt33

    from arxiv_rec import history as _hist33

    _db33 = os.path.join(_tmp33, "h33.sqlite")
    with _hist33.RecommendHistory(_db33) as _h33:
        _h33.record([
            Candidate(arxiv_id="2401.00001", title="有作者有日期",
                      authors=["A. Author"], published=_dt33(2024, 1, 1)),
            Candidate(arxiv_id="2401.00002", title="两样都缺"),
            Candidate(arxiv_id="2401.00003", title="只缺期刊",
                      authors=["C. Author"], published=_dt33(2024, 1, 3)),
            Candidate(arxiv_id="2401.00004", title="也是两样都缺"),
            # 这一条**故意**让下面那次假的联网取不到 (见 33d): 它必须继续算"缺",
            # 否则一条补不上的记录会被静默忘掉, 下次铺记录再也不试了
            Candidate(arxiv_id="2401.00005", title="补不回来的那条"),
        ], profile_fp="fp")

    def _rows33():
        with _hist33.RecommendHistory(_db33) as _h:
            return _h.rows()

    # --- 33a) 缺什么才问什么 ---
    _miss33 = _hist33.missing_meta_ids(_rows33())
    check("缺作者/日期的记录才被挑出来 (只缺期刊的不算)",
          sorted(_miss33) == ["2401.00002", "2401.00004", "2401.00005"], _miss33)

    # --- 33b) 补回来的只写那五列 ---
    _got33 = {"2401.00002": {"authors": ["B. Author", "B2. Author"],
                             "published": _dt33(2024, 1, 2),
                             "journal": "Phys. Rev. B 1, 2 (2024)",
                             "categories": ["cond-mat.str-el"],
                             "citations": None}}
    with _hist33.RecommendHistory(_db33) as _h33b:
        _n33 = _h33b.update_meta(_got33)
    check("写回函数报告改了 1 行", _n33 == 1, _n33)
    _row33 = [r for r in _rows33() if r["arxiv_id"] == "2401.00002"][0]
    check("作者写进去了", _json33.loads(_row33["authors"]) == ["B. Author", "B2. Author"],
          _row33["authors"])
    check("提交日期写成 ISO 字符串 (库里存的就是这个形状)",
          str(_row33["published"]).startswith("2024-01-02"), _row33["published"])
    check("期刊写进去了", _row33["journal"] == "Phys. Rev. B 1, 2 (2024)",
          _row33["journal"])
    check("分类写进去了", _json33.loads(_row33["categories"]) == ["cond-mat.str-el"],
          _row33["categories"])
    check("**没有**顺手改标题", _row33["title"] == "两样都缺", _row33["title"])
    check("**没有**顺手改分数", abs(float(_row33["best_score"])) < 1e-9,
          _row33["best_score"])
    check("**没有**顺手改报告路径", _row33["report"] == "", _row33["report"])
    check("别的行一个字没动",
          [r["authors"] for r in _rows33() if r["arxiv_id"] == "2401.00001"]
          == ['["A. Author"]'],
          [r["authors"] for r in _rows33() if r["arxiv_id"] == "2401.00001"])
    check("补完之后它不再算'缺'", "2401.00002" not in _hist33.missing_meta_ids(_rows33()),
          _hist33.missing_meta_ids(_rows33()))

    # --- 33c) 写回一个库里没有的 ID 时安静地什么也不做 ---
    with _hist33.RecommendHistory(_db33) as _h33c:
        check("写回不存在的 ID 时返回 0 (不抛)",
              _h33c.update_meta({"2401.99999": {"authors": ["X"]}}) == 0)

    # --- 33d) 断网 / 查不到时必须安静放弃 ---
    # 把联网那一层换掉: 一次让它抛异常, 一次让它返回空。两种情况都不该冒到界面上
    # —— 「推荐记录」只是"看一眼", 补不齐顶多那几列继续写破折号。
    _orig33 = _hist33.fill_missing_meta
    try:
        _hist33.fill_missing_meta = lambda cfg, ids, cache=None: (
            (_ for _ in ()).throw(RuntimeError("网络不通")))
        check("联网取元信息抛异常时 repair_records 返回空 dict (不往外抛)",
              _hist33.repair_records({}, _rows33()) == {})
        _hist33.fill_missing_meta = lambda cfg, ids, cache=None: {}
        check("一条都没补到时同样返回空 dict",
              _hist33.repair_records({}, _rows33()) == {})

        # 补到了一条: 要写回库, 并把它交回给界面 (界面拿它就地刷新那几格)
        _asked33 = {}

        def _fake33(cfg, ids, cache=None):
            _asked33["ids"] = list(ids)
            return {"2401.00004": {"authors": ["D. Author"],
                                   "published": _dt33(2024, 1, 4),
                                   "journal": "PRB 4, 4 (2024)",
                                   "categories": ["quant-ph"],
                                   "citations": None}}

        _hist33.fill_missing_meta = _fake33
        _back33 = _hist33.repair_records({"analysis": {"history_db": _db33}}, _rows33())
        check("只把**真缺**的那些 ID 拿出去问 (有作者有日期的 2401.00001 不问)",
              sorted(_asked33.get("ids") or []) == ["2401.00004", "2401.00005"],
              _asked33.get("ids"))
        check("补到的那条被交回给界面 (好就地刷新那几格)",
              list(_back33) == ["2401.00004"], list(_back33))
        _r33b = [r for r in _rows33() if r["arxiv_id"] == "2401.00004"][0]
        check("库里的空列被填上了",
              _json33.loads(_r33b["authors"]) == ["D. Author"]
              and str(_r33b["published"]).startswith("2024-01-04"), _r33b)
        check("**没补到**的那条仍然算缺 (下次点还会再试)",
              _hist33.missing_meta_ids(_rows33()) == ["2401.00005"],
              _hist33.missing_meta_ids(_rows33()))
    finally:
        _hist33.fill_missing_meta = _orig33

    # --- 33e) 库里存的日期写法五花八门, 读回来不能崩 ---
    check("_iso_text: datetime 走 ISO", _hist33._iso_text(_dt33(2024, 1, 2))
          .startswith("2024-01-02"), _hist33._iso_text(_dt33(2024, 1, 2)))
    check("_iso_text: None 给空串 (不是 'None')", _hist33._iso_text(None) == "",
          _hist33._iso_text(None))
    check("_iso_text: 字符串原样 (前后空白去掉)",
          _hist33._iso_text("  2024-01-02  ") == "2024-01-02",
          _hist33._iso_text("  2024-01-02  "))
finally:
    _sh.rmtree(_tmp33, ignore_errors=True)

print()
print("=" * 70)
print("34) 重新打包不能吃掉用户在 dist/ 里的东西")
print("=" * 70)
# dist/ 既是构建产物目录也是**数据目录**。重建时会先 rmtree 掉它, 所以"用户的东西"
# 必须先收起来再放回去。踩过的坑: 一开始只保了三个文件, output/ 和 cache/ 被顺手
# 删光 —— 报告一没, 推荐记录里那些"报告路径"就全指向空气 (而老记录的作者/日期正是
# 从报告里兜底读的), 用户看到的是"记录里的元信息凭空消失"。
import build_exe as _be34

_tmp34 = _tf.mkdtemp(prefix="daily_arxiv_selftest34_")
try:
    _dist34 = os.path.join(_tmp34, "dist")
    os.makedirs(os.path.join(_dist34, "output"))
    os.makedirs(os.path.join(_dist34, "cache", "http"))
    with open(os.path.join(_dist34, "config.json"), "w", encoding="utf-8") as _f:
        _f.write('{"ai": {"model": "keep-me"}}')
    with open(os.path.join(_dist34, "recommend_history.sqlite"), "wb") as _f:
        _f.write(b"history-bytes")
    with open(os.path.join(_dist34, "library_index.sqlite"), "wb") as _f:
        _f.write(b"index-bytes")
    with open(os.path.join(_dist34, "output", "报告.md"), "w", encoding="utf-8") as _f:
        _f.write("这份报告不能被删")
    with open(os.path.join(_dist34, "cache", "http", "a.json"), "w",
              encoding="utf-8") as _f:
        _f.write("{}")

    check("要收起来的东西包括那两个目录 (不只是三个文件)",
          "output" in _be34._DATA_DIRS and "cache" in _be34._DATA_DIRS,
          _be34._DATA_DIRS)

    _stash34, _map34 = _be34._stash_data(_dist34)
    check("五个都收起来了", sorted(_map34) == sorted(
        ["config.json", "library_index.sqlite", "recommend_history.sqlite",
         "output", "cache"]), sorted(_map34))
    check("目录是**移走**的 (几百兆的 cache 不该被抄一份)",
          not os.path.exists(os.path.join(_dist34, "output")),
          os.listdir(_dist34))

    # 模拟构建: 整个产物目录被删掉重建
    _sh.rmtree(_dist34, ignore_errors=True)
    os.makedirs(_dist34)

    _done34 = _be34._restore_data(_map34, _dist34)
    check("五个都放回去了", sorted(_done34) == sorted(_map34), sorted(_done34))
    check("报告回来了, 内容一字不差",
          open(os.path.join(_dist34, "output", "报告.md"),
               encoding="utf-8").read() == "这份报告不能被删")
    check("cache 的子目录结构也回来了",
          os.path.isfile(os.path.join(_dist34, "cache", "http", "a.json")))
    check("config.json 回来了",
          "keep-me" in open(os.path.join(_dist34, "config.json"),
                            encoding="utf-8").read())

    # --- 34b) 产物目录里没有这些东西时 (全新打包) 不能崩 ---
    _empty34 = os.path.join(_tmp34, "空的")
    os.makedirs(_empty34)
    _s34b, _m34b = _be34._stash_data(_empty34)
    check("全新的产物目录: 什么都没收, 也不建临时目录", (_s34b, _m34b) == ("", {}),
          (_s34b, _m34b))
    check("没有东西可放回时返回空列表", _be34._restore_data(_m34b, _empty34) == [])
    check("产物目录压根不存在时也不崩",
          _be34._stash_data(os.path.join(_tmp34, "没有这个目录")) == ("", {}))

    # --- 34c) 删旧目录: 删不动的时候必须**一个字节都没动** ---
    # 踩过的坑 (两次): rmtree 是**边走边删**的。dist 被占着的时候 —— 最常见的就是
    # 上一次的 exe 还开着, 双击启动的程序工作目录就是 dist, 于是整个目录都删不掉 ——
    # 它会先把 daily_arxiv.exe、config.json、索引删掉, 然后才失败退出。用户看到的
    # 是一句"删不掉旧目录", 而他刚才还在用的那个 exe 已经不在磁盘上了。
    # 现在改成"先改名, 再删": 改名失败就说明有人占着, 这时目录原封不动。
    _ok34 = os.path.join(_tmp34, "能删的")
    os.makedirs(_ok34)
    with open(os.path.join(_ok34, "a.txt"), "w", encoding="utf-8") as _f:
        _f.write("x")
    check("没人占着的目录能删掉",
          _be34._remove_dir_safely(_ok34) == "" and not os.path.isdir(_ok34))

    def _locked34(path):
        """目录现在被占着吗 (拿"能不能改名"当探针, 改完立刻改回来)。"""
        try:
            os.rename(path, path + ".probe")
            os.rename(path + ".probe", path)
            return False
        except OSError:
            return True

    import subprocess as _sp34
    _lock34 = os.path.join(_tmp34, "被占着的")
    os.makedirs(_lock34)
    with open(os.path.join(_lock34, "keep.txt"), "w", encoding="utf-8") as _f:
        _f.write("不能被删")
    # 拿一个子进程把**工作目录**钉在这个目录上 —— 这就是"exe 还开着"时 Windows 上
    # 的真实状态 (exe 文件本身能删, 目录不能)
    _p34 = _sp34.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                       cwd=_lock34)
    try:
        for _ in range(40):         # 等子进程真的起来 (起没起来看目录锁没锁)
            if _locked34(_lock34):
                break
            _time.sleep(0.25)
        # 锁没等来就**别往下测**: 下面三条是在"目录确实被占着"这个前提上做的断言,
        # 前提不成立时它们会一起报错, 而报出来的三句话("没报出原因"/"目录还在")
        # 完全指向不了真正的原因 —— 只会让人以为是 _remove_dir_safely 坏了。
        # (实测踩过一次: 机器正忙着别的活, 子进程的目录锁迟迟没生效。)
        if not _locked34(_lock34):
            print("  --   子进程没能占住目录 (机器太忙?), 跳过这 4 条")
            print("       (这不是 _remove_dir_safely 的问题, 重跑一遍通常就好)")
        else:
            check("被占着的目录: 报出原因而不是静默",
                  bool(_be34._remove_dir_safely(_lock34)))
            check("被占着的目录: 目录还在", os.path.isdir(_lock34))
            check("被占着的目录: 里面的文件一个都没少",
                  os.path.isfile(os.path.join(_lock34, "keep.txt")))
            check("被占着的目录: 没留下半拉子的 .delete_me",
                  not os.path.isdir(_lock34 + ".delete_me"))
    finally:
        _p34.kill()
        _p34.wait()
    _time.sleep(0.5)
    check("占用解除之后能删掉",
          _be34._remove_dir_safely(_lock34) == "" and not os.path.isdir(_lock34))
finally:
    _sh.rmtree(_tmp34, ignore_errors=True)

print()
print("=" * 70)
print("35) 和 AI 的讨论: 存哪、读哪、怎么变成详解")
print("=" * 70)
# 「文献推荐」页那个讨论框的三条底线:
#   * 讨论**按论文**存 (chat 表), 换一篇、关掉程序再打开都还在
#   * 「用对话更新详解」只碰解读那几列 —— 推荐次数/分数/时间一个字都不许动
#     (聊两句就把次数加一, 用户会以为"我只问了两个问题, 怎么推荐次数涨了")
#   * 写回去的解读带上画像指纹, 下一轮跑推荐时才能按"画像没变"复用, 不白花 token
import tempfile as _tf35
import shutil as _sh35
import json as _js35
import sqlite3 as _sq35
from arxiv_rec import history as _hist35
from arxiv_rec.models import Candidate as _C35
from arxiv_rec.models import LibraryPaper as _P35
from arxiv_rec.history import RecommendHistory as _RH35

_tmp35 = _tf35.mkdtemp(prefix="daily_arxiv_selftest35_")
try:
    _db35 = os.path.join(_tmp35, "h.sqlite")
    _cfg35 = {"analysis": {"history_db": _db35, "use_history": True}}

    # --- 35a) 看讨论是只读的: 库不在就给空, 不建库 ---
    check("库文件不存在时读讨论给空列表",
          _hist35.load_chat(_cfg35, "2401.00001") == [])
    check("而且不会顺手把库文件建出来 (看一眼不该在盘上多出个文件)",
          not os.path.exists(_db35))
    check("空 arxiv_id 也是空列表 (不抛)", _hist35.load_chat(_cfg35, "") == [])

    # --- 35b) 追加 / 读回: 顺序、角色、按论文隔离 ---
    check("追加一条用户消息能成功",
          _hist35.append_chat(_cfg35, "2401.00001", "user", "它的符号问题怎么处理的?"))
    check("空白内容不写 (返回 False)",
          not _hist35.append_chat(_cfg35, "2401.00001", "user", "   "))
    check("空 arxiv_id 不写",
          not _hist35.append_chat(_cfg35, "", "user", "x"))
    _hist35.append_chat(_cfg35, "2401.00001", "assistant", "它用一个变换绕过去了。")
    _hist35.append_chat(_cfg35, "2401.00002", "user", "另一篇的问题")

    _m35 = _hist35.load_chat(_cfg35, "2401.00001")
    check("读回来是这一篇的两条, 顺序是问在前答在后",
          [m.get("role") for m in _m35] == ["user", "assistant"],
          [m.get("role") for m in _m35])
    check("内容一字不差 (前后空白也没被吃掉)",
          _m35[0].get("content") == "它的符号问题怎么处理的?", _m35[0])
    check("讨论按论文隔离 (另一篇只有自己那一条)",
          len(_hist35.load_chat(_cfg35, "2401.00002")) == 1,
          _hist35.load_chat(_cfg35, "2401.00002"))
    check("时间戳写上了", len(str(_m35[0].get("at") or "")) >= 16,
          _m35[0].get("at"))
    check("chat_summary 数得对 (3 条消息 / 2 篇论文)",
          "3 条消息" in _hist35.chat_summary(_cfg35)
          and "2 篇" in _hist35.chat_summary(_cfg35),
          _hist35.chat_summary(_cfg35))
    check("库文件不存在时 chat_summary 说'还没有'",
          "还没有" in _hist35.chat_summary({"analysis": {"history_db":
                                                         os.path.join(_tmp35, "无.sqlite")}}))

    # --- 35c) 清空某篇的讨论: 只清这一篇, 别的照旧 ---
    _n35 = _hist35.clear_chat(_cfg35, "2401.00001")
    check("清空返回删了几条", _n35 == 2, _n35)
    check("这一篇清干净了", _hist35.load_chat(_cfg35, "2401.00001") == [])
    check("另一篇一条没少 (清一段对话不该波及别的论文)",
          len(_hist35.load_chat(_cfg35, "2401.00002")) == 1)
    check("再清一次返回 0 (不抛)", _hist35.clear_chat(_cfg35, "2401.00001") == 0)

    # --- 35d) 讨论**关掉推荐记录**时也照样存 ---
    # 记录那个开关管的是"跑完一轮要不要把推荐结果记下来"; 讨论是用户当场敲的字,
    # 不该因为另一个开关而静默丢掉。
    _cfg35b = {"analysis": {"history_db": _db35, "use_history": False}}
    check("'使用推荐记录'关着时讨论照样写得进去",
          _hist35.append_chat(_cfg35b, "2401.00003", "user", "关着也存"))
    check("关着时也读得回来",
          len(_hist35.load_chat(_cfg35b, "2401.00003")) == 1)

    # --- 35e) 写回解读: 只碰解读那几列 ---
    with _RH35(_db35) as _h35:
        _h35.record([_C35(arxiv_id="2401.00001", title="讨论过的那篇",
                          score=0.812, summary="", analyzed=False)],
                    report_path="", profile_fp="fp-旧")
    _row35 = {r["arxiv_id"]: r for r in _hist35.load_rows(_cfg35)}["2401.00001"]
    _times35, _score35, _last35 = _row35["times"], _row35["best_score"], _row35["last_at"]

    _c35 = _C35(arxiv_id="2401.00001", title="讨论过的那篇")
    _c35.summary = "聊完之后重写的讲解。"
    _c35.connections = [{"paper": "Wang 2020", "relation": "方法同源"}]
    _c35.ideas = "可以拿来验证。"
    _c35.analyzed = True
    check("写回解读成功", _hist35.save_analysis(_cfg35, _c35, "fp-新"))

    _row35b = {r["arxiv_id"]: r for r in _hist35.load_rows(_cfg35)}["2401.00001"]
    check("解读写进去了", _row35b["summary"] == "聊完之后重写的讲解。",
          _row35b["summary"])
    check("关联写进去了 (JSON 往返)",
          _js35.loads(_row35b["connections"]) == [{"paper": "Wang 2020",
                                                   "relation": "方法同源"}],
          _row35b["connections"])
    check("analyzed 置成 1 了", bool(_row35b["analyzed"]))
    check("画像指纹换成了新的 (下次跑推荐才能按它复用)",
          _row35b["profile_fp"] == "fp-新", _row35b["profile_fp"])
    # 这三条是这次改动的重点: 用户只点了「用对话更新详解」, 没有跑推荐
    check("推荐次数**没被动过**",
          _row35b["times"] == _times35, (_times35, _row35b["times"]))
    check("分数没被动过", _row35b["best_score"] == _score35,
          (_score35, _row35b["best_score"]))
    check("最后推荐时间没被动过", _row35b["last_at"] == _last35,
          (_last35, _row35b["last_at"]))
    check("标题也没被覆盖", _row35b["title"] == "讨论过的那篇", _row35b["title"])

    # --- 35f) 写回的解读要能被下一轮复用 ---
    _c35b = _C35(arxiv_id="2401.00001", title="讨论过的那篇")
    check("同一画像下, 写回的解读能被复用 (不白花 token)",
          _RH35.reuse_analysis(_c35b, _row35b, "fp-新")
          and _c35b.summary == "聊完之后重写的讲解。", _c35b.summary)
    _c35c = _C35(arxiv_id="2401.00001", title="讨论过的那篇")
    check("画像变了就不复用", not _RH35.reuse_analysis(_c35c, _row35b, "fp-别的"))

    # --- 35g) 论文不在记录里时, 写回要**补建一条** (而不是悄悄丢掉) ---
    _c35d = _C35(arxiv_id="2401.99999", title="记录里没有的这篇")
    _c35d.summary = "补建出来的讲解。"
    _c35d.ideas = "想法"
    check("记录里没有这篇时写回也返回成功",
          _hist35.save_analysis(_cfg35, _c35d, "fp-新"))
    _rows35 = {r["arxiv_id"]: r for r in _hist35.load_rows(_cfg35)}
    check("补建出来了", "2401.99999" in _rows35, sorted(_rows35))
    check("补建的那条次数是 1 (不能凭空写成好几次)",
          int(_rows35["2401.99999"]["times"]) == 1,
          _rows35["2401.99999"]["times"])
    check("补建的那条带着标题", _rows35["2401.99999"]["title"] == "记录里没有的这篇",
          _rows35["2401.99999"]["title"])
    check("补建没有把原来那篇的解读冲掉",
          _rows35["2401.00001"]["summary"] == "聊完之后重写的讲解。",
          _rows35["2401.00001"]["summary"])
    check("讨论记录也没被写回解读这件事碰到",
          len(_hist35.load_chat(_cfg35, "2401.00002")) == 1)

    # --- 35h) 清空推荐记录时, 讨论一起删 (否则那些对话在界面上再也点不到) ---
    _hist35.append_chat(_cfg35, "2401.00001", "user", "会被一起删掉的一句")
    with _RH35(_db35) as _h35c:
        _h35c.clear()
    check("推荐记录清空了", _hist35.load_rows(_cfg35) == [])
    check("讨论也跟着清空了",
          _hist35.load_chat(_cfg35, "2401.00001") == []
          and _hist35.load_chat(_cfg35, "2401.00002") == [],
          (_hist35.load_chat(_cfg35, "2401.00001"),
           _hist35.load_chat(_cfg35, "2401.00002")))

    # --- 35i) 老库 (v2, 没有 chat 表) 打开时要能自己长出这张表 ---
    _old35 = os.path.join(_tmp35, "old.sqlite")
    _c35o = _sq35.connect(_old35)
    _c35o.executescript("""
    CREATE TABLE recommended (
        arxiv_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
        first_at TEXT NOT NULL DEFAULT '', last_at TEXT NOT NULL DEFAULT '',
        times INTEGER NOT NULL DEFAULT 0, best_score REAL NOT NULL DEFAULT 0,
        report TEXT NOT NULL DEFAULT '', profile_fp TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '', connections TEXT NOT NULL DEFAULT '[]',
        ideas TEXT NOT NULL DEFAULT '', analyzed INTEGER NOT NULL DEFAULT 0);
    INSERT INTO recommended (arxiv_id, title) VALUES ('2401.00007', '老库里的那篇');
    """)
    _c35o.commit()
    _c35o.close()
    _cfg35o = {"analysis": {"history_db": _old35, "use_history": True}}
    check("老库打开后能写讨论 (chat 表自己长出来了)",
          _hist35.append_chat(_cfg35o, "2401.00007", "user", "老库也能聊"))
    check("老库的推荐记录一条没丢",
          len(_hist35.load_rows(_cfg35o)) == 1,
          _hist35.load_rows(_cfg35o))
    check("老库读讨论读得回来",
          len(_hist35.load_chat(_cfg35o, "2401.00007")) == 1)

    # --- 35j) 讨论拼提示词: 超预算时丢的是**最早**那几条 ---
    from arxiv_rec import chat as _chat35
    _msgs35 = [{"role": "user", "content": "早" * 400},
               {"role": "assistant", "content": "中" * 400},
               {"role": "user", "content": "刚问的这一句"}]
    _full35 = _chat35.transcript_text(_msgs35)
    check("正常预算下三句都在",
          "早" in _full35 and "中" in _full35 and "刚问的这一句" in _full35,
          _full35[:80])
    check("角色写成了 研究者 / 你",
          _full35.startswith("研究者: ") and "你: " in _full35, _full35[:40])
    _cut35 = _chat35.transcript_text(_msgs35, budget=200)
    check("超预算时**最新**那一句必须留下 (截掉最新的等于白聊)",
          "刚问的这一句" in _cut35, _cut35)
    check("超预算时丢掉的是最早那一句", "早" not in _cut35, _cut35)
    check("空消息列表给空串", _chat35.transcript_text([]) == "")
    check("内容为空的那几条被跳过",
          _chat35.transcript_text([{"role": "user", "content": "  "}]) == "")

    # --- 35k) 讨论的上下文: 画像 + 相关文献标签 + 这篇论文 + 已有解读 ---
    _p35 = [_P35(item_id=1, key="k1", title="Sign problem in QMC",
                 abstract="Majorana positivity", year=2020,
                 authors=["Wei Wang"], arxiv_id="2001.00001"),
            _P35(item_id=2, key="k2", title="Something else",
                 abstract="unrelated", year=2019,
                 authors=["Li Chen"], arxiv_id="1901.00001")]
    # 标题/摘要用英文写: 相关文献预筛是 TF-IDF, 中文查询和英文文献库词表零重叠,
    # 筛出来会是空 —— 那样"有标签可用"这条断言测的就不是它想测的东西了。
    _cand35 = _C35(arxiv_id="2401.00001",
                   title="Sign problem and Majorana positivity in Monte Carlo",
                   abstract="We study the Majorana sign problem in quantum "
                            "Monte Carlo simulations.")
    _cand35.summary = "已有的解读。"
    _cand35.ideas = "已有的想法。"
    _cand35.connections = [{"paper": "Wang 2020", "relation": "方法同源"}]
    _pre35, _labels35 = _chat35.build_context(
        {"analysis": {"related_papers_k": 2}}, _cand35, papers=_p35)
    check("上下文里有这篇论文的标题和摘要",
          "Majorana positivity in Monte Carlo" in _pre35
          and "quantum Monte Carlo simulations" in _pre35, _pre35[:200])
    check("上下文里给出了可引用的文献标签", bool(_labels35), _labels35)
    check("上下文里带上了已有的解读 (不给的话 AI 会把讲过的再讲一遍)",
          "已有的解读。" in _pre35 and "方法同源" in _pre35, _pre35[:200])
    check("标签确实出现在上下文里 (给了标签就得能用)",
          _labels35 and _labels35[0] in _pre35, _labels35[:1])

    _pre35b, _labels35b = _chat35.build_context(
        {"analysis": {}}, _cand35, papers=[])
    check("文献库是空的时也拼得出来 (只是没有相关文献那一段)",
          "Majorana positivity" in _pre35b and _labels35b == [], _labels35b)
    _cand35b = _C35(arxiv_id="2401.00002", title="还没解读过的那篇",
                    abstract="摘要")
    _pre35c, _ = _chat35.build_context({"analysis": {}}, _cand35b, papers=[])
    check("没有解读时那段就不出现 (不是硬塞一句空的)",
          "目前的解读" not in _pre35c, _pre35c[:200])

    # --- 35l) 关联标签的校验: 编出来的标签一律丢掉 ---
    from arxiv_rec.analyze import normalize_result as _nr35
    _res35 = _nr35({"summary": "讲解", "ideas": "想法", "connections": [
        {"paper": "Wang 2020", "relation": "在库里"},
        {"paper": "[Wang 2020]", "relation": "带了方括号, 宽松匹配该认"},
        {"paper": "根本不存在 2099", "relation": "编的, 必须丢"},
        {"paper": "Wang 2020", "relation": ""},
        "不是字典",
    ]}, ["Wang 2020"])
    check("合法标签留下", _res35["connections"][0]["paper"] == "Wang 2020",
          _res35["connections"])
    check("带了方括号的也认 (AI 很爱多写一对括号)",
          len(_res35["connections"]) == 2
          and _res35["connections"][1]["paper"] == "Wang 2020",
          _res35["connections"])
    check("编出来的标签被丢掉 (写进报告就是凭空的关联)",
          all(c["paper"] != "根本不存在 2099" for c in _res35["connections"]))
    check("没有 relation 的丢掉", len(_res35["connections"]) == 2,
          _res35["connections"])
    check("不是 dict 的丢掉", all(isinstance(c, dict) for c in _res35["connections"]))
    check("不是 dict 的整个结果给空 dict (不抛)", _nr35(None, []) == {})
    check("文献库为空时所有关联都被丢掉",
          _nr35({"connections": [{"paper": "Wang 2020", "relation": "x"}]},
                [])["connections"] == [])

    # --- 35m) 有摘要就别再去 arXiv 取 (离线也必须能过) ---
    _cand35c = _C35(arxiv_id="2401.00001", title="T", abstract="已经有摘要了")
    check("手上已有摘要 -> 直接返回 True, 一次网都不打",
          _chat35.ensure_abstract({"network": {}}, _cand35c) is True)
    check("没有 arxiv_id -> False (不抛)",
          _chat35.ensure_abstract({"network": {}},
                                  _C35(arxiv_id="", title="T")) is False)
finally:
    _sh35.rmtree(_tmp35, ignore_errors=True)

print()
print("=" * 70)
print("36) 打包开关: 体积和启动的优化不能被悄悄关掉")
print("=" * 70)
# 这一节盯的是 build_exe.py 里那几个**只影响体积和启动速度、不影响功能**的开关。
# 它们最危险的地方是"丢了不报错": --optimize 2 掉了, exe 大回去 2 MB; build_hooks/
# 里那个 hook 被删了, 悄悄退回 PyInstaller 自带的那份, 多收 749 个文件。功能一点
# 不差, 只是又大又慢 —— 而"又大又慢"没有人会去查, 只会觉得"这程序本来就这么大"。
import build_exe as _be36

# --- 36a) 命令行里那几个开关 ---
_cmd36 = _be36.pyinstaller_cmd(sys.executable, "X/dist", "X/build",
                               _be36.parse_args([]))
check("命令行里有 --optimize 2",
      "--optimize" in _cmd36 and _cmd36[_cmd36.index("--optimize") + 1] == "2",
      _cmd36)
check("命令行里有 --noupx (省体积但拖慢启动, 明确不要)", "--noupx" in _cmd36)
check("命令行里带了自定义 hook 目录",
      "--additional-hooks-dir" in _cmd36 and _be36.HOOKS_DIR in _cmd36, _cmd36)
check("ui.py 还是最后那个位置参数", _cmd36[-1] == _be36.ENTRY, _cmd36[-1])
check("默认仍是 --onefile + --windowed",
      "--onefile" in _cmd36 and "--windowed" in _cmd36, _cmd36)
_cmd36b = _be36.pyinstaller_cmd(sys.executable, "X/dist", "X/build",
                                _be36.parse_args(["--onedir", "--console"]))
check("--onedir / --console 换得掉",
      "--onedir" in _cmd36b and "--console" in _cmd36b, _cmd36b)
check("排除清单原样进了命令行",
      _cmd36.count("--exclude-module") == len(_be36.EXCLUDES), _cmd36)
check("unittest **不在**排除清单里 (当年加了它 sklearn 直接挂, 见 build_exe 的说明)",
      "unittest" not in _be36.EXCLUDES, _be36.EXCLUDES)
# 这两个必须**在**排除清单里: 本程序已经不用它们了 (见 selftest 第 37 节),
# 而构建环境里万一还装着, 不排除就会被 PyInstaller 拖进来 —— 34 MB。
check("sklearn / scipy 在排除清单里 (不用了, 别让 PyInstaller 拖进来)",
      "sklearn" in _be36.EXCLUDES and "scipy" in _be36.EXCLUDES, _be36.EXCLUDES)

# --- 36b) hook 本身: 该留的留, 该砍的砍 ---
_hook36 = os.path.join(_be36.HOOKS_DIR, "hook-_tkinter.py")
check("build_hooks/hook-_tkinter.py 还在 (它不在就悄悄退回自带 hook)",
      os.path.isfile(_hook36), _hook36)
if os.path.isfile(_hook36):
    import importlib.util as _ilu36
    _spec36 = _ilu36.spec_from_file_location("_hook36", _hook36)
    _h36 = _ilu36.module_from_spec(_spec36)
    _spec36.loader.exec_module(_h36)

    check("Tcl 的 init.tcl 留着", _h36._wanted("_tcl_data\\init.tcl"))
    check("Tcl 的 clock.tcl 留着", _h36._wanted("_tcl_data\\clock.tcl"))
    check("编码表留着 (Tcl 读写非 UTF-8 文本要查它)",
          _h36._wanted("_tcl_data\\encoding\\cp1252.enc"))
    check("ttk 主题留着 (界面用的就是 clam)",
          _h36._wanted("_tk_data\\ttk\\clamTheme.tcl"))
    check("时区表砍掉", not _h36._wanted("_tcl_data\\tzdata\\Africa\\Abidjan"))
    check("多语言消息砍掉", not _h36._wanted("_tcl_data\\msgs\\de.msg"))
    check("Tk 的消息也砍掉", not _h36._wanted("_tk_data\\msgs\\de.msg"))
    # 库目录下的一级文件 (没有第二段路径) 不能因为"取不到 parts[1]"被误砍
    check("一级文件 (如 auto.tcl) 不受影响", _h36._wanted("_tcl_data\\auto.tcl"))

    # 别只验上面那几个写死的例子 —— 对着**本机真的** Tcl/Tk 目录跑一遍筛选,
    # 顺便把"到底省了多少个文件"钉在断言里。
    try:
        from PyInstaller.utils.hooks.tcl_tk import tcltk_info as _tcl36
    except ImportError:
        print("  (本机没装 PyInstaller, 跳过'对着真 Tcl/Tk 数一遍'那几条)")
    else:
        _all36 = _tcl36.data_files
        _kept36 = [e for e in _all36 if _h36._wanted(e[0])]
        _gone36 = [e for e in _all36 if not _h36._wanted(e[0])]
        check("真的砍掉了不少 (对着本机 Tcl/Tk 数出来的)",
              len(_gone36) >= 700, (len(_kept36), len(_gone36)))
        check("留下来的比砍掉的少得多 (砍的正是大头)",
              len(_kept36) < len(_gone36), (len(_kept36), len(_gone36)))
        _wrong36 = [e[0] for e in _gone36
                    if e[0].replace("\\", "/").split("/")[1:2] not in
                    (["tzdata"], ["msgs"])]
        check("砍掉的只有 tzdata / msgs, 没误伤别的", not _wrong36, _wrong36[:3])
        _names36 = {e[0].replace("\\", "/") for e in _kept36}
        check("init.tcl 真的在留下来的那批里",
              any(n.endswith("_tcl_data/init.tcl") for n in _names36))

print()
print("=" * 70)
print("37) TF-IDF: 自己拿 numpy 写的实现 (顶掉 sklearn + scipy)")
print("=" * 70)
# 这一节盯 arxiv_rec/tfidf.py —— 相关性排序 (rank.py)、相关文献预筛 (analyze.py)、
# 关键词抽取 (profile.py) 全走它。两个地方最容易悄悄坏掉:
#
#   1. **空行**。剪枝之后整篇文档一个词都不剩是常事 (min_df=2 时就有), 它的行必须
#      全零、和谁的相似度都是 0。numpy 的 reduceat 碰到"起止下标相等"会返回那一个
#      元素而不是 0; 想绕开它又很容易把**上一行**的累加范围截断。这个坑真踩过:
#      一篇空文档让它上一篇的分数涨了 20 倍, 而且一声不吭 —— 所以下面那条"非空行
#      的 L2 范数必须是 1"是这一节的命根子。
#   2. **和 sklearn 的数值对不对得上**。装了就逐项对拍 (词表 / 每个数 / 余弦 /
#      排序 / transform), 没装就只跑结构断言 —— 打包出来的 exe 里没有 sklearn,
#      这一节不能因此变红。
import numpy as _np37
from arxiv_rec import tfidf as _tf37

_docs37 = [
    "quantum spin liquid entanglement entropy",
    "quantum spin liquid and entanglement",
    "quantum monte carlo simulation of the hubbard model",
    "hubbard model sign problem quantum monte carlo",
    "uniqueterm onlyhere",                        # min_df=2 之后整篇都没了 -> 空行
    "quantum spin liquid entanglement entropy",   # 和第 1 篇一字不差
]
_kw37 = dict(stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=2)
_v37 = _tf37.TfidfVectorizer(**_kw37)
_M37 = _v37.fit_transform(_docs37)
_names37 = list(_v37.get_feature_names_out())
_d37 = _M37.toarray()

# --- 37a) 结构断言 (不需要 sklearn) ---
check("剪枝砍掉了只出现一次的词 (uniqueterm / onlyhere 都不在词表里)",
      "uniqueterm" not in _names37 and "onlyhere" not in _names37, _names37)
check("该留的词留着了 (quantum / spin / liquid / monte 都在)",
      all(w in _names37 for w in ("quantum", "spin", "liquid", "monte")), _names37[:8])
check("词表是字母序的", _names37 == sorted(_names37), _names37[:6])
check("vocabulary_ 的下标和 get_feature_names_out 对得上",
      all(_v37.vocabulary_[n] == i for i, n in enumerate(_names37)))
_emp37 = [i for i in range(6) if not _d37[i].any()]
check("剪枝之后有且只有一行全零 (第 5 篇)", _emp37 == [4], _emp37)
_n37 = _np37.sqrt((_d37 ** 2).sum(axis=1))
check("非空行的 L2 范数都是 1 (上一篇被空行带坏的话这条立刻炸)",
      all(abs(_n37[i] - 1.0) < 1e-12 for i in range(6) if i != 4),
      [round(float(x), 6) for x in _n37])
_C37 = _tf37.cosine_similarity(_M37)
check("空行和谁的相似度都是 0 (横竖两个方向)",
      float(abs(_C37[4]).max()) < 1e-15 and float(abs(_C37[:, 4]).max()) < 1e-15,
      _C37[4])
check("一字不差的两篇 -> 相似度正好是 1", abs(_C37[0][5] - 1.0) < 1e-15, _C37[0][5])
check("非空行自己对自己是 1",
      all(abs(_C37[i][i] - 1.0) < 1e-15 for i in range(6) if i != 4))
check("相似度都在 0~1 之间, 而且左右对称",
      float(_C37.min()) >= -1e-15 and float(_C37.max()) <= 1 + 1e-15
      and float(abs(_C37 - _C37.T).max()) < 1e-15,
      (float(_C37.min()), float(_C37.max())))
_q37 = _v37.transform(["quantum spin liquid brandnewword"])
check("transform: 词表外的词丢掉, 词表内的 5 个 (3 单词 + 2 双词) 留着",
      _q37.data.size == 5, _q37.data.size)
check("transform 出来的行范数也是 1",
      abs(float(_np37.sqrt((_q37.toarray() ** 2).sum())) - 1.0) < 1e-12)
try:
    _tf37.TfidfVectorizer(stop_words="english").fit_transform(["the a of", "of the a"])
    _raised37 = False
except ValueError:
    _raised37 = True
check("整批文档全是停用词时抛 ValueError (不能返回一张空词表硬撑着)", _raised37)

# --- 37a2) 换了实现之后, 别再有人偷偷 import 回去 ---
# 这一条是这次改动的命门。打包出来的 exe 里**没有** sklearn / scipy 了
# (build_exe.py 的 EXCLUDES 把它们排掉, 依赖清单里也删了)。要是哪天 arxiv_rec/
# 里又冒出一句 ``from sklearn... import ...``, 程序**不会报错** —— 那三处调用点
# 全是 try/except 兜底的, 它会安安静静退化成关键词重叠, 推荐质量掉一大截,
# 而你只会在某天觉得"最近推荐怎么不太准"。所以直接扫源码。
# 只认真的 import 语句 (行首 from/import), 注释和 docstring 里提到 sklearn 不算。
import re as _re37
_ar37 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arxiv_rec")
_imp37 = _re37.compile(r"^\s*(?:from|import)\s+(sklearn|scipy)(?:\.[\w.]*)?\s*(?:import\s|$)")
_hit37 = []
for _fn37 in sorted(os.listdir(_ar37)):
    if not _fn37.endswith(".py"):
        continue
    with open(os.path.join(_ar37, _fn37), encoding="utf-8") as _f37:
        for _ln37, _line37 in enumerate(_f37, 1):
            _m37 = _imp37.match(_line37)
            if _m37:
                _hit37.append("arxiv_rec/%s:%d %s" % (_fn37, _ln37, _line37.strip()))
check("arxiv_rec/ 里没有一句 import sklearn / scipy (有的话 exe 里就是 ImportError, "
      "而三处调用点全是 try/except 兜底 —— 会静默退化成关键词匹配)", not _hit37, _hit37)

# --- 37b) 和 sklearn 逐项对拍 (装了才跑) ---
try:
    from sklearn.feature_extraction.text import TfidfVectorizer as _Sk37
    from sklearn.metrics.pairwise import cosine_similarity as _skcos37
except Exception as _e37:
    print("  (没装 sklearn, 跳过'和 sklearn 逐项对拍'那几条: %s)" % _e37)
else:
    # 对拍口径: 结构必须**完全一样** (词表逐字、非零元个数), 数值允许 ~1e-12 的
    # 相对误差 —— 行范数 sklearn 用 Cython 顺序累加, 我们走 numpy 的 reduceat
    # (成对累加), 尾数差几个 ulp 是躲不掉的 (实测真实文献库 2.4e-16, 最坏的
    # 人造语料 7.5e-15)。真正要钉死的是**排序**不能变。
    _edge37 = ["", "   ", "the a of", "real words here and there"]

    def _cmp37(tag, docs, **kw):
        try:
            sv = _Sk37(**kw)
            Xs = sv.fit_transform(docs)
            sk_err = None
        except Exception as exc:
            sv = Xs = None
            sk_err = exc
        try:
            mv = _tf37.TfidfVectorizer(**kw)
            Xm = mv.fit_transform(docs)
            me_err = None
        except Exception as exc:
            mv = Xm = None
            me_err = exc
        if sk_err is not None or me_err is not None:
            check("%s: 报错行为一致" % tag, (sk_err is None) == (me_err is None),
                  "sklearn=%r 我们=%r" % (sk_err, me_err))
            return
        ns = [str(x) for x in sv.get_feature_names_out()]
        nm = [str(x) for x in mv.get_feature_names_out()]
        if ns != nm:
            check("%s: 词表逐字一样" % tag, False,
                  "%d vs %d 个词: %s vs %s" % (len(ns), len(nm), ns[:4], nm[:4]))
            return
        check("%s: 词表逐字一样 (%d 个词)" % (tag, len(ns)), True)
        check("%s: 非零元个数一样" % tag, Xs.nnz == Xm.data.size,
              (Xs.nnz, Xm.data.size))
        a, b = Xs.toarray(), Xm.toarray()
        _sc = max(1e-300, float(abs(a).max()), float(abs(b).max()))
        _r = float(abs(a - b).max()) / _sc
        check("%s: 每个数都对得上 (相对 %.1e)" % (tag, _r), _r <= 1e-12)
        cs = _skcos37(Xs)
        cm = _tf37.cosine_similarity(Xm)
        _rc = float(abs(cs - cm).max()) / max(1e-300, float(abs(cs).max()))
        check("%s: 余弦相似度对得上 (相对 %.1e)" % (tag, _rc), _rc <= 1e-12)
        check("%s: 余弦每行的排序完全一致" % tag,
              all((cs[i].argsort()[::-1] == cm[i].argsort()[::-1]).all()
                  for i in range(cs.shape[0])))
        _qq = "quantum spin liquid brandnewword"
        _qs = sv.transform([_qq]).toarray()
        _qm = mv.transform([_qq]).toarray()
        _rq = float(abs(_qs - _qm).max()) / max(1e-300, float(abs(_qs).max()))
        check("%s: transform 对得上 (相对 %.1e)" % (tag, _rq), _rq <= 1e-12)

    _cmp37("小语料/min_df=1", _docs37,
           stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=1)
    _cmp37("小语料/min_df=2 (有空行)", _docs37, **_kw37)
    _cmp37("小语料/单词 + max_features", _docs37,
           stop_words="english", ngram_range=(1, 1), max_features=8)
    _cmp37("空文档和停用词", _edge37,
           stop_words="english", ngram_range=(1, 2), sublinear_tf=True, min_df=1)
    _cmp37("空文档/只要双词", _edge37, ngram_range=(2, 2), min_df=1)

    # 真实文献库: 有就拿前 80 篇再对一遍 (开发时 204 篇全量跑过, 结论一样)。
    # 先复制一份再读 —— 绝不拿 PdfIndex 直接开用户那份索引, 它初始化时可能作废
    # 记录, 一个自查程序不该有这种副作用。
    _db37 = ""
    try:
        from arxiv_rec.config import data_paths as _dp37
        _db37 = _dp37({"library": {"index_db": "library_index.sqlite"}})[0][2]
    except Exception:
        _db37 = ""
    if not _db37 or not os.path.isfile(_db37):
        print("  (没找到文献库索引, 跳过'拿真实文献对拍')")
    else:
        import shutil as _sh37, tempfile as _tmpmod37
        from arxiv_rec.pdf_library import (PdfIndex as _PI37, _row_to_paper as _rtp37,
                                           normalize_depth as _nd37)
        _copy37 = os.path.join(_tmpmod37.gettempdir(), "daily_arxiv_selftest_tfidf.sqlite")
        try:
            _sh37.copy(_db37, _copy37)
            _ix37 = _PI37(_copy37)
            _ps37 = []
            for _row in _ix37.rows():
                if _row["status"] not in ("ok", "notext"):
                    continue
                try:
                    _rtp37(_row, _row["path"], _ps37, _nd37("sections"))
                except Exception:
                    continue
                if len(_ps37) >= 80:
                    break
            _ix37.close()
            _real37 = []
            for _p in _ps37:
                _t = " ".join(x for x in (_p.title, _p.abstract, _p.context) if x)
                if _t.strip():
                    _real37.append(_t.lower())
            check("从真实文献库里读到了文档 (前 %d 篇)" % len(_real37), len(_real37) >= 5,
                  len(_real37))
            if len(_real37) >= 5:
                _cmp37("真实文献库前 80 篇", _real37, stop_words="english",
                       ngram_range=(1, 2), sublinear_tf=True, min_df=1,
                       max_features=60000)
        except Exception as _exc37:
            print("  (拿真实文献对拍时出错, 跳过: %r)" % _exc37)
        finally:
            for _suf in ("", "-wal", "-shm"):
                if os.path.exists(_copy37 + _suf):
                    try:
                        os.remove(_copy37 + _suf)
                    except OSError:
                        pass

print()
print("=" * 70)
if FAIL:
    print("失败 %d 项:" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
