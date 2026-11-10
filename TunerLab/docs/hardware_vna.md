# VNA integration

`MockVNAController` produces deterministic synthetic passive reciprocal two-port networks. Shared X shifts every probe with its configured fixed spacing; each Z controls a shunt susceptance. Its numerical dimensions, losses and capacitances are illustrative and are not specifications of a real tuner.

`PyVISAVNAController(VISAProfile(...), enabled=True)` is an opt-in transport adapter. It never opens a VISA session by default. Install a suitable VISA backend and verify the actual VNA programming guide, channel, calibrated reference planes, frequency sweep, power, IF bandwidth, trigger mode and trace definitions before enabling it. No manufacturer-specific SCPI profile is supplied or claimed verified.

The profile must supply resource address, timeout, frequency query in Hz, trigger commands, acquisition completion query, and four trace queries. Each trace must return corrected complex ASCII values as interleaved real/imaginary numbers. S21 maps to `s[:,1,0]`; S12 maps to `s[:,0,1]`. The adapter validates shape and finite values and propagates VISA errors for calibration retry logic. Binary block formats require a future instrument-specific adapter. A completion response confirms acquisition, not calibration validity.

Instrument measurements carry `simulated=False` and `calibration_validated=False`. This is provenance only: it does not certify DUT load impedance accuracy. Use a verified load-plane fixture/termination model and independent RF validation before treating tuning predictions as measured DUT impedance.
