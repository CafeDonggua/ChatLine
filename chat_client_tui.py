# chat_client_tui.py
# 需求: pip install prompt_toolkit
import socket
import ssl
import threading
import json
import argparse
import datetime
import unicodedata
from dataclasses import dataclass
from typing import Callable, List

from prompt_toolkit import Application
from prompt_toolkit.layout import HSplit, Window, Layout
from prompt_toolkit.layout.controls import FormattedTextControl, UIControl, UIContent
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.application.current import get_app
from prompt_toolkit.mouse_events import MouseEventType
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


@dataclass
class ChatEntry:
    user: str
    text: str
    ts: str


class ChatHistory:
    def __init__(self, max_entries: int = 10000):
        self.max_entries = max_entries
        self.entries: List[ChatEntry] = []
        self.view_start = 0
        self.follow_bottom = True
        self.last_height = 0
        self.lock = threading.Lock()
        self.on_change: Callable[[], None] | None = None
        self._last_snapshot = {
            "total": 0,
            "view_start": 0,
            "view_end": 0,
            "follow_bottom": True,
            "height": 0,
        }

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        self.on_change = callback

    def append(self, entry: ChatEntry) -> None:
        with self.lock:
            self.entries.append(entry)
            overflow = len(self.entries) - self.max_entries
            if overflow > 0:
                del self.entries[:overflow]
                self.view_start = max(0, self.view_start - overflow)

            if self.follow_bottom:
                target_start = self._max_start(height_hint=self.last_height)
                self.view_start = target_start
            else:
                self.view_start = self._clamp_view_start(self.view_start)

        self._notify_change()

    def render(self, width: int, height: int) -> List[str]:
        height = max(1, height)
        with self.lock:
            self.last_height = height
            total = len(self.entries)
            max_start = self._max_start(height_hint=height)

            if self.follow_bottom:
                self.view_start = max_start
            else:
                self.view_start = self._clamp_view_start(self.view_start, height)

            start = self.view_start
            end = min(total, start + height)
            visible_entries = self.entries[start:end]
            lines = [format_line(e.user, e.text, e.ts, width) for e in visible_entries]

            missing = height - len(lines)
            if missing > 0:
                lines.extend(["" for _ in range(missing)])

            self._last_snapshot = {
                "total": total,
                "view_start": start,
                "view_end": end,
                "follow_bottom": self.follow_bottom,
                "height": height,
            }

        return lines

    def scroll_up(self, amount: int) -> None:
        if amount <= 0:
            return
        with self.lock:
            self.follow_bottom = False
            self.view_start = max(0, self.view_start - amount)
        self._notify_change()

    def scroll_down(self, amount: int) -> None:
        if amount <= 0:
            return
        with self.lock:
            max_start = self._max_start(height_hint=self.last_height)
            new_start = min(self.view_start + amount, max_start)
            self.view_start = new_start
            self.follow_bottom = new_start >= max_start
        self._notify_change()

    def page_up(self) -> None:
        step = max(1, self.last_height - 1)
        self.scroll_up(step)

    def page_down(self) -> None:
        step = max(1, self.last_height - 1)
        self.scroll_down(step)

    def scroll_to_top(self) -> None:
        with self.lock:
            self.follow_bottom = False
            self.view_start = 0
        self._notify_change()

    def scroll_to_bottom(self) -> None:
        with self.lock:
            self.follow_bottom = True
            self.view_start = self._max_start(height_hint=self.last_height)
        self._notify_change()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self._last_snapshot)

    def _max_start(self, height_hint: int) -> int:
        height = max(1, height_hint)
        total = len(self.entries)
        return max(0, total - height)

    def _clamp_view_start(self, value: int, height_hint: int | None = None) -> int:
        height = self.last_height if height_hint is None else height_hint
        max_start = self._max_start(height)
        return max(0, min(value, max_start))

    def _notify_change(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass


class ChatHistoryControl(UIControl):
    def __init__(self, history: ChatHistory):
        self.history = history
        self._last_height = 1

    def is_focusable(self) -> bool:
        return False

    def create_content(self, width: int, height: int | None) -> UIContent:
        real_height = height if height and height > 0 else self._last_height
        real_height = max(1, real_height)
        self._last_height = real_height
        lines = self.history.render(width, real_height)

        if not lines:
            lines = [""]

        def get_line(i: int):
            return [("", lines[i])]

        return UIContent(
            get_line=get_line,
            line_count=len(lines),
            show_cursor=False,
        )

    def mouse_handler(self, mouse_event) -> object:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.history.scroll_up(3)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.history.scroll_down(3)
            return None
        return NotImplemented


class ChatClientTUI:
    def __init__(self, host: str, port: int, name: str):
        self.addr = (host, port)
        self.name = name
        base_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self.ssl_ctx.check_hostname = False
        self.ssl_ctx.verify_mode = ssl.CERT_NONE
        self.sock = self.ssl_ctx.wrap_socket(base_sock, server_hostname=host)
        self.running = True

        # UI：上方訊息窗 + 下方輸入列
        self.history = ChatHistory()
        self.history.set_on_change(self._invalidate)

        self.history_control = ChatHistoryControl(self.history)
        self.output_window = Window(
            content=self.history_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )

        self.status_control = FormattedTextControl(self._status_text)
        self.status_bar = Window(
            height=1,
            content=self.status_control,
            dont_extend_height=True,
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

        @kb.add("pageup")
        def _(event):
            self.history.page_up()

        @kb.add("pagedown")
        def _(event):
            self.history.page_down()

        @kb.add("c-home")
        def _(event):
            self.history.scroll_to_top()

        @kb.add("c-end")
        def _(event):
            self.history.scroll_to_bottom()

        root = HSplit([
            self.output_window,
            Window(height=1, char="-"),
            self.status_bar,
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
            mouse_support=True,
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

        if mtype == "chat":
            user = msg.get("name", "?")
            text = msg.get("text", "")
            self._append_entry(ChatEntry(user=user, text=text, ts=ts))
        elif mtype == "system":
            text = msg.get("text", "")
            self._append_entry(ChatEntry(user="SYSTEM", text=text, ts=ts))

    def _append_entry(self, entry: ChatEntry):
        try:
            self.history.append(entry)
        except Exception:
            pass

    def _invalidate(self) -> None:
        try:
            if hasattr(self, "app"):
                self.app.invalidate()
        except Exception:
            pass

    def _status_text(self) -> str:
        snap = self.history.snapshot()
        total = snap.get("total", 0)
        view_end = snap.get("view_end", 0)
        position = f"{view_end}/{total}" if total else "0/0"
        state = "最新" if snap.get("follow_bottom", True) else "已回捲"
        return (
            "滑鼠滾輪 或 PgUp/PgDn 捲動，Ctrl+Home 至頂，Ctrl+End 至底 | "
            f"{position} {state}"
        )


    def _append_system(self, text: str):
        ts = datetime.datetime.now().strftime("%m.%d %H:%M")
        self._append_entry(ChatEntry(user="SYSTEM", text=text, ts=ts))

    def _send_json(self, obj: dict):
        try:
            data = (json.dumps(obj) + "\n").encode(ENC)
            self.sock.sendall(data)
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="server host/IP")
    ap.add_argument("--port", type=int, default=5050, help="server port (default: 5050)")
    ap.add_argument("--name", required=True, help="your user name")
    args = ap.parse_args()
    ChatClientTUI(args.host, args.port, args.name).start()

if __name__ == "__main__":
    main()
