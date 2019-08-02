import re
import platform
import logging
import pathlib
from dataclasses import dataclass
from typing import Optional
import winreg

from consts import HP
from local.pathfinder import PathFinder
from local.localgame import LocalHumbleGame


@dataclass(frozen=True)
class UninstallKey:
    key_name: str
    display_name: str
    uninstall_string: str
    _install_location: Optional[str] = None
    _display_icon: Optional[str] = None
    quiet_uninstall_string: Optional[str] = None

    @property
    def install_location(self) -> Optional[pathlib.Path]:
        if not self._install_location:
            return None
        path = self._install_location.replace('"', '')
        return pathlib.Path(path)

    @property
    def display_icon(self) -> Optional[pathlib.Path]:
        if not self._display_icon:
            return None
        path = self._display_icon.split(',', 1)[0].replace('"', '')
        return pathlib.Path(path)

    @property
    def uninstall_string_path(self) -> Optional[pathlib.Path]:
        uspath = self.uninstall_string
        if not uspath:
            return None
        if uspath.startswith("MsiExec.exe"):
            return None
        if '"' not in uspath:
            return pathlib.Path(uspath)
        m = re.match(r'"(.+?)"', uspath)
        if m:
            return pathlib.Path(m.group(1))
        return None


class WindowsRegistryClient:
    _UNINSTALL_LOCATION = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
    _LOOKUP_REGISTRY_HIVES = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]

    def __init__(self):
        self.__uninstall_keys = []
        if self._is_os_64bit():
            self._ARCH_KEYS = [winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY]
        else:
            self._ARCH_KEYS = [0]

    @property
    def uninstall_keys(self):
        return self.__uninstall_keys

    @staticmethod
    def _is_os_64bit():
        return platform.machine().endswith('64')

    def __get_value(self, subkey, prop, optional=False):
        try:
            return winreg.QueryValueEx(subkey, prop)[0]
        except FileNotFoundError:
            if optional:
                return None
            raise

    def __parse_uninstall_key(self, name, subkey):
        return UninstallKey(
            key_name = name,
            display_name = self.__get_value(subkey, 'DisplayName'),
            uninstall_string = self.__get_value(subkey, 'UninstallString'),
            quiet_uninstall_string = self.__get_value(subkey, 'QuietUninstallString', optional=True),
            _display_icon = self.__get_value(subkey, 'DisplayIcon', optional=True),
            _install_location = self.__get_value(subkey, 'InstallLocation', optional=True),
        )

    def _iterate_uninstall_keys(self):
        for view_key in self._ARCH_KEYS:
            for hive in self._LOOKUP_REGISTRY_HIVES:
                with winreg.OpenKey(hive, self._UNINSTALL_LOCATION, 0, winreg.KEY_READ | view_key) as key:
                    subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(subkeys):
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            yield (subkey_name, subkey)

    def refresh(self):
        self.__uninstall_keys.clear()
        for name, subkey in self._iterate_uninstall_keys():
            try:
                ukey = self.__parse_uninstall_key(name, subkey)
            except FileNotFoundError:
                # logging.debug(f'key {name} do not have all required fields. Skip')
                continue
            else:
                self.__uninstall_keys.append(ukey)


class WindowsAppFinder:
    def __init__(self):
        self._reg = WindowsRegistryClient()
        self._pathfinder = PathFinder(HP.WINDOWS)

    @staticmethod
    def __matches(human_name: str, uk: UninstallKey) -> bool:
        def escape(x):
            return x.replace(':', '').lower()
        def escaped_matches(a, b):
            return escape(a) == escape(b)
        def norm(x):
            return x.replace(" III", " 3").replace(" II", " 2")

        if human_name == uk.display_name \
            or escaped_matches(human_name, uk.display_name) \
            or uk.key_name.startswith(human_name):
            return True
        if uk.install_location is not None:
            path = pathlib.PurePath(uk.install_location).name
            if escaped_matches(human_name, path):
                return True
        upath = uk.uninstall_string_path
        if upath and escape(human_name) in str(upath):
            return True
        # quickfix for Torchlight II ect., until better solution will be provided
        return escaped_matches(norm(human_name), norm(uk.display_name))

    def _match_uninstall_key(self, human_name: str) -> Optional[UninstallKey]:
        for uk in self._reg.uninstall_keys:
            if self.__matches(human_name, uk):
                return uk
        return None

    def find_executable(self, human_name: str, uk: UninstallKey) -> Optional[pathlib.Path]:
        """ Returns most probable app executable of given uk or None if not found.
        """
        # sometimes display_icon link to main executable
        upath = uk.uninstall_string_path
        ipath = uk.display_icon
        if ipath and ipath.suffix == '.exe':
            if ipath != upath and 'unins' not in str(ipath):  # exclude uninstaller
                return ipath
        return None

        # get install_location if present; if not, check for uninstall or display_icon parents
        udir = upath.parent if upath else None
        idir = ipath.parent if ipath else None
        location = uk.install_location or udir or idir

        # find all executables and get best maching (exclude uninstall_path)
        if location and location.exists():
            executables = set(self._pathfinder.find_executables(location)) - {upath}
            best_match = self._pathfinder.choose_main_executable(human_name, executables)
            if best_match is None:
                logging.warning(f'Main exe not found for {human_name}; \
                    loc: {uk.install_location}; up: {upath}; ip: {ipath}; execs: {executables}')
                return None
            return pathlib.Path(best_match)
        return None

    def find_local_game(self, machine_name: str, human_name: str) -> Optional[LocalHumbleGame]:
        uk = self._match_uninstall_key(human_name)
        if uk is not None:
            exe = self.find_executable(human_name, uk)
            if exe is not None:
                return LocalHumbleGame(machine_name, exe, uk.uninstall_string)
            logging.warning(f"Uninstall key matched, but cannot find game location for [{human_name}]; uk: {uk}")
        return None

    def refresh(self):
        self._reg.refresh()

