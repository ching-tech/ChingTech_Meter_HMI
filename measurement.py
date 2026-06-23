# -*- coding: utf-8 -*-
"""
量測邏輯模組 - 處理量測流程與 PASS/FAIL 判斷
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from enum import Enum
import time
import os
import csv
from datetime import datetime

class MeasurementState(Enum):
    IDLE = "idle"                    # 閒置
    WAITING_EMPTY = "waiting_empty"  # 等待空槍量測
    EMPTY_DONE = "empty_done"        # 空槍量測完成
    WAITING_MEASURE = "waiting_measure"  # 等待溫度量測
    MEASURING = "measuring"          # 量測中
    COMPLETE = "complete"            # 完成

class JudgeMode(Enum):
    NORMAL = "normal"        # 正常判定
    FORCE_OK = "force_ok"    # 強制全部 OK
    FORCE_NG = "force_ng"    # 強制全部 NG

class JudgeResult(Enum):
    WAIT = "wait"   # 等待
    PASS = "pass"   # 合格
    FAIL = "fail"   # 不合格

@dataclass
class ChannelData:
    """單一通道資料"""
    channel: int
    empty_value: Optional[float] = None      # 空槍值
    measure_value: Optional[float] = None    # 溫度量測值
    error_value: Optional[float] = None      # 誤差值
    result: JudgeResult = JudgeResult.WAIT
    timestamp: float = 0.0

@dataclass
class MeasurementResult:
    """量測結果"""
    channels: Dict[int, ChannelData] = field(default_factory=dict)
    pass_count: int = 0
    fail_count: int = 0
    state: MeasurementState = MeasurementState.IDLE


class MeasurementManager:
    """量測流程管理器"""

    def __init__(self, channel_count: int = 12,
                 tolerance_upper: float = 0.5,
                 tolerance_lower: float = -0.5,
                 log_dir: str = "logs",
                 enable_logging: bool = True):
        self.channel_count = channel_count
        self.tolerance_upper = tolerance_upper
        self.tolerance_lower = tolerance_lower
        self.log_dir = log_dir
        self.enable_logging = enable_logging

        self._state = MeasurementState.IDLE
        self.judge_mode = JudgeMode.NORMAL
        self._channels: Dict[int, ChannelData] = {}
        self._init_channels()

        # 當前批次 Log 檔案路徑
        self.current_log_file: Optional[str] = None

        # 回呼函式
        self._on_state_change: Optional[Callable[[MeasurementState], None]] = None
        self._on_channel_update: Optional[Callable[[int, ChannelData], None]] = None
        self._on_complete: Optional[Callable[[MeasurementResult], None]] = None

        # 確保 log 目錄存在 (僅在啟用記錄時建立)
        if self.enable_logging:
            os.makedirs(self.log_dir, exist_ok=True)

    def _init_channels(self):
        """初始化通道資料"""
        self._channels = {
            i: ChannelData(channel=i)
            for i in range(1, self.channel_count + 1)
        }

    def set_tolerance(self, upper: float, lower: float):
        """設定誤差容許範圍"""
        self.tolerance_upper = upper
        self.tolerance_lower = lower

    def set_callbacks(self,
                      on_state: Optional[Callable[[MeasurementState], None]] = None,
                      on_channel: Optional[Callable[[int, ChannelData], None]] = None,
                      on_complete: Optional[Callable[[MeasurementResult], None]] = None):
        """設定回呼函式"""
        self._on_state_change = on_state
        self._on_channel_update = on_channel
        self._on_complete = on_complete

    @property
    def state(self) -> MeasurementState:
        return self._state

    def get_channel(self, channel: int) -> Optional[ChannelData]:
        """取得通道資料"""
        return self._channels.get(channel)

    def clear_channel(self, channel: int):
        """清除指定通道的量測資料（停用通道時使用，避免殘留資料阻擋流程完成）"""
        if channel in self._channels:
            self._channels[channel].empty_value = None
            self._channels[channel].measure_value = None
            self._channels[channel].error_value = None
            self._channels[channel].result = JudgeResult.WAIT

    def get_all_channels(self) -> Dict[int, ChannelData]:
        """取得所有通道資料"""
        return self._channels.copy()

    def reset(self):
        """重設量測資料"""
        self._init_channels()
        self._update_state(MeasurementState.IDLE)

    def ensure_today_log_file(self, machine_name: str = "Machine1"):
        """確保今日 log 檔案存在 (檔名: log_YYYYMMDD_<machine_name>.csv)。
        檔案不存在時建立並寫入標題列；已存在時直接續寫。
        每次寫入前呼叫，可自動處理跨日與機台名變更。
        """
        if not self.enable_logging:
            return None
        os.makedirs(self.log_dir, exist_ok=True)
        date_str = datetime.now().strftime("%Y%m%d")
        safe_machine = machine_name.strip() or "Machine"
        filename = f"log_{date_str}_{safe_machine}.csv"
        filepath = os.path.join(self.log_dir, filename)

        if os.path.exists(filepath):
            self.current_log_file = filepath
            return filepath

        try:
            with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                self._write_log_headers(writer)
            self.current_log_file = filepath
            return filepath
        except Exception as e:
            print(f"建立今日記錄檔失敗: {e}")
            return None

    @staticmethod
    def _write_log_headers(writer):
        """寫入 log CSV 的兩列 header (主檔與 fallback 檔共用)"""
        # 第一列標籤 (批號欄位佔位 + A~M + 其餘空白)
        writer.writerow(['', '', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K', 'L', 'M'] + [''] * 41)
        # 第二列標題
        header = ['批號', '種類'] + [f'scan{i}' for i in range(1, 13)] + ['Time', '誤差上限', '誤差下限']
        header += [f'scan{i} cover' for i in range(1, 13)]
        header += [f'scan{i} OK' for i in range(1, 13)]
        header += [f'scan{i} NG' for i in range(1, 13)]
        header += ['TOTAL OK', 'TOTAL NG']
        writer.writerow(header)

    def _write_cycle_row(self, filepath: str, row: list) -> bool:
        """寫一列 cycle 資料；主檔被鎖定 (PermissionError，常見原因為 Excel 開啟)
        會 fallback 到同目錄 `<filename>_1.csv`，避免該筆資料丟失。"""
        try:
            with open(filepath, 'a', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(row)
            return True
        except PermissionError:
            base, ext = os.path.splitext(filepath)
            fallback = f"{base}_1{ext}"
            try:
                write_header = not os.path.exists(fallback)
                with open(fallback, 'a', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    if write_header:
                        self._write_log_headers(writer)
                    writer.writerow(row)
                print(f"[!] 主 log 被鎖定 (可能 Excel 開啟)，已寫入備用檔: {os.path.basename(fallback)}")
                return True
            except Exception as e:
                print(f"[!!] 主檔與備用檔均寫入失敗，本筆 cycle 遺失: {e}")
                return False
        except Exception as e:
            print(f"寫入量測記錄失敗: {e}")
            return False

    def start_empty_measurement(self):
        """開始空槍量測。
        清空所有通道的空槍/量測值（新零件、新基準）：之後只有實際收到推送的通道會被
        record_empty_values 填值，漏壓的通道自然維持 None，不會殘留上一輪舊值。"""
        for ch in self._channels.values():
            ch.empty_value = None
            ch.measure_value = None
            ch.error_value = None
            ch.result = JudgeResult.WAIT
        self._update_state(MeasurementState.WAITING_EMPTY)

    def record_empty_value(self, channel: int, value: float):
        """記錄空槍值"""
        if channel in self._channels:
            self._channels[channel].empty_value = value
            self._channels[channel].timestamp = time.time()
            self._notify_channel_update(channel)

        # 檢查是否所有通道都已記錄
        if self._any_empty_recorded():
            self._update_state(MeasurementState.EMPTY_DONE)

    def record_empty_values(self, values: Dict[int, float]):
        """批次記錄空槍值"""
        print(f"[measurement] record_empty_values: 收到 {len(values)} 通道空槍值")
        for channel, value in values.items():
            if channel in self._channels:
                self._channels[channel].empty_value = value
                self._channels[channel].timestamp = time.time()
                self._notify_channel_update(channel)

        if self._any_empty_recorded():
            print(f"[measurement] 空槍記錄完成，狀態 {self._state.value} → EMPTY_DONE")
            self._update_state(MeasurementState.EMPTY_DONE)

    def start_temperature_measurement(self):
        """開始溫度量測。
        只清量測值（保留空槍基準）：之後只有實際收到推送的通道會被 record_measure_values
        填值，漏壓的通道 measure_value 維持 None → 不算誤差、不判 PASS → 自然 NG。"""
        for ch in self._channels.values():
            ch.measure_value = None
            ch.error_value = None
            ch.result = JudgeResult.WAIT
        self._update_state(MeasurementState.WAITING_MEASURE)

    def force_finalize(self):
        """強制結束本輪量測（timeout 放行用）：未收到量測值的通道視為漏壓，不阻擋完成。
        若已 COMPLETE 則不重複觸發。"""
        if self._state != MeasurementState.COMPLETE:
            self._finalize()

    def record_measure_value(self, channel: int, value: float):
        """記錄溫度量測值並判斷"""
        if channel not in self._channels:
            return

        ch = self._channels[channel]
        ch.measure_value = value
        ch.timestamp = time.time()

        # 計算誤差並判斷
        if ch.empty_value is not None:
            ch.error_value = value - ch.empty_value
            ch.result = self._judge(ch.error_value)

        self._notify_channel_update(channel)

        # 檢查是否所有通道都已完成
        if self._all_measure_recorded():
            self._finalize()

    def record_measure_values(self, values: Dict[int, float]):
        """批次記錄溫度量測值"""
        print(f"[measurement] record_measure_values: 收到 {len(values)} 通道值")
        for channel, value in values.items():
            if channel not in self._channels:
                print(f"[measurement] CH{channel} 不在通道列表中，跳過")
                continue

            ch = self._channels[channel]
            ch.measure_value = value
            ch.timestamp = time.time()

            if ch.empty_value is not None:
                ch.error_value = value - ch.empty_value
                ch.result = self._judge(ch.error_value)
                print(f"[measurement] CH{channel}: empty={ch.empty_value:.2f}, measure={value:.2f}, error={ch.error_value:.2f}, result={ch.result.value}")
            else:
                print(f"[measurement] CH{channel}: 無空槍值，無法計算誤差")

            self._notify_channel_update(channel)

        all_done = self._all_measure_recorded()
        print(f"[measurement] _all_measure_recorded = {all_done}")
        if all_done:
            self._finalize()
        else:
            # 列出阻塞原因
            for c in self._channels.values():
                if c.empty_value is not None and c.measure_value is None:
                    print(f"[measurement] 阻塞: CH{c.channel} 有空槍值({c.empty_value:.2f})但無量測值")

    def get_results(self) -> List[bool]:
        """取得 12 通道的 PASS/FAIL 結果列表"""
        return [
            self._channels[i].result == JudgeResult.PASS
            for i in range(1, self.channel_count + 1)
        ]

    def get_result_summary(self) -> MeasurementResult:
        """取得量測結果摘要"""
        pass_count = sum(
            1 for ch in self._channels.values()
            if ch.result == JudgeResult.PASS
        )
        fail_count = sum(
            1 for ch in self._channels.values()
            if ch.result == JudgeResult.FAIL
        )
        return MeasurementResult(
            channels=self._channels.copy(),
            pass_count=pass_count,
            fail_count=fail_count,
            state=self._state
        )

    def _judge(self, error_value: float) -> JudgeResult:
        """判斷 PASS/FAIL（依 judge_mode 決定判定方式）"""
        if self.judge_mode == JudgeMode.FORCE_OK:
            return JudgeResult.PASS
        if self.judge_mode == JudgeMode.FORCE_NG:
            return JudgeResult.FAIL
        # 正常判定：誤差在 -下限 ~ +上限 範圍內為 PASS
        lower = -abs(self.tolerance_lower)
        upper = abs(self.tolerance_upper)
        if lower <= error_value <= upper:
            return JudgeResult.PASS
        return JudgeResult.FAIL

    def _any_empty_recorded(self) -> bool:
        """檢查是否有「任一」通道已記錄空槍值。
        注意：函式內用 any()，原本誤命名為 _all_empty_recorded 已修正。
        實際流程中，main.py 端會 retry 收齊所有啟用通道後才呼叫 save_cycle_log，
        故此處用 any() 已足夠判斷「至少有資料可寫」。
        """
        return any(
            ch.empty_value is not None
            for ch in self._channels.values()
        )

    def _all_measure_recorded(self) -> bool:
        """檢查有空槍值的通道是否都已完成量測"""
        channels_with_empty = [
            ch for ch in self._channels.values()
            if ch.empty_value is not None
        ]
        # 沒有任何空槍值 (例如全部通道停用) 時視為已完成，避免流程卡住
        if not channels_with_empty:
            return True
        # 檢查所有有空槍值的通道是否都有量測值
        return all(
            ch.measure_value is not None
            for ch in channels_with_empty
        )

    def _finalize(self):
        """完成量測流程"""
        print(f"[measurement] _finalize: 狀態 {self._state.value} → COMPLETE")
        self._update_state(MeasurementState.COMPLETE)
        # 注意：現在由 main.py 顯式呼叫 save_cycle_log 以包含 PLC 與耳套資訊
        if self._on_complete:
            print("[measurement] 呼叫 on_complete 回呼")
            self._on_complete(self.get_result_summary())
        else:
            print("[measurement] 警告: 無 on_complete 回呼")

    def save_cycle_log(self, is_empty: bool = False, plc_data=None,
                       ear_covers: Dict[int, str] = None,
                       enabled_channels: List[int] = None,
                       batch_no: str = "",
                       machine_name: str = "Machine1") -> bool:
        """保存單次量測的一列資料至 CSV (每次 D515/D500 觸發時呼叫)。
        會自動依據機台名與當天日期切換到對應的 log 檔。

        Args:
            is_empty: True=空槍觸發(D515), False=量測觸發(D500)
            plc_data: PLC 資料物件
            ear_covers: 各通道耳套狀態 dict {channel: "1111"/"0000"}
            enabled_channels: 已啟用的通道列表
            batch_no: 批號 (人員輸入，寫入列首)
            machine_name: 機台名稱 (決定 log 檔名)

        Returns:
            True=寫入成功, False=寫入失敗或未啟用
        """
        if not self.enable_logging:
            return False
        # 每次寫入前確認檔案 (處理跨日 / 機台名變更)
        self.ensure_today_log_file(machine_name)

        now = datetime.now()
        time_str = now.strftime("%Y/%m/%d %H:%M:%S")

        # 建立 scan_idx -> internal_ch 的映射 (scan1=CH1, scan2=CH2, ...)
        from config import CHANNEL_DISPLAY_NAMES
        scan_to_internal = {}
        for int_ch, name in CHANNEL_DISPLAY_NAMES.items():
            ch_num = int(name.replace('CH', ''))
            scan_to_internal[ch_num] = int_ch

        if enabled_channels is None:
            enabled_channels = []

        def get_temperature(ch_num):
            """取得該 scan 通道的溫度值。
            停用通道 → 0；啟用但漏壓 (值為 None) → 空白字串 (與停用的 0 區分)。"""
            int_ch = scan_to_internal.get(ch_num)
            if int_ch is None or int_ch not in enabled_channels:
                return 0
            if int_ch in self._channels:
                if is_empty:
                    val = self._channels[int_ch].empty_value
                else:
                    val = self._channels[int_ch].measure_value
                # 啟用通道但無值 = 漏壓 → 留空白
                return val if val is not None else ""
            return ""

        # 先建立完整 row，再交給 _write_cycle_row 處理 (主檔失敗會 fallback)
        row = []
        # 列首: 批號
        row.append(batch_no or "")
        # A欄: 空槍寫 "empty"，量測寫 PLC D516 值
        if is_empty:
            row.append("empty")
        else:
            row.append(plc_data.cycle_count if plc_data else "")
        # B~M欄: 12 支槍的數值 (scan1~scan12)
        for i in range(1, 13):
            val = get_temperature(i)
            # 數值 → 格式化；漏壓 (空字串) → 留空白
            row.append(f"{val:.2f}" if isinstance(val, (int, float)) else "")
        # N欄: 時間
        row.append(time_str)
        # O欄: 誤差上限, P欄: 誤差下限
        row.append(f"+{abs(self.tolerance_upper):.2f}")
        row.append(f"-{abs(self.tolerance_lower):.2f}")
        # Q~AB欄: 12 支槍耳溫套 (有="1111", 無="0000")
        for i in range(1, 13):
            int_ch = scan_to_internal.get(i)
            cover = ear_covers.get(int_ch, "") if ear_covers and int_ch else ""
            if cover == "1111":
                row.append("1111")
            elif cover == "0000":
                row.append("0000")
            else:
                row.append("")
        # AC~AN欄: OK counts (D517~D528), AO~AZ欄: NG counts (D529~D540)
        if plc_data:
            row.extend(plc_data.ok_counts[:12])
            row.extend(plc_data.ng_counts[:12])
        else:
            row.extend([0] * 24)
        # BA欄: TOTAL OK (AC~AN 加總), BB欄: TOTAL NG (AO~AZ 加總)
        if plc_data:
            row.append(sum(plc_data.ok_counts[:12]))
            row.append(sum(plc_data.ng_counts[:12]))
        else:
            row.append(0)
            row.append(0)

        return self._write_cycle_row(self.current_log_file, row)

    def get_log_filepath(self) -> str:
        return self.current_log_file if self.current_log_file else ""

    def _update_state(self, state: MeasurementState):
        """更新狀態"""
        old = self._state.value
        self._state = state
        print(f"[measurement] 狀態變更: {old} → {state.value}")
        if self._on_state_change:
            self._on_state_change(state)

    def _notify_channel_update(self, channel: int):
        """通知通道更新"""
        if self._on_channel_update and channel in self._channels:
            self._on_channel_update(channel, self._channels[channel])
