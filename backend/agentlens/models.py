from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CandidateRecord(Base):
    __tablename__ = "candidates"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExperimentRecord(Base):
    __tablename__ = "experiments"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    baseline_id: Mapped[str] = mapped_column(ForeignKey("candidates.id"))
    snapshot: Mapped[dict] = mapped_column(JSON)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    runs: Mapped[list[RunRecord]] = relationship(cascade="all, delete-orphan", back_populates="experiment")


class RunRecord(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiments.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    candidate_id: Mapped[str] = mapped_column(String(80), index=True)
    seed: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    success: Mapped[int] = mapped_column(Integer)
    trajectory_score: Mapped[float] = mapped_column(Float)
    cost_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict] = mapped_column(JSON)
    experiment: Mapped[ExperimentRecord] = relationship(back_populates="runs")
    events: Mapped[list[EventRecord]] = relationship(cascade="all, delete-orphan", back_populates="run")


class EventRecord(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    run: Mapped[RunRecord] = relationship(back_populates="events")
