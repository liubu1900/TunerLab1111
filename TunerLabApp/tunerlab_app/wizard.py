"""F-09: the five-step calibration wizard.

A standard wizard does not present "the calibration setup" as one long form.  It walks the
operator through five questions, and each question is answered on its own
screen:

① 参考面与仪器 — *where* is the reference plane, and which analyser?
② 运动与行程 — which motion source, how many probes, what travel box?
③ 频率方案 — one spot frequency, a sweep, harmonics or an explicit list?
④ 布点计划 — cartesian, equal-|Γ| rings or Smith-uniform, and how dense?
⑤ 检查与建库 — run the premise gate, then create the library.

The wizard exists for a practical reason as much as a design one: the single
form needed 923 px of height while the window offered 824 px, and Qt resolves
that shortfall by crushing the nested grids below their minimum height — the
rows then drew on top of each other.  One question per screen needs far less
height, and the operator sees only the parameters that matter for the step
they are on.

Every widget keeps the attribute name it had in the flat form, so the request
bodies (:meth:`StudioSetupPage._init_body` / :meth:`_plan_body`) and the
whole backend are untouched.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .fonts import px_font
from .ui import Card, field_grid, field_label, hint_label, page_header

__all__ = ['StepIndicator', 'WizardStep', 'Wizard']


#: The five steps, in order.  ``(key, title, subtitle)``.
WIZARD_STEPS = (
    ('reference', '参考面与仪器',
     '参考面决定测到的 Γ 属于哪个基准面；去嵌入文件必须覆盖库的全部频点，'
     '缺一个频点就必须中止，不得插值。'),
    ('motion', '运动与行程',
     '实机运动请申报真实行程：控制器出厂的 ±2e8 限位太宽，不能当保护用。'),
    ('frequency', '频率方案',
     '单频点校准默认 5 GHz，这是绝大多数 load-pull 场景；'
     '频带扫描需 起始 < 终止，列表模式仅本地网格。'),
    ('plan', '布点计划',
     '笛卡尔网格适合张量插值；等 Γ 圆分层可冷启动；Smith 均匀布点需已有实测点。'),
    ('check', '检查与建库',
     '建库前先过前提条件硬门禁；未通过时软件会拒绝建立校准库。'),
)


class StepIndicator(QWidget):
    """The ① ② ③ ④ ⑤ breadcrumb across the top of the wizard."""

    #: Emitted with the index of the step the operator clicked.
    step_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('StepBar')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.buttons: list[QPushButton] = []
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, (_key, title, _hint) in enumerate(WIZARD_STEPS):
            button = QPushButton(f'{index + 1}  {title}')
            button.setObjectName('StepButton')
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _checked=False, i=index:
                                   self.step_clicked.emit(i))
            self.group.addButton(button, index)
            layout.addWidget(button)
            self.buttons.append(button)

    def set_current(self, index: int) -> None:
        """Highlight the active step; the steps before it stay reachable."""
        for position, button in enumerate(self.buttons):
            button.setChecked(position == index)
            button.setProperty('state',
                               'current' if position == index
                               else 'done' if position < index else 'todo')
            # Re-polish so the state rule takes effect immediately.
            button.style().unpolish(button)
            button.style().polish(button)


class WizardStep(QWidget):
    """One screen of the wizard: a title, an explanation and a card body."""

    def __init__(self, index: int, title: str, hint: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        heading = QLabel(f'第 {index + 1} 步 · {title}')
        heading.setObjectName('StepTitle')
        heading.setFont(px_font(15, 'demibold'))
        layout.addWidget(heading)
        layout.addWidget(hint_label(hint))
        self.card = Card('')
        # The card hugs its content: a card that stretches to the bottom of the
        # screen pushes its last row far away from the ones above it, which
        # reads as three unrelated blocks instead of one panel.
        self.card.setSizePolicy(QSizePolicy.Policy.Preferred,
                                QSizePolicy.Policy.Maximum)
        layout.addWidget(self.card, 0)
        #: A second row of cards, for a step that needs more than one panel.
        self.extra = QVBoxLayout()
        self.extra.setContentsMargins(0, 0, 0, 0)
        self.extra.setSpacing(14)
        layout.addLayout(self.extra, 0)
        # Whatever height is left over belongs below the cards, not inside them.
        layout.addStretch(1)

    def body(self):
        """The card's inner layout — where the step's controls go."""
        return self.card.body

    def add_card(self, card: QWidget) -> None:
        """Put an extra card below the main one."""
        card.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Maximum)
        self.extra.addWidget(card, 0)


class Wizard(QWidget):
    """A step indicator, a stack of steps and a back / next bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('Wizard')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.indicator = StepIndicator()
        layout.addWidget(self.indicator)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.back_button = QPushButton('← 上一步')
        self.next_button = QPushButton('下一步 →')
        self.next_button.setProperty('accent', True)
        bar.addWidget(self.back_button)
        bar.addStretch()
        bar.addWidget(self.next_button)
        layout.addLayout(bar)

        self._index = 0
        self.back_button.clicked.connect(lambda: self.go(self._index - 1))
        self.next_button.clicked.connect(lambda: self.go(self._index + 1))
        self.indicator.step_clicked.connect(self.go)

    # -- steps -----------------------------------------------------------
    def add_step(self, step: WizardStep) -> None:
        self.stack.addWidget(step)

    def count(self) -> int:
        return self.stack.count()

    def current(self) -> int:
        return self._index

    def go(self, index: int) -> None:
        """Show a step; the indicator follows and the buttons adapt."""
        index = max(0, min(int(index), self.stack.count() - 1))
        self._index = index
        self.stack.setCurrentIndex(index)
        self.indicator.set_current(index)
        self.back_button.setEnabled(index > 0)
        last = index == self.stack.count() - 1
        self.next_button.setEnabled(not last)
        self.next_button.setText('下一步 →')

    def set_next_text(self, text: str) -> None:
        self.next_button.setText(text)
