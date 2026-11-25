import pytest

from core.risk import CONTRACT_SPECS, ContractSpecs, calc_lot_size_by_risk


@pytest.fixture(autouse=True)
def patch_sniper(monkeypatch):
    """Disable heavy sniper_agent imports from global fixtures."""
    yield


@pytest.fixture
def specs():
    return {
        "SYMBOL": ContractSpecs(
            symbol="SYMBOL",
            lot_step=0.01,
            min_lot=0.01,
            max_lot=5.0,
            base_currency="AAA",
            quote_currency="USD",
            contract_size=10000.0,
        )
    }


def test_normal_case(specs):
    # $10 risk / (0.0043 price units × 10,000 contract) = 0.2326 → floor 0.23.
    # sl_points is the absolute PRICE distance — the old test passed 43 price
    # units (43 × 10k = $430k/lot), asserting the pre-contract-size formula.
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.01,
        sl_points=0.0043,
        symbol="SYMBOL",
        entry_price=1.0,
        specs=specs,
    )
    assert lot == 0.23


def test_min_clamping(specs):
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.0001,
        sl_points=100,
        symbol="SYMBOL",
        entry_price=1.0,
        specs=specs,
    )
    assert lot == 0.01


def test_max_clamping(specs):
    # $1000 risk / (0.01 × 10,000) = 10 lots → clamped to max_lot 5.0.
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=1,
        sl_points=0.01,
        symbol="SYMBOL",
        entry_price=1.0,
        specs=specs,
    )
    assert lot == 5.0


@pytest.mark.parametrize(
    "balance, risk_pct, sl_points",
    [
        (0, 0.01, 10),
        (1000, 0, 10),
        (1000, 0.01, 0),
        (-1000, 0.01, 10),
        (1000, -0.01, 10),
        (1000, 0.01, -10),
    ],
)
def test_zero_or_negative_inputs(balance, risk_pct, sl_points, specs):

    with pytest.raises(ValueError):
        calc_lot_size_by_risk(balance, risk_pct, sl_points, "SYMBOL", specs)



def test_btcusd_contract_specs():
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.02,
        sl_points=100,
        symbol="BTCUSD",
        entry_price=1.0,
        specs=CONTRACT_SPECS,
    )
    assert lot == 0.2


def test_gbpusd_contract_specs():
    # 50 pips = 0.0050 price units: $10 risk / (0.0050 × 100,000) = 0.02 lots.
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.01,
        sl_points=0.0050,
        symbol="GBPUSD",
        entry_price=1.25,
        specs=CONTRACT_SPECS,
    )
    assert lot == 0.02


def test_xauusd_contract_specs():
    # $1.00 gold stop: $50 risk / (1.0 × 100 oz) = 0.5 lots.
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.05,
        sl_points=1.0,
        symbol="XAUUSD",
        entry_price=1.0,
        specs=CONTRACT_SPECS,
    )
    assert lot == 0.5


def test_base_currency_account_converts_with_entry_price():
    # GBP-denominated account trading GBPUSD (account == BASE currency):
    # risk_per_lot = 0.0025 × 100,000 / 1.25 = £200 → £10 / 200 = 0.05 lots.
    specs = {
        "GBPUSD": CONTRACT_SPECS["GBPUSD"],
    }
    lot = calc_lot_size_by_risk(
        balance=1000,
        risk_pct=0.01,
        sl_points=0.0025,
        symbol="GBPUSD",
        entry_price=1.25,
        specs=specs,
        account_currency="GBP",
    )
    assert lot == 0.05


def test_true_cross_account_fails_closed():
    # EUR account on GBPUSD needs a USD/EUR rate this function does not have.
    # The old code silently "converted" with the pair's own price — mis-sizing
    # the position. It must refuse instead.
    with pytest.raises(ValueError, match="Cross-quoted"):
        calc_lot_size_by_risk(
            balance=1000,
            risk_pct=0.01,
            sl_points=0.0025,
            symbol="GBPUSD",
            entry_price=1.25,
            specs={"GBPUSD": CONTRACT_SPECS["GBPUSD"]},
            account_currency="EUR",
        )
