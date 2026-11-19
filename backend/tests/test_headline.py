"""Instagram-style headline (local gaming fork): white rounded box, black text, colour emoji,
centred on the line between the facecam and the gameplay panel for the whole clip."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from sharkyclips import clips
from sharkyclips.clips import ClipOptions, Facecam, SplitSpec
from sharkyclips.edit import headline

TEXT = "Cops lost me AGAIN 😂"


def test_banner_style_option_is_validated():
    assert ClipOptions().banner_style == "ig"
    assert ClipOptions.from_dict({"banner": "x", "banner_style": "brand"}).banner_style == "brand"
    assert ClipOptions.from_dict({"banner_style": "neon"}).banner_style == "ig"  # unknown -> ig
    assert ClipOptions.from_dict({"banner": "  a   b  "}).banner == "a b"


def test_headline_png_is_a_white_rounded_box_with_black_text_and_colour_emoji():
    img = headline.draw(TEXT, 1080, 1.0)
    assert img is not None and img.mode == "RGBA"
    lay = headline.layout(TEXT, 1080, 1.0)
    assert (img.width, img.height) == (lay.width, lay.height) and len(lay.lines) == 1
    a = np.asarray(img).astype(int)
    assert a[0, 0, 3] == 0 and a[-1, -1, 3] == 0  # rounded corners: transparent
    assert (a[lay.height // 2, 4, :3] > 245).all() and a[lay.height // 2, 4, 3] == 255  # white padding
    opaque = a[..., 3] == 255
    dark = opaque & (a[..., :3].max(axis=2) < 60)
    assert dark.sum() > 2000  # black text
    if headline.emoji_file():
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        colour = opaque & (r > 200) & (g > 120) & (b < 110)  # the yellow laughing face
        assert colour.sum() > 300
    assert lay.width <= 1080 * headline.MAX_W


def test_long_headline_wraps_to_two_lines_and_fits():
    lay = headline.layout("This is a much longer headline that will certainly need two lines 🔥", 1080, 1.0)
    assert len(lay.lines) == 2 and lay.width <= 1080 * headline.MAX_W + 1
    assert headline.layout("   ", 1080) is None


def test_ig_style_has_no_ass_banner_or_hook_line():
    ig = clips.build_ass([], 5.0, ClipOptions.from_dict({"banner": TEXT}), "split", "en", "hook text")
    brand = clips.build_ass([], 5.0, ClipOptions.from_dict({"banner": TEXT, "banner_style": "brand"}), "split",
                            "en", "hook text")
    assert ",banner," not in ig and ",hook," not in ig
    assert ",banner," in brand


@pytest.mark.parametrize("order", ["top", "bottom"])
def test_captions_move_into_the_gameplay_panel(order):
    opts = ClipOptions.from_dict({"banner": TEXT, "split_order": order, "split_ratio": 1 / 3})
    token = clips._SPLIT.set(SplitSpec.from_options(opts))
    try:
        geo = clips.Geometry()
        y = clips.banner_caption_y(geo)
        sp = clips.split_spec()
        assert sp.game_y + 150 < y < sp.game_y + sp.game_h - 150
        assert abs(y - sp.seam) >= 200  # clear of the headline box
    finally:
        clips._SPLIT.reset(token)


def _mss():
    spec = importlib.util.spec_from_file_location("mss", Path(__file__).resolve().parents[2] / "scripts" /
                                                  "make_synthetic_stream.py")
    mss = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mss)  # type: ignore[union-attr]
    return mss


@pytest.mark.requires_ffmpeg
@pytest.mark.parametrize("order,path", [("top", "planned"), ("bottom", "planned"), ("top", "plain"),
                                        ("bottom", "plain_flash")])
def test_split_render_has_the_headline_centred_on_the_split_line(tmp_path, monkeypatch, order, path):
    """Both render paths: with an EditPlan (_render_planned) and the plain one (_render_core_inner,
    with and without the hook flash)."""
    from sharkyclips.edit import manual
    from sharkyclips.highlights import run_highlights
    from sharkyclips.ingest import ingest
    from sharkyclips.web import state

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg missing")
    mss = _mss()
    src, wav = tmp_path / "fixture.mp4", tmp_path / "fixture.wav"
    mss.make_audio(wav, 8.0, [4.0])
    info = mss.make_video(src, wav, 8.0, 640, 360, "br", [4.0], fps=30)
    project = ingest(str(src), mode="gaming")
    try:
        run_highlights(project, fetch_chat=False)
        cand = manual.add(project, 1.0, 4.0)
        opts = ClipOptions.from_dict({"layout": "split", "subtitles": "none", "hook": False, "split_order": order,
                                      "split_ratio": 1 / 3, "banner": TEXT, "hook_flash": path == "plain_flash"})
        if path == "planned":  # the fixture has nothing to edit: force the EditPlan path anyway
            from sharkyclips.edit import plan as plan_mod

            monkeypatch.setattr(plan_mod.EditPlan, "active", property(lambda self: True))
        else:
            monkeypatch.setattr(clips, "edit_plan", lambda *a, **k: None)
        out = tmp_path / f"hl_{order}.mp4"
        res = clips.render_clip(project, state.effective_cand(cand, state.load_review(project)), opts,
                                Facecam.from_dict(info["facecam"]), out)
        assert ("plan" in (res.info or {})) == (path == "planned")
        raw = tmp_path / "f.rgb"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(out), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
        px = np.frombuffer(raw.read_bytes(), np.uint8).reshape(1920, 1080, 3).astype(int)
        seam = SplitSpec.from_options(opts).seam
        lay = headline.layout(TEXT, 1080, 1.0)
        top = seam - lay.height // 2
        row = px[top + lay.pad_y // 2 + 2]  # inside the box's top padding: white, no text
        white = np.where(row.min(axis=1) > 225)[0]
        assert white.size > lay.width * 0.85, white.size
        assert abs((white.min() + white.max()) / 2 - 540) <= 6  # centred
        assert abs((white.max() - white.min() + 1) - lay.width) <= 30
        mid = px[seam, (1080 - lay.width) // 2 + 4:(1080 + lay.width) // 2 - 4]
        assert (mid.max(axis=1) < 80).sum() > 20  # black text on the split line
        assert (mid.min(axis=1) > 225).sum() > 100  # on white
        above, below = px[top - 12, 540], px[top + lay.height + 12, 540]
        assert above.min() < 200 and below.min() < 200  # the box ends: picture above and below it
    finally:
        shutil.rmtree(project.dir, ignore_errors=True)  # keep the shared test workspace empty


@pytest.mark.requires_ffmpeg
def test_direct_style_centres_the_whole_video_with_the_headline_above_it(tmp_path, monkeypatch):
    """Direct clip style (clipstyle.py): no facecam, the whole 16:9 picture in the middle on black,
    the headline box right above it, whatever layout the options ask for."""
    from sharkyclips import clipstyle
    from sharkyclips.edit import manual
    from sharkyclips.highlights import run_highlights
    from sharkyclips.ingest import ingest
    from sharkyclips.web import state

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg missing")
    mss = _mss()
    src, wav = tmp_path / "fixture.mp4", tmp_path / "fixture.wav"
    mss.make_audio(wav, 6.0, [3.0])
    info = mss.make_video(src, wav, 6.0, 640, 360, "br", [3.0], fps=30)
    project = ingest(str(src), mode="gaming")
    try:
        clipstyle.set_style(project, "direct")
        review = state.load_review(project)
        assert review["options"]["layout"] == "fit" and review["options"]["fit_bg"] == "black"
        assert review["facecam_checked"] and review["facecam"] is None  # no picture analysis at all
        run_highlights(project, fetch_chat=False, stories_wanted=False)
        cand = manual.add(project, 1.0, 3.5)
        monkeypatch.setattr(clips, "edit_plan", lambda *a, **k: None)
        # even "split" + a facecam from the caller: direct style renders the whole picture
        opts = ClipOptions.from_dict({"layout": "split", "subtitles": "none", "hook": True, "banner": TEXT})
        out = tmp_path / "direct.mp4"
        clips.render_clip(project, state.effective_cand(cand, state.load_review(project)), opts,
                          Facecam.from_dict(info["facecam"]), out)
        raw = tmp_path / "f.rgb"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.0", "-i", str(out), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], check=True)
        px = np.frombuffer(raw.read_bytes(), np.uint8).reshape(1920, 1080, 3).astype(int)
        top = clips.fit_video_top("fit", 640, 360, (1080, 1920))
        assert top == 656  # 1080x608 picture in the middle
        assert px[100:500, 20].max() < 30 and px[1400:1880, 20].max() < 30  # black bars
        assert px[top + 20:top + 588, :].std() > 10  # the picture itself
        lay = headline.layout(TEXT, 1080, 1.0)
        box_top = top - lay.height - 28
        row = px[box_top + lay.pad_y // 2 + 2]
        white = np.where(row.min(axis=1) > 225)[0]
        assert white.size > lay.width * 0.85 and abs((white.min() + white.max()) / 2 - 540) <= 6
        assert px[box_top - 10, 540].max() < 30  # black above the box, the box ends before the picture
    finally:
        shutil.rmtree(project.dir, ignore_errors=True)
