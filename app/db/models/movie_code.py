from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MovieCodeStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"


class MovieCode(Base):
    __tablename__ = "movie_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    status: Mapped[MovieCodeStatus] = mapped_column(
        SqlEnum(MovieCodeStatus, native_enum=False, validate_strings=True)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
