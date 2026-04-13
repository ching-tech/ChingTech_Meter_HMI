# meter-data-packet-parsing Specification

## Purpose
TBD - created by archiving change validate-meter-data-packet. Update Purpose after archive.
## Requirements
### Requirement: Valid JSON parsing

`from_json()` SHALL successfully parse well-formed data packets.

#### Scenario: Complete valid packet

- **WHEN** `from_json()` receives a valid JSON string with all required fields (`channel`, `meter_id`, `temperature`, `timestamp`) and valid values
- **THEN** it MUST return a `MeterDataPacket` instance with correct field values
- **AND** optional fields (`ear_cover`, `bt_state`) MUST use provided values or defaults

### Requirement: Invalid JSON handling

`from_json()` SHALL gracefully handle malformed JSON strings.

#### Scenario: Non-JSON input

- **WHEN** `from_json()` receives a string that is not valid JSON
- **THEN** it MUST return `None`
- **AND** MUST log a warning with the parse error details

### Requirement: Missing fields handling

`from_json()` SHALL detect incomplete data packets.

#### Scenario: Missing required field

- **WHEN** `from_json()` receives valid JSON but missing one or more required fields (`channel`, `meter_id`, `temperature`, `timestamp`)
- **THEN** it MUST return `None`
- **AND** MUST log a warning indicating which fields are missing

### Requirement: Field validation

`from_json()` SHALL validate field types and value ranges.

#### Scenario: Invalid channel

- **WHEN** `channel` is not an integer in range 1-12
- **THEN** it MUST return `None`
- **AND** MUST log a warning about the invalid channel value

#### Scenario: Invalid temperature

- **WHEN** `temperature` is not a number (int or float)
- **THEN** it MUST return `None`
- **AND** MUST log a warning about the invalid temperature value

#### Scenario: Invalid timestamp

- **WHEN** `timestamp` is not a positive number
- **THEN** it MUST return `None`
- **AND** MUST log a warning about the invalid timestamp value

### Requirement: No bare except

`from_json()` SHALL only catch expected exceptions.

#### Scenario: Exception specificity

- **WHEN** any exception occurs during parsing
- **THEN** only `json.JSONDecodeError`, `TypeError`, `ValueError`, and `KeyError` SHALL be caught
- **AND** `SystemExit` and `KeyboardInterrupt` MUST never be suppressed

