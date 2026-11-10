# Load Pull Tuner 0.1 specification

## Scope and dependencies
Phase 0 models/API → Phase 1 motion + Phase 2 VNA → Phase 3 DB/calibration → Phase 4 RF/search → Phase 5 UI → Phase 6 integration. Motion and VNA can develop independently after API freeze; UI consumes their abstractions. Six agents implemented isolated modules and primary agent integrated them.

Shared X controls all probes together. Fixed horizontal offsets are configuration, never motion axes. Full calibration means Cartesian product of explicitly configured discrete X and Z samples within limits, not the continuum of physical positions. Position count Nx × Nz^P; a production grid must reflect collision constraints, instrument settling, storage and time budget. This release enforces axis bounds. Axes are independent by default: X motion is not gated on probe position, the planner does not retract probes on X's behalf, and X homing does not wait for probes. The legacy retract-before-X interlock can be restored with `TunerConfig(independent_axes=False)`. No real collision envelope or motor adapter is certified; removing the software interlock makes physical collision avoidance the operator's and the hardware interlock's responsibility.

## RF contract
Real positive common reference impedance Z0. Tuner port 1 faces DUT; port 2 faces external termination. S21 is matrix [1,0]. Γin=S11+S12*S21*ΓT/(1−S22*ΓT). Γin equals S11 only for matched termination ΓT=0. A fixture is oriented port1 DUT, port2 tuner; cascade fixture then tuner. Z=Z0(1+Γ)/(1−Γ). Search predicts at this defined reference plane. Nearest measured grid point satisfying tolerance is reported reachable; failure means no calibrated candidate meets tolerance, not a mathematical proof of global physical impossibility. Local interpolation is a proposal and requires measurement before acceptance.

HDF5 stores full complex two-port matrices, frequencies, target/actual coordinates, validity commit marker, metadata, validation. SQLite sidecar tracks calibration identity, task status and point failures. A new scan requires a new filename; existing files are never silently overwritten. Keep .h5 and .sqlite together.

## Acceptance
Both 2- and 3-probe Mock calibrations save/reload and tune. Limits, homing, emergency, timeouts, interrupted scan, failure retry, RF transforms and fixture orientation have automated coverage. RF verification is only true for a nonsimulated remeasurement that meets tolerance; mechanical confirmation cannot set it. Mock model is deterministic and does not demonstrate physical accuracy or instrument uncertainty.

## Known production gates
Real controller command set, unit mapping, travel limits, collision envelope, encoder tolerance, backlash strategy, emergency wiring, settling times, VNA model/channel/trace format/calibration and termination/fixture data must be supplied and qualified. Software emergency stop cannot replace a hardware interlock. Large datasets require streaming/tiled search beyond current in-memory loading. No certified hardware performance or broad operating-system packaging is claimed.

RF reference: https://scikit-rf-official.readthedocs.io/en/latest/tutorials/Networks.html (cascade and de-embedding); independent scikit-rf comparison is included in tests.
