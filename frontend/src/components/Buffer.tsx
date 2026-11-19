// Local fork: Buffer publishing on the Gaming page (backend: bufferpost.py, web/clipper_api.py).
// Pick rendered clips -> they get time slots (N a day per platform) -> this app feeds Buffer, Buffer
// posts them to Instagram Reels, TikTok and YouTube Shorts.
import { useCallback, useEffect, useMemo, useState } from "react";
import { req, urls } from "../api";
import { useI18n } from "../i18n";

export type BPlatform = "instagram" | "tiktok" | "youtube";
export const B_PLATFORMS: BPlatform[] = ["instagram", "tiktok", "youtube"];

export interface BSettings {
  enabled: boolean;
  organization_id: string;
  organization_name: string;
  channels: Record<BPlatform, string>;
  channel_names: Record<BPlatform, string>;
  platforms: Record<BPlatform, boolean>;
  per_day: Record<BPlatform, number>;
  per_day_wanted: Record<BPlatform, number>;
  daily_limits: Record<BPlatform, number>;
  mode: "spread" | "interval" | "times";
  window_start: string;
  window_end: string;
  start_time: string;
  gap_min: number;
  times: string[];
  queue_cap: number;
  hashtags: string;
  youtube: { privacy: "public" | "unlisted" | "private"; category: string; made_for_kids: boolean; notify: boolean };
  instagram: { share_to_feed: boolean; first_comment: string };
  ai_label: boolean;
  delete_after: boolean;
}
export interface BItem {
  id: string; project_id: string; cid: string; platform: BPlatform; title: string; caption: string;
  due_at: string; state: "scheduled" | "sent" | "error" | "cancelled"; post_id: string | null;
  error: string | null; link?: string | null; pinned?: boolean;
}
export interface BView {
  settings: BSettings;
  keys: { buffer: string | null; has_buffer: boolean; has_cloudinary: boolean; has_r2: boolean; has_host: boolean;
    host: "r2" | "cloudinary" | null;
    r2: { account_id: string | null; access_key_id: string | null; bucket: string | null; public_url: string | null; has_secret: boolean };
    cloudinary: { cloud_name: string | null; api_key: string | null; has_secret: boolean } };
  platforms: Record<BPlatform, { label: string; enabled: boolean; per_day: number; channel: string; channel_name: string;
    scheduled: number; sent: number; errors: number; room: number | null; in_buffer: number | null;
    next_free: string[]; planned_until: string | null; day: string[] }>;
  sending: { running: boolean; done: number; total: number; sent: number; failed: number; skipped: number;
    message: string | null; finished_at: number | null };
  room_checked_at: number | null;
  room_error: string | null;
  paused: { reason: string; hint?: string | null } | null;
  requests_24h: number;
  budget: number;
  items: BItem[];
  log: { at: number; kind: string; platform: string; message: string }[];
  youtube_categories: Record<string, string>;
}
interface BClip {
  project_id: string; project_title: string; style: string; cid: string; rank: number; duration: number;
  score: number | null; type: string | null; size_mb: number; title: string; caption: string;
  queued: Partial<Record<BPlatform, string>>; created_at: string;
}

const PLAT_SHORT: Record<BPlatform, string> = { instagram: "IG", tiktok: "TT", youtube: "YT" };
/** in Buffer or posted on at least one platform (a failed send doesn't count: it can be sent again) */
const isSent = (c: { queued: Partial<Record<BPlatform, string>> }) =>
  Object.values(c.queued).some((x) => x === "scheduled" || x === "sent");
const fmtWhen = (iso: string) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const thumb = (pid: string, cid: string) => `/api/buffer/thumb/${encodeURIComponent(pid)}/${encodeURIComponent(cid)}.jpg`;
const localInput = (iso: string | null | undefined) => {
  // ISO (UTC) -> the value of an <input type="datetime-local"> in this PC's time
  const d = iso ? new Date(iso) : new Date(Date.now() + 3600_000);
  const z = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`;
};
const span = (a: string | null, b: string | null) => (a && b ? (a === b ? fmtWhen(a) : `${fmtWhen(a)} → ${fmtWhen(b)}`) : "-");
const errText = (e: unknown) => String((e as Error)?.message || e);

export function useBuffer() {
  const [v, setV] = useState<BView | null>(null);
  const load = useCallback(() => req<BView>("GET", "/api/buffer").then(setV).catch(() => undefined), []);
  useEffect(() => {
    load();
  }, [load]);
  return { v, setV, load };
}

// ===================================================================== setup (keys + test + options)

export function BufferSetupCard({ onReady }: { onReady?: () => void }) {
  const { t } = useI18n();
  const { v, setV } = useBuffer();
  const [key, setKey] = useState("");
  const [cloud, setCloud] = useState({ cloud_name: "", api_key: "", api_secret: "" });
  const R2_EMPTY = { account_id: "", access_key_id: "", secret_access_key: "", bucket: "sharkyclips", public_url: "" };
  const [r2, setR2] = useState(R2_EMPTY);
  const r2Typed = !!(r2.account_id || r2.access_key_id || r2.secret_access_key || r2.public_url);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [test, setTest] = useState<any>(null);
  if (!v) return null;
  const s = v.settings;
  const save = async (body: Record<string, unknown>) => {
    try {
      setV(await req<BView>("PUT", "/api/buffer/settings", body));
    } catch (e) {
      setMsg({ ok: false, text: errText(e) });
    }
  };
  const saveKeys = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const body: Record<string, unknown> = {};
      if (key.trim()) body.buffer = key.trim();
      if (cloud.cloud_name || cloud.api_key || cloud.api_secret) body.cloudinary = cloud;
      if (r2Typed) body.r2 = r2;
      setV(await req<BView>("PUT", "/api/buffer/keys", body));
      setKey("");
      setCloud({ cloud_name: "", api_key: "", api_secret: "" });
      setR2(R2_EMPTY);
      await runTest();
    } catch (e) {
      setMsg({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  };
  const runTest = async () => {
    setBusy(true);
    try {
      const r = await req<any>("POST", "/api/buffer/test");
      setTest(r);
      setV(r.view);
      setMsg({ ok: r.ok, text: r.ok ? t("buf.test_ok") : t("buf.test_bad") });
      if (r.ok) onReady?.();
    } catch (e) {
      setMsg({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  };
  const channels: any[] = test?.buffer?.channels ?? [];
  return (
    <section className="card gx-card">
      <div className="gx-card-head">
        <h3>{t("buf.setup_title")}</h3>
        <StatusChip ok={v.keys.has_buffer && v.keys.has_host && !!s.organization_id} t={t} />
      </div>
      <p className="muted small">{t("buf.setup_hint")}</p>
      <div className="gx-grid2">
        <div className="gx-sub">
          <strong>1 · {t("buf.key_title")}</strong>
          <p className="muted small">{t("buf.key_hint")}</p>
          {v.keys.buffer && <div className="small mono">{t("gaming.ai_saved_key")}: {v.keys.buffer}</div>}
          <label className="field">
            <span className="field-label">{t("buf.key")}</span>
            <input className="input input-sm" type="password" autoComplete="off" value={key}
              placeholder="Buffer API key" onChange={(e) => setKey(e.target.value)} />
          </label>
        </div>
        <div className="gx-sub">
          <strong>2 · {t("buf.r2_title")}</strong>
          <p className="muted small">{t("buf.r2_hint")}</p>
          {v.keys.r2.public_url && (
            <div className="small mono">{v.keys.r2.bucket} · {v.keys.r2.public_url}{v.keys.host === "r2" ? " ✓" : ""}</div>
          )}
          <div className="gx-row">
            <label className="field">
              <span className="field-label">Account ID</span>
              <input className="input input-sm" autoComplete="off" value={r2.account_id}
                onChange={(e) => setR2({ ...r2, account_id: e.target.value })} />
            </label>
            <label className="field">
              <span className="field-label">Access Key ID</span>
              <input className="input input-sm" autoComplete="off" value={r2.access_key_id}
                onChange={(e) => setR2({ ...r2, access_key_id: e.target.value })} />
            </label>
          </div>
          <div className="gx-row">
            <label className="field">
              <span className="field-label">Secret Access Key</span>
              <input className="input input-sm" type="password" autoComplete="off" value={r2.secret_access_key}
                onChange={(e) => setR2({ ...r2, secret_access_key: e.target.value })} />
            </label>
            <label className="field">
              <span className="field-label">{t("buf.r2_bucket")}</span>
              <input className="input input-sm" autoComplete="off" value={r2.bucket}
                onChange={(e) => setR2({ ...r2, bucket: e.target.value })} />
            </label>
          </div>
          <label className="field">
            <span className="field-label">{t("buf.r2_public")}</span>
            <input className="input input-sm" autoComplete="off" placeholder="https://pub-xxxxxxxx.r2.dev" value={r2.public_url}
              onChange={(e) => setR2({ ...r2, public_url: e.target.value })} />
          </label>
          <details className="gx-details">
            <summary className="small">{t("buf.cloud_title")} ({t("buf.alternative")})</summary>
            <p className="muted small">{t("buf.cloud_hint")}</p>
            {v.keys.cloudinary.cloud_name && (
              <div className="small mono">{v.keys.cloudinary.cloud_name} · {v.keys.cloudinary.api_key}{v.keys.host === "cloudinary" ? " ✓" : ""}</div>
            )}
            <div className="gx-row">
              <label className="field">
                <span className="field-label">{t("buf.cloud_name")}</span>
                <input className="input input-sm" autoComplete="off" value={cloud.cloud_name}
                  onChange={(e) => setCloud({ ...cloud, cloud_name: e.target.value })} />
              </label>
              <label className="field">
                <span className="field-label">{t("buf.cloud_key")}</span>
                <input className="input input-sm" autoComplete="off" value={cloud.api_key}
                  onChange={(e) => setCloud({ ...cloud, api_key: e.target.value })} />
              </label>
              <label className="field">
                <span className="field-label">{t("buf.cloud_secret")}</span>
                <input className="input input-sm" type="password" autoComplete="off" value={cloud.api_secret}
                  onChange={(e) => setCloud({ ...cloud, api_secret: e.target.value })} />
              </label>
            </div>
          </details>
        </div>
      </div>
      <div className="gx-actions">
        <button className="btn btn-primary" disabled={busy || (!key.trim() && !cloud.cloud_name && !cloud.api_key && !cloud.api_secret && !r2Typed)}
          onClick={saveKeys}>{t("buf.save_test")}</button>
        <button className="btn" disabled={busy || !v.keys.has_buffer} onClick={runTest}>{t("buf.test")}</button>
        {(v.keys.has_buffer || v.keys.has_cloudinary || v.keys.has_r2) && (
          <button className="btn btn-ghost btn-sm" onClick={async () => setV(await req<BView>("PUT", "/api/buffer/keys", { buffer: "", cloudinary: {}, r2: {} }))}>
            {t("buf.forget")}
          </button>
        )}
        {busy && <span className="spinner" aria-hidden />}
      </div>
      {msg && <div className={`notice small ${msg.ok ? "notice-success" : "notice-danger"}`} role="status">{msg.text}</div>}
      {test && (
        <ul className="gx-checks small">
          <li className={test.buffer?.ok ? "ok" : "bad"}>
            Buffer: {test.buffer?.ok ? `${test.buffer.organization} · ${channels.length} ${t("buf.channels")}` : `${test.buffer?.error ?? ""} ${test.buffer?.hint ?? ""}`}
          </li>
          <li className={test.hosting?.ok ? "ok" : "bad"}>
            {test.hosting?.host === "r2" ? "Cloudflare R2" : "Cloudinary"}: {test.hosting?.ok
              ? `${test.hosting.plan ?? "OK"}${test.hosting.credits_limit ? ` · ${test.hosting.credits_used}/${test.hosting.credits_limit} credits` : ""}`
              : `${test.hosting?.error ?? ""} ${test.hosting?.hint ?? ""}`}
          </li>
        </ul>
      )}

      <h4 style={{ marginTop: 16 }}>3 · {t("buf.channels_title")}</h4>
      <div className="gx-plats">
        {B_PLATFORMS.map((p) => {
          const mine = channels.filter((c) => c.service === p);
          return (
            <div key={p} className={`gx-plat ${s.platforms[p] ? "on" : ""}`}>
              <label className="check" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <input type="checkbox" checked={s.platforms[p]} onChange={(e) => save({ platforms: { [p]: e.target.checked } })} />
                <strong>{v.platforms[p].label}</strong>
              </label>
              <div className="small muted">
                {s.channels[p] ? `${t("buf.linked")}: ${s.channel_names[p] || s.channels[p]}` : t("buf.not_linked")}
              </div>
              {mine.length > 1 && (
                <select className="select select-sm" value={s.channels[p]} aria-label={v.platforms[p].label}
                  onChange={(e) => save({ channels: { [p]: e.target.value } })}>
                  {mine.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              )}
            </div>
          );
        })}
      </div>

      <p className="small" style={{ marginTop: 12 }}>{t("buf.plan_where")}</p>

      <details className="gx-details">
        <summary>{t("buf.post_options")}</summary>
        <div className="gx-row">
          <label className="field" style={{ flex: "2 1 260px" }}>
            <span className="field-label">{t("buf.hashtags")}</span>
            <HashtagField value={s.hashtags} onSave={(hashtags) => save({ hashtags })} />
          </label>
          <label className="field">
            <span className="field-label">{t("buf.yt_privacy")}</span>
            <select className="select select-sm" value={s.youtube.privacy} onChange={(e) => save({ youtube: { privacy: e.target.value } })}>
              <option value="public">{t("buf.public")}</option>
              <option value="unlisted">{t("buf.unlisted")}</option>
              <option value="private">{t("buf.private")}</option>
            </select>
          </label>
          <label className="field">
            <span className="field-label">{t("buf.yt_category")}</span>
            <select className="select select-sm" value={s.youtube.category} onChange={(e) => save({ youtube: { category: e.target.value } })}>
              {Object.entries(v.youtube_categories).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select>
          </label>
        </div>
        <div className="gx-checkrow">
          <Check label={t("buf.yt_notify")} value={s.youtube.notify} onChange={(x) => save({ youtube: { notify: x } })} />
          <Check label={t("buf.yt_kids")} value={s.youtube.made_for_kids} onChange={(x) => save({ youtube: { made_for_kids: x } })} />
          <Check label={t("buf.ig_feed")} value={s.instagram.share_to_feed} onChange={(x) => save({ instagram: { share_to_feed: x } })} />
          <Check label={t("buf.ai_label")} value={s.ai_label} onChange={(x) => save({ ai_label: x })} />
          <Check label={t("buf.delete_after")} value={s.delete_after} onChange={(x) => save({ delete_after: x })} />
        </div>
        <label className="field">
          <span className="field-label">{t("buf.ig_comment")}</span>
          <HashtagField value={s.instagram.first_comment} onSave={(c) => save({ instagram: { first_comment: c } })} />
        </label>
      </details>
    </section>
  );
}

function Check({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="check" style={{ display: "flex", gap: 8, alignItems: "center" }}>
      <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

function HashtagField({ value, onSave }: { value: string; onSave: (v: string) => void }) {
  const [txt, setTxt] = useState(value);
  useEffect(() => setTxt(value), [value]);
  return <input className="input input-sm" value={txt} onChange={(e) => setTxt(e.target.value)} onBlur={() => txt !== value && onSave(txt)} />;
}

export function StatusChip({ ok, t }: { ok: boolean; t: ReturnType<typeof useI18n>["t"] }) {
  return <span className={`chip ${ok ? "chip-success" : "chip-outline"}`}><span className="dot" />{ok ? t("buf.ready") : t("buf.not_ready")}</span>;
}

// ===================================================================== pick clips -> Buffer

export function BufferPicker({ goSetup }: { goSetup: () => void }) {
  const { t } = useI18n();
  const { v, setV } = useBuffer();
  const [clips, setClips] = useState<BClip[] | null>(null);
  const [sel, setSel] = useState<Record<string, boolean>>({});
  const [edit, setEdit] = useState<Record<string, { title: string; caption: string }>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [plats, setPlats] = useState<Record<BPlatform, boolean>>({ instagram: true, tiktok: true, youtube: true });
  const [project, setProject] = useState<string>("all");
  const [onlyNew, setOnlyNew] = useState(true);
  const [startMode, setStartMode] = useState<"asap" | "at">("asap");
  const [startAt, setStartAt] = useState(() => localInput(null));
  const [pv, setPv] = useState<Record<string, { first: string | null; last: string | null; days: number }> | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(() => req<{ clips: BClip[] }>("GET", "/api/buffer/clips").then((r) => setClips(r.clips)).catch(() => setClips([])), []);
  useEffect(() => {
    load();
    req<BView>("GET", "/api/buffer/room").then(setV).catch(() => undefined); // how much room Buffer has now
  }, [load, setV]);
  const running = !!v?.sending.running;
  useEffect(() => {
    if (!running) return;
    const iv = window.setInterval(() => req<BView>("GET", "/api/buffer").then((x) => {
      setV(x);
      if (!x.sending.running) {
        load();
        req<BView>("GET", "/api/buffer/room").then(setV).catch(() => undefined);
      }
    }).catch(() => undefined), 1500);
    return () => window.clearInterval(iv);
  }, [running, load, setV]);
  const projects = useMemo(() => {
    const m = new Map<string, string>();
    (clips ?? []).forEach((c) => m.set(c.project_id, c.project_title));
    return [...m.entries()];
  }, [clips]);
  const shown = (clips ?? []).filter((c) => (project === "all" || c.project_id === project)
    && (!onlyNew || !isSent(c)));
  const k = (c: BClip) => `${c.project_id}/${c.cid}`;
  const picked = shown.filter((c) => sel[k(c)]);
  const ready = !!v && v.keys.has_buffer && v.keys.has_host && !!v.settings.organization_id;
  const activePlats = B_PLATFORMS.filter((p) => plats[p] && v?.settings.platforms[p]);
  const start = startMode === "at" ? startAt : "";
  const platKey = activePlats.join(",");
  const roomMin = v && activePlats.length ? Math.min(...activePlats.map((p) => v.platforms[p].room ?? v.settings.queue_cap)) : 0;
  useEffect(() => {
    if (picked.length === 0 || !platKey) {
      setPv(null);
      return;
    }
    const n = roomMin > 0 ? Math.min(picked.length, roomMin) : picked.length;  // only what goes now
    const q = new URLSearchParams({ count: String(n), platforms: platKey, start });
    req<typeof pv>("GET", `/api/buffer/preview?${q}`).then(setPv).catch(() => setPv(null));
  }, [picked.length, platKey, start, roomMin]);
  const send = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const picks = picked.map((c) => ({ project_id: c.project_id, cid: c.cid, platforms: activePlats,
        title: edit[k(c)]?.title ?? c.title, caption: edit[k(c)]?.caption ?? c.caption }));
      const r = await req<{ count: number; skipped: number; view: BView }>("POST", "/api/buffer/send", { picks, start: start || null });
      setV(r.view);
      setSel({});
      setMsg(r.skipped ? { ok: true, text: t("buf.skipped", { n: r.skipped }) } : null);
    } catch (e) {
      setMsg({ ok: false, text: errText(e) });
    } finally {
      setBusy(false);
    }
  };
  if (clips === null) return <div className="card gx-card"><span className="spinner" /></div>;
  return (
    <section className="gx-picker">
      {!ready && (
        <div className="notice notice-warn small" role="status">
          {t("buf.need_setup")} <button className="btn btn-sm" onClick={goSetup}>{t("buf.open_setup")}</button>
        </div>
      )}
      {ready && v && (
        <div className="gx-room small" role="status">
          <b>{t("buf.room_now")}</b>
          {B_PLATFORMS.filter((p) => v.settings.platforms[p]).map((p) => (
            <span key={p} className={`chip ${(v.platforms[p].room ?? 0) > 0 ? "chip-success" : "chip-outline"}`}>
              {v.platforms[p].label}: {v.platforms[p].room ?? "?"}
            </span>
          ))}
          <span className="muted">{t("buf.room_hint", { size: v.settings.queue_cap })}</span>
          {v.room_error && <span className="danger">{v.room_error}</span>}
        </div>
      )}
      {v && (v.sending.running || v.sending.finished_at) && v.sending.total > 0 && (
        <div className={`notice small ${v.sending.running ? "" : v.sending.failed ? "notice-danger" : "notice-success"}`} role="status">
          {v.sending.running
            ? t("buf.sending", { done: v.sending.done, total: v.sending.total })
            : t("buf.sent_done", { sent: v.sending.sent, failed: v.sending.failed })}
          {v.sending.message ? ` · ${v.sending.message}` : ""}
          {v.sending.running && (
            <div className="progress" style={{ marginTop: 6 }}><span style={{ width: `${Math.round((v.sending.done / Math.max(1, v.sending.total)) * 100)}%` }} /></div>
          )}
        </div>
      )}
      <div className="gx-toolbar">
        <select className="select select-sm" value={project} aria-label={t("buf.filter_video")} onChange={(e) => setProject(e.target.value)}>
          <option value="all">{t("buf.all_videos")} ({clips.length})</option>
          {projects.map(([id, title]) => <option key={id} value={id}>{title}</option>)}
        </select>
        <Check label={t("buf.only_new")} value={onlyNew} onChange={setOnlyNew} />
        <span className="topbar-spacer" />
        <button className="btn btn-sm" onClick={() => setSel(Object.fromEntries(shown.map((c) => [k(c), true])))}>{t("buf.select_all")}</button>
        <button className="btn btn-sm btn-ghost" onClick={() => setSel({})}>{t("buf.select_none")}</button>
      </div>
      {shown.length === 0 ? (
        <div className="card gx-card gx-empty">
          <strong>{t("buf.no_clips")}</strong>
          <p className="muted small">{t("buf.no_clips_hint")}</p>
        </div>
      ) : (
        <div className="gx-clips">
          {shown.map((c) => {
            const key = k(c);
            const on = !!sel[key];
            const ed = edit[key] ?? { title: c.title, caption: c.caption };
            return (
              <article key={key} className={`gx-clip ${on ? "on" : ""} ${isSent(c) ? "sent" : ""}`}>
                <button className="gx-thumb" aria-pressed={on} aria-label={`${t("buf.pick")} ${c.title}`}
                  onClick={() => setSel({ ...sel, [key]: !on })}>
                  {open === key ? (
                    <video src={urls.clip(c.project_id, c.cid, c.created_at)} controls autoPlay playsInline
                      onClick={(e) => e.stopPropagation()} />
                  ) : (
                    <img src={thumb(c.project_id, c.cid)} alt="" loading="lazy" />
                  )}
                  <span className={`gx-tick ${on ? "on" : ""}`} aria-hidden>{on ? "✓" : ""}</span>
                  <span className="gx-dur mono">{Math.round(c.duration)} s</span>
                </button>
                <div className="gx-clip-body">
                  <div className="gx-headline">{ed.title}</div>
                  <div className="small muted">
                    {c.project_title} · #{c.rank}{c.score != null ? ` · ${t("buf.score")} ${c.score}` : ""} · {c.style === "direct" ? t("gx.style_direct_short") : t("gx.style_streamer_short")}
                  </div>
                  <div className="gx-qchips">
                    {B_PLATFORMS.map((p) => c.queued[p] ? (
                      <span key={p} className={`chip ${c.queued[p] === "sent" ? "chip-success" : "chip-outline"}`} title={c.queued[p]}>
                        {PLAT_SHORT[p]} · {t(`buf.state_${c.queued[p]}`)}
                      </span>
                    ) : null)}
                  </div>
                  <div className="gx-clip-actions">
                    <button className="btn btn-sm btn-ghost" onClick={() => setOpen(open === key ? null : key)}>
                      {open === key ? t("buf.hide") : t("buf.watch")}
                    </button>
                    <a className="btn btn-sm btn-ghost" href={urls.clipDownload(c.project_id, c.cid)}>{t("buf.download")}</a>
                  </div>
                  {on && (
                    <div className="gx-edit">
                      <label className="field">
                        <span className="field-label">{t("buf.yt_title")}</span>
                        <input className="input input-sm" maxLength={100} value={ed.title}
                          onChange={(e) => setEdit({ ...edit, [key]: { ...ed, title: e.target.value } })} />
                      </label>
                      <label className="field">
                        <span className="field-label">{t("buf.caption")}</span>
                        <textarea className="input input-sm" rows={3} value={ed.caption}
                          onChange={(e) => setEdit({ ...edit, [key]: { ...ed, caption: e.target.value } })} />
                      </label>
                    </div>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {msg && <div className={`notice small ${msg.ok ? "notice-success" : "notice-danger"}`} role="status">{msg.text}</div>}
      <div className={`gx-sendbar ${picked.length === 0 ? "idle" : ""}`} role="region" aria-label={t("buf.send")}>
        <strong>{t("buf.n_selected", { n: picked.length })}</strong>
        <div className="gx-platpick">
          {B_PLATFORMS.map((p) => (
            <label key={p} className={`chip-toggle ${plats[p] && v?.settings.platforms[p] ? "on" : ""}`}>
              <input type="checkbox" checked={plats[p]} disabled={!v?.settings.platforms[p]}
                onChange={(e) => setPlats({ ...plats, [p]: e.target.checked })} />
              {v?.platforms[p].label ?? p}
            </label>
          ))}
        </div>
        <div className="gx-start">
          <select className="select select-sm" value={startMode} aria-label={t("buf.start")}
            onChange={(e) => setStartMode(e.target.value === "at" ? "at" : "asap")}>
            <option value="asap">{t("buf.start_asap")}</option>
            <option value="at">{t("buf.start_at")}</option>
          </select>
          {startMode === "at" && (
            <input className="input input-sm" type="datetime-local" value={startAt} aria-label={t("buf.start_at")}
              onChange={(e) => setStartAt(e.target.value)} />
          )}
        </div>
        <span className="topbar-spacer" />
        <button className="btn btn-primary" disabled={busy || running || !ready || picked.length === 0 || activePlats.length === 0 || roomMin === 0} onClick={send}>
          {busy || running ? <span className="spinner" /> : null} {picked.length > roomMin && roomMin > 0 ? t("buf.send_n", { n: roomMin }) : t("buf.send")}
        </button>
        {pv && picked.length > 0 && v && (
          <ul className="gx-pv small">
            {activePlats.filter((p) => pv[p]).map((p) => (
              <li key={p}>
                <b>{v.platforms[p].label}</b>: <span className="mono">{span(pv[p].first, pv[p].last)}</span>
                {pv[p].days > 1 ? ` · ${t("buf.pv_days", { days: pv[p].days })}` : ""}
              </li>
            ))}
            {picked.length > roomMin && <li className="danger">{t("buf.over_room", { room: roomMin, n: picked.length - roomMin })}</li>}
            <li className="muted">{t("buf.pv_note")}</li>
          </ul>
        )}
      </div>
    </section>
  );
}

// ===================================================================== the schedule (outbox)

/** The times of one day for the plan being edited (same rules as bufferpost._day_times). */
function exampleDay(mode: string, n: number, a: string, b: string, start: string, gap: number, times: string): string[] {
  const mins = (hhmm: string) => {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
    return m ? Number(m[1]) * 60 + Number(m[2]) : null;
  };
  const fmt = (x: number) => `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(Math.floor(x % 60)).padStart(2, "0")}`;
  const k = Math.max(1, Math.min(50, n || 1));
  if (mode === "times") {
    return [...new Set(times.split(/[ ,;]+/).map(mins).filter((x): x is number => x != null && x < 1440))].sort((x, y) => x - y).slice(0, k).map(fmt);
  }
  if (mode === "interval") {
    const s0 = mins(start) ?? 600;
    return Array.from({ length: k }, (_, i) => s0 + gap * i).filter((x) => x < 1440).map(fmt);
  }
  const a0 = mins(a) ?? 600;
  let b0 = mins(b) ?? 1380;
  if (b0 <= a0) b0 = 1439;
  if (k === 1) return [fmt(a0 + (b0 - a0) / 2)];
  return Array.from({ length: k }, (_, i) => a0 + ((b0 - a0) * i) / (k - 1)).map(fmt);
}

export function PostingPlan({ v, setV }: { v: BView; setV: (v: BView) => void }) {
  const { t } = useI18n();
  const s = v.settings;
  const [d, setD] = useState(() => ({ mode: s.mode, window_start: s.window_start, window_end: s.window_end,
    start_time: s.start_time, gap_min: s.gap_min, times: s.times.join(", "), per_day: { ...s.per_day_wanted },
    queue_cap: s.queue_cap }));
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const dirty = JSON.stringify(d) !== JSON.stringify({ mode: s.mode, window_start: s.window_start, window_end: s.window_end,
    start_time: s.start_time, gap_min: s.gap_min, times: s.times.join(", "), per_day: s.per_day_wanted, queue_cap: s.queue_cap });
  const save = async () => {
    setErr(null);
    try {
      const nv = await req<BView>("PUT", "/api/buffer/settings", { ...d, times: d.times.split(/[ ,;]+/).filter(Boolean) });
      setV(nv);
      const x = nv.settings;
      setD({ mode: x.mode, window_start: x.window_start, window_end: x.window_end, start_time: x.start_time, gap_min: x.gap_min,
        times: x.times.join(", "), per_day: { ...x.per_day_wanted }, queue_cap: x.queue_cap });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setErr(errText(e));
    }
  };
  const day = exampleDay(d.mode, d.per_day.tiktok || d.per_day.instagram, d.window_start, d.window_end, d.start_time, d.gap_min, d.times);
  return (
    <section className="card gx-card gx-plan">
      <div className="gx-card-head">
        <h3>{t("buf.plan_title")}</h3>
        {dirty ? <span className="chip chip-outline">{t("buf.plan_unsaved")}</span> : null}
      </div>
      <p className="muted small">{t("buf.plan_hint")}</p>
      <div className="gx-plan-grid">
        <div>
          <h4>1 · {t("buf.plan_count")}</h4>
          <div className="gx-row">
            {B_PLATFORMS.map((p) => (
              <label key={p} className="field">
                <span className="field-label">{v.platforms[p].label}{s.platforms[p] ? "" : ` (${t("buf.off")})`}</span>
                <input className="input input-sm" type="number" min={1} max={50} value={d.per_day[p]}
                  onChange={(e) => setD({ ...d, per_day: { ...d.per_day, [p]: Number(e.target.value) } })} />
                {s.daily_limits[p] > 0 && d.per_day[p] > s.daily_limits[p] && (
                  <span className="small danger">{t("buf.daily_cap", { n: s.daily_limits[p] })}</span>
                )}
              </label>
            ))}
          </div>
        </div>
        <div>
          <h4>2 · {t("buf.plan_when")}</h4>
          <div className="segmented" role="radiogroup" aria-label={t("buf.plan_when")}>
            {(["spread", "interval", "times"] as const).map((m) => (
              <button key={m} role="radio" aria-checked={d.mode === m} onClick={() => setD({ ...d, mode: m })}>{t(`buf.mode_${m}`)}</button>
            ))}
          </div>
          <div className="gx-row" style={{ marginTop: 8 }}>
            {d.mode === "spread" && (
              <>
                <label className="field"><span className="field-label">{t("buf.window_start")}</span>
                  <input className="input input-sm" type="time" value={d.window_start} onChange={(e) => setD({ ...d, window_start: e.target.value })} /></label>
                <label className="field"><span className="field-label">{t("buf.window_end")}</span>
                  <input className="input input-sm" type="time" value={d.window_end} onChange={(e) => setD({ ...d, window_end: e.target.value })} /></label>
              </>
            )}
            {d.mode === "interval" && (
              <>
                <label className="field"><span className="field-label">{t("buf.window_start")}</span>
                  <input className="input input-sm" type="time" value={d.start_time} onChange={(e) => setD({ ...d, start_time: e.target.value })} /></label>
                <label className="field"><span className="field-label">{t("buf.gap")}</span>
                  <select className="select select-sm" value={d.gap_min} onChange={(e) => setD({ ...d, gap_min: Number(e.target.value) })}>
                    {[15, 30, 45, 60, 90, 120, 180, 240].map((m) => <option key={m} value={m}>{m < 60 ? `${m} min` : `${m / 60} h`}</option>)}
                  </select></label>
              </>
            )}
            {d.mode === "times" && (
              <label className="field" style={{ flex: "3 1 260px" }}><span className="field-label">{t("buf.fixed_times")}</span>
                <input className="input input-sm" value={d.times} placeholder="12:00, 15:00, 18:00, 21:00" onChange={(e) => setD({ ...d, times: e.target.value })} /></label>
            )}
          </div>
          <p className="small gx-example">{t("buf.example_day")}: <span className="mono">{day.join(" · ") || "-"}</span></p>
        </div>
        <div>
          <h4>3 · {t("buf.plan_limit")}</h4>
          <div className="gx-row">
            <label className="field" style={{ maxWidth: 220 }}><span className="field-label">{t("buf.queue_cap")}</span>
              <input className="input input-sm" type="number" min={1} max={200} value={d.queue_cap} onChange={(e) => setD({ ...d, queue_cap: Number(e.target.value) })} /></label>
          </div>
          <p className="muted small" style={{ marginTop: 6 }}>{t("buf.plan_limit_hint")}</p>
        </div>
      </div>
      <div className="gx-actions">
        <button className="btn btn-primary" disabled={!dirty} onClick={save}>{saved ? t("gaming.saved") : t("buf.plan_save")}</button>
        <span className="muted small">{t("buf.plan_save_hint")}</span>
      </div>
      {err && <div className="notice notice-danger small" role="status">{err}</div>}
    </section>
  );
}

function TimeEdit({ item, onDone }: { item: BItem; onDone: (v: BView) => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [val, setVal] = useState(localInput(item.due_at));
  const [err, setErr] = useState<string | null>(null);
  if (!open) return <button className="btn btn-sm btn-ghost" onClick={() => setOpen(true)}>{t("buf.change_time")}</button>;
  return (
    <span className="gx-timeedit">
      <input className="input input-sm" type="datetime-local" value={val} aria-label={t("buf.change_time")} onChange={(e) => setVal(e.target.value)} />
      <button className="btn btn-sm btn-primary" onClick={async () => {
        setErr(null);
        try {
          onDone(await req<BView>("PUT", `/api/buffer/items/${item.id}/time`, { due_at: val }));
          setOpen(false);
        } catch (e) {
          setErr(errText(e));
        }
      }}>{t("gaming.save")}</button>
      <button className="btn btn-sm btn-ghost" onClick={() => setOpen(false)}>{t("buf.cancel")}</button>
      {err && <span className="small danger">{err}</span>}
    </span>
  );
}

export function BufferSchedule() {
  const { t } = useI18n();
  const { v, setV, load } = useBuffer();
  const [filter, setFilter] = useState<"upcoming" | "sent" | "problems">("upcoming");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    // opening the tab = "what happened while the app was closed": posted / failed, and the room in Buffer
    req<{ view: BView }>("POST", "/api/buffer/run").then((r) => setV(r.view)).catch(() => undefined);
    const iv = window.setInterval(load, 30000);
    return () => window.clearInterval(iv);
  }, [load, setV]);
  if (!v) return null;
  const items = v.items.filter((i) => filter === "upcoming" ? i.state === "scheduled"
    : filter === "sent" ? i.state === "sent" : ["error", "cancelled"].includes(i.state));
  const byDay = new Map<string, BItem[]>();
  for (const i of items) {
    const d = new Date(i.due_at).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    byDay.set(d, [...(byDay.get(d) ?? []), i]);
  }
  const act = async (path: string) => {
    setBusy(true);
    try {
      const r = await req<any>("POST", path);
      setV(r.view ?? r);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section>
      {v.paused && (
        <div className="notice notice-danger small" role="status">
          {t("buf.paused")}: {v.paused.reason} {v.paused.hint ?? ""}
          <button className="btn btn-sm" style={{ marginInlineStart: 8 }} onClick={() => act("/api/buffer/resume")}>{t("buf.resume")}</button>
        </div>
      )}
      <PostingPlan key={JSON.stringify(v.settings)} v={v} setV={setV} />
      <div className="gx-stats">
        {B_PLATFORMS.map((p) => {
          const x = v.platforms[p];
          return (
            <div key={p} className={`card gx-stat ${x.enabled ? "" : "off"}`}>
              <strong>{x.label}</strong>
              <div className="gx-stat-nums">
                <span><b>{x.scheduled}</b> {t("buf.in_buffer")}</span>
                {x.room != null && <span><b>{x.room}</b> {t("buf.room")}</span>}
                <span><b>{x.sent}</b> {t("buf.sent")}</span>
                {x.errors > 0 && <span className="danger"><b>{x.errors}</b> {t("buf.failed")}</span>}
              </div>
              <div className="small muted">
                {x.enabled ? `${t("buf.per_day_short", { n: x.per_day })} · ${x.planned_until ? `${t("buf.until")} ${fmtWhen(x.planned_until)}` : t("buf.nothing_planned")}` : t("buf.off")}
              </div>
            </div>
          );
        })}
      </div>
      <div className="gx-toolbar">
        <div className="segmented" role="radiogroup" aria-label={t("buf.schedule")}>
          {(["upcoming", "sent", "problems"] as const).map((f) => (
            <button key={f} role="radio" aria-checked={filter === f} onClick={() => setFilter(f)}>{t(`buf.filter_${f}`)}</button>
          ))}
        </div>
        <span className="topbar-spacer" />
        <span className="small muted">{t("buf.requests", { n: v.requests_24h, max: v.budget })}</span>
        <button className="btn btn-sm" disabled={busy} onClick={() => act("/api/buffer/run")}>{t("buf.sync_now")}</button>
        {filter !== "upcoming" && <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => act("/api/buffer/clear")}>{t("buf.clear_done")}</button>}
      </div>
      {items.length === 0 ? (
        <div className="card gx-card gx-empty"><strong>{t("buf.empty")}</strong><p className="muted small">{t("buf.empty_hint")}</p></div>
      ) : (
        [...byDay.entries()].map(([day, list]) => (
          <div key={day} className="gx-day">
            <h4>{day} <span className="muted small">· {list.length}</span></h4>
            <ul className="gx-items">
              {list.map((i) => (
                <li key={i.id} className={`gx-item s-${i.state}`}>
                  <span className="mono gx-time" title={i.pinned ? t("buf.pinned") : undefined}>
                    {new Date(i.due_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}{i.pinned ? " 📌" : ""}
                  </span>
                  <span className={`chip chip-outline gx-p-${i.platform}`}>{PLAT_SHORT[i.platform]}</span>
                  <img className="gx-mini" src={thumb(i.project_id, i.cid)} alt="" loading="lazy" />
                  <span className="gx-item-title">{i.title}{i.error && <span className="small danger"> · {i.error}</span>}</span>
                  <span className={`chip ${i.state === "sent" ? "chip-success" : i.state === "error" ? "chip-danger" : "chip-outline"}`}>
                    {t(`buf.state_${i.state}`)}
                  </span>
                  {i.link && <a className="btn btn-sm btn-ghost" href={i.link} target="_blank" rel="noreferrer">{t("buf.open")}</a>}
                  {i.state === "scheduled" && <TimeEdit item={i} onDone={setV} />}
                  {i.state === "scheduled" && (
                    <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => act(`/api/buffer/items/${i.id}/cancel`)}>{t("buf.cancel")}</button>
                  )}
                  {["error", "cancelled"].includes(i.state) && (
                    <button className="btn btn-sm" disabled={busy} onClick={() => act(`/api/buffer/items/${i.id}/retry`)}>{t("buf.send_again")}</button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
      {v.log.length > 0 && (
        <details className="gx-details">
          <summary className="small">{t("autopost.log")}</summary>
          <ul className="small mono" style={{ paddingInlineStart: 16 }}>
            {v.log.map((l, n) => <li key={n}>{new Date(l.at * 1000).toLocaleString()} · {l.platform || "-"} · {l.kind} · {l.message}</li>)}
          </ul>
        </details>
      )}
    </section>
  );
}
