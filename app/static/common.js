// Shared helpers for the public and admin pages.

const RD = {};

RD.api = async function (url, opts = {}) {
  const res = await fetch(url, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
};

RD.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

RD.fmtTime = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
};

RD.applyConfig = function (cfg) {
  const r = document.documentElement.style;
  r.setProperty("--c-active", cfg.colors.active);
  r.setProperty("--c-out", cfg.colors.eliminated);
  r.setProperty("--c-last", cfg.colors.last_round);
  r.setProperty("--c-win", cfg.colors.winner);
  r.setProperty("--c-hl", cfg.colors.highlight);
};

// Status of one ticket: {cls, text}
RD.ticketStatus = function (state, t) {
  const r = state.status[t - 1];
  if (r) return { cls: "out", text: `Eliminated in ${state.schedule.labels[r - 1]}` };
  if (state.finished) return { cls: "win", text: "WINNER" };
  return { cls: "in", text: "Still in" };
};

RD.renderBoard = function (el, cfg, state, { highlight = null, holders = null } = {}) {
  el.style.gridTemplateColumns = `repeat(${cfg.grid_columns}, 1fr)`;
  const winners = new Set(state.winners.map((w) => w.ticket));
  const last = state.rounds_done;
  const parts = [];
  for (let t = 1; t <= state.schedule.total; t++) {
    const r = state.status[t - 1];
    let cls = winners.has(t) ? "win" : !r ? "active" : cfg.highlight_last_round && r === last ? "last" : "out";
    if (highlight && highlight.has(t)) cls += " hl";
    const who = holders && holders[t] ? ` · ${holders[t]}` : "";
    const tip = `#${t}${who} · ${RD.ticketStatus(state, t).text}`;
    parts.push(`<div class="cell ${cls}" title="${RD.esc(tip)}">${t}</div>`);
  }
  el.innerHTML = parts.join("");
  el.classList.toggle("searching", !!(highlight && highlight.size));
};

RD.announcement = function (cfg, state) {
  const total = state.schedule.total;
  if (!state.started) {
    return {
      headline: "Are you in it?",
      text: `The Reverse Draw starts with ${total} tickets. Every green square is in the running for ${cfg.prize_text}. Good luck!`,
    };
  }
  if (state.finished) {
    const names = state.winners
      .map((w) => `#${w.ticket}${w.holder ? ` (${w.holder})` : ""}`)
      .join(", ");
    return {
      headline: "We have a winner!",
      text: `Congratulations to ticket ${names}, who takes home ${cfg.prize_text}!`,
    };
  }
  const last = state.rounds[state.rounds.length - 1];
  return {
    headline: "Are you still in it?",
    text: `The Reverse Draw started with ${total} tickets. In ${last.label}, ${last.eliminated_count} tickets were pulled out. ` +
      `If your ticket number is in green in the image below, you're still in it, and still have a chance to win up to ${cfg.prize_text}! Good luck!`,
  };
};

RD.renderPost = function (cfg, state) {
  const a = RD.announcement(cfg, state);
  const last = state.rounds[state.rounds.length - 1];
  document.getElementById("postedIn").textContent = cfg.posted_in;
  document.getElementById("avatar").textContent = cfg.org_initials;
  document.getElementById("orgName").textContent = cfg.org_name;
  document.getElementById("stamp").textContent = last ? RD.fmtTime(last.timestamp) : "";
  document.getElementById("headline").textContent = a.headline;
  document.getElementById("announce").textContent = a.text;
  document.getElementById("closing").textContent = cfg.closing_note;
};

RD.renderStats = function (state) {
  const out = state.status.filter((r) => r > 0).length;
  document.getElementById("statIn").textContent = state.schedule.total - out;
  document.getElementById("statOut").textContent = out;
  document.getElementById("statRound").textContent = `${state.rounds_done} / ${state.schedule.survivors.length}`;
};

// Parse a search box: returns Set of matching tickets (by number, or by name if holders given)
RD.search = function (query, state, holders) {
  const q = query.trim().toLowerCase();
  const hits = new Set();
  if (!q) return hits;
  if (/^\d+$/.test(q)) {
    const t = parseInt(q, 10);
    if (t >= 1 && t <= state.schedule.total) hits.add(t);
    return hits;
  }
  if (holders) {
    for (const [t, name] of Object.entries(holders)) {
      if (name.toLowerCase().includes(q)) hits.add(Number(t));
    }
  }
  return hits;
};

RD.toast = function (msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isErr ? 5000 : 2500);
};
