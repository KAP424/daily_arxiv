"""AI 客户端: 同时支持 OpenAI 兼容接口与 Anthropic 官方接口。

设计要点:
  * 只依赖 requests, 不强制安装各家 SDK;
  * 所有响应落磁盘缓存, 反复调试不重复烧 token;
  * 统一 ``chat()`` 与 ``chat_json()``, 上层模块不关心 provider 差异。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from .config import resolve_api_key
from .utils import (DiskCache, log, proxies_for, retry_call, safe_json_loads)


class AIError(Exception):
    pass


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
        self.api_key = resolve_api_key(cfg)
        self.cache = cache
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._session = None
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
    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
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
             use_cache: bool = True) -> str:
        """发起一次对话, 返回文本回复。"""
        cache_key = json.dumps({
            "p": self.provider, "m": self.model, "t": self.temperature,
            "s": system, "u": user, "j": json_mode,
        }, ensure_ascii=False, sort_keys=True)

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
                  use_cache: bool = True) -> Any:
        """发起对话并把回复解析为 JSON。解析失败返回 ``default``。"""
        text = self.chat(system, user, json_mode=True, use_cache=use_cache)
        parsed = safe_json_loads(text, default=None)
        if parsed is None:
            log("AI 返回的 JSON 解析失败, 原文前 200 字符: %s"
                % (text or "")[:200].replace("\n", " "), "warn")
            return default
        return parsed


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

    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        session = self._session_get()
        endpoint = self._endpoint()
        headers = {
            "Authorization": "Bearer %s" % self.api_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        resp = session.post(endpoint, headers=headers, data=body,
                            timeout=self.timeout,
                            proxies=proxies_for(session, endpoint))

        if resp.status_code != 200:
            # 有些服务不支持 response_format, 去掉后重试一次
            if json_mode and resp.status_code in (400, 422):
                payload.pop("response_format", None)
                resp = session.post(
                    endpoint, headers=headers,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=self.timeout,
                    proxies=proxies_for(session, endpoint),
                )
            if resp.status_code != 200:
                raise AIError("AI 接口返回 %d: %s"
                              % (resp.status_code, resp.text[:400]))

        try:
            data = resp.json()
        except Exception as exc:
            raise AIError("AI 响应不是合法 JSON: %s" % exc)

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

    def _raw_chat(self, system: str, user: str, json_mode: bool) -> str:
        # Anthropic 没有 response_format, 用提示词约束 JSON
        if json_mode:
            system = system + "\n\n只输出一个合法的 JSON 对象, 不要任何解释文字或 markdown 围栏。"

        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

        session = self._session_get()
        endpoint = self._endpoint()
        resp = session.post(
            endpoint,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
            proxies=proxies_for(session, endpoint),
        )
        if resp.status_code != 200:
            raise AIError("AI 接口返回 %d: %s" % (resp.status_code, resp.text[:400]))

        try:
            data = resp.json()
        except Exception as exc:
            raise AIError("AI 响应不是合法 JSON: %s" % exc)

        usage = data.get("usage") or {}
        self._add_usage(int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0))

        blocks = data.get("content") or []
        parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        return "".join(parts)


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

    def chat(self, system: str, user: str, json_mode: bool = False,
             use_cache: bool = True) -> str:
        return ""

    def chat_json(self, system: str, user: str, default: Any = None,
                  use_cache: bool = True) -> Any:
        return default
