# chat_client_tui.py
# 需求: pip install prompt_toolkit
import socket
import threading
import json
import argparse
import datetime
import unicodedata

from prompt_toolkit import Application
from prompt_toolkit.layout import HSplit, Window, Layout
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.application.current import get_app
from wcwidth import wcswidth

ENC = "utf-8"

def east_asian_width(s: str) -> int:
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return w

def clip_width(s: str, maxw: int) -> str:
    w = 0
    out = []
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if w + cw > maxw:
            break
        out.append(ch)
        w += cw
    return "".join(out)

def format_line(user: str, text: str, ts: str, term_cols: int) -> str:
    """
    固定右側時間欄寬：time_w；左側訊息欄寬：max_text_w = term_cols - time_w - 1
    左欄內容： "[user]  " + text_clipped + padding
    右欄內容： " " + "[mm.dd hh:mm]"
    """
    ts_str = f"[{ts}]"                         # 例如 "[09.30 17:33]"
    # 預留：時間欄最小寬度 12（含括號），並採實際可見寬度較大者
    time_w = max(12, visible_width(ts_str))
    safe_gap = 1                               # 左右欄之間至少 1 空白
    # 預留 1 欄安全邊界，避免剛好卡到終端寬度（可依需要改成 2）
    safety_margin = 1
    cols = max(20, term_cols - safety_margin)

    max_text_w = max(1, cols - time_w - safe_gap)

    prefix = f"[{user}]  "
    prefix_w = visible_width(prefix)

    # 可分配給 text 的寬度
    max_text_only_w = max(0, max_text_w - prefix_w)
    text_clipped = clip_by_width(text, max_text_only_w)

    left = f"{prefix}{text_clipped}"
    left_w = visible_width(left)

    # 用空白補到左欄固定寬度
    pad = max(0, max_text_w - left_w)
    return f"{left}{' ' * pad}{' '}{ts_str}"


def visible_width(s: str) -> int:
    # 更精確的可見寬度估算（CJK/符號）
    w = wcswidth(s)
    return w if w >= 0 else len(s)

def clip_by_width(s: str, maxw: int) -> str:
    # 依可見寬度裁切字串到 maxw
    if maxw <= 0:
        return ""
    out, w = [], 0
    for ch in s:
        cw = wcswidth(ch)
        cw = cw if cw > 0 else 1
        if w + cw > maxw:
            break
        out.append(ch)
        w += cw
    return "".join(out)

class ChatClientTUI:
    def __init__(self, host: str, port: int, name: str):
        self.addr = (host, port)
        self.name = name
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.running = True

        # UI：上方訊息窗 + 下方輸入列
        self.output = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=False,
            focusable=False
        )
        self.input = TextArea(
            height=1,
            prompt="> ",
            multiline=False
        )

        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            txt = self.input.text.strip()
            if not txt:
                return
            if txt == "/exit":
                self._send_json({"type": "leave"})
                self.running = False
                event.app.exit()  # 關閉 TUI
                return
            self._send_json({"type": "chat", "text": txt})
            self.input.text = ""  # 清空輸入列

        root = HSplit([
            self.output,
            Window(height=1, char="-"),
            self.input
        ])

        style = Style.from_dict({
            "prompt": "bold",
        })
        self.app = Application(
            layout=Layout(root, focused_element=self.input),
            key_bindings=kb,
            style=style,
            full_screen=True,
        )

    def start(self):
        try:
            self.sock.connect(self.addr)
        except Exception as e:
            print(f"[CLIENT] Connect failed: {e}")
            return

        self._send_json({"type": "join", "name": self.name})

        # 開啟接收執行緒
        threading.Thread(target=self._recv_loop, daemon=True).start()

        # 起始提示
        self._append_system(f"Connected to {self.addr[0]}:{self.addr[1]} as {self.name}")
        self._append_system("Type /exit to leave.")

        # 進入 TUI 主迴圈
        self.app.run()

        # 清理
        self.running = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        self.sock.close()

    def _recv_loop(self):
        f = self.sock.makefile("r", encoding=ENC, newline="\n")
        while self.running:
            line = f.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle_msg(msg)
        self._append_system("Disconnected from server.")

        # 關閉應用（若還在）
        if self.running:
            self.running = False
            try:
                get_app().exit()
            except Exception:
                pass

    def _handle_msg(self, msg: dict):
        mtype = msg.get("type")
        ts = msg.get("ts") or datetime.datetime.now().strftime("%m.%d %H:%M")
        cols = self._term_cols()

        if mtype == "chat":
            user = msg.get("name", "?")
            text = msg.get("text", "")
            line = format_line(user, text, ts, cols)
            self._append_line(line)
        elif mtype == "system":
            self._append_line(format_line("SYSTEM", msg.get("text", ""), ts, cols))

    def _append_line(self, line: str):
        def append_text_direct():
            if self.output.text:
                self.output.text = self.output.text + "\n" + line
            else:
                self.output.text = line
            self.output.buffer.cursor_position = len(self.output.text)

        try:
            # app 尚未 run 時，沒有事件迴圈，直接寫
            from prompt_toolkit.application.current import get_app_or_none
            app = get_app_or_none()
            if app is None or not app.is_running:
                append_text_direct()
            else:
                self.app.call_from_executor(append_text_direct)
        except Exception:
            append_text_direct()



    def _append_system(self, text: str):
        cols = self._term_cols()
        ts = datetime.datetime.now().strftime("%m.%d %H:%M")
        self._append_line(format_line("SYSTEM", text, ts, cols))

    def _send_json(self, obj: dict):
        try:
            data = (json.dumps(obj) + "\n").encode(ENC)
            self.sock.sendall(data)
        except Exception:
            pass

    def _term_cols(self) -> int:
        try:
            return self.app.output.get_size().columns
        except Exception:
            return 80

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="server host/IP")
    ap.add_argument("--port", type=int, default=5050, help="server port (default: 5050)")
    ap.add_argument("--name", required=True, help="your user name")
    args = ap.parse_args()
    ChatClientTUI(args.host, args.port, args.name).start()

if __name__ == "__main__":
    main()

