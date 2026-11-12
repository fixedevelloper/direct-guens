"""Stockage MySQL (SQLAlchemy async + aiomysql)."""
import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Float, Integer, String, UniqueConstraint, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "mysql+aiomysql://camair:camair@127.0.0.1:3306/camair?charset=utf8mb4")
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
Session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Flight(Base):
    __tablename__ = "flights"
    __table_args__ = (UniqueConstraint("origin", "destination", "flight_date", "flight_number", "departure_time", name="uq_flight"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin: Mapped[str] = mapped_column(String(3), index=True)
    destination: Mapped[str] = mapped_column(String(3), index=True)
    origin_city: Mapped[str] = mapped_column(String(100))
    destination_city: Mapped[str] = mapped_column(String(100))
    flight_number: Mapped[str] = mapped_column(String(40))
    flight_date: Mapped[date] = mapped_column(Date, index=True)
    departure_time: Mapped[str] = mapped_column(String(5))
    arrival_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    arrival_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    duration: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    stops: Mapped[int] = mapped_column(Integer, default=0)
    price_xaf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    availability: Mapped[str] = mapped_column(String(30), default="available")
    scraped_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    def to_dict(self) -> dict:
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns if c.name != "id"}
        for k in ("flight_date", "arrival_date", "scraped_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


COLS = [c.name for c in Flight.__table__.columns if c.name != "id"]


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _row(f: dict) -> dict:
    r = {k: f.get(k) for k in COLS}
    r["flight_number"] = r["flight_number"] or "?"
    r["flight_date"] = date.fromisoformat(r["flight_date"])
    r["arrival_date"] = date.fromisoformat(r["arrival_date"]) if r["arrival_date"] else None
    r["scraped_at"] = datetime.fromisoformat(r["scraped_at"]).replace(tzinfo=None)
    return r


async def save_flights(rows: list[dict], window_start: date, window_end: date, run_started: datetime) -> None:
    """Upsert des vols du run, puis supprime les vols de la fenêtre non revus (disparus du site)."""
    async with Session() as s, s.begin():
        for i in range(0, len(rows), 500):
            stmt = mysql_insert(Flight).values([_row(f) for f in rows[i:i + 500]])
            upd = {c: stmt.inserted[c] for c in COLS if c not in ("origin", "destination", "flight_date", "flight_number", "departure_time")}
            await s.execute(stmt.on_duplicate_key_update(**upd))
        await s.execute(delete(Flight).where(Flight.flight_date.between(window_start, window_end),
                                             Flight.scraped_at < run_started.replace(tzinfo=None)))


async def count_flights() -> int:
    async with Session() as s:
        return (await s.execute(select(func.count()).select_from(Flight))).scalar_one()


async def query_flights(origin: Optional[str], destination: Optional[str], day: Optional[str], limit: int, offset: int):
    q = select(Flight)
    if origin:
        q = q.where(Flight.origin == origin.upper())
    if destination:
        q = q.where(Flight.destination == destination.upper())
    if day:
        q = q.where(Flight.flight_date == date.fromisoformat(day))
    async with Session() as s:
        total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        rows = (await s.execute(q.order_by(Flight.flight_date, Flight.origin, Flight.destination, Flight.departure_time)
                                .limit(limit).offset(offset))).scalars().all()
    return total, [r.to_dict() for r in rows]
