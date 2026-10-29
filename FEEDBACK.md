# Nebius Token Factory & NVIDIA Nemotron: Empirical Feedback & Observations

This document records first-hand technical observations, telemetry data, and friction points encountered while building **Saha** on Nebius Token Factory during the hackathon.

---

## 1. Latency Inversion & Nano-30B Finding

### Telemetry Summary (from 58 recorded calls in `llm_calls` table)

| Role | Model Identifier | Call Count | Avg Latency (ms) | Avg Prompt Tokens | Avg Completion Tokens |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **WORKER** | `nvidia/nemotron-3-super-120b-a12b` | 37 | **2,429.7 ms** | 100.0 | 239.8 |
| **PLANNER** | `nvidia/Nemotron-3-Ultra-550b-a55b` | 13 | **2,816.8 ms** | 1,959.4 | 747.3 |
| **FAST** | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | 8 | **5,817.4 ms** | 103.4 | 250.5 |

### Observation
We observed an unexpected **latency inversion**:
- The smallest model, **Nano-30B**, was by far the slowest (averaging **5.82 seconds**, with individual calls taking up to 7.8 seconds).
- The much larger **Super-120B** was the fastest model in the cluster (**2.43 seconds**, 2.4x faster than Nano).
- The flagship **Ultra-550B** processed massive prompt contexts (~2,000 tokens) with complex JSON schemas in **2.82 seconds**.

### Root Cause
1. **Unsuppressible Reasoning**: Nano-30B automatically generates internal chain-of-thought/thinking tokens before generating its response. Even when requesting a 1-sentence answer (`max_tokens=150`), the token generation budget is consumed by thinking, driving up latency.
2. **Infrastructure Allocation (Uncertain)**: It is possible that Nebius Token Factory assigns different hardware profiles or larger concurrency queues to Nano-30B compared to dedicated high-throughput nodes for Super-120B and Ultra-550B.

### Architectural Fix in Saha
Because the FAST model exceeded our 4.0-second interactive budget, we made the drift explanation non-blocking:
- The deterministic probe failure reason is committed to the database immediately.
- The FAST sentence is generated in a background thread and swapped into the UI asynchronously.
- A 15-second timeout falls back to the deterministic reason.

---

## 2. Model Identifier Casing Inconsistency

### Observation
The three Nemotron model IDs hosted on Nebius Token Factory do not follow a uniform naming or casing standard:

```
nvidia/nemotron-3-super-120b-a12b       <-- all lowercase
nvidia/Nemotron-3-Ultra-550b-a55b       <-- PascalCase 'Nemotron', 'Ultra'; lowercase 'b'
nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B   <-- UPPERCASE 'NVIDIA', '30B', 'A3B'
```

### Impact
When attempting to call the Nano model using lowercase `nvidia/nemotron-3-nano-30b-a3b`, the Token Factory API returned an HTTP 404 (Model Not Found). Developers must strictly preserve the exact mixed-casing string for each model.

---

## 3. `reasoning_effort` Parameter Behavior

### Observation
- Passing `reasoning_effort="none"` or `reasoning_effort="low"` in the OpenAI SDK call to `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` caused an API error (unrecognized or rejected parameter).
- Prompt instructions such as *"Do NOT guess a cause"* or *"In exactly one sentence"* were frequently echoed into the output content or internalized as part of the thinking process.
- We implemented regex sanitization (`clean_fast_explanation`) to strip `<think>...</think>` tags and prompt echoes before storing explanations in the ledger.

---

## 4. Model Strengths & Surprises

### 1. Nemotron-3-Ultra-550B (Planner Role) — Exceptional
- **Strength**: Ultra-550B exhibited remarkable instruction following for JSON generation. Out of 13 planning and replanning calls, it produced valid, parseable JSON on the first attempt 100% of the time.
- **Dependency Planning**: It successfully grasped DAG step dependencies (`depends_on: [1, 2]`), allowing our ledger state machine to determine which downstream steps to supersede on reality drift.

### 2. nemotron-3-super-120b (Worker Role) — Fast & Reliable
- **Strength**: 2.4s average latency for generating comprehensive file contents and Python diagnostic scripts. It never hallucinated markdown code fences when instructed to produce raw content.

---

## 5. Concrete Suggestions for Nebius & NVIDIA

1. **Normalize Model Identifiers**: Support case-insensitive model lookups on the Token Factory routing proxy so `nvidia/nemotron-3-nano-30b-a3b` resolves successfully.
2. **Expose Thinking Toggle for Nano-30B**: Add a first-class parameter (e.g. `enable_thinking=false` or support for `reasoning_effort="none"`) to allow developers to use Nano-30B as a true sub-second fast classifier/auditor.
3. **SDK Streaming Documentation**: Clarify in Nebius Token Factory documentation whether streaming deltas expose or hide internal `<think>` blocks across different Nemotron models.
