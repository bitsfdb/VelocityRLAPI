# CLAUDE.md — Project VelocityRL

Development instructions, architecture patterns, and operational guardrails for reverse-engineering the Rocket League asset ecosystem and internal network endpoints.

## Project Scope
1. **Asset Mapping Engine:** Reverse-engineering the `.upk` (Unreal Engine 3) package system and localization layer to programmatically resolve internal asset code names (e.g., `Body_Grain`, `wheel_SoccarBall_SF`) to their official English display names (e.g., "Fennec", "Cristiano Wheels").
2. **API Protocol Analysis:** Auditing the network communication between the game client and backend services to document the structure, payload schema, and authentication mechanics of the internal in-game item store.

## Technical Environment & Architecture
- **Environment:** Headless Linux Server (Ubuntu) running game files via the `Legendary` CLI client.
- **Backend Stack:** Python 3 (FastAPI, PyCryptodome, Cryptography).
- **Design Pattern:** **Sidecar Architecture**. A background worker handles heavy-lifting file operations and disk caching (`items.json`), while the API layer serves data atomically from memory/cache with zero-downtime hot reloads.
- **Strict Dependency Rules:** Global encryption libraries must use `cryptography` and `pycryptodome`. Absolute rejection of the legacy, obsolete PyPI `crypto` package.

## Development & Code Style Guideline
- **Explicit and Monolithic Snippets:** Scripts should avoid artificial multi-file imports where a unified standalone script reduces friction on a headless server.
- **Binary Accuracy:** When manipulating `.upk` files, byte alignment, header offset patching (`depends_offset`, `name_offset`), and `FName` table validation must be handled precisely to preserve structural integrity.
- **Robust Fallbacks:** When mapping assets, if a code name cannot be resolved via local localization files or the community API cache, fall back gracefully to the formatted internal string and flag it as `untranslated`.
- **Tools Avaliable:** You can use tools such as Google to search any pre-made repository or feedback or help regarding any question i give you.
