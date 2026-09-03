from app.reference import POSITION_IMPORTANCE, STADIUMS, canonical_abbreviation


def test_all_32_teams_have_stadium_coordinates():
    assert len(STADIUMS) == 32
    for abbr, info in STADIUMS.items():
        assert -90 <= info["lat"] <= 90
        assert -180 <= info["lon"] <= 180


def test_canonical_abbreviation_maps_relocated_teams():
    assert canonical_abbreviation("OAK") == "LV"
    assert canonical_abbreviation("SD") == "LAC"
    assert canonical_abbreviation("KC") == "KC"


def test_position_importance_has_qb_highest():
    assert POSITION_IMPORTANCE["QB"] == max(POSITION_IMPORTANCE.values())
