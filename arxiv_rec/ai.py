"""AI 客户端: 同时支持 OpenAI 兼容接口与 Anthropic 官方接口。

设计要点:
  * 只依赖 requests, 不强制安装各家 SDK;
  * 所有响应落磁盘缓存, 反复调试不重复烧 token;
  * 统一 ``chat()`` 与 ``chat_json()``, 上层模块不关心 provider 差异;
  * ``chat()`` 可以走流式 (给了 ``on_delta`` / ``stop_event`` 时), 好让界面上
    那场讨论能实时看到推理过程、也能中途掐掉。**只有讨论用流式** —— 打分和深度
    解读是多线程并发跑的, 流式对它们没意义。
"""

from __future__ import annotations

import json
import threading
import time
from typing import (Any, Callable, Dict, Iterable, List, Optional, Set, Tuple)

from .config import resolve_api_key
from .utils import (DiskCache, log, proxies_for, retry_call, safe_json_loads)


class AIError(Exception):
    pass


class AIRejected(AIError):
    """接口明确拒绝了这次请求 (400/422 之类)。

    单独一类是为了**不重试**: 请求体有问题, 原样再发一遍结果还是一样, 重试三次
    只是把等待拖长三倍。带 ``status`` 是为了让退让逻辑能照着状态码判断, 而不是
    去错误文本里抠数字 —— 响应体里正好出现 "400" 这几个字符太容易了。
    """

    def __init__(self, status: int, body: str = ""):
        self.status = status
        self.body = body
        super().__init__("AI 接口返回 %d: %s" % (status, body[:400]))


class AIStopped(AIError):
    """用户中途点了"停止"。

    和别的 AIError 分开是有用的: 它不是出错, 界面不该把它报成"讨论失败", 更
    不该被重试 (见 ``chat`` 里那个 ``can_retry``)。
    """


# --------------------------------------------------------------------------
# 思考程度
# --------------------------------------------------------------------------
# 界面上那个下拉的取值。留空 = 什么都不发, 让服务端按自己的默认来 —— 也就是这
# 一项加进来之前的行为, 所以它是默认值。
EFFORT_LEVELS = ("", "none", "low", "medium", "high")

# 档位的中文名。界面和日志共用这一份, 免得同一个档位在两处叫两个名字。
EFFORT_LABELS = {
    "": "默认 (接口自己决定)",
    "none": "不思考",
    "low": "低",
    "medium": "中",
    "high": "高",
}

# 反查表: 界面上的中文名 → 存进配置的值。
#
# 为什么界面上的变量存的是**中文名**而不是值: ttk.Combobox 显示的就是它
# textvariable 里的那个字符串。要让它显示"不思考"而不是"none", 变量里就得是
# "不思考"。设置页和推荐页两个下拉共用这一个变量, 于是两边自动同步。
EFFORT_BY_LABEL = {label: value for value, label in EFFORT_LABELS.items()}


def effort_value(label: str) -> str:
    """界面上的中文名 → 配置里存的值。认不出来的一律当"默认"。"""
    return EFFORT_BY_LABEL.get(str(label or "").strip(), "")


# Anthropic 的 ``thinking.budget_tokens``: 各档给多少思考预算。官方要求
# budget_tokens < max_tokens, 且开了 extended thinking 之后 temperature 只能
# 是 1 或干脆不传 —— 这两件事都在 AnthropicClient._payload 里处理。
ANTHROPIC_BUDGET = {"low": 4000, "medium": 10000, "high": 24000}


def parse_stream_event(provider: str, data: Dict[str, Any]) -> Tuple[str, str]:
    """流式响应里的一行 data (已经 json.loads 成 dict) → ``(种类, 文本)``。

    种类是 ``"thinking"`` / ``"answer"`` / ``""`` (这一行没有可显示的文本, 比如
    只有 usage 的收尾块)。

    **纯函数**: 不碰网络、不看任何状态。所以自检可以拿几段固定的字节流喂进来,
    断言思考和正文切得对 —— 流式这条路上最容易错的就是字段名, 而它又最不好手工
    试 (要等一个真的推理模型慢慢想才能看出来)。
    """
    if provider == "anthropic":
        if (data.get("type") or "") != "content_block_delta":
            return ("", "")
        delta = data.get("delta") or {}
        kind = delta.get("type") or ""
        if kind == "thinking_delta":
            return ("thinking", delta.get("thinking") or "")
        if kind == "text_delta":
            return ("answer", delta.get("text") or "")
        return ("", "")
    # OpenAI 兼容。推理模型的思考放在 reasoning_content 里, 但也有网关叫
    # reasoning —— 两个都认, 认错了最坏也只是把思考当成正文显示出来。
    choices = data.get("choices") or []
    if not choices:
        return ("", "")
    delta = choices[0].get("delta") or {}
    think = delta.get("reasoning_content")
    if think is None:
        think = delta.get("reasoning")
    if think:
        return ("thinking", think)
    text = delta.get("content")
    if text:
        return ("answer", text)
    return ("", "")


def stream_usage(provider: str, data: Dict[str, Any]) -> Tuple[int, int]:
    """从流式事件里抠出 token 用量, 返回 ``(输入, 输出)``。

    各家给的位置不一样, 而且不在同一个块里: OpenAI 兼容接口把 usage 挂在最后那
    个 chunk 上; Anthropic 是 message_start 给输入、message_delta 给输出。所以
    这里返回的是**增量**, 由调用方累加。
    """
    if provider == "anthropic":
        typ = data.get("type") or ""
        if typ == "message_start":
            usage = (data.get("message") or {}).get("usage") or {}
            return (int(usage.get("input_tokens") or 0), 0)
        if typ == "message_delta":
            usage = data.get("usage") or {}
            return (0, int(usage.get("output_tokens") or 0))
        return (0, 0)
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return (0, 0)
    return (int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0))


def stream_error(data: Dict[str, Any]) -> str:
    """流里夹带的错误 (Anthropic 的 error 事件 / OpenAI 的 error 字段)。

    流式请求的 HTTP 状态码在**开流的那一刻**就定了, 之后的报错只能靠流里的这种
    事件传达 —— 不看它就会把一条半截回复当成正常答完。
    """
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    if isinstance(err, str):
        return err
    if (data.get("type") or "") == "error":
        return str(data)[:300]
    return ""


# --------------------------------------------------------------------------
# 基类
# --------------------------------------------------------------------------
class AIClient:
    """AI 客户端基类。子类只需实现 ``_raw_chat``。"""

    def __init__(self, cfg: Dict[str, Any], cache: Optional[DiskCache] = None):
        ai = cfg.get("ai", {})
        self.cfg = cfg
        self.provider = (ai.get("provider") or "openai").lower()
        self.base_url = (ai.get("base_url") or "").rstrip("/")
        self.model = ai.get("model") or ""
        self.temperature = float(ai.get("temperature", 0.3))
        self.max_tokens = int(ai.get("max_tokens", 4096))
        self.timeout = int(ai.get("timeout", 240))
        self.max_retries = int(ai.get("max_retries", 3))
        # 思考程度。手写配置里写了个不认识的值时按"默认"处理并说一声 —— 悄悄
        # 当成默认的话, 用户会以为档位生效了, 只是效果不明显。
        effort = str(ai.get("reasoning_effort") or "").strip().lower()
        if effort not in EFFORT_LEVELS:
            log("ai.reasoning_effort 的值 %r 不认识, 这次按「默认」处理 (可选: %s)"
                % (effort, " / ".join(x or "默认" for x in EFFORT_LEVELS)), "warn")
            effort = ""
        self.reasoning_effort = effort
        # 逃生口: 直接并进请求体的额外字段。不是 dict 就当没写 —— 手写 JSON 写
        # 成字符串/数组是常事, 那会让 requests 那边报一个和根因毫无关系的错。
        extra = ai.get("extra_body")
        self.extra_body = dict(extra) if isinstance(extra, dict) else {}
        if extra and not isinstance(extra, dict):
            log("ai.extra_body 应该是 JSON 对象, 这次忽略", "warn")
        self.api_key = resolve_api_key(cfg)
        self.cache = cache
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._session = None
        # 已经在流里吐出来的字符数。重试与否看它 —— 见 chat 里那个 can_retry。
        self._emitted = 0
        # 这个接口认不认 stream: None = 还不知道, True/False = 试出来的结论。
        # 记下来是因为不认的话每轮都要先撞一次 400 才知道, 白等一个来回, 而且
        # 那行"不支持流式"的警告会刷满日志 —— 说一次就够了。
        self._stream_ok: Optional[bool] = None
        # 打分和深度解读都是多线程并发调用同一个客户端。计数器和 session 的懒
        # 初始化都得加锁 —— 前者是 `+=` 不是原子的 (并发下会丢计数, 报告里的
        # token 统计会偏小), 后者两个线程同时进来会各自建一个 session。
        self._lock = threading.Lock()
        if not self.api_key:
            raise AIError(
                "未配置 API key。请在 config.json 的 ai.api_key 填写, "
                "或设置环境变量 %s" % (ai.get("api_key_env") or "ARXIV_REC_API_KEY")
            )
        if not self.base_url:
            raise AIError("未配置 ai.base_url")

    # -- 子类实现 --------------------------------------------------------
    # 请求体的哪些字段属于"服务端不认就去掉"的可选项, 按最可能多余的排在前面。
    # 退让重发时按这个顺序**逐个**去掉 (见 _post) —— 一刀切全去掉会把本来能用
    # 的那个也丢掉, 而不同网关认的和不认的恰好不一样。
    DROPPABLE = ("response_format", "reasoning_effort", "thinking")

    def _payload(self, system: str, user: str, json_mode: bool,
                 skip: Iterable[str] = ()) -> Dict[str, Any]:
        """本次请求的请求体。``skip`` 里的字段不放进去 (400/422 退让时用)。"""
        raise NotImplementedError

    def _headers(self) -> Dict[str, str]:
        raise NotImplementedError

    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
        raise NotImplementedError

    def _parse_body(self, data: Dict[str, Any]) -> str:
        """把**非流式**响应体解析成正文, 顺带记账 token。"""
        raise NotImplementedError

    def _endpoint(self) -> str:
        raise NotImplementedError

    # -- 公共接口 --------------------------------------------------------
    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _session_get(self):
        if self._session is None:
            with self._lock:
                if self._session is None:
                    from .utils import build_session
                    self._session = build_session(
                        self.cfg.get("network", {}).get("proxy"),
                        timeout=self.timeout,
                    )
        return self._session

    def _add_usage(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """累加 token 用量 (并发安全)。``_raw_chat`` 里调, 只管 token 不管次数。"""
        with self._lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

    def chat(self, system: str, user: str, json_mode: bool = False,
             use_cache: bool = True,
             on_delta: Optional[Callable[[str, str, float], None]] = None,
             stop_event: Optional[threading.Event] = None) -> str:
        """发起一次对话, 返回文本回复 (流式时返回正文全文)。

        ``on_delta`` / ``stop_event`` 给了就走流式: 每收到一小段回调一次
        ``on_delta(种类, 这一小段, 已经过了几秒)``, 种类是 ``"thinking"`` 或
        ``"answer"``; 期间不断检查 ``stop_event``, 置位就掐断连接并抛
        ``AIStopped``。两个都不给就是原来的阻塞式调用, 一行都没变 —— 打分和
        深度解读走的就是那条路。

        流式的结果**不进磁盘缓存**: 它每次的提示词都不一样 (带着整场讨论),
        缓存只会白占地方。
        """
        if on_delta is None and stop_event is None:
            return self._chat_blocking(system, user, json_mode, use_cache)

        if self._stream_ok is False:
            # 已经试过这个接口不认 stream, 别再撞一次。这时"停止"就是"放弃等待"
            # (见下面那段 warn), 界面那边知道该怎么措辞。
            return self._chat_blocking(system, user, json_mode, use_cache=False)

        self._emitted = 0
        started = time.monotonic()

        def _once() -> str:
            resp = self._post(system, user, json_mode, stream=True)
            try:
                self._ensure_ok(resp)
                # 200 不等于真的在流。有的网关会把 stream 当没看见, 直接回一份
                # 完整 JSON —— 那种响应喂给 _read_stream 一行 data: 都读不到,
                # 会变成"答完了但是空的"。认出来就按阻塞式的结果用。
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in ctype:
                    self._stream_ok = False
                    log("这个接口没有回事件流 (Content-Type: %s), 按普通响应处理"
                        % (ctype or "空"), "warn")
                    return self._parse_body(resp.json())
                self._stream_ok = True
                # SSE 的 Content-Type 不带 charset。不指定的话 requests 按
                # ISO-8859-1 解, 中文全变乱码 —— 而且只在流式这条路上出问题,
                # 阻塞式那边是 resp.json() 自己按 UTF-8 解, 所以对照不出来。
                resp.encoding = "utf-8"
                return self._read_stream(resp, on_delta, stop_event, started)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        def _can_retry(exc: BaseException) -> bool:
            # 用户点了停止就不重试 (那不是失败)。接口明确拒绝也不重试 —— 原样再
            # 发一遍结果一样, 白等三轮。已经往界面上吐过字的更不重试: 重来一遍
            # 会在对话区里留下两段半截答案, 比直接报错难看得多。
            if isinstance(exc, (AIStopped, AIRejected)):
                return False
            return self._emitted == 0

        try:
            text = retry_call(_once, retries=self.max_retries, base_delay=3.0,
                              label="AI 流式调用", can_retry=_can_retry)
        except AIStopped:
            raise
        except AIError as exc:
            # 第二层退让: 万一网关不认 stream, 别让"停止"变成个坏按钮。
            #
            # 判断"是不是 stream 惹的"不靠抠错误文本 (响应体里正好出现 400 这几个
            # 字符太容易了), 而是**再发一次不带 stream 的**: 成了就说明确实是
            # stream 的问题, 而且这一发不是白费的 —— 拿到的就是这一轮的答案。
            # 失败就说明跟 stream 无关, 把原来那个错抛出去, 别拿第二个错盖住它。
            if self._emitted:
                raise
            try:
                text = self._raw_chat(system, user, json_mode)
            except Exception:
                raise exc
            log("这个接口不支持流式 (%s), 已改回阻塞式请求。停止按钮从此只是"
                "「放弃等待」: 后台这次请求会跑完, 结果会被丢掉。" % exc, "warn")
            with self._lock:
                self.calls += 1
            return text
        with self._lock:
            self.calls += 1
        return text

    def _chat_blocking(self, system: str, user: str, json_mode: bool,
                       use_cache: bool) -> str:
        """原来的那条路: 一次请求, 等整份回复。"""
        key_fields = {
            "p": self.provider, "m": self.model, "t": self.temperature,
            "s": system, "u": user, "j": json_mode,
        }
        # 思考档位和 extra_body 也算输入: 变了答案就不一样, 不带上它们的话改了档位
        # 会命中旧档位的缓存, 用户看到的是"改了没反应"。
        #
        # 但**默认档不加这两个键** —— 加了这个功能之前算出来的键是六个字段, 多两个
        # 字段会让每一份老缓存都失效: 用户只是升级了个补丁版本, 下一次跑推荐却要
        # 把几百次 AI 调用重打一遍。所以只在真的偏离默认值时才把它们并进键里 ——
        # 那样键才变成新的, 而"改了档位缓存就失效"这条行为一点没丢。
        if self.reasoning_effort:
            key_fields["e"] = self.reasoning_effort
        if self.extra_body:
            key_fields["x"] = self.extra_body
        cache_key = json.dumps(key_fields, ensure_ascii=False, sort_keys=True)

        if self.cache is not None and use_cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                log("AI 命中缓存 (%d 字符)" % len(hit), "dbg")
                return hit

        def _do() -> str:
            return self._raw_chat(system, user, json_mode)

        text = retry_call(_do, retries=self.max_retries, base_delay=3.0, label="AI 调用")
        with self._lock:
            self.calls += 1

        if self.cache is not None and use_cache and text:
            self.cache.set(cache_key, text)
        return text

    def chat_json(self, system: str, user: str, default: Any = None,
                  use_cache: bool = True,
                  stop_event: Optional[threading.Event] = None) -> Any:
        """发起对话并把回复解析为 JSON。解析失败返回 ``default``。

        ``stop_event`` 是给「用对话更新详解」用的: 它要的是一份 JSON, 流式显示没
        意义 (半截 JSON 看不出名堂), 但**能中途停掉**有意义 —— 那也是一次可能
        要想很久的调用。所以那边只传 stop_event, 不传 on_delta。
        """
        text = self.chat(system, user, json_mode=True, use_cache=use_cache,
                         stop_event=stop_event)
        parsed = safe_json_loads(text, default=None)
        if parsed is None:
            log("AI 返回的 JSON 解析失败, 原文前 200 字符: %s"
                % (text or "")[:200].replace("\n", " "), "warn")
            return default
        return parsed

    # -- HTTP 层 (阻塞与流式共用) ----------------------------------------
    def _apply_extra(self, payload: Dict[str, Any],
                     skip: Iterable[str] = ()) -> None:
        """把 ``ai.extra_body`` 并进请求体, 原地改 ``payload``。

        放在**档位之后**调用, 所以手写的字段能覆盖自动生成的那个 —— 网关有自己的
        说法时以用户的为准。``skip`` 里的不放 (退让重发用)。
        """
        for key, value in self.extra_body.items():
            if key not in skip:
                payload[key] = value

    def _optional_keys(self, payload: Dict[str, Any]) -> List[str]:
        """这份请求体里「服务端不认就可以去掉」的字段。"""
        keys = [k for k in self.DROPPABLE if k in payload]
        keys += [k for k in sorted(self.extra_body) if k in payload]
        return keys

    def _post(self, system: str, user: str, json_mode: bool,
              stream: bool = False):
        """发一次请求, 必要时退让重发。返回 Response (**调用方负责 close**)。

        退让: 400/422 时按 ``_optional_keys`` 的顺序**逐个**去掉字段重发 —— 先
        整份发, 再去掉最可能多余的那个, 再两个都去掉。逐个去而不是一刀切全去,
        是因为不同网关认的和不认的恰好不一样: 有的认 response_format 不认思考档
        位, 有的反过来, 全去掉就把本来能用的那个也丢了。
        """
        session = self._session_get()
        endpoint = self._endpoint()
        headers = self._headers()
        url_proxies = proxies_for(session, endpoint)

        plans: List[Set[str]] = [set()]
        for key in self._optional_keys(self._payload(system, user, json_mode)):
            plans.append(plans[-1] | {key})

        resp = None
        for i, skip in enumerate(plans):
            payload = self._payload(system, user, json_mode, skip)
            if stream:
                payload["stream"] = True
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            resp = session.post(endpoint, headers=headers, data=body,
                                timeout=self.timeout, stream=stream,
                                proxies=url_proxies)
            if resp.status_code == 200:
                return resp
            if resp.status_code not in (400, 422) or i == len(plans) - 1:
                break
            try:
                # stream=True 时响应体还没读, 不关掉就把这条连接留在池子里
                resp.close()
            except Exception:
                pass
            for key in sorted(plans[i + 1] - skip):
                log("接口不接受 %s, 已去掉重试 (%s)" % (key, self.provider), "warn")
        return resp

    @staticmethod
    def _ensure_ok(resp) -> None:
        if resp.status_code != 200:
            # 400/422 是"这次请求本身不对", 重试无意义 → AIRejected (不重试)。
            # 其余 (429/5xx/连接被掐) 是可能自己好的, 归 AIError, 交给 retry_call。
            if resp.status_code in (400, 422):
                raise AIRejected(resp.status_code, resp.text)
            raise AIError("AI 接口返回 %d: %s"
                          % (resp.status_code, resp.text[:400]))

    # 流式 ----------------------------------------------------------
    # 消化 SSE 的一行, 返回 None。只认 data: 开头的那种。
    def _consume_line(self, line, think, answer, usage, on_delta, started):
        line = line.strip()
        # 空行 = 事件分隔; ":" 开头 = 心跳/注释; "event: xxx" 用不上 —— 事件类型
        # 在 data 的 JSON 里 (两种协议都是), 再解析一遍只是多一处会走岔的地方。
        if not line or line.startswith(":") or not line.startswith("data:"):
            return
        text = line[5:].strip()
        if not text:
            return
        # 半截 JSON 之类的脏行, 跳过就好, 不值得打断整场回答。
        try:
            data = json.loads(text)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        err = stream_error(data)
        if err:
            raise AIError("流里报错: %s" % err)
        p, c = stream_usage(self.provider, data)
        usage[0] += p
        usage[1] += c
        kind, piece = parse_stream_event(self.provider, data)
        if not piece:
            return
        if kind == "answer":
            answer.append(piece)
            self._emitted += len(piece)
        else:
            think.append(piece)
        if on_delta is not None:
            on_delta(kind, piece, time.monotonic() - started)

    def _read_stream(self, resp, on_delta, stop_event, started: float) -> str:
        '''读 SSE, 边读边喂 on_delta, 返回正文全文 (不含思考)。'''
        think: List[str] = []
        answer: List[str] = []
        usage = [0, 0]
        # 按行收, 不自己切: SSE 的一行就是一个事件, iter_lines 会把跨 chunk 的
        # 半行攒起来再给, 中文也不会被从中间切开 (它用的是增量解码器)。
        #
        # 停止检查只能放在**每一行**上 —— 也就是每收到一个事件查一次。这意味着
        # "点了停止"的生效时刻是**下一个事件到达时**, 不是立刻。大多数时候这够用
        # (思考是一段一段吐的, 行与行之间间隔很短), 但推理模型在开口之前可能先
        # 静默几十秒, 那段时间里这一行就卡在 recv 上, 停止信号要等它吐出第一个
        # 字节才被看见。界面那边因此**先**把按钮改成「停止中 …」并写一行日志,
        # 让用户知道请求收到了、在等下一个检查点, 而不是"按钮没反应"。
        #
        # 试过拿 socket 读超时当心跳 (超时 → 查一次停止 → 接着读), 这条路**走不通**:
        # 实测 requests 的 read timeout 一触发就把整个连接废掉, 再读只会拿到
        # ConnectionError (服务端那边看到的是 WinError 10053 连接被中止)。所以
        # 想要"静默期也能立刻停"就得从另一个线程去关 socket, 那正是这套代码刻意
        # 避开的东西 (见 stop_chat 的注释)。这里选择保持简单: 只在事件边界检查。
        for line in resp.iter_lines(chunk_size=256, decode_unicode=True):
            if stop_event is not None and stop_event.is_set():
                raise AIStopped("已停止")
            self._consume_line(line or "", think, answer, usage, on_delta, started)
        self._add_usage(usage[0], usage[1])
        return "".join(answer)


# --------------------------------------------------------------------------
# OpenAI 兼容
# --------------------------------------------------------------------------
class OpenAIClient(AIClient):
    """适配 DeepSeek / Qwen / Kimi / 智谱 / OpenAI / vLLM / Ollama 等。"""

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/v1/chat/completions"

    def _payload(self, system: str, user: str, json_mode: bool,
                 skip: Iterable[str] = ()) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode and "response_format" not in skip:
            payload["response_format"] = {"type": "json_object"}
        # 思考档位。DeepSeek 收 none/low/high/max, 并且认别名 minimal→low、
        # medium→high —— 所以"中"在它那儿等同"高"。别的兼容接口未必认这个字段,
        # 不认就会 400, 由 _post 去掉重发。
        if self.reasoning_effort and "reasoning_effort" not in skip:
            payload["reasoning_effort"] = self.reasoning_effort
        self._apply_extra(payload, skip)
        return payload

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }

    def _parse_body(self, data: Dict[str, Any]) -> str:
        usage = data.get("usage") or {}
        self._add_usage(int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0))

        choices = data.get("choices") or []
        if not choices:
            raise AIError("AI 响应缺少 choices: %s" % str(data)[:300])
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content is None:
            # 某些推理模型把正文放在 reasoning_content
            content = msg.get("reasoning_content") or ""
        return content or ""

    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
        resp = self._post(system, user, json_mode)
        try:
            self._ensure_ok(resp)
            try:
                data = resp.json()
            except Exception as exc:
                raise AIError("AI 响应不是合法 JSON: %s" % exc)
            return self._parse_body(data)
        finally:
            try:
                resp.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------
class AnthropicClient(AIClient):
    """Claude 官方 /v1/messages 接口。"""

    def _endpoint(self) -> str:
        base = self.base_url
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"

    def _payload(self, system: str, user: str, json_mode: bool,
                 skip: Iterable[str] = ()) -> Dict[str, Any]:
        # Anthropic 没有 response_format, 用提示词约束 JSON
        if json_mode:
            system = system + "\n\n只输出一个合法的 JSON 对象, 不要任何解释文字或 markdown 围栏。"

        max_tokens = self.max_tokens
        thinking = None
        if self.reasoning_effort and "thinking" not in skip:
            if self.reasoning_effort == "none":
                thinking = {"type": "disabled"}
            else:
                budget = ANTHROPIC_BUDGET.get(self.reasoning_effort)
                if budget:
                    # 官方要求 budget_tokens < max_tokens, 且思考本身也算进
                    # max_tokens。预算比 max_tokens 还大就会被拒, 所以这里把
                    # max_tokens 抬上去 —— 不抬的话用户选了"高"反而报 400。
                    want = budget + 4096
                    if max_tokens < want:
                        log("思考档位「%s」需要更大的 max_tokens, 本次临时从 %d 提到 %d"
                            % (EFFORT_LABELS.get(self.reasoning_effort,
                                                 self.reasoning_effort),
                               max_tokens, min(want, 64000)), "dbg")
                        max_tokens = min(want, 64000)
                    thinking = {"type": "enabled", "budget_tokens": budget}

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # 开了 extended thinking 之后 temperature 只能是 1, 传别的值官方直接
        # 报错 —— 所以开思考时干脆不传, 让服务端用它该用的那个值。
        if thinking is None or thinking.get("type") != "enabled":
            payload["temperature"] = self.temperature
        if thinking is not None:
            payload["thinking"] = thinking
        self._apply_extra(payload, skip)
        return payload

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _parse_body(self, data: Dict[str, Any]) -> str:
        usage = data.get("usage") or {}
        self._add_usage(int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0))

        blocks = data.get("content") or []
        parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        return "".join(parts)

    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
        resp = self._post(system, user, json_mode)
        try:
            self._ensure_ok(resp)
            try:
                data = resp.json()
            except Exception as exc:
                raise AIError("AI 响应不是合法 JSON: %s" % exc)
            return self._parse_body(data)
        finally:
            try:
                resp.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def build_client(cfg: Dict[str, Any], cache: Optional[DiskCache] = None) -> AIClient:
    """按配置构造 AI 客户端。"""
    provider = (cfg.get("ai", {}).get("provider") or "openai").lower()
    if provider in ("openai", "openai-compatible", "compatible", "deepseek", "qwen"):
        return OpenAIClient(cfg, cache)
    if provider in ("anthropic", "claude"):
        return AnthropicClient(cfg, cache)
    raise AIError("未知的 ai.provider: %s (可选 openai / anthropic)" % provider)


class NullAIClient:
    """无 AI 时的降级实现: 全部返回空, 让流程能跑完 (纯启发式模式)。"""

    provider = "none"
    model = "none"
    calls = 0
    prompt_tokens = 0
    completion_tokens = 0

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        self.cfg = cfg or {}

    @property
    def total_tokens(self) -> int:
        return 0

    reasoning_effort = ""

    def chat(self, system: str, user: str, json_mode: bool = False,
             use_cache: bool = True, on_delta=None, stop_event=None) -> str:
        return ""

    def chat_json(self, system: str, user: str, default: Any = None,
                  use_cache: bool = True, stop_event=None) -> Any:
        return default
