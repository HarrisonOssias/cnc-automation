"""Guess a part's details from a STEP file name, using the parts already in the database.

Used by the office app's "Auto-fill from STEP" button (GET /parts/suggest). Pure functions, no I/O.
"""

import re
from difflib import SequenceMatcher
from pathlib import PurePath

MATCH_THRESHOLD = 0.6


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def part_number_from(stem: str) -> str:
    """'bracket_revB' -> 'BRACKET'. Drops a trailing revision marker, then joins words with dashes."""
    stem = re.sub(r"[\s_.-]+(rev[\s_-]?\w{1,3}|v\d+)$", "", stem, flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").upper()


def suggest_part(filename: str, parts: list[dict]) -> dict:
    """Best existing part for this file, or a new part number taken from the file name.

    `parts` items: {"part_number", "material", "tools", "files": [filename, ...]}.
    Scores: the part number appears in the file name (1.0) > a file with the same name is already
    on the part (0.9) > the names are similar (up to 0.8). Below MATCH_THRESHOLD it's a new part.
    """
    key = _norm(PurePath(filename).stem)
    best: tuple[float, dict, str] | None = None
    for part in parts:
        number = _norm(part["part_number"])
        stems = {_norm(PurePath(f).stem) for f in part["files"]} - {""}
        if number and number in key:
            candidate = (1.0, part, f"part number {part['part_number']} is in the file name")
        elif key in stems:
            candidate = (0.9, part, f"a file with this name is already on {part['part_number']}")
        else:
            ratio = max(SequenceMatcher(None, key, s).ratio() for s in stems | {number})
            candidate = (round(ratio * 0.8, 2), part, f"the file name is close to {part['part_number']}'s files")
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best and best[0] >= MATCH_THRESHOLD:
        score, part, reason = best
        return {
            "match": True, "score": score, "reason": reason, "part_number": part["part_number"],
            "material": part["material"], "tools": part["tools"],
        }
    # New part: number from the file name, material if the name mentions one we already use.
    material = next((p["material"] for p in parts if _norm(p["material"]) and _norm(p["material"]) in key), "")
    return {
        "match": False, "score": best[0] if best else 0.0,
        "reason": "no existing part matches; the part number comes from the file name",
        "part_number": part_number_from(PurePath(filename).stem), "material": material, "tools": [],
    }
