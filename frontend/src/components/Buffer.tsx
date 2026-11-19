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
  window_start: string;
  window_end: string;
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
  due_at: string; state: "waiting" | "scheduled" | "sent" | "error" | "cancelled"; post_id: string | null;
  error: string | null; link?: string | null;
}
export interface BView {
  settings: BSettings;
  keys: { buffer: string | null; has_buffer: boolean; has_cloudinary: boolean;
    cloudinary: { cloud_name: string | null; api_key: string | null; has_secret: boolean } };
  platforms: Record<BPlatform, { label: string; enabled: boolean; per_day: number; channel: string; channel_name: string;
    waiting: number; scheduled: number; sent: number; errors: number; next_free: string[]; planned_until: string | null }>;
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
const fmtWhen = (iso: string) => {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const thumb = (pid: string, cid: string) => `/api/buffer/thumb/${encodeURIComponent(pid)}/${encodeURIComponent(cid)}.jpg`;
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
      setV(await req<BView>("PUT", "/api/buffer/keys", body));
      setKey("");
      setCloud({ cloud_name: "", api_key: "", api_secret: "" });
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
        <StatusChip ok={v.keys.has_buffer && v.keys.has_cloudinary && !!s.organization_id} t={t} />
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
          <strong>2 · {t("buf.cloud_title")}</strong>
          <p className="muted small">{t("buf.cloud_hint")}</p>
          {v.keys.cloudinary.cloud_name && (
            <div className="small mono">{v.keys.cloudinary.cloud_name} · {v.keys.cloudinary.api_key}</div>
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
        </div>
      </div>
      <div className="gx-actions">
        <button className="btn btn-primary" disabled={busy || (!key.trim() && !cloud.cloud_name && !cloud.api_key && !cloud.api_secret)}
          onClick={saveKeys}>{t("buf.save_test")}</button>
        <button className="btn" disabled={busy || !v.keys.has_buffer} onClick={runTest}>{t("buf.test")}</button>
        {(v.keys.has_buffer || v.keys.has_cloudinary) && (
          <button className="btn btn-ghost btn-sm" onClick={async () => setV(await req<BView>("PUT", "/api/buffer/keys", { buffer: "", cloudinary: {} }))}>
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
          <li className={test.cloudinary?.ok ? "ok" : "bad"}>
            Cloudinary: {test.cloudinary?.ok ? `${test.cloudinary.plan ?? "OK"}${test.cloudinary.credits_limit ? ` · ${test.cloudinary.credits_used}/${test.cloudinary.credits_limit} credits` : ""}`
              : `${test.cloudinary?.error ?? ""} ${test.cloudinary?.hint ?? ""}`}
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
              <label className="field">
                <span className="field-label">{t("buf.per_day")}</span>
                <input className="input input-sm" type="number" min={1} max={50} value={s.per_day[p]}
                  onChange={(e) => save({ per_day: { [p]: Number(e.target.value) } })} />
              </label>
            </div>
          );
        })}
      </div>

      <h4 style={{ marginTop: 16 }}>4 · {t("buf.schedule_title")}</h4>
      <p className="muted small">{t("buf.schedule_hint")}</p>
      <div className="gx-row">
        <label className="field">
          <span className="field-label">{t("buf.window_start")}</span>
          <input className="input input-sm" type="time" value={s.window_start} onChange={(e) => save({ window_start: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-label">{t("buf.window_end")}</span>
          <input className="input input-sm" type="time" value={s.window_end} onChange={(e) => save({ window_end: e.target.value })} />
        </label>
        <TimesField value={s.times} onSave={(times) => save({ times })} />
        <label className="field">
          <span className="field-label">{t("buf.queue_cap")}</span>
          <input className="input input-sm" type="number" min={1} max={100} value={s.queue_cap}
            onChange={(e) => save({ queue_cap: Number(e.target.value) })} />
        </label>
      </div>

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

function TimesField({ value, onSave }: { value: string[]; onSave: (v: string[]) => void }) {
  const { t } = useI18n();
  const [txt, setTxt] = useState(value.join(", "));
  useEffect(() => setTxt(value.join(", ")), [value]);
  return (
    <label className="field" style={{ flex: "2 1 220px" }}>
      <span className="field-label">{t("buf.fixed_times")}</span>
      <input className="input input-sm" value={txt} placeholder="12:00, 15:00, 18:00, 21:00"
        onChange={(e) => setTxt(e.target.value)} onBlur={() => onSave(txt.split(/[ ,;]+/).filter(Boolean))} />
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

export function BufferPicker({ onSent, goSetup }: { onSent: () => void; goSetup: () => void }) {
  const { t } = useI18n();
  const { v, setV } = useBuffer();
  const [clips, setClips] = useState<BClip[] | null>(null);
  const [sel, setSel] = useState<Record<string, boolean>>({});
  const [edit, setEdit] = useState<Record<string, { title: string; caption: string }>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [plats, setPlats] = useState<Record<BPlatform, boolean>>({ instagram: true, tiktok: true, youtube: true });
  const [project, setProject] = useState<string>("all");
  const [onlyNew, setOnlyNew] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = useCallback(() => req<{ clips: BClip[] }>("GET", "/api/buffer/clips").then((r) => setClips(r.clips)).catch(() => setClips([])), []);
  useEffect(() => {
    load();
  }, [load]);
  const projects = useMemo(() => {
    const m = new Map<string, string>();
    (clips ?? []).forEach((c) => m.set(c.project_id, c.project_title));
    return [...m.entries()];
  }, [clips]);
  const shown = (clips ?? []).filter((c) => (project === "all" || c.project_id === project)
    && (!onlyNew || Object.keys(c.queued).length === 0));
  const k = (c: BClip) => `${c.project_id}/${c.cid}`;
  const picked = shown.filter((c) => sel[k(c)]);
  const ready = !!v && v.keys.has_buffer && v.keys.has_cloudinary && !!v.settings.organization_id;
  const activePlats = B_PLATFORMS.filter((p) => plats[p] && v?.settings.platforms[p]);
  const send = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const picks = picked.map((c) => ({ project_id: c.project_id, cid: c.cid, platforms: activePlats,
        title: edit[k(c)]?.title ?? c.title, caption: edit[k(c)]?.caption ?? c.caption }));
      const r = await req<{ added: number; view: BView }>("POST", "/api/buffer/queue", { picks });
      setV(r.view);
      setSel({});
      setMsg({ ok: true, text: t("buf.sent_ok", { n: r.added }) });
      load();
      onSent();
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
              <article key={key} className={`gx-clip ${on ? "on" : ""}`}>
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
        {v && activePlats.length > 0 && picked.length > 0 && (
          <span className="small muted">
            {t("buf.first_slot")}: {v.platforms[activePlats[0]].next_free[0] ? fmtWhen(v.platforms[activePlats[0]].next_free[0]) : "-"}
            {" · "}{t("buf.per_day_short", { n: v.settings.per_day[activePlats[0]] })}
          </span>
        )}
        <span className="topbar-spacer" />
        <button className="btn btn-primary" disabled={busy || !ready || picked.length === 0 || activePlats.length === 0} onClick={send}>
          {busy ? <span className="spinner" /> : null} {t("buf.send")}
        </button>
      </div>
    </section>
  );
}

// ===================================================================== the schedule (outbox)

export function BufferSchedule() {
  const { t } = useI18n();
  const { v, setV, load } = useBuffer();
  const [filter, setFilter] = useState<"upcoming" | "sent" | "problems">("upcoming");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const iv = window.setInterval(load, 20000);
    return () => window.clearInterval(iv);
  }, [load]);
  if (!v) return null;
  const items = v.items.filter((i) => filter === "upcoming" ? ["waiting", "scheduled"].includes(i.state)
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
      <div className="gx-stats">
        {B_PLATFORMS.map((p) => {
          const x = v.platforms[p];
          return (
            <div key={p} className={`card gx-stat ${x.enabled ? "" : "off"}`}>
              <strong>{x.label}</strong>
              <div className="gx-stat-nums">
                <span><b>{x.scheduled}</b> {t("buf.in_buffer")}</span>
                <span><b>{x.waiting}</b> {t("buf.waiting")}</span>
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
                  <span className="mono gx-time">{new Date(i.due_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span>
                  <span className={`chip chip-outline gx-p-${i.platform}`}>{PLAT_SHORT[i.platform]}</span>
                  <img className="gx-mini" src={thumb(i.project_id, i.cid)} alt="" loading="lazy" />
                  <span className="gx-item-title">{i.title}{i.error && <span className="small danger"> · {i.error}</span>}</span>
                  <span className={`chip ${i.state === "sent" ? "chip-success" : i.state === "error" ? "chip-danger" : "chip-outline"}`}>
                    {t(`buf.state_${i.state}`)}
                  </span>
                  {i.link && <a className="btn btn-sm btn-ghost" href={i.link} target="_blank" rel="noreferrer">{t("buf.open")}</a>}
                  {["waiting", "scheduled"].includes(i.state) && (
                    <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => act(`/api/buffer/items/${i.id}/cancel`)}>{t("buf.cancel")}</button>
                  )}
                  {["error", "cancelled"].includes(i.state) && (
                    <button className="btn btn-sm" disabled={busy} onClick={() => act(`/api/buffer/items/${i.id}/retry`)}>{t("buf.retry")}</button>
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
