# 通訊協定規格

## 1. 藍芽 SPP 協定 (bluetooth_comm.py)

### 連線方式
- **協定**: Bluetooth RFCOMM (Serial Port Profile)
- **Socket**: `AF_BLUETOOTH` / `SOCK_STREAM` / `BTPROTO_RFCOMM`
- **頻道**: Channel 1 (SPP 標準)
- **連線超時**: 10 秒
- **資料超時**: 2 秒
- **設備類型**: `REEB0001` (耳溫槍識別碼)

### 封包格式
```
[STX=0x02][Data][CheckSum(XOR)][ETX=0x03][EOT=0x04]
```
- CheckSum = 從 STX 到 Data 結尾的 XOR

### 指令類型

#### CB (ACK 回應)
```
TX: 0x02 | "CB" | "1"(成功)/"2"(失敗) | CheckSum | 0x03 | 0x04
```
用途: 收到資料後回覆 ACK

#### CD (量測請求)
```
TX: 0x02 | "CD" | "1" | CheckSum | 0x03 | 0x04
```
用途: 主動要求耳溫槍執行量測

#### DB (量測資料)
```
RX: 0x02 | DeviceType(8B) | DeviceID(10B) | "DB" | DataLen(2B) | Data | CheckSum | 0x03 | 0x04

Data 欄位:
  MeterID(10B) | TEMP(4B) | TRAN_TEMP(4B) | TEMPMode(1B)

  TEMP: 例 "3650" → 36.50°C
  TRAN_TEMP: "1111"=有耳溫套, "0000"=無耳溫套
  TEMPMode: 估算模式
```

### 連線管理
- 每個設備獨立執行緒 (`_device_thread`)
- 斷線後等待 5 秒重試
- 連線失敗等待 10 秒 (避免 WinError 10048)
- 使用接收 buffer 處理封包分割

## 2. PLC MC Protocol (plc_comm.py)

### 連線方式
- **協定**: MC Protocol 3E (SLMP)
- **通訊庫**: `pymcprotocol.Type3E`
- **PLC 型號**: 三菱 FX5U
- **傳輸層**: TCP/IP
- **預設 IP**: 192.168.1.10:5000

### 暫存器配置 (D500~D541)

| 暫存器 | 偏移 | 方向 | 說明 |
|--------|------|------|------|
| D500 | 0 | R/W | 量測觸發 (PLC→PC: 1=觸發, PC→PLC: 0=完成) |
| D501~D512 | 1~12 | W | 通道 1-12 判定結果 (0=PASS, 1=FAIL) |
| D513 | 13 | W | 藍芽連線錯誤遮罩 (bit0~11 對應通道 1-12) |
| D514 | 14 | W | PC 心跳 (每秒切換 0↔1) |
| D515 | 15 | R/W | 空槍測試觸發 (PLC→PC: 1=觸發, PC→PLC: 0=完成) |
| D516 | 16 | R | 測試週期次數 (PLC 管理) |
| D517~D528 | 17~28 | R/W | 通道 1-12 OK 計數 (PLC 累加, HMI 換批歸零) |
| D529~D540 | 29~40 | R/W | 通道 1-12 NG 計數 (PLC 累加, HMI 換批歸零) |
| D541 | 41 | R | HMI 異常復歸 (1=觸發) |

### OK/NG 計數規則
- **PLC 負責累加**: HMI 只寫判定結果 (D501~D512)，PLC 根據判定自行累加 D517~D540
- **HMI 讀取顯示**: UI 的 OK/NG 計數從 PLC D517~D540 讀取 (僅 Master 顯示)
- **換批歸零**: HMI 換批時寫入 D517~D540 = 0

### 觸發偵測
- 掃描週期: 100ms
- 偵測方式: **上升緣** (前值=0, 當前值=1)
- D500 上升緣 → 量測觸發
- D515 上升緣 → 空槍觸發
- D541 上升緣 → 異常復歸
- 連續失敗 5 次才判定斷線 (防閃爍)

### 心跳機制
- D514 每秒切換 0/1
- PLC 端可監控 PC 是否存活

## 3. Master-Slave 網路通訊 (network_comm.py)

### 連線方式
- **協定**: TCP/IP
- **預設 Port**: 5001
- **Master**: 監聽 (Server)
- **Slave**: 連線 (Client)

### 資料封包 (MeterDataPacket)
JSON 格式，以換行符 `\n` 分隔:
```json
{
  "channel": 7,
  "meter_id": "REEB000001",
  "temperature": 36.50,
  "timestamp": 1708123456.789,
  "ear_cover": "1111",
  "bt_state": "connected"
}
```

| 欄位 | 型別 | 必要 | 說明 |
|------|------|------|------|
| channel | int | Y | 通道編號 (7-12 for Slave) |
| meter_id | str | Y | 設備 ID |
| temperature | float | Y | 溫度值 |
| timestamp | float | Y | 時間戳記 |
| ear_cover | str | N | 耳溫套狀態 ("1111"=有, "0000"=無, ""=未知) |
| bt_state | str | N | 藍芽連線狀態 ("connected"/"disconnected"/"error"/"connecting") |

### Slave 發送時機
1. **藍芽收到量測資料** (`on_bluetooth_data`): 完整封包含溫度、耳套、BT 狀態
2. **藍芽連線狀態變更** (`on_bluetooth_state`): 純狀態封包 (temperature=0, ear_cover="")

### Master 接收處理 (`on_network_data`)
- 解析 `bt_state` → 更新對應通道的藍芽連線圖示
- 解析 `ear_cover` → 更新對應通道的耳溫套顯示 + 儲存至 `ear_cover_statuses`
- 溫度資料存入 `_received_data` 緩衝供量測時取用
- 純 BT 狀態封包 (temperature=0, 無 ear_cover) 只記 log，不更新溫度

### 連線管理
- Master 使用 `SO_REUSEADDR`，`accept()` 超時 1 秒
- Slave 連線超時 5 秒，失敗後等 3 秒重試
- 接收使用行緩衝 (以 `\n` 切割完整 JSON)

### 資料流向
```
Slave 耳溫槍 → BluetoothManager → on_bluetooth_data()
  ├─ 更新 Slave UI (空槍值/量測值依量測狀態決定)
  ├─ 更新耳套顯示
  ├─ 儲存耳套狀態
  └─ NetworkManager.send_data(溫度+耳套+BT狀態)
       ↓ TCP/IP
Master NetworkManager._receive_loop() → on_network_data()
  ├─ _received_data[channel] → 量測時取用
  ├─ update_meter_bt_status() → 更新藍芽圖示
  ├─ update_meter_ear_cover() → 更新耳套顯示
  └─ ear_cover_statuses[ch] → CSV 記錄用

Slave on_bluetooth_state() (BT 狀態變更)
  └─ NetworkManager.send_data(純BT狀態封包)
       ↓ TCP/IP
Master on_network_data() → update_meter_bt_status()
```
