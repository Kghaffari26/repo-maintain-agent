from ledgerlite.pagination import paginate


def test_first_page_starts_at_the_first_item():
    items = list(range(25))
    assert paginate(items, page=1) == list(range(10))
    assert paginate(items, page=3) == [20, 21, 22, 23, 24]
    assert paginate(items, page=2, per_page=5) == [5, 6, 7, 8, 9]
