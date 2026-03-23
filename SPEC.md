# 擎添耳溫槍探頭套檢測系統 — 系統規格書

> **版本**: 2.0.0
> **更新日期**: 2026-03-10

---

## 1. 系統概述

工業級 HMI 應用程式，用於檢測耳溫槍是否正確裝上探頭套。系統透過藍芽連接 12 支耳溫槍，透過 MC Protocol 與三菱 FX5U PLC 通訊，採用 Master/Slave 雙機架構。

- **前端框架**: NiceGUI (Python)，dark theme，以 native window 方式運行
- **PLC 通訊**: MC Protocol 3E (pymcprotocol)
- **藍芽通訊**: Windows RFCOMM (AF_BLUETOOTH / BTPROTO_RFCOMM)
- **Master/Slave 網路**: TCP Socket (JSON over newline-delimited stream)

---

## 2. 架構圖

```
                    ┌──────────────┐
                    │  三菱 FX5U   │
                    │    PLC       │
                    │ D500 ~ D542  │
                    └──────┬───────┘
                           │ MC Protocol 3E (TCP)
                           │
              ┌────────────┴────────────┐
              │      Master (PC-A)       │
              │      port 8080           │
              │  BT: CH11,9,7,5,3,1     │
              │  PLC 通訊 + 異常判定     │
              └────────────┬────────────┘
                           │ TCP (port 5001)
              ┌────────────┴────────────┐
              │      Slave (PC-B)        │
              │      port 8081           │
              │  BT: CH12,10,8,6,4,2    │
              │  純資料轉發，無判定       │
              └─────────────────────────┘
```

---

## 3. 檔案結構

| 檔案 | 說明 |
|---|---|
| `main.py` | 主程式：UI 建置、事件處理、異常檢測邏輯 |
| `config.py` | 設定管理：dataclass 定義、JSON 讀寫 |
| `plc_comm.py` | PLC MC Protocol 通訊模組 |
| `bluetooth_comm.py` | 藍芽 SPP RFCOMM 通訊模組 |
| `network_comm.py` | Master-Slave TCP 網路通訊模組 |
| `measurement.py` | 量測流程管理、PASS/FAIL 判定、CSV 批次記錄 |
| `config.json` | 使用者設定持久化檔案 |

---

## 4. 通道編號對應

系統使用 **internal channel** (1~12) 作為程式內部索引，與 PLC/UI 的 **logical channel** (CH1~CH12) 對應關係如下：

| Internal | Logical | 歸屬 | 藍芽連線 |
|---|---|---|---|
| 1 | CH11 | Master 本機 | Master BT slot 1 |
| 2 | CH9  | Master 本機 | Master BT slot 2 |
| 3 | CH7  | Master 本機 | Master BT slot 3 |
| 4 | CH5  | Master 本機 | Master BT slot 4 |
| 5 | CH3  | Master 本機 | Master BT slot 5 |
| 6 | CH1  | Master 本機 | Master BT slot 6 |
| 7 | CH12 | Slave 遠端  | Slave BT slot 1  |
| 8 | CH10 | Slave 遠端  | Slave BT slot 2  |
| 9 | CH8  | Slave 遠端  | Slave BT slot 3  |
| 10 | CH6 | Slave 遠端  | Slave BT slot 4  |
| 11 | CH4 | Slave 遠端  | Slave BT slot 5  |
| 12 | CH2 | Slave 遠端  | Slave BT slot 6  |

> 對應表定義於 `config.py` → `CHANNEL_DISPLAY_NAMES`

---

## 5. PLC 暫存器配置 (D500 ~ D542)

### 5.1 暫存器總表

| 暫存器 | 偏移 | 方向 | 說明 |
|---|---|---|---|
| D500 | 0 | PLC→HMI (R) / HMI→PLC (W:0) | 量測觸發 (1=觸發, HMI 寫 0=完成) |
| D501~D512 | 1~12 | HMI→PLC | 通道 1~12 判定結果 |
| D513 | 13 | HMI→PLC | 異常狀態 bitmask |
| D514 | 14 | HMI→PLC | PC 心跳 (每秒 0/1 交替) |
| D515 | 15 | PLC→HMI (R) / HMI→PLC (W:0) | 空槍量測觸發 |
| D516 | 16 | PLC→HMI | 測試週期次數 |
| D517~D528 | 17~28 | HMI→PLC | 通道 1~12 OK 計數 |
| D529~D540 | 29~40 | HMI→PLC | 通道 1~12 NG 計數 |
| D541 | 41 | PLC→HMI | HMI 異常復歸訊號 |
| D542 | 42 | PLC→HMI | 暖槍訊號 |

### 5.2 D501~D512 判定值

| 值 | 意義 |
|---|---|
| 0 | 不使用 (通道停用) |
| 1 | FAIL |
| 2 | PASS |

### 5.3 D513 異常 bitmask

| Bit | 說明 | 設定條件 |
|---|---|---|
| bit0~bit11 | 藍芽連線錯誤 (通道 1~12) | 對應通道 DISCONNECTED / ERROR / CONNECTING |
| bit12 | 溫度異常 | 量測溫度超出 temp_anomaly_upper/lower |
| bit13 | 連續無套異常 | 任一通道連續無套達 no_cover_anomaly_count |
| bit14 | 空槍值異常 | 空槍值超出 empty_upper/lower |

### 5.4 讀取方式

- 批次讀取 D500~D542 共 43 個 word (`D_READ_SIZE = 43`)
- 掃描週期: 100ms
- 心跳: D514 每秒 0/1 交替

---

## 6. 觸發條件與流程

### 6.1 量測觸發 (D500 上升緣)

```
PLC 寫 D500=1
  → HMI 偵測上升緣
  → on_plc_measure_trigger()
    → 清除 UI 量測欄位 (clear_meter_values)
    → measure_manager.start_temperature_measurement()
    → 通知 Slave: send_command("request_measure")
    → 對本機所有已啟用通道發送 CD 指令 (request_measurement)
    → 延遲 measure_collect_delay 後 collect_measure_values()
      → 收集本機 BT 資料 + Slave 網路資料
      → 異常檢測 (僅 Master):
        → check_temp_anomaly() — 逐通道
        → check_no_cover_anomaly() — 逐通道
      → measure_manager.record_measure_values()
  → on_measurement_complete()
    → 計算 PASS/FAIL
    → 寫入 D501~D512 判定結果
    → 寫入 D500=0 (write_complete_signal)
    → 寫入批次 CSV Log
    → 更新 PLC OK/NG 計數
```

### 6.2 空槍量測觸發 (D515 上升緣)

```
PLC 寫 D515=1
  → HMI 偵測上升緣
  → on_plc_empty_trigger()
    → 清除 UI 空槍欄位
    → measure_manager.start_empty_measurement()
    → 通知 Slave: send_command("request_empty")
    → 對本機通道發送 CD 指令
    → 延遲 empty_collect_delay 後 collect_empty_values()
      → 收集本機 BT 資料 + Slave 網路資料
      → 空槍值超限檢測 (僅 Master):
        → 暖槍中 (D542=1): 累計超限次數，達 3 次 → 警報 + D513 bit14 ON
        → 非暖槍 (D542=0): 任一次超限 → 立即警報 + D513 bit14 ON
      → measure_manager.record_empty_values()
      → 寫入批次 CSV Log
      → 清除 D515=0
```

### 6.3 異常復歸 (D541 上升緣)

```
PLC 寫 D541=1
  → HMI 偵測上升緣
  → on_plc_reset()
    → clear_d513() — D513 整個字組清為 0
    → 清除所有異常狀態變數:
      → temp_anomaly_active = False
      → no_cover_anomaly_active = False
      → empty_out_of_range_count = 0
      → no_cover_consecutive 全部清零
    → 清除 UI 無套計數顯示 (歸零)
    → 清除所有通道 highlight
    → stop_alert_flash() — 隱藏警報橫幅
```

### 6.4 HMI 異常復歸按鈕

```
使用者按下「異常復歸」按鈕 (Master 模式 UI)
  → on_reset_button_click()
    → 直接呼叫 on_plc_reset() (不寫 D541，避免與 PLC 寫入衝突)
```

> **注意**: D541 由 PLC 獨佔寫入，HMI 只讀取。HMI 按鈕直接執行復歸邏輯。

### 6.5 暖槍訊號 (D542)

| 狀態 | 行為 |
|---|---|
| D542=1 (暖槍中) | 空槍超限採累計模式，連續 3 次才觸發警報 |
| D542: 1→0 (暖槍結束) | 自動歸零 `empty_out_of_range_count`，清除 D513 bit14 |
| D542=0 (非暖槍) | 空槍超限任一次立即警報 |

---

## 7. 異常檢測邏輯 (僅 Master 執行)

### 7.1 溫度異常 (D513 bit12)

- **觸發時機**: `collect_measure_values()` (D500=1 流程)
- **條件**: `config.measurement.temp_anomaly_enabled == True`
- **判定**: 量測溫度 > `temp_anomaly_upper` 或 < `temp_anomaly_lower`
- **動作**:
  - D513 bit12 ON
  - 異常通道 highlight (紅色邊框)
  - UI 警報橫幅 + Alarm Log
- **設定**: 溫度異常上下限、使用開關 (config.json 持久化)

### 7.2 連續無套異常 (D513 bit13)

- **觸發時機**: `collect_measure_values()` (D500=1 流程)
- **計數邏輯** (不論開關是否啟用，持續計數):
  - `trans_temp_raw == "0000"` → 該通道計數 +1
  - `trans_temp_raw == "1111"` → 該通道計數歸零
- **異常觸發**: `config.measurement.no_cover_anomaly_enabled == True` 且任一通道計數 >= `no_cover_anomaly_count`
- **動作**:
  - D513 bit13 ON
  - 異常通道 highlight
  - UI 警報橫幅 + Alarm Log
- **恢復**: 所有通道計數 < threshold → D513 bit13 OFF
- **UI 顯示**: 各通道即時顯示連續無套次數 (橘色數字)

### 7.3 空槍值異常 (D513 bit14)

- **觸發時機**: `collect_empty_values()` (D515=1 流程)
- **判定**: 空槍溫度 > `empty_upper` 或 < `empty_lower`
- **暖槍模式** (D542=1):
  - 累計超限次數 `empty_out_of_range_count`
  - 達 3 次 → D513 bit14 ON + 警報
  - D542: 1→0 時自動歸零
- **非暖槍模式** (D542=0):
  - 任一次超限 → D513 bit14 ON + 警報
- **恢復**: 空槍值正常 → 累計歸零 + D513 bit14 OFF
- **動作**: 異常通道 highlight + UI 警報 + Alarm Log

### 7.4 藍芽連線異常 (D513 bit0~bit11)

- **觸發時機**: 藍芽狀態變更回呼 (`on_bluetooth_state` / `on_network_data`)
- **條件**: 通道已啟用且狀態為 DISCONNECTED / ERROR / CONNECTING
- **動作**: 對應 bit ON，UI 警報 + Alarm Log
- **恢復**: 狀態恢復為 CONNECTED → 對應 bit OFF

### 7.5 PLC 連線異常

- **觸發時機**: `on_plc_state()` 狀態變更
- **條件**: 狀態為 DISCONNECTED 或 ERROR
- **動作**: UI 警報橫幅 + Alarm Log
- **恢復**: 狀態恢復為 CONNECTED → Log 記錄

### 7.6 網路連線異常

- **觸發時機**: `on_network_state()` 狀態變更
- **條件**: 狀態為 DISCONNECTED 或 ERROR
- **動作**: UI 警報橫幅 + Alarm Log
- **恢復**: 狀態恢復為 CONNECTED → Log 記錄

---

## 8. 通道 Highlight 機制

異常發生時，對應通道列加上紅色背景邊框 (`bg-red-900/40 border border-red-500 rounded`)，讓使用者快速辨識問題通道。

| 設定來源 | 清除來源 |
|---|---|
| `check_temp_anomaly()` — 溫度超限的通道 | 同函式 — 溫度恢復正常時 |
| `check_no_cover_anomaly()` — 無套達閾值的通道 | 同函式 — 計數低於閾值時 |
| `collect_empty_values()` — 空槍超限的通道 | 同函式 — 空槍值正常時 |
| | `on_plc_reset()` — 復歸時全部清除 |

---

## 9. Master/Slave 通訊協議

### 9.1 角色分工

| 功能 | Master | Slave |
|---|---|---|
| PLC 通訊 | ✅ 連接 + 讀寫 | ❌ 不連接 |
| 藍芽通道 | CH11,9,7,5,3,1 (internal 1-6) | CH12,10,8,6,4,2 (internal 7-12) |
| 異常判定 | ✅ 所有 12 通道 | ❌ 不判定 |
| 量測觸發 | ✅ 由 PLC D500/D515 觸發 | 由 Master 指令觸發 |
| CSV Log | ✅ 寫入 | ❌ 不寫入 |
| 網路角色 | TCP Server (listen) | TCP Client (connect) |

### 9.2 封包格式

**MeterDataPacket** (Slave → Master):
```json
{
  "channel": 7,
  "meter_id": "REEB001234",
  "temperature": 36.50,
  "timestamp": 1710000000.0,
  "ear_cover": "1111",
  "bt_state": "connected"
}
```

**Command** (Master → Slave):
```json
{"type": "command", "command": "request_empty"}
{"type": "command", "command": "request_measure"}
```

**Channel Enabled** (Slave → Master):
```json
{"type": "channel_enabled", "channels": {"7": true, "8": true, ...}}
```

### 9.3 Slave 藍芽狀態同步

- 藍芽狀態變更時，Slave 即時傳送 BT 狀態封包給 Master
- 傳送失敗時加入 `_pending_bt_sync` 待補送佇列
- 網路恢復連線時 (`on_network_state(CONNECTED)`)，批次補送所有通道狀態
- `update_plc_display()` 每 500ms 嘗試重送待補送項目

---

## 10. 藍芽通訊

### 10.1 連線管理

- **連線方式**: 單一 `_connection_manager` thread 依序連接所有設備（避免並行連線干擾 Windows BT stack）
- **連線成功後**: 等待 3 秒讓 Windows 藍芽堆疊穩定
- **重連機制**: 連續失敗次數累加，失敗越多等待越久 (最長 30 秒)
- **舊 Socket 清理**: 連線前強制關閉舊 socket (`_connect_device`)

### 10.2 協議封包

| 封包 | 方向 | 說明 |
|---|---|---|
| DB | 耳溫槍→HMI | 量測資料 (設備 ID + 溫度 + 耳套狀態) |
| CB | HMI→耳溫槍 | ACK 回應 |
| CD | HMI→耳溫槍 | 主動要求量測 |

### 10.3 資料格式

- `trans_temp_raw == "1111"` → 有耳溫套
- `trans_temp_raw == "0000"` → 無耳溫套
- 溫度值: 4 位字串，前 2 位整數 + 後 2 位小數 (例: "3650" → 36.50°C)

### 10.4 關閉流程

```
handle_shutdown()
  → bt_manager.stop()
    → 關閉正在 connect() 的 socket
    → 關閉已連線的 socket
  → 等待所有 BT thread 結束 (join timeout=3s)
  → plc_manager.stop_monitoring()
  → net_manager.stop()
  → os._exit(0)
```

---

## 11. 量測判定邏輯

### 11.1 流程

```
空槍量測 (D515=1) → 記錄各通道空槍基準溫度
溫度量測 (D500=1) → 記錄量測溫度 → 計算誤差 = 量測值 - 空槍值
  → |誤差| 在容許範圍內 → PASS
  → |誤差| 超出容許範圍 → FAIL
```

### 11.2 判定模式

| 模式 | D501~D512 寫入 |
|---|---|
| NORMAL | 依實際判定: 2=PASS, 1=FAIL, 0=不使用 |
| FORCE_OK | 全部強制 PASS |
| FORCE_NG | 全部強制 FAIL |

### 11.3 結果寫入

```python
for internal_ch in range(1, 13):
    logical_idx = int(CH名稱.replace('CH','')) - 1
    if 通道啟用:
        logical_results[logical_idx] = 2 if PASS else 1
    else:
        logical_results[logical_idx] = 0  # 不使用
# 寫入 D501~D512
plc_manager.write_results(logical_results)
# 完成後清除觸發
plc_manager.write_complete_signal()  # D500=0
```

---

## 12. 設定系統

### 12.1 設定結構 (config.json)

```
AppConfig
├── version: str = "2.0.0"
├── title: str
├── window_width / window_height: int
├── simulation_mode: bool
├── log_dir: str = "logs"
├── current_batch: str
├── bluetooth: BluetoothConfig
│   ├── enabled: bool
│   ├── device_addresses: List[str]  (6 個 MAC)
│   ├── reconnect_interval: float = 5.0
│   └── timeout: float = 10.0
├── plc: PLCConfig
│   ├── enabled: bool
│   ├── ip_address: str
│   └── port: int
├── network: NetworkConfig
│   ├── mode: "master" | "slave"
│   ├── master_ip: str
│   ├── port: int = 5001
│   └── slave_meter_offset: int = 6
├── measurement: MeasurementConfig
│   ├── tolerance_upper / tolerance_lower: float
│   ├── empty_upper / empty_lower: float
│   ├── meter_count: int = 12
│   ├── channel_enabled: List[bool]  (12 個)
│   ├── temp_anomaly_enabled: bool
│   ├── temp_anomaly_upper / temp_anomaly_lower: float
│   ├── no_cover_anomaly_enabled: bool
│   └── no_cover_anomaly_count: int
└── timing: TimingConfig
    ├── empty_collect_delay: float = 0.5
    ├── measure_collect_delay: float = 0.5
    ├── bt_request_interval: float = 0.1
    ├── plc_poll_interval: float = 0.1
    └── result_hold_time: float = 1.0
```

### 12.2 設定面板

- 需密碼登入 (`36274806`) 才能存取進階設定
- 支援「儲存」(寫入 config.json) 和「即時套用」(不重啟程式)
- Slave 模式可傳送通道啟用狀態給 Master，Master 會檢查一致性

### 12.3 向後相容

- `_dict_to_config()` 使用欄位過濾，忽略舊版 config.json 中不存在的 key
- `channel_enabled` 不足 12 個時自動補 True

---

## 13. 記錄系統

### 13.1 批次 CSV Log

- 目錄: `logs/`
- 檔名格式: 由 `measurement.py` 管理 (可透過 `start_new_batch` 換批)
- 僅 Master 寫入
- 換批時 PLC OK/NG 計數歸零 (D517~D540)

### 13.2 Alarm Log

- 目錄: `D:\logs\Alarm\`
- 檔名格式: `alarm_YYYYMMDD.txt`
- 一天一個檔案
- 格式: `[YYYY-MM-DD HH:MM:SS] 訊息內容`
- 所有經由 `show_alert()` 觸發的警報都會記錄

### 13.3 Debug Log

- 目錄: `logs/`
- 檔名格式: `debug_YYYYMMDD_HHMMSS.txt`
- 程式啟動時建立，stdout/stderr 同時輸出到檔案 (`_TeeWriter`)

---

## 14. UI 結構

### 14.1 頂部工具列

| 元件 | 說明 |
|---|---|
| 系統標題 + 版本 | "擎添耳溫槍探頭套檢測系統 v2.0.0" |
| MASTER/SLAVE 標記 | 藍色/橘色 badge |
| 目前批次 + 換批按鈕 | 僅 Master |
| 測試週期 | 顯示 D516 值，僅 Master |
| 系統狀態 | 運行中 (綠) / 已停止 (紅) |
| 判定模式 | 正常判定 / 強制OK / 強制NG，僅 Master |
| 暖槍 | D542: "暖槍中" (橘) / "OFF" (灰)，僅 Master |
| PLC 狀態燈 | 綠/黃/紅，僅 Master |
| 網路狀態燈 | 綠/黃/紅 |
| 異常復歸按鈕 | 琥珀色，僅 Master |
| 設定齒輪按鈕 | 開啟設定面板 |

### 14.2 警報橫幅

- 全寬紅色橫幅，0.5 秒閃爍
- 顯示最新異常訊息
- 「確認」按鈕可關閉

### 14.3 通道顯示區塊

**Master 模式**: 兩個區塊
- 本機通道 (CH11, 9, 7, 5, 3, 1)
- Slave 通道 (CH12, 10, 8, 6, 4, 2)

**Slave 模式**: 一個區塊
- 本機通道 (CH12, 10, 8, 6, 4, 2)

**每個通道列包含**:

| 元件 | 說明 |
|---|---|
| CH 名稱 | 例: "CH11" |
| BT 狀態圖示 | bluetooth 圖示，藍/黃/紅/灰 |
| 耳溫套狀態 | "有" (綠) / "無" (紅) |
| 空槍值 | 數值顯示 |
| 量測溫度 | 數值顯示 |
| 誤差 | 數值顯示 |
| PASS/FAIL 燈號 | 綠燈 PASS / 紅燈 FAIL / 灰燈 WAIT |
| OK/NG 計數 | 從 PLC D517~D540 讀取 |
| 連續無套計數 | 橘色數字，僅 Master |
| 停用標記 | 通道停用時顯示，整列半透明 |
| **Highlight** | 異常時紅色邊框背景 |

### 14.4 右側面板

| 區塊 | 說明 |
|---|---|
| 目前設定 | 顯示誤差範圍、空槍範圍等 |
| PLC 暫存器監控 | 顯示 D500~D542 即時數值 (僅 Master) |
| 系統 Log | 即時系統訊息 + 清空按鈕 |
| 模擬按鈕 | 空槍量測 / 溫度量測 (模擬模式) |

---

## 15. 執行方式

```bash
# Master 模式 (預設)
python main.py

# Slave 模式 (指定設定檔)
python main.py --config slave_test/config.json

# Master: http://localhost:8080
# Slave:  http://localhost:8081
```

---

## 16. 狀態機

### 16.1 量測狀態 (MeasurementState)

```
IDLE → WAITING_EMPTY → EMPTY_DONE → WAITING_MEASURE → MEASURING → COMPLETE → IDLE
```

### 16.2 藍芽連線狀態 (ConnectionState)

```
DISCONNECTED ⇄ CONNECTING → CONNECTED
                    ↓
                  ERROR → DISCONNECTED (重連)
```

### 16.3 PLC 連線狀態 (PLCConnectionState)

```
DISCONNECTED → CONNECTING → CONNECTED
     ↑                         ↓
     └──── ERROR ←─────────────┘ (連續 5 次失敗)
```

### 16.4 網路連線狀態 (NetworkState)

```
Master: DISCONNECTED → LISTENING → CONNECTED → LISTENING (Slave 斷線)
Slave:  DISCONNECTED → CONNECTED → ERROR → DISCONNECTED (重連)
```

---

## 17. 時序圖

### 17.1 溫度量測完整流程

```
PLC          Master HMI           Slave HMI         耳溫槍(x12)
 │                │                   │                  │
 │── D500=1 ─────→│                   │                  │
 │                │── request_measure→│                  │
 │                │── CD 指令 ──────────────────────────→│ (本機 6 支)
 │                │                   │── CD 指令 ─────→│ (Slave 6 支)
 │                │                   │←── DB 溫度資料 ──│
 │                │←─ MeterDataPacket ─│                  │
 │                │←─────── DB 溫度資料 ─────────────────│
 │                │                   │                  │
 │                │ collect_measure_values()              │
 │                │ check_temp_anomaly() x12              │
 │                │ check_no_cover_anomaly() x12          │
 │                │ record_measure_values()               │
 │                │ on_measurement_complete()              │
 │                │                   │                  │
 │←─ D501~D512 ──│ (判定結果)         │                  │
 │←─ D500=0 ─────│ (量測完成)         │                  │
 │                │                   │                  │
```

### 17.2 空槍量測完整流程

```
PLC          Master HMI           Slave HMI         耳溫槍(x12)
 │                │                   │                  │
 │── D515=1 ─────→│                   │                  │
 │                │── request_empty ──→│                  │
 │                │── CD 指令 ──────────────────────────→│
 │                │                   │── CD 指令 ─────→│
 │                │                   │←── DB 溫度資料 ──│
 │                │←─ MeterDataPacket ─│                  │
 │                │←─────── DB 溫度資料 ─────────────────│
 │                │                   │                  │
 │                │ collect_empty_values()                │
 │                │ 空槍超限檢測 (暖槍/非暖槍)             │
 │                │ record_empty_values()                 │
 │                │ save_cycle_log()                      │
 │                │                   │                  │
 │←─ D515=0 ─────│ (空槍完成)         │                  │
 │                │                   │                  │
```
