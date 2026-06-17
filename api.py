import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading

from dotenv import load_dotenv
load_dotenv()
import logging
import time
from pathlib import Path
from typing import Any, Optional
from collections import defaultdict

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from fastapi import FastAPI, Query, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from extract_items import generate, OUTPUT_FILE, LANGUAGES, PAINTS, CERTIFICATIONS

_START_TIME = time.time()

DIAGNOSTIC_SECRET    = os.getenv("DIAGNOSTIC_SECRET", "")
INTERNAL_SECRET      = os.getenv("INTERNAL_SECRET", "")
BOT_URL              = os.getenv("BOT_URL", "http://127.0.0.1:3000")
UPDATE_MANIFEST_FILE = Path(__file__).parent / "update_manifest.json"
CLERK_PUBLISHABLE_KEY = os.getenv("CLERK_PUBLISHABLE_KEY", "")

# Derive JWKS URL from publishable key: base64-decode the part after "pk_test_" / "pk_live_"
def _clerk_jwks_url() -> str:
    try:
        b64 = CLERK_PUBLISHABLE_KEY.split("_", 2)[-1]
        # add padding
        b64 += "=" * (-len(b64) % 4)
        frontend_api = base64.b64decode(b64).decode().rstrip("$")
        return f"https://{frontend_api}/.well-known/jwks.json"
    except Exception:
        return ""

CLERK_JWKS_URL = _clerk_jwks_url()

# ---------------------------------------------------------------------------
# Clerk JWT verification
# ---------------------------------------------------------------------------

_jwks_cache: dict = {}
_jwks_fetched_at: float = 0.0

async def _get_jwks() -> dict:
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache and now - _jwks_fetched_at < 3600:
        return _jwks_cache
    if not CLERK_JWKS_URL:
        return {}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(CLERK_JWKS_URL)
        resp.raise_for_status()
        _jwks_cache = {k["kid"]: k for k in resp.json().get("keys", [])}
        _jwks_fetched_at = now
        return _jwks_cache

async def verify_clerk_token(token: str) -> Optional[dict]:
    """Verify a Clerk JWT. Returns the decoded payload or None."""
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        jwks = await _get_jwks()
        if not kid or kid not in jwks:
            return None
        public_key = RSAAlgorithm.from_jwk(json.dumps(jwks[kid]))
        payload = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_aud": False})
        return payload
    except Exception:
        return None

# ---------------------------------------------------------------------------
# API key store (SQLite)
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "apikeys.db"
_db_lock = threading.Lock()

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def _init_db():
    with _db_lock, _db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                name       TEXT NOT NULL,
                key_hash   TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                last_used  INTEGER,
                revoked    INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()

_init_db()

KEY_PREFIX = "vrl_"

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

def lookup_api_key(raw: str) -> Optional[dict]:
    """Return the key row if valid and not revoked, else None. Updates last_used."""
    h = _hash_key(raw)
    with _db_lock, _db() as con:
        row = con.execute(
            "SELECT * FROM api_keys WHERE key_hash=? AND revoked=0", (h,)
        ).fetchone()
        if row:
            con.execute("UPDATE api_keys SET last_used=? WHERE id=?", (int(time.time()), row["id"]))
            con.commit()
            return dict(row)
    return None


ICON_PATH = Path(__file__).parent / "bot" / "icon.svg"

THUMBNAILS_DIR = Path("/home/ubuntu/velrl/thumbnails")
THUMBNAILS_BASE = "/thumbnails"

log = logging.getLogger("uvicorn.error")

app = FastAPI(
    title="VelocityRL Products API",
    description="Rocket League product/item data extracted from game files with multi-language support.",
    version="2.0.0",
)

# Rate Limiting Configuration
RATE_LIMIT_WINDOW      = 60
MAX_REQUESTS_ANON      = 60
MAX_REQUESTS_AUTHED    = 300
request_history = defaultdict(list)
_last_cleanup = time.time()
_cleanup_lock = threading.Lock()


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        global _last_cleanup
        now = time.time()

        # Resolve identity: auth'd users get keyed by user_id, others by IP
        key = request.client.host if request.client else "unknown"
        limit = MAX_REQUESTS_ANON
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            if token.startswith(KEY_PREFIX):
                row = lookup_api_key(token)
                if row:
                    key = f"apikey:{row['user_id']}"
                    limit = MAX_REQUESTS_AUTHED
            else:
                payload = await verify_clerk_token(token)
                if payload and payload.get("sub"):
                    key = f"user:{payload['sub']}"
                    limit = MAX_REQUESTS_AUTHED

        # Periodic cleanup to prevent memory growth
        if now - _last_cleanup > 300:
            with _cleanup_lock:
                if now - _last_cleanup > 300:
                    for k in list(request_history.keys()):
                        request_history[k] = [t for t in request_history[k] if now - t < RATE_LIMIT_WINDOW]
                        if not request_history[k]:
                            del request_history[k]
                    _last_cleanup = now

        history = [t for t in request_history[key] if now - t < RATE_LIMIT_WINDOW]
        request_history[key] = history

        remaining  = max(0, limit - len(history) - 1)
        reset_time = int(RATE_LIMIT_WINDOW - (now - history[0])) if history else RATE_LIMIT_WINDOW

        if len(history) >= limit:
            resp = JSONResponse(
                status_code=429,
                content={"detail": f"Too Many Requests. Limit: {limit}/min. Sign in at velocityrl.tech/api-access.html for a higher limit."}
            )
            resp.headers["X-RateLimit-Limit"]     = str(limit)
            resp.headers["X-RateLimit-Remaining"] = "0"
            resp.headers["X-RateLimit-Reset"]     = str(reset_time)
            return resp

        request_history[key].append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"]     = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"]     = str(reset_time)
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


_QUALITY_NORMALIZE = {"VeryRare": "Very Rare", "BlackMarket": "Black Market"}


def _thumbnail_url(item: dict) -> str | None:
    asset = (item.get("thumbnail_asset") or "").strip().lower()
    if not asset:
        return None
    png = THUMBNAILS_DIR / (asset + ".png")
    return f"{THUMBNAILS_BASE}/{asset}.png" if png.exists() else None


def format_item(item: dict, lang_key: str, full: bool = False) -> dict:
    """Normalize fields from either capitalized or lowercase formats."""
    exclude = {"thumbnail_asset", "thumbnail_package", "thumbnail_base64"}
    if not full:
        exclude.add("translations")
    formatted = {k: v for k, v in item.items() if k not in exclude}

    # Extract properties safely regardless of payload casing
    item_id = item.get("id") if item.get("id") is not None else item.get("ID")
    raw_name = item.get("name") or item.get("label") or item.get("Product") or ""
    category = item.get("category") or item.get("slot") or item.get("Slot") or ""
    internal_name = item.get("internal_name") or item.get("asset_package") or item.get("AssetPackage") or ""
    
    # Resolve quality mappings safely
    quality_raw = (item.get("quality_label") 
                   or item.get("quality") 
                   or item.get("Quality") 
                   or "")
    if not isinstance(quality_raw, str):
        quality_raw = ""

    translations = item.get("translations", {})
    
    # Assign back to standardized internal keys
    formatted["id"] = item_id
    formatted["name"] = translations.get(lang_key) or raw_name
    formatted["category"] = category
    formatted["internal_name"] = internal_name
    formatted["quality"] = _QUALITY_NORMALIZE.get(quality_raw, quality_raw)
    formatted["thumbnail_url"] = _thumbnail_url(item)
    
    return formatted


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "velocityrl-products-api",
        "version": "2.0.0",
        "docs": "/docs",
        "endpoints": {
            "products":   "/v2/rl/products",
            "categories": "/v2/rl/categories",
            "attributes": "/v2/rl/attributes",
            "meta":       "/v2/rl/meta",
            "refresh":    "/v2/rl/refresh",
        }
    }


@app.get("/v2/rl/products", summary="All products")
def get_products(
    category: Optional[str] = Query(None, description="Filter by category_id (e.g. body, wheel, decal)"),
    search: Optional[str] = Query(None, description="Case-insensitive name search (matches default or target language name)"),
    lang: Optional[str] = Query(None, description="Language code (e.g. 'en', 'es', 'INT', 'ESN')"),
    limit: int = Query(0, ge=0, description="Max results (0 = no limit)"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    full: bool = Query(False, description="Include painted_variants, certifications, and all translations"),
):
    data = _load()
    items = data["items"]
    lang_key = get_lang_key(lang)

    if category:
        cat_lower = category.lower()
        items = [i for i in items if (i.get("category_id") or i.get("slot") or "").lower() == cat_lower]

    if search:
        q = search.lower()
        items = [
            i for i in items
            if q in (i.get("translations", {}).get(lang_key) or i.get("label") or i.get("name") or "").lower()
            or q in (i.get("asset_package") or i.get("internal_name") or "").lower()
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
        "products": [format_item(i, lang_key, full=full) for i in items],
    }


@app.get("/v2/rl/products/{product_id}", summary="Single product by ID")
def get_product(
    product_id: str,
    lang: Optional[str] = Query(None, description="Language code (e.g. 'en', 'es', 'INT', 'ESN')"),
    full: bool = Query(False, description="Include painted_variants, certifications, and all translations"),
):
    data = _load()
    lang_key = get_lang_key(lang)
    pid = product_id.strip().lower()
    for item in data["items"]:
        if str(item["id"]).lower() == pid:
            return format_item(item, lang_key, full=full)
    raise HTTPException(status_code=404, detail=f"Product '{product_id}' not found")


@app.get("/items.json", include_in_schema=False)
def items_json_compat():
    """Compatibility shim for the desktop app — returns raw items list."""
    data = _load()
    compat_items = []
    for item in data.get("items", []):
        asset = (item.get("thumbnail_asset") or "").strip().lower()
        src = ""
        if asset:
            png = THUMBNAILS_DIR / f"{asset}.png"
            if png.exists():
                src = f"https://api.velocityrl.tech{THUMBNAILS_BASE}/{asset}.png"

        quality_raw = (item.get("quality_label")
                       if item.get("quality_label")
                       else (item.get("quality") if isinstance(item.get("quality"), str) else ""))
        quality_mapped = _QUALITY_NORMALIZE.get(quality_raw, quality_raw)

        asset_package = item.get("asset_package") or ""
        if asset_package and not asset_package.endswith(".upk"):
            asset_package = f"{asset_package}_SF.upk"

        compat_items.append({
            "ID": item.get("id"),
            "Product": item.get("label") or item.get("name") or "",
            "Quality": quality_mapped or "",
            "Slot": item.get("slot") or item.get("category_id") or "",
            "AssetPackage": asset_package,
            "AssetPath": item.get("asset_path") or "",
            "src": src
        })
    return {"items": compat_items}


@app.get("/v2/rl/categories", summary="List all categories with counts")
def get_categories():
    data = _load()
    if "meta" in data and "categories" in data.get("meta", {}):
        return {"categories": data["meta"]["categories"]}
    counts: dict[str, int] = {}
    for i in data["items"]:
        slot = i.get("slot") or i.get("category_id") or "Unknown"
        counts[slot] = counts.get(slot, 0) + 1
    return {"categories": dict(sorted(counts.items()))}


@app.get("/v2/rl/attributes", summary="Paint colors and certifications lookup tables")
def get_attributes():
    return {
        "paints": {str(k): v for k, v in PAINTS.items()},
        "certifications": {str(k): v for k, v in CERTIFICATIONS.items()},
    }


@app.get("/v2/rl/meta", summary="Metadata: game version, item count, generated timestamp")
def get_meta():
    data = _load()
    if "meta" in data:
        return data["meta"]
    return {
        "generated_at": data.get("generated_at"),
        "total_items": data.get("item_count", len(data["items"])),
    }


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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    payload = await verify_clerk_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


@app.get("/auth/me", summary="Current authenticated user info")
async def auth_me(user: dict = Depends(_get_current_user)):
    return {
        "user_id":    user.get("sub"),
        "email":      user.get("email"),
        "username":   user.get("username"),
        "created_at": user.get("iat"),
        "plan":       "authenticated",
        "rate_limit": MAX_REQUESTS_AUTHED,
    }


class KeyCreateRequest(BaseModel):
    name: str


@app.post("/auth/keys", summary="Create a new API key")
async def create_key(body: KeyCreateRequest, user: dict = Depends(_get_current_user)):
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    name = body.name.strip()
    if not name or len(name) > 64:
        raise HTTPException(status_code=400, detail="name must be 1-64 characters")
    with _db_lock, _db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=? AND revoked=0", (user_id,)
        ).fetchone()[0]
        if count >= 10:
            raise HTTPException(status_code=400, detail="Maximum of 10 active API keys per user")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = secrets.token_urlsafe(16)
    with _db_lock, _db() as con:
        con.execute(
            "INSERT INTO api_keys (id, user_id, name, key_hash, created_at) VALUES (?,?,?,?,?)",
            (key_id, user_id, name, _hash_key(raw), int(time.time()))
        )
        con.commit()
    return {"id": key_id, "name": name, "key": raw, "created_at": int(time.time())}


@app.get("/auth/keys", summary="List your API keys")
async def list_keys(user: dict = Depends(_get_current_user)):
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    with _db_lock, _db() as con:
        rows = con.execute(
            "SELECT id, name, created_at, last_used FROM api_keys WHERE user_id=? AND revoked=0 ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
    return {"keys": [dict(r) for r in rows]}


@app.delete("/auth/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(key_id: str, user: dict = Depends(_get_current_user)):
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    with _db_lock, _db() as con:
        result = con.execute(
            "UPDATE api_keys SET revoked=1 WHERE id=? AND user_id=?",
            (key_id, user_id)
        )
        con.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Key not found")
    return {"revoked": True}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", summary="API and service status", include_in_schema=False)
def health():
    uptime_s = time.time() - _START_TIME
    d  = int(uptime_s // 86400)
    h  = int((uptime_s % 86400) // 3600)
    m  = int((uptime_s % 3600) // 60)
    s  = int(uptime_s % 60)
    friendly = " ".join(filter(None, [
        f"{d}d" if d else "", f"{h}h" if h else "",
        f"{m}m" if m else "", f"{s}s",
    ]))
    try:
        item_count = len(_load()["items"])
    except Exception:
        item_count = None
    return {
        "status":      "ok",
        "uptime":      friendly,
        "uptime_s":    int(uptime_s),
        "item_count":  item_count,
    }


# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

@app.get("/icon.svg", include_in_schema=False)
def serve_icon():
    if not ICON_PATH.exists():
        raise HTTPException(status_code=404, detail="icon.svg not found")
    return FileResponse(str(ICON_PATH), media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# Diagnostics — receives error reports from the VelocityRL desktop app
# ---------------------------------------------------------------------------

class DiagnosticPayload(BaseModel):
    # identity
    event:     Optional[str] = None
    context:   Optional[str] = None
    version:   Optional[str] = None
    os:        Optional[str] = None
    arch:      Optional[str] = None
    timestamp: Optional[int] = None   # unix seconds from client
    # error detail
    message:   Optional[str] = None
    stderr:    Optional[str] = None   # engine / sidecar stderr (Python traceback etc.)
    stdout:    Optional[str] = None
    backtrace: Optional[str] = None   # JS or Rust stack trace
    exit_code: Optional[int] = None
    # swap context
    owned_id:  Optional[str] = None
    wanted_id: Optional[str] = None
    game_dir:  Optional[str] = None   # last path component only, no full path
    # integrity context
    expected:  Optional[str] = None
    actual:    Optional[str] = None


@app.get("/latest.json", include_in_schema=False)
async def get_update_manifest():
    if UPDATE_MANIFEST_FILE.exists():
        return JSONResponse(json.loads(UPDATE_MANIFEST_FILE.read_text()))
    raise HTTPException(status_code=404, detail="No update manifest published yet")


@app.post("/internal/update-manifest", include_in_schema=False)
async def set_update_manifest(request: Request):
    ip = request.client.host
    if ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        raise HTTPException(status_code=403)
    secret = request.headers.get("x-internal-secret", "")
    if not INTERNAL_SECRET or secret != INTERNAL_SECRET:
        raise HTTPException(status_code=401)
    body = await request.json()
    UPDATE_MANIFEST_FILE.write_text(json.dumps(body, indent=2))
    return {"ok": True}


@app.post("/diagnostic", summary="Receive diagnostic report from VelocityRL app", include_in_schema=False)
async def receive_diagnostic(
    payload: DiagnosticPayload,
    authorization: Optional[str] = Header(None),
):
    if not DIAGNOSTIC_SECRET:
        raise HTTPException(status_code=503, detail="Diagnostics not configured")

    if authorization != f"Bearer {DIAGNOSTIC_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    if INTERNAL_SECRET:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{BOT_URL}/internal/diagnostic",
                    json=payload.model_dump(),
                    headers={"x-internal-secret": INTERNAL_SECRET},
                )
        except Exception as exc:
            log.warning("Failed to relay diagnostic to bot: %s", exc)

    return {"received": True}
