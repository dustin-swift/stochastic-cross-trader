from lib.universe import cap_per_sector


def test_cap_per_sector_basic():
    candidates = [
        {"symbol": "A", "sector": "Tech"},
        {"symbol": "B", "sector": "Tech"},
        {"symbol": "C", "sector": "Tech"},
        {"symbol": "D", "sector": "Health"},
    ]
    result = cap_per_sector(candidates, max_per_sector=2)
    symbols = [c["symbol"] for c in result]
    assert symbols == ["A", "B", "D"]


def test_cap_per_sector_preserves_order_across_sectors():
    candidates = [
        {"symbol": "A", "sector": "Tech"},
        {"symbol": "B", "sector": "Health"},
        {"symbol": "C", "sector": "Tech"},
        {"symbol": "D", "sector": "Tech"},
    ]
    result = cap_per_sector(candidates, max_per_sector=1)
    symbols = [c["symbol"] for c in result]
    assert symbols == ["A", "B"]


def test_cap_per_sector_missing_sector_treated_as_unknown_bucket():
    candidates = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    result = cap_per_sector(candidates, max_per_sector=2)
    symbols = [c["symbol"] for c in result]
    assert symbols == ["A", "B"]
