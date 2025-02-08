import socket
import struct
import threading
import time

FLAG_ACK  = 1
FLAG_DATA = 2

SIZE = 1024              
BUFFER_SIZE = 64 * SIZE  

HEADER_FORMAT = "!BII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

class UDPBasedProtocol:
    def __init__(self, *, local_addr, remote_addr):
        self.udp_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.remote_addr = remote_addr
        self.udp_socket.bind(local_addr)

    def sendto(self, data):
        return self.udp_socket.sendto(data, self.remote_addr)

    def recvfrom(self, n):
        msg, addr = self.udp_socket.recvfrom(n)
        return msg

    def close(self):
        self.udp_socket.close()


class MyTCPProtocol(UDPBasedProtocol):
    

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.send_lock = threading.Lock()
        self.send_cv = threading.Condition(self.send_lock)
        self.send_base = 0      
        self.next_seq = 0       
        self.sent_segments = {}
        self.retransmission_timeout = 0.01

        self.recv_lock = threading.Lock()
        self.recv_cv = threading.Condition(self.recv_lock)
        self.expected_seq = 0 
        self.recv_buffer = []
        self.out_of_order = {}

        self.closed = False
        self.udp_socket.settimeout(0.05)

        self.receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self.receiver_thread.start()
        self.retransmission_thread = threading.Thread(target=self._retransmission_loop, daemon=True)
        self.retransmission_thread.start()

    def send(self, data: bytes):
        total_len = len(data)
        sent = 0

        while sent < total_len:
            segment = data[sent:sent + SIZE]
            seg_len = len(segment)
            with self.recv_lock:
                current_ack = self.expected_seq

            with self.send_cv:
                while (self.next_seq - self.send_base) >= BUFFER_SIZE:
                    self.send_cv.wait()
                seq = self.next_seq
                header = struct.pack(HEADER_FORMAT, FLAG_DATA + FLAG_ACK, seq, current_ack)
                packet = header + segment
                self.sendto(packet)
                self.sent_segments[seq] = [packet, time.time(), seg_len]
                self.next_seq += seg_len
            sent += seg_len

        return total_len

    def recv(self, n: int):
        with self.recv_cv:
            while len(self.recv_buffer) < n:
                self.recv_cv.wait()
            result = self.recv_buffer[:n]
            del self.recv_buffer[:n]
            return bytes(result)

    def _process_ack(self, ack):
        with self.send_cv:
            if ack > self.send_base:
                while self.sent_segments:
                    first_key = next(iter(self.sent_segments))
                    if first_key < ack:
                        self.sent_segments.pop(first_key)
                    else:
                        break
                self.send_base = ack
                self.send_cv.notify_all()

    def _receiver_loop(self):
        while not self.closed:
            try:
                packet = self.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                continue
                
            if len(packet) < HEADER_SIZE:
                continue

            try:
                flags, seq, ack = struct.unpack(HEADER_FORMAT, packet[:HEADER_SIZE])
            except Exception:
                continue

            payload = packet[HEADER_SIZE:]

            if flags & FLAG_ACK:
                self._process_ack(ack)

            if flags & FLAG_DATA:
                with self.recv_cv:
                    if seq == self.expected_seq:
                        self.recv_buffer.extend(payload)
                        self.expected_seq += len(payload)
                        while self.expected_seq in self.out_of_order:
                            buffered_payload = self.out_of_order.pop(self.expected_seq)
                            self.recv_buffer.extend(buffered_payload)
                            self.expected_seq += len(buffered_payload)
                        self.recv_cv.notify_all()
                    elif seq > self.expected_seq:
                        if seq not in self.out_of_order:
                            self.out_of_order[seq] = payload

                ack_packet = struct.pack(HEADER_FORMAT, FLAG_ACK, 0, self.expected_seq)
                self.sendto(ack_packet)

    def _retransmission_loop(self):
        while not self.closed:
            time.sleep(0.005)
            now = time.time()
            with self.send_cv:
                for seq, info in self.sent_segments.items():
                    packet, timestamp, seg_len = info
                    if now - timestamp > self.retransmission_timeout:
                        self.sendto(packet)
                        self.sent_segments[seq][1] = now
                    else:
                        break

    def close(self):
        self.closed = True
        with self.send_cv:
            self.send_cv.notify_all()
        with self.recv_cv:
            self.recv_cv.notify_all()
        if self.receiver_thread.is_alive():
            self.receiver_thread.join(timeout=1)
        if self.retransmission_thread.is_alive():
            self.retransmission_thread.join(timeout=1)
        super().close()