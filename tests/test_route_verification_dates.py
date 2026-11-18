from datetime import date
from pathlib import Path

import pytest


def test_every_bundled_route_has_an_explicit_verification_date():
    from openmd.routing.generate import declarations
    rows = declarations()['routes']['routes']
    assert rows and all('last_verified' in row for row in rows)
    for row in rows:
        if row['last_verified'] is not None:
            date.fromisoformat(row['last_verified'])


def test_route_ages_separate_unknown_stale_fresh_and_future():
    from openmd.routing.loader import route_verification_status
    rows = [{'source': str(i), 'capability': 'content', 'last_verified': value} for i, value in enumerate(
        ['2026-10-18', '2026-10-19', '2026-11-18', None, '2026-11-19'])]
    result = route_verification_status(rows, today=date(2026, 11, 18))
    assert result['stale_count'] == 1
    assert result['unknown_count'] == 1
    assert result['future_count'] == 1
    assert [r['state'] for r in result['routes']] == ['stale', 'fresh', 'fresh', 'unknown', 'future']


def test_invalid_route_verification_dates_are_rejected():
    from openmd.routing.loader import load_catalog, validate_catalog
    catalog = load_catalog(include_plugins=False)
    data = {'schema': 'openmd.routes/v1', 'version': 'fixture', 'routes': [
        {'source': 'bilibili', 'capability': 'transcript', 'chain': ['opencli.bilibili.subtitle'], 'last_verified': 'yesterday'}]}
    with pytest.raises(ValueError, match='last_verified'):
        validate_catalog(list(catalog.cards.values()), data)


def test_generated_route_table_shows_last_verified():
    from openmd.routing.view import render_routes_markdown
    assert 'Last verified' in render_routes_markdown(effective=False)
