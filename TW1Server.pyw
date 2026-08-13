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
    'tab.mods': {'ru': '🧩  Моды', 'en': '🧩  Mods'},
    'tab.activation': {'ru': '🔑  Активация', 'en': '🔑  Activation'},
    'tab.log': {'ru': '📜  Лог', 'en': '📜  Log'},

    'mods.header': {'ru': 'Моды игры (.wd)', 'en': 'Game mods (.wd)'},
    'mods.folder': {'ru': 'Папка модов:', 'en': 'Mods folder:'},
    'mods.col_name': {'ru': 'Файл мода', 'en': 'Mod file'},
    'mods.col_state': {'ru': 'Состояние', 'en': 'State'},
    'mods.col_size': {'ru': 'Размер', 'en': 'Size'},
    'mods.on': {'ru': '● включён', 'en': '● enabled'},
    'mods.off': {'ru': '○ выключен', 'en': '○ disabled'},
    'mods.toggle': {'ru': 'Включить / выключить', 'en': 'Enable / disable'},
    'mods.refresh': {'ru': '🔄 Обновить список', 'en': '🔄 Refresh list'},
    'mods.open_folder': {'ru': '📂 Открыть папку модов', 'en': '📂 Open mods folder'},
    'mods.no_game': {'ru': 'Сначала укажи папку игры на вкладке «Игра».',
                      'en': 'Set the game folder on the "Game" tab first.'},
    'mods.empty': {'ru': 'В папке Mods нет ни одного .wd — положи туда файл мода и нажми «Обновить список».',
                    'en': 'No .wd files in the Mods folder - drop a mod there and click "Refresh list".'},
    'mods.hint': {
        'ru': 'Двойной клик по строке (или кнопка ниже) включает и выключает мод. Состояние хранится\n'
              'в реестре: HKCU\\Software\\Reality Pump\\TwoWorlds\\Mods, 1 — включён, 0 — выключен.\n'
              'После смены модов начинай НОВУЮ игру: старые сохранения тянут карты из самого сейва.\n'
              'В кооперативе набор включённых модов должен совпадать у всех игроков, иначе рассинхрон.',
        'en': 'Double-click a row (or use the button below) to toggle a mod. The state lives in the\n'
              'registry: HKCU\\Software\\Reality Pump\\TwoWorlds\\Mods, 1 = on, 0 = off.\n'
              'Start a NEW game after changing mods - old saves carry their own copy of the maps.\n'
              'In co-op every player must have the same mods enabled, or the session desyncs.'},
    'mods.registry_error': {'ru': 'Не удалось записать состояние мода в реестр:\n{err}',
                             'en': 'Could not write the mod state to the registry:\n{err}'},

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
    'settings.admins': {
        'ru': 'Админы в игровом чате (ники через запятую, пусто — выключено):',
        'en': 'In-game chat admins (comma-separated names, blank = disabled):'},
    'settings.admins_hint': {
        'ru': 'Команды пишутся в игровой чат: !help, !who, !kick, !say, !hz, !idle, !keepalive, !save',
        'en': 'Typed into the game chat: !help, !who, !kick, !say, !hz, !idle, !keepalive, !save'},
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
    'game.browse_title': {'ru': 'Выбери {exe}', 'en': 'Select {exe}'},
    'game.filetype_exe': {'ru': 'Исполняемый файл', 'en': 'Executable'},
    'game.filetype_all': {'ru': 'Все файлы', 'en': 'All files'},
    'game.registry_error_title': {'ru': 'Ошибка реестра', 'en': 'Registry error'},
    'game.not_found_title': {'ru': 'Не найдено', 'en': 'Not found'},
    'game.not_found_body': {'ru': '{exe} не найден рядом с указанным путём. Укажи папку игры (или сам этот файл) и нажми снова.',
                             'en': '{exe} not found next to the given path. Point to the game folder (or the file itself) and try again.'},
    'game.launch_failed_title': {'ru': 'Не удалось запустить игру', 'en': 'Could not launch the game'},

    'network.reach_header': {'ru': 'Доступность сервера из интернета', 'en': 'Reachability from the internet'},
    'network.local_ip': {'ru': 'Локальный IP:', 'en': 'Local IP:'},
    'network.public_ip': {'ru': 'Публичный IP:', 'en': 'Public IP:'},
    'network.determining': {'ru': '(определяется...)', 'en': '(determining...)'},
    'network.server_port': {'ru': 'Порт лобби (TCP):', 'en': 'Lobby port (TCP):'},
    'network.game_port': {'ru': 'Порт игры (TCP+UDP):', 'en': 'Game port (TCP+UDP):'},
    'network.game_port_hint': {
        'ru': 'Сама кооп-сессия идёт напрямую между игроками, минуя этот сервер, — по порту игры.\n'
              'Если он закрыт на роутере хозяина комнаты, лобби работает, а игра не соединяется.',
        'en': 'The co-op session runs directly between the players, not through this server - over the\n'
              'game port. If it is closed on the room host\'s router, the lobby works but the game does not connect.'},
    'network.refresh': {'ru': '🔄 Обновить', 'en': '🔄 Refresh'},
    'network.check_port': {'ru': '🌐 Проверить порт вручную (браузер)', 'en': '🌐 Check port manually (browser)'},
    'network.try_upnp': {'ru': '⚡ Пробросить оба порта автоматически (UPnP)',
                          'en': '⚡ Forward both ports automatically (UPnP)'},
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
    'network.upnp_line_ok': {'ru': '✔ {port}/{proto} — проброшен на {ip}',
                              'en': '✔ {port}/{proto} - forwarded to {ip}'},
    'network.upnp_line_fail': {'ru': '✘ {port}/{proto} — не удалось: {err}',
                                'en': '✘ {port}/{proto} - failed: {err}'},
    'network.upnp_all_ok': {
        'ru': 'Готово, роутер подтвердил все пробросы. Проверь кнопкой выше, чтобы убедиться снаружи.',
        'en': 'Done, the router confirmed every mapping. Check with the button above to be sure from outside.'},
    'network.upnp_some_failed': {
        'ru': 'Часть пробросов не прошла. Это нормально для многих роутеров (UPnP часто выключен) — '
              'непрошедшие строки придётся завести вручную в настройках роутера на локальный IP {ip}.',
        'en': 'Some mappings failed. That is normal for many routers (UPnP is often off) - '
              'add the failed lines by hand in the router settings, pointing at local IP {ip}.'},
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
# Mods are toggled through HKCU\...\TwoWorlds\Mods: one REG_DWORD per .wd file
# name, 1 = loaded, 0 = ignored. Confirmed against Buglord's Mod Selector,
# whose whole job is writing exactly these values.
#
# The ForceCameraAspectX/Y ("widescreen FOV") controls that used to live here
# are gone: those two registry values exist, but no observable effect on a
# running game was ever confirmed, so the panel was offering a knob that
# plausibly did nothing.
# ---------------------------------------------------------------------------

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


def detect_game_dir():
    """Best-effort path to the Two Worlds install folder, or ''. Checked in
    the order they actually occur: Steam's own library list first (the library
    can sit on any drive, so the hardcoded guesses below are not enough), then
    the usual GOG/standalone locations."""
    candidates = []
    steam_root = ''
    if winreg is not None:
        for hive, path in ((winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam'),
                           (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam')):
            try:
                with winreg.OpenKey(hive, path) as key:
                    steam_root = winreg.QueryValueEx(key, 'SteamPath' if hive == winreg.HKEY_CURRENT_USER
                                                     else 'InstallPath')[0]
                    break
            except OSError:
                continue
    libraries = []
    if steam_root:
        libraries.append(os.path.join(steam_root, 'steamapps', 'common'))
        vdf = os.path.join(steam_root, 'steamapps', 'libraryfolders.vdf')
        try:
            with open(vdf, encoding='utf-8', errors='ignore') as f:
                #The file is Valve's own key/value format; the only field
                #needed here is each library's "path", so a regex beats
                #pulling in a parser for it.
                for m in re.finditer(r'"path"\s*"([^"]+)"', f.read()):
                    libraries.append(os.path.join(m.group(1).replace('\\\\', '\\'),
                                                  'steamapps', 'common'))
        except OSError:
            pass
    for lib in libraries:
        candidates.append(os.path.join(lib, 'Two Worlds - Epic Edition'))
        candidates.append(os.path.join(lib, 'Two Worlds'))
    for drive in ('C:', 'D:', 'E:'):
        for base in ('Games', r'Program Files (x86)', 'Program Files',
                     r'Program Files (x86)\GOG Galaxy\Games', r'GOG Games'):
            candidates.append(os.path.join(drive + os.sep, base, 'Two Worlds - Epic Edition'))
            candidates.append(os.path.join(drive + os.sep, base, 'Two Worlds'))
    for folder in candidates:
        for exe in GAME_EXE_CANDIDATES:
            if os.path.isfile(os.path.join(folder, exe)):
                #normpath: Steam stores its own path with forward slashes, and
                #a half-and-half path looks like a bug when shown on screen
                return os.path.normpath(folder)
    return ''


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


#The co-op session itself does NOT go through this server. The lobby only
#introduces the players and hands the joiner an x-directplay address; the game
#then opens a direct connection to the host on this port. So the port that
#decides whether co-op works well is this one, on the host's router - and it is
#not the lobby port. The game keeps it here; 17771 is its default.
DEFAULT_GAME_PORT = 17771


def read_game_port():
    if winreg is None:
        return DEFAULT_GAME_PORT
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TW_NETWORK_KEY) as key:
            port = winreg.QueryValueEx(key, 'EarthNet_GamePort')[0]
    except OSError:
        return DEFAULT_GAME_PORT
    return int(port) if 0 < int(port) <= 65535 else DEFAULT_GAME_PORT


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
        #Only the player table: the town and game dictionaries this used to
        #build alongside it were walked twice a second and then thrown away,
        #because no widget has shown them since the panel was reorganised.
        if not self._running or self.server is None:
            return None
        try:
            return {'players': self.server.debug_dict_players()}
        except Exception:
            return None  # transient race while the server is mutating state


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
        if hasattr(self, 'mods_tree'):
            #The enabled/disabled cells are translated text, not widgets, so
            #they are outside the registry _tr() maintains.
            self._refresh_mods()

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
        self.tab_mods = self._make_scrollable_tab(nb, 'tab.mods')
        self.tab_network = self._make_scrollable_tab(nb, 'tab.network')
        self.tab_activation = self._make_scrollable_tab(nb, 'tab.activation')
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_log, text=T('tab.log'))
        self._tr('tab.log', lambda t: nb.tab(self.tab_log, text=t))

        self._build_server_tab()
        self._build_settings_tab()
        self._build_game_tab()
        self._build_mods_tab()
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

        #Row 13, not 12: this checkbox used to share its grid cell with the
        #PublicHostAddress entry above, so the two widgets were drawn on top of
        #each other - the checkbox covered the left half of the address field,
        #which could then neither be read nor reliably clicked into.
        self.set_hero_hex = tk.BooleanVar()
        cb5 = ttk.Checkbutton(f, variable=self.set_hero_hex)
        cb5.grid(row=13, column=1, sticky='w', padx=10)
        self._tr('settings.hero_id_hex', lambda t: cb5.configure(text=t))

        adm_lbl = ttk.Label(f)
        adm_lbl.grid(row=14, column=0, sticky='w', **pad)
        self._tr('settings.admins', lambda t: adm_lbl.configure(text=t))
        self.set_admins = tk.StringVar()
        ttk.Entry(f, textvariable=self.set_admins, width=40).grid(
            row=14, column=1, sticky='w', padx=10, pady=6)
        adm_hint = ttk.Label(f, foreground=self.MUTED)
        adm_hint.grid(row=15, column=1, sticky='w', padx=10)
        self._tr('settings.admins_hint', lambda t: adm_hint.configure(text=t))

        self.set_debug_cmds = tk.BooleanVar()
        cb6 = ttk.Checkbutton(f, variable=self.set_debug_cmds)
        cb6.grid(row=16, column=1, sticky='w', padx=10)
        self._tr('settings.debug_cmds', lambda t: cb6.configure(text=t))

        btns = ttk.Frame(f)
        btns.grid(row=17, column=1, sticky='w', padx=10, pady=16)
        load_btn = ttk.Button(btns, command=self._load_settings)
        load_btn.pack(side='left')
        self._tr('settings.load_current', lambda t: load_btn.configure(text=t))
        save_btn = ttk.Button(btns, style='Accent.TButton', command=self._save_settings)
        save_btn.pack(side='left', padx=8)
        self._tr('settings.save', lambda t: save_btn.configure(text=t))

        note = ttk.Label(f, foreground=self.MUTED, wraplength=560, justify='left')
        note.grid(row=18, column=1, sticky='w', padx=10)
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
        self.set_admins.set(sec.get('Admins', fallback=''))

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
        sec['Admins'] = self.set_admins.get().strip()
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

        self._load_game_tab_state()

    def _load_game_tab_state(self):
        cfg = self._load_game_settings_file()
        path = cfg['game'].get('ExePath', '')
        if not path:
            #First run: look where the game normally installs instead of
            #handing the user an empty box to fill in by hand.
            found = detect_game_dir()
            if found:
                path = found
                cfg['game']['ExePath'] = path
                self._save_game_settings_file(cfg)
                print(f'[Игра] Папка игры найдена автоматически: {path}')
        self.game_exe_var.set(path)
        self.game_single_core_var.set(cfg['game'].getboolean('SingleCoreAffinity', fallback=False))

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
            #The Mods tab reads its folder from this same path, so it would
            #otherwise keep showing the old install (or nothing) until restart.
            if hasattr(self, 'mods_tree'):
                self._refresh_mods()

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
    # Mods tab - list the .wd archives in the game's Mods folder and toggle
    # each one through the registry key the game reads on startup. This is
    # exactly what Buglord's Mod Selector does; having it here means a mod can
    # be switched on and the game launched from the same window, and it is the
    # half of the mod workflow that has nothing to do with building the mod.
    # ------------------------------------------------------------------
    def _mods_dir(self):
        """<game folder>/Mods, or '' if the game folder isn't known yet. The
        capital M matters - the game does not find a lowercase 'mods'."""
        path = ''
        if hasattr(self, 'game_exe_var'):
            path = self.game_exe_var.get().strip()
        if not path:
            path = self._load_game_settings_file()['game'].get('ExePath', '').strip()
        if not path:
            path = detect_game_dir()
        if not path:
            return ''
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        return os.path.normpath(os.path.join(folder, 'Mods'))

    def _build_mods_tab(self):
        f = self.tab_mods
        pad = {'padx': 10, 'pady': 6}

        hdr = ttk.Label(f, style='Header.TLabel')
        hdr.grid(row=0, column=0, columnspan=3, sticky='w', **pad)
        self._tr('mods.header', lambda t: hdr.configure(text=t))

        dir_lbl = ttk.Label(f)
        dir_lbl.grid(row=1, column=0, sticky='w', padx=10)
        self._tr('mods.folder', lambda t: dir_lbl.configure(text=t))
        self.mods_dir_label = ttk.Label(f, foreground=self.MUTED)
        self.mods_dir_label.grid(row=1, column=1, columnspan=2, sticky='w', padx=10)

        hint = ttk.Label(f, foreground=self.MUTED, justify='left')
        hint.grid(row=2, column=0, columnspan=3, sticky='w', padx=10, pady=(6, 0))
        self._tr('mods.hint', lambda t: hint.configure(text=t))

        self.mods_tree = ttk.Treeview(f, columns=('name', 'state', 'size'),
                                       show='headings', height=8)
        self._tr('mods.col_name', lambda t: self.mods_tree.heading('name', text=t))
        self._tr('mods.col_state', lambda t: self.mods_tree.heading('state', text=t))
        self._tr('mods.col_size', lambda t: self.mods_tree.heading('size', text=t))
        self.mods_tree.column('name', width=300, anchor='w')
        self.mods_tree.column('state', width=120, anchor='w')
        self.mods_tree.column('size', width=100, anchor='e')
        self.mods_tree.grid(row=3, column=0, columnspan=3, sticky='w', padx=10, pady=10)
        self.mods_tree.bind('<Double-1>', lambda e: self._toggle_selected_mod())

        btns = ttk.Frame(f)
        btns.grid(row=4, column=0, columnspan=3, sticky='w', padx=10, pady=(0, 6))
        toggle_btn = ttk.Button(btns, style='Accent.TButton', command=self._toggle_selected_mod)
        toggle_btn.pack(side='left')
        self._tr('mods.toggle', lambda t: toggle_btn.configure(text=t))
        refresh_btn = ttk.Button(btns, command=self._refresh_mods)
        refresh_btn.pack(side='left', padx=8)
        self._tr('mods.refresh', lambda t: refresh_btn.configure(text=t))
        open_btn = ttk.Button(btns, command=self._open_mods_folder)
        open_btn.pack(side='left')
        self._tr('mods.open_folder', lambda t: open_btn.configure(text=t))

        self.mods_status_label = ttk.Label(f, foreground=self.MUTED, wraplength=820,
                                            justify='left')
        self.mods_status_label.grid(row=5, column=0, columnspan=3, sticky='w', padx=10)

        self._refresh_mods()

    def _refresh_mods(self):
        mods_dir = self._mods_dir()
        self.mods_dir_label.configure(text=mods_dir or '—')
        for row in self.mods_tree.get_children():
            self.mods_tree.delete(row)
        if not mods_dir:
            self.mods_status_label.configure(text=T('mods.no_game'))
            return
        try:
            files = sorted(fn for fn in os.listdir(mods_dir) if fn.lower().endswith('.wd'))
        except OSError:
            files = []
        states = read_mod_states()
        for fn in files:
            try:
                size = os.path.getsize(os.path.join(mods_dir, fn))
            except OSError:
                size = 0
            #Registry value names are matched case-insensitively against the
            #file name, because the name written by another tool (or by hand)
            #need not match the file's own spelling.
            on = 0
            for key, val in states.items():
                if key.lower() == fn.lower():
                    on = val
                    break
            self.mods_tree.insert('', 'end', iid=fn, values=(
                fn, T('mods.on') if on else T('mods.off'), f'{size / (1 << 20):.1f} MB'))
        self.mods_status_label.configure(text='' if files else T('mods.empty'))

    def _toggle_selected_mod(self):
        sel = self.mods_tree.selection()
        if not sel:
            return
        name = sel[0]
        states = read_mod_states()
        on = 0
        for key, val in states.items():
            if key.lower() == name.lower():
                on = val
                break
        try:
            set_mod_state(name, not on)
        except Exception as e:
            messagebox.showerror(T('game.registry_error_title'), T('mods.registry_error', err=e))
            return
        print(f'[Моды] {name}: {"включён" if not on else "выключен"}')
        self._refresh_mods()
        self.mods_tree.selection_set(name)

    def _open_mods_folder(self):
        mods_dir = self._mods_dir()
        if not mods_dir:
            messagebox.showinfo(T('tab.mods'), T('mods.no_game'))
            return
        try:
            #Created rather than reported missing: a fresh install has no Mods
            #folder at all, and "open it" is the moment the user wants one.
            os.makedirs(mods_dir, exist_ok=True)
            os.startfile(mods_dir)  #noqa - Windows only, same as the rest
        except Exception as e:
            messagebox.showinfo(T('tab.mods'), f'{mods_dir}\n\n{e}')

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

        gport_lbl = ttk.Label(f)
        gport_lbl.grid(row=5, column=0, sticky='w', **pad)
        self._tr('network.game_port', lambda t: gport_lbl.configure(text=t))
        self.net_game_port_label = ttk.Label(f, text=str(DEFAULT_GAME_PORT))
        self.net_game_port_label.grid(row=5, column=1, sticky='w', **pad)
        gport_hint = ttk.Label(f, foreground=self.MUTED, justify='left')
        gport_hint.grid(row=6, column=0, columnspan=2, sticky='w', padx=10)
        self._tr('network.game_port_hint', lambda t: gport_hint.configure(text=t))

        btns = ttk.Frame(f)
        btns.grid(row=7, column=0, columnspan=2, sticky='w', padx=10, pady=14)
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
        self.net_status_label.grid(row=8, column=0, columnspan=2, sticky='w', padx=10, pady=(6, 0))

        ttk.Separator(f, orient='horizontal').grid(row=9, column=0, columnspan=2, sticky='ew', pady=10)

        srvhdr = ttk.Label(f, style='Header.TLabel')
        srvhdr.grid(row=10, column=0, columnspan=2, sticky='w', **pad)
        self._tr('network.server_list_header', lambda t: srvhdr.configure(text=t))
        ttk.Label(f, text='ВАЖНО: названия строк ("WarNet Europe" и т.п.) зашиты в саму игру - её меню всегда\n'
                           'покажет ровно эти же пункты, что бы тут ни было. Из реестра берётся только АДРЕС\n'
                           'для каждого из них. Поэтому переименовывать/добавлять новые пункты бессмысленно -\n'
                           'редактируется только колонка "Адрес" (двойной клик по ячейке).',
                  foreground=self.MUTED, justify='left').grid(row=11, column=0, columnspan=2, sticky='w', padx=10)

        self.servers_tree = ttk.Treeview(f, columns=('name', 'addr'), show='headings', height=6)
        self._tr('network.col_name', lambda t: self.servers_tree.heading('name', text=t))
        self._tr('network.col_addr', lambda t: self.servers_tree.heading('addr', text=t))
        self.servers_tree.column('name', width=200, anchor='w')
        self.servers_tree.column('addr', width=280, anchor='w')
        self.servers_tree.grid(row=12, column=0, columnspan=2, sticky='w', padx=10, pady=6)
        self.servers_tree.bind('<Double-1>', self._edit_server_address)

        fillrow = ttk.Frame(f)
        fillrow.grid(row=13, column=0, columnspan=2, sticky='w', padx=10, pady=(8, 4))
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
        srvbtns.grid(row=14, column=0, columnspan=2, sticky='w', padx=10, pady=(4, 4))
        save_srv_btn = ttk.Button(srvbtns, style='Accent.TButton', command=self._save_server_list)
        save_srv_btn.pack(side='left')
        self._tr('network.save_to_game', lambda t: save_srv_btn.configure(text=t))

        ttk.Label(f, text='"localhost"/"127.0.0.1" - для игры вдвоём с одного компьютера (второй клиент игры\n'
                           'на этой же машине). Для игры по локальной сети используй "Локальный IP".',
                  foreground=self.MUTED, justify='left').grid(row=15, column=0, columnspan=2, sticky='w', padx=10)

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
        self.net_game_port_label.configure(text=str(read_game_port()))
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
        #Both ports, in the order they matter: the lobby one decides whether
        #anyone can log in, the game one whether the session that follows can
        #connect. The site only tests TCP, which is enough to tell an open
        #forward from a closed one.
        port = f'{self.net_port_label.cget("text")} / {self.net_game_port_label.cget("text")}'
        self.net_status_label.configure(text=T('network.opened_port_checker', port=port))

    def _try_upnp(self):
        try:
            lobby = int(self.net_port_label.cget('text'))
            game = int(self.net_game_port_label.cget('text'))
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('network.bad_port'))
            return
        #The lobby speaks TCP. The co-op session is DirectPlay, which uses UDP
        #for gameplay and TCP to establish the session, so the game port is
        #forwarded for both - and it is the one that decides whether the
        #players can actually reach each other, since that traffic never
        #touches this server.
        wanted = [(lobby, 'TCP', 'TW1 Lobby Server'),
                  (game, 'TCP', 'TW1 Game (DirectPlay)'),
                  (game, 'UDP', 'TW1 Game (DirectPlay)')]
        self.net_status_label.configure(text=T('network.upnp_searching'))
        self.update_idletasks()
        threading.Thread(target=self._upnp_worker, args=(wanted,), daemon=True).start()

    def _upnp_worker(self, wanted):
        lines = []
        failed = False
        local_ip = get_local_ip()
        for (port, proto, description) in wanted:
            try:
                local_ip = upnp_add_port_mapping(port, description=description, protocol=proto)
                lines.append(T('network.upnp_line_ok', port=port, proto=proto, ip=local_ip))
                print(f'[Сеть] UPnP: {port}/{proto} проброшен на {local_ip}.')
            except UPnPError as e:
                lines.append(T('network.upnp_line_fail', port=port, proto=proto, err=e))
                print(f'[Сеть] UPnP {port}/{proto} не удался: {e}')
                failed = True
            except Exception as e:
                lines.append(T('network.upnp_unexpected', err=e))
                failed = True
        lines.append('')
        lines.append(T('network.upnp_some_failed', ip=local_ip) if failed
                     else T('network.upnp_all_ok'))
        msg = '\n'.join(lines)
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
        #Land the tail of the log before the process goes away: writes are
        #flushed on a half-second timer, so the last lines of a session - which
        #include the shutdown itself - were the ones most likely to be lost.
        if self._log_fh is not None:
            try:
                sys.stdout.flush()
                self._log_fh.close()
            except Exception:
                pass
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
    "RVJWRVIgQ09ERQoKUkVDVl9CVUZfTEVOID0gMioqMTIKCl9WRVJTSU9OID0gJzAuMy4wJwpwcmlu"
    "dChmJ1NlcnZlciB2ZXJpc2lvbiB7X1ZFUlNJT059JykKX0RFQlVHX0FMTE9XX0FOWV9MT0dJTiA9"
    "IEZhbHNlICNkb2VzIG5vdCB2ZXJpZnkgbG9naW5zLCBmb3IgZGVidWcgcmVhc29ucwpfVFdfTE9C"
    "QllfUE9SVCA9IDE3MTcxCl9BVVRPX1JFR0lTVEVSID0gVHJ1ZQojVXBwZXIgYm91bmQgZm9yIGEg"
    "c2luZ2xlIGxlbmd0aC1wcmVmaXhlZCBibG9iIGZyb20gYSBjbGllbnQgKHBsYXllcmRhdGEsCiNo"
    "ZXJvZGF0YSwgZ2FtZS1jb21tYW5kIHBheWxvYWQpLiBHZW5lcm91cyBjb21wYXJlZCB0byBhIHJl"
    "YWwgc2F2ZSwgYnV0IGZpbml0ZToKI3dpdGhvdXQgaXQgYSBjbGllbnQgY291bGQgYW5ub3VuY2Ug"
    "YW4gYXJiaXRyYXJ5IGxlbmd0aCBhbmQgbWFrZSB0aGUgc2VydmVyCiNidWZmZXIgdW50aWwgaXQg"
    "cmFuIG91dCBvZiBtZW1vcnkuCl9NQVhfQkxPQiA9IDE2ICogMTAyNCAqIDEwMjQKI1RpZ2h0ZXIg"
    "Y2VpbGluZyBmb3IgdGhlIG9uZSBibG9iIHRoYXQgaXMgcmUtc2VudCB0byBldmVyeSBvdGhlciBw"
    "bGF5ZXIgaW4gdGhlCiN0b3duIHJhdGhlciB0aGFuIGp1c3Qgc3RvcmVkIC0gc2VlIF9zZXR1c2Vy"
    "aGVyb2RhdGEuCl9NQVhfSEVST0RBVEEgPSAxMDI0ICogMTAyNAojSGFuZHNoYWtlL2xvZ2luIHBh"
    "Y2tldHMgYXJlIGEgZmV3IGh1bmRyZWQgYnl0ZXMgaW4gcHJhY3RpY2UuIFRoZXNlIGJvdW5kcwoj"
    "YXBwbHkgKmJlZm9yZSogYXV0aGVudGljYXRpb24sIHdoZXJlIGFueW9uZSB3aG8gY2FuIHJlYWNo"
    "IHRoZSBwb3J0IGNhbiBzZW5kCiN3aGF0ZXZlciB0aGV5IGxpa2UsIHNvIHRoZXkgYXJlIGRlbGli"
    "ZXJhdGVseSB0aWdodC4KX01BWF9IQU5EU0hBS0UgPSA2NCAqIDEwMjQKX01BWF9IQU5EU0hBS0Vf"
    "SU5GTEFURUQgPSAxMDI0ICogMTAyNAoKIy0tLSBzeW5jaHJvbmlzYXRpb24gdHVuaW5nIC0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KI0hvdyBvZnRlbiB0"
    "aGUgYWNjdW11bGF0ZWQgaGVybyBwb3NpdGlvbnMgaW4gYSB0b3duIGFyZSBwdXNoZWQgdG8gZXZl"
    "cnlvbmUgaW4KI2l0LiBUaGlzIHVzZWQgdG8gYmUgcGlubmVkIHRvIHRoZSAxcyBzb2NrZXRzZXJ2"
    "ZXIgcG9sbCBpbnRlcnZhbCwgd2hpY2ggaXMgd2hhdAojbWFkZSBvdGhlciBwbGF5ZXJzJyBtYXAg"
    "bWFya2VycyBqdW1wIGEgZnVsbCBzZWNvbmQgYXQgYSB0aW1lLiBFYWNoIHRpY2sgc2VuZHMKI29u"
    "ZSBwYWNrZXQgcGVyIHRvd24gYW5kIG9ubHkgaWYgc29tZWJvZHkgYWN0dWFsbHkgbW92ZWQsIHNv"
    "IGV2ZW4gYXQgdGhpcyByYXRlCiNpdCdzIGEgaGFuZGZ1bCBvZiBzbWFsbCBwYWNrZXRzL3NlYyBm"
    "b3IgYSBjby1vcC1zaXplZCBncm91cCAtIG5lZ2xpZ2libGUKI2JhbmR3aWR0aCBlaXRoZXIgb24g"
    "TEFOIG9yIG92ZXIgYSBob21lIGludGVybmV0IGNvbm5lY3Rpb24gLSB3aGlsZSBnZXR0aW5nCiNu"
    "b3RpY2VhYmx5IGNsb3NlciB0byBzbW9vdGggbW90aW9uIHRoYW4gdGhlIG9sZCAxSHogYmFzZWxp"
    "bmUuCl9QT1NfVVBEQVRFX0haID0gMTAuMApfUE9TX1VQREFURV9IWl9NQVggPSAyMC4wCiNEcm9w"
    "IGEgY29ubmVjdGlvbiB0aGF0IGhhcyBub3Qgc2VudCBhIHNpbmdsZSBieXRlIGluIHRoaXMgbG9u"
    "Zy4gQSBwbGF5ZXIgd2hvc2UKI2xpbmsgZGllcyB3aXRob3V0IGEgY2xlYW4gVENQIGNsb3NlIG90"
    "aGVyd2lzZSBrZWVwcyB0aGVpciB1c2VybmFtZSBjbGFpbWVkCiNmb3JldmVyLCBhbmQgdGhlaXIg"
    "bmV4dCBsb2dpbiBhdHRlbXB0IGlzIHJlamVjdGVkIHdpdGggJ0FjY291bnQgYWxyZWFkeSBsb2dn"
    "ZWQKI2luJyB1bnRpbCB0aGUgc2VydmVyIGlzIHJlc3RhcnRlZC4gMCBkaXNhYmxlcy4KX0lETEVf"
    "VElNRU9VVCA9IDMwMAojQmxvY2tpbmcgcmVjdigpIHRpbWVvdXQgaW4gdGhlIHJlYWQgbG9vcC4g"
    "T25seSBnb3Zlcm5zIGhvdyBxdWlja2x5IGEgdGhyZWFkCiNub3RpY2VzIHNlcnZlciBzaHV0ZG93"
    "biBhbmQgdGhlIGlkbGUgZGVhZGxpbmU7IG91dGJvdW5kIGxhdGVuY3kgbm8gbG9uZ2VyCiNkZXBl"
    "bmRzIG9uIGl0IG5vdyB0aGF0IGVhY2ggY29ubmVjdGlvbiBoYXMgaXRzIG93biB3cml0ZXIgdGhy"
    "ZWFkLgpfUkVBRF9USU1FT1VUID0gMS4wCiNIb3cgbG9uZyBhIGNsaWVudCBnZXRzIHRvIGZpbmlz"
    "aCBkZWxpdmVyaW5nIGEgYmxvYiBpdCBoYXMgYWxyZWFkeSBhbm5vdW5jZWQKI3RoZSBsZW5ndGgg"
    "b2YuIEdlbmVyb3VzIGZvciBhIGxhcmdlIHNhdmUgb3ZlciBhIHNsb3cgbGluaywgYnV0IGZpbml0"
    "ZSAtIHNlZQojX1JlYWRCbG9iLgpfQkxPQl9USU1FT1VUID0gNjAuMAojVGhlIGxvYmJ5IG9ubHkg"
    "YnJva2VycyB0aGUgY28tb3Agc2Vzc2lvbjsgdGhlIHNlc3Npb24gaXRzZWxmIGlzIGEgZGlyZWN0"
    "CiNEaXJlY3RQbGF5IGNvbm5lY3Rpb24gZnJvbSB0aGUgam9pbmluZyBwbGF5ZXIgdG8gdGhlIGhv"
    "c3QsIGF0IHRoZSBhZGRyZXNzIHRoZQojaG9zdCBwdXRzIGluIHRoZSB4LWRpcmVjdHBsYXkgVVJM"
    "IG9mIGl0cyAvY3JlYXRlZ2FtZS4gVGhlIGhvc3QncyBvd24gY2xpZW50CiNmaWxscyB0aGF0IGlu"
    "IGZyb20gaXRzIGxvY2FsIGFkYXB0ZXIsIHNvIGJlaGluZCBhIHJvdXRlciBpdCBhZHZlcnRpc2Vz"
    "CiNzb21ldGhpbmcgbGlrZSAxOTIuMTY4LjAuMTAgLSB1bnJlYWNoYWJsZSBmb3IgYW55b25lIG5v"
    "dCBvbiB0aGF0IExBTiwgYW5kIHRoZQojam9pbmVyIHNpdHMgb24gImNvbm5lY3RpbmciIHVudGls"
    "IGl0IGdpdmVzIHVwLiBFdmVyeXRoaW5nIHRoYXQgZ29lcyB0aHJvdWdoCiN0aGUgbG9iYnkgKHRv"
    "d24sIGNoYXQsIHNlZWluZyBlYWNoIG90aGVyIG1vdmUpIGtlZXBzIHdvcmtpbmcsIHdoaWNoIGlz"
    "IHdoYXQKI21ha2VzIHRoaXMgbG9vayBsaWtlIGEgcm9vbS1zcGVjaWZpYyBidWcuCiNUaGUgc2Vy"
    "dmVyIGFscmVhZHkga25vd3MgYW4gYWRkcmVzcyBmb3IgdGhlIGhvc3QgdGhhdCBldmVyeSBvdGhl"
    "ciBjbGllbnQgY2FuCiNyZWFjaDogdGhlIHNvdXJjZSBhZGRyZXNzIG9mIHRoZSBob3N0J3Mgb3du"
    "IGNvbm5lY3Rpb24gdG8gdXMuIFN1YnN0aXR1dGluZyBpdAojaXMgd2hhdCBtYWtlcyBjcm9zcy1p"
    "bnRlcm5ldCBjby1vcCB3b3JrIGF0IGFsbC4KI1R1cm4gb2ZmIChDb25maWcuaW5pOiBSZXdyaXRl"
    "R2FtZUhvc3QgPSBGYWxzZSkgaWYgZXZlcnkgcGxheWVyIGlzIG9uIHRoZSBzYW1lCiNMQU4gYXMg"
    "dGhlIGhvc3QgYnV0IHRoZSBsb2JieSBpcyBub3QgLSB0aGVuIHRoZSBob3N0J3Mgb3duIExBTiBh"
    "ZGRyZXNzIGlzIHRoZQojY29ycmVjdCBvbmUgYW5kIG91cnMgaXMgbm90LgpfUkVXUklURV9HQU1F"
    "X0hPU1QgPSBUcnVlCiNFeHBsaWNpdCBwdWJsaWMgYWRkcmVzcyBvZiB0aGUgbWFjaGluZSB0aGF0"
    "IGhvc3RzIHJvb21zLCBmb3IgdGhlIGNhc2UgdGhlCiNzZXJ2ZXIgY2Fubm90IHdvcmsgaXQgb3V0"
    "IChzZWUgX3B1YmxpY0FkZHJlc3MpLiBTZXQgaXQgaW4gQ29uZmlnLmluaSBhcwojUHVibGljSG9z"
    "dEFkZHJlc3MgaWYgYXV0by1kZXRlY3Rpb24gcGlja3MgdGhlIHdyb25nIG9uZS4KX1BVQkxJQ19I"
    "T1NUX0FERFJFU1MgPSAnJwojVGhlIGdhbWUgYXBwZW5kcyBhIHByb3ByaWV0YXJ5ICdhbHQ9JyBm"
    "aWVsZCB0byB0aGUgRGlyZWN0UGxheSBVUkwgaG9sZGluZwojZXZlcnkgYWRkcmVzcyBvZiBldmVy"
    "eSBhZGFwdGVyIHRoZSBob3N0IGhhczogb2JzZXJ2ZWQgaW4gdGhlIHdpbGQgaXQgY2FycmllZAoj"
    "YSBUZXJlZG8gMjAwMTowOjovMzIgYWRkcmVzcywgYW4gZmU4MDo6IGxpbmstbG9jYWwgb25lIGFu"
    "ZCB0aGUgaG9zdCdzIExBTgojSVB2NCAtIG5vbmUgb2YgdGhlbSByZWFjaGFibGUgZnJvbSBhbm90"
    "aGVyIG5ldHdvcmsuIEEgam9pbmVyIHRoYXQgd29ya3MKI3Rocm91Z2ggdGhhdCBjYW5kaWRhdGUg"
    "bGlzdCB3YWl0cyBvdXQgYSBjb25uZWN0aW9uIHRpbWVvdXQgb24gZWFjaCwgd2hpY2gKI2xvb2tz"
    "IGV4YWN0bHkgbGlrZSAiY29ubmVjdGluZyBmb3JldmVyIi4gRHJvcHBpbmcgdGhlIGZpZWxkIGxl"
    "YXZlcyB0aGUgc2luZ2xlCiNhZGRyZXNzIHRoaXMgc2VydmVyIGtub3dzIHRvIGJlIHJlYWNoYWJs"
    "ZS4KX1NUUklQX0FMVF9BRERSRVNTRVMgPSBUcnVlCiNMb2cgZXZlcnkgY29tbWFuZCByZWNlaXZl"
    "ZCBmcm9tIGNsaWVudHMsIHdpdGggaXRzIHJhdyB0ZXh0LiBWZXJib3NlLCBidXQgdGhpcwojcHJv"
    "dG9jb2wgaXMgb25seSBwYXJ0aWFsbHkgZG9jdW1lbnRlZCBhbmQgaXQgaXMgdGhlIG9ubHkgd2F5"
    "IHRvIHNlZSB3aGF0IHRoZQojY2xpZW50IGFjdHVhbGx5IGFza3MgZm9yIHdoZW4gYSBmZWF0dXJl"
    "IGRvZXMgbm90aGluZy4KX0RFQlVHX0xPR19DT01NQU5EUyA9IFRydWUKIy91cGRoZXJvcG9zIGFu"
    "ZCAvbm9wIGFycml2ZSB+MTAgdGltZXMgYSBzZWNvbmQgcGVyIHBsYXllciBhbmQgc2F5IG5vdGhp"
    "bmcKI3VzZWZ1bC4gTG9nZ2luZyB0aGVtIGNvc3QgdHdvIGZvcm1hdHRlZCBsaW5lcywgYSBxdWV1"
    "ZSBwdXQsIGEgR1VJIGluc2VydCBhbmQKI2EgZGlzayB3cml0ZSAqaW5zaWRlIHRoZSBjb21tYW5k"
    "IGhhbmRsZXIqLCBvbiB0aGUgb25lIHBhdGggdGhhdCBoYXMgdG8gc3RheQojcXVpY2sgLSBzZWxm"
    "LWluZmxpY3RlZCBsYXRlbmN5IGFuZCBqaXR0ZXIgb24gZXhhY3RseSB0aGUgdHJhZmZpYyBiZWlu"
    "ZwojZGVidWdnZWQsIHBsdXMgYSBsb2cgc28gbm9pc3kgdGhhdCB0aGUgaW50ZXJlc3RpbmcgbGlu"
    "ZXMgc2Nyb2xsIGF3YXkuIFNldAojRGVidWdDb21tYW5kc1ZlcmJvc2UgPSBUcnVlIGluIENvbmZp"
    "Zy5pbmkgdG8gc2VlIHRoZW0gYW55d2F5LgpfREVCVUdfTE9HX1ZFUkJPU0UgPSBGYWxzZQpfUVVJ"
    "RVRfQ09NTUFORFMgPSBmcm96ZW5zZXQoKCcvdXBkaGVyb3BvcycsICcvbm9wJykpCiNDb25zZXJ2"
    "YXRpdmUgY2FwIG9uIGEgc2luZ2xlIGdlbmVyYXRlZCBjb21tYW5kIGxpbmUuIE5vdGhpbmcgdGhl"
    "IHJldGFpbAojY2xpZW50IHNlbmRzIGNvbWVzIGNsb3NlIHRvIHRoaXMsIHNvIGl0IGlzIHdlbGwg"
    "aW5zaWRlIHdoYXRldmVyIHRoZSBjbGllbnQKI2l0c2VsZiBpcyBidWlsdCB0byBoYW5kbGUuCl9N"
    "QVhfV0lSRV9MSU5FID0gOTAwCiNQZXItZmllbGQgY2Fwcywgc28gbm8gY29tYmluYXRpb24gb2Yg"
    "c3RvcmVkIG9yIHR5cGVkIHRleHQgY2FuIGFkZCB1cCB0byBhIGxpbmUKI292ZXIgdGhhdCBsaW1p"
    "dC4gRXZlcnkgb25lIG9mIHRoZXNlIGZpZWxkcyBpcyBwbGF5ZXItY29udHJvbGxlZCBhbmQgdHJh"
    "dmVscyB0bwojKm90aGVyKiBwbGF5ZXJzJyBjbGllbnRzOgojIC0gY2hhdCB0ZXh0IGFuZCB0aGUg"
    "cm9vbSBuYW1lIGFyZSB0eXBlZCBzdHJhaWdodCBpbjsKIyAtIGVtYWlsL2xvY2F0aW9uL2Rlc2Ny"
    "aXB0aW9uIGNvbWUgZnJvbSAvdXBkYXRlIGFuZCBhcmUgcmVwbGF5ZWQgYnkgL3dob2lzIHRvCiMg"
    "ICB3aG9ldmVyIGFza3MsIGxvbmcgYWZ0ZXIgdGhlIGZhY3QgYW5kIHRvIHNvbWVib2R5IHdobyBu"
    "ZXZlciB0eXBlZCB0aGVtLgojTm9uZSBvZiB0aGVtIHdhcyBib3VuZGVkLCBzbyBvbmUgbG9uZyB2"
    "YWx1ZSB3YXMgZW5vdWdoIHRvIGhhbmQgYW5vdGhlciBwbGF5ZXIncwojY2xpZW50IGEgbGluZSBs"
    "b25nZXIgdGhhbiBpdCBpcyBidWlsdCB0byBwYXJzZSAtIHdoaWNoIGlzIG5vdCBhIGNvc21ldGlj"
    "CiNwcm9ibGVtIGluIGEgMjAwOCAzMi1iaXQgYmluYXJ5LCBpdCBpcyBhIGhlYXAgb3ZlcndyaXRl"
    "IGFuZCBhIGhhcmQgbG9jay11cCBvbgojYSBtYWNoaW5lIG90aGVyIHRoYW4gdGhlIG9uZSB0aGF0"
    "IGNhdXNlZCBpdC4KX01BWF9DSEFUX1RFWFQgPSAyNTUKX01BWF9XSE9JU19GSUVMRCA9IDY0ICAg"
    "ICNlbWFpbCwgbG9jYXRpb24KX01BWF9ERVNDUklQVElPTiA9IDI1NQpfTUFYX0dBTUVOQU1FID0g"
    "NjQKX01BWF9DSEFUTkFNRSA9IDQ4CiNQbGF5ZXItY3JlYXRlZCBjaGF0IGNoYW5uZWxzIGFyZSBw"
    "ZXIgdG93biBhbmQgYXJlIG5ldmVyIGdhcmJhZ2UgY29sbGVjdGVkLCBzbwojdGhlIGNvdW50IGlz"
    "IGJvdW5kZWQgcmF0aGVyIHRoYW4gbGVmdCB0byB3aG9ldmVyIGNsaWNrcyBmYXN0ZXN0LiBXZWxs"
    "IGFib3ZlIHRoZQojdHdvIHRoZSBnYW1lIHNoaXBzIHdpdGguCl9NQVhfQ0hBVF9DSEFOTkVMUyA9"
    "IDE2CiNTZXJ2ZXItY29udHJvbGxlZCB0ZXh0IHRoYXQgcmVhY2hlcyB0aGUgY2xpZW50OiB0aGUg"
    "dGl0bGUgYW5kIHRoZSBtZXNzYWdlIG9mCiN0aGUgZGF5IGFyZSB0eXBlZCBieSBhbiBhZG1pbiBp"
    "bnRvIHRoZSBHVUkgd2l0aCBubyBsZW5ndGggbGltaXQgYXQgYWxsLCBhbmQKI2JvdGggYXJlIGhh"
    "bmRlZCB0byB0aGUgY2xpZW50IGF0IGxvZ2luLCBiZWZvcmUgdGhlIHBsYXllciBjYW4gZG8gYW55"
    "dGhpbmcKI2Fib3V0IGl0LiBUcnVuY2F0ZSByYXRoZXIgdGhhbiB0cnVzdC4KX01BWF9USVRMRSA9"
    "IDEyOApfTUFYX01PVEQgPSAxMDI0CiNIZXJvIGlkcyBvbiB0aGUgd2lyZTogaGV4IG9yIGRlY2lt"
    "YWwuCiNFdmVyeXRoaW5nIHBvc2l0aW9uYWwgaW4gdGhpcyBwcm90b2NvbCBpcyBoZXggLSB0aGUg"
    "Y2xpZW50J3Mgb3duCiMvdXBkaGVyb3BvcyBjYXJyaWVzIGNvb3JkaW5hdGVzIGFzICIzOEE0IzJC"
    "MTciIC0gYW5kIHVwZGF0ZVBvcygpIGhhcyBhbHdheXMKI3ByZWZpeGVkIHRoZSBoZXJvIGlkIGlu"
    "IGhleCB0byBtYXRjaC4gQnV0ICRnYW1lY2hhbm5lbHVzZXIsIHRoZSBtZXNzYWdlIHRoYXQKI2Zp"
    "cnN0IHRlbGxzIGEgY2xpZW50IHdoaWNoIGlkIGJlbG9uZ3MgdG8gd2hpY2ggcGxheWVyLCBzZW50"
    "IHRoZSBzYW1lIGlkIGluCiNkZWNpbWFsLiBBIGNsaWVudCB0aGF0IHJlYWRzIGJvdGggZmllbGRz"
    "IHdpdGggb25lIHJhZGl4IHRoZXJlZm9yZSBjYW5ub3QKI21hdGNoIGEgcG9zaXRpb24gdXBkYXRl"
    "IHRvIHRoZSBwbGF5ZXIgaXQgYmVsb25ncyB0bywgYW5kIHRoYXQgaGVybyBzdG9wcwojbW92aW5n"
    "IG9uIGV2ZXJ5b25lIGVsc2UncyBtYXAgd2hpbGUgd2Fsa2luZyBub3JtYWxseSBvbiB0aGVpciBv"
    "d24uCiNMZWZ0IGFzIGEgc3dpdGNoIGJlY2F1c2Ugd2hpY2ggcmFkaXggdGhlIHJldGFpbCBjbGll"
    "bnQgd2FudHMgaXMgbm90CiNkb2N1bWVudGVkOiBpZiBoZXggdHVybnMgb3V0IHRvIGJlIHRoZSB3"
    "cm9uZyBndWVzcywgc2V0IEhlcm9JZEhleCA9IEZhbHNlIGluCiNDb25maWcuaW5pIGFuZCBib3Ro"
    "IG1lc3NhZ2VzIGZhbGwgYmFjayB0byBkZWNpbWFsIC0gc3RpbGwgY29uc2lzdGVudCwgd2hpY2gK"
    "I2lzIHRoZSBwYXJ0IHRoYXQgYWN0dWFsbHkgbWF0dGVycy4KX0hFUk9fSURfSEVYID0gVHJ1ZQoj"
    "T3B0aW9uYWwgc2VydmVyLT5jbGllbnQgJy9ub3AnIGhlYXJ0YmVhdCBldmVyeSAzcy4gTWFpbmx5"
    "IHVzZWZ1bCB0byBzdG9wIGhvbWUKI3JvdXRlcnMgZHJvcHBpbmcgdGhlIE5BVCBtYXBwaW5nIG9m"
    "IGFuIGlkbGUgY28tb3Agc2Vzc2lvbi4gT2ZmIGJ5IGRlZmF1bHQ6IHRoZQojcmVhbCBjbGllbnQn"
    "cyByZWFjdGlvbiB0byBhbiB1bnNvbGljaXRlZCAvbm9wIGhhcyBub3QgYmVlbiB2ZXJpZmllZC4K"
    "X1NFTkRfTk9QUyA9IEZhbHNlCiNIZXJvIGlkcyBhcmUgZHJhd24gZnJvbSAxLi5fTUFYX0hFUk9f"
    "SUQgYW5kIHJlbGVhc2VkIG9uIGRpc2Nvbm5lY3QgLSBzZWUKI0RhdGFIYW5kbGVyLmdldFVSYW5k"
    "b20uCl9NQVhfSEVST19JRCA9IDB4ODAwMAojSW4tZ2FtZSBhZG1pbiBjb25zb2xlLiBQcmVmaXgg"
    "YSBjaGF0IGxpbmUgd2l0aCB0aGlzIHRvIGFkZHJlc3MgdGhlIHNlcnZlcjsgdGhlCiNhY2NvdW50"
    "cyBhbGxvd2VkIHRvIGRvIHNvIGFyZSBsaXN0ZWQgaW4gQ29uZmlnLmluaSBhcyBhIGNvbW1hLXNl"
    "cGFyYXRlZAojQWRtaW5zPS4gRW1wdHkgYnkgZGVmYXVsdCwgd2hpY2ggZGlzYWJsZXMgdGhlIGNv"
    "bnNvbGUgb3V0cmlnaHQgLSBhIHNlcnZlciB3aXRoCiNubyBuYW1lZCBhZG1pbnMgaGFzIG5vIHBy"
    "aXZpbGVnZWQgY2hhdCBjb21tYW5kcyBhdCBhbGwuCl9BRE1JTl9QUkVGSVggPSAnIScKX0FETUlO"
    "UyA9IGZyb3plbnNldCgpCgoKREVGQVVMVF9USVRMRSA9ICdDb21tdW5pdHkgTXVsdGlwbGF5ZXIg"
    "U2VydmVyJwpERUZBVUxUX01PVEQgPSBmJzwweEZGMDAwMEZGPjxGMj5Db21tdW5pdHkgTXVsdGlw"
    "bGF5ZXIgU2VydmVyIFZlcnNpb24ge19WRVJTSU9OfTxicmVhaz0xMC4wPlxyXG4nCgojUm9vdCBu"
    "ZXh0IHRvIHRoaXMgc2NyaXB0IHJhdGhlciB0aGFuIHRoZSBwcm9jZXNzJyBjdXJyZW50IHdvcmtp"
    "bmcgZGlyZWN0b3J5LAojc28gdGhlIGRhdGFiYXNlL2NvbmZpZy9wbGF5ZXJkYXRhIGFsd2F5cyBs"
    "aXZlIGluIHRoZSBzYW1lIHBsYWNlIHdoZXRoZXIgdGhlCiNzZXJ2ZXIgaXMgZG91YmxlLWNsaWNr"
    "ZWQsIGxhdW5jaGVkIGZyb20gYSB0ZXJtaW5hbCBlbHNld2hlcmUsIG9yIGltcG9ydGVkIGJ5CiNh"
    "IEdVSSB3cmFwcGVyIChlLmcuIFRXMSBDb250cm9sIENlbnRlcikuCiNBbGxvd3MgYW4gZW1iZWRk"
    "aW5nIGhvc3QgKGUuZy4gYSBwb3J0YWJsZSBhbGwtaW4tb25lIGxhdW5jaGVyIHRoYXQgZXhlYygp"
    "cwojdGhpcyBmaWxlJ3Mgc291cmNlIGZyb20gbWVtb3J5LCB3aGVyZSBfX2ZpbGVfXyBpcyBtZWFu"
    "aW5nbGVzcykgdG8gcmVkaXJlY3QKI3doZXJlIHRoZSBkYXRhYmFzZS9jb25maWcvcGxheWVyZGF0"
    "YSBsaXZlIGJ5IHByZS1zZXR0aW5nIHRoaXMgbmFtZSBpbiB0aGUKI21vZHVsZSdzIGdsb2JhbHMg"
    "YmVmb3JlIHRoZSBtb2R1bGUgYm9keSBydW5zLiBTdGFuZGFsb25lIGV4ZWN1dGlvbiAodGhlCiNu"
    "b3JtYWwgYHB5dGhvbiBUVzFDUy5weWApIGlzIHVuYWZmZWN0ZWQ6IGZhbGxzIGJhY2sgdG8gbmV4"
    "dCB0byB0aGlzIHNjcmlwdC4KaWYgJ19FWFRFUk5BTF9EQVRBX0RJUicgaW4gZ2xvYmFscygpIGFu"
    "ZCBnbG9iYWxzKClbJ19FWFRFUk5BTF9EQVRBX0RJUiddOgogICAgX1BBVEhfUk9PVCA9IGdsb2Jh"
    "bHMoKVsnX0VYVEVSTkFMX0RBVEFfRElSJ10KZWxzZToKICAgIF9QQVRIX1JPT1QgPSBvcy5wYXRo"
    "LmRpcm5hbWUob3MucGF0aC5hYnNwYXRoKF9fZmlsZV9fKSkKX1BBVEhfREFUQUJBU0UgPSBvcy5w"
    "YXRoLmpvaW4oX1BBVEhfUk9PVCwnU2VydmVyRGF0YS5kYicpCl9QQVRIX0NPTkZJRyA9IG9zLnBh"
    "dGguam9pbihfUEFUSF9ST09ULCdDb25maWcuaW5pJykKX1BBVEhfUExBWUVSREFUQSA9IG9zLnBh"
    "dGguam9pbihfUEFUSF9ST09ULCdQbGF5ZXJEYXRhJykKCmRlZiBfZXNjYXBlTU9URChtb3RkKToK"
    "ICAgICNjb25maWdwYXJzZXIgdmFsdWVzIGNhbid0IHNhZmVseSBob2xkIHJhdyBDUi9MRiwgc3Rv"
    "cmUgYXMgXHJcbiBlc2NhcGVzCiAgICByZXR1cm4gbW90ZC5lbmNvZGUoJ3VuaWNvZGVfZXNjYXBl"
    "JykuZGVjb2RlKCdhc2NpaScpCmRlZiBfdW5lc2NhcGVNT1REKG1vdGQpOgogICAgI19lc2NhcGVN"
    "T1REIGFsd2F5cyB3cml0ZXMgcHVyZSBhc2NpaSwgYnV0IGEgaGFuZC1lZGl0ZWQgQ29uZmlnLmlu"
    "aSBtYXkgaG9sZAogICAgI3JhdyA4LWJpdCB0ZXh0OyB0b2xlcmF0ZSBpdCBpbnN0ZWFkIG9mIHJl"
    "ZnVzaW5nIHRvIHN0YXJ0IHRoZSBzZXJ2ZXIKICAgIHJldHVybiBtb3RkLmVuY29kZShfV0lSRV9F"
    "TkMsICdyZXBsYWNlJykuZGVjb2RlKCd1bmljb2RlX2VzY2FwZScpCl9DT05GSUdfREVGQVVMVFMg"
    "PSB7CiAgICAnU2VydmVyTmFtZSc6IERFRkFVTFRfVElUTEUsCiAgICAnTU9URCc6IF9lc2NhcGVN"
    "T1REKERFRkFVTFRfTU9URCksCiAgICAnUG9ydCc6IHN0cihfVFdfTE9CQllfUE9SVCksCiAgICAn"
    "QXV0b1JlZ2lzdGVyJzogc3RyKF9BVVRPX1JFR0lTVEVSKSwKICAgICdBbGxvd0FueUxvZ2luJzog"
    "c3RyKF9ERUJVR19BTExPV19BTllfTE9HSU4pLAogICAgJ1Bvc2l0aW9uVXBkYXRlSHonOiBzdHIo"
    "X1BPU19VUERBVEVfSFopLAogICAgJ0lkbGVUaW1lb3V0Jzogc3RyKF9JRExFX1RJTUVPVVQpLAog"
    "ICAgJ0tlZXBhbGl2ZSc6IHN0cihfU0VORF9OT1BTKSwKICAgICdSZXdyaXRlR2FtZUhvc3QnOiBz"
    "dHIoX1JFV1JJVEVfR0FNRV9IT1NUKSwKICAgICdQdWJsaWNIb3N0QWRkcmVzcyc6IF9QVUJMSUNf"
    "SE9TVF9BRERSRVNTLAogICAgJ1N0cmlwQWx0QWRkcmVzc2VzJzogc3RyKF9TVFJJUF9BTFRfQURE"
    "UkVTU0VTKSwKICAgICdIZXJvSWRIZXgnOiBzdHIoX0hFUk9fSURfSEVYKSwKICAgICdEZWJ1Z0Nv"
    "bW1hbmRzJzogc3RyKF9ERUJVR19MT0dfQ09NTUFORFMpLAogICAgJ0RlYnVnQ29tbWFuZHNWZXJi"
    "b3NlJzogc3RyKF9ERUJVR19MT0dfVkVSQk9TRSksCiAgICAnQWRtaW5zJzogJycsCiAgICAnQWRt"
    "aW5QcmVmaXgnOiBfQURNSU5fUFJFRklYLAp9CmRlZiBsb2FkQ29uZmlnKCk6CiAgICBjZmcgPSBj"
    "b25maWdwYXJzZXIuQ29uZmlnUGFyc2VyKCkKICAgIGNmZ1snc2VydmVyJ10gPSBkaWN0KF9DT05G"
    "SUdfREVGQVVMVFMpCiAgICBpZiBvcy5wYXRoLmV4aXN0cyhfUEFUSF9DT05GSUcpOgogICAgICAg"
    "IGNmZy5yZWFkKF9QQVRIX0NPTkZJRykKICAgIGVsc2U6CiAgICAgICAgc2F2ZUNvbmZpZyhjZmcp"
    "CiAgICByZXR1cm4gY2ZnCmRlZiBzYXZlQ29uZmlnKGNmZyk6CiAgICB3aXRoIG9wZW4oX1BBVEhf"
    "Q09ORklHLCAndycsIGVuY29kaW5nPSd1dGYtOCcpIGFzIGY6CiAgICAgICAgY2ZnLndyaXRlKGYp"
    "CmRlZiBhcHBseUNvbmZpZyhjZmcpOgogICAgI0FwcGxpZXMgY29uZmlnIHZhbHVlcyB0byB0aGUg"
    "bGl2ZSBtb2R1bGUgZ2xvYmFscy4gU2VydmVyTmFtZS9NT1RELwogICAgI0F1dG9SZWdpc3RlciB0"
    "YWtlIGVmZmVjdCBpbW1lZGlhdGVseSAocmVhZCBmcmVzaCBwZXIgbG9naW4gYXR0ZW1wdCk7CiAg"
    "ICAjUG9ydCBvbmx5IHRha2VzIGVmZmVjdCBmb3Igc2VydmVycyBzdGFydGVkIGFmdGVyIHRoaXMg"
    "Y2FsbC4KICAgIGdsb2JhbCBERUZBVUxUX1RJVExFLCBERUZBVUxUX01PVEQsIF9UV19MT0JCWV9Q"
    "T1JULCBfQVVUT19SRUdJU1RFUiwgX0RFQlVHX0FMTE9XX0FOWV9MT0dJTgogICAgZ2xvYmFsIF9Q"
    "T1NfVVBEQVRFX0haLCBfSURMRV9USU1FT1VULCBfU0VORF9OT1BTCiAgICBnbG9iYWwgX1JFV1JJ"
    "VEVfR0FNRV9IT1NULCBfREVCVUdfTE9HX0NPTU1BTkRTLCBfREVCVUdfTE9HX1ZFUkJPU0UKICAg"
    "IGdsb2JhbCBfUFVCTElDX0hPU1RfQUREUkVTUywgX1NUUklQX0FMVF9BRERSRVNTRVMsIF9IRVJP"
    "X0lEX0hFWAogICAgZ2xvYmFsIF9BRE1JTlMsIF9BRE1JTl9QUkVGSVgKICAgIHNlYyA9IGNmZ1sn"
    "c2VydmVyJ10KICAgIERFRkFVTFRfVElUTEUgPSBzZWMuZ2V0KCdTZXJ2ZXJOYW1lJywgZmFsbGJh"
    "Y2s9REVGQVVMVF9USVRMRSkKICAgIERFRkFVTFRfTU9URCA9IF91bmVzY2FwZU1PVEQoc2VjLmdl"
    "dCgnTU9URCcsIGZhbGxiYWNrPV9lc2NhcGVNT1REKERFRkFVTFRfTU9URCkpKQogICAgX1RXX0xP"
    "QkJZX1BPUlQgPSBzZWMuZ2V0aW50KCdQb3J0JywgZmFsbGJhY2s9X1RXX0xPQkJZX1BPUlQpCiAg"
    "ICBfQVVUT19SRUdJU1RFUiA9IHNlYy5nZXRib29sZWFuKCdBdXRvUmVnaXN0ZXInLCBmYWxsYmFj"
    "az1fQVVUT19SRUdJU1RFUikKICAgIF9ERUJVR19BTExPV19BTllfTE9HSU4gPSBzZWMuZ2V0Ym9v"
    "bGVhbignQWxsb3dBbnlMb2dpbicsIGZhbGxiYWNrPV9ERUJVR19BTExPV19BTllfTE9HSU4pCiAg"
    "ICAjQ2xhbXBlZCByYXRoZXIgdGhhbiB0cnVzdGVkOiB0aGVzZSBjb21lIGZyb20gYSBoYW5kLWVk"
    "aXRhYmxlIGluaSwgYW5kIGEKICAgICNzdHJheSAwIG9yIDEwMDAwIGhlcmUgd291bGQgZWl0aGVy"
    "IHN0b3AgcG9zaXRpb24gdXBkYXRlcyBlbnRpcmVseSBvciBzcGluCiAgICAjdGhlIHVwZGF0ZSB0"
    "aHJlYWQgZmxhdCBvdXQuCiAgICBoeiA9IHNlYy5nZXRmbG9hdCgnUG9zaXRpb25VcGRhdGVIeics"
    "IGZhbGxiYWNrPV9QT1NfVVBEQVRFX0haKQogICAgX1BPU19VUERBVEVfSFogPSBtaW4obWF4KGh6"
    "LCAwLjUpLCBfUE9TX1VQREFURV9IWl9NQVgpCiAgICBfSURMRV9USU1FT1VUID0gbWF4KDAsIHNl"
    "Yy5nZXRpbnQoJ0lkbGVUaW1lb3V0JywgZmFsbGJhY2s9X0lETEVfVElNRU9VVCkpCiAgICBfU0VO"
    "RF9OT1BTID0gc2VjLmdldGJvb2xlYW4oJ0tlZXBhbGl2ZScsIGZhbGxiYWNrPV9TRU5EX05PUFMp"
    "CiAgICBfUkVXUklURV9HQU1FX0hPU1QgPSBzZWMuZ2V0Ym9vbGVhbignUmV3cml0ZUdhbWVIb3N0"
    "JywgZmFsbGJhY2s9X1JFV1JJVEVfR0FNRV9IT1NUKQogICAgX1BVQkxJQ19IT1NUX0FERFJFU1Mg"
    "PSBzZWMuZ2V0KCdQdWJsaWNIb3N0QWRkcmVzcycsIGZhbGxiYWNrPV9QVUJMSUNfSE9TVF9BRERS"
    "RVNTKS5zdHJpcCgpCiAgICBfU1RSSVBfQUxUX0FERFJFU1NFUyA9IHNlYy5nZXRib29sZWFuKCdT"
    "dHJpcEFsdEFkZHJlc3NlcycsIGZhbGxiYWNrPV9TVFJJUF9BTFRfQUREUkVTU0VTKQogICAgX0hF"
    "Uk9fSURfSEVYID0gc2VjLmdldGJvb2xlYW4oJ0hlcm9JZEhleCcsIGZhbGxiYWNrPV9IRVJPX0lE"
    "X0hFWCkKICAgIF9ERUJVR19MT0dfQ09NTUFORFMgPSBzZWMuZ2V0Ym9vbGVhbignRGVidWdDb21t"
    "YW5kcycsIGZhbGxiYWNrPV9ERUJVR19MT0dfQ09NTUFORFMpCiAgICBfREVCVUdfTE9HX1ZFUkJP"
    "U0UgPSBzZWMuZ2V0Ym9vbGVhbignRGVidWdDb21tYW5kc1ZlcmJvc2UnLCBmYWxsYmFjaz1fREVC"
    "VUdfTE9HX1ZFUkJPU0UpCiAgICAjQ2FzZWZvbGRlZCBvbmNlIGhlcmUgcmF0aGVyIHRoYW4gcGVy"
    "IG1lc3NhZ2U6IG5hbWVzIGFyZSBjb21wYXJlZCBhZ2FpbnN0CiAgICAjdGhpcyBzZXQgb24gZXZl"
    "cnkgY2hhdCBsaW5lIHRoYXQgc3RhcnRzIHdpdGggdGhlIHByZWZpeC4KICAgIF9BRE1JTlMgPSBm"
    "cm96ZW5zZXQobi5zdHJpcCgpLmNhc2Vmb2xkKCkKICAgICAgICAgICAgICAgICAgICAgICAgZm9y"
    "IG4gaW4gc2VjLmdldCgnQWRtaW5zJywgZmFsbGJhY2s9JycpLnNwbGl0KCcsJykgaWYgbi5zdHJp"
    "cCgpKQogICAgX0FETUlOX1BSRUZJWCA9IHNlYy5nZXQoJ0FkbWluUHJlZml4JywgZmFsbGJhY2s9"
    "X0FETUlOX1BSRUZJWCkuc3RyaXAoKSBvciAnIScKQ0ZHID0gbG9hZENvbmZpZygpCmFwcGx5Q29u"
    "ZmlnKENGRykKCiMjIyBVU0VSIFNUUlVDVFVSRQojIGNvbm5lY3Rpb24KIyB1c2VybmFtZQojIGhl"
    "cm9kYXRhCiMgcG9zaXRpb24KIyBnYW1lY2hhbm5lbAojIGNoYXRjaGFubmVsCiMgZ2FtZQoKY2xh"
    "c3MgVXNlcigpOiAjVE9ETyBtZXJnZSB1c2VyIGludG8gY29ubmVjdGlvbj8sIHZhbGlkYXRpb24g"
    "Y2FuIGJlIGFzc3VtZWQgYnkgc3RhZ2UKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBuYW1lLCBjb24p"
    "OgogICAgICAgIHNlbGYuaGVyb2RhdGEgPSBiJycKICAgICAgICAjJzAjMCcsIG5vdCBOb25lOiB0"
    "aGlzIGdvZXMgc3RyYWlnaHQgaW50byB0aGUgJGdhbWVjaGFubmVsdXNlciBzZW50IHRvCiAgICAg"
    "ICAgI2V2ZXJ5IG90aGVyIGNsaWVudCwgYW5kIGFuIHVuc2V0IHZhbHVlIHVzZWQgdG8gcmVhY2gg"
    "dGhlbSBhcyB0aGUKICAgICAgICAjbGl0ZXJhbCB0ZXh0ICJOb25lIiB3aGVyZSBjb29yZGluYXRl"
    "cyB3ZXJlIGV4cGVjdGVkLgogICAgICAgIHNlbGYucG9zZGF0YSA9ICcwIzAnCiAgICAgICAgc2Vs"
    "Zi5wb3NjaGFuZ2VkID0gRmFsc2UKICAgICAgICBzZWxmLnJlcXVlc3RlZENoYW5uZWwgPSBOb25l"
    "CiAgICAgICAgc2VsZi5nYW1lY2hhbm5lbCA9IE5vbmUKICAgICAgICBzZWxmLmNoYXRjaGFubmVs"
    "ID0gTm9uZQogICAgICAgIHNlbGYucmVxdWVzdGVkR2FtZSA9IE5vbmUKICAgICAgICBzZWxmLmdh"
    "bWUgPSBOb25lCiAgICAgICAgc2VsZi5uYW1lID0gbmFtZQogICAgICAgICNDYWNoZWQsIG5vdCBs"
    "b29rZWQgdXAgcGVyIG1lc3NhZ2U6IHRoZSBndWlsZCBuYW1lIGdvZXMgb3V0IGluIHRoZQogICAg"
    "ICAgICNzZWNvbmQgZmllbGQgb2YgZXZlcnkgJGdhbWVjaGFubmVsdXNlciBhbmQgJGNoYXRjaGFu"
    "bmVsdXNlciAtIHRoZQogICAgICAgICNzYW1lIGZpZWxkIC93aG9pcyByZXBvcnRzIGFzIHRoZSBn"
    "dWlsZCAtIGFuZCB0aG9zZSBhcmUgc2VudCBmYXIgdG9vCiAgICAgICAgI29mdGVuIHRvIGhpdCB0"
    "aGUgZGF0YWJhc2UgZWFjaCB0aW1lLgogICAgICAgIHNlbGYuZ3VpbGQgPSBzYW5pdGl6ZVRleHQo"
    "R0RILmdldEd1aWxkTmFtZShuYW1lKSkKICAgICAgICBzZWxmLmxvZ2luVGltZSA9IGRhdGV0aW1l"
    "LmRhdGV0aW1lLm5vdygpCiAgICAgICAgc2VsZi5pZG51bSA9IEdESC5nZXRVUmFuZG9tKCkKICAg"
    "ICAgICBzZWxmLmNvbm5lY3Rpb24gPSBjb24gI3NlcnZlciA9IGNvbi5zZXJ2ZXIKICAgICAgICAj"
    "c2VsZi5jb25uZWN0aW9uLmd1aWQgLT4gZ3VpZCB3aGVuIHJlbGV2YW50CiAgICAgICAgc2VsZi5w"
    "Z3VpZCA9IHByZXR0eV9ndWlkKHNlbGYuY29ubmVjdGlvbi5ndWlkKQogICAgZGVmIGxlYXZlQ2hh"
    "bm5lbChzZWxmKToKICAgICAgICBpZiBzZWxmLnJlcXVlc3RlZENoYW5uZWw6CiAgICAgICAgICAg"
    "ICNsaXN0LnJlbW92ZSgpIHJhaXNlcyBWYWx1ZUVycm9yIHdoZW4gdGhlIGVudHJ5IGlzIGFscmVh"
    "ZHkgZ29uZTsKICAgICAgICAgICAgI3RoYXQgdXNlZCB0byBhYm9ydCB0aGUgcmVzdCBvZiB0aGUg"
    "ZGlzY29ubmVjdCBjbGVhbnVwCiAgICAgICAgICAgIGlmIHNlbGYuY29ubmVjdGlvbiBpbiBzZWxm"
    "LnJlcXVlc3RlZENoYW5uZWwucmVxdWVzdGVkOgogICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0"
    "ZWRDaGFubmVsLnJlcXVlc3RlZC5yZW1vdmUoc2VsZi5jb25uZWN0aW9uKQogICAgICAgICAgICBz"
    "ZWxmLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lCiAgICAgICAgaWYgc2VsZi5nYW1lY2hhbm5lbDoK"
    "ICAgICAgICAgICAgc2VsZi5nYW1lY2hhbm5lbC5sZWF2ZUNoYW5uZWwoc2VsZi5jb25uZWN0aW9u"
    "KQogICAgICAgICAgICAjbGVhdmVDaGFubmVsIGFsc28gbGVhdmVzIGNoYXQKICAgIGRlZiBsZWF2"
    "ZUNoYXQoc2VsZik6CiAgICAgICAgaWYgc2VsZi5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgaWYg"
    "c2VsZi5jb25uZWN0aW9uIGluIHNlbGYuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgICAgICBzZWxm"
    "LmNoYXRjaGFubmVsLnJlbW92ZShzZWxmLmNvbm5lY3Rpb24pCiAgICAgICAgICAgIGxlYXZlbXNn"
    "ID0gX2VtKGYnJmNoYXRjaGFubmVsdXNlciAie3NlbGYubmFtZX0iJykKICAgICAgICAgICAgc2Vs"
    "Zi5jb25uZWN0aW9uLnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6c2VsZi5jaGF0Y2hhbm5lbCwn"
    "bWVzc2FnZSc6bGVhdmVtc2d9KQogICAgICAgICAgICBzZWxmLmNoYXRjaGFubmVsPU5vbmUKICAg"
    "IGRlZiBzdG9wR2FtZShzZWxmKToKICAgICAgICBpZiBzZWxmLnJlcXVlc3RlZEdhbWU6CiAgICAg"
    "ICAgICAgICNCb3RoIGd1YXJkcyBtYXR0ZXI6IHRoZSBjaGFubmVsIG1heSBhbHJlYWR5IGJlIGdv"
    "bmUgKGxlYXZlQ2hhbm5lbAogICAgICAgICAgICAjY2xlYXJzIGl0IGJlZm9yZSBzdG9wR2FtZSBy"
    "dW5zIG9uIHNvbWUgcGF0aHMpIGFuZCB0aGUgcGVuZGluZwogICAgICAgICAgICAjcmVxdWVzdCBt"
    "YXkgYWxyZWFkeSBoYXZlIGJlZW4gY29uc3VtZWQgYnkgY3JlYXRlR2FtZS4gRWl0aGVyIG9uZQog"
    "ICAgICAgICAgICAjdXNlZCB0byByYWlzZSAoQXR0cmlidXRlRXJyb3Igb24gTm9uZSAvIEtleUVy"
    "cm9yKSBpbnNpZGUgdGhlCiAgICAgICAgICAgICNkaXNjb25uZWN0IHBhdGggYW5kIGFib3J0IHRo"
    "ZSByZXN0IG9mIHRoZSBjbGVhbnVwLCBsZWFraW5nIHRoZQogICAgICAgICAgICAjcGxheWVyJ3Mg"
    "ZW50cnkgaW4gYWN0aXZlVXNlcnMuCiAgICAgICAgICAgIGlmIHNlbGYuZ2FtZWNoYW5uZWw6CiAg"
    "ICAgICAgICAgICAgICBzZWxmLmdhbWVjaGFubmVsLmdhbWVSZXF1ZXN0cy5wb3Aoc2VsZi5yZXF1"
    "ZXN0ZWRHYW1lLCBOb25lKQogICAgICAgICAgICBzZWxmLnJlcXVlc3RlZEdhbWUgPSBOb25lCiAg"
    "ICAgICAgaWYgc2VsZi5nYW1lOgogICAgICAgICAgICBzZWxmLmdhbWUucmVtb3ZlKHNlbGYuY29u"
    "bmVjdGlvbikKICAgIGRlZiBkaXNjb25uZWN0KHNlbGYsIHNlcnZlcik6CiAgICAgICAgc2VsZi5z"
    "dG9wR2FtZSgpCiAgICAgICAgc2VsZi5sZWF2ZUNoYW5uZWwoKQogICAgICAgIHNlcnZlci5zdGF0"
    "ZS5yZWxlYXNlVXNlcihzZWxmLm5hbWUsIHNlbGYuY29ubmVjdGlvbikKICAgICAgICBHREgucmVs"
    "ZWFzZVVSYW5kb20oc2VsZi5pZG51bSkKICAgIGRlZiB3aXJlSWQoc2VsZik6CiAgICAgICAgI1Ro"
    "ZSBvbmUgcGxhY2UgdGhlIGhlcm8gaWQgaXMgZm9ybWF0dGVkLCBzbyAkZ2FtZWNoYW5uZWx1c2Vy"
    "IGFuZAogICAgICAgICMvdXBkaGVyb3BvcyBjYW4gbmV2ZXIgZGlzYWdyZWUgYWdhaW4gLSBzZWUg"
    "X0hFUk9fSURfSEVYLgogICAgICAgIHJldHVybiBmJ3tzZWxmLmlkbnVtOnh9JyBpZiBfSEVST19J"
    "RF9IRVggZWxzZSBmJ3tzZWxmLmlkbnVtfScKICAgIGRlZiBnZXRHQ1Vtc2coc2VsZik6CiAgICAg"
    "ICAgaGRsID0gbGVuKHNlbGYuaGVyb2RhdGEpCiAgICAgICAgaWYgaGRsPT0wOgogICAgICAgICAg"
    "ICByZXR1cm4gYicnCiAgICAgICAgcmV0dXJuIF9lbShmJyRnYW1lY2hhbm5lbHVzZXIgIntzZWxm"
    "Lm5hbWV9IiAie3NlbGYuZ3VpbGR9IiAiMTAwIiAie3NlbGYud2lyZUlkKCl9IiAiMCIgIntzZWxm"
    "LnBndWlkfSIgIntzZWxmLnBvc2RhdGF9IiAie2hkbH0iJykrc2VsZi5oZXJvZGF0YQogICAgZGVm"
    "IGdldENDVW1zZyhzZWxmKToKICAgICAgICB2YiA9IDAgI29yIDB4RkZGRkZGRkYoNDI5NDk2NzI5"
    "NT0gLTEmMzJiaXQ/KQogICAgICAgIHJldHVybiBfZW0oZickY2hhdGNoYW5uZWx1c2VyICJ7c2Vs"
    "Zi5uYW1lfSIgIntzZWxmLmd1aWxkfSIgInt2Yn0iICJ7c2VsZi5wZ3VpZH0iJykKICAgICAgICAj"
    "ICRjaGF0Y2hhbm5lbHVzZXIgIntuYW1lfSIgIiIgIjAiICJ7Z3VpZH0iCiMgaW5jcmVhc2luZyBt"
    "YXkgaW1wcm92ZSBzZWN1cml0eSBhdCB0aGUgY29zdCBvZiBwZXJmb3JtYW5jZQojIG9ubHkgdXBk"
    "YXRlcyB3aGVuIHVzZXIgbG9ncyBpbiBhbmQgaXMgc3RvcmVkIGFsb25nc2lkZSBzYWx0IGluIGRh"
    "dGFiYXNlCl9IQVNISVRFUiA9IDEwMDAwMApkZWYgX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHNhbHQs"
    "IGhJdHIpOgogICAgI3V0Zi04LCBub3QgYXNjaWk6IGEgcGFzc3dvcmQgd2l0aCBhbiA4LWJpdCBj"
    "aGFyYWN0ZXIgdXNlZCB0byByYWlzZSBoZXJlIGFuZAogICAgI2Ryb3AgdGhlIGNvbm5lY3Rpb24g"
    "aW5zdGVhZCBvZiBsb2dnaW5nIHRoZSBwbGF5ZXIgaW4uIFB1cmUtYXNjaWkgcGFzc3dvcmRzCiAg"
    "ICAjZW5jb2RlIHRvIGlkZW50aWNhbCBieXRlcyB1bmRlciBib3RoLCBzbyBubyBzdG9yZWQgaGFz"
    "aCBjaGFuZ2VzLgogICAgcmV0dXJuIGhhc2hsaWIucGJrZGYyX2htYWMoJ3NoYTI1NicsIHBhc3N3"
    "b3JkLmVuY29kZSgndXRmLTgnKSwgc2FsdCwgaEl0cikKICAgIAojIyMgU1FMIElORk8KIyBfREJJ"
    "TkZPOiBWRVJTSU9OIDEKIyB1c2VyVGFibGUKIyAtIHJvd2lkLCB1c2VybmFtZSwgcGFzc0hhc2gs"
    "IHNlcmlhbCwgdW5pcXVlU2FsdCwgbGFzdExvZ2luLCBlbWFpbCwgbG9jYXRpb24sIHllYXJvZmJp"
    "cnRoKGVzdGltYXRlKSwgZ2VuZGVyLCBkZXNjcmlwdGlvbgojIGZvcm1UYWJsZQojIC0gcm93aWQs"
    "IGZvcm0KIyMgLS0tLS0tLS0tLS0tLS0tLSAjIwojIFRPRE8gVkVSU0lPTiAyOiBndWlsZHMsIGxl"
    "YWRlcmJvYXJkLCBldGM/CgojVE9ETyBjb252ZXJ0IGRhdGFiYXNlIHRvIHNpbmdsZXRocmVhZCBh"
    "Y2Nlc3MgZm9yIGNvbXBhdGliaWxpdHk/IHVubmVjY2VzYXJ5PwojY2xhc3MgRGF0YVJlcXVlc3Qo"
    "dGhyZWFkaW5nLkV2ZW50KToKIyAgIGRhdGEgPSBOb25lCiMgICBkZWYgc2V0KHZhbCk6CiMgICAg"
    "ICAgc2VsZi5kYXRhPXZhbAojICAgICAgIHN1cGVyKCkuc2V0KCkKIyAgIGRlZiB3YWl0KCk6CiMg"
    "ICAgICAgc3VwZXIoKS53YWl0KCkKIyAgICAgICByZXR1cm4gc2VsZi5kYXRhCiMqIGRhdGFiYXNl"
    "IHRocmVhZDoKIyAgIF9kclEgPSBkYXRhIHJlcXVlc3QgcXVldWUsIHByb2Nlc3NlZCBpbiBkYXRh"
    "YmFzZSB0aHJlYWQKIyAgIGV4dGVybmFsIGZ1bmN0aW9ucyBhZGQgcmVxdWVzdCBmb3IgaW50ZXJu"
    "YWwgZnVuY3Rpb24gYW5kIHJldHVybiByZXF1ZXN0IHRvIGF3YWl0CiMgICBkcm9iaiBpbiBxdWV1"
    "ZSA9IChkciwgZnRhcmdldCwgKGFyZ3MpKSwgZHIuc2V0KGZ0YXJnZXQoKmFyZ3MpKQojVE9ETyBv"
    "cmdhbml6ZSBTUUwgY29tbWFuZHM/IG1ha2UgaXQgbW9yZSBiZWF1dGlmdWw/Cl9TUUxfZGJJbmZv"
    "RXhpc3RzID0gJ1NFTEVDVCBuYW1lIEZST00gc3FsaXRlX21hc3RlciBXSEVSRSBuYW1lPSJfREJJ"
    "TkZPIicKX1NRTF9kYlZlcnNpb24gPSAnU0VMRUNUIFZFUlNJT04gRlJPTSBfREJJTkZPJwpfU1FM"
    "SU5JVF9kYkluZm9UYWJsZSA9ICdDUkVBVEUgVEFCTEUgX0RCSU5GTyhWRVJTSU9OKScKX0RCQ1VS"
    "VkVSID0gMgpfU1FMSU5JVF9kYkluZm9WZXJzaW9uID0gZidJTlNFUlQgSU5UTyBfREJJTkZPIFZB"
    "TFVFUyAoe19EQkNVUlZFUn0pJwpfU1FMVVBEX2RiSW5mb1ZlcnNpb24gPSBmJ1VQREFURSBfREJJ"
    "TkZPIFNFVCBWRVJTSU9OID0ge19EQkNVUlZFUn0nCiN5b2IgPSB5ZWFyIG9mIGJpcnRoIChlc3Rp"
    "bWF0ZSkKI2dlbmRlcjogMCA9IE1hbGUKX1NRTElOSVRfZGJVc2VyVGFibGUgPSAnQ1JFQVRFIFRB"
    "QkxFIHVzZXJUYWJsZSh1c2VybmFtZSBVTklRVUUsIHBhc3NIYXNoLCBzZXJpYWwsIHVuaXF1ZVNh"
    "bHQsIGhhc2hJdGVyLCBsYXN0TG9naW4gVElNRVNUQU1QLCBlbWFpbCwgbG9jYXRpb24sIHlvYiwg"
    "Z2VuZGVyLCBkZXNjcmlwdGlvbiknCl9TUUxJTklUX2RiRm9ybVRhYmxlID0gJ0NSRUFURSBUQUJM"
    "RSBmb3JtVGFibGUoZm9ybSBVTklRVUUpJyAjdXNpbmcgcm93aWQgYXMgSUQKIy0tLSBndWlsZHMg"
    "KERCIHZlcnNpb24gMikgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tCiNyYW5rOiAyID0gZm91bmRlci9sZWFkZXIsIDEgPSBvZmZpY2VyLCAwID0gbWVt"
    "YmVyLiBBIHBsYXllciBpcyBpbiBhdCBtb3N0IG9uZQojZ3VpbGQsIHdoaWNoIGlzIHdoYXQgdGhl"
    "IGNsaWVudCdzIFVJIGFzc3VtZXMgKHdob2lzIGNhcnJpZXMgYSBzaW5nbGUgbmFtZSkuCiNndWls"
    "ZGtleSBpcyBndWlsZG5hbWUuY2FzZWZvbGQoKSBhbmQgaXMgd2hhdCB1bmlxdWVuZXNzIGFuZCBl"
    "dmVyeSBsb29rdXAgZ28KI3Rocm91Z2guIFNRTGl0ZSdzIG93biBDT0xMQVRFIE5PQ0FTRSBvbmx5"
    "IGZvbGRzIEEtWiwgc28gb24gdGhpcyBzZXJ2ZXIgLQojd2hlcmUgdGhlIG5hbWVzIGFyZSBDeXJp"
    "bGxpYyAtIGl0IHdvdWxkIGhhdmUgbGV0ICLQndC+0YfQvdGL0LUg0JLQvtC70LrQuCIgYW5kICLQ"
    "vdC+0YfQvdGL0LUKI9Cy0L7Qu9C60LgiIGNvZXhpc3QgYXMgdHdvIHNlcGFyYXRlIGd1aWxkcyB0"
    "aGF0IHBsYXllcnMgY291bGQgbm90IHRlbGwgYXBhcnQuCl9TUUxJTklUX2RiR3VpbGRUYWJsZSA9"
    "ICdDUkVBVEUgVEFCTEUgZ3VpbGRUYWJsZShndWlsZG5hbWUsIGd1aWxka2V5IFVOSVFVRSwgb3du"
    "ZXIsIGNyZWF0ZWQgVElNRVNUQU1QLCBkZXNjcmlwdGlvbiknCl9TUUxJTklUX2RiR3VpbGRNZW1i"
    "ZXJUYWJsZSA9ICdDUkVBVEUgVEFCTEUgZ3VpbGRNZW1iZXJUYWJsZShndWlsZG5hbWUsIHVzZXJu"
    "YW1lIFVOSVFVRSwgcmFuayknCl9TUUxfZ3VpbGRFeGlzdHMgPSAnU0VMRUNUIGd1aWxkbmFtZSBG"
    "Uk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRrZXkgPSA/JwpfU1FMX2NyZWF0ZUd1aWxkID0gJ0lO"
    "U0VSVCBJTlRPIGd1aWxkVGFibGUgVkFMVUVTICg/LD8sPyw/LD8pJwpfU1FMX2RlbGV0ZUd1aWxk"
    "ID0gJ0RFTEVURSBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycKX1NRTF9ndWls"
    "ZE93bmVyID0gJ1NFTEVDVCBvd25lciBGUk9NIGd1aWxkVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0g"
    "PycKX1NRTF9hZGRHdWlsZE1lbWJlciA9ICdJTlNFUlQgT1IgUkVQTEFDRSBJTlRPIGd1aWxkTWVt"
    "YmVyVGFibGUgVkFMVUVTICg/LD8sPyknCl9TUUxfZGVsR3VpbGRNZW1iZXIgPSAnREVMRVRFIEZS"
    "T00gZ3VpbGRNZW1iZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxfZGVsR3VpbGRNZW1i"
    "ZXJzID0gJ0RFTEVURSBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycK"
    "X1NRTF9ndWlsZE9mVXNlciA9ICdTRUxFQ1QgZ3VpbGRuYW1lLCByYW5rIEZST00gZ3VpbGRNZW1i"
    "ZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxfZ3VpbGRNZW1iZXJzID0gJ1NFTEVDVCB1"
    "c2VybmFtZSwgcmFuayBGUk9NIGd1aWxkTWVtYmVyVGFibGUgV0hFUkUgZ3VpbGRuYW1lID0gPycK"
    "X1NRTF9hbGxHdWlsZHMgPSAnU0VMRUNUIGd1aWxkbmFtZSBGUk9NIGd1aWxkVGFibGUgT1JERVIg"
    "QlkgZ3VpbGRuYW1lIENPTExBVEUgTk9DQVNFJwojU2FtZSBzaGFwZSBhcyB0aGUgdXNlcm5hbWUg"
    "cnVsZTogdGhlIG5hbWUgdHJhdmVscyBpbnNpZGUgcXVvdGVkIHByb3RvY29sCiNmaWVsZHMsIHNv"
    "IGFueXRoaW5nIHRoYXQgY291bGQgY2xvc2UgYSBxdW90ZSBpcyByZWplY3RlZCBvdXRyaWdodCBy"
    "YXRoZXIgdGhhbgojc2lsZW50bHkgcmV3cml0dGVuLiBTcGFjZXMgYXJlIGFsbG93ZWQgLSBndWls"
    "ZCBuYW1lcyBjb21tb25seSBoYXZlIHRoZW0uCl9SRV9WQUxJRF9HVUlMRE5BTUUgPSByZS5jb21w"
    "aWxlKHInXlteIlxyXG5cMF17MywzMn0kJykKCl9TUUxfdXNlcklEID0gJ1NFTEVDVCByb3dpZCBG"
    "Uk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxfdXNlcklEX1NjaGsgPSAnU0VM"
    "RUNUIHJvd2lkIEZST00gdXNlclRhYmxlIFdIRVJFIHNlcmlhbCA9ID8nCl9TUUxfdXNlcklEX3N0"
    "cmljdCA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2VyVGFibGUgV0hFUkUgdXNlcm5hbWUgPSA/IEFO"
    "RCBzZXJpYWwgPSA/JwpfU1FMX3JlZ2lzdGVyVXNlciA9ICdJTlNFUlQgSU5UTyB1c2VyVGFibGUg"
    "VkFMVUVTICg/LD8sPyw/LD8sPyw/LD8sPyw/LD8pJwpfU1FMX2RlbGV0ZVVzZXIgPSAnREVMRVRF"
    "IEZST00gdXNlclRhYmxlIFdIRVJFIHVzZXJuYW1lID0gPycKX1NRTF9nZXRMb2dpbiA9ICdTRUxF"
    "Q1QgdXNlcm5hbWUsIHBhc3NIYXNoLCB1bmlxdWVTYWx0LCBoYXNoSXRlciBGUk9NIHVzZXJUYWJs"
    "ZSBXSEVSRSByb3dpZCA9ID8nCl9TUUxVUERfcGFzc0hhc2ggPSAnVVBEQVRFIHVzZXJUYWJsZSBT"
    "RVQgcGFzc0hhc2ggPSA/LCBoYXNoSXRlciA9ID8gV0hFUkUgcm93aWQgPSA/JwpfU1FMX2xvZ2lu"
    "VXBkYXRlID0gJ1VQREFURSB1c2VyVGFibGUgU0VUIGxhc3RMb2dpbiA9ID8gV0hFUkUgcm93aWQg"
    "PSA/JwpfU1FMX2dldFdob2lzID0gJ1NFTEVDVCBlbWFpbCwgbG9jYXRpb24sIHlvYiwgZ2VuZGVy"
    "LCBkZXNjcmlwdGlvbiBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8nCl9TUUxVUERf"
    "d2hvaXMgPSAnVVBEQVRFIHVzZXJUYWJsZSBTRVQgZW1haWwgPSA/LCBsb2NhdGlvbiA9ID8sIHlv"
    "YiA9ID8sIGdlbmRlciA9ID8sIGRlc2NyaXB0aW9uID0gPyBXSEVSRSB1c2VybmFtZSA9ID8nCiNp"
    "ZiBkb2VzIG5vdCBleGlzdCwgZ2VuZXJhdGUsIGNoYW5nZSBmb3JtYXQgZm9yIG1vZHBhY2tzCl9T"
    "UUxfZm9ybUlEID0gJ1NFTEVDVCByb3dpZCBmcm9tIGZvcm1UYWJsZSBXSEVSRSBmb3JtID0gPycK"
    "X1NRTEFERF9mb3JtSUQgPSAnSU5TRVJUIElOVE8gZm9ybVRhYmxlIFZBTFVFUyAoPyknCl9GT1JN"
    "X1BERmlsZSA9ICd7Onh9X3s6eH0uYmluJyAjIHBsYXllcmRhdGFcdXNlcklEX2Zvcm1JRC5iaW4K"
    "CmRlZiByZWFkQmluKGZpbGVwYXRoKToKICAgIHdpdGggb3BlbihmaWxlcGF0aCwgInJiIikgYXMg"
    "ZjoKICAgICAgICByZXR1cm4gZi5yZWFkKCkKY2xhc3MgRGF0YUhhbmRsZXIoKToKICAgIGRlZiBf"
    "X2luaXRfXyhzZWxmKToKICAgICAgICAjaW5zdGFuY2UgYXR0cmlidXRlLCBub3QgYSBjbGFzcyBh"
    "dHRyaWJ1dGUgLSBzYW1lIHJlYXNvbmluZyBhcwogICAgICAgICNHYW1lU3RhdGUuYWN0aXZlVXNl"
    "cnM6IHNoYXJlZCBjbGFzcyBzdGF0ZSBsZWFrcyBiZXR3ZWVuIGluc3RhbmNlcwogICAgICAgIHNl"
    "bGYudXNlZE51bXMgPSBzZXQoKQogICAgICAgICNwcmludCgnc3FsaXRlMyB0aHJlYWRzYWZldHk6"
    "JyxzcWxpdGUzLnRocmVhZHNhZmV0eSkKICAgICAgICAjaWYgc3FsaXRlMy50aHJlYWRzYWZldHk8"
    "MzoKICAgICAgICAjICAgIHJhaXNlIEV4Y2VwdGlvbignTXVsdGlUaHJlYWQgc3VwcG9ydCByZXF1"
    "aXJlZCcpCiAgICAgICAgI1RPRE8gb3JnYW5pemUgc2luZ2xlIHRocmVhZGVkIGRhdGFiYXNlIGFj"
    "Y2Vzcz8gZXZlciBuZWVkZWQ/CiAgICAgICAgc2VsZi5sb2NrID0gdGhyZWFkaW5nLlJMb2NrKCkK"
    "ICAgICAgICBvcy5tYWtlZGlycyhfUEFUSF9QTEFZRVJEQVRBLCBleGlzdF9vaz1UcnVlKQogICAg"
    "ICAgIHNlbGYuZGIgPSBzcWxpdGUzLmNvbm5lY3QoX1BBVEhfREFUQUJBU0UsCiAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICBjaGVja19zYW1lX3RocmVhZCA9IEZhbHNlLAogICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgZGV0ZWN0X3R5cGVzPXNxbGl0ZTMuUEFSU0VfREVD"
    "TFRZUEVTIHwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIHNxbGl0ZTMuUEFSU0Vf"
    "Q09MTkFNRVMpCiAgICAgICAgaW5pdGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICBkYlVu"
    "aW5pdGlhbGl6ZWQgPSBpbml0Y3VyLmV4ZWN1dGUoX1NRTF9kYkluZm9FeGlzdHMpLmZldGNob25l"
    "KCkgaXMgTm9uZQogICAgICAgIGlmIGRiVW5pbml0aWFsaXplZDoKICAgICAgICAgICAgZGJWZXJS"
    "ZXMgPSAwCiAgICAgICAgZWxzZToKICAgICAgICAgICAgZGJWZXJSZXMgPSBpbml0Y3VyLmV4ZWN1"
    "dGUoX1NRTF9kYlZlcnNpb24pLmZldGNob25lKClbMF0KICAgICAgICBzZWxmLnVwZGF0ZURCRnJv"
    "bShkYlZlclJlcykgI2Vuc3VyZSBEQiBpcyB1cGRhdGVkCiAgICAgICAgCiAgICAgICAgaW5pdGN1"
    "ci5jbG9zZSgpCiAgICBkZWYgZ2V0VVJhbmRvbShzZWxmKToKICAgICAgICAjSGVybyBpZHMuIFRo"
    "ZSBwcm9iZSB1c2VkIHRvIGJlIGEgYmFyZSBgcm51bSArPSAxYCwgd2hpY2ggd2Fsa3Mgc3RyYWln"
    "aHQKICAgICAgICAjcGFzdCB0aGUgdG9wIG9mIHRoZSByYW5nZSBpbnN0ZWFkIG9mIHdyYXBwaW5n"
    "IC0gc28gb24gYSBidXN5IHNlcnZlciB0aGUKICAgICAgICAjaWRzIGhhbmRlZCBvdXQgZHJpZnQg"
    "YWJvdmUgMHg4MDAwLCBhbmQgaWYgZXZlcnkgaWQgd2VyZSBldmVyIHRha2VuIHRoZQogICAgICAg"
    "ICNsb29wIHdvdWxkIG5ldmVyIGVuZC4gV3JhcCBpbnNpZGUgdGhlIHJhbmdlIGFuZCBnaXZlIHVw"
    "IGFmdGVyIGEgZnVsbAogICAgICAgICNzd2VlcCBpbnN0ZWFkIG9mIHNwaW5uaW5nIGZvcmV2ZXIg"
    "aW5zaWRlIHRoZSBsb2NrLgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBybnVt"
    "ID0gcmFuZG9tLnJhbmRpbnQoMSwgX01BWF9IRVJPX0lEKQogICAgICAgICAgICBmb3IgXyBpbiBy"
    "YW5nZShfTUFYX0hFUk9fSUQpOgogICAgICAgICAgICAgICAgaWYgcm51bSBub3QgaW4gc2VsZi51"
    "c2VkTnVtczoKICAgICAgICAgICAgICAgICAgICBzZWxmLnVzZWROdW1zLmFkZChybnVtKQogICAg"
    "ICAgICAgICAgICAgICAgIHJldHVybiBybnVtCiAgICAgICAgICAgICAgICBybnVtID0gcm51bSAl"
    "IF9NQVhfSEVST19JRCArIDEKICAgICAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKCdubyBmcmVl"
    "IGhlcm8gaWQgKHNlcnZlciBmdWxsPyknKQogICAgZGVmIHJlbGVhc2VVUmFuZG9tKHNlbGYsIG51"
    "bSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHNlbGYudXNlZE51bXMuZGlz"
    "Y2FyZChudW0pI2Rpc2NhcmQ6IHNhZmUgZXZlbiBpZiBhbHJlYWR5IHJlbGVhc2VkCiAgICBkZWYg"
    "dXBkYXRlREJGcm9tKHNlbGYsIHZlcnNpb24pOgogICAgICAgIHByaW50KCdEYXRhYmFzZSBWZXJz"
    "aW9uOicsdmVyc2lvbikKICAgICAgICBpZiB2ZXJzaW9uID49IF9EQkNVUlZFUjoKICAgICAgICAg"
    "ICAgcmV0dXJuCiAgICAgICAgcHJpbnQoJ1VwZGF0aW5nIERhdGFiYXNlIHRvIFZlcnNpb24nLF9E"
    "QkNVUlZFUikKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgdXBkY3VyID0gc2Vs"
    "Zi5kYi5jdXJzb3IoKQogICAgICAgICAgICBpZiB2ZXJzaW9uID09IDA6CiAgICAgICAgICAgICAg"
    "ICB1cGRjdXIuZXhlY3V0ZShfU1FMSU5JVF9kYkluZm9UYWJsZSkKICAgICAgICAgICAgICAgIHVw"
    "ZGN1ci5leGVjdXRlKF9TUUxJTklUX2RiSW5mb1ZlcnNpb24pCiAgICAgICAgICAgICAgICB1cGRj"
    "dXIuZXhlY3V0ZShfU1FMSU5JVF9kYlVzZXJUYWJsZSkKICAgICAgICAgICAgICAgIHVwZGN1ci5l"
    "eGVjdXRlKF9TUUxJTklUX2RiRm9ybVRhYmxlKQogICAgICAgICAgICBpZiB2ZXJzaW9uIDwgMjoK"
    "ICAgICAgICAgICAgICAgICNHdWlsZCBzdG9yYWdlLiBBZGRpdGl2ZSBvbmx5LCBzbyBhbiBleGlz"
    "dGluZyB2MSBkYXRhYmFzZSB3aXRoCiAgICAgICAgICAgICAgICAjcmVhbCBhY2NvdW50cyBpbiBp"
    "dCB1cGdyYWRlcyBpbiBwbGFjZS4KICAgICAgICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9TUUxJ"
    "TklUX2RiR3VpbGRUYWJsZSkKICAgICAgICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9TUUxJTklU"
    "X2RiR3VpbGRNZW1iZXJUYWJsZSkKICAgICAgICAgICAgI1RoZSB2ZXJzaW9uIHJvdyB3YXMgb25s"
    "eSBldmVyIHdyaXR0ZW4gYnkgdGhlIHZlcnNpb249PTAgYnJhbmNoLCBzbwogICAgICAgICAgICAj"
    "ZXZlcnkgbGF0ZXIgbWlncmF0aW9uIHdvdWxkIGhhdmUgcmUtcnVuIG9uIHRoZSBuZXh0IHN0YXJ0"
    "LgogICAgICAgICAgICB1cGRjdXIuZXhlY3V0ZShfU1FMVVBEX2RiSW5mb1ZlcnNpb24pCiAgICAg"
    "ICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgdXBkY3VyLmNsb3NlKCkKICAgIGRl"
    "ZiBnZXRQREZOKHNlbGYsIG5hbWUsIGZvcm0sIGNyZWF0ZSk6CiAgICAgICAgd2l0aCBzZWxmLmxv"
    "Y2s6CiAgICAgICAgICAgIGZvcm1jdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHVp"
    "ZHJlcyA9IGZvcm1jdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKG5hbWUsICkpLmZldGNob25lKCkK"
    "ICAgICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICBmb3JtY3VyLmNs"
    "b3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNVc2VyIGRvZXNuJ3QgZXhpc3QKICAg"
    "ICAgICAgICAgZmlkcmVzID0gZm9ybWN1ci5leGVjdXRlKF9TUUxfZm9ybUlELCAoZm9ybSwgKSku"
    "ZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiBmaWRyZXMgaXMgTm9uZTogI2Zvcm1hdCBkb2VzIG5v"
    "dCBleGlzdAogICAgICAgICAgICAgICAgaWYgbm90IGNyZWF0ZToKICAgICAgICAgICAgICAgICAg"
    "ICBmb3JtY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjTmV3IGZv"
    "cm1hdCBub3QgY3JlYXRlZAogICAgICAgICAgICAgICAgZm9ybWN1ci5leGVjdXRlKF9TUUxBRERf"
    "Zm9ybUlELCAoZm9ybSwgKSkKICAgICAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkjVE9ETyBD"
    "aGVjayBpZiBnb3R0YSBjb21taXQgYmVmb3JlIHJlYWQtYmFjaz8KICAgICAgICAgICAgICAgIGZp"
    "ZHJlcyA9IGZvcm1jdXIuZXhlY3V0ZShfU1FMX2Zvcm1JRCwgKGZvcm0sICkpLmZldGNob25lKCkK"
    "ICAgICAgICAgICAgZm9ybWN1ci5jbG9zZSgpCiAgICAgICAgICAgIGZpZCA9IGZpZHJlc1swXQog"
    "ICAgICAgICAgICB1aWQgPSB1aWRyZXNbMF0KICAgICAgICAgICAgZmlsZW5hbWUgPSBfRk9STV9Q"
    "REZpbGUuZm9ybWF0KHVpZCwgZmlkKQogICAgICAgICAgICBmcGF0aCA9IG9zLnBhdGguam9pbihf"
    "UEFUSF9QTEFZRVJEQVRBLCBmaWxlbmFtZSkKICAgICAgICAgICAgaWYgb3MucGF0aC5leGlzdHMo"
    "ZnBhdGgpIG9yIGNyZWF0ZToKICAgICAgICAgICAgICAgIHJldHVybiBmcGF0aAogICAgICAgICAg"
    "ICByZXR1cm4gTm9uZQogICAgZGVmIGdldFBsYXllckRhdGEoc2VsZiwgbmFtZSwgZm9ybSk6CiAg"
    "ICAgICAgcGF0aCA9IHNlbGYuZ2V0UERGTihuYW1lLCBmb3JtLCBGYWxzZSkKICAgICAgICBpZiBu"
    "b3QgcGF0aDoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAgIHJldHVybiByZWFkQmluKHBh"
    "dGgpI1RPRE8gZGVmYXVsdCB0byBiJycgb24gZXJyb3I/CiAgICBkZWYgc2V0UGxheWVyRGF0YShz"
    "ZWxmLCBuYW1lLCBmb3JtLCBkYXRhKToKICAgICAgICBwYXRoID0gc2VsZi5nZXRQREZOKG5hbWUs"
    "IGZvcm0sIFRydWUpCiAgICAgICAgaWYgbm90IHBhdGg6I05PIEZJTEUgUEFUSCwgVE9ETyBDQVRD"
    "SCBFUlJPUgogICAgICAgICAgICByZXR1cm4KICAgICAgICAjV3JpdHRlbiB0byBhIHRlbXAgZmls"
    "ZSBhbmQgbW92ZWQgaW50byBwbGFjZSwgbm90IHdyaXR0ZW4gaW4gcGxhY2UuCiAgICAgICAgI1Ro"
    "ZSBnYW1lIGNhbGxzIC9zZXRwbGF5ZXJkYXRhIHRvIGF1dG9zYXZlIG1pZC1zZXNzaW9uLCBub3Qg"
    "b25seSBvbiBhCiAgICAgICAgI2NsZWFuIGV4aXQgLSB0aGUgbGl2ZSBsb2dzIHNob3cgaXQgZmly"
    "aW5nIHdoaWxlIGEgcGxheWVyIGlzIHdhbGtpbmcKICAgICAgICAjYXJvdW5kLCB3ZWxsIGJlZm9y"
    "ZSAvbGVhdmVnYW1lLiBgb3BlbihwYXRoLCd3YicpYCB0cnVuY2F0ZXMgdGhlIHNhdmUKICAgICAg"
    "ICAjdG8gemVybyBieXRlcyAqYmVmb3JlKiB3cml0aW5nIGEgc2luZ2xlIGJ5dGUgb2YgdGhlIG5l"
    "dyBvbmU6IGEgY3Jhc2gsCiAgICAgICAgI2Ega2lsbGVkIHByb2Nlc3Mgb3IgYSBsb3N0IGNvbm5l"
    "Y3Rpb24gYXQgZXhhY3RseSB0aGUgd3JvbmcgaW5zdGFudAogICAgICAgICNsZWZ0IGEgMC1ieXRl"
    "IG9yIGhhbGYtd3JpdHRlbiBzYXZlLCBhbmQgZ2V0UGxheWVyRGF0YSgpIHRoZW4gaGFuZGVkCiAg"
    "ICAgICAgI3RoYXQgYmFjayBhcyAieW91ciBjaGFyYWN0ZXIncyBkYXRhIiBvbiB0aGUgbmV4dCBs"
    "b2dpbiAtIHRoaXMgaXMKICAgICAgICAjYWxtb3N0IGNlcnRhaW5seSB0aGUgInByb2dyZXNzIGdl"
    "dHMgbG9zdCIgcmVwb3J0LiBvcy5yZXBsYWNlKCkgaXMKICAgICAgICAjYXRvbWljIG9uIGJvdGgg"
    "V2luZG93cyBhbmQgUE9TSVg6IHRoZSBmaWxlIG9uIGRpc2sgaXMgZWl0aGVyIHRoZQogICAgICAg"
    "ICNjb21wbGV0ZSBvbGQgc2F2ZSBvciB0aGUgY29tcGxldGUgbmV3IG9uZSwgbmV2ZXIgYSBwYXJ0"
    "aWFsIHdyaXRlLgogICAgICAgIHRtcCA9IHBhdGggKyBmJy57b3MuZ2V0cGlkKCl9Lnt0aHJlYWRp"
    "bmcuZ2V0X2lkZW50KCl9LnRtcCcKICAgICAgICB0cnk6CiAgICAgICAgICAgIHdpdGggb3Blbih0"
    "bXAsICd3YicpIGFzIGY6CiAgICAgICAgICAgICAgICBmLndyaXRlKGRhdGEpCiAgICAgICAgICAg"
    "ICAgICBmLmZsdXNoKCkKICAgICAgICAgICAgICAgIG9zLmZzeW5jKGYuZmlsZW5vKCkpCiAgICAg"
    "ICAgICAgIG9zLnJlcGxhY2UodG1wLCBwYXRoKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAg"
    "ICAgICAgICB0cnk6CiAgICAgICAgICAgICAgICBvcy5yZW1vdmUodG1wKQogICAgICAgICAgICBl"
    "eGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgICAgIHBhc3MKICAgICAgICAgICAgcmFpc2UKICAg"
    "IGRlZiBnZXRXaG9pcyhzZWxmLCBuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgd2N1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcmVzID0gd2N1ci5leGVj"
    "dXRlKF9TUUxfZ2V0V2hvaXMsIChuYW1lLCkpLmZldGNob25lKCkKICAgICAgICAgICAgd2N1ci5j"
    "bG9zZSgpCiAgICAgICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAgICAgICAgICAgICAgcmV0dXJu"
    "IE5vbmUKICAgICAgICAgICAgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRlc2NyaXB0"
    "aW9uKSA9IHJlcwogICAgICAgICAgICBjdXJZZWFyID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCku"
    "eWVhcgogICAgICAgICAgICBhZ2UgPSBtYXgoMCwgY3VyWWVhciAtIHlvYikgaWYgeW9iIGVsc2Ug"
    "MAogICAgICAgICAgICByZXR1cm4gewogICAgICAgICAgICAgICAgJ2VtYWlsJzogZW1haWwgb3Ig"
    "JycsCiAgICAgICAgICAgICAgICAnbG9jYXRpb24nOiBsb2NhdGlvbiBvciAnJywKICAgICAgICAg"
    "ICAgICAgICdhZ2UnOiBhZ2UsCiAgICAgICAgICAgICAgICAnZ2VuZGVyJzogZ2VuZGVyIGlmIGdl"
    "bmRlciBpcyBub3QgTm9uZSBlbHNlIDAsCiAgICAgICAgICAgICAgICAnZGVzY3JpcHRpb24nOiBk"
    "ZXNjcmlwdGlvbiBvciAnJwogICAgICAgICAgICB9CiAgICBkZWYgdXBkYXRlV2hvaXMoc2VsZiwg"
    "bmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgogICAgICAg"
    "IHRyeToKICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAgICBleGNlcHQgKFR5cGVFcnJv"
    "ciwgVmFsdWVFcnJvcik6CiAgICAgICAgICAgIGFnZSA9IDAKICAgICAgICB0cnk6CiAgICAgICAg"
    "ICAgIGdlbmRlciA9IGludChnZW5kZXIpCiAgICAgICAgZXhjZXB0IChUeXBlRXJyb3IsIFZhbHVl"
    "RXJyb3IpOgogICAgICAgICAgICBnZW5kZXIgPSAwCiAgICAgICAgeW9iID0gZGF0ZXRpbWUuZGF0"
    "ZXRpbWUubm93KCkueWVhciAtIGFnZQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAg"
    "ICB3Y3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICB3Y3VyLmV4ZWN1dGUoX1NRTFVQ"
    "RF93aG9pcywgKGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRlc2NyaXB0aW9uLCBuYW1l"
    "KSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICB3Y3VyLmNsb3NlKCkK"
    "ICAgICMjIEdVSUxEUwogICAgZGVmIGdldEd1aWxkT2Yoc2VsZiwgdXNlcm5hbWUpOgogICAgICAg"
    "ICMtPiAoZ3VpbGRuYW1lLCByYW5rKSBvciAoTm9uZSwgMCkKICAgICAgICB3aXRoIHNlbGYubG9j"
    "azoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBj"
    "dXIuZXhlY3V0ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5hbWUsKSkuZmV0Y2hvbmUoKQogICAg"
    "ICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIGlmIHJlcyBpcyBOb25lOgogICAgICAgICAgICBy"
    "ZXR1cm4gKE5vbmUsIDApCiAgICAgICAgcmV0dXJuIChyZXNbMF0sIHJlc1sxXSBvciAwKQogICAg"
    "ZGVmIGdldEd1aWxkTmFtZShzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgcmV0dXJuIHNlbGYuZ2V0"
    "R3VpbGRPZih1c2VybmFtZSlbMF0gb3IgJycKICAgIGRlZiBnZXRHdWlsZE1lbWJlcnMoc2VsZiwg"
    "Z3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2Vs"
    "Zi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkTWVt"
    "YmVycywgKGd1aWxkbmFtZSwpKS5mZXRjaGFsbCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAg"
    "ICAgICAgcmV0dXJuIFsoclswXSwgclsxXSBvciAwKSBmb3IgciBpbiByZXNdCiAgICBkZWYgZ3Vp"
    "bGRFeGlzdHMoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByb3cgPSBjdXIuZXhlY3V0"
    "ZShfU1FMX2d1aWxkRXhpc3RzLCAoKGd1aWxkbmFtZSBvciAnJykuY2FzZWZvbGQoKSwpKS5mZXRj"
    "aG9uZSgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIHJvdyBpcyBub3Qg"
    "Tm9uZQogICAgZGVmIGd1aWxkTmFtZUZyZWUoc2VsZiwgZ3VpbGRuYW1lKToKICAgICAgICAjU2Ft"
    "ZSBydWxlcyBjcmVhdGVHdWlsZCgpIGVuZm9yY2VzLCBhc2tlZCBpbiBhZHZhbmNlIC0gdGhlIGNs"
    "aWVudAogICAgICAgICNjaGVja3MgYSBuYW1lIHdpdGggL3Rlc3RjcmVhdGVndWlsZCBiZWZvcmUg"
    "aXQgd2lsbCBsZXQgdGhlIHBsYXllcgogICAgICAgICNjb25maXJtLiBBbnN3ZXJpbmcgImZyZWUi"
    "IGZvciBhIG5hbWUgY3JlYXRlR3VpbGQgd291bGQgdGhlbiByZWplY3QKICAgICAgICAjd291bGQg"
    "anVzdCBtb3ZlIHRoZSBkZWFkIGVuZCBvbmUgZGlhbG9nIGxhdGVyLgogICAgICAgIGlmIG5vdCBf"
    "UkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFtZSBvciAnJyk6CiAgICAgICAgICAgIHJl"
    "dHVybiBGYWxzZQogICAgICAgIHJldHVybiBub3Qgc2VsZi5ndWlsZEV4aXN0cyhndWlsZG5hbWUp"
    "CiAgICBkZWYgbGlzdEd1aWxkcyhzZWxmKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByb3dzID0gY3VyLmV4ZWN1"
    "dGUoX1NRTF9hbGxHdWlsZHMpLmZldGNoYWxsKCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAg"
    "ICAgICByZXR1cm4gW3JbMF0gZm9yIHIgaW4gcm93c10KICAgIGRlZiBjcmVhdGVHdWlsZChzZWxm"
    "LCBndWlsZG5hbWUsIG93bmVyLCBkZXNjcmlwdGlvbj0nJyk6CiAgICAgICAgIy0+IGd1aWxkbmFt"
    "ZSBvbiBzdWNjZXNzLCBvciBhbiBlcnJvciB0b2tlbiBmb3IgdGhlIGNsaWVudAogICAgICAgIGlm"
    "IG5vdCBfUkVfVkFMSURfR1VJTEROQU1FLm1hdGNoKGd1aWxkbmFtZSBvciAnJyk6CiAgICAgICAg"
    "ICAgIHJldHVybiAnYmFkR3VpbGROYW1lJwogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAg"
    "ICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9T"
    "UUxfZ3VpbGRPZlVzZXIsIChvd25lciwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAg"
    "ICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnYWxyZWFkeUluR3Vp"
    "bGQnCiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRFeGlzdHMsIChndWlsZG5h"
    "bWUuY2FzZWZvbGQoKSwpKS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAg"
    "Y3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiAnZ3VpbGROYW1lVGFrZW4nCiAgICAg"
    "ICAgICAgIGN1ci5leGVjdXRlKF9TUUxfY3JlYXRlR3VpbGQsCiAgICAgICAgICAgICAgICAgICAg"
    "ICAgIChndWlsZG5hbWUsIGd1aWxkbmFtZS5jYXNlZm9sZCgpLCBvd25lciwKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgIGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpLCBzYW5pdGl6ZVRleHQoZGVzY3Jp"
    "cHRpb24pKSkKICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9hZGRHdWlsZE1lbWJlciwgKGd1"
    "aWxkbmFtZSwgb3duZXIsIDIpKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAg"
    "ICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBqb2luR3VpbGQoc2Vs"
    "ZiwgZ3VpbGRuYW1lLCB1c2VybmFtZSk6CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAg"
    "ICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcm93ID0gY3VyLmV4ZWN1dGUo"
    "X1NRTF9ndWlsZEV4aXN0cywgKChndWlsZG5hbWUgb3IgJycpLmNhc2Vmb2xkKCksKSkuZmV0Y2hv"
    "bmUoKQogICAgICAgICAgICBpZiByb3cgaXMgTm9uZToKICAgICAgICAgICAgICAgIGN1ci5jbG9z"
    "ZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gJ3Vua25vd25HdWlsZCcKICAgICAgICAgICAgI1N0"
    "b3JlIHRoZSBndWlsZCdzIG93biBzcGVsbGluZywgbm90IHdoYXRldmVyIGNhc2UgdGhlIGNsaWVu"
    "dCB0eXBlZAogICAgICAgICAgICAjaW50byB0aGUgam9pbiBib3gsIHNvIGdldEd1aWxkTWVtYmVy"
    "cygpIGZpbmRzIHRoZSBtZW1iZXIgYmFjay4KICAgICAgICAgICAgZ3VpbGRuYW1lID0gcm93WzBd"
    "CiAgICAgICAgICAgIGlmIGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRPZlVzZXIsICh1c2VybmFtZSwp"
    "KS5mZXRjaG9uZSgpIGlzIG5vdCBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAg"
    "ICAgICAgICAgICAgIHJldHVybiAnYWxyZWFkeUluR3VpbGQnCiAgICAgICAgICAgIGN1ci5leGVj"
    "dXRlKF9TUUxfYWRkR3VpbGRNZW1iZXIsIChndWlsZG5hbWUsIHVzZXJuYW1lLCAwKSkKICAgICAg"
    "ICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIHJl"
    "dHVybiBOb25lCiAgICBkZWYgbGVhdmVHdWlsZChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgd2l0"
    "aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAg"
    "ICAgcmVzID0gY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwgKHVzZXJuYW1lLCkpLmZldGNo"
    "b25lKCkKICAgICAgICAgICAgaWYgcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xv"
    "c2UoKQogICAgICAgICAgICAgICAgcmV0dXJuICdub3RJbkd1aWxkJwogICAgICAgICAgICAoZ3Vp"
    "bGRuYW1lLCByYW5rKSA9IChyZXNbMF0sIHJlc1sxXSBvciAwKQogICAgICAgICAgICBjdXIuZXhl"
    "Y3V0ZShfU1FMX2RlbEd1aWxkTWVtYmVyLCAodXNlcm5hbWUsKSkKICAgICAgICAgICAgb3duZXIg"
    "PSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkT3duZXIsIChndWlsZG5hbWUsKSkuZmV0Y2hvbmUoKQog"
    "ICAgICAgICAgICBpZiBvd25lciBhbmQgb3duZXJbMF0gPT0gdXNlcm5hbWU6CiAgICAgICAgICAg"
    "ICAgICAjVGhlIGZvdW5kZXIgbGVhdmluZyBkaXNzb2x2ZXMgdGhlIGd1aWxkIHJhdGhlciB0aGFu"
    "IGxlYXZpbmcgYW4KICAgICAgICAgICAgICAgICNvd25lcmxlc3MgcmVjb3JkIHRoYXQgbm9ib2R5"
    "IGNhbiBldmVyIGFkbWluaXN0ZXIuCiAgICAgICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2Rl"
    "bEd1aWxkTWVtYmVycywgKGd1aWxkbmFtZSwpKQogICAgICAgICAgICAgICAgY3VyLmV4ZWN1dGUo"
    "X1NRTF9kZWxldGVHdWlsZCwgKGd1aWxkbmFtZSwpKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1p"
    "dCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBs"
    "b2dpblBsYXllcihzZWxmLCB1c2VybmFtZSwgY29uLCBwYXNzd29yZCk6I1RPRE8gc2hvdWxkIHJl"
    "dHVybiBlcnJvciBwcm9wZXJseSB0byBjbGllbnQKICAgICAgICBpZiBub3QgX1JFX1ZBTElEX1VT"
    "RVJOQU1FLm1hdGNoKHVzZXJuYW1lKToKICAgICAgICAgICAgI1JlZ2lzdHJhdGlvbiBoYXMgYWx3"
    "YXlzIHZhbGlkYXRlZCB0aGUgbmFtZTsgbG9nZ2luZyBpbiBkaWQgbm90LgogICAgICAgICAgICAj"
    "TmFtZXMgcmVhY2ggb3RoZXIgY2xpZW50cyBpbnNpZGUgcXVvdGVkIHByb3RvY29sIGZpZWxkcywg"
    "c28gYSBuYW1lCiAgICAgICAgICAgICNjb250YWluaW5nICciJyBmb3JnZXMgY29tbWFuZHMgLSBh"
    "bmQgdGhlIEFsbG93QW55TG9naW4gZGVidWcgcGF0aAogICAgICAgICAgICAjYmVsb3cgbmV2ZXIg"
    "dG91Y2hlcyB0aGUgZGF0YWJhc2UsIHdoaWNoIG1hZGUgaXQgdGhlIG9uZSB3YXkgdG8gZ2V0CiAg"
    "ICAgICAgICAgICNzdWNoIGEgbmFtZSBpbi4gQ2hlY2sgaGVyZSBzbyBib3RoIHBhdGhzIGFyZSBj"
    "b3ZlcmVkLgogICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgIGlmIF9ERUJVR19BTExPV19B"
    "TllfTE9HSU46ICNERUJVRyBBVVRPIEFMTE9XCiAgICAgICAgICAgIHJldHVybiBVc2VyKHVzZXJu"
    "YW1lLCBjb24pCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGxvZ2luQ3VyID0g"
    "c2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICAjRGVmYXVsdCB0byBTVFJJQ1QsIFRPRE8gYWxs"
    "b3cgZm9yIG5vbi1zdHJpY3Q/CiAgICAgICAgICAgIHVpZHJlcyA9IGxvZ2luQ3VyLmV4ZWN1dGUo"
    "X1NRTF91c2VySURfc3RyaWN0LCAodXNlcm5hbWUsIGNvbi5TSykpLmZldGNob25lKCkKICAgICAg"
    "ICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICAjcHJpbnQoJ2xvZ2luIGVy"
    "cm9yOiBubyB1c2VyIHdpdGggdGhhdCBzZXJpYWwga2V5JykKICAgICAgICAgICAgICAgIGxvZ2lu"
    "Q3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNObyBzdWNoIFVzZXIKICAg"
    "ICAgICAgICAgdWlkID0gdWlkcmVzWzBdCiAgICAgICAgICAgIChyVXNlciwgcGFzc2hhc2gsIHVT"
    "YWx0LCBoSXRyKSA9IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9nZXRMb2dpbiwgKHVpZCwgKSkuZmV0"
    "Y2hvbmUoKQogICAgICAgICAgICBpZiB1c2VybmFtZSAhPSByVXNlcjoKICAgICAgICAgICAgICAg"
    "ICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyB1c2VybmFtZToge3VzZXJuYW1lfScpCiAgICAg"
    "ICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAj"
    "V3JvbmcgVXNlcm5hbWUKICAgICAgICAgICAgdHBhcyA9IF9zYWx0X2hhc2hfKHBhc3N3b3JkLCB1"
    "U2FsdCwgaEl0cikKICAgICAgICAgICAgaWYgdHBhcyAhPSBwYXNzaGFzaDoKICAgICAgICAgICAg"
    "ICAgICNwcmludChmJ2xvZ2luIGVycm9yOiB3cm9uZyBwYXNzd29yZDoge3Bhc3N3b3JkfScpCiAg"
    "ICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAgICAgICAgICByZXR1cm4gTm9u"
    "ZSAjV3JvbmcgUGFzc3dvcmQKICAgICAgICAgICAgaWYgaEl0ciAhPSBfSEFTSElURVI6CiAgICAg"
    "ICAgICAgICAgICBucHNoID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHVTYWx0LCBfSEFTSElURVIp"
    "CiAgICAgICAgICAgICAgICBsb2dpbkN1ci5leGVjdXRlKF9TUUxVUERfcGFzc0hhc2gsIChucHNo"
    "LCBfSEFTSElURVIsIHVpZCkpCiAgICAgICAgICAgIHVzZXJvYmogPSBVc2VyKHVzZXJuYW1lLCBj"
    "b24pCiAgICAgICAgICAgICN1cGRhdGUgbGFzdCBsb2dpbgogICAgICAgICAgICBsb2dpbkN1ci5l"
    "eGVjdXRlKF9TUUxfbG9naW5VcGRhdGUsICh1c2Vyb2JqLmxvZ2luVGltZSwgdWlkKSkKICAgICAg"
    "ICAgICAgI1RPRE8gZGVmYXVsdCBkYXRldGltZSBhZGFwdGVyIGRlcHJlY2F0ZWQsIGNoZWNrIHJl"
    "cGxhY2VtZW50CiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgbG9naW5D"
    "dXIuY2xvc2UoKQogICAgICAgICAgICByZXR1cm4gdXNlcm9iagogICAgZGVmIHJlZ2lzdGVyUGxh"
    "eWVyKHNlbGYsIHVzZXJuYW1lLCBjb24sIHBhc3N3b3JkLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwg"
    "Z2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgaWYgbm90IF9SRV9WQUxJRF9VU0VSTkFNRS5t"
    "YXRjaCh1c2VybmFtZSk6CiAgICAgICAgICAgIHJldHVybiBOb25lICNJbnZhbGlkIHVzZXJuYW1l"
    "IChiYWQgY2hhcnMvbGVuZ3RoKSwgYWxzbyBibG9ja3MgcHJvdG9jb2wtaW5qZWN0aW9uIHZpYSAn"
    "IicKICAgICAgICBlbWFpbCA9IHNhbml0aXplVGV4dChlbWFpbCkKICAgICAgICBsb2NhdGlvbiA9"
    "IHNhbml0aXplVGV4dChsb2NhdGlvbikKICAgICAgICBkZXNjcmlwdGlvbiA9IHNhbml0aXplVGV4"
    "dChkZXNjcmlwdGlvbikKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgbG9naW5D"
    "dXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHVpZHJlcyA9IGxvZ2luQ3VyLmV4ZWN1"
    "dGUoX1NRTF91c2VySUQsICh1c2VybmFtZSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1"
    "aWRyZXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICAjcHJpbnQoZidyZWdpc3RlciBlcnJv"
    "cjogdXNlcm5hbWUgYWxyZWFkeSBpbiB1c2U6IHt1c2VybmFtZX0nKQogICAgICAgICAgICAgICAg"
    "bG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1VzZXIgZXhpc3Rz"
    "CiAgICAgICAgICAgICNpZiBzdHJpY3QsIGNoZWNrIGlmIHNlcmlhbCBpcyBpbiB1c2UgdG9vCiAg"
    "ICAgICAgICAgICNUT0RPIG9ubHkgYXBwbHkgaWYgc3RyaWN0CiAgICAgICAgICAgIHVpZHJlcyA9"
    "IGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF91c2VySURfU2NoaywgKGNvbi5TSywgKSkuZmV0Y2hvbmUo"
    "KQogICAgICAgICAgICBpZiB1aWRyZXMgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICAjcHJp"
    "bnQoJ3JlZ2lzdGVyIGVycm9yOiBzZXJpYWwgYWxyZWFkeSBpbiB1c2UnKQogICAgICAgICAgICAg"
    "ICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIE5vbmUgI1NlcmlhbCBp"
    "biB1c2UgZXhpc3RzCiAgICAgICAgICAgIHVTYWx0ID0gb3MudXJhbmRvbSgxNikKICAgICAgICAg"
    "ICAgcEhhc2ggPSBfc2FsdF9oYXNoXyhwYXNzd29yZCwgdVNhbHQsIF9IQVNISVRFUikKICAgICAg"
    "ICAgICAgY3VydGltZSA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdygpCiAgICAgICAgICAgIHRyeToj"
    "dHJ5IHNob3VsZG4ndCBiZSBuZWVkZWQgYXMgZW1wdHkgZmllbGQgaXMgc2V0IHRvIDI1NQogICAg"
    "ICAgICAgICAgICAgYWdlID0gaW50KGFnZSkKICAgICAgICAgICAgZXhjZXB0OgogICAgICAgICAg"
    "ICAgICAgYWdlID0gMAogICAgICAgICAgICB5b2IgPSBjdXJ0aW1lLnllYXIgLSBhZ2UKICAgICAg"
    "ICAgICAgcmVndmFscyA9ICgKICAgICAgICAgICAgICAgIHVzZXJuYW1lLHBIYXNoLAogICAgICAg"
    "ICAgICAgICAgY29uLlNLLHVTYWx0LF9IQVNISVRFUiwKICAgICAgICAgICAgICAgIGN1cnRpbWUs"
    "ZW1haWwsbG9jYXRpb24seW9iLGdlbmRlcixkZXNjcmlwdGlvbgogICAgICAgICAgICApCiAgICAg"
    "ICAgICAgIGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9yZWdpc3RlclVzZXIsIHJlZ3ZhbHMpCiAgICAg"
    "ICAgICAgICNUT0RPIGRlZmF1bHQgZGF0ZXRpbWUgYWRhcHRlciBkZXByZWNhdGVkLCBjaGVjayBy"
    "ZXBsYWNlbWVudAogICAgICAgICAgICB1c2Vyb2JqID0gVXNlcih1c2VybmFtZSwgY29uKQogICAg"
    "ICAgICAgICBzZWxmLmRiLmNvbW1pdCgpCiAgICAgICAgICAgIGxvZ2luQ3VyLmNsb3NlKCkKICAg"
    "ICAgICAgICAgcmV0dXJuIHVzZXJvYmoKICAgIGRlZiBuYW1lVGFrZW4oc2VsZiwgdXNlcm5hbWUp"
    "OgogICAgICAgICNEb2VzIGFuIGFjY291bnQgd2l0aCB0aGlzIG5hbWUgZXhpc3QgYXQgYWxsLCBy"
    "ZWdhcmRsZXNzIG9mIHNlcmlhbD8KICAgICAgICAjVXNlZCB0byB0ZWxsICJ0aGlzIG5hbWUgaXMg"
    "ZnJlZSBidXQgeW91ciBrZXkgZG9lcyBub3QgbWF0Y2ggdGhlCiAgICAgICAgI2FjY291bnQiIGFw"
    "YXJ0IGZyb20gInRoaXMgbmFtZSBpcyBnZW51aW5lbHkgdW51c2FibGUiLgogICAgICAgIHdpdGgg"
    "c2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAg"
    "IHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5hbWUsICkpLmZldGNob25lKCkK"
    "ICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4gcmVzIGlzIG5vdCBOb25lCiAg"
    "ICBkZWYgZGVsZXRlQWNjb3VudChzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0FkbWluLXBhbmVs"
    "IGFjdGlvbiAoR1VJICLQo9C00LDQu9C40YLRjCDQv9C10YDRgdC+0L3QsNC20LAiKTogcGVybWFu"
    "ZW50bHkgcmVtb3ZlcyBhbgogICAgICAgICNhY2NvdW50IGFuZCBldmVyeSBzYXZlZCBwbGF5ZXJk"
    "YXRhIGJsb2IgZm9yIGl0LiBJcnJldmVyc2libGUgLSB0aGUKICAgICAgICAjR1VJIGlzIGV4cGVj"
    "dGVkIHRvIGNvbmZpcm0gd2l0aCB0aGUgYWRtaW4gYmVmb3JlIGNhbGxpbmcgdGhpcy4KICAgICAg"
    "ICAjRG9lcyBOT1QgdG91Y2ggdGhlIGNhbGxlcidzIGxpdmUgY29ubmVjdGlvbi9zZXNzaW9uOyB0"
    "aGUgY2FsbGVyIGlzCiAgICAgICAgI3Jlc3BvbnNpYmxlIGZvciBraWNraW5nIGZpcnN0IGlmIHRo"
    "ZSBhY2NvdW50IGlzIGN1cnJlbnRseSBvbmxpbmUKICAgICAgICAjKHNlZSBDb3JlU2VydmVyLmRl"
    "bGV0ZUFjY291bnQpLCBvdGhlcndpc2UgYSBjb25uZWN0ZWQgY2xpZW50IHdvdWxkCiAgICAgICAg"
    "I2tlZXAgcGxheWluZyB3aXRoIGFuIGFjY291bnQgdGhhdCBubyBsb25nZXIgZXhpc3RzIGluIHRo"
    "ZSBEQi4KICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5j"
    "dXJzb3IoKQogICAgICAgICAgICB1aWRyZXMgPSBjdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKHVz"
    "ZXJuYW1lLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBOb25lOgogICAg"
    "ICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBGYWxzZQogICAg"
    "ICAgICAgICB1aWQgPSB1aWRyZXNbMF0KICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9kZWxl"
    "dGVVc2VyLCAodXNlcm5hbWUsICkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAg"
    "ICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAjR3VpbGQgbWVtYmVyc2hpcCBvdXRsaXZlcyB0aGUg"
    "dXNlclRhYmxlIHJvdyBvdGhlcndpc2UsIHNvIHRoZSBkZWxldGVkCiAgICAgICAgI25hbWUgd291"
    "bGQga2VlcCBzaG93aW5nIHVwIGluIGl0cyBndWlsZCdzIHJvc3RlciBmb3JldmVyLgogICAgICAg"
    "IHNlbGYubGVhdmVHdWlsZCh1c2VybmFtZSkKICAgICAgICAjUGxheWVyZGF0YSBmaWxlcyAoInt1"
    "c2VySUQ6eH1fe2Zvcm1JRDp4fS5iaW4iKSBsaXZlIG91dHNpZGUgdGhlIERCCiAgICAgICAgI3Ry"
    "YW5zYWN0aW9uIGFuZCBhcmUgbG9va2VkIHVwIGJ5IHByZWZpeCAtIGJlc3QgZWZmb3J0LCBhIGxl"
    "ZnRvdmVyCiAgICAgICAgI2ZpbGUgaGVyZSBpc24ndCB3b3J0aCBmYWlsaW5nIHRoZSB3aG9sZSBk"
    "ZWxldGlvbiBvdmVyLgogICAgICAgIHByZWZpeCA9IGYne3VpZDp4fV8nCiAgICAgICAgdHJ5Ogog"
    "ICAgICAgICAgICBmb3IgZm4gaW4gb3MubGlzdGRpcihfUEFUSF9QTEFZRVJEQVRBKToKICAgICAg"
    "ICAgICAgICAgIGlmIGZuLnN0YXJ0c3dpdGgocHJlZml4KToKICAgICAgICAgICAgICAgICAgICB0"
    "cnk6CiAgICAgICAgICAgICAgICAgICAgICAgIG9zLnJlbW92ZShvcy5wYXRoLmpvaW4oX1BBVEhf"
    "UExBWUVSREFUQSwgZm4pKQogICAgICAgICAgICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAg"
    "ICAgICAgICAgICAgICAgICAgICBwYXNzCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAg"
    "ICAgIHBhc3MKICAgICAgICByZXR1cm4gVHJ1ZQpHREggPSBEYXRhSGFuZGxlcigpCgpkZWYgX3dv"
    "VXNlcih1bCwgdXNyKToKICAgIHJldHVybiBsaXN0KCAoYSBmb3IgYSBpbiB1bCBpZiBhIGlzIG5v"
    "dCB1c3IpICkKZGVmIF9SZWFkQmxvYihjb24sIHNpemUpOgogICAgI3NpemUgY29tZXMgc3RyYWln"
    "aHQgb2ZmIHRoZSB3aXJlLCBzbyBpdCBpcyBuZWl0aGVyIHRydXN0ZWQgdG8gYmUgYSBudW1iZXIK"
    "ICAgICNub3IgdG8gYmUgc2FuZTogYSBjbGllbnQgY2xhaW1pbmcgYSBodWdlIGxlbmd0aCB1c2Vk"
    "IHRvIG1ha2UgdGhlIHNlcnZlcgogICAgI2J1ZmZlciB1bmJvdW5kZWRseSAobWVtb3J5IGV4aGF1"
    "c3Rpb24pLCBhbmQgYSBjbGllbnQgdGhhdCBkaXNjb25uZWN0ZWQKICAgICNtaWQtYmxvYiBtYWRl"
    "IHJlY3YoKSByZXR1cm4gYicnIGZvcmV2ZXIgLSBhIDEwMCUgQ1BVIGJ1c3ktbG9vcCwgdGhlIHNh"
    "bWUKICAgICNkZWZlY3QgYWxyZWFkeSBmaXhlZCBpbiBDb25uZWN0aW9uSGFuZGxlci5fcmVjdk1v"
    "cmUoKS4KICAgIHRyeToKICAgICAgICBzaXplID0gaW50KHNpemUpCiAgICBleGNlcHQgKFR5cGVF"
    "cnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2JhZCBibG9i"
    "IHNpemUge3NpemUhcn0nKQogICAgaWYgc2l6ZSA8IDAgb3Igc2l6ZSA+IF9NQVhfQkxPQjoKICAg"
    "ICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmxvYiBzaXplIHtzaXplfSBvdXQgb2YgcmFuZ2Ug"
    "KG1heCB7X01BWF9CTE9CfSknKQogICAgI0EgYmxvYiByZWFkIGJsb2NrcyB0aGlzIGNvbm5lY3Rp"
    "b24ncyBlbnRpcmUgaGFuZGxlciB0aHJlYWQuIEFubm91bmNpbmcgYQogICAgI2xlbmd0aCBhbmQg"
    "dGhlbiBnb2luZyBxdWlldCAtIGEgd2VkZ2VkIGNsaWVudCwgYSBsaW5rIHRoYXQgZHJvcHBlZAog"
    "ICAgI3dpdGhvdXQgYSByZXNldCAtIHVzZWQgdG8gYmxvY2sgaXQgZm9yZXZlcjogdGhlIHRocmVh"
    "ZCBuZXZlciByZXR1cm5lZCwgc28KICAgICN0aGUgcGxheWVyJ3MgYWNjb3VudCBzdGF5ZWQgY2xh"
    "aW1lZCBhbmQgYW55IHJvb20gdGhleSBob3N0ZWQgc3RheWVkCiAgICAjbGlzdGVkIHdpdGggbm90"
    "aGluZyBiZWhpbmQgaXQuIFRoZSBpZGxlIHRpbWVvdXQgbmV2ZXIgYXBwbGllZCBoZXJlLAogICAg"
    "I2JlY2F1c2UgaXQgaXMgb25seSBjb25zdWx0ZWQgYnkgdGhlIHJlYWQgbG9vcCB0aGlzIGNhbGwg"
    "aGFzIHN0ZXBwZWQgb3V0CiAgICAjb2YuCiAgICBkZWFkbGluZSA9IHRpbWUubW9ub3RvbmljKCkg"
    "KyBfQkxPQl9USU1FT1VUCiAgICB3aGlsZSBsZW4oY29uLmRhdGEpIDwgc2l6ZToKICAgICAgICBy"
    "ZW1haW5pbmcgPSBkZWFkbGluZSAtIHRpbWUubW9ub3RvbmljKCkKICAgICAgICBpZiByZW1haW5p"
    "bmcgPD0gMDoKICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2Jsb2Igb2Yge3NpemV9"
    "IGJ5dGVzIG5vdCBkZWxpdmVyZWQgd2l0aGluICcKICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICBmJ3tfQkxPQl9USU1FT1VUfXMgKHtsZW4oY29uLmRhdGEpfSByZWNlaXZlZCknKQogICAg"
    "ICAgICNzZWxlY3QoKSwgTk9UIHNldHRpbWVvdXQoKS4gQSBzb2NrZXQgdGltZW91dCBpcyBhIHBy"
    "b3BlcnR5IG9mIHRoZQogICAgICAgICNzb2NrZXQgcmF0aGVyIHRoYW4gb2YgdGhlIGNhbGwsIHNv"
    "IHRoZSBzZXR0aW1lb3V0KCkgdGhhdCB1c2VkIHRvIGJlCiAgICAgICAgI2hlcmUgYWxzbyBhcm1l"
    "ZCB0aGUgd3JpdGVyIHRocmVhZCdzIGNvbmN1cnJlbnQgc2VuZGFsbCgpIC0gYW5kIG5vdGhpbmcK"
    "ICAgICAgICAjZXZlciBkaXNhcm1lZCBpdCBhZ2Fpbiwgc28gaXQgc3RheWVkIGFybWVkIGZvciB0"
    "aGUgd2hvbGUgcmVtYWluaW5nIGxpZmUKICAgICAgICAjb2YgdGhlIGNvbm5lY3Rpb24uIEEgY2xp"
    "ZW50IHdob3NlIHJlY2VpdmUgd2luZG93IGZpbGxlZCB1cCBmb3IgYSBtb21lbnQKICAgICAgICAj"
    "KHByZWNpc2VseSB3aGF0IGhhcHBlbnMgaW4gYSBidXN5IGNvLW9wIHNlc3Npb24pIHRoZW4gbWFk"
    "ZSB0aGF0CiAgICAgICAgI3NlbmRhbGwoKSByYWlzZSBUaW1lb3V0RXJyb3IgKmFmdGVyIGhhdmlu"
    "ZyBhbHJlYWR5IHdyaXR0ZW4gcGFydCBvZiBhCiAgICAgICAgI3BhY2tldCo6IHRoZSB3cml0ZXIg"
    "dGhyZWFkIGRpZWQsIHRoZSBjbGllbnQgd2FzIGxlZnQgaG9sZGluZyBoYWxmIGEKICAgICAgICAj"
    "bWVzc2FnZSwgYW5kIGl0cyBjb21tYW5kIHN0cmVhbSB3YXMgZGVzeW5jaHJvbmlzZWQgZnJvbSB0"
    "aGF0IHBvaW50IG9uLgogICAgICAgICNUaGUgdmlzaWJsZSByZXN1bHQgaXMgYSBmcmVlemUgb3Ig"
    "YSBkcm9wIG1pbnV0ZXMgbGF0ZXIsIHdpdGggbm90aGluZyBpbgogICAgICAgICN0aGUgbG9nIHR5"
    "aW5nIGl0IGJhY2sgdG8gdGhlIGJsb2IgdGhhdCBhcm1lZCB0aGUgdGltZW91dC4gRXZlcnkKICAg"
    "ICAgICAjYmxvYi1jYXJyeWluZyBjb21tYW5kIGlzIG9uIHRoaXMgcGF0aCAtIC9zZXR1c2VyaGVy"
    "b2RhdGEsIHRoZQogICAgICAgICMvc2V0cGxheWVyZGF0YSBhdXRvc2F2ZSwgYW5kIC9nYW1lY29t"
    "bWFuZHRvdXNlciwgd2hpY2ggaXMgdGhlIHJlbGF5CiAgICAgICAgI2NhcnJ5aW5nIHRoZSBhY3R1"
    "YWwgaW4tZ2FtZSB0cmFmZmljIGJldHdlZW4gcGxheWVycy4gX2xvYmJ5SGFuZGxlCiAgICAgICAg"
    "I2FscmVhZHkgZG9jdW1lbnRzIHRoaXMgc2FtZSB0cmFwIGZvciB0aGUgcmVhZCBsb29wOyB0aGUg"
    "bG9vcCBiZWxvdwogICAgICAgICNzaW1wbHkgbGVhdmVzIHRoZSBzb2NrZXQgYmxvY2tpbmcgYW5k"
    "IHdhaXRzIHdpdGggc2VsZWN0KCkgaW5zdGVhZC4KICAgICAgICByZWFkeSwgXywgXyA9IHNlbGVj"
    "dC5zZWxlY3QoW2Nvbi5yZXF1ZXN0XSwgW10sIFtdLCByZW1haW5pbmcpCiAgICAgICAgaWYgbm90"
    "IHJlYWR5OgogICAgICAgICAgICBjb250aW51ZSAjZGVhZGxpbmUgaXMgcmUtY2hlY2tlZCBhdCB0"
    "aGUgdG9wIG9mIHRoZSBsb29wCiAgICAgICAgY2h1bmsgPSBjb24ucmVxdWVzdC5yZWN2KFJFQ1Zf"
    "QlVGX0xFTikKICAgICAgICBpZiBub3QgY2h1bms6CiAgICAgICAgICAgIHJhaXNlIENvbm5lY3Rp"
    "b25SZXNldEVycm9yKCdkaXNjb25uZWN0ZWQgZHVyaW5nIGJsb2IgcmVhZCcpCiAgICAgICAgY29u"
    "LmRhdGEgKz0gY2h1bmsKICAgIGJsYnVmID0gY29uLmRhdGFbMDpzaXplXQogICAgY29uLmRhdGEg"
    "PSBjb24uZGF0YVtzaXplOl0KICAgIHJldHVybiBibGJ1ZgoKI0NvbW1hbmQgZnVuY3Rpb25zCl9S"
    "RV9IRVJPX1BPUyA9IHJlLmNvbXBpbGUocideWzAtOUEtRmEtZl17MSw4fSNbMC05QS1GYS1mXXsx"
    "LDh9JCcpCmRlZiBfaGVyb1BvcyhyYXcpOgogICAgIy0+ICJ4eHh4I3l5eXkiIG9yIE5vbmUuCiAg"
    "ICAjIFRoZSBjbGllbnQgc2VuZHMgZWl0aGVyICJ4eHh4I3l5eXkiIG9yICJVSUQjeHh4eCN5eXl5"
    "IiwgYnV0IHVwZGF0ZVBvcygpCiAgICAjIHVuY29uZGl0aW9uYWxseSBwcmVmaXhlcyB0aGUgc2Vu"
    "ZGVyJ3MgaWQgd2hlbiBpdCBmYW5zIHRoZSBwb3NpdGlvbiBvdXQuCiAgICAjIFN0b3JpbmcgdGhl"
    "IHJhdyBmaWVsZCBtZWFudCB0aGUgc2Vjb25kIGZvcm0gd2VudCBiYWNrIG91dCBhcwogICAgIyAi"
    "VUlEI1VJRCN4eHh4I3l5eXkiLCB3aGljaCBubyBjbGllbnQgY2FuIG1hdGNoIHRvIGEgcGxheWVy"
    "OiB0aGF0IGhlcm8ncwogICAgIyBtYXJrZXIgdGhlbiBzdGF5ZWQgd2hlcmV2ZXIgaXQgd2FzIGxh"
    "c3Qgc3VjY2Vzc2Z1bGx5IHBhcnNlZCB3aGlsZSB0aGUKICAgICMgcGxheWVyIGFjdHVhbGx5IHdh"
    "bGtlZCBhd2F5LiBLZWVwIG9ubHkgdGhlIHRyYWlsaW5nIGNvb3JkaW5hdGUgcGFpciBzbwogICAg"
    "IyBleGFjdGx5IG9uZSBpZCBpcyBwcmVzZW50IG9uIHRoZSB3aXJlIHJlZ2FyZGxlc3Mgb2Ygd2hh"
    "dCB3YXMgc2VudC4KICAgICMgQW55dGhpbmcgdGhhdCBpcyBub3QgYSBwYWlyIG9mIGhleCBudW1i"
    "ZXJzIGlzIGRpc2NhcmRlZCByYXRoZXIgdGhhbgogICAgIyBzdG9yZWQ6IHRoaXMgdmFsdWUgaXMg"
    "Y29waWVkIHZlcmJhdGltIGludG8gYSBicm9hZGNhc3QgZXZlcnkgb3RoZXIgY2xpZW50CiAgICAj"
    "IGluIHRoZSB0b3duIGhhcyB0byBwYXJzZSwgc28gYSBzaW5nbGUganVuayBmaWVsZCBmcm9tIG9u"
    "ZSBjbGllbnQKICAgICMgKGEgdHJ1bmNhdGVkIHBhY2tldCwgYSBtb2RpZmllZCBjbGllbnQpIGJl"
    "Y2FtZSBldmVyeW9uZSBlbHNlJ3MgcHJvYmxlbS4KICAgIHBvcyA9ICcjJy5qb2luKHN0cihyYXcp"
    "LnNwbGl0KCcjJylbLTI6XSkKICAgIHJldHVybiBwb3MgaWYgX1JFX0hFUk9fUE9TLm1hdGNoKHBv"
    "cykgZWxzZSBOb25lCmRlZiBfbm9wKG1kLHVzcixyZXMpOgogICAgcmV0dXJuIE5vbmUKZGVmIF91"
    "cGRoZXJvcG9zKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgog"
    "ICAgICAgIHJldHVybiBOb25lICNub3QgaW4gYSBnYW1lIGNoYW5uZWwsIGlnbm9yZQogICAgcG9z"
    "ID0gX2hlcm9Qb3MocmVzWzFdKQogICAgaWYgcG9zIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5v"
    "bmUgI3VucGFyc2VhYmxlIGNvb3JkaW5hdGVzLCBzZWUgX2hlcm9Qb3MKICAgIHVzci51c2VyLnBv"
    "c2RhdGEgPSBwb3MKICAgIHVzci51c2VyLmdhbWVjaGFubmVsLmRpcnR5ID0gVHJ1ZQogICAgdXNy"
    "LnVzZXIucG9zY2hhbmdlZCA9IFRydWUKICAgIHJldHVybiBOb25lICNubyByZXNwb25zZQpkZWYg"
    "X3NldHBsYXllcmRhdGEobWQsdXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1c3IsIHJlc1sz"
    "XSkKICAgICNUT0RPIENIRUNLIHBlcm1pc3Npb25zIGZvciBzZXREYXRhKHNlbGYgb3Igb3RoZXIp"
    "CiAgICBpZiByZXNbMV0gPT0gdXNyLnVzZXIubmFtZToKICAgICAgICBHREguc2V0UGxheWVyRGF0"
    "YShyZXNbMV0sIHJlc1syXSwgcGQpCiAgICAjVE9ETyBoYW5kbGUgcmVtYWluaW5nIHZhbHVlcwog"
    "ICAgI3Jlc1t4XToKICAgICMwOiAvc2V0cGxheWVyZGF0YQogICAgIzE6IG5hbWUKICAgICMyOiBm"
    "b3JtCiAgICAjMzogYmxvYnNpemUKICAgICM0OiB1bmtub3duIChwb2ludHM/KQogICAgIzU6IHVu"
    "a25vd24sIDEgKGJvb2w/KQogICAgcmV0dXJuIE5vbmUKZGVmIF9nZXRwbGF5ZXJkYXRhKG1kLHVz"
    "cixyZXMpOgogICAgI1RPRE8gY2hlY2sgcGVybWlzc2lvbiBmb3IgZ2V0RGF0YShzZWxmIG9yIG90"
    "aGVyKQogICAgaWYgcmVzWzFdID09IHVzci51c2VyLm5hbWU6CiAgICAgICAgcGQgPSBHREguZ2V0"
    "UGxheWVyRGF0YShyZXNbMV0sIHJlc1syXSkKICAgICAgICAjcHJpbnQoJ09idGFpbmVkIFBsYXll"
    "cmRhdGEnLCBsZW4ocGQpKQogICAgICAgIHJldHVybiBfZW0oZicvZ2V0cGxheWVyZGF0YSAie3Jl"
    "c1sxXX0iICJ7cmVzWzJdfSIge2xlbihwZCl9JykrcGQKICAgICNwcmludCgnQWNjZXNzIEVycm9y"
    "Jyx1c3IudXNlci5uYW1lLCAnQ2FuXCd0IGdldCBwbGF5ZXJkYXRhIGZvcicscmVzWzFdKQogICAg"
    "cmV0dXJuIE5vbmUKZGVmIF9sZWF2ZWdhbWVjaGFubmVsKG1kLHVzcixyZXMpOgogICAgY2hubCA9"
    "IHVzci51c2VyLmdhbWVjaGFubmVsCiAgICBpZiBjaG5sOgogICAgICAgIGNobmwubGVhdmVDaGFu"
    "bmVsKHVzcikKICAgIHJldHVybiB1c3Iuc2VydmVyLnN0YXRlLmVudW1lcmF0ZUdDKCkKIy0tLSBj"
    "b21tYW5kcyB0YWtlbiBmcm9tIHRoZSBjbGllbnQncyBvd24gb3V0Z29pbmcgdGFibGUgLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0KI1RoZSBmaXZlIGhhbmRsZXJzIGJlbG93IGV4aXN0IGJlY2F1c2Ug"
    "dGhlIGZvcm1hdCB0YWJsZSBjb21waWxlZCBpbnRvIHRoZSByZXRhaWwKI2NsaWVudCAoRU5DbGll"
    "bnQuY3BwLCByZWNvdmVyZWQgZnJvbSBHYW1lSGVscGVyLmRsbCBpbiB0aGUgMS4zIFNESykgbGlz"
    "dHMgdGhlbQojYW5kIHRoaXMgc2VydmVyIGhhZCBubyBlbnRyeSBmb3IgYW55IG9mIHRoZW0uIEFu"
    "IHVucmVnaXN0ZXJlZCBjb21tYW5kIGlzIG5vdAojaWdub3JlZCBncmFjZWZ1bGx5OiBwYXJzZSgp"
    "IGxvZ3MgJ1VOS05PV04gQ09NTUFORCcgYW5kIHJldHVybnMgbm90aGluZywgYW5kIGEKI2NsaWVu"
    "dCB3YWl0aW5nIG9uIGFuIGFuc3dlciB3YWl0cyBmb3JldmVyLiBUaGF0IGlzIHRoZSBzYW1lIHNo"
    "YXBlIGFzIGV2ZXJ5IGhhbmcKI2FscmVhZHkgdHJhY2tlZCBkb3duIGluIHRoaXMgZmlsZS4KI1Ro"
    "ZSBjbGllbnQgc2VuZHMsIHZlcmJhdGltIGZyb20gdGhhdCB0YWJsZToKIyAgICAvZ2FtZWNoYW5u"
    "ZWxzbGlzdAojICAgIC9qb2luY2hhdGNoYW5uZWwgIiVTIiAiJVMiICIlZCIKIyAgICAvbXNnICIu"
    "Li4KIyAgICAvc2V0Z2FtZXBhcmFtcyAiJXMiICIlcyIKIyAgICAvbmV3Z2FtZWhvc3QgIiVzIgpk"
    "ZWYgX2dhbWVjaGFubmVsc2xpc3QobWQsdXNyLHJlcyk6CiAgICAjUGxhaW4gIndoYXQgdG93bnMg"
    "YXJlIHRoZXJlPyIuIGVudW1lcmF0ZUdDKCkgYWxyZWFkeSBidWlsZHMgZXhhY3RseSB0aGlzCiAg"
    "ICAjYW5zd2VyIC0gaXQgd2FzIG9ubHkgZXZlciBzZW50IGFzIHRoZSByZXBseSB0byAvbGVhdmVn"
    "YW1lY2hhbm5lbCwgc28gYQogICAgI2NsaWVudCB0aGF0IGFza2VkIGRpcmVjdGx5IGdvdCBzaWxl"
    "bmNlIGFuZCBhbiBlbXB0eSB0b3duIGxpc3QuCiAgICByZXR1cm4gdXNyLnNlcnZlci5zdGF0ZS5l"
    "bnVtZXJhdGVHQygpCmRlZiBfam9pbmNoYXRjaGFubmVsKG1kLHVzcixyZXMpOgogICAgIyhjaGFu"
    "bmVsLCBwYXNzd29yZCwgZmxhZykuIGpvaW5DaGF0KCkgYWxyZWFkeSByZXR1cm5zIHRoZSBmdWxs"
    "IHJlcGx5IHRoZQogICAgI2NsaWVudCBleHBlY3RzIC0gdGhlIGpvaW4gY29uZmlybWF0aW9uIHBs"
    "dXMgdGhlIHJvc3RlciAtIGFuZCB3YXMgb25seQogICAgI3JlYWNoYWJsZSBhcyBhIHNpZGUgZWZm"
    "ZWN0IG9mIGVudGVyaW5nIGEgdG93biwgc28gdGhlIHNlY29uZCBjaGF0IGNoYW5uZWwKICAgICMo"
    "VHJhZGUpIGNvdWxkIG5ldmVyIGJlIGpvaW5lZDogdGhlIGNvbW1hbmQgdG8gc3dpdGNoIHdhcyB1"
    "bmhhbmRsZWQuCiAgICAjVGhlIHBhc3N3b3JkIGlzIGFjY2VwdGVkIGFuZCBpZ25vcmVkLCBhcyBl"
    "dmVyeXdoZXJlIGVsc2UgaW4gdGhpcyBmaWxlOyB0aGUKICAgICN0cmFpbGluZyBpbnRlZ2VyJ3Mg"
    "bWVhbmluZyBpcyBub3Qga25vd24gYW5kIG5vdGhpbmcgaGVyZSBkZXBlbmRzIG9uIGl0LgogICAg"
    "Y2hubCA9IHVzci51c2VyLmdhbWVjaGFubmVsCiAgICBpZiBub3QgY2hubDoKICAgICAgICByZXR1"
    "cm4gTm9uZSAjbm90IGluIGEgdG93biwgbm90aGluZyB0byBqb2luCiAgICBuYW1lID0gc2FuaXRp"
    "emVUZXh0KHJlc1sxXSwgX01BWF9DSEFUTkFNRSkuc3RyaXAoKQogICAgaWYgbm90IG5hbWU6CiAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIG5hbWUgbm90IGluIGNobmwuY2hhdENoYW5uZWxzOgog"
    "ICAgICAgICNUaGUgY2xpZW50IGhhcyBhICJjcmVhdGUgY2hhdCBjaGFubmVsIiBjb250cm9sIG9m"
    "IGl0cyBvd24KICAgICAgICAjKElEQ19DUkVBVEVDSEFUQ0hBTk5FTCBpbiB0aGUgU0RLJ3MgRGlh"
    "bG9nc1Jlc291cmNlLmgpIGFuZCBubyBzZXBhcmF0ZQogICAgICAgICNjb21tYW5kIGZvciBpdCwg"
    "c28gam9pbmluZyBhIG5hbWUgdGhhdCBkb2VzIG5vdCBleGlzdCB5ZXQgKmlzKiBob3cgYQogICAg"
    "ICAgICNjaGFubmVsIGdldHMgY3JlYXRlZC4gUmVmdXNpbmcgbGVmdCB0aGF0IGJ1dHRvbiBkb2lu"
    "ZyBub3RoaW5nIGJ1dCBoYW5nCiAgICAgICAgI3RoZSBkaWFsb2cuIENhcHBlZCwgYmVjYXVzZSB0"
    "aGUgbmFtZSBpcyBwbGF5ZXItc3VwcGxpZWQgYW5kIHRoZXNlCiAgICAgICAgI291dGxpdmUgdGhl"
    "IHBsYXllciB3aG8gbWFkZSB0aGVtLgogICAgICAgIGlmIGxlbihjaG5sLmNoYXRDaGFubmVscykg"
    "Pj0gX01BWF9DSEFUX0NIQU5ORUxTOgogICAgICAgICAgICBwcmludChmJyoqKiB7dXNyLnVzZXIu"
    "bmFtZX0gY291bGQgbm90IGNyZWF0ZSBjaGF0IGNoYW5uZWwge25hbWUhcn06ICcKICAgICAgICAg"
    "ICAgICAgICAgZid0b3duIGFscmVhZHkgaGFzIHtsZW4oY2hubC5jaGF0Q2hhbm5lbHMpfScpCiAg"
    "ICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAgY2hubC5jaGF0Q2hhbm5lbHNbbmFtZV0gPSBb"
    "XQogICAgICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0gY3JlYXRlZCBjaGF0IGNo"
    "YW5uZWwgIntuYW1lfSIgaW4ge2NobmwubmFtZX0nKQogICAgICAgICNFdmVyeW9uZSBicm93c2lu"
    "ZyB0aGUgdG93biBnZXRzIHRoZSByZWZyZXNoZWQgY2hhbm5lbCBsaXN0LCBvdGhlcndpc2UKICAg"
    "ICAgICAjdGhlIG5ldyBjaGFubmVsIGlzIGludmlzaWJsZSB0byBhbGwgYnV0IGl0cyBjcmVhdG9y"
    "LgogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6bGlzdChjaG5sLnVzZXJsaXN0KSwnbWVzc2FnZSc6"
    "Y2hubC5lbnVtQ2hhdHMoKX0pCiAgICByZXR1cm4gY2hubC5qb2luQ2hhdCh1c3IsIG5hbWUsIHJl"
    "c1syXSBpZiBsZW4ocmVzKT4yIGVsc2UgJycpCmRlZiBfbXNnKG1kLHVzcixyZXMpOgogICAgI1By"
    "aXZhdGUgbWVzc2FnZS4gUmVsYXllZCBpbiB0aGUgc2FtZSBzaGFwZSAvc2VuZCB1c2VzIC0gIjxz"
    "ZW5kZXI+IiB0aGVuIHRoZQogICAgI3RleHQgLSBiZWNhdXNlIHRoYXQgaXMgdGhlIG9uZSB0d28t"
    "ZmllbGQgdGV4dCBtZXNzYWdlIHRoaXMgY2xpZW50IGlzIGtub3duCiAgICAjdG8gcmVuZGVyLiBU"
    "aGUgZXhhY3Qgc2VydmVyLT5jbGllbnQgc3BlbGxpbmcgZm9yIGEgcHJpdmF0ZSBtZXNzYWdlIGhh"
    "cyBub3QKICAgICNiZWVuIGNhcHR1cmVkOyBpZiBhIHNlc3Npb24gbG9nIGV2ZXIgc2hvd3MgdGhl"
    "IGNsaWVudCBtaXNoYW5kbGluZyBpdCwgdGhpcwogICAgI2lzIHRoZSBsaW5lIHRvIHJldmlzaXQu"
    "IERvaW5nIG5vdGhpbmcgd2FzIG5vdCB0aGUgc2FmZXIgb3B0aW9uOiBpdCBpcyB3aGF0CiAgICAj"
    "dGhlIHNlcnZlciBkaWQgdW50aWwgbm93LCBhbmQgcHJpdmF0ZSBtZXNzYWdlcyBzaW1wbHkgdmFu"
    "aXNoZWQuCiAgICBpZiBsZW4ocmVzKTwzOgogICAgICAgIHJldHVybiBOb25lCiAgICB0YXJnZXQg"
    "PSByZXNbMV0KICAgIHRleHQgPSBzYW5pdGl6ZVRleHQocmVzWzJdLCBfTUFYX0NIQVRfVEVYVCkK"
    "ICAgIGlmIG5vdCB0ZXh0OgogICAgICAgIHJldHVybiBOb25lCiAgICB0Y29uID0gdXNyLnNlcnZl"
    "ci5nZXRQbGF5ZXIodGFyZ2V0KQogICAgaWYgdGNvbiBpcyBOb25lOgogICAgICAgIHJldHVybiBO"
    "b25lICNyZWNpcGllbnQgb2ZmbGluZQogICAgdGNvbi5zZW5kKF9lbShmJy9tc2cgInt1c3IudXNl"
    "ci5uYW1lfSIgInt0ZXh0fSInKSkKICAgIHJldHVybiBOb25lCmRlZiBfc2V0Z2FtZXBhcmFtcyht"
    "ZCx1c3IscmVzKToKICAgICNUd28gc3RyaW5ncyB3aG9zZSBtZWFuaW5nIGlzIG5vdCBkb2N1bWVu"
    "dGVkIGFueXdoZXJlIGF2YWlsYWJsZSwgc28gbm90aGluZwogICAgI2lzICpjaGFuZ2VkKiBvbiB0"
    "aGUgc3RyZW5ndGggb2YgYSBndWVzcyAtIHRoZSByb29tJ3Mgc3RvcmVkIHBhcmFtZXRlcnMgYXJl"
    "CiAgICAjbGVmdCBleGFjdGx5IGFzIGl0cyAvY3JlYXRlZ2FtZSBzZXQgdGhlbS4gV2hhdCB0aGlz"
    "IGRvZXMgYnV5IGlzIHRoYXQgdGhlCiAgICAjY29tbWFuZCBzdG9wcyBiZWluZyBhbiB1bmtub3du"
    "IG9uZSwgYW5kIGV2ZXJ5b25lIGJyb3dzaW5nIGdldHMgYSByZWZyZXNoZWQKICAgICMkZ2FtZSBl"
    "bnRyeSwgd2hpY2ggaXMgYSBtZXNzYWdlIHRoZSBjbGllbnQgYWxyZWFkeSBoYW5kbGVzLiBUaGUg"
    "cmF3CiAgICAjYXJndW1lbnRzIGFyZSBsb2dnZWQgc28gYSByZWFsIHNlc3Npb24gY2FuIHNldHRs"
    "ZSB3aGF0IHRoZXkgbWVhbi4KICAgIGdtID0gdXNyLnVzZXIuZ2FtZQogICAgaWYgZ20gaXMgTm9u"
    "ZSBvciBnbS5ob3N0IGlzIG5vdCB1c3I6CiAgICAgICAgcmV0dXJuIE5vbmUgI29ubHkgdGhlIHJv"
    "b20ncyBvd24gaG9zdCBtYXkgdG91Y2ggaXRzIHBhcmFtZXRlcnMKICAgIHByaW50KGYnW0xvYmJ5"
    "XSB7dXNyLnVzZXIubmFtZX0gL3NldGdhbWVwYXJhbXMgZm9yICJ7Z20uZ25hbWV9IjogJwogICAg"
    "ICAgICAgZid7cmVzWzFdIXJ9IHtyZXNbMl0hcn0gKHJlY29yZGVkLCBub3QgYXBwbGllZCknKQog"
    "ICAgbXNnID0gZ20uZ2V0R2FtZVN0cmluZygpCiAgICBpZiBtc2c6CiAgICAgICAgbWQuYWRkKHsn"
    "dGFyZ2V0JzpnbS5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBOb25lCmRl"
    "ZiBfbmV3Z2FtZWhvc3QobWQsdXNyLHJlcyk6CiAgICAjQSBmcmVzaCB4LWRpcmVjdHBsYXkgVVJM"
    "IGZvciBhIHJvb20gdGhhdCBhbHJlYWR5IGV4aXN0cy4gSXQgY2FycmllcyB0aGUKICAgICNob3N0"
    "J3Mgb3duIGlkZWEgb2YgaXRzIGFkZHJlc3MsIHdoaWNoIGJlaGluZCBhIHJvdXRlciBpcyBhIExB"
    "TiBhZGRyZXNzIG5vCiAgICAjam9pbmVyIGNhbiByZWFjaCAtIHRoZSBzYW1lIHByb2JsZW0gL2Ny"
    "ZWF0ZWdhbWUgaGFzLCBhbmQgaXQgbXVzdCBnZXQgdGhlCiAgICAjc2FtZSB0cmVhdG1lbnQsIG9y"
    "IGEgcm9vbSB3aG9zZSBob3N0IHJlLWFkdmVydGlzZXMgc2lsZW50bHkgYmVjb21lcwogICAgI3Vu"
    "am9pbmFibGUgd2hpbGUgc3RpbGwgYmVpbmcgbGlzdGVkLgogICAgZ20gPSB1c3IudXNlci5nYW1l"
    "CiAgICBpZiBnbSBpcyBOb25lIG9yIGdtLmhvc3QgaXMgbm90IHVzcjoKICAgICAgICByZXR1cm4g"
    "Tm9uZSAjb25seSB0aGUgaG9zdCBkZXNjcmliZXMgd2hlcmUgdGhlIGdhbWUgaXMKICAgIHBlZXIg"
    "PSB1c3IuY2xpZW50X2FkZHJlc3NbMF0gaWYgdXNyLmNsaWVudF9hZGRyZXNzIGVsc2UgJycKICAg"
    "ICh1cmwsIG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0KHJlc1sxXSwgcGVlcikKICAgIGdtLnVybCA9"
    "IHVybAogICAgcHJpbnQoZidbTG9iYnldIHt1c3IudXNlci5uYW1lfSBtb3ZlZCByb29tICJ7Z20u"
    "Z25hbWV9Ijoge25vdGV9JykKICAgIHByaW50KGYnW0xvYmJ5XSAgIHVybCBhZHZlcnRpc2VkIHRv"
    "IGpvaW5lcnM6IHtnbS51cmx9JykKICAgIG1zZyA9IGdtLmdldEdhbWVTdHJpbmcoKQogICAgaWYg"
    "bXNnOgogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6Z20uX2F1ZGllbmNlKCksJ21lc3NhZ2UnOm1z"
    "Z30pCiAgICByZXR1cm4gTm9uZQpkZWYgX3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwobWQsdXNyLHJl"
    "cyk6CiAgICBjaG5sID0gdXNyLnNlcnZlci5zdGF0ZS5nYW1lQ2hhbm5lbHMuZ2V0KHJlc1sxXSkK"
    "ICAgIGlmIGNobmwgaXMgTm9uZToKICAgICAgICByZXR1cm4gX2VtKGYnL3JlcXVlc3Rqb2luZ2Ft"
    "ZWNoYW5uZWwgIntyZXNbMV19IiAiMCInKSAjdW5rbm93biBjaGFubmVsCiAgICAjVE9ETyBjaGVj"
    "ayBwZXJtaXNzaW9ucz8KICAgIGlmIGNobmwucmVxdWVzdEpvaW4odXNyKToKICAgICAgICByZXR1"
    "cm4gX2VtKGYnL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgIntyZXNbMV19IiAiMSInKQogICAgcmV0"
    "dXJuIF9lbShmJy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsICJ7cmVzWzFdfSIgIjAiJykKZGVmIF9q"
    "b2luZ2FtZWNoYW5uZWwobWQsdXNyLHJlcyk6CiAgICBjaG5sID0gdXNyLnNlcnZlci5zdGF0ZS5n"
    "YW1lQ2hhbm5lbHMuZ2V0KHJlc1sxXSkKICAgIGlmIGNobmwgaXMgTm9uZToKICAgICAgICByZXR1"
    "cm4gTm9uZSAjdW5rbm93biBjaGFubmVsLCBpZ25vcmUKICAgIGlmIGxlbihyZXMpPjI6CiAgICAg"
    "ICAgcG9zID0gX2hlcm9Qb3MocmVzWzJdKQogICAgICAgIGlmIHBvcyBpcyBub3QgTm9uZToKICAg"
    "ICAgICAgICAgdXNyLnVzZXIucG9zZGF0YSA9IHBvcwogICAgcmV0dXJuIGNobmwuam9pbkNoYW5u"
    "ZWwodXNyLCByZXNbMV0pCmRlZiBfc2V0dXNlcmhlcm9kYXRhKG1kLHVzcixyZXMpOgogICAgcGQg"
    "PSBfUmVhZEJsb2IodXNyLCByZXNbMl0pCiAgICBpZiBsZW4ocGQpID4gX01BWF9IRVJPREFUQToK"
    "ICAgICAgICAjVW5saWtlIC9zZXRwbGF5ZXJkYXRhLCB3aGljaCBpcyB3cml0dGVuIHRvIGRpc2sg"
    "YW5kIHJlYWQgYmFjayBieSBpdHMKICAgICAgICAjb3duZXIgYWxvbmUsIGhlcm9kYXRhIGlzIHJl"
    "LWJyb2FkY2FzdCB0byBldmVyeSBvdGhlciBwbGF5ZXIgaW4gdGhlIHRvd24KICAgICAgICAjb24g"
    "ZXZlcnkgam9pbiBhbmQgb24gZXZlcnkgY2hhbmdlLiBBdCB0aGUgZ2VuZXJhbCBfTUFYX0JMT0Ig"
    "Y2VpbGluZyBvbmUKICAgICAgICAjY2xpZW50IGNvdWxkIGhhbmQgdGhlIHNlcnZlciAxNiBNQiBh"
    "bmQgaGF2ZSBpdCBmYW5uZWQgb3V0IGZpZnR5IHRpbWVzLAogICAgICAgICN3aGljaCBibG93cyBw"
    "YXN0IGV2ZXJ5IHJlY2lwaWVudCdzIHNlbmQtYmFja2xvZyBjYXAgYW5kIGRyb3BzIHRoZSB3aG9s"
    "ZQogICAgICAgICN0b3duIGluc3RlYWQgb2YgdGhlIGNsaWVudCB0aGF0IGRpZCBpdC4gUmVhbCBo"
    "ZXJvIGFwcGVhcmFuY2UgZGF0YSBpcyBhCiAgICAgICAgI2ZldyBraWxvYnl0ZXMuCiAgICAgICAg"
    "cmFpc2UgUHJvdG9jb2xFcnJvcihmJ2hlcm9kYXRhIGJsb2Igb2Yge2xlbihwZCl9IGJ5dGVzIGV4"
    "Y2VlZHMgJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgZid7X01BWF9IRVJPREFUQX0nKQog"
    "ICAgdXNyLnVzZXIuaGVyb2RhdGEgPSBwZAogICAgaWYgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAg"
    "ICAgICAgbXNnID0gdXNyLnVzZXIuZ2V0R0NVbXNnKCkKICAgICAgICB0ZyA9IF93b1VzZXIodXNy"
    "LnVzZXIuZ2FtZWNoYW5uZWwudXNlcmxpc3QsIHVzcikKICAgICAgICBtZC5hZGQoeyd0YXJnZXQn"
    "OnRnLCdtZXNzYWdlJzptc2d9KQogICAgcmV0dXJuIE5vbmUKZGVmIF9zZW5kKG1kLHVzcixyZXMp"
    "OgogICAgaWYgbm90IHVzci51c2VyLmNoYXRjaGFubmVsOgogICAgICAgIHJldHVybiBOb25lCiAg"
    "ICBpZiBsZW4ocmVzKTwyOgogICAgICAgIHJldHVybiBOb25lCiAgICB0ZXh0ID0gc2FuaXRpemVU"
    "ZXh0KHJlc1sxXSwgX01BWF9DSEFUX1RFWFQpCiAgICBpZiBub3QgdGV4dDoKICAgICAgICByZXR1"
    "cm4gTm9uZQogICAgaWYgX0FETUlOUyBhbmQgdGV4dC5zdGFydHN3aXRoKF9BRE1JTl9QUkVGSVgp"
    "OgogICAgICAgICNOZXZlciByZWxheWVkIHRvIHRoZSBjaGFubmVsLCB3aG9ldmVyIHR5cGVkIGl0"
    "LiBGb3IgYW4gYWRtaW4gdGhhdAogICAgICAgICNrZWVwcyB0aGUgc2VydmVyJ3MgYnVzaW5lc3Mg"
    "b2ZmIHRoZSBwdWJsaWMgY2hhdDsgZm9yIGV2ZXJ5Ym9keSBlbHNlIGl0CiAgICAgICAgI3N0b3Bz"
    "IHRoZSByb29tIGxlYXJuaW5nIHdoaWNoIGNvbW1hbmRzIGV4aXN0IGJ5IHdhdGNoaW5nIHNvbWVv"
    "bmUgZ3Vlc3MKICAgICAgICAjYXQgdGhlbS4KICAgICAgICAjVGhlIGBfQURNSU5TIGFuZGAgZ3Vh"
    "cmQgbWF0dGVyczogd2l0aCBubyBhZG1pbnMgY29uZmlndXJlZCB0aGUgY29uc29sZQogICAgICAg"
    "ICNpcyBtZWFudCB0byBiZSBvZmYgb3V0cmlnaHQsIGJ1dCB0aGlzIGJyYW5jaCBzdGlsbCBhdGUg"
    "ZXZlcnkgY2hhdCBsaW5lCiAgICAgICAgI3RoYXQgaGFwcGVuZWQgdG8gc3RhcnQgd2l0aCAnIScg"
    "LSBzbyBvbiBhIGRlZmF1bHQgc2VydmVyICIhISEiIG9yCiAgICAgICAgIyIh0YPRgNCwIiBzaW1w"
    "bHkgbmV2ZXIgcmVhY2hlZCB0aGUgcm9vbSwgd2l0aCBub3RoaW5nIG9uIHNjcmVlbiB0byBzYXkK"
    "ICAgICAgICAjd2h5LiBXaXRoIG5vIGFkbWlucyB0aGVyZSBpcyBubyBjb25zb2xlLCBzbyB0aGVy"
    "ZSBpcyBub3RoaW5nIHRvIGhpZGUKICAgICAgICAjYW5kIHRoZSBsaW5lIGlzIG9yZGluYXJ5IGNo"
    "YXQuCiAgICAgICAgcmV0dXJuIGFkbWluQ29tbWFuZCh1c3IsIHRleHRbbGVuKF9BRE1JTl9QUkVG"
    "SVgpOl0uc3RyaXAoKSkKICAgIHVsID0gdXNyLnVzZXIuY2hhdGNoYW5uZWwKICAgIG1kLmFkZCh7"
    "J3RhcmdldCc6dWwsJ21lc3NhZ2UnOl9lbShmJy9zZW5kICJ7dXNyLnVzZXIubmFtZX0iICJ7dGV4"
    "dH0iJyl9KQogICAgcmV0dXJuIE5vbmUKIy0tLSBpbi1nYW1lIGFkbWluIGNvbnNvbGUgLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KI1R5cGVkIGlu"
    "dG8gdGhlIGdhbWUncyBvd24gY2hhdCBib3gsIHNvIGl0IG5lZWRzIG5vIGNsaWVudCBtb2RpZmlj"
    "YXRpb24gYXQgYWxsOgojdGhlIHJldGFpbCBjbGllbnQgYWxyZWFkeSBzZW5kcyBldmVyeXRoaW5n"
    "IHR5cGVkIHRoZXJlIGFzIC9zZW5kLCBhbmQgYWxyZWFkeQojcmVuZGVycyAnL2FkbWluIDx0ZXh0"
    "PicgY29taW5nIGJhY2sgdGhlIG90aGVyIHdheSAodGhhdCBpcyBob3cgYSBraWNrIG5vdGljZQoj"
    "cmVhY2hlcyBhIHBsYXllcikuIEJvdGggaGFsdmVzIGFyZSB0aGVyZWZvcmUga25vd24tZ29vZCBt"
    "ZXNzYWdlIHNoYXBlcywgd2hpY2gKI2lzIHdoYXQgbWFrZXMgdGhpcyBzYWZlIG9uIGEgMjAwOCBi"
    "aW5hcnkgLSBub3RoaW5nIG5ldyBpcyBpbnZlbnRlZCBvbiB0aGUgd2lyZS4KI09ubHkgYWNjb3Vu"
    "dHMgbGlzdGVkIGFzIEFkbWlucyBpbiBDb25maWcuaW5pIGFyZSBvYmV5ZWQuIEV2ZXJ5b25lIGVs"
    "c2UncwojY29tbWFuZHMgYXJlIHN3YWxsb3dlZCBzaWxlbnRseSByYXRoZXIgdGhhbiBhbnN3ZXJl"
    "ZCwgc28gdGhlIHByZXNlbmNlIG9mIHRoZQojY29uc29sZSBpcyBub3QgYWR2ZXJ0aXNlZCB0byB0"
    "aGUgcm9vbS4KZGVmIF9hZG1pblJlcGx5KHVzciwgbGluZXMpOgogICAgI09uZSAvYWRtaW4gcGVy"
    "IGxpbmU6IHRoZSBjbGllbnQgdHJlYXRzIGVhY2ggYXMgaXRzIG93biBzZXJ2ZXIgbWVzc2FnZSwg"
    "YW5kCiAgICAjYSBzaW5nbGUgbG9uZyBsaW5lIHdvdWxkIHJ1biBpbnRvIHRoZSB3aXJlLWxlbmd0"
    "aCBsaW1pdCBhbnl3YXkuCiAgICBvdXQgPSBiJycKICAgIGZvciBsaW5lIGluIGxpbmVzOgogICAg"
    "ICAgIG91dCArPSBfZW0oZicvYWRtaW4ge3Nhbml0aXplVGV4dChzdHIobGluZSksIF9NQVhfQ0hB"
    "VF9URVhUKX0nKQogICAgcmV0dXJuIG91dCBvciBOb25lCmRlZiBfZm10UGxheWVycyhzZXJ2ZXIp"
    "OgogICAgcm93cyA9IFtdCiAgICBmb3IgKG5hbWUsIGNvbikgaW4gc29ydGVkKHNlcnZlci5zdGF0"
    "ZS5hY3RpdmVVc2Vycy5pdGVtcygpKToKICAgICAgICB0b3duID0gY29uLnVzZXIuZ2FtZWNoYW5u"
    "ZWwubmFtZS5zcGxpdCgnIycpWzBdIGlmIGNvbi51c2VyLmdhbWVjaGFubmVsIGVsc2UgJy0nCiAg"
    "ICAgICAgZ2FtZSA9IGNvbi51c2VyLmdhbWUuZ25hbWUgaWYgY29uLnVzZXIuZ2FtZSBlbHNlICct"
    "JwogICAgICAgIHJvd3MuYXBwZW5kKGYne25hbWV9ICB0b3duOnt0b3dufSAgcm9vbTp7Z2FtZX0n"
    "KQogICAgcmV0dXJuIHJvd3Mgb3IgWydub2JvZHkgb25saW5lJ10KZGVmIGFkbWluQ29tbWFuZCh1"
    "c3IsIGxpbmUpOgogICAgc2VydmVyID0gdXNyLnNlcnZlcgogICAgd2hvID0gdXNyLnVzZXIubmFt"
    "ZQogICAgaWYgd2hvLmNhc2Vmb2xkKCkgbm90IGluIF9BRE1JTlM6CiAgICAgICAgcHJpbnQoZidb"
    "TG9iYnldIHt3aG99IHRyaWVkIGFuIGFkbWluIGNvbW1hbmQgd2l0aG91dCBiZWluZyBhbiBhZG1p"
    "bjoge2xpbmUhcn0nKQogICAgICAgIHJldHVybiBOb25lCiAgICBwYXJ0cyA9IGxpbmUuc3BsaXQo"
    "Tm9uZSwgMSkKICAgIGNtZCA9IHBhcnRzWzBdLmxvd2VyKCkgaWYgcGFydHMgZWxzZSAnJwogICAg"
    "YXJnID0gcGFydHNbMV0uc3RyaXAoKSBpZiBsZW4ocGFydHMpID4gMSBlbHNlICcnCiAgICBwcmlu"
    "dChmJ1tMb2JieV0gQURNSU4ge3dob306IHtsaW5lIXJ9JykKICAgIGdsb2JhbCBfUE9TX1VQREFU"
    "RV9IWiwgX0lETEVfVElNRU9VVCwgX1NFTkRfTk9QUywgREVGQVVMVF9NT1RECiAgICBpZiBjbWQg"
    "aW4gKCdoZWxwJywgJz8nLCAnJyk6CiAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgWwog"
    "ICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfXdobyAtIHdobyBpcyBvbmxpbmUnLAogICAgICAg"
    "ICAgICBmJ3tfQURNSU5fUFJFRklYfXNheSA8dGV4dD4gLSBhbm5vdW5jZSB0byBldmVyeW9uZScs"
    "CiAgICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9a2ljayA8bmFtZT4nLAogICAgICAgICAgICBm"
    "J3tfQURNSU5fUFJFRklYfW1vdGQgPHRleHQ+JywKICAgICAgICAgICAgZid7X0FETUlOX1BSRUZJ"
    "WH1oeiA8MC41LXtfUE9TX1VQREFURV9IWl9NQVh9PiAtIHBvc2l0aW9uIHN5bmMgcmF0ZScsCiAg"
    "ICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9aWRsZSA8c2Vjb25kcywgMD1vZmY+JywKICAgICAg"
    "ICAgICAgZid7X0FETUlOX1BSRUZJWH1rZWVwYWxpdmUgb258b2ZmJywKICAgICAgICAgICAgZid7"
    "X0FETUlOX1BSRUZJWH1zdGF0dXMnLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfXNhdmUg"
    "LSB3cml0ZSB0aGVzZSBzZXR0aW5ncyB0byBDb25maWcuaW5pJywKICAgICAgICBdKQogICAgaWYg"
    "Y21kID09ICd3aG8nOgogICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIF9mbXRQbGF5ZXJz"
    "KHNlcnZlcikpCiAgICBpZiBjbWQgPT0gJ3N0YXR1cyc6CiAgICAgICAgcmV0dXJuIF9hZG1pblJl"
    "cGx5KHVzciwgWwogICAgICAgICAgICBmJ3BsYXllcnMge2xlbihzZXJ2ZXIuc3RhdGUuYWN0aXZl"
    "VXNlcnMpfSwgJwogICAgICAgICAgICBmJ2h6IHtfUE9TX1VQREFURV9IWn0sIGlkbGUge19JRExF"
    "X1RJTUVPVVR9cywgJwogICAgICAgICAgICBmJ2tlZXBhbGl2ZSB7Im9uIiBpZiBfU0VORF9OT1BT"
    "IGVsc2UgIm9mZiJ9JywKICAgICAgICBdKQogICAgaWYgY21kID09ICdzYXknOgogICAgICAgIGlm"
    "IG5vdCBhcmc6CiAgICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsnc2F5IHdoYXQ/"
    "J10pCiAgICAgICAgbXNnID0gX2VtKGYnL2FkbWluIHtzYW5pdGl6ZVRleHQoYXJnLCBfTUFYX0NI"
    "QVRfVEVYVCl9JykKICAgICAgICBzZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOmxpc3Qoc2VydmVy"
    "LnN0YXRlLmFjdGl2ZVVzZXJzLnZhbHVlcygpKSwnbWVzc2FnZSc6bXNnfSkKICAgICAgICByZXR1"
    "cm4gTm9uZSAjdGhlIGFubm91bmNlbWVudCBpdHNlbGYgaXMgdGhlIGFkbWluJ3MgY29uZmlybWF0"
    "aW9uCiAgICBpZiBjbWQgPT0gJ2tpY2snOgogICAgICAgIGlmIG5vdCBhcmc6CiAgICAgICAgICAg"
    "IHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsna2ljayB3aG8/J10pCiAgICAgICAgaWYgYXJnLmNh"
    "c2Vmb2xkKCkgPT0gd2hvLmNhc2Vmb2xkKCk6CiAgICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBs"
    "eSh1c3IsIFsna2lja2luZyB5b3Vyc2VsZiBpcyBub3QgYSBwbGFuJ10pCiAgICAgICAgb2sgPSBz"
    "ZXJ2ZXIua2lja1BsYXllcihhcmcsIGYnS2lja2VkIGJ5IHt3aG99JykKICAgICAgICByZXR1cm4g"
    "X2FkbWluUmVwbHkodXNyLCBbZidraWNrZWQge2FyZ30nIGlmIG9rIGVsc2UgZid7YXJnfSBpcyBu"
    "b3Qgb25saW5lJ10pCiAgICBpZiBjbWQgPT0gJ21vdGQnOgogICAgICAgIGlmIG5vdCBhcmc6CiAg"
    "ICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsnbW90ZCBuZWVkcyBzb21lIHRleHQn"
    "XSkKICAgICAgICBERUZBVUxUX01PVEQgPSBhcmcKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHko"
    "dXNyLCBbJ21vdGQgc2V0IChzaG93biBhdCB0aGUgbmV4dCBsb2dpbiknXSkKICAgIGlmIGNtZCA9"
    "PSAnaHonOgogICAgICAgIHRyeToKICAgICAgICAgICAgaHogPSBmbG9hdChhcmcpCiAgICAgICAg"
    "ZXhjZXB0IFZhbHVlRXJyb3I6CiAgICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsn"
    "aHogbmVlZHMgYSBudW1iZXInXSkKICAgICAgICAjQ2xhbXBlZCBleGFjdGx5IGFzIGFwcGx5Q29u"
    "ZmlnKCkgZG9lcyAtIG9uZSBydWxlLCBvbmUgcGxhY2UgdG8gY2hhbmdlLgogICAgICAgIF9QT1Nf"
    "VVBEQVRFX0haID0gbWluKG1heChoeiwgMC41KSwgX1BPU19VUERBVEVfSFpfTUFYKQogICAgICAg"
    "IHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFtmJ3Bvc2l0aW9uIHN5bmMgbm93IHtfUE9TX1VQREFU"
    "RV9IWn0vcyddKQogICAgaWYgY21kID09ICdpZGxlJzoKICAgICAgICB0cnk6CiAgICAgICAgICAg"
    "IF9JRExFX1RJTUVPVVQgPSBtYXgoMCwgaW50KGFyZykpCiAgICAgICAgZXhjZXB0IFZhbHVlRXJy"
    "b3I6CiAgICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsnaWRsZSBuZWVkcyBhIHdo"
    "b2xlIG51bWJlciBvZiBzZWNvbmRzJ10pCiAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwg"
    "W2YnaWRsZSB0aW1lb3V0IG5vdyB7X0lETEVfVElNRU9VVH1zJ10pCiAgICBpZiBjbWQgPT0gJ2tl"
    "ZXBhbGl2ZSc6CiAgICAgICAgaWYgYXJnLmxvd2VyKCkgbm90IGluICgnb24nLCAnb2ZmJyk6CiAg"
    "ICAgICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsna2VlcGFsaXZlIG9ufG9mZiddKQog"
    "ICAgICAgIF9TRU5EX05PUFMgPSBhcmcubG93ZXIoKSA9PSAnb24nCiAgICAgICAgcmV0dXJuIF9h"
    "ZG1pblJlcGx5KHVzciwgW2Yna2VlcGFsaXZlIHsib24iIGlmIF9TRU5EX05PUFMgZWxzZSAib2Zm"
    "In0nXSkKICAgIGlmIGNtZCA9PSAnc2F2ZSc6CiAgICAgICAgI0V2ZXJ5dGhpbmcgYWJvdmUgY2hh"
    "bmdlcyB0aGUgbGl2ZSBzZXJ2ZXIgb25seS4gVGhpcyBpcyB0aGUgb25lIGNvbW1hbmQKICAgICAg"
    "ICAjdGhhdCB0b3VjaGVzIHRoZSBmaWxlLCBzbyBhIHNlc3Npb24gb2YgZXhwZXJpbWVudHMgY2Fu"
    "bm90IGJlIG1hZGUKICAgICAgICAjcGVybWFuZW50IGJ5IGFjY2lkZW50LgogICAgICAgIGNmZyA9"
    "IGxvYWRDb25maWcoKQogICAgICAgIHNlYyA9IGNmZ1snc2VydmVyJ10KICAgICAgICBzZWNbJ01P"
    "VEQnXSA9IF9lc2NhcGVNT1REKERFRkFVTFRfTU9URCkKICAgICAgICBzZWNbJ1Bvc2l0aW9uVXBk"
    "YXRlSHonXSA9IHN0cihfUE9TX1VQREFURV9IWikKICAgICAgICBzZWNbJ0lkbGVUaW1lb3V0J10g"
    "PSBzdHIoX0lETEVfVElNRU9VVCkKICAgICAgICBzZWNbJ0tlZXBhbGl2ZSddID0gc3RyKF9TRU5E"
    "X05PUFMpCiAgICAgICAgc2F2ZUNvbmZpZyhjZmcpCiAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5"
    "KHVzciwgWydzYXZlZCB0byBDb25maWcuaW5pJ10pCiAgICByZXR1cm4gX2FkbWluUmVwbHkodXNy"
    "LCBbZid1bmtub3duIGNvbW1hbmQge2NtZCFyfSAtIHRyeSB7X0FETUlOX1BSRUZJWH1oZWxwJ10p"
    "CmRlZiBfZ2V0Z3VpbGRyYW5rcG9pbnRzKG1kLHVzcixyZXMpOgogICAgKGEsYixjLGQpID0gX2dy"
    "cCgpCiAgICByZXR1cm4gX2VtKGYnL2dldGd1aWxkcmFua3BvaW50cyAie2F9IiAie2J9IiAie2N9"
    "IiAie2R9IicpCgojIyBHVUlMRFMKI0d1aWxkIGNyZWF0aW9uIGRpZCBub3RoaW5nIGF0IGFsbCBi"
    "ZWZvcmUgdGhpczogdGhlcmUgd2FzIG5vIC9jcmVhdGVndWlsZCAob3IKI2FueXRoaW5nIGVsc2Ug"
    "Z3VpbGQtcmVsYXRlZCkgaW4gX0NPTU1BTkRTLCBzbyB0aGUgY2xpZW50J3MgcmVxdWVzdCBmZWxs"
    "CiN0aHJvdWdoIHRvIHRoZSAiVW5rbm93biBDb21tYW5kIiBicmFuY2ggb2YgQ29tbWFuZFBhcnNl"
    "ci5wYXJzZSBhbmQgd2FzCiNkcm9wcGVkLiBUaGUgY2xpZW50IGdvdCBubyByZXBseSwgbm8gZXJy"
    "b3IsIGFuZCBubyBndWlsZC4KI05PVEUgT04gQ09NTUFORCBOQU1FUzogdGhlIGV4YWN0IHdpcmUg"
    "bmFtZXMgdGhlIHJldGFpbCBjbGllbnQgdXNlcyBmb3IgdGhlCiNndWlsZCBVSSBhcmUgbm90IGRv"
    "Y3VtZW50ZWQgYW55d2hlcmUgd2UgaGF2ZS4gVGhlIGhhbmRsZXJzIGJlbG93IGFyZQojcmVnaXN0"
    "ZXJlZCB1bmRlciBldmVyeSBzcGVsbGluZyB0aGF0IGZpdHMgdGhpcyBwcm90b2NvbCdzIGNvbnZl"
    "bnRpb25zLCBhbGwKI3JvdXRlZCB0byB0aGUgc2FtZSBpbXBsZW1lbnRhdGlvbiwgc28gd2hpY2hl"
    "dmVyIG9uZSB0aGUgY2xpZW50IGFjdHVhbGx5CiNzZW5kcyBpcyBzZXJ2ZWQuIHBhcnNlKCkgbm93"
    "IGxvZ3MgdGhlIHJhdyB0ZXh0IG9mIGFueXRoaW5nIHN0aWxsIHVubWF0Y2hlZCwKI3doaWNoIGlz"
    "IGhvdyB0byBjb25maXJtL3RyaW0gdGhpcyBsaXN0IGZyb20gYSByZWFsIHNlc3Npb24ncyBsb2cu"
    "CmRlZiBfdGVzdGNyZWF0ZWd1aWxkKG1kLHVzcixyZXMpOgogICAgI0NvbmZpcm1lZCBmcm9tIGEg"
    "bGl2ZSBjbGllbnQgY2FwdHVyZTogb3BlbmluZyB0aGUgZ3VpbGQgc2NyZWVuIHNlbmRzCiAgICAj"
    "L2d1aWxkc2xhZGRlciwgYW5kIHR5cGluZyBhIG5hbWUgYW5kIHByZXNzaW5nIGNyZWF0ZSBzZW5k"
    "cwogICAgIy90ZXN0Y3JlYXRlZ3VpbGQgIjxuYW1lPiIuIFRoZSBjbGllbnQgdGhlbiB3YWl0cyBm"
    "b3IgdGhlIHNlcnZlciB0byBzYXkKICAgICN3aGV0aGVyIHRoYXQgbmFtZSBjYW4gYmUgdXNlZCAt"
    "IHdpdGggbm8gYW5zd2VyIGl0IHdhaXRzIGZvcmV2ZXIsIHdoaWNoIGlzCiAgICAjd2hhdCB0aGUg"
    "Imd1aWxkIGNyZWF0aW9uIGhhbmdzIiByZXBvcnQgd2FzLiBFdmVyeSBndWlsZCBjb21tYW5kIG5h"
    "bWUKICAgICNndWVzc2VkIGJlZm9yZSB0aGlzIGNhcHR1cmUgKCAvY3JlYXRlZ3VpbGQsIC9qb2lu"
    "Z3VpbGQsIC4uLiApIHdhcyB3cm9uZzsKICAgICN0aGlzIG9uZSBjb21lcyBmcm9tIHRoZSB3aXJl"
    "LgogICAgbmFtZSA9IHNhbml0aXplVGV4dChyZXNbMV0pLnN0cmlwKCkKICAgIGZyZWUgPSAxIGlm"
    "IEdESC5ndWlsZE5hbWVGcmVlKG5hbWUpIGVsc2UgMAogICAgcHJpbnQoZidbTG9iYnldIHt1c3Iu"
    "dXNlci5uYW1lfSBjaGVja2VkIGd1aWxkIG5hbWUgIntuYW1lfSI6ICcKICAgICAgICAgIGYneyJh"
    "dmFpbGFibGUiIGlmIGZyZWUgZWxzZSAicmVqZWN0ZWQifScpCiAgICAjRWNoby1wbHVzLWZsYWcs"
    "IHRoZSBzYW1lIHNoYXBlIHRoZSBjbGllbnQgYWxyZWFkeSBhY2NlcHRzIGZyb20KICAgICMvcmVx"
    "dWVzdGpvaW5nYW1lY2hhbm5lbCAoIjEiIGdvIGFoZWFkIC8gIjAiIG5vKS4KICAgIHJldHVybiBf"
    "ZW0oZicvdGVzdGNyZWF0ZWd1aWxkICJ7bmFtZX0iICJ7ZnJlZX0iJykKZGVmIF9ndWlsZHNsYWRk"
    "ZXIobWQsdXNyLHJlcyk6CiAgICAjU2VudCB3aGVuIHRoZSBndWlsZCBzY3JlZW4gb3BlbnMuIFRo"
    "ZSBsYXlvdXQgb2YgYW4gaW5kaXZpZHVhbCBsYWRkZXIKICAgICNlbnRyeSBpcyBub3Qga25vd24s"
    "IGFuZCB0aGlzIGNsaWVudCBpcyBmcmFnaWxlIGVub3VnaCB0aGF0IGludmVudGluZyBvbmUKICAg"
    "ICNyaXNrcyB0YWtpbmcgaXQgZG93biAtIHNvIHRoZSBhbnN3ZXIgaXMgYW4gaG9uZXN0IGVtcHR5"
    "IGxhZGRlciwgd2hpY2ggaXMKICAgICNhbHNvIHRoZSB0cnV0aGZ1bCBvbmUgdW50aWwgZ3VpbGRz"
    "IGNhbiBhY3R1YWxseSBiZSBjcmVhdGVkLiBUaGUgY291bnQKICAgICNjb21lcyBsYXN0LCBtYXRj"
    "aGluZyAvam9pbmdhbWVjaGFubmVsJ3MgZWNoby1wbHVzLWNvdW50IHJlcGx5LgogICAgcGFnZSA9"
    "IHNhbml0aXplVGV4dChyZXNbMV0pIGlmIGxlbihyZXMpID4gMSBlbHNlICcxJwogICAgcmV0dXJu"
    "IF9lbShmJy9ndWlsZHNsYWRkZXIgIntwYWdlfSIgIjAiJykKZGVmIF9sYWRkZXIobWQsdXNyLHJl"
    "cyk6CiAgICAjU2VlbiBvbmNlIG9uIHRoZSB3aXJlLCByaWdodCBhZnRlciBhIHN1Y2Nlc3NmdWwg"
    "L2pvaW5ndWlsZCwgd2l0aCBubwogICAgI2FyZ3VtZW50cyBjYXB0dXJlZCAtIHByb2JhYmx5IGEg"
    "c2VydmVyLXdpZGUgbGVhZGVyYm9hcmQgcmF0aGVyIHRoYW4gYQogICAgI2d1aWxkIG9uZS4gSXRz"
    "IHJlcGx5IHNoYXBlIGlzIG5vdCBrbm93bi4gRXZlcnkgb3RoZXIgY29tbWFuZCBpbiB0aGlzCiAg"
    "ICAjZmlsZSB0aGF0IHJlYWNoZWQgdGhpcyBzdGF0ZSB3YXMgYW5zd2VyZWQgYnkgbWF0Y2hpbmcg"
    "YSBzaGFwZSB0aGUgY2xpZW50CiAgICAjaGFkIGFscmVhZHkgYmVlbiBzZWVuIGFjY2VwdGluZyBl"
    "bHNld2hlcmUgKGVjaG8rZmxhZywgZWNobytjb3VudCk7IHRoZXJlCiAgICAjaXMgbm8gc3VjaCBw"
    "cmVjZWRlbnQgZm9yIHRoaXMgb25lLiBHdWVzc2luZyBhIGZpZWxkIGxheW91dCByaXNrcyBmZWVk"
    "aW5nCiAgICAjdGhpcyBjbGllbnQgZGF0YSBpdCBkb2VzIG5vdCBleHBlY3QsIGFuZCBpdCBoYXMg"
    "YWxyZWFkeSBzaG93biBpdHNlbGYKICAgICN3aWxsaW5nIHRvIGNyYXNoIG9uIGJhZCBpbnB1dCBy"
    "YXRoZXIgdGhhbiByZWplY3QgaXQgZ3JhY2VmdWxseSAtIGEgd29yc2UKICAgICNvdXRjb21lIHRo"
    "YW4gYSBVSSBlbGVtZW50IHRoYXQgc3RheXMgZW1wdHkuIFJlZ2lzdGVyZWQgc28gaXQgc3RvcHMK"
    "ICAgICNzaG93aW5nIHVwIGFzIGFuIHVua25vd24gY29tbWFuZDsgZGVsaWJlcmF0ZWx5IGFuc3dl"
    "cmVkIHdpdGggbm90aGluZwogICAgI3VudGlsIGEgY2FwdHVyZSBzaG93cyB3aGF0IHJlcGx5IGl0"
    "IGFjdHVhbGx5IHdhaXRzIGZvci4KICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0g"
    "c2VudCAvbGFkZGVyIHtyZXNbMTpdIXJ9IC0gbm90IGFuc3dlcmVkLCAnCiAgICAgICAgIGYnc2hh"
    "cGUgdW5rbm93biAoc2VlIGNvbW1lbnQgYWJvdmUgX2xhZGRlciknKQogICAgcmV0dXJuIE5vbmUK"
    "ZGVmIF9qb2luZ3VpbGQobWQsdXNyLHJlcyk6CiAgICAjQ2FwdHVyZWQgZnJvbSB0aGUgcmV0YWls"
    "IGNsaWVudDogYWZ0ZXIgL3Rlc3RjcmVhdGVndWlsZCBhbnN3ZXJzIHRoYXQgYQogICAgI25hbWUg"
    "aXMgZnJlZSwgdGhlIGNsaWVudCBjcmVhdGVzIHRoZSBndWlsZCBieSBzZW5kaW5nCiAgICAjL2pv"
    "aW5ndWlsZCAiPG5hbWU+IiAiMSIgIjEiLiBTbyB0aGlzIG9uZSBjb21tYW5kIGNvdmVycyBib3Ro"
    "IGNyZWF0aW5nIGFuZAogICAgI2pvaW5pbmcsIGFuZCB3aGljaCBpdCBpcyBmb2xsb3dzIGZyb20g"
    "d2hldGhlciB0aGUgZ3VpbGQgYWxyZWFkeSBleGlzdHMgLQogICAgI3RoZSB0cmFpbGluZyBmbGFn"
    "cyBhcmUgbm90IG5lZWRlZCB0byB0ZWxsIHRoZW0gYXBhcnQuIEFuc3dlcmluZyBub3RoaW5nCiAg"
    "ICAjaGVyZSBpcyB3aGF0IGxlZnQgdGhlIGd1aWxkIGRpYWxvZyBzcGlubmluZy4KICAgIG5hbWUg"
    "PSBzYW5pdGl6ZVRleHQocmVzWzFdKS5zdHJpcCgpCiAgICBpZiBHREguZ3VpbGRFeGlzdHMobmFt"
    "ZSk6CiAgICAgICAgZXJyID0gR0RILmpvaW5HdWlsZChuYW1lLCB1c3IudXNlci5uYW1lKQogICAg"
    "ICAgIGFjdGlvbiA9ICdqb2luZWQnCiAgICBlbHNlOgogICAgICAgIGVyciA9IEdESC5jcmVhdGVH"
    "dWlsZChuYW1lLCB1c3IudXNlci5uYW1lKSAjdmFsaWRhdGVzIHRoZSBuYW1lIGl0c2VsZgogICAg"
    "ICAgIGFjdGlvbiA9ICdmb3VuZGVkJwogICAgaWYgZXJyOgogICAgICAgIHJldHVybiBfZW0oZicv"
    "ZXJyb3Ige2Vycn0gIntuYW1lfSInKQogICAgI0Nhbm9uaWNhbCBzcGVsbGluZyBmcm9tIHRoZSBk"
    "YXRhYmFzZSwgd2hpY2ggbWF5IGRpZmZlciBpbiBjYXNlIGZyb20gd2hhdAogICAgI3dhcyB0eXBl"
    "ZC4KICAgIG5hbWUgPSBHREguZ2V0R3VpbGROYW1lKHVzci51c2VyLm5hbWUpIG9yIG5hbWUKICAg"
    "IHVzci51c2VyLmd1aWxkID0gc2FuaXRpemVUZXh0KG5hbWUpCiAgICBwcmludChmJ1tMb2JieV0g"
    "e3Vzci51c2VyLm5hbWV9IHthY3Rpb259IGd1aWxkICJ7bmFtZX0iJykKICAgICNSZS1hbm5vdW5j"
    "ZSB0aGUgcGxheWVyIHRvIHRoZWlyIHRvd24gc28gdGhlIG90aGVycyBwaWNrIHVwIHRoZSBuZXcg"
    "dGFnCiAgICAjd2l0aG91dCByZWxvZ2dpbmcuIFRoaXMgcmV1c2VzICRnYW1lY2hhbm5lbHVzZXIg"
    "LSBhIG1lc3NhZ2UgZm9ybWF0IHRoZQogICAgI2NsaWVudCBkZW1vbnN0cmFibHkgYWNjZXB0cyAt"
    "IHJhdGhlciB0aGFuIGludmVudGluZyBhIGd1aWxkLXNwZWNpZmljIG9uZS4KICAgIGNobmwgPSB1"
    "c3IudXNlci5nYW1lY2hhbm5lbAogICAgaWYgY2hubDoKICAgICAgICBtZC5hZGQoeyd0YXJnZXQn"
    "Ol93b1VzZXIoY2hubC51c2VybGlzdCwgdXNyKSwKICAgICAgICAgICAgICAgICdtZXNzYWdlJzp1"
    "c3IudXNlci5nZXRHQ1Vtc2coKX0pCiAgICAjRWNobyBwbHVzIG1lbWJlciBjb3VudCwgdGhlIHNo"
    "YXBlIC9qb2luZ2FtZWNoYW5uZWwgYWxyZWFkeSByZXBsaWVzIHdpdGguCiAgICByZXR1cm4gX2Vt"
    "KGYnL2pvaW5ndWlsZCAie25hbWV9IiAie2xlbihHREguZ2V0R3VpbGRNZW1iZXJzKG5hbWUpKX0i"
    "JykKI1RoZSByb29tIG5hbWUgaXMgdHlwZWQgYnkgYSBwbGF5ZXIgYW5kIGlzIHRoZW4gYnJvYWRj"
    "YXN0IHRvIGV2ZXJ5b25lIGJyb3dzaW5nCiN0aGUgdG93biBpbnNpZGUgYSBxdW90ZWQgJGdhbWUg"
    "ZmllbGQuIEl0IHdhcyBwYXNzZWQgdGhyb3VnaCB1bnRvdWNoZWQ6IGEgJyInIGluCiNpdCBmb3Jn"
    "ZWQgcHJvdG9jb2wgZmllbGRzIGZvciBldmVyeSBvdGhlciBjbGllbnQsIGFuZCBpdHMgbGVuZ3Ro"
    "IHdhcyB1bmJvdW5kZWQuCiNCb3RoIGhhbmRsZXJzIG11c3QgZm9sZCBpdCBpZGVudGljYWxseSAt"
    "IHRoZSBuYW1lIGlzIGFsc28gdGhlIGRpY3Rpb25hcnkga2V5CiN0aGUgY3JlYXRlIHJlcXVlc3Qg"
    "aXMgbGF0ZXIgbWF0Y2hlZCBhZ2FpbnN0LCBzbyBhbnkgZGlmZmVyZW5jZSBiZXR3ZWVuIHRoZW0K"
    "I3dvdWxkIHR1cm4gYSBsZWdpdGltYXRlIGNyZWF0aW9uIGludG8gImdhbWVOYW1lVGFrZW4iLgpk"
    "ZWYgX2dhbWVOYW1lKHJhdyk6CiAgICByZXR1cm4gc2FuaXRpemVUZXh0KHJhdywgX01BWF9HQU1F"
    "TkFNRSkKZGVmIF9yZXF1ZXN0Y3JlYXRlZ2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3Iu"
    "dXNlci5nYW1lY2hhbm5lbDoKICAgICAgICByZXR1cm4gTm9uZSAjbm90IGluIGEgZ2FtZSBjaGFu"
    "bmVsIC0gdXNlZCB0byByYWlzZSBBdHRyaWJ1dGVFcnJvciBvbgogICAgICAgICAgICAgICAgICAg"
    "ICNOb25lIGFuZCBraWxsIHRoZSBjb25uZWN0aW9uJ3MgaGFuZGxlciB0aHJlYWQKICAgIHJldHVy"
    "biB1c3IudXNlci5nYW1lY2hhbm5lbC5yZXF1ZXN0Q3JlYXRlR2FtZSh1c3IsIF9nYW1lTmFtZShy"
    "ZXNbMV0pKQpkZWYgX2NyZWF0ZUdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiBub3QgdXNyLnVzZXIu"
    "Z2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUgI3NlZSBfcmVxdWVzdGNyZWF0ZWdhbWUK"
    "ICAgIHJldHVybiB1c3IudXNlci5nYW1lY2hhbm5lbC5jcmVhdGVHYW1lKF9nYW1lTmFtZShyZXNb"
    "MV0pLCB1c3IsIHJlc1syXSwgcmVzWzNdLCByZXNbNF0sIHJlc1s1XSwgcmVzWzZdLCByZXNbN10s"
    "IHJlc1s4XSwgcmVzWzldKQpkZWYgX3N0b3BnYW1lKG1kLHVzcixyZXMpOgogICAgaWYgdXNyLnVz"
    "ZXIuZ2FtZToKICAgICAgICByZXR1cm4gdXNyLnVzZXIuZ2FtZS5yZW1vdmUodXNyKQogICAgI3By"
    "aW50KCdVc2VyIGlzIG5vdCBpbiBhIGdhbWUnKQogICAgcmV0dXJuIE5vbmUKZGVmIF9zdGFydGlu"
    "Z2dhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHJldHVybiB1"
    "c3IudXNlci5nYW1lLnN0YXJ0R2FtZSh1c3IpCiAgICByZXR1cm4gTm9uZSAjVE9ETyB3aGF0IGRv"
    "ZXMgdGhpcyBldmVuIGRvPwpkZWYgX3N0YXJ0Z2FtZShtZCx1c3IscmVzKToKICAgICNUT0RPIGhh"
    "bmRsZSBwcm9wZXJseQogICAgaWYgdXNyLnVzZXIuZ2FtZToKICAgICAgICBwYXNzCiAgICByZXR1"
    "cm4gTm9uZQpkZWYgX2dhbWVjb21tYW5kdG91c2VyKG1kLHVzcixyZXMpOgogICAgZGF0ID0gX1Jl"
    "YWRCbG9iKHVzciwgcmVzWzJdKQogICAgdGNvbiA9IHVzci5zZXJ2ZXIuZ2V0UGxheWVyKHJlc1sx"
    "XSkKICAgICNBbGxvdyBjb21tYW5kcyB0byBhbnkgY29ubmVjdGVkIHBsYXllciwgcmVnYXJkbGVz"
    "cyBvZiBzdGF0ZSwgdG8gc3VwcG9ydCBtb2RkZWQgdXNlcwogICAgaWYgbm90IHRjb246CiAgICAg"
    "ICAgI3ByaW50KCdQbGF5ZXI6JyxyZXNbMV0sJ2RvZXMgbm90IGV4aXN0PycpCiAgICAgICAgcmV0"
    "dXJuIE5vbmUKICAgICNUT0RPIGNvbnNpZGVyIG9wdGltaXNpbmcgdGhpcyBjb21tYW5kIGluIHBh"
    "cnRpY3VsYXIKICAgIGZ1bG1zZyA9IF9lbShmJy9nYW1lY29tbWFuZHRvdXNlciAie3Vzci51c2Vy"
    "Lm5hbWV9IiAie2xlbihkYXQpfSInKStkYXQKICAgICNTdHJhaWdodCBvbnRvIHRoZSByZWNpcGll"
    "bnQncyBvd24gb3V0Ym91bmQgcXVldWUgaW5zdGVhZCBvZiB2aWEgdGhlCiAgICAjc2VydmVyLXdp"
    "ZGUgTWVzc2FnZURpc3RyaWJ1dG9yLiBUaGlzIGlzIHRoZSBjb21tYW5kIHRoYXQgY2FycmllcyB0"
    "aGUKICAgICNhY3R1YWwgaW4tZ2FtZSB0cmFmZmljIGJldHdlZW4gdHdvIHBsYXllcnMsIGl0IGFs"
    "d2F5cyBoYXMgZXhhY3RseSBvbmUKICAgICNyZWNpcGllbnQsIGFuZCBzZW5kKCkgaXMganVzdCBh"
    "IHF1ZXVlIHB1dCAtIHNvIHRoZSBkaXN0cmlidXRvciBob3AgYm91Z2h0CiAgICAjbm90aGluZyBi"
    "dXQgbGF0ZW5jeS4gV29yc2UsIHRoYXQgc2luZ2xlIGRpc3RyaWJ1dG9yIHRocmVhZCBpcyBzaGFy"
    "ZWQgYnkKICAgICNldmVyeSBjb25uZWN0aW9uIG9uIHRoZSBzZXJ2ZXI6IG9uZSBzbG93IGZhbi1v"
    "dXQgKGEgcG9zaXRpb24gYnJvYWRjYXN0IHRvCiAgICAjYSBmdWxsIHRvd24sIGEgaGVyb2RhdGEg"
    "YmxvYikgcXVldWVkIGFoZWFkIG9mIGEgZ2FtZSBjb21tYW5kIGRlbGF5ZWQgaXQKICAgICNmb3Ig"
    "ZXZlcnlvbmUuIERpcmVjdCBoYW5kLW9mZiByZW1vdmVzIGJvdGggdGhlIGV4dHJhIHRocmVhZCB3"
    "YWtlLXVwIGFuZAogICAgI3RoYXQgaGVhZC1vZi1saW5lIGJsb2NraW5nLCBhbmQgcmVsYXkgb3Jk"
    "ZXIgYmV0d2VlbiBhbnkgZ2l2ZW4gcGFpciBvZgogICAgI3BsYXllcnMgaXMgc3RpbGwgcHJlc2Vy"
    "dmVkIGJlY2F1c2UgdGhleSBhbGwgdGFrZSB0aGlzIHNhbWUgcGF0aC4KICAgIHRjb24uc2VuZChm"
    "dWxtc2cpCiAgICByZXR1cm4gTm9uZQpkZWYgX2pvaW5nYW1lKG1kLHVzcixyZXMpOgogICAgaWYg"
    "bm90IHVzci51c2VyLmdhbWVjaGFubmVsOgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5r"
    "bm93bkdhbWUgIntyZXNbMV19IicpICNub3QgaW4gYSBnYW1lIGNoYW5uZWwKICAgIGdtID0gdXNy"
    "LnVzZXIuZ2FtZWNoYW5uZWwuZ2FtZXMuZ2V0KF9nYW1lTmFtZShyZXNbMV0pLE5vbmUpCiAgICBp"
    "ZiBnbSA9PSBOb25lOgogICAgICAgICNBbnN3ZXIsIGRvbid0IGlnbm9yZTogdGhlIGNsaWVudCBp"
    "cyBzaXR0aW5nIG9uIGEgImNvbm5lY3RpbmciIGRpYWxvZwogICAgICAgICN0aGF0IG9ubHkgYSBy"
    "ZXBseSBkaXNtaXNzZXMuIEhhcHBlbnMgd2hlbmV2ZXIgdGhlIHJvb20gaXMgdG9ybiBkb3duCiAg"
    "ICAgICAgI2JldHdlZW4gdGhlIHBsYXllciBzZWVpbmcgaXQgaW4gdGhlIGxpc3QgYW5kIGNsaWNr"
    "aW5nIGl0LgogICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgdW5rbm93bkdhbWUgIntyZXNbMV19"
    "IicpCiAgICAjVGhlIHBhc3N3b3JkIGFyZ3VtZW50IGlzIGFic2VudCB3aGVuIHRoZSByb29tIGhh"
    "cyBub25lIC0gc2VlIHRoZSBhcml0eQogICAgI25vdGUgb24gX0NPTU1BTkRTLgogICAgcmV0dXJu"
    "IGdtLmFkZFVzZXIodXNyLCByZXNbMl0gaWYgbGVuKHJlcyk+MiBlbHNlICcnKQpkZWYgX3dob2lz"
    "KG1kLHVzcixyZXMpOgogICAgaWYgbGVuKHJlcyk8MjoKICAgICAgICByZXR1cm4gTm9uZQogICAg"
    "dGFyZ2V0ID0gcmVzWzFdCiAgICBpbmZvID0gR0RILmdldFdob2lzKHRhcmdldCkKICAgIGlmIGlu"
    "Zm8gaXMgTm9uZToKICAgICAgICByZXR1cm4gTm9uZSAjdW5rbm93biB1c2VyCiAgICB0Y29uID0g"
    "dXNyLnNlcnZlci5nZXRQbGF5ZXIodGFyZ2V0KQogICAgdG93biA9IHRjb24udXNlci5nYW1lY2hh"
    "bm5lbC5uYW1lIGlmICh0Y29uIGFuZCB0Y29uLnVzZXIuZ2FtZWNoYW5uZWwpIGVsc2UgJycKICAg"
    "IGNoYXRjaGFubmVsID0gJycKICAgIGlmIHRjb24gYW5kIHRjb24udXNlci5jaGF0Y2hhbm5lbDoK"
    "ICAgICAgICAjVGhlIHRhcmdldCdzIGNoYXQgY2hhbm5lbCBpcyBhIHBsYWluIGxpc3QsIHNvIGl0"
    "IGlzIGlkZW50aWZpZWQgYnkKICAgICAgICAjc2VhcmNoaW5nIGZvciB0aGUgb2JqZWN0LiBTdG9w"
    "IGF0IHRoZSBmaXJzdCBtYXRjaCBpbnN0ZWFkIG9mIHdhbGtpbmcKICAgICAgICAjZXZlcnkgY2hh"
    "bm5lbCBvZiBldmVyeSB0b3duIGFmdGVyd2FyZHMgLSBhbmQgdGFrZSB0aGUgbmFtZSBmcm9tIHRo"
    "ZQogICAgICAgICN0b3duIHRoZSBwbGF5ZXIgaXMgYWN0dWFsbHkgaW4sIHdoaWNoIHRoZSB1bmJy"
    "b2tlbiBsb29wIGNvdWxkIG92ZXJ3cml0ZQogICAgICAgICN3aXRoIGEgbGF0ZXIgdG93bidzIGlk"
    "ZW50aWNhbGx5LW5hbWVkIGNoYW5uZWwuCiAgICAgICAgZm9yIGNobiBpbiBsaXN0KHVzci5zZXJ2"
    "ZXIuc3RhdGUuZ2FtZUNoYW5uZWxzLnZhbHVlcygpKToKICAgICAgICAgICAgZm9yIGNuYW1lLCB1"
    "bGlzdCBpbiBsaXN0KGNobi5jaGF0Q2hhbm5lbHMuaXRlbXMoKSk6CiAgICAgICAgICAgICAgICBp"
    "ZiB1bGlzdCBpcyB0Y29uLnVzZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgICAgICAgICAgICAgY2hh"
    "dGNoYW5uZWwgPSBjbmFtZQogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgICAgIGlm"
    "IGNoYXRjaGFubmVsOgogICAgICAgICAgICAgICAgYnJlYWsKICAgIGd1aWxkID0gc2FuaXRpemVU"
    "ZXh0KEdESC5nZXRHdWlsZE5hbWUodGFyZ2V0KSkKICAgICNDYXBwZWQgYWdhaW4gb24gdGhlIHdh"
    "eSBvdXQsIG5vdCBvbmx5IG9uIHRoZSB3YXkgaW46IHJvd3Mgd3JpdHRlbiBiZWZvcmUKICAgICMv"
    "dXBkYXRlIHdhcyBib3VuZGVkIGFyZSBzdGlsbCBpbiB0aGUgZGF0YWJhc2UsIGFuZCB0aGlzIGlz"
    "IHRoZSBtZXNzYWdlIHRoYXQKICAgICNoYW5kcyB0aGVtIHRvIGEgKmRpZmZlcmVudCogcGxheWVy"
    "J3MgY2xpZW50LgogICAgcmV0dXJuIF9lbSgKICAgICAgICBmJy93aG9pcyAie3RhcmdldH0iICJ7"
    "Z3VpbGR9IiAie3Nhbml0aXplVGV4dCh0b3duKX0iICJ7c2FuaXRpemVUZXh0KGNoYXRjaGFubmVs"
    "KX0iICcKICAgICAgICBmJyJ7c2FuaXRpemVUZXh0KGluZm9bImVtYWlsIl0sIF9NQVhfV0hPSVNf"
    "RklFTEQpfSIgJwogICAgICAgIGYnIntzYW5pdGl6ZVRleHQoaW5mb1sibG9jYXRpb24iXSwgX01B"
    "WF9XSE9JU19GSUVMRCl9IiAnCiAgICAgICAgZid7aW5mb1siYWdlIl19IHtpbmZvWyJnZW5kZXIi"
    "XX0gIntzYW5pdGl6ZVRleHQoaW5mb1siZGVzY3JpcHRpb24iXSwgX01BWF9ERVNDUklQVElPTil9"
    "IicKICAgICkKZGVmIF91cGRhdGUobWQsdXNyLHJlcyk6CiAgICAjL3VwZGF0ZSAibmFtZSIgImVt"
    "YWlsIiAibG9jYXRpb24iICJhZ2UiICJnZW5kZXIiICJkZXNjcmlwdGlvbiIKICAgIGlmIGxlbihy"
    "ZXMpPDY6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIHJlc1sxXSAhPSB1c3IudXNlci5uYW1l"
    "OgogICAgICAgIHJldHVybiBOb25lICNjYW4gb25seSB1cGRhdGUgb3duIHdob2lzIGluZm8KICAg"
    "IGVtYWlsID0gc2FuaXRpemVUZXh0KHJlc1syXSwgX01BWF9XSE9JU19GSUVMRCkKICAgIGxvY2F0"
    "aW9uID0gc2FuaXRpemVUZXh0KHJlc1szXSwgX01BWF9XSE9JU19GSUVMRCkKICAgIGFnZSA9IHJl"
    "c1s0XQogICAgZ2VuZGVyID0gcmVzWzVdCiAgICBkZXNjcmlwdGlvbiA9IHNhbml0aXplVGV4dChy"
    "ZXNbNl0sIF9NQVhfREVTQ1JJUFRJT04pIGlmIGxlbihyZXMpPjYgZWxzZSAnJwogICAgR0RILnVw"
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
    "ICAgICAgICAgICAgICAoX3VwZGF0ZSwgNSksCiAgICAjQXJpdGllcyBiZWxvdyBhcmUgdGhlIGNs"
    "aWVudCdzIG93biwgZnJvbSBpdHMgZm9ybWF0IHRhYmxlIC0gc2VlIHRoZSBibG9jawogICAgI29m"
    "IGhhbmRsZXJzIGFib3ZlLiAvbXNnJ3MgbGF5b3V0IGlzIG5vdCBpbiB0aGF0IHRhYmxlICh0aGUg"
    "Y2xpZW50IGJ1aWxkcyBpdAogICAgI2J5IGNvbmNhdGVuYXRpb24sIGxpa2UgL3NlbmQpLCBzbyAy"
    "IGlzIHRoZSBzbWFsbGVzdCBzYW5lIHJlcXVpcmVtZW50LgogICAgJy9nYW1lY2hhbm5lbHNsaXN0"
    "JzogICAgICAgKF9nYW1lY2hhbm5lbHNsaXN0LCAwKSwKICAgICcvam9pbmNoYXRjaGFubmVsJzog"
    "ICAgICAgIChfam9pbmNoYXRjaGFubmVsLCAxKSwKICAgICcvbXNnJzogICAgICAgICAgICAgICAg"
    "ICAgIChfbXNnLCAyKSwKICAgICcvc2V0Z2FtZXBhcmFtcyc6ICAgICAgICAgIChfc2V0Z2FtZXBh"
    "cmFtcywgMiksCiAgICAnL25ld2dhbWVob3N0JzogICAgICAgICAgICAoX25ld2dhbWVob3N0LCAx"
    "KSwKICAgICNHdWlsZHMuIEV2ZXJ5IG5hbWUgaGVyZSBoYXMgYmVlbiBzZWVuIG9uIHRoZSB3aXJl"
    "IGZyb20gdGhlIHJldGFpbCBjbGllbnQuCiAgICAjVGhlIGJhdGNoIG9mIGd1ZXNzZWQgc3BlbGxp"
    "bmdzIHRoYXQgdXNlZCB0byBzaXQgYWxvbmdzaWRlIHRoZW0KICAgICMoL2NyZWF0ZWd1aWxkLCAv"
    "cmVxdWVzdGNyZWF0ZWd1aWxkLCAvY3JlYXRndWlsZCwgL2d1aWxkY3JlYXRlLAogICAgIy9yZXF1"
    "ZXN0am9pbmd1aWxkLCAvcXVpdGd1aWxkLCAvZ2V0Z3VpbGRpbmZvKSBpcyBnb25lOiB0aGUgY2Fw"
    "dHVyZSBzaG93ZWQKICAgICN0aGUgY2xpZW50IHNlbmRzIG5vbmUgb2YgdGhlbSwgYW5kIHRoYXQg"
    "L2pvaW5ndWlsZCBpcyB3aGF0IGNyZWF0ZXMgYQogICAgI2d1aWxkLiBMZWF2aW5nIGEgZ3VpbGQg"
    "aGFzIG5vdCBiZWVuIG9ic2VydmVkIHlldCwgc28gbm8gaGFuZGxlciBpcwogICAgI3JlZ2lzdGVy"
    "ZWQgZm9yIGl0IC0gdGhlIHJlYWwgbmFtZSB3aWxsIHNob3cgdXAgaW4gdGhlIGxvZyBhcyBhbiB1"
    "bmtub3duCiAgICAjY29tbWFuZCB0aGUgZmlyc3QgdGltZSBzb21lYm9keSB0cmllcy4KICAgICcv"
    "Z3VpbGRzbGFkZGVyJzogICAgICAgICAgIChfZ3VpbGRzbGFkZGVyLCAxKSwKICAgICcvdGVzdGNy"
    "ZWF0ZWd1aWxkJzogICAgICAgIChfdGVzdGNyZWF0ZWd1aWxkLCAxKSwKICAgICcvam9pbmd1aWxk"
    "JzogICAgICAgICAgICAgIChfam9pbmd1aWxkLCAxKSwKICAgICcvbGFkZGVyJzogICAgICAgICAg"
    "ICAgICAgIChfbGFkZGVyLCAwKSwKfQpjbGFzcyBDb21tYW5kUGFyc2VyKCk6CiAgICBkZWYgX19p"
    "bml0X18oc2VsZiwgbXNnZXIpOgogICAgICAgIHNlbGYuY29tbWFuZGxpc3QgPSBfQ09NTUFORFMK"
    "ICAgICAgICBzZWxmLm1kID0gbXNnZXIKCiAgICBkZWYgcGFyc2Uoc2VsZiwgZGF0YSwgb3JpZ2lu"
    "KToKICAgICAgICAjcHJpbnQoZidUZXN0IFBhcnNpbmcge2xlbihkYXRhKX06IHtieXRlcyhkYXRh"
    "LCAnYXNjaWknKX0nKQogICAgICAgIHJlcyA9IGxpc3QoIChpdG1bMF0raXRtWzFdIGZvciBpdG0g"
    "aW4gX1JFX0NNRC5maW5kYWxsKGRhdGEpKSApCiAgICAgICAgI3ByaW50KCdSZXM6JywgcmVzKQog"
    "ICAgICAgIGlmIG5vdCByZXM6CiAgICAgICAgICAgICNXYXMgYSBzaWxlbnQgZHJvcC4gSWYgYSBm"
    "ZWF0dXJlIGRvZXMgbm90aGluZyBhbmQgdGhlIGxvZyBzaG93cyBubwogICAgICAgICAgICAjY29t"
    "bWFuZCBmb3IgaXQgYXQgYWxsLCB0aGlzIGlzIG9uZSBvZiB0aGUgdHdvIHBsYWNlcyBpdCBjb3Vs"
    "ZAogICAgICAgICAgICAjaGF2ZSBkaXNhcHBlYXJlZCBpbnRvIC0gc28gc2F5IHNvIHJhdGhlciB0"
    "aGFuIGxlYXZlIGEgYmxpbmQgc3BvdC4KICAgICAgICAgICAgaWYgX0RFQlVHX0xPR19DT01NQU5E"
    "UyBhbmQgZGF0YToKICAgICAgICAgICAgICAgIHdobyA9IG9yaWdpbi51c2VyLm5hbWUgaWYgb3Jp"
    "Z2luLnVzZXIgZWxzZSAnPycKICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gLT4g"
    "KFVOUEFSU0VBQkxFKSB7ZGF0YSFyfScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAgICAg"
    "d2hvID0gb3JpZ2luLnVzZXIubmFtZSBpZiBvcmlnaW4udXNlciBlbHNlICc/JwogICAgICAgIGxv"
    "dWQgPSBfREVCVUdfTE9HX0NPTU1BTkRTIGFuZCAoX0RFQlVHX0xPR19WRVJCT1NFIG9yIHJlc1sw"
    "XSBub3QgaW4gX1FVSUVUX0NPTU1BTkRTKQogICAgICAgIGlmIGxvdWQ6CiAgICAgICAgICAgIHBy"
    "aW50KGYnW2NtZF0ge3dob30gLT4ge2RhdGF9JykKICAgICAgICBlbnRyeSA9IHNlbGYuY29tbWFu"
    "ZGxpc3QuZ2V0KHJlc1swXSkKICAgICAgICBpZiBlbnRyeSBpcyBOb25lOgogICAgICAgICAgICAj"
    "TG9nIHRoZSByYXcgbGluZSwgbm90IGp1c3QgdGhlIHRva2VuaXNlZCBsaXN0LiBBbiB1bmltcGxl"
    "bWVudGVkCiAgICAgICAgICAgICNjb21tYW5kIGlzIGV4YWN0bHkgdGhlIHNpdHVhdGlvbiB3aGVy"
    "ZSB0aGUgYXJndW1lbnQgbGF5b3V0IGlzCiAgICAgICAgICAgICN3aGF0IHdlIG5lZWQgdG8gc2Vl"
    "LCBhbmQgcmUtcXVvdGluZyB0aGUgc3BsaXQgdG9rZW5zIGxvc2VzIGl0LgogICAgICAgICAgICBw"
    "cmludChmJyoqKiBVTktOT1dOIENPTU1BTkQgZnJvbSB7d2hvfToge2RhdGEhcn0nKQogICAgICAg"
    "ICAgICByZXR1cm4gTm9uZQogICAgICAgIGhhbmRsZXIsIG1pbmFyZ3MgPSBlbnRyeQogICAgICAg"
    "IGlmIGxlbihyZXMpIC0gMSA8IG1pbmFyZ3M6CiAgICAgICAgICAgIHByaW50KGYnKioqIE1BTEZP"
    "Uk1FRCBDT01NQU5EIGZyb20ge3dob306ICcKICAgICAgICAgICAgICAgICAgZid7cmVzWzBdfSBu"
    "ZWVkcyB7bWluYXJnc30gYXJndW1lbnQocyksIGdvdCB7bGVuKHJlcyktMX0nKQogICAgICAgICAg"
    "ICByZXR1cm4gTm9uZQogICAgICAgICNwcmludChmJ1BhcnNlZCBDb21tYW5kIEZyb20ge29yaWdp"
    "bi51c2VyLm5hbWV9OicsIHJlcykKICAgICAgICBvdXQgPSBoYW5kbGVyKHNlbGYubWQsIG9yaWdp"
    "biwgcmVzKQogICAgICAgIGlmIGxvdWQ6CiAgICAgICAgICAgICMiKG5vIGRpcmVjdCByZXBseSki"
    "IGlzIHRoZSBzaWduYXR1cmUgb2YgZXZlcnkgaGFuZyByZXBvcnRlZCBzbwogICAgICAgICAgICAj"
    "ZmFyOiB0aGUgY2xpZW50IHdhaXRzIG9uIGFuIGFuc3dlciB0aGF0IHRoaXMgc2VydmVyIG5ldmVy"
    "IHNlbmRzLgogICAgICAgICAgICAjU29tZSBjb21tYW5kcyBsZWdpdGltYXRlbHkgYW5zd2VyIHdp"
    "dGggbm90aGluZywgc28gdGhpcyBpcyBhIGxlYWQsCiAgICAgICAgICAgICNub3QgYSB2ZXJkaWN0"
    "IC0gYnV0IGl0IGlzIHRoZSBmaXJzdCB0aGluZyB0byBsb29rIGF0LgogICAgICAgICAgICBpZiBv"
    "dXQ6CiAgICAgICAgICAgICAgICBoZWFkID0gb3V0LnNwbGl0KF9OKVswXS5kZWNvZGUoX1dJUkVf"
    "RU5DLCAncmVwbGFjZScpCiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IDwtIHto"
    "ZWFkfScpCiAgICAgICAgICAgIGVsc2U6CiAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3"
    "aG99IDwtIChubyBkaXJlY3QgcmVwbHkpJykKICAgICAgICByZXR1cm4gb3V0CgojdGhyZWFkIHRv"
    "IHNlbmQgbWVzc2FnZXMgYWNyb3NzIGFsbCBjb25uZWN0ZWQgY2xpZW50cwojX19FWEFNUExFX01F"
    "U1NBR0VfXyA9IHsKIyAgICAndGFyZ2V0JzpbJ3VzZXJsaXN0J10sCiMgICAgJ21lc3NhZ2UnOmIn"
    "L3doYXRldmVyXDAnK2InYmxvYicKI30KY2xhc3MgTWVzc2FnZURpc3RyaWJ1dG9yKCk6CiAgICBf"
    "RU5ESVRFTSA9IFsnU1RPUCddCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVyKToKICAgICAg"
    "ICBzZWxmLl9jUXVldWUgPSBTaW1wbGVRdWV1ZSgpCiAgICAgICAgc2VsZi5zZXJ2ZXIgPSBzZXJ2"
    "ZXIKICAgIGRlZiBzZXJ2ZV9mb3JldmVyKHNlbGYpOgogICAgICAgIHdoaWxlIFRydWU6ICNUT0RP"
    "IHBvc3NpYmxlIGNoZWNrIHNlbGYuc2VydmVyLl9pc19jbG9zaW5nCiAgICAgICAgICAgIHRyeToK"
    "ICAgICAgICAgICAgICAgIGNvbW1hbmQgPSBzZWxmLl9jUXVldWUuZ2V0KCkKICAgICAgICAgICAg"
    "ICAgICNwcmludCgnTUQ6JywgY29tbWFuZCwgc2VsZi5zZXJ2ZXIuX2lzX2Nsb3NpbmcpCiAgICAg"
    "ICAgICAgICAgICBpZiBjb21tYW5kID09IHNlbGYuX0VORElURU06CiAgICAgICAgICAgICAgICAg"
    "ICAgYnJlYWsKICAgICAgICAgICAgICAgIHVsID0gY29tbWFuZC5nZXQoJ3RhcmdldCcsW10pCiAg"
    "ICAgICAgICAgICAgICBtc2cgPSBjb21tYW5kLmdldCgnbWVzc2FnZScpCiAgICAgICAgICAgICAg"
    "ICBpZiBtc2c6CiAgICAgICAgICAgICAgICAgICAgZm9yIHVzciBpbiB1bDoKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgdXNyLnNlbmQobXNnKQogICAgICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgog"
    "ICAgICAgICAgICAgICAgcHJpbnQoJ1tMb2JieV0gRGlzdHJpYnV0b3IgZXJyb3I6XG4nICsgdHJh"
    "Y2ViYWNrLmZvcm1hdF9leGMoKSkKICAgIGRlZiBhZGQoc2VsZiwgcHJvcHMpOgogICAgICAgICNT"
    "bmFwc2hvdCB0aGUgdGFyZ2V0IGxpc3QgSEVSRSwgaW4gdGhlIGNhbGxpbmcgdGhyZWFkLiBDYWxs"
    "ZXJzIGhhbmQgdXMKICAgICAgICAjbGl2ZSBjb250YWluZXJzIChHYW1lQ2hhbm5lbC51c2VybGlz"
    "dCwgc3RhdGUuYWN0aXZlVXNlcnMudmFsdWVzKCksIC4uLikKICAgICAgICAjdGhhdCBvdGhlciBo"
    "YW5kbGVyIHRocmVhZHMgYXBwZW5kIHRvL3JlbW92ZSBmcm9tIGNvbnRpbnVvdXNseTsgdGhlCiAg"
    "ICAgICAgI2Rpc3RyaWJ1dG9yIHRocmVhZCBpdGVyYXRlZCB0aGVtIGxhdGVyIGFuZCBoaXQgJ2xp"
    "c3QgY2hhbmdlZCBzaXplCiAgICAgICAgI2R1cmluZyBpdGVyYXRpb24nLCB3aGljaCB0aGUgZXhj"
    "ZXB0IGFib3ZlIHN3YWxsb3dlZCAtIHNpbGVudGx5CiAgICAgICAgI2Ryb3BwaW5nIHRoZSBlbnRp"
    "cmUgYnJvYWRjYXN0LiB1cGRhdGVQb3MoKSBkb2VzIHRoaXMgb25jZSBhIHNlY29uZCBmb3IKICAg"
    "ICAgICAjZXZlcnkgY2hhbm5lbCwgc28gdGhpcyB3YXMgdGhlIGhvdCBwYXRoIGZvciB0aGUgcmFj"
    "ZS4KICAgICAgICBpZiBpc2luc3RhbmNlKHByb3BzLCBkaWN0KToKICAgICAgICAgICAgcHJvcHMg"
    "PSBkaWN0KHByb3BzKQogICAgICAgICAgICBwcm9wc1sndGFyZ2V0J10gPSBsaXN0KHByb3BzLmdl"
    "dCgndGFyZ2V0Jykgb3IgKCkpCiAgICAgICAgc2VsZi5fY1F1ZXVlLnB1dChwcm9wcykKICAgIGRl"
    "ZiBlbmQoc2VsZik6CiAgICAgICAgc2VsZi5hZGQoc2VsZi5fRU5ESVRFTSkKICAgIApkZWYgX2Ns"
    "YW1wSW50KHJhdywgZGVmYXVsdCwgbG8sIGhpKToKICAgICNFdmVyeSBudW1lcmljIGZpZWxkIG9m"
    "IC9jcmVhdGVnYW1lIGFycml2ZXMgYXMgdGV4dCBzdHJhaWdodCBvZmYgdGhlIHdpcmUuCiAgICAj"
    "aW50KCkgb24gaXQgdXNlZCB0byByYWlzZSBWYWx1ZUVycm9yIGZvciBhbnl0aGluZyBub24tbnVt"
    "ZXJpYywgYW5kIHRoYXQKICAgICNleGNlcHRpb24gbGVmdCB0aGUgaGFuZGxlciwgdG9yZSBkb3du"
    "IHRoZSBob3N0J3MgY29ubmVjdGlvbiB0aHJlYWQgYW5kCiAgICAjbG9nZ2VkIGEgdHJhY2ViYWNr"
    "IC0gb25lIG1hbGZvcm1lZCByb29tIHJlcXVlc3QgZGlzY29ubmVjdGVkIHRoZSBwbGF5ZXIKICAg"
    "ICNtYWtpbmcgaXQuIFRoZSByYW5nZSBjaGVjayBpcyB0aGUgc2FtZSByZWFzb25pbmcgYXBwbGll"
    "ZCB0byB2YWx1ZXMgdGhhdCBkbwogICAgI3BhcnNlOiBtYXhwbGF5ZXJzIGNhbWUgZnJvbSB0aGUg"
    "Y2xpZW50IHRvbywgc28gYSByb29tIGNvdWxkIGFkdmVydGlzZQogICAgI2l0c2VsZiBhcyBob2xk"
    "aW5nIHR3byBiaWxsaW9uIHBlb3BsZS4KICAgIHRyeToKICAgICAgICB2YWwgPSBpbnQocmF3KQog"
    "ICAgZXhjZXB0IChUeXBlRXJyb3IsIFZhbHVlRXJyb3IpOgogICAgICAgIHJldHVybiBkZWZhdWx0"
    "CiAgICByZXR1cm4gbWluKG1heCh2YWwsIGxvKSwgaGkpCmNsYXNzIEdhbWVFbnRyeSgpOgogICAg"
    "ZGVmIF9faW5pdF9fKHNlbGYsIHBhcmVudCwgbmFtZSwgaG9zdCwgcGFzdywgbWFwcCwgbWFwdCwg"
    "bnBqLCB1bjEsIHN0YXR1cywgbWF4cGxheWVycywgdXJsKToKICAgICAgICBpZiBob3N0LnVzZXIu"
    "Z2FtZToKICAgICAgICAgICAgaG9zdC51c2VyLmdhbWUucmVtb3ZlKGhvc3QpCiAgICAgICAgc2Vs"
    "Zi5wYXJlbnQgPSBwYXJlbnQgIyBHYW1lY2hhbm5lbAogICAgICAgIHNlbGYuZ25hbWUgPSBuYW1l"
    "ICMKICAgICAgICBzZWxmLmhvc3QgPSBob3N0ICMgQ29ubmVjdGlvbiBPYmplY3QKICAgICAgICBz"
    "ZWxmLnBhc3N3b3JkID0gcGFzdyAjICcnIG9yICdwYXNzd29yZCcKICAgICAgICBzZWxmLm1hcFBh"
    "ciA9IG1hcHAgIyAiTmV0X01fMDEgbnVsbCAwIDEiCiAgICAgICAgc2VsZi5tYXBUcmFuc2xhdGUg"
    "PSBtYXB0ICMgInRyYW5zbGF0ZU5ldF9NXzAxIgogICAgICAgIHNlbGYubnBqID0gX2NsYW1wSW50"
    "KG5waiwgMCwgMCwgMSkgIyAiZW5hYmxlIG5ldyBwbGF5ZXIgdG8gam9pbiAoYm9vbCkiCiAgICAg"
    "ICAgc2VsZi51bjEgPSBfY2xhbXBJbnQodW4xLCAwLCAwLCBfMzJiaXQpICMgMCBUT0RPIGZpZ3Vy"
    "ZSBvdXQgaWYgbWVhbnMgImd1aWxkIGdhbWUiCiAgICAgICAgc2VsZi5zdGF0dXMgPSBfY2xhbXBJ"
    "bnQoc3RhdHVzLCAwLCAwLCAxKSAjIGNoYW5nZXMgdG8gMSB3aGVuIHN0YXJ0ZWQsIG9ubHkgcmVs"
    "ZXZhbnQgd2hlbiBucGogdHJ1ZQogICAgICAgIHNlbGYubWF4cGxheWVycyA9IF9jbGFtcEludCht"
    "YXhwbGF5ZXJzLCA4LCAxLCBHYW1lQ2hhbm5lbC5tYXh1c2VyKSAjIDggI21heCB1c2Vycz8KICAg"
    "ICAgICAjeC1kaXJlY3RwbGF5IHVybCwgd2l0aCB0aGUgaG9zdCdzIGFkdmVydGlzZWQgYWRkcmVz"
    "cyByZXBsYWNlZCBieSB0aGUKICAgICAgICAjYWRkcmVzcyB0aGlzIHNlcnZlciBzZWVzIGl0IGNv"
    "bm5lY3QgZnJvbSAtIHNlZSByZXdyaXRlR2FtZUhvc3QoKS4KICAgICAgICBwZWVyID0gaG9zdC5j"
    "bGllbnRfYWRkcmVzc1swXSBpZiBob3N0LmNsaWVudF9hZGRyZXNzIGVsc2UgJycKICAgICAgICAo"
    "c2VsZi51cmwsIG5vdGUpID0gcmV3cml0ZUdhbWVIb3N0KHVybCwgcGVlcikKICAgICAgICBwcmlu"
    "dChmJ1tMb2JieV0gUm9vbSAie25hbWV9IiBieSB7aG9zdC51c2VyLm5hbWV9OiB7bm90ZX0nKQog"
    "ICAgICAgIHByaW50KGYnW0xvYmJ5XSAgIHVybCBhZHZlcnRpc2VkIHRvIGpvaW5lcnM6IHtzZWxm"
    "LnVybH0nKQogICAgICAgIHNlbGYudXNlcmxpc3QgPSBbaG9zdCxdCiAgICAgICAgc2VsZi5wYXJl"
    "bnQuZ2FtZXNbc2VsZi5nbmFtZV0gPSBzZWxmCiAgICAgICAgc2VsZi5ob3N0LnVzZXIuZ2FtZSA9"
    "IHNlbGYKICAgICAgICAjQWR2ZXJ0aXNlIG9uIGNyZWF0aW9uCiAgICAgICAgbXNnID0gc2VsZi5n"
    "ZXRHYW1lU3RyaW5nKCkKICAgICAgICB0ZyA9IHNlbGYucGFyZW50LnVzZXJsaXN0CiAgICAgICAg"
    "c2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bXNnfSkK"
    "ICAgIGRlZiBfYXVkaWVuY2Uoc2VsZik6CiAgICAgICAgI1dobyBuZWVkcyB0byBoZWFyIGFib3V0"
    "IHRoaXMgcm9vbSBjaGFuZ2luZzogZXZlcnlvbmUgYnJvd3NpbmcgdGhlCiAgICAgICAgI3Rvd24s"
    "IHBsdXMgZXZlcnlvbmUgYWxyZWFkeSBpbnNpZGUgdGhlIHJvb20uIE9uY2UgYSBnYW1lIHN0YXJ0"
    "cyBpdHMKICAgICAgICAjcGxheWVycyBhcmUgdGFrZW4gb2ZmIHRoZSB0b3duIHJvc3RlciAoc2Vl"
    "IHN0YXJ0R2FtZSksIHNvIHRoZSB0b3duCiAgICAgICAgI2xpc3QgYWxvbmUgbm8gbG9uZ2VyIHJl"
    "YWNoZXMgdGhlbSAtIGFuZCB0aGUgaG9zdCwgd2hvIGlzIGFsd2F5cwogICAgICAgICNpbi1nYW1l"
    "LCBpcyBleGFjdGx5IHdobyBuZWVkcyB0byBrbm93IHRoYXQgc29tZWJvZHkgam9pbmVkLgogICAg"
    "ICAgIHNlZW4gPSBsaXN0KHNlbGYucGFyZW50LnVzZXJsaXN0KQogICAgICAgIGZvciBjIGluIHNl"
    "bGYudXNlcmxpc3Q6CiAgICAgICAgICAgIGlmIGMgbm90IGluIHNlZW46CiAgICAgICAgICAgICAg"
    "ICBzZWVuLmFwcGVuZChjKQogICAgICAgIHJldHVybiBzZWVuCiAgICBkZWYgYWRkVXNlcihzZWxm"
    "LCB1c3IsIHBhc3cpOgogICAgICAgICNFdmVyeSByZWplY3Rpb24gYmVsb3cgaGFzIHRvIGFuc3dl"
    "ciB0aGUgY2xpZW50IHdpdGggKnNvbWV0aGluZyouIFRoZQogICAgICAgICNjbGllbnQgc2hvd3Mg"
    "ImNvbm5lY3RpbmcuLi4iIGZyb20gdGhlIG1vbWVudCBpdCBzZW5kcyAvam9pbmdhbWUgdW50aWwK"
    "ICAgICAgICAjdGhlIHNlcnZlciBhbnN3ZXJzLCBhbmQgaXQgaGFzIG5vIHRpbWVvdXQgb2YgaXRz"
    "IG93bjogcmV0dXJuaW5nIE5vbmUKICAgICAgICAjbGVmdCB0aGUgcGxheWVyIHN0YXJpbmcgYXQg"
    "dGhhdCBkaWFsb2cgdW50aWwgdGhleSBraWxsZWQgdGhlIGdhbWUuCiAgICAgICAgaWYgdXNyIGlu"
    "IHNlbGYudXNlcmxpc3Q6CiAgICAgICAgICAgICNBbHJlYWR5IGluIChkdXBsaWNhdGUgL2pvaW5n"
    "YW1lLCBlLmcuIHRoZSBwbGF5ZXIgZG91YmxlLWNsaWNrZWQKICAgICAgICAgICAgI3RoZSByb29t"
    "KS4gUmUtYW5zd2VyIGluc3RlYWQgb2YgYXBwZW5kaW5nIHRoZW0gYSBzZWNvbmQgdGltZS4KICAg"
    "ICAgICAgICAgcmV0dXJuIF9lbShmJy9qb2luZ2FtZSAie3NlbGYuZ25hbWV9IiAie3NlbGYudXJs"
    "fSIgIntzZWxmLnN0YXR1c30iJykKICAgICAgICBpZiBsZW4oc2VsZi51c2VybGlzdCk+PXNlbGYu"
    "bWF4cGxheWVyczoKICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lRnVsbCAie3Nl"
    "bGYuZ25hbWV9IicpCiAgICAgICAgaWYgc2VsZi5zdGF0dXMgYW5kIG5vdCBzZWxmLm5wajoKICAg"
    "ICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lQWxyZWFkeVN0YXJ0ZWQgIntzZWxmLmdu"
    "YW1lfSInKQogICAgICAgIGlmIHNlbGYucGFzc3dvcmQgIT0gcGFzdzoKICAgICAgICAgICAgcmV0"
    "dXJuIF9lbShmJy9lcnJvciBiYWRHYW1lUGFzc3dvcmQgIntzZWxmLmduYW1lfSInKQogICAgICAg"
    "IGlmIHVzci51c2VyLmdhbWUgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHVzci51c2VyLmdhbWUu"
    "cmVtb3ZlKHVzcikgI2xlYXZlIHRoZSBwcmV2aW91cyByb29tIGNsZWFubHkgZmlyc3QKICAgICAg"
    "ICBzZWxmLnVzZXJsaXN0LmFwcGVuZCh1c3IpCiAgICAgICAgdXNyLnVzZXIuZ2FtZSA9IHNlbGYK"
    "ICAgICAgICByZXQgPSBfZW0oZickZ2FtZXVzZXIgIntzZWxmLmduYW1lfSIgInt1c3IudXNlci5u"
    "YW1lfSIgIiIgIjEwMCIgIjAiJykKICAgICAgICAjVW5jb25kaXRpb25hbGx5LCB0byBldmVyeW9u"
    "ZSBpbiB0aGUgdG93bi4gVGhpcyB1c2VkIHRvIGJlIHNlbnQgb25seQogICAgICAgICN3aGVuIG5w"
    "aiAoIm5ldyBwbGF5ZXJzIG1heSBqb2luIGEgcnVubmluZyBnYW1lIikgd2FzIHNldCAtIGJ1dCBu"
    "cGoKICAgICAgICAjc2F5cyBub3RoaW5nIGFib3V0IHdobyBzaG91bGQgaGVhciBhYm91dCBhIGpv"
    "aW4sIGl0IG9ubHkgY29udHJvbHMKICAgICAgICAjd2hldGhlciBhICpzdGFydGVkKiBnYW1lIHN0"
    "YXlzIGxpc3RlZC4gRm9yIGFuIG9yZGluYXJ5IHJvb20sIHdoaWNoIGlzCiAgICAgICAgI2NyZWF0"
    "ZWQgd2l0aCBucGo9MCBhbmQgam9pbmVkIGJlZm9yZSBpdCBzdGFydHMsIG5vYm9keSB3YXMgZXZl"
    "ciB0b2xkOgogICAgICAgICN0aGUgaG9zdCdzIGxvYmJ5IG5ldmVyIGxpc3RlZCB0aGUgYXJyaXZp"
    "bmcgcGxheWVyLCBzbyB0aGUgaG9zdCBoYWQKICAgICAgICAjbm9ib2R5IHRvIHN0YXJ0IHRoZSBn"
    "YW1lIHdpdGgsIGFuZCB0aGUgam9pbmVyIHNhdCBpbiAiY29ubmVjdGluZyIKICAgICAgICAjZm9y"
    "ZXZlciB3YWl0aW5nIGZvciBhIHN0YXJ0IHRoYXQgY291bGQgbm90IGNvbWUuCiAgICAgICAgdXNy"
    "LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6c2VsZi5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6cmV0"
    "fSkKICAgICAgICByZXR1cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi51"
    "cmx9IiAie3NlbGYuc3RhdHVzfSInKQogICAgZGVmIGRlc3Ryb3koc2VsZik6CiAgICAgICAgI1Rl"
    "YXIgdGhlIHJvb20gZG93biBjb21wbGV0ZWx5OiBldmVyeW9uZSBzdGlsbCBsaXN0ZWQgaW4gaXQg"
    "aXMgcHV0CiAgICAgICAgI2JhY2sgdG8gIm5vdCBpbiBhIGdhbWUiLCBhbmQgdGhlIHJvb20gc3Rv"
    "cHMgYmVpbmcgYWR2ZXJ0aXNlZC4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAg"
    "ICBmb3IgYyBpbiBsaXN0KHNlbGYudXNlcmxpc3QpOgogICAgICAgICAgICBpZiBjLnVzZXI6CiAg"
    "ICAgICAgICAgICAgICBjLnVzZXIuZ2FtZSA9IE5vbmUKICAgICAgICBzZWxmLnVzZXJsaXN0ID0g"
    "W10KICAgICAgICBpZiBzZWxmLnBhcmVudC5nYW1lcy5nZXQoc2VsZi5nbmFtZSkgaXMgc2VsZjoK"
    "ICAgICAgICAgICAgZGVsIHNlbGYucGFyZW50LmdhbWVzW3NlbGYuZ25hbWVdCiAgICAgICAgc2Vs"
    "Zi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICdtZXNzYWdlJzpfZW0oZicmZ2FtZSAie3NlbGYuZ25hbWV9Iicp"
    "fSkKICAgIGRlZiByZW1vdmUoc2VsZiwgY29uPU5vbmUpOiNUT0RPIHJlY3JlYXRlIHByb3Blcmx5"
    "CiAgICAgICAgaWYgY29uIGlzIE5vbmUgb3IgY29uIG5vdCBpbiBzZWxmLnVzZXJsaXN0OgogICAg"
    "ICAgICAgICByZXR1cm4KICAgICAgICB0ZyA9IHNlbGYuX2F1ZGllbmNlKCkKICAgICAgICBzZWxm"
    "LnVzZXJsaXN0LnJlbW92ZShjb24pCiAgICAgICAgaWYgY29uLnVzZXIgaXMgTm9uZToKICAgICAg"
    "ICAgICAgI0Nvbm5lY3Rpb24gYWxyZWFkeSB0b3JuIGRvd24gKGl0cyBoYW5kbGVyIHJhbiBmaW5p"
    "c2goKSB3aGlsZSB0aGlzCiAgICAgICAgICAgICNyZW1vdmFsIHdhcyBvbiBpdHMgd2F5IHRocm91"
    "Z2ggYW5vdGhlciB0aHJlYWQpLiBOb3RoaW5nIGxlZnQgdG8KICAgICAgICAgICAgI2Fubm91bmNl"
    "IGFib3V0IGl0LCBidXQgdGhlIHJvb20gaXRzZWxmIHN0aWxsIGhhcyB0byBiZSB0aWRpZWQgdXAK"
    "ICAgICAgICAgICAgI2JlbG93LCBzbyBkb24ndCByZXR1cm4gZWFybHkuCiAgICAgICAgICAgIGxl"
    "YXZlbXNnID0gYicnCiAgICAgICAgZWxzZToKICAgICAgICAgICAgbGVhdmVtc2cgPSBfZW0oZicm"
    "Z2FtZXVzZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24udXNlci5nYW1lID0g"
    "Tm9uZQogICAgICAgIGlmIGNvbiBpcyBzZWxmLmhvc3Q6CiAgICAgICAgICAgICNUaGUgaG9zdCAq"
    "aXMqIHRoZSBnYW1lIHNlc3Npb246IHRoZSBjby1vcCB3b3JsZCBydW5zIG9uIHRoZWlyCiAgICAg"
    "ICAgICAgICNtYWNoaW5lIGFuZCB0aGUgcm9vbSdzIERpcmVjdFBsYXkgdXJsIHBvaW50cyBhdCBp"
    "dC4gT25jZSB0aGV5IGFyZQogICAgICAgICAgICAjZ29uZSB0aGUgcm9vbSBjYW5ub3QgYmUgam9p"
    "bmVkIGJ5IGFueWJvZHksIGJ1dCBpdCB1c2VkIHRvIHN0YXkKICAgICAgICAgICAgI2xpc3RlZCAt"
    "IHNvIHRoZSBuZXh0IHBsYXllciB0byBjbGljayBpdCBnb3QgYSB1cmwgdG8gYSBnYW1lIHRoYXQK"
    "ICAgICAgICAgICAgI25vIGxvbmdlciBleGlzdGVkIGFuZCBzYXQgb24gImNvbm5lY3RpbmciIHVu"
    "dGlsIHRoZXkgZ2F2ZSB1cC4KICAgICAgICAgICAgI1RoaXMgaXMgd2hhdCBhIGhvc3QgY3Jhc2gg"
    "bGVhdmVzIGJlaGluZC4KICAgICAgICAgICAgd2hvID0gY29uLnVzZXIubmFtZSBpZiBjb24udXNl"
    "ciBlbHNlICc/JwogICAgICAgICAgICBwcmludChmJ1tMb2JieV0gSG9zdCB7d2hvfSBsZWZ0IHJv"
    "b20gIntzZWxmLmduYW1lfSIsIGNsb3NpbmcgaXQnKQogICAgICAgICAgICBpZiBsZWF2ZW1zZzoK"
    "ICAgICAgICAgICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcs"
    "J21lc3NhZ2UnOmxlYXZlbXNnfSkKICAgICAgICAgICAgc2VsZi5kZXN0cm95KCkKICAgICAgICAg"
    "ICAgcmV0dXJuCiAgICAgICAgI2lmIDAgdXNlcnMgbGVmdCwgcmVtb3ZlIGdhbWUKICAgICAgICBp"
    "ZiBsZW4oc2VsZi51c2VybGlzdCk9PTA6CiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmdh"
    "bWUgIntzZWxmLmduYW1lfSInKQogICAgICAgICAgICAjT25seSBpZiB0aGlzIGVudHJ5IGlzIHN0"
    "aWxsIHRoZSBvbmUgcmVnaXN0ZXJlZCB1bmRlciB0aGF0IG5hbWUuIEEKICAgICAgICAgICAgI3Jv"
    "b20gd2hvc2UgaG9zdCByZWNvbm5lY3RzIGFuZCByZS1ob3N0cyBpcyByZXBsYWNlZCBieSBhICpu"
    "ZXcqCiAgICAgICAgICAgICNHYW1lRW50cnkgd2l0aCB0aGUgc2FtZSBuYW1lIChzZWUgX2lzU3Rh"
    "bGVHYW1lKTsgdGhlIG9sZCBvbmUncwogICAgICAgICAgICAjbGFzdCBwbGF5ZXIgbGVhdmluZyB0"
    "aGVuIGRlbGV0ZWQgdGhlIGxpdmUgcm9vbSBvdXQgb2YgdGhlIGNoYW5uZWwgLQogICAgICAgICAg"
    "ICAjb3IgcmFpc2VkIEtleUVycm9yIGlmIGl0IGhhZCBhbHJlYWR5IGdvbmUsIGluc2lkZSB0aGUg"
    "ZGlzY29ubmVjdAogICAgICAgICAgICAjcGF0aCwgd2hpY2ggYWJvcnRzIHRoZSByZXN0IG9mIHRo"
    "YXQgcGxheWVyJ3MgY2xlYW51cC4KICAgICAgICAgICAgaWYgc2VsZi5wYXJlbnQuZ2FtZXMuZ2V0"
    "KHNlbGYuZ25hbWUpIGlzIHNlbGY6CiAgICAgICAgICAgICAgICBkZWwgc2VsZi5wYXJlbnQuZ2Ft"
    "ZXNbc2VsZi5nbmFtZV0KICAgICAgICBpZiBsZWF2ZW1zZzoKICAgICAgICAgICAgc2VsZi5wYXJl"
    "bnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVzc2FnZSc6bGVhdmVtc2d9KQogICAg"
    "ZGVmIHN0YXJ0R2FtZShzZWxmLCB1c2VyPU5vbmUpOgogICAgICAgIGlmIG5vdCAodXNlciBhbmQg"
    "c2VsZi5ob3N0ID09IHVzZXIpOgogICAgICAgICAgICByZXR1cm4gTm9uZSAjdXNlciBub3QgaG9z"
    "dAogICAgICAgIHRnID0gc2VsZi5fYXVkaWVuY2UoKQogICAgICAgIHNlbGYuc3RhdHVzID0gMQog"
    "ICAgICAgIGZvciBjIGluIHNlbGYudXNlcmxpc3Q6I1RPRE8gaGF2ZSB1c2VyIHJlbW92ZSBpdHNl"
    "bGYgd2hlbiAvc3RhcnRpbmdnYW1lPwogICAgICAgICAgICB1biA9IGMudXNlci5uYW1lCiAgICAg"
    "ICAgICAgICNUT0RPIGNvbnNpZGVyIHJlbW92aW5nIHVzZXIgZnJvbSB0YXJnZXQgb3duIHNldD8K"
    "ICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0Jzp0ZywnbWVz"
    "c2FnZSc6X2VtKGYnJmNoYXRjaGFubmVsdXNlciAie3VufSInKStfZW0oZicmZ2FtZWNoYW5uZWx1"
    "c2VyICJ7dW59IicpfSkKICAgICAgICAjLi4uYW5kIGFjdHVhbGx5IHRha2UgdGhlbSBvZmYgdGhl"
    "IHRvd24gcm9zdGVyLCB3aGljaCB0aGlzIG9ubHkgZXZlcgogICAgICAgICMqYW5ub3VuY2VkKi4g"
    "TGVhdmluZyB0aGVtIGxpc3RlZCBtZWFudCB0aGUgc2VydmVyIHN0aWxsIGNvdW50ZWQgdGhlbQog"
    "ICAgICAgICNhcyBzdGFuZGluZyBpbiB0aGUgdG93biBmb3IgdGhlIHdob2xlIHNlc3Npb246IHRv"
    "d24gcG9wdWxhdGlvbiB3YXMKICAgICAgICAjd3JvbmcsIGFuZCBldmVyeSBwb3NpdGlvbiB1cGRh"
    "dGUgZnJvbSBhbnlvbmUgc3RpbGwgd2Fsa2luZyBhcm91bmQgd2FzCiAgICAgICAgI2Zhbm5lZCBv"
    "dXQgdG8gcGxheWVycyB3aG8gd2VyZSBhd2F5IGluIGEgY28tb3Agd29ybGQgYW5kIGNvdWxkIGRv"
    "CiAgICAgICAgI25vdGhpbmcgd2l0aCBpdC4gVGhlIGNsaWVudHMgd2VyZSB0b2xkIHRoZXkgbGVm"
    "dDsgbm93IHRoZSBzZXJ2ZXIKICAgICAgICAjYWdyZWVzIHdpdGggdGhlbS4KICAgICAgICBmb3Ig"
    "YyBpbiBsaXN0KHNlbGYudXNlcmxpc3QpOgogICAgICAgICAgICBjLnVzZXIubGVhdmVDaGF0KCkK"
    "ICAgICAgICAgICAgaWYgYyBpbiBzZWxmLnBhcmVudC51c2VybGlzdDoKICAgICAgICAgICAgICAg"
    "IHNlbGYucGFyZW50LnVzZXJsaXN0LnJlbW92ZShjKQogICAgICAgIGlmIG5vdCBzZWxmLm5wajoK"
    "ICAgICAgICAgICAgI2dhbWUgbm8gbG9uZ2VyIGpvaW5hYmxlL3Zpc2libGUgb25jZSBzdGFydGVk"
    "CiAgICAgICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21l"
    "c3NhZ2UnOl9lbShmJyZnYW1lICJ7c2VsZi5nbmFtZX0iJyl9KQogICAgICAgICNub3RpZnkgcGxh"
    "eWVycyBpbiB0aGUgZ2FtZSB0aGF0IGl0IGhhcyBzdGFydGVkCiAgICAgICAgZm9yIGMgaW4gc2Vs"
    "Zi51c2VybGlzdDoKICAgICAgICAgICAgaXNIb3N0ID0gMSBpZiBjIGlzIHNlbGYuaG9zdCBlbHNl"
    "IDAKICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2VydmVyLmRpc3QuYWRkKHsndGFyZ2V0JzooYywp"
    "LCdtZXNzYWdlJzpfZW0oZicvc3RhcnRnYW1lICIxIiAie2lzSG9zdH0iICIxIicpfSkKICAgICAg"
    "ICByZXR1cm4gTm9uZQogICAgZGVmIF9nZXRVc2VybGlzdChzZWxmKToKICAgICAgICByZXR1cm4g"
    "JyAnLmpvaW4oIChmJyJ7Yy51c2VyLm5hbWV9IiAiIiAiMTAwIiAiMCInIGZvciBjIGluIHNlbGYu"
    "dXNlcmxpc3QpICkKICAgIGRlZiBnZXRHYW1lU3RyaW5nKHNlbGYpOgogICAgICAgIGlmIHNlbGYu"
    "c3RhdHVzIGFuZCBub3Qgc2VsZi5ucGo6CiAgICAgICAgICAgIHJldHVybiBOb25lICNHYW1lIGRv"
    "ZXMgbm90IHNob3cgaWYgbmV3IHBsYXllcnMgY2FuJ3Qgam9pbiB3aGVuIGFjdGl2ZQogICAgICAg"
    "IHBhc3cgPSAnJwogICAgICAgIGlmIHNlbGYucGFzc3dvcmQ6CiAgICAgICAgICAgIHBhc3cgPSAn"
    "WFhYJwogICAgICAgIHJldHVybiBfZW0oZickZ2FtZSAie3NlbGYuZ25hbWV9IiAie3Bhc3d9IiAi"
    "e3NlbGYubWFwUGFyfSIgIntzZWxmLm1hcFRyYW5zbGF0ZX0iICJ7c2VsZi51bjF9IiAie3NlbGYu"
    "c3RhdHVzfSIgIntzZWxmLm1heHBsYXllcnN9IiB7c2VsZi5fZ2V0VXNlcmxpc3QoKX0nKQogICAg"
    "ZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAgICAgICAgICAgJ25hbWUn"
    "OnNlbGYuZ25hbWUsCiAgICAgICAgICAgICdob3N0JzpzZWxmLmhvc3QudXNlci5uYW1lLAogICAg"
    "ICAgICAgICAnc3RhdHVzJzpzZWxmLnN0YXR1cywKICAgICAgICAgICAgJ2hhc1Bhc3N3b3JkJzox"
    "IGlmIHNlbGYucGFzc3dvcmQgZWxzZSAwLAogICAgICAgICAgICAndXNlcnMnOnR1cGxlKFtjLnVz"
    "ZXIubmFtZSBmb3IgYyBpbiBzZWxmLnVzZXJsaXN0XSksCiAgICAgICAgICAgICd0b3duJzpzZWxm"
    "LnBhcmVudC5uYW1lLAogICAgICAgICAgICAncGFyYW1ldGVycyc6c2VsZi5tYXBQYXIsCiAgICAg"
    "ICAgICAgICdtYXBOYW1lJzpzZWxmLm1hcFRyYW5zbGF0ZSwKICAgICAgICAgICAgJ2NhbkpvaW5S"
    "dW5uaW5nJzpzZWxmLm5wagogICAgICAgIH0KIyB0cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwK"
    "IyB0cmFuc2xhdGVOZXRDaXR5VHJhZGVDaGFubmVsCiMgdHJhbnNsYXRlTmV0Q2l0eUNoYXRDaGFu"
    "bmVsCl9ERUZBVUxUX0NIQVRTID0gWyd0cmFuc2xhdGVOZXRDaXR5TWFpbkNoYW5uZWwnLCd0cmFu"
    "c2xhdGVOZXRDaXR5VHJhZGVDaGFubmVsJ10KY2xhc3MgR2FtZUNoYW5uZWwoKToKICAgIG1heHVz"
    "ZXIgPSA1MCAjVE9ETyBjb25maWd1cmVhYmxlCiAgICBkZWYgX19pbml0X18oc2VsZiwgc2VydmVy"
    "LCBjaG5OYW1lKToKICAgICAgICBzZWxmLnNlcnZlciA9IHNlcnZlcgogICAgICAgIHNlbGYubmFt"
    "ZSA9IGNobk5hbWUKICAgICAgICBzZWxmLnVzZXJsaXN0ID0gW10KICAgICAgICBzZWxmLmNoYXRD"
    "aGFubmVscyA9IHt9CiAgICAgICAgc2VsZi5nYW1lcyA9IHt9ICNUT0RPIGZpZ3VyZSBvdXQgQSBh"
    "bmQgQiB2YWx1ZSBmb3IgZGlzcGxheQogICAgICAgICNUT0RPIHJlcXVlc3Qgam9pbiByZXNlcnZl"
    "cyBzcGFjZSB3aXRoIHdlYWsgcmVmZXJlbmNlcwogICAgICAgICMtIHdlYWsgdmFsdWUgcmVmIHNo"
    "b3VsZCBlbnN1cmUgdGhhdCBjb25uZWN0aW9uIGlzIHJlbW92ZWQgZnJvbSBxdWV1ZSBpZiBpdCBk"
    "aXNjb25uZWN0cyBkdXJpbmcgdGhlIGpvaW4gcHJvY2VzcwogICAgICAgIHNlbGYucmVxdWVzdGVk"
    "ID0gW10KICAgICAgICBzZWxmLmdhbWVSZXF1ZXN0cyA9IHt9CiAgICAgICAgc2VsZi5kaXJ0eSA9"
    "IEZhbHNlCiAgICAgICAgZm9yIGNuIGluIF9ERUZBVUxUX0NIQVRTOgogICAgICAgICAgICBzZWxm"
    "LmNoYXRDaGFubmVsc1tjbl0gPSBbXSAjVXNlcmxpc3QKICAgIGRlZiByZXF1ZXN0Sm9pbihzZWxm"
    "LCBjb24pOgogICAgICAgICNsZWF2ZUNoYW5uZWwoKSBhbHJlYWR5IHJlbGVhc2VzIGFueSBvdXRz"
    "dGFuZGluZyByZXNlcnZhdGlvbiwgb24gdGhpcwogICAgICAgICNjaGFubmVsIG9yIGFub3RoZXIg"
    "b25lLiBUaGUgZm9sbG93LXVwIGJsb2NrIHRoYXQgdXNlZCB0byBzdGFuZCBoZXJlCiAgICAgICAg"
    "I2NvdWxkIHRoZXJlZm9yZSBuZXZlciBydW4gLSBhbmQgaWYgaXQgZXZlciBoYWQsIGl0cyB1bmd1"
    "YXJkZWQKICAgICAgICAjbGlzdC5yZW1vdmUoKSB3b3VsZCBoYXZlIHJhaXNlZCBWYWx1ZUVycm9y"
    "IGZvciBhIHJlc2VydmF0aW9uIHRoYXQgd2FzCiAgICAgICAgI2FscmVhZHkgZ29uZS4KICAgICAg"
    "ICBjb24udXNlci5sZWF2ZUNoYW5uZWwoKQogICAgICAgIGVsZW4gPSBsZW4oc2VsZi51c2VybGlz"
    "dCkrbGVuKHNlbGYucmVxdWVzdGVkKQogICAgICAgIGlmIGVsZW48c2VsZi5tYXh1c2VyOgogICAg"
    "ICAgICAgICBzZWxmLnJlcXVlc3RlZC5hcHBlbmQoY29uKQogICAgICAgICAgICBjb24udXNlci5y"
    "ZXF1ZXN0ZWRDaGFubmVsID0gc2VsZgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHJl"
    "dHVybiBGYWxzZQogICAgZGVmIF9pc1N0YWxlR2FtZShzZWxmLCBnZW50LCBjb24pOgogICAgICAg"
    "ICNBIHJvb20gd2hvc2UgaG9zdCBpcyBubyBsb25nZXIgdGhlIGxpdmUgc2Vzc2lvbiBmb3IgdGhh"
    "dCBhY2NvdW50LiBUaGUKICAgICAgICAjY2xpZW50IG5hbWVzIGEgcm9vbSBhZnRlciBpdHMgaG9z"
    "dCwgc28gd2hlbiBhIHBsYXllciB3aG9zZSBnYW1lCiAgICAgICAgI2NyYXNoZWQgcmVjb25uZWN0"
    "cyBhbmQgaG9zdHMgYWdhaW4sIHRoZSByb29tIGZyb20gdGhlIHNlc3Npb24gdGhhdAogICAgICAg"
    "ICNkaWVkIGlzIHN0aWxsIHNpdHRpbmcgaGVyZSB1bmRlciB0aGUgc2FtZSBuYW1lIC0gd2l0aCBh"
    "IGhvc3QKICAgICAgICAjY29ubmVjdGlvbiB0aGF0IG5vIGxvbmdlciBleGlzdHMgYW5kIGEgRGly"
    "ZWN0UGxheSB1cmwgcG9pbnRpbmcgYXQgYQogICAgICAgICNnYW1lIHRoYXQgaXMgZ29uZS4gQW55"
    "b25lIGpvaW5pbmcgaXQgd2FpdHMgZm9yZXZlci4KICAgICAgICBpZiBnZW50Lmhvc3QgaXMgY29u"
    "OgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIGhvc3RuYW1lID0gZ2VudC5ob3N0LnVz"
    "ZXIubmFtZSBpZiBnZW50Lmhvc3QudXNlciBlbHNlIE5vbmUKICAgICAgICBpZiBob3N0bmFtZSBp"
    "cyBOb25lOgogICAgICAgICAgICByZXR1cm4gVHJ1ZQogICAgICAgIHJldHVybiBzZWxmLnNlcnZl"
    "ci5nZXRQbGF5ZXIoaG9zdG5hbWUpIGlzIG5vdCBnZW50Lmhvc3QKICAgIGRlZiByZXF1ZXN0Q3Jl"
    "YXRlR2FtZShzZWxmLCBjb24sIGdhbWVOYW1lKToKICAgICAgICAjTmV2ZXIgcmV0dXJuIGEgYmFy"
    "ZSBGYWxzZSBmcm9tIGhlcmUuIHBhcnNlKCkgdHJlYXRzIGEgZmFsc3kgcmVzdWx0IGFzCiAgICAg"
    "ICAgIyJub3RoaW5nIHRvIHNlbmQiLCBzbyBldmVyeSByZWplY3Rpb24gYmVsb3cgdXNlZCB0byBs"
    "ZWF2ZSB0aGUgY2xpZW50CiAgICAgICAgI3dhaXRpbmcgb24gYW4gYW5zd2VyIHRoYXQgbmV2ZXIg"
    "Y2FtZSAtIHRoZSByb29tLWNyZWF0aW9uIGRpYWxvZyB0aGVuCiAgICAgICAgI3NwaW5zIGZvcmV2"
    "ZXIuCiAgICAgICAgaWYgY29uLnVzZXIucmVxdWVzdGVkR2FtZSBvciBjb24udXNlci5nYW1lOgog"
    "ICAgICAgICAgICBjb24udXNlci5zdG9wR2FtZSgpCiAgICAgICAgdGNuID0gc2VsZi5nYW1lUmVx"
    "dWVzdHMuZ2V0KGdhbWVOYW1lKQogICAgICAgIGlmIHRjbiBpcyBub3QgTm9uZSBhbmQgdGNuIGlz"
    "IG5vdCBjb246CiAgICAgICAgICAgIHJldHVybiBfZW0oZicvZXJyb3IgZ2FtZU5hbWVUYWtlbiAi"
    "e2dhbWVOYW1lfSInKQogICAgICAgICAgICAjZWxzZSB0Y24gaXMgY29uLCByZS1yZXF1ZXN0ZWQg"
    "Y3JlYXRpb24KICAgICAgICBnZW50ID0gc2VsZi5nYW1lcy5nZXQoZ2FtZU5hbWUpCiAgICAgICAg"
    "aWYgZ2VudCBpcyBub3QgTm9uZToKICAgICAgICAgICAgaWYgc2VsZi5faXNTdGFsZUdhbWUoZ2Vu"
    "dCwgY29uKToKICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBSZXBsYWNpbmcgc3RhbGUg"
    "cm9vbSAie2dhbWVOYW1lfSIgJwogICAgICAgICAgICAgICAgICAgICAgZicoaG9zdCBzZXNzaW9u"
    "IGdvbmUpIGF0IHRoZSByZXF1ZXN0IG9mIHtjb24udXNlci5uYW1lfScpCiAgICAgICAgICAgICAg"
    "ICBnZW50LmRlc3Ryb3koKQogICAgICAgICAgICBlbHNlOgogICAgICAgICAgICAgICAgcmV0dXJu"
    "IF9lbShmJy9lcnJvciBnYW1lTmFtZVRha2VuICJ7Z2FtZU5hbWV9IicpCiAgICAgICAgc2VsZi5n"
    "YW1lUmVxdWVzdHNbZ2FtZU5hbWVdID0gY29uCiAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkR2Ft"
    "ZSA9IGdhbWVOYW1lCiAgICAgICAgcmV0dXJuIF9lbShmJy9jcmVhdGVnYW1lICJ7Z2FtZU5hbWV9"
    "IicpCiAgICBkZWYgY3JlYXRlR2FtZShzZWxmLCBnYW1lTmFtZSwgaG9zdCwgcGFzdywgbWFwcCwg"
    "bWFwdCwgbnBqLCB1bjEsIHVuMiwgdW4zLCB1cmwpOgogICAgICAgIHJlcUhvc3QgPSBzZWxmLmdh"
    "bWVSZXF1ZXN0cy5nZXQoZ2FtZU5hbWUpCiAgICAgICAgaWYgcmVxSG9zdCBpcyBOb25lIG9yIHJl"
    "cUhvc3QgaXMgbm90IGhvc3Q6CiAgICAgICAgICAgICNTYW1lIHJlYXNvbmluZyBhcyBhYm92ZTog"
    "YW5zd2VyLCBuZXZlciBmYWxsIHNpbGVudC4KICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJv"
    "ciBnYW1lTmFtZVRha2VuICJ7Z2FtZU5hbWV9IicpCiAgICAgICAgZ2VudCA9IEdhbWVFbnRyeShz"
    "ZWxmLCBnYW1lTmFtZSwgaG9zdCwgcGFzdywgbWFwcCwgbWFwdCwgbnBqLCB1bjEsIHVuMiwgdW4z"
    "LCB1cmwpCiAgICAgICAgcmVxSG9zdC51c2VyLnJlcXVlc3RlZEdhbWUgPSBOb25lICNUT0RPIHJl"
    "b2dhbml6ZSBiZXR0ZXIKICAgICAgICBkZWwgc2VsZi5nYW1lUmVxdWVzdHNbZ2FtZU5hbWVdCiAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBsZWF2ZUNoYW5uZWwoc2VsZiwgY29uKToKICAgICAg"
    "ICAjVGhlIGNsZWFudXAgcnVucyB3aGV0aGVyIG9yIG5vdCB0aGUgcGxheWVyIGlzIHN0aWxsIG9u"
    "IHRoZSB0b3duCiAgICAgICAgI3Jvc3Rlci4gU2luY2Ugc3RhcnRHYW1lKCkgdGFrZXMgaXRzIHBs"
    "YXllcnMgb2ZmIHRoYXQgcm9zdGVyLCBhCiAgICAgICAgI3BsYXllciB3aG8gbGVhdmVzIChvciBk"
    "aXNjb25uZWN0cykgZnJvbSBpbnNpZGUgYSBydW5uaW5nIGdhbWUgdXNlZCB0bwogICAgICAgICNz"
    "a2lwIGFsbCBvZiB0aGlzOiB0aGVpciByb29tIHdhcyBuZXZlciBsZWZ0LCB0aGVpciBjaGF0IGNo"
    "YW5uZWwga2VwdAogICAgICAgICN0aGVpciBlbnRyeSwgYW5kIGdhbWVjaGFubmVsIHN0YXllZCBw"
    "b2ludGluZyBhdCBhIHRvd24gdGhleSB3ZXJlIG5vCiAgICAgICAgI2xvbmdlciBpbi4gT25seSB0"
    "aGUgcm9zdGVyIHJlbW92YWwgYW5kIHRoZSBhbm5vdW5jZW1lbnQgYXJlCiAgICAgICAgI2NvbmRp"
    "dGlvbmFsIG5vdyAtIGJlY2F1c2Ugb25seSB0aG9zZSBkZXBlbmQgb24gYmVpbmcgbGlzdGVkLgog"
    "ICAgICAgIGxpc3RlZCA9IGNvbiBpbiBzZWxmLnVzZXJsaXN0CiAgICAgICAgY29uLnVzZXIuc3Rv"
    "cEdhbWUoKQogICAgICAgIGNvbi51c2VyLmxlYXZlQ2hhdCgpCiAgICAgICAgaWYgbGlzdGVkOgog"
    "ICAgICAgICAgICBzZWxmLnVzZXJsaXN0LnJlbW92ZShjb24pCiAgICAgICAgICAgIGxlYXZlbXNn"
    "ID0gX2VtKGYnJmdhbWVjaGFubmVsdXNlciAie2Nvbi51c2VyLm5hbWV9IicpCiAgICAgICAgICAg"
    "IGNvbi5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnNlbGYudXNlcmxpc3QsJ21lc3NhZ2UnOmxl"
    "YXZlbXNnfSkKICAgICAgICBjb24udXNlci5nYW1lY2hhbm5lbD1Ob25lCiAgICBkZWYgbGVhdmVD"
    "aGF0KHNlbGYsIGNvbik6ICNUT0RPIGJldHRlciBjaGF0Y2hhbm5lbCBvYmplY3QgYW5kIG1vdmUg"
    "aXQgdGhlcmUuCiAgICAgICAgY29uLnVzZXIubGVhdmVDaGF0KCkKICAgICNUT0RPIGNoYW5nZSB0"
    "aGVzZSBmdW5jdGlvbnMgdG8gYWxzbyBoYW5kbGUgbWVzc2FnZSBmb3JtaW5nCiAgICBkZWYgam9p"
    "bkNoYW5uZWwoc2VsZiwgY29uLCBuYW0pOiNtb3ZlcyB1c2VyIGZyb20gcXVldWUgdG8gdXNlcmxp"
    "c3QKICAgICAgICBpZiBjb24gaW4gc2VsZi51c2VybGlzdDoKICAgICAgICAgICAgI0R1cGxpY2F0"
    "ZSAvam9pbmdhbWVjaGFubmVsIGZvciBhIHRvd24gd2UgYXJlIGFscmVhZHkgaW4uIFJlYnVpbGQK"
    "ICAgICAgICAgICAgI3RoZSByZXNlcnZhdGlvbiBzbyB0aGUgcmVxdWVzdCBiZWxvdyByZS1ydW5z"
    "IHRoZSBmdWxsIGVudW1lcmF0aW9uCiAgICAgICAgICAgICNhbmQgdGhlIGNsaWVudCBnZXRzIGEg"
    "Y29tcGxldGUgYW5zd2VyIHJhdGhlciB0aGFuIHNpbGVuY2UuCiAgICAgICAgICAgIHNlbGYudXNl"
    "cmxpc3QucmVtb3ZlKGNvbikKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikK"
    "ICAgICAgICAgICAgY29uLnVzZXIucmVxdWVzdGVkQ2hhbm5lbCA9IHNlbGYKICAgICAgICBpZiBj"
    "b24gbm90IGluIHNlbGYucmVxdWVzdGVkIGFuZCBjb24gbm90IGluIHNlbGYudXNlcmxpc3Q6CiAg"
    "ICAgICAgICAgICNObyBvdXRzdGFuZGluZyByZXNlcnZhdGlvbi4gVGhlIHJlc2VydmF0aW9uIGlz"
    "IGRyb3BwZWQgYnkgYW55CiAgICAgICAgICAgICNpbnRlcnZlbmluZyBsZWF2ZUNoYW5uZWwoKS9y"
    "ZXF1ZXN0Sm9pbigpIGFuZCBieSBhIHJlY29ubmVjdCwgc28gYQogICAgICAgICAgICAjY2xpZW50"
    "IHRoYXQgZ29lcyBzdHJhaWdodCB0byAvam9pbmdhbWVjaGFubmVsIC0gb3Igd2hvc2UgZWFybGll"
    "cgogICAgICAgICAgICAjL3JlcXVlc3Rqb2luZ2FtZWNoYW5uZWwgcmFjZWQgaXRzIG93biBjbGVh"
    "bnVwIC0gdXNlZCB0byBnZXQgbm8KICAgICAgICAgICAgI2Fuc3dlciBhdCBhbGwgYW5kIGhhbmcg"
    "b24gdGhlIGxvYWRpbmcgc2NyZWVuLiBBZG1pdCB0aGVtIGlmIHRoZQogICAgICAgICAgICAjdG93"
    "biBoYXMgcm9vbTsgb25seSBhIGdlbnVpbmVseSBmdWxsIHRvd24gaXMgcmVmdXNlZCBub3cuCiAg"
    "ICAgICAgICAgIGlmIGxlbihzZWxmLnVzZXJsaXN0KStsZW4oc2VsZi5yZXF1ZXN0ZWQpIDwgc2Vs"
    "Zi5tYXh1c2VyOgogICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0ZWQuYXBwZW5kKGNvbikKICAg"
    "ICAgICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBzZWxmCiAgICAgICAgICAg"
    "IGVsc2U6CiAgICAgICAgICAgICAgICByZXR1cm4gX2VtKGYnL2Vycm9yIGdhbWVDaGFubmVsRnVs"
    "bCAie25hbX0iJykKICAgICAgICBpZiBjb24gaW4gc2VsZi5yZXF1ZXN0ZWQ6CiAgICAgICAgICAg"
    "ICNUT0RPIHZlcmlmeSBvcmRlciBvZiBvcGVyYXRpb25zIGFuZCBwb3NzaWJsZSB0aW1pbmcgaXNz"
    "dWVzCiAgICAgICAgICAgIHNlbGYudXNlcmxpc3QuYXBwZW5kKGNvbikKICAgICAgICAgICAgY29u"
    "LnVzZXIuZ2FtZWNoYW5uZWwgPSBzZWxmCiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkLnJlbW92"
    "ZShjb24pCiAgICAgICAgICAgIGNvbi51c2VyLnJlcXVlc3RlZENoYW5uZWwgPSBOb25lICNUT0RP"
    "IG9yZ2FuaXplIGJldHRlcj8KICAgICAgICAgICAgdWwgPSBsZW4oc2VsZi51c2VybGlzdCkKICAg"
    "ICAgICAgICAgcmV0bXNnID0gX2VtKGYnL2pvaW5nYW1lY2hhbm5lbCAie25hbX0iICJ7dWx9Iicp"
    "CiAgICAgICAgICAgICNlbnVtZXJhdGUgaGVyb2RhdGEgb2YgZXhpc3RpbmcgdXNlcnMKICAgICAg"
    "ICAgICAgY2h1bmtzID0gW10KICAgICAgICAgICAgZm9yIHVzZXIgaW4gc2VsZi51c2VybGlzdDoK"
    "ICAgICAgICAgICAgICAgIGlmIHVzZXIgPT0gY29uOgogICAgICAgICAgICAgICAgICAgIGNvbnRp"
    "bnVlCiAgICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKHVzZXIudXNlci5nZXRHQ1Vtc2coKSkK"
    "ICAgICAgICAgICAgcmV0bXNnKz0gYicnLmpvaW4oY2h1bmtzKQogICAgICAgICAgICByZXRtc2cr"
    "PSBzZWxmLmpvaW5DaGF0KGNvbiwgX0RFRkFVTFRfQ0hBVFNbMF0pCiAgICAgICAgICAgIHJldG1z"
    "Zys9IHNlbGYuZW51bUNoYXRzKCkKICAgICAgICAgICAgcmV0bXNnKz0gc2VsZi5lbnVtR2FtZXMo"
    "KQogICAgICAgICAgICAjYnJvYWRjYXN0IGhlcm9kYXRhIHRvIG90aGVyIGV4aXN0aW5nIHVzZXJz"
    "CiAgICAgICAgICAgIGNvbi5zZXJ2ZXIuZGlzdC5hZGQoewogICAgICAgICAgICAgICAgJ3Rhcmdl"
    "dCc6X3dvVXNlcihzZWxmLnVzZXJsaXN0LCBjb24pLAogICAgICAgICAgICAgICAgJ21lc3NhZ2Un"
    "OmNvbi51c2VyLmdldEdDVW1zZygpfSkKICAgICAgICAgICAgcmV0dXJuIHJldG1zZwogICAgICAg"
    "IHJldHVybiBOb25lCiAgICBkZWYgam9pbkNoYXQoc2VsZiwgY29uLCBuYW0sIHBhcz0nJyk6CiAg"
    "ICAgICAgI1RPRE8gcGFzc3dvcmQgc3VwcG9ydD8KICAgICAgICAjLSByZXF1aXJlcyByZXN0cnVj"
    "dHVyZSBmcm9tIGxpc3QgdG8gY2hhbm5lbCBvYmplY3RzCiAgICAgICAgaWYgbm90IG5hbSBpbiBz"
    "ZWxmLmNoYXRDaGFubmVsczoKICAgICAgICAgICAgcmV0dXJuIGInJwogICAgICAgIGNvbi51c2Vy"
    "LmxlYXZlQ2hhdCgpCiAgICAgICAgI1RPRE8gY2hlY2sgaWYgY2xpZW50IGF1dG8tcHVyZ2VzIGNo"
    "YXRsaXN0CiAgICAgICAgI0Z1bGwgZm91ci1maWVsZCBmb3JtIChuYW1lLCBndWlsZCwgZmxhZ3Ms"
    "IGd1aWQpLCB3aGljaCBpcyB3aGF0IHRoZQogICAgICAgICNjbGllbnQgaXMgZG9jdW1lbnRlZCB0"
    "byBzZW5kIGFuZCB3aGF0IGdldENDVW1zZygpIGV4aXN0cyB0byBidWlsZCAtCiAgICAgICAgI3Nl"
    "ZSB0aGUgY2FwdHVyZSBub3RlZCBuZXh0IHRvIGl0LiBCb3RoIGFubm91bmNlbWVudHMgaGVyZSB1"
    "c2VkIHRvIGVtaXQKICAgICAgICAjYSBvbmUtZmllbGQgJyRjaGF0Y2hhbm5lbHVzZXIgIm5hbWUi"
    "JyBpbnN0ZWFkLCBzbyB0aGUgZ3VpbGQgY29sdW1uIHdhcwogICAgICAgICNhbHdheXMgYmxhbmsg"
    "aW4gY2hhdCBubyBtYXR0ZXIgd2hhdCBndWlsZCBhIHBsYXllciB3YXMgaW4sIGFuZCB0aGUKICAg"
    "ICAgICAjY2xpZW50IGhhZCB0byBmaWxsIHRocmVlIGZpZWxkcyBpdCB3YXMgbmV2ZXIgZ2l2ZW4u"
    "IFRoZSAkZ2FtZWNoYW5uZWx1c2VyCiAgICAgICAgI3BhdGggbmV4dCBkb29yIGhhcyBhbHdheXMg"
    "c2VudCBpdHMgZnVsbCBmb3JtOyB0aGVzZSB0d28gd2VyZSB0aGUKICAgICAgICAjc3RyYWdnbGVy"
    "cy4KICAgICAgICBjb24uc2VydmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgJ3RhcmdldCc6bGlz"
    "dChzZWxmLmNoYXRDaGFubmVsc1tuYW1dKSwKICAgICAgICAgICAgJ21lc3NhZ2UnOmNvbi51c2Vy"
    "LmdldENDVW1zZygpfSkKICAgICAgICBzZWxmLmNoYXRDaGFubmVsc1tuYW1dLmFwcGVuZChjb24p"
    "CiAgICAgICAgY29uLnVzZXIuY2hhdGNoYW5uZWwgPSBzZWxmLmNoYXRDaGFubmVsc1tuYW1dCiAg"
    "ICAgICAgdWwgPSAxI2xlbihjb24udXNlci5jaGF0Y2hhbm5lbCkKICAgICAgICByZXRtc2cgPSBf"
    "ZW0oZicvam9pbmNoYXRjaGFubmVsICJ7bmFtfSIgIiIgInt1bH0iJykKICAgICAgICAjZW51bWVy"
    "YXRlIG90aGVyIGNoYXQgdXNlcnM/CiAgICAgICAgY2h1bmtzID0gW10KICAgICAgICBmb3IgdWNv"
    "biBpbiBsaXN0KGNvbi51c2VyLmNoYXRjaGFubmVsKToKICAgICAgICAgICAgaWYgdWNvbiAhPSBj"
    "b246CiAgICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKHVjb24udXNlci5nZXRDQ1Vtc2coKSkK"
    "ICAgICAgICByZXRtc2crPWInJy5qb2luKGNodW5rcykKICAgICAgICByZXR1cm4gcmV0bXNnCiAg"
    "ICBkZWYgZW51bUNoYXRzKHNlbGYpOgogICAgICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIGNo"
    "YXROYW1lIGluIGxpc3Qoc2VsZi5jaGF0Q2hhbm5lbHMpOgogICAgICAgICAgICB1bGwgPSBsZW4o"
    "c2VsZi5jaGF0Q2hhbm5lbHNbY2hhdE5hbWVdKSNUT0RPIGltcHJvdmUKICAgICAgICAgICAgY2h1"
    "bmtzLmFwcGVuZCh3aXJlX2VuY29kZShmJyRjaGF0Y2hhbm5lbCAie2NoYXROYW1lfSIgIiIgInt1"
    "bGx9IicpKQogICAgICAgIGlmIG5vdCBjaHVua3M6CiAgICAgICAgICAgIHJldHVybiBiJycgI25l"
    "dmVyIGEgbG9uZSB0ZXJtaW5hdG9yOiB0aGF0IGlzIGFuIGVtcHR5IGNvbW1hbmQgbGluZQogICAg"
    "ICAgIHJldHVybiBfTi5qb2luKGNodW5rcykrX04KICAgIGRlZiBlbnVtR2FtZXMoc2VsZik6CiAg"
    "ICAgICAgY2h1bmtzID0gW10KICAgICAgICBmb3IgZ25hbWUgaW4gc2VsZi5nYW1lczoKICAgICAg"
    "ICAgICAgZ2FtZXN0ciA9IHNlbGYuZ2FtZXNbZ25hbWVdLmdldEdhbWVTdHJpbmcoKQogICAgICAg"
    "ICAgICBpZiBnYW1lc3RyOgogICAgICAgICAgICAgICAgY2h1bmtzLmFwcGVuZChnYW1lc3RyKQog"
    "ICAgICAgIHJldHVybiBiJycuam9pbihjaHVua3MpCiAgICBkZWYgdXBkYXRlUG9zKHNlbGYsIG1k"
    "KToKICAgICAgICBpZiBub3Qgc2VsZi5kaXJ0eToKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAg"
    "I0NsZWFyZWQgQkVGT1JFIHRoZSBzY2FuLCBub3QgYWZ0ZXIuIEEgL3VwZGhlcm9wb3MgdGhhdCBh"
    "cnJpdmVkIHdoaWxlCiAgICAgICAgI3RoZSBsb29wIGJlbG93IHdhcyBydW5uaW5nIHVzZWQgdG8g"
    "c2V0IGRpcnR5PVRydWUgYW5kIHRoZW4gaGF2ZSBpdAogICAgICAgICNpbW1lZGlhdGVseSBjbGVh"
    "cmVkIGFnYWluLCBzbyB0aGF0IHBsYXllcidzIG1vdmUgd2FzIG5vdCBicm9hZGNhc3QKICAgICAg"
    "ICAjdW50aWwgc29tZWJvZHkgZWxzZSBoYXBwZW5lZCB0byBtb3ZlLiBDbGVhcmluZyBmaXJzdCBt"
    "ZWFucyB0aGUgd29yc3QKICAgICAgICAjY2FzZSBpcyBvbmUgcmVkdW5kYW50IHBhc3MsIG5vdCBh"
    "IHNpbGVudGx5IGRyb3BwZWQgcG9zaXRpb24uCiAgICAgICAgc2VsZi5kaXJ0eSA9IEZhbHNlCiAg"
    "ICAgICAgI1NuYXBzaG90OiBwbGF5ZXJzIGpvaW4gYW5kIGxlYXZlIHRoZSB0b3duIHdoaWxlIHRo"
    "aXMgaXRlcmF0ZXMuCiAgICAgICAgdGcgPSBsaXN0KHNlbGYudXNlcmxpc3QpCiAgICAgICAgbW92"
    "ZXJzID0gW10KICAgICAgICBmb3IgdWNvbiBpbiB0ZzoKICAgICAgICAgICAgaWYgbm90IHVjb24u"
    "dXNlci5wb3NjaGFuZ2VkOgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgdWNv"
    "bi51c2VyLnBvc2NoYW5nZWQgPSBGYWxzZQogICAgICAgICAgICBpZiBub3QgdWNvbi51c2VyLmhl"
    "cm9kYXRhOgogICAgICAgICAgICAgICAgI0EgcGxheWVyIGlzIG9ubHkgYW5ub3VuY2VkIHRvIHRo"
    "ZSBvdGhlcnMgYnkgJGdhbWVjaGFubmVsdXNlciwKICAgICAgICAgICAgICAgICNhbmQgZ2V0R0NV"
    "bXNnKCkgZW1pdHMgbm90aGluZyBhdCBhbGwgdW50aWwgdGhlaXIgaGVyb2RhdGEgaGFzCiAgICAg"
    "ICAgICAgICAgICAjYXJyaXZlZC4gQnJvYWRjYXN0aW5nIGEgcG9zaXRpb24gZm9yIGEgaGVybyBp"
    "ZCBub2JvZHkgaGFzCiAgICAgICAgICAgICAgICAjYmVlbiB0b2xkIGFib3V0IGhhbmRzIGV2ZXJ5"
    "IGNsaWVudCBhbiB1cGRhdGUgZm9yIGEgcGxheWVyIGl0CiAgICAgICAgICAgICAgICAjZG9lcyBu"
    "b3Qga25vdyBleGlzdHMuIFdhaXQgdW50aWwgdGhleSBhcmUgYSByZWFsLCBhbm5vdW5jZWQKICAg"
    "ICAgICAgICAgICAgICNwbGF5ZXIuCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAg"
    "ICBtb3ZlcnMuYXBwZW5kKCh1Y29uLCBmJ3t1Y29uLnVzZXIud2lyZUlkKCl9I3t1Y29uLnVzZXIu"
    "cG9zZGF0YX0nKSkKICAgICAgICBpZiBub3QgbW92ZXJzOgogICAgICAgICAgICAjRXZlcnlvbmUg"
    "d2hvIHdhcyBkaXJ0eSBoYXMgc2luY2UgbGVmdCB0aGUgdG93bi4gU2VuZGluZyB0aGUKICAgICAg"
    "ICAgICAgI2FyZ3VtZW50LWxlc3MgJy91cGRoZXJvcG9zICcgdGhhdCB0aGlzIHVzZWQgdG8gcHJv"
    "ZHVjZSBqdXN0IGhhbmRzCiAgICAgICAgICAgICN0aGUgY2xpZW50IGFuIGVtcHR5IGNvbW1hbmQg"
    "dG8gcGFyc2UuCiAgICAgICAgICAgIHJldHVybgogICAgICAgICNOb2JvZHkgaXMgdG9sZCB0aGVp"
    "ciBvd24gcG9zaXRpb24uIFRoZSBjbGllbnQgaXMgdGhlIGF1dGhvcml0eSBvbgogICAgICAgICN3"
    "aGVyZSBpdHMgb3duIGhlcm8gaXMgLSBpdCBpcyB3aGF0IHNlbnQgdGhlIGNvb3JkaW5hdGVzIGlu"
    "IHRoZSBmaXJzdAogICAgICAgICNwbGFjZSAtIHNvIGVjaG9pbmcgdGhlbSBiYWNrIGEgZnJhY3Rp"
    "b24gb2YgYSBzZWNvbmQgbGF0ZXIgaXMgYXQgYmVzdAogICAgICAgICNyZWR1bmRhbnQgYW5kIGF0"
    "IHdvcnN0IGEgaGl0Y2gsIGFzIHRoZSBoZXJvIGlzIG51ZGdlZCBiYWNrIHRvIHdoZXJlCiAgICAg"
    "ICAgI2l0IHN0b29kIHdoZW4gdGhlIHBhY2tldCBsZWZ0LiBFdmVyeSBvdGhlciBicm9hZGNhc3Qg"
    "aW4gdGhpcyBmaWxlCiAgICAgICAgI2FscmVhZHkgZXhjbHVkZXMgdGhlIG9yaWdpbmF0b3IgKHNl"
    "ZSBfd29Vc2VyKTsgcG9zaXRpb25zIHdlcmUgdGhlCiAgICAgICAgI2V4Y2VwdGlvbi4gQ29zdHMg"
    "b25lIG1lc3NhZ2UgYnVpbHQgcGVyIG1vdmluZyBwbGF5ZXIsIGFuZCBub3Qgb25lCiAgICAgICAg"
    "I2V4dHJhIGJ5dGUgb24gdGhlIHdpcmU6IHRoZSBkaXN0cmlidXRvciBhbHJlYWR5IHdyaXRlcyB0"
    "byBlYWNoCiAgICAgICAgI3JlY2lwaWVudCBzZXBhcmF0ZWx5LgogICAgICAgIG1vdmVkID0gc2V0"
    "KHUgZm9yICh1LCBfKSBpbiBtb3ZlcnMpCiAgICAgICAgd2F0Y2hlcnMgPSBbYyBmb3IgYyBpbiB0"
    "ZyBpZiBjIG5vdCBpbiBtb3ZlZF0KICAgICAgICBpZiB3YXRjaGVyczoKICAgICAgICAgICAgZm9y"
    "IG1zZyBpbiBzZWxmLl9wb3NNZXNzYWdlcyhbY2ggZm9yIChfLCBjaCkgaW4gbW92ZXJzXSk6CiAg"
    "ICAgICAgICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOndhdGNoZXJzLCdtZXNzYWdlJzptc2d9KQog"
    "ICAgICAgIGZvciAodWNvbiwgXykgaW4gbW92ZXJzOgogICAgICAgICAgICBvdGhlcnMgPSBbY2gg"
    "Zm9yICh1LCBjaCkgaW4gbW92ZXJzIGlmIHUgaXMgbm90IHVjb25dCiAgICAgICAgICAgIGlmIG5v"
    "dCBvdGhlcnM6CiAgICAgICAgICAgICAgICBjb250aW51ZSAjb25seSBtb3ZlciBpbiB0aGUgdG93"
    "biwgbm90aGluZyB0byB0ZWxsIHRoZW0KICAgICAgICAgICAgZm9yIG1zZyBpbiBzZWxmLl9wb3NN"
    "ZXNzYWdlcyhvdGhlcnMpOgogICAgICAgICAgICAgICAgbWQuYWRkKHsndGFyZ2V0JzoodWNvbiwg"
    "KSwnbWVzc2FnZSc6bXNnfSkKICAgIGRlZiBfcG9zTWVzc2FnZXMoc2VsZiwgY2h1bmtzKToKICAg"
    "ICAgICAjU3BsaXQgaW50byBzZXZlcmFsIGNvbW1hbmRzIHJhdGhlciB0aGFuIG9uZSBhcmJpdHJh"
    "cmlseSBsb25nIGxpbmUuCiAgICAgICAgIy91cGRoZXJvcG9zIGlzIHRoZSBvbmx5IG1lc3NhZ2Ug"
    "d2hvc2UgbGVuZ3RoIGdyb3dzIHdpdGggdGhlIG51bWJlciBvZgogICAgICAgICNwbGF5ZXJzIC0g"
    "YSBidXN5IHRvd24gd291bGQgcHV0IGZpZnR5ICJpZCN4I3kiIGdyb3VwcyBvbiBhIHNpbmdsZQog"
    "ICAgICAgICNsaW5lLiBUaGUgcmV0YWlsIGNsaWVudCBpcyBhIDIwMDggMzItYml0IGJpbmFyeSBh"
    "bmQgaXRzIGxvYmJ5IHBhcnNlcgogICAgICAgICNjYW4gYmUgYXNzdW1lZCB0byB1c2UgZml4ZWQt"
    "c2l6ZSBidWZmZXJzOyBoYW5kaW5nIGl0IGEgbGluZSBsb25nZXIKICAgICAgICAjdGhhbiBpdCBl"
    "eHBlY3RzIGlzIHRoZSBjbGFzc2ljIHdheSB0byBjb3JydXB0IGl0cyBoZWFwIGFuZCB0YWtlIGl0"
    "CiAgICAgICAgI2Rvd24gd2l0aCBhbiBhY2Nlc3MgdmlvbGF0aW9uIHNvbWV3aGVyZSBlbHNlIGVu"
    "dGlyZWx5LiBTZXZlcmFsIHNob3J0CiAgICAgICAgI2NvbW1hbmRzIGFyZSBlcXVpdmFsZW50IGZv"
    "ciB0aGUgY2xpZW50IGFuZCBjb3N0IG9uZSBleHRyYSBoZWFkZXIKICAgICAgICAjZWFjaC4KICAg"
    "ICAgICBiYXRjaGVzID0gW10KICAgICAgICBjdXIgPSBbXQogICAgICAgIHByZWZpeCA9IGxlbign"
    "L3VwZGhlcm9wb3MgJykKICAgICAgICBjdXJsZW4gPSBwcmVmaXggI3RoZSBjb21tYW5kIHdvcmQg"
    "Y291bnRzIHRvd2FyZHMgdGhlIGxpbmUsIGl0IHdhcyBub3QKICAgICAgICAgICAgICAgICAgICAg"
    "ICAgI2JlaW5nIGNvdW50ZWQsIHNvIGEgZnVsbCBiYXRjaCBvdmVyc2hvdCB0aGUgY2FwIGJ5IDEy"
    "CiAgICAgICAgZm9yIGNoIGluIGNodW5rczoKICAgICAgICAgICAgaWYgY3VyIGFuZCBjdXJsZW4g"
    "KyBsZW4oY2gpICsgMSA+IF9NQVhfV0lSRV9MSU5FOgogICAgICAgICAgICAgICAgYmF0Y2hlcy5h"
    "cHBlbmQoY3VyKQogICAgICAgICAgICAgICAgY3VyID0gW10KICAgICAgICAgICAgICAgIGN1cmxl"
    "biA9IHByZWZpeAogICAgICAgICAgICBjdXIuYXBwZW5kKGNoKQogICAgICAgICAgICBjdXJsZW4g"
    "Kz0gbGVuKGNoKSArIDEKICAgICAgICBpZiBjdXI6CiAgICAgICAgICAgIGJhdGNoZXMuYXBwZW5k"
    "KGN1cikKICAgICAgICByZXR1cm4gW19lbSgnL3VwZGhlcm9wb3MgJyArICcgJy5qb2luKGIpKSBm"
    "b3IgYiBpbiBiYXRjaGVzXQogICAgZGVmIGRlYnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAgICBh"
    "Y3REaWN0ID0gW10KICAgICAgICBmb3IgZ24sIGcgaW4gbGlzdChzZWxmLmdhbWVzLml0ZW1zKCkp"
    "OgogICAgICAgICAgICBhY3REaWN0LmFwcGVuZChnLmRlYnVnX2RpY3QoKSkKICAgICAgICByZXR1"
    "cm4gYWN0RGljdAogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAg"
    "ICAgICAgICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUgZm9yIGMgaW4gc2VsZi51c2VybGlz"
    "dF0pLAogICAgICAgICAgICAnbWF4VXNlcnMnOnNlbGYubWF4dXNlciwKICAgICAgICAgICAgJ2dh"
    "bWVzJzp0dXBsZShbZ24gZm9yIGduIGluIHNlbGYuZ2FtZXNdKQogICAgICAgIH0KCl9NQVBOQU1F"
    "UyA9IFsnTmV0X1RfMDEnLCdOZXRfVF8wMicsJ05ldF9UXzAzJywnTmV0X1RfMDQnXSAjVE9ETyB1"
    "c2UgQ0ZHIG9iamVjdApjbGFzcyBHYW1lU3RhdGUoKToKICAgICNUT0RPIGF1dG8gZ3Jvd2FibGUg"
    "Y2hhbm5lbHMsIFttYXBuYW1lXQogICAgI1RPRE8gYXZhaWxhYmxlIGluZGV4ZXMsIFttYXBuYW1l"
    "XQogICAgZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlcik6CiAgICAgICAgI2luc3RhbmNlIGF0dHJp"
    "YnV0ZXMsIG5vdCBjbGFzcyBhdHRyaWJ1dGVzOiB0aGVzZSBtdXN0IE5PVCBiZSBzaGFyZWQKICAg"
    "ICAgICAjYmV0d2VlbiBzZXBhcmF0ZSBDb3JlU2VydmVyIGluc3RhbmNlcyAoZS5nLiBzdG9wL3N0"
    "YXJ0IGZyb20gYSBHVUkKICAgICAgICAjd2l0aGluIHRoZSBzYW1lIHByb2Nlc3MpIG9yIGxlZnRv"
    "dmVyIHBsYXllcnMvY2hhbm5lbHMgZnJvbSBhCiAgICAgICAgI3ByZXZpb3VzIHJ1biB3b3VsZCBs"
    "ZWFrIGludG8gdGhlIG5ldyBvbmUuCiAgICAgICAgc2VsZi5hY3RpdmVVc2VycyA9IHt9ICNUT0RP"
    "IHRyYWNrIHVzZXIgaGlzdG9yeT8gb3B0aW9uYWxseQogICAgICAgIHNlbGYuZ2FtZUNoYW5uZWxz"
    "ID0ge30gI2NoYW5uZWxbXSwga2V5ZWQgYnkgbWFwbmFtZQogICAgICAgIHNlbGYuc2VydmVyPXNl"
    "cnZlcgogICAgICAgIHNlbGYudXNlckxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgZm9y"
    "IG5hbWUgaW4gX01BUE5BTUVTOgogICAgICAgICAgICBmb3IgaSBpbiByYW5nZSgxKTogI1RPRE8g"
    "Y29uZmlndXJlYWJsZSB1cCB0byAyMD8KICAgICAgICAgICAgICAgIGNobk5hbWUgPSBfZ2Nobmwo"
    "bmFtZSwgMStpKQogICAgICAgICAgICAgICAgc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0gPSBH"
    "YW1lQ2hhbm5lbChzZWxmLnNlcnZlciwgY2huTmFtZSkgI1RPRE8gMSBhbmQgZ3Jvdz8KICAgIGRl"
    "ZiBjbGFpbVVzZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAjUHVibGlzaCBjb24gYXMgVEhF"
    "IGxpdmUgc2Vzc2lvbiBmb3IgbmFtZSwgYXRvbWljYWxseS4gVGhlIG9sZCBjb2RlCiAgICAgICAg"
    "I2NoZWNrZWQgZ2V0UGxheWVyKCkgZHVyaW5nIGxvZ2luIGFuZCB0aGVuIGluc2VydGVkIGludG8g"
    "YWN0aXZlVXNlcnMKICAgICAgICAjbXVjaCBsYXRlciwgaW4gX2xvYmJ5SGFuZGxlOyB0d28gY29u"
    "bmVjdGlvbnMgbG9nZ2luZyBpbiBhcyB0aGUgc2FtZQogICAgICAgICNhY2NvdW50IGF0IG9uY2Ug"
    "Ym90aCBwYXNzZWQgdGhlIGNoZWNrLCBhbmQgdGhlIHNlY29uZCBvbmUncyBpbnNlcnQKICAgICAg"
    "ICAjb3Zlcndyb3RlIHRoZSBmaXJzdC4gVGhlIGxvc2VyIHRoZW4gZGVsZXRlZCB0aGUgd2lubmVy"
    "J3MgZW50cnkgd2hlbiBpdAogICAgICAgICNkaXNjb25uZWN0ZWQsIGxlYXZpbmcgYSBjb25uZWN0"
    "ZWQgcGxheWVyIGludmlzaWJsZSB0byB0aGUgc2VydmVyIChubwogICAgICAgICNraWNrLCBubyB3"
    "aG9pcywgbm8gbWVzc2FnZXMpLgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAgICAg"
    "ICAgaWYgbmFtZSBpbiBzZWxmLmFjdGl2ZVVzZXJzOgogICAgICAgICAgICAgICAgcmV0dXJuIEZh"
    "bHNlCiAgICAgICAgICAgIHNlbGYuYWN0aXZlVXNlcnNbbmFtZV0gPSBjb24KICAgICAgICAgICAg"
    "cmV0dXJuIFRydWUKICAgIGRlZiByZWxlYXNlVXNlcihzZWxmLCBuYW1lLCBjb24pOgogICAgICAg"
    "ICNvbmx5IGNsZWFyIHRoZSBzbG90IGlmIHdlIHN0aWxsIG93biBpdCwgbmV2ZXIgc29tZW9uZSBl"
    "bHNlJ3Mgc2Vzc2lvbgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAgICAgICAgaWYg"
    "c2VsZi5hY3RpdmVVc2Vycy5nZXQobmFtZSkgaXMgY29uOgogICAgICAgICAgICAgICAgZGVsIHNl"
    "bGYuYWN0aXZlVXNlcnNbbmFtZV0KICAgIGRlZiBlbnVtZXJhdGVHQyhzZWxmKToKICAgICAgICBj"
    "aG5zID0gW10KICAgICAgICBmb3IgY2huTmFtZSBpbiBsaXN0KHNlbGYuZ2FtZUNoYW5uZWxzKToK"
    "ICAgICAgICAgICAgY2huID0gc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0KICAgICAgICAgICAg"
    "Y2hucy5hcHBlbmQod2lyZV9lbmNvZGUoZickZ2FtZWNoYW5uZWwgIntjaG5OYW1lfSIgIntsZW4o"
    "Y2huLnVzZXJsaXN0KX0iICJ7Y2huLm1heHVzZXJ9IiAiMCIgIjAiJykpICNUT0RPIEF2YWlsYWJs"
    "ZSAtIEFsbAogICAgICAgIGlmIG5vdCBjaG5zOgogICAgICAgICAgICByZXR1cm4gYicnICNzZWUg"
    "ZW51bUNoYXRzCiAgICAgICAgcmV0dXJuIF9OLmpvaW4oY2hucykrX04KICAgIGRlZiB1cGRhdGVQ"
    "b3Moc2VsZik6CiAgICAgICAgbWQgPSBzZWxmLnNlcnZlci5kaXN0CiAgICAgICAgZm9yIGNobiBp"
    "biBsaXN0KHNlbGYuZ2FtZUNoYW5uZWxzLnZhbHVlcygpKToKICAgICAgICAgICAgY2huLnVwZGF0"
    "ZVBvcyhtZCkKI2hhbmRsZXMgaW50ZXJhY3Rpb25zIGJldHdlZW4gYWxsIGVsZW1lbnRzCmNsYXNz"
    "IENvcmVTZXJ2ZXIoc29ja2V0c2VydmVyLlRocmVhZGluZ1RDUFNlcnZlcik6CiAgICBhbGxvd19y"
    "ZXVzZV9hZGRyZXNzID0gVHJ1ZSAjIFRPRE8gY2hlY2sgaWYgaW1wcm92ZXMgcmVzdGFydCB0aW1l"
    "cyB3aXRob3V0IG90aGVyIGlzc3VlcwogICAgZGFlbW9uX3RocmVhZHMgPSBUcnVlCiAgICBibG9j"
    "a19vbl9jbG9zZSA9IEZhbHNlCiAgICBfaXNfY2xvc2luZyA9IEZhbHNlCiAgICBkZWYgX19pbml0"
    "X18oc2VsZik6CiAgICAgICAgI1RPRE8gZ2V0IHZhbHVlcyBmcm9tIGNmZwogICAgICAgICNhZGRy"
    "ZXNzID0gJ2xvY2FsaG9zdCcKICAgICAgICBhZGRyZXNzID0gJycKICAgICAgICBwb3J0ID0gX1RX"
    "X0xPQkJZX1BPUlQKICAgICAgICBwcmludChmJ0luaXRpYWxpemluZyBzZXJ2ZXIgZm9yIHBvcnQg"
    "e3BvcnR9JykKICAgICAgICBzdXBlcigpLl9faW5pdF9fKChhZGRyZXNzLCBwb3J0KSwgQ29ubmVj"
    "dGlvbkhhbmRsZXIpCiAgICAgICAgc2VsZi5kaXN0ID0gTWVzc2FnZURpc3RyaWJ1dG9yKHNlbGYp"
    "CiAgICAgICAgc2VsZi5jb21wYXJzID0gQ29tbWFuZFBhcnNlcihzZWxmLmRpc3QpCiAgICAgICAg"
    "c2VsZi5zdGF0ZSA9IEdhbWVTdGF0ZShzZWxmKQogICAgICAgIHNlbGYuc3RhcnRUaW1lID0gZGF0"
    "ZXRpbWUuZGF0ZXRpbWUubm93KCkKICAgICAgICBzZWxmLnNlcnZpY2VfdGljayA9IDAKICAgICAg"
    "ICBzZWxmLl9wb3NTdG9wID0gdGhyZWFkaW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9wb3NUaHJl"
    "YWQgPSBOb25lCiAgICAgICAgI0V2ZXJ5IGxpdmUgY29ubmVjdGlvbiBoYW5kbGVyLiBzb2NrZXRz"
    "ZXJ2ZXIncyBzaHV0ZG93bigpIG9ubHkgc3RvcHMKICAgICAgICAjdGhlIGFjY2VwdCBsb29wIGFu"
    "ZCBjbG9zZXMgdGhlIGxpc3RlbmluZyBzb2NrZXQgLSBhbHJlYWR5LWVzdGFibGlzaGVkCiAgICAg"
    "ICAgI2Nvbm5lY3Rpb25zIGtlZXAgdGhlaXIgKGRhZW1vbikgdGhyZWFkcyBydW5uaW5nLCBzdGls"
    "bCByZWFkaW5nLCBzdGlsbAogICAgICAgICNsb2dnaW5nLCBmb3IgYXMgbG9uZyBhcyB0aGUgY2xp"
    "ZW50IHN0YXlzIGNvbm5lY3RlZC4gRnJvbSB0aGUgY29udHJvbAogICAgICAgICNwYW5lbCB0aGF0"
    "IGxvb2tzIGxpa2UgYSBzZXJ2ZXIgdGhhdCB3YXMgbmV2ZXIgc3RvcHBlZCBhdCBhbGwuCiAgICAg"
    "ICAgc2VsZi5fY29ubnMgPSBzZXQoKQogICAgICAgIHNlbGYuX2Nvbm5Mb2NrID0gdGhyZWFkaW5n"
    "LkxvY2soKQogICAgZGVmIHNlcnZlcl9hY3RpdmF0ZShzZWxmKToKICAgICAgICBwcmludChmJ1Nl"
    "cnZlciBTdGFydGluZyBhdCBQSUQ6IHtvcy5nZXRwaWQoKX0nKSNMT0cKICAgICAgICBzdXBlcigp"
    "LnNlcnZlcl9hY3RpdmF0ZSgpCiAgICBkZWYgZGVidWdfZGljdF9wbGF5ZXJzKHNlbGYpOgogICAg"
    "ICAgICNzbmFwc2hvdCB2aWEgbGlzdCgpIGZpcnN0OiBpdGVyYXRpbmcgdGhlIGxpdmUgZGljdCBk"
    "aXJlY3RseSByaXNrcwogICAgICAgICMnZGljdGlvbmFyeSBjaGFuZ2VkIHNpemUgZHVyaW5nIGl0"
    "ZXJhdGlvbicgd2hlbiBhIHBsYXllciBjb25uZWN0cwogICAgICAgICNvciBkaXNjb25uZWN0cyB3"
    "aGlsZSBhIG1vbml0b3JpbmcgVUkgaXMgcG9sbGluZyB0aGlzCiAgICAgICAgcmV0ID0ge30KICAg"
    "ICAgICBmb3IgbmFtZSwgY29uIGluIGxpc3Qoc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5pdGVtcygp"
    "KToKICAgICAgICAgICAgcmV0W25hbWVdID0gY29uLmRlYnVnX2RpY3QoKQogICAgICAgIHJldHVy"
    "biByZXQKICAgIGRlZiBkZWJ1Z19kaWN0X3Rvd25zKHNlbGYpOgogICAgICAgIHJldCA9IHt9CiAg"
    "ICAgICAgZm9yIG5hbWUsIGNobiBpbiBsaXN0KHNlbGYuc3RhdGUuZ2FtZUNoYW5uZWxzLml0ZW1z"
    "KCkpOgogICAgICAgICAgICByZXRbbmFtZV0gPSBjaG4uZGVidWdfZGljdCgpCiAgICAgICAgcmV0"
    "dXJuIHJldAogICAgZGVmIGRlYnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAgICByZXQgPSBbXQog"
    "ICAgICAgIGZvciBuYW1lLCBjaG4gaW4gbGlzdChzZWxmLnN0YXRlLmdhbWVDaGFubmVscy5pdGVt"
    "cygpKToKICAgICAgICAgICAgIHJldC5leHRlbmQoY2huLmRlYnVnX2Fycl9nYW1lcygpKQogICAg"
    "ICAgIHJldHVybiByZXQKICAgIGRlZiBfcG9zTG9vcChzZWxmKToKICAgICAgICAjUG9zaXRpb24g"
    "ZmFuLW91dCB1c2VkIHRvIHJpZGUgb24gc2VydmljZV9hY3Rpb25zKCksIHdoaWNoIHNvY2tldHNl"
    "cnZlcgogICAgICAgICNjYWxscyBvbmNlIHBlciBwb2xsX2ludGVydmFsIC0gb25lIHNlY29uZC4g"
    "VGhhdCB3YXMgdGhlIGNhZGVuY2UgYXQKICAgICAgICAjd2hpY2ggb3RoZXIgcGxheWVycycgbWFy"
    "a2VycyBtb3ZlZCBvbiB0aGUgbWFwOiBhIGZ1bGwgc2Vjb25kIG9mIGRlYWQKICAgICAgICAjcmVj"
    "a29uaW5nIGJldHdlZW4gdXBkYXRlcywgd2hpY2ggcmVhZHMgYXMgdGVsZXBvcnRpbmcgcmF0aGVy"
    "IHRoYW4KICAgICAgICAjd2Fsa2luZy4gSXRzIG93biB0aHJlYWQgZGVjb3VwbGVzIHRoZSBicm9h"
    "ZGNhc3QgcmF0ZSBmcm9tIHRoZSBhY2NlcHQKICAgICAgICAjbG9vcCdzIHBvbGwgcmF0ZSBzbyBp"
    "dCBjYW4gcnVuIHNldmVyYWwgdGltZXMgYSBzZWNvbmQuCiAgICAgICAgd2hpbGUgbm90IHNlbGYu"
    "X3Bvc1N0b3AuaXNfc2V0KCk6CiAgICAgICAgICAgIHBlcmlvZCA9IDEuMCAvIF9QT1NfVVBEQVRF"
    "X0haIGlmIF9QT1NfVVBEQVRFX0haID4gMCBlbHNlIDEuMAogICAgICAgICAgICAjd2FpdCgpIHJh"
    "dGhlciB0aGFuIHNsZWVwKCk6IHNodXRkb3duIGlzIGltbWVkaWF0ZSwgYW5kIHJlLXJlYWRpbmcK"
    "ICAgICAgICAgICAgI3RoZSBwZXJpb2QgZWFjaCBwYXNzIG1lYW5zIGEgY29uZmlnIGNoYW5nZSB0"
    "YWtlcyBlZmZlY3QgbGl2ZS4KICAgICAgICAgICAgaWYgc2VsZi5fcG9zU3RvcC53YWl0KHBlcmlv"
    "ZCk6CiAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAg"
    "ICBzZWxmLnN0YXRlLnVwZGF0ZVBvcygpCiAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAg"
    "ICAgICAgICAgICAgICAjbmV2ZXIgbGV0IG9uZSBiYWQgY2hhbm5lbCBraWxsIHBvc2l0aW9uIHN5"
    "bmMgZm9yIGV2ZXJ5b25lCiAgICAgICAgICAgICAgICBwcmludCgnW0xvYmJ5XSBQb3NpdGlvbiB1"
    "cGRhdGUgZXJyb3I6XG4nICsgdHJhY2ViYWNrLmZvcm1hdF9leGMoKSkKICAgIGRlZiBzZXJ2aWNl"
    "X2FjdGlvbnMoc2VsZik6ICNjYWxsZWQgZXZlcnkgcG9sbF9pbnRlcnZhbAogICAgICAgICMgdGlt"
    "ZSBpbnRlcnZhbHMKICAgICAgICAjUmVhZCBsaXZlLCBub3QgZnJvbSB0aGUgY29weSB0YWtlbiB3"
    "aGVuIHRoaXMgc2VydmVyIG9iamVjdCB3YXMgYnVpbHQuCiAgICAgICAgI0V2ZXJ5IG90aGVyIHN5"
    "bmNocm9uaXNhdGlvbiBzZXR0aW5nIHRha2VzIGVmZmVjdCBvbiBhIHJ1bm5pbmcgc2VydmVyIC0K"
    "ICAgICAgICAjYXBwbHlDb25maWcoKSB3cml0ZXMgdGhlIG1vZHVsZSBnbG9iYWxzIGFuZCB0aGUg"
    "bG9vcHMgcmUtcmVhZCB0aGVtIC0KICAgICAgICAjd2hpY2ggbWFkZSB0aGlzIHRoZSBvbmUgc3dp"
    "dGNoIGluIHRoYXQgZ3JvdXAgdGhhdCBzaWxlbnRseSBkaWQgbm90aGluZwogICAgICAgICN1bnRp"
    "bCB0aGUgbmV4dCByZXN0YXJ0LCB3aGlsZSB0aGUgR1VJIHNhaWQgb3RoZXJ3aXNlLgogICAgICAg"
    "IGlmIF9TRU5EX05PUFMgYW5kIChzZWxmLnNlcnZpY2VfdGljayUzKT09MDoKICAgICAgICAgICAg"
    "c2VsZi5kaXN0LmFkZCh7J3RhcmdldCc6c2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy52YWx1ZXMoKSwn"
    "bWVzc2FnZSc6X2VtKCcvbm9wJyl9KQogICAgICAgICAgICAjc2VuZCAnL25vcCcgdG8gYWxsIGV2"
    "ZXJ5IDMgc2VjIG9wdGlvbmFsbHkKICAgICAgICAjc2VydmljZSB0aWNrIDMgZGF5IHJlc2V0IGlu"
    "dGVydmFsIFRPRE8gdGVzdCBhbGlnbm1lbnQgd2l0aCBvdGhlciBmYWN0b3JzCiAgICAgICAgc2Vs"
    "Zi5zZXJ2aWNlX3RpY2sgPSAoc2VsZi5zZXJ2aWNlX3RpY2srMSklKDYwKjYwKjI0KjMpCiAgICAg"
    "ICAgc3VwZXIoKS5zZXJ2aWNlX2FjdGlvbnMoKQogICAgZGVmIHNlcnZlX2ZvcmV2ZXIoc2VsZik6"
    "CiAgICAgICAgZGlzdFRocmVhZCA9IHRocmVhZGluZy5UaHJlYWQodGFyZ2V0PXNlbGYuZGlzdC5z"
    "ZXJ2ZV9mb3JldmVyKQogICAgICAgIGRpc3RUaHJlYWQuc3RhcnQoKQogICAgICAgIHNlbGYuX3Bv"
    "c1N0b3AuY2xlYXIoKQogICAgICAgIHNlbGYuX3Bvc1RocmVhZCA9IHRocmVhZGluZy5UaHJlYWQo"
    "dGFyZ2V0PXNlbGYuX3Bvc0xvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNlbGYuX3Bvc1RocmVh"
    "ZC5zdGFydCgpCiAgICAgICAgI3BvbGxfaW50ZXJ2YWwgaXMgbm93IG9ubHkgdGhlIGFjY2VwdCBs"
    "b29wJ3Mgc2h1dGRvd24gcmVzcG9uc2l2ZW5lc3MgLQogICAgICAgICNwb3NpdGlvbiBicm9hZGNh"
    "c3RzIG5vIGxvbmdlciByaWRlIG9uIGl0CiAgICAgICAgc3VwZXIoKS5zZXJ2ZV9mb3JldmVyKDEp"
    "CiAgICAgICAgc2VsZi5fcG9zU3RvcC5zZXQoKQogICAgICAgIGlmIHNlbGYuX3Bvc1RocmVhZDoK"
    "ICAgICAgICAgICAgc2VsZi5fcG9zVGhyZWFkLmpvaW4odGltZW91dD0yLjApCiAgICAgICAgICAg"
    "IHNlbGYuX3Bvc1RocmVhZCA9IE5vbmUKICAgICAgICBzZWxmLmRpc3QuZW5kKCkjaW4gY2FzZSBp"
    "dCBoYXNuJ3QgYWxyZWFkeQogICAgICAgIGRpc3RUaHJlYWQuam9pbigpCiAgICBkZWYgaGFuZGxl"
    "X3NpZ25hbChzZWxmLCB0aW1lb3V0KToKICAgICAgICBkZWYgaGFuZGxlcihzaWdudW0sIF8pOgog"
    "ICAgICAgICAgICBkZWFkbGluZSA9IHRpbWUubW9ub3RvbmljKCkgKyB0aW1lb3V0CiAgICAgICAg"
    "ICAgIHNpZ25hbWUgPSBzaWduYWwuU2lnbmFscyhzaWdudW0pLm5hbWUKICAgICAgICAgICAgc2Vs"
    "Zi5faXNfY2xvc2luZyA9IFRydWUgI1RPRE8gcHJvcGVybHkgZW5kIGNvbm5lY3Rpb25zIGFmdGVy"
    "IGEgZGVsYXkKICAgICAgICAgICAgcHJpbnQoZidDbG9zaW5nIGluIHt0aW1lb3V0fScpCiAgICAg"
    "ICAgICAgICN3aGlsZSAoY3VycmVudF90aW1lIDo9IHRpbWUubW9ub3RvbmljKCkpIDwgZGVhZGxp"
    "bmU6CiAgICAgICAgICAgICMgICAgZGVsdGEgPSBpbnQoZGVhZGxpbmUgLSBjdXJyZW50X3RpbWUp"
    "CiAgICAgICAgICAgICAgICAjVE9ETyBzaWduYWwgdG8gcGxheWVycyB0aGF0IGNvbm5lY3Rpb24g"
    "aXMgc2h1dHRpbmcgZG93bgogICAgICAgICAgICAgICAgIy0gc2VsZi5zdGF0ZS5hY3RpdmVVc2Vy"
    "cy52YWx1ZXMoKQogICAgICAgICAgICAgICAgIy0gZicvYWRtaW4gU2VydmVyIGNsb3NpbmcgaW4g"
    "e2RlbHRhfScuZW5jb2RlKCdhc2NpaScpK19OCiAgICAgICAgICAgICAgICAjTE9HIENMT1NFCiAg"
    "ICAgICAgICAgICAgICAjVE9ETyBiZXR0ZXIgc2h1dGRvd24gaGFuZGxpbmcKICAgICAgICAgICAg"
    "IyAgICB0aW1lLnNsZWVwKDEpCiAgICAgICAgICAgIHRpbWUuc2xlZXAodGltZW91dCkjYWx0IHdo"
    "aWxlIG90aGVyIHN0dWZmIGlzIG9uZ29pbmcKICAgICAgICAgICAgc2VsZi5fQmFzZVNlcnZlcl9f"
    "c2h1dGRvd25fcmVxdWVzdCA9IFRydWUKICAgICAgICAgICAgI3NlbGYuc2h1dGRvd24oKSAjb25s"
    "eSBpZiBzZXJ2ZV9mb3JldmVyIGlzIGluIGEgZGlmZmVyZW50IHRocmVhZAogICAgICAgICAgICAj"
    "c2VsZi5zZXJ2ZXJfY2xvc2UoKSAjb25seSBuZWVkZWQgaWYgbm90IHVzaW5nIGEgd2l0aCBzdGF0"
    "ZW1lbnQKICAgICAgICByZXR1cm4gaGFuZGxlcgogICAgZGVmIHJlZ2lzdGVyQ29ubmVjdGlvbihz"
    "ZWxmLCBjb24pOgogICAgICAgIHdpdGggc2VsZi5fY29ubkxvY2s6CiAgICAgICAgICAgIHNlbGYu"
    "X2Nvbm5zLmFkZChjb24pCiAgICBkZWYgdW5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZiwgY29uKToK"
    "ICAgICAgICB3aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBzZWxmLl9jb25ucy5kaXNj"
    "YXJkKGNvbikKICAgIGRlZiBjbG9zZUNvbm5lY3Rpb25zKHNlbGYpOgogICAgICAgICNEcm9wIGV2"
    "ZXJ5IGNsaWVudC4gU2h1dHRpbmcgdGhlIHNvY2tldCBkb3duIHVuYmxvY2tzIHdoaWNoZXZlcgog"
    "ICAgICAgICNzZWxlY3QoKS9yZWN2KCkgdGhhdCBjb25uZWN0aW9uJ3MgdGhyZWFkIGlzIHNpdHRp"
    "bmcgaW4sIHNvIGl0IHJ1bnMKICAgICAgICAjaXRzIG5vcm1hbCBjbGVhbnVwIHBhdGggYW5kIGV4"
    "aXRzIGluc3RlYWQgb2YgbGluZ2VyaW5nLgogICAgICAgIHdpdGggc2VsZi5fY29ubkxvY2s6CiAg"
    "ICAgICAgICAgIGNvbm5zID0gbGlzdChzZWxmLl9jb25ucykKICAgICAgICBmb3IgY29uIGluIGNv"
    "bm5zOgogICAgICAgICAgICBjb24uZHJvcCgpCiAgICAgICAgcmV0dXJuIGxlbihjb25ucykKICAg"
    "IGRlZiBzaHV0ZG93bihzZWxmKToKICAgICAgICAjU3RvcHBpbmcgdGhlIHNlcnZlciBtZWFucyBz"
    "dG9wcGluZyBpdDogZmxhZyBpdCBmaXJzdCBzbyB0aGUgcmVhZAogICAgICAgICNsb29wcyBiYWls"
    "IG91dCByYXRoZXIgdGhhbiBzZXJ2aW5nIGFub3RoZXIgY29tbWFuZCwgdGhlbiBzdG9wIHRoZQog"
    "ICAgICAgICNhY2NlcHQgbG9vcCwgdGhlbiBldmljdCBldmVyeW9uZSBzdGlsbCBjb25uZWN0ZWQu"
    "CiAgICAgICAgc2VsZi5faXNfY2xvc2luZyA9IFRydWUKICAgICAgICBzdXBlcigpLnNodXRkb3du"
    "KCkKICAgICAgICBuID0gc2VsZi5jbG9zZUNvbm5lY3Rpb25zKCkKICAgICAgICBpZiBuOgogICAg"
    "ICAgICAgICBwcmludChmJ1tMb2JieV0gQ2xvc2VkIHtufSBjbGllbnQgY29ubmVjdGlvbihzKSBv"
    "biBzaHV0ZG93bicpCiAgICBkZWYgZ2V0UGxheWVyKHNlbGYsIHVzZXJuYW1lKToKICAgICAgICBy"
    "ZXR1cm4gc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5nZXQodXNlcm5hbWUpCiAgICBkZWYga2lja1Bs"
    "YXllcihzZWxmLCB1c2VybmFtZSwgcmVhc29uPSdLaWNrZWQgYnkgYWRtaW4nKToKICAgICAgICAj"
    "QWRtaW4tcGFuZWwgYWN0aW9uOiBmb3JjaWJseSBkaXNjb25uZWN0IGEgY29ubmVjdGVkIHBsYXll"
    "ci4gU2VuZHMgYQogICAgICAgICNiZXN0LWVmZm9ydCAvYWRtaW4gbm90aWNlIGZpcnN0IChjbGll"
    "bnQgc2hvd3MgaXQgbGlrZSBhbnkgb3RoZXIKICAgICAgICAjc2VydmVyIGFkbWluIG1lc3NhZ2Up"
    "LCB0aGVuIHNodXRzIGRvd24gdGhlIHNvY2tldCBzbyB0aGUgcGxheWVyJ3MKICAgICAgICAjaGFu"
    "ZGxlciB0aHJlYWQgdW5ibG9ja3MgZnJvbSBpdHMgcmVjdigpIGFuZCBydW5zIGl0cyBub3JtYWwK"
    "ICAgICAgICAjZGlzY29ubmVjdC9jbGVhbnVwIHBhdGguCiAgICAgICAgY29uID0gc2VsZi5nZXRQ"
    "bGF5ZXIodXNlcm5hbWUpCiAgICAgICAgaWYgY29uIGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVy"
    "biBGYWxzZQogICAgICAgICNRdWV1ZWQsIG5vdCB3cml0dGVuIGlubGluZS4gc2VuZFJhdygpIHRh"
    "a2VzIHRoYXQgY29ubmVjdGlvbidzIHNlbmQKICAgICAgICAjbG9jaywgYW5kIGl0cyB3cml0ZXIg"
    "dGhyZWFkIGhvbGRzIHRoYXQgbG9jayBmb3IgdGhlIHdob2xlIG9mIGEKICAgICAgICAjYmxvY2tp"
    "bmcgc2VuZGFsbCgpIC0gc28ga2lja2luZyBhIHBsYXllciB3aG9zZSBsaW5rIGhhZCBzdGFsbGVk"
    "IGJsb2NrZWQKICAgICAgICAjd2hvZXZlciBjYWxsZWQgdGhpcyB1bnRpbCB0aGUgc3RhbGxlZCBj"
    "bGllbnQgd2VudCBhd2F5LCBhbmQgdGhlIGNhbGxlcgogICAgICAgICNoZXJlIGlzIHRoZSBHVUkg"
    "dGhyZWFkLiBUaGUgYWRtaW4gcGFuZWwgZnJvemUgb24gZXhhY3RseSB0aGUgcGxheWVyIGl0CiAg"
    "ICAgICAgI3dhcyB0cnlpbmcgdG8gZ2V0IHJpZCBvZi4gQSBxdWV1ZSBwdXQgY2Fubm90IGJsb2Nr"
    "LgogICAgICAgIHRyeToKICAgICAgICAgICAgY29uLnNlbmQoX2VtKGYnL2FkbWluIHtyZWFzb259"
    "JykpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFzcyAjYmVzdCBlZmZv"
    "cnQsIGNvbm5lY3Rpb24gbWF5IGFscmVhZHkgYmUgb24gaXRzIHdheSBvdXQKICAgICAgICBjb24u"
    "Zmx1c2hQZW5kaW5nKDAuMykgI2JvdW5kZWQ6IGdpdmUgdGhlIG5vdGljZSBhIGNoYW5jZSB0byBn"
    "byBvdXQKICAgICAgICBjb24uZHJvcCgpCiAgICAgICAgcmV0dXJuIFRydWUKICAgIGRlZiBkZWxl"
    "dGVBY2NvdW50KHNlbGYsIHVzZXJuYW1lKToKICAgICAgICAjQWRtaW4tcGFuZWwgYWN0aW9uOiBw"
    "ZXJtYW5lbnRseSBkZWxldGVzIGEgY2hhcmFjdGVyL2FjY291bnQuCiAgICAgICAgI0tpY2tzIGZp"
    "cnN0IChuby1vcCBpZiBhbHJlYWR5IG9mZmxpbmUpIHNvIGEgY29ubmVjdGVkIGNsaWVudCBuZXZl"
    "cgogICAgICAgICNrZWVwcyBwbGF5aW5nIG9uIGFuIGFjY291bnQgdGhhdCBoYXMganVzdCB2YW5p"
    "c2hlZCBmcm9tIHRoZSBEQi4KICAgICAgICBzZWxmLmtpY2tQbGF5ZXIodXNlcm5hbWUsIHJlYXNv"
    "bj0nQWNjb3VudCBkZWxldGVkIGJ5IGFkbWluJykKICAgICAgICByZXR1cm4gR0RILmRlbGV0ZUFj"
    "Y291bnQodXNlcm5hbWUpCiNGYWlsZWQtbG9naW4gdGhyb3R0bGUsIHBlciBzb3VyY2UgSVAuCiNU"
    "d28gcmVhc29ucyB0aGlzIGlzIG5vdCBvcHRpb25hbCBvbiBhIHNlcnZlciByZWFjaGFibGUgZnJv"
    "bSB0aGUgaW50ZXJuZXQ6CiNhIHBhc3N3b3JkIGd1ZXNzIGlzIGNoZWFwIGZvciB0aGUgYXR0YWNr"
    "ZXIgYnV0IGNvc3RzICp1cyogYSAxMDBrLWl0ZXJhdGlvbgojUEJLREYyICh0ZW5zIG9mIG1zIG9m"
    "IENQVSBlYWNoKSwgc28gYW4gdW50aHJvdHRsZWQgbG9naW4gZW5kcG9pbnQgaXMgYm90aCBhCiNi"
    "cnV0ZS1mb3JjZSBvcmFjbGUgYW5kIGEgQ1BVIGFtcGxpZmllciAtIGEgaGFuZGZ1bCBvZiBjb25u"
    "ZWN0aW9ucyBjYW4gcGluCiNldmVyeSBjb3JlLiBTdWNjZXNzZnVsIGxvZ2lucyBjbGVhciB0aGUg"
    "Y291bnRlciwgc28gYSBwbGF5ZXIgZnVtYmxpbmcgdGhlaXIKI3Bhc3N3b3JkIGEgZmV3IHRpbWVz"
    "IGlzIG5ldmVyIGxvY2tlZCBvdXQgZm9yIGxvbmcuCl9MT0dJTl9GQUlMX0xJTUlUID0gNiAgICAg"
    "ICNmYWlsdXJlcyBhbGxvd2VkIGluc2lkZSB0aGUgd2luZG93IGJlZm9yZSBkZWxheWluZwpfTE9H"
    "SU5fRkFJTF9XSU5ET1cgPSAzMDAgICAjc2Vjb25kcyBhIGZhaWx1cmUgaXMgcmVtZW1iZXJlZApf"
    "TE9HSU5fRkFJTF9ERUxBWSA9IDIuMCAgICAjc2Vjb25kcyB0byBzdGFsbCBlYWNoIGF0dGVtcHQg"
    "b25jZSBvdmVyIHRoZSBsaW1pdApjbGFzcyBMb2dpblRocm90dGxlKCk6CiAgICBkZWYgX19pbml0"
    "X18oc2VsZik6CiAgICAgICAgc2VsZi5sb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIHNl"
    "bGYuZmFpbHMgPSB7fSAjaXAgLT4gW3RpbWVzdGFtcHNdCiAgICBkZWYgX3BydW5lKHNlbGYsIGlw"
    "LCBub3cpOgogICAgICAgIHJlY2VudCA9IFt0IGZvciB0IGluIHNlbGYuZmFpbHMuZ2V0KGlwLCAo"
    "KSkgaWYgbm93IC0gdCA8IF9MT0dJTl9GQUlMX1dJTkRPV10KICAgICAgICBpZiByZWNlbnQ6CiAg"
    "ICAgICAgICAgIHNlbGYuZmFpbHNbaXBdID0gcmVjZW50CiAgICAgICAgZWxzZToKICAgICAgICAg"
    "ICAgc2VsZi5mYWlscy5wb3AoaXAsIE5vbmUpCiAgICAgICAgcmV0dXJuIHJlY2VudAogICAgZGVm"
    "IGRlbGF5Rm9yKHNlbGYsIGlwKToKICAgICAgICBub3cgPSB0aW1lLm1vbm90b25pYygpCiAgICAg"
    "ICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJlY2VudCA9IHNlbGYuX3BydW5lKGlwLCBu"
    "b3cpCiAgICAgICAgcmV0dXJuIF9MT0dJTl9GQUlMX0RFTEFZIGlmIGxlbihyZWNlbnQpID49IF9M"
    "T0dJTl9GQUlMX0xJTUlUIGVsc2UgMC4wCiAgICBkZWYgcmVjb3JkRmFpbHVyZShzZWxmLCBpcCk6"
    "CiAgICAgICAgbm93ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgog"
    "ICAgICAgICAgICByZWNlbnQgPSBzZWxmLl9wcnVuZShpcCwgbm93KQogICAgICAgICAgICByZWNl"
    "bnQuYXBwZW5kKG5vdykKICAgICAgICAgICAgc2VsZi5mYWlsc1tpcF0gPSByZWNlbnQKICAgICAg"
    "ICAgICAgcmV0dXJuIGxlbihyZWNlbnQpCiAgICBkZWYgcmVjb3JkU3VjY2VzcyhzZWxmLCBpcCk6"
    "CiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHNlbGYuZmFpbHMucG9wKGlwLCBO"
    "b25lKQpMT0dJTl9USFJPVFRMRSA9IExvZ2luVGhyb3R0bGUoKQoKX0xPR0lOX0VSUk9SUyA9IHsK"
    "ICAgIDE6ICdJbnZhbGlkIHVzZXJuYW1lIG9yIHBhc3N3b3JkJywKICAgIDI6ICdBY2NvdW50IGFs"
    "cmVhZHkgbG9nZ2VkIGluJywKICAgIDM6ICdQYXNzd29yZCByZXF1aXJlZCcsCiAgICA0OiAnVXNl"
    "cm5hbWUgcmVxdWlyZWQnLAogICAgI0FjY291bnRzIGFyZSB0aWVkIHRvIHRoZSBzZXJpYWwgdGhl"
    "IGNsaWVudCBoYW5kc2hha2VzIHdpdGgsIHNvIGEKICAgICNyZWluc3RhbGxlZCBvciByZS1rZXll"
    "ZCBnYW1lIGNhbm5vdCByZWFjaCBhbiBleGlzdGluZyBhY2NvdW50IG5vIG1hdHRlcgogICAgI3do"
    "YXQgcGFzc3dvcmQgaXQgdHlwZXMuIFNheSB0aGF0LCByYXRoZXIgdGhhbiBibGFtaW5nIHRoZSBu"
    "YW1lLgogICAgNTogJ1RoaXMgbmFtZSBiZWxvbmdzIHRvIGFuIGFjY291bnQgcmVnaXN0ZXJlZCB3"
    "aXRoIGEgZGlmZmVyZW50IGdhbWUgc2VyaWFsJywKfQpfUkVHSVNURVJfRVJST1JTID0gewogICAg"
    "MTogJ0FjY291bnQgYWxyZWFkeSBsb2dnZWQgaW4nLAogICAgMjogJ1VzZXJuYW1lIHVuYXZhaWxh"
    "YmxlIG9yIGludmFsaWQnLAp9CiNDZWlsaW5nIG9uIGhvdyBtdWNoIHVuc2VudCBkYXRhIG1heSBw"
    "aWxlIHVwIGZvciBhIHNpbmdsZSBjbGllbnQuIFRoZSB3cml0ZXIKI3RocmVhZCBibG9ja3MgaW5z"
    "aWRlIHNlbmRhbGwoKSBmb3IgZXhhY3RseSBhcyBsb25nIGFzIGEgY2xpZW50IHJlZnVzZXMgdG8g"
    "cmVhZCwKI2FuZCBhIGZyb3plbiBnYW1lIGRvZXMgcHJlY2lzZWx5IHRoYXQgLSB3aGlsZSBhbHNv"
    "IHNlbmRpbmcgbm90aGluZywgc28gbm90aGluZwojZWxzZSBub3RpY2VzIGl0IHVudGlsIGEgZnVs"
    "bCBpZGxlIHRpbWVvdXQgaGFzIHBhc3NlZC4gRm9yIHRob3NlIG1pbnV0ZXMgZXZlcnkKI3Bvc2l0"
    "aW9uIGJyb2FkY2FzdCwgZXZlcnkgY2hhdCBsaW5lIGFuZCBldmVyeSByZWxheWVkIGdhbWUgY29t"
    "bWFuZCBmb3IgdGhhdAojcGxheWVyIGtlcHQgYmVpbmcgYXBwZW5kZWQgdG8gYW4gdW5ib3VuZGVk"
    "IHF1ZXVlLiBCb3VuZGluZyBpdCB0dXJucyAidGhlIHNlcnZlcgojcXVpZXRseSBncm93cyBvbiBi"
    "ZWhhbGYgb2YgYSBjbGllbnQgdGhhdCBpcyBhbHJlYWR5IGdvbmUiIGludG8gYSBjbGVhbiBkcm9w"
    "CiN3aXRoIGEgbGluZSBpbiB0aGUgbG9nLiBTaXplZCBmYXIgYWJvdmUgYW55IGxlZ2l0aW1hdGUg"
    "YnVyc3Q6IHRoZSBsYXJnZXN0CiNzaW5nbGUgdGhpbmcgdGhhdCBnb2VzIG91dCBpcyBhIGhlcm9k"
    "YXRhIGJsb2IsIGFuZCBhIHdob2xlIHRvd24gb2YgdGhlbSBkb2VzCiNub3QgY29tZSBjbG9zZS4K"
    "X01BWF9TRU5EX0JBQ0tMT0cgPSA0ICogMTAyNCAqIDEwMjQKI2hhbmRsZXMgaW5kaXZpZHVhbCBj"
    "b25uZWN0aW9ucwpjbGFzcyBDb25uZWN0aW9uSGFuZGxlcihzb2NrZXRzZXJ2ZXIuQmFzZVJlcXVl"
    "c3RIYW5kbGVyKToKICAgICNkZWZhdWx0IHByb3BlcnRpZXM6CiAgICAjIC0gcmVxdWVzdDogc29j"
    "a2V0IHRvIGRlc3RpbmF0aW9uCiAgICAjIC0gY2xpZW50X2FkZHJlc3MKICAgICMgLSBzZXJ2ZXI6"
    "IENvcmVTZXJ2ZXIKICAgIF9TVE9QV1JJVEVSID0gb2JqZWN0KCkKICAgIGRlZiBzZXR1cChzZWxm"
    "KToKICAgICAgICBzZWxmLl9zUXVldWUgPSBTaW1wbGVRdWV1ZSgpCiAgICAgICAgI0J5dGVzIHF1"
    "ZXVlZCBidXQgbm90IHlldCBoYW5kZWQgdG8gc2VuZGFsbCgpLCBhbmQgdGhlIGZsYWcgdGhhdCBz"
    "YXlzCiAgICAgICAgI3RoaXMgY29ubmVjdGlvbiBoYXMgYWxyZWFkeSBiZWVuIGdpdmVuIHVwIG9u"
    "IGZvciBleGNlZWRpbmcgdGhlIGNhcC4KICAgICAgICBzZWxmLl9xQnl0ZXMgPSAwCiAgICAgICAg"
    "c2VsZi5fcUxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgc2VsZi5fb3ZlcmZsb3dlZCA9"
    "IEZhbHNlCiAgICAgICAgc2VsZi51c2VyID0gTm9uZQogICAgICAgIHNlbGYuZ3VpZCA9IE5vbmUK"
    "ICAgICAgICBzZWxmLmRhdGEgPSBiJycKICAgICAgICBzZWxmLlNLID0gYnl0ZWFycmF5KHN0cnVj"
    "dC5wYWNrKCc8SUknLCAweEE2QUUxRjlCLCAweDQzOERGRjQwKSkKICAgICAgICAjU2VyaWFsaXNl"
    "cyB0aGUgcmF3IHNvY2tldCB3cml0ZXMuIFRocmVlIHRocmVhZHMgY2FuIHdhbnQgdG8gd3JpdGUg"
    "dG8KICAgICAgICAjb25lIGNsaWVudDogdGhpcyBjb25uZWN0aW9uJ3Mgb3duIHJlYWQgbG9vcCAo"
    "ZHVyaW5nIHRoZSBoYW5kc2hha2UpLAogICAgICAgICNpdHMgd3JpdGVyIHRocmVhZCwgYW5kIHRo"
    "ZSBHVUkgdGhyZWFkIHZpYSBraWNrUGxheWVyKCkuIFdpdGhvdXQgdGhlCiAgICAgICAgI2xvY2sg"
    "dHdvIHNlbmRhbGwoKSBjYWxscyBjYW4gaW50ZXJsZWF2ZSBhbmQgc3BsaXQgYSBwYWNrZXQgZG93"
    "biB0aGUKICAgICAgICAjbWlkZGxlLCB3aGljaCB0aGUgY2xpZW50IHNlZXMgYXMgcHJvdG9jb2wg"
    "Z2FyYmFnZS4KICAgICAgICBzZWxmLl9zZW5kTG9jayA9IHRocmVhZGluZy5Mb2NrKCkKICAgICAg"
    "ICBzZWxmLl93cml0ZXIgPSBOb25lCiAgICAgICAgc2VsZi5fd3JpdGVyRGVhZCA9IHRocmVhZGlu"
    "Zy5FdmVudCgpCiAgICAgICAgI1NldCB3aGVuIHRoaXMgY29ubmVjdGlvbiBoYXMgYmVlbiBnaXZl"
    "biB1cCBvbiBmcm9tICpvdXRzaWRlKiBpdHMgb3duCiAgICAgICAgI2hhbmRsZXIgdGhyZWFkIC0g"
    "YW4gYWRtaW4ga2ljaywgb3IgdGhlIHNlbmQtYmFja2xvZyBjYXAuIFNodXR0aW5nIHRoZQogICAg"
    "ICAgICNzb2NrZXQgZG93biBpcyBzdXBwb3NlZCB0byB3YWtlIHRoYXQgdGhyZWFkIG9uIGl0cyBv"
    "d24sIGFuZCBub3JtYWxseQogICAgICAgICNkb2VzOyB0aGlzIG1ha2VzIGl0IGNlcnRhaW4gcmF0"
    "aGVyIHRoYW4gZGVwZW5kZW50IG9uIHRoZSBzb2NrZXQKICAgICAgICAjcmVwb3J0aW5nIHRoZSBz"
    "aHV0ZG93biBwcm9tcHRseS4gQSBraWNrIHRoYXQgaXMgbm90IG5vdGljZWQgbGVhdmVzIHRoZQog"
    "ICAgICAgICNhY2NvdW50IGNsYWltZWQsIGFuZCB0aGUgcGxheWVyIGNhbm5vdCBnZXQgYmFjayBp"
    "biB1bnRpbCB0aGUgaWRsZQogICAgICAgICN0aW1lb3V0IGV4cGlyZXMgLSB0aGUgZXhhY3QgZmFp"
    "bHVyZSBhIGtpY2sgaXMgbWVhbnQgdG8gcmVzb2x2ZS4KICAgICAgICBzZWxmLl9kcm9wcGVkID0g"
    "dGhyZWFkaW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3Rvbmlj"
    "KCkKICAgICAgICBzZWxmLnNlcnZlci5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZikKICAgICAgICB0"
    "cnk6CiAgICAgICAgICAgICNOYWdsZSBiYXRjaGVzIHNtYWxsIHdyaXRlcyBieSBob2xkaW5nIHRo"
    "ZW0gZm9yIHVwIHRvIH40MG1zIHdhaXRpbmcKICAgICAgICAgICAgI2ZvciBtb3JlIGRhdGEuIEV2"
    "ZXJ5IG1lc3NhZ2UgdGhpcyBzZXJ2ZXIgc2VuZHMgaXMgc21hbGwgYW5kCiAgICAgICAgICAgICNs"
    "YXRlbmN5LXNlbnNpdGl2ZSAtIGNoYXQsIHBvc2l0aW9uIHVwZGF0ZXMgYW5kIGFib3ZlIGFsbCB0"
    "aGUKICAgICAgICAgICAgIy9nYW1lY29tbWFuZHRvdXNlciByZWxheSB0aGF0IGNhcnJpZXMgdGhl"
    "IGFjdHVhbCBpbi1nYW1lIGNvLW9wCiAgICAgICAgICAgICN0cmFmZmljIGJldHdlZW4gdHdvIHBs"
    "YXllcnMgLSBzbyB0aGUgZGVsYXkgaXMgcHVyZSBhZGRlZCBsYWcuCiAgICAgICAgICAgIHNlbGYu"
    "cmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5JUFBST1RPX1RDUCwgc29ja2V0LlRDUF9OT0RFTEFZ"
    "LCAxKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICBwYXNzICNub3QgZmF0YWws"
    "IGp1c3Qgc2xvd2VyCiAgICAgICAgdHJ5OgogICAgICAgICAgICAjQXNrIHRoZSBPUyB0byBwcm9i"
    "ZSBhbiBpZGxlIGNvbm5lY3Rpb24uIFdoZW4gYSBwbGF5ZXIncyBnYW1lCiAgICAgICAgICAgICNj"
    "cmFzaGVzIG91dHJpZ2h0IHRoZSBzb2NrZXQgaXMgdXN1YWxseSByZXNldCBhbmQgd2UgZmluZCBv"
    "dXQgYXQKICAgICAgICAgICAgI29uY2UsIGJ1dCBhIG1hY2hpbmUgdGhhdCBmcmVlemVzLCBzbGVl"
    "cHMgb3IgbG9zZXMgaXRzIGxpbmsgc2VuZHMKICAgICAgICAgICAgI25vdGhpbmcgYXQgYWxsOiB3"
    "aXRob3V0IHByb2JlcyB0aGF0IGNvbm5lY3Rpb24gc2l0cyB0aGVyZSBob2xkaW5nCiAgICAgICAg"
    "ICAgICN0aGUgYWNjb3VudCAoIkFjY291bnQgYWxyZWFkeSBsb2dnZWQgaW4iKSBhbmQgaXRzIHJv"
    "b20gdW50aWwgdGhlCiAgICAgICAgICAgICNpZGxlIHRpbWVvdXQgZXhwaXJlcyBtaW51dGVzIGxh"
    "dGVyLiBQcm9iZSBhZnRlciAzMHMgaWRsZSwgdGhlbgogICAgICAgICAgICAjZXZlcnkgNXMuCiAg"
    "ICAgICAgICAgIHNlbGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5TT0xfU09DS0VULCBzb2Nr"
    "ZXQuU09fS0VFUEFMSVZFLCAxKQogICAgICAgICAgICBpZiBoYXNhdHRyKHNlbGYucmVxdWVzdCwg"
    "J2lvY3RsJykgYW5kIGhhc2F0dHIoc29ja2V0LCAnU0lPX0tFRVBBTElWRV9WQUxTJyk6CiAgICAg"
    "ICAgICAgICAgICBzZWxmLnJlcXVlc3QuaW9jdGwoc29ja2V0LlNJT19LRUVQQUxJVkVfVkFMUywg"
    "KDEsIDMwMDAwLCA1MDAwKSkgI1dpbmRvd3MKICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAg"
    "ICAgIGZvciAob3B0LCB2YWwpIGluICgoJ1RDUF9LRUVQSURMRScsIDMwKSwgKCdUQ1BfS0VFUElO"
    "VFZMJywgNSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgKCdUQ1BfS0VFUENO"
    "VCcsIDQpKToKICAgICAgICAgICAgICAgICAgICBpZiBoYXNhdHRyKHNvY2tldCwgb3B0KToKICAg"
    "ICAgICAgICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LklQUFJP"
    "VE9fVENQLCBnZXRhdHRyKHNvY2tldCwgb3B0KSwgdmFsKQogICAgICAgIGV4Y2VwdCBPU0Vycm9y"
    "OgogICAgICAgICAgICBwYXNzICNrZWVwYWxpdmUgaXMgYW4gb3B0aW1pc2F0aW9uLCBub3QgYSBy"
    "ZXF1aXJlbWVudAogICAgZGVmIHNlbmRSYXcoc2VsZiwgbXNnKToKICAgICAgICAjVGhlIHNpbmds"
    "ZSBmdW5uZWwgZm9yIGV2ZXJ5IGJ5dGUgbGVhdmluZyB0aGUgc2VydmVyIG9uIHRoaXMgc29ja2V0"
    "LgogICAgICAgIHdpdGggc2VsZi5fc2VuZExvY2s6CiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5z"
    "ZW5kYWxsKG1zZykKICAgIGRlZiBzZW5kKHNlbGYsIG1zZyk6CiAgICAgICAgI05vcm1hbCBwYXRo"
    "IG9uY2UgdGhlIGNvbm5lY3Rpb24gaXMgbGl2ZTogaGFuZCBvZmYgdG8gdGhlIHdyaXRlciB0aHJl"
    "YWQKICAgICAgICAjc28gdGhlIGNhbGxlciAoYSBjb21tYW5kIGhhbmRsZXIsIG9yIHRoZSBkaXN0"
    "cmlidXRvcidzIGZhbi1vdXQpIG5ldmVyCiAgICAgICAgI2Jsb2NrcyBvbiBhIHNsb3cgb3Igc3Rh"
    "bGxlZCBjbGllbnQuCiAgICAgICAgaWYgbm90IG1zZzoKICAgICAgICAgICAgcmV0dXJuCiAgICAg"
    "ICAgd2l0aCBzZWxmLl9xTG9jazoKICAgICAgICAgICAgaWYgc2VsZi5fb3ZlcmZsb3dlZDoKICAg"
    "ICAgICAgICAgICAgIHJldHVybiAjYWxyZWFkeSBiZWluZyB0b3JuIGRvd24sIHN0b3AgYWNjb3Vu"
    "dGluZyBmb3IgaXQKICAgICAgICAgICAgc2VsZi5fcUJ5dGVzICs9IGxlbihtc2cpCiAgICAgICAg"
    "ICAgIG92ZXIgPSBzZWxmLl9xQnl0ZXMgPiBfTUFYX1NFTkRfQkFDS0xPRwogICAgICAgICAgICBz"
    "ZWxmLl9vdmVyZmxvd2VkID0gb3ZlcgogICAgICAgIGlmIG92ZXI6CiAgICAgICAgICAgICNTZWUg"
    "X01BWF9TRU5EX0JBQ0tMT0cuIFNodXR0aW5nIHRoZSBzb2NrZXQgZG93biBpcyB3aGF0IHRlbGxz"
    "IHRoZQogICAgICAgICAgICAjcmVhZCBsb29wIHRvIHJ1biB0aGlzIGNvbm5lY3Rpb24ncyBub3Jt"
    "YWwgY2xlYW51cCBwYXRoLgogICAgICAgICAgICB3aG8gPSBzZWxmLnVzZXIubmFtZSBpZiBzZWxm"
    "LnVzZXIgZWxzZSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAgICAgICAgIHByaW50KGYnW0xv"
    "YmJ5XSB7d2hvfTogb3ZlciB7X01BWF9TRU5EX0JBQ0tMT0d9IGJ5dGVzIHF1ZXVlZCB1bnJlYWQs"
    "IGRyb3BwaW5nJykKICAgICAgICAgICAgc2VsZi5kcm9wKCkKICAgICAgICAgICAgcmV0dXJuCiAg"
    "ICAgICAgc2VsZi5fc1F1ZXVlLnB1dChtc2cpCiAgICBkZWYgZHJvcChzZWxmKToKICAgICAgICAj"
    "RW5kIHRoaXMgY29ubmVjdGlvbiBmcm9tIGFub3RoZXIgdGhyZWFkLiBGbGFnZ2luZyBpdCBmaXJz"
    "dCBtZWFucyB0aGUKICAgICAgICAjcmVhZCBsb29wIGJhaWxzIG91dCBhdCBpdHMgbmV4dCBwYXNz"
    "IG5vIG1hdHRlciB3aGF0IHRoZSBzb2NrZXQgZG9lczsKICAgICAgICAjdGhlIHNodXRkb3duIGlz"
    "IHdoYXQgd2FrZXMgaXQgZnJvbSBzZWxlY3QoKSBzdHJhaWdodCBhd2F5LiBJdHMgb3duCiAgICAg"
    "ICAgI2hhbmRsZXIgdGhyZWFkIHN0aWxsIHJ1bnMgdGhlIG5vcm1hbCBmaW5pc2goKS9jbGVhbnVw"
    "IHBhdGgsIHNvIHRoZQogICAgICAgICNhY2NvdW50IGlzIHJlbGVhc2VkIGFuZCB0aGUgdG93biBy"
    "b3N0ZXIgdGlkaWVkIGV4YWN0bHkgYXMgb24gYW55IG90aGVyCiAgICAgICAgI2Rpc2Nvbm5lY3Qu"
    "IE5ldmVyIGNsb3NlKCkgaGVyZSAtIHNlZSBjbG9zZUNvbm5lY3Rpb25zKCkuCiAgICAgICAgc2Vs"
    "Zi5fZHJvcHBlZC5zZXQoKQogICAgICAgIHRyeToKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNo"
    "dXRkb3duKHNvY2tldC5TSFVUX1JEV1IpCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAg"
    "ICAgIHBhc3MgI2FscmVhZHkgZ29uZSwgb3IgbmV2ZXIgZnVsbHkgY29ubmVjdGVkCiAgICBkZWYg"
    "Zmx1c2hQZW5kaW5nKHNlbGYsIHRpbWVvdXQpOgogICAgICAgICNCZXN0LWVmZm9ydCwgc3RyaWN0"
    "bHkgYm91bmRlZCB3YWl0IGZvciB0aGUgb3V0Ym91bmQgcXVldWUgdG8gZHJhaW4uCiAgICAgICAg"
    "I0ZvciBjYWxsZXJzIHRoYXQgd2FudCBhIGxhc3QgbWVzc2FnZSB0byBoYXZlIGxlZnQgYmVmb3Jl"
    "IHRoZSBzb2NrZXQKICAgICAgICAjZ29lcyBkb3duICh0aGUgYWRtaW4ga2ljaykgd2l0aG91dCBp"
    "bmhlcml0aW5nIGEgc3RhbGxlZCBwZWVyJ3Mgc3RhbGwuCiAgICAgICAgZGVhZGxpbmUgPSB0aW1l"
    "Lm1vbm90b25pYygpICsgdGltZW91dAogICAgICAgIHdoaWxlIG5vdCBzZWxmLl9zUXVldWUuZW1w"
    "dHkoKSBhbmQgdGltZS5tb25vdG9uaWMoKSA8IGRlYWRsaW5lOgogICAgICAgICAgICB0aW1lLnNs"
    "ZWVwKDAuMDIpCiAgICBkZWYgX3dyaXRlckxvb3Aoc2VsZik6CiAgICAgICAgI0Jsb2NrcyBvbiB0"
    "aGUgcXVldWUgaW5zdGVhZCBvZiBiZWluZyBwb2xsZWQuIFByZXZpb3VzbHkgdGhlIHJlYWQgbG9v"
    "cAogICAgICAgICNkcmFpbmVkIHRoaXMgcXVldWUgaXRzZWxmIGJldHdlZW4gcmVjdigpIHRpbWVv"
    "dXRzLCBzbyBhbnl0aGluZyBxdWV1ZWQKICAgICAgICAjanVzdCBhZnRlciB0aGUgdGhyZWFkIHdl"
    "bnQgYmFjayBpbnRvIHJlY3YoKSB3YWl0ZWQgb3V0IHRoZSBmdWxsCiAgICAgICAgI3RpbWVvdXQg"
    "LSB1cCB0byAxMDBtcyBvZiBsYXRlbmN5IGFkZGVkIHRvIGV2ZXJ5IHJlbGF5ZWQgZ2FtZSBjb21t"
    "YW5kLAogICAgICAgICNvbiB0b3Agb2YgZXZlcnkgaWRsZSBjb25uZWN0aW9uIHdha2luZyAxMCB0"
    "aW1lcyBhIHNlY29uZCB0byBjaGVjay4KICAgICAgICB0cnk6CiAgICAgICAgICAgIHdoaWxlIFRy"
    "dWU6CiAgICAgICAgICAgICAgICBtc2cgPSBzZWxmLl9zUXVldWUuZ2V0KCkKICAgICAgICAgICAg"
    "ICAgIGlmIG1zZyBpcyBzZWxmLl9TVE9QV1JJVEVSOgogICAgICAgICAgICAgICAgICAgIGJyZWFr"
    "CiAgICAgICAgICAgICAgICAjQ29hbGVzY2Ugd2hhdGV2ZXIgZWxzZSBwaWxlZCB1cCBiZWhpbmQg"
    "aXQgaW50byBhIHNpbmdsZSB3cml0ZS4KICAgICAgICAgICAgICAgICNQb3NpdGlvbiBicm9hZGNh"
    "c3RzIGFuZCBnYW1lIGNvbW1hbmRzIG9mdGVuIGFycml2ZSBpbiBidXJzdHMuCiAgICAgICAgICAg"
    "ICAgICBjaHVua3MgPSBbbXNnXQogICAgICAgICAgICAgICAgc3RvcHBpbmcgPSBGYWxzZQogICAg"
    "ICAgICAgICAgICAgd2hpbGUgVHJ1ZToKICAgICAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAg"
    "ICAgICAgICAgICAgICAgIG54dCA9IHNlbGYuX3NRdWV1ZS5nZXRfbm93YWl0KCkKICAgICAgICAg"
    "ICAgICAgICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICAgICAgICAgICAgICBicmVh"
    "awogICAgICAgICAgICAgICAgICAgIGlmIG54dCBpcyBzZWxmLl9TVE9QV1JJVEVSOgogICAgICAg"
    "ICAgICAgICAgICAgICAgICBzdG9wcGluZyA9IFRydWUKICAgICAgICAgICAgICAgICAgICAgICAg"
    "YnJlYWsKICAgICAgICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKG54dCkKICAgICAgICAgICAg"
    "ICAgIHBheWxvYWQgPSBiJycuam9pbihjaHVua3MpCiAgICAgICAgICAgICAgICAjUmVsZWFzZWQg"
    "YmVmb3JlIHRoZSB3cml0ZSwgbm90IGFmdGVyOiB0aGUgYmFja2xvZyBleGlzdHMgdG8KICAgICAg"
    "ICAgICAgICAgICNkZXNjcmliZSB3aGF0IGlzIHN0aWxsIHdhaXRpbmcgZm9yIHRoZSBzb2NrZXQs"
    "IGFuZCB0aGVzZSBieXRlcwogICAgICAgICAgICAgICAgI2FyZSBvbiB0aGVpciB3YXkgb3V0LiBD"
    "b3VudGluZyB0aGVtIGFzIHBlbmRpbmcgZm9yIHRoZSB3aG9sZQogICAgICAgICAgICAgICAgI2R1"
    "cmF0aW9uIG9mIGEgc2xvdyBzZW5kYWxsKCkgd291bGQgbWFrZSBhIG1lcmVseSBzbG93IGxpbmsg"
    "bG9vawogICAgICAgICAgICAgICAgI2xpa2UgdGhlIHdlZGdlZCBjbGllbnQgdGhlIGNhcCBpcyB0"
    "aGVyZSB0byBjYXRjaC4KICAgICAgICAgICAgICAgIHdpdGggc2VsZi5fcUxvY2s6CiAgICAgICAg"
    "ICAgICAgICAgICAgc2VsZi5fcUJ5dGVzIC09IGxlbihwYXlsb2FkKQogICAgICAgICAgICAgICAg"
    "c2VsZi5zZW5kUmF3KHBheWxvYWQpCiAgICAgICAgICAgICAgICBpZiBzdG9wcGluZzoKICAgICAg"
    "ICAgICAgICAgICAgICByZXR1cm4KICAgICAgICBleGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9y"
    "LCBDb25uZWN0aW9uQWJvcnRlZEVycm9yLCBCcm9rZW5QaXBlRXJyb3IsIE9TRXJyb3IpOgogICAg"
    "ICAgICAgICBwYXNzICNwZWVyIGlzIGdvbmU7IHRoZSByZWFkIGxvb3Agbm90aWNlcyBhbmQgcnVu"
    "cyB0aGUgY2xlYW51cAogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHByaW50"
    "KCdbTG9iYnldIFdyaXRlciBlcnJvcjpcbicgKyB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAg"
    "ICAgIGZpbmFsbHk6CiAgICAgICAgICAgIHNlbGYuX3dyaXRlckRlYWQuc2V0KCkKICAgIGRlZiBf"
    "c3RhcnRXcml0ZXIoc2VsZik6CiAgICAgICAgc2VsZi5fd3JpdGVyID0gdGhyZWFkaW5nLlRocmVh"
    "ZCh0YXJnZXQ9c2VsZi5fd3JpdGVyTG9vcCwgZGFlbW9uPVRydWUpCiAgICAgICAgc2VsZi5fd3Jp"
    "dGVyLnN0YXJ0KCkKICAgIGRlZiBfc3RvcFdyaXRlcihzZWxmKToKICAgICAgICBpZiBzZWxmLl93"
    "cml0ZXIgaXMgTm9uZToKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgc2VsZi5fc1F1ZXVlLnB1"
    "dChzZWxmLl9TVE9QV1JJVEVSKQogICAgICAgIHNlbGYuX3dyaXRlci5qb2luKHRpbWVvdXQ9Mi4w"
    "KQogICAgICAgIHNlbGYuX3dyaXRlciA9IE5vbmUKICAgIGRlZiBfY2xhaW1TZXNzaW9uKHNlbGYp"
    "OgogICAgICAgICNUYWtlIG93bmVyc2hpcCBvZiB0aGUgdXNlcm5hbWUgc2xvdCBiZWZvcmUgdGVs"
    "bGluZyB0aGUgY2xpZW50IGl0IGlzCiAgICAgICAgI2xvZ2dlZCBpbi4gUmV0dXJucyBGYWxzZSBp"
    "ZiBhbm90aGVyIGNvbm5lY3Rpb24gZ290IHRoZXJlIGZpcnN0LgogICAgICAgIGlmIHNlbGYuc2Vy"
    "dmVyLnN0YXRlLmNsYWltVXNlcihzZWxmLnVzZXIubmFtZSwgc2VsZik6CiAgICAgICAgICAgIHJl"
    "dHVybiBUcnVlCiAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5zZXJ2ZXIpICNyZWxl"
    "YXNlcyB0aGUgaWRudW0gd2UganVzdCBhbGxvY2F0ZWQKICAgICAgICBzZWxmLnVzZXIgPSBOb25l"
    "CiAgICAgICAgcmV0dXJuIEZhbHNlCiAgICBkZWYgYXR0ZW1wdExvZ2luKHNlbGYsIHVzZXJuYW1l"
    "LCBwYXNzd29yZCk6CiAgICAgICAgaWYgbGVuKHVzZXJuYW1lKTwxOgogICAgICAgICAgICByZXR1"
    "cm4gNCAjTm8gVXNlcm5hbWUsIGxpa2VseSBmcmVzaCBsb2dpbgogICAgICAgICAgICAjVE9ETyBj"
    "aGVjayBpZiBzZXJpYWwgZXhpc3RzIGFuZCByZXR1cm4gdXNlcm5hbWUgcHJvcGVybHkKICAgICAg"
    "ICBpZiBsZW4ocGFzc3dvcmQpPDE6CiAgICAgICAgICAgIHJldHVybiAzICNQYXNzd29yZCB0b28g"
    "c2hvcnQKICAgICAgICAjVGVzdCBpZiBwbGF5ZXIgYWxyZWFkeSBsb2dnZWQgaW4gKGZhc3QgcGF0"
    "aDsgdGhlIGF1dGhvcml0YXRpdmUsCiAgICAgICAgI3JhY2UtZnJlZSBjaGVjayBpcyB0aGUgY2xh"
    "aW1Vc2VyKCkgYmVsb3cpCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuZ2V0UGxheWVyKHVzZXJuYW1l"
    "KToKICAgICAgICAgICAgcmV0dXJuIDIgI1RPRE8gUExBWUVSIExPR0dFRCBJTiBFUlJPUgogICAg"
    "ICAgICNwbGF5ZXIgbm90IGN1cnJlbnRseSBsb2dnZWQgaW4sIGF0dGVtcHQgdG8gbG9naW4gdmlh"
    "IGRhdGEgaGFuZGxlcgogICAgICAgIHNlbGYudXNlciA9IEdESC5sb2dpblBsYXllcih1c2VybmFt"
    "ZSwgc2VsZiwgcGFzc3dvcmQpCiAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICByZXR1"
    "cm4gMCBpZiBzZWxmLl9jbGFpbVNlc3Npb24oKSBlbHNlIDIKICAgICAgICByZXR1cm4gMSAjVE9E"
    "TyBHZXQgZnJvbSBHREgubG9naW5QbGF5ZXIsIHBhc3MgdXNlciBvYmplY3QgYWxvbmc/CiAgICBk"
    "ZWYgYXR0ZW1wdFJlZ2lzdGVyKHNlbGYsIHVzZXJuYW1lLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0"
    "aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgogICAgICAgICNUZXN0IGlmIHBsYXllciBh"
    "bHJlYWR5IGxvZ2dlZCBpbgogICAgICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1c2VybmFt"
    "ZSk6CiAgICAgICAgICAgIHJldHVybiAxICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJST1IKICAg"
    "ICAgICBzZWxmLnVzZXIgPSBHREgucmVnaXN0ZXJQbGF5ZXIodXNlcm5hbWUsIHNlbGYsIHBhc3N3"
    "b3JkLCBlbWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbikKICAgICAgICBp"
    "ZiBzZWxmLnVzZXI6CiAgICAgICAgICAgIHJldHVybiAwIGlmIHNlbGYuX2NsYWltU2Vzc2lvbigp"
    "IGVsc2UgMQogICAgICAgIHJldHVybiAyICNUT0RPIGdldCBlcnJvciBmcm9tIEdESAogICAgZGVm"
    "IGhhbmRsZShzZWxmKToKICAgICAgICB0cnk6ICNJbnRlcmNlcHQgYW5kIHByaW50IGVycm9ycyBm"
    "b3IgZGVidWdnaW5nCiAgICAgICAgICAgIHNlbGYuX2hhbmRsZSgpCiAgICAgICAgICAgICNUT0RP"
    "IGxvb3AgbG9iYnkgaGFuZGxlIGJldHRlciB0byBoYW5kbGUgZXhjZXB0aW9ucyBncmFjZWZ1bGx5"
    "CiAgICAgICAgICAgIHNlbGYuX2xvYmJ5SGFuZGxlKCkKICAgICAgICBleGNlcHQgUHJvdG9jb2xF"
    "cnJvciBhcyBlOgogICAgICAgICAgICAjbWFsZm9ybWVkL292ZXJzaXplZCBpbnB1dCAtIHRoZSBj"
    "bGllbnQncyBmYXVsdCwgbm90IG91cnMuIERyb3AgdGhlCiAgICAgICAgICAgICNjb25uZWN0aW9u"
    "IHdpdGggb25lIGxpbmUgaW5zdGVhZCBvZiBhIHRyYWNlYmFjay4KICAgICAgICAgICAgd2hvID0g"
    "c2VsZi51c2VyLm5hbWUgaWYgc2VsZi51c2VyIGVsc2Ugc2VsZi5jbGllbnRfYWRkcmVzc1swXQog"
    "ICAgICAgICAgICBwcmludChmJ1tMb2JieV0gUHJvdG9jb2wgZXJyb3IgZnJvbSB7d2hvfToge2V9"
    "JykKICAgICAgICBleGNlcHQgKHpsaWIuZXJyb3IsIHN0cnVjdC5lcnJvciwgVW5pY29kZURlY29k"
    "ZUVycm9yKSBhcyBlOgogICAgICAgICAgICAjdHJ1bmNhdGVkL2dhcmJhZ2UgcGFja2V0OiBwYXJz"
    "ZURzdHIgYW5kIHN0cnVjdC51bnBhY2sgYm90aCByYWlzZSBvbgogICAgICAgICAgICAjc2hvcnQg"
    "cmVhZHMsIGFuZCAuZGVjb2RlKCkgb24gbm9uLWFzY2lpIGp1bmsuIFNhbWUgY2F0ZWdvcnkuCiAg"
    "ICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBNYWxmb3JtZWQgcGFja2V0IGZyb20ge3NlbGYuY2xp"
    "ZW50X2FkZHJlc3NbMF19OiAnCiAgICAgICAgICAgICAgICAgIGYne3R5cGUoZSkuX19uYW1lX199"
    "OiB7ZX0nKQogICAgICAgIGV4Y2VwdCAoQ29ubmVjdGlvblJlc2V0RXJyb3IsIENvbm5lY3Rpb25B"
    "Ym9ydGVkRXJyb3IsIE9TRXJyb3IpIGFzIGU6CiAgICAgICAgICAgICMgZXhwZWN0ZWQgZm9ybSBv"
    "ZiBkaXNjb25uZWN0aW9uIChpbmNsdWRpbmcgYSBmb3JjZWQgYWRtaW4ga2ljayksCiAgICAgICAg"
    "ICAgICMgYnV0IGxlYXZlIGEgb25lLWxpbmUgYnJlYWRjcnVtYiByYXRoZXIgdGhhbiBzdGF5aW5n"
    "IGZ1bGx5IHNpbGVudAogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgICAgICBw"
    "cmludChmJ1tMb2JieV0gQ29ubmVjdGlvbiBjbG9zZWQgZm9yIHtzZWxmLnVzZXIubmFtZX06IHtl"
    "fScpCiAgICAgICAgZXhjZXB0IEV4Y2VwdGlvbjojIGFzIGU6CiAgICAgICAgICAgIHByaW50KHRy"
    "YWNlYmFjay5mb3JtYXRfZXhjKCkpCiAgICAgICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAg"
    "ICAgICAgIHByaW50KGYnVXNlcjoge3NlbGYudXNlci5uYW1lfScpCiAgICAgICAgICAgICNyYWlz"
    "ZSBlCiAgICBkZWYgX2xvYmJ5SGFuZGxlKHNlbGYpOgogICAgICAgICNhY3RpdmVVc2Vyc1suLi5d"
    "ID0gc2VsZiB1c2VkIHRvIGhhcHBlbiBoZXJlOyBpdCBub3cgaGFwcGVucyB1bmRlciBhCiAgICAg"
    "ICAgI2xvY2sgaW5zaWRlIGF0dGVtcHRMb2dpbi9hdHRlbXB0UmVnaXN0ZXIsIGJlZm9yZSB0aGUg"
    "d2VsY29tZSBwYWNrZXQKICAgICAgICAjZ29lcyBvdXQsIHNvIHR3byBsb2dpbnMgZm9yIG9uZSBh"
    "Y2NvdW50IGNhbid0IGJvdGggc3VjY2VlZC4KICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVz"
    "ZXIubmFtZX0gQ29ubmVjdGVkJykKICAgICAgICAjRnJvbSBoZXJlIG9uIG5vdGhpbmcgd3JpdGVz"
    "IHRvIHRoZSBzb2NrZXQgaW5saW5lOiB0aGUgd3JpdGVyIHRocmVhZAogICAgICAgICNvd25zIHRo"
    "ZSBvdXRib3VuZCBkaXJlY3Rpb24gYW5kIHRoaXMgbG9vcCBvbmx5IHJlYWRzLgogICAgICAgIHNl"
    "bGYuX3N0YXJ0V3JpdGVyKCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3Rvbmlj"
    "KCkKICAgICAgICAjVGhlIHNvY2tldCBzdGF5cyBpbiBibG9ja2luZyBtb2RlIGZvciBpdHMgd2hv"
    "bGUgbGlmZSBmcm9tIGhlcmUgb24sIGFuZAogICAgICAgICNyZWFkaW5lc3MgaXMgd2FpdGVkIGZv"
    "ciB3aXRoIHNlbGVjdCgpIGluc3RlYWQgb2YgYSBzb2NrZXQgdGltZW91dC4KICAgICAgICAjVGhp"
    "cyBpcyBub3QgYSBzdHlsZSBwcmVmZXJlbmNlIC0gYSBzb2NrZXQgdGltZW91dCBpcyBhIHByb3Bl"
    "cnR5IG9mIHRoZQogICAgICAgICMqc29ja2V0Kiwgbm90IG9mIHRoZSBjYWxsLCBzbyB0aGUgc2V0"
    "dGltZW91dChfUkVBRF9USU1FT1VUKSB0aGlzIGxvb3AKICAgICAgICAjdXNlZCB0byBkbyBvbiBl"
    "dmVyeSBwYXNzIGFsc28gYXJtZWQgYSAxcyB0aW1lb3V0IG9uIHRoZSB3cml0ZXIKICAgICAgICAj"
    "dGhyZWFkJ3MgY29uY3VycmVudCBzZW5kYWxsKCkuIEEgY2xpZW50IHdob3NlIHJlY2VpdmUgd2lu"
    "ZG93IHdhcyBmdWxsCiAgICAgICAgI2ZvciBhIHNlY29uZCAoZXhhY3RseSB0aGUgY2FzZSBkdXJp"
    "bmcgYSBidXN5IGNvLW9wIHNlc3Npb24pIG1hZGUgdGhhdAogICAgICAgICNzZW5kYWxsKCkgcmFp"
    "c2UgVGltZW91dEVycm9yICphZnRlciBoYXZpbmcgYWxyZWFkeSB3cml0dGVuIHBhcnQgb2YgdGhl"
    "CiAgICAgICAgI3BhY2tldCo6IHRoZSB3cml0ZXIgdGhyZWFkIGRpZWQsIGFuZCB3aGF0ZXZlciB0"
    "aGUgY2xpZW50IGhhZCByZWNlaXZlZAogICAgICAgICN3YXMgaGFsZiBhIG1lc3NhZ2UsIHNvIGl0"
    "cyBjb21tYW5kIHN0cmVhbSB3YXMgZGVzeW5jaHJvbmlzZWQgZnJvbQogICAgICAgICN0aGF0IHBv"
    "aW50IG9uLiBzZWxlY3QoKSBsZWF2ZXMgdGhlIHNvY2tldCBibG9ja2luZywgc28gd3JpdGVzIGFy"
    "ZQogICAgICAgICNuZXZlciBpbnRlcnJ1cHRlZCwgd2hpbGUgcmVhZHMgc3RpbGwgd2FrZSB1cCBy"
    "ZWd1bGFybHkgZW5vdWdoIHRvCiAgICAgICAgI25vdGljZSBzaHV0ZG93biBhbmQgdGhlIGlkbGUg"
    "ZGVhZGxpbmUuCiAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHRpbWVvdXQoTm9uZSkKICAgICAgICB3"
    "aGlsZSBUcnVlOgogICAgICAgICAgICBpZiBzZWxmLl9kcm9wcGVkLmlzX3NldCgpOgogICAgICAg"
    "ICAgICAgICAgYnJlYWsgI2tpY2tlZCwgb3IgZHJvcHBlZCBmb3IgYW4gdW5yZWFkIHNlbmQgYmFj"
    "a2xvZwogICAgICAgICAgICBpZiBzZWxmLl93cml0ZXJEZWFkLmlzX3NldCgpOgogICAgICAgICAg"
    "ICAgICAgYnJlYWsgI3BlZXIgd2VudCBhd2F5IHdoaWxlIHdlIHdlcmUgc2VuZGluZwogICAgICAg"
    "ICAgICBpZiBzZWxmLnNlcnZlci5faXNfY2xvc2luZzoKICAgICAgICAgICAgICAgIGJyZWFrICNz"
    "ZXJ2ZXIgaXMgc3RvcHBpbmcgLSBjaGVja2VkIGhlcmUsIG5vdCBvbmx5IG9uIGFuIGlkbGUKICAg"
    "ICAgICAgICAgICAgICAgICAgICN0aW1lb3V0LCBzbyBhIGNsaWVudCB0aGF0IGtlZXBzIHRhbGtp"
    "bmcgY2Fubm90IGtlZXAgaXRzCiAgICAgICAgICAgICAgICAgICAgICAjaGFuZGxlciB0aHJlYWQg"
    "KGFuZCBpdHMgbG9nIHNwYW0pIGFsaXZlIHBhc3Qgc2h1dGRvd24KICAgICAgICAgICAgdHJ5Ogog"
    "ICAgICAgICAgICAgICAgcmVhZHksIF8sIF8gPSBzZWxlY3Quc2VsZWN0KFtzZWxmLnJlcXVlc3Rd"
    "LCBbXSwgW10sIF9SRUFEX1RJTUVPVVQpCiAgICAgICAgICAgIGV4Y2VwdCAoT1NFcnJvciwgVmFs"
    "dWVFcnJvcik6CiAgICAgICAgICAgICAgICBicmVhayAjc29ja2V0IGNsb3NlZCB1bmRlciB1cyAo"
    "YWRtaW4ga2ljayAvIHNodXRkb3duKQogICAgICAgICAgICBpZiBub3QgcmVhZHk6CiAgICAgICAg"
    "ICAgICAgICBpZiBzZWxmLnNlcnZlci5faXNfY2xvc2luZzoKICAgICAgICAgICAgICAgICAgICBi"
    "cmVhayAjU2VydmVyIFNodXR0aW5nIGRvd24KICAgICAgICAgICAgICAgIGlmIF9JRExFX1RJTUVP"
    "VVQgYW5kICh0aW1lLm1vbm90b25pYygpIC0gc2VsZi5fbGFzdFJlY3YpID4gX0lETEVfVElNRU9V"
    "VDoKICAgICAgICAgICAgICAgICAgICAjSGFsZi1vcGVuIGNvbm5lY3Rpb246IHRoZSBwZWVyIGlz"
    "IHVucmVhY2hhYmxlIGJ1dCBuZXZlcgogICAgICAgICAgICAgICAgICAgICNzZW50IGEgRklOL1JT"
    "VCwgc28gcmVjdigpIGJsb2NrcyBmb3JldmVyIGFuZCB0aGUgYWNjb3VudAogICAgICAgICAgICAg"
    "ICAgICAgICNzdGF5cyBjbGFpbWVkLiBSZWFwIGl0IHNvIHRoZSBwbGF5ZXIgY2FuIGxvZyBiYWNr"
    "IGluLgogICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSB7c2VsZi51c2VyLm5hbWV9"
    "IGlkbGUgZm9yIHtfSURMRV9USU1FT1VUfXMsIGRyb3BwaW5nJykKICAgICAgICAgICAgICAgICAg"
    "ICBicmVhawogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgcm1zZyA9IHNlbGYu"
    "cmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikgI1RPRE8gbG9nIG5ldHdvcmsgYnl0ZXJhdGUKICAg"
    "ICAgICAgICAgaWYgbm90IHJtc2c6CiAgICAgICAgICAgICAgICBicmVhayAjRGlzY29ubmVjdGVk"
    "CiAgICAgICAgICAgIHNlbGYuZGF0YSs9cm1zZwogICAgICAgICAgICBzZWxmLl9sYXN0UmVjdiA9"
    "IHRpbWUubW9ub3RvbmljKCkKICAgICAgICAgICAgd2hpbGUgc2VsZi5kYXRhOgogICAgICAgICAg"
    "ICAgICAgdHJ5OgogICAgICAgICAgICAgICAgICAgIGNtZF9sID0gc2VsZi5kYXRhLmluZGV4KDAp"
    "CiAgICAgICAgICAgICAgICBleGNlcHQgVmFsdWVFcnJvcjoKICAgICAgICAgICAgICAgICAgICAj"
    "cHJpbnQoJ2NtZCBkZWNvZGUgZXJyb3I6XG4nLCB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAg"
    "ICAgICAgICAgICAgICAgIGJyZWFrOyNNYXkgcmVxdWlyZSBtb3JlIGRhdGEKICAgICAgICAgICAg"
    "ICAgIGNtZCA9IHdpcmVfZGVjb2RlKHNlbGYuZGF0YVswOmNtZF9sXSkKICAgICAgICAgICAgICAg"
    "IHNlbGYuZGF0YSA9IHNlbGYuZGF0YVtjbWRfbCsxOl0KICAgICAgICAgICAgICAgIHJlc3BvbnNl"
    "ID0gc2VsZi5zZXJ2ZXIuY29tcGFycy5wYXJzZShjbWQsIHNlbGYpCiAgICAgICAgICAgICAgICBp"
    "ZiByZXNwb25zZToKICAgICAgICAgICAgICAgICAgICAjUXVldWVkIHJhdGhlciB0aGFuIHNlbnQg"
    "aW5saW5lLCBzbyB0aGlzIGNvbm5lY3Rpb24gaGFzIGEKICAgICAgICAgICAgICAgICAgICAjc2lu"
    "Z2xlIG9yZGVyZWQgb3V0Ym91bmQgc3RyZWFtLiBTZW5kaW5nIGhlcmUgZGlyZWN0bHkKICAgICAg"
    "ICAgICAgICAgICAgICAjd291bGQgcmFjZSB0aGUgd3JpdGVyIHRocmVhZCBhbmQgY291bGQgbGFu"
    "ZCBpbiB0aGUgbWlkZGxlCiAgICAgICAgICAgICAgICAgICAgI29mIGEgYnJvYWRjYXN0IGl0IGlz"
    "IGFscmVhZHkgd3JpdGluZy4KICAgICAgICAgICAgICAgICAgICBzZWxmLnNlbmQocmVzcG9uc2Up"
    "CiAgICAgICAgICAgICAgICAjTG9vc2UgYmxvYnMgc2hvdWxkIG5vdCBoYXBwZW4gYW55bW9yZSBo"
    "b3BlZnVsbHkKICAgICAgICAgICAgICAgICNUT0RPIGZpeCB1bmNvbXByZXNzZWQgZGF0YSBibG9i"
    "cz8KICAgICAgICAgICAgICAgICNUT0RPIHNraXAgMSBieXRlIG9ubHkgd2hlbiBkZWNvZGUgZXJy"
    "b3I/CiAgICAgICAgICAgICAgICBpZiAobGVuKHNlbGYuZGF0YSk+MiBhbmQKICAgICAgICAgICAg"
    "ICAgICAgICAgICAgc2VsZi5kYXRhWzBdPT0weDc4IGFuZAogICAgICAgICAgICAgICAgICAgICAg"
    "ICBzZWxmLmRhdGFbMV09PTB4OWMpOgogICAgICAgICAgICAgICAgICAgICNMb29zZSB1bmhhbmRs"
    "ZWQgYmxvYiBhZnRlciBjb21tYW5kCiAgICAgICAgICAgICAgICAgICAgYmxvYiwgc2VsZi5kYXRh"
    "ID0gcF9nZXRCbG9iKHNlbGYuZGF0YSwgc2VsZi5yZXF1ZXN0KQogICAgICAgICAgICAgICAgICAg"
    "ICNUaGUgb3RoZXIgYmxpbmQgc3BvdDogYW55dGhpbmcgdGhlIGNsaWVudCBzZW5kcyBhcyBhCiAg"
    "ICAgICAgICAgICAgICAgICAgI2NvbXByZXNzZWQgYmxvYiByYXRoZXIgdGhhbiBhIHRleHQgY29t"
    "bWFuZCB3YXMgcmVhZCBhbmQKICAgICAgICAgICAgICAgICAgICAjdGhyb3duIGF3YXkgd2l0aG91"
    "dCBhIHRyYWNlLgogICAgICAgICAgICAgICAgICAgIGlmIF9ERUJVR19MT0dfQ09NTUFORFM6CiAg"
    "ICAgICAgICAgICAgICAgICAgICAgIHdobyA9IHNlbGYudXNlci5uYW1lIGlmIHNlbGYudXNlciBl"
    "bHNlICc/JwogICAgICAgICAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IC0+IChV"
    "TkhBTkRMRUQgQkxPQiBhZnRlciB7Y21kIXJ9KSAnCiAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgIGYne2xlbihibG9iKX0gYnl0ZXMnKQogICAgZGVmIF9yZWN2TW9yZShzZWxmKToKICAgICAg"
    "ICBjaHVuayA9IHNlbGYucmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBpZiBub3Qg"
    "Y2h1bms6CiAgICAgICAgICAgICNwZWVyIGRpc2Nvbm5lY3RlZCBkdXJpbmcgaGFuZHNoYWtlL2xv"
    "Z2luLCBzdG9wIHRoZSBidXN5LWxvb3AKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0"
    "RXJyb3IoJ2Rpc2Nvbm5lY3RlZCBkdXJpbmcgbG9naW4nKQogICAgICAgIHNlbGYuZGF0YSArPSBj"
    "aHVuawogICAgZGVmIF9oYW5kbGUoc2VsZik6CiAgICAgICAgI1RPRE8gbG9nIGxvZ2luIGF0dGVt"
    "cHRzPwogICAgICAgIHBlZXJfaXAgPSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAgICAgcHJp"
    "bnQoJ0Nvbm5lY3Rpb24gYXR0ZW1wdCBmcm9tOicsIHBlZXJfaXApCiAgICAgICAgTElTID0gMiAj"
    "bG9naW4gc3RhdGUgI1RPRE8gY29uc2lkZXIgbG9uZyB0aW1lb3V0cz8KICAgICAgICB3aGlsZSBM"
    "SVM6CiAgICAgICAgICAgIHdoaWxlIGxlbihzZWxmLmRhdGEpPDQ6CiAgICAgICAgICAgICAgICBz"
    "ZWxmLl9yZWN2TW9yZSgpCiAgICAgICAgICAgIHBhY2tfbGVuID0gc3RydWN0LnVucGFjaygnPEkn"
    "LHNlbGYuZGF0YVswOjRdKVswXQogICAgICAgICAgICBpZiBwYWNrX2xlbiA8IDQgb3IgcGFja19s"
    "ZW4gPiBfTUFYX0hBTkRTSEFLRToKICAgICAgICAgICAgICAgICN1bnZhbGlkYXRlZCwgdGhpcyBp"
    "cyBhIHByZS1hdXRoZW50aWNhdGlvbiBtZW1vcnkgYm9tYjogYW4KICAgICAgICAgICAgICAgICN1"
    "bmF1dGhlbnRpY2F0ZWQgcGVlciBhbm5vdW5jZXMgYSA0R0IgcGFja2V0IGFuZCB0aGUgbG9vcCBi"
    "ZWxvdwogICAgICAgICAgICAgICAgI2J1ZmZlcnMgdW50aWwgdGhlIHByb2Nlc3MgZGllcwogICAg"
    "ICAgICAgICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2hhbmRzaGFrZSBwYWNrZXQgbGVuZ3Ro"
    "IHtwYWNrX2xlbn0gb3V0IG9mIHJhbmdlJykKICAgICAgICAgICAgd2hpbGUobGVuKHNlbGYuZGF0"
    "YSk8cGFja19sZW4pOgogICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1vcmUoKQogICAgICAgICAg"
    "ICAjc2xpY2UgdG8gcGFja19sZW4gKG5vdCB0byB0aGUgZW5kIG9mIHRoZSBidWZmZXIpOiBhbnl0"
    "aGluZyBwYXN0CiAgICAgICAgICAgICN0aGlzIHBhY2tldCBiZWxvbmdzIHRvIHRoZSBuZXh0IG9u"
    "ZS4gQm91bmRlZCBkZWNvbXByZXNzLCBiZWNhdXNlIGEKICAgICAgICAgICAgIzY0ayBoYW5kc2hh"
    "a2Ugb2YgY29tcHJlc3NlZCB6ZXJvZXMgZXhwYW5kcyB0byBodW5kcmVkcyBvZiBNQi4KICAgICAg"
    "ICAgICAgcmVzID0gX2RlY29tcHJlc3NfYm91bmRlZChzZWxmLmRhdGFbNDpwYWNrX2xlbl0sIF9N"
    "QVhfSEFORFNIQUtFX0lORkxBVEVEKQogICAgICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFb"
    "cGFja19sZW46XQogICAgICAgICAgICBpZiBMSVMgPT0gMjoKICAgICAgICAgICAgICAgIGdhbWV2"
    "ZXJzaW9uID0gcmVzWzA6MTZdICNUT0RPIG5vdGUgZ2FtZSB2ZXJzaW9uICh1bnZlcmlmaWVkKSBw"
    "ZXIgdXNlcgogICAgICAgICAgICAgICAgbGFuZ25hbWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDE2"
    "KQogICAgICAgICAgICAgICAgI1RPRE8gY29uc2lkZXIgVFdTRSBpbmRpY2F0b3IgdG8gY3JlYXRl"
    "IHNlY3VyZSBjb25uZWN0aW9uPwogICAgICAgICAgICAgICAgI1RPRE8gY2hlY2sgaWYgdmFuaWxs"
    "YSBzZXJ2ZXIgaWdub3JlcyBleHRyYSBkYXRhIGluIGhhbmRzaGFrZSBwcm9jZXNzCiAgICAgICAg"
    "ICAgICAgICBSSyA9IHJlc1tvZmYrODpvZmYrMTZdCiAgICAgICAgICAgICAgICBmb3IgaSBpbiBy"
    "YW5nZShsZW4oUkspKToKICAgICAgICAgICAgICAgICAgICBzZWxmLlNLW2ldXj1SS1tpXQogICAg"
    "ICAgICAgICAgICAgI3dhcyBoYXJkY29kZWQgJ1RXMUNTJyB3aXRoIGEgIlNFUlZFUiBOQU1FIGNm"
    "Z1RPRE8iIG5vdGU6IHRoZQogICAgICAgICAgICAgICAgI25hbWUgY29uZmlndXJlZCBpbiBDb25m"
    "aWcuaW5pL3RoZSBHVUkgcmVhY2hlZCB0aGUgd2VsY29tZQogICAgICAgICAgICAgICAgI3BhY2tl"
    "dCBidXQgbmV2ZXIgdGhpcyBvbmUsIHNvIHRoZSBwcmUtbG9naW4gaGFuZHNoYWtlIGFsd2F5cwog"
    "ICAgICAgICAgICAgICAgI2Fubm91bmNlZCB0aGUgcGxhY2Vob2xkZXIuCiAgICAgICAgICAgICAg"
    "ICBzZWxmLnNlbmRSYXcoX3NlcnZlcl9pbmZvX3BhY2tldChzYW5pdGl6ZVRleHQoREVGQVVMVF9U"
    "SVRMRSkpKQogICAgICAgICAgICAgICAgI1RPRE8gVFcxQ1MgaW5kaWNhdG9yIGZvciBUV1NFIGNs"
    "aWVudCB0byBjcmVhdGUgc2VjdXJlIGNvbm5lY3Rpb24gb3IgcHJlLWhhc2ggcGFzc3dvcmQ/CiAg"
    "ICAgICAgICAgICAgICBMSVMgPSAxIAogICAgICAgICAgICAgICAgc2VsZi5TSyA9IGJ5dGVzKHNl"
    "bGYuU0spCiAgICAgICAgICAgIGVsaWYgTElTID09IDE6CiAgICAgICAgICAgICAgICBsb2dpbkVy"
    "cm9yID0gLTEKICAgICAgICAgICAgICAgICNTdGFsbCByZXBlYXQgb2ZmZW5kZXJzIGJlZm9yZSBk"
    "b2luZyBhbnkgUEJLREYyIHdvcmsgZm9yIHRoZW0uCiAgICAgICAgICAgICAgICAjU2xlZXBpbmcg"
    "aW4gdGhpcyBoYW5kbGVyIHRocmVhZCBpcyB0aGUgcG9pbnQ6IGl0IGNvc3RzIHVzCiAgICAgICAg"
    "ICAgICAgICAjbm90aGluZyBhbmQgcmF0ZS1saW1pdHMgdGhhdCBjb25uZWN0aW9uLgogICAgICAg"
    "ICAgICAgICAgZGVsYXkgPSBMT0dJTl9USFJPVFRMRS5kZWxheUZvcihwZWVyX2lwKQogICAgICAg"
    "ICAgICAgICAgaWYgZGVsYXk6CiAgICAgICAgICAgICAgICAgICAgdGltZS5zbGVlcChkZWxheSkK"
    "ICAgICAgICAgICAgICAgIHVzZXJuYW1lLCBvZmYgPSBwYXJzZURzdHIocmVzLCAwKQogICAgICAg"
    "ICAgICAgICAgcGFzc3dvcmQsIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAg"
    "ICAgICNUT0RPIFRXU0UgbW9kIGZvciBoaWdoZXIgbG9naW4gc2VjdXJpdHkKICAgICAgICAgICAg"
    "ICAgICMtZW5jcnlwdGVkIGNvbm5lY3Rpb24gdG8gcHJldmVudCByZXBsYXkgYXR0YWNrcwogICAg"
    "ICAgICAgICAgICAgIy1wcmVoYXNoIHBhc3N3b3JkIHdpdGggc2VyaWFsPywgY2hlY2sgaWYgcmVj"
    "b3ZlcnkgcG9zc2libGUuCiAgICAgICAgICAgICAgICBzZWxmLmd1aWQgPSBieXRlcyhyZXNbb2Zm"
    "Om9mZisxNl0pCiAgICAgICAgICAgICAgICAjcHJpbnQoJ2d1aWQgYnl0ZTonLCBzZWxmLmd1aWRb"
    "MV0pCiAgICAgICAgICAgICAgICAjc2VsZi5ndWlkID0gYnl0ZWFycmF5KHJlc1tvZmY6b2ZmKzE2"
    "XSkKICAgICAgICAgICAgICAgICNzZWxmLmd1aWRbMV1ePTB4MTYgI0RPIE5PVCBwZXJmb3JtIHNl"
    "cnZlcnNpZGUKICAgICAgICAgICAgICAgICNzZWxmLmd1aWQgPSBieXRlcyhzZWxmLmd1aWQpCiAg"
    "ICAgICAgICAgICAgICBvZmYrPTE2CiAgICAgICAgICAgICAgICBpc3JlZyA9IHN0cnVjdC51bnBh"
    "Y2soJzxJJyxyZXNbb2ZmOm9mZis0XSlbMF0KICAgICAgICAgICAgICAgIG9mZis9NAogICAgICAg"
    "ICAgICAgICAgdmlhUmVnaXN0ZXIgPSBib29sKGlzcmVnKQogICAgICAgICAgICAgICAgaWYgaXNy"
    "ZWc6CiAgICAgICAgICAgICAgICAgICAgZW1haWwsIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikK"
    "ICAgICAgICAgICAgICAgICAgICBsb2NhdGlvbiwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQog"
    "ICAgICAgICAgICAgICAgICAgIGFnZSA9IHJlc1tvZmZdCiAgICAgICAgICAgICAgICAgICAgZ2Vu"
    "ZGVyID0gcmVzW29mZisxXQogICAgICAgICAgICAgICAgICAgIG9mZis9MiAjYWdlLCBnZW5kZXIK"
    "ICAgICAgICAgICAgICAgICAgICBkZXNjcmlwdGlvbiwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2Zm"
    "KQogICAgICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSBzZWxmLmF0dGVtcHRSZWdpc3Rlcih1"
    "c2VybmFtZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0"
    "aW9uKQogICAgICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9y"
    "ID0gc2VsZi5hdHRlbXB0TG9naW4odXNlcm5hbWUsIHBhc3N3b3JkKQogICAgICAgICAgICAgICAg"
    "ICAgIGlmIGxvZ2luRXJyb3IgPT0gMSBhbmQgX0FVVE9fUkVHSVNURVI6CiAgICAgICAgICAgICAg"
    "ICAgICAgICAgIHZpYVJlZ2lzdGVyID0gVHJ1ZQogICAgICAgICAgICAgICAgICAgICAgICBsb2dp"
    "bkVycm9yID0gc2VsZi5hdHRlbXB0UmVnaXN0ZXIodXNlcm5hbWUsIHBhc3N3b3JkLCAiIiwgIiIs"
    "IDEsIDAsICIiKQogICAgICAgICAgICAgICAgICAgICAgICBpZiBsb2dpbkVycm9yIGFuZCBHREgu"
    "bmFtZVRha2VuKHVzZXJuYW1lKToKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNUaGUgYWNj"
    "b3VudCBleGlzdHMsIHNvIHRoaXMgd2FzIG5ldmVyIGEKICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICNyZWdpc3RyYXRpb246IHRoZSBsb2dpbiBiZWZvcmUgaXQgZmFpbGVkIG9uIHRoZQogICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgI3Bhc3N3b3JkIG9yIC0gZmFyIG1vcmUgb2Z0ZW4gLSBv"
    "biB0aGUgc2VyaWFsLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgI2JlY2F1c2UgYWNjb3Vu"
    "dHMgYXJlIGJvdW5kIHRvIHRoZSBrZXkgdGhlIGNsaWVudAogICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgI2hhbmRzaGFrZXMgd2l0aCAoc2VlIGxvZ2luUGxheWVyJ3Mgc3RyaWN0IGxvb2t1cCku"
    "CiAgICAgICAgICAgICAgICAgICAgICAgICAgICAjRmFsbGluZyB0aHJvdWdoIHRvIHRoZSByZWdp"
    "c3RyYXRpb24gd29yZGluZyB0b2xkIGEKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNwbGF5"
    "ZXIgd2hvIGhhZCByZWluc3RhbGxlZCB0aGUgZ2FtZSB0aGF0IHRoZWlyCiAgICAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAjKnVzZXJuYW1lKiB3YXMgaW52YWxpZCwgd2hpY2ggc2VudCB0aGVtIG9m"
    "ZgogICAgICAgICAgICAgICAgICAgICAgICAgICAgI2ludmVudGluZyBuZXcgbmFtZXMgdGhhdCBj"
    "b3VsZCBuZXZlciB3b3JrLgogICAgICAgICAgICAgICAgICAgICAgICAgICAgdmlhUmVnaXN0ZXIg"
    "PSBGYWxzZQogICAgICAgICAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IDUKICAgICAg"
    "ICAgICAgICAgIGlmIGxvZ2luRXJyb3IgPT0gMDoKICAgICAgICAgICAgICAgICAgICBMT0dJTl9U"
    "SFJPVFRMRS5yZWNvcmRTdWNjZXNzKHBlZXJfaXApCiAgICAgICAgICAgICAgICAgICAgI1RPRE8g"
    "YmV0dGVyIGhhbmRsaW5nIG9mIFRJVExFIEFORCBNT1RECiAgICAgICAgICAgICAgICAgICAgc2Vs"
    "Zi5zZW5kUmF3KF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQoYnl0ZXMoc2VsZi5TSyksIERFRkFVTFRf"
    "VElUTEUsIERFRkFVTFRfTU9URCkpCiAgICAgICAgICAgICAgICAgICAgTElTID0gMAogICAgICAg"
    "ICAgICAgICAgZWxzZTogI2Vycm9yIGJhc2VkIG9uIGxvZ2luRXJyb3IgbnVtYmVyCiAgICAgICAg"
    "ICAgICAgICAgICAgY291bnQgPSBMT0dJTl9USFJPVFRMRS5yZWNvcmRGYWlsdXJlKHBlZXJfaXAp"
    "CiAgICAgICAgICAgICAgICAgICAgaWYgY291bnQgPT0gX0xPR0lOX0ZBSUxfTElNSVQ6CiAgICAg"
    "ICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBUaHJvdHRsaW5nIGxvZ2lucyBmcm9t"
    "IHtwZWVyX2lwfSAnCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYnKHtjb3VudH0gZmFp"
    "bHVyZXMgaW4ge19MT0dJTl9GQUlMX1dJTkRPV31zKScpCiAgICAgICAgICAgICAgICAgICAgZXJy"
    "bXNncyA9IF9SRUdJU1RFUl9FUlJPUlMgaWYgdmlhUmVnaXN0ZXIgZWxzZSBfTE9HSU5fRVJST1JT"
    "CiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KF9pbml0X2Vycm9yKGVycm1zZ3MuZ2V0"
    "KGxvZ2luRXJyb3IsICdMb2dpbiBmYWlsZWQnKSkpCiAgICBkZWYgZmluaXNoKHNlbGYpOgogICAg"
    "ICAgIHNlbGYuc2VydmVyLnVucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYpCiAgICAgICAgI1N0b3Ag"
    "dGhlIHdyaXRlciBmaXJzdDogaXQgaG9sZHMgdGhpcyBzb2NrZXQgYW5kIHdvdWxkIG90aGVyd2lz"
    "ZSBrZWVwCiAgICAgICAgI3dyaXRpbmcgb24gYmVoYWxmIG9mIGEgcGxheWVyIHdobyBoYXMgYWxy"
    "ZWFkeSBsZWZ0IGV2ZXJ5IGNoYW5uZWwuCiAgICAgICAgc2VsZi5fc3RvcFdyaXRlcigpCiAgICAg"
    "ICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFt"
    "ZX0gRGlzY29ubmVjdGVkJykKICAgICAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5z"
    "ZXJ2ZXIpCiAgICAgICAgI2NsZWFudXAgdXNlciBkYXRhCiAgICAgICAgI1RPRE8gY2hlY2sgaWYg"
    "dHJpZ2dlcmVkIG9uIGNyYXNoZWQgY29ubmVjdGlvbgogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6"
    "CiAgICAgICAgaWYgc2VsZi51c2VyIGlzIE5vbmU6CiAgICAgICAgICAgICNQb2xsZWQgYnkgdGhl"
    "IGNvbnRyb2wgcGFuZWwgb25jZSBhIHNlY29uZCB3aGlsZSBwbGF5ZXJzIGNvbm5lY3QgYW5kCiAg"
    "ICAgICAgICAgICNkaXNjb25uZWN0OyBhIGNvbm5lY3Rpb24gY2F1Z2h0IGJldHdlZW4gdGhlIHR3"
    "byB1c2VkIHRvIHJhaXNlIGhlcmUKICAgICAgICAgICAgI2FuZCBjb3N0IHRoZSBwYW5lbCBpdHMg"
    "d2hvbGUgcGxheWVyIHRhYmxlIGZvciB0aGF0IHRpY2suCiAgICAgICAgICAgIHJldHVybiB7J2dh"
    "bWUnOicnLCAndG93bic6JycsICdwb3MnOicnLCAnaWQnOjAsICdsb2dpblRpbWUnOicnfQogICAg"
    "ICAgIHJldHVybiB7CiAgICAgICAgICAgICNUT0RPIElQIGZvciBlbGV2YXRlZCBhdXRob3JpdHkK"
    "ICAgICAgICAgICAgIyduYW1lJzpzZWxmLnVzZXIubmFtZSwKICAgICAgICAgICAgJ2dhbWUnOnNl"
    "bGYudXNlci5nYW1lLmduYW1lIGlmIHNlbGYudXNlci5nYW1lIGVsc2UgJycsCiAgICAgICAgICAg"
    "ICd0b3duJzpzZWxmLnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiBzZWxmLnVzZXIuZ2FtZWNoYW5u"
    "ZWwgZWxzZSAnJywKICAgICAgICAgICAgJ3Bvcyc6c2VsZi51c2VyLnBvc2RhdGEgaWYgc2VsZi51"
    "c2VyLnBvc2RhdGEgZWxzZSAnJywKICAgICAgICAgICAgJ2lkJzpzZWxmLnVzZXIuaWRudW0sCiAg"
    "ICAgICAgICAgICdsb2dpblRpbWUnOmpzb25UaW1lKHNlbGYudXNlci5sb2dpblRpbWUpCiAgICAg"
    "ICAgfSNUT0RPIGVsZXZhdGVkIGF1dGhvcml0eSB2ZXJzaW9uCgpkZWYgY21kX2RlZmF1bHQoKToj"
    "YXJncyk6CiAgICAjcHJpbnQoYXJncykKICAgICNfcmVhZGNvbmZpZygpCiAgICBzZXJ2ZXIgPSBD"
    "b3JlU2VydmVyKCkKICAgIHdpdGggc2VydmVyOgogICAgICAgIHRzdCA9IHNpZ25hbC5zaWduYWwo"
    "c2lnbmFsLlNJR0lOVCwgc2VydmVyLmhhbmRsZV9zaWduYWwodGltZW91dD0yKSkKICAgICAgICAj"
    "cHJpbnQoJ0Fzc2lnbmVkIFNpZ25hbD8nLCB0c3QpCiAgICAgICAgI3NpZ25hbC5zaWduYWwoc2ln"
    "bmFsLlNJR1RFUk0sIHNlcnZlci5oYW5kbGVfc2lnbmFsKHRpbWVvdXQ9MSkpCiAgICAgICAgc2Vy"
    "dmVyLnNlcnZlX2ZvcmV2ZXIoKQoKI3NjcmlwdCBsYXVuY2hlZCwgY2hlY2sgYXJndW1lbnRzIGFu"
    "ZCBjb25maWcuIHNldHVwIHZhcmlvdXMgb2JqZWN0cwppZiBfX25hbWVfXyA9PSAnX19tYWluX18n"
    "OgogICAgcHJpbnQoJ0luaXRpYWxpemluZyBTZXJ2ZXInKQogICAgY21kX2RlZmF1bHQoKQo="
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
