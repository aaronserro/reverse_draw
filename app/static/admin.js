(async function () {
  const $ = (id) => document.getElementById(id);
  let cfg, state;
  let csvDirty = false;

  // ================================================================ views
  function show(view) {
    $("loginView").classList.toggle("hidden", view !== "login");
    $("appView").classList.toggle("hidden", view !== "app");
    if (view === "login") setTimeout(() => $("password").focus(), 0);
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

  // ================================================================ rendering
  function holdersToCsv() {
    const lines = ["ticket,name"];
    Object.entries(state.holders)
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .forEach(([t, n]) => lines.push(`${t},${/[",\n]/.test(n) ? `"${n.replace(/"/g, '""')}"` : n}`));
    return lines.join("\n");
  }

  function renderControls() {
    const s = state.schedule;
    $("vtrack").innerHTML = s.labels.map((label, i) => {
      const cls = i < state.rounds_done ? "done" : i === state.rounds_done ? "current" : "";
      const dot = i < state.rounds_done ? "✓" : i + 1;
      return `<li class="${cls}"><span class="dot">${dot}</span><span class="name">${RD.esc(label)}</span><span class="cnt">→ ${RD.fmt(s.survivors[i])}</span></li>`;
    }).join("");

    const left = RD.remaining(state);
    if (state.finished) {
      const w = state.winners.map((x) => `#${x.ticket}${x.holder ? ` · ${RD.esc(x.holder)}` : ""}`).join(", ");
      $("nextBox").innerHTML = `<div class="k">Draw complete</div><div class="v">🏆 Winner ${w}</div>`;
    } else {
      const target = s.survivors[state.rounds_done];
      $("nextBox").innerHTML = `<div class="k">Up next</div><div class="v">${RD.esc(state.next_label)}</div>` +
        `<div class="s">${RD.fmt(left)} → ${RD.fmt(target)} · ${RD.fmt(left - target)} tickets will be eliminated</div>`;
    }
    const run = $("runNext");
    run.disabled = state.finished;
    run.textContent = state.finished ? "Draw complete" : `Run ${state.next_label}`;
    $("undo").disabled = !state.started;

    $("warnNoDb").classList.toggle("hidden", !state.warn_no_db);
    $("warnSchedule").classList.toggle("hidden", !state.schedule_pending);
    $("warnCode").classList.toggle("hidden", !!state.public_code_set);
    $("storageInfo").textContent = state.storage === "postgres" ? "Connected to database" : "Local SQLite file";
  }

  function renderStats() {
    const total = state.schedule.total;
    const left = RD.remaining(state);
    $("statIn").textContent = RD.fmt(left);
    $("statOut").textContent = RD.fmt(total - left);
    $("statRound").textContent = `${state.rounds_done}/${state.schedule.survivors.length}`;
    $("statHolders").textContent = `${RD.fmt(Object.keys(state.holders).length)}/${RD.fmt(total)}`;
    $("statBar").style.width = `${Math.max(0.5, (left / total) * 100)}%`;
  }

  function renderSearch() {
    const q = $("search").value;
    const hits = RD.search(q, state, state.holders);
    RD.renderBoard($("board"), cfg, state, { highlight: hits, holders: state.holders });
    const el = $("searchResult");
    if (!q.trim()) { el.innerHTML = ""; return; }
    if (!hits.size) { el.innerHTML = `<div class="muted small">No matching ticket or holder.</div>`; return; }
    const list = [...hits].sort((a, b) => a - b);
    const stillIn = list.filter((t) => !state.status[t - 1]).length;
    const rows = list.slice(0, 300).map((t) => {
      const s = RD.ticketStatus(state, t);
      return `<tr><td class="r" style="width:80px">#${t}</td><td>${RD.esc(state.holders[t] || "—")}</td><td><span class="tag ${s.cls}">${s.text}</span></td></tr>`;
    }).join("");
    el.innerHTML = `<div class="small muted" style="margin-bottom:8px"><b style="color:var(--text)">${list.length}</b> ticket(s) · <b style="color:var(--brand-hi)">${stillIn}</b> still in</div>` +
      `<div class="scroll" style="max-height:230px"><table><tr><th class="r">Ticket</th><th>Holder</th><th>Status</th></tr>${rows}</table></div>`;
  }

  function renderPeople() {
    const f = $("peopleFilter").value.trim().toLowerCase();
    const people = state.summary.filter((p) => !f || p.holder.toLowerCase().includes(f));
    const rows = people.map((p) =>
      `<tr><td><b>${RD.esc(p.holder)}</b></td><td class="r">${p.tickets.length}</td>` +
      `<td class="r"><span class="tag ${p.still_in ? "in" : "out"}">${p.still_in}</span></td>` +
      `<td class="small muted">${p.tickets.join(", ")}</td></tr>`).join("");
    $("people").innerHTML = state.summary.length
      ? `<tr><th>Holder</th><th class="r">Tickets</th><th class="r">Still in</th><th>Ticket numbers</th></tr>${rows}`
      : `<tr><td class="muted">No holders assigned yet. Add them on the Ticket holders tab.</td></tr>`;
  }

  function roundBlock(r, open, undone = false) {
    const rows = r.eliminated.map((t) => `<tr><td class="r" style="width:80px">#${t}</td><td>${RD.esc(state.holders[t] || "—")}</td></tr>`).join("");
    return `<details class="round${undone ? " undone" : ""}"${open ? " open" : ""}>` +
      `<summary><span><b>${RD.esc(r.label)}</b> <span class="muted">· ${RD.fmt(r.started_with)} → ${RD.fmt(r.survivors)} · ${RD.fmt(r.eliminated_count)} eliminated</span></span>` +
      `<span class="muted small">${undone ? "Undone " + RD.fmtTime(r.undone_at) : RD.fmtTime(r.timestamp)}</span></summary>` +
      `<div class="body"><div class="small muted" style="margin-bottom:10px">Ran ${RD.fmtTime(r.timestamp)} · random seed <code>${RD.esc(r.seed)}</code></div>` +
      `<div class="scroll" style="max-height:300px"><table><tr><th class="r">Ticket</th><th>Holder</th></tr>${rows}</table></div></div></details>`;
  }

  function renderLog() {
    let html = state.rounds.length ? "" : `<p class="muted">No rounds run yet.</p>`;
    html += state.rounds.slice().reverse().map((r, i) => roundBlock(r, i === 0)).join("");
    if (state.undone.length) {
      html += `<div class="section-title">Undone rounds (audit trail)</div>` +
        state.undone.slice().reverse().map((r) => roundBlock(r, false, true)).join("");
    }
    $("log").innerHTML = html;
  }

  function render() {
    RD.renderHero($("hero"), cfg, state);
    renderControls();
    renderStats();
    renderSearch();
    renderPeople();
    renderLog();
    if (!csvDirty) $("holdersCsv").value = holdersToCsv();
  }

  function renderRoundStage(stageState, fresh = null) {
    RD.renderBoard($("roundStageBoard"), cfg, stageState, { holders: stageState.holders, fresh });
  }

  function openRoundStage() {
    if (state.finished) return;
    const i = state.rounds_done;
    const left = RD.remaining(state);
    const target = state.schedule.survivors[i];
    const stage = $("roundStage");

    $("roundStageEyebrow").textContent = "Ready to draw";
    $("roundStageTitle").textContent = state.next_label;
    $("roundStageSummary").textContent = `${RD.fmt(left - target)} tickets will be eliminated, leaving ${RD.fmt(target)}.`;
    $("roundStageRun").textContent = `Run ${state.next_label}`;
    $("roundStageRun").disabled = false;
    $("roundStageRun").classList.remove("hidden");
    $("drawAnimation").classList.add("hidden");
    $("roundStageBoard").classList.remove("drawing");
    renderRoundStage(state);
    stage.dataset.round = String(i);
    stage.showModal();
  }

  function drawingDelay() {
    return new Promise((resolve) => {
      let count = 4;
      $("drawCount").textContent = count;
      const timer = setInterval(() => {
        count -= 1;
        if (count > 0) $("drawCount").textContent = count;
      }, 1000);
      setTimeout(() => {
        clearInterval(timer);
        resolve();
      }, 4000);
    });
  }

  // ================================================================ actions
  async function act(fn, okMsg) {
    try {
      state = await fn();
      render();
      if (okMsg) RD.toast(typeof okMsg === "function" ? okMsg(state) : okMsg);
      return true;
    } catch (e) {
      if (e.status === 401) { show("login"); return false; }
      RD.toast(e.message, true);
      load();
      return false;
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

  $("logout").onclick = async () => { await RD.api("/api/admin/logout", { method: "POST" }).catch(() => {}); show("login"); };

  $("runNext").onclick = openRoundStage;

  $("roundStageClose").onclick = () => $("roundStage").close();

  $("roundStageRun").onclick = async () => {
    const i = Number($("roundStage").dataset.round);
    const target = state.schedule.survivors[i];
    const stageButton = $("roundStageRun");
    const mainButton = $("runNext");

    stageButton.disabled = true;
    stageButton.textContent = "Drawing…";
    mainButton.disabled = true;
    $("roundStageEyebrow").textContent = "Round in progress";
    $("roundStageSummary").textContent = "Randomly selecting the eliminated tickets.";
    $("roundStageBoard").classList.add("drawing");
    $("drawAnimation").classList.remove("hidden");

    const request = RD.api("/api/admin/rounds/next", { method: "POST", body: { expected_rounds_done: i } });
    const [result] = await Promise.allSettled([request, drawingDelay()]);

    $("roundStageBoard").classList.remove("drawing");
    $("drawAnimation").classList.add("hidden");
    if (result.status === "rejected") {
      stageButton.disabled = false;
      stageButton.textContent = "Try again";
      $("roundStageEyebrow").textContent = "Round not run";
      $("roundStageSummary").textContent = result.reason.message;
      mainButton.disabled = false;
      RD.toast(result.reason.message, true);
      await load();
      return;
    }

    state = result.value;
    const round = state.rounds[state.rounds.length - 1];
    const fresh = new Set(round.eliminated);
    render();
    renderRoundStage(state, fresh);
    $("roundStageEyebrow").textContent = state.finished ? "Draw complete" : `${round.label} complete`;
    $("roundStageTitle").textContent = state.finished ? "We have a winner!" : `${RD.fmt(target)} tickets remain`;
    $("roundStageSummary").textContent = `${RD.fmt(round.eliminated_count)} tickets were eliminated in this round.`;
    stageButton.classList.add("hidden");
    RD.toast(state.finished ? "🏆 Draw complete. We have a winner!" : `${round.label} done: ${RD.fmt(target)} tickets left`);
  };

  $("undo").onclick = async () => {
    const last = state.rounds[state.rounds.length - 1];
    const ok = await RD.confirm({
      title: `Undo ${last.label}?`,
      body: `Its ${RD.fmt(last.eliminated_count)} tickets go back in. The undone round stays visible in the audit trail.`,
      confirmText: "Undo round", danger: true,
    });
    if (ok) act(() => RD.api("/api/admin/rounds/undo", { method: "POST", body: { expected_rounds_done: state.rounds_done } }), "Round undone");
  };

  $("reset").onclick = async () => {
    const keep = $("keepHolders").checked;
    const ok = await RD.confirm({
      title: "Reset the whole draw?",
      body: `Every round will be cleared${keep ? " (ticket holders are kept)" : " and all ticket holders deleted"}. This can't be undone.`,
      confirmText: "Reset draw", danger: true, requireText: "RESET",
    });
    if (ok) act(() => RD.api("/api/admin/reset", { method: "POST", body: { keep_holders: keep, confirm: "RESET" } }), "Draw reset");
  };

  $("holdersCsv").addEventListener("input", () => { csvDirty = true; });

  $("csvFile").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    const picker = ev.target;
    const label = picker.closest("label");
    const originalLabel = label.firstChild.textContent;
    label.firstChild.textContent = "Loading…";
    picker.disabled = true;
    try {
      const response = await fetch(`/api/admin/holders/file?filename=${encodeURIComponent(f.name)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream" },
        body: f,
      });
      const result = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error((result && result.detail) || `Upload failed (${response.status})`);
        error.status = response.status;
        throw error;
      }
      $("holdersCsv").value = result.csv;
      if (!result.imported) throw new Error("No valid ticket holders were found in the uploaded data.");
      csvDirty = true;
      RD.toast(`Loaded ${result.imported} ticket holder(s) from ${f.name}. Review, then Save or Merge.`);
    } catch (e) {
      if (e.status === 401) show("login");
      else RD.toast(e.message, true);
    } finally {
      label.firstChild.textContent = originalLabel;
      picker.disabled = false;
      picker.value = "";
    }
  });

  async function saveHolders(mode) {
    const ok = await act(() => RD.api("/api/admin/holders", { method: "PUT", body: { csv: $("holdersCsv").value, mode } }),
      (s) => `Saved ${RD.fmt(s.imported)} ticket holder(s)`);
    if (ok) { csvDirty = false; $("holdersCsv").value = holdersToCsv(); }
  }
  $("saveHolders").onclick = async () => {
    const ok = await RD.confirm({
      title: "Replace all ticket holders?",
      body: "Every current holder is replaced with exactly the list in the box. Tickets not in the list become unassigned.",
      confirmText: "Replace all",
    });
    if (ok) saveHolders("replace");
  };
  $("mergeHolders").onclick = () => saveHolders("merge");

  $("assignBlock").onclick = async () => {
    const start = Number($("blockStart").value);
    const end = Number($("blockEnd").value || $("blockStart").value);
    const ok = await act(() => RD.api("/api/admin/holders/block", { method: "POST", body: { name: $("blockName").value, start, end } }),
      (s) => `Assigned ${RD.fmt(s.assigned)} ticket(s)`);
    if (ok) { csvDirty = false; $("holdersCsv").value = holdersToCsv(); $("blockName").value = $("blockStart").value = $("blockEnd").value = ""; }
  };

  $("search").addEventListener("input", renderSearch);
  $("peopleFilter").addEventListener("input", renderPeople);

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b === btn));
      document.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== btn.dataset.tab));
    };
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden && state) load(); });

  // ================================================================ start
  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  $("legend").innerHTML = RD.legendHTML("Search match");
  $("roundStageLegend").innerHTML = RD.legendHTML();
  load();
})();
