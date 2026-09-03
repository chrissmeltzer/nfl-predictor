STADIUMS = {
    "ARI": {"name": "State Farm Stadium", "lat": 33.5276, "lon": -112.2626},
    "ATL": {"name": "Mercedes-Benz Stadium", "lat": 33.7554, "lon": -84.4008},
    "BAL": {"name": "M&T Bank Stadium", "lat": 39.2780, "lon": -76.6227},
    "BUF": {"name": "Highmark Stadium", "lat": 42.7738, "lon": -78.7870},
    "CAR": {"name": "Bank of America Stadium", "lat": 35.2258, "lon": -80.8528},
    "CHI": {"name": "Soldier Field", "lat": 41.8623, "lon": -87.6167},
    "CIN": {"name": "Paycor Stadium", "lat": 39.0954, "lon": -84.5160},
    "CLE": {"name": "Huntington Bank Field", "lat": 41.5061, "lon": -81.6995},
    "DAL": {"name": "AT&T Stadium", "lat": 32.7473, "lon": -97.0945},
    "DEN": {"name": "Empower Field at Mile High", "lat": 39.7439, "lon": -105.0201},
    "DET": {"name": "Ford Field", "lat": 42.3400, "lon": -83.0456},
    "GB": {"name": "Lambeau Field", "lat": 44.5013, "lon": -88.0622},
    "HOU": {"name": "NRG Stadium", "lat": 29.6847, "lon": -95.4107},
    "IND": {"name": "Lucas Oil Stadium", "lat": 39.7601, "lon": -86.1639},
    "JAX": {"name": "EverBank Stadium", "lat": 30.3239, "lon": -81.6373},
    "KC": {"name": "GEHA Field at Arrowhead Stadium", "lat": 39.0489, "lon": -94.4839},
    "LAC": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "LAR": {"name": "SoFi Stadium", "lat": 33.9535, "lon": -118.3392},
    "LV": {"name": "Allegiant Stadium", "lat": 36.0909, "lon": -115.1833},
    "MIA": {"name": "Hard Rock Stadium", "lat": 25.9580, "lon": -80.2389},
    "MIN": {"name": "U.S. Bank Stadium", "lat": 44.9735, "lon": -93.2575},
    "NE": {"name": "Gillette Stadium", "lat": 42.0909, "lon": -71.2643},
    "NO": {"name": "Caesars Superdome", "lat": 29.9511, "lon": -90.0812},
    "NYG": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745},
    "NYJ": {"name": "MetLife Stadium", "lat": 40.8135, "lon": -74.0745},
    "PHI": {"name": "Lincoln Financial Field", "lat": 39.9008, "lon": -75.1675},
    "PIT": {"name": "Acrisure Stadium", "lat": 40.4468, "lon": -80.0158},
    "SEA": {"name": "Lumen Field", "lat": 47.5952, "lon": -122.3316},
    "SF": {"name": "Levi's Stadium", "lat": 37.4030, "lon": -121.9700},
    "TB": {"name": "Raymond James Stadium", "lat": 27.9759, "lon": -82.5033},
    "TEN": {"name": "Nissan Stadium", "lat": 36.1665, "lon": -86.7713},
    "WSH": {"name": "Northwest Stadium", "lat": 38.9077, "lon": -76.8645},
}

# Historical nflverse team abbreviations for relocated/renamed franchises,
# mapped to the current abbreviation used as our canonical key everywhere.
# Canonical abbreviations are ESPN's (since the `teams` table is populated
# from ESPN) — confirmed live that ESPN uses "WSH" for Washington while
# nflverse's CSV uses "WAS", so that direction of the alias matters.
TEAM_ALIASES = {
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WAS": "WSH",
    "LA": "LAR",
}

POSITION_IMPORTANCE = {
    "QB": 7.0, "RB": 3.0, "WR": 2.5, "TE": 2.0,
    "OT": 2.0, "OG": 1.5, "G": 1.5, "C": 1.5,
    "DE": 2.0, "DT": 1.5, "NT": 1.5, "LB": 1.5,
    "CB": 2.0, "S": 1.5, "K": 1.0, "P": 0.5,
}
DEFAULT_POSITION_IMPORTANCE = 1.0


def canonical_abbreviation(abbr: str) -> str:
    return TEAM_ALIASES.get(abbr, abbr)
