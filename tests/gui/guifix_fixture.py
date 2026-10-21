"""Synthetic asynchronous CLI fixture; never reads a player library or key bytes."""
from PySide6.QtCore import QObject, Signal, QTimer


class Pending(QObject):
    done = Signal()


class FixtureClient(QObject):
    error = Signal(object)

    def __init__(self):
        super().__init__()
        self.commands = []
        self.titles = []
        self.jobs = []
        self.calls = set()
        self.schema = {
            'audio.enabled': dict(type='boolean', default=True, group='Audio', description='Enable game audio', label='Enable game audio'),
            'input.rumble': dict(type='boolean', default=True, group='Input', description='Allow controller vibration', label='Allow game controller vibration'),
            'run.memory_max': dict(type='string', default='8G', group='Performance', description='Game memory limit', concurrent_memory=True),
            'trace.enabled': dict(type='boolean', default=False, group='Diagnostics', description='Trace controller input', advanced=True),
        }
        self.values = {'audio.enabled': False, 'input.rumble': True, 'run.memory_max': '8G', 'trace.enabled': False}
        self.build_records = [dict(id='build-001', status='built', reason='Synthetic build', duration_seconds=12, logs='/synthetic/logs')]

    def call(self, args, result=None, event=None, error=None, **kwargs):
        args = list(args)
        self.commands.append(args)
        call = Pending(self)
        self.calls.add(call)
        def reply():
            data = {}
            if args[:2] == ['titles', 'list']:
                data = {'titles': self.titles}
            elif args[:2] == ['jobs', 'list']:
                data = {'jobs': self.jobs}
            elif args[0] == 'builds':
                data = {'builds': self.build_records}
            elif args[:2] == ['saves', 'list']:
                data = {'saves': [dict(id='SAVE001', title='Synthetic save', subtitle='Clock', size=4096)]}
            elif args[:2] == ['saves', 'backups']:
                data = {'backups': [dict(id='backup-001')]}
            elif args[:2] == ['runs', 'show']:
                data = {'run': dict(id='run-001', stop='STOP: synthetic evidence'), 'excerpt': 'Synthetic log only'}
            elif args[0] == 'runs':
                data = {'runs': [dict(id='run-001', build='build-001', fps_mean=59.94, stop='')]}
            elif args[:2] == ['settings', 'schema']:
                data = {'settings': self.schema}
            elif args[:2] == ['settings', 'get']:
                data = {'settings': self.values, 'overrides': {}}
            elif args[0] == 'doctor':
                data = {'checks': {'ram_bytes': 32*1024**3}}
            elif args[0] == 'inspect-iso':
                data = dict(id='BCES00511', title='Ratchet & Clank: A Crack in Time', version='01.00', space_ok=True,
                            estimated_required_bytes=20*1024**3, free_bytes=80*1024**3, compatibility='Synthetic disc for GUI inspection', firmware=['3.01-test'], keys={})
            elif args[:2] == ['pads', 'list']:
                data = {'pads': [dict(id='0', name='Synthetic DualSense', guid='fixture-guid', serial='fixture', rumble=True, motion=True, battery='wired')]}
            if result:
                result(data)
            self.calls.discard(call)
            call.done.emit()
        QTimer.singleShot(0, reply)
        return call

    def close(self):
        pass


def title(status='built', active='build-001'):
    return dict(id='BCES00511', title='Ratchet & Clank: A Crack in Time', status=status, active_build=active,
                version='01.00', iso_path='/synthetic/disc.iso', compatibility='Synthetic GUI fixture; no game executed')
