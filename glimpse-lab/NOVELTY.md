# Novelty review: glimpse-lab (2026-10-19, CDT)

Method: public web, GitHub, arXiv, Google Patents and WIPO results, all via web search. The 17 queries are listed
at the end. Searches can miss things, so "no prior art found" below means **found none in these 17 searches**. It
does not mean "first ever". Entries marked † are cited from the known literature and were not re-searched today.

## 1. Prior art and how close it is
### A. Self-improving harnesses and prompt optimization
| work | what it does | closeness to glimpse-lab |
|---|---|---|
| Harness² (google-research/harness2; arXiv 2610.14224) | Editor LLM rewrites agent harness files. Propose→Probe→Compose, blind selector, per-domain lesson library. | **Loop shape is borrowed directly** (INV/DOM, KEEP_BASE, ≤1 lesson, library). Not novel. |
| GEPA (github.com/gepa-ai/gepa), DSPy `dspy.GEPA`; OCR uses: Intrinsic Labs (+3.3–3.6 pts, https://www.intrinsic-labs.ai/research/ocr-gepa-v1.pdf), LanceDB handwritten OCR (54.1→59.4%) | Reflective prompt evolution on a metric, including multimodal OCR. | Very close for "optimize a VLM's instructions from failures". Uses supervised labels; nothing on authority-derived labels or observer isolation. |
| ACE, Agentic Context Engineering (https://arxiv.org/abs/2510.04618, github.com/ace-agent/ace) | Generator/Reflector/Curator; incremental "playbook" deltas with helpful/harmful counters; can learn from execution feedback without labels. | Close to the lesson library (incremental, counted lessons). No held-out promotion gate, no evidence binding. |
| Continual Harness (https://arxiv.org/abs/2605.09998) | Reset-free self-refinement of prompt, sub-agents, skills and memory in Pokémon from frames. | Close: a game, frames in, harness self-edits. No independent labels; no authority/observer split. |
| OPRO† (arXiv 2309.03409), TextGrad† (2406.07496), MIPRO† (2406.11695), Reflexion† (2303.11366), DSPy† (2310.03714) | LLM-as-optimizer, textual gradients, instruction+demo search, verbal self-reflection memory. | General techniques this design could use. Not novel. |
| JEV-Harness² (@openmys, https://x.com/openmys/status/2112024812751294954) | Teaser: cue→action lessons, confidence-gated emit/review, dev/held-out split. No repo. | Close in spirit (confidence routing, lessons). No published mechanism to compare against. |

### B. Game HUD / scorebug OCR and esports stat extraction
| work | what it does | closeness |
|---|---|---|
| Overwolf GEP (https://dev.overwolf.com/ow-electron/reference/live-game-data-gep/live-game-data-gep-intro/), Mobalytics | Live game events from game data; some titles use OCR (GEP changelog: Lethal Company "new OCR solution"). | Stat extraction, no self-improvement loop or verification chain. |
| Sunday Clipboard (https://www.sundayclipboard.com/) | Madden box-score screenshot OCR with human confirm and provenance; hand edits outrank OCR. | Close on "human decides, OCR advises" for Madden. No learning loop. |
| Scorebox localization (https://doi.org/10.1117/12.2575834), SSD scoreboard events (https://doi.org/10.1109/isai-nlp48611.2019.9045280), MediaEval 2023 (https://ceur-ws.org/Vol-3658/paper33.pdf), YOLO+CNN score OCR (IRJET 2024) | Classical/deep scoreboard OCR. Rule-based score deltas used for event detection. | We use the same legality idea (score deltas). These works don't use it as a label-trust filter for learning. |
| ASAP (https://arxiv.org/abs/2301.06866), basketball temporal grounding (https://arxiv.org/html/2411.00862) | OCR the scoreboard, then align play-by-play to frames to get **pseudo-labels at near-zero cost**. | **Close on "free labels from game state aligned in time"**. The direction is reversed: it trusts the OCR to align external labels. No cryptographic source, no observer gating. |
| Distantly supervised sports text (https://arxiv.org/abs/2111.00629) | Distant supervision for broadcast text recognition. | Close on weak supervision. No authority or receipts. |
| CS2-HUD-OCR-Crops (https://huggingface.co/datasets/ybashir/CS2-HUD-OCR-Crops) | HUD crops labelled from the game's **demo file** state (`dem_verified`), used to train a specialist OCR. Split by round to avoid leakage. | **Closest on "labels from an independent game-state record"**. It is a one-off dataset for fine-tuning: no per-game loop, no hash-verified source, no promotion gate, no observer invariant. |
| Chimera (github.com/skkwowee/chimera, from search snippet) | VLM fine-tuned on screenshot→game-state pairs (CS2 HUD). | Learning from game state. Weights, not harness; no gating. |

### C. Anti-cheat and verified-result systems
| work | what it does | closeness |
|---|---|---|
| US11020657B1 / WO2022231588 (https://patents.google.com/patent/US11020657B1/en) | Screenshot/video OCR of result regions with masks, validated against user-entered data. **Validated submissions grow a library used to auto-create masks for new or updated games.** | **Closest patent.** It learns new layouts from *validated* results, and it is in the reporting/authority path. Our design keeps the learner out of the authority path, takes labels only from a hash-verified chain, and gates promotion on held-out evidence. |
| US5746656 (https://www.freepatentsonline.com/5746656.html) | Captures the final-score screen, runs character recognition, sends it to a tournament computer. | Old screen-OCR result reporting. |
| US11973883 / US12294660 (crypto-protected transaction records); US10537809 (validate and sign game data) | Signed session records; server derives results. | Close on signed results. No learning from them. |
| WO2020051517A1 (https://patents.google.com/patent/WO2020051517A1/en) | Trust models trained on labelled player history. | ML in integrity, different labels. |
| Braket AI referee (https://braket.gg/docs/developers/ai-referee) | Two players' videos, a vision model scores end screens/overlays, confirms at ≥80 confidence, else human dispute queue. | **The AI sits in the authority path**, which is the opposite of observer-only. No self-improvement. |
| PLLAY (computer-vision referee for Madden/CoD wagers), TrustBridge (AI screenshot + PSN cross-check), Wombo (manual photo) | AI or manual result verification. | Authority-path AI; no learning loop described. |
| QorTroller (github.com/ConWan30/QorTroller) | Con's own: signed, hash-chained controller-input records. | Same author's ecosystem. Input corroboration idea comes from here. |

### D. Promotion, holdout and provenance
| work | closeness |
|---|---|
| Champion/challenger, shadow deployment with human approval and rollback (DataRobot https://www.datarobot.com/blog/introducing-mlops-champion-challenger-models/, AWS ML Lens MLREL04-BP02) | Our gate/approval/rollback is standard practice. **Not novel.** |
| Reusable holdout / Thresholdout (Dwork et al., Science 2015, https://www.science.org/doi/10.1126/science.aaa9375) | Our bank look budget is a simplified relative. **Not novel.** |
| Sigstore model-transparency (https://github.com/sigstore/model-transparency), Atlas ML provenance (https://arxiv.org/html/2502.19567v2), llm-audit-trail (PyPI) | Signed models, transparency logs, hash-chained event logs. **The hash-chain primitive is not novel.** |

## 2. What is NOT novel in glimpse-lab (plainly)
* The Propose→Probe→Compare→Library loop (Harness², GEPA, ACE).
* Prompt optimization for OCR/VLMs (GEPA OCR work).
* Shadow mode, champion/challenger, human-approved promotion, rollback (MLOps).
* McNemar paired testing; held-out banks; look budgets (Dwork-style).
* Hash-chained logs and signing (Sigstore, audit-trail libraries).
* Labels from game state (CS2 demo labels, ASAP pseudo-labels, distant supervision).
* Score-delta legality rules (scoreboard event-detection papers; QorTime's own rules).
* On-device or per-user adaptation in general (personalization is widespread; I did not search it specifically).
* Layout-keyed learning from validated results: **US11020657B1 already does a form of this** (a mask library learned
  from validated submissions).

## 3. Which combination appears new (no prior art found in these 17 searches)
The candidates Con listed, checked one by one:
| candidate | verdict |
|---|---|
| (a) A vision observer that learns **only** from labels tied to **signed, hash-chained match records**, provably independent of itself (circularity guard) | **No prior art found.** Closest: CS2 dem labels (independent, not cryptographic, no loop) and US11020657B1 (validated, but the learner is the authority). |
| (b) Promotion gated by verifiable held-out evidence **recorded in an evidence-bound ledger that cites the receipt chain** | **No prior art found** for the binding (lesson → checkpoint hashes + replay digest). Gating alone is standard. *Note:* the evidence is recorded in the **lab's** ledger that *points at* the receipt chain. It is not written into receipts, which Con's rule forbids. |
| (c) A learning loop that **by construction cannot affect the scoring authority** (observer-only invariant: no authority imports, own write root, sha-pinned prompts need a human PR) | Shadow mode exists. A *self-improving* observer whose outputs are structurally barred from the verification path, in the esports-verification setting: **no prior art found**. Braket and PLLAY put the AI in the authority path. |
| (d) Lessons keyed by scoreboard layout and game, plus controller-input corroboration | Layout keying is partly anticipated by US11020657B1's per-game mask library. Using controller input to *down-grade* label intervals: **no prior art found**, but this is a small idea. |
| (e) Per-game learning on the player's own hardware | Not novel by itself. Only notable as part of the combination. |
**Claim, stated carefully:** the *combination* (a)+(b)+(c) is what we found no prior art for: an observer-only VLM
harness that self-improves only from authority-chain-derived labels, records every learning step in an evidence-bound
hash-linked ledger, and can't feed back into the authority. Each part has precedent. It's the combination we didn't find.

## 4. Mechanisms added where the search showed a gap
* **N1. Evidence-bound lesson ledger (implemented, `glimpse_lab/ledger.py`, `session.py`).**
  * The log is append-only and hash-linked (prior art exists for that part).
  * **New here:** each lesson cites `receipt_refs` (chain head, the exact checkpoint hashes its labels came from, receipt
    sha256) and a `replay_digest`, a SHA-256 over the (frame, label, champion reply, candidate reply) tuples behind the
    decision. Anyone with the public receipt and the cached replies can recompute the labels and the verdict.
    `ledger.verify()` detects tampering (tested).
* **N2. One-way harness attestation (designed; needs a watcher change Con must approve).**
  * Each `advisory.jsonl` record would carry `harness_sha256`, the `lesson_ledger_head` at promotion, and the
    `receipt_sha256` it observed.
  * The observer references the authority. The authority never references the observer, so verification and receipts
    stay byte-identical with or without the lab.
  * Signing would use a separate lab key, never the referee or install key.
* **N3. Lag-aware authority-interval labelling with a label-noise audit (implemented, `labels.py`, `games.label_audit`).**
  * Trusted intervals only between legal steps, with measured lag margins (45 s / 60 s before merged TD locks).
  * No bridging across illegal steps, and no open interval without a confirmed final.
  * Measured on the box: label error went from 22/115 (19%) to 0/68.
  * I found no published label-trust rule for weak supervision from a lagging, legality-constrained scorer.

## 5. Searches run (17), today
1. GEPA OCR optimization Intrinsic Labs Omni OCR benchmark
2. LanceDB GEPA handwritten medical notes OCR blog
3. Continual Harness Pokemon self-improving agent harness paper
4. ACE Agentic Context Engineering evolving contexts self-improving language models arXiv
5. esports match result verification screenshot AI OCR wager platform Madden score
6. Overwolf game events provider how stats are extracted OCR vs game API Mobalytics tracker
7. sports broadcast scoreboard OCR deep learning scorebug recognition paper
8. weakly supervised scoreboard OCR labels from play-by-play data alignment self-supervised game frames
9. patent machine learning model trained on labels derived from cryptographically signed game result records video game score verification
10. Google Patents video game screen OCR score verification tournament automatic result reporting computer vision patent
11. transparency log hash chain ML model version provenance signed model updates audit (Sigstore model signing, in-toto, SLSA for ML)
12. reusable holdout thresholdout adaptive data analysis Dwork Science 2015
13. self-supervised OCR training labels from game state memory RAM automatic labeling HUD digits video game
14. champion challenger model shadow mode deployment promotion human approval MLOps rollback
15. github self-improving vision language model prompt optimization game HUD scoreboard reading GEPA VLM OCR video game
16. US11020657B1 gaming competition reporting integrity OCR screenshot masks result regions machine learning retraining
17. anti-cheat verified match result console capture card video hash chain signed receipt computer vision referee

Also fetched: the CS2-HUD-OCR-Crops dataset card, the Braket AI-referee docs and the Chimera repo page (empty).
From the earlier research step: GitHub search "harness2 jev" returned 0 results.
**Not searched:** non-English patent offices beyond WIPO, paywalled journals, and USPTO full text directly.
**Not legal advice:** a patentability opinion needs a professional search.
