from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _str_enum(enum_cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=16,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class EventType(str, enum.Enum):
    POST = "post"
    REPLY = "reply"
    REPOST = "repost"
    QUOTE = "quote"


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    ERROR = "error"  # transient failures, still retried
    UNAVAILABLE = "unavailable"  # not found / suspended / protected


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class User(Base):
    """Telegram user. Notification settings live here (one row per user)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notify_post: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notify_reply: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notify_repost: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notify_quote: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    quiet_start_hour: Mapped[int] = mapped_column(SmallInteger, default=23, server_default="23")
    quiet_end_hour: Mapped[int] = mapped_column(SmallInteger, default=8, server_default="8")
    grouping_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    error_notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def wants(self, event_type: EventType) -> bool:
        return {
            EventType.POST: self.notify_post,
            EventType.REPLY: self.notify_reply,
            EventType.REPOST: self.notify_repost,
            EventType.QUOTE: self.notify_quote,
        }[event_type]


class XAccount(Base):
    """A monitored X profile. Shared between users; never deleted so history stays intact."""

    __tablename__ = "x_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    protected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Highest post ID already processed; polling uses it as since_id.
    last_seen_post_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[AccountStatus] = mapped_column(
        _str_enum(AccountStatus, "account_status"), default=AccountStatus.ACTIVE, server_default="active"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_alert_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="account")

    @property
    def profile_url(self) -> str:
        return f"https://x.com/{self.username}"


class Subscription(Base):
    """User <-> monitored account link. Deleting it stops monitoring but keeps events/notifications."""

    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "x_account_id", name="uq_subscriptions_user_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    x_account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="CASCADE"), index=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="subscriptions")
    account: Mapped[XAccount] = relationship(back_populates="subscriptions")


class Event(Base):
    """A detected activity. post_id is unique: the core duplicate guard."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_account_posted", "x_account_id", "posted_at"),
        Index("ix_events_posted_at", "posted_at"),
        Index("ix_events_detected_at", "detected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    x_account_id: Mapped[int] = mapped_column(ForeignKey("x_accounts.id", ondelete="RESTRICT"))
    event_type: Mapped[EventType] = mapped_column(_str_enum(EventType, "event_type"), index=True)
    author_username: Mapped[str] = mapped_column(String(32))
    author_name: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(256))
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    in_reply_to_user_id: Mapped[int | None] = mapped_column(BigInteger)
    in_reply_to_username: Mapped[str | None] = mapped_column(String(32))
    referenced_post_id: Mapped[int | None] = mapped_column(BigInteger)
    referenced_author_username: Mapped[str | None] = mapped_column(String(32))
    referenced_author_name: Mapped[str | None] = mapped_column(String(128))
    referenced_text: Mapped[str | None] = mapped_column(Text)
    referenced_url: Mapped[str | None] = mapped_column(String(256))
    # For quote-replies: the quoted post when the main type is REPLY.
    quoted_post_id: Mapped[int | None] = mapped_column(BigInteger)
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")

    account: Mapped[XAccount] = relationship()


class Notification(Base):
    """Outbox row: one per (event, user). Unique pair guarantees an event is delivered at most once."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_notifications_event_user"),
        Index("ix_notifications_status_id", "status", "id"),
        Index("ix_notifications_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[NotificationStatus] = mapped_column(
        _str_enum(NotificationStatus, "notification_status"), default=NotificationStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(256))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship()
    user: Mapped[User] = relationship()


class KeyValue(Base):
    """Small persistent key/value store for service state (alert throttling etc.)."""

    __tablename__ = "kv_store"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
