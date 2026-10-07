"""本机阅读状态、外部处理选择及跨步骤任务。与小说一起级联清理。"""

from sqlalchemy import Column, ForeignKey, Integer, JSON, String, Text, Index, text

from backend.app.core.database import Base


class WorkspaceSetting(Base):
    """应用级非秘密设置；模型密钥继续由现有配置模块管理。"""

    __tablename__ = "workspace_settings"
    key = Column(String(80), primary_key=True)
    value = Column(JSON, nullable=False)


class ReadingState(Base):
    """按小说持久化阅读位置、偏好和书签，独立于浏览器来源。"""

    __tablename__ = "reading_states"
    novel_id = Column(Integer, ForeignKey("novels.id", ondelete="CASCADE"), primary_key=True)
    value = Column(JSON, nullable=False)


class Workflow(Base):
    """持久记录摘要到报告、索引到问答的串联步骤及错误。"""

    __tablename__ = "workflows"
    __table_args__ = (Index("uq_active_workflow", "novel_id", unique=True,
                           sqlite_where=text("status IN ('queued', 'running')")),)
    id = Column(String(36), primary_key=True)
    novel_id = Column(Integer, ForeignKey("novels.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="queued")
    phase = Column(String(80), nullable=False, default="等待处理")
    question = Column(Text, nullable=False, default="")
    error_message = Column(Text)
    created_at = Column(String(40), nullable=False)
