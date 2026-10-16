// Keep form errors on the page; use same-origin requests for local UI actions.
const status = document.querySelector("[data-form-status]");
for (const form of document.querySelectorAll("form")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const parameters = new URLSearchParams(new FormData(form));
    // A submit button named "action" shadows form.action in the DOM.
    const target = new URL(form.getAttribute("action") || window.location.href, window.location.href);
    if (event.submitter?.name) {
      parameters.set(event.submitter.name, event.submitter.value);
    }
    if ((form.getAttribute("method") || "get").toLowerCase() === "get") {
      target.search = parameters.toString();
      window.location.assign(target.href);
      return;
    }
    status.textContent = "Saving…";
    try {
      const response = await fetch(target.href, {
        method: "POST", credentials: "same-origin", body: parameters,
      });
      if (response.ok) {
        if (target.pathname === "/login") {
          // Establish the mounted chat session before opening a saved thread.
          // Otherwise the chat shell's first-login navigation opens a new chat.
          const chatSession = await fetch("/chat/auth/header", {
            method: "POST", credentials: "same-origin",
          });
          if (!chatSession.ok) {
            status.textContent = "Could not open the chat session. Reload this page and sign in again.";
            return;
          }
        }
        window.location.assign(response.url);
      } else {
        status.textContent = response.status === 401
          ? "Sign-in failed or expired. Check your local access code and sign in again."
          : "The change could not be saved. Reload the page and check the current state.";
      }
    } catch {
      status.textContent = "Cannot reach the UI server. Check that it is still running.";
    }
  });
}
