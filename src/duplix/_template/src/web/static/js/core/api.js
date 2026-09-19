/* The single path to the server.
 *
 * Every call funnels through `api()` so error handling is uniform: the
 * server answers a failure with {"error": "..."} and that text is what
 * the operator needs to see, not "HTTP 500".
 */

async function request(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.error) msg = j.error;
    } catch {
      /* non-JSON error body — the status line is all we have */
    }
    throw new Error(msg);
  }
  return res.json();
}

export const api = request;

/** GET a JSON endpoint. */
export const get = (path) => request(path);

/** POST a JSON body. */
export const post = (path, body) =>
  request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

/** PUT a JSON body. */
export const put = (path, body) =>
  request(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

/** DELETE. */
export const del = (path) => request(path, { method: "DELETE" });

/** GET that never throws — returns `fallback` and logs instead.
 *  Used by every read path: one failed panel shouldn't blank the page. */
export async function getOr(path, fallback, label) {
  try {
    return await request(path);
  } catch (e) {
    console.warn(`${label || path} load failed`, e);
    return fallback;
  }
}

/** Raw binary upload — the file body goes up as-is with the filename in
 *  a header, so there's no multipart parsing on the server. */
export async function upload(path, file) {
  const res = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/octet-stream",
      // encodeURIComponent so a non-ASCII filename survives the header
      // round-trip; the server decodes it.
      "X-Filename": encodeURIComponent(file.name),
    },
    body: await file.arrayBuffer(),
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      if (j && j.error) msg = j.error;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(msg);
  }
  return res.json();
}
