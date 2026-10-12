# V. EVALUATION

> Text of Section V as it appears in `ResolveAI_Research_Paper_Evaluated.pdf`.

## A. Experimental Setup

The unmodified ResolveAI implementation (repository commit aff7ccc) was evaluated on a Windows 11 workstation with a 16-thread Intel CPU, Python 3.11.6, chromadb 1.5.9, sentence-transformers 6.1.0, and google-genai 2.25.0, using the configured gemini-3.8-flash model, top-k = 3, and τ = 1.0. The benchmark contains 35 single-turn queries: the 11 questions in the project's evaluation/questions.md (Set A) and 24 queries constructed for this study (Set B), comprising one paraphrase for each of the 19 knowledge-base sections and five out-of-scope requests. Expected decisions follow the knowledge-base text: 18 queries are answerable from self-service sections, and 17 require escalation (10 human-required, 2 unclear, and 5 out-of-scope). An answer is counted as accurate when it contains every gold key fact of its section (for example, "30 minutes" or "Edit Profile"), matched case-insensitively. Each query was executed three times in each of two sessions (210 executions). Because no n8n webhook was available, the HTTP call in the ticket client was replaced by a local stub that records the payload; ticket storage, Slack notification, human review, and Gmail delivery were therefore not exercised. Table I summarizes the measured results.

## B. Retrieval and Routing

Retrieval and routing were deterministic across all six repetitions. The gold section was ranked first for 22 of 27 queries with a gold section (hit@1 = 81.5%) and appeared in the top three for 25 (hit@3 = 92.6%). Routing was correct for 25 of 35 queries (71.4%; Set A 9/11, Set B 16/24). Sixteen of the 18 answerable queries were answered, but only 9 of the 17 escalation-required queries were escalated (escalation precision 0.82, recall 0.53).

Six of the eight missed escalations retrieved the governing policy section at rank one, but four of the six human-support labels in the routing module do not match the section headings produced by the chunker (for example, "Change registered email" versus "Change your registered email address"); only the refund and duplicate-charge labels match. The remaining misses were a refund request whose policy section was not retrieved and an out-of-scope query with d₁ = 0.987 < τ. The two false escalations reflect the risks noted in Section VI. A: a "Duplicate charge" passage at rank three, and d₁ = 1.019, just above τ. As a diagnostic only, applying the rule with labels equal to the six section headings raises routing accuracy to 28/35 (recall 0.88) but lowers precision to 0.75, because the registered-email section is also retrieved at ranks two and three for display-name and notification queries.

## C. Response Accuracy and Resolution Rate

The free-tier Gemini quota (5 requests per minute and 20 per day per model) and transient HTTP 503 responses limited successful generation to 12 of 144 calls (8.3%). All other answered executions used the extractive fallback, which is reported separately. All 10 Gemini answers to answerable queries (9 distinct queries) contained every key fact, and inspection of all 12 generated answers found no content beyond the retrieved passages; this sample is too small to estimate general accuracy. The extractive fallback contained all key facts in 64 of 86 answers (74.4%). Every fallback failure occurred when the gold section was not ranked first, since the fallback returns only the top passage; Gemini answered two of those queries correctly from lower-ranked evidence. With generation forced to fail, the fallback fully resolved 12 of the 18 answerable queries (66.7%).

Overall, 24 of 35 queries (68.6%) were closed automatically, including 8 of the 17 that required escalation. For two of these, Gemini told the user that human support was required, but no ticket was created.

## D. Response Time and Escalation Latency

Dense retrieval took a median of 0.041 s (p95 0.057 s), and the routing rule took less than 1 ms. End-to-end time for Gemini-generated answers had a median of 5.65 s (mean 9.60 s, range 2.94–31.72 s, n = 12), while fallback answers after an API error took a median of 0.38 s. Escalation latency, measured from query submission until the ticket payload was passed to the n8n client, had a median of 0.041 s (p95 0.052 s, n = 66). Constructing SupportAgent took 4.9–6.3 s; because the Streamlit interface constructs a new agent for each submission, this cost is added to every interactive request. With no n8n service running, the unstubbed webhook call failed with a connection error after 4.1 s, and the request was not ticketed.

## E. Validity of the Results

These results describe a 35-query benchmark over a five-document corpus, partly constructed by the evaluators, and a single generator operating under free-tier limits. They do not measure human review time, Slack or email delivery, concurrent load, cost, or user outcomes. Key-fact matching confirms required content but not complete faithfulness.

---

**TABLE I — EVALUATION RESULTS (35 QUERIES, 210 EXECUTIONS)**

| Metric | Result |
|---|---|
| Routing accuracy | 25/35 (71.4%) |
| Escalation precision / recall | 0.82 / 0.53 |
| Retrieval hit@1 / hit@3 | 81.5% / 92.6% |
| Queries closed automatically | 24/35 (68.6%) |
| Answerable resolved (fallback only) | 12/18 (66.7%) |
| Key-fact accuracy, Gemini | 10/10 answers |
| Key-fact accuracy, fallback | 64/86 (74.4%) |
| Successful Gemini calls | 12/144 (8.3%) |
| Median retrieval time | 0.041 s |
| Median response time, Gemini | 5.65 s (n = 12) |
| Median escalation latency† | 0.041 s |
| Agent initialization per request | 4.9–6.3 s |
| n8n, Slack, and Gmail latency | Not measured |

† Query submission to ticket payload; n8n delivery stubbed.
