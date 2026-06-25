"""
YouGile writer (E3) — идемпотентная запись задачи в YouGile со стикерами.
Реализует yougile-integration. Контракт подтверждён на живом API:

  POST /tasks {title, columnId, description, assigned:[userId],
               deadline:{deadline:<ms>, withTime:false}, stickers:{stickerId:stateId},
               subtasks:[taskId]}
  POST /string-stickers {name, icon, states:[{name,color}]}
  POST /string-stickers/{id}/states {name,color}   — добавить состояние
  PUT  /tasks/{id} {deleted:true}                   — мягкое удаление

Маппинг (PROJECT_GUIDE §6):
  assignee_id  → assigned[]               (Исполнитель)
  controller   → кастомный стикер «Контролёр» (состояние = имя контролёра)
  due_date     → deadline (timestamp мс)  (Дедлайн)
  priority     → существующий стикер «Приоритет» (low|medium|high|urgent → low|normal|major|critical)
  description  → описание задачи
  checklist    → подзадачи (subtasks)

ИДЕМПОТЕНТНОСТЬ: перед созданием проверяем task_writes по internal_task_id. Если 'written' —
пропускаем (не дублируем). Маркер 'written' ставится ТОЛЬКО после успешного ответа API.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

import repo

load_dotenv()
log = logging.getLogger(__name__)

BASE_URL = os.environ.get("YOUGILE_BASE_URL", "https://ru.yougile.com/api-v2")
RETRYABLE = {429, 500, 502, 503, 504}

# Наши приоритеты → имена состояний существующего стикера «Приоритет».
PRIORITY_TO_STATE = {"low": "low", "medium": "normal", "high": "major", "urgent": "critical"}
CONTROLLER_STICKER_NAME = "Контролёр"
PRIORITY_STICKER_NAME = "Приоритет"
_STATE_COLOR = "#B0C3CC"


class YouGileClient:
    def __init__(self, base_url: str = BASE_URL, api_key: str | None = None):
        self.base = base_url
        self.key = api_key or os.environ.get("YOUGILE_API_KEY")
        if not self.key:
            raise RuntimeError("YOUGILE_API_KEY не задан в .env")

    def _req(self, method: str, path: str, *, json=None, params=None, retries=2) -> dict:
        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        for attempt in range(retries + 1):
            resp = requests.request(method, url, headers=headers, json=json, params=params, timeout=30)
            if resp.status_code in RETRYABLE and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()
        return {}

    # --- задачи ---
    def create_task(self, body: dict) -> str:
        return self._req("POST", "/tasks", json=body)["id"]

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/tasks/{task_id}")

    def delete_task(self, task_id: str) -> None:
        self._req("PUT", f"/tasks/{task_id}", json={"deleted": True})

    # --- строковые стикеры ---
    def list_string_stickers(self) -> list[dict]:
        return self._req("GET", "/string-stickers", params={"limit": 1000}).get("content", [])

    def get_string_sticker(self, sticker_id: str) -> dict:
        return self._req("GET", f"/string-stickers/{sticker_id}")

    def create_string_sticker(self, name: str, states: list[dict]) -> str:
        return self._req("POST", "/string-stickers", json={"name": name, "states": states})["id"]

    def add_sticker_state(self, sticker_id: str, name: str, color: str = _STATE_COLOR) -> str:
        return self._req("POST", f"/string-stickers/{sticker_id}/states",
                         json={"name": name, "color": color})["id"]


class StickerCatalog:
    """Резолвит стикеры Приоритет/Контролёр в (sticker_id, state_id). Кэширует в памяти."""

    def __init__(self, client: YouGileClient):
        self.client = client
        self._loaded = False
        self._priority_id: str | None = None
        self._priority_states: dict[str, str] = {}      # name -> state_id
        self._controller_id: str | None = None
        self._controller_states: dict[str, str] = {}    # name -> state_id

    def _load(self) -> None:
        if self._loaded:
            return
        for st in self.client.list_string_stickers():
            if st.get("name") == PRIORITY_STICKER_NAME and self._priority_id is None:
                self._priority_id = st["id"]
                self._priority_states = {s["name"]: s["id"] for s in st.get("states", [])}
            elif st.get("name") == CONTROLLER_STICKER_NAME and self._controller_id is None:
                self._controller_id = st["id"]
                self._controller_states = {s["name"]: s["id"] for s in st.get("states", [])}
        self._loaded = True

    def priority(self, priority: str | None) -> tuple[str, str] | None:
        """(sticker_id, state_id) для приоритета или None, если стикера/состояния нет."""
        if not priority:
            return None
        self._load()
        if not self._priority_id:
            return None  # стикера Приоритет нет — не создаём, просто пропускаем
        state_name = PRIORITY_TO_STATE.get(priority.lower())
        state_id = self._priority_states.get(state_name) if state_name else None
        if state_id is None:
            return None
        return self._priority_id, state_id

    def controller(self, name: str | None) -> tuple[str, str] | None:
        """(sticker_id, state_id) для контролёра. Стикер/состояние создаём при отсутствии."""
        if not name:
            return None
        self._load()
        if self._controller_id is None:
            self._controller_id = self.client.create_string_sticker(
                CONTROLLER_STICKER_NAME, states=[{"name": name, "color": _STATE_COLOR}]
            )
            # перечитать состояния
            sd = self.client.get_string_sticker(self._controller_id)
            self._controller_states = {s["name"]: s["id"] for s in sd.get("states", [])}
        if name not in self._controller_states:
            state_id = self.client.add_sticker_state(self._controller_id, name)
            self._controller_states[name] = state_id
        return self._controller_id, self._controller_states[name]


def _due_to_ms(due_date: str | None) -> int | None:
    if not due_date:
        return None
    try:
        dt = datetime.strptime(due_date, "%Y-%m-%d")
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def build_task_body(task: dict, routing: dict, catalog: StickerCatalog) -> dict:
    column_id = (routing or {}).get("yougile_column_id")
    if not column_id:
        raise ValueError("В маршруте проекта нет yougile_column_id — некуда писать задачу")

    body: dict = {"title": task.get("title") or "Без названия", "columnId": column_id}
    if task.get("description"):
        body["description"] = task["description"]
    if task.get("assignee_id"):
        body["assigned"] = [task["assignee_id"]]

    ms = _due_to_ms(task.get("due_date"))
    if ms is not None:
        body["deadline"] = {"deadline": ms, "withTime": False}

    stickers: dict[str, str] = {}
    prio = catalog.priority(task.get("priority"))
    if prio:
        stickers[prio[0]] = prio[1]
    # Контролёр: по имени (controller), id подставит lookup; стикер хранит имя как состояние.
    ctrl = catalog.controller(task.get("controller"))
    if ctrl:
        stickers[ctrl[0]] = ctrl[1]
    if stickers:
        body["stickers"] = stickers
    return body


_TITLE_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_TITLE_WS = re.compile(r"\s+")


def normalize_title(title) -> str:
    """Нормализация title для сравнения: регистр, пунктуация, пробелы."""
    t = (title or "").lower().strip()
    t = _TITLE_PUNCT.sub(" ", t)
    return _TITLE_WS.sub(" ", t).strip()


def load_column_titles(client: "YouGileClient", column_id: str | None) -> dict:
    """{нормализованный title → yougile_task_id} активных задач целевой колонки.

    Используется как защита от дублей на уровне записи: задача с таким же названием
    уже есть в колонке → не создаём повторно (ловит и повторные загрузки, и дубли
    в одном прогоне — независимо от internal_id, который привязан к meeting_id).
    """
    if not column_id:
        return {}
    data = client._req("GET", "/tasks", params={"columnId": column_id, "limit": 1000})
    out: dict[str, str] = {}
    for t in data.get("content", []):
        if t.get("deleted") or t.get("archived"):
            continue
        nt = normalize_title(t.get("title"))
        if nt:
            out.setdefault(nt, t.get("id"))
    return out


def write_task(
    client: YouGileClient,
    catalog: StickerCatalog,
    meeting_id: int,
    task: dict,
    routing: dict,
    column_titles: dict | None = None,
) -> dict:
    """Идемпотентно записать одну задачу. Возвращает {status, internal_id, yougile_task_id}.

    column_titles — карта {нормализованный title → task_id} уже существующих задач
    целевой колонки YouGile. Если задача с таким названием там уже есть — НЕ создаём
    повторно (дедуп на уровне записи; закрывает дубли от повторных загрузок и
    идентичные задачи, не пойманные ранее). Карта обновляется по мере создания.
    """
    internal_id = task.get("internal_id")
    if not internal_id:
        raise ValueError("У задачи нет internal_id — фаза 1 не проставила маркер")

    # 1) Идемпотентность по маркеру: уже записано → пропускаем.
    existing = repo.get_task_write_status(internal_id)
    if existing and existing["status"] == "written":
        return {"status": "skipped", "internal_id": internal_id,
                "yougile_task_id": existing["yougile_task_id"]}

    # 2) Дедуп на уровне записи: задача с таким title уже есть в колонке → не дублируем.
    norm = normalize_title(task.get("title"))
    if column_titles is not None and norm and norm in column_titles:
        existing_id = column_titles[norm]
        repo.upsert_task_write(meeting_id, internal_id, status="written", yougile_task_id=existing_id)
        return {"status": "skipped_existing", "internal_id": internal_id,
                "yougile_task_id": existing_id}

    # 3) Создаём задачу (стикеры резолвятся в catalog). Маркер — только после успеха.
    body = build_task_body(task, routing, catalog)
    try:
        yg_id = client.create_task(body)
    except Exception as exc:  # noqa: BLE001
        repo.upsert_task_write(meeting_id, internal_id, status="failed")
        log.error("write_task %s failed: %s", internal_id, exc)
        raise

    # 3) Чек-лист → подзадачи (если есть; в наших данных обычно пусто).
    for item in task.get("checklist") or []:
        try:
            child_id = client.create_task(
                {"title": item, "columnId": routing["yougile_column_id"]}
            )
            client._req("PUT", f"/tasks/{yg_id}", json={"subtasks": [child_id]})
        except Exception as exc:  # noqa: BLE001
            log.warning("checklist subtask '%s' failed: %s", item, exc)

    # Запомнить новый title, чтобы следующие одинаковые в этом же прогоне не задвоились.
    if column_titles is not None and norm:
        column_titles[norm] = yg_id

    repo.upsert_task_write(meeting_id, internal_id, status="written", yougile_task_id=yg_id)
    return {"status": "written", "internal_id": internal_id, "yougile_task_id": yg_id}
