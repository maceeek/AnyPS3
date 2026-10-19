"""Target identity metadata. A title name alone never verifies an input."""

import json
from pathlib import Path
import re

from .errors import AnyPS3Error


def check_profile(path: Path, sha256: str) -> dict:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise AnyPS3Error(f"Invalid profile JSON: {path}.") from exc
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise AnyPS3Error("Profile must be an object with schema_version=1.")
    if not isinstance(profile.get("title"), str) or not profile["title"].strip():
        raise AnyPS3Error("Profile requires a nonempty title.")
    expected = profile.get("executable_sha256")
    if expected is not None:
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
            raise AnyPS3Error("Profile executable_sha256 must be null or a 64-digit hex digest.")
        if expected.lower() != sha256:
            raise AnyPS3Error("Executable SHA-256 does not match the selected profile.")
    return {"title": profile["title"], "identity_verified": expected is not None,
            "note": "Hash matches the profile; compatibility is not established." if expected is not None
                    else "Profile has no executable hash; this input is not identified as the target game."}

