"""配置加载与校验。

配置来源优先级: 命令行参数 > 环境变量 > config.json > 内置默认值。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# 默认配置
# --------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    # 文献来源: 本地 PDF 文件夹。每一项:
    # {"path": "...", "enabled": true, "recursive": true}
    "pdf_folders": [],
    # PDF 解析与增量索引
    "library": {
        # 已解析结果存这里; 靠它跳过"上次读过的文件", 不重复花 token 和时间
        "index_db": "library_index.sqlite",
        # 读取深度, 三档:
        #   "metadata"  标题 + 摘要              最快, 送进 AI 的内容最少
        #   "sections"  标题 + 摘要 + 引言/结论   (推荐)
        #   "fulltext"  标题 + 摘要 + 引言/结论 + 全文节选
        # 调深了会自动重读文件补内容; 调浅了直接用索引里的旧结果, 不重读。
        "read_depth": "sections",
        # 每篇文献截取多少字符的正文 (只对 "fulltext" 档有意义)
        "fulltext_excerpt_chars": 3000,
        # 送进画像的文献数上限 (按年份取较新的)
        "max_papers_for_profile": 200,
        # 并行解析 PDF 的线程数
        "concurrency": 4,
        # 命中这些词的文件名视为附件 (补充材料/审稿意见等), 不当作文献
        "skip_patterns": [],
    },
    "ai": {
        # "openai" 兼容任意 OpenAI 格式服务 (DeepSeek/Qwen/Kimi/智谱/本地 vLLM...)
        # "anthropic" 走 Claude 官方 /v1/messages
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        # 也可以把 key 放在环境变量里, 留空 api_key 时会来这里找
        "api_key_env": "ARXIV_REC_API_KEY",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "max_tokens": 4096,
        "timeout": 240,
        "max_retries": 3,
    },
    "arxiv": {
        # 手动指定检索式; 留空则由 AI 根据你的文献库自动生成
        "queries": [],
        # 自动生成多少条检索式
        "auto_queries": 18,
        # 每条检索式抓多少条结果。走官方 Atom API, **一次请求拿完不翻页**,
        # 上限 2000 (arXiv 的硬限制)。
        "per_query": 200,
        # 检索式多于一条时, 每条实际抓多少篇按 max_candidates 摊薄 —— 上限,
        # 不再每条都抓满 per_query 篇。见 arxiv_search.collect 里的说明。
        # per_query_slack: 留多少余量给检索式之间的重叠 (同一篇被多条命中)
        "per_query_slack": 1.35,
        # per_query_min: 摊薄后的下限, 免得检索式一多每条只抓十来篇
        "per_query_min": 50,
        # 限定 arXiv 分类, 例如 ["cond-mat.str-el", "quant-ph"]; 留空不限定
        "categories": [],
        # 订阅模式: 不生成检索式, 候选论文全部来自 categories 里各分类当天的
        # arXiv 公告 (走 rss.arxiv.org, 和 zotero-arxiv-daily 一个路子)。
        # 关键词检索的短板是"换个说法的好文章搜不到"; 订阅是宁可多抓, 交给后面的
        # 相关性打分去筛。需要 categories 非空, 否则一条候选也抓不到。
        "subscribe_only": False,
        # 订阅模式保留哪些公告类型。arXiv 的 feed 混着四种: new (新论文) /
        # cross (新论文被交叉列表到本分类) / replace (**旧论文**的新版本) /
        # replace-cross。后两种不是新论文, 默认滤掉 —— 否则订阅模式会把几年前
        # 论文的修订版当成"今天的新论文"推给你。置空列表 = 全都保留。
        "subscribe_announce_types": ["new", "cross"],
        # 候选池上限 (去重后)
        "max_candidates": 800,
        # 相关性低于此值的直接丢弃 (0~1)
        "min_relevance": 0.30,
        # 全局请求间隔 (秒)。这是所有 arXiv/OpenAlex 请求共用的节流闸门, 不是
        # "每条检索式之间" —— 每条通道各自 sleep 挡不住并发, 加起来照样会触发
        # HTTP 429。arXiv 官方要求每 3 秒不超过 1 个请求, 所以 3.0 是安全下限,
        # 别往小调。
        "request_delay": 3.0,
        # 是否额外抓各分类当天的 RSS 公告。检索式是按你已有文献生成的, 覆盖不到
        # 的新方向不会出现; RSS 补上"今天刚出的全部论文"。
        "use_rss": True,
        # 抓哪些分类的 RSS; 留空则跟随上面的 categories
        "rss_categories": [],
        # 给缺摘要的候选 (主要是 RSS 来的) 用 id_list 批量补全元数据
        "fetch_details": False,
    },
    "ranking": {
        "weight_relevance": 0.60,
        "weight_recency": 0.20,
        "weight_importance": 0.20,
        # 时效性衰减时间常数 (天); 越小越偏向新论文
        "recency_tau_days": 365.0,
        # 超过这个天数时效性记 0
        "recency_hard_days": 1460.0,
        # 重要性: 引用数取 log 后的饱和尺度
        "citation_scale": 200.0,
        # "famous" 通道: 引用数达到此值即视为经典文献
        "famous_min_citations": 50,
        # 有期刊正式发表记录的加分
        "journal_ref_bonus": 0.15,
        # 顶级期刊/会议关键词加分
        "venue_keywords": [
            "phys. rev. lett", "prl", "nature", "science", "phys. rev. x",
            "prx", "nature physics", "nature materials", "pnas", "npj",
            "phys. rev. b", "prb", "quantum", "scipost",
        ],
        "venue_bonus": 0.10,
    },
    "analysis": {
        # 默认推荐篇数
        "top_n": 20,
        # 每次 AI 调用处理多少篇候选做相关性打分
        "score_batch_size": 8,
        # 每次 AI 调用深度解读多少篇
        "analyze_batch_size": 4,
        # 相关性打分阶段最多送多少篇候选进 AI (其余按启发式预筛)
        "max_for_ai_scoring": 300,
        # 最多给多少篇候选去 OpenAlex 查引用数 (按启发式相关性取最靠前的那些)。
        # 排在后面的候选本来就进不了 top_n, 为它们各查一次纯属浪费配额 ——
        # 这一步是整轮里最慢的网络环节。设 0 表示不限 (全部查)。
        "enrich_max": 400,
        # 深度解读时, 每篇候选配几篇"你自己的相关文献"作为上下文
        "related_papers_k": 8,
        # AI 调用的并发数, 同时管"相关性打分"和"深度解读"两处 (两者都是彼此
        # 独立的多批调用)。实测串着跑时打分那 38 批要 380 秒, 是整轮最慢的一步。
        # AI 接口限流紧就调成 1。
        "concurrency": 3,
        # --- 推荐记录 (见 history.py) ---
        # 记下推荐过哪些论文、以及那篇的解读。下次同一篇再进候选时:
        # 标记"已推荐过"、可选地跳过、画像没变就直接复用旧解读不调 AI。
        "history_db": "recommend_history.sqlite",
        "use_history": True,
        # 这次是否跳过以前推荐过的论文。默认关 —— 打开后推荐列表会明显变化
        # (也可能凑不满 top_n 篇), 适合"想看点新的"的时候
        "skip_recommended": False,
    },
    "network": {
        # None / "" -> 直连; "auto" -> 读系统代理; 或显式 "http://127.0.0.1:7890"
        "proxy": "http://127.0.0.1:7890",
        "timeout": 40,
        # arXiv 限流时返回 500, 需要多试几次
        "retries": 5,
        "cache": True,
        "cache_dir": "cache",
    },
    "output": {
        "dir": "output",
        "formats": ["markdown"],
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置, override 覆盖 base。"""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载配置文件并与默认值合并。

    ``path`` 为 None 时按 ``./config.json`` -> ``./config.example.json`` 顺序查找。
    """
    candidates = []
    if path:
        candidates.append(path)
    else:
        here = project_root()
        cwd_cfg = os.path.join(os.getcwd(), "config.json")
        # 打包成 exe 时**先看 exe 自己所在的目录**: 双击 exe 的"起始位置"未必
        # 是 exe 所在目录 (快捷方式、从资源管理器地址栏、被别的程序拉起来),
        # 当前目录下要是正好有一份 config.json, 程序就会读那一份 —— 于是
        # "设置里明明写了输出目录, 报告却跑到别处去了"。exe 旁边那份才是用户
        # 认的那份, 优先它。
        if is_frozen():
            candidates.append(os.path.join(here, "config.json"))
            candidates.append(cwd_cfg)
        else:
            # 命令行习惯: 当前目录优先 (python run.py 时 ./config.json 说了算)
            candidates.append(cwd_cfg)
            candidates.append(os.path.join(here, "config.json"))
        candidates.append(os.path.join(here, "config.example.json"))

    user_cfg: Dict[str, Any] = {}
    used = None
    for cand in candidates:
        if cand and os.path.exists(cand):
            try:
                with open(cand, "r", encoding="utf-8") as fh:
                    user_cfg = json.load(fh)
                used = cand
                break
            except Exception as exc:
                raise RuntimeError("配置文件解析失败 %s: %s" % (cand, exc))

    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg)
    # 手改配置写成 "pdf_folders": null (或者写成个字符串) 时, _deep_merge 会
    # 原样留着它 —— 它不是 dict, 走不到递归合并那一支。而消费它的地方写法不齐:
    # 多数是 `for f in (cfg.get("pdf_folders") or [])` (能忍), 但界面那个编辑器
    # 用的是 `setdefault("pdf_folders", [])`, 而 setdefault **不会**替换已存在的
    # None —— 于是 refresh() 里的 for 循环拿到 None, 启动就 TypeError, 表现是
    # "双击 exe 弹个看不懂的错", 而根因在一个手写的 null 上。在入口处统一成列表。
    if not isinstance(cfg.get("pdf_folders"), list):
        cfg["pdf_folders"] = []
    cfg["_config_path"] = used or ""
    resolve_api_key(cfg)
    return cfg


def resolve_api_key(cfg: Dict[str, Any]) -> str:
    """确定实际使用的 API key: 配置里的优先, 否则读环境变量。"""
    ai = cfg.get("ai", {})
    key = (ai.get("api_key") or "").strip()
    if not key:
        env_name = ai.get("api_key_env") or "ARXIV_REC_API_KEY"
        key = (os.environ.get(env_name) or "").strip()
    ai["_resolved_key"] = key
    return key


def has_ai(cfg: Dict[str, Any]) -> bool:
    """是否具备调用 AI 的条件。"""
    return bool(resolve_api_key(cfg))


def is_frozen() -> bool:
    """是不是打包成 exe 在跑 (PyInstaller 会设 ``sys.frozen``)。"""
    return bool(getattr(sys, "frozen", False))


def project_root() -> str:
    """程序"自己的"根目录 —— 配置、索引、输出、缓存都相对它解析。

    打包成 exe 之后 ``__file__`` 指向 PyInstaller 解包的临时目录 (onefile
    模式下进程一退就被删掉), 继续拿它当根目录的话: config.json 读不到、
    library_index.sqlite 每次启动都要重建、output 里的报告转身就没。
    所以冻结时改成 exe 自己所在的目录 —— 数据跟着 exe 走, 双击即用,
    整个文件夹拷到别的机器上也照样带着库和设置。
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_path(cfg_path_value: str, base: Optional[str] = None) -> str:
    """把配置里的相对路径解析成绝对路径 (相对项目根目录)。"""
    if not cfg_path_value:
        return ""
    if os.path.isabs(cfg_path_value):
        return cfg_path_value
    return os.path.join(base or project_root(), cfg_path_value)


def data_paths(cfg: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """三个"东西存在哪"的位置: ``[(名字, 配置里的原值, 解析后的绝对路径)]``。

    顺序就是设置页上的顺序 (读取记录 / 推荐记录 / 报告目录), 界面和自检都按
    这个顺序摆, 免得同一个问题在两个地方叫两个名字。
    """
    lib = cfg.get("library") or {}
    ana = cfg.get("analysis") or {}
    out = cfg.get("output") or {}
    return [
        ("读取记录", str(lib.get("index_db") or "library_index.sqlite"),
         resolve_path(lib.get("index_db") or "library_index.sqlite")),
        ("推荐记录", str(ana.get("history_db") or "recommend_history.sqlite"),
         resolve_path(ana.get("history_db") or "recommend_history.sqlite")),
        ("报告目录", str(out.get("dir") or "output"),
         resolve_path(out.get("dir") or "output")),
    ]


def check_data_paths(cfg: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """查这三个位置还在不在, 返回 ``[(名字, 原值, 绝对路径, 说明)]``。

    检查的是"**这个位置还能用吗**", 不是"文件在不在" —— 这两个差别很关键:

      * 读取记录和推荐记录是**程序自己建的** sqlite 文件, 全新安装时本来就
        不存在 (第一次跑完才有)。所以查的是它们**所在的目录**在不在。
      * 报告目录同理: 第一次跑之前那个文件夹还没建出来, 但它的父目录在,
        程序一写就建出来了, 这不是问题。

    真正要提醒用户的是"位置不可用": 盘符不对 (U 盘拔了、网盘没挂上)、
    文件夹被删掉或挪走了。这种情况下程序不会报错, 只会静默地把记录写到
    **另一个**地方 (比如相对路径一路退到程序目录), 用户看到的就是"设置里
    明明写了, 怎么不生效" —— 所以要主动提醒。
    """
    bad: List[Tuple[str, str, str]] = []
    for name, raw, path in data_paths(cfg):
        if not path:
            bad.append((name, raw, path, "路径是空的"))
            continue
        if os.path.exists(path):
            continue
        # 不存在: 看它的父目录。父目录在 => 只是还没建出来, 正常。
        parent = os.path.dirname(os.path.abspath(path))
        if os.path.isdir(parent):
            continue
        bad.append((name, raw, path, "上一级目录 %s 不存在" % parent))
    return bad


def describe(cfg: Dict[str, Any]) -> str:
    """生成配置摘要, 用于启动时打印。"""
    from .pdf_library import DEPTH_LABELS, normalize_depth
    ai = cfg.get("ai", {})
    net = cfg.get("network", {})
    key = ai.get("_resolved_key") or ""
    masked = (key[:6] + "..." + key[-4:]) if len(key) > 12 else ("<未设置>" if not key else "***")
    folders = [f for f in (cfg.get("pdf_folders") or [])
               if isinstance(f, dict) and f.get("enabled", True) and f.get("path")]
    return "\n".join([
        "  配置文件   : %s" % (cfg.get("_config_path") or "<内置默认>"),
        "  PDF 目录   : %d 个%s" % (
            len(folders),
            "" if not folders else " (" + "; ".join(
                str(f.get("path")) for f in folders[:3])
                + ("..." if len(folders) > 3 else "") + ")"),
        "  读取深度   : %s" % DEPTH_LABELS[
            normalize_depth(cfg.get("library", {}).get("read_depth"))],
        "  AI 服务    : %s (%s)" % (ai.get("provider"), ai.get("base_url")),
        "  AI 模型    : %s" % ai.get("model"),
        "  API Key    : %s" % masked,
        "  代理       : %s" % (net.get("proxy") or "直连"),
        "  推荐篇数   : %d" % cfg["analysis"]["top_n"],
    ])
