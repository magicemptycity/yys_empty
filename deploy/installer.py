from deploy.patch import pre_checks

pre_checks()

from deploy.adb import AdbManager
from deploy.process import ProcessManager
from deploy.fluentui import FluentuiManager
from deploy.config import ExecutionError
from deploy.git import GitManager
from deploy.pip import PipManager


class Installer(GitManager, PipManager, AdbManager, FluentuiManager, ProcessManager):
    def install(self, *, skip_adb=False):
        try:
            self.process_kill()
            self.git_install()
            self.pip_install()
            if not skip_adb:
                self.adb_install()
        except ExecutionError:
            exit(1)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-adb", action="store_true", help="Keep shared ADB and emulator agents unchanged")
    Installer().install(skip_adb=parser.parse_args().skip_adb)
