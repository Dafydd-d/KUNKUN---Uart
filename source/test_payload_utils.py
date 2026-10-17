"""纯函数单元测试：python test_payload_utils.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serial_core import parse_hex_bytes, checksum_add8, checksum_crc16, build_payload


def test_parse_hex_bytes():
    assert parse_hex_bytes("11 23") == b"\x11\x23"
    assert parse_hex_bytes("111") == b"\x01\x11"  # 奇数位左补零
    assert parse_hex_bytes("11\t23\n") == b"\x11\x23"
    assert parse_hex_bytes("") == b""
    try:
        parse_hex_bytes("gg")
    except ValueError:
        pass
    else:
        raise AssertionError("非法 HEX 未抛 ValueError")


def test_checksums():
    assert checksum_add8(b"\x01\x02\x03") == 0x06
    assert checksum_add8(b"A\r\n") == 0x58
    # CRC16/MODBUS 标准校验值：'123456789' -> 0x4B37
    assert checksum_crc16(b"123456789") == 0x4B37
    # Modbus RTU 示例报文 01 03 00 00 00 0A 的 CRC 为 0xCDC5
    assert checksum_crc16(bytes.fromhex("01 03 00 00 00 0A")) == 0xCDC5


def test_build_payload():
    assert build_payload("11 23", hex_mode=True) == b"\x11\x23"
    assert build_payload("AB", hex_mode=False, add_crlf=True) == b"AB\r\n"
    # 校验覆盖回车换行在内
    assert build_payload("A", hex_mode=False, add_crlf=True, checksum="ADD8") == b"A\r\n\x58"
    # CRC16 低字节在前
    assert build_payload("123456789", hex_mode=False, checksum="CRC16") == b"123456789\x37\x4B"
    assert build_payload("", hex_mode=True) == b""
    try:
        build_payload("zz", hex_mode=True)
    except ValueError:
        pass
    else:
        raise AssertionError("非法 HEX 未抛 ValueError")


if __name__ == "__main__":
    test_parse_hex_bytes()
    test_checksums()
    test_build_payload()
    print("all tests passed")
