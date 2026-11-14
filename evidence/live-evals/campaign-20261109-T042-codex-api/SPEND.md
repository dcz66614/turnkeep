# T042 codex-api campaign — actual spend (GPT-6 Luna, official OpenAI API)

All figures are computed from the rollout usage records saved per attempt
(`usage` split in `attempts.jsonl`: input / cached input / cache-write /
output / reasoning). The records are cumulative per session, so the
per-attempt split is the billed total for that attempt. Cross-checks:
input = cached + cache-write + fresh exactly, in both suites; and the
rollout's final `total_token_usage` equals input + output exactly
(spot-checked on conf-f01-changed-log-seed11: 40,132 + 197 = 40,329).

Caveat: the `tokens_total` field in each trace (parsed from codex's stderr
"tokens used" line) is the LAST TURN's count only, not the session total —
e.g. 13,770 vs the session total 40,329 above. Do NOT cost from
`tokens_total`; cost from the split below.

## Rates (Luna Standard, per 1M tokens, prompts ≤ 272K input)

- Uncached (fresh) input $0.10, cached input $0.01, cache write $0.125,
  output $0.50. Reasoning tokens are included in `output_tokens`
  (verified: total = input + output), so they are not added separately.
- No long-context repricing applied: per-turn prompts in the sampled
  rollouts were ~13–55k tokens, well under the 272K line.

## Formula (per attempt)

```
fresh = input_tokens - cached_input_tokens - cache_write_input_tokens
cost  = fresh*$0.10 + cached*$0.01 + cache_write*$0.125 + output*$0.50
        (all per 1M tokens)
```

## Totals

| Suite | Attempts | Input | Cached | Cache-write | Output | Reasoning | Fresh | Cost |
|---|---|---|---|---|---|---|---|---|
| live-pilot | 48 | 2,877,122 | 1,271,986 | 1,604,512 | 18,398 | 3,844 | 624 | **$0.2225** |
| live-confirmation | 432 | 35,079,495 | 24,518,557 | 10,553,588 | 224,352 | 50,815 | 7,350 | **$1.6773** |
| **Total** | **480** | **37,956,617** | **25,790,543** | **12,158,100** | **242,750** | **54,659** | **7,974** | **$1.8998** |

- **Total actual spend: $1.90 for all 480 attempts** (~$0.0040 per attempt).
- Budget cap was $25 (`state/eval-budget-codex-api.json`); actual spend is
  7.6% of the cap. The pre-run estimate ($10–16) assumed uncached input;
  the real cost is ~5–8x lower because prompt caching works: only 7,974
  of 37,956,617 input tokens (0.02%) were fresh — the ~49k fixed prompt
  is cached after the first turn of each session, and cache writes
  ($0.125/1M) cost only slightly more than fresh input ($0.10/1M).
- Output is tiny (242,750 tokens, 0.6% of input): Luna-low writes short
  tool calls and almost no prose, and reasoning (54,659) is a subset of
  output, not extra.
