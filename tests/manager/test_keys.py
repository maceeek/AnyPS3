"""Synthetic key material and encrypted fixtures only; downloads never use the network."""

import contextlib
import hashlib
import hmac
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from anyps3_manager import keys
from anyps3_manager.cli import main
from anyps3_manager.storage import Library, ManagerError, atomic_json, read_json, tree_hashes
from tests.test_firmware import tar_entry
from tests import test_ingest
from tests.manager.test_manager import iso_fixture

SOURCE = Path(__file__).resolve().parents[2]


def pup_fixture():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def encrypt(data, key, mode):
        worker = Cipher(algorithms.AES(key), mode).encryptor()
        return worker.update(data) + worker.finalize()

    root, iv, hkey = bytes(range(32)), bytes(range(16)), bytes(range(64))
    meta_key, meta_iv = b"m" * 16, b"v" * 16
    clear = b"synthetic firmware section"
    package_header = 320
    section_keys = hmac.new(hkey, clear, "sha1").digest() + bytes(12) + hkey
    metadata = struct.pack(">Q6I", 0, 0, 1, 6, 0, 0, 0)
    metadata += struct.pack(">QQ8I", package_header, len(clear), 1, 0, 2, 0, 1, 0, 0, 1) + section_keys
    metadata += bytes(package_header - 96 - len(metadata))
    package = struct.pack(">IIHHIQQ", 0x53434500, 2, 0, 3, 0, package_header, len(clear))
    package += encrypt(meta_key + bytes(16) + meta_iv + bytes(16), root, modes.CBC(iv))
    package += encrypt(metadata, meta_key, modes.CTR(meta_iv)) + clear
    entries = [(0x100, b"fake-version\n"), (0x300, tar_entry("dev_flash_fake.pkg", package))]
    header = 48 + len(entries) * 64
    data = struct.pack(">8s5Q", b"SCEUF\0\0\0", 1, 1, len(entries), header, sum(len(p) for _, p in entries))
    offset = header
    for ident, payload in entries:
        data += struct.pack(">4Q", ident, offset, len(payload), 0)
        offset += len(payload)
    for index, (_, payload) in enumerate(entries):
        data += struct.pack(">Q20s4x", index, hmac.new(hkey, payload, "sha1").digest())
    data += b"".join(payload for _, payload in entries)
    return data, dict(erk=root.hex(), riv=iv.hex(), pup_hmac=hkey.hex())


def source_fixture(pup, apps):
    header = ""
    for name, field in (("SCEPKG_ERK", "erk"), ("SCEPKG_RIV", "riv"), ("PUP_KEY", "pup_hmac")):
        values = bytes.fromhex(pup[field])
        header += f"constexpr u8 {name}[0x{len(values):x}] = {{" + ", ".join(f"0x{value:02X}" for value in values) + "};\n"
    cpp = "void KeyVault::LoadSelfAPPKeys()\n{\n sk_APP_arr.clear();\n"
    for app in apps:
        cpp += f'sk_APP_arr.emplace_back(0, 0, 0x{app["revision"]:04x}, KEY_APP, "{app["erk"]}", "{app["riv"]}", "", "", 0x11);\n'
    cpp += "\n}\n"
    return {"key_vault.h": header.encode(), "key_vault.cpp": cpp.encode()}


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="anyps3-key-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = Library(self.root/"library", SOURCE)
        self.lib.init()
        self.self_data, _, self.app = test_ingest.SelfTests.fixture()
        self.pup_data, self.pup = pup_fixture()
        self.selected = dict(app=self.self_data, pup=self.pup_data)
        self.files = source_fixture(self.pup, [self.app, dict(self.app, revision=7)])

    def cli(self, *args, stdin=""):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(sys, "stdin", io.StringIO(stdin)):
            code = main(["--library", str(self.lib.root), "--json", "keys", *args])
        return code, json.loads(output.getvalue())

    @contextlib.contextmanager
    def downloads(self):
        def fetch(url):
            self.assertTrue(url.startswith(keys.RPCS3_BASE))
            return self.files[url.rsplit("/", 1)[1]]
        with patch.object(keys, "download", side_effect=fetch) as mocked, patch.object(
                keys, "RPCS3_HASHES", {name: hashlib.sha256(data).hexdigest() for name, data in self.files.items()}):
            yield mocked

    def test_format_exact_lengths_types_and_no_values_in_errors(self):
        for kind, spec in (("app", self.app), ("pup", self.pup)):
            self.assertEqual(keys.format_key(kind, spec), spec)
            for field in ("erk", "riv", *(["pup_hmac"] if kind == "pup" else [])):
                for value in (None, 12, spec[field][:-1], "g" * len(spec[field]), "0x" + spec[field], spec[field] + " "):
                    with self.subTest(kind=kind, field=field, invalid_type=type(value)), self.assertRaises(ManagerError) as caught:
                        keys.format_key(kind, dict(spec, **{field: value}))
                    if isinstance(value, str):
                        self.assertNotIn(value, str(caught.exception))
        for value in (True, "1", -1, 32768):
            with self.assertRaises(ManagerError):
                keys.format_key("app", dict(self.app, revision=value))
        for value in (True, 3, "4"):
            with self.assertRaises(ManagerError):
                keys.format_key("app", dict(self.app, type=value))

    def test_format_only_explicit_without_targets(self):
        result = keys.enter_key(self.lib, "app", self.app, {})
        self.assertIn("Format only", result["keys"]["app"]["validation"])

    def test_pup_valid_and_wrong_erk_riv_hmac(self):
        self.assertIn("HMACs", keys.validate("pup", self.pup, self.selected))
        for field in ("erk", "riv", "pup_hmac"):
            wrong = dict(self.pup, **{field: bytes([37]).hex() * (len(self.pup[field])//2)})
            with self.subTest(field=field), self.assertRaises(ManagerError):
                keys.enter_key(self.lib, "pup", wrong, self.selected)
        self.assertFalse((self.lib.root/"keys.json").exists())
        self.assertFalse((self.lib.root/"private-keys").exists())

    def test_self_decrypted_structure_wrong_keys_and_revision(self):
        self.assertIn("metadata decrypted", keys.validate("app", self.app, self.selected))
        for field, value in (("erk", bytes([37]).hex()*32), ("riv", bytes([37]).hex()*16), ("revision", 2)):
            with self.subTest(field=field), self.assertRaises(ManagerError):
                keys.validate("app", dict(self.app, **{field: value}), self.selected)
        for truncated in (self.self_data[:80], self.self_data[:400]):
            with self.assertRaises(ManagerError):
                keys.validate("app", self.app, dict(app=truncated))

    def test_malformed_decrypted_metadata_rejected(self):
        # Change a known cleartext count in CTR ciphertext while preserving root padding.
        data = bytearray(self.self_data)
        data[0x1a0 + 12:0x1a0 + 16] = bytes(a ^ b for a, b in zip(data[0x1a0 + 12:0x1a0 + 16], struct.pack(">I", 2)))
        with self.assertRaises(ManagerError):
            keys.validate("app", self.app, dict(app=bytes(data)))

    def test_private_store_permissions_and_safe_cli_stdin(self):
        self_path = self.root/"EBOOT.BIN"
        self_path.write_bytes(self.self_data)
        code, result = self.cli("set", "app", "--stdin", "--self", str(self_path), stdin=json.dumps(self.app))
        self.assertEqual(code, 0)
        self.assertIn("metadata decrypted", result["keys"]["app"]["validation"])
        record = read_json(self.lib.root/"keys.json")["app"]
        self.assertEqual(Path(record["path"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.lib.root/"private-keys").stat().st_mode & 0o777, 0o700)
        for field in ("erk", "riv"):
            self.assertNotIn(self.app[field], json.dumps(result))
        self.assertEqual(keys.key_path(self.lib, "app"), record["path"])

    def test_invalid_json_errors_do_not_echo_paste_or_file_contents(self):
        secret = "unlogged-sensitive-input"
        code, result = self.cli("set", "app", "--stdin", stdin='{"' + secret)
        self.assertEqual(code, 6)
        self.assertNotIn(secret, json.dumps(result))
        path = self.root/"bad.json"
        path.write_text('{"' + secret)
        code, result = self.cli("set", "app", str(path))
        self.assertEqual(code, 6)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn("JSON format", result["message"])

    def test_missing_key_explains_purpose_format_and_sources(self):
        for kind in ("app", "pup"):
            with self.assertRaises(ManagerError) as caught:
                keys.key_path(self.lib, kind)
            self.assertEqual(caught.exception.code, "missing-key")
            for text in ("system keys", "JSON format", "own console", "RPCS3", "keys set", "fetch-rpcs3"):
                self.assertIn(text, str(caught.exception))

    def test_set_file_validates_before_registration_and_keeps_reference(self):
        path = self.root/"app.json"
        atomic_json(path, dict(self.app, revision=2))
        with self.assertRaises(ManagerError):
            keys.set_key(self.lib, "app", path, selected=self.selected)
        self.assertFalse((self.lib.root/"keys.json").exists())
        atomic_json(path, self.app)
        keys.set_key(self.lib, "app", path, selected=self.selected)
        self.assertEqual(keys.key_path(self.lib, "app"), str(path))
        path.write_text("changed")
        with self.assertRaisesRegex(ManagerError, "register it again"):
            keys.key_path(self.lib, "app")

    def test_targets_from_iso_and_pup_override(self):
        from tests.test_ingest import record
        iso = bytearray(iso_fixture())
        # Replace synthetic ISO EBOOT entry, keeping independent ISO9660 structure.
        directory = record(b"\0", 22, 2048, 2) + record(b"\1", 21, 2048, 2)
        directory += record(b"EBOOT.BIN;1", 24, len(self.self_data))
        iso[22*2048:23*2048] = directory.ljust(2048, b"\0")
        iso[24*2048:24*2048 + len(self.self_data)] = self.self_data
        path = self.root/"disc.iso"
        path.write_bytes(iso)
        pup = self.root/"update.pup"
        pup.write_bytes(self.pup_data)
        selected = keys.targets(iso=path, pup=pup)
        self.assertEqual(selected, self.selected)
        self.assertIn("metadata", keys.validate("app", self.app, selected))
        self.assertEqual(keys.targets(iso=path), dict(app=self.self_data))
        self.assertEqual(keys.targets(), {})
        with self.assertRaises(ManagerError):
            keys.targets(pup=self.root/"missing")

    def test_parser_synthetic_arrays_and_multiple_app_revisions(self):
        pup, apps = keys.parse_rpcs3(*(self.files[name].decode() for name in ("key_vault.h", "key_vault.cpp")))
        self.assertEqual(pup, self.pup)
        self.assertEqual(apps[1], self.app)
        self.assertEqual(set(apps), {1, 7})
        for header, cpp in (("", self.files["key_vault.cpp"].decode()),
                            (self.files["key_vault.h"].decode(), "")):
            with self.assertRaises(ManagerError):
                keys.parse_rpcs3(header, cpp)

    def test_fetch_pinned_hashes_validation_and_provenance(self):
        with self.downloads() as fetch:
            result = keys.fetch_rpcs3(self.lib, self.selected)
        self.assertEqual(fetch.call_count, 2)
        records = read_json(self.lib.root/"keys.json")
        for kind in ("app", "pup"):
            self.assertTrue(records[kind]["source"].startswith(keys.RPCS3_BASE))
            self.assertNotIn("Format only", result["keys"][kind]["validation"])
            stored = read_json(records[kind]["path"])
            self.assertEqual(stored["source"], records[kind]["source"])

    def test_fetch_selects_actual_self_revision(self):
        data = bytearray(self.self_data)
        struct.pack_into(">H", data, 8, 7)
        with self.downloads():
            keys.fetch_rpcs3(self.lib, dict(self.selected, app=bytes(data)))
        spec = read_json(keys.key_path(self.lib, "app"))
        self.assertEqual(spec["revision"], 7)

    def test_fetch_network_or_hash_failure_preserves_previous_keys(self):
        keys.enter_key(self.lib, "app", self.app, {})
        before = tree_hashes(self.lib.root)
        for failure in (OSError("network failure"), urllib.error.URLError("offline")):
            with patch.object(keys, "download", side_effect=failure), self.assertRaises(ManagerError) as caught:
                keys.fetch_rpcs3(self.lib, self.selected)
            self.assertEqual(caught.exception.code, "key-download")
            self.assertEqual(before, tree_hashes(self.lib.root))
        with self.downloads(), patch.object(keys, "RPCS3_HASHES", {"key_vault.h": hashlib.sha256(self.files["key_vault.h"]).hexdigest(), "key_vault.cpp": "bad"}):
            with self.assertRaises(ManagerError) as caught:
                keys.fetch_rpcs3(self.lib, self.selected)
        self.assertEqual(caught.exception.code, "key-source-hash")
        self.assertEqual(before, tree_hashes(self.lib.root))

    def test_fetch_validation_failure_stores_neither_key(self):
        wrong = bytearray(self.pup_data)
        wrong[-1] ^= 1
        with self.downloads(), self.assertRaises(ManagerError):
            keys.fetch_rpcs3(self.lib, dict(self.selected, pup=bytes(wrong)))
        self.assertFalse((self.lib.root/"keys.json").exists())
        self.assertFalse((self.lib.root/"private-keys").exists())

    def test_cli_fetch_without_targets_is_format_only(self):
        with self.downloads():
            code, result = self.cli("fetch-rpcs3")
        self.assertEqual(code, 0)
        self.assertTrue(all("Format only" in record["validation"] for record in result["keys"].values()))

    def test_redirect_refused(self):
        handler = keys.NoRedirect()
        with self.assertRaises(ManagerError):
            handler.redirect_request(None, None, 302, "redirect", {}, "https://example.org/unpinned")

    def test_private_store_refuses_symlink(self):
        other = self.root/"other"
        other.mkdir()
        (self.lib.root/"private-keys").symlink_to(other, target_is_directory=True)
        with self.assertRaises(ManagerError):
            keys.enter_key(self.lib, "app", self.app, {})
        self.assertEqual(list(other.iterdir()), [])

    def test_cli_validation_target_arguments_and_missing_arguments(self):
        pup = self.root/"update.pup"
        pup.write_bytes(self.pup_data)
        key = self.root/"pup.json"
        atomic_json(key, self.pup)
        code, result = self.cli("set", "pup", str(key), "--pup", str(pup))
        self.assertEqual(code, 0)
        self.assertIn("HMACs", result["validation"])
        self.assertEqual(result["sha256"], hashlib.sha256(key.read_bytes()).hexdigest())
        for args in (("set",), ("set", "app"), ("fetch-rpcs3", "app"), ("set", "app", str(key), "--stdin")):
            code, result = self.cli(*args)
            self.assertEqual(code, 2)

    def test_parser_rejects_ambiguous_app_revision(self):
        cpp = self.files["key_vault.cpp"].decode()
        cpp = cpp.replace("0x0007", "0x0001")
        with self.assertRaises(ManagerError) as caught:
            keys.parse_rpcs3(self.files["key_vault.h"].decode(), cpp)
        self.assertEqual(caught.exception.code, "key-source-format")


if __name__ == "__main__":
    unittest.main()
