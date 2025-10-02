# chat_server.py
import socket
import ssl
import threading
import json
import datetime
import argparse

ENC = "utf-8"
BUFSZ = 4096

class ChatServer:
    def __init__(self, host: str, port: int, certfile: str, keyfile: str):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.clients = {}       # conn -> {"name": str, "addr": (ip, port)}
        self.lock = threading.Lock()
        self.running = True
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)

    def start(self):
        self.sock.bind(self.addr)
        self.sock.listen(20)
        print(f"[SERVER] Listening on {self.addr[0]}:{self.addr[1]}")

        accept_th = threading.Thread(target=self._accept_loop, daemon=True)
        accept_th.start()

        try:
            while self.running:
                accept_th.join(0.2)
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down...")
        finally:
            self.running = False
            with self.lock:
                for c in list(self.clients.keys()):
                    try:
                        c.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    c.close()
            self.sock.close()

    def _accept_loop(self):
        while self.running:
            try:
                conn, caddr = self.sock.accept()
            except OSError:
                break
            try:
                tls_conn = self.ssl_ctx.wrap_socket(conn, server_side=True)
            except ssl.SSLError as err:
                print(f"[SERVER] TLS handshake failed for {caddr}: {err}")
                conn.close()
                continue
            threading.Thread(target=self._handle_client, args=(tls_conn, caddr), daemon=True).start()

    def _broadcast(self, payload: dict, exclude_conn=None):
        data = (json.dumps(payload) + "\n").encode(ENC)
        with self.lock:
            for c in list(self.clients.keys()):
                if c is exclude_conn:
                    continue
                try:
                    c.sendall(data)
                except Exception:
                    self._drop_client(c)

    def _drop_client(self, conn):
        info = self.clients.get(conn)
        if info:
            del self.clients[conn]
            try:
                conn.close()
            except Exception:
                pass

    def _handle_client(self, conn: socket.socket, caddr):
        f = conn.makefile("r", encoding=ENC, newline="\n")
        name = None
        try:
            # 等待 JOIN
            line = f.readline()
            if not line:
                conn.close()
                return
            msg = json.loads(line)
            if msg.get("type") != "join" or "name" not in msg:
                conn.close()
                return
            name = str(msg["name"]).strip() or f"{caddr[0]}:{caddr[1]}"

            with self.lock:
                self.clients[conn] = {"name": name, "addr": caddr}

            # 系統訊息：有人加入
            self._broadcast({
                "type": "system",
                "text": f"{name} joined",
                "ts": self._ts_now()
            })

            # 收訊息迴圈
            for line in f:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                mtype = msg.get("type")
                if mtype == "chat":
                    text = msg.get("text", "")
                    payload = {
                        "type": "chat",
                        "name": name,
                        "text": text,
                        "ts": self._ts_now()
                    }
                    self._broadcast(payload)
                elif mtype == "leave":
                    break

        except Exception:
            pass
        finally:
            # 離開
            with self.lock:
                if conn in self.clients:
                    del self.clients[conn]
            try:
                conn.close()
            except Exception:
                pass
            if name:
                print(f"[SERVER] LEAVE {name}")
                self._broadcast({
                    "type": "system",
                    "text": f"{name} left",
                    "ts": self._ts_now()
                })
                
    @staticmethod
    def _ts_now():
        # 格式 mm.dd hh:mm
        return datetime.datetime.now().strftime("%m.%d %H:%M")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="bind host (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=5050, help="port (default: 5050)")
    ap.add_argument("--cert", required=True, help="path to TLS certificate (PEM)")
    ap.add_argument("--key", required=True, help="path to TLS private key (PEM)")
    args = ap.parse_args()
    ChatServer(args.host, args.port, args.cert, args.key).start()

if __name__ == "__main__":
    main()
