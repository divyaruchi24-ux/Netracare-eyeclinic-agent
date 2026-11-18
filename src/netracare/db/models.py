"""SQLAlchemy ORM models."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netracare.db.base import Base, IdMixin, TimestampMixin, UTCDateTime, utcnow
from netracare.domain.enums import (
    ActorType,
    AppointmentStatus,
    AppointmentType,
    ApprovalKind,
    ApprovalStatus,
    DocType,
    NotificationChannel,
    NotificationStatus,
    RedFlagLevel,
    Role,
    SlotStatus,
)


def _enum(cls: type[Any], name: str) -> Enum:
    """Store enums by value as VARCHAR + CHECK (portable across SQLite/Postgres)."""
    return Enum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
        length=32,
    )


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(_enum(Role, "role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Specialty(IdMixin, Base):
    __tablename__ = "specialties"

    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Doctor(IdMixin, TimestampMixin, Base):
    __tablename__ = "doctors"

    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    specialty_id: Mapped[str] = mapped_column(ForeignKey("specialties.id"), nullable=False)
    bio_slug: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    specialty: Mapped[Specialty] = relationship()


class StaffUser(IdMixin, Base):
    __tablename__ = "staff_users"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    doctor_id: Mapped[str | None] = mapped_column(ForeignKey("doctors.id"), unique=True)

    user: Mapped[User] = relationship()


class Patient(IdMixin, TimestampMixin, Base):
    __tablename__ = "patients"

    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dob: Mapped[date] = mapped_column(Date, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(255))
    no_show_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consent_captured_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (CheckConstraint("no_show_count >= 0", name="no_show_nonneg"),)


class PatientGuardian(Base):
    __tablename__ = "patient_guardians"

    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), primary_key=True)
    guardian_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    relationship_label: Mapped[str] = mapped_column("relationship", String(32), nullable=False)
    consent_captured_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class ScheduleTemplate(IdMixin, Base):
    __tablename__ = "schedule_templates"

    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_local: Mapped[time] = mapped_column(Time, nullable=False)
    end_local: Mapped[time] = mapped_column(Time, nullable=False)
    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("start_local < end_local", name="start_before_end"),
        UniqueConstraint("doctor_id", "weekday", "start_local", "valid_from"),
    )


class Leave(IdMixin, Base):
    __tablename__ = "leaves"

    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason: Mapped[str] = mapped_column(String(128), default="leave", nullable=False)

    __table_args__ = (
        CheckConstraint("start_utc < end_utc", name="start_before_end"),
        UniqueConstraint("doctor_id", "start_utc"),
    )


class Holiday(Base):
    __tablename__ = "holidays"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Slot(IdMixin, Base):
    __tablename__ = "slots"

    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    status: Mapped[SlotStatus] = mapped_column(
        _enum(SlotStatus, "slot_status"), default=SlotStatus.OPEN, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("doctor_id", "start_utc"),
        CheckConstraint("start_utc < end_utc", name="start_before_end"),
    )


class Appointment(IdMixin, TimestampMixin, Base):
    __tablename__ = "appointments"

    patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False)
    doctor_id: Mapped[str] = mapped_column(ForeignKey("doctors.id"), nullable=False)
    type: Mapped[AppointmentType] = mapped_column(
        _enum(AppointmentType, "appointment_type"), nullable=False
    )
    status: Mapped[AppointmentStatus] = mapped_column(
        _enum(AppointmentStatus, "appointment_status"), nullable=False
    )
    start_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    end_utc: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(Text)
    booked_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    rescheduled_from_id: Mapped[str | None] = mapped_column(ForeignKey("appointments.id"))
    approval_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    red_flag_level: Mapped[RedFlagLevel | None] = mapped_column(
        _enum(RedFlagLevel, "red_flag_level")
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    holds: Mapped[list[AppointmentSlot]] = relationship(
        back_populates="appointment", cascade="all, delete-orphan"
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__ = (
        Index("ix_appointments_patient_start", "patient_id", "start_utc"),
        Index("ix_appointments_doctor_start", "doctor_id", "start_utc"),
    )


class AppointmentSlot(Base):
    """Links an appointment to the slot(s) it occupies.

    The partial unique index guarantees at most one *active* hold per slot - the DB-level
    guard against double-booking races.
    """

    __tablename__ = "appointment_slots"

    appointment_id: Mapped[str] = mapped_column(ForeignKey("appointments.id"), primary_key=True)
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    appointment: Mapped[Appointment] = relationship(back_populates="holds")

    __table_args__ = (
        Index(
            "uq_active_slot_hold",
            "slot_id",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active"),
        ),
    )


class AuditLog(IdMixin, Base):
    """Append-only. UPDATE/DELETE are blocked by DB triggers (see migration 0001)."""

    __tablename__ = "audit_logs"

    at_utc: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(36))
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType, "actor_type"), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(36))
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class KnowledgeDoc(IdMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_docs"

    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[DocType] = mapped_column(_enum(DocType, "doc_type"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    superseded_by_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_docs.id"))
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("slug", "version"),)


class NotificationOutbox(IdMixin, Base):
    __tablename__ = "notification_outbox"

    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel"), nullable=False
    )
    to_patient_id: Mapped[str] = mapped_column(ForeignKey("patients.id"), nullable=False)
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.QUEUED,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class RefreshToken(IdMixin, Base):
    """Opaque refresh token (only the sha256 hash is stored). Rotated on every use."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    replaced_by_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))


class ApprovalRequest(IdMixin, TimestampMixin, Base):
    """Human-in-the-loop queue: bookings/changes that need front-desk approval.

    `appointment_id` is the appointment the request is about (the new booking, or the
    existing appointment being rescheduled/cancelled). For reschedules, the proposed new
    appointment is `target_appointment_id`. At most one pending request per appointment.
    """

    __tablename__ = "approval_requests"

    kind: Mapped[ApprovalKind] = mapped_column(_enum(ApprovalKind, "approval_kind"), nullable=False)
    appointment_id: Mapped[str] = mapped_column(ForeignKey("appointments.id"), nullable=False)
    target_appointment_id: Mapped[str | None] = mapped_column(ForeignKey("appointments.id"))
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"), default=ApprovalStatus.PENDING, nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    decided_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decision_note: Mapped[str | None] = mapped_column(Text)
    request_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__ = (
        Index("ix_approval_requests_status_created", "status", "created_at"),
        Index(
            "uq_one_pending_per_appointment",
            "appointment_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )


class IdempotencyRecord(IdMixin, Base):
    """Stored response for (user, Idempotency-Key) so retries replay instead of re-executing."""

    __tablename__ = "idempotency_records"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "key"),)
