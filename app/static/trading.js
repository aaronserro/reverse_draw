(async function () {
  const $ = (id) => document.getElementById(id);
  const otp = $("traderOtp");
  const boxes = [...otp.querySelectorAll("input")];
  let cfg;
  let submitting = false;

  const codeValue = () => boxes.map((box) => box.value).join("");

  function syncForm() {
    boxes.forEach((box) => box.classList.toggle("filled", !!box.value));
    $("traderLoginButton").disabled =
      submitting || !$("traderName").value.trim() || codeValue().length !== 6;
  }

  function fillFrom(index, digits) {
    for (const digit of digits) {
      if (index >= boxes.length) break;
      boxes[index++].value = digit;
    }
    boxes[Math.min(index, boxes.length - 1)].focus();
    syncForm();
  }

  boxes.forEach((box, index) => {
    box.addEventListener("input", () => {
      const digits = box.value.replace(/\D/g, "");
      box.value = "";
      $("traderLoginError").textContent = "";
      otp.classList.remove("error");
      if (digits) fillFrom(index, digits);
      else syncForm();
    });
    box.addEventListener("keydown", (event) => {
      if (event.key === "Backspace" && !box.value && index > 0) {
        boxes[index - 1].value = "";
        boxes[index - 1].focus();
        event.preventDefault();
        syncForm();
      }
      if (event.key === "ArrowLeft" && index > 0) boxes[index - 1].focus();
      if (event.key === "ArrowRight" && index < boxes.length - 1) boxes[index + 1].focus();
    });
    box.addEventListener("paste", (event) => {
      event.preventDefault();
      const digits = (event.clipboardData.getData("text") || "")
        .replace(/\D/g, "")
        .slice(0, 6);
      if (digits) fillFrom(index, digits);
    });
    box.addEventListener("focus", () => box.select());
  });

  $("traderName").addEventListener("input", syncForm);

  function showLogin() {
    $("traderAccountView").classList.add("hidden");
    $("traderLoginView").classList.remove("hidden");
    $("traderLoginError").textContent = "";
    submitting = false;
    syncForm();
    if (window.matchMedia("(min-width: 801px)").matches) {
      setTimeout(() => $("traderName").focus(), 0);
    }
  }

  function showAccount(session) {
    $("traderLoginView").classList.add("hidden");
    $("traderAccountView").classList.remove("hidden");
    $("traderWelcome").textContent = `Welcome, ${session.name}`;
    $("traderTickets").innerHTML = session.tickets.map((ticket) =>
      `<div class="trader-ticket"><span class="ticket-number">#${RD.fmt(ticket.ticket)}</span>` +
      `<span class="tag">${RD.esc(ticket.status)}</span></div>`
    ).join("") || `<p class="muted">No tickets are currently assigned.</p>`;
  }

  $("traderLoginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || codeValue().length !== 6) return;
    submitting = true;
    syncForm();
    $("traderLoginButton").textContent = "Checking…";
    try {
      const session = await RD.api("/api/trading/login", {
        method: "POST",
        body: { name: $("traderName").value.trim(), code: codeValue() },
      });
      boxes.forEach((box) => (box.value = ""));
      showAccount(session);
    } catch (error) {
      $("traderLoginError").textContent = error.message;
      otp.classList.add("error", "shake");
      setTimeout(() => otp.classList.remove("shake"), 450);
      boxes.forEach((box) => (box.value = ""));
      boxes[0].focus();
    } finally {
      submitting = false;
      $("traderLoginButton").textContent = "Sign in securely";
      syncForm();
    }
  });

  $("traderLogout").addEventListener("click", async () => {
    await RD.api("/api/trading/logout", { method: "POST" }).catch(() => {});
    showLogin();
  });

  cfg = await RD.api("/api/config");
  RD.applyConfig(cfg);
  syncForm();

  try {
    const session = await RD.api("/api/trading/session");
    if (session.authenticated) showAccount(session);
    else showLogin();
  } catch (_) {
    showLogin();
  }
})();
