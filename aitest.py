# -*- coding: utf-8 -*-
"""用本地假 AI 服务跑通"开 AI"的完整流水线。

没有真 API key, 但 ai.py 的 HTTP/解析/缓存、profile 的 AI 归纳、rank 的 AI 打分、
analyze 的逐篇解读、report 的渲染 —— 这些管道都能验。假服务只做一件事:
看提示词里出现的是哪个 schema, 就回一个结构合法的 JSON。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from http.server import BaseHTTPRequestHandler, HTTPServer

FAIL = []
CALLS = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name +
          (("  <- " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAIL.append(name)


def reply_for(user_text):
    """按提示词里的 schema 关键字决定回什么。

    ``user_text`` 是**全部 messages 拼起来的** —— 系统提示词也在里面, 所以能靠
    "继续讨论" 这种字样认出这是 chat.py 发来的那一类请求。
    """
    # 和 AI 逐篇讨论 (chat.py 的 CHAT_SYSTEM 里写着"这是一场…继续讨论"): 要的是
    # **纯文字**回答, 不是 JSON —— 返回字符串, 下面 handler 就不套围栏了。
    # "用对话更新详解"那次提示词里带着 ANALYZE_SCHEMA (含 "connections"), 所以
    # 排除掉它, 让它落到下面那个 JSON 分支去。
    if "继续讨论" in user_text and '"connections"' not in user_text:
        return ("假服务: 它先做了一个规范变换, 把符号问题转成可解的本征值问题, "
                "附录 B 里给了构造和数值验证。")
    if '"scores"' in user_text:
        ids = re.findall(r'"id":\s*"([^"]+)"', user_text)
        scores = [{"id": i, "score": 80 - (n % 40),
                   "reason": "假服务: 与你的强关联方向重叠"} for n, i in enumerate(ids)]
        return {"scores": scores}
    if '"connections"' in user_text:
        # 引用标签在提示词里长这样: "[标签] 标题 (年份)"
        labels = re.findall(r"(?m)^\[([^\]]+)\] ", user_text)
        conns = [{"paper": lb, "relation": "假服务: 方法同源, 都在用 DQMC"}
                 for lb in labels[:2]]
        return {
            "summary": "假服务生成的讲解: 这篇论文研究了强关联体系里的符号问题, "
                       "用行列式量子蒙特卡罗在有限格点上做了系统扫描, "
                       "结论是低温下符号问题随尺寸指数恶化。",
            "connections": conns,
            "ideas": "可以把它的无符号探针思路搬到你的蜂窝格子模型上, 先在小格点验证。",
        }
    if '"queries"' in user_text:
        return {"summary": "假服务归纳的画像: 强关联电子体系与量子蒙特卡罗方法。",
                "topics": ["强关联电子", "量子相变"],
                "methods": ["行列式量子蒙特卡罗"],
                "keywords": ["determinant quantum Monte Carlo", "sign problem",
                             "Hubbard model"],
                "queries": ["sign problem", "hubbard model"]}
    if '"subfields"' in user_text or '"topics"' in user_text:
        return {"topics": ["强关联电子"], "methods": ["量子蒙特卡罗"],
                "keywords": ["sign problem", "hubbard model"],
                "subfields": ["cond-mat.str-el"]}
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8"))
        user_text = "\n".join(m.get("content", "") for m in body.get("messages", []))
        CALLS.append(self.path)
        payload = reply_for(user_text)
        if isinstance(payload, str):
            text = payload              # 聊天要的就是一段人话, 不套 JSON 围栏
        else:
            # 故意包一层 ```json 围栏, 顺带验 safe_json_loads 剥围栏
            text = "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
        out = json.dumps({
            "id": "mock", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("假 AI 服务: http://127.0.0.1:%d/v1" % port)

tmpdir = tempfile.mkdtemp(prefix="daily_arxiv_aitest_")
cfg = {
    "ai": {"provider": "openai", "base_url": "http://127.0.0.1:%d/v1" % port,
           "api_key": "sk-mock", "model": "mock-model",
           "temperature": 0.3, "max_tokens": 2000},
    "pdf_folders": [{"path": r"C:\Users\admin\Zotero\storage",
                     "enabled": True, "recursive": True}],
    "library": {"index_db": os.path.join(tmpdir, "idx.sqlite"),
                "read_depth": "sections",
                "fulltext_excerpt_chars": 1000, "max_papers_for_profile": 60,
                "concurrency": 4, "skip_patterns": []},
    "arxiv": {"queries": [], "auto_queries": 2, "categories": ["cond-mat.str-el"],
              "per_query": 25, "max_candidates": 120, "request_delay": 1.0,
              "min_relevance": 0.0, "enabled": True},
    "ranking": {"weight_relevance": 0.6, "weight_recency": 0.2,
                "weight_importance": 0.2, "recency_tau_days": 365.0,
                "recency_hard_days": 1460.0, "citation_scale": 200.0,
                "journal_ref_bonus": 0.15, "venue_keywords": ["phys. rev. lett"],
                "venue_bonus": 0.1},
    # history_db 必须指到临时目录: 相对路径是按**程序所在目录**解析的, 不写绝对
    # 路径的话, 这个用假 AI 跑的测试会把假的"解读"写进用户真正的推荐记录里 ——
    # 之后真跑一轮, 报告里就会出现测试假造的内容 (真踩过)。
    "analysis": {"top_n": 3, "concurrency": 2, "max_for_ai_scoring": 40,
                 "score_batch_size": 8,
                 "history_db": os.path.join(tmpdir, "history.sqlite")},
    "network": {"proxy": None, "retries": 2, "cache": True,
                "cache_dir": os.path.join(tmpdir, "cache")},
    "output": {"dir": os.path.join(tmpdir, "output")},
}
cfg_path = os.path.join(tmpdir, "config.json")
json.dump(cfg, io.open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

import copy as _copy

from arxiv_rec import pipeline as _pl
from arxiv_rec.config import load_config
from arxiv_rec.pipeline import PipelineOptions, run_pipeline
from arxiv_rec.utils import add_log_sink, remove_log_sink, setup_console

setup_console()

print()
print("=" * 70)
print("开 AI 跑完整流水线 (profile -> rank -> analyze -> report)")
print("=" * 70)
# 第一轮走真网络 (验的是检索链路), 顺手把抓到的候选池抄一份下来 —— 下面验
# AI 缓存时要拿它当第二轮的输入, 免得两轮的提示词因为 arXiv 限流而不同。
_pool = []
_orig_collect = _pl.collect


def _capture_collect(*a, **kw):
    out = _orig_collect(*a, **kw)
    _pool.append(_copy.deepcopy(out))
    return out


_pl.collect = _capture_collect
try:
    c = load_config(cfg_path)
    res = run_pipeline(c, PipelineOptions(stop_at="report"))
finally:
    _pl.collect = _orig_collect

print()
check("运行成功", res.ok, res.message)
check("AI 服务被调用过", len(CALLS) > 0, len(CALLS))
print("     AI 调用 %d 次, 约 %d tokens" % (res.stats.ai_calls, res.stats.ai_tokens))
check("统计到 AI 调用次数", res.stats.ai_calls > 0, res.stats.ai_calls)
check("统计到 token 数", res.stats.ai_tokens > 0, res.stats.ai_tokens)

check("研究画像来自 AI", res.profile is not None and
      "ai" in (res.profile.generated_by or ""),
      getattr(res.profile, "generated_by", None))
if res.profile is not None:
    check("画像用上了 AI 生成的检索式",
          res.profile.queries == ["sign problem", "hubbard model"],
          res.profile.queries)
    check("画像带上了 AI 摘要", bool(res.profile.summary), res.profile.summary)

check("有推荐结果", len(res.top) > 0, len(res.top))
if res.top:
    check("top 数量 = top_n", len(res.top) == 3, len(res.top))
    scored = [c for c in res.ranked if getattr(c, "relevance_reason", "")]
    check("AI 打分理由被写回候选", len(scored) > 0, len(scored))
    if scored:
        check("理由不是启发式占位",
              "启发式" not in scored[0].relevance_reason,
              scored[0].relevance_reason)
    analyzed = [c for c in res.top if c.analyzed]
    check("前几篇被 AI 深度解读", len(analyzed) > 0,
          "%d/%d" % (len(analyzed), len(res.top)))
    if analyzed:
        a = analyzed[0]
        check("解读带 summary", bool(a.summary), repr(str(a.summary)[:50]))
        check("解读带 connections", bool(a.connections), a.connections)
        check("connections 的标签都来自文献库",
              all(cn["paper"] for cn in (a.connections or [])), a.connections)
        check("解读带 ideas", bool(a.ideas), repr(str(a.ideas)[:50]))

check("报告已生成", len(res.report_paths) > 0, res.report_paths)
if res.report_paths:
    md = io.open(res.report_paths[0], encoding="utf-8").read()
    print("     报告 %d 字符: %s" % (len(md), res.report_paths[0]))
    check("报告里有 AI 讲解", "假服务生成的讲解" in md)
    check("报告里有研究关联", "假服务" in md or "关联" in md)
    check("报告里有研究方向建议", "研究" in md)
    check("报告里有推荐总览表", "|" in md)

# 缓存: 再跑一次应该零 AI 调用
#
# 第二次必须喂**和第一次一模一样**的候选池, 否则这条断言测的就不是缓存而是
# arXiv 的心情 —— 真踩过: 第一轮 `sign problem` 被 429 挡住 (只抓到 50 篇),
# 第二轮它成功了 (98 篇), 提示词全变, 缓存当然不命中, 于是报"缓存坏了"。
# 那是限流, 不是 bug, 而且这个失败还会随 arXiv 的状态时有时无。
#
# 所以: 第一轮照旧走真网络 (它验的是检索链路), 顺手把候选池抄下来;
# 第二轮把 collect 换成"把那份候选池原样交出去", 输入就完全确定了。
before = len(CALLS)
_hits = []


def _count_hits(msg, _lvl=""):
    if "AI 命中缓存" in msg:
        _hits.append(msg)


add_log_sink(_count_hits)
_orig_collect2 = _pl.collect
_pl.collect = lambda *a, **kw: _copy.deepcopy(_pool[0])
try:
    c2 = load_config(cfg_path)
    res2 = run_pipeline(c2, PipelineOptions(stop_at="report"))
finally:
    _pl.collect = _orig_collect2
    remove_log_sink(_count_hits)

check("第二次运行零 AI 调用", len(CALLS) == before,
      "%d -> %d" % (before, len(CALLS)))
# 光看"调用次数没涨"还不够: 第二轮那几篇会被**推荐记录**直接复用 (第一轮刚
# 写进去的), 一次 AI 都不调也能让上面那条通过。这里单独盯住缓存本身 ——
# 命中缓存的日志条数必须为正, 否则等于这条测试什么都没验。
check("第二次运行真的走了 AI 缓存 (而不是全靠推荐记录复用)", len(_hits) > 0,
      "命中缓存 %d 次, 日志: %s" % (len(_hits), _hits[:3]))
check("第二次运行仍有推荐结果", len(res2.top) == len(res.top),
      "%d vs %d" % (len(res2.top), len(res.top)))

print()
print("=" * 70)
print("和 AI 逐篇讨论 (chat.py): 真 HTTP 客户端走一遍, 再写回记录")
print("=" * 70)
# 前面验的是流水线里的"一次性解读"; 这一节验的是**接着往下聊**: 提示词拼得对不对
# (画像 + 相关文献标签 + 这篇论文 + 已有解读)、回答走的是纯文字而不是 JSON、
# "用对话更新详解"给回来的关联仍然要过标签校验、以及这些字最后落在哪。
from arxiv_rec import chat as _chat
from arxiv_rec import history as _hist
from arxiv_rec.ai import build_client
from arxiv_rec.models import Candidate as _Cand
from arxiv_rec.models import LibraryPaper as _Paper

_ai = build_client(c, None)
_hdb = cfg["analysis"]["history_db"]
_hcfg = {"analysis": {"history_db": _hdb, "use_history": True}}

_papers = [_Paper(item_id=1, key="k1", title="Sign problem in determinant QMC",
                  abstract="Majorana positivity and the sign problem",
                  year=2020, authors=["Wei Wang"], arxiv_id="2001.00001"),
           _Paper(item_id=2, key="k2", title="Reassessing the Pfaffian sign",
                  abstract="Pfaffian sign problem in lattice models",
                  year=2019, authors=["Li Chen"], arxiv_id="1901.00001")]

_talk = _Cand(arxiv_id="2401.90001",
              title="A sign-problem-free reformulation for lattice fermions",
              abstract="We reformulate the sign problem via a canonical "
                       "transformation and validate numerically.")
_talk.summary = "流水线给出的旧解读。"
_talk.ideas = "旧的方向。"
_talk.connections = [{"paper": "Wang 2020", "relation": "方法同源"}]

_prefix, _labels = _chat.build_context({"analysis": {}}, _talk, papers=_papers)
check("讨论的上下文里有这篇论文", "sign-problem-free reformulation" in _prefix)
check("讨论的上下文里有可引用的文献标签", bool(_labels), _labels)
check("讨论的上下文里带着已有的解读 (从它接着往下, 不重复讲)",
      "流水线给出的旧解读。" in _prefix)

_before = len(CALLS)
_msgs = [{"role": "user", "content": "它的符号问题是怎么绕过去的?"},
         {"role": "assistant", "content": "先做规范变换, 再验证。"}]
_ans = _chat.reply(_ai, _prefix, _msgs, "这个变换在小格点上还成立吗?")
check("追问拿到了回答", bool(_ans), repr(_ans[:60]))
check("回答是**纯文字**, 不是 JSON (聊天不该被 JSON 围栏包住)",
      "假服务: 它先做了一个规范变换" in _ans and "```" not in _ans, _ans[:80])
check("这一问只调了一次 AI", len(CALLS) == _before + 1, len(CALLS) - _before)

# 同一段提示词再问一次: 还是得真调一次 —— 讨论刻意不走 AI 缓存 (缓存按提示词
# 逐字做键, 这里每问一句提示词都不同, 命中率极低却会把整场问答都留在盘上)。
_before2 = len(CALLS)
_chat.reply(_ai, _prefix, _msgs, "这个变换在小格点上还成立吗?")
check("讨论不吃 AI 缓存 (同样的问法也真打一次接口)",
      len(CALLS) == _before2 + 1, len(CALLS) - _before2)

_got = _chat.rewrite_analysis(_ai, _prefix, _msgs, _labels)
check("重写详解拿到了 summary", bool(_got.get("summary")), _got.get("summary"))
check("重写详解拿到了 ideas", bool(_got.get("ideas")), _got.get("ideas"))
check("重写详解的关联标签来自文献库 (编出来的会被丢掉)",
      bool(_got.get("connections"))
      and all(cn["paper"] in _labels for cn in _got["connections"]),
      (_got.get("connections"), _labels))

# --- 存: 讨论按论文存, 解读只碰解读那几列 ---
_hist.append_chat(_hcfg, "2401.90001", "user", "它的符号问题是怎么绕过去的?")
_hist.append_chat(_hcfg, "2401.90001", "assistant", _ans)
check("讨论存进去了",
      [m["role"] for m in _hist.load_chat(_hcfg, "2401.90001")]
      == ["user", "assistant"],
      _hist.load_chat(_hcfg, "2401.90001"))

with _hist.RecommendHistory(_hdb) as _h:
    _h.record([_Cand(arxiv_id="2401.90001", title=_talk.title, score=0.77)],
              profile_fp="fp-before")
_row0 = {r["arxiv_id"]: r for r in _hist.load_rows(_hcfg)}["2401.90001"]
_talk.summary, _talk.ideas = _got["summary"], _got["ideas"]
_talk.connections = _got["connections"]
_talk.analyzed = True
check("写回详解成功", _hist.save_analysis(_hcfg, _talk, "fp-after"))
_row1 = {r["arxiv_id"]: r for r in _hist.load_rows(_hcfg)}["2401.90001"]
check("详解写进了记录库", _row1["summary"] == _got["summary"], _row1["summary"])
check("写回详解**没有**动推荐次数 (用户没跑推荐, 只是聊了聊)",
      _row1["times"] == _row0["times"], (_row0["times"], _row1["times"]))
check("写回详解没有动最后推荐时间",
      _row1["last_at"] == _row0["last_at"],
      (_row0["last_at"], _row1["last_at"]))
check("讨论和解读互不干扰 (聊完还在, 写完也还在)",
      len(_hist.load_chat(_hcfg, "2401.90001")) == 2
      and bool(_row1["summary"]))
# 写回去的解读带着新的画像指纹 -> 下一轮跑推荐时能直接复用, 不再花 token
_c_reuse = _Cand(arxiv_id="2401.90001", title=_talk.title)
check("写回的解读下一轮能被复用 (画像没变)",
      _hist.RecommendHistory.reuse_analysis(_c_reuse, _row1, "fp-after")
      and _c_reuse.summary == _got["summary"], _c_reuse.summary)
check("画像变了就不复用",
      not _hist.RecommendHistory.reuse_analysis(
          _Cand(arxiv_id="2401.90001", title=_talk.title), _row1, "fp-别的"))

srv.shutdown()
shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("=" * 70)
if FAIL:
    print("失败 %d 项:" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
