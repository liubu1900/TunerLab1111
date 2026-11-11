"""P2 Pattern Editor backend tests.

A pattern is a plan, so the tests check the two things a plan can get wrong:
the generator must produce exactly the points it promises, and the stored file
must reproduce the same pattern afterwards.
"""
import math

import pytest

from tuner.patterns import (
    delete_pattern, generate_points, list_patterns, load_pattern,
    pattern_points, save_pattern,
)


def test_a_circle_has_the_requested_radius_and_count():
    points = generate_points('circle', {'radius': 0.8, 'phase_start': 0.0,
                                        'phase_stop': 360.0, 'count': 8})
    assert len(points) == 8
    for point in points:
        assert abs(point) == pytest.approx(0.8)
    # A full turn must not repeat the first point.
    assert points[0] != points[-1]


def test_a_circle_starts_and_stops_where_asked():
    points = generate_points('circle', {'radius': 0.5, 'phase_start': 90.0,
                                        'phase_stop': 180.0, 'count': 3})
    assert points[0] == pytest.approx(0.5j)
    assert points[-1] == pytest.approx(-0.5 + 0j, abs=1e-9)


def test_a_radial_sweep_runs_from_one_radius_to_another():
    points = generate_points('radial', {'r_from': 0.0, 'r_to': 0.8,
                                        'phase': 0.0, 'count': 5})
    assert len(points) == 5
    assert abs(points[0]) == pytest.approx(0.0)
    assert abs(points[-1]) == pytest.approx(0.8)
    for point in points:
        assert point.imag == pytest.approx(0.0)


def test_manual_points_are_taken_as_given():
    points = generate_points('points', {'points': [[0.1, 0.2], {'re': 0.3,
                                                                  'im': 0.4}]})
    assert points == [complex(0.1, 0.2), complex(0.3, 0.4)]


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        generate_points('spiral', {})


def test_a_zero_count_is_refused():
    with pytest.raises(ValueError):
        generate_points('circle', {'count': 0})
    with pytest.raises(ValueError):
        generate_points('radial', {'count': 0})


def test_a_saved_pattern_can_be_read_back(tmp_path):
    points = generate_points('circle', {'radius': 0.6, 'count': 5})
    record = save_pattern('我的图案', 'circle', {'radius': 0.6, 'count': 5},
                          points, base=tmp_path)
    assert record['count'] == 5
    loaded = load_pattern(record['path'])
    assert loaded['name'] == '我的图案'
    assert pattern_points(loaded) == pytest.approx(points)


def test_patterns_are_listed_newest_first(tmp_path):
    first = save_pattern('A', 'points', {'points': [[0.1, 0.1]]},
                          base=tmp_path)
    second = save_pattern('B', 'points', {'points': [[0.2, 0.2]]},
                           base=tmp_path)
    listed = list_patterns(tmp_path)
    assert {record['name'] for record in listed} == {'A', 'B'}
    assert listed[0]['name'] == 'B'      # newer first
    assert first['path'] != second['path']


def test_rapid_saves_and_updates_sort_correctly_with_a_coarse_windows_clock(tmp_path, monkeypatch):
    from tuner import patterns
    monkeypatch.setattr(patterns.time, 'time', lambda: 1700000000.0)
    monkeypatch.setattr(patterns, '_last_created', 0.0)
    first = save_pattern('A', 'points', {'points': [[0.1, 0.1]]}, base=tmp_path)
    second = save_pattern('B', 'points', {'points': [[0.2, 0.2]]}, base=tmp_path)
    assert first['created'] < second['created']
    assert [record['name'] for record in list_patterns(tmp_path)] == ['B', 'A']
    updated = save_pattern('A', 'points', {'points': [[0.3, 0.3]]}, base=tmp_path)
    assert updated['created'] > second['created']
    assert [record['name'] for record in list_patterns(tmp_path)] == ['A', 'B']


def test_deleting_a_pattern_removes_it(tmp_path):
    record = save_pattern('临时', 'points', {'points': [[0.1, 0.1]]},
                          base=tmp_path)
    assert delete_pattern(record['path']) is True
    assert delete_pattern(record['path']) is False
    assert list_patterns(tmp_path) == []


def test_a_name_with_unsafe_characters_still_saves(tmp_path):
    record = save_pattern('a/b:c*图案', 'points', {'points': [[0.1, 0.1]]},
                          base=tmp_path)
    assert record['path'].endswith('.json')
    assert load_pattern(record['path'])['name'] == 'a/b:c*图案'


def test_an_empty_pattern_name_is_refused(tmp_path):
    with pytest.raises(ValueError):
        save_pattern('   ', 'points', {'points': []}, base=tmp_path)


def test_a_broken_pattern_file_does_not_hide_the_others(tmp_path):
    good = save_pattern('好的', 'points', {'points': [[0.1, 0.1]]},
                        base=tmp_path)
    (tmp_path / 'broken.json').write_text('{ not json', encoding='utf-8')
    listed = list_patterns(tmp_path)
    assert [record['name'] for record in listed] == ['好的']
    assert good['path']
