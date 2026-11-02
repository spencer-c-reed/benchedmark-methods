"""Synthetic arithmetic examples; no real games, decisions, or results."""
import math
from bm.score.forecast import brier, log_score, skill
from bm.score.picks import american_to_decimal, no_vig_probs, clv_prob

assert math.isclose(brier(0.75, True), 0.0625)
assert math.isclose(log_score(0.75, True), -math.log(0.75))
assert math.isclose(skill([0.0625], [0.25]), 0.75)
assert math.isclose(american_to_decimal(-100), 2.0)
assert all(math.isclose(p, 0.5) for p in no_vig_probs([-110, -110]))
assert math.isclose(clv_prob(0.55, -100), 0.05)
print('Synthetic arithmetic examples passed: Brier, log loss, skill, odds, no-vig and CLV.')
