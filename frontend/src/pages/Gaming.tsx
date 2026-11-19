// Local gaming fork: the clipper in four tabs: 1 add a video (local file, YouTube VOD, YouTube LIVE)
// in a clip style -> the AI finds the stories and the clips render by themselves; 2 pick clips and
// send them to Buffer; 3 the posting schedule; Setup: AI (Claude / Gemini / local), the prompt,
// Buffer + video hosting, clip lengths. Analysis + rendering run on this PC.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, req, uploadFile } from "../api";
import { useI18n } from "../i18n";
import { href } from "../router";
import { BufferPicker, BufferSchedule, BufferSetupCard, type BView } from "../components/Buffer";
import { EstimateCard, ProgressCard, PromptCard } from "../components/GamingExtras";

type LiveState =
  | "starting" | "connecting" | "recording" | "reconnecting" | "paused" | "stopped" | "ended" | "failed"
  | "interrupted" | "stopped_disk" | "pausing" | "stopping";

interface LiveCand {
  id: string;
  kind: "auto" | "mark";
  start: number;
  end: number;
  peak: number;
  score: number;
  status: "new" | "clipping" | "clipped" | "dismissed";
  reasons: { code: string; value?: string; strength: number }[];
  project_id?: string;
  wall?: string | null;
}

interface LiveSession {
  id: string;
  state: LiveState;
  url: string;
  name: string;
  title: string | null;
  channel: string | null;
  via: string | null;
  started_at: string;
  elapsed_s: number | null;
  recorded_s: number;
  buffered_s: number;
  first_t: number;
  segments: number;
  deleted_segments: number;
  last_segment: { n: number; t0: number; dur: number; wall_end: string } | null;
  bytes: number;
  bitrate_bps: number;
  disk: { free_bytes: number; low: boolean; critical: boolean; hours_left: number | null };
  analysis: { analyzed_until: number; pending_s: number; candidates: number };
  reconnects: number;
  errors: { at: string; text: string }[];
  settings: { quality: number; retention: "rolling" | "archive"; keep_minutes: number };
  backfill: { state: string; reason?: string; note?: string; file?: string } | null;
  end_reason: string | null;
  project_id: string | null;
  candidates: LiveCand[];
  timeline?: { step_s: number; db: number[] };
}

interface LiveOverview {
  sessions: LiveSession[];
  settings: LiveSession["settings"];
  tools: { ffmpeg: string | null; streamlink: string | null; yt_dlp: string | null; js_runtimes: string[] };
  clip_last: number[];
  disk_estimate_gb: Record<string, number>;
}

const ACTIVE: LiveState[] = ["starting", "connecting", "recording", "reconnecting", "pausing", "stopping"];

const hms = (t: number) => {
  const s = Math.max(0, Math.floor(t));
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const gb = (b: number) => `${(b / 1e9).toFixed(1)} GB`;

type GTab = "create" | "pick" | "schedule" | "setup";
const TAB_KEY = "sc.gaming.tab";

export default function Gaming() {
  const { t } = useI18n();
  const [ov, setOv] = useState<LiveOverview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [style, setStyleState] = useState<ClipStyle>("streamer");
  const [tab, setTabState] = useState<GTab>(() => {
    try {
      const v = localStorage.getItem(TAB_KEY);
      return v === "pick" || v === "schedule" || v === "setup" ? v : "create";
    } catch {
      return "create";
    }
  });
  const setTab = (v: GTab) => {
    setTabState(v);
    try {
      localStorage.setItem(TAB_KEY, v);
    } catch {
      /* private window */
    }
    window.scrollTo({ top: 0 });
  };
  const [ready, setReady] = useState<{ ai: string; buffer: boolean } | null>(null);
  const loadReady = useCallback(() => {
    Promise.all([req<AIView>("GET", "/api/gaming/ai"), req<BView>("GET", "/api/buffer")])
      .then(([a, b]) => setReady({ ai: a.provider, buffer: b.keys.has_buffer && b.keys.has_cloudinary && !!b.settings.organization_id }))
      .catch(() => undefined);
  }, []);
  useEffect(loadReady, [loadReady, tab]);
  useEffect(() => {
    req<{ clip_style?: ClipStyle }>("GET", "/api/gaming/settings")
      .then((s) => setStyleState(s.clip_style === "direct" ? "direct" : "streamer")).catch(() => undefined);
  }, []);
  const setStyle = (v: ClipStyle) => {
    setStyleState(v);
    req("PUT", "/api/gaming/settings", { clip_style: v }).catch(() => undefined);
  };

  const load = useCallback(async () => {
    try {
      const o = await req<LiveOverview>("GET", "/api/liverec");
      setOv(o);
      setErr(null);
      setOpen((cur) => cur ?? o.sessions.find((s) => ACTIVE.includes(s.state))?.id ?? o.sessions[0]?.id ?? null);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }, []);
  useEffect(() => {
    load();
    const iv = window.setInterval(load, 4000);
    return () => window.clearInterval(iv);
  }, [load]);

  const tabs: { id: GTab; label: string; badge?: string }[] = [
    { id: "create", label: `1 · ${t("gx.tab_create")}` },
    { id: "pick", label: `2 · ${t("gx.tab_pick")}` },
    { id: "schedule", label: `3 · ${t("gx.tab_schedule")}` },
    { id: "setup", label: t("gx.tab_setup"), badge: ready && (!ready.buffer || ready.ai === "ollama") ? "!" : undefined },
  ];
  return (
    <div className="page gaming-page">
      <div className="page-head">
        <div>
          <h1 className="display">{t("gaming.title")}</h1>
          <p className="lede muted">{t("gx.lede")}</p>
        </div>
      </div>
      <ol className="gx-steps" aria-label={t("gx.how")}>
        <li>{t("gx.step1")}</li>
        <li>{t("gx.step2")}</li>
        <li>{t("gx.step3")}</li>
        <li>{t("gx.step4")}</li>
      </ol>
      {ready && (ready.ai === "ollama" || !ready.buffer) && tab !== "setup" && (
        <div className="notice notice-warn small gx-ready" role="status">
          {ready.ai === "ollama" ? t("gx.need_ai") : null} {!ready.buffer ? t("gx.need_buffer") : null}
          <button className="btn btn-sm" onClick={() => setTab("setup")}>{t("buf.open_setup")}</button>
        </div>
      )}
      <div className="gx-tabs" role="tablist" aria-label={t("gaming.title")}>
        {tabs.map((x) => (
          <button key={x.id} role="tab" id={`gx-tab-${x.id}`} aria-selected={tab === x.id} aria-controls={`gx-panel-${x.id}`}
            className={tab === x.id ? "on" : ""} onClick={() => setTab(x.id)}>
            {x.label}{x.badge && <span className="gx-badge" aria-label={t("buf.not_ready")}>{x.badge}</span>}
          </button>
        ))}
      </div>
      {err && <div className="notice notice-danger">{err}</div>}
      <div role="tabpanel" id={`gx-panel-${tab}`} aria-labelledby={`gx-tab-${tab}`} className="gx-panel">
        {tab === "create" && (
          <>
            <StyleCard style={style} onChange={setStyle} />
            <div className="grid-cards" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 16, marginTop: 16 }}>
              <SourceCard kind="file" style={style} />
              <SourceCard kind="vod" style={style} />
              <LiveStartCard ov={ov} onStarted={(id) => { setOpen(id); load(); }} />
            </div>
            <ProgressCard />
            {ov && ov.sessions.length > 0 && (
              <section style={{ marginTop: 24 }}>
                <h2>{t("live.sessions")}</h2>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
                  {ov.sessions.map((s) => (
                    <button key={s.id} className={`btn btn-sm ${open === s.id ? "btn-primary" : ""}`} onClick={() => setOpen(s.id)}>
                      {s.name || s.title || s.id} · {t(`live.state.${s.state}`)}
                    </button>
                  ))}
                </div>
                {open && <LiveSessionView sid={open} clipLast={ov.clip_last} onChange={load} />}
              </section>
            )}
            <EstimateCard />
          </>
        )}
        {tab === "pick" && <BufferPicker goSetup={() => setTab("setup")} />}
        {tab === "schedule" && <BufferSchedule />}
        {tab === "setup" && (
          <>
            <AICard onChange={loadReady} />
            <PromptCard />
            <BufferSetupCard onReady={loadReady} />
            <TuningCard />
            <ExportCard />
            <details className="gx-details gx-advanced">
              <summary>{t("gx.advanced_direct")}</summary>
              <AutoPostCard />
            </details>
          </>
        )}
      </div>
    </div>
  );
}

type ClipStyle = "streamer" | "direct";

function StyleCard({ style, onChange }: { style: ClipStyle; onChange: (v: ClipStyle) => void }) {
  const { t } = useI18n();
  const opt = (v: ClipStyle, title: string, hint: string) => (
    <button role="radio" aria-checked={style === v} onClick={() => onChange(v)}
      className={`card ${style === v ? "card-selected" : ""}`}
      style={{ padding: 12, textAlign: "left", flex: "1 1 260px", cursor: "pointer",
        outline: style === v ? "2px solid var(--accent, #4f8cff)" : undefined }}>
      <strong>{title}</strong>
      <div className="muted small" style={{ marginTop: 4 }}>{hint}</div>
    </button>
  );
  return (
    <section style={{ marginTop: 8 }}>
      <h2 className="small" style={{ marginBottom: 8 }}>{t("gaming.style_title")}</h2>
      <div role="radiogroup" aria-label={t("gaming.style_title")} style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        {opt("streamer", t("gaming.style_streamer"), t("gaming.style_streamer_hint"))}
        {opt("direct", t("gaming.style_direct"), t("gaming.style_direct_hint"))}
      </div>
    </section>
  );
}

function SourceCard({ kind, style }: { kind: "file" | "vod"; style: ClipStyle }) {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const [streamer, setStreamer] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const body = kind === "file" ? { path: value.trim(), mode: "gaming" as const, language: "auto", clip_style: style, streamer }
        : { url: value.trim(), mode: "gaming" as const, language: "auto", clip_style: style, streamer };
      await api.create(body);
      setMsg(t("gaming.queued"));
      setValue("");
    } catch (e) {
      setMsg(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  };
  const fileInput = useRef<HTMLInputElement>(null);
  const [up, setUp] = useState<number | null>(null);
  const [drag, setDrag] = useState(false);
  const sendFile = async (f: File | null | undefined) => {
    if (!f) return;
    setMsg(null);
    setUp(0);
    try {
      await uploadFile(f, "gaming", "auto", setUp, false, style, streamer).promise;
      setMsg(t("gaming.queued"));
    } catch (e) {
      setMsg(String((e as Error).message || e));
    } finally {
      setUp(null);
    }
  };
  return (
    <div className="card" style={{ padding: 16 }}>
      <h3>{t(kind === "file" ? "gaming.file_title" : "gaming.vod_title")}</h3>
      <p className="muted small">{t(kind === "file" ? "gx.file_hint" : "gaming.vod_hint")}</p>
      {kind === "file" && (
        <div className={`gx-drop ${drag ? "on" : ""}`} role="button" tabIndex={0}
          onClick={() => fileInput.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && fileInput.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
          onDrop={(e) => { e.preventDefault(); setDrag(false); sendFile(e.dataTransfer.files?.[0]); }}>
          {up != null ? (
            <>
              <strong>{t("gx.uploading", { p: Math.round(up * 100) })}</strong>
              <div className="progress" style={{ width: "100%" }}><span style={{ width: `${Math.round(up * 100)}%` }} /></div>
            </>
          ) : (
            <>
              <strong>{t("gx.drop")}</strong>
              <span className="small muted">MP4 · MKV · MOV · TS</span>
            </>
          )}
          <input ref={fileInput} type="file" accept="video/*,.mkv,.flv,.ts" hidden onChange={(e) => sendFile(e.target.files?.[0])} />
        </div>
      )}
      <label className="field">
        <span className="field-label">{t(kind === "file" ? "gaming.file_path" : "gaming.vod_url")}</span>
        <input
          className="input"
          value={value}
          placeholder={kind === "file" ? "/home/me/Videos/stream.mkv" : "https://www.youtube.com/watch?v=..."}
          onChange={(e) => setValue(e.target.value)}
        />
      </label>
      <label className="field">
        <span className="field-label">{t("gx.streamer_name")}</span>
        <input className="input input-sm" value={streamer} placeholder="IShowSpeed" maxLength={60}
          onChange={(e) => setStreamer(e.target.value)} />
      </label>
      <button className="btn btn-primary" disabled={busy || !value.trim()} onClick={submit} style={{ marginTop: 8 }}>
        {t("gaming.import")}
      </button>
      {msg && <p className="small" style={{ marginTop: 8 }} role="status">{msg === t("gaming.queued") ? t("gx.queued") : msg}</p>}
    </div>
  );
}

function LiveStartCard({ ov, onStarted }: { ov: LiveOverview | null; onStarted: (id: string) => void }) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [quality, setQuality] = useState(720);
  const [retention, setRetention] = useState<"rolling" | "archive">("rolling");
  const [keep, setKeep] = useState(90);
  const [ok, setOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (ov) {
      setQuality(ov.settings.quality);
      setRetention(ov.settings.retention);
      setKeep(ov.settings.keep_minutes);
    }
  }, [ov?.settings.quality, ov?.settings.retention, ov?.settings.keep_minutes]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const s = await req<LiveSession>("POST", "/api/liverec/sessions", { url: url.trim(), name, quality, retention, keep_minutes: keep });
      onStarted(s.id);
      setUrl("");
    } catch (e) {
      setMsg(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card" style={{ padding: 16 }}>
      <h3>{t("live.title")}</h3>
      <p className="muted small">{t("live.from_now")}</p>
      <label className="field">
        <span className="field-label">{t("live.url")}</span>
        <input className="input" value={url} placeholder="https://www.youtube.com/watch?v=..." onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label className="field">
        <span className="field-label">{t("live.name")}</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 6 }}>
        <select className="select select-sm" value={quality} onChange={(e) => setQuality(Number(e.target.value))} aria-label={t("live.quality")}>
          <option value={720}>720p ({t("live.low_bw")})</option>
          <option value={1080}>1080p</option>
        </select>
        <select className="select select-sm" value={retention} onChange={(e) => setRetention(e.target.value as "rolling" | "archive")} aria-label={t("live.retention")}>
          <option value="rolling">{t("live.rolling")}</option>
          <option value="archive">{t("live.archive")}</option>
        </select>
        {retention === "rolling" && (
          <select className="select select-sm" value={keep} onChange={(e) => setKeep(Number(e.target.value))} aria-label={t("live.keep")}>
            {[30, 45, 60, 90, 120].map((m) => <option key={m} value={m}>{m} min</option>)}
          </select>
        )}
      </div>
      <p className="muted small" style={{ marginTop: 6 }}>
        {t("live.disk_note")} {ov ? `(7 h @ 4 Mbps ≈ ${ov.disk_estimate_gb["7h_4mbps"]} GB, @ 8 Mbps ≈ ${ov.disk_estimate_gb["7h_8mbps"]} GB)` : ""}
      </p>
      <label className="check small" style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <input type="checkbox" checked={ok} onChange={(e) => setOk(e.target.checked)} />
        {t("live.authorized")}
      </label>
      <button className="btn btn-primary" disabled={busy || !ok || !url.trim()} onClick={start} style={{ marginTop: 8 }}>
        {t("live.start")}
      </button>
      {msg && <p className="notice notice-danger small" style={{ marginTop: 8 }}>{msg}</p>}
      {ov && !ov.tools.yt_dlp && <p className="notice notice-danger small">yt-dlp missing</p>}
    </div>
  );
}

function Timeline({ s, onSeek }: { s: LiveSession; onSeek: (t: number) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current;
    if (!cv || !s.timeline) return;
    const W = cv.clientWidth || 800;
    const H = 70;
    cv.width = W;
    cv.height = H;
    const ctx = cv.getContext("2d")!;
    ctx.clearRect(0, 0, W, H);
    const pts = s.timeline.db;
    const total = Math.max(1, s.recorded_s);
    const css = getComputedStyle(document.documentElement);
    ctx.fillStyle = css.getPropertyValue("--surface-2") || "#1a1f2b";
    ctx.fillRect(0, 0, W, H);
    // kept part of the buffer
    ctx.fillStyle = "rgba(120,140,255,0.08)";
    ctx.fillRect((s.first_t / total) * W, 0, W - (s.first_t / total) * W, H);
    ctx.strokeStyle = "#7f8cff";
    ctx.beginPath();
    pts.forEach((v, i) => {
      const x = ((i * s.timeline!.step_s) / total) * W;
      const y = H - Math.max(0, Math.min(1, (v + 70) / 60)) * (H - 4);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    for (const c of s.candidates) {
      if (c.status === "dismissed") continue;
      ctx.fillStyle = c.kind === "mark" ? "rgba(255,200,60,0.55)" : "rgba(255,80,120,0.45)";
      ctx.fillRect((c.start / total) * W, 0, Math.max(2, ((c.end - c.start) / total) * W), H);
    }
  }, [s]);
  return (
    <canvas
      ref={ref}
      style={{ width: "100%", height: 70, borderRadius: 6, cursor: "pointer" }}
      onClick={(e) => {
        const r = (e.target as HTMLCanvasElement).getBoundingClientRect();
        onSeek(((e.clientX - r.left) / r.width) * s.recorded_s);
      }}
      aria-label="timeline"
    />
  );
}

function LiveSessionView({ sid, clipLast, onChange }: { sid: string; clipLast: number[]; onChange: () => void }) {
  const { t } = useI18n();
  const [s, setS] = useState<LiveSession | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ t0: number; t1: number } | null>(null);
  const [renderNow, setRenderNow] = useState(true);
  const load = useCallback(async () => {
    try {
      setS(await req<LiveSession>("GET", `/api/liverec/sessions/${sid}?timeline=1`));
    } catch (e) {
      setMsg(String((e as Error).message || e));
    }
  }, [sid]);
  useEffect(() => {
    load();
    const iv = window.setInterval(load, 3000);
    return () => window.clearInterval(iv);
  }, [load]);
  const act = async (method: string, path: string, body?: unknown, note?: string) => {
    setMsg(null);
    try {
      await req(method, `/api/liverec/sessions/${sid}${path}`, body);
      if (note) setMsg(note);
      await load();
      onChange();
    } catch (e) {
      setMsg(String((e as Error).message || e));
    }
  };
  const cands = useMemo(() => (s ? s.candidates.filter((c) => c.status !== "dismissed") : []), [s]);
  if (!s) return <div className="skeleton" style={{ height: 200 }} />;
  const active = ACTIVE.includes(s.state);
  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
        <div>
          <h3 style={{ margin: 0 }}>{s.name || s.title || s.id}</h3>
          <div className="muted small mono">{s.channel ? `${s.channel} · ` : ""}{s.url}</div>
        </div>
        <span className={`status ${active ? "status-running" : s.state === "failed" ? "status-error" : "status-ready"}`}>
          <span className="dot" /> {t(`live.state.${s.state}`)}
        </span>
      </div>
      <dl className="live-stats" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 8, margin: "12px 0" }}>
        <Stat k={t("live.elapsed")} v={s.elapsed_s != null ? hms(s.elapsed_s) : "-"} />
        <Stat k={t("live.recorded")} v={hms(s.recorded_s)} />
        <Stat k={t("live.buffered")} v={`${hms(s.buffered_s)} (${s.segments} seg.)`} />
        <Stat k={t("live.last_segment")} v={s.last_segment ? `#${s.last_segment.n} @ ${hms(s.last_segment.t0)}` : "-"} />
        <Stat k={t("live.disk_used")} v={gb(s.bytes)} />
        <Stat k={t("live.disk_free")} v={`${gb(s.disk.free_bytes)}${s.disk.hours_left != null ? ` (~${s.disk.hours_left} h)` : ""}`} warn={s.disk.low} />
        <Stat k={t("live.analysis")} v={`${hms(s.analysis.analyzed_until)} / ${t("live.pending")} ${Math.round(s.analysis.pending_s)} s`} />
        <Stat k={t("live.reconnects")} v={String(s.reconnects)} />
      </dl>
      {s.disk.critical && <div className="notice notice-danger">{t("live.disk_critical")}</div>}
      {!s.disk.critical && s.disk.low && <div className="notice notice-danger">{t("live.disk_low")}</div>}
      {s.end_reason && <div className="notice small">{s.end_reason}</div>}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", margin: "8px 0" }}>
        {active && <button className="btn" onClick={() => act("POST", "/pause")}>{t("live.pause")}</button>}
        {active && <button className="btn" onClick={() => act("POST", "/stop")}>{t("live.stop")}</button>}
        {!active && ["paused", "interrupted", "stopped", "failed", "stopped_disk"].includes(s.state) && (
          <button className="btn" onClick={() => act("POST", "/resume")}>{t("live.resume")}</button>
        )}
        <button className="btn btn-primary" disabled={!s.segments} onClick={() => act("POST", "/mark", {}, t("live.marked"))}>
          {t("live.mark")}
        </button>
        {clipLast.map((n) => (
          <button key={n} className="btn" disabled={s.recorded_s - s.first_t < n * 0.5}
            onClick={() => act("POST", "/clip-last", { seconds: n, render: renderNow }, t("live.clip_queued"))}>
            {t("live.clip_last", { n })}
          </button>
        ))}
        <label className="check small" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input type="checkbox" checked={renderNow} onChange={(e) => setRenderNow(e.target.checked)} />
          {t("live.render_now")}
        </label>
      </div>
      <p className="muted small">{t("live.no_magic")}</p>
      {s.timeline && <Timeline s={s} onSeek={(x) => setPreview({ t0: Math.max(s.first_t, x - 15), t1: Math.max(s.first_t, x - 15) + 30 })} />}
      {preview && (
        <div style={{ marginTop: 8 }}>
          <video key={`${preview.t0}-${preview.t1}`} controls autoPlay style={{ width: "100%", maxHeight: 360, background: "#000" }}
            src={`/api/liverec/sessions/${sid}/window.mp4?t0=${preview.t0.toFixed(1)}&t1=${preview.t1.toFixed(1)}`} />
          <div className="muted small">{t("live.preview_of", { a: hms(preview.t0), b: hms(preview.t1) })}</div>
        </div>
      )}
      <h4 style={{ marginTop: 16 }}>{t("live.candidates")} ({cands.length})</h4>
      {cands.length === 0 && <p className="muted small">{t("live.no_candidates")}</p>}
      <ul className="live-cands" style={{ listStyle: "none", padding: 0, display: "grid", gap: 8 }}>
        {cands.map((c) => (
          <li key={c.id} className="card" style={{ padding: 10, display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <span className="mono">{c.id}</span>
            <span className="mono">{hms(c.start)}–{hms(c.end)}</span>
            <span className="chip">{c.kind === "mark" ? t("live.kind_mark") : `${t("live.score")} ${c.score}`}</span>
            <span className="small muted" title={t("live.why")}>
              {c.reasons.map((r) => `${t(`live.reason.${r.code}`)}${r.value ? ` ${r.value}` : ""}`).join(" · ")}
            </span>
            <span style={{ flex: 1 }} />
            <button className="btn btn-sm" disabled={c.end > s.recorded_s} onClick={() => setPreview({ t0: c.start, t1: c.end })}>{t("live.preview")}</button>
            {c.status === "clipped" && c.project_id ? (
              <a className="btn btn-sm" href={href.project(c.project_id)}>{t("live.open_project")}</a>
            ) : (
              <button className="btn btn-sm btn-primary" disabled={c.status === "clipping" || c.end > s.recorded_s}
                onClick={() => act("POST", `/candidates/${c.id}/clip`, { render: renderNow }, t("live.clip_queued"))}>
                {c.status === "clipping" ? t("live.clipping") : c.end > s.recorded_s ? t("live.waiting") : t("live.make_clip")}
              </button>
            )}
            <button className="btn btn-sm" onClick={() => act("POST", `/candidates/${c.id}/dismiss`)}>{t("live.dismiss")}</button>
          </li>
        ))}
      </ul>
      <details style={{ marginTop: 12 }}>
        <summary>{t("live.retention")}</summary>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
          <select className="select select-sm" value={s.settings.retention}
            onChange={(e) => act("PUT", "/retention", { retention: e.target.value })} aria-label={t("live.retention")}>
            <option value="rolling">{t("live.rolling")}</option>
            <option value="archive">{t("live.archive")}</option>
          </select>
          <select className="select select-sm" value={s.settings.keep_minutes}
            onChange={(e) => act("PUT", "/retention", { keep_minutes: Number(e.target.value) })} aria-label={t("live.keep")}>
            {[30, 45, 60, 90, 120].map((m) => <option key={m} value={m}>{m} min</option>)}
          </select>
          <span className="muted small">{t("live.retention_hint", { n: s.deleted_segments })}</span>
        </div>
      </details>
      <details style={{ marginTop: 8 }}>
        <summary>{t("live.backfill")}</summary>
        <p className="small">{t("live.backfill_warn")}</p>
        {s.backfill && (
          <p className="small mono">
            {s.backfill.state}{s.backfill.reason ? ` - ${s.backfill.reason}` : ""}{s.backfill.file ? ` (${s.backfill.file})` : ""}
          </p>
        )}
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-sm" disabled={s.backfill?.state === "running"} onClick={() => act("POST", "/backfill")}>{t("live.backfill_start")}</button>
          {s.backfill?.state === "running" && <button className="btn btn-sm" onClick={() => act("DELETE", "/backfill")}>{t("live.backfill_cancel")}</button>}
          {s.backfill?.state === "finished" && <button className="btn btn-sm" onClick={() => act("POST", "/backfill/import", undefined, t("gaming.queued"))}>{t("live.backfill_import")}</button>}
        </div>
      </details>
      {!active && s.segments > 0 && (
        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          {s.project_id ? (
            <a className="btn" href={href.project(s.project_id)}>{t("live.open_project")}</a>
          ) : (
            <button className="btn" onClick={() => act("POST", "/finalize", undefined, t("gaming.queued"))}>{t("live.finalize")}</button>
          )}
          <span className="muted small">{t("live.finalize_hint")}</span>
        </div>
      )}
      {s.errors.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary>{t("live.errors")} ({s.errors.length})</summary>
          <ul className="small mono">{s.errors.map((e, i) => <li key={i}>{e.at}: {e.text}</li>)}</ul>
        </details>
      )}
      {msg && <p className="small" style={{ marginTop: 8 }}>{msg}</p>}
    </div>
  );
}

function Stat({ k, v, warn }: { k: string; v: string; warn?: boolean }) {
  return (
    <div>
      <dt className="muted small">{k}</dt>
      <dd className="mono" style={{ margin: 0, color: warn ? "var(--danger, #e5484d)" : undefined }}>{v}</dd>
    </div>
  );
}

interface Tuning {
  clip_min: number;
  clip_ideal: number;
  clip_max: number;
  pre_roll: [number, number];
  min_gap: number;
}

interface AIProvider {
  id: "ollama" | "anthropic" | "gemini";
  label: string;
  has_key: boolean;
  key: string | null;
  model: string | null;
}
interface AIView {
  provider: AIProvider["id"];
  providers: AIProvider[];
  anthropic_models: string[];
  gemini_models: string[];
  fallback: boolean;
  effort: string;
}

function AICard({ onChange }: { onChange?: () => void }) {
  const { t } = useI18n();
  const [v, setV] = useState<AIView | null>(null);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    req<AIView>("GET", "/api/gaming/ai").then(setV).catch(() => undefined);
  }, []);
  if (!v) return null;
  const put = async (body: Record<string, unknown>) => {
    setMsg(null);
    try {
      setV(await req<AIView>("PUT", "/api/gaming/ai", body));
      onChange?.();
      return true;
    } catch (e) {
      setMsg(String((e as Error).message || e));
      return false;
    }
  };
  const test = async (p: string) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await req<{ ok: boolean; model?: string; seconds?: number; answer?: string; error?: string; hint?: string }>(
        "POST", "/api/gaming/ai/test", { provider: p });
      setMsg(r.ok ? t("gaming.ai_test_ok", { model: r.model ?? "", s: r.seconds ?? 0, answer: r.answer ?? "" })
        : `${r.error ?? ""} ${r.hint ?? ""}`);
    } finally {
      setBusy(false);
    }
  };
  const cloud = v.providers.filter((p) => p.id !== "ollama");
  return (
    <section className="card gx-card">
      <h3>{t("gaming.ai_title")}</h3>
      <p className="muted small">{t("gaming.ai_hint")}</p>
      <div className="segmented" role="radiogroup" aria-label={t("gaming.ai_title")} style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {v.providers.map((p) => (
          <button key={p.id} role="radio" aria-checked={v.provider === p.id} disabled={!p.has_key}
            className={`btn btn-sm ${v.provider === p.id ? "btn-primary" : ""}`}
            onClick={() => put({ provider: p.id })}>
            {p.label}{p.id !== "ollama" && !p.has_key ? ` (${t("gaming.ai_no_key")})` : ""}
          </button>
        ))}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12, marginTop: 12 }}>
        {cloud.map((p) => (
          <div key={p.id} className="card" style={{ padding: 12 }}>
            <strong>{p.label}</strong>
            <p className="muted small">{t(p.id === "anthropic" ? "gaming.ai_anthropic_hint" : "gaming.ai_gemini_hint")}</p>
            {p.key && <div className="small mono">{t("gaming.ai_saved_key")}: {p.key}</div>}
            <label className="field">
              <span className="field-label">{t("gaming.ai_key")}</span>
              <input className="input input-sm" type="password" autoComplete="off" value={keys[p.id] ?? ""}
                placeholder={p.id === "anthropic" ? "sk-ant-..." : "AIza..."}
                onChange={(e) => setKeys({ ...keys, [p.id]: e.target.value })} />
            </label>
            <label className="field">
              <span className="field-label">{t("gaming.ai_model")}</span>
              <select className="input input-sm" value={p.model ?? "auto"}
                onChange={(e) => put({ model: { [p.id]: e.target.value } })}>
                {p.id === "gemini" && <option value="auto">auto</option>}
                {(p.id === "anthropic" ? v.anthropic_models : v.gemini_models).map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
              <button className="btn btn-sm btn-primary" disabled={!keys[p.id]} onClick={async () => {
                if (await put({ key: { [p.id]: keys[p.id] } })) {
                  setKeys({ ...keys, [p.id]: "" });
                  await put({ provider: p.id });
                }
              }}>{t("gaming.ai_save_use")}</button>
              <button className="btn btn-sm" disabled={!p.has_key || busy} onClick={() => test(p.id)}>{t("gaming.ai_test")}</button>
              {p.has_key && <button className="btn btn-sm btn-ghost" onClick={() => put({ key: { [p.id]: "" }, provider: v.provider === p.id ? "ollama" : v.provider })}>{t("gaming.ai_forget")}</button>}
            </div>
          </div>
        ))}
      </div>
      <div className="gx-row" style={{ marginTop: 12, alignItems: "center" }}>
        <label className="check" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={v.fallback} onChange={(e) => put({ fallback: e.target.checked })} />
          <span>{t("gaming.ai_fallback")}</span>
        </label>
        {v.provider === "anthropic" && (
          <label className="field" style={{ maxWidth: 220 }}>
            <span className="field-label">{t("gx.ai_effort")}</span>
            <select className="select select-sm" value={v.effort} onChange={(e) => put({ effort: e.target.value })}>
              <option value="low">{t("gx.effort_low")}</option>
              <option value="medium">{t("gx.effort_medium")}</option>
              <option value="high">{t("gx.effort_high")}</option>
            </select>
          </label>
        )}
      </div>
      <p className="small" style={{ marginTop: 8 }}>
        <span className={`chip ${v.provider === "ollama" ? "chip-outline" : "chip-success"}`}><span className="dot" />
          {t("gx.ai_now", { name: v.providers.find((p) => p.id === v.provider)?.label ?? v.provider })}
        </span>{" "}
        {v.provider !== "ollama" && <span className="muted">{v.fallback ? t("gx.ai_fallback_on") : t("gx.ai_fallback_off")}</span>}
      </p>
      {msg && <div className="notice small" role="status" style={{ marginTop: 8 }}>{msg}</div>}
    </section>
  );
}

function TuningCard() {
  const { t } = useI18n();
  const [tun, setTun] = useState<Tuning | null>(null);
  const [count, setCount] = useState<number | "">("");
  const [target, setTarget] = useState(12);
  const [weak, setWeak] = useState(35);
  const [stories, setStories] = useState(true);
  const [perClip, setPerClip] = useState(true);
  const [perHour, setPerHour] = useState(30);
  const [minScore, setMinScore] = useState(50);
  const [pkg, setPkg] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    req<Record<string, unknown>>("GET", "/api/gaming/settings").then((s) => {
      setTun(s.tuning as Tuning);
      setCount((s.candidate_count as number | null) ?? "");
      setTarget(s.daily_target as number);
      setWeak(s.weak_score as number);
      setStories(s.story_clips !== false);
      setPerClip(s.facecam_per_clip !== false);
      setPerHour((s.story_per_hour as number) ?? 30);
      setMinScore((s.story_min_score as number) ?? 50);
      setPkg(s.story_package === true);
    }).catch(() => undefined);
  }, []);
  if (!tun) return null;
  const save = async () => {
    await req("PUT", "/api/gaming/settings", { tuning: tun, candidate_count: count === "" ? null : count, daily_target: target, weak_score: weak,
      story_clips: stories, facecam_per_clip: perClip, story_per_hour: perHour, story_min_score: minScore, story_package: pkg });
    setSaved(true);
    window.setTimeout(() => setSaved(false), 2000);
  };
  const num = (label: string, value: number, set: (v: number) => void, min: number, max: number) => (
    <label className="field" style={{ minWidth: 120 }}>
      <span className="field-label">{label}</span>
      <input className="input input-sm" type="number" min={min} max={max} value={value} onChange={(e) => set(Number(e.target.value))} />
    </label>
  );
  return (
    <section className="card gx-card">
      <h3>{t("gaming.tuning")}</h3>
      <p className="muted small">{t("gaming.tuning_hint")}</p>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        {num(t("gaming.clip_min"), tun.clip_min, (v) => setTun({ ...tun, clip_min: v }), 5, 120)}
        {num(t("gaming.clip_ideal"), tun.clip_ideal, (v) => setTun({ ...tun, clip_ideal: v }), 5, 120)}
        {num(t("gaming.clip_max"), tun.clip_max, (v) => setTun({ ...tun, clip_max: v }), 5, 180)}
        {num(t("gaming.pre_min"), tun.pre_roll[0], (v) => setTun({ ...tun, pre_roll: [v, tun.pre_roll[1]] }), 0, 30)}
        {num(t("gaming.pre_max"), tun.pre_roll[1], (v) => setTun({ ...tun, pre_roll: [tun.pre_roll[0], v] }), 0, 30)}
        {num(t("gaming.min_gap"), tun.min_gap, (v) => setTun({ ...tun, min_gap: v }), 5, 600)}
        <label className="field" style={{ minWidth: 120 }}>
          <span className="field-label">{t("gx.count_fixed")}</span>
          <input className="input input-sm" type="number" min={1} max={1000} value={count} placeholder={t("gx.auto")}
            onChange={(e) => setCount(e.target.value === "" ? "" : Number(e.target.value))} />
        </label>
        {num(t("gx.per_hour"), perHour, setPerHour, 2, 60)}
        {num(t("gx.min_score"), minScore, setMinScore, 0, 90)}
        {num(t("gaming.daily_target"), target, setTarget, 1, 60)}
        {num(t("gaming.weak_score"), weak, setWeak, 0, 100)}
      </div>
      <label className="check" style={{ marginTop: 8, display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={stories} onChange={(e) => setStories(e.target.checked)} />
        <span>{t("gaming.story_clips")}</span>
      </label>
      <label className="check" style={{ marginTop: 4, display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={perClip} onChange={(e) => setPerClip(e.target.checked)} />
        <span>{t("gaming.facecam_per_clip")}</span>
      </label>
      <label className="check" style={{ marginTop: 4, display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={pkg} onChange={(e) => setPkg(e.target.checked)} />
        <span>{t("gx.story_package")}</span>
      </label>
      <p className="muted small" style={{ marginTop: 8 }}>{t("gx.count_hint")}</p>
      <button className="btn btn-primary" onClick={save} style={{ marginTop: 8 }}>{saved ? t("gaming.saved") : t("gaming.save")}</button>
    </section>
  );
}

function ExportCard() {
  const { t } = useI18n();
  const [res, setRes] = useState<{ folder: string; exported: string[]; missing: unknown[] } | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <section className="card gx-card">
      <h3>{t("gaming.export_title")}</h3>
      <p className="muted small">{t("gaming.export_hint")}</p>
      <button className="btn btn-primary" disabled={busy} onClick={async () => {
        setBusy(true);
        try {
          setRes(await req("POST", "/api/gaming/export-selected", { all_rendered: true }));
        } finally {
          setBusy(false);
        }
      }}>{t("gaming.export_all")}</button>
      {res && (
        <div className="small" style={{ marginTop: 8 }}>
          <div className="mono">{res.folder}</div>
          <div>{t("gaming.exported", { n: res.exported.length })}</div>
        </div>
      )}
    </section>
  );
}


// ===================================================================== auto-post (autopost.py)

type APPlatform = "youtube" | "tiktok" | "reels";
interface APPost { platform: APPlatform; project_id: string; cid: string; when: string; title: string; score: number; mode: string }
interface APView {
  settings: {
    enabled: boolean;
    platforms: Record<APPlatform, { enabled: boolean; per_day: number }>;
    times: string[];
    min_score: number;
    days_ahead: number;
    tiktok_consent: boolean;
    cap: number;
  };
  platforms: Record<APPlatform, {
    label: string; enabled: boolean; per_day: number; connected: boolean; mode: "auto" | "reminder";
    problem: string | null; paused: { reason: string } | null; today: number; next: APPost[];
  }>;
  log: { at: number; platform: string; kind: string; message: string }[];
}

function AutoPostCard() {
  const { t } = useI18n();
  const [v, setV] = useState<APView | null>(null);
  const [times, setTimes] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const load = useCallback(() => {
    req<APView>("GET", "/api/autopost").then((x) => { setV(x); setTimes(x.settings.times.join(", ")); }).catch(() => undefined);
  }, []);
  useEffect(load, [load]);
  if (!v) return null;
  const put = async (body: Record<string, unknown>) => {
    setMsg(null);
    try {
      const x = await req<APView>("PUT", "/api/autopost", body);
      setV(x);
      setTimes(x.settings.times.join(", "));
    } catch (e) {
      setMsg(String((e as Error).message || e));
    }
  };
  const plat = (p: APPlatform, body: { enabled?: boolean; per_day?: number }) =>
    put({ platforms: { [p]: { ...v.settings.platforms[p], ...body } } });
  const order: APPlatform[] = ["youtube", "tiktok", "reels"];
  return (
    <section className="card gx-card">
      <h3>{t("autopost.title")}</h3>
      <p className="muted small">{t("autopost.hint", { cap: v.settings.cap })}</p>
      <label className="check" style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input type="checkbox" checked={v.settings.enabled} onChange={(e) => put({ enabled: e.target.checked })} />
        <span>{t("autopost.enable")}</span>
      </label>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 12, marginTop: 12 }}>
        {order.map((p) => {
          const x = v.platforms[p];
          return (
            <div key={p} className="card" style={{ padding: 12 }}>
              <strong>{x.label}</strong>
              <div className="small" style={{ marginTop: 4 }}>
                {x.connected ? t("autopost.connected") : t("autopost.not_connected")}
                {" · "}
                {x.mode === "auto" ? t("autopost.mode_auto") : t("autopost.mode_reminder")}
                {!x.connected && <> · <a href={href.settings}>{t("autopost.connect")}</a></>}
              </div>
              {x.paused && (
                <div className="notice notice-danger small" role="status" style={{ marginTop: 6 }}>
                  {t("autopost.paused")}: {x.paused.reason}{" "}
                  <button className="btn btn-sm" onClick={async () => setV(await req<APView>("POST", `/api/autopost/resume/${p}`))}>
                    {t("autopost.resume")}
                  </button>
                </div>
              )}
              <label className="check" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
                <input type="checkbox" checked={x.enabled} onChange={(e) => plat(p, { enabled: e.target.checked })} />
                <span>{t("autopost.platform_on")}</span>
              </label>
              <label className="field" style={{ maxWidth: 160 }}>
                <span className="field-label">{t("autopost.per_day")}</span>
                <input className="input input-sm" type="number" min={1} max={v.settings.cap} value={x.per_day}
                  onChange={(e) => plat(p, { per_day: Number(e.target.value) })} />
              </label>
              <div className="small muted" style={{ marginTop: 6 }}>{t("autopost.next")}</div>
              {x.next.length === 0 ? <div className="small muted">{t("autopost.none")}</div> : (
                <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 16 }}>
                  {x.next.map((n) => (
                    <li key={`${n.project_id}:${n.cid}:${n.when}`}>
                      {new Date(n.when).toLocaleString()} — {n.title || n.cid} ({n.score})
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 12, alignItems: "flex-end" }}>
        <label className="field" style={{ minWidth: 220 }}>
          <span className="field-label">{t("autopost.times")}</span>
          <input className="input input-sm" value={times} onChange={(e) => setTimes(e.target.value)}
            onBlur={() => put({ times: times.split(/[ ,;]+/).filter(Boolean) })} />
        </label>
        <label className="field" style={{ maxWidth: 160 }}>
          <span className="field-label">{t("autopost.min_score")}</span>
          <input className="input input-sm" type="number" min={0} max={100} value={v.settings.min_score}
            onChange={(e) => put({ min_score: Number(e.target.value) })} />
        </label>
        <button className="btn btn-sm" onClick={async () => setV(await req<APView>("POST", "/api/autopost/run"))}>
          {t("autopost.plan_now")}
        </button>
      </div>
      <label className="check" style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
        <input type="checkbox" checked={v.settings.tiktok_consent} onChange={(e) => put({ tiktok_consent: e.target.checked })} />
        <span>{t("autopost.tiktok_consent")}</span>
      </label>
      {msg && <div className="notice small" role="status" style={{ marginTop: 8 }}>{msg}</div>}
      {v.log.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="small">{t("autopost.log")}</summary>
          <ul className="small mono" style={{ paddingLeft: 16 }}>
            {v.log.map((l, i) => <li key={i}>{new Date(l.at * 1000).toLocaleString()} · {l.platform} · {l.kind} · {l.message}</li>)}
          </ul>
        </details>
      )}
    </section>
  );
}
