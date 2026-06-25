"""
Абстракция LLM-провайдеров с failover и round-robin.

Список провайдеров берётся из .env (PROVIDER_ORDER + {NAME}_API_KEY/BASE_URL/MODEL).
Все — за OpenAI-совместимым клиентом, поэтому тест (Groq/OpenRouter) → прод (Ollama/vLLM)
меняется только конфигом. Ключи — в окружении, не в коде.

- Failover: при 429/413/5xx (или сетевом сбое) на провайдере запрос уходит на следующий.
- Round-robin: для мультичанка стартовый провайдер чередуется по чанкам — делит нагрузку
  между двумя бесплатными лимитами (каждый со своим TPM).
- Response cache (OpenRouter): опциональный заголовок для идентичных запросов при отладке
  (тесты/ретраи). На разных чанках не срабатывает — это нормально.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
)

load_dotenv()

log = logging.getLogger(__name__)

# Статусы, при которых имеет смысл уйти на запасной провайдер.
RETRYABLE_STATUS = {429, 413, 500, 502, 503, 529}

# Дефолты по известным провайдерам (можно переопределить в .env).
PROVIDER_DEFAULTS = {
    "groq": ("https://api.groq.com/openai/v1", "qwen/qwen3-32b"),
    "openrouter": ("https://openrouter.ai/api/v1", "qwen/qwen3-32b"),
}


@dataclass
class Provider:
    name: str
    client: OpenAI
    model: str
    extra_headers: dict = field(default_factory=dict)


def _env(name: str, suffix: str) -> str | None:
    return os.environ.get(f"{name.upper()}_{suffix}")


def load_providers(cache: bool | None = None) -> list[Provider]:
    """Собрать провайдеров из .env по PROVIDER_ORDER. Без ключа — пропускаем."""
    if cache is None:
        cache = os.environ.get("LLM_CACHE", "0") == "1"

    order = os.environ.get("PROVIDER_ORDER", "groq,openrouter")
    names = [n.strip() for n in order.split(",") if n.strip()]

    providers: list[Provider] = []
    for name in names:
        api_key = _env(name, "API_KEY")
        if not api_key:
            log.info("provider %s: нет API-ключа в .env — пропускаю", name)
            continue
        base_default, model_default = PROVIDER_DEFAULTS.get(name, (None, None))
        base_url = _env(name, "BASE_URL") or base_default
        model = _env(name, "MODEL") or model_default
        if not base_url or not model:
            log.warning("provider %s: неполная конфигурация — пропускаю", name)
            continue

        # max_retries=0: не ждём встроенный backoff на основном — сразу failover на запасной.
        client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)

        headers: dict[str, str] = {}
        if name == "openrouter":
            if cache:
                headers["X-OpenRouter-Cache"] = "true"
            ref = os.environ.get("OPENROUTER_REFERER")
            title = os.environ.get("OPENROUTER_TITLE")
            if ref:
                headers["HTTP-Referer"] = ref
            if title:
                headers["X-Title"] = title

        providers.append(Provider(name, client, model, headers))

    if not providers:
        raise RuntimeError(
            "Нет настроенных провайдеров. Проверь PROVIDER_ORDER и *_API_KEY в .env"
        )
    return providers


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return getattr(exc, "status_code", None) in RETRYABLE_STATUS
    return False


def _short(exc: Exception) -> str:
    code = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" {code}" if code else "")


class ProviderPool:
    """Пул провайдеров с failover и (опционально) round-robin."""

    def __init__(self, providers: list[Provider], round_robin: bool = False):
        self.providers = providers
        self.round_robin = round_robin
        self._counter = 0

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        prefer_index: int | None = None,
    ) -> tuple[str, dict]:
        """Вызов с failover. Возвращает (text, meta).

        meta = {provider, switched, tried}. prefer_index задаёт, с какого провайдера
        начать (для round-robin по чанкам). Если None и round_robin — берём по счётчику.
        """
        n = len(self.providers)
        if prefer_index is None:
            prefer_index = (self._counter % n) if self.round_robin else 0
        self._counter += 1

        order = [self.providers[(prefer_index + i) % n] for i in range(n)]
        tried: list[str] = []
        last_exc: Exception | None = None

        for j, p in enumerate(order):
            tried.append(p.name)
            try:
                resp = p.client.chat.completions.create(
                    model=p.model,
                    messages=messages,
                    temperature=temperature,
                    extra_headers=p.extra_headers or None,
                )
                return resp.choices[0].message.content, {
                    "provider": p.name,
                    "switched": j > 0,
                    "tried": list(tried),
                }
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_retryable(exc) and j < n - 1:
                    log.warning(
                        "provider %s сбой (%s) → failover на %s",
                        p.name, _short(exc), order[j + 1].name,
                    )
                    continue
                raise
        # Если дошли сюда — все провайдеры исчерпаны.
        raise last_exc if last_exc else RuntimeError("ProviderPool: нет провайдеров")
