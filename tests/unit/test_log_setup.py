"""GMS_LOG_LEVEL parsing."""

import logging

from foundation.log_setup import resolve_log_level


def test_resolve_log_level_accepts_names_aliases_and_numbers(capsys):
    assert resolve_log_level('debug') == logging.DEBUG
    assert resolve_log_level(' WARN ') == logging.WARNING
    assert resolve_log_level('fatal') == logging.CRITICAL
    assert resolve_log_level('20') == logging.INFO
    assert resolve_log_level(None) == logging.INFO
    assert resolve_log_level('   ') == logging.INFO
    assert resolve_log_level('nope') == logging.INFO
    assert 'Unknown GMS_LOG_LEVEL' in capsys.readouterr().err
