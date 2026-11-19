import os
import re
import sys
import time
import json
import threading
import html as html_lib
import argparse
import subprocess
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cffi_requests
import requests

HK_TZ = timezone(timedelta(hours=8))
MARKET_FILE = "market_prices.json"
RAW_MARKET_URL = "https://raw.githubusercontent.com/ka10kk24u-commits/lucku-monitor/main/market_prices.json"

LUCKU_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "language": "zh-Hant",
    "origin": "https://www.luckutcg.com",
    "referer": "https://www.luckutcg.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

DEFAULT_FX_TO_HKD = {
    "HKD": 1.0,
    "USD": 7.78,
    "JPY": 0.050,
    "EUR": 8.40,
    "GBP": 9.90,
    "SGD": 5.80,
    "AUD": 5.10,
    "CAD": 5.60,
    "TWD": 0.242,
    "CNY": 1.08,
    "KRW": 0.0056,
}

# 英文關鍵詞過濾：用來區分商品形態與語系版本
FOREIGN_EDITIONS = ["[CN]", "[IDN]", "[KR]", "[TC]", "[TH]"]
JUNK_TITLE_PATTERNS = ["campaign ended", "【campaign", "キャンペーン", "抽選", "プレゼント"]


def normalize_condition(cond: str) -> str:
    c = (cond or "").strip()
    if not c or c.lower() == "none":
        return ""
    up = re.sub(r"\s+", " ", c.upper())
    if up in ("PSA10", "PSA 10"):
        return "PSA 10"
    if up in ("PSA9", "PSA 9"):
        return "PSA 9"
    if up in ("PSA8", "PSA 8"):
        return "PSA 8"
    return up


def clean_card_name(name: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape((name or "").strip()))


def make_card_key(name: str, cond: str) -> str:
    return f"{clean_card_name(name)}||{normalize_condition(cond)}"


def extract_img_stem(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"/([^/?]+?)(?:\.[a-zA-Z0-9]+)?(?:\?|$)", url)
    return m.group(1).lower() if m else ""


def fetch_fx_rates() -> dict:
    """取得即時外幣轉港幣匯率表 (以 open.er-api.com 為主，失敗回退預設值)"""
    rates = DEFAULT_FX_TO_HKD.copy()
    try:
        r = requests.get("https://open.er-api.com/v6/latest/HKD", timeout=10)
        if r.status_code == 200:
            data = r.json()
            raw_rates = data.get("rates") or {}
            for cur in list(rates.keys()):
                if cur == "HKD":
                    rates["HKD"] = 1.0
                elif cur in raw_rates and float(raw_rates[cur]) > 0:
                    rates[cur] = round(1.0 / float(raw_rates[cur]), 6)
            print(f"[FX] 即時匯率載入成功: USD->HKD={rates['USD']}, JPY->HKD={rates['JPY']}")
    except Exception as e:
        print(f"[FX] 即時匯率載入失敗，使用預設值: {e}")
    return rates


def to_hkd(amount: float, currency: str, fx_rates: dict) -> float:
    cur = (currency or "HKD").upper()
    rate = fx_rates.get(cur) or DEFAULT_FX_TO_HKD.get(cur, 1.0)
    return round(float(amount) * float(rate), 2)


def is_voucher_card(name: str) -> bool:
    return any(k in name for k in ["兌換卷", "兌換券", "代金券", "金幣券", "金幣卷"])


def is_bulk_card(name: str, price: float) -> bool:
    return price <= 1.0 or "純白卡" in name


def is_mystery_card(name: str, img: str) -> bool:
    if any(k in name for k in ["隨機", "2份1專用", "二份一", "神秘", "福袋"]):
        return True
    if img and "snkrdunk" not in img.lower():
        return True
    return False


def classify_product_kind(name: str) -> str:
    """區分商品型態：pack / box / deck / tin / card"""
    low = name.lower()
    if re.search(r"\[[^\]]+\]", name):
        return "card"
    if any(k in low for k in ["ミニティン", "mini tin", " tin"]):
        return "tin"
    if any(k in low for k in ["デッキ", "deck"]):
        return "deck"
    if any(k in low for k in ["ボックス", "box", "carton", "カートン", "case"]):
        return "box"
    if any(k in low for k in ["パック", "pack"]):
        return "pack"
    return "other"


def extract_card_code(name: str) -> str:
    """取出方括號內卡號，例如 [SM9 102/095] 或 [M6a B/RGB]"""
    codes = re.findall(r"\[([^\]]+)\]", name)
    for c in codes:
        cu = c.strip().upper()
        if cu in ("EN", "CN", "JP", "KR", "IDN", "TC", "PROMO"):
            continue
        return c.strip()
    return ""


def norm_code(code: str) -> str:
    return re.sub(r"\s+", " ", (code or "").strip().upper())


def extract_en_title_from_desc(desc: str) -> str:
    """從 Lucku 內建的 SNKRDUNK 雙語描述取出英文標題"""
    if not desc:
        return ""
    text = html_lib.unescape(desc)
    m = re.search(r"（([^）]+)）の在庫あり", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def sanitize_query(q: str) -> str:
    """清除會導致 SNKRDUNK 搜尋回傳 0 筆的標點符號"""
    q = html_lib.unescape(q or "")
    q = re.sub(r'["「」『』（）()【】\[\]:：&＆,，]', " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    return q


def build_search_queries(name: str, desc: str) -> list:
    queries = []
    name_clean = clean_card_name(name)
    en_title = extract_en_title_from_desc(desc)
    code = extract_card_code(name_clean) or extract_card_code(en_title)
    kind = classify_product_kind(name_clean)

    # 1. 有卡號時：卡號是最精準且短的檢索鍵
    if code:
        # 完整卡號（標準數字卡號如 SM9 102/095）
        if re.search(r"\d+/\d+", code):
            queries.append(code)
            short_code = re.sub(r"/\d+", "", code).strip()
            if short_code != code:
                queries.append(short_code)
        else:
            # 特殊卡號如 M6a B/RGB、SV-P 等：把斜線換空白並搭配英文名稱
            code_space = re.sub(r"[/\\]", " ", code).strip()
            set_part = code.split()[0] if " " in code else code_space
            if en_title:
                en_head = re.split(r"[\[\(]", en_title)[0].strip()
                en_head = re.sub(r"\b(ex|GX|V|VMAX|VSTAR|SAR|SR|UR|AR|HR|CSR|CHR|RR|RRR|MUR|FUR|SA)\b", "", en_head, flags=re.I).strip()
                if en_head:
                    queries.append(f"{en_head} {set_part}".strip())
            queries.append(code_space)
            queries.append(code)

    # 2. 有英文原標題時：去掉括號與引號後搜尋（對卡盒、禮盒、單包極準）
    if en_title:
        en_no_paren = re.sub(r"\([^)]*\)", " ", en_title)
        en_q = sanitize_query(en_no_paren)
        if en_q:
            queries.append(en_q)
        # 若為 Box/Pack/Deck/Tin，取引號內主題名 + 類型（長標題同時加短詞前綴防 SNKRDUNK 過濾）
        m_en_q = re.search(r'"([^"]+)"', en_title)
        if m_en_q:
            sub = sanitize_query(m_en_q.group(1))
            sub_words = sub.split()
            if len(sub_words) > 3:
                queries.append(" ".join(sub_words[:3]))
            if len(sub_words) > 2:
                queries.append(" ".join(sub_words[:2]))
            if kind == "box":
                queries.append(f"{sub} Box")
            elif kind == "pack":
                queries.append(f"{sub} Pack")
            elif kind == "deck":
                queries.append(f"{sub} Deck")
            elif kind == "tin":
                queries.append(f"{sub} Tin")
            queries.append(sub)

    # 3. 從主標題引號「...」或 "..." 提取
    m_q = re.search(r'[「"]([^」"]+)[」"]', name_clean)
    if m_q:
        sub = sanitize_query(m_q.group(1))
        if sub:
            # 若引號內含英數前綴（例如 "30th CELEBRATION プレミアム..."），提取英數短詞
            ascii_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", sub)
            if len(ascii_words) >= 2:
                short2 = " ".join(ascii_words[:2])
                if kind == "box":
                    queries.append(f"{short2} Box")
                elif kind == "pack":
                    queries.append(f"{short2} Pack")
                queries.append(" ".join(ascii_words[:3]))
            if kind == "box":
                queries.append(f"{sub} Box")
            elif kind == "pack":
                queries.append(f"{sub} Pack")
            elif kind == "deck":
                queries.append(f"{sub} Deck")
            elif kind == "tin":
                queries.append(f"{sub} Tin")
            queries.append(sub)

    # 4. 清理過的主標題（去掉括號與前綴）
    no_paren = re.sub(r"\([^)]*\)", " ", name_clean)
    no_prefix = re.sub(r"^(Pokemon Card Game|ポケモンカードゲーム)\s*(MEGA|Scarlet & Violet|Sword & Shield)?\s*", "", no_paren, flags=re.I)
    sq = sanitize_query(no_prefix)
    if sq:
        queries.append(sq)

    # 去重保序
    dedup = []
    seen = set()
    for q in queries:
        q = q.strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            dedup.append(q)
    return dedup[:5]


def is_junk_candidate(cand_name: str, target_name: str, target_desc: str) -> bool:
    low = (cand_name or "").lower()
    if any(j in low for j in JUNK_TITLE_PATTERNS):
        return True
    target_all = f"{target_name} {target_desc}".upper()
    # 過濾掉非目標語系版本（如中文/印尼/韓文/英文版），除非原卡片本身就是該語系
    for ed in FOREIGN_EDITIONS:
        if ed in cand_name.upper() and ed not in target_all:
            return True
    target_is_en = ("英語版" in target_name) or ("[EN]" in target_all) or ("(EN)" in target_all)
    cand_is_en = ("[EN]" in cand_name.upper()) or ("ENGLISH" in low)
    if cand_is_en and not target_is_en:
        return True
    if target_is_en and not cand_is_en:
        return True
    return False


def match_candidate(cands: list, name: str, desc: str, img_stem: str) -> tuple:
    """
    3 層精準比對：
    Tier 1: 圖床檔名 UUID 完全一致
    Tier 2: 卡號 [SET NUM/DEN] 完全一致（且排除活動/異語系）
    Tier 3: 正規化英文標題 / 商品型態一致
    回傳 (candidate_dict, match_via) 或 (None, "")
    """
    name_clean = clean_card_name(name)
    en_title = extract_en_title_from_desc(desc)
    target_code = norm_code(extract_card_code(name_clean) or extract_card_code(en_title))
    target_kind = classify_product_kind(name_clean)

    # 過濾雜訊候選
    valid = [c for c in cands if not is_junk_candidate(c.get("name", ""), name_clean, desc)]

    # Tier 1: UUID 精準比對 (最高優先，不受語系/標題細微變化影響)
    if img_stem:
        for c in valid:
            if extract_img_stem(c.get("thumbnailUrl") or "") == img_stem:
                return c, "uuid"

    # Tier 2: 卡號精準比對
    if target_code:
        for c in valid:
            cname = clean_card_name(c.get("name", ""))
            cand_code = norm_code(extract_card_code(cname))
            if cand_code and cand_code == target_code:
                # 避免如 SV2a 025/165 的普通版 vs Master Ball Mirror 混配：若有鏡面字樣檢查一致
                t_master = ("マスターボール" in name_clean) or ("master ball" in (name_clean + " " + en_title).lower())
                c_master = "master ball" in cname.lower()
                t_poke = ("モンスターボール" in name_clean) or ("poke ball" in (name_clean + " " + en_title).lower())
                c_poke = "poke ball" in cname.lower()
                if t_master != c_master or t_poke != c_poke:
                    continue
                return c, "code"

    # Tier 3: 正規化標題比對（對無卡號原膜卡盒、禮盒、單包、構築牌組）
    target_norm_set = set()
    if en_title:
        target_norm_set.add(sanitize_query(en_title).lower())
    target_norm_set.add(sanitize_query(name_clean).lower())

    for c in valid:
        cname = clean_card_name(c.get("name", ""))
        cand_kind = classify_product_kind(cname)
        # 型態防呆：Box 不能配到 Pack，Card 不能配到 Box
        if target_kind in ("box", "pack", "deck", "tin") and cand_kind != target_kind:
            continue
        cnorm = sanitize_query(cname).lower()
        if cnorm in target_norm_set:
            return c, "title_exact"

    # Tier 3b: 高重疊詞彙比對（針對卡盒/牌組/單包等無卡號商品，需所有核心字皆命中且型態吻合）
    if not target_code and target_kind in ("box", "pack", "deck", "tin"):
        ref_str = en_title or name_clean
        # 取出關鍵識別詞（去除通用字）
        stop_words = {"pokemon", "card", "game", "mega", "expansion", "enhanced", "high", "class", "pack", "box", "constructed", "deck", "set", "mini", "tin", "scarlet", "violet", "sword", "shield", "the"}
        tokens = [w for w in sanitize_query(ref_str).lower().split() if len(w) >= 2 and w not in stop_words]
        if tokens:
            best_c = None
            best_score = 0.0
            for c in valid:
                cname = clean_card_name(c.get("name", ""))
                if classify_product_kind(cname) != target_kind:
                    continue
                cnorm = sanitize_query(cname).lower()
                hits = sum(1 for tok in tokens if tok in cnorm)
                score = hits / len(tokens)
                if score >= 0.8 and score > best_score:
                    best_score = score
                    best_c = c
            if best_c:
                return best_c, "title_fuzzy"

    return None, ""


def extract_initial_min_prices(page_html: str) -> dict:
    key = '"initialMinPrices":'
    pos = page_html.find(key)
    if pos == -1:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(page_html, pos + len(key))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def parse_snkr_price(initial_prices: dict, norm_cond: str, product_kind: str, fx_rates: dict) -> dict:
    """
    從 SNKRDUNK 詳情頁 initialMinPrices 解析最準確的單件港幣市價：
    - 單卡 (type=Conditions)：按 deal_condition 對應 psa_10 / psa_9 / like_new (A) 等。
    - 單包 (product_kind=pack)：排除 ¥1,000 (~HK$50) 最低下單門檻，計算多包組合最低單包價。
    - 卡盒/牌組/鐵盒 (type=Sizes)：取 1 box / 單件最低價。
    """
    if not initial_prices:
        return {}

    ptype = initial_prices.get("type") or ""
    entries = initial_prices.get("entries") or []
    top_min = initial_prices.get("minPrice") or {}
    top_cnt = str(initial_prices.get("listingCount") or "0")

    def make_result(val, cur, cond_label, cnt):
        if val is None or float(val) <= 0:
            return {}
        return {
            "market_hkd": to_hkd(float(val), cur, fx_rates),
            "raw_currency": (cur or "HKD").upper(),
            "raw_value": float(val),
            "snkr_cond": cond_label,
            "listing_count": str(cnt),
        }

    # Case 1: Single Card Conditions
    if ptype == "Conditions" or product_kind == "card":
        cond_map = {
            "PSA 10": ["psa_10"],
            "PSA 9": ["psa_9"],
            "PSA 8": ["psa_8_below"],
            "BGS 10": ["bgs_10_gold", "bgs_10_black"],
            "BGS 9.5": ["bgs_9_5"],
            "ARS 10": ["ars_10", "ars_10_plus"],
            "A": ["like_new"],
            "S": ["like_new"],
            "B": ["minor_scratches"],
            "C": ["moderate_scratches"],
            "": ["like_new"],
        }
        target_ids = cond_map.get(norm_cond, ["like_new"])
        for tid in target_ids:
            for e in entries:
                if e.get("id") == tid:
                    mp = e.get("minPrice") or {}
                    val = mp.get("value")
                    if val is not None and float(val) > 0:
                        return make_result(val, mp.get("currencyCode"), e.get("name") or tid, top_cnt)
        # 若為非鑑定卡且未指定條件，可用 top_min
        if not norm_cond and top_min.get("value"):
            return make_result(top_min["value"], top_min.get("currencyCode"), "min", top_cnt)
        return {}

    # Case 2: Booster Pack (Sizes) — 處理 ¥1,000 地板價，優先計算多包組合單包價
    if product_kind == "pack" and ptype == "Sizes":
        unit_candidates = []
        for e in entries:
            ename = (e.get("name") or "").lower()
            mp = e.get("minPrice") or {}
            val = mp.get("value")
            if val is None or float(val) <= 0:
                continue
            m_qty = re.search(r"(\d+)\s*pack", ename)
            qty = int(m_qty.group(1)) if m_qty else 1
            hkd_unit = to_hkd(float(val), mp.get("currencyCode"), fx_rates) / max(1, qty)
            unit_candidates.append((qty, hkd_unit, float(val), mp.get("currencyCode"), ename, e.get("listingCount")))

        if unit_candidates:
            # 優先挑選 qty >= 3 中單包最便宜者（避開 1~2 包的 ¥1,000 地板價）
            multi = [x for x in unit_candidates if x[0] >= 3]
            pool = multi if multi else unit_candidates
            best = min(pool, key=lambda x: x[1])
            qty, hkd_unit, raw_v, raw_c, ename, cnt = best
            return {
                "market_hkd": round(hkd_unit, 2),
                "raw_currency": (raw_c or "HKD").upper(),
                "raw_value": round(raw_v / qty, 2),
                "snkr_cond": f"1 pack (from {ename})",
                "listing_count": str(cnt or top_cnt),
            }

    # Case 3: Sealed Box / Deck / Tin (Sizes)
    if ptype == "Sizes" and entries:
        # 優先尋找 "1 box" / "1個" / "1 pack" 等單件規格
        for e in entries:
            ename = (e.get("name") or "").lower()
            if re.match(r"^1\s*(box|個|pack|tin|set|deck)?$", ename):
                mp = e.get("minPrice") or {}
                if mp.get("value"):
                    return make_result(mp["value"], mp.get("currencyCode"), e.get("name"), e.get("listingCount") or top_cnt)
        # 否則取第一個有價格的 entry
        for e in entries:
            mp = e.get("minPrice") or {}
            if mp.get("value"):
                return make_result(mp["value"], mp.get("currencyCode"), e.get("name") or "1 unit", e.get("listingCount") or top_cnt)

    if top_min.get("value"):
        return make_result(top_min["value"], top_min.get("currencyCode"), "min", top_cnt)

    return {}


class MarketPriceCache:
    """管理 market_prices.json 的載入、查詢、增量補價與持久化"""

    def __init__(self, filepath: str = MARKET_FILE):
        self.filepath = filepath
        self.data = {
            "schema_version": 1,
            "updated_at": "",
            "fx_rates": DEFAULT_FX_TO_HKD.copy(),
            "stats": {"total": 0, "verified": 0, "unverified": 0},
            "items": {},
            "img_index": {},
        }
        self.load_local()

    def load_local(self) -> bool:
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    obj = json.load(f)
                if isinstance(obj, dict) and isinstance(obj.get("items"), dict):
                    self.data = obj
                    return True
            except Exception as e:
                print(f"[市價快取] 讀取本地 {self.filepath} 失敗: {e}")
        return False

    def reload_from_remote(self) -> bool:
        """從 GitHub raw 下載最新 market_prices.json（供長跑中的 monitor.py 熱更新）"""
        try:
            r = requests.get(RAW_MARKET_URL, timeout=12)
            if r.status_code == 200:
                obj = r.json()
                if isinstance(obj, dict) and isinstance(obj.get("items"), dict):
                    self.data = obj
                    try:
                        with open(self.filepath, "w", encoding="utf-8") as f:
                            json.dump(obj, f, indent=2, ensure_ascii=False)
                    except Exception:
                        pass
                    return True
        except Exception as e:
            print(f"[市價快取] 遠端熱更新失敗: {e}")
        return False

    def save_local(self):
        items = self.data.get("items") or {}
        verified_cnt = sum(1 for v in items.values() if v.get("verified"))
        self.data["updated_at"] = datetime.now(HK_TZ).isoformat(timespec="seconds")
        self.data["stats"] = {
            "total": len(items),
            "verified": verified_cnt,
            "unverified": len(items) - verified_cnt,
        }
        # 重建 img_index 供桌面篡改猴快速索引
        img_idx = {}
        for k, v in items.items():
            stem = v.get("img_stem") or ""
            cond = v.get("cond") or ""
            if stem:
                img_idx[f"{stem}||{cond}"] = k
        self.data["img_index"] = img_idx

        with open(self.filepath, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def lookup(self, card: dict) -> dict:
        """依卡片字典查詢快取市價，支援 card_key 與 img_stem 雙重索引"""
        name = clean_card_name(card.get("card_name") or "")
        cond = normalize_condition(card.get("deal_condition") or "")
        items = self.data.get("items") or {}

        key = f"{name}||{cond}"
        if key in items:
            return items[key]

        stem = extract_img_stem(card.get("card_img") or "")
        if stem:
            img_idx = self.data.get("img_index") or {}
            mapped = img_idx.get(f"{stem}||{cond}")
            if mapped and mapped in items:
                return items[mapped]

        # 寬鬆名稱退回（不限條件）
        alt_key = f"{name}||"
        if alt_key in items:
            return items[alt_key]
        return {}


def lookup_single_card_live(session, card: dict, fx_rates: dict, existing: dict = None) -> dict:
    """針對單張卡片從 SNKRDUNK 查詢即時港幣市價"""
    name = clean_card_name(card.get("card_name") or "")
    cond = normalize_condition(card.get("deal_condition") or "")
    desc = html_lib.unescape(card.get("card_describe") or "")
    img = (card.get("card_img") or "").strip()
    img_stem = extract_img_stem(img)
    lucku_price = float(card.get("card_price") or 0)
    now_str = datetime.now(HK_TZ).isoformat(timespec="seconds")

    base = {
        "name": name,
        "cond": cond,
        "img_stem": img_stem,
        "lucku_price": round(lucku_price, 2),
        "market_hkd": None,
        "source": "unverified",
        "snkr_id": existing.get("snkr_id") if existing else None,
        "snkr_name": existing.get("snkr_name", "") if existing else "",
        "snkr_cond": "",
        "match_via": existing.get("match_via", "") if existing else "",
        "raw_currency": "",
        "raw_value": None,
        "listing_count": "",
        "verified": False,
        "low_liq": False,
        "updated_at": now_str,
    }

    # 1. 純白卡 / 極低價填充物
    if is_bulk_card(name, lucku_price):
        base.update({"market_hkd": round(lucku_price, 2), "source": "bulk", "verified": True})
        return base

    # 2. 平台金幣兌換券（實物市值 0，套利價值由退鑽/面額決定）
    if is_voucher_card(name):
        base.update({"market_hkd": 0.0, "source": "voucher", "verified": True})
        return base

    # 3. 隨機神秘獎項 / 非 SNKRDUNK 佔位符 -> 標註未驗價
    if is_mystery_card(name, img):
        base.update({"source": "mystery", "verified": False})
        return base

    product_kind = classify_product_kind(name)
    snkr_id = base["snkr_id"]
    match_via = base["match_via"]
    snkr_name = base["snkr_name"]

    # 若尚未綁定 snkr_id，透過 /en/v1/search 進行 3 層精準配對
    if not snkr_id:
        queries = build_search_queries(name, desc)
        for q in queries:
            try:
                url = f"https://snkrdunk.com/en/v1/search?keyword={urllib.parse.quote(q)}&perPage=20&page=1"
                r = session.get(url, timeout=12)
                if r.status_code == 429 or r.status_code == 403:
                    base["source"] = f"snkrdunk_http_{r.status_code}"
                    return base
                if r.status_code != 200:
                    continue
                d = r.json()
                cands = (d.get("streetwears") or []) + (d.get("sneakers") or [])
                hit, via = match_candidate(cands, name, desc, img_stem)
                if hit:
                    snkr_id = int(hit["id"])
                    snkr_name = hit.get("name") or ""
                    match_via = via
                    break
            except Exception:
                continue
            time.sleep(0.35)

    if not snkr_id:
        return base

    base["snkr_id"] = snkr_id
    base["snkr_name"] = snkr_name
    base["match_via"] = match_via

    # 取得 SNKRDUNK 詳情頁 JSON（含各鑑定等級與規格精準報價）
    try:
        r_page = session.get(f"https://snkrdunk.com/en/trading-cards/{snkr_id}", timeout=15)
        if r_page.status_code in (403, 429):
            base["source"] = f"snkrdunk_http_{r_page.status_code}"
            return base
        if r_page.status_code == 200:
            init_prices = extract_initial_min_prices(r_page.text)
            parsed = parse_snkr_price(init_prices, cond, product_kind, fx_rates)
            if parsed and parsed.get("market_hkd") is not None:
                mk = parsed["market_hkd"]
                cnt_str = str(parsed.get("listing_count") or "")
                # 低流動性異常防呆：僅 1 個掛單且開價超過 Lucku 標價 2.2 倍，查驗日本成交史避免惡搞掛單灌水
                if cnt_str == "1" and lucku_price > 0 and mk > lucku_price * 2.2:
                    has_sales = False
                    try:
                        r_hist = session.get(f"https://snkrdunk.com/v1/apparels/{snkr_id}/sales-history", timeout=10)
                        if r_hist.status_code == 200 and (r_hist.json().get("history") or []):
                            has_sales = True
                    except Exception:
                        pass
                    if not has_sales:
                        base.update(parsed)
                        base["source"] = "snkrdunk_low_liq"
                        base["low_liq"] = True
                        base["verified"] = False
                        return base

                base.update(parsed)
                base["source"] = "snkrdunk"
                base["verified"] = True
                return base
    except Exception as e:
        base["source"] = f"snkrdunk_err:{type(e).__name__}"

    return base


def collect_all_lucku_cards(session) -> dict:
    """從 Lucku 全站掃描所有不重複卡片 (key -> card_dict)"""
    r = session.post(
        "https://api.luckutcg.com/api/getCardSortByBrand",
        headers=LUCKU_HEADERS,
        json={"page": "1", "limit": "100", "brand": ""},
        timeout=15,
    )
    pools = r.json().get("results", {}).get("data", [])
    catalog = {}

    def fetch_pool(p):
        pid = p["card_sort_id"]
        try:
            r2 = session.post(
                "https://api.luckutcg.com/api/getCardsBySort",
                headers=LUCKU_HEADERS,
                json={"page": "1", "limit": "9999", "card_sort_id": str(pid)},
                timeout=15,
            )
            return r2.json().get("results") or {}
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for info in ex.map(fetch_pool, pools):
            if not isinstance(info, dict):
                continue
            for _tag, items in (info.get("data") or {}).items():
                for c in (items or []):
                    key = make_card_key(c.get("card_name") or "", c.get("deal_condition") or "")
                    if key not in catalog:
                        catalog[key] = c
    return catalog


def is_stale(entry: dict, max_age_hours: float) -> bool:
    if not entry:
        return True
    ts = entry.get("updated_at")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(ts)
        age_hours = (datetime.now(HK_TZ) - dt).total_seconds() / 3600.0
        # 未驗價卡片 6 小時重試一次；已驗價卡片依 max_age_hours 刷新
        limit = min(6.0, max_age_hours) if not entry.get("verified") else max_age_hours
        return age_hours >= limit
    except Exception:
        return True


def sync_market_file_to_git():
    try:
        subprocess.run(["git", "add", MARKET_FILE], timeout=15, check=True, capture_output=True)
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"], timeout=10)
        if diff_res.returncode == 0:
            print(f"[Git] {MARKET_FILE} 無實質變更，略過推送")
            return
        subprocess.run(
            ["git", "commit", "-m", "chore: update market prices cache [skip ci]"],
            timeout=15,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], timeout=30, capture_output=True)
        push_res = subprocess.run(["git", "push", "origin", "main"], timeout=30, capture_output=True, text=True)
        if push_res.returncode == 0:
            print(f"[Git] {MARKET_FILE} 已成功推送至 GitHub")
        else:
            print(f"[Git] push 失敗: {push_res.stderr}")
    except Exception as e:
        print(f"[Git] 同步 {MARKET_FILE} 異常: {e}")


def run_price_update(
    max_age_hours: float = 20.0,
    force: bool = False,
    retry_unverified: bool = False,
    limit: int = 0,
    workers: int = 1,
    delay: float = 0.75,
    push: bool = False,
):
    session = cffi_requests.Session(impersonate="chrome120")
    fx_rates = fetch_fx_rates()
    cache = MarketPriceCache(MARKET_FILE)
    cache.data["fx_rates"] = fx_rates

    # 先快速探測 SNKRDUNK 連通性與回傳幣別
    try:
        probe = session.get("https://snkrdunk.com/en/v1/search?keyword=SV8a+217&perPage=1&page=1", timeout=12)
        print(f"[SNKRDUNK 連通探測] HTTP {probe.status_code} | {probe.text[:160]}")
        if probe.status_code in (403, 429):
            print("[SNKRDUNK] 目前 IP 被限制 (403/429)，保留既有快取並中止更新。")
            return
    except Exception as e:
        print(f"[SNKRDUNK 連通探測異常] {e}")
        return

    print("[掃描] 正在從 Lucku 抓取全站卡片目錄...")
    catalog = collect_all_lucku_cards(session)
    print(f"[掃描] 全站共 {len(catalog)} 款不重複項目")

    items = cache.data.setdefault("items", {})
    to_fetch = []
    for key, card in catalog.items():
        existing = items.get(key)
        if force or is_stale(existing, max_age_hours) or (retry_unverified and existing and not existing.get("verified")):
            to_fetch.append((key, card, existing))

    if limit > 0:
        to_fetch = to_fetch[:limit]

    print(f"[更新] 本輪需查價 {len(to_fetch)} 款（{workers} 執行緒 / 間隔 {delay}s，其餘沿用快取）")
    lock = threading.Lock()
    tls = threading.local()
    stop_event = threading.Event()
    blocked_streak = [0]
    done = [0]

    def worker_fn(task):
        key, card, existing = task
        if stop_event.is_set():
            return
        if not hasattr(tls, "session"):
            tls.session = cffi_requests.Session(impersonate="chrome120")

        # 遇到 429 最多指數退避重試 2 次 (25s -> 50s)
        res = None
        for attempt in range(3):
            res = lookup_single_card_live(tls.session, card, fx_rates, existing=existing)
            src = res.get("source") or ""
            if src == "snkrdunk_http_429" and attempt < 2 and not stop_event.is_set():
                wait_s = 25 * (attempt + 1)
                print(f"[429 限速退避] 暫停 {wait_s} 秒後重試 ({key[:32]})...")
                time.sleep(wait_s)
                continue
            break

        src = (res or {}).get("source") or ""
        with lock:
            if src.startswith("snkrdunk_http_403") or src.startswith("snkrdunk_http_429"):
                blocked_streak[0] += 1
                print(f"[風控警示] 連續遇阻 {blocked_streak[0]} 次 ({src})")
                if blocked_streak[0] >= 3:
                    print("[風控中止] 連續被阻擋 3 次，保存已完成進度並停止本輪查價。")
                    stop_event.set()
                return
            blocked_streak[0] = 0
            if not res.get("verified") and existing and existing.get("verified"):
                existing["lucku_price"] = res["lucku_price"]
                items[key] = existing
            else:
                items[key] = res
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(to_fetch):
                v_cnt = sum(1 for v in items.values() if v.get("verified"))
                print(f"  進度 {done[0]}/{len(to_fetch)} | 累計已驗價 {v_cnt}/{len(items)}")
                cache.save_local()
        time.sleep(delay)

    if workers <= 1:
        for task in to_fetch:
            worker_fn(task)
            if stop_event.is_set():
                break
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(worker_fn, to_fetch))

    cache.save_local()
    st = cache.data["stats"]
    print(f"[完成] 總計 {st['total']} 款 | 已驗價 {st['verified']} 款 | 未驗價 {st['unverified']} 款")

    unv = [v for v in items.values() if not v.get("verified")]
    if unv:
        print(f"[未驗價清單 ({len(unv)} 款)]")
        for v in unv[:25]:
            print(f"  - ${v.get('lucku_price')} [{v.get('cond')}] ({v.get('source')}) {v.get('name')}")

    if push or os.getenv("GITHUB_ACTIONS") == "true":
        sync_market_file_to_git()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Lucku SNKRDUNK Market Price Updater")
    parser.add_argument("--max-age", type=float, default=20.0, help="快取過期小時數 (預設 20h)")
    parser.add_argument("--force", action="store_true", help="強制重抓所有項目")
    parser.add_argument("--retry-unverified", action="store_true", help="重新嘗試未驗價的項目")
    parser.add_argument("--limit", type=int, default=0, help="限制單輪最多查價筆數 (0=不限)")
    parser.add_argument("--workers", type=int, default=1, help="並發數 (預設 1 避免觸發 429)")
    parser.add_argument("--delay", type=float, default=0.75, help="每次查價間隔秒數 (預設 0.75s)")
    parser.add_argument("--push", action="store_true", help="完成後自動 commit & push 到 GitHub")
    args = parser.parse_args()
    run_price_update(
        max_age_hours=args.max_age,
        force=args.force,
        retry_unverified=args.retry_unverified,
        limit=args.limit,
        workers=args.workers,
        delay=args.delay,
        push=args.push,
    )
