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
    'settings.motd_hint': {
        'ru': 'Поддерживает цвет игры: <0xAARRGGBB>, шрифт <F2>, паузу <break=сек>',
        'en': 'Supports the game\'s markup: colour <0xAARRGGBB>, font <F2>, pause <break=sec>'},
    'settings.net_header': {'ru': 'Подключение игроков', 'en': 'Player connectivity'},
    'settings.rewrite_host': {
        'ru': 'Выдавать присоединяющимся реальный адрес хозяина комнаты (нужно для игры через интернет)',
        'en': 'Advertise the room host\'s real address to joiners (required for play over the internet)'},
    'settings.public_host': {
        'ru': 'Публичный адрес этого сервера (пусто — определять автоматически):',
        'en': 'Public address of this server (blank = detect automatically):'},
    'settings.hero_id_hex': {
        'ru': 'ID героев в шестнадцатеричном виде (выключить, если чужой герой стоит на месте)',
        'en': 'Hero ids in hexadecimal (turn off if other players\' heroes never move)'},
    'settings.debug_cmds': {
        'ru': 'Записывать в лог все команды клиента (диагностика)',
        'en': 'Log every command received from clients (diagnostics)'},
    'settings.note': {
        'ru': 'Всё, кроме порта, применяется сразу — перезапускать сервер не нужно.\n'
              'Изменение порта вступает в силу после перезапуска на вкладке «Сервер».',
        'en': 'Everything except the port takes effect immediately - no server restart needed.\n'
              'A port change applies after a restart on the "Server" tab.'},
    'settings.load_current': {'ru': 'Загрузить текущие', 'en': 'Load current'},
    'settings.save': {'ru': 'Сохранить', 'en': 'Save'},
    'settings.bad_pos_hz': {'ru': 'Частота синхронизации позиций должна быть числом.',
                             'en': 'The position sync rate must be a number.'},
    'settings.bad_idle_timeout': {'ru': 'Таймаут простоя должен быть целым числом секунд.',
                                   'en': 'The idle timeout must be a whole number of seconds.'},
    'settings.bad_public_host': {
        'ru': 'Публичный адрес должен быть IP-адресом или именем хоста — без пробелов, «;» и «/».',
        'en': 'The public address must be an IP address or hostname - no spaces, ";" or "/".'},
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
        motd_hint = ttk.Label(f, foreground=self.MUTED)
        motd_hint.grid(row=3, column=1, sticky='w', padx=10)
        self._tr('settings.motd_hint', lambda t: motd_hint.configure(text=t))

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

        # These four lived only in Config.ini, which meant the settings that
        # decide whether two players can reach each other at all were the ones
        # nobody could see. RewriteGameHost in particular is what makes co-op
        # work across the internet.
        net_lbl = ttk.Label(f, style='Header.TLabel')
        net_lbl.grid(row=10, column=0, columnspan=2, sticky='w', padx=10, pady=(20, 6))
        self._tr('settings.net_header', lambda t: net_lbl.configure(text=t))

        self.set_rewrite_host = tk.BooleanVar()
        cb4 = ttk.Checkbutton(f, variable=self.set_rewrite_host)
        cb4.grid(row=11, column=1, sticky='w', padx=10)
        self._tr('settings.rewrite_host', lambda t: cb4.configure(text=t))

        pub_lbl = ttk.Label(f)
        pub_lbl.grid(row=12, column=0, sticky='w', **pad)
        self._tr('settings.public_host', lambda t: pub_lbl.configure(text=t))
        self.set_public_host = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_public_host, width=24).grid(
            row=12, column=1, sticky='w', padx=10, pady=6)

        self.set_hero_hex = tk.BooleanVar()
        cb5 = ttk.Checkbutton(f, variable=self.set_hero_hex)
        cb5.grid(row=13, column=1, sticky='w', padx=10)
        self._tr('settings.hero_id_hex', lambda t: cb5.configure(text=t))

        self.set_debug_cmds = tk.BooleanVar()
        cb6 = ttk.Checkbutton(f, variable=self.set_debug_cmds)
        cb6.grid(row=14, column=1, sticky='w', padx=10)
        self._tr('settings.debug_cmds', lambda t: cb6.configure(text=t))

        btns = ttk.Frame(f)
        btns.grid(row=15, column=1, sticky='w', padx=10, pady=16)
        load_btn = ttk.Button(btns, command=self._load_settings)
        load_btn.pack(side='left')
        self._tr('settings.load_current', lambda t: load_btn.configure(text=t))
        save_btn = ttk.Button(btns, style='Accent.TButton', command=self._save_settings)
        save_btn.pack(side='left', padx=8)
        self._tr('settings.save', lambda t: save_btn.configure(text=t))

        note = ttk.Label(f, foreground=self.MUTED, wraplength=560, justify='left')
        note.grid(row=16, column=1, sticky='w', padx=10)
        self._tr('settings.note', lambda t: note.configure(text=t))

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
        self.set_rewrite_host.set(sec.getboolean('RewriteGameHost', fallback=mod._REWRITE_GAME_HOST))
        self.set_public_host.set(sec.get('PublicHostAddress', fallback=mod._PUBLIC_HOST_ADDRESS))
        self.set_hero_hex.set(sec.getboolean('HeroIdHex', fallback=mod._HERO_ID_HEX))
        self.set_debug_cmds.set(sec.getboolean('DebugCommands', fallback=mod._DEBUG_LOG_COMMANDS))

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
        # This value is substituted straight into the host's DirectPlay URL,
        # whose fields are ';'-separated - so a stray separator here does not
        # produce a wrong address, it produces a malformed URL and a room
        # nobody can join, with nothing obviously wrong on screen.
        public_host = self.set_public_host.get().strip()
        if public_host and (any(c.isspace() for c in public_host)
                            or ';' in public_host or '/' in public_host):
            messagebox.showerror(T('server.module_load_error_title'), T('settings.bad_public_host'))
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
        sec['RewriteGameHost'] = str(self.set_rewrite_host.get())
        sec['PublicHostAddress'] = public_host
        sec['HeroIdHex'] = str(self.set_hero_hex.get())
        sec['DebugCommands'] = str(self.set_debug_cmds.get())
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
    "QmxvYihkYXRhLCBjb24pOgogICAgI1JlYWRzIHRoZSByZXN0IG9mIGEgemxpYiBzdHJlYW0gd2hv"
    "c2UgZmlyc3QgYnl0ZXMgYXJlIGFscmVhZHkgaW4gYGRhdGFgLgogICAgI0JvdW5kZWQgaW4gc2l6"
    "ZSAqYW5kKiBpbiB0aW1lLiBUaGUgc2l6ZSBjYXAgd2FzIGFscmVhZHkgaGVyZTsgdGhlIGRlYWRs"
    "aW5lCiAgICAjd2FzIG5vdCwgc28gYSBjbGllbnQgdGhhdCBhbm5vdW5jZWQgYSBibG9iIGFuZCB0"
    "aGVuIHdlbnQgcXVpZXQgLSBhIHdlZGdlZAogICAgI2dhbWUsIGEgbGluayB0aGF0IGRyb3BwZWQg"
    "d2l0aG91dCBhIHJlc2V0IC0gcGFya2VkIHRoaXMgY29ubmVjdGlvbidzCiAgICAjaGFuZGxlciB0"
    "aHJlYWQgaGVyZSBmb3JldmVyLiBUaGF0IHRocmVhZCBpcyB0aGUgb25seSBvbmUgdGhhdCBldmVy"
    "IHJ1bnMKICAgICN0aGUgZGlzY29ubmVjdCBjbGVhbnVwLCBzbyB0aGUgcGxheWVyJ3MgYWNjb3Vu"
    "dCBzdGF5ZWQgY2xhaW1lZCAoJ0FjY291bnQKICAgICNhbHJlYWR5IGxvZ2dlZCBpbicgdW50aWwg"
    "YSByZXN0YXJ0KSBhbmQgYW55IHJvb20gdGhleSBob3N0ZWQgc3RheWVkCiAgICAjYWR2ZXJ0aXNl"
    "ZCB3aXRoIG5vdGhpbmcgYmVoaW5kIGl0LiBfUmVhZEJsb2Igd2FzIGhhcmRlbmVkIGFnYWluc3Qg"
    "ZXhhY3RseQogICAgI3RoaXM7IHRoaXMgcGF0aCB3YXMgbWlzc2VkLgogICAgI3NlbGVjdCgpIHJh"
    "dGhlciB0aGFuIHNldHRpbWVvdXQoKTogYSB0aW1lb3V0IGJlbG9uZ3MgdG8gdGhlICpzb2NrZXQq"
    "LCBub3QKICAgICN0byB0aGUgY2FsbCwgc28gYXJtaW5nIG9uZSBoZXJlIHdvdWxkIGFsc28gYXJt"
    "IHRoZSB3cml0ZXIgdGhyZWFkJ3MKICAgICNjb25jdXJyZW50IHNlbmRhbGwoKSBhbmQgbGV0IGl0"
    "IGRpZSBwYXJ0IHdheSB0aHJvdWdoIGEgcGFja2V0LiBTZWUgdGhlCiAgICAjbG9uZyBub3RlIGlu"
    "IF9SZWFkQmxvYi4KICAgIGRjbXAgPSB6bGliLmRlY29tcHJlc3NvYmooKQogICAgZGNtcC5kZWNv"
    "bXByZXNzKGRhdGEpCiAgICBjZGF0cyA9IFtkYXRhXQogICAgdG90YWwgPSBsZW4oZGF0YSkKICAg"
    "IGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIF9CTE9CX1RJTUVPVVQKICAgIHdoaWxlIG5v"
    "dCBkY21wLmVvZjoKICAgICAgICByZW1haW5pbmcgPSBkZWFkbGluZSAtIHRpbWUubW9ub3Rvbmlj"
    "KCkKICAgICAgICBpZiByZW1haW5pbmcgPD0gMDoKICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xF"
    "cnJvcihmJ2xvb3NlIGJsb2Igbm90IGNvbXBsZXRlZCB3aXRoaW4ge19CTE9CX1RJTUVPVVR9cyAn"
    "CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZicoe3RvdGFsfSBieXRlcyByZWNlaXZl"
    "ZCknKQogICAgICAgIHJlYWR5LCBfLCBfID0gc2VsZWN0LnNlbGVjdChbY29uXSwgW10sIFtdLCBy"
    "ZW1haW5pbmcpCiAgICAgICAgaWYgbm90IHJlYWR5OgogICAgICAgICAgICBjb250aW51ZSAjZGVh"
    "ZGxpbmUgaXMgcmUtY2hlY2tlZCBhdCB0aGUgdG9wIG9mIHRoZSBsb29wCiAgICAgICAgY2RhdCA9"
    "IGNvbi5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBpZiBub3QgY2RhdDoKICAgICAgICAgICAg"
    "I3BlZXIgdmFuaXNoZWQgbWlkLWJsb2I6IHJlY3YoKSBrZWVwcyByZXR1cm5pbmcgYicnIGluc3Rh"
    "bnRseSwgc28KICAgICAgICAgICAgI3dpdGhvdXQgdGhpcyB0aGUgbG9vcCBzcGlucyBhdCAxMDAl"
    "IENQVSBmb3JldmVyIChzYW1lIGRlZmVjdCB0aGF0CiAgICAgICAgICAgICN3YXMgYWxyZWFkeSBm"
    "aXhlZCBpbiBDb25uZWN0aW9uSGFuZGxlci5fcmVjdk1vcmUpCiAgICAgICAgICAgIHJhaXNlIENv"
    "bm5lY3Rpb25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGJsb2IgcmVhZCcpCiAgICAg"
    "ICAgdG90YWwgKz0gbGVuKGNkYXQpCiAgICAgICAgaWYgdG90YWwgPiBfTUFYX0JMT0I6CiAgICAg"
    "ICAgICAgIHJhaXNlIENvbm5lY3Rpb25SZXNldEVycm9yKGYnYmxvYiBleGNlZWRzIHtfTUFYX0JM"
    "T0J9IGJ5dGVzJykKICAgICAgICBjZGF0cy5hcHBlbmQoY2RhdCkKICAgICAgICBkY21wLmRlY29t"
    "cHJlc3MoY2RhdCkKICAgIGlmIGxlbihkY21wLnVudXNlZF9kYXRhKToKICAgICAgICBjZGF0c1st"
    "MV09Y2RhdHNbLTFdWzotbGVuKGRjbXAudW51c2VkX2RhdGEpXQogICAgZmNibCA9IGInJy5qb2lu"
    "KGNkYXRzKQogICAgcmV0dXJuIGZjYmwsIGRjbXAudW51c2VkX2RhdGEKI0RpcmVjdFBsYXkgYWRk"
    "cmVzc2VzIGFyZSBVUkxzIG9mIHRoZSBzaGFwZQojICB4LWRpcmVjdHBsYXk6L3Byb3ZpZGVyPSU3"
    "Qi4uJTdEO2hvc3RuYW1lPTE5Mi4xNjguMC4xMDtwb3J0PTIzMDIKI3dpdGggdW5vcmRlcmVkLCBz"
    "ZW1pY29sb24tc2VwYXJhdGVkIGtleT12YWx1ZSBwYWlycy4gT25seSB0aGUgaG9zdCBjb21wb25l"
    "bnQKI2lzIHRvdWNoZWQ7IGV2ZXJ5dGhpbmcgZWxzZSAocHJvdmlkZXIgR1VJRCwgcG9ydCwgYXBw"
    "bGljYXRpb24gaW5zdGFuY2UpIGlzCiN0aGUgaG9zdCdzIGJ1c2luZXNzIGFuZCBpcyBwYXNzZWQg"
    "dGhyb3VnaCB1bnRvdWNoZWQuCl9SRV9EUF9IT1NUTkFNRSA9IHJlLmNvbXBpbGUocicoP2kpKGhv"
    "c3RuYW1lPSkoW147XSopJykKX1JFX0RQX0FMVCA9IHJlLmNvbXBpbGUocicoP2kpOz9hbHQ9W147"
    "XSonKQpkZWYgX2lzR2xvYmFsQWRkcmVzcyhhZGRyKToKICAgICMiQ2FuIGEgcGxheWVyIG9uIGFu"
    "b3RoZXIgbmV0d29yayBvcGVuIGEgc29ja2V0IHRvIHRoaXM/IiBMb29wYmFjaywKICAgICNsaW5r"
    "LWxvY2FsIChpbmNsdWRpbmcgSVB2NiBmZTgwOjopIGFuZCBSRkMxOTE4IGFsbCBmYWlsIHRoYXQg"
    "dGVzdC4KICAgIHRyeToKICAgICAgICByZXR1cm4gaXBhZGRyZXNzLmlwX2FkZHJlc3MoYWRkciku"
    "aXNfZ2xvYmFsCiAgICBleGNlcHQgVmFsdWVFcnJvcjoKICAgICAgICByZXR1cm4gRmFsc2UKX3B1"
    "YmxpY0lwQ2FjaGUgPSBbTm9uZSwgMC4wXQpfcHVibGljSXBMb2NrID0gdGhyZWFkaW5nLkxvY2so"
    "KQpkZWYgX3NlcnZlclB1YmxpY0FkZHJlc3MoKToKICAgICNUaGUgcHVibGljIGFkZHJlc3Mgb2Yg"
    "dGhlIG1hY2hpbmUgdGhpcyBzZXJ2ZXIgcnVucyBvbi4gVXNlZCBmb3IgYSBob3N0CiAgICAjd2hv"
    "c2Ugb2JzZXJ2ZWQgYWRkcmVzcyBpcyBwcml2YXRlLCB3aGljaCBoYXBwZW5zIHdoZW5ldmVyIHRo"
    "ZSBob3N0IHNpdHMKICAgICNvbiB0aGUgc2FtZSBMQU4vcm91dGVyIGFzIHRoZSBsb2JieSAtIGlu"
    "Y2x1ZGluZyB0aGUgaGFpcnBpbi1OQVQgY2FzZQogICAgI3doZXJlIGEgbG9jYWwgcGxheWVyIHJl"
    "YWNoZXMgdGhlIHNlcnZlciB0aHJvdWdoIHRoZSByb3V0ZXIncyBwdWJsaWMKICAgICNhZGRyZXNz"
    "IGFuZCB0aGUgcm91dGVyIHJld3JpdGVzIHRoZSBzb3VyY2UgdG8gaXRzIG93biBMQU4gYWRkcmVz"
    "cy4gSW4gYWxsCiAgICAjb2YgdGhvc2UgdGhlIGhvc3QgcmVhY2hlcyB0aGUgaW50ZXJuZXQgdGhy"
    "b3VnaCB0aGUgc2FtZSByb3V0ZXIgYXMgd2UgZG8sCiAgICAjc28gb3VyIHB1YmxpYyBhZGRyZXNz"
    "IGlzIHRoZWlycy4KICAgIHdpdGggX3B1YmxpY0lwTG9jazoKICAgICAgICAoaXAsIGZldGNoZWQp"
    "ID0gX3B1YmxpY0lwQ2FjaGUKICAgICAgICBpZiBpcCBhbmQgKHRpbWUubW9ub3RvbmljKCkgLSBm"
    "ZXRjaGVkKSA8IDM2MDA6CiAgICAgICAgICAgIHJldHVybiBpcAogICAgZ290ID0gTm9uZQogICAg"
    "Zm9yICh1cmwsIGhkcnMpIGluICgoJ2h0dHBzOi8vMmlwLnJ1JywgeydVc2VyLUFnZW50JzogJ2N1"
    "cmwvOC4wJ30pLAogICAgICAgICAgICAgICAgICAgICAgICAoJ2h0dHBzOi8vYXBpLmlwaWZ5Lm9y"
    "ZycsIHt9KSk6CiAgICAgICAgdHJ5OgogICAgICAgICAgICByZXEgPSB1cmxsaWIucmVxdWVzdC5S"
    "ZXF1ZXN0KHVybCwgaGVhZGVycz1oZHJzKQogICAgICAgICAgICB3aXRoIHVybGxpYi5yZXF1ZXN0"
    "LnVybG9wZW4ocmVxLCB0aW1lb3V0PTQpIGFzIHI6CiAgICAgICAgICAgICAgICBjYW5kID0gci5y"
    "ZWFkKCkuZGVjb2RlKCdhc2NpaScsIGVycm9ycz0naWdub3JlJykuc3RyaXAoKQogICAgICAgICAg"
    "ICBpZiBfaXNHbG9iYWxBZGRyZXNzKGNhbmQpOgogICAgICAgICAgICAgICAgZ290ID0gY2FuZAog"
    "ICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAg"
    "ICBjb250aW51ZSAjb2ZmbGluZSBvciB0aGUgc2VydmljZSBpcyBibG9ja2VkOyBub3QgZmF0YWwK"
    "ICAgIHdpdGggX3B1YmxpY0lwTG9jazoKICAgICAgICBpZiBnb3Q6CiAgICAgICAgICAgIF9wdWJs"
    "aWNJcENhY2hlWzpdID0gW2dvdCwgdGltZS5tb25vdG9uaWMoKV0KICAgIHJldHVybiBnb3QKZGVm"
    "IHBpY2tHYW1lSG9zdEFkZHJlc3MocGVlcl9hZGRyKToKICAgICMtPiAoYWRkcmVzc19vcl9Ob25l"
    "LCBub3RlX2Zvcl90aGVfbG9nKQogICAgI1RoZSBob3N0J3Mgb3duIGFkZHJlc3Mgd2lucyB3aGVu"
    "ZXZlciBpdCBpcyBvbmUgdGhlIHJlc3Qgb2YgdGhlIGludGVybmV0CiAgICAjY2FuIHJlYWNoLiBQ"
    "dWJsaWNIb3N0QWRkcmVzcyBpcyBOT1QgYSBibGFua2V0IG92ZXJyaWRlOiBpdCBkZXNjcmliZXMg"
    "dGhlCiAgICAjbmV0d29yayAqdGhpcyBzZXJ2ZXIqIHNpdHMgb24sIHNvIGFwcGx5aW5nIGl0IHRv"
    "IGEgaG9zdCB3aG8gY29ubmVjdGVkCiAgICAjZnJvbSBzb21ld2hlcmUgZWxzZSBlbnRpcmVseSB3"
    "b3VsZCBzZW5kIGV2ZXJ5IGpvaW5lciB0byB0aGUgd3JvbmcKICAgICNtYWNoaW5lIC0gaXQgb25s"
    "eSBhbnN3ZXJzIHRoZSBxdWVzdGlvbiAid2hhdCBpcyB0aGUgcHVibGljIGFkZHJlc3Mgb2YgYQog"
    "ICAgI2hvc3QgdGhhdCBhcHBlYXJzIHRvIGJlIG9uIG91ciBvd24gTEFOIi4KICAgIGlmIF9pc0ds"
    "b2JhbEFkZHJlc3MocGVlcl9hZGRyKToKICAgICAgICByZXR1cm4gcGVlcl9hZGRyLCBmJ2hvc3Qg"
    "Y29ubmVjdGVkIGZyb20ge3BlZXJfYWRkcn0nCiAgICBpZiBfUFVCTElDX0hPU1RfQUREUkVTUzoK"
    "ICAgICAgICByZXR1cm4gX1BVQkxJQ19IT1NUX0FERFJFU1MsIChmJ2hvc3QgY29ubmVjdGVkIGZy"
    "b20ge3BlZXJfYWRkcn0gKHByaXZhdGUgLSBzYW1lICcKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICBmJ25ldHdvcmsgYXMgdGhpcyBzZXJ2ZXIpLCB1c2luZyBjb25maWd1cmVk"
    "ICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBmJ1B1YmxpY0hvc3RBZGRy"
    "ZXNzIHtfUFVCTElDX0hPU1RfQUREUkVTU30nKQogICAgcHViID0gX3NlcnZlclB1YmxpY0FkZHJl"
    "c3MoKQogICAgaWYgcHViOgogICAgICAgIHJldHVybiBwdWIsIChmJ2hvc3QgY29ubmVjdGVkIGZy"
    "b20ge3BlZXJfYWRkcn0gKHByaXZhdGUgLSBzYW1lIG5ldHdvcmsgYXMgJwogICAgICAgICAgICAg"
    "ICAgICAgICBmJ3RoaXMgc2VydmVyKSwgdXNpbmcgb3VyIHB1YmxpYyBhZGRyZXNzIHtwdWJ9JykK"
    "ICAgIHJldHVybiBOb25lLCAoZidob3N0IGNvbm5lY3RlZCBmcm9tIHtwZWVyX2FkZHJ9IChwcml2"
    "YXRlKSBhbmQgdGhpcyBzZXJ2ZXIgJwogICAgICAgICAgICAgICAgICBmJ2NvdWxkIG5vdCBkZXRl"
    "cm1pbmUgaXRzIG93biBwdWJsaWMgYWRkcmVzcycpCmRlZiByZXdyaXRlR2FtZUhvc3QodXJsLCBw"
    "ZWVyX2FkZHIpOgogICAgIy0+ICh1cmwsIG5vdGVfZm9yX3RoZV9sb2cpCiAgICBpZiBub3QgX1JF"
    "V1JJVEVfR0FNRV9IT1NUIG9yIG5vdCB1cmwgb3Igbm90IHBlZXJfYWRkcjoKICAgICAgICByZXR1"
    "cm4gdXJsLCAncmV3cml0ZSBkaXNhYmxlZCcKICAgIChhZGRyLCBub3RlKSA9IHBpY2tHYW1lSG9z"
    "dEFkZHJlc3MocGVlcl9hZGRyKQogICAgaWYgbm90IGFkZHI6CiAgICAgICAgcmV0dXJuIHVybCwg"
    "bm90ZSArICcgLSB1cmwgcGFzc2VkIHRocm91Z2ggdW5jaGFuZ2VkJwogICAgaWYgX1JFX0RQX0hP"
    "U1ROQU1FLnNlYXJjaCh1cmwpOgogICAgICAgIG9sZCA9IF9SRV9EUF9IT1NUTkFNRS5zZWFyY2go"
    "dXJsKS5ncm91cCgyKQogICAgICAgIHVybCA9IF9SRV9EUF9IT1NUTkFNRS5zdWIobGFtYmRhIG06"
    "IG0uZ3JvdXAoMSkgKyBhZGRyLCB1cmwsIGNvdW50PTEpCiAgICAgICAgbm90ZSArPSBmJzsgaG9z"
    "dG5hbWUge29sZCFyfSAtPiB7YWRkciFyfScKICAgIGVsc2U6CiAgICAgICAgI05vIGhvc3RuYW1l"
    "IGF0IGFsbDogdGhlIGpvaW5lciB3b3VsZCBoYXZlIG5vdGhpbmcgdG8gY29ubmVjdCB0by4KICAg"
    "ICAgICB1cmwgPSB1cmwgKyAoJycgaWYgdXJsLmVuZHN3aXRoKCc7JykgZWxzZSAnOycpICsgJ2hv"
    "c3RuYW1lPScgKyBhZGRyCiAgICAgICAgbm90ZSArPSBmJzsgbm8gaG9zdG5hbWUgaW4gdXJsLCBh"
    "cHBlbmRlZCB7YWRkciFyfScKICAgIGlmIF9TVFJJUF9BTFRfQUREUkVTU0VTIGFuZCBfUkVfRFBf"
    "QUxULnNlYXJjaCh1cmwpOgogICAgICAgIHVybCA9IF9SRV9EUF9BTFQuc3ViKCcnLCB1cmwpCiAg"
    "ICAgICAgbm90ZSArPSAnOyBkcm9wcGVkIGFsdD0gY2FuZGlkYXRlIGFkZHJlc3NlcycKICAgIHJl"
    "dHVybiB1cmwsIG5vdGUKZGVmIHByZXR0eV9ndWlkKGd1aWQpOgogICAgKGEsYixjLGQpID0gc3Ry"
    "dWN0LnVucGFjaygiPElISDhzIiwgZ3VpZCkKICAgIGRhID0gJycKICAgIGRiID0gJycKICAgIGZv"
    "ciBpIGluIGRbMDoyXToKICAgICAgICBkYSs9J3s6MDJ4fScuZm9ybWF0KGkpCiAgICBmb3IgaSBp"
    "biBkWzI6XToKICAgICAgICBkYis9J3s6MDJ4fScuZm9ybWF0KGkpCiAgICByZXR1cm4gJ3s6MDh4"
    "fS17OjA0eH0tezowNHh9LXt9LXt9Jy5mb3JtYXQoYSxiLGMsZGEsZGIpCmRlZiBfZW0obXNnKToK"
    "ICAgICNFdmVyeSB0ZXh0IGNvbW1hbmQgbGVhdmluZyB0aGlzIHNlcnZlciBpcyBmcmFtZWQgaGVy"
    "ZSwgd2hpY2ggbWFrZXMgaXQgdGhlCiAgICAjb25lIHBsYWNlIHRoYXQgY2FuIHNlZSBhbiBvdmVy"
    "LWxvbmcgbGluZSBubyBtYXR0ZXIgd2hpY2ggaGFuZGxlciBidWlsdCBpdC4KICAgICNUaGUgZmll"
    "bGRzIHRoYXQgZmVlZCB0aGVzZSBsaW5lcyBhcmUgY2FwcGVkIGluZGl2aWR1YWxseSwgc28gdGhp"
    "cyBzaG91bGQKICAgICNuZXZlciBmaXJlOyBpdCBleGlzdHMgYmVjYXVzZSAibmV2ZXIiIHdhcyBh"
    "bHNvIHRydWUgb2YgdGhlIGZpZWxkcyB0aGF0CiAgICAjdHVybmVkIG91dCB0byBiZSB1bmJvdW5k"
    "ZWQsIGFuZCBiZWNhdXNlIHRoZSBmYWlsdXJlIGl0IGd1YXJkcyBhZ2FpbnN0CiAgICAjc3VyZmFj"
    "ZXMgYXMgYW5vdGhlciBwbGF5ZXIncyBnYW1lIGxvY2tpbmcgdXAgc29saWQsIHdpdGggbm90aGlu"
    "ZyBpbiB0aGUgbG9nCiAgICAjcG9pbnRpbmcgYmFjayBoZXJlLiBMb2dnZWQgcmF0aGVyIHRoYW4g"
    "dHJ1bmNhdGVkOiBoYWxmIGEgY29tbWFuZCBpcyBhCiAgICAjcHJvdG9jb2wgZGVzeW5jLCB3aGlj"
    "aCBpcyBub3QgYW4gaW1wcm92ZW1lbnQgb24gYSBsb25nIG9uZS4KICAgIGlmIGxlbihtc2cpID4g"
    "X01BWF9XSVJFX0xJTkU6CiAgICAgICAgcHJpbnQoZidbTG9iYnldIFdBUk5JTkc6IHtsZW4obXNn"
    "KX0tYnl0ZSBsaW5lIGV4Y2VlZHMgdGhlIHtfTUFYX1dJUkVfTElORX0gJwogICAgICAgICAgICAg"
    "IGYnYnl0ZSBsaW1pdCBhbmQgbWF5IGRlc3RhYmlsaXNlIHRoZSBjbGllbnQ6IHttc2dbOjEyMF0h"
    "cn0uLi4nKQogICAgcmV0dXJuIHdpcmVfZW5jb2RlKG1zZykrX04KZGVmIF9kZWNvbXByZXNzX2Jv"
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
    "ZGVmIHNhbml0aXplVGV4dCh0ZXh0LCBtYXhsZW49Tm9uZSk6CiAgICAjc3RyaXAgY2hhcmFjdGVy"
    "cyB0aGF0IHdvdWxkIGJyZWFrIHRoZSBxdW90ZWQtc3RyaW5nIGJhc2VkIGxvYmJ5IHByb3RvY29s"
    "CiAgICAjb3IgYWxsb3cgYSBjbGllbnQgdG8gZm9yZ2UgYWRkaXRpb25hbCBwcm90b2NvbCBmaWVs"
    "ZHMgKHByb3RvY29sIGluamVjdGlvbikKICAgICNtYXhsZW4gY2FwcyB0aGUgZmllbGQncyBjb250"
    "cmlidXRpb24gdG8gdGhlIGxpbmUgaXQgZW5kcyB1cCBpbi4gTGVuZ3RoIGlzCiAgICAjYSBzYWZl"
    "dHkgcHJvcGVydHkgaGVyZSwgbm90IGNvc21ldGljczogc2VlIF9NQVhfV0lSRV9MSU5FLgogICAg"
    "aWYgdGV4dCBpcyBOb25lOgogICAgICAgIHJldHVybiAnJwogICAgdGV4dCA9IHRleHQucmVwbGFj"
    "ZSgnIicsICInIikucmVwbGFjZSgnXDAnLCAnJykucmVwbGFjZSgnXHInLCAnJykucmVwbGFjZSgn"
    "XG4nLCAnICcpCiAgICBpZiBtYXhsZW4gaXMgbm90IE5vbmUgYW5kIGxlbih0ZXh0KSA+IG1heGxl"
    "bjoKICAgICAgICB0ZXh0ID0gdGV4dFs6bWF4bGVuXQogICAgcmV0dXJuIHRleHQKZGVmIGpzb25U"
    "aW1lKGR0KToKICAgIGlmIG5vdCBkdC51dGNvZmZzZXQoKToKICAgICAgICB0emluZm8gPSBkYXRl"
    "dGltZS5kYXRldGltZS5ub3coZGF0ZXRpbWUudGltZXpvbmUudXRjKS5hc3RpbWV6b25lKCkudHpp"
    "bmZvCiAgICAgICAgZHQgPSBkdC5yZXBsYWNlKHR6aW5mbz10emluZm8pCiAgICBkdCA9IGR0LmFz"
    "dGltZXpvbmUoZGF0ZXRpbWUudGltZXpvbmUudXRjKS5yZXBsYWNlKHR6aW5mbz1Ob25lKQogICAg"
    "cmV0dXJuIGR0Lmlzb2Zvcm1hdCgpICsgIloiCiAgICAjc2hvdWxkIHJldHVybiAyMDEyLTA0LTIz"
    "VDE4OjI1OjQzLjUxMVogdXRjIHRpbWUgZm9yIGphdmFzY3JpcHQgcGFyc2luZwoKIyMgTUFJTiBT"
    "RVJWRVIgQ09ERQoKUkVDVl9CVUZfTEVOID0gMioqMTIKCl9WRVJTSU9OID0gJzAuMi4wJwpwcmlu"
    "dChmJ1NlcnZlciB2ZXJpc2lvbiB7X1ZFUlNJT059JykKX0RFQlVHX0FMTE9XX0FOWV9MT0dJTiA9"
    "IEZhbHNlICNkb2VzIG5vdCB2ZXJpZnkgbG9naW5zLCBmb3IgZGVidWcgcmVhc29ucwpfVFdfTE9C"
    "QllfUE9SVCA9IDE3MTcxCl9BVVRPX1JFR0lTVEVSID0gVHJ1ZQojVXBwZXIgYm91bmQgZm9yIGEg"
    "c2luZ2xlIGxlbmd0aC1wcmVmaXhlZCBibG9iIGZyb20gYSBjbGllbnQgKHBsYXllcmRhdGEsCiNo"
    "ZXJvZGF0YSwgZ2FtZS1jb21tYW5kIHBheWxvYWQpLiBHZW5lcm91cyBjb21wYXJlZCB0byBhIHJl"
    "YWwgc2F2ZSwgYnV0IGZpbml0ZToKI3dpdGhvdXQgaXQgYSBjbGllbnQgY291bGQgYW5ub3VuY2Ug"
    "YW4gYXJiaXRyYXJ5IGxlbmd0aCBhbmQgbWFrZSB0aGUgc2VydmVyCiNidWZmZXIgdW50aWwgaXQg"
    "cmFuIG91dCBvZiBtZW1vcnkuCl9NQVhfQkxPQiA9IDE2ICogMTAyNCAqIDEwMjQKI0hhbmRzaGFr"
    "ZS9sb2dpbiBwYWNrZXRzIGFyZSBhIGZldyBodW5kcmVkIGJ5dGVzIGluIHByYWN0aWNlLiBUaGVz"
    "ZSBib3VuZHMKI2FwcGx5ICpiZWZvcmUqIGF1dGhlbnRpY2F0aW9uLCB3aGVyZSBhbnlvbmUgd2hv"
    "IGNhbiByZWFjaCB0aGUgcG9ydCBjYW4gc2VuZAojd2hhdGV2ZXIgdGhleSBsaWtlLCBzbyB0aGV5"
    "IGFyZSBkZWxpYmVyYXRlbHkgdGlnaHQuCl9NQVhfSEFORFNIQUtFID0gNjQgKiAxMDI0Cl9NQVhf"
    "SEFORFNIQUtFX0lORkxBVEVEID0gMTAyNCAqIDEwMjQKCiMtLS0gc3luY2hyb25pc2F0aW9uIHR1"
    "bmluZyAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiNI"
    "b3cgb2Z0ZW4gdGhlIGFjY3VtdWxhdGVkIGhlcm8gcG9zaXRpb25zIGluIGEgdG93biBhcmUgcHVz"
    "aGVkIHRvIGV2ZXJ5b25lIGluCiNpdC4gVGhpcyB1c2VkIHRvIGJlIHBpbm5lZCB0byB0aGUgMXMg"
    "c29ja2V0c2VydmVyIHBvbGwgaW50ZXJ2YWwsIHdoaWNoIGlzIHdoYXQKI21hZGUgb3RoZXIgcGxh"
    "eWVycycgbWFwIG1hcmtlcnMganVtcCBhIGZ1bGwgc2Vjb25kIGF0IGEgdGltZS4gRWFjaCB0aWNr"
    "IHNlbmRzCiNvbmUgcGFja2V0IHBlciB0b3duIGFuZCBvbmx5IGlmIHNvbWVib2R5IGFjdHVhbGx5"
    "IG1vdmVkLCBzbyBldmVuIGF0IHRoaXMgcmF0ZQojaXQncyBhIGhhbmRmdWwgb2Ygc21hbGwgcGFj"
    "a2V0cy9zZWMgZm9yIGEgY28tb3Atc2l6ZWQgZ3JvdXAgLSBuZWdsaWdpYmxlCiNiYW5kd2lkdGgg"
    "ZWl0aGVyIG9uIExBTiBvciBvdmVyIGEgaG9tZSBpbnRlcm5ldCBjb25uZWN0aW9uIC0gd2hpbGUg"
    "Z2V0dGluZwojbm90aWNlYWJseSBjbG9zZXIgdG8gc21vb3RoIG1vdGlvbiB0aGFuIHRoZSBvbGQg"
    "MUh6IGJhc2VsaW5lLgpfUE9TX1VQREFURV9IWiA9IDEwLjAKX1BPU19VUERBVEVfSFpfTUFYID0g"
    "MjAuMAojRHJvcCBhIGNvbm5lY3Rpb24gdGhhdCBoYXMgbm90IHNlbnQgYSBzaW5nbGUgYnl0ZSBp"
    "biB0aGlzIGxvbmcuIEEgcGxheWVyIHdob3NlCiNsaW5rIGRpZXMgd2l0aG91dCBhIGNsZWFuIFRD"
    "UCBjbG9zZSBvdGhlcndpc2Uga2VlcHMgdGhlaXIgdXNlcm5hbWUgY2xhaW1lZAojZm9yZXZlciwg"
    "YW5kIHRoZWlyIG5leHQgbG9naW4gYXR0ZW1wdCBpcyByZWplY3RlZCB3aXRoICdBY2NvdW50IGFs"
    "cmVhZHkgbG9nZ2VkCiNpbicgdW50aWwgdGhlIHNlcnZlciBpcyByZXN0YXJ0ZWQuIDAgZGlzYWJs"
    "ZXMuCl9JRExFX1RJTUVPVVQgPSAzMDAKI0Jsb2NraW5nIHJlY3YoKSB0aW1lb3V0IGluIHRoZSBy"
    "ZWFkIGxvb3AuIE9ubHkgZ292ZXJucyBob3cgcXVpY2tseSBhIHRocmVhZAojbm90aWNlcyBzZXJ2"
    "ZXIgc2h1dGRvd24gYW5kIHRoZSBpZGxlIGRlYWRsaW5lOyBvdXRib3VuZCBsYXRlbmN5IG5vIGxv"
    "bmdlcgojZGVwZW5kcyBvbiBpdCBub3cgdGhhdCBlYWNoIGNvbm5lY3Rpb24gaGFzIGl0cyBvd24g"
    "d3JpdGVyIHRocmVhZC4KX1JFQURfVElNRU9VVCA9IDEuMAojSG93IGxvbmcgYSBjbGllbnQgZ2V0"
    "cyB0byBmaW5pc2ggZGVsaXZlcmluZyBhIGJsb2IgaXQgaGFzIGFscmVhZHkgYW5ub3VuY2VkCiN0"
    "aGUgbGVuZ3RoIG9mLiBHZW5lcm91cyBmb3IgYSBsYXJnZSBzYXZlIG92ZXIgYSBzbG93IGxpbmss"
    "IGJ1dCBmaW5pdGUgLSBzZWUKI19SZWFkQmxvYi4KX0JMT0JfVElNRU9VVCA9IDYwLjAKI1RoZSBs"
    "b2JieSBvbmx5IGJyb2tlcnMgdGhlIGNvLW9wIHNlc3Npb247IHRoZSBzZXNzaW9uIGl0c2VsZiBp"
    "cyBhIGRpcmVjdAojRGlyZWN0UGxheSBjb25uZWN0aW9uIGZyb20gdGhlIGpvaW5pbmcgcGxheWVy"
    "IHRvIHRoZSBob3N0LCBhdCB0aGUgYWRkcmVzcyB0aGUKI2hvc3QgcHV0cyBpbiB0aGUgeC1kaXJl"
    "Y3RwbGF5IFVSTCBvZiBpdHMgL2NyZWF0ZWdhbWUuIFRoZSBob3N0J3Mgb3duIGNsaWVudAojZmls"
    "bHMgdGhhdCBpbiBmcm9tIGl0cyBsb2NhbCBhZGFwdGVyLCBzbyBiZWhpbmQgYSByb3V0ZXIgaXQg"
    "YWR2ZXJ0aXNlcwojc29tZXRoaW5nIGxpa2UgMTkyLjE2OC4wLjEwIC0gdW5yZWFjaGFibGUgZm9y"
    "IGFueW9uZSBub3Qgb24gdGhhdCBMQU4sIGFuZCB0aGUKI2pvaW5lciBzaXRzIG9uICJjb25uZWN0"
    "aW5nIiB1bnRpbCBpdCBnaXZlcyB1cC4gRXZlcnl0aGluZyB0aGF0IGdvZXMgdGhyb3VnaAojdGhl"
    "IGxvYmJ5ICh0b3duLCBjaGF0LCBzZWVpbmcgZWFjaCBvdGhlciBtb3ZlKSBrZWVwcyB3b3JraW5n"
    "LCB3aGljaCBpcyB3aGF0CiNtYWtlcyB0aGlzIGxvb2sgbGlrZSBhIHJvb20tc3BlY2lmaWMgYnVn"
    "LgojVGhlIHNlcnZlciBhbHJlYWR5IGtub3dzIGFuIGFkZHJlc3MgZm9yIHRoZSBob3N0IHRoYXQg"
    "ZXZlcnkgb3RoZXIgY2xpZW50IGNhbgojcmVhY2g6IHRoZSBzb3VyY2UgYWRkcmVzcyBvZiB0aGUg"
    "aG9zdCdzIG93biBjb25uZWN0aW9uIHRvIHVzLiBTdWJzdGl0dXRpbmcgaXQKI2lzIHdoYXQgbWFr"
    "ZXMgY3Jvc3MtaW50ZXJuZXQgY28tb3Agd29yayBhdCBhbGwuCiNUdXJuIG9mZiAoQ29uZmlnLmlu"
    "aTogUmV3cml0ZUdhbWVIb3N0ID0gRmFsc2UpIGlmIGV2ZXJ5IHBsYXllciBpcyBvbiB0aGUgc2Ft"
    "ZQojTEFOIGFzIHRoZSBob3N0IGJ1dCB0aGUgbG9iYnkgaXMgbm90IC0gdGhlbiB0aGUgaG9zdCdz"
    "IG93biBMQU4gYWRkcmVzcyBpcyB0aGUKI2NvcnJlY3Qgb25lIGFuZCBvdXJzIGlzIG5vdC4KX1JF"
    "V1JJVEVfR0FNRV9IT1NUID0gVHJ1ZQojRXhwbGljaXQgcHVibGljIGFkZHJlc3Mgb2YgdGhlIG1h"
    "Y2hpbmUgdGhhdCBob3N0cyByb29tcywgZm9yIHRoZSBjYXNlIHRoZQojc2VydmVyIGNhbm5vdCB3"
    "b3JrIGl0IG91dCAoc2VlIF9wdWJsaWNBZGRyZXNzKS4gU2V0IGl0IGluIENvbmZpZy5pbmkgYXMK"
    "I1B1YmxpY0hvc3RBZGRyZXNzIGlmIGF1dG8tZGV0ZWN0aW9uIHBpY2tzIHRoZSB3cm9uZyBvbmUu"
    "Cl9QVUJMSUNfSE9TVF9BRERSRVNTID0gJycKI1RoZSBnYW1lIGFwcGVuZHMgYSBwcm9wcmlldGFy"
    "eSAnYWx0PScgZmllbGQgdG8gdGhlIERpcmVjdFBsYXkgVVJMIGhvbGRpbmcKI2V2ZXJ5IGFkZHJl"
    "c3Mgb2YgZXZlcnkgYWRhcHRlciB0aGUgaG9zdCBoYXM6IG9ic2VydmVkIGluIHRoZSB3aWxkIGl0"
    "IGNhcnJpZWQKI2EgVGVyZWRvIDIwMDE6MDo6LzMyIGFkZHJlc3MsIGFuIGZlODA6OiBsaW5rLWxv"
    "Y2FsIG9uZSBhbmQgdGhlIGhvc3QncyBMQU4KI0lQdjQgLSBub25lIG9mIHRoZW0gcmVhY2hhYmxl"
    "IGZyb20gYW5vdGhlciBuZXR3b3JrLiBBIGpvaW5lciB0aGF0IHdvcmtzCiN0aHJvdWdoIHRoYXQg"
    "Y2FuZGlkYXRlIGxpc3Qgd2FpdHMgb3V0IGEgY29ubmVjdGlvbiB0aW1lb3V0IG9uIGVhY2gsIHdo"
    "aWNoCiNsb29rcyBleGFjdGx5IGxpa2UgImNvbm5lY3RpbmcgZm9yZXZlciIuIERyb3BwaW5nIHRo"
    "ZSBmaWVsZCBsZWF2ZXMgdGhlIHNpbmdsZQojYWRkcmVzcyB0aGlzIHNlcnZlciBrbm93cyB0byBi"
    "ZSByZWFjaGFibGUuCl9TVFJJUF9BTFRfQUREUkVTU0VTID0gVHJ1ZQojTG9nIGV2ZXJ5IGNvbW1h"
    "bmQgcmVjZWl2ZWQgZnJvbSBjbGllbnRzLCB3aXRoIGl0cyByYXcgdGV4dC4gVmVyYm9zZSwgYnV0"
    "IHRoaXMKI3Byb3RvY29sIGlzIG9ubHkgcGFydGlhbGx5IGRvY3VtZW50ZWQgYW5kIGl0IGlzIHRo"
    "ZSBvbmx5IHdheSB0byBzZWUgd2hhdCB0aGUKI2NsaWVudCBhY3R1YWxseSBhc2tzIGZvciB3aGVu"
    "IGEgZmVhdHVyZSBkb2VzIG5vdGhpbmcuCl9ERUJVR19MT0dfQ09NTUFORFMgPSBUcnVlCiMvdXBk"
    "aGVyb3BvcyBhbmQgL25vcCBhcnJpdmUgfjEwIHRpbWVzIGEgc2Vjb25kIHBlciBwbGF5ZXIgYW5k"
    "IHNheSBub3RoaW5nCiN1c2VmdWwuIExvZ2dpbmcgdGhlbSBjb3N0IHR3byBmb3JtYXR0ZWQgbGlu"
    "ZXMsIGEgcXVldWUgcHV0LCBhIEdVSSBpbnNlcnQgYW5kCiNhIGRpc2sgd3JpdGUgKmluc2lkZSB0"
    "aGUgY29tbWFuZCBoYW5kbGVyKiwgb24gdGhlIG9uZSBwYXRoIHRoYXQgaGFzIHRvIHN0YXkKI3F1"
    "aWNrIC0gc2VsZi1pbmZsaWN0ZWQgbGF0ZW5jeSBhbmQgaml0dGVyIG9uIGV4YWN0bHkgdGhlIHRy"
    "YWZmaWMgYmVpbmcKI2RlYnVnZ2VkLCBwbHVzIGEgbG9nIHNvIG5vaXN5IHRoYXQgdGhlIGludGVy"
    "ZXN0aW5nIGxpbmVzIHNjcm9sbCBhd2F5LiBTZXQKI0RlYnVnQ29tbWFuZHNWZXJib3NlID0gVHJ1"
    "ZSBpbiBDb25maWcuaW5pIHRvIHNlZSB0aGVtIGFueXdheS4KX0RFQlVHX0xPR19WRVJCT1NFID0g"
    "RmFsc2UKX1FVSUVUX0NPTU1BTkRTID0gZnJvemVuc2V0KCgnL3VwZGhlcm9wb3MnLCAnL25vcCcp"
    "KQojQ29uc2VydmF0aXZlIGNhcCBvbiBhIHNpbmdsZSBnZW5lcmF0ZWQgY29tbWFuZCBsaW5lLiBO"
    "b3RoaW5nIHRoZSByZXRhaWwKI2NsaWVudCBzZW5kcyBjb21lcyBjbG9zZSB0byB0aGlzLCBzbyBp"
    "dCBpcyB3ZWxsIGluc2lkZSB3aGF0ZXZlciB0aGUgY2xpZW50CiNpdHNlbGYgaXMgYnVpbHQgdG8g"
    "aGFuZGxlLgpfTUFYX1dJUkVfTElORSA9IDkwMAojUGVyLWZpZWxkIGNhcHMsIHNvIG5vIGNvbWJp"
    "bmF0aW9uIG9mIHN0b3JlZCBvciB0eXBlZCB0ZXh0IGNhbiBhZGQgdXAgdG8gYSBsaW5lCiNvdmVy"
    "IHRoYXQgbGltaXQuIEV2ZXJ5IG9uZSBvZiB0aGVzZSBmaWVsZHMgaXMgcGxheWVyLWNvbnRyb2xs"
    "ZWQgYW5kIHRyYXZlbHMgdG8KIypvdGhlciogcGxheWVycycgY2xpZW50czoKIyAtIGNoYXQgdGV4"
    "dCBhbmQgdGhlIHJvb20gbmFtZSBhcmUgdHlwZWQgc3RyYWlnaHQgaW47CiMgLSBlbWFpbC9sb2Nh"
    "dGlvbi9kZXNjcmlwdGlvbiBjb21lIGZyb20gL3VwZGF0ZSBhbmQgYXJlIHJlcGxheWVkIGJ5IC93"
    "aG9pcyB0bwojICAgd2hvZXZlciBhc2tzLCBsb25nIGFmdGVyIHRoZSBmYWN0IGFuZCB0byBzb21l"
    "Ym9keSB3aG8gbmV2ZXIgdHlwZWQgdGhlbS4KI05vbmUgb2YgdGhlbSB3YXMgYm91bmRlZCwgc28g"
    "b25lIGxvbmcgdmFsdWUgd2FzIGVub3VnaCB0byBoYW5kIGFub3RoZXIgcGxheWVyJ3MKI2NsaWVu"
    "dCBhIGxpbmUgbG9uZ2VyIHRoYW4gaXQgaXMgYnVpbHQgdG8gcGFyc2UgLSB3aGljaCBpcyBub3Qg"
    "YSBjb3NtZXRpYwojcHJvYmxlbSBpbiBhIDIwMDggMzItYml0IGJpbmFyeSwgaXQgaXMgYSBoZWFw"
    "IG92ZXJ3cml0ZSBhbmQgYSBoYXJkIGxvY2stdXAgb24KI2EgbWFjaGluZSBvdGhlciB0aGFuIHRo"
    "ZSBvbmUgdGhhdCBjYXVzZWQgaXQuCl9NQVhfQ0hBVF9URVhUID0gMjU1Cl9NQVhfV0hPSVNfRklF"
    "TEQgPSA2NCAgICAjZW1haWwsIGxvY2F0aW9uCl9NQVhfREVTQ1JJUFRJT04gPSAyNTUKX01BWF9H"
    "QU1FTkFNRSA9IDY0Cl9NQVhfQ0hBVE5BTUUgPSA0OAojUGxheWVyLWNyZWF0ZWQgY2hhdCBjaGFu"
    "bmVscyBhcmUgcGVyIHRvd24gYW5kIGFyZSBuZXZlciBnYXJiYWdlIGNvbGxlY3RlZCwgc28KI3Ro"
    "ZSBjb3VudCBpcyBib3VuZGVkIHJhdGhlciB0aGFuIGxlZnQgdG8gd2hvZXZlciBjbGlja3MgZmFz"
    "dGVzdC4gV2VsbCBhYm92ZSB0aGUKI3R3byB0aGUgZ2FtZSBzaGlwcyB3aXRoLgpfTUFYX0NIQVRf"
    "Q0hBTk5FTFMgPSAxNgojU2VydmVyLWNvbnRyb2xsZWQgdGV4dCB0aGF0IHJlYWNoZXMgdGhlIGNs"
    "aWVudDogdGhlIHRpdGxlIGFuZCB0aGUgbWVzc2FnZSBvZgojdGhlIGRheSBhcmUgdHlwZWQgYnkg"
    "YW4gYWRtaW4gaW50byB0aGUgR1VJIHdpdGggbm8gbGVuZ3RoIGxpbWl0IGF0IGFsbCwgYW5kCiNi"
    "b3RoIGFyZSBoYW5kZWQgdG8gdGhlIGNsaWVudCBhdCBsb2dpbiwgYmVmb3JlIHRoZSBwbGF5ZXIg"
    "Y2FuIGRvIGFueXRoaW5nCiNhYm91dCBpdC4gVHJ1bmNhdGUgcmF0aGVyIHRoYW4gdHJ1c3QuCl9N"
    "QVhfVElUTEUgPSAxMjgKX01BWF9NT1REID0gMTAyNAojSGVybyBpZHMgb24gdGhlIHdpcmU6IGhl"
    "eCBvciBkZWNpbWFsLgojRXZlcnl0aGluZyBwb3NpdGlvbmFsIGluIHRoaXMgcHJvdG9jb2wgaXMg"
    "aGV4IC0gdGhlIGNsaWVudCdzIG93bgojL3VwZGhlcm9wb3MgY2FycmllcyBjb29yZGluYXRlcyBh"
    "cyAiMzhBNCMyQjE3IiAtIGFuZCB1cGRhdGVQb3MoKSBoYXMgYWx3YXlzCiNwcmVmaXhlZCB0aGUg"
    "aGVybyBpZCBpbiBoZXggdG8gbWF0Y2guIEJ1dCAkZ2FtZWNoYW5uZWx1c2VyLCB0aGUgbWVzc2Fn"
    "ZSB0aGF0CiNmaXJzdCB0ZWxscyBhIGNsaWVudCB3aGljaCBpZCBiZWxvbmdzIHRvIHdoaWNoIHBs"
    "YXllciwgc2VudCB0aGUgc2FtZSBpZCBpbgojZGVjaW1hbC4gQSBjbGllbnQgdGhhdCByZWFkcyBi"
    "b3RoIGZpZWxkcyB3aXRoIG9uZSByYWRpeCB0aGVyZWZvcmUgY2Fubm90CiNtYXRjaCBhIHBvc2l0"
    "aW9uIHVwZGF0ZSB0byB0aGUgcGxheWVyIGl0IGJlbG9uZ3MgdG8sIGFuZCB0aGF0IGhlcm8gc3Rv"
    "cHMKI21vdmluZyBvbiBldmVyeW9uZSBlbHNlJ3MgbWFwIHdoaWxlIHdhbGtpbmcgbm9ybWFsbHkg"
    "b24gdGhlaXIgb3duLgojTGVmdCBhcyBhIHN3aXRjaCBiZWNhdXNlIHdoaWNoIHJhZGl4IHRoZSBy"
    "ZXRhaWwgY2xpZW50IHdhbnRzIGlzIG5vdAojZG9jdW1lbnRlZDogaWYgaGV4IHR1cm5zIG91dCB0"
    "byBiZSB0aGUgd3JvbmcgZ3Vlc3MsIHNldCBIZXJvSWRIZXggPSBGYWxzZSBpbgojQ29uZmlnLmlu"
    "aSBhbmQgYm90aCBtZXNzYWdlcyBmYWxsIGJhY2sgdG8gZGVjaW1hbCAtIHN0aWxsIGNvbnNpc3Rl"
    "bnQsIHdoaWNoCiNpcyB0aGUgcGFydCB0aGF0IGFjdHVhbGx5IG1hdHRlcnMuCl9IRVJPX0lEX0hF"
    "WCA9IFRydWUKI09wdGlvbmFsIHNlcnZlci0+Y2xpZW50ICcvbm9wJyBoZWFydGJlYXQgZXZlcnkg"
    "M3MuIE1haW5seSB1c2VmdWwgdG8gc3RvcCBob21lCiNyb3V0ZXJzIGRyb3BwaW5nIHRoZSBOQVQg"
    "bWFwcGluZyBvZiBhbiBpZGxlIGNvLW9wIHNlc3Npb24uIE9mZiBieSBkZWZhdWx0OiB0aGUKI3Jl"
    "YWwgY2xpZW50J3MgcmVhY3Rpb24gdG8gYW4gdW5zb2xpY2l0ZWQgL25vcCBoYXMgbm90IGJlZW4g"
    "dmVyaWZpZWQuCl9TRU5EX05PUFMgPSBGYWxzZQoKCkRFRkFVTFRfVElUTEUgPSAnQ29tbXVuaXR5"
    "IE11bHRpcGxheWVyIFNlcnZlcicKREVGQVVMVF9NT1REID0gZic8MHhGRjAwMDBGRj48RjI+Q29t"
    "bXVuaXR5IE11bHRpcGxheWVyIFNlcnZlciBWZXJzaW9uIHtfVkVSU0lPTn08YnJlYWs9MTAuMD5c"
    "clxuJwoKI1Jvb3QgbmV4dCB0byB0aGlzIHNjcmlwdCByYXRoZXIgdGhhbiB0aGUgcHJvY2Vzcycg"
    "Y3VycmVudCB3b3JraW5nIGRpcmVjdG9yeSwKI3NvIHRoZSBkYXRhYmFzZS9jb25maWcvcGxheWVy"
    "ZGF0YSBhbHdheXMgbGl2ZSBpbiB0aGUgc2FtZSBwbGFjZSB3aGV0aGVyIHRoZQojc2VydmVyIGlz"
    "IGRvdWJsZS1jbGlja2VkLCBsYXVuY2hlZCBmcm9tIGEgdGVybWluYWwgZWxzZXdoZXJlLCBvciBp"
    "bXBvcnRlZCBieQojYSBHVUkgd3JhcHBlciAoZS5nLiBUVzEgQ29udHJvbCBDZW50ZXIpLgojQWxs"
    "b3dzIGFuIGVtYmVkZGluZyBob3N0IChlLmcuIGEgcG9ydGFibGUgYWxsLWluLW9uZSBsYXVuY2hl"
    "ciB0aGF0IGV4ZWMoKXMKI3RoaXMgZmlsZSdzIHNvdXJjZSBmcm9tIG1lbW9yeSwgd2hlcmUgX19m"
    "aWxlX18gaXMgbWVhbmluZ2xlc3MpIHRvIHJlZGlyZWN0CiN3aGVyZSB0aGUgZGF0YWJhc2UvY29u"
    "ZmlnL3BsYXllcmRhdGEgbGl2ZSBieSBwcmUtc2V0dGluZyB0aGlzIG5hbWUgaW4gdGhlCiNtb2R1"
    "bGUncyBnbG9iYWxzIGJlZm9yZSB0aGUgbW9kdWxlIGJvZHkgcnVucy4gU3RhbmRhbG9uZSBleGVj"
    "dXRpb24gKHRoZQojbm9ybWFsIGBweXRob24gVFcxQ1MucHlgKSBpcyB1bmFmZmVjdGVkOiBmYWxs"
    "cyBiYWNrIHRvIG5leHQgdG8gdGhpcyBzY3JpcHQuCmlmICdfRVhURVJOQUxfREFUQV9ESVInIGlu"
    "IGdsb2JhbHMoKSBhbmQgZ2xvYmFscygpWydfRVhURVJOQUxfREFUQV9ESVInXToKICAgIF9QQVRI"
    "X1JPT1QgPSBnbG9iYWxzKClbJ19FWFRFUk5BTF9EQVRBX0RJUiddCmVsc2U6CiAgICBfUEFUSF9S"
    "T09UID0gb3MucGF0aC5kaXJuYW1lKG9zLnBhdGguYWJzcGF0aChfX2ZpbGVfXykpCl9QQVRIX0RB"
    "VEFCQVNFID0gb3MucGF0aC5qb2luKF9QQVRIX1JPT1QsJ1NlcnZlckRhdGEuZGInKQpfUEFUSF9D"
    "T05GSUcgPSBvcy5wYXRoLmpvaW4oX1BBVEhfUk9PVCwnQ29uZmlnLmluaScpCl9QQVRIX1BMQVlF"
    "UkRBVEEgPSBvcy5wYXRoLmpvaW4oX1BBVEhfUk9PVCwnUGxheWVyRGF0YScpCgpkZWYgX2VzY2Fw"
    "ZU1PVEQobW90ZCk6CiAgICAjY29uZmlncGFyc2VyIHZhbHVlcyBjYW4ndCBzYWZlbHkgaG9sZCBy"
    "YXcgQ1IvTEYsIHN0b3JlIGFzIFxyXG4gZXNjYXBlcwogICAgcmV0dXJuIG1vdGQuZW5jb2RlKCd1"
    "bmljb2RlX2VzY2FwZScpLmRlY29kZSgnYXNjaWknKQpkZWYgX3VuZXNjYXBlTU9URChtb3RkKToK"
    "ICAgICNfZXNjYXBlTU9URCBhbHdheXMgd3JpdGVzIHB1cmUgYXNjaWksIGJ1dCBhIGhhbmQtZWRp"
    "dGVkIENvbmZpZy5pbmkgbWF5IGhvbGQKICAgICNyYXcgOC1iaXQgdGV4dDsgdG9sZXJhdGUgaXQg"
    "aW5zdGVhZCBvZiByZWZ1c2luZyB0byBzdGFydCB0aGUgc2VydmVyCiAgICByZXR1cm4gbW90ZC5l"
    "bmNvZGUoX1dJUkVfRU5DLCAncmVwbGFjZScpLmRlY29kZSgndW5pY29kZV9lc2NhcGUnKQpfQ09O"
    "RklHX0RFRkFVTFRTID0gewogICAgJ1NlcnZlck5hbWUnOiBERUZBVUxUX1RJVExFLAogICAgJ01P"
    "VEQnOiBfZXNjYXBlTU9URChERUZBVUxUX01PVEQpLAogICAgJ1BvcnQnOiBzdHIoX1RXX0xPQkJZ"
    "X1BPUlQpLAogICAgJ0F1dG9SZWdpc3Rlcic6IHN0cihfQVVUT19SRUdJU1RFUiksCiAgICAnQWxs"
    "b3dBbnlMb2dpbic6IHN0cihfREVCVUdfQUxMT1dfQU5ZX0xPR0lOKSwKICAgICdQb3NpdGlvblVw"
    "ZGF0ZUh6Jzogc3RyKF9QT1NfVVBEQVRFX0haKSwKICAgICdJZGxlVGltZW91dCc6IHN0cihfSURM"
    "RV9USU1FT1VUKSwKICAgICdLZWVwYWxpdmUnOiBzdHIoX1NFTkRfTk9QUyksCiAgICAnUmV3cml0"
    "ZUdhbWVIb3N0Jzogc3RyKF9SRVdSSVRFX0dBTUVfSE9TVCksCiAgICAnUHVibGljSG9zdEFkZHJl"
    "c3MnOiBfUFVCTElDX0hPU1RfQUREUkVTUywKICAgICdTdHJpcEFsdEFkZHJlc3Nlcyc6IHN0cihf"
    "U1RSSVBfQUxUX0FERFJFU1NFUyksCiAgICAnSGVyb0lkSGV4Jzogc3RyKF9IRVJPX0lEX0hFWCks"
    "CiAgICAnRGVidWdDb21tYW5kcyc6IHN0cihfREVCVUdfTE9HX0NPTU1BTkRTKSwKICAgICdEZWJ1"
    "Z0NvbW1hbmRzVmVyYm9zZSc6IHN0cihfREVCVUdfTE9HX1ZFUkJPU0UpLAp9CmRlZiBsb2FkQ29u"
    "ZmlnKCk6CiAgICBjZmcgPSBjb25maWdwYXJzZXIuQ29uZmlnUGFyc2VyKCkKICAgIGNmZ1snc2Vy"
    "dmVyJ10gPSBkaWN0KF9DT05GSUdfREVGQVVMVFMpCiAgICBpZiBvcy5wYXRoLmV4aXN0cyhfUEFU"
    "SF9DT05GSUcpOgogICAgICAgIGNmZy5yZWFkKF9QQVRIX0NPTkZJRykKICAgIGVsc2U6CiAgICAg"
    "ICAgc2F2ZUNvbmZpZyhjZmcpCiAgICByZXR1cm4gY2ZnCmRlZiBzYXZlQ29uZmlnKGNmZyk6CiAg"
    "ICB3aXRoIG9wZW4oX1BBVEhfQ09ORklHLCAndycsIGVuY29kaW5nPSd1dGYtOCcpIGFzIGY6CiAg"
    "ICAgICAgY2ZnLndyaXRlKGYpCmRlZiBhcHBseUNvbmZpZyhjZmcpOgogICAgI0FwcGxpZXMgY29u"
    "ZmlnIHZhbHVlcyB0byB0aGUgbGl2ZSBtb2R1bGUgZ2xvYmFscy4gU2VydmVyTmFtZS9NT1RELwog"
    "ICAgI0F1dG9SZWdpc3RlciB0YWtlIGVmZmVjdCBpbW1lZGlhdGVseSAocmVhZCBmcmVzaCBwZXIg"
    "bG9naW4gYXR0ZW1wdCk7CiAgICAjUG9ydCBvbmx5IHRha2VzIGVmZmVjdCBmb3Igc2VydmVycyBz"
    "dGFydGVkIGFmdGVyIHRoaXMgY2FsbC4KICAgIGdsb2JhbCBERUZBVUxUX1RJVExFLCBERUZBVUxU"
    "X01PVEQsIF9UV19MT0JCWV9QT1JULCBfQVVUT19SRUdJU1RFUiwgX0RFQlVHX0FMTE9XX0FOWV9M"
    "T0dJTgogICAgZ2xvYmFsIF9QT1NfVVBEQVRFX0haLCBfSURMRV9USU1FT1VULCBfU0VORF9OT1BT"
    "CiAgICBnbG9iYWwgX1JFV1JJVEVfR0FNRV9IT1NULCBfREVCVUdfTE9HX0NPTU1BTkRTLCBfREVC"
    "VUdfTE9HX1ZFUkJPU0UKICAgIGdsb2JhbCBfUFVCTElDX0hPU1RfQUREUkVTUywgX1NUUklQX0FM"
    "VF9BRERSRVNTRVMsIF9IRVJPX0lEX0hFWAogICAgc2VjID0gY2ZnWydzZXJ2ZXInXQogICAgREVG"
    "QVVMVF9USVRMRSA9IHNlYy5nZXQoJ1NlcnZlck5hbWUnLCBmYWxsYmFjaz1ERUZBVUxUX1RJVExF"
    "KQogICAgREVGQVVMVF9NT1REID0gX3VuZXNjYXBlTU9URChzZWMuZ2V0KCdNT1REJywgZmFsbGJh"
    "Y2s9X2VzY2FwZU1PVEQoREVGQVVMVF9NT1REKSkpCiAgICBfVFdfTE9CQllfUE9SVCA9IHNlYy5n"
    "ZXRpbnQoJ1BvcnQnLCBmYWxsYmFjaz1fVFdfTE9CQllfUE9SVCkKICAgIF9BVVRPX1JFR0lTVEVS"
    "ID0gc2VjLmdldGJvb2xlYW4oJ0F1dG9SZWdpc3RlcicsIGZhbGxiYWNrPV9BVVRPX1JFR0lTVEVS"
    "KQogICAgX0RFQlVHX0FMTE9XX0FOWV9MT0dJTiA9IHNlYy5nZXRib29sZWFuKCdBbGxvd0FueUxv"
    "Z2luJywgZmFsbGJhY2s9X0RFQlVHX0FMTE9XX0FOWV9MT0dJTikKICAgICNDbGFtcGVkIHJhdGhl"
    "ciB0aGFuIHRydXN0ZWQ6IHRoZXNlIGNvbWUgZnJvbSBhIGhhbmQtZWRpdGFibGUgaW5pLCBhbmQg"
    "YQogICAgI3N0cmF5IDAgb3IgMTAwMDAgaGVyZSB3b3VsZCBlaXRoZXIgc3RvcCBwb3NpdGlvbiB1"
    "cGRhdGVzIGVudGlyZWx5IG9yIHNwaW4KICAgICN0aGUgdXBkYXRlIHRocmVhZCBmbGF0IG91dC4K"
    "ICAgIGh6ID0gc2VjLmdldGZsb2F0KCdQb3NpdGlvblVwZGF0ZUh6JywgZmFsbGJhY2s9X1BPU19V"
    "UERBVEVfSFopCiAgICBfUE9TX1VQREFURV9IWiA9IG1pbihtYXgoaHosIDAuNSksIF9QT1NfVVBE"
    "QVRFX0haX01BWCkKICAgIF9JRExFX1RJTUVPVVQgPSBtYXgoMCwgc2VjLmdldGludCgnSWRsZVRp"
    "bWVvdXQnLCBmYWxsYmFjaz1fSURMRV9USU1FT1VUKSkKICAgIF9TRU5EX05PUFMgPSBzZWMuZ2V0"
    "Ym9vbGVhbignS2VlcGFsaXZlJywgZmFsbGJhY2s9X1NFTkRfTk9QUykKICAgIF9SRVdSSVRFX0dB"
    "TUVfSE9TVCA9IHNlYy5nZXRib29sZWFuKCdSZXdyaXRlR2FtZUhvc3QnLCBmYWxsYmFjaz1fUkVX"
    "UklURV9HQU1FX0hPU1QpCiAgICBfUFVCTElDX0hPU1RfQUREUkVTUyA9IHNlYy5nZXQoJ1B1Ymxp"
    "Y0hvc3RBZGRyZXNzJywgZmFsbGJhY2s9X1BVQkxJQ19IT1NUX0FERFJFU1MpLnN0cmlwKCkKICAg"
    "IF9TVFJJUF9BTFRfQUREUkVTU0VTID0gc2VjLmdldGJvb2xlYW4oJ1N0cmlwQWx0QWRkcmVzc2Vz"
    "JywgZmFsbGJhY2s9X1NUUklQX0FMVF9BRERSRVNTRVMpCiAgICBfSEVST19JRF9IRVggPSBzZWMu"
    "Z2V0Ym9vbGVhbignSGVyb0lkSGV4JywgZmFsbGJhY2s9X0hFUk9fSURfSEVYKQogICAgX0RFQlVH"
    "X0xPR19DT01NQU5EUyA9IHNlYy5nZXRib29sZWFuKCdEZWJ1Z0NvbW1hbmRzJywgZmFsbGJhY2s9"
    "X0RFQlVHX0xPR19DT01NQU5EUykKICAgIF9ERUJVR19MT0dfVkVSQk9TRSA9IHNlYy5nZXRib29s"
    "ZWFuKCdEZWJ1Z0NvbW1hbmRzVmVyYm9zZScsIGZhbGxiYWNrPV9ERUJVR19MT0dfVkVSQk9TRSkK"
    "Q0ZHID0gbG9hZENvbmZpZygpCmFwcGx5Q29uZmlnKENGRykKCiMjIyBVU0VSIFNUUlVDVFVSRQoj"
    "IGNvbm5lY3Rpb24KIyB1c2VybmFtZQojIGhlcm9kYXRhCiMgcG9zaXRpb24KIyBnYW1lY2hhbm5l"
    "bAojIGNoYXRjaGFubmVsCiMgZ2FtZQoKY2xhc3MgVXNlcigpOiAjVE9ETyBtZXJnZSB1c2VyIGlu"
    "dG8gY29ubmVjdGlvbj8sIHZhbGlkYXRpb24gY2FuIGJlIGFzc3VtZWQgYnkgc3RhZ2UKICAgIGRl"
    "ZiBfX2luaXRfXyhzZWxmLCBuYW1lLCBjb24pOgogICAgICAgIHNlbGYuaGVyb2RhdGEgPSBiJycK"
    "ICAgICAgICAjJzAjMCcsIG5vdCBOb25lOiB0aGlzIGdvZXMgc3RyYWlnaHQgaW50byB0aGUgJGdh"
    "bWVjaGFubmVsdXNlciBzZW50IHRvCiAgICAgICAgI2V2ZXJ5IG90aGVyIGNsaWVudCwgYW5kIGFu"
    "IHVuc2V0IHZhbHVlIHVzZWQgdG8gcmVhY2ggdGhlbSBhcyB0aGUKICAgICAgICAjbGl0ZXJhbCB0"
    "ZXh0ICJOb25lIiB3aGVyZSBjb29yZGluYXRlcyB3ZXJlIGV4cGVjdGVkLgogICAgICAgIHNlbGYu"
    "cG9zZGF0YSA9ICcwIzAnCiAgICAgICAgc2VsZi5wb3NjaGFuZ2VkID0gRmFsc2UKICAgICAgICBz"
    "ZWxmLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lCiAgICAgICAgc2VsZi5nYW1lY2hhbm5lbCA9IE5v"
    "bmUKICAgICAgICBzZWxmLmNoYXRjaGFubmVsID0gTm9uZQogICAgICAgIHNlbGYucmVxdWVzdGVk"
    "R2FtZSA9IE5vbmUKICAgICAgICBzZWxmLmdhbWUgPSBOb25lCiAgICAgICAgc2VsZi5uYW1lID0g"
    "bmFtZQogICAgICAgICNDYWNoZWQsIG5vdCBsb29rZWQgdXAgcGVyIG1lc3NhZ2U6IHRoZSBndWls"
    "ZCBuYW1lIGdvZXMgb3V0IGluIHRoZQogICAgICAgICNzZWNvbmQgZmllbGQgb2YgZXZlcnkgJGdh"
    "bWVjaGFubmVsdXNlciBhbmQgJGNoYXRjaGFubmVsdXNlciAtIHRoZQogICAgICAgICNzYW1lIGZp"
    "ZWxkIC93aG9pcyByZXBvcnRzIGFzIHRoZSBndWlsZCAtIGFuZCB0aG9zZSBhcmUgc2VudCBmYXIg"
    "dG9vCiAgICAgICAgI29mdGVuIHRvIGhpdCB0aGUgZGF0YWJhc2UgZWFjaCB0aW1lLgogICAgICAg"
    "IHNlbGYuZ3VpbGQgPSBzYW5pdGl6ZVRleHQoR0RILmdldEd1aWxkTmFtZShuYW1lKSkKICAgICAg"
    "ICBzZWxmLmxvZ2luVGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpCiAgICAgICAgc2VsZi5p"
    "ZG51bSA9IEdESC5nZXRVUmFuZG9tKCkKICAgICAgICBzZWxmLmNvbm5lY3Rpb24gPSBjb24gI3Nl"
    "cnZlciA9IGNvbi5zZXJ2ZXIKICAgICAgICAjc2VsZi5jb25uZWN0aW9uLmd1aWQgLT4gZ3VpZCB3"
    "aGVuIHJlbGV2YW50CiAgICAgICAgc2VsZi5wZ3VpZCA9IHByZXR0eV9ndWlkKHNlbGYuY29ubmVj"
    "dGlvbi5ndWlkKQogICAgZGVmIGxlYXZlQ2hhbm5lbChzZWxmKToKICAgICAgICBpZiBzZWxmLnJl"
    "cXVlc3RlZENoYW5uZWw6CiAgICAgICAgICAgICNsaXN0LnJlbW92ZSgpIHJhaXNlcyBWYWx1ZUVy"
    "cm9yIHdoZW4gdGhlIGVudHJ5IGlzIGFscmVhZHkgZ29uZTsKICAgICAgICAgICAgI3RoYXQgdXNl"
    "ZCB0byBhYm9ydCB0aGUgcmVzdCBvZiB0aGUgZGlzY29ubmVjdCBjbGVhbnVwCiAgICAgICAgICAg"
    "IGlmIHNlbGYuY29ubmVjdGlvbiBpbiBzZWxmLnJlcXVlc3RlZENoYW5uZWwucmVxdWVzdGVkOgog"
    "ICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWRDaGFubmVsLnJlcXVlc3RlZC5yZW1vdmUoc2Vs"
    "Zi5jb25uZWN0aW9uKQogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lCiAg"
    "ICAgICAgaWYgc2VsZi5nYW1lY2hhbm5lbDoKICAgICAgICAgICAgc2VsZi5nYW1lY2hhbm5lbC5s"
    "ZWF2ZUNoYW5uZWwoc2VsZi5jb25uZWN0aW9uKQogICAgICAgICAgICAjbGVhdmVDaGFubmVsIGFs"
    "c28gbGVhdmVzIGNoYXQKICAgIGRlZiBsZWF2ZUNoYXQoc2VsZik6CiAgICAgICAgaWYgc2VsZi5j"
    "aGF0Y2hhbm5lbDoKICAgICAgICAgICAgaWYgc2VsZi5jb25uZWN0aW9uIGluIHNlbGYuY2hhdGNo"
    "YW5uZWw6CiAgICAgICAgICAgICAgICBzZWxmLmNoYXRjaGFubmVsLnJlbW92ZShzZWxmLmNvbm5l"
    "Y3Rpb24pCiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmNoYXRjaGFubmVsdXNlciAie3Nl"
    "bGYubmFtZX0iJykKICAgICAgICAgICAgc2VsZi5jb25uZWN0aW9uLnNlcnZlci5kaXN0LmFkZCh7"
    "J3RhcmdldCc6c2VsZi5jaGF0Y2hhbm5lbCwnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgICAgICAg"
    "ICBzZWxmLmNoYXRjaGFubmVsPU5vbmUKICAgIGRlZiBzdG9wR2FtZShzZWxmKToKICAgICAgICBp"
    "ZiBzZWxmLnJlcXVlc3RlZEdhbWU6CiAgICAgICAgICAgICNCb3RoIGd1YXJkcyBtYXR0ZXI6IHRo"
    "ZSBjaGFubmVsIG1heSBhbHJlYWR5IGJlIGdvbmUgKGxlYXZlQ2hhbm5lbAogICAgICAgICAgICAj"
    "Y2xlYXJzIGl0IGJlZm9yZSBzdG9wR2FtZSBydW5zIG9uIHNvbWUgcGF0aHMpIGFuZCB0aGUgcGVu"
    "ZGluZwogICAgICAgICAgICAjcmVxdWVzdCBtYXkgYWxyZWFkeSBoYXZlIGJlZW4gY29uc3VtZWQg"
    "YnkgY3JlYXRlR2FtZS4gRWl0aGVyIG9uZQogICAgICAgICAgICAjdXNlZCB0byByYWlzZSAoQXR0"
    "cmlidXRlRXJyb3Igb24gTm9uZSAvIEtleUVycm9yKSBpbnNpZGUgdGhlCiAgICAgICAgICAgICNk"
    "aXNjb25uZWN0IHBhdGggYW5kIGFib3J0IHRoZSByZXN0IG9mIHRoZSBjbGVhbnVwLCBsZWFraW5n"
    "IHRoZQogICAgICAgICAgICAjcGxheWVyJ3MgZW50cnkgaW4gYWN0aXZlVXNlcnMuCiAgICAgICAg"
    "ICAgIGlmIHNlbGYuZ2FtZWNoYW5uZWw6CiAgICAgICAgICAgICAgICBzZWxmLmdhbWVjaGFubmVs"
    "LmdhbWVSZXF1ZXN0cy5wb3Aoc2VsZi5yZXF1ZXN0ZWRHYW1lLCBOb25lKQogICAgICAgICAgICBz"
    "ZWxmLnJlcXVlc3RlZEdhbWUgPSBOb25lCiAgICAgICAgaWYgc2VsZi5nYW1lOgogICAgICAgICAg"
    "ICBzZWxmLmdhbWUucmVtb3ZlKHNlbGYuY29ubmVjdGlvbikKICAgIGRlZiBkaXNjb25uZWN0KHNl"
    "bGYsIHNlcnZlcik6CiAgICAgICAgc2VsZi5zdG9wR2FtZSgpCiAgICAgICAgc2VsZi5sZWF2ZUNo"
    "YW5uZWwoKQogICAgICAgIHNlcnZlci5zdGF0ZS5yZWxlYXNlVXNlcihzZWxmLm5hbWUsIHNlbGYu"
    "Y29ubmVjdGlvbikKICAgICAgICBHREgucmVsZWFzZVVSYW5kb20oc2VsZi5pZG51bSkKICAgIGRl"
    "ZiB3aXJlSWQoc2VsZik6CiAgICAgICAgI1RoZSBvbmUgcGxhY2UgdGhlIGhlcm8gaWQgaXMgZm9y"
    "bWF0dGVkLCBzbyAkZ2FtZWNoYW5uZWx1c2VyIGFuZAogICAgICAgICMvdXBkaGVyb3BvcyBjYW4g"
    "bmV2ZXIgZGlzYWdyZWUgYWdhaW4gLSBzZWUgX0hFUk9fSURfSEVYLgogICAgICAgIHJldHVybiBm"
    "J3tzZWxmLmlkbnVtOnh9JyBpZiBfSEVST19JRF9IRVggZWxzZSBmJ3tzZWxmLmlkbnVtfScKICAg"
    "IGRlZiBnZXRHQ1Vtc2coc2VsZik6CiAgICAgICAgaGRsID0gbGVuKHNlbGYuaGVyb2RhdGEpCiAg"
    "ICAgICAgaWYgaGRsPT0wOgogICAgICAgICAgICByZXR1cm4gYicnCiAgICAgICAgcmV0dXJuIF9l"
    "bShmJyRnYW1lY2hhbm5lbHVzZXIgIntzZWxmLm5hbWV9IiAie3NlbGYuZ3VpbGR9IiAiMTAwIiAi"
    "e3NlbGYud2lyZUlkKCl9IiAiMCIgIntzZWxmLnBndWlkfSIgIntzZWxmLnBvc2RhdGF9IiAie2hk"
    "bH0iJykrc2VsZi5oZXJvZGF0YQogICAgZGVmIGdldENDVW1zZyhzZWxmKToKICAgICAgICB2YiA9"
    "IDAgI29yIDB4RkZGRkZGRkYoNDI5NDk2NzI5NT0gLTEmMzJiaXQ/KQogICAgICAgIHJldHVybiBf"
    "ZW0oZickY2hhdGNoYW5uZWx1c2VyICJ7c2VsZi5uYW1lfSIgIntzZWxmLmd1aWxkfSIgInt2Yn0i"
    "ICJ7c2VsZi5wZ3VpZH0iJykKICAgICAgICAjICRjaGF0Y2hhbm5lbHVzZXIgIntuYW1lfSIgIiIg"
    "IjAiICJ7Z3VpZH0iCiMgaW5jcmVhc2luZyBtYXkgaW1wcm92ZSBzZWN1cml0eSBhdCB0aGUgY29z"
    "dCBvZiBwZXJmb3JtYW5jZQojIG9ubHkgdXBkYXRlcyB3aGVuIHVzZXIgbG9ncyBpbiBhbmQgaXMg"
    "c3RvcmVkIGFsb25nc2lkZSBzYWx0IGluIGRhdGFiYXNlCl9IQVNISVRFUiA9IDEwMDAwMApkZWYg"
    "X3NhbHRfaGFzaF8ocGFzc3dvcmQsIHNhbHQsIGhJdHIpOgogICAgI3V0Zi04LCBub3QgYXNjaWk6"
    "IGEgcGFzc3dvcmQgd2l0aCBhbiA4LWJpdCBjaGFyYWN0ZXIgdXNlZCB0byByYWlzZSBoZXJlIGFu"
    "ZAogICAgI2Ryb3AgdGhlIGNvbm5lY3Rpb24gaW5zdGVhZCBvZiBsb2dnaW5nIHRoZSBwbGF5ZXIg"
    "aW4uIFB1cmUtYXNjaWkgcGFzc3dvcmRzCiAgICAjZW5jb2RlIHRvIGlkZW50aWNhbCBieXRlcyB1"
    "bmRlciBib3RoLCBzbyBubyBzdG9yZWQgaGFzaCBjaGFuZ2VzLgogICAgcmV0dXJuIGhhc2hsaWIu"
    "cGJrZGYyX2htYWMoJ3NoYTI1NicsIHBhc3N3b3JkLmVuY29kZSgndXRmLTgnKSwgc2FsdCwgaEl0"
    "cikKICAgIAojIyMgU1FMIElORk8KIyBfREJJTkZPOiBWRVJTSU9OIDEKIyB1c2VyVGFibGUKIyAt"
    "IHJvd2lkLCB1c2VybmFtZSwgcGFzc0hhc2gsIHNlcmlhbCwgdW5pcXVlU2FsdCwgbGFzdExvZ2lu"
    "LCBlbWFpbCwgbG9jYXRpb24sIHllYXJvZmJpcnRoKGVzdGltYXRlKSwgZ2VuZGVyLCBkZXNjcmlw"
    "dGlvbgojIGZvcm1UYWJsZQojIC0gcm93aWQsIGZvcm0KIyMgLS0tLS0tLS0tLS0tLS0tLSAjIwoj"
    "IFRPRE8gVkVSU0lPTiAyOiBndWlsZHMsIGxlYWRlcmJvYXJkLCBldGM/CgojVE9ETyBjb252ZXJ0"
    "IGRhdGFiYXNlIHRvIHNpbmdsZXRocmVhZCBhY2Nlc3MgZm9yIGNvbXBhdGliaWxpdHk/IHVubmVj"
    "Y2VzYXJ5PwojY2xhc3MgRGF0YVJlcXVlc3QodGhyZWFkaW5nLkV2ZW50KToKIyAgIGRhdGEgPSBO"
    "b25lCiMgICBkZWYgc2V0KHZhbCk6CiMgICAgICAgc2VsZi5kYXRhPXZhbAojICAgICAgIHN1cGVy"
    "KCkuc2V0KCkKIyAgIGRlZiB3YWl0KCk6CiMgICAgICAgc3VwZXIoKS53YWl0KCkKIyAgICAgICBy"
    "ZXR1cm4gc2VsZi5kYXRhCiMqIGRhdGFiYXNlIHRocmVhZDoKIyAgIF9kclEgPSBkYXRhIHJlcXVl"
    "c3QgcXVldWUsIHByb2Nlc3NlZCBpbiBkYXRhYmFzZSB0aHJlYWQKIyAgIGV4dGVybmFsIGZ1bmN0"
    "aW9ucyBhZGQgcmVxdWVzdCBmb3IgaW50ZXJuYWwgZnVuY3Rpb24gYW5kIHJldHVybiByZXF1ZXN0"
    "IHRvIGF3YWl0CiMgICBkcm9iaiBpbiBxdWV1ZSA9IChkciwgZnRhcmdldCwgKGFyZ3MpKSwgZHIu"
    "c2V0KGZ0YXJnZXQoKmFyZ3MpKQojVE9ETyBvcmdhbml6ZSBTUUwgY29tbWFuZHM/IG1ha2UgaXQg"
    "bW9yZSBiZWF1dGlmdWw/Cl9TUUxfZGJJbmZvRXhpc3RzID0gJ1NFTEVDVCBuYW1lIEZST00gc3Fs"
    "aXRlX21hc3RlciBXSEVSRSBuYW1lPSJfREJJTkZPIicKX1NRTF9kYlZlcnNpb24gPSAnU0VMRUNU"
    "IFZFUlNJT04gRlJPTSBfREJJTkZPJwpfU1FMSU5JVF9kYkluZm9UYWJsZSA9ICdDUkVBVEUgVEFC"
    "TEUgX0RCSU5GTyhWRVJTSU9OKScKX0RCQ1VSVkVSID0gMgpfU1FMSU5JVF9kYkluZm9WZXJzaW9u"
    "ID0gZidJTlNFUlQgSU5UTyBfREJJTkZPIFZBTFVFUyAoe19EQkNVUlZFUn0pJwpfU1FMVVBEX2Ri"
    "SW5mb1ZlcnNpb24gPSBmJ1VQREFURSBfREJJTkZPIFNFVCBWRVJTSU9OID0ge19EQkNVUlZFUn0n"
    "CiN5b2IgPSB5ZWFyIG9mIGJpcnRoIChlc3RpbWF0ZSkKI2dlbmRlcjogMCA9IE1hbGUKX1NRTElO"
    "SVRfZGJVc2VyVGFibGUgPSAnQ1JFQVRFIFRBQkxFIHVzZXJUYWJsZSh1c2VybmFtZSBVTklRVUUs"
    "IHBhc3NIYXNoLCBzZXJpYWwsIHVuaXF1ZVNhbHQsIGhhc2hJdGVyLCBsYXN0TG9naW4gVElNRVNU"
    "QU1QLCBlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVyLCBkZXNjcmlwdGlvbiknCl9TUUxJTklU"
    "X2RiRm9ybVRhYmxlID0gJ0NSRUFURSBUQUJMRSBmb3JtVGFibGUoZm9ybSBVTklRVUUpJyAjdXNp"
    "bmcgcm93aWQgYXMgSUQKIy0tLSBndWlsZHMgKERCIHZlcnNpb24gMikgLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tCiNyYW5rOiAyID0gZm91bmRlci9s"
    "ZWFkZXIsIDEgPSBvZmZpY2VyLCAwID0gbWVtYmVyLiBBIHBsYXllciBpcyBpbiBhdCBtb3N0IG9u"
    "ZQojZ3VpbGQsIHdoaWNoIGlzIHdoYXQgdGhlIGNsaWVudCdzIFVJIGFzc3VtZXMgKHdob2lzIGNh"
    "cnJpZXMgYSBzaW5nbGUgbmFtZSkuCiNndWlsZGtleSBpcyBndWlsZG5hbWUuY2FzZWZvbGQoKSBh"
    "bmQgaXMgd2hhdCB1bmlxdWVuZXNzIGFuZCBldmVyeSBsb29rdXAgZ28KI3Rocm91Z2guIFNRTGl0"
    "ZSdzIG93biBDT0xMQVRFIE5PQ0FTRSBvbmx5IGZvbGRzIEEtWiwgc28gb24gdGhpcyBzZXJ2ZXIg"
    "LQojd2hlcmUgdGhlIG5hbWVzIGFyZSBDeXJpbGxpYyAtIGl0IHdvdWxkIGhhdmUgbGV0ICLQndC+"
    "0YfQvdGL0LUg0JLQvtC70LrQuCIgYW5kICLQvdC+0YfQvdGL0LUKI9Cy0L7Qu9C60LgiIGNvZXhp"
    "c3QgYXMgdHdvIHNlcGFyYXRlIGd1aWxkcyB0aGF0IHBsYXllcnMgY291bGQgbm90IHRlbGwgYXBh"
    "cnQuCl9TUUxJTklUX2RiR3VpbGRUYWJsZSA9ICdDUkVBVEUgVEFCTEUgZ3VpbGRUYWJsZShndWls"
    "ZG5hbWUsIGd1aWxka2V5IFVOSVFVRSwgb3duZXIsIGNyZWF0ZWQgVElNRVNUQU1QLCBkZXNjcmlw"
    "dGlvbiknCl9TUUxJTklUX2RiR3VpbGRNZW1iZXJUYWJsZSA9ICdDUkVBVEUgVEFCTEUgZ3VpbGRN"
    "ZW1iZXJUYWJsZShndWlsZG5hbWUsIHVzZXJuYW1lIFVOSVFVRSwgcmFuayknCl9TUUxfZ3VpbGRF"
    "eGlzdHMgPSAnU0VMRUNUIGd1aWxkbmFtZSBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRrZXkg"
    "PSA/JwpfU1FMX2NyZWF0ZUd1aWxkID0gJ0lOU0VSVCBJTlRPIGd1aWxkVGFibGUgVkFMVUVTICg/"
    "LD8sPyw/LD8pJwpfU1FMX2RlbGV0ZUd1aWxkID0gJ0RFTEVURSBGUk9NIGd1aWxkVGFibGUgV0hF"
    "UkUgZ3VpbGRuYW1lID0gPycKX1NRTF9ndWlsZE93bmVyID0gJ1NFTEVDVCBvd25lciBGUk9NIGd1"
    "aWxkVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycKX1NRTF9hZGRHdWlsZE1lbWJlciA9ICdJTlNF"
    "UlQgT1IgUkVQTEFDRSBJTlRPIGd1aWxkTWVtYmVyVGFibGUgVkFMVUVTICg/LD8sPyknCl9TUUxf"
    "ZGVsR3VpbGRNZW1iZXIgPSAnREVMRVRFIEZST00gZ3VpbGRNZW1iZXJUYWJsZSBXSEVSRSB1c2Vy"
    "bmFtZSA9ID8nCl9TUUxfZGVsR3VpbGRNZW1iZXJzID0gJ0RFTEVURSBGUk9NIGd1aWxkTWVtYmVy"
    "VGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycKX1NRTF9ndWlsZE9mVXNlciA9ICdTRUxFQ1QgZ3Vp"
    "bGRuYW1lLCByYW5rIEZST00gZ3VpbGRNZW1iZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9T"
    "UUxfZ3VpbGRNZW1iZXJzID0gJ1NFTEVDVCB1c2VybmFtZSwgcmFuayBGUk9NIGd1aWxkTWVtYmVy"
    "VGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycKX1NRTF9hbGxHdWlsZHMgPSAnU0VMRUNUIGd1aWxk"
    "bmFtZSBGUk9NIGd1aWxkVGFibGUgT1JERVIgQlkgZ3VpbGRuYW1lIENPTExBVEUgTk9DQVNFJwoj"
    "U2FtZSBzaGFwZSBhcyB0aGUgdXNlcm5hbWUgcnVsZTogdGhlIG5hbWUgdHJhdmVscyBpbnNpZGUg"
    "cXVvdGVkIHByb3RvY29sCiNmaWVsZHMsIHNvIGFueXRoaW5nIHRoYXQgY291bGQgY2xvc2UgYSBx"
    "dW90ZSBpcyByZWplY3RlZCBvdXRyaWdodCByYXRoZXIgdGhhbgojc2lsZW50bHkgcmV3cml0dGVu"
    "LiBTcGFjZXMgYXJlIGFsbG93ZWQgLSBndWlsZCBuYW1lcyBjb21tb25seSBoYXZlIHRoZW0uCl9S"
    "RV9WQUxJRF9HVUlMRE5BTUUgPSByZS5jb21waWxlKHInXlteIlxyXG5cMF17MywzMn0kJykKCl9T"
    "UUxfdXNlcklEID0gJ1NFTEVDVCByb3dpZCBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9"
    "ID8nCl9TUUxfdXNlcklEX1NjaGsgPSAnU0VMRUNUIHJvd2lkIEZST00gdXNlclRhYmxlIFdIRVJF"
    "IHNlcmlhbCA9ID8nCl9TUUxfdXNlcklEX3N0cmljdCA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2Vy"
    "VGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/IEFORCBzZXJpYWwgPSA/JwpfU1FMX3JlZ2lzdGVyVXNl"
    "ciA9ICdJTlNFUlQgSU5UTyB1c2VyVGFibGUgVkFMVUVTICg/LD8sPyw/LD8sPyw/LD8sPyw/LD8p"
    "JwpfU1FMX2RlbGV0ZVVzZXIgPSAnREVMRVRFIEZST00gdXNlclRhYmxlIFdIRVJFIHVzZXJuYW1l"
    "ID0gPycKX1NRTF9nZXRMb2dpbiA9ICdTRUxFQ1QgdXNlcm5hbWUsIHBhc3NIYXNoLCB1bmlxdWVT"
    "YWx0LCBoYXNoSXRlciBGUk9NIHVzZXJUYWJsZSBXSEVSRSByb3dpZCA9ID8nCl9TUUxVUERfcGFz"
    "c0hhc2ggPSAnVVBEQVRFIHVzZXJUYWJsZSBTRVQgcGFzc0hhc2ggPSA/LCBoYXNoSXRlciA9ID8g"
    "V0hFUkUgcm93aWQgPSA/JwpfU1FMX2xvZ2luVXBkYXRlID0gJ1VQREFURSB1c2VyVGFibGUgU0VU"
    "IGxhc3RMb2dpbiA9ID8gV0hFUkUgcm93aWQgPSA/JwpfU1FMX2dldFdob2lzID0gJ1NFTEVDVCBl"
    "bWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVyLCBkZXNjcmlwdGlvbiBGUk9NIHVzZXJUYWJsZSBX"
    "SEVSRSB1c2VybmFtZSA9ID8nCl9TUUxVUERfd2hvaXMgPSAnVVBEQVRFIHVzZXJUYWJsZSBTRVQg"
    "ZW1haWwgPSA/LCBsb2NhdGlvbiA9ID8sIHlvYiA9ID8sIGdlbmRlciA9ID8sIGRlc2NyaXB0aW9u"
    "ID0gPyBXSEVSRSB1c2VybmFtZSA9ID8nCiNpZiBkb2VzIG5vdCBleGlzdCwgZ2VuZXJhdGUsIGNo"
    "YW5nZSBmb3JtYXQgZm9yIG1vZHBhY2tzCl9TUUxfZm9ybUlEID0gJ1NFTEVDVCByb3dpZCBmcm9t"
    "IGZvcm1UYWJsZSBXSEVSRSBmb3JtID0gPycKX1NRTEFERF9mb3JtSUQgPSAnSU5TRVJUIElOVE8g"
    "Zm9ybVRhYmxlIFZBTFVFUyAoPyknCl9GT1JNX1BERmlsZSA9ICd7Onh9X3s6eH0uYmluJyAjIHBs"
    "YXllcmRhdGFcdXNlcklEX2Zvcm1JRC5iaW4KCmRlZiByZWFkQmluKGZpbGVwYXRoKToKICAgIHdp"
    "dGggb3BlbihmaWxlcGF0aCwgInJiIikgYXMgZjoKICAgICAgICByZXR1cm4gZi5yZWFkKCkKY2xh"
    "c3MgRGF0YUhhbmRsZXIoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmKToKICAgICAgICAjaW5zdGFu"
    "Y2UgYXR0cmlidXRlLCBub3QgYSBjbGFzcyBhdHRyaWJ1dGUgLSBzYW1lIHJlYXNvbmluZyBhcwog"
    "ICAgICAgICNHYW1lU3RhdGUuYWN0aXZlVXNlcnM6IHNoYXJlZCBjbGFzcyBzdGF0ZSBsZWFrcyBi"
    "ZXR3ZWVuIGluc3RhbmNlcwogICAgICAgIHNlbGYudXNlZE51bXMgPSBzZXQoKQogICAgICAgICNw"
    "cmludCgnc3FsaXRlMyB0aHJlYWRzYWZldHk6JyxzcWxpdGUzLnRocmVhZHNhZmV0eSkKICAgICAg"
    "ICAjaWYgc3FsaXRlMy50aHJlYWRzYWZldHk8MzoKICAgICAgICAjICAgIHJhaXNlIEV4Y2VwdGlv"
    "bignTXVsdGlUaHJlYWQgc3VwcG9ydCByZXF1aXJlZCcpCiAgICAgICAgI1RPRE8gb3JnYW5pemUg"
    "c2luZ2xlIHRocmVhZGVkIGRhdGFiYXNlIGFjY2Vzcz8gZXZlciBuZWVkZWQ/CiAgICAgICAgc2Vs"
    "Zi5sb2NrID0gdGhyZWFkaW5nLlJMb2NrKCkKICAgICAgICBvcy5tYWtlZGlycyhfUEFUSF9QTEFZ"
    "RVJEQVRBLCBleGlzdF9vaz1UcnVlKQogICAgICAgIHNlbGYuZGIgPSBzcWxpdGUzLmNvbm5lY3Qo"
    "X1BBVEhfREFUQUJBU0UsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICBjaGVja19z"
    "YW1lX3RocmVhZCA9IEZhbHNlLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZGV0"
    "ZWN0X3R5cGVzPXNxbGl0ZTMuUEFSU0VfREVDTFRZUEVTIHwKICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgIHNxbGl0ZTMuUEFSU0VfQ09MTkFNRVMpCiAgICAgICAgaW5pdGN1ciA9IHNl"
    "bGYuZGIuY3Vyc29yKCkKICAgICAgICBkYlVuaW5pdGlhbGl6ZWQgPSBpbml0Y3VyLmV4ZWN1dGUo"
    "X1NRTF9kYkluZm9FeGlzdHMpLmZldGNob25lKCkgaXMgTm9uZQogICAgICAgIGlmIGRiVW5pbml0"
    "aWFsaXplZDoKICAgICAgICAgICAgZGJWZXJSZXMgPSAwCiAgICAgICAgZWxzZToKICAgICAgICAg"
    "ICAgZGJWZXJSZXMgPSBpbml0Y3VyLmV4ZWN1dGUoX1NRTF9kYlZlcnNpb24pLmZldGNob25lKClb"
    "MF0KICAgICAgICBzZWxmLnVwZGF0ZURCRnJvbShkYlZlclJlcykgI2Vuc3VyZSBEQiBpcyB1cGRh"
    "dGVkCiAgICAgICAgCiAgICAgICAgaW5pdGN1ci5jbG9zZSgpCiAgICBkZWYgZ2V0VVJhbmRvbShz"
    "ZWxmKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgcm51bSA9IHJhbmRvbS5y"
    "YW5kaW50KDEsMHg4MDAwKQogICAgICAgICAgICB3aGlsZSBybnVtIGluIHNlbGYudXNlZE51bXM6"
    "CiAgICAgICAgICAgICAgICBybnVtICs9IDEjRW5zdXJlIHVuaXF1ZQogICAgICAgICAgICBzZWxm"
    "LnVzZWROdW1zLmFkZChybnVtKQogICAgICAgICAgICByZXR1cm4gcm51bQogICAgZGVmIHJlbGVh"
    "c2VVUmFuZG9tKHNlbGYsIG51bSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAg"
    "IHNlbGYudXNlZE51bXMuZGlzY2FyZChudW0pI2Rpc2NhcmQ6IHNhZmUgZXZlbiBpZiBhbHJlYWR5"
    "IHJlbGVhc2VkCiAgICBkZWYgdXBkYXRlREJGcm9tKHNlbGYsIHZlcnNpb24pOgogICAgICAgIHBy"
    "aW50KCdEYXRhYmFzZSBWZXJzaW9uOicsdmVyc2lvbikKICAgICAgICBpZiB2ZXJzaW9uID49IF9E"
    "QkNVUlZFUjoKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgcHJpbnQoJ1VwZGF0aW5nIERhdGFi"
    "YXNlIHRvIFZlcnNpb24nLF9EQkNVUlZFUikKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgdXBkY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICBpZiB2ZXJzaW9uID09"
    "IDA6CiAgICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkluZm9UYWJsZSkK"
    "ICAgICAgICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9TUUxJTklUX2RiSW5mb1ZlcnNpb24pCiAg"
    "ICAgICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYlVzZXJUYWJsZSkKICAgICAg"
    "ICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9TUUxJTklUX2RiRm9ybVRhYmxlKQogICAgICAgICAg"
    "ICBpZiB2ZXJzaW9uIDwgMjoKICAgICAgICAgICAgICAgICNHdWlsZCBzdG9yYWdlLiBBZGRpdGl2"
    "ZSBvbmx5LCBzbyBhbiBleGlzdGluZyB2MSBkYXRhYmFzZSB3aXRoCiAgICAgICAgICAgICAgICAj"
    "cmVhbCBhY2NvdW50cyBpbiBpdCB1cGdyYWRlcyBpbiBwbGFjZS4KICAgICAgICAgICAgICAgIHVw"
    "ZGN1ci5leGVjdXRlKF9TUUxJTklUX2RiR3VpbGRUYWJsZSkKICAgICAgICAgICAgICAgIHVwZGN1"
    "ci5leGVjdXRlKF9TUUxJTklUX2RiR3VpbGRNZW1iZXJUYWJsZSkKICAgICAgICAgICAgI1RoZSB2"
    "ZXJzaW9uIHJvdyB3YXMgb25seSBldmVyIHdyaXR0ZW4gYnkgdGhlIHZlcnNpb249PTAgYnJhbmNo"
    "LCBzbwogICAgICAgICAgICAjZXZlcnkgbGF0ZXIgbWlncmF0aW9uIHdvdWxkIGhhdmUgcmUtcnVu"
    "IG9uIHRoZSBuZXh0IHN0YXJ0LgogICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMVVBEX2Ri"
    "SW5mb1ZlcnNpb24pCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgdXBk"
    "Y3VyLmNsb3NlKCkKICAgIGRlZiBnZXRQREZOKHNlbGYsIG5hbWUsIGZvcm0sIGNyZWF0ZSk6CiAg"
    "ICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGZvcm1jdXIgPSBzZWxmLmRiLmN1cnNv"
    "cigpCiAgICAgICAgICAgIHVpZHJlcyA9IGZvcm1jdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKG5h"
    "bWUsICkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAg"
    "ICAgICAgICBmb3JtY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNVc2Vy"
    "IGRvZXNuJ3QgZXhpc3QKICAgICAgICAgICAgZmlkcmVzID0gZm9ybWN1ci5leGVjdXRlKF9TUUxf"
    "Zm9ybUlELCAoZm9ybSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiBmaWRyZXMgaXMgTm9u"
    "ZTogI2Zvcm1hdCBkb2VzIG5vdCBleGlzdAogICAgICAgICAgICAgICAgaWYgbm90IGNyZWF0ZToK"
    "ICAgICAgICAgICAgICAgICAgICBmb3JtY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgICAgICBy"
    "ZXR1cm4gTm9uZSAjTmV3IGZvcm1hdCBub3QgY3JlYXRlZAogICAgICAgICAgICAgICAgZm9ybWN1"
    "ci5leGVjdXRlKF9TUUxBRERfZm9ybUlELCAoZm9ybSwgKSkKICAgICAgICAgICAgICAgIHNlbGYu"
    "ZGIuY29tbWl0KCkjVE9ETyBDaGVjayBpZiBnb3R0YSBjb21taXQgYmVmb3JlIHJlYWQtYmFjaz8K"
    "ICAgICAgICAgICAgICAgIGZpZHJlcyA9IGZvcm1jdXIuZXhlY3V0ZShfU1FMX2Zvcm1JRCwgKGZv"
    "cm0sICkpLmZldGNob25lKCkKICAgICAgICAgICAgZm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAg"
    "IGZpZCA9IGZpZHJlc1swXQogICAgICAgICAgICB1aWQgPSB1aWRyZXNbMF0KICAgICAgICAgICAg"
    "ZmlsZW5hbWUgPSBfRk9STV9QREZpbGUuZm9ybWF0KHVpZCwgZmlkKQogICAgICAgICAgICBmcGF0"
    "aCA9IG9zLnBhdGguam9pbihfUEFUSF9QTEFZRVJEQVRBLCBmaWxlbmFtZSkKICAgICAgICAgICAg"
    "aWYgb3MucGF0aC5leGlzdHMoZnBhdGgpIG9yIGNyZWF0ZToKICAgICAgICAgICAgICAgIHJldHVy"
    "biBmcGF0aAogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgZGVmIGdldFBsYXllckRhdGEoc2Vs"
    "ZiwgbmFtZSwgZm9ybSk6CiAgICAgICAgcGF0aCA9IHNlbGYuZ2V0UERGTihuYW1lLCBmb3JtLCBG"
    "YWxzZSkKICAgICAgICBpZiBub3QgcGF0aDoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAg"
    "IHJldHVybiByZWFkQmluKHBhdGgpI1RPRE8gZGVmYXVsdCB0byBiJycgb24gZXJyb3I/CiAgICBk"
    "ZWYgc2V0UGxheWVyRGF0YShzZWxmLCBuYW1lLCBmb3JtLCBkYXRhKToKICAgICAgICBwYXRoID0g"
    "c2VsZi5nZXRQREZOKG5hbWUsIGZvcm0sIFRydWUpCiAgICAgICAgaWYgbm90IHBhdGg6I05PIEZJ"
    "TEUgUEFUSCwgVE9ETyBDQVRDSCBFUlJPUgogICAgICAgICAgICByZXR1cm4KICAgICAgICAjV3Jp"
    "dHRlbiB0byBhIHRlbXAgZmlsZSBhbmQgbW92ZWQgaW50byBwbGFjZSwgbm90IHdyaXR0ZW4gaW4g"
    "cGxhY2UuCiAgICAgICAgI1RoZSBnYW1lIGNhbGxzIC9zZXRwbGF5ZXJkYXRhIHRvIGF1dG9zYXZl"
    "IG1pZC1zZXNzaW9uLCBub3Qgb25seSBvbiBhCiAgICAgICAgI2NsZWFuIGV4aXQgLSB0aGUgbGl2"
    "ZSBsb2dzIHNob3cgaXQgZmlyaW5nIHdoaWxlIGEgcGxheWVyIGlzIHdhbGtpbmcKICAgICAgICAj"
    "YXJvdW5kLCB3ZWxsIGJlZm9yZSAvbGVhdmVnYW1lLiBgb3BlbihwYXRoLCd3YicpYCB0cnVuY2F0"
    "ZXMgdGhlIHNhdmUKICAgICAgICAjdG8gemVybyBieXRlcyAqYmVmb3JlKiB3cml0aW5nIGEgc2lu"
    "Z2xlIGJ5dGUgb2YgdGhlIG5ldyBvbmU6IGEgY3Jhc2gsCiAgICAgICAgI2Ega2lsbGVkIHByb2Nl"
    "c3Mgb3IgYSBsb3N0IGNvbm5lY3Rpb24gYXQgZXhhY3RseSB0aGUgd3JvbmcgaW5zdGFudAogICAg"
    "ICAgICNsZWZ0IGEgMC1ieXRlIG9yIGhhbGYtd3JpdHRlbiBzYXZlLCBhbmQgZ2V0UGxheWVyRGF0"
    "YSgpIHRoZW4gaGFuZGVkCiAgICAgICAgI3RoYXQgYmFjayBhcyAieW91ciBjaGFyYWN0ZXIncyBk"
    "YXRhIiBvbiB0aGUgbmV4dCBsb2dpbiAtIHRoaXMgaXMKICAgICAgICAjYWxtb3N0IGNlcnRhaW5s"
    "eSB0aGUgInByb2dyZXNzIGdldHMgbG9zdCIgcmVwb3J0LiBvcy5yZXBsYWNlKCkgaXMKICAgICAg"
    "ICAjYXRvbWljIG9uIGJvdGggV2luZG93cyBhbmQgUE9TSVg6IHRoZSBmaWxlIG9uIGRpc2sgaXMg"
    "ZWl0aGVyIHRoZQogICAgICAgICNjb21wbGV0ZSBvbGQgc2F2ZSBvciB0aGUgY29tcGxldGUgbmV3"
    "IG9uZSwgbmV2ZXIgYSBwYXJ0aWFsIHdyaXRlLgogICAgICAgIHRtcCA9IHBhdGggKyBmJy57b3Mu"
    "Z2V0cGlkKCl9Lnt0aHJlYWRpbmcuZ2V0X2lkZW50KCl9LnRtcCcKICAgICAgICB0cnk6CiAgICAg"
    "ICAgICAgIHdpdGggb3Blbih0bXAsICd3YicpIGFzIGY6CiAgICAgICAgICAgICAgICBmLndyaXRl"
    "KGRhdGEpCiAgICAgICAgICAgICAgICBmLmZsdXNoKCkKICAgICAgICAgICAgICAgIG9zLmZzeW5j"
    "KGYuZmlsZW5vKCkpCiAgICAgICAgICAgIG9zLnJlcGxhY2UodG1wLCBwYXRoKQogICAgICAgIGV4"
    "Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBvcy5yZW1vdmUo"
    "dG1wKQogICAgICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgICAgIHBhc3MKICAg"
    "ICAgICAgICAgcmFpc2UKICAgIGRlZiBnZXRXaG9pcyhzZWxmLCBuYW1lKToKICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAg"
    "ICAgcmVzID0gd2N1ci5leGVjdXRlKF9TUUxfZ2V0V2hvaXMsIChuYW1lLCkpLmZldGNob25lKCkK"
    "ICAgICAgICAgICAgd2N1ci5jbG9zZSgpCiAgICAgICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAg"
    "ICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICAgICAgKGVtYWlsLCBsb2NhdGlvbiwgeW9i"
    "LCBnZW5kZXIsIGRlc2NyaXB0aW9uKSA9IHJlcwogICAgICAgICAgICBjdXJZZWFyID0gZGF0ZXRp"
    "bWUuZGF0ZXRpbWUubm93KCkueWVhcgogICAgICAgICAgICBhZ2UgPSBtYXgoMCwgY3VyWWVhciAt"
    "IHlvYikgaWYgeW9iIGVsc2UgMAogICAgICAgICAgICByZXR1cm4gewogICAgICAgICAgICAgICAg"
    "J2VtYWlsJzogZW1haWwgb3IgJycsCiAgICAgICAgICAgICAgICAnbG9jYXRpb24nOiBsb2NhdGlv"
    "biBvciAnJywKICAgICAgICAgICAgICAgICdhZ2UnOiBhZ2UsCiAgICAgICAgICAgICAgICAnZ2Vu"
    "ZGVyJzogZ2VuZGVyIGlmIGdlbmRlciBpcyBub3QgTm9uZSBlbHNlIDAsCiAgICAgICAgICAgICAg"
    "ICAnZGVzY3JpcHRpb24nOiBkZXNjcmlwdGlvbiBvciAnJwogICAgICAgICAgICB9CiAgICBkZWYg"
    "dXBkYXRlV2hvaXMoc2VsZiwgbmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVz"
    "Y3JpcHRpb24pOgogICAgICAgIHRyeToKICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAg"
    "ICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgICAgIGFnZSA9IDAKICAg"
    "ICAgICB0cnk6CiAgICAgICAgICAgIGdlbmRlciA9IGludChnZW5kZXIpCiAgICAgICAgZXhjZXB0"
    "IChUeXBlRXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgICAgICBnZW5kZXIgPSAwCiAgICAgICAg"
    "eW9iID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCkueWVhciAtIGFnZQogICAgICAgIHdpdGggc2Vs"
    "Zi5sb2NrOgogICAgICAgICAgICB3Y3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB3"
    "Y3VyLmV4ZWN1dGUoX1NRTFVQRF93aG9pcywgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIs"
    "IGRlc2NyaXB0aW9uLCBuYW1lKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAg"
    "ICAgICB3Y3VyLmNsb3NlKCkKICAgICMjIEdVSUxEUwogICAgZGVmIGdldEd1aWxkT2Yoc2VsZiwg"
    "dXNlcm5hbWUpOgogICAgICAgICMtPiAoZ3VpbGRuYW1lLCByYW5rKSBvciAoTm9uZSwgMCkKICAg"
    "ICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQog"
    "ICAgICAgICAgICByZXMgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5hbWUs"
    "KSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIGlmIHJlcyBpcyBO"
    "b25lOgogICAgICAgICAgICByZXR1cm4gKE5vbmUsIDApCiAgICAgICAgcmV0dXJuIChyZXNbMF0s"
    "IHJlc1sxXSBvciAwKQogICAgZGVmIGdldEd1aWxkTmFtZShzZWxmLCB1c2VybmFtZSk6CiAgICAg"
    "ICAgcmV0dXJuIHNlbGYuZ2V0R3VpbGRPZih1c2VybmFtZSlbMF0gb3IgJycKICAgIGRlZiBnZXRH"
    "dWlsZE1lbWJlcnMoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAg"
    "ICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBjdXIuZXhl"
    "Y3V0ZShfU1FMX2d1aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKS5mZXRjaGFsbCgpCiAgICAgICAg"
    "ICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIFsoclswXSwgclsxXSBvciAwKSBmb3IgciBp"
    "biByZXNdCiAgICBkZWYgZ3VpbGRFeGlzdHMoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAg"
    "ICByb3cgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkRXhpc3RzLCAoKGd1aWxkbmFtZSBvciAnJyku"
    "Y2FzZWZvbGQoKSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAg"
    "cmV0dXJuIHJvdyBpcyBub3QgTm9uZQogICAgZGVmIGd1aWxkTmFtZUZyZWUoc2VsZiwgZ3VpbGRu"
    "YW1lKToKICAgICAgICAjU2FtZSBydWxlcyBjcmVhdGVHdWlsZCgpIGVuZm9yY2VzLCBhc2tlZCBp"
    "biBhZHZhbmNlIC0gdGhlIGNsaWVudAogICAgICAgICNjaGVja3MgYSBuYW1lIHdpdGggL3Rlc3Rj"
    "cmVhdGVndWlsZCBiZWZvcmUgaXQgd2lsbCBsZXQgdGhlIHBsYXllcgogICAgICAgICNjb25maXJt"
    "LiBBbnN3ZXJpbmcgImZyZWUiIGZvciBhIG5hbWUgY3JlYXRlR3VpbGQgd291bGQgdGhlbiByZWpl"
    "Y3QKICAgICAgICAjd291bGQganVzdCBtb3ZlIHRoZSBkZWFkIGVuZCBvbmUgZGlhbG9nIGxhdGVy"
    "LgogICAgICAgIGlmIG5vdCBfUkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFtZSBvciAn"
    "Jyk6CiAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAgICAgIHJldHVybiBub3Qgc2VsZi5ndWls"
    "ZEV4aXN0cyhndWlsZG5hbWUpCiAgICBkZWYgbGlzdEd1aWxkcyhzZWxmKToKICAgICAgICB3aXRo"
    "IHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAg"
    "ICByb3dzID0gY3VyLmV4ZWN1dGUoX1NRTF9hbGxHdWlsZHMpLmZldGNoYWxsKCkKICAgICAgICAg"
    "ICAgY3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4gW3JbMF0gZm9yIHIgaW4gcm93c10KICAgIGRl"
    "ZiBjcmVhdGVHdWlsZChzZWxmLCBndWlsZG5hbWUsIG93bmVyLCBkZXNjcmlwdGlvbj0nJyk6CiAg"
    "ICAgICAgIy0+IGd1aWxkbmFtZSBvbiBzdWNjZXNzLCBvciBhbiBlcnJvciB0b2tlbiBmb3IgdGhl"
    "IGNsaWVudAogICAgICAgIGlmIG5vdCBfUkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFt"
    "ZSBvciAnJyk6CiAgICAgICAgICAgIHJldHVybiAnYmFkR3VpbGROYW1lJwogICAgICAgIHdpdGgg"
    "c2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAg"
    "IGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRPZlVzZXIsIChvd25lciwpKS5mZXRjaG9uZSgpIGlz"
    "IG5vdCBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJl"
    "dHVybiAnYWxyZWFkeUluR3VpbGQnCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3Vp"
    "bGRFeGlzdHMsIChndWlsZG5hbWUuY2FzZWZvbGQoKSwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25l"
    "OgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnZ3Vp"
    "bGROYW1lVGFrZW4nCiAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfY3JlYXRlR3VpbGQsCiAg"
    "ICAgICAgICAgICAgICAgICAgICAgIChndWlsZG5hbWUsIGd1aWxkbmFtZS5jYXNlZm9sZCgpLCBv"
    "d25lciwKICAgICAgICAgICAgICAgICAgICAgICAgIGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLCBz"
    "YW5pdGl6ZVRleHQoZGVzY3JpcHRpb24pKSkKICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9h"
    "ZGRHdWlsZE1lbWJlciwgKGd1aWxkbmFtZSwgb3duZXIsIDIpKQogICAgICAgICAgICBzZWxmLmRi"
    "LmNvbW1pdCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAg"
    "IGRlZiBqb2luR3VpbGQoc2VsZiwgZ3VpbGRuYW1lLCB1c2VybmFtZSk6CiAgICAgICAgd2l0aCBz"
    "ZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAg"
    "cm93ID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZEV4aXN0cywgKChndWlsZG5hbWUgb3IgJycpLmNh"
    "c2Vmb2xkKCksKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiByb3cgaXMgTm9uZToKICAgICAg"
    "ICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ3Vua25vd25HdWls"
    "ZCcKICAgICAgICAgICAgI1N0b3JlIHRoZSBndWlsZCdzIG93biBzcGVsbGluZywgbm90IHdoYXRl"
    "dmVyIGNhc2UgdGhlIGNsaWVudCB0eXBlZAogICAgICAgICAgICAjaW50byB0aGUgam9pbiBib3gs"
    "IHNvIGdldEd1aWxkTWVtYmVycygpIGZpbmRzIHRoZSBtZW1iZXIgYmFjay4KICAgICAgICAgICAg"
    "Z3VpbGRuYW1lID0gcm93WzBdCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRP"
    "ZlVzZXIsICh1c2VybmFtZSwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAgICAgICAg"
    "ICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnYWxyZWFkeUluR3VpbGQnCiAg"
    "ICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfYWRkR3VpbGRNZW1iZXIsIChndWlsZG5hbWUsIHVz"
    "ZXJuYW1lLCAwKSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIu"
    "Y2xvc2UoKQogICAgICAgIHJldHVybiBOb25lCiAgICBkZWYgbGVhdmVHdWlsZChzZWxmLCB1c2Vy"
    "bmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIu"
    "Y3Vyc29yKCkKICAgICAgICAgICAgcmVzID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwg"
    "KHVzZXJuYW1lLCkpLmZldGNob25lKCkKICAgICAgICAgICAgaWYgcmVzIGlzIE5vbmU6CiAgICAg"
    "ICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuICdub3RJbkd1aWxk"
    "JwogICAgICAgICAgICAoZ3VpbGRuYW1lLCByYW5rKSA9IChyZXNbMF0sIHJlc1sxXSBvciAwKQog"
    "ICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2RlbEd1aWxkTWVtYmVyLCAodXNlcm5hbWUsKSkK"
    "ICAgICAgICAgICAgb3duZXIgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT3duZXIsIChndWlsZG5h"
    "bWUsKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiBvd25lciBhbmQgb3duZXJbMF0gPT0gdXNl"
    "cm5hbWU6CiAgICAgICAgICAgICAgICAjVGhlIGZvdW5kZXIgbGVhdmluZyBkaXNzb2x2ZXMgdGhl"
    "IGd1aWxkIHJhdGhlciB0aGFuIGxlYXZpbmcgYW4KICAgICAgICAgICAgICAgICNvd25lcmxlc3Mg"
    "cmVjb3JkIHRoYXQgbm9ib2R5IGNhbiBldmVyIGFkbWluaXN0ZXIuCiAgICAgICAgICAgICAgICBj"
    "dXIuZXhlY3V0ZShfU1FMX2RlbEd1aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKQogICAgICAgICAg"
    "ICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxldGVHdWlsZCwgKGd1aWxkbmFtZSwpKQogICAgICAg"
    "ICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0"
    "dXJuIE5vbmUKICAgIGRlZiBsb2dpblBsYXllcihzZWxmLCB1c2VybmFtZSwgY29uLCBwYXNzd29y"
    "ZCk6I1RPRE8gc2hvdWxkIHJldHVybiBlcnJvciBwcm9wZXJseSB0byBjbGllbnQKICAgICAgICBp"
    "ZiBub3QgX1JFX1ZBTElEX1VTRVJOQU1FLm1hdGNoKHVzZXJuYW1lKToKICAgICAgICAgICAgI1Jl"
    "Z2lzdHJhdGlvbiBoYXMgYWx3YXlzIHZhbGlkYXRlZCB0aGUgbmFtZTsgbG9nZ2luZyBpbiBkaWQg"
    "bm90LgogICAgICAgICAgICAjTmFtZXMgcmVhY2ggb3RoZXIgY2xpZW50cyBpbnNpZGUgcXVvdGVk"
    "IHByb3RvY29sIGZpZWxkcywgc28gYSBuYW1lCiAgICAgICAgICAgICNjb250YWluaW5nICciJyBm"
    "b3JnZXMgY29tbWFuZHMgLSBhbmQgdGhlIEFsbG93QW55TG9naW4gZGVidWcgcGF0aAogICAgICAg"
    "ICAgICAjYmVsb3cgbmV2ZXIgdG91Y2hlcyB0aGUgZGF0YWJhc2UsIHdoaWNoIG1hZGUgaXQgdGhl"
    "IG9uZSB3YXkgdG8gZ2V0CiAgICAgICAgICAgICNzdWNoIGEgbmFtZSBpbi4gQ2hlY2sgaGVyZSBz"
    "byBib3RoIHBhdGhzIGFyZSBjb3ZlcmVkLgogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAg"
    "IGlmIF9ERUJVR19BTExPV19BTllfTE9HSU46ICNERUJVRyBBVVRPIEFMTE9XCiAgICAgICAgICAg"
    "IHJldHVybiBVc2VyKHVzZXJuYW1lLCBjb24pCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAg"
    "ICAgICAgIGxvZ2luQ3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICAjRGVmYXVsdCB0"
    "byBTVFJJQ1QsIFRPRE8gYWxsb3cgZm9yIG5vbi1zdHJpY3Q/CiAgICAgICAgICAgIHVpZHJlcyA9"
    "IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF91c2VySURfc3RyaWN0LCAodXNlcm5hbWUsIGNvbi5TSykp"
    "LmZldGNob25lKCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAg"
    "ICAjcHJpbnQoJ2xvZ2luIGVycm9yOiBubyB1c2VyIHdpdGggdGhhdCBzZXJpYWwga2V5JykKICAg"
    "ICAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25l"
    "ICNObyBzdWNoIFVzZXIKICAgICAgICAgICAgdWlkID0gdWlkcmVzWzBdCiAgICAgICAgICAgIChy"
    "VXNlciwgcGFzc2hhc2gsIHVTYWx0LCBoSXRyKSA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9nZXRM"
    "b2dpbiwgKHVpZCwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1c2VybmFtZSAhPSByVXNl"
    "cjoKICAgICAgICAgICAgICAgICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyB1c2VybmFtZTog"
    "e3VzZXJuYW1lfScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAg"
    "ICAgICByZXR1cm4gTm9uZSAjV3JvbmcgVXNlcm5hbWUKICAgICAgICAgICAgdHBhcyA9IF9zYWx0"
    "X2hhc2hfKHBhc3N3b3JkLCB1U2FsdCwgaEl0cikKICAgICAgICAgICAgaWYgdHBhcyAhPSBwYXNz"
    "aGFzaDoKICAgICAgICAgICAgICAgICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyBwYXNzd29y"
    "ZDoge3Bhc3N3b3JkfScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAg"
    "ICAgICAgICByZXR1cm4gTm9uZSAjV3JvbmcgUGFzc3dvcmQKICAgICAgICAgICAgaWYgaEl0ciAh"
    "PSBfSEFTSElURVI6CiAgICAgICAgICAgICAgICBucHNoID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQs"
    "IHVTYWx0LCBfSEFTSElURVIpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxV"
    "UERfcGFzc0hhc2gsIChucHNoLCBfSEFTSElURVIsIHVpZCkpCiAgICAgICAgICAgIHVzZXJvYmog"
    "PSBVc2VyKHVzZXJuYW1lLCBjb24pCiAgICAgICAgICAgICN1cGRhdGUgbGFzdCBsb2dpbgogICAg"
    "ICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxfbG9naW5VcGRhdGUsICh1c2Vyb2JqLmxvZ2lu"
    "VGltZSwgdWlkKSkKICAgICAgICAgICAgI1RPRE8gZGVmYXVsdCBkYXRldGltZSBhZGFwdGVyIGRl"
    "cHJlY2F0ZWQsIGNoZWNrIHJlcGxhY2VtZW50CiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkK"
    "ICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICByZXR1cm4gdXNlcm9iagog"
    "ICAgZGVmIHJlZ2lzdGVyUGxheWVyKHNlbGYsIHVzZXJuYW1lLCBjb24sIHBhc3N3b3JkLCBlbWFp"
    "bCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgaWYgbm90IF9S"
    "RV9WQUxJRF9VU0VSTkFNRS5tYXRjaCh1c2VybmFtZSk6CiAgICAgICAgICAgIHJldHVybiBOb25l"
    "ICNJbnZhbGlkIHVzZXJuYW1lIChiYWQgY2hhcnMvbGVuZ3RoKSwgYWxzbyBibG9ja3MgcHJvdG9j"
    "b2wtaW5qZWN0aW9uIHZpYSAnIicKICAgICAgICBlbWFpbCA9IHNhbml0aXplVGV4dChlbWFpbCkK"
    "ICAgICAgICBsb2NhdGlvbiA9IHNhbml0aXplVGV4dChsb2NhdGlvbikKICAgICAgICBkZXNjcmlw"
    "dGlvbiA9IHNhbml0aXplVGV4dChkZXNjcmlwdGlvbikKICAgICAgICB3aXRoIHNlbGYubG9jazoK"
    "ICAgICAgICAgICAgbG9naW5DdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHVpZHJl"
    "cyA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF91c2VySUQsICh1c2VybmFtZSwgKSkuZmV0Y2hvbmUo"
    "KQogICAgICAgICAgICBpZiB1aWRyZXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICAjcHJp"
    "bnQoZidyZWdpc3RlciBlcnJvcjogdXNlcm5hbWUgYWxyZWFkeSBpbiB1c2U6IHt1c2VybmFtZX0n"
    "KQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJu"
    "IE5vbmUgI1VzZXIgZXhpc3RzCiAgICAgICAgICAgICNpZiBzdHJpY3QsIGNoZWNrIGlmIHNlcmlh"
    "bCBpcyBpbiB1c2UgdG9vCiAgICAgICAgICAgICNUT0RPIG9ubHkgYXBwbHkgaWYgc3RyaWN0CiAg"
    "ICAgICAgICAgIHVpZHJlcyA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF91c2VySURfU2NoaywgKGNv"
    "bi5TSywgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1aWRyZXMgaXMgbm90IE5vbmU6CiAg"
    "ICAgICAgICAgICAgICAjcHJpbnQoJ3JlZ2lzdGVyIGVycm9yOiBzZXJpYWwgYWxyZWFkeSBpbiB1"
    "c2UnKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0"
    "dXJuIE5vbmUgI1NlcmlhbCBpbiB1c2UgZXhpc3RzCiAgICAgICAgICAgIHVTYWx0ID0gb3MudXJh"
    "bmRvbSgxNikKICAgICAgICAgICAgcEhhc2ggPSBfc2FsdF9oYXNoXyhwYXNzd29yZCwgdVNhbHQs"
    "IF9IQVNISVRFUikKICAgICAgICAgICAgY3VydGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygp"
    "CiAgICAgICAgICAgIHRyeTojdHJ5IHNob3VsZG4ndCBiZSBuZWVkZWQgYXMgZW1wdHkgZmllbGQg"
    "aXMgc2V0IHRvIDI1NQogICAgICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAgICAgICAg"
    "ZXhjZXB0OgogICAgICAgICAgICAgICAgYWdlID0gMAogICAgICAgICAgICB5b2IgPSBjdXJ0aW1l"
    "LnllYXIgLSBhZ2UKICAgICAgICAgICAgcmVndmFscyA9ICgKICAgICAgICAgICAgICAgIHVzZXJu"
    "YW1lLHBIYXNoLAogICAgICAgICAgICAgICAgY29uLlNLLHVTYWx0LF9IQVNISVRFUiwKICAgICAg"
    "ICAgICAgICAgIGN1cnRpbWUsZW1haWwsbG9jYXRpb24seW9iLGdlbmRlcixkZXNjcmlwdGlvbgog"
    "ICAgICAgICAgICApCiAgICAgICAgICAgIGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9yZWdpc3RlclVz"
    "ZXIsIHJlZ3ZhbHMpCiAgICAgICAgICAgICNUT0RPIGRlZmF1bHQgZGF0ZXRpbWUgYWRhcHRlciBk"
    "ZXByZWNhdGVkLCBjaGVjayByZXBsYWNlbWVudAogICAgICAgICAgICB1c2Vyb2JqID0gVXNlcih1"
    "c2VybmFtZSwgY29uKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIGxv"
    "Z2luQ3VyLmNsb3NlKCkKICAgICAgICAgICAgcmV0dXJuIHVzZXJvYmoKICAgIGRlZiBuYW1lVGFr"
    "ZW4oc2VsZiwgdXNlcm5hbWUpOgogICAgICAgICNEb2VzIGFuIGFjY291bnQgd2l0aCB0aGlzIG5h"
    "bWUgZXhpc3QgYXQgYWxsLCByZWdhcmRsZXNzIG9mIHNlcmlhbD8KICAgICAgICAjVXNlZCB0byB0"
    "ZWxsICJ0aGlzIG5hbWUgaXMgZnJlZSBidXQgeW91ciBrZXkgZG9lcyBub3QgbWF0Y2ggdGhlCiAg"
    "ICAgICAgI2FjY291bnQiIGFwYXJ0IGZyb20gInRoaXMgbmFtZSBpcyBnZW51aW5lbHkgdW51c2Fi"
    "bGUiLgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1"
    "cnNvcigpCiAgICAgICAgICAgIHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5h"
    "bWUsICkpLmZldGNob25lKCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4g"
    "cmVzIGlzIG5vdCBOb25lCiAgICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1c2VybmFtZSk6CiAg"
    "ICAgICAgI0FkbWluLXBhbmVsIGFjdGlvbiAoR1VJICLQo9C00LDQu9C40YLRjCDQv9C10YDRgdC+"
    "0L3QsNC20LAiKTogcGVybWFuZW50bHkgcmVtb3ZlcyBhbgogICAgICAgICNhY2NvdW50IGFuZCBl"
    "dmVyeSBzYXZlZCBwbGF5ZXJkYXRhIGJsb2IgZm9yIGl0LiBJcnJldmVyc2libGUgLSB0aGUKICAg"
    "ICAgICAjR1VJIGlzIGV4cGVjdGVkIHRvIGNvbmZpcm0gd2l0aCB0aGUgYWRtaW4gYmVmb3JlIGNh"
    "bGxpbmcgdGhpcy4KICAgICAgICAjRG9lcyBOT1QgdG91Y2ggdGhlIGNhbGxlcidzIGxpdmUgY29u"
    "bmVjdGlvbi9zZXNzaW9uOyB0aGUgY2FsbGVyIGlzCiAgICAgICAgI3Jlc3BvbnNpYmxlIGZvciBr"
    "aWNraW5nIGZpcnN0IGlmIHRoZSBhY2NvdW50IGlzIGN1cnJlbnRseSBvbmxpbmUKICAgICAgICAj"
    "KHNlZSBDb3JlU2VydmVyLmRlbGV0ZUFjY291bnQpLCBvdGhlcndpc2UgYSBjb25uZWN0ZWQgY2xp"
    "ZW50IHdvdWxkCiAgICAgICAgI2tlZXAgcGxheWluZyB3aXRoIGFuIGFjY291bnQgdGhhdCBubyBs"
    "b25nZXIgZXhpc3RzIGluIHRoZSBEQi4KICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAg"
    "ICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBjdXIuZXhlY3V0"
    "ZShfU1FMX3VzZXJJRCwgKHVzZXJuYW1lLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVp"
    "ZHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAg"
    "IHJldHVybiBGYWxzZQogICAgICAgICAgICB1aWQgPSB1aWRyZXNbMF0KICAgICAgICAgICAgY3Vy"
    "LmV4ZWN1dGUoX1NRTF9kZWxldGVVc2VyLCAodXNlcm5hbWUsICkpCiAgICAgICAgICAgIHNlbGYu"
    "ZGIuY29tbWl0KCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAjR3VpbGQgbWVtYmVy"
    "c2hpcCBvdXRsaXZlcyB0aGUgdXNlclRhYmxlIHJvdyBvdGhlcndpc2UsIHNvIHRoZSBkZWxldGVk"
    "CiAgICAgICAgI25hbWUgd291bGQga2VlcCBzaG93aW5nIHVwIGluIGl0cyBndWlsZCdzIHJvc3Rl"
    "ciBmb3JldmVyLgogICAgICAgIHNlbGYubGVhdmVHdWlsZCh1c2VybmFtZSkKICAgICAgICAjUGxh"
    "eWVyZGF0YSBmaWxlcyAoInt1c2VySUQ6eH1fe2Zvcm1JRDp4fS5iaW4iKSBsaXZlIG91dHNpZGUg"
    "dGhlIERCCiAgICAgICAgI3RyYW5zYWN0aW9uIGFuZCBhcmUgbG9va2VkIHVwIGJ5IHByZWZpeCAt"
    "IGJlc3QgZWZmb3J0LCBhIGxlZnRvdmVyCiAgICAgICAgI2ZpbGUgaGVyZSBpc24ndCB3b3J0aCBm"
    "YWlsaW5nIHRoZSB3aG9sZSBkZWxldGlvbiBvdmVyLgogICAgICAgIHByZWZpeCA9IGYne3VpZDp4"
    "fV8nCiAgICAgICAgdHJ5OgogICAgICAgICAgICBmb3IgZm4gaW4gb3MubGlzdGRpcihfUEFUSF9Q"
    "TEFZRVJEQVRBKToKICAgICAgICAgICAgICAgIGlmIGZuLnN0YXJ0c3dpdGgocHJlZml4KToKICAg"
    "ICAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICAgICAgICAgIG9zLnJlbW92ZShv"
    "cy5wYXRoLmpvaW4oX1BBVEhfUExBWUVSREFUQSwgZm4pKQogICAgICAgICAgICAgICAgICAgIGV4"
    "Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICAgICAgICAgICAgICBwYXNzCiAgICAgICAgZXhjZXB0"
    "IE9TRXJyb3I6CiAgICAgICAgICAgIHBhc3MKICAgICAgICByZXR1cm4gVHJ1ZQpHREggPSBEYXRh"
    "SGFuZGxlcigpCgpkZWYgX3dvVXNlcih1bCwgdXNyKToKICAgIHJldHVybiBsaXN0KCAoYSBmb3Ig"
    "YSBpbiB1bCBpZiBhIGlzIG5vdCB1c3IpICkKZGVmIF9SZWFkQmxvYihjb24sIHNpemUpOgogICAg"
    "I3NpemUgY29tZXMgc3RyYWlnaHQgb2ZmIHRoZSB3aXJlLCBzbyBpdCBpcyBuZWl0aGVyIHRydXN0"
    "ZWQgdG8gYmUgYSBudW1iZXIKICAgICNub3IgdG8gYmUgc2FuZTogYSBjbGllbnQgY2xhaW1pbmcg"
    "YSBodWdlIGxlbmd0aCB1c2VkIHRvIG1ha2UgdGhlIHNlcnZlcgogICAgI2J1ZmZlciB1bmJvdW5k"
    "ZWRseSAobWVtb3J5IGV4aGF1c3Rpb24pLCBhbmQgYSBjbGllbnQgdGhhdCBkaXNjb25uZWN0ZWQK"
    "ICAgICNtaWQtYmxvYiBtYWRlIHJlY3YoKSByZXR1cm4gYicnIGZvcmV2ZXIgLSBhIDEwMCUgQ1BV"
    "IGJ1c3ktbG9vcCwgdGhlIHNhbWUKICAgICNkZWZlY3QgYWxyZWFkeSBmaXhlZCBpbiBDb25uZWN0"
    "aW9uSGFuZGxlci5fcmVjdk1vcmUoKS4KICAgIHRyeToKICAgICAgICBzaXplID0gaW50KHNpemUp"
    "CiAgICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgcmFpc2UgUHJvdG9j"
    "b2xFcnJvcihmJ2JhZCBibG9iIHNpemUge3NpemUhcn0nKQogICAgaWYgc2l6ZSA8IDAgb3Igc2l6"
    "ZSA+IF9NQVhfQkxPQjoKICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmxvYiBzaXplIHtz"
    "aXplfSBvdXQgb2YgcmFuZ2UgKG1heCB7X01BWF9CTE9CfSknKQogICAgI0EgYmxvYiByZWFkIGJs"
    "b2NrcyB0aGlzIGNvbm5lY3Rpb24ncyBlbnRpcmUgaGFuZGxlciB0aHJlYWQuIEFubm91bmNpbmcg"
    "YQogICAgI2xlbmd0aCBhbmQgdGhlbiBnb2luZyBxdWlldCAtIGEgd2VkZ2VkIGNsaWVudCwgYSBs"
    "aW5rIHRoYXQgZHJvcHBlZAogICAgI3dpdGhvdXQgYSByZXNldCAtIHVzZWQgdG8gYmxvY2sgaXQg"
    "Zm9yZXZlcjogdGhlIHRocmVhZCBuZXZlciByZXR1cm5lZCwgc28KICAgICN0aGUgcGxheWVyJ3Mg"
    "YWNjb3VudCBzdGF5ZWQgY2xhaW1lZCBhbmQgYW55IHJvb20gdGhleSBob3N0ZWQgc3RheWVkCiAg"
    "ICAjbGlzdGVkIHdpdGggbm90aGluZyBiZWhpbmQgaXQuIFRoZSBpZGxlIHRpbWVvdXQgbmV2ZXIg"
    "YXBwbGllZCBoZXJlLAogICAgI2JlY2F1c2UgaXQgaXMgb25seSBjb25zdWx0ZWQgYnkgdGhlIHJl"
    "YWQgbG9vcCB0aGlzIGNhbGwgaGFzIHN0ZXBwZWQgb3V0CiAgICAjb2YuCiAgICBkZWFkbGluZSA9"
    "IHRpbWUubW9ub3RvbmljKCkgKyBfQkxPQl9USU1FT1VUCiAgICB3aGlsZSBsZW4oY29uLmRhdGEp"
    "IDwgc2l6ZToKICAgICAgICByZW1haW5pbmcgPSBkZWFkbGluZSAtIHRpbWUubW9ub3RvbmljKCkK"
    "ICAgICAgICBpZiByZW1haW5pbmcgPD0gMDoKICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJv"
    "cihmJ2Jsb2Igb2Yge3NpemV9IGJ5dGVzIG5vdCBkZWxpdmVyZWQgd2l0aGluICcKICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICBmJ3tfQkxPQl9USU1FT1VUfXMgKHtsZW4oY29uLmRhdGEp"
    "fSByZWNlaXZlZCknKQogICAgICAgICNzZWxlY3QoKSwgTk9UIHNldHRpbWVvdXQoKS4gQSBzb2Nr"
    "ZXQgdGltZW91dCBpcyBhIHByb3BlcnR5IG9mIHRoZQogICAgICAgICNzb2NrZXQgcmF0aGVyIHRo"
    "YW4gb2YgdGhlIGNhbGwsIHNvIHRoZSBzZXR0aW1lb3V0KCkgdGhhdCB1c2VkIHRvIGJlCiAgICAg"
    "ICAgI2hlcmUgYWxzbyBhcm1lZCB0aGUgd3JpdGVyIHRocmVhZCdzIGNvbmN1cnJlbnQgc2VuZGFs"
    "bCgpIC0gYW5kIG5vdGhpbmcKICAgICAgICAjZXZlciBkaXNhcm1lZCBpdCBhZ2Fpbiwgc28gaXQg"
    "c3RheWVkIGFybWVkIGZvciB0aGUgd2hvbGUgcmVtYWluaW5nIGxpZmUKICAgICAgICAjb2YgdGhl"
    "IGNvbm5lY3Rpb24uIEEgY2xpZW50IHdob3NlIHJlY2VpdmUgd2luZG93IGZpbGxlZCB1cCBmb3Ig"
    "YSBtb21lbnQKICAgICAgICAjKHByZWNpc2VseSB3aGF0IGhhcHBlbnMgaW4gYSBidXN5IGNvLW9w"
    "IHNlc3Npb24pIHRoZW4gbWFkZSB0aGF0CiAgICAgICAgI3NlbmRhbGwoKSByYWlzZSBUaW1lb3V0"
    "RXJyb3IgKmFmdGVyIGhhdmluZyBhbHJlYWR5IHdyaXR0ZW4gcGFydCBvZiBhCiAgICAgICAgI3Bh"
    "Y2tldCo6IHRoZSB3cml0ZXIgdGhyZWFkIGRpZWQsIHRoZSBjbGllbnQgd2FzIGxlZnQgaG9sZGlu"
    "ZyBoYWxmIGEKICAgICAgICAjbWVzc2FnZSwgYW5kIGl0cyBjb21tYW5kIHN0cmVhbSB3YXMgZGVz"
    "eW5jaHJvbmlzZWQgZnJvbSB0aGF0IHBvaW50IG9uLgogICAgICAgICNUaGUgdmlzaWJsZSByZXN1"
    "bHQgaXMgYSBmcmVlemUgb3IgYSBkcm9wIG1pbnV0ZXMgbGF0ZXIsIHdpdGggbm90aGluZyBpbgog"
    "ICAgICAgICN0aGUgbG9nIHR5aW5nIGl0IGJhY2sgdG8gdGhlIGJsb2IgdGhhdCBhcm1lZCB0aGUg"
    "dGltZW91dC4gRXZlcnkKICAgICAgICAjYmxvYi1jYXJyeWluZyBjb21tYW5kIGlzIG9uIHRoaXMg"
    "cGF0aCAtIC9zZXR1c2VyaGVyb2RhdGEsIHRoZQogICAgICAgICMvc2V0cGxheWVyZGF0YSBhdXRv"
    "c2F2ZSwgYW5kIC9nYW1lY29tbWFuZHRvdXNlciwgd2hpY2ggaXMgdGhlIHJlbGF5CiAgICAgICAg"
    "I2NhcnJ5aW5nIHRoZSBhY3R1YWwgaW4tZ2FtZSB0cmFmZmljIGJldHdlZW4gcGxheWVycy4gX2xv"
    "YmJ5SGFuZGxlCiAgICAgICAgI2FscmVhZHkgZG9jdW1lbnRzIHRoaXMgc2FtZSB0cmFwIGZvciB0"
    "aGUgcmVhZCBsb29wOyB0aGUgbG9vcCBiZWxvdwogICAgICAgICNzaW1wbHkgbGVhdmVzIHRoZSBz"
    "b2NrZXQgYmxvY2tpbmcgYW5kIHdhaXRzIHdpdGggc2VsZWN0KCkgaW5zdGVhZC4KICAgICAgICBy"
    "ZWFkeSwgXywgXyA9IHNlbGVjdC5zZWxlY3QoW2Nvbi5yZXF1ZXN0XSwgW10sIFtdLCByZW1haW5p"
    "bmcpCiAgICAgICAgaWYgbm90IHJlYWR5OgogICAgICAgICAgICBjb250aW51ZSAjZGVhZGxpbmUg"
    "aXMgcmUtY2hlY2tlZCBhdCB0aGUgdG9wIG9mIHRoZSBsb29wCiAgICAgICAgY2h1bmsgPSBjb24u"
    "cmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAg"
    "ICAgIHJhaXNlIENvbm5lY3Rpb25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGJsb2Ig"
    "cmVhZCcpCiAgICAgICAgY29uLmRhdGEgKz0gY2h1bmsKICAgIGJsYnVmID0gY29uLmRhdGFbMDpz"
    "aXplXQogICAgY29uLmRhdGEgPSBjb24uZGF0YVtzaXplOl0KICAgIHJldHVybiBibGJ1ZgoKI0Nv"
    "bW1hbmQgZnVuY3Rpb25zCmRlZiBfbm9wKG1kLHVzcixyZXMpOgogICAgcmV0dXJuIE5vbmUKZGVm"
    "IF91cGRoZXJvcG9zKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVs"
    "OgogICAgICAgIHJldHVybiBOb25lICNub3QgaW4gYSBnYW1lIGNoYW5uZWwsIGlnbm9yZQogICAg"
    "IyAieHh4eCN5eXl5IiByZXNwICJVSUQjeHh4eCN5eXl5IiAtIHRoZSBjbGllbnQgc2VuZHMgZWl0"
    "aGVyIGZvcm0sIGJ1dAogICAgIyB1cGRhdGVQb3MoKSB1bmNvbmRpdGlvbmFsbHkgcHJlZml4ZXMg"
    "dGhlIHNlbmRlcidzIGlkIHdoZW4gaXQgZmFucyB0aGUKICAgICMgcG9zaXRpb24gb3V0LiBTdG9y"
    "aW5nIHRoZSByYXcgZmllbGQgbWVhbnQgdGhlIHNlY29uZCBmb3JtIHdlbnQgYmFjayBvdXQKICAg"
    "ICMgYXMgIlVJRCNVSUQjeHh4eCN5eXl5Iiwgd2hpY2ggbm8gY2xpZW50IGNhbiBtYXRjaCB0byBh"
    "IHBsYXllcjogdGhhdAogICAgIyBoZXJvJ3MgbWFya2VyIHRoZW4gc3RheWVkIHdoZXJldmVyIGl0"
    "IHdhcyBsYXN0IHN1Y2Nlc3NmdWxseSBwYXJzZWQgd2hpbGUKICAgICMgdGhlIHBsYXllciBhY3R1"
    "YWxseSB3YWxrZWQgYXdheS4gS2VlcCBvbmx5IHRoZSB0cmFpbGluZyBjb29yZGluYXRlIHBhaXIK"
    "ICAgICMgc28gZXhhY3RseSBvbmUgaWQgaXMgcHJlc2VudCBvbiB0aGUgd2lyZSByZWdhcmRsZXNz"
    "IG9mIHdoYXQgd2FzIHNlbnQuCiAgICB1c3IudXNlci5wb3NkYXRhID0gJyMnLmpvaW4ocmVzWzFd"
    "LnNwbGl0KCcjJylbLTI6XSkKICAgIHVzci51c2VyLmdhbWVjaGFubmVsLmRpcnR5ID0gVHJ1ZQog"
    "ICAgdXNyLnVzZXIucG9zY2hhbmdlZCA9IFRydWUKICAgIHJldHVybiBOb25lICNubyByZXNwb25z"
    "ZQpkZWYgX3NldHBsYXllcmRhdGEobWQsdXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1c3Is"
    "IHJlc1szXSkKICAgICNUT0RPIENIRUNLIHBlcm1pc3Npb25zIGZvciBzZXREYXRhKHNlbGYgb3Ig"
    "b3RoZXIpCiAgICBpZiByZXNbMV0gPT0gdXNyLnVzZXIubmFtZToKICAgICAgICBHREguc2V0UGxh"
    "eWVyRGF0YShyZXNbMV0sIHJlc1syXSwgcGQpCiAgICAjVE9ETyBoYW5kbGUgcmVtYWluaW5nIHZh"
    "bHVlcwogICAgI3Jlc1t4XToKICAgICMwOiAvc2V0cGxheWVyZGF0YQogICAgIzE6IG5hbWUKICAg"
    "ICMyOiBmb3JtCiAgICAjMzogYmxvYnNpemUKICAgICM0OiB1bmtub3duIChwb2ludHM/KQogICAg"
    "IzU6IHVua25vd24sIDEgKGJvb2w/KQogICAgcmV0dXJuIE5vbmUKZGVmIF9nZXRwbGF5ZXJkYXRh"
    "KG1kLHVzcixyZXMpOgogICAgI1RPRE8gY2hlY2sgcGVybWlzc2lvbiBmb3IgZ2V0RGF0YShzZWxm"
    "IG9yIG90aGVyKQogICAgaWYgcmVzWzFdID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgcGQgPSBH"
    "REguZ2V0UGxheWVyRGF0YShyZXNbMV0sIHJlc1syXSkKICAgICAgICAjcHJpbnQoJ09idGFpbmVk"
    "IFBsYXllcmRhdGEnLCBsZW4ocGQpKQogICAgICAgIHJldHVybiBfZW0oZicvZ2V0cGxheWVyZGF0"
    "YSAie3Jlc1sxXX0iICJ7cmVzWzJdfSIge2xlbihwZCl9JykrcGQKICAgICNwcmludCgnQWNjZXNz"
    "IEVycm9yJyx1c3IudXNlci5uYW1lLCAnQ2FuXCd0IGdldCBwbGF5ZXJkYXRhIGZvcicscmVzWzFd"
    "KQogICAgcmV0dXJuIE5vbmUKZGVmIF9sZWF2ZWdhbWVjaGFubmVsKG1kLHVzcixyZXMpOgogICAg"
    "Y2hubCA9IHVzci51c2VyLmdhbWVjaGFubmVsCiAgICBpZiBjaG5sOgogICAgICAgIGNobmwubGVh"
    "dmVDaGFubmVsKHVzcikKICAgIHJldHVybiB1c3Iuc2VydmVyLnN0YXRlLmVudW1lcmF0ZUdDKCkK"
    "Iy0tLSBjb21tYW5kcyB0YWtlbiBmcm9tIHRoZSBjbGllbnQncyBvd24gb3V0Z29pbmcgdGFibGUg"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KI1RoZSBmaXZlIGhhbmRsZXJzIGJlbG93IGV4aXN0IGJl"
    "Y2F1c2UgdGhlIGZvcm1hdCB0YWJsZSBjb21waWxlZCBpbnRvIHRoZSByZXRhaWwKI2NsaWVudCAo"
    "RU5DbGllbnQuY3BwLCByZWNvdmVyZWQgZnJvbSBHYW1lSGVscGVyLmRsbCBpbiB0aGUgMS4zIFNE"
    "SykgbGlzdHMgdGhlbQojYW5kIHRoaXMgc2VydmVyIGhhZCBubyBlbnRyeSBmb3IgYW55IG9mIHRo"
    "ZW0uIEFuIHVucmVnaXN0ZXJlZCBjb21tYW5kIGlzIG5vdAojaWdub3JlZCBncmFjZWZ1bGx5OiBw"
    "YXJzZSgpIGxvZ3MgJ1VOS05PV04gQ09NTUFORCcgYW5kIHJldHVybnMgbm90aGluZywgYW5kIGEK"
    "I2NsaWVudCB3YWl0aW5nIG9uIGFuIGFuc3dlciB3YWl0cyBmb3JldmVyLiBUaGF0IGlzIHRoZSBz"
    "YW1lIHNoYXBlIGFzIGV2ZXJ5IGhhbmcKI2FscmVhZHkgdHJhY2tlZCBkb3duIGluIHRoaXMgZmls"
    "ZS4KI1RoZSBjbGllbnQgc2VuZHMsIHZlcmJhdGltIGZyb20gdGhhdCB0YWJsZToKIyAgICAvZ2Ft"
    "ZWNoYW5uZWxzbGlzdAojICAgIC9qb2luY2hhdGNoYW5uZWwgIiVTIiAiJVMiICIlZCIKIyAgICAv"
    "bXNnICIuLi4KIyAgICAvc2V0Z2FtZXBhcmFtcyAiJXMiICIlcyIKIyAgICAvbmV3Z2FtZWhvc3Qg"
    "IiVzIgpkZWYgX2dhbWVjaGFubmVsc2xpc3QobWQsdXNyLHJlcyk6CiAgICAjUGxhaW4gIndoYXQg"
    "dG93bnMgYXJlIHRoZXJlPyIuIGVudW1lcmF0ZUdDKCkgYWxyZWFkeSBidWlsZHMgZXhhY3RseSB0"
    "aGlzCiAgICAjYW5zd2VyIC0gaXQgd2FzIG9ubHkgZXZlciBzZW50IGFzIHRoZSByZXBseSB0byAv"
    "bGVhdmVnYW1lY2hhbm5lbCwgc28gYQogICAgI2NsaWVudCB0aGF0IGFza2VkIGRpcmVjdGx5IGdv"
    "dCBzaWxlbmNlIGFuZCBhbiBlbXB0eSB0b3duIGxpc3QuCiAgICByZXR1cm4gdXNyLnNlcnZlci5z"
    "dGF0ZS5lbnVtZXJhdGVHQygpCmRlZiBfam9pbmNoYXRjaGFubmVsKG1kLHVzcixyZXMpOgogICAg"
    "IyhjaGFubmVsLCBwYXNzd29yZCwgZmxhZykuIGpvaW5DaGF0KCkgYWxyZWFkeSByZXR1cm5zIHRo"
    "ZSBmdWxsIHJlcGx5IHRoZQogICAgI2NsaWVudCBleHBlY3RzIC0gdGhlIGpvaW4gY29uZmlybWF0"
    "aW9uIHBsdXMgdGhlIHJvc3RlciAtIGFuZCB3YXMgb25seQogICAgI3JlYWNoYWJsZSBhcyBhIHNp"
    "ZGUgZWZmZWN0IG9mIGVudGVyaW5nIGEgdG93biwgc28gdGhlIHNlY29uZCBjaGF0IGNoYW5uZWwK"
    "ICAgICMoVHJhZGUpIGNvdWxkIG5ldmVyIGJlIGpvaW5lZDogdGhlIGNvbW1hbmQgdG8gc3dpdGNo"
    "IHdhcyB1bmhhbmRsZWQuCiAgICAjVGhlIHBhc3N3b3JkIGlzIGFjY2VwdGVkIGFuZCBpZ25vcmVk"
    "LCBhcyBldmVyeXdoZXJlIGVsc2UgaW4gdGhpcyBmaWxlOyB0aGUKICAgICN0cmFpbGluZyBpbnRl"
    "Z2VyJ3MgbWVhbmluZyBpcyBub3Qga25vd24gYW5kIG5vdGhpbmcgaGVyZSBkZXBlbmRzIG9uIGl0"
    "LgogICAgY2hubCA9IHVzci51c2VyLmdhbWVjaGFubmVsCiAgICBpZiBub3QgY2hubDoKICAgICAg"
    "ICByZXR1cm4gTm9uZSAjbm90IGluIGEgdG93biwgbm90aGluZyB0byBqb2luCiAgICBuYW1lID0g"
    "c2FuaXRpemVUZXh0KHJlc1sxXSwgX01BWF9DSEFUTkFNRSkuc3RyaXAoKQogICAgaWYgbm90IG5h"
    "bWU6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIG5hbWUgbm90IGluIGNobmwuY2hhdENoYW5u"
    "ZWxzOgogICAgICAgICNUaGUgY2xpZW50IGhhcyBhICJjcmVhdGUgY2hhdCBjaGFubmVsIiBjb250"
    "cm9sIG9mIGl0cyBvd24KICAgICAgICAjKElEQ19DUkVBVEVDSEFUQ0hBTk5FTCBpbiB0aGUgU0RL"
    "J3MgRGlhbG9nc1Jlc291cmNlLmgpIGFuZCBubyBzZXBhcmF0ZQogICAgICAgICNjb21tYW5kIGZv"
    "ciBpdCwgc28gam9pbmluZyBhIG5hbWUgdGhhdCBkb2VzIG5vdCBleGlzdCB5ZXQgKmlzKiBob3cg"
    "YQogICAgICAgICNjaGFubmVsIGdldHMgY3JlYXRlZC4gUmVmdXNpbmcgbGVmdCB0aGF0IGJ1dHRv"
    "biBkb2luZyBub3RoaW5nIGJ1dCBoYW5nCiAgICAgICAgI3RoZSBkaWFsb2cuIENhcHBlZCwgYmVj"
    "YXVzZSB0aGUgbmFtZSBpcyBwbGF5ZXItc3VwcGxpZWQgYW5kIHRoZXNlCiAgICAgICAgI291dGxp"
    "dmUgdGhlIHBsYXllciB3aG8gbWFkZSB0aGVtLgogICAgICAgIGlmIGxlbihjaG5sLmNoYXRDaGFu"
    "bmVscykgPj0gX01BWF9DSEFUX0NIQU5ORUxTOgogICAgICAgICAgICBwcmludChmJyoqKiB7dXNy"
    "LnVzZXIubmFtZX0gY291bGQgbm90IGNyZWF0ZSBjaGF0IGNoYW5uZWwge25hbWUhcn06ICcKICAg"
    "ICAgICAgICAgICAgICAgZid0b3duIGFscmVhZHkgaGFzIHtsZW4oY2hubC5jaGF0Q2hhbm5lbHMp"
    "fScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgY2hubC5jaGF0Q2hhbm5lbHNbbmFt"
    "ZV0gPSBbXQogICAgICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gY3JlYXRlZCBj"
    "aGF0IGNoYW5uZWwgIntuYW1lfSIgaW4ge2NobmwubmFtZX0nKQogICAgICAgICNFdmVyeW9uZSBi"
    "cm93c2luZyB0aGUgdG93biBnZXRzIHRoZSByZWZyZXNoZWQgY2hhbm5lbCBsaXN0LCBvdGhlcndp"
    "c2UKICAgICAgICAjdGhlIG5ldyBjaGFubmVsIGlzIGludmlzaWJsZSB0byBhbGwgYnV0IGl0cyBj"
    "cmVhdG9yLgogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6bGlzdChjaG5sLnVzZXJsaXN0KSwnbWVz"
    "c2FnZSc6Y2hubC5lbnVtQ2hhdHMoKX0pCiAgICByZXR1cm4gY2hubC5qb2luQ2hhdCh1c3IsIG5h"
    "bWUsIHJlc1syXSBpZiBsZW4ocmVzKT4yIGVsc2UgJycpCmRlZiBfbXNnKG1kLHVzcixyZXMpOgog"
    "ICAgI1ByaXZhdGUgbWVzc2FnZS4gUmVsYXllZCBpbiB0aGUgc2FtZSBzaGFwZSAvc2VuZCB1c2Vz"
    "IC0gIjxzZW5kZXI+IiB0aGVuIHRoZQogICAgI3RleHQgLSBiZWNhdXNlIHRoYXQgaXMgdGhlIG9u"
    "ZSB0d28tZmllbGQgdGV4dCBtZXNzYWdlIHRoaXMgY2xpZW50IGlzIGtub3duCiAgICAjdG8gcmVu"
    "ZGVyLiBUaGUgZXhhY3Qgc2VydmVyLT5jbGllbnQgc3BlbGxpbmcgZm9yIGEgcHJpdmF0ZSBtZXNz"
    "YWdlIGhhcyBub3QKICAgICNiZWVuIGNhcHR1cmVkOyBpZiBhIHNlc3Npb24gbG9nIGV2ZXIgc2hv"
    "d3MgdGhlIGNsaWVudCBtaXNoYW5kbGluZyBpdCwgdGhpcwogICAgI2lzIHRoZSBsaW5lIHRvIHJl"
    "dmlzaXQuIERvaW5nIG5vdGhpbmcgd2FzIG5vdCB0aGUgc2FmZXIgb3B0aW9uOiBpdCBpcyB3aGF0"
    "CiAgICAjdGhlIHNlcnZlciBkaWQgdW50aWwgbm93LCBhbmQgcHJpdmF0ZSBtZXNzYWdlcyBzaW1w"
    "bHkgdmFuaXNoZWQuCiAgICBpZiBsZW4ocmVzKTwzOgogICAgICAgIHJldHVybiBOb25lCiAgICB0"
    "YXJnZXQgPSByZXNbMV0KICAgIHRleHQgPSBzYW5pdGl6ZVRleHQocmVzWzJdLCBfTUFYX0NIQVRf"
    "VEVYVCkKICAgIGlmIG5vdCB0ZXh0OgogICAgICAgIHJldHVybiBOb25lCiAgICB0Y29uID0gdXNy"
    "LnNlcnZlci5nZXRQbGF5ZXIodGFyZ2V0KQogICAgaWYgdGNvbiBpcyBOb25lOgogICAgICAgIHJl"
    "dHVybiBOb25lICNyZWNpcGllbnQgb2ZmbGluZQogICAgdGNvbi5zZW5kKF9lbShmJy9tc2cgInt1"
    "c3IudXNlci5uYW1lfSIgInt0ZXh0fSInKSkKICAgIHJldHVybiBOb25lCmRlZiBfc2V0Z2FtZXBh"
    "cmFtcyhtZCx1c3IscmVzKToKICAgICNUd28gc3RyaW5ncyB3aG9zZSBtZWFuaW5nIGlzIG5vdCBk"
    "b2N1bWVudGVkIGFueXdoZXJlIGF2YWlsYWJsZSwgc28gbm90aGluZwogICAgI2lzICpjaGFuZ2Vk"
    "KiBvbiB0aGUgc3RyZW5ndGggb2YgYSBndWVzcyAtIHRoZSByb29tJ3Mgc3RvcmVkIHBhcmFtZXRl"
    "cnMgYXJlCiAgICAjbGVmdCBleGFjdGx5IGFzIGl0cyAvY3JlYXRlZ2FtZSBzZXQgdGhlbS4gV2hh"
    "dCB0aGlzIGRvZXMgYnV5IGlzIHRoYXQgdGhlCiAgICAjY29tbWFuZCBzdG9wcyBiZWluZyBhbiB1"
    "bmtub3duIG9uZSwgYW5kIGV2ZXJ5b25lIGJyb3dzaW5nIGdldHMgYSByZWZyZXNoZWQKICAgICMk"
    "Z2FtZSBlbnRyeSwgd2hpY2ggaXMgYSBtZXNzYWdlIHRoZSBjbGllbnQgYWxyZWFkeSBoYW5kbGVz"
    "LiBUaGUgcmF3CiAgICAjYXJndW1lbnRzIGFyZSBsb2dnZWQgc28gYSByZWFsIHNlc3Npb24gY2Fu"
    "IHNldHRsZSB3aGF0IHRoZXkgbWVhbi4KICAgIGdtID0gdXNyLnVzZXIuZ2FtZQogICAgaWYgZ20g"
    "aXMgTm9uZSBvciBnbS5ob3N0IGlzIG5vdCB1c3I6CiAgICAgICAgcmV0dXJuIE5vbmUgI29ubHkg"
    "dGhlIHJvb20ncyBvd24gaG9zdCBtYXkgdG91Y2ggaXRzIHBhcmFtZXRlcnMKICAgIHByaW50KGYn"
    "W0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gL3NldGdhbWVwYXJhbXMgZm9yICJ7Z20uZ25hbWV9Ijog"
    "JwogICAgICAgICAgZid7cmVzWzFdIXJ9IHtyZXNbMl0hcn0gKHJlY29yZGVkLCBub3QgYXBwbGll"
    "ZCknKQogICAgbXNnID0gZ20uZ2V0R2FtZVN0cmluZygpCiAgICBpZiBtc2c6CiAgICAgICAgbWQu"
    "YWRkKHsndGFyZ2V0JzpnbS5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBO"
    "b25lCmRlZiBfbmV3Z2FtZWhvc3QobWQsdXNyLHJlcyk6CiAgICAjQSBmcmVzaCB4LWRpcmVjdHBs"
    "YXkgVVJMIGZvciBhIHJvb20gdGhhdCBhbHJlYWR5IGV4aXN0cy4gSXQgY2FycmllcyB0aGUKICAg"
    "ICNob3N0J3Mgb3duIGlkZWEgb2YgaXRzIGFkZHJlc3MsIHdoaWNoIGJlaGluZCBhIHJvdXRlciBp"
    "cyBhIExBTiBhZGRyZXNzIG5vCiAgICAjam9pbmVyIGNhbiByZWFjaCAtIHRoZSBzYW1lIHByb2Js"
    "ZW0gL2NyZWF0ZWdhbWUgaGFzLCBhbmQgaXQgbXVzdCBnZXQgdGhlCiAgICAjc2FtZSB0cmVhdG1l"
    "bnQsIG9yIGEgcm9vbSB3aG9zZSBob3N0IHJlLWFkdmVydGlzZXMgc2lsZW50bHkgYmVjb21lcwog"
    "ICAgI3Vuam9pbmFibGUgd2hpbGUgc3RpbGwgYmVpbmcgbGlzdGVkLgogICAgZ20gPSB1c3IudXNl"
    "ci5nYW1lCiAgICBpZiBnbSBpcyBOb25lIG9yIGdtLmhvc3QgaXMgbm90IHVzcjoKICAgICAgICBy"
    "ZXR1cm4gTm9uZSAjb25seSB0aGUgaG9zdCBkZXNjcmliZXMgd2hlcmUgdGhlIGdhbWUgaXMKICAg"
    "IHBlZXIgPSB1c3IuY2xpZW50X2FkZHJlc3NbMF0gaWYgdXNyLmNsaWVudF9hZGRyZXNzIGVsc2Ug"
    "JycKICAgICh1cmwsIG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0KHJlc1sxXSwgcGVlcikKICAgIGdt"
    "LnVybCA9IHVybAogICAgcHJpbnQoZidbTG9iYnldIHt1c3IudXNlci5uYW1lfSBtb3ZlZCByb29t"
    "ICJ7Z20uZ25hbWV9Ijoge25vdGV9JykKICAgIHByaW50KGYnW0xvYmJ5XSAgIHVybCBhZHZlcnRp"
    "c2VkIHRvIGpvaW5lcnM6IHtnbS51cmx9JykKICAgIG1zZyA9IGdtLmdldEdhbWVTdHJpbmcoKQog"
    "ICAgaWYgbXNnOgogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6Z20uX2F1ZGllbmNlKCksJ21lc3Nh"
    "Z2UnOm1zZ30pCiAgICByZXR1cm4gTm9uZQpkZWYgX3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwobWQs"
    "dXNyLHJlcyk6CiAgICBjaG5sID0gdXNyLnNlcnZlci5zdGF0ZS5nYW1lQ2hhbm5lbHMuZ2V0KHJl"
    "c1sxXSkKICAgIGlmIGNobmwgaXMgTm9uZToKICAgICAgICByZXR1cm4gX2VtKGYnL3JlcXVlc3Rq"
    "b2luZ2FtZWNoYW5uZWwgIntyZXNbMV19IiAiMCInKSAjdW5rbm93biBjaGFubmVsCiAgICAjVE9E"
    "TyBjaGVjayBwZXJtaXNzaW9ucz8KICAgIGlmIGNobmwucmVxdWVzdEpvaW4odXNyKToKICAgICAg"
    "ICByZXR1cm4gX2VtKGYnL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgIntyZXNbMV19IiAiMSInKQog"
    "ICAgcmV0dXJuIF9lbShmJy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIgIjAiJykK"
    "ZGVmIF9qb2luZ2FtZWNoYW5uZWwobWQsdXNyLHJlcyk6CiAgICBjaG5sID0gdXNyLnNlcnZlci5z"
    "dGF0ZS5nYW1lQ2hhbm5lbHMuZ2V0KHJlc1sxXSkKICAgIGlmIGNobmwgaXMgTm9uZToKICAgICAg"
    "ICByZXR1cm4gTm9uZSAjdW5rbm93biBjaGFubmVsLCBpZ25vcmUKICAgIGlmIGxlbihyZXMpPjI6"
    "CiAgICAgICAgdXNyLnVzZXIucG9zZGF0YSA9ICcjJy5qb2luKHJlc1syXS5zcGxpdCgnIycpWy0y"
    "Ol0pCiAgICByZXR1cm4gY2hubC5qb2luQ2hhbm5lbCh1c3IsIHJlc1sxXSkKZGVmIF9zZXR1c2Vy"
    "aGVyb2RhdGEobWQsdXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1c3IsIHJlc1syXSkKICAg"
    "IHVzci51c2VyLmhlcm9kYXRhID0gcGQKICAgIGlmIHVzci51c2VyLmdhbWVjaGFubmVsOgogICAg"
    "ICAgIG1zZyA9IHVzci51c2VyLmdldEdDVW1zZygpCiAgICAgICAgdGcgPSBfd29Vc2VyKHVzci51"
    "c2VyLmdhbWVjaGFubmVsLnVzZXJsaXN0LCB1c3IpCiAgICAgICAgbWQuYWRkKHsndGFyZ2V0Jzp0"
    "ZywnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBOb25lCmRlZiBfc2VuZChtZCx1c3IscmVzKToK"
    "ICAgICNUT0RPIGNvbnNpZGVyIHNwZWNpYWwgY2hhdCBjb21tYW5kcyBoZXJlCiAgICBpZiBub3Qg"
    "dXNyLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIGxlbihyZXMp"
    "PDI6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHRleHQgPSBzYW5pdGl6ZVRleHQocmVzWzFdLCBf"
    "TUFYX0NIQVRfVEVYVCkKICAgIGlmIG5vdCB0ZXh0OgogICAgICAgIHJldHVybiBOb25lCiAgICB1"
    "bCA9IHVzci51c2VyLmNoYXRjaGFubmVsCiAgICBtZC5hZGQoeyd0YXJnZXQnOnVsLCdtZXNzYWdl"
    "JzpfZW0oZicvc2VuZCAie3Vzci51c2VyLm5hbWV9IiAie3RleHR9IicpfSkKICAgIHJldHVybiBO"
    "b25lCmRlZiBfZ2V0Z3VpbGRyYW5rcG9pbnRzKG1kLHVzcixyZXMpOgogICAgKGEsYixjLGQpID0g"
    "X2dycCgpCiAgICByZXR1cm4gX2VtKGYnL2dldGd1aWxkcmFua3BvaW50cyAie2F9IiAie2J9IiAi"
    "e2N9IiAie2R9IicpCgojIyBHVUlMRFMKI0d1aWxkIGNyZWF0aW9uIGRpZCBub3RoaW5nIGF0IGFs"
    "bCBiZWZvcmUgdGhpczogdGhlcmUgd2FzIG5vIC9jcmVhdGVndWlsZCAob3IKI2FueXRoaW5nIGVs"
    "c2UgZ3VpbGQtcmVsYXRlZCkgaW4gX0NPTU1BTkRTLCBzbyB0aGUgY2xpZW50J3MgcmVxdWVzdCBm"
    "ZWxsCiN0aHJvdWdoIHRvIHRoZSAiVW5rbm93biBDb21tYW5kIiBicmFuY2ggb2YgQ29tbWFuZFBh"
    "cnNlci5wYXJzZSBhbmQgd2FzCiNkcm9wcGVkLiBUaGUgY2xpZW50IGdvdCBubyByZXBseSwgbm8g"
    "ZXJyb3IsIGFuZCBubyBndWlsZC4KI05PVEUgT04gQ09NTUFORCBOQU1FUzogdGhlIGV4YWN0IHdp"
    "cmUgbmFtZXMgdGhlIHJldGFpbCBjbGllbnQgdXNlcyBmb3IgdGhlCiNndWlsZCBVSSBhcmUgbm90"
    "IGRvY3VtZW50ZWQgYW55d2hlcmUgd2UgaGF2ZS4gVGhlIGhhbmRsZXJzIGJlbG93IGFyZQojcmVn"
    "aXN0ZXJlZCB1bmRlciBldmVyeSBzcGVsbGluZyB0aGF0IGZpdHMgdGhpcyBwcm90b2NvbCdzIGNv"
    "bnZlbnRpb25zLCBhbGwKI3JvdXRlZCB0byB0aGUgc2FtZSBpbXBsZW1lbnRhdGlvbiwgc28gd2hp"
    "Y2hldmVyIG9uZSB0aGUgY2xpZW50IGFjdHVhbGx5CiNzZW5kcyBpcyBzZXJ2ZWQuIHBhcnNlKCkg"
    "bm93IGxvZ3MgdGhlIHJhdyB0ZXh0IG9mIGFueXRoaW5nIHN0aWxsIHVubWF0Y2hlZCwKI3doaWNo"
    "IGlzIGhvdyB0byBjb25maXJtL3RyaW0gdGhpcyBsaXN0IGZyb20gYSByZWFsIHNlc3Npb24ncyBs"
    "b2cuCmRlZiBfdGVzdGNyZWF0ZWd1aWxkKG1kLHVzcixyZXMpOgogICAgI0NvbmZpcm1lZCBmcm9t"
    "IGEgbGl2ZSBjbGllbnQgY2FwdHVyZTogb3BlbmluZyB0aGUgZ3VpbGQgc2NyZWVuIHNlbmRzCiAg"
    "ICAjL2d1aWxkc2xhZGRlciwgYW5kIHR5cGluZyBhIG5hbWUgYW5kIHByZXNzaW5nIGNyZWF0ZSBz"
    "ZW5kcwogICAgIy90ZXN0Y3JlYXRlZ3VpbGQgIjxuYW1lPiIuIFRoZSBjbGllbnQgdGhlbiB3YWl0"
    "cyBmb3IgdGhlIHNlcnZlciB0byBzYXkKICAgICN3aGV0aGVyIHRoYXQgbmFtZSBjYW4gYmUgdXNl"
    "ZCAtIHdpdGggbm8gYW5zd2VyIGl0IHdhaXRzIGZvcmV2ZXIsIHdoaWNoIGlzCiAgICAjd2hhdCB0"
    "aGUgImd1aWxkIGNyZWF0aW9uIGhhbmdzIiByZXBvcnQgd2FzLiBFdmVyeSBndWlsZCBjb21tYW5k"
    "IG5hbWUKICAgICNndWVzc2VkIGJlZm9yZSB0aGlzIGNhcHR1cmUgKCAvY3JlYXRlZ3VpbGQsIC9q"
    "b2luZ3VpbGQsIC4uLiApIHdhcyB3cm9uZzsKICAgICN0aGlzIG9uZSBjb21lcyBmcm9tIHRoZSB3"
    "aXJlLgogICAgbmFtZSA9IHNhbml0aXplVGV4dChyZXNbMV0pLnN0cmlwKCkKICAgIGZyZWUgPSAx"
    "IGlmIEdESC5ndWlsZE5hbWVGcmVlKG5hbWUpIGVsc2UgMAogICAgcHJpbnQoZidbTG9iYnldIHt1"
    "c3IudXNlci5uYW1lfSBjaGVja2VkIGd1aWxkIG5hbWUgIntuYW1lfSI6ICcKICAgICAgICAgIGYn"
    "eyJhdmFpbGFibGUiIGlmIGZyZWUgZWxzZSAicmVqZWN0ZWQifScpCiAgICAjRWNoby1wbHVzLWZs"
    "YWcsIHRoZSBzYW1lIHNoYXBlIHRoZSBjbGllbnQgYWxyZWFkeSBhY2NlcHRzIGZyb20KICAgICMv"
    "cmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAoIjEiIGdvIGFoZWFkIC8gIjAiIG5vKS4KICAgIHJldHVy"
    "biBfZW0oZicvdGVzdGNyZWF0ZWd1aWxkICJ7bmFtZX0iICJ7ZnJlZX0iJykKZGVmIF9ndWlsZHNs"
    "YWRkZXIobWQsdXNyLHJlcyk6CiAgICAjU2VudCB3aGVuIHRoZSBndWlsZCBzY3JlZW4gb3BlbnMu"
    "IFRoZSBsYXlvdXQgb2YgYW4gaW5kaXZpZHVhbCBsYWRkZXIKICAgICNlbnRyeSBpcyBub3Qga25v"
    "d24sIGFuZCB0aGlzIGNsaWVudCBpcyBmcmFnaWxlIGVub3VnaCB0aGF0IGludmVudGluZyBvbmUK"
    "ICAgICNyaXNrcyB0YWtpbmcgaXQgZG93biAtIHNvIHRoZSBhbnN3ZXIgaXMgYW4gaG9uZXN0IGVt"
    "cHR5IGxhZGRlciwgd2hpY2ggaXMKICAgICNhbHNvIHRoZSB0cnV0aGZ1bCBvbmUgdW50aWwgZ3Vp"
    "bGRzIGNhbiBhY3R1YWxseSBiZSBjcmVhdGVkLiBUaGUgY291bnQKICAgICNjb21lcyBsYXN0LCBt"
    "YXRjaGluZyAvam9pbmdhbWVjaGFubmVsJ3MgZWNoby1wbHVzLWNvdW50IHJlcGx5LgogICAgcGFn"
    "ZSA9IHNhbml0aXplVGV4dChyZXNbMV0pIGlmIGxlbihyZXMpID4gMSBlbHNlICcxJwogICAgcmV0"
    "dXJuIF9lbShmJy9ndWlsZHNsYWRkZXIgIntwYWdlfSIgIjAiJykKZGVmIF9sYWRkZXIobWQsdXNy"
    "LHJlcyk6CiAgICAjU2VlbiBvbmNlIG9uIHRoZSB3aXJlLCByaWdodCBhZnRlciBhIHN1Y2Nlc3Nm"
    "dWwgL2pvaW5ndWlsZCwgd2l0aCBubwogICAgI2FyZ3VtZW50cyBjYXB0dXJlZCAtIHByb2JhYmx5"
    "IGEgc2VydmVyLXdpZGUgbGVhZGVyYm9hcmQgcmF0aGVyIHRoYW4gYQogICAgI2d1aWxkIG9uZS4g"
    "SXRzIHJlcGx5IHNoYXBlIGlzIG5vdCBrbm93bi4gRXZlcnkgb3RoZXIgY29tbWFuZCBpbiB0aGlz"
    "CiAgICAjZmlsZSB0aGF0IHJlYWNoZWQgdGhpcyBzdGF0ZSB3YXMgYW5zd2VyZWQgYnkgbWF0Y2hp"
    "bmcgYSBzaGFwZSB0aGUgY2xpZW50CiAgICAjaGFkIGFscmVhZHkgYmVlbiBzZWVuIGFjY2VwdGlu"
    "ZyBlbHNld2hlcmUgKGVjaG8rZmxhZywgZWNobytjb3VudCk7IHRoZXJlCiAgICAjaXMgbm8gc3Vj"
    "aCBwcmVjZWRlbnQgZm9yIHRoaXMgb25lLiBHdWVzc2luZyBhIGZpZWxkIGxheW91dCByaXNrcyBm"
    "ZWVkaW5nCiAgICAjdGhpcyBjbGllbnQgZGF0YSBpdCBkb2VzIG5vdCBleHBlY3QsIGFuZCBpdCBo"
    "YXMgYWxyZWFkeSBzaG93biBpdHNlbGYKICAgICN3aWxsaW5nIHRvIGNyYXNoIG9uIGJhZCBpbnB1"
    "dCByYXRoZXIgdGhhbiByZWplY3QgaXQgZ3JhY2VmdWxseSAtIGEgd29yc2UKICAgICNvdXRjb21l"
    "IHRoYW4gYSBVSSBlbGVtZW50IHRoYXQgc3RheXMgZW1wdHkuIFJlZ2lzdGVyZWQgc28gaXQgc3Rv"
    "cHMKICAgICNzaG93aW5nIHVwIGFzIGFuIHVua25vd24gY29tbWFuZDsgZGVsaWJlcmF0ZWx5IGFu"
    "c3dlcmVkIHdpdGggbm90aGluZwogICAgI3VudGlsIGEgY2FwdHVyZSBzaG93cyB3aGF0IHJlcGx5"
    "IGl0IGFjdHVhbGx5IHdhaXRzIGZvci4KICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFt"
    "ZX0gc2VudCAvbGFkZGVyIHtyZXNbMTpdIXJ9IC0gbm90IGFuc3dlcmVkLCAnCiAgICAgICAgIGYn"
    "c2hhcGUgdW5rbm93biAoc2VlIGNvbW1lbnQgYWJvdmUgX2xhZGRlciknKQogICAgcmV0dXJuIE5v"
    "bmUKZGVmIF9qb2luZ3VpbGQobWQsdXNyLHJlcyk6CiAgICAjQ2FwdHVyZWQgZnJvbSB0aGUgcmV0"
    "YWlsIGNsaWVudDogYWZ0ZXIgL3Rlc3RjcmVhdGVndWlsZCBhbnN3ZXJzIHRoYXQgYQogICAgI25h"
    "bWUgaXMgZnJlZSwgdGhlIGNsaWVudCBjcmVhdGVzIHRoZSBndWlsZCBieSBzZW5kaW5nCiAgICAj"
    "L2pvaW5ndWlsZCAiPG5hbWU+IiAiMSIgIjEiLiBTbyB0aGlzIG9uZSBjb21tYW5kIGNvdmVycyBi"
    "b3RoIGNyZWF0aW5nIGFuZAogICAgI2pvaW5pbmcsIGFuZCB3aGljaCBpdCBpcyBmb2xsb3dzIGZy"
    "b20gd2hldGhlciB0aGUgZ3VpbGQgYWxyZWFkeSBleGlzdHMgLQogICAgI3RoZSB0cmFpbGluZyBm"
    "bGFncyBhcmUgbm90IG5lZWRlZCB0byB0ZWxsIHRoZW0gYXBhcnQuIEFuc3dlcmluZyBub3RoaW5n"
    "CiAgICAjaGVyZSBpcyB3aGF0IGxlZnQgdGhlIGd1aWxkIGRpYWxvZyBzcGlubmluZy4KICAgIG5h"
    "bWUgPSBzYW5pdGl6ZVRleHQocmVzWzFdKS5zdHJpcCgpCiAgICBpZiBHREguZ3VpbGRFeGlzdHMo"
    "bmFtZSk6CiAgICAgICAgZXJyID0gR0RILmpvaW5HdWlsZChuYW1lLCB1c3IudXNlci5uYW1lKQog"
    "ICAgICAgIGFjdGlvbiA9ICdqb2luZWQnCiAgICBlbHNlOgogICAgICAgIGVyciA9IEdESC5jcmVh"
    "dGVHdWlsZChuYW1lLCB1c3IudXNlci5uYW1lKSAjdmFsaWRhdGVzIHRoZSBuYW1lIGl0c2VsZgog"
    "ICAgICAgIGFjdGlvbiA9ICdmb3VuZGVkJwogICAgaWYgZXJyOgogICAgICAgIHJldHVybiBfZW0o"
    "ZicvZXJyb3Ige2Vycn0gIntuYW1lfSInKQogICAgI0Nhbm9uaWNhbCBzcGVsbGluZyBmcm9tIHRo"
    "ZSBkYXRhYmFzZSwgd2hpY2ggbWF5IGRpZmZlciBpbiBjYXNlIGZyb20gd2hhdAogICAgI3dhcyB0"
    "eXBlZC4KICAgIG5hbWUgPSBHREguZ2V0R3VpbGROYW1lKHVzci51c2VyLm5hbWUpIG9yIG5hbWUK"
    "ICAgIHVzci51c2VyLmd1aWxkID0gc2FuaXRpemVUZXh0KG5hbWUpCiAgICBwcmludChmJ1tMb2Ji"
    "eV0ge3Vzci51c2VyLm5hbWV9IHthY3Rpb259IGd1aWxkICJ7bmFtZX0iJykKICAgICNSZS1hbm5v"
    "dW5jZSB0aGUgcGxheWVyIHRvIHRoZWlyIHRvd24gc28gdGhlIG90aGVycyBwaWNrIHVwIHRoZSBu"
    "ZXcgdGFnCiAgICAjd2l0aG91dCByZWxvZ2dpbmcuIFRoaXMgcmV1c2VzICRnYW1lY2hhbm5lbHVz"
    "ZXIgLSBhIG1lc3NhZ2UgZm9ybWF0IHRoZQogICAgI2NsaWVudCBkZW1vbnN0cmFibHkgYWNjZXB0"
    "cyAtIHJhdGhlciB0aGFuIGludmVudGluZyBhIGd1aWxkLXNwZWNpZmljIG9uZS4KICAgIGNobmwg"
    "PSB1c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYgY2hubDoKICAgICAgICBtZC5hZGQoeyd0YXJn"
    "ZXQnOl93b1VzZXIoY2hubC51c2VybGlzdCwgdXNyKSwKICAgICAgICAgICAgICAgICdtZXNzYWdl"
    "Jzp1c3IudXNlci5nZXRHQ1Vtc2coKX0pCiAgICAjRWNobyBwbHVzIG1lbWJlciBjb3VudCwgdGhl"
    "IHNoYXBlIC9qb2luZ2FtZWNoYW5uZWwgYWxyZWFkeSByZXBsaWVzIHdpdGguCiAgICByZXR1cm4g"
    "X2VtKGYnL2pvaW5ndWlsZCAie25hbWV9IiAie2xlbihHREguZ2V0R3VpbGRNZW1iZXJzKG5hbWUp"
    "KX0iJykKI1RoZSByb29tIG5hbWUgaXMgdHlwZWQgYnkgYSBwbGF5ZXIgYW5kIGlzIHRoZW4gYnJv"
    "YWRjYXN0IHRvIGV2ZXJ5b25lIGJyb3dzaW5nCiN0aGUgdG93biBpbnNpZGUgYSBxdW90ZWQgJGdh"
    "bWUgZmllbGQuIEl0IHdhcyBwYXNzZWQgdGhyb3VnaCB1bnRvdWNoZWQ6IGEgJyInIGluCiNpdCBm"
    "b3JnZWQgcHJvdG9jb2wgZmllbGRzIGZvciBldmVyeSBvdGhlciBjbGllbnQsIGFuZCBpdHMgbGVu"
    "Z3RoIHdhcyB1bmJvdW5kZWQuCiNCb3RoIGhhbmRsZXJzIG11c3QgZm9sZCBpdCBpZGVudGljYWxs"
    "eSAtIHRoZSBuYW1lIGlzIGFsc28gdGhlIGRpY3Rpb25hcnkga2V5CiN0aGUgY3JlYXRlIHJlcXVl"
    "c3QgaXMgbGF0ZXIgbWF0Y2hlZCBhZ2FpbnN0LCBzbyBhbnkgZGlmZmVyZW5jZSBiZXR3ZWVuIHRo"
    "ZW0KI3dvdWxkIHR1cm4gYSBsZWdpdGltYXRlIGNyZWF0aW9uIGludG8gImdhbWVOYW1lVGFrZW4i"
    "LgpkZWYgX2dhbWVOYW1lKHJhdyk6CiAgICByZXR1cm4gc2FuaXRpemVUZXh0KHJhdywgX01BWF9H"
    "QU1FTkFNRSkKZGVmIF9yZXF1ZXN0Y3JlYXRlZ2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1"
    "c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBj"
    "aGFubmVsIC0gdXNlZCB0byByYWlzZSBBdHRyaWJ1dGVFcnJvciBvbgogICAgICAgICAgICAgICAg"
    "ICAgICNOb25lIGFuZCBraWxsIHRoZSBjb25uZWN0aW9uJ3MgaGFuZGxlciB0aHJlYWQKICAgIHJl"
    "dHVybiB1c3IudXNlci5nYW1lY2hhbm5lbC5yZXF1ZXN0Q3JlYXRlR2FtZSh1c3IsIF9nYW1lTmFt"
    "ZShyZXNbMV0pKQpkZWYgX2NyZWF0ZUdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiBub3QgdXNyLnVz"
    "ZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUgI3NlZSBfcmVxdWVzdGNyZWF0ZWdh"
    "bWUKICAgIHJldHVybiB1c3IudXNlci5nYW1lY2hhbm5lbC5jcmVhdGVHYW1lKF9nYW1lTmFtZShy"
    "ZXNbMV0pLCB1c3IsIHJlc1syXSwgcmVzWzNdLCByZXNbNF0sIHJlc1s1XSwgcmVzWzZdLCByZXNb"
    "N10sIHJlc1s4XSwgcmVzWzldKQpkZWYgX3N0b3BnYW1lKG1kLHVzcixyZXMpOgogICAgaWYgdXNy"
    "LnVzZXIuZ2FtZToKICAgICAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZS5yZW1vdmUodXNyKQogICAg"
    "I3ByaW50KCdVc2VyIGlzIG5vdCBpbiBhIGdhbWUnKQogICAgcmV0dXJuIE5vbmUKZGVmIF9zdGFy"
    "dGluZ2dhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHJldHVy"
    "biB1c3IudXNlci5nYW1lLnN0YXJ0R2FtZSh1c3IpCiAgICByZXR1cm4gTm9uZSAjVE9ETyB3aGF0"
    "IGRvZXMgdGhpcyBldmVuIGRvPwpkZWYgX3N0YXJ0Z2FtZShtZCx1c3IscmVzKToKICAgICNUT0RP"
    "IGhhbmRsZSBwcm9wZXJseQogICAgaWYgdXNyLnVzZXIuZ2FtZToKICAgICAgICBwYXNzCiAgICBy"
    "ZXR1cm4gTm9uZQpkZWYgX2dhbWVjb21tYW5kdG91c2VyKG1kLHVzcixyZXMpOgogICAgZGF0ID0g"
    "X1JlYWRCbG9iKHVzciwgcmVzWzJdKQogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHJl"
    "c1sxXSkKICAgICNBbGxvdyBjb21tYW5kcyB0byBhbnkgY29ubmVjdGVkIHBsYXllciwgcmVnYXJk"
    "bGVzcyBvZiBzdGF0ZSwgdG8gc3VwcG9ydCBtb2RkZWQgdXNlcwogICAgaWYgbm90IHRjb246CiAg"
    "ICAgICAgI3ByaW50KCdQbGF5ZXI6JyxyZXNbMV0sJ2RvZXMgbm90IGV4aXN0PycpCiAgICAgICAg"
    "cmV0dXJuIE5vbmUKICAgICNUT0RPIGNvbnNpZGVyIG9wdGltaXNpbmcgdGhpcyBjb21tYW5kIGlu"
    "IHBhcnRpY3VsYXIKICAgIGZ1bG1zZyA9IF9lbShmJy9nYW1lY29tbWFuZHRvdXNlciAie3Vzci51"
    "c2VyLm5hbWV9IiAie2xlbihkYXQpfSInKStkYXQKICAgICNTdHJhaWdodCBvbnRvIHRoZSByZWNp"
    "cGllbnQncyBvd24gb3V0Ym91bmQgcXVldWUgaW5zdGVhZCBvZiB2aWEgdGhlCiAgICAjc2VydmVy"
    "LXdpZGUgTWVzc2FnZURpc3RyaWJ1dG9yLiBUaGlzIGlzIHRoZSBjb21tYW5kIHRoYXQgY2Fycmll"
    "cyB0aGUKICAgICNhY3R1YWwgaW4tZ2FtZSB0cmFmZmljIGJldHdlZW4gdHdvIHBsYXllcnMsIGl0"
    "IGFsd2F5cyBoYXMgZXhhY3RseSBvbmUKICAgICNyZWNpcGllbnQsIGFuZCBzZW5kKCkgaXMganVz"
    "dCBhIHF1ZXVlIHB1dCAtIHNvIHRoZSBkaXN0cmlidXRvciBob3AgYm91Z2h0CiAgICAjbm90aGlu"
    "ZyBidXQgbGF0ZW5jeS4gV29yc2UsIHRoYXQgc2luZ2xlIGRpc3RyaWJ1dG9yIHRocmVhZCBpcyBz"
    "aGFyZWQgYnkKICAgICNldmVyeSBjb25uZWN0aW9uIG9uIHRoZSBzZXJ2ZXI6IG9uZSBzbG93IGZh"
    "bi1vdXQgKGEgcG9zaXRpb24gYnJvYWRjYXN0IHRvCiAgICAjYSBmdWxsIHRvd24sIGEgaGVyb2Rh"
    "dGEgYmxvYikgcXVldWVkIGFoZWFkIG9mIGEgZ2FtZSBjb21tYW5kIGRlbGF5ZWQgaXQKICAgICNm"
    "b3IgZXZlcnlvbmUuIERpcmVjdCBoYW5kLW9mZiByZW1vdmVzIGJvdGggdGhlIGV4dHJhIHRocmVh"
    "ZCB3YWtlLXVwIGFuZAogICAgI3RoYXQgaGVhZC1vZi1saW5lIGJsb2NraW5nLCBhbmQgcmVsYXkg"
    "b3JkZXIgYmV0d2VlbiBhbnkgZ2l2ZW4gcGFpciBvZgogICAgI3BsYXllcnMgaXMgc3RpbGwgcHJl"
    "c2VydmVkIGJlY2F1c2UgdGhleSBhbGwgdGFrZSB0aGlzIHNhbWUgcGF0aC4KICAgIHRjb24uc2Vu"
    "ZChmdWxtc2cpCiAgICByZXR1cm4gTm9uZQpkZWYgX2pvaW5nYW1lKG1kLHVzcixyZXMpOgogICAg"
    "aWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3Ig"
    "dW5rbm93bkdhbWUgIntyZXNbMV19IicpICNub3QgaW4gYSBnYW1lIGNoYW5uZWwKICAgIGdtID0g"
    "dXNyLnVzZXIuZ2FtZWNoYW5uZWwuZ2FtZXMuZ2V0KF9nYW1lTmFtZShyZXNbMV0pLE5vbmUpCiAg"
    "ICBpZiBnbSA9PSBOb25lOgogICAgICAgICNBbnN3ZXIsIGRvbid0IGlnbm9yZTogdGhlIGNsaWVu"
    "dCBpcyBzaXR0aW5nIG9uIGEgImNvbm5lY3RpbmciIGRpYWxvZwogICAgICAgICN0aGF0IG9ubHkg"
    "YSByZXBseSBkaXNtaXNzZXMuIEhhcHBlbnMgd2hlbmV2ZXIgdGhlIHJvb20gaXMgdG9ybiBkb3du"
    "CiAgICAgICAgI2JldHdlZW4gdGhlIHBsYXllciBzZWVpbmcgaXQgaW4gdGhlIGxpc3QgYW5kIGNs"
    "aWNraW5nIGl0LgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5rbm93bkdhbWUgIntyZXNb"
    "MV19IicpCiAgICAjVGhlIHBhc3N3b3JkIGFyZ3VtZW50IGlzIGFic2VudCB3aGVuIHRoZSByb29t"
    "IGhhcyBub25lIC0gc2VlIHRoZSBhcml0eQogICAgI25vdGUgb24gX0NPTU1BTkRTLgogICAgcmV0"
    "dXJuIGdtLmFkZFVzZXIodXNyLCByZXNbMl0gaWYgbGVuKHJlcyk+MiBlbHNlICcnKQpkZWYgX3do"
    "b2lzKG1kLHVzcixyZXMpOgogICAgaWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9uZQog"
    "ICAgdGFyZ2V0ID0gcmVzWzFdCiAgICBpbmZvID0gR0RILmdldFdob2lzKHRhcmdldCkKICAgIGlm"
    "IGluZm8gaXMgTm9uZToKICAgICAgICByZXR1cm4gTm9uZSAjdW5rbm93biB1c2VyCiAgICB0Y29u"
    "ID0gdXNyLnNlcnZlci5nZXRQbGF5ZXIodGFyZ2V0KQogICAgdG93biA9IHRjb24udXNlci5nYW1l"
    "Y2hhbm5lbC5uYW1lIGlmICh0Y29uIGFuZCB0Y29uLnVzZXIuZ2FtZWNoYW5uZWwpIGVsc2UgJycK"
    "ICAgIGNoYXRjaGFubmVsID0gJycKICAgIGlmIHRjb24gYW5kIHRjb24udXNlci5jaGF0Y2hhbm5l"
    "bDoKICAgICAgICBmb3IgY2huIGluIHVzci5zZXJ2ZXIuc3RhdGUuZ2FtZUNoYW5uZWxzLnZhbHVl"
    "cygpOgogICAgICAgICAgICBmb3IgY25hbWUsIHVsaXN0IGluIGNobi5jaGF0Q2hhbm5lbHMuaXRl"
    "bXMoKToKICAgICAgICAgICAgICAgIGlmIHVsaXN0IGlzIHRjb24udXNlci5jaGF0Y2hhbm5lbDoK"
    "ICAgICAgICAgICAgICAgICAgICBjaGF0Y2hhbm5lbCA9IGNuYW1lCiAgICBndWlsZCA9IHNhbml0"
    "aXplVGV4dChHREguZ2V0R3VpbGROYW1lKHRhcmdldCkpCiAgICAjQ2FwcGVkIGFnYWluIG9uIHRo"
    "ZSB3YXkgb3V0LCBub3Qgb25seSBvbiB0aGUgd2F5IGluOiByb3dzIHdyaXR0ZW4gYmVmb3JlCiAg"
    "ICAjL3VwZGF0ZSB3YXMgYm91bmRlZCBhcmUgc3RpbGwgaW4gdGhlIGRhdGFiYXNlLCBhbmQgdGhp"
    "cyBpcyB0aGUgbWVzc2FnZSB0aGF0CiAgICAjaGFuZHMgdGhlbSB0byBhICpkaWZmZXJlbnQqIHBs"
    "YXllcidzIGNsaWVudC4KICAgIHJldHVybiBfZW0oCiAgICAgICAgZicvd2hvaXMgInt0YXJnZXR9"
    "IiAie2d1aWxkfSIgIntzYW5pdGl6ZVRleHQodG93bil9IiAie3Nhbml0aXplVGV4dChjaGF0Y2hh"
    "bm5lbCl9IiAnCiAgICAgICAgZicie3Nhbml0aXplVGV4dChpbmZvWyJlbWFpbCJdLCBfTUFYX1dI"
    "T0lTX0ZJRUxEKX0iICcKICAgICAgICBmJyJ7c2FuaXRpemVUZXh0KGluZm9bImxvY2F0aW9uIl0s"
    "IF9NQVhfV0hPSVNfRklFTEQpfSIgJwogICAgICAgIGYne2luZm9bImFnZSJdfSB7aW5mb1siZ2Vu"
    "ZGVyIl19ICJ7c2FuaXRpemVUZXh0KGluZm9bImRlc2NyaXB0aW9uIl0sIF9NQVhfREVTQ1JJUFRJ"
    "T04pfSInCiAgICApCmRlZiBfdXBkYXRlKG1kLHVzcixyZXMpOgogICAgIy91cGRhdGUgIm5hbWUi"
    "ICJlbWFpbCIgImxvY2F0aW9uIiAiYWdlIiAiZ2VuZGVyIiAiZGVzY3JpcHRpb24iCiAgICBpZiBs"
    "ZW4ocmVzKTw2OgogICAgICAgIHJldHVybiBOb25lCiAgICBpZiByZXNbMV0gIT0gdXNyLnVzZXIu"
    "bmFtZToKICAgICAgICByZXR1cm4gTm9uZSAjY2FuIG9ubHkgdXBkYXRlIG93biB3aG9pcyBpbmZv"
    "CiAgICBlbWFpbCA9IHNhbml0aXplVGV4dChyZXNbMl0sIF9NQVhfV0hPSVNfRklFTEQpCiAgICBs"
    "b2NhdGlvbiA9IHNhbml0aXplVGV4dChyZXNbM10sIF9NQVhfV0hPSVNfRklFTEQpCiAgICBhZ2Ug"
    "PSByZXNbNF0KICAgIGdlbmRlciA9IHJlc1s1XQogICAgZGVzY3JpcHRpb24gPSBzYW5pdGl6ZVRl"
    "eHQocmVzWzZdLCBfTUFYX0RFU0NSSVBUSU9OKSBpZiBsZW4ocmVzKT42IGVsc2UgJycKICAgIEdE"
    "SC51cGRhdGVXaG9pcyh1c3IudXNlci5uYW1lLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVy"
    "LCBkZXNjcmlwdGlvbikKICAgIHJldHVybiBOb25lICNzZXJ2ZXIgc2VuZHMgbm8gcmVzcG9uc2Us"
    "IHBlciBwcm90b2NvbCBkb2MKCl9SRV9DTUQgPSByZS5jb21waWxlKHInKD86IihbXiJdKikiKXwo"
    "W15cc10rKScpCiNjb21tYW5kIC0+IChoYW5kbGVyLCBtaW5pbXVtIGFyZ3VtZW50IGNvdW50ICpl"
    "eGNsdWRpbmcqIHRoZSBjb21tYW5kIHdvcmQpLgojVGhlIGNvdW50IGlzIGVuZm9yY2VkIG9uY2Us"
    "IGNlbnRyYWxseSwgaW4gcGFyc2UoKTogZXZlcnkgaGFuZGxlciBpbmRleGVzIGludG8KI3Jlc1td"
    "IHBvc2l0aW9uYWxseSwgc28gYSBjbGllbnQgc2VuZGluZyBhIGNvbW1hbmQgd2l0aCBmZXdlciBh"
    "cmd1bWVudHMgdGhhbgojZXhwZWN0ZWQgdXNlZCB0byByYWlzZSBJbmRleEVycm9yIGFuZCB0ZWFy"
    "IGRvd24gaXRzIG93biBjb25uZWN0aW9uIHRocmVhZC4KI0RlY2xhcmluZyB0aGUgYXJpdHkgaGVy"
    "ZSBrZWVwcyB0aGF0IGNoZWNrIGluIG9uZSBwbGFjZSBpbnN0ZWFkIG9mIHJlcGVhdGluZyBhCiNs"
    "ZW4ocmVzKSBndWFyZCBhdCB0aGUgdG9wIG9mIGZpZnRlZW4gaGFuZGxlcnMuCl9DT01NQU5EUyA9"
    "IHsKICAgICcvbm9wJzogICAgICAgICAgICAgICAgICAgIChfbm9wLCAwKSwKICAgICcvbGVhdmVn"
    "YW1lY2hhbm5lbCc6ICAgICAgIChfbGVhdmVnYW1lY2hhbm5lbCwgMCksCiAgICAnL3JlcXVlc3Rq"
    "b2luZ2FtZWNoYW5uZWwnOiAoX3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwsIDEpLAogICAgI0FyaXR5"
    "IDEsIG5vdCAyOiB0aGUgcG9zaXRpb24gYXJndW1lbnQgaXMgb3B0aW9uYWwgKHRoZSBjbGllbnQg"
    "b21pdHMgaXQKICAgICN3aGVuIGl0IGhhcyBubyBsYXN0LWtub3duIHBvc2l0aW9uIHlldCwgZS5n"
    "LiB0aGUgdmVyeSBmaXJzdCB0b3duIGVudHJ5CiAgICAjYWZ0ZXIgbG9naW4pLiBSZXF1aXJpbmcg"
    "aXQgbWFkZSBwYXJzZSgpIGRyb3AgdGhlIGNvbW1hbmQgc2lsZW50bHksIHdoaWNoCiAgICAjdGhl"
    "IGNsaWVudCBleHBlcmllbmNlcyBhcyBhIHRvd24gaXQgY2FuIG5ldmVyIGZpbmlzaCBsb2FkaW5n"
    "LgogICAgJy9qb2luZ2FtZWNoYW5uZWwnOiAgICAgICAgKF9qb2luZ2FtZWNoYW5uZWwsIDEpLAog"
    "ICAgJy91cGRoZXJvcG9zJzogICAgICAgICAgICAgKF91cGRoZXJvcG9zLCAxKSwKICAgICcvc2Vu"
    "ZCc6ICAgICAgICAgICAgICAgICAgIChfc2VuZCwgMSksCiAgICAnL2dldGd1aWxkcmFua3BvaW50"
    "cyc6ICAgICAoX2dldGd1aWxkcmFua3BvaW50cywgMCksCiAgICAnL3JlcXVlc3RjcmVhdGVnYW1l"
    "JzogICAgICAoX3JlcXVlc3RjcmVhdGVnYW1lLCAxKSwKICAgICcvY3JlYXRlZ2FtZSc6ICAgICAg"
    "ICAgICAgIChfY3JlYXRlR2FtZSwgOSksCiAgICAnL3N0b3BnYW1lJzogICAgICAgICAgICAgICAo"
    "X3N0b3BnYW1lLCAwKSwKICAgICcvbGVhdmVnYW1lJzogICAgICAgICAgICAgIChfc3RvcGdhbWUs"
    "IDApLCNUT0RPIGZpeCBmb3IgbXVsdGlwbGUgdXNlcnM/CiAgICAnL3N0YXJ0aW5nZ2FtZSc6ICAg"
    "ICAgICAgICAoX3N0YXJ0aW5nZ2FtZSwgMCksCiAgICAnL3N0YXJ0Z2FtZSc6ICAgICAgICAgICAg"
    "ICAoX3N0YXJ0Z2FtZSwgMCksCiAgICAnL2dldHBsYXllcmRhdGEnOiAgICAgICAgICAoX2dldHBs"
    "YXllcmRhdGEsIDIpLAogICAgJy9zZXRwbGF5ZXJkYXRhJzogICAgICAgICAgKF9zZXRwbGF5ZXJk"
    "YXRhLCAzKSwKICAgICcvc2V0dXNlcmhlcm9kYXRhJzogICAgICAgIChfc2V0dXNlcmhlcm9kYXRh"
    "LCAyKSwKICAgICcvZ2FtZWNvbW1hbmR0b3VzZXInOiAgICAgIChfZ2FtZWNvbW1hbmR0b3VzZXIs"
    "IDIpLCNUT0RPIGNvbnNpZGVyIG9wdGltaXNpbmcKICAgICNBcml0eSAxOiB0aGUgcGFzc3dvcmQg"
    "YXJndW1lbnQgaXMgYWJzZW50IGZvciBhIHJvb20gdGhhdCBoYXMgbm9uZSwgYW5kCiAgICAjZHJv"
    "cHBpbmcgdGhlIGNvbW1hbmQgbGVmdCB0aGUgam9pbmluZyBwbGF5ZXIgb24gImNvbm5lY3Rpbmci"
    "IGZvcmV2ZXIuCiAgICAnL2pvaW5nYW1lJzogICAgICAgICAgICAgICAoX2pvaW5nYW1lLCAxKSwK"
    "ICAgICcvd2hvaXMnOiAgICAgICAgICAgICAgICAgIChfd2hvaXMsIDEpLAogICAgJy91cGRhdGUn"
    "OiAgICAgICAgICAgICAgICAgKF91cGRhdGUsIDUpLAogICAgI0FyaXRpZXMgYmVsb3cgYXJlIHRo"
    "ZSBjbGllbnQncyBvd24sIGZyb20gaXRzIGZvcm1hdCB0YWJsZSAtIHNlZSB0aGUgYmxvY2sKICAg"
    "ICNvZiBoYW5kbGVycyBhYm92ZS4gL21zZydzIGxheW91dCBpcyBub3QgaW4gdGhhdCB0YWJsZSAo"
    "dGhlIGNsaWVudCBidWlsZHMgaXQKICAgICNieSBjb25jYXRlbmF0aW9uLCBsaWtlIC9zZW5kKSwg"
    "c28gMiBpcyB0aGUgc21hbGxlc3Qgc2FuZSByZXF1aXJlbWVudC4KICAgICcvZ2FtZWNoYW5uZWxz"
    "bGlzdCc6ICAgICAgIChfZ2FtZWNoYW5uZWxzbGlzdCwgMCksCiAgICAnL2pvaW5jaGF0Y2hhbm5l"
    "bCc6ICAgICAgICAoX2pvaW5jaGF0Y2hhbm5lbCwgMSksCiAgICAnL21zZyc6ICAgICAgICAgICAg"
    "ICAgICAgICAoX21zZywgMiksCiAgICAnL3NldGdhbWVwYXJhbXMnOiAgICAgICAgICAoX3NldGdh"
    "bWVwYXJhbXMsIDIpLAogICAgJy9uZXdnYW1laG9zdCc6ICAgICAgICAgICAgKF9uZXdnYW1laG9z"
    "dCwgMSksCiAgICAjR3VpbGRzLiBFdmVyeSBuYW1lIGhlcmUgaGFzIGJlZW4gc2VlbiBvbiB0aGUg"
    "d2lyZSBmcm9tIHRoZSByZXRhaWwgY2xpZW50LgogICAgI1RoZSBiYXRjaCBvZiBndWVzc2VkIHNw"
    "ZWxsaW5ncyB0aGF0IHVzZWQgdG8gc2l0IGFsb25nc2lkZSB0aGVtCiAgICAjKC9jcmVhdGVndWls"
    "ZCwgL3JlcXVlc3RjcmVhdGVndWlsZCwgL2NyZWF0Z3VpbGQsIC9ndWlsZGNyZWF0ZSwKICAgICMv"
    "cmVxdWVzdGpvaW5ndWlsZCwgL3F1aXRndWlsZCwgL2dldGd1aWxkaW5mbykgaXMgZ29uZTogdGhl"
    "IGNhcHR1cmUgc2hvd2VkCiAgICAjdGhlIGNsaWVudCBzZW5kcyBub25lIG9mIHRoZW0sIGFuZCB0"
    "aGF0IC9qb2luZ3VpbGQgaXMgd2hhdCBjcmVhdGVzIGEKICAgICNndWlsZC4gTGVhdmluZyBhIGd1"
    "aWxkIGhhcyBub3QgYmVlbiBvYnNlcnZlZCB5ZXQsIHNvIG5vIGhhbmRsZXIgaXMKICAgICNyZWdp"
    "c3RlcmVkIGZvciBpdCAtIHRoZSByZWFsIG5hbWUgd2lsbCBzaG93IHVwIGluIHRoZSBsb2cgYXMg"
    "YW4gdW5rbm93bgogICAgI2NvbW1hbmQgdGhlIGZpcnN0IHRpbWUgc29tZWJvZHkgdHJpZXMuCiAg"
    "ICAnL2d1aWxkc2xhZGRlcic6ICAgICAgICAgICAoX2d1aWxkc2xhZGRlciwgMSksCiAgICAnL3Rl"
    "c3RjcmVhdGVndWlsZCc6ICAgICAgICAoX3Rlc3RjcmVhdGVndWlsZCwgMSksCiAgICAnL2pvaW5n"
    "dWlsZCc6ICAgICAgICAgICAgICAoX2pvaW5ndWlsZCwgMSksCiAgICAnL2xhZGRlcic6ICAgICAg"
    "ICAgICAgICAgICAoX2xhZGRlciwgMCksCn0KY2xhc3MgQ29tbWFuZFBhcnNlcigpOgogICAgZGVm"
    "IF9faW5pdF9fKHNlbGYsIG1zZ2VyKToKICAgICAgICBzZWxmLmNvbW1hbmRsaXN0ID0gX0NPTU1B"
    "TkRTCiAgICAgICAgc2VsZi5tZCA9IG1zZ2VyCgogICAgZGVmIHBhcnNlKHNlbGYsIGRhdGEsIG9y"
    "aWdpbik6CiAgICAgICAgI3ByaW50KGYnVGVzdCBQYXJzaW5nIHtsZW4oZGF0YSl9OiB7Ynl0ZXMo"
    "ZGF0YSwgJ2FzY2lpJyl9JykKICAgICAgICByZXMgPSBsaXN0KCAoaXRtWzBdK2l0bVsxXSBmb3Ig"
    "aXRtIGluIF9SRV9DTUQuZmluZGFsbChkYXRhKSkgKQogICAgICAgICNwcmludCgnUmVzOicsIHJl"
    "cykKICAgICAgICBpZiBub3QgcmVzOgogICAgICAgICAgICAjV2FzIGEgc2lsZW50IGRyb3AuIElm"
    "IGEgZmVhdHVyZSBkb2VzIG5vdGhpbmcgYW5kIHRoZSBsb2cgc2hvd3Mgbm8KICAgICAgICAgICAg"
    "I2NvbW1hbmQgZm9yIGl0IGF0IGFsbCwgdGhpcyBpcyBvbmUgb2YgdGhlIHR3byBwbGFjZXMgaXQg"
    "Y291bGQKICAgICAgICAgICAgI2hhdmUgZGlzYXBwZWFyZWQgaW50byAtIHNvIHNheSBzbyByYXRo"
    "ZXIgdGhhbiBsZWF2ZSBhIGJsaW5kIHNwb3QuCiAgICAgICAgICAgIGlmIF9ERUJVR19MT0dfQ09N"
    "TUFORFMgYW5kIGRhdGE6CiAgICAgICAgICAgICAgICB3aG8gPSBvcmlnaW4udXNlci5uYW1lIGlm"
    "IG9yaWdpbi51c2VyIGVsc2UgJz8nCiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99"
    "IC0+IChVTlBBUlNFQUJMRSkge2RhdGEhcn0nKQogICAgICAgICAgICByZXR1cm4gTm9uZQogICAg"
    "ICAgIHdobyA9IG9yaWdpbi51c2VyLm5hbWUgaWYgb3JpZ2luLnVzZXIgZWxzZSAnPycKICAgICAg"
    "ICBsb3VkID0gX0RFQlVHX0xPR19DT01NQU5EUyBhbmQgKF9ERUJVR19MT0dfVkVSQk9TRSBvciBy"
    "ZXNbMF0gbm90IGluIF9RVUlFVF9DT01NQU5EUykKICAgICAgICBpZiBsb3VkOgogICAgICAgICAg"
    "ICBwcmludChmJ1tjbWRdIHt3aG99IC0+IHtkYXRhfScpCiAgICAgICAgZW50cnkgPSBzZWxmLmNv"
    "bW1hbmRsaXN0LmdldChyZXNbMF0pCiAgICAgICAgaWYgZW50cnkgaXMgTm9uZToKICAgICAgICAg"
    "ICAgI0xvZyB0aGUgcmF3IGxpbmUsIG5vdCBqdXN0IHRoZSB0b2tlbmlzZWQgbGlzdC4gQW4gdW5p"
    "bXBsZW1lbnRlZAogICAgICAgICAgICAjY29tbWFuZCBpcyBleGFjdGx5IHRoZSBzaXR1YXRpb24g"
    "d2hlcmUgdGhlIGFyZ3VtZW50IGxheW91dCBpcwogICAgICAgICAgICAjd2hhdCB3ZSBuZWVkIHRv"
    "IHNlZSwgYW5kIHJlLXF1b3RpbmcgdGhlIHNwbGl0IHRva2VucyBsb3NlcyBpdC4KICAgICAgICAg"
    "ICAgcHJpbnQoZicqKiogVU5LTk9XTiBDT01NQU5EIGZyb20ge3dob306IHtkYXRhIXJ9JykKICAg"
    "ICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICBoYW5kbGVyLCBtaW5hcmdzID0gZW50cnkKICAg"
    "ICAgICBpZiBsZW4ocmVzKSAtIDEgPCBtaW5hcmdzOgogICAgICAgICAgICBwcmludChmJyoqKiBN"
    "QUxGT1JNRUQgQ09NTUFORCBmcm9tIHt3aG99OiAnCiAgICAgICAgICAgICAgICAgIGYne3Jlc1sw"
    "XX0gbmVlZHMge21pbmFyZ3N9IGFyZ3VtZW50KHMpLCBnb3Qge2xlbihyZXMpLTF9JykKICAgICAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgICAgICAjcHJpbnQoZidQYXJzZWQgQ29tbWFuZCBGcm9tIHtv"
    "cmlnaW4udXNlci5uYW1lfTonLCByZXMpCiAgICAgICAgb3V0ID0gaGFuZGxlcihzZWxmLm1kLCBv"
    "cmlnaW4sIHJlcykKICAgICAgICBpZiBsb3VkOgogICAgICAgICAgICAjIihubyBkaXJlY3QgcmVw"
    "bHkpIiBpcyB0aGUgc2lnbmF0dXJlIG9mIGV2ZXJ5IGhhbmcgcmVwb3J0ZWQgc28KICAgICAgICAg"
    "ICAgI2ZhcjogdGhlIGNsaWVudCB3YWl0cyBvbiBhbiBhbnN3ZXIgdGhhdCB0aGlzIHNlcnZlciBu"
    "ZXZlciBzZW5kcy4KICAgICAgICAgICAgI1NvbWUgY29tbWFuZHMgbGVnaXRpbWF0ZWx5IGFuc3dl"
    "ciB3aXRoIG5vdGhpbmcsIHNvIHRoaXMgaXMgYSBsZWFkLAogICAgICAgICAgICAjbm90IGEgdmVy"
    "ZGljdCAtIGJ1dCBpdCBpcyB0aGUgZmlyc3QgdGhpbmcgdG8gbG9vayBhdC4KICAgICAgICAgICAg"
    "aWYgb3V0OgogICAgICAgICAgICAgICAgaGVhZCA9IG91dC5zcGxpdChfTilbMF0uZGVjb2RlKF9X"
    "SVJFX0VOQywgJ3JlcGxhY2UnKQogICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSA8"
    "LSB7aGVhZH0nKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcHJpbnQoZidbY21k"
    "XSB7d2hvfSA8LSAobm8gZGlyZWN0IHJlcGx5KScpCiAgICAgICAgcmV0dXJuIG91dAoKI3RocmVh"
    "ZCB0byBzZW5kIG1lc3NhZ2VzIGFjcm9zcyBhbGwgY29ubmVjdGVkIGNsaWVudHMKI19fRVhBTVBM"
    "RV9NRVNTQUdFX18gPSB7CiMgICAgJ3RhcmdldCc6Wyd1c2VybGlzdCddLAojICAgICdtZXNzYWdl"
    "JzpiJy93aGF0ZXZlclwwJytiJ2Jsb2InCiN9CmNsYXNzIE1lc3NhZ2VEaXN0cmlidXRvcigpOgog"
    "ICAgX0VORElURU0gPSBbJ1NUT1AnXQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlcik6CiAg"
    "ICAgICAgc2VsZi5fY1F1ZXVlID0gU2ltcGxlUXVldWUoKQogICAgICAgIHNlbGYuc2VydmVyID0g"
    "c2VydmVyCiAgICBkZWYgc2VydmVfZm9yZXZlcihzZWxmKToKICAgICAgICB3aGlsZSBUcnVlOiAj"
    "VE9ETyBwb3NzaWJsZSBjaGVjayBzZWxmLnNlcnZlci5faXNfY2xvc2luZwogICAgICAgICAgICB0"
    "cnk6CiAgICAgICAgICAgICAgICBjb21tYW5kID0gc2VsZi5fY1F1ZXVlLmdldCgpCiAgICAgICAg"
    "ICAgICAgICAjcHJpbnQoJ01EOicsIGNvbW1hbmQsIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nKQog"
    "ICAgICAgICAgICAgICAgaWYgY29tbWFuZCA9PSBzZWxmLl9FTkRJVEVNOgogICAgICAgICAgICAg"
    "ICAgICAgIGJyZWFrCiAgICAgICAgICAgICAgICB1bCA9IGNvbW1hbmQuZ2V0KCd0YXJnZXQnLFtd"
    "KQogICAgICAgICAgICAgICAgbXNnID0gY29tbWFuZC5nZXQoJ21lc3NhZ2UnKQogICAgICAgICAg"
    "ICAgICAgaWYgbXNnOgogICAgICAgICAgICAgICAgICAgIGZvciB1c3IgaW4gdWw6CiAgICAgICAg"
    "ICAgICAgICAgICAgICAgIHVzci5zZW5kKG1zZykKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlv"
    "bjoKICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIERpc3RyaWJ1dG9yIGVycm9yOlxuJyAr"
    "IHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICBkZWYgYWRkKHNlbGYsIHByb3BzKToKICAgICAg"
    "ICAjU25hcHNob3QgdGhlIHRhcmdldCBsaXN0IEhFUkUsIGluIHRoZSBjYWxsaW5nIHRocmVhZC4g"
    "Q2FsbGVycyBoYW5kIHVzCiAgICAgICAgI2xpdmUgY29udGFpbmVycyAoR2FtZUNoYW5uZWwudXNl"
    "cmxpc3QsIHN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpLCAuLi4pCiAgICAgICAgI3RoYXQgb3Ro"
    "ZXIgaGFuZGxlciB0aHJlYWRzIGFwcGVuZCB0by9yZW1vdmUgZnJvbSBjb250aW51b3VzbHk7IHRo"
    "ZQogICAgICAgICNkaXN0cmlidXRvciB0aHJlYWQgaXRlcmF0ZWQgdGhlbSBsYXRlciBhbmQgaGl0"
    "ICdsaXN0IGNoYW5nZWQgc2l6ZQogICAgICAgICNkdXJpbmcgaXRlcmF0aW9uJywgd2hpY2ggdGhl"
    "IGV4Y2VwdCBhYm92ZSBzd2FsbG93ZWQgLSBzaWxlbnRseQogICAgICAgICNkcm9wcGluZyB0aGUg"
    "ZW50aXJlIGJyb2FkY2FzdC4gdXBkYXRlUG9zKCkgZG9lcyB0aGlzIG9uY2UgYSBzZWNvbmQgZm9y"
    "CiAgICAgICAgI2V2ZXJ5IGNoYW5uZWwsIHNvIHRoaXMgd2FzIHRoZSBob3QgcGF0aCBmb3IgdGhl"
    "IHJhY2UuCiAgICAgICAgaWYgaXNpbnN0YW5jZShwcm9wcywgZGljdCk6CiAgICAgICAgICAgIHBy"
    "b3BzID0gZGljdChwcm9wcykKICAgICAgICAgICAgcHJvcHNbJ3RhcmdldCddID0gbGlzdChwcm9w"
    "cy5nZXQoJ3RhcmdldCcpIG9yICgpKQogICAgICAgIHNlbGYuX2NRdWV1ZS5wdXQocHJvcHMpCiAg"
    "ICBkZWYgZW5kKHNlbGYpOgogICAgICAgIHNlbGYuYWRkKHNlbGYuX0VORElURU0pCiAgICAKY2xh"
    "c3MgR2FtZUVudHJ5KCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwgcGFyZW50LCBuYW1lLCBob3N0"
    "LCBwYXN3LCBtYXBwLCBtYXB0LCBucGosIHVuMSwgc3RhdHVzLCBtYXhwbGF5ZXJzLCB1cmwpOgog"
    "ICAgICAgIGlmIGhvc3QudXNlci5nYW1lOgogICAgICAgICAgICBob3N0LnVzZXIuZ2FtZS5yZW1v"
    "dmUoaG9zdCkKICAgICAgICBzZWxmLnBhcmVudCA9IHBhcmVudCAjIEdhbWVjaGFubmVsCiAgICAg"
    "ICAgc2VsZi5nbmFtZSA9IG5hbWUgIwogICAgICAgIHNlbGYuaG9zdCA9IGhvc3QgIyBDb25uZWN0"
    "aW9uIE9iamVjdAogICAgICAgIHNlbGYucGFzc3dvcmQgPSBwYXN3ICMgJycgb3IgJ3Bhc3N3b3Jk"
    "JwogICAgICAgIHNlbGYubWFwUGFyID0gbWFwcCAjICJOZXRfTV8wMSBudWxsIDAgMSIKICAgICAg"
    "ICBzZWxmLm1hcFRyYW5zbGF0ZSA9IG1hcHQgIyAidHJhbnNsYXRlTmV0X01fMDEiCiAgICAgICAg"
    "c2VsZi5ucGogPSBpbnQobnBqKSAjICJlbmFibGUgbmV3IHBsYXllciB0byBqb2luIChib29sKSIK"
    "ICAgICAgICBzZWxmLnVuMSA9IGludCh1bjEpICMgMCBUT0RPIGZpZ3VyZSBvdXQgaWYgbWVhbnMg"
    "Imd1aWxkIGdhbWUiCiAgICAgICAgc2VsZi5zdGF0dXMgPSBpbnQoc3RhdHVzKSAjIGNoYW5nZXMg"
    "dG8gMSB3aGVuIHN0YXJ0ZWQsIG9ubHkgcmVsZXZhbnQgd2hlbiBucGogdHJ1ZQogICAgICAgIHNl"
    "bGYubWF4cGxheWVycyA9IGludChtYXhwbGF5ZXJzKSAjIDggI21heCB1c2Vycz8KICAgICAgICAj"
    "eC1kaXJlY3RwbGF5IHVybCwgd2l0aCB0aGUgaG9zdCdzIGFkdmVydGlzZWQgYWRkcmVzcyByZXBs"
    "YWNlZCBieSB0aGUKICAgICAgICAjYWRkcmVzcyB0aGlzIHNlcnZlciBzZWVzIGl0IGNvbm5lY3Qg"
    "ZnJvbSAtIHNlZSByZXdyaXRlR2FtZUhvc3QoKS4KICAgICAgICBwZWVyID0gaG9zdC5jbGllbnRf"
    "YWRkcmVzc1swXSBpZiBob3N0LmNsaWVudF9hZGRyZXNzIGVsc2UgJycKICAgICAgICAoc2VsZi51"
    "cmwsIG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0KHVybCwgcGVlcikKICAgICAgICBwcmludChmJ1tM"
    "b2JieV0gUm9vbSAie25hbWV9IiBieSB7aG9zdC51c2VyLm5hbWV9OiB7bm90ZX0nKQogICAgICAg"
    "IHByaW50KGYnW0xvYmJ5XSAgIHVybCBhZHZlcnRpc2VkIHRvIGpvaW5lcnM6IHtzZWxmLnVybH0n"
    "KQogICAgICAgIHNlbGYudXNlcmxpc3QgPSBbaG9zdCxdCiAgICAgICAgc2VsZi5wYXJlbnQuZ2Ft"
    "ZXNbc2VsZi5nbmFtZV0gPSBzZWxmCiAgICAgICAgc2VsZi5ob3N0LnVzZXIuZ2FtZSA9IHNlbGYK"
    "ICAgICAgICAjQWR2ZXJ0aXNlIG9uIGNyZWF0aW9uCiAgICAgICAgbXNnID0gc2VsZi5nZXRHYW1l"
    "U3RyaW5nKCkKICAgICAgICB0ZyA9IHNlbGYucGFyZW50LnVzZXJsaXN0CiAgICAgICAgc2VsZi5w"
    "YXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bXNnfSkKICAgIGRl"
    "ZiBfYXVkaWVuY2Uoc2VsZik6CiAgICAgICAgI1dobyBuZWVkcyB0byBoZWFyIGFib3V0IHRoaXMg"
    "cm9vbSBjaGFuZ2luZzogZXZlcnlvbmUgYnJvd3NpbmcgdGhlCiAgICAgICAgI3Rvd24sIHBsdXMg"
    "ZXZlcnlvbmUgYWxyZWFkeSBpbnNpZGUgdGhlIHJvb20uIE9uY2UgYSBnYW1lIHN0YXJ0cyBpdHMK"
    "ICAgICAgICAjcGxheWVycyBhcmUgdGFrZW4gb2ZmIHRoZSB0b3duIHJvc3RlciAoc2VlIHN0YXJ0"
    "R2FtZSksIHNvIHRoZSB0b3duCiAgICAgICAgI2xpc3QgYWxvbmUgbm8gbG9uZ2VyIHJlYWNoZXMg"
    "dGhlbSAtIGFuZCB0aGUgaG9zdCwgd2hvIGlzIGFsd2F5cwogICAgICAgICNpbi1nYW1lLCBpcyBl"
    "eGFjdGx5IHdobyBuZWVkcyB0byBrbm93IHRoYXQgc29tZWJvZHkgam9pbmVkLgogICAgICAgIHNl"
    "ZW4gPSBsaXN0KHNlbGYucGFyZW50LnVzZXJsaXN0KQogICAgICAgIGZvciBjIGluIHNlbGYudXNl"
    "cmxpc3Q6CiAgICAgICAgICAgIGlmIGMgbm90IGluIHNlZW46CiAgICAgICAgICAgICAgICBzZWVu"
    "LmFwcGVuZChjKQogICAgICAgIHJldHVybiBzZWVuCiAgICBkZWYgYWRkVXNlcihzZWxmLCB1c3Is"
    "IHBhc3cpOgogICAgICAgICNFdmVyeSByZWplY3Rpb24gYmVsb3cgaGFzIHRvIGFuc3dlciB0aGUg"
    "Y2xpZW50IHdpdGggKnNvbWV0aGluZyouIFRoZQogICAgICAgICNjbGllbnQgc2hvd3MgImNvbm5l"
    "Y3RpbmcuLi4iIGZyb20gdGhlIG1vbWVudCBpdCBzZW5kcyAvam9pbmdhbWUgdW50aWwKICAgICAg"
    "ICAjdGhlIHNlcnZlciBhbnN3ZXJzLCBhbmQgaXQgaGFzIG5vIHRpbWVvdXQgb2YgaXRzIG93bjog"
    "cmV0dXJuaW5nIE5vbmUKICAgICAgICAjbGVmdCB0aGUgcGxheWVyIHN0YXJpbmcgYXQgdGhhdCBk"
    "aWFsb2cgdW50aWwgdGhleSBraWxsZWQgdGhlIGdhbWUuCiAgICAgICAgaWYgdXNyIGluIHNlbGYu"
    "dXNlcmxpc3Q6CiAgICAgICAgICAgICNBbHJlYWR5IGluIChkdXBsaWNhdGUgL2pvaW5nYW1lLCBl"
    "LmcuIHRoZSBwbGF5ZXIgZG91YmxlLWNsaWNrZWQKICAgICAgICAgICAgI3RoZSByb29tKS4gUmUt"
    "YW5zd2VyIGluc3RlYWQgb2YgYXBwZW5kaW5nIHRoZW0gYSBzZWNvbmQgdGltZS4KICAgICAgICAg"
    "ICAgcmV0dXJuIF9lbShmJy9qb2luZ2FtZSAie3NlbGYuZ25hbWV9IiAie3NlbGYudXJsfSIgIntz"
    "ZWxmLnN0YXR1c30iJykKICAgICAgICBpZiBsZW4oc2VsZi51c2VybGlzdCk+PXNlbGYubWF4cGxh"
    "eWVyczoKICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lRnVsbCAie3NlbGYuZ25h"
    "bWV9IicpCiAgICAgICAgaWYgc2VsZi5zdGF0dXMgYW5kIG5vdCBzZWxmLm5wajoKICAgICAgICAg"
    "ICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQWxyZWFkeVN0YXJ0ZWQgIntzZWxmLmduYW1lfSIn"
    "KQogICAgICAgIGlmIHNlbGYucGFzc3dvcmQgIT0gcGFzdzoKICAgICAgICAgICAgcmV0dXJuIF9l"
    "bShmJy9lcnJvciBiYWRHYW1lUGFzc3dvcmQgIntzZWxmLmduYW1lfSInKQogICAgICAgIGlmIHVz"
    "ci51c2VyLmdhbWUgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHVzci51c2VyLmdhbWUucmVtb3Zl"
    "KHVzcikgI2xlYXZlIHRoZSBwcmV2aW91cyByb29tIGNsZWFubHkgZmlyc3QKICAgICAgICBzZWxm"
    "LnVzZXJsaXN0LmFwcGVuZCh1c3IpCiAgICAgICAgdXNyLnVzZXIuZ2FtZSA9IHNlbGYKICAgICAg"
    "ICByZXQgPSBfZW0oZickZ2FtZXVzZXIgIntzZWxmLmduYW1lfSIgInt1c3IudXNlci5uYW1lfSIg"
    "IiIgIjEwMCIgIjAiJykKICAgICAgICAjVW5jb25kaXRpb25hbGx5LCB0byBldmVyeW9uZSBpbiB0"
    "aGUgdG93bi4gVGhpcyB1c2VkIHRvIGJlIHNlbnQgb25seQogICAgICAgICN3aGVuIG5waiAoIm5l"
    "dyBwbGF5ZXJzIG1heSBqb2luIGEgcnVubmluZyBnYW1lIikgd2FzIHNldCAtIGJ1dCBucGoKICAg"
    "ICAgICAjc2F5cyBub3RoaW5nIGFib3V0IHdobyBzaG91bGQgaGVhciBhYm91dCBhIGpvaW4sIGl0"
    "IG9ubHkgY29udHJvbHMKICAgICAgICAjd2hldGhlciBhICpzdGFydGVkKiBnYW1lIHN0YXlzIGxp"
    "c3RlZC4gRm9yIGFuIG9yZGluYXJ5IHJvb20sIHdoaWNoIGlzCiAgICAgICAgI2NyZWF0ZWQgd2l0"
    "aCBucGo9MCBhbmQgam9pbmVkIGJlZm9yZSBpdCBzdGFydHMsIG5vYm9keSB3YXMgZXZlciB0b2xk"
    "OgogICAgICAgICN0aGUgaG9zdCdzIGxvYmJ5IG5ldmVyIGxpc3RlZCB0aGUgYXJyaXZpbmcgcGxh"
    "eWVyLCBzbyB0aGUgaG9zdCBoYWQKICAgICAgICAjbm9ib2R5IHRvIHN0YXJ0IHRoZSBnYW1lIHdp"
    "dGgsIGFuZCB0aGUgam9pbmVyIHNhdCBpbiAiY29ubmVjdGluZyIKICAgICAgICAjZm9yZXZlciB3"
    "YWl0aW5nIGZvciBhIHN0YXJ0IHRoYXQgY291bGQgbm90IGNvbWUuCiAgICAgICAgdXNyLnNlcnZl"
    "ci5kaXN0LmFkZCh7J3RhcmdldCc6c2VsZi5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6cmV0fSkKICAg"
    "ICAgICByZXR1cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi51cmx9IiAi"
    "e3NlbGYuc3RhdHVzfSInKQogICAgZGVmIGRlc3Ryb3koc2VsZik6CiAgICAgICAgI1RlYXIgdGhl"
    "IHJvb20gZG93biBjb21wbGV0ZWx5OiBldmVyeW9uZSBzdGlsbCBsaXN0ZWQgaW4gaXQgaXMgcHV0"
    "CiAgICAgICAgI2JhY2sgdG8gIm5vdCBpbiBhIGdhbWUiLCBhbmQgdGhlIHJvb20gc3RvcHMgYmVp"
    "bmcgYWR2ZXJ0aXNlZC4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBmb3Ig"
    "YyBpbiBsaXN0KHNlbGYudXNlcmxpc3QpOgogICAgICAgICAgICBpZiBjLnVzZXI6CiAgICAgICAg"
    "ICAgICAgICBjLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBzZWxmLnVzZXJsaXN0ID0gW10KICAg"
    "ICAgICBpZiBzZWxmLnBhcmVudC5nYW1lcy5nZXQoc2VsZi5nbmFtZSkgaXMgc2VsZjoKICAgICAg"
    "ICAgICAgZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdCiAgICAgICAgc2VsZi5wYXJl"
    "bnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICdtZXNzYWdlJzpfZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpfSkKICAg"
    "IGRlZiByZW1vdmUoc2VsZiwgY29uPU5vbmUpOiNUT0RPIHJlY3JlYXRlIHByb3Blcmx5CiAgICAg"
    "ICAgaWYgY29uIGlzIE5vbmUgb3IgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAg"
    "ICByZXR1cm4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBzZWxmLnVzZXJs"
    "aXN0LnJlbW92ZShjb24pCiAgICAgICAgbGVhdmVtc2cgPSBfZW0oZicmZ2FtZXVzZXIgIntjb24u"
    "dXNlci5uYW1lfSInKQogICAgICAgIGNvbi51c2VyLmdhbWUgPSBOb25lCiAgICAgICAgaWYgY29u"
    "IGlzIHNlbGYuaG9zdDoKICAgICAgICAgICAgI1RoZSBob3N0ICppcyogdGhlIGdhbWUgc2Vzc2lv"
    "bjogdGhlIGNvLW9wIHdvcmxkIHJ1bnMgb24gdGhlaXIKICAgICAgICAgICAgI21hY2hpbmUgYW5k"
    "IHRoZSByb29tJ3MgRGlyZWN0UGxheSB1cmwgcG9pbnRzIGF0IGl0LiBPbmNlIHRoZXkgYXJlCiAg"
    "ICAgICAgICAgICNnb25lIHRoZSByb29tIGNhbm5vdCBiZSBqb2luZWQgYnkgYW55Ym9keSwgYnV0"
    "IGl0IHVzZWQgdG8gc3RheQogICAgICAgICAgICAjbGlzdGVkIC0gc28gdGhlIG5leHQgcGxheWVy"
    "IHRvIGNsaWNrIGl0IGdvdCBhIHVybCB0byBhIGdhbWUgdGhhdAogICAgICAgICAgICAjbm8gbG9u"
    "Z2VyIGV4aXN0ZWQgYW5kIHNhdCBvbiAiY29ubmVjdGluZyIgdW50aWwgdGhleSBnYXZlIHVwLgog"
    "ICAgICAgICAgICAjVGhpcyBpcyB3aGF0IGEgaG9zdCBjcmFzaCBsZWF2ZXMgYmVoaW5kLgogICAg"
    "ICAgICAgICBwcmludChmJ1tMb2JieV0gSG9zdCB7Y29uLnVzZXIubmFtZX0gbGVmdCByb29tICJ7"
    "c2VsZi5nbmFtZX0iLCBjbG9zaW5nIGl0JykKICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVy"
    "LmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAgICAgICAgICBz"
    "ZWxmLmRlc3Ryb3koKQogICAgICAgICAgICByZXR1cm4KICAgICAgICAjaWYgMCB1c2VycyBsZWZ0"
    "LCByZW1vdmUgZ2FtZQogICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KT09MDoKICAgICAgICAg"
    "ICAgbGVhdmVtc2cgPSBfZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpCiAgICAgICAgICAgIGRl"
    "bCBzZWxmLnBhcmVudC5nYW1lc1tzZWxmLmduYW1lXQogICAgICAgIHNlbGYucGFyZW50LnNlcnZl"
    "ci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgIGRlZiBzdGFy"
    "dEdhbWUoc2VsZiwgdXNlcj1Ob25lKToKICAgICAgICBpZiBub3QgKHVzZXIgYW5kIHNlbGYuaG9z"
    "dCA9PSB1c2VyKToKICAgICAgICAgICAgcmV0dXJuIE5vbmUgI3VzZXIgbm90IGhvc3QKICAgICAg"
    "ICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBzZWxmLnN0YXR1cyA9IDEKICAgICAgICBm"
    "b3IgYyBpbiBzZWxmLnVzZXJsaXN0OiNUT0RPIGhhdmUgdXNlciByZW1vdmUgaXRzZWxmIHdoZW4g"
    "L3N0YXJ0aW5nZ2FtZT8KICAgICAgICAgICAgdW4gPSBjLnVzZXIubmFtZQogICAgICAgICAgICAj"
    "VE9ETyBjb25zaWRlciByZW1vdmluZyB1c2VyIGZyb20gdGFyZ2V0IG93biBzZXQ/CiAgICAgICAg"
    "ICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOl9l"
    "bShmJyZjaGF0Y2hhbm5lbHVzZXIgInt1bn0iJykrX2VtKGYnJmdhbWVjaGFubmVsdXNlciAie3Vu"
    "fSInKX0pCiAgICAgICAgIy4uLmFuZCBhY3R1YWxseSB0YWtlIHRoZW0gb2ZmIHRoZSB0b3duIHJv"
    "c3Rlciwgd2hpY2ggdGhpcyBvbmx5IGV2ZXIKICAgICAgICAjKmFubm91bmNlZCouIExlYXZpbmcg"
    "dGhlbSBsaXN0ZWQgbWVhbnQgdGhlIHNlcnZlciBzdGlsbCBjb3VudGVkIHRoZW0KICAgICAgICAj"
    "YXMgc3RhbmRpbmcgaW4gdGhlIHRvd24gZm9yIHRoZSB3aG9sZSBzZXNzaW9uOiB0b3duIHBvcHVs"
    "YXRpb24gd2FzCiAgICAgICAgI3dyb25nLCBhbmQgZXZlcnkgcG9zaXRpb24gdXBkYXRlIGZyb20g"
    "YW55b25lIHN0aWxsIHdhbGtpbmcgYXJvdW5kIHdhcwogICAgICAgICNmYW5uZWQgb3V0IHRvIHBs"
    "YXllcnMgd2hvIHdlcmUgYXdheSBpbiBhIGNvLW9wIHdvcmxkIGFuZCBjb3VsZCBkbwogICAgICAg"
    "ICNub3RoaW5nIHdpdGggaXQuIFRoZSBjbGllbnRzIHdlcmUgdG9sZCB0aGV5IGxlZnQ7IG5vdyB0"
    "aGUgc2VydmVyCiAgICAgICAgI2FncmVlcyB3aXRoIHRoZW0uCiAgICAgICAgZm9yIGMgaW4gbGlz"
    "dChzZWxmLnVzZXJsaXN0KToKICAgICAgICAgICAgYy51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAg"
    "ICAgIGlmIGMgaW4gc2VsZi5wYXJlbnQudXNlcmxpc3Q6CiAgICAgICAgICAgICAgICBzZWxmLnBh"
    "cmVudC51c2VybGlzdC5yZW1vdmUoYykKICAgICAgICBpZiBub3Qgc2VsZi5ucGo6CiAgICAgICAg"
    "ICAgICNnYW1lIG5vIGxvbmdlciBqb2luYWJsZS92aXNpYmxlIG9uY2Ugc3RhcnRlZAogICAgICAg"
    "ICAgICBzZWxmLnBhcmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzpf"
    "ZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9IicpfSkKICAgICAgICAjbm90aWZ5IHBsYXllcnMgaW4g"
    "dGhlIGdhbWUgdGhhdCBpdCBoYXMgc3RhcnRlZAogICAgICAgIGZvciBjIGluIHNlbGYudXNlcmxp"
    "c3Q6CiAgICAgICAgICAgIGlzSG9zdCA9IDEgaWYgYyBpcyBzZWxmLmhvc3QgZWxzZSAwCiAgICAg"
    "ICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6KGMsKSwnbWVzc2Fn"
    "ZSc6X2VtKGYnL3N0YXJ0Z2FtZSAiMSIgIntpc0hvc3R9IiAiMSInKX0pCiAgICAgICAgcmV0dXJu"
    "IE5vbmUKICAgIGRlZiBfZ2V0VXNlcmxpc3Qoc2VsZik6CiAgICAgICAgcmV0dXJuICcgJy5qb2lu"
    "KCAoZicie2MudXNlci5uYW1lfSIgIiIgIjEwMCIgIjAiJyBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0"
    "KSApCiAgICBkZWYgZ2V0R2FtZVN0cmluZyhzZWxmKToKICAgICAgICBpZiBzZWxmLnN0YXR1cyBh"
    "bmQgbm90IHNlbGYubnBqOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjR2FtZSBkb2VzIG5vdCBz"
    "aG93IGlmIG5ldyBwbGF5ZXJzIGNhbid0IGpvaW4gd2hlbiBhY3RpdmUKICAgICAgICBwYXN3ID0g"
    "JycKICAgICAgICBpZiBzZWxmLnBhc3N3b3JkOgogICAgICAgICAgICBwYXN3ID0gJ1hYWCcKICAg"
    "ICAgICByZXR1cm4gX2VtKGYnJGdhbWUgIntzZWxmLmduYW1lfSIgIntwYXN3fSIgIntzZWxmLm1h"
    "cFBhcn0iICJ7c2VsZi5tYXBUcmFuc2xhdGV9IiAie3NlbGYudW4xfSIgIntzZWxmLnN0YXR1c30i"
    "ICJ7c2VsZi5tYXhwbGF5ZXJzfSIge3NlbGYuX2dldFVzZXJsaXN0KCl9JykKICAgIGRlZiBkZWJ1"
    "Z19kaWN0KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAgICAgICAgICduYW1lJzpzZWxmLmdu"
    "YW1lLAogICAgICAgICAgICAnaG9zdCc6c2VsZi5ob3N0LnVzZXIubmFtZSwKICAgICAgICAgICAg"
    "J3N0YXR1cyc6c2VsZi5zdGF0dXMsCiAgICAgICAgICAgICdoYXNQYXNzd29yZCc6MSBpZiBzZWxm"
    "LnBhc3N3b3JkIGVsc2UgMCwKICAgICAgICAgICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUg"
    "Zm9yIGMgaW4gc2VsZi51c2VybGlzdF0pLAogICAgICAgICAgICAndG93bic6c2VsZi5wYXJlbnQu"
    "bmFtZSwKICAgICAgICAgICAgJ3BhcmFtZXRlcnMnOnNlbGYubWFwUGFyLAogICAgICAgICAgICAn"
    "bWFwTmFtZSc6c2VsZi5tYXBUcmFuc2xhdGUsCiAgICAgICAgICAgICdjYW5Kb2luUnVubmluZyc6"
    "c2VsZi5ucGoKICAgICAgICB9CiMgdHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsCiMgdHJhbnNs"
    "YXRlTmV0Q2l0eVRyYWRlQ2hhbm5lbAojIHRyYW5zbGF0ZU5ldENpdHlDaGF0Q2hhbm5lbApfREVG"
    "QVVMVF9DSEFUUyA9IFsndHJhbnNsYXRlTmV0Q2l0eU1haW5DaGFubmVsJywndHJhbnNsYXRlTmV0"
    "Q2l0eVRyYWRlQ2hhbm5lbCddCmNsYXNzIEdhbWVDaGFubmVsKCk6CiAgICBtYXh1c2VyID0gNTAg"
    "I1RPRE8gY29uZmlndXJlYWJsZQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlciwgY2huTmFt"
    "ZSk6CiAgICAgICAgc2VsZi5zZXJ2ZXIgPSBzZXJ2ZXIKICAgICAgICBzZWxmLm5hbWUgPSBjaG5O"
    "YW1lCiAgICAgICAgc2VsZi51c2VybGlzdCA9IFtdCiAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHMg"
    "PSB7fQogICAgICAgIHNlbGYuZ2FtZXMgPSB7fSAjVE9ETyBmaWd1cmUgb3V0IEEgYW5kIEIgdmFs"
    "dWUgZm9yIGRpc3BsYXkKICAgICAgICAjVE9ETyByZXF1ZXN0IGpvaW4gcmVzZXJ2ZXMgc3BhY2Ug"
    "d2l0aCB3ZWFrIHJlZmVyZW5jZXMKICAgICAgICAjLSB3ZWFrIHZhbHVlIHJlZiBzaG91bGQgZW5z"
    "dXJlIHRoYXQgY29ubmVjdGlvbiBpcyByZW1vdmVkIGZyb20gcXVldWUgaWYgaXQgZGlzY29ubmVj"
    "dHMgZHVyaW5nIHRoZSBqb2luIHByb2Nlc3MKICAgICAgICBzZWxmLnJlcXVlc3RlZCA9IFtdCiAg"
    "ICAgICAgc2VsZi5nYW1lUmVxdWVzdHMgPSB7fQogICAgICAgIHNlbGYuZGlydHkgPSBGYWxzZQog"
    "ICAgICAgIGZvciBjbiBpbiBfREVGQVVMVF9DSEFUUzoKICAgICAgICAgICAgc2VsZi5jaGF0Q2hh"
    "bm5lbHNbY25dID0gW10gI1VzZXJsaXN0CiAgICBkZWYgcmVxdWVzdEpvaW4oc2VsZiwgY29uKToK"
    "ICAgICAgICAjbGVhdmVDaGFubmVsKCkgYWxyZWFkeSByZWxlYXNlcyBhbnkgb3V0c3RhbmRpbmcg"
    "cmVzZXJ2YXRpb24sIG9uIHRoaXMKICAgICAgICAjY2hhbm5lbCBvciBhbm90aGVyIG9uZS4gVGhl"
    "IGZvbGxvdy11cCBibG9jayB0aGF0IHVzZWQgdG8gc3RhbmQgaGVyZQogICAgICAgICNjb3VsZCB0"
    "aGVyZWZvcmUgbmV2ZXIgcnVuIC0gYW5kIGlmIGl0IGV2ZXIgaGFkLCBpdHMgdW5ndWFyZGVkCiAg"
    "ICAgICAgI2xpc3QucmVtb3ZlKCkgd291bGQgaGF2ZSByYWlzZWQgVmFsdWVFcnJvciBmb3IgYSBy"
    "ZXNlcnZhdGlvbiB0aGF0IHdhcwogICAgICAgICNhbHJlYWR5IGdvbmUuCiAgICAgICAgY29uLnVz"
    "ZXIubGVhdmVDaGFubmVsKCkKICAgICAgICBlbGVuID0gbGVuKHNlbGYudXNlcmxpc3QpK2xlbihz"
    "ZWxmLnJlcXVlc3RlZCkKICAgICAgICBpZiBlbGVuPHNlbGYubWF4dXNlcjoKICAgICAgICAgICAg"
    "c2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29uLnVzZXIucmVxdWVzdGVk"
    "Q2hhbm5lbCA9IHNlbGYKICAgICAgICAgICAgcmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gRmFs"
    "c2UKICAgIGRlZiBfaXNTdGFsZUdhbWUoc2VsZiwgZ2VudCwgY29uKToKICAgICAgICAjQSByb29t"
    "IHdob3NlIGhvc3QgaXMgbm8gbG9uZ2VyIHRoZSBsaXZlIHNlc3Npb24gZm9yIHRoYXQgYWNjb3Vu"
    "dC4gVGhlCiAgICAgICAgI2NsaWVudCBuYW1lcyBhIHJvb20gYWZ0ZXIgaXRzIGhvc3QsIHNvIHdo"
    "ZW4gYSBwbGF5ZXIgd2hvc2UgZ2FtZQogICAgICAgICNjcmFzaGVkIHJlY29ubmVjdHMgYW5kIGhv"
    "c3RzIGFnYWluLCB0aGUgcm9vbSBmcm9tIHRoZSBzZXNzaW9uIHRoYXQKICAgICAgICAjZGllZCBp"
    "cyBzdGlsbCBzaXR0aW5nIGhlcmUgdW5kZXIgdGhlIHNhbWUgbmFtZSAtIHdpdGggYSBob3N0CiAg"
    "ICAgICAgI2Nvbm5lY3Rpb24gdGhhdCBubyBsb25nZXIgZXhpc3RzIGFuZCBhIERpcmVjdFBsYXkg"
    "dXJsIHBvaW50aW5nIGF0IGEKICAgICAgICAjZ2FtZSB0aGF0IGlzIGdvbmUuIEFueW9uZSBqb2lu"
    "aW5nIGl0IHdhaXRzIGZvcmV2ZXIuCiAgICAgICAgaWYgZ2VudC5ob3N0IGlzIGNvbjoKICAgICAg"
    "ICAgICAgcmV0dXJuIFRydWUKICAgICAgICBob3N0bmFtZSA9IGdlbnQuaG9zdC51c2VyLm5hbWUg"
    "aWYgZ2VudC5ob3N0LnVzZXIgZWxzZSBOb25lCiAgICAgICAgaWYgaG9zdG5hbWUgaXMgTm9uZToK"
    "ICAgICAgICAgICAgcmV0dXJuIFRydWUKICAgICAgICByZXR1cm4gc2VsZi5zZXJ2ZXIuZ2V0UGxh"
    "eWVyKGhvc3RuYW1lKSBpcyBub3QgZ2VudC5ob3N0CiAgICBkZWYgcmVxdWVzdENyZWF0ZUdhbWUo"
    "c2VsZiwgY29uLCBnYW1lTmFtZSk6CiAgICAgICAgI05ldmVyIHJldHVybiBhIGJhcmUgRmFsc2Ug"
    "ZnJvbSBoZXJlLiBwYXJzZSgpIHRyZWF0cyBhIGZhbHN5IHJlc3VsdCBhcwogICAgICAgICMibm90"
    "aGluZyB0byBzZW5kIiwgc28gZXZlcnkgcmVqZWN0aW9uIGJlbG93IHVzZWQgdG8gbGVhdmUgdGhl"
    "IGNsaWVudAogICAgICAgICN3YWl0aW5nIG9uIGFuIGFuc3dlciB0aGF0IG5ldmVyIGNhbWUgLSB0"
    "aGUgcm9vbS1jcmVhdGlvbiBkaWFsb2cgdGhlbgogICAgICAgICNzcGlucyBmb3JldmVyLgogICAg"
    "ICAgIGlmIGNvbi51c2VyLnJlcXVlc3RlZEdhbWUgb3IgY29uLnVzZXIuZ2FtZToKICAgICAgICAg"
    "ICAgY29uLnVzZXIuc3RvcEdhbWUoKQogICAgICAgIHRjbiA9IHNlbGYuZ2FtZVJlcXVlc3RzLmdl"
    "dChnYW1lTmFtZSkKICAgICAgICBpZiB0Y24gaXMgbm90IE5vbmUgYW5kIHRjbiBpcyBub3QgY29u"
    "OgogICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVOYW1lVGFrZW4gIntnYW1lTmFt"
    "ZX0iJykKICAgICAgICAgICAgI2Vsc2UgdGNuIGlzIGNvbiwgcmUtcmVxdWVzdGVkIGNyZWF0aW9u"
    "CiAgICAgICAgZ2VudCA9IHNlbGYuZ2FtZXMuZ2V0KGdhbWVOYW1lKQogICAgICAgIGlmIGdlbnQg"
    "aXMgbm90IE5vbmU6CiAgICAgICAgICAgIGlmIHNlbGYuX2lzU3RhbGVHYW1lKGdlbnQsIGNvbik6"
    "CiAgICAgICAgICAgICAgICBwcmludChmJ1tMb2JieV0gUmVwbGFjaW5nIHN0YWxlIHJvb20gIntn"
    "YW1lTmFtZX0iICcKICAgICAgICAgICAgICAgICAgICAgIGYnKGhvc3Qgc2Vzc2lvbiBnb25lKSBh"
    "dCB0aGUgcmVxdWVzdCBvZiB7Y29uLnVzZXIubmFtZX0nKQogICAgICAgICAgICAgICAgZ2VudC5k"
    "ZXN0cm95KCkKICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgIHJldHVybiBfZW0oZicv"
    "ZXJyb3IgZ2FtZU5hbWVUYWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgIHNlbGYuZ2FtZVJlcXVl"
    "c3RzW2dhbWVOYW1lXSA9IGNvbgogICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZEdhbWUgPSBnYW1l"
    "TmFtZQogICAgICAgIHJldHVybiBfZW0oZicvY3JlYXRlZ2FtZSAie2dhbWVOYW1lfSInKQogICAg"
    "ZGVmIGNyZWF0ZUdhbWUoc2VsZiwgZ2FtZU5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5w"
    "aiwgdW4xLCB1bjIsIHVuMywgdXJsKToKICAgICAgICByZXFIb3N0ID0gc2VsZi5nYW1lUmVxdWVz"
    "dHMuZ2V0KGdhbWVOYW1lKQogICAgICAgIGlmIHJlcUhvc3QgaXMgTm9uZSBvciByZXFIb3N0IGlz"
    "IG5vdCBob3N0OgogICAgICAgICAgICAjU2FtZSByZWFzb25pbmcgYXMgYWJvdmU6IGFuc3dlciwg"
    "bmV2ZXIgZmFsbCBzaWxlbnQuCiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5h"
    "bWVUYWtlbiAie2dhbWVOYW1lfSInKQogICAgICAgIGdlbnQgPSBHYW1lRW50cnkoc2VsZiwgZ2Ft"
    "ZU5hbWUsIGhvc3QsIHBhc3csIG1hcHAsIG1hcHQsIG5waiwgdW4xLCB1bjIsIHVuMywgdXJsKQog"
    "ICAgICAgIHJlcUhvc3QudXNlci5yZXF1ZXN0ZWRHYW1lID0gTm9uZSAjVE9ETyByZW9nYW5pemUg"
    "YmV0dGVyCiAgICAgICAgZGVsIHNlbGYuZ2FtZVJlcXVlc3RzW2dhbWVOYW1lXQogICAgICAgIHJl"
    "dHVybiBOb25lCiAgICBkZWYgbGVhdmVDaGFubmVsKHNlbGYsIGNvbik6CiAgICAgICAgI1RoZSBj"
    "bGVhbnVwIHJ1bnMgd2hldGhlciBvciBub3QgdGhlIHBsYXllciBpcyBzdGlsbCBvbiB0aGUgdG93"
    "bgogICAgICAgICNyb3N0ZXIuIFNpbmNlIHN0YXJ0R2FtZSgpIHRha2VzIGl0cyBwbGF5ZXJzIG9m"
    "ZiB0aGF0IHJvc3RlciwgYQogICAgICAgICNwbGF5ZXIgd2hvIGxlYXZlcyAob3IgZGlzY29ubmVj"
    "dHMpIGZyb20gaW5zaWRlIGEgcnVubmluZyBnYW1lIHVzZWQgdG8KICAgICAgICAjc2tpcCBhbGwg"
    "b2YgdGhpczogdGhlaXIgcm9vbSB3YXMgbmV2ZXIgbGVmdCwgdGhlaXIgY2hhdCBjaGFubmVsIGtl"
    "cHQKICAgICAgICAjdGhlaXIgZW50cnksIGFuZCBnYW1lY2hhbm5lbCBzdGF5ZWQgcG9pbnRpbmcg"
    "YXQgYSB0b3duIHRoZXkgd2VyZSBubwogICAgICAgICNsb25nZXIgaW4uIE9ubHkgdGhlIHJvc3Rl"
    "ciByZW1vdmFsIGFuZCB0aGUgYW5ub3VuY2VtZW50IGFyZQogICAgICAgICNjb25kaXRpb25hbCBu"
    "b3cgLSBiZWNhdXNlIG9ubHkgdGhvc2UgZGVwZW5kIG9uIGJlaW5nIGxpc3RlZC4KICAgICAgICBs"
    "aXN0ZWQgPSBjb24gaW4gc2VsZi51c2VybGlzdAogICAgICAgIGNvbi51c2VyLnN0b3BHYW1lKCkK"
    "ICAgICAgICBjb24udXNlci5sZWF2ZUNoYXQoKQogICAgICAgIGlmIGxpc3RlZDoKICAgICAgICAg"
    "ICAgc2VsZi51c2VybGlzdC5yZW1vdmUoY29uKQogICAgICAgICAgICBsZWF2ZW1zZyA9IF9lbShm"
    "JyZnYW1lY2hhbm5lbHVzZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24uc2Vy"
    "dmVyLmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLnVzZXJsaXN0LCdtZXNzYWdlJzpsZWF2ZW1zZ30p"
    "CiAgICAgICAgY29uLnVzZXIuZ2FtZWNoYW5uZWw9Tm9uZQogICAgZGVmIGxlYXZlQ2hhdChzZWxm"
    "LCBjb24pOiAjVE9ETyBiZXR0ZXIgY2hhdGNoYW5uZWwgb2JqZWN0IGFuZCBtb3ZlIGl0IHRoZXJl"
    "LgogICAgICAgIGNvbi51c2VyLmxlYXZlQ2hhdCgpCiAgICAjVE9ETyBjaGFuZ2UgdGhlc2UgZnVu"
    "Y3Rpb25zIHRvIGFsc28gaGFuZGxlIG1lc3NhZ2UgZm9ybWluZwogICAgZGVmIGpvaW5DaGFubmVs"
    "KHNlbGYsIGNvbiwgbmFtKTojbW92ZXMgdXNlciBmcm9tIHF1ZXVlIHRvIHVzZXJsaXN0CiAgICAg"
    "ICAgaWYgY29uIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICNEdXBsaWNhdGUgL2pvaW5n"
    "YW1lY2hhbm5lbCBmb3IgYSB0b3duIHdlIGFyZSBhbHJlYWR5IGluLiBSZWJ1aWxkCiAgICAgICAg"
    "ICAgICN0aGUgcmVzZXJ2YXRpb24gc28gdGhlIHJlcXVlc3QgYmVsb3cgcmUtcnVucyB0aGUgZnVs"
    "bCBlbnVtZXJhdGlvbgogICAgICAgICAgICAjYW5kIHRoZSBjbGllbnQgZ2V0cyBhIGNvbXBsZXRl"
    "IGFuc3dlciByYXRoZXIgdGhhbiBzaWxlbmNlLgogICAgICAgICAgICBzZWxmLnVzZXJsaXN0LnJl"
    "bW92ZShjb24pCiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAg"
    "ICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgaWYgY29uIG5vdCBp"
    "biBzZWxmLnJlcXVlc3RlZCBhbmQgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAgICAgICAg"
    "ICAjTm8gb3V0c3RhbmRpbmcgcmVzZXJ2YXRpb24uIFRoZSByZXNlcnZhdGlvbiBpcyBkcm9wcGVk"
    "IGJ5IGFueQogICAgICAgICAgICAjaW50ZXJ2ZW5pbmcgbGVhdmVDaGFubmVsKCkvcmVxdWVzdEpv"
    "aW4oKSBhbmQgYnkgYSByZWNvbm5lY3QsIHNvIGEKICAgICAgICAgICAgI2NsaWVudCB0aGF0IGdv"
    "ZXMgc3RyYWlnaHQgdG8gL2pvaW5nYW1lY2hhbm5lbCAtIG9yIHdob3NlIGVhcmxpZXIKICAgICAg"
    "ICAgICAgIy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsIHJhY2VkIGl0cyBvd24gY2xlYW51cCAtIHVz"
    "ZWQgdG8gZ2V0IG5vCiAgICAgICAgICAgICNhbnN3ZXIgYXQgYWxsIGFuZCBoYW5nIG9uIHRoZSBs"
    "b2FkaW5nIHNjcmVlbi4gQWRtaXQgdGhlbSBpZiB0aGUKICAgICAgICAgICAgI3Rvd24gaGFzIHJv"
    "b207IG9ubHkgYSBnZW51aW5lbHkgZnVsbCB0b3duIGlzIHJlZnVzZWQgbm93LgogICAgICAgICAg"
    "ICBpZiBsZW4oc2VsZi51c2VybGlzdCkrbGVuKHNlbGYucmVxdWVzdGVkKSA8IHNlbGYubWF4dXNl"
    "cjoKICAgICAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLmFwcGVuZChjb24pCiAgICAgICAgICAg"
    "ICAgICBjb24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gc2VsZgogICAgICAgICAgICBlbHNlOgog"
    "ICAgICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQ2hhbm5lbEZ1bGwgIntuYW19"
    "IicpCiAgICAgICAgaWYgY29uIGluIHNlbGYucmVxdWVzdGVkOgogICAgICAgICAgICAjVE9ETyB2"
    "ZXJpZnkgb3JkZXIgb2Ygb3BlcmF0aW9ucyBhbmQgcG9zc2libGUgdGltaW5nIGlzc3VlcwogICAg"
    "ICAgICAgICBzZWxmLnVzZXJsaXN0LmFwcGVuZChjb24pCiAgICAgICAgICAgIGNvbi51c2VyLmdh"
    "bWVjaGFubmVsID0gc2VsZgogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZC5yZW1vdmUoY29uKQog"
    "ICAgICAgICAgICBjb24udXNlci5yZXF1ZXN0ZWRDaGFubmVsID0gTm9uZSAjVE9ETyBvcmdhbml6"
    "ZSBiZXR0ZXI/CiAgICAgICAgICAgIHVsID0gbGVuKHNlbGYudXNlcmxpc3QpCiAgICAgICAgICAg"
    "IHJldG1zZyA9IF9lbShmJy9qb2luZ2FtZWNoYW5uZWwgIntuYW19IiAie3VsfSInKQogICAgICAg"
    "ICAgICAjZW51bWVyYXRlIGhlcm9kYXRhIG9mIGV4aXN0aW5nIHVzZXJzCiAgICAgICAgICAgIGNo"
    "dW5rcyA9IFtdCiAgICAgICAgICAgIGZvciB1c2VyIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAg"
    "ICAgICAgICBpZiB1c2VyID09IGNvbjoKICAgICAgICAgICAgICAgICAgICBjb250aW51ZQogICAg"
    "ICAgICAgICAgICAgY2h1bmtzLmFwcGVuZCh1c2VyLnVzZXIuZ2V0R0NVbXNnKCkpCiAgICAgICAg"
    "ICAgIHJldG1zZys9IGInJy5qb2luKGNodW5rcykKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5q"
    "b2luQ2hhdChjb24sIF9ERUZBVUxUX0NIQVRTWzBdKQogICAgICAgICAgICByZXRtc2crPSBzZWxm"
    "LmVudW1DaGF0cygpCiAgICAgICAgICAgIHJldG1zZys9IHNlbGYuZW51bUdhbWVzKCkKICAgICAg"
    "ICAgICAgI2Jyb2FkY2FzdCBoZXJvZGF0YSB0byBvdGhlciBleGlzdGluZyB1c2VycwogICAgICAg"
    "ICAgICBjb24uc2VydmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgICAgICd0YXJnZXQnOl93b1Vz"
    "ZXIoc2VsZi51c2VybGlzdCwgY29uKSwKICAgICAgICAgICAgICAgICdtZXNzYWdlJzpjb24udXNl"
    "ci5nZXRHQ1Vtc2coKX0pCiAgICAgICAgICAgIHJldHVybiByZXRtc2cKICAgICAgICByZXR1cm4g"
    "Tm9uZQogICAgZGVmIGpvaW5DaGF0KHNlbGYsIGNvbiwgbmFtLCBwYXM9JycpOgogICAgICAgICNU"
    "T0RPIHBhc3N3b3JkIHN1cHBvcnQ/CiAgICAgICAgIy0gcmVxdWlyZXMgcmVzdHJ1Y3R1cmUgZnJv"
    "bSBsaXN0IHRvIGNoYW5uZWwgb2JqZWN0cwogICAgICAgIGlmIG5vdCBuYW0gaW4gc2VsZi5jaGF0"
    "Q2hhbm5lbHM6CiAgICAgICAgICAgIHJldHVybiBiJycKICAgICAgICBjb24udXNlci5sZWF2ZUNo"
    "YXQoKQogICAgICAgICNUT0RPIGNoZWNrIGlmIGNsaWVudCBhdXRvLXB1cmdlcyBjaGF0bGlzdAog"
    "ICAgICAgICNGdWxsIGZvdXItZmllbGQgZm9ybSAobmFtZSwgZ3VpbGQsIGZsYWdzLCBndWlkKSwg"
    "d2hpY2ggaXMgd2hhdCB0aGUKICAgICAgICAjY2xpZW50IGlzIGRvY3VtZW50ZWQgdG8gc2VuZCBh"
    "bmQgd2hhdCBnZXRDQ1Vtc2coKSBleGlzdHMgdG8gYnVpbGQgLQogICAgICAgICNzZWUgdGhlIGNh"
    "cHR1cmUgbm90ZWQgbmV4dCB0byBpdC4gQm90aCBhbm5vdW5jZW1lbnRzIGhlcmUgdXNlZCB0byBl"
    "bWl0CiAgICAgICAgI2Egb25lLWZpZWxkICckY2hhdGNoYW5uZWx1c2VyICJuYW1lIicgaW5zdGVh"
    "ZCwgc28gdGhlIGd1aWxkIGNvbHVtbiB3YXMKICAgICAgICAjYWx3YXlzIGJsYW5rIGluIGNoYXQg"
    "bm8gbWF0dGVyIHdoYXQgZ3VpbGQgYSBwbGF5ZXIgd2FzIGluLCBhbmQgdGhlCiAgICAgICAgI2Ns"
    "aWVudCBoYWQgdG8gZmlsbCB0aHJlZSBmaWVsZHMgaXQgd2FzIG5ldmVyIGdpdmVuLiBUaGUgJGdh"
    "bWVjaGFubmVsdXNlcgogICAgICAgICNwYXRoIG5leHQgZG9vciBoYXMgYWx3YXlzIHNlbnQgaXRz"
    "IGZ1bGwgZm9ybTsgdGhlc2UgdHdvIHdlcmUgdGhlCiAgICAgICAgI3N0cmFnZ2xlcnMuCiAgICAg"
    "ICAgY29uLnNlcnZlci5kaXN0LmFkZCh7CiAgICAgICAgICAgICd0YXJnZXQnOmxpc3Qoc2VsZi5j"
    "aGF0Q2hhbm5lbHNbbmFtXSksCiAgICAgICAgICAgICdtZXNzYWdlJzpjb24udXNlci5nZXRDQ1Vt"
    "c2coKX0pCiAgICAgICAgc2VsZi5jaGF0Q2hhbm5lbHNbbmFtXS5hcHBlbmQoY29uKQogICAgICAg"
    "IGNvbi51c2VyLmNoYXRjaGFubmVsID0gc2VsZi5jaGF0Q2hhbm5lbHNbbmFtXQogICAgICAgIHVs"
    "ID0gMSNsZW4oY29uLnVzZXIuY2hhdGNoYW5uZWwpCiAgICAgICAgcmV0bXNnID0gX2VtKGYnL2pv"
    "aW5jaGF0Y2hhbm5lbCAie25hbX0iICIiICJ7dWx9IicpCiAgICAgICAgI2VudW1lcmF0ZSBvdGhl"
    "ciBjaGF0IHVzZXJzPwogICAgICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIHVjb24gaW4gbGlz"
    "dChjb24udXNlci5jaGF0Y2hhbm5lbCk6CiAgICAgICAgICAgIGlmIHVjb24gIT0gY29uOgogICAg"
    "ICAgICAgICAgICAgY2h1bmtzLmFwcGVuZCh1Y29uLnVzZXIuZ2V0Q0NVbXNnKCkpCiAgICAgICAg"
    "cmV0bXNnKz1iJycuam9pbihjaHVua3MpCiAgICAgICAgcmV0dXJuIHJldG1zZwogICAgZGVmIGVu"
    "dW1DaGF0cyhzZWxmKToKICAgICAgICBjaHVua3MgPSBbXQogICAgICAgIGZvciBjaGF0TmFtZSBp"
    "biBzZWxmLmNoYXRDaGFubmVsczoKICAgICAgICAgICAgdWxsID0gbGVuKHNlbGYuY2hhdENoYW5u"
    "ZWxzW2NoYXROYW1lXSkjVE9ETyBpbXByb3ZlCiAgICAgICAgICAgIGNodW5rcy5hcHBlbmQod2ly"
    "ZV9lbmNvZGUoZickY2hhdGNoYW5uZWwgIntjaGF0TmFtZX0iICIiICJ7dWxsfSInKSkKICAgICAg"
    "ICByZXR1cm4gX04uam9pbihjaHVua3MpK19OCiAgICBkZWYgZW51bUdhbWVzKHNlbGYpOgogICAg"
    "ICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIGduYW1lIGluIHNlbGYuZ2FtZXM6CiAgICAgICAg"
    "ICAgIGdhbWVzdHIgPSBzZWxmLmdhbWVzW2duYW1lXS5nZXRHYW1lU3RyaW5nKCkKICAgICAgICAg"
    "ICAgaWYgZ2FtZXN0cjoKICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQoZ2FtZXN0cikKICAg"
    "ICAgICByZXR1cm4gYicnLmpvaW4oY2h1bmtzKQogICAgZGVmIHVwZGF0ZVBvcyhzZWxmLCBtZCk6"
    "CiAgICAgICAgaWYgbm90IHNlbGYuZGlydHk6CiAgICAgICAgICAgIHJldHVybgogICAgICAgICND"
    "bGVhcmVkIEJFRk9SRSB0aGUgc2Nhbiwgbm90IGFmdGVyLiBBIC91cGRoZXJvcG9zIHRoYXQgYXJy"
    "aXZlZCB3aGlsZQogICAgICAgICN0aGUgbG9vcCBiZWxvdyB3YXMgcnVubmluZyB1c2VkIHRvIHNl"
    "dCBkaXJ0eT1UcnVlIGFuZCB0aGVuIGhhdmUgaXQKICAgICAgICAjaW1tZWRpYXRlbHkgY2xlYXJl"
    "ZCBhZ2Fpbiwgc28gdGhhdCBwbGF5ZXIncyBtb3ZlIHdhcyBub3QgYnJvYWRjYXN0CiAgICAgICAg"
    "I3VudGlsIHNvbWVib2R5IGVsc2UgaGFwcGVuZWQgdG8gbW92ZS4gQ2xlYXJpbmcgZmlyc3QgbWVh"
    "bnMgdGhlIHdvcnN0CiAgICAgICAgI2Nhc2UgaXMgb25lIHJlZHVuZGFudCBwYXNzLCBub3QgYSBz"
    "aWxlbnRseSBkcm9wcGVkIHBvc2l0aW9uLgogICAgICAgIHNlbGYuZGlydHkgPSBGYWxzZQogICAg"
    "ICAgICNTbmFwc2hvdDogcGxheWVycyBqb2luIGFuZCBsZWF2ZSB0aGUgdG93biB3aGlsZSB0aGlz"
    "IGl0ZXJhdGVzLgogICAgICAgIHRnID0gbGlzdChzZWxmLnVzZXJsaXN0KQogICAgICAgIG1vdmVy"
    "cyA9IFtdCiAgICAgICAgZm9yIHVjb24gaW4gdGc6CiAgICAgICAgICAgIGlmIG5vdCB1Y29uLnVz"
    "ZXIucG9zY2hhbmdlZDoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHVjb24u"
    "dXNlci5wb3NjaGFuZ2VkID0gRmFsc2UKICAgICAgICAgICAgaWYgbm90IHVjb24udXNlci5oZXJv"
    "ZGF0YToKICAgICAgICAgICAgICAgICNBIHBsYXllciBpcyBvbmx5IGFubm91bmNlZCB0byB0aGUg"
    "b3RoZXJzIGJ5ICRnYW1lY2hhbm5lbHVzZXIsCiAgICAgICAgICAgICAgICAjYW5kIGdldEdDVW1z"
    "ZygpIGVtaXRzIG5vdGhpbmcgYXQgYWxsIHVudGlsIHRoZWlyIGhlcm9kYXRhIGhhcwogICAgICAg"
    "ICAgICAgICAgI2Fycml2ZWQuIEJyb2FkY2FzdGluZyBhIHBvc2l0aW9uIGZvciBhIGhlcm8gaWQg"
    "bm9ib2R5IGhhcwogICAgICAgICAgICAgICAgI2JlZW4gdG9sZCBhYm91dCBoYW5kcyBldmVyeSBj"
    "bGllbnQgYW4gdXBkYXRlIGZvciBhIHBsYXllciBpdAogICAgICAgICAgICAgICAgI2RvZXMgbm90"
    "IGtub3cgZXhpc3RzLiBXYWl0IHVudGlsIHRoZXkgYXJlIGEgcmVhbCwgYW5ub3VuY2VkCiAgICAg"
    "ICAgICAgICAgICAjcGxheWVyLgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAg"
    "bW92ZXJzLmFwcGVuZCgodWNvbiwgZid7dWNvbi51c2VyLndpcmVJZCgpfSN7dWNvbi51c2VyLnBv"
    "c2RhdGF9JykpCiAgICAgICAgaWYgbm90IG1vdmVyczoKICAgICAgICAgICAgI0V2ZXJ5b25lIHdo"
    "byB3YXMgZGlydHkgaGFzIHNpbmNlIGxlZnQgdGhlIHRvd24uIFNlbmRpbmcgdGhlCiAgICAgICAg"
    "ICAgICNhcmd1bWVudC1sZXNzICcvdXBkaGVyb3BvcyAnIHRoYXQgdGhpcyB1c2VkIHRvIHByb2R1"
    "Y2UganVzdCBoYW5kcwogICAgICAgICAgICAjdGhlIGNsaWVudCBhbiBlbXB0eSBjb21tYW5kIHRv"
    "IHBhcnNlLgogICAgICAgICAgICByZXR1cm4KICAgICAgICAjTm9ib2R5IGlzIHRvbGQgdGhlaXIg"
    "b3duIHBvc2l0aW9uLiBUaGUgY2xpZW50IGlzIHRoZSBhdXRob3JpdHkgb24KICAgICAgICAjd2hl"
    "cmUgaXRzIG93biBoZXJvIGlzIC0gaXQgaXMgd2hhdCBzZW50IHRoZSBjb29yZGluYXRlcyBpbiB0"
    "aGUgZmlyc3QKICAgICAgICAjcGxhY2UgLSBzbyBlY2hvaW5nIHRoZW0gYmFjayBhIGZyYWN0aW9u"
    "IG9mIGEgc2Vjb25kIGxhdGVyIGlzIGF0IGJlc3QKICAgICAgICAjcmVkdW5kYW50IGFuZCBhdCB3"
    "b3JzdCBhIGhpdGNoLCBhcyB0aGUgaGVybyBpcyBudWRnZWQgYmFjayB0byB3aGVyZQogICAgICAg"
    "ICNpdCBzdG9vZCB3aGVuIHRoZSBwYWNrZXQgbGVmdC4gRXZlcnkgb3RoZXIgYnJvYWRjYXN0IGlu"
    "IHRoaXMgZmlsZQogICAgICAgICNhbHJlYWR5IGV4Y2x1ZGVzIHRoZSBvcmlnaW5hdG9yIChzZWUg"
    "X3dvVXNlcik7IHBvc2l0aW9ucyB3ZXJlIHRoZQogICAgICAgICNleGNlcHRpb24uIENvc3RzIG9u"
    "ZSBtZXNzYWdlIGJ1aWx0IHBlciBtb3ZpbmcgcGxheWVyLCBhbmQgbm90IG9uZQogICAgICAgICNl"
    "eHRyYSBieXRlIG9uIHRoZSB3aXJlOiB0aGUgZGlzdHJpYnV0b3IgYWxyZWFkeSB3cml0ZXMgdG8g"
    "ZWFjaAogICAgICAgICNyZWNpcGllbnQgc2VwYXJhdGVseS4KICAgICAgICBtb3ZlZCA9IHNldCh1"
    "IGZvciAodSwgXykgaW4gbW92ZXJzKQogICAgICAgIHdhdGNoZXJzID0gW2MgZm9yIGMgaW4gdGcg"
    "aWYgYyBub3QgaW4gbW92ZWRdCiAgICAgICAgaWYgd2F0Y2hlcnM6CiAgICAgICAgICAgIGZvciBt"
    "c2cgaW4gc2VsZi5fcG9zTWVzc2FnZXMoW2NoIGZvciAoXywgY2gpIGluIG1vdmVyc10pOgogICAg"
    "ICAgICAgICAgICAgbWQuYWRkKHsndGFyZ2V0Jzp3YXRjaGVycywnbWVzc2FnZSc6bXNnfSkKICAg"
    "ICAgICBmb3IgKHVjb24sIF8pIGluIG1vdmVyczoKICAgICAgICAgICAgb3RoZXJzID0gW2NoIGZv"
    "ciAodSwgY2gpIGluIG1vdmVycyBpZiB1IGlzIG5vdCB1Y29uXQogICAgICAgICAgICBpZiBub3Qg"
    "b3RoZXJzOgogICAgICAgICAgICAgICAgY29udGludWUgI29ubHkgbW92ZXIgaW4gdGhlIHRvd24s"
    "IG5vdGhpbmcgdG8gdGVsbCB0aGVtCiAgICAgICAgICAgIGZvciBtc2cgaW4gc2VsZi5fcG9zTWVz"
    "c2FnZXMob3RoZXJzKToKICAgICAgICAgICAgICAgIG1kLmFkZCh7J3RhcmdldCc6KHVjb24sICks"
    "J21lc3NhZ2UnOm1zZ30pCiAgICBkZWYgX3Bvc01lc3NhZ2VzKHNlbGYsIGNodW5rcyk6CiAgICAg"
    "ICAgI1NwbGl0IGludG8gc2V2ZXJhbCBjb21tYW5kcyByYXRoZXIgdGhhbiBvbmUgYXJiaXRyYXJp"
    "bHkgbG9uZyBsaW5lLgogICAgICAgICMvdXBkaGVyb3BvcyBpcyB0aGUgb25seSBtZXNzYWdlIHdo"
    "b3NlIGxlbmd0aCBncm93cyB3aXRoIHRoZSBudW1iZXIgb2YKICAgICAgICAjcGxheWVycyAtIGEg"
    "YnVzeSB0b3duIHdvdWxkIHB1dCBmaWZ0eSAiaWQjeCN5IiBncm91cHMgb24gYSBzaW5nbGUKICAg"
    "ICAgICAjbGluZS4gVGhlIHJldGFpbCBjbGllbnQgaXMgYSAyMDA4IDMyLWJpdCBiaW5hcnkgYW5k"
    "IGl0cyBsb2JieSBwYXJzZXIKICAgICAgICAjY2FuIGJlIGFzc3VtZWQgdG8gdXNlIGZpeGVkLXNp"
    "emUgYnVmZmVyczsgaGFuZGluZyBpdCBhIGxpbmUgbG9uZ2VyCiAgICAgICAgI3RoYW4gaXQgZXhw"
    "ZWN0cyBpcyB0aGUgY2xhc3NpYyB3YXkgdG8gY29ycnVwdCBpdHMgaGVhcCBhbmQgdGFrZSBpdAog"
    "ICAgICAgICNkb3duIHdpdGggYW4gYWNjZXNzIHZpb2xhdGlvbiBzb21ld2hlcmUgZWxzZSBlbnRp"
    "cmVseS4gU2V2ZXJhbCBzaG9ydAogICAgICAgICNjb21tYW5kcyBhcmUgZXF1aXZhbGVudCBmb3Ig"
    "dGhlIGNsaWVudCBhbmQgY29zdCBvbmUgZXh0cmEgaGVhZGVyCiAgICAgICAgI2VhY2guCiAgICAg"
    "ICAgYmF0Y2hlcyA9IFtdCiAgICAgICAgY3VyID0gW10KICAgICAgICBwcmVmaXggPSBsZW4oJy91"
    "cGRoZXJvcG9zICcpCiAgICAgICAgY3VybGVuID0gcHJlZml4ICN0aGUgY29tbWFuZCB3b3JkIGNv"
    "dW50cyB0b3dhcmRzIHRoZSBsaW5lLCBpdCB3YXMgbm90CiAgICAgICAgICAgICAgICAgICAgICAg"
    "ICNiZWluZyBjb3VudGVkLCBzbyBhIGZ1bGwgYmF0Y2ggb3ZlcnNob3QgdGhlIGNhcCBieSAxMgog"
    "ICAgICAgIGZvciBjaCBpbiBjaHVua3M6CiAgICAgICAgICAgIGlmIGN1ciBhbmQgY3VybGVuICsg"
    "bGVuKGNoKSArIDEgPiBfTUFYX1dJUkVfTElORToKICAgICAgICAgICAgICAgIGJhdGNoZXMuYXBw"
    "ZW5kKGN1cikKICAgICAgICAgICAgICAgIGN1ciA9IFtdCiAgICAgICAgICAgICAgICBjdXJsZW4g"
    "PSBwcmVmaXgKICAgICAgICAgICAgY3VyLmFwcGVuZChjaCkKICAgICAgICAgICAgY3VybGVuICs9"
    "IGxlbihjaCkgKyAxCiAgICAgICAgaWYgY3VyOgogICAgICAgICAgICBiYXRjaGVzLmFwcGVuZChj"
    "dXIpCiAgICAgICAgcmV0dXJuIFtfZW0oJy91cGRoZXJvcG9zICcgKyAnICcuam9pbihiKSkgZm9y"
    "IGIgaW4gYmF0Y2hlc10KICAgIGRlZiBkZWJ1Z19hcnJfZ2FtZXMoc2VsZik6CiAgICAgICAgYWN0"
    "RGljdCA9IFtdCiAgICAgICAgZm9yIGduLCBnIGluIGxpc3Qoc2VsZi5nYW1lcy5pdGVtcygpKToK"
    "ICAgICAgICAgICAgYWN0RGljdC5hcHBlbmQoZy5kZWJ1Z19kaWN0KCkpCiAgICAgICAgcmV0dXJu"
    "IGFjdERpY3QKICAgIGRlZiBkZWJ1Z19kaWN0KHNlbGYpOgogICAgICAgIHJldHVybiB7CiAgICAg"
    "ICAgICAgICd1c2Vycyc6dHVwbGUoW2MudXNlci5uYW1lIGZvciBjIGluIHNlbGYudXNlcmxpc3Rd"
    "KSwKICAgICAgICAgICAgJ21heFVzZXJzJzpzZWxmLm1heHVzZXIsCiAgICAgICAgICAgICdnYW1l"
    "cyc6dHVwbGUoW2duIGZvciBnbiBpbiBzZWxmLmdhbWVzXSkKICAgICAgICB9CgpfTUFQTkFNRVMg"
    "PSBbJ05ldF9UXzAxJywnTmV0X1RfMDInLCdOZXRfVF8wMycsJ05ldF9UXzA0J10gI1RPRE8gdXNl"
    "IENGRyBvYmplY3QKY2xhc3MgR2FtZVN0YXRlKCk6CiAgICAjVE9ETyBhdXRvIGdyb3dhYmxlIGNo"
    "YW5uZWxzLCBbbWFwbmFtZV0KICAgICNUT0RPIGF2YWlsYWJsZSBpbmRleGVzLCBbbWFwbmFtZV0K"
    "ICAgIGRlZiBfX2luaXRfXyhzZWxmLCBzZXJ2ZXIpOgogICAgICAgICNpbnN0YW5jZSBhdHRyaWJ1"
    "dGVzLCBub3QgY2xhc3MgYXR0cmlidXRlczogdGhlc2UgbXVzdCBOT1QgYmUgc2hhcmVkCiAgICAg"
    "ICAgI2JldHdlZW4gc2VwYXJhdGUgQ29yZVNlcnZlciBpbnN0YW5jZXMgKGUuZy4gc3RvcC9zdGFy"
    "dCBmcm9tIGEgR1VJCiAgICAgICAgI3dpdGhpbiB0aGUgc2FtZSBwcm9jZXNzKSBvciBsZWZ0b3Zl"
    "ciBwbGF5ZXJzL2NoYW5uZWxzIGZyb20gYQogICAgICAgICNwcmV2aW91cyBydW4gd291bGQgbGVh"
    "ayBpbnRvIHRoZSBuZXcgb25lLgogICAgICAgIHNlbGYuYWN0aXZlVXNlcnMgPSB7fSAjVE9ETyB0"
    "cmFjayB1c2VyIGhpc3Rvcnk/IG9wdGlvbmFsbHkKICAgICAgICBzZWxmLmdhbWVDaGFubmVscyA9"
    "IHt9ICNjaGFubmVsW10sIGtleWVkIGJ5IG1hcG5hbWUKICAgICAgICBzZWxmLnNlcnZlcj1zZXJ2"
    "ZXIKICAgICAgICBzZWxmLnVzZXJMb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIGZvciBu"
    "YW1lIGluIF9NQVBOQU1FUzoKICAgICAgICAgICAgZm9yIGkgaW4gcmFuZ2UoMSk6ICNUT0RPIGNv"
    "bmZpZ3VyZWFibGUgdXAgdG8gMjA/CiAgICAgICAgICAgICAgICBjaG5OYW1lID0gX2djaG5sKG5h"
    "bWUsIDEraSkKICAgICAgICAgICAgICAgIHNlbGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdID0gR2Ft"
    "ZUNoYW5uZWwoc2VsZi5zZXJ2ZXIsIGNobk5hbWUpICNUT0RPIDEgYW5kIGdyb3c/CiAgICBkZWYg"
    "Y2xhaW1Vc2VyKHNlbGYsIG5hbWUsIGNvbik6CiAgICAgICAgI1B1Ymxpc2ggY29uIGFzIFRIRSBs"
    "aXZlIHNlc3Npb24gZm9yIG5hbWUsIGF0b21pY2FsbHkuIFRoZSBvbGQgY29kZQogICAgICAgICNj"
    "aGVja2VkIGdldFBsYXllcigpIGR1cmluZyBsb2dpbiBhbmQgdGhlbiBpbnNlcnRlZCBpbnRvIGFj"
    "dGl2ZVVzZXJzCiAgICAgICAgI211Y2ggbGF0ZXIsIGluIF9sb2JieUhhbmRsZTsgdHdvIGNvbm5l"
    "Y3Rpb25zIGxvZ2dpbmcgaW4gYXMgdGhlIHNhbWUKICAgICAgICAjYWNjb3VudCBhdCBvbmNlIGJv"
    "dGggcGFzc2VkIHRoZSBjaGVjaywgYW5kIHRoZSBzZWNvbmQgb25lJ3MgaW5zZXJ0CiAgICAgICAg"
    "I292ZXJ3cm90ZSB0aGUgZmlyc3QuIFRoZSBsb3NlciB0aGVuIGRlbGV0ZWQgdGhlIHdpbm5lcidz"
    "IGVudHJ5IHdoZW4gaXQKICAgICAgICAjZGlzY29ubmVjdGVkLCBsZWF2aW5nIGEgY29ubmVjdGVk"
    "IHBsYXllciBpbnZpc2libGUgdG8gdGhlIHNlcnZlciAobm8KICAgICAgICAja2ljaywgbm8gd2hv"
    "aXMsIG5vIG1lc3NhZ2VzKS4KICAgICAgICB3aXRoIHNlbGYudXNlckxvY2s6CiAgICAgICAgICAg"
    "IGlmIG5hbWUgaW4gc2VsZi5hY3RpdmVVc2VyczoKICAgICAgICAgICAgICAgIHJldHVybiBGYWxz"
    "ZQogICAgICAgICAgICBzZWxmLmFjdGl2ZVVzZXJzW25hbWVdID0gY29uCiAgICAgICAgICAgIHJl"
    "dHVybiBUcnVlCiAgICBkZWYgcmVsZWFzZVVzZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAj"
    "b25seSBjbGVhciB0aGUgc2xvdCBpZiB3ZSBzdGlsbCBvd24gaXQsIG5ldmVyIHNvbWVvbmUgZWxz"
    "ZSdzIHNlc3Npb24KICAgICAgICB3aXRoIHNlbGYudXNlckxvY2s6CiAgICAgICAgICAgIGlmIHNl"
    "bGYuYWN0aXZlVXNlcnMuZ2V0KG5hbWUpIGlzIGNvbjoKICAgICAgICAgICAgICAgIGRlbCBzZWxm"
    "LmFjdGl2ZVVzZXJzW25hbWVdCiAgICBkZWYgZW51bWVyYXRlR0Moc2VsZik6CiAgICAgICAgY2hu"
    "cyA9IFtdCiAgICAgICAgZm9yIGNobk5hbWUgaW4gc2VsZi5nYW1lQ2hhbm5lbHM6CiAgICAgICAg"
    "ICAgIGNobiA9IHNlbGYuZ2FtZUNoYW5uZWxzW2Nobk5hbWVdCiAgICAgICAgICAgIGNobnMuYXBw"
    "ZW5kKHdpcmVfZW5jb2RlKGYnJGdhbWVjaGFubmVsICJ7Y2huTmFtZX0iICJ7bGVuKGNobi51c2Vy"
    "bGlzdCl9IiAie2Nobi5tYXh1c2VyfSIgIjAiICIwIicpKSAjVE9ETyBBdmFpbGFibGUgLSBBbGwK"
    "ICAgICAgICByZXR1cm4gX04uam9pbihjaG5zKStfTgogICAgZGVmIHVwZGF0ZVBvcyhzZWxmKToK"
    "ICAgICAgICBtZCA9IHNlbGYuc2VydmVyLmRpc3QKICAgICAgICBmb3IgY2huIGluIGxpc3Qoc2Vs"
    "Zi5nYW1lQ2hhbm5lbHMudmFsdWVzKCkpOgogICAgICAgICAgICBjaG4udXBkYXRlUG9zKG1kKQoj"
    "aGFuZGxlcyBpbnRlcmFjdGlvbnMgYmV0d2VlbiBhbGwgZWxlbWVudHMKY2xhc3MgQ29yZVNlcnZl"
    "cihzb2NrZXRzZXJ2ZXIuVGhyZWFkaW5nVENQU2VydmVyKToKICAgIGFsbG93X3JldXNlX2FkZHJl"
    "c3MgPSBUcnVlICMgVE9ETyBjaGVjayBpZiBpbXByb3ZlcyByZXN0YXJ0IHRpbWVzIHdpdGhvdXQg"
    "b3RoZXIgaXNzdWVzCiAgICBkYWVtb25fdGhyZWFkcyA9IFRydWUKICAgIGJsb2NrX29uX2Nsb3Nl"
    "ID0gRmFsc2UKICAgIF9pc19jbG9zaW5nID0gRmFsc2UKICAgIGRlZiBfX2luaXRfXyhzZWxmKToK"
    "ICAgICAgICAjVE9ETyBnZXQgdmFsdWVzIGZyb20gY2ZnCiAgICAgICAgI2FkZHJlc3MgPSAnbG9j"
    "YWxob3N0JwogICAgICAgIGFkZHJlc3MgPSAnJwogICAgICAgIHBvcnQgPSBfVFdfTE9CQllfUE9S"
    "VAogICAgICAgIHByaW50KGYnSW5pdGlhbGl6aW5nIHNlcnZlciBmb3IgcG9ydCB7cG9ydH0nKQog"
    "ICAgICAgIHN1cGVyKCkuX19pbml0X18oKGFkZHJlc3MsIHBvcnQpLCBDb25uZWN0aW9uSGFuZGxl"
    "cikKICAgICAgICBzZWxmLmRpc3QgPSBNZXNzYWdlRGlzdHJpYnV0b3Ioc2VsZikKICAgICAgICBz"
    "ZWxmLmNvbXBhcnMgPSBDb21tYW5kUGFyc2VyKHNlbGYuZGlzdCkKICAgICAgICBzZWxmLnN0YXRl"
    "ID0gR2FtZVN0YXRlKHNlbGYpCiAgICAgICAgc2VsZi5zdGFydFRpbWUgPSBkYXRldGltZS5kYXRl"
    "dGltZS5ub3coKQogICAgICAgIHNlbGYuc2VydmljZV90aWNrID0gMAogICAgICAgIHNlbGYuX3Bv"
    "c1N0b3AgPSB0aHJlYWRpbmcuRXZlbnQoKQogICAgICAgIHNlbGYuX3Bvc1RocmVhZCA9IE5vbmUK"
    "ICAgICAgICAjRXZlcnkgbGl2ZSBjb25uZWN0aW9uIGhhbmRsZXIuIHNvY2tldHNlcnZlcidzIHNo"
    "dXRkb3duKCkgb25seSBzdG9wcwogICAgICAgICN0aGUgYWNjZXB0IGxvb3AgYW5kIGNsb3NlcyB0"
    "aGUgbGlzdGVuaW5nIHNvY2tldCAtIGFscmVhZHktZXN0YWJsaXNoZWQKICAgICAgICAjY29ubmVj"
    "dGlvbnMga2VlcCB0aGVpciAoZGFlbW9uKSB0aHJlYWRzIHJ1bm5pbmcsIHN0aWxsIHJlYWRpbmcs"
    "IHN0aWxsCiAgICAgICAgI2xvZ2dpbmcsIGZvciBhcyBsb25nIGFzIHRoZSBjbGllbnQgc3RheXMg"
    "Y29ubmVjdGVkLiBGcm9tIHRoZSBjb250cm9sCiAgICAgICAgI3BhbmVsIHRoYXQgbG9va3MgbGlr"
    "ZSBhIHNlcnZlciB0aGF0IHdhcyBuZXZlciBzdG9wcGVkIGF0IGFsbC4KICAgICAgICBzZWxmLl9j"
    "b25ucyA9IHNldCgpCiAgICAgICAgc2VsZi5fY29ubkxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAg"
    "ICBkZWYgc2VydmVyX2FjdGl2YXRlKHNlbGYpOgogICAgICAgIHByaW50KGYnU2VydmVyIFN0YXJ0"
    "aW5nIGF0IFBJRDoge29zLmdldHBpZCgpfScpI0xPRwogICAgICAgIHN1cGVyKCkuc2VydmVyX2Fj"
    "dGl2YXRlKCkKICAgIGRlZiBkZWJ1Z19kaWN0X3BsYXllcnMoc2VsZik6CiAgICAgICAgI3NuYXBz"
    "aG90IHZpYSBsaXN0KCkgZmlyc3Q6IGl0ZXJhdGluZyB0aGUgbGl2ZSBkaWN0IGRpcmVjdGx5IHJp"
    "c2tzCiAgICAgICAgIydkaWN0aW9uYXJ5IGNoYW5nZWQgc2l6ZSBkdXJpbmcgaXRlcmF0aW9uJyB3"
    "aGVuIGEgcGxheWVyIGNvbm5lY3RzCiAgICAgICAgI29yIGRpc2Nvbm5lY3RzIHdoaWxlIGEgbW9u"
    "aXRvcmluZyBVSSBpcyBwb2xsaW5nIHRoaXMKICAgICAgICByZXQgPSB7fQogICAgICAgIGZvciBu"
    "YW1lLCBjb24gaW4gbGlzdChzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLml0ZW1zKCkpOgogICAgICAg"
    "ICAgICByZXRbbmFtZV0gPSBjb24uZGVidWdfZGljdCgpCiAgICAgICAgcmV0dXJuIHJldAogICAg"
    "ZGVmIGRlYnVnX2RpY3RfdG93bnMoc2VsZik6CiAgICAgICAgcmV0ID0ge30KICAgICAgICBmb3Ig"
    "bmFtZSwgY2huIGluIGxpc3Qoc2VsZi5zdGF0ZS5nYW1lQ2hhbm5lbHMuaXRlbXMoKSk6CiAgICAg"
    "ICAgICAgIHJldFtuYW1lXSA9IGNobi5kZWJ1Z19kaWN0KCkKICAgICAgICByZXR1cm4gcmV0CiAg"
    "ICBkZWYgZGVidWdfYXJyX2dhbWVzKHNlbGYpOgogICAgICAgIHJldCA9IFtdCiAgICAgICAgZm9y"
    "IG5hbWUsIGNobiBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1zKCkpOgogICAg"
    "ICAgICAgICAgcmV0LmV4dGVuZChjaG4uZGVidWdfYXJyX2dhbWVzKCkpCiAgICAgICAgcmV0dXJu"
    "IHJldAogICAgZGVmIF9wb3NMb29wKHNlbGYpOgogICAgICAgICNQb3NpdGlvbiBmYW4tb3V0IHVz"
    "ZWQgdG8gcmlkZSBvbiBzZXJ2aWNlX2FjdGlvbnMoKSwgd2hpY2ggc29ja2V0c2VydmVyCiAgICAg"
    "ICAgI2NhbGxzIG9uY2UgcGVyIHBvbGxfaW50ZXJ2YWwgLSBvbmUgc2Vjb25kLiBUaGF0IHdhcyB0"
    "aGUgY2FkZW5jZSBhdAogICAgICAgICN3aGljaCBvdGhlciBwbGF5ZXJzJyBtYXJrZXJzIG1vdmVk"
    "IG9uIHRoZSBtYXA6IGEgZnVsbCBzZWNvbmQgb2YgZGVhZAogICAgICAgICNyZWNrb25pbmcgYmV0"
    "d2VlbiB1cGRhdGVzLCB3aGljaCByZWFkcyBhcyB0ZWxlcG9ydGluZyByYXRoZXIgdGhhbgogICAg"
    "ICAgICN3YWxraW5nLiBJdHMgb3duIHRocmVhZCBkZWNvdXBsZXMgdGhlIGJyb2FkY2FzdCByYXRl"
    "IGZyb20gdGhlIGFjY2VwdAogICAgICAgICNsb29wJ3MgcG9sbCByYXRlIHNvIGl0IGNhbiBydW4g"
    "c2V2ZXJhbCB0aW1lcyBhIHNlY29uZC4KICAgICAgICB3aGlsZSBub3Qgc2VsZi5fcG9zU3RvcC5p"
    "c19zZXQoKToKICAgICAgICAgICAgcGVyaW9kID0gMS4wIC8gX1BPU19VUERBVEVfSFogaWYgX1BP"
    "U19VUERBVEVfSFogPiAwIGVsc2UgMS4wCiAgICAgICAgICAgICN3YWl0KCkgcmF0aGVyIHRoYW4g"
    "c2xlZXAoKTogc2h1dGRvd24gaXMgaW1tZWRpYXRlLCBhbmQgcmUtcmVhZGluZwogICAgICAgICAg"
    "ICAjdGhlIHBlcmlvZCBlYWNoIHBhc3MgbWVhbnMgYSBjb25maWcgY2hhbmdlIHRha2VzIGVmZmVj"
    "dCBsaXZlLgogICAgICAgICAgICBpZiBzZWxmLl9wb3NTdG9wLndhaXQocGVyaW9kKToKICAgICAg"
    "ICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgIHNlbGYuc3Rh"
    "dGUudXBkYXRlUG9zKCkKICAgICAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAg"
    "ICAgICNuZXZlciBsZXQgb25lIGJhZCBjaGFubmVsIGtpbGwgcG9zaXRpb24gc3luYyBmb3IgZXZl"
    "cnlvbmUKICAgICAgICAgICAgICAgIHByaW50KCdbTG9iYnldIFBvc2l0aW9uIHVwZGF0ZSBlcnJv"
    "cjpcbicgKyB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgZGVmIHNlcnZpY2VfYWN0aW9ucyhz"
    "ZWxmKTogI2NhbGxlZCBldmVyeSBwb2xsX2ludGVydmFsCiAgICAgICAgIyB0aW1lIGludGVydmFs"
    "cwogICAgICAgICNSZWFkIGxpdmUsIG5vdCBmcm9tIHRoZSBjb3B5IHRha2VuIHdoZW4gdGhpcyBz"
    "ZXJ2ZXIgb2JqZWN0IHdhcyBidWlsdC4KICAgICAgICAjRXZlcnkgb3RoZXIgc3luY2hyb25pc2F0"
    "aW9uIHNldHRpbmcgdGFrZXMgZWZmZWN0IG9uIGEgcnVubmluZyBzZXJ2ZXIgLQogICAgICAgICNh"
    "cHBseUNvbmZpZygpIHdyaXRlcyB0aGUgbW9kdWxlIGdsb2JhbHMgYW5kIHRoZSBsb29wcyByZS1y"
    "ZWFkIHRoZW0gLQogICAgICAgICN3aGljaCBtYWRlIHRoaXMgdGhlIG9uZSBzd2l0Y2ggaW4gdGhh"
    "dCBncm91cCB0aGF0IHNpbGVudGx5IGRpZCBub3RoaW5nCiAgICAgICAgI3VudGlsIHRoZSBuZXh0"
    "IHJlc3RhcnQsIHdoaWxlIHRoZSBHVUkgc2FpZCBvdGhlcndpc2UuCiAgICAgICAgaWYgX1NFTkRf"
    "Tk9QUyBhbmQgKHNlbGYuc2VydmljZV90aWNrJTMpPT0wOgogICAgICAgICAgICBzZWxmLmRpc3Qu"
    "YWRkKHsndGFyZ2V0JzpzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpLCdtZXNzYWdlJzpf"
    "ZW0oJy9ub3AnKX0pCiAgICAgICAgICAgICNzZW5kICcvbm9wJyB0byBhbGwgZXZlcnkgMyBzZWMg"
    "b3B0aW9uYWxseQogICAgICAgICNzZXJ2aWNlIHRpY2sgMyBkYXkgcmVzZXQgaW50ZXJ2YWwgVE9E"
    "TyB0ZXN0IGFsaWdubWVudCB3aXRoIG90aGVyIGZhY3RvcnMKICAgICAgICBzZWxmLnNlcnZpY2Vf"
    "dGljayA9IChzZWxmLnNlcnZpY2VfdGljaysxKSUoNjAqNjAqMjQqMykKICAgICAgICBzdXBlcigp"
    "LnNlcnZpY2VfYWN0aW9ucygpCiAgICBkZWYgc2VydmVfZm9yZXZlcihzZWxmKToKICAgICAgICBk"
    "aXN0VGhyZWFkID0gdGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9c2VsZi5kaXN0LnNlcnZlX2ZvcmV2"
    "ZXIpCiAgICAgICAgZGlzdFRocmVhZC5zdGFydCgpCiAgICAgICAgc2VsZi5fcG9zU3RvcC5jbGVh"
    "cigpCiAgICAgICAgc2VsZi5fcG9zVGhyZWFkID0gdGhyZWFkaW5nLlRocmVhZCh0YXJnZXQ9c2Vs"
    "Zi5fcG9zTG9vcCwgZGFlbW9uPVRydWUpCiAgICAgICAgc2VsZi5fcG9zVGhyZWFkLnN0YXJ0KCkK"
    "ICAgICAgICAjcG9sbF9pbnRlcnZhbCBpcyBub3cgb25seSB0aGUgYWNjZXB0IGxvb3AncyBzaHV0"
    "ZG93biByZXNwb25zaXZlbmVzcyAtCiAgICAgICAgI3Bvc2l0aW9uIGJyb2FkY2FzdHMgbm8gbG9u"
    "Z2VyIHJpZGUgb24gaXQKICAgICAgICBzdXBlcigpLnNlcnZlX2ZvcmV2ZXIoMSkKICAgICAgICBz"
    "ZWxmLl9wb3NTdG9wLnNldCgpCiAgICAgICAgaWYgc2VsZi5fcG9zVGhyZWFkOgogICAgICAgICAg"
    "ICBzZWxmLl9wb3NUaHJlYWQuam9pbih0aW1lb3V0PTIuMCkKICAgICAgICAgICAgc2VsZi5fcG9z"
    "VGhyZWFkID0gTm9uZQogICAgICAgIHNlbGYuZGlzdC5lbmQoKSNpbiBjYXNlIGl0IGhhc24ndCBh"
    "bHJlYWR5CiAgICAgICAgZGlzdFRocmVhZC5qb2luKCkKICAgIGRlZiBoYW5kbGVfc2lnbmFsKHNl"
    "bGYsIHRpbWVvdXQpOgogICAgICAgIGRlZiBoYW5kbGVyKHNpZ251bSwgXyk6CiAgICAgICAgICAg"
    "IGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIHRpbWVvdXQKICAgICAgICAgICAgc2lnbmFt"
    "ZSA9IHNpZ25hbC5TaWduYWxzKHNpZ251bSkubmFtZQogICAgICAgICAgICBzZWxmLl9pc19jbG9z"
    "aW5nID0gVHJ1ZSAjVE9ETyBwcm9wZXJseSBlbmQgY29ubmVjdGlvbnMgYWZ0ZXIgYSBkZWxheQog"
    "ICAgICAgICAgICBwcmludChmJ0Nsb3NpbmcgaW4ge3RpbWVvdXR9JykKICAgICAgICAgICAgI3do"
    "aWxlIChjdXJyZW50X3RpbWUgOj0gdGltZS5tb25vdG9uaWMoKSkgPCBkZWFkbGluZToKICAgICAg"
    "ICAgICAgIyAgICBkZWx0YSA9IGludChkZWFkbGluZSAtIGN1cnJlbnRfdGltZSkKICAgICAgICAg"
    "ICAgICAgICNUT0RPIHNpZ25hbCB0byBwbGF5ZXJzIHRoYXQgY29ubmVjdGlvbiBpcyBzaHV0dGlu"
    "ZyBkb3duCiAgICAgICAgICAgICAgICAjLSBzZWxmLnN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygp"
    "CiAgICAgICAgICAgICAgICAjLSBmJy9hZG1pbiBTZXJ2ZXIgY2xvc2luZyBpbiB7ZGVsdGF9Jy5l"
    "bmNvZGUoJ2FzY2lpJykrX04KICAgICAgICAgICAgICAgICNMT0cgQ0xPU0UKICAgICAgICAgICAg"
    "ICAgICNUT0RPIGJldHRlciBzaHV0ZG93biBoYW5kbGluZwogICAgICAgICAgICAjICAgIHRpbWUu"
    "c2xlZXAoMSkKICAgICAgICAgICAgdGltZS5zbGVlcCh0aW1lb3V0KSNhbHQgd2hpbGUgb3RoZXIg"
    "c3R1ZmYgaXMgb25nb2luZwogICAgICAgICAgICBzZWxmLl9CYXNlU2VydmVyX19zaHV0ZG93bl9y"
    "ZXF1ZXN0ID0gVHJ1ZQogICAgICAgICAgICAjc2VsZi5zaHV0ZG93bigpICNvbmx5IGlmIHNlcnZl"
    "X2ZvcmV2ZXIgaXMgaW4gYSBkaWZmZXJlbnQgdGhyZWFkCiAgICAgICAgICAgICNzZWxmLnNlcnZl"
    "cl9jbG9zZSgpICNvbmx5IG5lZWRlZCBpZiBub3QgdXNpbmcgYSB3aXRoIHN0YXRlbWVudAogICAg"
    "ICAgIHJldHVybiBoYW5kbGVyCiAgICBkZWYgcmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYsIGNvbik6"
    "CiAgICAgICAgd2l0aCBzZWxmLl9jb25uTG9jazoKICAgICAgICAgICAgc2VsZi5fY29ubnMuYWRk"
    "KGNvbikKICAgIGRlZiB1bnJlZ2lzdGVyQ29ubmVjdGlvbihzZWxmLCBjb24pOgogICAgICAgIHdp"
    "dGggc2VsZi5fY29ubkxvY2s6CiAgICAgICAgICAgIHNlbGYuX2Nvbm5zLmRpc2NhcmQoY29uKQog"
    "ICAgZGVmIGNsb3NlQ29ubmVjdGlvbnMoc2VsZik6CiAgICAgICAgI0Ryb3AgZXZlcnkgY2xpZW50"
    "LiBTaHV0dGluZyB0aGUgc29ja2V0IGRvd24gdW5ibG9ja3Mgd2hpY2hldmVyCiAgICAgICAgI3Nl"
    "bGVjdCgpL3JlY3YoKSB0aGF0IGNvbm5lY3Rpb24ncyB0aHJlYWQgaXMgc2l0dGluZyBpbiwgc28g"
    "aXQgcnVucwogICAgICAgICNpdHMgbm9ybWFsIGNsZWFudXAgcGF0aCBhbmQgZXhpdHMgaW5zdGVh"
    "ZCBvZiBsaW5nZXJpbmcuCiAgICAgICAgd2l0aCBzZWxmLl9jb25uTG9jazoKICAgICAgICAgICAg"
    "Y29ubnMgPSBsaXN0KHNlbGYuX2Nvbm5zKQogICAgICAgIGZvciBjb24gaW4gY29ubnM6CiAgICAg"
    "ICAgICAgIGNvbi5kcm9wKCkKICAgICAgICByZXR1cm4gbGVuKGNvbm5zKQogICAgZGVmIHNodXRk"
    "b3duKHNlbGYpOgogICAgICAgICNTdG9wcGluZyB0aGUgc2VydmVyIG1lYW5zIHN0b3BwaW5nIGl0"
    "OiBmbGFnIGl0IGZpcnN0IHNvIHRoZSByZWFkCiAgICAgICAgI2xvb3BzIGJhaWwgb3V0IHJhdGhl"
    "ciB0aGFuIHNlcnZpbmcgYW5vdGhlciBjb21tYW5kLCB0aGVuIHN0b3AgdGhlCiAgICAgICAgI2Fj"
    "Y2VwdCBsb29wLCB0aGVuIGV2aWN0IGV2ZXJ5b25lIHN0aWxsIGNvbm5lY3RlZC4KICAgICAgICBz"
    "ZWxmLl9pc19jbG9zaW5nID0gVHJ1ZQogICAgICAgIHN1cGVyKCkuc2h1dGRvd24oKQogICAgICAg"
    "IG4gPSBzZWxmLmNsb3NlQ29ubmVjdGlvbnMoKQogICAgICAgIGlmIG46CiAgICAgICAgICAgIHBy"
    "aW50KGYnW0xvYmJ5XSBDbG9zZWQge259IGNsaWVudCBjb25uZWN0aW9uKHMpIG9uIHNodXRkb3du"
    "JykKICAgIGRlZiBnZXRQbGF5ZXIoc2VsZiwgdXNlcm5hbWUpOgogICAgICAgIHJldHVybiBzZWxm"
    "LnN0YXRlLmFjdGl2ZVVzZXJzLmdldCh1c2VybmFtZSkKICAgIGRlZiBraWNrUGxheWVyKHNlbGYs"
    "IHVzZXJuYW1lLCByZWFzb249J0tpY2tlZCBieSBhZG1pbicpOgogICAgICAgICNBZG1pbi1wYW5l"
    "bCBhY3Rpb246IGZvcmNpYmx5IGRpc2Nvbm5lY3QgYSBjb25uZWN0ZWQgcGxheWVyLiBTZW5kcyBh"
    "CiAgICAgICAgI2Jlc3QtZWZmb3J0IC9hZG1pbiBub3RpY2UgZmlyc3QgKGNsaWVudCBzaG93cyBp"
    "dCBsaWtlIGFueSBvdGhlcgogICAgICAgICNzZXJ2ZXIgYWRtaW4gbWVzc2FnZSksIHRoZW4gc2h1"
    "dHMgZG93biB0aGUgc29ja2V0IHNvIHRoZSBwbGF5ZXIncwogICAgICAgICNoYW5kbGVyIHRocmVh"
    "ZCB1bmJsb2NrcyBmcm9tIGl0cyByZWN2KCkgYW5kIHJ1bnMgaXRzIG5vcm1hbAogICAgICAgICNk"
    "aXNjb25uZWN0L2NsZWFudXAgcGF0aC4KICAgICAgICBjb24gPSBzZWxmLmdldFBsYXllcih1c2Vy"
    "bmFtZSkKICAgICAgICBpZiBjb24gaXMgTm9uZToKICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAg"
    "ICAgICAgI1F1ZXVlZCwgbm90IHdyaXR0ZW4gaW5saW5lLiBzZW5kUmF3KCkgdGFrZXMgdGhhdCBj"
    "b25uZWN0aW9uJ3Mgc2VuZAogICAgICAgICNsb2NrLCBhbmQgaXRzIHdyaXRlciB0aHJlYWQgaG9s"
    "ZHMgdGhhdCBsb2NrIGZvciB0aGUgd2hvbGUgb2YgYQogICAgICAgICNibG9ja2luZyBzZW5kYWxs"
    "KCkgLSBzbyBraWNraW5nIGEgcGxheWVyIHdob3NlIGxpbmsgaGFkIHN0YWxsZWQgYmxvY2tlZAog"
    "ICAgICAgICN3aG9ldmVyIGNhbGxlZCB0aGlzIHVudGlsIHRoZSBzdGFsbGVkIGNsaWVudCB3ZW50"
    "IGF3YXksIGFuZCB0aGUgY2FsbGVyCiAgICAgICAgI2hlcmUgaXMgdGhlIEdVSSB0aHJlYWQuIFRo"
    "ZSBhZG1pbiBwYW5lbCBmcm96ZSBvbiBleGFjdGx5IHRoZSBwbGF5ZXIgaXQKICAgICAgICAjd2Fz"
    "IHRyeWluZyB0byBnZXQgcmlkIG9mLiBBIHF1ZXVlIHB1dCBjYW5ub3QgYmxvY2suCiAgICAgICAg"
    "dHJ5OgogICAgICAgICAgICBjb24uc2VuZChfZW0oZicvYWRtaW4ge3JlYXNvbn0nKSkKICAgICAg"
    "ICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICBwYXNzICNiZXN0IGVmZm9ydCwgY29ubmVj"
    "dGlvbiBtYXkgYWxyZWFkeSBiZSBvbiBpdHMgd2F5IG91dAogICAgICAgIGNvbi5mbHVzaFBlbmRp"
    "bmcoMC4zKSAjYm91bmRlZDogZ2l2ZSB0aGUgbm90aWNlIGEgY2hhbmNlIHRvIGdvIG91dAogICAg"
    "ICAgIGNvbi5kcm9wKCkKICAgICAgICByZXR1cm4gVHJ1ZQogICAgZGVmIGRlbGV0ZUFjY291bnQo"
    "c2VsZiwgdXNlcm5hbWUpOgogICAgICAgICNBZG1pbi1wYW5lbCBhY3Rpb246IHBlcm1hbmVudGx5"
    "IGRlbGV0ZXMgYSBjaGFyYWN0ZXIvYWNjb3VudC4KICAgICAgICAjS2lja3MgZmlyc3QgKG5vLW9w"
    "IGlmIGFscmVhZHkgb2ZmbGluZSkgc28gYSBjb25uZWN0ZWQgY2xpZW50IG5ldmVyCiAgICAgICAg"
    "I2tlZXBzIHBsYXlpbmcgb24gYW4gYWNjb3VudCB0aGF0IGhhcyBqdXN0IHZhbmlzaGVkIGZyb20g"
    "dGhlIERCLgogICAgICAgIHNlbGYua2lja1BsYXllcih1c2VybmFtZSwgcmVhc29uPSdBY2NvdW50"
    "IGRlbGV0ZWQgYnkgYWRtaW4nKQogICAgICAgIHJldHVybiBHREguZGVsZXRlQWNjb3VudCh1c2Vy"
    "bmFtZSkKI0ZhaWxlZC1sb2dpbiB0aHJvdHRsZSwgcGVyIHNvdXJjZSBJUC4KI1R3byByZWFzb25z"
    "IHRoaXMgaXMgbm90IG9wdGlvbmFsIG9uIGEgc2VydmVyIHJlYWNoYWJsZSBmcm9tIHRoZSBpbnRl"
    "cm5ldDoKI2EgcGFzc3dvcmQgZ3Vlc3MgaXMgY2hlYXAgZm9yIHRoZSBhdHRhY2tlciBidXQgY29z"
    "dHMgKnVzKiBhIDEwMGstaXRlcmF0aW9uCiNQQktERjIgKHRlbnMgb2YgbXMgb2YgQ1BVIGVhY2gp"
    "LCBzbyBhbiB1bnRocm90dGxlZCBsb2dpbiBlbmRwb2ludCBpcyBib3RoIGEKI2JydXRlLWZvcmNl"
    "IG9yYWNsZSBhbmQgYSBDUFUgYW1wbGlmaWVyIC0gYSBoYW5kZnVsIG9mIGNvbm5lY3Rpb25zIGNh"
    "biBwaW4KI2V2ZXJ5IGNvcmUuIFN1Y2Nlc3NmdWwgbG9naW5zIGNsZWFyIHRoZSBjb3VudGVyLCBz"
    "byBhIHBsYXllciBmdW1ibGluZyB0aGVpcgojcGFzc3dvcmQgYSBmZXcgdGltZXMgaXMgbmV2ZXIg"
    "bG9ja2VkIG91dCBmb3IgbG9uZy4KX0xPR0lOX0ZBSUxfTElNSVQgPSA2ICAgICAgI2ZhaWx1cmVz"
    "IGFsbG93ZWQgaW5zaWRlIHRoZSB3aW5kb3cgYmVmb3JlIGRlbGF5aW5nCl9MT0dJTl9GQUlMX1dJ"
    "TkRPVyA9IDMwMCAgICNzZWNvbmRzIGEgZmFpbHVyZSBpcyByZW1lbWJlcmVkCl9MT0dJTl9GQUlM"
    "X0RFTEFZID0gMi4wICAgICNzZWNvbmRzIHRvIHN0YWxsIGVhY2ggYXR0ZW1wdCBvbmNlIG92ZXIg"
    "dGhlIGxpbWl0CmNsYXNzIExvZ2luVGhyb3R0bGUoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmKToK"
    "ICAgICAgICBzZWxmLmxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgc2VsZi5mYWlscyA9"
    "IHt9ICNpcCAtPiBbdGltZXN0YW1wc10KICAgIGRlZiBfcHJ1bmUoc2VsZiwgaXAsIG5vdyk6CiAg"
    "ICAgICAgcmVjZW50ID0gW3QgZm9yIHQgaW4gc2VsZi5mYWlscy5nZXQoaXAsICgpKSBpZiBub3cg"
    "LSB0IDwgX0xPR0lOX0ZBSUxfV0lORE9XXQogICAgICAgIGlmIHJlY2VudDoKICAgICAgICAgICAg"
    "c2VsZi5mYWlsc1tpcF0gPSByZWNlbnQKICAgICAgICBlbHNlOgogICAgICAgICAgICBzZWxmLmZh"
    "aWxzLnBvcChpcCwgTm9uZSkKICAgICAgICByZXR1cm4gcmVjZW50CiAgICBkZWYgZGVsYXlGb3Io"
    "c2VsZiwgaXApOgogICAgICAgIG5vdyA9IHRpbWUubW9ub3RvbmljKCkKICAgICAgICB3aXRoIHNl"
    "bGYubG9jazoKICAgICAgICAgICAgcmVjZW50ID0gc2VsZi5fcHJ1bmUoaXAsIG5vdykKICAgICAg"
    "ICByZXR1cm4gX0xPR0lOX0ZBSUxfREVMQVkgaWYgbGVuKHJlY2VudCkgPj0gX0xPR0lOX0ZBSUxf"
    "TElNSVQgZWxzZSAwLjAKICAgIGRlZiByZWNvcmRGYWlsdXJlKHNlbGYsIGlwKToKICAgICAgICBu"
    "b3cgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAg"
    "IHJlY2VudCA9IHNlbGYuX3BydW5lKGlwLCBub3cpCiAgICAgICAgICAgIHJlY2VudC5hcHBlbmQo"
    "bm93KQogICAgICAgICAgICBzZWxmLmZhaWxzW2lwXSA9IHJlY2VudAogICAgICAgICAgICByZXR1"
    "cm4gbGVuKHJlY2VudCkKICAgIGRlZiByZWNvcmRTdWNjZXNzKHNlbGYsIGlwKToKICAgICAgICB3"
    "aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgc2VsZi5mYWlscy5wb3AoaXAsIE5vbmUpCkxPR0lO"
    "X1RIUk9UVExFID0gTG9naW5UaHJvdHRsZSgpCgpfTE9HSU5fRVJST1JTID0gewogICAgMTogJ0lu"
    "dmFsaWQgdXNlcm5hbWUgb3IgcGFzc3dvcmQnLAogICAgMjogJ0FjY291bnQgYWxyZWFkeSBsb2dn"
    "ZWQgaW4nLAogICAgMzogJ1Bhc3N3b3JkIHJlcXVpcmVkJywKICAgIDQ6ICdVc2VybmFtZSByZXF1"
    "aXJlZCcsCiAgICAjQWNjb3VudHMgYXJlIHRpZWQgdG8gdGhlIHNlcmlhbCB0aGUgY2xpZW50IGhh"
    "bmRzaGFrZXMgd2l0aCwgc28gYQogICAgI3JlaW5zdGFsbGVkIG9yIHJlLWtleWVkIGdhbWUgY2Fu"
    "bm90IHJlYWNoIGFuIGV4aXN0aW5nIGFjY291bnQgbm8gbWF0dGVyCiAgICAjd2hhdCBwYXNzd29y"
    "ZCBpdCB0eXBlcy4gU2F5IHRoYXQsIHJhdGhlciB0aGFuIGJsYW1pbmcgdGhlIG5hbWUuCiAgICA1"
    "OiAnVGhpcyBuYW1lIGJlbG9uZ3MgdG8gYW4gYWNjb3VudCByZWdpc3RlcmVkIHdpdGggYSBkaWZm"
    "ZXJlbnQgZ2FtZSBzZXJpYWwnLAp9Cl9SRUdJU1RFUl9FUlJPUlMgPSB7CiAgICAxOiAnQWNjb3Vu"
    "dCBhbHJlYWR5IGxvZ2dlZCBpbicsCiAgICAyOiAnVXNlcm5hbWUgdW5hdmFpbGFibGUgb3IgaW52"
    "YWxpZCcsCn0KI0NlaWxpbmcgb24gaG93IG11Y2ggdW5zZW50IGRhdGEgbWF5IHBpbGUgdXAgZm9y"
    "IGEgc2luZ2xlIGNsaWVudC4gVGhlIHdyaXRlcgojdGhyZWFkIGJsb2NrcyBpbnNpZGUgc2VuZGFs"
    "bCgpIGZvciBleGFjdGx5IGFzIGxvbmcgYXMgYSBjbGllbnQgcmVmdXNlcyB0byByZWFkLAojYW5k"
    "IGEgZnJvemVuIGdhbWUgZG9lcyBwcmVjaXNlbHkgdGhhdCAtIHdoaWxlIGFsc28gc2VuZGluZyBu"
    "b3RoaW5nLCBzbyBub3RoaW5nCiNlbHNlIG5vdGljZXMgaXQgdW50aWwgYSBmdWxsIGlkbGUgdGlt"
    "ZW91dCBoYXMgcGFzc2VkLiBGb3IgdGhvc2UgbWludXRlcyBldmVyeQojcG9zaXRpb24gYnJvYWRj"
    "YXN0LCBldmVyeSBjaGF0IGxpbmUgYW5kIGV2ZXJ5IHJlbGF5ZWQgZ2FtZSBjb21tYW5kIGZvciB0"
    "aGF0CiNwbGF5ZXIga2VwdCBiZWluZyBhcHBlbmRlZCB0byBhbiB1bmJvdW5kZWQgcXVldWUuIEJv"
    "dW5kaW5nIGl0IHR1cm5zICJ0aGUgc2VydmVyCiNxdWlldGx5IGdyb3dzIG9uIGJlaGFsZiBvZiBh"
    "IGNsaWVudCB0aGF0IGlzIGFscmVhZHkgZ29uZSIgaW50byBhIGNsZWFuIGRyb3AKI3dpdGggYSBs"
    "aW5lIGluIHRoZSBsb2cuIFNpemVkIGZhciBhYm92ZSBhbnkgbGVnaXRpbWF0ZSBidXJzdDogdGhl"
    "IGxhcmdlc3QKI3NpbmdsZSB0aGluZyB0aGF0IGdvZXMgb3V0IGlzIGEgaGVyb2RhdGEgYmxvYiwg"
    "YW5kIGEgd2hvbGUgdG93biBvZiB0aGVtIGRvZXMKI25vdCBjb21lIGNsb3NlLgpfTUFYX1NFTkRf"
    "QkFDS0xPRyA9IDQgKiAxMDI0ICogMTAyNAojaGFuZGxlcyBpbmRpdmlkdWFsIGNvbm5lY3Rpb25z"
    "CmNsYXNzIENvbm5lY3Rpb25IYW5kbGVyKHNvY2tldHNlcnZlci5CYXNlUmVxdWVzdEhhbmRsZXIp"
    "OgogICAgI2RlZmF1bHQgcHJvcGVydGllczoKICAgICMgLSByZXF1ZXN0OiBzb2NrZXQgdG8gZGVz"
    "dGluYXRpb24KICAgICMgLSBjbGllbnRfYWRkcmVzcwogICAgIyAtIHNlcnZlcjogQ29yZVNlcnZl"
    "cgogICAgX1NUT1BXUklURVIgPSBvYmplY3QoKQogICAgZGVmIHNldHVwKHNlbGYpOgogICAgICAg"
    "IHNlbGYuX3NRdWV1ZSA9IFNpbXBsZVF1ZXVlKCkKICAgICAgICAjQnl0ZXMgcXVldWVkIGJ1dCBu"
    "b3QgeWV0IGhhbmRlZCB0byBzZW5kYWxsKCksIGFuZCB0aGUgZmxhZyB0aGF0IHNheXMKICAgICAg"
    "ICAjdGhpcyBjb25uZWN0aW9uIGhhcyBhbHJlYWR5IGJlZW4gZ2l2ZW4gdXAgb24gZm9yIGV4Y2Vl"
    "ZGluZyB0aGUgY2FwLgogICAgICAgIHNlbGYuX3FCeXRlcyA9IDAKICAgICAgICBzZWxmLl9xTG9j"
    "ayA9IHRocmVhZGluZy5Mb2NrKCkKICAgICAgICBzZWxmLl9vdmVyZmxvd2VkID0gRmFsc2UKICAg"
    "ICAgICBzZWxmLnVzZXIgPSBOb25lCiAgICAgICAgc2VsZi5ndWlkID0gTm9uZQogICAgICAgIHNl"
    "bGYuZGF0YSA9IGInJwogICAgICAgIHNlbGYuU0sgPSBieXRlYXJyYXkoc3RydWN0LnBhY2soJzxJ"
    "SScsIDB4QTZBRTFGOUIsIDB4NDM4REZGNDApKQogICAgICAgICNTZXJpYWxpc2VzIHRoZSByYXcg"
    "c29ja2V0IHdyaXRlcy4gVGhyZWUgdGhyZWFkcyBjYW4gd2FudCB0byB3cml0ZSB0bwogICAgICAg"
    "ICNvbmUgY2xpZW50OiB0aGlzIGNvbm5lY3Rpb24ncyBvd24gcmVhZCBsb29wIChkdXJpbmcgdGhl"
    "IGhhbmRzaGFrZSksCiAgICAgICAgI2l0cyB3cml0ZXIgdGhyZWFkLCBhbmQgdGhlIEdVSSB0aHJl"
    "YWQgdmlhIGtpY2tQbGF5ZXIoKS4gV2l0aG91dCB0aGUKICAgICAgICAjbG9jayB0d28gc2VuZGFs"
    "bCgpIGNhbGxzIGNhbiBpbnRlcmxlYXZlIGFuZCBzcGxpdCBhIHBhY2tldCBkb3duIHRoZQogICAg"
    "ICAgICNtaWRkbGUsIHdoaWNoIHRoZSBjbGllbnQgc2VlcyBhcyBwcm90b2NvbCBnYXJiYWdlLgog"
    "ICAgICAgIHNlbGYuX3NlbmRMb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIHNlbGYuX3dy"
    "aXRlciA9IE5vbmUKICAgICAgICBzZWxmLl93cml0ZXJEZWFkID0gdGhyZWFkaW5nLkV2ZW50KCkK"
    "ICAgICAgICAjU2V0IHdoZW4gdGhpcyBjb25uZWN0aW9uIGhhcyBiZWVuIGdpdmVuIHVwIG9uIGZy"
    "b20gKm91dHNpZGUqIGl0cyBvd24KICAgICAgICAjaGFuZGxlciB0aHJlYWQgLSBhbiBhZG1pbiBr"
    "aWNrLCBvciB0aGUgc2VuZC1iYWNrbG9nIGNhcC4gU2h1dHRpbmcgdGhlCiAgICAgICAgI3NvY2tl"
    "dCBkb3duIGlzIHN1cHBvc2VkIHRvIHdha2UgdGhhdCB0aHJlYWQgb24gaXRzIG93biwgYW5kIG5v"
    "cm1hbGx5CiAgICAgICAgI2RvZXM7IHRoaXMgbWFrZXMgaXQgY2VydGFpbiByYXRoZXIgdGhhbiBk"
    "ZXBlbmRlbnQgb24gdGhlIHNvY2tldAogICAgICAgICNyZXBvcnRpbmcgdGhlIHNodXRkb3duIHBy"
    "b21wdGx5LiBBIGtpY2sgdGhhdCBpcyBub3Qgbm90aWNlZCBsZWF2ZXMgdGhlCiAgICAgICAgI2Fj"
    "Y291bnQgY2xhaW1lZCwgYW5kIHRoZSBwbGF5ZXIgY2Fubm90IGdldCBiYWNrIGluIHVudGlsIHRo"
    "ZSBpZGxlCiAgICAgICAgI3RpbWVvdXQgZXhwaXJlcyAtIHRoZSBleGFjdCBmYWlsdXJlIGEga2lj"
    "ayBpcyBtZWFudCB0byByZXNvbHZlLgogICAgICAgIHNlbGYuX2Ryb3BwZWQgPSB0aHJlYWRpbmcu"
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
    "dC4KICAgICAgICBpZiBub3QgbXNnOgogICAgICAgICAgICByZXR1cm4KICAgICAgICB3aXRoIHNl"
    "bGYuX3FMb2NrOgogICAgICAgICAgICBpZiBzZWxmLl9vdmVyZmxvd2VkOgogICAgICAgICAgICAg"
    "ICAgcmV0dXJuICNhbHJlYWR5IGJlaW5nIHRvcm4gZG93biwgc3RvcCBhY2NvdW50aW5nIGZvciBp"
    "dAogICAgICAgICAgICBzZWxmLl9xQnl0ZXMgKz0gbGVuKG1zZykKICAgICAgICAgICAgb3ZlciA9"
    "IHNlbGYuX3FCeXRlcyA+IF9NQVhfU0VORF9CQUNLTE9HCiAgICAgICAgICAgIHNlbGYuX292ZXJm"
    "bG93ZWQgPSBvdmVyCiAgICAgICAgaWYgb3ZlcjoKICAgICAgICAgICAgI1NlZSBfTUFYX1NFTkRf"
    "QkFDS0xPRy4gU2h1dHRpbmcgdGhlIHNvY2tldCBkb3duIGlzIHdoYXQgdGVsbHMgdGhlCiAgICAg"
    "ICAgICAgICNyZWFkIGxvb3AgdG8gcnVuIHRoaXMgY29ubmVjdGlvbidzIG5vcm1hbCBjbGVhbnVw"
    "IHBhdGguCiAgICAgICAgICAgIHdobyA9IHNlbGYudXNlci5uYW1lIGlmIHNlbGYudXNlciBlbHNl"
    "IHNlbGYuY2xpZW50X2FkZHJlc3NbMF0KICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIHt3aG99"
    "OiBvdmVyIHtfTUFYX1NFTkRfQkFDS0xPR30gYnl0ZXMgcXVldWVkIHVucmVhZCwgZHJvcHBpbmcn"
    "KQogICAgICAgICAgICBzZWxmLmRyb3AoKQogICAgICAgICAgICByZXR1cm4KICAgICAgICBzZWxm"
    "Ll9zUXVldWUucHV0KG1zZykKICAgIGRlZiBkcm9wKHNlbGYpOgogICAgICAgICNFbmQgdGhpcyBj"
    "b25uZWN0aW9uIGZyb20gYW5vdGhlciB0aHJlYWQuIEZsYWdnaW5nIGl0IGZpcnN0IG1lYW5zIHRo"
    "ZQogICAgICAgICNyZWFkIGxvb3AgYmFpbHMgb3V0IGF0IGl0cyBuZXh0IHBhc3Mgbm8gbWF0dGVy"
    "IHdoYXQgdGhlIHNvY2tldCBkb2VzOwogICAgICAgICN0aGUgc2h1dGRvd24gaXMgd2hhdCB3YWtl"
    "cyBpdCBmcm9tIHNlbGVjdCgpIHN0cmFpZ2h0IGF3YXkuIEl0cyBvd24KICAgICAgICAjaGFuZGxl"
    "ciB0aHJlYWQgc3RpbGwgcnVucyB0aGUgbm9ybWFsIGZpbmlzaCgpL2NsZWFudXAgcGF0aCwgc28g"
    "dGhlCiAgICAgICAgI2FjY291bnQgaXMgcmVsZWFzZWQgYW5kIHRoZSB0b3duIHJvc3RlciB0aWRp"
    "ZWQgZXhhY3RseSBhcyBvbiBhbnkgb3RoZXIKICAgICAgICAjZGlzY29ubmVjdC4gTmV2ZXIgY2xv"
    "c2UoKSBoZXJlIC0gc2VlIGNsb3NlQ29ubmVjdGlvbnMoKS4KICAgICAgICBzZWxmLl9kcm9wcGVk"
    "LnNldCgpCiAgICAgICAgdHJ5OgogICAgICAgICAgICBzZWxmLnJlcXVlc3Quc2h1dGRvd24oc29j"
    "a2V0LlNIVVRfUkRXUikKICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgcGFzcyAj"
    "YWxyZWFkeSBnb25lLCBvciBuZXZlciBmdWxseSBjb25uZWN0ZWQKICAgIGRlZiBmbHVzaFBlbmRp"
    "bmcoc2VsZiwgdGltZW91dCk6CiAgICAgICAgI0Jlc3QtZWZmb3J0LCBzdHJpY3RseSBib3VuZGVk"
    "IHdhaXQgZm9yIHRoZSBvdXRib3VuZCBxdWV1ZSB0byBkcmFpbi4KICAgICAgICAjRm9yIGNhbGxl"
    "cnMgdGhhdCB3YW50IGEgbGFzdCBtZXNzYWdlIHRvIGhhdmUgbGVmdCBiZWZvcmUgdGhlIHNvY2tl"
    "dAogICAgICAgICNnb2VzIGRvd24gKHRoZSBhZG1pbiBraWNrKSB3aXRob3V0IGluaGVyaXRpbmcg"
    "YSBzdGFsbGVkIHBlZXIncyBzdGFsbC4KICAgICAgICBkZWFkbGluZSA9IHRpbWUubW9ub3Rvbmlj"
    "KCkgKyB0aW1lb3V0CiAgICAgICAgd2hpbGUgbm90IHNlbGYuX3NRdWV1ZS5lbXB0eSgpIGFuZCB0"
    "aW1lLm1vbm90b25pYygpIDwgZGVhZGxpbmU6CiAgICAgICAgICAgIHRpbWUuc2xlZXAoMC4wMikK"
    "ICAgIGRlZiBfd3JpdGVyTG9vcChzZWxmKToKICAgICAgICAjQmxvY2tzIG9uIHRoZSBxdWV1ZSBp"
    "bnN0ZWFkIG9mIGJlaW5nIHBvbGxlZC4gUHJldmlvdXNseSB0aGUgcmVhZCBsb29wCiAgICAgICAg"
    "I2RyYWluZWQgdGhpcyBxdWV1ZSBpdHNlbGYgYmV0d2VlbiByZWN2KCkgdGltZW91dHMsIHNvIGFu"
    "eXRoaW5nIHF1ZXVlZAogICAgICAgICNqdXN0IGFmdGVyIHRoZSB0aHJlYWQgd2VudCBiYWNrIGlu"
    "dG8gcmVjdigpIHdhaXRlZCBvdXQgdGhlIGZ1bGwKICAgICAgICAjdGltZW91dCAtIHVwIHRvIDEw"
    "MG1zIG9mIGxhdGVuY3kgYWRkZWQgdG8gZXZlcnkgcmVsYXllZCBnYW1lIGNvbW1hbmQsCiAgICAg"
    "ICAgI29uIHRvcCBvZiBldmVyeSBpZGxlIGNvbm5lY3Rpb24gd2FraW5nIDEwIHRpbWVzIGEgc2Vj"
    "b25kIHRvIGNoZWNrLgogICAgICAgIHRyeToKICAgICAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAg"
    "ICAgICAgICAgIG1zZyA9IHNlbGYuX3NRdWV1ZS5nZXQoKQogICAgICAgICAgICAgICAgaWYgbXNn"
    "IGlzIHNlbGYuX1NUT1BXUklURVI6CiAgICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAg"
    "ICAgICAgICNDb2FsZXNjZSB3aGF0ZXZlciBlbHNlIHBpbGVkIHVwIGJlaGluZCBpdCBpbnRvIGEg"
    "c2luZ2xlIHdyaXRlLgogICAgICAgICAgICAgICAgI1Bvc2l0aW9uIGJyb2FkY2FzdHMgYW5kIGdh"
    "bWUgY29tbWFuZHMgb2Z0ZW4gYXJyaXZlIGluIGJ1cnN0cy4KICAgICAgICAgICAgICAgIGNodW5r"
    "cyA9IFttc2ddCiAgICAgICAgICAgICAgICBzdG9wcGluZyA9IEZhbHNlCiAgICAgICAgICAgICAg"
    "ICB3aGlsZSBUcnVlOgogICAgICAgICAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgbnh0ID0gc2VsZi5fc1F1ZXVlLmdldF9ub3dhaXQoKQogICAgICAgICAgICAgICAgICAg"
    "IGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAg"
    "ICAgICAgICAgICAgaWYgbnh0IGlzIHNlbGYuX1NUT1BXUklURVI6CiAgICAgICAgICAgICAgICAg"
    "ICAgICAgIHN0b3BwaW5nID0gVHJ1ZQogICAgICAgICAgICAgICAgICAgICAgICBicmVhawogICAg"
    "ICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQobnh0KQogICAgICAgICAgICAgICAgcGF5bG9h"
    "ZCA9IGInJy5qb2luKGNodW5rcykKICAgICAgICAgICAgICAgICNSZWxlYXNlZCBiZWZvcmUgdGhl"
    "IHdyaXRlLCBub3QgYWZ0ZXI6IHRoZSBiYWNrbG9nIGV4aXN0cyB0bwogICAgICAgICAgICAgICAg"
    "I2Rlc2NyaWJlIHdoYXQgaXMgc3RpbGwgd2FpdGluZyBmb3IgdGhlIHNvY2tldCwgYW5kIHRoZXNl"
    "IGJ5dGVzCiAgICAgICAgICAgICAgICAjYXJlIG9uIHRoZWlyIHdheSBvdXQuIENvdW50aW5nIHRo"
    "ZW0gYXMgcGVuZGluZyBmb3IgdGhlIHdob2xlCiAgICAgICAgICAgICAgICAjZHVyYXRpb24gb2Yg"
    "YSBzbG93IHNlbmRhbGwoKSB3b3VsZCBtYWtlIGEgbWVyZWx5IHNsb3cgbGluayBsb29rCiAgICAg"
    "ICAgICAgICAgICAjbGlrZSB0aGUgd2VkZ2VkIGNsaWVudCB0aGUgY2FwIGlzIHRoZXJlIHRvIGNh"
    "dGNoLgogICAgICAgICAgICAgICAgd2l0aCBzZWxmLl9xTG9jazoKICAgICAgICAgICAgICAgICAg"
    "ICBzZWxmLl9xQnl0ZXMgLT0gbGVuKHBheWxvYWQpCiAgICAgICAgICAgICAgICBzZWxmLnNlbmRS"
    "YXcocGF5bG9hZCkKICAgICAgICAgICAgICAgIGlmIHN0b3BwaW5nOgogICAgICAgICAgICAgICAg"
    "ICAgIHJldHVybgogICAgICAgIGV4Y2VwdCAoQ29ubmVjdGlvblJlc2V0RXJyb3IsIENvbm5lY3Rp"
    "b25BYm9ydGVkRXJyb3IsIEJyb2tlblBpcGVFcnJvciwgT1NFcnJvcik6CiAgICAgICAgICAgIHBh"
    "c3MgI3BlZXIgaXMgZ29uZTsgdGhlIHJlYWQgbG9vcCBub3RpY2VzIGFuZCBydW5zIHRoZSBjbGVh"
    "bnVwCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcHJpbnQoJ1tMb2JieV0g"
    "V3JpdGVyIGVycm9yOlxuJyArIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICAgICAgZmluYWxs"
    "eToKICAgICAgICAgICAgc2VsZi5fd3JpdGVyRGVhZC5zZXQoKQogICAgZGVmIF9zdGFydFdyaXRl"
    "cihzZWxmKToKICAgICAgICBzZWxmLl93cml0ZXIgPSB0aHJlYWRpbmcuVGhyZWFkKHRhcmdldD1z"
    "ZWxmLl93cml0ZXJMb29wLCBkYWVtb249VHJ1ZSkKICAgICAgICBzZWxmLl93cml0ZXIuc3RhcnQo"
    "KQogICAgZGVmIF9zdG9wV3JpdGVyKHNlbGYpOgogICAgICAgIGlmIHNlbGYuX3dyaXRlciBpcyBO"
    "b25lOgogICAgICAgICAgICByZXR1cm4KICAgICAgICBzZWxmLl9zUXVldWUucHV0KHNlbGYuX1NU"
    "T1BXUklURVIpCiAgICAgICAgc2VsZi5fd3JpdGVyLmpvaW4odGltZW91dD0yLjApCiAgICAgICAg"
    "c2VsZi5fd3JpdGVyID0gTm9uZQogICAgZGVmIF9jbGFpbVNlc3Npb24oc2VsZik6CiAgICAgICAg"
    "I1Rha2Ugb3duZXJzaGlwIG9mIHRoZSB1c2VybmFtZSBzbG90IGJlZm9yZSB0ZWxsaW5nIHRoZSBj"
    "bGllbnQgaXQgaXMKICAgICAgICAjbG9nZ2VkIGluLiBSZXR1cm5zIEZhbHNlIGlmIGFub3RoZXIg"
    "Y29ubmVjdGlvbiBnb3QgdGhlcmUgZmlyc3QuCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuc3RhdGUu"
    "Y2xhaW1Vc2VyKHNlbGYudXNlci5uYW1lLCBzZWxmKToKICAgICAgICAgICAgcmV0dXJuIFRydWUK"
    "ICAgICAgICBzZWxmLnVzZXIuZGlzY29ubmVjdChzZWxmLnNlcnZlcikgI3JlbGVhc2VzIHRoZSBp"
    "ZG51bSB3ZSBqdXN0IGFsbG9jYXRlZAogICAgICAgIHNlbGYudXNlciA9IE5vbmUKICAgICAgICBy"
    "ZXR1cm4gRmFsc2UKICAgIGRlZiBhdHRlbXB0TG9naW4oc2VsZiwgdXNlcm5hbWUsIHBhc3N3b3Jk"
    "KToKICAgICAgICBpZiBsZW4odXNlcm5hbWUpPDE6CiAgICAgICAgICAgIHJldHVybiA0ICNObyBV"
    "c2VybmFtZSwgbGlrZWx5IGZyZXNoIGxvZ2luCiAgICAgICAgICAgICNUT0RPIGNoZWNrIGlmIHNl"
    "cmlhbCBleGlzdHMgYW5kIHJldHVybiB1c2VybmFtZSBwcm9wZXJseQogICAgICAgIGlmIGxlbihw"
    "YXNzd29yZCk8MToKICAgICAgICAgICAgcmV0dXJuIDMgI1Bhc3N3b3JkIHRvbyBzaG9ydAogICAg"
    "ICAgICNUZXN0IGlmIHBsYXllciBhbHJlYWR5IGxvZ2dlZCBpbiAoZmFzdCBwYXRoOyB0aGUgYXV0"
    "aG9yaXRhdGl2ZSwKICAgICAgICAjcmFjZS1mcmVlIGNoZWNrIGlzIHRoZSBjbGFpbVVzZXIoKSBi"
    "ZWxvdykKICAgICAgICBpZiBzZWxmLnNlcnZlci5nZXRQbGF5ZXIodXNlcm5hbWUpOgogICAgICAg"
    "ICAgICByZXR1cm4gMiAjVE9ETyBQTEFZRVIgTE9HR0VEIElOIEVSUk9SCiAgICAgICAgI3BsYXll"
    "ciBub3QgY3VycmVudGx5IGxvZ2dlZCBpbiwgYXR0ZW1wdCB0byBsb2dpbiB2aWEgZGF0YSBoYW5k"
    "bGVyCiAgICAgICAgc2VsZi51c2VyID0gR0RILmxvZ2luUGxheWVyKHVzZXJuYW1lLCBzZWxmLCBw"
    "YXNzd29yZCkKICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgIHJldHVybiAwIGlmIHNl"
    "bGYuX2NsYWltU2Vzc2lvbigpIGVsc2UgMgogICAgICAgIHJldHVybiAxICNUT0RPIEdldCBmcm9t"
    "IEdESC5sb2dpblBsYXllciwgcGFzcyB1c2VyIG9iamVjdCBhbG9uZz8KICAgIGRlZiBhdHRlbXB0"
    "UmVnaXN0ZXIoc2VsZiwgdXNlcm5hbWUsIHBhc3N3b3JkLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwg"
    "Z2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgI1Rlc3QgaWYgcGxheWVyIGFscmVhZHkgbG9n"
    "Z2VkIGluCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuZ2V0UGxheWVyKHVzZXJuYW1lKToKICAgICAg"
    "ICAgICAgcmV0dXJuIDEgI1RPRE8gUExBWUVSIExPR0dFRCBJTiBFUlJPUgogICAgICAgIHNlbGYu"
    "dXNlciA9IEdESC5yZWdpc3RlclBsYXllcih1c2VybmFtZSwgc2VsZiwgcGFzc3dvcmQsIGVtYWls"
    "LCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKQogICAgICAgIGlmIHNlbGYudXNl"
    "cjoKICAgICAgICAgICAgcmV0dXJuIDAgaWYgc2VsZi5fY2xhaW1TZXNzaW9uKCkgZWxzZSAxCiAg"
    "ICAgICAgcmV0dXJuIDIgI1RPRE8gZ2V0IGVycm9yIGZyb20gR0RICiAgICBkZWYgaGFuZGxlKHNl"
    "bGYpOgogICAgICAgIHRyeTogI0ludGVyY2VwdCBhbmQgcHJpbnQgZXJyb3JzIGZvciBkZWJ1Z2dp"
    "bmcKICAgICAgICAgICAgc2VsZi5faGFuZGxlKCkKICAgICAgICAgICAgI1RPRE8gbG9vcCBsb2Ji"
    "eSBoYW5kbGUgYmV0dGVyIHRvIGhhbmRsZSBleGNlcHRpb25zIGdyYWNlZnVsbHkKICAgICAgICAg"
    "ICAgc2VsZi5fbG9iYnlIYW5kbGUoKQogICAgICAgIGV4Y2VwdCBQcm90b2NvbEVycm9yIGFzIGU6"
    "CiAgICAgICAgICAgICNtYWxmb3JtZWQvb3ZlcnNpemVkIGlucHV0IC0gdGhlIGNsaWVudCdzIGZh"
    "dWx0LCBub3Qgb3Vycy4gRHJvcCB0aGUKICAgICAgICAgICAgI2Nvbm5lY3Rpb24gd2l0aCBvbmUg"
    "bGluZSBpbnN0ZWFkIG9mIGEgdHJhY2ViYWNrLgogICAgICAgICAgICB3aG8gPSBzZWxmLnVzZXIu"
    "bmFtZSBpZiBzZWxmLnVzZXIgZWxzZSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAgICAgICAg"
    "IHByaW50KGYnW0xvYmJ5XSBQcm90b2NvbCBlcnJvciBmcm9tIHt3aG99OiB7ZX0nKQogICAgICAg"
    "IGV4Y2VwdCAoemxpYi5lcnJvciwgc3RydWN0LmVycm9yLCBVbmljb2RlRGVjb2RlRXJyb3IpIGFz"
    "IGU6CiAgICAgICAgICAgICN0cnVuY2F0ZWQvZ2FyYmFnZSBwYWNrZXQ6IHBhcnNlRHN0ciBhbmQg"
    "c3RydWN0LnVucGFjayBib3RoIHJhaXNlIG9uCiAgICAgICAgICAgICNzaG9ydCByZWFkcywgYW5k"
    "IC5kZWNvZGUoKSBvbiBub24tYXNjaWkganVuay4gU2FtZSBjYXRlZ29yeS4KICAgICAgICAgICAg"
    "cHJpbnQoZidbTG9iYnldIE1hbGZvcm1lZCBwYWNrZXQgZnJvbSB7c2VsZi5jbGllbnRfYWRkcmVz"
    "c1swXX06ICcKICAgICAgICAgICAgICAgICAgZid7dHlwZShlKS5fX25hbWVfX306IHtlfScpCiAg"
    "ICAgICAgZXhjZXB0IChDb25uZWN0aW9uUmVzZXRFcnJvciwgQ29ubmVjdGlvbkFib3J0ZWRFcnJv"
    "ciwgT1NFcnJvcikgYXMgZToKICAgICAgICAgICAgIyBleHBlY3RlZCBmb3JtIG9mIGRpc2Nvbm5l"
    "Y3Rpb24gKGluY2x1ZGluZyBhIGZvcmNlZCBhZG1pbiBraWNrKSwKICAgICAgICAgICAgIyBidXQg"
    "bGVhdmUgYSBvbmUtbGluZSBicmVhZGNydW1iIHJhdGhlciB0aGFuIHN0YXlpbmcgZnVsbHkgc2ls"
    "ZW50CiAgICAgICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgICAgIHByaW50KGYnW0xv"
    "YmJ5XSBDb25uZWN0aW9uIGNsb3NlZCBmb3Ige3NlbGYudXNlci5uYW1lfToge2V9JykKICAgICAg"
    "ICBleGNlcHQgRXhjZXB0aW9uOiMgYXMgZToKICAgICAgICAgICAgcHJpbnQodHJhY2ViYWNrLmZv"
    "cm1hdF9leGMoKSkKICAgICAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICAgICAgcHJp"
    "bnQoZidVc2VyOiB7c2VsZi51c2VyLm5hbWV9JykKICAgICAgICAgICAgI3JhaXNlIGUKICAgIGRl"
    "ZiBfbG9iYnlIYW5kbGUoc2VsZik6CiAgICAgICAgI2FjdGl2ZVVzZXJzWy4uLl0gPSBzZWxmIHVz"
    "ZWQgdG8gaGFwcGVuIGhlcmU7IGl0IG5vdyBoYXBwZW5zIHVuZGVyIGEKICAgICAgICAjbG9jayBp"
    "bnNpZGUgYXR0ZW1wdExvZ2luL2F0dGVtcHRSZWdpc3RlciwgYmVmb3JlIHRoZSB3ZWxjb21lIHBh"
    "Y2tldAogICAgICAgICNnb2VzIG91dCwgc28gdHdvIGxvZ2lucyBmb3Igb25lIGFjY291bnQgY2Fu"
    "J3QgYm90aCBzdWNjZWVkLgogICAgICAgIHByaW50KGYnVXNlcjoge3NlbGYudXNlci5uYW1lfSBD"
    "b25uZWN0ZWQnKQogICAgICAgICNGcm9tIGhlcmUgb24gbm90aGluZyB3cml0ZXMgdG8gdGhlIHNv"
    "Y2tldCBpbmxpbmU6IHRoZSB3cml0ZXIgdGhyZWFkCiAgICAgICAgI293bnMgdGhlIG91dGJvdW5k"
    "IGRpcmVjdGlvbiBhbmQgdGhpcyBsb29wIG9ubHkgcmVhZHMuCiAgICAgICAgc2VsZi5fc3RhcnRX"
    "cml0ZXIoKQogICAgICAgIHNlbGYuX2xhc3RSZWN2ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAg"
    "ICNUaGUgc29ja2V0IHN0YXlzIGluIGJsb2NraW5nIG1vZGUgZm9yIGl0cyB3aG9sZSBsaWZlIGZy"
    "b20gaGVyZSBvbiwgYW5kCiAgICAgICAgI3JlYWRpbmVzcyBpcyB3YWl0ZWQgZm9yIHdpdGggc2Vs"
    "ZWN0KCkgaW5zdGVhZCBvZiBhIHNvY2tldCB0aW1lb3V0LgogICAgICAgICNUaGlzIGlzIG5vdCBh"
    "IHN0eWxlIHByZWZlcmVuY2UgLSBhIHNvY2tldCB0aW1lb3V0IGlzIGEgcHJvcGVydHkgb2YgdGhl"
    "CiAgICAgICAgIypzb2NrZXQqLCBub3Qgb2YgdGhlIGNhbGwsIHNvIHRoZSBzZXR0aW1lb3V0KF9S"
    "RUFEX1RJTUVPVVQpIHRoaXMgbG9vcAogICAgICAgICN1c2VkIHRvIGRvIG9uIGV2ZXJ5IHBhc3Mg"
    "YWxzbyBhcm1lZCBhIDFzIHRpbWVvdXQgb24gdGhlIHdyaXRlcgogICAgICAgICN0aHJlYWQncyBj"
    "b25jdXJyZW50IHNlbmRhbGwoKS4gQSBjbGllbnQgd2hvc2UgcmVjZWl2ZSB3aW5kb3cgd2FzIGZ1"
    "bGwKICAgICAgICAjZm9yIGEgc2Vjb25kIChleGFjdGx5IHRoZSBjYXNlIGR1cmluZyBhIGJ1c3kg"
    "Y28tb3Agc2Vzc2lvbikgbWFkZSB0aGF0CiAgICAgICAgI3NlbmRhbGwoKSByYWlzZSBUaW1lb3V0"
    "RXJyb3IgKmFmdGVyIGhhdmluZyBhbHJlYWR5IHdyaXR0ZW4gcGFydCBvZiB0aGUKICAgICAgICAj"
    "cGFja2V0KjogdGhlIHdyaXRlciB0aHJlYWQgZGllZCwgYW5kIHdoYXRldmVyIHRoZSBjbGllbnQg"
    "aGFkIHJlY2VpdmVkCiAgICAgICAgI3dhcyBoYWxmIGEgbWVzc2FnZSwgc28gaXRzIGNvbW1hbmQg"
    "c3RyZWFtIHdhcyBkZXN5bmNocm9uaXNlZCBmcm9tCiAgICAgICAgI3RoYXQgcG9pbnQgb24uIHNl"
    "bGVjdCgpIGxlYXZlcyB0aGUgc29ja2V0IGJsb2NraW5nLCBzbyB3cml0ZXMgYXJlCiAgICAgICAg"
    "I25ldmVyIGludGVycnVwdGVkLCB3aGlsZSByZWFkcyBzdGlsbCB3YWtlIHVwIHJlZ3VsYXJseSBl"
    "bm91Z2ggdG8KICAgICAgICAjbm90aWNlIHNodXRkb3duIGFuZCB0aGUgaWRsZSBkZWFkbGluZS4K"
    "ICAgICAgICBzZWxmLnJlcXVlc3Quc2V0dGltZW91dChOb25lKQogICAgICAgIHdoaWxlIFRydWU6"
    "CiAgICAgICAgICAgIGlmIHNlbGYuX2Ryb3BwZWQuaXNfc2V0KCk6CiAgICAgICAgICAgICAgICBi"
    "cmVhayAja2lja2VkLCBvciBkcm9wcGVkIGZvciBhbiB1bnJlYWQgc2VuZCBiYWNrbG9nCiAgICAg"
    "ICAgICAgIGlmIHNlbGYuX3dyaXRlckRlYWQuaXNfc2V0KCk6CiAgICAgICAgICAgICAgICBicmVh"
    "ayAjcGVlciB3ZW50IGF3YXkgd2hpbGUgd2Ugd2VyZSBzZW5kaW5nCiAgICAgICAgICAgIGlmIHNl"
    "bGYuc2VydmVyLl9pc19jbG9zaW5nOgogICAgICAgICAgICAgICAgYnJlYWsgI3NlcnZlciBpcyBz"
    "dG9wcGluZyAtIGNoZWNrZWQgaGVyZSwgbm90IG9ubHkgb24gYW4gaWRsZQogICAgICAgICAgICAg"
    "ICAgICAgICAgI3RpbWVvdXQsIHNvIGEgY2xpZW50IHRoYXQga2VlcHMgdGFsa2luZyBjYW5ub3Qg"
    "a2VlcCBpdHMKICAgICAgICAgICAgICAgICAgICAgICNoYW5kbGVyIHRocmVhZCAoYW5kIGl0cyBs"
    "b2cgc3BhbSkgYWxpdmUgcGFzdCBzaHV0ZG93bgogICAgICAgICAgICB0cnk6CiAgICAgICAgICAg"
    "ICAgICByZWFkeSwgXywgXyA9IHNlbGVjdC5zZWxlY3QoW3NlbGYucmVxdWVzdF0sIFtdLCBbXSwg"
    "X1JFQURfVElNRU9VVCkKICAgICAgICAgICAgZXhjZXB0IChPU0Vycm9yLCBWYWx1ZUVycm9yKToK"
    "ICAgICAgICAgICAgICAgIGJyZWFrICNzb2NrZXQgY2xvc2VkIHVuZGVyIHVzIChhZG1pbiBraWNr"
    "IC8gc2h1dGRvd24pCiAgICAgICAgICAgIGlmIG5vdCByZWFkeToKICAgICAgICAgICAgICAgIGlm"
    "IHNlbGYuc2VydmVyLl9pc19jbG9zaW5nOgogICAgICAgICAgICAgICAgICAgIGJyZWFrICNTZXJ2"
    "ZXIgU2h1dHRpbmcgZG93bgogICAgICAgICAgICAgICAgaWYgX0lETEVfVElNRU9VVCBhbmQgKHRp"
    "bWUubW9ub3RvbmljKCkgLSBzZWxmLl9sYXN0UmVjdikgPiBfSURMRV9USU1FT1VUOgogICAgICAg"
    "ICAgICAgICAgICAgICNIYWxmLW9wZW4gY29ubmVjdGlvbjogdGhlIHBlZXIgaXMgdW5yZWFjaGFi"
    "bGUgYnV0IG5ldmVyCiAgICAgICAgICAgICAgICAgICAgI3NlbnQgYSBGSU4vUlNULCBzbyByZWN2"
    "KCkgYmxvY2tzIGZvcmV2ZXIgYW5kIHRoZSBhY2NvdW50CiAgICAgICAgICAgICAgICAgICAgI3N0"
    "YXlzIGNsYWltZWQuIFJlYXAgaXQgc28gdGhlIHBsYXllciBjYW4gbG9nIGJhY2sgaW4uCiAgICAg"
    "ICAgICAgICAgICAgICAgcHJpbnQoZidbTG9iYnldIHtzZWxmLnVzZXIubmFtZX0gaWRsZSBmb3Ig"
    "e19JRExFX1RJTUVPVVR9cywgZHJvcHBpbmcnKQogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAg"
    "ICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBybXNnID0gc2VsZi5yZXF1ZXN0LnJl"
    "Y3YoUkVDVl9CVUZfTEVOKSAjVE9ETyBsb2cgbmV0d29yayBieXRlcmF0ZQogICAgICAgICAgICBp"
    "ZiBub3Qgcm1zZzoKICAgICAgICAgICAgICAgIGJyZWFrICNEaXNjb25uZWN0ZWQKICAgICAgICAg"
    "ICAgc2VsZi5kYXRhKz1ybXNnCiAgICAgICAgICAgIHNlbGYuX2xhc3RSZWN2ID0gdGltZS5tb25v"
    "dG9uaWMoKQogICAgICAgICAgICB3aGlsZSBzZWxmLmRhdGE6CiAgICAgICAgICAgICAgICB0cnk6"
    "CiAgICAgICAgICAgICAgICAgICAgY21kX2wgPSBzZWxmLmRhdGEuaW5kZXgoMCkKICAgICAgICAg"
    "ICAgICAgIGV4Y2VwdCBWYWx1ZUVycm9yOgogICAgICAgICAgICAgICAgICAgICNwcmludCgnY21k"
    "IGRlY29kZSBlcnJvcjpcbicsIHRyYWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICAgICAgICAgICAg"
    "ICAgICAgYnJlYWs7I01heSByZXF1aXJlIG1vcmUgZGF0YQogICAgICAgICAgICAgICAgY21kID0g"
    "d2lyZV9kZWNvZGUoc2VsZi5kYXRhWzA6Y21kX2xdKQogICAgICAgICAgICAgICAgc2VsZi5kYXRh"
    "ID0gc2VsZi5kYXRhW2NtZF9sKzE6XQogICAgICAgICAgICAgICAgcmVzcG9uc2UgPSBzZWxmLnNl"
    "cnZlci5jb21wYXJzLnBhcnNlKGNtZCwgc2VsZikKICAgICAgICAgICAgICAgIGlmIHJlc3BvbnNl"
    "OgogICAgICAgICAgICAgICAgICAgICNRdWV1ZWQgcmF0aGVyIHRoYW4gc2VudCBpbmxpbmUsIHNv"
    "IHRoaXMgY29ubmVjdGlvbiBoYXMgYQogICAgICAgICAgICAgICAgICAgICNzaW5nbGUgb3JkZXJl"
    "ZCBvdXRib3VuZCBzdHJlYW0uIFNlbmRpbmcgaGVyZSBkaXJlY3RseQogICAgICAgICAgICAgICAg"
    "ICAgICN3b3VsZCByYWNlIHRoZSB3cml0ZXIgdGhyZWFkIGFuZCBjb3VsZCBsYW5kIGluIHRoZSBt"
    "aWRkbGUKICAgICAgICAgICAgICAgICAgICAjb2YgYSBicm9hZGNhc3QgaXQgaXMgYWxyZWFkeSB3"
    "cml0aW5nLgogICAgICAgICAgICAgICAgICAgIHNlbGYuc2VuZChyZXNwb25zZSkKICAgICAgICAg"
    "ICAgICAgICNMb29zZSBibG9icyBzaG91bGQgbm90IGhhcHBlbiBhbnltb3JlIGhvcGVmdWxseQog"
    "ICAgICAgICAgICAgICAgI1RPRE8gZml4IHVuY29tcHJlc3NlZCBkYXRhIGJsb2JzPwogICAgICAg"
    "ICAgICAgICAgI1RPRE8gc2tpcCAxIGJ5dGUgb25seSB3aGVuIGRlY29kZSBlcnJvcj8KICAgICAg"
    "ICAgICAgICAgIGlmIChsZW4oc2VsZi5kYXRhKT4yIGFuZAogICAgICAgICAgICAgICAgICAgICAg"
    "ICBzZWxmLmRhdGFbMF09PTB4NzggYW5kCiAgICAgICAgICAgICAgICAgICAgICAgIHNlbGYuZGF0"
    "YVsxXT09MHg5Yyk6CiAgICAgICAgICAgICAgICAgICAgI0xvb3NlIHVuaGFuZGxlZCBibG9iIGFm"
    "dGVyIGNvbW1hbmQKICAgICAgICAgICAgICAgICAgICBibG9iLCBzZWxmLmRhdGEgPSBwX2dldEJs"
    "b2Ioc2VsZi5kYXRhLCBzZWxmLnJlcXVlc3QpCiAgICAgICAgICAgICAgICAgICAgI1RoZSBvdGhl"
    "ciBibGluZCBzcG90OiBhbnl0aGluZyB0aGUgY2xpZW50IHNlbmRzIGFzIGEKICAgICAgICAgICAg"
    "ICAgICAgICAjY29tcHJlc3NlZCBibG9iIHJhdGhlciB0aGFuIGEgdGV4dCBjb21tYW5kIHdhcyBy"
    "ZWFkIGFuZAogICAgICAgICAgICAgICAgICAgICN0aHJvd24gYXdheSB3aXRob3V0IGEgdHJhY2Uu"
    "CiAgICAgICAgICAgICAgICAgICAgaWYgX0RFQlVHX0xPR19DT01NQU5EUzoKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgd2hvID0gc2VsZi51c2VyLm5hbWUgaWYgc2VsZi51c2VyIGVsc2UgJz8nCiAg"
    "ICAgICAgICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gLT4gKFVOSEFORExFRCBC"
    "TE9CIGFmdGVyIHtjbWQhcn0pICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZid7bGVu"
    "KGJsb2IpfSBieXRlcycpCiAgICBkZWYgX3JlY3ZNb3JlKHNlbGYpOgogICAgICAgIGNodW5rID0g"
    "c2VsZi5yZXF1ZXN0LnJlY3YoUkVDVl9CVUZfTEVOKQogICAgICAgIGlmIG5vdCBjaHVuazoKICAg"
    "ICAgICAgICAgI3BlZXIgZGlzY29ubmVjdGVkIGR1cmluZyBoYW5kc2hha2UvbG9naW4sIHN0b3Ag"
    "dGhlIGJ1c3ktbG9vcAogICAgICAgICAgICByYWlzZSBDb25uZWN0aW9uUmVzZXRFcnJvcignZGlz"
    "Y29ubmVjdGVkIGR1cmluZyBsb2dpbicpCiAgICAgICAgc2VsZi5kYXRhICs9IGNodW5rCiAgICBk"
    "ZWYgX2hhbmRsZShzZWxmKToKICAgICAgICAjVE9ETyBsb2cgbG9naW4gYXR0ZW1wdHM/CiAgICAg"
    "ICAgcGVlcl9pcCA9IHNlbGYuY2xpZW50X2FkZHJlc3NbMF0KICAgICAgICBwcmludCgnQ29ubmVj"
    "dGlvbiBhdHRlbXB0IGZyb206JywgcGVlcl9pcCkKICAgICAgICBMSVMgPSAyICNsb2dpbiBzdGF0"
    "ZSAjVE9ETyBjb25zaWRlciBsb25nIHRpbWVvdXRzPwogICAgICAgIHdoaWxlIExJUzoKICAgICAg"
    "ICAgICAgd2hpbGUgbGVuKHNlbGYuZGF0YSk8NDoKICAgICAgICAgICAgICAgIHNlbGYuX3JlY3ZN"
    "b3JlKCkKICAgICAgICAgICAgcGFja19sZW4gPSBzdHJ1Y3QudW5wYWNrKCc8SScsc2VsZi5kYXRh"
    "WzA6NF0pWzBdCiAgICAgICAgICAgIGlmIHBhY2tfbGVuIDwgNCBvciBwYWNrX2xlbiA+IF9NQVhf"
    "SEFORFNIQUtFOgogICAgICAgICAgICAgICAgI3VudmFsaWRhdGVkLCB0aGlzIGlzIGEgcHJlLWF1"
    "dGhlbnRpY2F0aW9uIG1lbW9yeSBib21iOiBhbgogICAgICAgICAgICAgICAgI3VuYXV0aGVudGlj"
    "YXRlZCBwZWVyIGFubm91bmNlcyBhIDRHQiBwYWNrZXQgYW5kIHRoZSBsb29wIGJlbG93CiAgICAg"
    "ICAgICAgICAgICAjYnVmZmVycyB1bnRpbCB0aGUgcHJvY2VzcyBkaWVzCiAgICAgICAgICAgICAg"
    "ICByYWlzZSBQcm90b2NvbEVycm9yKGYnaGFuZHNoYWtlIHBhY2tldCBsZW5ndGgge3BhY2tfbGVu"
    "fSBvdXQgb2YgcmFuZ2UnKQogICAgICAgICAgICB3aGlsZShsZW4oc2VsZi5kYXRhKTxwYWNrX2xl"
    "bik6CiAgICAgICAgICAgICAgICBzZWxmLl9yZWN2TW9yZSgpCiAgICAgICAgICAgICNzbGljZSB0"
    "byBwYWNrX2xlbiAobm90IHRvIHRoZSBlbmQgb2YgdGhlIGJ1ZmZlcik6IGFueXRoaW5nIHBhc3QK"
    "ICAgICAgICAgICAgI3RoaXMgcGFja2V0IGJlbG9uZ3MgdG8gdGhlIG5leHQgb25lLiBCb3VuZGVk"
    "IGRlY29tcHJlc3MsIGJlY2F1c2UgYQogICAgICAgICAgICAjNjRrIGhhbmRzaGFrZSBvZiBjb21w"
    "cmVzc2VkIHplcm9lcyBleHBhbmRzIHRvIGh1bmRyZWRzIG9mIE1CLgogICAgICAgICAgICByZXMg"
    "PSBfZGVjb21wcmVzc19ib3VuZGVkKHNlbGYuZGF0YVs0OnBhY2tfbGVuXSwgX01BWF9IQU5EU0hB"
    "S0VfSU5GTEFURUQpCiAgICAgICAgICAgIHNlbGYuZGF0YSA9IHNlbGYuZGF0YVtwYWNrX2xlbjpd"
    "CiAgICAgICAgICAgIGlmIExJUyA9PSAyOgogICAgICAgICAgICAgICAgZ2FtZXZlcnNpb24gPSBy"
    "ZXNbMDoxNl0gI1RPRE8gbm90ZSBnYW1lIHZlcnNpb24gKHVudmVyaWZpZWQpIHBlciB1c2VyCiAg"
    "ICAgICAgICAgICAgICBsYW5nbmFtZSwgb2ZmID0gcGFyc2VEc3RyKHJlcywgMTYpCiAgICAgICAg"
    "ICAgICAgICAjVE9ETyBjb25zaWRlciBUV1NFIGluZGljYXRvciB0byBjcmVhdGUgc2VjdXJlIGNv"
    "bm5lY3Rpb24/CiAgICAgICAgICAgICAgICAjVE9ETyBjaGVjayBpZiB2YW5pbGxhIHNlcnZlciBp"
    "Z25vcmVzIGV4dHJhIGRhdGEgaW4gaGFuZHNoYWtlIHByb2Nlc3MKICAgICAgICAgICAgICAgIFJL"
    "ID0gcmVzW29mZis4Om9mZisxNl0KICAgICAgICAgICAgICAgIGZvciBpIGluIHJhbmdlKGxlbihS"
    "SykpOgogICAgICAgICAgICAgICAgICAgIHNlbGYuU0tbaV1ePVJLW2ldCiAgICAgICAgICAgICAg"
    "ICAjd2FzIGhhcmRjb2RlZCAnVFcxQ1MnIHdpdGggYSAiU0VSVkVSIE5BTUUgY2ZnVE9ETyIgbm90"
    "ZTogdGhlCiAgICAgICAgICAgICAgICAjbmFtZSBjb25maWd1cmVkIGluIENvbmZpZy5pbmkvdGhl"
    "IEdVSSByZWFjaGVkIHRoZSB3ZWxjb21lCiAgICAgICAgICAgICAgICAjcGFja2V0IGJ1dCBuZXZl"
    "ciB0aGlzIG9uZSwgc28gdGhlIHByZS1sb2dpbiBoYW5kc2hha2UgYWx3YXlzCiAgICAgICAgICAg"
    "ICAgICAjYW5ub3VuY2VkIHRoZSBwbGFjZWhvbGRlci4KICAgICAgICAgICAgICAgIHNlbGYuc2Vu"
    "ZFJhdyhfc2VydmVyX2luZm9fcGFja2V0KHNhbml0aXplVGV4dChERUZBVUxUX1RJVExFKSkpCiAg"
    "ICAgICAgICAgICAgICAjVE9ETyBUVzFDUyBpbmRpY2F0b3IgZm9yIFRXU0UgY2xpZW50IHRvIGNy"
    "ZWF0ZSBzZWN1cmUgY29ubmVjdGlvbiBvciBwcmUtaGFzaCBwYXNzd29yZD8KICAgICAgICAgICAg"
    "ICAgIExJUyA9IDEgCiAgICAgICAgICAgICAgICBzZWxmLlNLID0gYnl0ZXMoc2VsZi5TSykKICAg"
    "ICAgICAgICAgZWxpZiBMSVMgPT0gMToKICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSAtMQog"
    "ICAgICAgICAgICAgICAgI1N0YWxsIHJlcGVhdCBvZmZlbmRlcnMgYmVmb3JlIGRvaW5nIGFueSBQ"
    "QktERjIgd29yayBmb3IgdGhlbS4KICAgICAgICAgICAgICAgICNTbGVlcGluZyBpbiB0aGlzIGhh"
    "bmRsZXIgdGhyZWFkIGlzIHRoZSBwb2ludDogaXQgY29zdHMgdXMKICAgICAgICAgICAgICAgICNu"
    "b3RoaW5nIGFuZCByYXRlLWxpbWl0cyB0aGF0IGNvbm5lY3Rpb24uCiAgICAgICAgICAgICAgICBk"
    "ZWxheSA9IExPR0lOX1RIUk9UVExFLmRlbGF5Rm9yKHBlZXJfaXApCiAgICAgICAgICAgICAgICBp"
    "ZiBkZWxheToKICAgICAgICAgICAgICAgICAgICB0aW1lLnNsZWVwKGRlbGF5KQogICAgICAgICAg"
    "ICAgICAgdXNlcm5hbWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDApCiAgICAgICAgICAgICAgICBw"
    "YXNzd29yZCwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAgICAgICAgI1RPRE8g"
    "VFdTRSBtb2QgZm9yIGhpZ2hlciBsb2dpbiBzZWN1cml0eQogICAgICAgICAgICAgICAgIy1lbmNy"
    "eXB0ZWQgY29ubmVjdGlvbiB0byBwcmV2ZW50IHJlcGxheSBhdHRhY2tzCiAgICAgICAgICAgICAg"
    "ICAjLXByZWhhc2ggcGFzc3dvcmQgd2l0aCBzZXJpYWw/LCBjaGVjayBpZiByZWNvdmVyeSBwb3Nz"
    "aWJsZS4KICAgICAgICAgICAgICAgIHNlbGYuZ3VpZCA9IGJ5dGVzKHJlc1tvZmY6b2ZmKzE2XSkK"
    "ICAgICAgICAgICAgICAgICNwcmludCgnZ3VpZCBieXRlOicsIHNlbGYuZ3VpZFsxXSkKICAgICAg"
    "ICAgICAgICAgICNzZWxmLmd1aWQgPSBieXRlYXJyYXkocmVzW29mZjpvZmYrMTZdKQogICAgICAg"
    "ICAgICAgICAgI3NlbGYuZ3VpZFsxXV49MHgxNiAjRE8gTk9UIHBlcmZvcm0gc2VydmVyc2lkZQog"
    "ICAgICAgICAgICAgICAgI3NlbGYuZ3VpZCA9IGJ5dGVzKHNlbGYuZ3VpZCkKICAgICAgICAgICAg"
    "ICAgIG9mZis9MTYKICAgICAgICAgICAgICAgIGlzcmVnID0gc3RydWN0LnVucGFjaygnPEknLHJl"
    "c1tvZmY6b2ZmKzRdKVswXQogICAgICAgICAgICAgICAgb2ZmKz00CiAgICAgICAgICAgICAgICB2"
    "aWFSZWdpc3RlciA9IGJvb2woaXNyZWcpCiAgICAgICAgICAgICAgICBpZiBpc3JlZzoKICAgICAg"
    "ICAgICAgICAgICAgICBlbWFpbCwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAgICAg"
    "ICAgICAgICAgIGxvY2F0aW9uLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAgICAg"
    "ICAgICAgICAgYWdlID0gcmVzW29mZl0KICAgICAgICAgICAgICAgICAgICBnZW5kZXIgPSByZXNb"
    "b2ZmKzFdCiAgICAgICAgICAgICAgICAgICAgb2ZmKz0yICNhZ2UsIGdlbmRlcgogICAgICAgICAg"
    "ICAgICAgICAgIGRlc2NyaXB0aW9uLCBvZmYgPSBwYXJzZURzdHIocmVzLCBvZmYpCiAgICAgICAg"
    "ICAgICAgICAgICAgbG9naW5FcnJvciA9IHNlbGYuYXR0ZW1wdFJlZ2lzdGVyKHVzZXJuYW1lLCBw"
    "YXNzd29yZCwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICAg"
    "ICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSBzZWxmLmF0"
    "dGVtcHRMb2dpbih1c2VybmFtZSwgcGFzc3dvcmQpCiAgICAgICAgICAgICAgICAgICAgaWYgbG9n"
    "aW5FcnJvciA9PSAxIGFuZCBfQVVUT19SRUdJU1RFUjoKICAgICAgICAgICAgICAgICAgICAgICAg"
    "dmlhUmVnaXN0ZXIgPSBUcnVlCiAgICAgICAgICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSBz"
    "ZWxmLmF0dGVtcHRSZWdpc3Rlcih1c2VybmFtZSwgcGFzc3dvcmQsICIiLCAiIiwgMSwgMCwgIiIp"
    "CiAgICAgICAgICAgICAgICAgICAgICAgIGlmIGxvZ2luRXJyb3IgYW5kIEdESC5uYW1lVGFrZW4o"
    "dXNlcm5hbWUpOgogICAgICAgICAgICAgICAgICAgICAgICAgICAgI1RoZSBhY2NvdW50IGV4aXN0"
    "cywgc28gdGhpcyB3YXMgbmV2ZXIgYQogICAgICAgICAgICAgICAgICAgICAgICAgICAgI3JlZ2lz"
    "dHJhdGlvbjogdGhlIGxvZ2luIGJlZm9yZSBpdCBmYWlsZWQgb24gdGhlCiAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAjcGFzc3dvcmQgb3IgLSBmYXIgbW9yZSBvZnRlbiAtIG9uIHRoZSBzZXJp"
    "YWwsCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjYmVjYXVzZSBhY2NvdW50cyBhcmUgYm91"
    "bmQgdG8gdGhlIGtleSB0aGUgY2xpZW50CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjaGFu"
    "ZHNoYWtlcyB3aXRoIChzZWUgbG9naW5QbGF5ZXIncyBzdHJpY3QgbG9va3VwKS4KICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICNGYWxsaW5nIHRocm91Z2ggdG8gdGhlIHJlZ2lzdHJhdGlvbiB3"
    "b3JkaW5nIHRvbGQgYQogICAgICAgICAgICAgICAgICAgICAgICAgICAgI3BsYXllciB3aG8gaGFk"
    "IHJlaW5zdGFsbGVkIHRoZSBnYW1lIHRoYXQgdGhlaXIKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICMqdXNlcm5hbWUqIHdhcyBpbnZhbGlkLCB3aGljaCBzZW50IHRoZW0gb2ZmCiAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAjaW52ZW50aW5nIG5ldyBuYW1lcyB0aGF0IGNvdWxkIG5ldmVy"
    "IHdvcmsuCiAgICAgICAgICAgICAgICAgICAgICAgICAgICB2aWFSZWdpc3RlciA9IEZhbHNlCiAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gNQogICAgICAgICAgICAgICAg"
    "aWYgbG9naW5FcnJvciA9PSAwOgogICAgICAgICAgICAgICAgICAgIExPR0lOX1RIUk9UVExFLnJl"
    "Y29yZFN1Y2Nlc3MocGVlcl9pcCkKICAgICAgICAgICAgICAgICAgICAjVE9ETyBiZXR0ZXIgaGFu"
    "ZGxpbmcgb2YgVElUTEUgQU5EIE1PVEQKICAgICAgICAgICAgICAgICAgICBzZWxmLnNlbmRSYXco"
    "X3NlcnZlcl93ZWxjb21lX3BhY2tldChieXRlcyhzZWxmLlNLKSwgREVGQVVMVF9USVRMRSwgREVG"
    "QVVMVF9NT1REKSkKICAgICAgICAgICAgICAgICAgICBMSVMgPSAwCiAgICAgICAgICAgICAgICBl"
    "bHNlOiAjZXJyb3IgYmFzZWQgb24gbG9naW5FcnJvciBudW1iZXIKICAgICAgICAgICAgICAgICAg"
    "ICBjb3VudCA9IExPR0lOX1RIUk9UVExFLnJlY29yZEZhaWx1cmUocGVlcl9pcCkKICAgICAgICAg"
    "ICAgICAgICAgICBpZiBjb3VudCA9PSBfTE9HSU5fRkFJTF9MSU1JVDoKICAgICAgICAgICAgICAg"
    "ICAgICAgICAgcHJpbnQoZidbTG9iYnldIFRocm90dGxpbmcgbG9naW5zIGZyb20ge3BlZXJfaXB9"
    "ICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZicoe2NvdW50fSBmYWlsdXJlcyBpbiB7"
    "X0xPR0lOX0ZBSUxfV0lORE9XfXMpJykKICAgICAgICAgICAgICAgICAgICBlcnJtc2dzID0gX1JF"
    "R0lTVEVSX0VSUk9SUyBpZiB2aWFSZWdpc3RlciBlbHNlIF9MT0dJTl9FUlJPUlMKICAgICAgICAg"
    "ICAgICAgICAgICBzZWxmLnNlbmRSYXcoX2luaXRfZXJyb3IoZXJybXNncy5nZXQobG9naW5FcnJv"
    "ciwgJ0xvZ2luIGZhaWxlZCcpKSkKICAgIGRlZiBmaW5pc2goc2VsZik6CiAgICAgICAgc2VsZi5z"
    "ZXJ2ZXIudW5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZikKICAgICAgICAjU3RvcCB0aGUgd3JpdGVy"
    "IGZpcnN0OiBpdCBob2xkcyB0aGlzIHNvY2tldCBhbmQgd291bGQgb3RoZXJ3aXNlIGtlZXAKICAg"
    "ICAgICAjd3JpdGluZyBvbiBiZWhhbGYgb2YgYSBwbGF5ZXIgd2hvIGhhcyBhbHJlYWR5IGxlZnQg"
    "ZXZlcnkgY2hhbm5lbC4KICAgICAgICBzZWxmLl9zdG9wV3JpdGVyKCkKICAgICAgICBpZiBzZWxm"
    "LnVzZXI6CiAgICAgICAgICAgIHByaW50KGYnVXNlcjoge3NlbGYudXNlci5uYW1lfSBEaXNjb25u"
    "ZWN0ZWQnKQogICAgICAgICAgICBzZWxmLnVzZXIuZGlzY29ubmVjdChzZWxmLnNlcnZlcikKICAg"
    "ICAgICAjY2xlYW51cCB1c2VyIGRhdGEKICAgICAgICAjVE9ETyBjaGVjayBpZiB0cmlnZ2VyZWQg"
    "b24gY3Jhc2hlZCBjb25uZWN0aW9uCiAgICBkZWYgZGVidWdfZGljdChzZWxmKToKICAgICAgICBy"
    "ZXR1cm4gewogICAgICAgICAgICAjVE9ETyBJUCBmb3IgZWxldmF0ZWQgYXV0aG9yaXR5CiAgICAg"
    "ICAgICAgICMnbmFtZSc6c2VsZi51c2VyLm5hbWUsCiAgICAgICAgICAgICdnYW1lJzpzZWxmLnVz"
    "ZXIuZ2FtZS5nbmFtZSBpZiBzZWxmLnVzZXIuZ2FtZSBlbHNlICcnLAogICAgICAgICAgICAndG93"
    "bic6c2VsZi51c2VyLmdhbWVjaGFubmVsLm5hbWUgaWYgc2VsZi51c2VyLmdhbWVjaGFubmVsIGVs"
    "c2UgJycsCiAgICAgICAgICAgICdwb3MnOnNlbGYudXNlci5wb3NkYXRhIGlmIHNlbGYudXNlci5w"
    "b3NkYXRhIGVsc2UgJycsCiAgICAgICAgICAgICdpZCc6c2VsZi51c2VyLmlkbnVtLAogICAgICAg"
    "ICAgICAnbG9naW5UaW1lJzpqc29uVGltZShzZWxmLnVzZXIubG9naW5UaW1lKQogICAgICAgIH0j"
    "VE9ETyBlbGV2YXRlZCBhdXRob3JpdHkgdmVyc2lvbgoKZGVmIGNtZF9kZWZhdWx0KCk6I2FyZ3Mp"
    "OgogICAgI3ByaW50KGFyZ3MpCiAgICAjX3JlYWRjb25maWcoKQogICAgc2VydmVyID0gQ29yZVNl"
    "cnZlcigpCiAgICB3aXRoIHNlcnZlcjoKICAgICAgICB0c3QgPSBzaWduYWwuc2lnbmFsKHNpZ25h"
    "bC5TSUdJTlQsIHNlcnZlci5oYW5kbGVfc2lnbmFsKHRpbWVvdXQ9MikpCiAgICAgICAgI3ByaW50"
    "KCdBc3NpZ25lZCBTaWduYWw/JywgdHN0KQogICAgICAgICNzaWduYWwuc2lnbmFsKHNpZ25hbC5T"
    "SUdURVJNLCBzZXJ2ZXIuaGFuZGxlX3NpZ25hbCh0aW1lb3V0PTEpKQogICAgICAgIHNlcnZlci5z"
    "ZXJ2ZV9mb3JldmVyKCkKCiNzY3JpcHQgbGF1bmNoZWQsIGNoZWNrIGFyZ3VtZW50cyBhbmQgY29u"
    "ZmlnLiBzZXR1cCB2YXJpb3VzIG9iamVjdHMKaWYgX19uYW1lX18gPT0gJ19fbWFpbl9fJzoKICAg"
    "IHByaW50KCdJbml0aWFsaXppbmcgU2VydmVyJykKICAgIGNtZF9kZWZhdWx0KCkK"
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
