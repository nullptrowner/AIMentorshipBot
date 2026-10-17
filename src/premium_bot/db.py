import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from premium_bot.domain import Json, now


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    restrictions: Mapped[Json] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Admin(Base):
    __tablename__ = "admins"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class Wallet(Base):
    __tablename__ = "user_balances"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    reserved: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    __table_args__ = (
        CheckConstraint(
            "balance >= 0 AND reserved >= 0 AND reserved <= balance", name="valid_balance"
        ),
    )


class Ledger(Base):
    __tablename__ = "wallet_transactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    reserve_delta: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=0)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Reservation(Base):
    __tablename__ = "balance_reservations"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    status: Mapped[str] = mapped_column(String(20), default="held")
    __table_args__ = (CheckConstraint("amount > 0", name="positive_reservation"),)


class AIModel(Base):
    __tablename__ = "ai_models"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))
    tier: Mapped[str] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[str | None] = mapped_column(String(100))
    api_schema: Mapped[Json] = mapped_column(JSONB, default=dict)
    defaults: Mapped[Json] = mapped_column(JSONB, default=dict)
    prompt: Mapped[str] = mapped_column(Text, default="{user_prompt}")
    prompt_revision: Mapped[int] = mapped_column(Integer, default=1)
    pricing: Mapped[Json] = mapped_column(JSONB, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelRevision(Base):
    __tablename__ = "model_revisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_key: Mapped[str] = mapped_column(ForeignKey("ai_models.key"))
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[Json] = mapped_column(JSONB)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("model_key", "revision"),)


class Job(Base):
    __tablename__ = "generation_jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_key: Mapped[str] = mapped_column(ForeignKey("ai_models.key"))
    status: Mapped[str] = mapped_column(String(30), default="created", index=True)
    snapshot: Mapped[Json] = mapped_column(JSONB)
    prompt: Mapped[str] = mapped_column(Text)
    params: Mapped[Json] = mapped_column(JSONB, default=dict)
    input_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 6))
    price: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    funding: Mapped[str | None] = mapped_column(String(20))
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subscriptions.id"))
    quota_day: Mapped[str | None] = mapped_column(String(10))
    quota_bucket: Mapped[int | None] = mapped_column(Integer)
    prediction_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    submit_started: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("price >= 0 AND estimated_cost >= 0", name="valid_job_price"),
    )


class Media(Base):
    __tablename__ = "media"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(100))
    mime: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(BigInteger)
    seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    megapixels: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    telegram_file_id: Mapped[str | None] = mapped_column(Text)
    output: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class JobEvent(Base):
    __tablename__ = "generation_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[Json] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Plan(Base):
    __tablename__ = "subscription_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    months: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    limits: Mapped[Json] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (CheckConstraint("months IN (1, 3) AND price > 0", name="valid_plan"),)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("subscription_plans.id"))
    limits: Mapped[Json] = mapped_column(JSONB)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    canceled: Mapped[bool] = mapped_column(Boolean, default=False)


class Usage(Base):
    __tablename__ = "subscription_usage"
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subscriptions.id"), primary_key=True
    )
    model_key: Mapped[str] = mapped_column(ForeignKey("ai_models.key"), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    bucket: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    held: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("held >= 0 AND used >= 0", name="valid_usage"),)


class Invoice(Base):
    __tablename__ = "payment_invoices"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    provider_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    stars: Mapped[int | None] = mapped_column(Integer)
    checkout_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="creating", index=True)
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("amount > 0", name="positive_invoice"),)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    key: Mapped[str] = mapped_column(String(250), primary_key=True)
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payment_invoices.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Setting(Base):
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Json] = mapped_column(JSONB)


class Audit(Base):
    __tablename__ = "admin_actions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200))
    details: Mapped[Json] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    """Transactional outbox/queue. Leases are recovered after worker restarts."""

    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[Json] = mapped_column(JSONB)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_tasks_pending", "done", "due_at", "lease_until"),)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[Json] = mapped_column(JSONB)
    segment: Mapped[str] = mapped_column(String(20), default="all")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    cursor: Mapped[int] = mapped_column(BigInteger, default=0)


class Delivery(Base):
    __tablename__ = "notifications"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def database(url: str) -> tuple[async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine(url, pool_size=5, max_overflow=3, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False), engine
