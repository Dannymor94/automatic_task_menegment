"""
Оркестратор (E1→E2) — тонкий Python-слой, ведёт пайплайн по порядку.

Фаза 1 (авто): transcribe → anchors → decompose → dedup → people-lookup+validate.
Останавливается на статусе `awaiting_review` и возвращает задачи менеджеру.
[ПАУЗА: ревью менеджером — пайплайн ждёт человека]
Фаза 2 (по одобрению): запускается отдельно по списку одобренных задач.
  Запись в YouGile — вне области E1/E2 (см. yougile-integration, этап E3). Здесь — заглушка.

E2: состояние созвона ведётся в БД (таблица meetings), не только в памяти:
  uploaded → transcribing → processing → awaiting_review (→ done на E3 / error).
Результат фазы 1 пишется в meetings.result_json. Маршрут проекта (yougile board/column)
берётся из таблицы projects по выбранному проекту. People lookup подставляет реальные
YouGile user ID в assignee_id/controller_id. Шаги логируются в logs/e1.jsonl.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import anchors as anchors_mod
import repo
from chunker import chunk_transcript, estimate_tokens
from decompose import (
    build_pool,
    count_low_confidence,
    decompose_pool,
    load_system_prompt,
    resolve_prompt_version,
)
from dedup import dedup_tasks
from people_lookup import PeopleDirectory
from transcribe import SUPPORTED_FORMATS as AUDIO_FORMATS
from transcribe import transcribe
from validate import validate_tasks
from yougile_writer import StickerCatalog, YouGileClient, load_column_titles, write_task

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "e1.jsonl"
TEXT_FORMATS = {".md", ".txt"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


@dataclass
class PipelineState:
    """Состояние одного созвона в памяти, живёт между фазами. Зеркалится в БД (meetings)."""

    meeting_id: str
    input_path: str
    status: str = "new"  # uploaded → transcribing → processing → awaiting_review → done/error
    transcript: str | None = None
    anchor_summary: dict | None = None
    tasks: list[dict] = field(default_factory=list)
    error: str | None = None
    db_id: int | None = None          # PK строки в meetings
    project_routing: dict | None = None  # yougile project/board/column по выбранному проекту


def _log_step(meeting_id: str, step: str, ok: bool, elapsed: float, **extra) -> None:
    record = {
        "meeting_id": meeting_id,
        "step": step,
        "ok": ok,
        "elapsed_seconds": round(elapsed, 2),
        **extra,
    }
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    status = "OK" if ok else "ERROR"
    log.info("[%s] %s [%s] %.2fs %s", meeting_id, step, status, elapsed, extra or "")


def _load_input_text(state: PipelineState) -> str:
    """Шаг 1: аудио → транскрипция; текст → чтение. Возвращает транскрипт."""
    path = Path(state.input_path)
    suffix = path.suffix.lower()
    t0 = time.monotonic()

    if suffix in AUDIO_FORMATS:
        text = transcribe(path)
        _log_step(
            state.meeting_id, "transcribe", True, time.monotonic() - t0,
            input=path.name, chars=len(text),
        )
    elif suffix in TEXT_FORMATS:
        text = path.read_text(encoding="utf-8")
        _log_step(
            state.meeting_id, "read_text", True, time.monotonic() - t0,
            input=path.name, chars=len(text),
        )
    else:
        raise ValueError(
            f"Неподдерживаемый вход {suffix!r}. "
            f"Аудио: {sorted(AUDIO_FORMATS)}; текст: {sorted(TEXT_FORMATS)}"
        )
    return text


def run_phase1(
    input_path: str | Path,
    meeting_id: str | None = None,
    project: str | None = None,
    people: set[str] | None = None,
    target_tokens: int = 2000,
    round_robin: bool = True,
    cache: bool | None = None,
    prompt_version: str | None = None,
    persist: bool = True,
    db_id: int | None = None,
) -> PipelineState:
    """Фаза 1 (авто): шаги 1–6. Финал — статус awaiting_review с задачами на ревью.

    project — имя проекта (явный выбор формы); по нему берётся YouGile board/column
    из таблицы projects (маршрут). Якорь ПРОЕКТ как маршрут НЕ применяется (решение E1).
    target_tokens — целевой размер чанка; меньше → больше чанков (для длинных созвонов
    или жёстких лимитов токенов на запрос).
    round_robin — чередовать стартовый провайдер по чанкам (делит нагрузку между
    бесплатными лимитами); failover на запасной работает всегда.
    cache — response cache OpenRouter для отладочных идентичных прогонов.
    prompt_version — 'en' (дефолт, по итогам A/B) или 'ru' (альтернатива). Можно также
    задать через env LLM_PROMPT_VERSION.
    persist — вести состояние в БД (meetings). False — только в памяти (для юнит-тестов).
    """
    input_path = str(input_path)
    meeting_id = meeting_id or Path(input_path).stem
    state = PipelineState(meeting_id=meeting_id, input_path=input_path, status="uploaded")

    # Состояние созвона в БД: uploaded. db_id может быть создан заранее (async upload).
    if persist:
        state.db_id = db_id if db_id is not None else repo.create_meeting(
            source_file=input_path, status="uploaded"
        )

    def _persist_status(status: str) -> None:
        state.status = status
        if persist and state.db_id is not None:
            repo.set_status(state.db_id, status)

    try:
        # Маршрут проекта из таблицы projects (по явно выбранному имени).
        if project and persist:
            state.project_routing = repo.get_project_routing(project)

        # Шаг 1 — транскрипция / чтение
        _persist_status("transcribing")
        state.transcript = _load_input_text(state)
        _persist_status("processing")

        # Шаг 2.5 — нарезка на чанки (чтобы не упереться в лимит токенов на запрос)
        t0 = time.monotonic()
        chunks = chunk_transcript(state.transcript, target_tokens=target_tokens)
        _log_step(
            state.meeting_id, "chunk", True, time.monotonic() - t0,
            n_chunks=len(chunks),
            chunk_tokens_est=[estimate_tokens(c) for c in chunks],
        )

        # Шаг 3 — парсинг якорей по всему транскрипту (для сводки/режима)
        t0 = time.monotonic()
        state.anchor_summary = anchors_mod.summarize(state.transcript)
        anchor_project = state.anchor_summary.get("project")
        _log_step(
            state.meeting_id, "anchors", True, time.monotonic() - t0,
            anchors_found=state.anchor_summary["anchors_found"],
            by_type=state.anchor_summary["by_type"],
            project=anchor_project,
        )

        # Шаг 5 — декомпозиция: ОТДЕЛЬНЫЙ LLM-вызов на каждый чанк (failover + round-robin)
        t0 = time.monotonic()
        pool = build_pool(round_robin=round_robin, cache=cache)
        version = resolve_prompt_version(prompt_version)  # 'en' по умолчанию
        system = load_system_prompt(version=version)
        # Контекст команды проекта (имя+специальность) — подсказка модели для assignee.
        team = repo.list_people(project) if (persist and project) else None
        mode = "anchors" if state.anchor_summary["anchors_found"] > 0 else "none"
        collected: list[dict] = []
        switches = 0
        providers_used: list[str] = []
        for i, chunk in enumerate(chunks):
            chunk_anchors = anchors_mod.summarize(chunk)["anchors_found"]
            ct0 = time.monotonic()
            # round-robin: чанк i стартует с провайдера i % N, failover на остальные
            chunk_tasks, _, meta = decompose_pool(chunk, pool, system, prefer_index=i, team=team)
            collected.extend(chunk_tasks)
            providers_used.append(meta["provider"])
            if meta["switched"]:
                switches += 1
            _log_step(
                state.meeting_id, "decompose_chunk", True, time.monotonic() - ct0,
                chunk=i, of=len(chunks), tokens_est=estimate_tokens(chunk),
                task_count=len(chunk_tasks), anchors_in_chunk=chunk_anchors,
                provider=meta["provider"], switched=meta["switched"], tried=meta["tried"],
            )
        tasks_before_dedup = len(collected)
        _log_step(
            state.meeting_id, "decompose", True, time.monotonic() - t0,
            prompt_version=version, providers=pool.names, n_chunks=len(chunks),
            task_count=tasks_before_dedup,
            low_confidence_count=count_low_confidence(collected),
            is_empty=(tasks_before_dedup == 0), mode=mode,
            providers_used=providers_used, failover_switches=switches,
        )

        # Шаг 5.5 — дедупликация задач со стыков чанков (без векторов, E6 — потом)
        t0 = time.monotonic()
        tasks, removed = dedup_tasks(collected)
        _log_step(
            state.meeting_id, "dedup", True, time.monotonic() - t0,
            tasks_before=tasks_before_dedup, tasks_after=len(tasks),
            duplicates_removed=removed,
        )

        # Маршрут проекта — ТОЛЬКО из явного параметра формы. Якорь ПРОЕКТ,
        # пойманный нечётко, ненадёжен: слово «проект» звучит и в обычной речи
        # («по проекту растём»), и автозаполнение им затрёт project мусором.
        # anchor_project остаётся подсказкой в логе/сводке, но не применяется —
        # реальный якорь ПРОЕКТ модель сама проставит в task.project из текста.
        if project:
            for t in tasks:
                if not t.get("project"):
                    t["project"] = project

        # Шаг 4+6 — people lookup + валидация: подстановка реальных YouGile ID и флаги
        t0 = time.monotonic()
        directory = PeopleDirectory.from_db() if persist else None
        validate_tasks(tasks, directory=directory)
        flagged = sum(1 for t in tasks if t.get("validator_flags"))
        resolved = sum(1 for t in tasks if t.get("assignee_id") or t.get("controller_id"))
        _log_step(
            state.meeting_id, "validate", True, time.monotonic() - t0,
            task_count=len(tasks), flagged=flagged,
            people_in_directory=(len(directory) if directory else 0),
            ids_resolved=resolved,
        )

        # Стабильный internal_id для идемпотентности записи (фаза 2): "<db_id>-<i>".
        for i, t in enumerate(tasks):
            t["internal_id"] = f"{state.db_id}-{i}" if state.db_id is not None else f"mem-{i}"

        state.tasks = tasks
        _persist_status("awaiting_review")  # ПАУЗА: ждём ревью менеджера

        # Результат фазы 1 в meetings.result_json (задачи + мета-маршрут).
        if persist and state.db_id is not None:
            repo.save_result(
                state.db_id,
                {"tasks": tasks, "project_routing": state.project_routing,
                 "anchor_summary": state.anchor_summary},
                status="awaiting_review",
            )
    except Exception as exc:  # noqa: BLE001
        state.error = str(exc)
        _persist_status("error")
        _log_step(state.meeting_id, "phase1", False, 0.0, error=str(exc))
        log.error("[%s] phase1 failed: %s", meeting_id, exc)

    return state


def run_phase2(
    meeting_db_id: int,
    approved_tasks: list[dict] | None = None,
) -> dict:
    """Фаза 2 (по одобрению): идемпотентная запись одобренных задач в YouGile (E3).

    approved_tasks — список одобренных менеджером задач. None → берём все задачи из
    meetings.result_json (имитация «одобрить всё»). Маршрут (колонка YouGile) — оттуда же.
    Идемпотентность: уже записанные (task_writes='written') пропускаются — дубль не создаём.
    По завершении meeting → done (если без ошибок).
    """
    meeting = repo.get_meeting(meeting_db_id)
    if meeting is None:
        raise ValueError(f"meeting {meeting_db_id} не найден")
    result = meeting.get("result_json") or {}
    routing = result.get("project_routing")
    tasks = approved_tasks if approved_tasks is not None else result.get("tasks", [])

    t0 = time.monotonic()
    client = YouGileClient()
    catalog = StickerCatalog(client)
    # Снимок названий задач в целевой колонке — дедуп на уровне записи (защита от дублей
    # от повторных загрузок того же созвона и любых одинаковых названий).
    column_id = (routing or {}).get("yougile_column_id")
    column_titles = load_column_titles(client, column_id)
    written = skipped = skipped_existing = failed = 0
    details: list[dict] = []

    for task in tasks:
        try:
            r = write_task(client, catalog, meeting_db_id, task, routing, column_titles=column_titles)
            if r["status"] == "written":
                written += 1
            elif r["status"] == "skipped":
                skipped += 1
            elif r["status"] == "skipped_existing":
                skipped_existing += 1
            details.append(r)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            details.append({"status": "error", "internal_id": task.get("internal_id"),
                            "error": str(exc)})

    final_status = "done" if failed == 0 else "awaiting_review"  # ошибки → можно повторить
    repo.set_status(meeting_db_id, final_status)

    _log_step(str(meeting_db_id), "phase2", failed == 0, time.monotonic() - t0,
              written=written, skipped=skipped, skipped_existing=skipped_existing,
              failed=failed, meeting_status=final_status)
    log.info("[%s] phase2: записано=%d пропущено_маркер=%d пропущено_в_колонке=%d ошибок=%d → meeting=%s",
             meeting_db_id, written, skipped, skipped_existing, failed, final_status)
    return {"meeting_id": meeting_db_id, "written": written, "skipped": skipped,
            "skipped_existing": skipped_existing, "failed": failed,
            "status": final_status, "details": details}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <audio_or_text_file> [project]")
        raise SystemExit(1)

    proj = sys.argv[2] if len(sys.argv) > 2 else None
    result = run_phase1(sys.argv[1], project=proj)
    print(f"\n=== Фаза 1: {result.status} ===")
    if result.error:
        print(f"Ошибка: {result.error}")
    else:
        if result.anchor_summary:
            print(f"Якоря: {result.anchor_summary['anchors_found']} "
                  f"{result.anchor_summary['by_type']} проект={result.anchor_summary['project']}")
        print(f"Задач на ревью: {len(result.tasks)}")
        print(json.dumps(result.tasks, ensure_ascii=False, indent=2))
