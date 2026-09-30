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
    boxes[0].focus();
  }

  // ================================================================ landing copy
  const refreshText = () => `Results update every ${cfg.refresh_seconds} seconds`;

  // The landing screen has no draw state to work from, so everything comes
  // straight from config.py via /api/config.
  function renderLanding() {
    const s = cfg.schedule;
    const winners = s.survivors[s.survivors.length - 1];
    const nRounds = s.labels.length;
    $("landingEyebrow").textContent = `${cfg.posted_in} · ${cfg.org_name}`;
    $("landingLead").textContent =
      `The ${cfg.org_name} starts with ${RD.fmt(s.total)} tickets and ends with ` +
      `${winners === 1 ? "one winner" : `${RD.fmt(winners)} winners`} of ${cfg.prize_text}. ` +
      `Enter the code from your invitation to follow along.`;
    $("landingFacts").innerHTML = [
      [RD.fmt(s.total), "tickets to start"],
      [nRounds, nRounds === 1 ? "round" : "rounds"],
      [RD.fmt(winners), winners === 1 ? "winner" : "winners"],
    ].map(([n, k]) => `<div class="fact"><span class="n">${n}</span><span class="k">${k}</span></div>`).join("");
    $("landingNote").textContent = cfg.closing_note;
    $("landingRefresh").textContent = refreshText();
  }

  function renderHowItWorks() {
    const s = cfg.schedule;
    $("how1").textContent =
      `All ${RD.fmt(s.total)} tickets are in the running for ${cfg.prize_text}. Nobody has to do anything to stay in.`;
    $("how2").textContent =
      `Across ${s.labels.length} rounds the board is cut down: ` +
      `${RD.fmt(s.total)} → ${s.survivors.map(RD.fmt).join(" → ")}. Green squares are still in.`;
    $("how3").textContent = `The last ticket standing wins ${cfg.prize_text}. ${cfg.closing_note}`;
  }

  // ================================================================ board page
  const lookup = $("lookup");

  function renderLookup(fresh = null) {
    const hits = RD.search(lookup.value, state, state.holders);
    RD.renderBoard($("board"), cfg, state, { highlight: hits, holders: state.holders, fresh });
    const out = $("lookupResult");
    const q = lookup.value.trim();
    if (!q) { out.innerHTML = ""; return; }
    RD.store.set("rd_ticket", q);
    if (!hits.size) {
      out.innerHTML = `<div class="res none"><div class="res-icon">?</div><div><div class="res-title">No matching ticket</div>` +
        `<div class="res-sub">Tickets run from 1 to ${RD.fmt(state.schedule.total)}.</div></div></div>`;
      return;
    }
    const list = [...hits].sort((a, b) => a - b);
    out.innerHTML = list.slice(0, 20).map((t) => {
      const s = RD.ticketStatus(state, t);
      const who = state.holders && state.holders[t] ? ` · ${RD.esc(state.holders[t])}` : "";
      if (s.cls === "win")
        return `<div class="res win"><div class="res-icon">★</div><div><div class="res-title">Ticket #${t} is the winner!</div><div class="res-sub">Congratulations${who}</div></div></div>`;
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
        return `<li class="${final.trim()}"><span class="dot">${i + 1}</span>` +
          `<span class="r-main"><b>${RD.esc(rec.label)}</b> · ${RD.fmt(rec.started_with)} → ${RD.fmt(rec.survivors)} ` +
          `<span class="r-sub">(${RD.fmt(rec.eliminated_count)} out)</span></span>` +
          `<span class="r-when">${RD.fmtTime(rec.timestamp)}</span></li>`;
      }
      const from = i === 0 ? s.total : s.survivors[i - 1];
      const to = s.survivors[i];
      return `<li class="todo${final}"><span class="dot">${i + 1}</span>` +
        `<span class="r-main"><b>${RD.esc(label)}</b> · ${RD.fmt(from)} → ${RD.fmt(to)} ` +
        `<span class="r-sub">(${RD.fmt(from - to)} out)</span></span>` +
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
          RD.toast(s.finished ? "The winner has been drawn!" : `${last.label} results are in: ${RD.fmt(last.survivors)} tickets left`);
        }
        state = s;
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
  $("legend").innerHTML = RD.legendHTML("Your ticket");
  $("closingNote").textContent = cfg.closing_note;
  $("refreshNote").textContent = refreshText();
  renderLanding();
  renderHowItWorks();
  if (cfg.show_holder_names) {
    lookup.placeholder = "Ticket number or name";
    lookup.setAttribute("inputmode", "text");
    $("lookupHint").textContent = "Type your ticket number or name to find it on the board.";
  }
  const saved = RD.store.get("rd_ticket");
  if (saved) lookup.value = saved;

  const access = await RD.api("/api/access");
  if (!access.required) $("leave").classList.add("hidden");
  if (access.ok) showApp(); else showGate();
})();
