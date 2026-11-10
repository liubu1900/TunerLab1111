# Frozen API v1
All coordinates are in configurable controller units; Mock examples use mm. Shared X and independent Z; fixed_offsets describe probe spacing and are never additional axes. S matrix is [frequency, output, input]. Frequencies are Hz. Positive Z means insertion in Mock only. Real adapters must explicitly map direction/units.

- TunerConfig: probes, limits, safe_z, fixed_offsets, z0, motion_timeout, simulated; axes, validate_position(), grid().
- MotionController: initialize(), home(), read_position(), move_to(position, timeout=None), jog(axis, delta), emergency_stop(), reset_emergency(), retract(). Mock by default; ZmotionMotionController provides the real adapter.
- MotionPlanner(config, controller): move_to(position); retract all probes before X travel.
- VNAController: initialize(), measure()->Measurement, close().
- CalibrationDatabase(path): create(config, frequencies, positions, metadata=None), write_point(index, actual, measurement), load(), pending_indices(), record_failure(index,error), save_validation(data).
- CalibrationManager(config,motion,vna,database): prepare(positions), run(progress=None), pause(), resume(), cancel().
- RFNetworkEngine: z_to_gamma(), gamma_to_z(), terminated_gamma(), cascade(), dut_gamma().
- GammaSearchEngine(data, z0=50, termination=0j, fixture=None): search(frequency,target_gamma,k=5,tolerance=0.05), search_multi(frequencies,targets,weights=None,k=5), optimize_local(frequency,target_gamma).
- TuningManager(config,motion,search_engine,vna=None): tune(frequency,target_impedance,tolerance=0.05,verify=False).

Data load returns dict: frequencies (F,), positions/actual (N,A), s (N,F,2,2), valid (N,), metadata dict. Configuration serialized as metadata. Invalid/pending points excluded from RF search. Real and simulated provenance must never mix.

## Zmotion backend (v1.1)

All lengths are user units (mm); `UNITS` is the pulse equivalent and is never applied in software.

- ZmotionController(library=None, ip='192.168.0.11'): open(ip=None, timeout=3.0, retries=3, backoff=0.4), close(), search(ip='', timeout_ms=1000), read_firmware(); per-axis get/set for atype, units, speed, accel, decel, creep, jog_speed, invert_step, datum_in, home_wait, fs_limit, rs_limit; get_dpos(), get_mpos(), set_dpos(), if_idle(), home_status(), axis_status(), axis_stop_reason(), axis_enable(), measured_speed(), read_all_axis_info(count); move_absolute(), move_relative(), jog(), stop_axis(), stop_axes(), emergency_stop(), home_axis(), wait_idle().
- ZmotionMotionController(config, link, axis_params=None, position_tolerance=0.002): initialize(), close(), apply_axis_parameters(), read_axis_parameters(), home(timeout=120), read_position(), read_feedback_position(), move_to(position, timeout=None), move_axis_to(axis, position, timeout=None), jog(axis, delta), stop_axis(axis), stop_all(), retract(), emergency_stop(), reset_emergency(), axis_status_snapshot(), all_axes_homed().
- AxisParams: name, atype, units, speed, accel, decel, creep, jog_speed, invert_step, datum_in, home_wait, fs_limit, rs_limit, datum_mode; to_dict(), from_dict().
- ConnectionSettings(ip, connect_on_start), TunerSettings(connection, axes), SettingsStore(path): load(), save(), export(path, settings=None), import_from(path).
- ZmotionError carries the vendor error code (20008 = controller not found).
