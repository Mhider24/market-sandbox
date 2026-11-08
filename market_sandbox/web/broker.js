import {createChart} from "./chart.js";

const byId = id => document.getElementById(id);
const chart = createChart(byId("broker-chart"), byId("broker-chart-empty"), byId("broker-chart-tooltip"));
const currency = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"});
const money = value => value == null ? "—" : currency.format(Number(value));
const stockSymbol = /^[A-Z][A-Z0-9.-]{0,11}$/;
const cancelable = new Set(["accepted", "new", "pending_new", "partially_filled", "held", "accepted_for_bidding"]);
let selected = "SPY";
let range = 63;
let bars = [];
let status = null;
let historyRequest;
let manualRequest;
let submitting = false;
let knownOrderId;
let userChoseMode = false;
let hasPendingOrders = false;

function note(id, text) {
  const node = byId(id);
  node.textContent = text;
  node.hidden = !text;
}

function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}

async function api(path, method = "GET", data, signal) {
  const response = await fetch("/alpaca/" + path, {
    method,
    headers: {"Content-Type": "application/json", "X-Market-Sandbox": "paper"},
    body: data === undefined ? undefined : JSON.stringify(data),
    signal,
  });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok) {
    const reason = Array.isArray(body.detail) ? body.detail[0]?.msg : body.detail;
    throw new Error(reason || "Alpaca Paper request failed.");
  }
  return body;
}

function tableRow(table, cells) {
  const row = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement("td");
    td.append(cell);
    row.append(td);
  }
  table.append(row);
}

function showReview(review) {
  const root = byId("bot-review-result");
  root.replaceChildren();
  if (!review) return;

  const proposal = review.proposal;
  let headline = "Hold";
  if (proposal?.side === "sell") headline = "Sell candidate · " + proposal.quantity + " shares of " + proposal.symbol;
  else if (proposal) headline = "Buy candidate · " + proposal.symbol + " " + money(proposal.amount_cents / 100);
  root.append(text("strong", headline));
  root.append(text("p", "Data through " + review.data_through));
  const list = document.createElement("ul");
  for (const candidate of review.candidates) {
    const metrics = candidate.next_log_return_bps == null ? "" :
      "Forecast " + candidate.next_log_return_bps.toFixed(1) + " bp · RMSE " +
      candidate.model_rmse_bps.toFixed(1) + " / " + candidate.baseline_rmse_bps.toFixed(1) + " bp. ";
    list.append(text("li", candidate.symbol + " · " + metrics + candidate.reason));
  }
  root.append(list);
}

function showWatchlist() {
  const root = byId("broker-watch");
  root.replaceChildren();
  for (const symbol of status?.settings.symbols ?? ["SPY", "QQQ"]) {
    const button = text("button", symbol);
    button.type = "button";
    button.setAttribute("aria-pressed", selected === symbol);
    button.addEventListener("click", () => loadChart(symbol));
    root.append(button);
  }
}

function showStatus(next) {
  const previous = status;
  status = next;
  note("bot-mode", status.armed ? "ARMED" : "PAUSED");
  note("bot-status", status.configured ? status.message : "Set the Alpaca Paper keys on the server to connect.");
  byId("broker-setup").hidden = status.configured;
  if (!status.configured) showConnection("connecting", "Alpaca keys missing");
  else if (!previous?.configured) showConnection("connecting", "Checking Alpaca");
  byId("bot-arm").disabled = !status.configured || status.armed;
  byId("bot-pause").disabled = !status.configured || !status.armed;
  byId("bot-review").disabled = !status.configured;
  byId("broker-submit").disabled = !status.configured || submitting;
  byId("broker-refresh").disabled = !status.configured;
  const fields = {
    "bot-symbols": status.settings.symbols.join(", "),
    "bot-budget": status.settings.max_order_cents / 100,
    "bot-position-cap": status.settings.position_cap_bps / 100,
    "bot-invested-cap": status.settings.invested_cap_bps / 100,
    "bot-stop-loss": status.settings.stop_loss_bps / 100,
    "bot-take-profit": status.settings.take_profit_bps / 100,
  };
  for (const [id, value] of Object.entries(fields)) {
    byId(id).disabled = status.armed;
    if (status.armed) byId(id).value = value;
  }
  byId("bot-exits").disabled = status.armed;
  if (status.armed) byId("bot-exits").checked = status.settings.manage_exits;
  updateExitFields();
  showReview(status.review);
  showWatchlist();
  const orderId = status.last_order?.id;
  if (orderId && orderId !== knownOrderId && document.body.dataset.mode === "alpaca") refreshAccount();
  knownOrderId = orderId;
}

function updateExitFields() {
  const enabled = byId("bot-exits").checked;
  byId("bot-exit-limits").hidden = !enabled;
  byId("bot-stop-loss").disabled = !enabled || Boolean(status?.armed);
  byId("bot-take-profit").disabled = !enabled || Boolean(status?.armed);
}

function showConnection(state, label) {
  byId("broker-indicator").dataset.state = state;
  note("broker-indicator-label", label);
}

async function refreshAudit() {
  if (!status?.configured || document.body.dataset.mode !== "alpaca") return;
  const list = byId("broker-audit-rows");
  try {
    const {events} = await api("audit");
    list.replaceChildren();
    note("broker-audit-message", events.length ? "" : "No events yet. Run a forecast to start the journal.");
    for (const entry of events) {
      const item = document.createElement("li");
      const time = text("time", new Date(entry.timestamp).toLocaleString());
      time.dateTime = entry.timestamp;
      item.append(time, text("span", entry.summary));
      const reason = entry.detail.proposal?.reason;
      if (reason) item.append(text("small", reason));
      list.append(item);
    }
  } catch (error) {
    note("broker-audit-message", error.message);
  }
}

async function refreshStatus() {
  try {
    showStatus(await api("status"));
  } catch (error) {
    showConnection("offline", "Alpaca status unavailable");
    note("bot-error", error.message);
  }
  await refreshAudit();
}

async function refreshAccount() {
  try {
    const account = await api("account");
    showConnection("ready", "Alpaca connected");
    note("broker-connection", "Alpaca Paper connected");
    note("broker-equity", money(account.equity));
    note("broker-cash", money(account.cash));
    note("broker-market", account.market_open ? "Regular session open" : "Regular session closed");

    const positions = byId("broker-position-rows");
    positions.replaceChildren();
    for (const position of account.positions) {
      tableRow(positions, [position.symbol, position.qty, money(position.market_value)]);
    }
    if (!positions.children.length) tableRow(positions, ["Cash only", "—", "—"]);

    const orders = byId("broker-order-rows");
    orders.replaceChildren();
    hasPendingOrders = account.orders.some(order => cancelable.has(order.status));
    for (const order of account.orders) {
      const action = text("span", "—");
      if (cancelable.has(order.status) && /^ms-(auto|manual)-/.test(order.client_order_id ?? "")) {
        const button = text("button", "Cancel");
        button.className = "button secondary";
        button.addEventListener("click", async () => {
          if (!window.confirm("Cancel this Alpaca paper order and pause automation?")) return;
          button.disabled = true;
          try {
            await api("orders/" + encodeURIComponent(order.id), "DELETE");
            await refreshStatus();
            await refreshAccount();
          } catch (error) {
            note("broker-order-message", error.message);
          } finally {
            button.disabled = false;
          }
        });
        action.replaceChildren(button);
      }
      const orderStatus = text("span", order.status.replaceAll("_", " "));
      if (order.filled_avg_price != null) {
        const fill = text("small", order.filled_qty + " @ " + money(order.filled_avg_price));
        fill.className = "order-fill";
        orderStatus.append(fill);
      }
      tableRow(orders, [order.side.toUpperCase() + " " + order.symbol, orderStatus, action]);
    }
    if (!orders.children.length) tableRow(orders, ["No orders", "—", "—"]);
  } catch (error) {
    showConnection("offline", "Alpaca unavailable");
    note("broker-connection", error.message);
  }
}

function renderChart() {
  chart(bars.slice(-range));
  const last = bars.at(-1);
  const previous = bars.at(-2);
  const change = previous && last ? (last.close / previous.close - 1) * 100 : null;
  note("broker-mark", money(last?.close));
  note("broker-change", change == null ? "—" : (change >= 0 ? "+" : "") + change.toFixed(2) + "%");
  byId("broker-change").className = change == null ? "muted" : change >= 0 ? "up" : "down";
  byId("broker-ranges").querySelectorAll("button").forEach(button => {
    button.setAttribute("aria-pressed", Number(button.dataset.range) === range);
  });
}

async function loadChart(symbol) {
  if (!stockSymbol.test(symbol)) {
    note("broker-order-message", "Enter a valid US stock symbol.");
    return;
  }
  selected = symbol;
  note("broker-chart-title", selected);
  byId("broker-order-symbol").value = selected;
  showWatchlist();
  historyRequest?.abort();
  bars = [];
  renderChart();
  note("broker-chart-empty", status?.configured ? "Loading Alpaca daily bars…" : "Connect Alpaca Paper to load a chart.");
  if (!status?.configured) return;

  const controller = new AbortController();
  historyRequest = controller;
  try {
    const result = await api("history/" + encodeURIComponent(symbol), "GET", undefined, controller.signal);
    if (historyRequest !== controller) return;
    bars = result.bars.map(bar => ({
      date: new Date(bar.t), open: bar.o, high: bar.h, low: bar.l, close: bar.c, volume: bar.v,
    }));
    note("broker-chart-empty", "No completed bars available for this symbol.");
    renderChart();
  } catch (error) {
    if (!controller.signal.aborted) note("broker-chart-empty", error.message);
  }
}

function setMode(mode) {
  const alpaca = mode === "alpaca";
  document.body.dataset.mode = mode;
  byId("mode-alpaca").setAttribute("aria-pressed", alpaca);
  byId("mode-sandbox").setAttribute("aria-pressed", !alpaca);
  byId("positions-link").href = alpaca ? "#broker-positions" : "#positions-panel";
  byId("ticket-link").href = alpaca ? "#broker-ticket" : "#ticket";
  if (alpaca) {
    if (status?.configured) {
      refreshAccount();
      refreshAudit();
    }
    loadChart(selected);
  }
}

function settings() {
  const manageExits = byId("bot-exits").checked;
  return {
    symbols: byId("bot-symbols").value.split(",").map(symbol => symbol.trim().toUpperCase()).filter(Boolean),
    max_order_cents: Math.round(byId("bot-budget").valueAsNumber * 100),
    position_cap_bps: Math.round(byId("bot-position-cap").valueAsNumber * 100),
    invested_cap_bps: Math.round(byId("bot-invested-cap").valueAsNumber * 100),
    manage_exits: manageExits,
    ...(manageExits && {
      stop_loss_bps: Math.round(byId("bot-stop-loss").valueAsNumber * 100),
      take_profit_bps: Math.round(byId("bot-take-profit").valueAsNumber * 100),
    }),
  };
}

async function botAction(action, payload) {
  note("bot-error", "");
  try {
    const result = await api(action, "POST", payload);
    if (action === "review") showReview(result);
    else showStatus(result);
  } catch (error) {
    note("bot-error", error.message);
  } finally {
    await refreshAudit();
  }
}

byId("mode-sandbox").addEventListener("click", () => { userChoseMode = true; setMode("sandbox"); });
byId("mode-alpaca").addEventListener("click", () => { userChoseMode = true; setMode("alpaca"); });
byId("broker-refresh").addEventListener("click", refreshAccount);
byId("broker-ranges").addEventListener("click", event => {
  const button = event.target.closest("button[data-range]");
  if (button) {
    range = Number(button.dataset.range);
    renderChart();
  }
});
document.addEventListener("alpaca-symbol", event => loadChart(event.detail));
byId("bot-form").addEventListener("submit", event => {
  event.preventDefault();
  botAction("review", settings());
});
byId("bot-arm").addEventListener("click", () => {
  if (!byId("bot-form").reportValidity()) return;
  const config = settings();
  const prompt = config.manage_exits
    ? "Arm Alpaca paper buys and allow the bot to sell watched long holdings, including shares bought manually?"
    : "Arm the bot to submit Alpaca paper buys?";
  if (window.confirm(prompt)) {
    botAction("arm", config);
  }
});
byId("bot-exits").addEventListener("change", updateExitFields);
byId("bot-pause").addEventListener("click", () => botAction("pause"));
byId("broker-side").addEventListener("change", () => {
  const sell = byId("broker-order-form").elements.side.value === "sell";
  const input = byId("broker-order-size");
  note("broker-order-unit", sell ? "Shares" : "Amount ($)");
  input.min = sell ? "0.000000001" : "1";
  input.step = sell ? "0.000000001" : "0.01";
  input.value = sell ? "1" : "10";
});
byId("broker-order-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (submitting) return;
  const side = byId("broker-order-form").elements.side.value;
  const symbol = byId("broker-order-symbol").value.trim().toUpperCase();
  const input = byId("broker-order-size");
  if (!stockSymbol.test(symbol)) {
    note("broker-order-message", "Enter a valid US stock symbol.");
    return;
  }
  const size = side === "buy" ? {amount_cents: Math.round(input.valueAsNumber * 100)} : {quantity: input.value};
  const details = side === "buy" ? money(size.amount_cents / 100) + " of " + symbol : input.value + " shares of " + symbol;
  if (!window.confirm("Submit an Alpaca paper " + side + " for " + details + "? This pauses automation.")) return;

  const order = {symbol, side, ...size};
  const fingerprint = JSON.stringify(order);
  if (manualRequest?.fingerprint !== fingerprint) manualRequest = {fingerprint, id: crypto.randomUUID()};
  const button = byId("broker-submit");
  submitting = true;
  button.disabled = true;
  note("broker-order-message", "Submitting to Alpaca Paper…");
  try {
    const result = await api("orders", "POST", {...order, request_id: manualRequest.id});
    manualRequest = null;
    note("broker-order-message", "Paper order " + result.status + " · " + result.symbol);
    await refreshAccount();
  } catch (error) {
    note("broker-order-message", error.message);
  } finally {
    await refreshStatus();
    submitting = false;
    button.disabled = !status?.configured;
  }
});

refreshStatus().then(() => {
  if (status?.configured && !userChoseMode) setMode("alpaca");
  else if (status?.configured && document.body.dataset.mode === "alpaca") setMode("alpaca");
});
setInterval(() => {
  if (document.body.dataset.mode === "alpaca") {
    refreshStatus();
    if (hasPendingOrders) refreshAccount();
  }
}, 15000);
