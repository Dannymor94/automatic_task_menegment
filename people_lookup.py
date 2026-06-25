"""
People lookup (шаг 4 пайплайна) — детерминированное сопоставление имени из задачи
с YouGile user ID по справочнику people. НЕ LLM, НЕ угадывание.

Имена в справочнике уникальны → сопоставление однозначно. Имя не найдено или
нормализуется неоднозначно (коллизия) → возвращаем None, валидатор ставит флаг
assignee_unmatched / controller_unmatched. Тёзки различаются именами («Дима П.» / «Дима С.»).
"""

from __future__ import annotations

import re

from db import Person, session_scope

_WS = re.compile(r"\s+")


def normalize(name) -> str:
    """Нормализация имени для сопоставления: регистр, пробелы. Без транслитерации."""
    if not isinstance(name, str):
        return ""
    return _WS.sub(" ", name.strip().lower())


class PeopleDirectory:
    """Снимок справочника людей в память для быстрого детерминированного lookup."""

    def __init__(self, by_norm: dict[str, str | None], collisions: set[str]):
        self._by_norm = by_norm        # normalized_name -> yougile_user_id
        self._collisions = collisions  # нормализованные имена с коллизией (неоднозначны)

    @classmethod
    def from_db(cls) -> "PeopleDirectory":
        by_norm: dict[str, str | None] = {}
        collisions: set[str] = set()
        with session_scope() as s:
            for p in s.query(Person).all():
                key = normalize(p.name)
                if not key:
                    continue
                if key in by_norm:
                    # Две разные записи нормализуются в одно имя → неоднозначно.
                    collisions.add(key)
                by_norm[key] = p.yougile_user_id
        return cls(by_norm, collisions)

    @classmethod
    def from_rows(cls, rows: list[dict]) -> "PeopleDirectory":
        """Для тестов: rows = [{'name':..., 'yougile_user_id':...}, ...]."""
        by_norm: dict[str, str | None] = {}
        collisions: set[str] = set()
        for r in rows:
            key = normalize(r.get("name"))
            if not key:
                continue
            if key in by_norm:
                collisions.add(key)
            by_norm[key] = r.get("yougile_user_id")
        return cls(by_norm, collisions)

    def resolve(self, name) -> tuple[str | None, bool]:
        """Имя → (yougile_user_id | None, matched). matched=False если не найдено/неоднозначно."""
        key = normalize(name)
        if not key or key in self._collisions or key not in self._by_norm:
            return None, False
        return self._by_norm[key], True

    def __len__(self) -> int:
        return len(self._by_norm)


if __name__ == "__main__":
    import sys

    d = PeopleDirectory.from_db()
    print(f"В справочнике людей: {len(d)} имён")
    for name in sys.argv[1:]:
        uid, matched = d.resolve(name)
        print(f"  {name!r} → user_id={uid} matched={matched}")
