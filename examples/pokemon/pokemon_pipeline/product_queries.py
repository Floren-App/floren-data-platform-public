"""Fixed product query boundary shared by the caller-bound explorer and tests."""
import fdp

TABLES = {'cards_current': 'reporting', 'set_summary': 'reporting',
          'card_prices_current': 'pokemon_market'}


def query_product(source, table, viewer_token):
    if not viewer_token:
        raise PermissionError('Open this dashboard through the authenticated platform')
    if table not in TABLES:
        raise ValueError('Unknown product table')
    return fdp.query(source, f'SELECT * FROM {TABLES[table]}.{table}',
                     max_rows=10000, viewer_token=viewer_token)
