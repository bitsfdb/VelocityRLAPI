"""
Watches for Rocket League game updates (via legendary).
Regenerates items.json whenever the game version changes.
"""

import json
import time
import logging
from pathlib import Path
from extract_items import generate, get_game_version, OUTPUT_FILE

STATE_FILE = Path("/home/ubuntu/velrl/.cache/watcher_state.json")
POLL_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"game_version": None}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_and_refresh(state: dict) -> dict:
    game_version = get_game_version()
    game_changed = game_version != state.get("game_version")

    if game_changed:
        log.info(
            "Game version changed (%s → %s) — regenerating items.json",
            state.get("game_version"), game_version,
        )
        generate(OUTPUT_FILE)
        state = {"game_version": game_version}
        save_state(state)
    else:
        log.debug("No change (game=%s)", game_version)

    return state


def run():
    log.info("Starting watcher (poll interval: %ds)", POLL_INTERVAL)

    if not OUTPUT_FILE.exists():
        log.info("items.json missing — generating now")
        generate(OUTPUT_FILE)

    state = load_state()
    state = check_and_refresh(state)

    while True:
        time.sleep(POLL_INTERVAL)
        state = check_and_refresh(state)


if __name__ == "__main__":
    run()
