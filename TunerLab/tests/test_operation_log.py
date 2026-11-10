"""The process-wide operation log."""
import json
import threading

import pytest

from tuner.operation_log import CATEGORIES, LEVELS, OperationLog


def test_entries_are_recorded_with_timestamp_and_level():
    log = OperationLog(capacity=10)
    entry = log.info('运动', '点动', 'X +0.1', axis='X', delta=0.1)
    assert entry.category == '运动'
    assert entry.action == '点动'
    assert entry.level == 'info'
    assert entry.data == {'axis': 'X', 'delta': 0.1}
    assert entry.timestamp and entry.time_text
    assert log.count() == 1


def test_recent_returns_newest_last_and_respects_filters():
    log = OperationLog(capacity=10)
    log.info('运动', 'a')
    log.warn('标度', 'b')
    log.error('急停', 'c')
    entries = log.recent(10)
    assert [e['action'] for e in entries] == ['a', 'b', 'c']
    assert log.recent(10, level='error')[0]['action'] == 'c'
    assert log.recent(10, category='运动')[0]['action'] == 'a'
    assert log.recent(1)[0]['action'] == 'c'


def test_since_seq_only_returns_newer_entries():
    log = OperationLog(capacity=10)
    first = log.info('系统', 'one')
    log.info('系统', 'two')
    entries = log.recent(10, since_seq=first.seq)
    assert [e['action'] for e in entries] == ['two']


def test_capacity_bounds_the_buffer():
    log = OperationLog(capacity=3)
    for index in range(10):
        log.info('系统', f'entry{index}')
    assert log.count() == 3
    assert log.recent(10)[0]['action'] == 'entry7'


def test_clear_empties_the_buffer():
    log = OperationLog(capacity=5)
    log.info('系统', 'x')
    assert log.clear() == 1
    assert log.count() == 0
    assert log.recent(10) == []


def test_jsonl_sink_appends_every_entry(tmp_path):
    sink = tmp_path / 'ops.jsonl'
    log = OperationLog(capacity=5, sink=sink)
    log.info('运动', 'a')
    log.error('急停', 'b')
    lines = sink.read_text(encoding='utf-8').strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first['action'] == 'a'
    assert first['level'] == 'info'


def test_export_writes_json(tmp_path):
    log = OperationLog(capacity=5)
    log.info('运动', 'a')
    target = log.export(tmp_path / 'out.json')
    payload = json.loads(target.read_text(encoding='utf-8'))
    assert payload[0]['action'] == 'a'


def test_unknown_level_is_rejected():
    log = OperationLog(capacity=5)
    with pytest.raises(ValueError):
        log.log('系统', 'x', '', 'critical')


def test_exception_entry_keeps_the_type():
    log = OperationLog(capacity=5)
    entry = log.exception('运动', '绝对定位', ValueError('boom'))
    assert entry.level == 'error'
    assert entry.detail == 'ValueError: boom'
    assert entry.data['error_type'] == 'ValueError'


def test_concurrent_writers_do_not_lose_entries():
    log = OperationLog(capacity=500)

    def worker(index):
        for step in range(50):
            log.info('系统', f'{index}-{step}')

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert log.count() == 200
    sequence = [e['seq'] for e in log.recent(500)]
    assert sequence == sorted(sequence)


def test_categories_and_levels_are_the_documented_ones():
    assert '通讯' in CATEGORIES and '校准' in CATEGORIES
    assert LEVELS == ('info', 'warn', 'error')
