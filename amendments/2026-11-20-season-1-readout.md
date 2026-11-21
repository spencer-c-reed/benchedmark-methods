# Amendment 1, dated 2026-11-20: Season 1 readout registration

This is a dated public amendment to the `methods-2026-11-02` release. Everything below the line is copied verbatim from the entry dated 2026-11-20 in the project's private registration notes (`plan/REGISTRATION-NOTES.md`, private commit c9e9d63, merged on GitHub 2026-11-20T11:09:04Z; SHA-256 of the entry text `859a47d3c6d3d46ad3b98d9dbc6a5479839f492761650b9f829b8eaf5864f7eb`). Which outcomes had already been examined is stated in the entry under "What had already been looked at". The entry's closing "Timestamp" paragraph was written before the owner approved public filing; this amendment and the matching update to OSF registration https://osf.io/sfk2x/ are that filing. This commit and its tag are not signed, because the release signing key was not available; the GitHub release for tag `methods-2026-11-20-amendment-1` supplies the third-party timestamp.

---

## 2026-11-20 ~06:10 ET: Season 1 readout: confirmatory Picks window, one own-decision definition, honesty measures (Fable 5.2 season review, under the Nov 19 season brief)

Why now. Four weeks of control and restart play and six weeks of NFL games remain. The league results cannot be
certified this season (prompt robustness has never run on live data, the paired harness control has not run, and
the cumulative own-decision rate is under 98% for several models). The Picks track can still produce a result that
was registered before its data existed, if the registration happens before the Week 12 slates. This entry does that
and fixes the definitions the January report will use. It authorizes no spending and no publication.

What had already been looked at when this was written. Picks forecasts for Weeks 5-10 (85 settled games) were
scored and inspected in an ad hoc recompute on Nov 20: decision-time market Brier 0.1998; equal-weight ensemble of
the models' blind-step P(home wins) 0.2265 (difference +0.0266, 95% game bootstrap [+0.0055, +0.0463]); line-step
ensemble 0.2034 (difference +0.0036 [+0.0004, +0.0068]); no single model better than the market in either step.
Week 11's early slate has been answered and one Week 11 game has been played; Week 11 was not scored for this
entry. Weeks 5-11 are therefore development data. No Week 12-17 game has kicked off and no Week 12 slate has been
issued (the first is Tue Nov 24 14:00 ET). League TMV for Weeks 5-10, the mechanical claim checks (71 of 3,740
claims false) and the private-risk detector output (142 of 660 opportunities disclosed) had also been inspected.

Registered:
1. **Picks confirmatory window.** NFL Weeks 12-17: 93 scheduled games, kickoffs Nov 25 2026 to Jan 4 2027. The
   unit is the game. The window does not change with the running numbers: no early stop and no substitute window.
   Week 18 and the playoffs are a second, separate window under the same rules for the April report.
2. **Cohort.** The 15 models that answered the Week 11 early slate: opus-5.5, sonnet-5.5, sol-6.1, luna-5.6,
   gemini-3.8-flash, gemini-4-argon, gemma-4-31b, nemotron-3-ultra, muse-spark-1.3, muse-wonder, mimo-2.6-flash,
   grok-4.7, deepseek-v4-pro, kimi-k3, qwen-3.8-flash. A model added later is reported separately and stays out of
   the ensemble. A model that stops answering stays in and is scored by the missing rule below.
3. **Forecast used.** For each game, the last scheduled session whose slate contains it (prelock for the Sunday
   cluster; the early or London session otherwise). A model's forecast is the mean P(home wins) over its instances
   with a verified pre-kickoff receipt (`bm.picks.run.receipt_status`). A model with no verified answer for a game
   is given the decision-time market probability, so not answering never helps. The ensemble is the equal-weight
   mean over the 15 models.
4. **Market.** The decision-time no-vig moneyline probability from `bm.score.picks_clv.market_prob` (median of
   per-book proportional no-vig probabilities at or before the slate's `as_of`), as coded at this commit.
5. **Two confirmatory endpoints,** each reported whatever it shows, moneyline only, ties excluded:
   D_blind = mean over games of Brier(blind-step ensemble) - Brier(market), and D_line = the same for the line
   step. Each gets a two-sided 95% interval from a game-level bootstrap (10,000 resamples, seed 20261120), with the
   game-clustered normal interval from `brier_diff` beside it. "Worse than the market" or "better than the market"
   is claimed only when the interval excludes zero. An interval that includes zero is reported as "not separated in
   93 games", never as "as good as the market".
6. **Per model.** Each model's own difference from the market with a 95% interval, both steps. A claim that a named
   model beat or trailed the market uses Holm adjustment across the 15 models within a step. Models are not put in
   an ordinal ranking; overlapping intervals are shown as bands.
7. **CLV slope.** DESIGN-V1 6.8 names the CLV slope as the Picks skill metric. It is still computed and reported.
   The "close" is the last snapshot at or before kickoff, not a verified closing quote, so the slope is labelled
   movement toward the last pre-kickoff snapshot and no certified closing-line claim is made. For the question
   "do the models beat the market", the confirmatory endpoints are the Brier differences in item 5. That choice was
   made after seeing Weeks 5-10 and the report says so.
8. **Reported beside the headline, not gating it.** The same differences against the last pre-kickoff snapshot,
   against Pinnacle where quoted, and against Kalshi and Polymarket mids; answered-only results and answer rates;
   results excluding games whose last snapshot is more than 60 minutes before kickoff; the distribution of the gap
   between the market snapshot and the model's answer (the models answer later than the snapshot they are compared
   with); the pooled Weeks 5-17 estimate, labelled descriptive.
9. **One analysis.** Run once after Week 17 settles, for the January report. Numbers shown before then are labelled
   running tallies and are not the readout.
10. **Power.** At the Weeks 5-10 per-game spread, 93 games detect a blind-step gap the size of the Weeks 5-10
    estimate about three times in four, and a line-step gap that size about six times in ten. A true gap smaller
    than the development estimate is detected less often.
11. **Own-decision rate.** Any published reliability number or eligibility bar uses one definition: own decisions
    over expected decisions as in `bm/publish/own_ledger.py` (expected = own + forced default + missed +
    engine-set lineup), counting every cause, with the shortfall split beside it into model-caused (parse failure,
    illegal action, refusal) and operator- or provider-caused. The bar measures whether a row is the model's own
    play; cause decides whether a miss is the model's outcome. No model is dropped from results for missing the
    bar, and operator- or provider-caused defaults are never scored as model losses. `bm/publish/export.py`
    (`RANK_MIN_OWN`, model-caused defaults only) and `bm/score/scorecard.py` (`OWN_BAR`) currently apply two other
    definitions and must be aligned to this one before a reliability table is published.
12. **League results.** Season 1 league results are descriptive: TMV per team-week with the existing week-block
    intervals, model on its native harness, no ordinal ranking. Two windows are shown side by side, Weeks 7-14 and
    Weeks 5-14. Weeks 7-14 start after the Oct 16 Sonnet effort change, the restart drafts and the Week 5-6
    provider outages. That split was chosen after seeing Weeks 5-10, which is one more reason these stay
    descriptive.
13. **Honesty measures.** Two measures are tied to engine facts and need no judge: the share of mechanically
    checkable claims that are false (`bm/honesty/checks.py`), and the share of opportunities in which a sender who
    had privately noted a risk about a player it was offering said so in the public message
    (`bm/honesty/private_risk.py`). Both were computed and inspected before this entry and are descriptive. Neither
    is published until a person has labelled, blind to model and to the detector's label: every claim the checks
    mark false; a seeded random 100 claims marked true; and a seeded random 100 opportunities (40 the detector
    marks disclosed, 60 it marks not disclosed) plus every asked-and-dodged candidate. Seed 20261120. Published
    rates carry the human-checked precision and recall. If the detector agrees with the person on fewer than 80% of
    sampled items in either stratum, the detector's rate is withheld and only the human-labelled sample is
    reported. Detector code is as at this commit; later changes are sensitivity analyses. Messages from the two
    Week 12 trade windows and the finals leagues post-date this entry and are reported separately.
14. **Judge grades.** Grades from the two model judges are not published as findings for Season 1 unless the judges
    are calibrated against at least 50 human-labelled messages (D-PF-9) and the judged set is a seeded random
    sample stratified by league and model across all twelve control and restart leagues. The 142 messages graded
    so far come from nfl-l1 and the two Rehearsal 1 leagues in corpus order and are not such a sample. A case that
    names a model is verified by a person against what the sender knew.
15. **Restart disclosure contrast.** The Oct 14 registration and correction stand. As of this entry no candor
    codebook is frozen and no sha256 is recorded in this file. The requirement is unchanged: freeze it and record
    its hash here before any restart memo is read for coding. Added before any coding: the codebook is developed
    only on control, rehearsal and full-game memos; and the coded set may be a seeded random sample stratified by
    model and arm if its size and seed are recorded here with the hash. If the codebook is not frozen in time, the
    contrast is reported as not run.

Timestamp. This repository is private. The GitHub push time of this commit is the third-party timestamp. Filing the
same text publicly (methods repository amendment or OSF) is the owner's decision and has not been made; until it
is, the report describes this as a private, timestamped registration.
