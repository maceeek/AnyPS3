"""Explicit key setup. No PS3 key material is shipped with the manager."""

import hashlib
import json
from pathlib import Path
import re
import struct
import urllib.error
import urllib.request

from .storage import ManagerError, atomic_json, identifier, read_json, sha256

RPCS3_COMMIT = "ffba2bf09ef3980638f9327b25240d81414034ab"
RPCS3_BASE = f"https://raw.githubusercontent.com/RPCS3/rpcs3/{RPCS3_COMMIT}/rpcs3/Crypto/"
RPCS3_HASHES = {
    "key_vault.h": "d5d0cede74c7900bb23ca8d60066194648ed4b976eaee60315c0bcbed7dc2aaf",
    "key_vault.cpp": "a2947ff6b681258c198552ec6344e16685e2df649018206daa3f41a0a1aadbd7",
}
LIMIT = 512 * 1024 * 1024
FORMATS = {
    "pup": '{"erk":"<64 hex digits>","riv":"<32 hex digits>","pup_hmac":"<128 hex digits>"}',
    "app": '{"type":4,"revision":1,"erk":"<64 hex digits>","riv":"<32 hex digits>"}',
}


def guidance(kind):
    purpose = "firmware update PS3UPDAT.PUP" if kind == "pup" else "game EBOOT.BIN SELF"
    return (f"The {kind.upper()} key decrypts the {purpose}. PS3 system keys are the same for every user, "
            "not per-game; APP selects the revision in the SELF header (1 for Ratchet). "
            f"JSON format: {FORMATS[kind]}. Obtain keys from your own console or published sources such as "
            "RPCS3 rpcs3/Crypto/key_vault.{h,cpp}. Choose a JSON file or paste hex values in the import wizard, "
            f"or run 'anyps3-manager keys set {kind} /path/to/key.json' "
            "(add --iso /path/to/disc.iso to validate). "
            "Alternatively explicitly choose Download from RPCS3 or run 'anyps3-manager keys fetch-rpcs3'.")


def invalid(kind, reason):
    return ManagerError("invalid-key", f"{reason} {guidance(kind)}", 6)


def format_key(kind, spec):
    if kind not in FORMATS:
        raise ManagerError("usage", "Key kind must be pup or app.", 2)
    if not isinstance(spec, dict):
        raise invalid(kind, "Expected a JSON object.")
    result = {}
    lengths = [("erk", 64), ("riv", 32)] + ([("pup_hmac", 128)] if kind == "pup" else [])
    for name, length in lengths:
        value = spec.get(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{%d}" % length, value):
            raise invalid(kind, f"Field {name} requires exactly {length} hex digits (no prefix or spaces).")
        result[name] = value.lower()
    if kind == "app":
        if type(spec.get("type")) is not int or spec["type"] != 4:
            raise invalid(kind, "Only APP type 4 is supported.")
        if type(spec.get("revision")) is not int or not 0 <= spec["revision"] < 0x8000:
            raise invalid(kind, "APP revision must be an integer from 0 to 32767 matching EBOOT.BIN.")
        result.update(type=4, revision=spec["revision"])
    return result


def read_key(kind, path):
    """Return (raw bytes, formatted key); callers that register a file hash these exact bytes."""
    try:
        with Path(path).expanduser().open("rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError()
        return data, format_key(kind, json.loads(data))
    except (OSError, ValueError, UnicodeError):
        # JSONDecodeError includes input fragments. Never surface those.
        raise invalid(kind, "Cannot read key JSON; choose a readable UTF-8 JSON object.") from None


def load_key(kind, path):
    return read_key(kind, path)[1]


def read_target(path):
    with Path(path).expanduser().open("rb") as stream:
        if Path(path).expanduser().stat().st_size > LIMIT:
            raise ValueError("Target exceeds the supported 512 MiB size limit.")
        return stream.read(LIMIT + 1)


def targets(*, iso=None, pup=None, self_path=None):
    """Read selected files without extracting a disc or writing any game data."""
    result = {}
    try:
        if iso:
            from anyps3.disc import inventory
            path = Path(iso).expanduser()
            info = inventory(path)
            wanted = {"PS3_GAME/USRDIR/EBOOT.BIN": "app"}
            if not pup:
                wanted["PS3_UPDATE/PS3UPDAT.PUP"] = "pup"
            if self_path:
                wanted.pop("PS3_GAME/USRDIR/EBOOT.BIN")
            with path.open("rb") as stream:
                for entry in info["entries"]:
                    kind = wanted.get(entry["path"].upper())
                    if not kind:
                        continue
                    if entry["directory"] or not entry["available"] or entry["size"] > LIMIT:
                        raise ValueError("Selected ISO contains an unavailable or oversized key validation target.")
                    chunks = []
                    for extent in entry["extents"]:
                        stream.seek(extent["lba"] * 2048)
                        chunk = stream.read(extent["size"])
                        if len(chunk) != extent["size"]:
                            raise ValueError("Selected ISO changed or is truncated.")
                        chunks.append(chunk)
                    result[kind] = b"".join(chunks)
            if not self_path and "app" not in result:
                raise ValueError("Selected ISO has no EBOOT.BIN SELF to validate.")
        if pup:
            result["pup"] = read_target(pup)
        if self_path:
            result["app"] = read_target(self_path)
        return result
    except Exception:
        raise ManagerError("key-target", "Cannot read the selected ISO/PUP/SELF for key validation. "
                           "Choose a complete readable file; no keys were saved.", 6) from None


def self_identity(data):
    from anyps3.self_file import span
    magic, version, revision, kind, _, _, _ = struct.unpack(">IIHHIQQ", span(data, 0, 32, "SCE header"))
    if magic != 0x53434500 or version != 2 or kind != 1 or revision & 0x8000:
        raise ValueError("Expected retail version 2 SELF.")
    appoff = struct.unpack_from(">Q", span(data, 32, 80, "SELF extended header"), 8)[0]
    app_type = struct.unpack_from(">I", span(data, appoff, 32, "APP header"), 12)[0]
    if app_type != 4:
        raise ValueError("Only APP SELF type 4 is supported.")
    return app_type, revision


def validate_self(data, spec):
    """Check root-key padding and the decrypted metadata structure, without extracting ELF."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from anyps3.self_file import span
    if self_identity(data) != (spec["type"], spec["revision"]):
        raise ValueError("Key revision/type differs from EBOOT.BIN.")
    _, _, _, _, meta, header, _ = struct.unpack_from(">IIHHIQQ", data)
    start = 32 + meta
    if not start + 96 <= header <= len(data):
        raise ValueError("Invalid SELF metadata size.")

    def decrypt(payload, key, mode):
        worker = Cipher(algorithms.AES(key), mode).decryptor()
        return worker.update(payload) + worker.finalize()

    info = decrypt(span(data, start, 64, "Metadata info"), bytes.fromhex(spec["erk"]),
                   modes.CBC(bytes.fromhex(spec["riv"])))
    if info[16:32] != bytes(16) or info[48:64] != bytes(16):
        raise ValueError("SELF metadata padding failed.")
    metadata = decrypt(data[start + 64:header], info[:16], modes.CTR(info[32:48]))
    _, _, count, key_count, _, _, _ = struct.unpack(">Q6I", span(metadata, 0, 32, "Metadata header"))
    if not 0 < count <= 256:
        raise ValueError("Invalid SELF section count.")
    table = span(metadata, 32, count * 48, "Section table")
    keys = span(metadata, 32 + count * 48, key_count * 16, "Section keys")
    for i in range(count):
        offset, size, _, _, hashed, hi, enc, ki, vi, compression = struct.unpack_from(">QQ8I", table, i * 48)
        if offset < header or offset + size > len(data) or enc not in (1, 3) or compression not in (1, 2):
            raise ValueError("Invalid SELF metadata section.")
        if hashed == 2:
            span(keys, hi * 16, 96, "Section HMAC")
        if enc == 3:
            span(keys, ki * 16, 16, "Section key")
            span(keys, vi * 16, 16, "Section IV")


def validate(kind, spec, selected):
    data = selected.get(kind)
    if data is None:
        return "Format only; no PUP selected." if kind == "pup" else "Format only; no EBOOT.BIN SELF selected."
    try:
        if kind == "app":
            validate_self(data, spec)
            return "EBOOT.BIN SELF metadata decrypted and structure validated (not an ECDSA check)."
        from anyps3.firmware import pup_entries, tar_files, package_sections
        entries = pup_entries(data, bytes.fromhex(spec["pup_hmac"]))
        if 0x100 not in entries or 0x300 not in entries:
            raise ValueError("Missing PUP version/update archive.")
        packages = tar_files(entries[0x300])
        package = next(value for name, value in packages.items() if name.startswith("dev_flash_"))
        package_sections(package, bytes.fromhex(spec["erk"]), bytes.fromhex(spec["riv"]))
        return "PUP entry HMACs and encrypted firmware package validated (not an ECDSA check)."
    except ImportError:
        raise ManagerError("key-dependency", "Key validation requires cryptography; install AnyPS3 with the [self] extra and retry.", 6) from None
    except Exception:
        raise invalid(kind, "Key validation failed: wrong key/revision, unsupported structure, or damaged target. "
                      "Check the key source and selected file, then enter the key again; no keys were saved.") from None


def key_path(lib, kind):
    record = read_json(lib.root/"keys.json", {}).get(kind)
    if not record or not Path(record["path"]).is_file():
        raise ManagerError("missing-key", f"No usable {kind.upper()} key file is configured. {guidance(kind)}", 6)
    if sha256(record["path"]) != record["sha256"]:
        raise ManagerError("key-changed", f"The {kind.upper()} key file changed; register it again. {guidance(kind)}", 6)
    load_key(kind, record["path"])
    return record["path"]


def store(lib, specs, checks, sources):
    """Publish one metadata pointer after all inputs have passed validation."""
    directory = lib.root/"private-keys"
    if directory.is_symlink():
        raise ManagerError("key-store", "Private key store must be a real directory inside the library; remove the symlink and retry.", 6)
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    written = []
    try:
        with lib.metadata():
            records = read_json(lib.root/"keys.json", {})
            for kind, spec in specs.items():
                path = directory/f"{kind}-{identifier()}.json"
                written.append(path)
                atomic_json(path, dict(spec, source=sources[kind]))
                records[kind] = dict(path=str(path), sha256=sha256(path), source=sources[kind], validation=checks[kind])
            atomic_json(lib.root/"keys.json", records)
    except BaseException:
        # The registry may already be published (e.g. a directory fsync failed after os.replace); keep every file it
        # references and remove only unreferenced new files.
        referenced = {record.get("path") for record in read_json(lib.root/"keys.json", {}).values() if isinstance(record, dict)}
        for path in written:
            if str(path) not in referenced:
                path.unlink(missing_ok=True)
        raise
    return dict(keys={kind: dict(validation=checks[kind], source=sources[kind]) for kind in specs})


def set_key(lib, kind, path, *, selected=None):
    path = Path(path).expanduser().resolve()
    raw, spec = read_key(kind, path)
    check = validate(kind, spec, selected or {})
    digest = hashlib.sha256(raw).hexdigest()
    with lib.metadata():
        # Register the digest of the bytes that were validated; a file replaced meanwhile is rejected.
        if read_key(kind, path)[0] != raw:
            raise ManagerError("key-changed", f"The {kind.upper()} key file changed during validation; register it again. {guidance(kind)}", 6)
        records = read_json(lib.root/"keys.json", {})
        records[kind] = dict(path=str(path), sha256=digest, source="user-supplied", validation=check)
        atomic_json(lib.root/"keys.json", records)
    return dict(kind=kind, path=str(path), sha256=records[kind]["sha256"], validation=check)


def enter_key(lib, kind, spec, selected):
    spec = format_key(kind, spec)
    check = validate(kind, spec, selected)
    return store(lib, {kind: spec}, {kind: check}, {kind: "user-entered"})


def parse_rpcs3(header, cpp):
    """Parse only the named arrays and APP constructors in the pinned source layout."""
    try:
        def array(name, size):
            matches = re.findall(r"constexpr\s+u8\s+" + name + r"\s*\[[^]]+\]\s*=\s*\{([^}]+)\}", header)
            if len(matches) != 1:
                raise ValueError()
            tokens = matches[0].strip().rstrip(",").split(",")
            if len(tokens) != size or any(not re.fullmatch(r"\s*0x[\da-fA-F]{2}\s*", t) for t in tokens):
                raise ValueError()
            return bytes(int(t, 16) for t in tokens).hex()
        pup = format_key("pup", dict(erk=array("SCEPKG_ERK", 32), riv=array("SCEPKG_RIV", 16), pup_hmac=array("PUP_KEY", 64)))
        block = re.search(r"void KeyVault::LoadSelfAPPKeys\(\)\s*\{(.*?)\n\}", cpp, re.S)
        if not block:
            raise ValueError()
        constructors = re.findall(r"sk_APP_arr\.emplace_back\((.*?)\);", block[1], re.S)
        apps = {}
        for constructor in constructors:
            fields = [field.strip() for field in constructor.split(",")]
            if len(fields) != 9 or fields[3] != "KEY_APP":
                raise ValueError()
            revision = int(fields[2], 0)
            if revision in apps or not all(re.fullmatch(r'"[\da-fA-F]*"', field) for field in fields[4:8]):
                raise ValueError()
            apps[revision] = format_key("app", dict(type=4, revision=revision, erk=fields[4][1:-1], riv=fields[5][1:-1]))
        if not apps:
            raise ValueError()
        return pup, apps
    except (ValueError, ManagerError):
        raise ManagerError("key-source-format", "RPCS3 key source has an unsupported or ambiguous layout; nothing saved. Report this error.", 6) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ManagerError("key-download", "RPCS3 download redirected; refusing an unpinned source. Nothing saved.", 6)


def download(url):
    with urllib.request.build_opener(NoRedirect).open(url, timeout=30) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ManagerError("key-download", "RPCS3 source exceeds size limit; nothing saved.", 6)
    return data


def fetch_rpcs3(lib, selected):
    revision = 1  # Ratchet default only when no target is available; reported as format-only.
    if "app" in selected:
        try:
            _, revision = self_identity(selected["app"])
        except Exception:
            raise invalid("app", "Cannot select an APP key: expected a readable retail APP SELF header.") from None
    contents = {}
    for name, expected in RPCS3_HASHES.items():
        try:
            data = download(RPCS3_BASE + name)
        except (OSError, urllib.error.URLError, TimeoutError):
            raise ManagerError("key-download", "Cannot download pinned RPCS3 source over HTTPS. Check your connection and retry; nothing saved.", 6) from None
        if hashlib.sha256(data).hexdigest() != expected:
            raise ManagerError("key-source-hash", f"RPCS3 {name} SHA-256 mismatch; nothing saved. Retry or report the source mismatch.", 6)
        contents[name] = data.decode("utf-8")
    pup, apps = parse_rpcs3(contents["key_vault.h"], contents["key_vault.cpp"])
    if revision not in apps:
        raise invalid("app", f"Pinned RPCS3 source has no APP revision {revision}; nothing saved.")
    specs = {"pup": pup, "app": apps[revision]}
    checks = {kind: validate(kind, spec, selected) for kind, spec in specs.items()}
    return store(lib, specs, checks, {"pup": RPCS3_BASE + "key_vault.h", "app": RPCS3_BASE + "key_vault.cpp"})
