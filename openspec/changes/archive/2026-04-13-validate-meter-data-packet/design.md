# Design: Validate MeterDataPacket from_json

## Context

`MeterDataPacket.from_json()` is the single entry point for deserializing JSON data received over the Master-Slave TCP link. It's called from `_handle_master_received()` on every incoming data line. Currently it uses a bare `except:` that silently returns `None` for any error.

## Goals / Non-Goals

**Goals:**
- Catch only expected exceptions (JSONDecodeError, TypeError, ValueError, KeyError)
- Validate required fields exist and have reasonable values
- Log warnings for invalid data to aid debugging

**Non-Goals:**
- Changing the `Optional[MeterDataPacket]` return type or caller interface
- Adding retry/recovery logic for bad packets
- Adding a logging framework — use `print()` consistent with the rest of the codebase

## Decisions

### Decision 1: Keep print() for warnings

The codebase uses `print()` throughout for status messages (e.g., `network_comm.py:124`, `182`, `194`). We'll use `print()` with a `[WARN]` prefix for validation failures to stay consistent, rather than introducing `logging` for a single method.

### Decision 2: Validate inside from_json, not in caller

Validation belongs in `from_json()` since it's the deserialization boundary. The caller (`_handle_master_received`) should continue to just check `if packet:` — no interface change needed.

### Decision 3: Separate parse and validate steps

First parse JSON, then validate fields. This gives clearer error messages: "invalid JSON" vs "channel out of range 15".
