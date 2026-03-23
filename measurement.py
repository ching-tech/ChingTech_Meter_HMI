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

    def get_all_channels(self) -> Dict[int, ChannelData]:
        """取得所有通道資料"""
        return self._channels.copy()

    def reset(self):
        """重設量測資料"""
        self._init_channels()
        self._update_state(MeasurementState.IDLE)

    def start_new_batch(self):
        """開始新批次，建立新的 CSV 檔案並寫入標題列"""
        if not self.enable_logging:
            return None
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d%H%M%S")
        self.current_log_file = os.path.join(self.log_dir, f"{timestamp}.csv")
        
        try:
            with open(self.current_log_file, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                # 第一列標籤
                writer.writerow(['', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'J', 'K', 'L', 'M'] + [''] * 41)
                # 第二列標題 (種類 + scan1~12 + 其他)
                header = ['種類'] + [f'scan{i}' for i in range(1, 13)] + ['Time', '誤差上限', '誤差下限']
                header += [f'scan{i} cover' for i in range(1, 13)]
                header += [f'scan{i} OK' for i in range(1, 13)]
                header += [f'scan{i} NG' for i in range(1, 13)]
                header += ['TOTAL OK', 'TOTAL NG']
                writer.writerow(header)
            return self.current_log_file
        except Exception as e:
            print(f"建立新批次檔案失敗: {e}")
            return None

    def resume_batch(self, filename: str) -> Optional[str]:
        """沿用既有批次檔案 (程式重啟時使用)"""
        filepath = os.path.join(self.log_dir, filename)
        if os.path.exists(filepath):
            self.current_log_file = filepath
            return filepath
        return None

    def start_empty_measurement(self):
        """開始空槍量測"""
        self._update_state(MeasurementState.WAITING_EMPTY)

    def record_empty_value(self, channel: int, value: float):
        """記錄空槍值"""
        if channel in self._channels:
            self._channels[channel].empty_value = value
            self._channels[channel].timestamp = time.time()
            self._notify_channel_update(channel)

        # 檢查是否所有通道都已記錄
        if self._all_empty_recorded():
            self._update_state(MeasurementState.EMPTY_DONE)

    def record_empty_values(self, values: Dict[int, float]):
        """批次記錄空槍值"""
        for channel, value in values.items():
            if channel in self._channels:
                self._channels[channel].empty_value = value
                self._channels[channel].timestamp = time.time()
                self._notify_channel_update(channel)

        if self._all_empty_recorded():
            self._update_state(MeasurementState.EMPTY_DONE)

    def start_temperature_measurement(self):
        """開始溫度量測"""
        self._update_state(MeasurementState.WAITING_MEASURE)

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
        for channel, value in values.items():
            if channel not in self._channels:
                continue

            ch = self._channels[channel]
            ch.measure_value = value
            ch.timestamp = time.time()

            if ch.empty_value is not None:
                ch.error_value = value - ch.empty_value
                ch.result = self._judge(ch.error_value)

            self._notify_channel_update(channel)

        if self._all_measure_recorded():
            self._finalize()

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

    def _all_empty_recorded(self) -> bool:
        """檢查是否有空槍值已記錄（至少一個通道）"""
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
        # 如果沒有任何空槍值，視為未完成
        if not channels_with_empty:
            return False
        # 檢查所有有空槍值的通道是否都有量測值
        return all(
            ch.measure_value is not None
            for ch in channels_with_empty
        )

    def _finalize(self):
        """完成量測流程"""
        self._update_state(MeasurementState.COMPLETE)
        # 注意：現在由 main.py 顯式呼叫 save_cycle_log 以包含 PLC 與耳套資訊
        if self._on_complete:
            self._on_complete(self.get_result_summary())

    def save_cycle_log(self, is_empty: bool = False, plc_data=None,
                       ear_covers: Dict[int, str] = None,
                       enabled_channels: List[int] = None) -> bool:
        """保存單次量測的一列 54 欄位資料至 CSV (每次 D515/D500 觸發時呼叫)

        Args:
            is_empty: True=空槍觸發(D515), False=量測觸發(D500)
            plc_data: PLC 資料物件
            ear_covers: 各通道耳套狀態 dict {channel: "1111"/"0000"}
            enabled_channels: 已啟用的通道列表

        Returns:
            True=寫入成功, False=寫入失敗或未啟用
        """
        if not self.enable_logging:
            return False
        if not self.current_log_file:
            self.start_new_batch()

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
            """取得該 scan 通道的溫度值，停用或異常回傳 0"""
            int_ch = scan_to_internal.get(ch_num)
            if int_ch is None or int_ch not in enabled_channels:
                return 0
            if int_ch in self._channels:
                if is_empty:
                    val = self._channels[int_ch].empty_value
                else:
                    val = self._channels[int_ch].measure_value
                return val if val is not None else 0
            return 0

        try:
            with open(self.current_log_file, 'a', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                row = []

                # A欄: 空槍寫 "empty"，量測寫 PLC D516 值
                if is_empty:
                    row.append("empty")
                else:
                    row.append(plc_data.cycle_count if plc_data else "")

                # B~M欄: 12 支槍的數值 (scan1~scan12)
                for i in range(1, 13):
                    val = get_temperature(i)
                    row.append(f"{val:.2f}" if isinstance(val, (int, float)) else "0")

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

                writer.writerow(row)
            return True
        except Exception as e:
            print(f"寫入量測記錄失敗: {e}")
            return False

    def get_log_filepath(self) -> str:
        return self.current_log_file if self.current_log_file else ""

    def _update_state(self, state: MeasurementState):
        """更新狀態"""
        self._state = state
        if self._on_state_change:
            self._on_state_change(state)

    def _notify_channel_update(self, channel: int):
        """通知通道更新"""
        if self._on_channel_update and channel in self._channels:
            self._on_channel_update(channel, self._channels[channel])
