# Connecting YouTube, TikTok and Instagram + auto-post

## Recommended: Buffer (1.2.53)

Buffer posts to Instagram Reels, TikTok and YouTube Shorts for you, also while this PC is off. The free
Buffer plan is enough: 3 channels, 10 scheduled posts per channel at a time, API access (600 requests a
day; the app uses about 2-3 per post).

1. **Buffer:** sign up at <https://buffer.com>, connect Instagram (Business or Creator account), TikTok
   and YouTube. Then <https://publish.buffer.com/settings/api> > create an API key.
2. **Video hosting: Cloudflare R2 (recommended, 1.2.57):** 10 GB free and no download fees.
   dash.cloudflare.com > R2 Object Storage > enable (a card may be asked; nothing is charged under
   10 GB) > Create bucket `sharkyclips` > Settings > Public Development URL > Enable (copy the
   `https://pub-….r2.dev` address) > back on R2: copy the Account ID, then Manage API tokens >
   Create Account API token (Object Read & Write, bucket `sharkyclips`) > copy the Access Key ID and
   Secret Access Key. Paste the four values in the app (Setup > Buffer > Video hosting).
   **Alternative: Cloudinary (free, no card):** Buffer's API can't take a file, it fetches each
   video from a public link. Sign up at <https://cloudinary.com>, copy the **Cloud name** (Dashboard)
   and an **API key + API secret** (Settings > API Keys). Each clip is uploaded right before it goes
   into Buffer and deleted after every platform posted it (max 100 MB per clip on the free plan; a
   bigger clip is re-encoded smaller first).
3. **In the app:** Gaming page > **Setup** > Buffer: paste the keys, press **Save and test**. The
   channels are linked by themselves. Set the posts a day per platform (default 10) and the time
   window (default 10:00-23:00, spread evenly) or fixed times.
4. **Use it (when you like, this PC needn't stay on):** Gaming page > **Pick & send** shows the
   room in Buffer right now (free plan: 10 scheduled posts per channel). Tick that many clips, edit
   title / caption if you like, **Send to Buffer**. They go into Buffer at once, each with its own
   time from the posting plan (Schedule tab: posts a day, spread / every N minutes / fixed times).
   Buffer posts them while the PC is off. When they are posted, open the app and send the next ones.
   Nothing runs in the background; opening the Schedule tab checks what was posted and deletes the
   hosted copies.

What Buffer's API lets us set (developers.buffer.com, checked 2026-11-19): post time (`customScheduled`
+ `dueAt`), caption, the video link + cover frame (`thumbnailOffset`, Instagram / TikTok), YouTube title,
category, privacy, made for kids, notify subscribers; Instagram post type (reel), share to feed, first
comment; the AI-generated label. Buffer's own posting-slot schedule can't be changed through the API,
so the app computes the times itself.

## Without Buffer (advanced)

Everything here is free. No passwords are ever stored by the app: YouTube and TikTok (API) use the
platforms' own sign-in pages and keep only an encrypted token on this PC; TikTok Studio (browser
mode) keeps only its own browser profile, in which you log in yourself.

## What is automatic

| Platform | How the app posts | Automatic? | Limits |
|---|---|---|---|
| YouTube Shorts | Official YouTube Data API, scheduled with `publishAt` (YouTube makes it public at the time; the PC needn't be on then) | Yes | Default Google quota 10,000 units/day = about 6 uploads. A **new, unaudited Google project keeps API uploads private** until Google audits it (the app detects this and tells you). |
| TikTok | Either TikTok Studio in the app's own browser (you log in once by hand) **or** the Content Posting API with your own TikTok developer app | Yes (PC on at the time; TikTok Studio's own planner is used for times 20 min - 10 days ahead) | Without TikTok's app audit, API posts are **"Only me"** (private). CAPTCHA / verification / login wall -> the app stops and that post falls back to "by hand"; it never solves or bypasses them. |
| Instagram Reels | **Manual mode** (upstream decision after Meta restricted an account for automation): at the planned time you get a reminder with the 9:16 file and the caption ready to copy | No: you press Post in the Instagram app | - |

Hard safety cap: at most **3 automatic posts a day per platform** (`platforms.DAILY_POST_MAX`).

## YouTube

1. Go to <https://console.cloud.google.com/> and create a project (free).
2. APIs & Services > Library > enable **YouTube Data API v3**.
3. OAuth consent screen: type *External*, add your Google account as a **Test user**.
4. Credentials > Create credentials > **OAuth client ID** > application type **Desktop app** > download the JSON.
5. In SharkyClips: **Settings > YouTube** > load that JSON (own client) > **Connect with Google** > pick your channel.
6. Optional, for public uploads without the private lock: request the YouTube API audit (Google's "API Services audit" form). Until then, uploads may stay private.

## TikTok (two ways; pick one)

**A. TikTok Studio in the app's browser (no developer app):**
1. Settings > Accounts > TikTok > **Log in to TikTok**: a normal Chrome window opens; log in yourself.
2. Close that window when you see TikTok Studio.
3. Switch TikTok's posting mode to **Automatic (browser)**.

**B. Content Posting API:**
1. <https://developers.tiktok.com/> > Manage apps > create an app; add **Login Kit** and **Content Posting API** (Direct Post).
2. Redirect URI: `http://127.0.0.1:8000/api/platforms/tiktok/callback` (use your port).
3. Settings > Accounts > TikTok > guided setup: paste the **client key** and **client secret** > Connect > approve on TikTok.
4. Until TikTok audits the app, posts are private ("Only me"). Submit the app for review to post publicly.
5. Tick "I confirm TikTok's Music Usage Confirmation" on the Gaming page for automatic API posts.

## Instagram

1. Use a **Professional (Creator or Business)** Instagram account.
2. Nothing to connect: auto-post plans the Reel and, at the planned time, shows a reminder (in-app bar + desktop notice) with the file (Downloads/SharkyClips) and the caption.
3. Open Instagram on your phone or <https://www.instagram.com/>, upload the file, paste the caption, Post; then mark it as posted in the app.

## Auto-post (Gaming page > Auto-post)

1. Render clips (Easy or Pro mode). Only rendered, non-weak clips with a score >= "Minimum clip score" are used, best first.
2. Switch on **Auto-post** and each platform you want; set **Clips per day** (1-3).
3. Set the **posting times** (local), e.g. `12:00, 18:00, 21:00`. Clips per day uses the first N times (more are spread at least 45 min apart).
4. The publish loop plans the next 2 days about once a minute (or press **Plan now**). Planned posts show under each platform.
5. A platform that is not connected, signed out, or hits a login wall / CAPTCHA is **paused** with the reason; fix it, then press **Resume**.
6. A clip is never posted twice on the same platform; a clip that fails to plan is skipped there for 6 hours, then tried again.
