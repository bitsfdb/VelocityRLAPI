"""
Rocket League item extractor.

Primary source: ProductDump.json (CodeRed game database dump via rocketleagueapi/items).
  - Has numeric product IDs, display names, quality, slot, paintable flag.
  - Asset field matches UPK filenames → confirmed items only.

Fallback: UPK file scan for items added after the last dump (no product ID, derived name).

To update the ProductDump, run CodeRed inside the game:
  database_dump_products {product_id} {product_long_label} ... [JSON] [UTF16]
Then drop the output into .cache/ProductDump.json
"""

import base64
import json
import re
import urllib.request
import urllib.error
import hashlib
import os
from pathlib import Path
from datetime import datetime, timezone

GAME_UPK_DIR = Path("/home/ubuntu/Games/rocketleague/TAGame/CookedPCConsole")
LEGENDARY_INSTALLED = Path("/home/ubuntu/.config/legendary/installed.json")
RL_WEBCACHE_DIR = Path("/home/ubuntu/My Games/Rocket League/TAGame/Cache/WebCache")
CACHE_DIR = Path("/home/ubuntu/velrl/.cache")
OUTPUT_FILE = Path("/home/ubuntu/velrl/items.json")

DUMP_URL = (
    "https://raw.githubusercontent.com/rocketleagueapi/items/main/src/raw/ProductDump.json"
)

# Slot index → (category_id, display)
SLOT_MAP = {
    0:  ("body",           "Body"),
    1:  ("decal",          "Decal"),
    2:  ("wheel",          "Wheels"),
    3:  ("boost",          "Rocket Boost"),
    4:  ("antenna",        "Antenna"),
    5:  ("topper",         "Topper"),
    6:  ("bumper",         "Bumper"),
    7:  ("paint_finish",   "Paint Finish"),
    10: ("underglow",      "Underglow"),
    13: ("engine_audio",   "Engine Audio"),
    14: ("trail",          "Trail"),
    15: ("goal_explosion", "Goal Explosion"),
    16: ("player_banner",  "Player Banner"),
    18: ("goal_stinger",   "Goal Stinger"),
    19: ("player_avatar",  "Player Avatar"),
    20: ("avatar_border",  "Avatar Border"),
    21: ("player_title",   "Player Title"),
}

QUALITY_MAP = {
    0: "Common", 1: "Uncommon", 2: "Rare", 3: "Very Rare",
    4: "Import", 5: "Exotic", 6: "Black Market",
    7: "Premium", 8: "Limited", 9: "Legacy",
}

# UPK filename prefix → slot index (for fallback items not in dump)
PREFIX_SLOT = {
    "body": 0, "skin": 1, "wheel": 2, "boost": 3, "antenna": 4,
    "hat": 5, "ss": 14, "explosion": 15, "playerbanner": 16,
    "avatarborder": 20, "engineaudio": 13, "paintfinish": 7,
    "flag": 4, "countryflag": 4, "streamerflag": 4, "anthem": -1,
}

KEEP_UPPER = {"RL", "RLCS", "II", "III", "IV", "GT", "GTS", "GTE",
              "XL", "XR", "FX", "2D", "3D", "DTS", "DT5"}

# Paint colors: id → name (source: rocketleagueapi/items package)
PAINTS: dict[int, str] = {
    1: "Crimson", 2: "Lime", 3: "Black", 4: "Sky Blue", 5: "Cobalt",
    6: "Burnt Sienna", 7: "Forest Green", 8: "Purple", 9: "Pink",
    10: "Orange", 11: "Grey", 12: "Titanium White", 13: "Saffron", 14: "Gold",
}

# Certifications: id → name (source: rocketleagueapi/items package)
CERTIFICATIONS: dict[int, str] = {
    1: "Aviator", 2: "Playmaker", 3: "Show-off", 4: "Acrobat", 5: "Tactician",
    6: "Sweeper", 7: "Guardian", 8: "Scorer", 9: "Juggler", 10: "Sniper",
    11: "Paragon", 12: "Goalkeeper", 13: "Striker", 14: "Turtle", 15: "Victor",
}

# Categories that support certifications
CERTIFIABLE_CATEGORIES = {
    "body", "decal", "wheel", "boost", "antenna", "topper", "trail", "goal_explosion",
}


# ── ProductDump helpers ────────────────────────────────────────────────────────

def _dump_cache_path() -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / "ProductDump.json"


def load_product_dump() -> list[dict]:
    """Load from local cache; download if missing."""
    path = _dump_cache_path()

    if not path.exists():
        print("[extract] Downloading ProductDump.json from rocketleagueapi/items...")
        try:
            req = urllib.request.Request(
                DUMP_URL, headers={"User-Agent": "velrl-extractor/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            path.write_bytes(data)
            print(f"[extract] Saved ProductDump.json ({len(data):,} bytes)")
        except Exception as e:
            raise RuntimeError(f"Cannot fetch ProductDump.json and no local cache: {e}")

    raw = path.read_bytes()
    # The dump is Windows-1252 encoded
    try:
        return json.loads(raw.decode("windows-1252"))
    except UnicodeDecodeError:
        return json.loads(raw.decode("utf-8", errors="replace"))


def build_asset_index(dump: list[dict]) -> dict[str, dict]:
    """Maps lowercase asset name → product entry."""
    index = {}
    for item in dump:
        asset = (item.get("Product Thumbnail Asset") or "").strip()
        if asset:
            index[asset.lower()] = item
    return index


def lookup_asset(stem: str, index: dict) -> dict | None:
    """Try stem, stem-without-_T, stem-with-_T."""
    sl = stem.lower()
    return (
        index.get(sl)
        or index.get(re.sub(r"_t$", "", sl))
        or index.get(sl + "_t")
    )


# ── UPK scan helpers ──────────────────────────────────────────────────────────

def _to_display_name(raw: str) -> str:
    tokens = []
    for part in raw.split("_"):
        sub = re.sub(r"([a-z])([A-Z])", r"\1 \2", part)
        sub = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", sub)
        tokens.extend(sub.split())
    words = []
    for t in tokens:
        if t.upper() in KEEP_UPPER:
            words.append(t.upper())
        elif t.isdigit():
            words.append(t)
        else:
            words.append(t.capitalize())
    return " ".join(words)


# In-memory verified mappings used when the Psyonix localization cache is unavailable
# or doesn't yet include a newly-added item. Keys are the post-prefix name part (lowercase).
STEM_NAME_OVERRIDES: dict[str, str] = {
    # Confirmed via in-memory cluster analysis (engineaudio/body adjacency)
    "zaku_t1":   "Shokunin",
    "stubbyhog": "BMW M2 Racing",
}


LANGUAGES = {
    "en": "INT",
    "es": "ESN",
    "fr": "FRA",
    "de": "DEU",
    "it": "ITA",
    "pt": "PTB",
    "ja": "JPN",
    "ko": "KOR",
    "ru": "RUS",
    "tr": "TRK",
    "nl": "DUT",
    "pl": "POL"
}


def load_psyonix_localization() -> dict[str, dict[str, str]]:
    """Load the official code→display-name map for all 12 supported languages.
    
    Attempts to fetch the live config directly from Psyonix for each language first.
    If a network request fails, falls back to the local cached config if available,
    and finally checks the local game client WebCache directory.
    
    Returns a dictionary mapping:
        codename -> { "en": "display_name", "es": "display_name", ... }
    """
    # codename -> { "en": "display", "es": "display", ... }
    out: dict[str, dict[str, str]] = {}
    
    for lang_code, psynet_code in LANGUAGES.items():
        lang_url = f"https://config.psynet.gg/v2/Config/BattleCars/-1652286008/Prod/Epic/{psynet_code}/"
        cache_file = CACHE_DIR / f"PsyonixConfig_{psynet_code}.json"
        
        data = None
        
        # 1. Try to fetch live from Psyonix config endpoint
        print(f"[extract] Querying live localization overrides ({lang_code}) from: {lang_url}")
        try:
            req = urllib.request.Request(
                lang_url,
                headers={"User-Agent": "velrl-extractor/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            
            # Save a copy as a local cached backup in .cache directory
            try:
                CACHE_DIR.mkdir(exist_ok=True)
                cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            except Exception as e:
                print(f"[extract] Warning: Could not write cache for {lang_code}: {e}")
                
        except Exception as e:
            print(f"[extract] Live request for {lang_code} failed ({e}). Trying backup cache...")
            
        # 2. Fallback to cached backup if it exists
        if not data and cache_file.exists():
            try:
                data = json.loads(cache_file.read_text())
                print(f"[extract] Loaded {lang_code} from backup cache: {cache_file}")
            except Exception as e:
                print(f"[extract] Error reading backup cache for {lang_code}: {e}")
                
        # 3. Parse overrides if we have data
        if data:
            overrides = data.get("LocalizationConfig", {}).get("Overrides", [])
            count = 0
            for o in overrides:
                if o.get("Package") != "Products" or o.get("Key") != "Label":
                    continue
                sect = (o.get("Section") or "").strip().lower()
                val = (o.get("Value") or "").strip()
                if sect and val:
                    if sect not in out:
                        out[sect] = {}
                    out[sect][lang_code] = val
                    count += 1
            print(f"[extract] Processed {count} overrides for {lang_code}")
            
    # 4. Fallback/merge with local game client WebCache directory
    if RL_WEBCACHE_DIR.is_dir():
        print(f"[extract] Scanning local WebCache directory for supplementary localization: {RL_WEBCACHE_DIR}")
        for f in RL_WEBCACHE_DIR.iterdir():
            try:
                decoded = base64.b64decode(f.name).decode("latin-1")
            except Exception:
                continue
            
            # Determine which language this WebCache file belongs to by looking at the path suffix
            lang_found = None
            for lc, pc in LANGUAGES.items():
                if f"/{pc}/" in decoded:
                    lang_found = lc
                    break
            
            if not lang_found:
                continue
                
            try:
                blob = f.read_bytes()
                js_start = blob.find(b"{")
                js_end = blob.rfind(b"}") + 1
                if js_start < 0 or js_end <= js_start:
                    continue
                cfg = json.loads(blob[js_start:js_end].decode("utf-8", errors="replace"))
                
                overrides = cfg.get("LocalizationConfig", {}).get("Overrides", [])
                for o in overrides:
                    if o.get("Package") != "Products" or o.get("Key") != "Label":
                        continue
                    sect = (o.get("Section") or "").strip().lower()
                    val = (o.get("Value") or "").strip()
                    if sect and val:
                        if sect not in out:
                            out[sect] = {}
                        if lang_found not in out[sect]:
                            out[sect][lang_found] = val
            except Exception:
                continue
                
    return out


def scan_new_upk_items(asset_index: dict, loc_map: dict[str, dict[str, str]]) -> list[dict]:
    """Return items present in game files but absent from ProductDump."""
    seen_keys: set[str] = set()
    new_items: list[dict] = []

    for fname in GAME_UPK_DIR.iterdir():
        if not fname.name.endswith(".upk"):
            continue
        stem = re.sub(r"(?i)_SF\.upk$", "", fname.name)

        if lookup_asset(stem, asset_index):
            continue  # already in dump

        prefix = stem.split("_")[0].lower()
        slot_id = PREFIX_SLOT.get(prefix)
        if slot_id is None or slot_id not in SLOT_MAP:
            continue  # not an item UPK

        name_part = "_".join(stem.split("_")[1:])
        name_part = re.sub(r"(?i)_premium_skins?$|_premium$", "", name_part)
        name_part = re.sub(r"(?i)_T\d*$|_T$", "", name_part)

        # Dedup on normalized name+slot (handles _T team variants)
        dedup_key = f"{slot_id}:{name_part.lower()}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        # Resolve display name. Priority:
        #   1. Psyonix BattleCars-cache localization (full stem like body_zrf)
        #   2. STEM_NAME_OVERRIDES (memory-verified, for items not in cache)
        #   3. Derive from filename
        full_stem = stem.lower()
        full_stem = re.sub(r"(?i)_t\d*$|_t$", "", full_stem)
        
        translations = loc_map.get(full_stem, {})
        eng_display = (
            translations.get("en")
            or STEM_NAME_OVERRIDES.get(name_part.lower())
            or (_to_display_name(name_part) if name_part else stem)
        )
        
        # Build complete translations dictionary for all languages
        merged_translations = {}
        for lang_code in LANGUAGES.keys():
            merged_translations[lang_code] = translations.get(lang_code) or eng_display

        cat_id, cat_display = SLOT_MAP[slot_id]
        internal_name = re.sub(r"(?i)_T\d*$|_T$", "", stem)
        new_items.append({
            "id":               None,
            "name":             eng_display,
            "internal_name":    internal_name,
            "category_id":      cat_id,
            "category":         cat_display,
            "quality_id":       None,
            "quality":          "Unknown",
            "paintable":        None,
            "tradable":         None,
            "blueprint":        False,
            "source":           "upk_scan" + ("+psynet_cache" if full_stem in loc_map else ""),
            "thumbnail_asset":  full_stem + "_t",
            "painted_variants": [],
            "certifications":   [],
            "translations":     merged_translations,
        })

    return new_items


# ── Main ──────────────────────────────────────────────────────────────────────

def get_game_version() -> str:
    try:
        return json.loads(LEGENDARY_INSTALLED.read_text()).get("Sugar", {}).get("version", "unknown")
    except Exception:
        return "unknown"


def get_dump_fingerprint() -> str:
    path = _dump_cache_path()
    if not path.exists():
        return "none"
    return hashlib.md5(path.read_bytes()).hexdigest()[:12]


def build_output() -> dict:
    dump = load_product_dump()
    asset_index = build_asset_index(dump)
    loc_map = load_psyonix_localization()

    items: list[dict] = []

    # 1. Known items from ProductDump
    for entry in dump:
        slot_id = entry.get("Slot Index", -1)
        if slot_id not in SLOT_MAP:
            continue
        quality_id = entry.get("Product Quality Id", 0)
        restrictions = entry.get("Product Trade Restrictions", [])

        cat_id, cat_display = SLOT_MAP[slot_id]
        
        # Try to find codename stem in loc_map using Product Thumbnail Asset
        asset_raw  = (entry.get("Product Thumbnail Asset") or "").strip()
        asset      = asset_raw.lower()
        asset_stem = re.sub(r"(?i)_t\d*$|_t$", "", asset)
        internal_name = re.sub(r"(?i)_t\d*$|_t$", "", asset_raw)
        
        translations = loc_map.get(asset_stem) or {}
        eng_display = entry["Product Long Label"]
        
        # Build complete translations dictionary for all languages
        merged_translations = {}
        for lang_code in LANGUAGES.keys():
            merged_translations[lang_code] = translations.get(lang_code) or eng_display

        is_paintable = bool(entry.get("Product Paintable", False))
        is_tradable  = not bool(restrictions)
        items.append({
            "id":               entry["Product Id"],
            "name":             eng_display,
            "internal_name":    internal_name,
            "category_id":      cat_id,
            "category":         cat_display,
            "quality_id":       quality_id,
            "quality":          QUALITY_MAP.get(quality_id, "Unknown"),
            "paintable":        is_paintable,
            "tradable":         is_tradable,
            "blueprint":        entry.get("Product Blueprint", False),
            "source":           "product_dump",
            "thumbnail_asset":  asset,
            "painted_variants": list(PAINTS.values()) if is_paintable else [],
            "certifications":   list(CERTIFICATIONS.values()) if (is_tradable and cat_id in CERTIFIABLE_CATEGORIES) else [],
            "translations":     merged_translations,
        })

    # 2. New items only in game files
    new_items = scan_new_upk_items(asset_index, loc_map)
    items.extend(new_items)

    items.sort(key=lambda x: (x["category_id"], x["name"]))

    by_category = {}
    for item in items:
        by_category[item["category_id"]] = by_category.get(item["category_id"], 0) + 1

    from_cache = sum(1 for it in new_items if "psynet_cache" in (it.get("source") or ""))

    return {
        "meta": {
            "game_version":    get_game_version(),
            "dump_fingerprint": get_dump_fingerprint(),
            "generated_at":    datetime.now(timezone.utc).isoformat(),
            "total_items":     len(items),
            "from_dump":       len(items) - len(new_items),
            "from_upk_scan":   len(new_items),
            "named_via_psynet_cache": from_cache,
            "psynet_cache_size": len(loc_map),
            "categories":      {k: v for k, v in sorted(by_category.items())},
        },
        "items": items,
    }


def generate(output_path: Path = OUTPUT_FILE) -> dict:
    data = build_output()
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    meta = data["meta"]
    print(
        f"[extract] {meta['total_items']} items "
        f"({meta['from_dump']} from dump + {meta['from_upk_scan']} new from UPK scan) "
        f"→ {output_path}"
    )
    return data


if __name__ == "__main__":
    generate()
