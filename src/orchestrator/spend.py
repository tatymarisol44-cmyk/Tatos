"""Compatibility composition of spending policy and its SQL adapter.

The database-independent policy is in budget; SQL is in spend_store. Existing callers
can keep constructing Spend(Database, Settings) and importing model_spend here.
"""

from __future__ import annotations

from orchestrator.budget import BudgetExceeded as BudgetExceeded
from orchestrator.budget import Spend as BudgetPolicy
from orchestrator.budget import period as period
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.spend_store import SqlSpendLedger
from orchestrator.spend_store import model_spend as model_spend


class Spend(BudgetPolicy):
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        super().__init__(SqlSpendLedger(db), settings.tenant_monthly_budget_usd)

    @property
    def budget_usd(self) -> float:
        return self.settings.tenant_monthly_budget_usd
