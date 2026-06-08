# lead-finder

A small Python CLI that finds local businesses **without a website** using the
Google **Places API (New) v1**, so you can build a cold-call list.

It outputs a CSV of businesses that have no website link, with their name,
phone, address, rating, and review count.

## How it works

The Places API does **not** return a website in search results, so the tool
runs in two stages:

1. **Text Search** (`places:searchText`) — cheap. Gets place IDs and basic
   fields for your query (e.g. `"plumbers in Asheville NC"`), following
   pagination up to `--max-pages`.
2. **Place Details** (`places/{id}`) — **billed per request**. Fetches
   `websiteUri` and the phone number for each candidate. This stage is gated
   behind a cost guard.

A business is kept **only if its `websiteUri` is missing/empty**.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Dependencies are intentionally minimal: `requests` and `python-dotenv`.

### 2. Get a Google Places API key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create (or select) a project.
3. Open **APIs & Services → Library**, search for **"Places API (New)"**, and
   click **Enable**. (Make sure it's the *New* one, not the legacy "Places
   API".)
4. Open **APIs & Services → Credentials → Create credentials → API key**.
5. Copy the key. It's strongly recommended to restrict it to the Places API.
6. Make sure **billing is enabled** on the project — the Places API requires it.

### 3. Configure your key

Copy the example env file and paste in your key:

```bash
cp .env.example .env
# then edit .env and set GOOGLE_PLACES_API_KEY=...
```

The tool reads `GOOGLE_PLACES_API_KEY` from the environment, loading a `.env`
file automatically if present. The key is never hardcoded, and `.env` is
git-ignored.

## Usage

```bash
python find_leads.py --query "plumbers in Asheville NC" --max-results 40 --min-reviews 10 --yes
```

### Flags

| Flag             | Default    | Description                                                |
|------------------|------------|------------------------------------------------------------|
| `--query`        | (required) | Search query, e.g. `"plumbers in Asheville NC"`.           |
| `--max-results`  | `50`       | **Hard cap** on billed Place Details calls.                |
| `--min-reviews`  | `5`        | Skip businesses with fewer reviews than this.              |
| `--min-rating`   | `0`        | Skip businesses rated below this.                          |
| `--max-pages`    | `3`        | Max Text Search pages to fetch (pagination).               |
| `--yes`          | off        | Skip the interactive cost-confirmation prompt.             |
| `--output-dir`   | `./output` | Where to write the timestamped CSV.                        |

### Output

A timestamped CSV is written to `./output/` (configurable) with columns:

```
name, phone, address, rating, review_count, place_id, social_only
```

- **social_only** — kept for downstream use. This API/field mask can't reliably
  detect whether a business is "social media only", so it's set to `FALSE`.
  These are still leads.

At the end you get a summary line: `X businesses checked, Y with no website.`

## Cost guard 💸

**Place Details calls are billed per request.** Before Stage 2, the tool prints
how many Details calls it will make and a rough cost estimate, then requires
either the `--yes` flag or an interactive `y` confirmation to proceed.

- `--max-results` is a hard cap on the number of billed Details calls.
- `--min-reviews` / `--min-rating` pre-filter candidates *before* Stage 2, so
  you don't spend Details calls on dead businesses.
- Every billed call is logged with a running count, so you always know your spend.

The per-call cost shown is a rough estimate (`$0.017/call`). **Check the
[current Google Places pricing](https://developers.google.com/maps/documentation/places/web-service/usage-and-billing)
for exact figures** — pricing and free-tier credits change over time. You are
responsible for your own API charges.

## Error handling

- Retries on `429` and `5xx` responses with exponential backoff.
- Fails fast with a clear message if the API key is missing (`401`/`403`).
