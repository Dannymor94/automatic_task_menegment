"""
Чанкер (шаг 2.5 пайплайна) — режет транскрипт на куски ~N токенов для отдельных
LLM-вызовов, чтобы не упираться в лимит токенов на запрос (free-tier TPM).

Режет ПО ГРАНИЦАМ РЕПЛИК, не разрывая фразу. Деградация по доступности границ:
  1) реплики    — строки вида «Имя: …» или абзацы, разделённые пустой строкой;
  2) предложения — если реплик нет (.!?…);
  3) слова       — если нет и пунктуации (сырой ASR-поток без знаков). Мид-слово не рвём.

Между соседними чанками — перекрытие (1–2 единицы / небольшое окно слов), чтобы
задача на стыке попала хотя бы в один чанк целиком. Дубли из перекрытия снимает dedup.py.

Короткий транскрипт (влезает в один чанк) → один чанк.
"""

from __future__ import annotations

import re

# Оценка токенов из символов. Для русского + Qwen BPE берём консервативно
# (скорее переоценим, чем словим 413): ~2.5 символа на токен.
CHARS_PER_TOKEN = 2.5
TARGET_TOKENS = 2000

# Перекрытие: для реплик/предложений — последние N единиц; для слов — окно из M слов.
OVERLAP_UNITS = 2
OVERLAP_WORDS = 30

# Реплика с явным спикером: «Лиза:», «**Дима:**», «Антон -».
_SPEAKER_LINE = re.compile(r"^\s*[*_>#-]*\s*[А-ЯЁA-Z][\w .]{0,30}\s*[:\-–]\s+", re.MULTILINE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN + 0.5)


def _split_units(text: str) -> tuple[list[str], str]:
    """Разбить текст на минимальные «единицы» сшивки. Возвращает (units, kind)."""
    stripped = text.strip()

    # 1) Реплики по спикерам: режем перед каждой строкой-спикером.
    speaker_starts = [m.start() for m in _SPEAKER_LINE.finditer(stripped)]
    if len(speaker_starts) >= 2:
        bounds = speaker_starts + [len(stripped)]
        units = [stripped[a:b].strip() for a, b in zip(bounds, bounds[1:])]
        units = [u for u in units if u]
        if len(units) >= 2:
            return units, "reply"

    # 2) Реплики как абзацы (пустая строка-разделитель).
    paras = [p.strip() for p in re.split(r"\n\s*\n", stripped) if p.strip()]
    if len(paras) >= 2:
        return paras, "paragraph"

    # 3) Предложения по пунктуации.
    sents = [s.strip() for s in _SENTENCE_SPLIT.split(stripped) if s.strip()]
    if len(sents) >= 2:
        return sents, "sentence"

    # 4) Слова (сырой ASR без знаков). Мид-слово не рвём — единица = слово.
    words = stripped.split()
    return words, "word"


def _join(units: list[str], kind: str) -> str:
    """Склейка единиц обратно в текст: слова через пробел, остальное через перенос."""
    sep = " " if kind == "word" else "\n"
    return sep.join(units)


def _overlap_count(kind: str) -> int:
    """Сколько хвостовых единиц прошлого чанка повторить в начале следующего."""
    return OVERLAP_WORDS if kind == "word" else OVERLAP_UNITS


def chunk_transcript(
    text: str, target_tokens: int = TARGET_TOKENS
) -> list[str]:
    """Транскрипт → список чанков (строк). Короткий текст → [text]."""
    text = (text or "").strip()
    if not text:
        return []
    if estimate_tokens(text) <= target_tokens:
        return [text]

    units, kind = _split_units(text)
    if len(units) <= 1:
        return [text]  # нечего резать по границам — отдаём как есть

    overlap_n = _overlap_count(kind)
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    for unit in units:
        ut = estimate_tokens(unit)
        # Закрываем чанк, если добавление единицы переполнит цель (и чанк не пуст).
        if current and current_tokens + ut > target_tokens:
            chunks.append(_join(current, kind))
            # Старт следующего чанка — хвост предыдущего (перекрытие).
            tail = current[-overlap_n:] if overlap_n else []
            current = list(tail)
            current_tokens = sum(estimate_tokens(u) for u in current)
        current.append(unit)
        current_tokens += ut

    if current:
        chunks.append(_join(current, kind))
    return chunks


def chunk_info(text: str, target_tokens: int = TARGET_TOKENS) -> dict:
    """Сводка для лога оркестратора."""
    _, kind = _split_units(text.strip()) if text.strip() else ([], "empty")
    chunks = chunk_transcript(text, target_tokens)
    return {
        "total_tokens_est": estimate_tokens(text),
        "unit_kind": kind,
        "n_chunks": len(chunks),
        "chunk_tokens_est": [estimate_tokens(c) for c in chunks],
    }


if __name__ == "__main__":
    import json
    import sys

    src = sys.stdin.read() if len(sys.argv) == 1 else open(sys.argv[1], encoding="utf-8").read()
    info = chunk_info(src)
    print(json.dumps(info, ensure_ascii=False, indent=2))
