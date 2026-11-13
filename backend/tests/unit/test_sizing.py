import pytest

from app.engine.config import ContractMapping, FollowerConfig
from app.engine.sizing import follower_targets, managed_symbols, order_delta, scaled_qty


@pytest.mark.parametrize(
    ("qty", "mult", "ratio", "expected"),
    [
        (1, 1.0, 1, 1),
        (2, 0.5, 1, 1),
        (1, 0.5, 1, 0),  # rounds down, skip
        (3, 0.5, 1, 1),
        (1, 1.0, 10, 10),  # NQ -> MNQ
        (3, 0.33, 10, 9),  # 9.9 -> 9
        (100, 0.29, 1, 29),  # float would give 28.999999999999996
        (3, 0.1, 10, 3),  # float would give 3.0000000000000004
        (7, 0.7, 1, 4),  # 4.9 -> 4
        (0, 2.0, 10, 0),
    ],
)
def test_scaled_qty_floors_exactly(qty, mult, ratio, expected):
    assert scaled_qty(qty, mult, ratio) == expected


def test_scaled_qty_rejects_negative():
    with pytest.raises(ValueError):
        scaled_qty(-1, 1, 1)


def cfg(**kw) -> FollowerConfig:
    return FollowerConfig("f", **kw)


def test_target_keeps_sign():
    assert follower_targets({"NQ": -3}, cfg(multiplier=0.5))["NQ"] == -1
    assert follower_targets({"NQ": 3}, cfg(multiplier=0.5))["NQ"] == 1


def test_target_cap_applies_to_both_sides():
    c = cfg(multiplier=2, max_contracts=5)
    assert follower_targets({"NQ": 4}, c)["NQ"] == 5
    assert follower_targets({"NQ": -4}, c)["NQ"] == -5


def test_mapping_redirects_symbol_and_applies_ratio():
    c = cfg(mappings=[ContractMapping("NQ", "MNQ", 10)])
    targets = follower_targets({"NQ": 2}, c)
    assert targets["MNQ"] == 20
    assert "NQ" not in targets  # NQ is not traded by this follower


def test_two_leader_symbols_into_one_follower_symbol():
    c = cfg(mappings=[ContractMapping("NQ", "MNQ", 10)])
    assert follower_targets({"NQ": 1, "MNQ": 5}, c)["MNQ"] == 15


def test_unmapped_symbols_copy_one_to_one():
    c = cfg(mappings=[ContractMapping("NQ", "MNQ", 10)])
    assert follower_targets({"ES": 2}, c)["ES"] == 2


def test_managed_symbols():
    c = cfg(mappings=[ContractMapping("NQ", "MNQ", 10)])
    assert "NQ" not in managed_symbols(c)
    assert {"MNQ", "ES", "MES", "YM", "MYM", "RTY", "M2K"} <= managed_symbols(c)


def test_order_delta_scale_in_rounding():
    c = cfg(multiplier=0.5)
    assert order_delta({}, c, "NQ", 1) == ("NQ", 0)
    assert order_delta({"NQ": 1}, c, "NQ", 1) == ("NQ", 1)
    assert order_delta({"NQ": 2}, c, "NQ", -1) == ("NQ", -1)
    assert order_delta({"NQ": 1}, c, "NQ", -1) == ("NQ", 0)


def test_order_delta_respects_cap():
    c = cfg(max_contracts=3)
    assert order_delta({"NQ": 2}, c, "NQ", 5) == ("NQ", 1)


def test_order_delta_reversal():
    assert order_delta({"NQ": 2}, cfg(), "NQ", -5) == ("NQ", -5)


def test_mapping_validation():
    with pytest.raises(ValueError):
        ContractMapping("NQ", "ES", 1).validate()  # different index
    with pytest.raises(ValueError):
        ContractMapping("NQ", "MNQ", 0).validate()
    with pytest.raises(ValueError):
        FollowerConfig("f", multiplier=0).validate()
    with pytest.raises(ValueError):
        FollowerConfig("f", mappings=[ContractMapping("NQ", "MNQ", 10),
                                      ContractMapping("NQ", "NQ", 1)]).validate()
