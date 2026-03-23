# Design: 藍芽耳溫槍通訊與 PLC 整合

## Context
- 現有系統使用隨機模擬數據
- 需要連接真實耳溫槍設備 (藍芽 SPP)
- 需要與三菱 5U PLC 整合控制量測流程
- 單台電腦藍芽限制：最多 6 個 SPP 連線

## Goals / Non-Goals
**Goals:**
- 實現 12 支耳溫槍藍芽 SPP 通訊
- 整合三菱 5U PLC MC Protocol 觸發控制
- Master-Slave 架構整合兩台電腦數據
- 誤差範圍判斷 PASS/FAIL

**Non-Goals:**
- 不支援其他品牌 PLC
- 不支援 BLE 低功耗藍芽
- 不實作資料庫歷史記錄 (此版本)

## Bluetooth Protocol (耳溫槍通訊協定)

### 封包結構

#### Device → GatewayAP (DB: 傳送量測資料)
| 欄位 | 長度 | 類型 | 說明 |
|------|------|------|------|
| Head Code | 1 | ASCII | 0x02 (STX) |
| DeviceTypeID | 8 | ASCII | `REEB0001` (耳溫槍) |
| DeviceID | 10 | ASCII | 機器編號 |
| FuncID | 2 | ASCII | `DB` |
| DataLen | 2 | HEX | 資料長度 |
| Data | ? | - | 量測資料 |
| CheckSum | 1 | - | XOR (HeadCode~Data) |
| End Code | 2 | ASCII | 0x03 0x04 (ETX+EOT) |

#### GatewayAP → Device (CB: ACK 回應)
| 欄位 | 長度 | 類型 | 說明 |
|------|------|------|------|
| Head Code | 1 | ASCII | 0x02 (STX) |
| FuncID | 2 | ASCII | `CB` |
| Data | 1 | ASCII | `1`=成功, `2`=重送 |
| CheckSum | 1 | - | XOR |
| End Code | 2 | ASCII | 0x03 0x04 |

範例: `0x02 0x43 0x42 0x31 0x32 0x03 0x04`

#### GatewayAP → Device (CD: 主動要求量測)
| 欄位 | 長度 | 類型 | 說明 |
|------|------|------|------|
| Head Code | 1 | ASCII | 0x02 (STX) |
| FuncID | 2 | ASCII | `CD` |
| Data | 1 | ASCII | `1`=要求量測 |
| CheckSum | 1 | - | XOR |
| End Code | 2 | ASCII | 0x03 0x04 |

範例: `0x02 0x43 0x44 0x31 0x34 0x03 0x04`

### 溫度資料格式 (Data 欄位)
| 順序 | 欄位 | 長度 | 說明 |
|------|------|------|------|
| 1 | MeterID | 10 | 機器編號 |
| 2 | TEMP | 4 | 溫度值 (ASCII) |
| 3 | TRAN_TEMP | 4 | 估算溫度 |
| 4 | TEMPMode | 1 | 估算模式 |

**TEMP 格式**: 4 位 ASCII
- 第1碼：整數十位
- 第2碼：整數個位
- 第3碼：小數第一位
- 第4碼：小數第二位
- 例: `3650` = 36.50°C

### CheckSum 計算
```python
def calc_checksum(data: bytes) -> int:
    result = 0
    for b in data:
        result ^= b
    return result
```

## Decisions

### 1. 藍芽通訊方案
- **Decision**: 使用 Python 原生 `socket.AF_BLUETOOTH` + `BTPROTO_RFCOMM` 實作經典藍芽 SPP
- **Alternatives considered**:
  - PyBluez (需額外安裝，Windows 編譯複雜)
  - Windows COM Port (需手動配對，不夠自動化)
- **Rationale**: Python 3.9+ 內建支援，無需安裝額外套件

### 2. PLC 通訊方案
- **Decision**: 使用 pymcprotocol 套件
- **Rationale**: 專為三菱 MC Protocol 設計，支援 SLMP

### 3. Master-Slave 通訊方案
- **Decision**: TCP Socket 自定義協定
- **Data format**: JSON `{"meter_id": 7, "temperature": 36.5, "timestamp": "..."}`
- **Port**: 5000 (可設定)

### 4. 程式架構
```
ChingTech_Meter_HMI/
├── main.py              # 主程式入口
├── config.py            # 設定管理
├── bluetooth_comm.py    # 藍芽 SPP 通訊
├── plc_comm.py          # MC Protocol 通訊
├── network_comm.py      # Master-Slave 網路
├── measurement.py       # 量測邏輯
└── ui_components.py     # UI 元件
```

## PLC 暫存器規劃 (實際實作)
| 暫存器 | 用途 | 說明 |
|--------|------|------|
| D500 | 溫度量測觸發 | 1=觸發, 0=完成 |
| D515 | 空槍量測觸發 | 1=觸發, 0=完成 |
| D501-D512 | PASS/FAIL 結果 | 0=PASS, 1=FAIL |
| D513 | 藍芽錯誤狀態 | Bitmask (Bit0=CH1...Bit11=CH12) |
| D514 | PC 心跳 | 每秒切換 0/1 |
| D517-D528 | OK 計數 | 各通道 OK 次數 |
| D529-D540 | NG 計數 | 各通道 NG 次數 |
| D541 | 異常復歸 | HMI 異常復歸訊號 |

## Risks / Trade-offs
- **Risk**: 藍芽連線不穩定 → 實作自動重連機制
- **Risk**: 網路延遲影響同步 → 加入超時處理
- **Trade-off**: `socket.AF_BLUETOOTH` 僅支援 Windows，需 Python 3.9+

## Open Questions
- [x] ~~耳溫槍藍芽名稱/MAC Address 格式？~~ → 使用 DeviceID (10 位)
- [x] ~~耳溫槍資料格式？~~ → 已定義 (TEMP 4 bytes ASCII)
- [ ] PLC 暫存器位址是否已規劃？
