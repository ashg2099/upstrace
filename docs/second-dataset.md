
# Does it work on anything but the taxi data?

The engine claims to be dataset-agnostic. That claim was never tested until this
document. Everything below was run against a project Upstrace had never seen,
built the way any outside user would build it: a separate folder, a separate
warehouse, a separate dbt project, and the tool installed from this repo.

## The setup

[Citi Bike trip data](https://citibikenyc.com/system-data), Jersey City,
January–March 2025. 169,159 rides across 91 days, 13 columns — and nothing in
common with taxi trips beyond the fact that both have a timestamp.

```
ride_id            VARCHAR       start_lat   DOUBLE
rideable_type      VARCHAR       start_lng   DOUBLE
started_at         TIMESTAMP     end_lat     DOUBLE
ended_at           TIMESTAMP     end_lng     DOUBLE
start_station_name VARCHAR       member_casual VARCHAR
...
```

A four-model dbt project on top of it:

```
rides (source) → stg_rides (view) → fct_rides (table) → agg_daily_rides
                                                      → agg_daily_quality
```

Then, from that folder:

```bash
upstrace init          # edit warehouse and dbt_project_dir
upstrace config
upstrace profile
upstrace profile
upstrace drift
```

## First result: it ran

Five nodes discovered, 53 column profiles written, 91 partitions per model, and
`No drift above threshold` on two identical runs — first attempt, no code
changes. The determinism work done for the taxi data held on types it had never
been tested against: doubles that are geographic coordinates, booleans, and
free-text station names.

## The fault

An upstream vendor starts sending `member` as `Member` from March onward. No
nulls, no schema change, no constraint violated:

```sql
UPDATE raw.rides
SET member_casual = upper(substr(member_casual, 1, 1)) || substr(member_casual, 2)
WHERE started_at >= DATE '2025-03-01';
```

73,289 rows affected. `dbt run` reports `PASS=4 WARN=0 ERROR=0`.

But `agg_daily_rides.member_share` computes `rider_type = 'member'`, which is
case-sensitive. For every day in March, the member share of rides silently
became **zero**.

## What Upstrace reported

```
critical │ rides           │ member_casual │ distinct_count │  2.0000 │ 4.0000 │ 100.0%
critical │ agg_daily_rides │ member_share  │ mean_value     │  0.8008 │ 0.0000 │ 100.0%
critical │ agg_daily_rides │ member_share  │ mean_value     │  0.8442 │ 0.5659 │  33.0%
warning  │ rides           │ member_casual │ min_value      │  casual │ Casual │ changed

INCIDENT 1 - CRITICAL  root: rides (source)
  also drifted: agg_daily_rides, fct_rides, stg_rides
```

Root cause correct on the first attempt.

And the two `member_share` rows reproduce, on a dataset nobody tuned for, the
finding that drove the whole per-partition rewrite:

| view              | baseline | current          | change           |
| ----------------- | -------- | ---------------- | ---------------- |
| whole-table       | 0.8442   | 0.5659           | 33.0%            |
| **per-day** | 0.8008   | **0.0000** | **100.0%** |

33% is worrying but ambiguous. Exactly 0.0000 has one explanation: the
comparison stopped matching. Averaging across 91 days turns a broken metric into
a merely suspicious one.

## The bug this found

The first explanation was wrong in an instructive way:

> The column name 'member_casual' strongly implies a binary flag... the new
> values likely include numbers outside the original 0-1 range (e.g., 2, 3, or
> negative numbers)

The model invented a 0/1 domain and never considered casing. Its suggested test,
`accepted_values [0, 1]`, would have failed every row.

The reason was in Upstrace, not the model. `drift.py` created min/max signals
like this:

```python
Signal(model, column, metric, None, None, 1.0, "warning")
```

`'casual'` and `'Casual'` were in hand and thrown away. The prompt said only
"min_value changed". The single most informative fact in the incident never
reached the model.

Fifty-six fault scenarios never surfaced this, because in the taxi data the
decisive clue is in a column *name* — `trip_distance_miles` — not in a value.
Here the clue is only in the value.

After carrying the actual values through `Signal` → `Evidence` → the prompt:

> The min_value changed from 'casual' to 'Casual', indicating a character-level
> rewrite rather than a replacement of the entire dataset... if downstream logic
> uses exact string matching (e.g. `CASE WHEN rider_type = 'member'`), the new
> 'Member' values will be misclassified or dropped, leading to undercounted
> member rides.

Correct cause, correct mechanism, and this time the suggested `accepted_values`
test is the right one.

## The privacy consequence

Sending min/max means sending real cell values — the smallest value in an email
column is somebody's email address. The rest of the design deliberately sends
metrics and never rows, and this would have quietly broken that.

So values are maskable per column:

```yaml
profile:
  mask_values: ["*email*", "*_name", "customer_id"]
```

Matched columns report `<masked>`, and the prompt tells the model not to guess
what was behind it.

## What this exercise was worth

- The dataset-agnostic claim in the README is now tested rather than asserted
- Per-day profiling's advantage reproduced on data it was not designed against
- One real bug found, of a kind the existing 56-scenario benchmark structurally
  could not find
- One privacy hole found and closed as a direct consequence of fixing it

Testing on a second dataset was worth more than adding a fifty-seventh fault
scenario to the first one.
