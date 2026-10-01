// Trading dashboard — the signed-in holder's real position in the draw.
//
// Everything on this page comes from /api/trading/session: the holder's name,
// their tickets and each ticket's in/out state, and the draw's stage and
// still-in counts.
//
// There is no market, order or trade backend. The order book, the transaction
// feed and the three price tiles therefore say they have nothing to show
// instead of standing in invented numbers, and the page is read-only: a holder
// cannot quote, request or approve anything yet. Wiring those panels up means
// adding real /api/trading/* endpoints and rendering their responses here.

(async function () {
  const $ = (id) => document.getElementById(id);
  const esc = RD.esc;

  let session = null;

  // ---------------------------------------------------------------- header
  function renderHead() {
    const d = session.draw;
    const stage = d.finished ? d.rounds_total : Math.min(d.rounds_done + 1, d.rounds_total);
    $("tdStage").innerHTML =
      `STAGE ${stage} / ${d.rounds_total}` +
      (d.rounds_done
        ? `<span class="td-only-d"> · ROUND ${d.rounds_done} DONE</span>`
        : "");
    $("tdWho").textContent = session.name;
    const dots = Math.min(d.rounds_total, 10);
    $("tdDots").innerHTML = Array.from(
      { length: dots },
      (_, i) => `<i class="${i < d.rounds_done ? "on" : ""}"></i>`
    ).join("");
  }

  // ---------------------------------------------------------------- stats
  function renderStats() {
    $("tdStubs").innerHTML = (session.tickets || []).map((t) => {
      const kind = t.active ? "in" : "out";
      return `<div class="td-stub ${kind}" title="${esc(t.status)}">` +
        `<b>${t.ticket}</b><span>${t.active ? "IN" : "OUT"}</span></div>`;
    }).join("") || `<span class="td-empty">No tickets assigned</span>`;

    $("tdStillIn").textContent = RD.fmt(session.draw.still_in);
    $("tdStillInOf").textContent = `of ${RD.fmt(session.draw.total)}`;
    // Best bid, low ask and last trade have no source to read, so they keep
    // the "--" the markup ships with rather than being filled in.
  }

  $("tdSignout").addEventListener("click", async () => {
    await RD.api("/api/trading/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/trading/login";
  });

  // ---------------------------------------------------------------- boot
  function failBoot(message) {
    $("tdBootMsg").textContent = message;
    $("tdBootLink").hidden = false;
  }

  try {
    const cfg = await RD.api("/api/config");
    RD.applyConfig(cfg);
  } catch (_) {
    // Static branding in the markup is a fine fallback.
  }

  try {
    session = await RD.api("/api/trading/session");
  } catch (_) {
    failBoot("We could not check your session. Please sign in again.");
    return;
  }

  if (!session.authenticated) {
    failBoot("Sign in as a ticket holder to open the trading dashboard.");
    return;
  }

  renderHead();
  renderStats();
  $("tdBoot").hidden = true;
  $("tdApp").hidden = false;
})();
