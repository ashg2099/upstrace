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

## Model Availability on Groq

Model names on free tiers change often. If `upstrace explain` returns a 404, list what your key can reach and set `UPSTRACE_LLM_MODEL` accordingly. Cached responses in `cache/llm/` keep working regardless — they outlive the model.

## How well does it work

56 seeded defects — varied across magnitude (1.03x to 1.6x), time window (one day
to one month), and pipeline layer (source vs transformation) — plus 4 negative
controls where nothing was broken and silence is the correct answer. Run against
a repeatable 500,000-row sample.

| metric            | result       |
| ----------------- | ------------ |
| drift detected    | 56/56 (100%) |
| correct root node | 55/56 (98%)  |
| false positives   | 0/4 controls |

The one failure is `fare-inflation-small-day`: a 3% change to one column on one
day out of ninety. It is the smallest fault in the narrowest window in the suite,
and it is where the thresholds are set to stop.

Faults introduced in a transformation rather than the source: 5/5. The analysis
named `fct_trips`, not the source table.

Reproduce with `upstrace eval`. Full breakdown: [docs/eval-results.md](docs/eval-results.md)

Reproduce with `upstrace eval`.
