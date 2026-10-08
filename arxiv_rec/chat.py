"""就某一篇论文和 AI 继续讨论, 以及"用这场讨论重写详解"。

和 analyze.py 的分工
--------------------------------------------------------------------------
analyze 是流水线里的**一次性**解读: 一篇论文进去, 三个字段 (内容讲解 / 关联 /
研究方向) 出来, 写进推荐记录就不动了。这里接着往下走 —— 用户在界面上追问
"这个符号问题它到底怎么处理的", AI 答完, 这一问一答也存进**同一篇论文的
记录**里 (见 history.py 的 chat 表)。

两件事共用同一套上下文 (研究画像 + 文献库里最相关的几篇 + 这篇论文本身),
所以上下文只在这里拼一次: ``build_context``。拼出来的标签列表还要留着 ——
"用对话更新详解"给出的关联必须真的指向文献库里的文献, 校验靠它。

详解**不会**每聊一句就重写
--------------------------------------------------------------------------
聊十句攒下的理解, 由用户自己决定什么时候落进详解里 (界面上的「用对话更新
详解」)。所以这里只有两个入口:
  * ``reply``            -- 回答一句追问, 不动详解
  * ``rewrite_analysis`` -- 把整场讨论整理成新的详解
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .analyze import (ANALYZE_SCHEMA, RELATED_K, LibraryIndex, candidate_block,
                      library_block, normalize_result)
from .models import Candidate, LibraryPaper, ResearchProfile
from .profile import build_profile, profile_digest_for_scoring
from .utils import log

# 送进提示词的最近几条对话 / 对话部分的字符上限。一篇论文聊到几十条时, 全塞
# 进去既贵又没必要 —— 最近几轮就够 AI 接上话; 更早的内容, 有用的那部分本来就
# 该在"用对话更新详解"之后进详解了。
CHAT_MAX_MESSAGES = 12
CHAT_MAX_CHARS = 9000

CHAT_SYSTEM = (
    "你是一位既懂物理又熟悉学术前沿的资深合作者。研究者正在和你逐篇讨论他"
    "打算精读的 arXiv 论文 —— 这是一场针对单篇论文的继续讨论, 不是泛泛的"
    "问答。回答要具体、有信息量: 能落到公式、量级、数值设置上就落上去; 论文里"
    "没写、你也不确定的地方直说不确定, 不要编造数据。用中文回答。"
)

REWRITE_SYSTEM = (
    CHAT_SYSTEM + "这一次的任务不是回答某一句, 而是把整场讨论的结论整理成一份"
    "解读 —— 讨论里已经说清楚的东西要吸收进去, 只复述摘要等于什么都没更新。"
    "只输出 JSON。"
)


# --------------------------------------------------------------------------
# 上下文
# --------------------------------------------------------------------------
def build_context(
    cfg: Dict[str, Any],
    cand: Candidate,
    profile: Optional[ResearchProfile] = None,
    papers: Optional[List[LibraryPaper]] = None,
) -> Tuple[str, List[str]]:
    """拼"讨论这篇论文"的提示词前缀, 返回 ``(前缀, 可用文献标签)``。

    标签是给 ``normalize_result`` 校验关联用的 (见 analyze.library_block)。

    ``papers`` / ``profile`` 不传就自己准备: 文献库从索引库读 (带缓存的那一层,
    第二次基本不花时间), 画像没有就**用启发式现算一份** —— 聊天不该为了拿一段
    画像说明就去调一次 AI, 那笔 token 花得莫名其妙。
    """
    if papers is None:
        try:
            from .library import load_library
            papers = load_library(cfg)[0]
        except Exception as exc:
            log("讨论: 读文献库失败 (%s), 这次不附相关文献" % exc, "warn")
            papers = []
    if profile is None and papers:
        try:
            profile = build_profile(cfg, papers, ai=None)
        except Exception as exc:
            log("讨论: 建研究画像失败 (%s)" % exc, "warn")
            profile = None

    related: List[LibraryPaper] = []
    if papers:
        try:
            k = int((cfg.get("analysis") or {}).get("related_papers_k", RELATED_K))
            related = LibraryIndex(papers).related(cand, k)
        except Exception as exc:
            log("讨论: 找相关文献失败 (%s)" % exc, "warn")

    lib_block, labels = library_block(related)

    parts = []
    if profile is not None:
        digest = profile_digest_for_scoring(profile)
        if digest:
            parts.append("研究者的研究方向:\n%s" % digest)
    if lib_block:
        parts.append("研究者已读的相关文献 (方括号内是引用标签, "
                     "引用它们时必须原样照抄):\n%s" % lib_block)
    parts.append("正在讨论的这篇论文:\n%s" % candidate_block(cand))

    # 已经有一份详解就一起给它: 讨论是在那份解读的基础上往下走的, 不给的话
    # AI 会把已经讲过的内容再讲一遍。
    have = []
    if getattr(cand, "summary", ""):
        have.append("内容讲解: %s" % cand.summary)
    if getattr(cand, "connections", None):
        rows = []
        for conn in cand.connections:
            label = conn.get("paper") or conn.get("title") or ""
            why = conn.get("relation") or conn.get("why") or ""
            rows.append("  · %s%s" % (label, (": " + why) if why else ""))
        have.append("与研究者文献的关联:\n%s" % "\n".join(rows))
    if getattr(cand, "ideas", ""):
        have.append("可以结合的研究方向: %s" % cand.ideas)
    if have:
        parts.append("这篇论文目前的解读 (讨论从它接着往下):\n%s"
                     % "\n".join(have))

    return "\n\n".join(parts), labels


def transcript_text(msgs: List[Dict[str, Any]],
                    budget: int = CHAT_MAX_CHARS) -> str:
    """把讨论记录拼成提示词里的一段。

    **从最近的往回取**: 超预算时丢掉的必须是最早那几条 —— 用户刚问的那句和
    AI 刚答的那段才是接下来要接的话, 把最新的截掉等于白聊。
    """
    lines: List[str] = []
    used = 0
    for m in reversed(list(msgs or [])[-CHAT_MAX_MESSAGES:]):
        who = "研究者" if str(m.get("role")) == "user" else "你"
        body = str(m.get("content") or "").strip()
        if not body:
            continue
        line = "%s: %s" % (who, body)
        if lines and used + len(line) > budget:
            break
        lines.append(line)
        used += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 两个入口
# --------------------------------------------------------------------------
def reply(ai: Any, prefix: str, msgs: List[Dict[str, Any]],
          question: str, on_delta: Any = None,
          stop_event: Any = None) -> str:
    """回答一句追问。``msgs`` 是**这之前**的讨论 (不含这一问)。

    刻意不走 AI 缓存 (``use_cache=False``): 缓存按提示词逐字做键, 而这里每问
    一句提示词都不一样 —— 命中率极低, 却会把每一次的完整问答都留在缓存目录
    里, 白占地方。
    """
    parts = [prefix]
    hist = transcript_text(msgs)
    if hist:
        parts.append("到目前为止的讨论:\n%s" % hist)
    parts.append("研究者刚才问:\n%s" % str(question or "").strip())
    parts.append("请直接回答这个追问, 300-600 字。不要重复上面已经说过的内容; "
                 "如果这个问题论文里没有明确答案, 就说明你的推断和依据。")
    # ``on_delta`` / ``stop_event`` 原样透给 AI 客户端: 讨论这条路走流式, 界面
    # 才能边出边显示、也才能中途掐掉 (见 ai.AIClient.chat)。
    text = ai.chat(CHAT_SYSTEM, "\n\n".join(parts), use_cache=False,
                   on_delta=on_delta, stop_event=stop_event)
    return (text or "").strip()


def rewrite_analysis(ai: Any, prefix: str, msgs: List[Dict[str, Any]],
                     labels: List[str], stop_event: Any = None) -> Dict[str, Any]:
    """把整场讨论整理成一份新的详解。返回 ``{summary, connections, ideas}``。"""
    hist = transcript_text(msgs)
    user = "\n\n".join([
        prefix,
        "下面是研究者和你就这篇论文展开的讨论:\n%s" % hist,
        "请**根据这场讨论**重写这篇论文的解读: 讨论里已经澄清、说透的内容要吸收"
        "进来 (尤其是那些摘要里没有、聊出来才明确的细节); 关联要落到上面给出的"
        "文献标签上。严格按此 JSON 结构输出:\n%s" % ANALYZE_SCHEMA,
    ])
    # 这条路只透 ``stop_event``, 不透 ``on_delta``: 要的是一份 JSON, 流式显示
    # 半截 JSON 看不出名堂; 但它同样可能想很久, 能停掉是有意义的。
    result = ai.chat_json(REWRITE_SYSTEM, user, default=None, use_cache=False,
                          stop_event=stop_event)
    return normalize_result(result, labels)


# --------------------------------------------------------------------------
# 讨论前把论文补齐
# --------------------------------------------------------------------------
def ensure_abstract(cfg: Dict[str, Any], cand: Candidate,
                    cache: Optional[Any] = None) -> bool:
    """手上没有摘要就去 arXiv 取一份 (顺带补作者/日期)。返回"现在有摘要了吗"。

    为什么需要这一步: 从「推荐记录」下拉框或「全部累计记录」里翻出来的论文,
    手上只有标题、分数和当时的解读 —— **摘要没有存进记录库**。拿"标题 + 没有
    摘要"去和 AI 讨论, 它只能泛泛而谈; 而摘要本来就躺在 arXiv 上, 一条请求
    (按 ID 批量取, 且同一篇取过一次就进缓存) 就能拿回来。

    失败不抛: 取不回来就按现有信息聊, 只是聊得浅一点 —— 不该因为网络问题把
    对话整个堵住。
    """
    if str(getattr(cand, "abstract", "") or "").strip():
        return True
    aid = str(getattr(cand, "arxiv_id", "") or "").strip()
    if not aid:
        return False
    try:
        from .arxiv_search import search_by_ids
        from .utils import build_session, set_global_interval

        ncfg = cfg.get("network") or {}
        delay = float((cfg.get("arxiv") or {}).get("request_delay", 3.0))
        # 全局节流闸门照设: 补摘要不该绕过"每条请求之间歇几秒"这条规矩
        set_global_interval(delay)
        session = build_session(ncfg.get("proxy"),
                                timeout=int(ncfg.get("timeout", 40)))
        got = search_by_ids(session, [aid], cache=cache, delay=delay, retries=2)
    except Exception as exc:
        log("讨论: 按 ID 取 %s 的摘要失败: %s" % (aid, exc), "warn")
        return False

    for c in got or []:
        if str(getattr(c, "arxiv_id", "") or "") != aid:
            continue
        # 只补**空的**: 手上已经有的 (标题、作者) 以手上的为准, arXiv 只是补缺
        if not str(getattr(cand, "title", "") or "").strip():
            cand.title = str(getattr(c, "title", "") or "")
        cand.abstract = str(getattr(c, "abstract", "") or "")
        if not cand.authors:
            cand.authors = list(getattr(c, "authors", None) or [])
        if cand.published is None:
            cand.published = getattr(c, "published", None)
        if not cand.categories:
            cand.categories = list(getattr(c, "categories", None) or [])
            if cand.categories:
                cand.primary_category = cand.categories[0]
        if not str(getattr(cand, "journal_ref", "") or ""):
            cand.journal_ref = str(getattr(c, "journal_ref", "") or "")
        log("讨论: 从 arXiv 取回了 %s 的摘要 (%d 字)"
            % (aid, len(cand.abstract)))
        return bool(cand.abstract.strip())

    log("讨论: 没能取回 %s 的摘要 (网络不通?), 只能按标题和已有解读来聊" % aid,
        "warn")
    return False
