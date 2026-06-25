"""
Дедупликация задач между чанками (после декомпозиции, до валидации).

Две причины дублей:
  1) перекрытие чанков — задача со стыка извлеклась дважды;
  2) РЕКАП в речи — менеджер в конце созвона переназывает задачи своими словами
     («подытожу: …»), и первое упоминание попадает в один чанк, рекап — в другой.

Дедуп СОЗНАТЕЛЬНО консервативен. Главный принцип проекта: пропустить реальную задачу
хуже, чем показать лишнюю (дубль менеджер удалит в один клик на ревью; потерянную
задачу никто не увидит). Поэтому снимаем только дубли с ВЫСОКОЙ уверенностью, а
сомнительные оставляем — пусть менеджер решит.

Сравнение лексическое (без векторов): схожесть title по символам (SequenceMatcher)
и по словам (Жаккар). Этого хватает на переформулировки с общим ядром
(«Описать логику…» ↔ «Реализовать логику…»), но НЕ на семантические рекапы другими
словами («Получить ключи телефонии» ↔ «Обеспечить доступы к телефонии») — такие
проходят на ревью как два черновика. Надёжный семантический дедуп — на E6 (Qdrant).

Из пары дублей оставляем «более полную» задачу (больше заполненных полей, выше confidence).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# Сильный сигнал: почти идентичный title → дубль сам по себе (ловит рекап того же
# названия с заменой глагола). Пороги высокие, чтобы не схлопнуть разные задачи.
TITLE_STRONG_SEQ = 0.85    # символьная схожесть title
TITLE_STRONG_JAC = 0.65    # словесная схожесть title (Жаккар)
# Средний сигнал: ощутимая схожесть title И совпадение источника.
TITLE_MED_SEQ = 0.70
SOURCE_THRESHOLD = 0.55

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def _norm(s) -> str:
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _seq(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _jaccard(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def is_duplicate(t1: dict, t2: dict) -> bool:
    n1, n2 = _norm(t1.get("title")), _norm(t2.get("title"))
    title_seq = _seq(n1, n2)
    title_jac = _jaccard(n1, n2)

    # Сильный сигнал по одному title — это дубль.
    if title_seq >= TITLE_STRONG_SEQ or title_jac >= TITLE_STRONG_JAC:
        return True
    # Средний сигнал по title + подтверждение совпадающим источником.
    if title_seq >= TITLE_MED_SEQ:
        s1, s2 = _norm(t1.get("source")), _norm(t2.get("source"))
        source_sim = max(_seq(s1, s2), _jaccard(s1, s2))
        if source_sim >= SOURCE_THRESHOLD:
            return True
    return False


def _completeness(task: dict) -> tuple[int, float, int]:
    """Чем «полнее» задача, тем лучше представитель. (заполненных полей, confidence, длина описания)."""
    fields = ["project", "assignee", "controller", "due_date", "description"]
    filled = sum(1 for f in fields if task.get(f))
    filled += 1 if task.get("checklist") else 0
    filled += 1 if task.get("acceptance_criteria") else 0
    conf = task.get("confidence")
    conf = conf if isinstance(conf, (int, float)) else 0.0
    return (filled, conf, len(task.get("description") or ""))


def dedup_tasks(tasks: list[dict]) -> tuple[list[dict], int]:
    """Убрать дубли. Возвращает (список_без_дублей, сколько_удалено).

    Порядок сохраняется по первому вхождению; представитель — более полная из пары.
    """
    kept: list[dict] = []
    removed = 0
    for task in tasks:
        dup_idx = next(
            (i for i, k in enumerate(kept) if is_duplicate(task, k)), None
        )
        if dup_idx is None:
            kept.append(task)
        else:
            removed += 1
            # Оставляем более полную версию на месте первого вхождения.
            if _completeness(task) > _completeness(kept[dup_idx]):
                kept[dup_idx] = task
    return kept, removed


if __name__ == "__main__":
    import json
    import sys

    data = json.load(sys.stdin if len(sys.argv) == 1 else open(sys.argv[1], encoding="utf-8"))
    deduped, removed = dedup_tasks(data)
    print(json.dumps({"kept": len(deduped), "removed": removed, "tasks": deduped},
                     ensure_ascii=False, indent=2))
