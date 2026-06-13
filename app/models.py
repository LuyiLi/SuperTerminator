from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Server(TimestampMixin, Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alias: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    project_links: Mapped[list[ProjectServer]] = relationship(back_populates="server")


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    git_url: Mapped[str] = mapped_column(String(1024), default="")
    default_workdir: Mapped[str] = mapped_column(String(1024), default="")

    server_links: Mapped[list[ProjectServer]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    templates: Mapped[list[Template]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    runs: Mapped[list[Run]] = relationship(back_populates="project")


class ProjectServer(Base):
    __tablename__ = "project_servers"
    __table_args__ = (UniqueConstraint("project_id", "server_id", name="uq_project_server"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    project: Mapped[Project] = relationship(back_populates="server_links")
    server: Mapped[Server] = relationship(back_populates="project_links")
    workdirs: Mapped[list[ProjectWorkdir]] = relationship(
        back_populates="project_server", cascade="all, delete-orphan"
    )


class ProjectWorkdir(Base):
    __tablename__ = "project_workdirs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_server_id: Mapped[int] = mapped_column(ForeignKey("project_servers.id"))
    path: Mapped[str] = mapped_column(String(1024))
    label: Mapped[str] = mapped_column(String(255), default="main")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    project_server: Mapped[ProjectServer] = relationship(back_populates="workdirs")


class Template(TimestampMixin, Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(255))
    command_template: Mapped[str] = mapped_column(Text)
    variables_schema: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    project: Mapped[Project] = relationship(back_populates="templates")
    presets: Mapped[list[Preset]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class Preset(Base):
    __tablename__ = "presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    name: Mapped[str] = mapped_column(String(255))
    values_json: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    template: Mapped[Template] = relationship(back_populates="presets")


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"))
    workdir: Mapped[str] = mapped_column(String(1024))
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    preset_id: Mapped[int | None] = mapped_column(ForeignKey("presets.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    tmux_session: Mapped[str] = mapped_column(String(255), default="")
    rendered_command: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="created")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped[Project] = relationship(back_populates="runs")
    server: Mapped[Server] = relationship()
    template: Mapped[Template] = relationship()
    preset: Mapped[Preset | None] = relationship()
