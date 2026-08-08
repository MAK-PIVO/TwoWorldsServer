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
import struct
import ctypes
import webbrowser
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import datetime
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
    'tab.server': {'ru': 'Сервер', 'en': 'Server'},
    'tab.settings': {'ru': 'Настройки', 'en': 'Settings'},
    'tab.game': {'ru': 'Игра', 'en': 'Game'},
    'tab.network': {'ru': 'Сеть', 'en': 'Network'},
    'tab.activation': {'ru': 'Активация', 'en': 'Activation'},
    'tab.log': {'ru': 'Лог', 'en': 'Log'},

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
    ACCENT = '#3b6ea5'
    OK_COLOR = '#2e8b3d'
    BAD_COLOR = '#b23b3b'

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
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TNotebook.Tab', padding=(14, 6))
        style.configure('Accent.TButton', foreground='white', background=self.ACCENT)
        style.map('Accent.TButton', background=[('active', '#2f5a86')])
        style.configure('Stop.TButton', foreground='white', background=self.BAD_COLOR)
        style.map('Stop.TButton', background=[('active', '#8f2f2f')])
        style.configure('Header.TLabel', font=('Segoe UI', 12, 'bold'))
        style.configure('Status.TLabel', font=('Segoe UI', 10, 'bold'))

    # -- layout --------------------------------------------------------------
    def _make_scrollable_tab(self, notebook, title_key):
        """A notebook tab whose content scrolls vertically when the window is
        too short to show everything - without this, the lower controls on the
        denser tabs (Game/Network) are simply unreachable on small screens."""
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=T(title_key))
        self._tr(title_key, lambda t, nb=notebook, o=outer: nb.tab(o, text=t))

        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
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

        self.players_menu = tk.Menu(self, tearoff=0)
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
        self.set_motd = scrolledtext.ScrolledText(f, width=55, height=4, wrap='word')
        self.set_motd.grid(row=2, column=1, sticky='w', **pad)
        ttk.Label(f, text='Поддерживает цвет игры: <0xAARRGGBB>, шрифт <F2>, паузу <break=сек>',
                  foreground='#666666').grid(row=3, column=1, sticky='w', padx=10)

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
        ttk.Label(f, text=note, foreground='#666666', wraplength=560, justify='left').grid(
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
        cfg['game'] = {'ExePath': ''}
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
                  foreground='#666666').grid(row=1, column=0, columnspan=3, sticky='w', padx=10)
        self.game_exe_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.game_exe_var, width=60).grid(row=2, column=0, columnspan=2, sticky='w', **pad)
        browse_btn = ttk.Button(f, command=self._browse_game_exe)
        browse_btn.grid(row=2, column=2, sticky='w', **pad)
        self._tr('game.browse', lambda t: browse_btn.configure(text=t))

        launch_btn = ttk.Button(f, style='Accent.TButton', command=self._launch_game)
        launch_btn.grid(row=3, column=0, sticky='w', padx=10, pady=14)
        self._tr('game.launch', lambda t: launch_btn.configure(text=t))

        ttk.Separator(f, orient='horizontal').grid(row=4, column=0, columnspan=3, sticky='ew', pady=10)

        fov_lbl = ttk.Label(f, style='Header.TLabel')
        fov_lbl.grid(row=5, column=0, columnspan=3, sticky='w', **pad)
        self._tr('game.fov_header', lambda t: fov_lbl.configure(text=t))
        ttk.Label(f, text='На широких мониторах камера в TW1 может казаться слишком "приближенной".\n'
                           'ForceCameraAspectX/Y - реальные поля в настройках игры (не выдуманные), но их точный\n'
                           'эффект не подтверждён - пробуйте на свой риск, здесь же можно мгновенно вернуть 0/0.',
                  foreground='#666666', justify='left').grid(row=6, column=0, columnspan=3, sticky='w', padx=10)
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

    def _launch_game(self):
        exe = self._resolve_game_exe(self.game_exe_var.get().strip())
        if not exe or not os.path.exists(exe):
            messagebox.showerror(T('game.not_found_title'), T('game.not_found_body', exe=GAME_EXE_NAME))
            return
        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
            print(f'[Игра] Запущена: {exe}')
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
                  foreground='#666666', justify='left').grid(row=1, column=0, columnspan=2, sticky='w', padx=10)

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
                  foreground='#666666', justify='left').grid(row=9, column=0, columnspan=2, sticky='w', padx=10)

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
                  foreground='#666666', justify='left').grid(row=13, column=0, columnspan=2, sticky='w', padx=10)

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
                                                    font=('Consolas', 9), background='#111417',
                                                    foreground='#d8dee4', insertbackground='#d8dee4')
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
    "IFRPRE8gQ0FUQ0ggRVJST1IKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgd2l0aCBvcGVuKHBh"
    "dGgsICd3YicpIGFzIGY6I1RPRE8gY2F0Y2ggZXJyb3JzCiAgICAgICAgICAgIGYud3JpdGUoZGF0"
    "YSkKICAgIGRlZiBnZXRXaG9pcyhzZWxmLCBuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcmVzID0gd2N1"
    "ci5leGVjdXRlKF9TUUxfZ2V0V2hvaXMsIChuYW1lLCkpLmZldGNob25lKCkKICAgICAgICAgICAg"
    "d2N1ci5jbG9zZSgpCiAgICAgICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAg"
    "cmV0dXJuIE5vbmUKICAgICAgICAgICAgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRl"
    "c2NyaXB0aW9uKSA9IHJlcwogICAgICAgICAgICBjdXJZZWFyID0gZGF0ZXRpbWUuZGF0ZXRpbWUu"
    "bm93KCkueWVhcgogICAgICAgICAgICBhZ2UgPSBtYXgoMCwgY3VyWWVhciAtIHlvYikgaWYgeW9i"
    "IGVsc2UgMAogICAgICAgICAgICByZXR1cm4gewogICAgICAgICAgICAgICAgJ2VtYWlsJzogZW1h"
    "aWwgb3IgJycsCiAgICAgICAgICAgICAgICAnbG9jYXRpb24nOiBsb2NhdGlvbiBvciAnJywKICAg"
    "ICAgICAgICAgICAgICdhZ2UnOiBhZ2UsCiAgICAgICAgICAgICAgICAnZ2VuZGVyJzogZ2VuZGVy"
    "IGlmIGdlbmRlciBpcyBub3QgTm9uZSBlbHNlIDAsCiAgICAgICAgICAgICAgICAnZGVzY3JpcHRp"
    "b24nOiBkZXNjcmlwdGlvbiBvciAnJwogICAgICAgICAgICB9CiAgICBkZWYgdXBkYXRlV2hvaXMo"
    "c2VsZiwgbmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgog"
    "ICAgICAgIHRyeToKICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAgICBleGNlcHQgKFR5"
    "cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgICAgIGFnZSA9IDAKICAgICAgICB0cnk6CiAg"
    "ICAgICAgICAgIGdlbmRlciA9IGludChnZW5kZXIpCiAgICAgICAgZXhjZXB0IChUeXBlRXJyb3Is"
    "IFZhbHVlRXJyb3IpOgogICAgICAgICAgICBnZW5kZXIgPSAwCiAgICAgICAgeW9iID0gZGF0ZXRp"
    "bWUuZGF0ZXRpbWUubm93KCkueWVhciAtIGFnZQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAg"
    "ICAgICAgICB3Y3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB3Y3VyLmV4ZWN1dGUo"
    "X1NRTFVQRF93aG9pcywgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRlc2NyaXB0aW9u"
    "LCBuYW1lKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICB3Y3VyLmNs"
    "b3NlKCkKICAgICMjIEdVSUxEUwogICAgZGVmIGdldEd1aWxkT2Yoc2VsZiwgdXNlcm5hbWUpOgog"
    "ICAgICAgICMtPiAoZ3VpbGRuYW1lLCByYW5rKSBvciAoTm9uZSwgMCkKICAgICAgICB3aXRoIHNl"
    "bGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICBy"
    "ZXMgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5hbWUsKSkuZmV0Y2hvbmUo"
    "KQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAgICAg"
    "ICAgICByZXR1cm4gKE5vbmUsIDApCiAgICAgICAgcmV0dXJuIChyZXNbMF0sIHJlc1sxXSBvciAw"
    "KQogICAgZGVmIGdldEd1aWxkTmFtZShzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgcmV0dXJuIHNl"
    "bGYuZ2V0R3VpbGRPZih1c2VybmFtZSlbMF0gb3IgJycKICAgIGRlZiBnZXRHdWlsZE1lbWJlcnMo"
    "c2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3Vy"
    "ID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBjdXIuZXhlY3V0ZShfU1FMX2d1"
    "aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKS5mZXRjaGFsbCgpCiAgICAgICAgICAgIGN1ci5jbG9z"
    "ZSgpCiAgICAgICAgcmV0dXJuIFsoclswXSwgclsxXSBvciAwKSBmb3IgciBpbiByZXNdCiAgICBk"
    "ZWYgZ3VpbGRFeGlzdHMoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByb3cgPSBjdXIu"
    "ZXhlY3V0ZShfU1FMX2d1aWxkRXhpc3RzLCAoKGd1aWxkbmFtZSBvciAnJykuY2FzZWZvbGQoKSwp"
    "KS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIHJvdyBp"
    "cyBub3QgTm9uZQogICAgZGVmIGd1aWxkTmFtZUZyZWUoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAg"
    "ICAjU2FtZSBydWxlcyBjcmVhdGVHdWlsZCgpIGVuZm9yY2VzLCBhc2tlZCBpbiBhZHZhbmNlIC0g"
    "dGhlIGNsaWVudAogICAgICAgICNjaGVja3MgYSBuYW1lIHdpdGggL3Rlc3RjcmVhdGVndWlsZCBi"
    "ZWZvcmUgaXQgd2lsbCBsZXQgdGhlIHBsYXllcgogICAgICAgICNjb25maXJtLiBBbnN3ZXJpbmcg"
    "ImZyZWUiIGZvciBhIG5hbWUgY3JlYXRlR3VpbGQgd291bGQgdGhlbiByZWplY3QKICAgICAgICAj"
    "d291bGQganVzdCBtb3ZlIHRoZSBkZWFkIGVuZCBvbmUgZGlhbG9nIGxhdGVyLgogICAgICAgIGlm"
    "IG5vdCBfUkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFtZSBvciAnJyk6CiAgICAgICAg"
    "ICAgIHJldHVybiBGYWxzZQogICAgICAgIHJldHVybiBub3Qgc2VsZi5ndWlsZEV4aXN0cyhndWls"
    "ZG5hbWUpCiAgICBkZWYgbGlzdEd1aWxkcyhzZWxmKToKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByb3dzID0gY3Vy"
    "LmV4ZWN1dGUoX1NRTF9hbGxHdWlsZHMpLmZldGNoYWxsKCkKICAgICAgICAgICAgY3VyLmNsb3Nl"
    "KCkKICAgICAgICByZXR1cm4gW3JbMF0gZm9yIHIgaW4gcm93c10KICAgIGRlZiBjcmVhdGVHdWls"
    "ZChzZWxmLCBndWlsZG5hbWUsIG93bmVyLCBkZXNjcmlwdGlvbj0nJyk6CiAgICAgICAgIy0+IGd1"
    "aWxkbmFtZSBvbiBzdWNjZXNzLCBvciBhbiBlcnJvciB0b2tlbiBmb3IgdGhlIGNsaWVudAogICAg"
    "ICAgIGlmIG5vdCBfUkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFtZSBvciAnJyk6CiAg"
    "ICAgICAgICAgIHJldHVybiAnYmFkR3VpbGROYW1lJwogICAgICAgIHdpdGggc2VsZi5sb2NrOgog"
    "ICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIGlmIGN1ci5leGVj"
    "dXRlKF9TUUxfZ3VpbGRPZlVzZXIsIChvd25lciwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgog"
    "ICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnYWxyZWFk"
    "eUluR3VpbGQnCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRFeGlzdHMsIChn"
    "dWlsZG5hbWUuY2FzZWZvbGQoKSwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAgICAg"
    "ICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnZ3VpbGROYW1lVGFrZW4n"
    "CiAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfY3JlYXRlR3VpbGQsCiAgICAgICAgICAgICAg"
    "ICAgICAgICAgIChndWlsZG5hbWUsIGd1aWxkbmFtZS5jYXNlZm9sZCgpLCBvd25lciwKICAgICAg"
    "ICAgICAgICAgICAgICAgICAgIGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLCBzYW5pdGl6ZVRleHQo"
    "ZGVzY3JpcHRpb24pKSkKICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9hZGRHdWlsZE1lbWJl"
    "ciwgKGd1aWxkbmFtZSwgb3duZXIsIDIpKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAg"
    "ICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBqb2luR3Vp"
    "bGQoc2VsZiwgZ3VpbGRuYW1lLCB1c2VybmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAg"
    "ICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcm93ID0gY3VyLmV4"
    "ZWN1dGUoX1NRTF9ndWlsZEV4aXN0cywgKChndWlsZG5hbWUgb3IgJycpLmNhc2Vmb2xkKCksKSku"
    "ZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiByb3cgaXMgTm9uZToKICAgICAgICAgICAgICAgIGN1"
    "ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ3Vua25vd25HdWlsZCcKICAgICAgICAg"
    "ICAgI1N0b3JlIHRoZSBndWlsZCdzIG93biBzcGVsbGluZywgbm90IHdoYXRldmVyIGNhc2UgdGhl"
    "IGNsaWVudCB0eXBlZAogICAgICAgICAgICAjaW50byB0aGUgam9pbiBib3gsIHNvIGdldEd1aWxk"
    "TWVtYmVycygpIGZpbmRzIHRoZSBtZW1iZXIgYmFjay4KICAgICAgICAgICAgZ3VpbGRuYW1lID0g"
    "cm93WzBdCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRPZlVzZXIsICh1c2Vy"
    "bmFtZSwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3Nl"
    "KCkKICAgICAgICAgICAgICAgIHJldHVybiAnYWxyZWFkeUluR3VpbGQnCiAgICAgICAgICAgIGN1"
    "ci5leGVjdXRlKF9TUUxfYWRkR3VpbGRNZW1iZXIsIChndWlsZG5hbWUsIHVzZXJuYW1lLCAwKSkK"
    "ICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAg"
    "ICAgIHJldHVybiBOb25lCiAgICBkZWYgbGVhdmVHdWlsZChzZWxmLCB1c2VybmFtZSk6CiAgICAg"
    "ICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAg"
    "ICAgICAgICAgcmVzID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwgKHVzZXJuYW1lLCkp"
    "LmZldGNob25lKCkKICAgICAgICAgICAgaWYgcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICBj"
    "dXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuICdub3RJbkd1aWxkJwogICAgICAgICAg"
    "ICAoZ3VpbGRuYW1lLCByYW5rKSA9IChyZXNbMF0sIHJlc1sxXSBvciAwKQogICAgICAgICAgICBj"
    "dXIuZXhlY3V0ZShfU1FMX2RlbEd1aWxkTWVtYmVyLCAodXNlcm5hbWUsKSkKICAgICAgICAgICAg"
    "b3duZXIgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT3duZXIsIChndWlsZG5hbWUsKSkuZmV0Y2hv"
    "bmUoKQogICAgICAgICAgICBpZiBvd25lciBhbmQgb3duZXJbMF0gPT0gdXNlcm5hbWU6CiAgICAg"
    "ICAgICAgICAgICAjVGhlIGZvdW5kZXIgbGVhdmluZyBkaXNzb2x2ZXMgdGhlIGd1aWxkIHJhdGhl"
    "ciB0aGFuIGxlYXZpbmcgYW4KICAgICAgICAgICAgICAgICNvd25lcmxlc3MgcmVjb3JkIHRoYXQg"
    "bm9ib2R5IGNhbiBldmVyIGFkbWluaXN0ZXIuCiAgICAgICAgICAgICAgICBjdXIuZXhlY3V0ZShf"
    "U1FMX2RlbEd1aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKQogICAgICAgICAgICAgICAgY3VyLmV4"
    "ZWN1dGUoX1NRTF9kZWxldGVHdWlsZCwgKGd1aWxkbmFtZSwpKQogICAgICAgICAgICBzZWxmLmRi"
    "LmNvbW1pdCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAg"
    "IGRlZiBsb2dpblBsYXllcihzZWxmLCB1c2VybmFtZSwgY29uLCBwYXNzd29yZCk6I1RPRE8gc2hv"
    "dWxkIHJldHVybiBlcnJvciBwcm9wZXJseSB0byBjbGllbnQKICAgICAgICBpZiBub3QgX1JFX1ZB"
    "TElEX1VTRVJOQU1FLm1hdGNoKHVzZXJuYW1lKToKICAgICAgICAgICAgI1JlZ2lzdHJhdGlvbiBo"
    "YXMgYWx3YXlzIHZhbGlkYXRlZCB0aGUgbmFtZTsgbG9nZ2luZyBpbiBkaWQgbm90LgogICAgICAg"
    "ICAgICAjTmFtZXMgcmVhY2ggb3RoZXIgY2xpZW50cyBpbnNpZGUgcXVvdGVkIHByb3RvY29sIGZp"
    "ZWxkcywgc28gYSBuYW1lCiAgICAgICAgICAgICNjb250YWluaW5nICciJyBmb3JnZXMgY29tbWFu"
    "ZHMgLSBhbmQgdGhlIEFsbG93QW55TG9naW4gZGVidWcgcGF0aAogICAgICAgICAgICAjYmVsb3cg"
    "bmV2ZXIgdG91Y2hlcyB0aGUgZGF0YWJhc2UsIHdoaWNoIG1hZGUgaXQgdGhlIG9uZSB3YXkgdG8g"
    "Z2V0CiAgICAgICAgICAgICNzdWNoIGEgbmFtZSBpbi4gQ2hlY2sgaGVyZSBzbyBib3RoIHBhdGhz"
    "IGFyZSBjb3ZlcmVkLgogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIGlmIF9ERUJVR19B"
    "TExPV19BTllfTE9HSU46ICNERUJVRyBBVVRPIEFMTE9XCiAgICAgICAgICAgIHJldHVybiBVc2Vy"
    "KHVzZXJuYW1lLCBjb24pCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGxvZ2lu"
    "Q3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICAjRGVmYXVsdCB0byBTVFJJQ1QsIFRP"
    "RE8gYWxsb3cgZm9yIG5vbi1zdHJpY3Q/CiAgICAgICAgICAgIHVpZHJlcyA9IGxvZ2luQ3VyLmV4"
    "ZWN1dGUoX1NRTF91c2VySURfc3RyaWN0LCAodXNlcm5hbWUsIGNvbi5TSykpLmZldGNob25lKCkK"
    "ICAgICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICAjcHJpbnQoJ2xv"
    "Z2luIGVycm9yOiBubyB1c2VyIHdpdGggdGhhdCBzZXJpYWwga2V5JykKICAgICAgICAgICAgICAg"
    "IGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNObyBzdWNoIFVz"
    "ZXIKICAgICAgICAgICAgdWlkID0gdWlkcmVzWzBdCiAgICAgICAgICAgIChyVXNlciwgcGFzc2hh"
    "c2gsIHVTYWx0LCBoSXRyKSA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9nZXRMb2dpbiwgKHVpZCwg"
    "KSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1c2VybmFtZSAhPSByVXNlcjoKICAgICAgICAg"
    "ICAgICAgICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyB1c2VybmFtZToge3VzZXJuYW1lfScp"
    "CiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4g"
    "Tm9uZSAjV3JvbmcgVXNlcm5hbWUKICAgICAgICAgICAgdHBhcyA9IF9zYWx0X2hhc2hfKHBhc3N3"
    "b3JkLCB1U2FsdCwgaEl0cikKICAgICAgICAgICAgaWYgdHBhcyAhPSBwYXNzaGFzaDoKICAgICAg"
    "ICAgICAgICAgICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyBwYXNzd29yZDoge3Bhc3N3b3Jk"
    "fScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1"
    "cm4gTm9uZSAjV3JvbmcgUGFzc3dvcmQKICAgICAgICAgICAgaWYgaEl0ciAhPSBfSEFTSElURVI6"
    "CiAgICAgICAgICAgICAgICBucHNoID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHVTYWx0LCBfSEFT"
    "SElURVIpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxVUERfcGFzc0hhc2gs"
    "IChucHNoLCBfSEFTSElURVIsIHVpZCkpCiAgICAgICAgICAgIHVzZXJvYmogPSBVc2VyKHVzZXJu"
    "YW1lLCBjb24pCiAgICAgICAgICAgICN1cGRhdGUgbGFzdCBsb2dpbgogICAgICAgICAgICBsb2dp"
    "bkN1ci5leGVjdXRlKF9TUUxfbG9naW5VcGRhdGUsICh1c2Vyb2JqLmxvZ2luVGltZSwgdWlkKSkK"
    "ICAgICAgICAgICAgI1RPRE8gZGVmYXVsdCBkYXRldGltZSBhZGFwdGVyIGRlcHJlY2F0ZWQsIGNo"
    "ZWNrIHJlcGxhY2VtZW50CiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAg"
    "bG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICByZXR1cm4gdXNlcm9iagogICAgZGVmIHJlZ2lz"
    "dGVyUGxheWVyKHNlbGYsIHVzZXJuYW1lLCBjb24sIHBhc3N3b3JkLCBlbWFpbCwgbG9jYXRpb24s"
    "IGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgaWYgbm90IF9SRV9WQUxJRF9VU0VS"
    "TkFNRS5tYXRjaCh1c2VybmFtZSk6CiAgICAgICAgICAgIHJldHVybiBOb25lICNJbnZhbGlkIHVz"
    "ZXJuYW1lIChiYWQgY2hhcnMvbGVuZ3RoKSwgYWxzbyBibG9ja3MgcHJvdG9jb2wtaW5qZWN0aW9u"
    "IHZpYSAnIicKICAgICAgICBlbWFpbCA9IHNhbml0aXplVGV4dChlbWFpbCkKICAgICAgICBsb2Nh"
    "dGlvbiA9IHNhbml0aXplVGV4dChsb2NhdGlvbikKICAgICAgICBkZXNjcmlwdGlvbiA9IHNhbml0"
    "aXplVGV4dChkZXNjcmlwdGlvbikKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAg"
    "bG9naW5DdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHVpZHJlcyA9IGxvZ2luQ3Vy"
    "LmV4ZWN1dGUoX1NRTF91c2VySUQsICh1c2VybmFtZSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAg"
    "ICBpZiB1aWRyZXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICAjcHJpbnQoZidyZWdpc3Rl"
    "ciBlcnJvcjogdXNlcm5hbWUgYWxyZWFkeSBpbiB1c2U6IHt1c2VybmFtZX0nKQogICAgICAgICAg"
    "ICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1VzZXIg"
    "ZXhpc3RzCiAgICAgICAgICAgICNpZiBzdHJpY3QsIGNoZWNrIGlmIHNlcmlhbCBpcyBpbiB1c2Ug"
    "dG9vCiAgICAgICAgICAgICNUT0RPIG9ubHkgYXBwbHkgaWYgc3RyaWN0CiAgICAgICAgICAgIHVp"
    "ZHJlcyA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF91c2VySURfU2NoaywgKGNvbi5TSywgKSkuZmV0"
    "Y2hvbmUoKQogICAgICAgICAgICBpZiB1aWRyZXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAg"
    "ICAjcHJpbnQoJ3JlZ2lzdGVyIGVycm9yOiBzZXJpYWwgYWxyZWFkeSBpbiB1c2UnKQogICAgICAg"
    "ICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1Nl"
    "cmlhbCBpbiB1c2UgZXhpc3RzCiAgICAgICAgICAgIHVTYWx0ID0gb3MudXJhbmRvbSgxNikKICAg"
    "ICAgICAgICAgcEhhc2ggPSBfc2FsdF9oYXNoXyhwYXNzd29yZCwgdVNhbHQsIF9IQVNISVRFUikK"
    "ICAgICAgICAgICAgY3VydGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpCiAgICAgICAgICAg"
    "IHRyeTojdHJ5IHNob3VsZG4ndCBiZSBuZWVkZWQgYXMgZW1wdHkgZmllbGQgaXMgc2V0IHRvIDI1"
    "NQogICAgICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAgICAgICAgZXhjZXB0OgogICAg"
    "ICAgICAgICAgICAgYWdlID0gMAogICAgICAgICAgICB5b2IgPSBjdXJ0aW1lLnllYXIgLSBhZ2UK"
    "ICAgICAgICAgICAgcmVndmFscyA9ICgKICAgICAgICAgICAgICAgIHVzZXJuYW1lLHBIYXNoLAog"
    "ICAgICAgICAgICAgICAgY29uLlNLLHVTYWx0LF9IQVNISVRFUiwKICAgICAgICAgICAgICAgIGN1"
    "cnRpbWUsZW1haWwsbG9jYXRpb24seW9iLGdlbmRlcixkZXNjcmlwdGlvbgogICAgICAgICAgICAp"
    "CiAgICAgICAgICAgIGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9yZWdpc3RlclVzZXIsIHJlZ3ZhbHMp"
    "CiAgICAgICAgICAgICNUT0RPIGRlZmF1bHQgZGF0ZXRpbWUgYWRhcHRlciBkZXByZWNhdGVkLCBj"
    "aGVjayByZXBsYWNlbWVudAogICAgICAgICAgICB1c2Vyb2JqID0gVXNlcih1c2VybmFtZSwgY29u"
    "KQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3Nl"
    "KCkKICAgICAgICAgICAgcmV0dXJuIHVzZXJvYmoKICAgIGRlZiBkZWxldGVBY2NvdW50KHNlbGYs"
    "IHVzZXJuYW1lKToKICAgICAgICAjQWRtaW4tcGFuZWwgYWN0aW9uIChHVUkgItCj0LTQsNC70LjR"
    "gtGMINC/0LXRgNGB0L7QvdCw0LbQsCIpOiBwZXJtYW5lbnRseSByZW1vdmVzIGFuCiAgICAgICAg"
    "I2FjY291bnQgYW5kIGV2ZXJ5IHNhdmVkIHBsYXllcmRhdGEgYmxvYiBmb3IgaXQuIElycmV2ZXJz"
    "aWJsZSAtIHRoZQogICAgICAgICNHVUkgaXMgZXhwZWN0ZWQgdG8gY29uZmlybSB3aXRoIHRoZSBh"
    "ZG1pbiBiZWZvcmUgY2FsbGluZyB0aGlzLgogICAgICAgICNEb2VzIE5PVCB0b3VjaCB0aGUgY2Fs"
    "bGVyJ3MgbGl2ZSBjb25uZWN0aW9uL3Nlc3Npb247IHRoZSBjYWxsZXIgaXMKICAgICAgICAjcmVz"
    "cG9uc2libGUgZm9yIGtpY2tpbmcgZmlyc3QgaWYgdGhlIGFjY291bnQgaXMgY3VycmVudGx5IG9u"
    "bGluZQogICAgICAgICMoc2VlIENvcmVTZXJ2ZXIuZGVsZXRlQWNjb3VudCksIG90aGVyd2lzZSBh"
    "IGNvbm5lY3RlZCBjbGllbnQgd291bGQKICAgICAgICAja2VlcCBwbGF5aW5nIHdpdGggYW4gYWNj"
    "b3VudCB0aGF0IG5vIGxvbmdlciBleGlzdHMgaW4gdGhlIERCLgogICAgICAgIHdpdGggc2VsZi5s"
    "b2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHVpZHJl"
    "cyA9IGN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5hbWUsICkpLmZldGNob25lKCkKICAg"
    "ICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xvc2UoKQog"
    "ICAgICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgICAgIHVpZCA9IHVpZHJlc1swXQog"
    "ICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2RlbGV0ZVVzZXIsICh1c2VybmFtZSwgKSkKICAg"
    "ICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAg"
    "ICNHdWlsZCBtZW1iZXJzaGlwIG91dGxpdmVzIHRoZSB1c2VyVGFibGUgcm93IG90aGVyd2lzZSwg"
    "c28gdGhlIGRlbGV0ZWQKICAgICAgICAjbmFtZSB3b3VsZCBrZWVwIHNob3dpbmcgdXAgaW4gaXRz"
    "IGd1aWxkJ3Mgcm9zdGVyIGZvcmV2ZXIuCiAgICAgICAgc2VsZi5sZWF2ZUd1aWxkKHVzZXJuYW1l"
    "KQogICAgICAgICNQbGF5ZXJkYXRhIGZpbGVzICgie3VzZXJJRDp4fV97Zm9ybUlEOnh9LmJpbiIp"
    "IGxpdmUgb3V0c2lkZSB0aGUgREIKICAgICAgICAjdHJhbnNhY3Rpb24gYW5kIGFyZSBsb29rZWQg"
    "dXAgYnkgcHJlZml4IC0gYmVzdCBlZmZvcnQsIGEgbGVmdG92ZXIKICAgICAgICAjZmlsZSBoZXJl"
    "IGlzbid0IHdvcnRoIGZhaWxpbmcgdGhlIHdob2xlIGRlbGV0aW9uIG92ZXIuCiAgICAgICAgcHJl"
    "Zml4ID0gZid7dWlkOnh9XycKICAgICAgICB0cnk6CiAgICAgICAgICAgIGZvciBmbiBpbiBvcy5s"
    "aXN0ZGlyKF9QQVRIX1BMQVlFUkRBVEEpOgogICAgICAgICAgICAgICAgaWYgZm4uc3RhcnRzd2l0"
    "aChwcmVmaXgpOgogICAgICAgICAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgICAgICAg"
    "ICAgb3MucmVtb3ZlKG9zLnBhdGguam9pbihfUEFUSF9QTEFZRVJEQVRBLCBmbikpCiAgICAgICAg"
    "ICAgICAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgICAgICAgICAgICAgIHBhc3MK"
    "ICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgcGFzcwogICAgICAgIHJldHVybiBU"
    "cnVlCkdESCA9IERhdGFIYW5kbGVyKCkKCmRlZiBfd29Vc2VyKHVsLCB1c3IpOgogICAgcmV0dXJu"
    "IGxpc3QoIChhIGZvciBhIGluIHVsIGlmIGEgaXMgbm90IHVzcikgKQpkZWYgX1JlYWRCbG9iKGNv"
    "biwgc2l6ZSk6CiAgICAjc2l6ZSBjb21lcyBzdHJhaWdodCBvZmYgdGhlIHdpcmUsIHNvIGl0IGlz"
    "IG5laXRoZXIgdHJ1c3RlZCB0byBiZSBhIG51bWJlcgogICAgI25vciB0byBiZSBzYW5lOiBhIGNs"
    "aWVudCBjbGFpbWluZyBhIGh1Z2UgbGVuZ3RoIHVzZWQgdG8gbWFrZSB0aGUgc2VydmVyCiAgICAj"
    "YnVmZmVyIHVuYm91bmRlZGx5IChtZW1vcnkgZXhoYXVzdGlvbiksIGFuZCBhIGNsaWVudCB0aGF0"
    "IGRpc2Nvbm5lY3RlZAogICAgI21pZC1ibG9iIG1hZGUgcmVjdigpIHJldHVybiBiJycgZm9yZXZl"
    "ciAtIGEgMTAwJSBDUFUgYnVzeS1sb29wLCB0aGUgc2FtZQogICAgI2RlZmVjdCBhbHJlYWR5IGZp"
    "eGVkIGluIENvbm5lY3Rpb25IYW5kbGVyLl9yZWN2TW9yZSgpLgogICAgdHJ5OgogICAgICAgIHNp"
    "emUgPSBpbnQoc2l6ZSkKICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yKToKICAgICAg"
    "ICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmFkIGJsb2Igc2l6ZSB7c2l6ZSFyfScpCiAgICBpZiBz"
    "aXplIDwgMCBvciBzaXplID4gX01BWF9CTE9COgogICAgICAgIHJhaXNlIFByb3RvY29sRXJyb3Io"
    "ZidibG9iIHNpemUge3NpemV9IG91dCBvZiByYW5nZSAobWF4IHtfTUFYX0JMT0J9KScpCiAgICAj"
    "QSBibG9iIHJlYWQgYmxvY2tzIHRoaXMgY29ubmVjdGlvbidzIGVudGlyZSBoYW5kbGVyIHRocmVh"
    "ZC4gQW5ub3VuY2luZyBhCiAgICAjbGVuZ3RoIGFuZCB0aGVuIGdvaW5nIHF1aWV0IC0gYSB3ZWRn"
    "ZWQgY2xpZW50LCBhIGxpbmsgdGhhdCBkcm9wcGVkCiAgICAjd2l0aG91dCBhIHJlc2V0IC0gdXNl"
    "ZCB0byBibG9jayBpdCBmb3JldmVyOiB0aGUgdGhyZWFkIG5ldmVyIHJldHVybmVkLCBzbwogICAg"
    "I3RoZSBwbGF5ZXIncyBhY2NvdW50IHN0YXllZCBjbGFpbWVkIGFuZCBhbnkgcm9vbSB0aGV5IGhv"
    "c3RlZCBzdGF5ZWQKICAgICNsaXN0ZWQgd2l0aCBub3RoaW5nIGJlaGluZCBpdC4gVGhlIGlkbGUg"
    "dGltZW91dCBuZXZlciBhcHBsaWVkIGhlcmUsCiAgICAjYmVjYXVzZSBpdCBpcyBvbmx5IGNvbnN1"
    "bHRlZCBieSB0aGUgcmVhZCBsb29wIHRoaXMgY2FsbCBoYXMgc3RlcHBlZCBvdXQKICAgICNvZi4K"
    "ICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIF9CTE9CX1RJTUVPVVQKICAgIHdoaWxl"
    "IGxlbihjb24uZGF0YSkgPCBzaXplOgogICAgICAgIHJlbWFpbmluZyA9IGRlYWRsaW5lIC0gdGlt"
    "ZS5tb25vdG9uaWMoKQogICAgICAgIGlmIHJlbWFpbmluZyA8PSAwOgogICAgICAgICAgICByYWlz"
    "ZSBQcm90b2NvbEVycm9yKGYnYmxvYiBvZiB7c2l6ZX0gYnl0ZXMgbm90IGRlbGl2ZXJlZCB3aXRo"
    "aW4gJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYne19CTE9CX1RJTUVPVVR9cyAo"
    "e2xlbihjb24uZGF0YSl9IHJlY2VpdmVkKScpCiAgICAgICAgY29uLnJlcXVlc3Quc2V0dGltZW91"
    "dChyZW1haW5pbmcpCiAgICAgICAgdHJ5OgogICAgICAgICAgICBjaHVuayA9IGNvbi5yZXF1ZXN0"
    "LnJlY3YoUkVDVl9CVUZfTEVOKQogICAgICAgIGV4Y2VwdCBUaW1lb3V0RXJyb3I6CiAgICAgICAg"
    "ICAgIGNvbnRpbnVlICNkZWFkbGluZSBpcyByZS1jaGVja2VkIGF0IHRoZSB0b3Agb2YgdGhlIGxv"
    "b3AKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAgICAgIHJhaXNlIENvbm5lY3Rpb25SZXNl"
    "dEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGJsb2IgcmVhZCcpCiAgICAgICAgY29uLmRhdGEg"
    "Kz0gY2h1bmsKICAgIGJsYnVmID0gY29uLmRhdGFbMDpzaXplXQogICAgY29uLmRhdGEgPSBjb24u"
    "ZGF0YVtzaXplOl0KICAgIHJldHVybiBibGJ1ZgoKI0NvbW1hbmQgZnVuY3Rpb25zCmRlZiBfbm9w"
    "KG1kLHVzcixyZXMpOgogICAgcmV0dXJuIE5vbmUKZGVmIF91cGRoZXJvcG9zKG1kLHVzcixyZXMp"
    "OgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBOb25lICNu"
    "b3QgaW4gYSBnYW1lIGNoYW5uZWwsIGlnbm9yZQogICAgIyAieHh4eCN5eXl5IiByZXNwICJVSUQj"
    "eHh4eCN5eXl5IiAtIHRoZSBjbGllbnQgc2VuZHMgZWl0aGVyIGZvcm0sIGJ1dAogICAgIyB1cGRh"
    "dGVQb3MoKSB1bmNvbmRpdGlvbmFsbHkgcHJlZml4ZXMgdGhlIHNlbmRlcidzIGlkIHdoZW4gaXQg"
    "ZmFucyB0aGUKICAgICMgcG9zaXRpb24gb3V0LiBTdG9yaW5nIHRoZSByYXcgZmllbGQgbWVhbnQg"
    "dGhlIHNlY29uZCBmb3JtIHdlbnQgYmFjayBvdXQKICAgICMgYXMgIlVJRCNVSUQjeHh4eCN5eXl5"
    "Iiwgd2hpY2ggbm8gY2xpZW50IGNhbiBtYXRjaCB0byBhIHBsYXllcjogdGhhdAogICAgIyBoZXJv"
    "J3MgbWFya2VyIHRoZW4gc3RheWVkIHdoZXJldmVyIGl0IHdhcyBsYXN0IHN1Y2Nlc3NmdWxseSBw"
    "YXJzZWQgd2hpbGUKICAgICMgdGhlIHBsYXllciBhY3R1YWxseSB3YWxrZWQgYXdheS4gS2VlcCBv"
    "bmx5IHRoZSB0cmFpbGluZyBjb29yZGluYXRlIHBhaXIKICAgICMgc28gZXhhY3RseSBvbmUgaWQg"
    "aXMgcHJlc2VudCBvbiB0aGUgd2lyZSByZWdhcmRsZXNzIG9mIHdoYXQgd2FzIHNlbnQuCiAgICB1"
    "c3IudXNlci5wb3NkYXRhID0gJyMnLmpvaW4ocmVzWzFdLnNwbGl0KCcjJylbLTI6XSkKICAgIHVz"
    "ci51c2VyLmdhbWVjaGFubmVsLmRpcnR5ID0gVHJ1ZQogICAgdXNyLnVzZXIucG9zY2hhbmdlZCA9"
    "IFRydWUKICAgIHJldHVybiBOb25lICNubyByZXNwb25zZQpkZWYgX3NldHBsYXllcmRhdGEobWQs"
    "dXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1c3IsIHJlc1szXSkKICAgICNUT0RPIENIRUNL"
    "IHBlcm1pc3Npb25zIGZvciBzZXREYXRhKHNlbGYgb3Igb3RoZXIpCiAgICBpZiByZXNbMV0gPT0g"
    "dXNyLnVzZXIubmFtZToKICAgICAgICBHREguc2V0UGxheWVyRGF0YShyZXNbMV0sIHJlc1syXSwg"
    "cGQpCiAgICAjVE9ETyBoYW5kbGUgcmVtYWluaW5nIHZhbHVlcwogICAgI3Jlc1t4XToKICAgICMw"
    "OiAvc2V0cGxheWVyZGF0YQogICAgIzE6IG5hbWUKICAgICMyOiBmb3JtCiAgICAjMzogYmxvYnNp"
    "emUKICAgICM0OiB1bmtub3duIChwb2ludHM/KQogICAgIzU6IHVua25vd24sIDEgKGJvb2w/KQog"
    "ICAgcmV0dXJuIE5vbmUKZGVmIF9nZXRwbGF5ZXJkYXRhKG1kLHVzcixyZXMpOgogICAgI1RPRE8g"
    "Y2hlY2sgcGVybWlzc2lvbiBmb3IgZ2V0RGF0YShzZWxmIG9yIG90aGVyKQogICAgaWYgcmVzWzFd"
    "ID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgcGQgPSBHREguZ2V0UGxheWVyRGF0YShyZXNbMV0s"
    "IHJlc1syXSkKICAgICAgICAjcHJpbnQoJ09idGFpbmVkIFBsYXllcmRhdGEnLCBsZW4ocGQpKQog"
    "ICAgICAgIHJldHVybiBfZW0oZicvZ2V0cGxheWVyZGF0YSAie3Jlc1sxXX0iICJ7cmVzWzJdfSIg"
    "e2xlbihwZCl9JykrcGQKICAgICNwcmludCgnQWNjZXNzIEVycm9yJyx1c3IudXNlci5uYW1lLCAn"
    "Q2FuXCd0IGdldCBwbGF5ZXJkYXRhIGZvcicscmVzWzFdKQogICAgcmV0dXJuIE5vbmUKZGVmIF9s"
    "ZWF2ZWdhbWVjaGFubmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9IHVzci51c2VyLmdhbWVjaGFu"
    "bmVsCiAgICBpZiBjaG5sOgogICAgICAgIGNobmwubGVhdmVDaGFubmVsKHVzcikKICAgIHJldHVy"
    "biB1c3Iuc2VydmVyLnN0YXRlLmVudW1lcmF0ZUdDKCkKZGVmIF9yZXF1ZXN0am9pbmdhbWVjaGFu"
    "bmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9IHVzci5zZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxz"
    "LmdldChyZXNbMV0pCiAgICBpZiBjaG5sIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIF9lbShmJy9y"
    "ZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIgIjAiJykgI3Vua25vd24gY2hhbm5lbAog"
    "ICAgI1RPRE8gY2hlY2sgcGVybWlzc2lvbnM/CiAgICBpZiBjaG5sLnJlcXVlc3RKb2luKHVzcik6"
    "CiAgICAgICAgcmV0dXJuIF9lbShmJy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIg"
    "IjEiJykKICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0i"
    "ICIwIicpCmRlZiBfam9pbmdhbWVjaGFubmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9IHVzci5z"
    "ZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxzLmdldChyZXNbMV0pCiAgICBpZiBjaG5sIGlzIE5vbmU6"
    "CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gY2hhbm5lbCwgaWdub3JlCiAgICBpZiBsZW4o"
    "cmVzKT4yOgogICAgICAgIHVzci51c2VyLnBvc2RhdGEgPSAnIycuam9pbihyZXNbMl0uc3BsaXQo"
    "JyMnKVstMjpdKQogICAgcmV0dXJuIGNobmwuam9pbkNoYW5uZWwodXNyLCByZXNbMV0pCmRlZiBf"
    "c2V0dXNlcmhlcm9kYXRhKG1kLHVzcixyZXMpOgogICAgcGQgPSBfUmVhZEJsb2IodXNyLCByZXNb"
    "Ml0pCiAgICB1c3IudXNlci5oZXJvZGF0YSA9IHBkCiAgICBpZiB1c3IudXNlci5nYW1lY2hhbm5l"
    "bDoKICAgICAgICBtc2cgPSB1c3IudXNlci5nZXRHQ1Vtc2coKQogICAgICAgIHRnID0gX3dvVXNl"
    "cih1c3IudXNlci5nYW1lY2hhbm5lbC51c2VybGlzdCwgdXNyKQogICAgICAgIG1kLmFkZCh7J3Rh"
    "cmdldCc6dGcsJ21lc3NhZ2UnOm1zZ30pCiAgICByZXR1cm4gTm9uZQpkZWYgX3NlbmQobWQsdXNy"
    "LHJlcyk6CiAgICAjVE9ETyBjb25zaWRlciBzcGVjaWFsIGNoYXQgY29tbWFuZHMgaGVyZQogICAg"
    "aWYgbm90IHVzci51c2VyLmNoYXRjaGFubmVsOgogICAgICAgIHJldHVybiBOb25lCiAgICBpZiBs"
    "ZW4ocmVzKTwyOgogICAgICAgIHJldHVybiBOb25lCiAgICB0ZXh0ID0gc2FuaXRpemVUZXh0KHJl"
    "c1sxXSkKICAgIGlmIG5vdCB0ZXh0OgogICAgICAgIHJldHVybiBOb25lCiAgICB1bCA9IHVzci51"
    "c2VyLmNoYXRjaGFubmVsCiAgICBtZC5hZGQoeyd0YXJnZXQnOnVsLCdtZXNzYWdlJzpfZW0oZicv"
    "c2VuZCAie3Vzci51c2VyLm5hbWV9IiAie3RleHR9IicpfSkKICAgIHJldHVybiBOb25lCmRlZiBf"
    "Z2V0Z3VpbGRyYW5rcG9pbnRzKG1kLHVzcixyZXMpOgogICAgKGEsYixjLGQpID0gX2dycCgpCiAg"
    "ICByZXR1cm4gX2VtKGYnL2dldGd1aWxkcmFua3BvaW50cyAie2F9IiAie2J9IiAie2N9IiAie2R9"
    "IicpCgojIyBHVUlMRFMKI0d1aWxkIGNyZWF0aW9uIGRpZCBub3RoaW5nIGF0IGFsbCBiZWZvcmUg"
    "dGhpczogdGhlcmUgd2FzIG5vIC9jcmVhdGVndWlsZCAob3IKI2FueXRoaW5nIGVsc2UgZ3VpbGQt"
    "cmVsYXRlZCkgaW4gX0NPTU1BTkRTLCBzbyB0aGUgY2xpZW50J3MgcmVxdWVzdCBmZWxsCiN0aHJv"
    "dWdoIHRvIHRoZSAiVW5rbm93biBDb21tYW5kIiBicmFuY2ggb2YgQ29tbWFuZFBhcnNlci5wYXJz"
    "ZSBhbmQgd2FzCiNkcm9wcGVkLiBUaGUgY2xpZW50IGdvdCBubyByZXBseSwgbm8gZXJyb3IsIGFu"
    "ZCBubyBndWlsZC4KI05PVEUgT04gQ09NTUFORCBOQU1FUzogdGhlIGV4YWN0IHdpcmUgbmFtZXMg"
    "dGhlIHJldGFpbCBjbGllbnQgdXNlcyBmb3IgdGhlCiNndWlsZCBVSSBhcmUgbm90IGRvY3VtZW50"
    "ZWQgYW55d2hlcmUgd2UgaGF2ZS4gVGhlIGhhbmRsZXJzIGJlbG93IGFyZQojcmVnaXN0ZXJlZCB1"
    "bmRlciBldmVyeSBzcGVsbGluZyB0aGF0IGZpdHMgdGhpcyBwcm90b2NvbCdzIGNvbnZlbnRpb25z"
    "LCBhbGwKI3JvdXRlZCB0byB0aGUgc2FtZSBpbXBsZW1lbnRhdGlvbiwgc28gd2hpY2hldmVyIG9u"
    "ZSB0aGUgY2xpZW50IGFjdHVhbGx5CiNzZW5kcyBpcyBzZXJ2ZWQuIHBhcnNlKCkgbm93IGxvZ3Mg"
    "dGhlIHJhdyB0ZXh0IG9mIGFueXRoaW5nIHN0aWxsIHVubWF0Y2hlZCwKI3doaWNoIGlzIGhvdyB0"
    "byBjb25maXJtL3RyaW0gdGhpcyBsaXN0IGZyb20gYSByZWFsIHNlc3Npb24ncyBsb2cuCmRlZiBf"
    "dGVzdGNyZWF0ZWd1aWxkKG1kLHVzcixyZXMpOgogICAgI0NvbmZpcm1lZCBmcm9tIGEgbGl2ZSBj"
    "bGllbnQgY2FwdHVyZTogb3BlbmluZyB0aGUgZ3VpbGQgc2NyZWVuIHNlbmRzCiAgICAjL2d1aWxk"
    "c2xhZGRlciwgYW5kIHR5cGluZyBhIG5hbWUgYW5kIHByZXNzaW5nIGNyZWF0ZSBzZW5kcwogICAg"
    "Iy90ZXN0Y3JlYXRlZ3VpbGQgIjxuYW1lPiIuIFRoZSBjbGllbnQgdGhlbiB3YWl0cyBmb3IgdGhl"
    "IHNlcnZlciB0byBzYXkKICAgICN3aGV0aGVyIHRoYXQgbmFtZSBjYW4gYmUgdXNlZCAtIHdpdGgg"
    "bm8gYW5zd2VyIGl0IHdhaXRzIGZvcmV2ZXIsIHdoaWNoIGlzCiAgICAjd2hhdCB0aGUgImd1aWxk"
    "IGNyZWF0aW9uIGhhbmdzIiByZXBvcnQgd2FzLiBFdmVyeSBndWlsZCBjb21tYW5kIG5hbWUKICAg"
    "ICNndWVzc2VkIGJlZm9yZSB0aGlzIGNhcHR1cmUgKCAvY3JlYXRlZ3VpbGQsIC9qb2luZ3VpbGQs"
    "IC4uLiApIHdhcyB3cm9uZzsKICAgICN0aGlzIG9uZSBjb21lcyBmcm9tIHRoZSB3aXJlLgogICAg"
    "bmFtZSA9IHNhbml0aXplVGV4dChyZXNbMV0pLnN0cmlwKCkKICAgIGZyZWUgPSAxIGlmIEdESC5n"
    "dWlsZE5hbWVGcmVlKG5hbWUpIGVsc2UgMAogICAgcHJpbnQoZidbTG9iYnldIHt1c3IudXNlci5u"
    "YW1lfSBjaGVja2VkIGd1aWxkIG5hbWUgIntuYW1lfSI6ICcKICAgICAgICAgIGYneyJhdmFpbGFi"
    "bGUiIGlmIGZyZWUgZWxzZSAicmVqZWN0ZWQifScpCiAgICAjRWNoby1wbHVzLWZsYWcsIHRoZSBz"
    "YW1lIHNoYXBlIHRoZSBjbGllbnQgYWxyZWFkeSBhY2NlcHRzIGZyb20KICAgICMvcmVxdWVzdGpv"
    "aW5nYW1lY2hhbm5lbCAoIjEiIGdvIGFoZWFkIC8gIjAiIG5vKS4KICAgIHJldHVybiBfZW0oZicv"
    "dGVzdGNyZWF0ZWd1aWxkICJ7bmFtZX0iICJ7ZnJlZX0iJykKZGVmIF9ndWlsZHNsYWRkZXIobWQs"
    "dXNyLHJlcyk6CiAgICAjU2VudCB3aGVuIHRoZSBndWlsZCBzY3JlZW4gb3BlbnMuIFRoZSBsYXlv"
    "dXQgb2YgYW4gaW5kaXZpZHVhbCBsYWRkZXIKICAgICNlbnRyeSBpcyBub3Qga25vd24sIGFuZCB0"
    "aGlzIGNsaWVudCBpcyBmcmFnaWxlIGVub3VnaCB0aGF0IGludmVudGluZyBvbmUKICAgICNyaXNr"
    "cyB0YWtpbmcgaXQgZG93biAtIHNvIHRoZSBhbnN3ZXIgaXMgYW4gaG9uZXN0IGVtcHR5IGxhZGRl"
    "ciwgd2hpY2ggaXMKICAgICNhbHNvIHRoZSB0cnV0aGZ1bCBvbmUgdW50aWwgZ3VpbGRzIGNhbiBh"
    "Y3R1YWxseSBiZSBjcmVhdGVkLiBUaGUgY291bnQKICAgICNjb21lcyBsYXN0LCBtYXRjaGluZyAv"
    "am9pbmdhbWVjaGFubmVsJ3MgZWNoby1wbHVzLWNvdW50IHJlcGx5LgogICAgcGFnZSA9IHNhbml0"
    "aXplVGV4dChyZXNbMV0pIGlmIGxlbihyZXMpID4gMSBlbHNlICcxJwogICAgcmV0dXJuIF9lbShm"
    "Jy9ndWlsZHNsYWRkZXIgIntwYWdlfSIgIjAiJykKZGVmIF9qb2luZ3VpbGQobWQsdXNyLHJlcyk6"
    "CiAgICAjQ2FwdHVyZWQgZnJvbSB0aGUgcmV0YWlsIGNsaWVudDogYWZ0ZXIgL3Rlc3RjcmVhdGVn"
    "dWlsZCBhbnN3ZXJzIHRoYXQgYQogICAgI25hbWUgaXMgZnJlZSwgdGhlIGNsaWVudCBjcmVhdGVz"
    "IHRoZSBndWlsZCBieSBzZW5kaW5nCiAgICAjL2pvaW5ndWlsZCAiPG5hbWU+IiAiMSIgIjEiLiBT"
    "byB0aGlzIG9uZSBjb21tYW5kIGNvdmVycyBib3RoIGNyZWF0aW5nIGFuZAogICAgI2pvaW5pbmcs"
    "IGFuZCB3aGljaCBpdCBpcyBmb2xsb3dzIGZyb20gd2hldGhlciB0aGUgZ3VpbGQgYWxyZWFkeSBl"
    "eGlzdHMgLQogICAgI3RoZSB0cmFpbGluZyBmbGFncyBhcmUgbm90IG5lZWRlZCB0byB0ZWxsIHRo"
    "ZW0gYXBhcnQuIEFuc3dlcmluZyBub3RoaW5nCiAgICAjaGVyZSBpcyB3aGF0IGxlZnQgdGhlIGd1"
    "aWxkIGRpYWxvZyBzcGlubmluZy4KICAgIG5hbWUgPSBzYW5pdGl6ZVRleHQocmVzWzFdKS5zdHJp"
    "cCgpCiAgICBpZiBHREguZ3VpbGRFeGlzdHMobmFtZSk6CiAgICAgICAgZXJyID0gR0RILmpvaW5H"
    "dWlsZChuYW1lLCB1c3IudXNlci5uYW1lKQogICAgICAgIGFjdGlvbiA9ICdqb2luZWQnCiAgICBl"
    "bHNlOgogICAgICAgIGVyciA9IEdESC5jcmVhdGVHdWlsZChuYW1lLCB1c3IudXNlci5uYW1lKSAj"
    "dmFsaWRhdGVzIHRoZSBuYW1lIGl0c2VsZgogICAgICAgIGFjdGlvbiA9ICdmb3VuZGVkJwogICAg"
    "aWYgZXJyOgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3Ige2Vycn0gIntuYW1lfSInKQogICAg"
    "I0Nhbm9uaWNhbCBzcGVsbGluZyBmcm9tIHRoZSBkYXRhYmFzZSwgd2hpY2ggbWF5IGRpZmZlciBp"
    "biBjYXNlIGZyb20gd2hhdAogICAgI3dhcyB0eXBlZC4KICAgIG5hbWUgPSBHREguZ2V0R3VpbGRO"
    "YW1lKHVzci51c2VyLm5hbWUpIG9yIG5hbWUKICAgIHVzci51c2VyLmd1aWxkID0gc2FuaXRpemVU"
    "ZXh0KG5hbWUpCiAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2VyLm5hbWV9IHthY3Rpb259IGd1"
    "aWxkICJ7bmFtZX0iJykKICAgICNSZS1hbm5vdW5jZSB0aGUgcGxheWVyIHRvIHRoZWlyIHRvd24g"
    "c28gdGhlIG90aGVycyBwaWNrIHVwIHRoZSBuZXcgdGFnCiAgICAjd2l0aG91dCByZWxvZ2dpbmcu"
    "IFRoaXMgcmV1c2VzICRnYW1lY2hhbm5lbHVzZXIgLSBhIG1lc3NhZ2UgZm9ybWF0IHRoZQogICAg"
    "I2NsaWVudCBkZW1vbnN0cmFibHkgYWNjZXB0cyAtIHJhdGhlciB0aGFuIGludmVudGluZyBhIGd1"
    "aWxkLXNwZWNpZmljIG9uZS4KICAgIGNobmwgPSB1c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYg"
    "Y2hubDoKICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOl93b1VzZXIoY2hubC51c2VybGlzdCwgdXNy"
    "KSwKICAgICAgICAgICAgICAgICdtZXNzYWdlJzp1c3IudXNlci5nZXRHQ1Vtc2coKX0pCiAgICAj"
    "RWNobyBwbHVzIG1lbWJlciBjb3VudCwgdGhlIHNoYXBlIC9qb2luZ2FtZWNoYW5uZWwgYWxyZWFk"
    "eSByZXBsaWVzIHdpdGguCiAgICByZXR1cm4gX2VtKGYnL2pvaW5ndWlsZCAie25hbWV9IiAie2xl"
    "bihHREguZ2V0R3VpbGRNZW1iZXJzKG5hbWUpKX0iJykKZGVmIF9yZXF1ZXN0Y3JlYXRlZ2FtZSht"
    "ZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1"
    "cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBjaGFubmVsIC0gdXNlZCB0byByYWlzZSBBdHRyaWJ1dGVF"
    "cnJvciBvbgogICAgICAgICAgICAgICAgICAgICNOb25lIGFuZCBraWxsIHRoZSBjb25uZWN0aW9u"
    "J3MgaGFuZGxlciB0aHJlYWQKICAgIHJldHVybiB1c3IudXNlci5nYW1lY2hhbm5lbC5yZXF1ZXN0"
    "Q3JlYXRlR2FtZSh1c3IsIHJlc1sxXSkKZGVmIF9jcmVhdGVHYW1lKG1kLHVzcixyZXMpOgogICAg"
    "aWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBOb25lICNzZWUgX3Jl"
    "cXVlc3RjcmVhdGVnYW1lCiAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZWNoYW5uZWwuY3JlYXRlR2Ft"
    "ZShyZXNbMV0sIHVzciwgcmVzWzJdLCByZXNbM10sIHJlc1s0XSwgcmVzWzVdLCByZXNbNl0sIHJl"
    "c1s3XSwgcmVzWzhdLCByZXNbOV0pCmRlZiBfc3RvcGdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1"
    "c3IudXNlci5nYW1lOgogICAgICAgIHJldHVybiB1c3IudXNlci5nYW1lLnJlbW92ZSh1c3IpCiAg"
    "ICAjcHJpbnQoJ1VzZXIgaXMgbm90IGluIGEgZ2FtZScpCiAgICByZXR1cm4gTm9uZQpkZWYgX3N0"
    "YXJ0aW5nZ2FtZShtZCx1c3IscmVzKToKICAgIGlmIHVzci51c2VyLmdhbWU6CiAgICAgICAgcmV0"
    "dXJuIHVzci51c2VyLmdhbWUuc3RhcnRHYW1lKHVzcikKICAgIHJldHVybiBOb25lICNUT0RPIHdo"
    "YXQgZG9lcyB0aGlzIGV2ZW4gZG8/CmRlZiBfc3RhcnRnYW1lKG1kLHVzcixyZXMpOgogICAgI1RP"
    "RE8gaGFuZGxlIHByb3Blcmx5CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHBhc3MKICAg"
    "IHJldHVybiBOb25lCmRlZiBfZ2FtZWNvbW1hbmR0b3VzZXIobWQsdXNyLHJlcyk6CiAgICBkYXQg"
    "PSBfUmVhZEJsb2IodXNyLCByZXNbMl0pCiAgICB0Y29uID0gdXNyLnNlcnZlci5nZXRQbGF5ZXIo"
    "cmVzWzFdKQogICAgI0FsbG93IGNvbW1hbmRzIHRvIGFueSBjb25uZWN0ZWQgcGxheWVyLCByZWdh"
    "cmRsZXNzIG9mIHN0YXRlLCB0byBzdXBwb3J0IG1vZGRlZCB1c2VzCiAgICBpZiBub3QgdGNvbjoK"
    "ICAgICAgICAjcHJpbnQoJ1BsYXllcjonLHJlc1sxXSwnZG9lcyBub3QgZXhpc3Q/JykKICAgICAg"
    "ICByZXR1cm4gTm9uZQogICAgI1RPRE8gY29uc2lkZXIgb3B0aW1pc2luZyB0aGlzIGNvbW1hbmQg"
    "aW4gcGFydGljdWxhcgogICAgZnVsbXNnID0gX2VtKGYnL2dhbWVjb21tYW5kdG91c2VyICJ7dXNy"
    "LnVzZXIubmFtZX0iICJ7bGVuKGRhdCl9IicpK2RhdAogICAgI1N0cmFpZ2h0IG9udG8gdGhlIHJl"
    "Y2lwaWVudCdzIG93biBvdXRib3VuZCBxdWV1ZSBpbnN0ZWFkIG9mIHZpYSB0aGUKICAgICNzZXJ2"
    "ZXItd2lkZSBNZXNzYWdlRGlzdHJpYnV0b3IuIFRoaXMgaXMgdGhlIGNvbW1hbmQgdGhhdCBjYXJy"
    "aWVzIHRoZQogICAgI2FjdHVhbCBpbi1nYW1lIHRyYWZmaWMgYmV0d2VlbiB0d28gcGxheWVycywg"
    "aXQgYWx3YXlzIGhhcyBleGFjdGx5IG9uZQogICAgI3JlY2lwaWVudCwgYW5kIHNlbmQoKSBpcyBq"
    "dXN0IGEgcXVldWUgcHV0IC0gc28gdGhlIGRpc3RyaWJ1dG9yIGhvcCBib3VnaHQKICAgICNub3Ro"
    "aW5nIGJ1dCBsYXRlbmN5LiBXb3JzZSwgdGhhdCBzaW5nbGUgZGlzdHJpYnV0b3IgdGhyZWFkIGlz"
    "IHNoYXJlZCBieQogICAgI2V2ZXJ5IGNvbm5lY3Rpb24gb24gdGhlIHNlcnZlcjogb25lIHNsb3cg"
    "ZmFuLW91dCAoYSBwb3NpdGlvbiBicm9hZGNhc3QgdG8KICAgICNhIGZ1bGwgdG93biwgYSBoZXJv"
    "ZGF0YSBibG9iKSBxdWV1ZWQgYWhlYWQgb2YgYSBnYW1lIGNvbW1hbmQgZGVsYXllZCBpdAogICAg"
    "I2ZvciBldmVyeW9uZS4gRGlyZWN0IGhhbmQtb2ZmIHJlbW92ZXMgYm90aCB0aGUgZXh0cmEgdGhy"
    "ZWFkIHdha2UtdXAgYW5kCiAgICAjdGhhdCBoZWFkLW9mLWxpbmUgYmxvY2tpbmcsIGFuZCByZWxh"
    "eSBvcmRlciBiZXR3ZWVuIGFueSBnaXZlbiBwYWlyIG9mCiAgICAjcGxheWVycyBpcyBzdGlsbCBw"
    "cmVzZXJ2ZWQgYmVjYXVzZSB0aGV5IGFsbCB0YWtlIHRoaXMgc2FtZSBwYXRoLgogICAgdGNvbi5z"
    "ZW5kKGZ1bG1zZykKICAgIHJldHVybiBOb25lCmRlZiBfam9pbmdhbWUobWQsdXNyLHJlcyk6CiAg"
    "ICBpZiBub3QgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJv"
    "ciB1bmtub3duR2FtZSAie3Jlc1sxXX0iJykgI25vdCBpbiBhIGdhbWUgY2hhbm5lbAogICAgZ20g"
    "PSB1c3IudXNlci5nYW1lY2hhbm5lbC5nYW1lcy5nZXQocmVzWzFdLE5vbmUpCiAgICBpZiBnbSA9"
    "PSBOb25lOgogICAgICAgICNBbnN3ZXIsIGRvbid0IGlnbm9yZTogdGhlIGNsaWVudCBpcyBzaXR0"
    "aW5nIG9uIGEgImNvbm5lY3RpbmciIGRpYWxvZwogICAgICAgICN0aGF0IG9ubHkgYSByZXBseSBk"
    "aXNtaXNzZXMuIEhhcHBlbnMgd2hlbmV2ZXIgdGhlIHJvb20gaXMgdG9ybiBkb3duCiAgICAgICAg"
    "I2JldHdlZW4gdGhlIHBsYXllciBzZWVpbmcgaXQgaW4gdGhlIGxpc3QgYW5kIGNsaWNraW5nIGl0"
    "LgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5rbm93bkdhbWUgIntyZXNbMV19IicpCiAg"
    "ICAjVGhlIHBhc3N3b3JkIGFyZ3VtZW50IGlzIGFic2VudCB3aGVuIHRoZSByb29tIGhhcyBub25l"
    "IC0gc2VlIHRoZSBhcml0eQogICAgI25vdGUgb24gX0NPTU1BTkRTLgogICAgcmV0dXJuIGdtLmFk"
    "ZFVzZXIodXNyLCByZXNbMl0gaWYgbGVuKHJlcyk+MiBlbHNlICcnKQpkZWYgX3dob2lzKG1kLHVz"
    "cixyZXMpOgogICAgaWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9uZQogICAgdGFyZ2V0"
    "ID0gcmVzWzFdCiAgICBpbmZvID0gR0RILmdldFdob2lzKHRhcmdldCkKICAgIGlmIGluZm8gaXMg"
    "Tm9uZToKICAgICAgICByZXR1cm4gTm9uZSAjdW5rbm93biB1c2VyCiAgICB0Y29uID0gdXNyLnNl"
    "cnZlci5nZXRQbGF5ZXIodGFyZ2V0KQogICAgdG93biA9IHRjb24udXNlci5nYW1lY2hhbm5lbC5u"
    "YW1lIGlmICh0Y29uIGFuZCB0Y29uLnVzZXIuZ2FtZWNoYW5uZWwpIGVsc2UgJycKICAgIGNoYXRj"
    "aGFubmVsID0gJycKICAgIGlmIHRjb24gYW5kIHRjb24udXNlci5jaGF0Y2hhbm5lbDoKICAgICAg"
    "ICBmb3IgY2huIGluIHVzci5zZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxzLnZhbHVlcygpOgogICAg"
    "ICAgICAgICBmb3IgY25hbWUsIHVsaXN0IGluIGNobi5jaGF0Q2hhbm5lbHMuaXRlbXMoKToKICAg"
    "ICAgICAgICAgICAgIGlmIHVsaXN0IGlzIHRjb24udXNlci5jaGF0Y2hhbm5lbDoKICAgICAgICAg"
    "ICAgICAgICAgICBjaGF0Y2hhbm5lbCA9IGNuYW1lCiAgICBndWlsZCA9IHNhbml0aXplVGV4dChH"
    "REguZ2V0R3VpbGROYW1lKHRhcmdldCkpCiAgICByZXR1cm4gX2VtKAogICAgICAgIGYnL3dob2lz"
    "ICJ7dGFyZ2V0fSIgIntndWlsZH0iICJ7c2FuaXRpemVUZXh0KHRvd24pfSIgIntzYW5pdGl6ZVRl"
    "eHQoY2hhdGNoYW5uZWwpfSIgJwogICAgICAgIGYnIntzYW5pdGl6ZVRleHQoaW5mb1siZW1haWwi"
    "XSl9IiAie3Nhbml0aXplVGV4dChpbmZvWyJsb2NhdGlvbiJdKX0iICcKICAgICAgICBmJ3tpbmZv"
    "WyJhZ2UiXX0ge2luZm9bImdlbmRlciJdfSAie3Nhbml0aXplVGV4dChpbmZvWyJkZXNjcmlwdGlv"
    "biJdKX0iJwogICAgKQpkZWYgX3VwZGF0ZShtZCx1c3IscmVzKToKICAgICMvdXBkYXRlICJuYW1l"
    "IiAiZW1haWwiICJsb2NhdGlvbiIgImFnZSIgImdlbmRlciIgImRlc2NyaXB0aW9uIgogICAgaWYg"
    "bGVuKHJlcyk8NjoKICAgICAgICByZXR1cm4gTm9uZQogICAgaWYgcmVzWzFdICE9IHVzci51c2Vy"
    "Lm5hbWU6CiAgICAgICAgcmV0dXJuIE5vbmUgI2NhbiBvbmx5IHVwZGF0ZSBvd24gd2hvaXMgaW5m"
    "bwogICAgZW1haWwgPSBzYW5pdGl6ZVRleHQocmVzWzJdKQogICAgbG9jYXRpb24gPSBzYW5pdGl6"
    "ZVRleHQocmVzWzNdKQogICAgYWdlID0gcmVzWzRdCiAgICBnZW5kZXIgPSByZXNbNV0KICAgIGRl"
    "c2NyaXB0aW9uID0gc2FuaXRpemVUZXh0KHJlc1s2XSkgaWYgbGVuKHJlcyk+NiBlbHNlICcnCiAg"
    "ICBHREgudXBkYXRlV2hvaXModXNyLnVzZXIubmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdl"
    "bmRlciwgZGVzY3JpcHRpb24pCiAgICByZXR1cm4gTm9uZSAjc2VydmVyIHNlbmRzIG5vIHJlc3Bv"
    "bnNlLCBwZXIgcHJvdG9jb2wgZG9jCgpfUkVfQ01EID0gcmUuY29tcGlsZShyJyg/OiIoW14iXSop"
    "Iil8KFteXHNdKyknKQojY29tbWFuZCAtPiAoaGFuZGxlciwgbWluaW11bSBhcmd1bWVudCBjb3Vu"
    "dCAqZXhjbHVkaW5nKiB0aGUgY29tbWFuZCB3b3JkKS4KI1RoZSBjb3VudCBpcyBlbmZvcmNlZCBv"
    "bmNlLCBjZW50cmFsbHksIGluIHBhcnNlKCk6IGV2ZXJ5IGhhbmRsZXIgaW5kZXhlcyBpbnRvCiNy"
    "ZXNbXSBwb3NpdGlvbmFsbHksIHNvIGEgY2xpZW50IHNlbmRpbmcgYSBjb21tYW5kIHdpdGggZmV3"
    "ZXIgYXJndW1lbnRzIHRoYW4KI2V4cGVjdGVkIHVzZWQgdG8gcmFpc2UgSW5kZXhFcnJvciBhbmQg"
    "dGVhciBkb3duIGl0cyBvd24gY29ubmVjdGlvbiB0aHJlYWQuCiNEZWNsYXJpbmcgdGhlIGFyaXR5"
    "IGhlcmUga2VlcHMgdGhhdCBjaGVjayBpbiBvbmUgcGxhY2UgaW5zdGVhZCBvZiByZXBlYXRpbmcg"
    "YQojbGVuKHJlcykgZ3VhcmQgYXQgdGhlIHRvcCBvZiBmaWZ0ZWVuIGhhbmRsZXJzLgpfQ09NTUFO"
    "RFMgPSB7CiAgICAnL25vcCc6ICAgICAgICAgICAgICAgICAgICAoX25vcCwgMCksCiAgICAnL2xl"
    "YXZlZ2FtZWNoYW5uZWwnOiAgICAgICAoX2xlYXZlZ2FtZWNoYW5uZWwsIDApLAogICAgJy9yZXF1"
    "ZXN0am9pbmdhbWVjaGFubmVsJzogKF9yZXF1ZXN0am9pbmdhbWVjaGFubmVsLCAxKSwKICAgICNB"
    "cml0eSAxLCBub3QgMjogdGhlIHBvc2l0aW9uIGFyZ3VtZW50IGlzIG9wdGlvbmFsICh0aGUgY2xp"
    "ZW50IG9taXRzIGl0CiAgICAjd2hlbiBpdCBoYXMgbm8gbGFzdC1rbm93biBwb3NpdGlvbiB5ZXQs"
    "IGUuZy4gdGhlIHZlcnkgZmlyc3QgdG93biBlbnRyeQogICAgI2FmdGVyIGxvZ2luKS4gUmVxdWly"
    "aW5nIGl0IG1hZGUgcGFyc2UoKSBkcm9wIHRoZSBjb21tYW5kIHNpbGVudGx5LCB3aGljaAogICAg"
    "I3RoZSBjbGllbnQgZXhwZXJpZW5jZXMgYXMgYSB0b3duIGl0IGNhbiBuZXZlciBmaW5pc2ggbG9h"
    "ZGluZy4KICAgICcvam9pbmdhbWVjaGFubmVsJzogICAgICAgIChfam9pbmdhbWVjaGFubmVsLCAx"
    "KSwKICAgICcvdXBkaGVyb3Bvcyc6ICAgICAgICAgICAgIChfdXBkaGVyb3BvcywgMSksCiAgICAn"
    "L3NlbmQnOiAgICAgICAgICAgICAgICAgICAoX3NlbmQsIDEpLAogICAgJy9nZXRndWlsZHJhbmtw"
    "b2ludHMnOiAgICAgKF9nZXRndWlsZHJhbmtwb2ludHMsIDApLAogICAgJy9yZXF1ZXN0Y3JlYXRl"
    "Z2FtZSc6ICAgICAgKF9yZXF1ZXN0Y3JlYXRlZ2FtZSwgMSksCiAgICAnL2NyZWF0ZWdhbWUnOiAg"
    "ICAgICAgICAgICAoX2NyZWF0ZUdhbWUsIDkpLAogICAgJy9zdG9wZ2FtZSc6ICAgICAgICAgICAg"
    "ICAgKF9zdG9wZ2FtZSwgMCksCiAgICAnL2xlYXZlZ2FtZSc6ICAgICAgICAgICAgICAoX3N0b3Bn"
    "YW1lLCAwKSwjVE9ETyBmaXggZm9yIG11bHRpcGxlIHVzZXJzPwogICAgJy9zdGFydGluZ2dhbWUn"
    "OiAgICAgICAgICAgKF9zdGFydGluZ2dhbWUsIDApLAogICAgJy9zdGFydGdhbWUnOiAgICAgICAg"
    "ICAgICAgKF9zdGFydGdhbWUsIDApLAogICAgJy9nZXRwbGF5ZXJkYXRhJzogICAgICAgICAgKF9n"
    "ZXRwbGF5ZXJkYXRhLCAyKSwKICAgICcvc2V0cGxheWVyZGF0YSc6ICAgICAgICAgIChfc2V0cGxh"
    "eWVyZGF0YSwgMyksCiAgICAnL3NldHVzZXJoZXJvZGF0YSc6ICAgICAgICAoX3NldHVzZXJoZXJv"
    "ZGF0YSwgMiksCiAgICAnL2dhbWVjb21tYW5kdG91c2VyJzogICAgICAoX2dhbWVjb21tYW5kdG91"
    "c2VyLCAyKSwjVE9ETyBjb25zaWRlciBvcHRpbWlzaW5nCiAgICAjQXJpdHkgMTogdGhlIHBhc3N3"
    "b3JkIGFyZ3VtZW50IGlzIGFic2VudCBmb3IgYSByb29tIHRoYXQgaGFzIG5vbmUsIGFuZAogICAg"
    "I2Ryb3BwaW5nIHRoZSBjb21tYW5kIGxlZnQgdGhlIGpvaW5pbmcgcGxheWVyIG9uICJjb25uZWN0"
    "aW5nIiBmb3JldmVyLgogICAgJy9qb2luZ2FtZSc6ICAgICAgICAgICAgICAgKF9qb2luZ2FtZSwg"
    "MSksCiAgICAnL3dob2lzJzogICAgICAgICAgICAgICAgICAoX3dob2lzLCAxKSwKICAgICcvdXBk"
    "YXRlJzogICAgICAgICAgICAgICAgIChfdXBkYXRlLCA1KSwKICAgICNHdWlsZHMuIEV2ZXJ5IG5h"
    "bWUgaGVyZSBoYXMgYmVlbiBzZWVuIG9uIHRoZSB3aXJlIGZyb20gdGhlIHJldGFpbCBjbGllbnQu"
    "CiAgICAjVGhlIGJhdGNoIG9mIGd1ZXNzZWQgc3BlbGxpbmdzIHRoYXQgdXNlZCB0byBzaXQgYWxv"
    "bmdzaWRlIHRoZW0KICAgICMoL2NyZWF0ZWd1aWxkLCAvcmVxdWVzdGNyZWF0ZWd1aWxkLCAvY3Jl"
    "YXRndWlsZCwgL2d1aWxkY3JlYXRlLAogICAgIy9yZXF1ZXN0am9pbmd1aWxkLCAvcXVpdGd1aWxk"
    "LCAvZ2V0Z3VpbGRpbmZvKSBpcyBnb25lOiB0aGUgY2FwdHVyZSBzaG93ZWQKICAgICN0aGUgY2xp"
    "ZW50IHNlbmRzIG5vbmUgb2YgdGhlbSwgYW5kIHRoYXQgL2pvaW5ndWlsZCBpcyB3aGF0IGNyZWF0"
    "ZXMgYQogICAgI2d1aWxkLiBMZWF2aW5nIGEgZ3VpbGQgaGFzIG5vdCBiZWVuIG9ic2VydmVkIHll"
    "dCwgc28gbm8gaGFuZGxlciBpcwogICAgI3JlZ2lzdGVyZWQgZm9yIGl0IC0gdGhlIHJlYWwgbmFt"
    "ZSB3aWxsIHNob3cgdXAgaW4gdGhlIGxvZyBhcyBhbiB1bmtub3duCiAgICAjY29tbWFuZCB0aGUg"
    "Zmlyc3QgdGltZSBzb21lYm9keSB0cmllcy4KICAgICcvZ3VpbGRzbGFkZGVyJzogICAgICAgICAg"
    "IChfZ3VpbGRzbGFkZGVyLCAxKSwKICAgICcvdGVzdGNyZWF0ZWd1aWxkJzogICAgICAgIChfdGVz"
    "dGNyZWF0ZWd1aWxkLCAxKSwKICAgICcvam9pbmd1aWxkJzogICAgICAgICAgICAgIChfam9pbmd1"
    "aWxkLCAxKSwKfQpjbGFzcyBDb21tYW5kUGFyc2VyKCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwg"
    "bXNnZXIpOgogICAgICAgIHNlbGYuY29tbWFuZGxpc3QgPSBfQ09NTUFORFMKICAgICAgICBzZWxm"
    "Lm1kID0gbXNnZXIKCiAgICBkZWYgcGFyc2Uoc2VsZiwgZGF0YSwgb3JpZ2luKToKICAgICAgICAj"
    "cHJpbnQoZidUZXN0IFBhcnNpbmcge2xlbihkYXRhKX06IHtieXRlcyhkYXRhLCAnYXNjaWknKX0n"
    "KQogICAgICAgIHJlcyA9IGxpc3QoIChpdG1bMF0raXRtWzFdIGZvciBpdG0gaW4gX1JFX0NNRC5m"
    "aW5kYWxsKGRhdGEpKSApCiAgICAgICAgI3ByaW50KCdSZXM6JywgcmVzKQogICAgICAgIGlmIG5v"
    "dCByZXM6CiAgICAgICAgICAgICNXYXMgYSBzaWxlbnQgZHJvcC4gSWYgYSBmZWF0dXJlIGRvZXMg"
    "bm90aGluZyBhbmQgdGhlIGxvZyBzaG93cyBubwogICAgICAgICAgICAjY29tbWFuZCBmb3IgaXQg"
    "YXQgYWxsLCB0aGlzIGlzIG9uZSBvZiB0aGUgdHdvIHBsYWNlcyBpdCBjb3VsZAogICAgICAgICAg"
    "ICAjaGF2ZSBkaXNhcHBlYXJlZCBpbnRvIC0gc28gc2F5IHNvIHJhdGhlciB0aGFuIGxlYXZlIGEg"
    "YmxpbmQgc3BvdC4KICAgICAgICAgICAgaWYgX0RFQlVHX0xPR19DT01NQU5EUyBhbmQgZGF0YToK"
    "ICAgICAgICAgICAgICAgIHdobyA9IG9yaWdpbi51c2VyLm5hbWUgaWYgb3JpZ2luLnVzZXIgZWxz"
    "ZSAnPycKICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gLT4gKFVOUEFSU0VBQkxF"
    "KSB7ZGF0YSFyfScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgd2hvID0gb3JpZ2lu"
    "LnVzZXIubmFtZSBpZiBvcmlnaW4udXNlciBlbHNlICc/JwogICAgICAgIGxvdWQgPSBfREVCVUdf"
    "TE9HX0NPTU1BTkRTIGFuZCAoX0RFQlVHX0xPR19WRVJCT1NFIG9yIHJlc1swXSBub3QgaW4gX1FV"
    "SUVUX0NPTU1BTkRTKQogICAgICAgIGlmIGxvdWQ6CiAgICAgICAgICAgIHByaW50KGYnW2NtZF0g"
    "e3dob30gLT4ge2RhdGF9JykKICAgICAgICBlbnRyeSA9IHNlbGYuY29tbWFuZGxpc3QuZ2V0KHJl"
    "c1swXSkKICAgICAgICBpZiBlbnRyeSBpcyBOb25lOgogICAgICAgICAgICAjTG9nIHRoZSByYXcg"
    "bGluZSwgbm90IGp1c3QgdGhlIHRva2VuaXNlZCBsaXN0LiBBbiB1bmltcGxlbWVudGVkCiAgICAg"
    "ICAgICAgICNjb21tYW5kIGlzIGV4YWN0bHkgdGhlIHNpdHVhdGlvbiB3aGVyZSB0aGUgYXJndW1l"
    "bnQgbGF5b3V0IGlzCiAgICAgICAgICAgICN3aGF0IHdlIG5lZWQgdG8gc2VlLCBhbmQgcmUtcXVv"
    "dGluZyB0aGUgc3BsaXQgdG9rZW5zIGxvc2VzIGl0LgogICAgICAgICAgICBwcmludChmJyoqKiBV"
    "TktOT1dOIENPTU1BTkQgZnJvbSB7d2hvfToge2RhdGEhcn0nKQogICAgICAgICAgICByZXR1cm4g"
    "Tm9uZQogICAgICAgIGhhbmRsZXIsIG1pbmFyZ3MgPSBlbnRyeQogICAgICAgIGlmIGxlbihyZXMp"
    "IC0gMSA8IG1pbmFyZ3M6CiAgICAgICAgICAgIHByaW50KGYnKioqIE1BTEZPUk1FRCBDT01NQU5E"
    "IGZyb20ge3dob306ICcKICAgICAgICAgICAgICAgICAgZid7cmVzWzBdfSBuZWVkcyB7bWluYXJn"
    "c30gYXJndW1lbnQocyksIGdvdCB7bGVuKHJlcyktMX0nKQogICAgICAgICAgICByZXR1cm4gTm9u"
    "ZQogICAgICAgICNwcmludChmJ1BhcnNlZCBDb21tYW5kIEZyb20ge29yaWdpbi51c2VyLm5hbWV9"
    "OicsIHJlcykKICAgICAgICBvdXQgPSBoYW5kbGVyKHNlbGYubWQsIG9yaWdpbiwgcmVzKQogICAg"
    "ICAgIGlmIGxvdWQ6CiAgICAgICAgICAgICMiKG5vIGRpcmVjdCByZXBseSkiIGlzIHRoZSBzaWdu"
    "YXR1cmUgb2YgZXZlcnkgaGFuZyByZXBvcnRlZCBzbwogICAgICAgICAgICAjZmFyOiB0aGUgY2xp"
    "ZW50IHdhaXRzIG9uIGFuIGFuc3dlciB0aGF0IHRoaXMgc2VydmVyIG5ldmVyIHNlbmRzLgogICAg"
    "ICAgICAgICAjU29tZSBjb21tYW5kcyBsZWdpdGltYXRlbHkgYW5zd2VyIHdpdGggbm90aGluZywg"
    "c28gdGhpcyBpcyBhIGxlYWQsCiAgICAgICAgICAgICNub3QgYSB2ZXJkaWN0IC0gYnV0IGl0IGlz"
    "IHRoZSBmaXJzdCB0aGluZyB0byBsb29rIGF0LgogICAgICAgICAgICBpZiBvdXQ6CiAgICAgICAg"
    "ICAgICAgICBoZWFkID0gb3V0LnNwbGl0KF9OKVswXS5kZWNvZGUoX1dJUkVfRU5DLCAncmVwbGFj"
    "ZScpCiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IDwtIHtoZWFkfScpCiAgICAg"
    "ICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IDwtIChubyBk"
    "aXJlY3QgcmVwbHkpJykKICAgICAgICByZXR1cm4gb3V0CgojdGhyZWFkIHRvIHNlbmQgbWVzc2Fn"
    "ZXMgYWNyb3NzIGFsbCBjb25uZWN0ZWQgY2xpZW50cwojX19FWEFNUExFX01FU1NBR0VfXyA9IHsK"
    "IyAgICAndGFyZ2V0JzpbJ3VzZXJsaXN0J10sCiMgICAgJ21lc3NhZ2UnOmInL3doYXRldmVyXDAn"
    "K2InYmxvYicKI30KY2xhc3MgTWVzc2FnZURpc3RyaWJ1dG9yKCk6CiAgICBfRU5ESVRFTSA9IFsn"
    "U1RPUCddCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyKToKICAgICAgICBzZWxmLl9jUXVl"
    "dWUgPSBTaW1wbGVRdWV1ZSgpCiAgICAgICAgc2VsZi5zZXJ2ZXIgPSBzZXJ2ZXIKICAgIGRlZiBz"
    "ZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIHdoaWxlIFRydWU6ICNUT0RPIHBvc3NpYmxlIGNo"
    "ZWNrIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAg"
    "ICAgIGNvbW1hbmQgPSBzZWxmLl9jUXVldWUuZ2V0KCkKICAgICAgICAgICAgICAgICNwcmludCgn"
    "TUQ6JywgY29tbWFuZCwgc2VsZi5zZXJ2ZXIuX2lzX2Nsb3NpbmcpCiAgICAgICAgICAgICAgICBp"
    "ZiBjb21tYW5kID09IHNlbGYuX0VORElURU06CiAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAg"
    "ICAgICAgICAgICAgIHVsID0gY29tbWFuZC5nZXQoJ3RhcmdldCcsW10pCiAgICAgICAgICAgICAg"
    "ICBtc2cgPSBjb21tYW5kLmdldCgnbWVzc2FnZScpCiAgICAgICAgICAgICAgICBpZiBtc2c6CiAg"
    "ICAgICAgICAgICAgICAgICAgZm9yIHVzciBpbiB1bDoKICAgICAgICAgICAgICAgICAgICAgICAg"
    "dXNyLnNlbmQobXNnKQogICAgICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICAg"
    "ICAgcHJpbnQoJ1tMb2JieV0gRGlzdHJpYnV0b3IgZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1h"
    "dF9leGMoKSkKICAgIGRlZiBhZGQoc2VsZiwgcHJvcHMpOgogICAgICAgICNTbmFwc2hvdCB0aGUg"
    "dGFyZ2V0IGxpc3QgSEVSRSwgaW4gdGhlIGNhbGxpbmcgdGhyZWFkLiBDYWxsZXJzIGhhbmQgdXMK"
    "ICAgICAgICAjbGl2ZSBjb250YWluZXJzIChHYW1lQ2hhbm5lbC51c2VybGlzdCwgc3RhdGUuYWN0"
    "aXZlVXNlcnMudmFsdWVzKCksIC4uLikKICAgICAgICAjdGhhdCBvdGhlciBoYW5kbGVyIHRocmVh"
    "ZHMgYXBwZW5kIHRvL3JlbW92ZSBmcm9tIGNvbnRpbnVvdXNseTsgdGhlCiAgICAgICAgI2Rpc3Ry"
    "aWJ1dG9yIHRocmVhZCBpdGVyYXRlZCB0aGVtIGxhdGVyIGFuZCBoaXQgJ2xpc3QgY2hhbmdlZCBz"
    "aXplCiAgICAgICAgI2R1cmluZyBpdGVyYXRpb24nLCB3aGljaCB0aGUgZXhjZXB0IGFib3ZlIHN3"
    "YWxsb3dlZCAtIHNpbGVudGx5CiAgICAgICAgI2Ryb3BwaW5nIHRoZSBlbnRpcmUgYnJvYWRjYXN0"
    "LiB1cGRhdGVQb3MoKSBkb2VzIHRoaXMgb25jZSBhIHNlY29uZCBmb3IKICAgICAgICAjZXZlcnkg"
    "Y2hhbm5lbCwgc28gdGhpcyB3YXMgdGhlIGhvdCBwYXRoIGZvciB0aGUgcmFjZS4KICAgICAgICBp"
    "ZiBpc2luc3RhbmNlKHByb3BzLCBkaWN0KToKICAgICAgICAgICAgcHJvcHMgPSBkaWN0KHByb3Bz"
    "KQogICAgICAgICAgICBwcm9wc1sndGFyZ2V0J10gPSBsaXN0KHByb3BzLmdldCgndGFyZ2V0Jykg"
    "b3IgKCkpCiAgICAgICAgc2VsZi5fY1F1ZXVlLnB1dChwcm9wcykKICAgIGRlZiBlbmQoc2VsZik6"
    "CiAgICAgICAgc2VsZi5hZGQoc2VsZi5fRU5ESVRFTSkKICAgIApjbGFzcyBHYW1lRW50cnkoKToK"
    "ICAgIGRlZiBfX2luaXRfXyhzZWxmLCBwYXJlbnQsIG5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1h"
    "cHQsIG5waiwgdW4xLCBzdGF0dXMsIG1heHBsYXllcnMsIHVybCk6CiAgICAgICAgaWYgaG9zdC51"
    "c2VyLmdhbWU6CiAgICAgICAgICAgIGhvc3QudXNlci5nYW1lLnJlbW92ZShob3N0KQogICAgICAg"
    "IHNlbGYucGFyZW50ID0gcGFyZW50ICMgR2FtZWNoYW5uZWwKICAgICAgICBzZWxmLmduYW1lID0g"
    "bmFtZSAjCiAgICAgICAgc2VsZi5ob3N0ID0gaG9zdCAjIENvbm5lY3Rpb24gT2JqZWN0CiAgICAg"
    "ICAgc2VsZi5wYXNzd29yZCA9IHBhc3cgIyAnJyBvciAncGFzc3dvcmQnCiAgICAgICAgc2VsZi5t"
    "YXBQYXIgPSBtYXBwICMgIk5ldF9NXzAxIG51bGwgMCAxIgogICAgICAgIHNlbGYubWFwVHJhbnNs"
    "YXRlID0gbWFwdCAjICJ0cmFuc2xhdGVOZXRfTV8wMSIKICAgICAgICBzZWxmLm5waiA9IGludChu"
    "cGopICMgImVuYWJsZSBuZXcgcGxheWVyIHRvIGpvaW4gKGJvb2wpIgogICAgICAgIHNlbGYudW4x"
    "ID0gaW50KHVuMSkgIyAwIFRPRE8gZmlndXJlIG91dCBpZiBtZWFucyAiZ3VpbGQgZ2FtZSIKICAg"
    "ICAgICBzZWxmLnN0YXR1cyA9IGludChzdGF0dXMpICMgY2hhbmdlcyB0byAxIHdoZW4gc3RhcnRl"
    "ZCwgb25seSByZWxldmFudCB3aGVuIG5waiB0cnVlCiAgICAgICAgc2VsZi5tYXhwbGF5ZXJzID0g"
    "aW50KG1heHBsYXllcnMpICMgOCAjbWF4IHVzZXJzPwogICAgICAgICN4LWRpcmVjdHBsYXkgdXJs"
    "LCB3aXRoIHRoZSBob3N0J3MgYWR2ZXJ0aXNlZCBhZGRyZXNzIHJlcGxhY2VkIGJ5IHRoZQogICAg"
    "ICAgICNhZGRyZXNzIHRoaXMgc2VydmVyIHNlZXMgaXQgY29ubmVjdCBmcm9tIC0gc2VlIHJld3Jp"
    "dGVHYW1lSG9zdCgpLgogICAgICAgIHBlZXIgPSBob3N0LmNsaWVudF9hZGRyZXNzWzBdIGlmIGhv"
    "c3QuY2xpZW50X2FkZHJlc3MgZWxzZSAnJwogICAgICAgIChzZWxmLnVybCwgbm90ZSkgPSByZXdy"
    "aXRlR2FtZUhvc3QodXJsLCBwZWVyKQogICAgICAgIHByaW50KGYnW0xvYmJ5XSBSb29tICJ7bmFt"
    "ZX0iIGJ5IHtob3N0LnVzZXIubmFtZX06IHtub3RlfScpCiAgICAgICAgcHJpbnQoZidbTG9iYnld"
    "ICAgdXJsIGFkdmVydGlzZWQgdG8gam9pbmVyczoge3NlbGYudXJsfScpCiAgICAgICAgc2VsZi51"
    "c2VybGlzdCA9IFtob3N0LF0KICAgICAgICBzZWxmLnBhcmVudC5nYW1lc1tzZWxmLmduYW1lXSA9"
    "IHNlbGYKICAgICAgICBzZWxmLmhvc3QudXNlci5nYW1lID0gc2VsZgogICAgICAgICNBZHZlcnRp"
    "c2Ugb24gY3JlYXRpb24KICAgICAgICBtc2cgPSBzZWxmLmdldEdhbWVTdHJpbmcoKQogICAgICAg"
    "IHRnID0gc2VsZi5wYXJlbnQudXNlcmxpc3QKICAgICAgICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlz"
    "dC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzptc2d9KQogICAgZGVmIF9hdWRpZW5jZShzZWxm"
    "KToKICAgICAgICAjV2hvIG5lZWRzIHRvIGhlYXIgYWJvdXQgdGhpcyByb29tIGNoYW5naW5nOiBl"
    "dmVyeW9uZSBicm93c2luZyB0aGUKICAgICAgICAjdG93biwgcGx1cyBldmVyeW9uZSBhbHJlYWR5"
    "IGluc2lkZSB0aGUgcm9vbS4gT25jZSBhIGdhbWUgc3RhcnRzIGl0cwogICAgICAgICNwbGF5ZXJz"
    "IGFyZSB0YWtlbiBvZmYgdGhlIHRvd24gcm9zdGVyIChzZWUgc3RhcnRHYW1lKSwgc28gdGhlIHRv"
    "d24KICAgICAgICAjbGlzdCBhbG9uZSBubyBsb25nZXIgcmVhY2hlcyB0aGVtIC0gYW5kIHRoZSBo"
    "b3N0LCB3aG8gaXMgYWx3YXlzCiAgICAgICAgI2luLWdhbWUsIGlzIGV4YWN0bHkgd2hvIG5lZWRz"
    "IHRvIGtub3cgdGhhdCBzb21lYm9keSBqb2luZWQuCiAgICAgICAgc2VlbiA9IGxpc3Qoc2VsZi5w"
    "YXJlbnQudXNlcmxpc3QpCiAgICAgICAgZm9yIGMgaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAg"
    "ICAgaWYgYyBub3QgaW4gc2VlbjoKICAgICAgICAgICAgICAgIHNlZW4uYXBwZW5kKGMpCiAgICAg"
    "ICAgcmV0dXJuIHNlZW4KICAgIGRlZiBhZGRVc2VyKHNlbGYsIHVzciwgcGFzdyk6CiAgICAgICAg"
    "I0V2ZXJ5IHJlamVjdGlvbiBiZWxvdyBoYXMgdG8gYW5zd2VyIHRoZSBjbGllbnQgd2l0aCAqc29t"
    "ZXRoaW5nKi4gVGhlCiAgICAgICAgI2NsaWVudCBzaG93cyAiY29ubmVjdGluZy4uLiIgZnJvbSB0"
    "aGUgbW9tZW50IGl0IHNlbmRzIC9qb2luZ2FtZSB1bnRpbAogICAgICAgICN0aGUgc2VydmVyIGFu"
    "c3dlcnMsIGFuZCBpdCBoYXMgbm8gdGltZW91dCBvZiBpdHMgb3duOiByZXR1cm5pbmcgTm9uZQog"
    "ICAgICAgICNsZWZ0IHRoZSBwbGF5ZXIgc3RhcmluZyBhdCB0aGF0IGRpYWxvZyB1bnRpbCB0aGV5"
    "IGtpbGxlZCB0aGUgZ2FtZS4KICAgICAgICBpZiB1c3IgaW4gc2VsZi51c2VybGlzdDoKICAgICAg"
    "ICAgICAgI0FscmVhZHkgaW4gKGR1cGxpY2F0ZSAvam9pbmdhbWUsIGUuZy4gdGhlIHBsYXllciBk"
    "b3VibGUtY2xpY2tlZAogICAgICAgICAgICAjdGhlIHJvb20pLiBSZS1hbnN3ZXIgaW5zdGVhZCBv"
    "ZiBhcHBlbmRpbmcgdGhlbSBhIHNlY29uZCB0aW1lLgogICAgICAgICAgICByZXR1cm4gX2VtKGYn"
    "L2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi51cmx9IiAie3NlbGYuc3RhdHVzfSInKQog"
    "ICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KT49c2VsZi5tYXhwbGF5ZXJzOgogICAgICAgICAg"
    "ICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVGdWxsICJ7c2VsZi5nbmFtZX0iJykKICAgICAgICBp"
    "ZiBzZWxmLnN0YXR1cyBhbmQgbm90IHNlbGYubnBqOgogICAgICAgICAgICByZXR1cm4gX2VtKGYn"
    "L2Vycm9yIGdhbWVBbHJlYWR5U3RhcnRlZCAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgaWYgc2Vs"
    "Zi5wYXNzd29yZCAhPSBwYXN3OgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGJhZEdh"
    "bWVQYXNzd29yZCAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgaWYgdXNyLnVzZXIuZ2FtZSBpcyBu"
    "b3QgTm9uZToKICAgICAgICAgICAgdXNyLnVzZXIuZ2FtZS5yZW1vdmUodXNyKSAjbGVhdmUgdGhl"
    "IHByZXZpb3VzIHJvb20gY2xlYW5seSBmaXJzdAogICAgICAgIHNlbGYudXNlcmxpc3QuYXBwZW5k"
    "KHVzcikKICAgICAgICB1c3IudXNlci5nYW1lID0gc2VsZgogICAgICAgIHJldCA9IF9lbShmJyRn"
    "YW1ldXNlciAie3NlbGYuZ25hbWV9IiAie3Vzci51c2VyLm5hbWV9IiAiIiAiMTAwIiAiMCInKQog"
    "ICAgICAgICNVbmNvbmRpdGlvbmFsbHksIHRvIGV2ZXJ5b25lIGluIHRoZSB0b3duLiBUaGlzIHVz"
    "ZWQgdG8gYmUgc2VudCBvbmx5CiAgICAgICAgI3doZW4gbnBqICgibmV3IHBsYXllcnMgbWF5IGpv"
    "aW4gYSBydW5uaW5nIGdhbWUiKSB3YXMgc2V0IC0gYnV0IG5wagogICAgICAgICNzYXlzIG5vdGhp"
    "bmcgYWJvdXQgd2hvIHNob3VsZCBoZWFyIGFib3V0IGEgam9pbiwgaXQgb25seSBjb250cm9scwog"
    "ICAgICAgICN3aGV0aGVyIGEgKnN0YXJ0ZWQqIGdhbWUgc3RheXMgbGlzdGVkLiBGb3IgYW4gb3Jk"
    "aW5hcnkgcm9vbSwgd2hpY2ggaXMKICAgICAgICAjY3JlYXRlZCB3aXRoIG5waj0wIGFuZCBqb2lu"
    "ZWQgYmVmb3JlIGl0IHN0YXJ0cywgbm9ib2R5IHdhcyBldmVyIHRvbGQ6CiAgICAgICAgI3RoZSBo"
    "b3N0J3MgbG9iYnkgbmV2ZXIgbGlzdGVkIHRoZSBhcnJpdmluZyBwbGF5ZXIsIHNvIHRoZSBob3N0"
    "IGhhZAogICAgICAgICNub2JvZHkgdG8gc3RhcnQgdGhlIGdhbWUgd2l0aCwgYW5kIHRoZSBqb2lu"
    "ZXIgc2F0IGluICJjb25uZWN0aW5nIgogICAgICAgICNmb3JldmVyIHdhaXRpbmcgZm9yIGEgc3Rh"
    "cnQgdGhhdCBjb3VsZCBub3QgY29tZS4KICAgICAgICB1c3Iuc2VydmVyLmRpc3QuYWRkKHsndGFy"
    "Z2V0JzpzZWxmLl9hdWRpZW5jZSgpLCdtZXNzYWdlJzpyZXR9KQogICAgICAgIHJldHVybiBfZW0o"
    "Zicvam9pbmdhbWUgIntzZWxmLmduYW1lfSIgIntzZWxmLnVybH0iICJ7c2VsZi5zdGF0dXN9Iicp"
    "CiAgICBkZWYgZGVzdHJveShzZWxmKToKICAgICAgICAjVGVhciB0aGUgcm9vbSBkb3duIGNvbXBs"
    "ZXRlbHk6IGV2ZXJ5b25lIHN0aWxsIGxpc3RlZCBpbiBpdCBpcyBwdXQKICAgICAgICAjYmFjayB0"
    "byAibm90IGluIGEgZ2FtZSIsIGFuZCB0aGUgcm9vbSBzdG9wcyBiZWluZyBhZHZlcnRpc2VkLgog"
    "ICAgICAgIHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIGZvciBjIGluIGxpc3Qoc2VsZi51"
    "c2VybGlzdCk6CiAgICAgICAgICAgIGlmIGMudXNlcjoKICAgICAgICAgICAgICAgIGMudXNlci5n"
    "YW1lID0gTm9uZQogICAgICAgIHNlbGYudXNlcmxpc3QgPSBbXQogICAgICAgIGlmIHNlbGYucGFy"
    "ZW50LmdhbWVzLmdldChzZWxmLmduYW1lKSBpcyBzZWxmOgogICAgICAgICAgICBkZWwgc2VsZi5w"
    "YXJlbnQuZ2FtZXNbc2VsZi5nbmFtZV0KICAgICAgICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlzdC5h"
    "ZGQoeyd0YXJnZXQnOnRnLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgJ21l"
    "c3NhZ2UnOl9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJyl9KQogICAgZGVmIHJlbW92ZShzZWxm"
    "LCBjb249Tm9uZSk6I1RPRE8gcmVjcmVhdGUgcHJvcGVybHkKICAgICAgICBpZiBjb24gaXMgTm9u"
    "ZSBvciBjb24gbm90IGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgIHJldHVybgogICAgICAg"
    "IHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIHNlbGYudXNlcmxpc3QucmVtb3ZlKGNvbikK"
    "ICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1ldXNlciAie2Nvbi51c2VyLm5hbWV9IicpCiAg"
    "ICAgICAgY29uLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBpZiBjb24gaXMgc2VsZi5ob3N0Ogog"
    "ICAgICAgICAgICAjVGhlIGhvc3QgKmlzKiB0aGUgZ2FtZSBzZXNzaW9uOiB0aGUgY28tb3Agd29y"
    "bGQgcnVucyBvbiB0aGVpcgogICAgICAgICAgICAjbWFjaGluZSBhbmQgdGhlIHJvb20ncyBEaXJl"
    "Y3RQbGF5IHVybCBwb2ludHMgYXQgaXQuIE9uY2UgdGhleSBhcmUKICAgICAgICAgICAgI2dvbmUg"
    "dGhlIHJvb20gY2Fubm90IGJlIGpvaW5lZCBieSBhbnlib2R5LCBidXQgaXQgdXNlZCB0byBzdGF5"
    "CiAgICAgICAgICAgICNsaXN0ZWQgLSBzbyB0aGUgbmV4dCBwbGF5ZXIgdG8gY2xpY2sgaXQgZ290"
    "IGEgdXJsIHRvIGEgZ2FtZSB0aGF0CiAgICAgICAgICAgICNubyBsb25nZXIgZXhpc3RlZCBhbmQg"
    "c2F0IG9uICJjb25uZWN0aW5nIiB1bnRpbCB0aGV5IGdhdmUgdXAuCiAgICAgICAgICAgICNUaGlz"
    "IGlzIHdoYXQgYSBob3N0IGNyYXNoIGxlYXZlcyBiZWhpbmQuCiAgICAgICAgICAgIHByaW50KGYn"
    "W0xvYmJ5XSBIb3N0IHtjb24udXNlci5uYW1lfSBsZWZ0IHJvb20gIntzZWxmLmduYW1lfSIsIGNs"
    "b3NpbmcgaXQnKQogICAgICAgICAgICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJn"
    "ZXQnOnRnLCdtZXNzYWdlJzpsZWF2ZW1zZ30pCiAgICAgICAgICAgIHNlbGYuZGVzdHJveSgpCiAg"
    "ICAgICAgICAgIHJldHVybgogICAgICAgICNpZiAwIHVzZXJzIGxlZnQsIHJlbW92ZSBnYW1lCiAg"
    "ICAgICAgaWYgbGVuKHNlbGYudXNlcmxpc3QpPT0wOgogICAgICAgICAgICBsZWF2ZW1zZyA9IF9l"
    "bShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJykKICAgICAgICAgICAgZGVsIHNlbGYucGFyZW50Lmdh"
    "bWVzW3NlbGYuZ25hbWVdCiAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFy"
    "Z2V0Jzp0ZywnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgZGVmIHN0YXJ0R2FtZShzZWxmLCB1c2Vy"
    "PU5vbmUpOgogICAgICAgIGlmIG5vdCAodXNlciBhbmQgc2VsZi5ob3N0ID09IHVzZXIpOgogICAg"
    "ICAgICAgICByZXR1cm4gTm9uZSAjdXNlciBub3QgaG9zdAogICAgICAgIHRnID0gc2VsZi5fYXVk"
    "aWVuY2UoKQogICAgICAgIHNlbGYuc3RhdHVzID0gMQogICAgICAgIGZvciBjIGluIHNlbGYudXNl"
    "cmxpc3Q6I1RPRE8gaGF2ZSB1c2VyIHJlbW92ZSBpdHNlbGYgd2hlbiAvc3RhcnRpbmdnYW1lPwog"
    "ICAgICAgICAgICB1biA9IGMudXNlci5uYW1lCiAgICAgICAgICAgICNUT0RPIGNvbnNpZGVyIHJl"
    "bW92aW5nIHVzZXIgZnJvbSB0YXJnZXQgb3duIHNldD8KICAgICAgICAgICAgc2VsZi5wYXJlbnQu"
    "c2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6X2VtKGYnJmNoYXRjaGFubmVs"
    "dXNlciAie3VufSInKStfZW0oZicmZ2FtZWNoYW5uZWx1c2VyICJ7dW59IicpfSkKICAgICAgICAj"
    "Li4uYW5kIGFjdHVhbGx5IHRha2UgdGhlbSBvZmYgdGhlIHRvd24gcm9zdGVyLCB3aGljaCB0aGlz"
    "IG9ubHkgZXZlcgogICAgICAgICMqYW5ub3VuY2VkKi4gTGVhdmluZyB0aGVtIGxpc3RlZCBtZWFu"
    "dCB0aGUgc2VydmVyIHN0aWxsIGNvdW50ZWQgdGhlbQogICAgICAgICNhcyBzdGFuZGluZyBpbiB0"
    "aGUgdG93biBmb3IgdGhlIHdob2xlIHNlc3Npb246IHRvd24gcG9wdWxhdGlvbiB3YXMKICAgICAg"
    "ICAjd3JvbmcsIGFuZCBldmVyeSBwb3NpdGlvbiB1cGRhdGUgZnJvbSBhbnlvbmUgc3RpbGwgd2Fs"
    "a2luZyBhcm91bmQgd2FzCiAgICAgICAgI2Zhbm5lZCBvdXQgdG8gcGxheWVycyB3aG8gd2VyZSBh"
    "d2F5IGluIGEgY28tb3Agd29ybGQgYW5kIGNvdWxkIGRvCiAgICAgICAgI25vdGhpbmcgd2l0aCBp"
    "dC4gVGhlIGNsaWVudHMgd2VyZSB0b2xkIHRoZXkgbGVmdDsgbm93IHRoZSBzZXJ2ZXIKICAgICAg"
    "ICAjYWdyZWVzIHdpdGggdGhlbS4KICAgICAgICBmb3IgYyBpbiBsaXN0KHNlbGYudXNlcmxpc3Qp"
    "OgogICAgICAgICAgICBjLnVzZXIubGVhdmVDaGF0KCkKICAgICAgICAgICAgaWYgYyBpbiBzZWxm"
    "LnBhcmVudC51c2VybGlzdDoKICAgICAgICAgICAgICAgIHNlbGYucGFyZW50LnVzZXJsaXN0LnJl"
    "bW92ZShjKQogICAgICAgIGlmIG5vdCBzZWxmLm5wajoKICAgICAgICAgICAgI2dhbWUgbm8gbG9u"
    "Z2VyIGpvaW5hYmxlL3Zpc2libGUgb25jZSBzdGFydGVkCiAgICAgICAgICAgIHNlbGYucGFyZW50"
    "LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOl9lbShmJyZnYW1lICJ7c2Vs"
    "Zi5nbmFtZX0iJyl9KQogICAgICAgICNub3RpZnkgcGxheWVycyBpbiB0aGUgZ2FtZSB0aGF0IGl0"
    "IGhhcyBzdGFydGVkCiAgICAgICAgZm9yIGMgaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAg"
    "aXNIb3N0ID0gMSBpZiBjIGlzIHNlbGYuaG9zdCBlbHNlIDAKICAgICAgICAgICAgc2VsZi5wYXJl"
    "bnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0JzooYywpLCdtZXNzYWdlJzpfZW0oZicvc3RhcnRn"
    "YW1lICIxIiAie2lzSG9zdH0iICIxIicpfSkKICAgICAgICByZXR1cm4gTm9uZQogICAgZGVmIF9n"
    "ZXRVc2VybGlzdChzZWxmKToKICAgICAgICByZXR1cm4gJyAnLmpvaW4oIChmJyJ7Yy51c2VyLm5h"
    "bWV9IiAiIiAiMTAwIiAiMCInIGZvciBjIGluIHNlbGYudXNlcmxpc3QpICkKICAgIGRlZiBnZXRH"
    "YW1lU3RyaW5nKHNlbGYpOgogICAgICAgIGlmIHNlbGYuc3RhdHVzIGFuZCBub3Qgc2VsZi5ucGo6"
    "CiAgICAgICAgICAgIHJldHVybiBOb25lICNHYW1lIGRvZXMgbm90IHNob3cgaWYgbmV3IHBsYXll"
    "cnMgY2FuJ3Qgam9pbiB3aGVuIGFjdGl2ZQogICAgICAgIHBhc3cgPSAnJwogICAgICAgIGlmIHNl"
    "bGYucGFzc3dvcmQ6CiAgICAgICAgICAgIHBhc3cgPSAnWFhYJwogICAgICAgIHJldHVybiBfZW0o"
    "ZickZ2FtZSAie3NlbGYuZ25hbWV9IiAie3Bhc3d9IiAie3NlbGYubWFwUGFyfSIgIntzZWxmLm1h"
    "cFRyYW5zbGF0ZX0iICJ7c2VsZi51bjF9IiAie3NlbGYuc3RhdHVzfSIgIntzZWxmLm1heHBsYXll"
    "cnN9IiB7c2VsZi5fZ2V0VXNlcmxpc3QoKX0nKQogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAg"
    "ICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgJ25hbWUnOnNlbGYuZ25hbWUsCiAgICAgICAgICAg"
    "ICdob3N0JzpzZWxmLmhvc3QudXNlci5uYW1lLAogICAgICAgICAgICAnc3RhdHVzJzpzZWxmLnN0"
    "YXR1cywKICAgICAgICAgICAgJ2hhc1Bhc3N3b3JkJzoxIGlmIHNlbGYucGFzc3dvcmQgZWxzZSAw"
    "LAogICAgICAgICAgICAndXNlcnMnOnR1cGxlKFtjLnVzZXIubmFtZSBmb3IgYyBpbiBzZWxmLnVz"
    "ZXJsaXN0XSksCiAgICAgICAgICAgICd0b3duJzpzZWxmLnBhcmVudC5uYW1lLAogICAgICAgICAg"
    "ICAncGFyYW1ldGVycyc6c2VsZi5tYXBQYXIsCiAgICAgICAgICAgICdtYXBOYW1lJzpzZWxmLm1h"
    "cFRyYW5zbGF0ZSwKICAgICAgICAgICAgJ2NhbkpvaW5SdW5uaW5nJzpzZWxmLm5wagogICAgICAg"
    "IH0KIyB0cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwKIyB0cmFuc2xhdGVOZXRDaXR5VHJhZGVD"
    "aGFubmVsCiMgdHJhbnNsYXRlTmV0Q2l0eUNoYXRDaGFubmVsCl9ERUZBVUxUX0NIQVRTID0gWyd0"
    "cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwnLCd0cmFuc2xhdGVOZXRDaXR5VHJhZGVDaGFubmVs"
    "J10KY2xhc3MgR2FtZUNoYW5uZWwoKToKICAgIG1heHVzZXIgPSA1MCAjVE9ETyBjb25maWd1cmVh"
    "YmxlCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyLCBjaG5OYW1lKToKICAgICAgICBzZWxm"
    "LnNlcnZlciA9IHNlcnZlcgogICAgICAgIHNlbGYubmFtZSA9IGNobk5hbWUKICAgICAgICBzZWxm"
    "LnVzZXJsaXN0ID0gW10KICAgICAgICBzZWxmLmNoYXRDaGFubmVscyA9IHt9CiAgICAgICAgc2Vs"
    "Zi5nYW1lcyA9IHt9ICNUT0RPIGZpZ3VyZSBvdXQgQSBhbmQgQiB2YWx1ZSBmb3IgZGlzcGxheQog"
    "ICAgICAgICNUT0RPIHJlcXVlc3Qgam9pbiByZXNlcnZlcyBzcGFjZSB3aXRoIHdlYWsgcmVmZXJl"
    "bmNlcwogICAgICAgICMtIHdlYWsgdmFsdWUgcmVmIHNob3VsZCBlbnN1cmUgdGhhdCBjb25uZWN0"
    "aW9uIGlzIHJlbW92ZWQgZnJvbSBxdWV1ZSBpZiBpdCBkaXNjb25uZWN0cyBkdXJpbmcgdGhlIGpv"
    "aW4gcHJvY2VzcwogICAgICAgIHNlbGYucmVxdWVzdGVkID0gW10KICAgICAgICBzZWxmLmdhbWVS"
    "ZXF1ZXN0cyA9IHt9CiAgICAgICAgc2VsZi5kaXJ0eSA9IEZhbHNlCiAgICAgICAgZm9yIGNuIGlu"
    "IF9ERUZBVUxUX0NIQVRTOgogICAgICAgICAgICBzZWxmLmNoYXRDaGFubmVsc1tjbl0gPSBbXSAj"
    "VXNlcmxpc3QKICAgIGRlZiByZXF1ZXN0Sm9pbihzZWxmLCBjb24pOgogICAgICAgICNsZWF2ZUNo"
    "YW5uZWwoKSBhbHJlYWR5IHJlbGVhc2VzIGFueSBvdXRzdGFuZGluZyByZXNlcnZhdGlvbiwgb24g"
    "dGhpcwogICAgICAgICNjaGFubmVsIG9yIGFub3RoZXIgb25lLiBUaGUgZm9sbG93LXVwIGJsb2Nr"
    "IHRoYXQgdXNlZCB0byBzdGFuZCBoZXJlCiAgICAgICAgI2NvdWxkIHRoZXJlZm9yZSBuZXZlciBy"
    "dW4gLSBhbmQgaWYgaXQgZXZlciBoYWQsIGl0cyB1bmd1YXJkZWQKICAgICAgICAjbGlzdC5yZW1v"
    "dmUoKSB3b3VsZCBoYXZlIHJhaXNlZCBWYWx1ZUVycm9yIGZvciBhIHJlc2VydmF0aW9uIHRoYXQg"
    "d2FzCiAgICAgICAgI2FscmVhZHkgZ29uZS4KICAgICAgICBjb24udXNlci5sZWF2ZUNoYW5uZWwo"
    "KQogICAgICAgIGVsZW4gPSBsZW4oc2VsZi51c2VybGlzdCkrbGVuKHNlbGYucmVxdWVzdGVkKQog"
    "ICAgICAgIGlmIGVsZW48c2VsZi5tYXh1c2VyOgogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZC5h"
    "cHBlbmQoY29uKQogICAgICAgICAgICBjb24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gc2VsZgog"
    "ICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHJldHVybiBGYWxzZQogICAgZGVmIF9pc1N0"
    "YWxlR2FtZShzZWxmLCBnZW50LCBjb24pOgogICAgICAgICNBIHJvb20gd2hvc2UgaG9zdCBpcyBu"
    "byBsb25nZXIgdGhlIGxpdmUgc2Vzc2lvbiBmb3IgdGhhdCBhY2NvdW50LiBUaGUKICAgICAgICAj"
    "Y2xpZW50IG5hbWVzIGEgcm9vbSBhZnRlciBpdHMgaG9zdCwgc28gd2hlbiBhIHBsYXllciB3aG9z"
    "ZSBnYW1lCiAgICAgICAgI2NyYXNoZWQgcmVjb25uZWN0cyBhbmQgaG9zdHMgYWdhaW4sIHRoZSBy"
    "b29tIGZyb20gdGhlIHNlc3Npb24gdGhhdAogICAgICAgICNkaWVkIGlzIHN0aWxsIHNpdHRpbmcg"
    "aGVyZSB1bmRlciB0aGUgc2FtZSBuYW1lIC0gd2l0aCBhIGhvc3QKICAgICAgICAjY29ubmVjdGlv"
    "biB0aGF0IG5vIGxvbmdlciBleGlzdHMgYW5kIGEgRGlyZWN0UGxheSB1cmwgcG9pbnRpbmcgYXQg"
    "YQogICAgICAgICNnYW1lIHRoYXQgaXMgZ29uZS4gQW55b25lIGpvaW5pbmcgaXQgd2FpdHMgZm9y"
    "ZXZlci4KICAgICAgICBpZiBnZW50Lmhvc3QgaXMgY29uOgogICAgICAgICAgICByZXR1cm4gVHJ1"
    "ZQogICAgICAgIGhvc3RuYW1lID0gZ2VudC5ob3N0LnVzZXIubmFtZSBpZiBnZW50Lmhvc3QudXNl"
    "ciBlbHNlIE5vbmUKICAgICAgICBpZiBob3N0bmFtZSBpcyBOb25lOgogICAgICAgICAgICByZXR1"
    "cm4gVHJ1ZQogICAgICAgIHJldHVybiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIoaG9zdG5hbWUpIGlz"
    "IG5vdCBnZW50Lmhvc3QKICAgIGRlZiByZXF1ZXN0Q3JlYXRlR2FtZShzZWxmLCBjb24sIGdhbWVO"
    "YW1lKToKICAgICAgICAjTmV2ZXIgcmV0dXJuIGEgYmFyZSBGYWxzZSBmcm9tIGhlcmUuIHBhcnNl"
    "KCkgdHJlYXRzIGEgZmFsc3kgcmVzdWx0IGFzCiAgICAgICAgIyJub3RoaW5nIHRvIHNlbmQiLCBz"
    "byBldmVyeSByZWplY3Rpb24gYmVsb3cgdXNlZCB0byBsZWF2ZSB0aGUgY2xpZW50CiAgICAgICAg"
    "I3dhaXRpbmcgb24gYW4gYW5zd2VyIHRoYXQgbmV2ZXIgY2FtZSAtIHRoZSByb29tLWNyZWF0aW9u"
    "IGRpYWxvZyB0aGVuCiAgICAgICAgI3NwaW5zIGZvcmV2ZXIuCiAgICAgICAgaWYgY29uLnVzZXIu"
    "cmVxdWVzdGVkR2FtZSBvciBjb24udXNlci5nYW1lOgogICAgICAgICAgICBjb24udXNlci5zdG9w"
    "R2FtZSgpCiAgICAgICAgdGNuID0gc2VsZi5nYW1lUmVxdWVzdHMuZ2V0KGdhbWVOYW1lKQogICAg"
    "ICAgIGlmIHRjbiBpcyBub3QgTm9uZSBhbmQgdGNuIGlzIG5vdCBjb246CiAgICAgICAgICAgIHJl"
    "dHVybiBfZW0oZicvZXJyb3IgZ2FtZU5hbWVUYWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgICAg"
    "ICAjZWxzZSB0Y24gaXMgY29uLCByZS1yZXF1ZXN0ZWQgY3JlYXRpb24KICAgICAgICBnZW50ID0g"
    "c2VsZi5nYW1lcy5nZXQoZ2FtZU5hbWUpCiAgICAgICAgaWYgZ2VudCBpcyBub3QgTm9uZToKICAg"
    "ICAgICAgICAgaWYgc2VsZi5faXNTdGFsZUdhbWUoZ2VudCwgY29uKToKICAgICAgICAgICAgICAg"
    "IHByaW50KGYnW0xvYmJ5XSBSZXBsYWNpbmcgc3RhbGUgcm9vbSAie2dhbWVOYW1lfSIgJwogICAg"
    "ICAgICAgICAgICAgICAgICAgZicoaG9zdCBzZXNzaW9uIGdvbmUpIGF0IHRoZSByZXF1ZXN0IG9m"
    "IHtjb24udXNlci5uYW1lfScpCiAgICAgICAgICAgICAgICBnZW50LmRlc3Ryb3koKQogICAgICAg"
    "ICAgICBlbHNlOgogICAgICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lTmFtZVRh"
    "a2VuICJ7Z2FtZU5hbWV9IicpCiAgICAgICAgc2VsZi5nYW1lUmVxdWVzdHNbZ2FtZU5hbWVdID0g"
    "Y29uCiAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkR2FtZSA9IGdhbWVOYW1lCiAgICAgICAgcmV0"
    "dXJuIF9lbShmJy9jcmVhdGVnYW1lICJ7Z2FtZU5hbWV9IicpCiAgICBkZWYgY3JlYXRlR2FtZShz"
    "ZWxmLCBnYW1lTmFtZSwgaG9zdCwgcGFzdywgbWFwcCwgbWFwdCwgbnBqLCB1bjEsIHVuMiwgdW4z"
    "LCB1cmwpOgogICAgICAgIHJlcUhvc3QgPSBzZWxmLmdhbWVSZXF1ZXN0cy5nZXQoZ2FtZU5hbWUp"
    "CiAgICAgICAgaWYgcmVxSG9zdCBpcyBOb25lIG9yIHJlcUhvc3QgaXMgbm90IGhvc3Q6CiAgICAg"
    "ICAgICAgICNTYW1lIHJlYXNvbmluZyBhcyBhYm92ZTogYW5zd2VyLCBuZXZlciBmYWxsIHNpbGVu"
    "dC4KICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lTmFtZVRha2VuICJ7Z2FtZU5h"
    "bWV9IicpCiAgICAgICAgZ2VudCA9IEdhbWVFbnRyeShzZWxmLCBnYW1lTmFtZSwgaG9zdCwgcGFz"
    "dywgbWFwcCwgbWFwdCwgbnBqLCB1bjEsIHVuMiwgdW4zLCB1cmwpCiAgICAgICAgcmVxSG9zdC51"
    "c2VyLnJlcXVlc3RlZEdhbWUgPSBOb25lICNUT0RPIHJlb2dhbml6ZSBiZXR0ZXIKICAgICAgICBk"
    "ZWwgc2VsZi5nYW1lUmVxdWVzdHNbZ2FtZU5hbWVdCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRl"
    "ZiBsZWF2ZUNoYW5uZWwoc2VsZiwgY29uKToKICAgICAgICAjVGhlIGNsZWFudXAgcnVucyB3aGV0"
    "aGVyIG9yIG5vdCB0aGUgcGxheWVyIGlzIHN0aWxsIG9uIHRoZSB0b3duCiAgICAgICAgI3Jvc3Rl"
    "ci4gU2luY2Ugc3RhcnRHYW1lKCkgdGFrZXMgaXRzIHBsYXllcnMgb2ZmIHRoYXQgcm9zdGVyLCBh"
    "CiAgICAgICAgI3BsYXllciB3aG8gbGVhdmVzIChvciBkaXNjb25uZWN0cykgZnJvbSBpbnNpZGUg"
    "YSBydW5uaW5nIGdhbWUgdXNlZCB0bwogICAgICAgICNza2lwIGFsbCBvZiB0aGlzOiB0aGVpciBy"
    "b29tIHdhcyBuZXZlciBsZWZ0LCB0aGVpciBjaGF0IGNoYW5uZWwga2VwdAogICAgICAgICN0aGVp"
    "ciBlbnRyeSwgYW5kIGdhbWVjaGFubmVsIHN0YXllZCBwb2ludGluZyBhdCBhIHRvd24gdGhleSB3"
    "ZXJlIG5vCiAgICAgICAgI2xvbmdlciBpbi4gT25seSB0aGUgcm9zdGVyIHJlbW92YWwgYW5kIHRo"
    "ZSBhbm5vdW5jZW1lbnQgYXJlCiAgICAgICAgI2NvbmRpdGlvbmFsIG5vdyAtIGJlY2F1c2Ugb25s"
    "eSB0aG9zZSBkZXBlbmQgb24gYmVpbmcgbGlzdGVkLgogICAgICAgIGxpc3RlZCA9IGNvbiBpbiBz"
    "ZWxmLnVzZXJsaXN0CiAgICAgICAgY29uLnVzZXIuc3RvcEdhbWUoKQogICAgICAgIGNvbi51c2Vy"
    "LmxlYXZlQ2hhdCgpCiAgICAgICAgaWYgbGlzdGVkOgogICAgICAgICAgICBzZWxmLnVzZXJsaXN0"
    "LnJlbW92ZShjb24pCiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmdhbWVjaGFubmVsdXNl"
    "ciAie2Nvbi51c2VyLm5hbWV9IicpCiAgICAgICAgICAgIGNvbi5zZXJ2ZXIuZGlzdC5hZGQoeyd0"
    "YXJnZXQnOnNlbGYudXNlcmxpc3QsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgICAgICBjb24udXNl"
    "ci5nYW1lY2hhbm5lbD1Ob25lCiAgICBkZWYgbGVhdmVDaGF0KHNlbGYsIGNvbik6ICNUT0RPIGJl"
    "dHRlciBjaGF0Y2hhbm5lbCBvYmplY3QgYW5kIG1vdmUgaXQgdGhlcmUuCiAgICAgICAgY29uLnVz"
    "ZXIubGVhdmVDaGF0KCkKICAgICNUT0RPIGNoYW5nZSB0aGVzZSBmdW5jdGlvbnMgdG8gYWxzbyBo"
    "YW5kbGUgbWVzc2FnZSBmb3JtaW5nCiAgICBkZWYgam9pbkNoYW5uZWwoc2VsZiwgY29uLCBuYW0p"
    "OiNtb3ZlcyB1c2VyIGZyb20gcXVldWUgdG8gdXNlcmxpc3QKICAgICAgICBpZiBjb24gaW4gc2Vs"
    "Zi51c2VybGlzdDoKICAgICAgICAgICAgI0R1cGxpY2F0ZSAvam9pbmdhbWVjaGFubmVsIGZvciBh"
    "IHRvd24gd2UgYXJlIGFscmVhZHkgaW4uIFJlYnVpbGQKICAgICAgICAgICAgI3RoZSByZXNlcnZh"
    "dGlvbiBzbyB0aGUgcmVxdWVzdCBiZWxvdyByZS1ydW5zIHRoZSBmdWxsIGVudW1lcmF0aW9uCiAg"
    "ICAgICAgICAgICNhbmQgdGhlIGNsaWVudCBnZXRzIGEgY29tcGxldGUgYW5zd2VyIHJhdGhlciB0"
    "aGFuIHNpbGVuY2UuCiAgICAgICAgICAgIHNlbGYudXNlcmxpc3QucmVtb3ZlKGNvbikKICAgICAg"
    "ICAgICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIucmVx"
    "dWVzdGVkQ2hhbm5lbCA9IHNlbGYKICAgICAgICBpZiBjb24gbm90IGluIHNlbGYucmVxdWVzdGVk"
    "IGFuZCBjb24gbm90IGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICNObyBvdXRzdGFuZGlu"
    "ZyByZXNlcnZhdGlvbi4gVGhlIHJlc2VydmF0aW9uIGlzIGRyb3BwZWQgYnkgYW55CiAgICAgICAg"
    "ICAgICNpbnRlcnZlbmluZyBsZWF2ZUNoYW5uZWwoKS9yZXF1ZXN0Sm9pbigpIGFuZCBieSBhIHJl"
    "Y29ubmVjdCwgc28gYQogICAgICAgICAgICAjY2xpZW50IHRoYXQgZ29lcyBzdHJhaWdodCB0byAv"
    "am9pbmdhbWVjaGFubmVsIC0gb3Igd2hvc2UgZWFybGllcgogICAgICAgICAgICAjL3JlcXVlc3Rq"
    "b2luZ2FtZWNoYW5uZWwgcmFjZWQgaXRzIG93biBjbGVhbnVwIC0gdXNlZCB0byBnZXQgbm8KICAg"
    "ICAgICAgICAgI2Fuc3dlciBhdCBhbGwgYW5kIGhhbmcgb24gdGhlIGxvYWRpbmcgc2NyZWVuLiBB"
    "ZG1pdCB0aGVtIGlmIHRoZQogICAgICAgICAgICAjdG93biBoYXMgcm9vbTsgb25seSBhIGdlbnVp"
    "bmVseSBmdWxsIHRvd24gaXMgcmVmdXNlZCBub3cuCiAgICAgICAgICAgIGlmIGxlbihzZWxmLnVz"
    "ZXJsaXN0KStsZW4oc2VsZi5yZXF1ZXN0ZWQpIDwgc2VsZi5tYXh1c2VyOgogICAgICAgICAgICAg"
    "ICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAgICAgICAgICAgIGNvbi51c2VyLnJl"
    "cXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBy"
    "ZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVDaGFubmVsRnVsbCAie25hbX0iJykKICAgICAgICBpZiBj"
    "b24gaW4gc2VsZi5yZXF1ZXN0ZWQ6CiAgICAgICAgICAgICNUT0RPIHZlcmlmeSBvcmRlciBvZiBv"
    "cGVyYXRpb25zIGFuZCBwb3NzaWJsZSB0aW1pbmcgaXNzdWVzCiAgICAgICAgICAgIHNlbGYudXNl"
    "cmxpc3QuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIuZ2FtZWNoYW5uZWwgPSBzZWxm"
    "CiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLnJlbW92ZShjb24pCiAgICAgICAgICAgIGNvbi51"
    "c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lICNUT0RPIG9yZ2FuaXplIGJldHRlcj8KICAgICAg"
    "ICAgICAgdWwgPSBsZW4oc2VsZi51c2VybGlzdCkKICAgICAgICAgICAgcmV0bXNnID0gX2VtKGYn"
    "L2pvaW5nYW1lY2hhbm5lbCAie25hbX0iICJ7dWx9IicpCiAgICAgICAgICAgICNlbnVtZXJhdGUg"
    "aGVyb2RhdGEgb2YgZXhpc3RpbmcgdXNlcnMKICAgICAgICAgICAgY2h1bmtzID0gW10KICAgICAg"
    "ICAgICAgZm9yIHVzZXIgaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAgICAgIGlmIHVzZXIg"
    "PT0gY29uOgogICAgICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgICAgICBjaHVu"
    "a3MuYXBwZW5kKHVzZXIudXNlci5nZXRHQ1Vtc2coKSkKICAgICAgICAgICAgcmV0bXNnKz0gYicn"
    "LmpvaW4oY2h1bmtzKQogICAgICAgICAgICByZXRtc2crPSBzZWxmLmpvaW5DaGF0KGNvbiwgX0RF"
    "RkFVTFRfQ0hBVFNbMF0pCiAgICAgICAgICAgIHJldG1zZys9IHNlbGYuZW51bUNoYXRzKCkKICAg"
    "ICAgICAgICAgcmV0bXNnKz0gc2VsZi5lbnVtR2FtZXMoKQogICAgICAgICAgICAjYnJvYWRjYXN0"
    "IGhlcm9kYXRhIHRvIG90aGVyIGV4aXN0aW5nIHVzZXJzCiAgICAgICAgICAgIGNvbi5zZXJ2ZXIu"
    "ZGlzdC5hZGQoewogICAgICAgICAgICAgICAgJ3RhcmdldCc6X3dvVXNlcihzZWxmLnVzZXJsaXN0"
    "LCBjb24pLAogICAgICAgICAgICAgICAgJ21lc3NhZ2UnOmNvbi51c2VyLmdldEdDVW1zZygpfSkK"
    "ICAgICAgICAgICAgcmV0dXJuIHJldG1zZwogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgam9p"
    "bkNoYXQoc2VsZiwgY29uLCBuYW0sIHBhcz0nJyk6CiAgICAgICAgI1RPRE8gcGFzc3dvcmQgc3Vw"
    "cG9ydD8KICAgICAgICAjLSByZXF1aXJlcyByZXN0cnVjdHVyZSBmcm9tIGxpc3QgdG8gY2hhbm5l"
    "bCBvYmplY3RzCiAgICAgICAgaWYgbm90IG5hbSBpbiBzZWxmLmNoYXRDaGFubmVsczoKICAgICAg"
    "ICAgICAgcmV0dXJuIGInJwogICAgICAgIGNvbi51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAgI1RP"
    "RE8gY2hlY2sgaWYgY2xpZW50IGF1dG8tcHVyZ2VzIGNoYXRsaXN0CiAgICAgICAgI1RPRE8gQ0hF"
    "Q0sgLVYtIGJyb2FkY2FzdCByZWxldmFudCBjaGFuZ2VzPwogICAgICAgIGNvbi5zZXJ2ZXIuZGlz"
    "dC5hZGQoewogICAgICAgICAgICAndGFyZ2V0JzpsaXN0KHNlbGYuY2hhdENoYW5uZWxzW25hbV0p"
    "LAogICAgICAgICAgICAnbWVzc2FnZSc6X2VtKGYnJGNoYXRjaGFubmVsdXNlciAie2Nvbi51c2Vy"
    "Lm5hbWV9IicpfSkKICAgICAgICBzZWxmLmNoYXRDaGFubmVsc1tuYW1dLmFwcGVuZChjb24pCiAg"
    "ICAgICAgY29uLnVzZXIuY2hhdGNoYW5uZWwgPSBzZWxmLmNoYXRDaGFubmVsc1tuYW1dCiAgICAg"
    "ICAgdWwgPSAxI2xlbihjb24udXNlci5jaGF0Y2hhbm5lbCkKICAgICAgICByZXRtc2cgPSBfZW0o"
    "Zicvam9pbmNoYXRjaGFubmVsICJ7bmFtfSIgIiIgInt1bH0iJykKICAgICAgICAjZW51bWVyYXRl"
    "IG90aGVyIGNoYXQgdXNlcnM/CiAgICAgICAgY2h1bmtzID0gW10KICAgICAgICBmb3IgdWNvbiBp"
    "biBjb24udXNlci5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgaWYgdWNvbiAhPSBjb246CiAgICAg"
    "ICAgICAgICAgICBjaHVua3MuYXBwZW5kKF9lbShmJyRjaGF0Y2hhbm5lbHVzZXIgInt1Y29uLnVz"
    "ZXIubmFtZX0iJykpCiAgICAgICAgcmV0bXNnKz1iJycuam9pbihjaHVua3MpCiAgICAgICAgcmV0"
    "dXJuIHJldG1zZwogICAgZGVmIGVudW1DaGF0cyhzZWxmKToKICAgICAgICBjaHVua3MgPSBbXQog"
    "ICAgICAgIGZvciBjaGF0TmFtZSBpbiBzZWxmLmNoYXRDaGFubmVsczoKICAgICAgICAgICAgdWxs"
    "ID0gbGVuKHNlbGYuY2hhdENoYW5uZWxzW2NoYXROYW1lXSkjVE9ETyBpbXByb3ZlCiAgICAgICAg"
    "ICAgIGNodW5rcy5hcHBlbmQod2lyZV9lbmNvZGUoZickY2hhdGNoYW5uZWwgIntjaGF0TmFtZX0i"
    "ICIiICJ7dWxsfSInKSkKICAgICAgICByZXR1cm4gX04uam9pbihjaHVua3MpK19OCiAgICBkZWYg"
    "ZW51bUdhbWVzKHNlbGYpOgogICAgICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIGduYW1lIGlu"
    "IHNlbGYuZ2FtZXM6CiAgICAgICAgICAgIGdhbWVzdHIgPSBzZWxmLmdhbWVzW2duYW1lXS5nZXRH"
    "YW1lU3RyaW5nKCkKICAgICAgICAgICAgaWYgZ2FtZXN0cjoKICAgICAgICAgICAgICAgIGNodW5r"
    "cy5hcHBlbmQoZ2FtZXN0cikKICAgICAgICByZXR1cm4gYicnLmpvaW4oY2h1bmtzKQogICAgZGVm"
    "IHVwZGF0ZVBvcyhzZWxmLCBtZCk6CiAgICAgICAgaWYgbm90IHNlbGYuZGlydHk6CiAgICAgICAg"
    "ICAgIHJldHVybgogICAgICAgICNDbGVhcmVkIEJFRk9SRSB0aGUgc2Nhbiwgbm90IGFmdGVyLiBB"
    "IC91cGRoZXJvcG9zIHRoYXQgYXJyaXZlZCB3aGlsZQogICAgICAgICN0aGUgbG9vcCBiZWxvdyB3"
    "YXMgcnVubmluZyB1c2VkIHRvIHNldCBkaXJ0eT1UcnVlIGFuZCB0aGVuIGhhdmUgaXQKICAgICAg"
    "ICAjaW1tZWRpYXRlbHkgY2xlYXJlZCBhZ2Fpbiwgc28gdGhhdCBwbGF5ZXIncyBtb3ZlIHdhcyBu"
    "b3QgYnJvYWRjYXN0CiAgICAgICAgI3VudGlsIHNvbWVib2R5IGVsc2UgaGFwcGVuZWQgdG8gbW92"
    "ZS4gQ2xlYXJpbmcgZmlyc3QgbWVhbnMgdGhlIHdvcnN0CiAgICAgICAgI2Nhc2UgaXMgb25lIHJl"
    "ZHVuZGFudCBwYXNzLCBub3QgYSBzaWxlbnRseSBkcm9wcGVkIHBvc2l0aW9uLgogICAgICAgIHNl"
    "bGYuZGlydHkgPSBGYWxzZQogICAgICAgICNTbmFwc2hvdDogcGxheWVycyBqb2luIGFuZCBsZWF2"
    "ZSB0aGUgdG93biB3aGlsZSB0aGlzIGl0ZXJhdGVzLgogICAgICAgIHRnID0gbGlzdChzZWxmLnVz"
    "ZXJsaXN0KQogICAgICAgIG1vdmVycyA9IFtdCiAgICAgICAgZm9yIHVjb24gaW4gdGc6CiAgICAg"
    "ICAgICAgIGlmIG5vdCB1Y29uLnVzZXIucG9zY2hhbmdlZDoKICAgICAgICAgICAgICAgIGNvbnRp"
    "bnVlCiAgICAgICAgICAgIHVjb24udXNlci5wb3NjaGFuZ2VkID0gRmFsc2UKICAgICAgICAgICAg"
    "aWYgbm90IHVjb24udXNlci5oZXJvZGF0YToKICAgICAgICAgICAgICAgICNBIHBsYXllciBpcyBv"
    "bmx5IGFubm91bmNlZCB0byB0aGUgb3RoZXJzIGJ5ICRnYW1lY2hhbm5lbHVzZXIsCiAgICAgICAg"
    "ICAgICAgICAjYW5kIGdldEdDVW1zZygpIGVtaXRzIG5vdGhpbmcgYXQgYWxsIHVudGlsIHRoZWly"
    "IGhlcm9kYXRhIGhhcwogICAgICAgICAgICAgICAgI2Fycml2ZWQuIEJyb2FkY2FzdGluZyBhIHBv"
    "c2l0aW9uIGZvciBhIGhlcm8gaWQgbm9ib2R5IGhhcwogICAgICAgICAgICAgICAgI2JlZW4gdG9s"
    "ZCBhYm91dCBoYW5kcyBldmVyeSBjbGllbnQgYW4gdXBkYXRlIGZvciBhIHBsYXllciBpdAogICAg"
    "ICAgICAgICAgICAgI2RvZXMgbm90IGtub3cgZXhpc3RzLiBXYWl0IHVudGlsIHRoZXkgYXJlIGEg"
    "cmVhbCwgYW5ub3VuY2VkCiAgICAgICAgICAgICAgICAjcGxheWVyLgogICAgICAgICAgICAgICAg"
    "Y29udGludWUKICAgICAgICAgICAgbW92ZXJzLmFwcGVuZCgodWNvbiwgZid7dWNvbi51c2VyLndp"
    "cmVJZCgpfSN7dWNvbi51c2VyLnBvc2RhdGF9JykpCiAgICAgICAgaWYgbm90IG1vdmVyczoKICAg"
    "ICAgICAgICAgI0V2ZXJ5b25lIHdobyB3YXMgZGlydHkgaGFzIHNpbmNlIGxlZnQgdGhlIHRvd24u"
    "IFNlbmRpbmcgdGhlCiAgICAgICAgICAgICNhcmd1bWVudC1sZXNzICcvdXBkaGVyb3BvcyAnIHRo"
    "YXQgdGhpcyB1c2VkIHRvIHByb2R1Y2UganVzdCBoYW5kcwogICAgICAgICAgICAjdGhlIGNsaWVu"
    "dCBhbiBlbXB0eSBjb21tYW5kIHRvIHBhcnNlLgogICAgICAgICAgICByZXR1cm4KICAgICAgICAj"
    "Tm9ib2R5IGlzIHRvbGQgdGhlaXIgb3duIHBvc2l0aW9uLiBUaGUgY2xpZW50IGlzIHRoZSBhdXRo"
    "b3JpdHkgb24KICAgICAgICAjd2hlcmUgaXRzIG93biBoZXJvIGlzIC0gaXQgaXMgd2hhdCBzZW50"
    "IHRoZSBjb29yZGluYXRlcyBpbiB0aGUgZmlyc3QKICAgICAgICAjcGxhY2UgLSBzbyBlY2hvaW5n"
    "IHRoZW0gYmFjayBhIGZyYWN0aW9uIG9mIGEgc2Vjb25kIGxhdGVyIGlzIGF0IGJlc3QKICAgICAg"
    "ICAjcmVkdW5kYW50IGFuZCBhdCB3b3JzdCBhIGhpdGNoLCBhcyB0aGUgaGVybyBpcyBudWRnZWQg"
    "YmFjayB0byB3aGVyZQogICAgICAgICNpdCBzdG9vZCB3aGVuIHRoZSBwYWNrZXQgbGVmdC4gRXZl"
    "cnkgb3RoZXIgYnJvYWRjYXN0IGluIHRoaXMgZmlsZQogICAgICAgICNhbHJlYWR5IGV4Y2x1ZGVz"
    "IHRoZSBvcmlnaW5hdG9yIChzZWUgX3dvVXNlcik7IHBvc2l0aW9ucyB3ZXJlIHRoZQogICAgICAg"
    "ICNleGNlcHRpb24uIENvc3RzIG9uZSBtZXNzYWdlIGJ1aWx0IHBlciBtb3ZpbmcgcGxheWVyLCBh"
    "bmQgbm90IG9uZQogICAgICAgICNleHRyYSBieXRlIG9uIHRoZSB3aXJlOiB0aGUgZGlzdHJpYnV0"
    "b3IgYWxyZWFkeSB3cml0ZXMgdG8gZWFjaAogICAgICAgICNyZWNpcGllbnQgc2VwYXJhdGVseS4K"
    "ICAgICAgICBtb3ZlZCA9IHNldCh1IGZvciAodSwgXykgaW4gbW92ZXJzKQogICAgICAgIHdhdGNo"
    "ZXJzID0gW2MgZm9yIGMgaW4gdGcgaWYgYyBub3QgaW4gbW92ZWRdCiAgICAgICAgaWYgd2F0Y2hl"
    "cnM6CiAgICAgICAgICAgIGZvciBtc2cgaW4gc2VsZi5fcG9zTWVzc2FnZXMoW2NoIGZvciAoXywg"
    "Y2gpIGluIG1vdmVyc10pOgogICAgICAgICAgICAgICAgbWQuYWRkKHsndGFyZ2V0Jzp3YXRjaGVy"
    "cywnbWVzc2FnZSc6bXNnfSkKICAgICAgICBmb3IgKHVjb24sIF8pIGluIG1vdmVyczoKICAgICAg"
    "ICAgICAgb3RoZXJzID0gW2NoIGZvciAodSwgY2gpIGluIG1vdmVycyBpZiB1IGlzIG5vdCB1Y29u"
    "XQogICAgICAgICAgICBpZiBub3Qgb3RoZXJzOgogICAgICAgICAgICAgICAgY29udGludWUgI29u"
    "bHkgbW92ZXIgaW4gdGhlIHRvd24sIG5vdGhpbmcgdG8gdGVsbCB0aGVtCiAgICAgICAgICAgIGZv"
    "ciBtc2cgaW4gc2VsZi5fcG9zTWVzc2FnZXMob3RoZXJzKToKICAgICAgICAgICAgICAgIG1kLmFk"
    "ZCh7J3RhcmdldCc6KHVjb24sICksJ21lc3NhZ2UnOm1zZ30pCiAgICBkZWYgX3Bvc01lc3NhZ2Vz"
    "KHNlbGYsIGNodW5rcyk6CiAgICAgICAgI1NwbGl0IGludG8gc2V2ZXJhbCBjb21tYW5kcyByYXRo"
    "ZXIgdGhhbiBvbmUgYXJiaXRyYXJpbHkgbG9uZyBsaW5lLgogICAgICAgICMvdXBkaGVyb3BvcyBp"
    "cyB0aGUgb25seSBtZXNzYWdlIHdob3NlIGxlbmd0aCBncm93cyB3aXRoIHRoZSBudW1iZXIgb2YK"
    "ICAgICAgICAjcGxheWVycyAtIGEgYnVzeSB0b3duIHdvdWxkIHB1dCBmaWZ0eSAiaWQjeCN5IiBn"
    "cm91cHMgb24gYSBzaW5nbGUKICAgICAgICAjbGluZS4gVGhlIHJldGFpbCBjbGllbnQgaXMgYSAy"
    "MDA4IDMyLWJpdCBiaW5hcnkgYW5kIGl0cyBsb2JieSBwYXJzZXIKICAgICAgICAjY2FuIGJlIGFz"
    "c3VtZWQgdG8gdXNlIGZpeGVkLXNpemUgYnVmZmVyczsgaGFuZGluZyBpdCBhIGxpbmUgbG9uZ2Vy"
    "CiAgICAgICAgI3RoYW4gaXQgZXhwZWN0cyBpcyB0aGUgY2xhc3NpYyB3YXkgdG8gY29ycnVwdCBp"
    "dHMgaGVhcCBhbmQgdGFrZSBpdAogICAgICAgICNkb3duIHdpdGggYW4gYWNjZXNzIHZpb2xhdGlv"
    "biBzb21ld2hlcmUgZWxzZSBlbnRpcmVseS4gU2V2ZXJhbCBzaG9ydAogICAgICAgICNjb21tYW5k"
    "cyBhcmUgZXF1aXZhbGVudCBmb3IgdGhlIGNsaWVudCBhbmQgY29zdCBvbmUgZXh0cmEgaGVhZGVy"
    "CiAgICAgICAgI2VhY2guCiAgICAgICAgYmF0Y2hlcyA9IFtdCiAgICAgICAgY3VyID0gW10KICAg"
    "ICAgICBjdXJsZW4gPSAwCiAgICAgICAgZm9yIGNoIGluIGNodW5rczoKICAgICAgICAgICAgaWYg"
    "Y3VyIGFuZCBjdXJsZW4gKyBsZW4oY2gpICsgMSA+IF9NQVhfV0lSRV9MSU5FOgogICAgICAgICAg"
    "ICAgICAgYmF0Y2hlcy5hcHBlbmQoY3VyKQogICAgICAgICAgICAgICAgY3VyID0gW10KICAgICAg"
    "ICAgICAgICAgIGN1cmxlbiA9IDAKICAgICAgICAgICAgY3VyLmFwcGVuZChjaCkKICAgICAgICAg"
    "ICAgY3VybGVuICs9IGxlbihjaCkgKyAxCiAgICAgICAgaWYgY3VyOgogICAgICAgICAgICBiYXRj"
    "aGVzLmFwcGVuZChjdXIpCiAgICAgICAgcmV0dXJuIFtfZW0oJy91cGRoZXJvcG9zICcgKyAnICcu"
    "am9pbihiKSkgZm9yIGIgaW4gYmF0Y2hlc10KICAgIGRlZiBkZWJ1Z19hcnJfZ2FtZXMoc2VsZik6"
    "CiAgICAgICAgYWN0RGljdCA9IFtdCiAgICAgICAgZm9yIGduLCBnIGluIGxpc3Qoc2VsZi5nYW1l"
    "cy5pdGVtcygpKToKICAgICAgICAgICAgYWN0RGljdC5hcHBlbmQoZy5kZWJ1Z19kaWN0KCkpCiAg"
    "ICAgICAgcmV0dXJuIGFjdERpY3QKICAgIGRlZiBkZWJ1Z19kaWN0KHNlbGYpOgogICAgICAgIHJl"
    "dHVybiB7CiAgICAgICAgICAgICd1c2Vycyc6dHVwbGUoW2MudXNlci5uYW1lIGZvciBjIGluIHNl"
    "bGYudXNlcmxpc3RdKSwKICAgICAgICAgICAgJ21heFVzZXJzJzpzZWxmLm1heHVzZXIsCiAgICAg"
    "ICAgICAgICdnYW1lcyc6dHVwbGUoW2duIGZvciBnbiBpbiBzZWxmLmdhbWVzXSkKICAgICAgICB9"
    "CgpfTUFQTkFNRVMgPSBbJ05ldF9UXzAxJywnTmV0X1RfMDInLCdOZXRfVF8wMycsJ05ldF9UXzA0"
    "J10gI1RPRE8gdXNlIENGRyBvYmplY3QKY2xhc3MgR2FtZVN0YXRlKCk6CiAgICAjVE9ETyBhdXRv"
    "IGdyb3dhYmxlIGNoYW5uZWxzLCBbbWFwbmFtZV0KICAgICNUT0RPIGF2YWlsYWJsZSBpbmRleGVz"
    "LCBbbWFwbmFtZV0KICAgIGRlZiBfX2luaXRfXyhzZWxmLCBzZXJ2ZXIpOgogICAgICAgICNpbnN0"
    "YW5jZSBhdHRyaWJ1dGVzLCBub3QgY2xhc3MgYXR0cmlidXRlczogdGhlc2UgbXVzdCBOT1QgYmUg"
    "c2hhcmVkCiAgICAgICAgI2JldHdlZW4gc2VwYXJhdGUgQ29yZVNlcnZlciBpbnN0YW5jZXMgKGUu"
    "Zy4gc3RvcC9zdGFydCBmcm9tIGEgR1VJCiAgICAgICAgI3dpdGhpbiB0aGUgc2FtZSBwcm9jZXNz"
    "KSBvciBsZWZ0b3ZlciBwbGF5ZXJzL2NoYW5uZWxzIGZyb20gYQogICAgICAgICNwcmV2aW91cyBy"
    "dW4gd291bGQgbGVhayBpbnRvIHRoZSBuZXcgb25lLgogICAgICAgIHNlbGYuYWN0aXZlVXNlcnMg"
    "PSB7fSAjVE9ETyB0cmFjayB1c2VyIGhpc3Rvcnk/IG9wdGlvbmFsbHkKICAgICAgICBzZWxmLmdh"
    "bWVDaGFubmVscyA9IHt9ICNjaGFubmVsW10sIGtleWVkIGJ5IG1hcG5hbWUKICAgICAgICBzZWxm"
    "LnNlcnZlcj1zZXJ2ZXIKICAgICAgICBzZWxmLnVzZXJMb2NrID0gdGhyZWFkaW5nLkxvY2soKQog"
    "ICAgICAgIGZvciBuYW1lIGluIF9NQVBOQU1FUzoKICAgICAgICAgICAgZm9yIGkgaW4gcmFuZ2Uo"
    "MSk6ICNUT0RPIGNvbmZpZ3VyZWFibGUgdXAgdG8gMjA/CiAgICAgICAgICAgICAgICBjaG5OYW1l"
    "ID0gX2djaG5sKG5hbWUsIDEraSkKICAgICAgICAgICAgICAgIHNlbGYuZ2FtZUNoYW5uZWxzW2No"
    "bk5hbWVdID0gR2FtZUNoYW5uZWwoc2VsZi5zZXJ2ZXIsIGNobk5hbWUpICNUT0RPIDEgYW5kIGdy"
    "b3c/CiAgICBkZWYgY2xhaW1Vc2VyKHNlbGYsIG5hbWUsIGNvbik6CiAgICAgICAgI1B1Ymxpc2gg"
    "Y29uIGFzIFRIRSBsaXZlIHNlc3Npb24gZm9yIG5hbWUsIGF0b21pY2FsbHkuIFRoZSBvbGQgY29k"
    "ZQogICAgICAgICNjaGVja2VkIGdldFBsYXllcigpIGR1cmluZyBsb2dpbiBhbmQgdGhlbiBpbnNl"
    "cnRlZCBpbnRvIGFjdGl2ZVVzZXJzCiAgICAgICAgI211Y2ggbGF0ZXIsIGluIF9sb2JieUhhbmRs"
    "ZTsgdHdvIGNvbm5lY3Rpb25zIGxvZ2dpbmcgaW4gYXMgdGhlIHNhbWUKICAgICAgICAjYWNjb3Vu"
    "dCBhdCBvbmNlIGJvdGggcGFzc2VkIHRoZSBjaGVjaywgYW5kIHRoZSBzZWNvbmQgb25lJ3MgaW5z"
    "ZXJ0CiAgICAgICAgI292ZXJ3cm90ZSB0aGUgZmlyc3QuIFRoZSBsb3NlciB0aGVuIGRlbGV0ZWQg"
    "dGhlIHdpbm5lcidzIGVudHJ5IHdoZW4gaXQKICAgICAgICAjZGlzY29ubmVjdGVkLCBsZWF2aW5n"
    "IGEgY29ubmVjdGVkIHBsYXllciBpbnZpc2libGUgdG8gdGhlIHNlcnZlciAobm8KICAgICAgICAj"
    "a2ljaywgbm8gd2hvaXMsIG5vIG1lc3NhZ2VzKS4KICAgICAgICB3aXRoIHNlbGYudXNlckxvY2s6"
    "CiAgICAgICAgICAgIGlmIG5hbWUgaW4gc2VsZi5hY3RpdmVVc2VyczoKICAgICAgICAgICAgICAg"
    "IHJldHVybiBGYWxzZQogICAgICAgICAgICBzZWxmLmFjdGl2ZVVzZXJzW25hbWVdID0gY29uCiAg"
    "ICAgICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYgcmVsZWFzZVVzZXIoc2VsZiwgbmFtZSwgY29u"
    "KToKICAgICAgICAjb25seSBjbGVhciB0aGUgc2xvdCBpZiB3ZSBzdGlsbCBvd24gaXQsIG5ldmVy"
    "IHNvbWVvbmUgZWxzZSdzIHNlc3Npb24KICAgICAgICB3aXRoIHNlbGYudXNlckxvY2s6CiAgICAg"
    "ICAgICAgIGlmIHNlbGYuYWN0aXZlVXNlcnMuZ2V0KG5hbWUpIGlzIGNvbjoKICAgICAgICAgICAg"
    "ICAgIGRlbCBzZWxmLmFjdGl2ZVVzZXJzW25hbWVdCiAgICBkZWYgZW51bWVyYXRlR0Moc2VsZik6"
    "CiAgICAgICAgY2hucyA9IFtdCiAgICAgICAgZm9yIGNobk5hbWUgaW4gc2VsZi5nYW1lQ2hhbm5l"
    "bHM6CiAgICAgICAgICAgIGNobiA9IHNlbGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdCiAgICAgICAg"
    "ICAgIGNobnMuYXBwZW5kKHdpcmVfZW5jb2RlKGYnJGdhbWVjaGFubmVsICJ7Y2huTmFtZX0iICJ7"
    "bGVuKGNobi51c2VybGlzdCl9IiAie2Nobi5tYXh1c2VyfSIgIjAiICIwIicpKSAjVE9ETyBBdmFp"
    "bGFibGUgLSBBbGwKICAgICAgICByZXR1cm4gX04uam9pbihjaG5zKStfTgogICAgZGVmIHVwZGF0"
    "ZVBvcyhzZWxmKToKICAgICAgICBtZCA9IHNlbGYuc2VydmVyLmRpc3QKICAgICAgICBmb3IgY2hu"
    "IGluIGxpc3Qoc2VsZi5nYW1lQ2hhbm5lbHMudmFsdWVzKCkpOgogICAgICAgICAgICBjaG4udXBk"
    "YXRlUG9zKG1kKQojaGFuZGxlcyBpbnRlcmFjdGlvbnMgYmV0d2VlbiBhbGwgZWxlbWVudHMKY2xh"
    "c3MgQ29yZVNlcnZlcihzb2NrZXRzZXJ2ZXIuVGhyZWFkaW5nVENQU2VydmVyKToKICAgIGFsbG93"
    "X3JldXNlX2FkZHJlc3MgPSBUcnVlICMgVE9ETyBjaGVjayBpZiBpbXByb3ZlcyByZXN0YXJ0IHRp"
    "bWVzIHdpdGhvdXQgb3RoZXIgaXNzdWVzCiAgICBkYWVtb25fdGhyZWFkcyA9IFRydWUKICAgIGJs"
    "b2NrX29uX2Nsb3NlID0gRmFsc2UKICAgIF9pc19jbG9zaW5nID0gRmFsc2UKICAgIGRlZiBfX2lu"
    "aXRfXyhzZWxmKToKICAgICAgICAjVE9ETyBnZXQgdmFsdWVzIGZyb20gY2ZnCiAgICAgICAgI2Fk"
    "ZHJlc3MgPSAnbG9jYWxob3N0JwogICAgICAgIGFkZHJlc3MgPSAnJwogICAgICAgIHBvcnQgPSBf"
    "VFdfTE9CQllfUE9SVAogICAgICAgIHByaW50KGYnSW5pdGlhbGl6aW5nIHNlcnZlciBmb3IgcG9y"
    "dCB7cG9ydH0nKQogICAgICAgIHN1cGVyKCkuX19pbml0X18oKGFkZHJlc3MsIHBvcnQpLCBDb25u"
    "ZWN0aW9uSGFuZGxlcikKICAgICAgICBzZWxmLmRpc3QgPSBNZXNzYWdlRGlzdHJpYnV0b3Ioc2Vs"
    "ZikKICAgICAgICBzZWxmLmNvbXBhcnMgPSBDb21tYW5kUGFyc2VyKHNlbGYuZGlzdCkKICAgICAg"
    "ICBzZWxmLnN0YXRlID0gR2FtZVN0YXRlKHNlbGYpCiAgICAgICAgc2VsZi5zdGFydFRpbWUgPSBk"
    "YXRldGltZS5kYXRldGltZS5ub3coKQogICAgICAgIHNlbGYuc2VydmljZV90aWNrID0gMAogICAg"
    "ICAgIHNlbGYuc2VuZF9ub3BzID0gX1NFTkRfTk9QUwogICAgICAgIHNlbGYuX3Bvc1N0b3AgPSB0"
    "aHJlYWRpbmcuRXZlbnQoKQogICAgICAgIHNlbGYuX3Bvc1RocmVhZCA9IE5vbmUKICAgICAgICAj"
    "RXZlcnkgbGl2ZSBjb25uZWN0aW9uIGhhbmRsZXIuIHNvY2tldHNlcnZlcidzIHNodXRkb3duKCkg"
    "b25seSBzdG9wcwogICAgICAgICN0aGUgYWNjZXB0IGxvb3AgYW5kIGNsb3NlcyB0aGUgbGlzdGVu"
    "aW5nIHNvY2tldCAtIGFscmVhZHktZXN0YWJsaXNoZWQKICAgICAgICAjY29ubmVjdGlvbnMga2Vl"
    "cCB0aGVpciAoZGFlbW9uKSB0aHJlYWRzIHJ1bm5pbmcsIHN0aWxsIHJlYWRpbmcsIHN0aWxsCiAg"
    "ICAgICAgI2xvZ2dpbmcsIGZvciBhcyBsb25nIGFzIHRoZSBjbGllbnQgc3RheXMgY29ubmVjdGVk"
    "LiBGcm9tIHRoZSBjb250cm9sCiAgICAgICAgI3BhbmVsIHRoYXQgbG9va3MgbGlrZSBhIHNlcnZl"
    "ciB0aGF0IHdhcyBuZXZlciBzdG9wcGVkIGF0IGFsbC4KICAgICAgICBzZWxmLl9jb25ucyA9IHNl"
    "dCgpCiAgICAgICAgc2VsZi5fY29ubkxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICBkZWYgc2Vy"
    "dmVyX2FjdGl2YXRlKHNlbGYpOgogICAgICAgIHByaW50KGYnU2VydmVyIFN0YXJ0aW5nIGF0IFBJ"
    "RDoge29zLmdldHBpZCgpfScpI0xPRwogICAgICAgIHN1cGVyKCkuc2VydmVyX2FjdGl2YXRlKCkK"
    "ICAgIGRlZiBkZWJ1Z19kaWN0X3BsYXllcnMoc2VsZik6CiAgICAgICAgI3NuYXBzaG90IHZpYSBs"
    "aXN0KCkgZmlyc3Q6IGl0ZXJhdGluZyB0aGUgbGl2ZSBkaWN0IGRpcmVjdGx5IHJpc2tzCiAgICAg"
    "ICAgIydkaWN0aW9uYXJ5IGNoYW5nZWQgc2l6ZSBkdXJpbmcgaXRlcmF0aW9uJyB3aGVuIGEgcGxh"
    "eWVyIGNvbm5lY3RzCiAgICAgICAgI29yIGRpc2Nvbm5lY3RzIHdoaWxlIGEgbW9uaXRvcmluZyBV"
    "SSBpcyBwb2xsaW5nIHRoaXMKICAgICAgICByZXQgPSB7fQogICAgICAgIGZvciBuYW1lLCBjb24g"
    "aW4gbGlzdChzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLml0ZW1zKCkpOgogICAgICAgICAgICByZXRb"
    "bmFtZV0gPSBjb24uZGVidWdfZGljdCgpCiAgICAgICAgcmV0dXJuIHJldAogICAgZGVmIGRlYnVn"
    "X2RpY3RfdG93bnMoc2VsZik6CiAgICAgICAgcmV0ID0ge30KICAgICAgICBmb3IgbmFtZSwgY2hu"
    "IGluIGxpc3Qoc2VsZi5zdGF0ZS5nYW1lQ2hhbm5lbHMuaXRlbXMoKSk6CiAgICAgICAgICAgIHJl"
    "dFtuYW1lXSA9IGNobi5kZWJ1Z19kaWN0KCkKICAgICAgICByZXR1cm4gcmV0CiAgICBkZWYgZGVi"
    "dWdfYXJyX2dhbWVzKHNlbGYpOgogICAgICAgIHJldCA9IFtdCiAgICAgICAgZm9yIG5hbWUsIGNo"
    "biBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1zKCkpOgogICAgICAgICAgICAg"
    "cmV0LmV4dGVuZChjaG4uZGVidWdfYXJyX2dhbWVzKCkpCiAgICAgICAgcmV0dXJuIHJldAogICAg"
    "ZGVmIF9wb3NMb29wKHNlbGYpOgogICAgICAgICNQb3NpdGlvbiBmYW4tb3V0IHVzZWQgdG8gcmlk"
    "ZSBvbiBzZXJ2aWNlX2FjdGlvbnMoKSwgd2hpY2ggc29ja2V0c2VydmVyCiAgICAgICAgI2NhbGxz"
    "IG9uY2UgcGVyIHBvbGxfaW50ZXJ2YWwgLSBvbmUgc2Vjb25kLiBUaGF0IHdhcyB0aGUgY2FkZW5j"
    "ZSBhdAogICAgICAgICN3aGljaCBvdGhlciBwbGF5ZXJzJyBtYXJrZXJzIG1vdmVkIG9uIHRoZSBt"
    "YXA6IGEgZnVsbCBzZWNvbmQgb2YgZGVhZAogICAgICAgICNyZWNrb25pbmcgYmV0d2VlbiB1cGRh"
    "dGVzLCB3aGljaCByZWFkcyBhcyB0ZWxlcG9ydGluZyByYXRoZXIgdGhhbgogICAgICAgICN3YWxr"
    "aW5nLiBJdHMgb3duIHRocmVhZCBkZWNvdXBsZXMgdGhlIGJyb2FkY2FzdCByYXRlIGZyb20gdGhl"
    "IGFjY2VwdAogICAgICAgICNsb29wJ3MgcG9sbCByYXRlIHNvIGl0IGNhbiBydW4gc2V2ZXJhbCB0"
    "aW1lcyBhIHNlY29uZC4KICAgICAgICB3aGlsZSBub3Qgc2VsZi5fcG9zU3RvcC5pc19zZXQoKToK"
    "ICAgICAgICAgICAgcGVyaW9kID0gMS4wIC8gX1BPU19VUERBVEVfSFogaWYgX1BPU19VUERBVEVf"
    "SFogPiAwIGVsc2UgMS4wCiAgICAgICAgICAgICN3YWl0KCkgcmF0aGVyIHRoYW4gc2xlZXAoKTog"
    "c2h1dGRvd24gaXMgaW1tZWRpYXRlLCBhbmQgcmUtcmVhZGluZwogICAgICAgICAgICAjdGhlIHBl"
    "cmlvZCBlYWNoIHBhc3MgbWVhbnMgYSBjb25maWcgY2hhbmdlIHRha2VzIGVmZmVjdCBsaXZlLgog"
    "ICAgICAgICAgICBpZiBzZWxmLl9wb3NTdG9wLndhaXQocGVyaW9kKToKICAgICAgICAgICAgICAg"
    "IGJyZWFrCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHNlbGYuc3RhdGUudXBkYXRl"
    "UG9zKCkKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgICAgICNuZXZl"
    "ciBsZXQgb25lIGJhZCBjaGFubmVsIGtpbGwgcG9zaXRpb24gc3luYyBmb3IgZXZlcnlvbmUKICAg"
    "ICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIFBvc2l0aW9uIHVwZGF0ZSBlcnJvcjpcbicgKyB0"
    "cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgZGVmIHNlcnZpY2VfYWN0aW9ucyhzZWxmKTogI2Nh"
    "bGxlZCBldmVyeSBwb2xsX2ludGVydmFsCiAgICAgICAgIyB0aW1lIGludGVydmFscwogICAgICAg"
    "IGlmIHNlbGYuc2VuZF9ub3BzIGFuZCAoc2VsZi5zZXJ2aWNlX3RpY2slMyk9PTA6CiAgICAgICAg"
    "ICAgIHNlbGYuZGlzdC5hZGQoeyd0YXJnZXQnOnNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVz"
    "KCksJ21lc3NhZ2UnOl9lbSgnL25vcCcpfSkKICAgICAgICAgICAgI3NlbmQgJy9ub3AnIHRvIGFs"
    "bCBldmVyeSAzIHNlYyBvcHRpb25hbGx5CiAgICAgICAgI3NlcnZpY2UgdGljayAzIGRheSByZXNl"
    "dCBpbnRlcnZhbCBUT0RPIHRlc3QgYWxpZ25tZW50IHdpdGggb3RoZXIgZmFjdG9ycwogICAgICAg"
    "IHNlbGYuc2VydmljZV90aWNrID0gKHNlbGYuc2VydmljZV90aWNrKzEpJSg2MCo2MCoyNCozKQog"
    "ICAgICAgIHN1cGVyKCkuc2VydmljZV9hY3Rpb25zKCkKICAgIGRlZiBzZXJ2ZV9mb3JldmVyKHNl"
    "bGYpOgogICAgICAgIGRpc3RUaHJlYWQgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLmRp"
    "c3Quc2VydmVfZm9yZXZlcikKICAgICAgICBkaXN0VGhyZWFkLnN0YXJ0KCkKICAgICAgICBzZWxm"
    "Ll9wb3NTdG9wLmNsZWFyKCkKICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSB0aHJlYWRpbmcuVGhy"
    "ZWFkKHRhcmdldD1zZWxmLl9wb3NMb29wLCBkYWVtb249VHJ1ZSkKICAgICAgICBzZWxmLl9wb3NU"
    "aHJlYWQuc3RhcnQoKQogICAgICAgICNwb2xsX2ludGVydmFsIGlzIG5vdyBvbmx5IHRoZSBhY2Nl"
    "cHQgbG9vcCdzIHNodXRkb3duIHJlc3BvbnNpdmVuZXNzIC0KICAgICAgICAjcG9zaXRpb24gYnJv"
    "YWRjYXN0cyBubyBsb25nZXIgcmlkZSBvbiBpdAogICAgICAgIHN1cGVyKCkuc2VydmVfZm9yZXZl"
    "cigxKQogICAgICAgIHNlbGYuX3Bvc1N0b3Auc2V0KCkKICAgICAgICBpZiBzZWxmLl9wb3NUaHJl"
    "YWQ6CiAgICAgICAgICAgIHNlbGYuX3Bvc1RocmVhZC5qb2luKHRpbWVvdXQ9Mi4wKQogICAgICAg"
    "ICAgICBzZWxmLl9wb3NUaHJlYWQgPSBOb25lCiAgICAgICAgc2VsZi5kaXN0LmVuZCgpI2luIGNh"
    "c2UgaXQgaGFzbid0IGFscmVhZHkKICAgICAgICBkaXN0VGhyZWFkLmpvaW4oKQogICAgZGVmIGhh"
    "bmRsZV9zaWduYWwoc2VsZiwgdGltZW91dCk6CiAgICAgICAgZGVmIGhhbmRsZXIoc2lnbnVtLCBf"
    "KToKICAgICAgICAgICAgZGVhZGxpbmUgPSB0aW1lLm1vbm90b25pYygpICsgdGltZW91dAogICAg"
    "ICAgICAgICBzaWduYW1lID0gc2lnbmFsLlNpZ25hbHMoc2lnbnVtKS5uYW1lCiAgICAgICAgICAg"
    "IHNlbGYuX2lzX2Nsb3NpbmcgPSBUcnVlICNUT0RPIHByb3Blcmx5IGVuZCBjb25uZWN0aW9ucyBh"
    "ZnRlciBhIGRlbGF5CiAgICAgICAgICAgIHByaW50KGYnQ2xvc2luZyBpbiB7dGltZW91dH0nKQog"
    "ICAgICAgICAgICAjd2hpbGUgKGN1cnJlbnRfdGltZSA6PSB0aW1lLm1vbm90b25pYygpKSA8IGRl"
    "YWRsaW5lOgogICAgICAgICAgICAjICAgIGRlbHRhID0gaW50KGRlYWRsaW5lIC0gY3VycmVudF90"
    "aW1lKQogICAgICAgICAgICAgICAgI1RPRE8gc2lnbmFsIHRvIHBsYXllcnMgdGhhdCBjb25uZWN0"
    "aW9uIGlzIHNodXR0aW5nIGRvd24KICAgICAgICAgICAgICAgICMtIHNlbGYuc3RhdGUuYWN0aXZl"
    "VXNlcnMudmFsdWVzKCkKICAgICAgICAgICAgICAgICMtIGYnL2FkbWluIFNlcnZlciBjbG9zaW5n"
    "IGluIHtkZWx0YX0nLmVuY29kZSgnYXNjaWknKStfTgogICAgICAgICAgICAgICAgI0xPRyBDTE9T"
    "RQogICAgICAgICAgICAgICAgI1RPRE8gYmV0dGVyIHNodXRkb3duIGhhbmRsaW5nCiAgICAgICAg"
    "ICAgICMgICAgdGltZS5zbGVlcCgxKQogICAgICAgICAgICB0aW1lLnNsZWVwKHRpbWVvdXQpI2Fs"
    "dCB3aGlsZSBvdGhlciBzdHVmZiBpcyBvbmdvaW5nCiAgICAgICAgICAgIHNlbGYuX0Jhc2VTZXJ2"
    "ZXJfX3NodXRkb3duX3JlcXVlc3QgPSBUcnVlCiAgICAgICAgICAgICNzZWxmLnNodXRkb3duKCkg"
    "I29ubHkgaWYgc2VydmVfZm9yZXZlciBpcyBpbiBhIGRpZmZlcmVudCB0aHJlYWQKICAgICAgICAg"
    "ICAgI3NlbGYuc2VydmVyX2Nsb3NlKCkgI29ubHkgbmVlZGVkIGlmIG5vdCB1c2luZyBhIHdpdGgg"
    "c3RhdGVtZW50CiAgICAgICAgcmV0dXJuIGhhbmRsZXIKICAgIGRlZiByZWdpc3RlckNvbm5lY3Rp"
    "b24oc2VsZiwgY29uKToKICAgICAgICB3aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBz"
    "ZWxmLl9jb25ucy5hZGQoY29uKQogICAgZGVmIHVucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYsIGNv"
    "bik6CiAgICAgICAgd2l0aCBzZWxmLl9jb25uTG9jazoKICAgICAgICAgICAgc2VsZi5fY29ubnMu"
    "ZGlzY2FyZChjb24pCiAgICBkZWYgY2xvc2VDb25uZWN0aW9ucyhzZWxmKToKICAgICAgICAjRHJv"
    "cCBldmVyeSBjbGllbnQuIFNodXR0aW5nIHRoZSBzb2NrZXQgZG93biB1bmJsb2NrcyB3aGljaGV2"
    "ZXIKICAgICAgICAjc2VsZWN0KCkvcmVjdigpIHRoYXQgY29ubmVjdGlvbidzIHRocmVhZCBpcyBz"
    "aXR0aW5nIGluLCBzbyBpdCBydW5zCiAgICAgICAgI2l0cyBub3JtYWwgY2xlYW51cCBwYXRoIGFu"
    "ZCBleGl0cyBpbnN0ZWFkIG9mIGxpbmdlcmluZy4KICAgICAgICB3aXRoIHNlbGYuX2Nvbm5Mb2Nr"
    "OgogICAgICAgICAgICBjb25ucyA9IGxpc3Qoc2VsZi5fY29ubnMpCiAgICAgICAgZm9yIGNvbiBp"
    "biBjb25uczoKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgY29uLnJlcXVlc3Quc2h1"
    "dGRvd24oc29ja2V0LlNIVVRfUkRXUikKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAg"
    "ICAgICAgICAgICAgIHBhc3MgI2FscmVhZHkgZGVhZCwgb3IgbmV2ZXIgZnVsbHkgY29ubmVjdGVk"
    "CiAgICAgICAgICAgICNEZWxpYmVyYXRlbHkgbm90IGNsb3NlKClkIGhlcmU6IHRoZSBoYW5kbGVy"
    "IHRocmVhZCBzdGlsbCBvd25zIHRoaXMKICAgICAgICAgICAgI3NvY2tldCBhbmQgY2xvc2luZyBp"
    "dCB1bmRlcm5lYXRoIGNhdXNlcyBpdHMgbmV4dCBjYWxsIHRvIGZhaWwgd2l0aAogICAgICAgICAg"
    "ICAjV2luRXJyb3IgMTAwMzggKCJub3QgYSBzb2NrZXQiKSwgd2hpY2ggdGhlbiBnZXRzIGxvZ2dl"
    "ZCBhcyBhCiAgICAgICAgICAgICNjb25uZWN0aW9uIGVycm9yIG9uIGEgcGVyZmVjdGx5IG5vcm1h"
    "bCBzaHV0ZG93bi4gc2h1dGRvd24oKSBhbG9uZQogICAgICAgICAgICAjd2FrZXMgdGhlIHRocmVh"
    "ZCwgYW5kIHNvY2tldHNlcnZlciBjbG9zZXMgdGhlIHNvY2tldCBpdHNlbGYgb25jZQogICAgICAg"
    "ICAgICAjdGhlIGhhbmRsZXIgcmV0dXJucy4KICAgICAgICByZXR1cm4gbGVuKGNvbm5zKQogICAg"
    "ZGVmIHNodXRkb3duKHNlbGYpOgogICAgICAgICNTdG9wcGluZyB0aGUgc2VydmVyIG1lYW5zIHN0"
    "b3BwaW5nIGl0OiBmbGFnIGl0IGZpcnN0IHNvIHRoZSByZWFkCiAgICAgICAgI2xvb3BzIGJhaWwg"
    "b3V0IHJhdGhlciB0aGFuIHNlcnZpbmcgYW5vdGhlciBjb21tYW5kLCB0aGVuIHN0b3AgdGhlCiAg"
    "ICAgICAgI2FjY2VwdCBsb29wLCB0aGVuIGV2aWN0IGV2ZXJ5b25lIHN0aWxsIGNvbm5lY3RlZC4K"
    "ICAgICAgICBzZWxmLl9pc19jbG9zaW5nID0gVHJ1ZQogICAgICAgIHN1cGVyKCkuc2h1dGRvd24o"
    "KQogICAgICAgIG4gPSBzZWxmLmNsb3NlQ29ubmVjdGlvbnMoKQogICAgICAgIGlmIG46CiAgICAg"
    "ICAgICAgIHByaW50KGYnW0xvYmJ5XSBDbG9zZWQge259IGNsaWVudCBjb25uZWN0aW9uKHMpIG9u"
    "IHNodXRkb3duJykKICAgIGRlZiBnZXRQbGF5ZXIoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgIHJl"
    "dHVybiBzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLmdldCh1c2VybmFtZSkKICAgIGRlZiBraWNrUGxh"
    "eWVyKHNlbGYsIHVzZXJuYW1lLCByZWFzb249J0tpY2tlZCBieSBhZG1pbicpOgogICAgICAgICNB"
    "ZG1pbi1wYW5lbCBhY3Rpb246IGZvcmNpYmx5IGRpc2Nvbm5lY3QgYSBjb25uZWN0ZWQgcGxheWVy"
    "LiBTZW5kcyBhCiAgICAgICAgI2Jlc3QtZWZmb3J0IC9hZG1pbiBub3RpY2UgZmlyc3QgKGNsaWVu"
    "dCBzaG93cyBpdCBsaWtlIGFueSBvdGhlcgogICAgICAgICNzZXJ2ZXIgYWRtaW4gbWVzc2FnZSks"
    "IHRoZW4gc2h1dHMgZG93biB0aGUgc29ja2V0IHNvIHRoZSBwbGF5ZXIncwogICAgICAgICNoYW5k"
    "bGVyIHRocmVhZCB1bmJsb2NrcyBmcm9tIGl0cyByZWN2KCkgYW5kIHJ1bnMgaXRzIG5vcm1hbAog"
    "ICAgICAgICNkaXNjb25uZWN0L2NsZWFudXAgcGF0aC4KICAgICAgICBjb24gPSBzZWxmLmdldFBs"
    "YXllcih1c2VybmFtZSkKICAgICAgICBpZiBjb24gaXMgTm9uZToKICAgICAgICAgICAgcmV0dXJu"
    "IEZhbHNlCiAgICAgICAgdHJ5OgogICAgICAgICAgICBjb24uc2VuZFJhdyhfZW0oZicvYWRtaW4g"
    "e3JlYXNvbn0nKSkKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzICNi"
    "ZXN0IGVmZm9ydCwgY29ubmVjdGlvbiBtYXkgYWxyZWFkeSBiZSBvbiBpdHMgd2F5IG91dAogICAg"
    "ICAgIHRyeToKICAgICAgICAgICAgY29uLnJlcXVlc3Quc2h1dGRvd24oc29ja2V0LlNIVVRfUkRX"
    "UikKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzCiAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICBjb24ucmVxdWVzdC5jbG9zZSgpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlv"
    "bjoKICAgICAgICAgICAgcGFzcwogICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYgZGVsZXRlQWNj"
    "b3VudChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbjogcGVybWFu"
    "ZW50bHkgZGVsZXRlcyBhIGNoYXJhY3Rlci9hY2NvdW50LgogICAgICAgICNLaWNrcyBmaXJzdCAo"
    "bm8tb3AgaWYgYWxyZWFkeSBvZmZsaW5lKSBzbyBhIGNvbm5lY3RlZCBjbGllbnQgbmV2ZXIKICAg"
    "ICAgICAja2VlcHMgcGxheWluZyBvbiBhbiBhY2NvdW50IHRoYXQgaGFzIGp1c3QgdmFuaXNoZWQg"
    "ZnJvbSB0aGUgREIuCiAgICAgICAgc2VsZi5raWNrUGxheWVyKHVzZXJuYW1lLCByZWFzb249J0Fj"
    "Y291bnQgZGVsZXRlZCBieSBhZG1pbicpCiAgICAgICAgcmV0dXJuIEdESC5kZWxldGVBY2NvdW50"
    "KHVzZXJuYW1lKQojRmFpbGVkLWxvZ2luIHRocm90dGxlLCBwZXIgc291cmNlIElQLgojVHdvIHJl"
    "YXNvbnMgdGhpcyBpcyBub3Qgb3B0aW9uYWwgb24gYSBzZXJ2ZXIgcmVhY2hhYmxlIGZyb20gdGhl"
    "IGludGVybmV0OgojYSBwYXNzd29yZCBndWVzcyBpcyBjaGVhcCBmb3IgdGhlIGF0dGFja2VyIGJ1"
    "dCBjb3N0cyAqdXMqIGEgMTAway1pdGVyYXRpb24KI1BCS0RGMiAodGVucyBvZiBtcyBvZiBDUFUg"
    "ZWFjaCksIHNvIGFuIHVudGhyb3R0bGVkIGxvZ2luIGVuZHBvaW50IGlzIGJvdGggYQojYnJ1dGUt"
    "Zm9yY2Ugb3JhY2xlIGFuZCBhIENQVSBhbXBsaWZpZXIgLSBhIGhhbmRmdWwgb2YgY29ubmVjdGlv"
    "bnMgY2FuIHBpbgojZXZlcnkgY29yZS4gU3VjY2Vzc2Z1bCBsb2dpbnMgY2xlYXIgdGhlIGNvdW50"
    "ZXIsIHNvIGEgcGxheWVyIGZ1bWJsaW5nIHRoZWlyCiNwYXNzd29yZCBhIGZldyB0aW1lcyBpcyBu"
    "ZXZlciBsb2NrZWQgb3V0IGZvciBsb25nLgpfTE9HSU5fRkFJTF9MSU1JVCA9IDYgICAgICAjZmFp"
    "bHVyZXMgYWxsb3dlZCBpbnNpZGUgdGhlIHdpbmRvdyBiZWZvcmUgZGVsYXlpbmcKX0xPR0lOX0ZB"
    "SUxfV0lORE9XID0gMzAwICAgI3NlY29uZHMgYSBmYWlsdXJlIGlzIHJlbWVtYmVyZWQKX0xPR0lO"
    "X0ZBSUxfREVMQVkgPSAyLjAgICAgI3NlY29uZHMgdG8gc3RhbGwgZWFjaCBhdHRlbXB0IG9uY2Ug"
    "b3ZlciB0aGUgbGltaXQKY2xhc3MgTG9naW5UaHJvdHRsZSgpOgogICAgZGVmIF9faW5pdF9fKHNl"
    "bGYpOgogICAgICAgIHNlbGYubG9jayA9IHRocmVhZGluZy5Mb2NrKCkKICAgICAgICBzZWxmLmZh"
    "aWxzID0ge30gI2lwIC0+IFt0aW1lc3RhbXBzXQogICAgZGVmIF9wcnVuZShzZWxmLCBpcCwgbm93"
    "KToKICAgICAgICByZWNlbnQgPSBbdCBmb3IgdCBpbiBzZWxmLmZhaWxzLmdldChpcCwgKCkpIGlm"
    "IG5vdyAtIHQgPCBfTE9HSU5fRkFJTF9XSU5ET1ddCiAgICAgICAgaWYgcmVjZW50OgogICAgICAg"
    "ICAgICBzZWxmLmZhaWxzW2lwXSA9IHJlY2VudAogICAgICAgIGVsc2U6CiAgICAgICAgICAgIHNl"
    "bGYuZmFpbHMucG9wKGlwLCBOb25lKQogICAgICAgIHJldHVybiByZWNlbnQKICAgIGRlZiBkZWxh"
    "eUZvcihzZWxmLCBpcCk6CiAgICAgICAgbm93ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAgIHdp"
    "dGggc2VsZi5sb2NrOgogICAgICAgICAgICByZWNlbnQgPSBzZWxmLl9wcnVuZShpcCwgbm93KQog"
    "ICAgICAgIHJldHVybiBfTE9HSU5fRkFJTF9ERUxBWSBpZiBsZW4ocmVjZW50KSA+PSBfTE9HSU5f"
    "RkFJTF9MSU1JVCBlbHNlIDAuMAogICAgZGVmIHJlY29yZEZhaWx1cmUoc2VsZiwgaXApOgogICAg"
    "ICAgIG5vdyA9IHRpbWUubW9ub3RvbmljKCkKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgcmVjZW50ID0gc2VsZi5fcHJ1bmUoaXAsIG5vdykKICAgICAgICAgICAgcmVjZW50LmFw"
    "cGVuZChub3cpCiAgICAgICAgICAgIHNlbGYuZmFpbHNbaXBdID0gcmVjZW50CiAgICAgICAgICAg"
    "IHJldHVybiBsZW4ocmVjZW50KQogICAgZGVmIHJlY29yZFN1Y2Nlc3Moc2VsZiwgaXApOgogICAg"
    "ICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBzZWxmLmZhaWxzLnBvcChpcCwgTm9uZSkK"
    "TE9HSU5fVEhST1RUTEUgPSBMb2dpblRocm90dGxlKCkKCl9MT0dJTl9FUlJPUlMgPSB7CiAgICAx"
    "OiAnSW52YWxpZCB1c2VybmFtZSBvciBwYXNzd29yZCcsCiAgICAyOiAnQWNjb3VudCBhbHJlYWR5"
    "IGxvZ2dlZCBpbicsCiAgICAzOiAnUGFzc3dvcmQgcmVxdWlyZWQnLAogICAgNDogJ1VzZXJuYW1l"
    "IHJlcXVpcmVkJywKfQpfUkVHSVNURVJfRVJST1JTID0gewogICAgMTogJ0FjY291bnQgYWxyZWFk"
    "eSBsb2dnZWQgaW4nLAogICAgMjogJ1VzZXJuYW1lIHVuYXZhaWxhYmxlIG9yIGludmFsaWQnLAp9"
    "CiNoYW5kbGVzIGluZGl2aWR1YWwgY29ubmVjdGlvbnMKY2xhc3MgQ29ubmVjdGlvbkhhbmRsZXIo"
    "c29ja2V0c2VydmVyLkJhc2VSZXF1ZXN0SGFuZGxlcik6CiAgICAjZGVmYXVsdCBwcm9wZXJ0aWVz"
    "OgogICAgIyAtIHJlcXVlc3Q6IHNvY2tldCB0byBkZXN0aW5hdGlvbgogICAgIyAtIGNsaWVudF9h"
    "ZGRyZXNzCiAgICAjIC0gc2VydmVyOiBDb3JlU2VydmVyCiAgICBfU1RPUFdSSVRFUiA9IG9iamVj"
    "dCgpCiAgICBkZWYgc2V0dXAoc2VsZik6CiAgICAgICAgc2VsZi5fc1F1ZXVlID0gU2ltcGxlUXVl"
    "dWUoKQogICAgICAgIHNlbGYudXNlciA9IE5vbmUKICAgICAgICBzZWxmLmd1aWQgPSBOb25lCiAg"
    "ICAgICAgc2VsZi5kYXRhID0gYicnCiAgICAgICAgc2VsZi5TSyA9IGJ5dGVhcnJheShzdHJ1Y3Qu"
    "cGFjaygnPElJJywgMHhBNkFFMUY5QiwgMHg0MzhERkY0MCkpCiAgICAgICAgI1NlcmlhbGlzZXMg"
    "dGhlIHJhdyBzb2NrZXQgd3JpdGVzLiBUaHJlZSB0aHJlYWRzIGNhbiB3YW50IHRvIHdyaXRlIHRv"
    "CiAgICAgICAgI29uZSBjbGllbnQ6IHRoaXMgY29ubmVjdGlvbidzIG93biByZWFkIGxvb3AgKGR1"
    "cmluZyB0aGUgaGFuZHNoYWtlKSwKICAgICAgICAjaXRzIHdyaXRlciB0aHJlYWQsIGFuZCB0aGUg"
    "R1VJIHRocmVhZCB2aWEga2lja1BsYXllcigpLiBXaXRob3V0IHRoZQogICAgICAgICNsb2NrIHR3"
    "byBzZW5kYWxsKCkgY2FsbHMgY2FuIGludGVybGVhdmUgYW5kIHNwbGl0IGEgcGFja2V0IGRvd24g"
    "dGhlCiAgICAgICAgI21pZGRsZSwgd2hpY2ggdGhlIGNsaWVudCBzZWVzIGFzIHByb3RvY29sIGdh"
    "cmJhZ2UuCiAgICAgICAgc2VsZi5fc2VuZExvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAg"
    "c2VsZi5fd3JpdGVyID0gTm9uZQogICAgICAgIHNlbGYuX3dyaXRlckRlYWQgPSB0aHJlYWRpbmcu"
    "RXZlbnQoKQogICAgICAgIHNlbGYuX2xhc3RSZWN2ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAg"
    "IHNlbGYuc2VydmVyLnJlZ2lzdGVyQ29ubmVjdGlvbihzZWxmKQogICAgICAgIHRyeToKICAgICAg"
    "ICAgICAgI05hZ2xlIGJhdGNoZXMgc21hbGwgd3JpdGVzIGJ5IGhvbGRpbmcgdGhlbSBmb3IgdXAg"
    "dG8gfjQwbXMgd2FpdGluZwogICAgICAgICAgICAjZm9yIG1vcmUgZGF0YS4gRXZlcnkgbWVzc2Fn"
    "ZSB0aGlzIHNlcnZlciBzZW5kcyBpcyBzbWFsbCBhbmQKICAgICAgICAgICAgI2xhdGVuY3ktc2Vu"
    "c2l0aXZlIC0gY2hhdCwgcG9zaXRpb24gdXBkYXRlcyBhbmQgYWJvdmUgYWxsIHRoZQogICAgICAg"
    "ICAgICAjL2dhbWVjb21tYW5kdG91c2VyIHJlbGF5IHRoYXQgY2FycmllcyB0aGUgYWN0dWFsIGlu"
    "LWdhbWUgY28tb3AKICAgICAgICAgICAgI3RyYWZmaWMgYmV0d2VlbiB0d28gcGxheWVycyAtIHNv"
    "IHRoZSBkZWxheSBpcyBwdXJlIGFkZGVkIGxhZy4KICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNl"
    "dHNvY2tvcHQoc29ja2V0LklQUFJPVE9fVENQLCBzb2NrZXQuVENQX05PREVMQVksIDEpCiAgICAg"
    "ICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MgI25vdCBmYXRhbCwganVzdCBzbG93"
    "ZXIKICAgICAgICB0cnk6CiAgICAgICAgICAgICNBc2sgdGhlIE9TIHRvIHByb2JlIGFuIGlkbGUg"
    "Y29ubmVjdGlvbi4gV2hlbiBhIHBsYXllcidzIGdhbWUKICAgICAgICAgICAgI2NyYXNoZXMgb3V0"
    "cmlnaHQgdGhlIHNvY2tldCBpcyB1c3VhbGx5IHJlc2V0IGFuZCB3ZSBmaW5kIG91dCBhdAogICAg"
    "ICAgICAgICAjb25jZSwgYnV0IGEgbWFjaGluZSB0aGF0IGZyZWV6ZXMsIHNsZWVwcyBvciBsb3Nl"
    "cyBpdHMgbGluayBzZW5kcwogICAgICAgICAgICAjbm90aGluZyBhdCBhbGw6IHdpdGhvdXQgcHJv"
    "YmVzIHRoYXQgY29ubmVjdGlvbiBzaXRzIHRoZXJlIGhvbGRpbmcKICAgICAgICAgICAgI3RoZSBh"
    "Y2NvdW50ICgiQWNjb3VudCBhbHJlYWR5IGxvZ2dlZCBpbiIpIGFuZCBpdHMgcm9vbSB1bnRpbCB0"
    "aGUKICAgICAgICAgICAgI2lkbGUgdGltZW91dCBleHBpcmVzIG1pbnV0ZXMgbGF0ZXIuIFByb2Jl"
    "IGFmdGVyIDMwcyBpZGxlLCB0aGVuCiAgICAgICAgICAgICNldmVyeSA1cy4KICAgICAgICAgICAg"
    "c2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LlNPTF9TT0NLRVQsIHNvY2tldC5TT19LRUVQ"
    "QUxJVkUsIDEpCiAgICAgICAgICAgIGlmIGhhc2F0dHIoc2VsZi5yZXF1ZXN0LCAnaW9jdGwnKSBh"
    "bmQgaGFzYXR0cihzb2NrZXQsICdTSU9fS0VFUEFMSVZFX1ZBTFMnKToKICAgICAgICAgICAgICAg"
    "IHNlbGYucmVxdWVzdC5pb2N0bChzb2NrZXQuU0lPX0tFRVBBTElWRV9WQUxTLCAoMSwgMzAwMDAs"
    "IDUwMDApKSAjV2luZG93cwogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgZm9yIChv"
    "cHQsIHZhbCkgaW4gKCgnVENQX0tFRVBJRExFJywgMzApLCAoJ1RDUF9LRUVQSU5UVkwnLCA1KSwK"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAoJ1RDUF9LRUVQQ05UJywgNCkpOgog"
    "ICAgICAgICAgICAgICAgICAgIGlmIGhhc2F0dHIoc29ja2V0LCBvcHQpOgogICAgICAgICAgICAg"
    "ICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2V0c29ja29wdChzb2NrZXQuSVBQUk9UT19UQ1AsIGdl"
    "dGF0dHIoc29ja2V0LCBvcHQpLCB2YWwpCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAg"
    "ICAgIHBhc3MgI2tlZXBhbGl2ZSBpcyBhbiBvcHRpbWlzYXRpb24sIG5vdCBhIHJlcXVpcmVtZW50"
    "CiAgICBkZWYgc2VuZFJhdyhzZWxmLCBtc2cpOgogICAgICAgICNUaGUgc2luZ2xlIGZ1bm5lbCBm"
    "b3IgZXZlcnkgYnl0ZSBsZWF2aW5nIHRoZSBzZXJ2ZXIgb24gdGhpcyBzb2NrZXQuCiAgICAgICAg"
    "d2l0aCBzZWxmLl9zZW5kTG9jazoKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNlbmRhbGwobXNn"
    "KQogICAgZGVmIHNlbmQoc2VsZiwgbXNnKToKICAgICAgICAjTm9ybWFsIHBhdGggb25jZSB0aGUg"
    "Y29ubmVjdGlvbiBpcyBsaXZlOiBoYW5kIG9mZiB0byB0aGUgd3JpdGVyIHRocmVhZAogICAgICAg"
    "ICNzbyB0aGUgY2FsbGVyIChhIGNvbW1hbmQgaGFuZGxlciwgb3IgdGhlIGRpc3RyaWJ1dG9yJ3Mg"
    "ZmFuLW91dCkgbmV2ZXIKICAgICAgICAjYmxvY2tzIG9uIGEgc2xvdyBvciBzdGFsbGVkIGNsaWVu"
    "dC4KICAgICAgICBpZiBtc2c6CiAgICAgICAgICAgIHNlbGYuX3NRdWV1ZS5wdXQobXNnKQogICAg"
    "ZGVmIF93cml0ZXJMb29wKHNlbGYpOgogICAgICAgICNCbG9ja3Mgb24gdGhlIHF1ZXVlIGluc3Rl"
    "YWQgb2YgYmVpbmcgcG9sbGVkLiBQcmV2aW91c2x5IHRoZSByZWFkIGxvb3AKICAgICAgICAjZHJh"
    "aW5lZCB0aGlzIHF1ZXVlIGl0c2VsZiBiZXR3ZWVuIHJlY3YoKSB0aW1lb3V0cywgc28gYW55dGhp"
    "bmcgcXVldWVkCiAgICAgICAgI2p1c3QgYWZ0ZXIgdGhlIHRocmVhZCB3ZW50IGJhY2sgaW50byBy"
    "ZWN2KCkgd2FpdGVkIG91dCB0aGUgZnVsbAogICAgICAgICN0aW1lb3V0IC0gdXAgdG8gMTAwbXMg"
    "b2YgbGF0ZW5jeSBhZGRlZCB0byBldmVyeSByZWxheWVkIGdhbWUgY29tbWFuZCwKICAgICAgICAj"
    "b24gdG9wIG9mIGV2ZXJ5IGlkbGUgY29ubmVjdGlvbiB3YWtpbmcgMTAgdGltZXMgYSBzZWNvbmQg"
    "dG8gY2hlY2suCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3aGlsZSBUcnVlOgogICAgICAgICAg"
    "ICAgICAgbXNnID0gc2VsZi5fc1F1ZXVlLmdldCgpCiAgICAgICAgICAgICAgICBpZiBtc2cgaXMg"
    "c2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICAg"
    "ICAgI0NvYWxlc2NlIHdoYXRldmVyIGVsc2UgcGlsZWQgdXAgYmVoaW5kIGl0IGludG8gYSBzaW5n"
    "bGUgd3JpdGUuCiAgICAgICAgICAgICAgICAjUG9zaXRpb24gYnJvYWRjYXN0cyBhbmQgZ2FtZSBj"
    "b21tYW5kcyBvZnRlbiBhcnJpdmUgaW4gYnVyc3RzLgogICAgICAgICAgICAgICAgY2h1bmtzID0g"
    "W21zZ10KICAgICAgICAgICAgICAgIHdoaWxlIFRydWU6CiAgICAgICAgICAgICAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICAgICAgICAgICAgICBueHQgPSBzZWxmLl9zUXVldWUuZ2V0X25vd2FpdCgp"
    "CiAgICAgICAgICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgYnJlYWsKICAgICAgICAgICAgICAgICAgICBpZiBueHQgaXMgc2VsZi5fU1RPUFdSSVRF"
    "UjoKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KGInJy5qb2luKGNodW5rcykp"
    "CiAgICAgICAgICAgICAgICAgICAgICAgIHJldHVybgogICAgICAgICAgICAgICAgICAgIGNodW5r"
    "cy5hcHBlbmQobnh0KQogICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KGInJy5qb2luKGNodW5r"
    "cykpCiAgICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVzZXRFcnJvciwgQ29ubmVjdGlvbkFib3J0"
    "ZWRFcnJvciwgQnJva2VuUGlwZUVycm9yLCBPU0Vycm9yKToKICAgICAgICAgICAgcGFzcyAjcGVl"
    "ciBpcyBnb25lOyB0aGUgcmVhZCBsb29wIG5vdGljZXMgYW5kIHJ1bnMgdGhlIGNsZWFudXAKICAg"
    "ICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwcmludCgnW0xvYmJ5XSBXcml0ZXIg"
    "ZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgICAgICBmaW5hbGx5OgogICAg"
    "ICAgICAgICBzZWxmLl93cml0ZXJEZWFkLnNldCgpCiAgICBkZWYgX3N0YXJ0V3JpdGVyKHNlbGYp"
    "OgogICAgICAgIHNlbGYuX3dyaXRlciA9IHRocmVhZGluZy5UaHJlYWQodGFyZ2V0PXNlbGYuX3dy"
    "aXRlckxvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNlbGYuX3dyaXRlci5zdGFydCgpCiAgICBk"
    "ZWYgX3N0b3BXcml0ZXIoc2VsZik6CiAgICAgICAgaWYgc2VsZi5fd3JpdGVyIGlzIE5vbmU6CiAg"
    "ICAgICAgICAgIHJldHVybgogICAgICAgIHNlbGYuX3NRdWV1ZS5wdXQoc2VsZi5fU1RPUFdSSVRF"
    "UikKICAgICAgICBzZWxmLl93cml0ZXIuam9pbih0aW1lb3V0PTIuMCkKICAgICAgICBzZWxmLl93"
    "cml0ZXIgPSBOb25lCiAgICBkZWYgX2NsYWltU2Vzc2lvbihzZWxmKToKICAgICAgICAjVGFrZSBv"
    "d25lcnNoaXAgb2YgdGhlIHVzZXJuYW1lIHNsb3QgYmVmb3JlIHRlbGxpbmcgdGhlIGNsaWVudCBp"
    "dCBpcwogICAgICAgICNsb2dnZWQgaW4uIFJldHVybnMgRmFsc2UgaWYgYW5vdGhlciBjb25uZWN0"
    "aW9uIGdvdCB0aGVyZSBmaXJzdC4KICAgICAgICBpZiBzZWxmLnNlcnZlci5zdGF0ZS5jbGFpbVVz"
    "ZXIoc2VsZi51c2VyLm5hbWUsIHNlbGYpOgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAg"
    "IHNlbGYudXNlci5kaXNjb25uZWN0KHNlbGYuc2VydmVyKSAjcmVsZWFzZXMgdGhlIGlkbnVtIHdl"
    "IGp1c3QgYWxsb2NhdGVkCiAgICAgICAgc2VsZi51c2VyID0gTm9uZQogICAgICAgIHJldHVybiBG"
    "YWxzZQogICAgZGVmIGF0dGVtcHRMb2dpbihzZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQpOgogICAg"
    "ICAgIGlmIGxlbih1c2VybmFtZSk8MToKICAgICAgICAgICAgcmV0dXJuIDQgI05vIFVzZXJuYW1l"
    "LCBsaWtlbHkgZnJlc2ggbG9naW4KICAgICAgICAgICAgI1RPRE8gY2hlY2sgaWYgc2VyaWFsIGV4"
    "aXN0cyBhbmQgcmV0dXJuIHVzZXJuYW1lIHByb3Blcmx5CiAgICAgICAgaWYgbGVuKHBhc3N3b3Jk"
    "KTwxOgogICAgICAgICAgICByZXR1cm4gMyAjUGFzc3dvcmQgdG9vIHNob3J0CiAgICAgICAgI1Rl"
    "c3QgaWYgcGxheWVyIGFscmVhZHkgbG9nZ2VkIGluIChmYXN0IHBhdGg7IHRoZSBhdXRob3JpdGF0"
    "aXZlLAogICAgICAgICNyYWNlLWZyZWUgY2hlY2sgaXMgdGhlIGNsYWltVXNlcigpIGJlbG93KQog"
    "ICAgICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1c2VybmFtZSk6CiAgICAgICAgICAgIHJl"
    "dHVybiAyICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJST1IKICAgICAgICAjcGxheWVyIG5vdCBj"
    "dXJyZW50bHkgbG9nZ2VkIGluLCBhdHRlbXB0IHRvIGxvZ2luIHZpYSBkYXRhIGhhbmRsZXIKICAg"
    "ICAgICBzZWxmLnVzZXIgPSBHREgubG9naW5QbGF5ZXIodXNlcm5hbWUsIHNlbGYsIHBhc3N3b3Jk"
    "KQogICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgcmV0dXJuIDAgaWYgc2VsZi5fY2xh"
    "aW1TZXNzaW9uKCkgZWxzZSAyCiAgICAgICAgcmV0dXJuIDEgI1RPRE8gR2V0IGZyb20gR0RILmxv"
    "Z2luUGxheWVyLCBwYXNzIHVzZXIgb2JqZWN0IGFsb25nPwogICAgZGVmIGF0dGVtcHRSZWdpc3Rl"
    "cihzZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIs"
    "IGRlc2NyaXB0aW9uKToKICAgICAgICAjVGVzdCBpZiBwbGF5ZXIgYWxyZWFkeSBsb2dnZWQgaW4K"
    "ICAgICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIodXNlcm5hbWUpOgogICAgICAgICAgICBy"
    "ZXR1cm4gMSAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVSUk9SCiAgICAgICAgc2VsZi51c2VyID0g"
    "R0RILnJlZ2lzdGVyUGxheWVyKHVzZXJuYW1lLCBzZWxmLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0"
    "aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICAgICAgaWYgc2VsZi51c2VyOgogICAg"
    "ICAgICAgICByZXR1cm4gMCBpZiBzZWxmLl9jbGFpbVNlc3Npb24oKSBlbHNlIDEKICAgICAgICBy"
    "ZXR1cm4gMiAjVE9ETyBnZXQgZXJyb3IgZnJvbSBHREgKICAgIGRlZiBoYW5kbGUoc2VsZik6CiAg"
    "ICAgICAgdHJ5OiAjSW50ZXJjZXB0IGFuZCBwcmludCBlcnJvcnMgZm9yIGRlYnVnZ2luZwogICAg"
    "ICAgICAgICBzZWxmLl9oYW5kbGUoKQogICAgICAgICAgICAjVE9ETyBsb29wIGxvYmJ5IGhhbmRs"
    "ZSBiZXR0ZXIgdG8gaGFuZGxlIGV4Y2VwdGlvbnMgZ3JhY2VmdWxseQogICAgICAgICAgICBzZWxm"
    "Ll9sb2JieUhhbmRsZSgpCiAgICAgICAgZXhjZXB0IFByb3RvY29sRXJyb3IgYXMgZToKICAgICAg"
    "ICAgICAgI21hbGZvcm1lZC9vdmVyc2l6ZWQgaW5wdXQgLSB0aGUgY2xpZW50J3MgZmF1bHQsIG5v"
    "dCBvdXJzLiBEcm9wIHRoZQogICAgICAgICAgICAjY29ubmVjdGlvbiB3aXRoIG9uZSBsaW5lIGlu"
    "c3RlYWQgb2YgYSB0cmFjZWJhY2suCiAgICAgICAgICAgIHdobyA9IHNlbGYudXNlci5uYW1lIGlm"
    "IHNlbGYudXNlciBlbHNlIHNlbGYuY2xpZW50X2FkZHJlc3NbMF0KICAgICAgICAgICAgcHJpbnQo"
    "ZidbTG9iYnldIFByb3RvY29sIGVycm9yIGZyb20ge3dob306IHtlfScpCiAgICAgICAgZXhjZXB0"
    "ICh6bGliLmVycm9yLCBzdHJ1Y3QuZXJyb3IsIFVuaWNvZGVEZWNvZGVFcnJvcikgYXMgZToKICAg"
    "ICAgICAgICAgI3RydW5jYXRlZC9nYXJiYWdlIHBhY2tldDogcGFyc2VEc3RyIGFuZCBzdHJ1Y3Qu"
    "dW5wYWNrIGJvdGggcmFpc2Ugb24KICAgICAgICAgICAgI3Nob3J0IHJlYWRzLCBhbmQgLmRlY29k"
    "ZSgpIG9uIG5vbi1hc2NpaSBqdW5rLiBTYW1lIGNhdGVnb3J5LgogICAgICAgICAgICBwcmludChm"
    "J1tMb2JieV0gTWFsZm9ybWVkIHBhY2tldCBmcm9tIHtzZWxmLmNsaWVudF9hZGRyZXNzWzBdfTog"
    "JwogICAgICAgICAgICAgICAgICBmJ3t0eXBlKGUpLl9fbmFtZV9ffToge2V9JykKICAgICAgICBl"
    "eGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25uZWN0aW9uQWJvcnRlZEVycm9yLCBPU0Vy"
    "cm9yKSBhcyBlOgogICAgICAgICAgICAjIGV4cGVjdGVkIGZvcm0gb2YgZGlzY29ubmVjdGlvbiAo"
    "aW5jbHVkaW5nIGEgZm9yY2VkIGFkbWluIGtpY2spLAogICAgICAgICAgICAjIGJ1dCBsZWF2ZSBh"
    "IG9uZS1saW5lIGJyZWFkY3J1bWIgcmF0aGVyIHRoYW4gc3RheWluZyBmdWxseSBzaWxlbnQKICAg"
    "ICAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIENv"
    "bm5lY3Rpb24gY2xvc2VkIGZvciB7c2VsZi51c2VyLm5hbWV9OiB7ZX0nKQogICAgICAgIGV4Y2Vw"
    "dCBFeGNlcHRpb246IyBhcyBlOgogICAgICAgICAgICBwcmludCh0cmFjZWJhY2suZm9ybWF0X2V4"
    "YygpKQogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgICAgICBwcmludChmJ1Vz"
    "ZXI6IHtzZWxmLnVzZXIubmFtZX0nKQogICAgICAgICAgICAjcmFpc2UgZQogICAgZGVmIF9sb2Ji"
    "eUhhbmRsZShzZWxmKToKICAgICAgICAjYWN0aXZlVXNlcnNbLi4uXSA9IHNlbGYgdXNlZCB0byBo"
    "YXBwZW4gaGVyZTsgaXQgbm93IGhhcHBlbnMgdW5kZXIgYQogICAgICAgICNsb2NrIGluc2lkZSBh"
    "dHRlbXB0TG9naW4vYXR0ZW1wdFJlZ2lzdGVyLCBiZWZvcmUgdGhlIHdlbGNvbWUgcGFja2V0CiAg"
    "ICAgICAgI2dvZXMgb3V0LCBzbyB0d28gbG9naW5zIGZvciBvbmUgYWNjb3VudCBjYW4ndCBib3Ro"
    "IHN1Y2NlZWQuCiAgICAgICAgcHJpbnQoZidVc2VyOiB7c2VsZi51c2VyLm5hbWV9IENvbm5lY3Rl"
    "ZCcpCiAgICAgICAgI0Zyb20gaGVyZSBvbiBub3RoaW5nIHdyaXRlcyB0byB0aGUgc29ja2V0IGlu"
    "bGluZTogdGhlIHdyaXRlciB0aHJlYWQKICAgICAgICAjb3ducyB0aGUgb3V0Ym91bmQgZGlyZWN0"
    "aW9uIGFuZCB0aGlzIGxvb3Agb25seSByZWFkcy4KICAgICAgICBzZWxmLl9zdGFydFdyaXRlcigp"
    "CiAgICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgI1RoZSBz"
    "b2NrZXQgc3RheXMgaW4gYmxvY2tpbmcgbW9kZSBmb3IgaXRzIHdob2xlIGxpZmUgZnJvbSBoZXJl"
    "IG9uLCBhbmQKICAgICAgICAjcmVhZGluZXNzIGlzIHdhaXRlZCBmb3Igd2l0aCBzZWxlY3QoKSBp"
    "bnN0ZWFkIG9mIGEgc29ja2V0IHRpbWVvdXQuCiAgICAgICAgI1RoaXMgaXMgbm90IGEgc3R5bGUg"
    "cHJlZmVyZW5jZSAtIGEgc29ja2V0IHRpbWVvdXQgaXMgYSBwcm9wZXJ0eSBvZiB0aGUKICAgICAg"
    "ICAjKnNvY2tldCosIG5vdCBvZiB0aGUgY2FsbCwgc28gdGhlIHNldHRpbWVvdXQoX1JFQURfVElN"
    "RU9VVCkgdGhpcyBsb29wCiAgICAgICAgI3VzZWQgdG8gZG8gb24gZXZlcnkgcGFzcyBhbHNvIGFy"
    "bWVkIGEgMXMgdGltZW91dCBvbiB0aGUgd3JpdGVyCiAgICAgICAgI3RocmVhZCdzIGNvbmN1cnJl"
    "bnQgc2VuZGFsbCgpLiBBIGNsaWVudCB3aG9zZSByZWNlaXZlIHdpbmRvdyB3YXMgZnVsbAogICAg"
    "ICAgICNmb3IgYSBzZWNvbmQgKGV4YWN0bHkgdGhlIGNhc2UgZHVyaW5nIGEgYnVzeSBjby1vcCBz"
    "ZXNzaW9uKSBtYWRlIHRoYXQKICAgICAgICAjc2VuZGFsbCgpIHJhaXNlIFRpbWVvdXRFcnJvciAq"
    "YWZ0ZXIgaGF2aW5nIGFscmVhZHkgd3JpdHRlbiBwYXJ0IG9mIHRoZQogICAgICAgICNwYWNrZXQq"
    "OiB0aGUgd3JpdGVyIHRocmVhZCBkaWVkLCBhbmQgd2hhdGV2ZXIgdGhlIGNsaWVudCBoYWQgcmVj"
    "ZWl2ZWQKICAgICAgICAjd2FzIGhhbGYgYSBtZXNzYWdlLCBzbyBpdHMgY29tbWFuZCBzdHJlYW0g"
    "d2FzIGRlc3luY2hyb25pc2VkIGZyb20KICAgICAgICAjdGhhdCBwb2ludCBvbi4gc2VsZWN0KCkg"
    "bGVhdmVzIHRoZSBzb2NrZXQgYmxvY2tpbmcsIHNvIHdyaXRlcyBhcmUKICAgICAgICAjbmV2ZXIg"
    "aW50ZXJydXB0ZWQsIHdoaWxlIHJlYWRzIHN0aWxsIHdha2UgdXAgcmVndWxhcmx5IGVub3VnaCB0"
    "bwogICAgICAgICNub3RpY2Ugc2h1dGRvd24gYW5kIHRoZSBpZGxlIGRlYWRsaW5lLgogICAgICAg"
    "IHNlbGYucmVxdWVzdC5zZXR0aW1lb3V0KE5vbmUpCiAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAg"
    "ICAgICAgaWYgc2VsZi5fd3JpdGVyRGVhZC5pc19zZXQoKToKICAgICAgICAgICAgICAgIGJyZWFr"
    "ICNwZWVyIHdlbnQgYXdheSB3aGlsZSB3ZSB3ZXJlIHNlbmRpbmcKICAgICAgICAgICAgaWYgc2Vs"
    "Zi5zZXJ2ZXIuX2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAgICBicmVhayAjc2VydmVyIGlzIHN0"
    "b3BwaW5nIC0gY2hlY2tlZCBoZXJlLCBub3Qgb25seSBvbiBhbiBpZGxlCiAgICAgICAgICAgICAg"
    "ICAgICAgICAjdGltZW91dCwgc28gYSBjbGllbnQgdGhhdCBrZWVwcyB0YWxraW5nIGNhbm5vdCBr"
    "ZWVwIGl0cwogICAgICAgICAgICAgICAgICAgICAgI2hhbmRsZXIgdGhyZWFkIChhbmQgaXRzIGxv"
    "ZyBzcGFtKSBhbGl2ZSBwYXN0IHNodXRkb3duCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAg"
    "ICAgIHJlYWR5LCBfLCBfID0gc2VsZWN0LnNlbGVjdChbc2VsZi5yZXF1ZXN0XSwgW10sIFtdLCBf"
    "UkVBRF9USU1FT1VUKQogICAgICAgICAgICBleGNlcHQgKE9TRXJyb3IsIFZhbHVlRXJyb3IpOgog"
    "ICAgICAgICAgICAgICAgYnJlYWsgI3NvY2tldCBjbG9zZWQgdW5kZXIgdXMgKGFkbWluIGtpY2sg"
    "LyBzaHV0ZG93bikKICAgICAgICAgICAgaWYgbm90IHJlYWR5OgogICAgICAgICAgICAgICAgaWYg"
    "c2VsZi5zZXJ2ZXIuX2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAgICAgICAgYnJlYWsgI1NlcnZl"
    "ciBTaHV0dGluZyBkb3duCiAgICAgICAgICAgICAgICBpZiBfSURMRV9USU1FT1VUIGFuZCAodGlt"
    "ZS5tb25vdG9uaWMoKSAtIHNlbGYuX2xhc3RSZWN2KSA+IF9JRExFX1RJTUVPVVQ6CiAgICAgICAg"
    "ICAgICAgICAgICAgI0hhbGYtb3BlbiBjb25uZWN0aW9uOiB0aGUgcGVlciBpcyB1bnJlYWNoYWJs"
    "ZSBidXQgbmV2ZXIKICAgICAgICAgICAgICAgICAgICAjc2VudCBhIEZJTi9SU1QsIHNvIHJlY3Yo"
    "KSBibG9ja3MgZm9yZXZlciBhbmQgdGhlIGFjY291bnQKICAgICAgICAgICAgICAgICAgICAjc3Rh"
    "eXMgY2xhaW1lZC4gUmVhcCBpdCBzbyB0aGUgcGxheWVyIGNhbiBsb2cgYmFjayBpbi4KICAgICAg"
    "ICAgICAgICAgICAgICBwcmludChmJ1tMb2JieV0ge3NlbGYudXNlci5uYW1lfSBpZGxlIGZvciB7"
    "X0lETEVfVElNRU9VVH1zLCBkcm9wcGluZycpCiAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAg"
    "ICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHJtc2cgPSBzZWxmLnJlcXVlc3QucmVj"
    "dihSRUNWX0JVRl9MRU4pICNUT0RPIGxvZyBuZXR3b3JrIGJ5dGVyYXRlCiAgICAgICAgICAgIGlm"
    "IG5vdCBybXNnOgogICAgICAgICAgICAgICAgYnJlYWsgI0Rpc2Nvbm5lY3RlZAogICAgICAgICAg"
    "ICBzZWxmLmRhdGErPXJtc2cKICAgICAgICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90"
    "b25pYygpCiAgICAgICAgICAgIHdoaWxlIHNlbGYuZGF0YToKICAgICAgICAgICAgICAgIHRyeToK"
    "ICAgICAgICAgICAgICAgICAgICBjbWRfbCA9IHNlbGYuZGF0YS5pbmRleCgwKQogICAgICAgICAg"
    "ICAgICAgZXhjZXB0IFZhbHVlRXJyb3I6CiAgICAgICAgICAgICAgICAgICAgI3ByaW50KCdjbWQg"
    "ZGVjb2RlIGVycm9yOlxuJywgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgICAgICAgICAgICAg"
    "ICAgICBicmVhazsjTWF5IHJlcXVpcmUgbW9yZSBkYXRhCiAgICAgICAgICAgICAgICBjbWQgPSB3"
    "aXJlX2RlY29kZShzZWxmLmRhdGFbMDpjbWRfbF0pCiAgICAgICAgICAgICAgICBzZWxmLmRhdGEg"
    "PSBzZWxmLmRhdGFbY21kX2wrMTpdCiAgICAgICAgICAgICAgICByZXNwb25zZSA9IHNlbGYuc2Vy"
    "dmVyLmNvbXBhcnMucGFyc2UoY21kLCBzZWxmKQogICAgICAgICAgICAgICAgaWYgcmVzcG9uc2U6"
    "CiAgICAgICAgICAgICAgICAgICAgI1F1ZXVlZCByYXRoZXIgdGhhbiBzZW50IGlubGluZSwgc28g"
    "dGhpcyBjb25uZWN0aW9uIGhhcyBhCiAgICAgICAgICAgICAgICAgICAgI3NpbmdsZSBvcmRlcmVk"
    "IG91dGJvdW5kIHN0cmVhbS4gU2VuZGluZyBoZXJlIGRpcmVjdGx5CiAgICAgICAgICAgICAgICAg"
    "ICAgI3dvdWxkIHJhY2UgdGhlIHdyaXRlciB0aHJlYWQgYW5kIGNvdWxkIGxhbmQgaW4gdGhlIG1p"
    "ZGRsZQogICAgICAgICAgICAgICAgICAgICNvZiBhIGJyb2FkY2FzdCBpdCBpcyBhbHJlYWR5IHdy"
    "aXRpbmcuCiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kKHJlc3BvbnNlKQogICAgICAgICAg"
    "ICAgICAgI0xvb3NlIGJsb2JzIHNob3VsZCBub3QgaGFwcGVuIGFueW1vcmUgaG9wZWZ1bGx5CiAg"
    "ICAgICAgICAgICAgICAjVE9ETyBmaXggdW5jb21wcmVzc2VkIGRhdGEgYmxvYnM/CiAgICAgICAg"
    "ICAgICAgICAjVE9ETyBza2lwIDEgYnl0ZSBvbmx5IHdoZW4gZGVjb2RlIGVycm9yPwogICAgICAg"
    "ICAgICAgICAgaWYgKGxlbihzZWxmLmRhdGEpPjIgYW5kCiAgICAgICAgICAgICAgICAgICAgICAg"
    "IHNlbGYuZGF0YVswXT09MHg3OCBhbmQKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5kYXRh"
    "WzFdPT0weDljKToKICAgICAgICAgICAgICAgICAgICAjTG9vc2UgdW5oYW5kbGVkIGJsb2IgYWZ0"
    "ZXIgY29tbWFuZAogICAgICAgICAgICAgICAgICAgIGJsb2IsIHNlbGYuZGF0YSA9IHBfZ2V0Qmxv"
    "YihzZWxmLmRhdGEsIHNlbGYucmVxdWVzdCkKICAgICAgICAgICAgICAgICAgICAjVGhlIG90aGVy"
    "IGJsaW5kIHNwb3Q6IGFueXRoaW5nIHRoZSBjbGllbnQgc2VuZHMgYXMgYQogICAgICAgICAgICAg"
    "ICAgICAgICNjb21wcmVzc2VkIGJsb2IgcmF0aGVyIHRoYW4gYSB0ZXh0IGNvbW1hbmQgd2FzIHJl"
    "YWQgYW5kCiAgICAgICAgICAgICAgICAgICAgI3Rocm93biBhd2F5IHdpdGhvdXQgYSB0cmFjZS4K"
    "ICAgICAgICAgICAgICAgICAgICBpZiBfREVCVUdfTE9HX0NPTU1BTkRTOgogICAgICAgICAgICAg"
    "ICAgICAgICAgICB3aG8gPSBzZWxmLnVzZXIubmFtZSBpZiBzZWxmLnVzZXIgZWxzZSAnPycKICAg"
    "ICAgICAgICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSAtPiAoVU5IQU5ETEVEIEJM"
    "T0IgYWZ0ZXIge2NtZCFyfSkgJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmJ3tsZW4o"
    "YmxvYil9IGJ5dGVzJykKICAgIGRlZiBfcmVjdk1vcmUoc2VsZik6CiAgICAgICAgY2h1bmsgPSBz"
    "ZWxmLnJlcXVlc3QucmVjdihSRUNWX0JVRl9MRU4pCiAgICAgICAgaWYgbm90IGNodW5rOgogICAg"
    "ICAgICAgICAjcGVlciBkaXNjb25uZWN0ZWQgZHVyaW5nIGhhbmRzaGFrZS9sb2dpbiwgc3RvcCB0"
    "aGUgYnVzeS1sb29wCiAgICAgICAgICAgIHJhaXNlIENvbm5lY3Rpb25SZXNldEVycm9yKCdkaXNj"
    "b25uZWN0ZWQgZHVyaW5nIGxvZ2luJykKICAgICAgICBzZWxmLmRhdGEgKz0gY2h1bmsKICAgIGRl"
    "ZiBfaGFuZGxlKHNlbGYpOgogICAgICAgICNUT0RPIGxvZyBsb2dpbiBhdHRlbXB0cz8KICAgICAg"
    "ICBwZWVyX2lwID0gc2VsZi5jbGllbnRfYWRkcmVzc1swXQogICAgICAgIHByaW50KCdDb25uZWN0"
    "aW9uIGF0dGVtcHQgZnJvbTonLCBwZWVyX2lwKQogICAgICAgIExJUyA9IDIgI2xvZ2luIHN0YXRl"
    "ICNUT0RPIGNvbnNpZGVyIGxvbmcgdGltZW91dHM/CiAgICAgICAgd2hpbGUgTElTOgogICAgICAg"
    "ICAgICB3aGlsZSBsZW4oc2VsZi5kYXRhKTw0OgogICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1v"
    "cmUoKQogICAgICAgICAgICBwYWNrX2xlbiA9IHN0cnVjdC51bnBhY2soJzxJJyxzZWxmLmRhdGFb"
    "MDo0XSlbMF0KICAgICAgICAgICAgaWYgcGFja19sZW4gPCA0IG9yIHBhY2tfbGVuID4gX01BWF9I"
    "QU5EU0hBS0U6CiAgICAgICAgICAgICAgICAjdW52YWxpZGF0ZWQsIHRoaXMgaXMgYSBwcmUtYXV0"
    "aGVudGljYXRpb24gbWVtb3J5IGJvbWI6IGFuCiAgICAgICAgICAgICAgICAjdW5hdXRoZW50aWNh"
    "dGVkIHBlZXIgYW5ub3VuY2VzIGEgNEdCIHBhY2tldCBhbmQgdGhlIGxvb3AgYmVsb3cKICAgICAg"
    "ICAgICAgICAgICNidWZmZXJzIHVudGlsIHRoZSBwcm9jZXNzIGRpZXMKICAgICAgICAgICAgICAg"
    "IHJhaXNlIFByb3RvY29sRXJyb3IoZidoYW5kc2hha2UgcGFja2V0IGxlbmd0aCB7cGFja19sZW59"
    "IG91dCBvZiByYW5nZScpCiAgICAgICAgICAgIHdoaWxlKGxlbihzZWxmLmRhdGEpPHBhY2tfbGVu"
    "KToKICAgICAgICAgICAgICAgIHNlbGYuX3JlY3ZNb3JlKCkKICAgICAgICAgICAgI3NsaWNlIHRv"
    "IHBhY2tfbGVuIChub3QgdG8gdGhlIGVuZCBvZiB0aGUgYnVmZmVyKTogYW55dGhpbmcgcGFzdAog"
    "ICAgICAgICAgICAjdGhpcyBwYWNrZXQgYmVsb25ncyB0byB0aGUgbmV4dCBvbmUuIEJvdW5kZWQg"
    "ZGVjb21wcmVzcywgYmVjYXVzZSBhCiAgICAgICAgICAgICM2NGsgaGFuZHNoYWtlIG9mIGNvbXBy"
    "ZXNzZWQgemVyb2VzIGV4cGFuZHMgdG8gaHVuZHJlZHMgb2YgTUIuCiAgICAgICAgICAgIHJlcyA9"
    "IF9kZWNvbXByZXNzX2JvdW5kZWQoc2VsZi5kYXRhWzQ6cGFja19sZW5dLCBfTUFYX0hBTkRTSEFL"
    "RV9JTkZMQVRFRCkKICAgICAgICAgICAgc2VsZi5kYXRhID0gc2VsZi5kYXRhW3BhY2tfbGVuOl0K"
    "ICAgICAgICAgICAgaWYgTElTID09IDI6CiAgICAgICAgICAgICAgICBnYW1ldmVyc2lvbiA9IHJl"
    "c1swOjE2XSAjVE9ETyBub3RlIGdhbWUgdmVyc2lvbiAodW52ZXJpZmllZCkgcGVyIHVzZXIKICAg"
    "ICAgICAgICAgICAgIGxhbmduYW1lLCBvZmYgPSBwYXJzZURzdHIocmVzLCAxNikKICAgICAgICAg"
    "ICAgICAgICNUT0RPIGNvbnNpZGVyIFRXU0UgaW5kaWNhdG9yIHRvIGNyZWF0ZSBzZWN1cmUgY29u"
    "bmVjdGlvbj8KICAgICAgICAgICAgICAgICNUT0RPIGNoZWNrIGlmIHZhbmlsbGEgc2VydmVyIGln"
    "bm9yZXMgZXh0cmEgZGF0YSBpbiBoYW5kc2hha2UgcHJvY2VzcwogICAgICAgICAgICAgICAgUksg"
    "PSByZXNbb2ZmKzg6b2ZmKzE2XQogICAgICAgICAgICAgICAgZm9yIGkgaW4gcmFuZ2UobGVuKFJL"
    "KSk6CiAgICAgICAgICAgICAgICAgICAgc2VsZi5TS1tpXV49UktbaV0KICAgICAgICAgICAgICAg"
    "ICN3YXMgaGFyZGNvZGVkICdUVzFDUycgd2l0aCBhICJTRVJWRVIgTkFNRSBjZmdUT0RPIiBub3Rl"
    "OiB0aGUKICAgICAgICAgICAgICAgICNuYW1lIGNvbmZpZ3VyZWQgaW4gQ29uZmlnLmluaS90aGUg"
    "R1VJIHJlYWNoZWQgdGhlIHdlbGNvbWUKICAgICAgICAgICAgICAgICNwYWNrZXQgYnV0IG5ldmVy"
    "IHRoaXMgb25lLCBzbyB0aGUgcHJlLWxvZ2luIGhhbmRzaGFrZSBhbHdheXMKICAgICAgICAgICAg"
    "ICAgICNhbm5vdW5jZWQgdGhlIHBsYWNlaG9sZGVyLgogICAgICAgICAgICAgICAgc2VsZi5zZW5k"
    "UmF3KF9zZXJ2ZXJfaW5mb19wYWNrZXQoc2FuaXRpemVUZXh0KERFRkFVTFRfVElUTEUpKSkKICAg"
    "ICAgICAgICAgICAgICNUT0RPIFRXMUNTIGluZGljYXRvciBmb3IgVFdTRSBjbGllbnQgdG8gY3Jl"
    "YXRlIHNlY3VyZSBjb25uZWN0aW9uIG9yIHByZS1oYXNoIHBhc3N3b3JkPwogICAgICAgICAgICAg"
    "ICAgTElTID0gMSAKICAgICAgICAgICAgICAgIHNlbGYuU0sgPSBieXRlcyhzZWxmLlNLKQogICAg"
    "ICAgICAgICBlbGlmIExJUyA9PSAxOgogICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IC0xCiAg"
    "ICAgICAgICAgICAgICAjU3RhbGwgcmVwZWF0IG9mZmVuZGVycyBiZWZvcmUgZG9pbmcgYW55IFBC"
    "S0RGMiB3b3JrIGZvciB0aGVtLgogICAgICAgICAgICAgICAgI1NsZWVwaW5nIGluIHRoaXMgaGFu"
    "ZGxlciB0aHJlYWQgaXMgdGhlIHBvaW50OiBpdCBjb3N0cyB1cwogICAgICAgICAgICAgICAgI25v"
    "dGhpbmcgYW5kIHJhdGUtbGltaXRzIHRoYXQgY29ubmVjdGlvbi4KICAgICAgICAgICAgICAgIGRl"
    "bGF5ID0gTE9HSU5fVEhST1RUTEUuZGVsYXlGb3IocGVlcl9pcCkKICAgICAgICAgICAgICAgIGlm"
    "IGRlbGF5OgogICAgICAgICAgICAgICAgICAgIHRpbWUuc2xlZXAoZGVsYXkpCiAgICAgICAgICAg"
    "ICAgICB1c2VybmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywgMCkKICAgICAgICAgICAgICAgIHBh"
    "c3N3b3JkLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAgICAgICAgICAjVE9ETyBU"
    "V1NFIG1vZCBmb3IgaGlnaGVyIGxvZ2luIHNlY3VyaXR5CiAgICAgICAgICAgICAgICAjLWVuY3J5"
    "cHRlZCBjb25uZWN0aW9uIHRvIHByZXZlbnQgcmVwbGF5IGF0dGFja3MKICAgICAgICAgICAgICAg"
    "ICMtcHJlaGFzaCBwYXNzd29yZCB3aXRoIHNlcmlhbD8sIGNoZWNrIGlmIHJlY292ZXJ5IHBvc3Np"
    "YmxlLgogICAgICAgICAgICAgICAgc2VsZi5ndWlkID0gYnl0ZXMocmVzW29mZjpvZmYrMTZdKQog"
    "ICAgICAgICAgICAgICAgI3ByaW50KCdndWlkIGJ5dGU6Jywgc2VsZi5ndWlkWzFdKQogICAgICAg"
    "ICAgICAgICAgI3NlbGYuZ3VpZCA9IGJ5dGVhcnJheShyZXNbb2ZmOm9mZisxNl0pCiAgICAgICAg"
    "ICAgICAgICAjc2VsZi5ndWlkWzFdXj0weDE2ICNETyBOT1QgcGVyZm9ybSBzZXJ2ZXJzaWRlCiAg"
    "ICAgICAgICAgICAgICAjc2VsZi5ndWlkID0gYnl0ZXMoc2VsZi5ndWlkKQogICAgICAgICAgICAg"
    "ICAgb2ZmKz0xNgogICAgICAgICAgICAgICAgaXNyZWcgPSBzdHJ1Y3QudW5wYWNrKCc8SScscmVz"
    "W29mZjpvZmYrNF0pWzBdCiAgICAgICAgICAgICAgICBvZmYrPTQKICAgICAgICAgICAgICAgIHZp"
    "YVJlZ2lzdGVyID0gYm9vbChpc3JlZykKICAgICAgICAgICAgICAgIGlmIGlzcmVnOgogICAgICAg"
    "ICAgICAgICAgICAgIGVtYWlsLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAgICAg"
    "ICAgICAgICAgbG9jYXRpb24sIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAg"
    "ICAgICAgICBhZ2UgPSByZXNbb2ZmXQogICAgICAgICAgICAgICAgICAgIGdlbmRlciA9IHJlc1tv"
    "ZmYrMV0KICAgICAgICAgICAgICAgICAgICBvZmYrPTIgI2FnZSwgZ2VuZGVyCiAgICAgICAgICAg"
    "ICAgICAgICAgZGVzY3JpcHRpb24sIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAg"
    "ICAgICAgICAgICBsb2dpbkVycm9yID0gc2VsZi5hdHRlbXB0UmVnaXN0ZXIodXNlcm5hbWUsIHBh"
    "c3N3b3JkLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbikKICAgICAg"
    "ICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IHNlbGYuYXR0"
    "ZW1wdExvZ2luKHVzZXJuYW1lLCBwYXNzd29yZCkKICAgICAgICAgICAgICAgICAgICBpZiBsb2dp"
    "bkVycm9yID09IDEgYW5kIF9BVVRPX1JFR0lTVEVSOgogICAgICAgICAgICAgICAgICAgICAgICB2"
    "aWFSZWdpc3RlciA9IFRydWUKICAgICAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IHNl"
    "bGYuYXR0ZW1wdFJlZ2lzdGVyKHVzZXJuYW1lLCBwYXNzd29yZCwgIiIsICIiLCAxLCAwLCAiIikK"
    "ICAgICAgICAgICAgICAgIGlmIGxvZ2luRXJyb3IgPT0gMDoKICAgICAgICAgICAgICAgICAgICBM"
    "T0dJTl9USFJPVFRMRS5yZWNvcmRTdWNjZXNzKHBlZXJfaXApCiAgICAgICAgICAgICAgICAgICAg"
    "I1RPRE8gYmV0dGVyIGhhbmRsaW5nIG9mIFRJVExFIEFORCBNT1RECiAgICAgICAgICAgICAgICAg"
    "ICAgc2VsZi5zZW5kUmF3KF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQoYnl0ZXMoc2VsZi5TSyksIERF"
    "RkFVTFRfVElUTEUsIERFRkFVTFRfTU9URCkpCiAgICAgICAgICAgICAgICAgICAgTElTID0gMAog"
    "ICAgICAgICAgICAgICAgZWxzZTogI2Vycm9yIGJhc2VkIG9uIGxvZ2luRXJyb3IgbnVtYmVyCiAg"
    "ICAgICAgICAgICAgICAgICAgY291bnQgPSBMT0dJTl9USFJPVFRMRS5yZWNvcmRGYWlsdXJlKHBl"
    "ZXJfaXApCiAgICAgICAgICAgICAgICAgICAgaWYgY291bnQgPT0gX0xPR0lOX0ZBSUxfTElNSVQ6"
    "CiAgICAgICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBUaHJvdHRsaW5nIGxvZ2lu"
    "cyBmcm9tIHtwZWVyX2lwfSAnCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYnKHtjb3Vu"
    "dH0gZmFpbHVyZXMgaW4ge19MT0dJTl9GQUlMX1dJTkRPV31zKScpCiAgICAgICAgICAgICAgICAg"
    "ICAgZXJybXNncyA9IF9SRUdJU1RFUl9FUlJPUlMgaWYgdmlhUmVnaXN0ZXIgZWxzZSBfTE9HSU5f"
    "RVJST1JTCiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KF9pbml0X2Vycm9yKGVycm1z"
    "Z3MuZ2V0KGxvZ2luRXJyb3IsICdMb2dpbiBmYWlsZWQnKSkpCiAgICBkZWYgZmluaXNoKHNlbGYp"
    "OgogICAgICAgIHNlbGYuc2VydmVyLnVucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYpCiAgICAgICAg"
    "I1N0b3AgdGhlIHdyaXRlciBmaXJzdDogaXQgaG9sZHMgdGhpcyBzb2NrZXQgYW5kIHdvdWxkIG90"
    "aGVyd2lzZSBrZWVwCiAgICAgICAgI3dyaXRpbmcgb24gYmVoYWxmIG9mIGEgcGxheWVyIHdobyBo"
    "YXMgYWxyZWFkeSBsZWZ0IGV2ZXJ5IGNoYW5uZWwuCiAgICAgICAgc2VsZi5fc3RvcFdyaXRlcigp"
    "CiAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVz"
    "ZXIubmFtZX0gRGlzY29ubmVjdGVkJykKICAgICAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qo"
    "c2VsZi5zZXJ2ZXIpCiAgICAgICAgI2NsZWFudXAgdXNlciBkYXRhCiAgICAgICAgI1RPRE8gY2hl"
    "Y2sgaWYgdHJpZ2dlcmVkIG9uIGNyYXNoZWQgY29ubmVjdGlvbgogICAgZGVmIGRlYnVnX2RpY3Qo"
    "c2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgI1RPRE8gSVAgZm9yIGVsZXZhdGVk"
    "IGF1dGhvcml0eQogICAgICAgICAgICAjJ25hbWUnOnNlbGYudXNlci5uYW1lLAogICAgICAgICAg"
    "ICAnZ2FtZSc6c2VsZi51c2VyLmdhbWUuZ25hbWUgaWYgc2VsZi51c2VyLmdhbWUgZWxzZSAnJywK"
    "ICAgICAgICAgICAgJ3Rvd24nOnNlbGYudXNlci5nYW1lY2hhbm5lbC5uYW1lIGlmIHNlbGYudXNl"
    "ci5nYW1lY2hhbm5lbCBlbHNlICcnLAogICAgICAgICAgICAncG9zJzpzZWxmLnVzZXIucG9zZGF0"
    "YSBpZiBzZWxmLnVzZXIucG9zZGF0YSBlbHNlICcnLAogICAgICAgICAgICAnaWQnOnNlbGYudXNl"
    "ci5pZG51bSwKICAgICAgICAgICAgJ2xvZ2luVGltZSc6anNvblRpbWUoc2VsZi51c2VyLmxvZ2lu"
    "VGltZSkKICAgICAgICB9I1RPRE8gZWxldmF0ZWQgYXV0aG9yaXR5IHZlcnNpb24KCmRlZiBjbWRf"
    "ZGVmYXVsdCgpOiNhcmdzKToKICAgICNwcmludChhcmdzKQogICAgI19yZWFkY29uZmlnKCkKICAg"
    "IHNlcnZlciA9IENvcmVTZXJ2ZXIoKQogICAgd2l0aCBzZXJ2ZXI6CiAgICAgICAgdHN0ID0gc2ln"
    "bmFsLnNpZ25hbChzaWduYWwuU0lHSU5ULCBzZXJ2ZXIuaGFuZGxlX3NpZ25hbCh0aW1lb3V0PTIp"
    "KQogICAgICAgICNwcmludCgnQXNzaWduZWQgU2lnbmFsPycsIHRzdCkKICAgICAgICAjc2lnbmFs"
    "LnNpZ25hbChzaWduYWwuU0lHVEVSTSwgc2VydmVyLmhhbmRsZV9zaWduYWwodGltZW91dD0xKSkK"
    "ICAgICAgICBzZXJ2ZXIuc2VydmVfZm9yZXZlcigpCgojc2NyaXB0IGxhdW5jaGVkLCBjaGVjayBh"
    "cmd1bWVudHMgYW5kIGNvbmZpZy4gc2V0dXAgdmFyaW91cyBvYmplY3RzCmlmIF9fbmFtZV9fID09"
    "ICdfX21haW5fXyc6CiAgICBwcmludCgnSW5pdGlhbGl6aW5nIFNlcnZlcicpCiAgICBjbWRfZGVm"
    "YXVsdCgpCg=="
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
