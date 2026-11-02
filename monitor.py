import os
import time
import requests
from curl_cffi import requests as cffi_requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# 從 GitHub Secrets 自動安全讀取金鑰
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# 雲端運行參數設定
SCAN_INTERVAL_SEC = 60          # 每 60 秒掃描一次全站
RUN_DURATION_MINUTES = 340      # 單次雲端任務持續運行 340 分鐘 (約 5.6 小時，無縫接力每 6 小時的排程)
ALARM_ROI_THRESHOLD = 95.0      # 回本率 >= 95% 觸發警報 (抓每日必賺池與 97% 池)
ALARM_DELTA_ROI = 6.0           # 或撿漏指數 ΔROI >= +6.0% 且當前 ROI >= 80% 觸發警報
PC_PACK_VALUE = 15.0            # PC散包預設估值
MUTED_POOL_IDS = {40}           # 預設忽略 #40 新人限2次池 (可透過 /mute 或 /unmute 動態增刪)

HK_TZ = timezone(timedelta(hours=8))

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "language": "zh-Hant",
    "origin": "https://www.luckutcg.com",
    "referer": "https://www.luckutcg.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 使用 curl_cffi 偽裝真實 Chrome TLS 指紋
session = cffi_requests.Session(impersonate="chrome120")
last_scan_error = ""
latest_pools = []

def send_tg(text: str, reply_to_message_id: int = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[⚠️ 未設定 TG_BOT_TOKEN 或 TG_CHAT_ID]\n" + text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"

    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)] if len(text) > 4000 else [text]

    for chunk in chunks:
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            reply_to_message_id = None
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"[TG 發送異常] {e}")

def setup_tg_commands():
    """啟動時自動呼叫 Telegram setMyCommands API 註冊指令選單"""
    if not TG_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "top", "description": "查看全站回本率 Top 10 (有貨且頭獎尚在)"},
        {"command": "snipe", "description": "查看撿漏指數 ΔROI Top 5"},
        {"command": "daily", "description": "查看每日任務與限抽福利池狀態"},
        {"command": "status", "description": "查看目前監控設定、靜音名單與運行狀態"},
        {"command": "help", "description": "顯示完整指令說明 (含 /pool, /mute, /unmute, /roi)"}
    ]
    try:
        r = requests.post(url, json={"commands": commands}, timeout=10)
        print(f"[TG 指令選單註冊] 狀態碼: {r.status_code}")
    except Exception as e:
        print(f"[TG setMyCommands 異常] {e}")

def init_tg_offset() -> int:
    """啟動時抓取最新 update_id，忽略啟動前的歷史訊息"""
    if not TG_BOT_TOKEN:
        return 0
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=5)
        if r.status_code == 200:
            res = r.json()
            if res.get("ok"):
                items = res.get("result", [])
                if items:
                    return items[-1]["update_id"] + 1
    except Exception as e:
        print(f"[初始化 TG Offset 異常] {e}")
    return 0

def get_tg_updates(offset: int = 0, timeout: int = 5) -> list:
    """呼叫 Telegram getUpdates 進行長輪詢監聽"""
    if not TG_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates"
    params = {
        "timeout": timeout,
        "allowed_updates": ["message"]
    }
    if offset:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=timeout + 5)
        if r.status_code == 200:
            res = r.json()
            if res.get("ok"):
                return res.get("result", [])
    except Exception:
        pass
    return []

def calc_pool_metrics(pool_id: int, info: dict, meta: dict) -> dict:
    price = float(info.get("card_draw_price") or 0)
    rem = int(info.get("total_remaining") or 0)
    stats = info.get("tag_stats") or {}
    data = info.get("data") or {}
    order = info.get("card_tag_order") or list(stats.keys())

    std_sum, init_sum, init_total, first_tag_rem = 0.0, 0.0, 0, 1
    min_unit = float("inf")
    top_prizes = []

    for idx, tag in enumerate(order):
        st = stats.get(tag)
        if not st:
            continue
        u, t = int(st.get("undrawn", 0)), int(st.get("total", 0))
        if idx == 0:
            first_tag_rem = u
        init_total += t

        items = data.get(tag) or []
        in_stock = [c for c in items if int(c.get("card_stock") or 0) > 0]
        stock_cnt = sum(int(c.get("card_stock") or 0) for c in in_stock)

        tag_lower = tag.lower()
        is_pc = "pc" in tag_lower
        is_filler = any(k in tag_lower for k in ["5等", "五等", "6等", "六等", "7等", "七等", "pc", "白卡", "末賞"])
        if not is_filler:
            top_prizes.append(f"{tag}:{u}/{t}")

        if items:
            all_ps = [float(c.get("card_price") or 0) for c in items]
            min_unit = min(min_unit, min(all_ps))
            v_init = sum(all_ps) if len(items) == t else t * (sum(all_ps) / len(all_ps))
        else:
            fb = PC_PACK_VALUE if is_pc else 0.0
            v_init = t * fb
            min_unit = min(min_unit, fb)
        init_sum += v_init

        if u == 0:
            v_std = 0.0
        elif items and stock_cnt == u:
            v_std = sum(float(c.get("card_price") or 0) * int(c.get("card_stock") or 0) for c in in_stock)
        elif in_stock:
            ps = [float(c.get("card_price") or 0) for c in in_stock]
            v_std = u * (sum(ps) / len(ps))
        else:
            v_std = u * (PC_PACK_VALUE if is_pc else 0.0)
        std_sum += v_std

    init_ev = (init_sum / init_total) if init_total > 0 else 0.0
    init_roi = (init_ev / price * 100) if price > 0 else 0.0
    ev_std = (std_sum / rem) if rem > 0 else init_ev
    roi_std = (ev_std / price * 100) if (rem > 0 and price > 0) else init_roi
    delta_roi = (roi_std - init_roi) if rem > 0 else 0.0

    lim_tags = []
    if int(info.get("daily_draw_limit") or 0) > 0:
        lim_tags.append(f"日限{info['daily_draw_limit']}抽")
    if int(info.get("draw_limit_per_user") or 0) > 0:
        lim_tags.append(f"人限{info['draw_limit_per_user']}抽")

    updated_at = info.get("updated_at") or meta.get("updated_at") or ""

    return {
        "id": pool_id,
        "name": info.get("card_sort_name", f"Pool #{pool_id}"),
        "price": price,
        "rem": rem,
        "total": init_total,
        "ev": ev_std,
        "roi": roi_std,
        "init_roi": init_roi,
        "delta_roi": delta_roi,
        "min_unit": 0.0 if min_unit == float("inf") else min_unit,
        "first_tag_rem": first_tag_rem,
        "limit_str": "/".join(lim_tags),
        "top_str": " | ".join(top_prizes),
        "updated_at": updated_at
    }

def fetch_single_pool(p_meta: dict):
    pid = p_meta.get("card_sort_id")
    try:
        pid_int = int(pid)
        r = session.post(
            "https://api.luckutcg.com/api/getCardsBySort",
            headers=API_HEADERS,
            json={"page": "1", "limit": "9999", "card_sort_id": str(pid_int)},
            timeout=15
        )
        if r.status_code != 200:
            print(f"[獲取卡池 #{pid} 異常] HTTP {r.status_code}: {r.text[:150]}")
            return None
        res_json = r.json()
        if res_json and isinstance(res_json.get("results"), dict):
            return calc_pool_metrics(pid_int, res_json["results"], p_meta)
    except Exception as e:
        print(f"[獲取卡池 #{pid} 異常] {type(e).__name__} - {e}")
    return None

def scan_all_pools() -> list:
    global last_scan_error
    last_scan_error = ""
    status_code = None
    try:
        r = session.post(
            "https://api.luckutcg.com/api/getCardSortByBrand",
            headers=API_HEADERS,
            json={"page": "1", "limit": "100", "brand": ""},
            timeout=15
        )
        status_code = r.status_code
        if r.status_code != 200:
            err = f"HTTP 狀態碼異常 [{r.status_code}]: {r.text[:200]}"
            print(f"[掃描全部卡池失敗] {err}")
            last_scan_error = err
            return []

        res_json = r.json()
        results_data = res_json.get("results")
        if isinstance(results_data, dict):
            pools_meta = results_data.get("data", [])
        elif isinstance(results_data, list):
            pools_meta = results_data
        else:
            pools_meta = []

        if not pools_meta:
            err = f"API 未回傳任何卡池資料 (msg: {res_json.get('msg', '無')})"
            print(f"[掃描全部卡池警告] {err}")
            last_scan_error = err
            return []

    except Exception as e:
        status_info = f" [HTTP {status_code}]" if status_code is not None else ""
        err = f"請求/解析異常{status_info}: {type(e).__name__} - {e}"
        print(f"[掃描全部卡池失敗] {err}")
        last_scan_error = err
        return []

    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single_pool, p) for p in pools_meta]
        for f in as_completed(futures):
            try:
                data = f.result()
                if data:
                    results.append(data)
            except Exception as e:
                print(f"[卡池解析異常] {type(e).__name__} - {e}")
    results.sort(key=lambda x: x["roi"], reverse=True)
    return results

# ======================== 指令處理函式 ========================

def handle_top() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    active = [p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
    if not active:
        return "ℹ️ 目前全站暫無「有貨且頭獎尚在」的卡池。"
    top10 = active[:10]
    lines = ["🏆 <b>全站回本率 Top 10 (有貨且頭獎在)：</b>\n"]
    for idx, p in enumerate(top10, 1):
        lim = f" [{p['limit_str']}]" if p['limit_str'] else ""
        lines.append(
            f"{idx}. <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {p['name']}</b></a>{lim}\n"
            f"   💰 單抽: <b>{p['price']:.0f}</b> | 📊 EV: <b>{p['ev']:.1f}</b>\n"
            f"   📈 ROI: <b>{p['roi']:.1f}%</b> (Δ{p['delta_roi']:+.1f}%) | 📦 剩: <b>{p['rem']}/{p['total']}</b>\n"
            f"   🎁 前排: {p['top_str']}"
        )
    return "\n\n".join(lines)

def handle_snipe() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    active = [p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
    if not active:
        return "ℹ️ 目前全站暫無「有貨且頭獎尚在」的卡池。"
    snipe_pools = sorted(active, key=lambda x: x["delta_roi"], reverse=True)[:5]
    lines = ["🎯 <b>撿漏指數 ΔROI Top 5 (大獎沉底回本飆升)：</b>\n"]
    for idx, p in enumerate(snipe_pools, 1):
        lim = f" [{p['limit_str']}]" if p['limit_str'] else ""
        lines.append(
            f"{idx}. <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {p['name']}</b></a>{lim}\n"
            f"   🎯 撿漏指數 ΔROI: <b>{p['delta_roi']:+.1f}%</b> (現 {p['roi']:.1f}% / 初 {p['init_roi']:.1f}%)\n"
            f"   💰 單抽: <b>{p['price']:.0f}</b> | 📊 EV: <b>{p['ev']:.1f}</b> | 📦 剩: <b>{p['rem']}/{p['total']}</b>\n"
            f"   🎁 前排: {p['top_str']}"
        )
    return "\n\n".join(lines)

def handle_daily() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    keywords = ["每日", "必賺", "新人", "福利"]
    daily_pools = [
        p for p in latest_pools
        if bool(p.get("limit_str")) or any(k in p.get("name", "") for k in keywords)
    ]
    if not daily_pools:
        return "ℹ️ 目前全站未發現每日任務或福利限抽卡池。"
    
    lines = ["🎁 <b>每日任務 / 福利限抽卡池狀態：</b>\n"]
    for p in daily_pools:
        stock_status = f"剩餘: <b>{p['rem']} / {p['total']}</b> 張" if p['rem'] > 0 else "<b>已售罄 (0 庫存)</b>"
        lim = f" [{p['limit_str']}]" if p['limit_str'] else ""
        up_time = p.get('updated_at') or "無"
        lines.append(
            f"• <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {p['name']}</b></a>{lim}\n"
            f"   📦 狀態: {stock_status}\n"
            f"   📈 初始 ROI: <b>{p['init_roi']:.1f}%</b> | 🛡️ 保底: <b>{p['min_unit']:.1f}</b> | 💰 單抽: <b>{p['price']:.0f}</b>\n"
            f"   🕒 最後異動: <code>{up_time}</code>"
        )
    return "\n\n".join(lines)

def get_pool_detail(pid: int) -> str:
    """即時呼叫 getCardsBySort 抓取該池完整明細"""
    try:
        r = session.post(
            "https://api.luckutcg.com/api/getCardsBySort",
            headers=API_HEADERS,
            json={"page": "1", "limit": "9999", "card_sort_id": str(pid)},
            timeout=15
        )
        if r.status_code != 200:
            return f"❌ 查詢卡池 #{pid} 失敗 (HTTP {r.status_code})"
        res_json = r.json()
        info = res_json.get("results")
        if not info or not isinstance(info, dict):
            return f"❌ 找不到卡池 #{pid}，請確認卡池編號是否正確。"

        name = info.get("card_sort_name", f"Pool #{pid}")
        price = float(info.get("card_draw_price") or 0)
        rem = int(info.get("total_remaining") or 0)
        stats = info.get("tag_stats") or {}
        data = info.get("data") or {}
        order = info.get("card_tag_order") or list(stats.keys())

        m = calc_pool_metrics(pid, info, {})

        lines = [
            f"🔍 <b>卡池詳情：#{pid} {name}</b>",
            f"💰 單抽: <b>{price:.0f}</b> | 📦 總剩餘: <b>{rem} / {m['total']}</b> 張",
            f"📈 ROI: <b>{m['roi']:.1f}%</b> (Δ{m['delta_roi']:+.1f}%) | 📊 EV: <b>{m['ev']:.1f}</b> (保底: <b>{m['min_unit']:.1f}</b>)",
            "",
            "<b>📋 各賞別即時明細：</b>"
        ]

        for tag in order:
            st = stats.get(tag)
            if not st:
                continue
            u, t = int(st.get("undrawn", 0)), int(st.get("total", 0))
            prob = (u / rem * 100) if rem > 0 else 0.0

            items = data.get(tag) or []
            in_stock = [c for c in items if int(c.get("card_stock") or 0) > 0]
            if in_stock:
                total_val = sum(float(c.get("card_price") or 0) * int(c.get("card_stock") or 0) for c in in_stock)
                total_cnt = sum(int(c.get("card_stock") or 0) for c in in_stock)
                avg_price = total_val / total_cnt if total_cnt > 0 else 0.0
            elif items:
                all_ps = [float(c.get("card_price") or 0) for c in items]
                avg_price = sum(all_ps) / len(all_ps) if all_ps else 0.0
            else:
                avg_price = PC_PACK_VALUE if "pc" in tag.lower() else 0.0

            tag_line = f"• <b>{tag}</b>: 剩 <b>{u}/{t}</b> (機率: <b>{prob:.2f}%</b>, 均價: <b>${avg_price:.0f}</b>)"
            lines.append(tag_line)

            # 前排大獎若在庫，列出各卡片與剩餘數量及價格 (取前 4 張)
            tag_lower = tag.lower()
            is_filler = any(k in tag_lower for k in ["5等", "五等", "6等", "六等", "7等", "七等", "pc", "白卡", "末賞"])
            if not is_filler and in_stock:
                for c in sorted(in_stock, key=lambda x: float(x.get("card_price") or 0), reverse=True)[:4]:
                    c_name = c.get("card_name", "卡片")
                    c_price = float(c.get("card_price") or 0)
                    c_stk = int(c.get("card_stock") or 0)
                    lines.append(f"  └ <code>{c_name}</code> (${c_price:.0f} x{c_stk})")
                if len(in_stock) > 4:
                    lines.append(f"  └ ... 其餘 {len(in_stock) - 4} 款")

        lines.append(f"\n👉 <a href='https://www.luckutcg.com/#/cards/{pid}'>點擊打開網頁抽卡</a>")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 獲取卡池 #{pid} 明細時發生例外：{e}"

def handle_mute(arg: str) -> str:
    global MUTED_POOL_IDS
    try:
        pid = int(arg)
        MUTED_POOL_IDS.add(pid)
        muted_list = sorted(list(MUTED_POOL_IDS))
        return f"🔇 已將卡池 <b>#{pid}</b> 加入靜音名單。\n目前靜音卡池：<code>{muted_list}</code>"
    except Exception:
        return "⚠️ 請輸入正確的卡池 ID，例如：<code>/mute 40</code>"

def handle_unmute(arg: str) -> str:
    global MUTED_POOL_IDS
    try:
        pid = int(arg)
        MUTED_POOL_IDS.discard(pid)
        muted_list = sorted(list(MUTED_POOL_IDS))
        return f"🔊 已將卡池 <b>#{pid}</b> 移出靜音名單。\n目前靜音卡池：<code>{muted_list}</code>"
    except Exception:
        return "⚠️ 請輸入正確的卡池 ID，例如：<code>/unmute 40</code>"

def handle_roi(arg: str) -> str:
    global ALARM_ROI_THRESHOLD
    try:
        val = float(arg)
        ALARM_ROI_THRESHOLD = val
        return f"⚙️ 已將警報回本率門檻修改為：<b>{ALARM_ROI_THRESHOLD:.1f}%</b>"
    except Exception:
        return "⚠️ 請輸入有效的數字，例如：<code>/roi 95</code>"

def handle_status(start_ts: float, end_ts: float) -> str:
    remaining_sec = max(0, int(end_ts - time.time()))
    rem_min = remaining_sec // 60
    rem_sec = remaining_sec % 60
    muted_str = ", ".join(f"#{x}" for x in sorted(MUTED_POOL_IDS)) if MUTED_POOL_IDS else "無"
    pools_cnt = len(latest_pools)
    active_cnt = len([p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0])

    return (
        f"📊 <b>雲端監控運行狀態：</b>\n\n"
        f"⏱️ <b>本輪剩餘時間：</b> {rem_min} 分 {rem_sec} 秒\n"
        f"🎯 <b>高回本率警報門檻：</b> ROI >= {ALARM_ROI_THRESHOLD:.1f}%\n"
        f"⚡ <b>撿漏指數警報門檻：</b> ΔROI >= +{ALARM_DELTA_ROI:.1f}% (且 ROI >= 80%)\n"
        f"📦 <b>PC 散包預設估值：</b> ${PC_PACK_VALUE:.1f}\n"
        f"🔇 <b>靜音卡池清單：</b> {muted_str}\n"
        f"🗃️ <b>當前快取卡池：</b> 共 {pools_cnt} 池 (有貨且頭獎在: {active_cnt} 池)"
    )

def handle_help() -> str:
    return (
        "🤖 <b>Lucku Monitor 雙向互動指令說明：</b>\n\n"
        "🏆 <code>/top</code> - 全站回本率 Top 10 (有貨且頭獎尚在)\n"
        "🎯 <code>/snipe</code> - 撿漏指數 ΔROI Top 5 (大獎沉底回本飆升)\n"
        "🎁 <code>/daily</code> - 每日任務 / 福利限抽卡池狀態\n"
        "🔍 <code>/pool &lt;ID&gt;</code> (或直接傳數字如 <code>54</code>) - 查詢卡池完整庫存、各賞別機率與剩餘大獎\n"
        "🔇 <code>/mute &lt;ID&gt;</code> - 加入靜音名單 (不再觸發該池警報)\n"
        "🔊 <code>/unmute &lt;ID&gt;</code> - 移出靜音名單\n"
        "⚙️ <code>/roi &lt;數值&gt;</code> - 動態調整報警門檻 (如 <code>/roi 95</code>)\n"
        "📊 <code>/status</code> - 檢視目前監控設定、靜音名單與運行狀態\n"
        "❓ <code>/help</code> - 顯示此指令說明選單"
    )

def dispatch_tg_command(raw_text: str, start_ts: float, end_ts: float) -> str:
    text = raw_text.strip()
    if not text:
        return ""

    if text.isdigit():
        return get_pool_detail(int(text))

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@")[0]

    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/top":
        return handle_top()
    elif cmd == "/snipe":
        return handle_snipe()
    elif cmd == "/daily":
        return handle_daily()
    elif cmd == "/pool":
        if arg.isdigit():
            return get_pool_detail(int(arg))
        else:
            return "⚠️ 請提供卡池 ID，例如：<code>/pool 54</code> 或直接輸入 <code>54</code>"
    elif cmd == "/mute":
        return handle_mute(arg)
    elif cmd == "/unmute":
        return handle_unmute(arg)
    elif cmd == "/roi":
        return handle_roi(arg)
    elif cmd == "/status":
        return handle_status(start_ts, end_ts)
    elif cmd in ["/help", "/start"]:
        return handle_help()
    return ""

# ======================== 主程式 ========================

def main():
    global latest_pools, ALARM_ROI_THRESHOLD, MUTED_POOL_IDS
    start_ts = time.time()
    end_ts = start_ts + (RUN_DURATION_MINUTES * 60)
    alerted_history = {}
    known_pool_ids = set()

    # 1. 註冊 Telegram 選單指令
    setup_tg_commands()

    # 2. 啟動時先抓一次最新 update_id，忽略啟動前的歷史訊息
    last_update_id = init_tg_offset()

    # 3. 初始掃描卡池
    try:
        pools = scan_all_pools()
        latest_pools = pools
        known_pool_ids = {p["id"] for p in pools}
        active = [p for p in pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]

        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            if len(pools) == 0:
                err_detail = last_scan_error if last_scan_error else "未知原因 (回傳 0 池)"
                msg = (
                    f"⚠️ <b>GitHub 雲端監控手動啟動 - 抓取卡池為 0！</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n"
                    f"❌ <b>錯誤原因：</b>\n<code>{err_detail}</code>"
                )
            elif active:
                top3 = "\n".join([
                    f"• <b>#{p['id']} {p['name']}</b> | ROI: <b>{p['roi']:.1f}%</b> (Δ{p['delta_roi']:+.1f}%) | 剩:{p['rem']}/{p['total']}"
                    for p in active[:3]
                ])
                msg = (
                    f"✅ <b>GitHub 雲端監控已手動啟動！</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n"
                    f"🏆 <b>目前有貨 Top 3：</b>\n{top3}\n\n"
                    f"💡 <i>您可隨時在 TG 輸入 /help 呼叫互動選單！</i>"
                )
            else:
                msg = (
                    f"✅ <b>GitHub 雲端監控已手動啟動！</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n"
                    f"🏆 <b>目前共抓取 {len(pools)} 池，但尚無有貨卡池</b>\n\n"
                    f"💡 <i>您可隨時在 TG 輸入 /help 呼叫互動選單！</i>"
                )
            send_tg(msg)
    except Exception as e:
        err_msg = f"[啟動初期掃描異常] {type(e).__name__} - {e}"
        print(err_msg)
        send_tg(f"⚠️ <b>GitHub 雲端監控啟動初期掃描異常：</b>\n<code>{err_msg}</code>\n將繼續進入主監控迴圈。")

    last_scan_ts = time.time()

    # 主輪詢與指令監聽迴圈
    while time.time() < end_ts:
        try:
            # 檢查是否到達 60 秒定期全站掃描
            now = time.time()
            if now - last_scan_ts >= SCAN_INTERVAL_SEC:
                last_scan_ts = now
                now_hk = datetime.now(HK_TZ).strftime("%H:%M:%S")
                pools = scan_all_pools()
                if pools:
                    latest_pools = pools
                print(f"[{now_hk} HKT] 掃描完成，共 {len(pools)} 池")

                for p in pools:
                    pid = p["id"]
                    is_brand_new = (len(known_pool_ids) > 0 and pid not in known_pool_ids)
                    known_pool_ids.add(pid)

                    if pid in MUTED_POOL_IDS:
                        continue

                    if p["rem"] == 0 or p["first_tag_rem"] == 0:
                        alerted_history.pop(pid, None)
                        continue

                    cond_high_roi = p["roi"] >= ALARM_ROI_THRESHOLD
                    cond_snipe = (p["delta_roi"] >= ALARM_DELTA_ROI and p["roi"] >= 80.0)

                    if cond_high_roi or cond_snipe or is_brand_new:
                        last_roi = alerted_history.get(pid)
                        if last_roi is None or (p["roi"] - last_roi >= 2.0):
                            alerted_history[pid] = p["roi"]
                            title = "🆕 <b>發現全新上架卡池！</b>" if is_brand_new and not cond_high_roi else f"🔥 <b>發現高價值卡池！({p['roi']:.1f}%)</b>"
                            lim_badge = f" [{p['limit_str']}]" if p["limit_str"] else ""
                            msg = (
                                f"{title}\n\n"
                                f"🎯 <b>#{pid} {p['name']}</b>{lim_badge}\n"
                                f"💰 單抽: <b>{p['price']:.0f}</b> | 📊 期望值: <b>{p['ev']:.1f}</b> (保底: <b>{p['min_unit']:.1f}</b>)\n"
                                f"📈 回本率: <b>{p['roi']:.1f}%</b> (Δ初始 <b>{p['delta_roi']:+.1f}%</b>)\n"
                                f"📦 剩餘: <b>{p['rem']} / {p['total']}</b> 張\n"
                                f"🎁 前排: {p['top_str']}\n\n"
                                f"👉 <a href='https://www.luckutcg.com/#/cards/{pid}'>點擊直接打開該卡池</a>"
                            )
                            send_tg(msg)

            # 在兩次定期掃描的空檔，進行 Telegram getUpdates 長輪詢 (timeout=5)
            time_until_scan = max(1, int(SCAN_INTERVAL_SEC - (time.time() - last_scan_ts)))
            poll_timeout = min(5, time_until_scan)

            updates = get_tg_updates(offset=last_update_id, timeout=poll_timeout)
            for u in updates:
                u_id = u["update_id"]
                last_update_id = max(last_update_id, u_id + 1)

                msg = u.get("message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                # 安全驗證：僅處理來自授權 TG_CHAT_ID 的訊息
                if str(TG_CHAT_ID) and chat_id != str(TG_CHAT_ID):
                    continue

                text = msg.get("text", "").strip()
                if not text:
                    continue

                msg_id = msg.get("message_id")
                reply = dispatch_tg_command(text, start_ts, end_ts)
                if reply:
                    send_tg(reply, reply_to_message_id=msg_id)

        except Exception as e:
            print(f"[主迴圈異常] {type(e).__name__} - {e}")
            time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[全域異常] {e}")
