(async function () {
  const $ = (id) => document.getElementById(id);
  let cfg, state;
  let csvDirty = false;

  // ---------- views ----------
  function show(view) {
    $("loginView").classList.toggle("hidden", view !== "login");
    $("appView").classList.toggle("hidden", view !== "app");
  }

  async function load() {
    try {
      state = await RD.api("/api/admin/state");
      show("app");
      render();
    } catch (e) {
      if (e.status === 401) show("login");
      else RD.toast(e.message, true);
    }
  }

  // ---------- rendering ----------
  function holdersToCsv() {
    const lines = ["ticket,name"];
    Object.entries(state.holders)
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .forEach(([t, n]) => lines.push(`${t},${/[",\n]/.test(n) ? `"${n.replace(/"/g, '""')}"` : n}`));
    return lines.join("\n");
  }

  function renderControls() {
    const s = state.schedule;
    $("steps").innerHTML = s.labels.map((label, i) => {
      const cls = i < state.rounds_done ? "done" : i === state.rounds_done ? "next" : "";
      return `<li class="${cls}"><span class="dot"></span><span>${RD.esc(label)}: cut to ${s.survivors[i]}</span></li>`;
    }).join("");
    const run = $("runNext");
    run.disabled = state.finished;
    run.textContent = state.finished ? "Draw complete" : `Run ${state.next_label}`;
    $("undo").disabled = !state.started;
    $("warnNoDb").classList.toggle("hidden", !state.warn_no_db);
    $("warnSchedule").classList.toggle("hidden", !state.schedule_pending);
  }

  function renderSearch() {
    const q = $("search").value;
    const hits = RD.search(q, state, state.holders);
    RD.renderBoard($("board"), cfg, state, { highlight: hits, holders: state.holders });
    const el = $("searchResult");
    if (!q.trim()) { el.innerHTML = ""; return; }
    if (!hits.size) { el.innerHTML = `<span class="muted">No matching ticket or holder.</span>`; return; }
    const list = [...hits].sort((a, b) => a - b);
    const stillIn = list.filter((t) => !state.status[t - 1]).length;
    const rows = list.slice(0, 200).map((t) => {
      const s = RD.ticketStatus(state, t);
      return `<tr><td class="num">#${t}</td><td>${RD.esc(state.holders[t] || "—")}</td><td><span class="pill ${s.cls}">${s.text}</span></td></tr>`;
    }).join("");
    el.innerHTML = `<div class="small muted" style="margin-bottom:6px">${list.length} ticket(s), ${stillIn} still in</div>` +
      `<div class="scroll" style="max-height:220px"><table>${rows}</table></div>`;
  }

  function renderPeople() {
    const rows = state.summary.map((p) =>
      `<tr><td>${RD.esc(p.holder)}</td><td class="num">${p.tickets.length}</td><td class="num">${p.still_in}</td>` +
      `<td class="small muted">${p.tickets.join(", ")}</td></tr>`).join("");
    $("people").innerHTML = state.summary.length
      ? `<tr><th>Holder</th><th class="num">Tickets</th><th class="num">Still in</th><th>Ticket numbers</th></tr>${rows}`
      : `<tr><td class="muted">No holders assigned yet.</td></tr>`;
  }

  function roundBlock(r, extra = "") {
    const rows = r.eliminated.map((t) => `<tr><td class="num">#${t}</td><td>${RD.esc(state.holders[t] || "—")}</td></tr>`).join("");
    return `<details${extra}><summary><b>${RD.esc(r.label)}</b> — ${r.started_with} → ${r.survivors} ` +
      `(${r.eliminated_count} eliminated) · <span class="muted">${RD.fmtTime(r.undone_at || r.timestamp)}</span></summary>` +
      `<div class="small muted" style="margin:8px 0">Ran ${RD.fmtTime(r.timestamp)} · seed ${RD.esc(r.seed)}` +
      `${r.undone_at ? ` · undone ${RD.fmtTime(r.undone_at)}` : ""}</div>` +
      `<div class="scroll" style="max-height:300px"><table><tr><th class="num">Ticket</th><th>Holder</th></tr>${rows}</table></div></details>`;
  }

  function renderLog() {
    let html = state.rounds.length ? "" : `<p class="muted">No rounds run yet.</p>`;
    html += state.rounds.slice().reverse().map((r, i) => roundBlock(r, i === 0 ? " open" : "")).join("");
    if (state.undone.length) {
      html += `<h2 style="margin-top:20px">Undone rounds (audit trail)</h2>` +
        state.undone.slice().reverse().map((r) => roundBlock(r)).join("");
    }
    $("log").innerHTML = html;
  }

  function render() {
    RD.renderPost(cfg, state);
    RD.renderStats(state);
    renderControls();
    renderSearch();
    renderPeople();
    renderLog();
    if (!csvDirty) $("holdersCsv").value = holdersToCsv();
  }

  // ---------- actions ----------
  async function act(fn, okMsg) {
    try {
      state = await fn();
      render();
      if (okMsg) RD.toast(typeof okMsg === "function" ? okMsg(state) : okMsg);
    } catch (e) {
      if (e.status === 401) { show("login"); return; }
      RD.toast(e.message, true);
      load();
    }
  }

  $("loginForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await RD.api("/api/admin/login", { method: "POST", body: { password: $("password").value } });
      $("password").value = "";
      await load();
    } catch (e) { RD.toast(e.message, true); }
  });

  $("logout").onclick = async () => { await RD.api("/api/admin/logout", { method: "POST" }); show("login"); };

  $("runNext").onclick = () => {
    const i = state.rounds_done;
    const pool = state.schedule.total - state.status.filter((r) => r > 0).length;
    const cut = pool - state.schedule.survivors[i];
    if (!confirm(`Run ${state.next_label}? This eliminates ${cut} tickets, leaving ${state.schedule.survivors[i]}.`)) return;
    const btn = $("runNext");
    btn.disabled = true;
    act(() => RD.api("/api/admin/rounds/next", { method: "POST", body: { expected_rounds_done: i } }),
      (s) => (s.finished ? "Draw complete — we have a winner!" : `${s.rounds[s.rounds.length - 1].label} done`));
  };

  $("undo").onclick = () => {
    const last = state.rounds[state.rounds.length - 1];
    if (!confirm(`Undo ${last.label}? Its ${last.eliminated_count} tickets go back in. The undo is recorded in the audit trail.`)) return;
    act(() => RD.api("/api/admin/rounds/undo", { method: "POST", body: { expected_rounds_done: state.rounds_done } }), "Round undone");
  };

  $("reset").onclick = () => act(() => RD.api("/api/admin/reset", {
    method: "POST", body: { keep_holders: $("keepHolders").checked, confirm: $("resetConfirm").value.trim() },
  }), () => { $("resetConfirm").value = ""; return "Draw reset"; });

  $("holdersCsv").addEventListener("input", () => { csvDirty = true; });

  $("csvFile").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    $("holdersCsv").value = (await f.text()).replace(/^﻿/, "");
    csvDirty = true;
    ev.target.value = "";
    RD.toast("File loaded — review it, then Save or Merge");
  });

  function saveHolders(mode) {
    csvDirty = false;
    act(() => RD.api("/api/admin/holders", { method: "PUT", body: { csv: $("holdersCsv").value, mode } }),
      (s) => `Saved ${s.imported} ticket holder(s)`);
  }
  $("saveHolders").onclick = () => {
    if (confirm("Replace ALL ticket holders with this list?")) saveHolders("replace");
  };
  $("mergeHolders").onclick = () => saveHolders("merge");

  $("assignBlock").onclick = () => act(() => RD.api("/api/admin/holders/block", {
    method: "POST",
    body: { name: $("blockName").value, start: Number($("blockStart").value), end: Number($("blockEnd").value || $("blockStart").value) },
  }), (s) => { csvDirty = false; $("holdersCsv").value = holdersToCsv(); return `Assigned ${s.assigned} ticket(s)`; });

  $("search").addEventListener("input", renderSearch);

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b === btn));
      document.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== btn.dataset.tab));
    };
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden && state) load(); });

  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  load();
})();
