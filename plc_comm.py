# -*- coding: utf-8 -*-
"""
PLC MC Protocol 通訊模組 - 與三菱 FX5U PLC 通訊 (3E 協議)
暫存器範圍: D500~D541 (42 個 D 字組)
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from enum import Enum

# --- D 暫存器常數 ---
D_BASE = 500        # 起始暫存器
D_READ_SIZE = 42    # 批次讀取數量 (D500~D541)

# 各暫存器偏移 (相對於 D_BASE)
_OFF_TRIGGER       = 0   # D500: 量測觸發
_OFF_RESULT_START  = 1   # D501~D512: 槍 1~12 判定
_OFF_RESULT_END    = 12
_OFF_BT_ERROR      = 13  # D513: 藍芽連線狀態 (bit mask)
_OFF_HEARTBEAT     = 14  # D514: PC 心跳
_OFF_EMPTY_TRIGGER = 15  # D515: 空槍測試觸發
_OFF_CYCLE_COUNT   = 16  # D516: 測試週期次數
_OFF_OK_START      = 17  # D517~D528: 槍 1~12 OK 數
_OFF_OK_END        = 28
_OFF_NG_START      = 29  # D529~D540: 槍 1~12 NG 數
_OFF_NG_END        = 40
_OFF_RESET         = 41  # D541: HMI 異常復歸


class PLCConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class PLCData:
    """D500~D541 暫存器快照"""
    trigger: int = 0                                # D500: 量測觸發
    results: List[int] = field(default_factory=lambda: [0] * 12)  # D501~D512
    bt_error: int = 0                               # D513: 藍芽連線狀態 bit mask
    heartbeat: int = 0                              # D514: PC 心跳
    empty_trigger: int = 0                          # D515: 空槍測試觸發
    cycle_count: int = 0                            # D516: 測試週期次數
    ok_counts: List[int] = field(default_factory=lambda: [0] * 12)   # D517~D528
    ng_counts: List[int] = field(default_factory=lambda: [0] * 12)   # D529~D540
    reset: int = 0                                  # D541: HMI 異常復歸


class PLCManager:
    """PLC 通訊管理器 (FX5U 3E 協議, D500~D541)"""

    def __init__(self, ip_address: str, port: int, simulation_mode: bool = True):
        self.ip_address = ip_address
        self.port = port
        self.simulation_mode = simulation_mode

        self._plc = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state = PLCConnectionState.DISCONNECTED

        # 最新暫存器快照
        self._plc_data = PLCData()
        self._prev_data = PLCData()

        # 心跳
        self._heartbeat_value = 0
        self._last_heartbeat_time = 0.0

        # D513 藍芽錯誤位元遮罩 (本地維護)
        self._bt_error_mask = 0

        # 回呼函式
        self._on_empty_trigger: Optional[Callable[[], None]] = None
        self._on_measure_trigger: Optional[Callable[[], None]] = None
        self._on_reset: Optional[Callable[[], None]] = None
        self._on_state_callback: Optional[Callable[[PLCConnectionState], None]] = None

        # 模擬用
        self._sim_empty_trigger = False
        self._sim_measure_trigger = False
        self._sim_reset_trigger = False
        self._sim_ok_counts = [0] * 12
        self._sim_ng_counts = [0] * 12

    def set_callbacks(self,
                      on_empty: Optional[Callable[[], None]] = None,
                      on_measure: Optional[Callable[[], None]] = None,
                      on_state: Optional[Callable[[PLCConnectionState], None]] = None,
                      on_reset: Optional[Callable[[], None]] = None):
        """設定回呼函式"""
        self._on_empty_trigger = on_empty
        self._on_measure_trigger = on_measure
        self._on_state_callback = on_state
        self._on_reset = on_reset

    @property
    def state(self) -> PLCConnectionState:
        return self._state

    @property
    def plc_data(self) -> PLCData:
        """回傳最新 PLCData 快照"""
        return self._plc_data

    def connect(self) -> bool:
        """連接 PLC"""
        print(f"[*] 嘗試連線 PLC: {self.ip_address}:{self.port} (模擬模式: {self.simulation_mode})")
        
        # 確保舊連線已徹底關閉
        self.disconnect()

        if self._state == PLCConnectionState.CONNECTED:
            return True

        self._update_state(PLCConnectionState.CONNECTING)

        if self.simulation_mode:
            print("[*] PLC 處於內部模擬模式")
            time.sleep(0.3)
            self._update_state(PLCConnectionState.CONNECTED)
            return True

        try:
            import pymcprotocol
            print("[*] 正在建立連線...")
            self._plc = pymcprotocol.Type3E()
            self._plc.connect(self.ip_address, self.port)
            print("[+] PLC 連線成功！")
            self._update_state(PLCConnectionState.CONNECTED)
            return True
        except Exception as e:
            print(f"[!] PLC 連線失敗: {e}")
            self._update_state(PLCConnectionState.ERROR)
            return False

    def disconnect(self):
        """斷開 PLC 並釋放資源"""
        if self._plc:
            try:
                # 某些版本的 pymcprotocol 可能沒有 close 方法或名稱不同
                if hasattr(self._plc, 'close'):
                    self._plc.close()
                elif hasattr(self._plc, 'disconnect'):
                    self._plc.disconnect()
            except:
                pass
            self._plc = None
        
        if self._state == PLCConnectionState.CONNECTED:
            self._update_state(PLCConnectionState.DISCONNECTED)

    def start_monitoring(self):
        """啟動觸發訊號監控"""
        if self._running:
            return

        self._running = True
        self._last_heartbeat_time = time.time()
        # 建立一個強健的監控執行緒
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop_monitoring(self):
        """停止監控"""
        self._running = False
        self.disconnect()

    # --- 寫入方法 ---

    def write_results(self, results: List[bool]) -> bool:
        """寫入 12 通道 PASS/FAIL 結果至 D501~D512"""
        if not self._plc or self._state != PLCConnectionState.CONNECTED:
            return False
        
        # ... (其餘邏輯不變) ...


    # --- 寫入方法 ---

    def write_results(self, results: List[bool]) -> bool:
        """寫入 12 通道 PASS/FAIL 結果至 D501~D512
        PASS → 0, FAIL → 1 (反轉邏輯)
        """
        if self._state != PLCConnectionState.CONNECTED:
            return False

        # 轉換: True(PASS)→0, False(FAIL)→1
        values = [0 if r else 1 for r in results[:12]]
        # 補足 12 個
        while len(values) < 12:
            values.append(0)

        if self.simulation_mode:
            print(f"[模擬] 寫入判定結果 D501~D512: {values}")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_RESULT_START}", values)
            return True
        except Exception as e:
            print(f"寫入判定結果失敗: {e}")
            return False

    def write_measure_trigger(self, value: int = 1) -> bool:
        """寫入量測觸發訊號 D500"""
        if self._state != PLCConnectionState.CONNECTED:
            return False
        if self.simulation_mode:
            self._sim_measure_trigger = (value == 1)
            return True
        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_TRIGGER}", [value])
            return True
        except Exception as e:
            print(f"寫入量測觸發失敗: {e}")
            return False

    def write_empty_trigger(self, value: int = 1) -> bool:
        """寫入空槍觸發訊號 D515"""
        if self._state != PLCConnectionState.CONNECTED:
            return False
        if self.simulation_mode:
            self._sim_empty_trigger = (value == 1)
            return True
        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_EMPTY_TRIGGER}", [value])
            return True
        except Exception as e:
            print(f"寫入空槍觸發失敗: {e}")
            return False

    def write_complete_signal(self) -> bool:
        """寫入量測完成訊號: D500=0 (清除觸發)"""
        if self._state != PLCConnectionState.CONNECTED:
            return False

        if self.simulation_mode:
            print("[模擬] 寫入 D500=0 (量測完成)")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_TRIGGER}", [0])
            return True
        except Exception as e:
            print(f"寫入完成訊號失敗: {e}")
            return False

    def clear_empty_trigger(self) -> bool:
        """清除空槍測試觸發: D515=0"""
        if self._state != PLCConnectionState.CONNECTED:
            return False

        if self.simulation_mode:
            print("[模擬] 寫入 D515=0 (空槍完成)")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_EMPTY_TRIGGER}", [0])
            return True
        except Exception as e:
            print(f"清除空槍觸發失敗: {e}")
            return False

    def set_bt_error(self, channel: int, error: bool) -> bool:
        """管理 D513 藍芽錯誤位元遮罩
        channel: 1~12, error: True=斷線, False=正常
        bit0~bit11 對應槍 1~12, bit ON=error
        """
        if not (1 <= channel <= 12):
            return False

        bit = 1 << (channel - 1)
        if error:
            self._bt_error_mask |= bit
        else:
            self._bt_error_mask &= ~bit

        if self._state != PLCConnectionState.CONNECTED:
            return False

        if self.simulation_mode:
            print(f"[模擬] 寫入 D513=0x{self._bt_error_mask:04X} (通道 {channel} {'斷線' if error else '正常'})")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_BT_ERROR}", [self._bt_error_mask])
            return True
        except Exception as e:
            print(f"寫入藍芽狀態失敗: {e}")
            return False

    def write_ok_ng_counts(self, ok_list: List[int], ng_list: List[int]) -> bool:
        """寫入 OK/NG 計數至 D517~D528 (OK) 及 D529~D540 (NG)"""
        if self._state != PLCConnectionState.CONNECTED:
            return False

        ok_values = list(ok_list[:12])
        ng_values = list(ng_list[:12])
        while len(ok_values) < 12:
            ok_values.append(0)
        while len(ng_values) < 12:
            ng_values.append(0)

        # 合併為連續 24 個字組 D517~D540
        values = ok_values + ng_values

        if self.simulation_mode:
            self._sim_ok_counts = list(ok_values)
            self._sim_ng_counts = list(ng_values)
            print(f"[模擬] 寫入 OK 計數 D517~D528: {ok_values}")
            print(f"[模擬] 寫入 NG 計數 D529~D540: {ng_values}")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_OK_START}", values)
            return True
        except Exception as e:
            print(f"寫入 OK/NG 計數失敗: {e}")
            return False

    # --- 模擬觸發 ---

    def simulate_empty_trigger(self):
        """模擬空槍量測觸發 (測試用)"""
        if self.simulation_mode:
            self._sim_empty_trigger = True

    def simulate_measure_trigger(self):
        """模擬溫度量測觸發 (測試用)"""
        if self.simulation_mode:
            self._sim_measure_trigger = True

    def simulate_reset_trigger(self):
        """模擬異常復歸觸發 (測試用)"""
        if self.simulation_mode:
            self._sim_reset_trigger = True

    # --- 監控迴圈 ---

    def _monitor_loop(self):
        """監控迴圈: 每 50ms 批次讀取 D500~D541, 偵測上升緣, 每秒切換心跳"""
        error_count = 0
        while self._running:
            try:
                data = self._batch_read()
                if data is None:
                    raise Exception("讀取傳回 None")

                # 成功讀取，重設錯誤計數並確保狀態為 CONNECTED
                error_count = 0
                if self._state != PLCConnectionState.CONNECTED:
                    self._update_state(PLCConnectionState.CONNECTED)

                # 偵測 D500 上升緣 (量測觸發)
                if data.trigger == 1 and self._prev_data.trigger == 0:
                    print("[PLC] D500 上升緣: 量測觸發")
                    if self._on_measure_trigger:
                        self._on_measure_trigger()

                # 偵測 D515 上升緣 (空槍觸發)
                if data.empty_trigger == 1 and self._prev_data.empty_trigger == 0:
                    print("[PLC] D515 上升緣: 空槍觸發")
                    if self._on_empty_trigger:
                        self._on_empty_trigger()

                # 偵測 D541 上升緣 (異常復歸)
                if data.reset == 1 and self._prev_data.reset == 0:
                    print("[PLC] D541 上升緣: HMI 異常復歸")
                    if self._on_reset:
                        self._on_reset()

                self._prev_data = data
                self._plc_data = data

                # 心跳: 每秒切換 D514
                now = time.time()
                if now - self._last_heartbeat_time >= 1.0:
                    self._heartbeat_value = 1 - self._heartbeat_value
                    self._write_heartbeat(self._heartbeat_value)
                    self._last_heartbeat_time = now

            except Exception as e:
                error_count += 1
                # 連續失敗 5 次 (約 250ms) 才視為斷線，避免閃爍
                if error_count >= 5:
                    if self._state != PLCConnectionState.DISCONNECTED:
                        print(f"PLC 通訊持續失敗: {e}")
                        self._update_state(PLCConnectionState.DISCONNECTED)
                    
                    if self._running:
                        time.sleep(2)  # 等待後嘗試重連
                        self.connect()
                else:
                    # 短暫錯誤，稍微等待
                    time.sleep(0.01)

            time.sleep(0.1)  # 100ms 掃描週期 (原為 50ms 以減輕模擬器負擔)

    def _batch_read(self) -> Optional[PLCData]:
        """批次讀取 D500~D541 (42 個字組)"""
        if self.simulation_mode:
            return self._sim_read()

        try:
            if not self._plc:
                return None
            raw = self._plc.batchread_wordunits(f"D{D_BASE}", D_READ_SIZE)
            data = PLCData()
            data.trigger = raw[_OFF_TRIGGER]
            data.results = raw[_OFF_RESULT_START:_OFF_RESULT_END + 1]
            data.bt_error = raw[_OFF_BT_ERROR]
            data.heartbeat = raw[_OFF_HEARTBEAT]
            data.empty_trigger = raw[_OFF_EMPTY_TRIGGER]
            data.cycle_count = raw[_OFF_CYCLE_COUNT]
            data.ok_counts = raw[_OFF_OK_START:_OFF_OK_END + 1]
            data.ng_counts = raw[_OFF_NG_START:_OFF_NG_END + 1]
            data.reset = raw[_OFF_RESET]
            return data
        except Exception as e:
            print(f"PLC 讀取失敗: {e}")
            return None

    def _sim_read(self) -> PLCData:
        """模擬模式讀取"""
        data = PLCData()

        # 量測觸發
        if self._sim_measure_trigger:
            data.trigger = 1
            self._sim_measure_trigger = False

        # 空槍觸發
        if self._sim_empty_trigger:
            data.empty_trigger = 1
            self._sim_empty_trigger = False

        # 異常復歸觸發
        if self._sim_reset_trigger:
            data.reset = 1
            self._sim_reset_trigger = False

        # 持續狀態
        data.heartbeat = self._heartbeat_value
        data.bt_error = self._bt_error_mask
        data.ok_counts = list(self._sim_ok_counts)
        data.ng_counts = list(self._sim_ng_counts)

        return data

    def _write_heartbeat(self, value: int) -> bool:
        """寫入心跳 D514"""
        if self.simulation_mode:
            print(f"[模擬] 心跳 D514={value}")
            return True

        try:
            self._plc.batchwrite_wordunits(f"D{D_BASE + _OFF_HEARTBEAT}", [value])
            return True
        except Exception as e:
            print(f"寫入心跳失敗: {e}")
            return False

    def _update_state(self, state: PLCConnectionState):
        """更新連線狀態"""
        self._state = state
        if self._on_state_callback:
            self._on_state_callback(state)
