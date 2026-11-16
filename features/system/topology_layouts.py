"""Presentation for the canonical Mermaid diagrams (copy and edges stay in .mmd).

All views share a 1240px canvas, 208 × 72 nodes and orthogonal arrow lanes.
Routes specify ports and bends so ownership boundaries remain easy to follow.
"""

from dataclasses import dataclass, field


Point = tuple[float, float]
Port = tuple[str, float]
ACCESS = 'var(--arch-access, #a78bfa)'
CONTROL = 'var(--arch-control, #38bdf8)'
WORKER = 'var(--arch-worker, #fbbf24)'
RESOURCE = 'var(--arch-resource, #34d399)'


@dataclass(frozen=True)
class Route:
    start: Port = ('right', 0)
    end: Port = ('left', 0)
    via: tuple[Point, ...] = ()
    label: Point | None = None
    label_width: int = 96
    label_side: str | None = None


@dataclass(frozen=True)
class Layout:
    positions: dict[str, Point]
    kinds: dict[str, str]
    routes: dict[tuple[str, str], Route] = field(default_factory=dict)


LAYOUTS = {
    'platform-topology.mmd': Layout(
        positions={
            'Browser': (140, 60), 'Agent': (440, 60), 'DB': (780, 60), 'Build': (1100, 60),
            'Source': (140, 220), 'Controller': (620, 220), 'External': (1100, 220),
            'WorkerA': (460, 380), 'WorkerB': (780, 380),
            'Devices': (460, 540), 'Suite': (780, 540),
        },
        kinds={
            'Browser': ACCESS, 'Agent': ACCESS, 'Controller': CONTROL,
            'WorkerA': WORKER, 'WorkerB': WORKER,
        },
        routes={
            ('Browser', 'Controller'): Route(('bottom', 0), ('top', -80),
                                            ((140, 140), (540, 140)), (325, 140), 180),
            ('Agent', 'Controller'): Route(('bottom', 0), ('top', 0),
                                          ((440, 112), (620, 112)), (610, 112), 140, 'above'),
            ('Controller', 'DB'): Route(('top', 64), ('bottom', 0), ((684, 128), (780, 128))),
            ('Controller', 'WorkerA'): Route(('bottom', -24), ('top', 28),
                                            ((596, 312), (488, 312)), (550, 312), 120),
            ('Controller', 'WorkerB'): Route(('bottom', 24), ('top', -28),
                                            ((644, 312), (752, 312)), (688, 312), 120),
            ('WorkerA', 'Controller'): Route(('top', -28), ('left', 0),
                                            ((432, 220),), (432, 277), 120),
            ('WorkerB', 'Controller'): Route(('top', 28), ('right', 0),
                                            ((808, 220),), (808, 277), 120),
            ('Source', 'WorkerA'): Route(('bottom', 0), ('left', 0), ((140, 380),), (250, 380)),
            ('WorkerA', 'Devices'): Route(('bottom', 0), ('top', 0), label=(460, 460), label_width=120),
            ('WorkerB', 'Devices'): Route(('right', 18), ('bottom', 0),
                                         ((934, 398), (934, 600), (460, 600)), (934, 500), 120),
            ('WorkerA', 'Suite'): Route(('right', 18), ('left', 0), ((650, 398), (650, 540))),
            ('WorkerB', 'Suite'): Route(('bottom', 0), ('top', 0)),
            ('Controller', 'Build'): Route(('right', -20), ('left', 0),
                                          ((940, 200), (940, 60)), (940, 128)),
            ('Controller', 'External'): Route(('right', 18), ('left', 18)),
        },
    ),
    'usbip-topology.mmd': Layout(
        positions={
            'DevSrc': (140, 260), 'WinSrc': (460, 68), 'LinuxSrc': (460, 452),
            'Ctrl': (460, 260), 'Worker': (780, 260),
            'DevWorker': (1100, 260), 'Tools': (1100, 452),
        },
        kinds={'Ctrl': CONTROL, 'Worker': WORKER, 'Tools': WORKER},
        routes={
            ('DevSrc', 'WinSrc'): Route(('top', 0), ('left', 0), ((140, 68),), (140, 164)),
            ('DevSrc', 'LinuxSrc'): Route(('bottom', 0), ('left', 0), ((140, 452),), (140, 356)),
            ('WinSrc', 'Worker'): Route(('right', 0), ('top', 0), ((780, 68),), (690, 68), 130),
            ('LinuxSrc', 'Worker'): Route(('right', 0), ('bottom', 0), ((780, 452),), (690, 452), 130),
            ('Ctrl', 'WinSrc'): Route(('top', 0), ('bottom', 0), label=(460, 164), label_width=130),
            ('Ctrl', 'LinuxSrc'): Route(('bottom', 0), ('top', 0), label=(460, 356), label_width=130),
            ('Ctrl', 'Worker'): Route(('right', -18), ('left', -18),
                                      label=(620, 242), label_width=100, label_side='above'),
            ('Worker', 'Ctrl'): Route(('left', 18), ('right', 18),
                                      label=(620, 278), label_width=100, label_side='below'),
            ('Worker', 'DevWorker'): Route(label=(940, 260)),
            ('DevWorker', 'Tools'): Route(('bottom', 0), ('top', 0), label=(1100, 356), label_width=150),
        },
    ),
    'aoa-topology.mmd': Layout(
        positions={
            'Browser': (140, 68), 'Ctrl': (460, 68), 'Store': (780, 68),
            'Worker': (460, 260), 'Acc': (780, 260), 'Dev': (1100, 260), 'Adb': (1100, 452),
        },
        kinds={'Browser': ACCESS, 'Ctrl': CONTROL, 'Worker': WORKER, 'Acc': WORKER},
        routes={
            ('Browser', 'Ctrl'): Route(label=(300, 68), label_width=104),
            ('Ctrl', 'Store'): Route(),
            ('Ctrl', 'Worker'): Route(('bottom', -64), ('top', -64), label=(396, 164), label_width=100),
            ('Worker', 'Ctrl'): Route(('top', 64), ('bottom', 64), label=(524, 164), label_width=100),
            ('Worker', 'Acc'): Route(label=(620, 260), label_width=100),
            ('Acc', 'Dev'): Route(label=(940, 260)),
            ('Worker', 'Adb'): Route(('bottom', 0), ('left', 0), ((460, 452),), (720, 452), 180),
            ('Dev', 'Adb'): Route(('bottom', 0), ('top', 0), label=(1100, 356), label_width=160),
        },
    ),
    'firmware-topology.mmd': Layout(
        positions={
            'Browser': (140, 68), 'Ctrl': (460, 68), 'Worker': (780, 68), 'Loader': (1100, 68),
            'AoaRec': (780, 260), 'Dev': (1100, 260),
            'SourceHost': (460, 452), 'SourceDev': (780, 452),
        },
        kinds={
            'Browser': ACCESS, 'Ctrl': CONTROL, 'Worker': WORKER, 'Loader': WORKER, 'AoaRec': WORKER,
        },
        routes={
            ('Browser', 'Ctrl'): Route(label=(300, 68), label_width=100),
            ('Ctrl', 'Worker'): Route(('right', -18), ('left', -18),
                                      label=(620, 50), label_width=100, label_side='above'),
            ('Worker', 'Ctrl'): Route(('left', 18), ('right', 18),
                                      label=(620, 86), label_width=100, label_side='below'),
            ('Worker', 'Loader'): Route(label=(940, 68), label_width=100),
            ('Loader', 'Dev'): Route(('bottom', 0), ('top', 0), label=(1100, 164)),
            ('Worker', 'AoaRec'): Route(('bottom', 0), ('top', 0), label=(780, 164), label_width=150),
            ('AoaRec', 'Dev'): Route(label=(940, 260)),
            ('Ctrl', 'SourceHost'): Route(('bottom', -64), ('top', -64),
                                         label=(396, 240), label_width=106),
            ('SourceHost', 'Ctrl'): Route(('top', 64), ('bottom', 64), label=(524, 340)),
            ('SourceHost', 'SourceDev'): Route(label=(620, 452)),
        },
    ),
    'test-flow-topology.mmd': Layout(
        positions={
            'Request': (140, 68), 'Gate': (460, 68), 'Job': (780, 68), 'Worker': (1100, 68),
            'Device': (1100, 260), 'Evidence': (780, 260), 'Report': (460, 260), 'Diagnosis': (140, 260),
        },
        kinds={'Request': ACCESS, 'Gate': CONTROL, 'Job': CONTROL, 'Worker': WORKER, 'Diagnosis': ACCESS},
        routes={
            ('Request', 'Gate'): Route(label=(300, 68)),
            ('Gate', 'Job'): Route(label=(620, 68)),
            ('Job', 'Worker'): Route(('right', -18), ('left', -18),
                                     label=(940, 50), label_width=100, label_side='above'),
            ('Worker', 'Job'): Route(('left', 18), ('right', 18),
                                     label=(940, 86), label_width=100, label_side='below'),
            ('Worker', 'Device'): Route(('bottom', 0), ('top', 0), label=(1100, 164), label_width=150),
            ('Worker', 'Evidence'): Route(('bottom', -80), ('top', 0),
                                          ((1020, 188), (780, 188)), (900, 188), 180),
            ('Evidence', 'Report'): Route(('left', 0), ('right', 0), label=(620, 260), label_width=100),
            ('Report', 'Diagnosis'): Route(('left', 0), ('right', 0), label=(300, 260)),
        },
    ),
    'agent-analysis-topology.mmd': Layout(
        positions={
            'Chat': (140, 68), 'Planner': (460, 68), 'Guard': (780, 68), 'Run': (1100, 68),
            'Evidence': (1100, 260), 'Collect': (780, 260), 'References': (460, 260),
            'Review': (780, 452), 'Monitor': (460, 452), 'Reply': (140, 452),
        },
        kinds={
            'Chat': ACCESS, 'Planner': ACCESS, 'Guard': CONTROL, 'Run': CONTROL,
            'Collect': WORKER, 'Review': ACCESS, 'Monitor': CONTROL, 'Reply': ACCESS,
        },
        routes={
            ('Chat', 'Planner'): Route(label=(300, 68)),
            ('Planner', 'Guard'): Route(label=(620, 68)),
            ('Guard', 'Run'): Route(label=(940, 68), label_width=100),
            ('Run', 'Collect'): Route(('bottom', 0), ('top', 0),
                                        ((1100, 164), (780, 164)), (950, 164), 160),
            ('Evidence', 'Collect'): Route(('left', 0), ('right', 0), label=(940, 260)),
            ('References', 'Collect'): Route(label=(620, 260)),
            ('Collect', 'Review'): Route(('bottom', 0), ('top', 0), label=(780, 356), label_width=150),
            ('Review', 'Monitor'): Route(('left', 0), ('right', 0), label=(620, 452), label_width=100),
            ('Planner', 'Monitor'): Route(('bottom', 0), ('top', 0),
                                         ((460, 164), (300, 164), (300, 384), (460, 384)), (300, 300)),
            ('Monitor', 'Reply'): Route(('left', 0), ('right', 0), label=(300, 452)),
        },
    ),
}
