# LAN-Chat (Windows CMD/PowerShell, TUI)

一個可在 Windows CMD／PowerShell 執行的區域網路純文字聊天室。
上半部顯示訊息，下半部單行輸入列。訊息格式：

```
[User_name]  text                                         [mm.dd hh:mm]
```

## 目錄結構

```
.
├─ chat_server.py        # 伺服器（TCP）
└─ chat_client_tui.py    # 客戶端（prompt_toolkit 全螢幕 TUI）
```

## 需求

* Python 3.10+（建議 3.11）
* 依賴套件：

  * `prompt_toolkit`（TUI 客戶端）
  * `wcwidth`（CJK 寬度計算，避免對齊錯位）
* Windows 10/11，PowerShell 或 CMD

安裝：

```powershell
python -m pip install --upgrade prompt_toolkit wcwidth
```


## TLS 憑證準備

伺服器啟動前請先在與 `chat_server.py` 相同的資料夾建立自簽憑證與私鑰：

```powershell
MSYS2_ARG_CONV_EXCL='*' openssl req -x509 -newkey rsa:2048 -keyout server.key -out server.crt -days 365 -nodes -subj "/CN=chat.local"
```

完成後會得到 `server.crt` 與 `server.key`，啟動伺服器時需透過參數載入；客戶端會自動建立 TLS 連線，不需額外設定。

## 啟動方式

### 1) 本機測試（單機多視窗）

伺服器（視窗 A）：

```powershell
python chat_server.py --host 127.0.0.1 --port 5050 --cert server.crt --key server.key
```

客戶端（視窗 B、C 各開一個）：

```powershell
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name Alice
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name Bob
```

### 2) 區網連線（伺服器跑在本機）

查本機區網 IP：

```powershell
ipconfig   # 取 IPv4，例如 192.168.1.23
```

伺服器（綁所有介面或綁該 IPv4）：

```powershell
python chat_server.py --host 0.0.0.0 --port 5050 --cert server.crt --key server.key
# 或
# python chat_server.py --host 192.168.1.23 --port 5050 --cert server.crt --key server.key
```

本機客戶端：

```powershell
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name LocalMe
```

另一台電腦的客戶端（同一子網）：

```powershell
python chat_client_tui.py --host 192.168.1.23 --port 5050 --name RemoteMe
```

首次執行若跳出 Windows 防火牆對話框，允許「私人網路」。

## 使用說明

* 下方輸入列輸入訊息並 Enter 送出。
* 指令：

  * `/exit`：離開聊天室。

額外參數：

* 若想觀察工作列閃爍行為的除錯資訊，可在啟動客戶端時加入 `--flash-debug`，日誌會以 `[FLASH]` 前綴顯示於終端畫面。

## 設計重點

* 傳輸：TCP，訊息以 NDJSON（JSON + `\n`）傳遞。
* 時間戳：由伺服器產生，格式 `mm.dd hh:mm`。
* TUI 佈局：上方歷史訊息視窗，下方單行輸入列。
* 對齊：右側時間欄採固定欄寬，並以 `wcwidth` 計算可視寬度。

## 常見問題與排錯

1. **客戶端畫面不顯示訊息**

   * 確認伺服器正在運作且沒有報錯。
   * 用 `python -m pip show prompt_toolkit wcwidth` 檢查安裝版本。
   * 專案資料夾不可有 `prompt_toolkit.py` 同名檔案或資料夾。

2. **另一台電腦連不上**

   * 伺服器需以 `0.0.0.0` 或實際區網 IP 綁定，不能用 `127.0.0.1`。
   * 在客戶端測試 TCP：
     `Test-NetConnection <server-ip> -Port 5050`
   * 防火牆允許 Python 的 5050/TCP 於「私人網路」。
   * 確認兩台在同一子網，未啟用 AP 隔離或 VPN 搶路由。

3. **時間戳 `]` 會掉到下一行**

   * 已採固定時間欄及安全邊界，若仍偶發，調整終端寬度或更換等寬字體。
   * 可在程式中把安全邊界由 1 提高到 2（已預留參數位）。

4. **埠占用**

   * `OSError: [WinError 10048]` 表示埠被占用，改用 `--port 5051` 並同步更新客戶端。

## 目前功能

* 多用戶連線與廣播
* 系統訊息：加入與離開
* TUI 輸入與歷史視窗分離
* CJK 寬度感知的對齊與裁切

## 待辦與方向

* 主機名驗證
* 訊息長度限制與簡單限流
* 房間與在線名單（/rooms, /who）
* 歷史補送、伺服器日誌輪替
* 管理指令（踢人、靜音、封鎖）

## 授權

MIT License

Copyright (c) 2025 CafeDonggua (冬瓜)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
