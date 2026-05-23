# 📡 API Endpoints

VelocityRL exposes endpoints under `/v2/rl/` to browse, search, and manage localized item configurations. All endpoints are accessible over secure HTTPS.

---

## 1. Search Products

`GET /v2/rl/products`

Retrieves, filters, and searches the complete database of Rocket League items in-memory.

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :---: | :---: | :--- |
| `category` | `string` | No | Filter by category ID (e.g., `body`, `wheel`, `decal`, `boost`, `topper`). |
| `search` | `string` | No | Case-insensitive name search. Matches both default (English) and translated localized names. |
| `lang` | `string` | No | Language translation identifier. Supports 2-letter ISO (e.g. `es`, `fr`) or 3-letter Psyonix codes (e.g. `ESN`, `FRA`). Defaults to `en`. |
| `limit` | `integer` | No | Maximum number of results to return (Pagination). Set to `0` for unlimited. Defaults to `0`. |
| `offset` | `integer` | No | Number of records to skip (Pagination). Defaults to `0`. |

### Sample Request

```bash
curl -s "https://api.sfdb.dev/v2/rl/products?category=wheel&search=cristiano&limit=1"
```

### Sample Response

```json
{
  "meta": {
    "returned": 1,
    "total_filtered": 5,
    "limit": 1,
    "offset": 0
  },
  "products": [
    {
      "id": 386,
      "name": "Cristiano",
      "category_id": "wheel",
      "category": "Wheels",
      "quality_id": 3,
      "quality": "Very Rare",
      "paintable": true,
      "tradable": true,
      "blueprint": false,
      "source": "product_dump",
      "thumbnail_url": "/thumbnails/wheel_soccerball_t.png"
    }
  ]
}
```

---

## 2. Get Product By ID

`GET /v2/rl/products/{product_id}`

Retrieves exact metadata details for a single item by its ID.

### Path Parameters

| Parameter | Type | Required | Description |
| :--- | :---: | :---: | :--- |
| `product_id` | `string` | Yes | The numeric identifier of the Rocket League item. |

### Query Parameters

| Parameter | Type | Required | Description |
| :--- | :---: | :---: | :--- |
| `lang` | `string` | No | Translation language (2-letter or 3-letter). Defaults to `en`. |

### Sample Request

```bash
curl -s "https://api.sfdb.dev/v2/rl/products/386?lang=de"
```

### Sample Response

```json
{
  "id": 386,
  "name": "Cristiano",
  "category_id": "wheel",
  "category": "Wheels",
  "quality_id": 3,
  "quality": "Very Rare",
  "paintable": true,
  "tradable": true,
  "blueprint": false,
  "source": "product_dump",
  "thumbnail_url": "/thumbnails/wheel_soccerball_t.png"
}
```

### Errors

If the ID doesn't exist, it returns a standard FastAPI `404 Not Found` error payload:

```json
{
  "detail": "Product '99999' not found"
}
```

---

## 3. List Categories with Counts

`GET /v2/rl/categories`

Retrieves a detailed dictionary of all item categories along with their respective item counts in the active database.

### Sample Request

```bash
curl -s "https://api.sfdb.dev/v2/rl/categories"
```

### Sample Response

```json
{
  "categories": {
    "antenna": 963,
    "avatar_border": 171,
    "body": 298,
    "boost": 427,
    "decal": 3173,
    "engine_audio": 244,
    "goal_explosion": 312,
    "goal_stinger": 246,
    "paint_finish": 148,
    "player_banner": 730,
    "player_title": 1,
    "topper": 590,
    "trail": 204,
    "wheel": 1221
  }
}
```

---

## 4. Get Database Telemetry & Metadata

`GET /v2/rl/meta`

Provides administrative metadata, dump details, and item counts parsed from the game binaries.

### Sample Request

```bash
curl -s "https://api.sfdb.dev/v2/rl/meta"
```

### Sample Response

```json
{
  "game_version": "++Prime+Update58.1-CL-517210",
  "dump_fingerprint": "4fec64afc788",
  "generated_at": "2026-05-23T09:12:50.822601+00:00",
  "total_items": 8728,
  "from_dump": 7173,
  "from_upk_scan": 1555,
  "named_via_psynet_cache": 471,
  "psynet_cache_size": 544,
  "categories": {
    "antenna": 963,
    "avatar_border": 171,
    "body": 298,
    "boost": 427,
    "decal": 3173,
    "engine_audio": 244,
    "goal_explosion": 312,
    "goal_stinger": 246,
    "paint_finish": 148,
    "player_banner": 730,
    "player_title": 1,
    "topper": 590,
    "trail": 204,
    "wheel": 1221
  }
}
```

---

## 5. Force Refresh / Regenerate Database

`POST /v2/rl/refresh`

Triggers the backend mapping parser engine to reload local Unreal package dumps and Psynet web caches to overwrite `items.json` and hot-reload API memory thread-safely.

### Sample Request

```bash
curl -X POST -s "https://api.sfdb.dev/v2/rl/refresh"
```

### Sample Response

```json
{
  "status": "ok",
  "meta": {
    "game_version": "++Prime+Update58.1-CL-517210",
    "dump_fingerprint": "4fec64afc788",
    "generated_at": "2026-05-23T09:28:03.990932+00:00",
    "total_items": 8728,
    "from_dump": 7173,
    "from_upk_scan": 1555,
    "named_via_psynet_cache": 471,
    "psynet_cache_size": 544,
    "categories": {
      "antenna": 963,
      "avatar_border": 171,
      "body": 298,
      "boost": 427,
      "decal": 3173,
      "engine_audio": 244,
      "goal_explosion": 312,
      "goal_stinger": 246,
      "paint_finish": 148,
      "player_banner": 730,
      "player_title": 1,
      "topper": 590,
      "trail": 204,
      "wheel": 1221
    }
  }
}
```
