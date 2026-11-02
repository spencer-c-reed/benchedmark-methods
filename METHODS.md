# Methods: what BenchedMark measures

Release date: November 2026. This is a methods-only snapshot of a running pilot, not a report of findings or a claim of certification. It describes the current public methodology and the relevant internal design and dated registration notes. Proposed checks below are requirements for future claims, not completed validation.

## The game

Managers play fantasy football on the real NFL season. The basic windowed game has ten teams, half a point per reception, a snake draft, a $100 season waiver budget with sealed bids, trade windows with free-text messages, and lineups that lock at each player's own kickoff. The YAML rules specify the scoring, roster limits, trading limits and season format. Full-game variants can use a twelve-team head-to-head format and a shared waiver/thinking wallet; the finals format has its own rules file. Read each format's rules rather than assuming all leagues are identical.

Windowed managers receive a briefing, optional research tools and an action schema. The original template asks for private notes and persistent memory. The replacement asks for a one-to-three-sentence decision memo and a notebook of facts and plans. Other managers do not see these records during play. Some disclosure arms are told the research team may publish them later. Full-game agents use the block-based prompt registry and schedule their own wakes within a budget. These are different instruments and are kept distinct in analysis.

## Four separate dimensions

Capability covers decision quality, forecasting, planning and negotiation. Efficiency records tokens and dollars at public list prices. Agentic ability concerns research, scheduling, memory and recovery at a matched thinking budget. Alignment concerns honesty and conduct, checked against facts available to the sender and its contemporaneous record. Speed is logged but is not a scored dimension. There is no single score combining the four dimensions.

The fantasy score is a deterministic sum of the scoring weights applied to the relevant player and team stat lines. Defense points allowed use the opponent's whole final score under the current rules. `bm/engine/scoring.py` implements these calculations. Missing fields in settled scoring are treated as zero; `bm/score/provisional.py` has stricter completeness checks for partial feeds. A partial feed must not be mistaken for a settled week.

The scorecard includes a descriptive diagnostic: the points a manager's decisions added relative to the Projection Bot policy from the same positions. Draft, waiver, trade and lineup components are evaluated sequentially on the roster produced by earlier steps. This is an approximation: other teams' actions are held fixed, and the counterfactual draft uses available players at the actual draft slots. The baseline had known weaknesses during Weeks 5 and 6. This diagnostic is not a certified comparison or a public headline.

Forecasts are committed before market tools unlock. The forecast module includes probability losses (Brier and log loss), quantile/distribution scores, calibration summaries, skill against a paired baseline, and resampling intervals. Copying the comparator yields no skill against it. Completeness, comparator provenance and availability matter: an absent or stale comparator cannot support an edge claim. Picks use moneylines, spreads and totals, with independent instances pooled for analysis. The code includes odds conversion, no-vig probabilities and closing-line comparisons. A last captured pre-kickoff quote is not automatically a certified closing line. Picks use paper bankrolls only.

Negotiation measures distinguish proposals, responses, messages, defaults and no-replies. A negative projected value spread is not by itself evidence of irrationality or deception. Agentic and conduct code contains diagnostic/composite calculations; implementation alone does not establish that the underlying opportunities, labels or components have been validated.

## Own decisions and refusals

The denominator includes every in-season decision a seat was expected to make, including decisions missed before lock and scored weeks whose lineups the engine set. The numerator includes answers actually made by the requested model. Forced defaults and engine-set lineups count against the own-decision share. A manager that considered its lineup and kept it made a decision. An answer served by another model counts for neither model. Older infrastructure-default exclusions and served-model corrections must be disclosed rather than silently reassigned.

Operational failures, such as a provider timeout, are retried until the real lock. They are identified separately from model refusals or unusable answers. A persistent refusal is reported as behavior, not dropped from reliability. A missed or infrastructure-filled action does not become evidence of the model's skill. Under the standing-lineup policy, the manager's last own lineup stands; the engine does not replace it with its preferred lineup.

`bm/score/validity.py` checks the lock time, absence of forced default, rendered-prompt and template hashes, a matching answering gateway call, observed served-model evidence and misserved annotations. Unknown lock or identity evidence is not a passing check. The coverage, refusals, research-use and scorecard modules expose related denominators and failure classifications. These are separate questions: a logged answer can exist without qualifying as valid or as the requested model's own timely answer.

## Certification before comparisons

The public method requires at least 98% own decisions, intervals across leagues, and robustness to meaning-preserving rewrites and option-order changes before a model comparison is published. The design specifies a frozen-state panel with lineup, waiver, trade-response and forecast states; canonical text, three meaning-preserving rewrites, shuffled options and list-format briefings; and repeated samples to distinguish prompt sensitivity from resampling noise. Its proposed gates are variant plus model-by-variant variance below 25% of between-model variance and Kendall agreement of at least 0.8 across the wording variants. Position bias above ten percentage points must be reported and addressed. This release contains no panel states or evidence that these checks have passed.

Human review and judge calibration are required for honesty claims. Automated checks only identify candidate cases. Claims in messages are checked against what the sender could see, contemporaneous memos and subsequent actions. The planned controlled P1 belief check asks for a stated belief in a fresh context; it must not be described as already completed. No honesty case or label set is released here.

## Record and limits

Rules and templates are versioned. League genesis records carry rules hashes; decision records carry template and rendered-prompt hashes; league logs are SHA-256 chained. Those private records are not part of this release. A current configured file is not proof of every historical byte served, and an internal hash freeze is not an independent public registration.

Draft slots rotate across leagues, and counterfactuals compare alternatives on the same state. All leagues still share one NFL season, so they are not independent worlds. Sparse counts need counts and uncertainty, not strong conclusions. CLI-mediated models also receive vendor instructions; harness and served-model uncertainty must accompany any eventual comparison. Fantasy football is one domain and does not establish general intelligence. Keeping a scenario private is not a guarantee that a model provider never trains on prompts it receives.

See [CHANGES.md](CHANGES.md) for dated deviations and [ANALYSIS-PLAN.md](ANALYSIS-PLAN.md) for the scope of this public registration.
