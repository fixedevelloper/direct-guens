"""Stockage MySQL (SQLAlchemy async + aiomysql)."""
import os
from datetime import date, datetime, timedelta
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


class Coverage(Base):
    """Mémoire du scraping : état connu de chaque (trajet, jour)."""
    __tablename__ = "scrape_coverage"
    origin: Mapped[str] = mapped_column(String(3), primary_key=True)
    destination: Mapped[str] = mapped_column(String(3), primary_key=True)
    flight_date: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(15))            # has_flights | empty | no_flights
    scraped_at: Mapped[datetime] = mapped_column(DateTime)


class RouteStatus(Base):
    __tablename__ = "route_status"
    origin: Mapped[str] = mapped_column(String(3), primary_key=True)
    destination: Mapped[str] = mapped_column(String(3), primary_key=True)
    status: Mapped[str] = mapped_column(String(10))            # ok | no_route
    checked_at: Mapped[datetime] = mapped_column(DateTime)


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


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


async def save_day(origin: str, dest: str, day: date, rows: list[dict], run_started: datetime) -> None:
    """Enregistre le résultat confirmé d'un (trajet, jour).
    Si des vols ont été lus : upsert + suppression des vols de ce jour qui ne sont plus affichés.
    Si rien n'a été lu (parse vide) : on conserve les anciennes lignes (pas de suppression sur un doute)."""
    async with Session() as s, s.begin():
        if rows:
            stmt = mysql_insert(Flight).values([_row(f) for f in rows])
            keys = ("origin", "destination", "flight_date", "flight_number", "departure_time")
            await s.execute(stmt.on_duplicate_key_update(**{c: stmt.inserted[c] for c in COLS if c not in keys}))
            await s.execute(delete(Flight).where(Flight.origin == origin, Flight.destination == dest, Flight.flight_date == day,
                                                 Flight.scraped_at < run_started.replace(tzinfo=None)))
        cov = mysql_insert(Coverage).values(origin=origin, destination=dest, flight_date=day,
                                            status="has_flights" if rows else "empty", scraped_at=_now())
        await s.execute(cov.on_duplicate_key_update(status=cov.inserted.status, scraped_at=cov.inserted.scraped_at))


async def mark_no_flights(origin: str, dest: str, days: list[date]) -> None:
    """Jours confirmés sans vol programmé (liste de dates du site) : supprime d'éventuels vols obsolètes."""
    if not days:
        return
    async with Session() as s, s.begin():
        await s.execute(delete(Flight).where(Flight.origin == origin, Flight.destination == dest, Flight.flight_date.in_(days)))
        cov = mysql_insert(Coverage).values([dict(origin=origin, destination=dest, flight_date=d, status="no_flights",
                                                  scraped_at=_now()) for d in days])
        await s.execute(cov.on_duplicate_key_update(status=cov.inserted.status, scraped_at=cov.inserted.scraped_at))


async def set_route_status(origin: str, dest: str, status: str) -> None:
    async with Session() as s, s.begin():
        st = mysql_insert(RouteStatus).values(origin=origin, destination=dest, status=status, checked_at=_now())
        await s.execute(st.on_duplicate_key_update(status=st.inserted.status, checked_at=st.inserted.checked_at))


async def get_no_route(recheck_days: int) -> set[tuple[str, str]]:
    """Trajets inexistants constatés récemment (au-delà de recheck_days, on les revérifie)."""
    limit = _now() - timedelta(days=recheck_days)
    async with Session() as s:
        rows = (await s.execute(select(RouteStatus.origin, RouteStatus.destination)
                                .where(RouteStatus.status == "no_route", RouteStatus.checked_at >= limit))).all()
    return {(o, d) for o, d in rows}


async def get_last_covered() -> Optional[date]:
    async with Session() as s:
        return (await s.execute(select(func.max(Coverage.flight_date)))).scalar_one()


async def purge_past(today: date) -> None:
    async with Session() as s, s.begin():
        await s.execute(delete(Flight).where(Flight.flight_date < today))
        await s.execute(delete(Coverage).where(Coverage.flight_date < today))


async def coverage_summary() -> dict:
    async with Session() as s:
        by_status = (await s.execute(select(Coverage.status, func.count()).group_by(Coverage.status))).all()
        lo, hi = (await s.execute(select(func.min(Coverage.flight_date), func.max(Coverage.flight_date)))).one()
        routes = (await s.execute(select(RouteStatus.status, func.count()).group_by(RouteStatus.status))).all()
    return {"first_date": lo.isoformat() if lo else None, "last_date": hi.isoformat() if hi else None,
            "days_by_status": dict(by_status), "routes_by_status": dict(routes)}


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
