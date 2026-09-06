
# Evaluation: fault injection (before per-partition profiling)

56 seeded defects, each with a known root cause. Every scenario ran against a
repeatable 500,000-row sample.

No language model is involved in this score. It measures profiling, drift
detection and graph traversal only.

> Reconstructed from the run log. The original report file was overwritten
> before it was committed; every number below is taken verbatim from that run's
> console output.

## Headline

| metric                  | result                |
| ----------------------- | --------------------- |
| drift detected          | 56/56 (100%)          |
| correct root node       | 44/56 (79%)           |
| correct column          | 44/56 (79%)           |
| **fully correct** | **44/56 (79%)** |

## By magnitude

| magnitude | passed      |
| --------- | ----------- |
| large     | 20/21 (95%) |
| medium    | 13/19 (68%) |
| small     | 11/16 (69%) |

## By window

| window | passed      |
| ------ | ----------- |
| all    | 1/1 (100%)  |
| month  | 20/21 (95%) |
| week   | 14/17 (82%) |
| day    | 9/17 (53%)  |

## By layer

| layer  | passed      |
| ------ | ----------- |
| source | 39/51 (76%) |
| model  | 5/5 (100%)  |

## Every failure

| scenario                        | detected | root said         |
| ------------------------------- | -------- | ----------------- |
| `precision-loss-medium-month` | yes      | agg_daily_quality |
| `null-spike-small-week`       | yes      | agg_daily_quality |
| `row-drop-small-week`         | yes      | agg_daily_revenue |
| `precision-loss-medium-week`  | yes      | agg_daily_quality |
| `fare-inflation-medium-day`   | yes      | agg_daily_quality |
| `fare-inflation-small-day`    | yes      | agg_daily_revenue |
| `null-spike-medium-day`       | yes      | agg_daily_revenue |
| `null-spike-small-day`        | yes      | agg_daily_revenue |
| `row-drop-large-day`          | yes      | agg_daily_quality |
| `row-drop-medium-day`         | yes      | agg_daily_revenue |
| `row-drop-small-day`          | yes      | agg_daily_revenue |
| `precision-loss-medium-day`   | yes      | agg_daily_revenue |

## Why the failures happen

All 12 failures share one failure mode: the analysis named a downstream
aggregate (`agg_daily_quality` or `agg_daily_revenue`) as the root instead of
the source table. Not one produced an unrelated answer.

The mechanism: profiles are computed over the whole table. `yellow_trips` holds
500,000 rows, so a fault confined to one day barely moves its mean and never
crosses the threshold. `agg_daily_quality` holds one row per day, so the same
fault moves that table's statistics sharply. Drift is therefore visible
downstream and invisible at the source, and the root-cause rule — "a node with
no drifting ancestor is the root" — correctly names the only node it can see.

The traversal is not wrong. The input is incomplete.

Detection itself never failed: 56/56 scenarios produced drift signals. The gap
is attribution, not sensitivity.

The fix is per-partition profiling, measured in `eval-results.md`.
