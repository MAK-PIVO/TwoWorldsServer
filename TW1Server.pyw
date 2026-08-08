#!/usr/bin/env python3
"""
TW1 Control Center
-------------------
All-in-one launcher/dashboard for the Two Worlds 1 revival tools: the
Community Lobby Server, the Solo-Multiplayer Server and the Local Activation
Server.

This file is fully self-contained on purpose: the three server scripts are
embedded below (base64-encoded, see _EMBEDDED_SOURCES near the bottom of the
file) and decoded/exec'd into memory at runtime. Nothing is read from sibling
files or folders, so this single .pyw can be copied/moved anywhere and still
run. Its own runtime data (Config.ini, ServerData.db, PlayerData/) lives in
%LOCALAPPDATA%\\TW1 Control Center instead of "next to the script".

Pure standard library (tkinter) - nothing to install, just run with Python 3.
"""
import os
import sys
import io
import re
import base64
import types
import tempfile
import configparser
import queue
import threading
import subprocess
import socketserver
import socket
import ctypes
import webbrowser
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    import winreg
except ImportError:
    winreg = None  # not on Windows - the game-settings/registry tab will disable itself

APP_TITLE = 'TW1 Control Center'
DEFAULT_PORT = 17171

#Persistent data for the embedded servers (database, config, saved player
#data) lives here - independent of wherever this script itself is placed.
APP_DATA_DIR = os.path.join(
    os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or tempfile.gettempdir(),
    'TW1 Control Center')
os.makedirs(APP_DATA_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Localisation - Russian (default, matches the original interface) and
# English on a runtime toggle, no restart needed. Scope is deliberately the
# "core" UI (tab titles, buttons, labels, checkboxes, dialogs) - the long
# gray explanatory paragraphs throughout stay Russian-only; translating those
# too was explicitly descoped to keep this change-sized rather than
# rewriting the whole file. `App._tr()` registers each translated widget so
# `App._apply_language()` can re-render everything in place on toggle,
# without touching widget state (server running/stopped, entered text,
# button enabled/disabled) - only `.configure(text=...)`-type calls, so
# switching language mid-session can't lose or reset anything.
APP_SETTINGS_PATH = os.path.join(APP_DATA_DIR, 'AppSettings.ini')
_LANG = 'ru'


def read_app_language():
    cfg = configparser.ConfigParser()
    if os.path.exists(APP_SETTINGS_PATH):
        try:
            cfg.read(APP_SETTINGS_PATH)
            lang = cfg.get('app', 'Language', fallback='ru')
            if lang in ('ru', 'en'):
                return lang
        except Exception:
            pass
    return 'ru'


def write_app_language(lang):
    cfg = configparser.ConfigParser()
    cfg['app'] = {'Language': lang}
    with open(APP_SETTINGS_PATH, 'w', encoding='utf-8') as f:
        cfg.write(f)


def T(key, **kwargs):
    """Look up `key` in the current language, with .format(**kwargs) applied
    if any were given. Falls back to the key itself (visibly wrong rather
    than silently blank) if a translation is missing - that's a bug to fix,
    not something to hide."""
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(_LANG, entry.get('ru', key))
    return text.format(**kwargs) if kwargs else text


#Filled in progressively alongside each tab's widgets below - see the
#_build_*_tab methods. Every value MUST have both 'ru' and 'en' keys.
_STRINGS = {
    'app.ready': {'ru': 'готов к работе.', 'en': 'ready.'},
    'app.data_dir_label': {'ru': 'Данные сервера (БД, Config.ini, сохранения):',
                            'en': 'Server data (DB, Config.ini, saves):'},
    'tab.server': {'ru': '🖥  Сервер', 'en': '🖥  Server'},
    'tab.settings': {'ru': '⚙  Настройки', 'en': '⚙  Settings'},
    'tab.game': {'ru': '🎮  Игра', 'en': '🎮  Game'},
    'tab.network': {'ru': '🌐  Сеть', 'en': '🌐  Network'},
    'tab.activation': {'ru': '🔑  Активация', 'en': '🔑  Activation'},
    'tab.log': {'ru': '📜  Лог', 'en': '📜  Log'},

    'server.mode_header': {'ru': 'Режим сервера', 'en': 'Server mode'},
    'server.mode_lobby': {'ru': 'Лобби-сервер (кооператив по сети)', 'en': 'Lobby server (co-op over network)'},
    'server.mode_solo': {'ru': 'Соло-сервер (одиночная игра оффлайн)', 'en': 'Solo server (offline single-player)'},
    'server.port': {'ru': 'Порт:', 'en': 'Port:'},
    'server.start': {'ru': '▶ Запустить', 'en': '▶ Start'},
    'server.stop': {'ru': '■ Остановить', 'en': '■ Stop'},
    'server.status_stopped': {'ru': '● Остановлен', 'en': '● Stopped'},
    'server.status_stopping': {'ru': '● Останавливается...', 'en': '● Stopping...'},
    'server.status_running': {'ru': '● Запущен на порту {port}', 'en': '● Running on port {port}'},
    'server.players_online': {'ru': 'Игроки онлайн', 'en': 'Players online'},
    'server.col_name': {'ru': 'Имя', 'en': 'Name'},
    'server.col_town': {'ru': 'Город/канал', 'en': 'Town/channel'},
    'server.col_game': {'ru': 'Игра', 'en': 'Game'},
    'server.col_pos': {'ru': 'Позиция', 'en': 'Position'},
    'server.col_login': {'ru': 'Вход в сеть', 'en': 'Logged in'},
    'server.kick': {'ru': 'Кикнуть', 'en': 'Kick'},
    'server.delete_char': {'ru': 'Удалить персонажа...', 'en': 'Delete character...'},
    'server.delete_by_name': {'ru': 'Удалить персонажа по нику:', 'en': 'Delete character by name:'},
    'server.delete': {'ru': 'Удалить', 'en': 'Delete'},
    'server.summary': {'ru': 'Игроков онлайн: {n}', 'en': 'Players online: {n}'},
    'server.summary_lobby_only': {'ru': '(статистика игроков доступна только в режиме лобби-сервера)',
                                   'en': '(player stats are only available in lobby-server mode)'},
    'server.mode_change_title': {'ru': 'Режим сервера', 'en': 'Server mode'},
    'server.mode_change_body': {'ru': 'Остановите текущий сервер, прежде чем менять режим.',
                                 'en': 'Stop the current server before changing mode.'},
    'server.bad_port_title': {'ru': 'Некорректный порт', 'en': 'Invalid port'},
    'server.bad_port_body': {'ru': 'Порт должен быть числом от 1 до 65535.',
                              'en': 'Port must be a number from 1 to 65535.'},
    'server.already_running_title': {'ru': 'Уже запущено', 'en': 'Already running'},
    'server.already_running_body': {'ru': 'Сначала остановите {label}-сервер.',
                                     'en': 'Stop the {label} server first.'},
    'server.start_failed_title': {'ru': 'Не удалось запустить сервер', 'en': 'Could not start the server'},
    'server.start_failed_port_busy': {'ru': 'Порт {port} уже занят или недоступен:\n{err}',
                                       'en': 'Port {port} is already in use or unavailable:\n{err}'},
    'server.delete_confirm_title': {'ru': 'Удалить персонажа', 'en': 'Delete character'},
    'server.delete_confirm_body': {'ru': 'Безвозвратно удалить аккаунт "{name}" и все его сохранения?\n'
                                          'Это действие нельзя отменить.',
                                    'en': 'Permanently delete the account "{name}" and all its saves?\n'
                                          'This cannot be undone.'},
    'server.module_load_error_title': {'ru': 'Ошибка', 'en': 'Error'},
    'server.module_load_error_body': {'ru': 'Не удалось загрузить модуль лобби-сервера:\n{err}',
                                       'en': 'Could not load the lobby server module:\n{err}'},
    'server.delete_error_title': {'ru': 'Ошибка', 'en': 'Error'},
    'server.delete_error_body': {'ru': 'Не удалось удалить "{name}":\n{err}',
                                  'en': 'Could not delete "{name}":\n{err}'},
    'server.delete_done_title': {'ru': 'Готово', 'en': 'Done'},
    'server.delete_done_body': {'ru': 'Персонаж "{name}" удалён.', 'en': 'Character "{name}" deleted.'},
    'server.delete_notfound_title': {'ru': 'Не найдено', 'en': 'Not found'},
    'server.delete_notfound_body': {'ru': 'Аккаунт "{name}" не найден.', 'en': 'Account "{name}" not found.'},
    'server.kick_confirm_title': {'ru': 'Кикнуть игрока', 'en': 'Kick player'},
    'server.kick_confirm_body': {'ru': 'Отключить игрока "{name}" от сервера?',
                                  'en': 'Disconnect player "{name}" from the server?'},
    'server.stopped_title': {'ru': 'Сервер остановлен', 'en': 'Server stopped'},
    'server.stopped_body': {'ru': 'Сервер больше не запущен — игрок не кикнут.',
                             'en': 'The server is no longer running - player not kicked.'},

    'settings.header': {'ru': 'Настройки лобби-сервера', 'en': 'Lobby server settings'},
    'settings.server_name': {'ru': 'Название сервера:', 'en': 'Server name:'},
    'settings.motd': {'ru': 'MOTD (приветствие):', 'en': 'MOTD (welcome message):'},
    'settings.autoreg': {'ru': 'Автоматически создавать аккаунт при первом входе',
                          'en': 'Automatically create an account on first login'},
    'settings.anylogin': {'ru': '(ОТЛАДКА) Пускать с любым паролем без проверки — НЕ включать на публичном сервере',
                           'en': '(DEBUG) Accept any password unchecked - do NOT enable on a public server'},
    'settings.sync_header': {'ru': 'Синхронизация', 'en': 'Synchronisation'},
    'settings.pos_hz': {'ru': 'Частота синхронизации позиций (раз/сек):', 'en': 'Position sync rate (times/sec):'},
    'settings.idle_timeout': {'ru': 'Таймаут простоя (сек., 0 — выключен):', 'en': 'Idle timeout (sec, 0 = off):'},
    'settings.keepalive': {'ru': 'Отправлять keepalive-пакеты простаивающим клиентам (эксперимент)',
                            'en': 'Send keepalive packets to idle clients (experimental)'},
    'settings.load_current': {'ru': 'Загрузить текущие', 'en': 'Load current'},
    'settings.save': {'ru': 'Сохранить', 'en': 'Save'},
    'settings.bad_pos_hz': {'ru': 'Частота синхронизации позиций должна быть числом.',
                             'en': 'The position sync rate must be a number.'},
    'settings.bad_idle_timeout': {'ru': 'Таймаут простоя должен быть целым числом секунд.',
                                   'en': 'The idle timeout must be a whole number of seconds.'},
    'settings.saved_title': {'ru': 'Готово', 'en': 'Done'},
    'settings.saved_body': {'ru': 'Настройки сохранены и применены.', 'en': 'Settings saved and applied.'},

    'game.windows_only': {'ru': 'Запуск игры доступен только на Windows.', 'en': 'Launching the game is only available on Windows.'},
    'game.path_header': {'ru': 'Путь к игре', 'en': 'Game path'},
    'game.browse': {'ru': 'Обзор...', 'en': 'Browse...'},
    'game.launch': {'ru': '▶ Запустить игру', 'en': '▶ Launch game'},
    'game.single_core': {
        'ru': 'Одно ядро ЦП (эксперимент, против рассинхрона)',
        'en': 'Single CPU core (experimental, against desync)'},
    'game.fov_header': {'ru': 'Экспериментально: широкоформатный FOV', 'en': 'Experimental: widescreen FOV'},
    'game.fov_apply': {'ru': 'Применить', 'en': 'Apply'},
    'game.fov_reset': {'ru': 'Сбросить (0/0 - авто)', 'en': 'Reset (0/0 - auto)'},
    'game.browse_title': {'ru': 'Выбери {exe}', 'en': 'Select {exe}'},
    'game.filetype_exe': {'ru': 'Исполняемый файл', 'en': 'Executable'},
    'game.filetype_all': {'ru': 'Все файлы', 'en': 'All files'},
    'game.bad_fov': {'ru': 'AspectX/AspectY должны быть целыми числами.', 'en': 'AspectX/AspectY must be whole numbers.'},
    'game.registry_error_title': {'ru': 'Ошибка реестра', 'en': 'Registry error'},
    'game.not_found_title': {'ru': 'Не найдено', 'en': 'Not found'},
    'game.not_found_body': {'ru': '{exe} не найден рядом с указанным путём. Укажи папку игры (или сам этот файл) и нажми снова.',
                             'en': '{exe} not found next to the given path. Point to the game folder (or the file itself) and try again.'},
    'game.launch_failed_title': {'ru': 'Не удалось запустить игру', 'en': 'Could not launch the game'},

    'network.reach_header': {'ru': 'Доступность сервера из интернета', 'en': 'Reachability from the internet'},
    'network.local_ip': {'ru': 'Локальный IP:', 'en': 'Local IP:'},
    'network.public_ip': {'ru': 'Публичный IP:', 'en': 'Public IP:'},
    'network.determining': {'ru': '(определяется...)', 'en': '(determining...)'},
    'network.server_port': {'ru': 'Порт сервера:', 'en': 'Server port:'},
    'network.refresh': {'ru': '🔄 Обновить', 'en': '🔄 Refresh'},
    'network.check_port': {'ru': '🌐 Проверить порт вручную (браузер)', 'en': '🌐 Check port manually (browser)'},
    'network.try_upnp': {'ru': '⚡ Попробовать пробросить порт автоматически (UPnP)',
                          'en': '⚡ Try to forward the port automatically (UPnP)'},
    'network.server_list_header': {'ru': 'Список серверов в игре', 'en': 'In-game server list'},
    'network.col_name': {'ru': 'Название (не менять)', 'en': 'Name (don\'t change)'},
    'network.col_addr': {'ru': 'Адрес (двойной клик - изменить)', 'en': 'Address (double-click to edit)'},
    'network.point_all_to': {'ru': 'Направить ВСЕ пункты на:', 'en': 'Point ALL entries to:'},
    'network.preset_lan': {'ru': 'Локальный IP (LAN)', 'en': 'Local IP (LAN)'},
    'network.preset_public': {'ru': 'Публичный IP (интернет, 2ip.ru)', 'en': 'Public IP (internet, 2ip.ru)'},
    'network.preset_localhost': {'ru': 'localhost (эта же машина)', 'en': 'localhost (this machine)'},
    'network.preset_127001': {'ru': '127.0.0.1 (эта же машина)', 'en': '127.0.0.1 (this machine)'},
    'network.preset_custom': {'ru': 'Свой адрес...', 'en': 'Custom address...'},
    'network.apply': {'ru': 'Применить', 'en': 'Apply'},
    'network.restore_defaults': {'ru': '🔄 Восстановить дефолтные адреса Reality Pump',
                                  'en': '🔄 Restore default Reality Pump addresses'},
    'network.save_to_game': {'ru': 'Сохранить в игру', 'en': 'Save to game'},
    'network.enter_addr': {'ru': 'Введи адрес (IP или домен) в поле рядом со списком.',
                            'en': 'Enter an address (IP or domain) in the field next to the list.'},
    'network.public_ip_failed': {'ru': 'Не удалось определить публичный IP (нет интернета?). '
                                        'Посмотри вручную во вкладке "Сеть" выше.',
                                  'en': 'Could not determine the public IP (no internet?). '
                                        'Check it manually in the Network tab above.'},
    'network.addrs_replaced_title': {'ru': 'Адреса заменены', 'en': 'Addresses replaced'},
    'network.addrs_replaced_body': {'ru': 'Все пункты списка теперь указывают на {addr}. Порт сервера: {port} '
                                           '(задаётся во вкладке "Сервер"). Не забудь нажать "Сохранить в игру".',
                                     'en': 'Every entry now points to {addr}. Server port: {port} '
                                           '(set on the Server tab). Don\'t forget to click "Save to game".'},
    'network.restore_confirm_title': {'ru': 'Восстановить дефолт', 'en': 'Restore defaults'},
    'network.restore_confirm_body': {'ru': 'Вернуть оригинальные (мёртвые) адреса Reality Pump? '
                                            'Полезно, если что-то сломалось и нужно начать заново.',
                                      'en': 'Restore the original (dead) Reality Pump addresses? '
                                            'Useful if something broke and you need to start over.'},
    'network.saved_body': {'ru': 'Сохранено. Названия пунктов в игре останутся прежними - '
                                  'изменились только адреса, на которые они указывают.',
                            'en': 'Saved. The entry names in-game stay the same - only the addresses '
                                  'they point to changed.'},
    'network.undetermined': {'ru': 'не определён', 'en': 'undetermined'},
    'network.public_ip_undetermined': {'ru': 'не удалось определить (нет интернета?)',
                                        'en': 'could not determine (no internet?)'},
    'network.opened_port_checker': {'ru': 'Открыл canyouseeme.org в браузере. Впиши туда порт {port} '
                                           'и нажми "Check Port" на сайте.',
                                     'en': 'Opened canyouseeme.org in the browser. Enter port {port} there '
                                           'and click "Check Port" on the site.'},
    'network.bad_port': {'ru': 'Некорректный порт.', 'en': 'Invalid port.'},
    'network.upnp_searching': {'ru': 'Ищу роутер по UPnP...', 'en': 'Looking for the router via UPnP...'},
    'network.upnp_success': {'ru': 'Готово! Роутер подтвердил проброс порта {port} (TCP) на {ip}. '
                                    'Проверь через "Проверить порт вручную" выше, чтобы убедиться.',
                              'en': 'Done! The router confirmed forwarding port {port} (TCP) to {ip}. '
                                    'Check with "Check port manually" above to be sure.'},
    'network.upnp_failed': {'ru': 'Не получилось автоматически: {err}\n\n'
                                   'Это нормально для многих роутеров (UPnP выключен по умолчанию) - '
                                   'придётся пробросить порт {port} (TCP) вручную в настройках роутера '
                                   'на локальный IP {ip}.',
                             'en': 'Automatic forwarding failed: {err}\n\n'
                                   'That\'s normal for many routers (UPnP is off by default) - '
                                   'you\'ll need to forward port {port} (TCP) manually in the router settings '
                                   'to local IP {ip}.'},
    'network.upnp_unexpected': {'ru': 'Неожиданная ошибка: {err}', 'en': 'Unexpected error: {err}'},

    'activation.header': {'ru': 'Локальный сервер активации', 'en': 'Local activation server'},
    'activation.body': {'ru': 'Официальные серверы активации Two Worlds давно отключены. Этот инструмент временно '
                               'подменяет адрес сервера активации в реестре на локальный, отвечает на один запрос игры '
                               'и возвращает исходный адрес обратно.\n\n'
                               '• Требуются права администратора (появится запрос UAC).\n'
                               '• Инструмент откроется в отдельном консольном окне — там же будет запрошен серийный '
                               'ключ, если он не найден в реестре автоматически.\n'
                               '• После завершения окно можно закрыть — реестр возвращается в исходное состояние.\n\n'
                               'Порядок действий: нажмите кнопку ниже, дождитесь "Awaiting connection from game...", '
                               'затем запустите Two Worlds и пройдите активацию как обычно.',
                         'en': 'Two Worlds\' official activation servers have long been shut down. This tool '
                               'temporarily swaps the activation server address in the registry for a local one, '
                               'answers a single request from the game, and restores the original address.\n\n'
                               '• Requires administrator rights (a UAC prompt will appear).\n'
                               '• The tool opens in a separate console window - it will ask for your serial key '
                               'there if it isn\'t found in the registry automatically.\n'
                               '• You can close the window once it\'s done - the registry is restored automatically.\n\n'
                               'Steps: click the button below, wait for "Awaiting connection from game...", '
                               'then launch Two Worlds and go through activation as usual.'},
    'activation.launch': {'ru': '▶ Запустить сервер активации', 'en': '▶ Start activation server'},
    'activation.launch_failed_title': {'ru': 'Не удалось запустить', 'en': 'Could not start'},

    'log.header': {'ru': 'Журнал событий', 'en': 'Event log'},
    'log.clear': {'ru': 'Очистить', 'en': 'Clear'},
    'log.copy': {'ru': 'Копировать всё', 'en': 'Copy all'},
    'log.openfile': {'ru': 'Открыть файл', 'en': 'Open log file'},
    'log.copied': {'ru': 'Лог скопирован в буфер обмена.', 'en': 'Log copied to clipboard.'},
    'log.nofile_title': {'ru': 'Файл лога', 'en': 'Log file'},
    'log.nofile_body': {'ru': 'Файл лога ещё не создан.', 'en': 'The log file has not been created yet.'},
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _load_embedded(name, source_b64, external_data_dir=None):
    """Decode an embedded, base64-encoded source blob and exec() it into a
    fresh, isolated module namespace - no files touch disk, no sibling
    folders required. external_data_dir is pre-seeded into the module's
    globals as _EXTERNAL_DATA_DIR before the module body runs, which TW1CS.py
    and the Solo server both check to decide where to keep their data
    (falling back to a path next to themselves when run standalone, which
    doesn't apply here since there is no real file for them)."""
    source = base64.b64decode(source_b64).decode('utf-8')
    mod = types.ModuleType(name)
    mod.__file__ = f'<embedded {name}>'
    if external_data_dir:
        mod.__dict__['_EXTERNAL_DATA_DIR'] = external_data_dir
    sys.modules[name] = mod
    exec(compile(source, f'<embedded {name}>', 'exec'), mod.__dict__)
    return mod


def _console_python():
    """Path to a console-attached python.exe, even if we were launched via
    pythonw.exe (double-clicking a .pyw has no console at all, but the
    activation server needs one to prompt for a serial key)."""
    exe = sys.executable
    lower = exe.lower()
    if lower.endswith('pythonw.exe'):
        candidate = exe[:-len('pythonw.exe')] + 'python.exe'
        if os.path.exists(candidate):
            return candidate
    return exe


class QueueWriter(io.TextIOBase):
    """Tees writes to a queue (for the GUI log) and to the original stream
    (so console output still works when run from a terminal)."""
    def __init__(self, q, orig=None, logfile=None):
        self.q = q
        self.orig = orig
        # Same log, on disk. The GUI log box is a live view - it is trimmed
        # when it grows, it scrolls under the cursor while the server is busy,
        # and selecting a long run of lines out of it to report a problem is
        # painful. The file has none of those constraints.
        self.logfile = logfile
        self._lock = threading.Lock()
        self._last_flush = 0.0
    def write(self, s):
        if s:
            self.q.put(s)
            if self.orig is not None:
                try:
                    self.orig.write(s)
                except Exception:
                    pass
            if self.logfile is not None:
                # Every server thread writes through here, so the file needs
                # the lock even though the queue does not.
                with self._lock:
                    try:
                        self.logfile.write(s)
                        # Flushing every line meant a synchronous disk write
                        # inside whichever server thread happened to print -
                        # on the connection handlers, that is latency added to
                        # the players. Flush on a short timer instead: the
                        # panel outlives the game, so a crashed *game* never
                        # costs us the log, and at most half a second of tail
                        # is at risk if the panel itself is killed.
                        now = time.monotonic()
                        if now - self._last_flush > 0.5:
                            self.logfile.flush()
                            self._last_flush = now
                    except Exception:
                        pass
        return len(s)
    def flush(self):
        if self.orig is not None:
            try:
                self.orig.flush()
            except Exception:
                pass
        if self.logfile is not None:
            with self._lock:
                try:
                    self.logfile.flush()
                    self._last_flush = time.monotonic()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Two Worlds 1 game registry helpers
#
# Confirmed by exporting HKCU\Software\Reality Pump\TwoWorlds\Graphics from a
# real installed copy of the game (not guessed): Width/Height/FullScreen are
# plain DWORDs the game reads on launch. ForceCameraAspectX/Y exist in the
# same key but their effect hasn't been confirmed against a live game - that
# control is clearly labelled experimental in the UI with an easy revert.
# ---------------------------------------------------------------------------

TW_GRAPHICS_KEY = r'Software\Reality Pump\TwoWorlds\Graphics'
TW_MODS_KEY = r'Software\Reality Pump\TwoWorlds\Mods'

GAME_SETTINGS_PATH = os.path.join(APP_DATA_DIR, 'GameSettings.ini')
# Which build to launch, in order of preference - see _resolve_game_exe().
# TwoWorldsExtended.exe leads because it is the Epic Edition's own main build
# and the one TWSE (twse.dll) hooks: every TWSE crash log in the install names
# it. TwoWorlds_RADEON.exe is a legacy ATI-specific renderer build; forcing it
# unconditionally, as this used to, meant anyone on an NVIDIA card was running
# the wrong renderer - and the crash reports from exactly that setup fault
# inside nvd3dum.dll, NVIDIA's own D3D9 driver. It stays last as a fallback
# for installs that genuinely have nothing else.
GAME_EXE_CANDIDATES = ('TwoWorldsExtended.exe', 'TwoWorlds.exe', 'TwoWorlds_RADEON.exe')
GAME_EXE_NAME = GAME_EXE_CANDIDATES[0]


def read_graphics_settings():
    """Returns the experimental FOV fields, or None if the key doesn't exist
    yet (game never launched / settings never touched)."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TW_GRAPHICS_KEY) as key:
            def get(name, default=0):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            return {
                'ForceCameraAspectX': get('ForceCameraAspectX', 0),
                'ForceCameraAspectY': get('ForceCameraAspectY', 0),
            }
    except OSError:
        return None


def write_graphics_settings(values):
    """values: dict of name -> int (DWORD). Creates the key if missing."""
    if winreg is None:
        raise RuntimeError('winreg недоступен (не Windows)')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TW_GRAPHICS_KEY, access=winreg.KEY_WRITE) as key:
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value) & 0xFFFFFFFF)


def read_mod_states():
    """Returns {modfilename: 0/1} from the registry, or {} if none set yet."""
    if winreg is None:
        return {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TW_MODS_KEY) as key:
            result = {}
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                result[name] = value
                i += 1
            return result
    except OSError:
        return {}


def set_mod_state(modname, enabled):
    if winreg is None:
        raise RuntimeError('winreg недоступен (не Windows)')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TW_MODS_KEY, access=winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, modname, 0, winreg.REG_DWORD, 1 if enabled else 0)


# ---------------------------------------------------------------------------
# In-game server list (the one shown in TW1's own "select server" dialog).
# Confirmed real, not guessed: EarthNet_ServerPort in this key (0x4313 =
# 17171 decimal) matches the lobby server's own default port exactly. Out of
# the box the address list only contains Reality Pump's long-dead official
# servers (warnet.2-worlds.com and friends) - replacing it with your own
# server is what actually lets the in-game browser find it instead of
# timing out against addresses that don't exist anymore.
# ---------------------------------------------------------------------------

TW_NETWORK_KEY = r'Software\Reality Pump\TwoWorlds\Network'


def read_server_list():
    """Returns [(name, address), ...] currently in the game's server list."""
    if winreg is None:
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TW_NETWORK_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, 'EarthNet_ServersAddresses')
    except OSError:
        return []
    parts = re.findall(r'"([^"]*)"', raw)
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def write_server_list(pairs):
    """pairs: [(name, address), ...]. Overwrites the whole list (this is the
    point - it's how the dead default entries get removed)."""
    if winreg is None:
        raise RuntimeError('winreg недоступен (не Windows)')
    # The format is quote-delimited, so a stray '"' typed into the address cell
    # would shift every following field and leave the game with a garbled list.
    raw = ''.join(f'"{str(name).replace(chr(34), "")}""{str(addr).replace(chr(34), "")}"'
                  for name, addr in pairs)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TW_NETWORK_KEY, access=winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, 'EarthNet_ServersAddresses', 0, winreg.REG_SZ, raw)


def read_server_port():
    if winreg is None:
        return DEFAULT_PORT
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TW_NETWORK_KEY) as key:
            return winreg.QueryValueEx(key, 'EarthNet_ServerPort')[0]
    except OSError:
        return DEFAULT_PORT


def write_server_port(port):
    if winreg is None:
        raise RuntimeError('winreg недоступен (не Windows)')
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TW_NETWORK_KEY, access=winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, 'EarthNet_ServerPort', 0, winreg.REG_DWORD, int(port) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Networking helpers - local/public IP display, and a best-effort automatic
# UPnP port forward on the router (falls back gracefully to "do it by hand"
# instructions if the router doesn't support/allow it, which is common and
# not a bug in this code).
# ---------------------------------------------------------------------------

def get_local_ip():
    """The LAN IP this machine would use to reach the internet - found via
    the classic no-actual-traffic UDP trick, works even with no default
    route configured oddly."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def get_public_ip(timeout=5):
    # 2ip.ru serves a plain-text IP (no HTML) to non-browser clients - this
    # is standard, widely-relied-on behaviour of that service, confirmed
    # working. ipify.org is kept as a fallback in case 2ip is unreachable
    # (e.g. blocked in some region) so the feature degrades gracefully
    # rather than just failing.
    sources = [
        ('https://2ip.ru', {'User-Agent': 'curl/8.0'}),
        ('https://api.ipify.org', {}),
    ]
    for url, headers in sources:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read().decode('ascii', errors='ignore').strip()
            if re.match(r'^\d{1,3}(\.\d{1,3}){3}$', ip):
                return ip
        except Exception:
            continue
    return None


class UPnPError(Exception):
    pass


def upnp_add_port_mapping(port, description='TW1 Lobby Server', protocol='TCP', timeout=4):
    """Best-effort automatic port forward via UPnP IGD. Returns the LAN IP the
    mapping now points at. Raises UPnPError with a human-readable reason on
    any failure - callers should treat that as 'do it manually', not a bug.
    """
    local_ip = get_local_ip()

    # 1. SSDP discovery: multicast M-SEARCH, collect the first IGD LOCATION url
    ssdp_req = (
        'M-SEARCH * HTTP/1.1\r\n'
        'HOST: 239.255.255.250:1900\r\n'
        'MAN: "ssdp:discover"\r\n'
        'MX: 2\r\n'
        'ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n'
    ).encode('ascii')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    location = None
    try:
        sock.sendto(ssdp_req, ('239.255.255.250', 1900))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            text = data.decode('utf-8', errors='ignore')
            m = [l for l in text.split('\r\n') if l.lower().startswith('location:')]
            if m:
                location = m[0].split(':', 1)[1].strip()
                break
    finally:
        sock.close()
    if not location:
        raise UPnPError('Роутер не ответил на UPnP-запрос (SSDP). '
                         'Либо UPnP выключен в настройках роутера, либо он его не поддерживает.')

    # 2. Fetch the device description XML, find the WANIPConnection (or PPP
    # variant) service and its controlURL.
    try:
        with urllib.request.urlopen(location, timeout=timeout) as r:
            desc_xml = r.read()
        base_url = location[:location.index('/', 8)] if location.count('/') > 2 else location
        root = ET.fromstring(desc_xml)
    except Exception as e:
        raise UPnPError(f'Не удалось получить описание устройства с роутера: {e}')

    ns = {'u': 'urn:schemas-upnp-org:device-1-0'}
    control_url = None
    service_type = None
    for svc in root.iter('{urn:schemas-upnp-org:device-1-0}service'):
        st = svc.findtext('{urn:schemas-upnp-org:device-1-0}serviceType', '')
        if 'WANIPConnection' in st or 'WANPPPConnection' in st:
            cu = svc.findtext('{urn:schemas-upnp-org:device-1-0}controlURL', '')
            if cu:
                control_url = cu
                service_type = st
                break
    if not control_url:
        raise UPnPError('Роутер откликнулся, но не нашёл сервис WANIPConnection в его описании.')
    if not control_url.startswith('http'):
        control_url = base_url + ('' if control_url.startswith('/') else '/') + control_url

    # 3. SOAP AddPortMapping request.
    soap_body = f'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:AddPortMapping xmlns:u="{service_type}">
<NewRemoteHost></NewRemoteHost>
<NewExternalPort>{port}</NewExternalPort>
<NewProtocol>{protocol}</NewProtocol>
<NewInternalPort>{port}</NewInternalPort>
<NewInternalClient>{local_ip}</NewInternalClient>
<NewEnabled>1</NewEnabled>
<NewPortMappingDescription>{description}</NewPortMappingDescription>
<NewLeaseDuration>0</NewLeaseDuration>
</u:AddPortMapping>
</s:Body>
</s:Envelope>'''.encode('utf-8')

    req = urllib.request.Request(control_url, data=soap_body, method='POST', headers={
        'Content-Type': 'text/xml; charset="utf-8"',
        'SOAPAction': f'"{service_type}#AddPortMapping"',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore')
        raise UPnPError(f'Роутер отклонил запрос на проброс порта (HTTP {e.code}). '
                         f'Возможно, UPnP отключён в настройках роутера.\n{body[:300]}')
    except Exception as e:
        raise UPnPError(f'Не удалось отправить запрос роутеру: {e}')

    return local_ip


# ---------------------------------------------------------------------------
# Server controllers - wrap the actual socketserver instances, run them on a
# background thread, and expose simple start()/stop()/is_running() to the UI.
# ---------------------------------------------------------------------------

class ServerController:
    label = 'Server'

    def __init__(self):
        self.module = None
        self.server = None
        self._thread = None
        self._running = False

    def is_running(self):
        return self._running

    def ensure_loaded(self):
        raise NotImplementedError

    def _build_server(self, port):
        raise NotImplementedError

    def start(self, port):
        self.ensure_loaded()
        if self._running:
            return
        self.server = self._build_server(port)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            with self.server:
                self.server.serve_forever()
        except Exception as e:
            print(f'[{self.label}] Server crashed: {e}')
        finally:
            self._running = False
            print(f'[{self.label}] Server stopped.')

    def stop(self):
        # Deliberately blocking (briefly, bounded by the ~1s poll interval):
        # a fire-and-forget shutdown() left a race where clicking Stop then
        # Start again immediately could see _running still True from the
        # not-yet-finished previous server and silently no-op the restart.
        # Joining here guarantees the old server is fully torn down before
        # this call returns, so the next start() always gets a fresh one.
        if not self._running or self.server is None:
            return
        self.server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._running = False


class LobbyController(ServerController):
    label = 'Lobby'

    def ensure_loaded(self):
        if self.module is None:
            self.module = _load_embedded('tw1cs_lobby', _LOBBY_SOURCE_B64, APP_DATA_DIR)

    def _build_server(self, port):
        self.module._TW_LOBBY_PORT = port
        return self.module.CoreServer()

    def stats(self):
        if not self._running or self.server is None:
            return None
        try:
            return {
                'players': self.server.debug_dict_players(),
                'towns': self.server.debug_dict_towns(),
                'games': self.server.debug_arr_games(),
            }
        except Exception:
            return None  # transient race while the server is mutating state

    def reload_config(self):
        if self.module is not None:
            self.module.CFG = self.module.loadConfig()
            self.module.applyConfig(self.module.CFG)


class SoloController(ServerController):
    label = 'Solo'

    def ensure_loaded(self):
        if self.module is None:
            self.module = _load_embedded('tw1_solo_server', _SOLO_SOURCE_B64, APP_DATA_DIR)

    def _build_server(self, port):
        return socketserver.TCPServer(('localhost', port), self.module.ConnectionHandler)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(tk.Tk):
    # Single source of truth for every colour in the app - every widget below
    # references these instead of a one-off hex literal, so the whole panel
    # is one dark, cohesive theme rather than a default-white 'clam' app with
    # a dark log box bolted on (which is what this used to be: the log tab
    # was the only place with deliberate colours, everywhere else was
    # whatever ttk's stock light theme happened to render).
    BG = '#1b1e23'            # window background - the darkest surface
    SURFACE = '#242830'       # panels, frames, labelframes, tab content
    SURFACE_ALT = '#2a2f38'   # tab strip, table headers, menus, idle buttons
    FIELD = '#20242b'         # entries, dropdowns, tables - reads as "recessed"
    BORDER = '#39404b'
    TEXT = '#e7eaf0'
    MUTED = '#8993a6'         # secondary/hint text - replaces the old '#666666',
                               # which was picked for a light background and
                               # went muddy-illegible once the app turned dark
    SELECT_BG = '#35404f'
    ACCENT = '#4f8fd9'
    ACCENT_HOVER = '#6aa3e6'
    OK_COLOR = '#3ecb6c'
    BAD_COLOR = '#e5595f'
    BAD_HOVER = '#c94249'

    def __init__(self):
        super().__init__()
        global _LANG
        _LANG = read_app_language()
        self.title(APP_TITLE)
        self._setup_dpi_and_geometry()

        self.log_queue = queue.Queue()
        self.log_path = os.path.join(APP_DATA_DIR, 'Server.log')
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            # Truncated per run: the interesting log is always the one from
            # the session that just went wrong, and an append-forever file
            # would grow without bound with per-command logging enabled.
            self._log_fh = open(self.log_path, 'w', encoding='utf-8')
        except Exception:
            self._log_fh = None #read-only data dir, etc - not worth failing over
        sys.stdout = QueueWriter(self.log_queue, sys.stdout, self._log_fh)
        sys.stderr = QueueWriter(self.log_queue, sys.stderr, self._log_fh)

        self.lobby = LobbyController()
        self.solo = SoloController()

        #(widget_configure_fn, key, kwargs) for every translated static
        #label/button/etc - see _tr()/_apply_language().
        self._i18n_registry = []

        self._setup_style()
        self._build_ui()

        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.after(200, self._poll_log)
        self.after(1000, self._poll_stats)
        print(f'{APP_TITLE} {T("app.ready")}')
        print(f'{T("app.data_dir_label")} {APP_DATA_DIR}')

    def _tr(self, key, apply_fn, **kwargs):
        """Apply a translated string now via `apply_fn(text)` (e.g.
        `lambda t: widget.configure(text=t)`, or `tree.heading`/`nb.tab`/
        menu.entryconfigure wrappers) and remember it so _apply_language()
        can re-render it after a language switch."""
        apply_fn(T(key, **kwargs))
        self._i18n_registry.append((apply_fn, key, kwargs))

    def _apply_language(self):
        """Re-renders every registered static string plus the handful of
        dynamic labels (server status, player count) that depend on live
        state, not just language - those aren't in the static registry
        because their content changes independent of language too."""
        for apply_fn, key, kwargs in self._i18n_registry:
            try:
                apply_fn(T(key, **kwargs))
            except tk.TclError:
                pass  # widget was destroyed since registering, skip it
        self._sync_server_status_display()
        self._refresh_players_games()
        if hasattr(self, 'addr_preset_combo'):
            self._apply_preset_combo_language()

    def _set_language(self, lang):
        global _LANG
        if lang == _LANG:
            return
        _LANG = lang
        try:
            write_app_language(lang)
        except Exception as e:
            print(f'[i18n] {e}')
        self._apply_language()

    # -- window sizing -------------------------------------------------------
    #Preferred size, but never larger than what actually fits on the user's
    #screen (a fixed 960x640 would overflow on small laptops/low resolutions
    #and get clipped, with no way to reach the buttons at the bottom).
    _PREFERRED_W = 1040
    _PREFERRED_H = 720
    _ABS_MIN_W = 720
    _ABS_MIN_H = 480

    def _setup_dpi_and_geometry(self):
        # Tell Windows we handle our own scaling, otherwise the whole UI gets
        # blurrily bitmap-stretched on high-DPI displays.
        if os.name == 'nt':
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()

        # leave room for the taskbar / window decorations
        max_w = int(sw * 0.92)
        max_h = int(sh * 0.88)

        # Clamp to the preferred size, but never exceed what the screen can
        # show: the min() with max_w/max_h must come LAST, otherwise the
        # absolute minimum wins on very small screens and pushes the window
        # off-screen (caught by the geometry test at 640x480).
        width = min(max(self._ABS_MIN_W, min(self._PREFERRED_W, max_w)), max_w, sw)
        height = min(max(self._ABS_MIN_H, min(self._PREFERRED_H, max_h)), max_h, sh)

        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)  # slightly above centre looks better
        self.geometry(f'{width}x{height}+{x}+{y}')
        self.minsize(min(self._ABS_MIN_W, width), min(self._ABS_MIN_H, height))

    # -- style -------------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style(self)
        try:
            # 'clam' is the only stock theme that actually honours colour
            # overrides on things like Treeview/Notebook/Scrollbar - 'vista'/
            # 'winnative' hardcode native chrome and ignore most of what
            # follows, which is why the previous style only ever managed to
            # recolour buttons and text.
            style.theme_use('clam')
        except tk.TclError:
            pass

        self.configure(background=self.BG)
        base_font = ('Segoe UI', 10)

        # '.' is ttk's fallback style - every class below inherits from it, so
        # setting the background/foreground/font here is what makes plain
        # ttk.Frame/Label/Checkbutton/Radiobutton widgets dark without a
        # style= argument at every call site.
        style.configure('.', background=self.SURFACE, foreground=self.TEXT,
                         fieldbackground=self.FIELD, bordercolor=self.BORDER,
                         darkcolor=self.SURFACE, lightcolor=self.SURFACE,
                         troughcolor=self.SURFACE_ALT, font=base_font)
        style.map('.', foreground=[('disabled', self.MUTED)])

        style.configure('TFrame', background=self.SURFACE)
        style.configure('TLabel', background=self.SURFACE, foreground=self.TEXT)
        style.configure('TLabelframe', background=self.SURFACE, bordercolor=self.BORDER)
        style.configure('TLabelframe.Label', background=self.SURFACE, foreground=self.TEXT,
                         font=('Segoe UI', 10, 'bold'))
        for cls in ('TCheckbutton', 'TRadiobutton'):
            style.configure(cls, background=self.SURFACE, foreground=self.TEXT)
            style.map(cls, background=[('active', self.SURFACE)],
                      indicatorcolor=[('selected', self.ACCENT)])
        style.configure('TSeparator', background=self.BORDER)

        # The notebook strip sits on the window background; only the selected
        # tab lights up to SURFACE, which is what makes it read as "this tab's
        # content continues below it" instead of every tab looking identical.
        style.configure('TNotebook', background=self.BG, bordercolor=self.BORDER)
        style.configure('TNotebook.Tab', padding=(16, 8), background=self.SURFACE_ALT,
                         foreground=self.MUTED, borderwidth=0)
        style.map('TNotebook.Tab',
                  background=[('selected', self.SURFACE)],
                  foreground=[('selected', self.TEXT)])

        style.configure('TEntry', fieldbackground=self.FIELD, foreground=self.TEXT,
                         bordercolor=self.BORDER, insertcolor=self.TEXT, padding=4)
        style.map('TEntry', bordercolor=[('focus', self.ACCENT)],
                  fieldbackground=[('disabled', self.SURFACE)])

        style.configure('TCombobox', fieldbackground=self.FIELD, background=self.SURFACE_ALT,
                         foreground=self.TEXT, arrowcolor=self.MUTED, bordercolor=self.BORDER,
                         padding=4)
        style.map('TCombobox',
                  fieldbackground=[('readonly', self.FIELD)],
                  foreground=[('readonly', self.TEXT)],
                  bordercolor=[('focus', self.ACCENT)])
        # the dropdown listbox is a raw Tk widget underneath TCombobox and
        # isn't reachable through ttk.Style - option_add is the only lever
        self.option_add('*TCombobox*Listbox.background', self.FIELD)
        self.option_add('*TCombobox*Listbox.foreground', self.TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', self.SELECT_BG)

        style.configure('TButton', background=self.SURFACE_ALT, foreground=self.TEXT,
                         bordercolor=self.BORDER, focusthickness=0, padding=(10, 6))
        style.map('TButton', background=[('active', self.SELECT_BG), ('disabled', self.SURFACE)],
                  foreground=[('disabled', self.MUTED)])

        style.configure('Accent.TButton', foreground='white', background=self.ACCENT,
                         bordercolor=self.ACCENT, padding=(14, 7), font=('Segoe UI', 10, 'bold'))
        style.map('Accent.TButton',
                  background=[('active', self.ACCENT_HOVER), ('disabled', self.BORDER)],
                  foreground=[('disabled', self.MUTED)])
        style.configure('Stop.TButton', foreground='white', background=self.BAD_COLOR,
                         bordercolor=self.BAD_COLOR, padding=(14, 7), font=('Segoe UI', 10, 'bold'))
        style.map('Stop.TButton',
                  background=[('active', self.BAD_HOVER), ('disabled', self.BORDER)],
                  foreground=[('disabled', self.MUTED)])

        style.configure('Header.TLabel', font=('Segoe UI', 12, 'bold'),
                         background=self.SURFACE, foreground=self.TEXT)
        style.configure('Status.TLabel', font=('Segoe UI', 10, 'bold'), background=self.SURFACE)

        style.configure('Treeview', background=self.FIELD, fieldbackground=self.FIELD,
                         foreground=self.TEXT, bordercolor=self.BORDER, borderwidth=0,
                         rowheight=24)
        style.map('Treeview', background=[('selected', self.SELECT_BG)],
                  foreground=[('selected', self.TEXT)])
        style.configure('Treeview.Heading', background=self.SURFACE_ALT, foreground=self.TEXT,
                         bordercolor=self.BORDER, relief='flat', font=('Segoe UI', 9, 'bold'))
        style.map('Treeview.Heading', background=[('active', self.SELECT_BG)])

        for orient in ('Vertical', 'Horizontal'):
            style.configure(f'{orient}.TScrollbar', background=self.SURFACE_ALT,
                             troughcolor=self.SURFACE, bordercolor=self.SURFACE,
                             arrowcolor=self.MUTED, relief='flat')
            style.map(f'{orient}.TScrollbar', background=[('active', self.SELECT_BG)])

    # -- layout --------------------------------------------------------------
    def _make_scrollable_tab(self, notebook, title_key):
        """A notebook tab whose content scrolls vertically when the window is
        too short to show everything - without this, the lower controls on the
        denser tabs (Game/Network) are simply unreachable on small screens."""
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=T(title_key))
        self._tr(title_key, lambda t, nb=notebook, o=outer: nb.tab(o, text=t))

        # A raw tk.Canvas, not ttk - ttk.Style has no reach here, so its
        # background has to be set directly or it shows through as the stock
        # Tk grey/white behind every scrollable tab.
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0, background=self.SURFACE)
        vsb = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor='nw')

        def _on_inner_configure(_evt=None):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def _on_canvas_configure(evt):
            # keep the inner frame at least as wide as the canvas so content
            # doesn't bunch up on the left when there's spare width
            canvas.itemconfigure(window_id, width=max(evt.width, inner.winfo_reqwidth()))

        inner.bind('<Configure>', _on_inner_configure)
        canvas.bind('<Configure>', _on_canvas_configure)

        def _on_mousewheel(evt):
            # only scroll when there's actually something hidden
            bbox = canvas.bbox('all')
            if bbox and bbox[3] > canvas.winfo_height():
                canvas.yview_scroll(int(-evt.delta / 120), 'units')

        # bind wheel only while the pointer is over this canvas, so the wheel
        # doesn't hijack scrolling in other widgets (e.g. the log text box)
        canvas.bind('<Enter>', lambda e: canvas.bind_all('<MouseWheel>', _on_mousewheel))
        canvas.bind('<Leave>', lambda e: canvas.unbind_all('<MouseWheel>'))

        return inner

    def _build_ui(self):
        # Language switcher - always visible above the tabs, not buried in
        # Settings, since it affects every tab at once.
        langbar = ttk.Frame(self)
        langbar.pack(fill='x', padx=8, pady=(6, 0))
        ttk.Label(langbar, text='').pack(side='left', expand=True)  # pushes the switch right
        self.lang_var = tk.StringVar(value=_LANG)
        ttk.Radiobutton(langbar, text='Русский', variable=self.lang_var, value='ru',
                         command=lambda: self._set_language('ru')).pack(side='left')
        ttk.Radiobutton(langbar, text='English', variable=self.lang_var, value='en',
                         command=lambda: self._set_language('en')).pack(side='left', padx=(6, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        self.notebook = nb

        # Server and Log tabs manage their own fill/expand layout (tables and
        # a log box that should stretch with the window), so they stay plain
        # frames; the form-style tabs get scroll containers.
        self.tab_server = ttk.Frame(nb)
        nb.add(self.tab_server, text=T('tab.server'))
        self._tr('tab.server', lambda t: nb.tab(self.tab_server, text=t))
        self.tab_settings = self._make_scrollable_tab(nb, 'tab.settings')
        self.tab_game = self._make_scrollable_tab(nb, 'tab.game')
        self.tab_network = self._make_scrollable_tab(nb, 'tab.network')
        self.tab_activation = self._make_scrollable_tab(nb, 'tab.activation')
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_log, text=T('tab.log'))
        self._tr('tab.log', lambda t: nb.tab(self.tab_log, text=t))

        self._build_server_tab()
        self._build_settings_tab()
        self._build_game_tab()
        self._build_network_tab()
        self._build_activation_tab()
        self._build_log_tab()

    # ------------------------------------------------------------------
    # Server tab
    # ------------------------------------------------------------------
    def _build_server_tab(self):
        f = self.tab_server

        top = ttk.Frame(f)
        top.pack(fill='x', padx=10, pady=10)

        lbl = ttk.Label(top, style='Header.TLabel')
        lbl.grid(row=0, column=0, sticky='w', columnspan=4)
        self._tr('server.mode_header', lambda t: lbl.configure(text=t))

        self.mode_var = tk.StringVar(value='lobby')
        rb1 = ttk.Radiobutton(top, variable=self.mode_var, value='lobby', command=self._on_mode_change)
        rb1.grid(row=1, column=0, sticky='w', pady=(4, 0))
        self._tr('server.mode_lobby', lambda t: rb1.configure(text=t))
        rb2 = ttk.Radiobutton(top, variable=self.mode_var, value='solo', command=self._on_mode_change)
        rb2.grid(row=1, column=1, sticky='w', padx=(20, 0), pady=(4, 0))
        self._tr('server.mode_solo', lambda t: rb2.configure(text=t))

        port_lbl = ttk.Label(top)
        port_lbl.grid(row=2, column=0, sticky='w', pady=(8, 0))
        self._tr('server.port', lambda t: port_lbl.configure(text=t))
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.port_entry = ttk.Entry(top, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=2, column=1, sticky='w', pady=(8, 0))

        self.start_btn = ttk.Button(top, style='Accent.TButton', command=self._start_server)
        self.start_btn.grid(row=2, column=2, padx=(20, 4), pady=(8, 0))
        self._tr('server.start', lambda t: self.start_btn.configure(text=t))
        self.stop_btn = ttk.Button(top, style='Stop.TButton', command=self._stop_server, state='disabled')
        self.stop_btn.grid(row=2, column=3, pady=(8, 0))
        self._tr('server.stop', lambda t: self.stop_btn.configure(text=t))

        self.status_label = ttk.Label(top, style='Status.TLabel', foreground=self.BAD_COLOR)
        self.status_label.grid(row=3, column=0, columnspan=4, sticky='w', pady=(10, 0))

        ttk.Separator(f).pack(fill='x', padx=10, pady=(4, 10))

        mid = ttk.Frame(f)
        mid.pack(fill='both', expand=True, padx=10)

        # Players
        pf = ttk.LabelFrame(mid)
        pf.pack(fill='both', expand=True, side='top', pady=(0, 8))
        self._tr('server.players_online', lambda t: pf.configure(text=t))
        cols = ('name', 'town', 'game', 'pos', 'login')
        self.players_tree = ttk.Treeview(pf, columns=cols, show='headings', height=8)
        header_keys = {'name': 'server.col_name', 'town': 'server.col_town', 'game': 'server.col_game',
                       'pos': 'server.col_pos', 'login': 'server.col_login'}
        widths = {'name': 130, 'town': 220, 'game': 150, 'pos': 100, 'login': 150}
        for c in cols:
            self._tr(header_keys[c], (lambda t, tree=self.players_tree, col=c: tree.heading(col, text=t)))
            self.players_tree.column(c, width=widths[c], anchor='w')
        self.players_tree.pack(fill='both', expand=True, side='left', padx=(6, 0), pady=6)
        psb = ttk.Scrollbar(pf, orient='vertical', command=self.players_tree.yview)
        self.players_tree.configure(yscrollcommand=psb.set)
        psb.pack(side='right', fill='y')

        # tk.Menu, like tk.Canvas above, is raw Tk - coloured by hand to match
        # rather than popping up as a stock white right-click menu.
        self.players_menu = tk.Menu(self, tearoff=0, background=self.SURFACE_ALT,
                                     foreground=self.TEXT, activebackground=self.SELECT_BG,
                                     activeforeground=self.TEXT, borderwidth=0)
        self.players_menu.add_command(command=self._kick_selected_player)
        self._tr('server.kick', (lambda t: self.players_menu.entryconfigure(0, label=t)))
        self.players_menu.add_command(command=self._delete_selected_player)
        self._tr('server.delete_char', (lambda t: self.players_menu.entryconfigure(1, label=t)))
        self.players_tree.bind('<Button-3>', self._on_player_right_click)

        # Manual deletion by name, for accounts that aren't currently online
        # (the right-click menu above only reaches players in the live list).
        delf = ttk.Frame(pf)
        delf.pack(fill='x', side='bottom', padx=6, pady=(0, 6))
        deln_lbl = ttk.Label(delf)
        deln_lbl.pack(side='left')
        self._tr('server.delete_by_name', lambda t: deln_lbl.configure(text=t))
        self.delete_name_var = tk.StringVar()
        ttk.Entry(delf, textvariable=self.delete_name_var, width=20).pack(side='left', padx=6)
        delbtn = ttk.Button(delf, style='Stop.TButton', command=self._delete_player_by_name)
        delbtn.pack(side='left')
        self._tr('server.delete', lambda t: delbtn.configure(text=t))

        self.summary_label = ttk.Label(f, text='')
        self.summary_label.pack(fill='x', padx=10, pady=(4, 8))

    def _on_mode_change(self):
        if self.lobby.is_running() or self.solo.is_running():
            messagebox.showinfo(T('server.mode_change_title'), T('server.mode_change_body'))
            self.mode_var.set('lobby' if self.lobby.is_running() else 'solo')

    def _active_controller(self):
        return self.lobby if self.mode_var.get() == 'lobby' else self.solo

    def _sync_server_status_display(self):
        """Re-renders the start/stop buttons + status label from the active
        controller's actual running state. Called after start/stop, and
        after a language switch (so an already-running server's status shows
        the right language immediately instead of only on the next click)."""
        ctrl = self._active_controller()
        running = ctrl.is_running()
        self.start_btn.configure(state='disabled' if running else 'normal')
        self.stop_btn.configure(state='normal' if running else 'disabled')
        self.port_entry.configure(state='disabled' if running else 'normal')
        if running:
            port = self.port_var.get().strip() or str(DEFAULT_PORT)
            self.status_label.configure(text=T('server.status_running', port=port), foreground=self.OK_COLOR)
        else:
            self.status_label.configure(text=T('server.status_stopped'), foreground=self.BAD_COLOR)

    def _start_server(self):
        port_str = self.port_var.get().strip()
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            messagebox.showerror(T('server.bad_port_title'), T('server.bad_port_body'))
            return
        port = int(port_str)
        ctrl = self._active_controller()
        other = self.solo if ctrl is self.lobby else self.lobby
        if other.is_running():
            messagebox.showwarning(T('server.already_running_title'),
                                    T('server.already_running_body', label=other.label))
            return
        try:
            ctrl.start(port)
        except OSError as e:
            messagebox.showerror(T('server.start_failed_title'),
                                  T('server.start_failed_port_busy', port=port, err=e))
            return
        except Exception as e:
            messagebox.showerror(T('server.start_failed_title'), str(e))
            return
        self._sync_server_status_display()
        print(f'[GUI] {ctrl.label}-сервер запускается на порту {port}...')

    def _stop_server(self):
        ctrl = self._active_controller()
        # disable both buttons for the brief (~1s) blocking stop() call so a
        # double-click can't race a restart against the still-shutting-down
        # server (see the comment on ServerController.stop()).
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='disabled')
        self.status_label.configure(text=T('server.status_stopping'), foreground=self.BAD_COLOR)
        self.update_idletasks()
        print(f'[GUI] Остановка {ctrl.label}-сервера...')
        ctrl.stop()
        self._sync_server_status_display()

    def _poll_stats(self):
        try:
            self._refresh_players_games()
        finally:
            self.after(1500, self._poll_stats)

    @staticmethod
    def _sync_tree(tree, rows):
        """Reconcile a Treeview against {iid: values} instead of clearing and
        re-inserting everything.

        The old code deleted every row on each 1.5s poll. That dropped the
        selection and scroll position between polls, which made the
        right-click-kick flow unreliable (the menu could act on a row that had
        already been recreated) and made the list flicker with many players
        online. Keying rows by player/game name lets untouched rows stay
        exactly as they are.
        """
        existing = set(tree.get_children())
        wanted = set(rows)
        for iid in existing - wanted:
            tree.delete(iid)
        for iid, values in rows.items():
            if iid in existing:
                if tuple(tree.item(iid, 'values')) != tuple(values):
                    tree.item(iid, values=values)
            else:
                tree.insert('', 'end', iid=iid, values=values)

    def _refresh_players_games(self):
        if not self.lobby.is_running():
            self._sync_tree(self.players_tree, {})
            self.summary_label.configure(text=T('server.summary_lobby_only'))
            return

        stats = self.lobby.stats()
        if stats is None:
            return  # transient race while the server mutates state; keep the
                    # last good view rather than blanking the tables
        players = stats['players']

        player_rows = {}
        for name, info in players.items():
            player_rows[name] = (name, info.get('town', ''), info.get('game', ''),
                                 info.get('pos', ''), info.get('loginTime', ''))

        self._sync_tree(self.players_tree, player_rows)
        self.summary_label.configure(text=T('server.summary', n=len(players)))

    def _on_player_right_click(self, event):
        row = self.players_tree.identify_row(event.y)
        if not row:
            return
        self.players_tree.selection_set(row)
        self.players_menu.tk_popup(event.x_root, event.y_root)

    def _kick_selected_player(self):
        sel = self.players_tree.selection()
        if not sel:
            return
        name = self.players_tree.item(sel[0], 'values')[0]
        if not self.lobby.is_running():
            return
        if not messagebox.askyesno(T('server.kick_confirm_title'), T('server.kick_confirm_body', name=name)):
            return
        # Re-check after the modal: the server can be stopped (or crash) while
        # the confirmation dialog is open, leaving self.lobby.server as None.
        server = self.lobby.server if self.lobby.is_running() else None
        if server is None:
            messagebox.showinfo(T('server.stopped_title'), T('server.stopped_body'))
            return
        try:
            ok = server.kickPlayer(name, reason='Kicked by admin')
        except Exception as e:
            print(f'[Admin] Не удалось кикнуть "{name}": {e}')
            return
        print(f'[Admin] Игрок "{name}" кикнут.' if ok
              else f'[Admin] Игрок "{name}" уже не в сети.')
        self._refresh_players_games()

    def _delete_account(self, name):
        """Shared confirm+delete flow for the right-click menu (online
        players) and the by-name entry below the list (works for offline
        accounts too, since it hits the DB directly rather than the live
        player list)."""
        name = name.strip()
        if not name:
            return
        if not messagebox.askyesno(T('server.delete_confirm_title'), T('server.delete_confirm_body', name=name)):
            return
        try:
            self.lobby.ensure_loaded()
        except Exception as e:
            messagebox.showerror(T('server.module_load_error_title'), T('server.module_load_error_body', err=e))
            return
        try:
            # Prefer the running server's method - it kicks first, so a
            # currently-connected player doesn't keep playing on an account
            # that just vanished from the DB. Offline accounts (server
            # stopped, or player just not connected) go straight to the data
            # handler, which owns the DB independently of whether the server
            # is serving.
            if self.lobby.is_running() and self.lobby.server is not None:
                ok = self.lobby.server.deleteAccount(name)
            else:
                ok = self.lobby.module.GDH.deleteAccount(name)
        except Exception as e:
            messagebox.showerror(T('server.delete_error_title'), T('server.delete_error_body', name=name, err=e))
            return
        if ok:
            print(f'[Admin] Персонаж "{name}" удалён.')
            messagebox.showinfo(T('server.delete_done_title'), T('server.delete_done_body', name=name))
        else:
            messagebox.showinfo(T('server.delete_notfound_title'), T('server.delete_notfound_body', name=name))
        self._refresh_players_games()

    def _delete_selected_player(self):
        sel = self.players_tree.selection()
        if not sel:
            return
        name = self.players_tree.item(sel[0], 'values')[0]
        self._delete_account(name)

    def _delete_player_by_name(self):
        self._delete_account(self.delete_name_var.get())
        self.delete_name_var.set('')

    # ------------------------------------------------------------------
    # Settings tab
    # ------------------------------------------------------------------
    def _build_settings_tab(self):
        f = self.tab_settings
        pad = {'padx': 10, 'pady': 6}

        lbl = ttk.Label(f, style='Header.TLabel')
        lbl.grid(row=0, column=0, columnspan=2, sticky='w', **pad)
        self._tr('settings.header', lambda t: lbl.configure(text=t))

        name_lbl = ttk.Label(f)
        name_lbl.grid(row=1, column=0, sticky='w', **pad)
        self._tr('settings.server_name', lambda t: name_lbl.configure(text=t))
        self.set_name = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_name, width=50).grid(row=1, column=1, sticky='w', **pad)

        motd_lbl = ttk.Label(f)
        motd_lbl.grid(row=2, column=0, sticky='nw', **pad)
        self._tr('settings.motd', lambda t: motd_lbl.configure(text=t))
        # Another raw Tk widget under the ScrolledText wrapper - same
        # treatment as the canvas/menu above.
        self.set_motd = scrolledtext.ScrolledText(
            f, width=55, height=4, wrap='word', background=self.FIELD, foreground=self.TEXT,
            insertbackground=self.TEXT, relief='flat', borderwidth=1,
            highlightthickness=1, highlightbackground=self.BORDER, highlightcolor=self.ACCENT)
        self.set_motd.grid(row=2, column=1, sticky='w', **pad)
        ttk.Label(f, text='Поддерживает цвет игры: <0xAARRGGBB>, шрифт <F2>, паузу <break=сек>',
                  foreground=self.MUTED).grid(row=3, column=1, sticky='w', padx=10)

        self.set_autoreg = tk.BooleanVar()
        cb1 = ttk.Checkbutton(f, variable=self.set_autoreg)
        cb1.grid(row=4, column=1, sticky='w', padx=10, pady=(10, 0))
        self._tr('settings.autoreg', lambda t: cb1.configure(text=t))

        self.set_anylogin = tk.BooleanVar()
        cb2 = ttk.Checkbutton(f, variable=self.set_anylogin)
        cb2.grid(row=5, column=1, sticky='w', padx=10)
        self._tr('settings.anylogin', lambda t: cb2.configure(text=t))

        sync_lbl = ttk.Label(f, style='Header.TLabel')
        sync_lbl.grid(row=6, column=0, columnspan=2, sticky='w', padx=10, pady=(20, 6))
        self._tr('settings.sync_header', lambda t: sync_lbl.configure(text=t))

        poshz_lbl = ttk.Label(f)
        poshz_lbl.grid(row=7, column=0, sticky='w', **pad)
        self._tr('settings.pos_hz', lambda t: poshz_lbl.configure(text=t))
        self.set_pos_hz = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_pos_hz, width=8).grid(row=7, column=1, sticky='w', padx=10, pady=6)

        idle_lbl = ttk.Label(f)
        idle_lbl.grid(row=8, column=0, sticky='w', **pad)
        self._tr('settings.idle_timeout', lambda t: idle_lbl.configure(text=t))
        self.set_idle_timeout = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_idle_timeout, width=8).grid(row=8, column=1, sticky='w', padx=10, pady=6)

        self.set_keepalive = tk.BooleanVar()
        cb3 = ttk.Checkbutton(f, variable=self.set_keepalive)
        cb3.grid(row=9, column=1, sticky='w', padx=10)
        self._tr('settings.keepalive', lambda t: cb3.configure(text=t))

        btns = ttk.Frame(f)
        btns.grid(row=10, column=1, sticky='w', padx=10, pady=16)
        load_btn = ttk.Button(btns, command=self._load_settings)
        load_btn.pack(side='left')
        self._tr('settings.load_current', lambda t: load_btn.configure(text=t))
        save_btn = ttk.Button(btns, style='Accent.TButton', command=self._save_settings)
        save_btn.pack(side='left', padx=8)
        self._tr('settings.save', lambda t: save_btn.configure(text=t))

        note = ('Название сервера, MOTD, автоматическая регистрация и параметры синхронизации '
                'применяются сразу, без перезапуска сервера (подхватываются при следующем входе игрока).\n'
                'Изменение порта требует перезапуска сервера на вкладке "Сервер".')
        ttk.Label(f, text=note, foreground=self.MUTED, wraplength=560, justify='left').grid(
            row=11, column=1, sticky='w', padx=10)

        self._load_settings()

    def _load_settings(self):
        try:
            self.lobby.ensure_loaded()
        except Exception as e:
            messagebox.showerror(T('server.module_load_error_title'), T('server.module_load_error_body', err=e))
            return
        mod = self.lobby.module
        cfg = mod.loadConfig()
        sec = cfg['server']
        self.set_name.set(sec.get('ServerName', fallback=mod.DEFAULT_TITLE))
        self.set_motd.delete('1.0', 'end')
        self.set_motd.insert('1.0', mod._unescapeMOTD(sec.get('MOTD', fallback=mod._escapeMOTD(mod.DEFAULT_MOTD))))
        self.set_autoreg.set(sec.getboolean('AutoRegister', fallback=True))
        self.set_anylogin.set(sec.getboolean('AllowAnyLogin', fallback=False))
        self.port_var.set(sec.get('Port', fallback=str(DEFAULT_PORT)))
        self.set_pos_hz.set(sec.get('PositionUpdateHz', fallback=str(mod._POS_UPDATE_HZ)))
        self.set_idle_timeout.set(sec.get('IdleTimeout', fallback=str(mod._IDLE_TIMEOUT)))
        self.set_keepalive.set(sec.getboolean('Keepalive', fallback=mod._SEND_NOPS))

    def _save_settings(self):
        try:
            self.lobby.ensure_loaded()
        except Exception as e:
            messagebox.showerror(T('server.module_load_error_title'), T('server.module_load_error_body', err=e))
            return
        mod = self.lobby.module
        # validated before anything touches Config.ini: applyConfig() reads
        # these back with getfloat/getint, which raises on garbage - better to
        # catch a typo here than crash straight after writing the file
        try:
            pos_hz = float(self.set_pos_hz.get().strip() or mod._POS_UPDATE_HZ)
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('settings.bad_pos_hz'))
            return
        try:
            idle_timeout = int(self.set_idle_timeout.get().strip() or mod._IDLE_TIMEOUT)
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('settings.bad_idle_timeout'))
            return

        cfg = mod.loadConfig()
        sec = cfg['server']
        sec['ServerName'] = self.set_name.get().strip() or mod.DEFAULT_TITLE
        motd_raw = self.set_motd.get('1.0', 'end-1c')
        sec['MOTD'] = mod._escapeMOTD(motd_raw)
        sec['Port'] = self.port_var.get().strip() or str(DEFAULT_PORT)
        sec['AutoRegister'] = str(self.set_autoreg.get())
        sec['AllowAnyLogin'] = str(self.set_anylogin.get())
        sec['PositionUpdateHz'] = str(pos_hz)
        sec['IdleTimeout'] = str(idle_timeout)
        sec['Keepalive'] = str(self.set_keepalive.get())
        mod.saveConfig(cfg)
        mod.applyConfig(cfg)
        # the Network tab caches the port in a label; without this it keeps
        # showing the old value after the port is changed here
        if hasattr(self, 'net_port_label'):
            self.net_port_label.configure(text=self.port_var.get().strip() or str(DEFAULT_PORT))
        print('[Настройки] Config.ini сохранён и применён.')
        messagebox.showinfo(T('settings.saved_title'), T('settings.saved_body'))

    # ------------------------------------------------------------------
    # Game tab - resolution / window mode / borderless / experimental FOV
    # ------------------------------------------------------------------
    def _load_game_settings_file(self):
        cfg = configparser.ConfigParser()
        cfg['game'] = {'ExePath': '', 'SingleCoreAffinity': 'False'}
        if os.path.exists(GAME_SETTINGS_PATH):
            cfg.read(GAME_SETTINGS_PATH)
        return cfg

    def _save_game_settings_file(self, cfg):
        with open(GAME_SETTINGS_PATH, 'w', encoding='utf-8') as f:
            cfg.write(f)

    def _resolve_game_exe(self, path):
        """Picks the build to launch from the install folder the user pointed
        at - the folder is what matters, not which exe inside it was selected,
        so an old stored path keeps working. Walks GAME_EXE_CANDIDATES in
        order and takes the first that exists."""
        if not path:
            return None
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        for name in GAME_EXE_CANDIDATES:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
        # Nothing recognised in that folder: hand back the preferred name so
        # the caller's existence check reports it by the name users expect.
        return os.path.join(folder, GAME_EXE_NAME)

    def _build_game_tab(self):
        f = self.tab_game
        pad = {'padx': 10, 'pady': 6}

        if winreg is None:
            lbl = ttk.Label(f, foreground=self.BAD_COLOR)
            lbl.pack(anchor='w', padx=10, pady=10)
            self._tr('game.windows_only', lambda t: lbl.configure(text=t))
            return

        path_lbl = ttk.Label(f, style='Header.TLabel')
        path_lbl.grid(row=0, column=0, columnspan=3, sticky='w', **pad)
        self._tr('game.path_header', lambda t: path_lbl.configure(text=t))
        ttk.Label(f, text=f'Укажи папку игры. Запускается {GAME_EXE_NAME}, '
                          f'а если его нет - следующий из: '
                          f'{", ".join(GAME_EXE_CANDIDATES[1:])}.',
                  foreground=self.MUTED).grid(row=1, column=0, columnspan=3, sticky='w', padx=10)
        self.game_exe_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.game_exe_var, width=60).grid(row=2, column=0, columnspan=2, sticky='w', **pad)
        browse_btn = ttk.Button(f, command=self._browse_game_exe)
        browse_btn.grid(row=2, column=2, sticky='w', **pad)
        self._tr('game.browse', lambda t: browse_btn.configure(text=t))

        launch_btn = ttk.Button(f, style='Accent.TButton', command=self._launch_game)
        launch_btn.grid(row=3, column=0, sticky='w', padx=10, pady=14)
        self._tr('game.launch', lambda t: launch_btn.configure(text=t))
        # The engine ships its own 'cpu_UseOneProcessor' console command - Two
        # Worlds is a 2007 title that timed its simulation off RDTSC/QPC, and
        # co-op runs a lockstep network model (net.SetTurnLength and friends,
        # confirmed from the same command dump this checkbox's tooltip refers
        # to): if that per-core clock drifts, the two players' simulations can
        # each compute a different result from the same input and diverge -
        # what shows up as desync. The engine command's own invocation is not
        # documented anywhere available, but pinning the whole process to one
        # core from outside has the identical effect and needs nothing from
        # the game side.
        self.game_single_core_var = tk.BooleanVar()
        singlecore_chk = ttk.Checkbutton(f, variable=self.game_single_core_var,
                                          command=self._save_single_core_setting)
        singlecore_chk.grid(row=3, column=1, sticky='w', padx=(0, 10), pady=14)
        self._tr('game.single_core', lambda t: singlecore_chk.configure(text=t))

        ttk.Separator(f, orient='horizontal').grid(row=4, column=0, columnspan=3, sticky='ew', pady=10)

        fov_lbl = ttk.Label(f, style='Header.TLabel')
        fov_lbl.grid(row=5, column=0, columnspan=3, sticky='w', **pad)
        self._tr('game.fov_header', lambda t: fov_lbl.configure(text=t))
        ttk.Label(f, text='На широких мониторах камера в TW1 может казаться слишком "приближенной".\n'
                           'ForceCameraAspectX/Y - реальные поля в настройках игры (не выдуманные), но их точный\n'
                           'эффект не подтверждён - пробуйте на свой риск, здесь же можно мгновенно вернуть 0/0.',
                  foreground=self.MUTED, justify='left').grid(row=6, column=0, columnspan=3, sticky='w', padx=10)
        self.fov_x_var = tk.StringVar(value='0')
        self.fov_y_var = tk.StringVar(value='0')
        ttk.Label(f, text='AspectX:').grid(row=7, column=0, sticky='e', padx=(10, 2))
        ttk.Entry(f, textvariable=self.fov_x_var, width=8).grid(row=7, column=0, sticky='w', padx=(70, 0))
        ttk.Label(f, text='AspectY:').grid(row=7, column=1, sticky='w', padx=(0, 2))
        ttk.Entry(f, textvariable=self.fov_y_var, width=8).grid(row=7, column=1, sticky='w', padx=(70, 0))
        fbtns = ttk.Frame(f)
        fbtns.grid(row=8, column=0, columnspan=3, sticky='w', padx=10, pady=10)
        apply_btn = ttk.Button(fbtns, command=self._apply_fov)
        apply_btn.pack(side='left')
        self._tr('game.fov_apply', lambda t: apply_btn.configure(text=t))
        reset_btn = ttk.Button(fbtns, command=self._reset_fov)
        reset_btn.pack(side='left', padx=8)
        self._tr('game.fov_reset', lambda t: reset_btn.configure(text=t))

        self._load_game_tab_state()

    def _load_game_tab_state(self):
        cfg = self._load_game_settings_file()
        self.game_exe_var.set(cfg['game'].get('ExePath', ''))
        self.game_single_core_var.set(cfg['game'].getboolean('SingleCoreAffinity', fallback=False))
        cur = read_graphics_settings()
        if cur:
            self.fov_x_var.set(str(cur['ForceCameraAspectX']))
            self.fov_y_var.set(str(cur['ForceCameraAspectY']))

    def _browse_game_exe(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title=T('game.browse_title', exe=GAME_EXE_NAME),
                                           filetypes=[(GAME_EXE_NAME, GAME_EXE_NAME),
                                                      (T('game.filetype_exe'), '*.exe'),
                                                      (T('game.filetype_all'), '*.*')])
        if path:
            self.game_exe_var.set(path)
            cfg = self._load_game_settings_file()
            cfg['game']['ExePath'] = path
            self._save_game_settings_file(cfg)

    def _apply_fov(self):
        try:
            x = int(self.fov_x_var.get())
            y = int(self.fov_y_var.get())
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('game.bad_fov'))
            return
        try:
            write_graphics_settings({'ForceCameraAspectX': x, 'ForceCameraAspectY': y})
            print(f'[Игра] ForceCameraAspectX/Y = {x}/{y}')
        except Exception as e:
            messagebox.showerror(T('game.registry_error_title'), str(e))

    def _reset_fov(self):
        self.fov_x_var.set('0')
        self.fov_y_var.set('0')
        self._apply_fov()

    def _save_single_core_setting(self):
        cfg = self._load_game_settings_file()
        cfg['game']['SingleCoreAffinity'] = str(self.game_single_core_var.get())
        self._save_game_settings_file(cfg)

    def _pin_to_one_core(self, pid, core=0):
        """Restricts the process to a single CPU core via the Win32 affinity
        mask - the same effect the engine's own (undocumented-to-invoke)
        cpu_UseOneProcessor console command would have, applied from outside
        instead. Best-effort: prints why on failure, never raises - a failed
        affinity pin should not stop the game from launching."""
        PROCESS_SET_INFORMATION = 0x0200
        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION, False, pid)
        if not handle:
            print(f'[Игра] Не удалось открыть процесс (PID {pid}) для установки сходства ЦП: '
                  f'{ctypes.WinError()}')
            return False
        try:
            mask = 1 << core
            if ctypes.windll.kernel32.SetProcessAffinityMask(handle, mask):
                print(f'[Игра] Сходство процессора ограничено ядром {core} (PID {pid})')
                return True
            print(f'[Игра] SetProcessAffinityMask не сработал: {ctypes.WinError()}')
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def _launch_game(self):
        exe = self._resolve_game_exe(self.game_exe_var.get().strip())
        if not exe or not os.path.exists(exe):
            messagebox.showerror(T('game.not_found_title'), T('game.not_found_body', exe=GAME_EXE_NAME))
            return
        try:
            proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))
            print(f'[Игра] Запущена: {exe}')
            if self.game_single_core_var.get():
                self._pin_to_one_core(proc.pid)
        except Exception as e:
            messagebox.showerror(T('game.launch_failed_title'), str(e))

    # ------------------------------------------------------------------
    # Network tab - local/public IP, manual port check link, best-effort
    # automatic UPnP port forward
    # ------------------------------------------------------------------
    _PRESET_KEYS = ['lan', 'public', 'localhost', '127001', 'custom']

    def _build_network_tab(self):
        f = self.tab_network
        pad = {'padx': 10, 'pady': 6}

        hdr = ttk.Label(f, style='Header.TLabel')
        hdr.grid(row=0, column=0, columnspan=2, sticky='w', **pad)
        self._tr('network.reach_header', lambda t: hdr.configure(text=t))
        ttk.Label(f, text='Если друзья не в твоей локальной сети, порт лобби-сервера должен быть\n'
                           'проброшен на роутере и открыт в брандмауэре.',
                  foreground=self.MUTED, justify='left').grid(row=1, column=0, columnspan=2, sticky='w', padx=10)

        lip_lbl = ttk.Label(f)
        lip_lbl.grid(row=2, column=0, sticky='w', **pad)
        self._tr('network.local_ip', lambda t: lip_lbl.configure(text=t))
        self.net_local_ip_label = ttk.Label(f, text='...')
        self.net_local_ip_label.grid(row=2, column=1, sticky='w', **pad)

        pip_lbl = ttk.Label(f)
        pip_lbl.grid(row=3, column=0, sticky='w', **pad)
        self._tr('network.public_ip', lambda t: pip_lbl.configure(text=t))
        self.net_public_ip_label = ttk.Label(f)
        self.net_public_ip_label.grid(row=3, column=1, sticky='w', **pad)
        self._tr('network.determining', lambda t: self.net_public_ip_label.configure(text=t))

        port_lbl = ttk.Label(f)
        port_lbl.grid(row=4, column=0, sticky='w', **pad)
        self._tr('network.server_port', lambda t: port_lbl.configure(text=t))
        self.net_port_label = ttk.Label(f, text=str(DEFAULT_PORT))
        self.net_port_label.grid(row=4, column=1, sticky='w', **pad)

        btns = ttk.Frame(f)
        btns.grid(row=5, column=0, columnspan=2, sticky='w', padx=10, pady=14)
        refresh_btn = ttk.Button(btns, command=self._refresh_network_info)
        refresh_btn.pack(side='left')
        self._tr('network.refresh', lambda t: refresh_btn.configure(text=t))
        check_btn = ttk.Button(btns, command=self._open_port_checker)
        check_btn.pack(side='left', padx=8)
        self._tr('network.check_port', lambda t: check_btn.configure(text=t))
        upnp_btn = ttk.Button(btns, style='Accent.TButton', command=self._try_upnp)
        upnp_btn.pack(side='left', padx=8)
        self._tr('network.try_upnp', lambda t: upnp_btn.configure(text=t))

        self.net_status_label = ttk.Label(f, text='', wraplength=820, justify='left')
        self.net_status_label.grid(row=6, column=0, columnspan=2, sticky='w', padx=10, pady=(6, 0))

        ttk.Separator(f, orient='horizontal').grid(row=7, column=0, columnspan=2, sticky='ew', pady=10)

        srvhdr = ttk.Label(f, style='Header.TLabel')
        srvhdr.grid(row=8, column=0, columnspan=2, sticky='w', **pad)
        self._tr('network.server_list_header', lambda t: srvhdr.configure(text=t))
        ttk.Label(f, text='ВАЖНО: названия строк ("WarNet Europe" и т.п.) зашиты в саму игру - её меню всегда\n'
                           'покажет ровно эти же пункты, что бы тут ни было. Из реестра берётся только АДРЕС\n'
                           'для каждого из них. Поэтому переименовывать/добавлять новые пункты бессмысленно -\n'
                           'редактируется только колонка "Адрес" (двойной клик по ячейке).',
                  foreground=self.MUTED, justify='left').grid(row=9, column=0, columnspan=2, sticky='w', padx=10)

        self.servers_tree = ttk.Treeview(f, columns=('name', 'addr'), show='headings', height=6)
        self._tr('network.col_name', lambda t: self.servers_tree.heading('name', text=t))
        self._tr('network.col_addr', lambda t: self.servers_tree.heading('addr', text=t))
        self.servers_tree.column('name', width=200, anchor='w')
        self.servers_tree.column('addr', width=280, anchor='w')
        self.servers_tree.grid(row=10, column=0, columnspan=2, sticky='w', padx=10, pady=6)
        self.servers_tree.bind('<Double-1>', self._edit_server_address)

        fillrow = ttk.Frame(f)
        fillrow.grid(row=11, column=0, columnspan=2, sticky='w', padx=10, pady=(8, 4))
        fill_lbl = ttk.Label(fillrow)
        fill_lbl.pack(side='left')
        self._tr('network.point_all_to', lambda t: fill_lbl.configure(text=t))
        self.addr_preset_var = tk.StringVar()
        self.addr_preset_combo = ttk.Combobox(fillrow, textvariable=self.addr_preset_var, width=26, state='readonly')
        self.addr_preset_combo.pack(side='left', padx=(6, 8))
        self.addr_preset_combo.bind('<<ComboboxSelected>>', self._on_preset_changed)
        self.custom_addr_var = tk.StringVar()
        self.custom_addr_entry = ttk.Entry(fillrow, textvariable=self.custom_addr_var, width=22, state='disabled')
        self.custom_addr_entry.pack(side='left', padx=(0, 8))
        apply_addr_btn = ttk.Button(fillrow, command=self._prefill_own_server)
        apply_addr_btn.pack(side='left')
        self._tr('network.apply', lambda t: apply_addr_btn.configure(text=t))
        self._apply_preset_combo_language()  # needs custom_addr_entry to exist - see _on_preset_changed

        srvbtns = ttk.Frame(f)
        srvbtns.grid(row=12, column=0, columnspan=2, sticky='w', padx=10, pady=(4, 4))
        restore_btn = ttk.Button(srvbtns, command=self._restore_default_servers)
        restore_btn.pack(side='left')
        self._tr('network.restore_defaults', lambda t: restore_btn.configure(text=t))
        save_srv_btn = ttk.Button(srvbtns, style='Accent.TButton', command=self._save_server_list)
        save_srv_btn.pack(side='left', padx=8)
        self._tr('network.save_to_game', lambda t: save_srv_btn.configure(text=t))

        ttk.Label(f, text='"localhost"/"127.0.0.1" - для игры вдвоём с одного компьютера (второй клиент игры\n'
                           'на этой же машине). Для игры по локальной сети используй "Локальный IP".',
                  foreground=self.MUTED, justify='left').grid(row=13, column=0, columnspan=2, sticky='w', padx=10)

        self._load_server_list()
        self._refresh_network_info()

    def _apply_preset_combo_language(self):
        """Combobox display text is language-dependent, but _on_preset_changed
        / _prefill_own_server need a STABLE value to branch on - comparing
        against literal Russian text would silently break in English mode.
        _PRESET_KEYS is that stable, language-independent ordering; this just
        re-renders the visible labels and keeps the same key selected."""
        prev_key = self._current_preset_key() if hasattr(self, 'addr_preset_combo') else 'lan'
        labels = [T(f'network.preset_{k}') for k in self._PRESET_KEYS]
        self.addr_preset_combo.configure(values=labels)
        self.addr_preset_combo.current(self._PRESET_KEYS.index(prev_key))
        self._on_preset_changed()

    def _current_preset_key(self):
        idx = self.addr_preset_combo.current()
        return self._PRESET_KEYS[idx] if 0 <= idx < len(self._PRESET_KEYS) else 'lan'

    _DEFAULT_SERVER_LIST = [
        ('WarNet Europe', 'warnet.2-worlds.com'),
        ('WarNet Europe 2', 'netserver.2-worlds.com'),
        ('WarNet America', 'hawk.2-worlds-us.com'),
    ]

    def _load_server_list(self):
        for row in self.servers_tree.get_children():
            self.servers_tree.delete(row)
        current = read_server_list()
        if not current:
            # nothing in the registry (or a slot got deleted by mistake) -
            # fall back to the game's own known slot names so there's always
            # something sensible to edit, never an empty/broken list
            current = self._DEFAULT_SERVER_LIST
        for name, addr in current:
            self.servers_tree.insert('', 'end', values=(name, addr))

    def _edit_server_address(self, event):
        row = self.servers_tree.identify_row(event.y)
        col = self.servers_tree.identify_column(event.x)
        if not row or col != '#2':  # only the Address column is editable
            return
        x, y, w, h = self.servers_tree.bbox(row, col)
        name, old_addr = self.servers_tree.item(row, 'values')
        var = tk.StringVar(value=old_addr)
        entry = ttk.Entry(self.servers_tree, textvariable=var)
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus()
        entry.select_range(0, 'end')

        def commit(_evt=None):
            self.servers_tree.set(row, 'addr', var.get().strip())
            entry.destroy()

        entry.bind('<Return>', commit)
        entry.bind('<FocusOut>', commit)
        entry.bind('<Escape>', lambda e: entry.destroy())

    def _on_preset_changed(self, event=None):
        is_custom = self._current_preset_key() == 'custom'
        self.custom_addr_entry.configure(state='normal' if is_custom else 'disabled')
        if is_custom:
            self.custom_addr_entry.focus()

    def _prefill_own_server(self):
        preset = self._current_preset_key()
        if preset == 'custom':
            addr = self.custom_addr_var.get().strip()
            if not addr:
                messagebox.showerror(T('server.module_load_error_title'), T('network.enter_addr'))
                return
        elif preset == 'public':
            self.addr_preset_combo.configure(state='disabled')
            self.update_idletasks()
            addr = get_public_ip()
            self.addr_preset_combo.configure(state='readonly')
            if not addr:
                messagebox.showerror(T('server.module_load_error_title'), T('network.public_ip_failed'))
                return
        elif preset == 'localhost':
            addr = 'localhost'
        elif preset == '127001':
            addr = '127.0.0.1'
        else:
            addr = get_local_ip()

        for row in self.servers_tree.get_children():
            self.servers_tree.set(row, 'addr', addr)
        port = self.port_var.get() or str(DEFAULT_PORT)
        messagebox.showinfo(T('network.addrs_replaced_title'), T('network.addrs_replaced_body', addr=addr, port=port))

    def _restore_default_servers(self):
        if not messagebox.askyesno(T('network.restore_confirm_title'), T('network.restore_confirm_body')):
            return
        for row in self.servers_tree.get_children():
            self.servers_tree.delete(row)
        for name, addr in self._DEFAULT_SERVER_LIST:
            self.servers_tree.insert('', 'end', values=(name, addr))

    def _save_server_list(self):
        pairs = [tuple(self.servers_tree.item(row, 'values')) for row in self.servers_tree.get_children()]
        try:
            write_server_list(pairs)
            port = self.port_var.get() or str(DEFAULT_PORT)
            write_server_port(int(port))
            print(f'[Сеть] Список серверов в игре сохранён ({len(pairs)} записей), порт {port}.')
            messagebox.showinfo(T('settings.saved_title'), T('network.saved_body'))
        except Exception as e:
            messagebox.showerror(T('game.registry_error_title'), str(e))

    def _refresh_network_info(self):
        self.net_port_label.configure(text=self.port_var.get() or str(DEFAULT_PORT))
        try:
            self.net_local_ip_label.configure(text=get_local_ip())
        except Exception:
            self.net_local_ip_label.configure(text=T('network.undetermined'))
        self.net_public_ip_label.configure(text=T('network.determining'))
        threading.Thread(target=self._fetch_public_ip, daemon=True).start()

    def _fetch_public_ip(self):
        ip = get_public_ip()
        self.after(0, lambda: self.net_public_ip_label.configure(text=ip or T('network.public_ip_undetermined')))

    def _open_port_checker(self):
        webbrowser.open('https://canyouseeme.org/')
        port = self.net_port_label.cget('text')
        self.net_status_label.configure(text=T('network.opened_port_checker', port=port))

    def _try_upnp(self):
        try:
            port = int(self.net_port_label.cget('text'))
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('network.bad_port'))
            return
        self.net_status_label.configure(text=T('network.upnp_searching'))
        self.update_idletasks()
        threading.Thread(target=self._upnp_worker, args=(port,), daemon=True).start()

    def _upnp_worker(self, port):
        try:
            local_ip = upnp_add_port_mapping(port)
            msg = T('network.upnp_success', port=port, ip=local_ip)
            print(f'[Сеть] UPnP: порт {port} проброшен на {local_ip}.')
        except UPnPError as e:
            msg = T('network.upnp_failed', err=e, port=port, ip=get_local_ip())
            print(f'[Сеть] UPnP не удался: {e}')
        except Exception as e:
            msg = T('network.upnp_unexpected', err=e)
        self.after(0, lambda: self.net_status_label.configure(text=msg))

    # ------------------------------------------------------------------
    # Activation tab
    # ------------------------------------------------------------------
    def _build_activation_tab(self):
        f = self.tab_activation
        akhdr = ttk.Label(f, style='Header.TLabel')
        akhdr.pack(anchor='w', padx=10, pady=(10, 4))
        self._tr('activation.header', lambda t: akhdr.configure(text=t))
        aktext = ttk.Label(f, wraplength=820, justify='left')
        aktext.pack(anchor='w', padx=10, pady=(0, 12))
        self._tr('activation.body', lambda t: aktext.configure(text=t))
        akbtn = ttk.Button(f, style='Accent.TButton', command=self._launch_activation)
        akbtn.pack(anchor='w', padx=10)
        self._tr('activation.launch', lambda t: akbtn.configure(text=t))

    def _launch_activation(self):
        # Needs its own console + a real file on disk (it self-elevates via
        # ShellExecuteW and blocks on input() for the serial key), so unlike
        # the lobby/solo servers it can't just be exec()'d into memory here.
        # Write the embedded source out once per launch instead of shipping
        # a companion file next to this script.
        try:
            source = base64.b64decode(_ACTIVATION_SOURCE_B64).decode('utf-8')
            activation_path = os.path.join(APP_DATA_DIR, 'TW1 Local Activation Server.py')
            with open(activation_path, 'w', encoding='utf-8') as f:
                f.write(source)
            kwargs = {}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen([_console_python(), activation_path], cwd=APP_DATA_DIR, **kwargs)
            print('[Активация] Запущен в отдельном окне.')
        except Exception as e:
            messagebox.showerror(T('activation.launch_failed_title'), str(e))

    # ------------------------------------------------------------------
    # Log tab
    # ------------------------------------------------------------------
    def _build_log_tab(self):
        f = self.tab_log
        bar = ttk.Frame(f)
        bar.pack(fill='x', padx=10, pady=(10, 4))
        loghdr = ttk.Label(bar, style='Header.TLabel')
        loghdr.pack(side='left')
        self._tr('log.header', lambda t: loghdr.configure(text=t))
        clearbtn = ttk.Button(bar, command=self._clear_log)
        clearbtn.pack(side='right')
        self._tr('log.clear', lambda t: clearbtn.configure(text=t))
        openbtn = ttk.Button(bar, command=self._open_log_file)
        openbtn.pack(side='right', padx=(0, 6))
        self._tr('log.openfile', lambda t: openbtn.configure(text=t))
        copybtn = ttk.Button(bar, command=self._copy_log)
        copybtn.pack(side='right', padx=(0, 6))
        self._tr('log.copy', lambda t: copybtn.configure(text=t))

        self.log_text = scrolledtext.ScrolledText(f, wrap='word', state='disabled',
                                                    font=('Consolas', 9), background=self.BG,
                                                    foreground=self.TEXT, insertbackground=self.TEXT)
        self.log_text.pack(fill='both', expand=True, padx=10, pady=(0, 10))

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    def _copy_log(self):
        #Whole buffer to the clipboard in one click. Selecting it by hand is
        #not practical while the server is running: new lines keep arriving.
        self.clipboard_clear()
        self.clipboard_append(self.log_text.get('1.0', 'end-1c'))
        print(f'[{T("tab.log")}] {T("log.copied")}')

    def _open_log_file(self):
        try:
            sys.stdout.flush()  #the file is written buffered - land the tail first
        except Exception:
            pass
        if not os.path.isfile(self.log_path):
            messagebox.showinfo(T('log.nofile_title'), T('log.nofile_body'))
            return
        try:
            os.startfile(self.log_path)  #noqa - Windows only, same as the rest
        except Exception:
            #No file association (or not Windows): show the path so it can be
            #opened by hand rather than failing silently.
            messagebox.showinfo(T('log.nofile_title'), self.log_path)

    #The log is append-only and the lobby server prints a line per connection,
    #per login and per disconnect. Left uncapped, a server running for days
    #grows the Text widget until the whole app crawls, so keep only a trailing
    #window - the full stream still goes to stdout when run from a console.
    _LOG_MAX_LINES = 5000
    _LOG_TRIM_TO = 4000

    def _poll_log(self):
        #Whether the view was pinned to the bottom *before* this batch is
        #appended. Scrolling to the end unconditionally, five times a second,
        #made the log impossible to read back or select from while the server
        #was busy: any attempt to scroll up was undone on the next tick.
        #Follow the tail only while the user is actually at the tail.
        try:
            at_bottom = self.log_text.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        drained = False
        try:
            while True:
                chunk = self.log_queue.get_nowait()
                if not drained:
                    self.log_text.configure(state='normal')
                    drained = True
                self.log_text.insert('end', chunk)
        except queue.Empty:
            pass
        if drained:
            self._trim_log()
            if at_bottom:
                self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.after(200, self._poll_log)

    def _trim_log(self):
        #'end-1c' is the last character; its line number is the line count.
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > self._LOG_MAX_LINES:
            #delete in one chunk rather than per line, and leave headroom so
            #this doesn't run on every poll
            self.log_text.delete('1.0', f'{lines - self._LOG_TRIM_TO}.0')

    # ------------------------------------------------------------------
    def _on_close(self):
        if self.lobby.is_running():
            self.lobby.stop()
        if self.solo.is_running():
            self.solo.stop()
        self.destroy()


# ---------------------------------------------------------------------------
# Embedded server sources (base64-encoded) - this is what makes this file
# fully portable: no sibling files or folders are required at all. Decoded
# and exec()'d into memory on demand by _load_embedded() / _launch_activation.
# Keep these in sync with the standalone copies under Lobby Server/,
# Local Solo-Multiplayer Server/ and Local Activation Server/ if those are
# ever edited again - this app does not read those files at runtime.
# ---------------------------------------------------------------------------
_LOBBY_SOURCE_B64 = (
    "aW1wb3J0IGNvbmZpZ3BhcnNlcgppbXBvcnQgemxpYgppbXBvcnQgc3RydWN0CmltcG9ydCBvcwpp"
    "bXBvcnQgcmUKaW1wb3J0IHNvY2tldHNlcnZlcgppbXBvcnQgc29ja2V0CmltcG9ydCBzZWxlY3QK"
    "aW1wb3J0IHVybGxpYi5yZXF1ZXN0CmltcG9ydCBpcGFkZHJlc3MKaW1wb3J0IHRocmVhZGluZwpp"
    "bXBvcnQgc2lnbmFsCmltcG9ydCB0aW1lCmltcG9ydCBkYXRldGltZQppbXBvcnQgaGFzaGxpYgpp"
    "bXBvcnQgc3FsaXRlMwppbXBvcnQgcmFuZG9tCmltcG9ydCB0cmFjZWJhY2sKZnJvbSBxdWV1ZSBp"
    "bXBvcnQgU2ltcGxlUXVldWUKCiMjIE1JU0MgVVRJTElUWSBGVU5DVElPTlMKXzMyYml0ID0gMHhG"
    "RkZGRkZGRgpfOGJpdCA9IDB4RkYKX04gPSBiJ1wwJwpfRzY0X0JBU0UgPSBieXRlcyhbCiAgICAw"
    "eEQyLCAweDEyLCAweDEzLCAweEQzLCAweDExLCAweEQxLCAweEQwLCAweDEwLCAweEYwLCAweDMw"
    "LCAKICAgIDB4MzEsIDB4RjEsIDB4MzMsIDB4RjMsIDB4RjIsIDB4MzIsIDB4MzYsIDB4RjYsIDB4"
    "RjcsIDB4MzcsIAogICAgMHhGNSwgMHgzNSwgMHgzNCwgMHhGNCwgMHgzQywgMHhGQywgMHhGRCwg"
    "MHgzRCwgMHhGRiwgMHgzRiwgCiAgICAweDNFLCAweEZFLCAweEZBLCAweDNBLCAweDNCLCAweEZC"
    "LCAweDM5LCAweEY5LCAweEY4LCAweDM4LCAKICAgIDB4MjgsIDB4RTgsIDB4RTksIDB4MjksIDB4"
    "RUIsIDB4MkIsIDB4MkEsIDB4RUEsIDB4RUUsIDB4MkUsIAogICAgMHgyRiwgMHhFRiwgMHgyRCwg"
    "MHhFRCwgMHhFQywgMHgyQywgMHhFNCwgMHgyNCwgMHgyNSwgMHhFNSwgCiAgICAweDI3LCAweEU3"
    "LCAweEU2LCAweDI2XSkKZGVmIF9zdGVwKG51bSk6CiAgICByZXR1cm4gKG51bSoweDM0M0ZEICsg"
    "MHgyNjlFQzMpJl8zMmJpdApkZWYgZ2VuNjQoY29tYmluZWQpOgogICAgb3V0ID0gYnl0ZWFycmF5"
    "KDB4NDApCiAgICBlYnAgPSBlZGkgPSB0bXAgPSAwCiAgICBmb3IgYiBpbiBjb21iaW5lZDoKICAg"
    "ICAgICBlZGkrPSBiK3RtcAogICAgICAgIHRtcF49IGIKICAgICAgICBlYnArPSB0bXAKICAgIGZv"
    "ciBpIGluIHJhbmdlKDB4NDApOgogICAgICAgIHJlcyA9IGNvbWJpbmVkWyhlYnAraSklOF0KICAg"
    "ICAgICBvdXRbaV0gPSByZXNeX0c2NF9CQVNFWyhlZGkraSklMHg0MF0KICAgIHJnID0gZWRpK2Vi"
    "cAogICAgZm9yIGkgaW4gcmFuZ2UoMHg0MCk6CiAgICAgICAgcmcgPSBfc3RlcChyZykKICAgICAg"
    "ICBvdXRbaV1ePSAocmc+PjB4MTApJl84Yml0CiAgICBmb3IgaSBpbiByYW5nZSgweDIwKToKICAg"
    "ICAgICByZyA9IF9zdGVwKHJnKQogICAgICAgIHNBID0gKHJnPj4weDEwKSUweDQwCiAgICAgICAg"
    "cmcgPSBfc3RlcChyZykKICAgICAgICBzQiA9IChyZz4+MHgxMCklMHg0MAogICAgICAgIChvdXRb"
    "c0FdLCBvdXRbc0JdKSA9IChvdXRbc0JdLCBvdXRbc0FdKQogICAgcmV0dXJuIGJ5dGVzKG91dCkK"
    "I1RoZSBsb2JieSBwcm90b2NvbCBpcyBhIDIwMDcgOC1iaXQgcHJvdG9jb2w6IHRoZSBnYW1lIHNl"
    "bmRzIHdoYXRldmVyIGl0cwojbG9jYWxpc2F0aW9uJ3MgY29kZXBhZ2UgcHJvZHVjZXMgYW5kIGV4"
    "cGVjdHMgdGhlIHNhbWUgYnl0ZXMgYmFjay4gVGhpcyBzZXJ2ZXIKI2lzIHJ1biBmb3IgYSBSdXNz"
    "aWFuLXNwZWFraW5nIGdyb3VwIChzZWUgQ0xBVURFLm1kKSwgc28gdGhhdCBjb2RlcGFnZSBpcwoj"
    "Y3AxMjUxIC0gYXNjaWktY29tcGF0aWJsZSBmb3IgMHgwMC0weDdGLCBDeXJpbGxpYyBmb3IgdGhl"
    "IHJlc3QuCiNUaGlzIHVzZWQgdG8gYmUgJ2FzY2lpJyBvbiB0aGUgd2F5IG91dCBhbmQgVVRGLTgg"
    "b24gdGhlIHdheSBpbiwgd2hpY2ggbWVhbnQgYQojc2luZ2xlIEN5cmlsbGljIGNoYXJhY3RlciBp"
    "biBjaGF0IGVpdGhlciBmYWlsZWQgdG8gZGVjb2RlIG9yIGZhaWxlZCB0byBlbmNvZGUuCiNFaXRo"
    "ZXIgd2F5IHRoZSBleGNlcHRpb24gcHJvcGFnYXRlZCBvdXQgb2YgdGhlIGNvbW1hbmQgaGFuZGxl"
    "ciBhbmQgZHJvcHBlZCB0aGUKI2Nvbm5lY3Rpb246IG9uIGEgUnVzc2lhbiBzZXJ2ZXIsIHR5cGlu"
    "ZyBpbiBjaGF0IGRpc2Nvbm5lY3RlZCB5b3UuCiMobGF0aW4tMSB3b3VsZCBiZSBieXRlLXRyYW5z"
    "cGFyZW50IGZvciBhcmJpdHJhcnkgYWxyZWFkeS1lbmNvZGVkIGJ5dGVzIGNvbWluZwojb2ZmIHRo"
    "ZSB3aXJlLCBidXQgaXQgY2FuJ3QgcmVwcmVzZW50IEN5cmlsbGljICpVbmljb2RlKiB0ZXh0IGF0"
    "IGFsbCAtIGEKI0N5cmlsbGljIE1PVEQgdHlwZWQgaW50byB0aGUgR1VJIHdvdWxkIHNpbGVudGx5"
    "IHR1cm4gaW50byAnPydzIG9uIGVuY29kZS4gQQojbmFtZWQgY29kZXBhZ2UgdGhhdCBtYXRjaGVz"
    "IHRoZSBhY3R1YWwgcGxheWVycyBpcyB0aGUgb25seSBjaG9pY2UgdGhhdCBpcwojY29ycmVjdCBp"
    "biBib3RoIGRpcmVjdGlvbnMuKQpfV0lSRV9FTkMgPSAnY3AxMjUxJwpkZWYgd2lyZV9lbmNvZGUo"
    "dGV4dCk6CiAgICByZXR1cm4gdGV4dC5lbmNvZGUoX1dJUkVfRU5DLCAncmVwbGFjZScpCmRlZiB3"
    "aXJlX2RlY29kZShkYXRhKToKICAgIHJldHVybiBieXRlcyhkYXRhKS5kZWNvZGUoX1dJUkVfRU5D"
    "KQpkZWYgbWFrZURzdHIodGV4dCk6CiAgICB0ZXh0ID0gd2lyZV9lbmNvZGUodGV4dCkKICAgIHRl"
    "eHRsZW4gPSBsZW4odGV4dCkKICAgIHJldHVybiBzdHJ1Y3QucGFjaygnPEl7fXMnLmZvcm1hdCh0"
    "ZXh0bGVuKSwgdGV4dGxlbiwgdGV4dCkKZGVmIHBhcnNlRHN0cihkYXRhLCBvZmYpOgogICAgW3N0"
    "cmxlbl0gPSBzdHJ1Y3QudW5wYWNrKCc8SScsIGRhdGFbb2ZmOm9mZis0XSkKICAgIG9mZis9IDQg"
    "KyBzdHJsZW4KICAgIHRleHQgPSB3aXJlX2RlY29kZShkYXRhW29mZi1zdHJsZW46IG9mZl0pCiAg"
    "ICByZXR1cm4gdGV4dCwgb2ZmCmRlZiBfc2VydmVyX2luZm9fcGFja2V0KHNlcnZlcm5hbWUpOgog"
    "ICAgbm0gPSBmJysie3NlcnZlcm5hbWVbOl9NQVhfVElUTEVdfSIiVFdNUDI7MTAuMC4wLjUiJwog"
    "ICAgZGV0cyA9IHN0cnVjdC5wYWNrKCc8SScsMCkgKyBtYWtlRHN0cihubSkKICAgIGNkZXRzID0g"
    "emxpYi5jb21wcmVzcyhkZXRzKQogICAgcmV0dXJuIHN0cnVjdC5wYWNrKCc8SScsbGVuKGNkZXRz"
    "KSs0KSArIGNkZXRzCmRlZiBfaW5pdF9lcnJvcihtc2c9J1Vua25vd24gZXJyb3InKToKICAgIGVy"
    "ciA9IHN0cnVjdC5wYWNrKCc8SScsMSkKICAgIGRldHMgPSBiJycuam9pbihbZXJyLCBtYWtlRHN0"
    "cihtc2cpXSkKICAgIGNkZXRzID0gemxpYi5jb21wcmVzcyhkZXRzKQogICAgcGFja2xlbiA9IHN0"
    "cnVjdC5wYWNrKCc8SScsbGVuKGNkZXRzKSs0KQogICAgcmV0dXJuIHBhY2tsZW4rY2RldHMKZGVm"
    "IF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQoc2VyaWFsLCB0aXRsZSwgbW90ZCk6CiAgICB0aXRsZSA9"
    "IHRpdGxlWzpfTUFYX1RJVExFXQogICAgbW90ZCA9IG1vdGRbOl9NQVhfTU9URF0KICAgIHVua0Eg"
    "PSBieXRlcyhbMCwwLDAsMCwgMHg1NSwgMHhhNiwgMHhkOCwgMHgzYl0pCiAgICB1bmtCID0gYnl0"
    "ZXMoWzBdKjQ5KQogICAgdW5rQis9IGdlbjY0KHNlcmlhbCkKICAgIHNlZWQgPSAwCiAgICBncnAg"
    "PSBfZ3JwKHNlZWQpCiAgICB1bmtCKz0gc3RydWN0LnBhY2soJzw2SScsMCxzZWVkLCpncnApCiAg"
    "ICBkZXRzID0gYicnLmpvaW4oW3Vua0EsIG1ha2VEc3RyKHRpdGxlKSwgbWFrZURzdHIobW90ZCks"
    "IHVua0JdKQogICAgY2RldHMgPSB6bGliLmNvbXByZXNzKGRldHMpCiAgICBwYWNrbGVuID0gc3Ry"
    "dWN0LnBhY2soJzxJJyxsZW4oY2RldHMpKzQpCiAgICByZXR1cm4gcGFja2xlbitjZGV0cwpkZWYg"
    "X2dycChzZWVkPTApOgogICAgI25vdCBzdXJlIGlmIGl0IG1hdHRlcnMsIHNob3VsZCBnZW5lcmF0"
    "ZSBmcm9tIHNlZWQ/IHNlZW1zIGZpbmUKICAgIHJldHVybiAoMTE1MzcyMTY0OCw0MDkxNTE5OTcs"
    "MTU0MzM4NzAzNSwxODEwMzA5MzEzKQpkZWYgX2djaG5sKG5hbWUsIGluZGV4KToKICAgIHJldHVy"
    "biBmJ3tuYW1lfSN0cmFuc2xhdGV7bmFtZX1fQ2hhbm5lbF97aW5kZXg6MDJkfScKZGVmIHBfZ2V0"
    "QmxvYihkYXRhLCBjb24pOgogICAgZGNtcCA9IHpsaWIuZGVjb21wcmVzc29iaigpCiAgICBkY21w"
    "LmRlY29tcHJlc3MoZGF0YSkKICAgIGNkYXRzID0gW2RhdGFdCiAgICB0b3RhbCA9IGxlbihkYXRh"
    "KQogICAgY29uLnNldHRpbWVvdXQoTm9uZSkKICAgIHdoaWxlIG5vdCBkY21wLmVvZjoKICAgICAg"
    "ICBjZGF0ID0gY29uLnJlY3YoUkVDVl9CVUZfTEVOKQogICAgICAgIGlmIG5vdCBjZGF0OgogICAg"
    "ICAgICAgICAjcGVlciB2YW5pc2hlZCBtaWQtYmxvYjogcmVjdigpIGtlZXBzIHJldHVybmluZyBi"
    "JycgaW5zdGFudGx5LCBzbwogICAgICAgICAgICAjd2l0aG91dCB0aGlzIHRoZSBsb29wIHNwaW5z"
    "IGF0IDEwMCUgQ1BVIGZvcmV2ZXIgKHNhbWUgZGVmZWN0IHRoYXQKICAgICAgICAgICAgI3dhcyBh"
    "bHJlYWR5IGZpeGVkIGluIENvbm5lY3Rpb25IYW5kbGVyLl9yZWN2TW9yZSkKICAgICAgICAgICAg"
    "cmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoJ2Rpc2Nvbm5lY3RlZCBkdXJpbmcgYmxvYiByZWFk"
    "JykKICAgICAgICB0b3RhbCArPSBsZW4oY2RhdCkKICAgICAgICBpZiB0b3RhbCA+IF9NQVhfQkxP"
    "QjoKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoZidibG9iIGV4Y2VlZHMg"
    "e19NQVhfQkxPQn0gYnl0ZXMnKQogICAgICAgIGNkYXRzLmFwcGVuZChjZGF0KQogICAgICAgIGRj"
    "bXAuZGVjb21wcmVzcyhjZGF0KQogICAgaWYgbGVuKGRjbXAudW51c2VkX2RhdGEpOgogICAgICAg"
    "IGNkYXRzWy0xXT1jZGF0c1stMV1bOi1sZW4oZGNtcC51bnVzZWRfZGF0YSldCiAgICBmY2JsID0g"
    "YicnLmpvaW4oY2RhdHMpCiAgICByZXR1cm4gZmNibCwgZGNtcC51bnVzZWRfZGF0YQojRGlyZWN0"
    "UGxheSBhZGRyZXNzZXMgYXJlIFVSTHMgb2YgdGhlIHNoYXBlCiMgIHgtZGlyZWN0cGxheTovcHJv"
    "dmlkZXI9JTdCLi4lN0Q7aG9zdG5hbWU9MTkyLjE2OC4wLjEwO3BvcnQ9MjMwMgojd2l0aCB1bm9y"
    "ZGVyZWQsIHNlbWljb2xvbi1zZXBhcmF0ZWQga2V5PXZhbHVlIHBhaXJzLiBPbmx5IHRoZSBob3N0"
    "IGNvbXBvbmVudAojaXMgdG91Y2hlZDsgZXZlcnl0aGluZyBlbHNlIChwcm92aWRlciBHVUlELCBw"
    "b3J0LCBhcHBsaWNhdGlvbiBpbnN0YW5jZSkgaXMKI3RoZSBob3N0J3MgYnVzaW5lc3MgYW5kIGlz"
    "IHBhc3NlZCB0aHJvdWdoIHVudG91Y2hlZC4KX1JFX0RQX0hPU1ROQU1FID0gcmUuY29tcGlsZShy"
    "Jyg/aSkoaG9zdG5hbWU9KShbXjtdKiknKQpfUkVfRFBfQUxUID0gcmUuY29tcGlsZShyJyg/aSk7"
    "P2FsdD1bXjtdKicpCmRlZiBfaXNHbG9iYWxBZGRyZXNzKGFkZHIpOgogICAgIyJDYW4gYSBwbGF5"
    "ZXIgb24gYW5vdGhlciBuZXR3b3JrIG9wZW4gYSBzb2NrZXQgdG8gdGhpcz8iIExvb3BiYWNrLAog"
    "ICAgI2xpbmstbG9jYWwgKGluY2x1ZGluZyBJUHY2IGZlODA6OikgYW5kIFJGQzE5MTggYWxsIGZh"
    "aWwgdGhhdCB0ZXN0LgogICAgdHJ5OgogICAgICAgIHJldHVybiBpcGFkZHJlc3MuaXBfYWRkcmVz"
    "cyhhZGRyKS5pc19nbG9iYWwKICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgIHJldHVybiBG"
    "YWxzZQpfcHVibGljSXBDYWNoZSA9IFtOb25lLCAwLjBdCl9wdWJsaWNJcExvY2sgPSB0aHJlYWRp"
    "bmcuTG9jaygpCmRlZiBfc2VydmVyUHVibGljQWRkcmVzcygpOgogICAgI1RoZSBwdWJsaWMgYWRk"
    "cmVzcyBvZiB0aGUgbWFjaGluZSB0aGlzIHNlcnZlciBydW5zIG9uLiBVc2VkIGZvciBhIGhvc3QK"
    "ICAgICN3aG9zZSBvYnNlcnZlZCBhZGRyZXNzIGlzIHByaXZhdGUsIHdoaWNoIGhhcHBlbnMgd2hl"
    "bmV2ZXIgdGhlIGhvc3Qgc2l0cwogICAgI29uIHRoZSBzYW1lIExBTi9yb3V0ZXIgYXMgdGhlIGxv"
    "YmJ5IC0gaW5jbHVkaW5nIHRoZSBoYWlycGluLU5BVCBjYXNlCiAgICAjd2hlcmUgYSBsb2NhbCBw"
    "bGF5ZXIgcmVhY2hlcyB0aGUgc2VydmVyIHRocm91Z2ggdGhlIHJvdXRlcidzIHB1YmxpYwogICAg"
    "I2FkZHJlc3MgYW5kIHRoZSByb3V0ZXIgcmV3cml0ZXMgdGhlIHNvdXJjZSB0byBpdHMgb3duIExB"
    "TiBhZGRyZXNzLiBJbiBhbGwKICAgICNvZiB0aG9zZSB0aGUgaG9zdCByZWFjaGVzIHRoZSBpbnRl"
    "cm5ldCB0aHJvdWdoIHRoZSBzYW1lIHJvdXRlciBhcyB3ZSBkbywKICAgICNzbyBvdXIgcHVibGlj"
    "IGFkZHJlc3MgaXMgdGhlaXJzLgogICAgd2l0aCBfcHVibGljSXBMb2NrOgogICAgICAgIChpcCwg"
    "ZmV0Y2hlZCkgPSBfcHVibGljSXBDYWNoZQogICAgICAgIGlmIGlwIGFuZCAodGltZS5tb25vdG9u"
    "aWMoKSAtIGZldGNoZWQpIDwgMzYwMDoKICAgICAgICAgICAgcmV0dXJuIGlwCiAgICBnb3QgPSBO"
    "b25lCiAgICBmb3IgKHVybCwgaGRycykgaW4gKCgnaHR0cHM6Ly8yaXAucnUnLCB7J1VzZXItQWdl"
    "bnQnOiAnY3VybC84LjAnfSksCiAgICAgICAgICAgICAgICAgICAgICAgICgnaHR0cHM6Ly9hcGku"
    "aXBpZnkub3JnJywge30pKToKICAgICAgICB0cnk6CiAgICAgICAgICAgIHJlcSA9IHVybGxpYi5y"
    "ZXF1ZXN0LlJlcXVlc3QodXJsLCBoZWFkZXJzPWhkcnMpCiAgICAgICAgICAgIHdpdGggdXJsbGli"
    "LnJlcXVlc3QudXJsb3BlbihyZXEsIHRpbWVvdXQ9NCkgYXMgcjoKICAgICAgICAgICAgICAgIGNh"
    "bmQgPSByLnJlYWQoKS5kZWNvZGUoJ2FzY2lpJywgZXJyb3JzPSdpZ25vcmUnKS5zdHJpcCgpCiAg"
    "ICAgICAgICAgIGlmIF9pc0dsb2JhbEFkZHJlc3MoY2FuZCk6CiAgICAgICAgICAgICAgICBnb3Qg"
    "PSBjYW5kCiAgICAgICAgICAgICAgICBicmVhawogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAg"
    "ICAgICAgICAgIGNvbnRpbnVlICNvZmZsaW5lIG9yIHRoZSBzZXJ2aWNlIGlzIGJsb2NrZWQ7IG5v"
    "dCBmYXRhbAogICAgd2l0aCBfcHVibGljSXBMb2NrOgogICAgICAgIGlmIGdvdDoKICAgICAgICAg"
    "ICAgX3B1YmxpY0lwQ2FjaGVbOl0gPSBbZ290LCB0aW1lLm1vbm90b25pYygpXQogICAgcmV0dXJu"
    "IGdvdApkZWYgcGlja0dhbWVIb3N0QWRkcmVzcyhwZWVyX2FkZHIpOgogICAgIy0+IChhZGRyZXNz"
    "X29yX05vbmUsIG5vdGVfZm9yX3RoZV9sb2cpCiAgICAjVGhlIGhvc3QncyBvd24gYWRkcmVzcyB3"
    "aW5zIHdoZW5ldmVyIGl0IGlzIG9uZSB0aGUgcmVzdCBvZiB0aGUgaW50ZXJuZXQKICAgICNjYW4g"
    "cmVhY2guIFB1YmxpY0hvc3RBZGRyZXNzIGlzIE5PVCBhIGJsYW5rZXQgb3ZlcnJpZGU6IGl0IGRl"
    "c2NyaWJlcyB0aGUKICAgICNuZXR3b3JrICp0aGlzIHNlcnZlciogc2l0cyBvbiwgc28gYXBwbHlp"
    "bmcgaXQgdG8gYSBob3N0IHdobyBjb25uZWN0ZWQKICAgICNmcm9tIHNvbWV3aGVyZSBlbHNlIGVu"
    "dGlyZWx5IHdvdWxkIHNlbmQgZXZlcnkgam9pbmVyIHRvIHRoZSB3cm9uZwogICAgI21hY2hpbmUg"
    "LSBpdCBvbmx5IGFuc3dlcnMgdGhlIHF1ZXN0aW9uICJ3aGF0IGlzIHRoZSBwdWJsaWMgYWRkcmVz"
    "cyBvZiBhCiAgICAjaG9zdCB0aGF0IGFwcGVhcnMgdG8gYmUgb24gb3VyIG93biBMQU4iLgogICAg"
    "aWYgX2lzR2xvYmFsQWRkcmVzcyhwZWVyX2FkZHIpOgogICAgICAgIHJldHVybiBwZWVyX2FkZHIs"
    "IGYnaG9zdCBjb25uZWN0ZWQgZnJvbSB7cGVlcl9hZGRyfScKICAgIGlmIF9QVUJMSUNfSE9TVF9B"
    "RERSRVNTOgogICAgICAgIHJldHVybiBfUFVCTElDX0hPU1RfQUREUkVTUywgKGYnaG9zdCBjb25u"
    "ZWN0ZWQgZnJvbSB7cGVlcl9hZGRyfSAocHJpdmF0ZSAtIHNhbWUgJwogICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgIGYnbmV0d29yayBhcyB0aGlzIHNlcnZlciksIHVzaW5nIGNv"
    "bmZpZ3VyZWQgJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYnUHVibGlj"
    "SG9zdEFkZHJlc3Mge19QVUJMSUNfSE9TVF9BRERSRVNTfScpCiAgICBwdWIgPSBfc2VydmVyUHVi"
    "bGljQWRkcmVzcygpCiAgICBpZiBwdWI6CiAgICAgICAgcmV0dXJuIHB1YiwgKGYnaG9zdCBjb25u"
    "ZWN0ZWQgZnJvbSB7cGVlcl9hZGRyfSAocHJpdmF0ZSAtIHNhbWUgbmV0d29yayBhcyAnCiAgICAg"
    "ICAgICAgICAgICAgICAgIGYndGhpcyBzZXJ2ZXIpLCB1c2luZyBvdXIgcHVibGljIGFkZHJlc3Mg"
    "e3B1Yn0nKQogICAgcmV0dXJuIE5vbmUsIChmJ2hvc3QgY29ubmVjdGVkIGZyb20ge3BlZXJfYWRk"
    "cn0gKHByaXZhdGUpIGFuZCB0aGlzIHNlcnZlciAnCiAgICAgICAgICAgICAgICAgIGYnY291bGQg"
    "bm90IGRldGVybWluZSBpdHMgb3duIHB1YmxpYyBhZGRyZXNzJykKZGVmIHJld3JpdGVHYW1lSG9z"
    "dCh1cmwsIHBlZXJfYWRkcik6CiAgICAjLT4gKHVybCwgbm90ZV9mb3JfdGhlX2xvZykKICAgIGlm"
    "IG5vdCBfUkVXUklURV9HQU1FX0hPU1Qgb3Igbm90IHVybCBvciBub3QgcGVlcl9hZGRyOgogICAg"
    "ICAgIHJldHVybiB1cmwsICdyZXdyaXRlIGRpc2FibGVkJwogICAgKGFkZHIsIG5vdGUpID0gcGlj"
    "a0dhbWVIb3N0QWRkcmVzcyhwZWVyX2FkZHIpCiAgICBpZiBub3QgYWRkcjoKICAgICAgICByZXR1"
    "cm4gdXJsLCBub3RlICsgJyAtIHVybCBwYXNzZWQgdGhyb3VnaCB1bmNoYW5nZWQnCiAgICBpZiBf"
    "UkVfRFBfSE9TVE5BTUUuc2VhcmNoKHVybCk6CiAgICAgICAgb2xkID0gX1JFX0RQX0hPU1ROQU1F"
    "LnNlYXJjaCh1cmwpLmdyb3VwKDIpCiAgICAgICAgdXJsID0gX1JFX0RQX0hPU1ROQU1FLnN1Yihs"
    "YW1iZGEgbTogbS5ncm91cCgxKSArIGFkZHIsIHVybCwgY291bnQ9MSkKICAgICAgICBub3RlICs9"
    "IGYnOyBob3N0bmFtZSB7b2xkIXJ9IC0+IHthZGRyIXJ9JwogICAgZWxzZToKICAgICAgICAjTm8g"
    "aG9zdG5hbWUgYXQgYWxsOiB0aGUgam9pbmVyIHdvdWxkIGhhdmUgbm90aGluZyB0byBjb25uZWN0"
    "IHRvLgogICAgICAgIHVybCA9IHVybCArICgnJyBpZiB1cmwuZW5kc3dpdGgoJzsnKSBlbHNlICc7"
    "JykgKyAnaG9zdG5hbWU9JyArIGFkZHIKICAgICAgICBub3RlICs9IGYnOyBubyBob3N0bmFtZSBp"
    "biB1cmwsIGFwcGVuZGVkIHthZGRyIXJ9JwogICAgaWYgX1NUUklQX0FMVF9BRERSRVNTRVMgYW5k"
    "IF9SRV9EUF9BTFQuc2VhcmNoKHVybCk6CiAgICAgICAgdXJsID0gX1JFX0RQX0FMVC5zdWIoJycs"
    "IHVybCkKICAgICAgICBub3RlICs9ICc7IGRyb3BwZWQgYWx0PSBjYW5kaWRhdGUgYWRkcmVzc2Vz"
    "JwogICAgcmV0dXJuIHVybCwgbm90ZQpkZWYgcHJldHR5X2d1aWQoZ3VpZCk6CiAgICAoYSxiLGMs"
    "ZCkgPSBzdHJ1Y3QudW5wYWNrKCI8SUhIOHMiLCBndWlkKQogICAgZGEgPSAnJwogICAgZGIgPSAn"
    "JwogICAgZm9yIGkgaW4gZFswOjJdOgogICAgICAgIGRhKz0nezowMnh9Jy5mb3JtYXQoaSkKICAg"
    "IGZvciBpIGluIGRbMjpdOgogICAgICAgIGRiKz0nezowMnh9Jy5mb3JtYXQoaSkKICAgIHJldHVy"
    "biAnezowOHh9LXs6MDR4fS17OjA0eH0te30te30nLmZvcm1hdChhLGIsYyxkYSxkYikKZGVmIF9l"
    "bShtc2cpOgogICAgcmV0dXJuIHdpcmVfZW5jb2RlKG1zZykrX04KZGVmIF9kZWNvbXByZXNzX2Jv"
    "dW5kZWQoZGF0YSwgbGltaXQpOgogICAgI3psaWIuZGVjb21wcmVzcygpIHdpdGggbm8gY2FwIHR1"
    "cm5zIGEgc21hbGwgY29tcHJlc3NlZCBwYWNrZXQgaW50byBhbgogICAgI2FyYml0cmFyaWx5IGxh"
    "cmdlIGFsbG9jYXRpb24gKHppcCBib21iKS4gbWF4X2xlbmd0aCBzdG9wcyBhdCB0aGUgY2FwLCBh"
    "bmQKICAgICNhIG5vbi1lbXB0eSB1bmNvbnN1bWVkX3RhaWwgdGVsbHMgdXMgdGhlIHJlYWwgcGF5"
    "bG9hZCB3YXMgYmlnZ2VyLgogICAgZGNtcCA9IHpsaWIuZGVjb21wcmVzc29iaigpCiAgICBvdXQg"
    "PSBkY21wLmRlY29tcHJlc3MoZGF0YSwgbGltaXQpCiAgICBpZiBkY21wLnVuY29uc3VtZWRfdGFp"
    "bDoKICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnZGVjb21wcmVzc2VkIHBheWxvYWQgZXhj"
    "ZWVkcyB7bGltaXR9IGJ5dGVzJykKICAgIHJldHVybiBvdXQKY2xhc3MgUHJvdG9jb2xFcnJvcihF"
    "eGNlcHRpb24pOgogICAgI0NsaWVudCBzZW50IHNvbWV0aGluZyBtYWxmb3JtZWQgb3Igb3V0IG9m"
    "IHJhbmdlLiBOb3QgYSBzZXJ2ZXIgZmF1bHQ6IHRoZQogICAgI2Nvbm5lY3Rpb24gaXMgZHJvcHBl"
    "ZCB3aXRoIGEgb25lLWxpbmUgbG9nIGluc3RlYWQgb2YgYSB0cmFjZWJhY2suCiAgICBwYXNzCl9S"
    "RV9WQUxJRF9VU0VSTkFNRSA9IHJlLmNvbXBpbGUocideW0EtWmEtejAtOV9cLV17MywzMn0kJykK"
    "ZGVmIHNhbml0aXplVGV4dCh0ZXh0KToKICAgICNzdHJpcCBjaGFyYWN0ZXJzIHRoYXQgd291bGQg"
    "YnJlYWsgdGhlIHF1b3RlZC1zdHJpbmcgYmFzZWQgbG9iYnkgcHJvdG9jb2wKICAgICNvciBhbGxv"
    "dyBhIGNsaWVudCB0byBmb3JnZSBhZGRpdGlvbmFsIHByb3RvY29sIGZpZWxkcyAocHJvdG9jb2wg"
    "aW5qZWN0aW9uKQogICAgaWYgdGV4dCBpcyBOb25lOgogICAgICAgIHJldHVybiAnJwogICAgcmV0"
    "dXJuIHRleHQucmVwbGFjZSgnIicsICInIikucmVwbGFjZSgnXDAnLCAnJykucmVwbGFjZSgnXHIn"
    "LCAnJykucmVwbGFjZSgnXG4nLCAnICcpCmRlZiBqc29uVGltZShkdCk6CiAgICBpZiBub3QgZHQu"
    "dXRjb2Zmc2V0KCk6CiAgICAgICAgdHppbmZvID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KGRhdGV0"
    "aW1lLnRpbWV6b25lLnV0YykuYXN0aW1lem9uZSgpLnR6aW5mbwogICAgICAgIGR0ID0gZHQucmVw"
    "bGFjZSh0emluZm89dHppbmZvKQogICAgZHQgPSBkdC5hc3RpbWV6b25lKGRhdGV0aW1lLnRpbWV6"
    "b25lLnV0YykucmVwbGFjZSh0emluZm89Tm9uZSkKICAgIHJldHVybiBkdC5pc29mb3JtYXQoKSAr"
    "ICJaIgogICAgI3Nob3VsZCByZXR1cm4gMjAxMi0wNC0yM1QxODoyNTo0My41MTFaIHV0YyB0aW1l"
    "IGZvciBqYXZhc2NyaXB0IHBhcnNpbmcKCiMjIE1BSU4gU0VSVkVSIENPREUKClJFQ1ZfQlVGX0xF"
    "TiA9IDIqKjEyCgpfVkVSU0lPTiA9ICcwLjIuMCcKcHJpbnQoZidTZXJ2ZXIgdmVyaXNpb24ge19W"
    "RVJTSU9OfScpCl9ERUJVR19BTExPV19BTllfTE9HSU4gPSBGYWxzZSAjZG9lcyBub3QgdmVyaWZ5"
    "IGxvZ2lucywgZm9yIGRlYnVnIHJlYXNvbnMKX1RXX0xPQkJZX1BPUlQgPSAxNzE3MQpfQVVUT19S"
    "RUdJU1RFUiA9IFRydWUKI1VwcGVyIGJvdW5kIGZvciBhIHNpbmdsZSBsZW5ndGgtcHJlZml4ZWQg"
    "YmxvYiBmcm9tIGEgY2xpZW50IChwbGF5ZXJkYXRhLAojaGVyb2RhdGEsIGdhbWUtY29tbWFuZCBw"
    "YXlsb2FkKS4gR2VuZXJvdXMgY29tcGFyZWQgdG8gYSByZWFsIHNhdmUsIGJ1dCBmaW5pdGU6CiN3"
    "aXRob3V0IGl0IGEgY2xpZW50IGNvdWxkIGFubm91bmNlIGFuIGFyYml0cmFyeSBsZW5ndGggYW5k"
    "IG1ha2UgdGhlIHNlcnZlcgojYnVmZmVyIHVudGlsIGl0IHJhbiBvdXQgb2YgbWVtb3J5LgpfTUFY"
    "X0JMT0IgPSAxNiAqIDEwMjQgKiAxMDI0CiNIYW5kc2hha2UvbG9naW4gcGFja2V0cyBhcmUgYSBm"
    "ZXcgaHVuZHJlZCBieXRlcyBpbiBwcmFjdGljZS4gVGhlc2UgYm91bmRzCiNhcHBseSAqYmVmb3Jl"
    "KiBhdXRoZW50aWNhdGlvbiwgd2hlcmUgYW55b25lIHdobyBjYW4gcmVhY2ggdGhlIHBvcnQgY2Fu"
    "IHNlbmQKI3doYXRldmVyIHRoZXkgbGlrZSwgc28gdGhleSBhcmUgZGVsaWJlcmF0ZWx5IHRpZ2h0"
    "LgpfTUFYX0hBTkRTSEFLRSA9IDY0ICogMTAyNApfTUFYX0hBTkRTSEFLRV9JTkZMQVRFRCA9IDEw"
    "MjQgKiAxMDI0CgojLS0tIHN5bmNocm9uaXNhdGlvbiB0dW5pbmcgLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQojSG93IG9mdGVuIHRoZSBhY2N1bXVsYXRl"
    "ZCBoZXJvIHBvc2l0aW9ucyBpbiBhIHRvd24gYXJlIHB1c2hlZCB0byBldmVyeW9uZSBpbgojaXQu"
    "IFRoaXMgdXNlZCB0byBiZSBwaW5uZWQgdG8gdGhlIDFzIHNvY2tldHNlcnZlciBwb2xsIGludGVy"
    "dmFsLCB3aGljaCBpcyB3aGF0CiNtYWRlIG90aGVyIHBsYXllcnMnIG1hcCBtYXJrZXJzIGp1bXAg"
    "YSBmdWxsIHNlY29uZCBhdCBhIHRpbWUuIEVhY2ggdGljayBzZW5kcwojb25lIHBhY2tldCBwZXIg"
    "dG93biBhbmQgb25seSBpZiBzb21lYm9keSBhY3R1YWxseSBtb3ZlZCwgc28gZXZlbiBhdCB0aGlz"
    "IHJhdGUKI2l0J3MgYSBoYW5kZnVsIG9mIHNtYWxsIHBhY2tldHMvc2VjIGZvciBhIGNvLW9wLXNp"
    "emVkIGdyb3VwIC0gbmVnbGlnaWJsZQojYmFuZHdpZHRoIGVpdGhlciBvbiBMQU4gb3Igb3ZlciBh"
    "IGhvbWUgaW50ZXJuZXQgY29ubmVjdGlvbiAtIHdoaWxlIGdldHRpbmcKI25vdGljZWFibHkgY2xv"
    "c2VyIHRvIHNtb290aCBtb3Rpb24gdGhhbiB0aGUgb2xkIDFIeiBiYXNlbGluZS4KX1BPU19VUERB"
    "VEVfSFogPSAxMC4wCl9QT1NfVVBEQVRFX0haX01BWCA9IDIwLjAKI0Ryb3AgYSBjb25uZWN0aW9u"
    "IHRoYXQgaGFzIG5vdCBzZW50IGEgc2luZ2xlIGJ5dGUgaW4gdGhpcyBsb25nLiBBIHBsYXllciB3"
    "aG9zZQojbGluayBkaWVzIHdpdGhvdXQgYSBjbGVhbiBUQ1AgY2xvc2Ugb3RoZXJ3aXNlIGtlZXBz"
    "IHRoZWlyIHVzZXJuYW1lIGNsYWltZWQKI2ZvcmV2ZXIsIGFuZCB0aGVpciBuZXh0IGxvZ2luIGF0"
    "dGVtcHQgaXMgcmVqZWN0ZWQgd2l0aCAnQWNjb3VudCBhbHJlYWR5IGxvZ2dlZAojaW4nIHVudGls"
    "IHRoZSBzZXJ2ZXIgaXMgcmVzdGFydGVkLiAwIGRpc2FibGVzLgpfSURMRV9USU1FT1VUID0gMzAw"
    "CiNCbG9ja2luZyByZWN2KCkgdGltZW91dCBpbiB0aGUgcmVhZCBsb29wLiBPbmx5IGdvdmVybnMg"
    "aG93IHF1aWNrbHkgYSB0aHJlYWQKI25vdGljZXMgc2VydmVyIHNodXRkb3duIGFuZCB0aGUgaWRs"
    "ZSBkZWFkbGluZTsgb3V0Ym91bmQgbGF0ZW5jeSBubyBsb25nZXIKI2RlcGVuZHMgb24gaXQgbm93"
    "IHRoYXQgZWFjaCBjb25uZWN0aW9uIGhhcyBpdHMgb3duIHdyaXRlciB0aHJlYWQuCl9SRUFEX1RJ"
    "TUVPVVQgPSAxLjAKI0hvdyBsb25nIGEgY2xpZW50IGdldHMgdG8gZmluaXNoIGRlbGl2ZXJpbmcg"
    "YSBibG9iIGl0IGhhcyBhbHJlYWR5IGFubm91bmNlZAojdGhlIGxlbmd0aCBvZi4gR2VuZXJvdXMg"
    "Zm9yIGEgbGFyZ2Ugc2F2ZSBvdmVyIGEgc2xvdyBsaW5rLCBidXQgZmluaXRlIC0gc2VlCiNfUmVh"
    "ZEJsb2IuCl9CTE9CX1RJTUVPVVQgPSA2MC4wCiNUaGUgbG9iYnkgb25seSBicm9rZXJzIHRoZSBj"
    "by1vcCBzZXNzaW9uOyB0aGUgc2Vzc2lvbiBpdHNlbGYgaXMgYSBkaXJlY3QKI0RpcmVjdFBsYXkg"
    "Y29ubmVjdGlvbiBmcm9tIHRoZSBqb2luaW5nIHBsYXllciB0byB0aGUgaG9zdCwgYXQgdGhlIGFk"
    "ZHJlc3MgdGhlCiNob3N0IHB1dHMgaW4gdGhlIHgtZGlyZWN0cGxheSBVUkwgb2YgaXRzIC9jcmVh"
    "dGVnYW1lLiBUaGUgaG9zdCdzIG93biBjbGllbnQKI2ZpbGxzIHRoYXQgaW4gZnJvbSBpdHMgbG9j"
    "YWwgYWRhcHRlciwgc28gYmVoaW5kIGEgcm91dGVyIGl0IGFkdmVydGlzZXMKI3NvbWV0aGluZyBs"
    "aWtlIDE5Mi4xNjguMC4xMCAtIHVucmVhY2hhYmxlIGZvciBhbnlvbmUgbm90IG9uIHRoYXQgTEFO"
    "LCBhbmQgdGhlCiNqb2luZXIgc2l0cyBvbiAiY29ubmVjdGluZyIgdW50aWwgaXQgZ2l2ZXMgdXAu"
    "IEV2ZXJ5dGhpbmcgdGhhdCBnb2VzIHRocm91Z2gKI3RoZSBsb2JieSAodG93biwgY2hhdCwgc2Vl"
    "aW5nIGVhY2ggb3RoZXIgbW92ZSkga2VlcHMgd29ya2luZywgd2hpY2ggaXMgd2hhdAojbWFrZXMg"
    "dGhpcyBsb29rIGxpa2UgYSByb29tLXNwZWNpZmljIGJ1Zy4KI1RoZSBzZXJ2ZXIgYWxyZWFkeSBr"
    "bm93cyBhbiBhZGRyZXNzIGZvciB0aGUgaG9zdCB0aGF0IGV2ZXJ5IG90aGVyIGNsaWVudCBjYW4K"
    "I3JlYWNoOiB0aGUgc291cmNlIGFkZHJlc3Mgb2YgdGhlIGhvc3QncyBvd24gY29ubmVjdGlvbiB0"
    "byB1cy4gU3Vic3RpdHV0aW5nIGl0CiNpcyB3aGF0IG1ha2VzIGNyb3NzLWludGVybmV0IGNvLW9w"
    "IHdvcmsgYXQgYWxsLgojVHVybiBvZmYgKENvbmZpZy5pbmk6IFJld3JpdGVHYW1lSG9zdCA9IEZh"
    "bHNlKSBpZiBldmVyeSBwbGF5ZXIgaXMgb24gdGhlIHNhbWUKI0xBTiBhcyB0aGUgaG9zdCBidXQg"
    "dGhlIGxvYmJ5IGlzIG5vdCAtIHRoZW4gdGhlIGhvc3QncyBvd24gTEFOIGFkZHJlc3MgaXMgdGhl"
    "CiNjb3JyZWN0IG9uZSBhbmQgb3VycyBpcyBub3QuCl9SRVdSSVRFX0dBTUVfSE9TVCA9IFRydWUK"
    "I0V4cGxpY2l0IHB1YmxpYyBhZGRyZXNzIG9mIHRoZSBtYWNoaW5lIHRoYXQgaG9zdHMgcm9vbXMs"
    "IGZvciB0aGUgY2FzZSB0aGUKI3NlcnZlciBjYW5ub3Qgd29yayBpdCBvdXQgKHNlZSBfcHVibGlj"
    "QWRkcmVzcykuIFNldCBpdCBpbiBDb25maWcuaW5pIGFzCiNQdWJsaWNIb3N0QWRkcmVzcyBpZiBh"
    "dXRvLWRldGVjdGlvbiBwaWNrcyB0aGUgd3Jvbmcgb25lLgpfUFVCTElDX0hPU1RfQUREUkVTUyA9"
    "ICcnCiNUaGUgZ2FtZSBhcHBlbmRzIGEgcHJvcHJpZXRhcnkgJ2FsdD0nIGZpZWxkIHRvIHRoZSBE"
    "aXJlY3RQbGF5IFVSTCBob2xkaW5nCiNldmVyeSBhZGRyZXNzIG9mIGV2ZXJ5IGFkYXB0ZXIgdGhl"
    "IGhvc3QgaGFzOiBvYnNlcnZlZCBpbiB0aGUgd2lsZCBpdCBjYXJyaWVkCiNhIFRlcmVkbyAyMDAx"
    "OjA6Oi8zMiBhZGRyZXNzLCBhbiBmZTgwOjogbGluay1sb2NhbCBvbmUgYW5kIHRoZSBob3N0J3Mg"
    "TEFOCiNJUHY0IC0gbm9uZSBvZiB0aGVtIHJlYWNoYWJsZSBmcm9tIGFub3RoZXIgbmV0d29yay4g"
    "QSBqb2luZXIgdGhhdCB3b3JrcwojdGhyb3VnaCB0aGF0IGNhbmRpZGF0ZSBsaXN0IHdhaXRzIG91"
    "dCBhIGNvbm5lY3Rpb24gdGltZW91dCBvbiBlYWNoLCB3aGljaAojbG9va3MgZXhhY3RseSBsaWtl"
    "ICJjb25uZWN0aW5nIGZvcmV2ZXIiLiBEcm9wcGluZyB0aGUgZmllbGQgbGVhdmVzIHRoZSBzaW5n"
    "bGUKI2FkZHJlc3MgdGhpcyBzZXJ2ZXIga25vd3MgdG8gYmUgcmVhY2hhYmxlLgpfU1RSSVBfQUxU"
    "X0FERFJFU1NFUyA9IFRydWUKI0xvZyBldmVyeSBjb21tYW5kIHJlY2VpdmVkIGZyb20gY2xpZW50"
    "cywgd2l0aCBpdHMgcmF3IHRleHQuIFZlcmJvc2UsIGJ1dCB0aGlzCiNwcm90b2NvbCBpcyBvbmx5"
    "IHBhcnRpYWxseSBkb2N1bWVudGVkIGFuZCBpdCBpcyB0aGUgb25seSB3YXkgdG8gc2VlIHdoYXQg"
    "dGhlCiNjbGllbnQgYWN0dWFsbHkgYXNrcyBmb3Igd2hlbiBhIGZlYXR1cmUgZG9lcyBub3RoaW5n"
    "LgpfREVCVUdfTE9HX0NPTU1BTkRTID0gVHJ1ZQojL3VwZGhlcm9wb3MgYW5kIC9ub3AgYXJyaXZl"
    "IH4xMCB0aW1lcyBhIHNlY29uZCBwZXIgcGxheWVyIGFuZCBzYXkgbm90aGluZwojdXNlZnVsLiBM"
    "b2dnaW5nIHRoZW0gY29zdCB0d28gZm9ybWF0dGVkIGxpbmVzLCBhIHF1ZXVlIHB1dCwgYSBHVUkg"
    "aW5zZXJ0IGFuZAojYSBkaXNrIHdyaXRlICppbnNpZGUgdGhlIGNvbW1hbmQgaGFuZGxlciosIG9u"
    "IHRoZSBvbmUgcGF0aCB0aGF0IGhhcyB0byBzdGF5CiNxdWljayAtIHNlbGYtaW5mbGljdGVkIGxh"
    "dGVuY3kgYW5kIGppdHRlciBvbiBleGFjdGx5IHRoZSB0cmFmZmljIGJlaW5nCiNkZWJ1Z2dlZCwg"
    "cGx1cyBhIGxvZyBzbyBub2lzeSB0aGF0IHRoZSBpbnRlcmVzdGluZyBsaW5lcyBzY3JvbGwgYXdh"
    "eS4gU2V0CiNEZWJ1Z0NvbW1hbmRzVmVyYm9zZSA9IFRydWUgaW4gQ29uZmlnLmluaSB0byBzZWUg"
    "dGhlbSBhbnl3YXkuCl9ERUJVR19MT0dfVkVSQk9TRSA9IEZhbHNlCl9RVUlFVF9DT01NQU5EUyA9"
    "IGZyb3plbnNldCgoJy91cGRoZXJvcG9zJywgJy9ub3AnKSkKI0NvbnNlcnZhdGl2ZSBjYXAgb24g"
    "YSBzaW5nbGUgZ2VuZXJhdGVkIGNvbW1hbmQgbGluZS4gTm90aGluZyB0aGUgcmV0YWlsCiNjbGll"
    "bnQgc2VuZHMgY29tZXMgY2xvc2UgdG8gdGhpcywgc28gaXQgaXMgd2VsbCBpbnNpZGUgd2hhdGV2"
    "ZXIgdGhlIGNsaWVudAojaXRzZWxmIGlzIGJ1aWx0IHRvIGhhbmRsZS4KX01BWF9XSVJFX0xJTkUg"
    "PSA5MDAKI1NlcnZlci1jb250cm9sbGVkIHRleHQgdGhhdCByZWFjaGVzIHRoZSBjbGllbnQ6IHRo"
    "ZSB0aXRsZSBhbmQgdGhlIG1lc3NhZ2Ugb2YKI3RoZSBkYXkgYXJlIHR5cGVkIGJ5IGFuIGFkbWlu"
    "IGludG8gdGhlIEdVSSB3aXRoIG5vIGxlbmd0aCBsaW1pdCBhdCBhbGwsIGFuZAojYm90aCBhcmUg"
    "aGFuZGVkIHRvIHRoZSBjbGllbnQgYXQgbG9naW4sIGJlZm9yZSB0aGUgcGxheWVyIGNhbiBkbyBh"
    "bnl0aGluZwojYWJvdXQgaXQuIFRydW5jYXRlIHJhdGhlciB0aGFuIHRydXN0LgpfTUFYX1RJVExF"
    "ID0gMTI4Cl9NQVhfTU9URCA9IDEwMjQKI0hlcm8gaWRzIG9uIHRoZSB3aXJlOiBoZXggb3IgZGVj"
    "aW1hbC4KI0V2ZXJ5dGhpbmcgcG9zaXRpb25hbCBpbiB0aGlzIHByb3RvY29sIGlzIGhleCAtIHRo"
    "ZSBjbGllbnQncyBvd24KIy91cGRoZXJvcG9zIGNhcnJpZXMgY29vcmRpbmF0ZXMgYXMgIjM4QTQj"
    "MkIxNyIgLSBhbmQgdXBkYXRlUG9zKCkgaGFzIGFsd2F5cwojcHJlZml4ZWQgdGhlIGhlcm8gaWQg"
    "aW4gaGV4IHRvIG1hdGNoLiBCdXQgJGdhbWVjaGFubmVsdXNlciwgdGhlIG1lc3NhZ2UgdGhhdAoj"
    "Zmlyc3QgdGVsbHMgYSBjbGllbnQgd2hpY2ggaWQgYmVsb25ncyB0byB3aGljaCBwbGF5ZXIsIHNl"
    "bnQgdGhlIHNhbWUgaWQgaW4KI2RlY2ltYWwuIEEgY2xpZW50IHRoYXQgcmVhZHMgYm90aCBmaWVs"
    "ZHMgd2l0aCBvbmUgcmFkaXggdGhlcmVmb3JlIGNhbm5vdAojbWF0Y2ggYSBwb3NpdGlvbiB1cGRh"
    "dGUgdG8gdGhlIHBsYXllciBpdCBiZWxvbmdzIHRvLCBhbmQgdGhhdCBoZXJvIHN0b3BzCiNtb3Zp"
    "bmcgb24gZXZlcnlvbmUgZWxzZSdzIG1hcCB3aGlsZSB3YWxraW5nIG5vcm1hbGx5IG9uIHRoZWly"
    "IG93bi4KI0xlZnQgYXMgYSBzd2l0Y2ggYmVjYXVzZSB3aGljaCByYWRpeCB0aGUgcmV0YWlsIGNs"
    "aWVudCB3YW50cyBpcyBub3QKI2RvY3VtZW50ZWQ6IGlmIGhleCB0dXJucyBvdXQgdG8gYmUgdGhl"
    "IHdyb25nIGd1ZXNzLCBzZXQgSGVyb0lkSGV4ID0gRmFsc2UgaW4KI0NvbmZpZy5pbmkgYW5kIGJv"
    "dGggbWVzc2FnZXMgZmFsbCBiYWNrIHRvIGRlY2ltYWwgLSBzdGlsbCBjb25zaXN0ZW50LCB3aGlj"
    "aAojaXMgdGhlIHBhcnQgdGhhdCBhY3R1YWxseSBtYXR0ZXJzLgpfSEVST19JRF9IRVggPSBUcnVl"
    "CiNPcHRpb25hbCBzZXJ2ZXItPmNsaWVudCAnL25vcCcgaGVhcnRiZWF0IGV2ZXJ5IDNzLiBNYWlu"
    "bHkgdXNlZnVsIHRvIHN0b3AgaG9tZQojcm91dGVycyBkcm9wcGluZyB0aGUgTkFUIG1hcHBpbmcg"
    "b2YgYW4gaWRsZSBjby1vcCBzZXNzaW9uLiBPZmYgYnkgZGVmYXVsdDogdGhlCiNyZWFsIGNsaWVu"
    "dCdzIHJlYWN0aW9uIHRvIGFuIHVuc29saWNpdGVkIC9ub3AgaGFzIG5vdCBiZWVuIHZlcmlmaWVk"
    "LgpfU0VORF9OT1BTID0gRmFsc2UKCgpERUZBVUxUX1RJVExFID0gJ0NvbW11bml0eSBNdWx0aXBs"
    "YXllciBTZXJ2ZXInCkRFRkFVTFRfTU9URCA9IGYnPDB4RkYwMDAwRkY+PEYyPkNvbW11bml0eSBN"
    "dWx0aXBsYXllciBTZXJ2ZXIgVmVyc2lvbiB7X1ZFUlNJT059PGJyZWFrPTEwLjA+XHJcbicKCiNS"
    "b290IG5leHQgdG8gdGhpcyBzY3JpcHQgcmF0aGVyIHRoYW4gdGhlIHByb2Nlc3MnIGN1cnJlbnQg"
    "d29ya2luZyBkaXJlY3RvcnksCiNzbyB0aGUgZGF0YWJhc2UvY29uZmlnL3BsYXllcmRhdGEgYWx3"
    "YXlzIGxpdmUgaW4gdGhlIHNhbWUgcGxhY2Ugd2hldGhlciB0aGUKI3NlcnZlciBpcyBkb3VibGUt"
    "Y2xpY2tlZCwgbGF1bmNoZWQgZnJvbSBhIHRlcm1pbmFsIGVsc2V3aGVyZSwgb3IgaW1wb3J0ZWQg"
    "YnkKI2EgR1VJIHdyYXBwZXIgKGUuZy4gVFcxIENvbnRyb2wgQ2VudGVyKS4KI0FsbG93cyBhbiBl"
    "bWJlZGRpbmcgaG9zdCAoZS5nLiBhIHBvcnRhYmxlIGFsbC1pbi1vbmUgbGF1bmNoZXIgdGhhdCBl"
    "eGVjKClzCiN0aGlzIGZpbGUncyBzb3VyY2UgZnJvbSBtZW1vcnksIHdoZXJlIF9fZmlsZV9fIGlz"
    "IG1lYW5pbmdsZXNzKSB0byByZWRpcmVjdAojd2hlcmUgdGhlIGRhdGFiYXNlL2NvbmZpZy9wbGF5"
    "ZXJkYXRhIGxpdmUgYnkgcHJlLXNldHRpbmcgdGhpcyBuYW1lIGluIHRoZQojbW9kdWxlJ3MgZ2xv"
    "YmFscyBiZWZvcmUgdGhlIG1vZHVsZSBib2R5IHJ1bnMuIFN0YW5kYWxvbmUgZXhlY3V0aW9uICh0"
    "aGUKI25vcm1hbCBgcHl0aG9uIFRXMUNTLnB5YCkgaXMgdW5hZmZlY3RlZDogZmFsbHMgYmFjayB0"
    "byBuZXh0IHRvIHRoaXMgc2NyaXB0LgppZiAnX0VYVEVSTkFMX0RBVEFfRElSJyBpbiBnbG9iYWxz"
    "KCkgYW5kIGdsb2JhbHMoKVsnX0VYVEVSTkFMX0RBVEFfRElSJ106CiAgICBfUEFUSF9ST09UID0g"
    "Z2xvYmFscygpWydfRVhURVJOQUxfREFUQV9ESVInXQplbHNlOgogICAgX1BBVEhfUk9PVCA9IG9z"
    "LnBhdGguZGlybmFtZShvcy5wYXRoLmFic3BhdGgoX19maWxlX18pKQpfUEFUSF9EQVRBQkFTRSA9"
    "IG9zLnBhdGguam9pbihfUEFUSF9ST09ULCdTZXJ2ZXJEYXRhLmRiJykKX1BBVEhfQ09ORklHID0g"
    "b3MucGF0aC5qb2luKF9QQVRIX1JPT1QsJ0NvbmZpZy5pbmknKQpfUEFUSF9QTEFZRVJEQVRBID0g"
    "b3MucGF0aC5qb2luKF9QQVRIX1JPT1QsJ1BsYXllckRhdGEnKQoKZGVmIF9lc2NhcGVNT1REKG1v"
    "dGQpOgogICAgI2NvbmZpZ3BhcnNlciB2YWx1ZXMgY2FuJ3Qgc2FmZWx5IGhvbGQgcmF3IENSL0xG"
    "LCBzdG9yZSBhcyBcclxuIGVzY2FwZXMKICAgIHJldHVybiBtb3RkLmVuY29kZSgndW5pY29kZV9l"
    "c2NhcGUnKS5kZWNvZGUoJ2FzY2lpJykKZGVmIF91bmVzY2FwZU1PVEQobW90ZCk6CiAgICAjX2Vz"
    "Y2FwZU1PVEQgYWx3YXlzIHdyaXRlcyBwdXJlIGFzY2lpLCBidXQgYSBoYW5kLWVkaXRlZCBDb25m"
    "aWcuaW5pIG1heSBob2xkCiAgICAjcmF3IDgtYml0IHRleHQ7IHRvbGVyYXRlIGl0IGluc3RlYWQg"
    "b2YgcmVmdXNpbmcgdG8gc3RhcnQgdGhlIHNlcnZlcgogICAgcmV0dXJuIG1vdGQuZW5jb2RlKF9X"
    "SVJFX0VOQywgJ3JlcGxhY2UnKS5kZWNvZGUoJ3VuaWNvZGVfZXNjYXBlJykKX0NPTkZJR19ERUZB"
    "VUxUUyA9IHsKICAgICdTZXJ2ZXJOYW1lJzogREVGQVVMVF9USVRMRSwKICAgICdNT1REJzogX2Vz"
    "Y2FwZU1PVEQoREVGQVVMVF9NT1REKSwKICAgICdQb3J0Jzogc3RyKF9UV19MT0JCWV9QT1JUKSwK"
    "ICAgICdBdXRvUmVnaXN0ZXInOiBzdHIoX0FVVE9fUkVHSVNURVIpLAogICAgJ0FsbG93QW55TG9n"
    "aW4nOiBzdHIoX0RFQlVHX0FMTE9XX0FOWV9MT0dJTiksCiAgICAnUG9zaXRpb25VcGRhdGVIeic6"
    "IHN0cihfUE9TX1VQREFURV9IWiksCiAgICAnSWRsZVRpbWVvdXQnOiBzdHIoX0lETEVfVElNRU9V"
    "VCksCiAgICAnS2VlcGFsaXZlJzogc3RyKF9TRU5EX05PUFMpLAogICAgJ1Jld3JpdGVHYW1lSG9z"
    "dCc6IHN0cihfUkVXUklURV9HQU1FX0hPU1QpLAogICAgJ1B1YmxpY0hvc3RBZGRyZXNzJzogX1BV"
    "QkxJQ19IT1NUX0FERFJFU1MsCiAgICAnU3RyaXBBbHRBZGRyZXNzZXMnOiBzdHIoX1NUUklQX0FM"
    "VF9BRERSRVNTRVMpLAogICAgJ0hlcm9JZEhleCc6IHN0cihfSEVST19JRF9IRVgpLAogICAgJ0Rl"
    "YnVnQ29tbWFuZHMnOiBzdHIoX0RFQlVHX0xPR19DT01NQU5EUyksCiAgICAnRGVidWdDb21tYW5k"
    "c1ZlcmJvc2UnOiBzdHIoX0RFQlVHX0xPR19WRVJCT1NFKSwKfQpkZWYgbG9hZENvbmZpZygpOgog"
    "ICAgY2ZnID0gY29uZmlncGFyc2VyLkNvbmZpZ1BhcnNlcigpCiAgICBjZmdbJ3NlcnZlciddID0g"
    "ZGljdChfQ09ORklHX0RFRkFVTFRTKQogICAgaWYgb3MucGF0aC5leGlzdHMoX1BBVEhfQ09ORklH"
    "KToKICAgICAgICBjZmcucmVhZChfUEFUSF9DT05GSUcpCiAgICBlbHNlOgogICAgICAgIHNhdmVD"
    "b25maWcoY2ZnKQogICAgcmV0dXJuIGNmZwpkZWYgc2F2ZUNvbmZpZyhjZmcpOgogICAgd2l0aCBv"
    "cGVuKF9QQVRIX0NPTkZJRywgJ3cnLCBlbmNvZGluZz0ndXRmLTgnKSBhcyBmOgogICAgICAgIGNm"
    "Zy53cml0ZShmKQpkZWYgYXBwbHlDb25maWcoY2ZnKToKICAgICNBcHBsaWVzIGNvbmZpZyB2YWx1"
    "ZXMgdG8gdGhlIGxpdmUgbW9kdWxlIGdsb2JhbHMuIFNlcnZlck5hbWUvTU9URC8KICAgICNBdXRv"
    "UmVnaXN0ZXIgdGFrZSBlZmZlY3QgaW1tZWRpYXRlbHkgKHJlYWQgZnJlc2ggcGVyIGxvZ2luIGF0"
    "dGVtcHQpOwogICAgI1BvcnQgb25seSB0YWtlcyBlZmZlY3QgZm9yIHNlcnZlcnMgc3RhcnRlZCBh"
    "ZnRlciB0aGlzIGNhbGwuCiAgICBnbG9iYWwgREVGQVVMVF9USVRMRSwgREVGQVVMVF9NT1RELCBf"
    "VFdfTE9CQllfUE9SVCwgX0FVVE9fUkVHSVNURVIsIF9ERUJVR19BTExPV19BTllfTE9HSU4KICAg"
    "IGdsb2JhbCBfUE9TX1VQREFURV9IWiwgX0lETEVfVElNRU9VVCwgX1NFTkRfTk9QUwogICAgZ2xv"
    "YmFsIF9SRVdSSVRFX0dBTUVfSE9TVCwgX0RFQlVHX0xPR19DT01NQU5EUywgX0RFQlVHX0xPR19W"
    "RVJCT1NFCiAgICBnbG9iYWwgX1BVQkxJQ19IT1NUX0FERFJFU1MsIF9TVFJJUF9BTFRfQUREUkVT"
    "U0VTLCBfSEVST19JRF9IRVgKICAgIHNlYyA9IGNmZ1snc2VydmVyJ10KICAgIERFRkFVTFRfVElU"
    "TEUgPSBzZWMuZ2V0KCdTZXJ2ZXJOYW1lJywgZmFsbGJhY2s9REVGQVVMVF9USVRMRSkKICAgIERF"
    "RkFVTFRfTU9URCA9IF91bmVzY2FwZU1PVEQoc2VjLmdldCgnTU9URCcsIGZhbGxiYWNrPV9lc2Nh"
    "cGVNT1REKERFRkFVTFRfTU9URCkpKQogICAgX1RXX0xPQkJZX1BPUlQgPSBzZWMuZ2V0aW50KCdQ"
    "b3J0JywgZmFsbGJhY2s9X1RXX0xPQkJZX1BPUlQpCiAgICBfQVVUT19SRUdJU1RFUiA9IHNlYy5n"
    "ZXRib29sZWFuKCdBdXRvUmVnaXN0ZXInLCBmYWxsYmFjaz1fQVVUT19SRUdJU1RFUikKICAgIF9E"
    "RUJVR19BTExPV19BTllfTE9HSU4gPSBzZWMuZ2V0Ym9vbGVhbignQWxsb3dBbnlMb2dpbicsIGZh"
    "bGxiYWNrPV9ERUJVR19BTExPV19BTllfTE9HSU4pCiAgICAjQ2xhbXBlZCByYXRoZXIgdGhhbiB0"
    "cnVzdGVkOiB0aGVzZSBjb21lIGZyb20gYSBoYW5kLWVkaXRhYmxlIGluaSwgYW5kIGEKICAgICNz"
    "dHJheSAwIG9yIDEwMDAwIGhlcmUgd291bGQgZWl0aGVyIHN0b3AgcG9zaXRpb24gdXBkYXRlcyBl"
    "bnRpcmVseSBvciBzcGluCiAgICAjdGhlIHVwZGF0ZSB0aHJlYWQgZmxhdCBvdXQuCiAgICBoeiA9"
    "IHNlYy5nZXRmbG9hdCgnUG9zaXRpb25VcGRhdGVIeicsIGZhbGxiYWNrPV9QT1NfVVBEQVRFX0ha"
    "KQogICAgX1BPU19VUERBVEVfSFogPSBtaW4obWF4KGh6LCAwLjUpLCBfUE9TX1VQREFURV9IWl9N"
    "QVgpCiAgICBfSURMRV9USU1FT1VUID0gbWF4KDAsIHNlYy5nZXRpbnQoJ0lkbGVUaW1lb3V0Jywg"
    "ZmFsbGJhY2s9X0lETEVfVElNRU9VVCkpCiAgICBfU0VORF9OT1BTID0gc2VjLmdldGJvb2xlYW4o"
    "J0tlZXBhbGl2ZScsIGZhbGxiYWNrPV9TRU5EX05PUFMpCiAgICBfUkVXUklURV9HQU1FX0hPU1Qg"
    "PSBzZWMuZ2V0Ym9vbGVhbignUmV3cml0ZUdhbWVIb3N0JywgZmFsbGJhY2s9X1JFV1JJVEVfR0FN"
    "RV9IT1NUKQogICAgX1BVQkxJQ19IT1NUX0FERFJFU1MgPSBzZWMuZ2V0KCdQdWJsaWNIb3N0QWRk"
    "cmVzcycsIGZhbGxiYWNrPV9QVUJMSUNfSE9TVF9BRERSRVNTKS5zdHJpcCgpCiAgICBfU1RSSVBf"
    "QUxUX0FERFJFU1NFUyA9IHNlYy5nZXRib29sZWFuKCdTdHJpcEFsdEFkZHJlc3NlcycsIGZhbGxi"
    "YWNrPV9TVFJJUF9BTFRfQUREUkVTU0VTKQogICAgX0hFUk9fSURfSEVYID0gc2VjLmdldGJvb2xl"
    "YW4oJ0hlcm9JZEhleCcsIGZhbGxiYWNrPV9IRVJPX0lEX0hFWCkKICAgIF9ERUJVR19MT0dfQ09N"
    "TUFORFMgPSBzZWMuZ2V0Ym9vbGVhbignRGVidWdDb21tYW5kcycsIGZhbGxiYWNrPV9ERUJVR19M"
    "T0dfQ09NTUFORFMpCiAgICBfREVCVUdfTE9HX1ZFUkJPU0UgPSBzZWMuZ2V0Ym9vbGVhbignRGVi"
    "dWdDb21tYW5kc1ZlcmJvc2UnLCBmYWxsYmFjaz1fREVCVUdfTE9HX1ZFUkJPU0UpCkNGRyA9IGxv"
    "YWRDb25maWcoKQphcHBseUNvbmZpZyhDRkcpCgojIyMgVVNFUiBTVFJVQ1RVUkUKIyBjb25uZWN0"
    "aW9uCiMgdXNlcm5hbWUKIyBoZXJvZGF0YQojIHBvc2l0aW9uCiMgZ2FtZWNoYW5uZWwKIyBjaGF0"
    "Y2hhbm5lbAojIGdhbWUKCmNsYXNzIFVzZXIoKTogI1RPRE8gbWVyZ2UgdXNlciBpbnRvIGNvbm5l"
    "Y3Rpb24/LCB2YWxpZGF0aW9uIGNhbiBiZSBhc3N1bWVkIGJ5IHN0YWdlCiAgICBkZWYgX19pbml0"
    "X18oc2VsZiwgbmFtZSwgY29uKToKICAgICAgICBzZWxmLmhlcm9kYXRhID0gYicnCiAgICAgICAg"
    "IycwIzAnLCBub3QgTm9uZTogdGhpcyBnb2VzIHN0cmFpZ2h0IGludG8gdGhlICRnYW1lY2hhbm5l"
    "bHVzZXIgc2VudCB0bwogICAgICAgICNldmVyeSBvdGhlciBjbGllbnQsIGFuZCBhbiB1bnNldCB2"
    "YWx1ZSB1c2VkIHRvIHJlYWNoIHRoZW0gYXMgdGhlCiAgICAgICAgI2xpdGVyYWwgdGV4dCAiTm9u"
    "ZSIgd2hlcmUgY29vcmRpbmF0ZXMgd2VyZSBleHBlY3RlZC4KICAgICAgICBzZWxmLnBvc2RhdGEg"
    "PSAnMCMwJwogICAgICAgIHNlbGYucG9zY2hhbmdlZCA9IEZhbHNlCiAgICAgICAgc2VsZi5yZXF1"
    "ZXN0ZWRDaGFubmVsID0gTm9uZQogICAgICAgIHNlbGYuZ2FtZWNoYW5uZWwgPSBOb25lCiAgICAg"
    "ICAgc2VsZi5jaGF0Y2hhbm5lbCA9IE5vbmUKICAgICAgICBzZWxmLnJlcXVlc3RlZEdhbWUgPSBO"
    "b25lCiAgICAgICAgc2VsZi5nYW1lID0gTm9uZQogICAgICAgIHNlbGYubmFtZSA9IG5hbWUKICAg"
    "ICAgICAjQ2FjaGVkLCBub3QgbG9va2VkIHVwIHBlciBtZXNzYWdlOiB0aGUgZ3VpbGQgbmFtZSBn"
    "b2VzIG91dCBpbiB0aGUKICAgICAgICAjc2Vjb25kIGZpZWxkIG9mIGV2ZXJ5ICRnYW1lY2hhbm5l"
    "bHVzZXIgYW5kICRjaGF0Y2hhbm5lbHVzZXIgLSB0aGUKICAgICAgICAjc2FtZSBmaWVsZCAvd2hv"
    "aXMgcmVwb3J0cyBhcyB0aGUgZ3VpbGQgLSBhbmQgdGhvc2UgYXJlIHNlbnQgZmFyIHRvbwogICAg"
    "ICAgICNvZnRlbiB0byBoaXQgdGhlIGRhdGFiYXNlIGVhY2ggdGltZS4KICAgICAgICBzZWxmLmd1"
    "aWxkID0gc2FuaXRpemVUZXh0KEdESC5nZXRHdWlsZE5hbWUobmFtZSkpCiAgICAgICAgc2VsZi5s"
    "b2dpblRpbWUgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAgICAgIHNlbGYuaWRudW0gPSBH"
    "REguZ2V0VVJhbmRvbSgpCiAgICAgICAgc2VsZi5jb25uZWN0aW9uID0gY29uICNzZXJ2ZXIgPSBj"
    "b24uc2VydmVyCiAgICAgICAgI3NlbGYuY29ubmVjdGlvbi5ndWlkIC0+IGd1aWQgd2hlbiByZWxl"
    "dmFudAogICAgICAgIHNlbGYucGd1aWQgPSBwcmV0dHlfZ3VpZChzZWxmLmNvbm5lY3Rpb24uZ3Vp"
    "ZCkKICAgIGRlZiBsZWF2ZUNoYW5uZWwoc2VsZik6CiAgICAgICAgaWYgc2VsZi5yZXF1ZXN0ZWRD"
    "aGFubmVsOgogICAgICAgICAgICAjbGlzdC5yZW1vdmUoKSByYWlzZXMgVmFsdWVFcnJvciB3aGVu"
    "IHRoZSBlbnRyeSBpcyBhbHJlYWR5IGdvbmU7CiAgICAgICAgICAgICN0aGF0IHVzZWQgdG8gYWJv"
    "cnQgdGhlIHJlc3Qgb2YgdGhlIGRpc2Nvbm5lY3QgY2xlYW51cAogICAgICAgICAgICBpZiBzZWxm"
    "LmNvbm5lY3Rpb24gaW4gc2VsZi5yZXF1ZXN0ZWRDaGFubmVsLnJlcXVlc3RlZDoKICAgICAgICAg"
    "ICAgICAgIHNlbGYucmVxdWVzdGVkQ2hhbm5lbC5yZXF1ZXN0ZWQucmVtb3ZlKHNlbGYuY29ubmVj"
    "dGlvbikKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWRDaGFubmVsID0gTm9uZQogICAgICAgIGlm"
    "IHNlbGYuZ2FtZWNoYW5uZWw6CiAgICAgICAgICAgIHNlbGYuZ2FtZWNoYW5uZWwubGVhdmVDaGFu"
    "bmVsKHNlbGYuY29ubmVjdGlvbikKICAgICAgICAgICAgI2xlYXZlQ2hhbm5lbCBhbHNvIGxlYXZl"
    "cyBjaGF0CiAgICBkZWYgbGVhdmVDaGF0KHNlbGYpOgogICAgICAgIGlmIHNlbGYuY2hhdGNoYW5u"
    "ZWw6CiAgICAgICAgICAgIGlmIHNlbGYuY29ubmVjdGlvbiBpbiBzZWxmLmNoYXRjaGFubmVsOgog"
    "ICAgICAgICAgICAgICAgc2VsZi5jaGF0Y2hhbm5lbC5yZW1vdmUoc2VsZi5jb25uZWN0aW9uKQog"
    "ICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZjaGF0Y2hhbm5lbHVzZXIgIntzZWxmLm5hbWV9"
    "IicpCiAgICAgICAgICAgIHNlbGYuY29ubmVjdGlvbi5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQn"
    "OnNlbGYuY2hhdGNoYW5uZWwsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgICAgICAgICAgc2VsZi5j"
    "aGF0Y2hhbm5lbD1Ob25lCiAgICBkZWYgc3RvcEdhbWUoc2VsZik6CiAgICAgICAgaWYgc2VsZi5y"
    "ZXF1ZXN0ZWRHYW1lOgogICAgICAgICAgICAjQm90aCBndWFyZHMgbWF0dGVyOiB0aGUgY2hhbm5l"
    "bCBtYXkgYWxyZWFkeSBiZSBnb25lIChsZWF2ZUNoYW5uZWwKICAgICAgICAgICAgI2NsZWFycyBp"
    "dCBiZWZvcmUgc3RvcEdhbWUgcnVucyBvbiBzb21lIHBhdGhzKSBhbmQgdGhlIHBlbmRpbmcKICAg"
    "ICAgICAgICAgI3JlcXVlc3QgbWF5IGFscmVhZHkgaGF2ZSBiZWVuIGNvbnN1bWVkIGJ5IGNyZWF0"
    "ZUdhbWUuIEVpdGhlciBvbmUKICAgICAgICAgICAgI3VzZWQgdG8gcmFpc2UgKEF0dHJpYnV0ZUVy"
    "cm9yIG9uIE5vbmUgLyBLZXlFcnJvcikgaW5zaWRlIHRoZQogICAgICAgICAgICAjZGlzY29ubmVj"
    "dCBwYXRoIGFuZCBhYm9ydCB0aGUgcmVzdCBvZiB0aGUgY2xlYW51cCwgbGVha2luZyB0aGUKICAg"
    "ICAgICAgICAgI3BsYXllcidzIGVudHJ5IGluIGFjdGl2ZVVzZXJzLgogICAgICAgICAgICBpZiBz"
    "ZWxmLmdhbWVjaGFubmVsOgogICAgICAgICAgICAgICAgc2VsZi5nYW1lY2hhbm5lbC5nYW1lUmVx"
    "dWVzdHMucG9wKHNlbGYucmVxdWVzdGVkR2FtZSwgTm9uZSkKICAgICAgICAgICAgc2VsZi5yZXF1"
    "ZXN0ZWRHYW1lID0gTm9uZQogICAgICAgIGlmIHNlbGYuZ2FtZToKICAgICAgICAgICAgc2VsZi5n"
    "YW1lLnJlbW92ZShzZWxmLmNvbm5lY3Rpb24pCiAgICBkZWYgZGlzY29ubmVjdChzZWxmLCBzZXJ2"
    "ZXIpOgogICAgICAgIHNlbGYuc3RvcEdhbWUoKQogICAgICAgIHNlbGYubGVhdmVDaGFubmVsKCkK"
    "ICAgICAgICBzZXJ2ZXIuc3RhdGUucmVsZWFzZVVzZXIoc2VsZi5uYW1lLCBzZWxmLmNvbm5lY3Rp"
    "b24pCiAgICAgICAgR0RILnJlbGVhc2VVUmFuZG9tKHNlbGYuaWRudW0pCiAgICBkZWYgd2lyZUlk"
    "KHNlbGYpOgogICAgICAgICNUaGUgb25lIHBsYWNlIHRoZSBoZXJvIGlkIGlzIGZvcm1hdHRlZCwg"
    "c28gJGdhbWVjaGFubmVsdXNlciBhbmQKICAgICAgICAjL3VwZGhlcm9wb3MgY2FuIG5ldmVyIGRp"
    "c2FncmVlIGFnYWluIC0gc2VlIF9IRVJPX0lEX0hFWC4KICAgICAgICByZXR1cm4gZid7c2VsZi5p"
    "ZG51bTp4fScgaWYgX0hFUk9fSURfSEVYIGVsc2UgZid7c2VsZi5pZG51bX0nCiAgICBkZWYgZ2V0"
    "R0NVbXNnKHNlbGYpOgogICAgICAgIGhkbCA9IGxlbihzZWxmLmhlcm9kYXRhKQogICAgICAgIGlm"
    "IGhkbD09MDoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAgIHJldHVybiBfZW0oZickZ2Ft"
    "ZWNoYW5uZWx1c2VyICJ7c2VsZi5uYW1lfSIgIntzZWxmLmd1aWxkfSIgIjEwMCIgIntzZWxmLndp"
    "cmVJZCgpfSIgIjAiICJ7c2VsZi5wZ3VpZH0iICJ7c2VsZi5wb3NkYXRhfSIgIntoZGx9IicpK3Nl"
    "bGYuaGVyb2RhdGEKICAgIGRlZiBnZXRDQ1Vtc2coc2VsZik6CiAgICAgICAgdmIgPSAwICNvciAw"
    "eEZGRkZGRkZGKDQyOTQ5NjcyOTU9IC0xJjMyYml0PykKICAgICAgICByZXR1cm4gX2VtKGYnJGNo"
    "YXRjaGFubmVsdXNlciAie3NlbGYubmFtZX0iICJ7c2VsZi5ndWlsZH0iICJ7dmJ9IiAie3NlbGYu"
    "cGd1aWR9IicpCiAgICAgICAgIyAkY2hhdGNoYW5uZWx1c2VyICJ7bmFtZX0iICIiICIwIiAie2d1"
    "aWR9IgojIGluY3JlYXNpbmcgbWF5IGltcHJvdmUgc2VjdXJpdHkgYXQgdGhlIGNvc3Qgb2YgcGVy"
    "Zm9ybWFuY2UKIyBvbmx5IHVwZGF0ZXMgd2hlbiB1c2VyIGxvZ3MgaW4gYW5kIGlzIHN0b3JlZCBh"
    "bG9uZ3NpZGUgc2FsdCBpbiBkYXRhYmFzZQpfSEFTSElURVIgPSAxMDAwMDAKZGVmIF9zYWx0X2hh"
    "c2hfKHBhc3N3b3JkLCBzYWx0LCBoSXRyKToKICAgICN1dGYtOCwgbm90IGFzY2lpOiBhIHBhc3N3"
    "b3JkIHdpdGggYW4gOC1iaXQgY2hhcmFjdGVyIHVzZWQgdG8gcmFpc2UgaGVyZSBhbmQKICAgICNk"
    "cm9wIHRoZSBjb25uZWN0aW9uIGluc3RlYWQgb2YgbG9nZ2luZyB0aGUgcGxheWVyIGluLiBQdXJl"
    "LWFzY2lpIHBhc3N3b3JkcwogICAgI2VuY29kZSB0byBpZGVudGljYWwgYnl0ZXMgdW5kZXIgYm90"
    "aCwgc28gbm8gc3RvcmVkIGhhc2ggY2hhbmdlcy4KICAgIHJldHVybiBoYXNobGliLnBia2RmMl9o"
    "bWFjKCdzaGEyNTYnLCBwYXNzd29yZC5lbmNvZGUoJ3V0Zi04JyksIHNhbHQsIGhJdHIpCiAgICAK"
    "IyMjIFNRTCBJTkZPCiMgX0RCSU5GTzogVkVSU0lPTiAxCiMgdXNlclRhYmxlCiMgLSByb3dpZCwg"
    "dXNlcm5hbWUsIHBhc3NIYXNoLCBzZXJpYWwsIHVuaXF1ZVNhbHQsIGxhc3RMb2dpbiwgZW1haWws"
    "IGxvY2F0aW9uLCB5ZWFyb2ZiaXJ0aChlc3RpbWF0ZSksIGdlbmRlciwgZGVzY3JpcHRpb24KIyBm"
    "b3JtVGFibGUKIyAtIHJvd2lkLCBmb3JtCiMjIC0tLS0tLS0tLS0tLS0tLS0gIyMKIyBUT0RPIFZF"
    "UlNJT04gMjogZ3VpbGRzLCBsZWFkZXJib2FyZCwgZXRjPwoKI1RPRE8gY29udmVydCBkYXRhYmFz"
    "ZSB0byBzaW5nbGV0aHJlYWQgYWNjZXNzIGZvciBjb21wYXRpYmlsaXR5PyB1bm5lY2Nlc2FyeT8K"
    "I2NsYXNzIERhdGFSZXF1ZXN0KHRocmVhZGluZy5FdmVudCk6CiMgICBkYXRhID0gTm9uZQojICAg"
    "ZGVmIHNldCh2YWwpOgojICAgICAgIHNlbGYuZGF0YT12YWwKIyAgICAgICBzdXBlcigpLnNldCgp"
    "CiMgICBkZWYgd2FpdCgpOgojICAgICAgIHN1cGVyKCkud2FpdCgpCiMgICAgICAgcmV0dXJuIHNl"
    "bGYuZGF0YQojKiBkYXRhYmFzZSB0aHJlYWQ6CiMgICBfZHJRID0gZGF0YSByZXF1ZXN0IHF1ZXVl"
    "LCBwcm9jZXNzZWQgaW4gZGF0YWJhc2UgdGhyZWFkCiMgICBleHRlcm5hbCBmdW5jdGlvbnMgYWRk"
    "IHJlcXVlc3QgZm9yIGludGVybmFsIGZ1bmN0aW9uIGFuZCByZXR1cm4gcmVxdWVzdCB0byBhd2Fp"
    "dAojICAgZHJvYmogaW4gcXVldWUgPSAoZHIsIGZ0YXJnZXQsIChhcmdzKSksIGRyLnNldChmdGFy"
    "Z2V0KCphcmdzKSkKI1RPRE8gb3JnYW5pemUgU1FMIGNvbW1hbmRzPyBtYWtlIGl0IG1vcmUgYmVh"
    "dXRpZnVsPwpfU1FMX2RiSW5mb0V4aXN0cyA9ICdTRUxFQ1QgbmFtZSBGUk9NIHNxbGl0ZV9tYXN0"
    "ZXIgV0hFUkUgbmFtZT0iX0RCSU5GTyInCl9TUUxfZGJWZXJzaW9uID0gJ1NFTEVDVCBWRVJTSU9O"
    "IEZST00gX0RCSU5GTycKX1NRTElOSVRfZGJJbmZvVGFibGUgPSAnQ1JFQVRFIFRBQkxFIF9EQklO"
    "Rk8oVkVSU0lPTiknCl9EQkNVUlZFUiA9IDIKX1NRTElOSVRfZGJJbmZvVmVyc2lvbiA9IGYnSU5T"
    "RVJUIElOVE8gX0RCSU5GTyBWQUxVRVMgKHtfREJDVVJWRVJ9KScKX1NRTFVQRF9kYkluZm9WZXJz"
    "aW9uID0gZidVUERBVEUgX0RCSU5GTyBTRVQgVkVSU0lPTiA9IHtfREJDVVJWRVJ9JwojeW9iID0g"
    "eWVhciBvZiBiaXJ0aCAoZXN0aW1hdGUpCiNnZW5kZXI6IDAgPSBNYWxlCl9TUUxJTklUX2RiVXNl"
    "clRhYmxlID0gJ0NSRUFURSBUQUJMRSB1c2VyVGFibGUodXNlcm5hbWUgVU5JUVVFLCBwYXNzSGFz"
    "aCwgc2VyaWFsLCB1bmlxdWVTYWx0LCBoYXNoSXRlciwgbGFzdExvZ2luIFRJTUVTVEFNUCwgZW1h"
    "aWwsIGxvY2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24pJwpfU1FMSU5JVF9kYkZvcm1U"
    "YWJsZSA9ICdDUkVBVEUgVEFCTEUgZm9ybVRhYmxlKGZvcm0gVU5JUVVFKScgI3VzaW5nIHJvd2lk"
    "IGFzIElECiMtLS0gZ3VpbGRzIChEQiB2ZXJzaW9uIDIpIC0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQojcmFuazogMiA9IGZvdW5kZXIvbGVhZGVyLCAx"
    "ID0gb2ZmaWNlciwgMCA9IG1lbWJlci4gQSBwbGF5ZXIgaXMgaW4gYXQgbW9zdCBvbmUKI2d1aWxk"
    "LCB3aGljaCBpcyB3aGF0IHRoZSBjbGllbnQncyBVSSBhc3N1bWVzICh3aG9pcyBjYXJyaWVzIGEg"
    "c2luZ2xlIG5hbWUpLgojZ3VpbGRrZXkgaXMgZ3VpbGRuYW1lLmNhc2Vmb2xkKCkgYW5kIGlzIHdo"
    "YXQgdW5pcXVlbmVzcyBhbmQgZXZlcnkgbG9va3VwIGdvCiN0aHJvdWdoLiBTUUxpdGUncyBvd24g"
    "Q09MTEFURSBOT0NBU0Ugb25seSBmb2xkcyBBLVosIHNvIG9uIHRoaXMgc2VydmVyIC0KI3doZXJl"
    "IHRoZSBuYW1lcyBhcmUgQ3lyaWxsaWMgLSBpdCB3b3VsZCBoYXZlIGxldCAi0J3QvtGH0L3Ri9C1"
    "INCS0L7Qu9C60LgiIGFuZCAi0L3QvtGH0L3Ri9C1CiPQstC+0LvQutC4IiBjb2V4aXN0IGFzIHR3"
    "byBzZXBhcmF0ZSBndWlsZHMgdGhhdCBwbGF5ZXJzIGNvdWxkIG5vdCB0ZWxsIGFwYXJ0LgpfU1FM"
    "SU5JVF9kYkd1aWxkVGFibGUgPSAnQ1JFQVRFIFRBQkxFIGd1aWxkVGFibGUoZ3VpbGRuYW1lLCBn"
    "dWlsZGtleSBVTklRVUUsIG93bmVyLCBjcmVhdGVkIFRJTUVTVEFNUCwgZGVzY3JpcHRpb24pJwpf"
    "U1FMSU5JVF9kYkd1aWxkTWVtYmVyVGFibGUgPSAnQ1JFQVRFIFRBQkxFIGd1aWxkTWVtYmVyVGFi"
    "bGUoZ3VpbGRuYW1lLCB1c2VybmFtZSBVTklRVUUsIHJhbmspJwpfU1FMX2d1aWxkRXhpc3RzID0g"
    "J1NFTEVDVCBndWlsZG5hbWUgRlJPTSBndWlsZFRhYmxlIFdIRVJFIGd1aWxka2V5ID0gPycKX1NR"
    "TF9jcmVhdGVHdWlsZCA9ICdJTlNFUlQgSU5UTyBndWlsZFRhYmxlIFZBTFVFUyAoPyw/LD8sPyw/"
    "KScKX1NRTF9kZWxldGVHdWlsZCA9ICdERUxFVEUgRlJPTSBndWlsZFRhYmxlIFdIRVJFIGd1aWxk"
    "bmFtZSA9ID8nCl9TUUxfZ3VpbGRPd25lciA9ICdTRUxFQ1Qgb3duZXIgRlJPTSBndWlsZFRhYmxl"
    "IFdIRVJFIGd1aWxkbmFtZSA9ID8nCl9TUUxfYWRkR3VpbGRNZW1iZXIgPSAnSU5TRVJUIE9SIFJF"
    "UExBQ0UgSU5UTyBndWlsZE1lbWJlclRhYmxlIFZBTFVFUyAoPyw/LD8pJwpfU1FMX2RlbEd1aWxk"
    "TWVtYmVyID0gJ0RFTEVURSBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/"
    "JwpfU1FMX2RlbEd1aWxkTWVtYmVycyA9ICdERUxFVEUgRlJPTSBndWlsZE1lbWJlclRhYmxlIFdI"
    "RVJFIGd1aWxkbmFtZSA9ID8nCl9TUUxfZ3VpbGRPZlVzZXIgPSAnU0VMRUNUIGd1aWxkbmFtZSwg"
    "cmFuayBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/JwpfU1FMX2d1aWxk"
    "TWVtYmVycyA9ICdTRUxFQ1QgdXNlcm5hbWUsIHJhbmsgRlJPTSBndWlsZE1lbWJlclRhYmxlIFdI"
    "RVJFIGd1aWxkbmFtZSA9ID8nCl9TUUxfYWxsR3VpbGRzID0gJ1NFTEVDVCBndWlsZG5hbWUgRlJP"
    "TSBndWlsZFRhYmxlIE9SREVSIEJZIGd1aWxkbmFtZSBDT0xMQVRFIE5PQ0FTRScKI1NhbWUgc2hh"
    "cGUgYXMgdGhlIHVzZXJuYW1lIHJ1bGU6IHRoZSBuYW1lIHRyYXZlbHMgaW5zaWRlIHF1b3RlZCBw"
    "cm90b2NvbAojZmllbGRzLCBzbyBhbnl0aGluZyB0aGF0IGNvdWxkIGNsb3NlIGEgcXVvdGUgaXMg"
    "cmVqZWN0ZWQgb3V0cmlnaHQgcmF0aGVyIHRoYW4KI3NpbGVudGx5IHJld3JpdHRlbi4gU3BhY2Vz"
    "IGFyZSBhbGxvd2VkIC0gZ3VpbGQgbmFtZXMgY29tbW9ubHkgaGF2ZSB0aGVtLgpfUkVfVkFMSURf"
    "R1VJTEROQU1FID0gcmUuY29tcGlsZShyJ15bXiJcclxuXDBdezMsMzJ9JCcpCgpfU1FMX3VzZXJJ"
    "RCA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2VyVGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/JwpfU1FM"
    "X3VzZXJJRF9TY2hrID0gJ1NFTEVDVCByb3dpZCBGUk9NIHVzZXJUYWJsZSBXSEVSRSBzZXJpYWwg"
    "PSA/JwpfU1FMX3VzZXJJRF9zdHJpY3QgPSAnU0VMRUNUIHJvd2lkIEZST00gdXNlclRhYmxlIFdI"
    "RVJFIHVzZXJuYW1lID0gPyBBTkQgc2VyaWFsID0gPycKX1NRTF9yZWdpc3RlclVzZXIgPSAnSU5T"
    "RVJUIElOVE8gdXNlclRhYmxlIFZBTFVFUyAoPyw/LD8sPyw/LD8sPyw/LD8sPyw/KScKX1NRTF9k"
    "ZWxldGVVc2VyID0gJ0RFTEVURSBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9T"
    "UUxfZ2V0TG9naW4gPSAnU0VMRUNUIHVzZXJuYW1lLCBwYXNzSGFzaCwgdW5pcXVlU2FsdCwgaGFz"
    "aEl0ZXIgRlJPTSB1c2VyVGFibGUgV0hFUkUgcm93aWQgPSA/JwpfU1FMVVBEX3Bhc3NIYXNoID0g"
    "J1VQREFURSB1c2VyVGFibGUgU0VUIHBhc3NIYXNoID0gPywgaGFzaEl0ZXIgPSA/IFdIRVJFIHJv"
    "d2lkID0gPycKX1NRTF9sb2dpblVwZGF0ZSA9ICdVUERBVEUgdXNlclRhYmxlIFNFVCBsYXN0TG9n"
    "aW4gPSA/IFdIRVJFIHJvd2lkID0gPycKX1NRTF9nZXRXaG9pcyA9ICdTRUxFQ1QgZW1haWwsIGxv"
    "Y2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24gRlJPTSB1c2VyVGFibGUgV0hFUkUgdXNl"
    "cm5hbWUgPSA/JwpfU1FMVVBEX3dob2lzID0gJ1VQREFURSB1c2VyVGFibGUgU0VUIGVtYWlsID0g"
    "PywgbG9jYXRpb24gPSA/LCB5b2IgPSA/LCBnZW5kZXIgPSA/LCBkZXNjcmlwdGlvbiA9ID8gV0hF"
    "UkUgdXNlcm5hbWUgPSA/JwojaWYgZG9lcyBub3QgZXhpc3QsIGdlbmVyYXRlLCBjaGFuZ2UgZm9y"
    "bWF0IGZvciBtb2RwYWNrcwpfU1FMX2Zvcm1JRCA9ICdTRUxFQ1Qgcm93aWQgZnJvbSBmb3JtVGFi"
    "bGUgV0hFUkUgZm9ybSA9ID8nCl9TUUxBRERfZm9ybUlEID0gJ0lOU0VSVCBJTlRPIGZvcm1UYWJs"
    "ZSBWQUxVRVMgKD8pJwpfRk9STV9QREZpbGUgPSAnezp4fV97Onh9LmJpbicgIyBwbGF5ZXJkYXRh"
    "XHVzZXJJRF9mb3JtSUQuYmluCgpkZWYgcmVhZEJpbihmaWxlcGF0aCk6CiAgICB3aXRoIG9wZW4o"
    "ZmlsZXBhdGgsICJyYiIpIGFzIGY6CiAgICAgICAgcmV0dXJuIGYucmVhZCgpCmNsYXNzIERhdGFI"
    "YW5kbGVyKCk6CiAgICBkZWYgX19pbml0X18oc2VsZik6CiAgICAgICAgI2luc3RhbmNlIGF0dHJp"
    "YnV0ZSwgbm90IGEgY2xhc3MgYXR0cmlidXRlIC0gc2FtZSByZWFzb25pbmcgYXMKICAgICAgICAj"
    "R2FtZVN0YXRlLmFjdGl2ZVVzZXJzOiBzaGFyZWQgY2xhc3Mgc3RhdGUgbGVha3MgYmV0d2VlbiBp"
    "bnN0YW5jZXMKICAgICAgICBzZWxmLnVzZWROdW1zID0gc2V0KCkKICAgICAgICAjcHJpbnQoJ3Nx"
    "bGl0ZTMgdGhyZWFkc2FmZXR5Oicsc3FsaXRlMy50aHJlYWRzYWZldHkpCiAgICAgICAgI2lmIHNx"
    "bGl0ZTMudGhyZWFkc2FmZXR5PDM6CiAgICAgICAgIyAgICByYWlzZSBFeGNlcHRpb24oJ011bHRp"
    "VGhyZWFkIHN1cHBvcnQgcmVxdWlyZWQnKQogICAgICAgICNUT0RPIG9yZ2FuaXplIHNpbmdsZSB0"
    "aHJlYWRlZCBkYXRhYmFzZSBhY2Nlc3M/IGV2ZXIgbmVlZGVkPwogICAgICAgIHNlbGYubG9jayA9"
    "IHRocmVhZGluZy5STG9jaygpCiAgICAgICAgb3MubWFrZWRpcnMoX1BBVEhfUExBWUVSREFUQSwg"
    "ZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICBzZWxmLmRiID0gc3FsaXRlMy5jb25uZWN0KF9QQVRIX0RB"
    "VEFCQVNFLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgY2hlY2tfc2FtZV90aHJl"
    "YWQgPSBGYWxzZSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGRldGVjdF90eXBl"
    "cz1zcWxpdGUzLlBBUlNFX0RFQ0xUWVBFUyB8CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICBzcWxpdGUzLlBBUlNFX0NPTE5BTUVTKQogICAgICAgIGluaXRjdXIgPSBzZWxmLmRiLmN1"
    "cnNvcigpCiAgICAgICAgZGJVbmluaXRpYWxpemVkID0gaW5pdGN1ci5leGVjdXRlKF9TUUxfZGJJ"
    "bmZvRXhpc3RzKS5mZXRjaG9uZSgpIGlzIE5vbmUKICAgICAgICBpZiBkYlVuaW5pdGlhbGl6ZWQ6"
    "CiAgICAgICAgICAgIGRiVmVyUmVzID0gMAogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGRiVmVy"
    "UmVzID0gaW5pdGN1ci5leGVjdXRlKF9TUUxfZGJWZXJzaW9uKS5mZXRjaG9uZSgpWzBdCiAgICAg"
    "ICAgc2VsZi51cGRhdGVEQkZyb20oZGJWZXJSZXMpICNlbnN1cmUgREIgaXMgdXBkYXRlZAogICAg"
    "ICAgIAogICAgICAgIGluaXRjdXIuY2xvc2UoKQogICAgZGVmIGdldFVSYW5kb20oc2VsZik6CiAg"
    "ICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJudW0gPSByYW5kb20ucmFuZGludCgx"
    "LDB4ODAwMCkKICAgICAgICAgICAgd2hpbGUgcm51bSBpbiBzZWxmLnVzZWROdW1zOgogICAgICAg"
    "ICAgICAgICAgcm51bSArPSAxI0Vuc3VyZSB1bmlxdWUKICAgICAgICAgICAgc2VsZi51c2VkTnVt"
    "cy5hZGQocm51bSkKICAgICAgICAgICAgcmV0dXJuIHJudW0KICAgIGRlZiByZWxlYXNlVVJhbmRv"
    "bShzZWxmLCBudW0pOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBzZWxmLnVz"
    "ZWROdW1zLmRpc2NhcmQobnVtKSNkaXNjYXJkOiBzYWZlIGV2ZW4gaWYgYWxyZWFkeSByZWxlYXNl"
    "ZAogICAgZGVmIHVwZGF0ZURCRnJvbShzZWxmLCB2ZXJzaW9uKToKICAgICAgICBwcmludCgnRGF0"
    "YWJhc2UgVmVyc2lvbjonLHZlcnNpb24pCiAgICAgICAgaWYgdmVyc2lvbiA+PSBfREJDVVJWRVI6"
    "CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHByaW50KCdVcGRhdGluZyBEYXRhYmFzZSB0byBW"
    "ZXJzaW9uJyxfREJDVVJWRVIpCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHVw"
    "ZGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgaWYgdmVyc2lvbiA9PSAwOgogICAg"
    "ICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJJbmZvVGFibGUpCiAgICAgICAg"
    "ICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkluZm9WZXJzaW9uKQogICAgICAgICAg"
    "ICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJVc2VyVGFibGUpCiAgICAgICAgICAgICAg"
    "ICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkZvcm1UYWJsZSkKICAgICAgICAgICAgaWYgdmVy"
    "c2lvbiA8IDI6CiAgICAgICAgICAgICAgICAjR3VpbGQgc3RvcmFnZS4gQWRkaXRpdmUgb25seSwg"
    "c28gYW4gZXhpc3RpbmcgdjEgZGF0YWJhc2Ugd2l0aAogICAgICAgICAgICAgICAgI3JlYWwgYWNj"
    "b3VudHMgaW4gaXQgdXBncmFkZXMgaW4gcGxhY2UuCiAgICAgICAgICAgICAgICB1cGRjdXIuZXhl"
    "Y3V0ZShfU1FMSU5JVF9kYkd1aWxkVGFibGUpCiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0"
    "ZShfU1FMSU5JVF9kYkd1aWxkTWVtYmVyVGFibGUpCiAgICAgICAgICAgICNUaGUgdmVyc2lvbiBy"
    "b3cgd2FzIG9ubHkgZXZlciB3cml0dGVuIGJ5IHRoZSB2ZXJzaW9uPT0wIGJyYW5jaCwgc28KICAg"
    "ICAgICAgICAgI2V2ZXJ5IGxhdGVyIG1pZ3JhdGlvbiB3b3VsZCBoYXZlIHJlLXJ1biBvbiB0aGUg"
    "bmV4dCBzdGFydC4KICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTFVQRF9kYkluZm9WZXJz"
    "aW9uKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIHVwZGN1ci5jbG9z"
    "ZSgpCiAgICBkZWYgZ2V0UERGTihzZWxmLCBuYW1lLCBmb3JtLCBjcmVhdGUpOgogICAgICAgIHdp"
    "dGggc2VsZi5sb2NrOgogICAgICAgICAgICBmb3JtY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAg"
    "ICAgICAgICB1aWRyZXMgPSBmb3JtY3VyLmV4ZWN1dGUoX1NRTF91c2VySUQsIChuYW1lLCApKS5m"
    "ZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAg"
    "Zm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjVXNlciBkb2Vzbid0"
    "IGV4aXN0CiAgICAgICAgICAgIGZpZHJlcyA9IGZvcm1jdXIuZXhlY3V0ZShfU1FMX2Zvcm1JRCwg"
    "KGZvcm0sICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgZmlkcmVzIGlzIE5vbmU6ICNmb3Jt"
    "YXQgZG9lcyBub3QgZXhpc3QKICAgICAgICAgICAgICAgIGlmIG5vdCBjcmVhdGU6CiAgICAgICAg"
    "ICAgICAgICAgICAgZm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICAgICAgcmV0dXJuIE5v"
    "bmUgI05ldyBmb3JtYXQgbm90IGNyZWF0ZWQKICAgICAgICAgICAgICAgIGZvcm1jdXIuZXhlY3V0"
    "ZShfU1FMQUREX2Zvcm1JRCwgKGZvcm0sICkpCiAgICAgICAgICAgICAgICBzZWxmLmRiLmNvbW1p"
    "dCgpI1RPRE8gQ2hlY2sgaWYgZ290dGEgY29tbWl0IGJlZm9yZSByZWFkLWJhY2s/CiAgICAgICAg"
    "ICAgICAgICBmaWRyZXMgPSBmb3JtY3VyLmV4ZWN1dGUoX1NRTF9mb3JtSUQsIChmb3JtLCApKS5m"
    "ZXRjaG9uZSgpCiAgICAgICAgICAgIGZvcm1jdXIuY2xvc2UoKQogICAgICAgICAgICBmaWQgPSBm"
    "aWRyZXNbMF0KICAgICAgICAgICAgdWlkID0gdWlkcmVzWzBdCiAgICAgICAgICAgIGZpbGVuYW1l"
    "ID0gX0ZPUk1fUERGaWxlLmZvcm1hdCh1aWQsIGZpZCkKICAgICAgICAgICAgZnBhdGggPSBvcy5w"
    "YXRoLmpvaW4oX1BBVEhfUExBWUVSREFUQSwgZmlsZW5hbWUpCiAgICAgICAgICAgIGlmIG9zLnBh"
    "dGguZXhpc3RzKGZwYXRoKSBvciBjcmVhdGU6CiAgICAgICAgICAgICAgICByZXR1cm4gZnBhdGgK"
    "ICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBnZXRQbGF5ZXJEYXRhKHNlbGYsIG5hbWUs"
    "IGZvcm0pOgogICAgICAgIHBhdGggPSBzZWxmLmdldFBERk4obmFtZSwgZm9ybSwgRmFsc2UpCiAg"
    "ICAgICAgaWYgbm90IHBhdGg6CiAgICAgICAgICAgIHJldHVybiBiJycKICAgICAgICByZXR1cm4g"
    "cmVhZEJpbihwYXRoKSNUT0RPIGRlZmF1bHQgdG8gYicnIG9uIGVycm9yPwogICAgZGVmIHNldFBs"
    "YXllckRhdGEoc2VsZiwgbmFtZSwgZm9ybSwgZGF0YSk6CiAgICAgICAgcGF0aCA9IHNlbGYuZ2V0"
    "UERGTihuYW1lLCBmb3JtLCBUcnVlKQogICAgICAgIGlmIG5vdCBwYXRoOiNOTyBGSUxFIFBBVEgs"
    "IFRPRE8gQ0FUQ0ggRVJST1IKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgI1dyaXR0ZW4gdG8g"
    "YSB0ZW1wIGZpbGUgYW5kIG1vdmVkIGludG8gcGxhY2UsIG5vdCB3cml0dGVuIGluIHBsYWNlLgog"
    "ICAgICAgICNUaGUgZ2FtZSBjYWxscyAvc2V0cGxheWVyZGF0YSB0byBhdXRvc2F2ZSBtaWQtc2Vz"
    "c2lvbiwgbm90IG9ubHkgb24gYQogICAgICAgICNjbGVhbiBleGl0IC0gdGhlIGxpdmUgbG9ncyBz"
    "aG93IGl0IGZpcmluZyB3aGlsZSBhIHBsYXllciBpcyB3YWxraW5nCiAgICAgICAgI2Fyb3VuZCwg"
    "d2VsbCBiZWZvcmUgL2xlYXZlZ2FtZS4gYG9wZW4ocGF0aCwnd2InKWAgdHJ1bmNhdGVzIHRoZSBz"
    "YXZlCiAgICAgICAgI3RvIHplcm8gYnl0ZXMgKmJlZm9yZSogd3JpdGluZyBhIHNpbmdsZSBieXRl"
    "IG9mIHRoZSBuZXcgb25lOiBhIGNyYXNoLAogICAgICAgICNhIGtpbGxlZCBwcm9jZXNzIG9yIGEg"
    "bG9zdCBjb25uZWN0aW9uIGF0IGV4YWN0bHkgdGhlIHdyb25nIGluc3RhbnQKICAgICAgICAjbGVm"
    "dCBhIDAtYnl0ZSBvciBoYWxmLXdyaXR0ZW4gc2F2ZSwgYW5kIGdldFBsYXllckRhdGEoKSB0aGVu"
    "IGhhbmRlZAogICAgICAgICN0aGF0IGJhY2sgYXMgInlvdXIgY2hhcmFjdGVyJ3MgZGF0YSIgb24g"
    "dGhlIG5leHQgbG9naW4gLSB0aGlzIGlzCiAgICAgICAgI2FsbW9zdCBjZXJ0YWlubHkgdGhlICJw"
    "cm9ncmVzcyBnZXRzIGxvc3QiIHJlcG9ydC4gb3MucmVwbGFjZSgpIGlzCiAgICAgICAgI2F0b21p"
    "YyBvbiBib3RoIFdpbmRvd3MgYW5kIFBPU0lYOiB0aGUgZmlsZSBvbiBkaXNrIGlzIGVpdGhlciB0"
    "aGUKICAgICAgICAjY29tcGxldGUgb2xkIHNhdmUgb3IgdGhlIGNvbXBsZXRlIG5ldyBvbmUsIG5l"
    "dmVyIGEgcGFydGlhbCB3cml0ZS4KICAgICAgICB0bXAgPSBwYXRoICsgZicue29zLmdldHBpZCgp"
    "fS57dGhyZWFkaW5nLmdldF9pZGVudCgpfS50bXAnCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3"
    "aXRoIG9wZW4odG1wLCAnd2InKSBhcyBmOgogICAgICAgICAgICAgICAgZi53cml0ZShkYXRhKQog"
    "ICAgICAgICAgICAgICAgZi5mbHVzaCgpCiAgICAgICAgICAgICAgICBvcy5mc3luYyhmLmZpbGVu"
    "bygpKQogICAgICAgICAgICBvcy5yZXBsYWNlKHRtcCwgcGF0aCkKICAgICAgICBleGNlcHQgT1NF"
    "cnJvcjoKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgb3MucmVtb3ZlKHRtcCkKICAg"
    "ICAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgICAgICBwYXNzCiAgICAgICAgICAg"
    "IHJhaXNlCiAgICBkZWYgZ2V0V2hvaXMoc2VsZiwgbmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxv"
    "Y2s6CiAgICAgICAgICAgIHdjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHJlcyA9"
    "IHdjdXIuZXhlY3V0ZShfU1FMX2dldFdob2lzLCAobmFtZSwpKS5mZXRjaG9uZSgpCiAgICAgICAg"
    "ICAgIHdjdXIuY2xvc2UoKQogICAgICAgICAgICBpZiByZXMgaXMgTm9uZToKICAgICAgICAgICAg"
    "ICAgIHJldHVybiBOb25lCiAgICAgICAgICAgIChlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVy"
    "LCBkZXNjcmlwdGlvbikgPSByZXMKICAgICAgICAgICAgY3VyWWVhciA9IGRhdGV0aW1lLmRhdGV0"
    "aW1lLm5vdygpLnllYXIKICAgICAgICAgICAgYWdlID0gbWF4KDAsIGN1clllYXIgLSB5b2IpIGlm"
    "IHlvYiBlbHNlIDAKICAgICAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgICAgICdlbWFpbCc6"
    "IGVtYWlsIG9yICcnLAogICAgICAgICAgICAgICAgJ2xvY2F0aW9uJzogbG9jYXRpb24gb3IgJycs"
    "CiAgICAgICAgICAgICAgICAnYWdlJzogYWdlLAogICAgICAgICAgICAgICAgJ2dlbmRlcic6IGdl"
    "bmRlciBpZiBnZW5kZXIgaXMgbm90IE5vbmUgZWxzZSAwLAogICAgICAgICAgICAgICAgJ2Rlc2Ny"
    "aXB0aW9uJzogZGVzY3JpcHRpb24gb3IgJycKICAgICAgICAgICAgfQogICAgZGVmIHVwZGF0ZVdo"
    "b2lzKHNlbGYsIG5hbWUsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9u"
    "KToKICAgICAgICB0cnk6CiAgICAgICAgICAgIGFnZSA9IGludChhZ2UpCiAgICAgICAgZXhjZXB0"
    "IChUeXBlRXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAgICBhZ2UgPSAwCiAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICBnZW5kZXIgPSBpbnQoZ2VuZGVyKQogICAgICAgIGV4Y2VwdCAoVHlwZUVy"
    "cm9yLCBWYWx1ZUVycm9yKToKICAgICAgICAgICAgZ2VuZGVyID0gMAogICAgICAgIHlvYiA9IGRh"
    "dGV0aW1lLmRhdGV0aW1lLm5vdygpLnllYXIgLSBhZ2UKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgd2N1ci5leGVj"
    "dXRlKF9TUUxVUERfd2hvaXMsIChlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVyLCBkZXNjcmlw"
    "dGlvbiwgbmFtZSkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgd2N1"
    "ci5jbG9zZSgpCiAgICAjIyBHVUlMRFMKICAgIGRlZiBnZXRHdWlsZE9mKHNlbGYsIHVzZXJuYW1l"
    "KToKICAgICAgICAjLT4gKGd1aWxkbmFtZSwgcmFuaykgb3IgKE5vbmUsIDApCiAgICAgICAgd2l0"
    "aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAg"
    "ICAgcmVzID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwgKHVzZXJuYW1lLCkpLmZldGNo"
    "b25lKCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICBpZiByZXMgaXMgTm9uZToKICAg"
    "ICAgICAgICAgcmV0dXJuIChOb25lLCAwKQogICAgICAgIHJldHVybiAocmVzWzBdLCByZXNbMV0g"
    "b3IgMCkKICAgIGRlZiBnZXRHdWlsZE5hbWUoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgIHJldHVy"
    "biBzZWxmLmdldEd1aWxkT2YodXNlcm5hbWUpWzBdIG9yICcnCiAgICBkZWYgZ2V0R3VpbGRNZW1i"
    "ZXJzKHNlbGYsIGd1aWxkbmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAg"
    "IGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcmVzID0gY3VyLmV4ZWN1dGUoX1NR"
    "TF9ndWlsZE1lbWJlcnMsIChndWlsZG5hbWUsKSkuZmV0Y2hhbGwoKQogICAgICAgICAgICBjdXIu"
    "Y2xvc2UoKQogICAgICAgIHJldHVybiBbKHJbMF0sIHJbMV0gb3IgMCkgZm9yIHIgaW4gcmVzXQog"
    "ICAgZGVmIGd1aWxkRXhpc3RzKHNlbGYsIGd1aWxkbmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxv"
    "Y2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcm93ID0g"
    "Y3VyLmV4ZWN1dGUoX1NRTF9ndWlsZEV4aXN0cywgKChndWlsZG5hbWUgb3IgJycpLmNhc2Vmb2xk"
    "KCksKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIHJldHVybiBy"
    "b3cgaXMgbm90IE5vbmUKICAgIGRlZiBndWlsZE5hbWVGcmVlKHNlbGYsIGd1aWxkbmFtZSk6CiAg"
    "ICAgICAgI1NhbWUgcnVsZXMgY3JlYXRlR3VpbGQoKSBlbmZvcmNlcywgYXNrZWQgaW4gYWR2YW5j"
    "ZSAtIHRoZSBjbGllbnQKICAgICAgICAjY2hlY2tzIGEgbmFtZSB3aXRoIC90ZXN0Y3JlYXRlZ3Vp"
    "bGQgYmVmb3JlIGl0IHdpbGwgbGV0IHRoZSBwbGF5ZXIKICAgICAgICAjY29uZmlybS4gQW5zd2Vy"
    "aW5nICJmcmVlIiBmb3IgYSBuYW1lIGNyZWF0ZUd1aWxkIHdvdWxkIHRoZW4gcmVqZWN0CiAgICAg"
    "ICAgI3dvdWxkIGp1c3QgbW92ZSB0aGUgZGVhZCBlbmQgb25lIGRpYWxvZyBsYXRlci4KICAgICAg"
    "ICBpZiBub3QgX1JFX1ZBTElEX0dVSUxETkFNRS5tYXRjaChndWlsZG5hbWUgb3IgJycpOgogICAg"
    "ICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICByZXR1cm4gbm90IHNlbGYuZ3VpbGRFeGlzdHMo"
    "Z3VpbGRuYW1lKQogICAgZGVmIGxpc3RHdWlsZHMoc2VsZik6CiAgICAgICAgd2l0aCBzZWxmLmxv"
    "Y2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcm93cyA9"
    "IGN1ci5leGVjdXRlKF9TUUxfYWxsR3VpbGRzKS5mZXRjaGFsbCgpCiAgICAgICAgICAgIGN1ci5j"
    "bG9zZSgpCiAgICAgICAgcmV0dXJuIFtyWzBdIGZvciByIGluIHJvd3NdCiAgICBkZWYgY3JlYXRl"
    "R3VpbGQoc2VsZiwgZ3VpbGRuYW1lLCBvd25lciwgZGVzY3JpcHRpb249JycpOgogICAgICAgICMt"
    "PiBndWlsZG5hbWUgb24gc3VjY2Vzcywgb3IgYW4gZXJyb3IgdG9rZW4gZm9yIHRoZSBjbGllbnQK"
    "ICAgICAgICBpZiBub3QgX1JFX1ZBTElEX0dVSUxETkFNRS5tYXRjaChndWlsZG5hbWUgb3IgJycp"
    "OgogICAgICAgICAgICByZXR1cm4gJ2JhZEd1aWxkTmFtZScKICAgICAgICB3aXRoIHNlbGYubG9j"
    "azoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICBpZiBjdXIu"
    "ZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAob3duZXIsKSkuZmV0Y2hvbmUoKSBpcyBub3QgTm9u"
    "ZToKICAgICAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ2Fs"
    "cmVhZHlJbkd1aWxkJwogICAgICAgICAgICBpZiBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkRXhpc3Rz"
    "LCAoZ3VpbGRuYW1lLmNhc2Vmb2xkKCksKSkuZmV0Y2hvbmUoKSBpcyBub3QgTm9uZToKICAgICAg"
    "ICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ2d1aWxkTmFtZVRh"
    "a2VuJwogICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2NyZWF0ZUd1aWxkLAogICAgICAgICAg"
    "ICAgICAgICAgICAgICAoZ3VpbGRuYW1lLCBndWlsZG5hbWUuY2FzZWZvbGQoKSwgb3duZXIsCiAg"
    "ICAgICAgICAgICAgICAgICAgICAgICBkYXRldGltZS5kYXRldGltZS5ub3coKSwgc2FuaXRpemVU"
    "ZXh0KGRlc2NyaXB0aW9uKSkpCiAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfYWRkR3VpbGRN"
    "ZW1iZXIsIChndWlsZG5hbWUsIG93bmVyLCAyKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQo"
    "KQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgam9p"
    "bkd1aWxkKHNlbGYsIGd1aWxkbmFtZSwgdXNlcm5hbWUpOgogICAgICAgIHdpdGggc2VsZi5sb2Nr"
    "OgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHJvdyA9IGN1"
    "ci5leGVjdXRlKF9TUUxfZ3VpbGRFeGlzdHMsICgoZ3VpbGRuYW1lIG9yICcnKS5jYXNlZm9sZCgp"
    "LCkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgcm93IGlzIE5vbmU6CiAgICAgICAgICAgICAg"
    "ICBjdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuICd1bmtub3duR3VpbGQnCiAgICAg"
    "ICAgICAgICNTdG9yZSB0aGUgZ3VpbGQncyBvd24gc3BlbGxpbmcsIG5vdCB3aGF0ZXZlciBjYXNl"
    "IHRoZSBjbGllbnQgdHlwZWQKICAgICAgICAgICAgI2ludG8gdGhlIGpvaW4gYm94LCBzbyBnZXRH"
    "dWlsZE1lbWJlcnMoKSBmaW5kcyB0aGUgbWVtYmVyIGJhY2suCiAgICAgICAgICAgIGd1aWxkbmFt"
    "ZSA9IHJvd1swXQogICAgICAgICAgICBpZiBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAo"
    "dXNlcm5hbWUsKSkuZmV0Y2hvbmUoKSBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgIGN1ci5j"
    "bG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ2FscmVhZHlJbkd1aWxkJwogICAgICAgICAg"
    "ICBjdXIuZXhlY3V0ZShfU1FMX2FkZEd1aWxkTWVtYmVyLCAoZ3VpbGRuYW1lLCB1c2VybmFtZSwg"
    "MCkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkK"
    "ICAgICAgICByZXR1cm4gTm9uZQogICAgZGVmIGxlYXZlR3VpbGQoc2VsZiwgdXNlcm5hbWUpOgog"
    "ICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigp"
    "CiAgICAgICAgICAgIHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRPZlVzZXIsICh1c2VybmFt"
    "ZSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAgICAgICAgICAg"
    "ICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnbm90SW5HdWlsZCcKICAgICAg"
    "ICAgICAgKGd1aWxkbmFtZSwgcmFuaykgPSAocmVzWzBdLCByZXNbMV0gb3IgMCkKICAgICAgICAg"
    "ICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxHdWlsZE1lbWJlciwgKHVzZXJuYW1lLCkpCiAgICAgICAg"
    "ICAgIG93bmVyID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE93bmVyLCAoZ3VpbGRuYW1lLCkpLmZl"
    "dGNob25lKCkKICAgICAgICAgICAgaWYgb3duZXIgYW5kIG93bmVyWzBdID09IHVzZXJuYW1lOgog"
    "ICAgICAgICAgICAgICAgI1RoZSBmb3VuZGVyIGxlYXZpbmcgZGlzc29sdmVzIHRoZSBndWlsZCBy"
    "YXRoZXIgdGhhbiBsZWF2aW5nIGFuCiAgICAgICAgICAgICAgICAjb3duZXJsZXNzIHJlY29yZCB0"
    "aGF0IG5vYm9keSBjYW4gZXZlciBhZG1pbmlzdGVyLgogICAgICAgICAgICAgICAgY3VyLmV4ZWN1"
    "dGUoX1NRTF9kZWxHdWlsZE1lbWJlcnMsIChndWlsZG5hbWUsKSkKICAgICAgICAgICAgICAgIGN1"
    "ci5leGVjdXRlKF9TUUxfZGVsZXRlR3VpbGQsIChndWlsZG5hbWUsKSkKICAgICAgICAgICAgc2Vs"
    "Zi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIHJldHVybiBOb25l"
    "CiAgICBkZWYgbG9naW5QbGF5ZXIoc2VsZiwgdXNlcm5hbWUsIGNvbiwgcGFzc3dvcmQpOiNUT0RP"
    "IHNob3VsZCByZXR1cm4gZXJyb3IgcHJvcGVybHkgdG8gY2xpZW50CiAgICAgICAgaWYgbm90IF9S"
    "RV9WQUxJRF9VU0VSTkFNRS5tYXRjaCh1c2VybmFtZSk6CiAgICAgICAgICAgICNSZWdpc3RyYXRp"
    "b24gaGFzIGFsd2F5cyB2YWxpZGF0ZWQgdGhlIG5hbWU7IGxvZ2dpbmcgaW4gZGlkIG5vdC4KICAg"
    "ICAgICAgICAgI05hbWVzIHJlYWNoIG90aGVyIGNsaWVudHMgaW5zaWRlIHF1b3RlZCBwcm90b2Nv"
    "bCBmaWVsZHMsIHNvIGEgbmFtZQogICAgICAgICAgICAjY29udGFpbmluZyAnIicgZm9yZ2VzIGNv"
    "bW1hbmRzIC0gYW5kIHRoZSBBbGxvd0FueUxvZ2luIGRlYnVnIHBhdGgKICAgICAgICAgICAgI2Jl"
    "bG93IG5ldmVyIHRvdWNoZXMgdGhlIGRhdGFiYXNlLCB3aGljaCBtYWRlIGl0IHRoZSBvbmUgd2F5"
    "IHRvIGdldAogICAgICAgICAgICAjc3VjaCBhIG5hbWUgaW4uIENoZWNrIGhlcmUgc28gYm90aCBw"
    "YXRocyBhcmUgY292ZXJlZC4KICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBpZiBfREVC"
    "VUdfQUxMT1dfQU5ZX0xPR0lOOiAjREVCVUcgQVVUTyBBTExPVwogICAgICAgICAgICByZXR1cm4g"
    "VXNlcih1c2VybmFtZSwgY29uKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBs"
    "b2dpbkN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgI0RlZmF1bHQgdG8gU1RSSUNU"
    "LCBUT0RPIGFsbG93IGZvciBub24tc3RyaWN0PwogICAgICAgICAgICB1aWRyZXMgPSBsb2dpbkN1"
    "ci5leGVjdXRlKF9TUUxfdXNlcklEX3N0cmljdCwgKHVzZXJuYW1lLCBjb24uU0spKS5mZXRjaG9u"
    "ZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgI3ByaW50"
    "KCdsb2dpbiBlcnJvcjogbm8gdXNlciB3aXRoIHRoYXQgc2VyaWFsIGtleScpCiAgICAgICAgICAg"
    "ICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjTm8gc3Vj"
    "aCBVc2VyCiAgICAgICAgICAgIHVpZCA9IHVpZHJlc1swXQogICAgICAgICAgICAoclVzZXIsIHBh"
    "c3NoYXNoLCB1U2FsdCwgaEl0cikgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfZ2V0TG9naW4sICh1"
    "aWQsICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdXNlcm5hbWUgIT0gclVzZXI6CiAgICAg"
    "ICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJvcjogd3JvbmcgdXNlcm5hbWU6IHt1c2VybmFt"
    "ZX0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0"
    "dXJuIE5vbmUgI1dyb25nIFVzZXJuYW1lCiAgICAgICAgICAgIHRwYXMgPSBfc2FsdF9oYXNoXyhw"
    "YXNzd29yZCwgdVNhbHQsIGhJdHIpCiAgICAgICAgICAgIGlmIHRwYXMgIT0gcGFzc2hhc2g6CiAg"
    "ICAgICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJvcjogd3JvbmcgcGFzc3dvcmQ6IHtwYXNz"
    "d29yZH0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAg"
    "cmV0dXJuIE5vbmUgI1dyb25nIFBhc3N3b3JkCiAgICAgICAgICAgIGlmIGhJdHIgIT0gX0hBU0hJ"
    "VEVSOgogICAgICAgICAgICAgICAgbnBzaCA9IF9zYWx0X2hhc2hfKHBhc3N3b3JkLCB1U2FsdCwg"
    "X0hBU0hJVEVSKQogICAgICAgICAgICAgICAgbG9naW5DdXIuZXhlY3V0ZShfU1FMVVBEX3Bhc3NI"
    "YXNoLCAobnBzaCwgX0hBU0hJVEVSLCB1aWQpKQogICAgICAgICAgICB1c2Vyb2JqID0gVXNlcih1"
    "c2VybmFtZSwgY29uKQogICAgICAgICAgICAjdXBkYXRlIGxhc3QgbG9naW4KICAgICAgICAgICAg"
    "bG9naW5DdXIuZXhlY3V0ZShfU1FMX2xvZ2luVXBkYXRlLCAodXNlcm9iai5sb2dpblRpbWUsIHVp"
    "ZCkpCiAgICAgICAgICAgICNUT0RPIGRlZmF1bHQgZGF0ZXRpbWUgYWRhcHRlciBkZXByZWNhdGVk"
    "LCBjaGVjayByZXBsYWNlbWVudAogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAg"
    "ICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgcmV0dXJuIHVzZXJvYmoKICAgIGRlZiBy"
    "ZWdpc3RlclBsYXllcihzZWxmLCB1c2VybmFtZSwgY29uLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0"
    "aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgogICAgICAgIGlmIG5vdCBfUkVfVkFMSURf"
    "VVNFUk5BTUUubWF0Y2godXNlcm5hbWUpOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjSW52YWxp"
    "ZCB1c2VybmFtZSAoYmFkIGNoYXJzL2xlbmd0aCksIGFsc28gYmxvY2tzIHByb3RvY29sLWluamVj"
    "dGlvbiB2aWEgJyInCiAgICAgICAgZW1haWwgPSBzYW5pdGl6ZVRleHQoZW1haWwpCiAgICAgICAg"
    "bG9jYXRpb24gPSBzYW5pdGl6ZVRleHQobG9jYXRpb24pCiAgICAgICAgZGVzY3JpcHRpb24gPSBz"
    "YW5pdGl6ZVRleHQoZGVzY3JpcHRpb24pCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAg"
    "ICAgIGxvZ2luQ3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBsb2dp"
    "bkN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5hbWUsICkpLmZldGNob25lKCkKICAgICAg"
    "ICAgICAgaWYgdWlkcmVzIGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAgI3ByaW50KGYncmVn"
    "aXN0ZXIgZXJyb3I6IHVzZXJuYW1lIGFscmVhZHkgaW4gdXNlOiB7dXNlcm5hbWV9JykKICAgICAg"
    "ICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNV"
    "c2VyIGV4aXN0cwogICAgICAgICAgICAjaWYgc3RyaWN0LCBjaGVjayBpZiBzZXJpYWwgaXMgaW4g"
    "dXNlIHRvbwogICAgICAgICAgICAjVE9ETyBvbmx5IGFwcGx5IGlmIHN0cmljdAogICAgICAgICAg"
    "ICB1aWRyZXMgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfdXNlcklEX1NjaGssIChjb24uU0ssICkp"
    "LmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIG5vdCBOb25lOgogICAgICAgICAg"
    "ICAgICAgI3ByaW50KCdyZWdpc3RlciBlcnJvcjogc2VyaWFsIGFscmVhZHkgaW4gdXNlJykKICAg"
    "ICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25l"
    "ICNTZXJpYWwgaW4gdXNlIGV4aXN0cwogICAgICAgICAgICB1U2FsdCA9IG9zLnVyYW5kb20oMTYp"
    "CiAgICAgICAgICAgIHBIYXNoID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHVTYWx0LCBfSEFTSElU"
    "RVIpCiAgICAgICAgICAgIGN1cnRpbWUgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAgICAg"
    "ICAgICB0cnk6I3RyeSBzaG91bGRuJ3QgYmUgbmVlZGVkIGFzIGVtcHR5IGZpZWxkIGlzIHNldCB0"
    "byAyNTUKICAgICAgICAgICAgICAgIGFnZSA9IGludChhZ2UpCiAgICAgICAgICAgIGV4Y2VwdDoK"
    "ICAgICAgICAgICAgICAgIGFnZSA9IDAKICAgICAgICAgICAgeW9iID0gY3VydGltZS55ZWFyIC0g"
    "YWdlCiAgICAgICAgICAgIHJlZ3ZhbHMgPSAoCiAgICAgICAgICAgICAgICB1c2VybmFtZSxwSGFz"
    "aCwKICAgICAgICAgICAgICAgIGNvbi5TSyx1U2FsdCxfSEFTSElURVIsCiAgICAgICAgICAgICAg"
    "ICBjdXJ0aW1lLGVtYWlsLGxvY2F0aW9uLHlvYixnZW5kZXIsZGVzY3JpcHRpb24KICAgICAgICAg"
    "ICAgKQogICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxfcmVnaXN0ZXJVc2VyLCByZWd2"
    "YWxzKQogICAgICAgICAgICAjVE9ETyBkZWZhdWx0IGRhdGV0aW1lIGFkYXB0ZXIgZGVwcmVjYXRl"
    "ZCwgY2hlY2sgcmVwbGFjZW1lbnQKICAgICAgICAgICAgdXNlcm9iaiA9IFVzZXIodXNlcm5hbWUs"
    "IGNvbikKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBsb2dpbkN1ci5j"
    "bG9zZSgpCiAgICAgICAgICAgIHJldHVybiB1c2Vyb2JqCiAgICBkZWYgZGVsZXRlQWNjb3VudChz"
    "ZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbiAoR1VJICLQo9C00LDQ"
    "u9C40YLRjCDQv9C10YDRgdC+0L3QsNC20LAiKTogcGVybWFuZW50bHkgcmVtb3ZlcyBhbgogICAg"
    "ICAgICNhY2NvdW50IGFuZCBldmVyeSBzYXZlZCBwbGF5ZXJkYXRhIGJsb2IgZm9yIGl0LiBJcnJl"
    "dmVyc2libGUgLSB0aGUKICAgICAgICAjR1VJIGlzIGV4cGVjdGVkIHRvIGNvbmZpcm0gd2l0aCB0"
    "aGUgYWRtaW4gYmVmb3JlIGNhbGxpbmcgdGhpcy4KICAgICAgICAjRG9lcyBOT1QgdG91Y2ggdGhl"
    "IGNhbGxlcidzIGxpdmUgY29ubmVjdGlvbi9zZXNzaW9uOyB0aGUgY2FsbGVyIGlzCiAgICAgICAg"
    "I3Jlc3BvbnNpYmxlIGZvciBraWNraW5nIGZpcnN0IGlmIHRoZSBhY2NvdW50IGlzIGN1cnJlbnRs"
    "eSBvbmxpbmUKICAgICAgICAjKHNlZSBDb3JlU2VydmVyLmRlbGV0ZUFjY291bnQpLCBvdGhlcndp"
    "c2UgYSBjb25uZWN0ZWQgY2xpZW50IHdvdWxkCiAgICAgICAgI2tlZXAgcGxheWluZyB3aXRoIGFu"
    "IGFjY291bnQgdGhhdCBubyBsb25nZXIgZXhpc3RzIGluIHRoZSBEQi4KICAgICAgICB3aXRoIHNl"
    "bGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB1"
    "aWRyZXMgPSBjdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKHVzZXJuYW1lLCApKS5mZXRjaG9uZSgp"
    "CiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3Nl"
    "KCkKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgICAgICB1aWQgPSB1aWRyZXNb"
    "MF0KICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxldGVVc2VyLCAodXNlcm5hbWUsICkp"
    "CiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAg"
    "ICAgICAjR3VpbGQgbWVtYmVyc2hpcCBvdXRsaXZlcyB0aGUgdXNlclRhYmxlIHJvdyBvdGhlcndp"
    "c2UsIHNvIHRoZSBkZWxldGVkCiAgICAgICAgI25hbWUgd291bGQga2VlcCBzaG93aW5nIHVwIGlu"
    "IGl0cyBndWlsZCdzIHJvc3RlciBmb3JldmVyLgogICAgICAgIHNlbGYubGVhdmVHdWlsZCh1c2Vy"
    "bmFtZSkKICAgICAgICAjUGxheWVyZGF0YSBmaWxlcyAoInt1c2VySUQ6eH1fe2Zvcm1JRDp4fS5i"
    "aW4iKSBsaXZlIG91dHNpZGUgdGhlIERCCiAgICAgICAgI3RyYW5zYWN0aW9uIGFuZCBhcmUgbG9v"
    "a2VkIHVwIGJ5IHByZWZpeCAtIGJlc3QgZWZmb3J0LCBhIGxlZnRvdmVyCiAgICAgICAgI2ZpbGUg"
    "aGVyZSBpc24ndCB3b3J0aCBmYWlsaW5nIHRoZSB3aG9sZSBkZWxldGlvbiBvdmVyLgogICAgICAg"
    "IHByZWZpeCA9IGYne3VpZDp4fV8nCiAgICAgICAgdHJ5OgogICAgICAgICAgICBmb3IgZm4gaW4g"
    "b3MubGlzdGRpcihfUEFUSF9QTEFZRVJEQVRBKToKICAgICAgICAgICAgICAgIGlmIGZuLnN0YXJ0"
    "c3dpdGgocHJlZml4KToKICAgICAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAg"
    "ICAgICAgIG9zLnJlbW92ZShvcy5wYXRoLmpvaW4oX1BBVEhfUExBWUVSREFUQSwgZm4pKQogICAg"
    "ICAgICAgICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICAgICAgICAgICAgICBw"
    "YXNzCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MKICAgICAgICByZXR1"
    "cm4gVHJ1ZQpHREggPSBEYXRhSGFuZGxlcigpCgpkZWYgX3dvVXNlcih1bCwgdXNyKToKICAgIHJl"
    "dHVybiBsaXN0KCAoYSBmb3IgYSBpbiB1bCBpZiBhIGlzIG5vdCB1c3IpICkKZGVmIF9SZWFkQmxv"
    "Yihjb24sIHNpemUpOgogICAgI3NpemUgY29tZXMgc3RyYWlnaHQgb2ZmIHRoZSB3aXJlLCBzbyBp"
    "dCBpcyBuZWl0aGVyIHRydXN0ZWQgdG8gYmUgYSBudW1iZXIKICAgICNub3IgdG8gYmUgc2FuZTog"
    "YSBjbGllbnQgY2xhaW1pbmcgYSBodWdlIGxlbmd0aCB1c2VkIHRvIG1ha2UgdGhlIHNlcnZlcgog"
    "ICAgI2J1ZmZlciB1bmJvdW5kZWRseSAobWVtb3J5IGV4aGF1c3Rpb24pLCBhbmQgYSBjbGllbnQg"
    "dGhhdCBkaXNjb25uZWN0ZWQKICAgICNtaWQtYmxvYiBtYWRlIHJlY3YoKSByZXR1cm4gYicnIGZv"
    "cmV2ZXIgLSBhIDEwMCUgQ1BVIGJ1c3ktbG9vcCwgdGhlIHNhbWUKICAgICNkZWZlY3QgYWxyZWFk"
    "eSBmaXhlZCBpbiBDb25uZWN0aW9uSGFuZGxlci5fcmVjdk1vcmUoKS4KICAgIHRyeToKICAgICAg"
    "ICBzaXplID0gaW50KHNpemUpCiAgICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAg"
    "ICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2JhZCBibG9iIHNpemUge3NpemUhcn0nKQogICAg"
    "aWYgc2l6ZSA8IDAgb3Igc2l6ZSA+IF9NQVhfQkxPQjoKICAgICAgICByYWlzZSBQcm90b2NvbEVy"
    "cm9yKGYnYmxvYiBzaXplIHtzaXplfSBvdXQgb2YgcmFuZ2UgKG1heCB7X01BWF9CTE9CfSknKQog"
    "ICAgI0EgYmxvYiByZWFkIGJsb2NrcyB0aGlzIGNvbm5lY3Rpb24ncyBlbnRpcmUgaGFuZGxlciB0"
    "aHJlYWQuIEFubm91bmNpbmcgYQogICAgI2xlbmd0aCBhbmQgdGhlbiBnb2luZyBxdWlldCAtIGEg"
    "d2VkZ2VkIGNsaWVudCwgYSBsaW5rIHRoYXQgZHJvcHBlZAogICAgI3dpdGhvdXQgYSByZXNldCAt"
    "IHVzZWQgdG8gYmxvY2sgaXQgZm9yZXZlcjogdGhlIHRocmVhZCBuZXZlciByZXR1cm5lZCwgc28K"
    "ICAgICN0aGUgcGxheWVyJ3MgYWNjb3VudCBzdGF5ZWQgY2xhaW1lZCBhbmQgYW55IHJvb20gdGhl"
    "eSBob3N0ZWQgc3RheWVkCiAgICAjbGlzdGVkIHdpdGggbm90aGluZyBiZWhpbmQgaXQuIFRoZSBp"
    "ZGxlIHRpbWVvdXQgbmV2ZXIgYXBwbGllZCBoZXJlLAogICAgI2JlY2F1c2UgaXQgaXMgb25seSBj"
    "b25zdWx0ZWQgYnkgdGhlIHJlYWQgbG9vcCB0aGlzIGNhbGwgaGFzIHN0ZXBwZWQgb3V0CiAgICAj"
    "b2YuCiAgICBkZWFkbGluZSA9IHRpbWUubW9ub3RvbmljKCkgKyBfQkxPQl9USU1FT1VUCiAgICB3"
    "aGlsZSBsZW4oY29uLmRhdGEpIDwgc2l6ZToKICAgICAgICByZW1haW5pbmcgPSBkZWFkbGluZSAt"
    "IHRpbWUubW9ub3RvbmljKCkKICAgICAgICBpZiByZW1haW5pbmcgPD0gMDoKICAgICAgICAgICAg"
    "cmFpc2UgUHJvdG9jb2xFcnJvcihmJ2Jsb2Igb2Yge3NpemV9IGJ5dGVzIG5vdCBkZWxpdmVyZWQg"
    "d2l0aGluICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmJ3tfQkxPQl9USU1FT1VU"
    "fXMgKHtsZW4oY29uLmRhdGEpfSByZWNlaXZlZCknKQogICAgICAgIGNvbi5yZXF1ZXN0LnNldHRp"
    "bWVvdXQocmVtYWluaW5nKQogICAgICAgIHRyeToKICAgICAgICAgICAgY2h1bmsgPSBjb24ucmVx"
    "dWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBleGNlcHQgVGltZW91dEVycm9yOgogICAg"
    "ICAgICAgICBjb250aW51ZSAjZGVhZGxpbmUgaXMgcmUtY2hlY2tlZCBhdCB0aGUgdG9wIG9mIHRo"
    "ZSBsb29wCiAgICAgICAgaWYgbm90IGNodW5rOgogICAgICAgICAgICByYWlzZSBDb25uZWN0aW9u"
    "UmVzZXRFcnJvcignZGlzY29ubmVjdGVkIGR1cmluZyBibG9iIHJlYWQnKQogICAgICAgIGNvbi5k"
    "YXRhICs9IGNodW5rCiAgICBibGJ1ZiA9IGNvbi5kYXRhWzA6c2l6ZV0KICAgIGNvbi5kYXRhID0g"
    "Y29uLmRhdGFbc2l6ZTpdCiAgICByZXR1cm4gYmxidWYKCiNDb21tYW5kIGZ1bmN0aW9ucwpkZWYg"
    "X25vcChtZCx1c3IscmVzKToKICAgIHJldHVybiBOb25lCmRlZiBfdXBkaGVyb3BvcyhtZCx1c3Is"
    "cmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9u"
    "ZSAjbm90IGluIGEgZ2FtZSBjaGFubmVsLCBpZ25vcmUKICAgICMgInh4eHgjeXl5eSIgcmVzcCAi"
    "VUlEI3h4eHgjeXl5eSIgLSB0aGUgY2xpZW50IHNlbmRzIGVpdGhlciBmb3JtLCBidXQKICAgICMg"
    "dXBkYXRlUG9zKCkgdW5jb25kaXRpb25hbGx5IHByZWZpeGVzIHRoZSBzZW5kZXIncyBpZCB3aGVu"
    "IGl0IGZhbnMgdGhlCiAgICAjIHBvc2l0aW9uIG91dC4gU3RvcmluZyB0aGUgcmF3IGZpZWxkIG1l"
    "YW50IHRoZSBzZWNvbmQgZm9ybSB3ZW50IGJhY2sgb3V0CiAgICAjIGFzICJVSUQjVUlEI3h4eHgj"
    "eXl5eSIsIHdoaWNoIG5vIGNsaWVudCBjYW4gbWF0Y2ggdG8gYSBwbGF5ZXI6IHRoYXQKICAgICMg"
    "aGVybydzIG1hcmtlciB0aGVuIHN0YXllZCB3aGVyZXZlciBpdCB3YXMgbGFzdCBzdWNjZXNzZnVs"
    "bHkgcGFyc2VkIHdoaWxlCiAgICAjIHRoZSBwbGF5ZXIgYWN0dWFsbHkgd2Fsa2VkIGF3YXkuIEtl"
    "ZXAgb25seSB0aGUgdHJhaWxpbmcgY29vcmRpbmF0ZSBwYWlyCiAgICAjIHNvIGV4YWN0bHkgb25l"
    "IGlkIGlzIHByZXNlbnQgb24gdGhlIHdpcmUgcmVnYXJkbGVzcyBvZiB3aGF0IHdhcyBzZW50Lgog"
    "ICAgdXNyLnVzZXIucG9zZGF0YSA9ICcjJy5qb2luKHJlc1sxXS5zcGxpdCgnIycpWy0yOl0pCiAg"
    "ICB1c3IudXNlci5nYW1lY2hhbm5lbC5kaXJ0eSA9IFRydWUKICAgIHVzci51c2VyLnBvc2NoYW5n"
    "ZWQgPSBUcnVlCiAgICByZXR1cm4gTm9uZSAjbm8gcmVzcG9uc2UKZGVmIF9zZXRwbGF5ZXJkYXRh"
    "KG1kLHVzcixyZXMpOgogICAgcGQgPSBfUmVhZEJsb2IodXNyLCByZXNbM10pCiAgICAjVE9ETyBD"
    "SEVDSyBwZXJtaXNzaW9ucyBmb3Igc2V0RGF0YShzZWxmIG9yIG90aGVyKQogICAgaWYgcmVzWzFd"
    "ID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgR0RILnNldFBsYXllckRhdGEocmVzWzFdLCByZXNb"
    "Ml0sIHBkKQogICAgI1RPRE8gaGFuZGxlIHJlbWFpbmluZyB2YWx1ZXMKICAgICNyZXNbeF06CiAg"
    "ICAjMDogL3NldHBsYXllcmRhdGEKICAgICMxOiBuYW1lCiAgICAjMjogZm9ybQogICAgIzM6IGJs"
    "b2JzaXplCiAgICAjNDogdW5rbm93biAocG9pbnRzPykKICAgICM1OiB1bmtub3duLCAxIChib29s"
    "PykKICAgIHJldHVybiBOb25lCmRlZiBfZ2V0cGxheWVyZGF0YShtZCx1c3IscmVzKToKICAgICNU"
    "T0RPIGNoZWNrIHBlcm1pc3Npb24gZm9yIGdldERhdGEoc2VsZiBvciBvdGhlcikKICAgIGlmIHJl"
    "c1sxXSA9PSB1c3IudXNlci5uYW1lOgogICAgICAgIHBkID0gR0RILmdldFBsYXllckRhdGEocmVz"
    "WzFdLCByZXNbMl0pCiAgICAgICAgI3ByaW50KCdPYnRhaW5lZCBQbGF5ZXJkYXRhJywgbGVuKHBk"
    "KSkKICAgICAgICByZXR1cm4gX2VtKGYnL2dldHBsYXllcmRhdGEgIntyZXNbMV19IiAie3Jlc1sy"
    "XX0iIHtsZW4ocGQpfScpK3BkCiAgICAjcHJpbnQoJ0FjY2VzcyBFcnJvcicsdXNyLnVzZXIubmFt"
    "ZSwgJ0NhblwndCBnZXQgcGxheWVyZGF0YSBmb3InLHJlc1sxXSkKICAgIHJldHVybiBOb25lCmRl"
    "ZiBfbGVhdmVnYW1lY2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwgPSB1c3IudXNlci5nYW1l"
    "Y2hhbm5lbAogICAgaWYgY2hubDoKICAgICAgICBjaG5sLmxlYXZlQ2hhbm5lbCh1c3IpCiAgICBy"
    "ZXR1cm4gdXNyLnNlcnZlci5zdGF0ZS5lbnVtZXJhdGVHQygpCmRlZiBfcmVxdWVzdGpvaW5nYW1l"
    "Y2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwgPSB1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFu"
    "bmVscy5nZXQocmVzWzFdKQogICAgaWYgY2hubCBpcyBOb25lOgogICAgICAgIHJldHVybiBfZW0o"
    "ZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0iICIwIicpICN1bmtub3duIGNoYW5u"
    "ZWwKICAgICNUT0RPIGNoZWNrIHBlcm1pc3Npb25zPwogICAgaWYgY2hubC5yZXF1ZXN0Sm9pbih1"
    "c3IpOgogICAgICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sx"
    "XX0iICIxIicpCiAgICByZXR1cm4gX2VtKGYnL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgIntyZXNb"
    "MV19IiAiMCInKQpkZWYgX2pvaW5nYW1lY2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwgPSB1"
    "c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy5nZXQocmVzWzFdKQogICAgaWYgY2hubCBpcyBO"
    "b25lOgogICAgICAgIHJldHVybiBOb25lICN1bmtub3duIGNoYW5uZWwsIGlnbm9yZQogICAgaWYg"
    "bGVuKHJlcyk+MjoKICAgICAgICB1c3IudXNlci5wb3NkYXRhID0gJyMnLmpvaW4ocmVzWzJdLnNw"
    "bGl0KCcjJylbLTI6XSkKICAgIHJldHVybiBjaG5sLmpvaW5DaGFubmVsKHVzciwgcmVzWzFdKQpk"
    "ZWYgX3NldHVzZXJoZXJvZGF0YShtZCx1c3IscmVzKToKICAgIHBkID0gX1JlYWRCbG9iKHVzciwg"
    "cmVzWzJdKQogICAgdXNyLnVzZXIuaGVyb2RhdGEgPSBwZAogICAgaWYgdXNyLnVzZXIuZ2FtZWNo"
    "YW5uZWw6CiAgICAgICAgbXNnID0gdXNyLnVzZXIuZ2V0R0NVbXNnKCkKICAgICAgICB0ZyA9IF93"
    "b1VzZXIodXNyLnVzZXIuZ2FtZWNoYW5uZWwudXNlcmxpc3QsIHVzcikKICAgICAgICBtZC5hZGQo"
    "eyd0YXJnZXQnOnRnLCdtZXNzYWdlJzptc2d9KQogICAgcmV0dXJuIE5vbmUKZGVmIF9zZW5kKG1k"
    "LHVzcixyZXMpOgogICAgI1RPRE8gY29uc2lkZXIgc3BlY2lhbCBjaGF0IGNvbW1hbmRzIGhlcmUK"
    "ICAgIGlmIG5vdCB1c3IudXNlci5jaGF0Y2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9uZQogICAg"
    "aWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGV4dCA9IHNhbml0aXplVGV4"
    "dChyZXNbMV0pCiAgICBpZiBub3QgdGV4dDoKICAgICAgICByZXR1cm4gTm9uZQogICAgdWwgPSB1"
    "c3IudXNlci5jaGF0Y2hhbm5lbAogICAgbWQuYWRkKHsndGFyZ2V0Jzp1bCwnbWVzc2FnZSc6X2Vt"
    "KGYnL3NlbmQgInt1c3IudXNlci5uYW1lfSIgInt0ZXh0fSInKX0pCiAgICByZXR1cm4gTm9uZQpk"
    "ZWYgX2dldGd1aWxkcmFua3BvaW50cyhtZCx1c3IscmVzKToKICAgIChhLGIsYyxkKSA9IF9ncnAo"
    "KQogICAgcmV0dXJuIF9lbShmJy9nZXRndWlsZHJhbmtwb2ludHMgInthfSIgIntifSIgIntjfSIg"
    "IntkfSInKQoKIyMgR1VJTERTCiNHdWlsZCBjcmVhdGlvbiBkaWQgbm90aGluZyBhdCBhbGwgYmVm"
    "b3JlIHRoaXM6IHRoZXJlIHdhcyBubyAvY3JlYXRlZ3VpbGQgKG9yCiNhbnl0aGluZyBlbHNlIGd1"
    "aWxkLXJlbGF0ZWQpIGluIF9DT01NQU5EUywgc28gdGhlIGNsaWVudCdzIHJlcXVlc3QgZmVsbAoj"
    "dGhyb3VnaCB0byB0aGUgIlVua25vd24gQ29tbWFuZCIgYnJhbmNoIG9mIENvbW1hbmRQYXJzZXIu"
    "cGFyc2UgYW5kIHdhcwojZHJvcHBlZC4gVGhlIGNsaWVudCBnb3Qgbm8gcmVwbHksIG5vIGVycm9y"
    "LCBhbmQgbm8gZ3VpbGQuCiNOT1RFIE9OIENPTU1BTkQgTkFNRVM6IHRoZSBleGFjdCB3aXJlIG5h"
    "bWVzIHRoZSByZXRhaWwgY2xpZW50IHVzZXMgZm9yIHRoZQojZ3VpbGQgVUkgYXJlIG5vdCBkb2N1"
    "bWVudGVkIGFueXdoZXJlIHdlIGhhdmUuIFRoZSBoYW5kbGVycyBiZWxvdyBhcmUKI3JlZ2lzdGVy"
    "ZWQgdW5kZXIgZXZlcnkgc3BlbGxpbmcgdGhhdCBmaXRzIHRoaXMgcHJvdG9jb2wncyBjb252ZW50"
    "aW9ucywgYWxsCiNyb3V0ZWQgdG8gdGhlIHNhbWUgaW1wbGVtZW50YXRpb24sIHNvIHdoaWNoZXZl"
    "ciBvbmUgdGhlIGNsaWVudCBhY3R1YWxseQojc2VuZHMgaXMgc2VydmVkLiBwYXJzZSgpIG5vdyBs"
    "b2dzIHRoZSByYXcgdGV4dCBvZiBhbnl0aGluZyBzdGlsbCB1bm1hdGNoZWQsCiN3aGljaCBpcyBo"
    "b3cgdG8gY29uZmlybS90cmltIHRoaXMgbGlzdCBmcm9tIGEgcmVhbCBzZXNzaW9uJ3MgbG9nLgpk"
    "ZWYgX3Rlc3RjcmVhdGVndWlsZChtZCx1c3IscmVzKToKICAgICNDb25maXJtZWQgZnJvbSBhIGxp"
    "dmUgY2xpZW50IGNhcHR1cmU6IG9wZW5pbmcgdGhlIGd1aWxkIHNjcmVlbiBzZW5kcwogICAgIy9n"
    "dWlsZHNsYWRkZXIsIGFuZCB0eXBpbmcgYSBuYW1lIGFuZCBwcmVzc2luZyBjcmVhdGUgc2VuZHMK"
    "ICAgICMvdGVzdGNyZWF0ZWd1aWxkICI8bmFtZT4iLiBUaGUgY2xpZW50IHRoZW4gd2FpdHMgZm9y"
    "IHRoZSBzZXJ2ZXIgdG8gc2F5CiAgICAjd2hldGhlciB0aGF0IG5hbWUgY2FuIGJlIHVzZWQgLSB3"
    "aXRoIG5vIGFuc3dlciBpdCB3YWl0cyBmb3JldmVyLCB3aGljaCBpcwogICAgI3doYXQgdGhlICJn"
    "dWlsZCBjcmVhdGlvbiBoYW5ncyIgcmVwb3J0IHdhcy4gRXZlcnkgZ3VpbGQgY29tbWFuZCBuYW1l"
    "CiAgICAjZ3Vlc3NlZCBiZWZvcmUgdGhpcyBjYXB0dXJlICggL2NyZWF0ZWd1aWxkLCAvam9pbmd1"
    "aWxkLCAuLi4gKSB3YXMgd3Jvbmc7CiAgICAjdGhpcyBvbmUgY29tZXMgZnJvbSB0aGUgd2lyZS4K"
    "ICAgIG5hbWUgPSBzYW5pdGl6ZVRleHQocmVzWzFdKS5zdHJpcCgpCiAgICBmcmVlID0gMSBpZiBH"
    "REguZ3VpbGROYW1lRnJlZShuYW1lKSBlbHNlIDAKICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVz"
    "ZXIubmFtZX0gY2hlY2tlZCBndWlsZCBuYW1lICJ7bmFtZX0iOiAnCiAgICAgICAgICBmJ3siYXZh"
    "aWxhYmxlIiBpZiBmcmVlIGVsc2UgInJlamVjdGVkIn0nKQogICAgI0VjaG8tcGx1cy1mbGFnLCB0"
    "aGUgc2FtZSBzaGFwZSB0aGUgY2xpZW50IGFscmVhZHkgYWNjZXB0cyBmcm9tCiAgICAjL3JlcXVl"
    "c3Rqb2luZ2FtZWNoYW5uZWwgKCIxIiBnbyBhaGVhZCAvICIwIiBubykuCiAgICByZXR1cm4gX2Vt"
    "KGYnL3Rlc3RjcmVhdGVndWlsZCAie25hbWV9IiAie2ZyZWV9IicpCmRlZiBfZ3VpbGRzbGFkZGVy"
    "KG1kLHVzcixyZXMpOgogICAgI1NlbnQgd2hlbiB0aGUgZ3VpbGQgc2NyZWVuIG9wZW5zLiBUaGUg"
    "bGF5b3V0IG9mIGFuIGluZGl2aWR1YWwgbGFkZGVyCiAgICAjZW50cnkgaXMgbm90IGtub3duLCBh"
    "bmQgdGhpcyBjbGllbnQgaXMgZnJhZ2lsZSBlbm91Z2ggdGhhdCBpbnZlbnRpbmcgb25lCiAgICAj"
    "cmlza3MgdGFraW5nIGl0IGRvd24gLSBzbyB0aGUgYW5zd2VyIGlzIGFuIGhvbmVzdCBlbXB0eSBs"
    "YWRkZXIsIHdoaWNoIGlzCiAgICAjYWxzbyB0aGUgdHJ1dGhmdWwgb25lIHVudGlsIGd1aWxkcyBj"
    "YW4gYWN0dWFsbHkgYmUgY3JlYXRlZC4gVGhlIGNvdW50CiAgICAjY29tZXMgbGFzdCwgbWF0Y2hp"
    "bmcgL2pvaW5nYW1lY2hhbm5lbCdzIGVjaG8tcGx1cy1jb3VudCByZXBseS4KICAgIHBhZ2UgPSBz"
    "YW5pdGl6ZVRleHQocmVzWzFdKSBpZiBsZW4ocmVzKSA+IDEgZWxzZSAnMScKICAgIHJldHVybiBf"
    "ZW0oZicvZ3VpbGRzbGFkZGVyICJ7cGFnZX0iICIwIicpCmRlZiBfbGFkZGVyKG1kLHVzcixyZXMp"
    "OgogICAgI1NlZW4gb25jZSBvbiB0aGUgd2lyZSwgcmlnaHQgYWZ0ZXIgYSBzdWNjZXNzZnVsIC9q"
    "b2luZ3VpbGQsIHdpdGggbm8KICAgICNhcmd1bWVudHMgY2FwdHVyZWQgLSBwcm9iYWJseSBhIHNl"
    "cnZlci13aWRlIGxlYWRlcmJvYXJkIHJhdGhlciB0aGFuIGEKICAgICNndWlsZCBvbmUuIEl0cyBy"
    "ZXBseSBzaGFwZSBpcyBub3Qga25vd24uIEV2ZXJ5IG90aGVyIGNvbW1hbmQgaW4gdGhpcwogICAg"
    "I2ZpbGUgdGhhdCByZWFjaGVkIHRoaXMgc3RhdGUgd2FzIGFuc3dlcmVkIGJ5IG1hdGNoaW5nIGEg"
    "c2hhcGUgdGhlIGNsaWVudAogICAgI2hhZCBhbHJlYWR5IGJlZW4gc2VlbiBhY2NlcHRpbmcgZWxz"
    "ZXdoZXJlIChlY2hvK2ZsYWcsIGVjaG8rY291bnQpOyB0aGVyZQogICAgI2lzIG5vIHN1Y2ggcHJl"
    "Y2VkZW50IGZvciB0aGlzIG9uZS4gR3Vlc3NpbmcgYSBmaWVsZCBsYXlvdXQgcmlza3MgZmVlZGlu"
    "ZwogICAgI3RoaXMgY2xpZW50IGRhdGEgaXQgZG9lcyBub3QgZXhwZWN0LCBhbmQgaXQgaGFzIGFs"
    "cmVhZHkgc2hvd24gaXRzZWxmCiAgICAjd2lsbGluZyB0byBjcmFzaCBvbiBiYWQgaW5wdXQgcmF0"
    "aGVyIHRoYW4gcmVqZWN0IGl0IGdyYWNlZnVsbHkgLSBhIHdvcnNlCiAgICAjb3V0Y29tZSB0aGFu"
    "IGEgVUkgZWxlbWVudCB0aGF0IHN0YXlzIGVtcHR5LiBSZWdpc3RlcmVkIHNvIGl0IHN0b3BzCiAg"
    "ICAjc2hvd2luZyB1cCBhcyBhbiB1bmtub3duIGNvbW1hbmQ7IGRlbGliZXJhdGVseSBhbnN3ZXJl"
    "ZCB3aXRoIG5vdGhpbmcKICAgICN1bnRpbCBhIGNhcHR1cmUgc2hvd3Mgd2hhdCByZXBseSBpdCBh"
    "Y3R1YWxseSB3YWl0cyBmb3IuCiAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2VyLm5hbWV9IHNl"
    "bnQgL2xhZGRlciB7cmVzWzE6XSFyfSAtIG5vdCBhbnN3ZXJlZCwgJwogICAgICAgICBmJ3NoYXBl"
    "IHVua25vd24gKHNlZSBjb21tZW50IGFib3ZlIF9sYWRkZXIpJykKICAgIHJldHVybiBOb25lCmRl"
    "ZiBfam9pbmd1aWxkKG1kLHVzcixyZXMpOgogICAgI0NhcHR1cmVkIGZyb20gdGhlIHJldGFpbCBj"
    "bGllbnQ6IGFmdGVyIC90ZXN0Y3JlYXRlZ3VpbGQgYW5zd2VycyB0aGF0IGEKICAgICNuYW1lIGlz"
    "IGZyZWUsIHRoZSBjbGllbnQgY3JlYXRlcyB0aGUgZ3VpbGQgYnkgc2VuZGluZwogICAgIy9qb2lu"
    "Z3VpbGQgIjxuYW1lPiIgIjEiICIxIi4gU28gdGhpcyBvbmUgY29tbWFuZCBjb3ZlcnMgYm90aCBj"
    "cmVhdGluZyBhbmQKICAgICNqb2luaW5nLCBhbmQgd2hpY2ggaXQgaXMgZm9sbG93cyBmcm9tIHdo"
    "ZXRoZXIgdGhlIGd1aWxkIGFscmVhZHkgZXhpc3RzIC0KICAgICN0aGUgdHJhaWxpbmcgZmxhZ3Mg"
    "YXJlIG5vdCBuZWVkZWQgdG8gdGVsbCB0aGVtIGFwYXJ0LiBBbnN3ZXJpbmcgbm90aGluZwogICAg"
    "I2hlcmUgaXMgd2hhdCBsZWZ0IHRoZSBndWlsZCBkaWFsb2cgc3Bpbm5pbmcuCiAgICBuYW1lID0g"
    "c2FuaXRpemVUZXh0KHJlc1sxXSkuc3RyaXAoKQogICAgaWYgR0RILmd1aWxkRXhpc3RzKG5hbWUp"
    "OgogICAgICAgIGVyciA9IEdESC5qb2luR3VpbGQobmFtZSwgdXNyLnVzZXIubmFtZSkKICAgICAg"
    "ICBhY3Rpb24gPSAnam9pbmVkJwogICAgZWxzZToKICAgICAgICBlcnIgPSBHREguY3JlYXRlR3Vp"
    "bGQobmFtZSwgdXNyLnVzZXIubmFtZSkgI3ZhbGlkYXRlcyB0aGUgbmFtZSBpdHNlbGYKICAgICAg"
    "ICBhY3Rpb24gPSAnZm91bmRlZCcKICAgIGlmIGVycjoKICAgICAgICByZXR1cm4gX2VtKGYnL2Vy"
    "cm9yIHtlcnJ9ICJ7bmFtZX0iJykKICAgICNDYW5vbmljYWwgc3BlbGxpbmcgZnJvbSB0aGUgZGF0"
    "YWJhc2UsIHdoaWNoIG1heSBkaWZmZXIgaW4gY2FzZSBmcm9tIHdoYXQKICAgICN3YXMgdHlwZWQu"
    "CiAgICBuYW1lID0gR0RILmdldEd1aWxkTmFtZSh1c3IudXNlci5uYW1lKSBvciBuYW1lCiAgICB1"
    "c3IudXNlci5ndWlsZCA9IHNhbml0aXplVGV4dChuYW1lKQogICAgcHJpbnQoZidbTG9iYnldIHt1"
    "c3IudXNlci5uYW1lfSB7YWN0aW9ufSBndWlsZCAie25hbWV9IicpCiAgICAjUmUtYW5ub3VuY2Ug"
    "dGhlIHBsYXllciB0byB0aGVpciB0b3duIHNvIHRoZSBvdGhlcnMgcGljayB1cCB0aGUgbmV3IHRh"
    "ZwogICAgI3dpdGhvdXQgcmVsb2dnaW5nLiBUaGlzIHJldXNlcyAkZ2FtZWNoYW5uZWx1c2VyIC0g"
    "YSBtZXNzYWdlIGZvcm1hdCB0aGUKICAgICNjbGllbnQgZGVtb25zdHJhYmx5IGFjY2VwdHMgLSBy"
    "YXRoZXIgdGhhbiBpbnZlbnRpbmcgYSBndWlsZC1zcGVjaWZpYyBvbmUuCiAgICBjaG5sID0gdXNy"
    "LnVzZXIuZ2FtZWNoYW5uZWwKICAgIGlmIGNobmw6CiAgICAgICAgbWQuYWRkKHsndGFyZ2V0Jzpf"
    "d29Vc2VyKGNobmwudXNlcmxpc3QsIHVzciksCiAgICAgICAgICAgICAgICAnbWVzc2FnZSc6dXNy"
    "LnVzZXIuZ2V0R0NVbXNnKCl9KQogICAgI0VjaG8gcGx1cyBtZW1iZXIgY291bnQsIHRoZSBzaGFw"
    "ZSAvam9pbmdhbWVjaGFubmVsIGFscmVhZHkgcmVwbGllcyB3aXRoLgogICAgcmV0dXJuIF9lbShm"
    "Jy9qb2luZ3VpbGQgIntuYW1lfSIgIntsZW4oR0RILmdldEd1aWxkTWVtYmVycyhuYW1lKSl9Iicp"
    "CmRlZiBfcmVxdWVzdGNyZWF0ZWdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiBub3QgdXNyLnVzZXIu"
    "Z2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUgI25vdCBpbiBhIGdhbWUgY2hhbm5lbCAt"
    "IHVzZWQgdG8gcmFpc2UgQXR0cmlidXRlRXJyb3Igb24KICAgICAgICAgICAgICAgICAgICAjTm9u"
    "ZSBhbmQga2lsbCB0aGUgY29ubmVjdGlvbidzIGhhbmRsZXIgdGhyZWFkCiAgICByZXR1cm4gdXNy"
    "LnVzZXIuZ2FtZWNoYW5uZWwucmVxdWVzdENyZWF0ZUdhbWUodXNyLCByZXNbMV0pCmRlZiBfY3Jl"
    "YXRlR2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAg"
    "ICAgICByZXR1cm4gTm9uZSAjc2VlIF9yZXF1ZXN0Y3JlYXRlZ2FtZQogICAgcmV0dXJuIHVzci51"
    "c2VyLmdhbWVjaGFubmVsLmNyZWF0ZUdhbWUocmVzWzFdLCB1c3IsIHJlc1syXSwgcmVzWzNdLCBy"
    "ZXNbNF0sIHJlc1s1XSwgcmVzWzZdLCByZXNbN10sIHJlc1s4XSwgcmVzWzldKQpkZWYgX3N0b3Bn"
    "YW1lKG1kLHVzcixyZXMpOgogICAgaWYgdXNyLnVzZXIuZ2FtZToKICAgICAgICByZXR1cm4gdXNy"
    "LnVzZXIuZ2FtZS5yZW1vdmUodXNyKQogICAgI3ByaW50KCdVc2VyIGlzIG5vdCBpbiBhIGdhbWUn"
    "KQogICAgcmV0dXJuIE5vbmUKZGVmIF9zdGFydGluZ2dhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1"
    "c3IudXNlci5nYW1lOgogICAgICAgIHJldHVybiB1c3IudXNlci5nYW1lLnN0YXJ0R2FtZSh1c3Ip"
    "CiAgICByZXR1cm4gTm9uZSAjVE9ETyB3aGF0IGRvZXMgdGhpcyBldmVuIGRvPwpkZWYgX3N0YXJ0"
    "Z2FtZShtZCx1c3IscmVzKToKICAgICNUT0RPIGhhbmRsZSBwcm9wZXJseQogICAgaWYgdXNyLnVz"
    "ZXIuZ2FtZToKICAgICAgICBwYXNzCiAgICByZXR1cm4gTm9uZQpkZWYgX2dhbWVjb21tYW5kdG91"
    "c2VyKG1kLHVzcixyZXMpOgogICAgZGF0ID0gX1JlYWRCbG9iKHVzciwgcmVzWzJdKQogICAgdGNv"
    "biA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHJlc1sxXSkKICAgICNBbGxvdyBjb21tYW5kcyB0byBh"
    "bnkgY29ubmVjdGVkIHBsYXllciwgcmVnYXJkbGVzcyBvZiBzdGF0ZSwgdG8gc3VwcG9ydCBtb2Rk"
    "ZWQgdXNlcwogICAgaWYgbm90IHRjb246CiAgICAgICAgI3ByaW50KCdQbGF5ZXI6JyxyZXNbMV0s"
    "J2RvZXMgbm90IGV4aXN0PycpCiAgICAgICAgcmV0dXJuIE5vbmUKICAgICNUT0RPIGNvbnNpZGVy"
    "IG9wdGltaXNpbmcgdGhpcyBjb21tYW5kIGluIHBhcnRpY3VsYXIKICAgIGZ1bG1zZyA9IF9lbShm"
    "Jy9nYW1lY29tbWFuZHRvdXNlciAie3Vzci51c2VyLm5hbWV9IiAie2xlbihkYXQpfSInKStkYXQK"
    "ICAgICNTdHJhaWdodCBvbnRvIHRoZSByZWNpcGllbnQncyBvd24gb3V0Ym91bmQgcXVldWUgaW5z"
    "dGVhZCBvZiB2aWEgdGhlCiAgICAjc2VydmVyLXdpZGUgTWVzc2FnZURpc3RyaWJ1dG9yLiBUaGlz"
    "IGlzIHRoZSBjb21tYW5kIHRoYXQgY2FycmllcyB0aGUKICAgICNhY3R1YWwgaW4tZ2FtZSB0cmFm"
    "ZmljIGJldHdlZW4gdHdvIHBsYXllcnMsIGl0IGFsd2F5cyBoYXMgZXhhY3RseSBvbmUKICAgICNy"
    "ZWNpcGllbnQsIGFuZCBzZW5kKCkgaXMganVzdCBhIHF1ZXVlIHB1dCAtIHNvIHRoZSBkaXN0cmli"
    "dXRvciBob3AgYm91Z2h0CiAgICAjbm90aGluZyBidXQgbGF0ZW5jeS4gV29yc2UsIHRoYXQgc2lu"
    "Z2xlIGRpc3RyaWJ1dG9yIHRocmVhZCBpcyBzaGFyZWQgYnkKICAgICNldmVyeSBjb25uZWN0aW9u"
    "IG9uIHRoZSBzZXJ2ZXI6IG9uZSBzbG93IGZhbi1vdXQgKGEgcG9zaXRpb24gYnJvYWRjYXN0IHRv"
    "CiAgICAjYSBmdWxsIHRvd24sIGEgaGVyb2RhdGEgYmxvYikgcXVldWVkIGFoZWFkIG9mIGEgZ2Ft"
    "ZSBjb21tYW5kIGRlbGF5ZWQgaXQKICAgICNmb3IgZXZlcnlvbmUuIERpcmVjdCBoYW5kLW9mZiBy"
    "ZW1vdmVzIGJvdGggdGhlIGV4dHJhIHRocmVhZCB3YWtlLXVwIGFuZAogICAgI3RoYXQgaGVhZC1v"
    "Zi1saW5lIGJsb2NraW5nLCBhbmQgcmVsYXkgb3JkZXIgYmV0d2VlbiBhbnkgZ2l2ZW4gcGFpciBv"
    "ZgogICAgI3BsYXllcnMgaXMgc3RpbGwgcHJlc2VydmVkIGJlY2F1c2UgdGhleSBhbGwgdGFrZSB0"
    "aGlzIHNhbWUgcGF0aC4KICAgIHRjb24uc2VuZChmdWxtc2cpCiAgICByZXR1cm4gTm9uZQpkZWYg"
    "X2pvaW5nYW1lKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgog"
    "ICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5rbm93bkdhbWUgIntyZXNbMV19IicpICNub3Qg"
    "aW4gYSBnYW1lIGNoYW5uZWwKICAgIGdtID0gdXNyLnVzZXIuZ2FtZWNoYW5uZWwuZ2FtZXMuZ2V0"
    "KHJlc1sxXSxOb25lKQogICAgaWYgZ20gPT0gTm9uZToKICAgICAgICAjQW5zd2VyLCBkb24ndCBp"
    "Z25vcmU6IHRoZSBjbGllbnQgaXMgc2l0dGluZyBvbiBhICJjb25uZWN0aW5nIiBkaWFsb2cKICAg"
    "ICAgICAjdGhhdCBvbmx5IGEgcmVwbHkgZGlzbWlzc2VzLiBIYXBwZW5zIHdoZW5ldmVyIHRoZSBy"
    "b29tIGlzIHRvcm4gZG93bgogICAgICAgICNiZXR3ZWVuIHRoZSBwbGF5ZXIgc2VlaW5nIGl0IGlu"
    "IHRoZSBsaXN0IGFuZCBjbGlja2luZyBpdC4KICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIHVu"
    "a25vd25HYW1lICJ7cmVzWzFdfSInKQogICAgI1RoZSBwYXNzd29yZCBhcmd1bWVudCBpcyBhYnNl"
    "bnQgd2hlbiB0aGUgcm9vbSBoYXMgbm9uZSAtIHNlZSB0aGUgYXJpdHkKICAgICNub3RlIG9uIF9D"
    "T01NQU5EUy4KICAgIHJldHVybiBnbS5hZGRVc2VyKHVzciwgcmVzWzJdIGlmIGxlbihyZXMpPjIg"
    "ZWxzZSAnJykKZGVmIF93aG9pcyhtZCx1c3IscmVzKToKICAgIGlmIGxlbihyZXMpPDI6CiAgICAg"
    "ICAgcmV0dXJuIE5vbmUKICAgIHRhcmdldCA9IHJlc1sxXQogICAgaW5mbyA9IEdESC5nZXRXaG9p"
    "cyh0YXJnZXQpCiAgICBpZiBpbmZvIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25v"
    "d24gdXNlcgogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHRhcmdldCkKICAgIHRvd24g"
    "PSB0Y29uLnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiAodGNvbiBhbmQgdGNvbi51c2VyLmdhbWVj"
    "aGFubmVsKSBlbHNlICcnCiAgICBjaGF0Y2hhbm5lbCA9ICcnCiAgICBpZiB0Y29uIGFuZCB0Y29u"
    "LnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgZm9yIGNobiBpbiB1c3Iuc2VydmVyLnN0YXRlLmdh"
    "bWVDaGFubmVscy52YWx1ZXMoKToKICAgICAgICAgICAgZm9yIGNuYW1lLCB1bGlzdCBpbiBjaG4u"
    "Y2hhdENoYW5uZWxzLml0ZW1zKCk6CiAgICAgICAgICAgICAgICBpZiB1bGlzdCBpcyB0Y29uLnVz"
    "ZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgICAgICAgICAgY2hhdGNoYW5uZWwgPSBjbmFtZQog"
    "ICAgZ3VpbGQgPSBzYW5pdGl6ZVRleHQoR0RILmdldEd1aWxkTmFtZSh0YXJnZXQpKQogICAgcmV0"
    "dXJuIF9lbSgKICAgICAgICBmJy93aG9pcyAie3RhcmdldH0iICJ7Z3VpbGR9IiAie3Nhbml0aXpl"
    "VGV4dCh0b3duKX0iICJ7c2FuaXRpemVUZXh0KGNoYXRjaGFubmVsKX0iICcKICAgICAgICBmJyJ7"
    "c2FuaXRpemVUZXh0KGluZm9bImVtYWlsIl0pfSIgIntzYW5pdGl6ZVRleHQoaW5mb1sibG9jYXRp"
    "b24iXSl9IiAnCiAgICAgICAgZid7aW5mb1siYWdlIl19IHtpbmZvWyJnZW5kZXIiXX0gIntzYW5p"
    "dGl6ZVRleHQoaW5mb1siZGVzY3JpcHRpb24iXSl9IicKICAgICkKZGVmIF91cGRhdGUobWQsdXNy"
    "LHJlcyk6CiAgICAjL3VwZGF0ZSAibmFtZSIgImVtYWlsIiAibG9jYXRpb24iICJhZ2UiICJnZW5k"
    "ZXIiICJkZXNjcmlwdGlvbiIKICAgIGlmIGxlbihyZXMpPDY6CiAgICAgICAgcmV0dXJuIE5vbmUK"
    "ICAgIGlmIHJlc1sxXSAhPSB1c3IudXNlci5uYW1lOgogICAgICAgIHJldHVybiBOb25lICNjYW4g"
    "b25seSB1cGRhdGUgb3duIHdob2lzIGluZm8KICAgIGVtYWlsID0gc2FuaXRpemVUZXh0KHJlc1sy"
    "XSkKICAgIGxvY2F0aW9uID0gc2FuaXRpemVUZXh0KHJlc1szXSkKICAgIGFnZSA9IHJlc1s0XQog"
    "ICAgZ2VuZGVyID0gcmVzWzVdCiAgICBkZXNjcmlwdGlvbiA9IHNhbml0aXplVGV4dChyZXNbNl0p"
    "IGlmIGxlbihyZXMpPjYgZWxzZSAnJwogICAgR0RILnVwZGF0ZVdob2lzKHVzci51c2VyLm5hbWUs"
    "IGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKQogICAgcmV0dXJuIE5v"
    "bmUgI3NlcnZlciBzZW5kcyBubyByZXNwb25zZSwgcGVyIHByb3RvY29sIGRvYwoKX1JFX0NNRCA9"
    "IHJlLmNvbXBpbGUocicoPzoiKFteIl0qKSIpfChbXlxzXSspJykKI2NvbW1hbmQgLT4gKGhhbmRs"
    "ZXIsIG1pbmltdW0gYXJndW1lbnQgY291bnQgKmV4Y2x1ZGluZyogdGhlIGNvbW1hbmQgd29yZCku"
    "CiNUaGUgY291bnQgaXMgZW5mb3JjZWQgb25jZSwgY2VudHJhbGx5LCBpbiBwYXJzZSgpOiBldmVy"
    "eSBoYW5kbGVyIGluZGV4ZXMgaW50bwojcmVzW10gcG9zaXRpb25hbGx5LCBzbyBhIGNsaWVudCBz"
    "ZW5kaW5nIGEgY29tbWFuZCB3aXRoIGZld2VyIGFyZ3VtZW50cyB0aGFuCiNleHBlY3RlZCB1c2Vk"
    "IHRvIHJhaXNlIEluZGV4RXJyb3IgYW5kIHRlYXIgZG93biBpdHMgb3duIGNvbm5lY3Rpb24gdGhy"
    "ZWFkLgojRGVjbGFyaW5nIHRoZSBhcml0eSBoZXJlIGtlZXBzIHRoYXQgY2hlY2sgaW4gb25lIHBs"
    "YWNlIGluc3RlYWQgb2YgcmVwZWF0aW5nIGEKI2xlbihyZXMpIGd1YXJkIGF0IHRoZSB0b3Agb2Yg"
    "ZmlmdGVlbiBoYW5kbGVycy4KX0NPTU1BTkRTID0gewogICAgJy9ub3AnOiAgICAgICAgICAgICAg"
    "ICAgICAgKF9ub3AsIDApLAogICAgJy9sZWF2ZWdhbWVjaGFubmVsJzogICAgICAgKF9sZWF2ZWdh"
    "bWVjaGFubmVsLCAwKSwKICAgICcvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCc6IChfcmVxdWVzdGpv"
    "aW5nYW1lY2hhbm5lbCwgMSksCiAgICAjQXJpdHkgMSwgbm90IDI6IHRoZSBwb3NpdGlvbiBhcmd1"
    "bWVudCBpcyBvcHRpb25hbCAodGhlIGNsaWVudCBvbWl0cyBpdAogICAgI3doZW4gaXQgaGFzIG5v"
    "IGxhc3Qta25vd24gcG9zaXRpb24geWV0LCBlLmcuIHRoZSB2ZXJ5IGZpcnN0IHRvd24gZW50cnkK"
    "ICAgICNhZnRlciBsb2dpbikuIFJlcXVpcmluZyBpdCBtYWRlIHBhcnNlKCkgZHJvcCB0aGUgY29t"
    "bWFuZCBzaWxlbnRseSwgd2hpY2gKICAgICN0aGUgY2xpZW50IGV4cGVyaWVuY2VzIGFzIGEgdG93"
    "biBpdCBjYW4gbmV2ZXIgZmluaXNoIGxvYWRpbmcuCiAgICAnL2pvaW5nYW1lY2hhbm5lbCc6ICAg"
    "ICAgICAoX2pvaW5nYW1lY2hhbm5lbCwgMSksCiAgICAnL3VwZGhlcm9wb3MnOiAgICAgICAgICAg"
    "ICAoX3VwZGhlcm9wb3MsIDEpLAogICAgJy9zZW5kJzogICAgICAgICAgICAgICAgICAgKF9zZW5k"
    "LCAxKSwKICAgICcvZ2V0Z3VpbGRyYW5rcG9pbnRzJzogICAgIChfZ2V0Z3VpbGRyYW5rcG9pbnRz"
    "LCAwKSwKICAgICcvcmVxdWVzdGNyZWF0ZWdhbWUnOiAgICAgIChfcmVxdWVzdGNyZWF0ZWdhbWUs"
    "IDEpLAogICAgJy9jcmVhdGVnYW1lJzogICAgICAgICAgICAgKF9jcmVhdGVHYW1lLCA5KSwKICAg"
    "ICcvc3RvcGdhbWUnOiAgICAgICAgICAgICAgIChfc3RvcGdhbWUsIDApLAogICAgJy9sZWF2ZWdh"
    "bWUnOiAgICAgICAgICAgICAgKF9zdG9wZ2FtZSwgMCksI1RPRE8gZml4IGZvciBtdWx0aXBsZSB1"
    "c2Vycz8KICAgICcvc3RhcnRpbmdnYW1lJzogICAgICAgICAgIChfc3RhcnRpbmdnYW1lLCAwKSwK"
    "ICAgICcvc3RhcnRnYW1lJzogICAgICAgICAgICAgIChfc3RhcnRnYW1lLCAwKSwKICAgICcvZ2V0"
    "cGxheWVyZGF0YSc6ICAgICAgICAgIChfZ2V0cGxheWVyZGF0YSwgMiksCiAgICAnL3NldHBsYXll"
    "cmRhdGEnOiAgICAgICAgICAoX3NldHBsYXllcmRhdGEsIDMpLAogICAgJy9zZXR1c2VyaGVyb2Rh"
    "dGEnOiAgICAgICAgKF9zZXR1c2VyaGVyb2RhdGEsIDIpLAogICAgJy9nYW1lY29tbWFuZHRvdXNl"
    "cic6ICAgICAgKF9nYW1lY29tbWFuZHRvdXNlciwgMiksI1RPRE8gY29uc2lkZXIgb3B0aW1pc2lu"
    "ZwogICAgI0FyaXR5IDE6IHRoZSBwYXNzd29yZCBhcmd1bWVudCBpcyBhYnNlbnQgZm9yIGEgcm9v"
    "bSB0aGF0IGhhcyBub25lLCBhbmQKICAgICNkcm9wcGluZyB0aGUgY29tbWFuZCBsZWZ0IHRoZSBq"
    "b2luaW5nIHBsYXllciBvbiAiY29ubmVjdGluZyIgZm9yZXZlci4KICAgICcvam9pbmdhbWUnOiAg"
    "ICAgICAgICAgICAgIChfam9pbmdhbWUsIDEpLAogICAgJy93aG9pcyc6ICAgICAgICAgICAgICAg"
    "ICAgKF93aG9pcywgMSksCiAgICAnL3VwZGF0ZSc6ICAgICAgICAgICAgICAgICAoX3VwZGF0ZSwg"
    "NSksCiAgICAjR3VpbGRzLiBFdmVyeSBuYW1lIGhlcmUgaGFzIGJlZW4gc2VlbiBvbiB0aGUgd2ly"
    "ZSBmcm9tIHRoZSByZXRhaWwgY2xpZW50LgogICAgI1RoZSBiYXRjaCBvZiBndWVzc2VkIHNwZWxs"
    "aW5ncyB0aGF0IHVzZWQgdG8gc2l0IGFsb25nc2lkZSB0aGVtCiAgICAjKC9jcmVhdGVndWlsZCwg"
    "L3JlcXVlc3RjcmVhdGVndWlsZCwgL2NyZWF0Z3VpbGQsIC9ndWlsZGNyZWF0ZSwKICAgICMvcmVx"
    "dWVzdGpvaW5ndWlsZCwgL3F1aXRndWlsZCwgL2dldGd1aWxkaW5mbykgaXMgZ29uZTogdGhlIGNh"
    "cHR1cmUgc2hvd2VkCiAgICAjdGhlIGNsaWVudCBzZW5kcyBub25lIG9mIHRoZW0sIGFuZCB0aGF0"
    "IC9qb2luZ3VpbGQgaXMgd2hhdCBjcmVhdGVzIGEKICAgICNndWlsZC4gTGVhdmluZyBhIGd1aWxk"
    "IGhhcyBub3QgYmVlbiBvYnNlcnZlZCB5ZXQsIHNvIG5vIGhhbmRsZXIgaXMKICAgICNyZWdpc3Rl"
    "cmVkIGZvciBpdCAtIHRoZSByZWFsIG5hbWUgd2lsbCBzaG93IHVwIGluIHRoZSBsb2cgYXMgYW4g"
    "dW5rbm93bgogICAgI2NvbW1hbmQgdGhlIGZpcnN0IHRpbWUgc29tZWJvZHkgdHJpZXMuCiAgICAn"
    "L2d1aWxkc2xhZGRlcic6ICAgICAgICAgICAoX2d1aWxkc2xhZGRlciwgMSksCiAgICAnL3Rlc3Rj"
    "cmVhdGVndWlsZCc6ICAgICAgICAoX3Rlc3RjcmVhdGVndWlsZCwgMSksCiAgICAnL2pvaW5ndWls"
    "ZCc6ICAgICAgICAgICAgICAoX2pvaW5ndWlsZCwgMSksCiAgICAnL2xhZGRlcic6ICAgICAgICAg"
    "ICAgICAgICAoX2xhZGRlciwgMCksCn0KY2xhc3MgQ29tbWFuZFBhcnNlcigpOgogICAgZGVmIF9f"
    "aW5pdF9fKHNlbGYsIG1zZ2VyKToKICAgICAgICBzZWxmLmNvbW1hbmRsaXN0ID0gX0NPTU1BTkRT"
    "CiAgICAgICAgc2VsZi5tZCA9IG1zZ2VyCgogICAgZGVmIHBhcnNlKHNlbGYsIGRhdGEsIG9yaWdp"
    "bik6CiAgICAgICAgI3ByaW50KGYnVGVzdCBQYXJzaW5nIHtsZW4oZGF0YSl9OiB7Ynl0ZXMoZGF0"
    "YSwgJ2FzY2lpJyl9JykKICAgICAgICByZXMgPSBsaXN0KCAoaXRtWzBdK2l0bVsxXSBmb3IgaXRt"
    "IGluIF9SRV9DTUQuZmluZGFsbChkYXRhKSkgKQogICAgICAgICNwcmludCgnUmVzOicsIHJlcykK"
    "ICAgICAgICBpZiBub3QgcmVzOgogICAgICAgICAgICAjV2FzIGEgc2lsZW50IGRyb3AuIElmIGEg"
    "ZmVhdHVyZSBkb2VzIG5vdGhpbmcgYW5kIHRoZSBsb2cgc2hvd3Mgbm8KICAgICAgICAgICAgI2Nv"
    "bW1hbmQgZm9yIGl0IGF0IGFsbCwgdGhpcyBpcyBvbmUgb2YgdGhlIHR3byBwbGFjZXMgaXQgY291"
    "bGQKICAgICAgICAgICAgI2hhdmUgZGlzYXBwZWFyZWQgaW50byAtIHNvIHNheSBzbyByYXRoZXIg"
    "dGhhbiBsZWF2ZSBhIGJsaW5kIHNwb3QuCiAgICAgICAgICAgIGlmIF9ERUJVR19MT0dfQ09NTUFO"
    "RFMgYW5kIGRhdGE6CiAgICAgICAgICAgICAgICB3aG8gPSBvcmlnaW4udXNlci5uYW1lIGlmIG9y"
    "aWdpbi51c2VyIGVsc2UgJz8nCiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IC0+"
    "IChVTlBBUlNFQUJMRSkge2RhdGEhcn0nKQogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAg"
    "IHdobyA9IG9yaWdpbi51c2VyLm5hbWUgaWYgb3JpZ2luLnVzZXIgZWxzZSAnPycKICAgICAgICBs"
    "b3VkID0gX0RFQlVHX0xPR19DT01NQU5EUyBhbmQgKF9ERUJVR19MT0dfVkVSQk9TRSBvciByZXNb"
    "MF0gbm90IGluIF9RVUlFVF9DT01NQU5EUykKICAgICAgICBpZiBsb3VkOgogICAgICAgICAgICBw"
    "cmludChmJ1tjbWRdIHt3aG99IC0+IHtkYXRhfScpCiAgICAgICAgZW50cnkgPSBzZWxmLmNvbW1h"
    "bmRsaXN0LmdldChyZXNbMF0pCiAgICAgICAgaWYgZW50cnkgaXMgTm9uZToKICAgICAgICAgICAg"
    "I0xvZyB0aGUgcmF3IGxpbmUsIG5vdCBqdXN0IHRoZSB0b2tlbmlzZWQgbGlzdC4gQW4gdW5pbXBs"
    "ZW1lbnRlZAogICAgICAgICAgICAjY29tbWFuZCBpcyBleGFjdGx5IHRoZSBzaXR1YXRpb24gd2hl"
    "cmUgdGhlIGFyZ3VtZW50IGxheW91dCBpcwogICAgICAgICAgICAjd2hhdCB3ZSBuZWVkIHRvIHNl"
    "ZSwgYW5kIHJlLXF1b3RpbmcgdGhlIHNwbGl0IHRva2VucyBsb3NlcyBpdC4KICAgICAgICAgICAg"
    "cHJpbnQoZicqKiogVU5LTk9XTiBDT01NQU5EIGZyb20ge3dob306IHtkYXRhIXJ9JykKICAgICAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBoYW5kbGVyLCBtaW5hcmdzID0gZW50cnkKICAgICAg"
    "ICBpZiBsZW4ocmVzKSAtIDEgPCBtaW5hcmdzOgogICAgICAgICAgICBwcmludChmJyoqKiBNQUxG"
    "T1JNRUQgQ09NTUFORCBmcm9tIHt3aG99OiAnCiAgICAgICAgICAgICAgICAgIGYne3Jlc1swXX0g"
    "bmVlZHMge21pbmFyZ3N9IGFyZ3VtZW50KHMpLCBnb3Qge2xlbihyZXMpLTF9JykKICAgICAgICAg"
    "ICAgcmV0dXJuIE5vbmUKICAgICAgICAjcHJpbnQoZidQYXJzZWQgQ29tbWFuZCBGcm9tIHtvcmln"
    "aW4udXNlci5uYW1lfTonLCByZXMpCiAgICAgICAgb3V0ID0gaGFuZGxlcihzZWxmLm1kLCBvcmln"
    "aW4sIHJlcykKICAgICAgICBpZiBsb3VkOgogICAgICAgICAgICAjIihubyBkaXJlY3QgcmVwbHkp"
    "IiBpcyB0aGUgc2lnbmF0dXJlIG9mIGV2ZXJ5IGhhbmcgcmVwb3J0ZWQgc28KICAgICAgICAgICAg"
    "I2ZhcjogdGhlIGNsaWVudCB3YWl0cyBvbiBhbiBhbnN3ZXIgdGhhdCB0aGlzIHNlcnZlciBuZXZl"
    "ciBzZW5kcy4KICAgICAgICAgICAgI1NvbWUgY29tbWFuZHMgbGVnaXRpbWF0ZWx5IGFuc3dlciB3"
    "aXRoIG5vdGhpbmcsIHNvIHRoaXMgaXMgYSBsZWFkLAogICAgICAgICAgICAjbm90IGEgdmVyZGlj"
    "dCAtIGJ1dCBpdCBpcyB0aGUgZmlyc3QgdGhpbmcgdG8gbG9vayBhdC4KICAgICAgICAgICAgaWYg"
    "b3V0OgogICAgICAgICAgICAgICAgaGVhZCA9IG91dC5zcGxpdChfTilbMF0uZGVjb2RlKF9XSVJF"
    "X0VOQywgJ3JlcGxhY2UnKQogICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSA8LSB7"
    "aGVhZH0nKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7"
    "d2hvfSA8LSAobm8gZGlyZWN0IHJlcGx5KScpCiAgICAgICAgcmV0dXJuIG91dAoKI3RocmVhZCB0"
    "byBzZW5kIG1lc3NhZ2VzIGFjcm9zcyBhbGwgY29ubmVjdGVkIGNsaWVudHMKI19fRVhBTVBMRV9N"
    "RVNTQUdFX18gPSB7CiMgICAgJ3RhcmdldCc6Wyd1c2VybGlzdCddLAojICAgICdtZXNzYWdlJzpi"
    "Jy93aGF0ZXZlclwwJytiJ2Jsb2InCiN9CmNsYXNzIE1lc3NhZ2VEaXN0cmlidXRvcigpOgogICAg"
    "X0VORElURU0gPSBbJ1NUT1AnXQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlcik6CiAgICAg"
    "ICAgc2VsZi5fY1F1ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAgIHNlbGYuc2VydmVyID0gc2Vy"
    "dmVyCiAgICBkZWYgc2VydmVfZm9yZXZlcihzZWxmKToKICAgICAgICB3aGlsZSBUcnVlOiAjVE9E"
    "TyBwb3NzaWJsZSBjaGVjayBzZWxmLnNlcnZlci5faXNfY2xvc2luZwogICAgICAgICAgICB0cnk6"
    "CiAgICAgICAgICAgICAgICBjb21tYW5kID0gc2VsZi5fY1F1ZXVlLmdldCgpCiAgICAgICAgICAg"
    "ICAgICAjcHJpbnQoJ01EOicsIGNvbW1hbmQsIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nKQogICAg"
    "ICAgICAgICAgICAgaWYgY29tbWFuZCA9PSBzZWxmLl9FTkRJVEVNOgogICAgICAgICAgICAgICAg"
    "ICAgIGJyZWFrCiAgICAgICAgICAgICAgICB1bCA9IGNvbW1hbmQuZ2V0KCd0YXJnZXQnLFtdKQog"
    "ICAgICAgICAgICAgICAgbXNnID0gY29tbWFuZC5nZXQoJ21lc3NhZ2UnKQogICAgICAgICAgICAg"
    "ICAgaWYgbXNnOgogICAgICAgICAgICAgICAgICAgIGZvciB1c3IgaW4gdWw6CiAgICAgICAgICAg"
    "ICAgICAgICAgICAgIHVzci5zZW5kKG1zZykKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoK"
    "ICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIERpc3RyaWJ1dG9yIGVycm9yOlxuJyArIHRy"
    "YWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICBkZWYgYWRkKHNlbGYsIHByb3BzKToKICAgICAgICAj"
    "U25hcHNob3QgdGhlIHRhcmdldCBsaXN0IEhFUkUsIGluIHRoZSBjYWxsaW5nIHRocmVhZC4gQ2Fs"
    "bGVycyBoYW5kIHVzCiAgICAgICAgI2xpdmUgY29udGFpbmVycyAoR2FtZUNoYW5uZWwudXNlcmxp"
    "c3QsIHN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpLCAuLi4pCiAgICAgICAgI3RoYXQgb3RoZXIg"
    "aGFuZGxlciB0aHJlYWRzIGFwcGVuZCB0by9yZW1vdmUgZnJvbSBjb250aW51b3VzbHk7IHRoZQog"
    "ICAgICAgICNkaXN0cmlidXRvciB0aHJlYWQgaXRlcmF0ZWQgdGhlbSBsYXRlciBhbmQgaGl0ICds"
    "aXN0IGNoYW5nZWQgc2l6ZQogICAgICAgICNkdXJpbmcgaXRlcmF0aW9uJywgd2hpY2ggdGhlIGV4"
    "Y2VwdCBhYm92ZSBzd2FsbG93ZWQgLSBzaWxlbnRseQogICAgICAgICNkcm9wcGluZyB0aGUgZW50"
    "aXJlIGJyb2FkY2FzdC4gdXBkYXRlUG9zKCkgZG9lcyB0aGlzIG9uY2UgYSBzZWNvbmQgZm9yCiAg"
    "ICAgICAgI2V2ZXJ5IGNoYW5uZWwsIHNvIHRoaXMgd2FzIHRoZSBob3QgcGF0aCBmb3IgdGhlIHJh"
    "Y2UuCiAgICAgICAgaWYgaXNpbnN0YW5jZShwcm9wcywgZGljdCk6CiAgICAgICAgICAgIHByb3Bz"
    "ID0gZGljdChwcm9wcykKICAgICAgICAgICAgcHJvcHNbJ3RhcmdldCddID0gbGlzdChwcm9wcy5n"
    "ZXQoJ3RhcmdldCcpIG9yICgpKQogICAgICAgIHNlbGYuX2NRdWV1ZS5wdXQocHJvcHMpCiAgICBk"
    "ZWYgZW5kKHNlbGYpOgogICAgICAgIHNlbGYuYWRkKHNlbGYuX0VORElURU0pCiAgICAKY2xhc3Mg"
    "R2FtZUVudHJ5KCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwgcGFyZW50LCBuYW1lLCBob3N0LCBw"
    "YXN3LCBtYXBwLCBtYXB0LCBucGosIHVuMSwgc3RhdHVzLCBtYXhwbGF5ZXJzLCB1cmwpOgogICAg"
    "ICAgIGlmIGhvc3QudXNlci5nYW1lOgogICAgICAgICAgICBob3N0LnVzZXIuZ2FtZS5yZW1vdmUo"
    "aG9zdCkKICAgICAgICBzZWxmLnBhcmVudCA9IHBhcmVudCAjIEdhbWVjaGFubmVsCiAgICAgICAg"
    "c2VsZi5nbmFtZSA9IG5hbWUgIwogICAgICAgIHNlbGYuaG9zdCA9IGhvc3QgIyBDb25uZWN0aW9u"
    "IE9iamVjdAogICAgICAgIHNlbGYucGFzc3dvcmQgPSBwYXN3ICMgJycgb3IgJ3Bhc3N3b3JkJwog"
    "ICAgICAgIHNlbGYubWFwUGFyID0gbWFwcCAjICJOZXRfTV8wMSBudWxsIDAgMSIKICAgICAgICBz"
    "ZWxmLm1hcFRyYW5zbGF0ZSA9IG1hcHQgIyAidHJhbnNsYXRlTmV0X01fMDEiCiAgICAgICAgc2Vs"
    "Zi5ucGogPSBpbnQobnBqKSAjICJlbmFibGUgbmV3IHBsYXllciB0byBqb2luIChib29sKSIKICAg"
    "ICAgICBzZWxmLnVuMSA9IGludCh1bjEpICMgMCBUT0RPIGZpZ3VyZSBvdXQgaWYgbWVhbnMgImd1"
    "aWxkIGdhbWUiCiAgICAgICAgc2VsZi5zdGF0dXMgPSBpbnQoc3RhdHVzKSAjIGNoYW5nZXMgdG8g"
    "MSB3aGVuIHN0YXJ0ZWQsIG9ubHkgcmVsZXZhbnQgd2hlbiBucGogdHJ1ZQogICAgICAgIHNlbGYu"
    "bWF4cGxheWVycyA9IGludChtYXhwbGF5ZXJzKSAjIDggI21heCB1c2Vycz8KICAgICAgICAjeC1k"
    "aXJlY3RwbGF5IHVybCwgd2l0aCB0aGUgaG9zdCdzIGFkdmVydGlzZWQgYWRkcmVzcyByZXBsYWNl"
    "ZCBieSB0aGUKICAgICAgICAjYWRkcmVzcyB0aGlzIHNlcnZlciBzZWVzIGl0IGNvbm5lY3QgZnJv"
    "bSAtIHNlZSByZXdyaXRlR2FtZUhvc3QoKS4KICAgICAgICBwZWVyID0gaG9zdC5jbGllbnRfYWRk"
    "cmVzc1swXSBpZiBob3N0LmNsaWVudF9hZGRyZXNzIGVsc2UgJycKICAgICAgICAoc2VsZi51cmws"
    "IG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0KHVybCwgcGVlcikKICAgICAgICBwcmludChmJ1tMb2Ji"
    "eV0gUm9vbSAie25hbWV9IiBieSB7aG9zdC51c2VyLm5hbWV9OiB7bm90ZX0nKQogICAgICAgIHBy"
    "aW50KGYnW0xvYmJ5XSAgIHVybCBhZHZlcnRpc2VkIHRvIGpvaW5lcnM6IHtzZWxmLnVybH0nKQog"
    "ICAgICAgIHNlbGYudXNlcmxpc3QgPSBbaG9zdCxdCiAgICAgICAgc2VsZi5wYXJlbnQuZ2FtZXNb"
    "c2VsZi5nbmFtZV0gPSBzZWxmCiAgICAgICAgc2VsZi5ob3N0LnVzZXIuZ2FtZSA9IHNlbGYKICAg"
    "ICAgICAjQWR2ZXJ0aXNlIG9uIGNyZWF0aW9uCiAgICAgICAgbXNnID0gc2VsZi5nZXRHYW1lU3Ry"
    "aW5nKCkKICAgICAgICB0ZyA9IHNlbGYucGFyZW50LnVzZXJsaXN0CiAgICAgICAgc2VsZi5wYXJl"
    "bnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bXNnfSkKICAgIGRlZiBf"
    "YXVkaWVuY2Uoc2VsZik6CiAgICAgICAgI1dobyBuZWVkcyB0byBoZWFyIGFib3V0IHRoaXMgcm9v"
    "bSBjaGFuZ2luZzogZXZlcnlvbmUgYnJvd3NpbmcgdGhlCiAgICAgICAgI3Rvd24sIHBsdXMgZXZl"
    "cnlvbmUgYWxyZWFkeSBpbnNpZGUgdGhlIHJvb20uIE9uY2UgYSBnYW1lIHN0YXJ0cyBpdHMKICAg"
    "ICAgICAjcGxheWVycyBhcmUgdGFrZW4gb2ZmIHRoZSB0b3duIHJvc3RlciAoc2VlIHN0YXJ0R2Ft"
    "ZSksIHNvIHRoZSB0b3duCiAgICAgICAgI2xpc3QgYWxvbmUgbm8gbG9uZ2VyIHJlYWNoZXMgdGhl"
    "bSAtIGFuZCB0aGUgaG9zdCwgd2hvIGlzIGFsd2F5cwogICAgICAgICNpbi1nYW1lLCBpcyBleGFj"
    "dGx5IHdobyBuZWVkcyB0byBrbm93IHRoYXQgc29tZWJvZHkgam9pbmVkLgogICAgICAgIHNlZW4g"
    "PSBsaXN0KHNlbGYucGFyZW50LnVzZXJsaXN0KQogICAgICAgIGZvciBjIGluIHNlbGYudXNlcmxp"
    "c3Q6CiAgICAgICAgICAgIGlmIGMgbm90IGluIHNlZW46CiAgICAgICAgICAgICAgICBzZWVuLmFw"
    "cGVuZChjKQogICAgICAgIHJldHVybiBzZWVuCiAgICBkZWYgYWRkVXNlcihzZWxmLCB1c3IsIHBh"
    "c3cpOgogICAgICAgICNFdmVyeSByZWplY3Rpb24gYmVsb3cgaGFzIHRvIGFuc3dlciB0aGUgY2xp"
    "ZW50IHdpdGggKnNvbWV0aGluZyouIFRoZQogICAgICAgICNjbGllbnQgc2hvd3MgImNvbm5lY3Rp"
    "bmcuLi4iIGZyb20gdGhlIG1vbWVudCBpdCBzZW5kcyAvam9pbmdhbWUgdW50aWwKICAgICAgICAj"
    "dGhlIHNlcnZlciBhbnN3ZXJzLCBhbmQgaXQgaGFzIG5vIHRpbWVvdXQgb2YgaXRzIG93bjogcmV0"
    "dXJuaW5nIE5vbmUKICAgICAgICAjbGVmdCB0aGUgcGxheWVyIHN0YXJpbmcgYXQgdGhhdCBkaWFs"
    "b2cgdW50aWwgdGhleSBraWxsZWQgdGhlIGdhbWUuCiAgICAgICAgaWYgdXNyIGluIHNlbGYudXNl"
    "cmxpc3Q6CiAgICAgICAgICAgICNBbHJlYWR5IGluIChkdXBsaWNhdGUgL2pvaW5nYW1lLCBlLmcu"
    "IHRoZSBwbGF5ZXIgZG91YmxlLWNsaWNrZWQKICAgICAgICAgICAgI3RoZSByb29tKS4gUmUtYW5z"
    "d2VyIGluc3RlYWQgb2YgYXBwZW5kaW5nIHRoZW0gYSBzZWNvbmQgdGltZS4KICAgICAgICAgICAg"
    "cmV0dXJuIF9lbShmJy9qb2luZ2FtZSAie3NlbGYuZ25hbWV9IiAie3NlbGYudXJsfSIgIntzZWxm"
    "LnN0YXR1c30iJykKICAgICAgICBpZiBsZW4oc2VsZi51c2VybGlzdCk+PXNlbGYubWF4cGxheWVy"
    "czoKICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lRnVsbCAie3NlbGYuZ25hbWV9"
    "IicpCiAgICAgICAgaWYgc2VsZi5zdGF0dXMgYW5kIG5vdCBzZWxmLm5wajoKICAgICAgICAgICAg"
    "cmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQWxyZWFkeVN0YXJ0ZWQgIntzZWxmLmduYW1lfSInKQog"
    "ICAgICAgIGlmIHNlbGYucGFzc3dvcmQgIT0gcGFzdzoKICAgICAgICAgICAgcmV0dXJuIF9lbShm"
    "Jy9lcnJvciBiYWRHYW1lUGFzc3dvcmQgIntzZWxmLmduYW1lfSInKQogICAgICAgIGlmIHVzci51"
    "c2VyLmdhbWUgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHVzci51c2VyLmdhbWUucmVtb3ZlKHVz"
    "cikgI2xlYXZlIHRoZSBwcmV2aW91cyByb29tIGNsZWFubHkgZmlyc3QKICAgICAgICBzZWxmLnVz"
    "ZXJsaXN0LmFwcGVuZCh1c3IpCiAgICAgICAgdXNyLnVzZXIuZ2FtZSA9IHNlbGYKICAgICAgICBy"
    "ZXQgPSBfZW0oZickZ2FtZXVzZXIgIntzZWxmLmduYW1lfSIgInt1c3IudXNlci5uYW1lfSIgIiIg"
    "IjEwMCIgIjAiJykKICAgICAgICAjVW5jb25kaXRpb25hbGx5LCB0byBldmVyeW9uZSBpbiB0aGUg"
    "dG93bi4gVGhpcyB1c2VkIHRvIGJlIHNlbnQgb25seQogICAgICAgICN3aGVuIG5waiAoIm5ldyBw"
    "bGF5ZXJzIG1heSBqb2luIGEgcnVubmluZyBnYW1lIikgd2FzIHNldCAtIGJ1dCBucGoKICAgICAg"
    "ICAjc2F5cyBub3RoaW5nIGFib3V0IHdobyBzaG91bGQgaGVhciBhYm91dCBhIGpvaW4sIGl0IG9u"
    "bHkgY29udHJvbHMKICAgICAgICAjd2hldGhlciBhICpzdGFydGVkKiBnYW1lIHN0YXlzIGxpc3Rl"
    "ZC4gRm9yIGFuIG9yZGluYXJ5IHJvb20sIHdoaWNoIGlzCiAgICAgICAgI2NyZWF0ZWQgd2l0aCBu"
    "cGo9MCBhbmQgam9pbmVkIGJlZm9yZSBpdCBzdGFydHMsIG5vYm9keSB3YXMgZXZlciB0b2xkOgog"
    "ICAgICAgICN0aGUgaG9zdCdzIGxvYmJ5IG5ldmVyIGxpc3RlZCB0aGUgYXJyaXZpbmcgcGxheWVy"
    "LCBzbyB0aGUgaG9zdCBoYWQKICAgICAgICAjbm9ib2R5IHRvIHN0YXJ0IHRoZSBnYW1lIHdpdGgs"
    "IGFuZCB0aGUgam9pbmVyIHNhdCBpbiAiY29ubmVjdGluZyIKICAgICAgICAjZm9yZXZlciB3YWl0"
    "aW5nIGZvciBhIHN0YXJ0IHRoYXQgY291bGQgbm90IGNvbWUuCiAgICAgICAgdXNyLnNlcnZlci5k"
    "aXN0LmFkZCh7J3RhcmdldCc6c2VsZi5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6cmV0fSkKICAgICAg"
    "ICByZXR1cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi51cmx9IiAie3Nl"
    "bGYuc3RhdHVzfSInKQogICAgZGVmIGRlc3Ryb3koc2VsZik6CiAgICAgICAgI1RlYXIgdGhlIHJv"
    "b20gZG93biBjb21wbGV0ZWx5OiBldmVyeW9uZSBzdGlsbCBsaXN0ZWQgaW4gaXQgaXMgcHV0CiAg"
    "ICAgICAgI2JhY2sgdG8gIm5vdCBpbiBhIGdhbWUiLCBhbmQgdGhlIHJvb20gc3RvcHMgYmVpbmcg"
    "YWR2ZXJ0aXNlZC4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBmb3IgYyBp"
    "biBsaXN0KHNlbGYudXNlcmxpc3QpOgogICAgICAgICAgICBpZiBjLnVzZXI6CiAgICAgICAgICAg"
    "ICAgICBjLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBzZWxmLnVzZXJsaXN0ID0gW10KICAgICAg"
    "ICBpZiBzZWxmLnBhcmVudC5nYW1lcy5nZXQoc2VsZi5nbmFtZSkgaXMgc2VsZjoKICAgICAgICAg"
    "ICAgZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdCiAgICAgICAgc2VsZi5wYXJlbnQu"
    "c2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywKICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICdtZXNzYWdlJzpfZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpfSkKICAgIGRl"
    "ZiByZW1vdmUoc2VsZiwgY29uPU5vbmUpOiNUT0RPIHJlY3JlYXRlIHByb3Blcmx5CiAgICAgICAg"
    "aWYgY29uIGlzIE5vbmUgb3IgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICBy"
    "ZXR1cm4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBzZWxmLnVzZXJsaXN0"
    "LnJlbW92ZShjb24pCiAgICAgICAgbGVhdmVtc2cgPSBfZW0oZicmZ2FtZXVzZXIgIntjb24udXNl"
    "ci5uYW1lfSInKQogICAgICAgIGNvbi51c2VyLmdhbWUgPSBOb25lCiAgICAgICAgaWYgY29uIGlz"
    "IHNlbGYuaG9zdDoKICAgICAgICAgICAgI1RoZSBob3N0ICppcyogdGhlIGdhbWUgc2Vzc2lvbjog"
    "dGhlIGNvLW9wIHdvcmxkIHJ1bnMgb24gdGhlaXIKICAgICAgICAgICAgI21hY2hpbmUgYW5kIHRo"
    "ZSByb29tJ3MgRGlyZWN0UGxheSB1cmwgcG9pbnRzIGF0IGl0LiBPbmNlIHRoZXkgYXJlCiAgICAg"
    "ICAgICAgICNnb25lIHRoZSByb29tIGNhbm5vdCBiZSBqb2luZWQgYnkgYW55Ym9keSwgYnV0IGl0"
    "IHVzZWQgdG8gc3RheQogICAgICAgICAgICAjbGlzdGVkIC0gc28gdGhlIG5leHQgcGxheWVyIHRv"
    "IGNsaWNrIGl0IGdvdCBhIHVybCB0byBhIGdhbWUgdGhhdAogICAgICAgICAgICAjbm8gbG9uZ2Vy"
    "IGV4aXN0ZWQgYW5kIHNhdCBvbiAiY29ubmVjdGluZyIgdW50aWwgdGhleSBnYXZlIHVwLgogICAg"
    "ICAgICAgICAjVGhpcyBpcyB3aGF0IGEgaG9zdCBjcmFzaCBsZWF2ZXMgYmVoaW5kLgogICAgICAg"
    "ICAgICBwcmludChmJ1tMb2JieV0gSG9zdCB7Y29uLnVzZXIubmFtZX0gbGVmdCByb29tICJ7c2Vs"
    "Zi5nbmFtZX0iLCBjbG9zaW5nIGl0JykKICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRp"
    "c3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgICAgICAgICBzZWxm"
    "LmRlc3Ryb3koKQogICAgICAgICAgICByZXR1cm4KICAgICAgICAjaWYgMCB1c2VycyBsZWZ0LCBy"
    "ZW1vdmUgZ2FtZQogICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KT09MDoKICAgICAgICAgICAg"
    "bGVhdmVtc2cgPSBfZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgICAgIGRlbCBz"
    "ZWxmLnBhcmVudC5nYW1lc1tzZWxmLmduYW1lXQogICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5k"
    "aXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgIGRlZiBzdGFydEdh"
    "bWUoc2VsZiwgdXNlcj1Ob25lKToKICAgICAgICBpZiBub3QgKHVzZXIgYW5kIHNlbGYuaG9zdCA9"
    "PSB1c2VyKToKICAgICAgICAgICAgcmV0dXJuIE5vbmUgI3VzZXIgbm90IGhvc3QKICAgICAgICB0"
    "ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBzZWxmLnN0YXR1cyA9IDEKICAgICAgICBmb3Ig"
    "YyBpbiBzZWxmLnVzZXJsaXN0OiNUT0RPIGhhdmUgdXNlciByZW1vdmUgaXRzZWxmIHdoZW4gL3N0"
    "YXJ0aW5nZ2FtZT8KICAgICAgICAgICAgdW4gPSBjLnVzZXIubmFtZQogICAgICAgICAgICAjVE9E"
    "TyBjb25zaWRlciByZW1vdmluZyB1c2VyIGZyb20gdGFyZ2V0IG93biBzZXQ/CiAgICAgICAgICAg"
    "IHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOl9lbShm"
    "JyZjaGF0Y2hhbm5lbHVzZXIgInt1bn0iJykrX2VtKGYnJmdhbWVjaGFubmVsdXNlciAie3VufSIn"
    "KX0pCiAgICAgICAgIy4uLmFuZCBhY3R1YWxseSB0YWtlIHRoZW0gb2ZmIHRoZSB0b3duIHJvc3Rl"
    "ciwgd2hpY2ggdGhpcyBvbmx5IGV2ZXIKICAgICAgICAjKmFubm91bmNlZCouIExlYXZpbmcgdGhl"
    "bSBsaXN0ZWQgbWVhbnQgdGhlIHNlcnZlciBzdGlsbCBjb3VudGVkIHRoZW0KICAgICAgICAjYXMg"
    "c3RhbmRpbmcgaW4gdGhlIHRvd24gZm9yIHRoZSB3aG9sZSBzZXNzaW9uOiB0b3duIHBvcHVsYXRp"
    "b24gd2FzCiAgICAgICAgI3dyb25nLCBhbmQgZXZlcnkgcG9zaXRpb24gdXBkYXRlIGZyb20gYW55"
    "b25lIHN0aWxsIHdhbGtpbmcgYXJvdW5kIHdhcwogICAgICAgICNmYW5uZWQgb3V0IHRvIHBsYXll"
    "cnMgd2hvIHdlcmUgYXdheSBpbiBhIGNvLW9wIHdvcmxkIGFuZCBjb3VsZCBkbwogICAgICAgICNu"
    "b3RoaW5nIHdpdGggaXQuIFRoZSBjbGllbnRzIHdlcmUgdG9sZCB0aGV5IGxlZnQ7IG5vdyB0aGUg"
    "c2VydmVyCiAgICAgICAgI2FncmVlcyB3aXRoIHRoZW0uCiAgICAgICAgZm9yIGMgaW4gbGlzdChz"
    "ZWxmLnVzZXJsaXN0KToKICAgICAgICAgICAgYy51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAgICAg"
    "IGlmIGMgaW4gc2VsZi5wYXJlbnQudXNlcmxpc3Q6CiAgICAgICAgICAgICAgICBzZWxmLnBhcmVu"
    "dC51c2VybGlzdC5yZW1vdmUoYykKICAgICAgICBpZiBub3Qgc2VsZi5ucGo6CiAgICAgICAgICAg"
    "ICNnYW1lIG5vIGxvbmdlciBqb2luYWJsZS92aXNpYmxlIG9uY2Ugc3RhcnRlZAogICAgICAgICAg"
    "ICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzpfZW0o"
    "ZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpfSkKICAgICAgICAjbm90aWZ5IHBsYXllcnMgaW4gdGhl"
    "IGdhbWUgdGhhdCBpdCBoYXMgc3RhcnRlZAogICAgICAgIGZvciBjIGluIHNlbGYudXNlcmxpc3Q6"
    "CiAgICAgICAgICAgIGlzSG9zdCA9IDEgaWYgYyBpcyBzZWxmLmhvc3QgZWxzZSAwCiAgICAgICAg"
    "ICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6KGMsKSwnbWVzc2FnZSc6"
    "X2VtKGYnL3N0YXJ0Z2FtZSAiMSIgIntpc0hvc3R9IiAiMSInKX0pCiAgICAgICAgcmV0dXJuIE5v"
    "bmUKICAgIGRlZiBfZ2V0VXNlcmxpc3Qoc2VsZik6CiAgICAgICAgcmV0dXJuICcgJy5qb2luKCAo"
    "Zicie2MudXNlci5uYW1lfSIgIiIgIjEwMCIgIjAiJyBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0KSAp"
    "CiAgICBkZWYgZ2V0R2FtZVN0cmluZyhzZWxmKToKICAgICAgICBpZiBzZWxmLnN0YXR1cyBhbmQg"
    "bm90IHNlbGYubnBqOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjR2FtZSBkb2VzIG5vdCBzaG93"
    "IGlmIG5ldyBwbGF5ZXJzIGNhbid0IGpvaW4gd2hlbiBhY3RpdmUKICAgICAgICBwYXN3ID0gJycK"
    "ICAgICAgICBpZiBzZWxmLnBhc3N3b3JkOgogICAgICAgICAgICBwYXN3ID0gJ1hYWCcKICAgICAg"
    "ICByZXR1cm4gX2VtKGYnJGdhbWUgIntzZWxmLmduYW1lfSIgIntwYXN3fSIgIntzZWxmLm1hcFBh"
    "cn0iICJ7c2VsZi5tYXBUcmFuc2xhdGV9IiAie3NlbGYudW4xfSIgIntzZWxmLnN0YXR1c30iICJ7"
    "c2VsZi5tYXhwbGF5ZXJzfSIge3NlbGYuX2dldFVzZXJsaXN0KCl9JykKICAgIGRlZiBkZWJ1Z19k"
    "aWN0KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICduYW1lJzpzZWxmLmduYW1l"
    "LAogICAgICAgICAgICAnaG9zdCc6c2VsZi5ob3N0LnVzZXIubmFtZSwKICAgICAgICAgICAgJ3N0"
    "YXR1cyc6c2VsZi5zdGF0dXMsCiAgICAgICAgICAgICdoYXNQYXNzd29yZCc6MSBpZiBzZWxmLnBh"
    "c3N3b3JkIGVsc2UgMCwKICAgICAgICAgICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUgZm9y"
    "IGMgaW4gc2VsZi51c2VybGlzdF0pLAogICAgICAgICAgICAndG93bic6c2VsZi5wYXJlbnQubmFt"
    "ZSwKICAgICAgICAgICAgJ3BhcmFtZXRlcnMnOnNlbGYubWFwUGFyLAogICAgICAgICAgICAnbWFw"
    "TmFtZSc6c2VsZi5tYXBUcmFuc2xhdGUsCiAgICAgICAgICAgICdjYW5Kb2luUnVubmluZyc6c2Vs"
    "Zi5ucGoKICAgICAgICB9CiMgdHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsCiMgdHJhbnNsYXRl"
    "TmV0Q2l0eVRyYWRlQ2hhbm5lbAojIHRyYW5zbGF0ZU5ldENpdHlDaGF0Q2hhbm5lbApfREVGQVVM"
    "VF9DSEFUUyA9IFsndHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsJywndHJhbnNsYXRlTmV0Q2l0"
    "eVRyYWRlQ2hhbm5lbCddCmNsYXNzIEdhbWVDaGFubmVsKCk6CiAgICBtYXh1c2VyID0gNTAgI1RP"
    "RE8gY29uZmlndXJlYWJsZQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlciwgY2huTmFtZSk6"
    "CiAgICAgICAgc2VsZi5zZXJ2ZXIgPSBzZXJ2ZXIKICAgICAgICBzZWxmLm5hbWUgPSBjaG5OYW1l"
    "CiAgICAgICAgc2VsZi51c2VybGlzdCA9IFtdCiAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHMgPSB7"
    "fQogICAgICAgIHNlbGYuZ2FtZXMgPSB7fSAjVE9ETyBmaWd1cmUgb3V0IEEgYW5kIEIgdmFsdWUg"
    "Zm9yIGRpc3BsYXkKICAgICAgICAjVE9ETyByZXF1ZXN0IGpvaW4gcmVzZXJ2ZXMgc3BhY2Ugd2l0"
    "aCB3ZWFrIHJlZmVyZW5jZXMKICAgICAgICAjLSB3ZWFrIHZhbHVlIHJlZiBzaG91bGQgZW5zdXJl"
    "IHRoYXQgY29ubmVjdGlvbiBpcyByZW1vdmVkIGZyb20gcXVldWUgaWYgaXQgZGlzY29ubmVjdHMg"
    "ZHVyaW5nIHRoZSBqb2luIHByb2Nlc3MKICAgICAgICBzZWxmLnJlcXVlc3RlZCA9IFtdCiAgICAg"
    "ICAgc2VsZi5nYW1lUmVxdWVzdHMgPSB7fQogICAgICAgIHNlbGYuZGlydHkgPSBGYWxzZQogICAg"
    "ICAgIGZvciBjbiBpbiBfREVGQVVMVF9DSEFUUzoKICAgICAgICAgICAgc2VsZi5jaGF0Q2hhbm5l"
    "bHNbY25dID0gW10gI1VzZXJsaXN0CiAgICBkZWYgcmVxdWVzdEpvaW4oc2VsZiwgY29uKToKICAg"
    "ICAgICAjbGVhdmVDaGFubmVsKCkgYWxyZWFkeSByZWxlYXNlcyBhbnkgb3V0c3RhbmRpbmcgcmVz"
    "ZXJ2YXRpb24sIG9uIHRoaXMKICAgICAgICAjY2hhbm5lbCBvciBhbm90aGVyIG9uZS4gVGhlIGZv"
    "bGxvdy11cCBibG9jayB0aGF0IHVzZWQgdG8gc3RhbmQgaGVyZQogICAgICAgICNjb3VsZCB0aGVy"
    "ZWZvcmUgbmV2ZXIgcnVuIC0gYW5kIGlmIGl0IGV2ZXIgaGFkLCBpdHMgdW5ndWFyZGVkCiAgICAg"
    "ICAgI2xpc3QucmVtb3ZlKCkgd291bGQgaGF2ZSByYWlzZWQgVmFsdWVFcnJvciBmb3IgYSByZXNl"
    "cnZhdGlvbiB0aGF0IHdhcwogICAgICAgICNhbHJlYWR5IGdvbmUuCiAgICAgICAgY29uLnVzZXIu"
    "bGVhdmVDaGFubmVsKCkKICAgICAgICBlbGVuID0gbGVuKHNlbGYudXNlcmxpc3QpK2xlbihzZWxm"
    "LnJlcXVlc3RlZCkKICAgICAgICBpZiBlbGVuPHNlbGYubWF4dXNlcjoKICAgICAgICAgICAgc2Vs"
    "Zi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkQ2hh"
    "bm5lbCA9IHNlbGYKICAgICAgICAgICAgcmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gRmFsc2UK"
    "ICAgIGRlZiBfaXNTdGFsZUdhbWUoc2VsZiwgZ2VudCwgY29uKToKICAgICAgICAjQSByb29tIHdo"
    "b3NlIGhvc3QgaXMgbm8gbG9uZ2VyIHRoZSBsaXZlIHNlc3Npb24gZm9yIHRoYXQgYWNjb3VudC4g"
    "VGhlCiAgICAgICAgI2NsaWVudCBuYW1lcyBhIHJvb20gYWZ0ZXIgaXRzIGhvc3QsIHNvIHdoZW4g"
    "YSBwbGF5ZXIgd2hvc2UgZ2FtZQogICAgICAgICNjcmFzaGVkIHJlY29ubmVjdHMgYW5kIGhvc3Rz"
    "IGFnYWluLCB0aGUgcm9vbSBmcm9tIHRoZSBzZXNzaW9uIHRoYXQKICAgICAgICAjZGllZCBpcyBz"
    "dGlsbCBzaXR0aW5nIGhlcmUgdW5kZXIgdGhlIHNhbWUgbmFtZSAtIHdpdGggYSBob3N0CiAgICAg"
    "ICAgI2Nvbm5lY3Rpb24gdGhhdCBubyBsb25nZXIgZXhpc3RzIGFuZCBhIERpcmVjdFBsYXkgdXJs"
    "IHBvaW50aW5nIGF0IGEKICAgICAgICAjZ2FtZSB0aGF0IGlzIGdvbmUuIEFueW9uZSBqb2luaW5n"
    "IGl0IHdhaXRzIGZvcmV2ZXIuCiAgICAgICAgaWYgZ2VudC5ob3N0IGlzIGNvbjoKICAgICAgICAg"
    "ICAgcmV0dXJuIFRydWUKICAgICAgICBob3N0bmFtZSA9IGdlbnQuaG9zdC51c2VyLm5hbWUgaWYg"
    "Z2VudC5ob3N0LnVzZXIgZWxzZSBOb25lCiAgICAgICAgaWYgaG9zdG5hbWUgaXMgTm9uZToKICAg"
    "ICAgICAgICAgcmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gc2VsZi5zZXJ2ZXIuZ2V0UGxheWVy"
    "KGhvc3RuYW1lKSBpcyBub3QgZ2VudC5ob3N0CiAgICBkZWYgcmVxdWVzdENyZWF0ZUdhbWUoc2Vs"
    "ZiwgY29uLCBnYW1lTmFtZSk6CiAgICAgICAgI05ldmVyIHJldHVybiBhIGJhcmUgRmFsc2UgZnJv"
    "bSBoZXJlLiBwYXJzZSgpIHRyZWF0cyBhIGZhbHN5IHJlc3VsdCBhcwogICAgICAgICMibm90aGlu"
    "ZyB0byBzZW5kIiwgc28gZXZlcnkgcmVqZWN0aW9uIGJlbG93IHVzZWQgdG8gbGVhdmUgdGhlIGNs"
    "aWVudAogICAgICAgICN3YWl0aW5nIG9uIGFuIGFuc3dlciB0aGF0IG5ldmVyIGNhbWUgLSB0aGUg"
    "cm9vbS1jcmVhdGlvbiBkaWFsb2cgdGhlbgogICAgICAgICNzcGlucyBmb3JldmVyLgogICAgICAg"
    "IGlmIGNvbi51c2VyLnJlcXVlc3RlZEdhbWUgb3IgY29uLnVzZXIuZ2FtZToKICAgICAgICAgICAg"
    "Y29uLnVzZXIuc3RvcEdhbWUoKQogICAgICAgIHRjbiA9IHNlbGYuZ2FtZVJlcXVlc3RzLmdldChn"
    "YW1lTmFtZSkKICAgICAgICBpZiB0Y24gaXMgbm90IE5vbmUgYW5kIHRjbiBpcyBub3QgY29uOgog"
    "ICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVOYW1lVGFrZW4gIntnYW1lTmFtZX0i"
    "JykKICAgICAgICAgICAgI2Vsc2UgdGNuIGlzIGNvbiwgcmUtcmVxdWVzdGVkIGNyZWF0aW9uCiAg"
    "ICAgICAgZ2VudCA9IHNlbGYuZ2FtZXMuZ2V0KGdhbWVOYW1lKQogICAgICAgIGlmIGdlbnQgaXMg"
    "bm90IE5vbmU6CiAgICAgICAgICAgIGlmIHNlbGYuX2lzU3RhbGVHYW1lKGdlbnQsIGNvbik6CiAg"
    "ICAgICAgICAgICAgICBwcmludChmJ1tMb2JieV0gUmVwbGFjaW5nIHN0YWxlIHJvb20gIntnYW1l"
    "TmFtZX0iICcKICAgICAgICAgICAgICAgICAgICAgIGYnKGhvc3Qgc2Vzc2lvbiBnb25lKSBhdCB0"
    "aGUgcmVxdWVzdCBvZiB7Y29uLnVzZXIubmFtZX0nKQogICAgICAgICAgICAgICAgZ2VudC5kZXN0"
    "cm95KCkKICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJy"
    "b3IgZ2FtZU5hbWVUYWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgIHNlbGYuZ2FtZVJlcXVlc3Rz"
    "W2dhbWVOYW1lXSA9IGNvbgogICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZEdhbWUgPSBnYW1lTmFt"
    "ZQogICAgICAgIHJldHVybiBfZW0oZicvY3JlYXRlZ2FtZSAie2dhbWVOYW1lfSInKQogICAgZGVm"
    "IGNyZWF0ZUdhbWUoc2VsZiwgZ2FtZU5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwg"
    "dW4xLCB1bjIsIHVuMywgdXJsKToKICAgICAgICByZXFIb3N0ID0gc2VsZi5nYW1lUmVxdWVzdHMu"
    "Z2V0KGdhbWVOYW1lKQogICAgICAgIGlmIHJlcUhvc3QgaXMgTm9uZSBvciByZXFIb3N0IGlzIG5v"
    "dCBob3N0OgogICAgICAgICAgICAjU2FtZSByZWFzb25pbmcgYXMgYWJvdmU6IGFuc3dlciwgbmV2"
    "ZXIgZmFsbCBzaWxlbnQuCiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5hbWVU"
    "YWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgIGdlbnQgPSBHYW1lRW50cnkoc2VsZiwgZ2FtZU5h"
    "bWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4xLCB1bjIsIHVuMywgdXJsKQogICAg"
    "ICAgIHJlcUhvc3QudXNlci5yZXF1ZXN0ZWRHYW1lID0gTm9uZSAjVE9ETyByZW9nYW5pemUgYmV0"
    "dGVyCiAgICAgICAgZGVsIHNlbGYuZ2FtZVJlcXVlc3RzW2dhbWVOYW1lXQogICAgICAgIHJldHVy"
    "biBOb25lCiAgICBkZWYgbGVhdmVDaGFubmVsKHNlbGYsIGNvbik6CiAgICAgICAgI1RoZSBjbGVh"
    "bnVwIHJ1bnMgd2hldGhlciBvciBub3QgdGhlIHBsYXllciBpcyBzdGlsbCBvbiB0aGUgdG93bgog"
    "ICAgICAgICNyb3N0ZXIuIFNpbmNlIHN0YXJ0R2FtZSgpIHRha2VzIGl0cyBwbGF5ZXJzIG9mZiB0"
    "aGF0IHJvc3RlciwgYQogICAgICAgICNwbGF5ZXIgd2hvIGxlYXZlcyAob3IgZGlzY29ubmVjdHMp"
    "IGZyb20gaW5zaWRlIGEgcnVubmluZyBnYW1lIHVzZWQgdG8KICAgICAgICAjc2tpcCBhbGwgb2Yg"
    "dGhpczogdGhlaXIgcm9vbSB3YXMgbmV2ZXIgbGVmdCwgdGhlaXIgY2hhdCBjaGFubmVsIGtlcHQK"
    "ICAgICAgICAjdGhlaXIgZW50cnksIGFuZCBnYW1lY2hhbm5lbCBzdGF5ZWQgcG9pbnRpbmcgYXQg"
    "YSB0b3duIHRoZXkgd2VyZSBubwogICAgICAgICNsb25nZXIgaW4uIE9ubHkgdGhlIHJvc3RlciBy"
    "ZW1vdmFsIGFuZCB0aGUgYW5ub3VuY2VtZW50IGFyZQogICAgICAgICNjb25kaXRpb25hbCBub3cg"
    "LSBiZWNhdXNlIG9ubHkgdGhvc2UgZGVwZW5kIG9uIGJlaW5nIGxpc3RlZC4KICAgICAgICBsaXN0"
    "ZWQgPSBjb24gaW4gc2VsZi51c2VybGlzdAogICAgICAgIGNvbi51c2VyLnN0b3BHYW1lKCkKICAg"
    "ICAgICBjb24udXNlci5sZWF2ZUNoYXQoKQogICAgICAgIGlmIGxpc3RlZDoKICAgICAgICAgICAg"
    "c2VsZi51c2VybGlzdC5yZW1vdmUoY29uKQogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZn"
    "YW1lY2hhbm5lbHVzZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24uc2VydmVy"
    "LmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLnVzZXJsaXN0LCdtZXNzYWdlJzpsZWF2ZW1zZ30pCiAg"
    "ICAgICAgY29uLnVzZXIuZ2FtZWNoYW5uZWw9Tm9uZQogICAgZGVmIGxlYXZlQ2hhdChzZWxmLCBj"
    "b24pOiAjVE9ETyBiZXR0ZXIgY2hhdGNoYW5uZWwgb2JqZWN0IGFuZCBtb3ZlIGl0IHRoZXJlLgog"
    "ICAgICAgIGNvbi51c2VyLmxlYXZlQ2hhdCgpCiAgICAjVE9ETyBjaGFuZ2UgdGhlc2UgZnVuY3Rp"
    "b25zIHRvIGFsc28gaGFuZGxlIG1lc3NhZ2UgZm9ybWluZwogICAgZGVmIGpvaW5DaGFubmVsKHNl"
    "bGYsIGNvbiwgbmFtKTojbW92ZXMgdXNlciBmcm9tIHF1ZXVlIHRvIHVzZXJsaXN0CiAgICAgICAg"
    "aWYgY29uIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICNEdXBsaWNhdGUgL2pvaW5nYW1l"
    "Y2hhbm5lbCBmb3IgYSB0b3duIHdlIGFyZSBhbHJlYWR5IGluLiBSZWJ1aWxkCiAgICAgICAgICAg"
    "ICN0aGUgcmVzZXJ2YXRpb24gc28gdGhlIHJlcXVlc3QgYmVsb3cgcmUtcnVucyB0aGUgZnVsbCBl"
    "bnVtZXJhdGlvbgogICAgICAgICAgICAjYW5kIHRoZSBjbGllbnQgZ2V0cyBhIGNvbXBsZXRlIGFu"
    "c3dlciByYXRoZXIgdGhhbiBzaWxlbmNlLgogICAgICAgICAgICBzZWxmLnVzZXJsaXN0LnJlbW92"
    "ZShjb24pCiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAgICAg"
    "IGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgaWYgY29uIG5vdCBpbiBz"
    "ZWxmLnJlcXVlc3RlZCBhbmQgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICAj"
    "Tm8gb3V0c3RhbmRpbmcgcmVzZXJ2YXRpb24uIFRoZSByZXNlcnZhdGlvbiBpcyBkcm9wcGVkIGJ5"
    "IGFueQogICAgICAgICAgICAjaW50ZXJ2ZW5pbmcgbGVhdmVDaGFubmVsKCkvcmVxdWVzdEpvaW4o"
    "KSBhbmQgYnkgYSByZWNvbm5lY3QsIHNvIGEKICAgICAgICAgICAgI2NsaWVudCB0aGF0IGdvZXMg"
    "c3RyYWlnaHQgdG8gL2pvaW5nYW1lY2hhbm5lbCAtIG9yIHdob3NlIGVhcmxpZXIKICAgICAgICAg"
    "ICAgIy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsIHJhY2VkIGl0cyBvd24gY2xlYW51cCAtIHVzZWQg"
    "dG8gZ2V0IG5vCiAgICAgICAgICAgICNhbnN3ZXIgYXQgYWxsIGFuZCBoYW5nIG9uIHRoZSBsb2Fk"
    "aW5nIHNjcmVlbi4gQWRtaXQgdGhlbSBpZiB0aGUKICAgICAgICAgICAgI3Rvd24gaGFzIHJvb207"
    "IG9ubHkgYSBnZW51aW5lbHkgZnVsbCB0b3duIGlzIHJlZnVzZWQgbm93LgogICAgICAgICAgICBp"
    "ZiBsZW4oc2VsZi51c2VybGlzdCkrbGVuKHNlbGYucmVxdWVzdGVkKSA8IHNlbGYubWF4dXNlcjoK"
    "ICAgICAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAgICAgICAg"
    "ICBjb24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gc2VsZgogICAgICAgICAgICBlbHNlOgogICAg"
    "ICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQ2hhbm5lbEZ1bGwgIntuYW19Iicp"
    "CiAgICAgICAgaWYgY29uIGluIHNlbGYucmVxdWVzdGVkOgogICAgICAgICAgICAjVE9ETyB2ZXJp"
    "Znkgb3JkZXIgb2Ygb3BlcmF0aW9ucyBhbmQgcG9zc2libGUgdGltaW5nIGlzc3VlcwogICAgICAg"
    "ICAgICBzZWxmLnVzZXJsaXN0LmFwcGVuZChjb24pCiAgICAgICAgICAgIGNvbi51c2VyLmdhbWVj"
    "aGFubmVsID0gc2VsZgogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZC5yZW1vdmUoY29uKQogICAg"
    "ICAgICAgICBjb24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gTm9uZSAjVE9ETyBvcmdhbml6ZSBi"
    "ZXR0ZXI/CiAgICAgICAgICAgIHVsID0gbGVuKHNlbGYudXNlcmxpc3QpCiAgICAgICAgICAgIHJl"
    "dG1zZyA9IF9lbShmJy9qb2luZ2FtZWNoYW5uZWwgIntuYW19IiAie3VsfSInKQogICAgICAgICAg"
    "ICAjZW51bWVyYXRlIGhlcm9kYXRhIG9mIGV4aXN0aW5nIHVzZXJzCiAgICAgICAgICAgIGNodW5r"
    "cyA9IFtdCiAgICAgICAgICAgIGZvciB1c2VyIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAg"
    "ICAgICBpZiB1c2VyID09IGNvbjoKICAgICAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAg"
    "ICAgICAgICAgY2h1bmtzLmFwcGVuZCh1c2VyLnVzZXIuZ2V0R0NVbXNnKCkpCiAgICAgICAgICAg"
    "IHJldG1zZys9IGInJy5qb2luKGNodW5rcykKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5qb2lu"
    "Q2hhdChjb24sIF9ERUZBVUxUX0NIQVRTWzBdKQogICAgICAgICAgICByZXRtc2crPSBzZWxmLmVu"
    "dW1DaGF0cygpCiAgICAgICAgICAgIHJldG1zZys9IHNlbGYuZW51bUdhbWVzKCkKICAgICAgICAg"
    "ICAgI2Jyb2FkY2FzdCBoZXJvZGF0YSB0byBvdGhlciBleGlzdGluZyB1c2VycwogICAgICAgICAg"
    "ICBjb24uc2VydmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgICAgICd0YXJnZXQnOl93b1VzZXIo"
    "c2VsZi51c2VybGlzdCwgY29uKSwKICAgICAgICAgICAgICAgICdtZXNzYWdlJzpjb24udXNlci5n"
    "ZXRHQ1Vtc2coKX0pCiAgICAgICAgICAgIHJldHVybiByZXRtc2cKICAgICAgICByZXR1cm4gTm9u"
    "ZQogICAgZGVmIGpvaW5DaGF0KHNlbGYsIGNvbiwgbmFtLCBwYXM9JycpOgogICAgICAgICNUT0RP"
    "IHBhc3N3b3JkIHN1cHBvcnQ/CiAgICAgICAgIy0gcmVxdWlyZXMgcmVzdHJ1Y3R1cmUgZnJvbSBs"
    "aXN0IHRvIGNoYW5uZWwgb2JqZWN0cwogICAgICAgIGlmIG5vdCBuYW0gaW4gc2VsZi5jaGF0Q2hh"
    "bm5lbHM6CiAgICAgICAgICAgIHJldHVybiBiJycKICAgICAgICBjb24udXNlci5sZWF2ZUNoYXQo"
    "KQogICAgICAgICNUT0RPIGNoZWNrIGlmIGNsaWVudCBhdXRvLXB1cmdlcyBjaGF0bGlzdAogICAg"
    "ICAgICNUT0RPIENIRUNLIC1WLSBicm9hZGNhc3QgcmVsZXZhbnQgY2hhbmdlcz8KICAgICAgICBj"
    "b24uc2VydmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgJ3RhcmdldCc6bGlzdChzZWxmLmNoYXRD"
    "aGFubmVsc1tuYW1dKSwKICAgICAgICAgICAgJ21lc3NhZ2UnOl9lbShmJyRjaGF0Y2hhbm5lbHVz"
    "ZXIgIntjb24udXNlci5uYW1lfSInKX0pCiAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHNbbmFtXS5h"
    "cHBlbmQoY29uKQogICAgICAgIGNvbi51c2VyLmNoYXRjaGFubmVsID0gc2VsZi5jaGF0Q2hhbm5l"
    "bHNbbmFtXQogICAgICAgIHVsID0gMSNsZW4oY29uLnVzZXIuY2hhdGNoYW5uZWwpCiAgICAgICAg"
    "cmV0bXNnID0gX2VtKGYnL2pvaW5jaGF0Y2hhbm5lbCAie25hbX0iICIiICJ7dWx9IicpCiAgICAg"
    "ICAgI2VudW1lcmF0ZSBvdGhlciBjaGF0IHVzZXJzPwogICAgICAgIGNodW5rcyA9IFtdCiAgICAg"
    "ICAgZm9yIHVjb24gaW4gY29uLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgIGlmIHVjb24g"
    "IT0gY29uOgogICAgICAgICAgICAgICAgY2h1bmtzLmFwcGVuZChfZW0oZickY2hhdGNoYW5uZWx1"
    "c2VyICJ7dWNvbi51c2VyLm5hbWV9IicpKQogICAgICAgIHJldG1zZys9YicnLmpvaW4oY2h1bmtz"
    "KQogICAgICAgIHJldHVybiByZXRtc2cKICAgIGRlZiBlbnVtQ2hhdHMoc2VsZik6CiAgICAgICAg"
    "Y2h1bmtzID0gW10KICAgICAgICBmb3IgY2hhdE5hbWUgaW4gc2VsZi5jaGF0Q2hhbm5lbHM6CiAg"
    "ICAgICAgICAgIHVsbCA9IGxlbihzZWxmLmNoYXRDaGFubmVsc1tjaGF0TmFtZV0pI1RPRE8gaW1w"
    "cm92ZQogICAgICAgICAgICBjaHVua3MuYXBwZW5kKHdpcmVfZW5jb2RlKGYnJGNoYXRjaGFubmVs"
    "ICJ7Y2hhdE5hbWV9IiAiIiAie3VsbH0iJykpCiAgICAgICAgcmV0dXJuIF9OLmpvaW4oY2h1bmtz"
    "KStfTgogICAgZGVmIGVudW1HYW1lcyhzZWxmKToKICAgICAgICBjaHVua3MgPSBbXQogICAgICAg"
    "IGZvciBnbmFtZSBpbiBzZWxmLmdhbWVzOgogICAgICAgICAgICBnYW1lc3RyID0gc2VsZi5nYW1l"
    "c1tnbmFtZV0uZ2V0R2FtZVN0cmluZygpCiAgICAgICAgICAgIGlmIGdhbWVzdHI6CiAgICAgICAg"
    "ICAgICAgICBjaHVua3MuYXBwZW5kKGdhbWVzdHIpCiAgICAgICAgcmV0dXJuIGInJy5qb2luKGNo"
    "dW5rcykKICAgIGRlZiB1cGRhdGVQb3Moc2VsZiwgbWQpOgogICAgICAgIGlmIG5vdCBzZWxmLmRp"
    "cnR5OgogICAgICAgICAgICByZXR1cm4KICAgICAgICAjQ2xlYXJlZCBCRUZPUkUgdGhlIHNjYW4s"
    "IG5vdCBhZnRlci4gQSAvdXBkaGVyb3BvcyB0aGF0IGFycml2ZWQgd2hpbGUKICAgICAgICAjdGhl"
    "IGxvb3AgYmVsb3cgd2FzIHJ1bm5pbmcgdXNlZCB0byBzZXQgZGlydHk9VHJ1ZSBhbmQgdGhlbiBo"
    "YXZlIGl0CiAgICAgICAgI2ltbWVkaWF0ZWx5IGNsZWFyZWQgYWdhaW4sIHNvIHRoYXQgcGxheWVy"
    "J3MgbW92ZSB3YXMgbm90IGJyb2FkY2FzdAogICAgICAgICN1bnRpbCBzb21lYm9keSBlbHNlIGhh"
    "cHBlbmVkIHRvIG1vdmUuIENsZWFyaW5nIGZpcnN0IG1lYW5zIHRoZSB3b3JzdAogICAgICAgICNj"
    "YXNlIGlzIG9uZSByZWR1bmRhbnQgcGFzcywgbm90IGEgc2lsZW50bHkgZHJvcHBlZCBwb3NpdGlv"
    "bi4KICAgICAgICBzZWxmLmRpcnR5ID0gRmFsc2UKICAgICAgICAjU25hcHNob3Q6IHBsYXllcnMg"
    "am9pbiBhbmQgbGVhdmUgdGhlIHRvd24gd2hpbGUgdGhpcyBpdGVyYXRlcy4KICAgICAgICB0ZyA9"
    "IGxpc3Qoc2VsZi51c2VybGlzdCkKICAgICAgICBtb3ZlcnMgPSBbXQogICAgICAgIGZvciB1Y29u"
    "IGluIHRnOgogICAgICAgICAgICBpZiBub3QgdWNvbi51c2VyLnBvc2NoYW5nZWQ6CiAgICAgICAg"
    "ICAgICAgICBjb250aW51ZQogICAgICAgICAgICB1Y29uLnVzZXIucG9zY2hhbmdlZCA9IEZhbHNl"
    "CiAgICAgICAgICAgIGlmIG5vdCB1Y29uLnVzZXIuaGVyb2RhdGE6CiAgICAgICAgICAgICAgICAj"
    "QSBwbGF5ZXIgaXMgb25seSBhbm5vdW5jZWQgdG8gdGhlIG90aGVycyBieSAkZ2FtZWNoYW5uZWx1"
    "c2VyLAogICAgICAgICAgICAgICAgI2FuZCBnZXRHQ1Vtc2coKSBlbWl0cyBub3RoaW5nIGF0IGFs"
    "bCB1bnRpbCB0aGVpciBoZXJvZGF0YSBoYXMKICAgICAgICAgICAgICAgICNhcnJpdmVkLiBCcm9h"
    "ZGNhc3RpbmcgYSBwb3NpdGlvbiBmb3IgYSBoZXJvIGlkIG5vYm9keSBoYXMKICAgICAgICAgICAg"
    "ICAgICNiZWVuIHRvbGQgYWJvdXQgaGFuZHMgZXZlcnkgY2xpZW50IGFuIHVwZGF0ZSBmb3IgYSBw"
    "bGF5ZXIgaXQKICAgICAgICAgICAgICAgICNkb2VzIG5vdCBrbm93IGV4aXN0cy4gV2FpdCB1bnRp"
    "bCB0aGV5IGFyZSBhIHJlYWwsIGFubm91bmNlZAogICAgICAgICAgICAgICAgI3BsYXllci4KICAg"
    "ICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIG1vdmVycy5hcHBlbmQoKHVjb24sIGYn"
    "e3Vjb24udXNlci53aXJlSWQoKX0je3Vjb24udXNlci5wb3NkYXRhfScpKQogICAgICAgIGlmIG5v"
    "dCBtb3ZlcnM6CiAgICAgICAgICAgICNFdmVyeW9uZSB3aG8gd2FzIGRpcnR5IGhhcyBzaW5jZSBs"
    "ZWZ0IHRoZSB0b3duLiBTZW5kaW5nIHRoZQogICAgICAgICAgICAjYXJndW1lbnQtbGVzcyAnL3Vw"
    "ZGhlcm9wb3MgJyB0aGF0IHRoaXMgdXNlZCB0byBwcm9kdWNlIGp1c3QgaGFuZHMKICAgICAgICAg"
    "ICAgI3RoZSBjbGllbnQgYW4gZW1wdHkgY29tbWFuZCB0byBwYXJzZS4KICAgICAgICAgICAgcmV0"
    "dXJuCiAgICAgICAgI05vYm9keSBpcyB0b2xkIHRoZWlyIG93biBwb3NpdGlvbi4gVGhlIGNsaWVu"
    "dCBpcyB0aGUgYXV0aG9yaXR5IG9uCiAgICAgICAgI3doZXJlIGl0cyBvd24gaGVybyBpcyAtIGl0"
    "IGlzIHdoYXQgc2VudCB0aGUgY29vcmRpbmF0ZXMgaW4gdGhlIGZpcnN0CiAgICAgICAgI3BsYWNl"
    "IC0gc28gZWNob2luZyB0aGVtIGJhY2sgYSBmcmFjdGlvbiBvZiBhIHNlY29uZCBsYXRlciBpcyBh"
    "dCBiZXN0CiAgICAgICAgI3JlZHVuZGFudCBhbmQgYXQgd29yc3QgYSBoaXRjaCwgYXMgdGhlIGhl"
    "cm8gaXMgbnVkZ2VkIGJhY2sgdG8gd2hlcmUKICAgICAgICAjaXQgc3Rvb2Qgd2hlbiB0aGUgcGFj"
    "a2V0IGxlZnQuIEV2ZXJ5IG90aGVyIGJyb2FkY2FzdCBpbiB0aGlzIGZpbGUKICAgICAgICAjYWxy"
    "ZWFkeSBleGNsdWRlcyB0aGUgb3JpZ2luYXRvciAoc2VlIF93b1VzZXIpOyBwb3NpdGlvbnMgd2Vy"
    "ZSB0aGUKICAgICAgICAjZXhjZXB0aW9uLiBDb3N0cyBvbmUgbWVzc2FnZSBidWlsdCBwZXIgbW92"
    "aW5nIHBsYXllciwgYW5kIG5vdCBvbmUKICAgICAgICAjZXh0cmEgYnl0ZSBvbiB0aGUgd2lyZTog"
    "dGhlIGRpc3RyaWJ1dG9yIGFscmVhZHkgd3JpdGVzIHRvIGVhY2gKICAgICAgICAjcmVjaXBpZW50"
    "IHNlcGFyYXRlbHkuCiAgICAgICAgbW92ZWQgPSBzZXQodSBmb3IgKHUsIF8pIGluIG1vdmVycykK"
    "ICAgICAgICB3YXRjaGVycyA9IFtjIGZvciBjIGluIHRnIGlmIGMgbm90IGluIG1vdmVkXQogICAg"
    "ICAgIGlmIHdhdGNoZXJzOgogICAgICAgICAgICBmb3IgbXNnIGluIHNlbGYuX3Bvc01lc3NhZ2Vz"
    "KFtjaCBmb3IgKF8sIGNoKSBpbiBtb3ZlcnNdKToKICAgICAgICAgICAgICAgIG1kLmFkZCh7J3Rh"
    "cmdldCc6d2F0Y2hlcnMsJ21lc3NhZ2UnOm1zZ30pCiAgICAgICAgZm9yICh1Y29uLCBfKSBpbiBt"
    "b3ZlcnM6CiAgICAgICAgICAgIG90aGVycyA9IFtjaCBmb3IgKHUsIGNoKSBpbiBtb3ZlcnMgaWYg"
    "dSBpcyBub3QgdWNvbl0KICAgICAgICAgICAgaWYgbm90IG90aGVyczoKICAgICAgICAgICAgICAg"
    "IGNvbnRpbnVlICNvbmx5IG1vdmVyIGluIHRoZSB0b3duLCBub3RoaW5nIHRvIHRlbGwgdGhlbQog"
    "ICAgICAgICAgICBmb3IgbXNnIGluIHNlbGYuX3Bvc01lc3NhZ2VzKG90aGVycyk6CiAgICAgICAg"
    "ICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOih1Y29uLCApLCdtZXNzYWdlJzptc2d9KQogICAgZGVm"
    "IF9wb3NNZXNzYWdlcyhzZWxmLCBjaHVua3MpOgogICAgICAgICNTcGxpdCBpbnRvIHNldmVyYWwg"
    "Y29tbWFuZHMgcmF0aGVyIHRoYW4gb25lIGFyYml0cmFyaWx5IGxvbmcgbGluZS4KICAgICAgICAj"
    "L3VwZGhlcm9wb3MgaXMgdGhlIG9ubHkgbWVzc2FnZSB3aG9zZSBsZW5ndGggZ3Jvd3Mgd2l0aCB0"
    "aGUgbnVtYmVyIG9mCiAgICAgICAgI3BsYXllcnMgLSBhIGJ1c3kgdG93biB3b3VsZCBwdXQgZmlm"
    "dHkgImlkI3gjeSIgZ3JvdXBzIG9uIGEgc2luZ2xlCiAgICAgICAgI2xpbmUuIFRoZSByZXRhaWwg"
    "Y2xpZW50IGlzIGEgMjAwOCAzMi1iaXQgYmluYXJ5IGFuZCBpdHMgbG9iYnkgcGFyc2VyCiAgICAg"
    "ICAgI2NhbiBiZSBhc3N1bWVkIHRvIHVzZSBmaXhlZC1zaXplIGJ1ZmZlcnM7IGhhbmRpbmcgaXQg"
    "YSBsaW5lIGxvbmdlcgogICAgICAgICN0aGFuIGl0IGV4cGVjdHMgaXMgdGhlIGNsYXNzaWMgd2F5"
    "IHRvIGNvcnJ1cHQgaXRzIGhlYXAgYW5kIHRha2UgaXQKICAgICAgICAjZG93biB3aXRoIGFuIGFj"
    "Y2VzcyB2aW9sYXRpb24gc29tZXdoZXJlIGVsc2UgZW50aXJlbHkuIFNldmVyYWwgc2hvcnQKICAg"
    "ICAgICAjY29tbWFuZHMgYXJlIGVxdWl2YWxlbnQgZm9yIHRoZSBjbGllbnQgYW5kIGNvc3Qgb25l"
    "IGV4dHJhIGhlYWRlcgogICAgICAgICNlYWNoLgogICAgICAgIGJhdGNoZXMgPSBbXQogICAgICAg"
    "IGN1ciA9IFtdCiAgICAgICAgY3VybGVuID0gMAogICAgICAgIGZvciBjaCBpbiBjaHVua3M6CiAg"
    "ICAgICAgICAgIGlmIGN1ciBhbmQgY3VybGVuICsgbGVuKGNoKSArIDEgPiBfTUFYX1dJUkVfTElO"
    "RToKICAgICAgICAgICAgICAgIGJhdGNoZXMuYXBwZW5kKGN1cikKICAgICAgICAgICAgICAgIGN1"
    "ciA9IFtdCiAgICAgICAgICAgICAgICBjdXJsZW4gPSAwCiAgICAgICAgICAgIGN1ci5hcHBlbmQo"
    "Y2gpCiAgICAgICAgICAgIGN1cmxlbiArPSBsZW4oY2gpICsgMQogICAgICAgIGlmIGN1cjoKICAg"
    "ICAgICAgICAgYmF0Y2hlcy5hcHBlbmQoY3VyKQogICAgICAgIHJldHVybiBbX2VtKCcvdXBkaGVy"
    "b3BvcyAnICsgJyAnLmpvaW4oYikpIGZvciBiIGluIGJhdGNoZXNdCiAgICBkZWYgZGVidWdfYXJy"
    "X2dhbWVzKHNlbGYpOgogICAgICAgIGFjdERpY3QgPSBbXQogICAgICAgIGZvciBnbiwgZyBpbiBs"
    "aXN0KHNlbGYuZ2FtZXMuaXRlbXMoKSk6CiAgICAgICAgICAgIGFjdERpY3QuYXBwZW5kKGcuZGVi"
    "dWdfZGljdCgpKQogICAgICAgIHJldHVybiBhY3REaWN0CiAgICBkZWYgZGVidWdfZGljdChzZWxm"
    "KToKICAgICAgICByZXR1cm4gewogICAgICAgICAgICAndXNlcnMnOnR1cGxlKFtjLnVzZXIubmFt"
    "ZSBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0XSksCiAgICAgICAgICAgICdtYXhVc2Vycyc6c2VsZi5t"
    "YXh1c2VyLAogICAgICAgICAgICAnZ2FtZXMnOnR1cGxlKFtnbiBmb3IgZ24gaW4gc2VsZi5nYW1l"
    "c10pCiAgICAgICAgfQoKX01BUE5BTUVTID0gWydOZXRfVF8wMScsJ05ldF9UXzAyJywnTmV0X1Rf"
    "MDMnLCdOZXRfVF8wNCddICNUT0RPIHVzZSBDRkcgb2JqZWN0CmNsYXNzIEdhbWVTdGF0ZSgpOgog"
    "ICAgI1RPRE8gYXV0byBncm93YWJsZSBjaGFubmVscywgW21hcG5hbWVdCiAgICAjVE9ETyBhdmFp"
    "bGFibGUgaW5kZXhlcywgW21hcG5hbWVdCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyKToK"
    "ICAgICAgICAjaW5zdGFuY2UgYXR0cmlidXRlcywgbm90IGNsYXNzIGF0dHJpYnV0ZXM6IHRoZXNl"
    "IG11c3QgTk9UIGJlIHNoYXJlZAogICAgICAgICNiZXR3ZWVuIHNlcGFyYXRlIENvcmVTZXJ2ZXIg"
    "aW5zdGFuY2VzIChlLmcuIHN0b3Avc3RhcnQgZnJvbSBhIEdVSQogICAgICAgICN3aXRoaW4gdGhl"
    "IHNhbWUgcHJvY2Vzcykgb3IgbGVmdG92ZXIgcGxheWVycy9jaGFubmVscyBmcm9tIGEKICAgICAg"
    "ICAjcHJldmlvdXMgcnVuIHdvdWxkIGxlYWsgaW50byB0aGUgbmV3IG9uZS4KICAgICAgICBzZWxm"
    "LmFjdGl2ZVVzZXJzID0ge30gI1RPRE8gdHJhY2sgdXNlciBoaXN0b3J5PyBvcHRpb25hbGx5CiAg"
    "ICAgICAgc2VsZi5nYW1lQ2hhbm5lbHMgPSB7fSAjY2hhbm5lbFtdLCBrZXllZCBieSBtYXBuYW1l"
    "CiAgICAgICAgc2VsZi5zZXJ2ZXI9c2VydmVyCiAgICAgICAgc2VsZi51c2VyTG9jayA9IHRocmVh"
    "ZGluZy5Mb2NrKCkKICAgICAgICBmb3IgbmFtZSBpbiBfTUFQTkFNRVM6CiAgICAgICAgICAgIGZv"
    "ciBpIGluIHJhbmdlKDEpOiAjVE9ETyBjb25maWd1cmVhYmxlIHVwIHRvIDIwPwogICAgICAgICAg"
    "ICAgICAgY2huTmFtZSA9IF9nY2hubChuYW1lLCAxK2kpCiAgICAgICAgICAgICAgICBzZWxmLmdh"
    "bWVDaGFubmVsc1tjaG5OYW1lXSA9IEdhbWVDaGFubmVsKHNlbGYuc2VydmVyLCBjaG5OYW1lKSAj"
    "VE9ETyAxIGFuZCBncm93PwogICAgZGVmIGNsYWltVXNlcihzZWxmLCBuYW1lLCBjb24pOgogICAg"
    "ICAgICNQdWJsaXNoIGNvbiBhcyBUSEUgbGl2ZSBzZXNzaW9uIGZvciBuYW1lLCBhdG9taWNhbGx5"
    "LiBUaGUgb2xkIGNvZGUKICAgICAgICAjY2hlY2tlZCBnZXRQbGF5ZXIoKSBkdXJpbmcgbG9naW4g"
    "YW5kIHRoZW4gaW5zZXJ0ZWQgaW50byBhY3RpdmVVc2VycwogICAgICAgICNtdWNoIGxhdGVyLCBp"
    "biBfbG9iYnlIYW5kbGU7IHR3byBjb25uZWN0aW9ucyBsb2dnaW5nIGluIGFzIHRoZSBzYW1lCiAg"
    "ICAgICAgI2FjY291bnQgYXQgb25jZSBib3RoIHBhc3NlZCB0aGUgY2hlY2ssIGFuZCB0aGUgc2Vj"
    "b25kIG9uZSdzIGluc2VydAogICAgICAgICNvdmVyd3JvdGUgdGhlIGZpcnN0LiBUaGUgbG9zZXIg"
    "dGhlbiBkZWxldGVkIHRoZSB3aW5uZXIncyBlbnRyeSB3aGVuIGl0CiAgICAgICAgI2Rpc2Nvbm5l"
    "Y3RlZCwgbGVhdmluZyBhIGNvbm5lY3RlZCBwbGF5ZXIgaW52aXNpYmxlIHRvIHRoZSBzZXJ2ZXIg"
    "KG5vCiAgICAgICAgI2tpY2ssIG5vIHdob2lzLCBubyBtZXNzYWdlcykuCiAgICAgICAgd2l0aCBz"
    "ZWxmLnVzZXJMb2NrOgogICAgICAgICAgICBpZiBuYW1lIGluIHNlbGYuYWN0aXZlVXNlcnM6CiAg"
    "ICAgICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICAgICAgc2VsZi5hY3RpdmVVc2Vyc1tu"
    "YW1lXSA9IGNvbgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgZGVmIHJlbGVhc2VVc2VyKHNl"
    "bGYsIG5hbWUsIGNvbik6CiAgICAgICAgI29ubHkgY2xlYXIgdGhlIHNsb3QgaWYgd2Ugc3RpbGwg"
    "b3duIGl0LCBuZXZlciBzb21lb25lIGVsc2UncyBzZXNzaW9uCiAgICAgICAgd2l0aCBzZWxmLnVz"
    "ZXJMb2NrOgogICAgICAgICAgICBpZiBzZWxmLmFjdGl2ZVVzZXJzLmdldChuYW1lKSBpcyBjb246"
    "CiAgICAgICAgICAgICAgICBkZWwgc2VsZi5hY3RpdmVVc2Vyc1tuYW1lXQogICAgZGVmIGVudW1l"
    "cmF0ZUdDKHNlbGYpOgogICAgICAgIGNobnMgPSBbXQogICAgICAgIGZvciBjaG5OYW1lIGluIHNl"
    "bGYuZ2FtZUNoYW5uZWxzOgogICAgICAgICAgICBjaG4gPSBzZWxmLmdhbWVDaGFubmVsc1tjaG5O"
    "YW1lXQogICAgICAgICAgICBjaG5zLmFwcGVuZCh3aXJlX2VuY29kZShmJyRnYW1lY2hhbm5lbCAi"
    "e2Nobk5hbWV9IiAie2xlbihjaG4udXNlcmxpc3QpfSIgIntjaG4ubWF4dXNlcn0iICIwIiAiMCIn"
    "KSkgI1RPRE8gQXZhaWxhYmxlIC0gQWxsCiAgICAgICAgcmV0dXJuIF9OLmpvaW4oY2hucykrX04K"
    "ICAgIGRlZiB1cGRhdGVQb3Moc2VsZik6CiAgICAgICAgbWQgPSBzZWxmLnNlcnZlci5kaXN0CiAg"
    "ICAgICAgZm9yIGNobiBpbiBsaXN0KHNlbGYuZ2FtZUNoYW5uZWxzLnZhbHVlcygpKToKICAgICAg"
    "ICAgICAgY2huLnVwZGF0ZVBvcyhtZCkKI2hhbmRsZXMgaW50ZXJhY3Rpb25zIGJldHdlZW4gYWxs"
    "IGVsZW1lbnRzCmNsYXNzIENvcmVTZXJ2ZXIoc29ja2V0c2VydmVyLlRocmVhZGluZ1RDUFNlcnZl"
    "cik6CiAgICBhbGxvd19yZXVzZV9hZGRyZXNzID0gVHJ1ZSAjIFRPRE8gY2hlY2sgaWYgaW1wcm92"
    "ZXMgcmVzdGFydCB0aW1lcyB3aXRob3V0IG90aGVyIGlzc3VlcwogICAgZGFlbW9uX3RocmVhZHMg"
    "PSBUcnVlCiAgICBibG9ja19vbl9jbG9zZSA9IEZhbHNlCiAgICBfaXNfY2xvc2luZyA9IEZhbHNl"
    "CiAgICBkZWYgX19pbml0X18oc2VsZik6CiAgICAgICAgI1RPRE8gZ2V0IHZhbHVlcyBmcm9tIGNm"
    "ZwogICAgICAgICNhZGRyZXNzID0gJ2xvY2FsaG9zdCcKICAgICAgICBhZGRyZXNzID0gJycKICAg"
    "ICAgICBwb3J0ID0gX1RXX0xPQkJZX1BPUlQKICAgICAgICBwcmludChmJ0luaXRpYWxpemluZyBz"
    "ZXJ2ZXIgZm9yIHBvcnQge3BvcnR9JykKICAgICAgICBzdXBlcigpLl9faW5pdF9fKChhZGRyZXNz"
    "LCBwb3J0KSwgQ29ubmVjdGlvbkhhbmRsZXIpCiAgICAgICAgc2VsZi5kaXN0ID0gTWVzc2FnZURp"
    "c3RyaWJ1dG9yKHNlbGYpCiAgICAgICAgc2VsZi5jb21wYXJzID0gQ29tbWFuZFBhcnNlcihzZWxm"
    "LmRpc3QpCiAgICAgICAgc2VsZi5zdGF0ZSA9IEdhbWVTdGF0ZShzZWxmKQogICAgICAgIHNlbGYu"
    "c3RhcnRUaW1lID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCkKICAgICAgICBzZWxmLnNlcnZpY2Vf"
    "dGljayA9IDAKICAgICAgICBzZWxmLnNlbmRfbm9wcyA9IF9TRU5EX05PUFMKICAgICAgICBzZWxm"
    "Ll9wb3NTdG9wID0gdGhyZWFkaW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSBO"
    "b25lCiAgICAgICAgI0V2ZXJ5IGxpdmUgY29ubmVjdGlvbiBoYW5kbGVyLiBzb2NrZXRzZXJ2ZXIn"
    "cyBzaHV0ZG93bigpIG9ubHkgc3RvcHMKICAgICAgICAjdGhlIGFjY2VwdCBsb29wIGFuZCBjbG9z"
    "ZXMgdGhlIGxpc3RlbmluZyBzb2NrZXQgLSBhbHJlYWR5LWVzdGFibGlzaGVkCiAgICAgICAgI2Nv"
    "bm5lY3Rpb25zIGtlZXAgdGhlaXIgKGRhZW1vbikgdGhyZWFkcyBydW5uaW5nLCBzdGlsbCByZWFk"
    "aW5nLCBzdGlsbAogICAgICAgICNsb2dnaW5nLCBmb3IgYXMgbG9uZyBhcyB0aGUgY2xpZW50IHN0"
    "YXlzIGNvbm5lY3RlZC4gRnJvbSB0aGUgY29udHJvbAogICAgICAgICNwYW5lbCB0aGF0IGxvb2tz"
    "IGxpa2UgYSBzZXJ2ZXIgdGhhdCB3YXMgbmV2ZXIgc3RvcHBlZCBhdCBhbGwuCiAgICAgICAgc2Vs"
    "Zi5fY29ubnMgPSBzZXQoKQogICAgICAgIHNlbGYuX2Nvbm5Mb2NrID0gdGhyZWFkaW5nLkxvY2so"
    "KQogICAgZGVmIHNlcnZlcl9hY3RpdmF0ZShzZWxmKToKICAgICAgICBwcmludChmJ1NlcnZlciBT"
    "dGFydGluZyBhdCBQSUQ6IHtvcy5nZXRwaWQoKX0nKSNMT0cKICAgICAgICBzdXBlcigpLnNlcnZl"
    "cl9hY3RpdmF0ZSgpCiAgICBkZWYgZGVidWdfZGljdF9wbGF5ZXJzKHNlbGYpOgogICAgICAgICNz"
    "bmFwc2hvdCB2aWEgbGlzdCgpIGZpcnN0OiBpdGVyYXRpbmcgdGhlIGxpdmUgZGljdCBkaXJlY3Rs"
    "eSByaXNrcwogICAgICAgICMnZGljdGlvbmFyeSBjaGFuZ2VkIHNpemUgZHVyaW5nIGl0ZXJhdGlv"
    "bicgd2hlbiBhIHBsYXllciBjb25uZWN0cwogICAgICAgICNvciBkaXNjb25uZWN0cyB3aGlsZSBh"
    "IG1vbml0b3JpbmcgVUkgaXMgcG9sbGluZyB0aGlzCiAgICAgICAgcmV0ID0ge30KICAgICAgICBm"
    "b3IgbmFtZSwgY29uIGluIGxpc3Qoc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5pdGVtcygpKToKICAg"
    "ICAgICAgICAgcmV0W25hbWVdID0gY29uLmRlYnVnX2RpY3QoKQogICAgICAgIHJldHVybiByZXQK"
    "ICAgIGRlZiBkZWJ1Z19kaWN0X3Rvd25zKHNlbGYpOgogICAgICAgIHJldCA9IHt9CiAgICAgICAg"
    "Zm9yIG5hbWUsIGNobiBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1zKCkpOgog"
    "ICAgICAgICAgICByZXRbbmFtZV0gPSBjaG4uZGVidWdfZGljdCgpCiAgICAgICAgcmV0dXJuIHJl"
    "dAogICAgZGVmIGRlYnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAgICByZXQgPSBbXQogICAgICAg"
    "IGZvciBuYW1lLCBjaG4gaW4gbGlzdChzZWxmLnN0YXRlLmdhbWVDaGFubmVscy5pdGVtcygpKToK"
    "ICAgICAgICAgICAgIHJldC5leHRlbmQoY2huLmRlYnVnX2Fycl9nYW1lcygpKQogICAgICAgIHJl"
    "dHVybiByZXQKICAgIGRlZiBfcG9zTG9vcChzZWxmKToKICAgICAgICAjUG9zaXRpb24gZmFuLW91"
    "dCB1c2VkIHRvIHJpZGUgb24gc2VydmljZV9hY3Rpb25zKCksIHdoaWNoIHNvY2tldHNlcnZlcgog"
    "ICAgICAgICNjYWxscyBvbmNlIHBlciBwb2xsX2ludGVydmFsIC0gb25lIHNlY29uZC4gVGhhdCB3"
    "YXMgdGhlIGNhZGVuY2UgYXQKICAgICAgICAjd2hpY2ggb3RoZXIgcGxheWVycycgbWFya2VycyBt"
    "b3ZlZCBvbiB0aGUgbWFwOiBhIGZ1bGwgc2Vjb25kIG9mIGRlYWQKICAgICAgICAjcmVja29uaW5n"
    "IGJldHdlZW4gdXBkYXRlcywgd2hpY2ggcmVhZHMgYXMgdGVsZXBvcnRpbmcgcmF0aGVyIHRoYW4K"
    "ICAgICAgICAjd2Fsa2luZy4gSXRzIG93biB0aHJlYWQgZGVjb3VwbGVzIHRoZSBicm9hZGNhc3Qg"
    "cmF0ZSBmcm9tIHRoZSBhY2NlcHQKICAgICAgICAjbG9vcCdzIHBvbGwgcmF0ZSBzbyBpdCBjYW4g"
    "cnVuIHNldmVyYWwgdGltZXMgYSBzZWNvbmQuCiAgICAgICAgd2hpbGUgbm90IHNlbGYuX3Bvc1N0"
    "b3AuaXNfc2V0KCk6CiAgICAgICAgICAgIHBlcmlvZCA9IDEuMCAvIF9QT1NfVVBEQVRFX0haIGlm"
    "IF9QT1NfVVBEQVRFX0haID4gMCBlbHNlIDEuMAogICAgICAgICAgICAjd2FpdCgpIHJhdGhlciB0"
    "aGFuIHNsZWVwKCk6IHNodXRkb3duIGlzIGltbWVkaWF0ZSwgYW5kIHJlLXJlYWRpbmcKICAgICAg"
    "ICAgICAgI3RoZSBwZXJpb2QgZWFjaCBwYXNzIG1lYW5zIGEgY29uZmlnIGNoYW5nZSB0YWtlcyBl"
    "ZmZlY3QgbGl2ZS4KICAgICAgICAgICAgaWYgc2VsZi5fcG9zU3RvcC53YWl0KHBlcmlvZCk6CiAg"
    "ICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBzZWxm"
    "LnN0YXRlLnVwZGF0ZVBvcygpCiAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAg"
    "ICAgICAgICAjbmV2ZXIgbGV0IG9uZSBiYWQgY2hhbm5lbCBraWxsIHBvc2l0aW9uIHN5bmMgZm9y"
    "IGV2ZXJ5b25lCiAgICAgICAgICAgICAgICBwcmludCgnW0xvYmJ5XSBQb3NpdGlvbiB1cGRhdGUg"
    "ZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgIGRlZiBzZXJ2aWNlX2FjdGlv"
    "bnMoc2VsZik6ICNjYWxsZWQgZXZlcnkgcG9sbF9pbnRlcnZhbAogICAgICAgICMgdGltZSBpbnRl"
    "cnZhbHMKICAgICAgICBpZiBzZWxmLnNlbmRfbm9wcyBhbmQgKHNlbGYuc2VydmljZV90aWNrJTMp"
    "PT0wOgogICAgICAgICAgICBzZWxmLmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLnN0YXRlLmFjdGl2"
    "ZVVzZXJzLnZhbHVlcygpLCdtZXNzYWdlJzpfZW0oJy9ub3AnKX0pCiAgICAgICAgICAgICNzZW5k"
    "ICcvbm9wJyB0byBhbGwgZXZlcnkgMyBzZWMgb3B0aW9uYWxseQogICAgICAgICNzZXJ2aWNlIHRp"
    "Y2sgMyBkYXkgcmVzZXQgaW50ZXJ2YWwgVE9ETyB0ZXN0IGFsaWdubWVudCB3aXRoIG90aGVyIGZh"
    "Y3RvcnMKICAgICAgICBzZWxmLnNlcnZpY2VfdGljayA9IChzZWxmLnNlcnZpY2VfdGljaysxKSUo"
    "NjAqNjAqMjQqMykKICAgICAgICBzdXBlcigpLnNlcnZpY2VfYWN0aW9ucygpCiAgICBkZWYgc2Vy"
    "dmVfZm9yZXZlcihzZWxmKToKICAgICAgICBkaXN0VGhyZWFkID0gdGhyZWFkaW5nLlRocmVhZCh0"
    "YXJnZXQ9c2VsZi5kaXN0LnNlcnZlX2ZvcmV2ZXIpCiAgICAgICAgZGlzdFRocmVhZC5zdGFydCgp"
    "CiAgICAgICAgc2VsZi5fcG9zU3RvcC5jbGVhcigpCiAgICAgICAgc2VsZi5fcG9zVGhyZWFkID0g"
    "dGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9c2VsZi5fcG9zTG9vcCwgZGFlbW9uPVRydWUpCiAgICAg"
    "ICAgc2VsZi5fcG9zVGhyZWFkLnN0YXJ0KCkKICAgICAgICAjcG9sbF9pbnRlcnZhbCBpcyBub3cg"
    "b25seSB0aGUgYWNjZXB0IGxvb3AncyBzaHV0ZG93biByZXNwb25zaXZlbmVzcyAtCiAgICAgICAg"
    "I3Bvc2l0aW9uIGJyb2FkY2FzdHMgbm8gbG9uZ2VyIHJpZGUgb24gaXQKICAgICAgICBzdXBlcigp"
    "LnNlcnZlX2ZvcmV2ZXIoMSkKICAgICAgICBzZWxmLl9wb3NTdG9wLnNldCgpCiAgICAgICAgaWYg"
    "c2VsZi5fcG9zVGhyZWFkOgogICAgICAgICAgICBzZWxmLl9wb3NUaHJlYWQuam9pbih0aW1lb3V0"
    "PTIuMCkKICAgICAgICAgICAgc2VsZi5fcG9zVGhyZWFkID0gTm9uZQogICAgICAgIHNlbGYuZGlz"
    "dC5lbmQoKSNpbiBjYXNlIGl0IGhhc24ndCBhbHJlYWR5CiAgICAgICAgZGlzdFRocmVhZC5qb2lu"
    "KCkKICAgIGRlZiBoYW5kbGVfc2lnbmFsKHNlbGYsIHRpbWVvdXQpOgogICAgICAgIGRlZiBoYW5k"
    "bGVyKHNpZ251bSwgXyk6CiAgICAgICAgICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSAr"
    "IHRpbWVvdXQKICAgICAgICAgICAgc2lnbmFtZSA9IHNpZ25hbC5TaWduYWxzKHNpZ251bSkubmFt"
    "ZQogICAgICAgICAgICBzZWxmLl9pc19jbG9zaW5nID0gVHJ1ZSAjVE9ETyBwcm9wZXJseSBlbmQg"
    "Y29ubmVjdGlvbnMgYWZ0ZXIgYSBkZWxheQogICAgICAgICAgICBwcmludChmJ0Nsb3NpbmcgaW4g"
    "e3RpbWVvdXR9JykKICAgICAgICAgICAgI3doaWxlIChjdXJyZW50X3RpbWUgOj0gdGltZS5tb25v"
    "dG9uaWMoKSkgPCBkZWFkbGluZToKICAgICAgICAgICAgIyAgICBkZWx0YSA9IGludChkZWFkbGlu"
    "ZSAtIGN1cnJlbnRfdGltZSkKICAgICAgICAgICAgICAgICNUT0RPIHNpZ25hbCB0byBwbGF5ZXJz"
    "IHRoYXQgY29ubmVjdGlvbiBpcyBzaHV0dGluZyBkb3duCiAgICAgICAgICAgICAgICAjLSBzZWxm"
    "LnN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpCiAgICAgICAgICAgICAgICAjLSBmJy9hZG1pbiBT"
    "ZXJ2ZXIgY2xvc2luZyBpbiB7ZGVsdGF9Jy5lbmNvZGUoJ2FzY2lpJykrX04KICAgICAgICAgICAg"
    "ICAgICNMT0cgQ0xPU0UKICAgICAgICAgICAgICAgICNUT0RPIGJldHRlciBzaHV0ZG93biBoYW5k"
    "bGluZwogICAgICAgICAgICAjICAgIHRpbWUuc2xlZXAoMSkKICAgICAgICAgICAgdGltZS5zbGVl"
    "cCh0aW1lb3V0KSNhbHQgd2hpbGUgb3RoZXIgc3R1ZmYgaXMgb25nb2luZwogICAgICAgICAgICBz"
    "ZWxmLl9CYXNlU2VydmVyX19zaHV0ZG93bl9yZXF1ZXN0ID0gVHJ1ZQogICAgICAgICAgICAjc2Vs"
    "Zi5zaHV0ZG93bigpICNvbmx5IGlmIHNlcnZlX2ZvcmV2ZXIgaXMgaW4gYSBkaWZmZXJlbnQgdGhy"
    "ZWFkCiAgICAgICAgICAgICNzZWxmLnNlcnZlcl9jbG9zZSgpICNvbmx5IG5lZWRlZCBpZiBub3Qg"
    "dXNpbmcgYSB3aXRoIHN0YXRlbWVudAogICAgICAgIHJldHVybiBoYW5kbGVyCiAgICBkZWYgcmVn"
    "aXN0ZXJDb25uZWN0aW9uKHNlbGYsIGNvbik6CiAgICAgICAgd2l0aCBzZWxmLl9jb25uTG9jazoK"
    "ICAgICAgICAgICAgc2VsZi5fY29ubnMuYWRkKGNvbikKICAgIGRlZiB1bnJlZ2lzdGVyQ29ubmVj"
    "dGlvbihzZWxmLCBjb24pOgogICAgICAgIHdpdGggc2VsZi5fY29ubkxvY2s6CiAgICAgICAgICAg"
    "IHNlbGYuX2Nvbm5zLmRpc2NhcmQoY29uKQogICAgZGVmIGNsb3NlQ29ubmVjdGlvbnMoc2VsZik6"
    "CiAgICAgICAgI0Ryb3AgZXZlcnkgY2xpZW50LiBTaHV0dGluZyB0aGUgc29ja2V0IGRvd24gdW5i"
    "bG9ja3Mgd2hpY2hldmVyCiAgICAgICAgI3NlbGVjdCgpL3JlY3YoKSB0aGF0IGNvbm5lY3Rpb24n"
    "cyB0aHJlYWQgaXMgc2l0dGluZyBpbiwgc28gaXQgcnVucwogICAgICAgICNpdHMgbm9ybWFsIGNs"
    "ZWFudXAgcGF0aCBhbmQgZXhpdHMgaW5zdGVhZCBvZiBsaW5nZXJpbmcuCiAgICAgICAgd2l0aCBz"
    "ZWxmLl9jb25uTG9jazoKICAgICAgICAgICAgY29ubnMgPSBsaXN0KHNlbGYuX2Nvbm5zKQogICAg"
    "ICAgIGZvciBjb24gaW4gY29ubnM6CiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIGNv"
    "bi5yZXF1ZXN0LnNodXRkb3duKHNvY2tldC5TSFVUX1JEV1IpCiAgICAgICAgICAgIGV4Y2VwdCBF"
    "eGNlcHRpb246CiAgICAgICAgICAgICAgICBwYXNzICNhbHJlYWR5IGRlYWQsIG9yIG5ldmVyIGZ1"
    "bGx5IGNvbm5lY3RlZAogICAgICAgICAgICAjRGVsaWJlcmF0ZWx5IG5vdCBjbG9zZSgpZCBoZXJl"
    "OiB0aGUgaGFuZGxlciB0aHJlYWQgc3RpbGwgb3ducyB0aGlzCiAgICAgICAgICAgICNzb2NrZXQg"
    "YW5kIGNsb3NpbmcgaXQgdW5kZXJuZWF0aCBjYXVzZXMgaXRzIG5leHQgY2FsbCB0byBmYWlsIHdp"
    "dGgKICAgICAgICAgICAgI1dpbkVycm9yIDEwMDM4ICgibm90IGEgc29ja2V0IiksIHdoaWNoIHRo"
    "ZW4gZ2V0cyBsb2dnZWQgYXMgYQogICAgICAgICAgICAjY29ubmVjdGlvbiBlcnJvciBvbiBhIHBl"
    "cmZlY3RseSBub3JtYWwgc2h1dGRvd24uIHNodXRkb3duKCkgYWxvbmUKICAgICAgICAgICAgI3dh"
    "a2VzIHRoZSB0aHJlYWQsIGFuZCBzb2NrZXRzZXJ2ZXIgY2xvc2VzIHRoZSBzb2NrZXQgaXRzZWxm"
    "IG9uY2UKICAgICAgICAgICAgI3RoZSBoYW5kbGVyIHJldHVybnMuCiAgICAgICAgcmV0dXJuIGxl"
    "bihjb25ucykKICAgIGRlZiBzaHV0ZG93bihzZWxmKToKICAgICAgICAjU3RvcHBpbmcgdGhlIHNl"
    "cnZlciBtZWFucyBzdG9wcGluZyBpdDogZmxhZyBpdCBmaXJzdCBzbyB0aGUgcmVhZAogICAgICAg"
    "ICNsb29wcyBiYWlsIG91dCByYXRoZXIgdGhhbiBzZXJ2aW5nIGFub3RoZXIgY29tbWFuZCwgdGhl"
    "biBzdG9wIHRoZQogICAgICAgICNhY2NlcHQgbG9vcCwgdGhlbiBldmljdCBldmVyeW9uZSBzdGls"
    "bCBjb25uZWN0ZWQuCiAgICAgICAgc2VsZi5faXNfY2xvc2luZyA9IFRydWUKICAgICAgICBzdXBl"
    "cigpLnNodXRkb3duKCkKICAgICAgICBuID0gc2VsZi5jbG9zZUNvbm5lY3Rpb25zKCkKICAgICAg"
    "ICBpZiBuOgogICAgICAgICAgICBwcmludChmJ1tMb2JieV0gQ2xvc2VkIHtufSBjbGllbnQgY29u"
    "bmVjdGlvbihzKSBvbiBzaHV0ZG93bicpCiAgICBkZWYgZ2V0UGxheWVyKHNlbGYsIHVzZXJuYW1l"
    "KToKICAgICAgICByZXR1cm4gc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5nZXQodXNlcm5hbWUpCiAg"
    "ICBkZWYga2lja1BsYXllcihzZWxmLCB1c2VybmFtZSwgcmVhc29uPSdLaWNrZWQgYnkgYWRtaW4n"
    "KToKICAgICAgICAjQWRtaW4tcGFuZWwgYWN0aW9uOiBmb3JjaWJseSBkaXNjb25uZWN0IGEgY29u"
    "bmVjdGVkIHBsYXllci4gU2VuZHMgYQogICAgICAgICNiZXN0LWVmZm9ydCAvYWRtaW4gbm90aWNl"
    "IGZpcnN0IChjbGllbnQgc2hvd3MgaXQgbGlrZSBhbnkgb3RoZXIKICAgICAgICAjc2VydmVyIGFk"
    "bWluIG1lc3NhZ2UpLCB0aGVuIHNodXRzIGRvd24gdGhlIHNvY2tldCBzbyB0aGUgcGxheWVyJ3MK"
    "ICAgICAgICAjaGFuZGxlciB0aHJlYWQgdW5ibG9ja3MgZnJvbSBpdHMgcmVjdigpIGFuZCBydW5z"
    "IGl0cyBub3JtYWwKICAgICAgICAjZGlzY29ubmVjdC9jbGVhbnVwIHBhdGguCiAgICAgICAgY29u"
    "ID0gc2VsZi5nZXRQbGF5ZXIodXNlcm5hbWUpCiAgICAgICAgaWYgY29uIGlzIE5vbmU6CiAgICAg"
    "ICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIHRyeToKICAgICAgICAgICAgY29uLnNlbmRSYXco"
    "X2VtKGYnL2FkbWluIHtyZWFzb259JykpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAg"
    "ICAgICAgcGFzcyAjYmVzdCBlZmZvcnQsIGNvbm5lY3Rpb24gbWF5IGFscmVhZHkgYmUgb24gaXRz"
    "IHdheSBvdXQKICAgICAgICB0cnk6CiAgICAgICAgICAgIGNvbi5yZXF1ZXN0LnNodXRkb3duKHNv"
    "Y2tldC5TSFVUX1JEV1IpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFz"
    "cwogICAgICAgIHRyeToKICAgICAgICAgICAgY29uLnJlcXVlc3QuY2xvc2UoKQogICAgICAgIGV4"
    "Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHBhc3MKICAgICAgICByZXR1cm4gVHJ1ZQogICAg"
    "ZGVmIGRlbGV0ZUFjY291bnQoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgICNBZG1pbi1wYW5lbCBh"
    "Y3Rpb246IHBlcm1hbmVudGx5IGRlbGV0ZXMgYSBjaGFyYWN0ZXIvYWNjb3VudC4KICAgICAgICAj"
    "S2lja3MgZmlyc3QgKG5vLW9wIGlmIGFscmVhZHkgb2ZmbGluZSkgc28gYSBjb25uZWN0ZWQgY2xp"
    "ZW50IG5ldmVyCiAgICAgICAgI2tlZXBzIHBsYXlpbmcgb24gYW4gYWNjb3VudCB0aGF0IGhhcyBq"
    "dXN0IHZhbmlzaGVkIGZyb20gdGhlIERCLgogICAgICAgIHNlbGYua2lja1BsYXllcih1c2VybmFt"
    "ZSwgcmVhc29uPSdBY2NvdW50IGRlbGV0ZWQgYnkgYWRtaW4nKQogICAgICAgIHJldHVybiBHREgu"
    "ZGVsZXRlQWNjb3VudCh1c2VybmFtZSkKI0ZhaWxlZC1sb2dpbiB0aHJvdHRsZSwgcGVyIHNvdXJj"
    "ZSBJUC4KI1R3byByZWFzb25zIHRoaXMgaXMgbm90IG9wdGlvbmFsIG9uIGEgc2VydmVyIHJlYWNo"
    "YWJsZSBmcm9tIHRoZSBpbnRlcm5ldDoKI2EgcGFzc3dvcmQgZ3Vlc3MgaXMgY2hlYXAgZm9yIHRo"
    "ZSBhdHRhY2tlciBidXQgY29zdHMgKnVzKiBhIDEwMGstaXRlcmF0aW9uCiNQQktERjIgKHRlbnMg"
    "b2YgbXMgb2YgQ1BVIGVhY2gpLCBzbyBhbiB1bnRocm90dGxlZCBsb2dpbiBlbmRwb2ludCBpcyBi"
    "b3RoIGEKI2JydXRlLWZvcmNlIG9yYWNsZSBhbmQgYSBDUFUgYW1wbGlmaWVyIC0gYSBoYW5kZnVs"
    "IG9mIGNvbm5lY3Rpb25zIGNhbiBwaW4KI2V2ZXJ5IGNvcmUuIFN1Y2Nlc3NmdWwgbG9naW5zIGNs"
    "ZWFyIHRoZSBjb3VudGVyLCBzbyBhIHBsYXllciBmdW1ibGluZyB0aGVpcgojcGFzc3dvcmQgYSBm"
    "ZXcgdGltZXMgaXMgbmV2ZXIgbG9ja2VkIG91dCBmb3IgbG9uZy4KX0xPR0lOX0ZBSUxfTElNSVQg"
    "PSA2ICAgICAgI2ZhaWx1cmVzIGFsbG93ZWQgaW5zaWRlIHRoZSB3aW5kb3cgYmVmb3JlIGRlbGF5"
    "aW5nCl9MT0dJTl9GQUlMX1dJTkRPVyA9IDMwMCAgICNzZWNvbmRzIGEgZmFpbHVyZSBpcyByZW1l"
    "bWJlcmVkCl9MT0dJTl9GQUlMX0RFTEFZID0gMi4wICAgICNzZWNvbmRzIHRvIHN0YWxsIGVhY2gg"
    "YXR0ZW1wdCBvbmNlIG92ZXIgdGhlIGxpbWl0CmNsYXNzIExvZ2luVGhyb3R0bGUoKToKICAgIGRl"
    "ZiBfX2luaXRfXyhzZWxmKToKICAgICAgICBzZWxmLmxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAg"
    "ICAgICAgc2VsZi5mYWlscyA9IHt9ICNpcCAtPiBbdGltZXN0YW1wc10KICAgIGRlZiBfcHJ1bmUo"
    "c2VsZiwgaXAsIG5vdyk6CiAgICAgICAgcmVjZW50ID0gW3QgZm9yIHQgaW4gc2VsZi5mYWlscy5n"
    "ZXQoaXAsICgpKSBpZiBub3cgLSB0IDwgX0xPR0lOX0ZBSUxfV0lORE9XXQogICAgICAgIGlmIHJl"
    "Y2VudDoKICAgICAgICAgICAgc2VsZi5mYWlsc1tpcF0gPSByZWNlbnQKICAgICAgICBlbHNlOgog"
    "ICAgICAgICAgICBzZWxmLmZhaWxzLnBvcChpcCwgTm9uZSkKICAgICAgICByZXR1cm4gcmVjZW50"
    "CiAgICBkZWYgZGVsYXlGb3Ioc2VsZiwgaXApOgogICAgICAgIG5vdyA9IHRpbWUubW9ub3Rvbmlj"
    "KCkKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgcmVjZW50ID0gc2VsZi5fcHJ1"
    "bmUoaXAsIG5vdykKICAgICAgICByZXR1cm4gX0xPR0lOX0ZBSUxfREVMQVkgaWYgbGVuKHJlY2Vu"
    "dCkgPj0gX0xPR0lOX0ZBSUxfTElNSVQgZWxzZSAwLjAKICAgIGRlZiByZWNvcmRGYWlsdXJlKHNl"
    "bGYsIGlwKToKICAgICAgICBub3cgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgd2l0aCBzZWxm"
    "LmxvY2s6CiAgICAgICAgICAgIHJlY2VudCA9IHNlbGYuX3BydW5lKGlwLCBub3cpCiAgICAgICAg"
    "ICAgIHJlY2VudC5hcHBlbmQobm93KQogICAgICAgICAgICBzZWxmLmZhaWxzW2lwXSA9IHJlY2Vu"
    "dAogICAgICAgICAgICByZXR1cm4gbGVuKHJlY2VudCkKICAgIGRlZiByZWNvcmRTdWNjZXNzKHNl"
    "bGYsIGlwKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgc2VsZi5mYWlscy5w"
    "b3AoaXAsIE5vbmUpCkxPR0lOX1RIUk9UVExFID0gTG9naW5UaHJvdHRsZSgpCgpfTE9HSU5fRVJS"
    "T1JTID0gewogICAgMTogJ0ludmFsaWQgdXNlcm5hbWUgb3IgcGFzc3dvcmQnLAogICAgMjogJ0Fj"
    "Y291bnQgYWxyZWFkeSBsb2dnZWQgaW4nLAogICAgMzogJ1Bhc3N3b3JkIHJlcXVpcmVkJywKICAg"
    "IDQ6ICdVc2VybmFtZSByZXF1aXJlZCcsCn0KX1JFR0lTVEVSX0VSUk9SUyA9IHsKICAgIDE6ICdB"
    "Y2NvdW50IGFscmVhZHkgbG9nZ2VkIGluJywKICAgIDI6ICdVc2VybmFtZSB1bmF2YWlsYWJsZSBv"
    "ciBpbnZhbGlkJywKfQojaGFuZGxlcyBpbmRpdmlkdWFsIGNvbm5lY3Rpb25zCmNsYXNzIENvbm5l"
    "Y3Rpb25IYW5kbGVyKHNvY2tldHNlcnZlci5CYXNlUmVxdWVzdEhhbmRsZXIpOgogICAgI2RlZmF1"
    "bHQgcHJvcGVydGllczoKICAgICMgLSByZXF1ZXN0OiBzb2NrZXQgdG8gZGVzdGluYXRpb24KICAg"
    "ICMgLSBjbGllbnRfYWRkcmVzcwogICAgIyAtIHNlcnZlcjogQ29yZVNlcnZlcgogICAgX1NUT1BX"
    "UklURVIgPSBvYmplY3QoKQogICAgZGVmIHNldHVwKHNlbGYpOgogICAgICAgIHNlbGYuX3NRdWV1"
    "ZSA9IFNpbXBsZVF1ZXVlKCkKICAgICAgICBzZWxmLnVzZXIgPSBOb25lCiAgICAgICAgc2VsZi5n"
    "dWlkID0gTm9uZQogICAgICAgIHNlbGYuZGF0YSA9IGInJwogICAgICAgIHNlbGYuU0sgPSBieXRl"
    "YXJyYXkoc3RydWN0LnBhY2soJzxJSScsIDB4QTZBRTFGOUIsIDB4NDM4REZGNDApKQogICAgICAg"
    "ICNTZXJpYWxpc2VzIHRoZSByYXcgc29ja2V0IHdyaXRlcy4gVGhyZWUgdGhyZWFkcyBjYW4gd2Fu"
    "dCB0byB3cml0ZSB0bwogICAgICAgICNvbmUgY2xpZW50OiB0aGlzIGNvbm5lY3Rpb24ncyBvd24g"
    "cmVhZCBsb29wIChkdXJpbmcgdGhlIGhhbmRzaGFrZSksCiAgICAgICAgI2l0cyB3cml0ZXIgdGhy"
    "ZWFkLCBhbmQgdGhlIEdVSSB0aHJlYWQgdmlhIGtpY2tQbGF5ZXIoKS4gV2l0aG91dCB0aGUKICAg"
    "ICAgICAjbG9jayB0d28gc2VuZGFsbCgpIGNhbGxzIGNhbiBpbnRlcmxlYXZlIGFuZCBzcGxpdCBh"
    "IHBhY2tldCBkb3duIHRoZQogICAgICAgICNtaWRkbGUsIHdoaWNoIHRoZSBjbGllbnQgc2VlcyBh"
    "cyBwcm90b2NvbCBnYXJiYWdlLgogICAgICAgIHNlbGYuX3NlbmRMb2NrID0gdGhyZWFkaW5nLkxv"
    "Y2soKQogICAgICAgIHNlbGYuX3dyaXRlciA9IE5vbmUKICAgICAgICBzZWxmLl93cml0ZXJEZWFk"
    "ID0gdGhyZWFkaW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3Rv"
    "bmljKCkKICAgICAgICBzZWxmLnNlcnZlci5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZikKICAgICAg"
    "ICB0cnk6CiAgICAgICAgICAgICNOYWdsZSBiYXRjaGVzIHNtYWxsIHdyaXRlcyBieSBob2xkaW5n"
    "IHRoZW0gZm9yIHVwIHRvIH40MG1zIHdhaXRpbmcKICAgICAgICAgICAgI2ZvciBtb3JlIGRhdGEu"
    "IEV2ZXJ5IG1lc3NhZ2UgdGhpcyBzZXJ2ZXIgc2VuZHMgaXMgc21hbGwgYW5kCiAgICAgICAgICAg"
    "ICNsYXRlbmN5LXNlbnNpdGl2ZSAtIGNoYXQsIHBvc2l0aW9uIHVwZGF0ZXMgYW5kIGFib3ZlIGFs"
    "bCB0aGUKICAgICAgICAgICAgIy9nYW1lY29tbWFuZHRvdXNlciByZWxheSB0aGF0IGNhcnJpZXMg"
    "dGhlIGFjdHVhbCBpbi1nYW1lIGNvLW9wCiAgICAgICAgICAgICN0cmFmZmljIGJldHdlZW4gdHdv"
    "IHBsYXllcnMgLSBzbyB0aGUgZGVsYXkgaXMgcHVyZSBhZGRlZCBsYWcuCiAgICAgICAgICAgIHNl"
    "bGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5JUFBST1RPX1RDUCwgc29ja2V0LlRDUF9OT0RF"
    "TEFZLCAxKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICBwYXNzICNub3QgZmF0"
    "YWwsIGp1c3Qgc2xvd2VyCiAgICAgICAgdHJ5OgogICAgICAgICAgICAjQXNrIHRoZSBPUyB0byBw"
    "cm9iZSBhbiBpZGxlIGNvbm5lY3Rpb24uIFdoZW4gYSBwbGF5ZXIncyBnYW1lCiAgICAgICAgICAg"
    "ICNjcmFzaGVzIG91dHJpZ2h0IHRoZSBzb2NrZXQgaXMgdXN1YWxseSByZXNldCBhbmQgd2UgZmlu"
    "ZCBvdXQgYXQKICAgICAgICAgICAgI29uY2UsIGJ1dCBhIG1hY2hpbmUgdGhhdCBmcmVlemVzLCBz"
    "bGVlcHMgb3IgbG9zZXMgaXRzIGxpbmsgc2VuZHMKICAgICAgICAgICAgI25vdGhpbmcgYXQgYWxs"
    "OiB3aXRob3V0IHByb2JlcyB0aGF0IGNvbm5lY3Rpb24gc2l0cyB0aGVyZSBob2xkaW5nCiAgICAg"
    "ICAgICAgICN0aGUgYWNjb3VudCAoIkFjY291bnQgYWxyZWFkeSBsb2dnZWQgaW4iKSBhbmQgaXRz"
    "IHJvb20gdW50aWwgdGhlCiAgICAgICAgICAgICNpZGxlIHRpbWVvdXQgZXhwaXJlcyBtaW51dGVz"
    "IGxhdGVyLiBQcm9iZSBhZnRlciAzMHMgaWRsZSwgdGhlbgogICAgICAgICAgICAjZXZlcnkgNXMu"
    "CiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5TT0xfU09DS0VULCBz"
    "b2NrZXQuU09fS0VFUEFMSVZFLCAxKQogICAgICAgICAgICBpZiBoYXNhdHRyKHNlbGYucmVxdWVz"
    "dCwgJ2lvY3RsJykgYW5kIGhhc2F0dHIoc29ja2V0LCAnU0lPX0tFRVBBTElWRV9WQUxTJyk6CiAg"
    "ICAgICAgICAgICAgICBzZWxmLnJlcXVlc3QuaW9jdGwoc29ja2V0LlNJT19LRUVQQUxJVkVfVkFM"
    "UywgKDEsIDMwMDAwLCA1MDAwKSkgI1dpbmRvd3MKICAgICAgICAgICAgZWxzZToKICAgICAgICAg"
    "ICAgICAgIGZvciAob3B0LCB2YWwpIGluICgoJ1RDUF9LRUVQSURMRScsIDMwKSwgKCdUQ1BfS0VF"
    "UElOVFZMJywgNSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgKCdUQ1BfS0VF"
    "UENOVCcsIDQpKToKICAgICAgICAgICAgICAgICAgICBpZiBoYXNhdHRyKHNvY2tldCwgb3B0KToK"
    "ICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LklQ"
    "UFJPVE9fVENQLCBnZXRhdHRyKHNvY2tldCwgb3B0KSwgdmFsKQogICAgICAgIGV4Y2VwdCBPU0Vy"
    "cm9yOgogICAgICAgICAgICBwYXNzICNrZWVwYWxpdmUgaXMgYW4gb3B0aW1pc2F0aW9uLCBub3Qg"
    "YSByZXF1aXJlbWVudAogICAgZGVmIHNlbmRSYXcoc2VsZiwgbXNnKToKICAgICAgICAjVGhlIHNp"
    "bmdsZSBmdW5uZWwgZm9yIGV2ZXJ5IGJ5dGUgbGVhdmluZyB0aGUgc2VydmVyIG9uIHRoaXMgc29j"
    "a2V0LgogICAgICAgIHdpdGggc2VsZi5fc2VuZExvY2s6CiAgICAgICAgICAgIHNlbGYucmVxdWVz"
    "dC5zZW5kYWxsKG1zZykKICAgIGRlZiBzZW5kKHNlbGYsIG1zZyk6CiAgICAgICAgI05vcm1hbCBw"
    "YXRoIG9uY2UgdGhlIGNvbm5lY3Rpb24gaXMgbGl2ZTogaGFuZCBvZmYgdG8gdGhlIHdyaXRlciB0"
    "aHJlYWQKICAgICAgICAjc28gdGhlIGNhbGxlciAoYSBjb21tYW5kIGhhbmRsZXIsIG9yIHRoZSBk"
    "aXN0cmlidXRvcidzIGZhbi1vdXQpIG5ldmVyCiAgICAgICAgI2Jsb2NrcyBvbiBhIHNsb3cgb3Ig"
    "c3RhbGxlZCBjbGllbnQuCiAgICAgICAgaWYgbXNnOgogICAgICAgICAgICBzZWxmLl9zUXVldWUu"
    "cHV0KG1zZykKICAgIGRlZiBfd3JpdGVyTG9vcChzZWxmKToKICAgICAgICAjQmxvY2tzIG9uIHRo"
    "ZSBxdWV1ZSBpbnN0ZWFkIG9mIGJlaW5nIHBvbGxlZC4gUHJldmlvdXNseSB0aGUgcmVhZCBsb29w"
    "CiAgICAgICAgI2RyYWluZWQgdGhpcyBxdWV1ZSBpdHNlbGYgYmV0d2VlbiByZWN2KCkgdGltZW91"
    "dHMsIHNvIGFueXRoaW5nIHF1ZXVlZAogICAgICAgICNqdXN0IGFmdGVyIHRoZSB0aHJlYWQgd2Vu"
    "dCBiYWNrIGludG8gcmVjdigpIHdhaXRlZCBvdXQgdGhlIGZ1bGwKICAgICAgICAjdGltZW91dCAt"
    "IHVwIHRvIDEwMG1zIG9mIGxhdGVuY3kgYWRkZWQgdG8gZXZlcnkgcmVsYXllZCBnYW1lIGNvbW1h"
    "bmQsCiAgICAgICAgI29uIHRvcCBvZiBldmVyeSBpZGxlIGNvbm5lY3Rpb24gd2FraW5nIDEwIHRp"
    "bWVzIGEgc2Vjb25kIHRvIGNoZWNrLgogICAgICAgIHRyeToKICAgICAgICAgICAgd2hpbGUgVHJ1"
    "ZToKICAgICAgICAgICAgICAgIG1zZyA9IHNlbGYuX3NRdWV1ZS5nZXQoKQogICAgICAgICAgICAg"
    "ICAgaWYgbXNnIGlzIHNlbGYuX1NUT1BXUklURVI6CiAgICAgICAgICAgICAgICAgICAgYnJlYWsK"
    "ICAgICAgICAgICAgICAgICNDb2FsZXNjZSB3aGF0ZXZlciBlbHNlIHBpbGVkIHVwIGJlaGluZCBp"
    "dCBpbnRvIGEgc2luZ2xlIHdyaXRlLgogICAgICAgICAgICAgICAgI1Bvc2l0aW9uIGJyb2FkY2Fz"
    "dHMgYW5kIGdhbWUgY29tbWFuZHMgb2Z0ZW4gYXJyaXZlIGluIGJ1cnN0cy4KICAgICAgICAgICAg"
    "ICAgIGNodW5rcyA9IFttc2ddCiAgICAgICAgICAgICAgICB3aGlsZSBUcnVlOgogICAgICAgICAg"
    "ICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgICAgICAgICAgbnh0ID0gc2VsZi5fc1F1ZXVl"
    "LmdldF9ub3dhaXQoKQogICAgICAgICAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAg"
    "ICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICAgICAgaWYgbnh0IGlzIHNl"
    "bGYuX1NUT1BXUklURVI6CiAgICAgICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhiJycu"
    "am9pbihjaHVua3MpKQogICAgICAgICAgICAgICAgICAgICAgICByZXR1cm4KICAgICAgICAgICAg"
    "ICAgICAgICBjaHVua3MuYXBwZW5kKG54dCkKICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhi"
    "Jycuam9pbihjaHVua3MpKQogICAgICAgIGV4Y2VwdCAoQ29ubmVjdGlvblJlc2V0RXJyb3IsIENv"
    "bm5lY3Rpb25BYm9ydGVkRXJyb3IsIEJyb2tlblBpcGVFcnJvciwgT1NFcnJvcik6CiAgICAgICAg"
    "ICAgIHBhc3MgI3BlZXIgaXMgZ29uZTsgdGhlIHJlYWQgbG9vcCBub3RpY2VzIGFuZCBydW5zIHRo"
    "ZSBjbGVhbnVwCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcHJpbnQoJ1tM"
    "b2JieV0gV3JpdGVyIGVycm9yOlxuJyArIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICAgICAg"
    "ZmluYWxseToKICAgICAgICAgICAgc2VsZi5fd3JpdGVyRGVhZC5zZXQoKQogICAgZGVmIF9zdGFy"
    "dFdyaXRlcihzZWxmKToKICAgICAgICBzZWxmLl93cml0ZXIgPSB0aHJlYWRpbmcuVGhyZWFkKHRh"
    "cmdldD1zZWxmLl93cml0ZXJMb29wLCBkYWVtb249VHJ1ZSkKICAgICAgICBzZWxmLl93cml0ZXIu"
    "c3RhcnQoKQogICAgZGVmIF9zdG9wV3JpdGVyKHNlbGYpOgogICAgICAgIGlmIHNlbGYuX3dyaXRl"
    "ciBpcyBOb25lOgogICAgICAgICAgICByZXR1cm4KICAgICAgICBzZWxmLl9zUXVldWUucHV0KHNl"
    "bGYuX1NUT1BXUklURVIpCiAgICAgICAgc2VsZi5fd3JpdGVyLmpvaW4odGltZW91dD0yLjApCiAg"
    "ICAgICAgc2VsZi5fd3JpdGVyID0gTm9uZQogICAgZGVmIF9jbGFpbVNlc3Npb24oc2VsZik6CiAg"
    "ICAgICAgI1Rha2Ugb3duZXJzaGlwIG9mIHRoZSB1c2VybmFtZSBzbG90IGJlZm9yZSB0ZWxsaW5n"
    "IHRoZSBjbGllbnQgaXQgaXMKICAgICAgICAjbG9nZ2VkIGluLiBSZXR1cm5zIEZhbHNlIGlmIGFu"
    "b3RoZXIgY29ubmVjdGlvbiBnb3QgdGhlcmUgZmlyc3QuCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIu"
    "c3RhdGUuY2xhaW1Vc2VyKHNlbGYudXNlci5uYW1lLCBzZWxmKToKICAgICAgICAgICAgcmV0dXJu"
    "IFRydWUKICAgICAgICBzZWxmLnVzZXIuZGlzY29ubmVjdChzZWxmLnNlcnZlcikgI3JlbGVhc2Vz"
    "IHRoZSBpZG51bSB3ZSBqdXN0IGFsbG9jYXRlZAogICAgICAgIHNlbGYudXNlciA9IE5vbmUKICAg"
    "ICAgICByZXR1cm4gRmFsc2UKICAgIGRlZiBhdHRlbXB0TG9naW4oc2VsZiwgdXNlcm5hbWUsIHBh"
    "c3N3b3JkKToKICAgICAgICBpZiBsZW4odXNlcm5hbWUpPDE6CiAgICAgICAgICAgIHJldHVybiA0"
    "ICNObyBVc2VybmFtZSwgbGlrZWx5IGZyZXNoIGxvZ2luCiAgICAgICAgICAgICNUT0RPIGNoZWNr"
    "IGlmIHNlcmlhbCBleGlzdHMgYW5kIHJldHVybiB1c2VybmFtZSBwcm9wZXJseQogICAgICAgIGlm"
    "IGxlbihwYXNzd29yZCk8MToKICAgICAgICAgICAgcmV0dXJuIDMgI1Bhc3N3b3JkIHRvbyBzaG9y"
    "dAogICAgICAgICNUZXN0IGlmIHBsYXllciBhbHJlYWR5IGxvZ2dlZCBpbiAoZmFzdCBwYXRoOyB0"
    "aGUgYXV0aG9yaXRhdGl2ZSwKICAgICAgICAjcmFjZS1mcmVlIGNoZWNrIGlzIHRoZSBjbGFpbVVz"
    "ZXIoKSBiZWxvdykKICAgICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIodXNlcm5hbWUpOgog"
    "ICAgICAgICAgICByZXR1cm4gMiAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVSUk9SCiAgICAgICAg"
    "I3BsYXllciBub3QgY3VycmVudGx5IGxvZ2dlZCBpbiwgYXR0ZW1wdCB0byBsb2dpbiB2aWEgZGF0"
    "YSBoYW5kbGVyCiAgICAgICAgc2VsZi51c2VyID0gR0RILmxvZ2luUGxheWVyKHVzZXJuYW1lLCBz"
    "ZWxmLCBwYXNzd29yZCkKICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgIHJldHVybiAw"
    "IGlmIHNlbGYuX2NsYWltU2Vzc2lvbigpIGVsc2UgMgogICAgICAgIHJldHVybiAxICNUT0RPIEdl"
    "dCBmcm9tIEdESC5sb2dpblBsYXllciwgcGFzcyB1c2VyIG9iamVjdCBhbG9uZz8KICAgIGRlZiBh"
    "dHRlbXB0UmVnaXN0ZXIoc2VsZiwgdXNlcm5hbWUsIHBhc3N3b3JkLCBlbWFpbCwgbG9jYXRpb24s"
    "IGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgI1Rlc3QgaWYgcGxheWVyIGFscmVh"
    "ZHkgbG9nZ2VkIGluCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuZ2V0UGxheWVyKHVzZXJuYW1lKToK"
    "ICAgICAgICAgICAgcmV0dXJuIDEgI1RPRE8gUExBWUVSIExPR0dFRCBJTiBFUlJPUgogICAgICAg"
    "IHNlbGYudXNlciA9IEdESC5yZWdpc3RlclBsYXllcih1c2VybmFtZSwgc2VsZiwgcGFzc3dvcmQs"
    "IGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKQogICAgICAgIGlmIHNl"
    "bGYudXNlcjoKICAgICAgICAgICAgcmV0dXJuIDAgaWYgc2VsZi5fY2xhaW1TZXNzaW9uKCkgZWxz"
    "ZSAxCiAgICAgICAgcmV0dXJuIDIgI1RPRE8gZ2V0IGVycm9yIGZyb20gR0RICiAgICBkZWYgaGFu"
    "ZGxlKHNlbGYpOgogICAgICAgIHRyeTogI0ludGVyY2VwdCBhbmQgcHJpbnQgZXJyb3JzIGZvciBk"
    "ZWJ1Z2dpbmcKICAgICAgICAgICAgc2VsZi5faGFuZGxlKCkKICAgICAgICAgICAgI1RPRE8gbG9v"
    "cCBsb2JieSBoYW5kbGUgYmV0dGVyIHRvIGhhbmRsZSBleGNlcHRpb25zIGdyYWNlZnVsbHkKICAg"
    "ICAgICAgICAgc2VsZi5fbG9iYnlIYW5kbGUoKQogICAgICAgIGV4Y2VwdCBQcm90b2NvbEVycm9y"
    "IGFzIGU6CiAgICAgICAgICAgICNtYWxmb3JtZWQvb3ZlcnNpemVkIGlucHV0IC0gdGhlIGNsaWVu"
    "dCdzIGZhdWx0LCBub3Qgb3Vycy4gRHJvcCB0aGUKICAgICAgICAgICAgI2Nvbm5lY3Rpb24gd2l0"
    "aCBvbmUgbGluZSBpbnN0ZWFkIG9mIGEgdHJhY2ViYWNrLgogICAgICAgICAgICB3aG8gPSBzZWxm"
    "LnVzZXIubmFtZSBpZiBzZWxmLnVzZXIgZWxzZSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAg"
    "ICAgICAgIHByaW50KGYnW0xvYmJ5XSBQcm90b2NvbCBlcnJvciBmcm9tIHt3aG99OiB7ZX0nKQog"
    "ICAgICAgIGV4Y2VwdCAoemxpYi5lcnJvciwgc3RydWN0LmVycm9yLCBVbmljb2RlRGVjb2RlRXJy"
    "b3IpIGFzIGU6CiAgICAgICAgICAgICN0cnVuY2F0ZWQvZ2FyYmFnZSBwYWNrZXQ6IHBhcnNlRHN0"
    "ciBhbmQgc3RydWN0LnVucGFjayBib3RoIHJhaXNlIG9uCiAgICAgICAgICAgICNzaG9ydCByZWFk"
    "cywgYW5kIC5kZWNvZGUoKSBvbiBub24tYXNjaWkganVuay4gU2FtZSBjYXRlZ29yeS4KICAgICAg"
    "ICAgICAgcHJpbnQoZidbTG9iYnldIE1hbGZvcm1lZCBwYWNrZXQgZnJvbSB7c2VsZi5jbGllbnRf"
    "YWRkcmVzc1swXX06ICcKICAgICAgICAgICAgICAgICAgZid7dHlwZShlKS5fX25hbWVfX306IHtl"
    "fScpCiAgICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVzZXRFcnJvciwgQ29ubmVjdGlvbkFib3J0"
    "ZWRFcnJvciwgT1NFcnJvcikgYXMgZToKICAgICAgICAgICAgIyBleHBlY3RlZCBmb3JtIG9mIGRp"
    "c2Nvbm5lY3Rpb24gKGluY2x1ZGluZyBhIGZvcmNlZCBhZG1pbiBraWNrKSwKICAgICAgICAgICAg"
    "IyBidXQgbGVhdmUgYSBvbmUtbGluZSBicmVhZGNydW1iIHJhdGhlciB0aGFuIHN0YXlpbmcgZnVs"
    "bHkgc2lsZW50CiAgICAgICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgICAgIHByaW50"
    "KGYnW0xvYmJ5XSBDb25uZWN0aW9uIGNsb3NlZCBmb3Ige3NlbGYudXNlci5uYW1lfToge2V9JykK"
    "ICAgICAgICBleGNlcHQgRXhjZXB0aW9uOiMgYXMgZToKICAgICAgICAgICAgcHJpbnQodHJhY2Vi"
    "YWNrLmZvcm1hdF9leGMoKSkKICAgICAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICAg"
    "ICAgcHJpbnQoZidVc2VyOiB7c2VsZi51c2VyLm5hbWV9JykKICAgICAgICAgICAgI3JhaXNlIGUK"
    "ICAgIGRlZiBfbG9iYnlIYW5kbGUoc2VsZik6CiAgICAgICAgI2FjdGl2ZVVzZXJzWy4uLl0gPSBz"
    "ZWxmIHVzZWQgdG8gaGFwcGVuIGhlcmU7IGl0IG5vdyBoYXBwZW5zIHVuZGVyIGEKICAgICAgICAj"
    "bG9jayBpbnNpZGUgYXR0ZW1wdExvZ2luL2F0dGVtcHRSZWdpc3RlciwgYmVmb3JlIHRoZSB3ZWxj"
    "b21lIHBhY2tldAogICAgICAgICNnb2VzIG91dCwgc28gdHdvIGxvZ2lucyBmb3Igb25lIGFjY291"
    "bnQgY2FuJ3QgYm90aCBzdWNjZWVkLgogICAgICAgIHByaW50KGYnVXNlcjoge3NlbGYudXNlci5u"
    "YW1lfSBDb25uZWN0ZWQnKQogICAgICAgICNGcm9tIGhlcmUgb24gbm90aGluZyB3cml0ZXMgdG8g"
    "dGhlIHNvY2tldCBpbmxpbmU6IHRoZSB3cml0ZXIgdGhyZWFkCiAgICAgICAgI293bnMgdGhlIG91"
    "dGJvdW5kIGRpcmVjdGlvbiBhbmQgdGhpcyBsb29wIG9ubHkgcmVhZHMuCiAgICAgICAgc2VsZi5f"
    "c3RhcnRXcml0ZXIoKQogICAgICAgIHNlbGYuX2xhc3RSZWN2ID0gdGltZS5tb25vdG9uaWMoKQog"
    "ICAgICAgICNUaGUgc29ja2V0IHN0YXlzIGluIGJsb2NraW5nIG1vZGUgZm9yIGl0cyB3aG9sZSBs"
    "aWZlIGZyb20gaGVyZSBvbiwgYW5kCiAgICAgICAgI3JlYWRpbmVzcyBpcyB3YWl0ZWQgZm9yIHdp"
    "dGggc2VsZWN0KCkgaW5zdGVhZCBvZiBhIHNvY2tldCB0aW1lb3V0LgogICAgICAgICNUaGlzIGlz"
    "IG5vdCBhIHN0eWxlIHByZWZlcmVuY2UgLSBhIHNvY2tldCB0aW1lb3V0IGlzIGEgcHJvcGVydHkg"
    "b2YgdGhlCiAgICAgICAgIypzb2NrZXQqLCBub3Qgb2YgdGhlIGNhbGwsIHNvIHRoZSBzZXR0aW1l"
    "b3V0KF9SRUFEX1RJTUVPVVQpIHRoaXMgbG9vcAogICAgICAgICN1c2VkIHRvIGRvIG9uIGV2ZXJ5"
    "IHBhc3MgYWxzbyBhcm1lZCBhIDFzIHRpbWVvdXQgb24gdGhlIHdyaXRlcgogICAgICAgICN0aHJl"
    "YWQncyBjb25jdXJyZW50IHNlbmRhbGwoKS4gQSBjbGllbnQgd2hvc2UgcmVjZWl2ZSB3aW5kb3cg"
    "d2FzIGZ1bGwKICAgICAgICAjZm9yIGEgc2Vjb25kIChleGFjdGx5IHRoZSBjYXNlIGR1cmluZyBh"
    "IGJ1c3kgY28tb3Agc2Vzc2lvbikgbWFkZSB0aGF0CiAgICAgICAgI3NlbmRhbGwoKSByYWlzZSBU"
    "aW1lb3V0RXJyb3IgKmFmdGVyIGhhdmluZyBhbHJlYWR5IHdyaXR0ZW4gcGFydCBvZiB0aGUKICAg"
    "ICAgICAjcGFja2V0KjogdGhlIHdyaXRlciB0aHJlYWQgZGllZCwgYW5kIHdoYXRldmVyIHRoZSBj"
    "bGllbnQgaGFkIHJlY2VpdmVkCiAgICAgICAgI3dhcyBoYWxmIGEgbWVzc2FnZSwgc28gaXRzIGNv"
    "bW1hbmQgc3RyZWFtIHdhcyBkZXN5bmNocm9uaXNlZCBmcm9tCiAgICAgICAgI3RoYXQgcG9pbnQg"
    "b24uIHNlbGVjdCgpIGxlYXZlcyB0aGUgc29ja2V0IGJsb2NraW5nLCBzbyB3cml0ZXMgYXJlCiAg"
    "ICAgICAgI25ldmVyIGludGVycnVwdGVkLCB3aGlsZSByZWFkcyBzdGlsbCB3YWtlIHVwIHJlZ3Vs"
    "YXJseSBlbm91Z2ggdG8KICAgICAgICAjbm90aWNlIHNodXRkb3duIGFuZCB0aGUgaWRsZSBkZWFk"
    "bGluZS4KICAgICAgICBzZWxmLnJlcXVlc3Quc2V0dGltZW91dChOb25lKQogICAgICAgIHdoaWxl"
    "IFRydWU6CiAgICAgICAgICAgIGlmIHNlbGYuX3dyaXRlckRlYWQuaXNfc2V0KCk6CiAgICAgICAg"
    "ICAgICAgICBicmVhayAjcGVlciB3ZW50IGF3YXkgd2hpbGUgd2Ugd2VyZSBzZW5kaW5nCiAgICAg"
    "ICAgICAgIGlmIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nOgogICAgICAgICAgICAgICAgYnJlYWsg"
    "I3NlcnZlciBpcyBzdG9wcGluZyAtIGNoZWNrZWQgaGVyZSwgbm90IG9ubHkgb24gYW4gaWRsZQog"
    "ICAgICAgICAgICAgICAgICAgICAgI3RpbWVvdXQsIHNvIGEgY2xpZW50IHRoYXQga2VlcHMgdGFs"
    "a2luZyBjYW5ub3Qga2VlcCBpdHMKICAgICAgICAgICAgICAgICAgICAgICNoYW5kbGVyIHRocmVh"
    "ZCAoYW5kIGl0cyBsb2cgc3BhbSkgYWxpdmUgcGFzdCBzaHV0ZG93bgogICAgICAgICAgICB0cnk6"
    "CiAgICAgICAgICAgICAgICByZWFkeSwgXywgXyA9IHNlbGVjdC5zZWxlY3QoW3NlbGYucmVxdWVz"
    "dF0sIFtdLCBbXSwgX1JFQURfVElNRU9VVCkKICAgICAgICAgICAgZXhjZXB0IChPU0Vycm9yLCBW"
    "YWx1ZUVycm9yKToKICAgICAgICAgICAgICAgIGJyZWFrICNzb2NrZXQgY2xvc2VkIHVuZGVyIHVz"
    "IChhZG1pbiBraWNrIC8gc2h1dGRvd24pCiAgICAgICAgICAgIGlmIG5vdCByZWFkeToKICAgICAg"
    "ICAgICAgICAgIGlmIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nOgogICAgICAgICAgICAgICAgICAg"
    "IGJyZWFrICNTZXJ2ZXIgU2h1dHRpbmcgZG93bgogICAgICAgICAgICAgICAgaWYgX0lETEVfVElN"
    "RU9VVCBhbmQgKHRpbWUubW9ub3RvbmljKCkgLSBzZWxmLl9sYXN0UmVjdikgPiBfSURMRV9USU1F"
    "T1VUOgogICAgICAgICAgICAgICAgICAgICNIYWxmLW9wZW4gY29ubmVjdGlvbjogdGhlIHBlZXIg"
    "aXMgdW5yZWFjaGFibGUgYnV0IG5ldmVyCiAgICAgICAgICAgICAgICAgICAgI3NlbnQgYSBGSU4v"
    "UlNULCBzbyByZWN2KCkgYmxvY2tzIGZvcmV2ZXIgYW5kIHRoZSBhY2NvdW50CiAgICAgICAgICAg"
    "ICAgICAgICAgI3N0YXlzIGNsYWltZWQuIFJlYXAgaXQgc28gdGhlIHBsYXllciBjYW4gbG9nIGJh"
    "Y2sgaW4uCiAgICAgICAgICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIHtzZWxmLnVzZXIubmFt"
    "ZX0gaWRsZSBmb3Ige19JRExFX1RJTUVPVVR9cywgZHJvcHBpbmcnKQogICAgICAgICAgICAgICAg"
    "ICAgIGJyZWFrCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBybXNnID0gc2Vs"
    "Zi5yZXF1ZXN0LnJlY3YoUkVDVl9CVUZfTEVOKSAjVE9ETyBsb2cgbmV0d29yayBieXRlcmF0ZQog"
    "ICAgICAgICAgICBpZiBub3Qgcm1zZzoKICAgICAgICAgICAgICAgIGJyZWFrICNEaXNjb25uZWN0"
    "ZWQKICAgICAgICAgICAgc2VsZi5kYXRhKz1ybXNnCiAgICAgICAgICAgIHNlbGYuX2xhc3RSZWN2"
    "ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAgICAgICB3aGlsZSBzZWxmLmRhdGE6CiAgICAgICAg"
    "ICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAgICAgY21kX2wgPSBzZWxmLmRhdGEuaW5kZXgo"
    "MCkKICAgICAgICAgICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICAgICAgICAg"
    "ICNwcmludCgnY21kIGRlY29kZSBlcnJvcjpcbicsIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAg"
    "ICAgICAgICAgICAgICAgICAgYnJlYWs7I01heSByZXF1aXJlIG1vcmUgZGF0YQogICAgICAgICAg"
    "ICAgICAgY21kID0gd2lyZV9kZWNvZGUoc2VsZi5kYXRhWzA6Y21kX2xdKQogICAgICAgICAgICAg"
    "ICAgc2VsZi5kYXRhID0gc2VsZi5kYXRhW2NtZF9sKzE6XQogICAgICAgICAgICAgICAgcmVzcG9u"
    "c2UgPSBzZWxmLnNlcnZlci5jb21wYXJzLnBhcnNlKGNtZCwgc2VsZikKICAgICAgICAgICAgICAg"
    "IGlmIHJlc3BvbnNlOgogICAgICAgICAgICAgICAgICAgICNRdWV1ZWQgcmF0aGVyIHRoYW4gc2Vu"
    "dCBpbmxpbmUsIHNvIHRoaXMgY29ubmVjdGlvbiBoYXMgYQogICAgICAgICAgICAgICAgICAgICNz"
    "aW5nbGUgb3JkZXJlZCBvdXRib3VuZCBzdHJlYW0uIFNlbmRpbmcgaGVyZSBkaXJlY3RseQogICAg"
    "ICAgICAgICAgICAgICAgICN3b3VsZCByYWNlIHRoZSB3cml0ZXIgdGhyZWFkIGFuZCBjb3VsZCBs"
    "YW5kIGluIHRoZSBtaWRkbGUKICAgICAgICAgICAgICAgICAgICAjb2YgYSBicm9hZGNhc3QgaXQg"
    "aXMgYWxyZWFkeSB3cml0aW5nLgogICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZChyZXNwb25z"
    "ZSkKICAgICAgICAgICAgICAgICNMb29zZSBibG9icyBzaG91bGQgbm90IGhhcHBlbiBhbnltb3Jl"
    "IGhvcGVmdWxseQogICAgICAgICAgICAgICAgI1RPRE8gZml4IHVuY29tcHJlc3NlZCBkYXRhIGJs"
    "b2JzPwogICAgICAgICAgICAgICAgI1RPRE8gc2tpcCAxIGJ5dGUgb25seSB3aGVuIGRlY29kZSBl"
    "cnJvcj8KICAgICAgICAgICAgICAgIGlmIChsZW4oc2VsZi5kYXRhKT4yIGFuZAogICAgICAgICAg"
    "ICAgICAgICAgICAgICBzZWxmLmRhdGFbMF09PTB4NzggYW5kCiAgICAgICAgICAgICAgICAgICAg"
    "ICAgIHNlbGYuZGF0YVsxXT09MHg5Yyk6CiAgICAgICAgICAgICAgICAgICAgI0xvb3NlIHVuaGFu"
    "ZGxlZCBibG9iIGFmdGVyIGNvbW1hbmQKICAgICAgICAgICAgICAgICAgICBibG9iLCBzZWxmLmRh"
    "dGEgPSBwX2dldEJsb2Ioc2VsZi5kYXRhLCBzZWxmLnJlcXVlc3QpCiAgICAgICAgICAgICAgICAg"
    "ICAgI1RoZSBvdGhlciBibGluZCBzcG90OiBhbnl0aGluZyB0aGUgY2xpZW50IHNlbmRzIGFzIGEK"
    "ICAgICAgICAgICAgICAgICAgICAjY29tcHJlc3NlZCBibG9iIHJhdGhlciB0aGFuIGEgdGV4dCBj"
    "b21tYW5kIHdhcyByZWFkIGFuZAogICAgICAgICAgICAgICAgICAgICN0aHJvd24gYXdheSB3aXRo"
    "b3V0IGEgdHJhY2UuCiAgICAgICAgICAgICAgICAgICAgaWYgX0RFQlVHX0xPR19DT01NQU5EUzoK"
    "ICAgICAgICAgICAgICAgICAgICAgICAgd2hvID0gc2VsZi51c2VyLm5hbWUgaWYgc2VsZi51c2Vy"
    "IGVsc2UgJz8nCiAgICAgICAgICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gLT4g"
    "KFVOSEFORExFRCBCTE9CIGFmdGVyIHtjbWQhcn0pICcKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgZid7bGVuKGJsb2IpfSBieXRlcycpCiAgICBkZWYgX3JlY3ZNb3JlKHNlbGYpOgogICAg"
    "ICAgIGNodW5rID0gc2VsZi5yZXF1ZXN0LnJlY3YoUkVDVl9CVUZfTEVOKQogICAgICAgIGlmIG5v"
    "dCBjaHVuazoKICAgICAgICAgICAgI3BlZXIgZGlzY29ubmVjdGVkIGR1cmluZyBoYW5kc2hha2Uv"
    "bG9naW4sIHN0b3AgdGhlIGJ1c3ktbG9vcAogICAgICAgICAgICByYWlzZSBDb25uZWN0aW9uUmVz"
    "ZXRFcnJvcignZGlzY29ubmVjdGVkIGR1cmluZyBsb2dpbicpCiAgICAgICAgc2VsZi5kYXRhICs9"
    "IGNodW5rCiAgICBkZWYgX2hhbmRsZShzZWxmKToKICAgICAgICAjVE9ETyBsb2cgbG9naW4gYXR0"
    "ZW1wdHM/CiAgICAgICAgcGVlcl9pcCA9IHNlbGYuY2xpZW50X2FkZHJlc3NbMF0KICAgICAgICBw"
    "cmludCgnQ29ubmVjdGlvbiBhdHRlbXB0IGZyb206JywgcGVlcl9pcCkKICAgICAgICBMSVMgPSAy"
    "ICNsb2dpbiBzdGF0ZSAjVE9ETyBjb25zaWRlciBsb25nIHRpbWVvdXRzPwogICAgICAgIHdoaWxl"
    "IExJUzoKICAgICAgICAgICAgd2hpbGUgbGVuKHNlbGYuZGF0YSk8NDoKICAgICAgICAgICAgICAg"
    "IHNlbGYuX3JlY3ZNb3JlKCkKICAgICAgICAgICAgcGFja19sZW4gPSBzdHJ1Y3QudW5wYWNrKCc8"
    "SScsc2VsZi5kYXRhWzA6NF0pWzBdCiAgICAgICAgICAgIGlmIHBhY2tfbGVuIDwgNCBvciBwYWNr"
    "X2xlbiA+IF9NQVhfSEFORFNIQUtFOgogICAgICAgICAgICAgICAgI3VudmFsaWRhdGVkLCB0aGlz"
    "IGlzIGEgcHJlLWF1dGhlbnRpY2F0aW9uIG1lbW9yeSBib21iOiBhbgogICAgICAgICAgICAgICAg"
    "I3VuYXV0aGVudGljYXRlZCBwZWVyIGFubm91bmNlcyBhIDRHQiBwYWNrZXQgYW5kIHRoZSBsb29w"
    "IGJlbG93CiAgICAgICAgICAgICAgICAjYnVmZmVycyB1bnRpbCB0aGUgcHJvY2VzcyBkaWVzCiAg"
    "ICAgICAgICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnaGFuZHNoYWtlIHBhY2tldCBsZW5n"
    "dGgge3BhY2tfbGVufSBvdXQgb2YgcmFuZ2UnKQogICAgICAgICAgICB3aGlsZShsZW4oc2VsZi5k"
    "YXRhKTxwYWNrX2xlbik6CiAgICAgICAgICAgICAgICBzZWxmLl9yZWN2TW9yZSgpCiAgICAgICAg"
    "ICAgICNzbGljZSB0byBwYWNrX2xlbiAobm90IHRvIHRoZSBlbmQgb2YgdGhlIGJ1ZmZlcik6IGFu"
    "eXRoaW5nIHBhc3QKICAgICAgICAgICAgI3RoaXMgcGFja2V0IGJlbG9uZ3MgdG8gdGhlIG5leHQg"
    "b25lLiBCb3VuZGVkIGRlY29tcHJlc3MsIGJlY2F1c2UgYQogICAgICAgICAgICAjNjRrIGhhbmRz"
    "aGFrZSBvZiBjb21wcmVzc2VkIHplcm9lcyBleHBhbmRzIHRvIGh1bmRyZWRzIG9mIE1CLgogICAg"
    "ICAgICAgICByZXMgPSBfZGVjb21wcmVzc19ib3VuZGVkKHNlbGYuZGF0YVs0OnBhY2tfbGVuXSwg"
    "X01BWF9IQU5EU0hBS0VfSU5GTEFURUQpCiAgICAgICAgICAgIHNlbGYuZGF0YSA9IHNlbGYuZGF0"
    "YVtwYWNrX2xlbjpdCiAgICAgICAgICAgIGlmIExJUyA9PSAyOgogICAgICAgICAgICAgICAgZ2Ft"
    "ZXZlcnNpb24gPSByZXNbMDoxNl0gI1RPRE8gbm90ZSBnYW1lIHZlcnNpb24gKHVudmVyaWZpZWQp"
    "IHBlciB1c2VyCiAgICAgICAgICAgICAgICBsYW5nbmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywg"
    "MTYpCiAgICAgICAgICAgICAgICAjVE9ETyBjb25zaWRlciBUV1NFIGluZGljYXRvciB0byBjcmVh"
    "dGUgc2VjdXJlIGNvbm5lY3Rpb24/CiAgICAgICAgICAgICAgICAjVE9ETyBjaGVjayBpZiB2YW5p"
    "bGxhIHNlcnZlciBpZ25vcmVzIGV4dHJhIGRhdGEgaW4gaGFuZHNoYWtlIHByb2Nlc3MKICAgICAg"
    "ICAgICAgICAgIFJLID0gcmVzW29mZis4Om9mZisxNl0KICAgICAgICAgICAgICAgIGZvciBpIGlu"
    "IHJhbmdlKGxlbihSSykpOgogICAgICAgICAgICAgICAgICAgIHNlbGYuU0tbaV1ePVJLW2ldCiAg"
    "ICAgICAgICAgICAgICAjd2FzIGhhcmRjb2RlZCAnVFcxQ1MnIHdpdGggYSAiU0VSVkVSIE5BTUUg"
    "Y2ZnVE9ETyIgbm90ZTogdGhlCiAgICAgICAgICAgICAgICAjbmFtZSBjb25maWd1cmVkIGluIENv"
    "bmZpZy5pbmkvdGhlIEdVSSByZWFjaGVkIHRoZSB3ZWxjb21lCiAgICAgICAgICAgICAgICAjcGFj"
    "a2V0IGJ1dCBuZXZlciB0aGlzIG9uZSwgc28gdGhlIHByZS1sb2dpbiBoYW5kc2hha2UgYWx3YXlz"
    "CiAgICAgICAgICAgICAgICAjYW5ub3VuY2VkIHRoZSBwbGFjZWhvbGRlci4KICAgICAgICAgICAg"
    "ICAgIHNlbGYuc2VuZFJhdyhfc2VydmVyX2luZm9fcGFja2V0KHNhbml0aXplVGV4dChERUZBVUxU"
    "X1RJVExFKSkpCiAgICAgICAgICAgICAgICAjVE9ETyBUVzFDUyBpbmRpY2F0b3IgZm9yIFRXU0Ug"
    "Y2xpZW50IHRvIGNyZWF0ZSBzZWN1cmUgY29ubmVjdGlvbiBvciBwcmUtaGFzaCBwYXNzd29yZD8K"
    "ICAgICAgICAgICAgICAgIExJUyA9IDEgCiAgICAgICAgICAgICAgICBzZWxmLlNLID0gYnl0ZXMo"
    "c2VsZi5TSykKICAgICAgICAgICAgZWxpZiBMSVMgPT0gMToKICAgICAgICAgICAgICAgIGxvZ2lu"
    "RXJyb3IgPSAtMQogICAgICAgICAgICAgICAgI1N0YWxsIHJlcGVhdCBvZmZlbmRlcnMgYmVmb3Jl"
    "IGRvaW5nIGFueSBQQktERjIgd29yayBmb3IgdGhlbS4KICAgICAgICAgICAgICAgICNTbGVlcGlu"
    "ZyBpbiB0aGlzIGhhbmRsZXIgdGhyZWFkIGlzIHRoZSBwb2ludDogaXQgY29zdHMgdXMKICAgICAg"
    "ICAgICAgICAgICNub3RoaW5nIGFuZCByYXRlLWxpbWl0cyB0aGF0IGNvbm5lY3Rpb24uCiAgICAg"
    "ICAgICAgICAgICBkZWxheSA9IExPR0lOX1RIUk9UVExFLmRlbGF5Rm9yKHBlZXJfaXApCiAgICAg"
    "ICAgICAgICAgICBpZiBkZWxheToKICAgICAgICAgICAgICAgICAgICB0aW1lLnNsZWVwKGRlbGF5"
    "KQogICAgICAgICAgICAgICAgdXNlcm5hbWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDApCiAgICAg"
    "ICAgICAgICAgICBwYXNzd29yZCwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAg"
    "ICAgICAgI1RPRE8gVFdTRSBtb2QgZm9yIGhpZ2hlciBsb2dpbiBzZWN1cml0eQogICAgICAgICAg"
    "ICAgICAgIy1lbmNyeXB0ZWQgY29ubmVjdGlvbiB0byBwcmV2ZW50IHJlcGxheSBhdHRhY2tzCiAg"
    "ICAgICAgICAgICAgICAjLXByZWhhc2ggcGFzc3dvcmQgd2l0aCBzZXJpYWw/LCBjaGVjayBpZiBy"
    "ZWNvdmVyeSBwb3NzaWJsZS4KICAgICAgICAgICAgICAgIHNlbGYuZ3VpZCA9IGJ5dGVzKHJlc1tv"
    "ZmY6b2ZmKzE2XSkKICAgICAgICAgICAgICAgICNwcmludCgnZ3VpZCBieXRlOicsIHNlbGYuZ3Vp"
    "ZFsxXSkKICAgICAgICAgICAgICAgICNzZWxmLmd1aWQgPSBieXRlYXJyYXkocmVzW29mZjpvZmYr"
    "MTZdKQogICAgICAgICAgICAgICAgI3NlbGYuZ3VpZFsxXV49MHgxNiAjRE8gTk9UIHBlcmZvcm0g"
    "c2VydmVyc2lkZQogICAgICAgICAgICAgICAgI3NlbGYuZ3VpZCA9IGJ5dGVzKHNlbGYuZ3VpZCkK"
    "ICAgICAgICAgICAgICAgIG9mZis9MTYKICAgICAgICAgICAgICAgIGlzcmVnID0gc3RydWN0LnVu"
    "cGFjaygnPEknLHJlc1tvZmY6b2ZmKzRdKVswXQogICAgICAgICAgICAgICAgb2ZmKz00CiAgICAg"
    "ICAgICAgICAgICB2aWFSZWdpc3RlciA9IGJvb2woaXNyZWcpCiAgICAgICAgICAgICAgICBpZiBp"
    "c3JlZzoKICAgICAgICAgICAgICAgICAgICBlbWFpbCwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2Zm"
    "KQogICAgICAgICAgICAgICAgICAgIGxvY2F0aW9uLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYp"
    "CiAgICAgICAgICAgICAgICAgICAgYWdlID0gcmVzW29mZl0KICAgICAgICAgICAgICAgICAgICBn"
    "ZW5kZXIgPSByZXNbb2ZmKzFdCiAgICAgICAgICAgICAgICAgICAgb2ZmKz0yICNhZ2UsIGdlbmRl"
    "cgogICAgICAgICAgICAgICAgICAgIGRlc2NyaXB0aW9uLCBvZmYgPSBwYXJzZURzdHIocmVzLCBv"
    "ZmYpCiAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IHNlbGYuYXR0ZW1wdFJlZ2lzdGVy"
    "KHVzZXJuYW1lLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3Jp"
    "cHRpb24pCiAgICAgICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgICAgIGxvZ2luRXJy"
    "b3IgPSBzZWxmLmF0dGVtcHRMb2dpbih1c2VybmFtZSwgcGFzc3dvcmQpCiAgICAgICAgICAgICAg"
    "ICAgICAgaWYgbG9naW5FcnJvciA9PSAxIGFuZCBfQVVUT19SRUdJU1RFUjoKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgdmlhUmVnaXN0ZXIgPSBUcnVlCiAgICAgICAgICAgICAgICAgICAgICAgIGxv"
    "Z2luRXJyb3IgPSBzZWxmLmF0dGVtcHRSZWdpc3Rlcih1c2VybmFtZSwgcGFzc3dvcmQsICIiLCAi"
    "IiwgMSwgMCwgIiIpCiAgICAgICAgICAgICAgICBpZiBsb2dpbkVycm9yID09IDA6CiAgICAgICAg"
    "ICAgICAgICAgICAgTE9HSU5fVEhST1RUTEUucmVjb3JkU3VjY2VzcyhwZWVyX2lwKQogICAgICAg"
    "ICAgICAgICAgICAgICNUT0RPIGJldHRlciBoYW5kbGluZyBvZiBUSVRMRSBBTkQgTU9URAogICAg"
    "ICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhfc2VydmVyX3dlbGNvbWVfcGFja2V0KGJ5dGVz"
    "KHNlbGYuU0spLCBERUZBVUxUX1RJVExFLCBERUZBVUxUX01PVEQpKQogICAgICAgICAgICAgICAg"
    "ICAgIExJUyA9IDAKICAgICAgICAgICAgICAgIGVsc2U6ICNlcnJvciBiYXNlZCBvbiBsb2dpbkVy"
    "cm9yIG51bWJlcgogICAgICAgICAgICAgICAgICAgIGNvdW50ID0gTE9HSU5fVEhST1RUTEUucmVj"
    "b3JkRmFpbHVyZShwZWVyX2lwKQogICAgICAgICAgICAgICAgICAgIGlmIGNvdW50ID09IF9MT0dJ"
    "Tl9GQUlMX0xJTUlUOgogICAgICAgICAgICAgICAgICAgICAgICBwcmludChmJ1tMb2JieV0gVGhy"
    "b3R0bGluZyBsb2dpbnMgZnJvbSB7cGVlcl9pcH0gJwogICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICBmJyh7Y291bnR9IGZhaWx1cmVzIGluIHtfTE9HSU5fRkFJTF9XSU5ET1d9cyknKQogICAg"
    "ICAgICAgICAgICAgICAgIGVycm1zZ3MgPSBfUkVHSVNURVJfRVJST1JTIGlmIHZpYVJlZ2lzdGVy"
    "IGVsc2UgX0xPR0lOX0VSUk9SUwogICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZFJhdyhfaW5p"
    "dF9lcnJvcihlcnJtc2dzLmdldChsb2dpbkVycm9yLCAnTG9naW4gZmFpbGVkJykpKQogICAgZGVm"
    "IGZpbmlzaChzZWxmKToKICAgICAgICBzZWxmLnNlcnZlci51bnJlZ2lzdGVyQ29ubmVjdGlvbihz"
    "ZWxmKQogICAgICAgICNTdG9wIHRoZSB3cml0ZXIgZmlyc3Q6IGl0IGhvbGRzIHRoaXMgc29ja2V0"
    "IGFuZCB3b3VsZCBvdGhlcndpc2Uga2VlcAogICAgICAgICN3cml0aW5nIG9uIGJlaGFsZiBvZiBh"
    "IHBsYXllciB3aG8gaGFzIGFscmVhZHkgbGVmdCBldmVyeSBjaGFubmVsLgogICAgICAgIHNlbGYu"
    "X3N0b3BXcml0ZXIoKQogICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgcHJpbnQoZidV"
    "c2VyOiB7c2VsZi51c2VyLm5hbWV9IERpc2Nvbm5lY3RlZCcpCiAgICAgICAgICAgIHNlbGYudXNl"
    "ci5kaXNjb25uZWN0KHNlbGYuc2VydmVyKQogICAgICAgICNjbGVhbnVwIHVzZXIgZGF0YQogICAg"
    "ICAgICNUT0RPIGNoZWNrIGlmIHRyaWdnZXJlZCBvbiBjcmFzaGVkIGNvbm5lY3Rpb24KICAgIGRl"
    "ZiBkZWJ1Z19kaWN0KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICNUT0RPIElQ"
    "IGZvciBlbGV2YXRlZCBhdXRob3JpdHkKICAgICAgICAgICAgIyduYW1lJzpzZWxmLnVzZXIubmFt"
    "ZSwKICAgICAgICAgICAgJ2dhbWUnOnNlbGYudXNlci5nYW1lLmduYW1lIGlmIHNlbGYudXNlci5n"
    "YW1lIGVsc2UgJycsCiAgICAgICAgICAgICd0b3duJzpzZWxmLnVzZXIuZ2FtZWNoYW5uZWwubmFt"
    "ZSBpZiBzZWxmLnVzZXIuZ2FtZWNoYW5uZWwgZWxzZSAnJywKICAgICAgICAgICAgJ3Bvcyc6c2Vs"
    "Zi51c2VyLnBvc2RhdGEgaWYgc2VsZi51c2VyLnBvc2RhdGEgZWxzZSAnJywKICAgICAgICAgICAg"
    "J2lkJzpzZWxmLnVzZXIuaWRudW0sCiAgICAgICAgICAgICdsb2dpblRpbWUnOmpzb25UaW1lKHNl"
    "bGYudXNlci5sb2dpblRpbWUpCiAgICAgICAgfSNUT0RPIGVsZXZhdGVkIGF1dGhvcml0eSB2ZXJz"
    "aW9uCgpkZWYgY21kX2RlZmF1bHQoKTojYXJncyk6CiAgICAjcHJpbnQoYXJncykKICAgICNfcmVh"
    "ZGNvbmZpZygpCiAgICBzZXJ2ZXIgPSBDb3JlU2VydmVyKCkKICAgIHdpdGggc2VydmVyOgogICAg"
    "ICAgIHRzdCA9IHNpZ25hbC5zaWduYWwoc2lnbmFsLlNJR0lOVCwgc2VydmVyLmhhbmRsZV9zaWdu"
    "YWwodGltZW91dD0yKSkKICAgICAgICAjcHJpbnQoJ0Fzc2lnbmVkIFNpZ25hbD8nLCB0c3QpCiAg"
    "ICAgICAgI3NpZ25hbC5zaWduYWwoc2lnbmFsLlNJR1RFUk0sIHNlcnZlci5oYW5kbGVfc2lnbmFs"
    "KHRpbWVvdXQ9MSkpCiAgICAgICAgc2VydmVyLnNlcnZlX2ZvcmV2ZXIoKQoKI3NjcmlwdCBsYXVu"
    "Y2hlZCwgY2hlY2sgYXJndW1lbnRzIGFuZCBjb25maWcuIHNldHVwIHZhcmlvdXMgb2JqZWN0cwpp"
    "ZiBfX25hbWVfXyA9PSAnX19tYWluX18nOgogICAgcHJpbnQoJ0luaXRpYWxpemluZyBTZXJ2ZXIn"
    "KQogICAgY21kX2RlZmF1bHQoKQo="
)
_SOLO_SOURCE_B64 = (
    "aW1wb3J0IHNvY2tldHNlcnZlcgppbXBvcnQgc3RydWN0CmltcG9ydCB6bGliCmltcG9ydCByZQppbXBvcnQgb3MKCiNuZXh0IHRvIHRoaXMgc2NyaXB0LCBub3QgdGhlIHByb2Nlc3MnIGN1cnJlbnQgd29ya2luZyBkaXJlY3RvcnkgLS0gbWF0dGVycwojb25jZSB0aGlzIGlzIGxhdW5jaGVkIGZyb20gYSBHVUkgd3JhcHBlciBsaXZpbmcgaW4gYSBkaWZmZXJlbnQgZm9sZGVyLgojQW4gZW1iZWRkaW5nIGhvc3QgdGhhdCBleGVjKClzIHRoaXMgZmlsZSdzIHNvdXJjZSBmcm9tIG1lbW9yeSAod2hlcmUKI19fZmlsZV9fIGlzIG1lYW5pbmdsZXNzKSBjYW4gcmVkaXJlY3QgdGhpcyBieSBwcmUtc2V0dGluZyBfRVhURVJOQUxfREFUQV9ESVIKI2luIHRoZSBtb2R1bGUncyBnbG9iYWxzIGJlZm9yZSB0aGUgbW9kdWxlIGJvZHkgcnVucy4KaWYgJ19FWFRFUk5BTF9EQVRBX0RJUicgaW4gZ2xvYmFscygpIGFuZCBnbG9iYWxzKClbJ19FWFRFUk5BTF9EQVRBX0RJUiddOgogICAgX0RBVEFfUEFUSCA9IG9zLnBhdGguam9pbihnbG9iYWxzKClbJ19FWFRFUk5BTF9EQVRBX0RJUiddLCAnUGxheWVyZGF0YS5iaW4nKQplbHNlOgogICAgX0RBVEFfUEFUSCA9IG9zLnBhdGguam9pbihvcy5wYXRoLmRpcm5hbWUob3MucGF0aC5hYnNwYXRoKF9fZmlsZV9fKSksICdQbGF5ZXJkYXRhLmJpbicpCgpfMzJiaXQgPSAweEZGRkZGRkZGCl84Yml0ID0gMHhGRgojYm91bmRzIGZvciBsZW5ndGggdmFsdWVzIHJlYWQgb2ZmIHRoZSB3aXJlIChzZWUgX2hhbmRsZS9nZXREYXRhKQpfTUFYX1BBQ0tFVCA9IDY0ICogMTAyNApfTUFYX0JMT0IgPSAxNiAqIDEwMjQgKiAxMDI0Cl9HNjRfQkFTRSA9IGJ5dGVzKFsKICAgIDB4RDIsIDB4MTIsIDB4MTMsIDB4RDMsIDB4MTEsIDB4RDEsIDB4RDAsIDB4MTAsIDB4RjAsIDB4MzAsIAogICAgMHgzMSwgMHhGMSwgMHgzMywgMHhGMywgMHhGMiwgMHgzMiwgMHgzNiwgMHhGNiwgMHhGNywgMHgzNywgCiAgICAweEY1LCAweDM1LCAweDM0LCAweEY0LCAweDNDLCAweEZDLCAweEZELCAweDNELCAweEZGLCAweDNGLCAKICAgIDB4M0UsIDB4RkUsIDB4RkEsIDB4M0EsIDB4M0IsIDB4RkIsIDB4MzksIDB4RjksIDB4RjgsIDB4MzgsIAogICAgMHgyOCwgMHhFOCwgMHhFOSwgMHgyOSwgMHhFQiwgMHgyQiwgMHgyQSwgMHhFQSwgMHhFRSwgMHgyRSwgCiAgICAweDJGLCAweEVGLCAweDJELCAweEVELCAweEVDLCAweDJDLCAweEU0LCAweDI0LCAweDI1LCAweEU1LCAKICAgIDB4MjcsIDB4RTcsIDB4RTYsIDB4MjZdKQoKZGVmIF9zYXZlRGF0YShkYXRhKToKICAgIHdpdGggb3BlbihfREFUQV9QQVRILCAnd2InKSBhcyBmOgogICAgICAgIGYud3JpdGUoZGF0YSkKZGVmIF9sb2FkRGF0YSgpOgogICAgdHJ5OgogICAgICAgIHdpdGggb3BlbihfREFUQV9QQVRILCAncmInKSBhcyBmOgogICAgICAgICAgICByZXR1cm4gZi5yZWFkKCkKICAgIGV4Y2VwdDoKICAgICAgICByZXR1cm4gYicnCiAgICAKZGVmIG1ha2VEc3RyKHRleHQpOgogICAgdGV4dCA9IHRleHQuZW5jb2RlKCJhc2NpaSIpCiAgICB0ZXh0bGVuID0gbGVuKHRleHQpCiAgICByZXR1cm4gc3RydWN0LnBhY2soIjxJe31zIi5mb3JtYXQodGV4dGxlbiksIHRleHRsZW4sIHRleHQpCmRlZiBwYXJzZURzdHIoZGF0YSwgb2ZmKToKICAgIFtzdHJsZW5dID0gc3RydWN0LnVucGFjaygiPEkiLCBkYXRhW29mZjpvZmYrNF0pCiAgICBvZmYrPSA0ICsgc3RybGVuCiAgICB0ZXh0ID0gZGF0YVtvZmYtc3RybGVuOiBvZmZdLmRlY29kZSgpCiAgICByZXR1cm4gdGV4dCwgb2ZmCmRlZiBfc2VydmVyX2luZm9fcGFja2V0KCk6CiAgICBubSA9ICcrIkxvY2FsSG9zdCIiVFdNUDI7MTAuMC4wLjUiJwogICAgZGV0cyA9IHN0cnVjdC5wYWNrKCI8SSIsMCkgKyBtYWtlRHN0cihubSkKICAgIGNkZXRzID0gemxpYi5jb21wcmVzcyhkZXRzKQogICAgcmV0dXJuIHN0cnVjdC5wYWNrKCI8SSIsbGVuKGNkZXRzKSs0KSArIGNkZXRzCmRlZiBfc3RlcChudW0pOgogICAgcmV0dXJuIChudW0qMHgzNDNGRCArIDB4MjY5RUMzKSZfMzJiaXQKZGVmIGdlbjY0KGNvbWJpbmVkKToKICAgIG91dCA9IGJ5dGVhcnJheSgweDQwKQogICAgZWJwID0gZWRpID0gdG1wID0gMAogICAgZm9yIGIgaW4gY29tYmluZWQ6CiAgICAgICAgZWRpKz0gYit0bXAKICAgICAgICB0bXBePSBiCiAgICAgICAgZWJwKz0gdG1wCiAgICBmb3IgaSBpbiByYW5nZSgweDQwKToKICAgICAgICByZXMgPSBjb21iaW5lZFsoZWJwK2kpJThdCiAgICAgICAgb3V0W2ldID0gcmVzXl9HNjRfQkFTRVsoZWRpK2kpJTB4NDBdCiAgICByZyA9IGVkaStlYnAKICAgIGZvciBpIGluIHJhbmdlKDB4NDApOgogICAgICAgIHJnID0gX3N0ZXAocmcpCiAgICAgICAgb3V0W2ldXj0gKHJnPj4weDEwKSZfOGJpdAogICAgZm9yIGkgaW4gcmFuZ2UoMHgyMCk6CiAgICAgICAgcmcgPSBfc3RlcChyZykKICAgICAgICBzQSA9IChyZz4+MHgxMCklMHg0MAogICAgICAgIHJnID0gX3N0ZXAocmcpCiAgICAgICAgc0IgPSAocmc+PjB4MTApJTB4NDAKICAgICAgICAob3V0W3NBXSwgb3V0W3NCXSkgPSAob3V0W3NCXSwgb3V0W3NBXSkKICAgIHJldHVybiBieXRlcyhvdXQpCmRlZiBfaW5pdF9lcnJvcigpOgogICAgbXNnID0gJycKICAgIGVyciA9IHN0cnVjdC5wYWNrKCc8SScsMCkKICAgIGRldHMgPSBiJycuam9pbihbZXJyLCBtYWtlRHN0cihtc2cpXSkKICAgIGNkZXRzID0gemxpYi5jb21wcmVzcyhkZXRzKQogICAgcGFja2xlbiA9IHN0cnVjdC5wYWNrKCI8SSIsbGVuKGNkZXRzKSs0KQogICAgcmV0dXJuIHBhY2tsZW4rY2RldHMKCmRlZiBfc2VydmVyX3dlbGNvbWVfcGFja2V0KHNlcmlhbCk6CiAgICB0eHQxID0gJ1NvbG8gU2VydmVyJwogICAgdHh0MiA9ICc8MHhGRjAwMDBGRj48RjI+U29sbyBPZmZsaW5lIFNlcnZlcjxicmVhaz0xMC4wPlxyXG4nCiAgICB1bmtBID0gYnl0ZXMoWzAsMCwwLDAsIDB4NTUsIDB4YTYsIDB4ZDgsIDB4M2JdKQogICAgdW5rQiA9IGJ5dGVzKFswXSo0OSkKICAgIHVua0IrPSBnZW42NChzZXJpYWwpCiAgICBzZWVkID0gMAogICAgZ3JwID0gX2dycChzZWVkKQogICAgdW5rQis9IHN0cnVjdC5wYWNrKCc8NkknLDAsc2VlZCwqZ3JwKQogICAgZGV0cyA9IGInJy5qb2luKFt1bmtBLCBtYWtlRHN0cih0eHQxKSwgbWFrZURzdHIodHh0MiksIHVua0JdKQogICAgY2RldHMgPSB6bGliLmNvbXByZXNzKGRldHMpCiAgICBwYWNrbGVuID0gc3RydWN0LnBhY2soIjxJIixsZW4oY2RldHMpKzQpCiAgICByZXR1cm4gcGFja2xlbitjZGV0cwpkZWYgX2dycChzZWVkPTApOgogICAgcmV0dXJuICgxMTUzNzIxNjQ4LDQwOTE1MTk5NywxNTQzMzg3MDM1LDE4MTAzMDkzMTMpCmRlZiBfY2hubChuYW1lLCBpbmRleCk6CiAgICByZXR1cm4gZid7bmFtZX0jdHJhbnNsYXRle25hbWV9X0NoYW5uZWxfe2luZGV4OjAyZH0nCl9DSEFOTkVMU18gPSBbCiAgICAgICAgKF9jaG5sKCJOZXRfVF8wMSIsMSksIDAsMSwwLDApLAogICAgICAgIChfY2hubCgiTmV0X1RfMDIiLDEpLCAwLDEsMCwwKSwKICAgICAgICAoX2NobmwoIk5ldF9UXzAzIiwxKSwgMCwxLDAsMCksCiAgICAgICAgKF9jaG5sKCJOZXRfVF8wNCIsMSksIDAsMSwwLDApLAogICAgXQpkZWYgZW51bWVyYXRlQ2hhbm5lbERhdGEoKToKICAgIGNodW5rcyA9IFtdCiAgICBmb3IgKGNoYW5uZWxOYW1lLCBjdXJQbGF5ZXJzLCBtYXhQbGF5ZXJzLCBnQSwgZ0IpIGluIF9DSEFOTkVMU186CiAgICAgICAgY2h1bmtzLmFwcGVuZChmJyRnYW1lY2hhbm5lbCAie2NoYW5uZWxOYW1lfSIgIntjdXJQbGF5ZXJzfSIgInttYXhQbGF5ZXJzfSIgIntnQX0iICJ7Z0J9IicuZW5jb2RlKCJhc2NpaSIpKQogICAgcmV0dXJuIGIiXDAiLmpvaW4oY2h1bmtzKStiIlwwIgpkZWYgam9pbkNoYXRhbmRFbnVtZXJhdGUoKToKICAgIGNodW5rcyA9IFtdCiAgICBjaHVua3MuYXBwZW5kKGInL2pvaW5jaGF0Y2hhbm5lbCAidHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsIiAiIiAiMSInKQogICAgY2h1bmtzLmFwcGVuZChiJyRjaGF0Y2hhbm5lbCAidHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsIiAiIiAiMSInKQogICAgcmV0dXJuIGIiXDAiLmpvaW4oY2h1bmtzKStiIlwwIgoKX2dldFBEID0gcmUuY29tcGlsZShyJy9nZXRwbGF5ZXJkYXRhICIoLispIiAiVHdvV29ybGRzLjEuMCInKQpfc2V0UEQgPSByZS5jb21waWxlKHInL3NldHBsYXllcmRhdGEgIi4rIiAiVHdvV29ybGRzLjEuMCIgIihcZCspIiAiXGQrIiAiXGQrIicpCl9sZWF2ZUdhbWVDID0gcmUuY29tcGlsZShyJy9sZWF2ZWdhbWVjaGFubmVsICIxIicpCl9yZXFKb2luR2FtZUMgPSByZS5jb21waWxlKHInL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgIiguKykiJykKX2pvaW5HYW1lQyA9IHJlLmNvbXBpbGUocicvam9pbmdhbWVjaGFubmVsICIoLispIiAiKC4rKSInKQpfcmVxQ3JlYXRlR2FtZSA9IHJlLmNvbXBpbGUocicvcmVxdWVzdGNyZWF0ZWdhbWUgIiguKykiJykKX2dldEdSUCA9IHJlLmNvbXBpbGUocicvZ2V0Z3VpbGRyYW5rcG9pbnRzJykKX0dDVFUgPSByZS5jb21waWxlKHInL2dhbWVjb21tYW5kdG91c2VyICIoLispIiAiKC4rKSInKQoKY2xhc3MgQ29ubmVjdGlvbkhhbmRsZXIoc29ja2V0c2VydmVyLkJhc2VSZXF1ZXN0SGFuZGxlcik6CiAgICBkZWYgaGFuZGxlUGFja2V0KHNlbGYsIGNtZCk6CiAgICAgICAgI3ByaW50KGNtZCkKICAgICAgICBpZiBtIDo9IF9nZXRQRC5tYXRjaChjbWQpOgogICAgICAgICAgICBwbGF5ZXJkYXRhID0gX2xvYWREYXRhKCkKICAgICAgICAgICAgcGRsID0gbGVuKHBsYXllcmRhdGEpCiAgICAgICAgICAgIGlmIHBkbDoKICAgICAgICAgICAgICAgIHByaW50KGYnUGxheWVyZGF0YSBsb2FkZWQge3BkbH1ieXRlcycpCiAgICAgICAgICAgIHJlc2NtZCA9IGYnL2dldHBsYXllcmRhdGEgInttLmdyb3VwKDEpfSIgIlR3b1dvcmxkcy4xLjAiIHtwZGx9XDAnCiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxsKGInJy5qb2luKFtyZXNjbWQuZW5jb2RlKCdhc2NpaScpLCBwbGF5ZXJkYXRhXSkpCiAgICAgICAgZWxpZiBtIDo9IF9zZXRQRC5tYXRjaChjbWQpOgogICAgICAgICAgICBwZGwgPSBpbnQobS5ncm91cCgxKSkKICAgICAgICAgICAgcGxheWVyZGF0YSA9IHNlbGYuZ2V0RGF0YShwZGwpCiAgICAgICAgICAgIF9zYXZlRGF0YShwbGF5ZXJkYXRhKQogICAgICAgICAgICBwcmludChmJ1BsYXllcmRhdGEgc2F2ZWQge3BkbH1ieXRlcycpCiAgICAgICAgZWxpZiBtIDo9IF9HQ1RVLm1hdGNoKGNtZCk6CiAgICAgICAgICAgIGRsID0gaW50KG0uZ3JvdXAoMSkpCiAgICAgICAgICAgIGRhdGEgPSBzZWxmLmdldERhdGEoZGwpCiAgICAgICAgZWxpZiBtIDo9IF9sZWF2ZUdhbWVDLm1hdGNoKGNtZCk6CiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxsKGVudW1lcmF0ZUNoYW5uZWxEYXRhKCkpCiAgICAgICAgZWxpZiBtIDo9IF9yZXFKb2luR2FtZUMubWF0Y2goY21kKToKICAgICAgICAgICAgcmVzY21kID0gZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie20uZ3JvdXAoMSl9IiAiMSJcMCcKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNlbmRhbGwocmVzY21kLmVuY29kZSgnYXNjaWknKSkKICAgICAgICBlbGlmIG0gOj0gX2pvaW5HYW1lQy5tYXRjaChjbWQpOgogICAgICAgICAgICByZXNjbWQgPSBmJy9qb2luZ2FtZWNoYW5uZWwgInttLmdyb3VwKDEpfSIgIjEiXDAnCiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxsKHJlc2NtZC5lbmNvZGUoJ2FzY2lpJykpCiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxsKGpvaW5DaGF0YW5kRW51bWVyYXRlKCkpCiAgICAgICAgZWxpZiBtIDo9IF9yZXFDcmVhdGVHYW1lLm1hdGNoKGNtZCk6CiAgICAgICAgICAgIHJlc2NtZCA9IGYnL2NyZWF0ZWdhbWUgInttLmdyb3VwKDEpfSJcMCcKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNlbmRhbGwocmVzY21kLmVuY29kZSgnYXNjaWknKSkKICAgICAgICBlbGlmIG0gOj0gX2dldEdSUC5tYXRjaChjbWQpOgogICAgICAgICAgICAoYSxiLGMsZCkgPSBfZ3JwKCkKICAgICAgICAgICAgcmVzY21kID0gZicvZ2V0Z3VpbGRyYW5rcG9pbnRzICJ7YX0iICJ7Yn0iICJ7Y30iICJ7ZH0iXDAnLmVuY29kZSgnYXNjaWknKQogICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2VuZGFsbChyZXNjbWQpCiAgICAgICAgI2Vsc2U6ICNVTklNUExFTUVOVEVELCBwcm9iYWJseSBub3QgbmVlZGVkIGZvciBzb2xvLgogICAgICAgICAgICAjcHJpbnQoJ05PVCBJTVBMRU1FTlRFRDonLCBjbWQpCiAgICBkZWYgc2V0dXAoc2VsZik6CiAgICAgICAgc2VsZi5kYXRhID0gYicnCiAgICAgICAgc2VsZi5TSyA9IGJ5dGVhcnJheShzdHJ1Y3QucGFjaygnPElJJywgMHhBNkFFMUY5QiwgMHg0MzhERkY0MCkpCiAgICBkZWYgX3JlY3ZNb3JlKHNlbGYpOgogICAgICAgIGNodW5rID0gc2VsZi5yZXF1ZXN0LnJlY3YoMjA0OCkKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAgICAgICNwZWVyIGRpc2Nvbm5lY3RlZCAtIHdpdGhvdXQgdGhpcywgdGhlIHdhaXQgbG9vcHMgYmVsb3cgd291bGQgc3BpbgogICAgICAgICAgICAjZm9yZXZlciBvbiB0aGUgY2xvc2VkIHNvY2tldCAocmVjdiBrZWVwcyByZXR1cm5pbmcgYicnIGluc3RhbnRseSkKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoJ2Rpc2Nvbm5lY3RlZCcpCiAgICAgICAgc2VsZi5kYXRhICs9IGNodW5rCiAgICBkZWYgaGFuZGxlKHNlbGYpOgogICAgICAgIHRyeToKICAgICAgICAgICAgc2VsZi5faGFuZGxlKCkKICAgICAgICBleGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25uZWN0aW9uQWJvcnRlZEVycm9yLCBPU0Vycm9yKToKICAgICAgICAgICAgcGFzcyAjY2xpZW50IGRpc2Nvbm5lY3RlZCAoYWJvcnQvcmVzZXQgYm90aCBvY2N1ciBvbiBXaW5kb3dzKQogICAgICAgIGV4Y2VwdCAoemxpYi5lcnJvciwgc3RydWN0LmVycm9yLCBVbmljb2RlRGVjb2RlRXJyb3IsIFZhbHVlRXJyb3IpIGFzIGU6CiAgICAgICAgICAgIHByaW50KGYnTWFsZm9ybWVkIGRhdGEgZnJvbSBjbGllbnQ6IHt0eXBlKGUpLl9fbmFtZV9ffToge2V9JykKICAgIGRlZiBfaGFuZGxlKHNlbGYpOgogICAgICAgICNIYW5kc2hha2UgLSBBY2NlcHRBbnkKICAgICAgICBMSVMgPSAyCiAgICAgICAgd2hpbGUgTElTOgogICAgICAgICAgICB3aGlsZShsZW4oc2VsZi5kYXRhKTw0KToKICAgICAgICAgICAgICAgIHNlbGYuX3JlY3ZNb3JlKCkKICAgICAgICAgICAgcGFja19sZW4gPSBzdHJ1Y3QudW5wYWNrKCI8SSIsc2VsZi5kYXRhWzA6NF0pWzBdCiAgICAgICAgICAgIGlmIHBhY2tfbGVuIDwgNCBvciBwYWNrX2xlbiA+IF9NQVhfUEFDS0VUOgogICAgICAgICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcihmJ2hhbmRzaGFrZSBwYWNrZXQgbGVuZ3RoIHtwYWNrX2xlbn0gb3V0IG9mIHJhbmdlJykKICAgICAgICAgICAgd2hpbGUobGVuKHNlbGYuZGF0YSk8cGFja19sZW4pOgogICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1vcmUoKQogICAgICAgICAgICAjc2xpY2UgdG8gcGFja19sZW46IGJ5dGVzIHBhc3QgaXQgYmVsb25nIHRvIHRoZSBuZXh0IHBhY2tldAogICAgICAgICAgICByZXMgPSB6bGliLmRlY29tcHJlc3Moc2VsZi5kYXRhWzQ6cGFja19sZW5dKQogICAgICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFbcGFja19sZW46XQogICAgICAgICAgICBpZiBMSVMgPT0gMjoKICAgICAgICAgICAgICAgICNza2lwIDE2CiAgICAgICAgICAgICAgICBsYW5nbmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywgMTYpCiAgICAgICAgICAgICAgICBSSyA9IHJlc1tvZmYrODpvZmYrMTZdCiAgICAgICAgICAgICAgICBmb3IgaSBpbiByYW5nZShsZW4oUkspKToKICAgICAgICAgICAgICAgICAgICBzZWxmLlNLW2ldXj1SS1tpXQogICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNlbmRhbGwoX3NlcnZlcl9pbmZvX3BhY2tldCgpKQogICAgICAgICAgICAgICAgTElTID0gMQogICAgICAgICAgICBlbGlmIExJUyA9PSAxOgogICAgICAgICAgICAgICAgdXNlcm5hbWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDApCiAgICAgICAgICAgICAgICBwYXNzd29yZCwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAgICAgICAgaWYgdXNlcm5hbWUgYW5kIHBhc3N3b3JkOgogICAgICAgICAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxsKF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQoYnl0ZXMoc2VsZi5TSykpKQogICAgICAgICAgICAgICAgICAgIExJUyA9IDAKICAgICAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICAgICAgI2dhbWUgc2VuZHMgZW1wdHkgVXNlci9QYXNzIHdoZW4gbm9uZSBhcmUgc3RvcmVkLgogICAgICAgICAgICAgICAgICAgICNvbmx5IHNob3dzIGxvZ2luL3JlZ2lzdGVyIHByb21wdCBhZnRlciBlcnJvciBpcyByZXR1cm5lZC4KICAgICAgICAgICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2VuZGFsbChfaW5pdF9lcnJvcigpKQogICAgICAgICNMT0dJTiBTVUNDRVNTCiAgICAgICAgcHJpbnQoJ1BsYXllciBDb25uZWN0ZWQnKQogICAgICAgIHRyeToKICAgICAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAgICAgICAgICAgIGlmIG5vdCBzZWxmLmRhdGE6CiAgICAgICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1vcmUoKSAjYmxvY2tzIGZvciBtb3JlIGRhdGEsIHJhaXNlcyBvbiBkaXNjb25uZWN0CiAgICAgICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgICAgICNjaGVjayBmb3IgbG9vc2UgZGF0YSBhbmQgZGlzY2FyZAogICAgICAgICAgICAgICAgaWYgKGxlbihzZWxmLmRhdGEpPjIgYW5kCiAgICAgICAgICAgICAgICAgICAgICAgIHNlbGYuZGF0YVswXT09MHg3OCBhbmQKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5kYXRhWzFdPT0weDljKToKICAgICAgICAgICAgICAgICAgICBkY21wID0gemxpYi5kZWNvbXByZXNzb2JqKCkKICAgICAgICAgICAgICAgICAgICBkY21wLmRlY29tcHJlc3Moc2VsZi5kYXRhKQogICAgICAgICAgICAgICAgICAgIHdoaWxlIG5vdCBkY21wLmVvZjoKICAgICAgICAgICAgICAgICAgICAgICAgY2RhdCA9IHNlbGYucmVxdWVzdC5yZWN2KDIwNDgpCiAgICAgICAgICAgICAgICAgICAgICAgIGlmIG5vdCBjZGF0OgogICAgICAgICAgICAgICAgICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoJ2Rpc2Nvbm5lY3RlZCcpCiAgICAgICAgICAgICAgICAgICAgICAgIGRjbXAuZGVjb21wcmVzcyhjZGF0KQogICAgICAgICAgICAgICAgICAgIHNlbGYuZGF0YSA9IGRjbXAudW51c2VkX2RhdGEKICAgICAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICAgICAgI3BhcnNlIGNvbW1hbmRzCiAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAgICAgY21kX2wgPSBzZWxmLmRhdGEuaW5kZXgoMCkKICAgICAgICAgICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICAgICAgICAgICNjb21tYW5kIHNwbGl0IGFjcm9zcyBwYWNrZXRzLCB3YWl0IGZvciB0aGUgcmVzdCBpbnN0ZWFkCiAgICAgICAgICAgICAgICAgICAgI29mIHNwaW5uaW5nIGhlcmUgZm9yZXZlciAocHJldmlvdXMgYmVoYXZpb3IgbmV2ZXIgY2FsbGVkCiAgICAgICAgICAgICAgICAgICAgI3JlY3YgYWdhaW4sIGhhbmdpbmcgdGhlIHRocmVhZCBhdCAxMDAlIENQVSkKICAgICAgICAgICAgICAgICAgICBzZWxmLl9yZWN2TW9yZSgpCiAgICAgICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgICAgIGNtZCA9IHNlbGYuZGF0YVswOmNtZF9sXS5kZWNvZGUoKQogICAgICAgICAgICAgICAgc2VsZi5kYXRhID0gc2VsZi5kYXRhW2NtZF9sKzE6XQogICAgICAgICAgICAgICAgaWYgY21kOgogICAgICAgICAgICAgICAgICAgIHNlbGYuaGFuZGxlUGFja2V0KGNtZCkKICAgICAgICBleGNlcHQgQ29ubmVjdGlvblJlc2V0RXJyb3I6CiAgICAgICAgICAgIHBhc3MgI2NsaWVudCBkaXNjb25uZWN0ZWQKICAgIGRlZiBmaW5pc2goc2VsZik6CiAgICAgICAgcHJpbnQoJ1BsYXllciBkaXNjb25uZWN0ZWQnKQogICAgZGVmIGdldERhdGEoc2VsZiwgbG4pOgogICAgICAgICNsZW5ndGggY29tZXMgb2ZmIHRoZSB3aXJlOyBib3VuZCBpdCBzbyBhIGJhZCB2YWx1ZSBjYW4ndCBtYWtlIHVzCiAgICAgICAgI2J1ZmZlciB1bnRpbCB3ZSBydW4gb3V0IG9mIG1lbW9yeQogICAgICAgIGlmIGxuIDwgMCBvciBsbiA+IF9NQVhfQkxPQjoKICAgICAgICAgICAgcmFpc2UgVmFsdWVFcnJvcihmJ2Jsb2Igc2l6ZSB7bG59IG91dCBvZiByYW5nZScpCiAgICAgICAgd2hpbGUgbGVuKHNlbGYuZGF0YSk8bG46CiAgICAgICAgICAgIHNlbGYuX3JlY3ZNb3JlKCkKICAgICAgICBkYXQgPSBzZWxmLmRhdGFbMDpsbl0KICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFbbG46XQogICAgICAgIHJldHVybiBkYXQKCmlmIF9fbmFtZV9fID09ICJfX21haW5fXyI6CiAgICBIT1NULCBQT1JUID0gImxvY2FsaG9zdCIsIDE3MTcxCgogICAgd2l0aCBzb2NrZXRzZXJ2ZXIuVENQU2VydmVyKChIT1NULCBQT1JUKSwgQ29ubmVjdGlvbkhhbmRsZXIpIGFzIHNlcnZlcjoKICAgICAgICBwcmludCgnQ29ubmVjdCB0byAxMjcuMC4wLjEnKQogICAgICAgIHByaW50KCdDbG9zZSB0aGlzIHdpbmRvdyB0byBjbG9zZSB0aGUgc2VydmVyJykKICAgICAgICBzZXJ2ZXIuc2VydmVfZm9yZXZlcigpCg=="
)
_ACTIVATION_SOURCE_B64 = (
    "aW1wb3J0IHNvY2tldHNlcnZlcgppbXBvcnQgaHR0cC5zZXJ2ZXIKaW1wb3J0IHdpbnJlZwppbXBvcnQgY3R5cGVzCmltcG9ydCBzdHJ1Y3QKaW1wb3J0IHN5cwppbXBvcnQgb3MKZnJvbSB0aW1lIGltcG9ydCBzbGVlcApmcm9tIHVybGxpYi5wYXJzZSBpbXBvcnQgdW5xdW90ZSwgdXJscGFyc2UKCkhPU1QgPSAnbG9jYWxob3N0JwpQT1JUID0gODAKI05PVEU6IGdhbWUgaGFyZCBjb2RlcyBwb3J0IG51bWJlcnMgZm9yIGFjdGl2YXRpb24gdXJsLCBodHRwPTgwLCBodHRwcz00NDMuCgpSRUdQQVRIID0gcidTT0ZUV0FSRVxSZWFsaXR5IFB1bXBcVHdvV29ybGRzXFNlcmlhbEtleScKUkVHS19QRVJNUyA9IHdpbnJlZy5LRVlfV09XNjRfMzJLRVl8d2lucmVnLktFWV9SRUFEfHdpbnJlZy5LRVlfV1JJVEUKUkVHS19BQ1RTRVJWID0gJ0FjdGl2YXRpb25TZXJ2ZXInClJFR0tfU0VSSUFMID0gJ1NlcmlhbEtleScKUkVHS19SRUdJU1QgPSAnUmVnaXN0ZXJlZCcKClNFUlZfQUNUUEFUSCA9ICcvVHdvV29ybGRzL0FjdGl2YXRpb25Gcm9udGVuZEdhbWUucGhwJwpSRUdWX05QQVRIID0gJ2h0dHA6Ly8xMjcuMC4wLjEnK1NFUlZfQUNUUEFUSAoKV0FSTl9BQ1RTRVJWPXJmJycnVW5hYmxlIHRvIHNldCBzZXJ2ZXIgcGF0aCBpbiByZWdpc3RyeS4gRW5zdXJlIHJlZ2lzdHJ5IGlzIHNldCBjb3JyZWN0bHk6CjY0Yml0OiBDb21wdXRlclxIS0VZX0xPQ0FMX01BQ0hJTkVcU09GVFdBUkVcV09XNjQzMk5vZGVcUmVhbGl0eSBQdW1wXFR3b1dvcmxkc1xTZXJpYWxLZXkKMzJiaXQ6IENvbXB1dGVyXEhLRVlfTE9DQUxfTUFDSElORVxTT0ZUV0FSRVxSZWFsaXR5IFB1bXBcVHdvV29ybGRzXFNlcmlhbEtleQpzdWJrZXk6IEFjdGl2YXRpb25TZXJ2ZXIKdmFsdWU6IHtSRUdWX05QQVRIfScnJwpXQVJOX1JFR1NFUj1yJ1VuYWJsZSB0byByZWFkIHNlcmlhbCBrZXkgZnJvbSByZWdpc3RyeSwgaW5wdXQgbWFudWFsbHkgaW4gWFhYWC1YWFhYLVhYWFgtWFhYWCBmb3JtYXQuJwpQUk1UX1NFUklBTD0nU2VyaWFsIGtleTogJwpFUlJfU0VSSUFMPSdTZXJpYWwga2V5IGludmFsaWQsIGVuc3VyZSBpbnB1dCBpcyBpbiBjb3JyZWN0IGZvcm1hdDogWFhYWC1YWFhYLVhYWFgtWFhYWCcKI1RPRE8gYmV0dGVyIGVycm9yIG1lc3NhZ2VzCgpOT1RFX0FDVFNFUlY9J0FjdGl2YXRpb24gc2VydmVyIHNldCB0byBsb2NhbGhvc3QuJwpOT1RFX1JFR1NFUj0nU2VyaWFsIHN1Y2Nlc3NmdWxseSByZWFkIGZyb20gcmVnaXN0cnkuJwpOT1RFX1dBSVRJTkc9J0F3YWl0aW5nIGNvbm5lY3Rpb24gZnJvbSBnYW1lLi4uJwpOT1RFX0RPTkU9J0FjdGl2YXRpb24gY29kZSBzZW50IHRvIHRoZSBnYW1lLicKTk9URV9SRVNUT1JFRD0nUmVnaXN0cnkgcmVzdG9yZWQgdG8gaXRzIG9yaWdpbmFsIHN0YXRlLicKRVJSX1BPUlQ9KCdDb3VsZCBub3QgbGlzdGVuIG9uIHBvcnQge3BvcnR9OiB7ZXJyfVxuJwogICAgICAgICAgJ1RoZSBnYW1lIGhhcmQtY29kZXMgdGhpcyBwb3J0LCBzbyBpdCBoYXMgdG8gYmUgZnJlZS4gU29tZXRoaW5nIGVsc2UgJwogICAgICAgICAgJ2lzIHVzaW5nIGl0IChJSVMsIGFub3RoZXIgd2ViIHNlcnZlcikgb3IgYWRtaW5pc3RyYXRvciByaWdodHMgYXJlICcKICAgICAgICAgICdtaXNzaW5nLicpCgojQ29tcHV0ZXJcSEtFWV9MT0NBTF9NQUNISU5FXFNPRlRXQVJFXFdPVzY0MzJOb2RlXFJlYWxpdHkgUHVtcFxUd29Xb3JsZHNcU2VyaWFsS2V5CiMgOiBBY3RpdmF0aW9uU2VydmVyIDwtPiBzZXQgYW5kIHJlc2V0IHRocm91Z2ggcHJvY2VzcwojICAgT1JJR0lOQUwgQUNUSVZBVElPTiBVUkw6IGh0dHBzOi8vc2VjdXJlLnp1eHhlei5jb20vVHdvV29ybGRzL0FjdGl2YXRpb25Gcm9udGVuZEdhbWUucGhwCiNDb21wdXRlclxIS0VZX0NVUlJFTlRfVVNFUlxTT0ZUV0FSRVxSZWFsaXR5IFB1bXBcVHdvV29ybGRzXFNlcmlhbEtleQojIDogU2VyaWFsS2V5IC0+IGdldCBhbmQgdXNlCgpfMzJiaXQgPSAweEZGRkZGRkZGCl84Yml0ID0gMHhGRgpfc2lnbiA9IDB4ODAwMDAwMDAKX3VfYXIgPSBbMHhDMUFDMDQ0OCwgMHgyMDgwQzI0MF0KX2NfYXIgPSBbMHgwMDAwLCAweEMwQzEsIDB4QzE4MSwgMHgwMTQwLCAweEMzMDEsIDB4MDNDMCwgMHgwMjgwLCAweEMyNDEsIDB4QzYwMSwgMHgwNkMwLCAweDA3ODAsIDB4Qzc0MSwgMHgwNTAwLCAweEM1QzEsIDB4QzQ4MSwKICAgICAgICAgMHgwNDQwLCAweENDMDEsIDB4MENDMCwgMHgwRDgwLCAweENENDEsIDB4MEYwMCwgMHhDRkMxLCAweENFODEsIDB4MEU0MCwgMHgwQTAwLCAweENBQzEsIDB4Q0I4MSwgMHgwQjQwLCAweEM5MDEsIDB4MDlDMCwKICAgICAgICAgMHgwODgwLCAweEM4NDEsIDB4RDgwMSwgMHgxOEMwLCAweDE5ODAsIDB4RDk0MSwgMHgxQjAwLCAweERCQzEsIDB4REE4MSwgMHgxQTQwLCAweDFFMDAsIDB4REVDMSwgMHhERjgxLCAweDFGNDAsIDB4REQwMSwKICAgICAgICAgMHgxREMwLCAweDFDODAsIDB4REM0MSwgMHgxNDAwLCAweEQ0QzEsIDB4RDU4MSwgMHgxNTQwLCAweEQ3MDEsIDB4MTdDMCwgMHgxNjgwLCAweEQ2NDEsIDB4RDIwMSwgMHgxMkMwLCAweDEzODAsIDB4RDM0MSwKICAgICAgICAgMHgxMTAwLCAweEQxQzEsIDB4RDA4MSwgMHgxMDQwLCAweEYwMDEsIDB4MzBDMCwgMHgzMTgwLCAweEYxNDEsIDB4MzMwMCwgMHhGM0MxLCAweEYyODEsIDB4MzI0MCwgMHgzNjAwLCAweEY2QzEsIDB4Rjc4MSwKICAgICAgICAgMHgzNzQwLCAweEY1MDEsIDB4MzVDMCwgMHgzNDgwLCAweEY0NDEsIDB4M0MwMCwgMHhGQ0MxLCAweEZEODEsIDB4M0Q0MCwgMHhGRjAxLCAweDNGQzAsIDB4M0U4MCwgMHhGRTQxLCAweEZBMDEsIDB4M0FDMCwKICAgICAgICAgMHgzQjgwLCAweEZCNDEsIDB4MzkwMCwgMHhGOUMxLCAweEY4ODEsIDB4Mzg0MCwgMHgyODAwLCAweEU4QzEsIDB4RTk4MSwgMHgyOTQwLCAweEVCMDEsIDB4MkJDMCwgMHgyQTgwLCAweEVBNDEsIDB4RUUwMSwKICAgICAgICAgMHgyRUMwLCAweDJGODAsIDB4RUY0MSwgMHgyRDAwLCAweEVEQzEsIDB4RUM4MSwgMHgyQzQwLCAweEU0MDEsIDB4MjRDMCwgMHgyNTgwLCAweEU1NDEsIDB4MjcwMCwgMHhFN0MxLCAweEU2ODEsIDB4MjY0MCwKICAgICAgICAgMHgyMjAwLCAweEUyQzEsIDB4RTM4MSwgMHgyMzQwLCAweEUxMDEsIDB4MjFDMCwgMHgyMDgwLCAweEUwNDEsIDB4QTAwMSwgMHg2MEMwLCAweDYxODAsIDB4QTE0MSwgMHg2MzAwLCAweEEzQzEsIDB4QTI4MSwKICAgICAgICAgMHg2MjQwLCAweDY2MDAsIDB4QTZDMSwgMHhBNzgxLCAweDY3NDAsIDB4QTUwMSwgMHg2NUMwLCAweDY0ODAsIDB4QTQ0MSwgMHg2QzAwLCAweEFDQzEsIDB4QUQ4MSwgMHg2RDQwLCAweEFGMDEsIDB4NkZDMCwKICAgICAgICAgMHg2RTgwLCAweEFFNDEsIDB4QUEwMSwgMHg2QUMwLCAweDZCODAsIDB4QUI0MSwgMHg2OTAwLCAweEE5QzEsIDB4QTg4MSwgMHg2ODQwLCAweDc4MDAsIDB4QjhDMSwgMHhCOTgxLCAweDc5NDAsIDB4QkIwMSwKICAgICAgICAgMHg3QkMwLCAweDdBODAsIDB4QkE0MSwgMHhCRTAxLCAweDdFQzAsIDB4N0Y4MCwgMHhCRjQxLCAweDdEMDAsIDB4QkRDMSwgMHhCQzgxLCAweDdDNDAsIDB4QjQwMSwgMHg3NEMwLCAweDc1ODAsIDB4QjU0MSwKICAgICAgICAgMHg3NzAwLCAweEI3QzEsIDB4QjY4MSwgMHg3NjQwLCAweDcyMDAsIDB4QjJDMSwgMHhCMzgxLCAweDczNDAsIDB4QjEwMSwgMHg3MUMwLCAweDcwODAsIDB4QjA0MSwgMHg1MDAwLCAweDkwQzEsIDB4OTE4MSwKICAgICAgICAgMHg1MTQwLCAweDkzMDEsIDB4NTNDMCwgMHg1MjgwLCAweDkyNDEsIDB4OTYwMSwgMHg1NkMwLCAweDU3ODAsIDB4OTc0MSwgMHg1NTAwLCAweDk1QzEsIDB4OTQ4MSwgMHg1NDQwLCAweDlDMDEsIDB4NUNDMCwKICAgICAgICAgMHg1RDgwLCAweDlENDEsIDB4NUYwMCwgMHg5RkMxLCAweDlFODEsIDB4NUU0MCwgMHg1QTAwLCAweDlBQzEsIDB4OUI4MSwgMHg1QjQwLCAweDk5MDEsIDB4NTlDMCwgMHg1ODgwLCAweDk4NDEsIDB4ODgwMSwKICAgICAgICAgMHg0OEMwLCAweDQ5ODAsIDB4ODk0MSwgMHg0QjAwLCAweDhCQzEsIDB4OEE4MSwgMHg0QTQwLCAweDRFMDAsIDB4OEVDMSwgMHg4RjgxLCAweDRGNDAsIDB4OEQwMSwgMHg0REMwLCAweDRDODAsIDB4OEM0MSwKICAgICAgICAgMHg0NDAwLCAweDg0QzEsIDB4ODU4MSwgMHg0NTQwLCAweDg3MDEsIDB4NDdDMCwgMHg0NjgwLCAweDg2NDEsIDB4ODIwMSwgMHg0MkMwLCAweDQzODAsIDB4ODM0MSwgMHg0MTAwLCAweDgxQzEsIDB4ODA4MSwKICAgICAgICAgMHg0MDQwXQpfU0xFVCA9IGIiMjM0NTY3ODlBQkNERUZHSEpLTE1OUFFSU1RVV1ZYWVoiICNWYWxpZCBsZXR0ZXJzCmRlZiBfa3ByKGtleSk6CiAgICBrZXlpbnRzID0gbGlzdCggc3RydWN0LnVucGFjaygiPDZJIixrZXkpICkKICAgIGtleWludHNbMF1ePSAweEZGRkZFRENCICMgLTB4MTIzNAogICAga2V5aW50c1sxXV49IDB4MTIzNAogICAga2V5aW50c1syXV49IDB4RkZGRmE5ODcgIyAtMHg1Njc4CiAgICBrZXlpbnRzWzNdXj0gMHg1Njc4CiAgICBrZXlpbnRzWzRdXj0gMHhGRkZGRURDQgogICAga2V5aW50c1s1XV49IDB4NTY3OAogICAgcmV0dXJuIHN0cnVjdC5wYWNrKCI8NkkiLCprZXlpbnRzKQpkZWYgX21rZXkoa2V5c3RyKToKICAgIHRtcCA9IGtleXN0ci5lbmNvZGUoJ2FzY2lpJykrYidceDAweFZceDAwXHgwMCcKICAgIHJldHVybiBfa3ByKHRtcCkKZGVmIGtleXByb2Nlc3Moa2V5KToKICAgIG5rZXkgPSBfa3ByKGtleSkKICAgIGwgPSAwCiAgICBmb3IgYyBpbiBua2V5OgogICAgICAgIGlmIG5vdCBjOgogICAgICAgICAgICBicmVhawogICAgICAgIGwrPTEKICAgIG5rZXkgPSBua2V5WzpsXQogICAgcmV0dXJuIG5rZXkKZGVmIF9rc3ViQShlYXgpOgogICAgaWYgZWF4ICYgX3NpZ246CiAgICAgICAgICAgIGVheCs9IDcKICAgIHRzID0gZWF4ICYgX3NpZ24KICAgIGVheCA9IChlYXg+PjMpfHRzCiAgICByZXR1cm4gZWF4CmRlZiB2ZXJrZXkoa2V5c3RyKToKICAgIGFyZzQgPSBbMF0qMTAKICAgIHZhcjggPSAwCiAgICBlZGkgPSAwCiAgICBmb3IgYyBpbiBrZXlzdHI6CiAgICAgICAgaWYgYyA9PSA0NTogI2InLScKICAgICAgICAgICAgaWYgZWRpJTQ6CiAgICAgICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICAgICAgY29udGludWUKICAgICAgICBlYnggPSBfU0xFVC5maW5kKGMpCiAgICAgICAgaWYgZWJ4ID09IC0xOgogICAgICAgICAgICByZXR1cm4gRmFsc2UKICAgICAgICBlc2kgPSBlZGkgJSA4CiAgICAgICAgZWF4ID1fa3N1YkEoZWRpKQogICAgICAgIGFsID0gZWJ4IDw8IGVzaQogICAgICAgIGFyZzRbZWF4XXw9IGFsICYgMHhGRgogICAgICAgIGlmIGVzaSA+IDM6CiAgICAgICAgICAgIGVheCA9IHZhcjgKICAgICAgICAgICAgZWF4Kz0gNQogICAgICAgICAgICBlYXggPV9rc3ViQShlYXgpCiAgICAgICAgICAgIGVjeCA9IDgtZXNpCiAgICAgICAgICAgIHRzID0gZWJ4ICYgX3NpZ24KICAgICAgICAgICAgY2wgPSBlY3ggJiAweEZGCiAgICAgICAgICAgIGVieCA9IChlYng+PmNsKXx0cwogICAgICAgICAgICBhcmc0W2VheF18PSBlYnggJiAweEZGCiAgICAgICAgdmFyOCs9NQogICAgICAgIGVkaSA9IHZhcjgKICAgIHJldHVybiBhcmc0CmRlZiBfYXNobDEoaSk6CiAgICB0bXAgPSAoMTw8aSkKICAgIGEgPSB0bXAgJiBfMzJiaXQKICAgIGIgPSAodG1wPj4zMikgJiBfMzJiaXQKICAgIHJldHVybiBhLGIKZGVmIHZlcnJlcyhrZXlhcik6CiAgICB2YXI4ID0gMCAjIFkKICAgIGVkaSA9IDAgIyBaCiAgICB2YXJDID0gMHgwRiAjIFgKICAgIFNDX0EgPSAwCiAgICBTQ19CID0gMAogICAgZm9yIHZhcjEwIGluIHJhbmdlKDB4NDApOgogICAgICAgIGVheCwgZWR4ID0gX2FzaGwxKHZhcjEwKQogICAgICAgIGVheCY9IF91X2FyWzBdCiAgICAgICAgZWR4Jj0gX3VfYXJbMV0KICAgICAgICBlY3ggPSB2YXIxMCAmIChfc2lnbnwweDcpCiAgICAgICAgY2wgPSBlY3ggJiAweEZGCiAgICAgICAgZGx0ID0gKDEgPDwgY2wpICYgMHhGRgogICAgICAgIGFsID0ga2V5YXJbIF9rc3ViQSh2YXIxMCkgXQogICAgICAgIGlmIGVheCA9PSAwIGFuZCBlZHggPT0gMDoKICAgICAgICAgICAgaWYgKGRsdCAmIGFsKToKICAgICAgICAgICAgICAgIGVheCwgZWR4ID0gX2FzaGwxKHZhcjEwKQogICAgICAgICAgICAgICAgU0NfQXw9IGVheAogICAgICAgICAgICAgICAgU0NfQnw9IGVkeAogICAgICAgIGVsc2U6CiAgICAgICAgICAgIGlmIChkbHQgJiBhbCk6CiAgICAgICAgICAgICAgICBjbCA9IHZhckMgJiAweEZGCiAgICAgICAgICAgICAgICB2YXI4fD0gKDEgPDwgY2wpCiAgICAgICAgICAgIAogICAgICAgICAgICBlY3ggPSBlZGkgJiAoX3NpZ258MHg3KQogICAgICAgICAgICBjbCA9IGVjeCAmIDB4RkYKICAgICAgICAgICAgY2wgPSAoMSA8PCBjbCkgJiAweEZGCiAgICAgICAgICAgIGVheCA9IF9rc3ViQSggZWRpKzB4NDAgKQogICAgICAgICAgICBhbCA9IGtleWFyW2VheF0gJiAweEZGCiAgICAgICAgICAgIGlmIChhbCAmIGNsKToKICAgICAgICAgICAgICAgIGVheCwgZWR4ID0gX2FzaGwxKHZhcjEwKQogICAgICAgICAgICAgICAgU0NfQXw9IGVheAogICAgICAgICAgICAgICAgU0NfQnw9IGVkeAogICAgICAgICAgICBlZGkrPSAxCiAgICAgICAgICAgIHZhckMtPSAxCiAgICBjaGVja2JpbiA9IHN0cnVjdC5wYWNrKCI8MkkiLCBTQ19BLCBTQ19CKQogICAgZWN4ID0gY2hlY2tiaW5bMF0KICAgIGVjeCA9IF9jX2FyW2VjeF0KICAgIGVkeCA9IDAKICAgIGZvciBpIGluIHJhbmdlKDEsOCk6CiAgICAgICAgZWR4ID0gKGNoZWNrYmluW2ldIF4gZWN4KSAmIDB4RkYKICAgICAgICBlY3ggPSAoZWN4ID4+IDgpIF4gX2NfYXJbZWR4XQogICAgdHIgPSAodmFyOCAtIGVjeCkgJiAweEZGRkYKICAgIGlmKHRyKToKICAgICAgICByZXR1cm4gRmFsc2UsIDAsIDAKICAgIGVsc2U6CiAgICAgICAgcmV0dXJuIFRydWUsIFNDX0EsIFNDX0IKZGVmIF9nZXRTQ19CKGtleXN0cik6CiAgICByZXN1bHQgPSB2ZXJrZXkoa2V5c3RyKQogICAgaWYgbm90IHJlc3VsdDoKICAgICAgICByZXR1cm4gRmFsc2UsMAogICAgKGEsIGIsIGMpID0gdmVycmVzKHJlc3VsdCkKICAgIHJldHVybiBhLGMKCl9Yb3JWYWx1ZXNfID0gKAogICAgMHgzNmVmNjRiYSwgMHg0MzM5MDlkNCwgMHg1ZGUzZWE2ZiwgMHg0MzhkZmY0MCwKICAgIDB4MDM3NTk0MWMsIDB4NGJhMmY5NDMsIDB4MTBkZjY2OWMsIDB4MGM5NWVkZmUsCiAgICAweDA3MDdhNTc3LCAweDNlZWNkMDk4LCAweDJkZDE2MWJmLCAweDQ3ZTAyYzc3LAogICAgMHgxNmFlMWY5YiwgMHg3NDdlM2ZmOCwgMHg2Y2IzODUyNCwgMHgwMjBjNWZkNSkKZGVmIF9yZXRLZXlJbmR4KHYzKToKICAgIHJldHVybiAodjNeKCh2M14oKHYzXigodjNeKCh2M14oKHYzXigodjNeKHYzPj4zKSk+PjQpKT4+NCkpPj40KSk+PjQpKT4+NCkpPj40KSkmMHhGOwpkZWYgX3JldHJpZXZlVmFscyhrLGEsYik6CiAgICB4aSA9IF9Yb3JWYWx1ZXNfW19yZXRLZXlJbmR4KGspXQogICAgcmV0dXJuIChhXnhpLCBiXnhpKQpkZWYgX2dlbktleUluZHgodik6CiAgICByZXR1cm4gKHZeKCh2Xih2Pj40KV4oKHZeKHY+PjQpXigodl4oKHZeKHY+PjMpKT4+NCkpPj44KSk+PjgpKT4+NCkpJjB4RgpkZWYgX3N1Yl83Yjg2MDAoYSxiLGMpOgogICAgdG1wID0gYV5iXmNeX1hvclZhbHVlc19bX2dlbktleUluZHgoYSldXl9Yb3JWYWx1ZXNfW19nZW5LZXlJbmR4KGIpXQogICAgcmV0dXJuIHRtcF5fWG9yVmFsdWVzX1tfZ2VuS2V5SW5keCh0bXApXQpkZWYgZ2VuQWN0U3RyKFNDX0EsIFNDX0IsIEhXREVUKToKICAgIHY2ID0gX3N1Yl83Yjg2MDAoU0NfQSwgSFdERVQsIFNDX0IpCiAgICB2NyA9IF9zdWJfN2I4NjAwKFNDX0IsIFNDX0EsIEhXREVUKQogICAgdjggPSBfc3ViXzdiODYwMCh2NiwgdjcsIEhXREVUKQogICAgcmV0dXJuIGYne3Y4OjAxMGR9e3Y2JjB4N0ZGRkY6MDZkfScKU0NfQiA9IDAKZGVmIGNhbGNBY3RpdmF0aW9uQ29kZShxcHJvcHMpOgogICAgdG1wID0gJycuam9pbigocXByb3BzWydzZXJpYWxfcGFydF9hJ10sCiAgICAgICAgICAgICAgICAgIHFwcm9wc1snc2VyaWFsX3BhcnRfYiddLAogICAgICAgICAgICAgICAgICBxcHJvcHNbJ3NlcmlhbF9wYXJ0X2MnXSwKICAgICAgICAgICAgICAgICAgcXByb3BzWydzZXJpYWxfcGFydF9kJ10pKQogICAgVFZLID0gaW50KHRtcFswOjNdLCBiYXNlPTEwKSAjIEtleSB0byBkZWNvZGluZyB2YWx1ZXMKICAgIFRWQSA9IGludCh0bXBbMzoxM10sIGJhc2U9MTApICMgU2VyaWFsIFBhcnQgQQogICAgVFZIVyA9IGludCh0bXBbMTM6MTZdLCBiYXNlPTEwKSAjIEhhcmR3YXJlIHNwZWNpZmljIHZhbHVlCiAgICAoU0NfQSwgSFdERVQpID0gX3JldHJpZXZlVmFscyhUVkssIFRWQSwgVFZIVykKICAgICNTQ19CIFJlcXVpcmVkIGJ1dCB1bmtub3duIGZyb20gdGhlc2UgdmFsdWVzLCBpbnB1dCBvbiBsYXVuY2gKICAgIHZhbCA9IGdlbkFjdFN0cihTQ19BLCBTQ19CLCBIV0RFVCYweDFGRikKICAgIHJldHVybiB2YWwKIyBrbm93biBwb3NzaWJsZSByZXBseSB2YWx1ZXMgZnJvbSBhY3RpdmF0aW9uIHNlcnZlciwgbWltZT10ZXh0L3BsYWluOgojIC0gIklOVkFMSURfS0VZXzEiCiMgLSAiSU5WQUxJRF9LRVlfMiIKIyAtICJJTlZBTElEX0tFWV8zIgojIC0gIklOVEVSTkFMX0VSUk9SIgojIC0gIkhXX0NPVU5UX0ZPUkNFU19SRUdJU1RSQVRJT04iCiMgLSAiSFdfQ09VTlRfTU9SRV9USEFOXzMiCiMgLSAiSU5URVJOQUxfRVJST1JfTk9EQVRBQkFTRSIKIyAtICJIV19LRVk9WyUxNnNdIiA8LS0gJTE2cyBzaG91bGQgY29udGFpbiByZXN1bHQgb2YgZ2VuQWN0U3RyCgpfU0VSSUFMX1BBUlRTID0gKCdzZXJpYWxfcGFydF9hJywgJ3NlcmlhbF9wYXJ0X2InLCAnc2VyaWFsX3BhcnRfYycsICdzZXJpYWxfcGFydF9kJykKY2xhc3MgV2ViQXBpU2VydmUoaHR0cC5zZXJ2ZXIuQmFzZUhUVFBSZXF1ZXN0SGFuZGxlcik6CiAgICBkZWYgZG9fR0VUKHNlbGYpOgogICAgICAgIHNlbGYucHJvdG9jb2xfdmVyc2lvbiA9ICdIVFRQLzEuMScKICAgICAgICBwcmVzID0gdXJscGFyc2Uoc2VsZi5wYXRoWzE6XSkKICAgICAgICBzZWxmLnBhdGggPSB1bnF1b3RlKHByZXMucGF0aCkKICAgICAgICBxcHJvcHMgPSB7fQogICAgICAgIGlmIHByZXMucXVlcnk6CiAgICAgICAgICAgIGZvciBwcnAgaW4gcHJlcy5xdWVyeS5zcGxpdCgnJicpOgogICAgICAgICAgICAgICAgaWYgJz0nIG5vdCBpbiBwcnA6CiAgICAgICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgICAgIChrLHYpID0gcHJwLnNwbGl0KCc9JywgbWF4c3BsaXQ9MSkKICAgICAgICAgICAgICAgIHFwcm9wc1t1bnF1b3RlKGspXSA9IHVucXVvdGUodikKICAgICAgICAjQW55dGhpbmcgdGhhdCBpc24ndCBhIGNvbXBsZXRlIGFjdGl2YXRpb24gcmVxdWVzdCAoYnJvd3NlciB0YWIsCiAgICAgICAgI2Zhdmljb24gcHJvYmUpIGdldHMgYSA0MDQgYW5kIGxlYXZlcyBzZXJ2ZXIuYW5zd2VyZWQgYWxvbmUsIHNvIHdlIGtlZXAKICAgICAgICAjd2FpdGluZyBmb3IgdGhlIGdhbWUgaW5zdGVhZCBvZiBleGl0aW5nLiBQcmV2aW91c2x5IGNhbGNBY3RpdmF0aW9uQ29kZQogICAgICAgICNyYWlzZWQgS2V5RXJyb3Igb24gc3VjaCBhIHJlcXVlc3QgYW5kIHRvb2sgdGhlIHdob2xlIHNlcnZlciBkb3duIHdpdGgKICAgICAgICAjaXQuCiAgICAgICAgbWlzc2luZyA9IFtwIGZvciBwIGluIF9TRVJJQUxfUEFSVFMgaWYgcCBub3QgaW4gcXByb3BzXQogICAgICAgIGlmIG1pc3Npbmc6CiAgICAgICAgICAgIHByaW50KGYnSWdub3JpbmcgcmVxdWVzdCBmb3Ige3NlbGYucGF0aCFyfSAobm90IGFuIGFjdGl2YXRpb24gJwogICAgICAgICAgICAgICAgICBmJ3JlcXVlc3QsIG1pc3NpbmcgeyIsICIuam9pbihtaXNzaW5nKX0pJykKICAgICAgICAgICAgc2VsZi5zZW5kX2Vycm9yKDQwNCkKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgdHJ5OgogICAgICAgICAgICBod2sgPSBjYWxjQWN0aXZhdGlvbkNvZGUocXByb3BzKQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb24gYXMgZToKICAgICAgICAgICAgcHJpbnQoZidDb3VsZCBub3QgY29tcHV0ZSBhY3RpdmF0aW9uIGNvZGU6IHtlfScpCiAgICAgICAgICAgIHNlbGYuc2VuZF9lcnJvcig1MDApCiAgICAgICAgICAgIHJldHVybgogICAgICAgIGt0eHQgPSBmJ0hXX0tFWT1be2h3a31dJwogICAgICAgIGJ0eHQgPSBieXRlcyhrdHh0LCAidXRmOCIpCiAgICAgICAgc2VsZi5zZW5kX3Jlc3BvbnNlKDIwMCkKICAgICAgICBzZWxmLnNlbmRfaGVhZGVyKCJDb250ZW50LVR5cGUiLCAndGV4dC9wbGFpbicpCiAgICAgICAgc2VsZi5zZW5kX2hlYWRlcigiQ29udGVudC1MZW5ndGgiLCBsZW4oYnR4dCkpCiAgICAgICAgc2VsZi5lbmRfaGVhZGVycygpCiAgICAgICAgc2VsZi53ZmlsZS53cml0ZShidHh0KQogICAgICAgIHNlbGYuc2VydmVyLmFuc3dlcmVkID0gVHJ1ZQogICAgZGVmIGxvZ19tZXNzYWdlKHNlbGYsIGZtdCwgKmFyZ3MpOgogICAgICAgIHBhc3MgI2RlZmF1bHQgbG9ncyBldmVyeSByZXF1ZXN0IHRvIHN0ZGVycjsgdGhlIGNvbnNvbGUgaXMgYSB1c2VyLWZhY2luZwogICAgICAgICAgICAgI3Byb21wdCBoZXJlLCBrZWVwIGl0IHJlYWRhYmxlCmRlZiBfc2V0QWN0U2VydihudmFsKTogI3JlcXVpcmVzIGFkbWluIHJpZ2h0cwogICAgI3JldHVybnMgKHN1Y2Nlc3MsIG9sZHZhbCk7IG9sZHZhbCBpcyAnJyB3aGVuIHRoZXJlIHdhcyBub3RoaW5nIHRvIHJlc3RvcmUKICAgIG9sZHZhbCA9ICcnCiAgICB0cnk6CiAgICAgICAgI0NyZWF0ZUtleUV4IG9wZW5zIHRoZSBrZXkgaWYgaXQgZXhpc3RzLCBvciBjcmVhdGVzIGl0IChhbmQgYW55IG1pc3NpbmcKICAgICAgICAjcGFyZW50IGtleXMpIGlmIGl0IGRvZXNuJ3QgLS0gT3BlbktleSB3b3VsZCBzaWxlbnRseSBmYWlsIGhlcmUgb24gYQogICAgICAgICNmcmVzaCBpbnN0YWxsIHdoZXJlIHRoZSBnYW1lIGhhc24ndCBjcmVhdGVkIHRoaXMgcGF0aCB5ZXQsIGxlYXZpbmcKICAgICAgICAjdGhlIGFjdGl2YXRpb24gdXJsIHVuc2V0IGFuZCB0aGUgbG9jYWwgc2VydmVyIHVucmVhY2hhYmxlLgogICAgICAgIHdpdGggd2lucmVnLkNyZWF0ZUtleUV4KHdpbnJlZy5IS0VZX0xPQ0FMX01BQ0hJTkUsIFJFR1BBVEgsIGFjY2Vzcz1SRUdLX1BFUk1TKSBhcyBsbV9rZXk6CiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIChvbGR2YWwsIHQpID0gd2lucmVnLlF1ZXJ5VmFsdWVFeChsbV9rZXksIFJFR0tfQUNUU0VSVikKICAgICAgICAgICAgZXhjZXB0IEZpbGVOb3RGb3VuZEVycm9yOgogICAgICAgICAgICAgICAgb2xkdmFsID0gJycgI25vIHByZXZpb3VzIHZhbHVlLCBrZXkgd2FzIGp1c3QgY3JlYXRlZAogICAgICAgICAgICB3aW5yZWcuU2V0VmFsdWVFeChsbV9rZXksIFJFR0tfQUNUU0VSViwgMCwgd2lucmVnLlJFR19TWiwgbnZhbCkKICAgIGV4Y2VwdCBPU0Vycm9yIGFzIGU6CiAgICAgICAgcmV0dXJuIEZhbHNlLCBvbGR2YWwjcmFpc2UgZQogICAgcmV0dXJuIFRydWUsIG9sZHZhbApkZWYgX2RlbEFjdFNlcnYoKToKICAgICNSZXN0b3JlLXRvLW5vdGhpbmcuIFdoZW4gd2UgaGFkIHRvIENSRUFURSB0aGUgU2VyaWFsS2V5IGtleSAoZnJlc2ggaW5zdGFsbCwKICAgICNvbGR2YWwgPT0gJycpLCB3cml0aW5nICcnIGJhY2sgd291bGQgbGVhdmUgdGhlIGdhbWUgcGVybWFuZW50bHkgcG9pbnRlZCBhdAogICAgI2h0dHA6Ly8xMjcuMC4wLjEgLSBzbyBldmVyeSBsYXRlciBhY3RpdmF0aW9uIGF0dGVtcHQgd291bGQgZmFpbCBhZ2FpbnN0IGEKICAgICNzZXJ2ZXIgdGhhdCBpcyBubyBsb25nZXIgcnVubmluZy4gRGVsZXRpbmcgdGhlIHZhbHVlIGlzIHRoZSByZWFsICJ1bmRvIi4KICAgIHRyeToKICAgICAgICB3aXRoIHdpbnJlZy5DcmVhdGVLZXlFeCh3aW5yZWcuSEtFWV9MT0NBTF9NQUNISU5FLCBSRUdQQVRILCBhY2Nlc3M9UkVHS19QRVJNUykgYXMgbG1fa2V5OgogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICB3aW5yZWcuRGVsZXRlVmFsdWUobG1fa2V5LCBSRUdLX0FDVFNFUlYpCiAgICAgICAgICAgIGV4Y2VwdCBGaWxlTm90Rm91bmRFcnJvcjoKICAgICAgICAgICAgICAgIHBhc3MgI2FscmVhZHkgZ29uZSwgbm90aGluZyB0byB1bmRvCiAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICByZXR1cm4gRmFsc2UKICAgIHJldHVybiBUcnVlCmRlZiBfc2V0UmVnaXN0ZXJlZCgpOgogICAgdHJ5OgogICAgICAgIHdpdGggd2lucmVnLk9wZW5LZXkod2lucmVnLkhLRVlfQ1VSUkVOVF9VU0VSLCBSRUdQQVRILCBhY2Nlc3M9UkVHS19QRVJNUykgYXMgY3Vfa2V5OgogICAgICAgICAgICB3aW5yZWcuU2V0VmFsdWVFeChjdV9rZXksIFJFR0tfUkVHSVNULCAwLCB3aW5yZWcuUkVHX0RXT1JELCAxKQogICAgZXhjZXB0IE9TRXJyb3IgYXMgZToKICAgICAgICBwYXNzI3JhaXNlIGUKICAgIHJldHVybgpkZWYgX2dldFNlcktleSgpOgogICAgdmFsID0gRmFsc2UKICAgIHRyeToKICAgICAgICB3aXRoIHdpbnJlZy5PcGVuS2V5KHdpbnJlZy5IS0VZX0NVUlJFTlRfVVNFUiwgUkVHUEFUSCwgYWNjZXNzPVJFR0tfUEVSTVMpIGFzIGN1X2tleToKICAgICAgICAgICAgKHZhbCwgdCkgPSB3aW5yZWcuUXVlcnlWYWx1ZUV4KGN1X2tleSwgUkVHS19TRVJJQUwpCiAgICBleGNlcHQgT1NFcnJvciBhcyBlOgogICAgICAgIHBhc3MjcmFpc2UgZQogICAgcmV0dXJuIHZhbApjbGFzcyBXZWJTZXJ2ZXIoc29ja2V0c2VydmVyLlRDUFNlcnZlcik6CiAgICBhbGxvd19yZXVzZV9hZGRyZXNzID0gVHJ1ZQpkZWYgcnVuU2VydmVyKCk6CiAgICBnbG9iYWwgU0NfQgogICAgdmFsaWQgPSBGYWxzZQogICAgc2VydnNldCA9IEZhbHNlCiAgICBvbGRzZXJ2ID0gJycKICAgIHRyeToKICAgICAgICAoc2VydnNldCwgb2xkc2VydikgPSBfc2V0QWN0U2VydihSRUdWX05QQVRIKQogICAgICAgIGlmIG5vdCBzZXJ2c2V0OgogICAgICAgICAgICBwcmludChXQVJOX0FDVFNFUlYpCiAgICAgICAgZWxzZToKICAgICAgICAgICAgcHJpbnQoTk9URV9BQ1RTRVJWKQogICAgICAgIHJlZ3NlciA9IF9nZXRTZXJLZXkoKQogICAgICAgIGlmIHJlZ3NlcjoKICAgICAgICAgICAga2V5c3RyID0ga2V5cHJvY2VzcyhyZWdzZXIpCiAgICAgICAgICAgICh2YWxpZCwgU0NfQikgPSBfZ2V0U0NfQihrZXlzdHIpCiAgICAgICAgICAgIGlmIHZhbGlkOgogICAgICAgICAgICAgICAgcHJpbnQoTk9URV9SRUdTRVIpCiAgICAgICAgICAgICAgICBwcmludChrZXlzdHIpCiAgICAgICAgaWYgbm90IHZhbGlkOgogICAgICAgICAgICBwcmludChXQVJOX1JFR1NFUikKICAgICAgICB3aGlsZSBub3QgdmFsaWQ6ICNUT0RPIHRlc3Qgc2VyaWFsIGlucHV0CiAgICAgICAgICAgIGtleXN0ciA9IGlucHV0KFBSTVRfU0VSSUFMKS51cHBlcigpIAogICAgICAgICAgICAodmFsaWQsIFNDX0IpID0gX2dldFNDX0Ioa2V5c3RyKQogICAgICAgICAgICBpZiBub3QgdmFsaWQ6CiAgICAgICAgICAgICAgICBwcmludChFUlJfU0VSSUFMKQogICAgICAgIHdpdGggV2ViU2VydmVyKChIT1NULCBQT1JUKSwgV2ViQXBpU2VydmUpIGFzIHNlcnZlcjoKICAgICAgICAgICAgcHJpbnQoTk9URV9XQUlUSU5HKQogICAgICAgICAgICAjU2VydmUgdW50aWwgdGhlIGdhbWUgYWN0dWFsbHkgYXNrcyBmb3IgYW4gYWN0aXZhdGlvbiBjb2RlLCBub3QKICAgICAgICAgICAgI2p1c3QgdW50aWwgKnNvbWV0aGluZyogY29ubmVjdHMuIGhhbmRsZV9yZXF1ZXN0KCkgdXNlZCB0byBydW4KICAgICAgICAgICAgI2V4YWN0bHkgb25jZSwgc28gYW55IHN0cmF5IHJlcXVlc3QgKGEgYnJvd3NlciB0YWIgbGVmdCBvcGVuIG9uCiAgICAgICAgICAgICNsb2NhbGhvc3QsIGEgZmF2aWNvbiBwcm9iZSkgY29uc3VtZWQgaXQgYW5kIHRoZSBzZXJ2ZXIgZXhpdGVkCiAgICAgICAgICAgICNiZWZvcmUgdGhlIGdhbWUgZXZlciBnb3QgaXRzIHR1cm4uCiAgICAgICAgICAgIHNlcnZlci5hbnN3ZXJlZCA9IEZhbHNlCiAgICAgICAgICAgIHdoaWxlIG5vdCBzZXJ2ZXIuYW5zd2VyZWQ6CiAgICAgICAgICAgICAgICBzZXJ2ZXIuaGFuZGxlX3JlcXVlc3QoKQogICAgICAgIHByaW50KE5PVEVfRE9ORSkKICAgIGV4Y2VwdCBPU0Vycm9yIGFzIGU6CiAgICAgICAgI2J5IGZhciB0aGUgbW9zdCBsaWtlbHk6IHBvcnQgODAgYWxyZWFkeSB0YWtlbiAoSUlTLCBTa3lwZSwgYW5vdGhlcgogICAgICAgICN3ZWIgc2VydmVyKSBvciBib3VuZCB3aXRob3V0IGFkbWluIHJpZ2h0cwogICAgICAgIHByaW50KEVSUl9QT1JULmZvcm1hdChwb3J0PVBPUlQsIGVycj1lKSkKICAgIGV4Y2VwdCBLZXlib2FyZEludGVycnVwdDoKICAgICAgICBwcmludCgnQ2FuY2VsbGVkLicpCiAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgIGltcG9ydCB0cmFjZWJhY2sKICAgICAgICB0cmFjZWJhY2sucHJpbnRfZXhjKCkKICAgIGZpbmFsbHk6CiAgICAgICAgI0Fsd2F5cyBwdXQgdGhlIHJlZ2lzdHJ5IGJhY2suIFJlc3RvcmluZyAnJyB3b3VsZCBwaW4gdGhlIGdhbWUgdG8gb3VyCiAgICAgICAgI2RlYWQgbG9jYWxob3N0IFVSTCwgc28gYW4gYWJzZW50IHByZXZpb3VzIHZhbHVlIG1lYW5zICJkZWxldGUiLgogICAgICAgIGlmIHNlcnZzZXQ6CiAgICAgICAgICAgIGlmIG9sZHNlcnY6CiAgICAgICAgICAgICAgICBfc2V0QWN0U2VydihvbGRzZXJ2KQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgX2RlbEFjdFNlcnYoKQogICAgICAgICAgICBwcmludChOT1RFX1JFU1RPUkVEKQogICAgaW5wdXQoJ1ByZXNzIEVudGVyIHRvIGNsb3NlLi4uJykKZGVmIGlzQWRtaW4oKToKICAgIHRyeToKICAgICAgICByZXR1cm4gY3R5cGVzLndpbmRsbC5zaGVsbDMyLklzVXNlckFuQWRtaW4oKQogICAgZXhjZXB0OgogICAgICAgIHJldHVybiBGYWxzZQpkZWYgdHJ5UnVuV2luQWRtaW4oKToKICAgIGlmIGlzQWRtaW4oKToKICAgICAgICBydW5TZXJ2ZXIoKQogICAgICAgIHJldHVybgogICAgI1F1b3RlIGV2ZXJ5IGFyZ3VtZW50LiBUaGlzIHNjcmlwdCBpcyB3cml0dGVuIHRvCiAgICAjJUxPQ0FMQVBQREFUQSVcVFcxIENvbnRyb2wgQ2VudGVyXFRXMSBMb2NhbCBBY3RpdmF0aW9uIFNlcnZlci5weSAtIGEgcGF0aAogICAgI3dpdGggc3BhY2VzIGluIGl0IC0gc28gdGhlIG9sZCAnICcuam9pbihzeXMuYXJndikgaGFuZGVkIHRoZSBlbGV2YXRlZAogICAgI3B5dGhvbi5leGUgdGhlIGZyYWdtZW50cyAiLi4uXFRXMSIsICJMb2NhbCIsICJBY3RpdmF0aW9uIiwgLi4uIGFuZCBpdCBkaWVkCiAgICAjd2l0aCAiY2FuJ3Qgb3BlbiBmaWxlIiBiZWZvcmUgZG9pbmcgYW55dGhpbmcuIFRoZSBVQUMgcHJvbXB0IGFwcGVhcmVkIGFuZAogICAgI3RoZW4gbm90aGluZyBoYXBwZW5lZCwgd2hpY2ggaXMgZXhhY3RseSB3aGF0IGl0IGxvb2tlZCBsaWtlIGluIHByYWN0aWNlLgogICAgcGFyYW1zID0gJyAnLmpvaW4oZicie2F9IicgZm9yIGEgaW4gc3lzLmFyZ3YpCiAgICByZXRWID0gY3R5cGVzLndpbmRsbC5zaGVsbDMyLlNoZWxsRXhlY3V0ZVcoTm9uZSwgInJ1bmFzIiwgc3lzLmV4ZWN1dGFibGUsIHBhcmFtcywgTm9uZSwgMSkKICAgIGlmIHJldFYgPD0gMzI6CiAgICAgICAgIzw9MzIgaXMgdGhlIGRvY3VtZW50ZWQgU2hlbGxFeGVjdXRlVyBmYWlsdXJlIHJhbmdlICg1ID0gdXNlciBkZWNsaW5lZAogICAgICAgICN0aGUgVUFDIHByb21wdCkuIEZhbGwgYmFjayB0byBydW5uaW5nIHVuZWxldmF0ZWQ6IHNldHRpbmcgdGhlCiAgICAgICAgI0hLTE0gYWN0aXZhdGlvbiBVUkwgd2lsbCBmYWlsIGFuZCBiZSByZXBvcnRlZCwgYnV0IGEgc2VyaWFsIGtleQogICAgICAgICNhbHJlYWR5IHByZXNlbnQgaW4gSEtDVSBzdGlsbCBsZXRzIHRoZSBmbG93IGNvbXBsZXRlLgogICAgICAgIHByaW50KGYnQ291bGQgbm90IHJlc3RhcnQgd2l0aCBhZG1pbmlzdHJhdG9yIHJpZ2h0cyAoY29kZSB7cmV0Vn0pLCAnCiAgICAgICAgICAgICAgZidjb250aW51aW5nIHdpdGhvdXQgdGhlbS4nKQogICAgICAgIHJ1blNlcnZlcigpCmRlZiBtYWluKCk6CiAgICBpZiBvcy5uYW1lID09J250JzoKICAgICAgICB0cnlSdW5XaW5BZG1pbigpCiAgICBlbHNlOgogICAgICAgIHJ1blNlcnZlcigpCm1haW4oKQo="
)


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
