## Context

耳溫套檢測機台一循環約 5~7 秒：1 次空槍（無套）+ N 次量測（套上耳套，N 由 PLC 設定預設 6，每次量不同套並由 OK/NG port 彈出）。汽缸一次下壓 6 支耳溫槍；槍因實體壓桿觸發而主動推送量測值（`DB` 封包）。

`cb_test/` 實測確認：
- 槍**只在壓桿觸發才推送**、閒置不推。
- `CD` 不觸發新量測，只回**最後一筆快取值**（連續發 CD 回相同值）。
- `CB`(ACK) 非必要；不送槍照常運作。送 CB 會讓槍進入「M」模式並擋掉 CD。

現行 `main.py` 量測流程（`_acquire_and_collect` / `collect_empty_values` / `collect_measure_values`）背負「壓桿/閒置雙模式 + 發 CD + 新鮮度 lookback + 無限重試」的複雜度，且漏壓（汽缸沒壓到某支）會沿用舊值或無限重試卡死，無法偵測。

握手契約（既有、不變）：`D515/D500=1 → HMI 收齊判定 → 寫 PLC/LOG → D515/D500=0 → PLC 確認後彈套`。PLC：`D513≠0` 即暫停機台；`D501~D512=1` 即噴 NG port。

## Goals / Non-Goals

**Goals:**
- 生產取值改為純主動推送，移除生產路徑的 CD。
- 可靠偵測漏壓並回報（D513 bit15 + D501~D512 NG + 警報），不沿用舊值、不卡流程。
- 簡化量測取值邏輯。
- 正常循環的流程、握手、log 與重構前零差異。

**Non-Goals:**
- 不改 PLC 梯形圖（暫停與噴 NG port 皆既有行為）。
- 不改 log 格式/路徑/檔名/非同步機制（僅漏壓欄位留空白）。
- 不改藍芽連線管理、斷線去抖動、Master/Slave 網路架構。
- 不移除手動擷取功能（保留並改用 CD 取快取值）。

## Decisions

### D1. 生產純推送、CD 僅保留手動模式
- **選擇**：以明確「手動旗標」區分生產 vs 手動，而非以「是否已有推送」推測。
- **理由**：若以「無 fresh push」推測手動，會把「整台汽缸失效、6 支全漏壓」誤判成手動 → 發 CD 拿到 6 支舊值 → 最嚴重情況反而漏掉。明確旗標可確保「真實 PLC 觸發一律走生產模式」，即使全漏也照樣偵測。
- **機制**：`on_simulate_empty/measure`（UI 手動擷取）在寫 D515/D500 前設模組旗標 `_manual_trigger`；`on_plc_empty_trigger/measure_trigger` 讀取並消費該旗標決定模式。
- **替代方案**：自動偵測（沿用現行 `_acquire_and_collect` 的 fresh_now 判斷）→ 否決，因上述全漏壓誤判風險。

### D2. 新鮮度以「時間戳是否在本輪觸發後更新」判定，timeout 放行
- **選擇**：記錄各通道「上一輪已採用的推送時間戳」為基準；本輪觸發後，通道推送時間戳超過基準即視為「本輪有壓到」。等待至多 `miss_timeout`（預設 3s）；全部到齊提早結束，逾時仍缺者判漏壓。
- **理由**：5~7 秒循環間隔足夠，單純比「是否有比上輪更新的推送」即可，不需固定秒數 lookback（且固定 lookback 在連續量測時可能跨輪誤判）。「無限重試」改「timeout 放行」是讓漏壓偵測成立的核心。
- **時鐘**：本機 BT 用 `last_data.timestamp`（本機時鐘），Slave 通道用 `_net_data_recv_at`（Master 收到封包時刻，本機時鐘）；皆與觸發時間同一時鐘，避免跨機器偏移。
- **替代方案**：固定 lookback 窗（現行）→ 連續量測有跨輪誤判風險，否決。

### D3. 漏壓處理：留 None、寫 NG、設 bit15
- 漏壓通道內部 `empty_value`/`measure_value` 留 None（不寫 0、不沿用舊值）→ `record_measure_value` 因 empty 為 None 不算誤差、不判 PASS → 自然 NG。
- 寫 D501~D512 = NG(1)（噴 NG port）；設 D513 bit15（暫停機台）。
- bit15 設定/清除時機與既有 bit12/13/14 對齊（收集判定階段、reset trigger 前；由異常復歸 D513 全清清除）。差異僅觸發條件：12/13/14「值收齊即判」、bit15「等到 timeout 仍缺即判」。
- **為何留 None 而非寫 0**：0 會被既有空槍範圍檢查歸到 bit14（空槍異常），與漏壓混淆；且同一支兩階段皆漏時 `0-0=0` 會落在容差內假 PASS。None 可堵此洞。

### D4. CSV 漏壓欄位留空白
- `measurement.py:save_cycle_log` 的 `get_temperature` 目前 `None → 0`；改為漏壓通道輸出空白字串（停用通道維持 0，以資區分）。
- 其餘 log 格式/路徑/檔名/非同步佇列不動。

### D5. 移除 CB 死碼
- `send_ack` / `build_cb_response` 從未被呼叫且實測不需要 → 移除（或保留定義但標註不使用）。傾向移除以減少混淆。

## Risks / Trade-offs

- [漏壓 timeout 拉長循環] → 僅在「真的有漏壓」時才吃滿 timeout（全部到齊提早結束）；預設 3s、可調，且漏壓本來就該停機檢查，可接受。
- [手動旗標 race] → 手動按鈕設旗標後才寫 PLC，PLC 輪詢回讀觸發時旗標仍在；單一操作員動作，風險極低。消費後即清除避免殘留。
- [Slave 通道時鐘基準] → 一律用 Master 收到時刻（`_net_data_recv_at`，僅真資料封包更新，排除純狀態封包），與觸發同時鐘，無跨機偏移。
- [全通道漏壓被當手動] → 由 D1 明確旗標消除：真實 PLC 觸發永遠走生產模式。
- [移除 CB 影響韌體] → 實測多輪無 CB 槍照常運作；保留 `cb_test/` 作為換槍/換韌體回歸工具。

## Migration Plan

1. 依 specs 實作；保留現行 `config.json` 相容（新增 `bluetooth.miss_timeout` 預設 3，缺欄位時走預設）。
2. 先在模擬/手動模式驗證取值與漏壓旗標、CSV 空白。
3. 實機單支驗證：正常壓桿（主動推送、log 正常）、刻意漏壓一支（bit15 ON、該支 NG 噴 NG port、警報、CSV 空白、流程不卡、握手照常 reset）。
4. 回歸：正常滿載循環確認 log 與重構前一致。
- **Rollback**：純軟體變更，git revert 即可回到重構前版本（基準 commit 已 push）。

## Open Questions

- `bluetooth.miss_timeout` 預設 3s 是否需依實機 Slave 延遲再校（可由 `_net_data_recv_at` − 壓桿時間的實測值微調）。
- 漏壓暫停後，操作員「異常復歸」是否需同時重置該輪量測狀態（沿用既有異常復歸流程即可，待實機確認）。
