# BenchedMark: public methods, November 2026

Rules, prompt templates and scoring source for the live fantasy-football research pilot. **No experimental results or participant records are included.** The release is licensed under [Apache-2.0](LICENSE).

Start with [METHODS.md](METHODS.md), then [ANALYSIS-PLAN.md](ANALYSIS-PLAN.md). [CHANGES.md](CHANGES.md) dates the changes to the instruments. [PROVENANCE.md](PROVENANCE.md) explains the source snapshot, exclusions and limitations. [REGISTRATION.md](REGISTRATION.md) explains the public timestamp and its prospective scope.

## Files

- `rules/`: all four rules files identified in the current non-void league configurations, copied verbatim.
- `bm/agent/prompts.py` and `bm/agent/prompts_restart.py`: the two configured windowed-league prompt templates, verbatim. Supporting forecast-book, full-game and Picks templates are also included.
- `bm/score/`: every Python module in the deployed scoring directory. `bm/engine/scoring.py`: fantasy-point scoring. `bm/publish/boards.py`: board construction. Supporting Python imports are included.
- `config/`: scoring-relevant metadata and prompt variants. Lane metadata is **only** a lane-name/adapter mapping, not usable provider configuration. Entrant metadata contains display identifiers, not performance. Succession evidence and private operational fields are omitted.
- `SOURCE-MANIFEST.json`: deployed-source and released-file hashes for Python and rules, with transformations stated per file. `SHA256SUMS`: hashes of every released payload file, including the analysis plan (excluding itself).
- `synthetic_demo.py`: invented arithmetic inputs; not a reproduction of a league or evidence about any model.

## Check the code without private data

Python 3.12 or later is required. In a new virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
sha256sum -c SHA256SUMS
python3 -B synthetic_demo.py
python3 -B verify_release.py
```

`verify_release.py` imports the released modules, checks rule parsing and prompt identities, and evaluates invented scoring inputs. It does not call a model, fetch sports data, or read a live league. Nothing here is a frozen replay pack: league logs, rendered decisions, real stats snapshots, provider accounts and held-out scenarios are excluded. Historical and operational scorers require those separately supplied inputs; their presence does not establish certification or a complete runnable benchmark service. Do not run the agent/league operational entrypoints as a demo.

Public context: [methodology](https://benchedmark.com/methodology/) · [plan](https://benchedmark.com/plan/).
