from sqlalchemy import Integer, Boolean, Text, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from datetime import datetime

class Base(DeclarativeBase):
    pass

class SystemSettings(Base):
    __tablename__ = "system_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    openvk_instance_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    openvk_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    openvk_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    poll_interval: Mapped[int] = mapped_column(Integer, default=10)


class BlacklistedUser(Base):
    __tablename__ = "blacklisted_users"

    vk_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class AutoBlockedUser(Base):
    __tablename__ = "auto_blocked_users"

    vk_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class UserActivity(Base):
    __tablename__ = "user_activities"

    vk_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    first_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    image_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SystemStats(Base):
    __tablename__ = "system_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_text_requests: Mapped[int] = mapped_column(Integer, default=0)
    total_image_requests: Mapped[int] = mapped_column(Integer, default=0)
    flux_success_count: Mapped[int] = mapped_column(Integer, default=0)
    fallback_success_count: Mapped[int] = mapped_column(Integer, default=0)
    total_likes_count: Mapped[int] = mapped_column(Integer, default=0)
