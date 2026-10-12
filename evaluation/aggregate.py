"""
Aggregate ResolveAI benchmark results into summary.json and results.csv.

Usage: python aggregate.py <results_dir>   (contains paced/ and unpaced/)
"""

import csv
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
BENCH = json.loads((Path(__file__).resolve().parent / "benchmark.json").read_text(encoding="utf-8"))["items"]

# Diagnostic only: the policy labels rewritten to the knowledge-base headings
# they were evidently meant to match (confidence.py uses different strings).
CORRECTED_LABELS = {
    "Refund requests",
    "Enrollment changes",
    "Change your registered email address",
    "Restore a deleted account",
    "Account-specific investigation",
    "Duplicate charge",
}


def load(path):
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]


def stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    xs = sorted(xs)
    p95 = statistics.quantiles(xs, n=20, method="inclusive")[18] if len(xs) > 1 else xs[0]
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs),
        "median": statistics.median(xs),
        "p95": p95,
        "min": xs[0],
        "max": xs[-1],
        "stdev": statistics.stdev(xs) if len(xs) > 1 else 0.0,
    }


def pct(num, den):
    return {"num": num, "den": den, "rate": (num / den) if den else None}


def route(retrieved, labels, tau=1.0):
    if not retrieved:
        return "ESCALATE"
    if retrieved[0]["distance"] > tau:
        return "ESCALATE"
    if any(r["section"] in labels for r in retrieved):
        return "ESCALATE"
    return "ANSWER"


def routing_metrics(records, decision_key="decision"):
    tp = sum(r[decision_key] == "ESCALATE" and r["expected"] == "ESCALATE" for r in records)
    fp = sum(r[decision_key] == "ESCALATE" and r["expected"] == "ANSWER" for r in records)
    fn = sum(r[decision_key] == "ANSWER" and r["expected"] == "ESCALATE" for r in records)
    tn = sum(r[decision_key] == "ANSWER" and r["expected"] == "ANSWER" for r in records)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else None
    return {
        "accuracy": pct(tp + tn, len(records)),
        "confusion": {"esc_esc": tp, "ans_esc": fp, "esc_ans": fn, "ans_ans": tn,
                      "_legend": "<predicted>_<expected>"},
        "escalation_precision": prec,
        "escalation_recall": rec,
        "escalation_f1": f1,
    }


def summarize_run(dirpath):
    raw = load(dirpath / "raw_runs.jsonl")
    fb = load(dirpath / "fallback_runs.jsonl")
    env = json.loads((dirpath / "environment.json").read_text(encoding="utf-8"))
    # This in-process probe was invalidated by the request stub (fixed later in
    # run_benchmark.py); the standalone probe in webhook_probe.json is used instead.
    env.pop("unstubbed_webhook_probe", None)
    init = json.loads((dirpath / "init_times.json").read_text(encoding="utf-8"))
    runs = sorted({r["run"] for r in raw})
    run1 = [r for r in raw if r["run"] == runs[0]]

    # Determinism of retrieval + routing across repeated runs.
    sig = {}
    deterministic = True
    for r in raw:
        s = (r["decision"], tuple((x["section"], round(x["distance"], 6)) for x in r["retrieved"]))
        if r["id"] in sig and sig[r["id"]] != s:
            deterministic = False
        sig.setdefault(r["id"], s)

    for r in run1:
        r["decision_corrected_labels"] = route(r["retrieved"], CORRECTED_LABELS)

    out = {"environment": env, "runs": runs, "routing_deterministic_across_runs": deterministic}

    # ---------------- routing (deterministic: computed on one run)
    out["routing"] = {
        "all": routing_metrics(run1),
        "set_A_authors": routing_metrics([r for r in run1 if r["set"] == "A"]),
        "set_B_constructed": routing_metrics([r for r in run1 if r["set"] == "B"]),
        "by_category": {
            c: pct(sum(r["routing_correct"] for r in run1 if r["category"] == c),
                   sum(1 for r in run1 if r["category"] == c))
            for c in sorted({r["category"] for r in run1})
        },
        "errors": [
            {"id": r["id"], "query": r["query"], "expected": r["expected"], "decision": r["decision"],
             "top1": r["retrieved"][0]["section"], "d1": r["d1"], "reason": r["reason"]}
            for r in run1 if not r["routing_correct"]
        ],
        "diagnostic_corrected_labels": routing_metrics(run1, "decision_corrected_labels"),
        "escalation_reasons": dict(Counter(
            ("distance>tau" if "not sufficiently" in r["reason"] else
             "policy_label" if "requires human support" in r["reason"] else
             "no_results" if "No relevant" in r["reason"] else "answer")
            for r in run1)),
    }

    # ---------------- retrieval
    gold = [r for r in run1 if r["gold_section"]]
    out["retrieval"] = {
        "hit_at_1": pct(sum(r["hit_at_1"] for r in gold), len(gold)),
        "hit_at_3": pct(sum(r["hit_at_3"] for r in gold), len(gold)),
        "d1_in_scope": stats([r["d1"] for r in run1 if r["category"] in ("answerable", "human_required") and r["gold_section"]]),
        "d1_no_gold_evidence": stats([r["d1"] for r in run1 if not r["gold_section"]]),
    }

    # ---------------- resolution
    answerable = [r for r in raw if r["expected"] == "ANSWER"]
    not_answerable = [r for r in raw if r["expected"] == "ESCALATE"]
    correct_auto = [r for r in answerable if r["decision"] == "ANSWER" and r.get("answer_correct")]
    out["resolution"] = {
        "automated_answer_rate_all_queries": pct(sum(r["decision"] == "ANSWER" for r in raw), len(raw)),
        "correct_automated_resolution_of_answerable": pct(len(correct_auto), len(answerable)),
        "answerable_routed_to_answer": pct(sum(r["decision"] == "ANSWER" for r in answerable), len(answerable)),
        "unsafe_auto_answer_of_escalation_required": pct(
            sum(r["decision"] == "ANSWER" for r in not_answerable), len(not_answerable)),
        "unsafe_auto_answers_that_deferred_or_abstained_in_text": pct(
            sum(bool(r.get("abstained")) for r in not_answerable if r["decision"] == "ANSWER"),
            sum(r["decision"] == "ANSWER" for r in not_answerable)),
    }

    # ---------------- answer accuracy by generator
    acc = {}
    for gen in ("gemini", "knowledge_base_fallback"):
        rs = [r for r in answerable if r["decision"] == "ANSWER" and r["answer_source"] == gen]
        facts = [h for r in rs for h in r.get("key_fact_hits", [])]
        acc[gen] = {
            "answers": len(rs),
            "all_key_facts_present": pct(sum(bool(r.get("answer_correct")) for r in rs), len(rs)),
            "key_fact_recall": pct(sum(facts), len(facts)),
        }
    fb_rs = [r for r in fb if r["decision"] == "ANSWER"]
    acc["injected_failure_fallback"] = {
        "answers": len(fb_rs),
        "all_key_facts_present": pct(sum(bool(r.get("answer_correct")) for r in fb_rs), len(fb_rs)),
        "source_values": dict(Counter(r["answer_source"] for r in fb_rs)),
    }
    out["answer_accuracy"] = acc

    # ---------------- generation availability
    gen_calls = [r for r in raw if r["decision"] == "ANSWER"]
    errs = [r.get("generation_error") for r in gen_calls if r.get("generation_error")]
    legacy_fallbacks = sum(r["answer_source"] == "knowledge_base_fallback" for r in gen_calls)
    out["generation_availability"] = {
        "generation_calls": len(gen_calls),
        "succeeded": sum(r["answer_source"] == "gemini" for r in gen_calls),
        "fell_back": legacy_fallbacks,
        "error_codes": dict(Counter(str(e.get("code")) for e in errs)) if errs else "not recorded in this run",
        "quota_ids": dict(Counter(str(e.get("quota_id")) for e in errs)) if errs else None,
    }

    # ---------------- latency
    out["latency_s"] = {
        "agent_init_first": init[0],
        "agent_init_subsequent": stats(init[1:]),
        "retrieval": stats([r["t_retrieval_s"] for r in raw]),
        "routing": stats([r["t_routing_s"] for r in raw]),
        "end_to_end_answer_gemini": stats([r["t_total_s"] for r in raw
                                           if r["decision"] == "ANSWER" and r["answer_source"] == "gemini"]),
        "generation_gemini": stats([r["t_generation_s"] for r in raw
                                    if r["decision"] == "ANSWER" and r["answer_source"] == "gemini"]),
        "end_to_end_answer_after_api_error": stats([r["t_total_s"] for r in raw
                                                    if r["decision"] == "ANSWER" and r["answer_source"] == "knowledge_base_fallback"]),
        "end_to_end_answer_injected_fallback": stats([r["t_total_s"] for r in fb_rs]),
        "end_to_end_escalate_to_ticket_payload": stats([r["t_to_ticket_s"] for r in raw if r["decision"] == "ESCALATE"]),
        "ticket_construction": stats([r["t_ticket_s"] for r in raw if r["decision"] == "ESCALATE"]),
    }
    return out, raw


def main():
    summary = {}
    all_rows = []
    for name in ("paced", "unpaced"):
        d = ROOT / name
        if (d / "raw_runs.jsonl").exists():
            s, raw = summarize_run(d)
            summary[name] = s
            for r in raw:
                all_rows.append({"mode": name, **r})
    # Gemini succeeded on only a few calls per mode (free-tier quota), so the
    # generator-specific answer metrics are also reported pooled over both modes.
    if all_rows:
        pooled = {}
        for gen in ("gemini", "knowledge_base_fallback"):
            rs = [r for r in all_rows if r["expected"] == "ANSWER" and r["decision"] == "ANSWER"
                  and r["answer_source"] == gen]
            facts = [h for r in rs for h in r.get("key_fact_hits", [])]
            pooled[gen] = {
                "answers": len(rs),
                "distinct_queries": len({r["id"] for r in rs}),
                "all_key_facts_present": pct(sum(bool(r.get("answer_correct")) for r in rs), len(rs)),
                "key_fact_recall": pct(sum(facts), len(facts)),
                "end_to_end_s": stats([r["t_total_s"] for r in rs]),
                "generation_s": stats([r["t_generation_s"] for r in rs]),
                "failures_by_id": sorted(r["id"] for r in rs if not r.get("answer_correct")),
            }
        not_ans = [r for r in all_rows if r["expected"] == "ESCALATE" and r["decision"] == "ANSWER"
                   and r["answer_source"] == "gemini"]
        pooled["gemini_on_escalation_required"] = {
            "answers": len(not_ans),
            "text_mentions_human_support_or_unavailability": sum(bool(r.get("abstained")) for r in not_ans),
            "ids": sorted(r["id"] for r in not_ans),
        }
        gen_calls = [r for r in all_rows if r["decision"] == "ANSWER"]
        pooled["generation_calls_total"] = len(gen_calls)
        pooled["generation_calls_succeeded"] = sum(r["answer_source"] == "gemini" for r in gen_calls)
        summary["pooled_answers_by_generator"] = pooled

    probe = ROOT / "webhook_probe.json"
    if probe.exists():
        summary["unstubbed_webhook_probe"] = json.loads(probe.read_text(encoding="utf-8"))
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    cols = ["mode", "run", "id", "set", "category", "query", "expected", "decision", "routing_correct",
            "answer_source", "answer_correct", "abstained", "d1", "hit_at_1", "hit_at_3",
            "t_total_s", "t_retrieval_s", "t_routing_s", "t_generation_s", "t_to_ticket_s"]
    with open(ROOT / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != "environment"}
                      if isinstance(v, dict) else v for k, v in summary.items()}, indent=1, default=str))


if __name__ == "__main__":
    main()
