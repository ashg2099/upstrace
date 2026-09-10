
# Which model, and does the prompt matter more?

One incident (`unit-change` fault injected into `raw.yellow_trips`), four runs:
two models, before and after one prompt change.

## The prompt change

Originally the prompt carried only the **root node's** column name. The root is a source table where the column is called `trip_distance` — no unit in the name. The renamed column `trip_distance_miles` exists only downstream, so the strongest available clue never reached the model.

The fix adds the drifted column names of every downstream node to the prompt.

## Results

|                         | before                                                                                 | after                                                                      |
| ----------------------- | -------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `openai/gpt-oss-120b` | "e.g., a mistaken unit conversion"; treated the observed 1.255 ratio as the multiplier | names miles→kilometres, cites 1.609, cites the downstream column name     |
| `qwen/qwen3.8-27b`    | names miles→km, confidence**medium**                                            | confidence**high**, quotes the contradicting column name as evidence |

## What this says

- Before the fix, the 27B model outreasoned the 120B model. Size did not decide
  the outcome on this task.
- After the fix, both are correct. The gain came from context, not capability.
- The 120B answers are shorter and offer fewer alternative hypotheses. The 27B
  answers rank multiple causes and are more useful for triage.

## Caveats found while reading the answers

- Both models suggest a range test such as "trip_distance between 0.1 and 50 miles". Real NYC trips to airports and out of state exceed 50 miles, so applying that suggestion as written would generate false positives. The model proposes; a human still decides.
- The 27B summary line asserts "~1.6x increase" while the observed ratio is 1.255x. Its own reasoning explains the gap correctly, but the one-line summary overstates it. Summaries can be more confident than the reasoning behind them. **Resolved in round three** — with per-day evidence the observed ratio is 1.6094 and the summary matches it.

Every response in this comparison is in `cache/llm/` and reproducible with
`upstrace explain --no-cache` and the model set via `UPSTRACE_LLM_MODEL`.

## Round three: after per-partition profiling

The two rounds above compared models against the same evidence. This round
changes the evidence instead: profiling moved from whole-table to per-day, so the
prompt now carries both figures for the same metric.

```
column          metric        baseline   current   change   days
trip_distance   mean_value      3.2496    5.2298    60.9%     31
trip_distance   mean_value      4.6339    5.7401    23.9%      -
trip_distance   distinct_count   3,214     3,906    21.5%      -
```

`qwen/qwen3.8-27b` picked the per-day row:

> The mean value increased by a factor of approximately 1.61 (3.25 to 5.23). The
> conversion factor from miles to kilometers is 1.60934. This ratio is too close
> to be coincidental.

Three things worth noting.

**It chose the right row.** Two mean_value rows were offered. The whole-table
pair gives 5.7401 / 4.6339 = 1.239 — close to nothing in particular. The per-day
pair gives 1.6094. The model anchored on the one that carries a signature, and
said why.

**It rejected the obvious alternative rather than listing it.** Distinct count
also moved 21.5%, which invites a duplicate-records explanation. Instead of
offering it as an equal option, the model argued against it — a 21.5% move in
distinct values cannot produce a 60.9% move in the mean unless the new values are
systematically larger, which is a unit change. Ranking hypotheses is more useful
than enumerating them.

**The summary overstatement is gone.** The caveat below was recorded when the
whole-table ratio was 1.255 and the summary claimed ~1.6x. The summary was ahead
of its evidence. With per-day evidence, ~1.6x is simply correct. The model did
not change; the measurement did.

That last point is the one worth carrying: a good deal of what reads as model
error is the prompt asserting something the evidence does not support. Fix the
evidence before fixing the model.
