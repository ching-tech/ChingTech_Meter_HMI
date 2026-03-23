# -*- coding: utf-8 -*-
"""
藍芽耳溫槍測試程式
根據 Protocol規劃_1050728.docx 實作
支援 Windows 藍芽 SPP 連線
"""
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import socket
import time
from dataclasses import dataclass
from typing import Optional
from enum import Enum


# --- 常數定義 ---
STX = 0x02  # Start Code
ETX = 0x03  # End Code 1
EOT = 0x04  # End Code 2
DEVICE_TYPE_THERMOMETER = "REEB0001"


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class ThermometerData:
    """耳溫槍量測資料"""
    meter_id: str
    temperature: float
    trans_temperature: float
    trans_temp_raw: str  # 原始估算溫度 ("1111"=有耳溫套, "0000"=無耳溫套)
    temp_mode: str
    timestamp: float


class BluetoothProtocol:
    """藍芽協定解析器"""

    @staticmethod
    def calc_checksum(data: bytes) -> int:
        """計算 XOR CheckSum"""
        result = 0
        for b in data:
            result ^= b
        return result

    @staticmethod
    def build_cb_response(success: bool) -> bytes:
        """建立 CB 回應封包 (ACK)"""
        func_id = b'CB'
        data = b'1' if success else b'2'
        payload = bytes([STX]) + func_id + data
        checksum = BluetoothProtocol.calc_checksum(payload)
        return payload + bytes([checksum, ETX, EOT])

    @staticmethod
    def build_cd_request() -> bytes:
        """建立 CD 請求封包 (主動要求量測)"""
        func_id = b'CD'
        data = b'1'
        payload = bytes([STX]) + func_id + data
        checksum = BluetoothProtocol.calc_checksum(payload)
        return payload + bytes([checksum, ETX, EOT])

    @staticmethod
    def parse_db_packet(packet: bytes) -> Optional[ThermometerData]:
        """解析 DB 封包 (量測資料)"""
        try:
            if len(packet) < 28 or packet[0] != STX:
                return None
            if packet[-2] != ETX or packet[-1] != EOT:
                return None

            device_type = packet[1:9].decode('ascii')
            if device_type != DEVICE_TYPE_THERMOMETER:
                return None

            device_id = packet[9:19].decode('ascii')
            func_id = packet[19:21].decode('ascii')
            if func_id != 'DB':
                return None

            data_len = int(packet[21:23].hex(), 16)
            data_start = 23
            data_end = data_start + data_len
            data = packet[data_start:data_end]

            checksum_received = packet[data_end]
            checksum_calculated = BluetoothProtocol.calc_checksum(packet[:data_end])
            if checksum_received != checksum_calculated:
                print(f"CheckSum 錯誤: 收到 {checksum_received}, 計算 {checksum_calculated}")
                return None

            meter_id = data[0:10].decode('ascii').strip()
            temp_raw = data[10:14].decode('ascii')
            trans_temp_raw = data[14:18].decode('ascii')
            temp_mode = data[18:19].decode('ascii')

            temperature = int(temp_raw[:2]) + int(temp_raw[2:]) / 100
            trans_temperature = int(trans_temp_raw[:2]) + int(trans_temp_raw[2:]) / 100

            return ThermometerData(
                meter_id=meter_id,
                temperature=temperature,
                trans_temperature=trans_temperature,
                trans_temp_raw=trans_temp_raw,
                temp_mode=temp_mode,
                timestamp=time.time()
            )
        except Exception as e:
            print(f"解析封包錯誤: {e}")
            return None


class ThermometerTestApp:
    """耳溫槍測試應用程式"""

    def __init__(self, root):
        self.root = root
        self.root.title("藍芽耳溫槍測試程式")
        self.root.geometry("600x600")
        self.root.resizable(False, False)

        self.socket: Optional[socket.socket] = None
        self.state = ConnectionState.DISCONNECTED
        self.running = False
        self.receive_thread: Optional[threading.Thread] = None
        self.simulation_mode = True
        self._waiting_cd_response = False  # 是否正在等待 CD 回應
        self._sim_timer_id = None  # 模擬被動 DB 定時器

        self._create_widgets()

    def _create_widgets(self):
        """建立 UI 元件"""
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === 連線設定區 ===
        conn_frame = ttk.LabelFrame(main_frame, text="連線設定", padding="10")
        conn_frame.pack(fill=tk.X, pady=(0, 10))

        # MAC 位址輸入
        ttk.Label(conn_frame, text="MAC 位址:").grid(row=0, column=0, sticky=tk.W)
        self.mac_entry = ttk.Entry(conn_frame, width=20)
        self.mac_entry.grid(row=0, column=1, padx=5)
        self.mac_entry.insert(0, "00:00:00:00:00:00")

        # RFCOMM 通道
        ttk.Label(conn_frame, text="通道:").grid(row=0, column=2, sticky=tk.W, padx=(10, 0))
        self.channel_entry = ttk.Entry(conn_frame, width=5)
        self.channel_entry.grid(row=0, column=3, padx=5)
        self.channel_entry.insert(0, "1")

        # 模擬模式勾選
        self.sim_var = tk.BooleanVar(value=True)
        self.sim_check = ttk.Checkbutton(
            conn_frame,
            text="模擬模式",
            variable=self.sim_var,
            command=self.on_simulation_toggle
        )
        self.sim_check.grid(row=1, column=0, columnspan=2, pady=(5, 0), sticky=tk.W)

        # 連線按鈕
        self.connect_btn = ttk.Button(
            conn_frame,
            text="連線",
            command=self.toggle_connection
        )
        self.connect_btn.grid(row=1, column=2, columnspan=2, pady=(5, 0))

        # 連線狀態
        self.status_label = ttk.Label(conn_frame, text="狀態: 未連線", foreground="gray")
        self.status_label.grid(row=2, column=0, columnspan=4, pady=(5, 0))

        # === 量測控制區 ===
        measure_frame = ttk.LabelFrame(main_frame, text="量測控制", padding="10")
        measure_frame.pack(fill=tk.X, pady=(0, 10))

        style = ttk.Style()
        style.configure('Large.TButton', font=('微軟正黑體', 16, 'bold'))

        self.measure_btn = ttk.Button(
            measure_frame,
            text="擷取溫度",
            command=self.capture_temperature,
            state=tk.DISABLED,
            style='Large.TButton'
        )
        self.measure_btn.pack(fill=tk.X, ipady=20)

        # === 資料顯示區 (左: 溫度, 右: CD) ===
        data_frame = ttk.Frame(main_frame)
        data_frame.pack(fill=tk.X, pady=(0, 10))
        data_frame.columnconfigure(0, weight=1)
        data_frame.columnconfigure(1, weight=1)

        # --- 左側: 溫度資料 (被動接收 DB) ---
        db_frame = ttk.LabelFrame(data_frame, text="溫度資料 (DB)", padding="10")
        db_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self.db_temp_display = ttk.Label(
            db_frame, text="--.- °C",
            font=('Arial', 28, 'bold'), anchor=tk.CENTER
        )
        self.db_temp_display.pack(fill=tk.X, pady=5)

        db_detail = ttk.Frame(db_frame)
        db_detail.pack(fill=tk.X)

        ttk.Label(db_detail, text="設備 ID:").grid(row=0, column=0, sticky=tk.W)
        self.db_device_id_label = ttk.Label(db_detail, text="--")
        self.db_device_id_label.grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Label(db_detail, text="耳溫套:").grid(row=1, column=0, sticky=tk.W)
        self.db_ear_cover_label = ttk.Label(db_detail, text="--")
        self.db_ear_cover_label.grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(db_detail, text="時間:").grid(row=2, column=0, sticky=tk.W)
        self.db_time_label = ttk.Label(db_detail, text="--")
        self.db_time_label.grid(row=2, column=1, sticky=tk.W, padx=5)

        # --- 右側: 主動要量測資料 (CD) ---
        cd_frame = ttk.LabelFrame(data_frame, text="主動要量測資料 (CD)", padding="10")
        cd_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        self.cd_temp_display = ttk.Label(
            cd_frame, text="--.- °C",
            font=('Arial', 28, 'bold'), anchor=tk.CENTER
        )
        self.cd_temp_display.pack(fill=tk.X, pady=5)

        cd_detail = ttk.Frame(cd_frame)
        cd_detail.pack(fill=tk.X)

        ttk.Label(cd_detail, text="設備 ID:").grid(row=0, column=0, sticky=tk.W)
        self.cd_device_id_label = ttk.Label(cd_detail, text="--")
        self.cd_device_id_label.grid(row=0, column=1, sticky=tk.W, padx=5)

        ttk.Label(cd_detail, text="耳溫套:").grid(row=1, column=0, sticky=tk.W)
        self.cd_ear_cover_label = ttk.Label(cd_detail, text="--")
        self.cd_ear_cover_label.grid(row=1, column=1, sticky=tk.W, padx=5)

        ttk.Label(cd_detail, text="時間:").grid(row=2, column=0, sticky=tk.W)
        self.cd_time_label = ttk.Label(cd_detail, text="--")
        self.cd_time_label.grid(row=2, column=1, sticky=tk.W, padx=5)

        # === 日誌區 ===
        log_frame = ttk.LabelFrame(main_frame, text="通訊日誌", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=8, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def log(self, message: str):
        """記錄日誌"""
        self.log_text.configure(state=tk.NORMAL)
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def on_simulation_toggle(self):
        """切換模擬模式"""
        self.simulation_mode = self.sim_var.get()
        mode = "模擬模式" if self.simulation_mode else "實際模式"
        self.log(f"切換至 {mode}")

    def toggle_connection(self):
        """切換連線狀態"""
        if self.state == ConnectionState.CONNECTED:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        """連線"""
        if self.simulation_mode:
            self.log("模擬模式連線...")
            self.state = ConnectionState.CONNECTED
            self.update_connection_status()
            self.log("模擬連線成功")
            self._start_sim_db_timer()
            return

        mac = self.mac_entry.get().strip()
        if mac == "00:00:00:00:00:00":
            messagebox.showwarning("警告", "請輸入有效的 MAC 位址")
            return

        try:
            channel = int(self.channel_entry.get().strip())
        except ValueError:
            channel = 1

        self.state = ConnectionState.CONNECTING
        self.update_connection_status()
        self.log(f"正在連線至 {mac} (通道 {channel})...")

        threading.Thread(target=self._connect_async, args=(mac, channel), daemon=True).start()

    def _connect_async(self, mac: str, channel: int):
        """非同步連線"""
        try:
            # Windows Bluetooth Socket (RFCOMM)
            # Protocol 3 = BTPROTO_RFCOMM
            self.socket = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM
            )
            self.socket.settimeout(10)
            self.socket.connect((mac, channel))
            self.socket.settimeout(2)

            self.running = True
            self.state = ConnectionState.CONNECTED
            self.root.after(0, self.update_connection_status)
            self.root.after(0, lambda: self.log("藍芽連線成功"))

            # 啟動接收執行緒
            self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.receive_thread.start()

        except Exception as e:
            self.state = ConnectionState.ERROR
            err_msg = str(e)
            self.root.after(0, self.update_connection_status)
            self.root.after(0, lambda msg=err_msg: self.log(f"連線失敗: {msg}"))

    def disconnect(self):
        """斷線"""
        self._stop_sim_db_timer()
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        self.state = ConnectionState.DISCONNECTED
        self.update_connection_status()
        self.log("已斷線")

    def _receive_loop(self):
        """接收資料迴圈"""
        buffer = b''
        while self.running and self.socket:
            try:
                data = self.socket.recv(1024)
                if data:
                    buffer += data
                    self.root.after(0, lambda d=data: self.log(
                        f"收到 HEX: {d.hex(' ').upper()}\n"
                        f"         文字: {d.decode('ascii', errors='replace')}"
                    ))

                    # 嘗試解析封包
                    while len(buffer) >= 2:
                        # 尋找 ETX EOT 結尾
                        end_pos = -1
                        for i in range(len(buffer) - 1):
                            if buffer[i] == ETX and buffer[i + 1] == EOT:
                                end_pos = i + 2
                                break

                        if end_pos == -1:
                            break

                        packet = buffer[:end_pos]
                        buffer = buffer[end_pos:]

                        result = BluetoothProtocol.parse_db_packet(packet)
                        if result:
                            self.root.after(0, lambda r=result: self.update_db_display(r))
                            if self._waiting_cd_response:
                                self._waiting_cd_response = False
                                self.root.after(0, lambda r=result: self.update_cd_display(r))

            except socket.timeout:
                pass
            except Exception as e:
                if self.running:
                    err_msg = str(e)
                    self.root.after(0, lambda msg=err_msg: self.log(f"接收錯誤: {msg}"))
                break

    def update_connection_status(self):
        """更新連線狀態顯示"""
        status_map = {
            ConnectionState.DISCONNECTED: ("狀態: 未連線", "gray", "連線", tk.DISABLED),
            ConnectionState.CONNECTING: ("狀態: 連線中...", "orange", "連線中...", tk.DISABLED),
            ConnectionState.CONNECTED: ("狀態: 已連線", "green", "斷線", tk.NORMAL),
            ConnectionState.ERROR: ("狀態: 連線錯誤", "red", "重新連線", tk.DISABLED),
        }

        text, color, btn_text, measure_state = status_map.get(
            self.state,
            ("狀態: 未知", "gray", "連線", tk.DISABLED)
        )

        self.status_label.configure(text=text, foreground=color)
        self.connect_btn.configure(text=btn_text)
        self.measure_btn.configure(state=measure_state)

    def capture_temperature(self):
        """擷取溫度 (發送 CD，等待 DB 回應)"""
        self._waiting_cd_response = True
        self.log("發送擷取溫度指令 (CD)...")

        if self.simulation_mode:
            import random
            temp = round(random.uniform(36.0, 37.5), 2)
            trans_raw = random.choice(["1111", "0000"])  # 隨機模擬有無耳溫套
            data = ThermometerData(
                meter_id="SIM0000001",
                temperature=temp,
                trans_temperature=0.0,
                trans_temp_raw=trans_raw,
                temp_mode="1",
                timestamp=time.time()
            )
            self.root.after(500, lambda: (self.update_db_display(data), self.update_cd_display(data)))
            self.log(f"模擬: CD 指令已發送 (耳溫套: {'有' if trans_raw == '1111' else '無'})")
            return

        if not self.socket:
            self.log("錯誤: 未連線")
            return

        try:
            packet = BluetoothProtocol.build_cd_request()
            self.socket.send(packet)
            self.log(f"CD 發送 HEX: {packet.hex(' ').upper()}\n"
                     f"         文字: {packet.decode('ascii', errors='replace')}")
        except Exception as e:
            self.log(f"發送失敗: {e}")

    def _get_temp_color(self, temp: float) -> str:
        """取得溫度對應顏色"""
        if temp < 35.0:
            return "blue"
        elif temp > 38.0:
            return "red"
        return "green"

    def _get_ear_cover_text(self, raw: str) -> tuple:
        """取得耳溫套狀態文字與顏色"""
        if raw == "1111":
            return "有耳溫套", "green"
        elif raw == "0000":
            return "無耳溫套", "red"
        return f"未知 ({raw})", "orange"

    def update_db_display(self, data: ThermometerData):
        """更新溫度資料顯示 (被動接收 DB)"""
        self.db_temp_display.configure(
            text=f"{data.temperature:.2f} °C",
            foreground=self._get_temp_color(data.temperature)
        )
        self.db_device_id_label.configure(text=data.meter_id)

        text, color = self._get_ear_cover_text(data.trans_temp_raw)
        self.db_ear_cover_label.configure(text=text, foreground=color)

        time_str = time.strftime("%H:%M:%S", time.localtime(data.timestamp))
        self.db_time_label.configure(text=time_str)

        self.log(f"[DB] 溫度: {data.temperature:.2f}°C, 耳溫套: {text}")

    def update_cd_display(self, data: ThermometerData):
        """更新主動要量測資料顯示 (CD 回應)"""
        self.cd_temp_display.configure(
            text=f"{data.temperature:.2f} °C",
            foreground=self._get_temp_color(data.temperature)
        )
        self.cd_device_id_label.configure(text=data.meter_id)

        text, color = self._get_ear_cover_text(data.trans_temp_raw)
        self.cd_ear_cover_label.configure(text=text, foreground=color)

        time_str = time.strftime("%H:%M:%S", time.localtime(data.timestamp))
        self.cd_time_label.configure(text=time_str)

        self.log(f"[CD] 溫度: {data.temperature:.2f}°C, 耳溫套: {text}")

    def _start_sim_db_timer(self):
        """啟動模擬被動 DB 資料定時器 (每 3 秒)"""
        import random
        temp = round(random.uniform(36.0, 37.5), 2)
        trans_raw = random.choice(["1111", "0000"])
        data = ThermometerData(
            meter_id="SIM0000001",
            temperature=temp,
            trans_temperature=0.0,
            trans_temp_raw=trans_raw,
            temp_mode="1",
            timestamp=time.time()
        )
        self.update_db_display(data)
        self._sim_timer_id = self.root.after(3000, self._start_sim_db_timer)

    def _stop_sim_db_timer(self):
        """停止模擬被動 DB 定時器"""
        if self._sim_timer_id:
            self.root.after_cancel(self._sim_timer_id)
            self._sim_timer_id = None

    def on_closing(self):
        """關閉視窗"""
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = ThermometerTestApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
