# Evaluation: fault injection

56 seeded defects, each with a known root cause, plus 4 controls where nothing was broken and the correct answer is silence. Every scenario runs against a repeatable 500,000-row sample.

No language model is involved in this score. It measures profiling, drift detection and graph traversal only.

## Headline

| metric | result |
|---|---|
| drift detected | 56/56 (100%) |
| correct root node | 55/56 (98%) |
| correct column | 55/56 (98%) |
| **fully correct** | **55/56 (98%)** |
| false positives | 0/4 controls |

Detection rate alone is not evidence of anything: a detector that fires on every run would score 100% on it. The false-positive row is what makes the rest meaningful.

## By magnitude

| magnitude | passed |
|---|---|
| large | 21/21 (100%) |
| medium | 19/19 (100%) |
| none | 4/4 (100%) |
| small | 15/16 (94%) |

## By window

| window | passed |
|---|---|
| all | 1/1 (100%) |
| day | 16/17 (94%) |
| month | 21/21 (100%) |
| none | 4/4 (100%) |
| week | 17/17 (100%) |

## By layer

| layer | passed |
|---|---|
| control | 4/4 (100%) |
| model | 5/5 (100%) |
| source | 50/51 (98%) |

## Everything that did not behave correctly

| scenario | what it was | detected | root said |
|---|---|---|---|
| `fare-inflation-small-day` | fare_amount multiplied by 1.03 over one day | yes | agg_daily_quality |
