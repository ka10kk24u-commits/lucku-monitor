# Lucku 24H Cloud Monitor（三軌真實市價 & 雙向互動版）

24 小時雲端監控 Lucku TCG 全站卡池，整合 **SNKRDUNK 即時港幣真實市價**、**純實卡 / 混合套利雙軌期望值計算**、**Telegram 雙向指令控制**、**跨班次永久記憶 (`state.json`)** 與 **桌面篡改猴即時雷達 (`lucku_monitor.user.js`)**。

## 系統架構

| 檔案 | 功能說明 |
| :--- | :--- |
| `monitor.py` | 24H 主監控腳本：每 60 秒掃描全站計算帳面／實卡／套利三軌 ROI，搭配 Telegram Long Polling 即時回覆指令。 |
| `market_prices.py` | SNKRDUNK 國際版市價爬蟲引擎：3 層精準比對（圖床 UUID → 卡號 → 標題），自動處理多包地板價、低流動性異常與外幣轉港幣匯率。 |
| `market_prices.json` | 雲端共享真實市價快取表，同時供雲端 Telegram 機器人與桌面篡改猴讀取。 |
| `state.json` | 跨班次永久記憶檔，保存靜音名單、各項警報門檻、退鑽折算率 (`diamond_rate`) 與已推播紀錄。 |
| `lucku_monitor.user.js` | 瀏覽器 Tampermonkey 腳本，於 `luckutcg.com` 網頁右下角顯示浮動實價雷達與單池提貨／退鑽建議。 |
| `.github/workflows/monitor.yml` | 每 6 小時接力運行 340 分鐘的主監控工作流。 |
| `.github/workflows/update_prices.yml` | 每 12 小時低頻更新 `market_prices.json` 的獨立市價工作流。 |

## 三軌回本率公式

1. **帳面 ROI (`roi`)**：按平台原標價計算。
2. **純實卡 ROI (`real_roi`)**：按 SNKRDUNK 對應品級（`PSA 10` / `A` / `1 box` / 多包單價）真實港元市價計算；查無市價時暫維持原標價並於卡池標註 `⚠️ 未驗價` 佔比警示。
   - **灌水倍數 (`inflation_ratio`)** = `帳面 EV / 實卡 EV`（`≥ 1.35x 🔴灌水`、`1.15–1.35x 🟡溢價`、`< 1.15x 🟢實價`）。
3. **混合套利 ROI (`hybrid_roi`)**：每張卡自動取 `max(實卡市值, 標價 × diamond_rate)`，計算「實價卡提領實物、灌水卡一鍵退鑽轉抽」的實戰最佳期望值（`diamond_rate` 預設 `0.80`，可用 `/diamond` 隨時調整）。

## Telegram 指令一覽

- `/real`：全站 **純實卡回本率 Top 10**（直接揭穿灌水池）
- `/top`：全站帳面回本率 Top 10（並列實卡／套利三軌指標）
- `/snipe`：撿漏指數 ΔROI Top 5
- `/daily`：每日任務與限抽福利池狀態
- `/pool <ID>` 或直接輸入數字（如 `71`）：單池完整各賞別與每張在庫大獎的 **SNKRDUNK 實價 vs 提貨/退鑽建議**
- `/mute <ID>` / `/unmute <ID>`：靜音管理（永久同步）
- `/roi <數值>` / `/delta <數值>` / `/pc <數值>` / `/diamond <數值>`：動態調參（永久同步）
- `/refresh`：立即從 GitHub 熱更新最新市價表
- `/status` / `/help`：檢視運行狀態、市價覆蓋率與指令說明

## 桌面篡改猴安裝

1. 在 Chrome / Edge 安裝 [Tampermonkey](https://www.tampermonkey.net/) 擴充套件。
2. 開啟 [`lucku_monitor.user.js`](https://raw.githubusercontent.com/ka10kk24u-commits/lucku-monitor/main/lucku_monitor.user.js) 進行一鍵安裝（或複製內容建立新腳本）。
3. 打開 `https://www.luckutcg.com`，點擊右下角 `💎 實價雷達` 即可查看全站三軌排行與單池詳情。
