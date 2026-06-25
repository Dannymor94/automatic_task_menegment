"""
Детерминированный валидатор (шаг 6 пайплайна). Ставит validator_flags по коду,
НЕ по LLM-confidence. Читает out/*.json, дописывает validator_flags в каждую
задачу, печатает сводку.

Флаги (см. pipeline-conventions):
  due_invented        — due_date есть, но в source нет даты/времени ИЛИ год < 2025.
  no_grounding        — поле source пустое или мусорное.
  assignee_unmatched  — имя assignee не найдено в справочнике people.json (если есть).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUT_DIR = BASE_DIR / "out"
PEOPLE_FILE = BASE_DIR / "people.json"

MIN_VALID_YEAR = 2025

# Временные выражения в source — русские относительные/абсолютные метки времени.
_MONTHS = (
    r"янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек"
)
# Стемы дней недели + любое падежное окончание (к пятнице, в среду, до субботы).
_WEEKDAYS = (
    r"понедельник[а-яё]*|вторник[а-яё]*|сред[а-яё]+|четверг[а-яё]*"
    r"|пятниц[а-яё]+|суббот[а-яё]+|воскресень[а-яё]+"
)
TEMPORAL_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"                      # ISO 2026-06-29
    r"|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?"         # 29.06 / 29.06.2026
    r"|\d{1,2}\s*(?:" + _MONTHS + r")"         # 29 июня
    r"|(?:" + _WEEKDAYS + r")"                 # к среде, в пятницу
    r"|сегодня|завтра|послезавтра"
    r"|числ[ауе]"                              # к 1 числу / первого числа
    r"|на\s+следующ|на\s+этой\s+недел"
    r"|к\s+концу\s+(?:недел|месяц)"
    r"|через\s+\d+\s*(?:дн|недел|месяц)",
    re.IGNORECASE,
)


def load_people() -> set[str] | None:
    """Справочник имён. Нет файла → None (флаг assignee_unmatched не ставим)."""
    if not PEOPLE_FILE.exists():
        return None
    data = json.loads(PEOPLE_FILE.read_text(encoding="utf-8"))
    # Поддерживаем и список имён, и список объектов {"name": ...}.
    names = set()
    for item in data:
        if isinstance(item, str):
            names.add(item.strip().lower())
        elif isinstance(item, dict) and item.get("name"):
            names.add(str(item["name"]).strip().lower())
    return names


def due_year(due_date) -> int | None:
    if not isinstance(due_date, str):
        return None
    m = re.match(r"\s*(\d{4})-\d{2}-\d{2}", due_date)
    return int(m.group(1)) if m else None


def validate_task(task: dict, people: set[str] | None, directory=None) -> list[str]:
    flags: list[str] = []

    source = task.get("source")
    has_grounding = isinstance(source, str) and len(source.strip()) >= 3
    if not has_grounding:
        flags.append("no_grounding")

    due = task.get("due_date")
    if due:
        year = due_year(due)
        if year is not None and year < MIN_VALID_YEAR:
            # Год из прошлого — модель выдумала дату (типичная галлюцинация).
            flags.append("due_invented")
        elif not (has_grounding and TEMPORAL_RE.search(source)):
            # Дата есть, но в source нет временного выражения — опоры нет.
            flags.append("due_invented")

    if directory is not None:
        # E2: подставляем реальные YouGile ID по справочнику people (детерминированно).
        for field, id_field, flag in (
            ("assignee", "assignee_id", "assignee_unmatched"),
            ("controller", "controller_id", "controller_unmatched"),
        ):
            name = task.get(field)
            if name:
                uid, matched = directory.resolve(name)
                task[id_field] = uid  # реальный YouGile user ID или None
                if not matched:
                    flags.append(flag)
            else:
                task[id_field] = None
    elif people is not None:
        # Легаси-режим (без БД): только флаг по множеству имён, без подстановки ID.
        assignee = task.get("assignee")
        if assignee and assignee.strip().lower() not in people:
            flags.append("assignee_unmatched")

    return flags


def validate_tasks(
    tasks: list[dict], people: set[str] | None = None, directory=None
) -> list[dict]:
    """Ядро шага 6: список задач → тот же список с проставленным validator_flags.

    directory — PeopleDirectory (E2): подставляет реальные YouGile ID в assignee_id/
    controller_id и флагует ненайденные. Если directory не задан, используется легаси-
    режим по множеству имён people (или people.json).
    """
    if directory is None and people is None:
        people = load_people()
    for task in tasks:
        task["validator_flags"] = validate_task(task, people, directory)
    return tasks


def validate_file(path: Path, people: set[str] | None) -> dict:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(tasks, list):
        raise ValueError(f"{path.name}: ожидался JSON-массив")

    validate_tasks(tasks, people)
    flagged = sum(1 for t in tasks if t.get("validator_flags"))

    path.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"file": path.name, "tasks": len(tasks), "flagged": flagged}


def main() -> None:
    people = load_people()
    if people is None:
        print("[i] people.json не найден — флаг assignee_unmatched пропускается")

    args = sys.argv[1:]
    files = (
        [Path(a) for a in args]
        if args
        else sorted(OUT_DIR.glob("*.json"))
    )
    if not files:
        print(f"Нет JSON-файлов в {OUT_DIR}")
        return

    total_tasks = total_flagged = 0
    for path in files:
        stats = validate_file(path, people)
        total_tasks += stats["tasks"]
        total_flagged += stats["flagged"]
        print(
            f"{stats['file']}: задач {stats['tasks']}, "
            f"с флагами {stats['flagged']}"
        )

    print(f"--- Итого: задач {total_tasks}, с флагами {total_flagged}")


if __name__ == "__main__":
    main()
