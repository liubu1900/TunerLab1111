"""Keep one QApplication alive until every desktop test widget is destroyed."""
import gc
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from shiboken6 import delete, isValid


@pytest.fixture(scope='session', autouse=True)
def desktop_application_lifetime():
    app = QApplication.instance() or QApplication([])
    yield app
    # close() only hides a QWidget. Destroy native widgets while the application
    # is still alive, including widgets retained by Python signal cycles.
    for widget in QApplication.topLevelWidgets():
        if isValid(widget):
            widget.close()
            delete(widget)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    app.processEvents()
    delete(app)
