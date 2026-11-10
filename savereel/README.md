# SaveReel — Facebook video & reel downloader

Paste a public Facebook video or reel link and download it as an MP4.
It's a FastAPI backend that uses yt-dlp, plus a one-page frontend. Videos stream
straight through to the visitor, and nothing is stored on the server.

## Deploy free on Render (about 10 minutes)

1. **Put the code on GitHub.** Create a new repo at github.com/new (e.g. `savereel`),
   then upload every file in this folder: drag them onto the repo page with
   "uploading an existing file". Keep `static/index.html` inside a `static` folder.
2. **Create the service.** Sign in at render.com with GitHub → **New → Blueprint** →
   pick your repo. Render reads `render.yaml` and builds the Docker image.
3. **Open it.** When the build finishes you'll get a URL like
   `https://savereel.onrender.com`. Share that link.
4. **Optional custom domain:** Settings → Custom Domains in Render, then add the
   CNAME record it shows you at your domain registrar.

The free plan sleeps after about 15 minutes idle, so the first visit takes around 30–50
seconds to wake up. The $7/month Starter plan stays awake.

Railway works too: New Project → Deploy from GitHub repo, and it detects the Dockerfile.

## Run it on your Mac

```bash
pip install -r requirements.txt
uvicorn app:app --reload
# open http://localhost:8000
```

## Keeping it working

- **Facebook changes often and yt-dlp catches up within days.** If downloads start
  failing, open Render → your service → **Manual Deploy → Clear build cache & deploy**.
  This rebuilds with the latest yt-dlp.
- **Facebook sometimes blocks data-centre IPs.** If public links work on your Mac but
  fail on Render, the server's IP is being blocked. Redeploying, trying another region,
  or moving to another host usually helps.
- **Rate limit:** each visitor gets 20 requests per 10 minutes. Change `RATE_LIMIT` in `app.py`.
- **Private videos aren't supported.** That's deliberate, since it would need someone's
  Facebook login on the server.

## Legal note

Facebook's terms don't allow downloading other people's content without permission.
The footer tells users to download only videos they own or have permission to use.
Downloader sites can get takedown or DMCA notices, so add a contact email to the
footer so rights holders can reach you, and remove content when asked.
