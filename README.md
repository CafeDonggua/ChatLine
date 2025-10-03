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

下面提供兩種情境：快速本機測試（自簽、客戶端略過驗證）與建立 CA + 正式簽發的伺服器憑證（客戶端可驗證）。請依需求擇一或全部完成。
憑證生成的部分請使用 Git Bash 執行。

### A. LocalCertificate：快速本機測試（不驗證模式）

建立憑證資料夾並產生伺服器自簽憑證：

```powershell
mkdir LocalCertificate
MSYS2_ARG_CONV_EXCL='*' openssl req -x509 -newkey rsa:2048 -keyout LocalCertificate/server.key -out LocalCertificate/server.crt -days 365 -nodes -subj "/CN=chat.local"
```

啟動伺服器：

```powershell
python chat_server.py --host 127.0.0.1 --port 5050 --cert LocalCertificate/server.crt --key LocalCertificate/server.key
```

客戶端若未指定 `--ca`，預設會停留在不驗證模式（與舊版相容）。可直接啟動：

```powershell
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name Alice
```

或手動標示目前為不驗證連線：

```powershell
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name Bob --insecure
```

首次連線會在 TUI 顯示「不驗證模式」警告，提醒僅適用於開發／內網。

### B. CACertificate：建立自有 CA 以啟用客戶端驗證

1. 建立資料夾並產生 CA 與伺服器金鑰：

   ```powershell
   mkdir CACertificate
   openssl genrsa -out CACertificate/ca.key 4096
   openssl genrsa -out CACertificate/server.key 2048
   ```

2. 建立 CA 憑證：

   ```powershell
   MSYS2_ARG_CONV_EXCL='*' openssl req -x509 -new -key CACertificate/ca.key -sha256 -days 3650 -out CACertificate/ca.crt -subj "/CN=lan-chat-ca" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -addext "subjectKeyIdentifier=hash"
   ```

   * 請將產生的 `CACertificate/ca.crt` 提供給所有需要驗證伺服器的客戶端，並同時保留此檔案於 `CACertificate` 資料夾中供伺服器使用。

3. 產生伺服器 CSR：

   ```powershell
   MSYS2_ARG_CONV_EXCL='*' openssl req -new -key CACertificate/server.key -out CACertificate/server.csr -subj "/CN=chat.local"
   ```

4. 建立 SubjectAltName 設定檔：

   ```powershell
   cat > CACertificate/san.cnf <<'EOF'
   basicConstraints = critical,CA:FALSE
   keyUsage = critical, digitalSignature, keyEncipherment
   extendedKeyUsage = serverAuth
   subjectKeyIdentifier = hash
   authorityKeyIdentifier = keyid,issuer
   subjectAltName = @alt_names

   [alt_names]
   DNS.1 = chat.local
   IP.1  = 192.168.47.33
   EOF
   ```

5. 由 CA 簽發伺服器憑證：

   ```powershell
   MSYS2_ARG_CONV_EXCL='*' openssl x509 -req -in CACertificate/server.csr -CA CACertificate/ca.crt -CAkey CACertificate/ca.key -CAcreateserial -out CACertificate/server.crt -days 825 -sha256 -extfile CACertificate/san.cnf
   ```

6. 啟動伺服器與驗證客戶端：

   ```powershell
   python chat_server.py --host 127.0.0.1 --port 5050 --cert CACertificate\server.crt --key CACertificate\server.key
   python chat_client_tui.py --host 127.0.0.1 --ca CACertificate\ca.crt --server-name chat.local --port 5050 --name Alice
   ```

   * `--ca` 指定 CA 憑證後會強制啟用驗證與主機名比對。
   * `--server-name` 控制 SNI 與憑證主機名驗證，可與實際連線的 `--host` 不同（例如 `--host` 使用 IP）。
   * 若使用 IP 連線，請確保 SAN 內含對應 IP（如上例的 `IP.1`）。

## 啟動方式

### 1) 本機測試（單機多視窗）

伺服器（視窗 A）：

```powershell
python chat_server.py --host 127.0.0.1 --port 5050 --cert LocalCertificate/server.crt --key LocalCertificate/server.key
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
python chat_server.py --host 0.0.0.0 --port 5050 --cert LocalCertificate/server.crt --key LocalCertificate/server.key
# 或
# python chat_server.py --host 192.168.1.23 --port 5050 --cert LocalCertificate/server.crt --key LocalCertificate/server.key
```

本機客戶端（不驗證，維持開發體驗）：

```powershell
python chat_client_tui.py --host 127.0.0.1 --port 5050 --name LocalMe
```

另一台電腦的客戶端（同一子網）：

```powershell
python chat_client_tui.py --host 192.168.1.23 --port 5050 --name RemoteMe
```

若要使用 CA 驗證，請改用上節的 `CACertificate` 憑證並於客戶端加入 `--ca` 與 `--server-name`。

首次執行若跳出 Windows 防火牆對話框，允許「私人網路」。

## 使用說明

* 下方輸入列輸入訊息並 Enter 送出。
* 指令：

  * `/exit`：離開聊天室。

額外參數：

* `--flash-debug`：觀察工作列閃爍除錯資訊，日誌會以 `[FLASH]` 顯示。
* `--insecure`：明確切換到不驗證模式，適用於開發或初次連線。未指定 `--ca` 時預設即為不驗證，但會在 TUI 顯示提醒。
* `--ca / --server-name`：啟用 TLS 憑證驗證與主機名比對（詳見「TLS 憑證準備」章節）。

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
* 客戶端支援 TLS 憑證驗證與主機名比對（`--ca` / `--server-name` / `--insecure`）

## 待辦與方向

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
