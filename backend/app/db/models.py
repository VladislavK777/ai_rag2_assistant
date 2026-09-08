"""Модели SQLAlchemy: RBAC, документы, сессии, аудит, GraphRAG."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def uid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("departments.id")
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    roles: Mapped[list["Role"]] = relationship(
        secondary="user_roles", lazy="selectin"
    )
    department: Mapped["Department | None"] = relationship(lazy="selectin")


class Department(Base):
    """Иерархия отделов — наследование прав вниз по дереву."""

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(255))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("departments.id")
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(50), unique=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("users.id"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("roles.id"), primary_key=True
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Permission(Base):
    """ACL: user-based или department-based право на workspace."""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("departments.id")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("workspaces.id"), index=True
    )
    level: Mapped[str] = mapped_column(String(10))  # read | write | admin


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("workspaces.id"), index=True
    )
    minio_key: Mapped[str] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("documents.id"), index=True
    )
    qdrant_point_id: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[str] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="Новый диалог")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("sessions.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list | None] = mapped_column(JSONB)
    tokens_prompt: Mapped[int] = mapped_column(Integer, default=0)
    tokens_completion: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class STTJob(Base):
    __tablename__ = "stt_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID, ForeignKey("documents.id")
    )
    audio_minio_key: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    transcript: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    action_items: Mapped[list | None] = mapped_column(JSONB)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class GraphNode(Base):
    """Knowledge Graph для GraphRAG (MVP: таблицы PostgreSQL, Фаза 3 — Neo4j)."""

    __tablename__ = "graph_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID, ForeignKey("workspaces.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    node_type: Mapped[str] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(Text)


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID, primary_key=True, default=uid)
    src_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("graph_nodes.id"))
    dst_id: Mapped[uuid.UUID] = mapped_column(UUID, ForeignKey("graph_nodes.id"))
    relation: Mapped[str] = mapped_column(String(100))
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class AuditLog(Base):
    """Append-only аудит: UPDATE/DELETE запрещены триггером (см. миграции)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(50), index=True)
    resource: Mapped[str | None] = mapped_column(String(255))
    decision: Mapped[str] = mapped_column(String(10))  # allow | deny
    trace_id: Mapped[str | None] = mapped_column(String(64))
