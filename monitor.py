import os
import time
import json
import html as html_lib
import subprocess
import requests
from curl_cffi import requests as cffi_requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from market_prices import (
    MarketPriceCache,
    MARKET_FILE,
    fetch_fx_rates,
    lookup_single_card_live,
    make_card_key,
    clean_card_name,
    is_voucher_card,
    is_bulk_card,
)

# 從 GitHub Secrets 自動安全讀取金鑰
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

# 雲端運行參數設定
SCAN_INTERVAL_SEC = 60          # 每 60 秒掃描一次全站
MARKET_RELOAD_INTERVAL_SEC = 900  # 每 15 分鐘從 GitHub 熱更新一次市價快取表
RUN_DURATION_MINUTES = 340      # 單次雲端任務持續運行 340 分鐘 (約 5.6 小時，無縫接力每 6 小時的排程)
STATE_FILE = "state.json"       # 跨班次持久化記憶檔案

HK_TZ = timezone(timedelta(hours=8))

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "language": "zh-Hant",
    "origin": "https://www.luckutcg.com",
    "referer": "https://www.luckutcg.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 預設狀態設定
DEFAULT_STATE = {
    "muted_pool_ids": [40],
    "alarm_roi_threshold": 95.0,
    "alarm_delta_roi": 6.0,
    "pc_pack_value": 15.0,
    "diamond_rate": 0.80,
    "alerted_history": {},
    "known_pool_ids": []
}


def esc(text) -> str:
    """安全跳脫 Telegram HTML 特殊字元 (&, <, >)"""
    return html_lib.escape(str(text or ""), quote=False)


def load_state() -> dict:
    """載入 state.json，若檔案不存在或格式毀損則使用預設值"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                state = DEFAULT_STATE.copy()
                state.update(data)
                state["muted_pool_ids"] = list(state.get("muted_pool_ids", [40]))
                state["alarm_roi_threshold"] = float(state.get("alarm_roi_threshold", 95.0))
                state["alarm_delta_roi"] = float(state.get("alarm_delta_roi", 6.0))
                state["pc_pack_value"] = float(state.get("pc_pack_value", 15.0))
                state["diamond_rate"] = float(state.get("diamond_rate", 0.80))
                state["alerted_history"] = dict(state.get("alerted_history", {}))
                state["known_pool_ids"] = list(state.get("known_pool_ids", []))
                print(
                    f"[載入記憶] 成功載入 {STATE_FILE}: 靜音 {state['muted_pool_ids']}, "
                    f"門檻 ROI {state['alarm_roi_threshold']}%, 退鑽率 {state['diamond_rate']}, "
                    f"已記錄 {len(state['known_pool_ids'])} 池, 歷史警報 {len(state['alerted_history'])} 筆"
                )
                return state
        except Exception as e:
            print(f"[載入記憶失敗，使用預設值] {e}")
    return DEFAULT_STATE.copy()


# 初始化全局參數 (從持久化記憶讀取)
_init_state = load_state()
MUTED_POOL_IDS = set(int(x) for x in _init_state["muted_pool_ids"])
ALARM_ROI_THRESHOLD = float(_init_state["alarm_roi_threshold"])
ALARM_DELTA_ROI = float(_init_state["alarm_delta_roi"])
PC_PACK_VALUE = float(_init_state["pc_pack_value"])
DIAMOND_RATE = float(_init_state["diamond_rate"])
alerted_history = {int(k): float(v) for k, v in _init_state.get("alerted_history", {}).items()}
known_pool_ids = set(int(x) for x in _init_state.get("known_pool_ids", []))

# 使用 curl_cffi 偽裝真實 Chrome TLS 指紋
session = cffi_requests.Session(impersonate="chrome120")
market_cache = MarketPriceCache(MARKET_FILE)
last_scan_error = ""
latest_pools = []


def save_state(sync_git: bool = True):
    """將當前記憶寫入 state.json，並在 GitHub Actions 環境中同步 commit & push 回 GitHub"""
    state_data = {
        "muted_pool_ids": sorted(list(MUTED_POOL_IDS)),
        "alarm_roi_threshold": round(float(ALARM_ROI_THRESHOLD), 2),
        "alarm_delta_roi": round(float(ALARM_DELTA_ROI), 2),
        "pc_pack_value": round(float(PC_PACK_VALUE), 2),
        "diamond_rate": round(float(DIAMOND_RATE), 3),
        "alerted_history": {str(k): round(float(v), 2) for k, v in alerted_history.items()},
        "known_pool_ids": sorted(list(known_pool_ids))
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[寫入本地 {STATE_FILE} 失敗] {e}")
        return

    if sync_git and os.getenv("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "add", STATE_FILE], timeout=15, check=True, capture_output=True)
            status_res = subprocess.run(["git", "diff", "--staged", "--quiet"], timeout=10)
            if status_res.returncode == 0:
                return
            commit_res = subprocess.run(
                ["git", "commit", "-m", "chore: update bot state [skip ci]"],
                timeout=15,
                capture_output=True,
                text=True
            )
            if commit_res.returncode != 0 and "nothing to commit" in commit_res.stdout:
                return
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], timeout=30, capture_output=True, text=True)
            push_res = subprocess.run(["git", "push", "origin", "main"], timeout=30, capture_output=True, text=True)
            if push_res.returncode == 0:
                print(f"[Git 記憶同步] {STATE_FILE} 已成功同步推送至 GitHub")
            else:
                print(f"[Git 記憶同步失敗] push 失敗: {push_res.stderr}")
        except subprocess.TimeoutExpired:
            print("[Git 記憶同步逾時] 略過本次同步")
        except Exception as e:
            print(f"[Git 記憶同步異常] {e}")


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
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                print(f"[TG 發送失敗] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[TG 發送異常] {e}")


def setup_tg_commands():
    """啟動時自動呼叫 Telegram setMyCommands API 註冊指令選單"""
    if not TG_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "top", "description": "全站帳面回本率 Top 10 (附實卡/套利三軌指標)"},
        {"command": "real", "description": "全站純實卡回本率 Top 10 (一眼揪出真神池)"},
        {"command": "snipe", "description": "查看撿漏指數 ΔROI Top 5"},
        {"command": "daily", "description": "查看每日任務與限抽福利池狀態"},
        {"command": "status", "description": "查看監控設定、市價覆蓋率與運行狀態"},
        {"command": "help", "description": "完整指令說明 (/pool, /diamond, /mute, /roi 等)"}
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


# ======================== 三軌價值核心引擎 ========================

def resolve_card_values(card: dict) -> tuple:
    """
    針對單張卡片計算三軌價值：
    回傳 (p_nom, p_real, p_hybrid, is_verified, source)
    - p_nom: 平台帳面標價
    - p_real: SNKRDUNK 真實港元市價（未驗價依策略暫維持原標價並標註 is_verified=False）
    - p_hybrid: 混合套利價值 = max(p_real, p_nom * DIAMOND_RATE)
    """
    p_nom = float(card.get("card_price") or 0)
    cname = clean_card_name(card.get("card_name") or "")
    mp = market_cache.lookup(card)

    if mp and mp.get("verified") and mp.get("market_hkd") is not None:
        p_real = float(mp["market_hkd"])
        is_ver = True
        src = mp.get("source") or "snkrdunk"
    elif is_bulk_card(cname, p_nom):
        p_real = p_nom
        is_ver = True
        src = "bulk"
    elif is_voucher_card(cname):
        p_real = 0.0
        is_ver = True
        src = "voucher"
    else:
        # 查無市價：維持原標價但標記為未驗價警示
        p_real = p_nom
        is_ver = False
        src = (mp.get("source") if mp else "") or "unverified"

    p_hybrid = max(p_real, p_nom * DIAMOND_RATE)
    return p_nom, p_real, p_hybrid, is_ver, src


def calc_pool_metrics(pool_id: int, info: dict, meta: dict) -> dict:
    price = float(info.get("card_draw_price") or 0)
    rem = int(info.get("total_remaining") or 0)
    stats = info.get("tag_stats") or {}
    data = info.get("data") or {}
    order = info.get("card_tag_order") or list(stats.keys())

    std_sum, init_sum = 0.0, 0.0
    real_sum, init_real_sum = 0.0, 0.0
    hyb_sum, init_hyb_sum = 0.0, 0.0
    unverified_nom_sum = 0.0
    unverified_keys = set()

    init_total, first_tag_rem = 0, 1
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

        # 初始總價值（帳面 / 實卡 / 套利）
        if items:
            vals = [resolve_card_values(c) for c in items]
            all_nom = [v[0] for v in vals]
            all_real = [v[1] for v in vals]
            all_hyb = [v[2] for v in vals]
            min_unit = min(min_unit, min(all_nom))
            if len(items) == t:
                v_init = sum(all_nom)
                v_real_init = sum(all_real)
                v_hyb_init = sum(all_hyb)
            else:
                scale = t / len(items)
                v_init = sum(all_nom) * scale
                v_real_init = sum(all_real) * scale
                v_hyb_init = sum(all_hyb) * scale
        else:
            fb = PC_PACK_VALUE if is_pc else 0.0
            v_init = t * fb
            v_real_init = t * fb
            v_hyb_init = t * fb
            min_unit = min(min_unit, fb)
        init_sum += v_init
        init_real_sum += v_real_init
        init_hyb_sum += v_hyb_init

        # 當前剩餘價值（帳面 / 實卡 / 套利 / 未驗價佔比）
        if u == 0:
            v_std = v_real = v_hyb = v_unv = 0.0
        elif items and stock_cnt == u:
            v_std = v_real = v_hyb = v_unv = 0.0
            for c in in_stock:
                stk = int(c.get("card_stock") or 0)
                p_nom, p_real, p_hyb, is_ver, _ = resolve_card_values(c)
                v_std += p_nom * stk
                v_real += p_real * stk
                v_hyb += p_hyb * stk
                if not is_ver:
                    v_unv += p_nom * stk
                    unverified_keys.add(make_card_key(c.get("card_name") or "", c.get("deal_condition") or ""))
        elif in_stock:
            vals_s = [resolve_card_values(c) for c in in_stock]
            n = len(in_stock)
            v_std = u * (sum(v[0] for v in vals_s) / n)
            v_real = u * (sum(v[1] for v in vals_s) / n)
            v_hyb = u * (sum(v[2] for v in vals_s) / n)
            v_unv = u * (sum(v[0] for v in vals_s if not v[3]) / n)
            for c, v in zip(in_stock, vals_s):
                if not v[3]:
                    unverified_keys.add(make_card_key(c.get("card_name") or "", c.get("deal_condition") or ""))
        else:
            fb = PC_PACK_VALUE if is_pc else 0.0
            v_std = v_real = v_hyb = u * fb
            v_unv = 0.0

        std_sum += v_std
        real_sum += v_real
        hyb_sum += v_hyb
        unverified_nom_sum += v_unv

    # 1. 帳面指標
    init_ev = (init_sum / init_total) if init_total > 0 else 0.0
    init_roi = (init_ev / price * 100) if price > 0 else 0.0
    ev_std = (std_sum / rem) if rem > 0 else init_ev
    roi_std = (ev_std / price * 100) if (rem > 0 and price > 0) else init_roi
    delta_roi = (roi_std - init_roi) if rem > 0 else 0.0

    # 2. 純實卡指標
    init_real_ev = (init_real_sum / init_total) if init_total > 0 else 0.0
    init_real_roi = (init_real_ev / price * 100) if price > 0 else 0.0
    real_ev = (real_sum / rem) if rem > 0 else init_real_ev
    real_roi = (real_ev / price * 100) if (rem > 0 and price > 0) else init_real_roi
    real_delta_roi = (real_roi - init_real_roi) if rem > 0 else 0.0
    inflation_ratio = (ev_std / real_ev) if real_ev > 0 else 0.0

    # 3. 混合套利指標 (實價提貨 / 灌水退鑽)
    init_hyb_ev = (init_hyb_sum / init_total) if init_total > 0 else 0.0
    init_hyb_roi = (init_hyb_ev / price * 100) if price > 0 else 0.0
    hyb_ev = (hyb_sum / rem) if rem > 0 else init_hyb_ev
    hyb_roi = (hyb_ev / price * 100) if (rem > 0 and price > 0) else init_hyb_roi
    hyb_delta_roi = (hyb_roi - init_hyb_roi) if rem > 0 else 0.0

    # 4. 未驗價警示
    unverified_cnt = len(unverified_keys)
    unverified_pct = (unverified_nom_sum / std_sum * 100) if (rem > 0 and std_sum > 0) else 0.0

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
        "real_ev": real_ev,
        "real_roi": real_roi,
        "init_real_roi": init_real_roi,
        "real_delta_roi": real_delta_roi,
        "inflation_ratio": inflation_ratio,
        "hybrid_ev": hyb_ev,
        "hybrid_roi": hyb_roi,
        "init_hybrid_roi": init_hyb_roi,
        "hybrid_delta_roi": hyb_delta_roi,
        "unverified_cnt": unverified_cnt,
        "unverified_pct": unverified_pct,
        "min_unit": 0.0 if min_unit == float("inf") else min_unit,
        "first_tag_rem": first_tag_rem,
        "limit_str": "/".join(lim_tags),
        "top_str": " | ".join(top_prizes),
        "updated_at": updated_at
    }


def format_unverified_badge(p: dict) -> str:
    if p.get("unverified_cnt", 0) > 0:
        return f" | ⚠️未驗價:{p['unverified_cnt']}款({p['unverified_pct']:.0f}%)"
    return ""


def format_inflation_tag(ratio: float) -> str:
    if ratio <= 0:
        return ""
    if ratio >= 1.35:
        return f"灌水 <b>{ratio:.2f}x</b> 🔴"
    if ratio >= 1.15:
        return f"溢價 {ratio:.2f}x 🟡"
    return f"實價 {ratio:.2f}x 🟢"


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

def format_pool_summary_block(idx: int, p: dict, sort_mode: str = "nominal") -> str:
    lim = f" [{esc(p['limit_str'])}]" if p["limit_str"] else ""
    unv = format_unverified_badge(p)
    inf_tag = format_inflation_tag(p["inflation_ratio"])
    header = f"{idx}. <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {esc(p['name'])}</b></a>{lim}"
    line_base = f"   💰 單抽: <b>{p['price']:.0f}</b> | 📦 剩: <b>{p['rem']}/{p['total']}</b>{unv}"
    line_nom = f"   📊 帳面: <b>{p['roi']:.1f}%</b> (EV {p['ev']:.1f}, Δ{p['delta_roi']:+.1f}%)"
    line_real = f"   💎 實卡: <b>{p['real_roi']:.1f}%</b> (EV {p['real_ev']:.1f}) | {inf_tag}"
    line_hyb = f"   ♻️ 套利: <b>{p['hybrid_roi']:.1f}%</b> (EV {p['hybrid_ev']:.1f}, 退鑽{DIAMOND_RATE:.2f})"
    line_top = f"   🎁 前排: {esc(p['top_str'])}"
    if sort_mode == "real":
        return "\n".join([header, line_base, line_real, line_hyb, line_nom, line_top])
    return "\n".join([header, line_base, line_nom, line_real, line_hyb, line_top])


def handle_top() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    active = [p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
    if not active:
        return "ℹ️ 目前全站暫無「有貨且頭獎尚在」的卡池。"
    top10 = active[:10]
    lines = ["🏆 <b>全站帳面回本率 Top 10 (附實卡/套利三軌指標)：</b>"]
    for idx, p in enumerate(top10, 1):
        lines.append(format_pool_summary_block(idx, p, sort_mode="nominal"))
    return "\n\n".join(lines)


def handle_real() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    active = [p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
    if not active:
        return "ℹ️ 目前全站暫無「有貨且頭獎尚在」的卡池。"
    real_top10 = sorted(active, key=lambda x: x["real_roi"], reverse=True)[:10]
    lines = ["💎 <b>全站純實卡回本率 Top 10 (剔除灌水真實排名)：</b>"]
    for idx, p in enumerate(real_top10, 1):
        lines.append(format_pool_summary_block(idx, p, sort_mode="real"))
    return "\n\n".join(lines)


def handle_snipe() -> str:
    global latest_pools
    if not latest_pools:
        return "⚠️ 目前尚無快取資料，正在背景掃描中，請稍候重試。"
    active = [p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
    if not active:
        return "ℹ️ 目前全站暫無「有貨且頭獎尚在」的卡池。"
    snipe_pools = sorted(active, key=lambda x: x["delta_roi"], reverse=True)[:5]
    lines = ["🎯 <b>撿漏指數 ΔROI Top 5 (大獎沉底回本飆升)：</b>"]
    for idx, p in enumerate(snipe_pools, 1):
        lim = f" [{esc(p['limit_str'])}]" if p["limit_str"] else ""
        unv = format_unverified_badge(p)
        inf_tag = format_inflation_tag(p["inflation_ratio"])
        lines.append(
            f"{idx}. <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {esc(p['name'])}</b></a>{lim}\n"
            f"   🎯 撿漏指數 ΔROI: <b>{p['delta_roi']:+.1f}%</b> (帳面 {p['roi']:.1f}% / 初 {p['init_roi']:.1f}%)\n"
            f"   💎 實卡: <b>{p['real_roi']:.1f}%</b> ({inf_tag}) | ♻️ 套利: <b>{p['hybrid_roi']:.1f}%</b>\n"
            f"   💰 單抽: <b>{p['price']:.0f}</b> | 📦 剩: <b>{p['rem']}/{p['total']}</b>{unv}\n"
            f"   🎁 前排: {esc(p['top_str'])}"
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

    lines = ["🎁 <b>每日任務 / 福利限抽卡池狀態：</b>"]
    for p in daily_pools:
        stock_status = f"剩餘: <b>{p['rem']} / {p['total']}</b> 張" if p["rem"] > 0 else "<b>已售罄 (0 庫存)</b>"
        lim = f" [{esc(p['limit_str'])}]" if p["limit_str"] else ""
        up_time = p.get("updated_at") or "無"
        unv = format_unverified_badge(p)
        lines.append(
            f"• <a href='https://www.luckutcg.com/#/cards/{p['id']}'><b>#{p['id']} {esc(p['name'])}</b></a>{lim}\n"
            f"   📦 狀態: {stock_status} | 💰 單抽: <b>{p['price']:.0f}</b> | 🛡️ 保底: <b>{p['min_unit']:.1f}</b>\n"
            f"   📊 初始ROI: 帳面 <b>{p['init_roi']:.1f}%</b> / 實卡 <b>{p['init_real_roi']:.1f}%</b> / 套利 <b>{p['init_hybrid_roi']:.1f}%</b>{unv}\n"
            f"   🕒 最後異動: <code>{esc(up_time)}</code>"
        )
    return "\n\n".join(lines)


def get_pool_detail(pid: int) -> str:
    """即時呼叫 getCardsBySort 抓取該池完整明細，並列帳面與 SNKRDUNK 實價"""
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
        inf_tag = format_inflation_tag(m["inflation_ratio"])
        unv_line = (
            f"⚠️ <b>未驗價警示：</b> 含 {m['unverified_cnt']} 款未驗價卡（佔剩餘帳面價值 {m['unverified_pct']:.1f}%，暫按原標價計算）"
            if m["unverified_cnt"] > 0 else "✅ <b>驗價完整度：</b> 100% 在庫卡片均已驗價"
        )

        lines = [
            f"🔍 <b>卡池詳情：#{pid} {esc(name)}</b>",
            f"💰 單抽: <b>{price:.0f}</b> | 📦 總剩餘: <b>{rem} / {m['total']}</b> 張 | 🛡️ 保底: <b>{m['min_unit']:.1f}</b>",
            f"📊 <b>帳面</b>: ROI <b>{m['roi']:.1f}%</b> (Δ{m['delta_roi']:+.1f}%) | EV <b>{m['ev']:.1f}</b>",
            f"💎 <b>實卡</b>: ROI <b>{m['real_roi']:.1f}%</b> (EV <b>{m['real_ev']:.1f}</b>) | {inf_tag}",
            f"♻️ <b>套利</b>: ROI <b>{m['hybrid_roi']:.1f}%</b> (EV <b>{m['hybrid_ev']:.1f}</b>, 退鑽率 {DIAMOND_RATE:.2f})",
            unv_line,
            "",
            "<b>📋 各賞別即時明細 (帳面 vs SNKRDUNK實價)：</b>"
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
                total_nom = sum(resolve_card_values(c)[0] * int(c.get("card_stock") or 0) for c in in_stock)
                total_real = sum(resolve_card_values(c)[1] * int(c.get("card_stock") or 0) for c in in_stock)
                total_cnt = sum(int(c.get("card_stock") or 0) for c in in_stock)
                avg_nom = total_nom / total_cnt if total_cnt > 0 else 0.0
                avg_real = total_real / total_cnt if total_cnt > 0 else 0.0
            elif items:
                vals = [resolve_card_values(c) for c in items]
                avg_nom = sum(v[0] for v in vals) / len(vals)
                avg_real = sum(v[1] for v in vals) / len(vals)
            else:
                avg_nom = avg_real = PC_PACK_VALUE if "pc" in tag.lower() else 0.0

            tag_line = f"• <b>{esc(tag)}</b>: 剩 <b>{u}/{t}</b> ({prob:.2f}%) | 帳均<b>${avg_nom:.0f}</b> / 實均<b>${avg_real:.0f}</b>"
            lines.append(tag_line)

            tag_lower = tag.lower()
            is_filler = any(k in tag_lower for k in ["5等", "五等", "6等", "六等", "7等", "七等", "pc", "白卡", "末賞"])
            if not is_filler and in_stock:
                for c in sorted(in_stock, key=lambda x: float(x.get("card_price") or 0), reverse=True)[:4]:
                    c_name = clean_card_name(c.get("card_name") or "卡片")
                    c_stk = int(c.get("card_stock") or 0)
                    p_nom, p_real, _p_hyb, is_ver, src = resolve_card_values(c)
                    if is_ver:
                        ratio = (p_nom / p_real) if p_real > 0 else 0.0
                        action = "📦提貨" if p_real >= p_nom * DIAMOND_RATE else "💎退鑽"
                        ratio_str = f" {ratio:.2f}x" if ratio > 0 else ""
                        price_info = f"帳${p_nom:.0f}→實<b>${p_real:.0f}</b>{ratio_str} {action}"
                    else:
                        price_info = f"帳${p_nom:.0f} ⚠️未驗價"
                    lines.append(f"  └ <code>{esc(c_name[:38])}</code> ({price_info} x{c_stk})")
                if len(in_stock) > 4:
                    lines.append(f"  └ ... 其餘 {len(in_stock) - 4} 款")

        lines.append(f"\n👉 <a href='https://www.luckutcg.com/#/cards/{pid}'>點擊打開網頁抽卡</a>")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 獲取卡池 #{pid} 明細時發生例外：{esc(e)}"


def handle_mute(arg: str) -> str:
    global MUTED_POOL_IDS
    try:
        pid = int(arg)
        MUTED_POOL_IDS.add(pid)
        save_state(sync_git=True)
        muted_list = sorted(list(MUTED_POOL_IDS))
        return f"🔇 已將卡池 <b>#{pid}</b> 加入靜音名單。\n目前靜音卡池：<code>{muted_list}</code>\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入正確的卡池 ID，例如：<code>/mute 40</code>"


def handle_unmute(arg: str) -> str:
    global MUTED_POOL_IDS
    try:
        pid = int(arg)
        MUTED_POOL_IDS.discard(pid)
        save_state(sync_git=True)
        muted_list = sorted(list(MUTED_POOL_IDS))
        return f"🔊 已將卡池 <b>#{pid}</b> 移出靜音名單。\n目前靜音卡池：<code>{muted_list}</code>\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入正確的卡池 ID，例如：<code>/unmute 40</code>"


def handle_roi(arg: str) -> str:
    global ALARM_ROI_THRESHOLD
    try:
        val = float(arg)
        ALARM_ROI_THRESHOLD = val
        save_state(sync_git=True)
        return f"⚙️ 已將警報回本率門檻修改為：<b>{ALARM_ROI_THRESHOLD:.1f}%</b>\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入有效的數字，例如：<code>/roi 95</code>"


def handle_delta(arg: str) -> str:
    global ALARM_DELTA_ROI
    try:
        val = float(arg)
        ALARM_DELTA_ROI = val
        save_state(sync_git=True)
        return f"⚡ 已將警報撿漏指數 (ΔROI) 門檻修改為：<b>+{ALARM_DELTA_ROI:.1f}%</b>\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入有效的數字，例如：<code>/delta 6.0</code>"


def handle_pc(arg: str) -> str:
    global PC_PACK_VALUE
    try:
        val = float(arg)
        PC_PACK_VALUE = val
        save_state(sync_git=True)
        return f"📦 已將 PC 散包預設估值修改為：<b>${PC_PACK_VALUE:.1f}</b>\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入有效的數字，例如：<code>/pc 15.0</code>"


def handle_diamond(arg: str) -> str:
    global DIAMOND_RATE, latest_pools
    try:
        val = float(arg)
        if not (0.1 <= val <= 1.0):
            return "⚠️ 退鑽折算率請介於 0.10 至 1.00 之間，例如：<code>/diamond 0.80</code>"
        DIAMOND_RATE = val
        save_state(sync_git=True)
        return f"♻️ 已將退鑽變現折算率修改為：<b>{DIAMOND_RATE:.2f}</b>（下輪掃描與詳情查詢立即生效）\n<i>(已同步至 GitHub 永久記憶)</i>"
    except Exception:
        return "⚠️ 請輸入有效的數值，例如：<code>/diamond 0.80</code>"


def handle_refresh() -> str:
    ok = market_cache.reload_from_remote()
    st = market_cache.data.get("stats") or {}
    up = market_cache.data.get("updated_at") or "未知"
    status_str = "✅ 已從 GitHub 熱更新最新市價表" if ok else "⚠️ 遠端抓取失敗，維持本地快取"
    return (
        f"{status_str}\n"
        f"💎 已驗價：<b>{st.get('verified', 0)} / {st.get('total', 0)}</b> 款（未驗價 {st.get('unverified', 0)} 款）\n"
        f"🕒 快取更新時間：<code>{esc(up)}</code>"
    )


def handle_status(start_ts: float, end_ts: float) -> str:
    remaining_sec = max(0, int(end_ts - time.time()))
    rem_min = remaining_sec // 60
    rem_sec = remaining_sec % 60
    muted_str = ", ".join(f"#{x}" for x in sorted(MUTED_POOL_IDS)) if MUTED_POOL_IDS else "無"
    pools_cnt = len(latest_pools)
    active_cnt = len([p for p in latest_pools if p["rem"] > 0 and p["first_tag_rem"] > 0])
    alerted_cnt = len(alerted_history)

    m_stats = market_cache.data.get("stats") or {}
    m_ver = m_stats.get("verified", 0)
    m_tot = m_stats.get("total", 0)
    m_upd = market_cache.data.get("updated_at") or "尚未建立"

    return (
        f"📊 <b>雲端監控運行狀態與記憶設定：</b>\n\n"
        f"⏱️ <b>本輪剩餘時間：</b> {rem_min} 分 {rem_sec} 秒\n"
        f"🎯 <b>高回本率警報門檻：</b> ROI >= {ALARM_ROI_THRESHOLD:.1f}%\n"
        f"⚡ <b>撿漏指數警報門檻：</b> ΔROI >= +{ALARM_DELTA_ROI:.1f}% (且 ROI >= 80%)\n"
        f"📦 <b>PC 散包預設估值：</b> ${PC_PACK_VALUE:.1f}\n"
        f"♻️ <b>退鑽變現折算率：</b> {DIAMOND_RATE:.2f}\n"
        f"🔇 <b>靜音卡池清單：</b> {muted_str}\n"
        f"🗃️ <b>當前快取卡池：</b> 共 {pools_cnt} 池 (有貨且頭獎在: {active_cnt} 池)\n"
        f"📝 <b>記憶已推播池：</b> {alerted_cnt} 池 (跨班次無縫記憶)\n"
        f"💎 <b>SNKRDUNK 市價表：</b> {m_ver}/{m_tot} 款已驗價\n"
        f"🕒 <b>市價表更新時間：</b> <code>{esc(m_upd)}</code>"
    )


def handle_help() -> str:
    return (
        "🤖 <b>Lucku Monitor 雙向互動指令說明：</b>\n\n"
        "🏆 <code>/top</code> - 全站帳面回本率 Top 10 (並列實卡/套利三軌指標)\n"
        "💎 <code>/real</code> - 全站純實卡回本率 Top 10 (按 SNKRDUNK 真實市價排序)\n"
        "🎯 <code>/snipe</code> - 撿漏指數 ΔROI Top 5 (大獎沉底回本飆升)\n"
        "🎁 <code>/daily</code> - 每日任務 / 福利限抽卡池狀態\n"
        "🔍 <code>/pool &lt;ID&gt;</code> (或直接傳數字如 <code>71</code>) - 查詢單池庫存、帳面 vs 實價與提貨/退鑽建議\n"
        "🔇 <code>/mute &lt;ID&gt;</code> / <code>/unmute &lt;ID&gt;</code> - 靜音設定 (永久同步)\n"
        "⚙️ <code>/roi &lt;數值&gt;</code> - 調整回本率報警門檻 (如 <code>/roi 95</code>)\n"
        "⚡ <code>/delta &lt;數值&gt;</code> - 調整撿漏指數門檻 (如 <code>/delta 6.0</code>)\n"
        "📦 <code>/pc &lt;數值&gt;</code> - 調整 PC 散包估值 (如 <code>/pc 15.0</code>)\n"
        "♻️ <code>/diamond &lt;數值&gt;</code> - 調整退鑽變現折算率 (如 <code>/diamond 0.80</code>)\n"
        "🔄 <code>/refresh</code> - 立即從 GitHub 熱更新最新市價表\n"
        "📊 <code>/status</code> - 檢視監控設定、市價覆蓋率與剩餘時間\n"
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
    elif cmd == "/real":
        return handle_real()
    elif cmd == "/snipe":
        return handle_snipe()
    elif cmd == "/daily":
        return handle_daily()
    elif cmd == "/pool":
        if arg.isdigit():
            return get_pool_detail(int(arg))
        else:
            return "⚠️ 請提供卡池 ID，例如：<code>/pool 71</code> 或直接輸入 <code>71</code>"
    elif cmd == "/mute":
        return handle_mute(arg)
    elif cmd == "/unmute":
        return handle_unmute(arg)
    elif cmd == "/roi":
        return handle_roi(arg)
    elif cmd == "/delta":
        return handle_delta(arg)
    elif cmd == "/pc":
        return handle_pc(arg)
    elif cmd == "/diamond":
        return handle_diamond(arg)
    elif cmd == "/refresh":
        return handle_refresh()
    elif cmd == "/status":
        return handle_status(start_ts, end_ts)
    elif cmd in ["/help", "/start"]:
        return handle_help()
    return ""


# ======================== 主程式 ========================

def main():
    global latest_pools, ALARM_ROI_THRESHOLD, ALARM_DELTA_ROI, PC_PACK_VALUE, DIAMOND_RATE, MUTED_POOL_IDS, alerted_history, known_pool_ids
    start_ts = time.time()
    end_ts = start_ts + (RUN_DURATION_MINUTES * 60)

    # 1. 註冊 Telegram 選單指令
    setup_tg_commands()

    # 2. 啟動時先抓一次最新 update_id，忽略啟動前的歷史舊訊息
    last_update_id = init_tg_offset()

    # 3. 嘗試從 GitHub 載入最新市價快取表
    market_cache.reload_from_remote()
    last_market_reload_ts = time.time()

    # 4. 初始掃描卡池
    try:
        pools = scan_all_pools()
        latest_pools = pools
        initial_new_pools = False
        for p in pools:
            if p["id"] not in known_pool_ids:
                known_pool_ids.add(p["id"])
                initial_new_pools = True
        if initial_new_pools:
            save_state(sync_git=True)

        active = [p for p in pools if p["rem"] > 0 and p["first_tag_rem"] > 0 and p["id"] not in MUTED_POOL_IDS]

        if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
            m_st = market_cache.data.get("stats") or {}
            if len(pools) == 0:
                err_detail = last_scan_error if last_scan_error else "未知原因 (回傳 0 池)"
                msg = (
                    f"⚠️ <b>GitHub 雲端監控手動啟動 - 抓取卡池為 0！</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n"
                    f"❌ <b>錯誤原因：</b>\n<code>{esc(err_detail)}</code>"
                )
            elif active:
                top3_real = sorted(active, key=lambda x: x["real_roi"], reverse=True)[:3]
                top3 = "\n".join([
                    f"• <b>#{p['id']} {esc(p['name'])}</b>\n"
                    f"  帳面 <b>{p['roi']:.1f}%</b> | 實卡 <b>{p['real_roi']:.1f}%</b> ({p['inflation_ratio']:.2f}x) | 套利 <b>{p['hybrid_roi']:.1f}%</b>{format_unverified_badge(p)}"
                    for p in top3_real
                ])
                msg = (
                    f"✅ <b>GitHub 雲端監控已手動啟動！(三軌真實市價版)</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n"
                    f"💎 SNKRDUNK 市價表：已驗價 <b>{m_st.get('verified', 0)}/{m_st.get('total', 0)}</b> 款\n\n"
                    f"🏆 <b>目前有貨「純實卡 ROI」Top 3：</b>\n{top3}\n\n"
                    f"💡 <i>輸入 /real 看實卡排行、/top 看帳面排行、/help 看全部指令！</i>"
                )
            else:
                msg = (
                    f"✅ <b>GitHub 雲端監控已手動啟動！</b>\n"
                    f"將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n"
                    f"🏆 <b>目前共抓取 {len(pools)} 池，但尚無有貨卡池</b>"
                )
            send_tg(msg)
    except Exception as e:
        err_msg = f"[啟動初期掃描異常] {type(e).__name__} - {e}"
        print(err_msg)
        send_tg(f"⚠️ <b>GitHub 雲端監控啟動初期掃描異常：</b>\n<code>{esc(err_msg)}</code>\n將繼續進入主監控迴圈。")

    last_scan_ts = time.time()

    # 主輪詢與指令監聽迴圈
    while time.time() < end_ts:
        try:
            now = time.time()

            # 每 15 分鐘自動熱更新一次 GitHub 上的最新市價快取表
            if now - last_market_reload_ts >= MARKET_RELOAD_INTERVAL_SEC:
                last_market_reload_ts = now
                market_cache.reload_from_remote()

            # 檢查是否到達 60 秒定期全站掃描
            if now - last_scan_ts >= SCAN_INTERVAL_SEC:
                last_scan_ts = now
                now_hk = datetime.now(HK_TZ).strftime("%H:%M:%S")
                pools = scan_all_pools()
                if pools:
                    latest_pools = pools
                print(f"[{now_hk} HKT] 掃描完成，共 {len(pools)} 池")

                state_changed = False
                for p in pools:
                    pid = p["id"]
                    is_brand_new = (len(known_pool_ids) > 0 and pid not in known_pool_ids)
                    if pid not in known_pool_ids:
                        known_pool_ids.add(pid)
                        state_changed = True

                    if pid in MUTED_POOL_IDS:
                        continue

                    if p["rem"] == 0 or p["first_tag_rem"] == 0:
                        if pid in alerted_history:
                            alerted_history.pop(pid, None)
                            state_changed = True
                        continue

                    cond_high_roi = p["roi"] >= ALARM_ROI_THRESHOLD
                    cond_snipe = (p["delta_roi"] >= ALARM_DELTA_ROI and p["roi"] >= 80.0)

                    if cond_high_roi or cond_snipe or is_brand_new:
                        last_roi = alerted_history.get(pid)
                        if last_roi is None or (p["roi"] - last_roi >= 2.0):
                            alerted_history[pid] = p["roi"]
                            state_changed = True
                            title = "🆕 <b>發現全新上架卡池！</b>" if is_brand_new and not cond_high_roi else f"🔥 <b>發現高價值卡池！(帳面 {p['roi']:.1f}% / 實卡 {p['real_roi']:.1f}%)</b>"
                            lim_badge = f" [{esc(p['limit_str'])}]" if p["limit_str"] else ""
                            inf_tag = format_inflation_tag(p["inflation_ratio"])
                            unv_line = f"\n⚠️ 未驗價: {p['unverified_cnt']} 款 (佔剩餘價值 {p['unverified_pct']:.0f}%)" if p["unverified_cnt"] > 0 else ""
                            msg = (
                                f"{title}\n\n"
                                f"🎯 <b>#{pid} {esc(p['name'])}</b>{lim_badge}\n"
                                f"💰 單抽: <b>{p['price']:.0f}</b> | 📦 剩餘: <b>{p['rem']} / {p['total']}</b> 張 (保底: <b>{p['min_unit']:.1f}</b>)\n"
                                f"📊 <b>帳面</b>: ROI <b>{p['roi']:.1f}%</b> (Δ{p['delta_roi']:+.1f}%) | EV <b>{p['ev']:.1f}</b>\n"
                                f"💎 <b>實卡</b>: ROI <b>{p['real_roi']:.1f}%</b> | EV <b>{p['real_ev']:.1f}</b> ({inf_tag})\n"
                                f"♻️ <b>套利</b>: ROI <b>{p['hybrid_roi']:.1f}%</b> | EV <b>{p['hybrid_ev']:.1f}</b> (退鑽{DIAMOND_RATE:.2f}){unv_line}\n"
                                f"🎁 前排: {esc(p['top_str'])}\n\n"
                                f"👉 <a href='https://www.luckutcg.com/#/cards/{pid}'>點擊直接打開該卡池</a>"
                            )
                            send_tg(msg)

                if state_changed:
                    save_state(sync_git=True)

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
