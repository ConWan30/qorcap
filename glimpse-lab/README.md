# glimpse-lab (proposal)
A post-match, sandboxed, **observer-only** self-improvement loop for Elorian Glimpse, the QorCap shadow scorebug reader.
* **DESIGN.md**: design, diagram, integration with QorCap v0.3.27 / watcher 0.1.3, cost, rollout, risks.
* **NOVELTY.md**: prior-art review (17 searches) and what is, and is not, new.
* `glimpse_lab/`: labels (from hash-verified QorCap locks only), held-out bank, harness registry with rollback,
  McNemar gate, hash-linked lesson and spend ledgers, safety guards, JEV tie-breaker stub (off), reports.
* `replay.py`: replays past games in play order using cached Glimpse replies only (**$0, no network**).
  It needs the box's saved bundles and replies under /workspace.
  `python3 replay.py --scenario forward` (today's watcher) or `--scenario coldstart [--assume-approved] [--min-bank N]`.
* `tests/`: `python3 -m unittest tests.test_lab` (25 tests; 4 skip without the box data).
* `replay_out/`: replay results (reports, ledgers, bank, registry, stdout).
Nothing here is imported by QorCap; it changes no QorCap file and writes nowhere but its own folder.
