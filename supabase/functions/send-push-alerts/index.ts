// Supabase Edge Function: send-push-alerts
//
// Sends a daily 9am NZT web-push summary of overdue (and "critical": overdue past a
// closed week) purchase orders to every device in push_subscriptions.
//
// Deploy:   supabase functions deploy send-push-alerts
// Secrets:  supabase secrets set VAPID_PRIVATE_KEY=<base64url private key>
//           (VAPID_PUBLIC_KEY is optional — defaults to the key hard-coded in desktop.html / mobile.html)
//
// Schedule (SQL editor). Runs at 20:00 and 21:00 UTC; the function only sends on the run that
// lands on 9am in Pacific/Auckland, so it stays at 9am across NZST/NZDT changes:
//   select cron.schedule('send-push-alerts', '0 20,21 * * *',
//     $$select net.http_post(url:='https://jrpysikxibrbbellroid.supabase.co/functions/v1/send-push-alerts',
//       headers:='{"Authorization":"Bearer <SERVICE_ROLE_KEY>"}'::jsonb) as request_id$$);
//
// Manual test (sends regardless of time):  POST .../send-push-alerts?force=1  with the same header.
// Dry run (builds alerts, sends nothing):  POST .../send-push-alerts?force=1&dry=1

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = "https://jrpysikxibrbbellroid.supabase.co";
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const VAPID_PUBLIC_KEY = Deno.env.get("VAPID_PUBLIC_KEY") ??
  "BPBuQgiHsDB_kUmXmPyPTqx_tnICe2gisK7hwc0RVMPVqH2OrQa_W1xZrcFG_8dMNCtuUkg14FMsbCsx-yldjJc";
const VAPID_PRIVATE_KEY = (Deno.env.get("VAPID_PRIVATE_KEY") ?? "").replace(/=+$/, "");
const VAPID_SUBJECT = "mailto:mark@expol.co.nz";
const APP_URL = "https://markmischefski.github.io/raw-materials/mobile.html";
const NZ_TZ = "Pacific/Auckland";

const sb = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY);
const enc = new TextEncoder();

// ── Encoding helpers ─────────────────────────────────────────────────────────

function b64urlEncode(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str: string): Uint8Array {
  const b64 = str.replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(b64.padEnd(b64.length + (4 - (b64.length % 4)) % 4, "=")), (c) => c.charCodeAt(0));
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}

// ── NZ date helpers ──────────────────────────────────────────────────────────

function nzParts(d = new Date()) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: NZ_TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23",
    }).formatToParts(d).map((x) => [x.type, x.value]),
  );
  return { date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour) };
}

// Last fully completed week-ending Sunday (on a Sunday, the current week isn't closed yet).
function lastClosedSunday(today: string): string {
  const d = new Date(today + "T00:00:00Z");
  const dow = d.getUTCDay();
  d.setUTCDate(d.getUTCDate() - (dow === 0 ? 7 : dow));
  return d.toISOString().slice(0, 10);
}

// ── VAPID (RFC 8292) ─────────────────────────────────────────────────────────

let vapidKey: CryptoKey | null = null;
async function getVapidKey(): Promise<CryptoKey> {
  if (vapidKey) return vapidKey;
  // Public key is an uncompressed P-256 point: 0x04 || x(32) || y(32)
  const pub = b64urlDecode(VAPID_PUBLIC_KEY);
  vapidKey = await crypto.subtle.importKey(
    "jwk",
    { kty: "EC", crv: "P-256", d: VAPID_PRIVATE_KEY, x: b64urlEncode(pub.slice(1, 33)), y: b64urlEncode(pub.slice(33, 65)) },
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign"],
  );
  return vapidKey;
}

async function buildVapidJwt(endpoint: string): Promise<string> {
  const { protocol, host } = new URL(endpoint);
  const header = b64urlEncode(enc.encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const claims = b64urlEncode(enc.encode(JSON.stringify({
    aud: `${protocol}//${host}`, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: VAPID_SUBJECT,
  })));
  const unsigned = `${header}.${claims}`;
  // WebCrypto ECDSA returns raw r||s, which is exactly the JWS ES256 format.
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, await getVapidKey(), enc.encode(unsigned)));
  return `${unsigned}.${b64urlEncode(sig)}`;
}

// ── Payload encryption: aes128gcm (RFC 8291 / 8188) ─────────────────────────
// aes128gcm is required by Safari/iOS and supported by Chrome, Edge and Firefox.

async function hkdf(salt: Uint8Array, ikm: Uint8Array, info: Uint8Array, length: number): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, length * 8));
}

async function encryptPayload(p256dh: string, auth: string, payload: string): Promise<Uint8Array> {
  const uaPublic = b64urlDecode(p256dh);
  const authSecret = b64urlDecode(auth);

  const asKeys = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const asPublic = new Uint8Array(await crypto.subtle.exportKey("raw", asKeys.publicKey));
  const uaKey = await crypto.subtle.importKey("raw", uaPublic, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = new Uint8Array(await crypto.subtle.deriveBits({ name: "ECDH", public: uaKey }, asKeys.privateKey, 256));

  const ikm = await hkdf(authSecret, shared, concat(enc.encode("WebPush: info\0"), uaPublic, asPublic), 32);
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const cek = await hkdf(salt, ikm, enc.encode("Content-Encoding: aes128gcm\0"), 16);
  const nonce = await hkdf(salt, ikm, enc.encode("Content-Encoding: nonce\0"), 12);

  const aesKey = await crypto.subtle.importKey("raw", cek, "AES-GCM", false, ["encrypt"]);
  // Single record: plaintext followed by the 0x02 "last record" delimiter.
  const ciphertext = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv: nonce }, aesKey, concat(enc.encode(payload), new Uint8Array([2]))),
  );

  // Header: salt(16) | record size(4) | key id length(1) | key id (server public key)
  const header = new Uint8Array(21 + asPublic.length);
  header.set(salt, 0);
  new DataView(header.buffer).setUint32(16, 4096);
  header[20] = asPublic.length;
  header.set(asPublic, 21);
  return concat(header, ciphertext);
}

type Sub = { endpoint: string; p256dh: string; auth: string };

async function sendPush(sub: Sub, message: object): Promise<boolean> {
  try {
    const body = await encryptPayload(sub.p256dh, sub.auth, JSON.stringify(message));
    const jwt = await buildVapidJwt(sub.endpoint);
    const res = await fetch(sub.endpoint, {
      method: "POST",
      headers: {
        Authorization: `vapid t=${jwt}, k=${VAPID_PUBLIC_KEY}`,
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        TTL: "86400",
        Urgency: "normal",
      },
      body,
    });
    if (res.status === 404 || res.status === 410) {
      await sb.from("push_subscriptions").delete().eq("endpoint", sub.endpoint);
      console.log("Deleted expired subscription:", sub.endpoint.slice(0, 60));
    } else if (!res.ok) {
      console.error("Push failed", res.status, await res.text(), sub.endpoint.slice(0, 60));
    }
    return res.ok;
  } catch (e) {
    console.error("sendPush error:", e);
    return false;
  }
}

// ── Data loading ─────────────────────────────────────────────────────────────

// PostgREST caps responses at 1000 rows; page through larger tables.
async function fetchAll(table: string, select = "*"): Promise<any[]> {
  const PAGE = 1000;
  let all: any[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data, error } = await sb.from(table).select(select).range(from, from + PAGE - 1);
    if (error) throw new Error(`${table}: ${error.message}`);
    all = all.concat(data ?? []);
    if (!data || data.length < PAGE) return all;
  }
}

// Same rule as visibleBranches() in desktop.html: only branches with a branch_locations row
// (all branches if no mappings exist).
function visibleBranches(branches: any[], locs: any[]): any[] {
  const mapped = new Set(locs.map((l) => l.branch_id));
  return mapped.size ? branches.filter((b) => mapped.has(b.id)) : branches;
}

// ── Main ─────────────────────────────────────────────────────────────────────

Deno.serve(async (req) => {
  // The anon key is public (it's in the HTML), so require the service role key explicitly.
  if (req.headers.get("Authorization") !== `Bearer ${SUPABASE_SERVICE_KEY}`) {
    return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 });
  }
  const params = new URL(req.url).searchParams;
  const force = params.has("force");
  const dry = params.has("dry");

  try {
    const { date: today, hour } = nzParts();
    // Cron fires at 20:00 and 21:00 UTC; only one of those is 9am in NZ.
    if (!force && hour !== 9) {
      return new Response(JSON.stringify({ ok: true, skipped: `NZ hour is ${hour}, not 9` }));
    }
    if (!VAPID_PRIVATE_KEY && !dry) throw new Error("VAPID_PRIVATE_KEY secret is not set");
    const closedWeekEnd = lastClosedSunday(today);

    const [branches, locs, materials, matTypes, orders, orderLines, recon, subscriptions] = await Promise.all([
      fetchAll("branches"),
      fetchAll("branch_locations", "branch_id"),
      fetchAll("materials", "id,name"),
      fetchAll("material_types", "id,label"),
      fetchAll("orders"),
      fetchAll("order_lines"),
      fetchAll("stock_reconciliation", "branch_id,material_id,variance,notes,created_at"),
      fetchAll("push_subscriptions"),
    ]);

    if (!subscriptions.length && !dry) {
      return new Response(JSON.stringify({ ok: true, message: "No subscriptions" }));
    }

    const matName = (id?: string, typeId?: string) =>
      materials.find((m) => m.id === id)?.name ?? matTypes.find((t) => t.id === typeId)?.label ?? "material";

    // One entry per branch + material + PO + ETA; critical replaces plain overdue.
    const alerts = new Map<string, { critical: boolean; msg: string }>();
    const openOrders = orders
      .filter((o) => !o.received && !o.is_forecast)
      .sort((a, b) => sortOrder(a, branches) - sortOrder(b, branches) || String(a.eta).localeCompare(String(b.eta)));
    const shown = new Set(visibleBranches(branches, locs).map((b) => b.id));

    for (const ord of openOrders) {
      if (!shown.has(ord.branch_id)) continue;
      const branch = branches.find((b) => b.id === ord.branch_id);
      const allLines = orderLines.filter((l) => l.order_id === ord.id);
      // Orders with lines: check each outstanding line (line ETA defaults to the header ETA).
      // Orders without lines: legacy single-material orders, use the header.
      const items = allLines.length
        ? allLines.filter((l) => !l.received).map((l) => ({ eta: l.eta || ord.eta, matId: l.material_id, typeId: l.material_type_id }))
        : [{ eta: ord.eta, matId: ord.material_id, typeId: ord.material_type_id }];

      for (const it of items) {
        if (!it.eta || it.eta > today) continue;
        const name = matName(it.matId, it.typeId);
        const po = ord.po_ref ? `PO ${ord.po_ref}` : "No PO";
        const label = `${branch?.name ?? ord.branch_id} — ${name}: ${po}`;
        const key = `${label}|${it.eta}`;

        // Critical: the due week has fully closed and the order still hasn't arrived.
        if (it.eta > closedWeekEnd) {
          if (!alerts.has(key)) alerts.set(key, { critical: false, msg: `${label} overdue (was due ${it.eta})` });
        } else {
          const reconRow = recon
            .filter((r) => r.branch_id === ord.branch_id && r.material_id === it.matId && !r.notes?.startsWith("Resolved"))
            .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
          const v = Number(reconRow?.variance || 0);
          const varianceNote = v ? ` (ERP variance ${v > 0 ? "+" : ""}${v.toLocaleString("en-NZ")} kg)` : "";
          alerts.set(key, { critical: true, msg: `⚠ CRITICAL: ${label} overdue since ${it.eta} — due week has closed${varianceNote}` });
        }
      }
    }

    if (!alerts.size) {
      return new Response(JSON.stringify({ ok: true, message: "No alerts today" }));
    }

    const criticalList = [...alerts.values()].filter((a) => a.critical).map((a) => a.msg);
    const overdueList = [...alerts.values()].filter((a) => !a.critical).map((a) => a.msg);
    const allAlerts = [...criticalList, ...overdueList];
    const title = criticalList.length
      ? `⚠ ${criticalList.length} CRITICAL Alert${criticalList.length > 1 ? "s" : ""} — Expol RM`
      : `📦 ${overdueList.length} Overdue Order${overdueList.length > 1 ? "s" : ""} — Expol RM`;
    const body = allAlerts.slice(0, 4).join("\n") + (allAlerts.length > 4 ? `\n+${allAlerts.length - 4} more` : "");
    const notification = { title, body, url: APP_URL, critical: criticalList.length > 0 };

    let sent = 0;
    if (!dry) {
      const results = await Promise.all(subscriptions.map((s) => sendPush(s, notification)));
      sent = results.filter(Boolean).length;
    }

    return new Response(JSON.stringify({
      ok: true, dry, sent, subscriptions: subscriptions.length,
      critical: criticalList.length, overdue: overdueList.length, alerts: allAlerts,
    }));
  } catch (e) {
    console.error(e);
    return new Response(JSON.stringify({ error: String(e) }), { status: 500 });
  }
});

function sortOrder(o: any, branches: any[]): number {
  return branches.find((b) => b.id === o.branch_id)?.sort_order ?? 999;
}
