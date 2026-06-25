"""
Слой БД (E2): SQLAlchemy-модели справочников и состояния созвонов.

Движок выбирается по DATABASE_URL (engine-agnostic):
- прод/стейдж: PostgreSQL (см. docker-compose.yml), напр.
  postgresql+psycopg2://sozvon:sozvon@localhost:5432/sozvon
- локальная проверка без Docker: SQLite-файл (DATABASE_URL не задан → sqlite:///./e2_local.db).

Те же модели и миграции (Alembic) работают на обоих движках. На этой машине Docker не
установлен, поэтому функциональная проверка идёт на SQLite; прод — Postgres через compose.

Таблицы:
- projects: маршрут проекта → YouGile project/board/column.
- people:   справочник «уникальное имя → YouGile user ID → роль по умолчанию».
- meetings: состояние созвона (state-machine) + результат фазы 1 (result_json).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()

DEFAULT_SQLITE_URL = "sqlite:///./e2_local.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)


# Статусы созвона (state-machine фазы 1; done — после записи в YouGile на E3).
MEETING_STATES = ("uploaded", "transcribing", "processing", "awaiting_review", "done", "error")


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    yougile_project_id: Mapped[Optional[str]] = mapped_column(String(100))
    yougile_board_id: Mapped[Optional[str]] = mapped_column(String(100))
    yougile_column_id: Mapped[Optional[str]] = mapped_column(String(100))


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (UniqueConstraint("name", name="uq_people_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Уникальное имя — основа детерминированного lookup (см. people_lookup.py).
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    yougile_user_id: Mapped[Optional[str]] = mapped_column(String(100))
    default_role: Mapped[Optional[str]] = mapped_column(String(50))  # assignee|controller|...
    specialty: Mapped[Optional[str]] = mapped_column(String(120))    # «СММ», «менеджер соцсетей»
    project: Mapped[Optional[str]] = mapped_column(String(200))      # привязка к проекту (имя)


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    status: Mapped[str] = mapped_column(String(30), default="uploaded", nullable=False)
    source_file: Mapped[Optional[str]] = mapped_column(String(500))
    # Результат фазы 1 (список задач + мета) — JSON. На Postgres можно мигрировать в JSONB.
    result_json: Mapped[Optional[Any]] = mapped_column(JSON)


class TaskWrite(Base):
    """Маркер идемпотентности записи в YouGile (E3).

    internal_task_id уникален → повторная фаза 2 находит маркер 'written' и НЕ создаёт
    дубль. yougile_task_id и статус 'written' ставятся ТОЛЬКО после успешного ответа API.
    """

    __tablename__ = "task_writes"
    __table_args__ = (UniqueConstraint("internal_task_id", name="uq_task_writes_internal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), nullable=False)
    internal_task_id: Mapped[str] = mapped_column(String(120), nullable=False)
    yougile_task_id: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending|written|failed


def make_engine(url: str | None = None, echo: bool = False):
    url = url or database_url()
    # check_same_thread только для SQLite; Postgres игнорирует connect_args.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


# Глобальная фабрика сессий (ленивая).
_engine = None
_Session = None


def get_sessionmaker():
    global _engine, _Session
    if _Session is None:
        _engine = make_engine()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _Session


def session_scope():
    """Контекст-менеджер сессии с commit/rollback."""
    return _SessionScope()


class _SessionScope:
    def __enter__(self):
        self.session = get_sessionmaker()()
        return self.session

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()


def create_all(url: str | None = None) -> None:
    """Создать таблицы напрямую (для тестов/быстрого старта; прод — через Alembic)."""
    Base.metadata.create_all(make_engine(url))
