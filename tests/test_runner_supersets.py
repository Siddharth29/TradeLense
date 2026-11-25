"""Tests for the _build_supersets helper in runner."""

from apps.backend.agents.runner import _build_supersets


def test_supersets_return_full_when_shorter():
    c5 = [{"value": i} for i in range(10)]
    c15 = [{"value": i} for i in range(5)]

    c5_super, c15_super = _build_supersets(c5, c15, want_c5=50, want_c15=20)

    assert c5_super == c5
    assert c15_super == c15


def test_supersets_tail_slice_when_longer():
    c5 = [{"idx": i} for i in range(120)]
    c15 = [{"idx": i} for i in range(60)]

    c5_super, c15_super = _build_supersets(c5, c15, want_c5=100, want_c15=48)

    assert len(c5_super) == 100
    assert c5_super[0] == {"idx": 20}
    assert c5_super[-1] == {"idx": 119}

    assert len(c15_super) == 48
    assert c15_super[0] == {"idx": 12}
    assert c15_super[-1] == {"idx": 59}


def test_supersets_preserve_order():
    c5 = [{"idx": i} for i in range(30)]
    c15 = [{"idx": i} for i in range(10)]

    c5_super, c15_super = _build_supersets(c5, c15, want_c5=15, want_c15=5)

    assert c5_super == c5[-15:]
    assert c15_super == c15[-5:]

