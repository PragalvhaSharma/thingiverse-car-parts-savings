// Shared review state for the savings review pages.
//
// GET  /api/state?key=<page>   -> { ok: true, decisions: {...} }
// POST { id, v }               -> merge one decision (v: null deletes = undo)
// POST { decisions: {...} }    -> bulk merge (the page's "Save now" button)
//
// Backed by one JSON blob per key in Vercel Blob, so every reviewer sees the
// same decisions. Until a Blob store is connected to the project (Storage tab ->
// Create Blob -> Connect Project, which injects BLOB_READ_WRITE_TOKEN), this
// responds { ok: false, reason: "no-blob" } and the page falls back to
// localStorage with a banner explaining the one-time setup.
//
// WHY THE `key` PARAM
//   Decisions are keyed by thing_id alone, so every page that omitted a key
//   shared ONE blob. That was fine for two pages over 500 rows, but the car
//   corpus is 9,190 rows across 78 per-brand pages with overlapping ids, and a
//   single blob would (a) collide across pages and (b) grow to ~2 MB, which the
//   read-modify-write below would then move on EVERY click. One blob per page
//   keeps each around the size that already works today (~0.2 MB for the
//   biggest brand). No key = the original blob, so index.html and prius.html
//   keep their existing decisions untouched.

import { put, list } from '@vercel/blob';

const DEFAULT_KEY = 'review/decisions.json';

// Keys land in a blob path, so allow only characters that cannot traverse or
// escape it. Anything else falls back to the default rather than 400ing, since
// a bad key should never cost a reviewer their click.
function blobKey(raw) {
  if (!raw) return DEFAULT_KEY;
  const clean = String(raw).toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 48);
  return clean ? `review/decisions-${clean}.json` : DEFAULT_KEY;
}

function validDecision(v) {
  if (v === 'keep' || v === 'veto') return true;
  return !!(v && typeof v === 'object' &&
    (v.v === 'keep' || v.v === 'veto' || v.v === 'accept'));
}

async function readMap(key) {
  const { blobs } = await list({ prefix: key, limit: 1 });
  if (!blobs.length) return {};
  // The blob is overwritten in place; a unique query string skips the CDN cache
  // so a read right after a write is never stale.
  const res = await fetch(`${blobs[0].url}?_=${Date.now()}`, { cache: 'no-store' });
  if (!res.ok) return {};
  try {
    const data = await res.json();
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

async function writeMap(key, map) {
  await put(key, JSON.stringify(map), {
    access: 'public',
    addRandomSuffix: false,
    allowOverwrite: true,
    contentType: 'application/json',
    // reads above bypass the CDN cache, so this only affects direct URL hits
    cacheControlMaxAge: 60,
  });
}

export default async function handler(req, res) {
  res.setHeader('cache-control', 'no-store');
  if (!process.env.BLOB_READ_WRITE_TOKEN) {
    res.status(200).json({ ok: false, reason: 'no-blob' });
    return;
  }
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const key = blobKey(url.searchParams.get('key'));

  try {
    if (req.method === 'GET') {
      res.status(200).json({ ok: true, decisions: await readMap(key) });
      return;
    }
    if (req.method === 'POST') {
      const body = req.body && typeof req.body === 'object' ? req.body : {};
      const map = await readMap(key);
      // Bulk merge is the rare recovery path and the caller adopts the merged
      // result, so it still gets the whole map back. A single decision is the
      // per-click hot path and the page only checks `ok` — returning the map
      // there would ship the entire blob back on every click.
      const bulk = !!(body.decisions && typeof body.decisions === 'object');

      if (bulk) {
        for (const [id, v] of Object.entries(body.decisions)) {
          if (v === null) delete map[String(id)];
          else if (validDecision(v)) map[String(id)] = v;
        }
      } else if (body.id !== undefined && body.id !== null && body.id !== '') {
        if (body.v === null || body.v === undefined) delete map[String(body.id)];
        else if (validDecision(body.v)) map[String(body.id)] = body.v;
        else {
          res.status(400).json({ ok: false, reason: 'bad-decision' });
          return;
        }
      } else {
        res.status(400).json({ ok: false, reason: 'bad-request' });
        return;
      }
      await writeMap(key, map);
      res.status(200).json(bulk ? { ok: true, decisions: map }
                                : { ok: true, n: Object.keys(map).length });
      return;
    }
    res.setHeader('allow', 'GET, POST');
    res.status(405).json({ ok: false, reason: 'method-not-allowed' });
  } catch (e) {
    res.status(200).json({ ok: false, reason: 'error', message: String((e && e.message) || e) });
  }
}
