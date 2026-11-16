# Flight Price Tracker (web)

Tracks the ticket price of one specific flight (or connecting itinerary) on one
travel date, and shows the price movement as a graph on a web page that works
on an iPad, a phone or a computer.

- **The page**: `https://<your GitHub username>.github.io/flight-price-web/`
- **The checking** is done by GitHub once an hour (`.github/workflows/tracker.yml`),
  so no device has to stay on.
- **Price data**: Google Flights, read through SerpApi. The SerpApi key is the
  repository secret `SERPAPI_KEY` and is never written to this repository.
- **The data** (tracked flights and every price reading) is in `data.json`.
  This repository is public, so anyone with the address can see it.

## Adding or changing flights

From the page: save a GitHub access token once per device under "Changes from
this device" (the page lists the steps), then use the form and buttons.

Without a token: open the **Actions** tab, choose **Flight price tracker**,
press **Run workflow**, fill in the fields and run it.

## Files

| File | Purpose |
|---|---|
| `index.html` | The page (graph, form, token setup). |
| `tracker.py` | Gets prices and updates `data.json`. Python standard library only. |
| `data.json` | Tracked flights and price readings. |
| `.github/workflows/tracker.yml` | Runs `tracker.py` hourly and on request. |

## Notes

- Each check uses 1 SerpApi search, plus 1 more when sellers are recorded.
  One flight checked twice a day with sellers is about 120 searches a month;
  the free SerpApi plan allows 250.
- GitHub's hourly schedule can run late at busy times, so a check may be
  recorded some minutes after it falls due.
- Tracking stops at the end of the chosen period or after the travel date.
