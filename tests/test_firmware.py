import importlib.util
import io
import hmac
import struct
import tarfile
import unittest

from anyps3.errors import AnyPS3Error
from anyps3.firmware import pup_entries, tar_files, package_sections


def tar_entry(name, payload=b'abc', kind=tarfile.REGTYPE):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as tar:
        item=tarfile.TarInfo(name); item.type=kind; item.size=len(payload) if kind==tarfile.REGTYPE else 0
        item.linkname='outside'
        tar.addfile(item,io.BytesIO(payload) if item.size else None)
    return out.getvalue()


class FirmwareTests(unittest.TestCase):
    def test_pup_hmac_bounds_and_hash_index(self):
        key=bytes(range(64));payload=b'3.01\n';header=112
        data=bytearray(struct.pack('>8s5Q',b'SCEUF\0\0\0',1,1,1,header,len(payload)))
        data.extend(struct.pack('>4Q',0x100,header,len(payload),0))
        data.extend(struct.pack('>Q20s4x',0,hmac.new(key,payload,'sha1').digest()));data.extend(payload)
        self.assertEqual(pup_entries(bytes(data),key),{0x100:payload})
        altered=data.copy();altered[-1]^=1
        with self.assertRaisesRegex(AnyPS3Error,'HMAC'):pup_entries(bytes(altered),key)
        altered=data.copy();struct.pack_into('>Q',altered,56,header-1)
        with self.assertRaisesRegex(AnyPS3Error,'entry'):pup_entries(bytes(altered),key)
        altered=data.copy();struct.pack_into('>Q',altered,80,1)
        with self.assertRaisesRegex(AnyPS3Error,'index'):pup_entries(bytes(altered),key)

    def test_archive_paths_and_links(self):
        self.assertEqual(tar_files(tar_entry('dev_flash/sys/test')),{'dev_flash/sys/test':b'abc'})
        for name in ('../outside','/absolute','dev_flash/../outside','dev_flash\\outside','dev_flash//file'):
            with self.subTest(name=name),self.assertRaises(AnyPS3Error):tar_files(tar_entry(name))
        for kind in (tarfile.SYMTYPE,tarfile.LNKTYPE,tarfile.CHRTYPE):
            with self.subTest(kind=kind),self.assertRaises(AnyPS3Error):tar_files(tar_entry('link',kind=kind))

    @unittest.skipUnless(importlib.util.find_spec('cryptography'),'Optional AES dependency unavailable')
    def test_encrypted_package_integrity(self):
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        def enc(data,key,mode):
            e=Cipher(algorithms.AES(key),mode).encryptor();return e.update(data)+e.finalize()
        key=bytes(range(32));iv=bytes(range(16));mk=b'M'*16;mi=b'I'*16;sk=b'S'*16;si=b'V'*16;hk=b'H'*64
        clear=b'firmware fixture';header=320
        keys=hmac.new(hk,clear,'sha1').digest()+bytes(12)+hk+sk+si
        meta=struct.pack('>Q6I',0,0,1,8,0,0,0)+struct.pack('>QQ8I',header,len(clear),1,0,2,0,3,6,7,1)+keys
        meta+=bytes(header-96-len(meta))
        data=struct.pack('>IIHHIQQ',0x53434500,2,0,3,0,header,len(clear))
        data+=enc(mk+bytes(16)+mi+bytes(16),key,modes.CBC(iv))
        data+=enc(meta,mk,modes.CTR(mi))+enc(clear,sk,modes.CTR(si))
        self.assertEqual(package_sections(data,key,iv),[clear])
        with self.assertRaisesRegex(AnyPS3Error,'HMAC'):package_sections(data[:-1]+bytes([data[-1]^1]),key,iv)
        with self.assertRaisesRegex(AnyPS3Error,'padding'):package_sections(data,bytes(32),iv)
