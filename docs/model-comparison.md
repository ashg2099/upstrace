
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
- The 27B summary line asserts "~1.6x increase" while the observed ratio is 1.255x. Its own reasoning explains the gap correctly, but the one-line summary overstates it. Summaries can be more confident than the reasoning behind them.

Every response in this comparison is in `cache/llm/` and reproducible with
`upstrace explain --no-cache` and the model set via `UPSTRACE_LLM_MODEL`.
