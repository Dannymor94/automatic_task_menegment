"""
SimpleNote как ВТОРОЙ источник входа (наравне с аудио). Только ЧТЕНИЕ заметок.

Синхронизацию устройств не трогаем — SimpleNote сам синкается между телефоном и ПК;
мы лишь читаем заметки через его сервис (Simperium) библиотекой `simplenote`.

Развилка по типу заметки:
- структурная (есть #task / якорь ЗАДАЧА / чёткие пункты-буллеты) → быстрый путь
  без полной LLM-декомпозиции: пункты → задачи детерминированно;
- сырая (поток мыслей) → обычный пайплайн через LLM (это делает оркестратор как для текста).

Ядро (chunker/decompose/validate/запись) не меняется: сырые заметки идут через тот же
run_phase1 (контент пишется во временный файл), структурные собираются здесь и проходят
ту же валидацию (validate_tasks) и то же хранилище (meetings.result_json, awaiting_review).
"""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv

import anchors as anchors_mod
import repo
from people_lookup import PeopleDirectory
from validate import validate_tasks

load_dotenv()

DEFAULT_TAG = "task"
# Маркеры пунктов-задач: -, *, •, 1. / 1), [ ] / [x]
_BULLET = re.compile(r"^\s*(?:[-*•]|\[[ xX]?\]|\d+[.)])\s+")
_HASHTAG = re.compile(r"#\w+")


def _strip_markers(line: str) -> str:
    """Снять все ведущие маркеры пункта (напр. «- [ ] текст» → «текст»)."""
    prev = None
    while line != prev:
        prev = line
        line = _BULLET.sub("", line, count=1)
    return line.strip()


# --- клиент SimpleNote --------------------------------------------------------
def _client():
    email = os.environ.get("SIMPLENOTE_EMAIL")
    password = os.environ.get("SIMPLENOTE_PASSWORD")
    if not email or not password:
        raise RuntimeError("SIMPLENOTE_EMAIL / SIMPLENOTE_PASSWORD не заданы в .env")
    from simplenote import Simplenote
    return Simplenote(email, password)


def _preview(content: str) -> str:
    for line in (content or "").splitlines():
        s = line.strip()
        if s:
            return s[:120]
    return ""


# --- классификация и сборка задач (детерминированно, без LLM) -----------------
def classify_note(content: str) -> str:
    """'structured' (быстрый путь) | 'raw' (через LLM)."""
    lines = [l for l in (content or "").splitlines() if l.strip()]
    if not lines:
        return "raw"
    by_type = anchors_mod.summarize(content).get("by_type", {})
    bullets = [l for l in lines if _BULLET.match(l)]
    # Структура — только при явных маркерах задач: якорь ЗАДАЧА, либо список-буллеты
    # (≥2 пункта, или все строки — буллеты). Иначе (поток мыслей) → raw, через LLM.
    if by_type.get("TASK"):
        return "structured"
    if len(bullets) >= 2 or (bullets and len(bullets) == len(lines)):
        return "structured"
    return "raw"


def build_structured_tasks(content: str) -> list[dict]:
    """Структурная заметка → список задач без LLM. Каждый пункт/строка-якорь → задача."""
    lines = [l for l in (content or "").splitlines() if l.strip()]
    bullets = [l for l in lines if _BULLET.match(l)]

    if bullets:
        items = [_strip_markers(l) for l in bullets]
    else:
        task_anchors = [a for a in anchors_mod.parse_anchors(content)
                        if a["type"] == "TASK" and a["value"]]
        if task_anchors:
            items = [a["value"] for a in task_anchors]
        elif len(lines) == 1:
            items = [lines[0].strip()]
        else:
            items = [l.strip() for l in lines]

    tasks: list[dict] = []
    for raw_item in items:
        title = _HASHTAG.sub("", raw_item).strip()  # убрать #task и пр. теги из заголовка
        if not title:
            continue
        tasks.append({
            "project": None,
            "title": title,
            "description": "",
            "checklist": [],
            "acceptance_criteria": [],
            "assignee": None,
            "controller": None,
            "due_date": None,
            "priority": "medium",
            "source": raw_item,
            "anchor_used": bool(anchors_mod.parse_anchors(raw_item)),
            "confidence": 0.6,
            "needs_review": True,
        })
    return tasks


# --- чтение заметок -----------------------------------------------------------
def get_notes(tag: str | None = DEFAULT_TAG) -> list[dict]:
    """Список заметок (опц. по тегу). Возвращает {id, content, modifydate, preview, type, tags}."""
    sn = _client()
    note_list, status = sn.get_note_list(data=True, tags=[tag] if tag else [])
    if status != 0:
        raise RuntimeError("SimpleNote: не удалось получить список заметок (проверь логин/пароль)")

    out: list[dict] = []
    for meta in note_list:
        if meta.get("deleted"):
            continue
        content = meta.get("content")
        if content is None:  # на всякий случай дочитываем тело
            note, st = sn.get_note(meta["key"])
            if st != 0:
                continue
            content = note.get("content", "")
        out.append({
            "id": meta["key"],
            "content": content,
            "modifydate": meta.get("modifydate"),
            "preview": _preview(content),
            "type": classify_note(content),
            "tags": meta.get("tags", []),
        })
    out.sort(key=lambda n: str(n.get("modifydate") or ""), reverse=True)
    return out


def get_note_content(note_id: str) -> str | None:
    sn = _client()
    note, status = sn.get_note(note_id)
    return note.get("content", "") if status == 0 else None


# --- быстрый путь: структурная заметка → meeting(awaiting_review) -------------
def process_structured(meeting_id: int, content: str, project: str | None) -> int:
    """Структурную заметку собрать в задачи без LLM, провалидировать и сохранить в meeting.

    Использует ту же валидацию (validate_tasks) и то же хранилище, что и обычный пайплайн.
    Возвращает число задач.
    """
    repo.set_status(meeting_id, "processing")
    tasks = build_structured_tasks(content)
    for i, t in enumerate(tasks):
        t["internal_id"] = f"{meeting_id}-{i}"

    routing = repo.get_project_routing(project) if project else None
    if project:
        for t in tasks:
            if not t.get("project"):
                t["project"] = project

    directory = PeopleDirectory.from_db()
    validate_tasks(tasks, directory=directory)

    repo.save_result(
        meeting_id,
        {"tasks": tasks, "project_routing": routing, "anchor_summary": None,
         "source": "simplenote", "fast_path": True},
        status="awaiting_review",
    )
    return len(tasks)
