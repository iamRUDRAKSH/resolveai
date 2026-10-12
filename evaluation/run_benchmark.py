"""
ResolveAI benchmark harness.

Runs the unmodified ResolveAI code (SupportAgent.process_query) from the
cloned repository against benchmark.json and writes raw and summary results.

Instrumentation only:
  * Retriever.search, evaluate_evidence, generate_answer and
    create_support_ticket are wrapped with timers (behaviour unchanged).
  * n8n_client.requests.post is replaced by a local stub because no n8n
    webhook was available. The real create_support_ticket code still builds
    the payload and calls raise_for_status(); only the HTTP transfer is skipped.
  * One separate experiment injects a generation exception to exercise the
    extractive fallback path.

  * Optional pacing: when <pace_s> > 0, the harness waits before each
    generation call so that consecutive Gemini requests start at least
    <pace_s> seconds apart (free-tier limit: 5 requests/min). The wait is
    excluded from all reported latencies.
  * Gemini exceptions are recorded (type, HTTP code, quota id) and re-raised
    so that ResolveAI's own fallback handles them unchanged.

Usage: python run_benchmark.py <repo_dir> <out_dir> [runs] [pace_s]
"""

import json
import os
import platform
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
PACE_S = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
BENCH = Path(__file__).resolve().parent / "benchmark.json"

OUT.mkdir(parents=True, exist_ok=True)
os.chdir(REPO)
sys.path.insert(0, str(REPO))

import agent as agent_mod  # noqa: E402
import confidence  # noqa: E402
import llm_service  # noqa: E402
import n8n_client  # noqa: E402

items = json.loads(BENCH.read_text(encoding="utf-8"))["items"]

# Words that indicate the generated text declined to answer from the KB.
ABSTAIN_RE = re.compile(
    r"not available in the knowledge base|does not (contain|include|provide|specify|mention)"
    r"|is not (available|specified|mentioned|provided)|no information|cannot (help|determine|answer)"
    r"|don't have (any )?information|requires? (human|support)|contact (human )?support",
    re.I,
)


def norm(text):
    return (text or "").replace("“", '"').replace("”", '"').replace("’", "'")


# ---------------------------------------------------------------- stubs/timers
captured_payloads = []


class _StubResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"stub": True, "note": "n8n webhook not available; payload captured locally"}


def _stub_post(url, json=None, timeout=None):
    captured_payloads.append({"url": url, "timeout": timeout, "payload": json})
    return _StubResponse()


import types  # noqa: E402

# Replace only n8n_client's reference to `requests`, not the shared module.
n8n_client.requests = types.SimpleNamespace(post=_stub_post)

timings = {}
_last_gen_start = [0.0]


def timed(name, fn):
    def wrapper(*args, **kwargs):
        if name == "generation" and PACE_S > 0:
            wait = PACE_S - (time.perf_counter() - _last_gen_start[0])
            if wait > 0:
                time.sleep(wait)
                timings["pace_wait_s"] = wait
            _last_gen_start[0] = time.perf_counter()
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            timings[name] = time.perf_counter() - t0
            if name == "ticket":
                timings["ticket_done_at"] = time.perf_counter()
    return wrapper


agent_mod.evaluate_evidence = timed("routing", confidence.evaluate_evidence)
agent_mod.generate_answer = timed("generation", llm_service.generate_answer)
agent_mod.create_support_ticket = timed("ticket", n8n_client.create_support_ticket)

_orig_generate_content = llm_service.client.models.generate_content


def _recording_generate_content(**kwargs):
    try:
        return _orig_generate_content(**kwargs)
    except Exception as e:
        msg = str(e)
        quota = re.search(r"'quotaId': '([^']+)'", msg)
        timings["generation_error"] = {
            "type": type(e).__name__,
            "code": getattr(e, "code", None),
            "quota_id": quota.group(1) if quota else None,
            "message": msg[:200],
        }
        raise


llm_service.client.models.generate_content = _recording_generate_content

# ---------------------------------------------------------------- environment
env = {
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "python": platform.python_version(),
    "platform": platform.platform(),
    "processor": platform.processor(),
    "cpu_count": os.cpu_count(),
    "gemini_model": llm_service.MODEL_NAME,
    "embedding_model": __import__("config").EMBEDDING_MODEL_NAME,
    "top_k": __import__("config").TOP_K,
    "tau": confidence.MAX_RELEVANT_DISTANCE,
    "human_support_labels": sorted(confidence.HUMAN_SUPPORT_SECTIONS),
    "n8n_webhook_url_in_code": n8n_client.N8N_WEBHOOK_URL,
    "n8n_mode": "stubbed (no webhook available)",
    "runs": RUNS,
    "pace_s": PACE_S,
}
try:
    import importlib.metadata as md
    env["packages"] = {p: md.version(p) for p in
                       ["chromadb", "sentence-transformers", "google-genai", "torch", "transformers"]}
except Exception as e:  # pragma: no cover
    env["packages_error"] = str(e)

# ---------------------------------------------------------------- cold start
init_times = []
for _ in range(4):
    t0 = time.perf_counter()
    sa = agent_mod.SupportAgent()
    init_times.append(time.perf_counter() - t0)
sa.retriever.search = timed("retrieval", sa.retriever.search)

try:
    coll = sa.retriever.collection
    env["chroma_collection_count"] = coll.count()
    cfg = getattr(coll, "configuration_json", None) or getattr(coll, "configuration", None)
    env["chroma_collection_configuration"] = json.loads(json.dumps(cfg, default=str))
    env["chroma_collection_metadata"] = coll.metadata
except Exception as e:
    env["chroma_config_error"] = str(e)

# Real (unstubbed) escalation behaviour with no n8n service running.
import requests as _real_requests  # noqa: E402

try:
    _real_requests.post(n8n_client.N8N_WEBHOOK_URL, json={}, timeout=10)
    env["unstubbed_webhook_probe"] = "reachable"
except Exception as e:
    env["unstubbed_webhook_probe"] = f"{type(e).__name__}"


# ---------------------------------------------------------------- main runs
def score_facts(answer, facts):
    text = norm(answer)
    hits = [bool(re.search(f, text, re.I)) for f in facts]
    return hits


raw = []
for run in range(1, RUNS + 1):
    for it in items:
        timings.clear()
        n_before = len(captured_payloads)
        t0 = time.perf_counter()
        res = sa.process_query(it["query"], user_email="benchmark@example.com")
        t_end = time.perf_counter() - timings.get("pace_wait_s", 0.0)
        rr = res["retrieved_results"]
        rec = {
            "run": run,
            "id": it["id"],
            "set": it["set"],
            "category": it["category"],
            "query": it["query"],
            "expected": it["expected"],
            "decision": res["decision"],
            "routing_correct": res["decision"] == it["expected"],
            "reason": res["reason"],
            "answer_source": res.get("source"),
            "answer": res.get("answer"),
            "ticket_id": res.get("ticket_id"),
            "retrieved": [
                {"source": r["source"], "section": r["section"], "distance": r["distance"]} for r in rr
            ],
            "d1": rr[0]["distance"] if rr else None,
            "gold_section": it["gold_section"],
            "hit_at_1": (rr[0]["section"] == it["gold_section"]) if (rr and it["gold_section"]) else None,
            "hit_at_3": (any(r["section"] == it["gold_section"] for r in rr)) if it["gold_section"] else None,
            "t_total_s": t_end - t0,
            "t_retrieval_s": timings.get("retrieval"),
            "t_routing_s": timings.get("routing"),
            "t_generation_s": timings.get("generation"),
            "t_ticket_s": timings.get("ticket"),
            "t_to_ticket_s": (timings["ticket_done_at"] - t0) if "ticket_done_at" in timings else None,
            "pace_wait_s": timings.get("pace_wait_s", 0.0),
            "generation_error": timings.get("generation_error"),
        }
        if res["decision"] == "ANSWER":
            hits = score_facts(res["answer"], it["key_facts"])
            rec["key_fact_hits"] = hits
            rec["answer_correct"] = all(hits) if it["key_facts"] else None
            rec["abstained"] = bool(ABSTAIN_RE.search(norm(res["answer"])))
        if len(captured_payloads) > n_before:
            rec["ticket_payload"] = captured_payloads[-1]["payload"]
        raw.append(rec)
        print(f"run {run} {it['id']} {res['decision']:<8} exp={it['expected']:<8} "
              f"src={res.get('source')} t={rec['t_total_s']:.2f}s", flush=True)

# ---------------------------------------------------------------- fallback experiment
_orig_client = llm_service.client


class _FailingModels:
    def generate_content(self, **kwargs):
        raise RuntimeError("injected generation failure (benchmark)")


class _FailingClient:
    models = _FailingModels()


llm_service.client = _FailingClient()
PACE_S = 0.0  # no API call is made in this experiment
fallback = []
for it in items:
    if it["expected"] != "ANSWER":
        continue
    timings.clear()
    t0 = time.perf_counter()
    res = sa.process_query(it["query"], user_email="benchmark@example.com")
    t = time.perf_counter() - t0
    rec = {"id": it["id"], "decision": res["decision"], "answer_source": res.get("source"),
           "t_total_s": t, "t_generation_s": timings.get("generation")}
    if res["decision"] == "ANSWER":
        hits = score_facts(res["answer"], it["key_facts"])
        rec["key_fact_hits"] = hits
        rec["answer_correct"] = all(hits)
        rec["answer"] = res["answer"]
    fallback.append(rec)
llm_service.client = _orig_client

# ---------------------------------------------------------------- write
(OUT / "environment.json").write_text(json.dumps(env, indent=2, default=str), encoding="utf-8")
(OUT / "init_times.json").write_text(json.dumps(init_times, indent=2), encoding="utf-8")
with open(OUT / "raw_runs.jsonl", "w", encoding="utf-8") as f:
    for r in raw:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
with open(OUT / "fallback_runs.jsonl", "w", encoding="utf-8") as f:
    for r in fallback:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("done", len(raw), "records;", len(fallback), "fallback records")
