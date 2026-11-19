// Local fork: Gaming page helpers: what is running now, how long a video takes on this PC, and the
// AI prompt (visible + the owner's extra instructions).
import { useEffect, useState } from "react";
import { req, urls } from "../api";
import { useI18n } from "../i18n";
import { isActive, useJobs } from "../jobs";
import { jobLabel } from "./ui";
import { href } from "../router";

const mins = (s: number) => (s < 90 ? `${Math.max(1, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

// ===================================================================== what is running

export function ProgressCard() {
  const { t, num } = useI18n();
  const { jobs } = useJobs();
  const active = jobs.filter(isActive);
  const recent = jobs.filter((j) => j.status === "error" && j.finished && Date.now() / 1000 - j.finished < 3600).slice(0, 3);
  if (active.length === 0 && recent.length === 0) return null;
  const projects = [...new Set(active.map((j) => j.project_id ?? "import"))];
  return (
    <section className="card gx-card">
      <div className="gx-card-head">
        <h3>{t("gx.running_title")}</h3>
        <a className="btn btn-sm btn-ghost" href={href.queue}>{t("gx.open_queue")}</a>
      </div>
      {projects.map((pid) => {
        const list = active.filter((j) => (j.project_id ?? "import") === pid);
        const renders = list.filter((j) => j.kind === "render");
        const others = list.filter((j) => j.kind !== "render");
        return (
          <div key={pid} className="gx-run">
            {others.map((j) => (
              <div key={j.id} className="gx-run-row">
                <span className="small">{jobLabel(j, t, num)}</span>
                <div className={`progress ${j.progress == null && j.status === "running" ? "indeterminate" : ""}`}>
                  <span style={{ width: `${Math.round((j.progress ?? (j.status === "running" ? 0.3 : 0)) * 100)}%` }} />
                </div>
              </div>
            ))}
            {renders.length > 0 && (
              <div className="gx-run-row">
                <span className="small">{t("gx.rendering", { n: renders.length })}</span>
                <div className="progress">
                  <span style={{ width: `${Math.round(((renders.find((j) => j.status === "running")?.progress ?? 0)) * 100)}%` }} />
                </div>
              </div>
            )}
          </div>
        );
      })}
      {recent.map((j) => (
        <div key={j.id} className="notice notice-danger small">{jobLabel(j, t, num)}: {j.error?.message} {j.error?.hint ?? ""}</div>
      ))}
    </section>
  );
}

// ===================================================================== time on this PC

interface EstPlan { minutes: number; clips: number; stages: Record<string, number>; total_s: number; download_gb: number }
interface Est {
  measured: string[]; cloud: boolean; provider: string; style: string;
  rows: { minutes: number; now: EstPlan; local: EstPlan; cloud: EstPlan }[];
}

export function EstimateCard() {
  const { t } = useI18n();
  const [e, setE] = useState<Est | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    req<Est>("GET", "/api/gaming/estimate").then(setE).catch(() => undefined);
  }, []);
  if (!e) return null;
  const label = (m: number) => (m < 60 ? `${m} min` : `${m / 60} h`);
  const one = e.rows.find((r) => r.minutes === 60)!;
  return (
    <section className="card gx-card">
      <div className="gx-card-head">
        <h3>{t("gx.est_title")}</h3>
        <span className="chip chip-outline">{e.cloud ? t("gx.est_with_cloud") : t("gx.est_with_local")}</span>
      </div>
      <p className="small">{t("gx.est_one_hour", { t: mins(one.now.total_s), n: one.now.clips })}</p>
      <div className="gx-table-wrap">
        <table className="gx-table small">
          <thead>
            <tr><th>{t("gx.est_video")}</th><th>{t("gx.est_clips")}</th><th>{t("gx.est_cloud")}</th><th>{t("gx.est_local")}</th><th>{t("gx.est_download")}</th></tr>
          </thead>
          <tbody>
            {e.rows.map((r) => (
              <tr key={r.minutes} className={r.minutes === 60 ? "hl" : ""}>
                <td>{label(r.minutes)}</td><td>{r.now.clips}</td><td>{mins(r.cloud.total_s)}</td><td>{mins(r.local.total_s)}</td><td>{r.now.download_gb} GB</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <button className="btn btn-sm btn-ghost" onClick={() => setOpen(!open)}>{open ? t("buf.hide") : t("gx.est_steps")}</button>
      {open && (
        <ul className="small gx-stages">
          {Object.entries(one.now.stages).filter(([, s]) => s > 0).map(([k, s]) => (
            <li key={k}><span>{t(`gx.stage_${k}`)}</span><span className="mono">{mins(s)}</span></li>
          ))}
        </ul>
      )}
      <p className="muted small">{t("gx.est_note", { n: e.measured.length })}</p>
    </section>
  );
}

// ===================================================================== the AI prompt

interface Prompts { story: { system: string; user: string; extra: string }; package_system: string; auto_render: boolean }

export function PromptCard() {
  const { t } = useI18n();
  const [p, setP] = useState<Prompts | null>(null);
  const [extra, setExtra] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    req<Prompts>("GET", "/api/gaming/prompts").then((x) => { setP(x); setExtra(x.story.extra); }).catch(() => undefined);
  }, []);
  if (!p) return null;
  const save = async (body: Record<string, unknown>) => {
    const x = await req<Prompts>("PUT", "/api/gaming/prompts", body);
    setP(x);
    setSaved(true);
    window.setTimeout(() => setSaved(false), 1800);
  };
  return (
    <section className="card gx-card">
      <h3>{t("gx.prompt_title")}</h3>
      <p className="muted small">{t("gx.prompt_hint")}</p>
      <label className="field">
        <span className="field-label">{t("gx.prompt_extra")}</span>
        <textarea className="input" rows={3} maxLength={2000} value={extra} placeholder={t("gx.prompt_extra_ph")}
          onChange={(e) => setExtra(e.target.value)} />
      </label>
      <div className="gx-actions">
        <button className="btn btn-primary btn-sm" disabled={extra === p.story.extra} onClick={() => save({ extra })}>{saved ? t("gaming.saved") : t("gaming.save")}</button>
        <label className="check" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={p.auto_render} onChange={(e) => save({ auto_render: e.target.checked })} />
          <span>{t("gx.auto_render")}</span>
        </label>
      </div>
      <details className="gx-details">
        <summary>{t("gx.prompt_system")}</summary>
        <pre className="gx-pre">{p.story.system}</pre>
      </details>
      <details className="gx-details">
        <summary>{t("gx.prompt_task")}</summary>
        <pre className="gx-pre">{p.story.user}</pre>
      </details>
      <details className="gx-details">
        <summary>{t("gx.prompt_package")}</summary>
        <pre className="gx-pre">{p.package_system}</pre>
      </details>
    </section>
  );
}


// ===================================================================== trim before clipping

interface TrimVideo { id: string; title: string; duration: number; style: string; streamer: string | null }

const hms = (t: number) => {
  const s = Math.max(0, Math.round(t));
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const parseHms = (v: string): number | null => {
  const p = v.trim().split(":").map((x) => Number(x));
  if (!p.length || p.some((x) => !Number.isFinite(x) || x < 0)) return null;
  return p.reduce((acc, x) => acc * 60 + x, 0);
};

export function TrimCard() {
  const [list, setList] = useState<TrimVideo[]>([]);
  useEffect(() => {
    const load = () => req<{ videos: TrimVideo[] }>("GET", "/api/gaming/trim").then((r) => setList(r.videos)).catch(() => undefined);
    load();
    const iv = window.setInterval(load, 5000);
    return () => window.clearInterval(iv);
  }, []);
  if (list.length === 0) return null;
  return (
    <>
      {list.map((v) => (
        <TrimEditor key={v.id} v={v} frameUrl={(t) => urls.frame(v.id, t, 480)}
          onStart={async (a, b) => {
            await req("POST", `/api/gaming/trim/${encodeURIComponent(v.id)}`, { start: a, end: b });
            setList(list.filter((x) => x.id !== v.id));
          }} />
      ))}
    </>
  );
}

function TimeBox({ label, value, max, onChange }: { label: string; value: number; max: number; onChange: (v: number) => void }) {
  const [txt, setTxt] = useState(hms(value));
  useEffect(() => setTxt(hms(value)), [value]);
  return (
    <label className="field" style={{ maxWidth: 140 }}>
      <span className="field-label">{label}</span>
      <input className="input input-sm mono" value={txt} onChange={(e) => setTxt(e.target.value)}
        onBlur={() => {
          const n = parseHms(txt);
          if (n == null) setTxt(hms(value));
          else onChange(Math.min(max, n));
        }} />
    </label>
  );
}

export function TrimEditor({ v, frameUrl, onStart, note, onCancel }: {
  v: TrimVideo; frameUrl: (t: number) => string; onStart: (a: number, b: number) => Promise<void>;
  note?: string; onCancel?: () => void;
}) {
  const { t } = useI18n();
  const D = Math.max(60, v.duration);
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(D);
  const [est, setEst] = useState<{ clips: number; stories: number; total_s: number; stages: Record<string, number> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [frames, setFrames] = useState({ a: 0, b: Math.max(0, D - 15) });
  const len = Math.max(0, end - start);
  useEffect(() => {
    const h = window.setTimeout(() => {
      setFrames({ a: start, b: Math.max(start, end - 15) });
      req<any>("GET", `/api/gaming/estimate?minutes=${(len / 60).toFixed(1)}&style=${v.style}`).then(setEst).catch(() => setEst(null));
    }, 350);
    return () => window.clearTimeout(h);
  }, [start, end, len, v.style]);
  const step = D > 4 * 3600 ? 60 : D > 3600 ? 30 : 5;
  const go = async (a: number, b: number) => {
    setBusy(true);
    setErr(null);
    try {
      await onStart(a, b);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="card gx-card gx-trim">
      <div className="gx-card-head">
        <h3>{t("gx.trim_title")}</h3>
        <span className="chip chip-outline">{v.style === "both" ? t("gx.style_both") : v.style === "direct" ? t("gx.style_direct_short") : t("gx.style_streamer_short")}</span>
      </div>
      <p className="small"><b>{v.title}</b> · {hms(v.duration)}{v.streamer ? ` · ${v.streamer}` : ""}</p>
      <p className="muted small">{note ?? t("gx.trim_hint")}</p>
      <div className="gx-trim-frames">
        <figure><img src={frameUrl(frames.a)} alt="" /><figcaption className="small mono">{t("gx.trim_from")} {hms(start)}</figcaption></figure>
        <figure><img src={frameUrl(frames.b)} alt="" /><figcaption className="small mono">{t("gx.trim_to")} {hms(end)}</figcaption></figure>
      </div>
      <div className="gx-trim-bars">
        <label className="small">{t("gx.trim_from")}
          <input type="range" min={0} max={D} step={step} value={start} aria-label={t("gx.trim_from")}
            onChange={(e) => setStart(Math.min(Number(e.target.value), end - 60))} />
        </label>
        <label className="small">{t("gx.trim_to")}
          <input type="range" min={0} max={D} step={step} value={end} aria-label={t("gx.trim_to")}
            onChange={(e) => setEnd(Math.max(Number(e.target.value), start + 60))} />
        </label>
      </div>
      <div className="gx-row">
        <TimeBox label={t("gx.trim_from")} value={start} max={end - 60} onChange={setStart} />
        <TimeBox label={t("gx.trim_to")} value={end} max={D} onChange={(x) => setEnd(Math.max(x, start + 60))} />
      </div>
      <div className="notice small gx-trim-est" role="status">
        <b>{t("gx.trim_selected", { len: hms(len) })}</b>
        {est && <> · {t("gx.trim_est", { clips: est.clips, stories: est.stories, time: mins(est.total_s) })}</>}
      </div>
      <div className="gx-actions">
        <button className="btn btn-primary" disabled={busy || len < 60} onClick={() => go(start, end)}>{t("gx.trim_start")}</button>
        <button className="btn btn-ghost" disabled={busy} onClick={() => go(0, D)}>{t("gx.trim_whole")}</button>
        {onCancel && <button className="btn btn-ghost" disabled={busy} onClick={onCancel}>{t("buf.cancel")}</button>}
        {busy && <span className="spinner" aria-hidden />}
      </div>
      {err && <div className="notice notice-danger small" role="status">{err}</div>}
    </section>
  );
}
