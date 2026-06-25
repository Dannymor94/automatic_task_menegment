"""
sync_yougile.py (E2) — наполнение справочников людьми и проектами из YouGile API.

Реализует yougile-integration: тянет реальных пользователей/проекты/доски/колонки,
показывает их с YouGile ID, помогает завести записи в people/projects.

Принцип (по твоему условию): ИМЕНА для lookup задаёшь ты вручную (уникальные),
YouGile ID подтягиваются из API. Поэтому есть и ручной ввод ID, и удобное
сопоставление по realName.

Команды:
  list-users                         показать пользователей YouGile (id + realName + email)
  list-projects                      показать проекты → доски → колонки (с id)
  add-person "<имя>" <user_id> [role]        завести человека (имя для lookup → YouGile id)
  add-person-by-realname "<имя>" "<realName>"  найти id по realName и завести
  add-project "<имя>" <proj_id> <board_id> <col_id>   завести маршрут проекта
  show                               показать текущие справочники в БД

Ключ — в YOUGILE_API_KEY (заголовок Bearer). Базовый URL — YOUGILE_BASE_URL
(дефолт https://ru.yougile.com/api-v2, см. skill).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv

from db import Person, Project, session_scope

load_dotenv()

BASE_URL = os.environ.get("YOUGILE_BASE_URL", "https://ru.yougile.com/api-v2")
API_KEY = os.environ.get("YOUGILE_API_KEY")
RETRYABLE = {500, 502, 503, 504}


# --- HTTP клиент -------------------------------------------------------------
def _headers() -> dict:
    if not API_KEY:
        raise RuntimeError("YOUGILE_API_KEY не задан в .env")
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def _get(path: str, params: dict | None = None, retries: int = 2) -> dict:
    url = f"{BASE_URL}{path}"
    for attempt in range(retries + 1):
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
        if resp.status_code in RETRYABLE and attempt < retries:
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


def _paged(path: str, params: dict | None = None) -> list[dict]:
    """Собрать все страницы (YouGile пагинирует через limit/offset)."""
    params = dict(params or {})
    params.setdefault("limit", 1000)
    items: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        data = _get(path, params)
        content = data.get("content", data if isinstance(data, list) else [])
        items.extend(content)
        paging = data.get("paging") or {}
        if not paging.get("next") and len(content) < params["limit"]:
            break
        if not content:
            break
        offset += len(content)
    return items


# --- API-обёртки -------------------------------------------------------------
def get_users() -> list[dict]:
    return _paged("/users")


def get_projects() -> list[dict]:
    return _paged("/projects")


def get_boards(project_id: str) -> list[dict]:
    return _paged("/boards", {"projectId": project_id})


def get_columns(board_id: str) -> list[dict]:
    return _paged("/columns", {"boardId": board_id})


# --- Команды -----------------------------------------------------------------
def cmd_list_users(_args) -> None:
    users = get_users()
    print(f"Пользователи YouGile: {len(users)}")
    for u in users:
        name = u.get("realName") or u.get("name") or "—"
        print(f"  id={u.get('id')}  {name!r}  <{u.get('email','')}>")


def cmd_list_projects(_args) -> None:
    projects = get_projects()
    print(f"Проекты YouGile: {len(projects)}")
    for p in projects:
        print(f"  project id={p.get('id')}  {p.get('title')!r}")
        for b in get_boards(p["id"]):
            print(f"    board id={b.get('id')}  {b.get('title')!r}")
            for c in get_columns(b["id"]):
                print(f"      column id={c.get('id')}  {c.get('title')!r}")


def _upsert_person(name: str, user_id: str, role: str | None) -> None:
    with session_scope() as s:
        p = s.query(Person).filter(Person.name == name).one_or_none()
        if p is None:
            s.add(Person(name=name, yougile_user_id=user_id, default_role=role))
            print(f"+ добавлен: {name!r} → {user_id} ({role})")
        else:
            p.yougile_user_id = user_id
            if role:
                p.default_role = role
            print(f"~ обновлён: {name!r} → {user_id} ({p.default_role})")


def cmd_add_person(args) -> None:
    _upsert_person(args.name, args.user_id, args.role)


def cmd_add_person_by_realname(args) -> None:
    users = get_users()
    matches = [u for u in users if (u.get("realName") or u.get("name")) == args.realname]
    if not matches:
        print(f"realName {args.realname!r} не найден в YouGile"); return
    if len(matches) > 1:
        print(f"Неоднозначно: {len(matches)} пользователей с realName {args.realname!r} — "
              f"заведи по id вручную: {[m['id'] for m in matches]}")
        return
    _upsert_person(args.name, matches[0]["id"], args.role)


def cmd_add_project(args) -> None:
    with session_scope() as s:
        p = s.query(Project).filter(Project.name == args.name).one_or_none()
        if p is None:
            s.add(Project(name=args.name, yougile_project_id=args.project_id,
                          yougile_board_id=args.board_id, yougile_column_id=args.column_id))
            print(f"+ проект {args.name!r} → board={args.board_id} column={args.column_id}")
        else:
            p.yougile_project_id = args.project_id
            p.yougile_board_id = args.board_id
            p.yougile_column_id = args.column_id
            print(f"~ проект {args.name!r} обновлён")


def cmd_show(_args) -> None:
    with session_scope() as s:
        people = s.query(Person).all()
        projects = s.query(Project).all()
        print(f"people ({len(people)}):")
        for p in people:
            print(f"  {p.name!r} → user_id={p.yougile_user_id} role={p.default_role}")
        print(f"projects ({len(projects)}):")
        for p in projects:
            print(f"  {p.name!r} → board={p.yougile_board_id} column={p.yougile_column_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Наполнение справочников из YouGile")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-users").set_defaults(func=cmd_list_users)
    sub.add_parser("list-projects").set_defaults(func=cmd_list_projects)

    ap = sub.add_parser("add-person")
    ap.add_argument("name"); ap.add_argument("user_id"); ap.add_argument("role", nargs="?")
    ap.set_defaults(func=cmd_add_person)

    arn = sub.add_parser("add-person-by-realname")
    arn.add_argument("name"); arn.add_argument("realname"); arn.add_argument("role", nargs="?")
    arn.set_defaults(func=cmd_add_person_by_realname)

    apr = sub.add_parser("add-project")
    apr.add_argument("name"); apr.add_argument("project_id")
    apr.add_argument("board_id"); apr.add_argument("column_id")
    apr.set_defaults(func=cmd_add_project)

    sub.add_parser("show").set_defaults(func=cmd_show)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        args.func(args)
    except requests.HTTPError as e:
        print(f"Ошибка YouGile API: {e}", file=sys.stderr)
        sys.exit(1)
