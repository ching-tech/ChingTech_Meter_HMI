# Change: 新增藍芽耳溫槍通訊與 PLC 整合功能

## Why
目前系統使用模擬數據，需要實現真實的藍芽 SPP 通訊連接 12 支耳溫槍，並整合三菱 5U PLC 的觸發訊號控制量測流程，完成探頭套自動化檢測。

## What Changes
- **新增藍芽 SPP 通訊模組** - 連接 6 支耳溫槍 (每台電腦)
- **新增 MC Protocol 通訊模組** - 與三菱 5U PLC 通訊
- **新增 Master-Slave 網路通訊** - 電腦 A 收集電腦 B 的數據
- **修改 UI** - 新增誤差上下限設定、空槍值顯示、連線狀態
- **修改判斷邏輯** - 從固定閾值改為誤差範圍判斷
- **新增 PLC 結果回傳** - 將 12 通道 PASS/FAIL 結果寫入 PLC

## Impact
- Affected specs: `thermometer-measurement` (新建)
- Affected code: `main.py` (重構為模組化架構)
- New files:
  - `bluetooth_comm.py` - 藍芽 SPP 通訊
  - `plc_comm.py` - MC Protocol 通訊
  - `network_comm.py` - Master-Slave 網路通訊
  - `config.py` - 設定管理
