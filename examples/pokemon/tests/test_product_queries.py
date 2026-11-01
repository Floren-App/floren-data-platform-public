"""Prove the explorer forwards viewer authority and never falls back to its identity."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pokemon_pipeline.product_queries import query_product


def test_viewers_receive_separate_queries_and_authority(monkeypatch):
    calls = []
    def query(source, sql, *, viewer_token, max_rows):
        calls.append((source, sql, viewer_token))
        return {'columns':['visible_to'], 'rows':[[viewer_token]]}
    monkeypatch.setattr('fdp.query', query)
    assert query_product('lake', 'cards_current', 'alice')['rows'] == [['alice']]
    assert query_product('lake', 'cards_current', 'bob')['rows'] == [['bob']]
    assert len(calls) == 2
    assert all(sql == 'SELECT * FROM reporting.cards_current' for _, sql, _ in calls)
    query_product('lake', 'card_prices_current', 'bob')
    assert calls[-1] == ('lake', 'SELECT * FROM pokemon_market.card_prices_current', 'bob')


def test_missing_viewer_never_uses_fixed_identity(monkeypatch):
    monkeypatch.setattr('fdp.query', lambda *a, **kw: pytest.fail('must fail before any SQL'))
    with pytest.raises(PermissionError):
        query_product('lake', 'cards_current', None)


def test_table_input_cannot_escape_product_allowlist(monkeypatch):
    monkeypatch.setattr('fdp.query', lambda *a, **kw: pytest.fail('must fail before any SQL'))
    with pytest.raises(ValueError):
        query_product('lake', 'cards_current; SELECT * FROM pokemon_raw.records', 'alice')
