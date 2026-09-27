from ledgerlite.text import slugify


def test_runs_of_separators_collapse():
    assert slugify("Eating  Out & Bars") == "eating-out-bars"
    assert slugify("  Rent!  ") == "rent"
    assert slugify("Eating Out") == "eating-out"
