"""Asynchronous JSON/JSON Lines client; deliberately no manager imports."""

import json

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal, Slot


class Call(QObject):
    # object preserves JSON uint64 values; Signal(dict) converts to QVariantMap
    # and can overflow the runtime's uint64 instruction-budget limits.
    result = Signal(object)
    notification = Signal(object)
    error = Signal(object)
    done = Signal()

    def __init__(self, command, arguments, parent):
        super().__init__(parent)
        self.buffer = b""
        self.stderr = bytearray()
        self.received = False
        self.failed = False
        self.process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("SDL_AUDIODRIVER", "dummy")
        env.insert("ANYPS3_AUDIO_SINK", "null")
        env.remove("ANYPS3_AUDIO_AUDIBLE")
        self.process.setProcessEnvironment(env)
        self.process.setProgram(command[0])
        self.process.setArguments([*command[1:], *arguments, "--json"])
        self.process.readyReadStandardOutput.connect(self.read_stdout)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.errorOccurred.connect(self.process_error)
        self.process.finished.connect(self.finished)

    def start(self):
        self.process.start()
        return self

    def read_stderr(self):
        self.stderr.extend(bytes(self.process.readAllStandardError()))
        del self.stderr[:-16384]

    def read_stdout(self):
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self.parse(line)
        if len(self.buffer) > 4*1024*1024:
            self.fail("protocol", "Manager response exceeded 4 MiB without a newline.")
            self.process.kill()

    def parse(self, line):
        if not line.strip():
            return
        try:
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("schema") != "anyps3.manager/1":
                raise ValueError("Unsupported manager response schema.")
        except (ValueError, UnicodeError) as exc:
            self.fail("protocol", str(exc))
            return
        self.received = True
        if value.get("event") == "error":
            self.failed = True
            self.error.emit(value)
        elif value.get("event") and value["event"] != "result":
            self.notification.emit(value)
        else:
            self.result.emit(value)

    def fail(self, code, message):
        if not self.failed:
            self.failed = True
            self.error.emit(dict(code=code, message=message, detail={}))

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.fail("cli-unavailable", self.process.errorString())
            self.done.emit()

    def finished(self, code, status):
        self.read_stdout()
        self.read_stderr()
        if self.buffer.strip():
            self.parse(self.buffer)
            self.buffer = b""
        if code and not self.failed:
            self.fail("cli-exit", self.stderr.decode(errors="replace") or f"Manager exited with code {code}.")
        elif not self.received and not self.failed:
            self.fail("protocol", "Manager returned no JSON response.")
        self.done.emit()


class Client(QObject):
    error = Signal(object)

    def __init__(self, command, library=None, parent=None):
        super().__init__(parent)
        self.command, self.library = command, library
        self.calls = set()

    def call(self, arguments, result=None, event=None, error=None):
        args = list(arguments)
        if self.library:
            args += ["--library", self.library]
        call = Call(self.command, args, self)
        self.calls.add(call)
        if result:
            call.result.connect(result)
        if event:
            call.notification.connect(event)
        call.error.connect(error or self.error.emit)
        call.done.connect(self.release)
        call.start()
        return call

    @Slot()
    def release(self):
        call = self.sender()
        self.calls.discard(call)
        call.deleteLater()

    def close(self):
        # These are CLI clients/followers. Build/import/game workers are detached.
        for call in tuple(self.calls):
            call.process.kill()
