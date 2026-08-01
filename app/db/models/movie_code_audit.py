from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MovieCodeAuditAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DEACTIVATE = "deactivate"
    DELETE = "delete"
    RESTORE = "restore"


class MovieCodeAuditSource(str, Enum):
    MANUAL = "manual"
    BULK_IMPORT = "bulk_import"


class MovieCodeAudit(Base):
    __tablename__ = "movie_code_audit"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String)
    old_title: Mapped[str | None] = mapped_column(String)
    new_title: Mapped[str | None] = mapped_column(String)
    action: Mapped[MovieCodeAuditAction] = mapped_column(
        SqlEnum(MovieCodeAuditAction, native_enum=False, validate_strings=True)
    )
    source: Mapped[MovieCodeAuditSource] = mapped_column(
        SqlEnum(MovieCodeAuditSource, native_enum=False, validate_strings=True)
    )
    changed_by_admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
