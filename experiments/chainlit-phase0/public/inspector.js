const runId = window.location.pathname.split("/").pop();
const explanations = {
  models: "The second model call includes the tool result. A follow-up also includes the saved conversation in its input.",
  checkpoints: "Compare the saved messages before and after this turn. These are scripted fixtures, not a live LangGraph database.",
  tools: "The tool-call ID links this synthetic result to the next model input.",
  logs: "The request and thread identifiers connect this event to the conversation."
};
async function load() {
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
  if (!response.ok) throw new Error("This run is unavailable for the current session.");
  const run = await response.json();
  document.querySelector("#back").href = `/chat/thread/${encodeURIComponent(run.thread_id)}`;
  document.querySelector("#status").textContent = `Status: ${run.status} · Request: ${run.id}`;
  const metrics = run.result.metrics || {};
  for (const [label, value] of [["Input tokens", metrics.input_tokens], ["Output tokens", metrics.output_tokens], ["Cached input", metrics.cache_read_tokens], ["Response time", `${run.result.elapsed_seconds} s`]]) {
    const tile = document.createElement("div");
    const title = document.createElement("small"); title.textContent = label;
    const detail = document.createElement("h2"); detail.textContent = value ?? "Unavailable";
    tile.append(title, detail); document.querySelector("#summary").append(tile);
  }
  function select(button) {
    document.querySelectorAll("[role=tab]").forEach(tab => tab.setAttribute("aria-selected", String(tab === button)));
    document.querySelector("#heading").textContent = button.textContent;
    document.querySelector("#explanation").textContent = explanations[button.dataset.key];
    document.querySelector("#payload").textContent = JSON.stringify(run.result[button.dataset.key], null, 2);
  }
  document.querySelectorAll("[role=tab]").forEach(button => button.addEventListener("click", () => select(button)));
  select(document.querySelector("[role=tab]"));
}
load().catch(error => { document.querySelector("#status").textContent = error.message; });

