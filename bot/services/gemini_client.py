from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from dataclasses import dataclass
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)

DEFAULT_ANSWER_CHECK_PROMPT_TEMPLATE = (
    "Ты проверяешь ФИНАЛЬНЫЙ ответ студента по математическому анализу. "
    "Студент прислал фотографию рукописного ОТВЕТА в тетради. "
    "Если фото действительно нечитабельно, сильно размыто, критично обрезано или символы невозможно разобрать, "
    "верни verdict=unreadable. "
    "Если большая часть записи читается, выбирай correct/incorrect, а не unreadable. "
    "Если фото читается, сравни ответ на фото с эталонным ответом математически. "
    "ВАЖНО: засчитывай как correct любые математически эквивалентные формы записи конечного ответа "
    "(например алгебраически преобразованные, эквивалентные тригонометрические/логарифмические формы, "
    "другая, но равносильная константа интегрирования). "
    "НЕ засчитывай промежуточные шаги решения как correct, если это не финальный ответ к задаче. "
    "Если на фото просто переписано условие (например снова записан исходный интеграл), это НЕ является решением: verdict=incorrect. "
    "Если ответ по смыслу неэквивалентен эталону — verdict=incorrect. "
    "Отвечай СТРОГО JSON-объектом без markdown: "
    '{"verdict":"correct|incorrect|unreadable","feedback":"краткий комментарий на русском"}.\n\n'
    "Задание: {{task_text}}\n"
    "Эталонный ответ: {{expected_answer}}"
)


@dataclass(slots=True)
class GeneratedTask:
    latex_integral: str
    hint: str
    answer: str


@dataclass(slots=True)
class AnswerCheckResult:
    verdict: str
    feedback: str


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        model: str,
        verify_ssl: bool = True,
        status_endpoint_template: str = "https://api.gen-api.ru/api/v1/request/get/{request_id}",
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._model = model
        self._verify_ssl = verify_ssl
        self._status_endpoint_template = status_endpoint_template

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def generate_task(self, prompt: str) -> GeneratedTask:
        if not self.enabled:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        if not prompt.strip():
            raise RuntimeError("LLM prompt is empty")

        payload = {
            "is_sync": True,
            "model": self._model,
            "temperature": 0.4,
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        headers = self._headers()

        data = await self._post_json(self._endpoint, payload, headers, verify_ssl=self._verify_ssl)
        data = await self._resolve_async_if_needed(data, headers)
        content = self._extract_content(data)
        content = self._extract_content_for_generation(data)
        return self._parse_generation(content)

    async def check_student_answer(
        self,
        answer_image_data_uri: str,
        expected_answer: str,
        task_text: str,
        prompt_template: str | None = None,
    ) -> AnswerCheckResult:
        if not self.enabled:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        template = prompt_template or DEFAULT_ANSWER_CHECK_PROMPT_TEMPLATE
        prompt = self._render_prompt_template(
            template,
            {
                "task_text": task_text,
                "expected_answer": expected_answer,
            },
        )

        payload = {
            "is_sync": True,
            "model": self._model,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": answer_image_data_uri}},
                    ],
                }
            ],
        }
        headers = self._headers()

        data = await self._post_json(self._endpoint, payload, headers, verify_ssl=self._verify_ssl)
        data = await self._resolve_async_if_needed(data, headers)

        content = self._extract_content_generic(data)
        return self._parse_answer_check(content)

    @staticmethod
    def _render_prompt_template(template: str, variables: dict[str, str]) -> str:
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace(f"{{{{{key}}}}}", value)
        return rendered

    async def _resolve_async_if_needed(self, data: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        status = str(data.get("status", "")).lower()
        request_id = data.get("request_id")

        if status in {"starting", "processing", "queued"} and request_id:
            logger.info("LLM async request started: request_id=%s status=%s", request_id, status)
            for _ in range(30):
                status_url = self._status_endpoint_template.replace("{request_id}", str(request_id))
                polled = await self._post_json(status_url, {}, headers, verify_ssl=self._verify_ssl, use_get=True)
                polled_status = str(polled.get("status", "")).lower()
                if polled_status == "success":
                    logger.info("LLM async request success: request_id=%s", request_id)
                    return polled
                if polled_status in {"failed", "error"}:
                    logger.error("LLM async request failed: request_id=%s payload=%s", request_id, self._short(polled))
                    raise RuntimeError(f"LLM request failed: {self._short(polled)}")
                await asyncio.sleep(1)

            raise RuntimeError("LLM request is still processing too long. Please retry.")

        return data
    
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        verify_ssl: bool,
        use_get: bool = False,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        connector = aiohttp.TCPConnector(ssl=verify_ssl)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), connector=connector) as session:
                if use_get:
                    async with session.get(url, headers=headers) as response:
                        body = await response.text()
                else:
                    async with session.post(url, json=payload, headers=headers) as response:
                        body = await response.text()

                if response.status >= 400:
                    logger.error(
                        "LLM HTTP error: status=%s method=%s url=%s duration_ms=%d",
                        response.status,
                        "GET" if use_get else "POST",
                        url,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    raise RuntimeError(f"LLM error {response.status}: {body[:500]}")
                logger.info(
                    "LLM HTTP success: status=%s method=%s url=%s duration_ms=%d",
                    response.status,
                    "GET" if use_get else "POST",
                    url,
                    int((time.monotonic() - started_at) * 1000),
                )
                return json.loads(body)
        except aiohttp.ClientConnectorCertificateError:
            if verify_ssl:
                logger.warning("LLM SSL verify failed, retrying with verify_ssl=false: url=%s", url)
                return await self._post_json(url, payload, headers, verify_ssl=False, use_get=use_get)
            raise RuntimeError(
                "SSL certificate verification failed for LLM endpoint. "
                "Set GEMINI_SSL_VERIFY=false or install proper CA certificates."
            )
        except ssl.SSLCertVerificationError:
            if verify_ssl:
                logger.warning("LLM SSL cert verification failed, retrying with verify_ssl=false: url=%s", url)
                return await self._post_json(url, payload, headers, verify_ssl=False, use_get=use_get)
            raise RuntimeError(
                "SSL certificate verification failed for LLM endpoint. "
                "Set GEMINI_SSL_VERIFY=false or install proper CA certificates."
            )

    def _extract_content_for_generation(self, data: dict[str, Any]) -> str:
        candidates = self._collect_text_candidates(data)
        generated = [t for t in candidates if "пример:" in t.lower() and "подсказка:" in t.lower() and "ответ:" in t.lower()]
        if generated:
            return generated[0]
        raise RuntimeError(f"Unexpected LLM response format: {self._short(data)}")
    
    def _extract_content(self, data: dict[str, Any]) -> str:
        return self._extract_content_for_generation(data)

    def _extract_content_generic(self, data: dict[str, Any]) -> str:
        candidates = self._collect_text_candidates(data)
        for text in candidates:
            if "{" in text and "}" in text:
                return text
        if candidates:
            return candidates[0]
        raise RuntimeError(f"Unexpected LLM response format: {self._short(data)}")

    def _collect_text_candidates(self, data: dict[str, Any]) -> list[str]:
        candidates: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, str):
                candidates.append(node)
                return
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return candidates

    def _parse_answer_check(self, text: str) -> AnswerCheckResult:
        cleaned = text.strip()
        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned.strip("`").strip()

        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()

        if "{" in cleaned and "}" in cleaned:
            cleaned = cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]

        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            payload = self._parse_answer_check_relaxed(cleaned)
            if payload is None:
                raise RuntimeError(f"Invalid answer-check JSON from LLM: {text}")

        verdict = str(payload.get("verdict", "")).strip().lower()
        feedback = str(payload.get("feedback", "")).strip() or "Без комментария"

        if verdict not in {"correct", "incorrect", "unreadable"}:
            raise RuntimeError(f"Unsupported verdict from LLM: {payload}")

        return AnswerCheckResult(verdict=verdict, feedback=feedback)
    
    def _parse_answer_check_relaxed(self, text: str) -> dict[str, str] | None:
        verdict_match = re.search(r'"verdict"\s*:\s*"(correct|incorrect|unreadable)"', text, flags=re.IGNORECASE)
        feedback_match = re.search(r'"feedback"\s*:\s*"(.*?)"\s*(?:,|})', text, flags=re.DOTALL)

        if not verdict_match:
            return None

        verdict = verdict_match.group(1).lower()
        feedback = feedback_match.group(1).strip() if feedback_match else "Без комментария"
        return {"verdict": verdict, "feedback": feedback}

    def _parse_generation(self, text: str) -> GeneratedTask:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < 3:
            raise RuntimeError(f"Invalid LLM format: {text}")

        p = next((line for line in lines if line.lower().startswith("пример:")), "")
        h = next((line for line in lines if line.lower().startswith("подсказка:")), "")
        a = next((line for line in lines if line.lower().startswith("ответ:")), "")
        if not (p and h and a):
            raise RuntimeError(f"Missing expected fields in LLM response: {text}")

        integral_raw = p.split(":", 1)[1].strip()
        hint = h.split(":", 1)[1].strip()
        answer_raw = a.split(":", 1)[1].strip()

        integral = self._sanitize_latex(integral_raw)
        answer = self._sanitize_latex(answer_raw)
        if not integral or not answer:
            raise RuntimeError("Generated task has empty formula/answer")

        return GeneratedTask(latex_integral=integral, hint=hint, answer=answer)

    def _sanitize_latex(self, value: str) -> str:
        cleaned = value.strip()

        if cleaned.startswith("```") and cleaned.endswith("```"):
            cleaned = cleaned.strip("`").strip()

        if (cleaned.startswith('r"') and cleaned.endswith('"')) or (
            cleaned.startswith("r'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[2:-1].strip()

        if cleaned.startswith("$") and cleaned.endswith("$") and len(cleaned) >= 2:
            cleaned = cleaned[1:-1].strip()

        if (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()

        return cleaned

    def _short(self, value: Any) -> str:
        raw = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        return raw[:500]
