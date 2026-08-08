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
# The Radeon-renderer build is the one that actually gets launched, regardless
# of which exe the stored path points at - see _resolve_game_exe().
GAME_EXE_NAME = 'TwoWorlds_RADEON.exe'


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
        """Always resolves to GAME_EXE_NAME next to whatever was picked -
        the stored path itself, or a sibling exe in the same install folder
        (so an old config pointing at TwoWorlds.exe keeps working: only the
        actual launch target changes)."""
        if not path:
            return None
        folder = path if os.path.isdir(path) else os.path.dirname(path)
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
        ttk.Label(f, text=f'Запускается всегда {GAME_EXE_NAME} - укажи папку игры или сам этот файл.',
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
    "ICAgICBzZWxmLmxvZ2luVGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpCiAgICAgICAgc2Vs"
    "Zi5pZG51bSA9IEdESC5nZXRVUmFuZG9tKCkKICAgICAgICBzZWxmLmNvbm5lY3Rpb24gPSBjb24g"
    "I3NlcnZlciA9IGNvbi5zZXJ2ZXIKICAgICAgICAjc2VsZi5jb25uZWN0aW9uLmd1aWQgLT4gZ3Vp"
    "ZCB3aGVuIHJlbGV2YW50CiAgICAgICAgc2VsZi5wZ3VpZCA9IHByZXR0eV9ndWlkKHNlbGYuY29u"
    "bmVjdGlvbi5ndWlkKQogICAgZGVmIGxlYXZlQ2hhbm5lbChzZWxmKToKICAgICAgICBpZiBzZWxm"
    "LnJlcXVlc3RlZENoYW5uZWw6CiAgICAgICAgICAgICNsaXN0LnJlbW92ZSgpIHJhaXNlcyBWYWx1"
    "ZUVycm9yIHdoZW4gdGhlIGVudHJ5IGlzIGFscmVhZHkgZ29uZTsKICAgICAgICAgICAgI3RoYXQg"
    "dXNlZCB0byBhYm9ydCB0aGUgcmVzdCBvZiB0aGUgZGlzY29ubmVjdCBjbGVhbnVwCiAgICAgICAg"
    "ICAgIGlmIHNlbGYuY29ubmVjdGlvbiBpbiBzZWxmLnJlcXVlc3RlZENoYW5uZWwucmVxdWVzdGVk"
    "OgogICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWRDaGFubmVsLnJlcXVlc3RlZC5yZW1vdmUo"
    "c2VsZi5jb25uZWN0aW9uKQogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZENoYW5uZWwgPSBOb25l"
    "CiAgICAgICAgaWYgc2VsZi5nYW1lY2hhbm5lbDoKICAgICAgICAgICAgc2VsZi5nYW1lY2hhbm5l"
    "bC5sZWF2ZUNoYW5uZWwoc2VsZi5jb25uZWN0aW9uKQogICAgICAgICAgICAjbGVhdmVDaGFubmVs"
    "IGFsc28gbGVhdmVzIGNoYXQKICAgIGRlZiBsZWF2ZUNoYXQoc2VsZik6CiAgICAgICAgaWYgc2Vs"
    "Zi5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgaWYgc2VsZi5jb25uZWN0aW9uIGluIHNlbGYuY2hh"
    "dGNoYW5uZWw6CiAgICAgICAgICAgICAgICBzZWxmLmNoYXRjaGFubmVsLnJlbW92ZShzZWxmLmNv"
    "bm5lY3Rpb24pCiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmNoYXRjaGFubmVsdXNlciAi"
    "e3NlbGYubmFtZX0iJykKICAgICAgICAgICAgc2VsZi5jb25uZWN0aW9uLnNlcnZlci5kaXN0LmFk"
    "ZCh7J3RhcmdldCc6c2VsZi5jaGF0Y2hhbm5lbCwnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgICAg"
    "ICAgICBzZWxmLmNoYXRjaGFubmVsPU5vbmUKICAgIGRlZiBzdG9wR2FtZShzZWxmKToKICAgICAg"
    "ICBpZiBzZWxmLnJlcXVlc3RlZEdhbWU6CiAgICAgICAgICAgICNCb3RoIGd1YXJkcyBtYXR0ZXI6"
    "IHRoZSBjaGFubmVsIG1heSBhbHJlYWR5IGJlIGdvbmUgKGxlYXZlQ2hhbm5lbAogICAgICAgICAg"
    "ICAjY2xlYXJzIGl0IGJlZm9yZSBzdG9wR2FtZSBydW5zIG9uIHNvbWUgcGF0aHMpIGFuZCB0aGUg"
    "cGVuZGluZwogICAgICAgICAgICAjcmVxdWVzdCBtYXkgYWxyZWFkeSBoYXZlIGJlZW4gY29uc3Vt"
    "ZWQgYnkgY3JlYXRlR2FtZS4gRWl0aGVyIG9uZQogICAgICAgICAgICAjdXNlZCB0byByYWlzZSAo"
    "QXR0cmlidXRlRXJyb3Igb24gTm9uZSAvIEtleUVycm9yKSBpbnNpZGUgdGhlCiAgICAgICAgICAg"
    "ICNkaXNjb25uZWN0IHBhdGggYW5kIGFib3J0IHRoZSByZXN0IG9mIHRoZSBjbGVhbnVwLCBsZWFr"
    "aW5nIHRoZQogICAgICAgICAgICAjcGxheWVyJ3MgZW50cnkgaW4gYWN0aXZlVXNlcnMuCiAgICAg"
    "ICAgICAgIGlmIHNlbGYuZ2FtZWNoYW5uZWw6CiAgICAgICAgICAgICAgICBzZWxmLmdhbWVjaGFu"
    "bmVsLmdhbWVSZXF1ZXN0cy5wb3Aoc2VsZi5yZXF1ZXN0ZWRHYW1lLCBOb25lKQogICAgICAgICAg"
    "ICBzZWxmLnJlcXVlc3RlZEdhbWUgPSBOb25lCiAgICAgICAgaWYgc2VsZi5nYW1lOgogICAgICAg"
    "ICAgICBzZWxmLmdhbWUucmVtb3ZlKHNlbGYuY29ubmVjdGlvbikKICAgIGRlZiBkaXNjb25uZWN0"
    "KHNlbGYsIHNlcnZlcik6CiAgICAgICAgc2VsZi5zdG9wR2FtZSgpCiAgICAgICAgc2VsZi5sZWF2"
    "ZUNoYW5uZWwoKQogICAgICAgIHNlcnZlci5zdGF0ZS5yZWxlYXNlVXNlcihzZWxmLm5hbWUsIHNl"
    "bGYuY29ubmVjdGlvbikKICAgICAgICBHREgucmVsZWFzZVVSYW5kb20oc2VsZi5pZG51bSkKICAg"
    "IGRlZiB3aXJlSWQoc2VsZik6CiAgICAgICAgI1RoZSBvbmUgcGxhY2UgdGhlIGhlcm8gaWQgaXMg"
    "Zm9ybWF0dGVkLCBzbyAkZ2FtZWNoYW5uZWx1c2VyIGFuZAogICAgICAgICMvdXBkaGVyb3BvcyBj"
    "YW4gbmV2ZXIgZGlzYWdyZWUgYWdhaW4gLSBzZWUgX0hFUk9fSURfSEVYLgogICAgICAgIHJldHVy"
    "biBmJ3tzZWxmLmlkbnVtOnh9JyBpZiBfSEVST19JRF9IRVggZWxzZSBmJ3tzZWxmLmlkbnVtfScK"
    "ICAgIGRlZiBnZXRHQ1Vtc2coc2VsZik6CiAgICAgICAgaGRsID0gbGVuKHNlbGYuaGVyb2RhdGEp"
    "CiAgICAgICAgaWYgaGRsPT0wOgogICAgICAgICAgICByZXR1cm4gYicnCiAgICAgICAgcmV0dXJu"
    "IF9lbShmJyRnYW1lY2hhbm5lbHVzZXIgIntzZWxmLm5hbWV9IiAiIiAiMTAwIiAie3NlbGYud2ly"
    "ZUlkKCl9IiAiMCIgIntzZWxmLnBndWlkfSIgIntzZWxmLnBvc2RhdGF9IiAie2hkbH0iJykrc2Vs"
    "Zi5oZXJvZGF0YQogICAgZGVmIGdldENDVW1zZyhzZWxmKToKICAgICAgICB2YiA9IDAgI29yIDB4"
    "RkZGRkZGRkYoNDI5NDk2NzI5NT0gLTEmMzJiaXQ/KQogICAgICAgIHJldHVybiBfZW0oZickY2hh"
    "dGNoYW5uZWx1c2VyICJ7c2VsZi5uYW1lfSIgIiIgInt2Yn0iICJ7c2VsZi5wZ3VpZH0iJykKICAg"
    "ICAgICAjICRjaGF0Y2hhbm5lbHVzZXIgIntuYW1lfSIgIiIgIjAiICJ7Z3VpZH0iCiMgaW5jcmVh"
    "c2luZyBtYXkgaW1wcm92ZSBzZWN1cml0eSBhdCB0aGUgY29zdCBvZiBwZXJmb3JtYW5jZQojIG9u"
    "bHkgdXBkYXRlcyB3aGVuIHVzZXIgbG9ncyBpbiBhbmQgaXMgc3RvcmVkIGFsb25nc2lkZSBzYWx0"
    "IGluIGRhdGFiYXNlCl9IQVNISVRFUiA9IDEwMDAwMApkZWYgX3NhbHRfaGFzaF8ocGFzc3dvcmQs"
    "IHNhbHQsIGhJdHIpOgogICAgI3V0Zi04LCBub3QgYXNjaWk6IGEgcGFzc3dvcmQgd2l0aCBhbiA4"
    "LWJpdCBjaGFyYWN0ZXIgdXNlZCB0byByYWlzZSBoZXJlIGFuZAogICAgI2Ryb3AgdGhlIGNvbm5l"
    "Y3Rpb24gaW5zdGVhZCBvZiBsb2dnaW5nIHRoZSBwbGF5ZXIgaW4uIFB1cmUtYXNjaWkgcGFzc3dv"
    "cmRzCiAgICAjZW5jb2RlIHRvIGlkZW50aWNhbCBieXRlcyB1bmRlciBib3RoLCBzbyBubyBzdG9y"
    "ZWQgaGFzaCBjaGFuZ2VzLgogICAgcmV0dXJuIGhhc2hsaWIucGJrZGYyX2htYWMoJ3NoYTI1Nics"
    "IHBhc3N3b3JkLmVuY29kZSgndXRmLTgnKSwgc2FsdCwgaEl0cikKICAgIAojIyMgU1FMIElORk8K"
    "IyBfREJJTkZPOiBWRVJTSU9OIDEKIyB1c2VyVGFibGUKIyAtIHJvd2lkLCB1c2VybmFtZSwgcGFz"
    "c0hhc2gsIHNlcmlhbCwgdW5pcXVlU2FsdCwgbGFzdExvZ2luLCBlbWFpbCwgbG9jYXRpb24sIHll"
    "YXJvZmJpcnRoKGVzdGltYXRlKSwgZ2VuZGVyLCBkZXNjcmlwdGlvbgojIGZvcm1UYWJsZQojIC0g"
    "cm93aWQsIGZvcm0KIyMgLS0tLS0tLS0tLS0tLS0tLSAjIwojIFRPRE8gVkVSU0lPTiAyOiBndWls"
    "ZHMsIGxlYWRlcmJvYXJkLCBldGM/CgojVE9ETyBjb252ZXJ0IGRhdGFiYXNlIHRvIHNpbmdsZXRo"
    "cmVhZCBhY2Nlc3MgZm9yIGNvbXBhdGliaWxpdHk/IHVubmVjY2VzYXJ5PwojY2xhc3MgRGF0YVJl"
    "cXVlc3QodGhyZWFkaW5nLkV2ZW50KToKIyAgIGRhdGEgPSBOb25lCiMgICBkZWYgc2V0KHZhbCk6"
    "CiMgICAgICAgc2VsZi5kYXRhPXZhbAojICAgICAgIHN1cGVyKCkuc2V0KCkKIyAgIGRlZiB3YWl0"
    "KCk6CiMgICAgICAgc3VwZXIoKS53YWl0KCkKIyAgICAgICByZXR1cm4gc2VsZi5kYXRhCiMqIGRh"
    "dGFiYXNlIHRocmVhZDoKIyAgIF9kclEgPSBkYXRhIHJlcXVlc3QgcXVldWUsIHByb2Nlc3NlZCBp"
    "biBkYXRhYmFzZSB0aHJlYWQKIyAgIGV4dGVybmFsIGZ1bmN0aW9ucyBhZGQgcmVxdWVzdCBmb3Ig"
    "aW50ZXJuYWwgZnVuY3Rpb24gYW5kIHJldHVybiByZXF1ZXN0IHRvIGF3YWl0CiMgICBkcm9iaiBp"
    "biBxdWV1ZSA9IChkciwgZnRhcmdldCwgKGFyZ3MpKSwgZHIuc2V0KGZ0YXJnZXQoKmFyZ3MpKQoj"
    "VE9ETyBvcmdhbml6ZSBTUUwgY29tbWFuZHM/IG1ha2UgaXQgbW9yZSBiZWF1dGlmdWw/Cl9TUUxf"
    "ZGJJbmZvRXhpc3RzID0gJ1NFTEVDVCBuYW1lIEZST00gc3FsaXRlX21hc3RlciBXSEVSRSBuYW1l"
    "PSJfREJJTkZPIicKX1NRTF9kYlZlcnNpb24gPSAnU0VMRUNUIFZFUlNJT04gRlJPTSBfREJJTkZP"
    "JwpfU1FMSU5JVF9kYkluZm9UYWJsZSA9ICdDUkVBVEUgVEFCTEUgX0RCSU5GTyhWRVJTSU9OKScK"
    "X0RCQ1VSVkVSID0gMgpfU1FMSU5JVF9kYkluZm9WZXJzaW9uID0gZidJTlNFUlQgSU5UTyBfREJJ"
    "TkZPIFZBTFVFUyAoe19EQkNVUlZFUn0pJwpfU1FMVVBEX2RiSW5mb1ZlcnNpb24gPSBmJ1VQREFU"
    "RSBfREJJTkZPIFNFVCBWRVJTSU9OID0ge19EQkNVUlZFUn0nCiN5b2IgPSB5ZWFyIG9mIGJpcnRo"
    "IChlc3RpbWF0ZSkKI2dlbmRlcjogMCA9IE1hbGUKX1NRTElOSVRfZGJVc2VyVGFibGUgPSAnQ1JF"
    "QVRFIFRBQkxFIHVzZXJUYWJsZSh1c2VybmFtZSBVTklRVUUsIHBhc3NIYXNoLCBzZXJpYWwsIHVu"
    "aXF1ZVNhbHQsIGhhc2hJdGVyLCBsYXN0TG9naW4gVElNRVNUQU1QLCBlbWFpbCwgbG9jYXRpb24s"
    "IHlvYiwgZ2VuZGVyLCBkZXNjcmlwdGlvbiknCl9TUUxJTklUX2RiRm9ybVRhYmxlID0gJ0NSRUFU"
    "RSBUQUJMRSBmb3JtVGFibGUoZm9ybSBVTklRVUUpJyAjdXNpbmcgcm93aWQgYXMgSUQKIy0tLSBn"
    "dWlsZHMgKERCIHZlcnNpb24gMikgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tCiNyYW5rOiAyID0gZm91bmRlci9sZWFkZXIsIDEgPSBvZmZpY2VyLCAw"
    "ID0gbWVtYmVyLiBBIHBsYXllciBpcyBpbiBhdCBtb3N0IG9uZQojZ3VpbGQsIHdoaWNoIGlzIHdo"
    "YXQgdGhlIGNsaWVudCdzIFVJIGFzc3VtZXMgKHdob2lzIGNhcnJpZXMgYSBzaW5nbGUgbmFtZSku"
    "CiNndWlsZGtleSBpcyBndWlsZG5hbWUuY2FzZWZvbGQoKSBhbmQgaXMgd2hhdCB1bmlxdWVuZXNz"
    "IGFuZCBldmVyeSBsb29rdXAgZ28KI3Rocm91Z2guIFNRTGl0ZSdzIG93biBDT0xMQVRFIE5PQ0FT"
    "RSBvbmx5IGZvbGRzIEEtWiwgc28gb24gdGhpcyBzZXJ2ZXIgLQojd2hlcmUgdGhlIG5hbWVzIGFy"
    "ZSBDeXJpbGxpYyAtIGl0IHdvdWxkIGhhdmUgbGV0ICLQndC+0YfQvdGL0LUg0JLQvtC70LrQuCIg"
    "YW5kICLQvdC+0YfQvdGL0LUKI9Cy0L7Qu9C60LgiIGNvZXhpc3QgYXMgdHdvIHNlcGFyYXRlIGd1"
    "aWxkcyB0aGF0IHBsYXllcnMgY291bGQgbm90IHRlbGwgYXBhcnQuCl9TUUxJTklUX2RiR3VpbGRU"
    "YWJsZSA9ICdDUkVBVEUgVEFCTEUgZ3VpbGRUYWJsZShndWlsZG5hbWUsIGd1aWxka2V5IFVOSVFV"
    "RSwgb3duZXIsIGNyZWF0ZWQgVElNRVNUQU1QLCBkZXNjcmlwdGlvbiknCl9TUUxJTklUX2RiR3Vp"
    "bGRNZW1iZXJUYWJsZSA9ICdDUkVBVEUgVEFCTEUgZ3VpbGRNZW1iZXJUYWJsZShndWlsZG5hbWUs"
    "IHVzZXJuYW1lIFVOSVFVRSwgcmFuayknCl9TUUxfZ3VpbGRFeGlzdHMgPSAnU0VMRUNUIGd1aWxk"
    "bmFtZSBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRrZXkgPSA/JwpfU1FMX2NyZWF0ZUd1aWxk"
    "ID0gJ0lOU0VSVCBJTlRPIGd1aWxkVGFibGUgVkFMVUVTICg/LD8sPyw/LD8pJwpfU1FMX2RlbGV0"
    "ZUd1aWxkID0gJ0RFTEVURSBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycKX1NR"
    "TF9ndWlsZE93bmVyID0gJ1NFTEVDVCBvd25lciBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRu"
    "YW1lID0gPycKX1NRTF9hZGRHdWlsZE1lbWJlciA9ICdJTlNFUlQgT1IgUkVQTEFDRSBJTlRPIGd1"
    "aWxkTWVtYmVyVGFibGUgVkFMVUVTICg/LD8sPyknCl9TUUxfZGVsR3VpbGRNZW1iZXIgPSAnREVM"
    "RVRFIEZST00gZ3VpbGRNZW1iZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxfZGVsR3Vp"
    "bGRNZW1iZXJzID0gJ0RFTEVURSBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgZ3VpbGRuYW1l"
    "ID0gPycKX1NRTF9ndWlsZE9mVXNlciA9ICdTRUxFQ1QgZ3VpbGRuYW1lLCByYW5rIEZST00gZ3Vp"
    "bGRNZW1iZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxfZ3VpbGRNZW1iZXJzID0gJ1NF"
    "TEVDVCB1c2VybmFtZSwgcmFuayBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgZ3VpbGRuYW1l"
    "ID0gPycKI1NhbWUgc2hhcGUgYXMgdGhlIHVzZXJuYW1lIHJ1bGU6IHRoZSBuYW1lIHRyYXZlbHMg"
    "aW5zaWRlIHF1b3RlZCBwcm90b2NvbAojZmllbGRzLCBzbyBhbnl0aGluZyB0aGF0IGNvdWxkIGNs"
    "b3NlIGEgcXVvdGUgaXMgcmVqZWN0ZWQgb3V0cmlnaHQgcmF0aGVyIHRoYW4KI3NpbGVudGx5IHJl"
    "d3JpdHRlbi4gU3BhY2VzIGFyZSBhbGxvd2VkIC0gZ3VpbGQgbmFtZXMgY29tbW9ubHkgaGF2ZSB0"
    "aGVtLgpfUkVfVkFMSURfR1VJTEROQU1FID0gcmUuY29tcGlsZShyJ15bXiJcclxuXDBdezMsMzJ9"
    "JCcpCgpfU1FMX3VzZXJJRCA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2VyVGFibGUgV0hFUkUgdXNl"
    "cm5hbWUgPSA/JwpfU1FMX3VzZXJJRF9TY2hrID0gJ1NFTEVDVCByb3dpZCBGUk9NIHVzZXJUYWJs"
    "ZSBXSEVSRSBzZXJpYWwgPSA/JwpfU1FMX3VzZXJJRF9zdHJpY3QgPSAnU0VMRUNUIHJvd2lkIEZS"
    "T00gdXNlclRhYmxlIFdIRVJFIHVzZXJuYW1lID0gPyBBTkQgc2VyaWFsID0gPycKX1NRTF9yZWdp"
    "c3RlclVzZXIgPSAnSU5TRVJUIElOVE8gdXNlclRhYmxlIFZBTFVFUyAoPyw/LD8sPyw/LD8sPyw/"
    "LD8sPyw/KScKX1NRTF9kZWxldGVVc2VyID0gJ0RFTEVURSBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1"
    "c2VybmFtZSA9ID8nCl9TUUxfZ2V0TG9naW4gPSAnU0VMRUNUIHVzZXJuYW1lLCBwYXNzSGFzaCwg"
    "dW5pcXVlU2FsdCwgaGFzaEl0ZXIgRlJPTSB1c2VyVGFibGUgV0hFUkUgcm93aWQgPSA/JwpfU1FM"
    "VVBEX3Bhc3NIYXNoID0gJ1VQREFURSB1c2VyVGFibGUgU0VUIHBhc3NIYXNoID0gPywgaGFzaEl0"
    "ZXIgPSA/IFdIRVJFIHJvd2lkID0gPycKX1NRTF9sb2dpblVwZGF0ZSA9ICdVUERBVEUgdXNlclRh"
    "YmxlIFNFVCBsYXN0TG9naW4gPSA/IFdIRVJFIHJvd2lkID0gPycKX1NRTF9nZXRXaG9pcyA9ICdT"
    "RUxFQ1QgZW1haWwsIGxvY2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24gRlJPTSB1c2Vy"
    "VGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/JwpfU1FMVVBEX3dob2lzID0gJ1VQREFURSB1c2VyVGFi"
    "bGUgU0VUIGVtYWlsID0gPywgbG9jYXRpb24gPSA/LCB5b2IgPSA/LCBnZW5kZXIgPSA/LCBkZXNj"
    "cmlwdGlvbiA9ID8gV0hFUkUgdXNlcm5hbWUgPSA/JwojaWYgZG9lcyBub3QgZXhpc3QsIGdlbmVy"
    "YXRlLCBjaGFuZ2UgZm9ybWF0IGZvciBtb2RwYWNrcwpfU1FMX2Zvcm1JRCA9ICdTRUxFQ1Qgcm93"
    "aWQgZnJvbSBmb3JtVGFibGUgV0hFUkUgZm9ybSA9ID8nCl9TUUxBRERfZm9ybUlEID0gJ0lOU0VS"
    "VCBJTlRPIGZvcm1UYWJsZSBWQUxVRVMgKD8pJwpfRk9STV9QREZpbGUgPSAnezp4fV97Onh9LmJp"
    "bicgIyBwbGF5ZXJkYXRhXHVzZXJJRF9mb3JtSUQuYmluCgpkZWYgcmVhZEJpbihmaWxlcGF0aCk6"
    "CiAgICB3aXRoIG9wZW4oZmlsZXBhdGgsICJyYiIpIGFzIGY6CiAgICAgICAgcmV0dXJuIGYucmVh"
    "ZCgpCmNsYXNzIERhdGFIYW5kbGVyKCk6CiAgICBkZWYgX19pbml0X18oc2VsZik6CiAgICAgICAg"
    "I2luc3RhbmNlIGF0dHJpYnV0ZSwgbm90IGEgY2xhc3MgYXR0cmlidXRlIC0gc2FtZSByZWFzb25p"
    "bmcgYXMKICAgICAgICAjR2FtZVN0YXRlLmFjdGl2ZVVzZXJzOiBzaGFyZWQgY2xhc3Mgc3RhdGUg"
    "bGVha3MgYmV0d2VlbiBpbnN0YW5jZXMKICAgICAgICBzZWxmLnVzZWROdW1zID0gc2V0KCkKICAg"
    "ICAgICAjcHJpbnQoJ3NxbGl0ZTMgdGhyZWFkc2FmZXR5Oicsc3FsaXRlMy50aHJlYWRzYWZldHkp"
    "CiAgICAgICAgI2lmIHNxbGl0ZTMudGhyZWFkc2FmZXR5PDM6CiAgICAgICAgIyAgICByYWlzZSBF"
    "eGNlcHRpb24oJ011bHRpVGhyZWFkIHN1cHBvcnQgcmVxdWlyZWQnKQogICAgICAgICNUT0RPIG9y"
    "Z2FuaXplIHNpbmdsZSB0aHJlYWRlZCBkYXRhYmFzZSBhY2Nlc3M/IGV2ZXIgbmVlZGVkPwogICAg"
    "ICAgIHNlbGYubG9jayA9IHRocmVhZGluZy5STG9jaygpCiAgICAgICAgb3MubWFrZWRpcnMoX1BB"
    "VEhfUExBWUVSREFUQSwgZXhpc3Rfb2s9VHJ1ZSkKICAgICAgICBzZWxmLmRiID0gc3FsaXRlMy5j"
    "b25uZWN0KF9QQVRIX0RBVEFCQVNFLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "Y2hlY2tfc2FtZV90aHJlYWQgPSBGYWxzZSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgIGRldGVjdF90eXBlcz1zcWxpdGUzLlBBUlNFX0RFQ0xUWVBFUyB8CiAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICBzcWxpdGUzLlBBUlNFX0NPTE5BTUVTKQogICAgICAgIGluaXRj"
    "dXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgZGJVbmluaXRpYWxpemVkID0gaW5pdGN1ci5l"
    "eGVjdXRlKF9TUUxfZGJJbmZvRXhpc3RzKS5mZXRjaG9uZSgpIGlzIE5vbmUKICAgICAgICBpZiBk"
    "YlVuaW5pdGlhbGl6ZWQ6CiAgICAgICAgICAgIGRiVmVyUmVzID0gMAogICAgICAgIGVsc2U6CiAg"
    "ICAgICAgICAgIGRiVmVyUmVzID0gaW5pdGN1ci5leGVjdXRlKF9TUUxfZGJWZXJzaW9uKS5mZXRj"
    "aG9uZSgpWzBdCiAgICAgICAgc2VsZi51cGRhdGVEQkZyb20oZGJWZXJSZXMpICNlbnN1cmUgREIg"
    "aXMgdXBkYXRlZAogICAgICAgIAogICAgICAgIGluaXRjdXIuY2xvc2UoKQogICAgZGVmIGdldFVS"
    "YW5kb20oc2VsZik6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJudW0gPSBy"
    "YW5kb20ucmFuZGludCgxLDB4ODAwMCkKICAgICAgICAgICAgd2hpbGUgcm51bSBpbiBzZWxmLnVz"
    "ZWROdW1zOgogICAgICAgICAgICAgICAgcm51bSArPSAxI0Vuc3VyZSB1bmlxdWUKICAgICAgICAg"
    "ICAgc2VsZi51c2VkTnVtcy5hZGQocm51bSkKICAgICAgICAgICAgcmV0dXJuIHJudW0KICAgIGRl"
    "ZiByZWxlYXNlVVJhbmRvbShzZWxmLCBudW0pOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAg"
    "ICAgICAgICBzZWxmLnVzZWROdW1zLmRpc2NhcmQobnVtKSNkaXNjYXJkOiBzYWZlIGV2ZW4gaWYg"
    "YWxyZWFkeSByZWxlYXNlZAogICAgZGVmIHVwZGF0ZURCRnJvbShzZWxmLCB2ZXJzaW9uKToKICAg"
    "ICAgICBwcmludCgnRGF0YWJhc2UgVmVyc2lvbjonLHZlcnNpb24pCiAgICAgICAgaWYgdmVyc2lv"
    "biA+PSBfREJDVVJWRVI6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHByaW50KCdVcGRhdGlu"
    "ZyBEYXRhYmFzZSB0byBWZXJzaW9uJyxfREJDVVJWRVIpCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6"
    "CiAgICAgICAgICAgIHVwZGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgaWYgdmVy"
    "c2lvbiA9PSAwOgogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJJbmZv"
    "VGFibGUpCiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkluZm9WZXJz"
    "aW9uKQogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJVc2VyVGFibGUp"
    "CiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkZvcm1UYWJsZSkKICAg"
    "ICAgICAgICAgaWYgdmVyc2lvbiA8IDI6CiAgICAgICAgICAgICAgICAjR3VpbGQgc3RvcmFnZS4g"
    "QWRkaXRpdmUgb25seSwgc28gYW4gZXhpc3RpbmcgdjEgZGF0YWJhc2Ugd2l0aAogICAgICAgICAg"
    "ICAgICAgI3JlYWwgYWNjb3VudHMgaW4gaXQgdXBncmFkZXMgaW4gcGxhY2UuCiAgICAgICAgICAg"
    "ICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkd1aWxkVGFibGUpCiAgICAgICAgICAgICAg"
    "ICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkd1aWxkTWVtYmVyVGFibGUpCiAgICAgICAgICAg"
    "ICNUaGUgdmVyc2lvbiByb3cgd2FzIG9ubHkgZXZlciB3cml0dGVuIGJ5IHRoZSB2ZXJzaW9uPT0w"
    "IGJyYW5jaCwgc28KICAgICAgICAgICAgI2V2ZXJ5IGxhdGVyIG1pZ3JhdGlvbiB3b3VsZCBoYXZl"
    "IHJlLXJ1biBvbiB0aGUgbmV4dCBzdGFydC4KICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NR"
    "TFVQRF9kYkluZm9WZXJzaW9uKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAg"
    "ICAgIHVwZGN1ci5jbG9zZSgpCiAgICBkZWYgZ2V0UERGTihzZWxmLCBuYW1lLCBmb3JtLCBjcmVh"
    "dGUpOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBmb3JtY3VyID0gc2VsZi5k"
    "Yi5jdXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBmb3JtY3VyLmV4ZWN1dGUoX1NRTF91c2Vy"
    "SUQsIChuYW1lLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgog"
    "ICAgICAgICAgICAgICAgZm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9u"
    "ZSAjVXNlciBkb2Vzbid0IGV4aXN0CiAgICAgICAgICAgIGZpZHJlcyA9IGZvcm1jdXIuZXhlY3V0"
    "ZShfU1FMX2Zvcm1JRCwgKGZvcm0sICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgZmlkcmVz"
    "IGlzIE5vbmU6ICNmb3JtYXQgZG9lcyBub3QgZXhpc3QKICAgICAgICAgICAgICAgIGlmIG5vdCBj"
    "cmVhdGU6CiAgICAgICAgICAgICAgICAgICAgZm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAgICAg"
    "ICAgICAgcmV0dXJuIE5vbmUgI05ldyBmb3JtYXQgbm90IGNyZWF0ZWQKICAgICAgICAgICAgICAg"
    "IGZvcm1jdXIuZXhlY3V0ZShfU1FMQUREX2Zvcm1JRCwgKGZvcm0sICkpCiAgICAgICAgICAgICAg"
    "ICBzZWxmLmRiLmNvbW1pdCgpI1RPRE8gQ2hlY2sgaWYgZ290dGEgY29tbWl0IGJlZm9yZSByZWFk"
    "LWJhY2s/CiAgICAgICAgICAgICAgICBmaWRyZXMgPSBmb3JtY3VyLmV4ZWN1dGUoX1NRTF9mb3Jt"
    "SUQsIChmb3JtLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGZvcm1jdXIuY2xvc2UoKQogICAg"
    "ICAgICAgICBmaWQgPSBmaWRyZXNbMF0KICAgICAgICAgICAgdWlkID0gdWlkcmVzWzBdCiAgICAg"
    "ICAgICAgIGZpbGVuYW1lID0gX0ZPUk1fUERGaWxlLmZvcm1hdCh1aWQsIGZpZCkKICAgICAgICAg"
    "ICAgZnBhdGggPSBvcy5wYXRoLmpvaW4oX1BBVEhfUExBWUVSREFUQSwgZmlsZW5hbWUpCiAgICAg"
    "ICAgICAgIGlmIG9zLnBhdGguZXhpc3RzKGZwYXRoKSBvciBjcmVhdGU6CiAgICAgICAgICAgICAg"
    "ICByZXR1cm4gZnBhdGgKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBnZXRQbGF5ZXJE"
    "YXRhKHNlbGYsIG5hbWUsIGZvcm0pOgogICAgICAgIHBhdGggPSBzZWxmLmdldFBERk4obmFtZSwg"
    "Zm9ybSwgRmFsc2UpCiAgICAgICAgaWYgbm90IHBhdGg6CiAgICAgICAgICAgIHJldHVybiBiJycK"
    "ICAgICAgICByZXR1cm4gcmVhZEJpbihwYXRoKSNUT0RPIGRlZmF1bHQgdG8gYicnIG9uIGVycm9y"
    "PwogICAgZGVmIHNldFBsYXllckRhdGEoc2VsZiwgbmFtZSwgZm9ybSwgZGF0YSk6CiAgICAgICAg"
    "cGF0aCA9IHNlbGYuZ2V0UERGTihuYW1lLCBmb3JtLCBUcnVlKQogICAgICAgIGlmIG5vdCBwYXRo"
    "OiNOTyBGSUxFIFBBVEgsIFRPRE8gQ0FUQ0ggRVJST1IKICAgICAgICAgICAgcmV0dXJuCiAgICAg"
    "ICAgd2l0aCBvcGVuKHBhdGgsICd3YicpIGFzIGY6I1RPRE8gY2F0Y2ggZXJyb3JzCiAgICAgICAg"
    "ICAgIGYud3JpdGUoZGF0YSkKICAgIGRlZiBnZXRXaG9pcyhzZWxmLCBuYW1lKToKICAgICAgICB3"
    "aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAg"
    "ICAgICAgcmVzID0gd2N1ci5leGVjdXRlKF9TUUxfZ2V0V2hvaXMsIChuYW1lLCkpLmZldGNob25l"
    "KCkKICAgICAgICAgICAgd2N1ci5jbG9zZSgpCiAgICAgICAgICAgIGlmIHJlcyBpcyBOb25lOgog"
    "ICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICAgICAgKGVtYWlsLCBsb2NhdGlvbiwg"
    "eW9iLCBnZW5kZXIsIGRlc2NyaXB0aW9uKSA9IHJlcwogICAgICAgICAgICBjdXJZZWFyID0gZGF0"
    "ZXRpbWUuZGF0ZXRpbWUubm93KCkueWVhcgogICAgICAgICAgICBhZ2UgPSBtYXgoMCwgY3VyWWVh"
    "ciAtIHlvYikgaWYgeW9iIGVsc2UgMAogICAgICAgICAgICByZXR1cm4gewogICAgICAgICAgICAg"
    "ICAgJ2VtYWlsJzogZW1haWwgb3IgJycsCiAgICAgICAgICAgICAgICAnbG9jYXRpb24nOiBsb2Nh"
    "dGlvbiBvciAnJywKICAgICAgICAgICAgICAgICdhZ2UnOiBhZ2UsCiAgICAgICAgICAgICAgICAn"
    "Z2VuZGVyJzogZ2VuZGVyIGlmIGdlbmRlciBpcyBub3QgTm9uZSBlbHNlIDAsCiAgICAgICAgICAg"
    "ICAgICAnZGVzY3JpcHRpb24nOiBkZXNjcmlwdGlvbiBvciAnJwogICAgICAgICAgICB9CiAgICBk"
    "ZWYgdXBkYXRlV2hvaXMoc2VsZiwgbmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwg"
    "ZGVzY3JpcHRpb24pOgogICAgICAgIHRyeToKICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAg"
    "ICAgICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgICAgIGFnZSA9IDAK"
    "ICAgICAgICB0cnk6CiAgICAgICAgICAgIGdlbmRlciA9IGludChnZW5kZXIpCiAgICAgICAgZXhj"
    "ZXB0IChUeXBlRXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAgICBnZW5kZXIgPSAwCiAgICAg"
    "ICAgeW9iID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCkueWVhciAtIGFnZQogICAgICAgIHdpdGgg"
    "c2VsZi5sb2NrOgogICAgICAgICAgICB3Y3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAg"
    "ICB3Y3VyLmV4ZWN1dGUoX1NRTFVQRF93aG9pcywgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5k"
    "ZXIsIGRlc2NyaXB0aW9uLCBuYW1lKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAg"
    "ICAgICAgICB3Y3VyLmNsb3NlKCkKICAgICMjIEdVSUxEUwogICAgZGVmIGdldEd1aWxkT2Yoc2Vs"
    "ZiwgdXNlcm5hbWUpOgogICAgICAgICMtPiAoZ3VpbGRuYW1lLCByYW5rKSBvciAoTm9uZSwgMCkK"
    "ICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3Io"
    "KQogICAgICAgICAgICByZXMgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5h"
    "bWUsKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIGlmIHJlcyBp"
    "cyBOb25lOgogICAgICAgICAgICByZXR1cm4gKE5vbmUsIDApCiAgICAgICAgcmV0dXJuIChyZXNb"
    "MF0sIHJlc1sxXSBvciAwKQogICAgZGVmIGdldEd1aWxkTmFtZShzZWxmLCB1c2VybmFtZSk6CiAg"
    "ICAgICAgcmV0dXJuIHNlbGYuZ2V0R3VpbGRPZih1c2VybmFtZSlbMF0gb3IgJycKICAgIGRlZiBn"
    "ZXRHdWlsZE1lbWJlcnMoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBjdXIu"
    "ZXhlY3V0ZShfU1FMX2d1aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKS5mZXRjaGFsbCgpCiAgICAg"
    "ICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIFsoclswXSwgclsxXSBvciAwKSBmb3Ig"
    "ciBpbiByZXNdCiAgICBkZWYgY3JlYXRlR3VpbGQoc2VsZiwgZ3VpbGRuYW1lLCBvd25lciwgZGVz"
    "Y3JpcHRpb249JycpOgogICAgICAgICMtPiBndWlsZG5hbWUgb24gc3VjY2Vzcywgb3IgYW4gZXJy"
    "b3IgdG9rZW4gZm9yIHRoZSBjbGllbnQKICAgICAgICBpZiBub3QgX1JFX1ZBTElEX0dVSUxETkFN"
    "RS5tYXRjaChndWlsZG5hbWUgb3IgJycpOgogICAgICAgICAgICByZXR1cm4gJ2JhZEd1aWxkTmFt"
    "ZScKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJz"
    "b3IoKQogICAgICAgICAgICBpZiBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAob3duZXIs"
    "KSkuZmV0Y2hvbmUoKSBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAg"
    "ICAgICAgICAgICAgICByZXR1cm4gJ2FscmVhZHlJbkd1aWxkJwogICAgICAgICAgICBpZiBjdXIu"
    "ZXhlY3V0ZShfU1FMX2d1aWxkRXhpc3RzLCAoZ3VpbGRuYW1lLmNhc2Vmb2xkKCksKSkuZmV0Y2hv"
    "bmUoKSBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAg"
    "ICAgICByZXR1cm4gJ2d1aWxkTmFtZVRha2VuJwogICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FM"
    "X2NyZWF0ZUd1aWxkLAogICAgICAgICAgICAgICAgICAgICAgICAoZ3VpbGRuYW1lLCBndWlsZG5h"
    "bWUuY2FzZWZvbGQoKSwgb3duZXIsCiAgICAgICAgICAgICAgICAgICAgICAgICBkYXRldGltZS5k"
    "YXRldGltZS5ub3coKSwgc2FuaXRpemVUZXh0KGRlc2NyaXB0aW9uKSkpCiAgICAgICAgICAgIGN1"
    "ci5leGVjdXRlKF9TUUxfYWRkR3VpbGRNZW1iZXIsIChndWlsZG5hbWUsIG93bmVyLCAyKSkKICAg"
    "ICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAg"
    "IHJldHVybiBOb25lCiAgICBkZWYgam9pbkd1aWxkKHNlbGYsIGd1aWxkbmFtZSwgdXNlcm5hbWUp"
    "OgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNv"
    "cigpCiAgICAgICAgICAgIHJvdyA9IGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRFeGlzdHMsICgoZ3Vp"
    "bGRuYW1lIG9yICcnKS5jYXNlZm9sZCgpLCkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgcm93"
    "IGlzIE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0"
    "dXJuICd1bmtub3duR3VpbGQnCiAgICAgICAgICAgICNTdG9yZSB0aGUgZ3VpbGQncyBvd24gc3Bl"
    "bGxpbmcsIG5vdCB3aGF0ZXZlciBjYXNlIHRoZSBjbGllbnQgdHlwZWQKICAgICAgICAgICAgI2lu"
    "dG8gdGhlIGpvaW4gYm94LCBzbyBnZXRHdWlsZE1lbWJlcnMoKSBmaW5kcyB0aGUgbWVtYmVyIGJh"
    "Y2suCiAgICAgICAgICAgIGd1aWxkbmFtZSA9IHJvd1swXQogICAgICAgICAgICBpZiBjdXIuZXhl"
    "Y3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5hbWUsKSkuZmV0Y2hvbmUoKSBpcyBub3QgTm9u"
    "ZToKICAgICAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ2Fs"
    "cmVhZHlJbkd1aWxkJwogICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2FkZEd1aWxkTWVtYmVy"
    "LCAoZ3VpbGRuYW1lLCB1c2VybmFtZSwgMCkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkK"
    "ICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4gTm9uZQogICAgZGVmIGxlYXZl"
    "R3VpbGQoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAg"
    "ICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHJlcyA9IGN1ci5leGVjdXRlKF9T"
    "UUxfZ3VpbGRPZlVzZXIsICh1c2VybmFtZSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHJl"
    "cyBpcyBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJl"
    "dHVybiAnbm90SW5HdWlsZCcKICAgICAgICAgICAgKGd1aWxkbmFtZSwgcmFuaykgPSAocmVzWzBd"
    "LCByZXNbMV0gb3IgMCkKICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxHdWlsZE1lbWJl"
    "ciwgKHVzZXJuYW1lLCkpCiAgICAgICAgICAgIG93bmVyID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWls"
    "ZE93bmVyLCAoZ3VpbGRuYW1lLCkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgb3duZXIgYW5k"
    "IG93bmVyWzBdID09IHVzZXJuYW1lOgogICAgICAgICAgICAgICAgI1RoZSBmb3VuZGVyIGxlYXZp"
    "bmcgZGlzc29sdmVzIHRoZSBndWlsZCByYXRoZXIgdGhhbiBsZWF2aW5nIGFuCiAgICAgICAgICAg"
    "ICAgICAjb3duZXJsZXNzIHJlY29yZCB0aGF0IG5vYm9keSBjYW4gZXZlciBhZG1pbmlzdGVyLgog"
    "ICAgICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxHdWlsZE1lbWJlcnMsIChndWlsZG5h"
    "bWUsKSkKICAgICAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfZGVsZXRlR3VpbGQsIChndWls"
    "ZG5hbWUsKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xv"
    "c2UoKQogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgbG9naW5QbGF5ZXIoc2VsZiwgdXNlcm5h"
    "bWUsIGNvbiwgcGFzc3dvcmQpOiNUT0RPIHNob3VsZCByZXR1cm4gZXJyb3IgcHJvcGVybHkgdG8g"
    "Y2xpZW50CiAgICAgICAgaWYgbm90IF9SRV9WQUxJRF9VU0VSTkFNRS5tYXRjaCh1c2VybmFtZSk6"
    "CiAgICAgICAgICAgICNSZWdpc3RyYXRpb24gaGFzIGFsd2F5cyB2YWxpZGF0ZWQgdGhlIG5hbWU7"
    "IGxvZ2dpbmcgaW4gZGlkIG5vdC4KICAgICAgICAgICAgI05hbWVzIHJlYWNoIG90aGVyIGNsaWVu"
    "dHMgaW5zaWRlIHF1b3RlZCBwcm90b2NvbCBmaWVsZHMsIHNvIGEgbmFtZQogICAgICAgICAgICAj"
    "Y29udGFpbmluZyAnIicgZm9yZ2VzIGNvbW1hbmRzIC0gYW5kIHRoZSBBbGxvd0FueUxvZ2luIGRl"
    "YnVnIHBhdGgKICAgICAgICAgICAgI2JlbG93IG5ldmVyIHRvdWNoZXMgdGhlIGRhdGFiYXNlLCB3"
    "aGljaCBtYWRlIGl0IHRoZSBvbmUgd2F5IHRvIGdldAogICAgICAgICAgICAjc3VjaCBhIG5hbWUg"
    "aW4uIENoZWNrIGhlcmUgc28gYm90aCBwYXRocyBhcmUgY292ZXJlZC4KICAgICAgICAgICAgcmV0"
    "dXJuIE5vbmUKICAgICAgICBpZiBfREVCVUdfQUxMT1dfQU5ZX0xPR0lOOiAjREVCVUcgQVVUTyBB"
    "TExPVwogICAgICAgICAgICByZXR1cm4gVXNlcih1c2VybmFtZSwgY29uKQogICAgICAgIHdpdGgg"
    "c2VsZi5sb2NrOgogICAgICAgICAgICBsb2dpbkN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAg"
    "ICAgICAgI0RlZmF1bHQgdG8gU1RSSUNULCBUT0RPIGFsbG93IGZvciBub24tc3RyaWN0PwogICAg"
    "ICAgICAgICB1aWRyZXMgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfdXNlcklEX3N0cmljdCwgKHVz"
    "ZXJuYW1lLCBjb24uU0spKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25l"
    "OgogICAgICAgICAgICAgICAgI3ByaW50KCdsb2dpbiBlcnJvcjogbm8gdXNlciB3aXRoIHRoYXQg"
    "c2VyaWFsIGtleScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAg"
    "ICAgICByZXR1cm4gTm9uZSAjTm8gc3VjaCBVc2VyCiAgICAgICAgICAgIHVpZCA9IHVpZHJlc1sw"
    "XQogICAgICAgICAgICAoclVzZXIsIHBhc3NoYXNoLCB1U2FsdCwgaEl0cikgPSBsb2dpbkN1ci5l"
    "eGVjdXRlKF9TUUxfZ2V0TG9naW4sICh1aWQsICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYg"
    "dXNlcm5hbWUgIT0gclVzZXI6CiAgICAgICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJvcjog"
    "d3JvbmcgdXNlcm5hbWU6IHt1c2VybmFtZX0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xv"
    "c2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1dyb25nIFVzZXJuYW1lCiAgICAgICAg"
    "ICAgIHRwYXMgPSBfc2FsdF9oYXNoXyhwYXNzd29yZCwgdVNhbHQsIGhJdHIpCiAgICAgICAgICAg"
    "IGlmIHRwYXMgIT0gcGFzc2hhc2g6CiAgICAgICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJv"
    "cjogd3JvbmcgcGFzc3dvcmQ6IHtwYXNzd29yZH0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIu"
    "Y2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1dyb25nIFBhc3N3b3JkCiAgICAg"
    "ICAgICAgIGlmIGhJdHIgIT0gX0hBU0hJVEVSOgogICAgICAgICAgICAgICAgbnBzaCA9IF9zYWx0"
    "X2hhc2hfKHBhc3N3b3JkLCB1U2FsdCwgX0hBU0hJVEVSKQogICAgICAgICAgICAgICAgbG9naW5D"
    "dXIuZXhlY3V0ZShfU1FMVVBEX3Bhc3NIYXNoLCAobnBzaCwgX0hBU0hJVEVSLCB1aWQpKQogICAg"
    "ICAgICAgICB1c2Vyb2JqID0gVXNlcih1c2VybmFtZSwgY29uKQogICAgICAgICAgICAjdXBkYXRl"
    "IGxhc3QgbG9naW4KICAgICAgICAgICAgbG9naW5DdXIuZXhlY3V0ZShfU1FMX2xvZ2luVXBkYXRl"
    "LCAodXNlcm9iai5sb2dpblRpbWUsIHVpZCkpCiAgICAgICAgICAgICNUT0RPIGRlZmF1bHQgZGF0"
    "ZXRpbWUgYWRhcHRlciBkZXByZWNhdGVkLCBjaGVjayByZXBsYWNlbWVudAogICAgICAgICAgICBz"
    "ZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAg"
    "cmV0dXJuIHVzZXJvYmoKICAgIGRlZiByZWdpc3RlclBsYXllcihzZWxmLCB1c2VybmFtZSwgY29u"
    "LCBwYXNzd29yZCwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgog"
    "ICAgICAgIGlmIG5vdCBfUkVfVkFMSURfVVNFUk5BTUUubWF0Y2godXNlcm5hbWUpOgogICAgICAg"
    "ICAgICByZXR1cm4gTm9uZSAjSW52YWxpZCB1c2VybmFtZSAoYmFkIGNoYXJzL2xlbmd0aCksIGFs"
    "c28gYmxvY2tzIHByb3RvY29sLWluamVjdGlvbiB2aWEgJyInCiAgICAgICAgZW1haWwgPSBzYW5p"
    "dGl6ZVRleHQoZW1haWwpCiAgICAgICAgbG9jYXRpb24gPSBzYW5pdGl6ZVRleHQobG9jYXRpb24p"
    "CiAgICAgICAgZGVzY3JpcHRpb24gPSBzYW5pdGl6ZVRleHQoZGVzY3JpcHRpb24pCiAgICAgICAg"
    "d2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGxvZ2luQ3VyID0gc2VsZi5kYi5jdXJzb3IoKQog"
    "ICAgICAgICAgICB1aWRyZXMgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5h"
    "bWUsICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIG5vdCBOb25lOgogICAg"
    "ICAgICAgICAgICAgI3ByaW50KGYncmVnaXN0ZXIgZXJyb3I6IHVzZXJuYW1lIGFscmVhZHkgaW4g"
    "dXNlOiB7dXNlcm5hbWV9JykKICAgICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAg"
    "ICAgICAgICAgIHJldHVybiBOb25lICNVc2VyIGV4aXN0cwogICAgICAgICAgICAjaWYgc3RyaWN0"
    "LCBjaGVjayBpZiBzZXJpYWwgaXMgaW4gdXNlIHRvbwogICAgICAgICAgICAjVE9ETyBvbmx5IGFw"
    "cGx5IGlmIHN0cmljdAogICAgICAgICAgICB1aWRyZXMgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxf"
    "dXNlcklEX1NjaGssIChjb24uU0ssICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVz"
    "IGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAgI3ByaW50KCdyZWdpc3RlciBlcnJvcjogc2Vy"
    "aWFsIGFscmVhZHkgaW4gdXNlJykKICAgICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAg"
    "ICAgICAgICAgICAgIHJldHVybiBOb25lICNTZXJpYWwgaW4gdXNlIGV4aXN0cwogICAgICAgICAg"
    "ICB1U2FsdCA9IG9zLnVyYW5kb20oMTYpCiAgICAgICAgICAgIHBIYXNoID0gX3NhbHRfaGFzaF8o"
    "cGFzc3dvcmQsIHVTYWx0LCBfSEFTSElURVIpCiAgICAgICAgICAgIGN1cnRpbWUgPSBkYXRldGlt"
    "ZS5kYXRldGltZS5ub3coKQogICAgICAgICAgICB0cnk6I3RyeSBzaG91bGRuJ3QgYmUgbmVlZGVk"
    "IGFzIGVtcHR5IGZpZWxkIGlzIHNldCB0byAyNTUKICAgICAgICAgICAgICAgIGFnZSA9IGludChh"
    "Z2UpCiAgICAgICAgICAgIGV4Y2VwdDoKICAgICAgICAgICAgICAgIGFnZSA9IDAKICAgICAgICAg"
    "ICAgeW9iID0gY3VydGltZS55ZWFyIC0gYWdlCiAgICAgICAgICAgIHJlZ3ZhbHMgPSAoCiAgICAg"
    "ICAgICAgICAgICB1c2VybmFtZSxwSGFzaCwKICAgICAgICAgICAgICAgIGNvbi5TSyx1U2FsdCxf"
    "SEFTSElURVIsCiAgICAgICAgICAgICAgICBjdXJ0aW1lLGVtYWlsLGxvY2F0aW9uLHlvYixnZW5k"
    "ZXIsZGVzY3JpcHRpb24KICAgICAgICAgICAgKQogICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRl"
    "KF9TUUxfcmVnaXN0ZXJVc2VyLCByZWd2YWxzKQogICAgICAgICAgICAjVE9ETyBkZWZhdWx0IGRh"
    "dGV0aW1lIGFkYXB0ZXIgZGVwcmVjYXRlZCwgY2hlY2sgcmVwbGFjZW1lbnQKICAgICAgICAgICAg"
    "dXNlcm9iaiA9IFVzZXIodXNlcm5hbWUsIGNvbikKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQo"
    "KQogICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgIHJldHVybiB1c2Vyb2Jq"
    "CiAgICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBh"
    "bmVsIGFjdGlvbiAoR1VJICLQo9C00LDQu9C40YLRjCDQv9C10YDRgdC+0L3QsNC20LAiKTogcGVy"
    "bWFuZW50bHkgcmVtb3ZlcyBhbgogICAgICAgICNhY2NvdW50IGFuZCBldmVyeSBzYXZlZCBwbGF5"
    "ZXJkYXRhIGJsb2IgZm9yIGl0LiBJcnJldmVyc2libGUgLSB0aGUKICAgICAgICAjR1VJIGlzIGV4"
    "cGVjdGVkIHRvIGNvbmZpcm0gd2l0aCB0aGUgYWRtaW4gYmVmb3JlIGNhbGxpbmcgdGhpcy4KICAg"
    "ICAgICAjRG9lcyBOT1QgdG91Y2ggdGhlIGNhbGxlcidzIGxpdmUgY29ubmVjdGlvbi9zZXNzaW9u"
    "OyB0aGUgY2FsbGVyIGlzCiAgICAgICAgI3Jlc3BvbnNpYmxlIGZvciBraWNraW5nIGZpcnN0IGlm"
    "IHRoZSBhY2NvdW50IGlzIGN1cnJlbnRseSBvbmxpbmUKICAgICAgICAjKHNlZSBDb3JlU2VydmVy"
    "LmRlbGV0ZUFjY291bnQpLCBvdGhlcndpc2UgYSBjb25uZWN0ZWQgY2xpZW50IHdvdWxkCiAgICAg"
    "ICAgI2tlZXAgcGxheWluZyB3aXRoIGFuIGFjY291bnQgdGhhdCBubyBsb25nZXIgZXhpc3RzIGlu"
    "IHRoZSBEQi4KICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5k"
    "Yi5jdXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBjdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwg"
    "KHVzZXJuYW1lLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgog"
    "ICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQog"
    "ICAgICAgICAgICB1aWQgPSB1aWRyZXNbMF0KICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9k"
    "ZWxldGVVc2VyLCAodXNlcm5hbWUsICkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAg"
    "ICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAjR3VpbGQgbWVtYmVyc2hpcCBvdXRsaXZlcyB0"
    "aGUgdXNlclRhYmxlIHJvdyBvdGhlcndpc2UsIHNvIHRoZSBkZWxldGVkCiAgICAgICAgI25hbWUg"
    "d291bGQga2VlcCBzaG93aW5nIHVwIGluIGl0cyBndWlsZCdzIHJvc3RlciBmb3JldmVyLgogICAg"
    "ICAgIHNlbGYubGVhdmVHdWlsZCh1c2VybmFtZSkKICAgICAgICAjUGxheWVyZGF0YSBmaWxlcyAo"
    "Int1c2VySUQ6eH1fe2Zvcm1JRDp4fS5iaW4iKSBsaXZlIG91dHNpZGUgdGhlIERCCiAgICAgICAg"
    "I3RyYW5zYWN0aW9uIGFuZCBhcmUgbG9va2VkIHVwIGJ5IHByZWZpeCAtIGJlc3QgZWZmb3J0LCBh"
    "IGxlZnRvdmVyCiAgICAgICAgI2ZpbGUgaGVyZSBpc24ndCB3b3J0aCBmYWlsaW5nIHRoZSB3aG9s"
    "ZSBkZWxldGlvbiBvdmVyLgogICAgICAgIHByZWZpeCA9IGYne3VpZDp4fV8nCiAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICBmb3IgZm4gaW4gb3MubGlzdGRpcihfUEFUSF9QTEFZRVJEQVRBKToKICAg"
    "ICAgICAgICAgICAgIGlmIGZuLnN0YXJ0c3dpdGgocHJlZml4KToKICAgICAgICAgICAgICAgICAg"
    "ICB0cnk6CiAgICAgICAgICAgICAgICAgICAgICAgIG9zLnJlbW92ZShvcy5wYXRoLmpvaW4oX1BB"
    "VEhfUExBWUVSREFUQSwgZm4pKQogICAgICAgICAgICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yOgog"
    "ICAgICAgICAgICAgICAgICAgICAgICBwYXNzCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAg"
    "ICAgICAgIHBhc3MKICAgICAgICByZXR1cm4gVHJ1ZQpHREggPSBEYXRhSGFuZGxlcigpCgpkZWYg"
    "X3dvVXNlcih1bCwgdXNyKToKICAgIHJldHVybiBsaXN0KCAoYSBmb3IgYSBpbiB1bCBpZiBhIGlz"
    "IG5vdCB1c3IpICkKZGVmIF9SZWFkQmxvYihjb24sIHNpemUpOgogICAgI3NpemUgY29tZXMgc3Ry"
    "YWlnaHQgb2ZmIHRoZSB3aXJlLCBzbyBpdCBpcyBuZWl0aGVyIHRydXN0ZWQgdG8gYmUgYSBudW1i"
    "ZXIKICAgICNub3IgdG8gYmUgc2FuZTogYSBjbGllbnQgY2xhaW1pbmcgYSBodWdlIGxlbmd0aCB1"
    "c2VkIHRvIG1ha2UgdGhlIHNlcnZlcgogICAgI2J1ZmZlciB1bmJvdW5kZWRseSAobWVtb3J5IGV4"
    "aGF1c3Rpb24pLCBhbmQgYSBjbGllbnQgdGhhdCBkaXNjb25uZWN0ZWQKICAgICNtaWQtYmxvYiBt"
    "YWRlIHJlY3YoKSByZXR1cm4gYicnIGZvcmV2ZXIgLSBhIDEwMCUgQ1BVIGJ1c3ktbG9vcCwgdGhl"
    "IHNhbWUKICAgICNkZWZlY3QgYWxyZWFkeSBmaXhlZCBpbiBDb25uZWN0aW9uSGFuZGxlci5fcmVj"
    "dk1vcmUoKS4KICAgIHRyeToKICAgICAgICBzaXplID0gaW50KHNpemUpCiAgICBleGNlcHQgKFR5"
    "cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2JhZCBi"
    "bG9iIHNpemUge3NpemUhcn0nKQogICAgaWYgc2l6ZSA8IDAgb3Igc2l6ZSA+IF9NQVhfQkxPQjoK"
    "ICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmxvYiBzaXplIHtzaXplfSBvdXQgb2YgcmFu"
    "Z2UgKG1heCB7X01BWF9CTE9CfSknKQogICAgI0EgYmxvYiByZWFkIGJsb2NrcyB0aGlzIGNvbm5l"
    "Y3Rpb24ncyBlbnRpcmUgaGFuZGxlciB0aHJlYWQuIEFubm91bmNpbmcgYQogICAgI2xlbmd0aCBh"
    "bmQgdGhlbiBnb2luZyBxdWlldCAtIGEgd2VkZ2VkIGNsaWVudCwgYSBsaW5rIHRoYXQgZHJvcHBl"
    "ZAogICAgI3dpdGhvdXQgYSByZXNldCAtIHVzZWQgdG8gYmxvY2sgaXQgZm9yZXZlcjogdGhlIHRo"
    "cmVhZCBuZXZlciByZXR1cm5lZCwgc28KICAgICN0aGUgcGxheWVyJ3MgYWNjb3VudCBzdGF5ZWQg"
    "Y2xhaW1lZCBhbmQgYW55IHJvb20gdGhleSBob3N0ZWQgc3RheWVkCiAgICAjbGlzdGVkIHdpdGgg"
    "bm90aGluZyBiZWhpbmQgaXQuIFRoZSBpZGxlIHRpbWVvdXQgbmV2ZXIgYXBwbGllZCBoZXJlLAog"
    "ICAgI2JlY2F1c2UgaXQgaXMgb25seSBjb25zdWx0ZWQgYnkgdGhlIHJlYWQgbG9vcCB0aGlzIGNh"
    "bGwgaGFzIHN0ZXBwZWQgb3V0CiAgICAjb2YuCiAgICBkZWFkbGluZSA9IHRpbWUubW9ub3Rvbmlj"
    "KCkgKyBfQkxPQl9USU1FT1VUCiAgICB3aGlsZSBsZW4oY29uLmRhdGEpIDwgc2l6ZToKICAgICAg"
    "ICByZW1haW5pbmcgPSBkZWFkbGluZSAtIHRpbWUubW9ub3RvbmljKCkKICAgICAgICBpZiByZW1h"
    "aW5pbmcgPD0gMDoKICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2Jsb2Igb2Yge3Np"
    "emV9IGJ5dGVzIG5vdCBkZWxpdmVyZWQgd2l0aGluICcKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICBmJ3tfQkxPQl9USU1FT1VUfXMgKHtsZW4oY29uLmRhdGEpfSByZWNlaXZlZCknKQog"
    "ICAgICAgIGNvbi5yZXF1ZXN0LnNldHRpbWVvdXQocmVtYWluaW5nKQogICAgICAgIHRyeToKICAg"
    "ICAgICAgICAgY2h1bmsgPSBjb24ucmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBl"
    "eGNlcHQgVGltZW91dEVycm9yOgogICAgICAgICAgICBjb250aW51ZSAjZGVhZGxpbmUgaXMgcmUt"
    "Y2hlY2tlZCBhdCB0aGUgdG9wIG9mIHRoZSBsb29wCiAgICAgICAgaWYgbm90IGNodW5rOgogICAg"
    "ICAgICAgICByYWlzZSBDb25uZWN0aW9uUmVzZXRFcnJvcignZGlzY29ubmVjdGVkIGR1cmluZyBi"
    "bG9iIHJlYWQnKQogICAgICAgIGNvbi5kYXRhICs9IGNodW5rCiAgICBibGJ1ZiA9IGNvbi5kYXRh"
    "WzA6c2l6ZV0KICAgIGNvbi5kYXRhID0gY29uLmRhdGFbc2l6ZTpdCiAgICByZXR1cm4gYmxidWYK"
    "CiNDb21tYW5kIGZ1bmN0aW9ucwpkZWYgX25vcChtZCx1c3IscmVzKToKICAgIHJldHVybiBOb25l"
    "CmRlZiBfdXBkaGVyb3BvcyhtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hh"
    "bm5lbDoKICAgICAgICByZXR1cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBjaGFubmVsLCBpZ25vcmUK"
    "ICAgICMgInh4eHgjeXl5eSIgcmVzcCAiVUlEI3h4eHgjeXl5eSIgLSB0aGUgY2xpZW50IHNlbmRz"
    "IGVpdGhlciBmb3JtLCBidXQKICAgICMgdXBkYXRlUG9zKCkgdW5jb25kaXRpb25hbGx5IHByZWZp"
    "eGVzIHRoZSBzZW5kZXIncyBpZCB3aGVuIGl0IGZhbnMgdGhlCiAgICAjIHBvc2l0aW9uIG91dC4g"
    "U3RvcmluZyB0aGUgcmF3IGZpZWxkIG1lYW50IHRoZSBzZWNvbmQgZm9ybSB3ZW50IGJhY2sgb3V0"
    "CiAgICAjIGFzICJVSUQjVUlEI3h4eHgjeXl5eSIsIHdoaWNoIG5vIGNsaWVudCBjYW4gbWF0Y2gg"
    "dG8gYSBwbGF5ZXI6IHRoYXQKICAgICMgaGVybydzIG1hcmtlciB0aGVuIHN0YXllZCB3aGVyZXZl"
    "ciBpdCB3YXMgbGFzdCBzdWNjZXNzZnVsbHkgcGFyc2VkIHdoaWxlCiAgICAjIHRoZSBwbGF5ZXIg"
    "YWN0dWFsbHkgd2Fsa2VkIGF3YXkuIEtlZXAgb25seSB0aGUgdHJhaWxpbmcgY29vcmRpbmF0ZSBw"
    "YWlyCiAgICAjIHNvIGV4YWN0bHkgb25lIGlkIGlzIHByZXNlbnQgb24gdGhlIHdpcmUgcmVnYXJk"
    "bGVzcyBvZiB3aGF0IHdhcyBzZW50LgogICAgdXNyLnVzZXIucG9zZGF0YSA9ICcjJy5qb2luKHJl"
    "c1sxXS5zcGxpdCgnIycpWy0yOl0pCiAgICB1c3IudXNlci5nYW1lY2hhbm5lbC5kaXJ0eSA9IFRy"
    "dWUKICAgIHVzci51c2VyLnBvc2NoYW5nZWQgPSBUcnVlCiAgICByZXR1cm4gTm9uZSAjbm8gcmVz"
    "cG9uc2UKZGVmIF9zZXRwbGF5ZXJkYXRhKG1kLHVzcixyZXMpOgogICAgcGQgPSBfUmVhZEJsb2Io"
    "dXNyLCByZXNbM10pCiAgICAjVE9ETyBDSEVDSyBwZXJtaXNzaW9ucyBmb3Igc2V0RGF0YShzZWxm"
    "IG9yIG90aGVyKQogICAgaWYgcmVzWzFdID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgR0RILnNl"
    "dFBsYXllckRhdGEocmVzWzFdLCByZXNbMl0sIHBkKQogICAgI1RPRE8gaGFuZGxlIHJlbWFpbmlu"
    "ZyB2YWx1ZXMKICAgICNyZXNbeF06CiAgICAjMDogL3NldHBsYXllcmRhdGEKICAgICMxOiBuYW1l"
    "CiAgICAjMjogZm9ybQogICAgIzM6IGJsb2JzaXplCiAgICAjNDogdW5rbm93biAocG9pbnRzPykK"
    "ICAgICM1OiB1bmtub3duLCAxIChib29sPykKICAgIHJldHVybiBOb25lCmRlZiBfZ2V0cGxheWVy"
    "ZGF0YShtZCx1c3IscmVzKToKICAgICNUT0RPIGNoZWNrIHBlcm1pc3Npb24gZm9yIGdldERhdGEo"
    "c2VsZiBvciBvdGhlcikKICAgIGlmIHJlc1sxXSA9PSB1c3IudXNlci5uYW1lOgogICAgICAgIHBk"
    "ID0gR0RILmdldFBsYXllckRhdGEocmVzWzFdLCByZXNbMl0pCiAgICAgICAgI3ByaW50KCdPYnRh"
    "aW5lZCBQbGF5ZXJkYXRhJywgbGVuKHBkKSkKICAgICAgICByZXR1cm4gX2VtKGYnL2dldHBsYXll"
    "cmRhdGEgIntyZXNbMV19IiAie3Jlc1syXX0iIHtsZW4ocGQpfScpK3BkCiAgICAjcHJpbnQoJ0Fj"
    "Y2VzcyBFcnJvcicsdXNyLnVzZXIubmFtZSwgJ0NhblwndCBnZXQgcGxheWVyZGF0YSBmb3InLHJl"
    "c1sxXSkKICAgIHJldHVybiBOb25lCmRlZiBfbGVhdmVnYW1lY2hhbm5lbChtZCx1c3IscmVzKToK"
    "ICAgIGNobmwgPSB1c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYgY2hubDoKICAgICAgICBjaG5s"
    "LmxlYXZlQ2hhbm5lbCh1c3IpCiAgICByZXR1cm4gdXNyLnNlcnZlci5zdGF0ZS5lbnVtZXJhdGVH"
    "QygpCmRlZiBfcmVxdWVzdGpvaW5nYW1lY2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwgPSB1"
    "c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy5nZXQocmVzWzFdKQogICAgaWYgY2hubCBpcyBO"
    "b25lOgogICAgICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sx"
    "XX0iICIwIicpICN1bmtub3duIGNoYW5uZWwKICAgICNUT0RPIGNoZWNrIHBlcm1pc3Npb25zPwog"
    "ICAgaWYgY2hubC5yZXF1ZXN0Sm9pbih1c3IpOgogICAgICAgIHJldHVybiBfZW0oZicvcmVxdWVz"
    "dGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0iICIxIicpCiAgICByZXR1cm4gX2VtKGYnL3JlcXVl"
    "c3Rqb2luZ2FtZWNoYW5uZWwgIntyZXNbMV19IiAiMCInKQpkZWYgX2pvaW5nYW1lY2hhbm5lbCht"
    "ZCx1c3IscmVzKToKICAgIGNobmwgPSB1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy5nZXQo"
    "cmVzWzFdKQogICAgaWYgY2hubCBpcyBOb25lOgogICAgICAgIHJldHVybiBOb25lICN1bmtub3du"
    "IGNoYW5uZWwsIGlnbm9yZQogICAgaWYgbGVuKHJlcyk+MjoKICAgICAgICB1c3IudXNlci5wb3Nk"
    "YXRhID0gJyMnLmpvaW4ocmVzWzJdLnNwbGl0KCcjJylbLTI6XSkKICAgIHJldHVybiBjaG5sLmpv"
    "aW5DaGFubmVsKHVzciwgcmVzWzFdKQpkZWYgX3NldHVzZXJoZXJvZGF0YShtZCx1c3IscmVzKToK"
    "ICAgIHBkID0gX1JlYWRCbG9iKHVzciwgcmVzWzJdKQogICAgdXNyLnVzZXIuaGVyb2RhdGEgPSBw"
    "ZAogICAgaWYgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgbXNnID0gdXNyLnVzZXIuZ2V0"
    "R0NVbXNnKCkKICAgICAgICB0ZyA9IF93b1VzZXIodXNyLnVzZXIuZ2FtZWNoYW5uZWwudXNlcmxp"
    "c3QsIHVzcikKICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzptc2d9KQogICAg"
    "cmV0dXJuIE5vbmUKZGVmIF9zZW5kKG1kLHVzcixyZXMpOgogICAgI1RPRE8gY29uc2lkZXIgc3Bl"
    "Y2lhbCBjaGF0IGNvbW1hbmRzIGhlcmUKICAgIGlmIG5vdCB1c3IudXNlci5jaGF0Y2hhbm5lbDoK"
    "ICAgICAgICByZXR1cm4gTm9uZQogICAgaWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9u"
    "ZQogICAgdGV4dCA9IHNhbml0aXplVGV4dChyZXNbMV0pCiAgICBpZiBub3QgdGV4dDoKICAgICAg"
    "ICByZXR1cm4gTm9uZQogICAgdWwgPSB1c3IudXNlci5jaGF0Y2hhbm5lbAogICAgbWQuYWRkKHsn"
    "dGFyZ2V0Jzp1bCwnbWVzc2FnZSc6X2VtKGYnL3NlbmQgInt1c3IudXNlci5uYW1lfSIgInt0ZXh0"
    "fSInKX0pCiAgICByZXR1cm4gTm9uZQpkZWYgX2dldGd1aWxkcmFua3BvaW50cyhtZCx1c3IscmVz"
    "KToKICAgIChhLGIsYyxkKSA9IF9ncnAoKQogICAgcmV0dXJuIF9lbShmJy9nZXRndWlsZHJhbmtw"
    "b2ludHMgInthfSIgIntifSIgIntjfSIgIntkfSInKQoKIyMgR1VJTERTCiNHdWlsZCBjcmVhdGlv"
    "biBkaWQgbm90aGluZyBhdCBhbGwgYmVmb3JlIHRoaXM6IHRoZXJlIHdhcyBubyAvY3JlYXRlZ3Vp"
    "bGQgKG9yCiNhbnl0aGluZyBlbHNlIGd1aWxkLXJlbGF0ZWQpIGluIF9DT01NQU5EUywgc28gdGhl"
    "IGNsaWVudCdzIHJlcXVlc3QgZmVsbAojdGhyb3VnaCB0byB0aGUgIlVua25vd24gQ29tbWFuZCIg"
    "YnJhbmNoIG9mIENvbW1hbmRQYXJzZXIucGFyc2UgYW5kIHdhcwojZHJvcHBlZC4gVGhlIGNsaWVu"
    "dCBnb3Qgbm8gcmVwbHksIG5vIGVycm9yLCBhbmQgbm8gZ3VpbGQuCiNOT1RFIE9OIENPTU1BTkQg"
    "TkFNRVM6IHRoZSBleGFjdCB3aXJlIG5hbWVzIHRoZSByZXRhaWwgY2xpZW50IHVzZXMgZm9yIHRo"
    "ZQojZ3VpbGQgVUkgYXJlIG5vdCBkb2N1bWVudGVkIGFueXdoZXJlIHdlIGhhdmUuIFRoZSBoYW5k"
    "bGVycyBiZWxvdyBhcmUKI3JlZ2lzdGVyZWQgdW5kZXIgZXZlcnkgc3BlbGxpbmcgdGhhdCBmaXRz"
    "IHRoaXMgcHJvdG9jb2wncyBjb252ZW50aW9ucywgYWxsCiNyb3V0ZWQgdG8gdGhlIHNhbWUgaW1w"
    "bGVtZW50YXRpb24sIHNvIHdoaWNoZXZlciBvbmUgdGhlIGNsaWVudCBhY3R1YWxseQojc2VuZHMg"
    "aXMgc2VydmVkLiBwYXJzZSgpIG5vdyBsb2dzIHRoZSByYXcgdGV4dCBvZiBhbnl0aGluZyBzdGls"
    "bCB1bm1hdGNoZWQsCiN3aGljaCBpcyBob3cgdG8gY29uZmlybS90cmltIHRoaXMgbGlzdCBmcm9t"
    "IGEgcmVhbCBzZXNzaW9uJ3MgbG9nLgpkZWYgX2d1aWxkUm9zdGVyKGd1aWxkbmFtZSk6CiAgICAj"
    "JyRndWlsZHVzZXInIGVudHJpZXMsIG1pcnJvcmluZyAkZ2FtZWNoYW5uZWx1c2VyLyRjaGF0Y2hh"
    "bm5lbHVzZXIuCiAgICBjaHVua3MgPSBbXQogICAgZm9yIChtbmFtZSwgcmFuaykgaW4gR0RILmdl"
    "dEd1aWxkTWVtYmVycyhndWlsZG5hbWUpOgogICAgICAgIGNodW5rcy5hcHBlbmQoX2VtKGYnJGd1"
    "aWxkdXNlciAie2d1aWxkbmFtZX0iICJ7bW5hbWV9IiAie3Jhbmt9IicpKQogICAgcmV0dXJuIGIn"
    "Jy5qb2luKGNodW5rcykKZGVmIF9hbm5vdW5jZUd1aWxkKHVzciwgZ3VpbGRuYW1lKToKICAgICNU"
    "ZWxsIHRoZSBwbGF5ZXIncyB0b3duIHRoYXQgdGhlaXIgZ3VpbGQgdGFnIGNoYW5nZWQsIHNvIG90"
    "aGVyIGNsaWVudHMgY2FuCiAgICAjdXBkYXRlIHRoZSBuYW1lIHRoZXkgc2hvdyBuZXh0IHRvIHRo"
    "ZW0gd2l0aG91dCBhIHJlbG9nLgogICAgY2hubCA9IHVzci51c2VyLmdhbWVjaGFubmVsCiAgICBp"
    "ZiBub3QgY2hubDoKICAgICAgICByZXR1cm4KICAgIG1zZyA9IF9lbShmJyRndWlsZCAie3Vzci51"
    "c2VyLm5hbWV9IiAie2d1aWxkbmFtZX0iJykKICAgIHVzci5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJn"
    "ZXQnOmNobmwudXNlcmxpc3QsJ21lc3NhZ2UnOm1zZ30pCmRlZiBfY3JlYXRlZ3VpbGQobWQsdXNy"
    "LHJlcyk6CiAgICBuYW1lID0gc2FuaXRpemVUZXh0KHJlc1sxXSkuc3RyaXAoKQogICAgZGVzY3Jp"
    "cHRpb24gPSBzYW5pdGl6ZVRleHQocmVzWzJdKSBpZiBsZW4ocmVzKT4yIGVsc2UgJycKICAgIGVy"
    "ciA9IEdESC5jcmVhdGVHdWlsZChuYW1lLCB1c3IudXNlci5uYW1lLCBkZXNjcmlwdGlvbikKICAg"
    "IGlmIGVycjoKICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIHtlcnJ9ICJ7bmFtZX0iJykKICAg"
    "IHByaW50KGYnW0xvYmJ5XSBHdWlsZCAie25hbWV9IiBjcmVhdGVkIGJ5IHt1c3IudXNlci5uYW1l"
    "fScpCiAgICBfYW5ub3VuY2VHdWlsZCh1c3IsIG5hbWUpCiAgICByZXR1cm4gX2VtKGYnL2NyZWF0"
    "ZWd1aWxkICJ7bmFtZX0iICIxIicpICsgX2d1aWxkUm9zdGVyKG5hbWUpCmRlZiBfam9pbmd1aWxk"
    "KG1kLHVzcixyZXMpOgogICAgbmFtZSA9IHNhbml0aXplVGV4dChyZXNbMV0pLnN0cmlwKCkKICAg"
    "IGVyciA9IEdESC5qb2luR3VpbGQobmFtZSwgdXNyLnVzZXIubmFtZSkKICAgIGlmIGVycjoKICAg"
    "ICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIHtlcnJ9ICJ7bmFtZX0iJykKICAgICNSZXBvcnQgdGhl"
    "IGd1aWxkJ3MgY2Fub25pY2FsIHNwZWxsaW5nIGJhY2ssIHdoaWNoIG1heSBkaWZmZXIgaW4gY2Fz"
    "ZSBmcm9tCiAgICAjd2hhdCB3YXMgdHlwZWQuCiAgICBuYW1lID0gR0RILmdldEd1aWxkTmFtZSh1"
    "c3IudXNlci5uYW1lKSBvciBuYW1lCiAgICBfYW5ub3VuY2VHdWlsZCh1c3IsIG5hbWUpCiAgICBy"
    "ZXR1cm4gX2VtKGYnL2pvaW5ndWlsZCAie25hbWV9IiAiMSInKSArIF9ndWlsZFJvc3RlcihuYW1l"
    "KQpkZWYgX2xlYXZlZ3VpbGQobWQsdXNyLHJlcyk6CiAgICAobmFtZSwgX3JhbmspID0gR0RILmdl"
    "dEd1aWxkT2YodXNyLnVzZXIubmFtZSkKICAgIGVyciA9IEdESC5sZWF2ZUd1aWxkKHVzci51c2Vy"
    "Lm5hbWUpCiAgICBpZiBlcnI6CiAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciB7ZXJyfSAiIicp"
    "CiAgICBfYW5ub3VuY2VHdWlsZCh1c3IsICcnKQogICAgcmV0dXJuIF9lbShmJy9sZWF2ZWd1aWxk"
    "ICJ7bmFtZSBvciAiIn0iICIxIicpCmRlZiBfZ3VpbGRpbmZvKG1kLHVzcixyZXMpOgogICAgbmFt"
    "ZSA9IHNhbml0aXplVGV4dChyZXNbMV0pLnN0cmlwKCkgaWYgbGVuKHJlcyk+MSBlbHNlIEdESC5n"
    "ZXRHdWlsZE5hbWUodXNyLnVzZXIubmFtZSkKICAgIGlmIG5vdCBuYW1lOgogICAgICAgIHJldHVy"
    "biBOb25lCiAgICBtZW1iZXJzID0gR0RILmdldEd1aWxkTWVtYmVycyhuYW1lKQogICAgaWYgbm90"
    "IG1lbWJlcnM6CiAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciB1bmtub3duR3VpbGQgIntuYW1l"
    "fSInKQogICAgcmV0dXJuIF9lbShmJy9ndWlsZGluZm8gIntuYW1lfSIgIntsZW4obWVtYmVycyl9"
    "IicpICsgX2d1aWxkUm9zdGVyKG5hbWUpCmRlZiBfcmVxdWVzdGNyZWF0ZWdhbWUobWQsdXNyLHJl"
    "cyk6CiAgICBpZiBub3QgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUg"
    "I25vdCBpbiBhIGdhbWUgY2hhbm5lbCAtIHVzZWQgdG8gcmFpc2UgQXR0cmlidXRlRXJyb3Igb24K"
    "ICAgICAgICAgICAgICAgICAgICAjTm9uZSBhbmQga2lsbCB0aGUgY29ubmVjdGlvbidzIGhhbmRs"
    "ZXIgdGhyZWFkCiAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZWNoYW5uZWwucmVxdWVzdENyZWF0ZUdh"
    "bWUodXNyLCByZXNbMV0pCmRlZiBfY3JlYXRlR2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1"
    "c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9uZSAjc2VlIF9yZXF1ZXN0Y3Jl"
    "YXRlZ2FtZQogICAgcmV0dXJuIHVzci51c2VyLmdhbWVjaGFubmVsLmNyZWF0ZUdhbWUocmVzWzFd"
    "LCB1c3IsIHJlc1syXSwgcmVzWzNdLCByZXNbNF0sIHJlc1s1XSwgcmVzWzZdLCByZXNbN10sIHJl"
    "c1s4XSwgcmVzWzldKQpkZWYgX3N0b3BnYW1lKG1kLHVzcixyZXMpOgogICAgaWYgdXNyLnVzZXIu"
    "Z2FtZToKICAgICAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZS5yZW1vdmUodXNyKQogICAgI3ByaW50"
    "KCdVc2VyIGlzIG5vdCBpbiBhIGdhbWUnKQogICAgcmV0dXJuIE5vbmUKZGVmIF9zdGFydGluZ2dh"
    "bWUobWQsdXNyLHJlcyk6CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHJldHVybiB1c3Iu"
    "dXNlci5nYW1lLnN0YXJ0R2FtZSh1c3IpCiAgICByZXR1cm4gTm9uZSAjVE9ETyB3aGF0IGRvZXMg"
    "dGhpcyBldmVuIGRvPwpkZWYgX3N0YXJ0Z2FtZShtZCx1c3IscmVzKToKICAgICNUT0RPIGhhbmRs"
    "ZSBwcm9wZXJseQogICAgaWYgdXNyLnVzZXIuZ2FtZToKICAgICAgICBwYXNzCiAgICByZXR1cm4g"
    "Tm9uZQpkZWYgX2dhbWVjb21tYW5kdG91c2VyKG1kLHVzcixyZXMpOgogICAgZGF0ID0gX1JlYWRC"
    "bG9iKHVzciwgcmVzWzJdKQogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHJlc1sxXSkK"
    "ICAgICNBbGxvdyBjb21tYW5kcyB0byBhbnkgY29ubmVjdGVkIHBsYXllciwgcmVnYXJkbGVzcyBv"
    "ZiBzdGF0ZSwgdG8gc3VwcG9ydCBtb2RkZWQgdXNlcwogICAgaWYgbm90IHRjb246CiAgICAgICAg"
    "I3ByaW50KCdQbGF5ZXI6JyxyZXNbMV0sJ2RvZXMgbm90IGV4aXN0PycpCiAgICAgICAgcmV0dXJu"
    "IE5vbmUKICAgICNUT0RPIGNvbnNpZGVyIG9wdGltaXNpbmcgdGhpcyBjb21tYW5kIGluIHBhcnRp"
    "Y3VsYXIKICAgIGZ1bG1zZyA9IF9lbShmJy9nYW1lY29tbWFuZHRvdXNlciAie3Vzci51c2VyLm5h"
    "bWV9IiAie2xlbihkYXQpfSInKStkYXQKICAgICNTdHJhaWdodCBvbnRvIHRoZSByZWNpcGllbnQn"
    "cyBvd24gb3V0Ym91bmQgcXVldWUgaW5zdGVhZCBvZiB2aWEgdGhlCiAgICAjc2VydmVyLXdpZGUg"
    "TWVzc2FnZURpc3RyaWJ1dG9yLiBUaGlzIGlzIHRoZSBjb21tYW5kIHRoYXQgY2FycmllcyB0aGUK"
    "ICAgICNhY3R1YWwgaW4tZ2FtZSB0cmFmZmljIGJldHdlZW4gdHdvIHBsYXllcnMsIGl0IGFsd2F5"
    "cyBoYXMgZXhhY3RseSBvbmUKICAgICNyZWNpcGllbnQsIGFuZCBzZW5kKCkgaXMganVzdCBhIHF1"
    "ZXVlIHB1dCAtIHNvIHRoZSBkaXN0cmlidXRvciBob3AgYm91Z2h0CiAgICAjbm90aGluZyBidXQg"
    "bGF0ZW5jeS4gV29yc2UsIHRoYXQgc2luZ2xlIGRpc3RyaWJ1dG9yIHRocmVhZCBpcyBzaGFyZWQg"
    "YnkKICAgICNldmVyeSBjb25uZWN0aW9uIG9uIHRoZSBzZXJ2ZXI6IG9uZSBzbG93IGZhbi1vdXQg"
    "KGEgcG9zaXRpb24gYnJvYWRjYXN0IHRvCiAgICAjYSBmdWxsIHRvd24sIGEgaGVyb2RhdGEgYmxv"
    "YikgcXVldWVkIGFoZWFkIG9mIGEgZ2FtZSBjb21tYW5kIGRlbGF5ZWQgaXQKICAgICNmb3IgZXZl"
    "cnlvbmUuIERpcmVjdCBoYW5kLW9mZiByZW1vdmVzIGJvdGggdGhlIGV4dHJhIHRocmVhZCB3YWtl"
    "LXVwIGFuZAogICAgI3RoYXQgaGVhZC1vZi1saW5lIGJsb2NraW5nLCBhbmQgcmVsYXkgb3JkZXIg"
    "YmV0d2VlbiBhbnkgZ2l2ZW4gcGFpciBvZgogICAgI3BsYXllcnMgaXMgc3RpbGwgcHJlc2VydmVk"
    "IGJlY2F1c2UgdGhleSBhbGwgdGFrZSB0aGlzIHNhbWUgcGF0aC4KICAgIHRjb24uc2VuZChmdWxt"
    "c2cpCiAgICByZXR1cm4gTm9uZQpkZWYgX2pvaW5nYW1lKG1kLHVzcixyZXMpOgogICAgaWYgbm90"
    "IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5rbm93"
    "bkdhbWUgIntyZXNbMV19IicpICNub3QgaW4gYSBnYW1lIGNoYW5uZWwKICAgIGdtID0gdXNyLnVz"
    "ZXIuZ2FtZWNoYW5uZWwuZ2FtZXMuZ2V0KHJlc1sxXSxOb25lKQogICAgaWYgZ20gPT0gTm9uZToK"
    "ICAgICAgICAjQW5zd2VyLCBkb24ndCBpZ25vcmU6IHRoZSBjbGllbnQgaXMgc2l0dGluZyBvbiBh"
    "ICJjb25uZWN0aW5nIiBkaWFsb2cKICAgICAgICAjdGhhdCBvbmx5IGEgcmVwbHkgZGlzbWlzc2Vz"
    "LiBIYXBwZW5zIHdoZW5ldmVyIHRoZSByb29tIGlzIHRvcm4gZG93bgogICAgICAgICNiZXR3ZWVu"
    "IHRoZSBwbGF5ZXIgc2VlaW5nIGl0IGluIHRoZSBsaXN0IGFuZCBjbGlja2luZyBpdC4KICAgICAg"
    "ICByZXR1cm4gX2VtKGYnL2Vycm9yIHVua25vd25HYW1lICJ7cmVzWzFdfSInKQogICAgI1RoZSBw"
    "YXNzd29yZCBhcmd1bWVudCBpcyBhYnNlbnQgd2hlbiB0aGUgcm9vbSBoYXMgbm9uZSAtIHNlZSB0"
    "aGUgYXJpdHkKICAgICNub3RlIG9uIF9DT01NQU5EUy4KICAgIHJldHVybiBnbS5hZGRVc2VyKHVz"
    "ciwgcmVzWzJdIGlmIGxlbihyZXMpPjIgZWxzZSAnJykKZGVmIF93aG9pcyhtZCx1c3IscmVzKToK"
    "ICAgIGlmIGxlbihyZXMpPDI6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHRhcmdldCA9IHJlc1sx"
    "XQogICAgaW5mbyA9IEdESC5nZXRXaG9pcyh0YXJnZXQpCiAgICBpZiBpbmZvIGlzIE5vbmU6CiAg"
    "ICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gdXNlcgogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0"
    "UGxheWVyKHRhcmdldCkKICAgIHRvd24gPSB0Y29uLnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiAo"
    "dGNvbiBhbmQgdGNvbi51c2VyLmdhbWVjaGFubmVsKSBlbHNlICcnCiAgICBjaGF0Y2hhbm5lbCA9"
    "ICcnCiAgICBpZiB0Y29uIGFuZCB0Y29uLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgZm9yIGNo"
    "biBpbiB1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy52YWx1ZXMoKToKICAgICAgICAgICAg"
    "Zm9yIGNuYW1lLCB1bGlzdCBpbiBjaG4uY2hhdENoYW5uZWxzLml0ZW1zKCk6CiAgICAgICAgICAg"
    "ICAgICBpZiB1bGlzdCBpcyB0Y29uLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgICAgICAg"
    "ICAgY2hhdGNoYW5uZWwgPSBjbmFtZQogICAgZ3VpbGQgPSBzYW5pdGl6ZVRleHQoR0RILmdldEd1"
    "aWxkTmFtZSh0YXJnZXQpKQogICAgcmV0dXJuIF9lbSgKICAgICAgICBmJy93aG9pcyAie3Rhcmdl"
    "dH0iICJ7Z3VpbGR9IiAie3Nhbml0aXplVGV4dCh0b3duKX0iICJ7c2FuaXRpemVUZXh0KGNoYXRj"
    "aGFubmVsKX0iICcKICAgICAgICBmJyJ7c2FuaXRpemVUZXh0KGluZm9bImVtYWlsIl0pfSIgIntz"
    "YW5pdGl6ZVRleHQoaW5mb1sibG9jYXRpb24iXSl9IiAnCiAgICAgICAgZid7aW5mb1siYWdlIl19"
    "IHtpbmZvWyJnZW5kZXIiXX0gIntzYW5pdGl6ZVRleHQoaW5mb1siZGVzY3JpcHRpb24iXSl9IicK"
    "ICAgICkKZGVmIF91cGRhdGUobWQsdXNyLHJlcyk6CiAgICAjL3VwZGF0ZSAibmFtZSIgImVtYWls"
    "IiAibG9jYXRpb24iICJhZ2UiICJnZW5kZXIiICJkZXNjcmlwdGlvbiIKICAgIGlmIGxlbihyZXMp"
    "PDY6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIHJlc1sxXSAhPSB1c3IudXNlci5uYW1lOgog"
    "ICAgICAgIHJldHVybiBOb25lICNjYW4gb25seSB1cGRhdGUgb3duIHdob2lzIGluZm8KICAgIGVt"
    "YWlsID0gc2FuaXRpemVUZXh0KHJlc1syXSkKICAgIGxvY2F0aW9uID0gc2FuaXRpemVUZXh0KHJl"
    "c1szXSkKICAgIGFnZSA9IHJlc1s0XQogICAgZ2VuZGVyID0gcmVzWzVdCiAgICBkZXNjcmlwdGlv"
    "biA9IHNhbml0aXplVGV4dChyZXNbNl0pIGlmIGxlbihyZXMpPjYgZWxzZSAnJwogICAgR0RILnVw"
    "ZGF0ZVdob2lzKHVzci51c2VyLm5hbWUsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRl"
    "c2NyaXB0aW9uKQogICAgcmV0dXJuIE5vbmUgI3NlcnZlciBzZW5kcyBubyByZXNwb25zZSwgcGVy"
    "IHByb3RvY29sIGRvYwoKX1JFX0NNRCA9IHJlLmNvbXBpbGUocicoPzoiKFteIl0qKSIpfChbXlxz"
    "XSspJykKI2NvbW1hbmQgLT4gKGhhbmRsZXIsIG1pbmltdW0gYXJndW1lbnQgY291bnQgKmV4Y2x1"
    "ZGluZyogdGhlIGNvbW1hbmQgd29yZCkuCiNUaGUgY291bnQgaXMgZW5mb3JjZWQgb25jZSwgY2Vu"
    "dHJhbGx5LCBpbiBwYXJzZSgpOiBldmVyeSBoYW5kbGVyIGluZGV4ZXMgaW50bwojcmVzW10gcG9z"
    "aXRpb25hbGx5LCBzbyBhIGNsaWVudCBzZW5kaW5nIGEgY29tbWFuZCB3aXRoIGZld2VyIGFyZ3Vt"
    "ZW50cyB0aGFuCiNleHBlY3RlZCB1c2VkIHRvIHJhaXNlIEluZGV4RXJyb3IgYW5kIHRlYXIgZG93"
    "biBpdHMgb3duIGNvbm5lY3Rpb24gdGhyZWFkLgojRGVjbGFyaW5nIHRoZSBhcml0eSBoZXJlIGtl"
    "ZXBzIHRoYXQgY2hlY2sgaW4gb25lIHBsYWNlIGluc3RlYWQgb2YgcmVwZWF0aW5nIGEKI2xlbihy"
    "ZXMpIGd1YXJkIGF0IHRoZSB0b3Agb2YgZmlmdGVlbiBoYW5kbGVycy4KX0NPTU1BTkRTID0gewog"
    "ICAgJy9ub3AnOiAgICAgICAgICAgICAgICAgICAgKF9ub3AsIDApLAogICAgJy9sZWF2ZWdhbWVj"
    "aGFubmVsJzogICAgICAgKF9sZWF2ZWdhbWVjaGFubmVsLCAwKSwKICAgICcvcmVxdWVzdGpvaW5n"
    "YW1lY2hhbm5lbCc6IChfcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCwgMSksCiAgICAjQXJpdHkgMSwg"
    "bm90IDI6IHRoZSBwb3NpdGlvbiBhcmd1bWVudCBpcyBvcHRpb25hbCAodGhlIGNsaWVudCBvbWl0"
    "cyBpdAogICAgI3doZW4gaXQgaGFzIG5vIGxhc3Qta25vd24gcG9zaXRpb24geWV0LCBlLmcuIHRo"
    "ZSB2ZXJ5IGZpcnN0IHRvd24gZW50cnkKICAgICNhZnRlciBsb2dpbikuIFJlcXVpcmluZyBpdCBt"
    "YWRlIHBhcnNlKCkgZHJvcCB0aGUgY29tbWFuZCBzaWxlbnRseSwgd2hpY2gKICAgICN0aGUgY2xp"
    "ZW50IGV4cGVyaWVuY2VzIGFzIGEgdG93biBpdCBjYW4gbmV2ZXIgZmluaXNoIGxvYWRpbmcuCiAg"
    "ICAnL2pvaW5nYW1lY2hhbm5lbCc6ICAgICAgICAoX2pvaW5nYW1lY2hhbm5lbCwgMSksCiAgICAn"
    "L3VwZGhlcm9wb3MnOiAgICAgICAgICAgICAoX3VwZGhlcm9wb3MsIDEpLAogICAgJy9zZW5kJzog"
    "ICAgICAgICAgICAgICAgICAgKF9zZW5kLCAxKSwKICAgICcvZ2V0Z3VpbGRyYW5rcG9pbnRzJzog"
    "ICAgIChfZ2V0Z3VpbGRyYW5rcG9pbnRzLCAwKSwKICAgICcvcmVxdWVzdGNyZWF0ZWdhbWUnOiAg"
    "ICAgIChfcmVxdWVzdGNyZWF0ZWdhbWUsIDEpLAogICAgJy9jcmVhdGVnYW1lJzogICAgICAgICAg"
    "ICAgKF9jcmVhdGVHYW1lLCA5KSwKICAgICcvc3RvcGdhbWUnOiAgICAgICAgICAgICAgIChfc3Rv"
    "cGdhbWUsIDApLAogICAgJy9sZWF2ZWdhbWUnOiAgICAgICAgICAgICAgKF9zdG9wZ2FtZSwgMCks"
    "I1RPRE8gZml4IGZvciBtdWx0aXBsZSB1c2Vycz8KICAgICcvc3RhcnRpbmdnYW1lJzogICAgICAg"
    "ICAgIChfc3RhcnRpbmdnYW1lLCAwKSwKICAgICcvc3RhcnRnYW1lJzogICAgICAgICAgICAgIChf"
    "c3RhcnRnYW1lLCAwKSwKICAgICcvZ2V0cGxheWVyZGF0YSc6ICAgICAgICAgIChfZ2V0cGxheWVy"
    "ZGF0YSwgMiksCiAgICAnL3NldHBsYXllcmRhdGEnOiAgICAgICAgICAoX3NldHBsYXllcmRhdGEs"
    "IDMpLAogICAgJy9zZXR1c2VyaGVyb2RhdGEnOiAgICAgICAgKF9zZXR1c2VyaGVyb2RhdGEsIDIp"
    "LAogICAgJy9nYW1lY29tbWFuZHRvdXNlcic6ICAgICAgKF9nYW1lY29tbWFuZHRvdXNlciwgMiks"
    "I1RPRE8gY29uc2lkZXIgb3B0aW1pc2luZwogICAgI0FyaXR5IDE6IHRoZSBwYXNzd29yZCBhcmd1"
    "bWVudCBpcyBhYnNlbnQgZm9yIGEgcm9vbSB0aGF0IGhhcyBub25lLCBhbmQKICAgICNkcm9wcGlu"
    "ZyB0aGUgY29tbWFuZCBsZWZ0IHRoZSBqb2luaW5nIHBsYXllciBvbiAiY29ubmVjdGluZyIgZm9y"
    "ZXZlci4KICAgICcvam9pbmdhbWUnOiAgICAgICAgICAgICAgIChfam9pbmdhbWUsIDEpLAogICAg"
    "Jy93aG9pcyc6ICAgICAgICAgICAgICAgICAgKF93aG9pcywgMSksCiAgICAnL3VwZGF0ZSc6ICAg"
    "ICAgICAgICAgICAgICAoX3VwZGF0ZSwgNSksCiAgICAjR3VpbGRzIC0gc2VlIHRoZSBub3RlIGFi"
    "b3ZlIF9jcmVhdGVndWlsZCBhYm91dCB0aGUgYWx0ZXJuYXRpdmUgc3BlbGxpbmdzLgogICAgJy9j"
    "cmVhdGVndWlsZCc6ICAgICAgICAgICAgKF9jcmVhdGVndWlsZCwgMSksCiAgICAnL3JlcXVlc3Rj"
    "cmVhdGVndWlsZCc6ICAgICAoX2NyZWF0ZWd1aWxkLCAxKSwKICAgICcvY3JlYXRndWlsZCc6ICAg"
    "ICAgICAgICAgIChfY3JlYXRlZ3VpbGQsIDEpLAogICAgJy9ndWlsZGNyZWF0ZSc6ICAgICAgICAg"
    "ICAgKF9jcmVhdGVndWlsZCwgMSksCiAgICAnL2pvaW5ndWlsZCc6ICAgICAgICAgICAgICAoX2pv"
    "aW5ndWlsZCwgMSksCiAgICAnL3JlcXVlc3Rqb2luZ3VpbGQnOiAgICAgICAoX2pvaW5ndWlsZCwg"
    "MSksCiAgICAnL2xlYXZlZ3VpbGQnOiAgICAgICAgICAgICAoX2xlYXZlZ3VpbGQsIDApLAogICAg"
    "Jy9xdWl0Z3VpbGQnOiAgICAgICAgICAgICAgKF9sZWF2ZWd1aWxkLCAwKSwKICAgICcvZ3VpbGRp"
    "bmZvJzogICAgICAgICAgICAgIChfZ3VpbGRpbmZvLCAwKSwKICAgICcvZ2V0Z3VpbGRpbmZvJzog"
    "ICAgICAgICAgIChfZ3VpbGRpbmZvLCAwKSwKfQpjbGFzcyBDb21tYW5kUGFyc2VyKCk6CiAgICBk"
    "ZWYgX19pbml0X18oc2VsZiwgbXNnZXIpOgogICAgICAgIHNlbGYuY29tbWFuZGxpc3QgPSBfQ09N"
    "TUFORFMKICAgICAgICBzZWxmLm1kID0gbXNnZXIKCiAgICBkZWYgcGFyc2Uoc2VsZiwgZGF0YSwg"
    "b3JpZ2luKToKICAgICAgICAjcHJpbnQoZidUZXN0IFBhcnNpbmcge2xlbihkYXRhKX06IHtieXRl"
    "cyhkYXRhLCAnYXNjaWknKX0nKQogICAgICAgIHJlcyA9IGxpc3QoIChpdG1bMF0raXRtWzFdIGZv"
    "ciBpdG0gaW4gX1JFX0NNRC5maW5kYWxsKGRhdGEpKSApCiAgICAgICAgI3ByaW50KCdSZXM6Jywg"
    "cmVzKQogICAgICAgIGlmIG5vdCByZXM6CiAgICAgICAgICAgICNXYXMgYSBzaWxlbnQgZHJvcC4g"
    "SWYgYSBmZWF0dXJlIGRvZXMgbm90aGluZyBhbmQgdGhlIGxvZyBzaG93cyBubwogICAgICAgICAg"
    "ICAjY29tbWFuZCBmb3IgaXQgYXQgYWxsLCB0aGlzIGlzIG9uZSBvZiB0aGUgdHdvIHBsYWNlcyBp"
    "dCBjb3VsZAogICAgICAgICAgICAjaGF2ZSBkaXNhcHBlYXJlZCBpbnRvIC0gc28gc2F5IHNvIHJh"
    "dGhlciB0aGFuIGxlYXZlIGEgYmxpbmQgc3BvdC4KICAgICAgICAgICAgaWYgX0RFQlVHX0xPR19D"
    "T01NQU5EUyBhbmQgZGF0YToKICAgICAgICAgICAgICAgIHdobyA9IG9yaWdpbi51c2VyLm5hbWUg"
    "aWYgb3JpZ2luLnVzZXIgZWxzZSAnPycKICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3do"
    "b30gLT4gKFVOUEFSU0VBQkxFKSB7ZGF0YSFyfScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAg"
    "ICAgICAgd2hvID0gb3JpZ2luLnVzZXIubmFtZSBpZiBvcmlnaW4udXNlciBlbHNlICc/JwogICAg"
    "ICAgIGxvdWQgPSBfREVCVUdfTE9HX0NPTU1BTkRTIGFuZCAoX0RFQlVHX0xPR19WRVJCT1NFIG9y"
    "IHJlc1swXSBub3QgaW4gX1FVSUVUX0NPTU1BTkRTKQogICAgICAgIGlmIGxvdWQ6CiAgICAgICAg"
    "ICAgIHByaW50KGYnW2NtZF0ge3dob30gLT4ge2RhdGF9JykKICAgICAgICBlbnRyeSA9IHNlbGYu"
    "Y29tbWFuZGxpc3QuZ2V0KHJlc1swXSkKICAgICAgICBpZiBlbnRyeSBpcyBOb25lOgogICAgICAg"
    "ICAgICAjTG9nIHRoZSByYXcgbGluZSwgbm90IGp1c3QgdGhlIHRva2VuaXNlZCBsaXN0LiBBbiB1"
    "bmltcGxlbWVudGVkCiAgICAgICAgICAgICNjb21tYW5kIGlzIGV4YWN0bHkgdGhlIHNpdHVhdGlv"
    "biB3aGVyZSB0aGUgYXJndW1lbnQgbGF5b3V0IGlzCiAgICAgICAgICAgICN3aGF0IHdlIG5lZWQg"
    "dG8gc2VlLCBhbmQgcmUtcXVvdGluZyB0aGUgc3BsaXQgdG9rZW5zIGxvc2VzIGl0LgogICAgICAg"
    "ICAgICBwcmludChmJyoqKiBVTktOT1dOIENPTU1BTkQgZnJvbSB7d2hvfToge2RhdGEhcn0nKQog"
    "ICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIGhhbmRsZXIsIG1pbmFyZ3MgPSBlbnRyeQog"
    "ICAgICAgIGlmIGxlbihyZXMpIC0gMSA8IG1pbmFyZ3M6CiAgICAgICAgICAgIHByaW50KGYnKioq"
    "IE1BTEZPUk1FRCBDT01NQU5EIGZyb20ge3dob306ICcKICAgICAgICAgICAgICAgICAgZid7cmVz"
    "WzBdfSBuZWVkcyB7bWluYXJnc30gYXJndW1lbnQocyksIGdvdCB7bGVuKHJlcyktMX0nKQogICAg"
    "ICAgICAgICByZXR1cm4gTm9uZQogICAgICAgICNwcmludChmJ1BhcnNlZCBDb21tYW5kIEZyb20g"
    "e29yaWdpbi51c2VyLm5hbWV9OicsIHJlcykKICAgICAgICBvdXQgPSBoYW5kbGVyKHNlbGYubWQs"
    "IG9yaWdpbiwgcmVzKQogICAgICAgIGlmIGxvdWQ6CiAgICAgICAgICAgICMiKG5vIGRpcmVjdCBy"
    "ZXBseSkiIGlzIHRoZSBzaWduYXR1cmUgb2YgZXZlcnkgaGFuZyByZXBvcnRlZCBzbwogICAgICAg"
    "ICAgICAjZmFyOiB0aGUgY2xpZW50IHdhaXRzIG9uIGFuIGFuc3dlciB0aGF0IHRoaXMgc2VydmVy"
    "IG5ldmVyIHNlbmRzLgogICAgICAgICAgICAjU29tZSBjb21tYW5kcyBsZWdpdGltYXRlbHkgYW5z"
    "d2VyIHdpdGggbm90aGluZywgc28gdGhpcyBpcyBhIGxlYWQsCiAgICAgICAgICAgICNub3QgYSB2"
    "ZXJkaWN0IC0gYnV0IGl0IGlzIHRoZSBmaXJzdCB0aGluZyB0byBsb29rIGF0LgogICAgICAgICAg"
    "ICBpZiBvdXQ6CiAgICAgICAgICAgICAgICBoZWFkID0gb3V0LnNwbGl0KF9OKVswXS5kZWNvZGUo"
    "X1dJUkVfRU5DLCAncmVwbGFjZScpCiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99"
    "IDwtIHtoZWFkfScpCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBwcmludChmJ1tj"
    "bWRdIHt3aG99IDwtIChubyBkaXJlY3QgcmVwbHkpJykKICAgICAgICByZXR1cm4gb3V0CgojdGhy"
    "ZWFkIHRvIHNlbmQgbWVzc2FnZXMgYWNyb3NzIGFsbCBjb25uZWN0ZWQgY2xpZW50cwojX19FWEFN"
    "UExFX01FU1NBR0VfXyA9IHsKIyAgICAndGFyZ2V0JzpbJ3VzZXJsaXN0J10sCiMgICAgJ21lc3Nh"
    "Z2UnOmInL3doYXRldmVyXDAnK2InYmxvYicKI30KY2xhc3MgTWVzc2FnZURpc3RyaWJ1dG9yKCk6"
    "CiAgICBfRU5ESVRFTSA9IFsnU1RPUCddCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyKToK"
    "ICAgICAgICBzZWxmLl9jUXVldWUgPSBTaW1wbGVRdWV1ZSgpCiAgICAgICAgc2VsZi5zZXJ2ZXIg"
    "PSBzZXJ2ZXIKICAgIGRlZiBzZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIHdoaWxlIFRydWU6"
    "ICNUT0RPIHBvc3NpYmxlIGNoZWNrIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nCiAgICAgICAgICAg"
    "IHRyeToKICAgICAgICAgICAgICAgIGNvbW1hbmQgPSBzZWxmLl9jUXVldWUuZ2V0KCkKICAgICAg"
    "ICAgICAgICAgICNwcmludCgnTUQ6JywgY29tbWFuZCwgc2VsZi5zZXJ2ZXIuX2lzX2Nsb3Npbmcp"
    "CiAgICAgICAgICAgICAgICBpZiBjb21tYW5kID09IHNlbGYuX0VORElURU06CiAgICAgICAgICAg"
    "ICAgICAgICAgYnJlYWsKICAgICAgICAgICAgICAgIHVsID0gY29tbWFuZC5nZXQoJ3RhcmdldCcs"
    "W10pCiAgICAgICAgICAgICAgICBtc2cgPSBjb21tYW5kLmdldCgnbWVzc2FnZScpCiAgICAgICAg"
    "ICAgICAgICBpZiBtc2c6CiAgICAgICAgICAgICAgICAgICAgZm9yIHVzciBpbiB1bDoKICAgICAg"
    "ICAgICAgICAgICAgICAgICAgdXNyLnNlbmQobXNnKQogICAgICAgICAgICBleGNlcHQgRXhjZXB0"
    "aW9uOgogICAgICAgICAgICAgICAgcHJpbnQoJ1tMb2JieV0gRGlzdHJpYnV0b3IgZXJyb3I6XG4n"
    "ICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgIGRlZiBhZGQoc2VsZiwgcHJvcHMpOgogICAg"
    "ICAgICNTbmFwc2hvdCB0aGUgdGFyZ2V0IGxpc3QgSEVSRSwgaW4gdGhlIGNhbGxpbmcgdGhyZWFk"
    "LiBDYWxsZXJzIGhhbmQgdXMKICAgICAgICAjbGl2ZSBjb250YWluZXJzIChHYW1lQ2hhbm5lbC51"
    "c2VybGlzdCwgc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCksIC4uLikKICAgICAgICAjdGhhdCBv"
    "dGhlciBoYW5kbGVyIHRocmVhZHMgYXBwZW5kIHRvL3JlbW92ZSBmcm9tIGNvbnRpbnVvdXNseTsg"
    "dGhlCiAgICAgICAgI2Rpc3RyaWJ1dG9yIHRocmVhZCBpdGVyYXRlZCB0aGVtIGxhdGVyIGFuZCBo"
    "aXQgJ2xpc3QgY2hhbmdlZCBzaXplCiAgICAgICAgI2R1cmluZyBpdGVyYXRpb24nLCB3aGljaCB0"
    "aGUgZXhjZXB0IGFib3ZlIHN3YWxsb3dlZCAtIHNpbGVudGx5CiAgICAgICAgI2Ryb3BwaW5nIHRo"
    "ZSBlbnRpcmUgYnJvYWRjYXN0LiB1cGRhdGVQb3MoKSBkb2VzIHRoaXMgb25jZSBhIHNlY29uZCBm"
    "b3IKICAgICAgICAjZXZlcnkgY2hhbm5lbCwgc28gdGhpcyB3YXMgdGhlIGhvdCBwYXRoIGZvciB0"
    "aGUgcmFjZS4KICAgICAgICBpZiBpc2luc3RhbmNlKHByb3BzLCBkaWN0KToKICAgICAgICAgICAg"
    "cHJvcHMgPSBkaWN0KHByb3BzKQogICAgICAgICAgICBwcm9wc1sndGFyZ2V0J10gPSBsaXN0KHBy"
    "b3BzLmdldCgndGFyZ2V0Jykgb3IgKCkpCiAgICAgICAgc2VsZi5fY1F1ZXVlLnB1dChwcm9wcykK"
    "ICAgIGRlZiBlbmQoc2VsZik6CiAgICAgICAgc2VsZi5hZGQoc2VsZi5fRU5ESVRFTSkKICAgIApj"
    "bGFzcyBHYW1lRW50cnkoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBwYXJlbnQsIG5hbWUsIGhv"
    "c3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4xLCBzdGF0dXMsIG1heHBsYXllcnMsIHVybCk6"
    "CiAgICAgICAgaWYgaG9zdC51c2VyLmdhbWU6CiAgICAgICAgICAgIGhvc3QudXNlci5nYW1lLnJl"
    "bW92ZShob3N0KQogICAgICAgIHNlbGYucGFyZW50ID0gcGFyZW50ICMgR2FtZWNoYW5uZWwKICAg"
    "ICAgICBzZWxmLmduYW1lID0gbmFtZSAjCiAgICAgICAgc2VsZi5ob3N0ID0gaG9zdCAjIENvbm5l"
    "Y3Rpb24gT2JqZWN0CiAgICAgICAgc2VsZi5wYXNzd29yZCA9IHBhc3cgIyAnJyBvciAncGFzc3dv"
    "cmQnCiAgICAgICAgc2VsZi5tYXBQYXIgPSBtYXBwICMgIk5ldF9NXzAxIG51bGwgMCAxIgogICAg"
    "ICAgIHNlbGYubWFwVHJhbnNsYXRlID0gbWFwdCAjICJ0cmFuc2xhdGVOZXRfTV8wMSIKICAgICAg"
    "ICBzZWxmLm5waiA9IGludChucGopICMgImVuYWJsZSBuZXcgcGxheWVyIHRvIGpvaW4gKGJvb2wp"
    "IgogICAgICAgIHNlbGYudW4xID0gaW50KHVuMSkgIyAwIFRPRE8gZmlndXJlIG91dCBpZiBtZWFu"
    "cyAiZ3VpbGQgZ2FtZSIKICAgICAgICBzZWxmLnN0YXR1cyA9IGludChzdGF0dXMpICMgY2hhbmdl"
    "cyB0byAxIHdoZW4gc3RhcnRlZCwgb25seSByZWxldmFudCB3aGVuIG5waiB0cnVlCiAgICAgICAg"
    "c2VsZi5tYXhwbGF5ZXJzID0gaW50KG1heHBsYXllcnMpICMgOCAjbWF4IHVzZXJzPwogICAgICAg"
    "ICN4LWRpcmVjdHBsYXkgdXJsLCB3aXRoIHRoZSBob3N0J3MgYWR2ZXJ0aXNlZCBhZGRyZXNzIHJl"
    "cGxhY2VkIGJ5IHRoZQogICAgICAgICNhZGRyZXNzIHRoaXMgc2VydmVyIHNlZXMgaXQgY29ubmVj"
    "dCBmcm9tIC0gc2VlIHJld3JpdGVHYW1lSG9zdCgpLgogICAgICAgIHBlZXIgPSBob3N0LmNsaWVu"
    "dF9hZGRyZXNzWzBdIGlmIGhvc3QuY2xpZW50X2FkZHJlc3MgZWxzZSAnJwogICAgICAgIChzZWxm"
    "LnVybCwgbm90ZSkgPSByZXdyaXRlR2FtZUhvc3QodXJsLCBwZWVyKQogICAgICAgIHByaW50KGYn"
    "W0xvYmJ5XSBSb29tICJ7bmFtZX0iIGJ5IHtob3N0LnVzZXIubmFtZX06IHtub3RlfScpCiAgICAg"
    "ICAgcHJpbnQoZidbTG9iYnldICAgdXJsIGFkdmVydGlzZWQgdG8gam9pbmVyczoge3NlbGYudXJs"
    "fScpCiAgICAgICAgc2VsZi51c2VybGlzdCA9IFtob3N0LF0KICAgICAgICBzZWxmLnBhcmVudC5n"
    "YW1lc1tzZWxmLmduYW1lXSA9IHNlbGYKICAgICAgICBzZWxmLmhvc3QudXNlci5nYW1lID0gc2Vs"
    "ZgogICAgICAgICNBZHZlcnRpc2Ugb24gY3JlYXRpb24KICAgICAgICBtc2cgPSBzZWxmLmdldEdh"
    "bWVTdHJpbmcoKQogICAgICAgIHRnID0gc2VsZi5wYXJlbnQudXNlcmxpc3QKICAgICAgICBzZWxm"
    "LnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzptc2d9KQogICAg"
    "ZGVmIF9hdWRpZW5jZShzZWxmKToKICAgICAgICAjV2hvIG5lZWRzIHRvIGhlYXIgYWJvdXQgdGhp"
    "cyByb29tIGNoYW5naW5nOiBldmVyeW9uZSBicm93c2luZyB0aGUKICAgICAgICAjdG93biwgcGx1"
    "cyBldmVyeW9uZSBhbHJlYWR5IGluc2lkZSB0aGUgcm9vbS4gT25jZSBhIGdhbWUgc3RhcnRzIGl0"
    "cwogICAgICAgICNwbGF5ZXJzIGFyZSB0YWtlbiBvZmYgdGhlIHRvd24gcm9zdGVyIChzZWUgc3Rh"
    "cnRHYW1lKSwgc28gdGhlIHRvd24KICAgICAgICAjbGlzdCBhbG9uZSBubyBsb25nZXIgcmVhY2hl"
    "cyB0aGVtIC0gYW5kIHRoZSBob3N0LCB3aG8gaXMgYWx3YXlzCiAgICAgICAgI2luLWdhbWUsIGlz"
    "IGV4YWN0bHkgd2hvIG5lZWRzIHRvIGtub3cgdGhhdCBzb21lYm9keSBqb2luZWQuCiAgICAgICAg"
    "c2VlbiA9IGxpc3Qoc2VsZi5wYXJlbnQudXNlcmxpc3QpCiAgICAgICAgZm9yIGMgaW4gc2VsZi51"
    "c2VybGlzdDoKICAgICAgICAgICAgaWYgYyBub3QgaW4gc2VlbjoKICAgICAgICAgICAgICAgIHNl"
    "ZW4uYXBwZW5kKGMpCiAgICAgICAgcmV0dXJuIHNlZW4KICAgIGRlZiBhZGRVc2VyKHNlbGYsIHVz"
    "ciwgcGFzdyk6CiAgICAgICAgI0V2ZXJ5IHJlamVjdGlvbiBiZWxvdyBoYXMgdG8gYW5zd2VyIHRo"
    "ZSBjbGllbnQgd2l0aCAqc29tZXRoaW5nKi4gVGhlCiAgICAgICAgI2NsaWVudCBzaG93cyAiY29u"
    "bmVjdGluZy4uLiIgZnJvbSB0aGUgbW9tZW50IGl0IHNlbmRzIC9qb2luZ2FtZSB1bnRpbAogICAg"
    "ICAgICN0aGUgc2VydmVyIGFuc3dlcnMsIGFuZCBpdCBoYXMgbm8gdGltZW91dCBvZiBpdHMgb3du"
    "OiByZXR1cm5pbmcgTm9uZQogICAgICAgICNsZWZ0IHRoZSBwbGF5ZXIgc3RhcmluZyBhdCB0aGF0"
    "IGRpYWxvZyB1bnRpbCB0aGV5IGtpbGxlZCB0aGUgZ2FtZS4KICAgICAgICBpZiB1c3IgaW4gc2Vs"
    "Zi51c2VybGlzdDoKICAgICAgICAgICAgI0FscmVhZHkgaW4gKGR1cGxpY2F0ZSAvam9pbmdhbWUs"
    "IGUuZy4gdGhlIHBsYXllciBkb3VibGUtY2xpY2tlZAogICAgICAgICAgICAjdGhlIHJvb20pLiBS"
    "ZS1hbnN3ZXIgaW5zdGVhZCBvZiBhcHBlbmRpbmcgdGhlbSBhIHNlY29uZCB0aW1lLgogICAgICAg"
    "ICAgICByZXR1cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi51cmx9IiAi"
    "e3NlbGYuc3RhdHVzfSInKQogICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KT49c2VsZi5tYXhw"
    "bGF5ZXJzOgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVGdWxsICJ7c2VsZi5n"
    "bmFtZX0iJykKICAgICAgICBpZiBzZWxmLnN0YXR1cyBhbmQgbm90IHNlbGYubnBqOgogICAgICAg"
    "ICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVBbHJlYWR5U3RhcnRlZCAie3NlbGYuZ25hbWV9"
    "IicpCiAgICAgICAgaWYgc2VsZi5wYXNzd29yZCAhPSBwYXN3OgogICAgICAgICAgICByZXR1cm4g"
    "X2VtKGYnL2Vycm9yIGJhZEdhbWVQYXNzd29yZCAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgaWYg"
    "dXNyLnVzZXIuZ2FtZSBpcyBub3QgTm9uZToKICAgICAgICAgICAgdXNyLnVzZXIuZ2FtZS5yZW1v"
    "dmUodXNyKSAjbGVhdmUgdGhlIHByZXZpb3VzIHJvb20gY2xlYW5seSBmaXJzdAogICAgICAgIHNl"
    "bGYudXNlcmxpc3QuYXBwZW5kKHVzcikKICAgICAgICB1c3IudXNlci5nYW1lID0gc2VsZgogICAg"
    "ICAgIHJldCA9IF9lbShmJyRnYW1ldXNlciAie3NlbGYuZ25hbWV9IiAie3Vzci51c2VyLm5hbWV9"
    "IiAiIiAiMTAwIiAiMCInKQogICAgICAgICNVbmNvbmRpdGlvbmFsbHksIHRvIGV2ZXJ5b25lIGlu"
    "IHRoZSB0b3duLiBUaGlzIHVzZWQgdG8gYmUgc2VudCBvbmx5CiAgICAgICAgI3doZW4gbnBqICgi"
    "bmV3IHBsYXllcnMgbWF5IGpvaW4gYSBydW5uaW5nIGdhbWUiKSB3YXMgc2V0IC0gYnV0IG5wagog"
    "ICAgICAgICNzYXlzIG5vdGhpbmcgYWJvdXQgd2hvIHNob3VsZCBoZWFyIGFib3V0IGEgam9pbiwg"
    "aXQgb25seSBjb250cm9scwogICAgICAgICN3aGV0aGVyIGEgKnN0YXJ0ZWQqIGdhbWUgc3RheXMg"
    "bGlzdGVkLiBGb3IgYW4gb3JkaW5hcnkgcm9vbSwgd2hpY2ggaXMKICAgICAgICAjY3JlYXRlZCB3"
    "aXRoIG5waj0wIGFuZCBqb2luZWQgYmVmb3JlIGl0IHN0YXJ0cywgbm9ib2R5IHdhcyBldmVyIHRv"
    "bGQ6CiAgICAgICAgI3RoZSBob3N0J3MgbG9iYnkgbmV2ZXIgbGlzdGVkIHRoZSBhcnJpdmluZyBw"
    "bGF5ZXIsIHNvIHRoZSBob3N0IGhhZAogICAgICAgICNub2JvZHkgdG8gc3RhcnQgdGhlIGdhbWUg"
    "d2l0aCwgYW5kIHRoZSBqb2luZXIgc2F0IGluICJjb25uZWN0aW5nIgogICAgICAgICNmb3JldmVy"
    "IHdhaXRpbmcgZm9yIGEgc3RhcnQgdGhhdCBjb3VsZCBub3QgY29tZS4KICAgICAgICB1c3Iuc2Vy"
    "dmVyLmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLl9hdWRpZW5jZSgpLCdtZXNzYWdlJzpyZXR9KQog"
    "ICAgICAgIHJldHVybiBfZW0oZicvam9pbmdhbWUgIntzZWxmLmduYW1lfSIgIntzZWxmLnVybH0i"
    "ICJ7c2VsZi5zdGF0dXN9IicpCiAgICBkZWYgZGVzdHJveShzZWxmKToKICAgICAgICAjVGVhciB0"
    "aGUgcm9vbSBkb3duIGNvbXBsZXRlbHk6IGV2ZXJ5b25lIHN0aWxsIGxpc3RlZCBpbiBpdCBpcyBw"
    "dXQKICAgICAgICAjYmFjayB0byAibm90IGluIGEgZ2FtZSIsIGFuZCB0aGUgcm9vbSBzdG9wcyBi"
    "ZWluZyBhZHZlcnRpc2VkLgogICAgICAgIHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIGZv"
    "ciBjIGluIGxpc3Qoc2VsZi51c2VybGlzdCk6CiAgICAgICAgICAgIGlmIGMudXNlcjoKICAgICAg"
    "ICAgICAgICAgIGMudXNlci5nYW1lID0gTm9uZQogICAgICAgIHNlbGYudXNlcmxpc3QgPSBbXQog"
    "ICAgICAgIGlmIHNlbGYucGFyZW50LmdhbWVzLmdldChzZWxmLmduYW1lKSBpcyBzZWxmOgogICAg"
    "ICAgICAgICBkZWwgc2VsZi5wYXJlbnQuZ2FtZXNbc2VsZi5nbmFtZV0KICAgICAgICBzZWxmLnBh"
    "cmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLAogICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgJ21lc3NhZ2UnOl9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJyl9KQog"
    "ICAgZGVmIHJlbW92ZShzZWxmLCBjb249Tm9uZSk6I1RPRE8gcmVjcmVhdGUgcHJvcGVybHkKICAg"
    "ICAgICBpZiBjb24gaXMgTm9uZSBvciBjb24gbm90IGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAg"
    "ICAgIHJldHVybgogICAgICAgIHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIHNlbGYudXNl"
    "cmxpc3QucmVtb3ZlKGNvbikKICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1ldXNlciAie2Nv"
    "bi51c2VyLm5hbWV9IicpCiAgICAgICAgY29uLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBpZiBj"
    "b24gaXMgc2VsZi5ob3N0OgogICAgICAgICAgICAjVGhlIGhvc3QgKmlzKiB0aGUgZ2FtZSBzZXNz"
    "aW9uOiB0aGUgY28tb3Agd29ybGQgcnVucyBvbiB0aGVpcgogICAgICAgICAgICAjbWFjaGluZSBh"
    "bmQgdGhlIHJvb20ncyBEaXJlY3RQbGF5IHVybCBwb2ludHMgYXQgaXQuIE9uY2UgdGhleSBhcmUK"
    "ICAgICAgICAgICAgI2dvbmUgdGhlIHJvb20gY2Fubm90IGJlIGpvaW5lZCBieSBhbnlib2R5LCBi"
    "dXQgaXQgdXNlZCB0byBzdGF5CiAgICAgICAgICAgICNsaXN0ZWQgLSBzbyB0aGUgbmV4dCBwbGF5"
    "ZXIgdG8gY2xpY2sgaXQgZ290IGEgdXJsIHRvIGEgZ2FtZSB0aGF0CiAgICAgICAgICAgICNubyBs"
    "b25nZXIgZXhpc3RlZCBhbmQgc2F0IG9uICJjb25uZWN0aW5nIiB1bnRpbCB0aGV5IGdhdmUgdXAu"
    "CiAgICAgICAgICAgICNUaGlzIGlzIHdoYXQgYSBob3N0IGNyYXNoIGxlYXZlcyBiZWhpbmQuCiAg"
    "ICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBIb3N0IHtjb24udXNlci5uYW1lfSBsZWZ0IHJvb20g"
    "IntzZWxmLmduYW1lfSIsIGNsb3NpbmcgaXQnKQogICAgICAgICAgICBzZWxmLnBhcmVudC5zZXJ2"
    "ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzpsZWF2ZW1zZ30pCiAgICAgICAgICAg"
    "IHNlbGYuZGVzdHJveSgpCiAgICAgICAgICAgIHJldHVybgogICAgICAgICNpZiAwIHVzZXJzIGxl"
    "ZnQsIHJlbW92ZSBnYW1lCiAgICAgICAgaWYgbGVuKHNlbGYudXNlcmxpc3QpPT0wOgogICAgICAg"
    "ICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJykKICAgICAgICAgICAg"
    "ZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdCiAgICAgICAgc2VsZi5wYXJlbnQuc2Vy"
    "dmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgZGVmIHN0"
    "YXJ0R2FtZShzZWxmLCB1c2VyPU5vbmUpOgogICAgICAgIGlmIG5vdCAodXNlciBhbmQgc2VsZi5o"
    "b3N0ID09IHVzZXIpOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjdXNlciBub3QgaG9zdAogICAg"
    "ICAgIHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIHNlbGYuc3RhdHVzID0gMQogICAgICAg"
    "IGZvciBjIGluIHNlbGYudXNlcmxpc3Q6I1RPRE8gaGF2ZSB1c2VyIHJlbW92ZSBpdHNlbGYgd2hl"
    "biAvc3RhcnRpbmdnYW1lPwogICAgICAgICAgICB1biA9IGMudXNlci5uYW1lCiAgICAgICAgICAg"
    "ICNUT0RPIGNvbnNpZGVyIHJlbW92aW5nIHVzZXIgZnJvbSB0YXJnZXQgb3duIHNldD8KICAgICAg"
    "ICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6"
    "X2VtKGYnJmNoYXRjaGFubmVsdXNlciAie3VufSInKStfZW0oZicmZ2FtZWNoYW5uZWx1c2VyICJ7"
    "dW59IicpfSkKICAgICAgICAjLi4uYW5kIGFjdHVhbGx5IHRha2UgdGhlbSBvZmYgdGhlIHRvd24g"
    "cm9zdGVyLCB3aGljaCB0aGlzIG9ubHkgZXZlcgogICAgICAgICMqYW5ub3VuY2VkKi4gTGVhdmlu"
    "ZyB0aGVtIGxpc3RlZCBtZWFudCB0aGUgc2VydmVyIHN0aWxsIGNvdW50ZWQgdGhlbQogICAgICAg"
    "ICNhcyBzdGFuZGluZyBpbiB0aGUgdG93biBmb3IgdGhlIHdob2xlIHNlc3Npb246IHRvd24gcG9w"
    "dWxhdGlvbiB3YXMKICAgICAgICAjd3JvbmcsIGFuZCBldmVyeSBwb3NpdGlvbiB1cGRhdGUgZnJv"
    "bSBhbnlvbmUgc3RpbGwgd2Fsa2luZyBhcm91bmQgd2FzCiAgICAgICAgI2Zhbm5lZCBvdXQgdG8g"
    "cGxheWVycyB3aG8gd2VyZSBhd2F5IGluIGEgY28tb3Agd29ybGQgYW5kIGNvdWxkIGRvCiAgICAg"
    "ICAgI25vdGhpbmcgd2l0aCBpdC4gVGhlIGNsaWVudHMgd2VyZSB0b2xkIHRoZXkgbGVmdDsgbm93"
    "IHRoZSBzZXJ2ZXIKICAgICAgICAjYWdyZWVzIHdpdGggdGhlbS4KICAgICAgICBmb3IgYyBpbiBs"
    "aXN0KHNlbGYudXNlcmxpc3QpOgogICAgICAgICAgICBjLnVzZXIubGVhdmVDaGF0KCkKICAgICAg"
    "ICAgICAgaWYgYyBpbiBzZWxmLnBhcmVudC51c2VybGlzdDoKICAgICAgICAgICAgICAgIHNlbGYu"
    "cGFyZW50LnVzZXJsaXN0LnJlbW92ZShjKQogICAgICAgIGlmIG5vdCBzZWxmLm5wajoKICAgICAg"
    "ICAgICAgI2dhbWUgbm8gbG9uZ2VyIGpvaW5hYmxlL3Zpc2libGUgb25jZSBzdGFydGVkCiAgICAg"
    "ICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2Un"
    "Ol9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJyl9KQogICAgICAgICNub3RpZnkgcGxheWVycyBp"
    "biB0aGUgZ2FtZSB0aGF0IGl0IGhhcyBzdGFydGVkCiAgICAgICAgZm9yIGMgaW4gc2VsZi51c2Vy"
    "bGlzdDoKICAgICAgICAgICAgaXNIb3N0ID0gMSBpZiBjIGlzIHNlbGYuaG9zdCBlbHNlIDAKICAg"
    "ICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0JzooYywpLCdtZXNz"
    "YWdlJzpfZW0oZicvc3RhcnRnYW1lICIxIiAie2lzSG9zdH0iICIxIicpfSkKICAgICAgICByZXR1"
    "cm4gTm9uZQogICAgZGVmIF9nZXRVc2VybGlzdChzZWxmKToKICAgICAgICByZXR1cm4gJyAnLmpv"
    "aW4oIChmJyJ7Yy51c2VyLm5hbWV9IiAiIiAiMTAwIiAiMCInIGZvciBjIGluIHNlbGYudXNlcmxp"
    "c3QpICkKICAgIGRlZiBnZXRHYW1lU3RyaW5nKHNlbGYpOgogICAgICAgIGlmIHNlbGYuc3RhdHVz"
    "IGFuZCBub3Qgc2VsZi5ucGo6CiAgICAgICAgICAgIHJldHVybiBOb25lICNHYW1lIGRvZXMgbm90"
    "IHNob3cgaWYgbmV3IHBsYXllcnMgY2FuJ3Qgam9pbiB3aGVuIGFjdGl2ZQogICAgICAgIHBhc3cg"
    "PSAnJwogICAgICAgIGlmIHNlbGYucGFzc3dvcmQ6CiAgICAgICAgICAgIHBhc3cgPSAnWFhYJwog"
    "ICAgICAgIHJldHVybiBfZW0oZickZ2FtZSAie3NlbGYuZ25hbWV9IiAie3Bhc3d9IiAie3NlbGYu"
    "bWFwUGFyfSIgIntzZWxmLm1hcFRyYW5zbGF0ZX0iICJ7c2VsZi51bjF9IiAie3NlbGYuc3RhdHVz"
    "fSIgIntzZWxmLm1heHBsYXllcnN9IiB7c2VsZi5fZ2V0VXNlcmxpc3QoKX0nKQogICAgZGVmIGRl"
    "YnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgJ25hbWUnOnNlbGYu"
    "Z25hbWUsCiAgICAgICAgICAgICdob3N0JzpzZWxmLmhvc3QudXNlci5uYW1lLAogICAgICAgICAg"
    "ICAnc3RhdHVzJzpzZWxmLnN0YXR1cywKICAgICAgICAgICAgJ2hhc1Bhc3N3b3JkJzoxIGlmIHNl"
    "bGYucGFzc3dvcmQgZWxzZSAwLAogICAgICAgICAgICAndXNlcnMnOnR1cGxlKFtjLnVzZXIubmFt"
    "ZSBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0XSksCiAgICAgICAgICAgICd0b3duJzpzZWxmLnBhcmVu"
    "dC5uYW1lLAogICAgICAgICAgICAncGFyYW1ldGVycyc6c2VsZi5tYXBQYXIsCiAgICAgICAgICAg"
    "ICdtYXBOYW1lJzpzZWxmLm1hcFRyYW5zbGF0ZSwKICAgICAgICAgICAgJ2NhbkpvaW5SdW5uaW5n"
    "JzpzZWxmLm5wagogICAgICAgIH0KIyB0cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwKIyB0cmFu"
    "c2xhdGVOZXRDaXR5VHJhZGVDaGFubmVsCiMgdHJhbnNsYXRlTmV0Q2l0eUNoYXRDaGFubmVsCl9E"
    "RUZBVUxUX0NIQVRTID0gWyd0cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwnLCd0cmFuc2xhdGVO"
    "ZXRDaXR5VHJhZGVDaGFubmVsJ10KY2xhc3MgR2FtZUNoYW5uZWwoKToKICAgIG1heHVzZXIgPSA1"
    "MCAjVE9ETyBjb25maWd1cmVhYmxlCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyLCBjaG5O"
    "YW1lKToKICAgICAgICBzZWxmLnNlcnZlciA9IHNlcnZlcgogICAgICAgIHNlbGYubmFtZSA9IGNo"
    "bk5hbWUKICAgICAgICBzZWxmLnVzZXJsaXN0ID0gW10KICAgICAgICBzZWxmLmNoYXRDaGFubmVs"
    "cyA9IHt9CiAgICAgICAgc2VsZi5nYW1lcyA9IHt9ICNUT0RPIGZpZ3VyZSBvdXQgQSBhbmQgQiB2"
    "YWx1ZSBmb3IgZGlzcGxheQogICAgICAgICNUT0RPIHJlcXVlc3Qgam9pbiByZXNlcnZlcyBzcGFj"
    "ZSB3aXRoIHdlYWsgcmVmZXJlbmNlcwogICAgICAgICMtIHdlYWsgdmFsdWUgcmVmIHNob3VsZCBl"
    "bnN1cmUgdGhhdCBjb25uZWN0aW9uIGlzIHJlbW92ZWQgZnJvbSBxdWV1ZSBpZiBpdCBkaXNjb25u"
    "ZWN0cyBkdXJpbmcgdGhlIGpvaW4gcHJvY2VzcwogICAgICAgIHNlbGYucmVxdWVzdGVkID0gW10K"
    "ICAgICAgICBzZWxmLmdhbWVSZXF1ZXN0cyA9IHt9CiAgICAgICAgc2VsZi5kaXJ0eSA9IEZhbHNl"
    "CiAgICAgICAgZm9yIGNuIGluIF9ERUZBVUxUX0NIQVRTOgogICAgICAgICAgICBzZWxmLmNoYXRD"
    "aGFubmVsc1tjbl0gPSBbXSAjVXNlcmxpc3QKICAgIGRlZiByZXF1ZXN0Sm9pbihzZWxmLCBjb24p"
    "OgogICAgICAgICNsZWF2ZUNoYW5uZWwoKSBhbHJlYWR5IHJlbGVhc2VzIGFueSBvdXRzdGFuZGlu"
    "ZyByZXNlcnZhdGlvbiwgb24gdGhpcwogICAgICAgICNjaGFubmVsIG9yIGFub3RoZXIgb25lLiBU"
    "aGUgZm9sbG93LXVwIGJsb2NrIHRoYXQgdXNlZCB0byBzdGFuZCBoZXJlCiAgICAgICAgI2NvdWxk"
    "IHRoZXJlZm9yZSBuZXZlciBydW4gLSBhbmQgaWYgaXQgZXZlciBoYWQsIGl0cyB1bmd1YXJkZWQK"
    "ICAgICAgICAjbGlzdC5yZW1vdmUoKSB3b3VsZCBoYXZlIHJhaXNlZCBWYWx1ZUVycm9yIGZvciBh"
    "IHJlc2VydmF0aW9uIHRoYXQgd2FzCiAgICAgICAgI2FscmVhZHkgZ29uZS4KICAgICAgICBjb24u"
    "dXNlci5sZWF2ZUNoYW5uZWwoKQogICAgICAgIGVsZW4gPSBsZW4oc2VsZi51c2VybGlzdCkrbGVu"
    "KHNlbGYucmVxdWVzdGVkKQogICAgICAgIGlmIGVsZW48c2VsZi5tYXh1c2VyOgogICAgICAgICAg"
    "ICBzZWxmLnJlcXVlc3RlZC5hcHBlbmQoY29uKQogICAgICAgICAgICBjb24udXNlci5yZXF1ZXN0"
    "ZWRDaGFubmVsID0gc2VsZgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHJldHVybiBG"
    "YWxzZQogICAgZGVmIF9pc1N0YWxlR2FtZShzZWxmLCBnZW50LCBjb24pOgogICAgICAgICNBIHJv"
    "b20gd2hvc2UgaG9zdCBpcyBubyBsb25nZXIgdGhlIGxpdmUgc2Vzc2lvbiBmb3IgdGhhdCBhY2Nv"
    "dW50LiBUaGUKICAgICAgICAjY2xpZW50IG5hbWVzIGEgcm9vbSBhZnRlciBpdHMgaG9zdCwgc28g"
    "d2hlbiBhIHBsYXllciB3aG9zZSBnYW1lCiAgICAgICAgI2NyYXNoZWQgcmVjb25uZWN0cyBhbmQg"
    "aG9zdHMgYWdhaW4sIHRoZSByb29tIGZyb20gdGhlIHNlc3Npb24gdGhhdAogICAgICAgICNkaWVk"
    "IGlzIHN0aWxsIHNpdHRpbmcgaGVyZSB1bmRlciB0aGUgc2FtZSBuYW1lIC0gd2l0aCBhIGhvc3QK"
    "ICAgICAgICAjY29ubmVjdGlvbiB0aGF0IG5vIGxvbmdlciBleGlzdHMgYW5kIGEgRGlyZWN0UGxh"
    "eSB1cmwgcG9pbnRpbmcgYXQgYQogICAgICAgICNnYW1lIHRoYXQgaXMgZ29uZS4gQW55b25lIGpv"
    "aW5pbmcgaXQgd2FpdHMgZm9yZXZlci4KICAgICAgICBpZiBnZW50Lmhvc3QgaXMgY29uOgogICAg"
    "ICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIGhvc3RuYW1lID0gZ2VudC5ob3N0LnVzZXIubmFt"
    "ZSBpZiBnZW50Lmhvc3QudXNlciBlbHNlIE5vbmUKICAgICAgICBpZiBob3N0bmFtZSBpcyBOb25l"
    "OgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHJldHVybiBzZWxmLnNlcnZlci5nZXRQ"
    "bGF5ZXIoaG9zdG5hbWUpIGlzIG5vdCBnZW50Lmhvc3QKICAgIGRlZiByZXF1ZXN0Q3JlYXRlR2Ft"
    "ZShzZWxmLCBjb24sIGdhbWVOYW1lKToKICAgICAgICAjTmV2ZXIgcmV0dXJuIGEgYmFyZSBGYWxz"
    "ZSBmcm9tIGhlcmUuIHBhcnNlKCkgdHJlYXRzIGEgZmFsc3kgcmVzdWx0IGFzCiAgICAgICAgIyJu"
    "b3RoaW5nIHRvIHNlbmQiLCBzbyBldmVyeSByZWplY3Rpb24gYmVsb3cgdXNlZCB0byBsZWF2ZSB0"
    "aGUgY2xpZW50CiAgICAgICAgI3dhaXRpbmcgb24gYW4gYW5zd2VyIHRoYXQgbmV2ZXIgY2FtZSAt"
    "IHRoZSByb29tLWNyZWF0aW9uIGRpYWxvZyB0aGVuCiAgICAgICAgI3NwaW5zIGZvcmV2ZXIuCiAg"
    "ICAgICAgaWYgY29uLnVzZXIucmVxdWVzdGVkR2FtZSBvciBjb24udXNlci5nYW1lOgogICAgICAg"
    "ICAgICBjb24udXNlci5zdG9wR2FtZSgpCiAgICAgICAgdGNuID0gc2VsZi5nYW1lUmVxdWVzdHMu"
    "Z2V0KGdhbWVOYW1lKQogICAgICAgIGlmIHRjbiBpcyBub3QgTm9uZSBhbmQgdGNuIGlzIG5vdCBj"
    "b246CiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5hbWVUYWtlbiAie2dhbWVO"
    "YW1lfSInKQogICAgICAgICAgICAjZWxzZSB0Y24gaXMgY29uLCByZS1yZXF1ZXN0ZWQgY3JlYXRp"
    "b24KICAgICAgICBnZW50ID0gc2VsZi5nYW1lcy5nZXQoZ2FtZU5hbWUpCiAgICAgICAgaWYgZ2Vu"
    "dCBpcyBub3QgTm9uZToKICAgICAgICAgICAgaWYgc2VsZi5faXNTdGFsZUdhbWUoZ2VudCwgY29u"
    "KToKICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBSZXBsYWNpbmcgc3RhbGUgcm9vbSAi"
    "e2dhbWVOYW1lfSIgJwogICAgICAgICAgICAgICAgICAgICAgZicoaG9zdCBzZXNzaW9uIGdvbmUp"
    "IGF0IHRoZSByZXF1ZXN0IG9mIHtjb24udXNlci5uYW1lfScpCiAgICAgICAgICAgICAgICBnZW50"
    "LmRlc3Ryb3koKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcmV0dXJuIF9lbShm"
    "Jy9lcnJvciBnYW1lTmFtZVRha2VuICJ7Z2FtZU5hbWV9IicpCiAgICAgICAgc2VsZi5nYW1lUmVx"
    "dWVzdHNbZ2FtZU5hbWVdID0gY29uCiAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkR2FtZSA9IGdh"
    "bWVOYW1lCiAgICAgICAgcmV0dXJuIF9lbShmJy9jcmVhdGVnYW1lICJ7Z2FtZU5hbWV9IicpCiAg"
    "ICBkZWYgY3JlYXRlR2FtZShzZWxmLCBnYW1lTmFtZSwgaG9zdCwgcGFzdywgbWFwcCwgbWFwdCwg"
    "bnBqLCB1bjEsIHVuMiwgdW4zLCB1cmwpOgogICAgICAgIHJlcUhvc3QgPSBzZWxmLmdhbWVSZXF1"
    "ZXN0cy5nZXQoZ2FtZU5hbWUpCiAgICAgICAgaWYgcmVxSG9zdCBpcyBOb25lIG9yIHJlcUhvc3Qg"
    "aXMgbm90IGhvc3Q6CiAgICAgICAgICAgICNTYW1lIHJlYXNvbmluZyBhcyBhYm92ZTogYW5zd2Vy"
    "LCBuZXZlciBmYWxsIHNpbGVudC4KICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1l"
    "TmFtZVRha2VuICJ7Z2FtZU5hbWV9IicpCiAgICAgICAgZ2VudCA9IEdhbWVFbnRyeShzZWxmLCBn"
    "YW1lTmFtZSwgaG9zdCwgcGFzdywgbWFwcCwgbWFwdCwgbnBqLCB1bjEsIHVuMiwgdW4zLCB1cmwp"
    "CiAgICAgICAgcmVxSG9zdC51c2VyLnJlcXVlc3RlZEdhbWUgPSBOb25lICNUT0RPIHJlb2dhbml6"
    "ZSBiZXR0ZXIKICAgICAgICBkZWwgc2VsZi5nYW1lUmVxdWVzdHNbZ2FtZU5hbWVdCiAgICAgICAg"
    "cmV0dXJuIE5vbmUKICAgIGRlZiBsZWF2ZUNoYW5uZWwoc2VsZiwgY29uKToKICAgICAgICAjVGhl"
    "IGNsZWFudXAgcnVucyB3aGV0aGVyIG9yIG5vdCB0aGUgcGxheWVyIGlzIHN0aWxsIG9uIHRoZSB0"
    "b3duCiAgICAgICAgI3Jvc3Rlci4gU2luY2Ugc3RhcnRHYW1lKCkgdGFrZXMgaXRzIHBsYXllcnMg"
    "b2ZmIHRoYXQgcm9zdGVyLCBhCiAgICAgICAgI3BsYXllciB3aG8gbGVhdmVzIChvciBkaXNjb25u"
    "ZWN0cykgZnJvbSBpbnNpZGUgYSBydW5uaW5nIGdhbWUgdXNlZCB0bwogICAgICAgICNza2lwIGFs"
    "bCBvZiB0aGlzOiB0aGVpciByb29tIHdhcyBuZXZlciBsZWZ0LCB0aGVpciBjaGF0IGNoYW5uZWwg"
    "a2VwdAogICAgICAgICN0aGVpciBlbnRyeSwgYW5kIGdhbWVjaGFubmVsIHN0YXllZCBwb2ludGlu"
    "ZyBhdCBhIHRvd24gdGhleSB3ZXJlIG5vCiAgICAgICAgI2xvbmdlciBpbi4gT25seSB0aGUgcm9z"
    "dGVyIHJlbW92YWwgYW5kIHRoZSBhbm5vdW5jZW1lbnQgYXJlCiAgICAgICAgI2NvbmRpdGlvbmFs"
    "IG5vdyAtIGJlY2F1c2Ugb25seSB0aG9zZSBkZXBlbmQgb24gYmVpbmcgbGlzdGVkLgogICAgICAg"
    "IGxpc3RlZCA9IGNvbiBpbiBzZWxmLnVzZXJsaXN0CiAgICAgICAgY29uLnVzZXIuc3RvcEdhbWUo"
    "KQogICAgICAgIGNvbi51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAgaWYgbGlzdGVkOgogICAgICAg"
    "ICAgICBzZWxmLnVzZXJsaXN0LnJlbW92ZShjb24pCiAgICAgICAgICAgIGxlYXZlbXNnID0gX2Vt"
    "KGYnJmdhbWVjaGFubmVsdXNlciAie2Nvbi51c2VyLm5hbWV9IicpCiAgICAgICAgICAgIGNvbi5z"
    "ZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnNlbGYudXNlcmxpc3QsJ21lc3NhZ2UnOmxlYXZlbXNn"
    "fSkKICAgICAgICBjb24udXNlci5nYW1lY2hhbm5lbD1Ob25lCiAgICBkZWYgbGVhdmVDaGF0KHNl"
    "bGYsIGNvbik6ICNUT0RPIGJldHRlciBjaGF0Y2hhbm5lbCBvYmplY3QgYW5kIG1vdmUgaXQgdGhl"
    "cmUuCiAgICAgICAgY29uLnVzZXIubGVhdmVDaGF0KCkKICAgICNUT0RPIGNoYW5nZSB0aGVzZSBm"
    "dW5jdGlvbnMgdG8gYWxzbyBoYW5kbGUgbWVzc2FnZSBmb3JtaW5nCiAgICBkZWYgam9pbkNoYW5u"
    "ZWwoc2VsZiwgY29uLCBuYW0pOiNtb3ZlcyB1c2VyIGZyb20gcXVldWUgdG8gdXNlcmxpc3QKICAg"
    "ICAgICBpZiBjb24gaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAgI0R1cGxpY2F0ZSAvam9p"
    "bmdhbWVjaGFubmVsIGZvciBhIHRvd24gd2UgYXJlIGFscmVhZHkgaW4uIFJlYnVpbGQKICAgICAg"
    "ICAgICAgI3RoZSByZXNlcnZhdGlvbiBzbyB0aGUgcmVxdWVzdCBiZWxvdyByZS1ydW5zIHRoZSBm"
    "dWxsIGVudW1lcmF0aW9uCiAgICAgICAgICAgICNhbmQgdGhlIGNsaWVudCBnZXRzIGEgY29tcGxl"
    "dGUgYW5zd2VyIHJhdGhlciB0aGFuIHNpbGVuY2UuCiAgICAgICAgICAgIHNlbGYudXNlcmxpc3Qu"
    "cmVtb3ZlKGNvbikKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAg"
    "ICAgICAgY29uLnVzZXIucmVxdWVzdGVkQ2hhbm5lbCA9IHNlbGYKICAgICAgICBpZiBjb24gbm90"
    "IGluIHNlbGYucmVxdWVzdGVkIGFuZCBjb24gbm90IGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAg"
    "ICAgICNObyBvdXRzdGFuZGluZyByZXNlcnZhdGlvbi4gVGhlIHJlc2VydmF0aW9uIGlzIGRyb3Bw"
    "ZWQgYnkgYW55CiAgICAgICAgICAgICNpbnRlcnZlbmluZyBsZWF2ZUNoYW5uZWwoKS9yZXF1ZXN0"
    "Sm9pbigpIGFuZCBieSBhIHJlY29ubmVjdCwgc28gYQogICAgICAgICAgICAjY2xpZW50IHRoYXQg"
    "Z29lcyBzdHJhaWdodCB0byAvam9pbmdhbWVjaGFubmVsIC0gb3Igd2hvc2UgZWFybGllcgogICAg"
    "ICAgICAgICAjL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgcmFjZWQgaXRzIG93biBjbGVhbnVwIC0g"
    "dXNlZCB0byBnZXQgbm8KICAgICAgICAgICAgI2Fuc3dlciBhdCBhbGwgYW5kIGhhbmcgb24gdGhl"
    "IGxvYWRpbmcgc2NyZWVuLiBBZG1pdCB0aGVtIGlmIHRoZQogICAgICAgICAgICAjdG93biBoYXMg"
    "cm9vbTsgb25seSBhIGdlbnVpbmVseSBmdWxsIHRvd24gaXMgcmVmdXNlZCBub3cuCiAgICAgICAg"
    "ICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KStsZW4oc2VsZi5yZXF1ZXN0ZWQpIDwgc2VsZi5tYXh1"
    "c2VyOgogICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAgICAg"
    "ICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgICAgIGVsc2U6"
    "CiAgICAgICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVDaGFubmVsRnVsbCAie25h"
    "bX0iJykKICAgICAgICBpZiBjb24gaW4gc2VsZi5yZXF1ZXN0ZWQ6CiAgICAgICAgICAgICNUT0RP"
    "IHZlcmlmeSBvcmRlciBvZiBvcGVyYXRpb25zIGFuZCBwb3NzaWJsZSB0aW1pbmcgaXNzdWVzCiAg"
    "ICAgICAgICAgIHNlbGYudXNlcmxpc3QuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIu"
    "Z2FtZWNoYW5uZWwgPSBzZWxmCiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLnJlbW92ZShjb24p"
    "CiAgICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lICNUT0RPIG9yZ2Fu"
    "aXplIGJldHRlcj8KICAgICAgICAgICAgdWwgPSBsZW4oc2VsZi51c2VybGlzdCkKICAgICAgICAg"
    "ICAgcmV0bXNnID0gX2VtKGYnL2pvaW5nYW1lY2hhbm5lbCAie25hbX0iICJ7dWx9IicpCiAgICAg"
    "ICAgICAgICNlbnVtZXJhdGUgaGVyb2RhdGEgb2YgZXhpc3RpbmcgdXNlcnMKICAgICAgICAgICAg"
    "Y2h1bmtzID0gW10KICAgICAgICAgICAgZm9yIHVzZXIgaW4gc2VsZi51c2VybGlzdDoKICAgICAg"
    "ICAgICAgICAgIGlmIHVzZXIgPT0gY29uOgogICAgICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAg"
    "ICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKHVzZXIudXNlci5nZXRHQ1Vtc2coKSkKICAgICAg"
    "ICAgICAgcmV0bXNnKz0gYicnLmpvaW4oY2h1bmtzKQogICAgICAgICAgICByZXRtc2crPSBzZWxm"
    "LmpvaW5DaGF0KGNvbiwgX0RFRkFVTFRfQ0hBVFNbMF0pCiAgICAgICAgICAgIHJldG1zZys9IHNl"
    "bGYuZW51bUNoYXRzKCkKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5lbnVtR2FtZXMoKQogICAg"
    "ICAgICAgICAjYnJvYWRjYXN0IGhlcm9kYXRhIHRvIG90aGVyIGV4aXN0aW5nIHVzZXJzCiAgICAg"
    "ICAgICAgIGNvbi5zZXJ2ZXIuZGlzdC5hZGQoewogICAgICAgICAgICAgICAgJ3RhcmdldCc6X3dv"
    "VXNlcihzZWxmLnVzZXJsaXN0LCBjb24pLAogICAgICAgICAgICAgICAgJ21lc3NhZ2UnOmNvbi51"
    "c2VyLmdldEdDVW1zZygpfSkKICAgICAgICAgICAgcmV0dXJuIHJldG1zZwogICAgICAgIHJldHVy"
    "biBOb25lCiAgICBkZWYgam9pbkNoYXQoc2VsZiwgY29uLCBuYW0sIHBhcz0nJyk6CiAgICAgICAg"
    "I1RPRE8gcGFzc3dvcmQgc3VwcG9ydD8KICAgICAgICAjLSByZXF1aXJlcyByZXN0cnVjdHVyZSBm"
    "cm9tIGxpc3QgdG8gY2hhbm5lbCBvYmplY3RzCiAgICAgICAgaWYgbm90IG5hbSBpbiBzZWxmLmNo"
    "YXRDaGFubmVsczoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAgIGNvbi51c2VyLmxlYXZl"
    "Q2hhdCgpCiAgICAgICAgI1RPRE8gY2hlY2sgaWYgY2xpZW50IGF1dG8tcHVyZ2VzIGNoYXRsaXN0"
    "CiAgICAgICAgI1RPRE8gQ0hFQ0sgLVYtIGJyb2FkY2FzdCByZWxldmFudCBjaGFuZ2VzPwogICAg"
    "ICAgIGNvbi5zZXJ2ZXIuZGlzdC5hZGQoewogICAgICAgICAgICAndGFyZ2V0JzpsaXN0KHNlbGYu"
    "Y2hhdENoYW5uZWxzW25hbV0pLAogICAgICAgICAgICAnbWVzc2FnZSc6X2VtKGYnJGNoYXRjaGFu"
    "bmVsdXNlciAie2Nvbi51c2VyLm5hbWV9IicpfSkKICAgICAgICBzZWxmLmNoYXRDaGFubmVsc1tu"
    "YW1dLmFwcGVuZChjb24pCiAgICAgICAgY29uLnVzZXIuY2hhdGNoYW5uZWwgPSBzZWxmLmNoYXRD"
    "aGFubmVsc1tuYW1dCiAgICAgICAgdWwgPSAxI2xlbihjb24udXNlci5jaGF0Y2hhbm5lbCkKICAg"
    "ICAgICByZXRtc2cgPSBfZW0oZicvam9pbmNoYXRjaGFubmVsICJ7bmFtfSIgIiIgInt1bH0iJykK"
    "ICAgICAgICAjZW51bWVyYXRlIG90aGVyIGNoYXQgdXNlcnM/CiAgICAgICAgY2h1bmtzID0gW10K"
    "ICAgICAgICBmb3IgdWNvbiBpbiBjb24udXNlci5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgaWYg"
    "dWNvbiAhPSBjb246CiAgICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKF9lbShmJyRjaGF0Y2hh"
    "bm5lbHVzZXIgInt1Y29uLnVzZXIubmFtZX0iJykpCiAgICAgICAgcmV0bXNnKz1iJycuam9pbihj"
    "aHVua3MpCiAgICAgICAgcmV0dXJuIHJldG1zZwogICAgZGVmIGVudW1DaGF0cyhzZWxmKToKICAg"
    "ICAgICBjaHVua3MgPSBbXQogICAgICAgIGZvciBjaGF0TmFtZSBpbiBzZWxmLmNoYXRDaGFubmVs"
    "czoKICAgICAgICAgICAgdWxsID0gbGVuKHNlbGYuY2hhdENoYW5uZWxzW2NoYXROYW1lXSkjVE9E"
    "TyBpbXByb3ZlCiAgICAgICAgICAgIGNodW5rcy5hcHBlbmQod2lyZV9lbmNvZGUoZickY2hhdGNo"
    "YW5uZWwgIntjaGF0TmFtZX0iICIiICJ7dWxsfSInKSkKICAgICAgICByZXR1cm4gX04uam9pbihj"
    "aHVua3MpK19OCiAgICBkZWYgZW51bUdhbWVzKHNlbGYpOgogICAgICAgIGNodW5rcyA9IFtdCiAg"
    "ICAgICAgZm9yIGduYW1lIGluIHNlbGYuZ2FtZXM6CiAgICAgICAgICAgIGdhbWVzdHIgPSBzZWxm"
    "LmdhbWVzW2duYW1lXS5nZXRHYW1lU3RyaW5nKCkKICAgICAgICAgICAgaWYgZ2FtZXN0cjoKICAg"
    "ICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQoZ2FtZXN0cikKICAgICAgICByZXR1cm4gYicnLmpv"
    "aW4oY2h1bmtzKQogICAgZGVmIHVwZGF0ZVBvcyhzZWxmLCBtZCk6CiAgICAgICAgaWYgbm90IHNl"
    "bGYuZGlydHk6CiAgICAgICAgICAgIHJldHVybgogICAgICAgICNDbGVhcmVkIEJFRk9SRSB0aGUg"
    "c2Nhbiwgbm90IGFmdGVyLiBBIC91cGRoZXJvcG9zIHRoYXQgYXJyaXZlZCB3aGlsZQogICAgICAg"
    "ICN0aGUgbG9vcCBiZWxvdyB3YXMgcnVubmluZyB1c2VkIHRvIHNldCBkaXJ0eT1UcnVlIGFuZCB0"
    "aGVuIGhhdmUgaXQKICAgICAgICAjaW1tZWRpYXRlbHkgY2xlYXJlZCBhZ2Fpbiwgc28gdGhhdCBw"
    "bGF5ZXIncyBtb3ZlIHdhcyBub3QgYnJvYWRjYXN0CiAgICAgICAgI3VudGlsIHNvbWVib2R5IGVs"
    "c2UgaGFwcGVuZWQgdG8gbW92ZS4gQ2xlYXJpbmcgZmlyc3QgbWVhbnMgdGhlIHdvcnN0CiAgICAg"
    "ICAgI2Nhc2UgaXMgb25lIHJlZHVuZGFudCBwYXNzLCBub3QgYSBzaWxlbnRseSBkcm9wcGVkIHBv"
    "c2l0aW9uLgogICAgICAgIHNlbGYuZGlydHkgPSBGYWxzZQogICAgICAgICNTbmFwc2hvdDogcGxh"
    "eWVycyBqb2luIGFuZCBsZWF2ZSB0aGUgdG93biB3aGlsZSB0aGlzIGl0ZXJhdGVzLgogICAgICAg"
    "IHRnID0gbGlzdChzZWxmLnVzZXJsaXN0KQogICAgICAgIG1vdmVycyA9IFtdCiAgICAgICAgZm9y"
    "IHVjb24gaW4gdGc6CiAgICAgICAgICAgIGlmIG5vdCB1Y29uLnVzZXIucG9zY2hhbmdlZDoKICAg"
    "ICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHVjb24udXNlci5wb3NjaGFuZ2VkID0g"
    "RmFsc2UKICAgICAgICAgICAgaWYgbm90IHVjb24udXNlci5oZXJvZGF0YToKICAgICAgICAgICAg"
    "ICAgICNBIHBsYXllciBpcyBvbmx5IGFubm91bmNlZCB0byB0aGUgb3RoZXJzIGJ5ICRnYW1lY2hh"
    "bm5lbHVzZXIsCiAgICAgICAgICAgICAgICAjYW5kIGdldEdDVW1zZygpIGVtaXRzIG5vdGhpbmcg"
    "YXQgYWxsIHVudGlsIHRoZWlyIGhlcm9kYXRhIGhhcwogICAgICAgICAgICAgICAgI2Fycml2ZWQu"
    "IEJyb2FkY2FzdGluZyBhIHBvc2l0aW9uIGZvciBhIGhlcm8gaWQgbm9ib2R5IGhhcwogICAgICAg"
    "ICAgICAgICAgI2JlZW4gdG9sZCBhYm91dCBoYW5kcyBldmVyeSBjbGllbnQgYW4gdXBkYXRlIGZv"
    "ciBhIHBsYXllciBpdAogICAgICAgICAgICAgICAgI2RvZXMgbm90IGtub3cgZXhpc3RzLiBXYWl0"
    "IHVudGlsIHRoZXkgYXJlIGEgcmVhbCwgYW5ub3VuY2VkCiAgICAgICAgICAgICAgICAjcGxheWVy"
    "LgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgbW92ZXJzLmFwcGVuZCgodWNv"
    "biwgZid7dWNvbi51c2VyLndpcmVJZCgpfSN7dWNvbi51c2VyLnBvc2RhdGF9JykpCiAgICAgICAg"
    "aWYgbm90IG1vdmVyczoKICAgICAgICAgICAgI0V2ZXJ5b25lIHdobyB3YXMgZGlydHkgaGFzIHNp"
    "bmNlIGxlZnQgdGhlIHRvd24uIFNlbmRpbmcgdGhlCiAgICAgICAgICAgICNhcmd1bWVudC1sZXNz"
    "ICcvdXBkaGVyb3BvcyAnIHRoYXQgdGhpcyB1c2VkIHRvIHByb2R1Y2UganVzdCBoYW5kcwogICAg"
    "ICAgICAgICAjdGhlIGNsaWVudCBhbiBlbXB0eSBjb21tYW5kIHRvIHBhcnNlLgogICAgICAgICAg"
    "ICByZXR1cm4KICAgICAgICAjTm9ib2R5IGlzIHRvbGQgdGhlaXIgb3duIHBvc2l0aW9uLiBUaGUg"
    "Y2xpZW50IGlzIHRoZSBhdXRob3JpdHkgb24KICAgICAgICAjd2hlcmUgaXRzIG93biBoZXJvIGlz"
    "IC0gaXQgaXMgd2hhdCBzZW50IHRoZSBjb29yZGluYXRlcyBpbiB0aGUgZmlyc3QKICAgICAgICAj"
    "cGxhY2UgLSBzbyBlY2hvaW5nIHRoZW0gYmFjayBhIGZyYWN0aW9uIG9mIGEgc2Vjb25kIGxhdGVy"
    "IGlzIGF0IGJlc3QKICAgICAgICAjcmVkdW5kYW50IGFuZCBhdCB3b3JzdCBhIGhpdGNoLCBhcyB0"
    "aGUgaGVybyBpcyBudWRnZWQgYmFjayB0byB3aGVyZQogICAgICAgICNpdCBzdG9vZCB3aGVuIHRo"
    "ZSBwYWNrZXQgbGVmdC4gRXZlcnkgb3RoZXIgYnJvYWRjYXN0IGluIHRoaXMgZmlsZQogICAgICAg"
    "ICNhbHJlYWR5IGV4Y2x1ZGVzIHRoZSBvcmlnaW5hdG9yIChzZWUgX3dvVXNlcik7IHBvc2l0aW9u"
    "cyB3ZXJlIHRoZQogICAgICAgICNleGNlcHRpb24uIENvc3RzIG9uZSBtZXNzYWdlIGJ1aWx0IHBl"
    "ciBtb3ZpbmcgcGxheWVyLCBhbmQgbm90IG9uZQogICAgICAgICNleHRyYSBieXRlIG9uIHRoZSB3"
    "aXJlOiB0aGUgZGlzdHJpYnV0b3IgYWxyZWFkeSB3cml0ZXMgdG8gZWFjaAogICAgICAgICNyZWNp"
    "cGllbnQgc2VwYXJhdGVseS4KICAgICAgICBtb3ZlZCA9IHNldCh1IGZvciAodSwgXykgaW4gbW92"
    "ZXJzKQogICAgICAgIHdhdGNoZXJzID0gW2MgZm9yIGMgaW4gdGcgaWYgYyBub3QgaW4gbW92ZWRd"
    "CiAgICAgICAgaWYgd2F0Y2hlcnM6CiAgICAgICAgICAgIGZvciBtc2cgaW4gc2VsZi5fcG9zTWVz"
    "c2FnZXMoW2NoIGZvciAoXywgY2gpIGluIG1vdmVyc10pOgogICAgICAgICAgICAgICAgbWQuYWRk"
    "KHsndGFyZ2V0Jzp3YXRjaGVycywnbWVzc2FnZSc6bXNnfSkKICAgICAgICBmb3IgKHVjb24sIF8p"
    "IGluIG1vdmVyczoKICAgICAgICAgICAgb3RoZXJzID0gW2NoIGZvciAodSwgY2gpIGluIG1vdmVy"
    "cyBpZiB1IGlzIG5vdCB1Y29uXQogICAgICAgICAgICBpZiBub3Qgb3RoZXJzOgogICAgICAgICAg"
    "ICAgICAgY29udGludWUgI29ubHkgbW92ZXIgaW4gdGhlIHRvd24sIG5vdGhpbmcgdG8gdGVsbCB0"
    "aGVtCiAgICAgICAgICAgIGZvciBtc2cgaW4gc2VsZi5fcG9zTWVzc2FnZXMob3RoZXJzKToKICAg"
    "ICAgICAgICAgICAgIG1kLmFkZCh7J3RhcmdldCc6KHVjb24sICksJ21lc3NhZ2UnOm1zZ30pCiAg"
    "ICBkZWYgX3Bvc01lc3NhZ2VzKHNlbGYsIGNodW5rcyk6CiAgICAgICAgI1NwbGl0IGludG8gc2V2"
    "ZXJhbCBjb21tYW5kcyByYXRoZXIgdGhhbiBvbmUgYXJiaXRyYXJpbHkgbG9uZyBsaW5lLgogICAg"
    "ICAgICMvdXBkaGVyb3BvcyBpcyB0aGUgb25seSBtZXNzYWdlIHdob3NlIGxlbmd0aCBncm93cyB3"
    "aXRoIHRoZSBudW1iZXIgb2YKICAgICAgICAjcGxheWVycyAtIGEgYnVzeSB0b3duIHdvdWxkIHB1"
    "dCBmaWZ0eSAiaWQjeCN5IiBncm91cHMgb24gYSBzaW5nbGUKICAgICAgICAjbGluZS4gVGhlIHJl"
    "dGFpbCBjbGllbnQgaXMgYSAyMDA4IDMyLWJpdCBiaW5hcnkgYW5kIGl0cyBsb2JieSBwYXJzZXIK"
    "ICAgICAgICAjY2FuIGJlIGFzc3VtZWQgdG8gdXNlIGZpeGVkLXNpemUgYnVmZmVyczsgaGFuZGlu"
    "ZyBpdCBhIGxpbmUgbG9uZ2VyCiAgICAgICAgI3RoYW4gaXQgZXhwZWN0cyBpcyB0aGUgY2xhc3Np"
    "YyB3YXkgdG8gY29ycnVwdCBpdHMgaGVhcCBhbmQgdGFrZSBpdAogICAgICAgICNkb3duIHdpdGgg"
    "YW4gYWNjZXNzIHZpb2xhdGlvbiBzb21ld2hlcmUgZWxzZSBlbnRpcmVseS4gU2V2ZXJhbCBzaG9y"
    "dAogICAgICAgICNjb21tYW5kcyBhcmUgZXF1aXZhbGVudCBmb3IgdGhlIGNsaWVudCBhbmQgY29z"
    "dCBvbmUgZXh0cmEgaGVhZGVyCiAgICAgICAgI2VhY2guCiAgICAgICAgYmF0Y2hlcyA9IFtdCiAg"
    "ICAgICAgY3VyID0gW10KICAgICAgICBjdXJsZW4gPSAwCiAgICAgICAgZm9yIGNoIGluIGNodW5r"
    "czoKICAgICAgICAgICAgaWYgY3VyIGFuZCBjdXJsZW4gKyBsZW4oY2gpICsgMSA+IF9NQVhfV0lS"
    "RV9MSU5FOgogICAgICAgICAgICAgICAgYmF0Y2hlcy5hcHBlbmQoY3VyKQogICAgICAgICAgICAg"
    "ICAgY3VyID0gW10KICAgICAgICAgICAgICAgIGN1cmxlbiA9IDAKICAgICAgICAgICAgY3VyLmFw"
    "cGVuZChjaCkKICAgICAgICAgICAgY3VybGVuICs9IGxlbihjaCkgKyAxCiAgICAgICAgaWYgY3Vy"
    "OgogICAgICAgICAgICBiYXRjaGVzLmFwcGVuZChjdXIpCiAgICAgICAgcmV0dXJuIFtfZW0oJy91"
    "cGRoZXJvcG9zICcgKyAnICcuam9pbihiKSkgZm9yIGIgaW4gYmF0Y2hlc10KICAgIGRlZiBkZWJ1"
    "Z19hcnJfZ2FtZXMoc2VsZik6CiAgICAgICAgYWN0RGljdCA9IFtdCiAgICAgICAgZm9yIGduLCBn"
    "IGluIGxpc3Qoc2VsZi5nYW1lcy5pdGVtcygpKToKICAgICAgICAgICAgYWN0RGljdC5hcHBlbmQo"
    "Zy5kZWJ1Z19kaWN0KCkpCiAgICAgICAgcmV0dXJuIGFjdERpY3QKICAgIGRlZiBkZWJ1Z19kaWN0"
    "KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICd1c2Vycyc6dHVwbGUoW2MudXNl"
    "ci5uYW1lIGZvciBjIGluIHNlbGYudXNlcmxpc3RdKSwKICAgICAgICAgICAgJ21heFVzZXJzJzpz"
    "ZWxmLm1heHVzZXIsCiAgICAgICAgICAgICdnYW1lcyc6dHVwbGUoW2duIGZvciBnbiBpbiBzZWxm"
    "LmdhbWVzXSkKICAgICAgICB9CgpfTUFQTkFNRVMgPSBbJ05ldF9UXzAxJywnTmV0X1RfMDInLCdO"
    "ZXRfVF8wMycsJ05ldF9UXzA0J10gI1RPRE8gdXNlIENGRyBvYmplY3QKY2xhc3MgR2FtZVN0YXRl"
    "KCk6CiAgICAjVE9ETyBhdXRvIGdyb3dhYmxlIGNoYW5uZWxzLCBbbWFwbmFtZV0KICAgICNUT0RP"
    "IGF2YWlsYWJsZSBpbmRleGVzLCBbbWFwbmFtZV0KICAgIGRlZiBfX2luaXRfXyhzZWxmLCBzZXJ2"
    "ZXIpOgogICAgICAgICNpbnN0YW5jZSBhdHRyaWJ1dGVzLCBub3QgY2xhc3MgYXR0cmlidXRlczog"
    "dGhlc2UgbXVzdCBOT1QgYmUgc2hhcmVkCiAgICAgICAgI2JldHdlZW4gc2VwYXJhdGUgQ29yZVNl"
    "cnZlciBpbnN0YW5jZXMgKGUuZy4gc3RvcC9zdGFydCBmcm9tIGEgR1VJCiAgICAgICAgI3dpdGhp"
    "biB0aGUgc2FtZSBwcm9jZXNzKSBvciBsZWZ0b3ZlciBwbGF5ZXJzL2NoYW5uZWxzIGZyb20gYQog"
    "ICAgICAgICNwcmV2aW91cyBydW4gd291bGQgbGVhayBpbnRvIHRoZSBuZXcgb25lLgogICAgICAg"
    "IHNlbGYuYWN0aXZlVXNlcnMgPSB7fSAjVE9ETyB0cmFjayB1c2VyIGhpc3Rvcnk/IG9wdGlvbmFs"
    "bHkKICAgICAgICBzZWxmLmdhbWVDaGFubmVscyA9IHt9ICNjaGFubmVsW10sIGtleWVkIGJ5IG1h"
    "cG5hbWUKICAgICAgICBzZWxmLnNlcnZlcj1zZXJ2ZXIKICAgICAgICBzZWxmLnVzZXJMb2NrID0g"
    "dGhyZWFkaW5nLkxvY2soKQogICAgICAgIGZvciBuYW1lIGluIF9NQVBOQU1FUzoKICAgICAgICAg"
    "ICAgZm9yIGkgaW4gcmFuZ2UoMSk6ICNUT0RPIGNvbmZpZ3VyZWFibGUgdXAgdG8gMjA/CiAgICAg"
    "ICAgICAgICAgICBjaG5OYW1lID0gX2djaG5sKG5hbWUsIDEraSkKICAgICAgICAgICAgICAgIHNl"
    "bGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdID0gR2FtZUNoYW5uZWwoc2VsZi5zZXJ2ZXIsIGNobk5h"
    "bWUpICNUT0RPIDEgYW5kIGdyb3c/CiAgICBkZWYgY2xhaW1Vc2VyKHNlbGYsIG5hbWUsIGNvbik6"
    "CiAgICAgICAgI1B1Ymxpc2ggY29uIGFzIFRIRSBsaXZlIHNlc3Npb24gZm9yIG5hbWUsIGF0b21p"
    "Y2FsbHkuIFRoZSBvbGQgY29kZQogICAgICAgICNjaGVja2VkIGdldFBsYXllcigpIGR1cmluZyBs"
    "b2dpbiBhbmQgdGhlbiBpbnNlcnRlZCBpbnRvIGFjdGl2ZVVzZXJzCiAgICAgICAgI211Y2ggbGF0"
    "ZXIsIGluIF9sb2JieUhhbmRsZTsgdHdvIGNvbm5lY3Rpb25zIGxvZ2dpbmcgaW4gYXMgdGhlIHNh"
    "bWUKICAgICAgICAjYWNjb3VudCBhdCBvbmNlIGJvdGggcGFzc2VkIHRoZSBjaGVjaywgYW5kIHRo"
    "ZSBzZWNvbmQgb25lJ3MgaW5zZXJ0CiAgICAgICAgI292ZXJ3cm90ZSB0aGUgZmlyc3QuIFRoZSBs"
    "b3NlciB0aGVuIGRlbGV0ZWQgdGhlIHdpbm5lcidzIGVudHJ5IHdoZW4gaXQKICAgICAgICAjZGlz"
    "Y29ubmVjdGVkLCBsZWF2aW5nIGEgY29ubmVjdGVkIHBsYXllciBpbnZpc2libGUgdG8gdGhlIHNl"
    "cnZlciAobm8KICAgICAgICAja2ljaywgbm8gd2hvaXMsIG5vIG1lc3NhZ2VzKS4KICAgICAgICB3"
    "aXRoIHNlbGYudXNlckxvY2s6CiAgICAgICAgICAgIGlmIG5hbWUgaW4gc2VsZi5hY3RpdmVVc2Vy"
    "czoKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgICAgICBzZWxmLmFjdGl2ZVVz"
    "ZXJzW25hbWVdID0gY29uCiAgICAgICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYgcmVsZWFzZVVz"
    "ZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAjb25seSBjbGVhciB0aGUgc2xvdCBpZiB3ZSBz"
    "dGlsbCBvd24gaXQsIG5ldmVyIHNvbWVvbmUgZWxzZSdzIHNlc3Npb24KICAgICAgICB3aXRoIHNl"
    "bGYudXNlckxvY2s6CiAgICAgICAgICAgIGlmIHNlbGYuYWN0aXZlVXNlcnMuZ2V0KG5hbWUpIGlz"
    "IGNvbjoKICAgICAgICAgICAgICAgIGRlbCBzZWxmLmFjdGl2ZVVzZXJzW25hbWVdCiAgICBkZWYg"
    "ZW51bWVyYXRlR0Moc2VsZik6CiAgICAgICAgY2hucyA9IFtdCiAgICAgICAgZm9yIGNobk5hbWUg"
    "aW4gc2VsZi5nYW1lQ2hhbm5lbHM6CiAgICAgICAgICAgIGNobiA9IHNlbGYuZ2FtZUNoYW5uZWxz"
    "W2Nobk5hbWVdCiAgICAgICAgICAgIGNobnMuYXBwZW5kKHdpcmVfZW5jb2RlKGYnJGdhbWVjaGFu"
    "bmVsICJ7Y2huTmFtZX0iICJ7bGVuKGNobi51c2VybGlzdCl9IiAie2Nobi5tYXh1c2VyfSIgIjAi"
    "ICIwIicpKSAjVE9ETyBBdmFpbGFibGUgLSBBbGwKICAgICAgICByZXR1cm4gX04uam9pbihjaG5z"
    "KStfTgogICAgZGVmIHVwZGF0ZVBvcyhzZWxmKToKICAgICAgICBtZCA9IHNlbGYuc2VydmVyLmRp"
    "c3QKICAgICAgICBmb3IgY2huIGluIGxpc3Qoc2VsZi5nYW1lQ2hhbm5lbHMudmFsdWVzKCkpOgog"
    "ICAgICAgICAgICBjaG4udXBkYXRlUG9zKG1kKQojaGFuZGxlcyBpbnRlcmFjdGlvbnMgYmV0d2Vl"
    "biBhbGwgZWxlbWVudHMKY2xhc3MgQ29yZVNlcnZlcihzb2NrZXRzZXJ2ZXIuVGhyZWFkaW5nVENQ"
    "U2VydmVyKToKICAgIGFsbG93X3JldXNlX2FkZHJlc3MgPSBUcnVlICMgVE9ETyBjaGVjayBpZiBp"
    "bXByb3ZlcyByZXN0YXJ0IHRpbWVzIHdpdGhvdXQgb3RoZXIgaXNzdWVzCiAgICBkYWVtb25fdGhy"
    "ZWFkcyA9IFRydWUKICAgIGJsb2NrX29uX2Nsb3NlID0gRmFsc2UKICAgIF9pc19jbG9zaW5nID0g"
    "RmFsc2UKICAgIGRlZiBfX2luaXRfXyhzZWxmKToKICAgICAgICAjVE9ETyBnZXQgdmFsdWVzIGZy"
    "b20gY2ZnCiAgICAgICAgI2FkZHJlc3MgPSAnbG9jYWxob3N0JwogICAgICAgIGFkZHJlc3MgPSAn"
    "JwogICAgICAgIHBvcnQgPSBfVFdfTE9CQllfUE9SVAogICAgICAgIHByaW50KGYnSW5pdGlhbGl6"
    "aW5nIHNlcnZlciBmb3IgcG9ydCB7cG9ydH0nKQogICAgICAgIHN1cGVyKCkuX19pbml0X18oKGFk"
    "ZHJlc3MsIHBvcnQpLCBDb25uZWN0aW9uSGFuZGxlcikKICAgICAgICBzZWxmLmRpc3QgPSBNZXNz"
    "YWdlRGlzdHJpYnV0b3Ioc2VsZikKICAgICAgICBzZWxmLmNvbXBhcnMgPSBDb21tYW5kUGFyc2Vy"
    "KHNlbGYuZGlzdCkKICAgICAgICBzZWxmLnN0YXRlID0gR2FtZVN0YXRlKHNlbGYpCiAgICAgICAg"
    "c2VsZi5zdGFydFRpbWUgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAgICAgIHNlbGYuc2Vy"
    "dmljZV90aWNrID0gMAogICAgICAgIHNlbGYuc2VuZF9ub3BzID0gX1NFTkRfTk9QUwogICAgICAg"
    "IHNlbGYuX3Bvc1N0b3AgPSB0aHJlYWRpbmcuRXZlbnQoKQogICAgICAgIHNlbGYuX3Bvc1RocmVh"
    "ZCA9IE5vbmUKICAgICAgICAjRXZlcnkgbGl2ZSBjb25uZWN0aW9uIGhhbmRsZXIuIHNvY2tldHNl"
    "cnZlcidzIHNodXRkb3duKCkgb25seSBzdG9wcwogICAgICAgICN0aGUgYWNjZXB0IGxvb3AgYW5k"
    "IGNsb3NlcyB0aGUgbGlzdGVuaW5nIHNvY2tldCAtIGFscmVhZHktZXN0YWJsaXNoZWQKICAgICAg"
    "ICAjY29ubmVjdGlvbnMga2VlcCB0aGVpciAoZGFlbW9uKSB0aHJlYWRzIHJ1bm5pbmcsIHN0aWxs"
    "IHJlYWRpbmcsIHN0aWxsCiAgICAgICAgI2xvZ2dpbmcsIGZvciBhcyBsb25nIGFzIHRoZSBjbGll"
    "bnQgc3RheXMgY29ubmVjdGVkLiBGcm9tIHRoZSBjb250cm9sCiAgICAgICAgI3BhbmVsIHRoYXQg"
    "bG9va3MgbGlrZSBhIHNlcnZlciB0aGF0IHdhcyBuZXZlciBzdG9wcGVkIGF0IGFsbC4KICAgICAg"
    "ICBzZWxmLl9jb25ucyA9IHNldCgpCiAgICAgICAgc2VsZi5fY29ubkxvY2sgPSB0aHJlYWRpbmcu"
    "TG9jaygpCiAgICBkZWYgc2VydmVyX2FjdGl2YXRlKHNlbGYpOgogICAgICAgIHByaW50KGYnU2Vy"
    "dmVyIFN0YXJ0aW5nIGF0IFBJRDoge29zLmdldHBpZCgpfScpI0xPRwogICAgICAgIHN1cGVyKCku"
    "c2VydmVyX2FjdGl2YXRlKCkKICAgIGRlZiBkZWJ1Z19kaWN0X3BsYXllcnMoc2VsZik6CiAgICAg"
    "ICAgI3NuYXBzaG90IHZpYSBsaXN0KCkgZmlyc3Q6IGl0ZXJhdGluZyB0aGUgbGl2ZSBkaWN0IGRp"
    "cmVjdGx5IHJpc2tzCiAgICAgICAgIydkaWN0aW9uYXJ5IGNoYW5nZWQgc2l6ZSBkdXJpbmcgaXRl"
    "cmF0aW9uJyB3aGVuIGEgcGxheWVyIGNvbm5lY3RzCiAgICAgICAgI29yIGRpc2Nvbm5lY3RzIHdo"
    "aWxlIGEgbW9uaXRvcmluZyBVSSBpcyBwb2xsaW5nIHRoaXMKICAgICAgICByZXQgPSB7fQogICAg"
    "ICAgIGZvciBuYW1lLCBjb24gaW4gbGlzdChzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLml0ZW1zKCkp"
    "OgogICAgICAgICAgICByZXRbbmFtZV0gPSBjb24uZGVidWdfZGljdCgpCiAgICAgICAgcmV0dXJu"
    "IHJldAogICAgZGVmIGRlYnVnX2RpY3RfdG93bnMoc2VsZik6CiAgICAgICAgcmV0ID0ge30KICAg"
    "ICAgICBmb3IgbmFtZSwgY2huIGluIGxpc3Qoc2VsZi5zdGF0ZS5nYW1lQ2hhbm5lbHMuaXRlbXMo"
    "KSk6CiAgICAgICAgICAgIHJldFtuYW1lXSA9IGNobi5kZWJ1Z19kaWN0KCkKICAgICAgICByZXR1"
    "cm4gcmV0CiAgICBkZWYgZGVidWdfYXJyX2dhbWVzKHNlbGYpOgogICAgICAgIHJldCA9IFtdCiAg"
    "ICAgICAgZm9yIG5hbWUsIGNobiBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1z"
    "KCkpOgogICAgICAgICAgICAgcmV0LmV4dGVuZChjaG4uZGVidWdfYXJyX2dhbWVzKCkpCiAgICAg"
    "ICAgcmV0dXJuIHJldAogICAgZGVmIF9wb3NMb29wKHNlbGYpOgogICAgICAgICNQb3NpdGlvbiBm"
    "YW4tb3V0IHVzZWQgdG8gcmlkZSBvbiBzZXJ2aWNlX2FjdGlvbnMoKSwgd2hpY2ggc29ja2V0c2Vy"
    "dmVyCiAgICAgICAgI2NhbGxzIG9uY2UgcGVyIHBvbGxfaW50ZXJ2YWwgLSBvbmUgc2Vjb25kLiBU"
    "aGF0IHdhcyB0aGUgY2FkZW5jZSBhdAogICAgICAgICN3aGljaCBvdGhlciBwbGF5ZXJzJyBtYXJr"
    "ZXJzIG1vdmVkIG9uIHRoZSBtYXA6IGEgZnVsbCBzZWNvbmQgb2YgZGVhZAogICAgICAgICNyZWNr"
    "b25pbmcgYmV0d2VlbiB1cGRhdGVzLCB3aGljaCByZWFkcyBhcyB0ZWxlcG9ydGluZyByYXRoZXIg"
    "dGhhbgogICAgICAgICN3YWxraW5nLiBJdHMgb3duIHRocmVhZCBkZWNvdXBsZXMgdGhlIGJyb2Fk"
    "Y2FzdCByYXRlIGZyb20gdGhlIGFjY2VwdAogICAgICAgICNsb29wJ3MgcG9sbCByYXRlIHNvIGl0"
    "IGNhbiBydW4gc2V2ZXJhbCB0aW1lcyBhIHNlY29uZC4KICAgICAgICB3aGlsZSBub3Qgc2VsZi5f"
    "cG9zU3RvcC5pc19zZXQoKToKICAgICAgICAgICAgcGVyaW9kID0gMS4wIC8gX1BPU19VUERBVEVf"
    "SFogaWYgX1BPU19VUERBVEVfSFogPiAwIGVsc2UgMS4wCiAgICAgICAgICAgICN3YWl0KCkgcmF0"
    "aGVyIHRoYW4gc2xlZXAoKTogc2h1dGRvd24gaXMgaW1tZWRpYXRlLCBhbmQgcmUtcmVhZGluZwog"
    "ICAgICAgICAgICAjdGhlIHBlcmlvZCBlYWNoIHBhc3MgbWVhbnMgYSBjb25maWcgY2hhbmdlIHRh"
    "a2VzIGVmZmVjdCBsaXZlLgogICAgICAgICAgICBpZiBzZWxmLl9wb3NTdG9wLndhaXQocGVyaW9k"
    "KToKICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAg"
    "IHNlbGYuc3RhdGUudXBkYXRlUG9zKCkKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAg"
    "ICAgICAgICAgICAgICNuZXZlciBsZXQgb25lIGJhZCBjaGFubmVsIGtpbGwgcG9zaXRpb24gc3lu"
    "YyBmb3IgZXZlcnlvbmUKICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIFBvc2l0aW9uIHVw"
    "ZGF0ZSBlcnJvcjpcbicgKyB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgZGVmIHNlcnZpY2Vf"
    "YWN0aW9ucyhzZWxmKTogI2NhbGxlZCBldmVyeSBwb2xsX2ludGVydmFsCiAgICAgICAgIyB0aW1l"
    "IGludGVydmFscwogICAgICAgIGlmIHNlbGYuc2VuZF9ub3BzIGFuZCAoc2VsZi5zZXJ2aWNlX3Rp"
    "Y2slMyk9PTA6CiAgICAgICAgICAgIHNlbGYuZGlzdC5hZGQoeyd0YXJnZXQnOnNlbGYuc3RhdGUu"
    "YWN0aXZlVXNlcnMudmFsdWVzKCksJ21lc3NhZ2UnOl9lbSgnL25vcCcpfSkKICAgICAgICAgICAg"
    "I3NlbmQgJy9ub3AnIHRvIGFsbCBldmVyeSAzIHNlYyBvcHRpb25hbGx5CiAgICAgICAgI3NlcnZp"
    "Y2UgdGljayAzIGRheSByZXNldCBpbnRlcnZhbCBUT0RPIHRlc3QgYWxpZ25tZW50IHdpdGggb3Ro"
    "ZXIgZmFjdG9ycwogICAgICAgIHNlbGYuc2VydmljZV90aWNrID0gKHNlbGYuc2VydmljZV90aWNr"
    "KzEpJSg2MCo2MCoyNCozKQogICAgICAgIHN1cGVyKCkuc2VydmljZV9hY3Rpb25zKCkKICAgIGRl"
    "ZiBzZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIGRpc3RUaHJlYWQgPSB0aHJlYWRpbmcuVGhy"
    "ZWFkKHRhcmdldD1zZWxmLmRpc3Quc2VydmVfZm9yZXZlcikKICAgICAgICBkaXN0VGhyZWFkLnN0"
    "YXJ0KCkKICAgICAgICBzZWxmLl9wb3NTdG9wLmNsZWFyKCkKICAgICAgICBzZWxmLl9wb3NUaHJl"
    "YWQgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLl9wb3NMb29wLCBkYWVtb249VHJ1ZSkK"
    "ICAgICAgICBzZWxmLl9wb3NUaHJlYWQuc3RhcnQoKQogICAgICAgICNwb2xsX2ludGVydmFsIGlz"
    "IG5vdyBvbmx5IHRoZSBhY2NlcHQgbG9vcCdzIHNodXRkb3duIHJlc3BvbnNpdmVuZXNzIC0KICAg"
    "ICAgICAjcG9zaXRpb24gYnJvYWRjYXN0cyBubyBsb25nZXIgcmlkZSBvbiBpdAogICAgICAgIHN1"
    "cGVyKCkuc2VydmVfZm9yZXZlcigxKQogICAgICAgIHNlbGYuX3Bvc1N0b3Auc2V0KCkKICAgICAg"
    "ICBpZiBzZWxmLl9wb3NUaHJlYWQ6CiAgICAgICAgICAgIHNlbGYuX3Bvc1RocmVhZC5qb2luKHRp"
    "bWVvdXQ9Mi4wKQogICAgICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSBOb25lCiAgICAgICAgc2Vs"
    "Zi5kaXN0LmVuZCgpI2luIGNhc2UgaXQgaGFzbid0IGFscmVhZHkKICAgICAgICBkaXN0VGhyZWFk"
    "LmpvaW4oKQogICAgZGVmIGhhbmRsZV9zaWduYWwoc2VsZiwgdGltZW91dCk6CiAgICAgICAgZGVm"
    "IGhhbmRsZXIoc2lnbnVtLCBfKToKICAgICAgICAgICAgZGVhZGxpbmUgPSB0aW1lLm1vbm90b25p"
    "YygpICsgdGltZW91dAogICAgICAgICAgICBzaWduYW1lID0gc2lnbmFsLlNpZ25hbHMoc2lnbnVt"
    "KS5uYW1lCiAgICAgICAgICAgIHNlbGYuX2lzX2Nsb3NpbmcgPSBUcnVlICNUT0RPIHByb3Blcmx5"
    "IGVuZCBjb25uZWN0aW9ucyBhZnRlciBhIGRlbGF5CiAgICAgICAgICAgIHByaW50KGYnQ2xvc2lu"
    "ZyBpbiB7dGltZW91dH0nKQogICAgICAgICAgICAjd2hpbGUgKGN1cnJlbnRfdGltZSA6PSB0aW1l"
    "Lm1vbm90b25pYygpKSA8IGRlYWRsaW5lOgogICAgICAgICAgICAjICAgIGRlbHRhID0gaW50KGRl"
    "YWRsaW5lIC0gY3VycmVudF90aW1lKQogICAgICAgICAgICAgICAgI1RPRE8gc2lnbmFsIHRvIHBs"
    "YXllcnMgdGhhdCBjb25uZWN0aW9uIGlzIHNodXR0aW5nIGRvd24KICAgICAgICAgICAgICAgICMt"
    "IHNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCkKICAgICAgICAgICAgICAgICMtIGYnL2Fk"
    "bWluIFNlcnZlciBjbG9zaW5nIGluIHtkZWx0YX0nLmVuY29kZSgnYXNjaWknKStfTgogICAgICAg"
    "ICAgICAgICAgI0xPRyBDTE9TRQogICAgICAgICAgICAgICAgI1RPRE8gYmV0dGVyIHNodXRkb3du"
    "IGhhbmRsaW5nCiAgICAgICAgICAgICMgICAgdGltZS5zbGVlcCgxKQogICAgICAgICAgICB0aW1l"
    "LnNsZWVwKHRpbWVvdXQpI2FsdCB3aGlsZSBvdGhlciBzdHVmZiBpcyBvbmdvaW5nCiAgICAgICAg"
    "ICAgIHNlbGYuX0Jhc2VTZXJ2ZXJfX3NodXRkb3duX3JlcXVlc3QgPSBUcnVlCiAgICAgICAgICAg"
    "ICNzZWxmLnNodXRkb3duKCkgI29ubHkgaWYgc2VydmVfZm9yZXZlciBpcyBpbiBhIGRpZmZlcmVu"
    "dCB0aHJlYWQKICAgICAgICAgICAgI3NlbGYuc2VydmVyX2Nsb3NlKCkgI29ubHkgbmVlZGVkIGlm"
    "IG5vdCB1c2luZyBhIHdpdGggc3RhdGVtZW50CiAgICAgICAgcmV0dXJuIGhhbmRsZXIKICAgIGRl"
    "ZiByZWdpc3RlckNvbm5lY3Rpb24oc2VsZiwgY29uKToKICAgICAgICB3aXRoIHNlbGYuX2Nvbm5M"
    "b2NrOgogICAgICAgICAgICBzZWxmLl9jb25ucy5hZGQoY29uKQogICAgZGVmIHVucmVnaXN0ZXJD"
    "b25uZWN0aW9uKHNlbGYsIGNvbik6CiAgICAgICAgd2l0aCBzZWxmLl9jb25uTG9jazoKICAgICAg"
    "ICAgICAgc2VsZi5fY29ubnMuZGlzY2FyZChjb24pCiAgICBkZWYgY2xvc2VDb25uZWN0aW9ucyhz"
    "ZWxmKToKICAgICAgICAjRHJvcCBldmVyeSBjbGllbnQuIFNodXR0aW5nIHRoZSBzb2NrZXQgZG93"
    "biB1bmJsb2NrcyB3aGljaGV2ZXIKICAgICAgICAjc2VsZWN0KCkvcmVjdigpIHRoYXQgY29ubmVj"
    "dGlvbidzIHRocmVhZCBpcyBzaXR0aW5nIGluLCBzbyBpdCBydW5zCiAgICAgICAgI2l0cyBub3Jt"
    "YWwgY2xlYW51cCBwYXRoIGFuZCBleGl0cyBpbnN0ZWFkIG9mIGxpbmdlcmluZy4KICAgICAgICB3"
    "aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBjb25ucyA9IGxpc3Qoc2VsZi5fY29ubnMp"
    "CiAgICAgICAgZm9yIGNvbiBpbiBjb25uczoKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAg"
    "ICAgY29uLnJlcXVlc3Quc2h1dGRvd24oc29ja2V0LlNIVVRfUkRXUikKICAgICAgICAgICAgZXhj"
    "ZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgICAgIHBhc3MgI2FscmVhZHkgZGVhZCwgb3IgbmV2"
    "ZXIgZnVsbHkgY29ubmVjdGVkCiAgICAgICAgICAgICNEZWxpYmVyYXRlbHkgbm90IGNsb3NlKClk"
    "IGhlcmU6IHRoZSBoYW5kbGVyIHRocmVhZCBzdGlsbCBvd25zIHRoaXMKICAgICAgICAgICAgI3Nv"
    "Y2tldCBhbmQgY2xvc2luZyBpdCB1bmRlcm5lYXRoIGNhdXNlcyBpdHMgbmV4dCBjYWxsIHRvIGZh"
    "aWwgd2l0aAogICAgICAgICAgICAjV2luRXJyb3IgMTAwMzggKCJub3QgYSBzb2NrZXQiKSwgd2hp"
    "Y2ggdGhlbiBnZXRzIGxvZ2dlZCBhcyBhCiAgICAgICAgICAgICNjb25uZWN0aW9uIGVycm9yIG9u"
    "IGEgcGVyZmVjdGx5IG5vcm1hbCBzaHV0ZG93bi4gc2h1dGRvd24oKSBhbG9uZQogICAgICAgICAg"
    "ICAjd2FrZXMgdGhlIHRocmVhZCwgYW5kIHNvY2tldHNlcnZlciBjbG9zZXMgdGhlIHNvY2tldCBp"
    "dHNlbGYgb25jZQogICAgICAgICAgICAjdGhlIGhhbmRsZXIgcmV0dXJucy4KICAgICAgICByZXR1"
    "cm4gbGVuKGNvbm5zKQogICAgZGVmIHNodXRkb3duKHNlbGYpOgogICAgICAgICNTdG9wcGluZyB0"
    "aGUgc2VydmVyIG1lYW5zIHN0b3BwaW5nIGl0OiBmbGFnIGl0IGZpcnN0IHNvIHRoZSByZWFkCiAg"
    "ICAgICAgI2xvb3BzIGJhaWwgb3V0IHJhdGhlciB0aGFuIHNlcnZpbmcgYW5vdGhlciBjb21tYW5k"
    "LCB0aGVuIHN0b3AgdGhlCiAgICAgICAgI2FjY2VwdCBsb29wLCB0aGVuIGV2aWN0IGV2ZXJ5b25l"
    "IHN0aWxsIGNvbm5lY3RlZC4KICAgICAgICBzZWxmLl9pc19jbG9zaW5nID0gVHJ1ZQogICAgICAg"
    "IHN1cGVyKCkuc2h1dGRvd24oKQogICAgICAgIG4gPSBzZWxmLmNsb3NlQ29ubmVjdGlvbnMoKQog"
    "ICAgICAgIGlmIG46CiAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBDbG9zZWQge259IGNsaWVu"
    "dCBjb25uZWN0aW9uKHMpIG9uIHNodXRkb3duJykKICAgIGRlZiBnZXRQbGF5ZXIoc2VsZiwgdXNl"
    "cm5hbWUpOgogICAgICAgIHJldHVybiBzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLmdldCh1c2VybmFt"
    "ZSkKICAgIGRlZiBraWNrUGxheWVyKHNlbGYsIHVzZXJuYW1lLCByZWFzb249J0tpY2tlZCBieSBh"
    "ZG1pbicpOgogICAgICAgICNBZG1pbi1wYW5lbCBhY3Rpb246IGZvcmNpYmx5IGRpc2Nvbm5lY3Qg"
    "YSBjb25uZWN0ZWQgcGxheWVyLiBTZW5kcyBhCiAgICAgICAgI2Jlc3QtZWZmb3J0IC9hZG1pbiBu"
    "b3RpY2UgZmlyc3QgKGNsaWVudCBzaG93cyBpdCBsaWtlIGFueSBvdGhlcgogICAgICAgICNzZXJ2"
    "ZXIgYWRtaW4gbWVzc2FnZSksIHRoZW4gc2h1dHMgZG93biB0aGUgc29ja2V0IHNvIHRoZSBwbGF5"
    "ZXIncwogICAgICAgICNoYW5kbGVyIHRocmVhZCB1bmJsb2NrcyBmcm9tIGl0cyByZWN2KCkgYW5k"
    "IHJ1bnMgaXRzIG5vcm1hbAogICAgICAgICNkaXNjb25uZWN0L2NsZWFudXAgcGF0aC4KICAgICAg"
    "ICBjb24gPSBzZWxmLmdldFBsYXllcih1c2VybmFtZSkKICAgICAgICBpZiBjb24gaXMgTm9uZToK"
    "ICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgdHJ5OgogICAgICAgICAgICBjb24uc2Vu"
    "ZFJhdyhfZW0oZicvYWRtaW4ge3JlYXNvbn0nKSkKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgog"
    "ICAgICAgICAgICBwYXNzICNiZXN0IGVmZm9ydCwgY29ubmVjdGlvbiBtYXkgYWxyZWFkeSBiZSBv"
    "biBpdHMgd2F5IG91dAogICAgICAgIHRyeToKICAgICAgICAgICAgY29uLnJlcXVlc3Quc2h1dGRv"
    "d24oc29ja2V0LlNIVVRfUkRXUikKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAg"
    "ICBwYXNzCiAgICAgICAgdHJ5OgogICAgICAgICAgICBjb24ucmVxdWVzdC5jbG9zZSgpCiAgICAg"
    "ICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFzcwogICAgICAgIHJldHVybiBUcnVl"
    "CiAgICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBh"
    "bmVsIGFjdGlvbjogcGVybWFuZW50bHkgZGVsZXRlcyBhIGNoYXJhY3Rlci9hY2NvdW50LgogICAg"
    "ICAgICNLaWNrcyBmaXJzdCAobm8tb3AgaWYgYWxyZWFkeSBvZmZsaW5lKSBzbyBhIGNvbm5lY3Rl"
    "ZCBjbGllbnQgbmV2ZXIKICAgICAgICAja2VlcHMgcGxheWluZyBvbiBhbiBhY2NvdW50IHRoYXQg"
    "aGFzIGp1c3QgdmFuaXNoZWQgZnJvbSB0aGUgREIuCiAgICAgICAgc2VsZi5raWNrUGxheWVyKHVz"
    "ZXJuYW1lLCByZWFzb249J0FjY291bnQgZGVsZXRlZCBieSBhZG1pbicpCiAgICAgICAgcmV0dXJu"
    "IEdESC5kZWxldGVBY2NvdW50KHVzZXJuYW1lKQojRmFpbGVkLWxvZ2luIHRocm90dGxlLCBwZXIg"
    "c291cmNlIElQLgojVHdvIHJlYXNvbnMgdGhpcyBpcyBub3Qgb3B0aW9uYWwgb24gYSBzZXJ2ZXIg"
    "cmVhY2hhYmxlIGZyb20gdGhlIGludGVybmV0OgojYSBwYXNzd29yZCBndWVzcyBpcyBjaGVhcCBm"
    "b3IgdGhlIGF0dGFja2VyIGJ1dCBjb3N0cyAqdXMqIGEgMTAway1pdGVyYXRpb24KI1BCS0RGMiAo"
    "dGVucyBvZiBtcyBvZiBDUFUgZWFjaCksIHNvIGFuIHVudGhyb3R0bGVkIGxvZ2luIGVuZHBvaW50"
    "IGlzIGJvdGggYQojYnJ1dGUtZm9yY2Ugb3JhY2xlIGFuZCBhIENQVSBhbXBsaWZpZXIgLSBhIGhh"
    "bmRmdWwgb2YgY29ubmVjdGlvbnMgY2FuIHBpbgojZXZlcnkgY29yZS4gU3VjY2Vzc2Z1bCBsb2dp"
    "bnMgY2xlYXIgdGhlIGNvdW50ZXIsIHNvIGEgcGxheWVyIGZ1bWJsaW5nIHRoZWlyCiNwYXNzd29y"
    "ZCBhIGZldyB0aW1lcyBpcyBuZXZlciBsb2NrZWQgb3V0IGZvciBsb25nLgpfTE9HSU5fRkFJTF9M"
    "SU1JVCA9IDYgICAgICAjZmFpbHVyZXMgYWxsb3dlZCBpbnNpZGUgdGhlIHdpbmRvdyBiZWZvcmUg"
    "ZGVsYXlpbmcKX0xPR0lOX0ZBSUxfV0lORE9XID0gMzAwICAgI3NlY29uZHMgYSBmYWlsdXJlIGlz"
    "IHJlbWVtYmVyZWQKX0xPR0lOX0ZBSUxfREVMQVkgPSAyLjAgICAgI3NlY29uZHMgdG8gc3RhbGwg"
    "ZWFjaCBhdHRlbXB0IG9uY2Ugb3ZlciB0aGUgbGltaXQKY2xhc3MgTG9naW5UaHJvdHRsZSgpOgog"
    "ICAgZGVmIF9faW5pdF9fKHNlbGYpOgogICAgICAgIHNlbGYubG9jayA9IHRocmVhZGluZy5Mb2Nr"
    "KCkKICAgICAgICBzZWxmLmZhaWxzID0ge30gI2lwIC0+IFt0aW1lc3RhbXBzXQogICAgZGVmIF9w"
    "cnVuZShzZWxmLCBpcCwgbm93KToKICAgICAgICByZWNlbnQgPSBbdCBmb3IgdCBpbiBzZWxmLmZh"
    "aWxzLmdldChpcCwgKCkpIGlmIG5vdyAtIHQgPCBfTE9HSU5fRkFJTF9XSU5ET1ddCiAgICAgICAg"
    "aWYgcmVjZW50OgogICAgICAgICAgICBzZWxmLmZhaWxzW2lwXSA9IHJlY2VudAogICAgICAgIGVs"
    "c2U6CiAgICAgICAgICAgIHNlbGYuZmFpbHMucG9wKGlwLCBOb25lKQogICAgICAgIHJldHVybiBy"
    "ZWNlbnQKICAgIGRlZiBkZWxheUZvcihzZWxmLCBpcCk6CiAgICAgICAgbm93ID0gdGltZS5tb25v"
    "dG9uaWMoKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICByZWNlbnQgPSBzZWxm"
    "Ll9wcnVuZShpcCwgbm93KQogICAgICAgIHJldHVybiBfTE9HSU5fRkFJTF9ERUxBWSBpZiBsZW4o"
    "cmVjZW50KSA+PSBfTE9HSU5fRkFJTF9MSU1JVCBlbHNlIDAuMAogICAgZGVmIHJlY29yZEZhaWx1"
    "cmUoc2VsZiwgaXApOgogICAgICAgIG5vdyA9IHRpbWUubW9ub3RvbmljKCkKICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgcmVjZW50ID0gc2VsZi5fcHJ1bmUoaXAsIG5vdykKICAg"
    "ICAgICAgICAgcmVjZW50LmFwcGVuZChub3cpCiAgICAgICAgICAgIHNlbGYuZmFpbHNbaXBdID0g"
    "cmVjZW50CiAgICAgICAgICAgIHJldHVybiBsZW4ocmVjZW50KQogICAgZGVmIHJlY29yZFN1Y2Nl"
    "c3Moc2VsZiwgaXApOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBzZWxmLmZh"
    "aWxzLnBvcChpcCwgTm9uZSkKTE9HSU5fVEhST1RUTEUgPSBMb2dpblRocm90dGxlKCkKCl9MT0dJ"
    "Tl9FUlJPUlMgPSB7CiAgICAxOiAnSW52YWxpZCB1c2VybmFtZSBvciBwYXNzd29yZCcsCiAgICAy"
    "OiAnQWNjb3VudCBhbHJlYWR5IGxvZ2dlZCBpbicsCiAgICAzOiAnUGFzc3dvcmQgcmVxdWlyZWQn"
    "LAogICAgNDogJ1VzZXJuYW1lIHJlcXVpcmVkJywKfQpfUkVHSVNURVJfRVJST1JTID0gewogICAg"
    "MTogJ0FjY291bnQgYWxyZWFkeSBsb2dnZWQgaW4nLAogICAgMjogJ1VzZXJuYW1lIHVuYXZhaWxh"
    "YmxlIG9yIGludmFsaWQnLAp9CiNoYW5kbGVzIGluZGl2aWR1YWwgY29ubmVjdGlvbnMKY2xhc3Mg"
    "Q29ubmVjdGlvbkhhbmRsZXIoc29ja2V0c2VydmVyLkJhc2VSZXF1ZXN0SGFuZGxlcik6CiAgICAj"
    "ZGVmYXVsdCBwcm9wZXJ0aWVzOgogICAgIyAtIHJlcXVlc3Q6IHNvY2tldCB0byBkZXN0aW5hdGlv"
    "bgogICAgIyAtIGNsaWVudF9hZGRyZXNzCiAgICAjIC0gc2VydmVyOiBDb3JlU2VydmVyCiAgICBf"
    "U1RPUFdSSVRFUiA9IG9iamVjdCgpCiAgICBkZWYgc2V0dXAoc2VsZik6CiAgICAgICAgc2VsZi5f"
    "c1F1ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAgIHNlbGYudXNlciA9IE5vbmUKICAgICAgICBz"
    "ZWxmLmd1aWQgPSBOb25lCiAgICAgICAgc2VsZi5kYXRhID0gYicnCiAgICAgICAgc2VsZi5TSyA9"
    "IGJ5dGVhcnJheShzdHJ1Y3QucGFjaygnPElJJywgMHhBNkFFMUY5QiwgMHg0MzhERkY0MCkpCiAg"
    "ICAgICAgI1NlcmlhbGlzZXMgdGhlIHJhdyBzb2NrZXQgd3JpdGVzLiBUaHJlZSB0aHJlYWRzIGNh"
    "biB3YW50IHRvIHdyaXRlIHRvCiAgICAgICAgI29uZSBjbGllbnQ6IHRoaXMgY29ubmVjdGlvbidz"
    "IG93biByZWFkIGxvb3AgKGR1cmluZyB0aGUgaGFuZHNoYWtlKSwKICAgICAgICAjaXRzIHdyaXRl"
    "ciB0aHJlYWQsIGFuZCB0aGUgR1VJIHRocmVhZCB2aWEga2lja1BsYXllcigpLiBXaXRob3V0IHRo"
    "ZQogICAgICAgICNsb2NrIHR3byBzZW5kYWxsKCkgY2FsbHMgY2FuIGludGVybGVhdmUgYW5kIHNw"
    "bGl0IGEgcGFja2V0IGRvd24gdGhlCiAgICAgICAgI21pZGRsZSwgd2hpY2ggdGhlIGNsaWVudCBz"
    "ZWVzIGFzIHByb3RvY29sIGdhcmJhZ2UuCiAgICAgICAgc2VsZi5fc2VuZExvY2sgPSB0aHJlYWRp"
    "bmcuTG9jaygpCiAgICAgICAgc2VsZi5fd3JpdGVyID0gTm9uZQogICAgICAgIHNlbGYuX3dyaXRl"
    "ckRlYWQgPSB0aHJlYWRpbmcuRXZlbnQoKQogICAgICAgIHNlbGYuX2xhc3RSZWN2ID0gdGltZS5t"
    "b25vdG9uaWMoKQogICAgICAgIHNlbGYuc2VydmVyLnJlZ2lzdGVyQ29ubmVjdGlvbihzZWxmKQog"
    "ICAgICAgIHRyeToKICAgICAgICAgICAgI05hZ2xlIGJhdGNoZXMgc21hbGwgd3JpdGVzIGJ5IGhv"
    "bGRpbmcgdGhlbSBmb3IgdXAgdG8gfjQwbXMgd2FpdGluZwogICAgICAgICAgICAjZm9yIG1vcmUg"
    "ZGF0YS4gRXZlcnkgbWVzc2FnZSB0aGlzIHNlcnZlciBzZW5kcyBpcyBzbWFsbCBhbmQKICAgICAg"
    "ICAgICAgI2xhdGVuY3ktc2Vuc2l0aXZlIC0gY2hhdCwgcG9zaXRpb24gdXBkYXRlcyBhbmQgYWJv"
    "dmUgYWxsIHRoZQogICAgICAgICAgICAjL2dhbWVjb21tYW5kdG91c2VyIHJlbGF5IHRoYXQgY2Fy"
    "cmllcyB0aGUgYWN0dWFsIGluLWdhbWUgY28tb3AKICAgICAgICAgICAgI3RyYWZmaWMgYmV0d2Vl"
    "biB0d28gcGxheWVycyAtIHNvIHRoZSBkZWxheSBpcyBwdXJlIGFkZGVkIGxhZy4KICAgICAgICAg"
    "ICAgc2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LklQUFJPVE9fVENQLCBzb2NrZXQuVENQ"
    "X05PREVMQVksIDEpCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MgI25v"
    "dCBmYXRhbCwganVzdCBzbG93ZXIKICAgICAgICB0cnk6CiAgICAgICAgICAgICNBc2sgdGhlIE9T"
    "IHRvIHByb2JlIGFuIGlkbGUgY29ubmVjdGlvbi4gV2hlbiBhIHBsYXllcidzIGdhbWUKICAgICAg"
    "ICAgICAgI2NyYXNoZXMgb3V0cmlnaHQgdGhlIHNvY2tldCBpcyB1c3VhbGx5IHJlc2V0IGFuZCB3"
    "ZSBmaW5kIG91dCBhdAogICAgICAgICAgICAjb25jZSwgYnV0IGEgbWFjaGluZSB0aGF0IGZyZWV6"
    "ZXMsIHNsZWVwcyBvciBsb3NlcyBpdHMgbGluayBzZW5kcwogICAgICAgICAgICAjbm90aGluZyBh"
    "dCBhbGw6IHdpdGhvdXQgcHJvYmVzIHRoYXQgY29ubmVjdGlvbiBzaXRzIHRoZXJlIGhvbGRpbmcK"
    "ICAgICAgICAgICAgI3RoZSBhY2NvdW50ICgiQWNjb3VudCBhbHJlYWR5IGxvZ2dlZCBpbiIpIGFu"
    "ZCBpdHMgcm9vbSB1bnRpbCB0aGUKICAgICAgICAgICAgI2lkbGUgdGltZW91dCBleHBpcmVzIG1p"
    "bnV0ZXMgbGF0ZXIuIFByb2JlIGFmdGVyIDMwcyBpZGxlLCB0aGVuCiAgICAgICAgICAgICNldmVy"
    "eSA1cy4KICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LlNPTF9TT0NL"
    "RVQsIHNvY2tldC5TT19LRUVQQUxJVkUsIDEpCiAgICAgICAgICAgIGlmIGhhc2F0dHIoc2VsZi5y"
    "ZXF1ZXN0LCAnaW9jdGwnKSBhbmQgaGFzYXR0cihzb2NrZXQsICdTSU9fS0VFUEFMSVZFX1ZBTFMn"
    "KToKICAgICAgICAgICAgICAgIHNlbGYucmVxdWVzdC5pb2N0bChzb2NrZXQuU0lPX0tFRVBBTElW"
    "RV9WQUxTLCAoMSwgMzAwMDAsIDUwMDApKSAjV2luZG93cwogICAgICAgICAgICBlbHNlOgogICAg"
    "ICAgICAgICAgICAgZm9yIChvcHQsIHZhbCkgaW4gKCgnVENQX0tFRVBJRExFJywgMzApLCAoJ1RD"
    "UF9LRUVQSU5UVkwnLCA1KSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAoJ1RD"
    "UF9LRUVQQ05UJywgNCkpOgogICAgICAgICAgICAgICAgICAgIGlmIGhhc2F0dHIoc29ja2V0LCBv"
    "cHQpOgogICAgICAgICAgICAgICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2V0c29ja29wdChzb2Nr"
    "ZXQuSVBQUk9UT19UQ1AsIGdldGF0dHIoc29ja2V0LCBvcHQpLCB2YWwpCiAgICAgICAgZXhjZXB0"
    "IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MgI2tlZXBhbGl2ZSBpcyBhbiBvcHRpbWlzYXRpb24s"
    "IG5vdCBhIHJlcXVpcmVtZW50CiAgICBkZWYgc2VuZFJhdyhzZWxmLCBtc2cpOgogICAgICAgICNU"
    "aGUgc2luZ2xlIGZ1bm5lbCBmb3IgZXZlcnkgYnl0ZSBsZWF2aW5nIHRoZSBzZXJ2ZXIgb24gdGhp"
    "cyBzb2NrZXQuCiAgICAgICAgd2l0aCBzZWxmLl9zZW5kTG9jazoKICAgICAgICAgICAgc2VsZi5y"
    "ZXF1ZXN0LnNlbmRhbGwobXNnKQogICAgZGVmIHNlbmQoc2VsZiwgbXNnKToKICAgICAgICAjTm9y"
    "bWFsIHBhdGggb25jZSB0aGUgY29ubmVjdGlvbiBpcyBsaXZlOiBoYW5kIG9mZiB0byB0aGUgd3Jp"
    "dGVyIHRocmVhZAogICAgICAgICNzbyB0aGUgY2FsbGVyIChhIGNvbW1hbmQgaGFuZGxlciwgb3Ig"
    "dGhlIGRpc3RyaWJ1dG9yJ3MgZmFuLW91dCkgbmV2ZXIKICAgICAgICAjYmxvY2tzIG9uIGEgc2xv"
    "dyBvciBzdGFsbGVkIGNsaWVudC4KICAgICAgICBpZiBtc2c6CiAgICAgICAgICAgIHNlbGYuX3NR"
    "dWV1ZS5wdXQobXNnKQogICAgZGVmIF93cml0ZXJMb29wKHNlbGYpOgogICAgICAgICNCbG9ja3Mg"
    "b24gdGhlIHF1ZXVlIGluc3RlYWQgb2YgYmVpbmcgcG9sbGVkLiBQcmV2aW91c2x5IHRoZSByZWFk"
    "IGxvb3AKICAgICAgICAjZHJhaW5lZCB0aGlzIHF1ZXVlIGl0c2VsZiBiZXR3ZWVuIHJlY3YoKSB0"
    "aW1lb3V0cywgc28gYW55dGhpbmcgcXVldWVkCiAgICAgICAgI2p1c3QgYWZ0ZXIgdGhlIHRocmVh"
    "ZCB3ZW50IGJhY2sgaW50byByZWN2KCkgd2FpdGVkIG91dCB0aGUgZnVsbAogICAgICAgICN0aW1l"
    "b3V0IC0gdXAgdG8gMTAwbXMgb2YgbGF0ZW5jeSBhZGRlZCB0byBldmVyeSByZWxheWVkIGdhbWUg"
    "Y29tbWFuZCwKICAgICAgICAjb24gdG9wIG9mIGV2ZXJ5IGlkbGUgY29ubmVjdGlvbiB3YWtpbmcg"
    "MTAgdGltZXMgYSBzZWNvbmQgdG8gY2hlY2suCiAgICAgICAgdHJ5OgogICAgICAgICAgICB3aGls"
    "ZSBUcnVlOgogICAgICAgICAgICAgICAgbXNnID0gc2VsZi5fc1F1ZXVlLmdldCgpCiAgICAgICAg"
    "ICAgICAgICBpZiBtc2cgaXMgc2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICBi"
    "cmVhawogICAgICAgICAgICAgICAgI0NvYWxlc2NlIHdoYXRldmVyIGVsc2UgcGlsZWQgdXAgYmVo"
    "aW5kIGl0IGludG8gYSBzaW5nbGUgd3JpdGUuCiAgICAgICAgICAgICAgICAjUG9zaXRpb24gYnJv"
    "YWRjYXN0cyBhbmQgZ2FtZSBjb21tYW5kcyBvZnRlbiBhcnJpdmUgaW4gYnVyc3RzLgogICAgICAg"
    "ICAgICAgICAgY2h1bmtzID0gW21zZ10KICAgICAgICAgICAgICAgIHdoaWxlIFRydWU6CiAgICAg"
    "ICAgICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgICAgICAgICBueHQgPSBzZWxmLl9z"
    "UXVldWUuZ2V0X25vd2FpdCgpCiAgICAgICAgICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoK"
    "ICAgICAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgICAgICAgICBpZiBueHQg"
    "aXMgc2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3"
    "KGInJy5qb2luKGNodW5rcykpCiAgICAgICAgICAgICAgICAgICAgICAgIHJldHVybgogICAgICAg"
    "ICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQobnh0KQogICAgICAgICAgICAgICAgc2VsZi5zZW5k"
    "UmF3KGInJy5qb2luKGNodW5rcykpCiAgICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVzZXRFcnJv"
    "ciwgQ29ubmVjdGlvbkFib3J0ZWRFcnJvciwgQnJva2VuUGlwZUVycm9yLCBPU0Vycm9yKToKICAg"
    "ICAgICAgICAgcGFzcyAjcGVlciBpcyBnb25lOyB0aGUgcmVhZCBsb29wIG5vdGljZXMgYW5kIHJ1"
    "bnMgdGhlIGNsZWFudXAKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwcmlu"
    "dCgnW0xvYmJ5XSBXcml0ZXIgZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAg"
    "ICAgICBmaW5hbGx5OgogICAgICAgICAgICBzZWxmLl93cml0ZXJEZWFkLnNldCgpCiAgICBkZWYg"
    "X3N0YXJ0V3JpdGVyKHNlbGYpOgogICAgICAgIHNlbGYuX3dyaXRlciA9IHRocmVhZGluZy5UaHJl"
    "YWQodGFyZ2V0PXNlbGYuX3dyaXRlckxvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNlbGYuX3dy"
    "aXRlci5zdGFydCgpCiAgICBkZWYgX3N0b3BXcml0ZXIoc2VsZik6CiAgICAgICAgaWYgc2VsZi5f"
    "d3JpdGVyIGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHNlbGYuX3NRdWV1ZS5w"
    "dXQoc2VsZi5fU1RPUFdSSVRFUikKICAgICAgICBzZWxmLl93cml0ZXIuam9pbih0aW1lb3V0PTIu"
    "MCkKICAgICAgICBzZWxmLl93cml0ZXIgPSBOb25lCiAgICBkZWYgX2NsYWltU2Vzc2lvbihzZWxm"
    "KToKICAgICAgICAjVGFrZSBvd25lcnNoaXAgb2YgdGhlIHVzZXJuYW1lIHNsb3QgYmVmb3JlIHRl"
    "bGxpbmcgdGhlIGNsaWVudCBpdCBpcwogICAgICAgICNsb2dnZWQgaW4uIFJldHVybnMgRmFsc2Ug"
    "aWYgYW5vdGhlciBjb25uZWN0aW9uIGdvdCB0aGVyZSBmaXJzdC4KICAgICAgICBpZiBzZWxmLnNl"
    "cnZlci5zdGF0ZS5jbGFpbVVzZXIoc2VsZi51c2VyLm5hbWUsIHNlbGYpOgogICAgICAgICAgICBy"
    "ZXR1cm4gVHJ1ZQogICAgICAgIHNlbGYudXNlci5kaXNjb25uZWN0KHNlbGYuc2VydmVyKSAjcmVs"
    "ZWFzZXMgdGhlIGlkbnVtIHdlIGp1c3QgYWxsb2NhdGVkCiAgICAgICAgc2VsZi51c2VyID0gTm9u"
    "ZQogICAgICAgIHJldHVybiBGYWxzZQogICAgZGVmIGF0dGVtcHRMb2dpbihzZWxmLCB1c2VybmFt"
    "ZSwgcGFzc3dvcmQpOgogICAgICAgIGlmIGxlbih1c2VybmFtZSk8MToKICAgICAgICAgICAgcmV0"
    "dXJuIDQgI05vIFVzZXJuYW1lLCBsaWtlbHkgZnJlc2ggbG9naW4KICAgICAgICAgICAgI1RPRE8g"
    "Y2hlY2sgaWYgc2VyaWFsIGV4aXN0cyBhbmQgcmV0dXJuIHVzZXJuYW1lIHByb3Blcmx5CiAgICAg"
    "ICAgaWYgbGVuKHBhc3N3b3JkKTwxOgogICAgICAgICAgICByZXR1cm4gMyAjUGFzc3dvcmQgdG9v"
    "IHNob3J0CiAgICAgICAgI1Rlc3QgaWYgcGxheWVyIGFscmVhZHkgbG9nZ2VkIGluIChmYXN0IHBh"
    "dGg7IHRoZSBhdXRob3JpdGF0aXZlLAogICAgICAgICNyYWNlLWZyZWUgY2hlY2sgaXMgdGhlIGNs"
    "YWltVXNlcigpIGJlbG93KQogICAgICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1c2VybmFt"
    "ZSk6CiAgICAgICAgICAgIHJldHVybiAyICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJST1IKICAg"
    "ICAgICAjcGxheWVyIG5vdCBjdXJyZW50bHkgbG9nZ2VkIGluLCBhdHRlbXB0IHRvIGxvZ2luIHZp"
    "YSBkYXRhIGhhbmRsZXIKICAgICAgICBzZWxmLnVzZXIgPSBHREgubG9naW5QbGF5ZXIodXNlcm5h"
    "bWUsIHNlbGYsIHBhc3N3b3JkKQogICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgcmV0"
    "dXJuIDAgaWYgc2VsZi5fY2xhaW1TZXNzaW9uKCkgZWxzZSAyCiAgICAgICAgcmV0dXJuIDEgI1RP"
    "RE8gR2V0IGZyb20gR0RILmxvZ2luUGxheWVyLCBwYXNzIHVzZXIgb2JqZWN0IGFsb25nPwogICAg"
    "ZGVmIGF0dGVtcHRSZWdpc3RlcihzZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2Nh"
    "dGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKToKICAgICAgICAjVGVzdCBpZiBwbGF5ZXIg"
    "YWxyZWFkeSBsb2dnZWQgaW4KICAgICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIodXNlcm5h"
    "bWUpOgogICAgICAgICAgICByZXR1cm4gMSAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVSUk9SCiAg"
    "ICAgICAgc2VsZi51c2VyID0gR0RILnJlZ2lzdGVyUGxheWVyKHVzZXJuYW1lLCBzZWxmLCBwYXNz"
    "d29yZCwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICAgICAg"
    "aWYgc2VsZi51c2VyOgogICAgICAgICAgICByZXR1cm4gMCBpZiBzZWxmLl9jbGFpbVNlc3Npb24o"
    "KSBlbHNlIDEKICAgICAgICByZXR1cm4gMiAjVE9ETyBnZXQgZXJyb3IgZnJvbSBHREgKICAgIGRl"
    "ZiBoYW5kbGUoc2VsZik6CiAgICAgICAgdHJ5OiAjSW50ZXJjZXB0IGFuZCBwcmludCBlcnJvcnMg"
    "Zm9yIGRlYnVnZ2luZwogICAgICAgICAgICBzZWxmLl9oYW5kbGUoKQogICAgICAgICAgICAjVE9E"
    "TyBsb29wIGxvYmJ5IGhhbmRsZSBiZXR0ZXIgdG8gaGFuZGxlIGV4Y2VwdGlvbnMgZ3JhY2VmdWxs"
    "eQogICAgICAgICAgICBzZWxmLl9sb2JieUhhbmRsZSgpCiAgICAgICAgZXhjZXB0IFByb3RvY29s"
    "RXJyb3IgYXMgZToKICAgICAgICAgICAgI21hbGZvcm1lZC9vdmVyc2l6ZWQgaW5wdXQgLSB0aGUg"
    "Y2xpZW50J3MgZmF1bHQsIG5vdCBvdXJzLiBEcm9wIHRoZQogICAgICAgICAgICAjY29ubmVjdGlv"
    "biB3aXRoIG9uZSBsaW5lIGluc3RlYWQgb2YgYSB0cmFjZWJhY2suCiAgICAgICAgICAgIHdobyA9"
    "IHNlbGYudXNlci5uYW1lIGlmIHNlbGYudXNlciBlbHNlIHNlbGYuY2xpZW50X2FkZHJlc3NbMF0K"
    "ICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIFByb3RvY29sIGVycm9yIGZyb20ge3dob306IHtl"
    "fScpCiAgICAgICAgZXhjZXB0ICh6bGliLmVycm9yLCBzdHJ1Y3QuZXJyb3IsIFVuaWNvZGVEZWNv"
    "ZGVFcnJvcikgYXMgZToKICAgICAgICAgICAgI3RydW5jYXRlZC9nYXJiYWdlIHBhY2tldDogcGFy"
    "c2VEc3RyIGFuZCBzdHJ1Y3QudW5wYWNrIGJvdGggcmFpc2Ugb24KICAgICAgICAgICAgI3Nob3J0"
    "IHJlYWRzLCBhbmQgLmRlY29kZSgpIG9uIG5vbi1hc2NpaSBqdW5rLiBTYW1lIGNhdGVnb3J5Lgog"
    "ICAgICAgICAgICBwcmludChmJ1tMb2JieV0gTWFsZm9ybWVkIHBhY2tldCBmcm9tIHtzZWxmLmNs"
    "aWVudF9hZGRyZXNzWzBdfTogJwogICAgICAgICAgICAgICAgICBmJ3t0eXBlKGUpLl9fbmFtZV9f"
    "fToge2V9JykKICAgICAgICBleGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25uZWN0aW9u"
    "QWJvcnRlZEVycm9yLCBPU0Vycm9yKSBhcyBlOgogICAgICAgICAgICAjIGV4cGVjdGVkIGZvcm0g"
    "b2YgZGlzY29ubmVjdGlvbiAoaW5jbHVkaW5nIGEgZm9yY2VkIGFkbWluIGtpY2spLAogICAgICAg"
    "ICAgICAjIGJ1dCBsZWF2ZSBhIG9uZS1saW5lIGJyZWFkY3J1bWIgcmF0aGVyIHRoYW4gc3RheWlu"
    "ZyBmdWxseSBzaWxlbnQKICAgICAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICAgICAg"
    "cHJpbnQoZidbTG9iYnldIENvbm5lY3Rpb24gY2xvc2VkIGZvciB7c2VsZi51c2VyLm5hbWV9OiB7"
    "ZX0nKQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246IyBhcyBlOgogICAgICAgICAgICBwcmludCh0"
    "cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAg"
    "ICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFtZX0nKQogICAgICAgICAgICAjcmFp"
    "c2UgZQogICAgZGVmIF9sb2JieUhhbmRsZShzZWxmKToKICAgICAgICAjYWN0aXZlVXNlcnNbLi4u"
    "XSA9IHNlbGYgdXNlZCB0byBoYXBwZW4gaGVyZTsgaXQgbm93IGhhcHBlbnMgdW5kZXIgYQogICAg"
    "ICAgICNsb2NrIGluc2lkZSBhdHRlbXB0TG9naW4vYXR0ZW1wdFJlZ2lzdGVyLCBiZWZvcmUgdGhl"
    "IHdlbGNvbWUgcGFja2V0CiAgICAgICAgI2dvZXMgb3V0LCBzbyB0d28gbG9naW5zIGZvciBvbmUg"
    "YWNjb3VudCBjYW4ndCBib3RoIHN1Y2NlZWQuCiAgICAgICAgcHJpbnQoZidVc2VyOiB7c2VsZi51"
    "c2VyLm5hbWV9IENvbm5lY3RlZCcpCiAgICAgICAgI0Zyb20gaGVyZSBvbiBub3RoaW5nIHdyaXRl"
    "cyB0byB0aGUgc29ja2V0IGlubGluZTogdGhlIHdyaXRlciB0aHJlYWQKICAgICAgICAjb3ducyB0"
    "aGUgb3V0Ym91bmQgZGlyZWN0aW9uIGFuZCB0aGlzIGxvb3Agb25seSByZWFkcy4KICAgICAgICBz"
    "ZWxmLl9zdGFydFdyaXRlcigpCiAgICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25p"
    "YygpCiAgICAgICAgI1RoZSBzb2NrZXQgc3RheXMgaW4gYmxvY2tpbmcgbW9kZSBmb3IgaXRzIHdo"
    "b2xlIGxpZmUgZnJvbSBoZXJlIG9uLCBhbmQKICAgICAgICAjcmVhZGluZXNzIGlzIHdhaXRlZCBm"
    "b3Igd2l0aCBzZWxlY3QoKSBpbnN0ZWFkIG9mIGEgc29ja2V0IHRpbWVvdXQuCiAgICAgICAgI1Ro"
    "aXMgaXMgbm90IGEgc3R5bGUgcHJlZmVyZW5jZSAtIGEgc29ja2V0IHRpbWVvdXQgaXMgYSBwcm9w"
    "ZXJ0eSBvZiB0aGUKICAgICAgICAjKnNvY2tldCosIG5vdCBvZiB0aGUgY2FsbCwgc28gdGhlIHNl"
    "dHRpbWVvdXQoX1JFQURfVElNRU9VVCkgdGhpcyBsb29wCiAgICAgICAgI3VzZWQgdG8gZG8gb24g"
    "ZXZlcnkgcGFzcyBhbHNvIGFybWVkIGEgMXMgdGltZW91dCBvbiB0aGUgd3JpdGVyCiAgICAgICAg"
    "I3RocmVhZCdzIGNvbmN1cnJlbnQgc2VuZGFsbCgpLiBBIGNsaWVudCB3aG9zZSByZWNlaXZlIHdp"
    "bmRvdyB3YXMgZnVsbAogICAgICAgICNmb3IgYSBzZWNvbmQgKGV4YWN0bHkgdGhlIGNhc2UgZHVy"
    "aW5nIGEgYnVzeSBjby1vcCBzZXNzaW9uKSBtYWRlIHRoYXQKICAgICAgICAjc2VuZGFsbCgpIHJh"
    "aXNlIFRpbWVvdXRFcnJvciAqYWZ0ZXIgaGF2aW5nIGFscmVhZHkgd3JpdHRlbiBwYXJ0IG9mIHRo"
    "ZQogICAgICAgICNwYWNrZXQqOiB0aGUgd3JpdGVyIHRocmVhZCBkaWVkLCBhbmQgd2hhdGV2ZXIg"
    "dGhlIGNsaWVudCBoYWQgcmVjZWl2ZWQKICAgICAgICAjd2FzIGhhbGYgYSBtZXNzYWdlLCBzbyBp"
    "dHMgY29tbWFuZCBzdHJlYW0gd2FzIGRlc3luY2hyb25pc2VkIGZyb20KICAgICAgICAjdGhhdCBw"
    "b2ludCBvbi4gc2VsZWN0KCkgbGVhdmVzIHRoZSBzb2NrZXQgYmxvY2tpbmcsIHNvIHdyaXRlcyBh"
    "cmUKICAgICAgICAjbmV2ZXIgaW50ZXJydXB0ZWQsIHdoaWxlIHJlYWRzIHN0aWxsIHdha2UgdXAg"
    "cmVndWxhcmx5IGVub3VnaCB0bwogICAgICAgICNub3RpY2Ugc2h1dGRvd24gYW5kIHRoZSBpZGxl"
    "IGRlYWRsaW5lLgogICAgICAgIHNlbGYucmVxdWVzdC5zZXR0aW1lb3V0KE5vbmUpCiAgICAgICAg"
    "d2hpbGUgVHJ1ZToKICAgICAgICAgICAgaWYgc2VsZi5fd3JpdGVyRGVhZC5pc19zZXQoKToKICAg"
    "ICAgICAgICAgICAgIGJyZWFrICNwZWVyIHdlbnQgYXdheSB3aGlsZSB3ZSB3ZXJlIHNlbmRpbmcK"
    "ICAgICAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuX2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAgICBi"
    "cmVhayAjc2VydmVyIGlzIHN0b3BwaW5nIC0gY2hlY2tlZCBoZXJlLCBub3Qgb25seSBvbiBhbiBp"
    "ZGxlCiAgICAgICAgICAgICAgICAgICAgICAjdGltZW91dCwgc28gYSBjbGllbnQgdGhhdCBrZWVw"
    "cyB0YWxraW5nIGNhbm5vdCBrZWVwIGl0cwogICAgICAgICAgICAgICAgICAgICAgI2hhbmRsZXIg"
    "dGhyZWFkIChhbmQgaXRzIGxvZyBzcGFtKSBhbGl2ZSBwYXN0IHNodXRkb3duCiAgICAgICAgICAg"
    "IHRyeToKICAgICAgICAgICAgICAgIHJlYWR5LCBfLCBfID0gc2VsZWN0LnNlbGVjdChbc2VsZi5y"
    "ZXF1ZXN0XSwgW10sIFtdLCBfUkVBRF9USU1FT1VUKQogICAgICAgICAgICBleGNlcHQgKE9TRXJy"
    "b3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAgICAgICAgYnJlYWsgI3NvY2tldCBjbG9zZWQgdW5k"
    "ZXIgdXMgKGFkbWluIGtpY2sgLyBzaHV0ZG93bikKICAgICAgICAgICAgaWYgbm90IHJlYWR5Ogog"
    "ICAgICAgICAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuX2lzX2Nsb3Npbmc6CiAgICAgICAgICAgICAg"
    "ICAgICAgYnJlYWsgI1NlcnZlciBTaHV0dGluZyBkb3duCiAgICAgICAgICAgICAgICBpZiBfSURM"
    "RV9USU1FT1VUIGFuZCAodGltZS5tb25vdG9uaWMoKSAtIHNlbGYuX2xhc3RSZWN2KSA+IF9JRExF"
    "X1RJTUVPVVQ6CiAgICAgICAgICAgICAgICAgICAgI0hhbGYtb3BlbiBjb25uZWN0aW9uOiB0aGUg"
    "cGVlciBpcyB1bnJlYWNoYWJsZSBidXQgbmV2ZXIKICAgICAgICAgICAgICAgICAgICAjc2VudCBh"
    "IEZJTi9SU1QsIHNvIHJlY3YoKSBibG9ja3MgZm9yZXZlciBhbmQgdGhlIGFjY291bnQKICAgICAg"
    "ICAgICAgICAgICAgICAjc3RheXMgY2xhaW1lZC4gUmVhcCBpdCBzbyB0aGUgcGxheWVyIGNhbiBs"
    "b2cgYmFjayBpbi4KICAgICAgICAgICAgICAgICAgICBwcmludChmJ1tMb2JieV0ge3NlbGYudXNl"
    "ci5uYW1lfSBpZGxlIGZvciB7X0lETEVfVElNRU9VVH1zLCBkcm9wcGluZycpCiAgICAgICAgICAg"
    "ICAgICAgICAgYnJlYWsKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHJtc2cg"
    "PSBzZWxmLnJlcXVlc3QucmVjdihSRUNWX0JVRl9MRU4pICNUT0RPIGxvZyBuZXR3b3JrIGJ5dGVy"
    "YXRlCiAgICAgICAgICAgIGlmIG5vdCBybXNnOgogICAgICAgICAgICAgICAgYnJlYWsgI0Rpc2Nv"
    "bm5lY3RlZAogICAgICAgICAgICBzZWxmLmRhdGErPXJtc2cKICAgICAgICAgICAgc2VsZi5fbGFz"
    "dFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgICAgIHdoaWxlIHNlbGYuZGF0YToKICAg"
    "ICAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgICAgICBjbWRfbCA9IHNlbGYuZGF0YS5p"
    "bmRleCgwKQogICAgICAgICAgICAgICAgZXhjZXB0IFZhbHVlRXJyb3I6CiAgICAgICAgICAgICAg"
    "ICAgICAgI3ByaW50KCdjbWQgZGVjb2RlIGVycm9yOlxuJywgdHJhY2ViYWNrLmZvcm1hdF9leGMo"
    "KSkKICAgICAgICAgICAgICAgICAgICBicmVhazsjTWF5IHJlcXVpcmUgbW9yZSBkYXRhCiAgICAg"
    "ICAgICAgICAgICBjbWQgPSB3aXJlX2RlY29kZShzZWxmLmRhdGFbMDpjbWRfbF0pCiAgICAgICAg"
    "ICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFbY21kX2wrMTpdCiAgICAgICAgICAgICAgICBy"
    "ZXNwb25zZSA9IHNlbGYuc2VydmVyLmNvbXBhcnMucGFyc2UoY21kLCBzZWxmKQogICAgICAgICAg"
    "ICAgICAgaWYgcmVzcG9uc2U6CiAgICAgICAgICAgICAgICAgICAgI1F1ZXVlZCByYXRoZXIgdGhh"
    "biBzZW50IGlubGluZSwgc28gdGhpcyBjb25uZWN0aW9uIGhhcyBhCiAgICAgICAgICAgICAgICAg"
    "ICAgI3NpbmdsZSBvcmRlcmVkIG91dGJvdW5kIHN0cmVhbS4gU2VuZGluZyBoZXJlIGRpcmVjdGx5"
    "CiAgICAgICAgICAgICAgICAgICAgI3dvdWxkIHJhY2UgdGhlIHdyaXRlciB0aHJlYWQgYW5kIGNv"
    "dWxkIGxhbmQgaW4gdGhlIG1pZGRsZQogICAgICAgICAgICAgICAgICAgICNvZiBhIGJyb2FkY2Fz"
    "dCBpdCBpcyBhbHJlYWR5IHdyaXRpbmcuCiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kKHJl"
    "c3BvbnNlKQogICAgICAgICAgICAgICAgI0xvb3NlIGJsb2JzIHNob3VsZCBub3QgaGFwcGVuIGFu"
    "eW1vcmUgaG9wZWZ1bGx5CiAgICAgICAgICAgICAgICAjVE9ETyBmaXggdW5jb21wcmVzc2VkIGRh"
    "dGEgYmxvYnM/CiAgICAgICAgICAgICAgICAjVE9ETyBza2lwIDEgYnl0ZSBvbmx5IHdoZW4gZGVj"
    "b2RlIGVycm9yPwogICAgICAgICAgICAgICAgaWYgKGxlbihzZWxmLmRhdGEpPjIgYW5kCiAgICAg"
    "ICAgICAgICAgICAgICAgICAgIHNlbGYuZGF0YVswXT09MHg3OCBhbmQKICAgICAgICAgICAgICAg"
    "ICAgICAgICAgc2VsZi5kYXRhWzFdPT0weDljKToKICAgICAgICAgICAgICAgICAgICAjTG9vc2Ug"
    "dW5oYW5kbGVkIGJsb2IgYWZ0ZXIgY29tbWFuZAogICAgICAgICAgICAgICAgICAgIGJsb2IsIHNl"
    "bGYuZGF0YSA9IHBfZ2V0QmxvYihzZWxmLmRhdGEsIHNlbGYucmVxdWVzdCkKICAgICAgICAgICAg"
    "ICAgICAgICAjVGhlIG90aGVyIGJsaW5kIHNwb3Q6IGFueXRoaW5nIHRoZSBjbGllbnQgc2VuZHMg"
    "YXMgYQogICAgICAgICAgICAgICAgICAgICNjb21wcmVzc2VkIGJsb2IgcmF0aGVyIHRoYW4gYSB0"
    "ZXh0IGNvbW1hbmQgd2FzIHJlYWQgYW5kCiAgICAgICAgICAgICAgICAgICAgI3Rocm93biBhd2F5"
    "IHdpdGhvdXQgYSB0cmFjZS4KICAgICAgICAgICAgICAgICAgICBpZiBfREVCVUdfTE9HX0NPTU1B"
    "TkRTOgogICAgICAgICAgICAgICAgICAgICAgICB3aG8gPSBzZWxmLnVzZXIubmFtZSBpZiBzZWxm"
    "LnVzZXIgZWxzZSAnPycKICAgICAgICAgICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hv"
    "fSAtPiAoVU5IQU5ETEVEIEJMT0IgYWZ0ZXIge2NtZCFyfSkgJwogICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICBmJ3tsZW4oYmxvYil9IGJ5dGVzJykKICAgIGRlZiBfcmVjdk1vcmUoc2VsZik6"
    "CiAgICAgICAgY2h1bmsgPSBzZWxmLnJlcXVlc3QucmVjdihSRUNWX0JVRl9MRU4pCiAgICAgICAg"
    "aWYgbm90IGNodW5rOgogICAgICAgICAgICAjcGVlciBkaXNjb25uZWN0ZWQgZHVyaW5nIGhhbmRz"
    "aGFrZS9sb2dpbiwgc3RvcCB0aGUgYnVzeS1sb29wCiAgICAgICAgICAgIHJhaXNlIENvbm5lY3Rp"
    "b25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGxvZ2luJykKICAgICAgICBzZWxmLmRh"
    "dGEgKz0gY2h1bmsKICAgIGRlZiBfaGFuZGxlKHNlbGYpOgogICAgICAgICNUT0RPIGxvZyBsb2dp"
    "biBhdHRlbXB0cz8KICAgICAgICBwZWVyX2lwID0gc2VsZi5jbGllbnRfYWRkcmVzc1swXQogICAg"
    "ICAgIHByaW50KCdDb25uZWN0aW9uIGF0dGVtcHQgZnJvbTonLCBwZWVyX2lwKQogICAgICAgIExJ"
    "UyA9IDIgI2xvZ2luIHN0YXRlICNUT0RPIGNvbnNpZGVyIGxvbmcgdGltZW91dHM/CiAgICAgICAg"
    "d2hpbGUgTElTOgogICAgICAgICAgICB3aGlsZSBsZW4oc2VsZi5kYXRhKTw0OgogICAgICAgICAg"
    "ICAgICAgc2VsZi5fcmVjdk1vcmUoKQogICAgICAgICAgICBwYWNrX2xlbiA9IHN0cnVjdC51bnBh"
    "Y2soJzxJJyxzZWxmLmRhdGFbMDo0XSlbMF0KICAgICAgICAgICAgaWYgcGFja19sZW4gPCA0IG9y"
    "IHBhY2tfbGVuID4gX01BWF9IQU5EU0hBS0U6CiAgICAgICAgICAgICAgICAjdW52YWxpZGF0ZWQs"
    "IHRoaXMgaXMgYSBwcmUtYXV0aGVudGljYXRpb24gbWVtb3J5IGJvbWI6IGFuCiAgICAgICAgICAg"
    "ICAgICAjdW5hdXRoZW50aWNhdGVkIHBlZXIgYW5ub3VuY2VzIGEgNEdCIHBhY2tldCBhbmQgdGhl"
    "IGxvb3AgYmVsb3cKICAgICAgICAgICAgICAgICNidWZmZXJzIHVudGlsIHRoZSBwcm9jZXNzIGRp"
    "ZXMKICAgICAgICAgICAgICAgIHJhaXNlIFByb3RvY29sRXJyb3IoZidoYW5kc2hha2UgcGFja2V0"
    "IGxlbmd0aCB7cGFja19sZW59IG91dCBvZiByYW5nZScpCiAgICAgICAgICAgIHdoaWxlKGxlbihz"
    "ZWxmLmRhdGEpPHBhY2tfbGVuKToKICAgICAgICAgICAgICAgIHNlbGYuX3JlY3ZNb3JlKCkKICAg"
    "ICAgICAgICAgI3NsaWNlIHRvIHBhY2tfbGVuIChub3QgdG8gdGhlIGVuZCBvZiB0aGUgYnVmZmVy"
    "KTogYW55dGhpbmcgcGFzdAogICAgICAgICAgICAjdGhpcyBwYWNrZXQgYmVsb25ncyB0byB0aGUg"
    "bmV4dCBvbmUuIEJvdW5kZWQgZGVjb21wcmVzcywgYmVjYXVzZSBhCiAgICAgICAgICAgICM2NGsg"
    "aGFuZHNoYWtlIG9mIGNvbXByZXNzZWQgemVyb2VzIGV4cGFuZHMgdG8gaHVuZHJlZHMgb2YgTUIu"
    "CiAgICAgICAgICAgIHJlcyA9IF9kZWNvbXByZXNzX2JvdW5kZWQoc2VsZi5kYXRhWzQ6cGFja19s"
    "ZW5dLCBfTUFYX0hBTkRTSEFLRV9JTkZMQVRFRCkKICAgICAgICAgICAgc2VsZi5kYXRhID0gc2Vs"
    "Zi5kYXRhW3BhY2tfbGVuOl0KICAgICAgICAgICAgaWYgTElTID09IDI6CiAgICAgICAgICAgICAg"
    "ICBnYW1ldmVyc2lvbiA9IHJlc1swOjE2XSAjVE9ETyBub3RlIGdhbWUgdmVyc2lvbiAodW52ZXJp"
    "ZmllZCkgcGVyIHVzZXIKICAgICAgICAgICAgICAgIGxhbmduYW1lLCBvZmYgPSBwYXJzZURzdHIo"
    "cmVzLCAxNikKICAgICAgICAgICAgICAgICNUT0RPIGNvbnNpZGVyIFRXU0UgaW5kaWNhdG9yIHRv"
    "IGNyZWF0ZSBzZWN1cmUgY29ubmVjdGlvbj8KICAgICAgICAgICAgICAgICNUT0RPIGNoZWNrIGlm"
    "IHZhbmlsbGEgc2VydmVyIGlnbm9yZXMgZXh0cmEgZGF0YSBpbiBoYW5kc2hha2UgcHJvY2Vzcwog"
    "ICAgICAgICAgICAgICAgUksgPSByZXNbb2ZmKzg6b2ZmKzE2XQogICAgICAgICAgICAgICAgZm9y"
    "IGkgaW4gcmFuZ2UobGVuKFJLKSk6CiAgICAgICAgICAgICAgICAgICAgc2VsZi5TS1tpXV49Uktb"
    "aV0KICAgICAgICAgICAgICAgICN3YXMgaGFyZGNvZGVkICdUVzFDUycgd2l0aCBhICJTRVJWRVIg"
    "TkFNRSBjZmdUT0RPIiBub3RlOiB0aGUKICAgICAgICAgICAgICAgICNuYW1lIGNvbmZpZ3VyZWQg"
    "aW4gQ29uZmlnLmluaS90aGUgR1VJIHJlYWNoZWQgdGhlIHdlbGNvbWUKICAgICAgICAgICAgICAg"
    "ICNwYWNrZXQgYnV0IG5ldmVyIHRoaXMgb25lLCBzbyB0aGUgcHJlLWxvZ2luIGhhbmRzaGFrZSBh"
    "bHdheXMKICAgICAgICAgICAgICAgICNhbm5vdW5jZWQgdGhlIHBsYWNlaG9sZGVyLgogICAgICAg"
    "ICAgICAgICAgc2VsZi5zZW5kUmF3KF9zZXJ2ZXJfaW5mb19wYWNrZXQoc2FuaXRpemVUZXh0KERF"
    "RkFVTFRfVElUTEUpKSkKICAgICAgICAgICAgICAgICNUT0RPIFRXMUNTIGluZGljYXRvciBmb3Ig"
    "VFdTRSBjbGllbnQgdG8gY3JlYXRlIHNlY3VyZSBjb25uZWN0aW9uIG9yIHByZS1oYXNoIHBhc3N3"
    "b3JkPwogICAgICAgICAgICAgICAgTElTID0gMSAKICAgICAgICAgICAgICAgIHNlbGYuU0sgPSBi"
    "eXRlcyhzZWxmLlNLKQogICAgICAgICAgICBlbGlmIExJUyA9PSAxOgogICAgICAgICAgICAgICAg"
    "bG9naW5FcnJvciA9IC0xCiAgICAgICAgICAgICAgICAjU3RhbGwgcmVwZWF0IG9mZmVuZGVycyBi"
    "ZWZvcmUgZG9pbmcgYW55IFBCS0RGMiB3b3JrIGZvciB0aGVtLgogICAgICAgICAgICAgICAgI1Ns"
    "ZWVwaW5nIGluIHRoaXMgaGFuZGxlciB0aHJlYWQgaXMgdGhlIHBvaW50OiBpdCBjb3N0cyB1cwog"
    "ICAgICAgICAgICAgICAgI25vdGhpbmcgYW5kIHJhdGUtbGltaXRzIHRoYXQgY29ubmVjdGlvbi4K"
    "ICAgICAgICAgICAgICAgIGRlbGF5ID0gTE9HSU5fVEhST1RUTEUuZGVsYXlGb3IocGVlcl9pcCkK"
    "ICAgICAgICAgICAgICAgIGlmIGRlbGF5OgogICAgICAgICAgICAgICAgICAgIHRpbWUuc2xlZXAo"
    "ZGVsYXkpCiAgICAgICAgICAgICAgICB1c2VybmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywgMCkK"
    "ICAgICAgICAgICAgICAgIHBhc3N3b3JkLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAg"
    "ICAgICAgICAgICAjVE9ETyBUV1NFIG1vZCBmb3IgaGlnaGVyIGxvZ2luIHNlY3VyaXR5CiAgICAg"
    "ICAgICAgICAgICAjLWVuY3J5cHRlZCBjb25uZWN0aW9uIHRvIHByZXZlbnQgcmVwbGF5IGF0dGFj"
    "a3MKICAgICAgICAgICAgICAgICMtcHJlaGFzaCBwYXNzd29yZCB3aXRoIHNlcmlhbD8sIGNoZWNr"
    "IGlmIHJlY292ZXJ5IHBvc3NpYmxlLgogICAgICAgICAgICAgICAgc2VsZi5ndWlkID0gYnl0ZXMo"
    "cmVzW29mZjpvZmYrMTZdKQogICAgICAgICAgICAgICAgI3ByaW50KCdndWlkIGJ5dGU6Jywgc2Vs"
    "Zi5ndWlkWzFdKQogICAgICAgICAgICAgICAgI3NlbGYuZ3VpZCA9IGJ5dGVhcnJheShyZXNbb2Zm"
    "Om9mZisxNl0pCiAgICAgICAgICAgICAgICAjc2VsZi5ndWlkWzFdXj0weDE2ICNETyBOT1QgcGVy"
    "Zm9ybSBzZXJ2ZXJzaWRlCiAgICAgICAgICAgICAgICAjc2VsZi5ndWlkID0gYnl0ZXMoc2VsZi5n"
    "dWlkKQogICAgICAgICAgICAgICAgb2ZmKz0xNgogICAgICAgICAgICAgICAgaXNyZWcgPSBzdHJ1"
    "Y3QudW5wYWNrKCc8SScscmVzW29mZjpvZmYrNF0pWzBdCiAgICAgICAgICAgICAgICBvZmYrPTQK"
    "ICAgICAgICAgICAgICAgIHZpYVJlZ2lzdGVyID0gYm9vbChpc3JlZykKICAgICAgICAgICAgICAg"
    "IGlmIGlzcmVnOgogICAgICAgICAgICAgICAgICAgIGVtYWlsLCBvZmYgPSBwYXJzZURzdHIocmVz"
    "LCBvZmYpCiAgICAgICAgICAgICAgICAgICAgbG9jYXRpb24sIG9mZiA9IHBhcnNlRHN0cihyZXMs"
    "IG9mZikKICAgICAgICAgICAgICAgICAgICBhZ2UgPSByZXNbb2ZmXQogICAgICAgICAgICAgICAg"
    "ICAgIGdlbmRlciA9IHJlc1tvZmYrMV0KICAgICAgICAgICAgICAgICAgICBvZmYrPTIgI2FnZSwg"
    "Z2VuZGVyCiAgICAgICAgICAgICAgICAgICAgZGVzY3JpcHRpb24sIG9mZiA9IHBhcnNlRHN0cihy"
    "ZXMsIG9mZikKICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gc2VsZi5hdHRlbXB0UmVn"
    "aXN0ZXIodXNlcm5hbWUsIHBhc3N3b3JkLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBk"
    "ZXNjcmlwdGlvbikKICAgICAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICAgICAgbG9n"
    "aW5FcnJvciA9IHNlbGYuYXR0ZW1wdExvZ2luKHVzZXJuYW1lLCBwYXNzd29yZCkKICAgICAgICAg"
    "ICAgICAgICAgICBpZiBsb2dpbkVycm9yID09IDEgYW5kIF9BVVRPX1JFR0lTVEVSOgogICAgICAg"
    "ICAgICAgICAgICAgICAgICB2aWFSZWdpc3RlciA9IFRydWUKICAgICAgICAgICAgICAgICAgICAg"
    "ICAgbG9naW5FcnJvciA9IHNlbGYuYXR0ZW1wdFJlZ2lzdGVyKHVzZXJuYW1lLCBwYXNzd29yZCwg"
    "IiIsICIiLCAxLCAwLCAiIikKICAgICAgICAgICAgICAgIGlmIGxvZ2luRXJyb3IgPT0gMDoKICAg"
    "ICAgICAgICAgICAgICAgICBMT0dJTl9USFJPVFRMRS5yZWNvcmRTdWNjZXNzKHBlZXJfaXApCiAg"
    "ICAgICAgICAgICAgICAgICAgI1RPRE8gYmV0dGVyIGhhbmRsaW5nIG9mIFRJVExFIEFORCBNT1RE"
    "CiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQo"
    "Ynl0ZXMoc2VsZi5TSyksIERFRkFVTFRfVElUTEUsIERFRkFVTFRfTU9URCkpCiAgICAgICAgICAg"
    "ICAgICAgICAgTElTID0gMAogICAgICAgICAgICAgICAgZWxzZTogI2Vycm9yIGJhc2VkIG9uIGxv"
    "Z2luRXJyb3IgbnVtYmVyCiAgICAgICAgICAgICAgICAgICAgY291bnQgPSBMT0dJTl9USFJPVFRM"
    "RS5yZWNvcmRGYWlsdXJlKHBlZXJfaXApCiAgICAgICAgICAgICAgICAgICAgaWYgY291bnQgPT0g"
    "X0xPR0lOX0ZBSUxfTElNSVQ6CiAgICAgICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5"
    "XSBUaHJvdHRsaW5nIGxvZ2lucyBmcm9tIHtwZWVyX2lwfSAnCiAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgIGYnKHtjb3VudH0gZmFpbHVyZXMgaW4ge19MT0dJTl9GQUlMX1dJTkRPV31zKScp"
    "CiAgICAgICAgICAgICAgICAgICAgZXJybXNncyA9IF9SRUdJU1RFUl9FUlJPUlMgaWYgdmlhUmVn"
    "aXN0ZXIgZWxzZSBfTE9HSU5fRVJST1JTCiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3"
    "KF9pbml0X2Vycm9yKGVycm1zZ3MuZ2V0KGxvZ2luRXJyb3IsICdMb2dpbiBmYWlsZWQnKSkpCiAg"
    "ICBkZWYgZmluaXNoKHNlbGYpOgogICAgICAgIHNlbGYuc2VydmVyLnVucmVnaXN0ZXJDb25uZWN0"
    "aW9uKHNlbGYpCiAgICAgICAgI1N0b3AgdGhlIHdyaXRlciBmaXJzdDogaXQgaG9sZHMgdGhpcyBz"
    "b2NrZXQgYW5kIHdvdWxkIG90aGVyd2lzZSBrZWVwCiAgICAgICAgI3dyaXRpbmcgb24gYmVoYWxm"
    "IG9mIGEgcGxheWVyIHdobyBoYXMgYWxyZWFkeSBsZWZ0IGV2ZXJ5IGNoYW5uZWwuCiAgICAgICAg"
    "c2VsZi5fc3RvcFdyaXRlcigpCiAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICBwcmlu"
    "dChmJ1VzZXI6IHtzZWxmLnVzZXIubmFtZX0gRGlzY29ubmVjdGVkJykKICAgICAgICAgICAgc2Vs"
    "Zi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5zZXJ2ZXIpCiAgICAgICAgI2NsZWFudXAgdXNlciBkYXRh"
    "CiAgICAgICAgI1RPRE8gY2hlY2sgaWYgdHJpZ2dlcmVkIG9uIGNyYXNoZWQgY29ubmVjdGlvbgog"
    "ICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgI1RP"
    "RE8gSVAgZm9yIGVsZXZhdGVkIGF1dGhvcml0eQogICAgICAgICAgICAjJ25hbWUnOnNlbGYudXNl"
    "ci5uYW1lLAogICAgICAgICAgICAnZ2FtZSc6c2VsZi51c2VyLmdhbWUuZ25hbWUgaWYgc2VsZi51"
    "c2VyLmdhbWUgZWxzZSAnJywKICAgICAgICAgICAgJ3Rvd24nOnNlbGYudXNlci5nYW1lY2hhbm5l"
    "bC5uYW1lIGlmIHNlbGYudXNlci5nYW1lY2hhbm5lbCBlbHNlICcnLAogICAgICAgICAgICAncG9z"
    "JzpzZWxmLnVzZXIucG9zZGF0YSBpZiBzZWxmLnVzZXIucG9zZGF0YSBlbHNlICcnLAogICAgICAg"
    "ICAgICAnaWQnOnNlbGYudXNlci5pZG51bSwKICAgICAgICAgICAgJ2xvZ2luVGltZSc6anNvblRp"
    "bWUoc2VsZi51c2VyLmxvZ2luVGltZSkKICAgICAgICB9I1RPRE8gZWxldmF0ZWQgYXV0aG9yaXR5"
    "IHZlcnNpb24KCmRlZiBjbWRfZGVmYXVsdCgpOiNhcmdzKToKICAgICNwcmludChhcmdzKQogICAg"
    "I19yZWFkY29uZmlnKCkKICAgIHNlcnZlciA9IENvcmVTZXJ2ZXIoKQogICAgd2l0aCBzZXJ2ZXI6"
    "CiAgICAgICAgdHN0ID0gc2lnbmFsLnNpZ25hbChzaWduYWwuU0lHSU5ULCBzZXJ2ZXIuaGFuZGxl"
    "X3NpZ25hbCh0aW1lb3V0PTIpKQogICAgICAgICNwcmludCgnQXNzaWduZWQgU2lnbmFsPycsIHRz"
    "dCkKICAgICAgICAjc2lnbmFsLnNpZ25hbChzaWduYWwuU0lHVEVSTSwgc2VydmVyLmhhbmRsZV9z"
    "aWduYWwodGltZW91dD0xKSkKICAgICAgICBzZXJ2ZXIuc2VydmVfZm9yZXZlcigpCgojc2NyaXB0"
    "IGxhdW5jaGVkLCBjaGVjayBhcmd1bWVudHMgYW5kIGNvbmZpZy4gc2V0dXAgdmFyaW91cyBvYmpl"
    "Y3RzCmlmIF9fbmFtZV9fID09ICdfX21haW5fXyc6CiAgICBwcmludCgnSW5pdGlhbGl6aW5nIFNl"
    "cnZlcicpCiAgICBjbWRfZGVmYXVsdCgpCg=="
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
