#!/usr/bin/env python3
"""
RL Item Swap Tool
=================
Performs client-side visual asset replacement for Rocket League items by
patching the UPK name table of the SOURCE item's package to reference the
TARGET item's assets. The game loads Source's filename but renders Target's
mesh, materials, and textures.

Supports all item categories that have UPK packages (bodies, wheels, decals,
boosts, antennas, toppers, trails, goal explosions, goal stingers, engine
audio, paint finishes, player banners, avatar borders).

Player titles have no UPK — they are patched directly in the localization table.

Usage
-----
    python3 swap_item.py <source_internal_name> <target_internal_name>
    python3 swap_item.py --restore <source_internal_name>
    python3 swap_item.py --list-backups

Environment
-----------
Set these env vars (or edit the constants below) to point to your tools:
    RL_DECRYPT=/path/to/rl_decrypt     # decrypts a UPK → {name}_dec.upk
    RL_ENCRYPT=/path/to/rl_encrypt     # re-encrypts {name}_dec.upk → {name}.upk
    RL_HEXEDIT=/path/to/hexedit        # CLI hex editor (used for manual inspection)
    RL_GAME_DIR=/path/to/CookedPCConsole
"""

import os
import re
import sys
import json
import struct
import shutil
import zlib
import math
import hashlib
import subprocess
import tempfile
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

GAME_DIR    = Path(os.getenv("RL_GAME_DIR",
              "/home/ubuntu/Games/rocketleague/TAGame/CookedPCConsole"))
BACKUP_DIR  = Path("/home/ubuntu/velrl/swap_backups")
ITEMS_JSON  = Path("/home/ubuntu/velrl/items.json")

# External tool commands (None = use built-in Python fallback)
RL_DECRYPT  = os.getenv("RL_DECRYPT")   # e.g. "rl_upktool --decrypt"
RL_ENCRYPT  = os.getenv("RL_ENCRYPT")   # e.g. "rl_upktool --encrypt"
RL_HEXEDIT  = os.getenv("RL_HEXEDIT")   # e.g. "hexedit" (for --inspect mode)

# AES key for built-in decrypt/re-encrypt fallback
AES_KEY_HEX = "c7df6b13252acc7147bb51c98ad7e34b7fe500b77fa5fab293e2f24e6b17e779"

# UE3 magic
UE3_TAG = 0x9E2A83C1

# Localization UPKs that contain player title strings (checked in order)
TITLE_UPKS = ["TAGame_INT_SF.upk", "Core_INT_SF.upk", "UI_INT_SF.upk"]

# ── Item lookup ────────────────────────────────────────────────────────────────

def load_items() -> dict[str, dict]:
    """Return mapping of internal_name (lower) → item dict from items.json."""
    data = json.loads(ITEMS_JSON.read_text())
    return {i["internal_name"].lower(): i for i in data["items"] if i.get("internal_name")}


def find_upk(internal_name: str) -> Path | None:
    """Locate the primary UPK package for an item in the game directory."""
    stem_lower = internal_name.lower()
    for f in GAME_DIR.iterdir():
        if not f.suffix == ".upk":
            continue
        fstem = f.stem.lower()
        # Body_Octane_SF.upk  →  stem_lower = "body_octane"
        # Also handle _T_SF (thumbnail) — we want the main package, not the thumbnail
        fstem_no_sf = re.sub(r"_sf$", "", fstem)
        fstem_no_tsf = re.sub(r"_t_sf$|_t$", "", fstem_no_sf)
        if fstem_no_sf == stem_lower or fstem_no_tsf == stem_lower:
            # Prefer the non-thumbnail _SF variant
            if "_t_sf" not in f.stem.lower():
                return f
    return None


def find_upk_by_item(item: dict) -> Path | None:
    """Find the UPK for an item using internal_name, then fallback to thumbnail_asset stem."""
    upk = find_upk(item["internal_name"])
    if upk:
        return upk
    # Try via thumbnail_asset stem (e.g. "wheel_soccerball" → "wheel_soccerball_SF.upk")
    asset = (item.get("thumbnail_asset") or "").strip().lower()
    if asset:
        stem = re.sub(r"_t$", "", asset)
        for f in GAME_DIR.iterdir():
            if f.suffix == ".upk" and re.sub(r"_sf$", "", f.stem.lower()) == stem:
                return f
    return None

# ── UPK binary parsing ─────────────────────────────────────────────────────────

def _read_fstring(data: bytes, pos: int) -> tuple[str, int]:
    """Read a UE3 FString (int32 len + chars). Returns (string, new_pos)."""
    slen = struct.unpack_from("<i", data, pos)[0]; pos += 4
    if slen == 0:
        return ("", pos)
    if slen < 0:
        byte_len = (-slen) * 2
        s = data[pos:pos + byte_len - 2].decode("utf-16-le", errors="replace")
        pos += byte_len
    else:
        s = data[pos:pos + slen - 1].decode("ascii", errors="replace")
        pos += slen
    return (s, pos)


def parse_upk_summary(data: bytes) -> dict:
    """Parse FPackageFileSummary from a (possibly partially-readable) UPK."""
    pos = 0
    tag = struct.unpack_from("<I", data, 0)[0]
    if tag != UE3_TAG:
        raise ValueError(f"Not a UE3 package (tag=0x{tag:08X})")
    pos = 4
    fver, lver = struct.unpack_from("<HH", data, pos); pos += 4
    total_header = struct.unpack_from("<I", data, pos)[0]; pos += 4
    folder, pos = _read_fstring(data, pos)
    pkg_flags = struct.unpack_from("<I", data, pos)[0]; pos += 4
    name_count, name_offset = struct.unpack_from("<ii", data, pos); pos += 8
    export_count, export_offset = struct.unpack_from("<ii", data, pos); pos += 8
    import_count, import_offset = struct.unpack_from("<ii", data, pos); pos += 8
    return {
        "fver": fver, "lver": lver,
        "total_header": total_header,
        "pkg_flags": pkg_flags,
        "name_count": name_count, "name_offset": name_offset,
        "export_count": export_count, "export_offset": export_offset,
        "import_count": import_count, "import_offset": import_offset,
    }


def parse_name_table(data: bytes, summary: dict) -> list[dict]:
    """
    Parse the UE3 FNameEntry array.
    Returns list of dicts: {offset, string, raw, flags}
    Works on both decrypted and decompressed data.
    Uses 8-byte flags (UE3 >= v855).
    """
    names = []
    p = summary["name_offset"]
    for _ in range(summary["name_count"]):
        entry_start = p
        slen = struct.unpack_from("<i", data, p)[0]; p += 4
        if slen < 0:                          # UTF-16
            byte_len = (-slen) * 2
            s = data[p:p + byte_len - 2].decode("utf-16-le", errors="replace")
            p += byte_len
            encoding = "utf16"
        elif slen > 0:
            s = data[p:p + slen - 1].decode("ascii", errors="replace")
            p += slen
            encoding = "ascii"
        else:
            s = ""
            encoding = "ascii"
        flags = struct.unpack_from("<Q", data, p)[0]; p += 8
        names.append({
            "offset": entry_start,
            "string": s,
            "flags": flags,
            "encoding": encoding,
            "raw": bytes(data[entry_start:p]),
        })
    return names


def _encode_name_entry(s: str, flags: int, encoding: str) -> bytes:
    """Serialize a single FNameEntry back to bytes."""
    if encoding == "utf16":
        body = (s + "\x00").encode("utf-16-le")
        slen = -(len(s) + 1)
        return struct.pack("<i", slen) + body + struct.pack("<Q", flags)
    else:
        body = (s + "\x00").encode("ascii", errors="replace")
        slen = len(body)
        return struct.pack("<i", slen) + body + struct.pack("<Q", flags)


def substitute_names(
    data: bytes,
    names: list[dict],
    replacements: dict[str, str],
    summary: dict,
) -> bytes:
    """
    Replace strings in the name table.
    replacements: {old_string: new_string}
    Handles entries of different lengths by recalculating all offsets that
    follow the modified entry in the file.
    Returns patched data as bytes.
    """
    buf = bytearray(data)
    # Work from last entry to first so offsets don't shift as we go
    offset_delta = 0
    for entry in names:
        if entry["string"] not in replacements:
            continue
        old_raw = entry["raw"]
        new_str = replacements[entry["string"]]
        new_raw = _encode_name_entry(new_str, entry["flags"], entry["encoding"])
        delta = len(new_raw) - len(old_raw)
        pos = entry["offset"] + offset_delta

        buf[pos:pos + len(old_raw)] = new_raw
        if delta != 0:
            # Shift remaining bytes and update all header offsets that point
            # past this insertion point
            buf = buf[:pos + len(new_raw)] + buf[pos + len(old_raw):]
            _patch_offsets(buf, pos + len(new_raw), delta, summary)
            offset_delta += delta
    return bytes(buf)


def _patch_offsets(buf: bytearray, insertion_point: int, delta: int, summary: dict):
    """Increment every 4-byte file offset in the summary header that points
    past insertion_point by delta."""
    OFFSET_POSITIONS = [
        # (field_offset_in_file, field_name)  — standard positions in FPackageFileSummary
        (29, "name_offset"),
        (37, "export_offset"),
        (45, "import_offset"),
        (49, "depends_offset"),
    ]
    for field_pos, _ in OFFSET_POSITIONS:
        val = struct.unpack_from("<i", buf, field_pos)[0]
        if val > insertion_point:
            struct.pack_into("<i", buf, field_pos, val + delta)

# ── Compression helpers ────────────────────────────────────────────────────────

def _decompress_upk_chunks(data: bytes, chunk_file_offset: int) -> bytes:
    """
    Decompress UE3 compressed chunk data starting at chunk_file_offset.
    Format: sig(4) block_size(4) total_comp(4) total_uncomp(4)
            [comp_sz(4) uncomp_sz(4)] × n_blocks
            [compressed_data] × n_blocks
    """
    pos = chunk_file_offset
    sig, block_size, comp_total, uncomp_total = struct.unpack_from("<IIII", data, pos)
    if sig != UE3_TAG:
        raise ValueError(f"Bad chunk sig 0x{sig:08X}")
    n_blocks = math.ceil(uncomp_total / block_size)
    pos += 16
    sub_headers = []
    for _ in range(n_blocks):
        c_sz, u_sz = struct.unpack_from("<II", data, pos); pos += 8
        sub_headers.append((c_sz, u_sz))
    out = bytearray()
    for c_sz, u_sz in sub_headers:
        out += zlib.decompress(data[pos:pos + c_sz])
        pos += c_sz
    return bytes(out)


def _recompress_upk_chunks(decompressed: bytes, block_size: int = 131072) -> bytes:
    """Re-compress decompressed data into UE3 chunk format."""
    n_blocks = math.ceil(len(decompressed) / block_size)
    blocks = []
    for i in range(n_blocks):
        chunk = decompressed[i * block_size:(i + 1) * block_size]
        blocks.append(zlib.compress(chunk, 6))

    comp_total = sum(len(b) for b in blocks)
    uncomp_total = len(decompressed)
    header = struct.pack("<IIII", UE3_TAG, block_size, comp_total, uncomp_total)
    sub_hdrs = b"".join(struct.pack("<II", len(b), min(block_size, uncomp_total - i * block_size))
                        for i, b in enumerate(blocks))
    return header + sub_hdrs + b"".join(blocks)

# ── AES decrypt/encrypt (built-in fallback) ────────────────────────────────────

def _aes_ecb(data: bytes, encrypt: bool = False) -> bytes:
    """AES-256-ECB encrypt or decrypt. Pads to 16-byte boundary."""
    from Crypto.Cipher import AES
    key = bytes.fromhex(AES_KEY_HEX)
    cipher = AES.new(key, AES.MODE_ECB)
    pad = (-len(data)) % 16
    padded = data + b"\x00" * pad
    result = bytearray()
    for i in range(0, len(padded), 16):
        block = padded[i:i + 16]
        result += cipher.encrypt(block) if encrypt else cipher.decrypt(block)
    return bytes(result[:len(data)])

# ── External tool wrappers ─────────────────────────────────────────────────────

def decrypt_file(src: Path, dst: Path):
    """Decrypt a RL UPK file to dst using the configured tool."""
    if RL_DECRYPT:
        cmd = RL_DECRYPT.split() + [str(src), str(dst)]
        subprocess.run(cmd, check=True)
    else:
        # Built-in: probe if file has valid plaintext header, then decompress
        # the object chunk if present. If AES-encrypted header, attempt AES-ECB.
        _builtin_decrypt(src, dst)


def _builtin_decrypt(src: Path, dst: Path):
    """
    Best-effort built-in decrypt:
    1. Read raw file
    2. Verify UE3 tag at byte 0
    3. Decompress the object-data chunk at TotalHeaderSize into a flat buffer
    4. Write the flat (decompressed) file

    If a proper decrypt tool is provided via RL_DECRYPT, that takes precedence.
    """
    raw = src.read_bytes()
    tag = struct.unpack_from("<I", raw, 0)[0]
    if tag != UE3_TAG:
        raise ValueError(f"{src.name}: not a valid UE3 package (tag=0x{tag:08X})")

    summary = parse_upk_summary(raw)
    total_header = summary["total_header"]

    # Check whether there is a compressed chunk at TotalHeaderSize
    if total_header < len(raw) - 16:
        probe = struct.unpack_from("<I", raw, total_header)[0]
        if probe == UE3_TAG:
            decomp = _decompress_upk_chunks(raw, total_header)
            # Write: raw header prefix + decompressed object data
            dst.write_bytes(raw[:total_header] + decomp)
            return

    # No compression found — copy as-is
    shutil.copy2(src, dst)


def encrypt_file(src: Path, dst: Path):
    """Re-encrypt a modified UPK."""
    if RL_ENCRYPT:
        cmd = RL_ENCRYPT.split() + [str(src), str(dst)]
        subprocess.run(cmd, check=True)
    else:
        # Built-in: re-compress the object data portion and write
        _builtin_encrypt(src, dst)


def _builtin_encrypt(src: Path, dst: Path):
    """
    Re-compress the object-data portion of a decompressed UPK.
    Inverse of _builtin_decrypt.
    """
    data = src.read_bytes()
    summary = parse_upk_summary(data)
    total_header = summary["total_header"]

    # Compress everything after the header
    obj_data = data[total_header:]
    if obj_data:
        compressed = _recompress_upk_chunks(obj_data)
        dst.write_bytes(data[:total_header] + compressed)
    else:
        shutil.copy2(src, dst)

# ── Name table patching pipeline ───────────────────────────────────────────────

def build_name_replacements(src_item: dict, tgt_item: dict) -> dict[str, str]:
    """
    Build the {old_name: new_name} substitution map for the name table.
    We swap target's internal name(s) to match source's so the game finds
    the right objects by the names it expects.
    """
    reps: dict[str, str] = {}
    src_name = src_item["internal_name"]   # e.g. Body_Octane
    tgt_name = tgt_item["internal_name"]   # e.g. Body_Fennec

    # Direct match: "Body_Fennec" → "Body_Octane"
    reps[tgt_name] = src_name

    # Handle prefixed variants: "Body_Fennec_Mesh", "Body_Fennec_Material0" etc.
    tgt_low = tgt_name.lower()
    src_low = src_name.lower()
    # We'll do prefix substitution at parse time for any name starting with tgt_name
    reps["__prefix_sub__"] = (tgt_low, src_low)

    return reps


def apply_prefix_subs(names: list[dict], tgt_low: str, src_low: str) -> dict[str, str]:
    """
    For each name that starts with tgt_low (case-insensitive), generate a
    replacement mapping that substitutes the prefix.
    """
    reps: dict[str, str] = {}
    for entry in names:
        s = entry["string"]
        if s.lower().startswith(tgt_low) and s.lower() != tgt_low:
            new_s = src_low + s[len(tgt_low):]
            # Preserve original casing of suffix
            suffix = s[len(tgt_low):]
            src_cased = src_low[0].upper() + src_low[1:]  # rough Title-case
            # Use actual source name as prefix, preserve suffix casing
            src_item_name = src_low  # will be overridden by caller
            reps[s] = s  # placeholder; overridden below
    return reps


def patch_upk_names(src_path: Path, tgt_path: Path, src_item: dict, tgt_item: dict) -> bytes:
    """
    Full pipeline: decrypt → parse → patch names → re-encrypt.
    Returns the patched bytes ready to write to disk.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        src_dec = tmp / "src_dec.upk"
        tgt_dec = tmp / "tgt_dec.upk"

        print(f"  [decrypt] {tgt_path.name}")
        decrypt_file(tgt_path, tgt_dec)

        dec_data = tgt_dec.read_bytes()
        summary = parse_upk_summary(dec_data)

        print(f"  [parse]   name_count={summary['name_count']} name_offset={summary['name_offset']}")
        names = parse_name_table(dec_data, summary)

        src_iname = src_item["internal_name"]
        tgt_iname = tgt_item["internal_name"]
        tgt_low = tgt_iname.lower()
        src_low = src_iname.lower()

        # Build full replacement map
        reps: dict[str, str] = {}
        for entry in names:
            s = entry["string"]
            s_low = s.lower()
            if s_low == tgt_low:
                reps[s] = src_iname
            elif s_low.startswith(tgt_low + "_") or s_low.startswith(tgt_low + "."):
                reps[s] = src_iname + s[len(tgt_iname):]

        if not reps:
            print(f"  [warn]    No names matching '{tgt_iname}' found in name table")
            print(f"            (names present: {[n['string'] for n in names[:10]]})")
        else:
            print(f"  [patch]   {len(reps)} name(s) substituted:")
            for old, new in list(reps.items())[:6]:
                print(f"             '{old}' → '{new}'")

        patched = substitute_names(dec_data, names, reps, summary)

        patched_dec = tmp / "patched_dec.upk"
        patched_dec.write_bytes(patched)

        print(f"  [encrypt] writing patched package")
        out_enc = tmp / "patched.upk"
        encrypt_file(patched_dec, out_enc)

        return out_enc.read_bytes()

# ── Player title patching ──────────────────────────────────────────────────────

def find_title_string_in_upk(upk_path: Path, title_key: str) -> tuple[int, int] | None:
    """
    Scan a (decompressed) UPK for a title string and return (file_offset, length).
    Handles both ASCII and UTF-16 LE FStrings.
    """
    with tempfile.TemporaryDirectory() as td:
        dec_path = Path(td) / "dec.upk"
        decrypt_file(upk_path, dec_path)
        data = dec_path.read_bytes()

    needle_ascii = title_key.encode("ascii")
    needle_utf16 = title_key.encode("utf-16-le")
    for needle in (needle_ascii, needle_utf16):
        idx = data.find(needle)
        if idx != -1:
            return (idx, len(needle))
    return None


def swap_player_title(src_item: dict, tgt_item: dict) -> bool:
    """
    For player titles: find the title string in localization UPKs and
    replace it. Falls back to a config-file patch if no UPK is found.
    """
    src_name = src_item["name"]   # display name  e.g. "RLCS Season 1 World Champion"
    tgt_name = tgt_item["name"]
    src_key  = src_item["internal_name"]
    tgt_key  = tgt_item["internal_name"]

    print(f"  [title]  Looking for title strings in localization UPKs...")
    for upk_name in TITLE_UPKS:
        upk_path = GAME_DIR / upk_name
        if not upk_path.exists():
            continue
        result = find_title_string_in_upk(upk_path, tgt_name)
        if result is None:
            continue
        offset, length = result
        print(f"  [title]  Found '{tgt_name}' at offset {offset} in {upk_name}")
        # Backup
        _backup_file(upk_path)
        # Read, patch, write
        data = bytearray(upk_path.read_bytes())
        needle = tgt_name.encode("ascii" if len(tgt_name) == length else "utf-16-le")
        # Patch with same encoding, pad/truncate to same length
        replacement = src_name.encode("ascii" if length == len(tgt_name) else "utf-16-le")
        if len(replacement) <= len(needle):
            replacement = replacement.ljust(len(needle), b"\x00")
        else:
            replacement = replacement[:len(needle)]
        data[offset:offset + len(needle)] = replacement
        upk_path.write_bytes(bytes(data))
        print(f"  [title]  Patched '{tgt_name}' → '{src_name}' in {upk_name}")
        return True

    print(f"  [title]  Title strings not found in known localization UPKs.")
    print(f"           Manual hex-editor approach: search for '{tgt_name}' in the")
    print(f"           decrypted TAGame_INT_SF.upk and replace with '{src_name}'.")
    return False

# ── Backup / restore ───────────────────────────────────────────────────────────

def _backup_file(path: Path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    bak = BACKUP_DIR / path.name
    if not bak.exists():
        shutil.copy2(path, bak)
        print(f"  [backup] {path.name} → swap_backups/")


def restore_item(internal_name: str):
    items = load_items()
    item = items.get(internal_name.lower())
    if not item:
        print(f"ERROR: '{internal_name}' not found in items.json")
        return False

    upk = find_upk_by_item(item)
    if not upk:
        print(f"ERROR: No UPK found for '{internal_name}'")
        return False

    bak = BACKUP_DIR / upk.name
    if not bak.exists():
        print(f"ERROR: No backup found for {upk.name}")
        return False

    shutil.copy2(bak, upk)
    print(f"Restored {upk.name} from backup.")
    return True

# ── Offset inspection helper (uses hex editor) ─────────────────────────────────

def inspect_offsets(internal_name: str):
    """Launch the configured hex editor on the decrypted package for manual inspection."""
    items = load_items()
    item = items.get(internal_name.lower())
    if not item:
        print(f"ERROR: '{internal_name}' not found"); return

    upk = find_upk_by_item(item)
    if not upk:
        print(f"ERROR: No UPK for '{internal_name}'"); return

    with tempfile.NamedTemporaryFile(suffix=".upk", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    decrypt_file(upk, tmp_path)
    summary = parse_upk_summary(tmp_path.read_bytes())
    print(f"Package:      {upk.name}")
    print(f"Name table:   offset=0x{summary['name_offset']:X}  count={summary['name_count']}")
    print(f"Export table: offset=0x{summary['export_offset']:X}  count={summary['export_count']}")
    print(f"Import table: offset=0x{summary['import_offset']:X}  count={summary['import_count']}")

    if RL_HEXEDIT:
        print(f"\nLaunching {RL_HEXEDIT} at name table offset 0x{summary['name_offset']:X}...")
        subprocess.run([RL_HEXEDIT, str(tmp_path)])
    else:
        print(f"\nHex editor not configured. Set RL_HEXEDIT env var.")
        print(f"Decrypted file at: {tmp_path}")

# ── Main swap entry point ──────────────────────────────────────────────────────

def swap(src_internal: str, tgt_internal: str):
    """Swap source item to visually render as target item."""
    items = load_items()

    src_item = items.get(src_internal.lower())
    tgt_item = items.get(tgt_internal.lower())
    if not src_item:
        print(f"ERROR: source '{src_internal}' not found in items.json"); return False
    if not tgt_item:
        print(f"ERROR: target '{tgt_internal}' not found in items.json"); return False

    src_cat = src_item["category_id"]
    tgt_cat = tgt_item["category_id"]
    if src_cat != tgt_cat:
        print(f"WARNING: category mismatch — {src_cat} vs {tgt_cat}")
        print("Swapping across categories may crash the game or produce no visible change.")

    print(f"\nSwapping: [{src_cat}] {src_item['name']} (will look like) {tgt_item['name']}")
    print(f"  source internal: {src_item['internal_name']}")
    print(f"  target internal: {tgt_item['internal_name']}")

    # ── Player title (no UPK — text-only patch) ────────────────────────────────
    if src_cat == "player_title":
        return swap_player_title(src_item, tgt_item)

    # ── All other categories (UPK-based visual swap) ───────────────────────────
    src_upk = find_upk_by_item(src_item)
    tgt_upk = find_upk_by_item(tgt_item)

    if not src_upk:
        print(f"ERROR: no UPK found for source '{src_internal}' in {GAME_DIR}"); return False
    if not tgt_upk:
        print(f"ERROR: no UPK found for target '{tgt_internal}' in {GAME_DIR}"); return False

    print(f"  source UPK: {src_upk.name}  ({src_upk.stat().st_size:,} bytes)")
    print(f"  target UPK: {tgt_upk.name}  ({tgt_upk.stat().st_size:,} bytes)")

    # Backup the source BEFORE we modify it
    _backup_file(src_upk)

    # Produce a patched copy of tgt_upk with src's names
    patched_bytes = patch_upk_names(src_upk, tgt_upk, src_item, tgt_item)

    # Write patched bytes to src's file location (replacing it)
    src_upk.write_bytes(patched_bytes)
    md5 = hashlib.md5(patched_bytes).hexdigest()[:8]
    print(f"  [done]    {src_upk.name} patched ({len(patched_bytes):,} bytes, md5={md5})")
    print(f"\nResult: In-game, '{src_item['name']}' will render as '{tgt_item['name']}'")
    print(f"        Restart the game to apply.")
    return True

# ── CLI ─────────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    if args[0] == "--list-backups":
        if not BACKUP_DIR.exists():
            print("No backups found.")
        else:
            for f in sorted(BACKUP_DIR.iterdir()):
                print(f"  {f.name}  ({f.stat().st_size:,} bytes)")
        return

    if args[0] == "--restore":
        if len(args) < 2:
            print("Usage: swap_item.py --restore <internal_name>"); return
        restore_item(args[1])
        return

    if args[0] == "--inspect":
        if len(args) < 2:
            print("Usage: swap_item.py --inspect <internal_name>"); return
        inspect_offsets(args[1])
        return

    if args[0] == "--list":
        items = load_items()
        cat_filter = args[1] if len(args) > 1 else None
        for name, item in sorted(items.items()):
            if cat_filter and item["category_id"] != cat_filter:
                continue
            upk = find_upk_by_item(item)
            upk_str = upk.name if upk else "(no UPK)"
            print(f"  {item['internal_name']:40} [{item['category_id']:15}]  {upk_str}")
        return

    if len(args) < 2:
        print("Usage: swap_item.py <source_internal_name> <target_internal_name>")
        print("       swap_item.py --restore <source_internal_name>")
        print("       swap_item.py --list [category]")
        print("       swap_item.py --inspect <internal_name>")
        return

    swap(args[0], args[1])


if __name__ == "__main__":
    main()
