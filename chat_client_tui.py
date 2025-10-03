# chat_client_tui.py
# 需求: pip install prompt_toolkit
import socket
import ssl
import threading
import json
import argparse
import datetime
import unicodedata
import platform
import time
import ctypes
import ipaddress
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

from prompt_toolkit import Application
from prompt_toolkit.layout import HSplit, Window, Layout
from prompt_toolkit.layout.controls import FormattedTextControl, UIControl, UIContent
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.application.current import get_app
from prompt_toolkit.mouse_events import MouseEventType
from wcwidth import wcswidth

if platform.system() == "Windows":
    from ctypes import wintypes
else:
    wintypes = None

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


class TaskbarFlasher:
    """Windows 專用工作列閃動控制器，非 Windows 上為 no-op。"""

    _THROTTLE_SECONDS = 1.0
    _FOCUS_POLL_INTERVAL = 0.5
    def __init__(self, debug: bool = False) -> None:
        self.enabled = False
        self.is_flashing = False
        self.has_focus = True
        self.hwnd = 0
        self._lock = threading.Lock()
        self._last_trigger = 0.0
        self._running = False
        self._focus_thread: Optional[threading.Thread] = None
        self._user32 = None
        self._kernel32 = None
        self._flashwinfo_cls = None
        self._debug = debug and platform.system() == "Windows"
        self._setup()

    def _setup(self) -> None:
        if platform.system() != "Windows" or wintypes is None:
            return
        try:
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
        except Exception:
            return

        try:
            self.FLASHW_STOP = 0
            self.FLASHW_CAPTION = 0x00000001
            self.FLASHW_TRAY = 0x00000002
            self.FLASHW_ALL = self.FLASHW_CAPTION | self.FLASHW_TRAY
            self.FLASHW_TIMER = 0x00000004
            self.FLASHW_TIMERNOFG = 0x0000000C

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("hwnd", wintypes.HWND),
                    ("dwFlags", ctypes.c_uint),
                    ("uCount", ctypes.c_uint),
                    ("dwTimeout", ctypes.c_uint),
                ]

            self._flashwinfo_cls = FLASHWINFO
            if not self._ensure_hwnd(force=True):
                self._debug_print("setup", "未取得 hwnd，停用")
                return
            self.enabled = True
            self._debug_print("setup", f"啟用成功 hwnd=0x{int(self.hwnd):X}")
            self.refresh_focus()
        except Exception:
            self.enabled = False
            self.hwnd = 0
            self._debug_print("setup", "初始化失敗，停用")

    def start(self) -> None:
        if not self.enabled or self._running:
            return
        self._running = True
        self._focus_thread = threading.Thread(target=self._focus_loop, daemon=True)
        self._focus_thread.start()
        self._debug_print("thread", "焦點監控啟動")

    def shutdown(self) -> None:
        if not self.enabled:
            return
        self._running = False
        self.stop()
        self._debug_print("thread", "焦點監控停止")

    def _focus_loop(self) -> None:
        while self._running:
            try:
                self.refresh_focus()
            except Exception:
                pass
            time.sleep(self._FOCUS_POLL_INTERVAL)

    def refresh_focus(self) -> bool:
        if not self.enabled or not self._user32:
            return False
        if not self._ensure_hwnd():
            self._debug_print("focus", "未取得 hwnd，視為無焦點")
            self.has_focus = False
            return False
        try:
            fg = self._user32.GetForegroundWindow()
        except Exception:
            return self.has_focus
        focus = bool(fg) and fg == self.hwnd
        should_stop = False
        with self._lock:
            if focus != self.has_focus:
                self.has_focus = focus
                self._debug_print(
                    "focus",
                    f"fg=0x{int(fg) if fg else 0:X} hwnd=0x{int(self.hwnd):X} focus={focus}",
                )
            if focus and self.is_flashing:
                should_stop = True
        if should_stop:
            self.stop()
        return focus

    def maybe_flash(self, at_bottom: bool) -> None:
        if not self.enabled:
            return
        focus = self.refresh_focus()
        self._debug_print(
            "maybe",
            f"focus={focus} at_bottom={at_bottom} flashing={self.is_flashing} now={time.monotonic():.2f} last={self._last_trigger:.2f}",
        )
        if focus and at_bottom:
            self._debug_print("maybe", "略過：前景且在底部")
            return
        now = time.monotonic()
        with self._lock:
            if self.is_flashing:
                self._debug_print("maybe", "略過：已在閃動")
                return
            if now - self._last_trigger < self._THROTTLE_SECONDS:
                self._debug_print("maybe", f"略過：節流 {now - self._last_trigger:.2f}s")
                return
            if not self._flashwinfo_cls:
                self._debug_print("maybe", "略過：缺少 flash 結構")
                return
            if self._flash(self.FLASHW_ALL | self.FLASHW_TIMERNOFG):
                self.is_flashing = True
                self._last_trigger = now
                self._debug_print("maybe", "已觸發閃動")

    def stop(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._stop_flash_locked()

    def _stop_flash_locked(self) -> None:
        if not self.is_flashing or not self._flashwinfo_cls:
            return
        self._flash(self.FLASHW_STOP)
        self.is_flashing = False
        self._last_trigger = time.monotonic()
        self._debug_print("stop", f"停止 flags=STOP hwnd=0x{int(self.hwnd):X}")

    def _flash(self, flags: int) -> bool:
        if not self.enabled or not self._flashwinfo_cls or not self._user32:
            return False
        if not self._ensure_hwnd():
            return False
        info = self._flashwinfo_cls()
        info.cbSize = ctypes.sizeof(info)
        info.hwnd = self.hwnd
        info.dwFlags = flags
        info.uCount = 0
        info.dwTimeout = 0
        try:
            self._user32.FlashWindowEx(ctypes.byref(info))
            self._debug_print(
                "flash",
                f"flags=0x{flags:X} hwnd=0x{int(self.hwnd):X} is_flashing={self.is_flashing}",
            )
        except Exception:
            self._debug_print("flash", "呼叫失敗")
            return False
        return True

    def _ensure_hwnd(self, force: bool = False) -> bool:
        if not self._user32:
            return False
        if self.hwnd and not force:
            try:
                if self._user32.IsWindow(self.hwnd):
                    self._debug_print("ensure_hwnd", f"沿用 hwnd=0x{int(self.hwnd):X}")
                    return True
            except Exception:
                pass
        hwnd = 0
        console_hwnd = 0
        if self._kernel32:
            try:
                console_hwnd = self._kernel32.GetConsoleWindow()
            except Exception:
                console_hwnd = 0
        if console_hwnd:
            hwnd = console_hwnd
        if not hwnd:
            try:
                fg = self._user32.GetForegroundWindow()
                if fg and self._user32.IsWindow(fg):
                    hwnd = fg
            except Exception:
                hwnd = 0
        if hwnd:
            resolved = self._resolve_flash_hwnd(hwnd)
            self.hwnd = resolved
            self._debug_print(
                "ensure_hwnd",
                f"console=0x{int(console_hwnd):X} candidate=0x{int(hwnd):X} resolved=0x{int(resolved):X}",
            )
            return True
        self._debug_print(
            "ensure_hwnd",
            f"console=0x{int(console_hwnd):X} selected=0x0 取得失敗",
        )
        return False

    def _debug_print(self, tag: str, msg: str) -> None:
        if not self._debug:
            return
        try:
            print(f"[FLASH] {tag}: {msg}")
        except Exception:
            pass

    def _resolve_flash_hwnd(self, hwnd: int) -> int:
        if not hwnd or not self._user32:
            return hwnd
        GA_ROOT = 2
        GA_ROOTOWNER = 3
        try:
            root_owner = self._user32.GetAncestor(hwnd, GA_ROOTOWNER)
        except Exception:
            root_owner = 0
        try:
            root = self._user32.GetAncestor(hwnd, GA_ROOT)
        except Exception:
            root = 0
        for candidate in (root_owner, root, hwnd):
            if candidate and self._user32.IsWindow(candidate):
                if candidate != hwnd:
                    self._debug_print(
                        "resolve",
                        f"hwnd=0x{int(hwnd):X} -> 0x{int(candidate):X}",
                    )
                return candidate
        return hwnd

    def on_history_change(self, at_bottom: bool) -> None:
        if not self.enabled:
            return
        focus = self.refresh_focus()
        if at_bottom and focus:
            self.stop()

    def notify_user_activity(self) -> None:
        if not self.enabled:
            return
        self._debug_print("input", f"停止閃動 hwnd=0x{int(self.hwnd):X}")
        self.stop()

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
    def __init__(
        self,
        host: str,
        port: int,
        name: str,
        flash_debug: bool = False,
        ca_path: Optional[str] = None,
        server_name: Optional[str] = None,
        insecure: bool = False,
    ):
        self.host = host
        self.addr = (host, port)
        self.name = name
        self.server_name = server_name or host
        self._verification_enabled = bool(ca_path)
        if self._verification_enabled:
            self._insecure_mode = False
            self._insecure_reason = None
        else:
            self._insecure_mode = True
            self._insecure_reason = "--insecure" if insecure else "未指定 --ca"

        base_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self._verification_enabled:
            self.ssl_ctx = ssl.create_default_context(
                ssl.Purpose.SERVER_AUTH,
                cafile=ca_path,
            )
            self.ssl_ctx.check_hostname = True
            self.sock = self.ssl_ctx.wrap_socket(
                base_sock,
                server_hostname=self.server_name,
            )
        else:
            self.ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            self.ssl_ctx.check_hostname = False
            self.ssl_ctx.verify_mode = ssl.CERT_NONE
            self.sock = self.ssl_ctx.wrap_socket(
                base_sock,
                server_hostname=self.server_name,
            )
        self.running = True

        # UI：上方訊息窗 + 下方輸入列
        self._flasher = TaskbarFlasher(debug=flash_debug)
        self.history = ChatHistory()
        self.history.set_on_change(self._on_history_change)

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
        try:
            self.input.buffer.on_text_changed += self._on_input_buffer_changed
        except Exception:
            pass

        kb = KeyBindings()

        @kb.add("enter")
        def _(event):
            self._flasher.notify_user_activity()
            txt = self.input.text.strip()
            if not txt:
                return
            if txt == "/exit":
                self._send_json({"type": "leave"})
                self.running = False
                event.app.exit()  # 關閉 TUI
                return
            if txt == "/list":
                self._send_json({"type": "list"})
                self.input.text = ""
                return
            self._send_json({"type": "chat", "text": txt})
            self.input.text = ""  # 清空輸入列

        @kb.add("pageup")
        def _(event):
            self._flasher.notify_user_activity()
            self.history.page_up()

        @kb.add("pagedown")
        def _(event):
            self._flasher.notify_user_activity()
            self.history.page_down()

        @kb.add("c-home")
        def _(event):
            self._flasher.notify_user_activity()
            self.history.scroll_to_top()

        @kb.add("c-end")
        def _(event):
            self._flasher.notify_user_activity()
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
        except ssl.SSLCertVerificationError as err:
            self._handle_cert_error(err)
            self._cleanup_failed_connect()
            return
        except ssl.SSLError as err:
            self._handle_ssl_error(err)
            self._cleanup_failed_connect()
            return
        except Exception as err:
            print(f"[CLIENT] Connect failed: {err}")
            self._cleanup_failed_connect()
            return

        self._send_json({"type": "join", "name": self.name})

        # 開啟接收執行緒
        threading.Thread(target=self._recv_loop, daemon=True).start()

        self._flasher.start()
        self._flasher._debug_print("client", f"start enabled={self._flasher.enabled} hwnd=0x{int(self._flasher.hwnd):X}")

        # 起始提示
        self._append_system(f"Connected to {self.addr[0]}:{self.addr[1]} as {self.name}")
        if self._insecure_mode:
            reason = self._insecure_reason or "未指定 --ca"
            self._append_system(f"警告: 目前為不驗證模式（{reason}）")

        # 進入 TUI 主迴圈
        try:
            self.app.run()
        finally:
            # 清理
            self.running = False
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            self.sock.close()
            self._flasher.shutdown()

    def _cleanup_failed_connect(self) -> None:
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        self._flasher.shutdown()

    def _handle_cert_error(self, err: ssl.SSLCertVerificationError) -> None:
        print("[CLIENT] TLS 憑證驗證失敗:")
        print(f"  錯誤: {err}")
        print(f"  server_name={self.server_name} host={self.addr[0]} port={self.addr[1]}")
        if self._is_ip_address(self.addr[0]):
            print(
                "  目前使用 IP 連線，請確認伺服器憑證的 SubjectAltName 包含該 IP，"
                "或改用 --server-name 指定憑證中的 DNS 名稱並確保可解析。"
            )
        else:
            print(
                "  請確認伺服器憑證的 SubjectAltName/DNS 名稱包含上述 server_name，"
                "並與 --server-name 設定一致。"
            )

    def _handle_ssl_error(self, err: ssl.SSLError) -> None:
        print("[CLIENT] TLS 連線錯誤:")
        print(f"  錯誤: {err}")
        print(f"  server_name={self.server_name} host={self.addr[0]} port={self.addr[1]}")
        print(
            "  建議檢查客戶端與伺服器是否都啟用 TLS、TLS 版本與加密套件是否相容，"
            "以及憑證/金鑰是否正確。"
        )

    @staticmethod
    def _is_ip_address(value: str) -> bool:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

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
            self._maybe_flash_for_new_entry()
        elif mtype == "system":
            text = msg.get("text", "")
            self._append_system_with_ts(text, ts)
        elif mtype == "roster":
            users = msg.get("users")
            if not isinstance(users, list):
                users = []
            roster_text = self._format_roster_line(users)
            self._append_system_with_ts(roster_text, ts)

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

    def _on_history_change(self) -> None:
        self._invalidate()
        try:
            at_bottom = bool(self.history.follow_bottom)
        except Exception:
            at_bottom = True
        snap = self.history.snapshot()
        self._flasher._debug_print(
            "history",
            f"follow_bottom={at_bottom} view_start={snap.get('view_start')} view_end={snap.get('view_end')} total={snap.get('total')}",
        )
        self._flasher.on_history_change(at_bottom)

    def _on_input_buffer_changed(self, buffer) -> None:
        try:
            text = buffer.text
        except Exception:
            text = self.input.text
        if text and any(not ch.isspace() for ch in text):
            self._flasher.notify_user_activity()

    def _maybe_flash_for_new_entry(self) -> None:
        try:
            at_bottom = bool(self.history.follow_bottom)
        except Exception:
            at_bottom = True
        self._flasher._debug_print("event", f"new_entry at_bottom={at_bottom}")
        self._flasher.maybe_flash(at_bottom)

    def _status_text(self) -> str:
        snap = self.history.snapshot()
        total = snap.get("total", 0)
        view_end = snap.get("view_end", 0)
        position = f"{view_end}/{total}" if total else "0/0"
        state = "最新" if snap.get("follow_bottom", True) else "已回捲"
        tips_scroll = "滑鼠滾輪 或 PgUp/PgDn 捲動，Ctrl+Home 至頂，Ctrl+End 至底"
        tips_cmd = "/list 顯示名單 /exit 離開"
        return f"{tips_scroll} | {tips_cmd} | {position} {state}"


    def _append_system(self, text: str):
        ts = datetime.datetime.now().strftime("%m.%d %H:%M")
        self._append_system_with_ts(text, ts)

    def _append_system_with_ts(self, text: str, ts: str):
        self._append_entry(ChatEntry(user="SYSTEM", text=text, ts=ts))
        self._maybe_flash_for_new_entry()

    def _format_roster_line(self, users: List[object]) -> str:
        formatted = []
        for u in users:
            uname = str(u)
            if uname == self.name:
                formatted.append(f"{uname} (you)")
            else:
                formatted.append(uname)
        if not formatted:
            return "Online: (none)"
        return "Online: " + ", ".join(formatted)

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
    ap.add_argument(
        "--flash-debug",
        action="store_true",
        help="enable taskbar flash debug logging",
    )
    ap.add_argument(
        "--ca",
        help="path to CA certificate in PEM format",
    )
    ap.add_argument(
        "--server-name",
        help="override server name used for TLS SNI and hostname verification",
    )
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification",
    )
    args = ap.parse_args()
    if args.ca and args.insecure:
        ap.error("--ca 與 --insecure 不可同時使用")

    ca_path = os.path.expanduser(args.ca) if args.ca else None
    ChatClientTUI(
        args.host,
        args.port,
        args.name,
        flash_debug=args.flash_debug,
        ca_path=ca_path,
        server_name=args.server_name,
        insecure=args.insecure,
    ).start()

if __name__ == "__main__":
    main()
