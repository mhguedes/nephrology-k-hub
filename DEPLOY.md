# Deploy the hub with automatic weekly updates (GitHub Actions)

This makes the hub **auto-refresh every Monday** and publishes it as a live website —
no server, no upkeep, free. One-time setup, ~5 minutes.

## What you'll get

- A weekly job (GitHub Actions) re-queries NIH RePORTER, re-classifies, and rewrites `data.json`.
- The site is republished to **GitHub Pages** automatically, so the live page always shows current data.
- You can also trigger a refresh any time from the **Actions** tab → *Weekly nephrology K-grant refresh* → *Run workflow*.

## One-time setup

You'll need a free [GitHub account](https://github.com/signup) and either the
[`gh` CLI](https://cli.github.com/) (easiest) or `git`.

### Option A — using the `gh` CLI (recommended)

From inside the `nephrology-k-hub` folder:

```bash
cd nephrology-k-hub
git init -b main
git add .
git commit -m "Nephrology K-Grant Hub"
gh repo create nephrology-k-hub --public --source=. --push
```

Then enable Pages to publish from the workflow:

```bash
gh api -X POST repos/:owner/nephrology-k-hub/pages -f build_type=workflow || true
```

(If that command errors, just set it in the UI: **Settings → Pages → Build and deployment → Source = "GitHub Actions"**.)

### Option B — using the GitHub website + git

1. On GitHub, click **New repository**, name it `nephrology-k-hub`, make it **Public**, and create it (no README).
2. In the `nephrology-k-hub` folder on your computer:

   ```bash
   cd nephrology-k-hub
   git init -b main
   git add .
   git commit -m "Nephrology K-Grant Hub"
   git remote add origin https://github.com/<your-username>/nephrology-k-hub.git
   git push -u origin main
   ```
3. In the repo on GitHub: **Settings → Pages → Build and deployment → Source → "GitHub Actions"**.

## Turn it on

1. Open the **Actions** tab. If prompted, click **"I understand my workflows, enable them."**
2. Click **Weekly nephrology K-grant refresh → Run workflow** to do the first run now.
3. When it finishes, your live site is at:
   **`https://<your-username>.github.io/nephrology-k-hub/`**

After that it runs by itself every Monday.

## Notes

- The pipeline uses only the Python standard library — nothing to install.
- It pulls a **rolling fiscal-year window** around the current year, so the dataset stays current automatically.
- The default filter is the kidney **study-population** filter; the workflow can be switched to
  maximum recall by changing the run line to `python pipeline/reporter_pull.py --loose`.
- GitHub disables scheduled workflows after 60 days of **no repo activity**; the weekly commit
  counts as activity, so this normally keeps itself alive. If it ever pauses, open Actions and click
  **Enable workflow** (or run it once manually).
- Cost: free for public repositories.
