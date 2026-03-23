# 三橋耳溫槍探頭套檢測系統

![Version](https://img.shields.io/badge/version-2.0.0-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

## 專案概述

本系統為 **三橋耳溫槍探頭套檢測系統** 的人機介面 (HMI) 應用程式，用於自動化檢測耳溫槍探頭套的溫度量測精度。系統支援 12 通道同時量測，採用 Master-Slave 架構實現雙機協作。

## 系統架構

```
+------------------------------------------------------------------+
|                         檢測系統架構                               |
+------------------------------------------------------------------+
|                                                                   |
|  +-----------------+                                              |
|  |   三菱 5U PLC    |                                              |
|  +--------+--------+                                              |
|           |                                                       |
|           | MC Protocol (觸發/結果)                                |
|           v                                                       |
|  +--------+--------+       TCP/IP        +-----------------+      |
|  |     Master      | <=================> |      Slave      |      |
|  |    (電腦 A)      |      Port:5001      |     (電腦 B)     |      |
|  |    通道 1-6      |                     |    通道 7-12     |      |
|  +--------+--------+                     +--------+--------+      |
|           |                                       |               |
|           | Bluetooth SPP                         | Bluetooth SPP |
|           v                                       v               |
|  +-----------------+                     +-----------------+      |
|  |   耳溫槍 1-6     |                     |   耳溫槍 7-12    |      |
|  +-----------------+                     +-----------------+      |
|                                                                   |
+-------------------------------------------------------------------+
```

### 通訊流程

```
1. PLC 發送觸發訊號
   [PLC] --MC Protocol--> [Master]

2. Master/Slave 同時量測
   [Master] --Bluetooth--> [耳溫槍 1-6] --溫度資料--> [Master]
   [Slave]  --Bluetooth--> [耳溫槍 7-12] --溫度資料--> [Slave]

3. Slave 傳送資料給 Master
   [Slave] --TCP/IP (通道7-12資料)--> [Master]

4. Master 彙整結果並回傳 PLC
   [Master] --MC Protocol (12通道結果)--> [PLC]
```

## 功能特色

- **12 通道同步量測**：支援 12 支耳溫槍同時進行溫度量測
- **Master-Slave 架構**：雙機協作，各負責 6 個通道
- **藍芽 SPP 通訊**：透過藍芽 SPP 協定與耳溫槍設備通訊
- **PLC 整合**：支援三菱 5U PLC MC Protocol 通訊
- **即時監控介面**：使用 NiceGUI 建構現代化深色主題介面
- **自動 PASS/FAIL 判定**：根據誤差容許範圍自動判定檢測結果
- **模擬模式**：無硬體時可使用模擬模式進行測試

## 目錄結構

```
3Bridge_Meter_HMI/
├── main.py              # 主程式與 UI 建構
├── config.py            # 設定管理模組
├── config.json          # 系統設定檔
├── bluetooth_comm.py    # 藍芽 SPP 通訊模組
├── plc_comm.py          # PLC MC Protocol 通訊模組
├── network_comm.py      # Master-Slave 網路通訊模組
├── measurement.py       # 量測邏輯與判定模組
└── README.md            # 專案說明文件
```

## 安裝需求

### 系統需求
- Windows 10/11
- Python 3.8 或以上版本
- 藍芽適配器 (支援 SPP 協定)

### Python 套件
```bash
pip install nicegui
pip install pyserial        # 藍芽序列通訊
pip install pymcprotocol    # 三菱 PLC 通訊 (選用)
```

## 快速開始

### 1. 安裝相依套件
```bash
pip install -r requirements.txt
```

### 2. 設定系統參數
編輯 `config.json` 設定以下參數：

```json
{
  "simulation_mode": false,
  "bluetooth": {
    "device_addresses": ["AA:BB:CC:DD:EE:01", "..."]
  },
  "plc": {
    "ip_address": "192.168.1.10",
    "port": 5000
  },
  "network": {
    "mode": "master",
    "master_ip": "192.168.1.100"
  },
  "measurement": {
    "tolerance_upper": 0.2,
    "tolerance_lower": -0.2
  }
}
```

### 3. 啟動系統
```bash
python main.py
```

## 設定說明

### 基本設定
| 參數 | 說明 | 預設值 |
|------|------|--------|
| `simulation_mode` | 模擬模式 (無硬體時使用) | `true` |
| `window_width` | 視窗寬度 | `1200` |
| `window_height` | 視窗高度 | `850` |

### 藍芽設定
| 參數 | 說明 | 預設值 |
|------|------|--------|
| `device_addresses` | 6 支耳溫槍的藍芽 MAC 位址 | `[]` |
| `reconnect_interval` | 斷線重連間隔 (秒) | `5.0` |
| `timeout` | 連線超時 (秒) | `10.0` |

### PLC 設定
| 參數 | 說明 | 預設值 |
|------|------|--------|
| `ip_address` | PLC IP 位址 | `192.168.1.10` |
| `port` | 通訊埠 | `5000` |
| `trigger_empty_addr` | 空槍量測觸發位址 | `M100` |
| `trigger_measure_addr` | 溫度量測觸發位址 | `M101` |
| `result_base_addr` | 結果輸出起始位址 | `M200` |
| `complete_addr` | 量測完成訊號位址 | `M300` |

### 網路設定
| 參數 | 說明 | 預設值 |
|------|------|--------|
| `mode` | 運作模式 (`master` / `slave`) | `master` |
| `master_ip` | Master 端 IP 位址 | `192.168.1.100` |
| `port` | 網路通訊埠 | `5001` |

### 量測設定
| 參數 | 說明 | 預設值 |
|------|------|--------|
| `tolerance_upper` | 誤差上限 (°C) | `0.2` |
| `tolerance_lower` | 誤差下限 (°C) | `-0.2` |
| `meter_count` | 總通道數 | `12` |

## 操作流程

1. **系統啟動**：點擊「開始」按鈕啟動藍芽連線、PLC 監控與網路通訊
2. **空槍量測**：PLC 發送空槍量測觸發訊號，系統記錄各通道空槍值
3. **溫度量測**：PLC 發送溫度量測觸發訊號，系統記錄量測值並計算誤差
4. **結果判定**：系統根據誤差容許範圍判定 PASS/FAIL
5. **結果回傳**：量測結果寫回 PLC 指定位址

## 量測判定邏輯

```
誤差值 = 量測值 - 空槍值

若 誤差下限 ≤ 誤差值 ≤ 誤差上限：
    結果 = PASS
否則：
    結果 = FAIL
```

## 通訊協定

### 藍芽 SPP 協定
- 使用 XOR CheckSum 驗證資料完整性
- 封包格式：`[STX][Data][CheckSum][ETX][EOT]`
- 設備類型碼：`REEB0001`

### PLC MC Protocol
- 支援三菱 5U 系列 PLC
- 使用 M 暫存器進行觸發與結果傳輸

### 網路通訊協定
- TCP/IP Socket 通訊
- JSON 格式資料封包

## 授權資訊

本專案為三橋公司專屬軟體，未經授權禁止複製或散佈。

## 版本歷程

### v2.0.0
- 重構系統架構，採用模組化設計
- 新增 Master-Slave 雙機架構
- 使用 NiceGUI 重新設計使用者介面
- 新增模擬模式功能
