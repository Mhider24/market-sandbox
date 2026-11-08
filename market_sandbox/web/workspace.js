import {createChart, demoBars} from "./chart.js";

const demoQuotes = {
  QQQ: {name: "Invesco QQQ Trust", seed: 3084, change: .0074},
  SPY: {name: "SPDR S&P 500 ETF Trust", seed: 4886, change: -.0036},
  IWM: {name: "iShares Russell 2000 ETF", seed: 7725, change: .0052},
  DIA: {name: "SPDR Dow Jones Industrial Average ETF", seed: 5201, change: -.0018},
  TLT: {name: "iShares 20+ Year Treasury Bond ETF", seed: 4170, change: .0024},
  GLD: {name: "SPDR Gold Shares", seed: 8295, change: .0091},
};

const state = {
  portfolio: {
    as_of_session: "2026-11-06",
    cash_cents: 630000,
    max_stock_bps: 2000,
    max_sector_bps: 4000,
    assets: [
      {symbol: "QQQ", sector: "equity-etf", shares: "6", close_cents: 45000},
      {symbol: "SPY", sector: "equity-etf", shares: "2", close_cents: 50000},
      {symbol: "IWM", sector: "equity-etf", shares: "0", close_cents: 22235},
      {symbol: "DIA", sector: "equity-etf", shares: "0", close_cents: 42176},
      {symbol: "TLT", sector: "treasuries", shares: "0", close_cents: 9154},
      {symbol: "GLD", sector: "gold", shares: "0", close_cents: 30612},
    ],
  },
  selected: "QQQ",
  range: 63,
  exposure: "positions",
  report: null,
  pending: null,
  editorRequest: null,
};

const byId = id => document.getElementById(id);
const display = (id, value) => { byId(id).textContent = value; };
const currency = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"});
const sessionDate = new Intl.DateTimeFormat("en-US", {
  month: "short", day: "numeric", year: "numeric", timeZone: "UTC",
});
const dollars = cents => cents == null ? "—" : currency.format(cents / 100);
const percent = bps => bps == null ? "—" : (bps / 100).toFixed(2) + "%";
const asCents = input => Math.round(input.valueAsNumber * 100);
const chart = createChart(byId("market-chart"), byId("chart-empty"), byId("chart-tooltip"));
const editor = byId("portfolio-editor");

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function message(id, value) {
  const node = byId(id);
  node.hidden = !value;
  node.textContent = value;
}

function connection(status) {
  const node = byId("connection");
  node.dataset.state = status;
  node.querySelector("span").textContent = status === "ready" ? "API connected" : "API unavailable";
}

function changeText(change) {
  return change == null ? "—" : (change >= 0 ? "+" : "") + (change * 100).toFixed(2) + "%";
}

function changeClass(change) {
  return change == null ? "muted" : change >= 0 ? "up" : "down";
}

function selectedAsset() {
  return state.portfolio.assets.find(asset => asset.symbol === state.selected);
}

function renderWatchlist() {
  const list = byId("watchlist");
  const options = byId("symbol-options");
  list.replaceChildren();
  options.replaceChildren();
  for (const asset of state.portfolio.assets) {
    const quote = Object.hasOwn(demoQuotes, asset.symbol) ? demoQuotes[asset.symbol] : null;
    const row = element("tr");
    row.dataset.symbol = asset.symbol;
    const symbol = element("td");
    const button = element("button", "", asset.symbol);
    button.type = "button";
    button.dataset.symbol = asset.symbol;
    symbol.append(button);
    row.append(symbol, element("td", "", dollars(asset.close_cents)),
      element("td", changeClass(quote?.change), changeText(quote?.change)));
    list.append(row);

    const option = element("option");
    option.value = asset.symbol;
    options.append(option);
  }

  if (!list.children.length) {
    const row = element("tr");
    const cell = element("td", "muted", "Add a symbol in Edit portfolio.");
    cell.colSpan = 3;
    row.append(cell);
    list.append(row);
  }
  display("watch-count", state.portfolio.assets.length + " SYMBOLS");
}

function renderChart() {
  const asset = selectedAsset();
  const quote = asset && Object.hasOwn(demoQuotes, asset.symbol) ? demoQuotes[asset.symbol] : null;
  display("chart-title", asset?.symbol ?? "—");
  display("instrument-name", quote?.name ?? (asset ? "Custom paper symbol" : "Select a watchlist symbol"));
  display("quote-mark", dollars(asset?.close_cents));
  display("quote-change", changeText(quote?.change));
  byId("quote-change").className = changeClass(quote?.change);
  display("ticket-symbol", asset?.symbol ?? "—");
  display("ticket-mark", asset ? "Mark " + dollars(asset.close_cents) : "");

  byId("watchlist").querySelectorAll("tr[data-symbol]").forEach(row => {
    const selected = row.dataset.symbol === state.selected;
    row.dataset.selected = selected;
    row.querySelector("button").setAttribute("aria-pressed", selected);
  });
  byId("chart-ranges").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", Number(button.dataset.range) === state.range);
  });
  chart(demoBars(asset, quote, state.portfolio.as_of_session).slice(-state.range));
}

function renderAccount() {
  const current = state.report?.current;
  display("session-label", sessionDate.format(new Date(state.portfolio.as_of_session + "T00:00:00Z")));
  display("account-value", dollars(current?.total_cents));
  display("cash-value", dollars(current?.cash_cents));
  display("invested-value", dollars(current ? current.total_cents - current.cash_cents : null));
  display("cash-weight", percent(current?.cash_weight_bps));
  display("limits-label",
    "Position " + percent(state.portfolio.max_stock_bps) + " · Group " + percent(state.portfolio.max_sector_bps));
}

function renderExposure() {
  const rows = byId("exposure-rows");
  rows.replaceChildren();
  byId("exposure-tabs").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", button.dataset.exposure === state.exposure);
  });
  display("exposure-column", state.exposure === "positions" ? "Symbol" : "Group");
  const items = state.report?.current[state.exposure].filter(row => row.market_value_cents > 0) ?? [];
  for (const item of items) {
    const row = element("tr");
    const name = element("td");
    name.append(element("strong", "", item.symbol ?? item.sector));
    if (item.over_limit) name.append(element("small", "", percent(item.excess_bps) + " over cap"));
    row.append(name, element("td", "", dollars(item.market_value_cents)),
      element("td", item.over_limit ? "down" : "", percent(item.weight_bps)));
    rows.append(row);
  }
  if (!items.length) {
    const row = element("tr");
    const cell = element("td", "muted", state.report ? "No invested exposure." : "Waiting for portfolio report…");
    cell.colSpan = 3;
    row.append(cell);
    rows.append(row);
  }
}

function riskMessage(className, title, description) {
  const box = element("div", "risk-message " + className);
  box.append(element("strong", "", title), element("p", "", description));
  return box;
}

function riskMeter(label, before, after) {
  const meter = element("div", "risk-meter");
  const heading = element("div");
  heading.append(element("span", "", label),
    element("strong", "", percent(before.weight_bps) + " → " + percent(after.weight_bps)));
  const track = element("div", "meter-track");
  track.setAttribute("aria-hidden", "true");
  const fill = element("i", after.over_limit ? "meter-fill over" : "meter-fill");
  fill.style.width = ((after.weight_bps ?? 0) / 100) + "%";
  const limit = element("i", "meter-limit");
  limit.style.left = "min(" + (after.limit_bps / 100) + "%, calc(100% - 2px))";
  track.append(fill, limit);
  meter.append(heading, track, element("small", "",
    "Cap " + percent(after.limit_bps) + (after.over_limit ? " · " + percent(after.excess_bps) + " over" : "")));
  return meter;
}

function renderRisk() {
  const panel = byId("risk-result");
  panel.replaceChildren();
  const pending = state.pending !== null;
  const button = byId("preview-button");
  button.disabled = pending || !state.selected;
  button.firstChild.textContent = pending ? "Checking… " : "Preview buy ";
  panel.setAttribute("aria-busy", pending);

  if (pending) {
    panel.append(riskMessage("", "Checking paper buy", "Calculating cash and exposure limits."));
    return;
  }

  const preview = state.report?.preview;
  if (!preview) {
    panel.append(riskMessage("", "Preview a paper buy", "Choose a symbol and enter an order value to compare before and after."));
    return;
  }

  const copy = {
    within_limits: ["success", "Within limits"],
    exceeds_limits: ["danger", "Exceeds limits"],
    insufficient_cash: ["danger", "Insufficient cash"],
  };
  const [className, status] = copy[preview.status];
  panel.append(riskMessage(className, status, "BUY " + preview.symbol + " · " + dollars(preview.amount_cents)));
  const current = state.report.current;
  const position = current.positions.find(row => row.symbol === preview.symbol);
  if (preview.projected) {
    const projected = preview.projected;
    panel.append(
      riskMeter(preview.symbol + " weight", position, projected.positions.find(row => row.symbol === preview.symbol)),
      riskMeter(position.sector + " group", current.sectors.find(row => row.sector === position.sector),
        projected.sectors.find(row => row.sector === position.sector)),
    );
  }
  const room = element("div", "risk-cash", "Room for this buy");
  room.append(element("strong", "", dollars(preview.max_buy_cents)));
  panel.append(room);
}

function renderReport() {
  renderAccount();
  renderExposure();
  renderRisk();
}

async function requestRisk(payload, signal) {
  let response;
  try {
    response = await fetch("/risk", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
      signal,
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    connection("offline");
    throw new Error("Could not reach the risk API. Check that the server is running.");
  }
  connection("ready");
  if (!response.ok) {
    const detail = (await response.json().catch(() => null))?.detail;
    const error = Array.isArray(detail) ? detail[0]?.msg : detail;
    throw new Error(typeof error === "string" ? error : "The risk API returned HTTP " + response.status + ".");
  }
  return response.json();
}

function invalidatePreview() {
  state.pending?.abort();
  state.pending = null;
  if (state.report) state.report = {...state.report, preview: null};
  message("feedback", "");
  renderRisk();
}

async function runPreview() {
  if (!state.selected || !byId("ticket-form").reportValidity()) return;
  state.pending?.abort();
  const controller = new AbortController();
  state.pending = controller;
  message("feedback", "");
  renderRisk();
  try {
    const report = await requestRisk({
      ...state.portfolio,
      paper_buy: {symbol: state.selected, amount_cents: asCents(byId("trade-amount"))},
    }, controller.signal);
    if (state.pending === controller) state.report = report;
  } catch (error) {
    if (state.pending === controller && error.name !== "AbortError") {
      if (state.report) state.report = {...state.report, preview: null};
      message("feedback", error.message);
    }
  } finally {
    if (state.pending === controller) {
      state.pending = null;
      renderReport();
    }
  }
}

function selectSymbol(symbol) {
  if (!state.portfolio.assets.some(asset => asset.symbol === symbol)) return false;
  if (state.selected !== symbol) {
    state.selected = symbol;
    invalidatePreview();
    renderChart();
  }
  return true;
}

function appendEditorAsset(asset = {symbol: "", sector: "", shares: "0", close_cents: null}) {
  const row = byId("asset-template").content.firstElementChild.cloneNode(true);
  row.querySelector('[name="symbol"]').value = asset.symbol;
  row.querySelector('[name="sector"]').value = asset.sector;
  row.querySelector('[name="shares"]').value = asset.shares;
  row.querySelector('[name="close"]').value = asset.close_cents == null ? "" : (asset.close_cents / 100).toFixed(2);
  row.querySelector("[data-remove]").addEventListener("click", () => {
    row.remove();
    byId("add-asset").disabled = false;
  });
  byId("editor-assets").append(row);
  byId("add-asset").disabled = byId("editor-assets").children.length >= 20;
  return row;
}

function showEditor() {
  byId("edit-cash").value = (state.portfolio.cash_cents / 100).toFixed(2);
  byId("edit-position-cap").value = state.portfolio.max_stock_bps / 100;
  byId("edit-group-cap").value = state.portfolio.max_sector_bps / 100;
  byId("edit-date").value = state.portfolio.as_of_session;
  byId("editor-assets").replaceChildren();
  byId("add-asset").disabled = false;
  state.portfolio.assets.forEach(appendEditorAsset);
  message("editor-error", "");
  editor.showModal();
}

async function savePortfolio(event) {
  event.preventDefault();
  const assets = Array.from(byId("editor-assets").children, row => ({
    symbol: row.querySelector('[name="symbol"]').value.trim().toUpperCase(),
    sector: row.querySelector('[name="sector"]').value.trim().toLowerCase(),
    shares: row.querySelector('[name="shares"]').value,
    close_cents: asCents(row.querySelector('[name="close"]')),
  }));
  const portfolio = {
    as_of_session: byId("edit-date").value,
    cash_cents: asCents(byId("edit-cash")),
    max_stock_bps: asCents(byId("edit-position-cap")),
    max_sector_bps: asCents(byId("edit-group-cap")),
    assets,
  };
  const controller = new AbortController();
  state.editorRequest = controller;
  byId("save-portfolio").disabled = true;
  message("editor-error", "");
  try {
    const report = await requestRisk(portfolio, controller.signal);
    if (state.editorRequest !== controller) return;
    invalidatePreview();
    state.portfolio = portfolio;
    state.report = report;
    if (!selectedAsset()) state.selected = assets[0]?.symbol ?? null;
    renderWatchlist();
    renderChart();
    renderReport();
    editor.close();
  } catch (error) {
    if (error.name !== "AbortError") message("editor-error", error.message);
  } finally {
    if (state.editorRequest === controller) {
      state.editorRequest = null;
      byId("save-portfolio").disabled = false;
    }
  }
}

byId("watchlist").addEventListener("click", event => {
  const button = event.target.closest("button[data-symbol]");
  if (button) selectSymbol(button.dataset.symbol);
});
byId("symbol-search").addEventListener("submit", event => {
  event.preventDefault();
  const query = byId("symbol-query");
  if (document.body.dataset.mode === "alpaca") {
    document.dispatchEvent(new CustomEvent("alpaca-symbol", {detail: query.value.trim().toUpperCase()}));
    query.value = "";
    return;
  }
  if (selectSymbol(query.value.trim().toUpperCase())) {
    query.value = "";
    message("feedback", "");
  } else {
    message("feedback", "That symbol is outside this watchlist. Add it in Edit portfolio.");
  }
});
byId("chart-ranges").addEventListener("click", event => {
  const button = event.target.closest("button[data-range]");
  if (button) {
    state.range = Number(button.dataset.range);
    renderChart();
  }
});
byId("exposure-tabs").addEventListener("click", event => {
  const button = event.target.closest("button[data-exposure]");
  if (button) {
    state.exposure = button.dataset.exposure;
    renderExposure();
  }
});
byId("ticket-form").addEventListener("submit", event => {
  event.preventDefault();
  runPreview();
});
byId("trade-amount").addEventListener("input", invalidatePreview);
byId("edit-portfolio").addEventListener("click", showEditor);
byId("close-editor").addEventListener("click", () => editor.close());
byId("cancel-editor").addEventListener("click", () => editor.close());
editor.addEventListener("close", () => {
  state.editorRequest?.abort();
  state.editorRequest = null;
  byId("save-portfolio").disabled = false;
});
byId("add-asset").addEventListener("click", () => appendEditorAsset().querySelector("input").focus());
byId("portfolio-form").addEventListener("submit", savePortfolio);

renderWatchlist();
renderChart();
renderReport();
runPreview();
