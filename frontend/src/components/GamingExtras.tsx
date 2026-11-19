// Local fork: Gaming page helpers: what is running now, how long a video takes on this PC, and the
// AI prompt (visible + the owner's extra instructions).
import { useEffect, useState } from "react";
import { req } from "../api";
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
