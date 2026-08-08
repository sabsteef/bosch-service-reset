#!/usr/bin/env python3
"""
reset_service.py — Reset SERVICE_DUE on Bosch Smart System via USB.

Standalone script — no external dependencies besides PyUSB.

Writes parameter SERVICE_DUE (0x2185) on the RemoteControl (BRC3600).
Protobuf payload: timestamp (field 1, nested message) + odometer (field 2, varint).

Requirements:
  - BRC3600 USB connected (cable to LED Remote on handlebar)
  - Bike powered on

Usage:
    python3 reset_service.py                    # read-only: show current SERVICE_DUE + odometer
    python3 reset_service.py --reset            # dry-run: show what would be written
    python3 reset_service.py --reset --confirm  # confirmed write
    python3 reset_service.py --reset --confirm --interval 2000  # different interval

Discovery: SERVICE_DUE = ReadableWritableSubscribable DataPoint
  Address: 0x2185 (service=0xa1, param=0x85)
  Protobuf: { Timestamp { int64 value = 1 }, int32 odometer = 2 }
  Source: bdp-messagebus-1.2.3.jar RemoteControlAddresses enum
"""
import argparse
import struct
import sys
import time

try:
    import usb.core
    import usb.util
except ImportError:
    sys.exit("pip install pyusb  (and: brew install libusb)")


# ============================================================
# USB constants — BRC3600 LED Remote
# ============================================================
VID, PID = 0x108C, 0x01C0
EP_BULK_IN = 0x83
EP_BULK_OUT = 0x04
TIMEOUT = 1000

STATUS_CODES = {
    0: "SUCCESS",
    1: "OVERLOADED",
    2: "NO_ROUTE_FOUND",
    3: "NOT_READY",
    4: "UNSUPPORTED",
    6: "DENIED",
    7: "INVALID_VALUE",
    8: "MALFORMED",
    9: "TIMEOUT",
    10: "TOO_LARGE",
    255: "UNKNOWN_ERROR",
}


# ============================================================
# USB transport layer
# ============================================================
def ctrl_in(dev, req: int, length: int, value: int = 0, index: int = 0) -> bytes:
    return bytes(dev.ctrl_transfer(0xC1, req, value, index, length, TIMEOUT))


def ctrl_out(dev, req: int, data: bytes = b'', value: int = 0, index: int = 0):
    return dev.ctrl_transfer(0x41, req, value, index, data, TIMEOUT)


def open_brc(verbose: bool = True):
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("BRC3600 not found. Check: connected, powered, no other process.")
    try:
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
    except (NotImplementedError, usb.core.USBError):
        pass
    dev.set_configuration()
    usb.util.claim_interface(dev, 0)
    if verbose:
        print(f"[ok] BRC3600 SN={dev.serial_number}")
    return dev


def init_session(dev, verbose: bool = True):
    r = ctrl_in(dev, 0x01, 1)
    if verbose:
        print(f"[init] PROTO_QUERY -> {r.hex()}  (expect 06)")
    ctrl_out(dev, 0x00)
    cfg = bytes.fromhex("008000000004000000")
    ctrl_out(dev, 0x43, cfg)
    ctrl_out(dev, 0x23, cfg)
    ctrl_out(dev, 0x41)
    ctrl_out(dev, 0x21)

    try:
        intro = bytes(dev.read(EP_BULK_IN, 256, timeout=300))
        if verbose:
            print(f"[init] intro bulk IN ({len(intro)}b): {intro.hex()[:100]}...")
    except usb.core.USBError:
        if verbose:
            print("[init] (no immediate intro)")
    try:
        ctrl_out(dev, 0x45)
    except usb.core.USBError:
        pass

    if verbose:
        print("[init] sending wake-up handshake (9 bulk OUTs)...")
    wakeup_frames = [
        bytes.fromhex("10020103"),
        bytes.fromhex("10030404"),
        bytes.fromhex("1006020100100000"),
        bytes.fromhex("1006020200100000"),
        bytes.fromhex("1006020300100000"),
        bytes.fromhex("1006020400000000"),
        bytes.fromhex("1006020500000000"),
        bytes.fromhex("1006020600000000"),
        bytes.fromhex("1006020700000000"),
    ]
    arm_lengths = [4, 5, 8, 8, 8, 8, 8, 8, 8]
    for idx, (frame, arm_len) in enumerate(zip(wakeup_frames, arm_lengths)):
        try:
            ctrl_out(dev, 0x48, struct.pack('<I', arm_len))
            padded = frame + b'\x00' * (64 - len(frame))
            dev.write(EP_BULK_OUT, padded, timeout=TIMEOUT)
            ack = ctrl_in(dev, 0x47, 3)
            if verbose:
                print(f"  wake[{idx+1}]: {frame.hex()} -> ack={ack.hex()}")
        except usb.core.USBError as e:
            if verbose:
                print(f"  wake[{idx+1}]: failed ({e.errno})")
            break

    if verbose:
        print("[init] sending ENTER_DIAGNOSTIC_MODE...")
    try:
        send_frame(dev, bytes.fromhex("0e10a10641"), verbose=verbose)
        r1 = receive_frame(dev, verbose=verbose)
        if verbose and r1:
            print(f"  diag-trigger response: {r1.hex()[:40]}")
    except usb.core.USBError as e:
        if verbose:
            print(f"  diag-mode trigger failed: {e}")
    try:
        send_frame(dev, bytes.fromhex("0e10a0e402"), verbose=verbose)
        r2 = receive_frame(dev, verbose=verbose)
        if verbose and r2:
            print(f"  second-init-read response: {r2.hex()[:40]}")
    except usb.core.USBError as e:
        if verbose:
            print(f"  second init read failed: {e}")

    if verbose:
        print("[init] session ready")


def send_frame(dev, body: bytes, verbose: bool = False) -> bytes:
    frame = bytes([0x30, len(body)]) + body
    padded = frame + b'\x00' * (64 - len(frame))
    arm_len = len(body) + 2 if len(body) >= 5 else 4
    ctrl_out(dev, 0x48, struct.pack('<I', arm_len))
    dev.write(EP_BULK_OUT, padded, timeout=TIMEOUT)
    ack = ctrl_in(dev, 0x47, 3)
    if verbose:
        print(f"  -> {frame.hex()}  ack={ack.hex()}")
    return ack


def poll_until_data(dev, max_ms: int = 500):
    end = time.time() + max_ms / 1000
    while time.time() < end:
        s = ctrl_in(dev, 0x44, 7)
        if s and s[0] not in (0x00, 0x40, 0x41):
            return s
        time.sleep(0.005)
    return None


def receive_frame(dev, verbose: bool = False) -> bytes | None:
    try:
        data = bytes(dev.read(EP_BULK_IN, 256, timeout=500))
    except usb.core.USBError:
        return None
    for i in range(len(data) - 1):
        if data[i] == 0x30:
            ln = data[i + 1]
            if 0 < ln < 0x80 and i + 2 + ln <= len(data):
                if verbose:
                    print(f"  <- {data[i:i+2+ln].hex()}")
                return data[i + 2 : i + 2 + ln]
    return data


def drain_bulk_in(dev, max_iter: int = 5):
    for _ in range(max_iter):
        try:
            data = bytes(dev.read(EP_BULK_IN, 256, timeout=80))
            if not data:
                break
        except usb.core.USBError:
            break


def read_param(dev, svc: int, param: int, seq: int,
               verbose: bool = False, clean: bool = False) -> bytes | None:
    if clean:
        drain_bulk_in(dev)
    attempts = 3 if clean else 1
    cur_seq = seq
    for attempt in range(attempts):
        body = bytes([0x0e, 0x10, svc, param, cur_seq])
        try:
            send_frame(dev, body, verbose)
        except usb.core.USBError as e:
            if verbose:
                print(f"  send_frame failed: {e}")
            return None
        poll_until_data(dev, max_ms=150)
        try:
            ctrl_out(dev, 0x45)
        except usb.core.USBError:
            pass
        r = receive_frame(dev, verbose)
        if not clean:
            return r
        if r and len(r) >= 2 and r[1] == param:
            return r
        if verbose:
            print(f"  (mismatch: got msg_id {r[:2].hex() if r else '?'}, expected XX{param:02x}; retrying)")
        cur_seq = (cur_seq + 1) & 0xFF
    if verbose:
        print(f"  ALL {attempts} attempts failed to get reply for param 0x{param:02x}")
    return None


# ============================================================
# Protobuf helpers
# ============================================================
def encode_varint(value: int) -> bytes:
    result = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            result.append(b | 0x80)
        else:
            result.append(b)
            break
    return bytes(result)


def decode_varint(data: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
    return val, pos


def decode_protobuf_simple(data: bytes) -> dict:
    fields = {}
    pos = 0
    while pos < len(data):
        tag, pos = decode_varint(data, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = decode_varint(data, pos)
            fields[field_num] = ('varint', val)
        elif wire_type == 2:
            length, pos = decode_varint(data, pos)
            fields[field_num] = ('bytes', data[pos:pos + length])
            pos += length
        elif wire_type == 1:
            fields[field_num] = ('fixed64', int.from_bytes(data[pos:pos + 8], 'little'))
            pos += 8
        elif wire_type == 5:
            fields[field_num] = ('fixed32', int.from_bytes(data[pos:pos + 4], 'little'))
            pos += 4
        else:
            break
    return fields


def build_service_due_protobuf(timestamp: int, odometer: int) -> bytes:
    ts_inner = b'\x08' + encode_varint(timestamp)
    pb = b'\x0a' + encode_varint(len(ts_inner)) + ts_inner
    pb += b'\x10' + encode_varint(odometer)
    return pb


# ============================================================
# Service read/write
# ============================================================
def read_service_due(dev, seq: int) -> bytes | None:
    print("\n=== READ SERVICE_DUE (0x2185) ===")
    drain_bulk_in(dev)
    return read_param(dev, 0xa1, 0x85, seq, verbose=True, clean=True)


def read_odometer(dev, seq: int) -> int | None:
    print("\n=== READ ODOMETER (0x1818) ===")
    drain_bulk_in(dev)
    r = read_param(dev, 0x98, 0x18, seq, verbose=True, clean=True)
    if r and len(r) >= 6:
        payload_start = 5
        if payload_start < len(r):
            status = r[payload_start]
            if status == 0x08:
                fields = decode_protobuf_simple(r[payload_start:])
            else:
                fields = decode_protobuf_simple(r[payload_start + 1:])
            if 1 in fields:
                _, val = fields[1]
                print(f"  Odometer raw value: {val}")
                return val
    return None


def write_service_due(dev, timestamp: int, odometer: int, seq: int) -> bool:
    print(f"\n=== WRITE SERVICE_DUE ===")
    print(f"  timestamp: {timestamp}")
    print(f"  odometer:  {odometer}")

    pb_payload = build_service_due_protobuf(timestamp, odometer)
    print(f"  protobuf:  {pb_payload.hex()}")

    typeseq = (2 << 4) | (seq & 0x0F)
    src_hi, src_lo = 0x0E, 0x10
    dest_hi, dest_lo = 0xa1, 0x85

    body = bytes([src_hi, src_lo, dest_hi, dest_lo, typeseq]) + pb_payload
    print(f"  MB frame:  {body.hex()}")

    drain_bulk_in(dev)
    try:
        send_frame(dev, body, verbose=True)
    except Exception as e:
        print(f"  SEND ERROR: {e}")
        return False

    poll_until_data(dev, max_ms=500)
    try:
        ctrl_out(dev, 0x45)
    except Exception:
        pass
    reply = receive_frame(dev, verbose=True)

    if reply and len(reply) >= 5:
        print(f"  Reply: {reply.hex()}")
        typeseq = reply[4]
        resp_type = (typeseq >> 4) & 0x0F
        if len(reply) == 5 and resp_type == 3:
            print(f"  Status: WRITE_ACK (bare response = success)")
            return True
        if len(reply) >= 6:
            status_byte = reply[5]
            status_name = STATUS_CODES.get(status_byte, f"UNKNOWN({status_byte})")
            print(f"  Status: {status_byte} = {status_name}")
            return status_byte == 0
    else:
        print(f"  No valid reply received")

    return False


# ============================================================
# Main
# ============================================================
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true",
                    help="Write SERVICE_DUE (without --confirm it is a dry-run)")
    ap.add_argument("--confirm", action="store_true",
                    help="Confirm the write (safety)")
    ap.add_argument("--interval", type=int, default=1000,
                    help="Service interval in km (default: 1000)")
    ap.add_argument("--timestamp", type=int, default=None,
                    help="Custom timestamp (default: 3588192000 = far future)")
    ap.add_argument("--odometer", type=int, default=None,
                    help="Custom odometer value (default: current + interval)")
    args = ap.parse_args()

    dev = open_brc(verbose=True)
    init_session(dev, verbose=True)
    print("[INIT] BRC ready\n")
    drain_bulk_in(dev)

    seq = 1

    r = read_service_due(dev, seq)
    seq += 1
    if r:
        print(f"  Raw response: {r.hex()}")
        if len(r) >= 6:
            for offset in (5, 6):
                try:
                    fields = decode_protobuf_simple(r[offset:])
                    if fields:
                        print(f"  Parsed (offset {offset}): {fields}")
                        for fnum, (wtype, val) in sorted(fields.items()):
                            if fnum == 1 and wtype == 'varint':
                                from datetime import datetime, timezone
                                try:
                                    dt = datetime.fromtimestamp(val, tz=timezone.utc)
                                    print(f"    field {fnum}: {val} = {dt.isoformat()} (timestamp)")
                                except (ValueError, OSError):
                                    print(f"    field {fnum}: {val}")
                            elif fnum == 2 and wtype == 'varint':
                                print(f"    field {fnum}: {val} (odometer -- {val/1000:.1f} km)")
                            else:
                                print(f"    field {fnum}: {val}")
                except Exception:
                    pass

    odo = read_odometer(dev, seq)
    seq += 1

    if not args.reset:
        print("\n[INFO] Read-only mode. Use --reset --confirm to write.")
        return

    FAR_FUTURE_TS = 3588192000
    ts = args.timestamp if args.timestamp else FAR_FUTURE_TS

    if args.odometer is not None:
        odo_write = args.odometer
    elif odo is not None:
        interval_m = args.interval * 1000
        odo_write = odo + interval_m
    else:
        print("[ERROR] Cannot read odometer and no --odometer specified.")
        return

    print(f"\n{'='*60}")
    print(f"SERVICE RESET PLAN:")
    print(f"  timestamp:      {ts} ({'(far future)' if ts > 2000000000 else time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))})")
    print(f"  current km:     {odo / 1000:.1f} km" if odo else "")
    print(f"  next service:   {odo_write / 1000:.1f} km (+{args.interval} km interval)")
    print(f"  odometer raw:   {odo_write}")
    print(f"{'='*60}")

    if not args.confirm:
        print("\n[DRY-RUN] Add --confirm to actually write.")
        return

    ok = write_service_due(dev, ts, odo_write, seq)
    seq += 1

    if ok:
        print("\n[OK] SERVICE_DUE written. Service warning should be cleared.")
    else:
        print("\n[FAIL] Write failed or DENIED.")

    print("\n=== VERIFY: re-read SERVICE_DUE ===")
    time.sleep(0.5)
    r2 = read_service_due(dev, seq)
    if r2:
        print(f"  Verify response: {r2.hex()}")


if __name__ == '__main__':
    main()
