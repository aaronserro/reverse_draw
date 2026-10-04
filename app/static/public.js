(async function () {
  const $ = (id) => document.getElementById(id);
  let cfg, state = null, timer = null;

  // ================================================================ access code
  const otp = $("otp");
  const boxes = [...otp.querySelectorAll("input")];
  const codeValue = () => boxes.map((b) => b.value).join("");

  function syncOtp() {
    boxes.forEach((b) => b.classList.toggle("filled", !!b.value));
    $("codeSubmit").disabled = codeValue().length !== 6;
  }

  function fillFrom(i, digits) {
    for (const d of digits) {
      if (i >= boxes.length) break;
      boxes[i++].value = d;
    }
    boxes[Math.min(i, boxes.length - 1)].focus();
    syncOtp();
    if (codeValue().length === 6) submitCode();
  }

  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      const digits = box.value.replace(/\D/g, "");
      box.value = "";
      $("codeError").textContent = "";
      otp.classList.remove("error");
      if (digits) fillFrom(i, digits); else syncOtp();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) { boxes[i - 1].value = ""; boxes[i - 1].focus(); syncOtp(); e.preventDefault(); }
      if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
      if (e.key === "ArrowRight" && i < 5) boxes[i + 1].focus();
    });
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      const digits = (e.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, 6);
      if (digits) fillFrom(i, digits);
    });
    box.addEventListener("focus", () => box.select());
  });

  let submitting = false;
  async function submitCode() {
    if (submitting || codeValue().length !== 6) return;
    submitting = true;
    $("codeSubmit").disabled = true;
    $("codeSubmit").textContent = "Checking…";
    try {
      await RD.api("/api/access", { method: "POST", body: { code: codeValue() } });
      showApp();
    } catch (e) {
      $("codeError").textContent = e.message;
      otp.classList.add("error", "shake");
      setTimeout(() => otp.classList.remove("shake"), 450);
      boxes.forEach((b) => (b.value = ""));
      boxes[0].focus();
      syncOtp();
    } finally {
      submitting = false;
      $("codeSubmit").textContent = "Continue to the board";
    }
  }
  $("codeForm").addEventListener("submit", (e) => { e.preventDefault(); submitCode(); });

  function showGate() {
    clearInterval(timer);
    state = null;
    $("appView").classList.add("hidden");
    $("gateView").classList.remove("hidden");
    boxes.forEach((b) => (b.value = ""));
    syncOtp();
    if (window.matchMedia("(min-width: 801px)").matches) boxes[0].focus();
  }

  // ================================================================ landing copy
  const refreshText = () => `Results update every ${cfg.refresh_seconds} seconds`;

  // The landing screen has no draw state to work from, so everything comes
  // straight from config.py via /api/config.
  function renderLanding() {
    const s = cfg.schedule;
    const finalists = s.survivors[s.survivors.length - 1];
    const nRounds = s.labels.length;
    $("landingEyebrow").textContent = `${cfg.posted_in} · ${cfg.org_name}`;
    $("landingLead").textContent =
      `The ${cfg.org_name} starts with ${RD.fmt(s.total)} tickets and ends with ` +
      `${finalists === 1 ? `one ${s.completion_label.toLowerCase()}` : `${RD.fmt(finalists)} ${s.completion_label_plural.toLowerCase()}`}. ` +
      `Enter the code from your invitation to follow along.`;
    $("landingFacts").innerHTML = [
      [RD.fmt(s.total), "tickets to start"],
      [nRounds, nRounds === 1 ? "round" : "rounds"],
      [RD.fmt(finalists), finalists === 1 ? s.completion_label.toLowerCase() : s.completion_label_plural.toLowerCase()],
    ].map(([n, k]) => `<div class="fact"><span class="n">${n}</span><span class="k">${k}</span></div>`).join("");
    $("landingNote").textContent = cfg.closing_note;
    $("landingRefresh").textContent = refreshText();
  }

  function renderHowItWorks() {
    const s = cfg.schedule;
    const finalCount = s.survivors[s.survivors.length - 1];
    const finalLabel = s.labels[s.labels.length - 1];
    const prizeDraws = s.kinds.filter((kind) => kind === "prize").length;
    $("how1").textContent =
      `All ${RD.fmt(s.total)} tickets are in the running for ${cfg.prize_text}. Nobody has to do anything to stay in.`;
    $("how2").textContent =
      `Across ${s.labels.length} stages, elimination rounds alternate with ${prizeDraws} gift-card draws: ` +
      `${RD.fmt(s.total)} → ${s.survivors.map(RD.fmt).join(" → ")}. Gift-card winners leave the grand-prize pool.`;
    $("how3Title").textContent = finalCount === 1
      ? `One ${s.completion_label.toLowerCase()} remains`
      : `${RD.fmt(finalCount)} ${s.completion_label_plural.toLowerCase()} remain`;
    $("how3").textContent =
      `${finalLabel} leaves ${RD.fmt(finalCount)} ` +
      `${finalCount === 1 ? s.completion_label.toLowerCase() : s.completion_label_plural.toLowerCase()}. ` +
      `The online draw stops there. ${cfg.closing_note}`;
  }

  // ================================================================ board page
  const lookup = $("lookup");

  function renderHolderOptions() {
    const options = $("holderOptions");
    if (!options) return;
    const names = [...new Set(Object.values(state.holders || {}))]
      .sort((a, b) => a.localeCompare(b));
    options.innerHTML = names
      .map((name) => `<option value="${RD.esc(name)}"></option>`)
      .join("");
  }

  function renderLookup(fresh = null) {
    const hits = RD.search(lookup.value, state, state.holders);
    RD.renderBoard($("board"), cfg, state, { highlight: hits, holders: state.holders, fresh });
    const out = $("lookupResult");
    const q = lookup.value.trim();
    if (!q) { out.innerHTML = ""; return; }
    RD.store.set("rd_ticket", q);
    if (!hits.size) {
      out.innerHTML = `<div class="res none"><div class="res-icon">?</div><div><div class="res-title">No matching ticket or person</div>` +
        `<div class="res-sub">Try a ticket from 1 to ${RD.fmt(state.schedule.total)} or choose a name from the list.</div></div></div>`;
      return;
    }
    const list = [...hits].sort((a, b) => a - b);
    if (!/^#?\d+$/.test(q) && state.holders) {
      const people = new Map();
      list.forEach((ticket) => {
        const name = state.holders[ticket];
        if (!name) return;
        if (!people.has(name)) people.set(name, []);
        people.get(name).push(ticket);
      });
      out.innerHTML = [...people.entries()].map(([name, tickets]) => {
        const active = tickets.filter((ticket) =>
          ["in", "win"].includes(RD.ticketStatus(state, ticket).cls));
        const ticketList = tickets.map((ticket) => `#${ticket}`).join(", ");
        const status = active.length === tickets.length
          ? "All are still in"
          : active.length
            ? `${active.length} of ${tickets.length} still in`
            : "No tickets still in";
        return `<div class="res person"><div class="res-icon">${tickets.length}</div><div>` +
          `<div class="res-title">${RD.esc(name)}</div>` +
          `<div class="res-tickets">${RD.esc(ticketList)}</div>` +
          `<div class="res-sub">${RD.esc(status)}</div></div></div>`;
      }).join("");
      return;
    }
    out.innerHTML = list.slice(0, 20).map((t) => {
      const s = RD.ticketStatus(state, t);
      const who = state.holders && state.holders[t] ? ` · ${RD.esc(state.holders[t])}` : "";
      if (s.cls === "win")
        return `<div class="res win"><div class="res-icon">★</div><div><div class="res-title">Ticket #${t} is a ${RD.esc(state.schedule.completion_label.toLowerCase())}!</div><div class="res-sub">Congratulations${who}</div></div></div>`;
      if (s.cls === "prize")
        return `<div class="res prize"><div class="res-icon">★</div><div><div class="res-title">Ticket #${t} won a gift card!</div><div class="res-sub">${RD.esc(s.text)}${who} · Removed from the grand-prize pool</div></div></div>`;
      if (s.cls === "in") {
        const next = state.next_label ? `Next up: ${RD.esc(state.next_label)}` : "";
        return `<div class="res in"><div class="res-icon">✓</div><div><div class="res-title">Ticket #${t} is still in!</div><div class="res-sub">${next}${who}</div></div></div>`;
      }
      return `<div class="res out"><div class="res-icon">✕</div><div><div class="res-title">Ticket #${t} is out</div><div class="res-sub">${RD.esc(s.text)}${who}</div></div></div>`;
    }).join("") + (list.length > 20 ? `<div class="muted small" style="margin-top:8px">+${list.length - 20} more</div>` : "");
  }

  function renderStats() {
    const total = state.schedule.total;
    const left = RD.remaining(state);
    $("statIn").textContent = RD.fmt(left);
    $("statOut").textContent = RD.fmt(total - left);
    $("statRound").textContent = `${state.rounds_done} / ${state.schedule.survivors.length}`;
  }

  // One line per round, in draw order: rounds that have run show their real
  // numbers and timestamp, the rest show the target from the schedule.
  function renderRounds() {
    const s = state.schedule;
    $("history").innerHTML = s.labels.map((label, i) => {
      const rec = state.rounds[i];
      const final = i === s.labels.length - 1 ? " final" : "";
      if (rec) {
        const result = rec.kind === "prize"
          ? `${RD.esc(rec.prize || "Gift card")} winner: ${rec.selected_tickets.map((ticket) => `#${ticket}`).join(", ")}`
          : `${RD.fmt(rec.eliminated_count)} out`;
        return `<li class="${final.trim()}"><span class="dot">${i + 1}</span>` +
          `<span class="r-main"><b>${RD.esc(rec.label)}</b> · ${RD.fmt(rec.started_with)} → ${RD.fmt(rec.survivors)} ` +
          `<span class="r-sub">(${result})</span></span>` +
          `<span class="r-when">${RD.fmtTime(rec.timestamp)}</span></li>`;
      }
      const from = i === 0 ? s.total : s.survivors[i - 1];
      const to = s.survivors[i];
      const upcoming = s.kinds[i] === "prize"
        ? `1 ${RD.esc(s.prizes[i] || "gift card")} winner`
        : `${RD.fmt(from - to)} out`;
      return `<li class="todo${final}"><span class="dot">${i + 1}</span>` +
        `<span class="r-main"><b>${RD.esc(label)}</b> · ${RD.fmt(from)} → ${RD.fmt(to)} ` +
        `<span class="r-sub">(${upcoming})</span></span>` +
        `<span class="r-when"><span class="tag">${i === state.rounds_done ? "Up next" : "Upcoming"}</span></span></li>`;
    }).join("");
  }

  function renderPostHead() {
    const last = state.rounds[state.rounds.length - 1];
    $("postedAt").textContent = `Posted in ${cfg.posted_in}` + (last ? ` · ${RD.fmtTime(last.timestamp)}` : "");
    $("boardMeta").textContent = `${RD.fmt(state.schedule.total)} tickets · ${cfg.grid_columns} per row`;
  }

  function render(fresh = null) {
    RD.renderHero($("hero"), cfg, state);
    renderStats();
    renderPostHead();
    renderRounds();
    renderLookup(fresh);
    $("updated").textContent = `Updated ${RD.fmtClock(new Date())}`;
  }

  async function refresh() {
    try {
      const s = await RD.api("/api/state");
      if (!state || s.version !== state.version) {
        let fresh = null;
        if (state && s.rounds_done > state.rounds_done) {
          fresh = new Set();
          s.status.forEach((r, i) => { if (r === s.rounds_done) fresh.add(i + 1); });
          const last = s.rounds[s.rounds.length - 1];
          const resultCount = s.winners.length;
          const resultLabel = resultCount === 1
            ? s.schedule.completion_label.toLowerCase()
            : s.schedule.completion_label_plural.toLowerCase();
          RD.toast(s.finished
            ? `${RD.fmt(resultCount)} ${resultLabel} confirmed!`
            : `${last.label} results are in: ${RD.fmt(last.survivors)} tickets left`);
        }
        state = s;
        renderHolderOptions();
        render(fresh);
      } else {
        $("updated").textContent = `Updated ${RD.fmtClock(new Date())}`;
      }
    } catch (e) {
      if (e.status === 401) showGate();
      else console.warn("Refresh failed", e);
    }
  }

  async function showApp() {
    $("gateView").classList.add("hidden");
    $("appView").classList.remove("hidden");
    await refresh();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) refresh(); }, cfg.refresh_seconds * 1000);
  }

  lookup.addEventListener("input", () => renderLookup());
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state) refresh(); });
  $("leave").addEventListener("click", async () => {
    await RD.api("/api/access/logout", { method: "POST" }).catch(() => {});
    showGate();
  });

  // ================================================================ start
  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  $("legend").innerHTML = RD.legendHTML(
    "Your ticket",
    cfg.schedule.kinds.includes("prize"),
    cfg.schedule.completion_label,
  );
  $("closingNote").textContent = cfg.closing_note;
  $("refreshNote").textContent = refreshText();
  renderLanding();
  renderHowItWorks();
  if (cfg.show_holder_names) {
    lookup.placeholder = "Ticket number or person";
    lookup.setAttribute("inputmode", "text");
    $("lookupHint").textContent =
      "Search by ticket number or choose a person to see all of their tickets.";
  } else {
    $("holderOptions").remove();
  }
  const saved = RD.store.get("rd_ticket");
  if (saved) lookup.value = saved;

  const access = await RD.api("/api/access");
  if (!access.required) $("leave").classList.add("hidden");
  if (access.ok) showApp(); else showGate();
})();
