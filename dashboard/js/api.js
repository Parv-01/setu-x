// Same-origin JSON API client. Every failure becomes an ApiError with a message a person can act on.

export class ApiError extends Error {
  constructor(status, code, message, requestId = null, details = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  describe() {
    const where = this.status ? `HTTP ${this.status} ${this.code}` : this.code;
    return `${where}: ${this.message}${this.requestId ? ` (request ${this.requestId})` : ""}`;
  }
}

function detailText(details) {
  if (!Array.isArray(details)) return "";
  return details.slice(0, 4).map((d) => {
    const loc = Array.isArray(d.loc) ? d.loc.filter((x) => x !== "body").join(".") : "";
    return `${loc ? `${loc}: ` : ""}${d.msg ?? JSON.stringify(d)}`;
  }).join("; ");
}

export async function api(method, path, body = undefined, { timeoutMs = 15000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
      cache: "no-store",
    });
  } catch (e) {
    const why = e.name === "AbortError" ? `no answer within ${timeoutMs / 1000} s` : e.message;
    throw new ApiError(0, "network_error", `controller not reachable (${why}). Is the stack running?`);
  } finally {
    clearTimeout(timer);
  }
  const raw = await res.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
  if (!res.ok) {
    const err = data && data.error ? data.error : null;
    const extra = err ? detailText(err.details) : "";
    const message = (err?.message || raw.slice(0, 200) || res.statusText || "request failed") + (extra ? ` — ${extra}` : "");
    throw new ApiError(res.status, err?.code || `http_${res.status}`, message, err?.request_id || null, err?.details);
  }
  if (data === null && raw) {
    throw new ApiError(res.status, "bad_response", `expected JSON from ${path}`);
  }
  return data;
}

/** Fetch a results file as text (CSV/JSONL) through the controller. */
export async function fetchText(path) {
  let res;
  try {
    res = await fetch(path, { cache: "no-store" });
  } catch (e) {
    throw new ApiError(0, "network_error", `controller not reachable (${e.message})`);
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error.message; } catch { /* keep statusText */ }
    throw new ApiError(res.status, `http_${res.status}`, msg);
  }
  return res.text();
}
