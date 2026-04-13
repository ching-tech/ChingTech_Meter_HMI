# Proposal: Validate MeterDataPacket from_json

## Why

`MeterDataPacket.from_json()` uses a bare `except:` that silently swallows all errors, making it impossible to diagnose malformed data from Slave devices. Invalid field values (wrong types, out-of-range channels) pass through unchecked, which could cause subtle downstream bugs in measurement processing.

## What Changes

- Replace bare `except:` with specific exception handling (`json.JSONDecodeError`, `TypeError`, `KeyError`)
- Add field validation: channel range (1-12), temperature is numeric, timestamp is positive
- Return `None` for invalid data but log the reason for easier debugging

## Capabilities

### Modified Capabilities
- `meter-data-packet-parsing`: `from_json()` now validates field types and ranges, and catches only expected exceptions

## Impact

- `network_comm.py`: Modify `MeterDataPacket.from_json()` method (~15-20 lines changed)
