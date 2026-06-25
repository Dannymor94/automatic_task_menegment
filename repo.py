"""
Репозиторий БД (E2): доступ к projects и meetings для оркестратора.
Возвращает простые dict/id (не ORM-объекты), чтобы не ловить detached-instance.
"""

from __future__ import annotations

from db import Meeting, Person, Project, TaskWrite, session_scope


# --- Маршрут проекта ---------------------------------------------------------
def get_project_routing(name: str | None) -> dict | None:
    """Имя проекта → YouGile project/board/column из таблицы projects."""
    if not name:
        return None
    with session_scope() as s:
        p = s.query(Project).filter(Project.name == name).one_or_none()
        if p is None:
            return None
        return {
            "name": p.name,
            "yougile_project_id": p.yougile_project_id,
            "yougile_board_id": p.yougile_board_id,
            "yougile_column_id": p.yougile_column_id,
        }


def upsert_project_route(
    name: str, yougile_project_id: str | None,
    yougile_board_id: str | None, yougile_column_id: str | None,
) -> None:
    """Завести/обновить маршрут проекта (имя → YouGile board/column).

    Нужен, чтобы выбранный в форме РЕАЛЬНЫЙ проект YouGile имел маршрут для записи.
    """
    with session_scope() as s:
        p = s.query(Project).filter(Project.name == name).one_or_none()
        if p is None:
            s.add(Project(name=name, yougile_project_id=yougile_project_id,
                          yougile_board_id=yougile_board_id, yougile_column_id=yougile_column_id))
        else:
            p.yougile_project_id = yougile_project_id
            p.yougile_board_id = yougile_board_id
            p.yougile_column_id = yougile_column_id


def list_projects() -> list[dict]:
    """Справочник проектов для выпадающего списка формы загрузки."""
    with session_scope() as s:
        return [{"id": p.id, "name": p.name} for p in s.query(Project).order_by(Project.name).all()]


# --- База команды (people) ---------------------------------------------------
def list_people(project: str | None = None) -> list[dict]:
    """Список сотрудников (опц. по проекту) для UI и контекста промпта."""
    with session_scope() as s:
        q = s.query(Person).order_by(Person.name)
        if project:
            q = q.filter(Person.project == project)
        return [
            {"id": p.id, "name": p.name, "specialty": p.specialty,
             "project": p.project, "yougile_user_id": p.yougile_user_id,
             "matched": bool(p.yougile_user_id)}
            for p in q.all()
        ]


def upsert_person(
    name: str, specialty: str | None, project: str | None, yougile_user_id: str | None
) -> dict:
    """Завести/обновить сотрудника. Возвращает строку."""
    with session_scope() as s:
        p = s.query(Person).filter(Person.name == name).one_or_none()
        if p is None:
            p = Person(name=name, specialty=specialty, project=project,
                       yougile_user_id=yougile_user_id, default_role="assignee")
            s.add(p)
        else:
            p.specialty = specialty
            p.project = project
            if yougile_user_id is not None:
                p.yougile_user_id = yougile_user_id
        s.flush()
        return {"id": p.id, "name": p.name, "specialty": p.specialty,
                "project": p.project, "yougile_user_id": p.yougile_user_id,
                "matched": bool(p.yougile_user_id)}


def update_person(
    person_id: int, name: str, specialty: str | None, project: str | None,
    yougile_user_id: str | None,
) -> dict | None:
    """Обновить сотрудника по id. yougile_user_id пересчитывается в API при смене имени."""
    with session_scope() as s:
        p = s.get(Person, person_id)
        if p is None:
            return None
        p.name = name
        p.specialty = specialty
        p.project = project
        p.yougile_user_id = yougile_user_id
        s.flush()
        return {"id": p.id, "name": p.name, "specialty": p.specialty,
                "project": p.project, "yougile_user_id": p.yougile_user_id,
                "matched": bool(p.yougile_user_id)}


def delete_person(person_id: int) -> bool:
    with session_scope() as s:
        p = s.get(Person, person_id)
        if p is None:
            return False
        s.delete(p)
        return True


# --- Состояние созвонов (state-machine) -------------------------------------
def create_meeting(source_file: str | None, status: str = "uploaded") -> int:
    with session_scope() as s:
        m = Meeting(source_file=source_file, status=status)
        s.add(m)
        s.flush()
        return m.id


def set_status(meeting_id: int, status: str) -> None:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if m is not None:
            m.status = status


def save_result(meeting_id: int, result_json, status: str = "awaiting_review") -> None:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if m is not None:
            m.result_json = result_json
            m.status = status


def get_meeting(meeting_id: int) -> dict | None:
    with session_scope() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            return None
        return {
            "id": m.id,
            "created_at": str(m.created_at),
            "status": m.status,
            "source_file": m.source_file,
            "result_json": m.result_json,
        }


# --- Маркеры идемпотентности записи в YouGile (E3) ---------------------------
def get_task_write_status(internal_task_id: str) -> dict | None:
    """Текущий маркер по internal_task_id (или None, если записи ещё не было)."""
    with session_scope() as s:
        tw = (
            s.query(TaskWrite)
            .filter(TaskWrite.internal_task_id == internal_task_id)
            .one_or_none()
        )
        if tw is None:
            return None
        return {"status": tw.status, "yougile_task_id": tw.yougile_task_id}


def upsert_task_write(
    meeting_id: int, internal_task_id: str, status: str, yougile_task_id: str | None = None
) -> None:
    """Завести/обновить маркер. 'written' ставится ТОЛЬКО после успешного ответа API."""
    with session_scope() as s:
        tw = (
            s.query(TaskWrite)
            .filter(TaskWrite.internal_task_id == internal_task_id)
            .one_or_none()
        )
        if tw is None:
            s.add(
                TaskWrite(
                    meeting_id=meeting_id,
                    internal_task_id=internal_task_id,
                    status=status,
                    yougile_task_id=yougile_task_id,
                )
            )
        else:
            tw.status = status
            if yougile_task_id is not None:
                tw.yougile_task_id = yougile_task_id
