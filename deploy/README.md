# Running KasFlex unattended

The daily job fetches whatever the cache is missing, plans the next day, and
appends one record to `results/daily.jsonl`. It is safe to run repeatedly: a day
already cached is never re-fetched, and re-running produces the same record.

Time it for **after the day-ahead auction**. Dutch day-ahead prices publish around
13:00 CET; before that, tomorrow's prices do not exist and ENTSO-E returns an
acknowledgement rather than data.

## One-time setup

```bash
git clone <this repository> /opt/kasflex && cd /opt/kasflex
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p logs data/cache results
```

Get an ENTSO-E token: register at <https://transparency.entsoe.eu/>, then email
their helpdesk to request API access (it is not automatic).

Confirm it works before scheduling anything:

```bash
export ENTSOE_API_KEY=...
.venv/bin/kasflex fetch --date "$(date -d yesterday +%F)"
.venv/bin/kasflex daily --date "$(date -d yesterday +%F)"
```

## cron

```bash
crontab deploy/kasflex-daily.cron
```

Cron does not read your shell profile, so `ENTSOE_API_KEY` must be set in the
crontab itself or in a file the job sources. That omission is the most common
reason a job like this quietly stops working.

## systemd

```bash
sudo install -d /etc/kasflex
echo 'ENTSOE_API_KEY=...' | sudo tee /etc/kasflex/env
sudo chmod 600 /etc/kasflex/env
sudo cp deploy/kasflex-daily.{service,timer} /etc/systemd/system/
sudo systemctl enable --now kasflex-daily.timer
systemctl list-timers kasflex-daily
```

Preferred over cron here: `Persistent=true` means a machine that was off at 14:10
still runs the job when it comes back, and the journal keeps the output.

## GitHub Actions

`.github/workflows/daily.yml` runs the same job on a schedule and commits the
cache back to the repository, which doubles as an audit trail of exactly what data
each result was computed from. Set `ENTSOE_API_KEY` as a repository secret.

Note that GitHub's scheduled runners are best-effort and can be delayed by tens of
minutes under load. That is fine for a research pipeline and not fine for anything
operational.

## What to watch

* `results/daily.jsonl` should gain one line per day.
* A record with `"actuals_available": false` was scored against the forecast
  because the weather archive had not caught up. Re-running that date after the
  fact produces a genuine out-of-sample score.
* `data/cache/MANIFEST.json` records the source, licence, retrieval date and
  checksum of every series. If a checksum ever fails to match, the run stops
  rather than quietly using data that no longer matches its provenance.
