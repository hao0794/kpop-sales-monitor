# K-pop 商品銷量監控系統

從電商網站公開的**庫存變化**推算 K-pop 簽售／抽獎商品的銷量，並自動發送 Discord 報表與 LINE 警示。

網站不會公開「賣了幾張」，這個專案的核心問題是：只靠看得到的庫存數字，怎麼還原出可信的銷量。

## 功能

- **多平台資料蒐集**：支援 5 種電商平台、8 家台灣通路，新增商品時自動判斷平台
- **銷量推算**：把庫存變化分成實際銷售、未付款回流、店家補貨、異常歸零四類
- **可追溯的資料流程**：原始讀數全部存在 `raw.csv`，規則調整後可用 `rebuild` 從頭重算
- **自動化報表**：Discord 即時更新總銷量、新增銷量、未付款回流、剩餘庫存與前 80 名單次購買量排名
- **失敗警示**：連續抓取失敗或數據異常時，透過 LINE Messaging API 通知
- **遠端操作**：Mac 背景執行，iPhone／iPad 透過 Tailscale + SSH + iOS 捷徑新增商品、修改設定、啟動與停止

## 支援平台

| 平台 | 通路 | 取得庫存的方式 |
|---|---|---|
| CYBERBIZ | K-MONSTAR、HG 韓居、Koreepop | 讀取 HTML 屬性 `data-inventory_quantity` |
| 91APP | Willmusic 微樂客 | 從頁面取得 ShopId、SKU，再呼叫庫存 API |
| SHOPLINE | 仙女樹、FANME | 還原 JavaScript 內嵌的 `JSON.parse('...')` 字串後解析 |
| Ktown4u | Ktown4u | 解析頁面 JSON；不限量簽售的庫存為「已售量的負值」 |
| WooCommerce | DJB STORE | 呼叫 Store API，從庫存文字解析數字 |

另外實測過 Weverse、Makestar、Yes24、Aladin、HMV、Tower Records、Music Korea 等 20 餘家通路，因為不公開庫存、不限量或數字不會即時更新而不支援。

## 銷量判斷規則

每次抓取後，比較這次與上次的庫存（`apply_reading()`）：

| 庫存變化 | 視為 | 銷量 |
|---|---|---|
| 減少，未達 `drop_threshold` | 新增銷量 | 加 |
| 減少，達到 `drop_threshold` | 異常（例如販售結束被歸零） | 不計 |
| 增加，未達 `restock_threshold` | 未付款回流 | 扣 |
| 增加，達到 `restock_threshold` | 店家補貨 | 不計 |

總銷量 = (初始庫存 − 第一次讀到的庫存) + 監控期間淨銷量。不知道初始庫存時，只計算監控期間的變化。

門檻是根據實際資料調整出來的：曾有一筆紀錄被當成「補貨 300 件」，回查 `raw.csv` 才發現是 300 → 0 的下降，也就是活動結束後庫存被歸零，因此加入 `drop_threshold`，再用 `rebuild` 修正歷史資料。

## 架構

```
商品頁面 / 庫存 API
        │  fetch()：依平台選擇解析方式
        ▼
data/<id>/raw.csv        ← 原始讀數，永遠先存
        │  apply_reading()：分類庫存變化
        ▼
data/<id>/state.json     ← 銷量、回流、補貨、排名
        │
        ├─▶ Discord：排名頻道（編輯同一則訊息）＋ 通知頻道（新增／回流短訊息）
        └─▶ LINE：連續失敗、恢復正常、異常下降警示
```

- `config.json` 每輪重新讀取，監控執行中新增商品或修改設定會自動生效
- 商品層級沒填的設定沿用全域設定
- 用 `data/run.pid` 鎖檔，避免同時執行兩個監控

## 安裝與使用

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # 填入自己的 Discord webhook、LINE token
```

| 指令 | 說明 |
|---|---|
| `python sales_monitor.py add <網址> [--name 名稱]` | 新增商品並記錄第一筆庫存 |
| `python sales_monitor.py check <網址>` | 只查看目前庫存 |
| `python sales_monitor.py list` | 列出監控中的商品 |
| `python sales_monitor.py run` | 持續監控 |
| `python sales_monitor.py once` | 所有商品各抓一次 |
| `python sales_monitor.py rebuild <id>` | 用原始紀錄和目前設定重算 |
| `python sales_monitor.py test-line` | 測試 LINE 通知 |
| `python sales_monitor.py sc start / stop / status` | 背景啟動、停止、查看狀態（iOS 捷徑使用） |

參數說明見 [`docs/參數說明.html`](docs/參數說明.html) 與 [`docs/config說明.txt`](docs/config說明.txt)，遠端操作方式見 [`docs/遠端執行監控.txt`](docs/遠端執行監控.txt)。

## 使用技術

Python・requests・BeautifulSoup・JSON / CSV 資料處理・REST API・Discord Webhook・LINE Messaging API・Tailscale・iOS 捷徑

## 注意事項

本專案僅讀取網站公開顯示的庫存資訊，每個商品預設數分鐘抓取一次，避免對網站造成負擔。推算出的銷量是間接估計，可能因補貨、下架等情況與實際數字有差異。
