import pathlib

def test_frontend_contains_error_logging():
    html = pathlib.Path('apps/frontend/index.html').read_text()
    assert 'Stream error' in html
    assert 'Invalid log event' in html
