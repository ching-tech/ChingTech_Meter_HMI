# Project Context

## Purpose
三橋耳溫槍探頭套檢測系統 - 自動化檢測 12 支耳溫槍探頭套品質。透過比較空槍值與溫度量測值的誤差，判斷探頭套是否合格，將結果回傳 PLC 執行分選：OK 套出貨，FAIL 套銷毀。

## Tech Stack
- **Python 3.x** - 主要開發語言
- **NiceGUI** - UI 框架 (Native 桌面模式)
- **經典藍芽 SPP** - Serial Port Profile 連接耳溫槍
- **MC Protocol (SLMP)** - 三菱 PLC 通訊協定 (TCP/IP)

## System Architecture
```
┌─────────────────┐                 ┌─────────────────┐
│  三菱 5U PLC    │◄───TCP/IP────►│    電腦 A       │◄──藍芽 SPP──► Meter 1-6
│                 │   MC Protocol   │   (Master)      │
│  1. 空槍量測訊號│                 │                 │◄──網路────────┐
│  2. 溫度量測訊號│                 │  收集 12 支數據 │               │
│  3. 接收結果    │                 │  判斷 PASS/FAIL │               │
│  4. 控制分選    │                 │  回傳 PLC       │               │
│  5. 累加OK/NG   │                 └─────────────────┘               │
└─────────────────┘                                                    │
                                    ┌─────────────────┐               │
                                    │    電腦 B       │───────────────┘
                                    │   (Slave)       │◄──藍芽 SPP──► Meter 7-12
                                    │  傳送溫度+耳套  │
                                    │  +藍芽連線狀態  │
                                    └─────────────────┘
```

## Testing Logic (判斷邏輯)
```
誤差值 = 溫度量測值 - 空槍值

若 誤差下限 ≤ 誤差值 ≤ 誤差上限 → PASS
否則 → FAIL
```
- **空槍值**: PLC 發送「空槍量測訊號」時擷取 (無探頭套)
- **溫度量測值**: PLC 發送「溫度量測訊號」時擷取 (有探頭套)
- **誤差上限/下限**: 使用者於 UI 設定

## Workflow
1. **PLC 發送空槍量測訊號** → 電腦擷取 12 支空槍值 → 寫入 CSV 一列
2. **PLC 發送溫度量測訊號** → 電腦擷取 12 支溫度量測值
3. **電腦 A** 計算各通道誤差值，判斷 PASS/FAIL
4. **電腦 A** 回傳判定結果給 PLC (D501~D512) → 寫入 CSV 一列
5. **PLC** 根據判定結果累加 OK/NG 計數 (D517~D540)
6. **PLC** 執行分選：PASS 收集出貨，FAIL 銷毀

## OK/NG 計數職責
- **PLC 負責**: 累加 OK/NG 計數 (D517~D540)
- **HMI 負責**: 讀取並顯示 (僅 Master)、換批時歸零

## Specs 目錄
- `specs/system-architecture/` - 系統架構 (硬體、軟體模組、通道映射、Port 配置)
- `specs/program-flow/` - 程式流程 (啟動、量測、狀態機、Slave 即時顯示、關閉)
- `specs/communication-protocols/` - 通訊協定 (藍芽 SPP、PLC MC、網路含耳套/BT狀態)
- `specs/configuration/` - 設定管理 (config.json 結構、即時套用機制)
- `specs/csv-logging/` - CSV 記錄 (批次檔案、每次觸發一列、54 欄格式)
- `specs/ui-layout/` - UI 介面 (Master/Slave 差異、OK/NG 僅 Master 顯示)

## Project Conventions

### Code Style
- 繁體中文註解
- snake_case 變數命名
- 常數大寫

### Architecture Patterns
- 模組化：藍芽通訊、PLC 通訊、UI 分離
- Master-Slave：電腦 A 主控，電腦 B 傳送數據 (含耳套+BT狀態)
- 事件驅動：PLC 訊號觸發量測流程
- 回呼驅動：各管理器透過 set_callbacks 註冊回呼函式
- 執行緒安全：UI 更新使用 `with client:` 上下文保護

## Domain Context
- **耳溫套檢測** - 檢測探頭套對量測精度的影響
- **空槍值** - 基準值，無套時的溫度讀數
- **誤差容許範圍** - 使用者設定的上下限值

## Important Constraints
- 單台電腦最多連接 6 支藍芽耳溫槍
- 電腦 A 必須彙整 12 支數據後才判斷並回傳 PLC
- Windows 平台
- 更新設定時不可重啟管理器 (避免執行緒衝突)

## External Dependencies
- **NiceGUI** - UI 框架
- **PyBluez** - 經典藍芽 SPP 通訊
- **pymcprotocol** - 三菱 MC Protocol
- **三菱 5U PLC** - 外部硬體
- **耳溫槍 x12** - 藍芽 SPP 設備
