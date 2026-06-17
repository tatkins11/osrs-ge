export function SignalBadge({ signal }: { signal?: string }) {
  if (!signal) return null;
  return <span className={`badge badge-${signal}`}>{signal.replace("_", " ")}</span>;
}
