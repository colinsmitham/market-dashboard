# Market Conditions Dashboard

A free, self-updating data feed and website widget that shows whether Nasdaq-100 (QQQ) conditions call for a **normal, increased, or aggressive** dollar-cost-averaging contribution, based on a transparent rules-based framework.

*Educational personal framework. Not investment advice.*

## How it works

```
Every weekday after the US market close
  GitHub Actions runs fetch_data.py
     → downloads QQQ prices (free, no API key)
     → calculates the 200-day average, drawdown, tier, backtest
     → saves data/market.json and commits it to this repository
  GitHub Pages publishes that file at a public web address
  The widget on the Squarespace page reads the file and draws the dashboard
```

Nothing costs money: public repositories get GitHub Actions and GitHub Pages free.

### What's in this repository

| File | What it does |
|---|---|
| `fetch_data.py` | Downloads prices, does the math, writes `data/market.json`. **The framework thresholds are at the top of this file.** |
| `.github/workflows/update.yml` | The schedule. It tells GitHub to run the script every weekday and save the result. |
| `data/market.json` | The output. It's created automatically the first time the workflow runs. Don't edit it by hand. |
| `README.md` | This guide. |

The widget code (`squarespace-widget.html`) goes in your Squarespace page, not in this repository.

---

## One-time setup (about 20 minutes)

### Step 1: Create a GitHub account
1. Go to **https://github.com** and click **Sign up**.
2. Pick a username. It becomes part of your data address (for example `stonewall-insights`), so choose something you're happy to show.
3. Verify your email address.

### Step 2: Create the repository (the project folder)
1. Once you're signed in, click the **+** at the top right, then **New repository**.
2. **Repository name:** `market-dashboard` (or any short name with no spaces).
3. Choose **Public**. This is required for the free web address the widget reads from.
4. Leave every other box unchecked, then click **Create repository**.

### Step 3: Upload the script and this README
1. On the new, empty repository page, click the link **uploading an existing file**.
2. Drag in `fetch_data.py` and `README.md`.
3. Scroll down and click **Commit changes**. ("Commit" is GitHub's word for "save".)

### Step 4: Add the workflow file
The workflow lives in a folder whose name starts with a dot (`.github`). Mac and Windows hide those folders, so it's easier to create this file directly on GitHub:

1. In your repository, click **Add file**, then **Create new file**.
2. In the name box, type exactly: `.github/workflows/update.yml`
   Each `/` you type turns the part before it into a folder. That's expected.
3. Open `update.yml` on your computer in a text editor (TextEdit or Notepad), copy everything, and paste it into the big editor box on GitHub.
4. Click **Commit changes…**, then **Commit changes** again.

### Step 5: Run the workflow once by hand
1. Click the **Actions** tab at the top of the repository.
2. If you see a message about enabling workflows, click the green button to enable them.
3. In the left sidebar, click **Update market data**.
4. Click **Run workflow** on the right, leave the branch as `main`, then click the green **Run workflow** button.
5. Wait 1–2 minutes and refresh. A **green check mark** means it worked.
6. Go back to the **Code** tab. You should now see a `data` folder containing `market.json`.

> **If the run shows a red ✗ with "Permission denied" or "403" at the push step:** go to **Settings → Actions → General**, scroll to **Workflow permissions**, select **Read and write permissions**, click **Save**, then repeat Step 5.

### Step 6: Turn on GitHub Pages (the public web address)
1. Click **Settings** (top of the repository), then **Pages** in the left sidebar.
2. Under **Build and deployment → Source**, choose **Deploy from a branch**.
3. Under **Branch**, choose `main` and `/ (root)`, then click **Save**.
4. Wait 2–3 minutes and refresh the page. A box will show your site address, like
   `https://YOUR-USERNAME.github.io/market-dashboard/`
5. Test it: open this address in your browser (with your own username and repository name):
   `https://YOUR-USERNAME.github.io/market-dashboard/data/market.json`
   A page full of numbers and text means it's working.

### Step 7: Put the widget on your Squarespace page
1. Open `squarespace-widget.html` in a text editor.
2. Near the top of the `<script>` section, find this line:
   ```js
   var DATA_URL = "https://YOUR-USERNAME.github.io/YOUR-REPO/data/market.json";
   ```
   Replace it with the address you tested in Step 6, keeping the quotation marks.
3. In Squarespace, edit the page and add a **full-width section**. Add a **Code** block to it, leave the mode as **HTML**, and paste the whole file in.
4. Turn **Display Source** off. Set the section background to white with minimal padding.
5. Save, then check the page in **Preview** or on the live site. Squarespace doesn't run scripts inside the editor, so the dashboard looks empty while you're editing. That's normal.

Setup is done. From now on, the data refreshes itself every weekday.

---

## Day-to-day: what to expect

- **When it updates:** at 22:15 UTC each weekday, which is about 4:15 pm Dallas time in winter and 5:15 pm in summer. GitHub sometimes starts scheduled runs 5–30 minutes late.
- **Market holidays:** the script runs, finds no new close, and saves nothing.
- **If the data sources fail:** the script **keeps the previous `market.json`** instead of overwriting it with bad data. The run shows a yellow warning ("Data NOT updated…"), and the widget keeps showing the last good data with its real date. The widget also shows a "data may be out of date" notice if the data is more than 4 days old.
- **Checking on it:** the **Actions** tab lists every run. Green means updated, and a yellow warning means the old data was kept.

## Changing the framework

All thresholds are at the top of `fetch_data.py` under **EDITABLE SETTINGS**:

| Setting | Default | Meaning |
|---|---|---|
| `MA_BAND_PCT` | 5.0 | Tier 1 if within ±5% of the 200-day average; Tier 0 if more than 5% above it |
| `MA_DEEP_PCT` | 10.0 | Tier 2 if 5–10% below the average; Tier 3 if more than 10% below it |
| `DD_LEAN_PCT` | 10.0 | Tier 2 if the drawdown from the high is 10–20% |
| `DD_AGGR_PCT` | 20.0 | Tier 3 if the drawdown is more than 20% |
| `DIP_DAY_PCT` | -1.5 | Shows the "buy-the-dip day" flag if the latest session fell 1.5% or more |
| `TIERS` | 1× / 1.5× / 2× / 3× | Tier names and multipliers |
| `HISTORY_YEARS` | 3 | Years of history in the chart and backtest |
| `BACKTEST_MONTHLY_AMOUNT` | 1000 | Hypothetical base monthly amount used in the backtest |
| `INCLUDE_VIX` | True | Shows the VIX as context. It never changes the tier. |

To change a setting:
1. Click `fetch_data.py` on GitHub, then the **pencil icon** (Edit).
2. Change the number, then click **Commit changes**.
3. Go to **Actions → Update market data → Run workflow** so the page updates now instead of tomorrow.

The widget reads the thresholds from the data file, so the rules shown on your page always match the rules actually used. You never need to edit the widget to change the framework.

## Data sources (all free, no API key)

1. **yfinance**, a Python library that reads Yahoo Finance. This is the main source.
2. **Yahoo Finance's public chart endpoint**, called directly. It's the same data through a different route, so it still works if the yfinance library breaks after a Yahoo change.
3. **Stooq (optional).** Since March 2026, Stooq requires a free API key obtained through a CAPTCHA. If you want it as a third backup, get a key at stooq.com, then in your repository go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `STOOQ_APIKEY` and paste the key. Without the secret, this source is skipped.

Prices are daily closes adjusted for splits but not for dividends.

## Troubleshooting

| What you see | What to do |
|---|---|
| The widget says it couldn't load the data | Open the `DATA_URL` in your browser. If you get a 404 error, recheck Step 6, and make sure `data/market.json` exists in the repository. |
| Red ✗ on a run | Click the run, then the failed step, and read the last lines. A permission error means you should repeat the fix under Step 5. |
| Yellow "Data NOT updated" warning for several days in a row | Yahoo may have changed something. Running the workflow manually installs the newest yfinance, which usually fixes it. You can also add the optional Stooq key. |
| Updates stopped and the Actions tab shows a "disabled" banner | GitHub pauses scheduled workflows in a repository with no activity for 60 days. Click **Enable workflow**. The daily data commits normally keep it active. |
| You'd rather not use GitHub Pages | Use this address as `DATA_URL` instead: `https://raw.githubusercontent.com/YOUR-USERNAME/YOUR-REPO/main/data/market.json` |
