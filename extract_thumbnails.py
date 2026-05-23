"""
Batch-extract item thumbnails from Rocket League _T_SF.upk files using umodel.

Outputs: thumbnails/{asset_stem}.png  (e.g. body_grain_t.png for the Fennec)
"""

import subprocess
import shutil
import tempfile
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

UMODEL       = Path("/tmp/UEViewer/umodel")
GAME_DIR     = Path("/home/ubuntu/Games/rocketleague/TAGame/CookedPCConsole")
THUMBNAILS   = Path("/home/ubuntu/velrl/thumbnails")
WORKERS      = 12
TIMEOUT      = 30


def extract_one(pkg: Path, tmp_root: Path) -> tuple[str, str]:
    stem = pkg.stem                           # body_Grain_T_SF
    base = stem[:-3] if stem.endswith("_SF") else stem  # body_Grain_T
    out_png = THUMBNAILS / (base.lower() + ".png")

    if out_png.exists():
        return (pkg.name, "skip")

    worker_tmp = tmp_root / stem
    worker_tmp.mkdir(exist_ok=True)

    try:
        subprocess.run(
            [str(UMODEL),
             f"-path={GAME_DIR}",
             "-game=rocketleague",
             "-export", "-png",
             f"-out={worker_tmp}",
             pkg.name],
            capture_output=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (pkg.name, "timeout")
    except Exception as e:
        return (pkg.name, f"error:{e}")

    texture_dir = worker_tmp / stem / "Texture2D"
    if not texture_dir.exists():
        shutil.rmtree(worker_tmp, ignore_errors=True)
        return (pkg.name, "no_texture_dir")

    # Pick the *Thumbnail.png (skip ColorMaskBackground, PaintedThumbnail, etc.)
    candidates = [f for f in texture_dir.iterdir()
                  if f.suffix == ".png" and "Thumbnail" in f.name
                  and "ColorMask" not in f.name and "Painted" not in f.name]

    if not candidates:
        shutil.rmtree(worker_tmp, ignore_errors=True)
        return (pkg.name, "no_thumb")

    # If multiple, prefer the one matching {base}Thumbnail
    target = next((c for c in candidates
                   if c.stem.lower() == base.lower() + "thumbnail"), candidates[0])

    shutil.copy2(target, out_png)
    shutil.rmtree(worker_tmp, ignore_errors=True)
    return (pkg.name, "ok")


def run(force: bool = False):
    if not UMODEL.exists():
        print(f"[thumb] umodel not found at {UMODEL} — build it first")
        sys.exit(1)

    THUMBNAILS.mkdir(exist_ok=True)

    pkgs = sorted(GAME_DIR.glob("*_T_SF.upk"))
    if not pkgs:
        print("[thumb] No _T_SF.upk files found")
        return

    if force:
        # wipe existing so everything re-extracts
        for f in THUMBNAILS.glob("*.png"):
            f.unlink()

    already = sum(1 for p in pkgs
                  if (THUMBNAILS / (p.stem[:-3].lower() + ".png")).exists())
    todo = len(pkgs) - already
    print(f"[thumb] {len(pkgs)} packages — {already} already extracted, {todo} to do")

    if todo == 0:
        print("[thumb] Nothing to do.")
        return

    ok = skip = fail = 0
    with tempfile.TemporaryDirectory(prefix="umodel_") as tmp_root:
        tmp_root = Path(tmp_root)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(extract_one, p, tmp_root): p for p in pkgs}
            done = 0
            for fut in as_completed(futures):
                name, status = fut.result()
                done += 1
                if status == "ok":
                    ok += 1
                elif status == "skip":
                    skip += 1
                else:
                    fail += 1
                if done % 500 == 0 or done == len(pkgs):
                    print(f"  [{done}/{len(pkgs)}] ok={ok} skip={skip} fail={fail}",
                          flush=True)

    print(f"[thumb] Done — {ok} extracted, {skip} skipped, {fail} failed")
    print(f"[thumb] Thumbnails at {THUMBNAILS}/")


if __name__ == "__main__":
    force = "--force" in sys.argv
    run(force=force)
