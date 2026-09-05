
# Upstrace

Trace a failed data quality check upstream to the change that caused it.

## Why

Data pipelines fail loudly when they crash. They fail *silently* when the data
is simply wrong — and that is the expensive case.

Here is a real demonstration, reproducible in this repo. A single upstream unit
change is injected: `trip_distance` starts arriving in kilometres instead of miles,
from March onward.

$ upstrace fault unit-change

$ dbt run    →  Completed successfully.  PASS=4  ERROR=0

$ dbt test   →  PASS=11  ERROR=2   (the same two failures as before the fault)

dbt is entirely satisfied. Meanwhile:

| month   | trips   | avg distance | revenue per mile |
| ------- | ------- | ------------ | ---------------- |
| 2024-01 | 2749310 | 3.272        | 8.35             |
| 2024-02 | 2744210 | 3.39         | 8.053            |
| 2024-03 | 3063188 | 5.719        | 4.94             |

`revenue_per_mile` — a metric an operations team actually watches — drops ~38%
in March. Nothing in the stack reports it.

Upstrace does:

$ upstrace profile

$ upstrace drift

## What the data itself already contains

Before injecting anything, three months of NYC TLC yellow taxi data (9,554,778 trips)
already carry:

- **$20.1M of fares (7.8%)** in rows that fail at least one basic quality rule
- **751,962 trips (7.9%)** with `payment_type = 0`, a value the TLC data dictionary
  does not document
- trips dated **2002, 2008 and 2009** inside files named `2024-01`, `2024-02`, `2024-03`
- one trip recorded **twice**, byte-identical, in 9.5M rows
