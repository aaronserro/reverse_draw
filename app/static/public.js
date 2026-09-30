(async function () {
  let cfg, state, version = null;
  const board = document.getElementById("board");
  const lookup = document.getElementById("lookup");
  const result = document.getElementById("lookupResult");

  function renderLookup() {
    const hits = RD.search(lookup.value, state, state.holders);
    RD.renderBoard(board, cfg, state, { highlight: hits, holders: state.holders });
    if (!lookup.value.trim()) { result.innerHTML = ""; return; }
    if (!hits.size) {
      result.innerHTML = `<span class="muted">No matching ticket.</span>`;
      return;
    }
    const rows = [...hits].sort((a, b) => a - b).slice(0, 50).map((t) => {
      const s = RD.ticketStatus(state, t);
      const who = state.holders && state.holders[t] ? ` <span class="muted">${RD.esc(state.holders[t])}</span>` : "";
      return `<div>Ticket <b>#${t}</b>${who} &nbsp;<span class="pill ${s.cls}">${s.text}</span></div>`;
    });
    result.innerHTML = rows.join("");
  }

  function renderHistory() {
    const el = document.getElementById("history");
    const done = state.rounds.map((r) =>
      `<li><span><b>${RD.esc(r.label)}</b> — ${r.started_with} → ${r.survivors} ` +
      `<span class="muted">(${r.eliminated_count} out)</span></span><span class="when">${RD.fmtTime(r.timestamp)}</span></li>`);
    const upcoming = state.schedule.labels.slice(state.rounds_done).map((label, i) =>
      `<li class="muted"><span>${RD.esc(label)} — cut to ${state.schedule.survivors[state.rounds_done + i]}</span><span class="when">Upcoming</span></li>`);
    el.innerHTML = done.concat(upcoming).join("");
  }

  function render() {
    RD.renderPost(cfg, state);
    RD.renderStats(state);
    renderHistory();
    renderLookup();
  }

  async function refresh() {
    try {
      const s = await RD.api("/api/state");
      if (s.version !== version) {
        state = s;
        version = s.version;
        render();
      }
    } catch (e) {
      console.warn("Refresh failed", e);
    }
  }

  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  if (cfg.show_holder_names) lookup.placeholder = "Enter your ticket number or name";
  await refresh();
  lookup.addEventListener("input", renderLookup);
  setInterval(() => { if (!document.hidden) refresh(); }, cfg.refresh_seconds * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
