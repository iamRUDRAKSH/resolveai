# ResolveAI Evaluation Report

System under test: `github.com/Pushkar236/resolveai` at commit `aff7ccc` ("Complete CampusHelp end-to-end workflow"), unmodified.
Run date: 2026-10-11 (UTC). Harness: `run_benchmark.py`; aggregation: `aggregate.py`; benchmark: `benchmark.json`.
Machine-readable results: `results/summary.json`, `results/results.csv`, `results/{paced,unpaced}/raw_runs.jsonl`, `results/{paced,unpaced}/fallback_runs.jsonl`, `results/webhook_probe.json`.

## 1. What was measured, and how

| Item | Value |
|---|---|
| Environment | Windows 11 (10.0.26200), Intel 16-logical-core CPU, Python 3.11.6 |
| Packages (repo pins) | chromadb 1.5.9, sentence-transformers 6.1.0, google-genai 2.25.0, torch 2.14.0, transformers 5.17.0 |
| Index | 5 Markdown documents → 19 heading-based chunks; Chroma HNSW space `l2` (squared L2, verified from collection config) |
| Configuration | all-MiniLM-L6-v2, top-k = 3, τ = 1.0, generator `gemini-3.8-flash` |
| Benchmark | 35 single-turn queries: Set A = the 11 questions in the repo's `evaluation/questions.md`; Set B = 24 queries written for this evaluation (one paraphrase per KB section + 5 out-of-scope) |
| Expected labels | 18 ANSWER (self-service sections); 17 ESCALATE (10 human-required, 2 unclear, 5 out-of-scope) |
| Repetitions | 3 runs × 35 queries in each of two sessions = 210 executions; plus 2 × 18 injected-failure executions |
| Answer scoring | All gold key facts present (case-insensitive regex from the gold section text, e.g. `30 minutes`, `edit profile`) |

**Instrumentation and deviations (all disclosed):**
- **n8n stubbed.** No n8n webhook was available. In the ticket client, only `requests.post` was replaced by a local stub that records the payload and returns HTTP 200. The real `create_support_ticket` code still built the payload. Ticket storage, Slack notification, human review, form submission and Gmail delivery were **not exercised**.
- **Timers.** `Retriever.search`, `evaluate_evidence`, `generate_answer` and `create_support_ticket` were wrapped with `perf_counter` timers. Behaviour is unchanged.
- **Sessions.** The *unpaced* session sent Gemini requests back-to-back. The *paced* session left ≥15 s between Gemini requests. The pacing wait is excluded from all latencies.
- **Injected failure.** A separate experiment replaced the Gemini client with one that raises an exception, to exercise the extractive fallback deterministically.
- **Harness bug (fixed).** The first version of the stub patched the shared `requests` module, which invalidated the in-process "webhook reachable" probe. That field is removed from the summaries. The standalone probe in `results/webhook_probe.json` is used instead. No metric depended on it.

## 2. Results

### 2.1 Routing and retrieval (deterministic; identical in all 6 repetitions)

| Metric | All (35) | Set A (11) | Set B (24) |
|---|---|---|---|
| Routing accuracy | 25/35 (71.4%) | 9/11 (81.8%) | 16/24 (66.7%) |
| Escalation precision | 0.82 (9/11) | 1.00 | 0.71 |
| Escalation recall | 0.53 (9/17) | 0.67 | 0.45 |
| Answerable → ANSWER | 16/18 (88.9%) | 5/5 | 11/13 |

- Retrieval: hit@1 = 22/27 (81.5%) and hit@3 = 25/27 (92.6%), over the queries that have a gold section.
- Top-1 distance d₁: in-scope queries have mean 0.746 (range 0.379–1.019). Queries with no supporting section have mean 1.175 (range 0.987–1.919). The two ranges overlap around τ = 1.0.

**Routing errors (10):**

| ID | Query | Expected → Got | Cause |
|---|---|---|---|
| A08 | Please restore my deleted account. | ESC → ANS | Label mismatch (top-1 = "Restore a deleted account") |
| A09 | Change my registered email address. | ESC → ANS | Label mismatch (top-1 = "Change your registered email address", d₁ = 0.43) |
| B07 | Why has my account been suspended? | ESC → ANS | Label mismatch (top-1 = "Account-specific investigation") |
| B11 | Please transfer my enrollment to a different course. | ESC → ANS | Label mismatch (top-1 = "Enrollment changes") |
| B17 | I need to update the email address linked to my account. | ESC → ANS | Label mismatch |
| B19 | Can you bring back my account that I deleted last week? | ESC → ANS | Label mismatch |
| B15 | I want my money back for a course I purchased. | ESC → ANS | Retrieval miss: "Refund requests" not in top-3 |
| B21 | Can I download a certificate after finishing a course? | ESC → ANS | Out-of-scope query with d₁ = 0.987 < τ |
| B12 | Money was deducted from my bank but my purchase shows as failed. | ANS → ESC | "Duplicate charge" label matched at rank 3 |
| B13 | What details should I include when contacting support about a payment problem? | ANS → ESC | d₁ = 1.019 > τ |

**Root cause of the label mismatch.** `confidence.HUMAN_SUPPORT_SECTIONS` contains `Enrollment transfer/manual restore`, `Change registered email`, `Restore deleted account` and `Account-specific suspension/disabled investigation`. None of these equals a section heading produced by `chunker.py`. Only `Refund requests` and `Duplicate charge` match.

**Diagnostic (not the deployed system).** Re-running the same rule with labels set to the six actual headings gives 28/35 (80.0%) accuracy, recall 0.88 (15/17) and precision 0.75 (15/20). The new false escalations are A05, B16 and B18: "Change your registered email address" appears at rank 2–3 for display-name and notification queries.

### 2.2 Generation availability

Free-tier quota for `gemini-3.8-flash`: 5 requests/min and 20 requests/day per project.

| Session | Generation calls | Succeeded | 429 (quota) | 503 (high demand) |
|---|---|---|---|---|
| Unpaced | 72 | 7 | 60 (per-minute) | 5 |
| Paced | 72 | 5 | 63 (per-day) | 4 |
| **Total** | **144** | **12 (8.3%)** | 123 | 9 |

Every failed call was handled by ResolveAI's own extractive fallback (`source = knowledge_base_fallback`). Fallback answers are always reported separately and never counted as Gemini answers.

### 2.3 Response accuracy

| Generator | Answers (answerable queries) | All key facts present | Key-fact recall |
|---|---|---|---|
| Gemini (gemini-3.8-flash) | 10 (9 distinct queries) | 10/10 | 16/16 |
| Extractive fallback (after real API error) | 86 | 64/86 (74.4%) | 95/134 (70.9%) |
| Extractive fallback (injected failure) | 32 (2 × 16) | 24/32 (75.0%) | — |

- **Fallback failures.** All fallback failures came from four queries (B01, B02, B03, B06) where the gold section was not ranked first. The fallback returns only the top-1 passage. On B01 and B02, Gemini answered correctly by using the lower-ranked evidence.
- **Gemini answers checked by hand.** All 12 Gemini outputs, including two on escalation-required queries, were read in full (`raw_runs.jsonl`). None contained information that wasn't in the retrieved passages. This check was done by the evaluator (an AI assistant) and should be confirmed by the authors. Ten answers is too few to estimate general accuracy.
- **Gemini on escalation-required queries.** For B11 and B17, which were wrongly routed to ANSWER, Gemini told the user that human support was required. No ticket was created, so the human handoff did not happen.

### 2.4 Resolution rate

| Metric | Value |
|---|---|
| Queries closed automatically (routed to ANSWER) | 24/35 (68.6%) |
| Answerable queries routed to ANSWER | 16/18 (88.9%) |
| Answerable queries fully resolved by the extractive fallback (injected failure, deterministic) | 12/18 (66.7%) |
| Answerable queries fully resolved by Gemini | Not estimable: Gemini answered only 9 of the 16 routed queries (all correctly) |
| Escalation-required queries auto-answered instead of ticketed | 8/17 (47.1%) |

### 2.5 Latency (seconds; warm agent unless stated)

| Stage | n | Median | p95 / range |
|---|---|---|---|
| Retrieval (embed + Chroma query) | 210 | 0.041 | p95 0.057 |
| Routing rule | 210 | <0.001 | — |
| End-to-end, Gemini answer | 12 | 5.65 | mean 9.60, range 2.94–31.72 |
| End-to-end, fallback after API error | 132 | 0.38 | p95 0.96 |
| End-to-end, fallback after injected failure | 32 | 0.040 | — |
| **Escalation latency:** query → ticket payload handed to the n8n client | 66 | 0.041 | p95 0.052, max 0.087 |
| `SupportAgent()` construction (model load + Chroma) | 8 | 4.93 | range 4.88–6.27 (first in process ≈ 6.1–6.3) |
| Unstubbed webhook call, n8n not running | 1 | 4.12 → `ConnectionError` | — |

- **Per-request startup cost.** `app.py` constructs a new `SupportAgent` on every Streamlit submission, so each interactive request also pays the ≈5 s initialization.
- **What happens without n8n.** If n8n is unreachable, `create_support_ticket` raises, `process_query` propagates the exception, and the Streamlit UI shows an error. No ticket is recorded.

### 2.6 Not measured

- n8n webhook and row-insert latency.
- Slack notification delivery.
- Human review time.
- Gmail delivery latency and success rate.
- Persistence of `sent_at` / `email_message_id`.
- Behaviour under concurrent load.
- Cost.
- User satisfaction.
- Multi-turn conversations.
- Performance of any other generator.

## 3. Threats to validity

- **Small benchmark.** 35 queries over a 5-document corpus. Set B and all key-fact patterns were written by the evaluator from the KB text.
- **Gemini sample is small and biased.** Only 12 Gemini answers, limited by the free-tier quota. They are not a random sample of the benchmark, since Gemini succeeded on the earliest calls in each window.
- **Network-dependent latency.** Gemini latency depends on network conditions and provider load at run time (2026-10-11, about 19:00 UTC).
- **Key-fact matching is coarse.** It checks that required content is present. It does not verify complete faithfulness or the absence of harmful additions.

## 4. Reproduction

From the repository root:

```bash
uv venv .venv --python 3.11 && uv pip install --python .venv/Scripts/python.exe -r evaluation/requirements_utf8.txt
echo GEMINI_API_KEY=... > .env && .venv/Scripts/python.exe ingest.py
.venv/Scripts/python.exe evaluation/run_benchmark.py . evaluation/results/paced 3 15
.venv/Scripts/python.exe evaluation/aggregate.py evaluation/results
```
Arguments to `run_benchmark.py`: repo path, output directory, runs per query (default 3), seconds of pacing between Gemini calls (default 0). The `unpaced` results were produced with pacing 0.
(`requirements.txt` is UTF-16 encoded, so `evaluation/requirements_utf8.txt` is a UTF-8 copy for uv.)
