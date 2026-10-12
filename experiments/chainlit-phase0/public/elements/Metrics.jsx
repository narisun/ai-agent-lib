export default function Metrics() {
  const total = props.input_tokens + props.output_tokens;
  const ratio = props.input_tokens > 0 ? 100 * props.cache_read_tokens / props.input_tokens : null;
  return (
    <section aria-label="Response metrics" className="phase0-metrics">
      <strong>{total.toLocaleString()} tokens</strong>
      <span title="Cached read tokens divided by total input tokens">{ratio === null ? "Cache unavailable" : `${ratio.toFixed(0)}% prompt cache hit`}</span>
      <span>{props.elapsed_seconds.toFixed(2)} s</span>
      <small>Simulated usage · measured HTTP time</small>
      <a href={`/inspector/${encodeURIComponent(props.run_id)}`} target="_blank" rel="noopener noreferrer">Inspect run</a>
      <a href={`/artifacts/${encodeURIComponent(props.run_id)}?download=true`}>Download memo</a>
    </section>
  );
}

