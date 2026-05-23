import json
import threading
import logging
import time
from pathlib import Path
from typing import Optional
from collections import defaultdict

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from extract_items import generate, OUTPUT_FILE, LANGUAGES

THUMBNAILS_DIR = Path("/home/ubuntu/velrl/thumbnails")
THUMBNAILS_BASE = "/thumbnails"

log = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="VelocityRL Products API",
    description="Rocket League product/item data extracted from game files with multi-language support.",
    version="2.0.0",
)

# Rate Limiting Configuration
RATE_LIMIT_WINDOW = 60  # window size in seconds
MAX_REQUESTS_PER_WINDOW = 60  # max requests per window per IP
request_history = defaultdict(list)
_last_cleanup = time.time()
_cleanup_lock = threading.Lock()


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global _last_cleanup
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Periodically clean up old IP records to prevent memory leaks (runs every 5 mins)
        if now - _last_cleanup > 300:
            with _cleanup_lock:
                if now - _last_cleanup > 300:
                    for ip in list(request_history.keys()):
                        request_history[ip] = [t for t in request_history[ip] if now - t < RATE_LIMIT_WINDOW]
                        if not request_history[ip]:
                            del request_history[ip]
                    _last_cleanup = now

        # Fetch and clean IP request history
        history = request_history[client_ip]
        history = [t for t in history if now - t < RATE_LIMIT_WINDOW]
        request_history[client_ip] = history

        remaining = max(0, MAX_REQUESTS_PER_WINDOW - len(history) - 1)
        reset_time = int(RATE_LIMIT_WINDOW - (now - history[0])) if history else RATE_LIMIT_WINDOW

        if len(history) >= MAX_REQUESTS_PER_WINDOW:
            response = JSONResponse(
                status_code=429,
                content={"detail": "Too Many Requests. Please slow down (limit: 60 requests/min)."}
            )
            response.headers["X-RateLimit-Limit"] = str(MAX_REQUESTS_PER_WINDOW)
            response.headers["X-RateLimit-Remaining"] = "0"
            response.headers["X-RateLimit-Reset"] = str(reset_time)
            return response

        request_history[client_ip].append(now)

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(MAX_REQUESTS_PER_WINDOW)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_time)
        return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)

THUMBNAILS_DIR.mkdir(exist_ok=True)
app.mount(THUMBNAILS_BASE, StaticFiles(directory=str(THUMBNAILS_DIR)), name="thumbnails")

_reload_lock = threading.Lock()
_data_cache: dict | None = None
_cache_mtime: float = 0.0


def _load() -> dict:
    global _data_cache, _cache_mtime
    try:
        mtime = OUTPUT_FILE.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0

    if _data_cache is not None and mtime == _cache_mtime:
        return _data_cache

    with _reload_lock:
        try:
            mtime = OUTPUT_FILE.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0

        if _data_cache is not None and mtime == _cache_mtime:
            return _data_cache

        if mtime == 0.0:
            log.info("items.json not found — generating now")
            generate(OUTPUT_FILE)
            mtime = OUTPUT_FILE.stat().st_mtime

        _data_cache = json.loads(OUTPUT_FILE.read_text())
        _cache_mtime = mtime
        return _data_cache


def get_lang_key(lang: Optional[str]) -> str:
    """Resolve standard 2-letter or 3-letter language code to the internal 2-letter key."""
    if not lang or not isinstance(lang, str):
        return "en"
    lang_clean = lang.strip().lower()
    # Check if direct 2-letter key matches
    if lang_clean in LANGUAGES:
        return lang_clean
    # Check if 3-letter Psyonix value matches
    for k, v in LANGUAGES.items():
        if v.lower() == lang_clean:
            return k
    return "en"


def _thumbnail_url(item: dict) -> str | None:
    asset = (item.get("thumbnail_asset") or "").strip().lower()
    if not asset:
        return None
    png = THUMBNAILS_DIR / (asset + ".png")
    return f"{THUMBNAILS_BASE}/{asset}.png" if png.exists() else None


def format_item(item: dict, lang_key: str) -> dict:
    """Resolve localized name, attach thumbnail_url, strip internal fields."""
    formatted = {k: v for k, v in item.items() if k not in ("translations", "thumbnail_asset")}
    translations = item.get("translations", {})
    formatted["name"] = translations.get(lang_key) or item.get("name")
    formatted["thumbnail_url"] = _thumbnail_url(item)
    return formatted


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "velocityrl-products-api",
        "version": "2.0.0",
        "docs": "/docs",
        "endpoints": {
            "products": "/v2/rl/products",
            "categories": "/v2/rl/categories",
            "meta": "/v2/rl/meta",
            "refresh": "/v2/rl/refresh"
        }
    }


@app.get("/v2/rl/products", summary="All products")
def get_products(
    category: Optional[str] = Query(None, description="Filter by category_id (e.g. body, wheel, decal)"),
    search: Optional[str] = Query(None, description="Case-insensitive name search (matches default or target language name)"),
    lang: Optional[str] = Query(None, description="Language code (e.g. 'en', 'es', 'INT', 'ESN')"),
    limit: int = Query(0, ge=0, description="Max results (0 = no limit)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    data = _load()
    items = data["items"]
    lang_key = get_lang_key(lang)

    if category:
        items = [i for i in items if i["category_id"] == category.lower()]

    if search:
        q = search.lower()
        items = [
            i for i in items
            if q in i.get("translations", {}).get(lang_key, i.get("name", "")).lower()
            or q in i.get("name", "").lower()
        ]

    total = len(items)

    if offset:
        items = items[offset:]
    if limit:
        items = items[:limit]

    return {
        "meta": {
            "returned": len(items),
            "total_filtered": total,
            "limit": limit,
            "offset": offset,
        },
        "products": [format_item(i, lang_key) for i in items],
    }


@app.get("/v2/rl/products/{product_id}", summary="Single product by ID")
def get_product(
    product_id: str,
    lang: Optional[str] = Query(None, description="Language code (e.g. 'en', 'es', 'INT', 'ESN')"),
):
    data = _load()
    lang_key = get_lang_key(lang)
    pid = product_id.strip().lower()
    for item in data["items"]:
        if str(item["id"]).lower() == pid:
            return format_item(item, lang_key)
    raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found")


@app.get("/v2/rl/categories", summary="List all categories with counts")
def get_categories():
    data = _load()
    return {"categories": data["meta"]["categories"]}


@app.get("/v2/rl/meta", summary="Metadata: game version, item count, generated timestamp")
def get_meta():
    data = _load()
    return data["meta"]


@app.post("/v2/rl/refresh", summary="Force regenerate items.json from game files")
def refresh_products():
    global _data_cache, _cache_mtime
    with _reload_lock:
        data = generate(OUTPUT_FILE)
        _data_cache = data
        _cache_mtime = OUTPUT_FILE.stat().st_mtime
    return {
        "status": "ok",
        "meta": data["meta"],
    }
