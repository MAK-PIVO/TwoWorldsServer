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
    def __init__(self, q, orig=None):
        self.q = q
        self.orig = orig
    def write(self, s):
        if s:
            self.q.put(s)
            if self.orig is not None:
                try:
                    self.orig.write(s)
                except Exception:
                    pass
        return len(s)
    def flush(self):
        if self.orig is not None:
            try:
                self.orig.flush()
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
        sys.stdout = QueueWriter(self.log_queue, sys.stdout)
        sys.stderr = QueueWriter(self.log_queue, sys.stderr)

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

        self.log_text = scrolledtext.ScrolledText(f, wrap='word', state='disabled',
                                                    font=('Consolas', 9), background='#111417',
                                                    foreground='#d8dee4', insertbackground='#d8dee4')
        self.log_text.pack(fill='both', expand=True, padx=10, pady=(0, 10))

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.configure(state='disabled')

    #The log is append-only and the lobby server prints a line per connection,
    #per login and per disconnect. Left uncapped, a server running for days
    #grows the Text widget until the whole app crawls, so keep only a trailing
    #window - the full stream still goes to stdout when run from a console.
    _LOG_MAX_LINES = 5000
    _LOG_TRIM_TO = 4000

    def _poll_log(self):
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
    "I2ltcG9ydCBhcmdwYXJzZQppbXBvcnQgY29uZmlncGFyc2VyCiNpbXBvcnQgZ2V0dGV4dAojaW1w"
    "b3J0IGxvZ2dpbmcKaW1wb3J0IHpsaWIKaW1wb3J0IHN0cnVjdAppbXBvcnQgb3MKaW1wb3J0IHJl"
    "CmltcG9ydCBzeXMKaW1wb3J0IGh0dHAuc2VydmVyCmltcG9ydCBzb2NrZXRzZXJ2ZXIKaW1wb3J0"
    "IHNvY2tldAppbXBvcnQganNvbgppbXBvcnQgdGhyZWFkaW5nCmltcG9ydCBzaWduYWwKaW1wb3J0"
    "IHRpbWUKaW1wb3J0IGRhdGV0aW1lCmltcG9ydCBoYXNobGliCmltcG9ydCBzcWxpdGUzCmltcG9y"
    "dCByYW5kb20KaW1wb3J0IHRyYWNlYmFjawpmcm9tIHVybGxpYi5wYXJzZSBpbXBvcnQgdW5xdW90"
    "ZSwgdXJscGFyc2UKZnJvbSBxdWV1ZSBpbXBvcnQgU2ltcGxlUXVldWUKZnJvbSBjb2xsZWN0aW9u"
    "cyBpbXBvcnQgZGVxdWUKCiMjIE1JU0MgVVRJTElUWSBGVU5DVElPTlMKXzMyYml0ID0gMHhGRkZG"
    "RkZGRgpfOGJpdCA9IDB4RkYKX04gPSBiJ1wwJwpfRzY0X0JBU0UgPSBieXRlcyhbCiAgICAweEQy"
    "LCAweDEyLCAweDEzLCAweEQzLCAweDExLCAweEQxLCAweEQwLCAweDEwLCAweEYwLCAweDMwLCAK"
    "ICAgIDB4MzEsIDB4RjEsIDB4MzMsIDB4RjMsIDB4RjIsIDB4MzIsIDB4MzYsIDB4RjYsIDB4Rjcs"
    "IDB4MzcsIAogICAgMHhGNSwgMHgzNSwgMHgzNCwgMHhGNCwgMHgzQywgMHhGQywgMHhGRCwgMHgz"
    "RCwgMHhGRiwgMHgzRiwgCiAgICAweDNFLCAweEZFLCAweEZBLCAweDNBLCAweDNCLCAweEZCLCAw"
    "eDM5LCAweEY5LCAweEY4LCAweDM4LCAKICAgIDB4MjgsIDB4RTgsIDB4RTksIDB4MjksIDB4RUIs"
    "IDB4MkIsIDB4MkEsIDB4RUEsIDB4RUUsIDB4MkUsIAogICAgMHgyRiwgMHhFRiwgMHgyRCwgMHhF"
    "RCwgMHhFQywgMHgyQywgMHhFNCwgMHgyNCwgMHgyNSwgMHhFNSwgCiAgICAweDI3LCAweEU3LCAw"
    "eEU2LCAweDI2XSkKZGVmIF9zdGVwKG51bSk6CiAgICByZXR1cm4gKG51bSoweDM0M0ZEICsgMHgy"
    "NjlFQzMpJl8zMmJpdApkZWYgZ2VuNjQoY29tYmluZWQpOgogICAgb3V0ID0gYnl0ZWFycmF5KDB4"
    "NDApCiAgICBlYnAgPSBlZGkgPSB0bXAgPSAwCiAgICBmb3IgYiBpbiBjb21iaW5lZDoKICAgICAg"
    "ICBlZGkrPSBiK3RtcAogICAgICAgIHRtcF49IGIKICAgICAgICBlYnArPSB0bXAKICAgIGZvciBp"
    "IGluIHJhbmdlKDB4NDApOgogICAgICAgIHJlcyA9IGNvbWJpbmVkWyhlYnAraSklOF0KICAgICAg"
    "ICBvdXRbaV0gPSByZXNeX0c2NF9CQVNFWyhlZGkraSklMHg0MF0KICAgIHJnID0gZWRpK2VicAog"
    "ICAgZm9yIGkgaW4gcmFuZ2UoMHg0MCk6CiAgICAgICAgcmcgPSBfc3RlcChyZykKICAgICAgICBv"
    "dXRbaV1ePSAocmc+PjB4MTApJl84Yml0CiAgICBmb3IgaSBpbiByYW5nZSgweDIwKToKICAgICAg"
    "ICByZyA9IF9zdGVwKHJnKQogICAgICAgIHNBID0gKHJnPj4weDEwKSUweDQwCiAgICAgICAgcmcg"
    "PSBfc3RlcChyZykKICAgICAgICBzQiA9IChyZz4+MHgxMCklMHg0MAogICAgICAgIChvdXRbc0Fd"
    "LCBvdXRbc0JdKSA9IChvdXRbc0JdLCBvdXRbc0FdKQogICAgcmV0dXJuIGJ5dGVzKG91dCkKI1Ro"
    "ZSBsb2JieSBwcm90b2NvbCBpcyBhIDIwMDcgOC1iaXQgcHJvdG9jb2w6IHRoZSBnYW1lIHNlbmRz"
    "IHdoYXRldmVyIGl0cwojbG9jYWxpc2F0aW9uJ3MgY29kZXBhZ2UgcHJvZHVjZXMgYW5kIGV4cGVj"
    "dHMgdGhlIHNhbWUgYnl0ZXMgYmFjay4gVGhpcyBzZXJ2ZXIKI2lzIHJ1biBmb3IgYSBSdXNzaWFu"
    "LXNwZWFraW5nIGdyb3VwIChzZWUgQ0xBVURFLm1kKSwgc28gdGhhdCBjb2RlcGFnZSBpcwojY3Ax"
    "MjUxIC0gYXNjaWktY29tcGF0aWJsZSBmb3IgMHgwMC0weDdGLCBDeXJpbGxpYyBmb3IgdGhlIHJl"
    "c3QuCiNUaGlzIHVzZWQgdG8gYmUgJ2FzY2lpJyBvbiB0aGUgd2F5IG91dCBhbmQgVVRGLTggb24g"
    "dGhlIHdheSBpbiwgd2hpY2ggbWVhbnQgYQojc2luZ2xlIEN5cmlsbGljIGNoYXJhY3RlciBpbiBj"
    "aGF0IGVpdGhlciBmYWlsZWQgdG8gZGVjb2RlIG9yIGZhaWxlZCB0byBlbmNvZGUuCiNFaXRoZXIg"
    "d2F5IHRoZSBleGNlcHRpb24gcHJvcGFnYXRlZCBvdXQgb2YgdGhlIGNvbW1hbmQgaGFuZGxlciBh"
    "bmQgZHJvcHBlZCB0aGUKI2Nvbm5lY3Rpb246IG9uIGEgUnVzc2lhbiBzZXJ2ZXIsIHR5cGluZyBp"
    "biBjaGF0IGRpc2Nvbm5lY3RlZCB5b3UuCiMobGF0aW4tMSB3b3VsZCBiZSBieXRlLXRyYW5zcGFy"
    "ZW50IGZvciBhcmJpdHJhcnkgYWxyZWFkeS1lbmNvZGVkIGJ5dGVzIGNvbWluZwojb2ZmIHRoZSB3"
    "aXJlLCBidXQgaXQgY2FuJ3QgcmVwcmVzZW50IEN5cmlsbGljICpVbmljb2RlKiB0ZXh0IGF0IGFs"
    "bCAtIGEKI0N5cmlsbGljIE1PVEQgdHlwZWQgaW50byB0aGUgR1VJIHdvdWxkIHNpbGVudGx5IHR1"
    "cm4gaW50byAnPydzIG9uIGVuY29kZS4gQQojbmFtZWQgY29kZXBhZ2UgdGhhdCBtYXRjaGVzIHRo"
    "ZSBhY3R1YWwgcGxheWVycyBpcyB0aGUgb25seSBjaG9pY2UgdGhhdCBpcwojY29ycmVjdCBpbiBi"
    "b3RoIGRpcmVjdGlvbnMuKQpfV0lSRV9FTkMgPSAnY3AxMjUxJwpkZWYgd2lyZV9lbmNvZGUodGV4"
    "dCk6CiAgICByZXR1cm4gdGV4dC5lbmNvZGUoX1dJUkVfRU5DLCAncmVwbGFjZScpCmRlZiB3aXJl"
    "X2RlY29kZShkYXRhKToKICAgIHJldHVybiBieXRlcyhkYXRhKS5kZWNvZGUoX1dJUkVfRU5DKQpk"
    "ZWYgbWFrZURzdHIodGV4dCk6CiAgICB0ZXh0ID0gd2lyZV9lbmNvZGUodGV4dCkKICAgIHRleHRs"
    "ZW4gPSBsZW4odGV4dCkKICAgIHJldHVybiBzdHJ1Y3QucGFjaygnPEl7fXMnLmZvcm1hdCh0ZXh0"
    "bGVuKSwgdGV4dGxlbiwgdGV4dCkKZGVmIHBhcnNlRHN0cihkYXRhLCBvZmYpOgogICAgW3N0cmxl"
    "bl0gPSBzdHJ1Y3QudW5wYWNrKCc8SScsIGRhdGFbb2ZmOm9mZis0XSkKICAgIG9mZis9IDQgKyBz"
    "dHJsZW4KICAgIHRleHQgPSB3aXJlX2RlY29kZShkYXRhW29mZi1zdHJsZW46IG9mZl0pCiAgICBy"
    "ZXR1cm4gdGV4dCwgb2ZmCmRlZiBfc2VydmVyX2luZm9fcGFja2V0KHNlcnZlcm5hbWUpOgogICAg"
    "bm0gPSBmJysie3NlcnZlcm5hbWV9IiJUV01QMjsxMC4wLjAuNSInCiAgICBkZXRzID0gc3RydWN0"
    "LnBhY2soJzxJJywwKSArIG1ha2VEc3RyKG5tKQogICAgY2RldHMgPSB6bGliLmNvbXByZXNzKGRl"
    "dHMpCiAgICByZXR1cm4gc3RydWN0LnBhY2soJzxJJyxsZW4oY2RldHMpKzQpICsgY2RldHMKZGVm"
    "IF9pbml0X2Vycm9yKG1zZz0nVW5rbm93biBlcnJvcicpOgogICAgZXJyID0gc3RydWN0LnBhY2so"
    "JzxJJywxKQogICAgZGV0cyA9IGInJy5qb2luKFtlcnIsIG1ha2VEc3RyKG1zZyldKQogICAgY2Rl"
    "dHMgPSB6bGliLmNvbXByZXNzKGRldHMpCiAgICBwYWNrbGVuID0gc3RydWN0LnBhY2soJzxJJyxs"
    "ZW4oY2RldHMpKzQpCiAgICByZXR1cm4gcGFja2xlbitjZGV0cwpkZWYgX3NlcnZlcl93ZWxjb21l"
    "X3BhY2tldChzZXJpYWwsIHRpdGxlLCBtb3RkKToKICAgIHVua0EgPSBieXRlcyhbMCwwLDAsMCwg"
    "MHg1NSwgMHhhNiwgMHhkOCwgMHgzYl0pCiAgICB1bmtCID0gYnl0ZXMoWzBdKjQ5KQogICAgdW5r"
    "Qis9IGdlbjY0KHNlcmlhbCkKICAgIHNlZWQgPSAwCiAgICBncnAgPSBfZ3JwKHNlZWQpCiAgICB1"
    "bmtCKz0gc3RydWN0LnBhY2soJzw2SScsMCxzZWVkLCpncnApCiAgICBkZXRzID0gYicnLmpvaW4o"
    "W3Vua0EsIG1ha2VEc3RyKHRpdGxlKSwgbWFrZURzdHIobW90ZCksIHVua0JdKQogICAgY2RldHMg"
    "PSB6bGliLmNvbXByZXNzKGRldHMpCiAgICBwYWNrbGVuID0gc3RydWN0LnBhY2soJzxJJyxsZW4o"
    "Y2RldHMpKzQpCiAgICByZXR1cm4gcGFja2xlbitjZGV0cwpkZWYgX2dycChzZWVkPTApOgogICAg"
    "I25vdCBzdXJlIGlmIGl0IG1hdHRlcnMsIHNob3VsZCBnZW5lcmF0ZSBmcm9tIHNlZWQ/IHNlZW1z"
    "IGZpbmUKICAgIHJldHVybiAoMTE1MzcyMTY0OCw0MDkxNTE5OTcsMTU0MzM4NzAzNSwxODEwMzA5"
    "MzEzKQpkZWYgX2djaG5sKG5hbWUsIGluZGV4KToKICAgIHJldHVybiBmJ3tuYW1lfSN0cmFuc2xh"
    "dGV7bmFtZX1fQ2hhbm5lbF97aW5kZXg6MDJkfScKZGVmIHBfZ2V0QmxvYihkYXRhLCBjb24pOgog"
    "ICAgZGNtcCA9IHpsaWIuZGVjb21wcmVzc29iaigpCiAgICBkY21wLmRlY29tcHJlc3MoZGF0YSkK"
    "ICAgIGNkYXRzID0gW2RhdGFdCiAgICB0b3RhbCA9IGxlbihkYXRhKQogICAgY29uLnNldHRpbWVv"
    "dXQoTm9uZSkKICAgIHdoaWxlIG5vdCBkY21wLmVvZjoKICAgICAgICBjZGF0ID0gY29uLnJlY3Yo"
    "UkVDVl9CVUZfTEVOKQogICAgICAgIGlmIG5vdCBjZGF0OgogICAgICAgICAgICAjcGVlciB2YW5p"
    "c2hlZCBtaWQtYmxvYjogcmVjdigpIGtlZXBzIHJldHVybmluZyBiJycgaW5zdGFudGx5LCBzbwog"
    "ICAgICAgICAgICAjd2l0aG91dCB0aGlzIHRoZSBsb29wIHNwaW5zIGF0IDEwMCUgQ1BVIGZvcmV2"
    "ZXIgKHNhbWUgZGVmZWN0IHRoYXQKICAgICAgICAgICAgI3dhcyBhbHJlYWR5IGZpeGVkIGluIENv"
    "bm5lY3Rpb25IYW5kbGVyLl9yZWN2TW9yZSkKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJl"
    "c2V0RXJyb3IoJ2Rpc2Nvbm5lY3RlZCBkdXJpbmcgYmxvYiByZWFkJykKICAgICAgICB0b3RhbCAr"
    "PSBsZW4oY2RhdCkKICAgICAgICBpZiB0b3RhbCA+IF9NQVhfQkxPQjoKICAgICAgICAgICAgcmFp"
    "c2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoZidibG9iIGV4Y2VlZHMge19NQVhfQkxPQn0gYnl0ZXMn"
    "KQogICAgICAgIGNkYXRzLmFwcGVuZChjZGF0KQogICAgICAgIGRjbXAuZGVjb21wcmVzcyhjZGF0"
    "KQogICAgaWYgbGVuKGRjbXAudW51c2VkX2RhdGEpOgogICAgICAgIGNkYXRzWy0xXT1jZGF0c1st"
    "MV1bOi1sZW4oZGNtcC51bnVzZWRfZGF0YSldCiAgICBmY2JsID0gYicnLmpvaW4oY2RhdHMpCiAg"
    "ICByZXR1cm4gZmNibCwgZGNtcC51bnVzZWRfZGF0YQpkZWYgcHJldHR5X2d1aWQoZ3VpZCk6CiAg"
    "ICAoYSxiLGMsZCkgPSBzdHJ1Y3QudW5wYWNrKCI8SUhIOHMiLCBndWlkKQogICAgZGEgPSAnJwog"
    "ICAgZGIgPSAnJwogICAgZm9yIGkgaW4gZFswOjJdOgogICAgICAgIGRhKz0nezowMnh9Jy5mb3Jt"
    "YXQoaSkKICAgIGZvciBpIGluIGRbMjpdOgogICAgICAgIGRiKz0nezowMnh9Jy5mb3JtYXQoaSkK"
    "ICAgIHJldHVybiAnezowOHh9LXs6MDR4fS17OjA0eH0te30te30nLmZvcm1hdChhLGIsYyxkYSxk"
    "YikKZGVmIF9lbShtc2cpOgogICAgcmV0dXJuIHdpcmVfZW5jb2RlKG1zZykrX04KZGVmIF9kZWNv"
    "bXByZXNzX2JvdW5kZWQoZGF0YSwgbGltaXQpOgogICAgI3psaWIuZGVjb21wcmVzcygpIHdpdGgg"
    "bm8gY2FwIHR1cm5zIGEgc21hbGwgY29tcHJlc3NlZCBwYWNrZXQgaW50byBhbgogICAgI2FyYml0"
    "cmFyaWx5IGxhcmdlIGFsbG9jYXRpb24gKHppcCBib21iKS4gbWF4X2xlbmd0aCBzdG9wcyBhdCB0"
    "aGUgY2FwLCBhbmQKICAgICNhIG5vbi1lbXB0eSB1bmNvbnN1bWVkX3RhaWwgdGVsbHMgdXMgdGhl"
    "IHJlYWwgcGF5bG9hZCB3YXMgYmlnZ2VyLgogICAgZGNtcCA9IHpsaWIuZGVjb21wcmVzc29iaigp"
    "CiAgICBvdXQgPSBkY21wLmRlY29tcHJlc3MoZGF0YSwgbGltaXQpCiAgICBpZiBkY21wLnVuY29u"
    "c3VtZWRfdGFpbDoKICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnZGVjb21wcmVzc2VkIHBh"
    "eWxvYWQgZXhjZWVkcyB7bGltaXR9IGJ5dGVzJykKICAgIHJldHVybiBvdXQKY2xhc3MgUHJvdG9j"
    "b2xFcnJvcihFeGNlcHRpb24pOgogICAgI0NsaWVudCBzZW50IHNvbWV0aGluZyBtYWxmb3JtZWQg"
    "b3Igb3V0IG9mIHJhbmdlLiBOb3QgYSBzZXJ2ZXIgZmF1bHQ6IHRoZQogICAgI2Nvbm5lY3Rpb24g"
    "aXMgZHJvcHBlZCB3aXRoIGEgb25lLWxpbmUgbG9nIGluc3RlYWQgb2YgYSB0cmFjZWJhY2suCiAg"
    "ICBwYXNzCl9SRV9WQUxJRF9VU0VSTkFNRSA9IHJlLmNvbXBpbGUocideW0EtWmEtejAtOV9cLV17"
    "MywzMn0kJykKZGVmIHNhbml0aXplVGV4dCh0ZXh0KToKICAgICNzdHJpcCBjaGFyYWN0ZXJzIHRo"
    "YXQgd291bGQgYnJlYWsgdGhlIHF1b3RlZC1zdHJpbmcgYmFzZWQgbG9iYnkgcHJvdG9jb2wKICAg"
    "ICNvciBhbGxvdyBhIGNsaWVudCB0byBmb3JnZSBhZGRpdGlvbmFsIHByb3RvY29sIGZpZWxkcyAo"
    "cHJvdG9jb2wgaW5qZWN0aW9uKQogICAgaWYgdGV4dCBpcyBOb25lOgogICAgICAgIHJldHVybiAn"
    "JwogICAgcmV0dXJuIHRleHQucmVwbGFjZSgnIicsICInIikucmVwbGFjZSgnXDAnLCAnJykucmVw"
    "bGFjZSgnXHInLCAnJykucmVwbGFjZSgnXG4nLCAnICcpCmRlZiBqc29uVGltZShkdCk6CiAgICBp"
    "ZiBub3QgZHQudXRjb2Zmc2V0KCk6CiAgICAgICAgdHppbmZvID0gZGF0ZXRpbWUuZGF0ZXRpbWUu"
    "bm93KGRhdGV0aW1lLnRpbWV6b25lLnV0YykuYXN0aW1lem9uZSgpLnR6aW5mbwogICAgICAgIGR0"
    "ID0gZHQucmVwbGFjZSh0emluZm89dHppbmZvKQogICAgZHQgPSBkdC5hc3RpbWV6b25lKGRhdGV0"
    "aW1lLnRpbWV6b25lLnV0YykucmVwbGFjZSh0emluZm89Tm9uZSkKICAgIHJldHVybiBkdC5pc29m"
    "b3JtYXQoKSArICJaIgogICAgI3Nob3VsZCByZXR1cm4gMjAxMi0wNC0yM1QxODoyNTo0My41MTFa"
    "IHV0YyB0aW1lIGZvciBqYXZhc2NyaXB0IHBhcnNpbmcKCiMjIE1BSU4gU0VSVkVSIENPREUKClJF"
    "Q1ZfQlVGX0xFTiA9IDIqKjEyCgojZ2V0dGV4dC5iaW5kdGV4dGRvbWFpbignVFcxQ1MnLCdsb2Nh"
    "bGUnKQojZ2V0dGV4dC50ZXh0ZG9tYWluKCdUVzFDUycpCgojbG9nZ2VyID0gbG9nZ2luZy5nZXRM"
    "b2dnZXIoX19uYW1lX18pCiNfZmlsZWxvZyA9IEZhbHNlICMgdGltZWQgcm90YXRpbmcgZmlsZSBo"
    "YW5kbGVyPwojX2NvbnNvbGVsb2cgPSBsb2dnaW5nLlN0cmVhbUhhbmRsZXIoc3lzLnN0ZG91dCkK"
    "I2xvZ2dlci5hZGRIYW5kbGVyKF9jb25zb2xlbG9nKQoKX1ZFUlNJT04gPSAnMC4yLjAnCnByaW50"
    "KGYnU2VydmVyIHZlcmlzaW9uIHtfVkVSU0lPTn0nKQpfREVCVUdfQUxMT1dfQU5ZX0xPR0lOID0g"
    "RmFsc2UgI2RvZXMgbm90IHZlcmlmeSBsb2dpbnMsIGZvciBkZWJ1ZyByZWFzb25zCl9UV19MT0JC"
    "WV9QT1JUID0gMTcxNzEKX0FVVE9fUkVHSVNURVIgPSBUcnVlCiMgV2ViIFN0YXR1cyBBUEkgKipF"
    "eHBlcmltZW50YWwqKgpfRU5BQkxFX1dFQl9TRVJWRVIgPSBGYWxzZQpfRU5BQkxFX1dFQl9ERUJV"
    "R19BUEkgPSBGYWxzZQpfRU5BQkxFX0RFQlVHX1JBVEVNT05JVE9SID0gRmFsc2UKX0VOQUJMRV9Q"
    "RERPV05MT0FEID0gRmFsc2UKI19XRUJfU0VSVkVSX1BPUlQgPSA4MCAjZm9yIGh0dHAKI19XRUJf"
    "U0VSVkVSX1BPUlQgPSA0NDMgI2ZvciBodHRwcwpfV0VCX1NFUlZFUl9QT1JUID0gMTcwNzEgI2Zv"
    "ciB0ZXN0aW5nLCBJIGd1ZXNzCiMgVE9ETyBDcmVhdGUgYW5kIEVuYWJsZSBhIHdlYiBiYXNlZCBz"
    "dGF0dXMgYXBpIGZvciBhZG1pbiAocmVxdWlyZSBIVFRQUykKX1BFUkZfTE9HX0xJTUlUID0gMTIg"
    "I2hvdXJzIG9mIGxvZ2dpbmcKX01BWF9QT1NUID0gMHgxMDAwMCAjKDB4MTAwMDAgPSA2NWtiKSBU"
    "T0RPIGNoZWNrIGlmICgweDEwMDAwMCA9IDFNQikgaXMgYmV0dGVyIGNob2ljZT8KI1VwcGVyIGJv"
    "dW5kIGZvciBhIHNpbmdsZSBsZW5ndGgtcHJlZml4ZWQgYmxvYiBmcm9tIGEgY2xpZW50IChwbGF5"
    "ZXJkYXRhLAojaGVyb2RhdGEsIGdhbWUtY29tbWFuZCBwYXlsb2FkKS4gR2VuZXJvdXMgY29tcGFy"
    "ZWQgdG8gYSByZWFsIHNhdmUsIGJ1dCBmaW5pdGU6CiN3aXRob3V0IGl0IGEgY2xpZW50IGNvdWxk"
    "IGFubm91bmNlIGFuIGFyYml0cmFyeSBsZW5ndGggYW5kIG1ha2UgdGhlIHNlcnZlcgojYnVmZmVy"
    "IHVudGlsIGl0IHJhbiBvdXQgb2YgbWVtb3J5LgpfTUFYX0JMT0IgPSAxNiAqIDEwMjQgKiAxMDI0"
    "CiNIYW5kc2hha2UvbG9naW4gcGFja2V0cyBhcmUgYSBmZXcgaHVuZHJlZCBieXRlcyBpbiBwcmFj"
    "dGljZS4gVGhlc2UgYm91bmRzCiNhcHBseSAqYmVmb3JlKiBhdXRoZW50aWNhdGlvbiwgd2hlcmUg"
    "YW55b25lIHdobyBjYW4gcmVhY2ggdGhlIHBvcnQgY2FuIHNlbmQKI3doYXRldmVyIHRoZXkgbGlr"
    "ZSwgc28gdGhleSBhcmUgZGVsaWJlcmF0ZWx5IHRpZ2h0LgpfTUFYX0hBTkRTSEFLRSA9IDY0ICog"
    "MTAyNApfTUFYX0hBTkRTSEFLRV9JTkZMQVRFRCA9IDEwMjQgKiAxMDI0CgojLS0tIHN5bmNocm9u"
    "aXNhdGlvbiB0dW5pbmcgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLQojSG93IG9mdGVuIHRoZSBhY2N1bXVsYXRlZCBoZXJvIHBvc2l0aW9ucyBpbiBhIHRv"
    "d24gYXJlIHB1c2hlZCB0byBldmVyeW9uZSBpbgojaXQuIFRoaXMgdXNlZCB0byBiZSBwaW5uZWQg"
    "dG8gdGhlIDFzIHNvY2tldHNlcnZlciBwb2xsIGludGVydmFsLCB3aGljaCBpcyB3aGF0CiNtYWRl"
    "IG90aGVyIHBsYXllcnMnIG1hcCBtYXJrZXJzIGp1bXAgYSBmdWxsIHNlY29uZCBhdCBhIHRpbWUu"
    "IEVhY2ggdGljayBzZW5kcwojb25lIHBhY2tldCBwZXIgdG93biBhbmQgb25seSBpZiBzb21lYm9k"
    "eSBhY3R1YWxseSBtb3ZlZCwgc28gZXZlbiBhdCB0aGlzIHJhdGUKI2l0J3MgYSBoYW5kZnVsIG9m"
    "IHNtYWxsIHBhY2tldHMvc2VjIGZvciBhIGNvLW9wLXNpemVkIGdyb3VwIC0gbmVnbGlnaWJsZQoj"
    "YmFuZHdpZHRoIGVpdGhlciBvbiBMQU4gb3Igb3ZlciBhIGhvbWUgaW50ZXJuZXQgY29ubmVjdGlv"
    "biAtIHdoaWxlIGdldHRpbmcKI25vdGljZWFibHkgY2xvc2VyIHRvIHNtb290aCBtb3Rpb24gdGhh"
    "biB0aGUgb2xkIDFIeiBiYXNlbGluZS4KX1BPU19VUERBVEVfSFogPSAxMC4wCl9QT1NfVVBEQVRF"
    "X0haX01BWCA9IDIwLjAKI0Ryb3AgYSBjb25uZWN0aW9uIHRoYXQgaGFzIG5vdCBzZW50IGEgc2lu"
    "Z2xlIGJ5dGUgaW4gdGhpcyBsb25nLiBBIHBsYXllciB3aG9zZQojbGluayBkaWVzIHdpdGhvdXQg"
    "YSBjbGVhbiBUQ1AgY2xvc2Ugb3RoZXJ3aXNlIGtlZXBzIHRoZWlyIHVzZXJuYW1lIGNsYWltZWQK"
    "I2ZvcmV2ZXIsIGFuZCB0aGVpciBuZXh0IGxvZ2luIGF0dGVtcHQgaXMgcmVqZWN0ZWQgd2l0aCAn"
    "QWNjb3VudCBhbHJlYWR5IGxvZ2dlZAojaW4nIHVudGlsIHRoZSBzZXJ2ZXIgaXMgcmVzdGFydGVk"
    "LiAwIGRpc2FibGVzLgpfSURMRV9USU1FT1VUID0gMzAwCiNCbG9ja2luZyByZWN2KCkgdGltZW91"
    "dCBpbiB0aGUgcmVhZCBsb29wLiBPbmx5IGdvdmVybnMgaG93IHF1aWNrbHkgYSB0aHJlYWQKI25v"
    "dGljZXMgc2VydmVyIHNodXRkb3duIGFuZCB0aGUgaWRsZSBkZWFkbGluZTsgb3V0Ym91bmQgbGF0"
    "ZW5jeSBubyBsb25nZXIKI2RlcGVuZHMgb24gaXQgbm93IHRoYXQgZWFjaCBjb25uZWN0aW9uIGhh"
    "cyBpdHMgb3duIHdyaXRlciB0aHJlYWQuCl9SRUFEX1RJTUVPVVQgPSAxLjAKI09wdGlvbmFsIHNl"
    "cnZlci0+Y2xpZW50ICcvbm9wJyBoZWFydGJlYXQgZXZlcnkgM3MuIE1haW5seSB1c2VmdWwgdG8g"
    "c3RvcCBob21lCiNyb3V0ZXJzIGRyb3BwaW5nIHRoZSBOQVQgbWFwcGluZyBvZiBhbiBpZGxlIGNv"
    "LW9wIHNlc3Npb24uIE9mZiBieSBkZWZhdWx0OiB0aGUKI3JlYWwgY2xpZW50J3MgcmVhY3Rpb24g"
    "dG8gYW4gdW5zb2xpY2l0ZWQgL25vcCBoYXMgbm90IGJlZW4gdmVyaWZpZWQuCl9TRU5EX05PUFMg"
    "PSBGYWxzZQoKX1BST1RPQ09MX1ZFUiA9ICdIVFRQLzEuMScKClBFUk1fVVBEICA9ICgxPDwwKSAj"
    "IHVwbG9hZF9wbGF5ZXJkYXRhClBFUk1fRE9QRCA9ICgxPDwxKSAjIGRvd25sb2FkX290aGVyX3Bs"
    "YXllcmRhdGEKCkRFRkFVTFRfVElUTEUgPSAnQ29tbXVuaXR5IE11bHRpcGxheWVyIFNlcnZlcicK"
    "REVGQVVMVF9NT1REID0gZic8MHhGRjAwMDBGRj48RjI+Q29tbXVuaXR5IE11bHRpcGxheWVyIFNl"
    "cnZlciBWZXJzaW9uIHtfVkVSU0lPTn08YnJlYWs9MTAuMD5cclxuJwoKI1Jvb3QgbmV4dCB0byB0"
    "aGlzIHNjcmlwdCByYXRoZXIgdGhhbiB0aGUgcHJvY2VzcycgY3VycmVudCB3b3JraW5nIGRpcmVj"
    "dG9yeSwKI3NvIHRoZSBkYXRhYmFzZS9jb25maWcvcGxheWVyZGF0YSBhbHdheXMgbGl2ZSBpbiB0"
    "aGUgc2FtZSBwbGFjZSB3aGV0aGVyIHRoZQojc2VydmVyIGlzIGRvdWJsZS1jbGlja2VkLCBsYXVu"
    "Y2hlZCBmcm9tIGEgdGVybWluYWwgZWxzZXdoZXJlLCBvciBpbXBvcnRlZCBieQojYSBHVUkgd3Jh"
    "cHBlciAoZS5nLiBUVzEgQ29udHJvbCBDZW50ZXIpLgojQWxsb3dzIGFuIGVtYmVkZGluZyBob3N0"
    "IChlLmcuIGEgcG9ydGFibGUgYWxsLWluLW9uZSBsYXVuY2hlciB0aGF0IGV4ZWMoKXMKI3RoaXMg"
    "ZmlsZSdzIHNvdXJjZSBmcm9tIG1lbW9yeSwgd2hlcmUgX19maWxlX18gaXMgbWVhbmluZ2xlc3Mp"
    "IHRvIHJlZGlyZWN0CiN3aGVyZSB0aGUgZGF0YWJhc2UvY29uZmlnL3BsYXllcmRhdGEgbGl2ZSBi"
    "eSBwcmUtc2V0dGluZyB0aGlzIG5hbWUgaW4gdGhlCiNtb2R1bGUncyBnbG9iYWxzIGJlZm9yZSB0"
    "aGUgbW9kdWxlIGJvZHkgcnVucy4gU3RhbmRhbG9uZSBleGVjdXRpb24gKHRoZQojbm9ybWFsIGBw"
    "eXRob24gVFcxQ1MucHlgKSBpcyB1bmFmZmVjdGVkOiBmYWxscyBiYWNrIHRvIG5leHQgdG8gdGhp"
    "cyBzY3JpcHQuCmlmICdfRVhURVJOQUxfREFUQV9ESVInIGluIGdsb2JhbHMoKSBhbmQgZ2xvYmFs"
    "cygpWydfRVhURVJOQUxfREFUQV9ESVInXToKICAgIF9QQVRIX1JPT1QgPSBnbG9iYWxzKClbJ19F"
    "WFRFUk5BTF9EQVRBX0RJUiddCmVsc2U6CiAgICBfUEFUSF9ST09UID0gb3MucGF0aC5kaXJuYW1l"
    "KG9zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpCl9QQVRIX0RBVEFCQVNFID0gb3MucGF0aC5qb2lu"
    "KF9QQVRIX1JPT1QsJ1NlcnZlckRhdGEuZGInKQpfUEFUSF9DT05GSUcgPSBvcy5wYXRoLmpvaW4o"
    "X1BBVEhfUk9PVCwnQ29uZmlnLmluaScpCl9QQVRIX1BMQVlFUkRBVEEgPSBvcy5wYXRoLmpvaW4o"
    "X1BBVEhfUk9PVCwnUGxheWVyRGF0YScpCl9QQVRIX1dFQiA9IG9zLnBhdGguam9pbihfUEFUSF9S"
    "T09ULCdXZWInKQoKZGVmIF9lc2NhcGVNT1REKG1vdGQpOgogICAgI2NvbmZpZ3BhcnNlciB2YWx1"
    "ZXMgY2FuJ3Qgc2FmZWx5IGhvbGQgcmF3IENSL0xGLCBzdG9yZSBhcyBcclxuIGVzY2FwZXMKICAg"
    "IHJldHVybiBtb3RkLmVuY29kZSgndW5pY29kZV9lc2NhcGUnKS5kZWNvZGUoJ2FzY2lpJykKZGVm"
    "IF91bmVzY2FwZU1PVEQobW90ZCk6CiAgICAjX2VzY2FwZU1PVEQgYWx3YXlzIHdyaXRlcyBwdXJl"
    "IGFzY2lpLCBidXQgYSBoYW5kLWVkaXRlZCBDb25maWcuaW5pIG1heSBob2xkCiAgICAjcmF3IDgt"
    "Yml0IHRleHQ7IHRvbGVyYXRlIGl0IGluc3RlYWQgb2YgcmVmdXNpbmcgdG8gc3RhcnQgdGhlIHNl"
    "cnZlcgogICAgcmV0dXJuIG1vdGQuZW5jb2RlKF9XSVJFX0VOQywgJ3JlcGxhY2UnKS5kZWNvZGUo"
    "J3VuaWNvZGVfZXNjYXBlJykKX0NPTkZJR19ERUZBVUxUUyA9IHsKICAgICdTZXJ2ZXJOYW1lJzog"
    "REVGQVVMVF9USVRMRSwKICAgICdNT1REJzogX2VzY2FwZU1PVEQoREVGQVVMVF9NT1REKSwKICAg"
    "ICdQb3J0Jzogc3RyKF9UV19MT0JCWV9QT1JUKSwKICAgICdBdXRvUmVnaXN0ZXInOiBzdHIoX0FV"
    "VE9fUkVHSVNURVIpLAogICAgJ0FsbG93QW55TG9naW4nOiBzdHIoX0RFQlVHX0FMTE9XX0FOWV9M"
    "T0dJTiksCiAgICAnUG9zaXRpb25VcGRhdGVIeic6IHN0cihfUE9TX1VQREFURV9IWiksCiAgICAn"
    "SWRsZVRpbWVvdXQnOiBzdHIoX0lETEVfVElNRU9VVCksCiAgICAnS2VlcGFsaXZlJzogc3RyKF9T"
    "RU5EX05PUFMpLAp9CmRlZiBsb2FkQ29uZmlnKCk6CiAgICBjZmcgPSBjb25maWdwYXJzZXIuQ29u"
    "ZmlnUGFyc2VyKCkKICAgIGNmZ1snc2VydmVyJ10gPSBkaWN0KF9DT05GSUdfREVGQVVMVFMpCiAg"
    "ICBpZiBvcy5wYXRoLmV4aXN0cyhfUEFUSF9DT05GSUcpOgogICAgICAgIGNmZy5yZWFkKF9QQVRI"
    "X0NPTkZJRykKICAgIGVsc2U6CiAgICAgICAgc2F2ZUNvbmZpZyhjZmcpCiAgICByZXR1cm4gY2Zn"
    "CmRlZiBzYXZlQ29uZmlnKGNmZyk6CiAgICB3aXRoIG9wZW4oX1BBVEhfQ09ORklHLCAndycsIGVu"
    "Y29kaW5nPSd1dGYtOCcpIGFzIGY6CiAgICAgICAgY2ZnLndyaXRlKGYpCmRlZiBhcHBseUNvbmZp"
    "ZyhjZmcpOgogICAgI0FwcGxpZXMgY29uZmlnIHZhbHVlcyB0byB0aGUgbGl2ZSBtb2R1bGUgZ2xv"
    "YmFscy4gU2VydmVyTmFtZS9NT1RELwogICAgI0F1dG9SZWdpc3RlciB0YWtlIGVmZmVjdCBpbW1l"
    "ZGlhdGVseSAocmVhZCBmcmVzaCBwZXIgbG9naW4gYXR0ZW1wdCk7CiAgICAjUG9ydCBvbmx5IHRh"
    "a2VzIGVmZmVjdCBmb3Igc2VydmVycyBzdGFydGVkIGFmdGVyIHRoaXMgY2FsbC4KICAgIGdsb2Jh"
    "bCBERUZBVUxUX1RJVExFLCBERUZBVUxUX01PVEQsIF9UV19MT0JCWV9QT1JULCBfQVVUT19SRUdJ"
    "U1RFUiwgX0RFQlVHX0FMTE9XX0FOWV9MT0dJTgogICAgZ2xvYmFsIF9QT1NfVVBEQVRFX0haLCBf"
    "SURMRV9USU1FT1VULCBfU0VORF9OT1BTCiAgICBzZWMgPSBjZmdbJ3NlcnZlciddCiAgICBERUZB"
    "VUxUX1RJVExFID0gc2VjLmdldCgnU2VydmVyTmFtZScsIGZhbGxiYWNrPURFRkFVTFRfVElUTEUp"
    "CiAgICBERUZBVUxUX01PVEQgPSBfdW5lc2NhcGVNT1REKHNlYy5nZXQoJ01PVEQnLCBmYWxsYmFj"
    "az1fZXNjYXBlTU9URChERUZBVUxUX01PVEQpKSkKICAgIF9UV19MT0JCWV9QT1JUID0gc2VjLmdl"
    "dGludCgnUG9ydCcsIGZhbGxiYWNrPV9UV19MT0JCWV9QT1JUKQogICAgX0FVVE9fUkVHSVNURVIg"
    "PSBzZWMuZ2V0Ym9vbGVhbignQXV0b1JlZ2lzdGVyJywgZmFsbGJhY2s9X0FVVE9fUkVHSVNURVIp"
    "CiAgICBfREVCVUdfQUxMT1dfQU5ZX0xPR0lOID0gc2VjLmdldGJvb2xlYW4oJ0FsbG93QW55TG9n"
    "aW4nLCBmYWxsYmFjaz1fREVCVUdfQUxMT1dfQU5ZX0xPR0lOKQogICAgI0NsYW1wZWQgcmF0aGVy"
    "IHRoYW4gdHJ1c3RlZDogdGhlc2UgY29tZSBmcm9tIGEgaGFuZC1lZGl0YWJsZSBpbmksIGFuZCBh"
    "CiAgICAjc3RyYXkgMCBvciAxMDAwMCBoZXJlIHdvdWxkIGVpdGhlciBzdG9wIHBvc2l0aW9uIHVw"
    "ZGF0ZXMgZW50aXJlbHkgb3Igc3BpbgogICAgI3RoZSB1cGRhdGUgdGhyZWFkIGZsYXQgb3V0Lgog"
    "ICAgaHogPSBzZWMuZ2V0ZmxvYXQoJ1Bvc2l0aW9uVXBkYXRlSHonLCBmYWxsYmFjaz1fUE9TX1VQ"
    "REFURV9IWikKICAgIF9QT1NfVVBEQVRFX0haID0gbWluKG1heChoeiwgMC41KSwgX1BPU19VUERB"
    "VEVfSFpfTUFYKQogICAgX0lETEVfVElNRU9VVCA9IG1heCgwLCBzZWMuZ2V0aW50KCdJZGxlVGlt"
    "ZW91dCcsIGZhbGxiYWNrPV9JRExFX1RJTUVPVVQpKQogICAgX1NFTkRfTk9QUyA9IHNlYy5nZXRi"
    "b29sZWFuKCdLZWVwYWxpdmUnLCBmYWxsYmFjaz1fU0VORF9OT1BTKQpDRkcgPSBsb2FkQ29uZmln"
    "KCkKYXBwbHlDb25maWcoQ0ZHKQoKIyMjIFVTRVIgU1RSVUNUVVJFCiMgY29ubmVjdGlvbgojIHVz"
    "ZXJuYW1lCiMgaGVyb2RhdGEKIyBwb3NpdGlvbgojIGdhbWVjaGFubmVsCiMgY2hhdGNoYW5uZWwK"
    "IyBnYW1lCgpjbGFzcyBVc2VyKCk6ICNUT0RPIG1lcmdlIHVzZXIgaW50byBjb25uZWN0aW9uPywg"
    "dmFsaWRhdGlvbiBjYW4gYmUgYXNzdW1lZCBieSBzdGFnZQogICAgZGVmIF9faW5pdF9fKHNlbGYs"
    "IG5hbWUsIGNvbik6CiAgICAgICAgc2VsZi5oZXJvZGF0YSA9IGInJwogICAgICAgIHNlbGYucG9z"
    "ZGF0YSA9IE5vbmUKICAgICAgICBzZWxmLnBvc2NoYW5nZWQgPSBGYWxzZQogICAgICAgIHNlbGYu"
    "cmVxdWVzdGVkQ2hhbm5lbCA9IE5vbmUKICAgICAgICBzZWxmLmdhbWVjaGFubmVsID0gTm9uZQog"
    "ICAgICAgIHNlbGYuY2hhdGNoYW5uZWwgPSBOb25lCiAgICAgICAgc2VsZi5yZXF1ZXN0ZWRHYW1l"
    "ID0gTm9uZQogICAgICAgIHNlbGYuZ2FtZSA9IE5vbmUKICAgICAgICBzZWxmLm5hbWUgPSBuYW1l"
    "CiAgICAgICAgc2VsZi5sb2dpblRpbWUgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAgICAg"
    "IHNlbGYuaWRudW0gPSBHREguZ2V0VVJhbmRvbSgpCiAgICAgICAgc2VsZi5jb25uZWN0aW9uID0g"
    "Y29uICNzZXJ2ZXIgPSBjb24uc2VydmVyCiAgICAgICAgI3NlbGYuY29ubmVjdGlvbi5ndWlkIC0+"
    "IGd1aWQgd2hlbiByZWxldmFudAogICAgICAgIHNlbGYucGd1aWQgPSBwcmV0dHlfZ3VpZChzZWxm"
    "LmNvbm5lY3Rpb24uZ3VpZCkKICAgIGRlZiBsZWF2ZUNoYW5uZWwoc2VsZik6CiAgICAgICAgaWYg"
    "c2VsZi5yZXF1ZXN0ZWRDaGFubmVsOgogICAgICAgICAgICAjbGlzdC5yZW1vdmUoKSByYWlzZXMg"
    "VmFsdWVFcnJvciB3aGVuIHRoZSBlbnRyeSBpcyBhbHJlYWR5IGdvbmU7CiAgICAgICAgICAgICN0"
    "aGF0IHVzZWQgdG8gYWJvcnQgdGhlIHJlc3Qgb2YgdGhlIGRpc2Nvbm5lY3QgY2xlYW51cAogICAg"
    "ICAgICAgICBpZiBzZWxmLmNvbm5lY3Rpb24gaW4gc2VsZi5yZXF1ZXN0ZWRDaGFubmVsLnJlcXVl"
    "c3RlZDoKICAgICAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkQ2hhbm5lbC5yZXF1ZXN0ZWQucmVt"
    "b3ZlKHNlbGYuY29ubmVjdGlvbikKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWRDaGFubmVsID0g"
    "Tm9uZQogICAgICAgIGlmIHNlbGYuZ2FtZWNoYW5uZWw6CiAgICAgICAgICAgIHNlbGYuZ2FtZWNo"
    "YW5uZWwubGVhdmVDaGFubmVsKHNlbGYuY29ubmVjdGlvbikKICAgICAgICAgICAgI2xlYXZlQ2hh"
    "bm5lbCBhbHNvIGxlYXZlcyBjaGF0CiAgICBkZWYgbGVhdmVDaGF0KHNlbGYpOgogICAgICAgIGlm"
    "IHNlbGYuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgIGlmIHNlbGYuY29ubmVjdGlvbiBpbiBzZWxm"
    "LmNoYXRjaGFubmVsOgogICAgICAgICAgICAgICAgc2VsZi5jaGF0Y2hhbm5lbC5yZW1vdmUoc2Vs"
    "Zi5jb25uZWN0aW9uKQogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZjaGF0Y2hhbm5lbHVz"
    "ZXIgIntzZWxmLm5hbWV9IicpCiAgICAgICAgICAgIHNlbGYuY29ubmVjdGlvbi5zZXJ2ZXIuZGlz"
    "dC5hZGQoeyd0YXJnZXQnOnNlbGYuY2hhdGNoYW5uZWwsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAg"
    "ICAgICAgICAgc2VsZi5jaGF0Y2hhbm5lbD1Ob25lCiAgICBkZWYgc3RvcEdhbWUoc2VsZik6CiAg"
    "ICAgICAgaWYgc2VsZi5yZXF1ZXN0ZWRHYW1lOgogICAgICAgICAgICAjQm90aCBndWFyZHMgbWF0"
    "dGVyOiB0aGUgY2hhbm5lbCBtYXkgYWxyZWFkeSBiZSBnb25lIChsZWF2ZUNoYW5uZWwKICAgICAg"
    "ICAgICAgI2NsZWFycyBpdCBiZWZvcmUgc3RvcEdhbWUgcnVucyBvbiBzb21lIHBhdGhzKSBhbmQg"
    "dGhlIHBlbmRpbmcKICAgICAgICAgICAgI3JlcXVlc3QgbWF5IGFscmVhZHkgaGF2ZSBiZWVuIGNv"
    "bnN1bWVkIGJ5IGNyZWF0ZUdhbWUuIEVpdGhlciBvbmUKICAgICAgICAgICAgI3VzZWQgdG8gcmFp"
    "c2UgKEF0dHJpYnV0ZUVycm9yIG9uIE5vbmUgLyBLZXlFcnJvcikgaW5zaWRlIHRoZQogICAgICAg"
    "ICAgICAjZGlzY29ubmVjdCBwYXRoIGFuZCBhYm9ydCB0aGUgcmVzdCBvZiB0aGUgY2xlYW51cCwg"
    "bGVha2luZyB0aGUKICAgICAgICAgICAgI3BsYXllcidzIGVudHJ5IGluIGFjdGl2ZVVzZXJzLgog"
    "ICAgICAgICAgICBpZiBzZWxmLmdhbWVjaGFubmVsOgogICAgICAgICAgICAgICAgc2VsZi5nYW1l"
    "Y2hhbm5lbC5nYW1lUmVxdWVzdHMucG9wKHNlbGYucmVxdWVzdGVkR2FtZSwgTm9uZSkKICAgICAg"
    "ICAgICAgc2VsZi5yZXF1ZXN0ZWRHYW1lID0gTm9uZQogICAgICAgIGlmIHNlbGYuZ2FtZToKICAg"
    "ICAgICAgICAgc2VsZi5nYW1lLnJlbW92ZShzZWxmLmNvbm5lY3Rpb24pCiAgICBkZWYgZGlzY29u"
    "bmVjdChzZWxmLCBzZXJ2ZXIpOgogICAgICAgIHNlbGYuc3RvcEdhbWUoKQogICAgICAgIHNlbGYu"
    "bGVhdmVDaGFubmVsKCkKICAgICAgICBzZXJ2ZXIuc3RhdGUucmVsZWFzZVVzZXIoc2VsZi5uYW1l"
    "LCBzZWxmLmNvbm5lY3Rpb24pCiAgICAgICAgR0RILnJlbGVhc2VVUmFuZG9tKHNlbGYuaWRudW0p"
    "CiAgICBkZWYgZ2V0R0NVbXNnKHNlbGYpOgogICAgICAgIGhkbCA9IGxlbihzZWxmLmhlcm9kYXRh"
    "KQogICAgICAgIGlmIGhkbD09MDoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAgIHJldHVy"
    "biBfZW0oZickZ2FtZWNoYW5uZWx1c2VyICJ7c2VsZi5uYW1lfSIgIiIgIjEwMCIgIntzZWxmLmlk"
    "bnVtfSIgIjAiICJ7c2VsZi5wZ3VpZH0iICJ7c2VsZi5wb3NkYXRhfSIgIntoZGx9IicpK3NlbGYu"
    "aGVyb2RhdGEKICAgIGRlZiBnZXRDQ1Vtc2coc2VsZik6CiAgICAgICAgdmIgPSAwICNvciAweEZG"
    "RkZGRkZGKDQyOTQ5NjcyOTU9IC0xJjMyYml0PykKICAgICAgICByZXR1cm4gX2VtKGYnJGNoYXRj"
    "aGFubmVsdXNlciAie3NlbGYubmFtZX0iICIiICJ7dmJ9IiAie3NlbGYucGd1aWR9IicpCiAgICAg"
    "ICAgIyAkY2hhdGNoYW5uZWx1c2VyICJ7bmFtZX0iICIiICIwIiAie2d1aWR9IgojIGluY3JlYXNp"
    "bmcgbWF5IGltcHJvdmUgc2VjdXJpdHkgYXQgdGhlIGNvc3Qgb2YgcGVyZm9ybWFuY2UKIyBvbmx5"
    "IHVwZGF0ZXMgd2hlbiB1c2VyIGxvZ3MgaW4gYW5kIGlzIHN0b3JlZCBhbG9uZ3NpZGUgc2FsdCBp"
    "biBkYXRhYmFzZQpfSEFTSElURVIgPSAxMDAwMDAKZGVmIF9zYWx0X2hhc2hfKHBhc3N3b3JkLCBz"
    "YWx0LCBoSXRyKToKICAgICN1dGYtOCwgbm90IGFzY2lpOiBhIHBhc3N3b3JkIHdpdGggYW4gOC1i"
    "aXQgY2hhcmFjdGVyIHVzZWQgdG8gcmFpc2UgaGVyZSBhbmQKICAgICNkcm9wIHRoZSBjb25uZWN0"
    "aW9uIGluc3RlYWQgb2YgbG9nZ2luZyB0aGUgcGxheWVyIGluLiBQdXJlLWFzY2lpIHBhc3N3b3Jk"
    "cwogICAgI2VuY29kZSB0byBpZGVudGljYWwgYnl0ZXMgdW5kZXIgYm90aCwgc28gbm8gc3RvcmVk"
    "IGhhc2ggY2hhbmdlcy4KICAgIHJldHVybiBoYXNobGliLnBia2RmMl9obWFjKCdzaGEyNTYnLCBw"
    "YXNzd29yZC5lbmNvZGUoJ3V0Zi04JyksIHNhbHQsIGhJdHIpCiAgICAKIyMjIFNRTCBJTkZPCiMg"
    "X0RCSU5GTzogVkVSU0lPTiAxCiMgdXNlclRhYmxlCiMgLSByb3dpZCwgdXNlcm5hbWUsIHBhc3NI"
    "YXNoLCBzZXJpYWwsIHVuaXF1ZVNhbHQsIGxhc3RMb2dpbiwgZW1haWwsIGxvY2F0aW9uLCB5ZWFy"
    "b2ZiaXJ0aChlc3RpbWF0ZSksIGdlbmRlciwgZGVzY3JpcHRpb24KIyBmb3JtVGFibGUKIyAtIHJv"
    "d2lkLCBmb3JtCiMjIC0tLS0tLS0tLS0tLS0tLS0gIyMKIyBUT0RPIFZFUlNJT04gMjogZ3VpbGRz"
    "LCBsZWFkZXJib2FyZCwgZXRjPwoKI1RPRE8gY29udmVydCBkYXRhYmFzZSB0byBzaW5nbGV0aHJl"
    "YWQgYWNjZXNzIGZvciBjb21wYXRpYmlsaXR5PyB1bm5lY2Nlc2FyeT8KI2NsYXNzIERhdGFSZXF1"
    "ZXN0KHRocmVhZGluZy5FdmVudCk6CiMgICBkYXRhID0gTm9uZQojICAgZGVmIHNldCh2YWwpOgoj"
    "ICAgICAgIHNlbGYuZGF0YT12YWwKIyAgICAgICBzdXBlcigpLnNldCgpCiMgICBkZWYgd2FpdCgp"
    "OgojICAgICAgIHN1cGVyKCkud2FpdCgpCiMgICAgICAgcmV0dXJuIHNlbGYuZGF0YQojKiBkYXRh"
    "YmFzZSB0aHJlYWQ6CiMgICBfZHJRID0gZGF0YSByZXF1ZXN0IHF1ZXVlLCBwcm9jZXNzZWQgaW4g"
    "ZGF0YWJhc2UgdGhyZWFkCiMgICBleHRlcm5hbCBmdW5jdGlvbnMgYWRkIHJlcXVlc3QgZm9yIGlu"
    "dGVybmFsIGZ1bmN0aW9uIGFuZCByZXR1cm4gcmVxdWVzdCB0byBhd2FpdAojICAgZHJvYmogaW4g"
    "cXVldWUgPSAoZHIsIGZ0YXJnZXQsIChhcmdzKSksIGRyLnNldChmdGFyZ2V0KCphcmdzKSkKI1RP"
    "RE8gb3JnYW5pemUgU1FMIGNvbW1hbmRzPyBtYWtlIGl0IG1vcmUgYmVhdXRpZnVsPwpfU1FMX2Ri"
    "SW5mb0V4aXN0cyA9ICdTRUxFQ1QgbmFtZSBGUk9NIHNxbGl0ZV9tYXN0ZXIgV0hFUkUgbmFtZT0i"
    "X0RCSU5GTyInCl9TUUxfZGJWZXJzaW9uID0gJ1NFTEVDVCBWRVJTSU9OIEZST00gX0RCSU5GTycK"
    "X1NRTElOSVRfZGJJbmZvVGFibGUgPSAnQ1JFQVRFIFRBQkxFIF9EQklORk8oVkVSU0lPTiknCl9E"
    "QkNVUlZFUiA9IDEKX1NRTElOSVRfZGJJbmZvVmVyc2lvbiA9IGYnSU5TRVJUIElOVE8gX0RCSU5G"
    "TyBWQUxVRVMgKHtfREJDVVJWRVJ9KScKX1NRTFVQRF9kYkluZm9WZXJzaW9uID0gZidVUERBVEUg"
    "X0RCSU5GTyBTRVQgVkVSU0lPTiA9IHtfREJDVVJWRVJ9JwojeW9iID0geWVhciBvZiBiaXJ0aCAo"
    "ZXN0aW1hdGUpCiNnZW5kZXI6IDAgPSBNYWxlCl9TUUxJTklUX2RiVXNlclRhYmxlID0gJ0NSRUFU"
    "RSBUQUJMRSB1c2VyVGFibGUodXNlcm5hbWUgVU5JUVVFLCBwYXNzSGFzaCwgc2VyaWFsLCB1bmlx"
    "dWVTYWx0LCBoYXNoSXRlciwgbGFzdExvZ2luIFRJTUVTVEFNUCwgZW1haWwsIGxvY2F0aW9uLCB5"
    "b2IsIGdlbmRlciwgZGVzY3JpcHRpb24pJwpfU1FMSU5JVF9kYkZvcm1UYWJsZSA9ICdDUkVBVEUg"
    "VEFCTEUgZm9ybVRhYmxlKGZvcm0gVU5JUVVFKScgI3VzaW5nIHJvd2lkIGFzIElECgpfU1FMX3Vz"
    "ZXJJRCA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2VyVGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/Jwpf"
    "U1FMX3VzZXJJRF9TY2hrID0gJ1NFTEVDVCByb3dpZCBGUk9NIHVzZXJUYWJsZSBXSEVSRSBzZXJp"
    "YWwgPSA/JwpfU1FMX3VzZXJJRF9zdHJpY3QgPSAnU0VMRUNUIHJvd2lkIEZST00gdXNlclRhYmxl"
    "IFdIRVJFIHVzZXJuYW1lID0gPyBBTkQgc2VyaWFsID0gPycKX1NRTF9yZWdpc3RlclVzZXIgPSAn"
    "SU5TRVJUIElOVE8gdXNlclRhYmxlIFZBTFVFUyAoPyw/LD8sPyw/LD8sPyw/LD8sPyw/KScKX1NR"
    "TF9kZWxldGVVc2VyID0gJ0RFTEVURSBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8n"
    "Cl9TUUxfZ2V0TG9naW4gPSAnU0VMRUNUIHVzZXJuYW1lLCBwYXNzSGFzaCwgdW5pcXVlU2FsdCwg"
    "aGFzaEl0ZXIgRlJPTSB1c2VyVGFibGUgV0hFUkUgcm93aWQgPSA/JwpfU1FMVVBEX3Bhc3NIYXNo"
    "ID0gJ1VQREFURSB1c2VyVGFibGUgU0VUIHBhc3NIYXNoID0gPywgaGFzaEl0ZXIgPSA/IFdIRVJF"
    "IHJvd2lkID0gPycKX1NRTF9sb2dpblVwZGF0ZSA9ICdVUERBVEUgdXNlclRhYmxlIFNFVCBsYXN0"
    "TG9naW4gPSA/IFdIRVJFIHJvd2lkID0gPycKX1NRTF9nZXRXaG9pcyA9ICdTRUxFQ1QgZW1haWws"
    "IGxvY2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24gRlJPTSB1c2VyVGFibGUgV0hFUkUg"
    "dXNlcm5hbWUgPSA/JwpfU1FMVVBEX3dob2lzID0gJ1VQREFURSB1c2VyVGFibGUgU0VUIGVtYWls"
    "ID0gPywgbG9jYXRpb24gPSA/LCB5b2IgPSA/LCBnZW5kZXIgPSA/LCBkZXNjcmlwdGlvbiA9ID8g"
    "V0hFUkUgdXNlcm5hbWUgPSA/JwojaWYgZG9lcyBub3QgZXhpc3QsIGdlbmVyYXRlLCBjaGFuZ2Ug"
    "Zm9ybWF0IGZvciBtb2RwYWNrcwpfU1FMX2Zvcm1JRCA9ICdTRUxFQ1Qgcm93aWQgZnJvbSBmb3Jt"
    "VGFibGUgV0hFUkUgZm9ybSA9ID8nCl9TUUxBRERfZm9ybUlEID0gJ0lOU0VSVCBJTlRPIGZvcm1U"
    "YWJsZSBWQUxVRVMgKD8pJwpfRk9STV9QREZpbGUgPSAnezp4fV97Onh9LmJpbicgIyBwbGF5ZXJk"
    "YXRhXHVzZXJJRF9mb3JtSUQuYmluCgpfTUlNRV9KU18gPSAidGV4dC9qYXZhc2NyaXB0IgpfTUlN"
    "RV9IVE1MXyA9ICJ0ZXh0L2h0bWwiCl9NSU1FX0pTT05fID0gImFwcGxpY2F0aW9uL2pzb24iCl9N"
    "SU1FX0dMU0xfID0gInRleHQvZ2xzbCIKX01JTUVfSUNPXyA9ICJpbWFnZS94LWljb24iCl9NSU1F"
    "X1BMQUlOVEVYVF8gPSAndGV4dC9wbGFpbicKX01JTUVfQklOQVJZXyA9ICdhcHBsaWNhdGlvbi9v"
    "Y3RldC1zdHJlYW0nCl9ESVNQX1BMQVlFUkRBVEFfID0gJ2F0dGFjaG1lbnQ7IGZpbGVuYW1lPSJQ"
    "bGF5ZXJkYXRhLmJpbiInCl9NQU5VQUxfRklMRVMgPSB7CiAgICAnJzpbb3MucGF0aC5qb2luKF9Q"
    "QVRIX1dFQiwnaW5kZXguaHRtbCcpLF9NSU1FX0hUTUxfLCBGYWxzZV0sCn0KZGVmIG1hcEZpbGVz"
    "KHNvdXJjZSk6CiAgICBmb3IgKHJvb3QsIGRpcnMsIGZpbGVzKSBpbiBvcy53YWxrKHNvdXJjZSk6"
    "CiAgICAgICAgZm9yIG5hbWUgaW4gZmlsZXM6CiAgICAgICAgICAgIGZ1bGxQYXRoID0gb3MucGF0"
    "aC5qb2luKHJvb3QsIG5hbWUpCiAgICAgICAgICAgIHJlbFBhdGggPSBvcy5wYXRoLnJlbHBhdGgo"
    "ZnVsbFBhdGgsIHN0YXJ0PXNvdXJjZSkKICAgICAgICAgICAgZW5kID0gb3MucGF0aC5zcGxpdGV4"
    "dChmdWxsUGF0aClbMV0KICAgICAgICAgICAgaWYgZW5kPT0nLmdsc2wnOgogICAgICAgICAgICAg"
    "ICAgX01BTlVBTF9GSUxFU1tyZWxQYXRoXT1bZnVsbFBhdGgsX01JTUVfR0xTTF8sIEZhbHNlXQog"
    "ICAgICAgICAgICBlbGlmIGVuZD09Jy5qcyc6CiAgICAgICAgICAgICAgICBfTUFOVUFMX0ZJTEVT"
    "W3JlbFBhdGhdPVtmdWxsUGF0aCxfTUlNRV9KU18sIEZhbHNlXQogICAgICAgICAgICBlbGlmIGVu"
    "ZD09Jy5odG1sJzoKICAgICAgICAgICAgICAgIF9NQU5VQUxfRklMRVNbcmVsUGF0aF09W2Z1bGxQ"
    "YXRoLF9NSU1FX0hUTUxfLCBGYWxzZV0KICAgICAgICAgICAgZWxpZiBlbmQ9PScuaWNvJzoKICAg"
    "ICAgICAgICAgICAgIF9NQU5VQUxfRklMRVNbcmVsUGF0aF09W2Z1bGxQYXRoLF9NSU1FX0lDT18s"
    "IFRydWVdCiAgICAgICAgICAgIGVsc2U6IGNvbnRpbnVlCgpkZWYgcmVhZFRleHQoZmlsZXBhdGgp"
    "OgogICAgd2l0aCBvcGVuKGZpbGVwYXRoLCAiciIpIGFzIGY6CiAgICAgICAgcmV0dXJuIGYucmVh"
    "ZCgpCmRlZiByZWFkQmluKGZpbGVwYXRoKToKICAgIHdpdGggb3BlbihmaWxlcGF0aCwgInJiIikg"
    "YXMgZjoKICAgICAgICByZXR1cm4gZi5yZWFkKCkKY2xhc3MgRGF0YUhhbmRsZXIoKToKICAgIGRl"
    "ZiBfX2luaXRfXyhzZWxmKToKICAgICAgICAjaW5zdGFuY2UgYXR0cmlidXRlLCBub3QgYSBjbGFz"
    "cyBhdHRyaWJ1dGUgLSBzYW1lIHJlYXNvbmluZyBhcwogICAgICAgICNHYW1lU3RhdGUuYWN0aXZl"
    "VXNlcnM6IHNoYXJlZCBjbGFzcyBzdGF0ZSBsZWFrcyBiZXR3ZWVuIGluc3RhbmNlcwogICAgICAg"
    "IHNlbGYudXNlZE51bXMgPSBzZXQoKQogICAgICAgICNwcmludCgnc3FsaXRlMyB0aHJlYWRzYWZl"
    "dHk6JyxzcWxpdGUzLnRocmVhZHNhZmV0eSkKICAgICAgICAjaWYgc3FsaXRlMy50aHJlYWRzYWZl"
    "dHk8MzoKICAgICAgICAjICAgIHJhaXNlIEV4Y2VwdGlvbignTXVsdGlUaHJlYWQgc3VwcG9ydCBy"
    "ZXF1aXJlZCcpCiAgICAgICAgI1RPRE8gb3JnYW5pemUgc2luZ2xlIHRocmVhZGVkIGRhdGFiYXNl"
    "IGFjY2Vzcz8gZXZlciBuZWVkZWQ/CiAgICAgICAgc2VsZi5sb2NrID0gdGhyZWFkaW5nLlJMb2Nr"
    "KCkKICAgICAgICBvcy5tYWtlZGlycyhfUEFUSF9QTEFZRVJEQVRBLCBleGlzdF9vaz1UcnVlKQog"
    "ICAgICAgIHNlbGYuZGIgPSBzcWxpdGUzLmNvbm5lY3QoX1BBVEhfREFUQUJBU0UsCiAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICBjaGVja19zYW1lX3RocmVhZCA9IEZhbHNlLAogICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZGV0ZWN0X3R5cGVzPXNxbGl0ZTMuUEFSU0Vf"
    "REVDTFRZUEVTIHwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHNxbGl0ZTMuUEFS"
    "U0VfQ09MTkFNRVMpCiAgICAgICAgaW5pdGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICBk"
    "YlVuaW5pdGlhbGl6ZWQgPSBpbml0Y3VyLmV4ZWN1dGUoX1NRTF9kYkluZm9FeGlzdHMpLmZldGNo"
    "b25lKCkgaXMgTm9uZQogICAgICAgIGlmIGRiVW5pbml0aWFsaXplZDoKICAgICAgICAgICAgZGJW"
    "ZXJSZXMgPSAwCiAgICAgICAgZWxzZToKICAgICAgICAgICAgZGJWZXJSZXMgPSBpbml0Y3VyLmV4"
    "ZWN1dGUoX1NRTF9kYlZlcnNpb24pLmZldGNob25lKClbMF0KICAgICAgICBzZWxmLnVwZGF0ZURC"
    "RnJvbShkYlZlclJlcykgI2Vuc3VyZSBEQiBpcyB1cGRhdGVkCiAgICAgICAgCiAgICAgICAgaW5p"
    "dGN1ci5jbG9zZSgpCiAgICAgICAgbWFwRmlsZXMoX1BBVEhfV0VCKSNUT0RPIENIRUNLIGlmIHdv"
    "cmtpbmcgcHJvcGVybHkgd2hlbiBmb2xkZXIgbWlzc2luZwogICAgICAgIGlmIF9FTkFCTEVfREVC"
    "VUdfUkFURU1PTklUT1I6CiAgICAgICAgICAgIHNlbGYuTG9iYnlTZW5kUmF0ZXMgPSBCeXRlUmF0"
    "ZUxvZ2dlcigpCiAgICAgICAgICAgIHNlbGYuTG9iYnlSZWN2UmF0ZXMgPSBCeXRlUmF0ZUxvZ2dl"
    "cigpCiAgICAgICAgICAgICMgb25seSBjb3VudHMgZ2FtZWNvbW1hbmR0b3VzZXIKICAgICAgICAg"
    "ICAgIyAoYWx3YXlzIGJvdGggZGlyZWN0aW9ucyAoZnJvbSBvbmUgY2xpZW50IHRvIGFub3RoZXIp"
    "KQogICAgICAgICAgICBzZWxmLkxvYmJ5R0NUVVJhdGVzID0gQnl0ZVJhdGVMb2dnZXIoKQogICAg"
    "ICAgICAgICBzZWxmLmxhc3RIb3VybHkgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAgZGVm"
    "IGdldFVSYW5kb20oc2VsZik6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJu"
    "dW0gPSByYW5kb20ucmFuZGludCgxLDB4ODAwMCkKICAgICAgICAgICAgd2hpbGUgcm51bSBpbiBz"
    "ZWxmLnVzZWROdW1zOgogICAgICAgICAgICAgICAgcm51bSArPSAxI0Vuc3VyZSB1bmlxdWUKICAg"
    "ICAgICAgICAgc2VsZi51c2VkTnVtcy5hZGQocm51bSkKICAgICAgICAgICAgcmV0dXJuIHJudW0K"
    "ICAgIGRlZiByZWxlYXNlVVJhbmRvbShzZWxmLCBudW0pOgogICAgICAgIHdpdGggc2VsZi5sb2Nr"
    "OgogICAgICAgICAgICBzZWxmLnVzZWROdW1zLmRpc2NhcmQobnVtKSNkaXNjYXJkOiBzYWZlIGV2"
    "ZW4gaWYgYWxyZWFkeSByZWxlYXNlZAogICAgZGVmIHVwZGF0ZURCRnJvbShzZWxmLCB2ZXJzaW9u"
    "KToKICAgICAgICBwcmludCgnRGF0YWJhc2UgVmVyc2lvbjonLHZlcnNpb24pCiAgICAgICAgaWYg"
    "dmVyc2lvbiA+PSBfREJDVVJWRVI6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHByaW50KCdV"
    "cGRhdGluZyBEYXRhYmFzZSB0byBWZXJzaW9uJyxfREJDVVJWRVIpCiAgICAgICAgd2l0aCBzZWxm"
    "LmxvY2s6CiAgICAgICAgICAgIHVwZGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAg"
    "aWYgdmVyc2lvbiA9PSAwOgogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRf"
    "ZGJJbmZvVGFibGUpCiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYklu"
    "Zm9WZXJzaW9uKQogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJVc2Vy"
    "VGFibGUpCiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkZvcm1UYWJs"
    "ZSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICB1cGRjdXIuY2xvc2Uo"
    "KQogICAgZGVmIGdldFBERk4oc2VsZiwgbmFtZSwgZm9ybSwgY3JlYXRlKToKICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgZm9ybWN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAg"
    "ICAgICAgdWlkcmVzID0gZm9ybWN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAobmFtZSwgKSkuZmV0"
    "Y2hvbmUoKQogICAgICAgICAgICBpZiB1aWRyZXMgaXMgTm9uZToKICAgICAgICAgICAgICAgIGZv"
    "cm1jdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1VzZXIgZG9lc24ndCBl"
    "eGlzdAogICAgICAgICAgICBmaWRyZXMgPSBmb3JtY3VyLmV4ZWN1dGUoX1NRTF9mb3JtSUQsIChm"
    "b3JtLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIGZpZHJlcyBpcyBOb25lOiAjZm9ybWF0"
    "IGRvZXMgbm90IGV4aXN0CiAgICAgICAgICAgICAgICBpZiBub3QgY3JlYXRlOgogICAgICAgICAg"
    "ICAgICAgICAgIGZvcm1jdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgICAgIHJldHVybiBOb25l"
    "ICNOZXcgZm9ybWF0IG5vdCBjcmVhdGVkCiAgICAgICAgICAgICAgICBmb3JtY3VyLmV4ZWN1dGUo"
    "X1NRTEFERF9mb3JtSUQsIChmb3JtLCApKQogICAgICAgICAgICAgICAgc2VsZi5kYi5jb21taXQo"
    "KSNUT0RPIENoZWNrIGlmIGdvdHRhIGNvbW1pdCBiZWZvcmUgcmVhZC1iYWNrPwogICAgICAgICAg"
    "ICAgICAgZmlkcmVzID0gZm9ybWN1ci5leGVjdXRlKF9TUUxfZm9ybUlELCAoZm9ybSwgKSkuZmV0"
    "Y2hvbmUoKQogICAgICAgICAgICBmb3JtY3VyLmNsb3NlKCkKICAgICAgICAgICAgZmlkID0gZmlk"
    "cmVzWzBdCiAgICAgICAgICAgIHVpZCA9IHVpZHJlc1swXQogICAgICAgICAgICBmaWxlbmFtZSA9"
    "IF9GT1JNX1BERmlsZS5mb3JtYXQodWlkLCBmaWQpCiAgICAgICAgICAgIGZwYXRoID0gb3MucGF0"
    "aC5qb2luKF9QQVRIX1BMQVlFUkRBVEEsIGZpbGVuYW1lKQogICAgICAgICAgICBpZiBvcy5wYXRo"
    "LmV4aXN0cyhmcGF0aCkgb3IgY3JlYXRlOgogICAgICAgICAgICAgICAgcmV0dXJuIGZwYXRoCiAg"
    "ICAgICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgZ2V0UGxheWVyRGF0YShzZWxmLCBuYW1lLCBm"
    "b3JtKToKICAgICAgICBwYXRoID0gc2VsZi5nZXRQREZOKG5hbWUsIGZvcm0sIEZhbHNlKQogICAg"
    "ICAgIGlmIG5vdCBwYXRoOgogICAgICAgICAgICByZXR1cm4gYicnCiAgICAgICAgcmV0dXJuIHJl"
    "YWRCaW4ocGF0aCkjVE9ETyBkZWZhdWx0IHRvIGInJyBvbiBlcnJvcj8KICAgIGRlZiBzZXRQbGF5"
    "ZXJEYXRhKHNlbGYsIG5hbWUsIGZvcm0sIGRhdGEpOgogICAgICAgIHBhdGggPSBzZWxmLmdldFBE"
    "Rk4obmFtZSwgZm9ybSwgVHJ1ZSkKICAgICAgICBpZiBub3QgcGF0aDojTk8gRklMRSBQQVRILCBU"
    "T0RPIENBVENIIEVSUk9SCiAgICAgICAgICAgIHJldHVybgogICAgICAgIHdpdGggb3BlbihwYXRo"
    "LCAnd2InKSBhcyBmOiNUT0RPIGNhdGNoIGVycm9ycwogICAgICAgICAgICBmLndyaXRlKGRhdGEp"
    "CiAgICBkZWYgZ2V0V2hvaXMoc2VsZiwgbmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAg"
    "ICAgICAgICAgIHdjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHJlcyA9IHdjdXIu"
    "ZXhlY3V0ZShfU1FMX2dldFdob2lzLCAobmFtZSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIHdj"
    "dXIuY2xvc2UoKQogICAgICAgICAgICBpZiByZXMgaXMgTm9uZToKICAgICAgICAgICAgICAgIHJl"
    "dHVybiBOb25lCiAgICAgICAgICAgIChlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVyLCBkZXNj"
    "cmlwdGlvbikgPSByZXMKICAgICAgICAgICAgY3VyWWVhciA9IGRhdGV0aW1lLmRhdGV0aW1lLm5v"
    "dygpLnllYXIKICAgICAgICAgICAgYWdlID0gbWF4KDAsIGN1clllYXIgLSB5b2IpIGlmIHlvYiBl"
    "bHNlIDAKICAgICAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgICAgICdlbWFpbCc6IGVtYWls"
    "IG9yICcnLAogICAgICAgICAgICAgICAgJ2xvY2F0aW9uJzogbG9jYXRpb24gb3IgJycsCiAgICAg"
    "ICAgICAgICAgICAnYWdlJzogYWdlLAogICAgICAgICAgICAgICAgJ2dlbmRlcic6IGdlbmRlciBp"
    "ZiBnZW5kZXIgaXMgbm90IE5vbmUgZWxzZSAwLAogICAgICAgICAgICAgICAgJ2Rlc2NyaXB0aW9u"
    "JzogZGVzY3JpcHRpb24gb3IgJycKICAgICAgICAgICAgfQogICAgZGVmIHVwZGF0ZVdob2lzKHNl"
    "bGYsIG5hbWUsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKToKICAg"
    "ICAgICB0cnk6CiAgICAgICAgICAgIGFnZSA9IGludChhZ2UpCiAgICAgICAgZXhjZXB0IChUeXBl"
    "RXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAgICBhZ2UgPSAwCiAgICAgICAgdHJ5OgogICAg"
    "ICAgICAgICBnZW5kZXIgPSBpbnQoZ2VuZGVyKQogICAgICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBW"
    "YWx1ZUVycm9yKToKICAgICAgICAgICAgZ2VuZGVyID0gMAogICAgICAgIHlvYiA9IGRhdGV0aW1l"
    "LmRhdGV0aW1lLm5vdygpLnllYXIgLSBhZ2UKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgd2N1ci5leGVjdXRlKF9T"
    "UUxVUERfd2hvaXMsIChlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVyLCBkZXNjcmlwdGlvbiwg"
    "bmFtZSkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgd2N1ci5jbG9z"
    "ZSgpCiAgICBkZWYgbG9naW5QbGF5ZXIoc2VsZiwgdXNlcm5hbWUsIGNvbiwgcGFzc3dvcmQpOiNU"
    "T0RPIHNob3VsZCByZXR1cm4gZXJyb3IgcHJvcGVybHkgdG8gY2xpZW50CiAgICAgICAgaWYgbm90"
    "IF9SRV9WQUxJRF9VU0VSTkFNRS5tYXRjaCh1c2VybmFtZSk6CiAgICAgICAgICAgICNSZWdpc3Ry"
    "YXRpb24gaGFzIGFsd2F5cyB2YWxpZGF0ZWQgdGhlIG5hbWU7IGxvZ2dpbmcgaW4gZGlkIG5vdC4K"
    "ICAgICAgICAgICAgI05hbWVzIHJlYWNoIG90aGVyIGNsaWVudHMgaW5zaWRlIHF1b3RlZCBwcm90"
    "b2NvbCBmaWVsZHMsIHNvIGEgbmFtZQogICAgICAgICAgICAjY29udGFpbmluZyAnIicgZm9yZ2Vz"
    "IGNvbW1hbmRzIC0gYW5kIHRoZSBBbGxvd0FueUxvZ2luIGRlYnVnIHBhdGgKICAgICAgICAgICAg"
    "I2JlbG93IG5ldmVyIHRvdWNoZXMgdGhlIGRhdGFiYXNlLCB3aGljaCBtYWRlIGl0IHRoZSBvbmUg"
    "d2F5IHRvIGdldAogICAgICAgICAgICAjc3VjaCBhIG5hbWUgaW4uIENoZWNrIGhlcmUgc28gYm90"
    "aCBwYXRocyBhcmUgY292ZXJlZC4KICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBpZiBf"
    "REVCVUdfQUxMT1dfQU5ZX0xPR0lOOiAjREVCVUcgQVVUTyBBTExPVwogICAgICAgICAgICByZXR1"
    "cm4gVXNlcih1c2VybmFtZSwgY29uKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAg"
    "ICBsb2dpbkN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgI0RlZmF1bHQgdG8gU1RS"
    "SUNULCBUT0RPIGFsbG93IGZvciBub24tc3RyaWN0PwogICAgICAgICAgICB1aWRyZXMgPSBsb2dp"
    "bkN1ci5leGVjdXRlKF9TUUxfdXNlcklEX3N0cmljdCwgKHVzZXJuYW1lLCBjb24uU0spKS5mZXRj"
    "aG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgI3By"
    "aW50KCdsb2dpbiBlcnJvcjogbm8gdXNlciB3aXRoIHRoYXQgc2VyaWFsIGtleScpCiAgICAgICAg"
    "ICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjTm8g"
    "c3VjaCBVc2VyCiAgICAgICAgICAgIHVpZCA9IHVpZHJlc1swXQogICAgICAgICAgICAoclVzZXIs"
    "IHBhc3NoYXNoLCB1U2FsdCwgaEl0cikgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfZ2V0TG9naW4s"
    "ICh1aWQsICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdXNlcm5hbWUgIT0gclVzZXI6CiAg"
    "ICAgICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJvcjogd3JvbmcgdXNlcm5hbWU6IHt1c2Vy"
    "bmFtZX0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAg"
    "cmV0dXJuIE5vbmUgI1dyb25nIFVzZXJuYW1lCiAgICAgICAgICAgIHRwYXMgPSBfc2FsdF9oYXNo"
    "XyhwYXNzd29yZCwgdVNhbHQsIGhJdHIpCiAgICAgICAgICAgIGlmIHRwYXMgIT0gcGFzc2hhc2g6"
    "CiAgICAgICAgICAgICAgICAjcHJpbnQoZidsb2dpbiBlcnJvcjogd3JvbmcgcGFzc3dvcmQ6IHtw"
    "YXNzd29yZH0nKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAg"
    "ICAgcmV0dXJuIE5vbmUgI1dyb25nIFBhc3N3b3JkCiAgICAgICAgICAgIGlmIGhJdHIgIT0gX0hB"
    "U0hJVEVSOgogICAgICAgICAgICAgICAgbnBzaCA9IF9zYWx0X2hhc2hfKHBhc3N3b3JkLCB1U2Fs"
    "dCwgX0hBU0hJVEVSKQogICAgICAgICAgICAgICAgbG9naW5DdXIuZXhlY3V0ZShfU1FMVVBEX3Bh"
    "c3NIYXNoLCAobnBzaCwgX0hBU0hJVEVSLCB1aWQpKQogICAgICAgICAgICB1c2Vyb2JqID0gVXNl"
    "cih1c2VybmFtZSwgY29uKQogICAgICAgICAgICAjdXBkYXRlIGxhc3QgbG9naW4KICAgICAgICAg"
    "ICAgbG9naW5DdXIuZXhlY3V0ZShfU1FMX2xvZ2luVXBkYXRlLCAodXNlcm9iai5sb2dpblRpbWUs"
    "IHVpZCkpCiAgICAgICAgICAgICNUT0RPIGRlZmF1bHQgZGF0ZXRpbWUgYWRhcHRlciBkZXByZWNh"
    "dGVkLCBjaGVjayByZXBsYWNlbWVudAogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAg"
    "ICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgcmV0dXJuIHVzZXJvYmoKICAgIGRl"
    "ZiByZWdpc3RlclBsYXllcihzZWxmLCB1c2VybmFtZSwgY29uLCBwYXNzd29yZCwgZW1haWwsIGxv"
    "Y2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgogICAgICAgIGlmIG5vdCBfUkVfVkFM"
    "SURfVVNFUk5BTUUubWF0Y2godXNlcm5hbWUpOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjSW52"
    "YWxpZCB1c2VybmFtZSAoYmFkIGNoYXJzL2xlbmd0aCksIGFsc28gYmxvY2tzIHByb3RvY29sLWlu"
    "amVjdGlvbiB2aWEgJyInCiAgICAgICAgZW1haWwgPSBzYW5pdGl6ZVRleHQoZW1haWwpCiAgICAg"
    "ICAgbG9jYXRpb24gPSBzYW5pdGl6ZVRleHQobG9jYXRpb24pCiAgICAgICAgZGVzY3JpcHRpb24g"
    "PSBzYW5pdGl6ZVRleHQoZGVzY3JpcHRpb24pCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAg"
    "ICAgICAgIGxvZ2luQ3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBs"
    "b2dpbkN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5hbWUsICkpLmZldGNob25lKCkKICAg"
    "ICAgICAgICAgaWYgdWlkcmVzIGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAgI3ByaW50KGYn"
    "cmVnaXN0ZXIgZXJyb3I6IHVzZXJuYW1lIGFscmVhZHkgaW4gdXNlOiB7dXNlcm5hbWV9JykKICAg"
    "ICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25l"
    "ICNVc2VyIGV4aXN0cwogICAgICAgICAgICAjaWYgc3RyaWN0LCBjaGVjayBpZiBzZXJpYWwgaXMg"
    "aW4gdXNlIHRvbwogICAgICAgICAgICAjVE9ETyBvbmx5IGFwcGx5IGlmIHN0cmljdAogICAgICAg"
    "ICAgICB1aWRyZXMgPSBsb2dpbkN1ci5leGVjdXRlKF9TUUxfdXNlcklEX1NjaGssIChjb24uU0ss"
    "ICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIG5vdCBOb25lOgogICAgICAg"
    "ICAgICAgICAgI3ByaW50KCdyZWdpc3RlciBlcnJvcjogc2VyaWFsIGFscmVhZHkgaW4gdXNlJykK"
    "ICAgICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBO"
    "b25lICNTZXJpYWwgaW4gdXNlIGV4aXN0cwogICAgICAgICAgICB1U2FsdCA9IG9zLnVyYW5kb20o"
    "MTYpCiAgICAgICAgICAgIHBIYXNoID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHVTYWx0LCBfSEFT"
    "SElURVIpCiAgICAgICAgICAgIGN1cnRpbWUgPSBkYXRldGltZS5kYXRldGltZS5ub3coKQogICAg"
    "ICAgICAgICB0cnk6I3RyeSBzaG91bGRuJ3QgYmUgbmVlZGVkIGFzIGVtcHR5IGZpZWxkIGlzIHNl"
    "dCB0byAyNTUKICAgICAgICAgICAgICAgIGFnZSA9IGludChhZ2UpCiAgICAgICAgICAgIGV4Y2Vw"
    "dDoKICAgICAgICAgICAgICAgIGFnZSA9IDAKICAgICAgICAgICAgeW9iID0gY3VydGltZS55ZWFy"
    "IC0gYWdlCiAgICAgICAgICAgIHJlZ3ZhbHMgPSAoCiAgICAgICAgICAgICAgICB1c2VybmFtZSxw"
    "SGFzaCwKICAgICAgICAgICAgICAgIGNvbi5TSyx1U2FsdCxfSEFTSElURVIsCiAgICAgICAgICAg"
    "ICAgICBjdXJ0aW1lLGVtYWlsLGxvY2F0aW9uLHlvYixnZW5kZXIsZGVzY3JpcHRpb24KICAgICAg"
    "ICAgICAgKQogICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxfcmVnaXN0ZXJVc2VyLCBy"
    "ZWd2YWxzKQogICAgICAgICAgICAjVE9ETyBkZWZhdWx0IGRhdGV0aW1lIGFkYXB0ZXIgZGVwcmVj"
    "YXRlZCwgY2hlY2sgcmVwbGFjZW1lbnQKICAgICAgICAgICAgdXNlcm9iaiA9IFVzZXIodXNlcm5h"
    "bWUsIGNvbikKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBsb2dpbkN1"
    "ci5jbG9zZSgpCiAgICAgICAgICAgIHJldHVybiB1c2Vyb2JqCiAgICBkZWYgZGVsZXRlQWNjb3Vu"
    "dChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbiAoR1VJICLQo9C0"
    "0LDQu9C40YLRjCDQv9C10YDRgdC+0L3QsNC20LAiKTogcGVybWFuZW50bHkgcmVtb3ZlcyBhbgog"
    "ICAgICAgICNhY2NvdW50IGFuZCBldmVyeSBzYXZlZCBwbGF5ZXJkYXRhIGJsb2IgZm9yIGl0LiBJ"
    "cnJldmVyc2libGUgLSB0aGUKICAgICAgICAjR1VJIGlzIGV4cGVjdGVkIHRvIGNvbmZpcm0gd2l0"
    "aCB0aGUgYWRtaW4gYmVmb3JlIGNhbGxpbmcgdGhpcy4KICAgICAgICAjRG9lcyBOT1QgdG91Y2gg"
    "dGhlIGNhbGxlcidzIGxpdmUgY29ubmVjdGlvbi9zZXNzaW9uOyB0aGUgY2FsbGVyIGlzCiAgICAg"
    "ICAgI3Jlc3BvbnNpYmxlIGZvciBraWNraW5nIGZpcnN0IGlmIHRoZSBhY2NvdW50IGlzIGN1cnJl"
    "bnRseSBvbmxpbmUKICAgICAgICAjKHNlZSBDb3JlU2VydmVyLmRlbGV0ZUFjY291bnQpLCBvdGhl"
    "cndpc2UgYSBjb25uZWN0ZWQgY2xpZW50IHdvdWxkCiAgICAgICAgI2tlZXAgcGxheWluZyB3aXRo"
    "IGFuIGFjY291bnQgdGhhdCBubyBsb25nZXIgZXhpc3RzIGluIHRoZSBEQi4KICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAg"
    "ICB1aWRyZXMgPSBjdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKHVzZXJuYW1lLCApKS5mZXRjaG9u"
    "ZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNs"
    "b3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgICAgICB1aWQgPSB1aWRy"
    "ZXNbMF0KICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxldGVVc2VyLCAodXNlcm5hbWUs"
    "ICkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkK"
    "ICAgICAgICAjUGxheWVyZGF0YSBmaWxlcyAoInt1c2VySUQ6eH1fe2Zvcm1JRDp4fS5iaW4iKSBs"
    "aXZlIG91dHNpZGUgdGhlIERCCiAgICAgICAgI3RyYW5zYWN0aW9uIGFuZCBhcmUgbG9va2VkIHVw"
    "IGJ5IHByZWZpeCAtIGJlc3QgZWZmb3J0LCBhIGxlZnRvdmVyCiAgICAgICAgI2ZpbGUgaGVyZSBp"
    "c24ndCB3b3J0aCBmYWlsaW5nIHRoZSB3aG9sZSBkZWxldGlvbiBvdmVyLgogICAgICAgIHByZWZp"
    "eCA9IGYne3VpZDp4fV8nCiAgICAgICAgdHJ5OgogICAgICAgICAgICBmb3IgZm4gaW4gb3MubGlz"
    "dGRpcihfUEFUSF9QTEFZRVJEQVRBKToKICAgICAgICAgICAgICAgIGlmIGZuLnN0YXJ0c3dpdGgo"
    "cHJlZml4KToKICAgICAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAgICAgICAg"
    "IG9zLnJlbW92ZShvcy5wYXRoLmpvaW4oX1BBVEhfUExBWUVSREFUQSwgZm4pKQogICAgICAgICAg"
    "ICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICAgICAgICAgICAgICBwYXNzCiAg"
    "ICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MKICAgICAgICByZXR1cm4gVHJ1"
    "ZQogICAgZGVmIGRlYnVnX3VzZXJsaXN0KHNlbGYsIGFjY2Vzc1Rva2VuLCBwYWdlPTApOgogICAg"
    "ICAgIHBhc3MgIyBUT0RPIGRlYnVnIGxpc3Qgb2YgYWxsIHVzZXJzCiAgICAgICAgI2VsZXZhdGVk"
    "IGFjY2VzcyBvbmx5PyBvcHRpb25hbGx5PwogICAgICAgICNJRCwgTkFNRSwgTEFTVExPR0lOCiAg"
    "ICAgICAgI3dob2lzIGV4dHJhcyBFTUFJTCwgTE9DQVRJT04sIFlPQiwgR0VOREVSLCBERVNDUklQ"
    "VElPTgogICAgICAgICNwbGF5ZXJkYXRhIGZpbGVzIGFuZCBzaXplcz8gb3B0aW9uYWwKICAgICNU"
    "T0RPIGNvbnZlcnQgdG8gbW9yZSBkeW5hbWljIGZvcm1hdAogICAgZGVmIGRlYnVnX3NlcnZlcl9p"
    "bmZvKHNlbGYsIGFjY2Vzc1Rva2VuKToKICAgICAgICBwYXNzICNlbGV2YXRlZCBhY2Nlc3Mgb25s"
    "eT8gb3B0aW9uYWxseT8KICAgICAgICAjX0RCSU5GTzogVkVSU0lPTgogICAgZGVmIHJhdGVNb25p"
    "dG9yVXBkYXRlKHNlbGYsIHRpY2spOgogICAgICAgIHNlbGYuY29uc29saWRhdGVTaG9ydCgpI3Nl"
    "Y29uZAogICAgICAgIGlmIHRpY2slNjAgPT0gMDojbWludXRlCiAgICAgICAgICAgIHNlbGYuY29u"
    "c29saWRhdGVNaWQoKQogICAgICAgIGlmIHRpY2slKDYwKjYwKSA9PSAwOiNob3VyCiAgICAgICAg"
    "ICAgIHNlbGYuY29uc29saWRhdGVMb25nKCkKICAgICAgICAgICAgc2VsZi5sYXN0SG91cmx5ID0g"
    "ZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCkKICAgIGRlZiB1cGRhdGUoc2VsZiwgdGljayk6CiAgICAg"
    "ICAgaWYgX0VOQUJMRV9ERUJVR19SQVRFTU9OSVRPUjoKICAgICAgICAgICAgc2VsZi5yYXRlTW9u"
    "aXRvclVwZGF0ZSh0aWNrKSAjd2FzIGEgYmFyZSByYXRlTW9uaXRvclVwZGF0ZShzZWxmLCB0aWNr"
    "KToKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAjbm8gc3VjaCBnbG9i"
    "YWwsIE5hbWVFcnJvciBldmVyeSB0aWNrCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgI29uY2UgdGhlIHJhdGUgbW9uaXRvciB3YXMgZW5hYmxlZAogICAgZGVmIGNvbnNv"
    "bGlkYXRlU2hvcnQoc2VsZik6ICNldmVyeSBzZXJ2aWNlIGludGVydmFsPyAoc2Vjb25kKQogICAg"
    "ICAgIHNlbGYuTG9iYnlTZW5kUmF0ZXMuY29uc29saWRhdGVTaG9ydCgpCiAgICAgICAgc2VsZi5M"
    "b2JieVJlY3ZSYXRlcy5jb25zb2xpZGF0ZVNob3J0KCkKICAgICAgICBzZWxmLkxvYmJ5R0NUVVJh"
    "dGVzLmNvbnNvbGlkYXRlU2hvcnQoKQogICAgZGVmIGNvbnNvbGlkYXRlTWlkKHNlbGYpOiAjb24g"
    "dGhlIG1pbnV0ZQogICAgICAgIHNlbGYuTG9iYnlTZW5kUmF0ZXMuY29uc29saWRhdGVNaWQoKQog"
    "ICAgICAgIHNlbGYuTG9iYnlSZWN2UmF0ZXMuY29uc29saWRhdGVNaWQoKQogICAgICAgIHNlbGYu"
    "TG9iYnlHQ1RVUmF0ZXMuY29uc29saWRhdGVNaWQoKQogICAgZGVmIGNvbnNvbGlkYXRlTG9uZyhz"
    "ZWxmKTogI29uIHRoZSBob3VyCiAgICAgICAgc2VsZi5Mb2JieVNlbmRSYXRlcy5jb25zb2xpZGF0"
    "ZUxvbmcoKQogICAgICAgIHNlbGYuTG9iYnlSZWN2UmF0ZXMuY29uc29saWRhdGVMb25nKCkKICAg"
    "ICAgICBzZWxmLkxvYmJ5R0NUVVJhdGVzLmNvbnNvbGlkYXRlTG9uZygpCiAgICBkZWYgZ2V0Qnl0"
    "ZXJhdGVIb3VyKHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICdsYXN0SG91cmx5"
    "Jzpqc29uVGltZShzZWxmLmxhc3RIb3VybHkpLAogICAgICAgICAgICAnU2VuZCc6dHVwbGUoc2Vs"
    "Zi5Mb2JieVNlbmRSYXRlcy5sQnl0ZXMpLAogICAgICAgICAgICAnUmVjdic6dHVwbGUoc2VsZi5M"
    "b2JieVJlY3ZSYXRlcy5sQnl0ZXMpLAogICAgICAgICAgICAnR0NUVSc6dHVwbGUoc2VsZi5Mb2Ji"
    "eUdDVFVSYXRlcy5sQnl0ZXMpCiAgICAgICAgfQogICAgZGVmIGdldEJ5dGVyYXRlTWludXRlKHNl"
    "bGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICdTZW5kJzp0dXBsZShzZWxmLkxvYmJ5"
    "U2VuZFJhdGVzLm1CeXRlcyksCiAgICAgICAgICAgICdSZWN2Jzp0dXBsZShzZWxmLkxvYmJ5UmVj"
    "dlJhdGVzLm1CeXRlcyksCiAgICAgICAgICAgICdHQ1RVJzp0dXBsZShzZWxmLkxvYmJ5R0NUVVJh"
    "dGVzLm1CeXRlcykKICAgICAgICB9CiAgICBkZWYgZ2V0Qnl0ZXJhdGVTZWNvbmQoc2VsZik6CiAg"
    "ICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgJ1NlbmQnOnR1cGxlKHNlbGYuTG9iYnlTZW5kUmF0"
    "ZXMuc0J5dGVzKSwKICAgICAgICAgICAgJ1JlY3YnOnR1cGxlKHNlbGYuTG9iYnlSZWN2UmF0ZXMu"
    "c0J5dGVzKSwKICAgICAgICAgICAgJ0dDVFUnOnR1cGxlKHNlbGYuTG9iYnlHQ1RVUmF0ZXMuc0J5"
    "dGVzKQogICAgICAgIH0KY2xhc3MgQnl0ZVJhdGVMb2dnZXIoKTogIyBUT0RPIG1ha2UgbW9kaWZp"
    "ZWQgdmVyc2lvbiB0byB0cmFjayBwbGF5ZXJjb3VudAogICAgZGVmIF9faW5pdF9fKHNlbGYpOgog"
    "ICAgICAgIHNlbGYuX2J5dGVsb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIHNlbGYuYnl0"
    "ZXJhdGUgPSAwCiAgICAgICAgc2VsZi5zQnl0ZXMgPSBkZXF1ZShtYXhsZW49NjApCiAgICAgICAg"
    "c2VsZi5tQnl0ZXMgPSBkZXF1ZShtYXhsZW49NjApCiAgICAgICAgc2VsZi5sQnl0ZXMgPSBkZXF1"
    "ZShtYXhsZW49X1BFUkZfTE9HX0xJTUlUKQogICAgZGVmIGxvZ0J5dGVzKHNlbGYsIGNudCk6CiAg"
    "ICAgICAgd2l0aCBzZWxmLl9ieXRlbG9jazoKICAgICAgICAgICAgc2VsZi5ieXRlcmF0ZSArPSBj"
    "bnQKICAgIGRlZiBjb25zb2xpZGF0ZVNob3J0KHNlbGYpOgogICAgICAgIHdpdGggc2VsZi5fYnl0"
    "ZWxvY2s6CiAgICAgICAgICAgIHRiciA9IHNlbGYuYnl0ZXJhdGUKICAgICAgICAgICAgc2VsZi5i"
    "eXRlcmF0ZSA9IDAKICAgICAgICBzZWxmLnNCeXRlcy5hcHBlbmQodGJyKQogICAgZGVmIGNvbnNv"
    "bGlkYXRlTWlkKHNlbGYpOgogICAgICAgIHdpdGggc2VsZi5fYnl0ZWxvY2s6CiAgICAgICAgICAg"
    "IHRiciA9IHN1bShzZWxmLnNCeXRlcykKICAgICAgICBzZWxmLm1CeXRlcy5hcHBlbmQodGJyKQog"
    "ICAgZGVmIGNvbnNvbGlkYXRlTG9uZyhzZWxmKToKICAgICAgICB3aXRoIHNlbGYuX2J5dGVsb2Nr"
    "OgogICAgICAgICAgICB0YnIgPSBzdW0oc2VsZi5tQnl0ZXMpCiAgICAgICAgc2VsZi5sQnl0ZXMu"
    "YXBwZW5kKHRicikKI1RPRE8gY29uc2lkZXIgcGxheWVyY291bnQgbG9nZ2VyLCBsb2dzIG1heCgp"
    "IG5vdCBzdW0oKSBidXQgb3RoZXJ3aXNlIGlkZW50aWNhbAojLSBzaW1wbGUgbWF4KCkgbG9ncyBw"
    "ZWFrIHBsYXllciBwb3Agb25seSwgbG9nIHNldHMgb2YgdXNlcklEcyBmb3IgdW5pcXVlIHBsYXll"
    "cnM/CgpHREggPSBEYXRhSGFuZGxlcigpCgpkZWYgX3dvVXNlcih1bCwgdXNyKToKICAgIHJldHVy"
    "biBsaXN0KCAoYSBmb3IgYSBpbiB1bCBpZiBhIGlzIG5vdCB1c3IpICkKZGVmIF9SZWFkQmxvYihj"
    "b24sIHNpemUpOgogICAgI3NpemUgY29tZXMgc3RyYWlnaHQgb2ZmIHRoZSB3aXJlLCBzbyBpdCBp"
    "cyBuZWl0aGVyIHRydXN0ZWQgdG8gYmUgYSBudW1iZXIKICAgICNub3IgdG8gYmUgc2FuZTogYSBj"
    "bGllbnQgY2xhaW1pbmcgYSBodWdlIGxlbmd0aCB1c2VkIHRvIG1ha2UgdGhlIHNlcnZlcgogICAg"
    "I2J1ZmZlciB1bmJvdW5kZWRseSAobWVtb3J5IGV4aGF1c3Rpb24pLCBhbmQgYSBjbGllbnQgdGhh"
    "dCBkaXNjb25uZWN0ZWQKICAgICNtaWQtYmxvYiBtYWRlIHJlY3YoKSByZXR1cm4gYicnIGZvcmV2"
    "ZXIgLSBhIDEwMCUgQ1BVIGJ1c3ktbG9vcCwgdGhlIHNhbWUKICAgICNkZWZlY3QgYWxyZWFkeSBm"
    "aXhlZCBpbiBDb25uZWN0aW9uSGFuZGxlci5fcmVjdk1vcmUoKS4KICAgIHRyeToKICAgICAgICBz"
    "aXplID0gaW50KHNpemUpCiAgICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAg"
    "ICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2JhZCBibG9iIHNpemUge3NpemUhcn0nKQogICAgaWYg"
    "c2l6ZSA8IDAgb3Igc2l6ZSA+IF9NQVhfQkxPQjoKICAgICAgICByYWlzZSBQcm90b2NvbEVycm9y"
    "KGYnYmxvYiBzaXplIHtzaXplfSBvdXQgb2YgcmFuZ2UgKG1heCB7X01BWF9CTE9CfSknKQogICAg"
    "d2hpbGUgbGVuKGNvbi5kYXRhKSA8IHNpemU6CiAgICAgICAgY2h1bmsgPSBjb24ucmVxdWVzdC5y"
    "ZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAgICAgIHJhaXNl"
    "IENvbm5lY3Rpb25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGJsb2IgcmVhZCcpCiAg"
    "ICAgICAgY29uLmRhdGEgKz0gY2h1bmsKICAgIGJsYnVmID0gY29uLmRhdGFbMDpzaXplXQogICAg"
    "Y29uLmRhdGEgPSBjb24uZGF0YVtzaXplOl0KICAgIHJldHVybiBibGJ1ZgoKI0NvbW1hbmQgZnVu"
    "Y3Rpb25zCmRlZiBfbm9wKG1kLHVzcixyZXMpOgogICAgcmV0dXJuIE5vbmUKZGVmIF91cGRoZXJv"
    "cG9zKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAg"
    "IHJldHVybiBOb25lICNub3QgaW4gYSBnYW1lIGNoYW5uZWwsIGlnbm9yZQogICAgdXNyLnVzZXIu"
    "cG9zZGF0YSA9IHJlc1sxXSMgInh4eHgjeXl5eSIgcmVzcCAiVUlEI3h4eHgjeXl5eSIKICAgIHVz"
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
    "CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gY2hhbm5lbCwgaWdub3JlCiAgICB1c3IudXNl"
    "ci5wb3NkYXRhID0gcmVzWzJdCiAgICByZXR1cm4gY2hubC5qb2luQ2hhbm5lbCh1c3IsIHJlc1sx"
    "XSkKZGVmIF9zZXR1c2VyaGVyb2RhdGEobWQsdXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1"
    "c3IsIHJlc1syXSkKICAgIHVzci51c2VyLmhlcm9kYXRhID0gcGQKICAgIGlmIHVzci51c2VyLmdh"
    "bWVjaGFubmVsOgogICAgICAgIG1zZyA9IHVzci51c2VyLmdldEdDVW1zZygpCiAgICAgICAgdGcg"
    "PSBfd29Vc2VyKHVzci51c2VyLmdhbWVjaGFubmVsLnVzZXJsaXN0LCB1c3IpCiAgICAgICAgbWQu"
    "YWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBOb25lCmRlZiBfc2Vu"
    "ZChtZCx1c3IscmVzKToKICAgICNUT0RPIGNvbnNpZGVyIHNwZWNpYWwgY2hhdCBjb21tYW5kcyBo"
    "ZXJlCiAgICBpZiBub3QgdXNyLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUK"
    "ICAgIGlmIGxlbihyZXMpPDI6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHRleHQgPSBzYW5pdGl6"
    "ZVRleHQocmVzWzFdKQogICAgaWYgbm90IHRleHQ6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHVs"
    "ID0gdXNyLnVzZXIuY2hhdGNoYW5uZWwKICAgIG1kLmFkZCh7J3RhcmdldCc6dWwsJ21lc3NhZ2Un"
    "Ol9lbShmJy9zZW5kICJ7dXNyLnVzZXIubmFtZX0iICJ7dGV4dH0iJyl9KQogICAgcmV0dXJuIE5v"
    "bmUKZGVmIF9nZXRndWlsZHJhbmtwb2ludHMobWQsdXNyLHJlcyk6CiAgICAoYSxiLGMsZCkgPSBf"
    "Z3JwKCkKICAgIHJldHVybiBfZW0oZicvZ2V0Z3VpbGRyYW5rcG9pbnRzICJ7YX0iICJ7Yn0iICJ7"
    "Y30iICJ7ZH0iJykKZGVmIF9yZXF1ZXN0Y3JlYXRlZ2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5v"
    "dCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9uZSAjbm90IGluIGEgZ2Ft"
    "ZSBjaGFubmVsIC0gdXNlZCB0byByYWlzZSBBdHRyaWJ1dGVFcnJvciBvbgogICAgICAgICAgICAg"
    "ICAgICAgICNOb25lIGFuZCBraWxsIHRoZSBjb25uZWN0aW9uJ3MgaGFuZGxlciB0aHJlYWQKICAg"
    "IHJldHVybiB1c3IudXNlci5nYW1lY2hhbm5lbC5yZXF1ZXN0Q3JlYXRlR2FtZSh1c3IsIHJlc1sx"
    "XSkKZGVmIF9jcmVhdGVHYW1lKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVj"
    "aGFubmVsOgogICAgICAgIHJldHVybiBOb25lICNzZWUgX3JlcXVlc3RjcmVhdGVnYW1lCiAgICBy"
    "ZXR1cm4gdXNyLnVzZXIuZ2FtZWNoYW5uZWwuY3JlYXRlR2FtZShyZXNbMV0sIHVzciwgcmVzWzJd"
    "LCByZXNbM10sIHJlc1s0XSwgcmVzWzVdLCByZXNbNl0sIHJlc1s3XSwgcmVzWzhdLCByZXNbOV0p"
    "CmRlZiBfc3RvcGdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAg"
    "IHJldHVybiB1c3IudXNlci5nYW1lLnJlbW92ZSh1c3IpCiAgICAjcHJpbnQoJ1VzZXIgaXMgbm90"
    "IGluIGEgZ2FtZScpCiAgICByZXR1cm4gTm9uZQpkZWYgX3N0YXJ0aW5nZ2FtZShtZCx1c3IscmVz"
    "KToKICAgIGlmIHVzci51c2VyLmdhbWU6CiAgICAgICAgcmV0dXJuIHVzci51c2VyLmdhbWUuc3Rh"
    "cnRHYW1lKHVzcikKICAgIHJldHVybiBOb25lICNUT0RPIHdoYXQgZG9lcyB0aGlzIGV2ZW4gZG8/"
    "CmRlZiBfc3RhcnRnYW1lKG1kLHVzcixyZXMpOgogICAgI1RPRE8gaGFuZGxlIHByb3Blcmx5CiAg"
    "ICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHBhc3MKICAgIHJldHVybiBOb25lCmRlZiBfZ2Ft"
    "ZWNvbW1hbmR0b3VzZXIobWQsdXNyLHJlcyk6CiAgICBkYXQgPSBfUmVhZEJsb2IodXNyLCByZXNb"
    "Ml0pCiAgICB0Y29uID0gdXNyLnNlcnZlci5nZXRQbGF5ZXIocmVzWzFdKQogICAgI0FsbG93IGNv"
    "bW1hbmRzIHRvIGFueSBjb25uZWN0ZWQgcGxheWVyLCByZWdhcmRsZXNzIG9mIHN0YXRlLCB0byBz"
    "dXBwb3J0IG1vZGRlZCB1c2VzCiAgICBpZiBub3QgdGNvbjoKICAgICAgICAjcHJpbnQoJ1BsYXll"
    "cjonLHJlc1sxXSwnZG9lcyBub3QgZXhpc3Q/JykKICAgICAgICByZXR1cm4gTm9uZQogICAgI1RP"
    "RE8gY29uc2lkZXIgb3B0aW1pc2luZyB0aGlzIGNvbW1hbmQgaW4gcGFydGljdWxhcgogICAgZnVs"
    "bXNnID0gX2VtKGYnL2dhbWVjb21tYW5kdG91c2VyICJ7dXNyLnVzZXIubmFtZX0iICJ7bGVuKGRh"
    "dCl9IicpK2RhdAogICAgbWQuYWRkKHsndGFyZ2V0JzoodGNvbiwgKSwnbWVzc2FnZSc6ZnVsbXNn"
    "fSkKICAgIGlmIF9FTkFCTEVfREVCVUdfUkFURU1PTklUT1I6CiAgICAgICAgR0RILkxvYmJ5R0NU"
    "VVJhdGVzLmxvZ0J5dGVzKGxlbihmdWxtc2cpKQogICAgcmV0dXJuIE5vbmUKZGVmIF9qb2luZ2Ft"
    "ZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICBy"
    "ZXR1cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBjaGFubmVsCiAgICBnbSA9IHVzci51c2VyLmdhbWVj"
    "aGFubmVsLmdhbWVzLmdldChyZXNbMV0sTm9uZSkKICAgIGlmIGdtID09IE5vbmU6CiAgICAgICAg"
    "cmV0dXJuIE5vbmUgI1RPRE8gZXJyb3I/CiAgICByZXR1cm4gZ20uYWRkVXNlcih1c3IsIHJlc1sy"
    "XSkKZGVmIF93aG9pcyhtZCx1c3IscmVzKToKICAgIGlmIGxlbihyZXMpPDI6CiAgICAgICAgcmV0"
    "dXJuIE5vbmUKICAgIHRhcmdldCA9IHJlc1sxXQogICAgaW5mbyA9IEdESC5nZXRXaG9pcyh0YXJn"
    "ZXQpCiAgICBpZiBpbmZvIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUgI3Vua25vd24gdXNl"
    "cgogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHRhcmdldCkKICAgIHRvd24gPSB0Y29u"
    "LnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiAodGNvbiBhbmQgdGNvbi51c2VyLmdhbWVjaGFubmVs"
    "KSBlbHNlICcnCiAgICBjaGF0Y2hhbm5lbCA9ICcnCiAgICBpZiB0Y29uIGFuZCB0Y29uLnVzZXIu"
    "Y2hhdGNoYW5uZWw6CiAgICAgICAgZm9yIGNobiBpbiB1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFu"
    "bmVscy52YWx1ZXMoKToKICAgICAgICAgICAgZm9yIGNuYW1lLCB1bGlzdCBpbiBjaG4uY2hhdENo"
    "YW5uZWxzLml0ZW1zKCk6CiAgICAgICAgICAgICAgICBpZiB1bGlzdCBpcyB0Y29uLnVzZXIuY2hh"
    "dGNoYW5uZWw6CiAgICAgICAgICAgICAgICAgICAgY2hhdGNoYW5uZWwgPSBjbmFtZQogICAgZ3Vp"
    "bGQgPSAnJyNndWlsZHMgbm90IGltcGxlbWVudGVkCiAgICByZXR1cm4gX2VtKAogICAgICAgIGYn"
    "L3dob2lzICJ7dGFyZ2V0fSIgIntndWlsZH0iICJ7c2FuaXRpemVUZXh0KHRvd24pfSIgIntzYW5p"
    "dGl6ZVRleHQoY2hhdGNoYW5uZWwpfSIgJwogICAgICAgIGYnIntzYW5pdGl6ZVRleHQoaW5mb1si"
    "ZW1haWwiXSl9IiAie3Nhbml0aXplVGV4dChpbmZvWyJsb2NhdGlvbiJdKX0iICcKICAgICAgICBm"
    "J3tpbmZvWyJhZ2UiXX0ge2luZm9bImdlbmRlciJdfSAie3Nhbml0aXplVGV4dChpbmZvWyJkZXNj"
    "cmlwdGlvbiJdKX0iJwogICAgKQpkZWYgX3VwZGF0ZShtZCx1c3IscmVzKToKICAgICMvdXBkYXRl"
    "ICJuYW1lIiAiZW1haWwiICJsb2NhdGlvbiIgImFnZSIgImdlbmRlciIgImRlc2NyaXB0aW9uIgog"
    "ICAgaWYgbGVuKHJlcyk8NjoKICAgICAgICByZXR1cm4gTm9uZQogICAgaWYgcmVzWzFdICE9IHVz"
    "ci51c2VyLm5hbWU6CiAgICAgICAgcmV0dXJuIE5vbmUgI2NhbiBvbmx5IHVwZGF0ZSBvd24gd2hv"
    "aXMgaW5mbwogICAgZW1haWwgPSBzYW5pdGl6ZVRleHQocmVzWzJdKQogICAgbG9jYXRpb24gPSBz"
    "YW5pdGl6ZVRleHQocmVzWzNdKQogICAgYWdlID0gcmVzWzRdCiAgICBnZW5kZXIgPSByZXNbNV0K"
    "ICAgIGRlc2NyaXB0aW9uID0gc2FuaXRpemVUZXh0KHJlc1s2XSkgaWYgbGVuKHJlcyk+NiBlbHNl"
    "ICcnCiAgICBHREgudXBkYXRlV2hvaXModXNyLnVzZXIubmFtZSwgZW1haWwsIGxvY2F0aW9uLCBh"
    "Z2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICByZXR1cm4gTm9uZSAjc2VydmVyIHNlbmRzIG5v"
    "IHJlc3BvbnNlLCBwZXIgcHJvdG9jb2wgZG9jCgpfUkVfQ01EID0gcmUuY29tcGlsZShyJyg/OiIo"
    "W14iXSopIil8KFteXHNdKyknKQojY29tbWFuZCAtPiAoaGFuZGxlciwgbWluaW11bSBhcmd1bWVu"
    "dCBjb3VudCAqZXhjbHVkaW5nKiB0aGUgY29tbWFuZCB3b3JkKS4KI1RoZSBjb3VudCBpcyBlbmZv"
    "cmNlZCBvbmNlLCBjZW50cmFsbHksIGluIHBhcnNlKCk6IGV2ZXJ5IGhhbmRsZXIgaW5kZXhlcyBp"
    "bnRvCiNyZXNbXSBwb3NpdGlvbmFsbHksIHNvIGEgY2xpZW50IHNlbmRpbmcgYSBjb21tYW5kIHdp"
    "dGggZmV3ZXIgYXJndW1lbnRzIHRoYW4KI2V4cGVjdGVkIHVzZWQgdG8gcmFpc2UgSW5kZXhFcnJv"
    "ciBhbmQgdGVhciBkb3duIGl0cyBvd24gY29ubmVjdGlvbiB0aHJlYWQuCiNEZWNsYXJpbmcgdGhl"
    "IGFyaXR5IGhlcmUga2VlcHMgdGhhdCBjaGVjayBpbiBvbmUgcGxhY2UgaW5zdGVhZCBvZiByZXBl"
    "YXRpbmcgYQojbGVuKHJlcykgZ3VhcmQgYXQgdGhlIHRvcCBvZiBmaWZ0ZWVuIGhhbmRsZXJzLgpf"
    "Q09NTUFORFMgPSB7CiAgICAnL25vcCc6ICAgICAgICAgICAgICAgICAgICAoX25vcCwgMCksCiAg"
    "ICAnL2xlYXZlZ2FtZWNoYW5uZWwnOiAgICAgICAoX2xlYXZlZ2FtZWNoYW5uZWwsIDApLAogICAg"
    "Jy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsJzogKF9yZXF1ZXN0am9pbmdhbWVjaGFubmVsLCAxKSwK"
    "ICAgICcvam9pbmdhbWVjaGFubmVsJzogICAgICAgIChfam9pbmdhbWVjaGFubmVsLCAyKSwKICAg"
    "ICcvdXBkaGVyb3Bvcyc6ICAgICAgICAgICAgIChfdXBkaGVyb3BvcywgMSksCiAgICAnL3NlbmQn"
    "OiAgICAgICAgICAgICAgICAgICAoX3NlbmQsIDEpLAogICAgJy9nZXRndWlsZHJhbmtwb2ludHMn"
    "OiAgICAgKF9nZXRndWlsZHJhbmtwb2ludHMsIDApLAogICAgJy9yZXF1ZXN0Y3JlYXRlZ2FtZSc6"
    "ICAgICAgKF9yZXF1ZXN0Y3JlYXRlZ2FtZSwgMSksCiAgICAnL2NyZWF0ZWdhbWUnOiAgICAgICAg"
    "ICAgICAoX2NyZWF0ZUdhbWUsIDkpLAogICAgJy9zdG9wZ2FtZSc6ICAgICAgICAgICAgICAgKF9z"
    "dG9wZ2FtZSwgMCksCiAgICAnL2xlYXZlZ2FtZSc6ICAgICAgICAgICAgICAoX3N0b3BnYW1lLCAw"
    "KSwjVE9ETyBmaXggZm9yIG11bHRpcGxlIHVzZXJzPwogICAgJy9zdGFydGluZ2dhbWUnOiAgICAg"
    "ICAgICAgKF9zdGFydGluZ2dhbWUsIDApLAogICAgJy9zdGFydGdhbWUnOiAgICAgICAgICAgICAg"
    "KF9zdGFydGdhbWUsIDApLAogICAgJy9nZXRwbGF5ZXJkYXRhJzogICAgICAgICAgKF9nZXRwbGF5"
    "ZXJkYXRhLCAyKSwKICAgICcvc2V0cGxheWVyZGF0YSc6ICAgICAgICAgIChfc2V0cGxheWVyZGF0"
    "YSwgMyksCiAgICAnL3NldHVzZXJoZXJvZGF0YSc6ICAgICAgICAoX3NldHVzZXJoZXJvZGF0YSwg"
    "MiksCiAgICAnL2dhbWVjb21tYW5kdG91c2VyJzogICAgICAoX2dhbWVjb21tYW5kdG91c2VyLCAy"
    "KSwjVE9ETyBjb25zaWRlciBvcHRpbWlzaW5nCiAgICAnL2pvaW5nYW1lJzogICAgICAgICAgICAg"
    "ICAoX2pvaW5nYW1lLCAyKSwKICAgICcvd2hvaXMnOiAgICAgICAgICAgICAgICAgIChfd2hvaXMs"
    "IDEpLAogICAgJy91cGRhdGUnOiAgICAgICAgICAgICAgICAgKF91cGRhdGUsIDUpLAp9CmNsYXNz"
    "IENvbW1hbmRQYXJzZXIoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBtc2dlcik6CiAgICAgICAg"
    "c2VsZi5jb21tYW5kbGlzdCA9IF9DT01NQU5EUwogICAgICAgIHNlbGYubWQgPSBtc2dlcgoKICAg"
    "IGRlZiBwYXJzZShzZWxmLCBkYXRhLCBvcmlnaW4pOgogICAgICAgICNwcmludChmJ1Rlc3QgUGFy"
    "c2luZyB7bGVuKGRhdGEpfToge2J5dGVzKGRhdGEsICdhc2NpaScpfScpCiAgICAgICAgcmVzID0g"
    "bGlzdCggKGl0bVswXStpdG1bMV0gZm9yIGl0bSBpbiBfUkVfQ01ELmZpbmRhbGwoZGF0YSkpICkK"
    "ICAgICAgICAjcHJpbnQoJ1JlczonLCByZXMpCiAgICAgICAgaWYgbm90IHJlczoKICAgICAgICAg"
    "ICAgcmV0dXJuIE5vbmUKICAgICAgICBlbnRyeSA9IHNlbGYuY29tbWFuZGxpc3QuZ2V0KHJlc1sw"
    "XSkKICAgICAgICBpZiBlbnRyeSBpcyBOb25lOgogICAgICAgICAgICBwcmludChmJ1Vua25vd24g"
    "Q29tbWFuZCBGcm9tIHtvcmlnaW4udXNlci5uYW1lfTonLCByZXMpCiAgICAgICAgICAgIHJldHVy"
    "biBOb25lCiAgICAgICAgaGFuZGxlciwgbWluYXJncyA9IGVudHJ5CiAgICAgICAgaWYgbGVuKHJl"
    "cykgLSAxIDwgbWluYXJnczoKICAgICAgICAgICAgcHJpbnQoZidNYWxmb3JtZWQgQ29tbWFuZCBG"
    "cm9tIHtvcmlnaW4udXNlci5uYW1lfTogJwogICAgICAgICAgICAgICAgICBmJ3tyZXNbMF19IG5l"
    "ZWRzIHttaW5hcmdzfSBhcmd1bWVudChzKSwgZ290IHtsZW4ocmVzKS0xfScpCiAgICAgICAgICAg"
    "IHJldHVybiBOb25lCiAgICAgICAgI3ByaW50KGYnUGFyc2VkIENvbW1hbmQgRnJvbSB7b3JpZ2lu"
    "LnVzZXIubmFtZX06JywgcmVzKQogICAgICAgIHJldHVybiBoYW5kbGVyKHNlbGYubWQsIG9yaWdp"
    "biwgcmVzKQoKI3RocmVhZCB0byBzZW5kIG1lc3NhZ2VzIGFjcm9zcyBhbGwgY29ubmVjdGVkIGNs"
    "aWVudHMKI19fRVhBTVBMRV9NRVNTQUdFX18gPSB7CiMgICAgJ3RhcmdldCc6Wyd1c2VybGlzdCdd"
    "LAojICAgICdtZXNzYWdlJzpiJy93aGF0ZXZlclwwJytiJ2Jsb2InCiN9CmNsYXNzIE1lc3NhZ2VE"
    "aXN0cmlidXRvcigpOgogICAgX0VORElURU0gPSBbJ1NUT1AnXQogICAgZGVmIF9faW5pdF9fKHNl"
    "bGYsIHNlcnZlcik6CiAgICAgICAgc2VsZi5fY1F1ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAg"
    "IHNlbGYuc2VydmVyID0gc2VydmVyCiAgICBkZWYgc2VydmVfZm9yZXZlcihzZWxmKToKICAgICAg"
    "ICB3aGlsZSBUcnVlOiAjVE9ETyBwb3NzaWJsZSBjaGVjayBzZWxmLnNlcnZlci5faXNfY2xvc2lu"
    "ZwogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBjb21tYW5kID0gc2VsZi5fY1F1ZXVl"
    "LmdldCgpCiAgICAgICAgICAgICAgICAjcHJpbnQoJ01EOicsIGNvbW1hbmQsIHNlbGYuc2VydmVy"
    "Ll9pc19jbG9zaW5nKQogICAgICAgICAgICAgICAgaWYgY29tbWFuZCA9PSBzZWxmLl9FTkRJVEVN"
    "OgogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICB1bCA9IGNvbW1hbmQu"
    "Z2V0KCd0YXJnZXQnLFtdKQogICAgICAgICAgICAgICAgbXNnID0gY29tbWFuZC5nZXQoJ21lc3Nh"
    "Z2UnKQogICAgICAgICAgICAgICAgaWYgbXNnOgogICAgICAgICAgICAgICAgICAgIGZvciB1c3Ig"
    "aW4gdWw6CiAgICAgICAgICAgICAgICAgICAgICAgIHVzci5zZW5kKG1zZykKICAgICAgICAgICAg"
    "ZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIERpc3RyaWJ1"
    "dG9yIGVycm9yOlxuJyArIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICBkZWYgYWRkKHNlbGYs"
    "IHByb3BzKToKICAgICAgICAjU25hcHNob3QgdGhlIHRhcmdldCBsaXN0IEhFUkUsIGluIHRoZSBj"
    "YWxsaW5nIHRocmVhZC4gQ2FsbGVycyBoYW5kIHVzCiAgICAgICAgI2xpdmUgY29udGFpbmVycyAo"
    "R2FtZUNoYW5uZWwudXNlcmxpc3QsIHN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpLCAuLi4pCiAg"
    "ICAgICAgI3RoYXQgb3RoZXIgaGFuZGxlciB0aHJlYWRzIGFwcGVuZCB0by9yZW1vdmUgZnJvbSBj"
    "b250aW51b3VzbHk7IHRoZQogICAgICAgICNkaXN0cmlidXRvciB0aHJlYWQgaXRlcmF0ZWQgdGhl"
    "bSBsYXRlciBhbmQgaGl0ICdsaXN0IGNoYW5nZWQgc2l6ZQogICAgICAgICNkdXJpbmcgaXRlcmF0"
    "aW9uJywgd2hpY2ggdGhlIGV4Y2VwdCBhYm92ZSBzd2FsbG93ZWQgLSBzaWxlbnRseQogICAgICAg"
    "ICNkcm9wcGluZyB0aGUgZW50aXJlIGJyb2FkY2FzdC4gdXBkYXRlUG9zKCkgZG9lcyB0aGlzIG9u"
    "Y2UgYSBzZWNvbmQgZm9yCiAgICAgICAgI2V2ZXJ5IGNoYW5uZWwsIHNvIHRoaXMgd2FzIHRoZSBo"
    "b3QgcGF0aCBmb3IgdGhlIHJhY2UuCiAgICAgICAgaWYgaXNpbnN0YW5jZShwcm9wcywgZGljdCk6"
    "CiAgICAgICAgICAgIHByb3BzID0gZGljdChwcm9wcykKICAgICAgICAgICAgcHJvcHNbJ3Rhcmdl"
    "dCddID0gbGlzdChwcm9wcy5nZXQoJ3RhcmdldCcpIG9yICgpKQogICAgICAgIHNlbGYuX2NRdWV1"
    "ZS5wdXQocHJvcHMpCiAgICBkZWYgZW5kKHNlbGYpOgogICAgICAgIHNlbGYuYWRkKHNlbGYuX0VO"
    "RElURU0pCiAgICAKY2xhc3MgR2FtZUVudHJ5KCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwgcGFy"
    "ZW50LCBuYW1lLCBob3N0LCBwYXN3LCBtYXBwLCBtYXB0LCBucGosIHVuMSwgc3RhdHVzLCBtYXhw"
    "bGF5ZXJzLCB1cmwpOgogICAgICAgIGlmIGhvc3QudXNlci5nYW1lOgogICAgICAgICAgICBob3N0"
    "LnVzZXIuZ2FtZS5yZW1vdmUoaG9zdCkKICAgICAgICBzZWxmLnBhcmVudCA9IHBhcmVudCAjIEdh"
    "bWVjaGFubmVsCiAgICAgICAgc2VsZi5nbmFtZSA9IG5hbWUgIwogICAgICAgIHNlbGYuaG9zdCA9"
    "IGhvc3QgIyBDb25uZWN0aW9uIE9iamVjdAogICAgICAgIHNlbGYucGFzc3dvcmQgPSBwYXN3ICMg"
    "Jycgb3IgJ3Bhc3N3b3JkJwogICAgICAgIHNlbGYubWFwUGFyID0gbWFwcCAjICJOZXRfTV8wMSBu"
    "dWxsIDAgMSIKICAgICAgICBzZWxmLm1hcFRyYW5zbGF0ZSA9IG1hcHQgIyAidHJhbnNsYXRlTmV0"
    "X01fMDEiCiAgICAgICAgc2VsZi5ucGogPSBpbnQobnBqKSAjICJlbmFibGUgbmV3IHBsYXllciB0"
    "byBqb2luIChib29sKSIKICAgICAgICBzZWxmLnVuMSA9IGludCh1bjEpICMgMCBUT0RPIGZpZ3Vy"
    "ZSBvdXQgaWYgbWVhbnMgImd1aWxkIGdhbWUiCiAgICAgICAgc2VsZi5zdGF0dXMgPSBpbnQoc3Rh"
    "dHVzKSAjIGNoYW5nZXMgdG8gMSB3aGVuIHN0YXJ0ZWQsIG9ubHkgcmVsZXZhbnQgd2hlbiBucGog"
    "dHJ1ZQogICAgICAgIHNlbGYubWF4cGxheWVycyA9IGludChtYXhwbGF5ZXJzKSAjIDggI21heCB1"
    "c2Vycz8KICAgICAgICBzZWxmLnVybCA9IHVybCAjIHgtZGlyZWN0cGxheSB1cmwKICAgICAgICBz"
    "ZWxmLnVzZXJsaXN0ID0gW2hvc3QsXQogICAgICAgIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25h"
    "bWVdID0gc2VsZgogICAgICAgIHNlbGYuaG9zdC51c2VyLmdhbWUgPSBzZWxmCiAgICAgICAgI0Fk"
    "dmVydGlzZSBvbiBjcmVhdGlvbgogICAgICAgIG1zZyA9IHNlbGYuZ2V0R2FtZVN0cmluZygpCiAg"
    "ICAgICAgdGcgPSBzZWxmLnBhcmVudC51c2VybGlzdAogICAgICAgIHNlbGYucGFyZW50LnNlcnZl"
    "ci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOm1zZ30pCiAgICBkZWYgYWRkVXNlcihz"
    "ZWxmLCB1c3IsIHBhc3cpOgogICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KT49c2VsZi5tYXhw"
    "bGF5ZXJzOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjVE9ETyBlcnJvcj8gU2VydmVyIEZ1bGwK"
    "ICAgICAgICBpZiBzZWxmLnN0YXR1cyBhbmQgbm90IHNlbGYubnBqOgogICAgICAgICAgICByZXR1"
    "cm4gTm9uZSAjVE9ETyBlcnJvcj8gR2FtZSBhbHJlYWR5IHN0YXJ0ZWQgYW5kIGRvZXMgbm90IGFs"
    "bG93IG5ldyBwbGF5ZXJzCiAgICAgICAgaWYgc2VsZi5wYXNzd29yZCA9PSBwYXN3OgogICAgICAg"
    "ICAgICBzZWxmLnVzZXJsaXN0LmFwcGVuZCh1c3IpCiAgICAgICAgICAgIHVzci51c2VyLmdhbWUg"
    "PSBzZWxmCiAgICAgICAgICAgIHJldCA9IF9lbShmJyRnYW1ldXNlciAie3NlbGYuZ25hbWV9IiAi"
    "e3Vzci51c2VyLm5hbWV9IiAiIiAiMTAwIiAiMCInKQogICAgICAgICAgICBpZiBzZWxmLm5wajoK"
    "ICAgICAgICAgICAgICAgIHVzci5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnNlbGYucGFyZW50"
    "LnVzZXJsaXN0LCdtZXNzYWdlJzpyZXR9KQogICAgICAgICAgICAjYWJvdmUgc2VudCB0byBhbGw/"
    "CiAgICAgICAgICAgIHJldHVybiBfZW0oZicvam9pbmdhbWUgIntzZWxmLmduYW1lfSIgIntzZWxm"
    "LnVybH0iICJ7c2VsZi5zdGF0dXN9IicpICN2ZXJpZmllZCBoYXMgdG8gYmUgIjAiPwogICAgICAg"
    "IGVsc2U6CiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgYmFkR2FtZVBhc3N3b3JkICJ7"
    "c2VsZi5nbmFtZX0iJykKICAgIGRlZiByZW1vdmUoc2VsZiwgY29uPU5vbmUpOiNUT0RPIHJlY3Jl"
    "YXRlIHByb3Blcmx5CiAgICAgICAgaWYgY29uIGlzIE5vbmUgb3IgY29uIG5vdCBpbiBzZWxmLnVz"
    "ZXJsaXN0OgogICAgICAgICAgICByZXR1cm4KICAgICAgICB0ZyA9IHNlbGYucGFyZW50LnVzZXJs"
    "aXN0CiAgICAgICAgc2VsZi51c2VybGlzdC5yZW1vdmUoY29uKQogICAgICAgIGxlYXZlbXNnID0g"
    "X2VtKGYnJmdhbWV1c2VyICJ7Y29uLnVzZXIubmFtZX0iJykKICAgICAgICBjb24udXNlci5nYW1l"
    "ID0gTm9uZQogICAgICAgICNpZiAwIHVzZXJzIGxlZnQsIHJlbW92ZSBnYW1lCiAgICAgICAgaWYg"
    "bGVuKHNlbGYudXNlcmxpc3QpPT0wOgogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZnYW1l"
    "ICJ7c2VsZi5nbmFtZX0iJykKICAgICAgICAgICAgZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYu"
    "Z25hbWVdCiAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0Zywn"
    "bWVzc2FnZSc6bGVhdmVtc2d9KQogICAgZGVmIHN0YXJ0R2FtZShzZWxmLCB1c2VyPU5vbmUpOgog"
    "ICAgICAgIGlmIG5vdCAodXNlciBhbmQgc2VsZi5ob3N0ID09IHVzZXIpOgogICAgICAgICAgICBy"
    "ZXR1cm4gTm9uZSAjdXNlciBub3QgaG9zdAogICAgICAgIHRnID0gc2VsZi5wYXJlbnQudXNlcmxp"
    "c3QKICAgICAgICBzZWxmLnN0YXR1cyA9IDEKICAgICAgICBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0"
    "OiNUT0RPIGhhdmUgdXNlciByZW1vdmUgaXRzZWxmIHdoZW4gL3N0YXJ0aW5nZ2FtZT8KICAgICAg"
    "ICAgICAgdW4gPSBjLnVzZXIubmFtZQogICAgICAgICAgICAjVE9ETyBjb25zaWRlciByZW1vdmlu"
    "ZyB1c2VyIGZyb20gdGFyZ2V0IG93biBzZXQ/CiAgICAgICAgICAgIHNlbGYucGFyZW50LnNlcnZl"
    "ci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOl9lbShmJyZjaGF0Y2hhbm5lbHVzZXIg"
    "Int1bn0iJykrX2VtKGYnJmdhbWVjaGFubmVsdXNlciAie3VufSInKX0pCiAgICAgICAgaWYgbm90"
    "IHNlbGYubnBqOgogICAgICAgICAgICAjZ2FtZSBubyBsb25nZXIgam9pbmFibGUvdmlzaWJsZSBv"
    "bmNlIHN0YXJ0ZWQKICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFy"
    "Z2V0Jzp0ZywnbWVzc2FnZSc6X2VtKGYnJmdhbWUgIntzZWxmLmduYW1lfSInKX0pCiAgICAgICAg"
    "I25vdGlmeSBwbGF5ZXJzIGluIHRoZSBnYW1lIHRoYXQgaXQgaGFzIHN0YXJ0ZWQKICAgICAgICBm"
    "b3IgYyBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICBpc0hvc3QgPSAxIGlmIGMgaXMgc2Vs"
    "Zi5ob3N0IGVsc2UgMAogICAgICAgICAgICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0"
    "YXJnZXQnOihjLCksJ21lc3NhZ2UnOl9lbShmJy9zdGFydGdhbWUgIjEiICJ7aXNIb3N0fSIgIjEi"
    "Jyl9KQogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgX2dldFVzZXJsaXN0KHNlbGYpOgogICAg"
    "ICAgIHJldHVybiAnICcuam9pbiggKGYnIntjLnVzZXIubmFtZX0iICIiICIxMDAiICIwIicgZm9y"
    "IGMgaW4gc2VsZi51c2VybGlzdCkgKQogICAgZGVmIGdldEdhbWVTdHJpbmcoc2VsZik6CiAgICAg"
    "ICAgaWYgc2VsZi5zdGF0dXMgYW5kIG5vdCBzZWxmLm5wajoKICAgICAgICAgICAgcmV0dXJuIE5v"
    "bmUgI0dhbWUgZG9lcyBub3Qgc2hvdyBpZiBuZXcgcGxheWVycyBjYW4ndCBqb2luIHdoZW4gYWN0"
    "aXZlCiAgICAgICAgcGFzdyA9ICcnCiAgICAgICAgaWYgc2VsZi5wYXNzd29yZDoKICAgICAgICAg"
    "ICAgcGFzdyA9ICdYWFgnCiAgICAgICAgcmV0dXJuIF9lbShmJyRnYW1lICJ7c2VsZi5nbmFtZX0i"
    "ICJ7cGFzd30iICJ7c2VsZi5tYXBQYXJ9IiAie3NlbGYubWFwVHJhbnNsYXRlfSIgIntzZWxmLnVu"
    "MX0iICJ7c2VsZi5zdGF0dXN9IiAie3NlbGYubWF4cGxheWVyc30iIHtzZWxmLl9nZXRVc2VybGlz"
    "dCgpfScpCiAgICBkZWYgZGVidWdfZGljdChzZWxmKToKICAgICAgICByZXR1cm4gewogICAgICAg"
    "ICAgICAnbmFtZSc6c2VsZi5nbmFtZSwKICAgICAgICAgICAgJ2hvc3QnOnNlbGYuaG9zdC51c2Vy"
    "Lm5hbWUsCiAgICAgICAgICAgICdzdGF0dXMnOnNlbGYuc3RhdHVzLAogICAgICAgICAgICAnaGFz"
    "UGFzc3dvcmQnOjEgaWYgc2VsZi5wYXNzd29yZCBlbHNlIDAsCiAgICAgICAgICAgICd1c2Vycyc6"
    "dHVwbGUoW2MudXNlci5uYW1lIGZvciBjIGluIHNlbGYudXNlcmxpc3RdKSwKICAgICAgICAgICAg"
    "J3Rvd24nOnNlbGYucGFyZW50Lm5hbWUsCiAgICAgICAgICAgICdwYXJhbWV0ZXJzJzpzZWxmLm1h"
    "cFBhciwKICAgICAgICAgICAgJ21hcE5hbWUnOnNlbGYubWFwVHJhbnNsYXRlLAogICAgICAgICAg"
    "ICAnY2FuSm9pblJ1bm5pbmcnOnNlbGYubnBqCiAgICAgICAgfQojIHRyYW5zbGF0ZU5ldENpdHlN"
    "YWluQ2hhbm5lbAojIHRyYW5zbGF0ZU5ldENpdHlUcmFkZUNoYW5uZWwKIyB0cmFuc2xhdGVOZXRD"
    "aXR5Q2hhdENoYW5uZWwKX0RFRkFVTFRfQ0hBVFMgPSBbJ3RyYW5zbGF0ZU5ldENpdHlNYWluQ2hh"
    "bm5lbCcsJ3RyYW5zbGF0ZU5ldENpdHlUcmFkZUNoYW5uZWwnXQpjbGFzcyBHYW1lQ2hhbm5lbCgp"
    "OgogICAgbWF4dXNlciA9IDUwICNUT0RPIGNvbmZpZ3VyZWFibGUKICAgIGRlZiBfX2luaXRfXyhz"
    "ZWxmLCBzZXJ2ZXIsIGNobk5hbWUpOgogICAgICAgIHNlbGYuc2VydmVyID0gc2VydmVyCiAgICAg"
    "ICAgc2VsZi5uYW1lID0gY2huTmFtZQogICAgICAgIHNlbGYudXNlcmxpc3QgPSBbXQogICAgICAg"
    "IHNlbGYuY2hhdENoYW5uZWxzID0ge30KICAgICAgICBzZWxmLmdhbWVzID0ge30gI1RPRE8gZmln"
    "dXJlIG91dCBBIGFuZCBCIHZhbHVlIGZvciBkaXNwbGF5CiAgICAgICAgI1RPRE8gcmVxdWVzdCBq"
    "b2luIHJlc2VydmVzIHNwYWNlIHdpdGggd2VhayByZWZlcmVuY2VzCiAgICAgICAgIy0gd2VhayB2"
    "YWx1ZSByZWYgc2hvdWxkIGVuc3VyZSB0aGF0IGNvbm5lY3Rpb24gaXMgcmVtb3ZlZCBmcm9tIHF1"
    "ZXVlIGlmIGl0IGRpc2Nvbm5lY3RzIGR1cmluZyB0aGUgam9pbiBwcm9jZXNzCiAgICAgICAgc2Vs"
    "Zi5yZXF1ZXN0ZWQgPSBbXQogICAgICAgIHNlbGYuZ2FtZVJlcXVlc3RzID0ge30KICAgICAgICBz"
    "ZWxmLmRpcnR5ID0gRmFsc2UKICAgICAgICBmb3IgY24gaW4gX0RFRkFVTFRfQ0hBVFM6CiAgICAg"
    "ICAgICAgIHNlbGYuY2hhdENoYW5uZWxzW2NuXSA9IFtdICNVc2VybGlzdAogICAgZGVmIHJlcXVl"
    "c3RKb2luKHNlbGYsIGNvbik6CiAgICAgICAgY29uLnVzZXIubGVhdmVDaGFubmVsKCkjIEp1c3Qg"
    "aW4gY2FzZSBJIGd1ZXNzPyBUT0RPIHJlb3JnYW5pemUgY29kZQogICAgICAgIGlmIGNvbi51c2Vy"
    "LnJlcXVlc3RlZENoYW5uZWw6CiAgICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwu"
    "cmVxdWVzdGVkLnJlbW92ZShjb24pCiAgICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5u"
    "ZWwgPSBOb25lCiAgICAgICAgI0NoZWNrIHJlcXVlc3RlZCB0aW1lc3RhbXBzCiAgICAgICAgZWxl"
    "biA9IGxlbihzZWxmLnVzZXJsaXN0KStsZW4oc2VsZi5yZXF1ZXN0ZWQpCiAgICAgICAgaWYgZWxl"
    "bjxzZWxmLm1heHVzZXI6CiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAg"
    "ICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgICAgIHJl"
    "dHVybiBUcnVlCiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBkZWYgcmVxdWVzdENyZWF0ZUdhbWUo"
    "c2VsZiwgY29uLCBnYW1lTmFtZSk6CiAgICAgICAgaWYgY29uLnVzZXIucmVxdWVzdGVkR2FtZSBv"
    "ciBjb24udXNlci5nYW1lOgogICAgICAgICAgICBjb24udXNlci5zdG9wR2FtZSgpCiAgICAgICAg"
    "aWYgZ2FtZU5hbWUgaW4gc2VsZi5nYW1lUmVxdWVzdHM6CiAgICAgICAgICAgIHRjbiA9IHNlbGYu"
    "Z2FtZVJlcXVlc3RzW2dhbWVOYW1lXQogICAgICAgICAgICBpZiB0Y24gIT0gY29uOgogICAgICAg"
    "ICAgICAgICAgcmV0dXJuIEZhbHNlICMgcmV0dXJuIGVycm9yCiAgICAgICAgICAgICNlbHNlIHRj"
    "biA9PSBjb24sIHJlLXJlcXVlc3RlZCBjcmVhdGlvbgogICAgICAgIGlmIGdhbWVOYW1lIGluIHNl"
    "bGYuZ2FtZXM6CiAgICAgICAgICAgIHJldHVybiBGYWxzZSAjIHJldHVybiBlcnJvcj8KICAgICAg"
    "ICBzZWxmLmdhbWVSZXF1ZXN0c1tnYW1lTmFtZV0gPSBjb24KICAgICAgICBjb24udXNlci5yZXF1"
    "ZXN0ZWRHYW1lID0gZ2FtZU5hbWUKICAgICAgICByZXR1cm4gX2VtKGYnL2NyZWF0ZWdhbWUgIntn"
    "YW1lTmFtZX0iJykKICAgIGRlZiBjcmVhdGVHYW1lKHNlbGYsIGdhbWVOYW1lLCBob3N0LCBwYXN3"
    "LCBtYXBwLCBtYXB0LCBucGosIHVuMSwgdW4yLCB1bjMsIHVybCk6CiAgICAgICAgcmVxSG9zdCA9"
    "IHNlbGYuZ2FtZVJlcXVlc3RzLmdldChnYW1lTmFtZSkKICAgICAgICBpZiByZXFIb3N0ID09IE5v"
    "bmU6CiAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIGlmIHJlcUhvc3QgIT0gaG9zdDoK"
    "ICAgICAgICAgICAgcmV0dXJuIEZhbHNlICMgcmV0dXJuIGVycm9yPwogICAgICAgIGdlbnQgPSBH"
    "YW1lRW50cnkoc2VsZiwgZ2FtZU5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4x"
    "LCB1bjIsIHVuMywgdXJsKQogICAgICAgIHJlcUhvc3QudXNlci5yZXF1ZXN0ZWRHYW1lID0gTm9u"
    "ZSAjVE9ETyByZW9nYW5pemUgYmV0dGVyCiAgICAgICAgZGVsIHNlbGYuZ2FtZVJlcXVlc3RzW2dh"
    "bWVOYW1lXQogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgbGVhdmVDaGFubmVsKHNlbGYsIGNv"
    "bik6CiAgICAgICAgaWYgY29uIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgIGNvbi51c2Vy"
    "LnN0b3BHYW1lKCkKICAgICAgICAgICAgY29uLnVzZXIubGVhdmVDaGF0KCkKICAgICAgICAgICAg"
    "c2VsZi51c2VybGlzdC5yZW1vdmUoY29uKQogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShmJyZn"
    "YW1lY2hhbm5lbHVzZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24uc2VydmVy"
    "LmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLnVzZXJsaXN0LCdtZXNzYWdlJzpsZWF2ZW1zZ30pCiAg"
    "ICAgICAgICAgIGNvbi51c2VyLmdhbWVjaGFubmVsPU5vbmUKICAgIGRlZiBsZWF2ZUNoYXQoc2Vs"
    "ZiwgY29uKTogI1RPRE8gYmV0dGVyIGNoYXRjaGFubmVsIG9iamVjdCBhbmQgbW92ZSBpdCB0aGVy"
    "ZS4KICAgICAgICBjb24udXNlci5sZWF2ZUNoYXQoKQogICAgI1RPRE8gY2hhbmdlIHRoZXNlIGZ1"
    "bmN0aW9ucyB0byBhbHNvIGhhbmRsZSBtZXNzYWdlIGZvcm1pbmcKICAgIGRlZiBqb2luQ2hhbm5l"
    "bChzZWxmLCBjb24sIG5hbSk6I21vdmVzIHVzZXIgZnJvbSBxdWV1ZSB0byB1c2VybGlzdAogICAg"
    "ICAgIGlmIGNvbiBpbiBzZWxmLnJlcXVlc3RlZDoKICAgICAgICAgICAgI1RPRE8gdmVyaWZ5IG9y"
    "ZGVyIG9mIG9wZXJhdGlvbnMgYW5kIHBvc3NpYmxlIHRpbWluZyBpc3N1ZXMKICAgICAgICAgICAg"
    "c2VsZi51c2VybGlzdC5hcHBlbmQoY29uKQogICAgICAgICAgICBjb24udXNlci5nYW1lY2hhbm5l"
    "bCA9IHNlbGYKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWQucmVtb3ZlKGNvbikKICAgICAgICAg"
    "ICAgY29uLnVzZXIucmVxdWVzdGVkQ2hhbm5lbCA9IE5vbmUgI1RPRE8gb3JnYW5pemUgYmV0dGVy"
    "PwogICAgICAgICAgICB1bCA9IGxlbihzZWxmLnVzZXJsaXN0KQogICAgICAgICAgICByZXRtc2cg"
    "PSBfZW0oZicvam9pbmdhbWVjaGFubmVsICJ7bmFtfSIgInt1bH0iJykKICAgICAgICAgICAgI2Vu"
    "dW1lcmF0ZSBoZXJvZGF0YSBvZiBleGlzdGluZyB1c2VycwogICAgICAgICAgICBjaHVua3MgPSBb"
    "XQogICAgICAgICAgICBmb3IgdXNlciBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAgICAgICAg"
    "aWYgdXNlciA9PSBjb246CiAgICAgICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAg"
    "ICAgIGNodW5rcy5hcHBlbmQodXNlci51c2VyLmdldEdDVW1zZygpKQogICAgICAgICAgICByZXRt"
    "c2crPSBiJycuam9pbihjaHVua3MpCiAgICAgICAgICAgIHJldG1zZys9IHNlbGYuam9pbkNoYXQo"
    "Y29uLCBfREVGQVVMVF9DSEFUU1swXSkKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5lbnVtQ2hh"
    "dHMoKQogICAgICAgICAgICByZXRtc2crPSBzZWxmLmVudW1HYW1lcygpCiAgICAgICAgICAgICNi"
    "cm9hZGNhc3QgaGVyb2RhdGEgdG8gb3RoZXIgZXhpc3RpbmcgdXNlcnMKICAgICAgICAgICAgY29u"
    "LnNlcnZlci5kaXN0LmFkZCh7CiAgICAgICAgICAgICAgICAndGFyZ2V0Jzpfd29Vc2VyKHNlbGYu"
    "dXNlcmxpc3QsIGNvbiksCiAgICAgICAgICAgICAgICAnbWVzc2FnZSc6Y29uLnVzZXIuZ2V0R0NV"
    "bXNnKCl9KQogICAgICAgICAgICByZXR1cm4gcmV0bXNnCiAgICAgICAgcmV0dXJuIE5vbmUKICAg"
    "IGRlZiBqb2luQ2hhdChzZWxmLCBjb24sIG5hbSwgcGFzPScnKToKICAgICAgICAjVE9ETyBwYXNz"
    "d29yZCBzdXBwb3J0PwogICAgICAgICMtIHJlcXVpcmVzIHJlc3RydWN0dXJlIGZyb20gbGlzdCB0"
    "byBjaGFubmVsIG9iamVjdHMKICAgICAgICBpZiBub3QgbmFtIGluIHNlbGYuY2hhdENoYW5uZWxz"
    "OgogICAgICAgICAgICByZXR1cm4gYicnCiAgICAgICAgY29uLnVzZXIubGVhdmVDaGF0KCkKICAg"
    "ICAgICAjVE9ETyBjaGVjayBpZiBjbGllbnQgYXV0by1wdXJnZXMgY2hhdGxpc3QKICAgICAgICAj"
    "VE9ETyBDSEVDSyAtVi0gYnJvYWRjYXN0IHJlbGV2YW50IGNoYW5nZXM/CiAgICAgICAgY29uLnNl"
    "cnZlci5kaXN0LmFkZCh7CiAgICAgICAgICAgICd0YXJnZXQnOmxpc3Qoc2VsZi5jaGF0Q2hhbm5l"
    "bHNbbmFtXSksCiAgICAgICAgICAgICdtZXNzYWdlJzpfZW0oZickY2hhdGNoYW5uZWx1c2VyICJ7"
    "Y29uLnVzZXIubmFtZX0iJyl9KQogICAgICAgIHNlbGYuY2hhdENoYW5uZWxzW25hbV0uYXBwZW5k"
    "KGNvbikKICAgICAgICBjb24udXNlci5jaGF0Y2hhbm5lbCA9IHNlbGYuY2hhdENoYW5uZWxzW25h"
    "bV0KICAgICAgICB1bCA9IDEjbGVuKGNvbi51c2VyLmNoYXRjaGFubmVsKQogICAgICAgIHJldG1z"
    "ZyA9IF9lbShmJy9qb2luY2hhdGNoYW5uZWwgIntuYW19IiAiIiAie3VsfSInKQogICAgICAgICNl"
    "bnVtZXJhdGUgb3RoZXIgY2hhdCB1c2Vycz8KICAgICAgICBjaHVua3MgPSBbXQogICAgICAgIGZv"
    "ciB1Y29uIGluIGNvbi51c2VyLmNoYXRjaGFubmVsOgogICAgICAgICAgICBpZiB1Y29uICE9IGNv"
    "bjoKICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQoX2VtKGYnJGNoYXRjaGFubmVsdXNlciAi"
    "e3Vjb24udXNlci5uYW1lfSInKSkKICAgICAgICByZXRtc2crPWInJy5qb2luKGNodW5rcykKICAg"
    "ICAgICByZXR1cm4gcmV0bXNnCiAgICBkZWYgZW51bUNoYXRzKHNlbGYpOgogICAgICAgIGNodW5r"
    "cyA9IFtdCiAgICAgICAgZm9yIGNoYXROYW1lIGluIHNlbGYuY2hhdENoYW5uZWxzOgogICAgICAg"
    "ICAgICB1bGwgPSBsZW4oc2VsZi5jaGF0Q2hhbm5lbHNbY2hhdE5hbWVdKSNUT0RPIGltcHJvdmUK"
    "ICAgICAgICAgICAgY2h1bmtzLmFwcGVuZCh3aXJlX2VuY29kZShmJyRjaGF0Y2hhbm5lbCAie2No"
    "YXROYW1lfSIgIiIgInt1bGx9IicpKQogICAgICAgIHJldHVybiBfTi5qb2luKGNodW5rcykrX04K"
    "ICAgIGRlZiBlbnVtR2FtZXMoc2VsZik6CiAgICAgICAgY2h1bmtzID0gW10KICAgICAgICBmb3Ig"
    "Z25hbWUgaW4gc2VsZi5nYW1lczoKICAgICAgICAgICAgZ2FtZXN0ciA9IHNlbGYuZ2FtZXNbZ25h"
    "bWVdLmdldEdhbWVTdHJpbmcoKQogICAgICAgICAgICBpZiBnYW1lc3RyOgogICAgICAgICAgICAg"
    "ICAgY2h1bmtzLmFwcGVuZChnYW1lc3RyKQogICAgICAgIHJldHVybiBiJycuam9pbihjaHVua3Mp"
    "CiAgICBkZWYgdXBkYXRlUG9zKHNlbGYsIG1kKToKICAgICAgICBpZiBub3Qgc2VsZi5kaXJ0eToK"
    "ICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgI0NsZWFyZWQgQkVGT1JFIHRoZSBzY2FuLCBub3Qg"
    "YWZ0ZXIuIEEgL3VwZGhlcm9wb3MgdGhhdCBhcnJpdmVkIHdoaWxlCiAgICAgICAgI3RoZSBsb29w"
    "IGJlbG93IHdhcyBydW5uaW5nIHVzZWQgdG8gc2V0IGRpcnR5PVRydWUgYW5kIHRoZW4gaGF2ZSBp"
    "dAogICAgICAgICNpbW1lZGlhdGVseSBjbGVhcmVkIGFnYWluLCBzbyB0aGF0IHBsYXllcidzIG1v"
    "dmUgd2FzIG5vdCBicm9hZGNhc3QKICAgICAgICAjdW50aWwgc29tZWJvZHkgZWxzZSBoYXBwZW5l"
    "ZCB0byBtb3ZlLiBDbGVhcmluZyBmaXJzdCBtZWFucyB0aGUgd29yc3QKICAgICAgICAjY2FzZSBp"
    "cyBvbmUgcmVkdW5kYW50IHBhc3MsIG5vdCBhIHNpbGVudGx5IGRyb3BwZWQgcG9zaXRpb24uCiAg"
    "ICAgICAgc2VsZi5kaXJ0eSA9IEZhbHNlCiAgICAgICAgI1NuYXBzaG90OiBwbGF5ZXJzIGpvaW4g"
    "YW5kIGxlYXZlIHRoZSB0b3duIHdoaWxlIHRoaXMgaXRlcmF0ZXMuCiAgICAgICAgdGcgPSBsaXN0"
    "KHNlbGYudXNlcmxpc3QpCiAgICAgICAgbXNnY2h1bmtzID0gW10KICAgICAgICBmb3IgdWNvbiBp"
    "biB0ZzoKICAgICAgICAgICAgaWYgdWNvbi51c2VyLnBvc2NoYW5nZWQ6CiAgICAgICAgICAgICAg"
    "ICBtc2djaHVua3MuYXBwZW5kKGYne3Vjb24udXNlci5pZG51bTp4fSN7dWNvbi51c2VyLnBvc2Rh"
    "dGF9JykKICAgICAgICAgICAgICAgIHVjb24udXNlci5wb3NjaGFuZ2VkID0gRmFsc2UKICAgICAg"
    "ICBpZiBub3QgbXNnY2h1bmtzOgogICAgICAgICAgICAjRXZlcnlvbmUgd2hvIHdhcyBkaXJ0eSBo"
    "YXMgc2luY2UgbGVmdCB0aGUgdG93bi4gU2VuZGluZyB0aGUKICAgICAgICAgICAgI2FyZ3VtZW50"
    "LWxlc3MgJy91cGRoZXJvcG9zICcgdGhhdCB0aGlzIHVzZWQgdG8gcHJvZHVjZSBqdXN0IGhhbmRz"
    "CiAgICAgICAgICAgICN0aGUgY2xpZW50IGFuIGVtcHR5IGNvbW1hbmQgdG8gcGFyc2UuCiAgICAg"
    "ICAgICAgIHJldHVybgogICAgICAgIHB1cGQgPSAnICcuam9pbihtc2djaHVua3MpCiAgICAgICAg"
    "bXNnID0gX2VtKGYnL3VwZGhlcm9wb3Mge3B1cGR9JykKICAgICAgICBtZC5hZGQoeyd0YXJnZXQn"
    "OnRnLCdtZXNzYWdlJzptc2d9KQogICAgZGVmIGRlYnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAg"
    "ICBhY3REaWN0ID0gW10KICAgICAgICBmb3IgZ24sIGcgaW4gbGlzdChzZWxmLmdhbWVzLml0ZW1z"
    "KCkpOgogICAgICAgICAgICBhY3REaWN0LmFwcGVuZChnLmRlYnVnX2RpY3QoKSkKICAgICAgICBy"
    "ZXR1cm4gYWN0RGljdAogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsK"
    "ICAgICAgICAgICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUgZm9yIGMgaW4gc2VsZi51c2Vy"
    "bGlzdF0pLAogICAgICAgICAgICAnbWF4VXNlcnMnOnNlbGYubWF4dXNlciwKICAgICAgICAgICAg"
    "J2dhbWVzJzp0dXBsZShbZ24gZm9yIGduIGluIHNlbGYuZ2FtZXNdKQogICAgICAgIH0KCl9NQVBO"
    "QU1FUyA9IFsnTmV0X1RfMDEnLCdOZXRfVF8wMicsJ05ldF9UXzAzJywnTmV0X1RfMDQnXSAjVE9E"
    "TyB1c2UgQ0ZHIG9iamVjdApjbGFzcyBHYW1lU3RhdGUoKToKICAgICNUT0RPIGF1dG8gZ3Jvd2Fi"
    "bGUgY2hhbm5lbHMsIFttYXBuYW1lXQogICAgI1RPRE8gYXZhaWxhYmxlIGluZGV4ZXMsIFttYXBu"
    "YW1lXQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlcik6CiAgICAgICAgI2luc3RhbmNlIGF0"
    "dHJpYnV0ZXMsIG5vdCBjbGFzcyBhdHRyaWJ1dGVzOiB0aGVzZSBtdXN0IE5PVCBiZSBzaGFyZWQK"
    "ICAgICAgICAjYmV0d2VlbiBzZXBhcmF0ZSBDb3JlU2VydmVyIGluc3RhbmNlcyAoZS5nLiBzdG9w"
    "L3N0YXJ0IGZyb20gYSBHVUkKICAgICAgICAjd2l0aGluIHRoZSBzYW1lIHByb2Nlc3MpIG9yIGxl"
    "ZnRvdmVyIHBsYXllcnMvY2hhbm5lbHMgZnJvbSBhCiAgICAgICAgI3ByZXZpb3VzIHJ1biB3b3Vs"
    "ZCBsZWFrIGludG8gdGhlIG5ldyBvbmUuCiAgICAgICAgc2VsZi5hY3RpdmVVc2VycyA9IHt9ICNU"
    "T0RPIHRyYWNrIHVzZXIgaGlzdG9yeT8gb3B0aW9uYWxseQogICAgICAgIHNlbGYuZ2FtZUNoYW5u"
    "ZWxzID0ge30gI2NoYW5uZWxbXSwga2V5ZWQgYnkgbWFwbmFtZQogICAgICAgIHNlbGYuc2VydmVy"
    "PXNlcnZlcgogICAgICAgIHNlbGYudXNlckxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAg"
    "Zm9yIG5hbWUgaW4gX01BUE5BTUVTOgogICAgICAgICAgICBmb3IgaSBpbiByYW5nZSgxKTogI1RP"
    "RE8gY29uZmlndXJlYWJsZSB1cCB0byAyMD8KICAgICAgICAgICAgICAgIGNobk5hbWUgPSBfZ2No"
    "bmwobmFtZSwgMStpKQogICAgICAgICAgICAgICAgc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0g"
    "PSBHYW1lQ2hhbm5lbChzZWxmLnNlcnZlciwgY2huTmFtZSkgI1RPRE8gMSBhbmQgZ3Jvdz8KICAg"
    "IGRlZiBjbGFpbVVzZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAjUHVibGlzaCBjb24gYXMg"
    "VEhFIGxpdmUgc2Vzc2lvbiBmb3IgbmFtZSwgYXRvbWljYWxseS4gVGhlIG9sZCBjb2RlCiAgICAg"
    "ICAgI2NoZWNrZWQgZ2V0UGxheWVyKCkgZHVyaW5nIGxvZ2luIGFuZCB0aGVuIGluc2VydGVkIGlu"
    "dG8gYWN0aXZlVXNlcnMKICAgICAgICAjbXVjaCBsYXRlciwgaW4gX2xvYmJ5SGFuZGxlOyB0d28g"
    "Y29ubmVjdGlvbnMgbG9nZ2luZyBpbiBhcyB0aGUgc2FtZQogICAgICAgICNhY2NvdW50IGF0IG9u"
    "Y2UgYm90aCBwYXNzZWQgdGhlIGNoZWNrLCBhbmQgdGhlIHNlY29uZCBvbmUncyBpbnNlcnQKICAg"
    "ICAgICAjb3Zlcndyb3RlIHRoZSBmaXJzdC4gVGhlIGxvc2VyIHRoZW4gZGVsZXRlZCB0aGUgd2lu"
    "bmVyJ3MgZW50cnkgd2hlbiBpdAogICAgICAgICNkaXNjb25uZWN0ZWQsIGxlYXZpbmcgYSBjb25u"
    "ZWN0ZWQgcGxheWVyIGludmlzaWJsZSB0byB0aGUgc2VydmVyIChubwogICAgICAgICNraWNrLCBu"
    "byB3aG9pcywgbm8gbWVzc2FnZXMpLgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAg"
    "ICAgICAgaWYgbmFtZSBpbiBzZWxmLmFjdGl2ZVVzZXJzOgogICAgICAgICAgICAgICAgcmV0dXJu"
    "IEZhbHNlCiAgICAgICAgICAgIHNlbGYuYWN0aXZlVXNlcnNbbmFtZV0gPSBjb24KICAgICAgICAg"
    "ICAgcmV0dXJuIFRydWUKICAgIGRlZiByZWxlYXNlVXNlcihzZWxmLCBuYW1lLCBjb24pOgogICAg"
    "ICAgICNvbmx5IGNsZWFyIHRoZSBzbG90IGlmIHdlIHN0aWxsIG93biBpdCwgbmV2ZXIgc29tZW9u"
    "ZSBlbHNlJ3Mgc2Vzc2lvbgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAgICAgICAg"
    "aWYgc2VsZi5hY3RpdmVVc2Vycy5nZXQobmFtZSkgaXMgY29uOgogICAgICAgICAgICAgICAgZGVs"
    "IHNlbGYuYWN0aXZlVXNlcnNbbmFtZV0KICAgIGRlZiBlbnVtZXJhdGVHQyhzZWxmKToKICAgICAg"
    "ICBjaG5zID0gW10KICAgICAgICBmb3IgY2huTmFtZSBpbiBzZWxmLmdhbWVDaGFubmVsczoKICAg"
    "ICAgICAgICAgY2huID0gc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0KICAgICAgICAgICAgY2hu"
    "cy5hcHBlbmQod2lyZV9lbmNvZGUoZickZ2FtZWNoYW5uZWwgIntjaG5OYW1lfSIgIntsZW4oY2hu"
    "LnVzZXJsaXN0KX0iICJ7Y2huLm1heHVzZXJ9IiAiMCIgIjAiJykpICNUT0RPIEF2YWlsYWJsZSAt"
    "IEFsbAogICAgICAgIHJldHVybiBfTi5qb2luKGNobnMpK19OCiAgICBkZWYgdXBkYXRlUG9zKHNl"
    "bGYpOgogICAgICAgIG1kID0gc2VsZi5zZXJ2ZXIuZGlzdAogICAgICAgIGZvciBjaG4gaW4gbGlz"
    "dChzZWxmLmdhbWVDaGFubmVscy52YWx1ZXMoKSk6CiAgICAgICAgICAgIGNobi51cGRhdGVQb3Mo"
    "bWQpCmNsYXNzIFdlYlNlcnZlcihzb2NrZXRzZXJ2ZXIuVGhyZWFkaW5nVENQU2VydmVyKToKICAg"
    "IGRhZW1vbl90aHJlYWRzID0gVHJ1ZQogICAgYWxsb3dfcmV1c2VfYWRkcmVzcyA9IFRydWUgIyBU"
    "T0RPIGNoZWNrIGlmIGltcHJvdmVzIHJlc3RhcnQgdGltZXMgd2l0aG91dCBvdGhlciBpc3N1ZXMK"
    "ICAgIGRlZiBfX2luaXRfXyhzZWxmLCBwYXJlbnQpOgogICAgICAgIHByaW50KGYnSW5pdGlhbGl6"
    "aW5nIFdlYlNlcnZlciBmb3IgcG9ydCB7X1dFQl9TRVJWRVJfUE9SVH0nKQogICAgICAgIHN1cGVy"
    "KCkuX19pbml0X18oKCIiLCBfV0VCX1NFUlZFUl9QT1JUKSwgV2ViQXBpU2VydmUpCiAgICAgICAg"
    "c2VsZi5jb3JlID0gcGFyZW50CiNoYW5kbGVzIGludGVyYWN0aW9ucyBiZXR3ZWVuIGFsbCBlbGVt"
    "ZW50cwpjbGFzcyBDb3JlU2VydmVyKHNvY2tldHNlcnZlci5UaHJlYWRpbmdUQ1BTZXJ2ZXIpOgog"
    "ICAgYWxsb3dfcmV1c2VfYWRkcmVzcyA9IFRydWUgIyBUT0RPIGNoZWNrIGlmIGltcHJvdmVzIHJl"
    "c3RhcnQgdGltZXMgd2l0aG91dCBvdGhlciBpc3N1ZXMKICAgIGRhZW1vbl90aHJlYWRzID0gVHJ1"
    "ZQogICAgYmxvY2tfb25fY2xvc2UgPSBGYWxzZQogICAgX2lzX2Nsb3NpbmcgPSBGYWxzZQogICAg"
    "ZGVmIF9faW5pdF9fKHNlbGYpOgogICAgICAgICNUT0RPIGdldCB2YWx1ZXMgZnJvbSBjZmcKICAg"
    "ICAgICAjYWRkcmVzcyA9ICdsb2NhbGhvc3QnCiAgICAgICAgYWRkcmVzcyA9ICcnCiAgICAgICAg"
    "cG9ydCA9IF9UV19MT0JCWV9QT1JUCiAgICAgICAgcHJpbnQoZidJbml0aWFsaXppbmcgc2VydmVy"
    "IGZvciBwb3J0IHtwb3J0fScpCiAgICAgICAgc3VwZXIoKS5fX2luaXRfXygoYWRkcmVzcywgcG9y"
    "dCksIENvbm5lY3Rpb25IYW5kbGVyKQogICAgICAgIGlmIF9FTkFCTEVfREVCVUdfUkFURU1PTklU"
    "T1I6CiAgICAgICAgICAgIHNlbGYuc29ja2V0ID0gUmF0ZU1vbml0b3Ioc2VsZi5zb2NrZXQpCiAg"
    "ICAgICAgc2VsZi5kaXN0ID0gTWVzc2FnZURpc3RyaWJ1dG9yKHNlbGYpCiAgICAgICAgc2VsZi5j"
    "b21wYXJzID0gQ29tbWFuZFBhcnNlcihzZWxmLmRpc3QpCiAgICAgICAgc2VsZi5zdGF0ZSA9IEdh"
    "bWVTdGF0ZShzZWxmKQogICAgICAgIHNlbGYuc3RhcnRUaW1lID0gZGF0ZXRpbWUuZGF0ZXRpbWUu"
    "bm93KCkKICAgICAgICBzZWxmLnNlcnZpY2VfdGljayA9IDAKICAgICAgICBzZWxmLnNlbmRfbm9w"
    "cyA9IF9TRU5EX05PUFMKICAgICAgICBzZWxmLl9wb3NTdG9wID0gdGhyZWFkaW5nLkV2ZW50KCkK"
    "ICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSBOb25lCiAgICAgICAgaWYgX0VOQUJMRV9XRUJfU0VS"
    "VkVSOgogICAgICAgICAgICBzZWxmLndlYmFwaSA9IFdlYlNlcnZlcihzZWxmKQogICAgZGVmIHNl"
    "cnZlcl9hY3RpdmF0ZShzZWxmKToKICAgICAgICBwcmludChmJ1NlcnZlciBTdGFydGluZyBhdCBQ"
    "SUQ6IHtvcy5nZXRwaWQoKX0nKSNMT0cKICAgICAgICBzdXBlcigpLnNlcnZlcl9hY3RpdmF0ZSgp"
    "CiAgICBkZWYgZGVidWdfZGljdF9wbGF5ZXJzKHNlbGYpOgogICAgICAgICNzbmFwc2hvdCB2aWEg"
    "bGlzdCgpIGZpcnN0OiBpdGVyYXRpbmcgdGhlIGxpdmUgZGljdCBkaXJlY3RseSByaXNrcwogICAg"
    "ICAgICMnZGljdGlvbmFyeSBjaGFuZ2VkIHNpemUgZHVyaW5nIGl0ZXJhdGlvbicgd2hlbiBhIHBs"
    "YXllciBjb25uZWN0cwogICAgICAgICNvciBkaXNjb25uZWN0cyB3aGlsZSBhIG1vbml0b3Jpbmcg"
    "VUkgaXMgcG9sbGluZyB0aGlzCiAgICAgICAgcmV0ID0ge30KICAgICAgICBmb3IgbmFtZSwgY29u"
    "IGluIGxpc3Qoc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5pdGVtcygpKToKICAgICAgICAgICAgcmV0"
    "W25hbWVdID0gY29uLmRlYnVnX2RpY3QoKQogICAgICAgIHJldHVybiByZXQKICAgIGRlZiBkZWJ1"
    "Z19kaWN0X3Rvd25zKHNlbGYpOgogICAgICAgIHJldCA9IHt9CiAgICAgICAgZm9yIG5hbWUsIGNo"
    "biBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1zKCkpOgogICAgICAgICAgICBy"
    "ZXRbbmFtZV0gPSBjaG4uZGVidWdfZGljdCgpCiAgICAgICAgcmV0dXJuIHJldAogICAgZGVmIGRl"
    "YnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAgICByZXQgPSBbXQogICAgICAgIGZvciBuYW1lLCBj"
    "aG4gaW4gbGlzdChzZWxmLnN0YXRlLmdhbWVDaGFubmVscy5pdGVtcygpKToKICAgICAgICAgICAg"
    "IHJldC5leHRlbmQoY2huLmRlYnVnX2Fycl9nYW1lcygpKQogICAgICAgIHJldHVybiByZXQKICAg"
    "IGRlZiBfcG9zTG9vcChzZWxmKToKICAgICAgICAjUG9zaXRpb24gZmFuLW91dCB1c2VkIHRvIHJp"
    "ZGUgb24gc2VydmljZV9hY3Rpb25zKCksIHdoaWNoIHNvY2tldHNlcnZlcgogICAgICAgICNjYWxs"
    "cyBvbmNlIHBlciBwb2xsX2ludGVydmFsIC0gb25lIHNlY29uZC4gVGhhdCB3YXMgdGhlIGNhZGVu"
    "Y2UgYXQKICAgICAgICAjd2hpY2ggb3RoZXIgcGxheWVycycgbWFya2VycyBtb3ZlZCBvbiB0aGUg"
    "bWFwOiBhIGZ1bGwgc2Vjb25kIG9mIGRlYWQKICAgICAgICAjcmVja29uaW5nIGJldHdlZW4gdXBk"
    "YXRlcywgd2hpY2ggcmVhZHMgYXMgdGVsZXBvcnRpbmcgcmF0aGVyIHRoYW4KICAgICAgICAjd2Fs"
    "a2luZy4gSXRzIG93biB0aHJlYWQgZGVjb3VwbGVzIHRoZSBicm9hZGNhc3QgcmF0ZSBmcm9tIHRo"
    "ZSBhY2NlcHQKICAgICAgICAjbG9vcCdzIHBvbGwgcmF0ZSBzbyBpdCBjYW4gcnVuIHNldmVyYWwg"
    "dGltZXMgYSBzZWNvbmQuCiAgICAgICAgd2hpbGUgbm90IHNlbGYuX3Bvc1N0b3AuaXNfc2V0KCk6"
    "CiAgICAgICAgICAgIHBlcmlvZCA9IDEuMCAvIF9QT1NfVVBEQVRFX0haIGlmIF9QT1NfVVBEQVRF"
    "X0haID4gMCBlbHNlIDEuMAogICAgICAgICAgICAjd2FpdCgpIHJhdGhlciB0aGFuIHNsZWVwKCk6"
    "IHNodXRkb3duIGlzIGltbWVkaWF0ZSwgYW5kIHJlLXJlYWRpbmcKICAgICAgICAgICAgI3RoZSBw"
    "ZXJpb2QgZWFjaCBwYXNzIG1lYW5zIGEgY29uZmlnIGNoYW5nZSB0YWtlcyBlZmZlY3QgbGl2ZS4K"
    "ICAgICAgICAgICAgaWYgc2VsZi5fcG9zU3RvcC53YWl0KHBlcmlvZCk6CiAgICAgICAgICAgICAg"
    "ICBicmVhawogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBzZWxmLnN0YXRlLnVwZGF0"
    "ZVBvcygpCiAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgICAgICAjbmV2"
    "ZXIgbGV0IG9uZSBiYWQgY2hhbm5lbCBraWxsIHBvc2l0aW9uIHN5bmMgZm9yIGV2ZXJ5b25lCiAg"
    "ICAgICAgICAgICAgICBwcmludCgnW0xvYmJ5XSBQb3NpdGlvbiB1cGRhdGUgZXJyb3I6XG4nICsg"
    "dHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgIGRlZiBzZXJ2aWNlX2FjdGlvbnMoc2VsZik6ICNj"
    "YWxsZWQgZXZlcnkgcG9sbF9pbnRlcnZhbAogICAgICAgIEdESC51cGRhdGUoc2VsZi5zZXJ2aWNl"
    "X3RpY2spCiAgICAgICAgIyB0aW1lIGludGVydmFscwogICAgICAgIGlmIHNlbGYuc2VuZF9ub3Bz"
    "IGFuZCAoc2VsZi5zZXJ2aWNlX3RpY2slMyk9PTA6CiAgICAgICAgICAgIHNlbGYuZGlzdC5hZGQo"
    "eyd0YXJnZXQnOnNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCksJ21lc3NhZ2UnOl9lbSgn"
    "L25vcCcpfSkKICAgICAgICAgICAgI3NlbmQgJy9ub3AnIHRvIGFsbCBldmVyeSAzIHNlYyBvcHRp"
    "b25hbGx5CiAgICAgICAgI3NlcnZpY2UgdGljayAzIGRheSByZXNldCBpbnRlcnZhbCBUT0RPIHRl"
    "c3QgYWxpZ25tZW50IHdpdGggb3RoZXIgZmFjdG9ycwogICAgICAgIHNlbGYuc2VydmljZV90aWNr"
    "ID0gKHNlbGYuc2VydmljZV90aWNrKzEpJSg2MCo2MCoyNCozKQogICAgICAgIHN1cGVyKCkuc2Vy"
    "dmljZV9hY3Rpb25zKCkKICAgIGRlZiBzZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIGRpc3RU"
    "aHJlYWQgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLmRpc3Quc2VydmVfZm9yZXZlcikK"
    "ICAgICAgICBkaXN0VGhyZWFkLnN0YXJ0KCkKICAgICAgICBzZWxmLl9wb3NTdG9wLmNsZWFyKCkK"
    "ICAgICAgICBzZWxmLl9wb3NUaHJlYWQgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1zZWxmLl9w"
    "b3NMb29wLCBkYWVtb249VHJ1ZSkKICAgICAgICBzZWxmLl9wb3NUaHJlYWQuc3RhcnQoKQogICAg"
    "ICAgIGlmIF9FTkFCTEVfV0VCX1NFUlZFUjoKICAgICAgICAgICAgd2ViVGhyZWFkID0gdGhyZWFk"
    "aW5nLlRocmVhZCh0YXJnZXQ9c2VsZi53ZWJhcGkuc2VydmVfZm9yZXZlcikKICAgICAgICAgICAg"
    "d2ViVGhyZWFkLnN0YXJ0KCkKICAgICAgICAjcG9sbF9pbnRlcnZhbCBpcyBub3cgb25seSB0aGUg"
    "YWNjZXB0IGxvb3AncyBzaHV0ZG93biByZXNwb25zaXZlbmVzcyAtCiAgICAgICAgI3Bvc2l0aW9u"
    "IGJyb2FkY2FzdHMgbm8gbG9uZ2VyIHJpZGUgb24gaXQKICAgICAgICBzdXBlcigpLnNlcnZlX2Zv"
    "cmV2ZXIoMSkKICAgICAgICBzZWxmLl9wb3NTdG9wLnNldCgpCiAgICAgICAgaWYgc2VsZi5fcG9z"
    "VGhyZWFkOgogICAgICAgICAgICBzZWxmLl9wb3NUaHJlYWQuam9pbih0aW1lb3V0PTIuMCkKICAg"
    "ICAgICAgICAgc2VsZi5fcG9zVGhyZWFkID0gTm9uZQogICAgICAgIHNlbGYuZGlzdC5lbmQoKSNp"
    "biBjYXNlIGl0IGhhc24ndCBhbHJlYWR5CiAgICAgICAgaWYgX0VOQUJMRV9XRUJfU0VSVkVSOgog"
    "ICAgICAgICAgICBzZWxmLndlYmFwaS5zaHV0ZG93bigpCiAgICAgICAgICAgIHdlYlRocmVhZC5q"
    "b2luKCkKICAgICAgICAgICAgc2VsZi53ZWJhcGkuc2VydmVyX2Nsb3NlKCkKICAgICAgICBkaXN0"
    "VGhyZWFkLmpvaW4oKQogICAgZGVmIGhhbmRsZV9zaWduYWwoc2VsZiwgdGltZW91dCk6CiAgICAg"
    "ICAgZGVmIGhhbmRsZXIoc2lnbnVtLCBfKToKICAgICAgICAgICAgZGVhZGxpbmUgPSB0aW1lLm1v"
    "bm90b25pYygpICsgdGltZW91dAogICAgICAgICAgICBzaWduYW1lID0gc2lnbmFsLlNpZ25hbHMo"
    "c2lnbnVtKS5uYW1lCiAgICAgICAgICAgIHNlbGYuX2lzX2Nsb3NpbmcgPSBUcnVlICNUT0RPIHBy"
    "b3Blcmx5IGVuZCBjb25uZWN0aW9ucyBhZnRlciBhIGRlbGF5CiAgICAgICAgICAgIHByaW50KGYn"
    "Q2xvc2luZyBpbiB7dGltZW91dH0nKQogICAgICAgICAgICAjd2hpbGUgKGN1cnJlbnRfdGltZSA6"
    "PSB0aW1lLm1vbm90b25pYygpKSA8IGRlYWRsaW5lOgogICAgICAgICAgICAjICAgIGRlbHRhID0g"
    "aW50KGRlYWRsaW5lIC0gY3VycmVudF90aW1lKQogICAgICAgICAgICAgICAgI1RPRE8gc2lnbmFs"
    "IHRvIHBsYXllcnMgdGhhdCBjb25uZWN0aW9uIGlzIHNodXR0aW5nIGRvd24KICAgICAgICAgICAg"
    "ICAgICMtIHNlbGYuc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCkKICAgICAgICAgICAgICAgICMt"
    "IGYnL2FkbWluIFNlcnZlciBjbG9zaW5nIGluIHtkZWx0YX0nLmVuY29kZSgnYXNjaWknKStfTgog"
    "ICAgICAgICAgICAgICAgI0xPRyBDTE9TRQogICAgICAgICAgICAgICAgI1RPRE8gYmV0dGVyIHNo"
    "dXRkb3duIGhhbmRsaW5nCiAgICAgICAgICAgICMgICAgdGltZS5zbGVlcCgxKQogICAgICAgICAg"
    "ICB0aW1lLnNsZWVwKHRpbWVvdXQpI2FsdCB3aGlsZSBvdGhlciBzdHVmZiBpcyBvbmdvaW5nCiAg"
    "ICAgICAgICAgIHNlbGYuX0Jhc2VTZXJ2ZXJfX3NodXRkb3duX3JlcXVlc3QgPSBUcnVlCiAgICAg"
    "ICAgICAgICNzZWxmLnNodXRkb3duKCkgI29ubHkgaWYgc2VydmVfZm9yZXZlciBpcyBpbiBhIGRp"
    "ZmZlcmVudCB0aHJlYWQKICAgICAgICAgICAgI3NlbGYuc2VydmVyX2Nsb3NlKCkgI29ubHkgbmVl"
    "ZGVkIGlmIG5vdCB1c2luZyBhIHdpdGggc3RhdGVtZW50CiAgICAgICAgcmV0dXJuIGhhbmRsZXIK"
    "ICAgIGRlZiBnZXRQbGF5ZXIoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgIHJldHVybiBzZWxmLnN0"
    "YXRlLmFjdGl2ZVVzZXJzLmdldCh1c2VybmFtZSkKICAgIGRlZiBraWNrUGxheWVyKHNlbGYsIHVz"
    "ZXJuYW1lLCByZWFzb249J0tpY2tlZCBieSBhZG1pbicpOgogICAgICAgICNBZG1pbi1wYW5lbCBh"
    "Y3Rpb246IGZvcmNpYmx5IGRpc2Nvbm5lY3QgYSBjb25uZWN0ZWQgcGxheWVyLiBTZW5kcyBhCiAg"
    "ICAgICAgI2Jlc3QtZWZmb3J0IC9hZG1pbiBub3RpY2UgZmlyc3QgKGNsaWVudCBzaG93cyBpdCBs"
    "aWtlIGFueSBvdGhlcgogICAgICAgICNzZXJ2ZXIgYWRtaW4gbWVzc2FnZSksIHRoZW4gc2h1dHMg"
    "ZG93biB0aGUgc29ja2V0IHNvIHRoZSBwbGF5ZXIncwogICAgICAgICNoYW5kbGVyIHRocmVhZCB1"
    "bmJsb2NrcyBmcm9tIGl0cyByZWN2KCkgYW5kIHJ1bnMgaXRzIG5vcm1hbAogICAgICAgICNkaXNj"
    "b25uZWN0L2NsZWFudXAgcGF0aC4KICAgICAgICBjb24gPSBzZWxmLmdldFBsYXllcih1c2VybmFt"
    "ZSkKICAgICAgICBpZiBjb24gaXMgTm9uZToKICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAg"
    "ICAgdHJ5OgogICAgICAgICAgICBjb24uc2VuZFJhdyhfZW0oZicvYWRtaW4ge3JlYXNvbn0nKSkK"
    "ICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzICNiZXN0IGVmZm9ydCwg"
    "Y29ubmVjdGlvbiBtYXkgYWxyZWFkeSBiZSBvbiBpdHMgd2F5IG91dAogICAgICAgIHRyeToKICAg"
    "ICAgICAgICAgY29uLnJlcXVlc3Quc2h1dGRvd24oc29ja2V0LlNIVVRfUkRXUikKICAgICAgICBl"
    "eGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzCiAgICAgICAgdHJ5OgogICAgICAgICAg"
    "ICBjb24ucmVxdWVzdC5jbG9zZSgpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAg"
    "ICAgcGFzcwogICAgICAgIHJldHVybiBUcnVlCiAgICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1"
    "c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbjogcGVybWFuZW50bHkgZGVsZXRl"
    "cyBhIGNoYXJhY3Rlci9hY2NvdW50LgogICAgICAgICNLaWNrcyBmaXJzdCAobm8tb3AgaWYgYWxy"
    "ZWFkeSBvZmZsaW5lKSBzbyBhIGNvbm5lY3RlZCBjbGllbnQgbmV2ZXIKICAgICAgICAja2VlcHMg"
    "cGxheWluZyBvbiBhbiBhY2NvdW50IHRoYXQgaGFzIGp1c3QgdmFuaXNoZWQgZnJvbSB0aGUgREIu"
    "CiAgICAgICAgc2VsZi5raWNrUGxheWVyKHVzZXJuYW1lLCByZWFzb249J0FjY291bnQgZGVsZXRl"
    "ZCBieSBhZG1pbicpCiAgICAgICAgcmV0dXJuIEdESC5kZWxldGVBY2NvdW50KHVzZXJuYW1lKQpk"
    "ZWYgc3BsaXRRdWVyeShxLHApOgogICAgZm9yIHBycCBpbiBxLnNwbGl0KCcmJyk6CiAgICAgICAg"
    "KGssdikgPSBwcnAuc3BsaXQoJz0nLCBtYXhzcGxpdD0xKQogICAgICAgIHBbdW5xdW90ZShrKV0g"
    "PSB1bnF1b3RlKHYpCmRlZiBkZWJ1Z1JhdGVNZXNzYWdlKHEpOgogICAgaWYgX0VOQUJMRV9ERUJV"
    "R19SQVRFTU9OSVRPUjoKICAgICAgICByZXQgPSB7fQogICAgICAgIGlmICdob3VyJyBpbiBxOgog"
    "ICAgICAgICAgICByZXRbJ0hvdXInXSA9IEdESC5nZXRCeXRlcmF0ZUhvdXIoKQogICAgICAgIGlm"
    "ICdtaW51dGUnIGluIHE6CiAgICAgICAgICAgIHJldFsnTWludXRlJ10gPSBHREguZ2V0Qnl0ZXJh"
    "dGVNaW51dGUoKQogICAgICAgIGlmICdzZWNvbmQnIGluIHE6CiAgICAgICAgICAgIHJldFsnU2Vj"
    "b25kJ10gPSBHREguZ2V0Qnl0ZXJhdGVTZWNvbmQoKQogICAgICAgIHJldHVybiByZXQKICAgIGVs"
    "c2U6CiAgICAgICAgcmV0dXJuIHsnZXJyb3InOidSYXRlIG1vbml0b3JpbmcgaXMgZGlzYWJsZWQu"
    "J30KY2xhc3MgV2ViQXBpU2VydmUoaHR0cC5zZXJ2ZXIuQmFzZUhUVFBSZXF1ZXN0SGFuZGxlcik6"
    "CiAgICBkZWYgZG9fR0VUKHNlbGYpOgogICAgICAgIHNlbGYucHJvdG9jb2xfdmVyc2lvbiA9IF9Q"
    "Uk9UT0NPTF9WRVIKICAgICAgICAjcHJpbnQoJ1JBV0dldFJlcXVlc3Q6Jywgc2VsZi5wYXRoKQog"
    "ICAgICAgIHByZXMgPSB1cmxwYXJzZShzZWxmLnBhdGhbMTpdKQogICAgICAgIHNlbGYucGF0aCA9"
    "IHVucXVvdGUocHJlcy5wYXRoKQogICAgICAgIHFwcm9wcyA9IHt9CiAgICAgICAgaWYgcHJlcy5x"
    "dWVyeToKICAgICAgICAgICAgc3BsaXRRdWVyeShwcmVzLnF1ZXJ5LCBxcHJvcHMpCiAgICAgICAg"
    "ICAgICNwcmludChxcHJvcHMpCiAgICAgICAgaWYgcHJlcy5mcmFnbWVudDoKICAgICAgICAgICAg"
    "cGFzcyNwcmludCgnZnJhZ21lbnQ6JywgdW5xdW90ZShwcmVzLmZyYWdtZW50KSkKICAgICAgICBs"
    "cGF0aCA9IHNlbGYucGF0aC5sb3dlcigpCiAgICAgICAgI3ByaW50KCdXZWJHZXRSZXF1ZXN0Oics"
    "IGxwYXRoKQogICAgICAgIG1lc3NhZ2U9RmFsc2UKICAgICAgICAjVE9ETyBjcmVhdGUgYSBtb3Jl"
    "IGNvcnJlY3QgYXBpCiAgICAgICAgaWYgX0VOQUJMRV9XRUJfREVCVUdfQVBJIGFuZCBscGF0aCA9"
    "PSAnZGVidWcnOgogICAgICAgICAgICAjVE9ETyBxcHJvcHMgZm9yIGxpbWl0aW5nIHJlc3VsdHMK"
    "ICAgICAgICAgICAgbWVzc2FnZSA9IHsKICAgICAgICAgICAgICAgICd2ZXJzaW9uJzpfVkVSU0lP"
    "TiwKICAgICAgICAgICAgICAgICdzdGFydGVkJzpqc29uVGltZShzZWxmLnNlcnZlci5jb3JlLnN0"
    "YXJ0VGltZSkKICAgICAgICAgICAgICAgICNUT0RPIGNvbnNpZGVyIGJldHRlciB0aW1lIGZvcm1h"
    "dHRpbmc/IHVzZSBhIHN0YW5kYXJkPwogICAgICAgICAgICB9CiAgICAgICAgICAgIGlmICdyYXRl"
    "cycgaW4gcXByb3BzOgogICAgICAgICAgICAgICAgbWVzc2FnZVsncmF0ZXMnXSA9IGRlYnVnUmF0"
    "ZU1lc3NhZ2UocXByb3BzWydyYXRlcyddLmxvd2VyKCkuc3BsaXQoJysnKSkKICAgICAgICAgICAg"
    "aWYgJ2xpc3RzJyBpbiBxcHJvcHM6CiAgICAgICAgICAgICAgICBycWxzdCA9IHFwcm9wc1snbGlz"
    "dHMnXS5sb3dlcigpLnNwbGl0KCcrJykKICAgICAgICAgICAgICAgIGlmICdwbGF5ZXInIGluIHJx"
    "bHN0OgogICAgICAgICAgICAgICAgICAgIG1lc3NhZ2VbJ3BsYXllcnMnXSA9IHNlbGYuc2VydmVy"
    "LmNvcmUuZGVidWdfZGljdF9wbGF5ZXJzKCkKICAgICAgICAgICAgICAgIGlmICd0b3duJyBpbiBy"
    "cWxzdDoKICAgICAgICAgICAgICAgICAgICBtZXNzYWdlWyd0b3ducyddID0gc2VsZi5zZXJ2ZXIu"
    "Y29yZS5kZWJ1Z19kaWN0X3Rvd25zKCkKICAgICAgICAgICAgICAgIGlmICdnYW1lJyBpbiBycWxz"
    "dDoKICAgICAgICAgICAgICAgICAgICBtZXNzYWdlWydnYW1lcyddID0gc2VsZi5zZXJ2ZXIuY29y"
    "ZS5kZWJ1Z19hcnJfZ2FtZXMoKQogICAgICAgICAgICAgICAgI1RPRE8gYWNjb3VudCBsaXN0aW5n"
    "PyBhZG1pbiBvbmx5PwogICAgICAgICAgICAjVE9ETyBzdXBwb3J0IGFkbWluIG9ubHkgZGVidWcg"
    "YXBpCiAgICAgICAgICAgICNUT0RPIHNlcnZlciBzdGFydCB0aW1lCiAgICAgICAgICAgIAogICAg"
    "ICAgIGVsaWYgbm90IF9FTkFCTEVfV0VCX0RFQlVHX0FQSSBhbmQgbHBhdGggPT0gJ2RlYnVnJzoK"
    "ICAgICAgICAgICAgcGFzcyNUT0RPIHJldHVybiBlcnJvciBmb3IgZGVidWcgbm90IGVuYWJsZWQK"
    "ICAgICAgICBlbGlmIGxwYXRoID09ICdwbGF5ZXJkYXRhJzoKICAgICAgICAgICAgZXJyb3IgPSAw"
    "CiAgICAgICAgICAgIHBkYXQgPSBiJycKICAgICAgICAgICAgaWYgbm90IF9FTkFCTEVfUERET1dO"
    "TE9BRDoKICAgICAgICAgICAgICAgIGVycm9yID0gJ25vdCBlbmFibGVkJwogICAgICAgICAgICBl"
    "bGlmIG5vdCAnbmFtZScgaW4gcXByb3BzOgogICAgICAgICAgICAgICAgZXJyb3IgPSAnbm8gbmFt"
    "ZSBpbiByZXF1ZXN0JwogICAgICAgICAgICBlbGlmIG5vdCAnZm9ybScgaW4gcXByb3BzOgogICAg"
    "ICAgICAgICAgICAgZXJyb3IgPSAnbm8gZm9ybWF0IGluIHJlcXVlc3QnCgogICAgICAgICAgICAj"
    "VE9ETyBjaGVjayBwZXJtcyBpZiByZXF1aXJlZAogICAgICAgICAgICBpZiBub3QgZXJyb3I6ICMg"
    "Z2V0IHBsYXllcmRhdGEKICAgICAgICAgICAgICAgIHBkYXQgPSBHREguZ2V0UGxheWVyRGF0YShx"
    "cHJvcHNbJ25hbWUnXSwgcXByb3BzWydmb3JtJ10pCiAgICAgICAgICAgICAgICBpZiBub3QgcGRh"
    "dDogIyBlcnJvciBpZiBlbXB0eQogICAgICAgICAgICAgICAgICAgIGVycm9yID0gJ25vIGRhdGEn"
    "CiAgICAgICAgICAgIAogICAgICAgICAgICBpZiBub3QgZXJyb3I6CiAgICAgICAgICAgICAgICBz"
    "ZWxmLnNlbmRfcmVzcG9uc2UoMjAwKQogICAgICAgICAgICAgICAgc2VsZi5zZW5kX2hlYWRlcign"
    "Q29udGVudC1UeXBlJyxfTUlNRV9CSU5BUllfKQogICAgICAgICAgICAgICAgc2VsZi5zZW5kX2hl"
    "YWRlcignQ29udGVudC1MZW5ndGgnLCBsZW4ocGRhdCkpCiAgICAgICAgICAgICAgICBzZWxmLnNl"
    "bmRfaGVhZGVyKCdDb250ZW50LURpc3Bvc2l0aW9uJywgX0RJU1BfUExBWUVSREFUQV8pCiAgICAg"
    "ICAgICAgICAgICBzZWxmLmVuZF9oZWFkZXJzKCkKICAgICAgICAgICAgICAgIHNlbGYud2ZpbGUu"
    "d3JpdGUocGRhdCkKICAgICAgICAgICAgICAgIHJldHVybgogICAgICAgICAgICBlbHNlOgogICAg"
    "ICAgICAgICAgICAgcGFzcyAjVE9ETyByZXR1cm4gZXJyb3IKICAgICAgICAjVE9ETyBubyBjaGF0"
    "IGxvZyBpcyBrZXB0LCBpbXBsZW1lbnQgY2hhdCBsb2dnaW5nIGFuZCBhZG1pbmlzdHJhdGluZz8K"
    "ICAgICAgICBpZiBtZXNzYWdlOgogICAgICAgICAgICBtZXNzYWdlID0ganNvbi5kdW1wcyhtZXNz"
    "YWdlKQogICAgICAgICAgICBzZWxmLnNlbmRfcmVzcG9uc2UoMjAwKQogICAgICAgICAgICBzZWxm"
    "LnNlbmRfaGVhZGVyKCJDb250ZW50LVR5cGUiLCBfTUlNRV9KU09OXykKICAgICAgICAgICAgc2Vs"
    "Zi5zZW5kX2hlYWRlcigiQ29udGVudC1MZW5ndGgiLCBsZW4obWVzc2FnZSkpCiAgICAgICAgICAg"
    "IHNlbGYuZW5kX2hlYWRlcnMoKQogICAgICAgICAgICBzZWxmLndmaWxlLndyaXRlKGJ5dGVzKG1l"
    "c3NhZ2UsICJ1dGY4IikpCiAgICAgICAgZWxzZToKICAgICAgICAgICAgaWYgbm90IHNlbGYuc0Zp"
    "bGUoc2VsZi5wYXRoKToKICAgICAgICAgICAgICAgIHNlbGYuczQwNCgpCiAgICBkZWYgZG9fUE9T"
    "VChzZWxmKTojVE9ETyB1c2UgZm9yIGFkbWluPwogICAgICAgIHNlbGYucHJvdG9jb2xfdmVyc2lv"
    "biA9IF9QUk9UT0NPTF9WRVIKICAgICAgICAjcHJpbnQoJ1JBV1Bvc3RSZXF1ZXN0OicsIHNlbGYu"
    "cGF0aCkKICAgICAgICBwcmVzID0gdXJscGFyc2Uoc2VsZi5wYXRoWzE6XSkKICAgICAgICBzZWxm"
    "LnBhdGggPSB1bnF1b3RlKHByZXMucGF0aCkKICAgICAgICBxcHJvcHMgPSB7fQogICAgICAgIGlm"
    "IHByZXMucXVlcnk6CiAgICAgICAgICAgIHNwbGl0UXVlcnkocHJlcy5xdWVyeSwgcXByb3BzKQog"
    "ICAgICAgIGlmIHByZXMuZnJhZ21lbnQ6CiAgICAgICAgICAgIHBhc3MjcHJpbnQoJ2ZyYWdtZW50"
    "OicsIHVucXVvdGUocHJlcy5mcmFnbWVudCkpCiAgICAgICAgbHBhdGggPSBzZWxmLnBhdGgubG93"
    "ZXIoKQogICAgICAgIGNvbnRlbnRfbGVuID0gaW50KHNlbGYuaGVhZGVycy5nZXQoJ0NvbnRlbnQt"
    "TGVuZ3RoJywgMCkpCiAgICAgICAgI3ByaW50KCdXZWJQb3N0UmVxdWVzdDonLCBscGF0aCkKICAg"
    "ICAgICAjVE9ETyBDSEVDSyBWQUxJRCBBRE1JTiBCRUZPUkUgUkVBRElORyBDT05URU5UCiAgICAg"
    "ICAgI3ByaW50KCJEYXRhTGVuZ3RoOiIsIGNvbnRlbnRfbGVuKQogICAgICAgIGlmIGNvbnRlbnRf"
    "bGVuPF9NQVhfUE9TVDoKICAgICAgICAgICAgaWYgY29udGVudF9sZW46CiAgICAgICAgICAgICAg"
    "ICBwb3N0X2JvZHkgPSBzZWxmLnJmaWxlLnJlYWQoY29udGVudF9sZW4pCiAgICAgICAgICAgIGVs"
    "c2U6CiAgICAgICAgICAgICAgICBwb3N0X2JvZHkgPSBiJycKICAgICAgICBlbHNlOgogICAgICAg"
    "ICAgICByZXR1cm4gI1BPU1QgVE9PIEJJRyBFUlJPUgogICAgICAgICNUT0RPIGZvciBhZG1pbiBz"
    "dHVmZgogICAgZGVmIHNGaWxlKHNlbGYsIHJlcXBhdGgpOgogICAgICAgIHRyeToKICAgICAgICAg"
    "ICAgaWYocmVxcGF0aCBpbiBfTUFOVUFMX0ZJTEVTKToKICAgICAgICAgICAgICAgIFtwYXRoLCBt"
    "aW1lLCBiaW5hcnldID0gX01BTlVBTF9GSUxFU1tyZXFwYXRoXQogICAgICAgICAgICAgICAgaWYo"
    "YmluYXJ5KToKICAgICAgICAgICAgICAgICAgICBtZXNzYWdlID0gcmVhZEJpbihwYXRoKQogICAg"
    "ICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgICAgICBtZXNzYWdlID0gcmVhZFRleHQo"
    "cGF0aCkKICAgICAgICAgICAgICAgIHNlbGYuc2VuZF9yZXNwb25zZSgyMDApCiAgICAgICAgICAg"
    "ICAgICBzZWxmLnNlbmRfaGVhZGVyKCdDb250ZW50LVR5cGUnLCBtaW1lKQogICAgICAgICAgICAg"
    "ICAgc2VsZi5zZW5kX2hlYWRlcignQ29udGVudC1MZW5ndGgnLCBsZW4obWVzc2FnZSkpCiAgICAg"
    "ICAgICAgICAgICBzZWxmLmVuZF9oZWFkZXJzKCkKICAgICAgICAgICAgICAgIGlmIGJpbmFyeToK"
    "ICAgICAgICAgICAgICAgICAgICBzZWxmLndmaWxlLndyaXRlKG1lc3NhZ2UpCiAgICAgICAgICAg"
    "ICAgICBlbHNlOgogICAgICAgICAgICAgICAgICAgIHNlbGYud2ZpbGUud3JpdGUoYnl0ZXMobWVz"
    "c2FnZSwgJ3V0ZjgnKSkKICAgICAgICAgICAgICAgIHJldHVybiBUcnVlCiAgICAgICAgZXhjZXB0"
    "IEZpbGVOb3RGb3VuZEVycm9yOgogICAgICAgICAgICByZXR1cm4gRmFsc2UgI2luIGNhc2UgYSBm"
    "aWxlIHdhcyBkZWxldGVkIChvciBpbmRleCBtaXNzaW5nKSBhZnRlciBsYXVuY2gKICAgICAgICBy"
    "ZXR1cm4gRmFsc2UKICAgIGRlZiBzNDA0KHNlbGYpOgogICAgICAgIGlmIG5vdCBzZWxmLnNGaWxl"
    "KCc0MDQuaHRtbCcpOgogICAgICAgICAgICBtZXNzYWdlID0gJ0ZpbGUgTm90IEZvdW5kJyAjVE9E"
    "TyA0MDQgcGFnZT8KICAgICAgICAgICAgc2VsZi5zZW5kX3Jlc3BvbnNlKDQwNCkKICAgICAgICAg"
    "ICAgc2VsZi5zZW5kX2hlYWRlcignQ29udGVudC1MZW5ndGgnLCBsZW4obWVzc2FnZSkpCiAgICAg"
    "ICAgICAgIHNlbGYuZW5kX2hlYWRlcnMoKQogICAgICAgICAgICBzZWxmLndmaWxlLndyaXRlKGJ5"
    "dGVzKG1lc3NhZ2UsICd1dGY4JykpCgojVE9ETyBpc24ndCBzdGFja2FibGU/IGhvdyBkb2VzIFRM"
    "UyB3cmFwIHdvcmsgZm9yIGh0dHBzPwojVE9ETyBtYWtlIG9uZSBmb3IgaW1wcm92ZWQgbG9naW4g"
    "c2VjdXJpdHkgZm9yIFRXMQojLSBpbXByb3ZlZCBsb2dpbiBzZWN1cml0eSByZXF1aXJlcyBzZXJ2"
    "ZXIgbm90IGtub3cgcGFzc3dvcmQgZWl0aGVyLCBhcyBpdCBpcyB1bnRydXN0ZWQKY2xhc3MgUmF0"
    "ZU1vbml0b3Ioc29ja2V0LnNvY2tldCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwgc29jayk6CiAg"
    "ICAgICAga3dhcmdzID0gZGljdCgKICAgICAgICAgICAgZmFtaWx5PXNvY2suZmFtaWx5LCB0eXBl"
    "PXNvY2sudHlwZSwgcHJvdG89c29jay5wcm90bywKICAgICAgICAgICAgZmlsZW5vPXNvY2suZmls"
    "ZW5vKCkKICAgICAgICApCiAgICAgICAgc3VwZXIoKS5fX2luaXRfXygqKmt3YXJncykKICAgICAg"
    "ICBzb2NrLmRldGFjaCgpCiAgICBkZWYgcmVjdihzZWxmLCBtYXhsZW4pOgogICAgICAgIHZhbCA9"
    "IHN1cGVyKCkucmVjdihtYXhsZW4pCiAgICAgICAgI3ByaW50KGYnUmVjaWV2ZWQge2xlbih2YWwp"
    "fSBieXRlcycpCiAgICAgICAgR0RILkxvYmJ5UmVjdlJhdGVzLmxvZ0J5dGVzKGxlbih2YWwpKQog"
    "ICAgICAgIHJldHVybiB2YWwKICAgIGRlZiBzZW5kYWxsKHNlbGYsIHJlc3BvbnNlKToKICAgICAg"
    "ICAjcHJpbnQoZidTZW5kaW5nIHtsZW4ocmVzcG9uc2UpfSBieXRlcycpCiAgICAgICAgR0RILkxv"
    "YmJ5U2VuZFJhdGVzLmxvZ0J5dGVzKGxlbihyZXNwb25zZSkpCiAgICAgICAgcmV0dXJuIHN1cGVy"
    "KCkuc2VuZGFsbChyZXNwb25zZSkKICAgIGRlZiBhY2NlcHQoc2VsZik6CiAgICAgICAgbmV3c29j"
    "aywgYWRkciA9IHN1cGVyKCkuYWNjZXB0KCkKICAgICAgICBuZXdzb2NrID0gUmF0ZU1vbml0b3Io"
    "bmV3c29jaykKICAgICAgICByZXR1cm4gbmV3c29jaywgYWRkcgoKIyhfU0hPUlRfVElNRU9VVCB1"
    "c2VkIHRvIGJlIHRoZSAwLjFzIHJlY3YoKSB0aW1lb3V0IHRoYXQgYWxzbyBkb3VibGVkIGFzIHRo"
    "ZQojb3V0Ym91bmQgZmx1c2ggaW50ZXJ2YWw7IHRoZSByZWFkIGxvb3Agbm93IHVzZXMgX1JFQURf"
    "VElNRU9VVCBwdXJlbHkgdG8gbm90aWNlCiNzaHV0ZG93biwgYW5kIHNlbmRpbmcgaXMgdGhlIHdy"
    "aXRlciB0aHJlYWQncyBqb2IuKQoKI0ZhaWxlZC1sb2dpbiB0aHJvdHRsZSwgcGVyIHNvdXJjZSBJ"
    "UC4KI1R3byByZWFzb25zIHRoaXMgaXMgbm90IG9wdGlvbmFsIG9uIGEgc2VydmVyIHJlYWNoYWJs"
    "ZSBmcm9tIHRoZSBpbnRlcm5ldDoKI2EgcGFzc3dvcmQgZ3Vlc3MgaXMgY2hlYXAgZm9yIHRoZSBh"
    "dHRhY2tlciBidXQgY29zdHMgKnVzKiBhIDEwMGstaXRlcmF0aW9uCiNQQktERjIgKHRlbnMgb2Yg"
    "bXMgb2YgQ1BVIGVhY2gpLCBzbyBhbiB1bnRocm90dGxlZCBsb2dpbiBlbmRwb2ludCBpcyBib3Ro"
    "IGEKI2JydXRlLWZvcmNlIG9yYWNsZSBhbmQgYSBDUFUgYW1wbGlmaWVyIC0gYSBoYW5kZnVsIG9m"
    "IGNvbm5lY3Rpb25zIGNhbiBwaW4KI2V2ZXJ5IGNvcmUuIFN1Y2Nlc3NmdWwgbG9naW5zIGNsZWFy"
    "IHRoZSBjb3VudGVyLCBzbyBhIHBsYXllciBmdW1ibGluZyB0aGVpcgojcGFzc3dvcmQgYSBmZXcg"
    "dGltZXMgaXMgbmV2ZXIgbG9ja2VkIG91dCBmb3IgbG9uZy4KX0xPR0lOX0ZBSUxfTElNSVQgPSA2"
    "ICAgICAgI2ZhaWx1cmVzIGFsbG93ZWQgaW5zaWRlIHRoZSB3aW5kb3cgYmVmb3JlIGRlbGF5aW5n"
    "Cl9MT0dJTl9GQUlMX1dJTkRPVyA9IDMwMCAgICNzZWNvbmRzIGEgZmFpbHVyZSBpcyByZW1lbWJl"
    "cmVkCl9MT0dJTl9GQUlMX0RFTEFZID0gMi4wICAgICNzZWNvbmRzIHRvIHN0YWxsIGVhY2ggYXR0"
    "ZW1wdCBvbmNlIG92ZXIgdGhlIGxpbWl0CmNsYXNzIExvZ2luVGhyb3R0bGUoKToKICAgIGRlZiBf"
    "X2luaXRfXyhzZWxmKToKICAgICAgICBzZWxmLmxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAg"
    "ICAgc2VsZi5mYWlscyA9IHt9ICNpcCAtPiBbdGltZXN0YW1wc10KICAgIGRlZiBfcHJ1bmUoc2Vs"
    "ZiwgaXAsIG5vdyk6CiAgICAgICAgcmVjZW50ID0gW3QgZm9yIHQgaW4gc2VsZi5mYWlscy5nZXQo"
    "aXAsICgpKSBpZiBub3cgLSB0IDwgX0xPR0lOX0ZBSUxfV0lORE9XXQogICAgICAgIGlmIHJlY2Vu"
    "dDoKICAgICAgICAgICAgc2VsZi5mYWlsc1tpcF0gPSByZWNlbnQKICAgICAgICBlbHNlOgogICAg"
    "ICAgICAgICBzZWxmLmZhaWxzLnBvcChpcCwgTm9uZSkKICAgICAgICByZXR1cm4gcmVjZW50CiAg"
    "ICBkZWYgZGVsYXlGb3Ioc2VsZiwgaXApOgogICAgICAgIG5vdyA9IHRpbWUubW9ub3RvbmljKCkK"
    "ICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgcmVjZW50ID0gc2VsZi5fcHJ1bmUo"
    "aXAsIG5vdykKICAgICAgICByZXR1cm4gX0xPR0lOX0ZBSUxfREVMQVkgaWYgbGVuKHJlY2VudCkg"
    "Pj0gX0xPR0lOX0ZBSUxfTElNSVQgZWxzZSAwLjAKICAgIGRlZiByZWNvcmRGYWlsdXJlKHNlbGYs"
    "IGlwKToKICAgICAgICBub3cgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgd2l0aCBzZWxmLmxv"
    "Y2s6CiAgICAgICAgICAgIHJlY2VudCA9IHNlbGYuX3BydW5lKGlwLCBub3cpCiAgICAgICAgICAg"
    "IHJlY2VudC5hcHBlbmQobm93KQogICAgICAgICAgICBzZWxmLmZhaWxzW2lwXSA9IHJlY2VudAog"
    "ICAgICAgICAgICByZXR1cm4gbGVuKHJlY2VudCkKICAgIGRlZiByZWNvcmRTdWNjZXNzKHNlbGYs"
    "IGlwKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgc2VsZi5mYWlscy5wb3Ao"
    "aXAsIE5vbmUpCkxPR0lOX1RIUk9UVExFID0gTG9naW5UaHJvdHRsZSgpCgpfTE9HSU5fRVJST1JT"
    "ID0gewogICAgMTogJ0ludmFsaWQgdXNlcm5hbWUgb3IgcGFzc3dvcmQnLAogICAgMjogJ0FjY291"
    "bnQgYWxyZWFkeSBsb2dnZWQgaW4nLAogICAgMzogJ1Bhc3N3b3JkIHJlcXVpcmVkJywKICAgIDQ6"
    "ICdVc2VybmFtZSByZXF1aXJlZCcsCn0KX1JFR0lTVEVSX0VSUk9SUyA9IHsKICAgIDE6ICdBY2Nv"
    "dW50IGFscmVhZHkgbG9nZ2VkIGluJywKICAgIDI6ICdVc2VybmFtZSB1bmF2YWlsYWJsZSBvciBp"
    "bnZhbGlkJywKfQojaGFuZGxlcyBpbmRpdmlkdWFsIGNvbm5lY3Rpb25zCmNsYXNzIENvbm5lY3Rp"
    "b25IYW5kbGVyKHNvY2tldHNlcnZlci5CYXNlUmVxdWVzdEhhbmRsZXIpOgogICAgI2RlZmF1bHQg"
    "cHJvcGVydGllczoKICAgICMgLSByZXF1ZXN0OiBzb2NrZXQgdG8gZGVzdGluYXRpb24KICAgICMg"
    "LSBjbGllbnRfYWRkcmVzcwogICAgIyAtIHNlcnZlcjogQ29yZVNlcnZlcgogICAgX1NUT1BXUklU"
    "RVIgPSBvYmplY3QoKQogICAgZGVmIHNldHVwKHNlbGYpOgogICAgICAgIHNlbGYuX3NRdWV1ZSA9"
    "IFNpbXBsZVF1ZXVlKCkKICAgICAgICBzZWxmLnVzZXIgPSBOb25lCiAgICAgICAgc2VsZi5ndWlk"
    "ID0gTm9uZQogICAgICAgIHNlbGYuZGF0YSA9IGInJwogICAgICAgIHNlbGYuU0sgPSBieXRlYXJy"
    "YXkoc3RydWN0LnBhY2soJzxJSScsIDB4QTZBRTFGOUIsIDB4NDM4REZGNDApKQogICAgICAgICNT"
    "ZXJpYWxpc2VzIHRoZSByYXcgc29ja2V0IHdyaXRlcy4gVGhyZWUgdGhyZWFkcyBjYW4gd2FudCB0"
    "byB3cml0ZSB0bwogICAgICAgICNvbmUgY2xpZW50OiB0aGlzIGNvbm5lY3Rpb24ncyBvd24gcmVh"
    "ZCBsb29wIChkdXJpbmcgdGhlIGhhbmRzaGFrZSksCiAgICAgICAgI2l0cyB3cml0ZXIgdGhyZWFk"
    "LCBhbmQgdGhlIEdVSSB0aHJlYWQgdmlhIGtpY2tQbGF5ZXIoKS4gV2l0aG91dCB0aGUKICAgICAg"
    "ICAjbG9jayB0d28gc2VuZGFsbCgpIGNhbGxzIGNhbiBpbnRlcmxlYXZlIGFuZCBzcGxpdCBhIHBh"
    "Y2tldCBkb3duIHRoZQogICAgICAgICNtaWRkbGUsIHdoaWNoIHRoZSBjbGllbnQgc2VlcyBhcyBw"
    "cm90b2NvbCBnYXJiYWdlLgogICAgICAgIHNlbGYuX3NlbmRMb2NrID0gdGhyZWFkaW5nLkxvY2so"
    "KQogICAgICAgIHNlbGYuX3dyaXRlciA9IE5vbmUKICAgICAgICBzZWxmLl93cml0ZXJEZWFkID0g"
    "dGhyZWFkaW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3Rvbmlj"
    "KCkKICAgICAgICB0cnk6CiAgICAgICAgICAgICNOYWdsZSBiYXRjaGVzIHNtYWxsIHdyaXRlcyBi"
    "eSBob2xkaW5nIHRoZW0gZm9yIHVwIHRvIH40MG1zIHdhaXRpbmcKICAgICAgICAgICAgI2ZvciBt"
    "b3JlIGRhdGEuIEV2ZXJ5IG1lc3NhZ2UgdGhpcyBzZXJ2ZXIgc2VuZHMgaXMgc21hbGwgYW5kCiAg"
    "ICAgICAgICAgICNsYXRlbmN5LXNlbnNpdGl2ZSAtIGNoYXQsIHBvc2l0aW9uIHVwZGF0ZXMgYW5k"
    "IGFib3ZlIGFsbCB0aGUKICAgICAgICAgICAgIy9nYW1lY29tbWFuZHRvdXNlciByZWxheSB0aGF0"
    "IGNhcnJpZXMgdGhlIGFjdHVhbCBpbi1nYW1lIGNvLW9wCiAgICAgICAgICAgICN0cmFmZmljIGJl"
    "dHdlZW4gdHdvIHBsYXllcnMgLSBzbyB0aGUgZGVsYXkgaXMgcHVyZSBhZGRlZCBsYWcuCiAgICAg"
    "ICAgICAgIHNlbGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5JUFBST1RPX1RDUCwgc29ja2V0"
    "LlRDUF9OT0RFTEFZLCAxKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICBwYXNz"
    "ICNub3QgZmF0YWwsIGp1c3Qgc2xvd2VyCiAgICBkZWYgc2VuZFJhdyhzZWxmLCBtc2cpOgogICAg"
    "ICAgICNUaGUgc2luZ2xlIGZ1bm5lbCBmb3IgZXZlcnkgYnl0ZSBsZWF2aW5nIHRoZSBzZXJ2ZXIg"
    "b24gdGhpcyBzb2NrZXQuCiAgICAgICAgd2l0aCBzZWxmLl9zZW5kTG9jazoKICAgICAgICAgICAg"
    "c2VsZi5yZXF1ZXN0LnNlbmRhbGwobXNnKQogICAgZGVmIHNlbmQoc2VsZiwgbXNnKToKICAgICAg"
    "ICAjTm9ybWFsIHBhdGggb25jZSB0aGUgY29ubmVjdGlvbiBpcyBsaXZlOiBoYW5kIG9mZiB0byB0"
    "aGUgd3JpdGVyIHRocmVhZAogICAgICAgICNzbyB0aGUgY2FsbGVyIChhIGNvbW1hbmQgaGFuZGxl"
    "ciwgb3IgdGhlIGRpc3RyaWJ1dG9yJ3MgZmFuLW91dCkgbmV2ZXIKICAgICAgICAjYmxvY2tzIG9u"
    "IGEgc2xvdyBvciBzdGFsbGVkIGNsaWVudC4KICAgICAgICBpZiBtc2c6CiAgICAgICAgICAgIHNl"
    "bGYuX3NRdWV1ZS5wdXQobXNnKQogICAgZGVmIF93cml0ZXJMb29wKHNlbGYpOgogICAgICAgICNC"
    "bG9ja3Mgb24gdGhlIHF1ZXVlIGluc3RlYWQgb2YgYmVpbmcgcG9sbGVkLiBQcmV2aW91c2x5IHRo"
    "ZSByZWFkIGxvb3AKICAgICAgICAjZHJhaW5lZCB0aGlzIHF1ZXVlIGl0c2VsZiBiZXR3ZWVuIHJl"
    "Y3YoKSB0aW1lb3V0cywgc28gYW55dGhpbmcgcXVldWVkCiAgICAgICAgI2p1c3QgYWZ0ZXIgdGhl"
    "IHRocmVhZCB3ZW50IGJhY2sgaW50byByZWN2KCkgd2FpdGVkIG91dCB0aGUgZnVsbAogICAgICAg"
    "ICN0aW1lb3V0IC0gdXAgdG8gMTAwbXMgb2YgbGF0ZW5jeSBhZGRlZCB0byBldmVyeSByZWxheWVk"
    "IGdhbWUgY29tbWFuZCwKICAgICAgICAjb24gdG9wIG9mIGV2ZXJ5IGlkbGUgY29ubmVjdGlvbiB3"
    "YWtpbmcgMTAgdGltZXMgYSBzZWNvbmQgdG8gY2hlY2suCiAgICAgICAgdHJ5OgogICAgICAgICAg"
    "ICB3aGlsZSBUcnVlOgogICAgICAgICAgICAgICAgbXNnID0gc2VsZi5fc1F1ZXVlLmdldCgpCiAg"
    "ICAgICAgICAgICAgICBpZiBtc2cgaXMgc2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAg"
    "ICAgICBicmVhawogICAgICAgICAgICAgICAgI0NvYWxlc2NlIHdoYXRldmVyIGVsc2UgcGlsZWQg"
    "dXAgYmVoaW5kIGl0IGludG8gYSBzaW5nbGUgd3JpdGUuCiAgICAgICAgICAgICAgICAjUG9zaXRp"
    "b24gYnJvYWRjYXN0cyBhbmQgZ2FtZSBjb21tYW5kcyBvZnRlbiBhcnJpdmUgaW4gYnVyc3RzLgog"
    "ICAgICAgICAgICAgICAgY2h1bmtzID0gW21zZ10KICAgICAgICAgICAgICAgIHdoaWxlIFRydWU6"
    "CiAgICAgICAgICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgICAgICAgICBueHQgPSBz"
    "ZWxmLl9zUXVldWUuZ2V0X25vd2FpdCgpCiAgICAgICAgICAgICAgICAgICAgZXhjZXB0IEV4Y2Vw"
    "dGlvbjoKICAgICAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgICAgICAgICBp"
    "ZiBueHQgaXMgc2VsZi5fU1RPUFdSSVRFUjoKICAgICAgICAgICAgICAgICAgICAgICAgc2VsZi5z"
    "ZW5kUmF3KGInJy5qb2luKGNodW5rcykpCiAgICAgICAgICAgICAgICAgICAgICAgIHJldHVybgog"
    "ICAgICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQobnh0KQogICAgICAgICAgICAgICAgc2Vs"
    "Zi5zZW5kUmF3KGInJy5qb2luKGNodW5rcykpCiAgICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVz"
    "ZXRFcnJvciwgQ29ubmVjdGlvbkFib3J0ZWRFcnJvciwgQnJva2VuUGlwZUVycm9yLCBPU0Vycm9y"
    "KToKICAgICAgICAgICAgcGFzcyAjcGVlciBpcyBnb25lOyB0aGUgcmVhZCBsb29wIG5vdGljZXMg"
    "YW5kIHJ1bnMgdGhlIGNsZWFudXAKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAg"
    "ICBwcmludCgnW0xvYmJ5XSBXcml0ZXIgZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMo"
    "KSkKICAgICAgICBmaW5hbGx5OgogICAgICAgICAgICBzZWxmLl93cml0ZXJEZWFkLnNldCgpCiAg"
    "ICBkZWYgX3N0YXJ0V3JpdGVyKHNlbGYpOgogICAgICAgIHNlbGYuX3dyaXRlciA9IHRocmVhZGlu"
    "Zy5UaHJlYWQodGFyZ2V0PXNlbGYuX3dyaXRlckxvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNl"
    "bGYuX3dyaXRlci5zdGFydCgpCiAgICBkZWYgX3N0b3BXcml0ZXIoc2VsZik6CiAgICAgICAgaWYg"
    "c2VsZi5fd3JpdGVyIGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVybgogICAgICAgIHNlbGYuX3NR"
    "dWV1ZS5wdXQoc2VsZi5fU1RPUFdSSVRFUikKICAgICAgICBzZWxmLl93cml0ZXIuam9pbih0aW1l"
    "b3V0PTIuMCkKICAgICAgICBzZWxmLl93cml0ZXIgPSBOb25lCiAgICBkZWYgX2NsYWltU2Vzc2lv"
    "bihzZWxmKToKICAgICAgICAjVGFrZSBvd25lcnNoaXAgb2YgdGhlIHVzZXJuYW1lIHNsb3QgYmVm"
    "b3JlIHRlbGxpbmcgdGhlIGNsaWVudCBpdCBpcwogICAgICAgICNsb2dnZWQgaW4uIFJldHVybnMg"
    "RmFsc2UgaWYgYW5vdGhlciBjb25uZWN0aW9uIGdvdCB0aGVyZSBmaXJzdC4KICAgICAgICBpZiBz"
    "ZWxmLnNlcnZlci5zdGF0ZS5jbGFpbVVzZXIoc2VsZi51c2VyLm5hbWUsIHNlbGYpOgogICAgICAg"
    "ICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHNlbGYudXNlci5kaXNjb25uZWN0KHNlbGYuc2VydmVy"
    "KSAjcmVsZWFzZXMgdGhlIGlkbnVtIHdlIGp1c3QgYWxsb2NhdGVkCiAgICAgICAgc2VsZi51c2Vy"
    "ID0gTm9uZQogICAgICAgIHJldHVybiBGYWxzZQogICAgZGVmIGF0dGVtcHRMb2dpbihzZWxmLCB1"
    "c2VybmFtZSwgcGFzc3dvcmQpOgogICAgICAgIGlmIGxlbih1c2VybmFtZSk8MToKICAgICAgICAg"
    "ICAgcmV0dXJuIDQgI05vIFVzZXJuYW1lLCBsaWtlbHkgZnJlc2ggbG9naW4KICAgICAgICAgICAg"
    "I1RPRE8gY2hlY2sgaWYgc2VyaWFsIGV4aXN0cyBhbmQgcmV0dXJuIHVzZXJuYW1lIHByb3Blcmx5"
    "CiAgICAgICAgaWYgbGVuKHBhc3N3b3JkKTwxOgogICAgICAgICAgICByZXR1cm4gMyAjUGFzc3dv"
    "cmQgdG9vIHNob3J0CiAgICAgICAgI1Rlc3QgaWYgcGxheWVyIGFscmVhZHkgbG9nZ2VkIGluIChm"
    "YXN0IHBhdGg7IHRoZSBhdXRob3JpdGF0aXZlLAogICAgICAgICNyYWNlLWZyZWUgY2hlY2sgaXMg"
    "dGhlIGNsYWltVXNlcigpIGJlbG93KQogICAgICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1"
    "c2VybmFtZSk6CiAgICAgICAgICAgIHJldHVybiAyICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJS"
    "T1IKICAgICAgICAjcGxheWVyIG5vdCBjdXJyZW50bHkgbG9nZ2VkIGluLCBhdHRlbXB0IHRvIGxv"
    "Z2luIHZpYSBkYXRhIGhhbmRsZXIKICAgICAgICBzZWxmLnVzZXIgPSBHREgubG9naW5QbGF5ZXIo"
    "dXNlcm5hbWUsIHNlbGYsIHBhc3N3b3JkKQogICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAg"
    "ICAgcmV0dXJuIDAgaWYgc2VsZi5fY2xhaW1TZXNzaW9uKCkgZWxzZSAyCiAgICAgICAgcmV0dXJu"
    "IDEgI1RPRE8gR2V0IGZyb20gR0RILmxvZ2luUGxheWVyLCBwYXNzIHVzZXIgb2JqZWN0IGFsb25n"
    "PwogICAgZGVmIGF0dGVtcHRSZWdpc3RlcihzZWxmLCB1c2VybmFtZSwgcGFzc3dvcmQsIGVtYWls"
    "LCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKToKICAgICAgICAjVGVzdCBpZiBw"
    "bGF5ZXIgYWxyZWFkeSBsb2dnZWQgaW4KICAgICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIo"
    "dXNlcm5hbWUpOgogICAgICAgICAgICByZXR1cm4gMSAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVS"
    "Uk9SCiAgICAgICAgc2VsZi51c2VyID0gR0RILnJlZ2lzdGVyUGxheWVyKHVzZXJuYW1lLCBzZWxm"
    "LCBwYXNzd29yZCwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAg"
    "ICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICByZXR1cm4gMCBpZiBzZWxmLl9jbGFpbVNl"
    "c3Npb24oKSBlbHNlIDEKICAgICAgICByZXR1cm4gMiAjVE9ETyBnZXQgZXJyb3IgZnJvbSBHREgK"
    "ICAgIGRlZiBoYW5kbGUoc2VsZik6CiAgICAgICAgdHJ5OiAjSW50ZXJjZXB0IGFuZCBwcmludCBl"
    "cnJvcnMgZm9yIGRlYnVnZ2luZwogICAgICAgICAgICBzZWxmLl9oYW5kbGUoKQogICAgICAgICAg"
    "ICAjVE9ETyBsb29wIGxvYmJ5IGhhbmRsZSBiZXR0ZXIgdG8gaGFuZGxlIGV4Y2VwdGlvbnMgZ3Jh"
    "Y2VmdWxseQogICAgICAgICAgICBzZWxmLl9sb2JieUhhbmRsZSgpCiAgICAgICAgZXhjZXB0IFBy"
    "b3RvY29sRXJyb3IgYXMgZToKICAgICAgICAgICAgI21hbGZvcm1lZC9vdmVyc2l6ZWQgaW5wdXQg"
    "LSB0aGUgY2xpZW50J3MgZmF1bHQsIG5vdCBvdXJzLiBEcm9wIHRoZQogICAgICAgICAgICAjY29u"
    "bmVjdGlvbiB3aXRoIG9uZSBsaW5lIGluc3RlYWQgb2YgYSB0cmFjZWJhY2suCiAgICAgICAgICAg"
    "IHdobyA9IHNlbGYudXNlci5uYW1lIGlmIHNlbGYudXNlciBlbHNlIHNlbGYuY2xpZW50X2FkZHJl"
    "c3NbMF0KICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIFByb3RvY29sIGVycm9yIGZyb20ge3do"
    "b306IHtlfScpCiAgICAgICAgZXhjZXB0ICh6bGliLmVycm9yLCBzdHJ1Y3QuZXJyb3IsIFVuaWNv"
    "ZGVEZWNvZGVFcnJvcikgYXMgZToKICAgICAgICAgICAgI3RydW5jYXRlZC9nYXJiYWdlIHBhY2tl"
    "dDogcGFyc2VEc3RyIGFuZCBzdHJ1Y3QudW5wYWNrIGJvdGggcmFpc2Ugb24KICAgICAgICAgICAg"
    "I3Nob3J0IHJlYWRzLCBhbmQgLmRlY29kZSgpIG9uIG5vbi1hc2NpaSBqdW5rLiBTYW1lIGNhdGVn"
    "b3J5LgogICAgICAgICAgICBwcmludChmJ1tMb2JieV0gTWFsZm9ybWVkIHBhY2tldCBmcm9tIHtz"
    "ZWxmLmNsaWVudF9hZGRyZXNzWzBdfTogJwogICAgICAgICAgICAgICAgICBmJ3t0eXBlKGUpLl9f"
    "bmFtZV9ffToge2V9JykKICAgICAgICBleGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25u"
    "ZWN0aW9uQWJvcnRlZEVycm9yLCBPU0Vycm9yKSBhcyBlOgogICAgICAgICAgICAjIGV4cGVjdGVk"
    "IGZvcm0gb2YgZGlzY29ubmVjdGlvbiAoaW5jbHVkaW5nIGEgZm9yY2VkIGFkbWluIGtpY2spLAog"
    "ICAgICAgICAgICAjIGJ1dCBsZWF2ZSBhIG9uZS1saW5lIGJyZWFkY3J1bWIgcmF0aGVyIHRoYW4g"
    "c3RheWluZyBmdWxseSBzaWxlbnQKICAgICAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAg"
    "ICAgICAgcHJpbnQoZidbTG9iYnldIENvbm5lY3Rpb24gY2xvc2VkIGZvciB7c2VsZi51c2VyLm5h"
    "bWV9OiB7ZX0nKQogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246IyBhcyBlOgogICAgICAgICAgICBw"
    "cmludCh0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAg"
    "ICAgICAgICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFtZX0nKQogICAgICAgICAg"
    "ICAjcmFpc2UgZQogICAgZGVmIF9sb2JieUhhbmRsZShzZWxmKToKICAgICAgICAjYWN0aXZlVXNl"
    "cnNbLi4uXSA9IHNlbGYgdXNlZCB0byBoYXBwZW4gaGVyZTsgaXQgbm93IGhhcHBlbnMgdW5kZXIg"
    "YQogICAgICAgICNsb2NrIGluc2lkZSBhdHRlbXB0TG9naW4vYXR0ZW1wdFJlZ2lzdGVyLCBiZWZv"
    "cmUgdGhlIHdlbGNvbWUgcGFja2V0CiAgICAgICAgI2dvZXMgb3V0LCBzbyB0d28gbG9naW5zIGZv"
    "ciBvbmUgYWNjb3VudCBjYW4ndCBib3RoIHN1Y2NlZWQuCiAgICAgICAgcHJpbnQoZidVc2VyOiB7"
    "c2VsZi51c2VyLm5hbWV9IENvbm5lY3RlZCcpCiAgICAgICAgI0Zyb20gaGVyZSBvbiBub3RoaW5n"
    "IHdyaXRlcyB0byB0aGUgc29ja2V0IGlubGluZTogdGhlIHdyaXRlciB0aHJlYWQKICAgICAgICAj"
    "b3ducyB0aGUgb3V0Ym91bmQgZGlyZWN0aW9uIGFuZCB0aGlzIGxvb3Agb25seSByZWFkcy4KICAg"
    "ICAgICBzZWxmLl9zdGFydFdyaXRlcigpCiAgICAgICAgc2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1v"
    "bm90b25pYygpCiAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAgICAgICAgaWYgc2VsZi5fd3JpdGVy"
    "RGVhZC5pc19zZXQoKToKICAgICAgICAgICAgICAgIGJyZWFrICNwZWVyIHdlbnQgYXdheSB3aGls"
    "ZSB3ZSB3ZXJlIHNlbmRpbmcKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHRpbWVvdXQoX1JF"
    "QURfVElNRU9VVCkKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgcm1zZyA9IHNlbGYu"
    "cmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikgI1RPRE8gbG9nIG5ldHdvcmsgYnl0ZXJhdGUKICAg"
    "ICAgICAgICAgICAgIGlmIG5vdCBybXNnOgogICAgICAgICAgICAgICAgICAgIGJyZWFrICNEaXNj"
    "b25uZWN0ZWQKICAgICAgICAgICAgICAgIHNlbGYuZGF0YSs9cm1zZwogICAgICAgICAgICAgICAg"
    "c2VsZi5fbGFzdFJlY3YgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgICAgIGV4Y2VwdCBUaW1l"
    "b3V0RXJyb3I6CiAgICAgICAgICAgICAgICBpZiBzZWxmLnNlcnZlci5faXNfY2xvc2luZzoKICAg"
    "ICAgICAgICAgICAgICAgICBicmVhayAjU2VydmVyIFNodXR0aW5nIGRvd24KICAgICAgICAgICAg"
    "ICAgIGlmIF9JRExFX1RJTUVPVVQgYW5kICh0aW1lLm1vbm90b25pYygpIC0gc2VsZi5fbGFzdFJl"
    "Y3YpID4gX0lETEVfVElNRU9VVDoKICAgICAgICAgICAgICAgICAgICAjSGFsZi1vcGVuIGNvbm5l"
    "Y3Rpb246IHRoZSBwZWVyIGlzIHVucmVhY2hhYmxlIGJ1dCBuZXZlcgogICAgICAgICAgICAgICAg"
    "ICAgICNzZW50IGEgRklOL1JTVCwgc28gcmVjdigpIGJsb2NrcyBmb3JldmVyIGFuZCB0aGUgYWNj"
    "b3VudAogICAgICAgICAgICAgICAgICAgICNzdGF5cyBjbGFpbWVkLiBSZWFwIGl0IHNvIHRoZSBw"
    "bGF5ZXIgY2FuIGxvZyBiYWNrIGluLgogICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5"
    "XSB7c2VsZi51c2VyLm5hbWV9IGlkbGUgZm9yIHtfSURMRV9USU1FT1VUfXMsIGRyb3BwaW5nJykK"
    "ICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICAgICAgY29udGludWUKICAgICAg"
    "ICAgICAgc2VsZi5yZXF1ZXN0LnNldHRpbWVvdXQoTm9uZSkjYmxvYiByZWFkcyBiZWxvdyBtdXN0"
    "IG5vdCB0aW1lIG91dAogICAgICAgICAgICB3aGlsZSBzZWxmLmRhdGE6CiAgICAgICAgICAgICAg"
    "ICB0cnk6CiAgICAgICAgICAgICAgICAgICAgY21kX2wgPSBzZWxmLmRhdGEuaW5kZXgoMCkKICAg"
    "ICAgICAgICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICAgICAgICAgICNwcmlu"
    "dCgnY21kIGRlY29kZSBlcnJvcjpcbicsIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICAgICAg"
    "ICAgICAgICAgICAgYnJlYWs7I01heSByZXF1aXJlIG1vcmUgZGF0YQogICAgICAgICAgICAgICAg"
    "Y21kID0gd2lyZV9kZWNvZGUoc2VsZi5kYXRhWzA6Y21kX2xdKQogICAgICAgICAgICAgICAgc2Vs"
    "Zi5kYXRhID0gc2VsZi5kYXRhW2NtZF9sKzE6XQogICAgICAgICAgICAgICAgcmVzcG9uc2UgPSBz"
    "ZWxmLnNlcnZlci5jb21wYXJzLnBhcnNlKGNtZCwgc2VsZikKICAgICAgICAgICAgICAgIGlmIHJl"
    "c3BvbnNlOgogICAgICAgICAgICAgICAgICAgICNRdWV1ZWQgcmF0aGVyIHRoYW4gc2VudCBpbmxp"
    "bmUsIHNvIHRoaXMgY29ubmVjdGlvbiBoYXMgYQogICAgICAgICAgICAgICAgICAgICNzaW5nbGUg"
    "b3JkZXJlZCBvdXRib3VuZCBzdHJlYW0uIFNlbmRpbmcgaGVyZSBkaXJlY3RseQogICAgICAgICAg"
    "ICAgICAgICAgICN3b3VsZCByYWNlIHRoZSB3cml0ZXIgdGhyZWFkIGFuZCBjb3VsZCBsYW5kIGlu"
    "IHRoZSBtaWRkbGUKICAgICAgICAgICAgICAgICAgICAjb2YgYSBicm9hZGNhc3QgaXQgaXMgYWxy"
    "ZWFkeSB3cml0aW5nLgogICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZChyZXNwb25zZSkKICAg"
    "ICAgICAgICAgICAgICNMb29zZSBibG9icyBzaG91bGQgbm90IGhhcHBlbiBhbnltb3JlIGhvcGVm"
    "dWxseQogICAgICAgICAgICAgICAgI1RPRE8gZml4IHVuY29tcHJlc3NlZCBkYXRhIGJsb2JzPwog"
    "ICAgICAgICAgICAgICAgI1RPRE8gc2tpcCAxIGJ5dGUgb25seSB3aGVuIGRlY29kZSBlcnJvcj8K"
    "ICAgICAgICAgICAgICAgIGlmIChsZW4oc2VsZi5kYXRhKT4yIGFuZAogICAgICAgICAgICAgICAg"
    "ICAgICAgICBzZWxmLmRhdGFbMF09PTB4NzggYW5kCiAgICAgICAgICAgICAgICAgICAgICAgIHNl"
    "bGYuZGF0YVsxXT09MHg5Yyk6CiAgICAgICAgICAgICAgICAgICAgI0xvb3NlIHVuaGFuZGxlZCBi"
    "bG9iIGFmdGVyIGNvbW1hbmQKICAgICAgICAgICAgICAgICAgICBibG9iLCBzZWxmLmRhdGEgPSBw"
    "X2dldEJsb2Ioc2VsZi5kYXRhLCBzZWxmLnJlcXVlc3QpCiAgICAgICAgICAgICAgICAgICAgI3By"
    "aW50KCdVbmhhbmRsZWQgQmxvYjonLGNtZCxmJzx7bGVuKGJsb2IpfWJ5dGUgYmxvYj4nKQogICAg"
    "ZGVmIF9yZWN2TW9yZShzZWxmKToKICAgICAgICBjaHVuayA9IHNlbGYucmVxdWVzdC5yZWN2KFJF"
    "Q1ZfQlVGX0xFTikKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAgICAgICNwZWVyIGRpc2Nv"
    "bm5lY3RlZCBkdXJpbmcgaGFuZHNoYWtlL2xvZ2luLCBzdG9wIHRoZSBidXN5LWxvb3AKICAgICAg"
    "ICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoJ2Rpc2Nvbm5lY3RlZCBkdXJpbmcgbG9n"
    "aW4nKQogICAgICAgIHNlbGYuZGF0YSArPSBjaHVuawogICAgZGVmIF9oYW5kbGUoc2VsZik6CiAg"
    "ICAgICAgI1RPRE8gbG9nIGxvZ2luIGF0dGVtcHRzPwogICAgICAgIHBlZXJfaXAgPSBzZWxmLmNs"
    "aWVudF9hZGRyZXNzWzBdCiAgICAgICAgcHJpbnQoJ0Nvbm5lY3Rpb24gYXR0ZW1wdCBmcm9tOics"
    "IHBlZXJfaXApCiAgICAgICAgTElTID0gMiAjbG9naW4gc3RhdGUgI1RPRE8gY29uc2lkZXIgbG9u"
    "ZyB0aW1lb3V0cz8KICAgICAgICB3aGlsZSBMSVM6CiAgICAgICAgICAgIHdoaWxlIGxlbihzZWxm"
    "LmRhdGEpPDQ6CiAgICAgICAgICAgICAgICBzZWxmLl9yZWN2TW9yZSgpCiAgICAgICAgICAgIHBh"
    "Y2tfbGVuID0gc3RydWN0LnVucGFjaygnPEknLHNlbGYuZGF0YVswOjRdKVswXQogICAgICAgICAg"
    "ICBpZiBwYWNrX2xlbiA8IDQgb3IgcGFja19sZW4gPiBfTUFYX0hBTkRTSEFLRToKICAgICAgICAg"
    "ICAgICAgICN1bnZhbGlkYXRlZCwgdGhpcyBpcyBhIHByZS1hdXRoZW50aWNhdGlvbiBtZW1vcnkg"
    "Ym9tYjogYW4KICAgICAgICAgICAgICAgICN1bmF1dGhlbnRpY2F0ZWQgcGVlciBhbm5vdW5jZXMg"
    "YSA0R0IgcGFja2V0IGFuZCB0aGUgbG9vcCBiZWxvdwogICAgICAgICAgICAgICAgI2J1ZmZlcnMg"
    "dW50aWwgdGhlIHByb2Nlc3MgZGllcwogICAgICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJv"
    "cihmJ2hhbmRzaGFrZSBwYWNrZXQgbGVuZ3RoIHtwYWNrX2xlbn0gb3V0IG9mIHJhbmdlJykKICAg"
    "ICAgICAgICAgd2hpbGUobGVuKHNlbGYuZGF0YSk8cGFja19sZW4pOgogICAgICAgICAgICAgICAg"
    "c2VsZi5fcmVjdk1vcmUoKQogICAgICAgICAgICAjc2xpY2UgdG8gcGFja19sZW4gKG5vdCB0byB0"
    "aGUgZW5kIG9mIHRoZSBidWZmZXIpOiBhbnl0aGluZyBwYXN0CiAgICAgICAgICAgICN0aGlzIHBh"
    "Y2tldCBiZWxvbmdzIHRvIHRoZSBuZXh0IG9uZS4gQm91bmRlZCBkZWNvbXByZXNzLCBiZWNhdXNl"
    "IGEKICAgICAgICAgICAgIzY0ayBoYW5kc2hha2Ugb2YgY29tcHJlc3NlZCB6ZXJvZXMgZXhwYW5k"
    "cyB0byBodW5kcmVkcyBvZiBNQi4KICAgICAgICAgICAgcmVzID0gX2RlY29tcHJlc3NfYm91bmRl"
    "ZChzZWxmLmRhdGFbNDpwYWNrX2xlbl0sIF9NQVhfSEFORFNIQUtFX0lORkxBVEVEKQogICAgICAg"
    "ICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFbcGFja19sZW46XQogICAgICAgICAgICBpZiBMSVMg"
    "PT0gMjoKICAgICAgICAgICAgICAgIGdhbWV2ZXJzaW9uID0gcmVzWzA6MTZdICNUT0RPIG5vdGUg"
    "Z2FtZSB2ZXJzaW9uICh1bnZlcmlmaWVkKSBwZXIgdXNlcgogICAgICAgICAgICAgICAgbGFuZ25h"
    "bWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDE2KQogICAgICAgICAgICAgICAgI1RPRE8gY29uc2lk"
    "ZXIgVFdTRSBpbmRpY2F0b3IgdG8gY3JlYXRlIHNlY3VyZSBjb25uZWN0aW9uPwogICAgICAgICAg"
    "ICAgICAgI1RPRE8gY2hlY2sgaWYgdmFuaWxsYSBzZXJ2ZXIgaWdub3JlcyBleHRyYSBkYXRhIGlu"
    "IGhhbmRzaGFrZSBwcm9jZXNzCiAgICAgICAgICAgICAgICBSSyA9IHJlc1tvZmYrODpvZmYrMTZd"
    "CiAgICAgICAgICAgICAgICBmb3IgaSBpbiByYW5nZShsZW4oUkspKToKICAgICAgICAgICAgICAg"
    "ICAgICBzZWxmLlNLW2ldXj1SS1tpXQogICAgICAgICAgICAgICAgI3dhcyBoYXJkY29kZWQgJ1RX"
    "MUNTJyB3aXRoIGEgIlNFUlZFUiBOQU1FIGNmZ1RPRE8iIG5vdGU6IHRoZQogICAgICAgICAgICAg"
    "ICAgI25hbWUgY29uZmlndXJlZCBpbiBDb25maWcuaW5pL3RoZSBHVUkgcmVhY2hlZCB0aGUgd2Vs"
    "Y29tZQogICAgICAgICAgICAgICAgI3BhY2tldCBidXQgbmV2ZXIgdGhpcyBvbmUsIHNvIHRoZSBw"
    "cmUtbG9naW4gaGFuZHNoYWtlIGFsd2F5cwogICAgICAgICAgICAgICAgI2Fubm91bmNlZCB0aGUg"
    "cGxhY2Vob2xkZXIuCiAgICAgICAgICAgICAgICBzZWxmLnNlbmRSYXcoX3NlcnZlcl9pbmZvX3Bh"
    "Y2tldChzYW5pdGl6ZVRleHQoREVGQVVMVF9USVRMRSkpKQogICAgICAgICAgICAgICAgI1RPRE8g"
    "VFcxQ1MgaW5kaWNhdG9yIGZvciBUV1NFIGNsaWVudCB0byBjcmVhdGUgc2VjdXJlIGNvbm5lY3Rp"
    "b24gb3IgcHJlLWhhc2ggcGFzc3dvcmQ/CiAgICAgICAgICAgICAgICBMSVMgPSAxIAogICAgICAg"
    "ICAgICAgICAgc2VsZi5TSyA9IGJ5dGVzKHNlbGYuU0spCiAgICAgICAgICAgIGVsaWYgTElTID09"
    "IDE6CiAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gLTEKICAgICAgICAgICAgICAgICNTdGFs"
    "bCByZXBlYXQgb2ZmZW5kZXJzIGJlZm9yZSBkb2luZyBhbnkgUEJLREYyIHdvcmsgZm9yIHRoZW0u"
    "CiAgICAgICAgICAgICAgICAjU2xlZXBpbmcgaW4gdGhpcyBoYW5kbGVyIHRocmVhZCBpcyB0aGUg"
    "cG9pbnQ6IGl0IGNvc3RzIHVzCiAgICAgICAgICAgICAgICAjbm90aGluZyBhbmQgcmF0ZS1saW1p"
    "dHMgdGhhdCBjb25uZWN0aW9uLgogICAgICAgICAgICAgICAgZGVsYXkgPSBMT0dJTl9USFJPVFRM"
    "RS5kZWxheUZvcihwZWVyX2lwKQogICAgICAgICAgICAgICAgaWYgZGVsYXk6CiAgICAgICAgICAg"
    "ICAgICAgICAgdGltZS5zbGVlcChkZWxheSkKICAgICAgICAgICAgICAgIHVzZXJuYW1lLCBvZmYg"
    "PSBwYXJzZURzdHIocmVzLCAwKQogICAgICAgICAgICAgICAgcGFzc3dvcmQsIG9mZiA9IHBhcnNl"
    "RHN0cihyZXMsIG9mZikKICAgICAgICAgICAgICAgICNUT0RPIFRXU0UgbW9kIGZvciBoaWdoZXIg"
    "bG9naW4gc2VjdXJpdHkKICAgICAgICAgICAgICAgICMtZW5jcnlwdGVkIGNvbm5lY3Rpb24gdG8g"
    "cHJldmVudCByZXBsYXkgYXR0YWNrcwogICAgICAgICAgICAgICAgIy1wcmVoYXNoIHBhc3N3b3Jk"
    "IHdpdGggc2VyaWFsPywgY2hlY2sgaWYgcmVjb3ZlcnkgcG9zc2libGUuCiAgICAgICAgICAgICAg"
    "ICBzZWxmLmd1aWQgPSBieXRlcyhyZXNbb2ZmOm9mZisxNl0pCiAgICAgICAgICAgICAgICAjcHJp"
    "bnQoJ2d1aWQgYnl0ZTonLCBzZWxmLmd1aWRbMV0pCiAgICAgICAgICAgICAgICAjc2VsZi5ndWlk"
    "ID0gYnl0ZWFycmF5KHJlc1tvZmY6b2ZmKzE2XSkKICAgICAgICAgICAgICAgICNzZWxmLmd1aWRb"
    "MV1ePTB4MTYgI0RPIE5PVCBwZXJmb3JtIHNlcnZlcnNpZGUKICAgICAgICAgICAgICAgICNzZWxm"
    "Lmd1aWQgPSBieXRlcyhzZWxmLmd1aWQpCiAgICAgICAgICAgICAgICBvZmYrPTE2CiAgICAgICAg"
    "ICAgICAgICBpc3JlZyA9IHN0cnVjdC51bnBhY2soJzxJJyxyZXNbb2ZmOm9mZis0XSlbMF0KICAg"
    "ICAgICAgICAgICAgIG9mZis9NAogICAgICAgICAgICAgICAgdmlhUmVnaXN0ZXIgPSBib29sKGlz"
    "cmVnKQogICAgICAgICAgICAgICAgaWYgaXNyZWc6CiAgICAgICAgICAgICAgICAgICAgZW1haWws"
    "IG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAgICAgICAgICBsb2NhdGlvbiwg"
    "b2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAgICAgICAgICAgIGFnZSA9IHJlc1tv"
    "ZmZdCiAgICAgICAgICAgICAgICAgICAgZ2VuZGVyID0gcmVzW29mZisxXQogICAgICAgICAgICAg"
    "ICAgICAgIG9mZis9MiAjYWdlLCBnZW5kZXIKICAgICAgICAgICAgICAgICAgICBkZXNjcmlwdGlv"
    "biwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAgICAgICAgICAgIGxvZ2luRXJy"
    "b3IgPSBzZWxmLmF0dGVtcHRSZWdpc3Rlcih1c2VybmFtZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2Nh"
    "dGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKQogICAgICAgICAgICAgICAgZWxzZToKICAg"
    "ICAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gc2VsZi5hdHRlbXB0TG9naW4odXNlcm5hbWUs"
    "IHBhc3N3b3JkKQogICAgICAgICAgICAgICAgICAgIGlmIGxvZ2luRXJyb3IgPT0gMSBhbmQgX0FV"
    "VE9fUkVHSVNURVI6CiAgICAgICAgICAgICAgICAgICAgICAgIHZpYVJlZ2lzdGVyID0gVHJ1ZQog"
    "ICAgICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gc2VsZi5hdHRlbXB0UmVnaXN0ZXIo"
    "dXNlcm5hbWUsIHBhc3N3b3JkLCAiIiwgIiIsIDEsIDAsICIiKQogICAgICAgICAgICAgICAgaWYg"
    "bG9naW5FcnJvciA9PSAwOgogICAgICAgICAgICAgICAgICAgIExPR0lOX1RIUk9UVExFLnJlY29y"
    "ZFN1Y2Nlc3MocGVlcl9pcCkKICAgICAgICAgICAgICAgICAgICAjVE9ETyBiZXR0ZXIgaGFuZGxp"
    "bmcgb2YgVElUTEUgQU5EIE1PVEQKICAgICAgICAgICAgICAgICAgICBzZWxmLnNlbmRSYXcoX3Nl"
    "cnZlcl93ZWxjb21lX3BhY2tldChieXRlcyhzZWxmLlNLKSwgREVGQVVMVF9USVRMRSwgREVGQVVM"
    "VF9NT1REKSkKICAgICAgICAgICAgICAgICAgICBMSVMgPSAwCiAgICAgICAgICAgICAgICBlbHNl"
    "OiAjZXJyb3IgYmFzZWQgb24gbG9naW5FcnJvciBudW1iZXIKICAgICAgICAgICAgICAgICAgICBj"
    "b3VudCA9IExPR0lOX1RIUk9UVExFLnJlY29yZEZhaWx1cmUocGVlcl9pcCkKICAgICAgICAgICAg"
    "ICAgICAgICBpZiBjb3VudCA9PSBfTE9HSU5fRkFJTF9MSU1JVDoKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgcHJpbnQoZidbTG9iYnldIFRocm90dGxpbmcgbG9naW5zIGZyb20ge3BlZXJfaXB9ICcK"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZicoe2NvdW50fSBmYWlsdXJlcyBpbiB7X0xP"
    "R0lOX0ZBSUxfV0lORE9XfXMpJykKICAgICAgICAgICAgICAgICAgICBlcnJtc2dzID0gX1JFR0lT"
    "VEVSX0VSUk9SUyBpZiB2aWFSZWdpc3RlciBlbHNlIF9MT0dJTl9FUlJPUlMKICAgICAgICAgICAg"
    "ICAgICAgICBzZWxmLnNlbmRSYXcoX2luaXRfZXJyb3IoZXJybXNncy5nZXQobG9naW5FcnJvciwg"
    "J0xvZ2luIGZhaWxlZCcpKSkKICAgIGRlZiBmaW5pc2goc2VsZik6CiAgICAgICAgI1N0b3AgdGhl"
    "IHdyaXRlciBmaXJzdDogaXQgaG9sZHMgdGhpcyBzb2NrZXQgYW5kIHdvdWxkIG90aGVyd2lzZSBr"
    "ZWVwCiAgICAgICAgI3dyaXRpbmcgb24gYmVoYWxmIG9mIGEgcGxheWVyIHdobyBoYXMgYWxyZWFk"
    "eSBsZWZ0IGV2ZXJ5IGNoYW5uZWwuCiAgICAgICAgc2VsZi5fc3RvcFdyaXRlcigpCiAgICAgICAg"
    "aWYgc2VsZi51c2VyOgogICAgICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFtZX0g"
    "RGlzY29ubmVjdGVkJykKICAgICAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5zZXJ2"
    "ZXIpCiAgICAgICAgI2NsZWFudXAgdXNlciBkYXRhCiAgICAgICAgI1RPRE8gY2hlY2sgaWYgdHJp"
    "Z2dlcmVkIG9uIGNyYXNoZWQgY29ubmVjdGlvbgogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAg"
    "ICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgI1RPRE8gSVAgZm9yIGVsZXZhdGVkIGF1dGhvcml0"
    "eQogICAgICAgICAgICAjJ25hbWUnOnNlbGYudXNlci5uYW1lLAogICAgICAgICAgICAnZ2FtZSc6"
    "c2VsZi51c2VyLmdhbWUuZ25hbWUgaWYgc2VsZi51c2VyLmdhbWUgZWxzZSAnJywKICAgICAgICAg"
    "ICAgJ3Rvd24nOnNlbGYudXNlci5nYW1lY2hhbm5lbC5uYW1lIGlmIHNlbGYudXNlci5nYW1lY2hh"
    "bm5lbCBlbHNlICcnLAogICAgICAgICAgICAncG9zJzpzZWxmLnVzZXIucG9zZGF0YSBpZiBzZWxm"
    "LnVzZXIucG9zZGF0YSBlbHNlICcnLAogICAgICAgICAgICAnaWQnOnNlbGYudXNlci5pZG51bSwK"
    "ICAgICAgICAgICAgJ2xvZ2luVGltZSc6anNvblRpbWUoc2VsZi51c2VyLmxvZ2luVGltZSkKICAg"
    "ICAgICB9I1RPRE8gZWxldmF0ZWQgYXV0aG9yaXR5IHZlcnNpb24KCiNkZWYgX3dyaXRlY29uZmln"
    "KCk6CiMgICAgcGFzcwojZGVmIF9yZWFkY29uZmlnKCk6CiMgICAgdHJ5OgojICAgICAgICBjZmcu"
    "cmVhZChfUEFUSF9DT05GSUcpCiMgICAgZXhjZXB0OgojICAgICAgICBwcmludCgnTm8gQ29uZmln"
    "JykKI2RlZiBjbWRfc2V0dXAoKTojYXJncyk6CiMgICAgcHJpbnQoYXJncykKICAgICNUT0RPIENy"
    "ZWF0ZSBmb2xkZXIgc3RydWN0dXJlIGFuZCBkYXRhYmFzZSBmaWxlcyBhbmQgY29uZmlnIGZpbGUK"
    "CmRlZiBjbWRfZGVmYXVsdCgpOiNhcmdzKToKICAgICNwcmludChhcmdzKQogICAgI19yZWFkY29u"
    "ZmlnKCkKICAgIHNlcnZlciA9IENvcmVTZXJ2ZXIoKQogICAgd2l0aCBzZXJ2ZXI6CiAgICAgICAg"
    "dHN0ID0gc2lnbmFsLnNpZ25hbChzaWduYWwuU0lHSU5ULCBzZXJ2ZXIuaGFuZGxlX3NpZ25hbCh0"
    "aW1lb3V0PTIpKQogICAgICAgICNwcmludCgnQXNzaWduZWQgU2lnbmFsPycsIHRzdCkKICAgICAg"
    "ICAjc2lnbmFsLnNpZ25hbChzaWduYWwuU0lHVEVSTSwgc2VydmVyLmhhbmRsZV9zaWduYWwodGlt"
    "ZW91dD0xKSkKICAgICAgICAKICAgICAgICBzZXJ2ZXIuc2VydmVfZm9yZXZlcigpCiAgICAgICAg"
    "I3NlcnZlclRyZWFkID0gdGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9c2VydmVyLnNlcnZlX2ZvcmV2"
    "ZXIpCiAgICAgICAgI3NlcnZlclRyZWFkLnN0YXJ0KCkKICAgICAgICAjc2VydmVyVHJlYWQuam9p"
    "bigpCiAgICAgICAgCgojc2NyaXB0IGxhdW5jaGVkLCBjaGVjayBhcmd1bWVudHMgYW5kIGNvbmZp"
    "Zy4gc2V0dXAgdmFyaW91cyBvYmplY3RzCmlmIF9fbmFtZV9fID09ICdfX21haW5fXyc6CiAgICBw"
    "cmludCgnSW5pdGlhbGl6aW5nIFNlcnZlcicpIyBubyBhcmd1bWVudHMgaW4gdGhpcyB2ZXJzaW9u"
    "CiAgICAjXyA9IGdldHRleHQuZ2V0dGV4dCAjVE9ETyBwcm9wZXJseSB1dGlsaXplCiAgICAjcGFy"
    "c2VyID0gYXJncGFyc2UuQXJndW1lbnRQYXJzZXIoCiAgICAjICAgIHByb2c9J1RXMUNTJywKICAg"
    "ICMgICAgZGVzY3JpcHRpb249XygnVHdvIFdvcmxkcyAxIENvbW11bml0eSBTZXJ2ZXInKSkKICAg"
    "ICNzdWJwYXJzZXJzID0gcGFyc2VyLmFkZF9zdWJwYXJzZXJzKGhlbHA9J3N1Yi1jb21tYW5kIGhl"
    "bHAnKQogICAgI3NldHVwX3BhcnNlciA9IHN1YnBhcnNlcnMuYWRkX3BhcnNlcignc2V0dXAnLCBo"
    "ZWxwPV8oJ0NyZWF0ZSBpbml0aWFsIGNvbmZpZ3VyYXRpb24nKSkKICAgICNzZXR1cF9wYXJzZXIu"
    "YWRkX2FyZ3VtZW50KCdyb290X3BhdGgnLCBoZWxwPV8oJ1Jvb3QgcGF0aCBmb3IgZGF0YWJhc2Ug"
    "YW5kIHBsYXllcmRhdGEnIykpCiAgICAjc2V0dXBfcGFyc2VyLmFkZF9hcmd1bWVudCgnLWZsJywn"
    "LS1maWxlbG9nJywKICAgICMgICAgICAgICAgICAgICAgICAgICAgICAgIGNob2ljZXM9WydERUJV"
    "RycsICdJTkZPJywgJ1dBUk5JTkcnLCAnRVJST1InLCAnQ1JJVElDQUwnXSwKICAgICMgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgIGhlbHA9XygnTG9nIGxldmVsIHN0b3JlZCB0byBmaWxlJykpCiAg"
    "ICAjc2V0dXBfcGFyc2VyLmFkZF9hcmd1bWVudCgnLWNsJywnLS1jb25zb2xlbG9nJywKICAgICMg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgIGNob2ljZXM9WydERUJVRycsICdJTkZPJywgJ1dBUk5J"
    "TkcnLCAnRVJST1InLCAnQ1JJVElDQUwnXSwKICAgICMgICAgICAgICAgICAgICAgICAgICAgICAg"
    "IGhlbHA9XygnTG9nIGxldmVsIHByaW50ZWQgdG8gY29uc29sZScpKQoKICAgICNwYXJzZXIuYWRk"
    "X2FyZ3VtZW50KCctbCcsICctLWxvZycsCiAgICAjICAgZGVzdD0nbG9nTGV2ZWwnLCBjaG9pY2Vz"
    "PVsnREVCVUcnLCAnSU5GTycsICdXQVJOSU5HJywgJ0VSUk9SJywgJ0NSSVRJQ0FMJ10sCiAgICAj"
    "ICAgaGVscD0nU2V0IHRoZSBsb2dnaW5nIGxldmVsJykKICAgICNpZiBhcmdzLmxvZ0xldmVsOgog"
    "ICAgIyAgIGxvZ2dpbmcuYmFzaWNDb25maWcobGV2ZWw9Z2V0YXR0cihsb2dnaW5nLCBhcmdzLmxv"
    "Z0xldmVsKSkKICAgICNhcmdzID0gcGFyc2VyLnBhcnNlX2FyZ3MoKQogICAgI1RPRE8gc2V0dXAK"
    "ICAgIGNtZF9kZWZhdWx0KCkjYXJncykK"
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
