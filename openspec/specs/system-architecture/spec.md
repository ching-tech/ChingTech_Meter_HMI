# 系統架構規格

## 概覽
擎添耳溫槍探頭套檢測系統採用 Master-Slave 雙機架構，透過藍芽 SPP 連接 12 支耳溫槍，由 PLC 觸發量測流程，自動判斷 PASS/FAIL 並回傳結果。

## 系統組成

### 硬體架構
```
┌─────────────────┐                 ┌─────────────────┐
│  三菱 FX5U PLC  │◄───TCP/IP────►│    電腦 A        │◄──藍芽 SPP──► 耳溫槍 1-6
│                 │   MC Protocol   │   (Master)       │
│  D500~D541      │   Port 5000    │                  │◄──TCP/IP─────┐
└─────────────────┘                 └─────────────────┘   Port 5001   │
                                                                       │
                                    ┌─────────────────┐               │
                                    │    電腦 B        │───────────────┘
                                    │   (Slave)        │◄──藍芽 SPP──► 耳溫槍 7-12
                                    └─────────────────┘
```

### 軟體模組
| 模組 | 檔案 | 職責 |
|------|------|------|
| 主程式/UI | `main.py` | 系統協調、NiceGUI 介面、事件處理 |
| 設定管理 | `config.py` | config.json 讀寫、資料模型定義 |
| 藍芽通訊 | `bluetooth_comm.py` | SPP 連線、封包解析、模擬模式 |
| PLC 通訊 | `plc_comm.py` | MC Protocol 3E、暫存器讀寫、觸發偵測 |
| 網路通訊 | `network_comm.py` | Master-Slave TCP/IP 資料同步 (含 BT 狀態、耳套狀態) |
| 量測邏輯 | `measurement.py` | 狀態機、PASS/FAIL 判斷、CSV 記錄 |
| PLC 模擬器 | `plc_simulator.py` | NiceGUI 模擬 PLC 暫存器，可編輯 D516/D517~D540 |

## 通道對應表
內部通道 (1-12) 與顯示名稱的映射：
```
內部 1→CH11, 2→CH9, 3→CH7, 4→CH5, 5→CH3, 6→CH1
內部 7→CH12, 8→CH10, 9→CH8, 10→CH6, 11→CH4, 12→CH2
```
- Master 管理內部通道 1-6 (對應 CH11, CH9, CH7, CH5, CH3, CH1)
- Slave 管理內部通道 7-12 (對應 CH12, CH10, CH8, CH6, CH4, CH2)

## 運行模式
- **正常模式** (`simulation_mode: false`): 連接實體硬體
- **模擬模式** (`simulation_mode: true`): 所有通訊模組使用內建模擬，無需硬體
- **PLC 模擬器** (`plc_simulator.py`): 獨立 NiceGUI 程式 (port 8082)，模擬 SLMP TCP 通訊
  - 可編輯 D516 測試週期、D517~D540 OK/NG 計數
  - 按鈕觸發 D500/D515/D541
  - HMI 寫入暫存器時自動同步顯示

## 網路 Port 配置
| Port | 用途 |
|------|------|
| 5000 | PLC MC Protocol (SLMP TCP) |
| 5001 | Master-Slave 網路通訊 |
| 8080 | Master HMI UI |
| 8081 | Slave HMI UI |
| 8082 | PLC 模擬器 UI |
