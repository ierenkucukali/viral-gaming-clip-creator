"""Two clip styles (local fork), chosen per project when a video is imported (Gaming page):

  streamer  the streamer's camera cut out on top (1/3) and the gameplay below (2/3), both from the
            same frame (perfect lip sync); the camera box is found by face detection and checked
            again inside every clip (facecam.for_window). Instagram headline on the panel line.
  direct    NO picture analysis at all: no facecam detection, no cropping. The whole 16:9 video sits
            in the middle of the 9:16 frame (black or blurred background), the AI's headline right
            above it, the captions below. Only the transcript AI (storyfind.py) picks the moments.

meta.json "clip_style" holds the project's style; config "clip_style" is the default for new imports.
"""

from __future__ import annotations

from . import config

STYLES = ("streamer", "direct")
CHOICES = STYLES + ("both",)  # "both": analysed once as streamer style, a direct-style twin follows (twins.py)
FIT_BGS = ("black", "blur", "white")


def default() -> str:
    s = str(config.get("clip_style") or "streamer")
    return s if s in CHOICES else "streamer"


def of(meta: dict | None) -> str:
    s = str((meta or {}).get("clip_style") or "streamer")
    return s if s in STYLES else "streamer"


def is_direct(project) -> bool:
    try:
        return of(project.load_meta()) == "direct"
    except Exception:  # noqa: BLE001
        return False


def review_defaults(style: str) -> dict:
    """Render options a new project of this style starts with (the Project page shows them)."""
    if style == "direct":
        bg = str(config.get("direct_fit_bg") or "black")
        return {"layout": "fit", "fit_bg": bg if bg in FIT_BGS else "black", "banner_style": "ig", "hook": True}
    return {"layout": "auto", "banner_style": "ig", "hook": True}


def set_style(project, style: str) -> None:
    """Store the style in meta.json and give the project's review options the style's defaults."""
    from .web import state

    both = style == "both"
    style = style if style in STYLES else "streamer"
    meta = project.load_meta()
    meta["clip_style"] = style
    if both:
        meta["both_styles"] = True
    project.save_meta(meta)
    changes: dict = {"options": review_defaults(style)}
    if style == "direct":
        changes["facecam"] = None  # "checked, none": nothing to detect, the auto facecam job is skipped
    state.save_review(project, changes)


def apply(opts, project=None):
    """Render options for a direct-style project: the whole picture, centered, no tracking. The
    background is the project's choice (review options, black by default), whatever the caller sent."""
    from .clips import ClipOptions

    d = opts.to_dict()
    d.update(layout="fit", track="off")
    bg = None
    if project is not None:
        try:
            from .web import state

            bg = (state.load_review(project).get("options") or {}).get("fit_bg")
        except Exception:  # noqa: BLE001
            bg = None
    d["fit_bg"] = bg if bg in FIT_BGS else (str(config.get("direct_fit_bg") or "black"))
    if d["fit_bg"] not in FIT_BGS:
        d["fit_bg"] = "black"
    return ClipOptions.from_dict(d)
