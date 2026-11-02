"""Read-only D-PROMPT-8 decision-agreement diagnostic; never calls a gateway.

python3 -m bm.score.rank_stability --out analysis/out/rank-stability.json

Estimator: balanced two-way random-effects ANOVA, method of moments:
  Y[m,v,s] = mu + model[m] + variant[v] + interaction[m,v] + residual[m,v,s]
  model = (MS_model - MS_interaction)/(V*N)
  variant = (MS_variant - MS_interaction)/(M*N)
  interaction = (MS_interaction - MS_residual)/N; residual = MS_residual.
See NIST's crossed layout and method-of-moments descriptions:
https://itl.nist.gov/div898/handbook/ppc/section2/ppc232.htm
https://itl.nist.gov/div898/handbook/prc/section4/prc44.htm

Each observation is agreement with that entrant/state's P0 sample 1. Only
sample 2 is scored for EVERY variant, including P0: scoring the reference
against itself would guarantee P0=1 and bias the variance and rankings.
Only states with valid reference AND scored answers in every model/variant
cell enter ANOVA. Missing/failed/undecided calls are not zeros. States are
replicates; state difficulty and resampling enter residual. Shared states,
binary scores, and few selected models/variants limit the random-effects
interpretation; no independence-based intervals or population inference.
Negative moment estimates are exposed raw and clipped at zero for the gate.
Variance share is (variant + interaction)/model, NOT share of total variance.
Tau is the minimum over all variant pairs (tau-b for ties; the existing
kendall_tau for strict rankings). Undefined tau or zero model variance cannot
certify a rank. P0/O1/F1 is diagnostic; ONLY P0/P1/P2/P3 is the wording gate.

This is a decision self-consistency ranking, not correctness, decision-value,
fantasy-points or leaderboard ability ranks. No decision values are present
in panel_call records. Downstream value ranks require their own scored panel.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path
from statistics import mean

from bm.paths import runtime_root
from bm import replay

ROOT = Path(__file__).resolve().parents[2]
WORDING = ("P0", "P1", "P2", "P3")
PRESENTATION = ("P0", "O1", "F1")
HISTORICAL = {"sonnet-5.5": (0.845, 0.769), "opus-5.5": (0.70, 0.64), "sol-6.1": (0.75, 0.70)}


def variance_components(cells: dict[str, dict[str, list[float]]]) -> dict:
    """Balanced crossed ANOVA; reject missing/unbalanced/nonfinite inputs."""
    models = sorted(cells)
    if len(models) < 2:
        raise ValueError("at least two models required")
    variants = sorted(cells[models[0]])
    if len(variants) < 2 or any(set(cells[m]) != set(variants) for m in models):
        raise ValueError("at least two variants and a complete crossed layout required")
    n = len(cells[models[0]][variants[0]])
    if n < 2 or any(len(cells[m][v]) != n for m in models for v in variants):
        raise ValueError("at least two balanced replicates per cell required")
    if any(not math.isfinite(x) for m in models for v in variants for x in cells[m][v]):
        raise ValueError("observations must be finite")
    a, b = len(models), len(variants)
    cm = {(m, v): mean(cells[m][v]) for m in models for v in variants}
    mm = {m: mean(cm[m, v] for v in variants) for m in models}
    vm = {v: mean(cm[m, v] for m in models) for v in variants}
    grand = mean(cm.values())
    ss = {
        "model": b*n*sum((mm[m]-grand)**2 for m in models),
        "variant": a*n*sum((vm[v]-grand)**2 for v in variants),
        "interaction": n*sum((cm[m, v]-mm[m]-vm[v]+grand)**2 for m in models for v in variants),
        "residual": sum((x-cm[m, v])**2 for m in models for v in variants for x in cells[m][v]),
    }
    df = {"model": a-1, "variant": b-1, "interaction": (a-1)*(b-1), "residual": a*b*(n-1)}
    ms = {k: ss[k]/df[k] for k in ss}
    raw = {"model": (ms["model"]-ms["interaction"])/(b*n),
           "variant": (ms["variant"]-ms["interaction"])/(a*n),
           "interaction": (ms["interaction"]-ms["residual"])/n, "residual": ms["residual"]}
    # Subtraction of equal mean squares can leave a tiny positive model
    # component and an astronomical ratio. Treat cancellation within 64 ULPs
    # of the calculation's scale as numerical zero, not evidence of separation.
    zero_tolerance = 64*math.ulp(max(abs(x) for x in [*raw.values(), *ms.values()]))
    clipped = {k: x if x > zero_tolerance else 0.0 for k, x in raw.items()}
    share = (clipped["variant"]+clipped["interaction"])/clipped["model"] if clipped["model"] > 0 else None
    return {"raw": raw, "components": clipped, "variance_share": share, "numerical_zero_tolerance": zero_tolerance,
            "mean_squares": ms, "degrees_of_freedom": df, "replicates_per_cell": n,
            "cell_means": {v: {m: cm[m, v] for m in models} for v in variants}}


def ranking_tau(a: dict[str, float], b: dict[str, float]) -> float | None:
    """Kendall tau-b; ties cannot be resolved by arbitrary entrant ID order."""
    if set(a) != set(b) or len(a) < 2:
        return None
    if len(set(a.values())) == len(a) and len(set(b.values())) == len(b):
        return replay.kendall_tau(sorted(a, key=a.get, reverse=True), sorted(b, key=b.get, reverse=True))
    concordant = discordant = tied_a = tied_b = 0
    for x, y in combinations(sorted(a), 2):
        da, db = a[x]-a[y], b[x]-b[y]
        if da == 0 and db == 0:
            continue
        if da == 0:
            tied_a += 1
        elif db == 0:
            tied_b += 1
        elif da*db > 0:
            concordant += 1
        else:
            discordant += 1
    denom = math.sqrt((concordant+discordant+tied_a)*(concordant+discordant+tied_b))
    return (concordant-discordant)/denom if denom else None


def panel_result(rows: list[dict], variants: tuple[str, ...]) -> dict:
    """Gate one explicitly matched model x variant x state panel."""
    usable = [r for r in rows if not r.get("error") and r.get("decision") is not None]
    models = sorted({r["entrant"] for r in rows})
    states = sorted({r["state"] for r in rows})
    by = {(r["entrant"], r["state"], r["variant"], r["sample"]): r["decision"] for r in usable}
    missing = {m: [v for v in variants if not any((m, s, v, 2) in by for s in states)] for m in models}
    matched = [s for s in states if all((m, s, "P0", 1) in by and (m, s, v, 2) in by
                                       for m in models for v in variants)]
    out = {"variants": list(variants), "entrants": models, "candidate_states": len(states),
           "matched_states": len(matched), "matched_state_ids": matched,
           "missing_scored_variants": {m: vs for m, vs in missing.items() if vs},
           "metric": "decision_agreement_to_own_P0_sample_1", "scored_sample": 2,
           "variance_share": None, "tau": None, "ranked": False, "label": "insufficient", "reasons": []}
    if len(models) < 2:
        out["reasons"].append("fewer_than_two_entrants")
    if len(matched) < 2:
        out["reasons"].append("fewer_than_two_complete_matched_states")
    if out["missing_scored_variants"]:
        out["reasons"].append("missing_scored_variants")
    if out["reasons"]:
        return out
    cells = {m: {v: [float(by[m, s, v, 2] == by[m, s, "P0", 1]) for s in matched] for v in variants} for m in models}
    result = variance_components(cells)
    out["anova"] = result
    out["variance_share"] = result["variance_share"]
    means = result["cell_means"]
    out["rankings"] = {v: sorted(models, key=lambda m: (-means[v][m], m)) for v in variants}
    out["rankings_note"] = "IDs order tied scores for display only; tau-b retains ties."
    pairs = {f"{a}:{b}": ranking_tau(means[a], means[b]) for a, b in combinations(variants, 2)}
    out["tau_pairs"] = pairs
    if any(x is None for x in pairs.values()):
        out["reasons"].append("undefined_tau_all_models_tied")
    else:
        out["tau"] = min(pairs.values())
    if result["variance_share"] is None:
        out["reasons"].append("zero_between_model_variance")
    if not out["reasons"]:
        out.update(replay.gate(out["variance_share"], out["tau"]))
    return out


def read_records(directory: Path) -> tuple[list[dict], list[dict]]:
    """Read snapshots directly (ChainLog's constructor can create directories).

    Latest attempt per logical cell wins, like replay.panel; a later failure
    remains missing rather than resurrecting an earlier success. Snapshot
    hashes and counts bind the report to the exact bytes that were consumed.
    """
    latest, sources = {}, []
    for path in sorted(directory.glob("panel-*-panel-v2.jsonl")):
        data = path.read_bytes()
        n = 0
        for line in data.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)["payload"]
            if row.get("kind") != "panel_call" or row.get("runner") != "panel-v2":
                raise ValueError(f"unexpected record type or runner in {path}")
            identity = (row["entrant"], row["state"], row["variant"], row["sample"])
            if row["sample"] < 1:
                raise ValueError(f"nonpositive sample in {path}")
            previous = latest.get(identity)
            if previous and previous["decision_kind"] != row["decision_kind"]:
                raise ValueError(f"conflicting decision kinds in {path}")
            latest[identity] = row
            n += 1
        sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data), "rows": n})
    return sorted(latest.values(), key=lambda r: (r["entrant"], r["state"], r["variant"], r["sample"])), sources


def report(rows: list[dict]) -> dict:
    entrants = sorted({r["entrant"] for r in rows} | set(HISTORICAL))
    per_entrant = {}
    for entrant in entrants:
        rs = [r for r in rows if r["entrant"] == entrant]
        valid = [r for r in rs if not r.get("error") and r.get("decision") is not None]
        first_two = [r for r in valid if r["sample"] in (1, 2) and r["variant"] in PRESENTATION]
        agreement = replay.measures(first_two)
        expected = HISTORICAL.get(entrant)
        observed = (agreement["within_variant_agreement"], agreement["across_variant_agreement_vs_P0"])
        per_entrant[entrant] = {
            "status": "observed" if valid else "insufficient", "retained_cells": len(rs),
            "scheduled_states": len({r["state"] for r in rs}), "usable_calls": len(valid),
            "errors": dict(sorted(Counter(r["error"] for r in rs if r.get("error")).items())),
            "undecided_without_error": sum(not r.get("error") and r.get("decision") is None for r in rs),
            "usable_by_variant": dict(sorted(Counter(r["variant"] for r in valid).items())),
            "agreement_samples_1_2": agreement,
            "agreement_first_two_valid_samples": replay.measures(valid),
            "wording_status": "observed" if all(any(r["variant"] == v for r in valid) for v in WORDING) else "insufficient",
        }
        if expected:
            per_entrant[entrant]["historical_comparison"] = {
                "expected_within": expected[0], "expected_across": expected[1],
                "matches": observed == expected,
                "explanation": ("Rounded agreements reproduce the historical report." if observed == expected else
                                "No retained panel-v2 file for this entrant; historical Sol cells are absent from this runtime root." if not rs else
                                "BUILD-STATUS is a historical 40-state partial-panel snapshot. Current retained cells have a different "
                                "state/sample population; this report uses valid samples 1/2, excludes failures and undecided answers, "
                                "and reports exact denominators. No historical state manifest is supplied to reconstruct that snapshot."),
            }
    panels = {}
    for kind in ["all", *sorted({r["decision_kind"] for r in rows})]:
        subset = rows if kind == "all" else [r for r in rows if r["decision_kind"] == kind]
        panels[kind] = {"wording": panel_result(subset, WORDING),
                        "presentation_diagnostic": panel_result(subset, PRESENTATION)}
    return {"schema": "rank-stability/1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "metric": "decision_agreement_to_own_P0_sample_1",
            "interpretation": "Self-consistency diagnostic, not decision value or leaderboard ability. "
                              "Only the wording panel applies D-PROMPT-8 to this metric; absent records remain insufficient.",
            "method": {"estimator": "balanced two-way random-effects ANOVA method of moments",
                       "negative_components": "clip at zero; retain raw estimates",
                       "variance_share": "(variant + model_x_variant) / between_model",
                       "tau": "minimum pairwise tau across all variants; tau-b preserves ties",
                       "reference": "own entrant/state P0 sample 1", "scored_sample": 2,
                       "matching": "same complete states for every entrant/variant",
                       "limitations": "Binary scores, shared states and selected factors; residual includes state and resampling variation. "
                                      "No confidence intervals, population inference or decision-value certification.",
                       "sources": ["https://itl.nist.gov/div898/handbook/ppc/section2/ppc232.htm",
                                   "https://itl.nist.gov/div898/handbook/prc/section4/prc44.htm"]},
            "entrants": per_entrant, "panels": panels}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-root", type=Path, default=runtime_root(ROOT)/"records"/"replay")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    out = args.out.resolve()
    protected = [args.records_root.resolve(), *(runtime_root(ROOT)/p for p in ("records", "leagues", "logs", "state", "export"))]
    if any(out.is_relative_to(p.resolve()) for p in protected):
        parser.error("output must be outside retained/runtime data directories")
    rows, sources = read_records(args.records_root)
    result = report(rows)
    result["records_root"] = str(args.records_root.resolve())
    result["sources"] = sources
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+"\n")
    for entrant, data in result["entrants"].items():
        a = data["agreement_samples_1_2"]
        print(f"{entrant}: within={a['within_variant_agreement']} (n={a['n_within']}) "
              f"across={a['across_variant_agreement_vs_P0']} (n={a['n_across']}) wording={data['wording_status']}")
    for kind, data in result["panels"].items():
        for name, p in data.items():
            print(f"{kind}/{name}: {p['label']} variance_share={p['variance_share']} tau={p['tau']} matched_states={p['matched_states']}")
    print(f"output={out}; retained_files={len(sources)}; latest_cells={len(rows)}; model_calls=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
