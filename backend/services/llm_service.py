"""
LLM服务模块
支持多种LLM Provider: Ollama, OpenAI, Claude
"""
import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import aiohttp

from ..config import config

logger = logging.getLogger(__name__)


def _dedupe_text_list(items: List[str], limit: int = 12) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        text = str(item or '').strip()
        if not text:
            continue
        key = re.sub(r'\s+', ' ', text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_todo(item: Any) -> Optional[Dict[str, str]]:
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return {"content": content, "assignee": "", "deadline": ""}

    if isinstance(item, dict):
        content = str(item.get("content") or item.get("task") or item.get("todo") or '').strip()
        if not content:
            return None
        return {
            "content": content,
            "assignee": str(item.get("assignee") or item.get("owner") or '').strip(),
            "deadline": str(item.get("deadline") or item.get("due_date") or '').strip(),
        }

    return None


def _normalize_decision(item: Any) -> Optional[Dict[str, str]]:
    if isinstance(item, str):
        content = item.strip()
        if not content:
            return None
        return {"content": content, "vote_result": ""}

    if isinstance(item, dict):
        content = str(item.get("content") or item.get("decision") or '').strip()
        if not content:
            return None
        return {
            "content": content,
            "vote_result": str(item.get("vote_result") or item.get("result") or '').strip()
        }

    return None


def _dedupe_dict_items(items: List[Dict[str, str]], key_field: str, limit: int = 20) -> List[Dict[str, str]]:
    result: List[Dict[str, str]] = []
    seen = set()
    for item in items:
        key = re.sub(r'\s+', ' ', str(item.get(key_field, '')).strip())
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _extract_json_candidates(text: str) -> List[str]:
    candidates: List[str] = []

    # 优先提取 markdown json 代码块
    for match in re.finditer(r'```(?:json)?\s*([\s\S]*?)```', text, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if block.startswith('{') or block.startswith('['):
            candidates.append(block)

    # 回退：提取正文里的平衡 JSON 对象
    depth = 0
    start = None
    in_string = False
    escaped = False
    for idx, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == '{':
            if depth == 0:
                start = idx
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start:idx + 1].strip()
                    if len(chunk) <= 30000:
                        candidates.append(chunk)
                    start = None

    # 去重（保序）
    uniq: List[str] = []
    seen = set()
    for item in candidates:
        key = item.strip()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def _extract_structured_data(text: str) -> Dict[str, Any]:
    data = {
        "key_points": [],
        "todos": [],
        "decisions": []
    }

    for candidate in _extract_json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue

        if isinstance(parsed, dict):
            key_points = parsed.get("key_points") or parsed.get("keypoints") or parsed.get("highlights") or []
            todos = parsed.get("todos") or parsed.get("todo_items") or []
            decisions = parsed.get("decisions") or []

            if isinstance(key_points, list):
                data["key_points"].extend(str(item).strip() for item in key_points if str(item).strip())
            if isinstance(todos, list):
                data["todos"].extend(todos)
            if isinstance(decisions, list):
                data["decisions"].extend(decisions)

    return data


def _strip_structured_blocks(text: str) -> str:
    cleaned = re.sub(r'```(?:json)?[\s\S]*?```', '', text, flags=re.IGNORECASE)
    cleaned = re.split(r'(?:待办事项?\s*\(?json\)?|决策\s*\(?json\)?)', cleaned, maxsplit=1, flags=re.IGNORECASE)[0]
    return cleaned.strip()


def _extract_key_points_from_text(text: str) -> List[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    items: List[str] = []
    in_keypoint_section = False

    for line in lines:
        if re.search(r'(关键要点|会议要点|要点|重点)', line, flags=re.IGNORECASE):
            in_keypoint_section = True
            continue
        if in_keypoint_section and re.search(r'(待办|行动项|决策|json)', line, flags=re.IGNORECASE):
            in_keypoint_section = False
            continue

        is_bullet = bool(re.match(r'^(?:[-*•·]\s*|\d+[.)、]\s*)', line))
        if not is_bullet:
            continue

        item = re.sub(r'^(?:[-*•·]\s*|\d+[.)、]\s*)', '', line).strip()
        if not item:
            continue

        if in_keypoint_section or len(item) >= 4:
            items.append(item)

    return _dedupe_text_list(items, limit=10)


def _extract_todos_from_text(text: str) -> List[Dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    todos: List[Dict[str, str]] = []
    in_todo_section = False

    for line in lines:
        if re.search(r'(待办事项?|action\s*items?)', line, flags=re.IGNORECASE):
            in_todo_section = True
            continue
        if in_todo_section and re.search(r'(决策|要点|总结|json)', line, flags=re.IGNORECASE):
            in_todo_section = False
            continue

        content = ''
        if '[待办]' in line:
            content = line.split('[待办]', 1)[1].strip()
        elif in_todo_section and re.match(r'^(?:[-*•·]\s*|\d+[.)、]\s*)', line):
            content = re.sub(r'^(?:[-*•·]\s*|\d+[.)、]\s*)', '', line).strip()

        if not content:
            continue

        todo = _normalize_todo(content)
        if todo:
            todos.append(todo)

    return _dedupe_dict_items(todos, key_field="content", limit=20)


def _extract_decisions_from_text(text: str) -> List[Dict[str, str]]:
    cleaned = re.sub(r'```[\s\S]*?```', '', text, flags=re.IGNORECASE)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    decisions: List[Dict[str, str]] = []

    for line in lines:
        if re.search(r'json', line, flags=re.IGNORECASE):
            continue
        if line.startswith('{') or line.startswith('}') or line.startswith('"'):
            continue

        content = ''
        if '[决策]' in line:
            content = line.split('[决策]', 1)[1].strip()
        elif re.search(r'(决策|决定|通过|同意|确认)', line) and len(line) >= 8:
            content = line

        if not content:
            continue

        decision = _normalize_decision(content)
        if decision:
            decisions.append(decision)

    return _dedupe_dict_items(decisions, key_field="content", limit=20)


def _parse_llm_response(response: str) -> Dict[str, Any]:
    text = str(response or '').strip()
    structured = _extract_structured_data(text)
    summary_text = _strip_structured_blocks(text) or text

    key_points = _dedupe_text_list(structured["key_points"] + _extract_key_points_from_text(summary_text), limit=10)

    todos: List[Dict[str, str]] = []
    for item in structured["todos"]:
        normalized = _normalize_todo(item)
        if normalized:
            todos.append(normalized)
    todos.extend(_extract_todos_from_text(text))
    todos = _dedupe_dict_items(todos, key_field="content", limit=20)

    decisions: List[Dict[str, str]] = []
    for item in structured["decisions"]:
        normalized = _normalize_decision(item)
        if normalized:
            decisions.append(normalized)
    decisions.extend(_extract_decisions_from_text(text))
    decisions = _dedupe_dict_items(decisions, key_field="content", limit=20)

    return {
        'summary': summary_text,
        'key_points': key_points,
        'todos': todos,
        'decisions': decisions
    }


def _split_sentences(text: str, limit: int = 80) -> List[str]:
    cleaned = str(text or '').replace('\r', '\n')
    parts = re.split(r'[。！？!?；;\n]+', cleaned)
    sentences: List[str] = []
    for part in parts:
        line = re.sub(r'\s+', ' ', part).strip()
        if len(line) < 2:
            continue
        line = re.sub(r'^[，。！？!?、；;：:,.·~…—\-]+', '', line).strip()
        if not line:
            continue
        sentences.append(line)
        if len(sentences) >= limit:
            break
    return sentences


def _fallback_summarize(text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    options = options or {}
    sentences = _split_sentences(text, limit=120)

    summary = "；".join(sentences[:4]).strip()
    if not summary:
        summary = str(text or '').strip()[:200]
    if len(summary) > 260:
        summary = f"{summary[:257]}..."

    key_points = _dedupe_text_list(sentences[:8], limit=8)

    todos = _extract_todos_from_text(text)
    if not todos:
        for sentence in sentences:
            if re.search(r'(待办|跟进|推进|落实|负责人|截止|完成|排期|安排)', sentence):
                normalized = _normalize_todo(sentence)
                if normalized:
                    todos.append(normalized)
    todos = _dedupe_dict_items(todos, key_field="content", limit=20)

    decisions = _extract_decisions_from_text(text)
    if not decisions:
        for sentence in sentences:
            if re.search(r'(决策|决定|通过|同意|确认|结论)', sentence):
                normalized = _normalize_decision(sentence)
                if normalized:
                    decisions.append(normalized)
    decisions = _dedupe_dict_items(decisions, key_field="content", limit=20)

    if options.get('summary_length') == 'brief' and len(summary) > 120:
        summary = f"{summary[:117]}..."

    return {
        'summary': summary,
        'key_points': key_points,
        'todos': todos,
        'decisions': decisions,
    }


def _normalize_turning_point(item: Any) -> Optional[Dict[str, str]]:
    if isinstance(item, str):
        label = item.strip()
        if not label:
            return None
        return {"label": label, "type": "milestone"}

    if isinstance(item, dict):
        label = str(item.get("label") or item.get("content") or item.get("summary") or "").strip()
        if not label:
            return None
        point_type = str(item.get("type") or item.get("kind") or item.get("category") or "milestone").strip().lower()
        if point_type not in ("milestone", "decision", "action"):
            point_type = "milestone"
        return {"label": label, "type": point_type}

    return None


def _parse_realtime_ai_response(response_text: str) -> Dict[str, Any]:
    raw_text = str(response_text or "").strip()
    parsed: Dict[str, Any] = {}

    for candidate in _extract_json_candidates(raw_text):
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict):
            parsed = obj
            break

    incremental = str(
        parsed.get("incremental")
        or parsed.get("summary")
        or parsed.get("update")
        or raw_text
    ).strip()
    topic = str(parsed.get("topic") or parsed.get("meeting_topic") or "").strip()
    context_summary = str(parsed.get("context_summary") or parsed.get("rolling_summary") or incremental).strip()

    key_points_raw = parsed.get("key_points") or parsed.get("keypoints") or []
    decisions_raw = parsed.get("decisions") or []
    turning_points_raw = parsed.get("turning_points") or parsed.get("milestones") or parsed.get("timeline_points") or []

    key_points = _dedupe_text_list([str(item).strip() for item in key_points_raw if str(item).strip()], limit=8)
    decisions = _dedupe_text_list([
        str(item.get("content") if isinstance(item, dict) else item).strip()
        for item in decisions_raw
        if str(item.get("content") if isinstance(item, dict) else item).strip()
    ], limit=6)

    turning_points: List[Dict[str, str]] = []
    seen = set()
    for item in turning_points_raw:
        normalized = _normalize_turning_point(item)
        if not normalized:
            continue
        key = normalized["label"]
        if key in seen:
            continue
        seen.add(key)
        turning_points.append(normalized)
        if len(turning_points) >= 8:
            break

    if not turning_points:
        for item in key_points[:4]:
            turning_points.append({"label": item, "type": "milestone"})
        for item in decisions[:3]:
            turning_points.append({"label": item, "type": "decision"})

    return {
        "topic": topic,
        "incremental": incremental,
        "turning_points": turning_points[:8],
        "key_points": key_points,
        "decisions": decisions,
        "context_summary": context_summary,
    }


class LLMProvider(ABC):
    """LLM Provider抽象基类"""

    @abstractmethod
    async def summarize(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """总结会议内容"""
        pass

    @abstractmethod
    async def is_available(self) -> bool:
        """检查服务是否可用"""
        pass


class OllamaProvider(LLMProvider):
    """Ollama本地LLM Provider"""

    def __init__(self, model: str = None, api_base: str = None):
        self.model = model or config.llm.get('model', 'qwen2.5:7b')
        self.api_base = api_base or config.llm.get('api_base', 'http://localhost:11434')
        self.summary_config = config.llm.get('summary', {})

    async def _call_api(self, prompt: str) -> str:
        """调用Ollama API"""
        url = f"{self.api_base}/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": self.summary_config.get('max_tokens', 4096),
                "temperature": self.summary_config.get('temperature', 0.7),
            }
        }

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    raise Exception(f"Ollama API error: {response.status}")

                result = await response.json()
                return result.get('response', '')

    async def summarize(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """总结会议内容"""
        options = options or {}

        # 构建提示词
        prompt = self._build_prompt(text, options)

        # 调用LLM
        response_text = await self._call_api(prompt)

        # 解析结果
        return self._parse_response(response_text)

    def _build_prompt(self, text: str, options: Dict[str, Any]) -> str:
        """构建提示词"""
        template = self.summary_config.get(
            'prompt_template',
            "你是一个专业的会议记录助手。请对以下会议内容进行总结，提取关键要点、待办事项和决策。\n\n{text}"
        )

        prompt = template.format(text=text)

        # 添加选项
        if options.get('extract_todos'):
            prompt += "\n\n请特别标注待办事项，格式为：【待办】任务内容 - 负责人（如果有）"

        if options.get('extract_decisions'):
            prompt += "\n\n请特别标注会议决策，格式为：【决策】决策内容"

        if options.get('summary_length') == 'brief':
            prompt += "\n\n请用简洁的语言总结，字数控制在200字以内。"

        return prompt

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """解析LLM响应"""
        return _parse_llm_response(response)

    async def is_available(self) -> bool:
        """检查Ollama是否可用"""
        try:
            url = f"{self.api_base}/api/tags"
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return response.status == 200
        except Exception:
            return False


class OpenAIProvider(LLMProvider):
    """OpenAI API Provider"""

    def __init__(self, model: str = None, api_key: str = None, api_base: str = None):
        self.model = model or config.llm.get('model', 'gpt-4')
        self.api_key = api_key or config.llm.get('api_key', '')
        self.api_base = api_base or config.llm.get('api_base', 'https://api.openai.com/v1')
        self.summary_config = config.llm.get('summary', {})

    async def _call_api(self, prompt: str) -> str:
        """调用OpenAI API"""
        url = f"{self.api_base}/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是一个专业的会议记录助手。"},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": self.summary_config.get('max_tokens', 4096),
            "temperature": self.summary_config.get('temperature', 0.7),
        }

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    error = await response.text()
                    raise Exception(f"OpenAI API error: {error}")

                result = await response.json()
                return result['choices'][0]['message']['content']

    async def summarize(self, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """总结会议内容"""
        prompt = self._build_prompt(text, options)
        response_text = await self._call_api(prompt)
        return self._parse_response(response_text)

    def _build_prompt(self, text: str, options: Dict[str, Any]) -> str:
        """构建提示词"""
        template = self.summary_config.get(
            'prompt_template',
            "你是一个专业的会议记录助手。请对以下会议内容进行总结，提取关键要点、待办事项和决策。\n\n{text}"
        )

        prompt = template.format(text=text)

        if options.get('extract_todos'):
            prompt += "\n\n请用JSON格式返回待办事项: {\"todos\": [{\"content\": \"任务\", \"assignee\": \"负责人\", \"deadline\": \"截止日期\"}]}"

        if options.get('extract_decisions'):
            prompt += "\n\n请用JSON格式返回决策: {\"decisions\": [{\"content\": \"决策\", \"vote_result\": \"表决结果\"}]}"

        return prompt

    def _parse_response(self, response: str) -> Dict[str, Any]:
        """解析响应"""
        return _parse_llm_response(response)

    async def is_available(self) -> bool:
        """检查OpenAI API是否可用"""
        try:
            url = f"{self.api_base}/models"
            headers = {"Authorization": f"Bearer {self.api_key}"}

            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    return response.status == 200
        except Exception:
            return False


class ClaudeProvider(OpenAIProvider):
    """Claude API Provider"""

    def __init__(self, model: str = None, api_key: str = None, api_base: str = None):
        # Claude使用不同的默认配置
        super().__init__(
            model=model or config.llm.get('model', 'claude-sonnet-4-20250514'),
            api_key=api_key,
            api_base=api_base or 'https://api.anthropic.com/v1'
        )
        self.api_base = api_base or config.llm.get('api_base', 'https://api.anthropic.com/v1')

    async def _call_api(self, prompt: str) -> str:
        """调用Claude API"""
        url = f"{self.api_base}/messages"

        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }

        payload = {
            "model": self.model,
            "max_tokens": self.summary_config.get('max_tokens', 4096),
            "messages": [
                {"role": "user", "content": prompt}
            ]
        }

        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    error = await response.text()
                    raise Exception(f"Claude API error: {error}")

                result = await response.json()
                return result['content'][0]['text']


class LLMService:
    """LLM服务管理类"""

    _providers: Dict[str, LLMProvider] = {}

    @classmethod
    def get_provider(cls) -> LLMProvider:
        """获取当前配置的LLM Provider"""
        provider_type = config.llm.get('provider', 'ollama')

        if provider_type in cls._providers:
            return cls._providers[provider_type]

        if provider_type == 'ollama':
            provider = OllamaProvider()
        elif provider_type == 'openai':
            provider = OpenAIProvider()
        elif provider_type == 'claude':
            provider = ClaudeProvider()
        else:
            provider = OllamaProvider()

        cls._providers[provider_type] = provider
        return provider

    @classmethod
    async def summarize(cls, text: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """总结文本"""
        provider = cls.get_provider()
        options = options or {}
        allow_rule_fallback = bool(options.get("allow_rule_fallback", False))
        try:
            return await provider.summarize(text, options)
        except Exception as exc:
            if allow_rule_fallback:
                logger.warning("LLM总结失败，回退到规则摘要: %s", exc)
                return _fallback_summarize(text, options)
            raise

    @classmethod
    async def is_available(cls) -> bool:
        """检查LLM服务是否可用"""
        provider = cls.get_provider()
        return await provider.is_available()

    @classmethod
    def reset(cls) -> None:
        """重置Provider缓存"""
        cls._providers.clear()

    @classmethod
    async def generate_realtime_summary(cls, text: str, previous_summary: str = "") -> Dict[str, Any]:
        """
        实时生成会议摘要
        基于之前的摘要和新增内容，生成增量摘要

        Args:
            text: 新增的会议内容
            previous_summary: 之前的摘要
        """
        provider = cls.get_provider()

        # 构建实时摘要提示词
        prompt = cls._build_realtime_prompt(text, previous_summary)
        try:
            response_text = await provider._call_api(prompt)
            return _parse_realtime_ai_response(response_text)
        except Exception as exc:
            logger.warning("实时时间线AI生成失败，本轮返回空结果: %s", exc)
            return {
                "topic": "",
                "incremental": "",
                "turning_points": [],
                "key_points": [],
                "decisions": [],
                "context_summary": previous_summary or "",
            }

    @classmethod
    def _build_realtime_prompt(cls, text: str, previous_summary: str) -> str:
        """构建实时摘要提示词"""
        if previous_summary:
            template = """
你是“会议时间线提炼助手”。

之前累计摘要：
{previous_summary}

新增语句：
{text}

请只输出 JSON（不要 Markdown 代码块、不要解释文字），格式如下：
{{
  "topic": "会议主题（若暂时无法确定可留空）",
  "incremental": "基于新增语句的增量总结（50-120字）",
  "turning_points": [
    {{"label": "出现转折/决策/阶段变化时的一句话", "type": "milestone|decision|action"}}
  ],
  "key_points": ["关键点1", "关键点2"],
  "decisions": ["决策1"],
  "context_summary": "更新后的累计摘要（供下一轮继续增量）"
}}

要求：
1) turning_points 只保留“关键转折点”，没有就返回空数组。
2) 内容必须来自新增语句，不要编造。
3) 单条 label 控制在 12-36 字。
"""
        else:
            template = """
你是“会议时间线提炼助手”。

新增语句：
{text}

请只输出 JSON（不要 Markdown 代码块、不要解释文字），格式如下：
{{
  "topic": "会议主题（若暂时无法确定可留空）",
  "incremental": "本轮增量总结（50-120字）",
  "turning_points": [
    {{"label": "关键转折点一句话", "type": "milestone|decision|action"}}
  ],
  "key_points": ["关键点1", "关键点2"],
  "decisions": ["决策1"],
  "context_summary": "累计摘要"
}}
"""
        return template.format(previous_summary=previous_summary, text=text)
