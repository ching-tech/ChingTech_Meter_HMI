# CSV 記錄規格

## 批次檔案

### 建立時機
- 系統啟動時自動建立初始批次
- 使用者點擊「換批」按鈕

### 檔案命名
- 格式: `YYYYMMDDHHMMSS.csv`
- 路徑: `{log_dir}/` (預設 `logs/`，Slave 為 `slave_test/logs/`)
- 編碼: UTF-8 BOM (`utf-8-sig`)

### 標題列結構
```
第 1 列 (欄位標籤): "", A, B, C, D, E, F, G, H, J, K, L, M, ...
第 2 列 (標題): 種類, scan1~scan12, Time, 誤差上限, 誤差下限,
                scan1~12 cover, scan1~12 OK, scan1~12 NG, TOTAL OK, TOTAL NG
```

## 單次量測記錄 (每次觸發一列)

每次 D515 空槍觸發或 D500 量測觸發時，`save_cycle_log()` 追加一列，共 54 欄:

### 欄位定義

| 欄位 | Excel 欄 | 內容 | 說明 |
|------|----------|------|------|
| A | A | 種類 | 空槍觸發寫 `"empty"`，量測觸發寫 PLC D516 值 |
| B~M | B~M | scan1~scan12 溫度值 | 12 支槍的數值，停用或異常寫 `0`，格式 `%.2f` |
| N | N | 時間 | 格式: `YYYY/MM/DD HH:MM:SS` |
| O | O | 誤差上限 | 格式: `+X.XX` |
| P | P | 誤差下限 | 格式: `-X.XX` |
| Q~AB | Q~AB | scan1~12 耳溫套 | 有耳溫套寫 `"1111"`，無耳溫套寫 `"0000"`，未知寫空 |
| AC~AN | AC~AN | scan1~12 OK 計數 | 讀取 PLC D517~D528 |
| AO~AZ | AO~AZ | scan1~12 NG 計數 | 讀取 PLC D529~D540 |
| BA | BA | TOTAL OK | AC~AN 加總 |
| BB | BB | TOTAL NG | AO~AZ 加總 |

### 溫度欄位邏輯
- **空槍觸發** (`is_empty=True`): B~M 寫入各通道的 `empty_value` (空槍值)
- **量測觸發** (`is_empty=False`): B~M 寫入各通道的 `measure_value` (量測值)
- 停用通道 (不在 `enabled_channels`) 一律寫 `0`
- 無資料的通道寫 `0`

## 通道映射
CSV 中 scan1~scan12 按邏輯通道 CH1~CH12 排列。
內部通道透過 `CHANNEL_DISPLAY_NAMES` 反查 `scan_to_internal` 映射到正確的 scan 欄位。

## 換批行為
1. 建立新 CSV 檔案 (含標題列)
2. PLC OK/NG 計數歸零 (D517~D540 全寫 0)
3. 更新 UI 批次標籤
4. UI 通知使用者
