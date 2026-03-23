# -*- coding: utf-8 -*-
"""
PLC 模擬器 (NiceGUI 版) - 可編輯所有 D500~D541 暫存器
"""
import socket
import threading
import struct
import time
import multiprocessing
from nicegui import ui, app

class PLCSimulator:
    def __init__(self, host='127.0.0.1', port=5000):
        self.host = host
        self.port = port
        self.registers = [0] * 1000  # 增大範圍
        self.running = False
        self.server_socket = None
        self.clients = []
        self.reg_labels = {}
        self.reg_inputs = {}   # 可編輯的 number input
        self.client_label = None

    def start_server(self):
        """啟動 TCP 服務 (在背景執行緒跑)"""
        if self.running: return

        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(5)
            self.running = True
            print(f"[*] SLMP TCP Server started on {self.host}:{self.port}")

            while self.running:
                try:
                    client_sock, addr = self.server_socket.accept()
                    client_info = f"{addr[0]}:{addr[1]}"
                    self.clients.append(client_info)
                    print(f"[+] HMI Connected: {client_info}")
                    threading.Thread(target=self._handle_client, args=(client_sock, client_info), daemon=True).start()
                except: break
        except Exception as e:
            print(f"[!] Server Bind Error: {e}")
        finally:
            if self.server_socket: self.server_socket.close()

    def _handle_client(self, sock, client_info):
        try:
            while self.running:
                header = sock.recv(9)
                if len(header) < 9: break

                data_len = struct.unpack('<H', header[7:9])[0]
                body = sock.recv(data_len)
                if len(body) < data_len: break

                command = struct.unpack('<H', body[2:4])[0]

                if command == 0x0401: # Read
                    self._process_read(sock, body)
                elif command == 0x1401: # Write
                    self._process_write(sock, body)
        except: pass
        finally:
            if client_info in self.clients: self.clients.remove(client_info)
            print(f"[-] HMI Disconnected: {client_info}")
            sock.close()

    def _process_read(self, sock, body):
        try:
            addr = body[6] + (body[7] << 8) + (body[8] << 16)
            points = struct.unpack('<H', body[10:12])[0]
            offset = addr - 500
            data = b''
            for i in range(points):
                val = self.registers[offset + i] if 0 <= (offset + i) < len(self.registers) else 0
                data += struct.pack('<H', val)
            self._send_response(sock, data)
        except: pass

    def _process_write(self, sock, body):
        try:
            addr = body[6] + (body[7] << 8) + (body[8] << 16)
            points = struct.unpack('<H', body[10:12])[0]
            values = body[12:]
            offset = addr - 500
            for i in range(points):
                if 0 <= (offset + i) < len(self.registers):
                    val = struct.unpack('<H', values[i*2:i*2+2])[0]
                    self.registers[offset + i] = val
            self._send_response(sock, b'')
        except: pass

    def _send_response(self, sock, data, end_code=0):
        header = b'\xD0\x00\x00\xFF\xFF\x03\x00'
        header += struct.pack('<H', 2 + len(data))
        sock.sendall(header + struct.pack('<H', end_code) + data)

    def trigger_bit(self, offset):
        self.registers[offset] = 1

    def set_register(self, offset, value):
        """設定單一暫存器值"""
        if 0 <= offset < len(self.registers):
            self.registers[offset] = int(value) & 0xFFFF

    def on_input_change(self, offset):
        """input 變更時立刻寫入暫存器"""
        inp = self.reg_inputs.get(offset)
        if inp is not None:
            try:
                self.set_register(offset, int(inp.value or 0))
            except (ValueError, TypeError):
                pass

    def add_input(self, offset, **kwargs):
        """建立 number input 並綁定 on_change 自動寫入暫存器"""
        inp = ui.number(on_change=lambda _, o=offset: self.on_input_change(o), **kwargs)
        self.reg_inputs[offset] = inp
        return inp

    def update_ui(self):
        # 更新客戶端標籤
        if self.client_label:
            if self.clients:
                self.client_label.set_text(f"已連線: {', '.join(self.clients)}")
                self.client_label.classes('text-green-400', remove='text-yellow-400')
            else:
                self.client_label.set_text("等待連線...")
                self.client_label.classes('text-yellow-400', remove='text-green-400')

        # 更新唯讀標籤
        for off, label in self.reg_labels.items():
            val = self.registers[off]
            label.set_text(str(val))
            if off in [0, 15, 41]:
                label.classes('text-green-500' if val else 'text-gray-400', remove='text-green-500 text-gray-400')

        # 同步暫存器值到可編輯 input (HMI 寫入時反映到 UI)
        # 因為 on_change 已即時寫入暫存器，使用者輸入不會被覆蓋
        for off, inp in self.reg_inputs.items():
            reg_val = self.registers[off]
            try:
                ui_val = int(inp.value or 0)
            except (ValueError, TypeError):
                ui_val = -1
            if ui_val != reg_val:
                inp.value = reg_val

    def reset_registers(self):
        for i in range(len(self.registers)): self.registers[i] = 0

# --- 初始化單例 ---
sim = PLCSimulator()

@ui.page('/')
def main_page():
    ui.colors(primary='#334155')
    with ui.header().classes('bg-slate-800 justify-between items-center px-4'):
        ui.label('PLC Simulator (FX5U SLMP)').classes('text-xl font-bold')
        sim.client_label = ui.label('等待連線...').classes('text-sm font-mono text-yellow-400')

    with ui.column().classes('w-full p-4 gap-4 bg-slate-50 min-h-screen'):
        # --- 觸發按鈕 ---
        with ui.row().classes('w-full gap-4'):
            ui.button('量測觸發 (D500)', on_click=lambda: sim.trigger_bit(0)).props('color=blue icon=play_arrow')
            ui.button('空槍觸發 (D515)', on_click=lambda: sim.trigger_bit(15)).props('color=cyan icon=science')
            ui.button('復歸觸發 (D541)', on_click=lambda: sim.trigger_bit(41)).props('color=red icon=refresh')
            ui.button('歸零暫存器', on_click=sim.reset_registers).props('color=grey icon=delete')

        with ui.row().classes('w-full gap-4'):
            # --- 核心暫存器 (唯讀監看) ---
            with ui.card().classes('p-4').style('width: 260px'):
                ui.label('核心暫存器 (唯讀)').classes('font-bold border-b mb-2 text-slate-700')
                for off, name in [
                    (0, 'D500 觸發'),
                    (15, 'D515 空槍'),
                    (14, 'D514 心跳'),
                    (13, 'D513 BT錯誤'),
                    (41, 'D541 復歸'),
                ]:
                    with ui.row().classes('w-full justify-between items-center'):
                        ui.label(name).classes('text-xs text-gray-500')
                        sim.reg_labels[off] = ui.label('0').classes('font-mono font-bold')

            # --- D516 測試週期 (即時寫入) ---
            with ui.card().classes('p-4').style('width: 260px'):
                ui.label('D516 測試週期次數').classes('font-bold border-b mb-2 text-slate-700')
                sim.add_input(16, value=0, step=1, min=0, max=65535).props('outlined dense').classes('w-32')

            # --- D501~D512 判定結果 (唯讀, HMI 寫入) ---
            with ui.card().classes('p-4 flex-grow'):
                ui.label('判定結果 D501~D512 (HMI 寫入, 0=PASS 1=FAIL)').classes('font-bold border-b mb-2 text-slate-700')
                with ui.row().classes('w-full justify-between'):
                    for i in range(1, 13):
                        with ui.column().classes('items-center'):
                            ui.label(f"CH{i}").classes('text-[10px]')
                            sim.reg_labels[i] = ui.label('0').classes('font-mono')

        # --- OK/NG 計數 D517~D540 (即時寫入) ---
        with ui.card().classes('p-4 w-full'):
            ui.label('OK/NG 計數 D517~D540 (修改即生效)').classes('font-bold border-b mb-2 text-slate-700')
            with ui.grid(columns=6).classes('w-full gap-2 mt-2'):
                for i in range(1, 13):
                    with ui.column().classes('items-center border rounded p-1 bg-white'):
                        ui.label(f"CH{i}").classes('text-[10px] text-gray-400')
                        ui.label('OK').classes('text-[9px] text-green-600')
                        sim.add_input(i+16, value=0, step=1, min=0, max=65535).props('outlined dense').classes('w-16').style('font-size: 12px')
                        ui.label('NG').classes('text-[9px] text-red-600')
                        sim.add_input(i+28, value=0, step=1, min=0, max=65535).props('outlined dense').classes('w-16').style('font-size: 12px')

    ui.timer(0.5, sim.update_ui)

if __name__ in {"__main__", "__mp_main__"}:
    # 確保只啟動一次服務
    if multiprocessing.current_process().name == 'MainProcess':
        threading.Thread(target=sim.start_server, daemon=True).start()

    ui.run(title="PLC Simulator", port=8082, show=False, reload=False)
