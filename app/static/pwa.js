// Install support shared by the public and admin pages.
(function () {
  const installButtons = [...document.querySelectorAll("[data-install-app]")];
  const standalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  let installPrompt = null;

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch((error) => {
        console.warn("App installation is unavailable", error);
      });
    });
  }

  if (standalone) return;

  function showButtons() {
    installButtons.forEach((button) => button.classList.remove("hidden"));
  }

  function showIosInstructions() {
    const dialog = document.createElement("dialog");
    dialog.className = "modal";
    dialog.innerHTML =
      `<div class="modal-body"><h3>Add Reverse Draw to Home Screen</h3>` +
      `<p>In Safari, tap the <b>Share</b> button, then choose <b>Add to Home Screen</b>.</p></div>` +
      `<div class="modal-actions"><button class="btn primary" type="button">Got it</button></div>`;
    document.body.appendChild(dialog);
    dialog.querySelector("button").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal();
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    showButtons();
  });

  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const isSafari = isIos && /safari/i.test(navigator.userAgent) && !/crios|fxios|edgios/i.test(navigator.userAgent);
  if (isSafari) showButtons();

  installButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      if (!installPrompt) {
        showIosInstructions();
        return;
      }
      installPrompt.prompt();
      await installPrompt.userChoice;
      installPrompt = null;
      installButtons.forEach((item) => item.classList.add("hidden"));
    });
  });

  window.addEventListener("appinstalled", () => {
    installPrompt = null;
    installButtons.forEach((button) => button.classList.add("hidden"));
  });
})();
