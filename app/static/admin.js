(async function () {
  const $ = (id) => document.getElementById(id);
  let cfg, state;
  let csvDirty = false;
  let notificationPreview = null;
  let emailPoll = null;

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
      loadSourceDataframe();
      loadNotifications();
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
      `<td class="small muted">${p.tickets.join(", ")}</td>` +
      `<td class="r"><button class="btn sm" data-unassign-holder="${encodeURIComponent(p.holder)}">Deallocate</button></td></tr>`).join("");
    $("people").innerHTML = state.summary.length
      ? `<tr><th>Holder</th><th class="r">Tickets</th><th class="r">Still in</th><th>Ticket numbers</th><th></th></tr>${rows}`
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

  // The server reads the workbook with pandas and returns ticket,name rows, so
  // the file's bytes must never reach the textarea.
  async function uploadHolderFile(file) {
    const picker = $("csvFile");
    const label = picker.closest("label");
    const originalLabel = label.firstChild.textContent;
    label.firstChild.textContent = "Loading…";
    picker.disabled = true;
    $("uploadPreview").innerHTML = "";
    try {
      const response = await fetch(`/api/admin/holders/file?filename=${encodeURIComponent(file.name)}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
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
      renderUploadPreview(result, file.name);
      renderSourceDataframe(result.dataframe);
      loadNotifications();
      RD.toast(`Loaded ${RD.fmt(result.imported)} ticket(s) for ${RD.fmt(result.people.length)} holder(s) from ${file.name}. Review, then Save or Merge.`);
    } catch (e) {
      if (e.status === 401) show("login");
      else RD.toast(e.message, true);
    } finally {
      label.firstChild.textContent = originalLabel;
      picker.disabled = false;
      picker.value = "";
    }
  }

  // Shows how the tickets were allocated per holder, before anything is saved.
  function renderUploadPreview(result, filename) {
    const rows = result.people.map((p) =>
      `<tr><td><b>${RD.esc(p.name)}</b></td><td class="r">${RD.fmt(p.tickets.length)}</td>` +
      `<td class="small muted">${p.tickets.join(", ")}</td></tr>`).join("");
    $("uploadPreview").innerHTML =
      `<div class="section-title">Allocation from ${RD.esc(filename)} — not saved yet</div>` +
      `<p class="hint">${RD.fmt(result.imported)} ticket(s) across ${RD.fmt(result.people.length)} holder(s), ` +
      `sorted by holder. Use Save or Merge above to apply it.</p>` +
      `<div class="scroll" style="max-height:260px"><table>` +
      `<tr><th>Holder</th><th class="r">Tickets</th><th>Ticket numbers</th></tr>${rows}</table></div>`;
  }

  function renderSourceDataframe(dataframe) {
    const container = $("sourceDataframe");
    if (!dataframe) {
      container.innerHTML = "";
      return;
    }
    const head = dataframe.columns.map((column) =>
      `<th>${RD.esc(column)}</th>`).join("");
    const body = dataframe.rows.map((row) =>
      `<tr>${row.map((value) => `<td>${RD.esc(value ?? "")}</td>`).join("")}</tr>`
    ).join("");
    container.innerHTML =
      `<div class="section-title">Saved source DataFrame</div>` +
      `<p class="hint"><b>${RD.esc(dataframe.filename)}</b> · ${RD.fmt(dataframe.row_count)} rows × ` +
      `${RD.fmt(dataframe.column_count)} columns. This is the complete uploaded sheet and is stored separately from ticket allocation.</p>` +
      `<div class="scroll dataframe-scroll"><table class="dataframe-table">` +
      `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  async function loadSourceDataframe() {
    try {
      const result = await RD.api("/api/admin/holders/dataframe");
      renderSourceDataframe(result.dataframe);
    } catch (e) {
      if (e.status !== 401) console.warn("Could not load source DataFrame", e);
    }
  }

  function ticketList(tickets) {
    return tickets.map((ticket) => `#${ticket}`).join(", ");
  }

  function renderNotifications(preview) {
    notificationPreview = preview;
    $("sendAllEmails").disabled = !preview.ready;
    $("clearEmailHistory").disabled = preview.sending || preview.history.length === 0;
    $("cancelEmails").classList.toggle("hidden", !preview.sending);
    const statusClass = preview.ready ? "ok" : preview.blocked.length ? "err" : "warn";
    $("emailStatus").innerHTML =
      `<div class="banner ${statusClass} email-status"><div><b>${RD.fmt(preview.pending_people)} pending recipient(s) · ` +
      `${RD.fmt(preview.pending_tickets)} new ticket(s)</b>` +
      `<br><span class="small">Sender: ${RD.esc(preview.sender || "not configured")}</span>` +
      `${preview.reason ? `<br>${RD.esc(preview.reason)}` : ""}` +
      `</div></div>`;

    const rows = preview.recipients.map((person) =>
      `<tr><td><b>${RD.esc(person.name)}</b><div class="small muted">${RD.esc(person.email)}</div></td>` +
      `<td><span class="ticket-list new">${ticketList(person.new_tickets)}</span></td>` +
      `<td class="small muted">${ticketList(person.all_tickets)}</td></tr>`).join("");
    const blocked = preview.blocked.map((person) =>
      `<tr class="blocked"><td><b>${RD.esc(person.name)}</b></td><td>${ticketList(person.tickets)}</td>` +
      `<td>${RD.esc(person.reason)}</td></tr>`).join("");
    $("emailRecipients").innerHTML =
      `<div class="section-title">Send preview</div>` +
      (rows ? `<div class="scroll"><table><tr><th>Recipient</th><th>New tickets</th><th>All current tickets</th></tr>${rows}</table></div>` : `<p class="muted">No recipients are currently pending.</p>`) +
      (blocked ? `<div class="section-title">Blocked — not included</div><div class="scroll"><table><tr><th>Holder</th><th>Tickets</th><th>Reason</th></tr>${blocked}</table></div>` : "");

    const history = preview.history.map((batch) => {
      const sent = batch.jobs.filter((job) => job.status === "sent").length;
      const issues = batch.jobs.length - sent;
      const details = batch.jobs.filter((job) => job.status !== "sent").map((job) => {
        const resolution = job.status === "unknown" ?
          ` <span class="email-resolution"><button class="btn sm" data-email-resolution="delivered" data-batch="${encodeURIComponent(batch.id)}" data-email="${encodeURIComponent(job.email)}">Mark delivered</button>` +
          `<button class="btn sm" data-email-resolution="retry" data-batch="${encodeURIComponent(batch.id)}" data-email="${encodeURIComponent(job.email)}">Allow retry</button></span>` : "";
        return `<li><b>${RD.esc(job.name)}</b> · ${RD.esc(job.status)}${job.error ? ` — ${RD.esc(job.error)}` : ""}${resolution}</li>`;
      }).join("");
      return `<details class="round"><summary><span><b>${RD.esc(batch.status)}</b> <span class="muted">· ${sent}/${batch.jobs.length} sent${issues ? ` · ${issues} need attention` : ""}</span></span><span class="muted small">${RD.fmtTime(batch.created_at)}</span></summary>` +
        `<div class="body small">${details ? `<ul class="email-errors">${details}</ul>` : "All messages were accepted by the email provider."}</div></details>`;
    }).join("");
    $("emailHistory").innerHTML = `<div class="section-title">Recent batches</div>${history || `<p class="muted">No email batches have been sent.</p>`}`;

    clearTimeout(emailPoll);
    if (preview.sending) {
      emailPoll = setTimeout(loadNotifications, 2000);
    }
  }

  async function loadNotifications() {
    try {
      renderNotifications(await RD.api("/api/admin/notifications/preview"));
    } catch (e) {
      if (e.status === 401) show("login");
      else RD.toast(e.message, true);
    }
  }

  $("csvFile").addEventListener("change", (ev) => {
    const file = ev.target.files[0];
    if (file) uploadHolderFile(file);
  });

  // Dropping a workbook on the page used to paste its raw bytes into the
  // textarea (or navigate away). Send it to the parser instead, wherever it lands.
  const dropZone = document.querySelector('[data-pane="holders"]');
  const hasFiles = (ev) => [...(ev.dataTransfer?.types || [])].includes("Files");

  document.addEventListener("dragover", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    dropZone.classList.add("dropping");
  });
  ["dragleave", "dragend"].forEach((name) =>
    document.addEventListener(name, () => dropZone.classList.remove("dropping")));
  document.addEventListener("drop", (ev) => {
    if (!hasFiles(ev)) return;
    ev.preventDefault();
    dropZone.classList.remove("dropping");
    const file = ev.dataTransfer.files[0];
    if (!file) return;
    showTab("holders");
    uploadHolderFile(file);
  });

  async function saveHolders(mode) {
    const ok = await act(() => RD.api("/api/admin/holders", { method: "PUT", body: { csv: $("holdersCsv").value, mode } }),
      (s) => `Saved ${RD.fmt(s.imported)} ticket holder(s)`);
    if (ok) { csvDirty = false; $("holdersCsv").value = holdersToCsv(); loadNotifications(); }
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

  $("refreshEmails").onclick = loadNotifications;
  $("clearEmailHistory").onclick = async () => {
    const ok = await RD.confirm({
      title: "Clear all email notification history?",
      body: "Every currently assigned ticket will become pending again and can be emailed again. Ticket assignments and uploaded data will not be deleted. Use this only for testing or when you intentionally need to resend every ticket email.",
      confirmText: "Clear email history",
      danger: true,
    });
    if (!ok) return;
    try {
      const result = await RD.api("/api/admin/notifications/clear-history", { method: "POST" });
      renderNotifications(result.preview);
      RD.toast(`Cleared ${RD.fmt(result.cleared_batches)} email batch(es). Current tickets can now be emailed again.`);
    } catch (e) { RD.toast(e.message, true); }
  };
  $("cancelEmails").onclick = async () => {
    const ok = await RD.confirm({
      title: "Cancel the sending batch?",
      body: "Messages already accepted by the provider remain sent. Messages not started will return to the pending list.",
      confirmText: "Cancel batch",
      danger: true,
    });
    if (!ok) return;
    try {
      renderNotifications(await RD.api("/api/admin/notifications/cancel", { method: "POST" }));
      RD.toast("Email batch canceled.");
    } catch (e) { RD.toast(e.message, true); }
  };
  $("sendAllEmails").onclick = async () => {
    if (!notificationPreview?.ready) return;
    const ok = await RD.confirm({
      title: `Email ${notificationPreview.pending_people} recipient(s)?`,
      body: `${notificationPreview.pending_tickets} new ticket(s) will be sent as separate personalized messages. Review the list before continuing.`,
      confirmText: "Send all emails",
    });
    if (!ok) return;
    $("sendAllEmails").disabled = true;
    try {
      const result = await RD.api("/api/admin/notifications/send-all", { method: "POST" });
      renderNotifications(result.preview);
      RD.toast(`Email batch started for ${RD.fmt(result.batch.jobs.length)} recipient(s).`);
    } catch (e) {
      RD.toast(e.message, true);
      loadNotifications();
    }
  };

  $("emailHistory").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-email-resolution]");
    if (!button) return;
    const delivered = button.dataset.emailResolution === "delivered";
    const ok = await RD.confirm({
      title: delivered ? "Mark this email delivered?" : "Allow this email to retry?",
      body: delivered
        ? "Only do this after confirming the message appears in the sender mailbox or reached the recipient. Its tickets will count as emailed."
        : "Only do this after confirming the message was not delivered. It will return to the next Send all pending batch.",
      confirmText: delivered ? "Mark delivered" : "Allow retry",
      danger: !delivered,
    });
    if (!ok) return;
    try {
      const preview = await RD.api("/api/admin/notifications/resolve-unknown", {
        method: "POST",
        body: {
          batch_id: decodeURIComponent(button.dataset.batch),
          email: decodeURIComponent(button.dataset.email),
          delivered,
        },
      });
      renderNotifications(preview);
      RD.toast(delivered ? "Email marked delivered." : "Email cleared for retry.");
    } catch (e) { RD.toast(e.message, true); }
  });

  $("assignBlock").onclick = async () => {
    const start = Number($("blockStart").value);
    const end = Number($("blockEnd").value || $("blockStart").value);
    const ok = await act(() => RD.api("/api/admin/holders/block", { method: "POST", body: { name: $("blockName").value, start, end } }),
      (s) => `Assigned ${RD.fmt(s.assigned)} ticket(s)`);
    if (ok) { csvDirty = false; $("holdersCsv").value = holdersToCsv(); $("blockName").value = $("blockStart").value = $("blockEnd").value = ""; }
  };

  async function unassignTickets(body, description) {
    const ok = await RD.confirm({
      title: `Deallocate ${description}?`,
      body: "The selected ticket assignments will be removed. Existing email delivery history will not be changed.",
      confirmText: "Deallocate",
      danger: true,
    });
    if (!ok) return false;
    const changed = await act(
      () => RD.api("/api/admin/holders/unassign", { method: "POST", body }),
      (s) => `Deallocated ${RD.fmt(s.unassigned)} ticket(s)`,
    );
    if (changed) {
      csvDirty = false;
      $("holdersCsv").value = holdersToCsv();
      loadNotifications();
    }
    return changed;
  }

  $("unassignBlock").onclick = () => {
    const start = Number($("unassignStart").value);
    if (!start) { RD.toast("Enter a ticket number to deallocate.", true); return; }
    const end = Number($("unassignEnd").value || start);
    unassignTickets({ start, end }, start === end ? `ticket #${start}` : `tickets #${start}–#${end}`).then((changed) => {
      if (changed) $("unassignStart").value = $("unassignEnd").value = "";
    });
  };

  $("people").addEventListener("click", (event) => {
    const button = event.target.closest("[data-unassign-holder]");
    if (!button) return;
    const name = decodeURIComponent(button.dataset.unassignHolder);
    unassignTickets({ name }, `all tickets for ${name}`);
  });

  $("search").addEventListener("input", renderSearch);
  $("peopleFilter").addEventListener("input", renderPeople);

  function showTab(name) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    document.querySelectorAll("[data-pane]").forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== name));
  }
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => showTab(btn.dataset.tab);
  });

  document.addEventListener("visibilitychange", () => { if (!document.hidden && state) load(); });

  // ================================================================ start
  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  $("legend").innerHTML = RD.legendHTML("Search match");
  $("roundStageLegend").innerHTML = RD.legendHTML();
  load();
})();
