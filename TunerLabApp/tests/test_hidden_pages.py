"""0.5.13.2: four pages are hidden from the operator's navigation.

The page classes and their NAVIGATION entries are deliberately kept; this is
a visibility switch, not a removal.  Hidden pages must not be constructed,
must not appear in the tree, and a programmatic ``go(hidden_key)`` must be a
no-op instead of raising or showing a stale page.
"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from tunerlab_app import shell


HIDDEN = ('studio-pattern', 'studio-tune', 'studio-validation', 'studio-audit')
HIDDEN_LABELS = ('目标图案编辑器', '阻抗调谐与验证', '验证报告', '报告与审计')


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    win = shell.MainWindow(smoke_test=True)
    try:
        yield win
    finally:
        win.close()


def _all_keys():
    return {key for _, entries in shell.NAVIGATION for key, _, _ in entries}


def test_hidden_set_is_exactly_the_four_pages_and_keeps_definitions():
    assert shell.HIDDEN_PAGES == frozenset(HIDDEN)
    assert set(HIDDEN) <= _all_keys(), 'hidden pages must stay in NAVIGATION'


def test_hidden_pages_are_not_built(window):
    for key in HIDDEN:
        assert key not in window.pages
        assert key not in window.views
        assert key not in window.items
        assert key not in window._nav_meta
    for key in ('studio-smith', 'studio-overview', 'hw-comm', 'about'):
        assert key in window.pages
    assert len(window.pages) == len(_all_keys()) - len(HIDDEN)


def test_nav_tree_has_no_hidden_labels(window):
    labels = []
    for i in range(window.nav.topLevelItemCount()):
        group = window.nav.topLevelItem(i)
        for j in range(group.childCount()):
            labels.append(group.child(j).text(0))
    for label in HIDDEN_LABELS:
        assert label not in labels
    assert 'Smith 图' in labels
    assert '系统总览 / 前提检查' in labels
    assert '通讯设置' in labels


def test_go_to_hidden_page_is_inert(window):
    before = window.stack.currentWidget()
    for key in HIDDEN:
        window.go(key)
        assert window.stack.currentWidget() is before
