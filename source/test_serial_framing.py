"""分帧/回环单元测试：python test_serial_framing.py"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from serial_core import (
    SerialManager, pop_fixed_frames, build_payload,
    LOOPBACK_MAX_PENDING, LOOPBACK_MAX_AGE,
)


def test_pop_fixed_frames():
    buf = bytearray(b"\x01\x02\x03\x04\x05")
    frames = pop_fixed_frames(buf, 3)
    assert frames == [b"\x01\x02\x03"]
    assert bytes(buf) == b"\x04\x05"
    assert pop_fixed_frames(buf, 3) == []
    buf.extend(b"\x06")
    frames = pop_fixed_frames(buf, 3)
    assert frames == [b"\x04\x05\x06"]
    assert bytes(buf) == b""
    assert pop_fixed_frames(bytearray(b"\x01"), 0) == []


def test_loopback_exact_match():
    sm = SerialManager()
    sm._note_loopback_frame(b"\x11\x23")
    sm._recv_buffer.extend(b"\x11\x23\xaa")
    frames = sm._extract_loopback_frames()
    assert frames == [b"\x11\x23"]
    assert bytes(sm._recv_buffer) == b"\xaa"
    assert not sm._loopback_frames


def test_loopback_mismatch_keeps_device_data():
    sm = SerialManager()
    sm._note_loopback_frame(b"\x11\x23")
    sm._recv_buffer.extend(b"\x99\x88")
    frames = sm._extract_loopback_frames()
    # 真实设备数据不被错误丢弃，回环条目留给普通收包逻辑/过期清理
    assert frames == []
    assert bytes(sm._recv_buffer) == b"\x99\x88"
    assert len(sm._loopback_frames) == 1


def test_loopback_expiry_drops_stale():
    sm = SerialManager()
    sm._loopback_frames.append((time.monotonic() - LOOPBACK_MAX_AGE - 1.0, b"\x11\x23"))
    sm._recv_buffer.extend(b"\x99")
    frames = sm._extract_loopback_frames()
    assert frames == []
    assert not sm._loopback_frames


def test_loopback_pending_cap():
    sm = SerialManager()
    for _ in range(LOOPBACK_MAX_PENDING * 2):
        sm._note_loopback_frame(b"\x00")
    assert len(sm._loopback_frames) <= LOOPBACK_MAX_PENDING


def test_build_payload_encoding():
    assert build_payload("AB", hex_mode=False) == "AB".encode("gbk")
    assert build_payload("测", hex_mode=False, encoding="UTF-8") == "测".encode("utf-8")


if __name__ == "__main__":
    test_pop_fixed_frames()
    test_loopback_exact_match()
    test_loopback_mismatch_keeps_device_data()
    test_loopback_expiry_drops_stale()
    test_loopback_pending_cap()
    test_build_payload_encoding()
    print("all framing tests passed")
