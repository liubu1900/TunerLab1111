"""Mock-first desktop workflow. Run with python -m tuner.ui."""
from __future__ import annotations
import json
from dataclasses import asdict
import sys
from datetime import datetime
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QPointF
from PySide6.QtGui import QPainter, QColor, QPen, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QFormLayout, QLabel, QPushButton, QComboBox, QSpinBox,
    QDoubleSpinBox, QLineEdit, QTextEdit, QListWidget, QStackedWidget,
    QProgressBar, QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem)
from .models import TunerConfig, comparable_config_dict
from .motion import MotionController, MotionPlanner
from .vna import MockVNAController
from .database import CalibrationDatabase
from .calibration import CalibrationManager
from .search import GammaSearchEngine
from .rf import RFNetworkEngine
from .validation import repeatability_report
from .smith_view import SmithView
from .smith_interaction import SmithViewInteraction


class Worker(QThread):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(str)
    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
    def run(self):
        try:
            self.succeeded.emit(self.operation(self.progress.emit))
        except Exception as exc:
            self.failed.emit(f'{type(exc).__name__}: {exc}')


def smith_gamma_from_pixel(x, y, width, height):
    """Map a widget pixel to a reflection coefficient.

    Returns ``None`` outside the unit circle (or for a degenerate widget), so a
    click can never request a non-passive impedance.  Kept as a pure function
    so the geometry can be regression-tested without a Qt event loop.
    """
    radius = min(width - 36, height - 36) / 2
    if radius <= 0:
        return None
    gamma = complex((x - width / 2) / radius, (height / 2 - y) / radius)
    return gamma if abs(gamma) <= 1.0 else None


class SmithChart(QWidget, SmithViewInteraction):
    """Constant R and X circles mapped analytically into normalized Gamma.

    The viewer is interactive: hovering shows the cursor position and a left
    click (in zoom mode, with no drag) emits :attr:`picked` with the chosen Γ.
    ``points``/``target``/``selected`` keep their original read-only semantics.

    Zoom/pan (F-24 + follow-up): an explicit **缩放 / 移动** mode toggle drives
    the mouse — in *缩放* a left-drag draws a rubber-band that zooms to the
    chosen region (a plain click still picks Γ), in *移动* a left-drag pans.
    The wheel always magnifies about the cursor, middle/Ctrl+left always pans,
    and a double click restores the 1:1 chart.  All geometry goes through
    :class:`~tuner.smith_view.SmithView`, so ``zoom == 1`` is bit-for-bit the
    original layout and click-to-pick keeps working at any magnification.
    """

    #: Pixel inset used for the unit circle (kept identical to the legacy map).
    _INSET = 36.0

    picked = Signal(object)
    hovered = Signal(object)
    right_picked = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = np.array([], complex)
        self.target = None
        self.selected = None
        self.picked_point = None
        self.hover_point = None
        self.view = SmithView()
        self._init_smith_interaction()
        self.setMinimumSize(420, 360)
        self.setMouseTracking(True)
        self._apply_mode_cursor()
        self.setToolTip('左键拖框放大局部 · 「缩放/移动」按钮切换模式 · '
                        '滚轮以光标为中心缩放 · 中键或 Ctrl+左键拖拽平移 · '
                        '双击复位 · 圆图内单击左键点选目标 Γ')

    # -- zoom/pan public API --------------------------------------------
    def zoom_in(self) -> None:
        if self.view.zoom_step(1, self.width(), self.height(), self._INSET):
            self.update()

    def zoom_out(self) -> None:
        if self.view.zoom_step(-1, self.width(), self.height(), self._INSET):
            self.update()

    def reset_view(self) -> None:
        self.view.reset()
        self.update()

    def zoom_text(self) -> str:
        return f'×{self.view.zoom:.1f}' if self.view.zoomed else '×1.0'

    # -- interaction hooks ----------------------------------------------
    def _smith_pick(self, gamma: complex) -> None:
        self.picked_point = gamma
        self.picked.emit(gamma)
        self.update()

    def _smith_right_pick(self, gamma: complex) -> None:
        self.right_picked.emit(gamma)

    # -- event dispatch (delegated to SmithViewInteraction) -------------
    def _gamma_at(self, event):
        gamma = self.view.to_gamma(event.position().x(), event.position().y(),
                                   self.width(), self.height(), self._INSET)
        # Preserve the original contract: never pick outside the unit circle.
        return gamma if abs(gamma) <= 1.0 else None

    def wheelEvent(self, event):
        if self._smith_wheel(event):
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event):
        if self._smith_press(event):
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._smith_move(event):
            return
        self.hover_point = self._gamma_at(event)
        self.hovered.emit(self.hover_point)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._smith_release(event):
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._smith_double(event):
            return
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        self.hover_point = None
        self.hovered.emit(None)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor('#101b2b'))
        radius = self.view.radius(self.width(), self.height(), self._INSET)
        def xy(g):
            x, y = self.view.to_pixel(g, self.width(), self.height(), self._INSET)
            return QPointF(x, y)
        def curve(gs):
            previous = None
            for g in gs:
                if np.isfinite(g.real) and np.isfinite(g.imag) and abs(g) <= 1.001:
                    point = xy(g)
                    if previous is not None: p.drawLine(previous, point)
                    previous = point
                else: previous = None
        p.setPen(QPen(QColor('#35506b'), 1))
        p.drawEllipse(xy(0j), radius, radius)
        p.drawLine(xy(-1+0j), xy(1+0j))
        x = np.linspace(-70, 70, 1600)
        for r in (0, .2, .5, 1, 2, 5):
            z = r+1j*x
            curve((z-1)/(z+1))
        for reactance in (.2, .5, 1, 2, 5, -.2, -.5, -1, -2, -5):
            z = np.linspace(0, 70, 700)+1j*reactance
            curve((z-1)/(z+1))
        p.setPen(QPen(QColor('#42c5b7'), 3))
        for g in self.points[::max(1, len(self.points)//2500)]:
            if np.isfinite(g): p.drawPoint(xy(g))
        for g, color in ((self.target, '#ffbc5a'), (self.selected, '#ff6582')):
            if g is not None and np.isfinite(g):
                p.setPen(QPen(QColor(color), 3))
                p.drawEllipse(xy(g), 6, 6)
        if self.hover_point is not None:
            p.setPen(QPen(QColor('#7f9cb9'), 1))
            hp = xy(self.hover_point)
            p.drawLine(hp.x() - 7, hp.y(), hp.x() + 7, hp.y())
            p.drawLine(hp.x(), hp.y() - 7, hp.x(), hp.y() + 7)
        if self.picked_point is not None:
            p.setPen(QPen(QColor('#c9a7ff'), 2))
            gp = xy(self.picked_point)
            p.drawEllipse(gp, 9, 9)
            p.drawLine(gp.x() - 13, gp.y(), gp.x() + 13, gp.y())
            p.drawLine(gp.x(), gp.y() - 13, gp.x(), gp.y() + 13)
        if self.view.zoomed:
            # Small unobtrusive badge so the operator can tell a magnified
            # local view from the full chart, and knows how to get back.
            badge = f'{self.zoom_text()} · 双击复位'
            p.setFont(QFont('Segoe UI', 9))
            metrics = p.fontMetrics()
            w = metrics.horizontalAdvance(badge) + 14
            p.setPen(Qt.PenStyle.NoPen)
            badge_bg = QColor('#101b2b')
            badge_bg.setAlpha(210)
            p.setBrush(badge_bg)
            p.drawRoundedRect(8, 8, w, 22, 4, 4)
            p.setPen(QColor('#7f9cb9'))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawText(15, 23, badge)
        self.paint_rubber_band(p)
        p.end()


class MainWindow(QMainWindow):
    pages = ['Dashboard', 'Hardware Control', 'Calibration Setup', 'Calibration Run',
             'Calibration Database', 'Smith Chart Viewer', 'Impedance Tuning', 'Validation Report']
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Load Pull Tuner • Mock Calibration Studio')
        self.resize(1180, 790)
        self.worker = None
        self.manager = None
        self.data = None
        self.search_engine = None
        self.last_candidates = []
        self.setStyleSheet('QMainWindow,QWidget {background:#142032;color:#e7eef7;} '
            'QPushButton {background:#245b72;padding:9px;border-radius:4px;} '
            'QPushButton:disabled {background:#354052;color:#8993a0;} '
            'QLineEdit,QSpinBox,QDoubleSpinBox,QComboBox,QTextEdit,QTableWidget {background:#0d1726;padding:6px;} '
            'QListWidget {background:#0d1726;border:0;} QListWidget::item {padding:13px;} '
            'QListWidget::item:selected {background:#245b72;}')
        root = QWidget(); self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        self.nav = QListWidget(); self.nav.addItems(self.pages); self.nav.setMaximumWidth(230)
        self.stack = QStackedWidget()
        sidebar = QVBoxLayout(); brand = QLabel('TunerLab\n🟢 MOCK online'); brand.setStyleSheet('font-size:22px;padding:12px;color:#42c5b7;'); sidebar.addWidget(brand); sidebar.addWidget(self.nav); layout.addLayout(sidebar); layout.addWidget(self.stack, 1)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.layouts = []
        for title in self.pages:
            page = QWidget(); box = QVBoxLayout(page)
            label = QLabel(title); label.setStyleSheet('font-size:25px;font-weight:600;padding:10px 0;')
            box.addWidget(label); self.stack.addWidget(page); self.layouts.append(box)
        self.build_dashboard(); self.build_hardware(); self.build_setup(); self.build_run()
        self.build_database(); self.build_smith(); self.build_tuning(); self.build_validation()
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage('SIMULATION ONLY • no motor transport is connected')
    def button(self, text, callback, layout):
        b = QPushButton(text)
        b.clicked.connect(lambda: self.guard(callback))
        layout.addWidget(b)
        return b
    def guard(self, callback):
        try: callback()
        except Exception as exc:
            self.log(f'{type(exc).__name__}: {exc}')
            QMessageBox.warning(self, 'Operation failed', str(exc))
    def log(self, message):
        self.run_log.append(f'{datetime.now():%H:%M:%S}  {message}')
        self.statusBar().showMessage(str(message))
    def idle(self):
        if self.worker is not None and self.worker.isRunning(): raise RuntimeError('Wait for the active worker, or pause calibration first')
    def hardware(self):
        if not hasattr(self, 'motion'): raise RuntimeError('Initialize virtual hardware first')
    def build_dashboard(self):
        box = self.layouts[0]
        box.addWidget(QLabel('Shared X • independent probes (3-axis: Z1/Z2 • 4-axis: Z1/Z2/Z3)\nFull two-port S-parameters • checkpointed HDF5 • DUT-plane Gamma search'))
        self.summary = QLabel('No calibration loaded. Start with Hardware Control, then Calibration Setup.')
        self.summary.setWordWrap(True); box.addWidget(self.summary)
        box.addWidget(QLabel('SIMULATED DATA ONLY\nMechanical positioning success does not establish RF impedance accuracy.\nPort convention: DUT → tuner port 1; port 2 terminated in 50 Ω.'))
        box.addStretch()
    def build_hardware(self):
        box = self.layouts[1]; form = QFormLayout(); box.addLayout(form)
        self.probes = QComboBox(); self.probes.addItems(['2 probes: X, Z1, Z2', '3 probes: X, Z1, Z2, Z3'])
        form.addRow('Mechanical structure', self.probes)
        self.offsets = QLineEdit('0, 2'); form.addRow('Fixed probe offsets (Mock mm)', self.offsets)
        self.probes.currentIndexChanged.connect(self.on_probes_changed)
        self.button('Initialize virtual hardware and home', self.initialize_hardware, box)
        self.coordinates = QLabel('Axes not initialized'); box.addWidget(self.coordinates)
        self.axis = QComboBox(); self.axis.addItems(['X','Z1','Z2','Z3'])
        self.on_probes_changed(self.probes.currentIndex())
        self.delta = QDoubleSpinBox(); self.delta.setRange(-10,10); self.delta.setValue(.1); self.delta.setDecimals(3)
        form2 = QFormLayout(); form2.addRow('Jog axis', self.axis); form2.addRow('Jog increment (Mock mm)', self.delta); box.addLayout(form2)
        self.button('Jog with safety planner', self.jog, box)
        self.button('Safe retract all Z', self.retract, box)
        self.button('EMERGENCY STOP', self.estop, box)
        self.button('Reset emergency stop and re-home', self.reset_home, box)
        box.addStretch()
    def on_probes_changed(self, index):
        """Keep the offsets and the axis list in step with the probe count.

        A 3-axis tuner (2 probes) has X/Z1/Z2, a 4-axis tuner (3 probes) adds Z3.
        """
        self.offsets.setText('0, 2' if index == 0 else '0, 2, 4')
        axes = ['X','Z1','Z2'] if index == 0 else ['X','Z1','Z2','Z3']
        current = self.axis.currentText()
        self.axis.clear(); self.axis.addItems(axes)
        if current in axes: self.axis.setCurrentText(current)

    def initialize_hardware(self):
        self.idle()
        count = self.probes.currentIndex()+2
        self.config = TunerConfig(probes=count, fixed_offsets=tuple(float(v) for v in self.offsets.text().split(',')))
        self.motion = MotionController(self.config)
        self.motion.initialize(); self.motion.home()
        self.planner = MotionPlanner(self.config, self.motion)
        self.vna = MockVNAController(self.config, self.motion, np.linspace(1e9, 20e9, 41))
        self.vna.initialize(); self.manager = None
        self.data=None; self.search_engine=None; self.last_candidates=[]
        self.candidates.setRowCount(0); self.chart.points=np.array([],complex)
        self.chart.target=None; self.chart.selected=None; self.chart.update()
        self.summary.setText('Virtual hardware reinitialized; load or create a matching calibration.')
        self.update_position(); self.log('Virtual hardware initialized and homed')
    def update_position(self):
        self.coordinates.setText('  |  '.join(f'{axis}: {value:.4f}' for axis,value in zip(self.config.axes,self.motion.read_position())))
    def jog(self):
        self.idle(); self.hardware()
        if self.axis.currentText() not in self.config.axes: raise ValueError('Axis absent for this structure')
        p = self.motion.read_position().copy(); p[self.config.axes.index(self.axis.currentText())] += self.delta.value()
        self.planner.move_to(p); self.update_position()
    def retract(self):
        self.idle(); self.hardware(); self.motion.retract(); self.update_position()
    def estop(self):
        self.hardware(); self.motion.emergency_stop()
        if self.manager: self.manager.pause()
        self.log('Emergency stop latched; reset and re-home required')
    def reset_home(self):
        self.idle(); self.hardware(); self.motion.reset_emergency(); self.motion.home(); self.update_position()
    def build_setup(self):
        box = self.layouts[2]; form = QFormLayout(); box.addLayout(form)
        self.x_count = QSpinBox(); self.x_count.setRange(2,51); self.x_count.setValue(9)
        self.z_count = QSpinBox(); self.z_count.setRange(2,15); self.z_count.setValue(4)
        form.addRow('X grid points (0–10 mm)',self.x_count); form.addRow('Points per Z (0–1 mm)',self.z_count)
        self.path = QLineEdit(str(Path.cwd()/'calibrations'/f'mock_{datetime.now():%Y%m%d_%H%M%S}.h5'))
        form.addRow('New calibration file',self.path)
        box.addWidget(QLabel('All Cartesian positions are scanned, including simultaneous Z insertion.\nMock limits and fixed offsets are illustrative configuration, not real hardware specifications.\nVNA: 1–20 GHz, 41 frequency points; S11, S21, S12, S22 at every position.'))
        self.grid_label = QLabel(); box.addWidget(self.grid_label)
        self.x_count.valueChanged.connect(self.update_grid_count); self.z_count.valueChanged.connect(self.update_grid_count)
        self.probes.currentIndexChanged.connect(self.update_grid_count); self.update_grid_count()
        self.button('Create new calibration task', self.prepare_calibration, box); box.addStretch()
    def update_grid_count(self):
        self.grid_label.setText(f'{self.x_count.value()*self.z_count.value()**(self.probes.currentIndex()+2):,} mechanical positions')
    def prepare_calibration(self):
        self.idle(); self.hardware()
        positions = self.config.grid(np.linspace(*self.config.x_limits,self.x_count.value()), np.linspace(*self.config.z_limits,self.z_count.value()))
        self.database = CalibrationDatabase(self.path.text())
        self.manager = CalibrationManager(self.config,self.motion,self.vna,self.database)
        self.manager.prepare(positions)
        self.log(f'Created {len(positions)}-position simulated task'); self.nav.setCurrentRow(3)
    def build_run(self):
        box=self.layouts[3]; self.progress=QProgressBar(); box.addWidget(self.progress)
        row=QHBoxLayout(); box.addLayout(row)
        self.button('Run / resume',self.run_calibration,row)
        self.button('Pause at next checkpoint',self.pause_calibration,row)
        self.button('Cancel task',self.cancel_calibration,row)
        self.run_log=QTextEdit(); self.run_log.setReadOnly(True); box.addWidget(self.run_log)
    def run_calibration(self):
        self.idle()
        if self.manager is None: raise RuntimeError('Create or reopen a calibration task first')
        self.manager.resume()
        self.worker=Worker(lambda progress: self.manager.run(progress=progress), self)
        self.worker.progress.connect(lambda i,n: self.progress.setValue(round(100*(i+1)/max(1,n))))
        self.worker.succeeded.connect(self.calibration_finished)
        self.worker.failed.connect(self.log)
        self.worker.start(); self.log('Calibration running in background')
    def pause_calibration(self):
        if self.manager: self.manager.pause(); self.log('Pause requested; waiting for point checkpoint')
    def cancel_calibration(self):
        if self.manager: self.manager.cancel(); self.log('Cancel requested; valid checkpoints retained')
    def calibration_finished(self, state):
        self.log(f'Calibration state: {state}')
        self.update_position()
        if str(state) in ('completed','completed_with_errors'):
            self.load_database_path(str(self.database.path))
    def build_database(self):
        box=self.layouts[4]
        self.button('Open HDF5 database / resume task',self.open_database,box)
        self.db_info=QTextEdit(); self.db_info.setReadOnly(True); box.addWidget(self.db_info)
    def open_database(self):
        self.idle()
        path,_=QFileDialog.getOpenFileName(self,'Open calibration','','HDF5 (*.h5 *.hdf5)')
        if path: self.load_database_path(path, resume=True)
    def load_database_path(self,path,resume=False):
        db=CalibrationDatabase(path); data=db.load()
        self.database=db; self.data=data
        self.search_engine=GammaSearchEngine(data,z0=float(data['metadata'].get('config',{}).get('z0',50))) if np.any(data['valid']) else None
        self.last_candidates=[]; self.candidates.setRowCount(0)
        valid=int(np.count_nonzero(data['valid']))
        self.summary.setText(f"{path}\n{valid}/{len(data['valid'])} valid positions • {len(data['frequencies'])} frequencies\nSIMULATED = {data['metadata'].get('simulated')}")
        self.db_info.setText(json.dumps(data['metadata'],indent=2)+f'\nValid: {valid}/{len(data["valid"])}')
        if resume:
            self.hardware()
            self.require_matching_config()
            self.vna=MockVNAController(self.config,self.motion,data['frequencies']); self.vna.initialize()
            self.manager=CalibrationManager(self.config,self.motion,self.vna,db)
        self.frequency.setRange(data['frequencies'][0]/1e9,data['frequencies'][-1]/1e9)
        self.refresh_chart(); self.log(f'Loaded calibration: {valid} valid points')
    def build_smith(self):
        box=self.layouts[5]
        self.chart=SmithChart(); box.addWidget(self.chart,1)
        box.addWidget(QLabel('Teal: valid database DUT-plane loads • amber: target • pink: selected prediction\nWheel: zoom · middle/Ctrl+left drag: pan · double click: reset view'))
        zoom_row=QHBoxLayout()
        self.button('缩放',lambda: self.chart.smith_set_mode('zoom'),zoom_row)
        self.button('移动',lambda: self.chart.smith_set_mode('pan'),zoom_row)
        self.button('放大',self.chart.zoom_in,zoom_row)
        self.button('缩小',self.chart.zoom_out,zoom_row)
        self.button('复位视图',self.chart.reset_view,zoom_row)
        zoom_row.addStretch()
        box.addLayout(zoom_row)
        self.button('Refresh at tuning frequency',self.refresh_chart,box)
    def refresh_chart(self):
        if self.data is None: return
        if self.search_engine is None:
            self.chart.points=np.array([],complex)
        else:
            self.chart.points=self.search_engine.gammas(self.frequency.value()*1e9)
        self.chart.update()
    def build_tuning(self):
        box=self.layouts[6]; form=QFormLayout(); box.addLayout(form)
        self.frequency=QDoubleSpinBox(); self.frequency.setRange(1,20); self.frequency.setValue(10); self.frequency.setDecimals(6)
        self.resistance=QDoubleSpinBox(); self.resistance.setRange(0,1e6); self.resistance.setValue(50)
        self.reactance=QDoubleSpinBox(); self.reactance.setRange(-1e6,1e6)
        self.tolerance=QDoubleSpinBox(); self.tolerance.setRange(.00001,1); self.tolerance.setValue(.08); self.tolerance.setDecimals(5)
        form.addRow('Frequency (GHz)',self.frequency); form.addRow('Target R (Ω)',self.resistance); form.addRow('Target X (Ω)',self.reactance); form.addRow('Reachability tolerance |ΔΓ|',self.tolerance)
        self.button('Search nearest calibrated candidates',self.search_target,box)
        self.candidates=QTableWidget(0,5); self.candidates.setHorizontalHeaderLabels(['Index','Mechanical position','Predicted Γ','Predicted Z / Ω','|ΔΓ|'])
        self.candidates.horizontalHeader().setStretchLastSection(True); box.addWidget(self.candidates)
        self.button('Move to selected calibrated position',self.move_candidate,box)
        self.button('Measure current mock impedance vs target',self.verify_target,box)
        self.tuning_result=QLabel('Prediction only. RF verification is performed separately.'); self.tuning_result.setWordWrap(True); box.addWidget(self.tuning_result)
    def search_target(self):
        self.idle()
        if self.search_engine is None: raise RuntimeError('Load a completed or partial calibration first')
        target=complex(self.resistance.value(),self.reactance.value())
        gamma=RFNetworkEngine.z_to_gamma(target,self.search_engine.z0)
        results=self.search_engine.search(self.frequency.value()*1e9,gamma,k=5,tolerance=self.tolerance.value())
        self.last_candidates=results; self.candidates.setRowCount(len(results))
        for row,result in enumerate(results):
            for col,value in enumerate((result.index,np.array2string(result.position,precision=4),str(result.gamma),str(result.impedance),f'{result.error:.6g}')):
                self.candidates.setItem(row,col,QTableWidgetItem(str(value)))
        self.candidates.selectRow(0)
        if results:
            self.chart.target=complex(gamma); self.chart.selected=results[0].gamma
            self.tuning_result.setText(f'Nearest prediction |ΔΓ|={results[0].error:.6g}; within sampled tolerance: {results[0].reachable}. No continuous-coverage guarantee.')
        self.refresh_chart()
    def move_candidate(self):
        self.idle(); self.hardware()
        row=self.candidates.currentRow()
        if row < 0 or row >= len(self.last_candidates): raise RuntimeError('Search and select a candidate first')
        result=self.last_candidates[row]
        self.require_matching_config()
        self.planner.move_to(result.position); self.update_position()
        self.tuning_result.setText(f'Mock mechanical positioning completed. Predicted Γ={result.gamma}; |ΔΓ|={result.error:.6g}. RF impedance not yet measured.')
    def require_matching_config(self):
        self.hardware()
        if self.data is None: raise RuntimeError('Load a calibration first')
        stored=self.data['metadata']
        if bool(stored.get('simulated')) != bool(self.config.simulated):
            raise ValueError('Calibration/hardware simulation provenance mismatch')
        if json.dumps(comparable_config_dict(stored.get('config',{})),sort_keys=True) != json.dumps(comparable_config_dict(self.config),sort_keys=True):
            raise ValueError('Full hardware configuration differs from calibration')
    def verify_target(self):
        self.idle(); self.require_matching_config()
        if self.search_engine is None: raise RuntimeError('No valid calibration points')
        m=self.vna.measure()
        f=self.frequency.value()*1e9
        if not m.frequencies[0] <= f <= m.frequencies[-1]: raise ValueError('Target outside VNA frequency grid')
        measured=RFNetworkEngine.dut_gamma(m.s)
        gamma=complex(np.interp(f,m.frequencies,measured.real),np.interp(f,m.frequencies,measured.imag))
        target=RFNetworkEngine.z_to_gamma(complex(self.resistance.value(),self.reactance.value()),self.search_engine.z0)
        error=float(abs(gamma-target))
        self.validation_report={'kind':'impedance_verification','simulated':bool(m.simulated),
            'rf_hardware_verified':False,'frequency_hz':f,'measured_gamma_real':gamma.real,
            'measured_gamma_imag':gamma.imag,'vector_error':error,
            'within_tolerance':error<=self.tolerance.value(),
            'interpretation':'Simulated RF verification only; no physical RF accuracy claim.'}
        self.database.save_validation(self.validation_report)
        self.validation_text.setText(json.dumps(self.validation_report,indent=2))
        self.tuning_result.setText(f'Simulated RF measurement: Γ={gamma}; Z={RFNetworkEngine.gamma_to_z(gamma,self.search_engine.z0)} Ω; |ΔΓ|={error:.6g}. Physical RF verification: false.')
    def build_validation(self):
        box=self.layouts[7]
        box.addWidget(QLabel('Acquire 3 A→B→A cycles and compare complex S11/S21 across the full frequency grid.\nA and B are first and last valid calibrated positions. All results remain simulated.'))
        self.button('Run mock vector-repeatability validation',self.validate_repeatability,box)
        self.validation_text=QTextEdit(); self.validation_text.setReadOnly(True); box.addWidget(self.validation_text)
        self.button('Save validation JSON',self.save_validation,box)
        self.validation_report=None
    def validate_repeatability(self):
        self.idle(); self.hardware()
        self.require_matching_config()
        indices=np.flatnonzero(self.data['valid'])
        if len(indices)<2: raise ValueError('Two valid positions required')
        if not np.array_equal(self.vna.frequencies,self.data['frequencies']):
            raise ValueError('Current VNA frequency grid differs; reopen database to align the mock VNA')
        def operation(progress):
            a,b=self.data['positions'][indices[[0,-1]]]
            report=repeatability_report(self.planner,self.vna,a,b,repeats=3)
            self.database.save_validation(report)
            return report
        self.worker=Worker(operation,self)
        self.worker.succeeded.connect(self.validation_finished); self.worker.failed.connect(self.log); self.worker.start()
    def validation_finished(self,report):
        self.validation_report=report; self.validation_text.setText(json.dumps(report,indent=2)); self.update_position()
    def save_validation(self):
        if self.validation_report is None: raise RuntimeError('Run validation first')
        path,_=QFileDialog.getSaveFileName(self,'Save validation report','validation.json','JSON (*.json)')
        if path: Path(path).write_text(json.dumps(self.validation_report,indent=2),encoding='utf-8'); self.log('Validation report saved')
    def closeEvent(self,event):
        if self.worker is not None and self.worker.isRunning():
            if self.manager: self.manager.cancel()
            self.statusBar().showMessage('Stopping background operation; close again when it finishes')
            event.ignore(); return
        event.accept()


def main():
    app=QApplication.instance() or QApplication(sys.argv)
    window=MainWindow(); window.show()
    return app.exec()

if __name__ == '__main__':
    raise SystemExit(main())
