"""The single application window.

Before this shell the operator had to know which module to launch: the motion
console, the Mock calibration studio or the web server.  Here every page lives in
one window with one hardware session, one settings file and one audit log, which
is what "a normal application" means for the people standing at the machine.

The pages themselves are unchanged: the hardware pages come from
:mod:`tuner.ui_hardware`, the calibration pages are thin clients over
:mod:`tuner.web.handlers`.  Nothing about the motion safety rules is re-implemented
here.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QScrollArea, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import version
from tuner.operation_log import operation_log
from tuner.settings import SettingsStore
from tuner.ui_hardware import HardwareSession, OperationLogPage
from tuner.ui_theme import STYLESHEET, apply_theme, numeric_font, text_font
from tuner.web.state import AppState
from . import paths
from .fonts import (
    FAMILY as UI_FAMILY, px_font, ui_font,
    get_ui_scale, set_ui_scale, UI_SCALE_MIN, UI_SCALE_MAX,
)
from .library_context import LibraryContext
from .pages_about import SettingsAboutPage
from .pages_comm import SafeCommunicationPage
from .pages_params import SafeAxisParameterPage
from .pages_motion import SafeAxisMotionPage
from .pages_audit import StudioAuditPage
from .pages_p1 import StudioLibraryBrowserPage
from .pages_pattern import StudioPatternPage
from .pages_studio import (
    StudioDatabasePage, StudioOverviewPage, StudioRunPage, StudioSetupPage,
)
from .pages_tune import StudioSmithPage, StudioTuningPage, StudioValidationPage
from .pages_vna import VnaPage
from .pages_vna_debug import VnaDebugPage
from .session import TunerSession
from .runtime import enable_crash_log, install_exception_hook, setup_logging
from .theme import SHELL_QSS, PILL_STYLES, scaled_stylesheet
from .web_console import WebConsole
from .widgets import StatusPill

#: The two shipped style sheets, concatenated once and re-scaled on resize.
BASE_STYLESHEET = STYLESHEET + SHELL_QSS

#: Pages that are taller than a small laptop screen; the shell wraps them in a
#: vertical scroll area so no control is ever clipped.
#:
#: ``studio-setup`` is in the list because its two-column workbench still needs
#: 760 px at the 1140x720 minimum window size.  Without a scroll area Qt
#: resolves the shortfall by crushing the form rows to a few pixels, which is
#: what made the setup page's inputs overlap each other.
#:
#: ``studio-smith`` needs 10 px more than the minimum window offers; the
#: verdict card is the one that gives way, so it scrolls instead.
#:
#: ``studio-pattern`` carries a chart, a generator panel and a resolution
#: table; at 1140x720 they do not fit side by side either.
_SCROLLED_PAGES = frozenset({'hw-motion', 'studio-setup', 'studio-smith',
                             'studio-pattern', 'vna-debug'})

#: Navigation tree: (group title, [(key, label, page factory), ...]).
#: Grouped per TunerLab_0.3.9 设计规格: 准备 / 数据与目标 / 验证与追溯 / 系统.
NAVIGATION = (
    ('准备', (
        ('studio-overview', '系统总览 / 前提检查', lambda w: StudioOverviewPage(w.state)),
        ('studio-setup', '建库向导', lambda w: StudioSetupPage(w.state)),
        ('studio-run', '采集运行', lambda w: StudioRunPage(w.state)),
    )),
    ('数据与目标', (
        ('studio-db', '校准库', lambda w: StudioDatabasePage(w.state)),
        ('studio-browser', '库浏览器', lambda w: StudioLibraryBrowserPage(w.state)),
        ('studio-pattern', '目标图案编辑器', lambda w: StudioPatternPage(w.state)),
    )),
    ('验证与追溯', (
        ('studio-smith', 'Smith 图', lambda w: StudioSmithPage(w.state)),
        ('studio-tune', '阻抗调谐与验证', lambda w: StudioTuningPage(w.state)),
        ('studio-validation', '验证报告', lambda w: StudioValidationPage(w.state)),
        ('studio-audit', '报告与审计', lambda w: StudioAuditPage(w.state)),
    )),
    ('系统', (
        ('hw-comm', '通讯设置', lambda w: SafeCommunicationPage(w.session)),
        ('hw-motion', '轴移动控制', lambda w: SafeAxisMotionPage(w.session)),
        ('hw-params', '轴参数设定', lambda w: SafeAxisParameterPage(w.session)),
        ('vna', '矢网控制 (VNA)', lambda w: VnaPage(w.state)),
        ('vna-debug', 'SCPI 调试工具', lambda w: VnaDebugPage(w.state)),
        ('oplog', '操作日志', lambda w: OperationLogPage(w.session)),
        ('about', '设置与关于', lambda w: SettingsAboutPage(w.state)),
    )),
)

#: Pages temporarily hidden from the operator's navigation (0.5.13.2).
#: The page classes and their factories stay in :data:`NAVIGATION`, so this is
#: a pure visibility switch: remove a key -- or empty the set -- to restore a
#: page without touching any page code.  ``MainWindow.go`` already ignores
#: keys without a navigation item, so in-page links to a hidden page are inert.
HIDDEN_PAGES = frozenset({
    'studio-pattern',      # 目标图案编辑器
    'studio-tune',         # 阻抗调谐与验证
    'studio-validation',   # 验证报告
    'studio-audit',        # 报告与审计（现场称「报告与设计」）
})


class SharedState(AppState):
    """``AppState`` that reuses the desktop session's controller link.

    ``AppState.__init__`` builds its own :class:`HardwareSession`, which is right
    for a browser client but wrong here: the hardware pages already own a session
    and two sessions would open two handles on the same controller.  Subclassing
    keeps the original module untouched.
    """

    def __init__(self, hw: HardwareSession):
        super().__init__()
        self.hw = hw
        #: The single active calibration library and its change broadcast.
        #: Pages subscribe through :class:`_StudioPage`; the badge and the
        #: Calibration Browser subscribe through :meth:`MainWindow._wire`.
        self.library = LibraryContext(self)


class MainWindow(QMainWindow):
    """One window, four navigation groups; keys in HIDDEN_PAGES are not built."""

    def __init__(self, smoke_test: bool = False):
        super().__init__()
        #: A smoke test must not touch the real controller (see ``main``).
        self._smoke_test = bool(smoke_test)
        self.store = SettingsStore()
        self.session = TunerSession(self.store)
        if self._smoke_test:
            # Belt and braces: even if some code path connects during the
            # check, it must land on the simulated controller, never on the
            # machine the workshop PC is plugged into.
            #
            # The setting is changed *in memory only* — ``set_mode()`` would
            # persist it, and an installation check must never rewrite the
            # operator's configuration.
            try:
                self.session.settings.mode = 'virtual'
            except Exception:  # noqa: BLE001 - a missing setting must not abort the check
                pass
        # Share the hardware session: one process, one controller handle.
        self.state = SharedState(self.session)
        #: The browser console, bound to this very state so both faces agree.
        self.console = WebConsole(self.state)

        self.pages: dict[str, QWidget] = {}
        #: Widget actually inserted into the stack for each key.  A tall page is
        #: wrapped in a scroll area so a short window scrolls instead of clipping.
        self.views: dict[str, QWidget] = {}
        self.items: dict[str, QTreeWidgetItem] = {}

        self.setWindowTitle(f'{version.__app_name__} {version.__version__} · '
                            f'调谐器运动控制与校准')
        self.resize(1340, 880)
        self.setMinimumSize(1140, 720)
        self._set_icon()

        self._build_ui()
        self._build_menu()
        self._build_status()
        self._wire()
        self._sync_top_badge()
        # Type size follows the real window from the first frame (a smoke test
        # never shows the window, so resizeEvent alone would leave it at 1.0).
        self._apply_ui_scale(force=True)

        self.go('studio-overview' if self.session.connected else 'hw-comm')

        # A smoke test is an installation check, not an operator session: it must
        # never reach out to the real controller.  Auto-connecting during one
        # would open a live link to the machine, and the motion page's startup
        # path can issue a positioning command on it — which is exactly what an
        # unattended installation check must not do.
        if (self.session.settings.connection.connect_on_start
                and not self._smoke_test):
            QTimer.singleShot(400, self._auto_connect)

    # -- adaptive type ---------------------------------------------------
    def _apply_ui_scale(self, force: bool = False) -> None:
        """Match type size to the window width.

        The stylesheet is application-wide, so one re-polish moves every caption,
        button, table header and menu together.  Called once at construction and
        afterwards from a debounced resize handler.
        """
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return
        scale = max(UI_SCALE_MIN, min(UI_SCALE_MAX, self.width() / 1280.0))
        if not force and abs(scale - get_ui_scale()) < 0.02:
            return
        set_ui_scale(scale)
        app.setStyleSheet(scaled_stylesheet(BASE_STYLESHEET, scale))
        app.setFont(px_font(13))

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        timer = getattr(self, '_scale_timer', None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(180)
            timer.timeout.connect(lambda: self._apply_ui_scale())
            self._scale_timer = timer
        timer.start()

    def closeEvent(self, event):  # noqa: N802 - Qt override
        # A pending debounce must not fire against a torn-down stylesheet.
        timer = getattr(self, '_scale_timer', None)
        if timer is not None:
            timer.stop()
        super().closeEvent(event)

    # -- construction ----------------------------------------------------
    def _set_icon(self) -> None:
        icon = paths.app_root() / 'packaging' / 'tunerlab.ico'
        if icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName('Root')
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName('Sidebar')
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(200)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 18, 0, 14)
        side.setSpacing(0)

        brand_row = QWidget()
        brand_row.setObjectName('BrandRow')
        brand_row.setFixedHeight(52)
        brand_layout = QHBoxLayout(brand_row)
        brand_layout.setContentsMargins(18, 0, 16, 6)
        brand_layout.setSpacing(10)

        mark = QLabel('TL')
        mark.setObjectName('BrandMark')
        mark.setFixedSize(34, 34)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFont(px_font(12, 'bold'))
        brand_layout.addWidget(mark)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand = QLabel('TunerLab')
        brand.setObjectName('BrandName')
        brand.setFont(px_font(14, 'bold'))
        sub = QLabel(f'v{version.__version__}')
        sub.setObjectName('BrandSub')
        sub.setFont(px_font(10.5, 'light'))
        brand_text.addWidget(brand)
        brand_text.addWidget(sub)
        brand_layout.addLayout(brand_text, 1)
        side.addWidget(brand_row)

        self.nav = QTreeWidget()
        self.nav.setObjectName('NavTree')
        self.nav.setHeaderHidden(True)
        self.nav.setIndentation(12)
        self.nav.setRootIsDecorated(False)
        self.nav.setExpandsOnDoubleClick(False)
        self.nav.setAnimated(False)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        side.addWidget(self.nav, 1)

        # The top bar carries the connection badge; the rail stays clean.
        self.state_pill = StatusPill('未连接', 'idle')
        self.state_pill.hide()

        layout.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.topbar = QWidget()
        self.topbar.setObjectName('TopBar')
        self.topbar.setFixedHeight(56)
        top = QHBoxLayout(self.topbar)
        top.setContentsMargins(22, 0, 22, 0)
        top.setSpacing(10)
        self.top_title = QLabel('通讯设置')
        self.top_title.setObjectName('TopTitle')
        self.top_title.setFont(px_font(17, 'bold'))
        self.top_crumb = QLabel('设备')
        self.top_crumb.setObjectName('TopCrumb')
        self.top_crumb.setFont(px_font(12, 'light'))
        top.addWidget(self.top_title)
        top.addWidget(self.top_crumb)
        top.addSpacing(4)
        top.addStretch()
        self.top_message = QLabel('')
        self.top_message.setObjectName('TopCrumb')
        self.top_message.setFont(px_font(12, 'light'))
        self.top_message.setMaximumWidth(340)
        top.addWidget(self.top_message)
        top.addSpacing(4)

        # Two links, two badges: the controller and the VNA are separate
        # instruments with separate addresses, and "connected" is a property of
        # each, not of the window.
        self.ctrl_badge = QLabel('● 控制器 未连接')
        self.vna_badge = QLabel('● 矢网 未连接')
        for badge in (self.ctrl_badge, self.vna_badge):
            badge.setObjectName('TopBadge')
            badge.setFont(px_font(11.5, 'medium'))
            badge.setContentsMargins(13, 5, 13, 5)
            badge.setStyleSheet('color:#8496ad;')
            top.addWidget(badge)
        # F-23: live calibration-library / motion badge in the top bar.
        self.cal_badge = QLabel('○ 校准库 无')
        self.cal_badge.setObjectName('TopBadge')
        self.cal_badge.setFont(px_font(11.5, 'medium'))
        self.cal_badge.setContentsMargins(13, 5, 13, 5)
        self.cal_badge.setStyleSheet('color:#8496ad;')
        top.addWidget(self.cal_badge)
        content_layout.addWidget(self.topbar)

        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)
        layout.addWidget(content, 1)

        self._nav_meta: dict[str, tuple[str, str]] = {}
        for group, entries in NAVIGATION:
            visible = [entry for entry in entries
                       if entry[0] not in HIDDEN_PAGES]
            if not visible:
                continue
            group_item = QTreeWidgetItem([group])
            group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            font = QFont(group_item.font(0))
            font.setBold(True)
            font.setPointSize(10)
            group_item.setFont(0, font)
            group_item.setForeground(0, QColor('#97a6b6'))
            group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.nav.addTopLevelItem(group_item)
            for key, label, factory in visible:
                page = factory(self)
                self.pages[key] = page
                view: QWidget = page
                if key in _SCROLLED_PAGES:
                    scroll = QScrollArea()
                    scroll.setWidgetResizable(True)
                    scroll.setFrameShape(QFrame.Shape.NoFrame)
                    scroll.setWidget(page)
                    view = scroll
                self.views[key] = view
                self.stack.addWidget(view)
                item = QTreeWidgetItem([label])
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                self._nav_meta[key] = (group, label)
                group_item.addChild(item)
                self.items[key] = item
                if hasattr(page, 'navigate'):
                    page.navigate.connect(self.go)
            group_item.setExpanded(True)

    def _build_menu(self) -> None:
        bar = self.menuBar()
        bar.hide()

        file_menu = bar.addMenu('文件(&F)')
        open_action = QAction('打开校准库…', self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.on_open_database)
        file_menu.addAction(open_action)

        export_action = QAction('导出操作日志…', self)
        export_action.triggered.connect(self.on_export_log)
        file_menu.addAction(export_action)

        # The browser console is the same program in a different face: it
        # shares this session, this library and this log.
        self.console_action = QAction('打开浏览器控制台', self)
        self.console_action.triggered.connect(self.on_open_web_console)
        file_menu.addAction(self.console_action)

        file_menu.addSeparator()
        quit_action = QAction('退出', self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        device_menu = bar.addMenu('设备(&D)')
        connect_action = QAction('连接控制器', self)
        connect_action.setShortcut(QKeySequence('F5'))
        connect_action.triggered.connect(self.on_connect)
        device_menu.addAction(connect_action)

        disconnect_action = QAction('断开控制器', self)
        disconnect_action.setShortcut(QKeySequence('Shift+F5'))
        disconnect_action.triggered.connect(self.on_disconnect)
        device_menu.addAction(disconnect_action)

        device_menu.addSeparator()
        virtual_action = QAction('连接虚拟控制器（无硬件演练）', self)
        virtual_action.triggered.connect(lambda: self.on_connect('virtual'))
        device_menu.addAction(virtual_action)

        help_menu = bar.addMenu('帮助(&H)')
        docs_action = QAction('打开文档目录', self)
        docs_action.triggered.connect(self.on_open_docs)
        help_menu.addAction(docs_action)
        help_menu.addSeparator()
        about_action = QAction('关于 TunerLab', self)
        about_action.triggered.connect(self.on_about)
        help_menu.addAction(about_action)

    def _build_status(self) -> None:
        # Scheme 5 has no status bar; the transient message lives in the top bar.
        self.model_label = QLabel('')
        self.model_label.setFont(numeric_font(9))
        self.statusBar().addPermanentWidget(self.model_label)
        self.statusBar().hide()
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self.top_message.setText(''))
        self._refresh_model_label()
        self._update_cal_badge()

    def _sync_top_badge(self) -> None:
        """Refresh the controller badge.

        It used to be written once at start-up and never again, so a successful
        connect left "● 未连接" sitting in the top bar while the status card and
        the message both said 已连接.
        """
        if not hasattr(self, 'ctrl_badge'):
            return
        connected = bool(self.session.connected)
        address = (self.session.settings.connection.ip or '').strip()
        mode = '虚拟' if self.session.settings.mode == 'virtual' else '真实'
        unit = self.session.settings.unit
        axes = '/'.join(str(a) for a in unit.physical_axes)
        if connected:
            self.ctrl_badge.setText(f'● 控制器 {address or mode} · {unit.label}')
            self.ctrl_badge.setStyleSheet(PILL_STYLES['ok'])
            self.ctrl_badge.setToolTip(
                f'控制器已连接\n地址：{address or "（虚拟）"}\n模式：{mode}\n'
                f'当前 Tuner：{unit.label}（物理轴 {axes}）')
        else:
            self.ctrl_badge.setText('● 控制器 未连接')
            self.ctrl_badge.setStyleSheet(PILL_STYLES['idle'])
            self.ctrl_badge.setToolTip(
                f'控制器未连接\n上次地址：{address or "未配置"}\n模式：{mode}\n'
                f'当前 Tuner：{unit.label}（物理轴 {axes}）')

    def _on_vna_state(self, connected: bool, host: str, idn: str) -> None:
        """Refresh the VNA badge from the instrument page's poll."""
        if not hasattr(self, 'vna_badge'):
            return
        text = f'● 矢网 {host or "已连接"}' if connected else '● 矢网 未连接'
        if self.vna_badge.text() != text:
            self.vna_badge.setText(text)
            self.vna_badge.setStyleSheet(
                PILL_STYLES['ok'] if connected else PILL_STYLES['idle'])
        detail = idn or f'目标地址：{host or "未配置"}'
        if self.vna_badge.toolTip() != detail:
            self.vna_badge.setToolTip(detail)

    def _wire(self) -> None:
        self.nav.currentItemChanged.connect(self._on_nav_changed)
        self.session.connection_changed.connect(self._on_connection)
        self.session.settings_changed.connect(self._refresh_model_label)
        self.session.status_changed.connect(self.show_status)
        # One active calibration for the whole window: every library change
        # refreshes the top-bar badge, whichever face initiated it.
        self.state.library.changed.connect(lambda _path: self._update_cal_badge())
        self.state.library.activation_failed.connect(
            lambda message: self.show_status(f'切换校准库失败: {message}'))

        for key, page in self.pages.items():
            if isinstance(page, StudioSetupPage):
                page.created.connect(self._on_database_created)
            if isinstance(page, StudioDatabasePage):
                # The page already opened the library through the handler;
                # publish the new context to every dependent view.
                page.loaded.connect(lambda _path: self._refresh_calibration_views())
            if isinstance(page, StudioLibraryBrowserPage):
                page.activated.connect(lambda path: self._on_browser_activated(path))
            if isinstance(page, VnaPage):
                page.status_changed.connect(self._on_vna_state)
                # The page has already polled once while being built; replay it
                # so the badge does not start out stale.
                self._on_vna_state(*page.connection_info())

    # -- navigation ------------------------------------------------------
    def _apply_nav_meta(self, key: str) -> None:
        """Mirror a page's group/label into the top title and breadcrumb.

        Both navigation paths must call this.  Clicking the tree fires
        ``_on_nav_changed`` while ``go()`` is used by in-page links and startup;
        updating the header in only one of them left the breadcrumb stuck on
        whatever page was active when the window opened.
        """
        meta = getattr(self, '_nav_meta', {}).get(key)
        if meta and hasattr(self, 'top_title'):
            group, label = meta
            self.top_title.setText(label)
            self.top_crumb.setText(group)

    def _on_nav_changed(self, current: QTreeWidgetItem | None, previous=None) -> None:
        if current is None:
            return
        key = current.data(0, Qt.ItemDataRole.UserRole)
        if key in self.views:
            self.stack.setCurrentWidget(self.views[key])
            self._apply_nav_meta(key)

    def go(self, key: str) -> None:
        """Show one page by key, keeping the tree in sync."""
        item = self.items.get(key)
        if item is None:
            return
        # The browser console and a finished acquisition can change the active
        # library through the backend without emitting our Qt signal.  Compare
        # fingerprints on navigation and broadcast only when it really changed,
        # so the desktop and the console never disagree about the library.
        self.state.library.ensure_current()
        self.nav.setCurrentItem(item)
        self.stack.setCurrentWidget(self.views[key])
        self._apply_nav_meta(key)

    # -- status ----------------------------------------------------------
    def show_status(self, message: str, timeout: int = 8000) -> None:
        text = str(message)
        if hasattr(self, 'top_message'):
            # A long "已连接 … 固件 …" line used to push the two badges out of
            # the bar; elide it and keep the full text in a tooltip.
            metrics = self.top_message.fontMetrics()
            shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, 330)
            self.top_message.setText(shown)
            self.top_message.setToolTip(text if shown != text else '')
            self._status_timer.start(max(1000, int(timeout)))
        self.statusBar().showMessage(text, timeout)

    def _refresh_model_label(self) -> None:
        settings = self.session.settings
        mode = '虚拟' if settings.mode == 'virtual' else '真实'
        unit = settings.unit
        axes = ', '.join(str(a) for a in unit.physical_axes)
        self.model_label.setText(
            f'{unit.label} · {settings.axis_count} 轴 '
            f'({", ".join(settings.active_axes)}) · 物理轴 {axes} · {mode}模式')

    def _update_cal_badge(self) -> None:
        """F-23: show the current calibration library and motion state in the bar."""
        if not hasattr(self, 'cal_badge'):
            return
        database = self.state.studio.get('database')
        state = str(self.state.studio.get('calibration_state', 'idle'))
        if database is not None and database.path.is_file():
            name = database.path.name
            motion = ' · 运动中' if state in ('running', 'paused') else ''
            self.cal_badge.setText(f'● 校准库 {name}{motion}')
            self.cal_badge.setStyleSheet(PILL_STYLES['ok'])
        else:
            self.cal_badge.setText('○ 校准库 无')
            self.cal_badge.setStyleSheet(PILL_STYLES['idle'])

    def _on_connection(self, connected: bool) -> None:
        self.state_pill.set_state('ok' if connected else 'idle',
                                  '已连接' if connected else '未连接')
        self._sync_top_badge()
        self._refresh_model_label()

    def _refresh_calibration_views(self) -> None:
        """Broadcast the active calibration to *every* dependent view.

        Previously this refreshed the Smith page alone, and that page kept its
        own 10 GHz default while a 5 GHz library was loaded — the chart came
        back empty and looked "not linked".  The context now publishes one
        event; each page aligns its frequency and invalidates its own stale
        conclusions (see ``on_library_changed`` on the individual pages).
        """
        self.state.library.notify_current()
        self._update_cal_badge()

    def _on_browser_activated(self, path: str) -> None:
        """The Calibration Browser made an old library active."""
        self.show_status(f'当前校准库已切换: {Path(path).name}，相关页面已同步')

    # -- menu actions ----------------------------------------------------
    def on_connect(self, mode: str | None = None) -> None:
        try:
            self.session.connect(mode=mode or self.session.mode)
        except Exception as exc:  # noqa: BLE001 - every failure is shown
            QMessageBox.warning(self, '连接控制器', f'{type(exc).__name__}: {exc}')
            self.show_status(f'连接失败: {exc}')
            return
        self.show_status('控制器已连接')
        self.go('hw-motion')

    def _auto_connect(self) -> None:
        """Honour "启动时自动连接" at startup.

        The setting was previously stored but only acted on silently: a failure
        produced one status-bar line the operator could easily miss, on a page
        they had not opened.  Now the attempt is logged and, on failure, the
        communication page is brought forward so the cause is in front of them.
        """
        target = self.session.settings.connection.ip
        self.show_status(f'自动连接 {target} …')
        try:
            self.session.connect()
        except Exception as exc:  # noqa: BLE001 - startup must not die on a bad link
            self.show_status(f'自动连接失败: {exc}')
            self.go('hw-comm')
            page = self.pages.get('hw-comm')
            if page is not None:
                page.state_label.setText(f'自动连接失败：{exc}')
            return
        self.show_status(f'已自动连接 {target}')
        self.go('hw-motion' if self.session.connected else 'hw-comm')

    def on_disconnect(self) -> None:
        try:
            self.session.disconnect()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, '断开控制器', str(exc))
        self.show_status('控制器已断开')

    def on_open_database(self) -> None:
        start = str(paths.calibration_dir())
        path, _ = QFileDialog.getOpenFileName(
            self, '打开校准库', start, 'HDF5 (*.h5 *.hdf5);;所有文件 (*)')
        if not path:
            return
        page = self.pages.get('studio-db')
        if isinstance(page, StudioDatabasePage):
            self.go('studio-db')
            # load() emits ``loaded`` -> _refresh_calibration_views(), which
            # broadcasts once to every page and refreshes the badge.
            page.load(path)

    def on_export_log(self) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self, '导出操作日志', str(paths.default_report_path('operations')),
            'JSON (*.json)')
        if not target:
            return
        operation_log.export(target)
        self.show_status(f'操作日志已导出: {target}')

    def on_open_web_console(self) -> None:
        """Open the browser console on the *same* session and library."""
        try:
            url = self.console.start()
        except Exception as exc:  # noqa: BLE001 - a busy port must not kill the app
            self.show_status(f'浏览器控制台启动失败: {exc}')
            QMessageBox.warning(self, '浏览器控制台',
                                f'无法启动浏览器控制台：\n{type(exc).__name__}: {exc}')
            return
        self.show_status(f'浏览器控制台: {url}')
        self.console_action.setText('关闭浏览器控制台')
        self.console_action.triggered.disconnect(self.on_open_web_console)
        self.console_action.triggered.connect(self.on_close_web_console)

    def on_close_web_console(self) -> None:
        self.console.stop()
        self.show_status('浏览器控制台已关闭')
        self.console_action.setText('打开浏览器控制台')
        self.console_action.triggered.disconnect(self.on_close_web_console)
        self.console_action.triggered.connect(self.on_open_web_console)

    def on_open_docs(self) -> None:
        docs = paths.app_root() / 'docs'
        if not docs.is_dir():
            QMessageBox.information(
                self, '文档', f'未找到文档目录：\n{docs}\n\n'
                              f'操作手册与硬件接入说明位于源码的 docs/ 目录。')
            return
        try:
            os.startfile(str(docs))  # noqa: S606 - Windows shell open
        except Exception as exc:  # noqa: BLE001
            QMessageBox.information(self, '文档', f'无法打开文档目录：{exc}')

    def on_about(self) -> None:
        QMessageBox.about(
            self, '关于 TunerLab',
            f'<b>{version.__app_name__} {version.__version__}</b><br>'
            f'{version.__app_title__}<br><br>'
            f'构建日期：{version.__release_date__}<br>'
            f'运行方式：{"安装包" if paths.is_frozen() else "源码"}<br>'
            f'配置文件：{self.store.path}<br>'
            f'日志目录：{paths.log_dir()}<br><br>'
            '软件急停不能替代硬件安全联锁。')

    # -- lifecycle -------------------------------------------------------
    def _on_database_created(self, path: str) -> None:
        self.go('studio-run')
        page = self.pages.get('studio-run')
        if isinstance(page, StudioRunPage):
            page.append(f'校准库已建立：{path}')
        self._refresh_calibration_views()

    def closeEvent(self, event):  # noqa: N802 - Qt override
        thread = self.state.studio.get('calibration_thread')
        if thread is not None and thread.is_alive():
            answer = QMessageBox.question(
                self, '任务进行中',
                '采集或验证仍在运行，退出会中断当前点（已完成点会保留）。确定退出？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            manager = self.state.studio.get('manager')
            if manager is not None:
                manager.cancel()
            # Cancellation is cooperative.  The worker may still be measuring
            # or writing HDF5; leave its connection and window alive until it
            # acknowledges the cancellation instead of racing disconnect().
            self.show_status('已请求停止任务，请等待当前点结束后再次关闭窗口。')
            event.ignore()
            return
        # Only tear down services once the exit has actually been accepted.
        self.console.stop()
        try:
            if self.session.connected:
                self.session.disconnect()
        except Exception:  # noqa: BLE001 - never block shutdown on a link error
            pass
        event.accept()


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m tunerlab_app`` and for the frozen exe.

    Supports two installation-check flags:

    * ``--version``  print the version and exit (useful for support scripts);
    * ``--smoke-test`` build the whole window, let the event loop spin for a
      moment and exit with 0 — proves a fresh install can start without a
      human clicking anything.
    """
    import argparse
    import sys
    from PySide6.QtWidgets import QApplication

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    args, _unknown = parser.parse_known_args(sys.argv[1:] if argv is None else argv)

    if args.version:
        print(f'{version.__app_name__} {version.__version__}')
        return 0

    paths.ensure_app_dirs()
    logger = setup_logging()
    # Must be armed before the first window: a native fault cannot be caught later.
    enable_crash_log()
    # A modal crash dialog would hang an unattended smoke test.
    install_exception_hook(logger, show_dialog=not args.smoke_test)

    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(version.__app_name__)
    app.setApplicationVersion(version.__version__)
    app.setOrganizationName('TunerLab')
    apply_theme(app)
    # One family for text, numbers and table cells alike.
    app.setStyleSheet(scaled_stylesheet(BASE_STYLESHEET, get_ui_scale()))
    app.setFont(px_font(13, 'regular'))

    from .runtime import SingleInstance
    guard = SingleInstance()
    # A smoke test is an installation check, not an operator session: it must be
    # able to run while the installed copy is open.
    if not args.smoke_test and not guard.acquire():
        return 0

    window = MainWindow(smoke_test=args.smoke_test)
    guard.bind_window(window)
    window.show()
    logger.info('%s %s started', version.__app_name__, version.__version__)
    if args.smoke_test:
        # The browser console is a shipped capability, so the installation check
        # proves its assets are really there — in a frozen build they are data
        # files next to the app, not imports, and a missing one would only
        # show up as a blank page on the operator's machine.
        try:
            from tuner.web.server import STATIC_DIR
            missing = [name for name in ('index.html', 'app.js', 'style.css')
                       if not (STATIC_DIR / name).is_file()]
            if missing:
                logger.error('smoke test: 控制台静态资源缺失 %s（目录 %s）',
                             missing, STATIC_DIR)
            else:
                logger.info('smoke test: 控制台静态资源齐全 (%s)', STATIC_DIR)
        except Exception as exc:  # noqa: BLE001 - report, do not abort the check
            logger.error('smoke test: 无法检查控制台静态资源: %s', exc)
        from PySide6.QtCore import QTimer
        def _finish() -> None:
            motion_page = type(window.pages.get('hw-motion')).__name__
            logger.info('smoke test: %d pages built (motion page %s), closing',
                        len(window.pages), motion_page)
            window.close()
            app.quit()
        QTimer.singleShot(1200, _finish)
    try:
        code = app.exec()
        if args.smoke_test:
            logger.info('smoke test exit code %s', code)
        return code
    finally:
        if not args.smoke_test:
            guard.release()
