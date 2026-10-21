"""Real, offline installer integration; opt in with ANYPS3_INSTALLER_WHEELS.

The wheel directory must contain gui/self dependencies and setuptools>=68.
All installed applications, Git fixtures and libraries live in a temporary home.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
WHEELS = os.environ.get("ANYPS3_INSTALLER_WHEELS")


@unittest.skipUnless(WHEELS, "set ANYPS3_INSTALLER_WHEELS to run real offline installations")
class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="anyps3-installer-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Shell and desktop quoting must survive spaces, quotes, dollars and %.
        self.home = self.root / "user's home $cash %test"
        self.home.mkdir()
        # setuptools itself expands dollar variables in its installation prefix;
        # keep those in HOME/launcher paths, but not in the custom app data path.
        self.data = self.root / "application's data %test"
        self.app = self.data / "anyps3-app"
        self.library = self.data / "anyps3-library"
        self.bin = self.home / ".local/bin"
        self.env = dict(os.environ, HOME=str(self.home), XDG_DATA_HOME=str(self.data),
                        XDG_CONFIG_HOME=str(self.home / "config"),
                        QT_QPA_PLATFORM="offscreen", SDL_AUDIODRIVER="dummy",
                        ANYPS3_AUDIO_SINK="null")
        self.repo = self.root / "development"
        self.repo.mkdir()
        for name in ("anyps3", "anyps3_manager", "anyps3_gui", "profiles"):
            shutil.copytree(ROOT / name, self.repo / name, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("pyproject.toml", "README.md", "LICENSE", ".gitignore"):
            shutil.copyfile(ROOT / name, self.repo / name)
        self.git("init", "-q", "-b", "main")
        self.commit()
        self.counter = 0

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Installer test", "-c", "user.email=installer@example.invalid",
                 "commit", "-qm", "Installer fixture")

    def install(self, *args, success=True):
        result = subprocess.run(["bash", str(ROOT / "scripts/install.sh"), "--repo", str(self.repo),
                                 "--wheelhouse", str(Path(WHEELS).resolve()), *args],
                                cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=180)
        self.counter += 1
        if log_dir := os.environ.get("ANYPS3_INSTALLER_LOG_DIR"):
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{self._testMethodName}-{self.counter}.log").write_text(result.stdout + result.stderr)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def cli(self, *args, env=None):
        result = subprocess.run([str(self.bin / "anyps3-manager"), *args, "--json"],
                                cwd=self.repo, env=env or self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout)

    def test_install_update_failure_and_development_isolation(self):
        # A pre-existing development library and configuration must remain intact.
        dev_library = self.data / "anyps3"
        dev_library.mkdir(parents=True)
        dev_save = dev_library / "save.sentinel"
        dev_save.write_bytes(b"development save")
        dev_config = self.home / "config/AnyPS3/Manager.conf"
        dev_config.parent.mkdir(parents=True)
        dev_config.write_text("development preferences")
        # Both pip environment and configuration must be ignored by the installer.
        pip_config = self.root / "development-pip.conf"
        pip_config.write_text(f"[global]\ntarget = {self.repo / 'wrong-config-target'}\n")
        self.env.update(PIP_TARGET=str(self.repo / "wrong-env-target"),
                        PIP_CONFIG_FILE=str(pip_config))
        self.install()
        first = (self.app / "current").resolve()
        self.assertNotEqual(first / "source", self.repo)
        self.assertEqual((first / "revision").read_text().strip(), self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.repo / "wrong-env-target").exists())
        self.assertFalse((self.repo / "wrong-config-target").exists())
        self.assertFalse(self.library.exists(), "install must not initialize or modify game storage")
        desktop = self.data / "applications/anyps3.desktop"
        if validator := shutil.which("desktop-file-validate"):
            subprocess.run([validator, str(desktop)], check=True, capture_output=True)

        # A dirty checkout plus a hostile Python/Qt environment must not affect
        # installed CLI or GUI, including the GUI's real QProcess CLI children.
        (self.repo / "anyps3_manager/__init__.py").write_text("raise RuntimeError('development imported')\n")
        (self.repo / "anyps3_gui/__init__.py").write_text("raise RuntimeError('development GUI imported')\n")
        hostile = dict(self.env, PYTHONPATH=str(self.repo), PYTHONHOME=str(self.repo / "invalid-python"),
                       VIRTUAL_ENV=str(self.repo / ".venv"), QT_PLUGIN_PATH=str(self.repo / "invalid-qt"))
        result = self.cli("init", env=hostile)
        self.assertEqual(result["library"], str(self.library))
        save = self.library / "save.sentinel"
        save.write_bytes(b"installed save")
        with subprocess.Popen([str(self.bin / "anyps3-gui")], cwd=self.repo, env=hostile,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as gui:
            try:
                time.sleep(2)
                self.assertIsNone(gui.poll(), "Installed GUI exited during startup")
            finally:
                gui.terminate()
                stdout, stderr = gui.communicate(timeout=10)
            self.assertNotIn("development imported", stderr + stdout)
            self.assertNotIn("Traceback", stderr + stdout)
        self.assertEqual(dev_save.read_bytes(), b"development save")
        self.assertEqual(dev_config.read_text(), "development preferences")

        # Update from a new commit. Running workers can still use the old release.
        self.git("restore", "anyps3_manager/__init__.py", "anyps3_gui/__init__.py")
        with (self.repo / "anyps3/__init__.py").open("a") as stream:
            stream.write("\nINSTALLER_TEST_VERSION = 'updated'\n")
        self.commit()
        self.install()
        second = (self.app / "current").resolve()
        self.assertNotEqual(first, second)
        self.assertTrue((first / "venv/bin/python").exists())
        self.assertEqual(save.read_bytes(), b"installed save")
        self.assertEqual(self.cli("titles", "list")["titles"], [])
        query = "import anyps3; print(anyps3.INSTALLER_TEST_VERSION)"
        output = subprocess.check_output([str(second / "venv/bin/python"), "-I", "-c", query], text=True)
        self.assertEqual(output.strip(), "updated")

        # Resolve failure in a new environment must never break the active one.
        project = self.repo / "pyproject.toml"
        project.write_text(project.read_text().replace('self = ["cryptography"]',
                           'self = ["anyps3-nonexistent-installer-test-dependency==0"]'))
        self.commit()
        self.install(success=False)
        self.assertEqual((self.app / "current").resolve(), second)
        self.assertEqual(len(list((self.app / "releases").iterdir())), 2)
        self.assertEqual(self.cli("titles", "list")["titles"], [])
        self.assertEqual(save.read_bytes(), b"installed save")
        self.assertEqual(dev_save.read_bytes(), b"development save")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_conflicting_launcher_is_preserved(self):
        self.bin.mkdir(parents=True)
        launcher = self.bin / "anyps3-gui"
        launcher.write_text("unrelated developer launcher")
        result = self.install(success=False)
        self.assertIn("Unrelated file already exists", result.stderr)
        self.assertEqual(launcher.read_text(), "unrelated developer launcher")
        self.assertFalse((self.app / "current").exists())

    def test_missing_ref_does_not_publish(self):
        self.install("--ref", "no-such-installer-test-branch", success=False)
        self.assertFalse((self.app / "current").exists())
        self.assertEqual(list((self.app / "releases").iterdir()), [])

    def test_truncated_download_does_not_start_installing(self):
        script = (ROOT / "scripts/install.sh").read_text()
        result = subprocess.run(["bash"], input=script[:script.index("git clone")],
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.app.exists())


if __name__ == "__main__":
    unittest.main()
