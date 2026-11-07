"""Spend control. Live mode books worst case before each call and stops at the cap; replay mode is $0."""
GAME_CAP_USD = 0.25
WEEK_CAP_USD = 10.00          # shared with the live watcher and everything else on the key
LAB_WEEK_CEILING_USD = 7.00   # the lab stops here so the live watcher always keeps >= $3 of the shared week
WORST_PER_CALL = 0.0020       # watcher's own worst-case booking per crop call
MEAN_PER_CALL = {"watch_012": 0.000948, "crop_v1": 0.000816, "crop_v0": 0.000381, "full_v0": 0.000510}

class BudgetStop(Exception):
    pass

class Budget:
    def __init__(self, ledger, mode="replay", week_spent_external=0.0):
        self.ledger, self.mode, self.week_external = ledger, mode, week_spent_external
        self.game_spent = 0.0
        self.would_cost = 0.0

    def week_spent(self):
        own = sum(r["body"].get("usd", 0) for r in self.ledger.rows if r["kind"] == "spend")
        return max(own, self.week_external)

    def authorise(self, n_calls, harness):
        """Live: refuse if the worst case would cross a cap. Replay: record what it would have cost."""
        est = n_calls * MEAN_PER_CALL.get(harness, WORST_PER_CALL)
        if self.mode == "replay":
            self.would_cost += est
            return 0.0
        worst = n_calls * WORST_PER_CALL
        if (self.game_spent + worst > GAME_CAP_USD or self.week_spent() + worst > LAB_WEEK_CEILING_USD
                or self.week_spent() + worst > WEEK_CAP_USD):
            raise BudgetStop(f"cap: game ${self.game_spent:.3f}+{worst:.3f} / week ${self.week_spent():.3f}")
        raise BudgetStop("live mode is not enabled in this proposal build")
