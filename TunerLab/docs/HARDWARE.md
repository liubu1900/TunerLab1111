# Hardware integration

## Zmotion controller (real, commissioned)

The Tuner is driven by a Zmotion (正运动) ZMC controller over ethernet.

| Item | Value |
|---|---|
| Controller address | `192.168.0.11` (factory default) |
| Firmware | `2.9600` |
| Host address | `192.168.0.1/24` (same subnet, required) |
| SDK | `zauxdll.dll` 64-bit, official "Windows Python (64位)" package |
| SDK location | `TunerLab/vendor/zmotion/` |
| Header reference | `TunerLab/vendor/zmotion/zauxdll2.h` |

`ZAux_OpenEth` locates the controller with a **UDP broadcast** on the local subnet, so
a TCP port scan of the controller shows nothing useful; the link is established through
the vendor library, not through a raw socket.

### Commanded parameter set (read back from the controller)

| Axis | Controller axis | ATYPE | UNITS (pulses/unit) | SPEED | ACCEL | DECEL | CREEP | DATUM_IN | HOME |
|---|---|---|---|---|---|---|---|---|---|
| X  | 0 | 4 | 2500 | 10 | 10 | 10 | 100 | 16 | 已回零 |
| Z1 | 1 | 4 | 10000 | 10 | 10 | 10 | 100 | 17 | 已回零 |
| Z2 | 2 | 4 | 10000 | 10 | 10 | 10 | 100 | 18 | 已回零 |
| Z3 | 3 | 4 | 10000 | 10 | 10 | 10 | 100 | 19 | 已回零 |

ATYPE 4 = encoder feedback axis. Every axis carries its own origin input channel.

### Tuner models (3-axis and 4-axis)

The controller always exposes **four** axes at fixed indices (X=0, Z1=1, Z2=2,
Z3=3).  Two tuner models are supported, and they differ only in *which* of those
axes belong to the tuner:

| Model | Axes | Probes | Calibration grid size |
|---|---|---|---|
| 3-axis | X, Z1, Z2 | 2 | `x_count × z_count²` |
| 4-axis | X, Z1, Z2, Z3 | 3 | `x_count × z_count³` |

The model is a configuration value (`axis_count` in the settings file, selectable
on the *Communication* page) rather than a compile-time constant, because:

* the **controller axis indices never change** — a stored calibration or a tuned
  `UNITS` value stays valid when the model is switched, and
* a 3-axis machine may still be wired to four controller axes; the software
  simply commands only the axes of the active model.

Parameters of an axis that leaves the model are kept in the settings file, so
switching back and forth does not lose a tuned axis.

`FS_LIMIT` / `RS_LIMIT` are still at the controller's factory defaults
(`±200000000`); the physical travel of the machine has not been surveyed yet, so these
must be set to the real mechanical limits before the machine is used for production
moves.

### Units

`UNITS` is the pulse equivalent (pulses per user unit). Distances passed to
`MOVE`/`MOVEABS` and positions reported by `DPOS`/`MPOS` are in **user units**
(millimetres on this machine); the controller converts internally with
`pulses = distance × UNITS`. The software therefore speaks user units end to end and
never applies `UNITS` itself.

**This does not hold on the machine as it currently stands — see the scale defect
below.** A commanded distance cannot be trusted until the scale is corrected.

### Scale defect: the command scale does not match the feedback scale

`DPOS` (commanded position) and `MPOS` (encoder feedback) do **not** describe the same
distance on this machine. Commanding `+0.005` user units produces an encoder-reported
travel of roughly 0.2–0.48 user units:

| Axis | UNITS | Commanded | Encoder moved | Ratio |
|---|---|---|---|---|
| X  | 2500  | 0.005 | 0.20 – 0.32 | ≈ 40 – 67 |
| Z1 | 10000 | 0.005 | 0.42 – 0.48 | ≈ 84 – 96 |
| Z3 | 10000 | 0.005 | 0.45 – 0.48 | ≈ 90 – 96 |
| Z2 | 10000 | — | does not move | — |

The ratio is not stable between runs, so it is not a clean scale factor: the axes also
carry a significant following error on top of the scale mismatch.

Raw encoder counts confirm the mismatch is real and not a display artefact. On Z1 a
command of `0.005` (= 50 pulses at `UNITS=10000`) produced an encoder count change of
**168**, while `MPOS` changed by 0.42 — i.e. `MPOS` is the encoder count divided by
400, not by `UNITS`.

Most likely cause: `UNITS` was configured with the **encoder** resolution
(counts/mm) instead of the **drive pulse** resolution (pulses/mm). The encoder is
roughly 40–96 times finer than the pulse output, so each commanded unit produces far
more physical travel than the number suggests.

**Consequence for the machine:** a command of `0.05` in the software moves the axis
several millimetres. Any motion performed with the current controller configuration
overshoots the intended distance by that factor.

**What is needed to resolve it** (not determinable from the controller alone):

1. the drive's pulse-per-mm setting and the axis' lead screw / gear ratio, to compute
   the correct `UNITS` for each axis;
2. one physical measurement per axis with a dial indicator or a calibrated scale, to
   confirm the travel per commanded unit — this is the only independent witness;
3. re-run the scale self-check afterwards; the ratio must come back as 1.0.

Until then the software refuses the move: see *Encoder-verified motion* below.

### Z2 is not available

Z2 reports `AXIS_STATUS = 4194304` (bit 22) and `STOP_REASON = 4196352`
(bits 22 and 11) while the other axes report `0`. Its `HOME_STATUS` is `0`, i.e. it was
never successfully homed, and it does not execute motion commands: a commanded move
leaves `DPOS` unchanged at 0. The meaning of the status bits was not identified and
the underlying cause is still open.

Because of this the software tracks the homing state **per axis**. An unhomed axis no
longer blocks the other three, but that axis itself cannot be moved.

### Encoder-verified motion

A move is verified against the **encoder feedback**, not against the commanded
position. `DPOS` always equals the command by definition, so comparing it proves
nothing; only the encoder gives an independent witness of the physical displacement.

When the encoder travel does not match the commanded travel — within 5 % of the
expected value — the move is rejected with an explicit message and the axis is left
where it stopped. On the machine as it stands, every move on X, Z1 and Z3 is rejected
this way, which is the intended behaviour while the scale is wrong.

`AxisParams.feedback_scale` records the measured ratio once it is known, so that the
check compares against the expected encoder travel instead of rejecting every move.
The *标度自检* button in the motion page measures that ratio with a small reversible
probe move.

### Connection reliability

Error code `20008` means "controller not found". It is returned for a genuinely wrong
address *and* for a discovery broadcast that the controller did not answer on that
attempt — the two are indistinguishable from the host. The driver therefore retries
the open up to three times with a short backoff. A persistent `20008` still means the
address is wrong, the controller is powered off, or the host is on a different subnet.

### Verified behaviour

Checked against the real controller with `tests/test_zmotion_hardware.py`. These tests
need a real machine, so they are **off by default** and only run when the opt-in switch
`TUNERLAB_ENABLE_HARDWARE_TESTS=1` is set — this keeps a routine `pytest` run from
reaching the controller (which is discovered by UDP broadcast on the local subnet) and
from issuing any axis motion. Even when enabled they touch the machine lightly: a
zero-displacement move plus a 5 µm scale probe that returns the axis to where it
started. Run them with::

    TUNERLAB_ENABLE_HARDWARE_TESTS=1 TUNERLAB_TEST_IP=192.168.0.11 \
        python -m pytest tests/test_zmotion_hardware.py

* connection, firmware read-back and clean close,
* all four axes readable, parameter values match the table above,
* every axis parameter round-trips when written back with its current value,
* `DPOS`/`MPOS` agree between the per-axis and the bulk read,
* a zero-displacement move completes the command → idle → read-back chain,
* a single-axis move whose encoder travel disagrees with the command is **rejected**,
* the retract-before-X interlock refuses a shared X move with a probe extended
  (legacy mode only; `independent_axes=False`),
* the scale self-check measures the command-to-feedback ratio and returns the axis to
  its starting position.

## Motion

`motion.py` defines the `MotionController` contract. Explicitly select an adapter; no
auto-discovery sends motor commands. Supply axis units, encoder conversions, limits,
insertion direction, safe retract, fixed offsets and timeout. `move_to` must block
until readback confirms in tolerance or fail; emergency_stop must latch.
`MotionPlanner` issues one direct move by default (`independent_axes=True`); with
`independent_axes=False` it restores the staged retract → X → Z plan. The adapter
must retain independent safety and physical emergency interlocks in either mode.

`tuner/zmotion_motion.py` adapts the Zmotion driver to that contract, so the planner
and the calibration code can run against real hardware. The safety rules are:

* only axes that actually have to travel are commanded — a zero-length move is never
  sent to an axis that is unhomed or carries an alarm,
* positions are checked against the configured soft limits before they are sent,
* a move waits for the controller to report idle and then verifies the **encoder**
  feedback against the expected travel; a disagreement rejects the move,
* the emergency stop calls `ZAux_Direct_Rapidstop`, latches the software state and
  requires an explicit reset plus re-homing,
* **axes are independent by default**: X travel is not gated on probe positions,
  and X homing does not wait for the probes. With `independent_axes=False` the
  legacy rule returns: every probe must be retracted at both endpoints of an X
  move, and every probe must be homed before X can be homed.
* homing is never a side effect of connecting; the controller's own per-axis home
  status is adopted instead, and the homing state is tracked per axis.

The software emergency stop does **not** replace a hardware interlock. In the
default independent mode the software no longer prevents a probe from being
dragged across the DUT while X moves; that risk must be covered by the hardware
interlock or by the operator's own clearance check.

## VNA

See hardware_vna.md for configurable VISAProfile. Supply exact model-supported SCPI, trace selection per S11/S21/S12/S22, frequency query, trigger/complete query, ASCII/binary data format, timeout and calibration configuration. This release includes a generic explicit-profile ASCII driver, not a certified Keysight preset. Profile examples in tests are fictitious test protocol, never instrument instructions. Do not assume a VNA sweep command performs SOLT/TRL calibration.

## Commissioning

Record serials, firmware, calibration kit, reference plane, actual termination Γ(f), fixture S2P, Z0, power/IFBW/averaging and ambient conditions in metadata. Confirm cable port mapping with known standards. Determine settling and repeated-position uncertainty experimentally. Use simulated=False only for actual non-Mock adapters and actual instrument measurements. Final acceptance requires independent impedance remeasurement and physical safety qualification.

## Still open

* **Scale defect (blocking).** `UNITS` does not match the encoder scale on X, Z1 and
  Z3, so a commanded distance is not the distance travelled. The drive pulse
  resolution and one physical measurement per axis are needed to correct it. Until
  then the software rejects the moves.
* **Z2 is not usable.** It never homed, carries status bits 22 and 11 in
  `AXIS_STATUS`/`STOP_REASON`, and ignores motion commands. Cause not identified.
* Mechanical travel limits per axis are not surveyed; `FS_LIMIT`/`RS_LIMIT` still hold
  the factory defaults.
* The direction convention of each axis (which way is positive) has not been confirmed
  against the mechanics.
* The command-to-feedback ratio is not stable between runs, so the axes also carry a
  noticeable following error that has not been characterised.
* No hardware interlock, limit switch or emergency-stop wiring has been verified.
