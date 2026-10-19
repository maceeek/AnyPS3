import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from anyps3.analysis import analyze, imports, sections
from anyps3.demo import make_demo
from anyps3.disc import inventory, extract
from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.self_file import unpack_self
from anyps3.sfo import parse_sfo
from anyps3.archive import recover_iso


def sfo_fixture():
    names = b"TITLE_ID\0APP_VER\0"
    key_start = 20 + 32
    value_start = key_start + len(names)
    return (struct.pack("<5I", 0x46535000, 0x101, key_start, value_start, 2)
            + struct.pack("<HHIII", 0, 0x204, 10, 10, 0)
            + struct.pack("<HHIII", 9, 0x204, 6, 6, 10)
            + names + b"TEST00001\0" + b"01.00\0")


def both(value, width):
    return value.to_bytes(width, "little") + value.to_bytes(width, "big")


def record(name, lba, size, flags=0):
    length = 33 + len(name) + (len(name) % 2 == 0)
    data = bytearray(length)
    data[0] = length
    data[2:10] = both(lba, 4)
    data[10:18] = both(size, 4)
    data[25] = flags
    data[28:32] = both(1, 2)
    data[32] = len(name)
    data[33:33 + len(name)] = name
    return data


def iso_fixture(*, partial=False, unsafe=False, multi=False):
    data = bytearray(24 * 2048)
    pvd = bytearray(2048)
    pvd[:7] = b"\x01CD001\x01"
    pvd[40:72] = b"TEST".ljust(32, b" ")
    pvd[80:88] = both(24, 4)
    pvd[128:132] = both(2048, 2)
    root = record(b"\0", 20, 2048, 2)
    pvd[156:156 + len(root)] = root
    data[16 * 2048:17 * 2048] = pvd
    names = b"../OUT;1" if unsafe else b"FIRST.BIN;1"
    directory = root + record(b"\1", 20, 2048, 2) + record(names, 21, 3, 128 if multi else 0)
    if multi:
        directory += record(names, 22, 2)
    directory += record(b"LAST.BIN;1", 23, 3)
    data[20 * 2048:20 * 2048 + len(directory)] = directory
    data[21 * 2048:21 * 2048 + 3] = b"abc"
    data[22 * 2048:22 * 2048 + 2] = b"de"
    data[23 * 2048:23 * 2048 + 3] = b"xyz"
    return bytes(data[:23 * 2048] if partial else data)


class SfoTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(parse_sfo(sfo_fixture()), {"TITLE_ID": "TEST00001", "APP_VER": "01.00"})

    def test_truncation_and_invalid_tables(self):
        for data in (sfo_fixture()[:-1], b"wrong", sfo_fixture()[:12] + b"\xff" * 8):
            with self.subTest(data=len(data)), self.assertRaises(AnyPS3Error):
                parse_sfo(data)


class ArchiveTests(unittest.TestCase):
    def test_no_central_directory_is_needed_for_complete_member(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "disc.zip"
            payload = iso_fixture()
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr("README.TXT", "original test fixture")
                with z.open("disc.iso", "w", force_zip64=True) as member:
                    member.write(payload)
            data = source.read_bytes()
            source.write_bytes(data[:data.index(b"PK\x01\x02")])
            report = recover_iso(source, root / "out.iso")
            self.assertTrue(report["member_crc_verified"])
            self.assertEqual((root / "out.iso").read_bytes(), payload)

    def test_partial_stored_member_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "disc.zip"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("disc.iso", b"0123456789")
            data = source.read_bytes()
            source.write_bytes(data[:30 + len("disc.iso") + 5])
            with self.assertRaisesRegex(AnyPS3Error, "incomplete"):
                recover_iso(source, root / "refused.iso")
            self.assertFalse((root / "refused.iso").exists())
            report = recover_iso(source, root / "partial.iso", allow_partial=True)
            self.assertFalse(report["member_crc_verified"])
            self.assertEqual((root / "partial.iso").read_bytes(), b"01234")

    def test_bad_crc_is_an_error_even_in_partial_mode(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "disc.zip"
            with zipfile.ZipFile(source, "w") as z:
                z.writestr("disc.iso", b"0123456789")
            data = bytearray(source.read_bytes())
            data[14] ^= 1
            source.write_bytes(data)
            with self.assertRaisesRegex(AnyPS3Error, "CRC"):
                recover_iso(source, root / "bad.iso", allow_partial=True)


class DiscTests(unittest.TestCase):
    def test_partial_extraction_reports_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = root / "test.iso"
            iso.write_bytes(iso_fixture(partial=True))
            report = inventory(iso)
            self.assertEqual(report["file_count"], 2)
            self.assertEqual(report["available_file_count"], 1)
            with self.assertRaisesRegex(AnyPS3Error, "partial"):
                extract(iso, root / "refused")
            self.assertFalse((root / "refused").exists())
            result = extract(iso, root / "out", allow_partial=True)
            self.assertEqual(result["missing"], ["LAST.BIN"])
            self.assertEqual((root / "out/FIRST.BIN").read_bytes(), b"abc")
            self.assertFalse((root / "out/LAST.BIN").exists())

    def test_multi_extent_file_is_joined(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = root / "test.iso"
            iso.write_bytes(iso_fixture(multi=True))
            result = extract(iso, root / "out")
            self.assertEqual(result["extracted_count"], 2)
            self.assertEqual((root / "out/FIRST.BIN").read_bytes(), b"abcde")
            self.assertEqual(result["extracted"][0]["sha256"], hashlib.sha256(b"abcde").hexdigest())

    def test_path_traversal_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = root / "test.iso"
            iso.write_bytes(iso_fixture(unsafe=True))
            with self.assertRaisesRegex(AnyPS3Error, "Unsafe"):
                extract(iso, root / "out")
            self.assertFalse((root / "out").exists())

    def test_existing_directory_is_never_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            iso = root / "test.iso"
            iso.write_bytes(iso_fixture())
            with self.assertRaises(FileExistsError):
                extract(iso, root)

    def test_mismatched_endian_field(self):
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "test.iso"
            data = bytearray(iso_fixture())
            data[16 * 2048 + 87] ^= 1
            iso.write_bytes(data)
            with self.assertRaisesRegex(AnyPS3Error, "endian"):
                inventory(iso)


class AnalysisTests(unittest.TestCase):
    @staticmethod
    def image_with_import():
        data = bytearray(make_demo())
        data.extend(bytes(0x480 - len(data)))
        struct.pack_into(">H", data, 56, 3)
        struct.pack_into(">QQ", data, 120 + 32, 512, 512)
        struct.pack_into(">II6Q", data, 176, 0x60000002, 0, 0x210, 0x20010, 0, 32, 32, 4)
        struct.pack_into(">II", data, 0x210, 32, 0x1B434CEC)
        struct.pack_into(">II", data, 0x228, 0x20040, 0x2006C)
        data[0x240] = 44
        struct.pack_into(">H", data, 0x246, 1)
        struct.pack_into(">III", data, 0x250, 0x20090, 0x200A0, 0x200B0)
        data[0x290:0x295] = b"test\0"
        struct.pack_into(">I", data, 0x2A0, 0x12345678)
        struct.pack_into(">I", data, 0x2B0, 0x10000)
        struct.pack_into(">Q", data, 40, 0x400)
        struct.pack_into(">HHH", data, 58, 64, 2, 0)
        struct.pack_into(">II4QII2Q", data, 0x440, 0, 1, 3, 0x20000, 0x200, 8, 0, 0, 8, 0)
        return data

    def test_opd_and_import_tables(self):
        result = analyze(ElfImage.parse(bytes(self.image_with_import())))
        self.assertEqual(result["opd_descriptor_count"], 1)
        self.assertEqual(result["unique_code_entry_count"], 1)
        self.assertEqual(result["recompiler_survey"]["supported_entry_count"], 1)
        self.assertEqual(result["import_function_count"], 1)
        self.assertEqual(result["imports"][0]["library"], "test")
        self.assertEqual(result["imports"][0]["functions"], [{"nid": "0x12345678", "stub": 0x10000}])

    def test_zero_sized_import_record_does_not_loop(self):
        data = self.image_with_import()
        data[0x240] = 0
        with self.assertRaisesRegex(AnyPS3Error, "record size"):
            imports(ElfImage.parse(bytes(data)))

    def test_empty_load_headers_with_unused_alignment(self):
        data = bytearray(make_demo())
        # Real retail EBOOT contains empty PT_LOAD entries at VA=0, arbitrary file offset.
        struct.pack_into(">II6Q", data, 120, 1, 4, 0x123, 0, 0, 0, 0, 0x10000)
        image = ElfImage.parse(bytes(data))
        self.assertEqual(image.segments[1].memory_size, 0)

    def test_sectionless_demo_does_not_invent_functions(self):
        result = analyze(ElfImage.parse(make_demo()))
        self.assertEqual(result["unique_code_entry_count"], 0)
        self.assertEqual(result["imports"], [])
        self.assertIsNone(result["recompiler_survey"]["entry_blocker"])

    def test_bad_section_table(self):
        data = bytearray(make_demo())
        struct.pack_into(">Q", data, 40, 0x10000000)
        struct.pack_into(">HHH", data, 58, 64, 1, 0)
        with self.assertRaisesRegex(AnyPS3Error, "section table"):
            sections(ElfImage.parse(bytes(data)))


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Optional SELF tests require cryptography.")
class SelfTests(unittest.TestCase):
    @staticmethod
    def fixture():
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        def encrypt(data, key, mode):
            worker = Cipher(algorithms.AES(key), mode).encryptor()
            return worker.update(data) + worker.finalize()
        original = make_demo()
        root_key, root_iv = bytes(range(32)), bytes(range(16))
        meta_key, meta_iv = bytes([0xA5]) * 16, bytes([0x5A]) * 16
        result = bytearray(0x418)
        struct.pack_into(">IIHHIQQ", result, 0, 0x53434500, 2, 1, 1, 0x140, 0x400, len(original))
        struct.pack_into(">10Q", result, 32, 3, 112, 144, 208, 0, 0, 0, 0, 0, 0)
        struct.pack_into(">QIIQQ", result, 112, 0, 0, 4, 0, 0)
        result[144:208] = original[:64]
        result[208:320] = original[64:176]
        keys, records = bytearray(), bytearray()
        for i, (src, dst, size) in enumerate(((0x100, 0x400, 16), (0x200, 0x410, 8))):
            clear = original[src:src + size]
            hkey = bytes([i + 1]) * 64
            aes_key, aes_iv = bytes([i + 2]) * 16, bytes([i + 3]) * 16
            keys.extend(hmac.new(hkey, clear, "sha1").digest() + bytes(12) + hkey + aes_key + aes_iv)
            records.extend(struct.pack(">QQ8I", dst, size, 2, i, 2, i * 8, 3, i * 8 + 6, i * 8 + 7, 1))
            result[dst:dst + size] = encrypt(clear, aes_key, modes.CTR(aes_iv))
        info = meta_key + bytes(16) + meta_iv + bytes(16)
        result[0x160:0x1A0] = encrypt(info, root_key, modes.CBC(root_iv))
        metadata = struct.pack(">Q6I", 0, 0, 2, 16, 0, 0, 0) + records + keys
        metadata += bytes(0x400 - 0x1A0 - len(metadata))
        result[0x1A0:0x400] = encrypt(metadata, meta_key, modes.CTR(meta_iv))
        keyfile = {"type": 4, "revision": 1, "erk": root_key.hex(), "riv": root_iv.hex()}
        return bytes(result), original, keyfile

    def test_verified_reconstruction(self):
        data, expected, key = self.fixture()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "key.json"
            path.write_text(json.dumps(key))
            elf, report = unpack_self(data, path)
            self.assertEqual(elf, expected)
            self.assertTrue(all(s["hmac_verified"] for s in report["sections"]))
            self.assertFalse(report["ecdsa_signature_verified"])

    def test_tampered_payload_is_rejected(self):
        data, _, key = self.fixture()
        data = data[:-1] + bytes([data[-1] ^ 1])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "key.json"
            path.write_text(json.dumps(key))
            with self.assertRaisesRegex(AnyPS3Error, "HMAC failed"):
                unpack_self(data, path)

    def test_prx_requires_explicit_extraction_and_stays_nonexecutable(self):
        data, _, key = self.fixture()
        data=bytearray(data);struct.pack_into('>H',data,144+16,0xffa4)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'key.json';path.write_text(json.dumps(key))
            with self.assertRaisesRegex(AnyPS3Error,'ET_EXEC'):unpack_self(bytes(data),path)
            elf,report=unpack_self(bytes(data),path,allow_prx=True)
            self.assertTrue(report['requires_relocation'])
            with self.assertRaisesRegex(AnyPS3Error,'ET_EXEC'):ElfImage.parse(elf).require_ppu_executable()

    def test_wrong_key_and_short_file(self):
        data, _, key = self.fixture()
        key["erk"] = "00" * 32
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "key.json"
            path.write_text(json.dumps(key))
            with self.assertRaisesRegex(AnyPS3Error, "padding"):
                unpack_self(data, path)
            with self.assertRaises(AnyPS3Error):
                unpack_self(data[:80], path)


if __name__ == "__main__":
    unittest.main()
