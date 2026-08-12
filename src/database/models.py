from sqlalchemy import Integer, Boolean, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
