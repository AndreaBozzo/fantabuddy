from __future__ import annotations

import pytest
from pydantic import ValidationError

from fantabuddy.config import LeagueConfig


def test_unknown_administrative_rules_are_supported() -> None:
    config = LeagueConfig()

    assert config.auction.random_by_role is None
    assert config.auction.long_injury_replacement is None
    assert config.lineup.captain is None


def test_min_bid_must_fit_budget_and_price_caps() -> None:
    with pytest.raises(ValidationError, match="base d'asta"):
        LeagueConfig(auction={"min_bid": 50})


def test_enabled_defense_modifier_requires_ordered_bands() -> None:
    with pytest.raises(ValidationError, match="soglie uniche e crescenti"):
        LeagueConfig(
            defense_modifier={
                "enabled": True,
                "bands": [
                    {"min_average": 6.5, "bonus": 3},
                    {"min_average": 6.0, "bonus": 1},
                ],
            }
        )
