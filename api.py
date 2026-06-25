"""
FastAPI-бэкенд E4 — тонкий HTTP-слой поверх существующего пайплайна.
Логику не переписываем: эндпоинты вызывают orchestrator/repo как есть.

Двухфазность сохранена:
  Фаза 1: POST /upload → фаза 1 в ФОНЕ (BackgroundTasks), статус опрашивается.
  [ПАУЗА: ревью менеджером в UI]
  Фаза 2: POST /meetings/{id}/approve → запись одобренных в YouGile.

Эндпоинты:
  GET  /api/projects               — справочник проектов (для формы загрузки)
  POST /api/upload                 — файл + project_id → meeting_id, фаза 1 в фоне
  GET  /api/meetings/{id}/status   — статус из meetings
  GET  /api/meetings/{id}/tasks    — задачи на ревью + флаги валидатора + источник
  POST /api/meetings/{id}/approve  — одобренные задачи → фаза 2 (запись в YouGile)
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import repo
import simplenote_source
from db import create_all
from downloader import DownloadError, download
from orchestrator import run_phase1, run_phase2

BASE_DIR = Path(__file__).parent
UPLOADS = BASE_DIR / "uploads"
UPLOADS.mkdir(exist_ok=True)
WEB_DIST = BASE_DIR / "web" / "dist"

# Тестовый проект YouGile — ссылка на доску для UI (после записи).
YOUGILE_BOARD_URL = "https://ru.yougile.com/team/0/projects"

app = FastAPI(title="Созвон → Задачи (ревью)")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.on_event("startup")
def _startup() -> None:
    # Локально гарантируем таблицы (прод — Alembic).
    create_all()


# --- модели запроса ----------------------------------------------------------
class ApproveBody(BaseModel):
    tasks: list[dict]  # одобренные/отредактированные задачи (каждая с internal_id)


class PersonBody(BaseModel):
    name: str
    specialty: Optional[str] = None
    project: Optional[str] = None


class SimpleNoteProcessBody(BaseModel):
    note_ids: list[str]
    project: Optional[str] = None
    yougile_project_id: Optional[str] = None
    yougile_board_id: Optional[str] = None
    yougile_column_id: Optional[str] = None


def _persist_yougile_projects_cache(projects: list[dict]) -> None:
    """Сохранить маршруты реальных проектов в таблицу projects (для записи задач)."""
    for p in projects:
        if p.get("board_id") and p.get("column_id"):
            repo.upsert_project_route(
                p["title"], p.get("project_id"), p["board_id"], p["column_id"]
            )


def _resolve_yougile_user(name: str) -> tuple[Optional[str], Optional[str]]:
    """Имя → YouGile user_id по realName. (id, warning). id=None если не найдено/неоднозначно."""
    try:
        from sync_yougile import get_users
        users = get_users()
    except Exception as e:  # noqa: BLE001
        return None, f"YouGile недоступен ({e}); сохранено без ID — сматчится позже."
    n = name.strip().lower()
    exact = [u for u in users if (u.get("realName") or u.get("name") or "").strip().lower() == n]
    part = [u for u in users if n in (u.get("realName") or u.get("name") or "").strip().lower()]
    cands = exact or part
    if len(cands) == 1:
        return cands[0]["id"], None
    if len(cands) > 1:
        return None, f"В YouGile несколько пользователей под «{name}» — ID не проставлен, уточните вручную."
    return None, f"В YouGile нет пользователя «{name}» — сохранено без ID (сматчится, когда появится)."


# --- фоновая фаза 1 ----------------------------------------------------------
def _run_phase1_bg(meeting_id: int, file_path: str, project: str | None) -> None:
    # persist=True, db_id уже создан в /upload → переиспользуем строку meetings.
    run_phase1(file_path, project=project, db_id=meeting_id, persist=True)


# --- эндпоинты ---------------------------------------------------------------
@app.get("/api/projects")
def get_projects() -> list[dict]:
    return repo.list_projects()


@app.get("/api/yougile/projects")
def get_yougile_projects() -> list[dict]:
    """РЕАЛЬНЫЕ проекты из YouGile (логика sync_yougile list-projects).

    Для каждого проекта берём первую доску и первую колонку как маршрут по умолчанию.
    Заодно кэшируем маршрут в таблицу projects, чтобы выбранный проект был записываемым.
    """
    from sync_yougile import get_boards, get_columns, get_projects as yg_projects

    out: list[dict] = []
    for p in yg_projects():
        boards = get_boards(p["id"])
        board = boards[0] if boards else None
        col = None
        if board:
            cols = get_columns(board["id"])
            col = cols[0] if cols else None
        out.append({
            "project_id": p["id"], "title": p.get("title"),
            "board_id": board["id"] if board else None,
            "board_title": board.get("title") if board else None,
            "column_id": col["id"] if col else None,
            "column_title": col.get("title") if col else None,
        })
    _persist_yougile_projects_cache(out)
    return out


@app.get("/api/people")
def get_people(project: Optional[str] = None) -> list[dict]:
    return repo.list_people(project)


@app.post("/api/people")
def add_person(body: PersonBody) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "Имя обязательно")
    user_id, warning = _resolve_yougile_user(body.name)
    person = repo.upsert_person(body.name.strip(), body.specialty, body.project, user_id)
    return {"person": person, "warning": warning}


@app.put("/api/people/{person_id}")
def edit_person(person_id: int, body: PersonBody) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "Имя обязательно")
    # Имя могло измениться → пересопоставляем YouGile ID.
    user_id, warning = _resolve_yougile_user(body.name)
    person = repo.update_person(person_id, body.name.strip(), body.specialty, body.project, user_id)
    if person is None:
        raise HTTPException(404, "Сотрудник не найден")
    return {"person": person, "warning": warning}


@app.delete("/api/people/{person_id}")
def remove_person(person_id: int) -> dict:
    if not repo.delete_person(person_id):
        raise HTTPException(404, "Сотрудник не найден")
    return {"deleted": person_id}


@app.get("/api/simplenote/notes")
def simplenote_notes(tag: Optional[str] = "task") -> list[dict]:
    """Список заметок SimpleNote (по умолчанию фильтр по тегу 'task'). Только чтение."""
    try:
        notes = simplenote_source.get_notes(tag or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"SimpleNote: {e}")
    # content в списке не отдаём целиком — только превью/тип (контент тянем при обработке)
    return [{"id": n["id"], "preview": n["preview"], "type": n["type"],
             "modifydate": n["modifydate"], "tags": n["tags"]} for n in notes]


@app.post("/api/simplenote/process")
def simplenote_process(body: SimpleNoteProcessBody, background: BackgroundTasks) -> dict:
    """Обработать выбранные заметки. Структурные — быстрый путь (без LLM), сырые — через LLM.

    Каждая заметка → отдельный meeting(awaiting_review), как и аудио/ссылка.
    """
    if not body.note_ids:
        raise HTTPException(400, "Не выбрано ни одной заметки")
    # Маршрут выбранного проекта YouGile → таблица projects (как в /upload).
    if body.project and body.yougile_board_id and body.yougile_column_id:
        repo.upsert_project_route(body.project, body.yougile_project_id,
                                  body.yougile_board_id, body.yougile_column_id)

    results: list[dict] = []
    for nid in body.note_ids:
        try:
            content = simplenote_source.get_note_content(nid)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"SimpleNote: {e}")
        if content is None:
            results.append({"note_id": nid, "error": "заметка не найдена"})
            continue

        ntype = simplenote_source.classify_note(content)
        meeting_id = repo.create_meeting(source_file=f"simplenote:{nid}", status="uploaded")

        if ntype == "structured":
            # Быстрый путь: без LLM, синхронно.
            n = simplenote_source.process_structured(meeting_id, content, body.project)
            results.append({"note_id": nid, "meeting_id": meeting_id,
                            "type": "structured", "tasks": n})
        else:
            # Сырая заметка → обычный пайплайн (LLM) в фоне: пишем контент во временный файл.
            dest = UPLOADS / f"meeting_{meeting_id}.txt"
            dest.write_text(content, encoding="utf-8")
            background.add_task(_run_phase1_bg, meeting_id, str(dest), body.project)
            results.append({"note_id": nid, "meeting_id": meeting_id, "type": "raw"})

    return {"results": results}


@app.post("/api/upload")
async def upload(
    background: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    project: Optional[str] = Form(None),
    yougile_project_id: Optional[str] = Form(None),
    yougile_board_id: Optional[str] = Form(None),
    yougile_column_id: Optional[str] = Form(None),
) -> dict:
    if file is None and not (url and url.strip()):
        raise HTTPException(400, "Нужен файл или ссылка")

    meeting_id = repo.create_meeting(source_file=(file.filename if file else url), status="uploaded")

    if file is not None:
        suffix = Path(file.filename or "upload.txt").suffix or ".txt"
        dest = UPLOADS / f"meeting_{meeting_id}{suffix}"
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    else:
        try:
            dest = download(url.strip(), meeting_id)
        except DownloadError as e:
            repo.set_status(meeting_id, "error")
            raise HTTPException(400, str(e))

    # Маршрут выбранного РЕАЛЬНОГО проекта YouGile → таблица projects (ядро читает её по имени).
    if project and yougile_board_id and yougile_column_id:
        repo.upsert_project_route(project, yougile_project_id, yougile_board_id, yougile_column_id)

    background.add_task(_run_phase1_bg, meeting_id, str(dest), project)
    return {"meeting_id": meeting_id, "status": "uploaded"}


@app.get("/api/meetings/{meeting_id}/status")
def get_status(meeting_id: int) -> dict:
    m = repo.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(404, "meeting not found")
    return {"meeting_id": meeting_id, "status": m["status"]}


@app.get("/api/meetings/{meeting_id}/tasks")
def get_tasks(meeting_id: int) -> dict:
    m = repo.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(404, "meeting not found")
    result = m.get("result_json") or {}
    return {
        "meeting_id": meeting_id,
        "status": m["status"],
        "tasks": result.get("tasks", []),
        "project_routing": result.get("project_routing"),
    }


@app.post("/api/meetings/{meeting_id}/approve")
def approve(meeting_id: int, body: ApproveBody) -> dict:
    m = repo.get_meeting(meeting_id)
    if m is None:
        raise HTTPException(404, "meeting not found")
    # Фаза 2 — запись одобренных в YouGile (идемпотентно).
    summary = run_phase2(meeting_id, approved_tasks=body.tasks)
    # Ссылки на записанное (id задачи в YouGile).
    written = [
        {"internal_id": d.get("internal_id"), "yougile_task_id": d.get("yougile_task_id"),
         "status": d.get("status")}
        for d in summary.get("details", [])
    ]
    summary["written_links"] = written
    summary["board_url"] = YOUGILE_BOARD_URL
    return summary


# --- статика SPA (если собрана) ---------------------------------------------
if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
