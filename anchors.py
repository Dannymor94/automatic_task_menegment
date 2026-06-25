"""
Шаг 3 пайплайна — парсинг голосовых якорей (детерминированно, regex).

Якоря ОПЦИОНАЛЬНЫ: система работает и без них. Здесь — только детекция и пометка;
маршрут/назначение не применяются (это делает оркестратор/валидатор/код позже).

ASR искажает якоря: «задача» → «за дача», регистр теряется, окончания плавают.
Поэтому совпадение нечёткое: по стему, с опциональными пробелами между буквами,
без оглядки на регистр.

Якорь — усилитель, не обязательное условие. Найденные якоря оркестратор логирует
и может приложить к транскрипту как подсказку для LLM.
"""

from __future__ import annotations

import re

# canonical_type -> список стемов (укороченных, чтобы ловить разные словоформы)
ANCHOR_STEMS: dict[str, list[str]] = {
    "PROJECT": ["проект"],
    "TASK": ["задач"],
    "ASSIGNEE": ["исполнител"],
    "CONTROLLER": ["контрол"],
    "DUE": ["срок", "дедлайн"],
    "PRIORITY": ["приоритет"],
    "DONE": ["готов"],
    "SHIFT": ["сдвиг"],
    "BLOCK": ["блок"],
}

# Где обрывается значение якоря: следующий якорь, конец строки или конец предложения.
_VALUE_STOP = re.compile(r"[.!?\n…]")
# Мусор-разделители между якорем и значением: двоеточие, тире, лишние пробелы.
_LEADING_JUNK = re.compile(r"^[\s:–—-]+")


def _fuzzy(stem: str) -> str:
    """Стем → regex, терпимый к вставленному пробелу между буквами (ASR)."""
    return r"\s?".join(re.escape(ch) for ch in stem)


def _build_patterns() -> list[tuple[str, re.Pattern]]:
    patterns: list[tuple[str, re.Pattern]] = []
    for atype, stems in ANCHOR_STEMS.items():
        for stem in stems:
            # Стем + хвост словоформы ([а-яё]*), чтобы целиком съесть слово
            # («задач»→«задача», «исполнител»→«исполнитель») и не оставить буквы в value.
            patterns.append(
                (atype, re.compile(r"\b" + _fuzzy(stem) + r"[а-яё]*", re.IGNORECASE))
            )
    return patterns


_PATTERNS = _build_patterns()


def _find_raw_matches(text: str) -> list[dict]:
    """Все вхождения якорей: (start, end, type, matched). Перекрытия снимаем по start."""
    hits: list[dict] = []
    for atype, pat in _PATTERNS:
        for m in pat.finditer(text):
            hits.append(
                {"type": atype, "start": m.start(), "end": m.end(), "matched": m.group()}
            )
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))

    # Убираем перекрывающиеся совпадения — оставляем самое раннее/длинное.
    deduped: list[dict] = []
    last_end = -1
    for h in hits:
        if h["start"] >= last_end:
            deduped.append(h)
            last_end = h["end"]
    return deduped


def parse_anchors(text: str) -> list[dict]:
    """Найти якоря и их значения.

    Возвращает список: {type, keyword, value, start}. value — текст после якоря
    до следующего якоря / конца строки / конца предложения (может быть пустым).
    """
    matches = _find_raw_matches(text)
    result: list[dict] = []

    for i, h in enumerate(matches):
        value_start = h["end"]
        # Граница значения = начало следующего якоря (если он раньше пунктуации).
        next_start = matches[i + 1]["start"] if i + 1 < len(matches) else len(text)
        segment = text[value_start:next_start]

        stop = _VALUE_STOP.search(segment)
        if stop:
            segment = segment[: stop.start()]

        value = _LEADING_JUNK.sub("", segment).strip()
        result.append(
            {
                "type": h["type"],
                "keyword": h["matched"],
                "value": value,
                "start": h["start"],
            }
        )
    return result


def detect_project(text: str) -> str | None:
    """Удобный хелпер: значение первого якоря ПРОЕКТ (маршрут на весь созвон)."""
    for a in parse_anchors(text):
        if a["type"] == "PROJECT" and a["value"]:
            return a["value"]
    return None


def summarize(text: str) -> dict:
    """Сводка для лога оркестратора: сколько якорей, какие типы, проект."""
    anchors = parse_anchors(text)
    by_type: dict[str, int] = {}
    for a in anchors:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1
    return {
        "anchors_found": len(anchors),
        "by_type": by_type,
        "project": detect_project(text),
        "anchors": anchors,
    }


if __name__ == "__main__":
    import json
    import sys

    src = sys.stdin.read() if len(sys.argv) == 1 else open(sys.argv[1], encoding="utf-8").read()
    print(json.dumps(summarize(src), ensure_ascii=False, indent=2))
