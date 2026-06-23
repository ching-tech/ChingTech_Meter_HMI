# -*- coding: utf-8 -*-
"""
耳溫槍 CB(ACK)流程獨立測試工具
================================
目的：用「一支」藍芽耳溫槍，測試客戶文件的正常握手流程 CD → DB → CB，
      並驗證「有送 CB / 沒送 CB」槍的行為差異（主程式從來沒送過 CB）。

此檔完全獨立，不依賴主專案任何模組，只用 Python 標準函式庫 (socket)。

用法：
    python test_cb_flow.py <MAC>
    例：python test_cb_flow.py 00:18:E4:34:D2:1A
    (不帶參數則會提示輸入)

連線後進入互動模式，可輸入指令：
    cd     送出 CD（主動要求量測）
    cb     手動送一次 CB（ACK）
    cbon   開啟「收到 DB 自動回 CB」(符合規格的握手)
    cboff  關閉自動 CB（預設；純監聽，測槍是否只在壓桿時推送）
    stat   顯示統計（收到 DB 數 / 送出 CB 數）
    q      離開

測試建議：
    1. 預設自動 CB 開：實體按壓槍數次，觀察每次 DB 後是否成功回 CB、槍是否持續正常。
    2. 輸入 cboff 關掉自動 CB，再連續按壓多次，觀察槍是否在數次後停止回應
       （若會停 → 代表 CB 是必要的；若照常 → 代表 CB 可省略）。
    3. 輸入 cd 測試主動要求量測（槍閒置時應會回 DB）。
"""
import socket
import sys
import threading
import time
import datetime

# ---- 協定常數 (與主專案 bluetooth_comm.py 一致) ----
STX = 0x02  # Start
ETX = 0x03  # End 1
EOT = 0x04  # End 2
DEVICE_TYPE_THERMOMETER = "REEB0001"
RFCOMM_PORT = 1
CONNECT_TIMEOUT = 10.0


def now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def calc_checksum(data: bytes) -> int:
    """XOR CheckSum"""
    result = 0
    for b in data:
        result ^= b
    return result


def build_cb(success: bool = True) -> bytes:
    """CB (ACK)：0x02 'CB' '1'/'2' CheckSum 0x03 0x04"""
    payload = bytes([STX]) + b'CB' + (b'1' if success else b'2')
    return payload + bytes([calc_checksum(payload), ETX, EOT])


def build_cd() -> bytes:
    """CD (要求量測)：0x02 'CD' '1' CheckSum 0x03 0x04"""
    payload = bytes([STX]) + b'CD' + b'1'
    return payload + bytes([calc_checksum(payload), ETX, EOT])


def parse_db(packet: bytes):
    """解析 DB 封包，回傳 dict 或 None（附帶錯誤原因 print）"""
    if len(packet) < 28 or packet[0] != STX:
        print(f"  [解析] 長度不足或起始碼錯誤 (len={len(packet)})")
        return None
    if packet[-2] != ETX or packet[-1] != EOT:
        print("  [解析] 結束碼錯誤")
        return None
    device_type = packet[1:9].decode('ascii', 'replace')
    if device_type != DEVICE_TYPE_THERMOMETER:
        print(f"  [解析] 設備型號不符: {device_type!r}")
        return None
    device_id = packet[9:19].decode('ascii', 'replace')
    func_id = packet[19:21].decode('ascii', 'replace')
    if func_id != 'DB':
        print(f"  [解析] 非 DB 封包 (func={func_id!r})")
        return None
    data_len = int(packet[21:23].hex(), 16)
    data_start = 23
    data_end = data_start + data_len
    data = packet[data_start:data_end]
    checksum_recv = packet[data_end]
    checksum_calc = calc_checksum(packet[:data_end])
    checksum_ok = (checksum_recv == checksum_calc)
    if not checksum_ok:
        print(f"  [解析] CheckSum 不符: 收到 0x{checksum_recv:02X}, 計算 0x{checksum_calc:02X}")
    try:
        meter_id = data[0:10].decode('ascii', 'replace').strip()
        temp_raw = data[10:14].decode('ascii', 'replace')
        trans_temp_raw = data[14:18].decode('ascii', 'replace')
        temp_mode = data[18:19].decode('ascii', 'replace')
        temperature = int(temp_raw[:2]) + int(temp_raw[2:]) / 100
    except (ValueError, IndexError) as e:
        print(f"  [解析] Data 欄位解析失敗: {e}")
        return None
    return {
        "device_id": device_id,
        "meter_id": meter_id,
        "temperature": temperature,
        "trans_temp_raw": trans_temp_raw,
        "ear_cover": "有耳溫套" if trans_temp_raw == "1111" else "無耳溫套" if trans_temp_raw == "0000" else trans_temp_raw,
        "temp_mode": temp_mode,
        "checksum_ok": checksum_ok,
    }


class CBTester:
    def __init__(self, mac: str):
        self.mac = mac
        self.sock = None
        self.running = False
        self.auto_cb = False  # 預設不自動回 CB (純監聽，測槍是否只在壓桿時推送)
        self.recv_buffer = b''
        self.db_count = 0
        self.cb_count = 0
        self.cd_count = 0
        self._lock = threading.Lock()

    def connect(self) -> bool:
        print(f"[{now()}] 連線中 → {self.mac} (RFCOMM port {RFCOMM_PORT}, timeout {CONNECT_TIMEOUT:.0f}s)…")
        try:
            self.sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
            self.sock.settimeout(CONNECT_TIMEOUT)
            self.sock.connect((self.mac, RFCOMM_PORT))
            self.sock.settimeout(2.0)
            print(f"[{now()}] ✅ 連線成功")
            return True
        except Exception as e:
            print(f"[{now()}] ❌ 連線失敗: {e}")
            return False

    def send(self, data: bytes, label: str):
        try:
            self.sock.sendall(data)
            print(f"[{now()}] → 送出 {label}: {hexdump(data)}")
            return True
        except Exception as e:
            print(f"[{now()}] ❌ 送出 {label} 失敗: {e}")
            return False

    def send_cb(self, success: bool = True):
        if self.send(build_cb(success), f"CB(ACK,{'1' if success else '2'})"):
            with self._lock:
                self.cb_count += 1

    def send_cd(self):
        if self.send(build_cd(), "CD(要求量測)"):
            with self._lock:
                self.cd_count += 1

    def _handle_packet(self, packet: bytes):
        print(f"[{now()}] ← 收到封包: {hexdump(packet)}")
        result = parse_db(packet)
        if result:
            with self._lock:
                self.db_count += 1
            cs = "OK" if result["checksum_ok"] else "✗"
            print(f"[{now()}]   DB #{self.db_count}: {result['temperature']:.2f}°C "
                  f"({result['ear_cover']}) MeterID={result['meter_id']} "
                  f"DeviceID={result['device_id']} mode={result['temp_mode']} CheckSum={cs}")
            # 收到 DB 後依設定自動回 CB
            if self.auto_cb:
                self.send_cb(True)
            else:
                print(f"[{now()}]   (自動 CB 已關閉，未回 ACK)")

    def _recv_loop(self):
        while self.running:
            try:
                data = self.sock.recv(1024)
                if not data:
                    print(f"[{now()}] ⚠ 連線中斷：對方關閉")
                    self.running = False
                    break
                self.recv_buffer += data
                # 以 ETX+EOT 切割完整封包
                while len(self.recv_buffer) >= 2:
                    end_pos = -1
                    for i in range(len(self.recv_buffer) - 1):
                        if self.recv_buffer[i] == ETX and self.recv_buffer[i + 1] == EOT:
                            end_pos = i + 2
                            break
                    if end_pos == -1:
                        break
                    packet = self.recv_buffer[:end_pos]
                    self.recv_buffer = self.recv_buffer[end_pos:]
                    self._handle_packet(packet)
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    print(f"[{now()}] ⚠ 接收錯誤: {e}")
                self.running = False
                break

    def print_stat(self):
        with self._lock:
            print(f"[{now()}] 📊 統計 — 收到 DB: {self.db_count} | 送出 CB: {self.cb_count} | "
                  f"送出 CD: {self.cd_count} | 自動CB: {'開' if self.auto_cb else '關'}")

    def run(self):
        if not self.connect():
            return
        self.running = True
        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

        print()
        print("=" * 60)
        print("進入互動模式。指令: cd / cb / cbon / cboff / stat / q")
        print("※ 預設自動 CB = 關 (純監聽)。先「完全不碰槍」觀察是否仍有 DB，")
        print("  即可判斷槍是否會閒置自動推送；再實體按壓觀察壓桿觸發。")
        print("=" * 60)
        try:
            while self.running:
                try:
                    cmd = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    break
                if not self.running:
                    break
                if cmd == "q":
                    break
                elif cmd == "cd":
                    self.send_cd()
                elif cmd == "cb":
                    self.send_cb(True)
                elif cmd == "cbon":
                    self.auto_cb = True
                    print(f"[{now()}] 自動 CB = 開 (收到 DB 會自動回 ACK)")
                elif cmd == "cboff":
                    self.auto_cb = False
                    print(f"[{now()}] 自動 CB = 關 (收到 DB 不回 ACK)")
                elif cmd == "stat":
                    self.print_stat()
                elif cmd == "":
                    continue
                else:
                    print(f"未知指令: {cmd!r} (可用: cd / cb / cbon / cboff / stat / q)")
        finally:
            self.running = False
            self.print_stat()
            try:
                if self.sock:
                    self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            print(f"[{now()}] 已關閉連線，結束。")


def main():
    if len(sys.argv) >= 2:
        mac = sys.argv[1].strip()
    else:
        mac = input("請輸入耳溫槍 MAC 位址 (例 00:18:E4:34:D2:1A): ").strip()
    if not mac:
        print("未提供 MAC，結束。")
        return
    CBTester(mac).run()


if __name__ == "__main__":
    main()
