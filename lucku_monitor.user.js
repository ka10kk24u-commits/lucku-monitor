// ==UserScript==
// @name         Lucku TCG 真實市價 & 雙軌套利雷達
// @namespace    https://github.com/ka10kk24u-commits/lucku-monitor
// @version      1.0.0
// @description  自動同步 GitHub SNKRDUNK 港幣實價快取表，在 Lucku TCG 網頁即時計算帳面 ROI、純實卡 ROI、混合套利 ROI（提貨 vs 退鑽）與灌水倍數
// @match        https://www.luckutcg.com/*
// @grant        GM_xmlhttpRequest
// @connect      raw.githubusercontent.com
// @connect      api.luckutcg.com
// ==/UserScript==

(function () {
  'use strict';

  const RAW_BASE = 'https://raw.githubusercontent.com/ka10kk24u-commits/lucku-monitor/main';
  const MARKET_URL = `${RAW_BASE}/market_prices.json`;
  const STATE_URL = `${RAW_BASE}/state.json`;
  const API_BASE = 'https://api.luckutcg.com/api';

  const state = {
    market: { items: {}, img_index: {}, stats: { total: 0, verified: 0, unverified: 0 }, updated_at: '' },
    diamondRate: 0.80,
    pcPackValue: 15.0,
    mutedIds: new Set([40]),
    pools: [],
    currentPoolId: null,
    currentPoolInfo: null,
    tab: 'rank', // 'rank' | 'detail'
    sortBy: 'real_roi', // 'real_roi' | 'hybrid_roi' | 'roi' | 'delta_roi' | 'inflation_ratio'
    collapsed: true,
    loading: false,
  };

  function gmFetchJson(url, method = 'GET', body = null, headers = {}) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url,
        headers,
        data: body ? JSON.stringify(body) : undefined,
        timeout: 15000,
        onload: (res) => {
          try {
            if (res.status >= 200 && res.status < 300) {
              resolve(JSON.parse(res.responseText));
            } else {
              reject(new Error(`HTTP ${res.status}`));
            }
          } catch (e) {
            reject(e);
          }
        },
        onerror: (e) => reject(e),
        ontimeout: () => reject(new Error('Timeout')),
      });
    });
  }

  function decodeHtml(str) {
    if (!str) return '';
    const txt = document.createElement('textarea');
    txt.innerHTML = String(str);
    return txt.value.replace(/\s+/g, ' ').trim();
  }

  function escHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function normCond(cond) {
    const c = String(cond || '').trim();
    if (!c || c.toLowerCase() === 'none') return '';
    const up = c.toUpperCase().replace(/\s+/g, ' ');
    if (up === 'PSA10' || up === 'PSA 10') return 'PSA 10';
    if (up === 'PSA9' || up === 'PSA 9') return 'PSA 9';
    if (up === 'PSA8' || up === 'PSA 8') return 'PSA 8';
    return up;
  }

  function extractImgStem(url) {
    if (!url) return '';
    const m = String(url).match(/\/([^/?]+?)(?:\.[a-zA-Z0-9]+)?(?:\?|$)/);
    return m ? m[1].toLowerCase() : '';
  }

  function isVoucher(name) {
    return ['兌換卷', '兌換券', '代金券', '金幣券', '金幣卷'].some((k) => name.includes(k));
  }

  function isBulk(name, price) {
    return price <= 1.0 || name.includes('純白卡');
  }

  function lookupMarket(card) {
    const items = state.market.items || {};
    const imgIdx = state.market.img_index || {};
    const name = decodeHtml(card.card_name || '');
    const cond = normCond(card.deal_condition || '');

    const key = `${name}||${cond}`;
    if (items[key]) return items[key];

    const stem = extractImgStem(card.card_img || '');
    if (stem) {
      const mapped = imgIdx[`${stem}||${cond}`];
      if (mapped && items[mapped]) return items[mapped];
    }
    const altKey = `${name}||`;
    if (items[altKey]) return items[altKey];
    return null;
  }

  function resolveCardValues(card) {
    const pNom = parseFloat(card.card_price || 0) || 0;
    const name = decodeHtml(card.card_name || '');
    const mp = lookupMarket(card);

    let pReal = pNom;
    let verified = false;
    let snkrId = null;

    if (mp && mp.verified && mp.market_hkd !== null && mp.market_hkd !== undefined) {
      pReal = parseFloat(mp.market_hkd);
      verified = true;
      snkrId = mp.snkr_id || null;
    } else if (isBulk(name, pNom)) {
      pReal = pNom;
      verified = true;
    } else if (isVoucher(name)) {
      pReal = 0.0;
      verified = true;
    } else {
      pReal = pNom;
      verified = false;
      snkrId = mp ? (mp.snkr_id || null) : null;
    }

    const pHybrid = Math.max(pReal, pNom * state.diamondRate);
    return { pNom, pReal, pHybrid, verified, snkrId };
  }

  function calcPoolMetrics(poolId, info) {
    const price = parseFloat(info.card_draw_price || 0) || 0;
    const rem = parseInt(info.total_remaining || 0, 10) || 0;
    const stats = info.tag_stats || {};
    const data = info.data || {};
    const order = info.card_tag_order || Object.keys(stats);

    let stdSum = 0, initSum = 0;
    let realSum = 0, initRealSum = 0;
    let hybSum = 0, initHybSum = 0;
    let unvNomSum = 0;
    const unvKeys = new Set();
    let initTotal = 0, firstTagRem = 1;

    order.forEach((tag, idx) => {
      const st = stats[tag];
      if (!st) return;
      const u = parseInt(st.undrawn || 0, 10) || 0;
      const t = parseInt(st.total || 0, 10) || 0;
      if (idx === 0) firstTagRem = u;
      initTotal += t;

      const items = data[tag] || [];
      const inStock = items.filter((c) => (parseInt(c.card_stock || 0, 10) || 0) > 0);
      const stockCnt = inStock.reduce((acc, c) => acc + (parseInt(c.card_stock || 0, 10) || 0), 0);
      const isPc = tag.toLowerCase().includes('pc');

      if (items.length > 0) {
        const vals = items.map(resolveCardValues);
        const scale = items.length === t ? 1 : t / items.length;
        initSum += vals.reduce((a, v) => a + v.pNom, 0) * scale;
        initRealSum += vals.reduce((a, v) => a + v.pReal, 0) * scale;
        initHybSum += vals.reduce((a, v) => a + v.pHybrid, 0) * scale;
      } else {
        const fb = isPc ? state.pcPackValue : 0;
        initSum += t * fb;
        initRealSum += t * fb;
        initHybSum += t * fb;
      }

      if (u === 0) return;
      if (items.length > 0 && stockCnt === u) {
        inStock.forEach((c) => {
          const stk = parseInt(c.card_stock || 0, 10) || 0;
          const v = resolveCardValues(c);
          stdSum += v.pNom * stk;
          realSum += v.pReal * stk;
          hybSum += v.pHybrid * stk;
          if (!v.verified) {
            unvNomSum += v.pNom * stk;
            unvKeys.add(`${decodeHtml(c.card_name)}||${normCond(c.deal_condition)}`);
          }
        });
      } else if (inStock.length > 0) {
        const vals = inStock.map(resolveCardValues);
        const n = inStock.length;
        stdSum += u * (vals.reduce((a, v) => a + v.pNom, 0) / n);
        realSum += u * (vals.reduce((a, v) => a + v.pReal, 0) / n);
        hybSum += u * (vals.reduce((a, v) => a + v.pHybrid, 0) / n);
        unvNomSum += u * (vals.filter((v) => !v.verified).reduce((a, v) => a + v.pNom, 0) / n);
        inStock.forEach((c, i) => {
          if (!vals[i].verified) unvKeys.add(`${decodeHtml(c.card_name)}||${normCond(c.deal_condition)}`);
        });
      } else {
        const fb = isPc ? state.pcPackValue : 0;
        stdSum += u * fb;
        realSum += u * fb;
        hybSum += u * fb;
      }
    });

    const initEv = initTotal > 0 ? initSum / initTotal : 0;
    const initRoi = price > 0 ? (initEv / price) * 100 : 0;
    const ev = rem > 0 ? stdSum / rem : initEv;
    const roi = rem > 0 && price > 0 ? (ev / price) * 100 : initRoi;
    const deltaRoi = rem > 0 ? roi - initRoi : 0;

    const initRealEv = initTotal > 0 ? initRealSum / initTotal : 0;
    const initRealRoi = price > 0 ? (initRealEv / price) * 100 : 0;
    const realEv = rem > 0 ? realSum / rem : initRealEv;
    const realRoi = rem > 0 && price > 0 ? (realEv / price) * 100 : initRealRoi;

    const initHybEv = initTotal > 0 ? initHybSum / initTotal : 0;
    const initHybRoi = price > 0 ? (initHybEv / price) * 100 : 0;
    const hybEv = rem > 0 ? hybSum / rem : initHybEv;
    const hybRoi = rem > 0 && price > 0 ? (hybEv / price) * 100 : initHybRoi;

    const inflationRatio = realEv > 0 ? ev / realEv : 0;
    const unverifiedCnt = unvKeys.size;
    const unverifiedPct = rem > 0 && stdSum > 0 ? (unvNomSum / stdSum) * 100 : 0;

    return {
      id: poolId,
      name: info.card_sort_name || `Pool #${poolId}`,
      price,
      rem,
      total: initTotal,
      firstTagRem,
      ev,
      roi,
      deltaRoi,
      realEv,
      realRoi,
      hybEv,
      hybRoi,
      inflationRatio,
      unverifiedCnt,
      unverifiedPct,
      info,
    };
  }

  const LUCKU_HEADERS = {
    accept: 'application/json, text/plain, */*',
    'content-type': 'application/json',
    language: 'zh-Hant',
  };

  async function loadCloudCaches(bustCache = false) {
    const q = bustCache ? `?t=${Date.now()}` : '';
    try {
      const m = await gmFetchJson(`${MARKET_URL}${q}`);
      if (m && m.items) state.market = m;
    } catch (e) {
      console.warn('[Lucku Radar] Failed to load market_prices.json:', e);
    }
    try {
      const s = await gmFetchJson(`${STATE_URL}${q}`);
      if (s) {
        if (typeof s.diamond_rate === 'number') state.diamondRate = s.diamond_rate;
        if (typeof s.pc_pack_value === 'number') state.pcPackValue = s.pc_pack_value;
        if (Array.isArray(s.muted_pool_ids)) state.mutedIds = new Set(s.muted_pool_ids.map(Number));
      }
    } catch (e) {
      console.warn('[Lucku Radar] Failed to load state.json:', e);
    }
  }

  async function fetchPoolById(pid) {
    const res = await gmFetchJson(`${API_BASE}/getCardsBySort`, 'POST', {
      page: '1',
      limit: '9999',
      card_sort_id: String(pid),
    }, LUCKU_HEADERS);
    return res && res.results ? res.results : null;
  }

  async function scanAllPools() {
    state.loading = true;
    render();
    try {
      const res = await gmFetchJson(`${API_BASE}/getCardSortByBrand`, 'POST', {
        page: '1',
        limit: '100',
        brand: '',
      }, LUCKU_HEADERS);
      const metas = (res && res.results && res.results.data) || [];
      const rawInfos = [];
      const batchSize = 6;
      for (let i = 0; i < metas.length; i += batchSize) {
        const chunk = metas.slice(i, i + batchSize);
        const fetched = await Promise.all(
          chunk.map(async (p) => {
            try {
              const info = await fetchPoolById(p.card_sort_id);
              return info ? { id: Number(p.card_sort_id), info } : null;
            } catch {
              return null;
            }
          })
        );
        fetched.forEach((x) => x && rawInfos.push(x));
      }
      state.pools = rawInfos.map((x) => calcPoolMetrics(x.id, x.info));
    } catch (e) {
      console.error('[Lucku Radar] Scan error:', e);
    } finally {
      state.loading = false;
      render();
    }
  }

  function recalcAll() {
    state.pools = state.pools.map((p) => calcPoolMetrics(p.id, p.info));
    render();
  }

  function detectRoutePoolId() {
    const m = window.location.hash.match(/\/cards\/(\d+)/) || window.location.pathname.match(/\/cards\/(\d+)/);
    return m ? Number(m[1]) : null;
  }

  async function syncCurrentPool(forceFetch = false) {
    const pid = detectRoutePoolId();
    if (!pid) {
      state.currentPoolId = null;
      state.currentPoolInfo = null;
      return;
    }
    if (pid === state.currentPoolId && state.currentPoolInfo && !forceFetch) return;
    state.currentPoolId = pid;
    const cached = state.pools.find((p) => p.id === pid);
    if (cached && !forceFetch) {
      state.currentPoolInfo = cached.info;
      render();
      return;
    }
    try {
      const info = await fetchPoolById(pid);
      if (info) {
        state.currentPoolInfo = info;
        render();
      }
    } catch (e) {
      console.warn('[Lucku Radar] Fetch current pool failed:', e);
    }
  }

  // ==================== UI ====================
  const style = document.createElement('style');
  style.textContent = `
    #lucku-radar-root {
      position: fixed; right: 16px; bottom: 16px; z-index: 999999;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      color: #f1f5f9; font-size: 12px; line-height: 1.45;
    }
    #lucku-radar-toggle {
      background: linear-gradient(135deg, #0f172a, #1e293b);
      border: 1px solid #38bdf8; color: #f8fafc; padding: 8px 14px;
      border-radius: 999px; cursor: pointer; font-weight: 700; font-size: 13px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.45);
    }
    #lucku-radar-panel {
      width: 470px; max-width: 94vw; max-height: 82vh; display: flex; flex-direction: column;
      background: rgba(15, 23, 42, 0.96); backdrop-filter: blur(10px);
      border: 1px solid rgba(56, 189, 248, 0.45); border-radius: 12px;
      box-shadow: 0 16px 40px rgba(0,0,0,0.6); overflow: hidden;
    }
    .lr-hdr {
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 12px; background: #0f172a; border-bottom: 1px solid #334155;
    }
    .lr-title { font-weight: 700; font-size: 13px; color: #38bdf8; }
    .lr-btn {
      background: #1e293b; border: 1px solid #475569; color: #e2e8f0;
      border-radius: 6px; padding: 3px 8px; cursor: pointer; font-size: 11px;
    }
    .lr-btn:hover { background: #334155; }
    .lr-btn.active { background: #0284c7; border-color: #38bdf8; color: #fff; }
    .lr-bar {
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
      padding: 8px 12px; background: #1e293b; border-bottom: 1px solid #334155;
    }
    .lr-body { padding: 8px 12px; overflow-y: auto; flex: 1; }
    .lr-card {
      background: rgba(30, 41, 59, 0.85); border: 1px solid #334155;
      border-radius: 8px; padding: 8px 10px; margin-bottom: 8px; cursor: pointer;
    }
    .lr-card:hover { border-color: #38bdf8; }
    .lr-kpi-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 8px 0; }
    .lr-kpi { background: #0f172a; border: 1px solid #334155; border-radius: 6px; padding: 6px; text-align: center; }
    .lr-kpi-val { font-size: 15px; font-weight: 700; }
    .lr-badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; margin-left: 4px; }
    .lr-g { background: rgba(34,197,94,0.2); color: #4ade80; }
    .lr-y { background: rgba(234,179,8,0.2); color: #facc15; }
    .lr-r { background: rgba(239,68,68,0.2); color: #f87171; }
    .lr-w { background: rgba(245,158,11,0.25); color: #fbbf24; }
    .lr-row { display: flex; justify-content: space-between; align-items: center; gap: 6px; padding: 4px 0; border-bottom: 1px dashed #334155; }
    .lr-input { width: 52px; background: #0f172a; color: #f8fafc; border: 1px solid #475569; border-radius: 4px; padding: 2px 4px; font-size: 11px; }
  `;
  document.head.appendChild(style);

  const root = document.createElement('div');
  root.id = 'lucku-radar-root';
  document.body.appendChild(root);

  function inflationBadge(ratio) {
    if (!ratio || ratio <= 0) return '';
    if (ratio >= 1.35) return `<span class="lr-badge lr-r">灌水 ${ratio.toFixed(2)}x</span>`;
    if (ratio >= 1.15) return `<span class="lr-badge lr-y">溢價 ${ratio.toFixed(2)}x</span>`;
    return `<span class="lr-badge lr-g">實價 ${ratio.toFixed(2)}x</span>`;
  }

  function renderRankBody() {
    if (state.loading) return '<div style="padding:18px;text-align:center;color:#94a3b8;">正在掃描全站卡池與比對 SNKRDUNK 港幣實價...</div>';
    const active = state.pools.filter((p) => p.rem > 0 && p.firstTagRem > 0 && !state.mutedIds.has(p.id));
    if (!active.length) return '<div style="padding:18px;text-align:center;color:#94a3b8;">尚無資料，請點擊上方「掃描全站」</div>';

    const sorted = [...active].sort((a, b) => {
      if (state.sortBy === 'real_roi') return b.realRoi - a.realRoi;
      if (state.sortBy === 'hybrid_roi') return b.hybRoi - a.hybRoi;
      if (state.sortBy === 'delta_roi') return b.deltaRoi - a.deltaRoi;
      if (state.sortBy === 'inflation_ratio') return b.inflationRatio - a.inflationRatio;
      return b.roi - a.roi;
    });

    return sorted.map((p, i) => `
      <div class="lr-card" data-pid="${p.id}">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="font-weight:700;color:#e2e8f0;">${i + 1}. #${p.id} ${escHtml(p.name)}</div>
          <div style="color:#94a3b8;white-space:nowrap;">$${p.price.toFixed(0)} · 剩${p.rem}/${p.total}</div>
        </div>
        <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
          <span>💎 實卡 <b style="color:#38bdf8;">${p.realRoi.toFixed(1)}%</b></span>
          <span>♻️ 套利 <b style="color:#4ade80;">${p.hybRoi.toFixed(1)}%</b></span>
          <span>📊 帳面 <b>${p.roi.toFixed(1)}%</b> (Δ${p.deltaRoi >= 0 ? '+' : ''}${p.deltaRoi.toFixed(1)}%)</span>
        </div>
        <div style="margin-top:3px;">
          ${inflationBadge(p.inflationRatio)}
          ${p.unverifiedCnt > 0 ? `<span class="lr-badge lr-w">⚠️未驗價 ${p.unverifiedCnt}款(${p.unverifiedPct.toFixed(0)}%)</span>` : '<span class="lr-badge lr-g">✓100%驗價</span>'}
        </div>
      </div>
    `).join('');
  }

  function renderDetailBody() {
    if (!state.currentPoolId || !state.currentPoolInfo) {
      return '<div style="padding:18px;text-align:center;color:#94a3b8;">請先進入任一卡池頁面（#/cards/ID）或在「全站排行」點擊任一卡池。</div>';
    }
    const info = state.currentPoolInfo;
    const m = calcPoolMetrics(state.currentPoolId, info);
    const stats = info.tag_stats || {};
    const data = info.data || {};
    const order = info.card_tag_order || Object.keys(stats);

    let tiersHtml = '';
    order.forEach((tag) => {
      const st = stats[tag];
      if (!st) return;
      const u = parseInt(st.undrawn || 0, 10) || 0;
      const t = parseInt(st.total || 0, 10) || 0;
      const prob = m.rem > 0 ? ((u / m.rem) * 100).toFixed(2) : '0.00';
      const items = data[tag] || [];
      const inStock = items.filter((c) => (parseInt(c.card_stock || 0, 10) || 0) > 0);
      const isFiller = ['5等', '五等', '6等', '六等', '7等', '七等', 'pc', '白卡', '末賞'].some((k) => tag.toLowerCase().includes(k));

      let cardsHtml = '';
      if (!isFiller && inStock.length > 0) {
        const sortedCards = [...inStock].sort((a, b) => (parseFloat(b.card_price || 0) - parseFloat(a.card_price || 0)));
        cardsHtml = sortedCards.slice(0, 8).map((c) => {
          const v = resolveCardValues(c);
          const stk = parseInt(c.card_stock || 0, 10) || 0;
          const cname = decodeHtml(c.card_name || '');
          const ratio = v.pReal > 0 ? v.pNom / v.pReal : 0;
          let badge = '';
          if (!v.verified) {
            badge = '<span class="lr-badge lr-w">⚠️未驗價</span>';
          } else if (v.pReal >= v.pNom * state.diamondRate) {
            badge = `<span class="lr-badge lr-g">📦提貨 ${ratio.toFixed(2)}x</span>`;
          } else {
            badge = `<span class="lr-badge lr-r">💎退鑽 ${ratio > 0 ? ratio.toFixed(2) + 'x' : ''}</span>`;
          }
          const linkOpen = v.snkrId ? `<a href="https://snkrdunk.com/en/trading-cards/${v.snkrId}" target="_blank" style="color:#93c5fd;text-decoration:none;">` : '';
          const linkClose = v.snkrId ? '</a>' : '';
          return `
            <div class="lr-row">
              <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px;" title="${escHtml(cname)}">${linkOpen}${escHtml(cname)}${linkClose} <span style="color:#94a3b8;">x${stk}</span></div>
              <div style="white-space:nowrap;">帳$${v.pNom.toFixed(0)} → 實<b>$${v.pReal.toFixed(0)}</b>${badge}</div>
            </div>
          `;
        }).join('');
      }

      tiersHtml += `
        <div style="margin-top:8px;padding:6px 8px;background:#0f172a;border-radius:6px;border:1px solid #334155;">
          <div style="font-weight:700;color:#cbd5e1;">${escHtml(tag)} — 剩 ${u}/${t} (${prob}%)</div>
          ${cardsHtml}
        </div>
      `;
    });

    return `
      <div>
        <div style="font-weight:700;font-size:13px;color:#f8fafc;">#${m.id} ${escHtml(m.name)} ${inflationBadge(m.inflationRatio)}</div>
        <div style="color:#94a3b8;margin-top:2px;">單抽 $${m.price.toFixed(0)} · 剩餘 ${m.rem}/${m.total} ${m.unverifiedCnt > 0 ? `· <span style="color:#fbbf24;">⚠️ 未驗價 ${m.unverifiedCnt}款 (${m.unverifiedPct.toFixed(1)}%)</span>` : '· ✅ 100%已驗價'}</div>
        <div class="lr-kpi-grid">
          <div class="lr-kpi"><div style="color:#94a3b8;font-size:10px;">💎 純實卡 ROI</div><div class="lr-kpi-val" style="color:#38bdf8;">${m.realRoi.toFixed(1)}%</div><div style="color:#94a3b8;font-size:10px;">EV $${m.realEv.toFixed(1)}</div></div>
          <div class="lr-kpi"><div style="color:#94a3b8;font-size:10px;">♻️ 混合套利 ROI</div><div class="lr-kpi-val" style="color:#4ade80;">${m.hybRoi.toFixed(1)}%</div><div style="color:#94a3b8;font-size:10px;">EV $${m.hybEv.toFixed(1)}</div></div>
          <div class="lr-kpi"><div style="color:#94a3b8;font-size:10px;">📊 平台帳面 ROI</div><div class="lr-kpi-val" style="color:#e2e8f0;">${m.roi.toFixed(1)}%</div><div style="color:#94a3b8;font-size:10px;">Δ${m.deltaRoi >= 0 ? '+' : ''}${m.deltaRoi.toFixed(1)}%</div></div>
        </div>
        ${tiersHtml}
      </div>
    `;
  }

  function render() {
    if (state.collapsed) {
      root.innerHTML = `<button id="lucku-radar-toggle">💎 實價雷達 (${state.market.stats?.verified || 0}/${state.market.stats?.total || 0})</button>`;
      root.querySelector('#lucku-radar-toggle').onclick = async () => {
        state.collapsed = false;
        const pid = detectRoutePoolId();
        if (pid) {
          state.tab = 'detail';
          await syncCurrentPool(true);
        } else if (!state.pools.length) {
          scanAllPools();
        }
        render();
      };
      return;
    }

    const upd = (state.market.updated_at || '').slice(5, 16).replace('T', ' ');
    root.innerHTML = `
      <div id="lucku-radar-panel">
        <div class="lr-hdr">
          <div class="lr-title">💎 Lucku 真實市價雷達 <span style="color:#94a3b8;font-weight:400;font-size:10px;">(${state.market.stats?.verified || 0}/${state.market.stats?.total || 0} · ${escHtml(upd)})</span></div>
          <div style="display:flex;gap:5px;">
            <button class="lr-btn" id="lr-refresh">🔄 刷新</button>
            <button class="lr-btn" id="lr-close">收合</button>
          </div>
        </div>
        <div class="lr-bar">
          <button class="lr-btn ${state.tab === 'rank' ? 'active' : ''}" id="lr-tab-rank">全站排行</button>
          <button class="lr-btn ${state.tab === 'detail' ? 'active' : ''}" id="lr-tab-detail">當前卡池${state.currentPoolId ? ` #${state.currentPoolId}` : ''}</button>
          <span style="margin-left:auto;color:#94a3b8;font-size:11px;">退鑽率:</span>
          <input class="lr-input" id="lr-diamond" type="number" step="0.05" min="0.1" max="1.0" value="${state.diamondRate.toFixed(2)}" />
        </div>
        ${state.tab === 'rank' ? `
        <div class="lr-bar" style="padding:5px 12px;background:#0f172a;">
          <span style="color:#94a3b8;font-size:11px;">排序:</span>
          <button class="lr-btn ${state.sortBy === 'real_roi' ? 'active' : ''}" data-sort="real_roi">💎實卡</button>
          <button class="lr-btn ${state.sortBy === 'hybrid_roi' ? 'active' : ''}" data-sort="hybrid_roi">♻️套利</button>
          <button class="lr-btn ${state.sortBy === 'roi' ? 'active' : ''}" data-sort="roi">📊帳面</button>
          <button class="lr-btn ${state.sortBy === 'delta_roi' ? 'active' : ''}" data-sort="delta_roi">🎯撿漏</button>
          <button class="lr-btn ${state.sortBy === 'inflation_ratio' ? 'active' : ''}" data-sort="inflation_ratio">🔴灌水</button>
        </div>` : ''}
        <div class="lr-body">
          ${state.tab === 'rank' ? renderRankBody() : renderDetailBody()}
        </div>
      </div>
    `;

    root.querySelector('#lr-close').onclick = () => { state.collapsed = true; render(); };
    root.querySelector('#lr-refresh').onclick = async () => {
      await loadCloudCaches(true);
      await scanAllPools();
      if (state.currentPoolId) await syncCurrentPool(true);
    };
    root.querySelector('#lr-tab-rank').onclick = () => {
      state.tab = 'rank';
      if (!state.pools.length) scanAllPools();
      else render();
    };
    root.querySelector('#lr-tab-detail').onclick = async () => {
      state.tab = 'detail';
      await syncCurrentPool(true);
      render();
    };
    root.querySelector('#lr-diamond').onchange = (e) => {
      const v = parseFloat(e.target.value);
      if (v >= 0.1 && v <= 1.0) {
        state.diamondRate = v;
        recalcAll();
      }
    };
    root.querySelectorAll('[data-sort]').forEach((btn) => {
      btn.onclick = () => { state.sortBy = btn.getAttribute('data-sort'); render(); };
    });
    root.querySelectorAll('.lr-card[data-pid]').forEach((card) => {
      card.onclick = async () => {
        const pid = Number(card.getAttribute('data-pid'));
        window.location.hash = `#/cards/${pid}`;
        state.currentPoolId = pid;
        state.tab = 'detail';
        await syncCurrentPool(false);
        render();
      };
    });
  }

  // 初始化
  (async () => {
    await loadCloudCaches(false);
    render();
    window.addEventListener('hashchange', () => {
      if (!state.collapsed) syncCurrentPool(false);
    });
  })();
})();
