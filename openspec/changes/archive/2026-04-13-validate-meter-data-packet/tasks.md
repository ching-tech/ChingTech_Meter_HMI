# Tasks: Validate MeterDataPacket from_json

## 1. Rewrite from_json with specific exception handling

- [x] 1.1 Replace bare `except:` with `except (json.JSONDecodeError, TypeError, KeyError, ValueError)`
- [x] 1.2 Add warning print on parse failure with error details

## 2. Add field validation

- [x] 2.1 Check required fields exist (`channel`, `meter_id`, `temperature`, `timestamp`)
- [x] 2.2 Validate `channel` is int in range 1-12
- [x] 2.3 Validate `temperature` is numeric (int or float)
- [x] 2.4 Validate `timestamp` is a positive number

## 3. Verify

- [x] 3.1 Confirm caller interface unchanged (returns Optional[MeterDataPacket])
- [x] 3.2 Confirm `to_json()` → `from_json()` round-trip still works
