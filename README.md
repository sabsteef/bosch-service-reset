# Bosch Smart System (BES3) Service Reset

Reset the service/maintenance warning on Bosch Smart System e-bikes via USB,
without a dealer account or the Bosch DiagTool.

## What it does

Bosch BES3 e-bikes store a `SERVICE_DUE` data point in the RemoteControl (BRC3600).
When the odometer passes that value, the display shows a service warning.
Dealers reset this via the Bosch DiagTool after maintenance.

This script reads and writes that data point directly via USB.

## Hardware

- Bosch BES3 e-bike with BRC3600 LED Remote (on the handlebar)
- USB cable to the BRC3600 (VID `0x108C`, PID `0x01C0`)
- Bike powered on

## Two ways to use it

### 🌐 Option 1 — Web version (easiest, no install)

Just plug in the USB cable and open the website:

**➜ [tune.pc-online.me](https://tune.pc-online.me)**

- Works in any modern **Chrome** or **Edge** browser (WebUSB required)
- No Python, no libusb, no command line
- The **service reset feature is free** — no license required
- Available in Dutch 🇳🇱 and English 🇬🇧
- Steps: connect BRC3600 via USB → click "Connect to bike" → open the "Reset service warning" panel → read status → pick interval → reset

The site also offers paid speed-tuning services which do require a license,
but the service reset shown in this repo is free to use.

### 🐍 Option 2 — Python script (this repo)

For advanced users, offline use, scripting, or if you don't want to trust
a third-party website with your bike:

```bash
# Read current service status and odometer (safe, read-only)
python3 reset_service.py

# Dry-run: show what would be written (default: +1000 km)
python3 reset_service.py --reset

# Actually write
python3 reset_service.py --reset --confirm

# Different interval (e.g. next service in 2000 km)
python3 reset_service.py --reset --confirm --interval 2000
```

### Example output

```
SERVICE RESET PLAN:
  timestamp:     3588192000 ((far future))
  current km:    509.6 km
  next service:  1509.6 km (+1000 km interval)
  odometer raw:  1509589
```

## Protocol details

### SERVICE_DUE (address 0x2185)

- **Service**: `0xa1` (RemoteControl)
- **Parameter**: `0x85`
- **Access**: ReadableWritableSubscribable — no signing required
- **Protobuf**:

```protobuf
message Timestamp {
  int64 value = 1;
}

message ServiceDue {
  Timestamp timestamp = 1;  // unix seconds, or far future for distance-only
  int32 odometer = 2;       // meters
}
```

### ODOMETER (address 0x1818)

- **Service**: `0x98` (DriveUnit)
- **Parameter**: `0x18`
- **Unit**: meters

### Key finding

`SERVICE_DUE` is a **writable** data point that requires **no** cryptographic signing
(unlike speed region or assist mode changes which need ECDSA-signed containers).
The DiagTool writes it via a plain MessageBus SET command.

A successful write returns a bare 5-byte WRITE_ACK (type=3, no status byte).
The script verifies by re-reading SERVICE_DUE after the write.

### ⚠ One-way only — you cannot lower the service due

The Bosch bike **only accepts SERVICE_DUE writes that INCREASE the value**.
If you send a lower value:

- The BRC still returns an ARM ack for the frame (looks fine at the transport level)
- The bike sends **no response frame** and does **not persist** the new value
- On some hosts the USB pipe stays stuck (Chrome WebUSB is especially prone) —
  a fresh session / hard refresh is needed to recover

The default flow of this script (`--interval N`) adds the interval to the
**current odometer** for the next service, which is almost always higher than
the previous due. If you want to postpone the next service by a fixed amount
without moving the odometer baseline, add the interval to the existing due
instead (read SERVICE_DUE first, then `--odometer <current_due + N*1000>`).

### Factory defaults

- First service interval: **500 km**
- Timestamp: `3588192000` (2083-09-15) = only distance matters
- Subsequent intervals: typically 1000-2000 km

## Dependencies

- Python 3.10+
- PyUSB (`pip install pyusb`)
- libusb (macOS: `brew install libusb`)

## Disclaimer

This is for personal diagnostic use on your own bike.
Service intervals exist for safety — have your bike serviced
by a qualified mechanic at appropriate intervals.

## License

GPL-3.0 — see [LICENSE](LICENSE)
