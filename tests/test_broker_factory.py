import pytest
from packages.adapters.factory import create_broker
from packages.adapters.mt5_adapter import MT5Adapter


def test_create_broker_instances():
    assert isinstance(create_broker("mt5"), MT5Adapter)
    with pytest.raises(ValueError):
        create_broker("paper")
    with pytest.raises(ValueError):
        create_broker("unknown")
