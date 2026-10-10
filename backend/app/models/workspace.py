"""本机阅读状态、外部处理选择及跨步骤任务。与小说一起级联清理。"""

from __future__ import annotations
from datetime import datetime
from typing import Any
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, Index, text

from backend.app.core.database import Base
from backend.app.core.time import utc_now


class WorkspaceSetting(Base):
    """应用级非秘密设置；模型密钥继续由现有配置模块管理。"""

    __tablename__ = "workspace_settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class ReadingState(Base):
    """按小说持久化阅读位置、偏好和书签，独立于浏览器来源。"""

    __tablename__ = "reading_states"
    novel_id: Mapped[int] = mapped_column(Integer, ForeignKey("novels.id", ondelete="CASCADE"), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (Index("uq_active_workflow", "novel_id", unique=True,
                           sqlite_where=text("status IN ('queued', 'running')")),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    novel_id: Mapped[int] = mapped_column(Integer, ForeignKey("novels.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    phase: Mapped[str] = mapped_column(String(80), nullable=False, default="等待处理")
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
