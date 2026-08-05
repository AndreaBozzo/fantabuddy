from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

ROLES = ("P", "D", "C", "A")


class ScoringConfig(BaseModel):
    goal: float = 3.0
    assist: float = 1.0
    yellow_card: float = -0.5
    red_card: float = -1.0
    own_goal: float = -2.0
    penalty_missed: float = -3.0
    penalty_saved: float = 3.0
    goal_conceded: float = -1.0


class LeagueConfig(BaseModel):
    name: str = "Classic 10 - 1000"
    teams: int = Field(default=10, ge=2)
    budget: int = Field(default=1000, ge=25)
    roster: dict[str, int] = Field(default_factory=lambda: {"P": 3, "D": 8, "C": 8, "A": 6})
    role_budget_shares: dict[str, float] = Field(
        default_factory=lambda: {"P": 0.08, "D": 0.16, "C": 0.28, "A": 0.48}
    )
    player_price_caps: dict[str, int] = Field(
        default_factory=lambda: {"P": 45, "D": 65, "C": 140, "A": 250}
    )
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    price_curve_gamma: float = Field(default=1.15, gt=0)

    @model_validator(mode="after")
    def validate_roles(self) -> LeagueConfig:
        if set(self.roster) != set(ROLES):
            raise ValueError(f"roster deve contenere esattamente {ROLES}")
        if set(self.role_budget_shares) != set(ROLES):
            raise ValueError(f"role_budget_shares deve contenere esattamente {ROLES}")
        if set(self.player_price_caps) != set(ROLES):
            raise ValueError(f"player_price_caps deve contenere esattamente {ROLES}")
        if any(value <= 0 for value in self.roster.values()):
            raise ValueError("ogni ruolo deve avere almeno uno slot")
        if abs(sum(self.role_budget_shares.values()) - 1.0) > 1e-9:
            raise ValueError("le quote di budget per ruolo devono sommare a 1")
        if any(value < 1 for value in self.player_price_caps.values()):
            raise ValueError("ogni tetto di prezzo deve essere almeno 1")
        return self

    @property
    def total_slots(self) -> int:
        return self.teams * sum(self.roster.values())

    @property
    def total_budget(self) -> int:
        return self.teams * self.budget


def load_league_config(path: Path) -> LeagueConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return LeagueConfig.model_validate(payload.get("league", payload))
