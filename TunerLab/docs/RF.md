# RF conventions and mathematical scope

All two-port S matrices use S[output,input], so S21 is S[1,0]. Common real reference impedance defaults to 50 ohm. Port 1 faces the DUT, port 2 faces the configured termination. Adapters must swap ports before persistence if hardware uses the opposite orientation.

Gamma = (Z-Z0)/(Z+Z0), Z = Z0(1+Gamma)/(1-Gamma).

Gamma_load_at_tuner_port1 = S11 + S12*S21*Gamma_termination/(1-S22*Gamma_termination).

S11 equals the load reflection only when port 2 is matched. The fixture is ordered DUT-facing port 1 to tuner-facing port 2; its terminated reflection transforms this load to the DUT reference plane. Fixture and tuner must share frequency grid/reference impedance. The code does not infer fixture orientation or renormalize impedances silently.

Complex S values are linearly interpolated in frequency inside the calibration band. Extrapolation is rejected. This is an approximation; resonant or delay-rich networks need sufficiently dense sweeps and independent validation. The configured termination is fixed across the sweep in this version.

Nearest sampled complex Gamma uses a 2D KD-tree. Multiple-frequency ranking minimizes weighted RMS complex Gamma error. SearchResult.gamma reports the first requested frequency for multi-frequency search; SearchResult.error reports aggregate weighted RMS. Reachable means a valid calibrated sample meets the stated tolerance. False means unsupported by sampled data, not proof of physical impossibility.

Local optimization interpolates complex Gamma versus actual coordinates within the sampled convex hull. It returns a predicted coordinate only and falls back to the closest measured point on degenerate geometry or failed optimization. It does not command an unmeasured position automatically. Refinement requires movement, fresh RF measurement, and an explicitly recorded validation result.

The tuner manager separately reports mechanical positioning, predicted Gamma error, simulated RF verification, and real RF verification. Simulated measurements always leave rf_verified false.

Unit tests include analytical short/open/match transformations, loaded two-port equations, fixture transforms, independent scikit-rf cascade comparison, invalid sample rejection, ranking, multi-frequency objectives, local interpolation, and simulated status separation.
