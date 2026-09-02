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
    clean_sheet: float = 0.0


class DefenseModifierBand(BaseModel):
    min_average: float
    bonus: float


class DefenseModifierConfig(BaseModel):
    enabled: bool = False
    includes_goalkeeper: bool = True
    bands: list[DefenseModifierBand] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bands(self) -> DefenseModifierConfig:
        if self.enabled and not self.bands:
            raise ValueError("un modificatore difesa attivo richiede almeno una fascia")
        thresholds = [band.min_average for band in self.bands]
        if thresholds != sorted(set(thresholds)):
            raise ValueError("le fasce del modificatore devono avere soglie uniche e crescenti")
        return self


class GoalBandsConfig(BaseModel):
    first_goal: int = Field(default=66, ge=1)
    step: int = Field(default=4, ge=1)


class AuctionRulesConfig(BaseModel):
    min_bid: int = Field(default=1, ge=1)
    random_by_role: bool | None = None
    repair_release_slots: int | None = Field(default=None, ge=0)
    voluntary_release_refund_share: float | None = Field(default=None, ge=0, le=1)
    transferred_player_full_refund: bool | None = None
    long_injury_replacement: bool | None = None


class LineupRulesConfig(BaseModel):
    captain: bool | None = None
    max_bench_players: int | None = Field(default=None, ge=0)
    substitutions: int = Field(default=5, ge=0)


class LeagueConfig(BaseModel):
    name: str = "Classic 10 - 1000"
    teams: int = Field(default=10, ge=2)
    budget: int = Field(default=1000, ge=25)
    roster: dict[str, int] = Field(default_factory=lambda: {"P": 3, "D": 8, "C": 8, "A": 6})
    role_budget_shares: dict[str, float] = Field(
        default_factory=lambda: {"P": 0.08, "D": 0.16, "C": 0.28, "A": 0.48}
    )
    player_price_caps: dict[str, int] = Field(
        default_factory=lambda: {"P": 90, "D": 130, "C": 280, "A": 500}
    )
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    defense_modifier: DefenseModifierConfig = Field(default_factory=DefenseModifierConfig)
    goal_bands: GoalBandsConfig = Field(default_factory=GoalBandsConfig)
    auction: AuctionRulesConfig = Field(default_factory=AuctionRulesConfig)
    lineup: LineupRulesConfig = Field(default_factory=LineupRulesConfig)
    rules_source: str | None = None
    strategy_notes: list[str] = Field(default_factory=list)
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
        if any(value < self.auction.min_bid for value in self.player_price_caps.values()):
            raise ValueError("ogni tetto di prezzo deve essere almeno pari alla base d'asta")
        if self.total_budget < self.total_slots * self.auction.min_bid:
            raise ValueError("budget insufficiente per garantire la base d'asta a ogni slot")
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
