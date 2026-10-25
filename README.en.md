# nekro-media-forward

> Video / image-post auto forwarding for QQ — a [NekroAgent](https://github.com/KroMiose/nekro-agent)
> plugin with an optional local resolver. Works with OneBot v11 / NapCat.

中文说明见 [README.md](README.md)。

Auto-detect **Douyin (抖音) / Xiaohongshu (小红书) / TikTok / Bilibili (B站) / X (Twitter)** links in QQ messages and
re-send the media as a QQ **merged forward** (合并转发) card.

- **Zero third-party dependencies** — the plugin is a single Python file; the resolver is a single
  Node.js file that only uses the standard library plus the system Edge browser.
- **Bilibili needs neither** — it goes straight to the public `api.bilibili.com` endpoints from the
  server (`view` + wbi-signed `playurl`), and merges DASH video/audio with ffmpeg, giving 1080p/4K
  once a login cookie is configured (see `tools/bilibili-qr-login.sh`).
- **Doesn't interrupt the conversation** — link messages are handled by the plugin and the AI trigger
  is blocked, so the bot doesn't reply with small talk on top of the video.
- **Residential-IP resolution** — datacenter IPs are blocked by Douyin/Xiaohongshu anti-bot. The
  resolver runs on your home PC and drives a real headless browser, then hands back direct URLs.
- **Polls outbound** — the resolver works behind NAT; your server never needs to reach the home PC.
- **Bilibili supported** — the public `api.bilibili.com` endpoints are used (`view` for cid,
  `playurl` with `fnval=1` for a single-file mp4), because datacenter IPs get HTTP 412 from the web
  pages. So **Bilibili needs neither the local resolver nor ffmpeg**; without login it caps at 720p.
- **X (Twitter) supported** — reads the public embed ("syndication") endpoint
  `cdn.syndication.twimg.com/tweet-result` that X's own embed widget uses: **no login, no API token, no local
  resolver**. The card carries the post text (author, Beijing time, body, quoted post) followed by the
  images / video (highest-bitrate mp4 under `x_max_size_mb`; animated GIFs are mp4 too; photos use `?name=orig`).
  Accepted links: `x.com` / `twitter.com` / `mobile.twitter.com` / `fxtwitter.com` / `vxtwitter.com`
  `/<user>/status/<id>` and `/i/status/<id>` (with `?s=46`, `/photo/1`, `/video/1`). Config keys:
  `x_enabled`, `x_max_size_mb`, `x_send_text`, `x_show_stats`.
  **Limits:** the endpoint is *not* an official API and may change or rate-limit at any time; only public single
  posts can be read (protected / deleted / login-gated / age-restricted posts fail with a readable message);
  long posts (note tweets) are truncated; `t.co` short links and media inside a quoted post are not handled.
  When the endpoint is unavailable the plugin falls back to yt-dlp (video only, no text).
- **Image posts supported** — Xiaohongshu/Douyin image notes are delivered as one merged card with
  up to `max_images` (default 9) images.

## Architecture

```
QQ message with a link
        │
        ▼
┌──────────────────────────────┐
│ NekroAgent + this plugin     │  ① detect link → ② resolve → ③ fetch → ④ merged forward
│  /resolver/* job queue API    │
└───────────────┬──────────────┘
                │ long-poll with a shared token (resolver initiates)
                ▼
┌──────────────────────────────┐
│ media-resolver (home PC)     │  headless Edge + CDP, reads page state, returns direct URLs
│  Douyin / XHS / TikTok        │  or downloads TikTok through a proxy
└──────────────────────────────┘
```

## Install

### 1. Plugin

Copy `plugin/plugin.py` into NekroAgent's plugin directory and restart the container, then fill in the
plugin config (see `plugin/config.example.yaml`). At minimum:

- `public_base_url` — URL of your NekroAgent that NapCat can reach (e.g. `http://1.2.3.4:8022`)
- `resolver_token` — any long random string; the resolver must use the same value

### 2. Local resolver (optional, required for Douyin / Xiaohongshu / TikTok)

Windows + Node.js ≥ 18 + Edge.

```powershell
mkdir C:\media-resolver
copy resolver\resolver.js      C:\media-resolver\
copy resolver\agent.js         C:\media-resolver\
copy resolver\start.bat        C:\media-resolver\
copy resolver\config.example.json C:\media-resolver\config.json
# edit config.json: server -> your NekroAgent URL, token -> same as resolver_token
```

Run `start.bat`. Optional: `resolver/install-autostart.ps1` registers a scheduled task that starts it
at logon and keeps it alive.

## Two deployment shapes

| Shape | NapCat location | Setting | Delivery |
|---|---|---|---|
| **Same machine** (typical home setup) | same Windows PC as the resolver | `allow_resolver_local_path: true` | resolver returns `local_path`; the plugin passes that path to NapCat directly (fastest, no relay) |
| **Split** (NapCat in the cloud) | cloud server | `allow_resolver_local_path: false` | resolver uploads the file via `POST /resolver/upload`; the plugin serves it as `/media/{token}` |

Configuration keys and the resolver options are documented in [README.md](README.md) (Chinese) and
`plugin/config.example.yaml` / `resolver/config.example.json`.

## Self-test

```bash
curl "http://<server>:8022/plugins/local_video.video_forward/resolver/self-test?token=<token>&url=<url>&download=true"
```

Returns JSON describing what was resolved, e.g. `{"ok":true,"platform":"douyin","downloaded_mb":4.9}`
or `{"ok":true,"platform":"xiaohongshu","mode":"images","image_count":5}`.

## Troubleshooting

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) (Chinese, but the error strings are literal):
browser profile locks, datacenter-IP blocking, wrong `local_path` shape, TikTok short-link casing.

## Disclaimer

For personal, non-commercial use only. Respect the terms of service of each platform and local law.
Only public content is supported; paid / private / friends-only / login-required content is not
bypassed. Respect the original authors' copyright before forwarding media to public groups.

## License

**PolyForm Noncommercial 1.0.0** — personal use, modification and redistribution are permitted;
**commercial use is not**. Full text in [LICENSE](LICENSE).

> ⚠️ This is a source-available license, not an OSI-approved open-source license.
> Contact the author for commercial licensing.
