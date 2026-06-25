"""
Шаг 5 пайплайна — декомпозиция: транскрипт → задачи JSON.
ЕДИНСТВЕННАЯ точка, где работает LLM (см. pipeline-conventions, decompose-prompt).
Всё остальное в пайплайне — детерминированный код.

Системный промпт берётся из CLAUDE.md (не дублируется здесь).
Провайдер за конфигом (LLM_BASE_URL + LLM_MODEL + LLM_API_KEY) — тест→локаль одной строкой.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from providers import ProviderPool, load_providers

load_dotenv()

BASE_DIR = Path(__file__).parent
# Рантайм-промпты декомпозиции (разделы 2–8), НЕ документация проекта (CLAUDE.md).
# EN — основной по итогам A/B (крепче grounding на речи без якорей); RU — A/B-альтернатива.
PROMPT_FILES = {
    "en": BASE_DIR / "prompts" / "decompose_en.md",
    "ru": BASE_DIR / "prompts" / "decompose.md",
}
DEFAULT_PROMPT_VERSION = "en"
# Обратная совместимость: старое имя указывает на текущий дефолт.
PROMPT_FILE = PROMPT_FILES[DEFAULT_PROMPT_VERSION]

MAX_PARSE_RETRIES = 2

log = logging.getLogger(__name__)

JSON_FORMAT_INSTRUCTION = (
    "Respond with ONLY a JSON array. "
    "No markdown, no backticks, no explanations. "
    "Start your response with [ and end with ]"
)


def build_client() -> tuple[OpenAI, str]:
    """Один клиент основного провайдера (для E0-замера, без failover)."""
    primary = load_providers()[0]
    return primary.client, primary.model


def build_pool(round_robin: bool = False, cache: bool | None = None) -> ProviderPool:
    """Пул провайдеров с failover (и round-robin для мультичанка)."""
    return ProviderPool(load_providers(cache=cache), round_robin=round_robin)


def resolve_prompt_version(version: str | None = None) -> str:
    """Версия промпта: аргумент → env LLM_PROMPT_VERSION → дефолт (en)."""
    v = (version or os.environ.get("LLM_PROMPT_VERSION") or DEFAULT_PROMPT_VERSION).lower()
    if v not in PROMPT_FILES:
        raise ValueError(f"Неизвестная версия промпта {v!r}, доступны: {list(PROMPT_FILES)}")
    return v


def load_system_prompt(
    prompt_file: str | Path | None = None, version: str | None = None
) -> str:
    """Системный промпт = требование формата + рантайм-правила из файла промпта.

    Приоритет выбора файла: явный prompt_file → version (en/ru) → env → дефолт (en).
    """
    if prompt_file:
        path = Path(prompt_file)
    else:
        path = PROMPT_FILES[resolve_prompt_version(version)]
    content = path.read_text(encoding="utf-8")
    return f"{JSON_FORMAT_INSTRUCTION}\n\n{content}"


def extract_json_block(text: str) -> str:
    """Извлечь JSON-массив по внешним скобкам [ ... ] (устойчиво к тексту вокруг)."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON array brackets found in response (len={len(text)})")
    return text[start : end + 1]


def parse_tasks(raw_response: str) -> list[dict]:
    return json.loads(extract_json_block(raw_response))


_REMINDER = (
    "Верни ТОЛЬКО чистый JSON-массив задач без пояснений, "
    "не оборачивай в markdown, не добавляй текст до массива."
)


def _parse_with_retry(caller) -> tuple[list[dict], str, dict]:
    """Общая логика: вызвать caller(reminder), распарсить, при сбое — повторить.

    caller(reminder: str | None) -> (raw_text, meta). meta пробрасывается наружу
    (для failover-пула — какой провайдер ответил). Возвращает (tasks, raw, meta).
    """
    raw, meta = caller(None)
    for attempt in range(MAX_PARSE_RETRIES + 1):
        try:
            tasks = parse_tasks(raw)
            if not isinstance(tasks, list):
                raise ValueError("Expected JSON array at top level")
            return tasks, raw, meta
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == MAX_PARSE_RETRIES:
                raise RuntimeError(
                    f"JSON parse failed after {MAX_PARSE_RETRIES + 1} attempts: {exc}"
                ) from exc
            log.warning("Parse failed (attempt %d), retrying with reminder", attempt + 1)
            raw, meta = caller(_REMINDER)
    raise RuntimeError("decompose: unexpected exit from retry loop")


def _call_llm(client: OpenAI, model: str, system: str, transcript: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content


def decompose_raw(
    transcript: str,
    client: OpenAI,
    model: str,
    system: str,
) -> tuple[list[dict], str]:
    """Вызов LLM с ретраем на ошибке парсинга. Возвращает (tasks, raw_response).

    Пустой массив [] — ВАЛИДНЫЙ результат («задач нет»), не ошибка.
    """
    def caller(reminder):
        text = transcript if reminder is None else f"{transcript}\n\n[Напоминание: {reminder}]"
        return _call_llm(client, model, system, text), {"provider": model}

    tasks, raw, _ = _parse_with_retry(caller)
    return tasks, raw


def build_team_block(team: list[dict] | None) -> str:
    """Опциональный блок «известная команда проекта» (имя — специальность).

    Подсказка модели для assignee. Имена всё равно резолвятся детерминированно
    через people_lookup — назначению LLM вслепую не доверяем.
    """
    if not team:
        return ""
    lines = []
    for p in team:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        spec = (p.get("specialty") or "").strip()
        lines.append(f"- {name} — {spec}" if spec else f"- {name}")
    if not lines:
        return ""
    return (
        "\n\n## Известная команда проекта (подсказка для assignee)\n"
        "Если в задаче исполнитель назван по роли/специальности, сопоставь его с этим "
        "списком и поставь имя как здесь. Имена ниже — единственный источник; не выдумывай "
        "других. Точное сопоставление имени с YouGile ID делает код после тебя.\n"
        + "\n".join(lines)
    )


def decompose_pool(
    transcript: str,
    pool: ProviderPool,
    system: str,
    prefer_index: int = 0,
    team: list[dict] | None = None,
) -> tuple[list[dict], str, dict]:
    """Декомпозиция через пул с failover. Возвращает (tasks, raw, meta).

    team — опциональная «команда проекта» (имя+специальность), добавляется в системный
    промпт как подсказка для assignee (см. build_team_block). По умолчанию None — поведение
    не меняется.
    """
    system_full = system + build_team_block(team)

    def caller(reminder):
        text = transcript if reminder is None else f"{transcript}\n\n[Напоминание: {reminder}]"
        messages = [
            {"role": "system", "content": system_full},
            {"role": "user", "content": text},
        ]
        return pool.chat(messages, prefer_index=prefer_index)

    return _parse_with_retry(caller)


def decompose(
    transcript: str,
    client: OpenAI | None = None,
    model: str | None = None,
    system: str | None = None,
) -> list[dict]:
    """Удобная обёртка: транскрипт → список задач. Создаёт клиент/промпт при нужде."""
    if client is None or model is None:
        client, model = build_client()
    if system is None:
        system = load_system_prompt()
    tasks, _ = decompose_raw(transcript, client, model, system)
    return tasks


def count_low_confidence(tasks: list[dict], threshold: float = 0.5) -> int:
    return sum(
        1
        for t in tasks
        if isinstance(t.get("confidence"), (int, float)) and t["confidence"] < threshold
    )
