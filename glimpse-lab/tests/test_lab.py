import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from glimpse_lab import chain as C, labels as L, bank as B, stats, ledger, safety, harness as H, loop, jev, budget

MNP = "/workspace/glimpse-shadow/in/match-20261017-162055/score_checkpoints.jsonl"
BOX = unittest.skipUnless(os.path.exists(MNP), "needs the box's saved bundles and cached replies")

class Chain(unittest.TestCase):
    @BOX
    def test_real_chain_verifies(self):
        ok, head, cps = C.verify_checkpoints(MNP)
        self.assertTrue(ok); self.assertEqual(len(cps), 100)
    @BOX
    def test_tampered_score_breaks_chain(self):
        rows = [json.loads(l) for l in open(MNP)]
        rows[5]["qorcap"]["score"]["home"] += 1
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write("\n".join(json.dumps(r) for r in rows))
        self.assertFalse(C.verify_checkpoints(fh.name)[0])
    def test_segments_split_on_reset(self):
        segs = C.segments([(0, 0, 0, "a"), (10, 0, 7, "b"), (20, 0, 0, "c")])
        self.assertEqual(len(segs), 2)

class Labels(unittest.TestCase):
    segs = [[(0, 0, 0, "h0"), (100_000, 7, 0, "h1"), (300_000, 7, 3, "h2")]]
    def lab(self, t, final=None):
        return L.label_frames([{"id": "x", "t": t}], self.segs, source="checkpoints", chain_verified=True,
                              final=final)["x"]
    def test_refuses_glimpse_as_label_source(self):
        with self.assertRaises(L.CircularLabel):
            L.label_frames([], [], source="glimpse_advisory", chain_verified=True)
    def test_stable_interval_is_labelled_and_independent(self):
        r = self.lab(150_000)
        self.assertEqual(r["label"], {"away": 7, "home": 0}); self.assertTrue(r["independent_of_glimpse"])
    def test_transition_margins(self):
        self.assertIsNone(self.lab(101_000)["label"])            # just after a lock
        self.assertIsNone(self.lab(299_000)["label"])            # just before the next lock
        self.assertIsNone(self.lab(60_000)["label"])             # 40 s before a +7 lock (merged TD margin)
    def test_open_interval_needs_confirmed_final(self):
        self.assertIsNone(self.lab(400_000)["label"])
        r = self.lab(400_000, final={"away": 7, "home": 3, "confirmed": "end_menu"})
        self.assertEqual(r["label"], {"away": 7, "home": 3}); self.assertEqual(r["grade"], "A")
    def test_gap_before_illegal_step_unlabelled(self):
        segs = C.segments([(0, 0, 0, "a"), (100_000, 0, 7, "b"), (900_000, 7, 14, "c")])
        r = L.label_frames([{"id": "x", "t": 500_000}], segs, source="checkpoints", chain_verified=True)["x"]
        self.assertIsNone(r["label"])

class Bank(unittest.TestCase):
    def test_assignment_deterministic_and_never_reassigned(self):
        d = tempfile.mkdtemp(); b = B.Bank(os.path.join(d, "bank.json"))
        frames = [{"id": f"f{i}", "layout": "primetime"} for i in range(200)]
        labs = {f["id"]: {"label": {"away": 0, "home": 0}, "grade": "B", "basis": "h"} for f in frames}
        bank, tune = b.add_game("m1", frames, labs)
        self.assertTrue(40 < len(bank) < 120)
        self.assertEqual(set(bank) & set(tune), set())
        bank2, tune2 = b.add_game("m1-again", frames, labs)      # same frames never move
        self.assertEqual(bank2, []); self.assertEqual(tune2, [])   # tuned frames can never enter the bank later
        self.assertEqual(B.assign("m1", "f3"), B.assign("m1", "f3"))
    def test_rotation_retires_oldest(self):
        d = tempfile.mkdtemp(); b = B.Bank(os.path.join(d, "bank.json"))
        old = B.CAP_PER_LAYOUT; B.CAP_PER_LAYOUT = 10
        try:
            for g in range(5):
                fr = [{"id": f"g{g}f{i}", "layout": "x"} for i in range(30)]
                b.add_game(f"g{g}", fr, {f["id"]: {"label": {"away": 0, "home": 0}, "grade": "B", "basis": ""} for f in fr})
            self.assertLessEqual(len(b.active("x")), 10)
            self.assertTrue(all(r["match"] in ("g3", "g4") for r in b.active("x").values()))
        finally:
            B.CAP_PER_LAYOUT = old

class Gate(unittest.TestCase):
    def test_mcnemar(self):
        self.assertAlmostEqual(stats.mcnemar_exact(5, 0), 1 / 32)
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)
    def _setup(self, cand_reads):
        d = tempfile.mkdtemp(); bank = B.Bank(os.path.join(d, "b.json"))
        frames = {f"f{i}": {"id": f"f{i}", "layout": "p", "lock_in_force": None} for i in range(200)}
        labs = {i: {"label": {"away": 7, "home": 0}, "grade": "B", "basis": "h"} for i in frames}
        bank.add_game("m", [{"id": i, "layout": "p"} for i in frames], labs)
        ok = {"region": "SCOREBUG", "away": 7, "home": 0, "valid": True}
        pool = {"champ": {i: ok for i in frames}, "cand": {i: cand_reads(i, ok) for i in frames}}
        return bank, frames, labs, pool
    def test_new_wrong_blocks_promotion(self):
        bank, frames, labs, pool = self._setup(lambda i, ok: dict(ok, away=1) if i == "f3" else ok)
        if "f3" not in bank.active():
            pool["cand"] = {i: (dict(r, away=1) if i == next(iter(bank.active())) else r) for i, r in pool["cand"].items()}
        g = loop.bank_gate(H.make("champ"), H.make("cand"), bank, frames, labs, pool, ["p"])
        self.assertEqual(g["verdict"], "FAIL_NEW_WRONG")
    def test_registry_never_auto_promotes_and_rolls_back(self):
        d = tempfile.mkdtemp(); r = H.Registry(os.path.join(d, "r.json"), H.make("a"))
        c = H.make("b"); r.propose_pending(c, {})
        self.assertEqual(H.name(r.champion), "a")                 # pending is not champion
        r.approve(c["sha256"], "Con"); self.assertEqual(H.name(r.champion), "b")
        r.rollback(); self.assertEqual(H.name(r.champion), "a")

class Ledger(unittest.TestCase):
    def test_hash_chain_and_tamper(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "l.jsonl"); lg = ledger.HashLedger(p)
        for k in range(3):
            lg.append("lesson", {"k": k, "receipt_refs": {"chain_head": "ab" * 32}})
        self.assertEqual(ledger.verify(p), (True, 3))
        rows = open(p).read().splitlines(); rows[1] = rows[1].replace('"k": 1', '"k": 9')
        open(p, "w").write("\n".join(rows) + "\n")
        self.assertFalse(ledger.verify(p)[0])

class Safety(unittest.TestCase):
    def test_disk_floor(self):
        with self.assertRaises(safety.Refused):
            safety.check_disk("/", need=10**18)
    def test_refuses_while_recording(self):
        d = tempfile.mkdtemp(); os.makedirs(os.path.join(d, ".qorcap-run"))
        open(os.path.join(d, ".qorcap-run", "recording.lock"), "w").close()
        with self.assertRaises(safety.Refused):
            safety.check_not_recording(d)
    def test_write_scope(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(safety.Refused):
            safety.check_write("/tmp/elsewhere.json", d)
        safety.check_write(os.path.join(d, "ok.json"), d)
    def test_live_budget_refuses(self):
        d = tempfile.mkdtemp(); b = budget.Budget(ledger.HashLedger(os.path.join(d, "s.jsonl")), mode="live")
        with self.assertRaises(budget.BudgetStop):
            b.authorise(1, "watch_012")
        with self.assertRaises(budget.BudgetStop):
            b.authorise(10_000, "watch_012")                     # 10k calls would cross the game cap

class Ceiling(unittest.TestCase):
    def test_lab_yields_to_watcher_at_7(self):
        d = tempfile.mkdtemp(); lg = ledger.HashLedger(os.path.join(d, "s.jsonl"))
        b = budget.Budget(lg, mode="live", week_spent_external=6.999)
        with self.assertRaises(budget.BudgetStop) as cm:
            b.authorise(1, "watch_012")
        self.assertIn("cap", str(cm.exception))

class ObserverOnly(unittest.TestCase):
    SRC = sorted((ROOT / "glimpse_lab").glob("*.py")) + [ROOT / "replay.py"]
    def test_no_network_no_qorcap_scoring_imports(self):
        bad = re.compile(r"^\s*(import|from)\s+(urllib|requests|socket|http\.client|qorcap_|qortime|qor_receipt)", re.M)
        for p in self.SRC:
            self.assertIsNone(bad.search(p.read_text()), p.name)
    def test_never_mentions_api_key_env(self):
        for p in self.SRC:
            self.assertNotIn("OPENROUTER_API_KEY", p.read_text(), p.name)

class Jev(unittest.TestCase):
    def test_uncalibrated_is_off(self):
        self.assertIsNone(jev.JevTieBreaker(ask=lambda i, o: {o[0]: 1.0}).tiebreak(None, ["7-3", "1-3"]))
    def test_never_mints_digits(self):
        t = jev.JevTieBreaker(ask=lambda i, o: {"9-9": 0.99}); t.threshold = 0.5
        with self.assertRaises(ValueError):
            t.tiebreak(None, ["7-3", "1-3"])
    def test_calibration_needs_labels(self):
        with self.assertRaises(jev.NotCalibrated):
            jev.JevTieBreaker().calibrate([({"a": 0.9, "b": 0.1}, "a")] * 10)

class Replay(unittest.TestCase):
    @BOX
    def test_forward_replay_is_free_and_auditable(self):
        out = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        r = subprocess.run([sys.executable, str(ROOT / "replay.py"), "--scenario", "forward", "--out", out],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("lesson ledger verified=True", r.stdout)
        summ = json.load(open(os.path.join(out, "summary.json")))
        self.assertEqual(sum(g["spend_usd"] for g in summ), 0.0)
        reg = json.load(open(os.path.join(out, "harness_registry.json")))
        self.assertEqual(reg["history"], [reg["champion"]])          # nothing promoted without approval
        for line in open(os.path.join(out, "spend_ledger.jsonl")):
            self.assertEqual(json.loads(line)["body"]["usd"], 0.0)

if __name__ == "__main__":
    unittest.main()
