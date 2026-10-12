// Image proxy for the review page (same idea as backend/main.py's /api/img).
//
// eBay and Thingiverse image hosts are unreliable when hotlinked from another
// origin (referer checks, intermittent 403s), so the page loads every photo via
// /api/img?url=… from an allowlisted host set, and the response is cached hard
// on Vercel's CDN — each image is fetched from the source roughly once.

const ALLOWED_HOSTS = new Set([
  'i.ebayimg.com',
  'thumbs.ebaystatic.com',
  'resize.thingiverse.com',
  'cdn.thingiverse.com',
]);

const MAX_BYTES = 4 * 1024 * 1024; // stay under the 4.5 MB function response cap

export default async function handler(req, res) {
  const raw = typeof req.query.url === 'string' ? req.query.url : '';
  let target;
  try {
    target = new URL(raw);
  } catch {
    res.status(400).send('bad url');
    return;
  }
  if (target.protocol !== 'https:' || !ALLOWED_HOSTS.has(target.hostname)) {
    res.status(403).send('host not allowed');
    return;
  }
  try {
    const up = await fetch(target.toString(), {
      redirect: 'follow',
      headers: {
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
        accept: 'image/avif,image/webp,image/*,*/*;q=0.8',
      },
    });
    if (!up.ok) {
      res.status(302).setHeader('location', target.toString());
      res.end();
      return;
    }
    const type = up.headers.get('content-type') || 'image/jpeg';
    if (!type.startsWith('image/')) {
      res.status(415).send('not an image');
      return;
    }
    const buf = Buffer.from(await up.arrayBuffer());
    if (buf.length > MAX_BYTES) {
      res.status(302).setHeader('location', target.toString());
      res.end();
      return;
    }
    res.setHeader('content-type', type);
    res.setHeader('cache-control', 'public, max-age=86400, s-maxage=2592000, stale-while-revalidate=86400');
    res.status(200).send(buf);
  } catch {
    // last resort: let the browser try the source directly
    res.status(302).setHeader('location', target.toString());
    res.end();
  }
}
