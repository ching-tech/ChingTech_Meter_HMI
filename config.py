# -*- coding: utf-8 -*-
"""
設定管理模組 - 管理系統設定與參數
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import List, Optional

# --- 設定檔路徑 ---
# 支援命令列參數: python main.py --config slave_test/config.json
import sys
CONFIG_FILE = "config.json"
for i, arg in enumerate(sys.argv):
    if arg == "--config" and i + 1 < len(sys.argv):
        CONFIG_FILE = sys.argv[i + 1]
        break

# --- 通道顯示名稱對應表 ---
CHANNEL_DISPLAY_NAMES = {
    1: 'CH11', 2: 'CH9', 3: 'CH7', 4: 'CH5', 5: 'CH3', 6: 'CH1',
    7: 'CH12', 8: 'CH10', 9: 'CH8', 10: 'CH6', 11: 'CH4', 12: 'CH2',
}

def get_channel_display_name(channel: int) -> str:
    """取得通道的顯示名稱"""
    return CHANNEL_DISPLAY_NAMES.get(channel, f'CH{channel}')

@dataclass
class BluetoothConfig:
    """藍芽設定"""
    enabled: bool = True
    device_addresses: List[str] = field(default_factory=lambda: [""] * 6)  # 6 支耳溫槍 MAC
    reconnect_interval: float = 5.0  # 重連間隔 (秒)
    timeout: float = 10.0  # 連線超時 (秒)

@dataclass
class PLCConfig:
    """PLC 設定 (FX5U 3E 協議, 暫存器 D500~D541 固定)"""
    enabled: bool = True
    ip_address: str = "192.168.1.10"
    port: int = 5000

@dataclass
class NetworkConfig:
    """Master-Slave 網路設定"""
    mode: str = "master"  # "master" 或 "slave"
    master_ip: str = "192.168.1.100"
    port: int = 5001
    slave_meter_offset: int = 6  # Slave 的 Meter ID 偏移 (7-12)

@dataclass
class TimingConfig:
    """時序設定"""
    empty_collect_delay: float = 0.5    # 空槍值收集延遲 (秒)
    measure_collect_delay: float = 0.5  # 溫度量測收集延遲 (秒)
    bt_request_interval: float = 0.1    # 藍芽請求間隔 (秒)
    plc_poll_interval: float = 0.1      # PLC 輪詢間隔 (秒)
    result_hold_time: float = 1.0       # 結果保持時間 (秒)

@dataclass
class MeasurementConfig:
    """量測設定"""
    tolerance_upper: float = 0.5   # 誤差上限 (°C)
    tolerance_lower: float = -0.5  # 誤差下限 (°C)
    empty_upper: float = 40.0      # 空槍值上限 (°C)
    empty_lower: float = 20.0      # 空槍值下限 (°C)
    meter_count: int = 12          # 總通道數
    # 通道啟用狀態 (True=啟用, False=停用)
    channel_enabled: List[bool] = field(default_factory=lambda: [True] * 12)
    # 溫度異常檢測
    temp_anomaly_enabled: bool = False   # 溫度異常使用開關
    temp_anomaly_upper: float = 42.0     # 溫度異常上限 (°C)
    temp_anomaly_lower: float = 30.0     # 溫度異常下限 (°C)
    # 連續無套異常檢測
    no_cover_anomaly_enabled: bool = False  # 連續無套異常使用開關
    no_cover_anomaly_count: int = 3         # 連續無套觸發次數

@dataclass
class AppConfig:
    """應用程式設定"""
    version: str = "2.0.0"
    title: str = "三橋耳溫槍探頭套檢測系統"
    window_width: int = 1920
    window_height: int = 900
    simulation_mode: bool = True  # 模擬模式 (無硬體時使用)
    log_dir: str = "logs"  # 量測記錄目錄
    current_batch: str = ""  # 目前批次檔名 (程式重啟時沿用)

    bluetooth: BluetoothConfig = field(default_factory=BluetoothConfig)
    plc: PLCConfig = field(default_factory=PLCConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)


def load_config() -> AppConfig:
    """載入設定檔"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _dict_to_config(data)
        except Exception as e:
            print(f"載入設定檔失敗: {e}，使用預設值")
    return AppConfig()


def save_config(config: AppConfig) -> bool:
    """儲存設定檔"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(asdict(config), f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"儲存設定檔失敗: {e}")
        return False


def _dict_to_config(data: dict) -> AppConfig:
    """將字典轉換為設定物件"""
    config = AppConfig()

    # 更新基本欄位
    for key in ['version', 'title', 'window_width', 'window_height', 'simulation_mode', 'log_dir', 'current_batch']:
        if key in data:
            setattr(config, key, data[key])

    # 更新子設定
    if 'bluetooth' in data:
        config.bluetooth = BluetoothConfig(**data['bluetooth'])
    if 'plc' in data:
        # 過濾未知 key，相容舊 config.json 中的 M 暫存器欄位
        plc_fields = {f.name for f in PLCConfig.__dataclass_fields__.values()}
        plc_data = {k: v for k, v in data['plc'].items() if k in plc_fields}
        config.plc = PLCConfig(**plc_data)
    if 'network' in data:
        config.network = NetworkConfig(**data['network'])
    if 'measurement' in data:
        meas_data = data['measurement'].copy()
        # 過濾未知 key，相容舊版 config.json
        meas_fields = {f.name for f in MeasurementConfig.__dataclass_fields__.values()}
        meas_data = {k: v for k, v in meas_data.items() if k in meas_fields}
        # 確保 channel_enabled 有 12 個元素
        if 'channel_enabled' in meas_data:
            enabled = meas_data['channel_enabled']
            # 補足不足的通道為 True
            while len(enabled) < 12:
                enabled.append(True)
            meas_data['channel_enabled'] = enabled[:12]
        config.measurement = MeasurementConfig(**meas_data)
    if 'timing' in data:
        config.timing = TimingConfig(**data['timing'])

    return config


# --- 全域設定實例 ---
config = load_config()
