import os
import time
import requests
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
MUTED_POOL_IDS = {40}           # 預設忽略 #40 新人限2次池 (若想監控可刪掉 40)

HK_TZ = timezone(timedelta(hours=8))

API_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "language": "zh-Hant",
    "origin": "https://www.luckutcg.com",
    "referer": "https://www.luckutcg.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
}

def send_tg(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[⚠️ 未設定 TG_BOT_TOKEN 或 TG_CHAT_ID]\n" + text)
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
    except Exception as e:
        print(f"[TG 發送異常] {e}")

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
        "top_str": " | ".join(top_prizes)
    }

def fetch_single_pool(p_meta: dict):
    pid = int(p_meta["card_sort_id"])
    r = requests.post(
        "https://api.luckutcg.com/api/getCardsBySort",
        headers=API_HEADERS,
        json={"page": "1", "limit": "9999", "card_sort_id": str(pid)},
        timeout=10
    )
    res_json = r.json()
    if res_json.get("results"):
        return calc_pool_metrics(pid, res_json["results"], p_meta)
    return None

def scan_all_pools() -> list:
    r = requests.post(
        "https://api.luckutcg.com/api/getCardSortByBrand",
        headers=API_HEADERS,
        json={"page": "1", "limit": "100", "brand": ""},
        timeout=10
    )
    pools_meta = r.json().get("results", {}).get("data", [])
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(fetch_single_pool, p) for p in pools_meta]
        for f in as_completed(futures):
            try:
                data = f.result()
                if data:
                    results.append(data)
            except Exception:
                pass
    results.sort(key=lambda x: x["roi"], reverse=True)
    return results

def main():
    start_ts = time.time()
    end_ts = start_ts + (RUN_DURATION_MINUTES * 60)
    alerted_history = {}
    known_pool_ids = set()

    # 若為手動點擊觸發 (workflow_dispatch)，發送一則啟動測試與 Top 3 排行榜
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        pools = scan_all_pools()
        known_pool_ids = {p["id"] for p in pools}
        active = [p for p in pools if p["rem"] > 0 and p["id"] not in MUTED_POOL_IDS]
        top3 = "\n".join([
            f"• <b>#{p['id']} {p['name']}</b> | ROI: <b>{p['roi']:.1f}%</b> (Δ{p['delta_roi']:+.1f}%) | 剩:{p['rem']}/{p['total']}"
            for p in active[:3]
        ])
        send_tg(f"✅ <b>GitHub 雲端監控已手動啟動！</b>\n將在雲端連續監控 {RUN_DURATION_MINUTES} 分鐘。\n\n🏆 <b>目前有貨 Top 3：</b>\n{top3}")

    while time.time() < end_ts:
        try:
            now_hk = datetime.now(HK_TZ).strftime("%H:%M:%S")
            pools = scan_all_pools()
            print(f"[{now_hk} HKT] 掃描完成，共 {len(pools)} 池")

            for p in pools:
                pid = p["id"]
                # 偵測是否為剛上架的全新卡池
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
        except Exception as e:
            print(f"[掃描異常] {e}")

        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
