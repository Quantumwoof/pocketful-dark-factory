
const app = document.getElementById("app");
const headerEl = document.getElementById("top");
let token = localStorage.getItem("pocketful_token") || "";
let me = null;
let refreshGen = 0;
const memory = {};

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function money(minor, units, currency) {
  if (units === 0) return `${minor} ${currency}`;
  const text = String(minor).padStart(units + 1, "0");
  return `${text.slice(0, -units)}.${text.slice(-units)} ${currency}`;
}

function toMinor(raw, units) {
  const text = String(raw).trim();
  if (!/^\d+(\.\d+)?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  if (fraction.length > units) return null;
  const padded = fraction.padEnd(units, "0");
  const scale = units === 0 ? 1 : 10 ** units;
  return Number(whole) * scale + (padded ? Number(padded) : 0);
}

function minorToInput(minor, units) {
  if (units === 0) return String(minor);
  const text = String(minor).padStart(units + 1, "0");
  return `${text.slice(0, -units)}.${text.slice(-units)}`;
}

function equalSplit(amount, count) {
  const base = Math.floor(amount / count);
  const extra = amount - base * count;
  return Array.from({ length: count }, (_, index) => base + (index < extra ? 1 : 0));
}

async function callApi(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { method: options.method || "GET", headers, body });
  const text = await response.text();
  return { status: response.status, data: text ? JSON.parse(text) : null };
}

function setToken(value) {
  token = value || "";
  if (token) localStorage.setItem("pocketful_token", token);
  else localStorage.removeItem("pocketful_token");
}

async function loadMe() {
  if (!token) { me = null; return; }
  const result = await callApi("/me");
  if (result.status !== 200) { setToken(""); me = null; return; }
  me = result.data;
}

function paintHeader() {
  if (!me) {
    headerEl.innerHTML = `<a class="brand" href="/">Pocketful</a><nav><a href="/login">Log in</a><a href="/signup">Open a wallet</a></nav>`;
    return;
  }
  headerEl.innerHTML = `
    <a class="brand" href="/">Pocketful</a>
    <nav>
      <a href="/">Wallet</a>
      <a href="/requests">Requests</a>
      <a href="/split">Split</a>
      <a href="/authorizations">Holds</a>
    </nav>
    <div class="who">
      <strong data-testid="current-user">${escapeHtml(me.display_name)}</strong>
      <span data-testid="current-handle">${escapeHtml(me.handle)}</span>
      <button type="button" data-testid="logout-button" id="logout">Log out</button>
    </div>`;
  document.getElementById("logout").onclick = () => {
    setToken("");
    me = null;
    location.href = "/login";
  };
}

function showMessage(form, testid, text) {
  let node = form.querySelector(`[data-testid="${testid}"]`);
  if (!node) {
    node = document.createElement("p");
    node.dataset.testid = testid;
    node.className = testid.endsWith("uncertain") ? "uncertain" : "error";
    form.append(node);
  }
  node.textContent = text;
}

function clearMessage(form, testid) {
  form.querySelector(`[data-testid="${testid}"]`)?.remove();
}

function stableKey(slot, fingerprint) {
  if (memory[slot] !== fingerprint) {
    memory[slot] = fingerprint;
    memory[`${slot}:key`] = crypto.randomUUID();
  }
  return memory[`${slot}:key`];
}

function paintMoney() {
  const units = me.minor_units;
  const currency = me.currency;
  const total = me.total ?? me.balance;
  const available = me.available ?? me.balance;
  const held = me.held || 0;
  const balance = document.querySelector("[data-testid='wallet-balance']");
  balance.textContent = money(total, units, currency);
  balance.dataset.amount = String(total);
  const avail = document.querySelector("[data-testid='wallet-available']");
  if (avail) {
    avail.textContent = money(available, units, currency);
    avail.dataset.amount = String(available);
  }
  const existing = document.querySelector("[data-testid='wallet-held']");
  if (held === 0) {
    existing?.remove();
  } else if (existing) {
    existing.textContent = money(held, units, currency);
    existing.dataset.amount = String(held);
  } else {
    const node = document.createElement("p");
    node.dataset.testid = "wallet-held";
    node.dataset.amount = String(held);
    node.textContent = money(held, units, currency);
    document.getElementById("held-slot").append(node);
  }
}

async function refreshHome() {
  const generation = ++refreshGen;
  const [profile, feed] = await Promise.all([callApi("/me"), callApi("/activity?limit=50")]);
  if (generation !== refreshGen || profile.status !== 200) return;
  me = profile.data;
  paintHeader();
  paintMoney();
  paintFeed(feed.data.payments || []);
}

function paintFeed(payments) {
  const feed = document.getElementById("feed");
  feed.replaceChildren();
  if (!payments.length) {
    const empty = document.createElement("p");
    empty.dataset.testid = "empty-activity";
    empty.textContent = "Nothing here yet. A payment you send, receive, or that is public will show up.";
    feed.append(empty);
    return;
  }
  const list = document.createElement("div");
  list.dataset.testid = "activity-list";
  for (const payment of payments) {
    const item = document.createElement("article");
    item.dataset.testid = `activity-item-${payment.payment_id}`;
    item.dataset.visibility = payment.visibility;
    const parties = document.createElement("p");
    parties.dataset.testid = `activity-parties-${payment.payment_id}`;
    parties.textContent = `${payment.from_handle} to ${payment.to_handle}`;
    const amount = document.createElement("p");
    amount.dataset.testid = `activity-amount-${payment.payment_id}`;
    amount.textContent = money(payment.amount, me.minor_units, me.currency);
    const note = document.createElement("p");
    note.dataset.testid = `activity-note-${payment.payment_id}`;
    note.textContent = payment.note;
    item.append(parties, amount, note);
    list.append(item);
  }
  feed.append(list);
}

function renderHome() {
  app.innerHTML = `
    <section class="hero">
      <p class="eyebrow">Available to spend</p>
      <p class="money" data-testid="wallet-available"></p>
      <p class="secondary">Total <span data-testid="wallet-balance"></span></p>
      <p class="secondary" id="held-slot"></p>
      <button type="button" data-testid="wallet-refresh" id="refresh">Refresh</button>
    </section>
    <div class="grid">
      <form id="pay" class="card">
        <h2>Pay</h2>
        <label>Their handle <input id="pay-handle" data-testid="pay-handle" autocomplete="off"></label>
        <label>Amount <input id="pay-amount" data-testid="pay-amount" inputmode="decimal" autocomplete="off"></label>
        <label>Note <input id="pay-note" data-testid="pay-note" autocomplete="off"></label>
        <label>Who can see it
          <select id="pay-visibility" data-testid="pay-visibility">
            <option value="public">public</option>
            <option value="private">private</option>
          </select>
        </label>
        <button data-testid="pay-submit" type="submit">Send money</button>
      </form>
      <form id="ask" class="card">
        <h2>Request</h2>
        <label>Their handle <input id="request-handle" data-testid="request-handle" autocomplete="off"></label>
        <label>Amount <input id="request-amount" data-testid="request-amount" inputmode="decimal" autocomplete="off"></label>
        <label>Note <input id="request-note" data-testid="request-note" autocomplete="off"></label>
        <button data-testid="request-submit" type="submit">Ask</button>
      </form>
      <form id="hold" class="card">
        <h2>Reserve</h2>
        <label>Their handle <input id="authorize-handle" data-testid="authorize-handle" autocomplete="off"></label>
        <label>Amount <input id="authorize-amount" data-testid="authorize-amount" inputmode="decimal" autocomplete="off"></label>
        <label>Note <input id="authorize-note" data-testid="authorize-note" autocomplete="off"></label>
        <label>Who can see the capture
          <select id="authorize-visibility" data-testid="authorize-visibility">
            <option value="public">public</option>
            <option value="private">private</option>
          </select>
        </label>
        <button data-testid="authorize-submit" type="submit">Place hold</button>
      </form>
    </div>
    <section><h2>Activity</h2><div id="feed"></div></section>`;
  document.getElementById("refresh").onclick = () => refreshHome();
  document.getElementById("pay").onsubmit = (event) => submitPay(event);
  document.getElementById("ask").onsubmit = (event) => submitAsk(event);
  document.getElementById("hold").onsubmit = (event) => submitHold(event);
  refreshHome();
}

async function submitPay(event) {
  event.preventDefault();
  const form = event.target;
  clearMessage(form, "pay-error");
  clearMessage(form, "pay-uncertain");
  const handle = document.getElementById("pay-handle").value.trim();
  const amountText = document.getElementById("pay-amount").value;
  const note = document.getElementById("pay-note").value;
  const visibility = document.getElementById("pay-visibility").value;
  const amount = toMinor(amountText, me.minor_units);
  if (amount == null) {
    showMessage(form, "pay-error", "Use a number with no more decimal places than this currency allows.");
    return;
  }
  const key = stableKey("pay", JSON.stringify({ handle, amountText, note, visibility }));
  try {
    const result = await callApi("/payments", {
      method: "POST",
      body: { to_handle: handle, amount, note, visibility },
      headers: { "Idempotency-Key": key },
    });
    if (result.status === 201 || result.status === 200) {
      clearMessage(form, "pay-error");
      clearMessage(form, "pay-uncertain");
      await refreshHome();
    } else {
      showMessage(form, "pay-error", result.data?.error?.message || "Payment refused");
      await refreshHome();
    }
  } catch (error) {
    showMessage(form, "pay-uncertain", "We did not get a confirmation. Send again with the same details and the money will move only once.");
  }
}

async function submitAsk(event) {
  event.preventDefault();
  const form = event.target;
  clearMessage(form, "request-error");
  const handle = document.getElementById("request-handle").value.trim();
  const amountText = document.getElementById("request-amount").value;
  const note = document.getElementById("request-note").value;
  const amount = toMinor(amountText, me.minor_units);
  if (amount == null) {
    showMessage(form, "request-error", "Check the amount.");
    return;
  }
  const key = stableKey("ask", JSON.stringify({ handle, amountText, note }));
  const result = await callApi("/requests", {
    method: "POST",
    body: { payer_handle: handle, amount, note },
    headers: { "Idempotency-Key": key },
  });
  if (result.status === 201 || result.status === 200) clearMessage(form, "request-error");
  else showMessage(form, "request-error", result.data?.error?.message || "Request refused");
}

async function submitHold(event) {
  event.preventDefault();
  const form = event.target;
  clearMessage(form, "authorize-error");
  const handle = document.getElementById("authorize-handle").value.trim();
  const amountText = document.getElementById("authorize-amount").value;
  const note = document.getElementById("authorize-note").value;
  const visibility = document.getElementById("authorize-visibility").value;
  const amount = toMinor(amountText, me.minor_units);
  if (amount == null) {
    showMessage(form, "authorize-error", "Check the amount.");
    return;
  }
  const key = stableKey("hold", JSON.stringify({ handle, amountText, note, visibility }));
  const result = await callApi("/authorizations", {
    method: "POST",
    body: { to_handle: handle, amount, note, visibility },
    headers: { "Idempotency-Key": key },
  });
  if (result.status === 201 || result.status === 200) {
    clearMessage(form, "authorize-error");
    await refreshHome();
  } else showMessage(form, "authorize-error", result.data?.error?.message || "Hold refused");
}

function renderAuthForm(kind) {
  const signup = kind === "signup";
  app.innerHTML = `
    <form id="auth" class="card narrow">
      <h1>${signup ? "Open a wallet" : "Welcome back"}</h1>
      <label>Email <input id="email" data-testid="${signup ? "signup-email" : "login-email"}" autocomplete="username"></label>
      <label>Password <input id="password" type="password" data-testid="${signup ? "signup-password" : "login-password"}" autocomplete="${signup ? "new-password" : "current-password"}"></label>
      ${signup ? '<label>Name <input id="name" data-testid="signup-display-name" autocomplete="name"></label>' : ""}
      <button type="submit" data-testid="${signup ? "signup-submit" : "login-submit"}">${signup ? "Create wallet" : "Log in"}</button>
    </form>`;
  document.getElementById("auth").onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    clearMessage(form, "auth-error");
    const email = document.getElementById("email").value;
    const password = document.getElementById("password").value;
    const result = signup
      ? await callApi("/auth/signup", { method: "POST", body: { email, password, display_name: document.getElementById("name").value } })
      : await callApi("/auth/login", { method: "POST", body: { email, password } });
    if (result.status === 201 || result.status === 200) {
      setToken(result.data.token);
      location.href = "/";
    } else showMessage(form, "auth-error", result.data?.error?.message || "That did not work");
  };
}

async function renderRequests() {
  app.innerHTML = `<section class="card"><h1>Requests</h1><div id="request-root">Loading…</div></section>`;
  await paintRequests();
}

async function paintRequests() {
  const result = await callApi("/requests?limit=200");
  const rows = result.data?.requests || [];
  const incoming = rows.filter((row) => row.payer_handle === me.handle);
  const outgoing = rows.filter((row) => row.requester_handle === me.handle);
  const root = document.getElementById("request-root");
  root.replaceChildren();
  if (!incoming.length && !outgoing.length) {
    const empty = document.createElement("p");
    empty.dataset.testid = "empty-requests";
    empty.textContent = "No requests yet.";
    root.append(empty);
  }
  root.append(requestList("Incoming", "incoming-list", incoming, true));
  root.append(requestList("Outgoing", "outgoing-list", outgoing, false));
}

function requestList(title, testid, rows, incoming) {
  const wrap = document.createElement("section");
  const heading = document.createElement("h2");
  heading.textContent = title;
  const list = document.createElement("div");
  list.dataset.testid = testid;
  for (const row of rows) {
    const item = document.createElement("article");
    item.dataset.testid = `request-item-${row.request_id}`;
    item.dataset.status = row.status;
    const amount = document.createElement("p");
    amount.dataset.testid = `request-amount-${row.request_id}`;
    amount.textContent = money(row.amount, me.minor_units, me.currency);
    const who = document.createElement("p");
    who.textContent = incoming ? `From ${row.requester_handle}` : `To ${row.payer_handle}`;
    item.append(who, amount);
    if (row.status === "pending" && incoming) {
      item.append(actionButton(`request-pay-${row.request_id}`, "Pay", () => actRequest(row.request_id, "pay")));
      item.append(actionButton(`request-decline-${row.request_id}`, "Decline", () => actRequest(row.request_id, "decline")));
    }
    if (row.status === "pending" && !incoming) {
      item.append(actionButton(`request-cancel-${row.request_id}`, "Cancel", () => actRequest(row.request_id, "cancel")));
    }
    list.append(item);
  }
  wrap.append(heading, list);
  return wrap;
}

function actionButton(testid, label, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.testid = testid;
  button.textContent = label;
  button.onclick = onClick;
  return button;
}

async function actRequest(id, action) {
  const root = document.getElementById("request-root");
  clearMessage(root, "request-error");
  const options = { method: "POST" };
  if (action === "pay") options.headers = { "Idempotency-Key": crypto.randomUUID() };
  if (action === "pay") options.body = {};
  const result = await callApi(`/requests/${id}/${action}`, options);
  if (result.status !== 200 && result.status !== 201) {
    showMessage(root, "request-error", result.data?.error?.message || "That did not work");
  }
  await paintRequests();
}

function renderSplit() {
  app.innerHTML = `
    <form id="split" class="card narrow">
      <h1>Split a bill</h1>
      <label>Amount <input id="split-amount" data-testid="split-amount" inputmode="decimal"></label>
      <label>Handles, in order <input id="split-handles" data-testid="split-handles" placeholder="ada, bob, cy"></label>
      <label>Note <input id="split-note" data-testid="split-note"></label>
      <div id="split-preview" data-testid="split-preview" hidden></div>
      <button data-testid="split-submit" type="submit">Create requests</button>
    </form>`;
  const form = document.getElementById("split");
  const update = () => paintSplitPreview();
  form.addEventListener("input", update);
  form.onsubmit = async (event) => {
    event.preventDefault();
    clearMessage(form, "split-error");
    const parsed = splitFields();
    if (!parsed) {
      showMessage(form, "split-error", "Check the amount and the handles.");
      return;
    }
    const key = stableKey("split", JSON.stringify(parsed));
    const result = await callApi("/splits", {
      method: "POST",
      body: { amount: parsed.amount, participant_handles: parsed.handles, note: parsed.note },
      headers: { "Idempotency-Key": key },
    });
    if (result.status === 201 || result.status === 200) clearMessage(form, "split-error");
    else showMessage(form, "split-error", result.data?.error?.message || "Split refused");
  };
}

function splitFields() {
  const amount = toMinor(document.getElementById("split-amount").value, me.minor_units);
  const handles = document.getElementById("split-handles").value.split(",").map((part) => part.trim()).filter(Boolean);
  const note = document.getElementById("split-note").value;
  if (amount == null || !handles.length) return null;
  return { amount, handles, note };
}

function paintSplitPreview() {
  const preview = document.getElementById("split-preview");
  const parsed = splitFields();
  preview.replaceChildren();
  if (!parsed) { preview.hidden = true; return; }
  preview.hidden = false;
  const shares = equalSplit(parsed.amount, parsed.handles.length);
  parsed.handles.forEach((handle, index) => {
    const row = document.createElement("p");
    row.dataset.testid = `split-share-${handle}`;
    row.textContent = money(shares[index], me.minor_units, me.currency);
    preview.append(row);
  });
}

async function renderAuthorizations() {
  app.innerHTML = `<section class="card"><h1>Holds</h1><div id="auth-root">Loading…</div></section>`;
  const result = await callApi("/authorizations?limit=200");
  const rows = result.data?.authorizations || [];
  const root = document.getElementById("auth-root");
  root.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.dataset.testid = "empty-authorizations";
    empty.textContent = "No holds.";
    root.append(empty);
  }
  const list = document.createElement("div");
  list.dataset.testid = "authorization-list";
  for (const row of rows) {
    const item = document.createElement("article");
    item.dataset.testid = `authorization-item-${row.authorization_id}`;
    item.dataset.status = row.status;
    const amount = document.createElement("p");
    amount.dataset.testid = `authorization-amount-${row.authorization_id}`;
    amount.textContent = money(row.amount, me.minor_units, me.currency);
    const expires = document.createElement("p");
    expires.dataset.testid = `authorization-expires-${row.authorization_id}`;
    expires.textContent = row.expires_at;
    item.append(amount, expires);
    if (row.status === "captured") {
      const captured = document.createElement("p");
      captured.dataset.testid = `authorization-captured-${row.authorization_id}`;
      captured.textContent = money(row.captured_amount, me.minor_units, me.currency);
      item.append(captured);
    }
    const incoming = row.to_handle === me.handle;
    const outgoing = row.from_handle === me.handle;
    if (row.status === "open" && incoming) {
      const input = document.createElement("input");
      input.dataset.testid = `authorization-capture-amount-${row.authorization_id}`;
      input.value = minorToInput(row.remaining_amount, me.minor_units);
      item.append(input);
      item.append(actionButton(`authorization-capture-${row.authorization_id}`, "Capture", () => capture(row.authorization_id, input.value)));
    }
    if (row.status === "open" && outgoing) {
      item.append(actionButton(`authorization-void-${row.authorization_id}`, "Void", () => voidHold(row.authorization_id)));
    }
    list.append(item);
  }
  root.append(list);
}

async function capture(id, text) {
  const root = document.getElementById("auth-root");
  const amount = toMinor(text, me.minor_units);
  if (amount == null) { showMessage(root, "authorization-error", "Check the amount."); return; }
  const result = await callApi(`/authorizations/${id}/capture`, {
    method: "POST",
    body: { amount },
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
  if (result.status !== 201 && result.status !== 200) showMessage(root, "authorization-error", result.data?.error?.message || "Capture refused");
  await renderAuthorizations();
}

async function voidHold(id) {
  const result = await callApi(`/authorizations/${id}/void`, { method: "POST" });
  const root = document.getElementById("auth-root");
  if (result.status !== 200) showMessage(root, "authorization-error", result.data?.error?.message || "Void refused");
  await renderAuthorizations();
}

async function boot() {
  await loadMe();
  paintHeader();
  const path = location.pathname;
  if (path === "/signup") return renderAuthForm("signup");
  if (path === "/login") return renderAuthForm("login");
  if (!me) { location.href = "/login"; return; }
  if (path === "/requests") return renderRequests();
  if (path === "/split") return renderSplit();
  if (path === "/authorizations") return renderAuthorizations();
  return renderHome();
}

boot();
