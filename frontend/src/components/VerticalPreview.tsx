import { useEffect, useMemo, useRef, type RefObject } from "react";
import { urls, type BrandPreset, type Candidate, type ClipOptions, type Facecam, type Word } from "../api";
import { captionAt, groupWords, joinerFor, upper } from "../captions";
import { camCoverRect, coverRect, OUT_W, splitFromOptions, splitRects, zoomRect, zoomsAt, type PreviewFx, type Rect } from "../previewMath";

// Canvas size (9:16). The picture is cut and zoomed with previewMath.ts = what the renderer
// does (backend/sharkyclips/clips.py at 1080x1920); captions / hook / brand are drawn on top.
const W = 360;
const H = 640;
const S = W / OUT_W; // scale from the 1080-wide render to this canvas
const GOLD = "#D9B56A";

interface Props {
  video: RefObject<HTMLVideoElement>;
  clip: Candidate | null;
  /** the clip's (edited) start / end */
  range?: { start: number; end: number } | null;
  options: ClipOptions;
  facecam: Facecam | null;
  words: Word[];
  lang: string | null;
  hookText: string;
  /** smart zoom + meme shake / punch from the render plan (null: none) */
  fx?: PreviewFx | null;
  brand?: BrandPreset | null;
}

export function resolveLayout(layout: ClipOptions["layout"], facecam: Facecam | null) {
  if (layout === "auto") return facecam ? "split" : "fit";
  if (layout === "split" && !facecam) return "fit";
  return layout;
}

function blit(ctx: CanvasRenderingContext2D, v: CanvasImageSource, r: Rect, dx: number, dy: number, dw: number, dh: number) {
  if (r[2] > 0 && r[3] > 0) ctx.drawImage(v, r[0], r[1], r[2], r[3], dx, dy, dw, dh);
}

// Instagram-style headline: the same numbers as backend/sharkyclips/edit/headline.py (change both).
const HL = { font: 54, lineH: 1.22, padX: 0.45, padY: 0.3, radius: 0.3, maxW: 0.88, maxLines: 2, minShrink: 0.6 };
const HL_FONT = `Arial, "Liberation Sans", Helvetica, "Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", sans-serif`;

/** Lines, font size and box size of the headline on this canvas (null: no text). */
function headlineBox(ctx: CanvasRenderingContext2D, text: string, canvasW: number, scale: number) {
  const words = text.split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const base = Math.max(8, Math.round(HL.font * scale));
  const maxBox = canvasW * HL.maxW;
  const measure = (s: string, size: number) => {
    ctx.font = `700 ${size}px ${HL_FONT}`;
    return ctx.measureText(s).width;
  };
  const wrap = (size: number, maxText: number): string[] | null => {
    const lines: string[] = [];
    let cur = "";
    for (const w of words) {
      const test = cur ? `${cur} ${w}` : w;
      if (cur && measure(test, size) > maxText) {
        lines.push(cur);
        cur = w;
      } else cur = test;
    }
    if (cur) lines.push(cur);
    return lines.length > HL.maxLines || lines.some((l) => measure(l, size) > maxText) ? null : lines;
  };
  let size = base;
  let lines: string[] | null = null;
  while (size >= base * HL.minShrink) {
    lines = wrap(size, maxBox - 2 * Math.round(size * HL.padX));
    if (lines) break;
    size = Math.floor(size * 0.94);
  }
  if (!lines) {
    size = Math.max(8, Math.round(base * HL.minShrink));
    lines = (wrap(size, 1e9) ?? [words.join(" ")]).slice(0, HL.maxLines);
  }
  const padX = Math.round(size * HL.padX);
  const padY = Math.round(size * HL.padY);
  const lineH = Math.round(size * HL.lineH);
  const width = Math.min(Math.round(Math.max(...lines.map((l) => measure(l, size)))) + 2 * padX, Math.round(maxBox));
  const height = lineH * lines.length + 2 * padY;
  return { lines, size, padY, lineH, width, height, radius: Math.round(HL.radius * lineH) };
}

export default function VerticalPreview({ video, clip, range, options, facecam, words, lang, hookText, fx, brand }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const tiny = useMemo(() => {
    const c = document.createElement("canvas");
    c.width = 27;
    c.height = 48;
    return c;
  }, []);
  const groups = useMemo(
    () => (options.subtitles === "none" ? [] : groupWords(words, options.subtitles, lang)),
    [words, options.subtitles, lang],
  );
  const logo = useMemo(() => {
    if (!brand?.logo) return null;
    const img = new Image();
    img.src = urls.brandLogo(brand.id, brand.rev);
    return img;
  }, [brand?.id, brand?.logo, brand?.rev]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const cv = canvas.current;
    const v = video.current;
    if (!cv || !v) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = W * dpr;
    cv.height = H * dpr;
    const ctx = cv.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const layout = resolveLayout(options.layout, facecam);
    const sp = splitFromOptions(options);
    const camPx = sp.camH * S;
    const gamePx = H - camPx;
    const camY = sp.order === "top" ? 0 : gamePx;
    const gameY = sp.order === "top" ? camPx : 0;
    const seam = sp.order === "top" ? camPx : gamePx;
    // headline: it replaces the hook text; on the split line the captions move into the gameplay panel
    const banner = (options.banner ?? "").trim();
    const igBanner = !!banner && (options.banner_style ?? "ig") === "ig";
    const bannerCapY = sp.order === "top" ? seam + 260 * S : seam - 260 * S;
    const start = range?.start ?? clip?.start ?? 0;
    const end = range?.end ?? clip?.end ?? 0;
    const prim = brand?.caption_color || "#fff";
    const high = brand?.caption_highlight || GOLD;
    const outline = brand?.caption_outline || "#000";
    const fontFam = `${brand?.caption_font ? `"${brand.caption_font}", ` : ""}Geist, Arial, "Noto Sans Arabic", "Microsoft YaHei", sans-serif`;
    let raf = 0;

    const draw = () => {
      ctx.fillStyle = "#05080f";
      ctx.fillRect(0, 0, W, H);
      const vw = v.videoWidth;
      const vh = v.videoHeight;
      const t = v.currentTime;
      const inClip = !!clip && t >= start - 0.05 && t <= end + 0.05;
      // zoom factors, all from scratch for this frame (nothing carries over between frames or clips)
      const z = zoomsAt(t, inClip ? fx ?? null : null);
      if (v.readyState >= 2 && vw && vh) {
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.save();
        if (z.shake.z > 1.0001) {
          // meme shake / punch: the renderer zooms the whole picture (not the captions)
          const k = z.shake.z;
          const left = (0.5 - 0.5 / k + z.shake.dx) * W;
          const top = (0.5 - 0.5 / k + z.shake.dy) * H;
          ctx.setTransform(dpr * k, 0, 0, dpr * k, -left * dpr * k, -top * dpr * k);
        }
        const whole: Rect = [0, 0, vw, vh];
        if (layout === "crop") {
          blit(ctx, v, zoomRect(coverRect(whole, W, H), z.full), 0, 0, W, H);
        } else if (layout === "fit") {
          if (options.fit_bg === "black") {
            ctx.fillStyle = "#000";
            ctx.fillRect(0, 0, W, H);
          } else {
            const tc = tiny.getContext("2d")!;
            blit(tc, v, coverRect(whole, tiny.width, tiny.height), 0, 0, tiny.width, tiny.height);
            ctx.drawImage(tiny, 0, 0, W, H);
            ctx.fillStyle = "rgba(0,0,0,0.25)";
            ctx.fillRect(0, 0, W, H);
          }
          const scale = Math.min(W / vw, H / vh);
          const dw = vw * scale;
          const dh = vh * scale;
          blit(ctx, v, zoomRect(whole, z.full), (W - dw) / 2, (H - dh) / 2, dw, dh);
        } else if (facecam) {
          // the renderer's exact crops (clips.split_rects): the cam rect is scaled to the panel,
          // the gameplay rect center-cropped to it, then each zoomed around its focus point
          const { cam, game } = splitRects(vw, vh, facecam, sp);
          blit(ctx, v, zoomRect(cam, z.cam, 0.45), 0, camY, W, camPx);
          ctx.save();
          ctx.beginPath();
          ctx.rect(0, gameY, W, gamePx);
          ctx.clip();
          let gd: [number, number, number, number]; // where the gameplay rect lands on the canvas
          if (sp.gameFit === "crop") {
            const r = coverRect(game, W, gamePx);
            blit(ctx, v, zoomRect(r, z.scene), 0, gameY, W, gamePx);
            const k = W / r[2];
            gd = [-(r[0] - game[0]) * k, gameY - (r[1] - game[1]) * k, game[2] * k, game[3] * k];
          } else {
            if (sp.gameFit === "blur") {
              const tc = tiny.getContext("2d")!;
              blit(tc, v, coverRect(game, tiny.width, tiny.height), 0, 0, tiny.width, tiny.height);
              ctx.drawImage(tiny, 0, gameY, W, gamePx);
              ctx.fillStyle = "rgba(0,0,0,0.25)";
              ctx.fillRect(0, gameY, W, gamePx);
            } else {
              ctx.fillStyle = "#000";
              ctx.fillRect(0, gameY, W, gamePx);
            }
            const k = Math.min(W / game[2], gamePx / game[3]);
            gd = [(W - game[2] * k) / 2, gameY + (gamePx - game[3] * k) / 2, game[2] * k, game[3] * k];
            blit(ctx, v, zoomRect(game, z.scene), gd[0], gd[1], gd[2], gd[3]);
          }
          const cover = sp.hideCam ? camCoverRect(vw, vh, facecam, game) : null;
          if (cover) {
            // the renderer blurs the original overlay here: the preview shows the same spot darkened
            const k = gd[2] / game[2];
            ctx.fillStyle = "rgba(20,24,34,0.85)";
            ctx.fillRect(gd[0] + cover[0] * k, gd[1] + cover[1] * k, cover[2] * k, cover[3] * k);
          }
          ctx.restore();
        }
        ctx.restore();
      }

      if (inClip && clip) {
        // captions
        const cap = options.subtitles === "none" ? null : captionAt(groups, t, end);
        if (cap) {
          const capY = layout === "split" ? (banner ? bannerCapY : seam) : 1390 * S;
          const bold = options.subtitles === "bold";
          const size = (bold ? 86 : options.subtitles === "clean" ? 62 : 56) * S;
          ctx.font = `${options.subtitles === "minimal" ? 500 : 800} ${size}px ${fontFam}`;
          ctx.textBaseline = "middle";
          ctx.direction = lang === "ar" ? "rtl" : "ltr";
          ctx.textAlign = "center";
          const texts = cap.words.map((w) => (bold ? upper(w, lang) : w));
          const joiner = joinerFor(lang);
          const full = texts.join(joiner);
          const fw = ctx.measureText(full).width;
          if (options.subtitles === "clean") {
            ctx.fillStyle = "rgba(0,0,0,0.69)";
            const padX = 14 * S * 1.4;
            const padY = 14 * S;
            ctx.beginPath();
            ctx.roundRect(W / 2 - fw / 2 - padX, capY - size / 2 - padY, fw + padX * 2, size + padY * 2, 6);
            ctx.fill();
            ctx.fillStyle = prim;
            ctx.fillText(full, W / 2, capY);
          } else if (bold && lang !== "ar") {
            // word by word so the spoken word can be highlighted
            ctx.textAlign = "left";
            let x = W / 2 - fw / 2;
            ctx.lineJoin = "round";
            ctx.lineWidth = 7 * S * 2;
            texts.forEach((w, i) => {
              const piece = i < texts.length - 1 ? w + joiner : w;
              ctx.strokeStyle = outline;
              ctx.strokeText(w, x, capY);
              ctx.fillStyle = i === cap.active ? high : prim;
              ctx.fillText(w, x, capY);
              x += ctx.measureText(piece).width;
            });
          } else {
            ctx.lineJoin = "round";
            ctx.lineWidth = (bold ? 7 : 2) * S * 2;
            ctx.strokeStyle = bold ? outline : "rgba(0,0,0,0.6)";
            ctx.strokeText(full, W / 2, capY);
            ctx.fillStyle = prim;
            ctx.fillText(full, W / 2, capY);
          }
        }
        // hook
        // (a seamless loop shows it for the last moment too: it is already there when the clip starts over)
        if (options.hook && hookText && !banner && (t - start < 3.2 || (options.loop && t <= end && end - t < 0.8))) {
          const hookY = (layout === "fit" ? 330 : 120) * S;
          const size = 64 * S;
          ctx.font = `800 ${size}px ${fontFam}`;
          ctx.direction = lang === "ar" ? "rtl" : "ltr";
          ctx.textAlign = "center";
          ctx.textBaseline = "top";
          // wrap to max width
          const maxW = W - 2 * 90 * S - 2 * 22 * S;
          const lines: string[] = [];
          let line = "";
          for (const word of hookText.split(/\s+/)) {
            const test = line ? `${line} ${word}` : word;
            if (ctx.measureText(test).width > maxW && line) {
              lines.push(line);
              line = word;
            } else line = test;
          }
          if (line) lines.push(line);
          const lh = size * 1.18;
          const pad = 22 * S;
          ctx.fillStyle = "#fff";
          lines.forEach((l, i) => {
            const lw = ctx.measureText(l).width;
            ctx.fillRect(W / 2 - lw / 2 - pad, hookY + i * lh - pad / 2, lw + pad * 2, lh + pad);
          });
          ctx.fillStyle = "#0E1A33";
          lines.forEach((l, i) => ctx.fillText(l, W / 2, hookY + i * lh + (lh - size) / 2));
        }
        // Instagram-style headline for the whole clip (edit/headline.py): over the captions,
        // its centre on the split line, else its top at the hook position
        if (igBanner) {
          const hb = headlineBox(ctx, banner, W, S);
          if (hb) {
            // "fit" with a wide picture: right above it (clips.fit_video_top + 28 px), like the renderer
            const vt = layout === "fit" && vw / vh > 1080 / 1920 + 0.02 ? Math.floor((1920 - Math.round((1080 * vh) / vw)) / 2) : null;
            const top = layout === "split" ? seam - hb.height / 2
              : vt !== null ? Math.max(40 * S, vt * S - hb.height - 28 * S)
              : (layout === "fit" ? 330 : 120) * S;
            const left = (W - hb.width) / 2;
            ctx.fillStyle = "#fff";
            ctx.beginPath();
            ctx.roundRect(left, top, hb.width, hb.height, hb.radius);
            ctx.fill();
            ctx.font = `700 ${hb.size}px ${HL_FONT}`;
            ctx.direction = "ltr";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillStyle = "#000";
            hb.lines.forEach((l, i) => ctx.fillText(l, W / 2, top + hb.padY + i * hb.lineH + hb.lineH / 2));
          }
        }
      }

      // brand kit: logo watermark + @handle (same placement as the renderer)
      if (brand) {
        const m = 40 * S;
        const right = brand.logo_pos === "tr" || brand.logo_pos === "br";
        const top = brand.logo_pos === "tl" || brand.logo_pos === "tr";
        let lh = 0;
        if (logo && logo.complete && logo.naturalWidth) {
          const lw = (W * brand.logo_size) / 100;
          lh = (lw * logo.naturalHeight) / logo.naturalWidth;
          ctx.globalAlpha = brand.logo_opacity;
          ctx.drawImage(logo, right ? W - lw - m : m, top ? m : H - lh - m, lw, lh);
          ctx.globalAlpha = 1;
        }
        if (brand.handle && brand.show_handle) {
          ctx.font = `800 ${38 * S}px ${fontFam}`;
          ctx.textAlign = right ? "right" : "left";
          ctx.textBaseline = top ? "top" : "bottom";
          ctx.direction = "ltr";
          const y = top ? m + lh + (lh ? 10 * S : 0) : H - m - lh - (lh ? 10 * S : 0);
          ctx.lineWidth = 3 * S * 2;
          ctx.strokeStyle = "rgba(0,0,0,0.6)";
          ctx.strokeText(`@${brand.handle}`, right ? W - m : m, y);
          ctx.fillStyle = "#fff";
          ctx.fillText(`@${brand.handle}`, right ? W - m : m, y);
        }
      }
    };

    const loop = () => {
      draw();
      if (!v.paused && !v.ended) raf = requestAnimationFrame(loop);
    };
    const kick = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(loop);
    };
    const events = ["play", "seeked", "loadeddata", "timeupdate", "pause"];
    events.forEach((e) => v.addEventListener(e, kick));
    logo?.addEventListener("load", kick);
    kick();
    return () => {
      cancelAnimationFrame(raf);
      events.forEach((e) => v.removeEventListener(e, kick));
      logo?.removeEventListener("load", kick);
    };
  }, [video, clip, range?.start, range?.end, options, facecam, groups, lang, hookText, tiny, fx, brand, logo]); // eslint-disable-line react-hooks/exhaustive-deps

  return <canvas ref={canvas} width={W} height={H} aria-hidden="true" />;
}
