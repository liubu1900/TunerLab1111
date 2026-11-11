"""Real-instrument SCPI dialect negotiation for measurement definition.

Regression for the first Agilent/Keysight hardware session: the analyzer
rejected both ``CALC1:PAR:DEF`` and ``CALC1:PAR:SDEF`` with
``-113,"Undefined header"``.  PNA / PNA-X firmware speaks
``CALC1:PAR:DEF:EXT`` (or ``CALC1:PAR:EXT``), so the driver must negotiate
beyond the two original candidates.  A scripted transport reproduces the
error queue semantics without any instrument attached.
"""
from __future__ import annotations

import pytest

from tuner import vna_drivers as V


class ScriptedTransport:
    """Fake SCPI transport with a real IEEE-488.2 style error queue.

    ``accepted_header`` is the only measurement-definition header the
    simulated instrument understands; everything else queues -113.  Set
    ``errors_per_reject`` > 1 to reproduce analyzers that queue several
    entries for a single bad command.
    """

    def __init__(self, accepted_header: str | None,
                 errors_per_reject: int = 1,
                 idn: str = 'Agilent Technologies,N4225A,SN1,A.01.00'):
        self.accepted_header = accepted_header
        self.errors_per_reject = errors_per_reject
        self.idn = idn
        self.sent: list[str] = []
        self.queue: list[str] = []
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    @property
    def connected(self) -> bool:
        return self.opened

    def write(self, cmd: str) -> None:
        self.sent.append(cmd)
        if cmd == '*CLS':
            self.queue.clear()
        elif cmd.startswith((':CALC1:PAR:DEF', ':CALC1:PAR:SDEF',
                             ':CALC1:PAR:EXT')):
            header = cmd.split(' ', 1)[0]
            if header != self.accepted_header:
                self.queue.extend(['-113,"Undefined header"']
                                  * self.errors_per_reject)

    def read(self) -> str:
        return ''

    def query(self, cmd: str) -> str:
        self.sent.append(cmd)
        if cmd == ':SENS1:BAND?':
            return '1000'
        if cmd == ':CALC1:EQU:STAT?':
            return '0'
        if cmd == '*IDN?':
            return self.idn
        if cmd == ':SYST:ERR?':
            return self.queue.pop(0) if self.queue else '+0,"No error"'
        if cmd.endswith('*OPC?'):
            return '1'
        if cmd.startswith(':CALC1:DATA?'):
            return '0.1,0.02,0.1,0.02'
        return ''


@pytest.mark.parametrize('accepted', [
    ':CALC1:PAR:DEF',
    ':CALC1:PAR:SDEF',
    ':CALC1:PAR:DEF:EXT',
    ':CALC1:PAR:EXT',
])
def test_negotiates_every_known_define_dialect(accepted):
    tr = ScriptedTransport(accepted)
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    vna.connect()
    assert vna._define_cmd == accepted
    defines = [c for c in tr.sent
               if c.startswith((':CALC1:PAR:DEF', ':CALC1:PAR:SDEF',
                                ':CALC1:PAR:EXT'))
               and "'S11'" in c]
    assert defines[-1].startswith(accepted + ' ')
    # S21/S12/S22 reuse the working dialect instead of re-probing every time.
    assert sum(1 for c in tr.sent if c.startswith(accepted + ' ')) == 4


def test_define_reuses_winning_dialect_for_all_four_s_params():
    tr = ScriptedTransport(':CALC1:PAR:DEF:EXT')
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    vna.connect()
    for param in V.TCPVNA.S_PARAMS:
        assert f":CALC1:PAR:DEF:EXT '{param}','{param}'" in tr.sent
    # Only S11 had to try (and reject) DEF / SDEF.
    assert sum(1 for c in tr.sent if c.startswith(':CALC1:PAR:DEF ')) == 1
    assert sum(1 for c in tr.sent if c.startswith(':CALC1:PAR:SDEF ')) == 1


def test_stale_error_queue_entries_do_not_poison_next_candidate():
    """A rejected command that queues TWO errors must not hide a later win."""
    tr = ScriptedTransport(':CALC1:PAR:DEF:EXT', errors_per_reject=2)
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    vna.connect()
    assert vna._define_cmd == ':CALC1:PAR:DEF:EXT'


def test_every_candidate_rejected_reports_all_tried_commands():
    tr = ScriptedTransport(None)
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    with pytest.raises(RuntimeError, match='failed to define measurement S11') as ei:
        vna.connect()
    message = str(ei.value)
    for header in (':CALC1:PAR:DEF', ':CALC1:PAR:SDEF',
                   ':CALC1:PAR:DEF:EXT', ':CALC1:PAR:EXT'):
        assert header in message


def test_keysight_pna_profile_starts_from_def_ext():
    """A PNA must not queue a -113 on every connect just to find its dialect."""
    tr = ScriptedTransport(':CALC1:PAR:DEF:EXT',
                           idn='Agilent Technologies,N5247A,US,A.09.00')
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    vna.connect()
    assert vna.vendor_key == 'keysight_pna'
    assert vna._define_cmd == ':CALC1:PAR:DEF:EXT'
    assert not any(c.startswith(':CALC1:PAR:DEF ') for c in tr.sent)
    assert not any(c.startswith(':CALC1:PAR:SDEF ') for c in tr.sent)


def test_first_candidate_still_wins_without_any_errors():
    """Backwards compatibility: mock / R&S-style instruments stay unchanged."""
    tr = ScriptedTransport(':CALC1:PAR:DEF')
    vna = V.TCPVNA('192.168.2.111', transport=tr)
    vna.connect()
    assert vna._define_cmd == ':CALC1:PAR:DEF'
    assert not any(c.startswith(':CALC1:PAR:DEF:EXT') for c in tr.sent)


def test_profile_candidates_are_exposed_for_diagnostics():
    # The exception message doubles as the operator's command list: keep all
    # four dialects present even when a profile starts from SDEF (R&S).
    tr = ScriptedTransport(None)
    vna = V.TCPVNA('192.168.2.111', transport=tr, vendor_key='rsp')
    with pytest.raises(RuntimeError) as ei:
        vna.connect()
    assert ':CALC1:PAR:SDEF' in str(ei.value)
    assert ':CALC1:PAR:DEF:EXT' in str(ei.value)
