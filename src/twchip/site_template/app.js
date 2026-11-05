// 台股籌碼資料庫前端。全部資料由 GitHub Actions 預先算好放在 data/，這裡只負責顯示。
"use strict";

const $ = (s) => document.querySelector(s);
const cache = new Map();
const charts = {};
let META = null, STOCKS = [], BROKERS = null;

async function get(path) {
  if (!cache.has(path)) {
    cache.set(path, fetch(`data/${path}`, { cache: "no-cache" }).then((r) => {
      if (!r.ok) throw new Error(`${path}：HTTP ${r.status}`);
      return r.json();
    }));
  }
  return cache.get(path);
}

const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "—"
  : Number(v).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });
const sgn = (v, d = 0) => (v === null || v === undefined) ? "—" : (v > 0 ? "+" : "") + fmt(v, d);
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const status = (msg) => { $("#status").textContent = msg || ""; };

function table(el, head, rows, opt = {}) {
  const th = head.map((h) => `<th class="${h.n ? "n" : ""}">${esc(h.t)}</th>`).join("");
  const body = rows.map((r) => {
    const tds = r.cells.map((c, i) => `<td class="${head[i].n ? "n " : ""}${c.c || ""}">${c.h ?? esc(c.t)}</td>`).join("");
    return `<tr class="${r.link ? "link" : ""} ${r.sel ? "sel" : ""} ${r.cls || ""}" ${r.link ? `data-link="${esc(r.link)}"` : ""}>${tds}</tr>`;
  }).join("");
  el.innerHTML = `<thead><tr>${th}</tr></thead><tbody>${body || `<tr><td colspan="${head.length}">${opt.empty || "無資料"}</td></tr>`}</tbody>`;
  el.querySelectorAll("tr[data-link]").forEach((tr) => tr.addEventListener("click", () => { location.hash = tr.dataset.link; }));
}

// ------------------------------------------------------------------ 路由
const TABS = ["rank", "stock", "broker", "health"];
async function route() {
  const [tab, arg] = (location.hash.slice(1) || "rank").split("/");
  const t = TABS.includes(tab) ? tab : "rank";
  TABS.forEach((x) => { $(`#tab-${x}`).hidden = x !== t; });
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.tab === t));
  status("");
  try {
    if (t === "rank") await showRank();
    if (t === "stock") await showStock(arg ? decodeURIComponent(arg) : (localStorage_get("lastStock") || "2330"));
    if (t === "broker") await showBroker(arg ? decodeURIComponent(arg) : null);
    if (t === "health") await showHealth();
  } catch (e) {
    status(`載入失敗：${e.message}`);
  }
}
function localStorage_get(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorage_set(k, v) { try { localStorage.setItem(k, v); } catch { /* 無法儲存時略過 */ } }

// ------------------------------------------------------------------ 法人排行
const ETF = /^00/;
async function showRank() {
  const sel = $("#r-win");
  if (!sel.options.length) {
    META.windows.forEach((w) => sel.add(new Option(w === 1 ? "當日" : `${w} 日`, w)));
    sel.value = META.windows.includes(5) ? 5 : META.windows[0];
    ["#r-win", "#r-who", "#r-side", "#r-mkt", "#r-etf"].forEach((s) => $(s).addEventListener("change", showRank));
  }
  const w = sel.value, who = $("#r-who").value, desc = $("#r-side").value === "desc";
  const mkt = $("#r-mkt").value, noEtf = $("#r-etf").checked;
  const d = await get(`rank_${w}.json`);
  const F = Object.fromEntries(d.fields.map((f, i) => [f, i]));
  let rows = d.rows.filter((r) => (!mkt || r[F.market] === mkt) && (!noEtf || !ETF.test(r[F.code])) && r[F[who]] !== null);
  rows.sort((a, b) => (desc ? b[F[who]] - a[F[who]] : a[F[who]] - b[F[who]]) || (a[F.code] < b[F.code] ? -1 : 1));
  rows = rows.slice(0, 100);
  $("#r-range").textContent = `${d.start} ～ ${d.end}，${d.trading_days} 個交易日。買賣超單位：張。外資持股變化單位：百分點（官方持股統計）。`;
  const head = [{ t: "#" }, { t: "股票" }, { t: "市場" }, { t: "外資", n: 1 }, { t: "投信", n: 1 }, { t: "自營商", n: 1 },
    { t: "合計", n: 1 }, { t: "收盤", n: 1 }, { t: "區間漲跌%", n: 1 }, { t: "外資持股%", n: 1 }, { t: "外資持股變化", n: 1 }];
  table($("#r-table"), head, rows.map((r, i) => ({
    link: `stock/${r[F.code]}`,
    cells: [{ t: i + 1 }, { h: `<span class="badge">${esc(r[F.code])}</span>${esc(r[F.name])}` }, { t: r[F.market] },
      ...["foreign", "trust", "dealer", "total"].map((k) => ({ t: sgn(r[F[k]]), c: cls(r[F[k]]) })),
      { t: fmt(r[F.close], 2) }, { t: sgn(r[F.chg_pct], 2), c: cls(r[F.chg_pct]) },
      { t: fmt(r[F.foreign_ratio], 2) }, { t: sgn(r[F.foreign_ratio_chg], 2), c: cls(r[F.foreign_ratio_chg]) }],
  })));
}

// ------------------------------------------------------------------ 個股
function chart(id, type, labels, datasets, opt = {}) {
  if (charts[id]) charts[id].destroy();
  if (typeof Chart === "undefined") return;
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--muted").trim(), line = css.getPropertyValue("--line").trim();
  charts[id] = new Chart(document.getElementById(id), {
    type, data: { labels, datasets },
    options: {
      animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: datasets.length > 1, labels: { color: ink } } },
      scales: {
        x: { stacked: !!opt.stacked, ticks: { color: ink, maxTicksLimit: 8 }, grid: { color: line } },
        y: { stacked: !!opt.stacked, ticks: { color: ink }, grid: { color: line } },
      },
      elements: { point: { radius: 0 } },
    },
  });
}

async function showStock(code) {
  status("");
  let d;
  try { d = await get(`stock/${code}.json`); } catch { status(`找不到 ${code} 的資料`); return; }
  localStorage_set("lastStock", code);
  if (!$("#s-range").dataset.bound) { $("#s-range").addEventListener("change", () => showStock(code)); $("#s-range").dataset.bound = 1; }
  const n = Number($("#s-range").value) || d.date.length, k = Math.max(0, d.date.length - n);
  const cut = (a) => (a || []).slice(k);
  const css = getComputedStyle(document.documentElement);
  const col = (v) => css.getPropertyValue(v).trim();
  $("#s-title").textContent = `${d.code} ${d.name}（${d.market}）`;
  const last = d.date.length - 1;
  const sum = (a, m) => (a || []).slice(-m).reduce((s, x) => s + (x || 0), 0);
  $("#s-cards").innerHTML = [
    ["最新日期", d.date[last]], ["收盤", fmt(d.close[last], 2)],
    ["外資 5 日（張）", sgn(sum(d.foreign, 5))], ["投信 5 日（張）", sgn(sum(d.trust, 5))],
    ["外資持股", d.foreign_ratio.length ? fmt(d.foreign_ratio[last], 2) + "%" : "—"],
  ].map(([kk, v]) => `<div class="card"><div class="k">${kk}</div><div class="v">${esc(v)}</div></div>`).join("");
  const L = cut(d.date);
  chart("c-price", "line", L, [{ label: "收盤", data: cut(d.close), borderColor: col("--accent"), borderWidth: 1.5 }]);
  chart("c-flow", "bar", L, [
    { label: "外資", data: cut(d.foreign), backgroundColor: "#4c78a8" },
    { label: "投信", data: cut(d.trust), backgroundColor: "#f58518" },
    { label: "自營商", data: cut(d.dealer), backgroundColor: "#9d755d" },
  ], { stacked: true });
  $("#box-fr").hidden = !d.foreign_ratio.length;
  if (d.foreign_ratio.length) chart("c-fr", "line", L, [{ label: "外資持股%", data: cut(d.foreign_ratio), borderColor: col("--accent"), borderWidth: 1.5 }]);

  const b = d.broker;
  $("#s-broker").hidden = !b;
  if (b) {
    const sel = $("#s-bdate"), dates = Object.keys(b.days).sort().reverse();
    sel.innerHTML = dates.map((x) => `<option>${x}</option>`).join("");
    const draw = () => {
      const day = b.days[sel.value];
      const head = [{ t: "#" }, { t: "分點" }, { t: "買進", n: 1 }, { t: "賣出", n: 1 }, { t: "買賣超", n: 1 }, { t: "佔比%", n: 1 }];
      const rows = (side) => day[side].map((r) => ({
        link: r[2] ? `broker/${r[2]}` : "",   // r[2] 是分點鍵（key）
        cells: [{ t: r[0] }, { t: r[1] }, { t: fmt(r[3]) }, { t: fmt(r[4]) }, { t: sgn(r[5]), c: cls(r[5]) }, { t: fmt(r[6], 2) }],
      }));
      table($("#s-buy"), head, rows("buy"));
      table($("#s-sell"), head, rows("sell"));
    };
    sel.onchange = draw;
    draw();
  }
}

// ------------------------------------------------------------------ 券商分點
// 釘選的分點存在這個瀏覽器的 localStorage（網站是靜態頁，沒有帳號可以同步）。
// 以「分點鍵」儲存，不用名稱：名稱會因合併改變，918G 與 918g 也是不同分點。
// 瀏覽器不允許儲存時（例如部分無痕模式），釘選仍在這次瀏覽中有效，只是關掉頁面後不保留。
const PIN_STORE = "pinnedBranches";
let PINS = null;
function loadPins() {
  if (!PINS) {
    try { const a = JSON.parse(localStorage_get(PIN_STORE) || "[]"); PINS = new Set(Array.isArray(a) ? a.map(String) : []); }
    catch { PINS = new Set(); }
  }
  return PINS;
}
function savePins(s) { PINS = s; localStorage_set(PIN_STORE, JSON.stringify([...s].sort())); }

let BKEY = null;   // 目前右邊顯示的分點；搜尋框與開關的事件都讀它，不綁死第一次開啟時的分點
async function showBroker(key) {
  BKEY = key;
  if (!BROKERS) BROKERS = await get("broker/index.json");
  const F = Object.fromEntries(BROKERS.fields.map((f, i) => [f, i]));
  const q = $("#b-q"), only = $("#b-only");
  if (!q.dataset.bound) { q.addEventListener("input", () => showBroker(BKEY)); q.dataset.bound = 1; }
  if (!only.dataset.bound) { only.addEventListener("change", () => showBroker(BKEY)); only.dataset.bound = 1; }
  const kw = q.value.trim();
  const pins = loadPins();
  // 釘選了、但目前資料裡找不到的分點：多半是券商合併後富邦改了分點代號
  const known = new Set(BROKERS.rows.map((r) => r[F.key]));
  const lost = [...pins].filter((k) => !known.has(k)).length;
  $("#b-pins").textContent = pins.size
    ? `已釘選 ${pins.size - lost} 個` + (lost ? `；另有 ${lost} 個目前找不到（可能是券商合併後代號改變）` : "") : "";
  // 分點代號區分大小寫（918G 與 918g 是不同分點），搜尋時不分大小寫比對，顯示時保留原樣
  let list = BROKERS.rows.filter((r) => !kw || r[F.branch].toUpperCase().includes(kw.toUpperCase()) || r[F.name].includes(kw) || r[F.aliases].some((a) => a.includes(kw)));
  if (only.checked) list = list.filter((r) => pins.has(r[F.key]));
  // 釘選的排最前面；同一組內依上榜次數由多到少
  list.sort((a, b) => (pins.has(b[F.key]) - pins.has(a[F.key])) || (b[F.appearances] - a[F.appearances]));
  table($("#b-list"), [{ t: "" }, { t: "分點" }, { t: "上榜次數", n: 1 }, { t: "股票數", n: 1 }, { t: "淨買賣超（張）", n: 1 }],
    list.slice(0, Math.max(300, pins.size)).map((r) => {
      const k = r[F.key], on = pins.has(k), act = on ? "取消釘選" : "釘選";
      return {
        link: `broker/${k}`, sel: k === key, cls: on ? "pinned" : "",
        cells: [{ c: "pin-cell", h: `<label title="${act}"><input type="checkbox" class="pin" data-key="${esc(k)}"${on ? " checked" : ""} aria-label="${act} ${esc(r[F.branch])} ${esc(r[F.name])}"></label>` },
          { h: `<span class="badge">${esc(r[F.branch])}</span>${esc(r[F.name])}` }, { t: fmt(r[F.appearances]) },
          { t: r[F.stocks] }, { t: sgn(r[F.net_lots]), c: cls(r[F.net_lots]) }],
      };
    }), { empty: only.checked ? "還沒有釘選的分點。取消「只看釘選」，再勾選分點左邊的方框。" : "找不到符合的分點" });
  $("#b-list").querySelectorAll("td.pin-cell").forEach((td) => {
    td.addEventListener("click", (e) => e.stopPropagation());   // 點方框只釘選，不切換右邊顯示的分點
    td.querySelector("input.pin").addEventListener("change", (e) => {
      const s = loadPins();
      if (e.target.checked) s.add(e.target.dataset.key); else s.delete(e.target.dataset.key);
      savePins(s);
      showBroker(BKEY);
    });
  });
  if (!key) { $("#b-title").textContent = "選一個分點"; $("#b-alias").textContent = ""; $("#b-rows").innerHTML = ""; return; }
  const meta = BROKERS.rows.find((r) => r[F.key] === key);
  const d = await get(`broker/${key}.json`);
  $("#b-title").textContent = meta ? `${meta[F.branch]} ${meta[F.name]}（總公司 ${meta[F.hq]}）` : key;
  $("#b-alias").textContent = meta && meta[F.aliases].length ? `曾用名稱：${meta[F.aliases].join("、")}（券商合併或改名）` : "";
  table($("#b-rows"), [{ t: "日期" }, { t: "股票" }, { t: "方向" }, { t: "名次", n: 1 }, { t: "買賣超（張）", n: 1 }, { t: "佔比%", n: 1 }],
    d.rows.slice().reverse().map((r) => ({
      link: `stock/${r[1]}`,
      cells: [{ t: r[0] }, { h: `<span class="badge">${esc(r[1])}</span>${esc(r[2])}` }, { t: r[3] === "buy" ? "買超" : "賣超" },
        { t: r[4] }, { t: sgn(r[5]), c: cls(r[5]) }, { t: fmt(r[6], 2) }],
    })));
}

// ------------------------------------------------------------------ 資料健康
const PILL = { published: "ok", cached: "ok", quarantined: "bad", error: "bad", blocked: "warn", pending: "warn", not_published: "none" };
async function showHealth() {
  const h = await get("health.json");
  const units = [...new Set(h.days.flatMap((d) => Object.keys(d.units)))].sort();
  const short = (u) => u.replace("foreign_holdings", "外資持股").replace("inst_flows", "法人").replace("quotes", "行情").replace("broker_top", "分點");
  table($("#h-days"), [{ t: "日期" }, { t: "日曆" }, ...units.map((u) => ({ t: short(u) }))],
    h.days.map((d) => ({
      cells: [{ t: d.date, c: "d" }, { t: d.calendar },
        ...units.map((u) => { const s = d.units[u] || ""; return { h: s ? `<span class="pill ${PILL[s] || "none"}">${esc(s)}</span>` : "" }; })],
    })), { empty: "尚無紀錄" });
  table($("#h-checks"), [{ t: "檢查" }, { t: "說明" }, { t: "等級" }, { t: "通過", n: 1 }, { t: "未通過", n: 1 }, { t: "略過", n: 1 }],
    h.checks.map((c) => ({ cells: [{ t: c.id }, { t: c.name }, { t: c.level === "B" ? "阻擋" : "警告" }, { t: fmt(c.pass) },
      { t: fmt(c.fail), c: c.fail ? "pos" : "" }, { t: fmt(c.skip) }] })));
  table($("#h-fails"), [{ t: "日期" }, { t: "單位" }, { t: "檢查" }, { t: "等級" }, { t: "內容" }],
    h.fails.map((f) => ({ cells: [{ t: f[0] }, { t: f[1] }, { t: f[2] }, { t: f[3] === "B" ? "阻擋" : "警告" }, { t: f[4] }] })),
    { empty: "近期沒有未通過的檢查" });
  $("#h-legacy").innerHTML = h.legacy ? `<h3>與舊專案資料逐列比對</h3><div class="table-wrap"><table class="num"><thead><tr><th>資料集</th><th class="n">兩邊都有</th><th class="n">不一致</th><th class="n">只在官方</th><th class="n">只在舊資料</th></tr></thead><tbody>${
    Object.entries(h.legacy).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${fmt(v.both)}</td><td class="n">${fmt(v.mismatched)}</td><td class="n">${fmt(v.only_official)}</td><td class="n">${fmt(v.only_legacy)}</td></tr>`).join("")
  }</tbody></table></div>` : "";
}

// ------------------------------------------------------------------ 啟動
async function main() {
  try {
    META = await get("meta.json");
  } catch (e) {
    status(`無法載入資料：${e.message}`); return;
  }
  if (META.empty) { status("尚無資料：等第一次回補完成後就會出現。"); TABS.forEach((x) => { $(`#tab-${x}`).hidden = x !== "health"; }); await showHealth(); return; }
  $("#built").textContent = `資料至 ${META.latest}，更新於 ${META.built_at.slice(0, 16).replace("T", " ")}`;
  $("#nav-broker").hidden = !META.broker;
  const s = await get("stocks.json");
  STOCKS = s.rows;
  $("#stocklist").innerHTML = STOCKS.map((r) => `<option value="${esc(r[0])} ${esc(r[1])}">`).join("");
  $("#search").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("#q").value.trim(), code = v.split(/\s+/)[0];
    if (!v) return; // 選單選取（change）與 Enter 可能各送出一次，第二次是空字串
    const hit = STOCKS.find((r) => r[0] === code) || STOCKS.find((r) => r[1].includes(v));
    if (hit) { location.hash = `stock/${hit[0]}`; $("#q").value = ""; } else status(`找不到「${v}」`);
  });
  $("#q").addEventListener("change", () => $("#search").requestSubmit());
  window.addEventListener("hashchange", route);
  await route();
}
main();
