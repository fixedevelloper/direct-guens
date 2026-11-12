"""Camair-Co flight scraper + FastAPI + APScheduler.

Run:  uvicorn app_scraper:app --host 0.0.0.0 --port 8000
"""
import asyncio
import itertools
import json
import logging
import os
import random
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from db import count_flights, init_db, query_flights, save_flights
from playwright.async_api import Page, TimeoutError as PWTimeout, async_playwright

# ----------------------------------------------------------------- config
TARGET_URL = os.getenv("TARGET_URL", "https://www.camair-co.cm/page/CM/fr/reserver-un-vol")
AIRPORTS = os.getenv("AIRPORTS", "BFX,BZV,COO,DLA,GOU,LBV,MVR,NGJ,NGE,PNR,NSI").split(",")
RESULTS_URL_PART = os.getenv("RESULTS_URL_PART", "kiusys.net/flightresults")
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "30"))
SCRAPE_INTERVAL_HOURS = float(os.getenv("SCRAPE_INTERVAL_HOURS", "6"))
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
CONCURRENCY = int(os.getenv("CONCURRENCY", "2"))          # pages en parallèle
DELAY_MIN = float(os.getenv("DELAY_MIN", "2"))
DELAY_MAX = float(os.getenv("DELAY_MAX", "6"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
DEBUG_DIR = os.getenv("DEBUG_DIR", "debug")                # captures JSON/HTML pour adapter les sélecteurs
API_KEY = os.getenv("API_KEY", "")
ENABLE_DOCS = os.getenv("ENABLE_DOCS", "false").lower() == "true"
RUN_ON_STARTUP = os.getenv("RUN_ON_STARTUP", "false").lower() == "true"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Le formulaire de réservation est une iframe du moteur Kiusys.
IFRAME_SEL = os.getenv("IFRAME_SEL", "iframe[src*='kiusys']")

log = logging.getLogger("camair")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ----------------------------------------------------------------- state
state: dict[str, Any] = {
    "in_progress": False, "last_started_at": None, "last_finished_at": None,
    "last_error": None, "routes_done": 0, "routes_total": 0, "flights_count": 0,
}
_lock = asyncio.Lock()


# ----------------------------------------------------------------- parsing
TIME_RE = re.compile(r"(\d{1,2})[:hH](\d{2})")


def _time(v) -> Optional[str]:
    if v is None:
        return None
    m = TIME_RE.search(str(v))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


MONTHS = {"JAN": 1, "FEV": 2, "FÉV": 2, "MAR": 3, "AVR": 4, "MAI": 5, "JUN": 6, "JUI": 6, "JUL": 7, "AOU": 8, "AOÛ": 8,
          "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12, "DÉC": 12}
FLIGHT_RE = re.compile(
    r"(?P<nums>(?:QC\s?\d{3,4}\s*)+)\s+"
    r"(?P<dep>\d{1,2}:\d{2})\s+(?P<ddate>\d{1,2} \S+ \d{4})\s+(?P<ocity>[^\n]+)\s+(?P<ocode>[A-Z]{3})\s+"
    r"(?P<dur>[0-9hm ]+?)\s+(?P<kind>VOL DIRECT|\d+ CONNEXIONS?)\s+(?:détail\s+)?"
    r"(?P<arr>\d{1,2}:\d{2})\s+(?P<adate>\d{1,2} \S+ \d{4})\s+(?P<dcity>[^\n]+)\s+(?P<dcode>[A-Z]{3})\s+"
    r"(?P<price>[\d.,]+)\s*XAF", re.I)


def parse_results_text(text: str, origin: str, dest: str, day: date) -> list[dict]:
    """Parse le texte de la page https://ecommerce-us.kiusys.net/flightresults/."""
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for m in FLIGHT_RE.finditer(text):
        price = m.group("price").replace(".", "").replace(",", ".")
        out.append({
            "origin": m.group("ocode").upper(), "destination": m.group("dcode").upper(),
            "origin_city": m.group("ocity").strip(), "destination_city": m.group("dcity").strip(),
            "flight_number": " ".join(m.group("nums").split()),
            "flight_date": day.isoformat(),
            "departure_time": _time(m.group("dep")), "arrival_time": _time(m.group("arr")),
            "arrival_date": _parse_fr_date(m.group("adate")),
            "duration": m.group("dur").strip(), "stops": 0 if "DIRECT" in m.group("kind").upper() else int(m.group("kind").split()[0]),
            "price_xaf": float(price), "availability": "available", "scraped_at": now,
        })
    return out


def _parse_fr_date(s: str) -> Optional[str]:
    try:
        d, mon, y = s.split()
        return date(int(y), MONTHS[mon.upper()[:3]], int(d)).isoformat()
    except (ValueError, KeyError):
        return None


class NoRoute(Exception):
    """Le site ne propose pas ce trajet (destination absente de la liste)."""


NO_ROUTE: set[tuple[str, str]] = set()      # trajets inexistants constatés pendant le run


async def _pick_airport(frame, field: str, code: str) -> None:
    el = frame.locator(f"input[name={field}]")
    await el.click()
    await el.fill("")
    await el.type(code, delay=random.randint(70, 140))
    row = frame.locator(".sg-results__row", has_text=code).first
    try:
        await row.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        if field == "destination":
            raise NoRoute(f"{code} non proposé comme destination")
        raise
    await asyncio.sleep(0.8)
    await row.click()
    if code not in (await el.input_value()):
        raise RuntimeError(f"Aéroport {code} non sélectionné ({field})")


async def search_once(page: Page, origin: str, dest: str, day: date) -> list[dict]:
    ctx = page.context
    available_dates: list[str] = []
    statuses: list[int] = []

    async def on_response(resp):
        statuses.append(resp.status)
        try:
            if resp.request.resource_type in ("fetch", "xhr") and resp.status == 200 and "kiusys" in resp.url:
                if "/searchflight/api/v1/dates/" in resp.url and "is_round_trip=0" in resp.url:
                    available_dates[:] = (await resp.json()).get("response", {}).get("ow", [])
        except Exception:
            pass

    ctx.on("response", on_response)
    try:
        await page.goto(TARGET_URL, timeout=90000, wait_until="domcontentloaded")
        frame = page.frame_locator(IFRAME_SEL)
        await frame.locator("#one_way").wait_for(state="attached", timeout=45000)
        await frame.locator("#one_way").check(force=True)
        await _pick_airport(frame, "origin", origin)
        await _pick_airport(frame, "destination", dest)
        await asyncio.sleep(2)                                # laisse arriver la liste des dates
        if available_dates and day.isoformat() not in available_dates:
            log.info("%s→%s %s : aucun vol programmé", origin, dest, day)
            return []

        await frame.locator("input[name=date]").first.click()
        cell = frame.locator(f"td.cell[title='{day.strftime('%d/%m/%Y')}']:not(.disabled)").first
        for _ in range(3):                                    # navigue jusqu'au bon mois
            if await cell.count():
                break
            await frame.locator("#firstDatePicker .mx-btn-icon-right").first.click()
            await asyncio.sleep(0.5)
        await cell.click(timeout=8000)
        await frame.locator("#search_button").click()

        await page.wait_for_url(f"**{RESULTS_URL_PART}**", timeout=45000)
        await page.wait_for_load_state("networkidle", timeout=45000)
        if any(s in (429, 503) for s in statuses):
            raise RuntimeError("Rate-limit / service indisponible (429/503)")
        try:
            await page.get_by_text(re.compile(r"XAF|Aucun vol|aucun résultat|indisponible", re.I)).first.wait_for(timeout=25000)
        except PWTimeout:
            pass
        await asyncio.sleep(1.5)

        text = await page.inner_text("body")
        results = parse_results_text(text, origin, dest, day)
        if not results and DEBUG_DIR:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            tag = f"{origin}-{dest}-{day}"
            open(f"{DEBUG_DIR}/{tag}.txt", "w", encoding="utf-8").write(text)
            await page.screenshot(path=f"{DEBUG_DIR}/{tag}.png")
        return results
    finally:
        ctx.remove_listener("response", on_response)


async def search_with_retry(context, origin: str, dest: str, day: date) -> list[dict]:
    if (origin, dest) in NO_ROUTE:
        return []
    for attempt in range(1, MAX_RETRIES + 1):
        page = await context.new_page()
        try:
            return await search_once(page, origin, dest, day)
        except NoRoute as e:
            NO_ROUTE.add((origin, dest))
            log.info("%s→%s : trajet inexistant (%s), ignoré", origin, dest, e)
            return []
        except (PWTimeout, RuntimeError, Exception) as e:  # noqa: BLE001
            wait = (2 ** attempt) * random.uniform(3, 6)
            log.warning("%s→%s %s tentative %d/%d: %s (pause %.0fs)", origin, dest, day, attempt, MAX_RETRIES, e, wait)
            await asyncio.sleep(wait)
        finally:
            for p in list(context.pages):
                await p.close()
    return []


async def run_scrape() -> None:
    if _lock.locked():
        log.info("Scraping déjà en cours, ignoré")
        return
    async with _lock:
        started = datetime.now(timezone.utc)
        state.update(in_progress=True, last_started_at=started.isoformat(), last_error=None, routes_done=0)
        today = date.today()
        days = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
        jobs = [(o, d, day) for o, d in itertools.permutations(AIRPORTS, 2) for day in days]
        NO_ROUTE.clear()
        state["routes_total"] = len(jobs)
        new: list[dict] = []
        sem = asyncio.Semaphore(CONCURRENCY)
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=HEADLESS)

                async def worker(job):
                    o, d, day = job
                    async with sem:
                        if (o, d) in NO_ROUTE:
                            state["routes_done"] += 1
                            return
                        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                        ctx = await browser.new_context(
                            user_agent=random.choice(USER_AGENTS), locale="fr-FR",
                            timezone_id="Africa/Douala", viewport={"width": 1366, "height": 768},
                            extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7"})
                        try:
                            new.extend(await search_with_retry(ctx, o, d, day))
                        finally:
                            await ctx.close()
                            state["routes_done"] += 1

                await asyncio.gather(*(worker(j) for j in jobs))
                await browser.close()
            await save_flights(new, days[0], days[-1], started)   # uniquement si le run a abouti
            state["flights_count"] = await count_flights()
        except Exception as e:  # noqa: BLE001
            log.exception("Échec du scraping")
            state["last_error"] = str(e)
        finally:
            state.update(in_progress=False, last_finished_at=datetime.now(timezone.utc).isoformat())
            log.info("Scraping terminé : %d vols collectés", len(new))


# ----------------------------------------------------------------- API
scheduler = AsyncIOScheduler()


_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: Optional[str] = Security(_key_header)) -> None:
    if not key or not secrets.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(401, "Clé API invalide ou absente", headers={"WWW-Authenticate": "ApiKey"})


@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(API_KEY) < 16:
        raise RuntimeError("API_KEY manquante ou trop courte (16 caractères minimum). Générer : openssl rand -hex 32")
    await init_db()
    state["flights_count"] = await count_flights()
    scheduler.add_job(run_scrape, "interval", hours=SCRAPE_INTERVAL_HOURS, id="scrape",
                      max_instances=1, coalesce=True,
                      next_run_time=datetime.now(timezone.utc) if RUN_ON_STARTUP else None)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Camair-Co Scraper API", version="1.0", lifespan=lifespan,
              docs_url="/docs" if ENABLE_DOCS else None, redoc_url=None,
              openapi_url="/openapi.json" if ENABLE_DOCS else None)


@app.get("/")
async def root():
    return {"service": "Camair-Co flight scraper", "status": "ok"}


@app.get("/api/info", dependencies=[Depends(require_api_key)])
async def info():
    job = scheduler.get_job("scrape")
    return {"service": "Camair-Co flight scraper", "airports": AIRPORTS, "days_ahead": DAYS_AHEAD,
            "interval_hours": SCRAPE_INTERVAL_HOURS, "next_scheduled_run": job.next_run_time if job else None,
            "flights_count": await count_flights(), "docs": "/docs"}


@app.post("/api/scrape/start", status_code=202, dependencies=[Depends(require_api_key)])
async def start(bg: BackgroundTasks):
    if state["in_progress"]:
        raise HTTPException(409, "Scraping déjà en cours")
    bg.add_task(run_scrape)
    return {"message": "Scraping lancé en arrière-plan"}


@app.get("/api/scrape/status", dependencies=[Depends(require_api_key)])
async def status():
    return state


@app.get("/api/flights", dependencies=[Depends(require_api_key)])
async def get_flights(origin: Optional[str] = Query(None, description="Code IATA"),
                      destination: Optional[str] = Query(None, description="Code IATA"),
                      date: Optional[str] = Query(None, description="YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$"),
                      limit: int = Query(1000, ge=1, le=10000), offset: int = Query(0, ge=0)):
    total, rows = await query_flights(origin, destination, date, limit, offset)
    return {"count": total, "returned": len(rows), "flights": rows}
