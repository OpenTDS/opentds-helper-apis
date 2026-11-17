"""Built-in AES and RC4 (used where the 'cryptography' package is absent)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import mail_crypto as c  # noqa: E402


@pytest.fixture(autouse=True)
def pure(monkeypatch):
    monkeypatch.setattr(c, '_HAVE_CRYPTOGRAPHY', False)


@pytest.mark.parametrize('key,expected', [
    ('000102030405060708090a0b0c0d0e0f', '69c4e0d86a7b0430d8cdb78070b4c55a'),           # FIPS-197 C.1
    ('000102030405060708090a0b0c0d0e0f1011121314151617', 'dda97ca4864cdfe06eaf70a0ec0d7191'),
    ('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f',
     '8ea2b7ca516745bfeafc49904b496089')])                                                # FIPS-197 C.3
def test_aes_block_vectors(key, expected):
    pt = bytes.fromhex('00112233445566778899aabbccddeeff')
    a = c.AES(bytes.fromhex(key))
    ct = c._pack(*a.encrypt_block(*c._words(pt, 0)))
    assert ct.hex() == expected
    assert c._pack(*a.decrypt_block(*c._words(ct, 0))) == pt


def test_aes_cbc_sp800_38a():
    # NIST SP 800-38A F.2.1 / F.2.5 (first block)
    iv = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    pt = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
    k128 = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    assert c.aes_cbc_encrypt(k128, iv, pt).hex() == '7649abac8119b246cee98e9b12e9197d'
    k256 = bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
    ct = c.aes_cbc_encrypt(k256, iv, pt)
    assert ct.hex() == 'f58c4c04d6e5f1ba779eabfb5f7bfbd6'
    assert c.aes_cbc_decrypt(k256, iv, ct) == pt


def test_aes_pdf_padding_and_rc4():
    key = bytes(range(16))
    iv = bytes(16)
    plain = b'BT /F1 9 Tf (Hi) Tj ET'
    pad = 16 - len(plain) % 16
    blob = iv + c.aes_cbc_encrypt(key, iv, plain + bytes([pad]) * pad)
    assert c.aes_decrypt_pdf(key, blob) == plain
    # RC4 test vector (RFC 6229 is long; the classic 'Key'/'Plaintext' vector)
    assert c.rc4(b'Key', b'Plaintext').hex() == 'bbf316e8d940af0ad3'
