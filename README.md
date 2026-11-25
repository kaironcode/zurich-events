# Zurich events

A personal app that checks Zurich event sources every morning and shows new events as swipeable cards.

- **Daily check:** `.github/workflows/daily.yml` runs `scraper/run.py` on GitHub's servers at 04:37 UTC (05:37 Zurich time in winter, 06:37 in summer). No laptop needed.
- **Extraction:** pages that changed since the last check are read by the Claude API (`claude-haiku-5-5`). Meetup groups are read from their iCal feeds.
- **Storage:** Supabase (Postgres) holds events, your swipes and your settings.
- **Site:** `site/` is served by GitHub Pages and can be added to your phone's home screen.

Events stay in Discover until you swipe them, the event has passed, or its registration deadline has passed. Offers stay until you hide or save them.

## One-time setup

### 1. Supabase
1. Create a free project at supabase.com (region: Zurich or Frankfurt).
2. **SQL Editor → New query**: paste `supabase/schema.sql` and click **Run**.
3. **Authentication → URL Configuration**: set **Site URL** to `https://kaironcode.github.io/zurich-events/` and add the same URL under **Redirect URLs**.
4. **Project Settings → API**: copy the **Project URL**, the **anon public** key and the **service_role** key.

### 2. GitHub repository settings
**Settings → Secrets and variables → Actions**
- *Secrets*: `ANTHROPIC_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`
- *Variables*: `SUPABASE_URL`, `SUPABASE_ANON_KEY`

**Settings → Pages → Build and deployment → Source: GitHub Actions**

### 3. First run
- **Actions → Deploy site → Run workflow** publishes the site.
- **Actions → Daily event check → Run workflow** fills the database. You can enter a few source ids to test only those.
- Open `https://kaironcode.github.io/zurich-events/`, sign in with your email, then add it to your home screen.
- Optional: after you have signed in, turn off new sign-ups in **Supabase → Authentication → Sign In / Providers**, so nobody else can create an account.

## Adding or removing sources
Edit `sources.yaml`. Each source has an `id`, `name`, `category` (book, read, philo, art, event, offer) and `url`. Optional settings:

- `kind: ical` for calendar feeds
- `kind: offer` for discount pages
- `aggregator: true` for big listings; only events matching your interests are kept
- `details: true` to open new events' own pages to look for a price
- `enabled: false` to stop checking a source

Hiding a source in the app's **Sources** tab only hides its events for you; the source is still checked.
