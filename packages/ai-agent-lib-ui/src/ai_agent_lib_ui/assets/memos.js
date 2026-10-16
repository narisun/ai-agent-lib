// A self-contained panel loaded by Chainlit's supported custom_js setting.
// It uses our authenticated HTTP endpoints and the public thread URL only.
(() => {
  function mount() {
    if (document.getElementById("agent-lab-memos")) return;
    const panel = document.createElement("aside");
    panel.id = "agent-lab-memos";
    panel.className = "agent-lab-surface";
    panel.hidden = true;
    panel.setAttribute("aria-label", "Memo panel");
    panel.innerHTML = `<header><h2>Memos</h2><button type="button" data-close aria-label="Close memo panel"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg></button></header>
      <label>Memo version <select aria-label="Memo version"></select></label>
      <div class="memo-actions"><button type="button" data-refresh>Refresh versions</button>
      <button type="button" data-older hidden>Older versions</button></div>
      <p role="status" aria-live="polite"></p>
      <details data-details hidden><summary>Memo details</summary><small data-meta></small></details>
      <article aria-label="Memo content" tabindex="0"></article>`;
    document.body.append(panel);
    const select = panel.querySelector("select");
    const status = panel.querySelector('[role="status"]');
    const article = panel.querySelector("article");
    const meta = panel.querySelector("[data-meta]");
    const older = panel.querySelector("[data-older]");
    const details = panel.querySelector("[data-details]");
    let thread = null, controller = null, next = null, selected = "", refreshed = 0;
    const versions = new Map();
    const currentThread = () => location.pathname.match(/^\/chat\/thread\/([A-Za-z0-9_-]{1,200})\/?$/)?.[1] || null;
    function reset() {
      controller?.abort();
      versions.clear(); select.replaceChildren(); article.replaceChildren();
      meta.textContent = ""; selected = ""; next = null; older.hidden = true;
      details.hidden = true; details.open = false;
    }
    function close() {
      panel.hidden = true; document.body.classList.remove("agent-lab-memos-open");
      reset();
    }
    async function read(url, signal, html = false) {
      const response = await fetch(url, {credentials: "same-origin", signal, redirect: "error"});
      if (!response.ok) throw new Error(response.status === 401 ? "Sign in again to view memos." : "Memos are unavailable. Check access or refresh this page.");
      return html ? response.text() : response.json();
    }
    async function content(signal) {
      const version = versions.get(select.value);
      if (!version || selected === version.id) return;
      article.replaceChildren(); meta.textContent = ""; status.textContent = "Loading memo…";
      // selected describes rendered content, not the last completed fetch. A
      // cancelled A -> B -> A switch must fetch A again after clearing it.
      selected = ""; details.hidden = true; details.open = false;
      const html = await read(`/api/threads/${encodeURIComponent(thread)}/documents/${encodeURIComponent(version.id)}`, signal, true);
      if (signal.aborted) return;
      // This endpoint renders Markdown with HTML and images disabled. Model text
      // never becomes markup directly in the browser.
      article.innerHTML = html;
      selected = version.id;
      details.hidden = false;
      meta.textContent = `Memo ${version.id} · Version ${version.version} · ${version.created_at} · Request ${version.request_id}`;
      status.textContent = "Viewing a saved version. Include its memo reference when asking about an older memo.";
    }
    async function load(append = false) {
      controller?.abort(); controller = new AbortController();
      const signal = controller.signal;
      refreshed = Date.now();
      if (!thread) {
        reset(); status.textContent = "Choose an agent and send a memo request first."; return;
      }
      try {
        const data = await read(`/api/threads/${encodeURIComponent(thread)}/documents${append && next ? `?before=${next}` : ""}`, signal);
        if (signal.aborted) return;
        for (const version of data.items) versions.set(version.id, version);
        // Refresh the newest page without dropping previously loaded older versions.
        if (append || versions.size <= 50) next = data.next_before;
        const previous = select.value;
        select.replaceChildren(...Array.from(versions.values()).sort((a, b) =>
          b.created_at.localeCompare(a.created_at) || b.version - a.version
        ).map(version => {
          const option = document.createElement("option"); option.value = version.id;
          option.textContent = `${version.title} · v${version.version}`; return option;
        }));
        if (versions.has(previous)) select.value = previous;
        older.hidden = !next;
        if (!versions.size) {
          status.textContent = "No saved memos yet. Ask this agent to generate a memo. Agents must publish document artifacts; older text-only replies remain in chat.";
          return;
        }
        await content(signal);
      } catch (error) {
        if (signal.aborted) return;
        reset(); status.textContent = error.message;
      }
    }
    function open() {
      thread = currentThread(); panel.hidden = false;
      document.body.classList.add("agent-lab-memos-open");
      void load(); panel.querySelector("[data-close]").focus();
    }
    document.addEventListener("click", event => {
      if (event.target.closest?.('a[href="#memos"]')) {
        event.preventDefault(); panel.hidden ? open() : close();
      }
    });
    panel.querySelector("[data-close]").addEventListener("click", () => {
      close(); document.querySelector('a[href="#memos"]')?.focus();
    });
    panel.addEventListener("keydown", event => {
      if (event.key === "Escape") {
        event.preventDefault(); close(); document.querySelector('a[href="#memos"]')?.focus();
      }
    });
    panel.querySelector("[data-refresh]").addEventListener("click", () => void load());
    older.addEventListener("click", () => void load(true));
    select.addEventListener("change", async () => {
      controller?.abort(); controller = new AbortController();
      const signal = controller.signal;
      try { await content(signal); }
      catch (error) { if (!signal.aborted) { reset(); status.textContent = error.message; } }
    });
    // Opening another thread clears all previous content immediately. Polling
    // while open refreshes metadata only, never the model or a selected old version.
    setInterval(() => {
      if (panel.hidden) return;
      if (currentThread() !== thread) { reset(); thread = currentThread(); void load(); }
      else if (!document.hidden && Date.now() - refreshed >= 5000) void load();
    }, 500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount, {once:true});
  else mount();
})();
