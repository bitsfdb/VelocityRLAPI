# VelocityRL API

**[Full documentation available in the GitHub Wiki](https://github.com/bitsfdb/VelocityRLAPI/wiki)**

Welcome to the **VelocityRL API** developer documentation. This is a high-performance, developer-centric, secure API designed for querying, searching, and managing Rocket League products and item metadata parsed directly from localized game files.

> This API is used by the **[VelocityRL Repository](https://github.com/bitsfdb/VelocityRLAPI)** to power item metadata lookups and localized name resolution.

## Architecture

```mermaid
graph TD
    UPK[Rocket League .upk Files] -->|Parser| Extractor[extract_items.py]
    Psynet[Psynet Web Cache] -->|Metadata| Extractor
    Extractor -->|Dumps JSON| DB[items.json]
    Watcher[watcher.py] -->|Polls updates| Extractor
    DB -->|Atomic Thread-Safe Load| API[api.py FastAPI]
    API -->|Serves over HTTPS| Client[API Client]
```

---

## Overview

The API serves as a secure, fast, and unified source of truth for Rocket League items, translating complex internal asset names (e.g., `wheel_SoccarBall_SF`) into clean, localized product names across 12 distinct game-supported languages.

### Core Features

* **High Performance**: Serving data atomically from memory using a fast, hot-reloading sidecar design.
* **Localization Out-of-the-Box**: Built-in support for both standard ISO and internal Psyonix 3-letter language codes.
* **Standard-Compliant Paginated Search**: Seamless item querying with `category`, `search`, `limit`, and `offset` support.
* **IP-based Rate Limiting**: Clean, rolling-window rate-limiting protecting endpoints with standard headers.
* **Production-Grade HTTPS**: Secured with automated Let's Encrypt SSL certificates behind an Nginx reverse proxy.

---

## Quick Start

The public API is hosted securely at `api.velocityrl.tech`.

### Fetching Cristiano Wheels (Spanish translation, limit 1)

```bash
curl -s "https://api.velocityrl.tech/v2/rl/products?lang=es&category=wheel&search=cristiano&limit=1"
```

#### Response Payload

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
      "internal_name": "Wheel_SoccerBall",
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

## Base Path & Endpoints

All requests should be routed over `HTTPS` to:
`https://api.velocityrl.tech`

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/v2/rl/products` | `GET` | Paginated and filtered search of Rocket League items. |
| `/v2/rl/products/{product_id}` | `GET` | Retrieve detailed metadata for a single item by its ID. |
| `/v2/rl/categories` | `GET` | List all available item categories along with counts. |
| `/v2/rl/meta` | `GET` | Retrieve API metadata, dump fingerprints, and database stats. |
| `/v2/rl/refresh` | `POST` | Force the sidecar parser to regenerate item database from source UPKs. |
