"""Self-contained single-page Admin Web UI served at /admin."""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SiQL Admin Console</title>
<style>
  :root {
    --bg: #0b1120;
    --surface: #111827;
    --surface-2: #1a2234;
    --surface-3: #222d44;
    --border: #2a3750;
    --text: #e5e7eb;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --accent-dim: rgba(56, 189, 248, 0.14);
    --ok: #34d399;
    --ok-dim: rgba(52, 211, 153, 0.14);
    --warn: #fbbf24;
    --warn-dim: rgba(251, 191, 36, 0.14);
    --err: #f87171;
    --err-dim: rgba(248, 113, 113, 0.14);
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
    --sans: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; background: var(--bg); color: var(--text); font-family: var(--sans); font-size: 14px; }
  button, input, textarea { font-family: inherit; color: inherit; }
  button {
    background: var(--surface-3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 11px;
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    transition: background .15s, border-color .15s;
  }
  button:hover { background: #2c3a57; border-color: #3d5175; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary { background: #0284c7; border-color: #0ea5e9; color: #fff; }
  button.primary:hover { background: #0369a1; }
  button.danger { background: rgba(220, 38, 38, 0.2); border-color: rgba(248, 113, 113, 0.45); color: #fca5a5; }
  button.danger:hover { background: rgba(220, 38, 38, 0.35); }
  button.ghost { background: transparent; border-color: transparent; color: var(--muted); }
  button.ghost:hover { background: var(--surface-2); color: var(--text); }

  .app { display: flex; flex-direction: column; height: 100vh; }

  /* Top status bar */
  .topbar {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 16px; background: var(--surface); border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-title { font-weight: 700; font-size: 16px; letter-spacing: 0.2px; }
  .badge {
    display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px;
    border-radius: 999px; font-size: 11.5px; font-weight: 600;
    background: var(--surface-2); border: 1px solid var(--border); color: var(--muted);
  }
  .badge.ok { background: var(--ok-dim); border-color: rgba(52, 211, 153, 0.35); color: var(--ok); }
  .badge.warn { background: var(--warn-dim); border-color: rgba(251, 191, 36, 0.35); color: var(--warn); }
  .badge.err { background: var(--err-dim); border-color: rgba(248, 113, 113, 0.35); color: var(--err); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .metrics-strip { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12.5px; }
  .metrics-strip strong { color: var(--text); font-weight: 600; }
  .top-actions { display: flex; align-items: center; gap: 8px; }

  /* Main layout */
  .layout { display: grid; grid-template-columns: 290px 1fr; flex: 1; min-height: 0; }
  @media (max-width: 860px) { .layout { grid-template-columns: 1fr; grid-template-rows: auto 1fr; } }

  /* Sidebar */
  .sidebar {
    background: var(--surface); border-right: 1px solid var(--border);
    display: flex; flex-direction: column; min-height: 0;
  }
  .sidebar-head { padding: 12px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 8px; }
  .sidebar-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .sidebar-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); font-weight: 700; }
  .filter-input {
    width: 100%; background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 13px; outline: none;
  }
  .filter-input:focus { border-color: var(--accent); }
  .table-list { flex: 1; overflow-y: auto; padding: 6px; }
  .table-item {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 8px 10px; border-radius: 6px; cursor: pointer; border: 1px solid transparent;
    margin-bottom: 2px;
  }
  .table-item:hover { background: var(--surface-2); }
  .table-item.active { background: var(--accent-dim); border-color: rgba(56, 189, 248, 0.4); }
  .table-name { font-family: var(--mono); font-size: 13px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .table-meta { font-size: 11.5px; color: var(--muted); white-space: nowrap; }
  .empty-note { padding: 14px 10px; color: var(--muted); font-size: 12.5px; text-align: center; }

  .inspector {
    border-top: 1px solid var(--border); padding: 12px; background: #0e1526;
    max-height: 46%; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
  }
  .inspector-actions { display: flex; flex-wrap: wrap; gap: 6px; }
  .schema-list { display: flex; flex-direction: column; gap: 4px; font-family: var(--mono); font-size: 12px; }
  .schema-col {
    display: flex; justify-content: space-between; padding: 4px 8px;
    background: var(--surface); border-radius: 4px; border: 1px solid var(--border);
  }
  .schema-type { color: var(--accent); }
  .kv-mini { font-size: 11.5px; color: var(--muted); line-height: 1.5; }

  /* Workspace */
  .main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .query-block {
    padding: 14px 16px; border-bottom: 1px solid var(--border); background: #0d1424;
    display: flex; flex-direction: column; gap: 10px;
  }
  .chips { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .chips-label { font-size: 11.5px; color: var(--muted); margin-right: 4px; }
  .chip {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: 999px;
    padding: 3px 9px; font-size: 11.5px; font-family: var(--mono); color: var(--muted); cursor: pointer;
  }
  .chip:hover { color: var(--text); border-color: var(--accent); }
  #query-input {
    width: 100%; min-height: 126px; max-height: 320px; resize: vertical;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; font-family: var(--mono); font-size: 13.5px; line-height: 1.48;
    outline: none; tab-size: 2;
  }
  #query-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.12); }
  .query-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
  .toolbar-group { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .hint { font-size: 11.5px; color: var(--muted); }

  /* Tabs & output */
  .tabs { display: flex; align-items: center; gap: 4px; padding: 0 16px; background: var(--surface); border-bottom: 1px solid var(--border); }
  .tab {
    padding: 10px 14px; font-size: 13px; font-weight: 600; color: var(--muted);
    background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0; cursor: pointer;
  }
  .tab:hover { color: var(--text); background: transparent; }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .output-area { flex: 1; overflow: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px; }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .card-head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 9px 12px; background: var(--surface-2); border-bottom: 1px solid var(--border);
    font-size: 12.5px; font-weight: 600;
  }
  .card-body { padding: 12px; overflow-x: auto; }

  .error-banner {
    padding: 11px 14px; border-radius: 8px; background: var(--err-dim);
    border: 1px solid rgba(248, 113, 113, 0.45); color: #fecaca; font-family: var(--mono); font-size: 13px;
    white-space: pre-wrap;
  }
  .msg-banner {
    padding: 10px 12px; border-radius: 6px; background: var(--ok-dim);
    border: 1px solid rgba(52, 211, 153, 0.35); color: #a7f3d0; font-size: 13px;
  }

  table.data { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12.5px; }
  table.data th, table.data td {
    text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border);
    white-space: nowrap; max-width: 420px; overflow: hidden; text-overflow: ellipsis;
  }
  table.data th { background: #141d30; color: var(--muted); font-weight: 600; position: sticky; top: 0; }
  table.data tr:hover td { background: rgba(255, 255, 255, 0.025); }
  .val-null { color: var(--muted); font-style: italic; }
  .val-bool { color: var(--warn); }
  .val-num { color: #93c5fd; }

  pre.json-view, pre.code-block {
    margin: 0; font-family: var(--mono); font-size: 12.5px; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word;
  }

  .history-item {
    display: grid; grid-template-columns: 76px 1fr auto; gap: 10px; align-items: center;
    padding: 9px 12px; border-bottom: 1px solid var(--border); cursor: pointer;
  }
  .history-item:last-child { border-bottom: none; }
  .history-item:hover { background: var(--surface-2); }
  .history-sql { font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .history-meta { font-size: 11.5px; color: var(--muted); white-space: nowrap; }

  .runtime-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
  .kv-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .kv-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
  .kv-table td:first-child { color: var(--muted); width: 42%; }
  .kv-table td:last-child { font-family: var(--mono); word-break: break-all; }

  /* Auth modal */
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(2, 6, 23, 0.78);
    display: none; align-items: center; justify-content: center; padding: 16px; z-index: 50;
  }
  .modal-backdrop.open { display: flex; }
  .modal {
    width: 100%; max-width: 420px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 18px; display: flex; flex-direction: column; gap: 12px;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.5);
  }
  .modal h3 { margin: 0; font-size: 16px; }
  .modal p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <div class="brand">
      <span class="brand-title">SiQL Admin</span>
      <span id="version-badge" class="badge">v-</span>
      <span id="status-pill" class="badge warn"><span class="dot"></span><span id="status-text">Connecting</span></span>
    </div>
    <div class="metrics-strip">
      <span>Uptime: <strong id="stat-uptime">-</strong></span>
      <span>PID: <strong id="stat-pid">-</strong></span>
      <span>RSS: <strong id="stat-rss">-</strong></span>
      <span>Tables: <strong id="stat-tables">-</strong></span>
      <span>Rows: <strong id="stat-rows">-</strong></span>
    </div>
    <div class="top-actions">
      <button id="btn-refresh" type="button">Refresh</button>
      <button id="btn-token" type="button">Token</button>
    </div>
  </header>

  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar-head">
        <div class="sidebar-row">
          <span class="sidebar-title">Database Tables</span>
          <button id="btn-new-table" type="button">+ New Table</button>
        </div>
        <input id="table-filter" class="filter-input" type="search" placeholder="Filter tables..." autocomplete="off">
      </div>
      <div id="table-list" class="table-list">
        <div class="empty-note">Loading tables...</div>
      </div>
      <div id="table-inspector" class="inspector" style="display:none;"></div>
    </aside>

    <main class="main">
      <section class="query-block">
        <div class="chips">
          <span class="chips-label">Snippets:</span>
          <button class="chip" type="button" data-sql="SHOW TABLES;">SHOW TABLES</button>
          <button class="chip" type="button" data-sql="SHOW FILES;">SHOW FILES</button>
          <button class="chip" type="button" data-sql="CREATE TABLE items (id int, name str, active bool);">CREATE TABLE</button>
          <button class="chip" type="button" data-sql="SELECT * FROM items LIMIT 50;">SELECT</button>
          <button class="chip" type="button" data-sql="INSERT INTO items VALUES (1, 'alpha', TRUE);">INSERT</button>
          <button class="chip" type="button" data-sql="COMPACT;">COMPACT</button>
        </div>
        <textarea id="query-input" spellcheck="false" placeholder="Enter one or more SiQL statements separated by ';'... (Ctrl+Enter to run)">SHOW TABLES;</textarea>
        <div class="query-toolbar">
          <div class="toolbar-group">
            <button id="btn-run" class="primary" type="button">Run Query</button>
            <button id="btn-clear" type="button">Clear</button>
            <span class="hint">Ctrl+Enter / Cmd+Enter to execute</span>
          </div>
          <div class="toolbar-group">
            <span id="query-badge" class="badge" style="display:none;"></span>
            <button id="btn-view-toggle" type="button">View: Table</button>
            <button id="btn-copy-json" type="button" disabled>Copy JSON</button>
            <button id="btn-download-csv" type="button" disabled>Download CSV</button>
          </div>
        </div>
      </section>

      <nav class="tabs">
        <button class="tab active" type="button" data-tab="results">Results</button>
        <button class="tab" type="button" data-tab="history">History</button>
        <button class="tab" type="button" data-tab="runtime">Runtime</button>
      </nav>

      <section id="tab-results" class="output-area">
        <div class="empty-note">Run a query or select a table from the sidebar to inspect data.</div>
      </section>
      <section id="tab-history" class="output-area" style="display:none;"></section>
      <section id="tab-runtime" class="output-area" style="display:none;"></section>
    </main>
  </div>
</div>

<div id="token-modal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal">
    <h3>Bearer Token</h3>
    <p id="token-prompt-msg">Enter the server Bearer token (stored only in this browser tab's sessionStorage).</p>
    <input id="token-input" class="filter-input" type="password" placeholder="Paste SIQL_TOKEN..." autocomplete="off">
    <div class="modal-actions">
      <button id="btn-token-clear" class="danger" type="button">Clear Token</button>
      <button id="btn-token-cancel" type="button">Cancel</button>
      <button id="btn-token-save" class="primary" type="button">Save &amp; Connect</button>
    </div>
  </div>
</div>

<script>
(function() {
  "use strict";

  const TOKEN_KEY = "siql_admin_token";
  const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    tables: [],
    selectedTable: null,
    status: null,
    history: [],
    lastQueryPayload: null,
    lastQueryError: null,
    viewMode: "table",
    activeTab: "results",
  };

  const el = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatBytes(n) {
    if (n === null || n === undefined) return "-";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }

  function formatUptime(sec) {
    if (sec === null || sec === undefined) return "-";
    const s = Math.floor(sec);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    if (d > 0) return d + "d " + h + "h " + m + "m";
    if (h > 0) return h + "h " + m + "m " + r + "s";
    if (m > 0) return m + "m " + r + "s";
    return r + "s";
  }

  function setStatusPill(kind, text) {
    const pill = el("status-pill");
    pill.className = "badge " + kind;
    el("status-text").textContent = text;
  }

  async function api(path, opts) {
    const options = Object.assign({ method: "GET", headers: {} }, opts || {});
    if (state.token) {
      options.headers["Authorization"] = "Bearer " + state.token;
    }
    if (options.body !== undefined && typeof options.body !== "string") {
      options.body = JSON.stringify(options.body);
      options.headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(path, options);
    const ct = (resp.headers.get("Content-Type") || "").toLowerCase();
    const data = ct.includes("application/json") ? await resp.json() : await resp.text();
    if (resp.status === 401) {
      setStatusPill("warn", "Auth Required");
      openTokenModal("Authentication is enabled on this server. Enter the Bearer token to continue.");
      throw { status: 401, data };
    }
    if (!resp.ok) {
      throw { status: resp.status, data };
    }
    return data;
  }

  function openTokenModal(message) {
    if (message) el("token-prompt-msg").textContent = message;
    el("token-input").value = state.token;
    el("token-modal").classList.add("open");
    setTimeout(() => el("token-input").focus(), 30);
  }

  function closeTokenModal() {
    el("token-modal").classList.remove("open");
  }

  async function refreshAll() {
    try {
      const [statusData, tablesData, historyData] = await Promise.all([
        api("/api/status"),
        api("/api/tables"),
        api("/api/history"),
      ]);
      state.status = statusData;
      state.tables = tablesData.tables || [];
      state.history = historyData.history || [];
      setStatusPill("ok", "Online");
      renderTopBar();
      renderTableList();
      if (state.selectedTable) {
        const exists = state.tables.some((t) => t.name === state.selectedTable);
        if (exists) {
          await loadInspector(state.selectedTable);
        } else {
          state.selectedTable = null;
          el("table-inspector").style.display = "none";
        }
      }
      renderHistory();
      renderRuntime();
    } catch (err) {
      if (!err || err.status !== 401) {
        setStatusPill("err", "Offline");
      }
    }
  }

  function renderTopBar() {
    const s = state.status;
    if (!s) return;
    el("version-badge").textContent = "v" + s.version;
    el("stat-uptime").textContent = formatUptime(s.uptime_seconds);
    el("stat-pid").textContent = s.process && s.process.pid ? s.process.pid : "-";
    el("stat-rss").textContent = s.process ? formatBytes(s.process.rss_bytes) : "-";
    el("stat-tables").textContent = s.database ? s.database.tables : "-";
    el("stat-rows").textContent = s.database ? s.database.total_rows : "-";
  }

  function renderTableList() {
    const list = el("table-list");
    const filter = el("table-filter").value.trim().toLowerCase();
    const visible = state.tables.filter((t) => !filter || t.name.toLowerCase().includes(filter));
    if (!visible.length) {
      list.innerHTML = '<div class="empty-note">' + (state.tables.length ? "No matching tables." : "No tables yet.") + "</div>";
      return;
    }
    list.innerHTML = visible.map((t) => {
      const active = t.name === state.selectedTable ? " active" : "";
      return (
        '<div class="table-item' + active + '" data-table="' + escapeHtml(t.name) + '">' +
          '<span class="table-name">' + escapeHtml(t.name) + '</span>' +
          '<span class="table-meta">' + escapeHtml(t.rows) + ' rows · ' + escapeHtml(formatBytes(t.bytes)) + '</span>' +
        '</div>'
      );
    }).join("");
    list.querySelectorAll(".table-item").forEach((node) => {
      node.addEventListener("click", () => selectTable(node.getAttribute("data-table")));
    });
  }

  async function selectTable(name) {
    state.selectedTable = name;
    renderTableList();
    await loadInspector(name);
  }

  async function loadInspector(name) {
    const box = el("table-inspector");
    try {
      const detail = await api("/api/tables/" + encodeURIComponent(name) + "?limit=25&offset=0");
      const colsHtml = (detail.schema || []).map((c) =>
        '<div class="schema-col"><span>' + escapeHtml(c.name) + '</span><span class="schema-type">' + escapeHtml(c.type) + '</span></div>'
      ).join("") || '<div class="kv-mini">No columns defined</div>';
      const cache = detail.cache || {};
      box.style.display = "flex";
      box.innerHTML =
        '<div class="sidebar-row">' +
          '<strong class="table-name">' + escapeHtml(detail.name) + '</strong>' +
          '<span class="table-meta">' + escapeHtml(detail.total_rows) + ' rows</span>' +
        '</div>' +
        '<div class="inspector-actions">' +
          '<button type="button" id="ins-browse">Browse</button>' +
          '<button type="button" id="ins-describe">Describe</button>' +
          '<button type="button" id="ins-compact">Compact</button>' +
          '<button type="button" class="danger" id="ins-drop">Drop</button>' +
        '</div>' +
        '<div class="schema-list">' + colsHtml + '</div>' +
        '<div class="kv-mini">Cache: ' + escapeHtml(cache.entries ?? 0) + ' entries · ' +
          escapeHtml(cache.hits ?? 0) + ' hits / ' + escapeHtml(cache.misses ?? 0) + ' misses · ' +
          escapeHtml(formatBytes(cache.bytes ?? 0)) + '</div>';

      el("ins-browse").addEventListener("click", () => {
        el("query-input").value = "SELECT * FROM `" + name.replace(/`/g, "``") + "` LIMIT 100;";
        runQuery();
      });
      el("ins-describe").addEventListener("click", () => {
        el("query-input").value = "DESCRIBE `" + name.replace(/`/g, "``") + "`;";
        runQuery();
      });
      el("ins-compact").addEventListener("click", async () => {
        try {
          const res = await api("/api/tables/" + encodeURIComponent(name) + "/compact", { method: "POST" });
          state.lastQueryError = null;
          state.lastQueryPayload = {
            elapsed_ms: 0,
            results: [{
              columns: ["table", "bytes_before", "bytes_after"],
              rows: [[res.table, res.bytes_before, res.bytes_after]],
              rowcount: 1,
              message: "Compacted " + res.table,
            }],
          };
          setTab("results");
          renderResults();
          await refreshAll();
        } catch (err) {
          if (err && err.status !== 401) {
            state.lastQueryError = (err.data && err.data.error) || "Compact failed";
            setTab("results");
            renderResults();
          }
        }
      });
      el("ins-drop").addEventListener("click", async () => {
        if (!window.confirm("Drop table '" + name + "' and delete its .siql file?")) return;
        try {
          const res = await api("/api/tables/" + encodeURIComponent(name), { method: "DELETE" });
          state.selectedTable = null;
          el("table-inspector").style.display = "none";
          state.lastQueryError = null;
          state.lastQueryPayload = {
            elapsed_ms: 0,
            results: [{ columns: [], rows: [], rowcount: 0, message: res.message || ("Dropped " + name) }],
          };
          setTab("results");
          renderResults();
          await refreshAll();
        } catch (err) {
          if (err && err.status !== 401) {
            state.lastQueryError = (err.data && err.data.error) || "Drop failed";
            setTab("results");
            renderResults();
          }
        }
      });
    } catch (err) {
      if (err && err.status !== 401) {
        box.style.display = "none";
      }
    }
  }

  function formatCell(value) {
    if (value === null || value === undefined) return '<span class="val-null">NULL</span>';
    if (typeof value === "boolean") return '<span class="val-bool">' + (value ? "TRUE" : "FALSE") + '</span>';
    if (typeof value === "number") return '<span class="val-num">' + escapeHtml(value) + '</span>';
    if (typeof value === "object") return escapeHtml(JSON.stringify(value));
    return escapeHtml(value);
  }

  function renderResults() {
    const container = el("tab-results");
    const badge = el("query-badge");
    const copyBtn = el("btn-copy-json");
    const csvBtn = el("btn-download-csv");

    if (!state.lastQueryPayload && !state.lastQueryError) {
      badge.style.display = "none";
      copyBtn.disabled = true;
      csvBtn.disabled = true;
      container.innerHTML = '<div class="empty-note">Run a query or select a table from the sidebar to inspect data.</div>';
      return;
    }

    const payload = state.lastQueryPayload || { results: [] };
    const results = payload.results || [];
    const elapsed = payload.elapsed_ms !== undefined ? payload.elapsed_ms + " ms" : "";

    badge.style.display = "inline-flex";
    if (state.lastQueryError) {
      badge.className = "badge err";
      badge.textContent = "Error" + (elapsed ? " · " + elapsed : "");
    } else {
      badge.className = "badge ok";
      badge.textContent = results.length + " statement" + (results.length === 1 ? "" : "s") + (elapsed ? " · " + elapsed : "");
    }

    copyBtn.disabled = false;
    csvBtn.disabled = !results.some((r) => r.columns && r.columns.length > 0);

    let html = "";
    if (state.lastQueryError) {
      html += '<div class="error-banner">' + escapeHtml(state.lastQueryError) + '</div>';
    }
    if (state.viewMode === "json") {
      const raw = state.lastQueryError ? { error: state.lastQueryError, results } : payload;
      html += '<div class="card"><div class="card-head"><span>JSON Response</span></div><div class="card-body"><pre class="json-view">' +
        escapeHtml(JSON.stringify(raw, null, 2)) + '</pre></div></div>';
      container.innerHTML = html;
      return;
    }

    if (!results.length && !state.lastQueryError) {
      html += '<div class="empty-note">No statements executed.</div>';
    }
    results.forEach((res, idx) => {
      if (res.columns && res.columns.length > 0) {
        const thead = "<tr>" + res.columns.map((c) => "<th>" + escapeHtml(c) + "</th>").join("") + "</tr>";
        const tbody = (res.rows || []).map((row) =>
          "<tr>" + row.map((cell) => "<td>" + formatCell(cell) + "</td>").join("") + "</tr>"
        ).join("");
        html +=
          '<div class="card">' +
            '<div class="card-head"><span>Statement #' + (idx + 1) + '</span><span class="table-meta">' + escapeHtml(res.message || "") + '</span></div>' +
            '<div class="card-body"><table class="data"><thead>' + thead + '</thead><tbody>' +
              (tbody || '<tr><td colspan="' + res.columns.length + '" class="val-null">(0 rows)</td></tr>') +
            '</tbody></table></div>' +
          '</div>';
      } else {
        html += '<div class="msg-banner"><strong>Statement #' + (idx + 1) + ':</strong> ' + escapeHtml(res.message || "OK") + '</div>';
      }
    });
    container.innerHTML = html;
  }

  function renderHistory() {
    const container = el("tab-history");
    const items = state.history || [];
    let body = "";
    if (!items.length) {
      body = '<div class="empty-note">No queries recorded yet.</div>';
    } else {
      body = items.map((h) => {
        const pill = h.ok ? '<span class="badge ok">OK</span>' : '<span class="badge err">ERR</span>';
        return (
          '<div class="history-item" data-sql="' + escapeHtml(h.query) + '">' +
            pill +
            '<div class="history-sql" title="' + escapeHtml(h.query) + '">' + escapeHtml(h.query) + '</div>' +
            '<div class="history-meta">' + escapeHtml(h.elapsed_ms) + ' ms · ' + escapeHtml((h.timestamp || "").replace("T", " ").slice(0, 19)) + '</div>' +
          '</div>'
        );
      }).join("");
    }
    container.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><span>Recent Queries (' + items.length + ')</span>' +
          '<button type="button" id="btn-clear-history">Clear History</button></div>' +
        '<div>' + body + '</div>' +
      '</div>';
    el("btn-clear-history").addEventListener("click", async () => {
      await api("/api/history", { method: "DELETE" });
      await refreshAll();
    });
    container.querySelectorAll(".history-item").forEach((node) => {
      node.addEventListener("click", () => {
        el("query-input").value = node.getAttribute("data-sql") || "";
        el("query-input").focus();
      });
    });
  }

  function renderRuntime() {
    const container = el("tab-runtime");
    const s = state.status;
    if (!s) {
      container.innerHTML = '<div class="empty-note">Runtime metrics unavailable.</div>';
      return;
    }
    const p = s.process || {};
    const d = s.database || {};
    const stats = s.stats || {};
    const base = window.location.origin;
    const rowsA = [
      ["Version", s.version],
      ["Platform", p.platform || "-"],
      ["PID", p.pid ?? "-"],
      ["Started", s.started_at || "-"],
      ["Uptime", formatUptime(s.uptime_seconds)],
      ["HTTP Address", s.listen_address || "-"],
      ["Unix Socket", s.unix_socket || "disabled"],
      ["Data Directory", d.path || "-"],
    ];
    const rowsB = [
      ["RSS Memory", formatBytes(p.rss_bytes)],
      ["Virtual Memory", formatBytes(p.vms_bytes)],
      ["Threads", p.threads ?? "-"],
      ["Open FDs", p.open_fds ?? "-"],
      ["HTTP Requests", stats.requests ?? 0],
      ["Queries Run", stats.queries ?? 0],
      ["Query Errors", stats.errors ?? 0],
      ["Auth Required", s.auth_required ? "Yes (Bearer)" : "No"],
    ];
    const kvTable = (rows) => '<table class="kv-table">' +
      rows.map((r) => "<tr><td>" + escapeHtml(r[0]) + "</td><td>" + escapeHtml(r[1]) + "</td></tr>").join("") +
      "</table>";
    const curlRef =
      "curl " + base + "/health\n" +
      "curl " + base + "/api/status\n" +
      "curl " + base + "/api/tables\n" +
      "curl -X POST " + base + '/query -H "Content-Type: application/json" -d \'{"query": "SHOW TABLES"}\'';
    container.innerHTML =
      '<div class="runtime-grid">' +
        '<div class="card"><div class="card-head"><span>Runtime &amp; Listeners</span></div><div class="card-body">' + kvTable(rowsA) + '</div></div>' +
        '<div class="card"><div class="card-head"><span>Process &amp; Counters</span></div><div class="card-body">' + kvTable(rowsB) + '</div></div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-head"><span>Network Endpoints Reference</span></div>' +
        '<div class="card-body"><pre class="code-block">' + escapeHtml(curlRef) + '</pre></div>' +
      '</div>';
  }

  function setTab(name) {
    state.activeTab = name;
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    el("tab-results").style.display = name === "results" ? "flex" : "none";
    el("tab-history").style.display = name === "history" ? "flex" : "none";
    el("tab-runtime").style.display = name === "runtime" ? "flex" : "none";
  }

  async function runQuery() {
    const sql = el("query-input").value;
    if (!sql.trim()) return;
    const btn = el("btn-run");
    btn.disabled = true;
    btn.textContent = "Running...";
    setTab("results");
    try {
      const payload = await api("/query", { method: "POST", body: { query: sql } });
      state.lastQueryError = null;
      state.lastQueryPayload = payload;
      renderResults();
      await refreshAll();
    } catch (err) {
      if (err && err.status === 401) return;
      state.lastQueryPayload = err && err.data && typeof err.data === "object" ? err.data : null;
      state.lastQueryError = (err && err.data && err.data.error) || "Request failed";
      renderResults();
      await refreshAll();
    } finally {
      btn.disabled = false;
      btn.textContent = "Run Query";
    }
  }

  function downloadCsv() {
    const results = (state.lastQueryPayload && state.lastQueryPayload.results) || [];
    const tableRes = results.find((r) => r.columns && r.columns.length > 0);
    if (!tableRes) return;
    const esc = (v) => {
      if (v === null || v === undefined) return "";
      const s = typeof v === "object" ? JSON.stringify(v) : String(v);
      return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const lines = [tableRes.columns.map(esc).join(",")];
    (tableRes.rows || []).forEach((row) => lines.push(row.map(esc).join(",")));
    const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "siql-result.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }

  // Event bindings
  el("btn-run").addEventListener("click", runQuery);
  el("btn-clear").addEventListener("click", () => { el("query-input").value = ""; el("query-input").focus(); });
  el("btn-refresh").addEventListener("click", refreshAll);
  el("btn-token").addEventListener("click", () => openTokenModal());
  el("btn-new-table").addEventListener("click", () => {
    el("query-input").value = "CREATE TABLE new_table (id int, name str, created bool);\nINSERT INTO new_table VALUES (1, 'sample', TRUE);\nSELECT * FROM new_table;";
    el("query-input").focus();
  });
  el("table-filter").addEventListener("input", renderTableList);
  el("btn-view-toggle").addEventListener("click", () => {
    state.viewMode = state.viewMode === "table" ? "json" : "table";
    el("btn-view-toggle").textContent = "View: " + (state.viewMode === "table" ? "Table" : "JSON");
    renderResults();
  });
  el("btn-copy-json").addEventListener("click", async () => {
    const raw = state.lastQueryError
      ? { error: state.lastQueryError, results: (state.lastQueryPayload && state.lastQueryPayload.results) || [] }
      : state.lastQueryPayload;
    if (navigator.clipboard && raw) {
      await navigator.clipboard.writeText(JSON.stringify(raw, null, 2));
    }
  });
  el("btn-download-csv").addEventListener("click", downloadCsv);

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      el("query-input").value = chip.getAttribute("data-sql") || "";
      el("query-input").focus();
    });
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setTab(tab.getAttribute("data-tab")));
  });

  el("query-input").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery();
    } else if (e.key === "Tab") {
      e.preventDefault();
      const ta = e.target;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      ta.value = ta.value.slice(0, start) + "  " + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = start + 2;
    }
  });

  el("btn-token-cancel").addEventListener("click", closeTokenModal);
  el("btn-token-clear").addEventListener("click", () => {
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    closeTokenModal();
    refreshAll();
  });
  el("btn-token-save").addEventListener("click", () => {
    state.token = el("token-input").value.trim();
    if (state.token) sessionStorage.setItem(TOKEN_KEY, state.token);
    else sessionStorage.removeItem(TOKEN_KEY);
    closeTokenModal();
    refreshAll();
  });
  el("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") el("btn-token-save").click();
  });

  refreshAll();
})();
</script>
</body>
</html>
"""
