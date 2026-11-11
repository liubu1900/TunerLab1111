"""One shared installation selector for the three hardware pages."""
from PySide6.QtWidgets import QWidget, QHBoxLayout, QComboBox, QLabel, QMessageBox


class InstallationBar(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        row = QHBoxLayout(self)
        self.selector = QComboBox()
        self.label = QLabel()
        self.label.setWordWrap(True)
        row.addWidget(self.selector)
        row.addWidget(self.label, 1)
        self.selector.currentIndexChanged.connect(self.select)
        session.settings_changed.connect(self.refresh)
        session.connection_changed.connect(self.refresh)
        self.refresh()

    def refresh(self, *_):
        self.selector.blockSignals(True)
        self.selector.clear()
        for key, unit in self.session.settings.units.items():
            self.selector.addItem(unit.label, key)
        self.selector.setCurrentIndex(self.selector.findData(self.session.settings.active_unit))
        self.selector.setEnabled(self.selector.count() > 1)
        self.selector.blockSignals(False)
        self.label.setText(self.session.context_text())

    def select(self):
        key = self.selector.currentData()
        if key and key != self.session.settings.active_unit:
            try:
                self.session.set_active_unit(key)
            except Exception as exc:
                QMessageBox.warning(self, '切换 Tuner', str(exc))
                self.refresh()
