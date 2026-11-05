"""
商品銷量監控 → Discord 通知
支援平台：
  CYBERBIZ（K-MONSTAR、韓居、Koreepop）、91APP（微樂客）、SHOPLINE（仙女樹、FANME）、
  Ktown4u、WooCommerce（DJB STORE）

指令：
  python sales_monitor.py add <網址> [--name 顯示名稱]   新增商品並立刻記錄第一筆庫存
  python sales_monitor.py check <網址>                  只查看目前庫存（不加入監控）
  python sales_monitor.py list                          列出所有監控商品
  python sales_monitor.py test-line                     測試 LINE 失敗通知
  python sales_monitor.py run                           開始持續監控（預設指令）
  python sales_monitor.py once                          所有商品各抓一次
  python sales_monitor.py rebuild <id>                  用原始紀錄 + 目前設定重算銷量與排名

config.json 每輪重新讀取：監控執行中新增商品、改名稱、改頻率都會自動生效。
每次抓到的原始庫存都存在 data/<id>/raw.csv，之後改設定可用 rebuild 重算。
"""
import argparse
import csv
import hashlib
import base64
import json
import logging
import os
import signal
import subprocess
import re
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "config.json"
DATA_DIR = BASE / "data"
TIME_FMT = "%Y/%m/%d %H:%M:%S"
DEFAULT_SELECTOR = "input.product-variant-id[data-inventory_quantity]"
DEFAULT_ATTR = "data-inventory_quantity"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "monitor.log", encoding="utf-8")],
)
log = logging.getLogger("sales_monitor")


# ---------- 檔案 ----------
def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_config():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("找不到 config.json")
    cfg.setdefault("products", [])
    changed = False
    for product in cfg["products"]:
        url = (product.get("url") or "").strip()
        if url and not product.get("id"):  # 手動填了網址但沒填 id → 自動產生
            product["url"] = normalize_url(url)
            product["id"] = make_id(url)
            changed = True
    if changed:
        save_json(CONFIG_PATH, cfg)
    return cfg


def is_active(product):
    return bool(product.get("url")) and bool(product.get("id")) and product.get("enabled", True)


def setting(cfg, product, key, default=None):
    """商品自己的設定優先，沒有就用全域設定"""
    if product.get(key) is not None:
        return product[key]
    return cfg.get(key, default)


def fresh_state():
    return {
        "page_title": None, "first_inventory": None, "last_inventory": None, "sold": 0,
        "history": [], "unpaid_records": [], "restocks": [], "message_id": None,
        "last_delta": 0, "last_returned": 0, "last_time": None,
    }


def state_path(pid):
    return DATA_DIR / pid / "state.json"


def load_state(pid):
    st = fresh_state()
    st.update(load_json(state_path(pid), {}))
    return st


def append_raw(pid, ts, inventory, variants):
    path = DATA_DIR / pid / "raw.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "inventory", "variants"])
        w.writerow([ts, inventory, json.dumps(variants, ensure_ascii=False)])


# ---------- 抓取 ----------
KEEP_QUERY_HOSTS = ("ktown4u.com",)


def normalize_url(url):
    p = urlsplit(url.strip())
    host = p.netloc.lower()
    query = p.query if any(host == h or host.endswith("." + h) for h in KEEP_QUERY_HOSTS) else ""
    return urlunsplit((p.scheme, p.netloc, p.path, query, ""))  # 一般網站去掉 ?query；一律去掉 #desc_section


def make_id(url):
    return hashlib.md5(normalize_url(url).encode()).hexdigest()[:8]


def to_int(value):
    digits = re.sub(r"[^\d-]", "", str(value))
    if not digits:
        raise ValueError(f"無法轉成數字：{value!r}")
    return int(digits)


# 已知但尚未支援的平台：之後找出資料來源，就在這裡加上對應的讀取方式
UNSUPPORTED = {
    "makestar.com": "Makestar",
}


NINETYONE_PATH = re.compile(r"/SalePage/Index/\d+", re.IGNORECASE)
_META_CACHE = {}  # 91APP 商品的 shopId / 規格編號，只在第一次抓頁面時取得


def fetch(cfg, product, session):
    """依網址/頁面內容判斷平台，回傳 (總剩餘庫存, 各規格庫存, 頁面標題)"""
    parts = urlsplit(product["url"])
    host = parts.netloc.lower()
    for domain, name in UNSUPPORTED.items():
        if host == domain or host.endswith("." + domain):
            raise ValueError(f"尚未支援 {name}，請先把這個商品的 enabled 設為 false")
    if NINETYONE_PATH.search(parts.path):
        platform = "91APP"
        _, variants, title = fetch_91app(cfg, product, session)
    else:
        resp = session.get(product["url"], headers=cfg.get("headers", {}), timeout=20)
        resp.raise_for_status()
        html = resp.text
        if host == "ktown4u.com" or host.endswith(".ktown4u.com"):
            platform = "Ktown4u"
            variants, title = parse_ktown4u(html)
        elif SHOPLINE_MARKER in html:
            platform = "SHOPLINE"
            variants, title = parse_shopline(html)
        elif CYBERBIZ_MARKER in html:
            platform = "CYBERBIZ"
            variants, title = parse_cyberbiz(html, product)
        elif WOO_MARKER in html:
            platform = "WooCommerce"
            variants, title = fetch_woocommerce(cfg, product, html, session)
        else:
            raise ValueError("無法辨識網站平台（目前支援 CYBERBIZ、91APP、SHOPLINE、Ktown4u、WooCommerce）")
    total, variants, title = apply_variant_filter(product, variants, title)
    return total, variants, title, platform


def apply_variant_filter(product, variants, title):
    """variant_filter：只加總名稱含有這些關鍵字的規格，例如 ["WVS通路"]"""
    keywords = product.get("variant_filter")
    if keywords:
        variants = {k: v for k, v in variants.items() if any(w in k for w in keywords)}
        if not variants:
            raise ValueError(f"沒有規格符合 variant_filter：{keywords}")
    return sum(variants.values()), variants, title


def load_91app_meta(cfg, product, session):
    """從商品頁取出 shopId、規格編號、標題"""
    resp = session.get(product["url"], headers=cfg.get("headers", {}), timeout=20)
    resp.raise_for_status()
    text = resp.text
    marker = 'window.ServerRenderData["SalePageIndexViewModel"]'
    i = text.find(marker)
    if i < 0:
        raise ValueError("找不到 91APP 商品資料（可能是排隊頁、商品下架或頁面改版）")
    start = text.index("{", i)
    try:
        data, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        # 商品名稱有單引號時，91APP 會輸出 JSON 不允許的 \'，先還原再讀
        data, _ = json.JSONDecoder().raw_decode(text[start:].replace("\\'", "'"))
    sku_ids = data.get("SaleProductSKUIdList") or [
        x["SaleProductSKUId"] for x in data.get("SKUPropertySetList", [])]
    if not sku_ids:
        raise ValueError("91APP 商品資料裡沒有規格編號")
    names = {}
    for x in data.get("SKUPropertySetList", []):
        parts = [t for t in str(x.get("PropertyNameSet", "")).split(":") if t and t != "-1"]
        names[str(x["SaleProductSKUId"])] = parts[-1] if parts else ""
    return {"shop_id": data["ShopId"], "sku_ids": sku_ids, "names": names, "title": data.get("Title", "")}


def fetch_91app(cfg, product, session):
    """91APP 架設的商店（例如微音樂）：向庫存 API 取得真實可售數量"""
    url = product["url"]
    meta = _META_CACHE.get(url)
    if meta is None:
        meta = load_91app_meta(cfg, product, session)
        _META_CACHE[url] = meta

    parts = urlsplit(url)
    api = f"{parts.scheme}://{parts.netloc}/webapi/ProductStock/GetSellingQtyListNew"
    headers = dict(cfg.get("headers", {}))
    headers.update({"Accept": "application/json, text/plain, */*", "Referer": url})
    try:
        r = session.post(
            api,
            params={"v": 0, "shopId": meta["shop_id"], "lang": "zh-TW"},
            json={"ids": ",".join(str(x) for x in meta["sku_ids"]), "isShowSaleProductOuterId": False},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception:
        _META_CACHE.pop(url, None)  # 下次重新讀商品頁
        raise
    if isinstance(rows, dict):
        rows = rows.get("Data") or rows.get("data") or []

    field = product.get("qty_field") or "SellingQty"  # 也可改用 StockQty
    names = meta.get("names", {})
    variants = {}
    for x in rows:
        if field in x:
            sid = str(x["SaleProductSKUId"])
            variants[f"{sid} {names.get(sid, '')}".strip()] = to_int(x[field])
    if not variants:
        raise ValueError("91APP 沒有回傳庫存資料")
    return sum(variants.values()), variants, meta["title"]


def parse_cyberbiz(html, product):
    """CYBERBIZ 架設的商店（例如 K-MONSTAR、韓居）"""
    soup = BeautifulSoup(html, "html.parser")
    selector = product.get("selector") or DEFAULT_SELECTOR
    attr = product.get("attr") or DEFAULT_ATTR
    variants = {}
    for el in soup.select(selector):
        value = el.get(attr)
        if value is None:
            continue
        name = el.get("data-variant") or el.get("data-sku") or ""
        key = f"{el.get('value') or len(variants)} {name}".strip()
        variants[key] = to_int(value)  # 同一規格在電腦/手機版重複出現時只算一次
    if not variants:
        raise ValueError("找不到庫存欄位（可能是排隊頁、商品下架、頁面改版或不支援的平台）")
    all_ids = set(re.findall(r'"variant_id":(\d+)', html))
    if len(all_ids) > len(variants):
        log.warning("此商品有 %d 個版本，頁面只提供 %d 個版本的庫存，總銷量只計算預設版本",
                    len(all_ids), len(variants))

    og = soup.find("meta", attrs={"property": "og:title"})
    title = og["content"] if og and og.get("content") else (soup.title.string if soup.title else "")
    title = re.sub(r"\s+\|\s+[^|]*$", "", (title or "").strip())  # 去掉「 | 商店名稱」
    return variants, title


SHOPLINE_MARKER = "app.value('product', JSON.parse('"


def js_single_quoted(text, start):
    """讀出從 start 開始、以單引號結尾的 JavaScript 字串，並還原跳脫字元"""
    out, i = [], start
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "0": "\0"}
    while i < len(text):
        c = text[i]
        if c == "\\":
            n = text[i + 1]
            if n == "u":
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            if n == "x":
                out.append(chr(int(text[i + 2:i + 4], 16)))
                i += 4
                continue
            out.append(simple.get(n, n))
            i += 2
            continue
        if c == "'":
            return "".join(out)
        out.append(c)
        i += 1
    raise ValueError("SHOPLINE 商品資料格式不完整")


def parse_shopline(html):
    """SHOPLINE 架設的商店（例如仙女樹）"""
    start = html.index(SHOPLINE_MARKER) + len(SHOPLINE_MARKER)
    data = json.loads(js_single_quoted(html, start))
    if data.get("unlimited_quantity"):
        raise ValueError("商家設定不限庫存（unlimited_quantity），無法監控")

    title = (data.get("title_translations") or {}).get("zh-hant") or ""
    variants = {}
    for v in data.get("variations") or []:
        names = (v.get("fields_translations") or {}).get("zh-hant") or []
        name = " / ".join(x for x in names if x) or v.get("sku") or v.get("key")
        variants[name] = to_int(v.get("quantity", 0))
    if not variants:  # 沒有規格的商品
        variants["全部"] = to_int(data.get("quantity", 0))
    return variants, title


CYBERBIZ_MARKER = "data-inventory_quantity"
WOO_MARKER = "woocommerce"


def find_key(obj, key):
    """在巢狀 JSON 裡找第一個名為 key 的物件"""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for v in children:
        found = find_key(v, key)
        if found is not None:
            return found
    return None


def parse_ktown4u(html):
    """
    Ktown4u：簽售商品隱藏「Sales」，但 productDetails.quantity 會隨訂單往下扣（可為負數），
    用它當剩餘庫存；一般商品若有公開 totalSaleAmount，就用「負的累計銷量」，不受入庫影響。
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("找不到 Ktown4u 商品資料（可能是排隊頁或頁面改版）")
    detail = find_key(json.loads(m.group(1)), "productDetails")
    if not detail or "quantity" not in detail:
        raise ValueError("Ktown4u 商品資料裡沒有數量欄位")
    name = detail.get("productName") or ""
    sales = detail.get("totalSaleAmount")
    if not detail.get("sellQuantityHide") and isinstance(sales, (int, float)):
        value = -int(sales)
    else:
        value = to_int(detail["quantity"])
    return {f"{detail.get('productNo', '')} {name}".strip(): value}, name


def woo_stock(info):
    """WooCommerce Store API：從「6 件庫存」這類文字取出數量"""
    if not info.get("is_in_stock"):
        return 0
    text = (info.get("stock_availability") or {}).get("text", "")
    digits = re.sub(r"[^\d]", "", text)
    if digits:
        return int(digits)
    if info.get("low_stock_remaining") is not None:
        return int(info["low_stock_remaining"])
    raise ValueError("商家沒有公開庫存數量（WooCommerce 未開啟庫存顯示）")


def fetch_woocommerce(cfg, product, html, session):
    m = re.search(r"postid-(\d+)", html) or re.search(r'data-product_id="(\d+)"', html)
    if not m:
        raise ValueError("找不到 WooCommerce 商品編號")
    parts = urlsplit(product["url"])
    api = f"{parts.scheme}://{parts.netloc}/wp-json/wc/store/v1/products/"
    headers = dict(cfg.get("headers", {}))
    info = session.get(api + m.group(1), headers=headers, timeout=20).json()
    if info.get("code"):
        raise ValueError(f"WooCommerce API 錯誤：{info.get('message')}")
    title = re.sub(r"<[^>]+>", "", info.get("name", ""))
    variants = {}
    if info.get("type") == "variable" and info.get("variations"):
        for v in info["variations"]:
            vinfo = session.get(api + str(v["id"]), headers=headers, timeout=20).json()
            label = " / ".join(a.get("value", "") for a in v.get("attributes", [])) or str(v["id"])
            variants[f"{v['id']} {label}"] = woo_stock(vinfo)
    else:
        variants[f"{info.get('id')} {title}"] = woo_stock(info)
    return variants, title


# ---------- 計算 ----------
def apply_reading(state, inventory, ts, restock_threshold, drop_threshold=None):
    """用一筆新的庫存讀數更新狀態，回傳 (新增銷量, 回流量)"""
    last = state["last_inventory"]
    state["last_inventory"] = inventory
    state["last_time"] = ts
    if last is None:
        state["first_inventory"] = inventory
        return 0, 0

    change = last - inventory
    if change > 0 and drop_threshold and change >= drop_threshold:
        # 一次減少太多（例如販售結束、網站把庫存歸零），不算銷量
        state.setdefault("drops", []).append({"amount": change, "time": ts})
        log.info("庫存一次減少 %d，超過異常下降門檻，不計入銷量", change)
        return 0, 0
    if change > 0:
        state["history"].append({"delta": change, "time": ts})
        state["sold"] += change
        return change, 0
    if change < 0:
        up = -change
        if restock_threshold and up >= restock_threshold:
            state["restocks"].append({"amount": up, "time": ts})
            log.info("庫存增加 %d，超過門檻，視為補貨", up)
            return 0, 0
        state["unpaid_records"].append({"returned": up, "time": ts})
        state["sold"] -= up
        return 0, up
    return 0, 0


def total_sold(state, product):
    """監控期間銷量 + 監控前已賣出（由 initial_stock 推算）"""
    before = 0
    if isinstance(product.get("initial_stock"), (int, float)) and state["first_inventory"] is not None:
        before = int(product["initial_stock"]) - state["first_inventory"]
    return before + state["sold"]


# ---------- 排程 ----------
def parse_hm(s):
    if s == "24:00":
        return None
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def to_seconds(obj):
    if "interval_minutes" in obj:
        return max(1, int(float(obj["interval_minutes"]) * 60))
    if "interval_seconds" in obj:
        return max(1, int(obj["interval_seconds"]))
    return 300


def current_interval(cfg, product, now):
    t = now.time()
    for rule in setting(cfg, product, "schedules", []) or []:
        if not isinstance(rule, dict) or not rule.get("start") or not rule.get("end") \
                or rule.get("interval_minutes", rule.get("interval_seconds")) is None:
            log.warning("[%s] schedules 裡有一條設定不完整，已略過：%s", product.get("id"), rule)
            continue
        if rule.get("dates") and now.strftime("%Y-%m-%d") not in rule["dates"]:
            continue
        if rule.get("weekdays") is not None and now.weekday() not in rule["weekdays"]:
            continue
        try:
            start, end = parse_hm(rule["start"]), parse_hm(rule["end"])
        except ValueError:
            log.warning("[%s] schedules 時間格式錯誤，已略過：%s", product.get("id"), rule)
            continue
        if end is None:
            inside = t >= start
        elif start <= end:
            inside = start <= t < end
        else:
            inside = t >= start or t < end
        if inside:
            return to_seconds(rule)
    return to_seconds(setting(cfg, product, "default", {"interval_minutes": 5}))


# ---------- Discord ----------
def rank(records, key, n):
    return sorted(records, key=lambda h: (-h[key], h["time"]))[:n]


def fit(lines, limit=4000):
    out = []
    for line in lines:
        if sum(len(x) + 1 for x in out) + len(line) > limit:
            out.append("…")
            break
        out.append(line)
    return "\n".join(out)


def build_embeds(cfg, product, state):
    n = int(setting(cfg, product, "ranking_count", 80))
    m = int(setting(cfg, product, "unpaid_ranking_count", 50))
    name = display_name(cfg, product, state)
    footer = {"text": f"更新時間 | {state['last_time']}"}

    main = [
        f"總銷量: {total_sold(state, product)}",
        f"新增銷量: {state['last_delta']}",
        f"未付款回留紀錄: {state['last_returned']}",
    ]
    if setting(cfg, product, "show_inventory", True):
        main.append(f"剩餘庫存: {state['last_inventory']}")
        variants = state.get("last_variants") or {}
        if len(variants) > 1:
            main += [f"　• {k}: {v}" for k, v in variants.items()]
    main += ["", f"🏆 前{n}名新增銷量紀錄"]
    main += [f"{i}. +{h['delta']} 張 ({h['time']})"
             for i, h in enumerate(rank(state["history"], "delta", n), 1)] or ["（尚無紀錄）"]
    unpaid = [f"{i}. +{h['returned']} 張 ({h['time']})"
              for i, h in enumerate(rank(state["unpaid_records"], "returned", m), 1)] or ["（尚無紀錄）"]

    return [
        {"title": f"{setting(cfg, product, 'title_prefix', '')}{name}"[:256],
         "description": fit(main), "color": 0xFFFFFF, "footer": footer},
        {"title": f"💰 前{m}名未付款回留紀錄",
         "description": fit(unpaid), "color": 0xFFE500, "footer": footer},
    ]


def discord_request(session, method, url, **kwargs):
    r = None
    for _ in range(3):
        r = session.request(method, url, timeout=15, **kwargs)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        return r
    return r


def valid_webhook(url):
    return url if url and url.startswith("https://") and "你的" not in url else None


def webhook_of(cfg, product):
    return valid_webhook(setting(cfg, product, "discord_webhook_url", ""))


def alert_webhook_of(cfg, product):
    return valid_webhook(setting(cfg, product, "alert_webhook_url", ""))


def build_alert(cfg, product, state):
    """新增銷量頻道用的簡短訊息"""
    lines = []
    if state["last_delta"]:
        lines.append(f"📈 新增銷量: +{state['last_delta']} 張")
    if state["last_returned"]:
        lines.append(f"💰 未付款回流: +{state['last_returned']} 張")
    lines.append(f"總銷量: {total_sold(state, product)}")
    if setting(cfg, product, "show_inventory", True):
        lines.append(f"剩餘庫存: {state['last_inventory']}")
    return {
        "title": f"{setting(cfg, product, 'title_prefix', '')}{display_name(cfg, product, state)}"[:256],
        "description": "\n".join(lines),
        "color": 0x57F287 if state["last_delta"] else 0xFFE500,
        "footer": {"text": f"更新時間 | {state['last_time']}"},
    }


def edit_dashboard(session, url, payload, state):
    """編輯同一則訊息；訊息不存在就發新的"""
    if state["message_id"]:
        r = discord_request(session, "PATCH", f"{url}/messages/{state['message_id']}", json=payload)
        if r.status_code not in (403, 404):  # 訊息被刪或換了 webhook → 改發新訊息
            r.raise_for_status()
            return
    state["message_id"] = post_message(session, url, payload)


def display_name(cfg, product, state):
    return product.get("display_name") or state["page_title"] or product["url"]


def post_message(session, url, payload):
    r = discord_request(session, "POST", url, params={"wait": "true"}, json=payload)
    r.raise_for_status()
    return r.json()["id"]


def send_discord(cfg, product, state, session, changed=True):
    """
    discord_mode：
      edit  持續編輯同一則訊息（預設，不洗版、但不會推播）
      new   有變化時發一則新的完整訊息（會推播）
      both  持續編輯完整訊息 + 有變化時另發一則簡短通知（會推播）
    """
    url = webhook_of(cfg, product)
    if not url:
        log.warning("尚未設定 Discord webhook，只記錄不通知")
        return
    mode = setting(cfg, product, "discord_mode", "edit")
    identity = {}
    if setting(cfg, product, "webhook_username"):
        identity["username"] = setting(cfg, product, "webhook_username")
    if setting(cfg, product, "webhook_avatar_url"):
        identity["avatar_url"] = setting(cfg, product, "webhook_avatar_url")
    full = {**identity, "embeds": build_embeds(cfg, product, state)}

    # 有設定 alert_webhook_url：排名固定在主頻道，新增銷量另發到通知頻道
    alert_url = alert_webhook_of(cfg, product)
    if alert_url:
        edit_dashboard(session, url, full, state)
        if changed and (state["last_delta"] or state["last_returned"]):
            post_message(session, alert_url, {**identity, "embeds": [build_alert(cfg, product, state)]})
        return

    if mode in ("edit", "both"):
        edit_dashboard(session, url, full, state)

    if not changed:
        return
    if mode == "new":
        post_message(session, url, full)
    elif mode == "both":
        name = display_name(cfg, product, state)
        total = total_sold(state, product)
        lines = []
        if state["last_delta"]:
            lines.append(f"📈 {name}｜新增銷量 +{state['last_delta']} 張｜總銷量 {total}")
        if state["last_returned"]:
            lines.append(f"💰 {name}｜未付款回流 +{state['last_returned']} 張｜總銷量 {total}")
        if lines:
            post_message(session, url, {**identity, "content": "\n".join(lines)})


# ---------- 單一商品一輪 ----------
def option_name(key):
    """規格 key 是「編號 名稱」，顯示時只留名稱"""
    parts = key.split(" ", 1)
    return parts[1] if len(parts) == 2 and parts[0].isdigit() else key


def sub_products(product, variants, title):
    """split_variants：每個選項當成獨立活動"""
    subs = []
    stocks = product.get("initial_stock") if isinstance(product.get("initial_stock"), dict) else {}
    for key in variants:
        name = option_name(key)
        sub = dict(product)
        sub["id"] = f"{product['id']}-{hashlib.md5(key.encode()).hexdigest()[:4]}"
        sub["parent_id"] = product["id"]
        sub["variant_key"] = key
        base = product.get("display_name") or title
        sub["display_name"] = f"{base}｜{name}" if base else name
        sub["initial_stock"] = next((v for k, v in stocks.items() if k in key), None)
        subs.append(sub)
    return subs


def process_reading(cfg, product, inventory, variants, title, ts, session):
    pid = product["id"]
    append_raw(pid, ts, inventory, variants)  # 原始資料永遠先存
    state = load_state(pid)
    state["page_title"] = title or state["page_title"]
    state["last_variants"] = variants
    first = state["last_inventory"] is None
    drops_before = len(state.get("drops", []))
    delta, returned = apply_reading(state, inventory, ts, product.get("restock_threshold"),
                                    product.get("drop_threshold"))
    state["last_delta"], state["last_returned"] = delta, returned
    save_json(state_path(pid), state)

    if len(state.get("drops", [])) > drops_before:
        amount = state["drops"][-1]["amount"]
        send_line(cfg, f"⚠️ {product_label(product)}\n庫存一次減少 {amount}（{state['last_time']}），"
                       f"超過異常下降門檻，沒有算進銷量。\n如果其實是真的賣出，請調高門檻後執行 rebuild。", session)
    log.info("[%s] 剩餘 %s → 總銷量 %s，新增 %s，回留 %s",
             pid, inventory, total_sold(state, product), delta, returned)
    send_discord(cfg, product, state, session, changed=first or delta > 0 or returned > 0)
    save_json(state_path(pid), state)  # 存 message_id


def run_product(cfg, product, session):
    tz = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
    inventory, variants, title, _ = fetch(cfg, product, session)
    ts = datetime.now(tz).strftime(TIME_FMT)
    if product.get("split_variants"):
        for sub in sub_products(product, variants, title):
            key = sub["variant_key"]
            process_reading(cfg, sub, variants[key], {key: variants[key]}, title, ts, session)
            time.sleep(1)  # Discord 訊息之間錯開
    else:
        process_reading(cfg, product, inventory, variants, title, ts, session)
    return inventory, title


def find_product(cfg, pid):
    """一般商品或拆分後的選項（id 形如 母id-xxxx）"""
    for p in cfg["products"]:
        if p.get("id") == pid:
            return p
        if p.get("id") and pid.startswith(p["id"] + "-") and p.get("split_variants"):
            st = load_state(pid)
            for sub in sub_products(p, st.get("last_variants") or {}, st.get("page_title") or ""):
                if sub["id"] == pid:
                    return sub
    return None


# ---------- 指令 ----------
def cmd_add(args):
    cfg = load_config()
    url = normalize_url(args.url)
    pid = make_id(url)
    if any(p.get("id") == pid for p in cfg["products"]):
        sys.exit(f"已經在監控清單裡了（id: {pid}）")
    product = {"id": pid, "url": url, "display_name": args.name,
               "initial_stock": None, "restock_threshold": None, "drop_threshold": None, "enabled": True}
    cfg["products"].append(product)
    save_json(CONFIG_PATH, cfg)
    print(f"已加入監控，id: {pid}")
    try:
        inventory, title = run_product(cfg, product, requests.Session())
        print(f"商品：{title}\n目前剩餘庫存：{inventory}（已記錄為監控起點）")
    except Exception as e:
        print(f"⚠️ 第一次抓取失敗：{e}\n商品仍在清單中，監控執行時會繼續重試。")


def cmd_check(args):
    """只查看目前庫存，不加入監控、不記錄"""
    cfg = load_config()
    product = {"url": normalize_url(args.url)}
    try:
        total, variants, title, platform = fetch(cfg, product, requests.Session())
    except Exception as e:
        sys.exit(f"❌ 無法讀取：{e}")
    print(f"平台：{platform}\n商品：{title}")
    for k, v in variants.items():
        print(f"  • {k}: {v}")
    print(f"剩餘庫存合計：{total}")


def cmd_list(args):
    cfg = load_config()
    if not cfg["products"]:
        print("目前沒有監控商品")
    for p in cfg["products"]:
        if not p.get("url") or not p.get("id"):
            print("（空白範本，尚未填網址）")
            continue
        status = "監控中" if p.get("enabled", True) else "已停用"
        if p.get("split_variants"):
            print(f"[{p['id']}] {status}｜各選項分開計算\n    {p['url']}")
            for d in sorted(DATA_DIR.glob(p["id"] + "-*")):
                sub = find_product(cfg, d.name)
                if sub:
                    st = load_state(d.name)
                    print(f"    [{d.name}] {sub['display_name']}｜剩餘 {st['last_inventory']}"
                          f"｜總銷量 {total_sold(st, sub)}｜最後更新 {st['last_time']}")
            continue
        st = load_state(p["id"])
        name = p.get("display_name") or st["page_title"] or "（尚未抓到名稱）"
        print(f"[{p['id']}] {status}｜{name}\n    剩餘 {st['last_inventory']}｜總銷量 {total_sold(st, p)}"
              f"｜最後更新 {st['last_time']}\n    {p['url']}")


def cmd_rebuild(args):
    cfg = load_config()
    product = find_product(cfg, args.id)
    if product is None:
        sys.exit(f"找不到 id：{args.id}")
    raw = DATA_DIR / args.id / "raw.csv"
    if not raw.exists():
        sys.exit("沒有原始紀錄可重算")
    old = load_state(args.id)
    state = fresh_state()
    state["page_title"], state["message_id"] = old["page_title"], old["message_id"]
    with open(raw, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d, r = apply_reading(state, int(row["inventory"]), row["time"], product.get("restock_threshold"),
                                 product.get("drop_threshold"))
            state["last_delta"], state["last_returned"] = d, r
    save_json(state_path(args.id), state)
    print(f"重算完成：總銷量 {total_sold(state, product)}，銷量紀錄 {len(state['history'])} 筆，"
          f"回流 {len(state['unpaid_records'])} 筆，補貨 {len(state['restocks'])} 次，"
          f"異常下降 {len(state.get('drops', []))} 次")
    send_discord(cfg, product, state, requests.Session())
    save_json(state_path(args.id), state)


# ---------- LINE 通知（抓取失敗用） ----------
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def product_label(product):
    name = product.get("display_name") or load_state(product["id"])["page_title"]
    if not name and product.get("split_variants"):
        name = next((load_state(d.name)["page_title"] for d in DATA_DIR.glob(product["id"] + "-*")), None)
    name = name or product["url"]
    return f"[{product['id']}] {name}"


def send_line(cfg, text, session):
    token = (cfg.get("line_channel_access_token") or "").strip()
    user_id = (cfg.get("line_user_id") or "").strip()
    if not token or not user_id:
        log.warning("尚未設定 LINE，失敗通知只記錄在終端機")
        return False
    try:
        r = session.post(
            LINE_PUSH_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"to": user_id, "messages": [{"type": "text", "text": text[:4900]}]},
            timeout=15,
        )
    except Exception:
        log.exception("LINE 通知發送失敗")
        return False
    if r.status_code != 200:
        log.error("LINE 通知發送失敗：%s %s", r.status_code, r.text[:300])
        return False
    return True


def cmd_test_line(args):
    cfg = load_config()
    ok = send_line(cfg, "✅ 銷量監控：LINE 通知測試成功", requests.Session())
    print("已送出，請查看 LINE" if ok else "❌ 送出失敗，請看上方錯誤訊息")


PID_PATH = DATA_DIR / "run.pid"


def running_pid():
    """回傳正在執行的監控 PID，沒有就回傳 None"""
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def cmd_run(args, once=False):
    if not once:
        other = running_pid()
        if other and other != os.getpid():
            sys.exit(f"監控已經在執行中（PID {other}），不需要再啟動")
        DATA_DIR.mkdir(exist_ok=True)
        PID_PATH.write_text(str(os.getpid()))
    session = requests.Session()
    next_run, failures = {}, {}
    while True:
        cfg = load_config()
        tz = ZoneInfo(cfg.get("timezone", "Asia/Taipei"))
        for product in cfg["products"]:
            if not is_active(product):
                continue
            pid = product["id"]
            if next_run.get(pid, 0) > time.time():
                continue
            threshold = cfg.get("alert_after_failures", 5)
            try:
                run_product(cfg, product, session)
                if failures.get(pid, 0) >= threshold and cfg.get("line_notify_recovery", True):
                    send_line(cfg, f"✅ {product_label(product)}\n已恢復正常抓取", session)
                failures[pid] = 0
            except Exception as e:
                failures[pid] = failures.get(pid, 0) + 1
                log.exception("[%s] 第 %d 次連續失敗", pid, failures[pid])
                if failures[pid] == threshold:  # 同一段連續失敗只通知一次
                    send_line(cfg, f"⚠️ {product_label(product)}\n已連續 {failures[pid]} 次抓取失敗\n原因：{e}", session)
            next_run[pid] = time.time() + current_interval(cfg, product, datetime.now(tz))
            time.sleep(2)  # 多個商品之間錯開
        if once:
            return
        time.sleep(5)



# ---------- 捷徑用指令（輸出純文字，輸入可用 base64 避免引號與中文問題） ----------
SETTING_KEYS = {
    "顯示名稱": "display_name",
    "抓取頻率（分鐘）": "default",
    "初始庫存（張）": "initial_stock",
    "補貨門檻（張）": "restock_threshold",
    "異常下降門檻（張）": "drop_threshold",
    "拆分選項": "split_variants",
    "篩選選項": "variant_filter",
    "排名頻道": "discord_webhook_url",
    "通知頻道": "alert_webhook_url",
    "加速時段": "schedules",
}
SETTING_NOTE = {
    "顯示名稱": "-代表自動使用頁面標題",
    "抓取頻率（分鐘）": "-代表全域預設",
    "初始庫存（張）": "輸入數字，-代表不知道",
    "補貨門檻（張）": "輸入數字，-代表不判斷補貨",
    "異常下降門檻（張）": "一次減少達這個數字不算銷量，-代表不判斷",
    "拆分選項": "輸入「是」或「否」",
    "篩選選項": "關鍵字用逗號隔開，-代表全部",
    "排名頻道": "輸入頻道名稱或 webhook 網址，-代表全域",
    "通知頻道": "輸入頻道名稱或 webhook 網址，-代表不通知",
    "加速時段": "格式 2026-11-01 22:00-24:00 0.5，-代表清除",
}


def channel_label(cfg, url):
    if not url:
        return "未設定"
    for name, u in (cfg.get("channels") or {}).items():
        if u == url:
            return name
    return "已設定（webhook 網址）"


def fmt_num(x):
    return f"{x:g}" if isinstance(x, float) else str(x)


def current_settings(cfg, p):
    """各設定目前實際生效的值（全域設定直接顯示數值）"""
    sched = setting(cfg, p, "schedules", []) or []
    return {
        "顯示名稱": product_name(p),
        "抓取頻率（分鐘）": fmt_num(current_interval(cfg, {**p, "schedules": []}, datetime.now()) / 60),
        "初始庫存（張）": "未設定" if p.get("initial_stock") is None else str(p["initial_stock"]),
        "補貨門檻（張）": "未設定" if p.get("restock_threshold") is None else str(p["restock_threshold"]),
        "異常下降門檻（張）": "未設定" if p.get("drop_threshold") is None else str(p["drop_threshold"]),
        "拆分選項": "是" if p.get("split_variants") else "否",
        "篩選選項": "、".join(p["variant_filter"]) if p.get("variant_filter") else "全部",
        "排名頻道": channel_label(cfg, setting(cfg, p, "discord_webhook_url", "")),
        "通知頻道": channel_label(cfg, setting(cfg, p, "alert_webhook_url", "")),
        "加速時段": "、".join(f"{'/'.join(r.get('dates') or ['每天'])} {r.get('start')}-{r.get('end')} 每{fmt_num(r.get('interval_minutes'))}分"
                         for r in sched if isinstance(r, dict)) or "無",
    }


def is_blank(v):
    return v is None or v.strip() in ("", "-", "－")


def product_name(p):
    return p.get("display_name") or load_state(p["id"])["page_title"] or p["url"]


def find_by_line(cfg, line):
    if "返回" in line:
        sys.exit("已返回")
    pid = line.split("｜")[0].split("SET:")[-1].strip()  # 捷徑會在商品前面加上 SET:
    for p in cfg["products"]:
        if p.get("id") == pid:
            return p
    sys.exit(f"找不到商品：{line}")


def resolve_channel(cfg, value):
    if is_blank(value):
        return None
    value = value.strip()
    channels = cfg.get("channels") or {}
    if value in channels:
        return channels[value]
    if value.startswith("https://"):
        return value
    names = "、".join(channels) or "（尚未登記任何頻道）"
    sys.exit(f"找不到頻道「{value}」。已登記的頻道：{names}")


BACK_ITEM = "↩︎ 返回主選單"


def sc_pick(cfg, line="-"):
    """沒有商品：列出商品清單；有「SET:商品」：列出該商品的設定項目與目前的值"""
    if not is_blank(line) and "SET:" in line:
        p = find_by_line(cfg, line)
        now = current_settings(cfg, p)
        rows = [f"{label}｜{now[label]}（{SETTING_NOTE[label]}）" for label in SETTING_KEYS]
        sys.stdout.write("\n".join([BACK_ITEM] + rows))
        return
    lines = [f"{p['id']}｜{'▶️' if p.get('enabled', True) else '⏸'} {product_name(p)}"
             for p in cfg["products"] if p.get("id") and p.get("url")]
    sys.stdout.write("\n".join([BACK_ITEM] + lines))


def sc_status(cfg):
    pid = running_pid()
    print(f"監控程式：{'🟢 執行中' if pid else '🔴 未執行'}")
    shown = False
    for p in cfg["products"]:
        if not (p.get("id") and p.get("url")):
            continue
        shown = True
        mark = "▶️" if p.get("enabled", True) else "⏸"
        subs = sorted(DATA_DIR.glob(p["id"] + "-*")) if p.get("split_variants") else []
        print(f"\n{mark} {product_name(p)}")
        if subs:
            for d in subs:
                sub = find_product(cfg, d.name)
                if sub:
                    st = load_state(d.name)
                    print(f"  • {option_name(sub['variant_key'])}：總銷量 {total_sold(st, sub)}，剩餘 {st['last_inventory']}")
        else:
            st = load_state(p["id"])
            print(f"  總銷量 {total_sold(st, p)}｜剩餘 {st['last_inventory']}｜更新 {st['last_time'] or '—'}")
    if not shown:
        print("\n目前沒有監控中的商品")


def sc_add(cfg, url, name):
    url = normalize_url(url)
    pid = make_id(url)
    if any(p.get("id") == pid for p in cfg["products"]):
        sys.exit(f"這個商品已經在清單裡了（{pid}）")
    product = {"id": pid, "url": url, "enabled": True, "display_name": None if is_blank(name) else name.strip(),
               "initial_stock": None, "restock_threshold": None, "drop_threshold": None, "discord_webhook_url": None,
               "alert_webhook_url": None, "split_variants": False, "variant_filter": None,
               "default": None, "schedules": None}
    if "ktown4u.com" in url:
        product["initial_stock"] = 0  # Ktown4u 簽售從 0 往下扣
    try:
        _, variants, title, platform = fetch(cfg, product, requests.Session())
    except Exception as e:
        sys.exit(f"❌ 無法讀取這個網址，沒有加入：{e}")
    cfg["products"].append(product)
    save_json(CONFIG_PATH, cfg)
    try:
        run_product(cfg, product, requests.Session())
    except Exception as e:
        print(f"⚠️ 已加入，但第一次記錄失敗：{e}")
    print(f"✅ 已加入監控\n平台：{platform}\n商品：{product['display_name'] or title}")
    for k, v in variants.items():
        print(f"  • {option_name(k)}：{v}")
    if len(variants) > 1:
        print("\n這個商品有多個選項。若每個選項是不同活動，請到「修改設定」把「拆分選項」設為 是。")
    print("\n其他設定（頻道、頻率、補貨門檻…）可到「修改設定」調整。")


def sc_set(cfg, line, label, value):
    p = find_by_line(cfg, line)
    label = label.split("｜")[0].strip()  # 捷徑選單項目會附上填寫說明
    key = SETTING_KEYS.get(label)
    if not key:
        sys.exit(f"沒有這個設定項目：{label}")
    v = (value or "").strip()
    try:
        if key == "display_name":
            p[key] = None if is_blank(v) else v
        elif key == "default":
            p[key] = None if is_blank(v) else {"interval_minutes": float(v)}
        elif key in ("initial_stock", "restock_threshold", "drop_threshold"):
            p[key] = None if is_blank(v) else int(v.replace(",", ""))
        elif key == "split_variants":
            p[key] = v in ("是", "y", "Y", "yes", "true", "1", "開")
        elif key == "variant_filter":
            p[key] = None if is_blank(v) else [x.strip() for x in re.split(r"[,，]", v) if x.strip()]
        elif key in ("discord_webhook_url", "alert_webhook_url"):
            p[key] = resolve_channel(cfg, v)
        elif key == "schedules":
            if is_blank(v):
                p[key] = None
            else:
                m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}:\d{2})\s*[-~～]\s*(\d{1,2}:\d{2})\s+([\d.]+)", v)
                if not m:
                    sys.exit("格式錯誤，範例：2026-11-01 22:00-24:00 0.5")
                y, mo, d, start, end, mins = m.groups()
                rule = {"dates": [f"{y}-{int(mo):02d}-{int(d):02d}"], "start": start.zfill(5),
                        "end": end.zfill(5), "interval_minutes": float(mins)}
                p[key] = (p.get(key) or []) + [rule]
    except ValueError:
        sys.exit(f"「{v}」不是正確的數字")
    save_json(CONFIG_PATH, cfg)
    print(f"✅ {product_name(p)}\n{label} → {current_settings(cfg, p)[label]}")
    if key in ("restock_threshold", "drop_threshold"):
        print("（若要套用到過去的紀錄，請在電腦上執行 rebuild）")


def sc_toggle(cfg, line):
    p = find_by_line(cfg, line)
    p["enabled"] = not p.get("enabled", True)
    save_json(CONFIG_PATH, cfg)
    print(f"{'▶️ 已恢復監控' if p['enabled'] else '⏸ 已暫停監控'}：{product_name(p)}")


def sc_channel(cfg, name, url):
    if is_blank(name) or not url.strip().startswith("https://"):
        sys.exit("請輸入頻道名稱，以及 https:// 開頭的 webhook 網址")
    cfg.setdefault("channels", {})[name.strip()] = url.strip()
    save_json(CONFIG_PATH, cfg)
    print(f"✅ 已登記頻道「{name.strip()}」\n之後在「修改設定」的排名頻道／通知頻道輸入這個名稱即可")


def sc_start(cfg):
    pid = running_pid()
    if pid:
        print(f"🟢 監控已經在執行中（PID {pid}）")
        return
    DATA_DIR.mkdir(exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).resolve()), "run"]
    if Path("/usr/bin/caffeinate").exists():
        cmd = ["/usr/bin/caffeinate", "-i"] + cmd  # 避免 Mac 進入睡眠
    with open(BASE / "run.log", "a") as out:
        subprocess.Popen(cmd, cwd=BASE, stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(3)
    print("🟢 監控已啟動" if running_pid() else "⚠️ 啟動失敗，請查看 run.log")


def sc_stop(cfg):
    pid = running_pid()
    if not pid:
        print("監控目前沒有在執行")
        return
    os.killpg(os.getpgid(pid), signal.SIGTERM)
    time.sleep(1)
    PID_PATH.unlink(missing_ok=True)
    print("🔴 監控已停止")


SHORTCUT_COMMANDS = {
    "pick": (sc_pick, 1), "status": (sc_status, 0), "add": (sc_add, 2), "set": (sc_set, 3),
    "toggle": (sc_toggle, 1), "channel": (sc_channel, 2), "start": (sc_start, 0), "stop": (sc_stop, 0),
    "help-set": (None, 1), "check": (None, 1),
}


def cmd_sc(argv):
    """捷徑入口：sales_monitor.py sc <指令> [base64 參數…]"""
    if not argv or argv[0] not in SHORTCUT_COMMANDS:
        print("未知的指令")
        return
    name, raw = argv[0], argv[1:]
    try:
        params = [base64.b64decode(x + "=" * (-len(x) % 4)).decode("utf-8") if x != "-" else "-" for x in raw]
    except Exception:
        params = raw
    func, n = SHORTCUT_COMMANDS[name]
    params = (params + ["-"] * n)[:n]
    try:
        if name == "help-set":
            print(SETTING_NOTE.get(params[0].split("｜")[0].strip(), "請輸入新的值"))
        elif name == "check":
            cmd_check(argparse.Namespace(url=params[0].strip()))
        else:
            func(load_config(), *params)
    except SystemExit as e:  # 捷徑只看得到 stdout
        if e.code not in (None, 0):
            print(e.code)


def main():
    ap = argparse.ArgumentParser(description="商品銷量監控")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add")
    a.add_argument("url")
    a.add_argument("--name", default=None)
    sub.add_parser("list")
    sub.add_parser("test-line")
    c = sub.add_parser("check")
    c.add_argument("url")
    sub.add_parser("run")
    sub.add_parser("once")
    r = sub.add_parser("rebuild")
    r.add_argument("id")
    if len(sys.argv) > 1 and sys.argv[1] == "sc":
        logging.getLogger().handlers = [h for h in logging.getLogger().handlers
                                        if not isinstance(h, logging.StreamHandler) or isinstance(h, logging.FileHandler)]
        cmd_sc(sys.argv[2:])
        return
    args = ap.parse_args()

    if args.cmd == "add":
        cmd_add(args)
    elif args.cmd == "test-line":
        cmd_test_line(args)
    elif args.cmd == "check":
        cmd_check(args)
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "rebuild":
        cmd_rebuild(args)
    elif args.cmd == "once":
        cmd_run(args, once=True)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()
