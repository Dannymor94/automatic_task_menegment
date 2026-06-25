"""
Тесты пайплайна E1. Запуск: python test_pipeline.py

Детерминированные тесты (anchors, validate) — всегда офлайн, без сети.
Интеграционный тест оркестратора — на файлах из raw/, требует LLM_API_KEY
(если ключа нет — тест помечается SKIP, не падает).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import anchors as anchors_mod
import chunker
import dedup
import validate
import transcribe

BASE_DIR = Path(__file__).parent
RAW_DIR = BASE_DIR / "raw"

_passed = 0
_failed = 0
_skipped = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def skip(name: str, why: str) -> None:
    global _skipped
    _skipped += 1
    print(f"  SKIP  {name}  ({why})")


# --------------------------------------------------------------------------- #
# anchors.py
# --------------------------------------------------------------------------- #
def test_anchors() -> None:
    print("test_anchors")
    txt = (
        "ПРОЕКТ Лендинг. за дача подготовить макет. ИСПОЛНИТЕЛЬ Антон. "
        "СРОК 27 июня. КОНТРОЛЬ Лиза. ПРИОРИТЕТ высокий. "
        "Готово по платёжке. СДВИГ форма на вторник. БЛОК оплата ждём доки."
    )
    by_type = {a["type"]: a for a in anchors_mod.parse_anchors(txt)}

    check("project detected", anchors_mod.detect_project(txt) == "Лендинг")
    # ASR-искажение «за дача» опознаётся как TASK, хвост не течёт в value
    check("ASR 'за дача' -> TASK", "TASK" in by_type)
    check("TASK value clean", by_type.get("TASK", {}).get("value") == "подготовить макет",
          repr(by_type.get("TASK", {}).get("value")))
    check("assignee name only", by_type.get("ASSIGNEE", {}).get("value") == "Антон",
          repr(by_type.get("ASSIGNEE", {}).get("value")))
    check("due value", by_type.get("DUE", {}).get("value") == "27 июня")
    check("controller name only", by_type.get("CONTROLLER", {}).get("value") == "Лиза")
    check("all update anchors found",
          {"DONE", "SHIFT", "BLOCK"} <= set(by_type.keys()))

    # Без якорей — пустой результат, не ошибка
    empty = anchors_mod.summarize("Просто болтаем про погоду и футбол.")
    check("no anchors -> 0", empty["anchors_found"] == 0)
    check("no project -> None", empty["project"] is None)


# --------------------------------------------------------------------------- #
# validate.py — флаги ставит код, не модель
# --------------------------------------------------------------------------- #
def test_validate() -> None:
    print("test_validate")
    tasks = [
        # год < 2025 → due_invented (выдуманная дата)
        {"title": "A", "due_date": "2023-10-05", "source": "к четвергу"},
        # дата есть, но в source нет числа/месяца → due_invented
        {"title": "B", "due_date": "2026-06-30", "source": "надо сделать страницу"},
        # дата с явным числом в source → чисто
        {"title": "C", "due_date": "2026-06-27", "source": "подготовить к 27 июня"},
        # дата с днём недели в source → чисто
        {"title": "D", "due_date": "2026-06-27", "source": "давай к пятнице"},
        # пустой source → no_grounding
        {"title": "E", "due_date": None, "source": ""},
        # нет даты, есть grounding → без флагов
        {"title": "F", "due_date": None, "source": "возьми на себя"},
    ]
    out = validate.validate_tasks([dict(t) for t in tasks], people=None)
    flags = [t["validator_flags"] for t in out]

    check("year<2025 -> due_invented", flags[0] == ["due_invented"], str(flags[0]))
    check("no date in source -> due_invented", flags[1] == ["due_invented"], str(flags[1]))
    check("explicit number -> clean", flags[2] == [], str(flags[2]))
    check("weekday in source -> clean", flags[3] == [], str(flags[3]))
    check("empty source -> no_grounding", flags[4] == ["no_grounding"], str(flags[4]))
    check("grounded, no due -> clean", flags[5] == [], str(flags[5]))

    # assignee_unmatched — только если есть справочник
    with_people = validate.validate_tasks(
        [{"title": "G", "assignee": "Гость", "source": "возьми на себя", "due_date": None}],
        people={"антон", "лиза"},
    )
    check("unknown assignee -> assignee_unmatched",
          with_people[0]["validator_flags"] == ["assignee_unmatched"],
          str(with_people[0]["validator_flags"]))


# --------------------------------------------------------------------------- #
# transcribe.py — без аудио проверяем только валидацию формата
# --------------------------------------------------------------------------- #
def test_chunker() -> None:
    print("test_chunker")
    # Короткий текст → один чанк
    short = "Привет, это короткий созвон. Задач нет."
    check("short -> 1 chunk", chunker.chunk_transcript(short) == [short])

    # Длинный текст по репликам → несколько чанков, фразы не разорваны
    replies = "\n".join(f"Спикер{i % 3}: " + ("слово " * 60).strip() for i in range(20))
    chunks = chunker.chunk_transcript(replies, target_tokens=300)
    check("long -> multiple chunks", len(chunks) > 1, f"got {len(chunks)}")
    check("no chunk wildly over target",
          all(chunker.estimate_tokens(c) <= 300 * 1.6 for c in chunks))

    # Сырой ASR-поток без пунктуации → режется по словам, мид-слово не рвётся
    stream = ("каждоесловоцелое " * 400).strip()
    cstream = chunker.chunk_transcript(stream, target_tokens=200)
    check("raw stream -> multiple chunks", len(cstream) > 1, f"got {len(cstream)}")
    rejoined_words = set(" ".join(cstream).split())
    check("no mid-word split", rejoined_words == {"каждоесловоцелое"},
          str(list(rejoined_words)[:3]))

    # Перекрытие: хвост предыдущего чанка повторяется в начале следующего
    info = chunker.chunk_info(replies, target_tokens=300)
    check("chunk_info has n_chunks", info["n_chunks"] == len(chunks))


def test_dedup() -> None:
    print("test_dedup")
    # Один и тот же таск со стыка двух чанков (title почти идентичен, source совпадает)
    a = {"title": "Описать логику передачи спорных случаев оператору",
         "source": "логику передачи спорных случаев оператору надо описать к среде",
         "confidence": 0.7, "assignee": None}
    b = {"title": "Описать логику передачи спорных случаев оператору.",
         "source": "логику передачи спорных случаев оператору надо описать к среде",
         "confidence": 0.8, "assignee": "Дима"}
    c = {"title": "Получить доступы к телефонии",
         "source": "беру на себя дедлайн до конца недели", "confidence": 0.8}

    deduped, removed = dedup.dedup_tasks([a, b, c])
    check("removes 1 seam duplicate", removed == 1, f"removed={removed}")
    check("keeps 2 distinct tasks", len(deduped) == 2, f"kept={len(deduped)}")
    # Оставлен более полный представитель (b: есть assignee, выше confidence)
    kept_titles = {t["title"] for t in deduped}
    check("keeps fuller representative",
          any(t.get("assignee") == "Дима" for t in deduped), str(kept_titles))

    # Разные задачи не схлопываются
    d2, r2 = dedup.dedup_tasks([a, c])
    check("distinct tasks survive", r2 == 0 and len(d2) == 2)


def test_people_lookup() -> None:
    print("test_people_lookup")
    from people_lookup import PeopleDirectory

    d = PeopleDirectory.from_rows([
        {"name": "Дима", "yougile_user_id": "yg-1"},
        {"name": "Лиза", "yougile_user_id": "yg-2"},
    ])
    check("resolve exact", d.resolve("Дима") == ("yg-1", True))
    check("resolve case/space-insensitive", d.resolve("  лиза ") == ("yg-2", True))
    check("unknown -> None+False", d.resolve("Гость") == (None, False))
    check("empty -> None+False", d.resolve("") == (None, False))

    # Коллизия нормализации → неоднозначно → не матчим
    dc = PeopleDirectory.from_rows([
        {"name": "Дима", "yougile_user_id": "yg-1"},
        {"name": "дима", "yougile_user_id": "yg-9"},
    ])
    check("collision -> unmatched", dc.resolve("Дима") == (None, False))


def test_validate_with_directory() -> None:
    print("test_validate_with_directory")
    from people_lookup import PeopleDirectory

    d = PeopleDirectory.from_rows([{"name": "Дима", "yougile_user_id": "yg-1"}])
    tasks = [
        {"title": "A", "assignee": "Дима", "controller": "Неизвестный",
         "source": "дима сделает", "due_date": None},
        {"title": "B", "assignee": "Чужой", "controller": None,
         "source": "кто-то", "due_date": None},
    ]
    validate.validate_tasks(tasks, directory=d)
    check("real id substituted", tasks[0]["assignee_id"] == "yg-1", str(tasks[0].get("assignee_id")))
    check("controller unmatched flagged",
          "controller_unmatched" in tasks[0]["validator_flags"], str(tasks[0]["validator_flags"]))
    check("unknown assignee -> None + flag",
          tasks[1]["assignee_id"] is None and "assignee_unmatched" in tasks[1]["validator_flags"],
          str(tasks[1]["validator_flags"]))


def test_meeting_state_db() -> None:
    print("test_meeting_state_db")
    import db
    import repo
    db.create_all()  # идемпотентно: гарантируем таблицы в локальной БД

    mid = repo.create_meeting(source_file="raw/_test.txt", status="uploaded")
    check("meeting created", isinstance(mid, int))
    check("initial status uploaded", repo.get_meeting(mid)["status"] == "uploaded")

    for st in ("transcribing", "processing"):
        repo.set_status(mid, st)
    check("status advanced to processing", repo.get_meeting(mid)["status"] == "processing")

    repo.save_result(mid, {"tasks": [{"title": "X"}]}, status="awaiting_review")
    m = repo.get_meeting(mid)
    check("result_json persisted", m["result_json"]["tasks"][0]["title"] == "X")
    check("final status awaiting_review", m["status"] == "awaiting_review")


def test_yougile_write_idempotent() -> None:
    print("test_yougile_write_idempotent")
    import db
    import repo
    from yougile_writer import StickerCatalog, write_task
    db.create_all()

    mid = repo.create_meeting("raw/_idem.txt", "awaiting_review")
    routing = {"yougile_column_id": "col-1"}
    task = {"title": "T", "internal_id": f"{mid}-0", "priority": None, "controller": None}

    calls = {"create": 0}

    class FakeClient:
        def list_string_stickers(self):
            return []

        def create_task(self, body):
            calls["create"] += 1
            return "yg-task-xyz"

    client = FakeClient()
    catalog = StickerCatalog(client)

    r1 = write_task(client, catalog, mid, task, routing)
    r2 = write_task(client, catalog, mid, task, routing)  # повтор той же задачи

    check("first write -> written", r1["status"] == "written", str(r1))
    check("second write -> skipped (idempotent)", r2["status"] == "skipped", str(r2))
    check("YouGile create called exactly once", calls["create"] == 1, str(calls))
    check("marker persisted as written",
          repo.get_task_write_status(f"{mid}-0")["status"] == "written")


def test_yougile_write_dedup_by_column_title() -> None:
    print("test_yougile_write_dedup_by_column_title")
    import repo
    from yougile_writer import StickerCatalog, normalize_title, write_task

    mid = repo.create_meeting("raw/_coldedup.txt", "awaiting_review")
    routing = {"yougile_column_id": "col-Z"}
    # Та же задача из ДРУГОГО прогона — НОВЫЙ internal_id (как при повторной загрузке).
    task = {"title": "Создать два анонса", "internal_id": f"{mid}-0",
            "priority": None, "controller": None}

    calls = {"create": 0}

    class FakeClient:
        def list_string_stickers(self):
            return []

        def create_task(self, body):
            calls["create"] += 1
            return "yg-new"

    client = FakeClient()
    catalog = StickerCatalog(client)
    # В колонке уже есть задача с таким названием (из прошлого прогона).
    column_titles = {normalize_title("Создать два анонса"): "yg-existing-1"}

    r = write_task(client, catalog, mid, task, routing, column_titles=column_titles)
    check("duplicate title -> skipped_existing", r["status"] == "skipped_existing", str(r))
    check("points to existing YouGile task", r["yougile_task_id"] == "yg-existing-1")
    check("YouGile create NOT called for dup", calls["create"] == 0, str(calls))

    # Новая (другая) задача — создаётся и попадает в карту, второй такой же — пропускается.
    column_titles2 = {}
    t1 = {"title": "Новая уникальная задача", "internal_id": f"{mid}-1", "priority": None, "controller": None}
    t2 = {"title": "новая   уникальная задача", "internal_id": f"{mid}-2", "priority": None, "controller": None}
    a = write_task(client, catalog, mid, t1, routing, column_titles=column_titles2)
    b = write_task(client, catalog, mid, t2, routing, column_titles=column_titles2)
    check("first unique -> written", a["status"] == "written", str(a))
    check("same title in same run -> skipped_existing", b["status"] == "skipped_existing", str(b))


def test_provider_failover() -> None:
    print("test_provider_failover")
    import httpx
    from types import SimpleNamespace
    from openai import APIConnectionError
    from providers import Provider, ProviderPool

    def ok_create(**kw):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))]
        )

    def fail_create(**kw):
        raise APIConnectionError(request=httpx.Request("POST", "http://x"))

    fail = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail_create)))
    ok = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=ok_create)))
    p_primary = Provider("groq", fail, "m")
    p_backup = Provider("openrouter", ok, "m")

    # Основной падает (сетевой сбой) → переключение на запасной
    pool = ProviderPool([p_primary, p_backup])
    _, meta = pool.chat([{"role": "user", "content": "x"}], prefer_index=0)
    check("failover to backup", meta["provider"] == "openrouter" and meta["switched"],
          str(meta))
    check("tried both in order", meta["tried"] == ["groq", "openrouter"], str(meta["tried"]))

    # Round-robin: prefer_index=1 стартует с запасного (он рабочий) — без переключения
    _, meta2 = pool.chat([{"role": "user", "content": "x"}], prefer_index=1)
    check("round-robin starts at backup", meta2["provider"] == "openrouter" and not meta2["switched"],
          str(meta2))

    # Оба падают → исключение пробрасывается
    pool_bad = ProviderPool([p_primary, Provider("openrouter", fail, "m")])
    try:
        pool_bad.chat([{"role": "user", "content": "x"}], prefer_index=0)
        check("all providers down raises", False, "не бросило")
    except APIConnectionError:
        check("all providers down raises", True)


def test_transcribe_format_guard() -> None:
    print("test_transcribe_format_guard")
    try:
        transcribe.transcribe(RAW_DIR / "text.txt")  # .txt — не аудио
        check("rejects non-audio", False, "не бросило исключение")
    except (ValueError, FileNotFoundError):
        check("rejects non-audio", True)


# --------------------------------------------------------------------------- #
# orchestrator.py — интеграция на файле из raw/ (нужен LLM_API_KEY)
# --------------------------------------------------------------------------- #
def test_orchestrator_phase1() -> None:
    print("test_orchestrator_phase1")
    text_files = sorted(p for p in RAW_DIR.glob("*.txt"))
    if not os.environ.get("LLM_API_KEY"):
        skip("phase1 on raw/", "нет LLM_API_KEY")
        return
    if not text_files:
        skip("phase1 on raw/", "нет .txt в raw/")
        return

    import orchestrator

    target = text_files[0]
    state = orchestrator.run_phase1(target)

    # Сбой внешнего API (rate limit / сеть) — не дефект пайплайна: SKIP, не FAIL.
    # Но проверяем, что оркестратор не упал, а корректно вернул error-состояние.
    external_err = state.status == "error" and any(
        m in (state.error or "").lower()
        for m in ("rate_limit", "too large", "413", "connection", "timeout", "429")
    )
    if external_err:
        check("orchestrator handles API error gracefully",
              state.error is not None and isinstance(state.tasks, list))
        skip("phase1 awaiting_review", f"внешний сбой API: {state.error[:80]}")
        return

    check("status awaiting_review", state.status == "awaiting_review",
          f"status={state.status} err={state.error}")
    check("tasks is a list", isinstance(state.tasks, list))
    check("anchor summary present", state.anchor_summary is not None)
    # Каждая задача прошла валидатор → имеет validator_flags
    check("all tasks have validator_flags",
          all("validator_flags" in t for t in state.tasks))
    # internal_id проставлен для идемпотентной записи (фаза 2)
    check("tasks have internal_id", all(t.get("internal_id") for t in state.tasks))
    # Фаза 2 НЕ вызываем в юнит-тестах: она пишет в живой YouGile (проверяется отдельно).


def main() -> None:
    test_anchors()
    test_validate()
    test_chunker()
    test_dedup()
    test_people_lookup()
    test_validate_with_directory()
    test_meeting_state_db()
    test_yougile_write_idempotent()
    test_yougile_write_dedup_by_column_title()
    test_provider_failover()
    test_transcribe_format_guard()
    test_orchestrator_phase1()
    print(f"\n=== passed={_passed} failed={_failed} skipped={_skipped} ===")
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
