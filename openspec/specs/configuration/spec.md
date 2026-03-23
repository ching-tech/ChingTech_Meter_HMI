# 設定管理規格

## 設定檔案
- **路徑**: `config.json` (專案根目錄，Slave 為 `slave_test/config.json`)
- **編碼**: UTF-8
- **格式**: JSON
- **載入時機**: 模組 import 時 (`config.py` 底部 `config = load_config()`)

## 設定結構

### AppConfig (頂層)
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| version | str | "2.1.0" | 版本號 |
| title | str | "三橋耳溫槍探頭套檢測系統" | 視窗標題 |
| window_width | int | 1200 | 視窗寬度 |
| window_height | int | 850 | 視窗高度 |
| simulation_mode | bool | false | 模擬模式開關 |
| log_dir | str | "logs" | 量測記錄目錄 |

### BluetoothConfig
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| enabled | bool | true | 藍芽啟用 |
| device_addresses | List[str] | [""] * 6 | 6 支耳溫槍 MAC 位址 |
| reconnect_interval | float | 5.0 | 重連間隔 (秒) |
| timeout | float | 10.0 | 連線超時 (秒) |

### PLCConfig
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| enabled | bool | true | PLC 啟用 (Slave 設為 false) |
| ip_address | str | "127.0.0.1" | PLC IP 位址 |
| port | int | 5000 | PLC 通訊埠 |

### NetworkConfig
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| mode | str | "master" | 運行模式 ("master" / "slave") |
| master_ip | str | "127.0.0.1" | Master IP 位址 |
| port | int | 5001 | 網路通訊埠 |
| slave_meter_offset | int | 6 | Slave 通道偏移 |

### MeasurementConfig
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| tolerance_upper | float | 0.3 | 誤差上限 (°C) |
| tolerance_lower | float | 0.2 | 誤差下限 (°C) |
| meter_count | int | 12 | 總通道數 |
| channel_enabled | List[bool] | [true]*12 | 通道啟用狀態 |

### TimingConfig
| 欄位 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| empty_collect_delay | float | 0.5 | 空槍值收集延遲 (秒) |
| measure_collect_delay | float | 0.5 | 溫度量測收集延遲 (秒) |
| bt_request_interval | float | 0.1 | 藍芽請求間隔 (秒) |
| plc_poll_interval | float | 0.1 | PLC 輪詢間隔 (秒) |
| result_hold_time | float | 1.0 | 結果保持時間 (秒) |

## UI 設定面板
右側抽屜 (`build_settings_drawer`) 提供修改:
- 系統設定: 誤差上下限、運行模式
- 時序設定: 各延遲參數
- PLC 設定: IP/Port
- 藍芽設定: MAC 位址、重連間隔、超時
- 通道啟用: 12 通道獨立開關

### 操作按鈕
| 按鈕 | 顏色 | 行為 |
|------|------|------|
| 儲存進階設定 | 藍色 | 存檔 + 誤差/通道啟用即時生效 |
| 更新資料 (即時套用) | 綠色 | 存檔 + 所有參數即時套用到管理器 |

### 即時套用機制 (`on_apply_settings`)
不重啟管理器（避免執行緒衝突），只更新參數:
- **誤差容許值**: `measure_manager.set_tolerance()` 立即生效
- **通道啟用**: 更新 UI 顯示立即生效
- **藍芽 MAC**: 更新 `device.mac_address`，下次重連時生效
- **PLC IP/Port**: 更新 `plc_manager.ip_address/port`，下次連線時生效
- **網路參數**: 更新 `net_manager.master_ip/port`
- **時序參數**: 直接寫入 `config.timing.*`，下次觸發時讀取
