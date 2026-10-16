"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const state = { status: null, settings: null, articles: [], total: 0, offset: 0, limit: 50, csrf: "", online: false, refreshing: false, articlesBusy: false, busy: false, settingsDirty: false, telegramDirty: false, renderedArticleKey: "", view: "news", filters: { search: "", ticker: "", source: "" } };
  const number = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const text = (value, fallback = "") => typeof value === "string" || typeof value === "number" ? String(value) : fallback;
  const count = (value) => number(value).toLocaleString();
  const nodes = (tag, className, content) => { const element = document.createElement(tag); if (className) element.className = className; if (content !== undefined) element.textContent = text(content); return element; };
  const replace = (element, children) => element.replaceChildren(...children);
  const array = (value) => Array.isArray(value) ? value : [];
  const symbol = (company) => typeof company === "string" ? company : text(company?.symbol || company?.ticker);
  const safeURL = (value) => { try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; } };
  const link = (label, address, className) => { const url = safeURL(address); const node = nodes(url ? "a" : "span", className, label); if (url) { node.href = url; node.target = "_blank"; node.rel = "noopener noreferrer"; } return node; };
  const absoluteDate = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? "Not available" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); };
  const relativeDate = (value) => { if (!value) return "Not checked yet"; const date = new Date(value); if (Number.isNaN(date.getTime())) return "Not available"; const minutes = Math.floor((Date.now() - date.getTime()) / 60000); if (minutes < 0) return absoluteDate(value); if (minutes < 1) return "Just now"; if (minutes < 60) return `${minutes}m ago`; const hours = Math.floor(minutes / 60); if (hours < 24) return `${hours}h ago`; const days = Math.floor(hours / 24); return days < 7 ? `${days}d ago` : date.toLocaleDateString(undefined, { month: "short", day: "numeric" }); };

  function showNotice(message, kind = "") { const notice = byId("notice"); notice.textContent = message; notice.className = `notice${kind ? ` ${kind}` : ""}`; notice.hidden = !message; }
  function setConnection(online) { state.online = online; const node = byId("connection"); node.className = `connection ${online ? "online" : "offline"}`; replace(node, [nodes("span", "status-dot"), document.createTextNode(online ? (state.status?.running ? "Collector running" : "Collector stopped") : "Collector offline")]); }
  function offline(error) { if (error && !error.network && state.status) { showNotice(error.message, "error"); return; } setConnection(false); showNotice(error?.message || "Cannot reach your collector. Start it on this machine, then refresh this page.", "error"); if (!state.status) { state.renderedArticleKey = ""; renderEmpty("Your collector is offline", "Start TechBrief on this machine to collect news and connect your dashboard."); } }

  async function api(path, options = {}) {
    const method = options.method || "GET";
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 20000);
    try {
      const headers = { Accept: "application/json" };
      if (method !== "GET") { if (!state.csrf) throw new Error("Refresh the page to reconnect before making changes."); headers["Content-Type"] = "application/json"; headers["X-CSRF-Token"] = state.csrf; }
      const response = await fetch(path, { method, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal, ...(method !== "GET" ? { body: JSON.stringify(options.body || {}) } : {}) });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(text(data.error || data.message, `Request failed (${response.status}).`));
      return data;
    } catch (error) { if (error.name === "AbortError") throw new Error("The collector took too long to respond. Please try again."); if (error instanceof TypeError) { const unavailable = new Error("Cannot reach your collector. Start it on this machine, then refresh this page."); unavailable.network = true; throw unavailable; } throw error; }
    finally { window.clearTimeout(timer); }
  }

  function sourceDefinitions() { const sources = array(state.settings?.sources); return sources.length ? sources : array(state.status?.sources); }
  function companies() { const values = array(state.settings?.companies); return values.length ? values : array(state.status?.watchlist); }
  function selectedWatchlist() { return array(state.settings?.watchlist || state.status?.watchlist).map(symbol).filter(Boolean); }
  function selectedSources() { const value = state.settings?.enabled_sources; return Array.isArray(value) ? value.map(String) : sourceDefinitions().filter((item) => item.enabled !== false).map((item) => text(item.id)); }
  function fillSelect(id, first, entries) { const select = byId(id); const previous = select.value; const options = [new Option(first, ""), ...entries.map(([value, label]) => new Option(label, value))]; replace(select, options); if (options.some((option) => option.value === previous)) select.value = previous; }

  function renderStatus() {
    const status = state.status;
    if (!status) return;
    setConnection(state.online);
    byId("article-count").textContent = count(status.article_count);
    byId("nav-count").textContent = count(status.article_count);
    byId("app-version").textContent = status.version ? `v${text(status.version).replace(/^v/, "")}` : "";
    const sources = array(status.sources);
    const enabled = state.settings ? new Set(selectedSources()) : null;
    const active = sources.filter((source) => enabled ? enabled.has(text(source.id)) : source.enabled !== false && source.status !== "disabled");
    const unhealthy = active.filter((source) => source.last_error || ["error", "failed"].includes(source.status));
    byId("source-count").textContent = count(active.length);
    byId("source-description").textContent = unhealthy.length ? `${unhealthy.length} need attention · See Sources` : "Your selected publisher feeds";
    byId("last-scan").textContent = status.scanning ? "Checking for new headlines…" : status.last_scan ? `Last checked ${relativeDate(status.last_scan)}` : "No completed checks yet";
    byId("last-scan").title = status.last_scan ? absoluteDate(status.last_scan) : "";
    byId("next-scan").textContent = status.next_scan ? `Next check: ${absoluteDate(status.next_scan)}` : "Checks run automatically while the collector is running.";
    byId("scan-button").disabled = Boolean(status.scanning) || state.busy;
    byId("first-scan").disabled = Boolean(status.scanning) || state.busy;
    const telegram = status.telegram || {};
    const connected = Boolean(telegram.configured);
    const delivering = connected && Boolean(telegram.enabled);
    byId("telegram-overview").textContent = delivering ? "Connected" : connected ? "Paused" : "Not connected";
    byId("telegram-description").textContent = delivering ? `${count(telegram.delivered)} delivered · ${count(telegram.pending)} pending` : connected ? "Enable delivery in Telegram settings" : "Connect your own bot to get updates";
    byId("telegram-badge").textContent = delivering ? "Delivery enabled" : connected ? "Delivery paused" : "Not connected";
    byId("telegram-badge").className = `pill${delivering ? " success" : ""}`;
    byId("delivery-sent").textContent = count(telegram.delivered);
    byId("delivery-pending").textContent = count(telegram.pending);
    byId("delivery-failed").textContent = count(telegram.failed);
    byId("telegram-test").disabled = !connected || state.busy;
    byId("telegram-test").previousElementSibling.textContent = telegram.last_test_status === "success" ? "Last test delivered successfully." : telegram.last_test_status === "error" ? "The last test failed. Check your connection and try again." : "Send one test message to confirm delivery.";
    if (!state.telegramDirty) byId("telegram-enabled").checked = Boolean(telegram.enabled);
    const pairing = text(telegram.pairing_command || (telegram.pairing_code ? `/start ${text(telegram.pairing_code)}` : ""));
    byId("pairing-box").hidden = !pairing;
    byId("pairing-command").textContent = pairing;
    byId("onboarding").hidden = number(status.article_count) > 0;
    renderSources();
  }

  function renderSources() {
    const live = new Map(array(state.status?.sources).map((source) => [text(source.id), source]));
    const enabled = new Set(selectedSources());
    const sources = sourceDefinitions();
    if (!sources.length) { replace(byId("sources-list"), [nodes("p", "muted", "No sources are available yet.")]); return; }
    replace(byId("sources-list"), sources.map((definition) => {
      const source = { ...definition, ...(live.get(text(definition.id)) || {}) };
      const isEnabled = state.settings ? enabled.has(text(source.id)) : source.enabled !== false && source.status !== "disabled";
      const hasError = Boolean(source.last_error) || ["error", "failed"].includes(source.status);
      const healthy = isEnabled && !hasError && Boolean(source.last_success_at);
      const card = nodes("article", "source-card");
      const top = nodes("div", "source-card-top");
      top.append(nodes("span", "source-avatar", initials(source.name)), nodes("h2", "", source.name || source.id), nodes("span", `pill${hasError && isEnabled ? " error" : healthy ? " success" : ""}`, !isEnabled ? "Disabled" : hasError ? "Needs attention" : healthy ? "Healthy" : "Not checked"));
      card.append(top, nodes("p", "source-description", source.description || source.category || "Publisher feed"));
      const details = nodes("dl", "source-details");
      details.append(nodes("dt", "", "Last successful check"), nodes("dd", "", relativeDate(source.last_success_at)), nodes("dt", "", "Articles collected"), nodes("dd", "", count(source.total_new_articles)));
      card.append(details);
      if (hasError && isEnabled) card.append(nodes("p", "source-error", source.last_error || "The latest check failed. The collector will try again."));
      if (safeURL(source.homepage || source.url)) card.append(link("Visit publisher ↗", source.homepage || source.url, "source-link"));
      return card;
    }));
  }

  function renderSettings() {
    if (!state.settings || state.settingsDirty) return;
    byId("interval").value = String(number(state.settings.interval_minutes) || 5);
    const watchlist = new Set(selectedWatchlist());
    const allCompanies = companies();
    replace(byId("company-options"), allCompanies.map((company) => {
      const label = nodes("label", "company-option");
      const input = document.createElement("input"); input.type = "checkbox"; input.name = "company"; input.value = symbol(company); input.checked = watchlist.has(input.value);
      const content = nodes("span"); content.append(nodes("strong", "", symbol(company)), nodes("small", "", company.name || symbol(company)));
      label.append(input, content); return label;
    }));
    const sources = new Set(selectedSources());
    replace(byId("source-options"), sourceDefinitions().map((source) => {
      const label = nodes("label", "source-option"); const input = document.createElement("input"); input.type = "checkbox"; input.name = "source"; input.value = text(source.id); input.checked = sources.has(input.value);
      const content = nodes("span", "", source.name || source.id); if (source.category) content.append(nodes("small", "", source.category));
      label.append(input, content); return label;
    }));
    fillSelect("ticker-filter", "All companies", allCompanies.map((company) => [symbol(company), `${symbol(company)}${company.name ? ` · ${company.name}` : ""}`]));
    fillSelect("source-filter", "All sources", sourceDefinitions().map((source) => [text(source.id), text(source.name || source.id)]));
  }

  function initials(name) { return text(name, "TB").split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join("").toUpperCase(); }
  function renderEmpty(title, description) { const container = nodes("div", "empty-state"); container.append(nodes("span", "empty-symbol", "▤"), nodes("h3", "", title), nodes("p", "", description)); replace(byId("articles"), [container]); }
  function renderArticles() {
    byId("results-count").textContent = state.total ? `${count(state.total)} headline${state.total === 1 ? "" : "s"}${state.filters.search || state.filters.ticker || state.filters.source ? " matching your filters" : " in your library"}` : "No headlines yet";
    byId("page-label").textContent = state.total ? `${count(state.offset + 1)}–${count(Math.min(state.offset + state.articles.length, state.total))} of ${count(state.total)}` : "";
    byId("previous-page").disabled = !state.offset || state.articlesBusy;
    byId("next-page").disabled = state.offset + state.limit >= state.total || state.articlesBusy;
    const renderKey = JSON.stringify({ articles: state.articles, total: state.total, offset: state.offset });
    if (renderKey === state.renderedArticleKey) { document.querySelectorAll(".article time[datetime]").forEach((time) => { time.textContent = relativeDate(time.dateTime); }); return; }
    state.renderedArticleKey = renderKey;
    if (!state.articles.length) { const filtered = state.filters.search || state.filters.ticker || state.filters.source; renderEmpty(filtered ? "No matching headlines" : "Your next briefing starts here", filtered ? "Try a different keyword, company, or source." : "Choose your sources and check for news. New headlines will appear here as they are collected."); return; }
    replace(byId("articles"), state.articles.map((article) => {
      const card = nodes("article", "article"); const avatar = nodes("span", "source-avatar", initials(article.source_name)); avatar.setAttribute("aria-hidden", "true");
      const body = nodes("div", "article-body"); const meta = nodes("div", "article-meta");
      const date = article.published_at || article.first_seen_at;
      const time = nodes("time", "", date ? relativeDate(date) : "Publication time unavailable"); if (date && !Number.isNaN(new Date(date).getTime())) { time.dateTime = new Date(date).toISOString(); time.title = absoluteDate(date); }
      meta.append(nodes("span", "source-name", article.source_name || "Publisher"), nodes("span", "meta-separator", "·"), time);
      const heading = nodes("h3"); heading.append(link(article.title || "Untitled headline", article.url, "article-title"));
      body.append(meta, heading);
      if (article.summary) body.append(nodes("p", "article-summary", article.summary));
      const bottom = nodes("div", "article-bottom"); const tags = nodes("div", "tags");
      array(article.tickers).forEach((ticker) => tags.append(nodes("span", "tag", symbol(ticker))));
      bottom.append(tags, link("Read original ↗", article.url, "read-link")); body.append(bottom); card.append(avatar, body); return card;
    }));
  }

  function renderDeliveries(data) {
    const deliveries = array(data.deliveries).slice(0, 12);
    if (!deliveries.length) { replace(byId("delivery-list"), [nodes("p", "muted", "No delivery activity yet. New updates will appear here after you connect Telegram.")]); return; }
    replace(byId("delivery-list"), deliveries.map((delivery) => {
      const item = nodes("div", "delivery-item"); const status = text(delivery.status, "pending");
      const label = ["sent", "delivered"].includes(status) ? "Delivered" : ["failed", "error"].includes(status) ? "Failed" : "Pending";
      item.append(nodes("span", `pill ${label === "Delivered" ? "success" : label === "Failed" ? "error" : "pending"}`, label), nodes("p", "", delivery.title || delivery.article_title || delivery.source_name || "News update"), nodes("small", "", relativeDate(delivery.delivered_at || delivery.sent_at || delivery.updated_at || delivery.created_at)));
      return item;
    }));
  }

  async function loadArticles() {
    if (state.articlesBusy) return;
    state.articlesBusy = true;
    try { const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) }); Object.entries(state.filters).forEach(([key, value]) => { if (value) params.set(key, value); }); const data = await api(`/api/articles?${params}`); state.articles = array(data.articles); state.total = number(data.total); renderArticles(); }
    finally { state.articlesBusy = false; byId("previous-page").disabled = !state.offset; byId("next-page").disabled = state.offset + state.limit >= state.total; }
  }

  async function refresh(includeSettings = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    try {
      const status = await api("/api/status"); const reconnecting = !state.online; state.status = status; state.csrf = text(status.csrf_token); setConnection(true); if (reconnecting) showNotice("");
      if (includeSettings || !state.settings) { state.settings = await api("/api/settings"); renderSettings(); }
      renderStatus();
      if (state.view === "news") await loadArticles();
      if (state.view === "telegram") renderDeliveries(await api("/api/deliveries"));
    } catch (error) { offline(error); }
    finally { state.refreshing = false; }
  }

  async function mutate(path, body, success) {
    if (state.busy) return false;
    state.busy = true; renderStatus();
    try { await api(path, { method: "POST", body }); showNotice(success, "success"); return true; }
    catch (error) { if (error.network) offline(error); else showNotice(error.message, "error"); return false; }
    finally { state.busy = false; renderStatus(); }
  }

  async function scan() { if (await mutate("/api/scan", {}, "Checking your selected feeds. New headlines will appear as the check finishes.")) { await refresh(); window.setTimeout(() => { if (!document.hidden) refresh(); }, 4000); } }
  function changeView() {
    const requested = window.location.hash.slice(1);
    state.view = ["news", "sources", "telegram", "settings"].includes(requested) ? requested : "news";
    for (const view of ["news", "sources", "telegram", "settings"]) byId(`view-${view}`).hidden = view !== state.view;
    document.querySelectorAll("[data-view]").forEach((item) => { const active = item.dataset.view === state.view; item.classList.toggle("active", active); if (active) item.setAttribute("aria-current", "page"); else item.removeAttribute("aria-current"); });
    document.title = `${state.view[0].toUpperCase() + state.view.slice(1)} · TechBrief`;
    refresh();
  }

  byId("scan-button").addEventListener("click", scan);
  byId("first-scan").addEventListener("click", scan);
  byId("filter-form").addEventListener("submit", async (event) => { event.preventDefault(); if (state.articlesBusy) return; state.filters = { search: byId("search").value.trim(), ticker: byId("ticker-filter").value, source: byId("source-filter").value }; state.offset = 0; try { await loadArticles(); } catch (error) { offline(error); } });
  for (const [id, delta] of [["previous-page", -50], ["next-page", 50]]) byId(id).addEventListener("click", async () => { if (state.articlesBusy) return; state.offset = Math.max(0, state.offset + delta); try { await loadArticles(); } catch (error) { offline(error); } });
  byId("settings-form").addEventListener("change", () => { state.settingsDirty = true; byId("settings-message").textContent = "You have unsaved changes."; });
  byId("select-companies").addEventListener("click", () => { const inputs = [...document.querySelectorAll('input[name="company"]')]; const selectAll = inputs.some((input) => !input.checked); inputs.forEach((input) => { input.checked = selectAll; }); byId("select-companies").textContent = selectAll ? "Clear all" : "Select all"; state.settingsDirty = true; byId("settings-message").textContent = "You have unsaved changes."; });
  byId("settings-form").addEventListener("submit", async (event) => { event.preventDefault(); if (!byId("settings-form").reportValidity()) return; const body = { interval_minutes: Number(byId("interval").value), watchlist: [...document.querySelectorAll('input[name="company"]:checked')].map((input) => input.value), enabled_sources: [...document.querySelectorAll('input[name="source"]:checked')].map((input) => input.value), telegram_enabled: Boolean(state.status?.telegram?.enabled) }; if (!body.watchlist.length || !body.enabled_sources.length) { showNotice("Choose at least one company and one news source before saving.", "error"); return; } if (await mutate("/api/settings", body, "Preferences saved. Your next check will use these settings.")) { state.settingsDirty = false; byId("settings-message").textContent = "All changes saved."; await refresh(true); } });
  byId("telegram-form").addEventListener("input", () => { state.telegramDirty = true; });
  byId("telegram-form").addEventListener("submit", async (event) => { event.preventDefault(); if (!byId("telegram-form").reportValidity()) return; const input = byId("bot-token"); const body = { token: input.value.trim(), chat_id: byId("chat-id").value.trim(), enabled: byId("telegram-enabled").checked }; if (!/^\d{5,}:[A-Za-z0-9_-]{20,}$/.test(body.token)) { showNotice("Paste a valid bot token from @BotFather before connecting.", "error"); return; } if (body.chat_id && !/^-?\d{1,20}$/.test(body.chat_id)) { showNotice("Use a numeric Chat ID, or leave it blank after sending the private pairing command.", "error"); return; } try { if (await mutate("/api/telegram/configure", body, "Telegram connected. Use Send test message to check delivery.")) { state.telegramDirty = false; byId("chat-id").value = ""; await refresh(true); } } finally { input.value = ""; body.token = ""; } });
  byId("telegram-test").addEventListener("click", async () => { if (await mutate("/api/telegram/test", {}, "Test message sent. Check your Telegram chat.")) await refresh(); });
  window.addEventListener("hashchange", changeView);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.addEventListener("online", () => refresh());
  window.addEventListener("offline", () => offline());
  window.setInterval(() => { if (!document.hidden && !state.busy) refresh(); }, 30000);
  changeView();
})();
