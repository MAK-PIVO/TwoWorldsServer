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
    'network.vpn_ip': {'ru': 'Адрес в VPN / второй сети:', 'en': 'VPN / second network address:'},
    'network.vpn_ip_none': {'ru': '(других адаптеров нет)', 'en': '(no other adapters)'},
    'network.vpn_hint': {
        'ru': 'Играете через Radmin VPN или Hamachi — раздавайте друзьям адрес из строки «Адрес в VPN»,\n'
              'а не публичный. Пробрасывать порты на роутере при этом не нужно: мешает только\n'
              'брандмауэр Windows, который считает адаптер VPN общедоступной сетью.',
        'en': 'Playing over Radmin VPN or Hamachi: give friends the address from the "VPN address" row,\n'
              'not the public one. No router forwarding is needed then - the only thing in the way is\n'
              'the Windows firewall, which treats a VPN adapter as a public network.'},
    'network.firewall': {'ru': '🛡 Открыть порты в брандмауэре Windows',
                          'en': '🛡 Open the ports in Windows Firewall'},
    'network.firewall_working': {'ru': 'Запрашиваю права администратора...',
                                  'en': 'Asking for administrator rights...'},
    'network.firewall_ok': {'ru': '✔ Правила добавлены: {rules}.\nПорты открыты для всех сетей, включая VPN.',
                             'en': '✔ Rules added: {rules}.\nThe ports are open on every network, VPN included.'},
    'network.firewall_partial': {
        'ru': '✘ Добавлены не все правила: {rules}.\nОстальные можно завести вручную: Монитор брандмауэра '
              'Защитника Windows → Правила для входящих подключений.',
        'en': '✘ Not every rule was added: {rules}.\nThe rest can be added by hand: Windows Defender '
              'Firewall with Advanced Security → Inbound Rules.'},
    'network.firewall_failed': {'ru': '✘ Не удалось: {err}', 'en': '✘ Failed: {err}'},
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


#Address ranges the popular "virtual LAN" tools hand out. They are ordinary
#public ranges as far as an address itself can tell, which is why they have to
#be recognised by prefix rather than by asking whether they are private.
_VPN_RANGES = (('26.', 'Radmin VPN'), ('25.', 'Hamachi'))


def local_ipv4_addresses():
    """Every IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    return found


def describe_vpn_addresses(addresses, internet_ip):
    """-> [(address, name_of_the_network_or_empty)]

    The addresses to give friends who reach this machine some way other than
    over the internet: a VPN adapter, or a second LAN. get_local_ip() cannot
    answer this - it answers "which adapter reaches the internet", so it never
    names the Radmin one, which is the only address a Radmin session can use.
    """
    out = []
    for addr in addresses:
        if addr == internet_ip or addr.startswith('127.'):
            continue
        name = ''
        for prefix, label in _VPN_RANGES:
            if addr.startswith(prefix):
                name = label
                break
        out.append((addr, name))
    return out


def firewall_rules(lobby_port, game_port):
    """-> [rule dicts] - what has to be open for a co-op session.

    The lobby port for this panel's own server, and the game port both ways
    for the DirectPlay session the players open directly to each other. Every
    profile, because a VPN adapter is an unidentified - that is, public -
    network to Windows, and the "private networks only" tick a game usually
    gets on first run does not cover it. Over a VPN this is the whole problem:
    there is no router to forward anything, so nothing but the firewall stands
    between the two players.
    """
    return [{'name': 'TW1 Lobby', 'port': int(lobby_port), 'protocol': 'TCP',
             'dir': 'in', 'action': 'allow', 'profile': 'any'},
            {'name': 'TW1 Game (TCP)', 'port': int(game_port), 'protocol': 'TCP',
             'dir': 'in', 'action': 'allow', 'profile': 'any'},
            {'name': 'TW1 Game (UDP)', 'port': int(game_port), 'protocol': 'UDP',
             'dir': 'in', 'action': 'allow', 'profile': 'any'}]


def firewall_script(rules):
    """The netsh lines that install `rules`, each preceded by a delete of the
    same name so pressing the button a second time replaces rather than
    duplicates (Windows happily keeps several identically named rules)."""
    lines = ['@echo off']
    for r in rules:
        lines.append(f'netsh advfirewall firewall delete rule name="{r["name"]}" >nul 2>&1')
        lines.append(f'netsh advfirewall firewall add rule name="{r["name"]}" '
                     f'dir={r["dir"]} action={r["action"]} protocol={r["protocol"]} '
                     f'localport={r["port"]} profile={r["profile"]}')
    return lines


def firewall_rule_present(name):
    """Is a rule of this name installed? Readable without administrator
    rights, so this is how the elevated run is checked rather than trusting
    its exit code (which belongs to the UAC prompt, not to netsh)."""
    try:
        r = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule',
                            f'name={name}'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return r.returncode == 0
    except OSError:
        return False


class FirewallError(Exception):
    pass


def firewall_open_ports(lobby_port, game_port):
    """Install the inbound rules, asking for administrator rights. Returns the
    list of rules that are present afterwards; raises FirewallError if the
    elevation prompt was refused or the platform has no ShellExecute."""
    rules = firewall_rules(lobby_port, game_port)
    if not hasattr(ctypes, 'windll'):
        raise FirewallError('Только для Windows.')
    path = os.path.join(tempfile.gettempdir(), 'tw1_firewall.cmd')
    with open(path, 'w', encoding='cp866') as f:
        f.write('\r\n'.join(firewall_script(rules)) + '\r\n')
    #ShellExecuteW with "runas" is the UAC prompt. Anything above 32 means the
    #prompt was accepted and the script was started; 5 is "user said no".
    rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', 'cmd.exe',
                                             f'/c "{path}"', None, 0)
    if rc <= 32:
        raise FirewallError('Запрос прав администратора отклонён.' if rc == 5
                            else f'Не удалось запустить netsh (код {rc}).')
    #ShellExecuteW does not wait, and netsh takes a moment per rule.
    deadline = time.monotonic() + 20
    present = []
    while time.monotonic() < deadline:
        present = [r['name'] for r in rules if firewall_rule_present(r['name'])]
        if len(present) == len(rules):
            break
        time.sleep(0.5)
    return rules, present


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
    # Single source of truth for every colour in the app - every widget below
    # references these instead of a one-off hex literal, so the whole panel
    # is one dark, cohesive theme rather than a default-white 'clam' app with
    # a dark log box bolted on.
    BG = '#1b1e23'            # window background - the darkest surface
    SURFACE = '#242830'       # panels, frames, labelframes, tab content
    SURFACE_ALT = '#2a2f38'   # tab strip, table headers, menus, idle buttons
    FIELD = '#20242b'         # entries, dropdowns, tables - reads as "recessed"
    BORDER = '#39404b'
    TEXT = '#e7eaf0'
    MUTED = '#8993a6'         # secondary/hint text
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
        self._fit_to_content()

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

    def _fit_to_content(self):
        """Resize once, after the tabs exist, to whatever they actually need.

        The numbers above are a guess made before a single widget is built, so
        they are either too small (the densest tab gets a scrollbar it did not
        need) or too large (empty space on a short tab). Tk can answer the
        question properly once the widgets are laid out: winfo_reqwidth is the
        width at which nothing is squeezed. Clamped to the screen, and never
        shrinking below the guess, so this can only improve the fit."""
        self.update_idletasks()
        need_w = self.winfo_reqwidth()
        need_h = self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        #a little air around the content, then the screen has the final say
        width = min(max(need_w + 24, self._ABS_MIN_W), int(sw * 0.92))
        height = min(max(need_h + 24, self._ABS_MIN_H), int(sh * 0.88))
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 3)
        self.geometry(f'{width}x{height}+{x}+{y}')

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
        style.configure('Status.TLabel', font=('Segoe UI', 10, 'bold'),
                         background=self.SURFACE)

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

        #The address above is the one that reaches the internet, and over a
        #VPN it is the wrong one to give anybody - see describe_vpn_addresses.
        vip_lbl = ttk.Label(f)
        vip_lbl.grid(row=3, column=0, sticky='w', **pad)
        self._tr('network.vpn_ip', lambda t: vip_lbl.configure(text=t))
        self.net_vpn_ip_label = ttk.Label(f, text='...')
        self.net_vpn_ip_label.grid(row=3, column=1, sticky='w', **pad)

        pip_lbl = ttk.Label(f)
        pip_lbl.grid(row=4, column=0, sticky='w', **pad)
        self._tr('network.public_ip', lambda t: pip_lbl.configure(text=t))
        self.net_public_ip_label = ttk.Label(f)
        self.net_public_ip_label.grid(row=4, column=1, sticky='w', **pad)
        self._tr('network.determining', lambda t: self.net_public_ip_label.configure(text=t))

        port_lbl = ttk.Label(f)
        port_lbl.grid(row=5, column=0, sticky='w', **pad)
        self._tr('network.server_port', lambda t: port_lbl.configure(text=t))
        self.net_port_label = ttk.Label(f, text=str(DEFAULT_PORT))
        self.net_port_label.grid(row=5, column=1, sticky='w', **pad)

        gport_lbl = ttk.Label(f)
        gport_lbl.grid(row=6, column=0, sticky='w', **pad)
        self._tr('network.game_port', lambda t: gport_lbl.configure(text=t))
        self.net_game_port_label = ttk.Label(f, text=str(DEFAULT_GAME_PORT))
        self.net_game_port_label.grid(row=6, column=1, sticky='w', **pad)
        gport_hint = ttk.Label(f, foreground=self.MUTED, justify='left')
        gport_hint.grid(row=7, column=0, columnspan=2, sticky='w', padx=10)
        self._tr('network.game_port_hint', lambda t: gport_hint.configure(text=t))

        vpn_hint = ttk.Label(f, foreground=self.MUTED, justify='left')
        vpn_hint.grid(row=8, column=0, columnspan=2, sticky='w', padx=10, pady=(6, 0))
        self._tr('network.vpn_hint', lambda t: vpn_hint.configure(text=t))

        btns = ttk.Frame(f)
        btns.grid(row=9, column=0, columnspan=2, sticky='w', padx=10, pady=14)
        refresh_btn = ttk.Button(btns, command=self._refresh_network_info)
        refresh_btn.pack(side='left')
        self._tr('network.refresh', lambda t: refresh_btn.configure(text=t))
        check_btn = ttk.Button(btns, command=self._open_port_checker)
        check_btn.pack(side='left', padx=8)
        self._tr('network.check_port', lambda t: check_btn.configure(text=t))
        upnp_btn = ttk.Button(btns, style='Accent.TButton', command=self._try_upnp)
        upnp_btn.pack(side='left', padx=8)
        self._tr('network.try_upnp', lambda t: upnp_btn.configure(text=t))
        fw_btn = ttk.Button(btns, command=self._open_firewall)
        fw_btn.pack(side='left', padx=8)
        self._tr('network.firewall', lambda t: fw_btn.configure(text=t))

        self.net_status_label = ttk.Label(f, text='', wraplength=820, justify='left')
        self.net_status_label.grid(row=10, column=0, columnspan=2, sticky='w', padx=10, pady=(6, 0))

        ttk.Separator(f, orient='horizontal').grid(row=11, column=0, columnspan=2, sticky='ew', pady=10)

        srvhdr = ttk.Label(f, style='Header.TLabel')
        srvhdr.grid(row=12, column=0, columnspan=2, sticky='w', **pad)
        self._tr('network.server_list_header', lambda t: srvhdr.configure(text=t))
        ttk.Label(f, text='ВАЖНО: названия строк ("WarNet Europe" и т.п.) зашиты в саму игру - её меню всегда\n'
                           'покажет ровно эти же пункты, что бы тут ни было. Из реестра берётся только АДРЕС\n'
                           'для каждого из них. Поэтому переименовывать/добавлять новые пункты бессмысленно -\n'
                           'редактируется только колонка "Адрес" (двойной клик по ячейке).',
                  foreground=self.MUTED, justify='left').grid(row=13, column=0, columnspan=2, sticky='w', padx=10)

        self.servers_tree = ttk.Treeview(f, columns=('name', 'addr'), show='headings', height=6)
        self._tr('network.col_name', lambda t: self.servers_tree.heading('name', text=t))
        self._tr('network.col_addr', lambda t: self.servers_tree.heading('addr', text=t))
        self.servers_tree.column('name', width=200, anchor='w')
        self.servers_tree.column('addr', width=280, anchor='w')
        self.servers_tree.grid(row=14, column=0, columnspan=2, sticky='w', padx=10, pady=6)
        self.servers_tree.bind('<Double-1>', self._edit_server_address)

        fillrow = ttk.Frame(f)
        fillrow.grid(row=15, column=0, columnspan=2, sticky='w', padx=10, pady=(8, 4))
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
        srvbtns.grid(row=16, column=0, columnspan=2, sticky='w', padx=10, pady=(4, 4))
        save_srv_btn = ttk.Button(srvbtns, style='Accent.TButton', command=self._save_server_list)
        save_srv_btn.pack(side='left')
        self._tr('network.save_to_game', lambda t: save_srv_btn.configure(text=t))

        ttk.Label(f, text='"localhost"/"127.0.0.1" - для игры вдвоём с одного компьютера (второй клиент игры\n'
                           'на этой же машине). Для игры по локальной сети используй "Локальный IP".',
                  foreground=self.MUTED, justify='left').grid(row=17, column=0, columnspan=2, sticky='w', padx=10)

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
            internet_ip = get_local_ip()
            self.net_local_ip_label.configure(text=internet_ip)
        except Exception:
            internet_ip = ''
            self.net_local_ip_label.configure(text=T('network.undetermined'))
        others = describe_vpn_addresses(local_ipv4_addresses(), internet_ip)
        self.net_vpn_ip_label.configure(
            text=', '.join(f'{a} ({n})' if n else a for (a, n) in others)
                 or T('network.vpn_ip_none'))
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

    def _open_firewall(self):
        #The counterpart of the UPnP button for everyone who is not going
        #through a router at all: over Radmin/Hamachi there is nothing to
        #forward, and the firewall is the only thing left in the way.
        try:
            lobby = int(self.net_port_label.cget('text'))
            game = int(self.net_game_port_label.cget('text'))
        except ValueError:
            messagebox.showerror(T('server.module_load_error_title'), T('network.bad_port'))
            return
        self.net_status_label.configure(text=T('network.firewall_working'))
        self.update_idletasks()
        threading.Thread(target=self._firewall_worker, args=(lobby, game), daemon=True).start()

    def _firewall_worker(self, lobby, game):
        try:
            (rules, present) = firewall_open_ports(lobby, game)
        except Exception as e:
            print(f'[Сеть] Брандмауэр: {e}')
            msg = T('network.firewall_failed', err=e)
        else:
            names = ', '.join(present) if present else '-'
            if len(present) == len(rules):
                print(f'[Сеть] Брандмауэр: правила добавлены ({names}).')
                msg = T('network.firewall_ok', rules=names)
            else:
                print(f'[Сеть] Брандмауэр: добавлены не все правила ({names}).')
                msg = T('network.firewall_partial', rules=names)
        self.after(0, lambda: self.net_status_label.configure(text=msg))

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
    "aXNfZ2xvYmFsCiAgICBleGNlcHQgVmFsdWVFcnJvcjoKICAgICAgICByZXR1cm4gRmFsc2UKZGVm"
    "IF9pc0xvb3BiYWNrQWRkcmVzcyhhZGRyKToKICAgICNBIGhvc3QgdGhhdCByZWFjaGVkIHRoZSBs"
    "b2JieSB0aHJvdWdoIGxvY2FsaG9zdCBpcyBydW5uaW5nIG9uIHRoaXMgdmVyeQogICAgI21hY2hp"
    "bmUgLSBzZWUgcGlja0pvaW5BZGRyZXNzLgogICAgdHJ5OgogICAgICAgIHJldHVybiBpcGFkZHJl"
    "c3MuaXBfYWRkcmVzcyhhZGRyKS5pc19sb29wYmFjawogICAgZXhjZXB0IFZhbHVlRXJyb3I6CiAg"
    "ICAgICAgcmV0dXJuIEZhbHNlCl9wdWJsaWNJcENhY2hlID0gW05vbmUsIDAuMF0KX3B1YmxpY0lw"
    "TG9jayA9IHRocmVhZGluZy5Mb2NrKCkKZGVmIF9zZXJ2ZXJQdWJsaWNBZGRyZXNzKCk6CiAgICAj"
    "VGhlIHB1YmxpYyBhZGRyZXNzIG9mIHRoZSBtYWNoaW5lIHRoaXMgc2VydmVyIHJ1bnMgb24uIFVz"
    "ZWQgZm9yIGEgaG9zdAogICAgI3dob3NlIG9ic2VydmVkIGFkZHJlc3MgaXMgcHJpdmF0ZSwgd2hp"
    "Y2ggaGFwcGVucyB3aGVuZXZlciB0aGUgaG9zdCBzaXRzCiAgICAjb24gdGhlIHNhbWUgTEFOL3Jv"
    "dXRlciBhcyB0aGUgbG9iYnkgLSBpbmNsdWRpbmcgdGhlIGhhaXJwaW4tTkFUIGNhc2UKICAgICN3"
    "aGVyZSBhIGxvY2FsIHBsYXllciByZWFjaGVzIHRoZSBzZXJ2ZXIgdGhyb3VnaCB0aGUgcm91dGVy"
    "J3MgcHVibGljCiAgICAjYWRkcmVzcyBhbmQgdGhlIHJvdXRlciByZXdyaXRlcyB0aGUgc291cmNl"
    "IHRvIGl0cyBvd24gTEFOIGFkZHJlc3MuIEluIGFsbAogICAgI29mIHRob3NlIHRoZSBob3N0IHJl"
    "YWNoZXMgdGhlIGludGVybmV0IHRocm91Z2ggdGhlIHNhbWUgcm91dGVyIGFzIHdlIGRvLAogICAg"
    "I3NvIG91ciBwdWJsaWMgYWRkcmVzcyBpcyB0aGVpcnMuCiAgICB3aXRoIF9wdWJsaWNJcExvY2s6"
    "CiAgICAgICAgKGlwLCBmZXRjaGVkKSA9IF9wdWJsaWNJcENhY2hlCiAgICAgICAgaWYgaXAgYW5k"
    "ICh0aW1lLm1vbm90b25pYygpIC0gZmV0Y2hlZCkgPCAzNjAwOgogICAgICAgICAgICByZXR1cm4g"
    "aXAKICAgIGdvdCA9IE5vbmUKICAgIGZvciAodXJsLCBoZHJzKSBpbiAoKCdodHRwczovLzJpcC5y"
    "dScsIHsnVXNlci1BZ2VudCc6ICdjdXJsLzguMCd9KSwKICAgICAgICAgICAgICAgICAgICAgICAg"
    "KCdodHRwczovL2FwaS5pcGlmeS5vcmcnLCB7fSkpOgogICAgICAgIHRyeToKICAgICAgICAgICAg"
    "cmVxID0gdXJsbGliLnJlcXVlc3QuUmVxdWVzdCh1cmwsIGhlYWRlcnM9aGRycykKICAgICAgICAg"
    "ICAgd2l0aCB1cmxsaWIucmVxdWVzdC51cmxvcGVuKHJlcSwgdGltZW91dD00KSBhcyByOgogICAg"
    "ICAgICAgICAgICAgY2FuZCA9IHIucmVhZCgpLmRlY29kZSgnYXNjaWknLCBlcnJvcnM9J2lnbm9y"
    "ZScpLnN0cmlwKCkKICAgICAgICAgICAgaWYgX2lzR2xvYmFsQWRkcmVzcyhjYW5kKToKICAgICAg"
    "ICAgICAgICAgIGdvdCA9IGNhbmQKICAgICAgICAgICAgICAgIGJyZWFrCiAgICAgICAgZXhjZXB0"
    "IEV4Y2VwdGlvbjoKICAgICAgICAgICAgY29udGludWUgI29mZmxpbmUgb3IgdGhlIHNlcnZpY2Ug"
    "aXMgYmxvY2tlZDsgbm90IGZhdGFsCiAgICB3aXRoIF9wdWJsaWNJcExvY2s6CiAgICAgICAgaWYg"
    "Z290OgogICAgICAgICAgICBfcHVibGljSXBDYWNoZVs6XSA9IFtnb3QsIHRpbWUubW9ub3Rvbmlj"
    "KCldCiAgICByZXR1cm4gZ290CmRlZiBwaWNrR2FtZUhvc3RBZGRyZXNzKHBlZXJfYWRkcik6CiAg"
    "ICAjLT4gKGFkZHJlc3Nfb3JfTm9uZSwgbm90ZV9mb3JfdGhlX2xvZykKICAgICNUaGUgaG9zdCdz"
    "IG93biBhZGRyZXNzIHdpbnMgd2hlbmV2ZXIgaXQgaXMgb25lIHRoZSByZXN0IG9mIHRoZSBpbnRl"
    "cm5ldAogICAgI2NhbiByZWFjaC4gUHVibGljSG9zdEFkZHJlc3MgaXMgTk9UIGEgYmxhbmtldCBv"
    "dmVycmlkZTogaXQgZGVzY3JpYmVzIHRoZQogICAgI25ldHdvcmsgKnRoaXMgc2VydmVyKiBzaXRz"
    "IG9uLCBzbyBhcHBseWluZyBpdCB0byBhIGhvc3Qgd2hvIGNvbm5lY3RlZAogICAgI2Zyb20gc29t"
    "ZXdoZXJlIGVsc2UgZW50aXJlbHkgd291bGQgc2VuZCBldmVyeSBqb2luZXIgdG8gdGhlIHdyb25n"
    "CiAgICAjbWFjaGluZSAtIGl0IG9ubHkgYW5zd2VycyB0aGUgcXVlc3Rpb24gIndoYXQgaXMgdGhl"
    "IHB1YmxpYyBhZGRyZXNzIG9mIGEKICAgICNob3N0IHRoYXQgYXBwZWFycyB0byBiZSBvbiBvdXIg"
    "b3duIExBTiIuCiAgICBpZiBfaXNHbG9iYWxBZGRyZXNzKHBlZXJfYWRkcik6CiAgICAgICAgcmV0"
    "dXJuIHBlZXJfYWRkciwgZidob3N0IGNvbm5lY3RlZCBmcm9tIHtwZWVyX2FkZHJ9JwogICAgaWYg"
    "X1BVQkxJQ19IT1NUX0FERFJFU1M6CiAgICAgICAgcmV0dXJuIF9QVUJMSUNfSE9TVF9BRERSRVNT"
    "LCAoZidob3N0IGNvbm5lY3RlZCBmcm9tIHtwZWVyX2FkZHJ9IChwcml2YXRlIC0gc2FtZSAnCiAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgZiduZXR3b3JrIGFzIHRoaXMgc2Vy"
    "dmVyKSwgdXNpbmcgY29uZmlndXJlZCAnCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgZidQdWJsaWNIb3N0QWRkcmVzcyB7X1BVQkxJQ19IT1NUX0FERFJFU1N9JykKICAgIHB1"
    "YiA9IF9zZXJ2ZXJQdWJsaWNBZGRyZXNzKCkKICAgIGlmIHB1YjoKICAgICAgICByZXR1cm4gcHVi"
    "LCAoZidob3N0IGNvbm5lY3RlZCBmcm9tIHtwZWVyX2FkZHJ9IChwcml2YXRlIC0gc2FtZSBuZXR3"
    "b3JrIGFzICcKICAgICAgICAgICAgICAgICAgICAgZid0aGlzIHNlcnZlciksIHVzaW5nIG91ciBw"
    "dWJsaWMgYWRkcmVzcyB7cHVifScpCiAgICByZXR1cm4gTm9uZSwgKGYnaG9zdCBjb25uZWN0ZWQg"
    "ZnJvbSB7cGVlcl9hZGRyfSAocHJpdmF0ZSkgYW5kIHRoaXMgc2VydmVyICcKICAgICAgICAgICAg"
    "ICAgICAgZidjb3VsZCBub3QgZGV0ZXJtaW5lIGl0cyBvd24gcHVibGljIGFkZHJlc3MnKQpkZWYg"
    "X25hdEJldHdlZW4oam9pbmVyX3BlZXIsIGpvaW5lcl9sb2NhbCk6CiAgICAjRGlkIGEgcm91dGVy"
    "IHN0YW5kIGJldHdlZW4gdGhpcyBqb2luZXIgYW5kIHVzPyBUaGV5IGNhbWUgaW4gZnJvbSB0aGUK"
    "ICAgICNpbnRlcm5ldCwgYnV0IHRoZSBhZGRyZXNzIHRoZXkgbGFuZGVkIG9uIGlzIGEgcHJpdmF0"
    "ZSBvbmUgb2Ygb3Vycywgc28gYQogICAgI3BvcnQgZm9yd2FyZCBjYXJyaWVkIHRoZW0gdGhlIGxh"
    "c3QgaG9wIC0gYW5kIHRoYXQgcHJpdmF0ZSBhZGRyZXNzIGlzIG5vdAogICAgI3NvbWV0aGluZyB0"
    "aGV5IGNhbiBvcGVuIGEgc2Vjb25kIGNvbm5lY3Rpb24gdG8uCiAgICByZXR1cm4gKF9pc0dsb2Jh"
    "bEFkZHJlc3Moam9pbmVyX3BlZXIpIGFuZCBib29sKGpvaW5lcl9sb2NhbCkKICAgICAgICAgICAg"
    "YW5kIG5vdCBfaXNHbG9iYWxBZGRyZXNzKGpvaW5lcl9sb2NhbCkKICAgICAgICAgICAgYW5kIG5v"
    "dCBfaXNMb29wYmFja0FkZHJlc3Moam9pbmVyX2xvY2FsKSkKZGVmIHBpY2tKb2luQWRkcmVzcyho"
    "b3N0X3BlZXIsIGpvaW5lcl9wZWVyLCBqb2luZXJfbG9jYWwpOgogICAgIy0+IChhZGRyZXNzX29y"
    "X05vbmUsIG5vdGVfZm9yX3RoZV9sb2cpCiAgICAjV2hpY2ggb2YgdGhlIGhvc3QncyBhZGRyZXNz"
    "ZXMgVEhJUyBqb2luZXIgc2hvdWxkIGJlIHNlbnQgdG8uIEFza2VkIG9uY2UKICAgICNwZXIgam9p"
    "bmVyLCB3aGVuIC9qb2luZ2FtZSBpcyBhbnN3ZXJlZCwgYmVjYXVzZSB0aGUgYW5zd2VyIGRlcGVu"
    "ZHMgb24KICAgICN3aGVyZSB0aGUgam9pbmVyIGlzOiB0aGUgTEFOIGFkZHJlc3MgdGhhdCBpcyBl"
    "eGFjdGx5IHJpZ2h0IGZvciBhIHBsYXllcgogICAgI29uIHRoZSBob3N0J3MgTEFOIGlzIHVucmVh"
    "Y2hhYmxlIGZvciBldmVyeW9uZSBlbHNlLCBhbmQgdGhlIHB1YmxpYwogICAgI2FkZHJlc3MgdGhh"
    "dCBpcyByaWdodCBmb3IgYSBwbGF5ZXIgb3V0IG9uIHRoZSBpbnRlcm5ldCBpcyB1bnJlYWNoYWJs"
    "ZQogICAgI292ZXIgYSBWUE4gbGlrZSBSYWRtaW4gb3IgSGFtYWNoaSwgd2hlcmUgbm8gcm91dGVy"
    "IGZvcndhcmRzIGFueXRoaW5nIGFuZAogICAgI3RoZSBwdWJsaWMgYWRkcmVzcyBqdXN0IGxlYWRz"
    "IHRvIGEgY2xvc2VkIHBvcnQuCiAgICBpZiBfaXNHbG9iYWxBZGRyZXNzKGhvc3RfcGVlcik6CiAg"
    "ICAgICAgI0FscmVhZHkgcmVhY2hhYmxlIGFzIGl0IHN0YW5kcyAtIHRoZSBwbGFpbiBpbnRlcm5l"
    "dCBjYXNlLCBhbmQgYWxzbwogICAgICAgICNSYWRtaW4vSGFtYWNoaTogdGhlaXIgMjYvOCBhbmQg"
    "MjUvOCBhZGRyZXNzZXMgYXJlIG9yZGluYXJ5IHB1YmxpYwogICAgICAgICNyYW5nZXMgYXMgZmFy"
    "IGFzIHRoZSBhZGRyZXNzIGl0c2VsZiBjYW4gdGVsbCwgd2hpY2ggaXMgZXhhY3RseSB3aHkKICAg"
    "ICAgICAjYSBWUE4gcGxheWVyIGNhbm5vdCBiZSByZWNvZ25pc2VkIGJ5IHRoZSBzaGFwZSBvZiB0"
    "aGVpciBhZGRyZXNzLgogICAgICAgIHJldHVybiBob3N0X3BlZXIsIGYnaG9zdCBjb25uZWN0ZWQg"
    "ZnJvbSB7aG9zdF9wZWVyfScKICAgIGlmIF9pc0xvb3BiYWNrQWRkcmVzcyhob3N0X3BlZXIpOgog"
    "ICAgICAgICNUaGUgaG9zdCdzIGdhbWUgaXMgb24gdGhpcyB2ZXJ5IG1hY2hpbmUgLSBpdCByZWFj"
    "aGVkIHRoZSBsb2JieQogICAgICAgICN0aHJvdWdoIGxvY2FsaG9zdCAtIHNvICJ3aGVyZSBpcyB0"
    "aGUgaG9zdCIgYW5kICJ3aGVyZSBpcyB0aGlzCiAgICAgICAgI3NlcnZlciIgYXJlIHRoZSBzYW1l"
    "IHF1ZXN0aW9uLCBhbmQgdGhlIGpvaW5lciBoYXMgYWxyZWFkeSBhbnN3ZXJlZAogICAgICAgICNp"
    "dCBieSByZWFjaGluZyB1cyBhdCBqb2luZXJfbG9jYWwuIFRydXN0IHRoYXQgdW5sZXNzIGEgcm91"
    "dGVyCiAgICAgICAgI2ZvcndhcmRlZCB0aGVtIGhlcmUsIGluIHdoaWNoIGNhc2Ugb3VyIG93biBh"
    "ZGRyZXNzIGlzIG5vdCB0aGVpcnMuCiAgICAgICAgaWYgam9pbmVyX2xvY2FsIGFuZCBub3QgX25h"
    "dEJldHdlZW4oam9pbmVyX3BlZXIsIGpvaW5lcl9sb2NhbCk6CiAgICAgICAgICAgIHJldHVybiBq"
    "b2luZXJfbG9jYWwsIChmJ2hvc3QgaXMgb24gdGhpcyBtYWNoaW5lICh7aG9zdF9wZWVyfSk7IGpv"
    "aW5lciBjYW1lIGluICcKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYnb24ge2pv"
    "aW5lcl9sb2NhbH0sIGFkdmVydGlzaW5nIHRoYXQnKQogICAgZWxpZiBqb2luZXJfcGVlciBhbmQg"
    "bm90IF9pc0dsb2JhbEFkZHJlc3Moam9pbmVyX3BlZXIpOgogICAgICAgICNIb3N0IGFuZCBqb2lu"
    "ZXIgYm90aCBvbiBwcml2YXRlIGFkZHJlc3Mgc3BhY2U6IHNhbWUgTEFOLCBzbyB0aGUKICAgICAg"
    "ICAjaG9zdCdzIG93biBhZGRyZXNzIGlzIHRoZSBvbmUgdGhhdCB3b3JrcyBhbmQgYSBwdWJsaWMg"
    "b25lIHdvdWxkIG5vdC4KICAgICAgICByZXR1cm4gaG9zdF9wZWVyLCAoZidob3N0IGNvbm5lY3Rl"
    "ZCBmcm9tIHtob3N0X3BlZXJ9LCBqb2luZXIgZnJvbSAnCiAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgIGYne2pvaW5lcl9wZWVyfSAtIHNhbWUgbmV0d29yaywga2VlcGluZyB0aGUgaG9zdCBhZGRy"
    "ZXNzJykKICAgICNBIGhvc3Qgd2UgY2Fubm90IHJlYWNoIGRpcmVjdGx5IGFuZCBhIGpvaW5lciBv"
    "dXQgb24gdGhlIGludGVybmV0OiBvbmx5IGEKICAgICNwdWJsaWMgYWRkcmVzcyBvZiB0aGUgaG9z"
    "dCdzIG93biBuZXR3b3JrIGJyaWRnZXMgdGhhdCwgYW5kIGl0IGhhcyB0byBiZQogICAgI2Zvcndh"
    "cmRlZCB0aGVyZS4KICAgIHJldHVybiBwaWNrR2FtZUhvc3RBZGRyZXNzKGhvc3RfcGVlcikKZGVm"
    "IF9hcHBseUdhbWVIb3N0KHVybCwgYWRkciwgbm90ZSk6CiAgICAjLT4gKHVybCwgbm90ZV9mb3Jf"
    "dGhlX2xvZykKICAgIGlmIG5vdCBhZGRyOgogICAgICAgIHJldHVybiB1cmwsIG5vdGUgKyAnIC0g"
    "dXJsIHBhc3NlZCB0aHJvdWdoIHVuY2hhbmdlZCcKICAgIGlmIF9SRV9EUF9IT1NUTkFNRS5zZWFy"
    "Y2godXJsKToKICAgICAgICBvbGQgPSBfUkVfRFBfSE9TVE5BTUUuc2VhcmNoKHVybCkuZ3JvdXAo"
    "MikKICAgICAgICB1cmwgPSBfUkVfRFBfSE9TVE5BTUUuc3ViKGxhbWJkYSBtOiBtLmdyb3VwKDEp"
    "ICsgYWRkciwgdXJsLCBjb3VudD0xKQogICAgICAgIG5vdGUgKz0gZic7IGhvc3RuYW1lIHtvbGQh"
    "cn0gLT4ge2FkZHIhcn0nCiAgICBlbHNlOgogICAgICAgICNObyBob3N0bmFtZSBhdCBhbGw6IHRo"
    "ZSBqb2luZXIgd291bGQgaGF2ZSBub3RoaW5nIHRvIGNvbm5lY3QgdG8uCiAgICAgICAgdXJsID0g"
    "dXJsICsgKCcnIGlmIHVybC5lbmRzd2l0aCgnOycpIGVsc2UgJzsnKSArICdob3N0bmFtZT0nICsg"
    "YWRkcgogICAgICAgIG5vdGUgKz0gZic7IG5vIGhvc3RuYW1lIGluIHVybCwgYXBwZW5kZWQge2Fk"
    "ZHIhcn0nCiAgICBpZiBfU1RSSVBfQUxUX0FERFJFU1NFUyBhbmQgX1JFX0RQX0FMVC5zZWFyY2go"
    "dXJsKToKICAgICAgICB1cmwgPSBfUkVfRFBfQUxULnN1YignJywgdXJsKQogICAgICAgIG5vdGUg"
    "Kz0gJzsgZHJvcHBlZCBhbHQ9IGNhbmRpZGF0ZSBhZGRyZXNzZXMnCiAgICByZXR1cm4gdXJsLCBu"
    "b3RlCmRlZiByZXdyaXRlR2FtZUhvc3QodXJsLCBwZWVyX2FkZHIpOgogICAgIy0+ICh1cmwsIG5v"
    "dGVfZm9yX3RoZV9sb2cpCiAgICAjVGhlIHJvb20ncyBvd24gdXJsLCBkZWNpZGVkIHdoZW4gaXQg"
    "aXMgY3JlYXRlZCBhbmQgYmVmb3JlIGFueW9uZSBoYXMKICAgICNhc2tlZCB0byBqb2luIGl0LiBV"
    "c2VkIGZvciB0aGUgbG9nIGFuZCBhcyB0aGUgZmFsbGJhY2s7IHdoYXQgYSBqb2luZXIgaXMKICAg"
    "ICNhY3R1YWxseSBoYW5kZWQgY29tZXMgZnJvbSByZXdyaXRlR2FtZUhvc3RGb3JKb2luZXIoKS4K"
    "ICAgIGlmIG5vdCBfUkVXUklURV9HQU1FX0hPU1Qgb3Igbm90IHVybCBvciBub3QgcGVlcl9hZGRy"
    "OgogICAgICAgIHJldHVybiB1cmwsICdyZXdyaXRlIGRpc2FibGVkJwogICAgcmV0dXJuIF9hcHBs"
    "eUdhbWVIb3N0KHVybCwgKnBpY2tHYW1lSG9zdEFkZHJlc3MocGVlcl9hZGRyKSkKZGVmIHJld3Jp"
    "dGVHYW1lSG9zdEZvckpvaW5lcih1cmwsIGhvc3RfcGVlciwgam9pbmVyX3BlZXIsIGpvaW5lcl9s"
    "b2NhbCk6CiAgICAjLT4gKHVybCwgbm90ZV9mb3JfdGhlX2xvZykKICAgIGlmIG5vdCBfUkVXUklU"
    "RV9HQU1FX0hPU1Qgb3Igbm90IHVybCBvciBub3QgaG9zdF9wZWVyOgogICAgICAgIHJldHVybiB1"
    "cmwsICdyZXdyaXRlIGRpc2FibGVkJwogICAgcmV0dXJuIF9hcHBseUdhbWVIb3N0KHVybCwgKnBp"
    "Y2tKb2luQWRkcmVzcyhob3N0X3BlZXIsIGpvaW5lcl9wZWVyLCBqb2luZXJfbG9jYWwpKQpkZWYg"
    "cHJldHR5X2d1aWQoZ3VpZCk6CiAgICAoYSxiLGMsZCkgPSBzdHJ1Y3QudW5wYWNrKCI8SUhIOHMi"
    "LCBndWlkKQogICAgZGEgPSAnJwogICAgZGIgPSAnJwogICAgZm9yIGkgaW4gZFswOjJdOgogICAg"
    "ICAgIGRhKz0nezowMnh9Jy5mb3JtYXQoaSkKICAgIGZvciBpIGluIGRbMjpdOgogICAgICAgIGRi"
    "Kz0nezowMnh9Jy5mb3JtYXQoaSkKICAgIHJldHVybiAnezowOHh9LXs6MDR4fS17OjA0eH0te30t"
    "e30nLmZvcm1hdChhLGIsYyxkYSxkYikKZGVmIF9lbShtc2cpOgogICAgI0V2ZXJ5IHRleHQgY29t"
    "bWFuZCBsZWF2aW5nIHRoaXMgc2VydmVyIGlzIGZyYW1lZCBoZXJlLCB3aGljaCBtYWtlcyBpdCB0"
    "aGUKICAgICNvbmUgcGxhY2UgdGhhdCBjYW4gc2VlIGFuIG92ZXItbG9uZyBsaW5lIG5vIG1hdHRl"
    "ciB3aGljaCBoYW5kbGVyIGJ1aWx0IGl0LgogICAgI1RoZSBmaWVsZHMgdGhhdCBmZWVkIHRoZXNl"
    "IGxpbmVzIGFyZSBjYXBwZWQgaW5kaXZpZHVhbGx5LCBzbyB0aGlzIHNob3VsZAogICAgI25ldmVy"
    "IGZpcmU7IGl0IGV4aXN0cyBiZWNhdXNlICJuZXZlciIgd2FzIGFsc28gdHJ1ZSBvZiB0aGUgZmll"
    "bGRzIHRoYXQKICAgICN0dXJuZWQgb3V0IHRvIGJlIHVuYm91bmRlZCwgYW5kIGJlY2F1c2UgdGhl"
    "IGZhaWx1cmUgaXQgZ3VhcmRzIGFnYWluc3QKICAgICNzdXJmYWNlcyBhcyBhbm90aGVyIHBsYXll"
    "cidzIGdhbWUgbG9ja2luZyB1cCBzb2xpZCwgd2l0aCBub3RoaW5nIGluIHRoZSBsb2cKICAgICNw"
    "b2ludGluZyBiYWNrIGhlcmUuIExvZ2dlZCByYXRoZXIgdGhhbiB0cnVuY2F0ZWQ6IGhhbGYgYSBj"
    "b21tYW5kIGlzIGEKICAgICNwcm90b2NvbCBkZXN5bmMsIHdoaWNoIGlzIG5vdCBhbiBpbXByb3Zl"
    "bWVudCBvbiBhIGxvbmcgb25lLgogICAgaWYgbGVuKG1zZykgPiBfTUFYX1dJUkVfTElORToKICAg"
    "ICAgICBwcmludChmJ1tMb2JieV0gV0FSTklORzoge2xlbihtc2cpfS1ieXRlIGxpbmUgZXhjZWVk"
    "cyB0aGUge19NQVhfV0lSRV9MSU5FfSAnCiAgICAgICAgICAgICAgZidieXRlIGxpbWl0IGFuZCBt"
    "YXkgZGVzdGFiaWxpc2UgdGhlIGNsaWVudDoge21zZ1s6MTIwXSFyfS4uLicpCiAgICByZXR1cm4g"
    "d2lyZV9lbmNvZGUobXNnKStfTgpkZWYgX2RlY29tcHJlc3NfYm91bmRlZChkYXRhLCBsaW1pdCk6"
    "CiAgICAjemxpYi5kZWNvbXByZXNzKCkgd2l0aCBubyBjYXAgdHVybnMgYSBzbWFsbCBjb21wcmVz"
    "c2VkIHBhY2tldCBpbnRvIGFuCiAgICAjYXJiaXRyYXJpbHkgbGFyZ2UgYWxsb2NhdGlvbiAoemlw"
    "IGJvbWIpLiBtYXhfbGVuZ3RoIHN0b3BzIGF0IHRoZSBjYXAsIGFuZAogICAgI2Egbm9uLWVtcHR5"
    "IHVuY29uc3VtZWRfdGFpbCB0ZWxscyB1cyB0aGUgcmVhbCBwYXlsb2FkIHdhcyBiaWdnZXIuCiAg"
    "ICBkY21wID0gemxpYi5kZWNvbXByZXNzb2JqKCkKICAgIG91dCA9IGRjbXAuZGVjb21wcmVzcyhk"
    "YXRhLCBsaW1pdCkKICAgIGlmIGRjbXAudW5jb25zdW1lZF90YWlsOgogICAgICAgIHJhaXNlIFBy"
    "b3RvY29sRXJyb3IoZidkZWNvbXByZXNzZWQgcGF5bG9hZCBleGNlZWRzIHtsaW1pdH0gYnl0ZXMn"
    "KQogICAgcmV0dXJuIG91dApjbGFzcyBQcm90b2NvbEVycm9yKEV4Y2VwdGlvbik6CiAgICAjQ2xp"
    "ZW50IHNlbnQgc29tZXRoaW5nIG1hbGZvcm1lZCBvciBvdXQgb2YgcmFuZ2UuIE5vdCBhIHNlcnZl"
    "ciBmYXVsdDogdGhlCiAgICAjY29ubmVjdGlvbiBpcyBkcm9wcGVkIHdpdGggYSBvbmUtbGluZSBs"
    "b2cgaW5zdGVhZCBvZiBhIHRyYWNlYmFjay4KICAgIHBhc3MKX1JFX1ZBTElEX1VTRVJOQU1FID0g"
    "cmUuY29tcGlsZShyJ15bQS1aYS16MC05X1wtXXszLDMyfSQnKQpkZWYgc2FuaXRpemVUZXh0KHRl"
    "eHQsIG1heGxlbj1Ob25lKToKICAgICNzdHJpcCBjaGFyYWN0ZXJzIHRoYXQgd291bGQgYnJlYWsg"
    "dGhlIHF1b3RlZC1zdHJpbmcgYmFzZWQgbG9iYnkgcHJvdG9jb2wKICAgICNvciBhbGxvdyBhIGNs"
    "aWVudCB0byBmb3JnZSBhZGRpdGlvbmFsIHByb3RvY29sIGZpZWxkcyAocHJvdG9jb2wgaW5qZWN0"
    "aW9uKQogICAgI21heGxlbiBjYXBzIHRoZSBmaWVsZCdzIGNvbnRyaWJ1dGlvbiB0byB0aGUgbGlu"
    "ZSBpdCBlbmRzIHVwIGluLiBMZW5ndGggaXMKICAgICNhIHNhZmV0eSBwcm9wZXJ0eSBoZXJlLCBu"
    "b3QgY29zbWV0aWNzOiBzZWUgX01BWF9XSVJFX0xJTkUuCiAgICBpZiB0ZXh0IGlzIE5vbmU6CiAg"
    "ICAgICAgcmV0dXJuICcnCiAgICB0ZXh0ID0gdGV4dC5yZXBsYWNlKCciJywgIiciKS5yZXBsYWNl"
    "KCdcMCcsICcnKS5yZXBsYWNlKCdccicsICcnKS5yZXBsYWNlKCdcbicsICcgJykKICAgIGlmIG1h"
    "eGxlbiBpcyBub3QgTm9uZSBhbmQgbGVuKHRleHQpID4gbWF4bGVuOgogICAgICAgIHRleHQgPSB0"
    "ZXh0WzptYXhsZW5dCiAgICByZXR1cm4gdGV4dApkZWYganNvblRpbWUoZHQpOgogICAgaWYgbm90"
    "IGR0LnV0Y29mZnNldCgpOgogICAgICAgIHR6aW5mbyA9IGRhdGV0aW1lLmRhdGV0aW1lLm5vdyhk"
    "YXRldGltZS50aW1lem9uZS51dGMpLmFzdGltZXpvbmUoKS50emluZm8KICAgICAgICBkdCA9IGR0"
    "LnJlcGxhY2UodHppbmZvPXR6aW5mbykKICAgIGR0ID0gZHQuYXN0aW1lem9uZShkYXRldGltZS50"
    "aW1lem9uZS51dGMpLnJlcGxhY2UodHppbmZvPU5vbmUpCiAgICByZXR1cm4gZHQuaXNvZm9ybWF0"
    "KCkgKyAiWiIKICAgICNzaG91bGQgcmV0dXJuIDIwMTItMDQtMjNUMTg6MjU6NDMuNTExWiB1dGMg"
    "dGltZSBmb3IgamF2YXNjcmlwdCBwYXJzaW5nCgojIyBNQUlOIFNFUlZFUiBDT0RFCgpSRUNWX0JV"
    "Rl9MRU4gPSAyKioxMgoKX1ZFUlNJT04gPSAnMC4zLjAnCnByaW50KGYnU2VydmVyIHZlcmlzaW9u"
    "IHtfVkVSU0lPTn0nKQpfREVCVUdfQUxMT1dfQU5ZX0xPR0lOID0gRmFsc2UgI2RvZXMgbm90IHZl"
    "cmlmeSBsb2dpbnMsIGZvciBkZWJ1ZyByZWFzb25zCl9UV19MT0JCWV9QT1JUID0gMTcxNzEKX0FV"
    "VE9fUkVHSVNURVIgPSBUcnVlCiNVcHBlciBib3VuZCBmb3IgYSBzaW5nbGUgbGVuZ3RoLXByZWZp"
    "eGVkIGJsb2IgZnJvbSBhIGNsaWVudCAocGxheWVyZGF0YSwKI2hlcm9kYXRhLCBnYW1lLWNvbW1h"
    "bmQgcGF5bG9hZCkuIEdlbmVyb3VzIGNvbXBhcmVkIHRvIGEgcmVhbCBzYXZlLCBidXQgZmluaXRl"
    "Ogojd2l0aG91dCBpdCBhIGNsaWVudCBjb3VsZCBhbm5vdW5jZSBhbiBhcmJpdHJhcnkgbGVuZ3Ro"
    "IGFuZCBtYWtlIHRoZSBzZXJ2ZXIKI2J1ZmZlciB1bnRpbCBpdCByYW4gb3V0IG9mIG1lbW9yeS4K"
    "X01BWF9CTE9CID0gMTYgKiAxMDI0ICogMTAyNAojVGlnaHRlciBjZWlsaW5nIGZvciB0aGUgb25l"
    "IGJsb2IgdGhhdCBpcyByZS1zZW50IHRvIGV2ZXJ5IG90aGVyIHBsYXllciBpbiB0aGUKI3Rvd24g"
    "cmF0aGVyIHRoYW4ganVzdCBzdG9yZWQgLSBzZWUgX3NldHVzZXJoZXJvZGF0YS4KX01BWF9IRVJP"
    "REFUQSA9IDEwMjQgKiAxMDI0CiNIYW5kc2hha2UvbG9naW4gcGFja2V0cyBhcmUgYSBmZXcgaHVu"
    "ZHJlZCBieXRlcyBpbiBwcmFjdGljZS4gVGhlc2UgYm91bmRzCiNhcHBseSAqYmVmb3JlKiBhdXRo"
    "ZW50aWNhdGlvbiwgd2hlcmUgYW55b25lIHdobyBjYW4gcmVhY2ggdGhlIHBvcnQgY2FuIHNlbmQK"
    "I3doYXRldmVyIHRoZXkgbGlrZSwgc28gdGhleSBhcmUgZGVsaWJlcmF0ZWx5IHRpZ2h0LgpfTUFY"
    "X0hBTkRTSEFLRSA9IDY0ICogMTAyNApfTUFYX0hBTkRTSEFLRV9JTkZMQVRFRCA9IDEwMjQgKiAx"
    "MDI0CgojLS0tIHN5bmNocm9uaXNhdGlvbiB0dW5pbmcgLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQojSG93IG9mdGVuIHRoZSBhY2N1bXVsYXRlZCBoZXJv"
    "IHBvc2l0aW9ucyBpbiBhIHRvd24gYXJlIHB1c2hlZCB0byBldmVyeW9uZSBpbgojaXQuIFRoaXMg"
    "dXNlZCB0byBiZSBwaW5uZWQgdG8gdGhlIDFzIHNvY2tldHNlcnZlciBwb2xsIGludGVydmFsLCB3"
    "aGljaCBpcyB3aGF0CiNtYWRlIG90aGVyIHBsYXllcnMnIG1hcCBtYXJrZXJzIGp1bXAgYSBmdWxs"
    "IHNlY29uZCBhdCBhIHRpbWUuIEVhY2ggdGljayBzZW5kcwojb25lIHBhY2tldCBwZXIgdG93biBh"
    "bmQgb25seSBpZiBzb21lYm9keSBhY3R1YWxseSBtb3ZlZCwgc28gZXZlbiBhdCB0aGlzIHJhdGUK"
    "I2l0J3MgYSBoYW5kZnVsIG9mIHNtYWxsIHBhY2tldHMvc2VjIGZvciBhIGNvLW9wLXNpemVkIGdy"
    "b3VwIC0gbmVnbGlnaWJsZQojYmFuZHdpZHRoIGVpdGhlciBvbiBMQU4gb3Igb3ZlciBhIGhvbWUg"
    "aW50ZXJuZXQgY29ubmVjdGlvbiAtIHdoaWxlIGdldHRpbmcKI25vdGljZWFibHkgY2xvc2VyIHRv"
    "IHNtb290aCBtb3Rpb24gdGhhbiB0aGUgb2xkIDFIeiBiYXNlbGluZS4KX1BPU19VUERBVEVfSFog"
    "PSAxMC4wCl9QT1NfVVBEQVRFX0haX01BWCA9IDIwLjAKI0Ryb3AgYSBjb25uZWN0aW9uIHRoYXQg"
    "aGFzIG5vdCBzZW50IGEgc2luZ2xlIGJ5dGUgaW4gdGhpcyBsb25nLiBBIHBsYXllciB3aG9zZQoj"
    "bGluayBkaWVzIHdpdGhvdXQgYSBjbGVhbiBUQ1AgY2xvc2Ugb3RoZXJ3aXNlIGtlZXBzIHRoZWly"
    "IHVzZXJuYW1lIGNsYWltZWQKI2ZvcmV2ZXIsIGFuZCB0aGVpciBuZXh0IGxvZ2luIGF0dGVtcHQg"
    "aXMgcmVqZWN0ZWQgd2l0aCAnQWNjb3VudCBhbHJlYWR5IGxvZ2dlZAojaW4nIHVudGlsIHRoZSBz"
    "ZXJ2ZXIgaXMgcmVzdGFydGVkLiAwIGRpc2FibGVzLgpfSURMRV9USU1FT1VUID0gMzAwCiNCbG9j"
    "a2luZyByZWN2KCkgdGltZW91dCBpbiB0aGUgcmVhZCBsb29wLiBPbmx5IGdvdmVybnMgaG93IHF1"
    "aWNrbHkgYSB0aHJlYWQKI25vdGljZXMgc2VydmVyIHNodXRkb3duIGFuZCB0aGUgaWRsZSBkZWFk"
    "bGluZTsgb3V0Ym91bmQgbGF0ZW5jeSBubyBsb25nZXIKI2RlcGVuZHMgb24gaXQgbm93IHRoYXQg"
    "ZWFjaCBjb25uZWN0aW9uIGhhcyBpdHMgb3duIHdyaXRlciB0aHJlYWQuCl9SRUFEX1RJTUVPVVQg"
    "PSAxLjAKI0hvdyBsb25nIGEgY2xpZW50IGdldHMgdG8gZmluaXNoIGRlbGl2ZXJpbmcgYSBibG9i"
    "IGl0IGhhcyBhbHJlYWR5IGFubm91bmNlZAojdGhlIGxlbmd0aCBvZi4gR2VuZXJvdXMgZm9yIGEg"
    "bGFyZ2Ugc2F2ZSBvdmVyIGEgc2xvdyBsaW5rLCBidXQgZmluaXRlIC0gc2VlCiNfUmVhZEJsb2Iu"
    "Cl9CTE9CX1RJTUVPVVQgPSA2MC4wCiNUaGUgbG9iYnkgb25seSBicm9rZXJzIHRoZSBjby1vcCBz"
    "ZXNzaW9uOyB0aGUgc2Vzc2lvbiBpdHNlbGYgaXMgYSBkaXJlY3QKI0RpcmVjdFBsYXkgY29ubmVj"
    "dGlvbiBmcm9tIHRoZSBqb2luaW5nIHBsYXllciB0byB0aGUgaG9zdCwgYXQgdGhlIGFkZHJlc3Mg"
    "dGhlCiNob3N0IHB1dHMgaW4gdGhlIHgtZGlyZWN0cGxheSBVUkwgb2YgaXRzIC9jcmVhdGVnYW1l"
    "LiBUaGUgaG9zdCdzIG93biBjbGllbnQKI2ZpbGxzIHRoYXQgaW4gZnJvbSBpdHMgbG9jYWwgYWRh"
    "cHRlciwgc28gYmVoaW5kIGEgcm91dGVyIGl0IGFkdmVydGlzZXMKI3NvbWV0aGluZyBsaWtlIDE5"
    "Mi4xNjguMC4xMCAtIHVucmVhY2hhYmxlIGZvciBhbnlvbmUgbm90IG9uIHRoYXQgTEFOLCBhbmQg"
    "dGhlCiNqb2luZXIgc2l0cyBvbiAiY29ubmVjdGluZyIgdW50aWwgaXQgZ2l2ZXMgdXAuIEV2ZXJ5"
    "dGhpbmcgdGhhdCBnb2VzIHRocm91Z2gKI3RoZSBsb2JieSAodG93biwgY2hhdCwgc2VlaW5nIGVh"
    "Y2ggb3RoZXIgbW92ZSkga2VlcHMgd29ya2luZywgd2hpY2ggaXMgd2hhdAojbWFrZXMgdGhpcyBs"
    "b29rIGxpa2UgYSByb29tLXNwZWNpZmljIGJ1Zy4KI1RoZSBzZXJ2ZXIgYWxyZWFkeSBrbm93cyBh"
    "biBhZGRyZXNzIGZvciB0aGUgaG9zdCB0aGF0IGV2ZXJ5IG90aGVyIGNsaWVudCBjYW4KI3JlYWNo"
    "OiB0aGUgc291cmNlIGFkZHJlc3Mgb2YgdGhlIGhvc3QncyBvd24gY29ubmVjdGlvbiB0byB1cy4g"
    "U3Vic3RpdHV0aW5nIGl0CiNpcyB3aGF0IG1ha2VzIGNyb3NzLWludGVybmV0IGNvLW9wIHdvcmsg"
    "YXQgYWxsLgojVHVybiBvZmYgKENvbmZpZy5pbmk6IFJld3JpdGVHYW1lSG9zdCA9IEZhbHNlKSBp"
    "ZiBldmVyeSBwbGF5ZXIgaXMgb24gdGhlIHNhbWUKI0xBTiBhcyB0aGUgaG9zdCBidXQgdGhlIGxv"
    "YmJ5IGlzIG5vdCAtIHRoZW4gdGhlIGhvc3QncyBvd24gTEFOIGFkZHJlc3MgaXMgdGhlCiNjb3Jy"
    "ZWN0IG9uZSBhbmQgb3VycyBpcyBub3QuCl9SRVdSSVRFX0dBTUVfSE9TVCA9IFRydWUKI0V4cGxp"
    "Y2l0IHB1YmxpYyBhZGRyZXNzIG9mIHRoZSBtYWNoaW5lIHRoYXQgaG9zdHMgcm9vbXMsIGZvciB0"
    "aGUgY2FzZSB0aGUKI3NlcnZlciBjYW5ub3Qgd29yayBpdCBvdXQgKHNlZSBfcHVibGljQWRkcmVz"
    "cykuIFNldCBpdCBpbiBDb25maWcuaW5pIGFzCiNQdWJsaWNIb3N0QWRkcmVzcyBpZiBhdXRvLWRl"
    "dGVjdGlvbiBwaWNrcyB0aGUgd3Jvbmcgb25lLgpfUFVCTElDX0hPU1RfQUREUkVTUyA9ICcnCiNU"
    "aGUgZ2FtZSBhcHBlbmRzIGEgcHJvcHJpZXRhcnkgJ2FsdD0nIGZpZWxkIHRvIHRoZSBEaXJlY3RQ"
    "bGF5IFVSTCBob2xkaW5nCiNldmVyeSBhZGRyZXNzIG9mIGV2ZXJ5IGFkYXB0ZXIgdGhlIGhvc3Qg"
    "aGFzOiBvYnNlcnZlZCBpbiB0aGUgd2lsZCBpdCBjYXJyaWVkCiNhIFRlcmVkbyAyMDAxOjA6Oi8z"
    "MiBhZGRyZXNzLCBhbiBmZTgwOjogbGluay1sb2NhbCBvbmUgYW5kIHRoZSBob3N0J3MgTEFOCiNJ"
    "UHY0IC0gbm9uZSBvZiB0aGVtIHJlYWNoYWJsZSBmcm9tIGFub3RoZXIgbmV0d29yay4gQSBqb2lu"
    "ZXIgdGhhdCB3b3JrcwojdGhyb3VnaCB0aGF0IGNhbmRpZGF0ZSBsaXN0IHdhaXRzIG91dCBhIGNv"
    "bm5lY3Rpb24gdGltZW91dCBvbiBlYWNoLCB3aGljaAojbG9va3MgZXhhY3RseSBsaWtlICJjb25u"
    "ZWN0aW5nIGZvcmV2ZXIiLiBEcm9wcGluZyB0aGUgZmllbGQgbGVhdmVzIHRoZSBzaW5nbGUKI2Fk"
    "ZHJlc3MgdGhpcyBzZXJ2ZXIga25vd3MgdG8gYmUgcmVhY2hhYmxlLgpfU1RSSVBfQUxUX0FERFJF"
    "U1NFUyA9IFRydWUKI0xvZyBldmVyeSBjb21tYW5kIHJlY2VpdmVkIGZyb20gY2xpZW50cywgd2l0"
    "aCBpdHMgcmF3IHRleHQuIFZlcmJvc2UsIGJ1dCB0aGlzCiNwcm90b2NvbCBpcyBvbmx5IHBhcnRp"
    "YWxseSBkb2N1bWVudGVkIGFuZCBpdCBpcyB0aGUgb25seSB3YXkgdG8gc2VlIHdoYXQgdGhlCiNj"
    "bGllbnQgYWN0dWFsbHkgYXNrcyBmb3Igd2hlbiBhIGZlYXR1cmUgZG9lcyBub3RoaW5nLgpfREVC"
    "VUdfTE9HX0NPTU1BTkRTID0gVHJ1ZQojL3VwZGhlcm9wb3MgYW5kIC9ub3AgYXJyaXZlIH4xMCB0"
    "aW1lcyBhIHNlY29uZCBwZXIgcGxheWVyIGFuZCBzYXkgbm90aGluZwojdXNlZnVsLiBMb2dnaW5n"
    "IHRoZW0gY29zdCB0d28gZm9ybWF0dGVkIGxpbmVzLCBhIHF1ZXVlIHB1dCwgYSBHVUkgaW5zZXJ0"
    "IGFuZAojYSBkaXNrIHdyaXRlICppbnNpZGUgdGhlIGNvbW1hbmQgaGFuZGxlciosIG9uIHRoZSBv"
    "bmUgcGF0aCB0aGF0IGhhcyB0byBzdGF5CiNxdWljayAtIHNlbGYtaW5mbGljdGVkIGxhdGVuY3kg"
    "YW5kIGppdHRlciBvbiBleGFjdGx5IHRoZSB0cmFmZmljIGJlaW5nCiNkZWJ1Z2dlZCwgcGx1cyBh"
    "IGxvZyBzbyBub2lzeSB0aGF0IHRoZSBpbnRlcmVzdGluZyBsaW5lcyBzY3JvbGwgYXdheS4gU2V0"
    "CiNEZWJ1Z0NvbW1hbmRzVmVyYm9zZSA9IFRydWUgaW4gQ29uZmlnLmluaSB0byBzZWUgdGhlbSBh"
    "bnl3YXkuCl9ERUJVR19MT0dfVkVSQk9TRSA9IEZhbHNlCl9RVUlFVF9DT01NQU5EUyA9IGZyb3pl"
    "bnNldCgoJy91cGRoZXJvcG9zJywgJy9ub3AnKSkKI0NvbnNlcnZhdGl2ZSBjYXAgb24gYSBzaW5n"
    "bGUgZ2VuZXJhdGVkIGNvbW1hbmQgbGluZS4gTm90aGluZyB0aGUgcmV0YWlsCiNjbGllbnQgc2Vu"
    "ZHMgY29tZXMgY2xvc2UgdG8gdGhpcywgc28gaXQgaXMgd2VsbCBpbnNpZGUgd2hhdGV2ZXIgdGhl"
    "IGNsaWVudAojaXRzZWxmIGlzIGJ1aWx0IHRvIGhhbmRsZS4KX01BWF9XSVJFX0xJTkUgPSA5MDAK"
    "I1Blci1maWVsZCBjYXBzLCBzbyBubyBjb21iaW5hdGlvbiBvZiBzdG9yZWQgb3IgdHlwZWQgdGV4"
    "dCBjYW4gYWRkIHVwIHRvIGEgbGluZQojb3ZlciB0aGF0IGxpbWl0LiBFdmVyeSBvbmUgb2YgdGhl"
    "c2UgZmllbGRzIGlzIHBsYXllci1jb250cm9sbGVkIGFuZCB0cmF2ZWxzIHRvCiMqb3RoZXIqIHBs"
    "YXllcnMnIGNsaWVudHM6CiMgLSBjaGF0IHRleHQgYW5kIHRoZSByb29tIG5hbWUgYXJlIHR5cGVk"
    "IHN0cmFpZ2h0IGluOwojIC0gZW1haWwvbG9jYXRpb24vZGVzY3JpcHRpb24gY29tZSBmcm9tIC91"
    "cGRhdGUgYW5kIGFyZSByZXBsYXllZCBieSAvd2hvaXMgdG8KIyAgIHdob2V2ZXIgYXNrcywgbG9u"
    "ZyBhZnRlciB0aGUgZmFjdCBhbmQgdG8gc29tZWJvZHkgd2hvIG5ldmVyIHR5cGVkIHRoZW0uCiNO"
    "b25lIG9mIHRoZW0gd2FzIGJvdW5kZWQsIHNvIG9uZSBsb25nIHZhbHVlIHdhcyBlbm91Z2ggdG8g"
    "aGFuZCBhbm90aGVyIHBsYXllcidzCiNjbGllbnQgYSBsaW5lIGxvbmdlciB0aGFuIGl0IGlzIGJ1"
    "aWx0IHRvIHBhcnNlIC0gd2hpY2ggaXMgbm90IGEgY29zbWV0aWMKI3Byb2JsZW0gaW4gYSAyMDA4"
    "IDMyLWJpdCBiaW5hcnksIGl0IGlzIGEgaGVhcCBvdmVyd3JpdGUgYW5kIGEgaGFyZCBsb2NrLXVw"
    "IG9uCiNhIG1hY2hpbmUgb3RoZXIgdGhhbiB0aGUgb25lIHRoYXQgY2F1c2VkIGl0LgpfTUFYX0NI"
    "QVRfVEVYVCA9IDI1NQpfTUFYX1dIT0lTX0ZJRUxEID0gNjQgICAgI2VtYWlsLCBsb2NhdGlvbgpf"
    "TUFYX0RFU0NSSVBUSU9OID0gMjU1Cl9NQVhfR0FNRU5BTUUgPSA2NApfTUFYX0NIQVROQU1FID0g"
    "NDgKI1BsYXllci1jcmVhdGVkIGNoYXQgY2hhbm5lbHMgYXJlIHBlciB0b3duIGFuZCBhcmUgbmV2"
    "ZXIgZ2FyYmFnZSBjb2xsZWN0ZWQsIHNvCiN0aGUgY291bnQgaXMgYm91bmRlZCByYXRoZXIgdGhh"
    "biBsZWZ0IHRvIHdob2V2ZXIgY2xpY2tzIGZhc3Rlc3QuIFdlbGwgYWJvdmUgdGhlCiN0d28gdGhl"
    "IGdhbWUgc2hpcHMgd2l0aC4KX01BWF9DSEFUX0NIQU5ORUxTID0gMTYKI1NlcnZlci1jb250cm9s"
    "bGVkIHRleHQgdGhhdCByZWFjaGVzIHRoZSBjbGllbnQ6IHRoZSB0aXRsZSBhbmQgdGhlIG1lc3Nh"
    "Z2Ugb2YKI3RoZSBkYXkgYXJlIHR5cGVkIGJ5IGFuIGFkbWluIGludG8gdGhlIEdVSSB3aXRoIG5v"
    "IGxlbmd0aCBsaW1pdCBhdCBhbGwsIGFuZAojYm90aCBhcmUgaGFuZGVkIHRvIHRoZSBjbGllbnQg"
    "YXQgbG9naW4sIGJlZm9yZSB0aGUgcGxheWVyIGNhbiBkbyBhbnl0aGluZwojYWJvdXQgaXQuIFRy"
    "dW5jYXRlIHJhdGhlciB0aGFuIHRydXN0LgpfTUFYX1RJVExFID0gMTI4Cl9NQVhfTU9URCA9IDEw"
    "MjQKI0hlcm8gaWRzIG9uIHRoZSB3aXJlOiBoZXggb3IgZGVjaW1hbC4KI0V2ZXJ5dGhpbmcgcG9z"
    "aXRpb25hbCBpbiB0aGlzIHByb3RvY29sIGlzIGhleCAtIHRoZSBjbGllbnQncyBvd24KIy91cGRo"
    "ZXJvcG9zIGNhcnJpZXMgY29vcmRpbmF0ZXMgYXMgIjM4QTQjMkIxNyIgLSBhbmQgdXBkYXRlUG9z"
    "KCkgaGFzIGFsd2F5cwojcHJlZml4ZWQgdGhlIGhlcm8gaWQgaW4gaGV4IHRvIG1hdGNoLiBCdXQg"
    "JGdhbWVjaGFubmVsdXNlciwgdGhlIG1lc3NhZ2UgdGhhdAojZmlyc3QgdGVsbHMgYSBjbGllbnQg"
    "d2hpY2ggaWQgYmVsb25ncyB0byB3aGljaCBwbGF5ZXIsIHNlbnQgdGhlIHNhbWUgaWQgaW4KI2Rl"
    "Y2ltYWwuIEEgY2xpZW50IHRoYXQgcmVhZHMgYm90aCBmaWVsZHMgd2l0aCBvbmUgcmFkaXggdGhl"
    "cmVmb3JlIGNhbm5vdAojbWF0Y2ggYSBwb3NpdGlvbiB1cGRhdGUgdG8gdGhlIHBsYXllciBpdCBi"
    "ZWxvbmdzIHRvLCBhbmQgdGhhdCBoZXJvIHN0b3BzCiNtb3Zpbmcgb24gZXZlcnlvbmUgZWxzZSdz"
    "IG1hcCB3aGlsZSB3YWxraW5nIG5vcm1hbGx5IG9uIHRoZWlyIG93bi4KI0xlZnQgYXMgYSBzd2l0"
    "Y2ggYmVjYXVzZSB3aGljaCByYWRpeCB0aGUgcmV0YWlsIGNsaWVudCB3YW50cyBpcyBub3QKI2Rv"
    "Y3VtZW50ZWQ6IGlmIGhleCB0dXJucyBvdXQgdG8gYmUgdGhlIHdyb25nIGd1ZXNzLCBzZXQgSGVy"
    "b0lkSGV4ID0gRmFsc2UgaW4KI0NvbmZpZy5pbmkgYW5kIGJvdGggbWVzc2FnZXMgZmFsbCBiYWNr"
    "IHRvIGRlY2ltYWwgLSBzdGlsbCBjb25zaXN0ZW50LCB3aGljaAojaXMgdGhlIHBhcnQgdGhhdCBh"
    "Y3R1YWxseSBtYXR0ZXJzLgpfSEVST19JRF9IRVggPSBUcnVlCiNPcHRpb25hbCBzZXJ2ZXItPmNs"
    "aWVudCAnL25vcCcgaGVhcnRiZWF0IGV2ZXJ5IDNzLiBNYWlubHkgdXNlZnVsIHRvIHN0b3AgaG9t"
    "ZQojcm91dGVycyBkcm9wcGluZyB0aGUgTkFUIG1hcHBpbmcgb2YgYW4gaWRsZSBjby1vcCBzZXNz"
    "aW9uLiBPZmYgYnkgZGVmYXVsdDogdGhlCiNyZWFsIGNsaWVudCdzIHJlYWN0aW9uIHRvIGFuIHVu"
    "c29saWNpdGVkIC9ub3AgaGFzIG5vdCBiZWVuIHZlcmlmaWVkLgpfU0VORF9OT1BTID0gRmFsc2UK"
    "I0hlcm8gaWRzIGFyZSBkcmF3biBmcm9tIDEuLl9NQVhfSEVST19JRCBhbmQgcmVsZWFzZWQgb24g"
    "ZGlzY29ubmVjdCAtIHNlZQojRGF0YUhhbmRsZXIuZ2V0VVJhbmRvbS4KX01BWF9IRVJPX0lEID0g"
    "MHg4MDAwCiNJbi1nYW1lIGFkbWluIGNvbnNvbGUuIFByZWZpeCBhIGNoYXQgbGluZSB3aXRoIHRo"
    "aXMgdG8gYWRkcmVzcyB0aGUgc2VydmVyOyB0aGUKI2FjY291bnRzIGFsbG93ZWQgdG8gZG8gc28g"
    "YXJlIGxpc3RlZCBpbiBDb25maWcuaW5pIGFzIGEgY29tbWEtc2VwYXJhdGVkCiNBZG1pbnM9LiBF"
    "bXB0eSBieSBkZWZhdWx0LCB3aGljaCBkaXNhYmxlcyB0aGUgY29uc29sZSBvdXRyaWdodCAtIGEg"
    "c2VydmVyIHdpdGgKI25vIG5hbWVkIGFkbWlucyBoYXMgbm8gcHJpdmlsZWdlZCBjaGF0IGNvbW1h"
    "bmRzIGF0IGFsbC4KX0FETUlOX1BSRUZJWCA9ICchJwpfQURNSU5TID0gZnJvemVuc2V0KCkKCgpE"
    "RUZBVUxUX1RJVExFID0gJ0NvbW11bml0eSBNdWx0aXBsYXllciBTZXJ2ZXInCkRFRkFVTFRfTU9U"
    "RCA9IGYnPDB4RkYwMDAwRkY+PEYyPkNvbW11bml0eSBNdWx0aXBsYXllciBTZXJ2ZXIgVmVyc2lv"
    "biB7X1ZFUlNJT059PGJyZWFrPTEwLjA+XHJcbicKCiNSb290IG5leHQgdG8gdGhpcyBzY3JpcHQg"
    "cmF0aGVyIHRoYW4gdGhlIHByb2Nlc3MnIGN1cnJlbnQgd29ya2luZyBkaXJlY3RvcnksCiNzbyB0"
    "aGUgZGF0YWJhc2UvY29uZmlnL3BsYXllcmRhdGEgYWx3YXlzIGxpdmUgaW4gdGhlIHNhbWUgcGxh"
    "Y2Ugd2hldGhlciB0aGUKI3NlcnZlciBpcyBkb3VibGUtY2xpY2tlZCwgbGF1bmNoZWQgZnJvbSBh"
    "IHRlcm1pbmFsIGVsc2V3aGVyZSwgb3IgaW1wb3J0ZWQgYnkKI2EgR1VJIHdyYXBwZXIgKGUuZy4g"
    "VFcxIENvbnRyb2wgQ2VudGVyKS4KI0FsbG93cyBhbiBlbWJlZGRpbmcgaG9zdCAoZS5nLiBhIHBv"
    "cnRhYmxlIGFsbC1pbi1vbmUgbGF1bmNoZXIgdGhhdCBleGVjKClzCiN0aGlzIGZpbGUncyBzb3Vy"
    "Y2UgZnJvbSBtZW1vcnksIHdoZXJlIF9fZmlsZV9fIGlzIG1lYW5pbmdsZXNzKSB0byByZWRpcmVj"
    "dAojd2hlcmUgdGhlIGRhdGFiYXNlL2NvbmZpZy9wbGF5ZXJkYXRhIGxpdmUgYnkgcHJlLXNldHRp"
    "bmcgdGhpcyBuYW1lIGluIHRoZQojbW9kdWxlJ3MgZ2xvYmFscyBiZWZvcmUgdGhlIG1vZHVsZSBi"
    "b2R5IHJ1bnMuIFN0YW5kYWxvbmUgZXhlY3V0aW9uICh0aGUKI25vcm1hbCBgcHl0aG9uIFRXMUNT"
    "LnB5YCkgaXMgdW5hZmZlY3RlZDogZmFsbHMgYmFjayB0byBuZXh0IHRvIHRoaXMgc2NyaXB0Lgpp"
    "ZiAnX0VYVEVSTkFMX0RBVEFfRElSJyBpbiBnbG9iYWxzKCkgYW5kIGdsb2JhbHMoKVsnX0VYVEVS"
    "TkFMX0RBVEFfRElSJ106CiAgICBfUEFUSF9ST09UID0gZ2xvYmFscygpWydfRVhURVJOQUxfREFU"
    "QV9ESVInXQplbHNlOgogICAgX1BBVEhfUk9PVCA9IG9zLnBhdGguZGlybmFtZShvcy5wYXRoLmFi"
    "c3BhdGgoX19maWxlX18pKQpfUEFUSF9EQVRBQkFTRSA9IG9zLnBhdGguam9pbihfUEFUSF9ST09U"
    "LCdTZXJ2ZXJEYXRhLmRiJykKX1BBVEhfQ09ORklHID0gb3MucGF0aC5qb2luKF9QQVRIX1JPT1Qs"
    "J0NvbmZpZy5pbmknKQpfUEFUSF9QTEFZRVJEQVRBID0gb3MucGF0aC5qb2luKF9QQVRIX1JPT1Qs"
    "J1BsYXllckRhdGEnKQoKZGVmIF9lc2NhcGVNT1REKG1vdGQpOgogICAgI2NvbmZpZ3BhcnNlciB2"
    "YWx1ZXMgY2FuJ3Qgc2FmZWx5IGhvbGQgcmF3IENSL0xGLCBzdG9yZSBhcyBcclxuIGVzY2FwZXMK"
    "ICAgIHJldHVybiBtb3RkLmVuY29kZSgndW5pY29kZV9lc2NhcGUnKS5kZWNvZGUoJ2FzY2lpJykK"
    "ZGVmIF91bmVzY2FwZU1PVEQobW90ZCk6CiAgICAjX2VzY2FwZU1PVEQgYWx3YXlzIHdyaXRlcyBw"
    "dXJlIGFzY2lpLCBidXQgYSBoYW5kLWVkaXRlZCBDb25maWcuaW5pIG1heSBob2xkCiAgICAjcmF3"
    "IDgtYml0IHRleHQ7IHRvbGVyYXRlIGl0IGluc3RlYWQgb2YgcmVmdXNpbmcgdG8gc3RhcnQgdGhl"
    "IHNlcnZlcgogICAgcmV0dXJuIG1vdGQuZW5jb2RlKF9XSVJFX0VOQywgJ3JlcGxhY2UnKS5kZWNv"
    "ZGUoJ3VuaWNvZGVfZXNjYXBlJykKX0NPTkZJR19ERUZBVUxUUyA9IHsKICAgICdTZXJ2ZXJOYW1l"
    "JzogREVGQVVMVF9USVRMRSwKICAgICdNT1REJzogX2VzY2FwZU1PVEQoREVGQVVMVF9NT1REKSwK"
    "ICAgICdQb3J0Jzogc3RyKF9UV19MT0JCWV9QT1JUKSwKICAgICdBdXRvUmVnaXN0ZXInOiBzdHIo"
    "X0FVVE9fUkVHSVNURVIpLAogICAgJ0FsbG93QW55TG9naW4nOiBzdHIoX0RFQlVHX0FMTE9XX0FO"
    "WV9MT0dJTiksCiAgICAnUG9zaXRpb25VcGRhdGVIeic6IHN0cihfUE9TX1VQREFURV9IWiksCiAg"
    "ICAnSWRsZVRpbWVvdXQnOiBzdHIoX0lETEVfVElNRU9VVCksCiAgICAnS2VlcGFsaXZlJzogc3Ry"
    "KF9TRU5EX05PUFMpLAogICAgJ1Jld3JpdGVHYW1lSG9zdCc6IHN0cihfUkVXUklURV9HQU1FX0hP"
    "U1QpLAogICAgJ1B1YmxpY0hvc3RBZGRyZXNzJzogX1BVQkxJQ19IT1NUX0FERFJFU1MsCiAgICAn"
    "U3RyaXBBbHRBZGRyZXNzZXMnOiBzdHIoX1NUUklQX0FMVF9BRERSRVNTRVMpLAogICAgJ0hlcm9J"
    "ZEhleCc6IHN0cihfSEVST19JRF9IRVgpLAogICAgJ0RlYnVnQ29tbWFuZHMnOiBzdHIoX0RFQlVH"
    "X0xPR19DT01NQU5EUyksCiAgICAnRGVidWdDb21tYW5kc1ZlcmJvc2UnOiBzdHIoX0RFQlVHX0xP"
    "R19WRVJCT1NFKSwKICAgICdBZG1pbnMnOiAnJywKICAgICdBZG1pblByZWZpeCc6IF9BRE1JTl9Q"
    "UkVGSVgsCn0KZGVmIGxvYWRDb25maWcoKToKICAgIGNmZyA9IGNvbmZpZ3BhcnNlci5Db25maWdQ"
    "YXJzZXIoKQogICAgY2ZnWydzZXJ2ZXInXSA9IGRpY3QoX0NPTkZJR19ERUZBVUxUUykKICAgIGlm"
    "IG9zLnBhdGguZXhpc3RzKF9QQVRIX0NPTkZJRyk6CiAgICAgICAgY2ZnLnJlYWQoX1BBVEhfQ09O"
    "RklHKQogICAgZWxzZToKICAgICAgICBzYXZlQ29uZmlnKGNmZykKICAgIHJldHVybiBjZmcKZGVm"
    "IHNhdmVDb25maWcoY2ZnKToKICAgIHdpdGggb3BlbihfUEFUSF9DT05GSUcsICd3JywgZW5jb2Rp"
    "bmc9J3V0Zi04JykgYXMgZjoKICAgICAgICBjZmcud3JpdGUoZikKZGVmIGFwcGx5Q29uZmlnKGNm"
    "Zyk6CiAgICAjQXBwbGllcyBjb25maWcgdmFsdWVzIHRvIHRoZSBsaXZlIG1vZHVsZSBnbG9iYWxz"
    "LiBTZXJ2ZXJOYW1lL01PVEQvCiAgICAjQXV0b1JlZ2lzdGVyIHRha2UgZWZmZWN0IGltbWVkaWF0"
    "ZWx5IChyZWFkIGZyZXNoIHBlciBsb2dpbiBhdHRlbXB0KTsKICAgICNQb3J0IG9ubHkgdGFrZXMg"
    "ZWZmZWN0IGZvciBzZXJ2ZXJzIHN0YXJ0ZWQgYWZ0ZXIgdGhpcyBjYWxsLgogICAgZ2xvYmFsIERF"
    "RkFVTFRfVElUTEUsIERFRkFVTFRfTU9URCwgX1RXX0xPQkJZX1BPUlQsIF9BVVRPX1JFR0lTVEVS"
    "LCBfREVCVUdfQUxMT1dfQU5ZX0xPR0lOCiAgICBnbG9iYWwgX1BPU19VUERBVEVfSFosIF9JRExF"
    "X1RJTUVPVVQsIF9TRU5EX05PUFMKICAgIGdsb2JhbCBfUkVXUklURV9HQU1FX0hPU1QsIF9ERUJV"
    "R19MT0dfQ09NTUFORFMsIF9ERUJVR19MT0dfVkVSQk9TRQogICAgZ2xvYmFsIF9QVUJMSUNfSE9T"
    "VF9BRERSRVNTLCBfU1RSSVBfQUxUX0FERFJFU1NFUywgX0hFUk9fSURfSEVYCiAgICBnbG9iYWwg"
    "X0FETUlOUywgX0FETUlOX1BSRUZJWAogICAgc2VjID0gY2ZnWydzZXJ2ZXInXQogICAgREVGQVVM"
    "VF9USVRMRSA9IHNlYy5nZXQoJ1NlcnZlck5hbWUnLCBmYWxsYmFjaz1ERUZBVUxUX1RJVExFKQog"
    "ICAgREVGQVVMVF9NT1REID0gX3VuZXNjYXBlTU9URChzZWMuZ2V0KCdNT1REJywgZmFsbGJhY2s9"
    "X2VzY2FwZU1PVEQoREVGQVVMVF9NT1REKSkpCiAgICBfVFdfTE9CQllfUE9SVCA9IHNlYy5nZXRp"
    "bnQoJ1BvcnQnLCBmYWxsYmFjaz1fVFdfTE9CQllfUE9SVCkKICAgIF9BVVRPX1JFR0lTVEVSID0g"
    "c2VjLmdldGJvb2xlYW4oJ0F1dG9SZWdpc3RlcicsIGZhbGxiYWNrPV9BVVRPX1JFR0lTVEVSKQog"
    "ICAgX0RFQlVHX0FMTE9XX0FOWV9MT0dJTiA9IHNlYy5nZXRib29sZWFuKCdBbGxvd0FueUxvZ2lu"
    "JywgZmFsbGJhY2s9X0RFQlVHX0FMTE9XX0FOWV9MT0dJTikKICAgICNDbGFtcGVkIHJhdGhlciB0"
    "aGFuIHRydXN0ZWQ6IHRoZXNlIGNvbWUgZnJvbSBhIGhhbmQtZWRpdGFibGUgaW5pLCBhbmQgYQog"
    "ICAgI3N0cmF5IDAgb3IgMTAwMDAgaGVyZSB3b3VsZCBlaXRoZXIgc3RvcCBwb3NpdGlvbiB1cGRh"
    "dGVzIGVudGlyZWx5IG9yIHNwaW4KICAgICN0aGUgdXBkYXRlIHRocmVhZCBmbGF0IG91dC4KICAg"
    "IGh6ID0gc2VjLmdldGZsb2F0KCdQb3NpdGlvblVwZGF0ZUh6JywgZmFsbGJhY2s9X1BPU19VUERB"
    "VEVfSFopCiAgICBfUE9TX1VQREFURV9IWiA9IG1pbihtYXgoaHosIDAuNSksIF9QT1NfVVBEQVRF"
    "X0haX01BWCkKICAgIF9JRExFX1RJTUVPVVQgPSBtYXgoMCwgc2VjLmdldGludCgnSWRsZVRpbWVv"
    "dXQnLCBmYWxsYmFjaz1fSURMRV9USU1FT1VUKSkKICAgIF9TRU5EX05PUFMgPSBzZWMuZ2V0Ym9v"
    "bGVhbignS2VlcGFsaXZlJywgZmFsbGJhY2s9X1NFTkRfTk9QUykKICAgIF9SRVdSSVRFX0dBTUVf"
    "SE9TVCA9IHNlYy5nZXRib29sZWFuKCdSZXdyaXRlR2FtZUhvc3QnLCBmYWxsYmFjaz1fUkVXUklU"
    "RV9HQU1FX0hPU1QpCiAgICBfUFVCTElDX0hPU1RfQUREUkVTUyA9IHNlYy5nZXQoJ1B1YmxpY0hv"
    "c3RBZGRyZXNzJywgZmFsbGJhY2s9X1BVQkxJQ19IT1NUX0FERFJFU1MpLnN0cmlwKCkKICAgIF9T"
    "VFJJUF9BTFRfQUREUkVTU0VTID0gc2VjLmdldGJvb2xlYW4oJ1N0cmlwQWx0QWRkcmVzc2VzJywg"
    "ZmFsbGJhY2s9X1NUUklQX0FMVF9BRERSRVNTRVMpCiAgICBfSEVST19JRF9IRVggPSBzZWMuZ2V0"
    "Ym9vbGVhbignSGVyb0lkSGV4JywgZmFsbGJhY2s9X0hFUk9fSURfSEVYKQogICAgX0RFQlVHX0xP"
    "R19DT01NQU5EUyA9IHNlYy5nZXRib29sZWFuKCdEZWJ1Z0NvbW1hbmRzJywgZmFsbGJhY2s9X0RF"
    "QlVHX0xPR19DT01NQU5EUykKICAgIF9ERUJVR19MT0dfVkVSQk9TRSA9IHNlYy5nZXRib29sZWFu"
    "KCdEZWJ1Z0NvbW1hbmRzVmVyYm9zZScsIGZhbGxiYWNrPV9ERUJVR19MT0dfVkVSQk9TRSkKICAg"
    "ICNDYXNlZm9sZGVkIG9uY2UgaGVyZSByYXRoZXIgdGhhbiBwZXIgbWVzc2FnZTogbmFtZXMgYXJl"
    "IGNvbXBhcmVkIGFnYWluc3QKICAgICN0aGlzIHNldCBvbiBldmVyeSBjaGF0IGxpbmUgdGhhdCBz"
    "dGFydHMgd2l0aCB0aGUgcHJlZml4LgogICAgX0FETUlOUyA9IGZyb3plbnNldChuLnN0cmlwKCku"
    "Y2FzZWZvbGQoKQogICAgICAgICAgICAgICAgICAgICAgICBmb3IgbiBpbiBzZWMuZ2V0KCdBZG1p"
    "bnMnLCBmYWxsYmFjaz0nJykuc3BsaXQoJywnKSBpZiBuLnN0cmlwKCkpCiAgICBfQURNSU5fUFJF"
    "RklYID0gc2VjLmdldCgnQWRtaW5QcmVmaXgnLCBmYWxsYmFjaz1fQURNSU5fUFJFRklYKS5zdHJp"
    "cCgpIG9yICchJwpDRkcgPSBsb2FkQ29uZmlnKCkKYXBwbHlDb25maWcoQ0ZHKQoKIyMjIFVTRVIg"
    "U1RSVUNUVVJFCiMgY29ubmVjdGlvbgojIHVzZXJuYW1lCiMgaGVyb2RhdGEKIyBwb3NpdGlvbgoj"
    "IGdhbWVjaGFubmVsCiMgY2hhdGNoYW5uZWwKIyBnYW1lCgpjbGFzcyBVc2VyKCk6ICNUT0RPIG1l"
    "cmdlIHVzZXIgaW50byBjb25uZWN0aW9uPywgdmFsaWRhdGlvbiBjYW4gYmUgYXNzdW1lZCBieSBz"
    "dGFnZQogICAgZGVmIF9faW5pdF9fKHNlbGYsIG5hbWUsIGNvbik6CiAgICAgICAgc2VsZi5oZXJv"
    "ZGF0YSA9IGInJwogICAgICAgICMnMCMwJywgbm90IE5vbmU6IHRoaXMgZ29lcyBzdHJhaWdodCBp"
    "bnRvIHRoZSAkZ2FtZWNoYW5uZWx1c2VyIHNlbnQgdG8KICAgICAgICAjZXZlcnkgb3RoZXIgY2xp"
    "ZW50LCBhbmQgYW4gdW5zZXQgdmFsdWUgdXNlZCB0byByZWFjaCB0aGVtIGFzIHRoZQogICAgICAg"
    "ICNsaXRlcmFsIHRleHQgIk5vbmUiIHdoZXJlIGNvb3JkaW5hdGVzIHdlcmUgZXhwZWN0ZWQuCiAg"
    "ICAgICAgc2VsZi5wb3NkYXRhID0gJzAjMCcKICAgICAgICBzZWxmLnBvc2NoYW5nZWQgPSBGYWxz"
    "ZQogICAgICAgIHNlbGYucmVxdWVzdGVkQ2hhbm5lbCA9IE5vbmUKICAgICAgICBzZWxmLmdhbWVj"
    "aGFubmVsID0gTm9uZQogICAgICAgIHNlbGYuY2hhdGNoYW5uZWwgPSBOb25lCiAgICAgICAgc2Vs"
    "Zi5yZXF1ZXN0ZWRHYW1lID0gTm9uZQogICAgICAgIHNlbGYuZ2FtZSA9IE5vbmUKICAgICAgICBz"
    "ZWxmLm5hbWUgPSBuYW1lCiAgICAgICAgI0NhY2hlZCwgbm90IGxvb2tlZCB1cCBwZXIgbWVzc2Fn"
    "ZTogdGhlIGd1aWxkIG5hbWUgZ29lcyBvdXQgaW4gdGhlCiAgICAgICAgI3NlY29uZCBmaWVsZCBv"
    "ZiBldmVyeSAkZ2FtZWNoYW5uZWx1c2VyIGFuZCAkY2hhdGNoYW5uZWx1c2VyIC0gdGhlCiAgICAg"
    "ICAgI3NhbWUgZmllbGQgL3dob2lzIHJlcG9ydHMgYXMgdGhlIGd1aWxkIC0gYW5kIHRob3NlIGFy"
    "ZSBzZW50IGZhciB0b28KICAgICAgICAjb2Z0ZW4gdG8gaGl0IHRoZSBkYXRhYmFzZSBlYWNoIHRp"
    "bWUuCiAgICAgICAgc2VsZi5ndWlsZCA9IHNhbml0aXplVGV4dChHREguZ2V0R3VpbGROYW1lKG5h"
    "bWUpKQogICAgICAgIHNlbGYubG9naW5UaW1lID0gZGF0ZXRpbWUuZGF0ZXRpbWUubm93KCkKICAg"
    "ICAgICBzZWxmLmlkbnVtID0gR0RILmdldFVSYW5kb20oKQogICAgICAgIHNlbGYuY29ubmVjdGlv"
    "biA9IGNvbiAjc2VydmVyID0gY29uLnNlcnZlcgogICAgICAgICNzZWxmLmNvbm5lY3Rpb24uZ3Vp"
    "ZCAtPiBndWlkIHdoZW4gcmVsZXZhbnQKICAgICAgICBzZWxmLnBndWlkID0gcHJldHR5X2d1aWQo"
    "c2VsZi5jb25uZWN0aW9uLmd1aWQpCiAgICBkZWYgbGVhdmVDaGFubmVsKHNlbGYpOgogICAgICAg"
    "IGlmIHNlbGYucmVxdWVzdGVkQ2hhbm5lbDoKICAgICAgICAgICAgI2xpc3QucmVtb3ZlKCkgcmFp"
    "c2VzIFZhbHVlRXJyb3Igd2hlbiB0aGUgZW50cnkgaXMgYWxyZWFkeSBnb25lOwogICAgICAgICAg"
    "ICAjdGhhdCB1c2VkIHRvIGFib3J0IHRoZSByZXN0IG9mIHRoZSBkaXNjb25uZWN0IGNsZWFudXAK"
    "ICAgICAgICAgICAgaWYgc2VsZi5jb25uZWN0aW9uIGluIHNlbGYucmVxdWVzdGVkQ2hhbm5lbC5y"
    "ZXF1ZXN0ZWQ6CiAgICAgICAgICAgICAgICBzZWxmLnJlcXVlc3RlZENoYW5uZWwucmVxdWVzdGVk"
    "LnJlbW92ZShzZWxmLmNvbm5lY3Rpb24pCiAgICAgICAgICAgIHNlbGYucmVxdWVzdGVkQ2hhbm5l"
    "bCA9IE5vbmUKICAgICAgICBpZiBzZWxmLmdhbWVjaGFubmVsOgogICAgICAgICAgICBzZWxmLmdh"
    "bWVjaGFubmVsLmxlYXZlQ2hhbm5lbChzZWxmLmNvbm5lY3Rpb24pCiAgICAgICAgICAgICNsZWF2"
    "ZUNoYW5uZWwgYWxzbyBsZWF2ZXMgY2hhdAogICAgZGVmIGxlYXZlQ2hhdChzZWxmKToKICAgICAg"
    "ICBpZiBzZWxmLmNoYXRjaGFubmVsOgogICAgICAgICAgICBpZiBzZWxmLmNvbm5lY3Rpb24gaW4g"
    "c2VsZi5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgICAgIHNlbGYuY2hhdGNoYW5uZWwucmVtb3Zl"
    "KHNlbGYuY29ubmVjdGlvbikKICAgICAgICAgICAgbGVhdmVtc2cgPSBfZW0oZicmY2hhdGNoYW5u"
    "ZWx1c2VyICJ7c2VsZi5uYW1lfSInKQogICAgICAgICAgICBzZWxmLmNvbm5lY3Rpb24uc2VydmVy"
    "LmRpc3QuYWRkKHsndGFyZ2V0JzpzZWxmLmNoYXRjaGFubmVsLCdtZXNzYWdlJzpsZWF2ZW1zZ30p"
    "CiAgICAgICAgICAgIHNlbGYuY2hhdGNoYW5uZWw9Tm9uZQogICAgZGVmIHN0b3BHYW1lKHNlbGYp"
    "OgogICAgICAgIGlmIHNlbGYucmVxdWVzdGVkR2FtZToKICAgICAgICAgICAgI0JvdGggZ3VhcmRz"
    "IG1hdHRlcjogdGhlIGNoYW5uZWwgbWF5IGFscmVhZHkgYmUgZ29uZSAobGVhdmVDaGFubmVsCiAg"
    "ICAgICAgICAgICNjbGVhcnMgaXQgYmVmb3JlIHN0b3BHYW1lIHJ1bnMgb24gc29tZSBwYXRocykg"
    "YW5kIHRoZSBwZW5kaW5nCiAgICAgICAgICAgICNyZXF1ZXN0IG1heSBhbHJlYWR5IGhhdmUgYmVl"
    "biBjb25zdW1lZCBieSBjcmVhdGVHYW1lLiBFaXRoZXIgb25lCiAgICAgICAgICAgICN1c2VkIHRv"
    "IHJhaXNlIChBdHRyaWJ1dGVFcnJvciBvbiBOb25lIC8gS2V5RXJyb3IpIGluc2lkZSB0aGUKICAg"
    "ICAgICAgICAgI2Rpc2Nvbm5lY3QgcGF0aCBhbmQgYWJvcnQgdGhlIHJlc3Qgb2YgdGhlIGNsZWFu"
    "dXAsIGxlYWtpbmcgdGhlCiAgICAgICAgICAgICNwbGF5ZXIncyBlbnRyeSBpbiBhY3RpdmVVc2Vy"
    "cy4KICAgICAgICAgICAgaWYgc2VsZi5nYW1lY2hhbm5lbDoKICAgICAgICAgICAgICAgIHNlbGYu"
    "Z2FtZWNoYW5uZWwuZ2FtZVJlcXVlc3RzLnBvcChzZWxmLnJlcXVlc3RlZEdhbWUsIE5vbmUpCiAg"
    "ICAgICAgICAgIHNlbGYucmVxdWVzdGVkR2FtZSA9IE5vbmUKICAgICAgICBpZiBzZWxmLmdhbWU6"
    "CiAgICAgICAgICAgIHNlbGYuZ2FtZS5yZW1vdmUoc2VsZi5jb25uZWN0aW9uKQogICAgZGVmIGRp"
    "c2Nvbm5lY3Qoc2VsZiwgc2VydmVyKToKICAgICAgICBzZWxmLnN0b3BHYW1lKCkKICAgICAgICBz"
    "ZWxmLmxlYXZlQ2hhbm5lbCgpCiAgICAgICAgc2VydmVyLnN0YXRlLnJlbGVhc2VVc2VyKHNlbGYu"
    "bmFtZSwgc2VsZi5jb25uZWN0aW9uKQogICAgICAgIEdESC5yZWxlYXNlVVJhbmRvbShzZWxmLmlk"
    "bnVtKQogICAgZGVmIHdpcmVJZChzZWxmKToKICAgICAgICAjVGhlIG9uZSBwbGFjZSB0aGUgaGVy"
    "byBpZCBpcyBmb3JtYXR0ZWQsIHNvICRnYW1lY2hhbm5lbHVzZXIgYW5kCiAgICAgICAgIy91cGRo"
    "ZXJvcG9zIGNhbiBuZXZlciBkaXNhZ3JlZSBhZ2FpbiAtIHNlZSBfSEVST19JRF9IRVguCiAgICAg"
    "ICAgcmV0dXJuIGYne3NlbGYuaWRudW06eH0nIGlmIF9IRVJPX0lEX0hFWCBlbHNlIGYne3NlbGYu"
    "aWRudW19JwogICAgZGVmIGdldEdDVW1zZyhzZWxmKToKICAgICAgICBoZGwgPSBsZW4oc2VsZi5o"
    "ZXJvZGF0YSkKICAgICAgICBpZiBoZGw9PTA6CiAgICAgICAgICAgIHJldHVybiBiJycKICAgICAg"
    "ICByZXR1cm4gX2VtKGYnJGdhbWVjaGFubmVsdXNlciAie3NlbGYubmFtZX0iICJ7c2VsZi5ndWls"
    "ZH0iICIxMDAiICJ7c2VsZi53aXJlSWQoKX0iICIwIiAie3NlbGYucGd1aWR9IiAie3NlbGYucG9z"
    "ZGF0YX0iICJ7aGRsfSInKStzZWxmLmhlcm9kYXRhCiAgICBkZWYgZ2V0Q0NVbXNnKHNlbGYpOgog"
    "ICAgICAgIHZiID0gMCAjb3IgMHhGRkZGRkZGRig0Mjk0OTY3Mjk1PSAtMSYzMmJpdD8pCiAgICAg"
    "ICAgcmV0dXJuIF9lbShmJyRjaGF0Y2hhbm5lbHVzZXIgIntzZWxmLm5hbWV9IiAie3NlbGYuZ3Vp"
    "bGR9IiAie3ZifSIgIntzZWxmLnBndWlkfSInKQogICAgICAgICMgJGNoYXRjaGFubmVsdXNlciAi"
    "e25hbWV9IiAiIiAiMCIgIntndWlkfSIKIyBpbmNyZWFzaW5nIG1heSBpbXByb3ZlIHNlY3VyaXR5"
    "IGF0IHRoZSBjb3N0IG9mIHBlcmZvcm1hbmNlCiMgb25seSB1cGRhdGVzIHdoZW4gdXNlciBsb2dz"
    "IGluIGFuZCBpcyBzdG9yZWQgYWxvbmdzaWRlIHNhbHQgaW4gZGF0YWJhc2UKX0hBU0hJVEVSID0g"
    "MTAwMDAwCmRlZiBfc2FsdF9oYXNoXyhwYXNzd29yZCwgc2FsdCwgaEl0cik6CiAgICAjdXRmLTgs"
    "IG5vdCBhc2NpaTogYSBwYXNzd29yZCB3aXRoIGFuIDgtYml0IGNoYXJhY3RlciB1c2VkIHRvIHJh"
    "aXNlIGhlcmUgYW5kCiAgICAjZHJvcCB0aGUgY29ubmVjdGlvbiBpbnN0ZWFkIG9mIGxvZ2dpbmcg"
    "dGhlIHBsYXllciBpbi4gUHVyZS1hc2NpaSBwYXNzd29yZHMKICAgICNlbmNvZGUgdG8gaWRlbnRp"
    "Y2FsIGJ5dGVzIHVuZGVyIGJvdGgsIHNvIG5vIHN0b3JlZCBoYXNoIGNoYW5nZXMuCiAgICByZXR1"
    "cm4gaGFzaGxpYi5wYmtkZjJfaG1hYygnc2hhMjU2JywgcGFzc3dvcmQuZW5jb2RlKCd1dGYtOCcp"
    "LCBzYWx0LCBoSXRyKQogICAgCiMjIyBTUUwgSU5GTwojIF9EQklORk86IFZFUlNJT04gMQojIHVz"
    "ZXJUYWJsZQojIC0gcm93aWQsIHVzZXJuYW1lLCBwYXNzSGFzaCwgc2VyaWFsLCB1bmlxdWVTYWx0"
    "LCBsYXN0TG9naW4sIGVtYWlsLCBsb2NhdGlvbiwgeWVhcm9mYmlydGgoZXN0aW1hdGUpLCBnZW5k"
    "ZXIsIGRlc2NyaXB0aW9uCiMgZm9ybVRhYmxlCiMgLSByb3dpZCwgZm9ybQojIyAtLS0tLS0tLS0t"
    "LS0tLS0tICMjCiMgVE9ETyBWRVJTSU9OIDI6IGd1aWxkcywgbGVhZGVyYm9hcmQsIGV0Yz8KCiNU"
    "T0RPIGNvbnZlcnQgZGF0YWJhc2UgdG8gc2luZ2xldGhyZWFkIGFjY2VzcyBmb3IgY29tcGF0aWJp"
    "bGl0eT8gdW5uZWNjZXNhcnk/CiNjbGFzcyBEYXRhUmVxdWVzdCh0aHJlYWRpbmcuRXZlbnQpOgoj"
    "ICAgZGF0YSA9IE5vbmUKIyAgIGRlZiBzZXQodmFsKToKIyAgICAgICBzZWxmLmRhdGE9dmFsCiMg"
    "ICAgICAgc3VwZXIoKS5zZXQoKQojICAgZGVmIHdhaXQoKToKIyAgICAgICBzdXBlcigpLndhaXQo"
    "KQojICAgICAgIHJldHVybiBzZWxmLmRhdGEKIyogZGF0YWJhc2UgdGhyZWFkOgojICAgX2RyUSA9"
    "IGRhdGEgcmVxdWVzdCBxdWV1ZSwgcHJvY2Vzc2VkIGluIGRhdGFiYXNlIHRocmVhZAojICAgZXh0"
    "ZXJuYWwgZnVuY3Rpb25zIGFkZCByZXF1ZXN0IGZvciBpbnRlcm5hbCBmdW5jdGlvbiBhbmQgcmV0"
    "dXJuIHJlcXVlc3QgdG8gYXdhaXQKIyAgIGRyb2JqIGluIHF1ZXVlID0gKGRyLCBmdGFyZ2V0LCAo"
    "YXJncykpLCBkci5zZXQoZnRhcmdldCgqYXJncykpCiNUT0RPIG9yZ2FuaXplIFNRTCBjb21tYW5k"
    "cz8gbWFrZSBpdCBtb3JlIGJlYXV0aWZ1bD8KX1NRTF9kYkluZm9FeGlzdHMgPSAnU0VMRUNUIG5h"
    "bWUgRlJPTSBzcWxpdGVfbWFzdGVyIFdIRVJFIG5hbWU9Il9EQklORk8iJwpfU1FMX2RiVmVyc2lv"
    "biA9ICdTRUxFQ1QgVkVSU0lPTiBGUk9NIF9EQklORk8nCl9TUUxJTklUX2RiSW5mb1RhYmxlID0g"
    "J0NSRUFURSBUQUJMRSBfREJJTkZPKFZFUlNJT04pJwpfREJDVVJWRVIgPSAyCl9TUUxJTklUX2Ri"
    "SW5mb1ZlcnNpb24gPSBmJ0lOU0VSVCBJTlRPIF9EQklORk8gVkFMVUVTICh7X0RCQ1VSVkVSfSkn"
    "Cl9TUUxVUERfZGJJbmZvVmVyc2lvbiA9IGYnVVBEQVRFIF9EQklORk8gU0VUIFZFUlNJT04gPSB7"
    "X0RCQ1VSVkVSfScKI3lvYiA9IHllYXIgb2YgYmlydGggKGVzdGltYXRlKQojZ2VuZGVyOiAwID0g"
    "TWFsZQpfU1FMSU5JVF9kYlVzZXJUYWJsZSA9ICdDUkVBVEUgVEFCTEUgdXNlclRhYmxlKHVzZXJu"
    "YW1lIFVOSVFVRSwgcGFzc0hhc2gsIHNlcmlhbCwgdW5pcXVlU2FsdCwgaGFzaEl0ZXIsIGxhc3RM"
    "b2dpbiBUSU1FU1RBTVAsIGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRlc2NyaXB0aW9u"
    "KScKX1NRTElOSVRfZGJGb3JtVGFibGUgPSAnQ1JFQVRFIFRBQkxFIGZvcm1UYWJsZShmb3JtIFVO"
    "SVFVRSknICN1c2luZyByb3dpZCBhcyBJRAojLS0tIGd1aWxkcyAoREIgdmVyc2lvbiAyKSAtLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0KI3Jhbms6IDIg"
    "PSBmb3VuZGVyL2xlYWRlciwgMSA9IG9mZmljZXIsIDAgPSBtZW1iZXIuIEEgcGxheWVyIGlzIGlu"
    "IGF0IG1vc3Qgb25lCiNndWlsZCwgd2hpY2ggaXMgd2hhdCB0aGUgY2xpZW50J3MgVUkgYXNzdW1l"
    "cyAod2hvaXMgY2FycmllcyBhIHNpbmdsZSBuYW1lKS4KI2d1aWxka2V5IGlzIGd1aWxkbmFtZS5j"
    "YXNlZm9sZCgpIGFuZCBpcyB3aGF0IHVuaXF1ZW5lc3MgYW5kIGV2ZXJ5IGxvb2t1cCBnbwojdGhy"
    "b3VnaC4gU1FMaXRlJ3Mgb3duIENPTExBVEUgTk9DQVNFIG9ubHkgZm9sZHMgQS1aLCBzbyBvbiB0"
    "aGlzIHNlcnZlciAtCiN3aGVyZSB0aGUgbmFtZXMgYXJlIEN5cmlsbGljIC0gaXQgd291bGQgaGF2"
    "ZSBsZXQgItCd0L7Rh9C90YvQtSDQktC+0LvQutC4IiBhbmQgItC90L7Rh9C90YvQtQoj0LLQvtC7"
    "0LrQuCIgY29leGlzdCBhcyB0d28gc2VwYXJhdGUgZ3VpbGRzIHRoYXQgcGxheWVycyBjb3VsZCBu"
    "b3QgdGVsbCBhcGFydC4KX1NRTElOSVRfZGJHdWlsZFRhYmxlID0gJ0NSRUFURSBUQUJMRSBndWls"
    "ZFRhYmxlKGd1aWxkbmFtZSwgZ3VpbGRrZXkgVU5JUVVFLCBvd25lciwgY3JlYXRlZCBUSU1FU1RB"
    "TVAsIGRlc2NyaXB0aW9uKScKX1NRTElOSVRfZGJHdWlsZE1lbWJlclRhYmxlID0gJ0NSRUFURSBU"
    "QUJMRSBndWlsZE1lbWJlclRhYmxlKGd1aWxkbmFtZSwgdXNlcm5hbWUgVU5JUVVFLCByYW5rKScK"
    "X1NRTF9ndWlsZEV4aXN0cyA9ICdTRUxFQ1QgZ3VpbGRuYW1lIEZST00gZ3VpbGRUYWJsZSBXSEVS"
    "RSBndWlsZGtleSA9ID8nCl9TUUxfY3JlYXRlR3VpbGQgPSAnSU5TRVJUIElOVE8gZ3VpbGRUYWJs"
    "ZSBWQUxVRVMgKD8sPyw/LD8sPyknCl9TUUxfZGVsZXRlR3VpbGQgPSAnREVMRVRFIEZST00gZ3Vp"
    "bGRUYWJsZSBXSEVSRSBndWlsZG5hbWUgPSA/JwpfU1FMX2d1aWxkT3duZXIgPSAnU0VMRUNUIG93"
    "bmVyIEZST00gZ3VpbGRUYWJsZSBXSEVSRSBndWlsZG5hbWUgPSA/JwpfU1FMX2FkZEd1aWxkTWVt"
    "YmVyID0gJ0lOU0VSVCBPUiBSRVBMQUNFIElOVE8gZ3VpbGRNZW1iZXJUYWJsZSBWQUxVRVMgKD8s"
    "Pyw/KScKX1NRTF9kZWxHdWlsZE1lbWJlciA9ICdERUxFVEUgRlJPTSBndWlsZE1lbWJlclRhYmxl"
    "IFdIRVJFIHVzZXJuYW1lID0gPycKX1NRTF9kZWxHdWlsZE1lbWJlcnMgPSAnREVMRVRFIEZST00g"
    "Z3VpbGRNZW1iZXJUYWJsZSBXSEVSRSBndWlsZG5hbWUgPSA/JwpfU1FMX2d1aWxkT2ZVc2VyID0g"
    "J1NFTEVDVCBndWlsZG5hbWUsIHJhbmsgRlJPTSBndWlsZE1lbWJlclRhYmxlIFdIRVJFIHVzZXJu"
    "YW1lID0gPycKX1NRTF9ndWlsZE1lbWJlcnMgPSAnU0VMRUNUIHVzZXJuYW1lLCByYW5rIEZST00g"
    "Z3VpbGRNZW1iZXJUYWJsZSBXSEVSRSBndWlsZG5hbWUgPSA/JwpfU1FMX2FsbEd1aWxkcyA9ICdT"
    "RUxFQ1QgZ3VpbGRuYW1lIEZST00gZ3VpbGRUYWJsZSBPUkRFUiBCWSBndWlsZG5hbWUgQ09MTEFU"
    "RSBOT0NBU0UnCiNTYW1lIHNoYXBlIGFzIHRoZSB1c2VybmFtZSBydWxlOiB0aGUgbmFtZSB0cmF2"
    "ZWxzIGluc2lkZSBxdW90ZWQgcHJvdG9jb2wKI2ZpZWxkcywgc28gYW55dGhpbmcgdGhhdCBjb3Vs"
    "ZCBjbG9zZSBhIHF1b3RlIGlzIHJlamVjdGVkIG91dHJpZ2h0IHJhdGhlciB0aGFuCiNzaWxlbnRs"
    "eSByZXdyaXR0ZW4uIFNwYWNlcyBhcmUgYWxsb3dlZCAtIGd1aWxkIG5hbWVzIGNvbW1vbmx5IGhh"
    "dmUgdGhlbS4KX1JFX1ZBTElEX0dVSUxETkFNRSA9IHJlLmNvbXBpbGUocideW14iXHJcblwwXXsz"
    "LDMyfSQnKQoKX1NRTF91c2VySUQgPSAnU0VMRUNUIHJvd2lkIEZST00gdXNlclRhYmxlIFdIRVJF"
    "IHVzZXJuYW1lID0gPycKX1NRTF91c2VySURfU2NoayA9ICdTRUxFQ1Qgcm93aWQgRlJPTSB1c2Vy"
    "VGFibGUgV0hFUkUgc2VyaWFsID0gPycKX1NRTF91c2VySURfc3RyaWN0ID0gJ1NFTEVDVCByb3dp"
    "ZCBGUk9NIHVzZXJUYWJsZSBXSEVSRSB1c2VybmFtZSA9ID8gQU5EIHNlcmlhbCA9ID8nCl9TUUxf"
    "cmVnaXN0ZXJVc2VyID0gJ0lOU0VSVCBJTlRPIHVzZXJUYWJsZSBWQUxVRVMgKD8sPyw/LD8sPyw/"
    "LD8sPyw/LD8sPyknCl9TUUxfZGVsZXRlVXNlciA9ICdERUxFVEUgRlJPTSB1c2VyVGFibGUgV0hF"
    "UkUgdXNlcm5hbWUgPSA/JwpfU1FMX2dldExvZ2luID0gJ1NFTEVDVCB1c2VybmFtZSwgcGFzc0hh"
    "c2gsIHVuaXF1ZVNhbHQsIGhhc2hJdGVyIEZST00gdXNlclRhYmxlIFdIRVJFIHJvd2lkID0gPycK"
    "X1NRTFVQRF9wYXNzSGFzaCA9ICdVUERBVEUgdXNlclRhYmxlIFNFVCBwYXNzSGFzaCA9ID8sIGhh"
    "c2hJdGVyID0gPyBXSEVSRSByb3dpZCA9ID8nCl9TUUxfbG9naW5VcGRhdGUgPSAnVVBEQVRFIHVz"
    "ZXJUYWJsZSBTRVQgbGFzdExvZ2luID0gPyBXSEVSRSByb3dpZCA9ID8nCl9TUUxfZ2V0V2hvaXMg"
    "PSAnU0VMRUNUIGVtYWlsLCBsb2NhdGlvbiwgeW9iLCBnZW5kZXIsIGRlc2NyaXB0aW9uIEZST00g"
    "dXNlclRhYmxlIFdIRVJFIHVzZXJuYW1lID0gPycKX1NRTFVQRF93aG9pcyA9ICdVUERBVEUgdXNl"
    "clRhYmxlIFNFVCBlbWFpbCA9ID8sIGxvY2F0aW9uID0gPywgeW9iID0gPywgZ2VuZGVyID0gPywg"
    "ZGVzY3JpcHRpb24gPSA/IFdIRVJFIHVzZXJuYW1lID0gPycKI2lmIGRvZXMgbm90IGV4aXN0LCBn"
    "ZW5lcmF0ZSwgY2hhbmdlIGZvcm1hdCBmb3IgbW9kcGFja3MKX1NRTF9mb3JtSUQgPSAnU0VMRUNU"
    "IHJvd2lkIGZyb20gZm9ybVRhYmxlIFdIRVJFIGZvcm0gPSA/JwpfU1FMQUREX2Zvcm1JRCA9ICdJ"
    "TlNFUlQgSU5UTyBmb3JtVGFibGUgVkFMVUVTICg/KScKX0ZPUk1fUERGaWxlID0gJ3s6eH1fezp4"
    "fS5iaW4nICMgcGxheWVyZGF0YVx1c2VySURfZm9ybUlELmJpbgoKZGVmIHJlYWRCaW4oZmlsZXBh"
    "dGgpOgogICAgd2l0aCBvcGVuKGZpbGVwYXRoLCAicmIiKSBhcyBmOgogICAgICAgIHJldHVybiBm"
    "LnJlYWQoKQpjbGFzcyBEYXRhSGFuZGxlcigpOgogICAgZGVmIF9faW5pdF9fKHNlbGYpOgogICAg"
    "ICAgICNpbnN0YW5jZSBhdHRyaWJ1dGUsIG5vdCBhIGNsYXNzIGF0dHJpYnV0ZSAtIHNhbWUgcmVh"
    "c29uaW5nIGFzCiAgICAgICAgI0dhbWVTdGF0ZS5hY3RpdmVVc2Vyczogc2hhcmVkIGNsYXNzIHN0"
    "YXRlIGxlYWtzIGJldHdlZW4gaW5zdGFuY2VzCiAgICAgICAgc2VsZi51c2VkTnVtcyA9IHNldCgp"
    "CiAgICAgICAgI3ByaW50KCdzcWxpdGUzIHRocmVhZHNhZmV0eTonLHNxbGl0ZTMudGhyZWFkc2Fm"
    "ZXR5KQogICAgICAgICNpZiBzcWxpdGUzLnRocmVhZHNhZmV0eTwzOgogICAgICAgICMgICAgcmFp"
    "c2UgRXhjZXB0aW9uKCdNdWx0aVRocmVhZCBzdXBwb3J0IHJlcXVpcmVkJykKICAgICAgICAjVE9E"
    "TyBvcmdhbml6ZSBzaW5nbGUgdGhyZWFkZWQgZGF0YWJhc2UgYWNjZXNzPyBldmVyIG5lZWRlZD8K"
    "ICAgICAgICBzZWxmLmxvY2sgPSB0aHJlYWRpbmcuUkxvY2soKQogICAgICAgIG9zLm1ha2VkaXJz"
    "KF9QQVRIX1BMQVlFUkRBVEEsIGV4aXN0X29rPVRydWUpCiAgICAgICAgc2VsZi5kYiA9IHNxbGl0"
    "ZTMuY29ubmVjdChfUEFUSF9EQVRBQkFTRSwKICAgICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgIGNoZWNrX3NhbWVfdGhyZWFkID0gRmFsc2UsCiAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICBkZXRlY3RfdHlwZXM9c3FsaXRlMy5QQVJTRV9ERUNMVFlQRVMgfAogICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgc3FsaXRlMy5QQVJTRV9DT0xOQU1FUykKICAgICAgICBp"
    "bml0Y3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgIGRiVW5pbml0aWFsaXplZCA9IGluaXRj"
    "dXIuZXhlY3V0ZShfU1FMX2RiSW5mb0V4aXN0cykuZmV0Y2hvbmUoKSBpcyBOb25lCiAgICAgICAg"
    "aWYgZGJVbmluaXRpYWxpemVkOgogICAgICAgICAgICBkYlZlclJlcyA9IDAKICAgICAgICBlbHNl"
    "OgogICAgICAgICAgICBkYlZlclJlcyA9IGluaXRjdXIuZXhlY3V0ZShfU1FMX2RiVmVyc2lvbiku"
    "ZmV0Y2hvbmUoKVswXQogICAgICAgIHNlbGYudXBkYXRlREJGcm9tKGRiVmVyUmVzKSAjZW5zdXJl"
    "IERCIGlzIHVwZGF0ZWQKICAgICAgICAKICAgICAgICBpbml0Y3VyLmNsb3NlKCkKICAgIGRlZiBn"
    "ZXRVUmFuZG9tKHNlbGYpOgogICAgICAgICNIZXJvIGlkcy4gVGhlIHByb2JlIHVzZWQgdG8gYmUg"
    "YSBiYXJlIGBybnVtICs9IDFgLCB3aGljaCB3YWxrcyBzdHJhaWdodAogICAgICAgICNwYXN0IHRo"
    "ZSB0b3Agb2YgdGhlIHJhbmdlIGluc3RlYWQgb2Ygd3JhcHBpbmcgLSBzbyBvbiBhIGJ1c3kgc2Vy"
    "dmVyIHRoZQogICAgICAgICNpZHMgaGFuZGVkIG91dCBkcmlmdCBhYm92ZSAweDgwMDAsIGFuZCBp"
    "ZiBldmVyeSBpZCB3ZXJlIGV2ZXIgdGFrZW4gdGhlCiAgICAgICAgI2xvb3Agd291bGQgbmV2ZXIg"
    "ZW5kLiBXcmFwIGluc2lkZSB0aGUgcmFuZ2UgYW5kIGdpdmUgdXAgYWZ0ZXIgYSBmdWxsCiAgICAg"
    "ICAgI3N3ZWVwIGluc3RlYWQgb2Ygc3Bpbm5pbmcgZm9yZXZlciBpbnNpZGUgdGhlIGxvY2suCiAg"
    "ICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJudW0gPSByYW5kb20ucmFuZGludCgx"
    "LCBfTUFYX0hFUk9fSUQpCiAgICAgICAgICAgIGZvciBfIGluIHJhbmdlKF9NQVhfSEVST19JRCk6"
    "CiAgICAgICAgICAgICAgICBpZiBybnVtIG5vdCBpbiBzZWxmLnVzZWROdW1zOgogICAgICAgICAg"
    "ICAgICAgICAgIHNlbGYudXNlZE51bXMuYWRkKHJudW0pCiAgICAgICAgICAgICAgICAgICAgcmV0"
    "dXJuIHJudW0KICAgICAgICAgICAgICAgIHJudW0gPSBybnVtICUgX01BWF9IRVJPX0lEICsgMQog"
    "ICAgICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoJ25vIGZyZWUgaGVybyBpZCAoc2VydmVyIGZ1"
    "bGw/KScpCiAgICBkZWYgcmVsZWFzZVVSYW5kb20oc2VsZiwgbnVtKToKICAgICAgICB3aXRoIHNl"
    "bGYubG9jazoKICAgICAgICAgICAgc2VsZi51c2VkTnVtcy5kaXNjYXJkKG51bSkjZGlzY2FyZDog"
    "c2FmZSBldmVuIGlmIGFscmVhZHkgcmVsZWFzZWQKICAgIGRlZiB1cGRhdGVEQkZyb20oc2VsZiwg"
    "dmVyc2lvbik6CiAgICAgICAgcHJpbnQoJ0RhdGFiYXNlIFZlcnNpb246Jyx2ZXJzaW9uKQogICAg"
    "ICAgIGlmIHZlcnNpb24gPj0gX0RCQ1VSVkVSOgogICAgICAgICAgICByZXR1cm4KICAgICAgICBw"
    "cmludCgnVXBkYXRpbmcgRGF0YWJhc2UgdG8gVmVyc2lvbicsX0RCQ1VSVkVSKQogICAgICAgIHdp"
    "dGggc2VsZi5sb2NrOgogICAgICAgICAgICB1cGRjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAg"
    "ICAgICAgIGlmIHZlcnNpb24gPT0gMDoKICAgICAgICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9T"
    "UUxJTklUX2RiSW5mb1RhYmxlKQogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElO"
    "SVRfZGJJbmZvVmVyc2lvbikKICAgICAgICAgICAgICAgIHVwZGN1ci5leGVjdXRlKF9TUUxJTklU"
    "X2RiVXNlclRhYmxlKQogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJG"
    "b3JtVGFibGUpCiAgICAgICAgICAgIGlmIHZlcnNpb24gPCAyOgogICAgICAgICAgICAgICAgI0d1"
    "aWxkIHN0b3JhZ2UuIEFkZGl0aXZlIG9ubHksIHNvIGFuIGV4aXN0aW5nIHYxIGRhdGFiYXNlIHdp"
    "dGgKICAgICAgICAgICAgICAgICNyZWFsIGFjY291bnRzIGluIGl0IHVwZ3JhZGVzIGluIHBsYWNl"
    "LgogICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJHdWlsZFRhYmxlKQog"
    "ICAgICAgICAgICAgICAgdXBkY3VyLmV4ZWN1dGUoX1NRTElOSVRfZGJHdWlsZE1lbWJlclRhYmxl"
    "KQogICAgICAgICAgICAjVGhlIHZlcnNpb24gcm93IHdhcyBvbmx5IGV2ZXIgd3JpdHRlbiBieSB0"
    "aGUgdmVyc2lvbj09MCBicmFuY2gsIHNvCiAgICAgICAgICAgICNldmVyeSBsYXRlciBtaWdyYXRp"
    "b24gd291bGQgaGF2ZSByZS1ydW4gb24gdGhlIG5leHQgc3RhcnQuCiAgICAgICAgICAgIHVwZGN1"
    "ci5leGVjdXRlKF9TUUxVUERfZGJJbmZvVmVyc2lvbikKICAgICAgICAgICAgc2VsZi5kYi5jb21t"
    "aXQoKQogICAgICAgICAgICB1cGRjdXIuY2xvc2UoKQogICAgZGVmIGdldFBERk4oc2VsZiwgbmFt"
    "ZSwgZm9ybSwgY3JlYXRlKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgZm9y"
    "bWN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgdWlkcmVzID0gZm9ybWN1ci5leGVj"
    "dXRlKF9TUUxfdXNlcklELCAobmFtZSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1aWRy"
    "ZXMgaXMgTm9uZToKICAgICAgICAgICAgICAgIGZvcm1jdXIuY2xvc2UoKQogICAgICAgICAgICAg"
    "ICAgcmV0dXJuIE5vbmUgI1VzZXIgZG9lc24ndCBleGlzdAogICAgICAgICAgICBmaWRyZXMgPSBm"
    "b3JtY3VyLmV4ZWN1dGUoX1NRTF9mb3JtSUQsIChmb3JtLCApKS5mZXRjaG9uZSgpCiAgICAgICAg"
    "ICAgIGlmIGZpZHJlcyBpcyBOb25lOiAjZm9ybWF0IGRvZXMgbm90IGV4aXN0CiAgICAgICAgICAg"
    "ICAgICBpZiBub3QgY3JlYXRlOgogICAgICAgICAgICAgICAgICAgIGZvcm1jdXIuY2xvc2UoKQog"
    "ICAgICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNOZXcgZm9ybWF0IG5vdCBjcmVhdGVkCiAg"
    "ICAgICAgICAgICAgICBmb3JtY3VyLmV4ZWN1dGUoX1NRTEFERF9mb3JtSUQsIChmb3JtLCApKQog"
    "ICAgICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKSNUT0RPIENoZWNrIGlmIGdvdHRhIGNvbW1p"
    "dCBiZWZvcmUgcmVhZC1iYWNrPwogICAgICAgICAgICAgICAgZmlkcmVzID0gZm9ybWN1ci5leGVj"
    "dXRlKF9TUUxfZm9ybUlELCAoZm9ybSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBmb3JtY3Vy"
    "LmNsb3NlKCkKICAgICAgICAgICAgZmlkID0gZmlkcmVzWzBdCiAgICAgICAgICAgIHVpZCA9IHVp"
    "ZHJlc1swXQogICAgICAgICAgICBmaWxlbmFtZSA9IF9GT1JNX1BERmlsZS5mb3JtYXQodWlkLCBm"
    "aWQpCiAgICAgICAgICAgIGZwYXRoID0gb3MucGF0aC5qb2luKF9QQVRIX1BMQVlFUkRBVEEsIGZp"
    "bGVuYW1lKQogICAgICAgICAgICBpZiBvcy5wYXRoLmV4aXN0cyhmcGF0aCkgb3IgY3JlYXRlOgog"
    "ICAgICAgICAgICAgICAgcmV0dXJuIGZwYXRoCiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICBk"
    "ZWYgZ2V0UGxheWVyRGF0YShzZWxmLCBuYW1lLCBmb3JtKToKICAgICAgICBwYXRoID0gc2VsZi5n"
    "ZXRQREZOKG5hbWUsIGZvcm0sIEZhbHNlKQogICAgICAgIGlmIG5vdCBwYXRoOgogICAgICAgICAg"
    "ICByZXR1cm4gYicnCiAgICAgICAgcmV0dXJuIHJlYWRCaW4ocGF0aCkjVE9ETyBkZWZhdWx0IHRv"
    "IGInJyBvbiBlcnJvcj8KICAgIGRlZiBzZXRQbGF5ZXJEYXRhKHNlbGYsIG5hbWUsIGZvcm0sIGRh"
    "dGEpOgogICAgICAgIHBhdGggPSBzZWxmLmdldFBERk4obmFtZSwgZm9ybSwgVHJ1ZSkKICAgICAg"
    "ICBpZiBub3QgcGF0aDojTk8gRklMRSBQQVRILCBUT0RPIENBVENIIEVSUk9SCiAgICAgICAgICAg"
    "IHJldHVybgogICAgICAgICNXcml0dGVuIHRvIGEgdGVtcCBmaWxlIGFuZCBtb3ZlZCBpbnRvIHBs"
    "YWNlLCBub3Qgd3JpdHRlbiBpbiBwbGFjZS4KICAgICAgICAjVGhlIGdhbWUgY2FsbHMgL3NldHBs"
    "YXllcmRhdGEgdG8gYXV0b3NhdmUgbWlkLXNlc3Npb24sIG5vdCBvbmx5IG9uIGEKICAgICAgICAj"
    "Y2xlYW4gZXhpdCAtIHRoZSBsaXZlIGxvZ3Mgc2hvdyBpdCBmaXJpbmcgd2hpbGUgYSBwbGF5ZXIg"
    "aXMgd2Fsa2luZwogICAgICAgICNhcm91bmQsIHdlbGwgYmVmb3JlIC9sZWF2ZWdhbWUuIGBvcGVu"
    "KHBhdGgsJ3diJylgIHRydW5jYXRlcyB0aGUgc2F2ZQogICAgICAgICN0byB6ZXJvIGJ5dGVzICpi"
    "ZWZvcmUqIHdyaXRpbmcgYSBzaW5nbGUgYnl0ZSBvZiB0aGUgbmV3IG9uZTogYSBjcmFzaCwKICAg"
    "ICAgICAjYSBraWxsZWQgcHJvY2VzcyBvciBhIGxvc3QgY29ubmVjdGlvbiBhdCBleGFjdGx5IHRo"
    "ZSB3cm9uZyBpbnN0YW50CiAgICAgICAgI2xlZnQgYSAwLWJ5dGUgb3IgaGFsZi13cml0dGVuIHNh"
    "dmUsIGFuZCBnZXRQbGF5ZXJEYXRhKCkgdGhlbiBoYW5kZWQKICAgICAgICAjdGhhdCBiYWNrIGFz"
    "ICJ5b3VyIGNoYXJhY3RlcidzIGRhdGEiIG9uIHRoZSBuZXh0IGxvZ2luIC0gdGhpcyBpcwogICAg"
    "ICAgICNhbG1vc3QgY2VydGFpbmx5IHRoZSAicHJvZ3Jlc3MgZ2V0cyBsb3N0IiByZXBvcnQuIG9z"
    "LnJlcGxhY2UoKSBpcwogICAgICAgICNhdG9taWMgb24gYm90aCBXaW5kb3dzIGFuZCBQT1NJWDog"
    "dGhlIGZpbGUgb24gZGlzayBpcyBlaXRoZXIgdGhlCiAgICAgICAgI2NvbXBsZXRlIG9sZCBzYXZl"
    "IG9yIHRoZSBjb21wbGV0ZSBuZXcgb25lLCBuZXZlciBhIHBhcnRpYWwgd3JpdGUuCiAgICAgICAg"
    "dG1wID0gcGF0aCArIGYnLntvcy5nZXRwaWQoKX0ue3RocmVhZGluZy5nZXRfaWRlbnQoKX0udG1w"
    "JwogICAgICAgIHRyeToKICAgICAgICAgICAgd2l0aCBvcGVuKHRtcCwgJ3diJykgYXMgZjoKICAg"
    "ICAgICAgICAgICAgIGYud3JpdGUoZGF0YSkKICAgICAgICAgICAgICAgIGYuZmx1c2goKQogICAg"
    "ICAgICAgICAgICAgb3MuZnN5bmMoZi5maWxlbm8oKSkKICAgICAgICAgICAgb3MucmVwbGFjZSh0"
    "bXAsIHBhdGgpCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHRyeToKICAgICAg"
    "ICAgICAgICAgIG9zLnJlbW92ZSh0bXApCiAgICAgICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAg"
    "ICAgICAgICAgICAgcGFzcwogICAgICAgICAgICByYWlzZQogICAgZGVmIGdldFdob2lzKHNlbGYs"
    "IG5hbWUpOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICB3Y3VyID0gc2VsZi5k"
    "Yi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSB3Y3VyLmV4ZWN1dGUoX1NRTF9nZXRXaG9pcywg"
    "KG5hbWUsKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICB3Y3VyLmNsb3NlKCkKICAgICAgICAgICAg"
    "aWYgcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZQogICAgICAgICAgICAo"
    "ZW1haWwsIGxvY2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24pID0gcmVzCiAgICAgICAg"
    "ICAgIGN1clllYXIgPSBkYXRldGltZS5kYXRldGltZS5ub3coKS55ZWFyCiAgICAgICAgICAgIGFn"
    "ZSA9IG1heCgwLCBjdXJZZWFyIC0geW9iKSBpZiB5b2IgZWxzZSAwCiAgICAgICAgICAgIHJldHVy"
    "biB7CiAgICAgICAgICAgICAgICAnZW1haWwnOiBlbWFpbCBvciAnJywKICAgICAgICAgICAgICAg"
    "ICdsb2NhdGlvbic6IGxvY2F0aW9uIG9yICcnLAogICAgICAgICAgICAgICAgJ2FnZSc6IGFnZSwK"
    "ICAgICAgICAgICAgICAgICdnZW5kZXInOiBnZW5kZXIgaWYgZ2VuZGVyIGlzIG5vdCBOb25lIGVs"
    "c2UgMCwKICAgICAgICAgICAgICAgICdkZXNjcmlwdGlvbic6IGRlc2NyaXB0aW9uIG9yICcnCiAg"
    "ICAgICAgICAgIH0KICAgIGRlZiB1cGRhdGVXaG9pcyhzZWxmLCBuYW1lLCBlbWFpbCwgbG9jYXRp"
    "b24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbik6CiAgICAgICAgdHJ5OgogICAgICAgICAgICBh"
    "Z2UgPSBpbnQoYWdlKQogICAgICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yKToKICAg"
    "ICAgICAgICAgYWdlID0gMAogICAgICAgIHRyeToKICAgICAgICAgICAgZ2VuZGVyID0gaW50KGdl"
    "bmRlcikKICAgICAgICBleGNlcHQgKFR5cGVFcnJvciwgVmFsdWVFcnJvcik6CiAgICAgICAgICAg"
    "IGdlbmRlciA9IDAKICAgICAgICB5b2IgPSBkYXRldGltZS5kYXRldGltZS5ub3coKS55ZWFyIC0g"
    "YWdlCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHdjdXIgPSBzZWxmLmRiLmN1"
    "cnNvcigpCiAgICAgICAgICAgIHdjdXIuZXhlY3V0ZShfU1FMVVBEX3dob2lzLCAoZW1haWwsIGxv"
    "Y2F0aW9uLCB5b2IsIGdlbmRlciwgZGVzY3JpcHRpb24sIG5hbWUpKQogICAgICAgICAgICBzZWxm"
    "LmRiLmNvbW1pdCgpCiAgICAgICAgICAgIHdjdXIuY2xvc2UoKQogICAgIyMgR1VJTERTCiAgICBk"
    "ZWYgZ2V0R3VpbGRPZihzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgIy0+IChndWlsZG5hbWUsIHJh"
    "bmspIG9yIChOb25lLCAwKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIg"
    "PSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAgIHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfZ3Vp"
    "bGRPZlVzZXIsICh1c2VybmFtZSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgp"
    "CiAgICAgICAgaWYgcmVzIGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVybiAoTm9uZSwgMCkKICAg"
    "ICAgICByZXR1cm4gKHJlc1swXSwgcmVzWzFdIG9yIDApCiAgICBkZWYgZ2V0R3VpbGROYW1lKHNl"
    "bGYsIHVzZXJuYW1lKToKICAgICAgICByZXR1cm4gc2VsZi5nZXRHdWlsZE9mKHVzZXJuYW1lKVsw"
    "XSBvciAnJwogICAgZGVmIGdldEd1aWxkTWVtYmVycyhzZWxmLCBndWlsZG5hbWUpOgogICAgICAg"
    "IHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAg"
    "ICAgICAgIHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRNZW1iZXJzLCAoZ3VpbGRuYW1lLCkp"
    "LmZldGNoYWxsKCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4gWyhyWzBd"
    "LCByWzFdIG9yIDApIGZvciByIGluIHJlc10KICAgIGRlZiBndWlsZEV4aXN0cyhzZWxmLCBndWls"
    "ZG5hbWUpOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRi"
    "LmN1cnNvcigpCiAgICAgICAgICAgIHJvdyA9IGN1ci5leGVjdXRlKF9TUUxfZ3VpbGRFeGlzdHMs"
    "ICgoZ3VpbGRuYW1lIG9yICcnKS5jYXNlZm9sZCgpLCkpLmZldGNob25lKCkKICAgICAgICAgICAg"
    "Y3VyLmNsb3NlKCkKICAgICAgICByZXR1cm4gcm93IGlzIG5vdCBOb25lCiAgICBkZWYgZ3VpbGRO"
    "YW1lRnJlZShzZWxmLCBndWlsZG5hbWUpOgogICAgICAgICNTYW1lIHJ1bGVzIGNyZWF0ZUd1aWxk"
    "KCkgZW5mb3JjZXMsIGFza2VkIGluIGFkdmFuY2UgLSB0aGUgY2xpZW50CiAgICAgICAgI2NoZWNr"
    "cyBhIG5hbWUgd2l0aCAvdGVzdGNyZWF0ZWd1aWxkIGJlZm9yZSBpdCB3aWxsIGxldCB0aGUgcGxh"
    "eWVyCiAgICAgICAgI2NvbmZpcm0uIEFuc3dlcmluZyAiZnJlZSIgZm9yIGEgbmFtZSBjcmVhdGVH"
    "dWlsZCB3b3VsZCB0aGVuIHJlamVjdAogICAgICAgICN3b3VsZCBqdXN0IG1vdmUgdGhlIGRlYWQg"
    "ZW5kIG9uZSBkaWFsb2cgbGF0ZXIuCiAgICAgICAgaWYgbm90IF9SRV9WQUxJRF9HVUlMRE5BTUUu"
    "bWF0Y2goZ3VpbGRuYW1lIG9yICcnKToKICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAg"
    "cmV0dXJuIG5vdCBzZWxmLmd1aWxkRXhpc3RzKGd1aWxkbmFtZSkKICAgIGRlZiBsaXN0R3VpbGRz"
    "KHNlbGYpOgogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRi"
    "LmN1cnNvcigpCiAgICAgICAgICAgIHJvd3MgPSBjdXIuZXhlY3V0ZShfU1FMX2FsbEd1aWxkcyku"
    "ZmV0Y2hhbGwoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgIHJldHVybiBbclswXSBm"
    "b3IgciBpbiByb3dzXQogICAgZGVmIGNyZWF0ZUd1aWxkKHNlbGYsIGd1aWxkbmFtZSwgb3duZXIs"
    "IGRlc2NyaXB0aW9uPScnKToKICAgICAgICAjLT4gZ3VpbGRuYW1lIG9uIHN1Y2Nlc3MsIG9yIGFu"
    "IGVycm9yIHRva2VuIGZvciB0aGUgY2xpZW50CiAgICAgICAgaWYgbm90IF9SRV9WQUxJRF9HVUlM"
    "RE5BTUUubWF0Y2goZ3VpbGRuYW1lIG9yICcnKToKICAgICAgICAgICAgcmV0dXJuICdiYWRHdWls"
    "ZE5hbWUnCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIGN1ciA9IHNlbGYuZGIu"
    "Y3Vyc29yKCkKICAgICAgICAgICAgaWYgY3VyLmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwgKG93"
    "bmVyLCkpLmZldGNob25lKCkgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xvc2Uo"
    "KQogICAgICAgICAgICAgICAgcmV0dXJuICdhbHJlYWR5SW5HdWlsZCcKICAgICAgICAgICAgaWYg"
    "Y3VyLmV4ZWN1dGUoX1NRTF9ndWlsZEV4aXN0cywgKGd1aWxkbmFtZS5jYXNlZm9sZCgpLCkpLmZl"
    "dGNob25lKCkgaXMgbm90IE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAg"
    "ICAgICAgICAgcmV0dXJuICdndWlsZE5hbWVUYWtlbicKICAgICAgICAgICAgY3VyLmV4ZWN1dGUo"
    "X1NRTF9jcmVhdGVHdWlsZCwKICAgICAgICAgICAgICAgICAgICAgICAgKGd1aWxkbmFtZSwgZ3Vp"
    "bGRuYW1lLmNhc2Vmb2xkKCksIG93bmVyLAogICAgICAgICAgICAgICAgICAgICAgICAgZGF0ZXRp"
    "bWUuZGF0ZXRpbWUubm93KCksIHNhbml0aXplVGV4dChkZXNjcmlwdGlvbikpKQogICAgICAgICAg"
    "ICBjdXIuZXhlY3V0ZShfU1FMX2FkZEd1aWxkTWVtYmVyLCAoZ3VpbGRuYW1lLCBvd25lciwgMikp"
    "CiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAg"
    "ICAgICByZXR1cm4gTm9uZQogICAgZGVmIGpvaW5HdWlsZChzZWxmLCBndWlsZG5hbWUsIHVzZXJu"
    "YW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgY3VyID0gc2VsZi5kYi5j"
    "dXJzb3IoKQogICAgICAgICAgICByb3cgPSBjdXIuZXhlY3V0ZShfU1FMX2d1aWxkRXhpc3RzLCAo"
    "KGd1aWxkbmFtZSBvciAnJykuY2FzZWZvbGQoKSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlm"
    "IHJvdyBpcyBOb25lOgogICAgICAgICAgICAgICAgY3VyLmNsb3NlKCkKICAgICAgICAgICAgICAg"
    "IHJldHVybiAndW5rbm93bkd1aWxkJwogICAgICAgICAgICAjU3RvcmUgdGhlIGd1aWxkJ3Mgb3du"
    "IHNwZWxsaW5nLCBub3Qgd2hhdGV2ZXIgY2FzZSB0aGUgY2xpZW50IHR5cGVkCiAgICAgICAgICAg"
    "ICNpbnRvIHRoZSBqb2luIGJveCwgc28gZ2V0R3VpbGRNZW1iZXJzKCkgZmluZHMgdGhlIG1lbWJl"
    "ciBiYWNrLgogICAgICAgICAgICBndWlsZG5hbWUgPSByb3dbMF0KICAgICAgICAgICAgaWYgY3Vy"
    "LmV4ZWN1dGUoX1NRTF9ndWlsZE9mVXNlciwgKHVzZXJuYW1lLCkpLmZldGNob25lKCkgaXMgbm90"
    "IE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xvc2UoKQogICAgICAgICAgICAgICAgcmV0dXJu"
    "ICdhbHJlYWR5SW5HdWlsZCcKICAgICAgICAgICAgY3VyLmV4ZWN1dGUoX1NRTF9hZGRHdWlsZE1l"
    "bWJlciwgKGd1aWxkbmFtZSwgdXNlcm5hbWUsIDApKQogICAgICAgICAgICBzZWxmLmRiLmNvbW1p"
    "dCgpCiAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGRlZiBs"
    "ZWF2ZUd1aWxkKHNlbGYsIHVzZXJuYW1lKToKICAgICAgICB3aXRoIHNlbGYubG9jazoKICAgICAg"
    "ICAgICAgY3VyID0gc2VsZi5kYi5jdXJzb3IoKQogICAgICAgICAgICByZXMgPSBjdXIuZXhlY3V0"
    "ZShfU1FMX2d1aWxkT2ZVc2VyLCAodXNlcm5hbWUsKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBp"
    "ZiByZXMgaXMgTm9uZToKICAgICAgICAgICAgICAgIGN1ci5jbG9zZSgpCiAgICAgICAgICAgICAg"
    "ICByZXR1cm4gJ25vdEluR3VpbGQnCiAgICAgICAgICAgIChndWlsZG5hbWUsIHJhbmspID0gKHJl"
    "c1swXSwgcmVzWzFdIG9yIDApCiAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfZGVsR3VpbGRN"
    "ZW1iZXIsICh1c2VybmFtZSwpKQogICAgICAgICAgICBvd25lciA9IGN1ci5leGVjdXRlKF9TUUxf"
    "Z3VpbGRPd25lciwgKGd1aWxkbmFtZSwpKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIG93bmVy"
    "IGFuZCBvd25lclswXSA9PSB1c2VybmFtZToKICAgICAgICAgICAgICAgICNUaGUgZm91bmRlciBs"
    "ZWF2aW5nIGRpc3NvbHZlcyB0aGUgZ3VpbGQgcmF0aGVyIHRoYW4gbGVhdmluZyBhbgogICAgICAg"
    "ICAgICAgICAgI293bmVybGVzcyByZWNvcmQgdGhhdCBub2JvZHkgY2FuIGV2ZXIgYWRtaW5pc3Rl"
    "ci4KICAgICAgICAgICAgICAgIGN1ci5leGVjdXRlKF9TUUxfZGVsR3VpbGRNZW1iZXJzLCAoZ3Vp"
    "bGRuYW1lLCkpCiAgICAgICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2RlbGV0ZUd1aWxkLCAo"
    "Z3VpbGRuYW1lLCkpCiAgICAgICAgICAgIHNlbGYuZGIuY29tbWl0KCkKICAgICAgICAgICAgY3Vy"
    "LmNsb3NlKCkKICAgICAgICByZXR1cm4gTm9uZQogICAgZGVmIGxvZ2luUGxheWVyKHNlbGYsIHVz"
    "ZXJuYW1lLCBjb24sIHBhc3N3b3JkKTojVE9ETyBzaG91bGQgcmV0dXJuIGVycm9yIHByb3Blcmx5"
    "IHRvIGNsaWVudAogICAgICAgIGlmIG5vdCBfUkVfVkFMSURfVVNFUk5BTUUubWF0Y2godXNlcm5h"
    "bWUpOgogICAgICAgICAgICAjUmVnaXN0cmF0aW9uIGhhcyBhbHdheXMgdmFsaWRhdGVkIHRoZSBu"
    "YW1lOyBsb2dnaW5nIGluIGRpZCBub3QuCiAgICAgICAgICAgICNOYW1lcyByZWFjaCBvdGhlciBj"
    "bGllbnRzIGluc2lkZSBxdW90ZWQgcHJvdG9jb2wgZmllbGRzLCBzbyBhIG5hbWUKICAgICAgICAg"
    "ICAgI2NvbnRhaW5pbmcgJyInIGZvcmdlcyBjb21tYW5kcyAtIGFuZCB0aGUgQWxsb3dBbnlMb2dp"
    "biBkZWJ1ZyBwYXRoCiAgICAgICAgICAgICNiZWxvdyBuZXZlciB0b3VjaGVzIHRoZSBkYXRhYmFz"
    "ZSwgd2hpY2ggbWFkZSBpdCB0aGUgb25lIHdheSB0byBnZXQKICAgICAgICAgICAgI3N1Y2ggYSBu"
    "YW1lIGluLiBDaGVjayBoZXJlIHNvIGJvdGggcGF0aHMgYXJlIGNvdmVyZWQuCiAgICAgICAgICAg"
    "IHJldHVybiBOb25lCiAgICAgICAgaWYgX0RFQlVHX0FMTE9XX0FOWV9MT0dJTjogI0RFQlVHIEFV"
    "VE8gQUxMT1cKICAgICAgICAgICAgcmV0dXJuIFVzZXIodXNlcm5hbWUsIGNvbikKICAgICAgICB3"
    "aXRoIHNlbGYubG9jazoKICAgICAgICAgICAgbG9naW5DdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAg"
    "ICAgICAgICAgICNEZWZhdWx0IHRvIFNUUklDVCwgVE9ETyBhbGxvdyBmb3Igbm9uLXN0cmljdD8K"
    "ICAgICAgICAgICAgdWlkcmVzID0gbG9naW5DdXIuZXhlY3V0ZShfU1FMX3VzZXJJRF9zdHJpY3Qs"
    "ICh1c2VybmFtZSwgY29uLlNLKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBpZiB1aWRyZXMgaXMg"
    "Tm9uZToKICAgICAgICAgICAgICAgICNwcmludCgnbG9naW4gZXJyb3I6IG5vIHVzZXIgd2l0aCB0"
    "aGF0IHNlcmlhbCBrZXknKQogICAgICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAg"
    "ICAgICAgICAgcmV0dXJuIE5vbmUgI05vIHN1Y2ggVXNlcgogICAgICAgICAgICB1aWQgPSB1aWRy"
    "ZXNbMF0KICAgICAgICAgICAgKHJVc2VyLCBwYXNzaGFzaCwgdVNhbHQsIGhJdHIpID0gbG9naW5D"
    "dXIuZXhlY3V0ZShfU1FMX2dldExvZ2luLCAodWlkLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAg"
    "IGlmIHVzZXJuYW1lICE9IHJVc2VyOgogICAgICAgICAgICAgICAgI3ByaW50KGYnbG9naW4gZXJy"
    "b3I6IHdyb25nIHVzZXJuYW1lOiB7dXNlcm5hbWV9JykKICAgICAgICAgICAgICAgIGxvZ2luQ3Vy"
    "LmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNXcm9uZyBVc2VybmFtZQogICAg"
    "ICAgICAgICB0cGFzID0gX3NhbHRfaGFzaF8ocGFzc3dvcmQsIHVTYWx0LCBoSXRyKQogICAgICAg"
    "ICAgICBpZiB0cGFzICE9IHBhc3NoYXNoOgogICAgICAgICAgICAgICAgI3ByaW50KGYnbG9naW4g"
    "ZXJyb3I6IHdyb25nIHBhc3N3b3JkOiB7cGFzc3dvcmR9JykKICAgICAgICAgICAgICAgIGxvZ2lu"
    "Q3VyLmNsb3NlKCkKICAgICAgICAgICAgICAgIHJldHVybiBOb25lICNXcm9uZyBQYXNzd29yZAog"
    "ICAgICAgICAgICBpZiBoSXRyICE9IF9IQVNISVRFUjoKICAgICAgICAgICAgICAgIG5wc2ggPSBf"
    "c2FsdF9oYXNoXyhwYXNzd29yZCwgdVNhbHQsIF9IQVNISVRFUikKICAgICAgICAgICAgICAgIGxv"
    "Z2luQ3VyLmV4ZWN1dGUoX1NRTFVQRF9wYXNzSGFzaCwgKG5wc2gsIF9IQVNISVRFUiwgdWlkKSkK"
    "ICAgICAgICAgICAgdXNlcm9iaiA9IFVzZXIodXNlcm5hbWUsIGNvbikKICAgICAgICAgICAgI3Vw"
    "ZGF0ZSBsYXN0IGxvZ2luCiAgICAgICAgICAgIGxvZ2luQ3VyLmV4ZWN1dGUoX1NRTF9sb2dpblVw"
    "ZGF0ZSwgKHVzZXJvYmoubG9naW5UaW1lLCB1aWQpKQogICAgICAgICAgICAjVE9ETyBkZWZhdWx0"
    "IGRhdGV0aW1lIGFkYXB0ZXIgZGVwcmVjYXRlZCwgY2hlY2sgcmVwbGFjZW1lbnQKICAgICAgICAg"
    "ICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAgICAgICAg"
    "ICAgIHJldHVybiB1c2Vyb2JqCiAgICBkZWYgcmVnaXN0ZXJQbGF5ZXIoc2VsZiwgdXNlcm5hbWUs"
    "IGNvbiwgcGFzc3dvcmQsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9u"
    "KToKICAgICAgICBpZiBub3QgX1JFX1ZBTElEX1VTRVJOQU1FLm1hdGNoKHVzZXJuYW1lKToKICAg"
    "ICAgICAgICAgcmV0dXJuIE5vbmUgI0ludmFsaWQgdXNlcm5hbWUgKGJhZCBjaGFycy9sZW5ndGgp"
    "LCBhbHNvIGJsb2NrcyBwcm90b2NvbC1pbmplY3Rpb24gdmlhICciJwogICAgICAgIGVtYWlsID0g"
    "c2FuaXRpemVUZXh0KGVtYWlsKQogICAgICAgIGxvY2F0aW9uID0gc2FuaXRpemVUZXh0KGxvY2F0"
    "aW9uKQogICAgICAgIGRlc2NyaXB0aW9uID0gc2FuaXRpemVUZXh0KGRlc2NyaXB0aW9uKQogICAg"
    "ICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAgICAgICBsb2dpbkN1ciA9IHNlbGYuZGIuY3Vyc29y"
    "KCkKICAgICAgICAgICAgdWlkcmVzID0gbG9naW5DdXIuZXhlY3V0ZShfU1FMX3VzZXJJRCwgKHVz"
    "ZXJuYW1lLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVpZHJlcyBpcyBub3QgTm9uZToK"
    "ICAgICAgICAgICAgICAgICNwcmludChmJ3JlZ2lzdGVyIGVycm9yOiB1c2VybmFtZSBhbHJlYWR5"
    "IGluIHVzZToge3VzZXJuYW1lfScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgpCiAg"
    "ICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjVXNlciBleGlzdHMKICAgICAgICAgICAgI2lmIHN0"
    "cmljdCwgY2hlY2sgaWYgc2VyaWFsIGlzIGluIHVzZSB0b28KICAgICAgICAgICAgI1RPRE8gb25s"
    "eSBhcHBseSBpZiBzdHJpY3QKICAgICAgICAgICAgdWlkcmVzID0gbG9naW5DdXIuZXhlY3V0ZShf"
    "U1FMX3VzZXJJRF9TY2hrLCAoY29uLlNLLCApKS5mZXRjaG9uZSgpCiAgICAgICAgICAgIGlmIHVp"
    "ZHJlcyBpcyBub3QgTm9uZToKICAgICAgICAgICAgICAgICNwcmludCgncmVnaXN0ZXIgZXJyb3I6"
    "IHNlcmlhbCBhbHJlYWR5IGluIHVzZScpCiAgICAgICAgICAgICAgICBsb2dpbkN1ci5jbG9zZSgp"
    "CiAgICAgICAgICAgICAgICByZXR1cm4gTm9uZSAjU2VyaWFsIGluIHVzZSBleGlzdHMKICAgICAg"
    "ICAgICAgdVNhbHQgPSBvcy51cmFuZG9tKDE2KQogICAgICAgICAgICBwSGFzaCA9IF9zYWx0X2hh"
    "c2hfKHBhc3N3b3JkLCB1U2FsdCwgX0hBU0hJVEVSKQogICAgICAgICAgICBjdXJ0aW1lID0gZGF0"
    "ZXRpbWUuZGF0ZXRpbWUubm93KCkKICAgICAgICAgICAgdHJ5OiN0cnkgc2hvdWxkbid0IGJlIG5l"
    "ZWRlZCBhcyBlbXB0eSBmaWVsZCBpcyBzZXQgdG8gMjU1CiAgICAgICAgICAgICAgICBhZ2UgPSBp"
    "bnQoYWdlKQogICAgICAgICAgICBleGNlcHQ6CiAgICAgICAgICAgICAgICBhZ2UgPSAwCiAgICAg"
    "ICAgICAgIHlvYiA9IGN1cnRpbWUueWVhciAtIGFnZQogICAgICAgICAgICByZWd2YWxzID0gKAog"
    "ICAgICAgICAgICAgICAgdXNlcm5hbWUscEhhc2gsCiAgICAgICAgICAgICAgICBjb24uU0ssdVNh"
    "bHQsX0hBU0hJVEVSLAogICAgICAgICAgICAgICAgY3VydGltZSxlbWFpbCxsb2NhdGlvbix5b2Is"
    "Z2VuZGVyLGRlc2NyaXB0aW9uCiAgICAgICAgICAgICkKICAgICAgICAgICAgbG9naW5DdXIuZXhl"
    "Y3V0ZShfU1FMX3JlZ2lzdGVyVXNlciwgcmVndmFscykKICAgICAgICAgICAgI1RPRE8gZGVmYXVs"
    "dCBkYXRldGltZSBhZGFwdGVyIGRlcHJlY2F0ZWQsIGNoZWNrIHJlcGxhY2VtZW50CiAgICAgICAg"
    "ICAgIHVzZXJvYmogPSBVc2VyKHVzZXJuYW1lLCBjb24pCiAgICAgICAgICAgIHNlbGYuZGIuY29t"
    "bWl0KCkKICAgICAgICAgICAgbG9naW5DdXIuY2xvc2UoKQogICAgICAgICAgICByZXR1cm4gdXNl"
    "cm9iagogICAgZGVmIG5hbWVUYWtlbihzZWxmLCB1c2VybmFtZSk6CiAgICAgICAgI0RvZXMgYW4g"
    "YWNjb3VudCB3aXRoIHRoaXMgbmFtZSBleGlzdCBhdCBhbGwsIHJlZ2FyZGxlc3Mgb2Ygc2VyaWFs"
    "PwogICAgICAgICNVc2VkIHRvIHRlbGwgInRoaXMgbmFtZSBpcyBmcmVlIGJ1dCB5b3VyIGtleSBk"
    "b2VzIG5vdCBtYXRjaCB0aGUKICAgICAgICAjYWNjb3VudCIgYXBhcnQgZnJvbSAidGhpcyBuYW1l"
    "IGlzIGdlbnVpbmVseSB1bnVzYWJsZSIuCiAgICAgICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAg"
    "ICAgIGN1ciA9IHNlbGYuZGIuY3Vyc29yKCkKICAgICAgICAgICAgcmVzID0gY3VyLmV4ZWN1dGUo"
    "X1NRTF91c2VySUQsICh1c2VybmFtZSwgKSkuZmV0Y2hvbmUoKQogICAgICAgICAgICBjdXIuY2xv"
    "c2UoKQogICAgICAgIHJldHVybiByZXMgaXMgbm90IE5vbmUKICAgIGRlZiBkZWxldGVBY2NvdW50"
    "KHNlbGYsIHVzZXJuYW1lKToKICAgICAgICAjQWRtaW4tcGFuZWwgYWN0aW9uIChHVUkgItCj0LTQ"
    "sNC70LjRgtGMINC/0LXRgNGB0L7QvdCw0LbQsCIpOiBwZXJtYW5lbnRseSByZW1vdmVzIGFuCiAg"
    "ICAgICAgI2FjY291bnQgYW5kIGV2ZXJ5IHNhdmVkIHBsYXllcmRhdGEgYmxvYiBmb3IgaXQuIEly"
    "cmV2ZXJzaWJsZSAtIHRoZQogICAgICAgICNHVUkgaXMgZXhwZWN0ZWQgdG8gY29uZmlybSB3aXRo"
    "IHRoZSBhZG1pbiBiZWZvcmUgY2FsbGluZyB0aGlzLgogICAgICAgICNEb2VzIE5PVCB0b3VjaCB0"
    "aGUgY2FsbGVyJ3MgbGl2ZSBjb25uZWN0aW9uL3Nlc3Npb247IHRoZSBjYWxsZXIgaXMKICAgICAg"
    "ICAjcmVzcG9uc2libGUgZm9yIGtpY2tpbmcgZmlyc3QgaWYgdGhlIGFjY291bnQgaXMgY3VycmVu"
    "dGx5IG9ubGluZQogICAgICAgICMoc2VlIENvcmVTZXJ2ZXIuZGVsZXRlQWNjb3VudCksIG90aGVy"
    "d2lzZSBhIGNvbm5lY3RlZCBjbGllbnQgd291bGQKICAgICAgICAja2VlcCBwbGF5aW5nIHdpdGgg"
    "YW4gYWNjb3VudCB0aGF0IG5vIGxvbmdlciBleGlzdHMgaW4gdGhlIERCLgogICAgICAgIHdpdGgg"
    "c2VsZi5sb2NrOgogICAgICAgICAgICBjdXIgPSBzZWxmLmRiLmN1cnNvcigpCiAgICAgICAgICAg"
    "IHVpZHJlcyA9IGN1ci5leGVjdXRlKF9TUUxfdXNlcklELCAodXNlcm5hbWUsICkpLmZldGNob25l"
    "KCkKICAgICAgICAgICAgaWYgdWlkcmVzIGlzIE5vbmU6CiAgICAgICAgICAgICAgICBjdXIuY2xv"
    "c2UoKQogICAgICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAgICAgICAgICAgIHVpZCA9IHVpZHJl"
    "c1swXQogICAgICAgICAgICBjdXIuZXhlY3V0ZShfU1FMX2RlbGV0ZVVzZXIsICh1c2VybmFtZSwg"
    "KSkKICAgICAgICAgICAgc2VsZi5kYi5jb21taXQoKQogICAgICAgICAgICBjdXIuY2xvc2UoKQog"
    "ICAgICAgICNHdWlsZCBtZW1iZXJzaGlwIG91dGxpdmVzIHRoZSB1c2VyVGFibGUgcm93IG90aGVy"
    "d2lzZSwgc28gdGhlIGRlbGV0ZWQKICAgICAgICAjbmFtZSB3b3VsZCBrZWVwIHNob3dpbmcgdXAg"
    "aW4gaXRzIGd1aWxkJ3Mgcm9zdGVyIGZvcmV2ZXIuCiAgICAgICAgc2VsZi5sZWF2ZUd1aWxkKHVz"
    "ZXJuYW1lKQogICAgICAgICNQbGF5ZXJkYXRhIGZpbGVzICgie3VzZXJJRDp4fV97Zm9ybUlEOnh9"
    "LmJpbiIpIGxpdmUgb3V0c2lkZSB0aGUgREIKICAgICAgICAjdHJhbnNhY3Rpb24gYW5kIGFyZSBs"
    "b29rZWQgdXAgYnkgcHJlZml4IC0gYmVzdCBlZmZvcnQsIGEgbGVmdG92ZXIKICAgICAgICAjZmls"
    "ZSBoZXJlIGlzbid0IHdvcnRoIGZhaWxpbmcgdGhlIHdob2xlIGRlbGV0aW9uIG92ZXIuCiAgICAg"
    "ICAgcHJlZml4ID0gZid7dWlkOnh9XycKICAgICAgICB0cnk6CiAgICAgICAgICAgIGZvciBmbiBp"
    "biBvcy5saXN0ZGlyKF9QQVRIX1BMQVlFUkRBVEEpOgogICAgICAgICAgICAgICAgaWYgZm4uc3Rh"
    "cnRzd2l0aChwcmVmaXgpOgogICAgICAgICAgICAgICAgICAgIHRyeToKICAgICAgICAgICAgICAg"
    "ICAgICAgICAgb3MucmVtb3ZlKG9zLnBhdGguam9pbihfUEFUSF9QTEFZRVJEQVRBLCBmbikpCiAg"
    "ICAgICAgICAgICAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgICAgICAgICAgICAg"
    "IHBhc3MKICAgICAgICBleGNlcHQgT1NFcnJvcjoKICAgICAgICAgICAgcGFzcwogICAgICAgIHJl"
    "dHVybiBUcnVlCkdESCA9IERhdGFIYW5kbGVyKCkKCmRlZiBfd29Vc2VyKHVsLCB1c3IpOgogICAg"
    "cmV0dXJuIGxpc3QoIChhIGZvciBhIGluIHVsIGlmIGEgaXMgbm90IHVzcikgKQpkZWYgX1JlYWRC"
    "bG9iKGNvbiwgc2l6ZSk6CiAgICAjc2l6ZSBjb21lcyBzdHJhaWdodCBvZmYgdGhlIHdpcmUsIHNv"
    "IGl0IGlzIG5laXRoZXIgdHJ1c3RlZCB0byBiZSBhIG51bWJlcgogICAgI25vciB0byBiZSBzYW5l"
    "OiBhIGNsaWVudCBjbGFpbWluZyBhIGh1Z2UgbGVuZ3RoIHVzZWQgdG8gbWFrZSB0aGUgc2VydmVy"
    "CiAgICAjYnVmZmVyIHVuYm91bmRlZGx5IChtZW1vcnkgZXhoYXVzdGlvbiksIGFuZCBhIGNsaWVu"
    "dCB0aGF0IGRpc2Nvbm5lY3RlZAogICAgI21pZC1ibG9iIG1hZGUgcmVjdigpIHJldHVybiBiJycg"
    "Zm9yZXZlciAtIGEgMTAwJSBDUFUgYnVzeS1sb29wLCB0aGUgc2FtZQogICAgI2RlZmVjdCBhbHJl"
    "YWR5IGZpeGVkIGluIENvbm5lY3Rpb25IYW5kbGVyLl9yZWN2TW9yZSgpLgogICAgdHJ5OgogICAg"
    "ICAgIHNpemUgPSBpbnQoc2l6ZSkKICAgIGV4Y2VwdCAoVHlwZUVycm9yLCBWYWx1ZUVycm9yKToK"
    "ICAgICAgICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmFkIGJsb2Igc2l6ZSB7c2l6ZSFyfScpCiAg"
    "ICBpZiBzaXplIDwgMCBvciBzaXplID4gX01BWF9CTE9COgogICAgICAgIHJhaXNlIFByb3RvY29s"
    "RXJyb3IoZidibG9iIHNpemUge3NpemV9IG91dCBvZiByYW5nZSAobWF4IHtfTUFYX0JMT0J9KScp"
    "CiAgICAjQSBibG9iIHJlYWQgYmxvY2tzIHRoaXMgY29ubmVjdGlvbidzIGVudGlyZSBoYW5kbGVy"
    "IHRocmVhZC4gQW5ub3VuY2luZyBhCiAgICAjbGVuZ3RoIGFuZCB0aGVuIGdvaW5nIHF1aWV0IC0g"
    "YSB3ZWRnZWQgY2xpZW50LCBhIGxpbmsgdGhhdCBkcm9wcGVkCiAgICAjd2l0aG91dCBhIHJlc2V0"
    "IC0gdXNlZCB0byBibG9jayBpdCBmb3JldmVyOiB0aGUgdGhyZWFkIG5ldmVyIHJldHVybmVkLCBz"
    "bwogICAgI3RoZSBwbGF5ZXIncyBhY2NvdW50IHN0YXllZCBjbGFpbWVkIGFuZCBhbnkgcm9vbSB0"
    "aGV5IGhvc3RlZCBzdGF5ZWQKICAgICNsaXN0ZWQgd2l0aCBub3RoaW5nIGJlaGluZCBpdC4gVGhl"
    "IGlkbGUgdGltZW91dCBuZXZlciBhcHBsaWVkIGhlcmUsCiAgICAjYmVjYXVzZSBpdCBpcyBvbmx5"
    "IGNvbnN1bHRlZCBieSB0aGUgcmVhZCBsb29wIHRoaXMgY2FsbCBoYXMgc3RlcHBlZCBvdXQKICAg"
    "ICNvZi4KICAgIGRlYWRsaW5lID0gdGltZS5tb25vdG9uaWMoKSArIF9CTE9CX1RJTUVPVVQKICAg"
    "IHdoaWxlIGxlbihjb24uZGF0YSkgPCBzaXplOgogICAgICAgIHJlbWFpbmluZyA9IGRlYWRsaW5l"
    "IC0gdGltZS5tb25vdG9uaWMoKQogICAgICAgIGlmIHJlbWFpbmluZyA8PSAwOgogICAgICAgICAg"
    "ICByYWlzZSBQcm90b2NvbEVycm9yKGYnYmxvYiBvZiB7c2l6ZX0gYnl0ZXMgbm90IGRlbGl2ZXJl"
    "ZCB3aXRoaW4gJwogICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYne19CTE9CX1RJTUVP"
    "VVR9cyAoe2xlbihjb24uZGF0YSl9IHJlY2VpdmVkKScpCiAgICAgICAgI3NlbGVjdCgpLCBOT1Qg"
    "c2V0dGltZW91dCgpLiBBIHNvY2tldCB0aW1lb3V0IGlzIGEgcHJvcGVydHkgb2YgdGhlCiAgICAg"
    "ICAgI3NvY2tldCByYXRoZXIgdGhhbiBvZiB0aGUgY2FsbCwgc28gdGhlIHNldHRpbWVvdXQoKSB0"
    "aGF0IHVzZWQgdG8gYmUKICAgICAgICAjaGVyZSBhbHNvIGFybWVkIHRoZSB3cml0ZXIgdGhyZWFk"
    "J3MgY29uY3VycmVudCBzZW5kYWxsKCkgLSBhbmQgbm90aGluZwogICAgICAgICNldmVyIGRpc2Fy"
    "bWVkIGl0IGFnYWluLCBzbyBpdCBzdGF5ZWQgYXJtZWQgZm9yIHRoZSB3aG9sZSByZW1haW5pbmcg"
    "bGlmZQogICAgICAgICNvZiB0aGUgY29ubmVjdGlvbi4gQSBjbGllbnQgd2hvc2UgcmVjZWl2ZSB3"
    "aW5kb3cgZmlsbGVkIHVwIGZvciBhIG1vbWVudAogICAgICAgICMocHJlY2lzZWx5IHdoYXQgaGFw"
    "cGVucyBpbiBhIGJ1c3kgY28tb3Agc2Vzc2lvbikgdGhlbiBtYWRlIHRoYXQKICAgICAgICAjc2Vu"
    "ZGFsbCgpIHJhaXNlIFRpbWVvdXRFcnJvciAqYWZ0ZXIgaGF2aW5nIGFscmVhZHkgd3JpdHRlbiBw"
    "YXJ0IG9mIGEKICAgICAgICAjcGFja2V0KjogdGhlIHdyaXRlciB0aHJlYWQgZGllZCwgdGhlIGNs"
    "aWVudCB3YXMgbGVmdCBob2xkaW5nIGhhbGYgYQogICAgICAgICNtZXNzYWdlLCBhbmQgaXRzIGNv"
    "bW1hbmQgc3RyZWFtIHdhcyBkZXN5bmNocm9uaXNlZCBmcm9tIHRoYXQgcG9pbnQgb24uCiAgICAg"
    "ICAgI1RoZSB2aXNpYmxlIHJlc3VsdCBpcyBhIGZyZWV6ZSBvciBhIGRyb3AgbWludXRlcyBsYXRl"
    "ciwgd2l0aCBub3RoaW5nIGluCiAgICAgICAgI3RoZSBsb2cgdHlpbmcgaXQgYmFjayB0byB0aGUg"
    "YmxvYiB0aGF0IGFybWVkIHRoZSB0aW1lb3V0LiBFdmVyeQogICAgICAgICNibG9iLWNhcnJ5aW5n"
    "IGNvbW1hbmQgaXMgb24gdGhpcyBwYXRoIC0gL3NldHVzZXJoZXJvZGF0YSwgdGhlCiAgICAgICAg"
    "Iy9zZXRwbGF5ZXJkYXRhIGF1dG9zYXZlLCBhbmQgL2dhbWVjb21tYW5kdG91c2VyLCB3aGljaCBp"
    "cyB0aGUgcmVsYXkKICAgICAgICAjY2FycnlpbmcgdGhlIGFjdHVhbCBpbi1nYW1lIHRyYWZmaWMg"
    "YmV0d2VlbiBwbGF5ZXJzLiBfbG9iYnlIYW5kbGUKICAgICAgICAjYWxyZWFkeSBkb2N1bWVudHMg"
    "dGhpcyBzYW1lIHRyYXAgZm9yIHRoZSByZWFkIGxvb3A7IHRoZSBsb29wIGJlbG93CiAgICAgICAg"
    "I3NpbXBseSBsZWF2ZXMgdGhlIHNvY2tldCBibG9ja2luZyBhbmQgd2FpdHMgd2l0aCBzZWxlY3Qo"
    "KSBpbnN0ZWFkLgogICAgICAgIHJlYWR5LCBfLCBfID0gc2VsZWN0LnNlbGVjdChbY29uLnJlcXVl"
    "c3RdLCBbXSwgW10sIHJlbWFpbmluZykKICAgICAgICBpZiBub3QgcmVhZHk6CiAgICAgICAgICAg"
    "IGNvbnRpbnVlICNkZWFkbGluZSBpcyByZS1jaGVja2VkIGF0IHRoZSB0b3Agb2YgdGhlIGxvb3AK"
    "ICAgICAgICBjaHVuayA9IGNvbi5yZXF1ZXN0LnJlY3YoUkVDVl9CVUZfTEVOKQogICAgICAgIGlm"
    "IG5vdCBjaHVuazoKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3IoJ2Rpc2Nv"
    "bm5lY3RlZCBkdXJpbmcgYmxvYiByZWFkJykKICAgICAgICBjb24uZGF0YSArPSBjaHVuawogICAg"
    "YmxidWYgPSBjb24uZGF0YVswOnNpemVdCiAgICBjb24uZGF0YSA9IGNvbi5kYXRhW3NpemU6XQog"
    "ICAgcmV0dXJuIGJsYnVmCgojQ29tbWFuZCBmdW5jdGlvbnMKX1JFX0hFUk9fUE9TID0gcmUuY29t"
    "cGlsZShyJ15bMC05QS1GYS1mXXsxLDh9I1swLTlBLUZhLWZdezEsOH0kJykKZGVmIF9oZXJvUG9z"
    "KHJhdyk6CiAgICAjLT4gInh4eHgjeXl5eSIgb3IgTm9uZS4KICAgICMgVGhlIGNsaWVudCBzZW5k"
    "cyBlaXRoZXIgInh4eHgjeXl5eSIgb3IgIlVJRCN4eHh4I3l5eXkiLCBidXQgdXBkYXRlUG9zKCkK"
    "ICAgICMgdW5jb25kaXRpb25hbGx5IHByZWZpeGVzIHRoZSBzZW5kZXIncyBpZCB3aGVuIGl0IGZh"
    "bnMgdGhlIHBvc2l0aW9uIG91dC4KICAgICMgU3RvcmluZyB0aGUgcmF3IGZpZWxkIG1lYW50IHRo"
    "ZSBzZWNvbmQgZm9ybSB3ZW50IGJhY2sgb3V0IGFzCiAgICAjICJVSUQjVUlEI3h4eHgjeXl5eSIs"
    "IHdoaWNoIG5vIGNsaWVudCBjYW4gbWF0Y2ggdG8gYSBwbGF5ZXI6IHRoYXQgaGVybydzCiAgICAj"
    "IG1hcmtlciB0aGVuIHN0YXllZCB3aGVyZXZlciBpdCB3YXMgbGFzdCBzdWNjZXNzZnVsbHkgcGFy"
    "c2VkIHdoaWxlIHRoZQogICAgIyBwbGF5ZXIgYWN0dWFsbHkgd2Fsa2VkIGF3YXkuIEtlZXAgb25s"
    "eSB0aGUgdHJhaWxpbmcgY29vcmRpbmF0ZSBwYWlyIHNvCiAgICAjIGV4YWN0bHkgb25lIGlkIGlz"
    "IHByZXNlbnQgb24gdGhlIHdpcmUgcmVnYXJkbGVzcyBvZiB3aGF0IHdhcyBzZW50LgogICAgIyBB"
    "bnl0aGluZyB0aGF0IGlzIG5vdCBhIHBhaXIgb2YgaGV4IG51bWJlcnMgaXMgZGlzY2FyZGVkIHJh"
    "dGhlciB0aGFuCiAgICAjIHN0b3JlZDogdGhpcyB2YWx1ZSBpcyBjb3BpZWQgdmVyYmF0aW0gaW50"
    "byBhIGJyb2FkY2FzdCBldmVyeSBvdGhlciBjbGllbnQKICAgICMgaW4gdGhlIHRvd24gaGFzIHRv"
    "IHBhcnNlLCBzbyBhIHNpbmdsZSBqdW5rIGZpZWxkIGZyb20gb25lIGNsaWVudAogICAgIyAoYSB0"
    "cnVuY2F0ZWQgcGFja2V0LCBhIG1vZGlmaWVkIGNsaWVudCkgYmVjYW1lIGV2ZXJ5b25lIGVsc2Un"
    "cyBwcm9ibGVtLgogICAgcG9zID0gJyMnLmpvaW4oc3RyKHJhdykuc3BsaXQoJyMnKVstMjpdKQog"
    "ICAgcmV0dXJuIHBvcyBpZiBfUkVfSEVST19QT1MubWF0Y2gocG9zKSBlbHNlIE5vbmUKZGVmIF9u"
    "b3AobWQsdXNyLHJlcyk6CiAgICByZXR1cm4gTm9uZQpkZWYgX3VwZGhlcm9wb3MobWQsdXNyLHJl"
    "cyk6CiAgICBpZiBub3QgdXNyLnVzZXIuZ2FtZWNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUg"
    "I25vdCBpbiBhIGdhbWUgY2hhbm5lbCwgaWdub3JlCiAgICBwb3MgPSBfaGVyb1BvcyhyZXNbMV0p"
    "CiAgICBpZiBwb3MgaXMgTm9uZToKICAgICAgICByZXR1cm4gTm9uZSAjdW5wYXJzZWFibGUgY29v"
    "cmRpbmF0ZXMsIHNlZSBfaGVyb1BvcwogICAgdXNyLnVzZXIucG9zZGF0YSA9IHBvcwogICAgdXNy"
    "LnVzZXIuZ2FtZWNoYW5uZWwuZGlydHkgPSBUcnVlCiAgICB1c3IudXNlci5wb3NjaGFuZ2VkID0g"
    "VHJ1ZQogICAgcmV0dXJuIE5vbmUgI25vIHJlc3BvbnNlCmRlZiBfc2V0cGxheWVyZGF0YShtZCx1"
    "c3IscmVzKToKICAgIHBkID0gX1JlYWRCbG9iKHVzciwgcmVzWzNdKQogICAgI1RPRE8gQ0hFQ0sg"
    "cGVybWlzc2lvbnMgZm9yIHNldERhdGEoc2VsZiBvciBvdGhlcikKICAgIGlmIHJlc1sxXSA9PSB1"
    "c3IudXNlci5uYW1lOgogICAgICAgIEdESC5zZXRQbGF5ZXJEYXRhKHJlc1sxXSwgcmVzWzJdLCBw"
    "ZCkKICAgICNUT0RPIGhhbmRsZSByZW1haW5pbmcgdmFsdWVzCiAgICAjcmVzW3hdOgogICAgIzA6"
    "IC9zZXRwbGF5ZXJkYXRhCiAgICAjMTogbmFtZQogICAgIzI6IGZvcm0KICAgICMzOiBibG9ic2l6"
    "ZQogICAgIzQ6IHVua25vd24gKHBvaW50cz8pCiAgICAjNTogdW5rbm93biwgMSAoYm9vbD8pCiAg"
    "ICByZXR1cm4gTm9uZQpkZWYgX2dldHBsYXllcmRhdGEobWQsdXNyLHJlcyk6CiAgICAjVE9ETyBj"
    "aGVjayBwZXJtaXNzaW9uIGZvciBnZXREYXRhKHNlbGYgb3Igb3RoZXIpCiAgICBpZiByZXNbMV0g"
    "PT0gdXNyLnVzZXIubmFtZToKICAgICAgICBwZCA9IEdESC5nZXRQbGF5ZXJEYXRhKHJlc1sxXSwg"
    "cmVzWzJdKQogICAgICAgICNwcmludCgnT2J0YWluZWQgUGxheWVyZGF0YScsIGxlbihwZCkpCiAg"
    "ICAgICAgcmV0dXJuIF9lbShmJy9nZXRwbGF5ZXJkYXRhICJ7cmVzWzFdfSIgIntyZXNbMl19IiB7"
    "bGVuKHBkKX0nKStwZAogICAgI3ByaW50KCdBY2Nlc3MgRXJyb3InLHVzci51c2VyLm5hbWUsICdD"
    "YW5cJ3QgZ2V0IHBsYXllcmRhdGEgZm9yJyxyZXNbMV0pCiAgICByZXR1cm4gTm9uZQpkZWYgX2xl"
    "YXZlZ2FtZWNoYW5uZWwobWQsdXNyLHJlcyk6CiAgICBjaG5sID0gdXNyLnVzZXIuZ2FtZWNoYW5u"
    "ZWwKICAgIGlmIGNobmw6CiAgICAgICAgY2hubC5sZWF2ZUNoYW5uZWwodXNyKQogICAgcmV0dXJu"
    "IHVzci5zZXJ2ZXIuc3RhdGUuZW51bWVyYXRlR0MoKQojLS0tIGNvbW1hbmRzIHRha2VuIGZyb20g"
    "dGhlIGNsaWVudCdzIG93biBvdXRnb2luZyB0YWJsZSAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQoj"
    "VGhlIGZpdmUgaGFuZGxlcnMgYmVsb3cgZXhpc3QgYmVjYXVzZSB0aGUgZm9ybWF0IHRhYmxlIGNv"
    "bXBpbGVkIGludG8gdGhlIHJldGFpbAojY2xpZW50IChFTkNsaWVudC5jcHAsIHJlY292ZXJlZCBm"
    "cm9tIEdhbWVIZWxwZXIuZGxsIGluIHRoZSAxLjMgU0RLKSBsaXN0cyB0aGVtCiNhbmQgdGhpcyBz"
    "ZXJ2ZXIgaGFkIG5vIGVudHJ5IGZvciBhbnkgb2YgdGhlbS4gQW4gdW5yZWdpc3RlcmVkIGNvbW1h"
    "bmQgaXMgbm90CiNpZ25vcmVkIGdyYWNlZnVsbHk6IHBhcnNlKCkgbG9ncyAnVU5LTk9XTiBDT01N"
    "QU5EJyBhbmQgcmV0dXJucyBub3RoaW5nLCBhbmQgYQojY2xpZW50IHdhaXRpbmcgb24gYW4gYW5z"
    "d2VyIHdhaXRzIGZvcmV2ZXIuIFRoYXQgaXMgdGhlIHNhbWUgc2hhcGUgYXMgZXZlcnkgaGFuZwoj"
    "YWxyZWFkeSB0cmFja2VkIGRvd24gaW4gdGhpcyBmaWxlLgojVGhlIGNsaWVudCBzZW5kcywgdmVy"
    "YmF0aW0gZnJvbSB0aGF0IHRhYmxlOgojICAgIC9nYW1lY2hhbm5lbHNsaXN0CiMgICAgL2pvaW5j"
    "aGF0Y2hhbm5lbCAiJVMiICIlUyIgIiVkIgojICAgIC9tc2cgIi4uLgojICAgIC9zZXRnYW1lcGFy"
    "YW1zICIlcyIgIiVzIgojICAgIC9uZXdnYW1laG9zdCAiJXMiCmRlZiBfZ2FtZWNoYW5uZWxzbGlz"
    "dChtZCx1c3IscmVzKToKICAgICNQbGFpbiAid2hhdCB0b3ducyBhcmUgdGhlcmU/Ii4gZW51bWVy"
    "YXRlR0MoKSBhbHJlYWR5IGJ1aWxkcyBleGFjdGx5IHRoaXMKICAgICNhbnN3ZXIgLSBpdCB3YXMg"
    "b25seSBldmVyIHNlbnQgYXMgdGhlIHJlcGx5IHRvIC9sZWF2ZWdhbWVjaGFubmVsLCBzbyBhCiAg"
    "ICAjY2xpZW50IHRoYXQgYXNrZWQgZGlyZWN0bHkgZ290IHNpbGVuY2UgYW5kIGFuIGVtcHR5IHRv"
    "d24gbGlzdC4KICAgIHJldHVybiB1c3Iuc2VydmVyLnN0YXRlLmVudW1lcmF0ZUdDKCkKZGVmIF9q"
    "b2luY2hhdGNoYW5uZWwobWQsdXNyLHJlcyk6CiAgICAjKGNoYW5uZWwsIHBhc3N3b3JkLCBmbGFn"
    "KS4gam9pbkNoYXQoKSBhbHJlYWR5IHJldHVybnMgdGhlIGZ1bGwgcmVwbHkgdGhlCiAgICAjY2xp"
    "ZW50IGV4cGVjdHMgLSB0aGUgam9pbiBjb25maXJtYXRpb24gcGx1cyB0aGUgcm9zdGVyIC0gYW5k"
    "IHdhcyBvbmx5CiAgICAjcmVhY2hhYmxlIGFzIGEgc2lkZSBlZmZlY3Qgb2YgZW50ZXJpbmcgYSB0"
    "b3duLCBzbyB0aGUgc2Vjb25kIGNoYXQgY2hhbm5lbAogICAgIyhUcmFkZSkgY291bGQgbmV2ZXIg"
    "YmUgam9pbmVkOiB0aGUgY29tbWFuZCB0byBzd2l0Y2ggd2FzIHVuaGFuZGxlZC4KICAgICNUaGUg"
    "cGFzc3dvcmQgaXMgYWNjZXB0ZWQgYW5kIGlnbm9yZWQsIGFzIGV2ZXJ5d2hlcmUgZWxzZSBpbiB0"
    "aGlzIGZpbGU7IHRoZQogICAgI3RyYWlsaW5nIGludGVnZXIncyBtZWFuaW5nIGlzIG5vdCBrbm93"
    "biBhbmQgbm90aGluZyBoZXJlIGRlcGVuZHMgb24gaXQuCiAgICBjaG5sID0gdXNyLnVzZXIuZ2Ft"
    "ZWNoYW5uZWwKICAgIGlmIG5vdCBjaG5sOgogICAgICAgIHJldHVybiBOb25lICNub3QgaW4gYSB0"
    "b3duLCBub3RoaW5nIHRvIGpvaW4KICAgIG5hbWUgPSBzYW5pdGl6ZVRleHQocmVzWzFdLCBfTUFY"
    "X0NIQVROQU1FKS5zdHJpcCgpCiAgICBpZiBub3QgbmFtZToKICAgICAgICByZXR1cm4gTm9uZQog"
    "ICAgaWYgbmFtZSBub3QgaW4gY2hubC5jaGF0Q2hhbm5lbHM6CiAgICAgICAgI1RoZSBjbGllbnQg"
    "aGFzIGEgImNyZWF0ZSBjaGF0IGNoYW5uZWwiIGNvbnRyb2wgb2YgaXRzIG93bgogICAgICAgICMo"
    "SURDX0NSRUFURUNIQVRDSEFOTkVMIGluIHRoZSBTREsncyBEaWFsb2dzUmVzb3VyY2UuaCkgYW5k"
    "IG5vIHNlcGFyYXRlCiAgICAgICAgI2NvbW1hbmQgZm9yIGl0LCBzbyBqb2luaW5nIGEgbmFtZSB0"
    "aGF0IGRvZXMgbm90IGV4aXN0IHlldCAqaXMqIGhvdyBhCiAgICAgICAgI2NoYW5uZWwgZ2V0cyBj"
    "cmVhdGVkLiBSZWZ1c2luZyBsZWZ0IHRoYXQgYnV0dG9uIGRvaW5nIG5vdGhpbmcgYnV0IGhhbmcK"
    "ICAgICAgICAjdGhlIGRpYWxvZy4gQ2FwcGVkLCBiZWNhdXNlIHRoZSBuYW1lIGlzIHBsYXllci1z"
    "dXBwbGllZCBhbmQgdGhlc2UKICAgICAgICAjb3V0bGl2ZSB0aGUgcGxheWVyIHdobyBtYWRlIHRo"
    "ZW0uCiAgICAgICAgaWYgbGVuKGNobmwuY2hhdENoYW5uZWxzKSA+PSBfTUFYX0NIQVRfQ0hBTk5F"
    "TFM6CiAgICAgICAgICAgIHByaW50KGYnKioqIHt1c3IudXNlci5uYW1lfSBjb3VsZCBub3QgY3Jl"
    "YXRlIGNoYXQgY2hhbm5lbCB7bmFtZSFyfTogJwogICAgICAgICAgICAgICAgICBmJ3Rvd24gYWxy"
    "ZWFkeSBoYXMge2xlbihjaG5sLmNoYXRDaGFubmVscyl9JykKICAgICAgICAgICAgcmV0dXJuIE5v"
    "bmUKICAgICAgICBjaG5sLmNoYXRDaGFubmVsc1tuYW1lXSA9IFtdCiAgICAgICAgcHJpbnQoZidb"
    "TG9iYnldIHt1c3IudXNlci5uYW1lfSBjcmVhdGVkIGNoYXQgY2hhbm5lbCAie25hbWV9IiBpbiB7"
    "Y2hubC5uYW1lfScpCiAgICAgICAgI0V2ZXJ5b25lIGJyb3dzaW5nIHRoZSB0b3duIGdldHMgdGhl"
    "IHJlZnJlc2hlZCBjaGFubmVsIGxpc3QsIG90aGVyd2lzZQogICAgICAgICN0aGUgbmV3IGNoYW5u"
    "ZWwgaXMgaW52aXNpYmxlIHRvIGFsbCBidXQgaXRzIGNyZWF0b3IuCiAgICAgICAgbWQuYWRkKHsn"
    "dGFyZ2V0JzpsaXN0KGNobmwudXNlcmxpc3QpLCdtZXNzYWdlJzpjaG5sLmVudW1DaGF0cygpfSkK"
    "ICAgIHJldHVybiBjaG5sLmpvaW5DaGF0KHVzciwgbmFtZSwgcmVzWzJdIGlmIGxlbihyZXMpPjIg"
    "ZWxzZSAnJykKZGVmIF9tc2cobWQsdXNyLHJlcyk6CiAgICAjUHJpdmF0ZSBtZXNzYWdlLiBSZWxh"
    "eWVkIGluIHRoZSBzYW1lIHNoYXBlIC9zZW5kIHVzZXMgLSAiPHNlbmRlcj4iIHRoZW4gdGhlCiAg"
    "ICAjdGV4dCAtIGJlY2F1c2UgdGhhdCBpcyB0aGUgb25lIHR3by1maWVsZCB0ZXh0IG1lc3NhZ2Ug"
    "dGhpcyBjbGllbnQgaXMga25vd24KICAgICN0byByZW5kZXIuIFRoZSBleGFjdCBzZXJ2ZXItPmNs"
    "aWVudCBzcGVsbGluZyBmb3IgYSBwcml2YXRlIG1lc3NhZ2UgaGFzIG5vdAogICAgI2JlZW4gY2Fw"
    "dHVyZWQ7IGlmIGEgc2Vzc2lvbiBsb2cgZXZlciBzaG93cyB0aGUgY2xpZW50IG1pc2hhbmRsaW5n"
    "IGl0LCB0aGlzCiAgICAjaXMgdGhlIGxpbmUgdG8gcmV2aXNpdC4gRG9pbmcgbm90aGluZyB3YXMg"
    "bm90IHRoZSBzYWZlciBvcHRpb246IGl0IGlzIHdoYXQKICAgICN0aGUgc2VydmVyIGRpZCB1bnRp"
    "bCBub3csIGFuZCBwcml2YXRlIG1lc3NhZ2VzIHNpbXBseSB2YW5pc2hlZC4KICAgIGlmIGxlbihy"
    "ZXMpPDM6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIHRhcmdldCA9IHJlc1sxXQogICAgdGV4dCA9"
    "IHNhbml0aXplVGV4dChyZXNbMl0sIF9NQVhfQ0hBVF9URVhUKQogICAgaWYgbm90IHRleHQ6CiAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgIHRjb24gPSB1c3Iuc2VydmVyLmdldFBsYXllcih0YXJnZXQp"
    "CiAgICBpZiB0Y29uIGlzIE5vbmU6CiAgICAgICAgcmV0dXJuIE5vbmUgI3JlY2lwaWVudCBvZmZs"
    "aW5lCiAgICB0Y29uLnNlbmQoX2VtKGYnL21zZyAie3Vzci51c2VyLm5hbWV9IiAie3RleHR9Iicp"
    "KQogICAgcmV0dXJuIE5vbmUKZGVmIF9zZXRnYW1lcGFyYW1zKG1kLHVzcixyZXMpOgogICAgI1R3"
    "byBzdHJpbmdzIHdob3NlIG1lYW5pbmcgaXMgbm90IGRvY3VtZW50ZWQgYW55d2hlcmUgYXZhaWxh"
    "YmxlLCBzbyBub3RoaW5nCiAgICAjaXMgKmNoYW5nZWQqIG9uIHRoZSBzdHJlbmd0aCBvZiBhIGd1"
    "ZXNzIC0gdGhlIHJvb20ncyBzdG9yZWQgcGFyYW1ldGVycyBhcmUKICAgICNsZWZ0IGV4YWN0bHkg"
    "YXMgaXRzIC9jcmVhdGVnYW1lIHNldCB0aGVtLiBXaGF0IHRoaXMgZG9lcyBidXkgaXMgdGhhdCB0"
    "aGUKICAgICNjb21tYW5kIHN0b3BzIGJlaW5nIGFuIHVua25vd24gb25lLCBhbmQgZXZlcnlvbmUg"
    "YnJvd3NpbmcgZ2V0cyBhIHJlZnJlc2hlZAogICAgIyRnYW1lIGVudHJ5LCB3aGljaCBpcyBhIG1l"
    "c3NhZ2UgdGhlIGNsaWVudCBhbHJlYWR5IGhhbmRsZXMuIFRoZSByYXcKICAgICNhcmd1bWVudHMg"
    "YXJlIGxvZ2dlZCBzbyBhIHJlYWwgc2Vzc2lvbiBjYW4gc2V0dGxlIHdoYXQgdGhleSBtZWFuLgog"
    "ICAgZ20gPSB1c3IudXNlci5nYW1lCiAgICBpZiBnbSBpcyBOb25lIG9yIGdtLmhvc3QgaXMgbm90"
    "IHVzcjoKICAgICAgICByZXR1cm4gTm9uZSAjb25seSB0aGUgcm9vbSdzIG93biBob3N0IG1heSB0"
    "b3VjaCBpdHMgcGFyYW1ldGVycwogICAgcHJpbnQoZidbTG9iYnldIHt1c3IudXNlci5uYW1lfSAv"
    "c2V0Z2FtZXBhcmFtcyBmb3IgIntnbS5nbmFtZX0iOiAnCiAgICAgICAgICBmJ3tyZXNbMV0hcn0g"
    "e3Jlc1syXSFyfSAocmVjb3JkZWQsIG5vdCBhcHBsaWVkKScpCiAgICBtc2cgPSBnbS5nZXRHYW1l"
    "U3RyaW5nKCkKICAgIGlmIG1zZzoKICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOmdtLl9hdWRpZW5j"
    "ZSgpLCdtZXNzYWdlJzptc2d9KQogICAgcmV0dXJuIE5vbmUKZGVmIF9uZXdnYW1laG9zdChtZCx1"
    "c3IscmVzKToKICAgICNBIGZyZXNoIHgtZGlyZWN0cGxheSBVUkwgZm9yIGEgcm9vbSB0aGF0IGFs"
    "cmVhZHkgZXhpc3RzLiBJdCBjYXJyaWVzIHRoZQogICAgI2hvc3QncyBvd24gaWRlYSBvZiBpdHMg"
    "YWRkcmVzcywgd2hpY2ggYmVoaW5kIGEgcm91dGVyIGlzIGEgTEFOIGFkZHJlc3Mgbm8KICAgICNq"
    "b2luZXIgY2FuIHJlYWNoIC0gdGhlIHNhbWUgcHJvYmxlbSAvY3JlYXRlZ2FtZSBoYXMsIGFuZCBp"
    "dCBtdXN0IGdldCB0aGUKICAgICNzYW1lIHRyZWF0bWVudCwgb3IgYSByb29tIHdob3NlIGhvc3Qg"
    "cmUtYWR2ZXJ0aXNlcyBzaWxlbnRseSBiZWNvbWVzCiAgICAjdW5qb2luYWJsZSB3aGlsZSBzdGls"
    "bCBiZWluZyBsaXN0ZWQuCiAgICBnbSA9IHVzci51c2VyLmdhbWUKICAgIGlmIGdtIGlzIE5vbmUg"
    "b3IgZ20uaG9zdCBpcyBub3QgdXNyOgogICAgICAgIHJldHVybiBOb25lICNvbmx5IHRoZSBob3N0"
    "IGRlc2NyaWJlcyB3aGVyZSB0aGUgZ2FtZSBpcwogICAgcGVlciA9IHVzci5jbGllbnRfYWRkcmVz"
    "c1swXSBpZiB1c3IuY2xpZW50X2FkZHJlc3MgZWxzZSAnJwogICAgKHVybCwgbm90ZSkgPSByZXdy"
    "aXRlR2FtZUhvc3QocmVzWzFdLCBwZWVyKQogICAgZ20udXJsID0gdXJsCiAgICBwcmludChmJ1tM"
    "b2JieV0ge3Vzci51c2VyLm5hbWV9IG1vdmVkIHJvb20gIntnbS5nbmFtZX0iOiB7bm90ZX0nKQog"
    "ICAgcHJpbnQoZidbTG9iYnldICAgdXJsIGFkdmVydGlzZWQgdG8gam9pbmVyczoge2dtLnVybH0n"
    "KQogICAgbXNnID0gZ20uZ2V0R2FtZVN0cmluZygpCiAgICBpZiBtc2c6CiAgICAgICAgbWQuYWRk"
    "KHsndGFyZ2V0JzpnbS5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6bXNnfSkKICAgIHJldHVybiBOb25l"
    "CmRlZiBfcmVxdWVzdGpvaW5nYW1lY2hhbm5lbChtZCx1c3IscmVzKToKICAgIGNobmwgPSB1c3Iu"
    "c2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy5nZXQocmVzWzFdKQogICAgaWYgY2hubCBpcyBOb25l"
    "OgogICAgICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpvaW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0i"
    "ICIwIicpICN1bmtub3duIGNoYW5uZWwKICAgICNUT0RPIGNoZWNrIHBlcm1pc3Npb25zPwogICAg"
    "aWYgY2hubC5yZXF1ZXN0Sm9pbih1c3IpOgogICAgICAgIHJldHVybiBfZW0oZicvcmVxdWVzdGpv"
    "aW5nYW1lY2hhbm5lbCAie3Jlc1sxXX0iICIxIicpCiAgICByZXR1cm4gX2VtKGYnL3JlcXVlc3Rq"
    "b2luZ2FtZWNoYW5uZWwgIntyZXNbMV19IiAiMCInKQpkZWYgX2pvaW5nYW1lY2hhbm5lbChtZCx1"
    "c3IscmVzKToKICAgIGNobmwgPSB1c3Iuc2VydmVyLnN0YXRlLmdhbWVDaGFubmVscy5nZXQocmVz"
    "WzFdKQogICAgaWYgY2hubCBpcyBOb25lOgogICAgICAgIHJldHVybiBOb25lICN1bmtub3duIGNo"
    "YW5uZWwsIGlnbm9yZQogICAgaWYgbGVuKHJlcyk+MjoKICAgICAgICBwb3MgPSBfaGVyb1Bvcyhy"
    "ZXNbMl0pCiAgICAgICAgaWYgcG9zIGlzIG5vdCBOb25lOgogICAgICAgICAgICB1c3IudXNlci5w"
    "b3NkYXRhID0gcG9zCiAgICByZXR1cm4gY2hubC5qb2luQ2hhbm5lbCh1c3IsIHJlc1sxXSkKZGVm"
    "IF9zZXR1c2VyaGVyb2RhdGEobWQsdXNyLHJlcyk6CiAgICBwZCA9IF9SZWFkQmxvYih1c3IsIHJl"
    "c1syXSkKICAgIGlmIGxlbihwZCkgPiBfTUFYX0hFUk9EQVRBOgogICAgICAgICNVbmxpa2UgL3Nl"
    "dHBsYXllcmRhdGEsIHdoaWNoIGlzIHdyaXR0ZW4gdG8gZGlzayBhbmQgcmVhZCBiYWNrIGJ5IGl0"
    "cwogICAgICAgICNvd25lciBhbG9uZSwgaGVyb2RhdGEgaXMgcmUtYnJvYWRjYXN0IHRvIGV2ZXJ5"
    "IG90aGVyIHBsYXllciBpbiB0aGUgdG93bgogICAgICAgICNvbiBldmVyeSBqb2luIGFuZCBvbiBl"
    "dmVyeSBjaGFuZ2UuIEF0IHRoZSBnZW5lcmFsIF9NQVhfQkxPQiBjZWlsaW5nIG9uZQogICAgICAg"
    "ICNjbGllbnQgY291bGQgaGFuZCB0aGUgc2VydmVyIDE2IE1CIGFuZCBoYXZlIGl0IGZhbm5lZCBv"
    "dXQgZmlmdHkgdGltZXMsCiAgICAgICAgI3doaWNoIGJsb3dzIHBhc3QgZXZlcnkgcmVjaXBpZW50"
    "J3Mgc2VuZC1iYWNrbG9nIGNhcCBhbmQgZHJvcHMgdGhlIHdob2xlCiAgICAgICAgI3Rvd24gaW5z"
    "dGVhZCBvZiB0aGUgY2xpZW50IHRoYXQgZGlkIGl0LiBSZWFsIGhlcm8gYXBwZWFyYW5jZSBkYXRh"
    "IGlzIGEKICAgICAgICAjZmV3IGtpbG9ieXRlcy4KICAgICAgICByYWlzZSBQcm90b2NvbEVycm9y"
    "KGYnaGVyb2RhdGEgYmxvYiBvZiB7bGVuKHBkKX0gYnl0ZXMgZXhjZWVkcyAnCiAgICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICBmJ3tfTUFYX0hFUk9EQVRBfScpCiAgICB1c3IudXNlci5oZXJvZGF0"
    "YSA9IHBkCiAgICBpZiB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAgICBtc2cgPSB1c3IudXNl"
    "ci5nZXRHQ1Vtc2coKQogICAgICAgIHRnID0gX3dvVXNlcih1c3IudXNlci5nYW1lY2hhbm5lbC51"
    "c2VybGlzdCwgdXNyKQogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6dGcsJ21lc3NhZ2UnOm1zZ30p"
    "CiAgICByZXR1cm4gTm9uZQpkZWYgX3NlbmQobWQsdXNyLHJlcyk6CiAgICBpZiBub3QgdXNyLnVz"
    "ZXIuY2hhdGNoYW5uZWw6CiAgICAgICAgcmV0dXJuIE5vbmUKICAgIGlmIGxlbihyZXMpPDI6CiAg"
    "ICAgICAgcmV0dXJuIE5vbmUKICAgIHRleHQgPSBzYW5pdGl6ZVRleHQocmVzWzFdLCBfTUFYX0NI"
    "QVRfVEVYVCkKICAgIGlmIG5vdCB0ZXh0OgogICAgICAgIHJldHVybiBOb25lCiAgICBpZiBfQURN"
    "SU5TIGFuZCB0ZXh0LnN0YXJ0c3dpdGgoX0FETUlOX1BSRUZJWCk6CiAgICAgICAgI05ldmVyIHJl"
    "bGF5ZWQgdG8gdGhlIGNoYW5uZWwsIHdob2V2ZXIgdHlwZWQgaXQuIEZvciBhbiBhZG1pbiB0aGF0"
    "CiAgICAgICAgI2tlZXBzIHRoZSBzZXJ2ZXIncyBidXNpbmVzcyBvZmYgdGhlIHB1YmxpYyBjaGF0"
    "OyBmb3IgZXZlcnlib2R5IGVsc2UgaXQKICAgICAgICAjc3RvcHMgdGhlIHJvb20gbGVhcm5pbmcg"
    "d2hpY2ggY29tbWFuZHMgZXhpc3QgYnkgd2F0Y2hpbmcgc29tZW9uZSBndWVzcwogICAgICAgICNh"
    "dCB0aGVtLgogICAgICAgICNUaGUgYF9BRE1JTlMgYW5kYCBndWFyZCBtYXR0ZXJzOiB3aXRoIG5v"
    "IGFkbWlucyBjb25maWd1cmVkIHRoZSBjb25zb2xlCiAgICAgICAgI2lzIG1lYW50IHRvIGJlIG9m"
    "ZiBvdXRyaWdodCwgYnV0IHRoaXMgYnJhbmNoIHN0aWxsIGF0ZSBldmVyeSBjaGF0IGxpbmUKICAg"
    "ICAgICAjdGhhdCBoYXBwZW5lZCB0byBzdGFydCB3aXRoICchJyAtIHNvIG9uIGEgZGVmYXVsdCBz"
    "ZXJ2ZXIgIiEhISIgb3IKICAgICAgICAjIiHRg9GA0LAiIHNpbXBseSBuZXZlciByZWFjaGVkIHRo"
    "ZSByb29tLCB3aXRoIG5vdGhpbmcgb24gc2NyZWVuIHRvIHNheQogICAgICAgICN3aHkuIFdpdGgg"
    "bm8gYWRtaW5zIHRoZXJlIGlzIG5vIGNvbnNvbGUsIHNvIHRoZXJlIGlzIG5vdGhpbmcgdG8gaGlk"
    "ZQogICAgICAgICNhbmQgdGhlIGxpbmUgaXMgb3JkaW5hcnkgY2hhdC4KICAgICAgICByZXR1cm4g"
    "YWRtaW5Db21tYW5kKHVzciwgdGV4dFtsZW4oX0FETUlOX1BSRUZJWCk6XS5zdHJpcCgpKQogICAg"
    "dWwgPSB1c3IudXNlci5jaGF0Y2hhbm5lbAogICAgbWQuYWRkKHsndGFyZ2V0Jzp1bCwnbWVzc2Fn"
    "ZSc6X2VtKGYnL3NlbmQgInt1c3IudXNlci5uYW1lfSIgInt0ZXh0fSInKX0pCiAgICByZXR1cm4g"
    "Tm9uZQojLS0tIGluLWdhbWUgYWRtaW4gY29uc29sZSAtLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0t"
    "LS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLQojVHlwZWQgaW50byB0aGUgZ2FtZSdzIG93biBj"
    "aGF0IGJveCwgc28gaXQgbmVlZHMgbm8gY2xpZW50IG1vZGlmaWNhdGlvbiBhdCBhbGw6CiN0aGUg"
    "cmV0YWlsIGNsaWVudCBhbHJlYWR5IHNlbmRzIGV2ZXJ5dGhpbmcgdHlwZWQgdGhlcmUgYXMgL3Nl"
    "bmQsIGFuZCBhbHJlYWR5CiNyZW5kZXJzICcvYWRtaW4gPHRleHQ+JyBjb21pbmcgYmFjayB0aGUg"
    "b3RoZXIgd2F5ICh0aGF0IGlzIGhvdyBhIGtpY2sgbm90aWNlCiNyZWFjaGVzIGEgcGxheWVyKS4g"
    "Qm90aCBoYWx2ZXMgYXJlIHRoZXJlZm9yZSBrbm93bi1nb29kIG1lc3NhZ2Ugc2hhcGVzLCB3aGlj"
    "aAojaXMgd2hhdCBtYWtlcyB0aGlzIHNhZmUgb24gYSAyMDA4IGJpbmFyeSAtIG5vdGhpbmcgbmV3"
    "IGlzIGludmVudGVkIG9uIHRoZSB3aXJlLgojT25seSBhY2NvdW50cyBsaXN0ZWQgYXMgQWRtaW5z"
    "IGluIENvbmZpZy5pbmkgYXJlIG9iZXllZC4gRXZlcnlvbmUgZWxzZSdzCiNjb21tYW5kcyBhcmUg"
    "c3dhbGxvd2VkIHNpbGVudGx5IHJhdGhlciB0aGFuIGFuc3dlcmVkLCBzbyB0aGUgcHJlc2VuY2Ug"
    "b2YgdGhlCiNjb25zb2xlIGlzIG5vdCBhZHZlcnRpc2VkIHRvIHRoZSByb29tLgpkZWYgX2FkbWlu"
    "UmVwbHkodXNyLCBsaW5lcyk6CiAgICAjT25lIC9hZG1pbiBwZXIgbGluZTogdGhlIGNsaWVudCB0"
    "cmVhdHMgZWFjaCBhcyBpdHMgb3duIHNlcnZlciBtZXNzYWdlLCBhbmQKICAgICNhIHNpbmdsZSBs"
    "b25nIGxpbmUgd291bGQgcnVuIGludG8gdGhlIHdpcmUtbGVuZ3RoIGxpbWl0IGFueXdheS4KICAg"
    "IG91dCA9IGInJwogICAgZm9yIGxpbmUgaW4gbGluZXM6CiAgICAgICAgb3V0ICs9IF9lbShmJy9h"
    "ZG1pbiB7c2FuaXRpemVUZXh0KHN0cihsaW5lKSwgX01BWF9DSEFUX1RFWFQpfScpCiAgICByZXR1"
    "cm4gb3V0IG9yIE5vbmUKZGVmIF9mbXRQbGF5ZXJzKHNlcnZlcik6CiAgICByb3dzID0gW10KICAg"
    "IGZvciAobmFtZSwgY29uKSBpbiBzb3J0ZWQoc2VydmVyLnN0YXRlLmFjdGl2ZVVzZXJzLml0ZW1z"
    "KCkpOgogICAgICAgIHRvd24gPSBjb24udXNlci5nYW1lY2hhbm5lbC5uYW1lLnNwbGl0KCcjJylb"
    "MF0gaWYgY29uLnVzZXIuZ2FtZWNoYW5uZWwgZWxzZSAnLScKICAgICAgICBnYW1lID0gY29uLnVz"
    "ZXIuZ2FtZS5nbmFtZSBpZiBjb24udXNlci5nYW1lIGVsc2UgJy0nCiAgICAgICAgcm93cy5hcHBl"
    "bmQoZid7bmFtZX0gIHRvd246e3Rvd259ICByb29tOntnYW1lfScpCiAgICByZXR1cm4gcm93cyBv"
    "ciBbJ25vYm9keSBvbmxpbmUnXQpkZWYgYWRtaW5Db21tYW5kKHVzciwgbGluZSk6CiAgICBzZXJ2"
    "ZXIgPSB1c3Iuc2VydmVyCiAgICB3aG8gPSB1c3IudXNlci5uYW1lCiAgICBpZiB3aG8uY2FzZWZv"
    "bGQoKSBub3QgaW4gX0FETUlOUzoKICAgICAgICBwcmludChmJ1tMb2JieV0ge3dob30gdHJpZWQg"
    "YW4gYWRtaW4gY29tbWFuZCB3aXRob3V0IGJlaW5nIGFuIGFkbWluOiB7bGluZSFyfScpCiAgICAg"
    "ICAgcmV0dXJuIE5vbmUKICAgIHBhcnRzID0gbGluZS5zcGxpdChOb25lLCAxKQogICAgY21kID0g"
    "cGFydHNbMF0ubG93ZXIoKSBpZiBwYXJ0cyBlbHNlICcnCiAgICBhcmcgPSBwYXJ0c1sxXS5zdHJp"
    "cCgpIGlmIGxlbihwYXJ0cykgPiAxIGVsc2UgJycKICAgIHByaW50KGYnW0xvYmJ5XSBBRE1JTiB7"
    "d2hvfToge2xpbmUhcn0nKQogICAgZ2xvYmFsIF9QT1NfVVBEQVRFX0haLCBfSURMRV9USU1FT1VU"
    "LCBfU0VORF9OT1BTLCBERUZBVUxUX01PVEQKICAgIGlmIGNtZCBpbiAoJ2hlbHAnLCAnPycsICcn"
    "KToKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbCiAgICAgICAgICAgIGYne19BRE1J"
    "Tl9QUkVGSVh9d2hvIC0gd2hvIGlzIG9ubGluZScsCiAgICAgICAgICAgIGYne19BRE1JTl9QUkVG"
    "SVh9c2F5IDx0ZXh0PiAtIGFubm91bmNlIHRvIGV2ZXJ5b25lJywKICAgICAgICAgICAgZid7X0FE"
    "TUlOX1BSRUZJWH1raWNrIDxuYW1lPicsCiAgICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9bW90"
    "ZCA8dGV4dD4nLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfWh6IDwwLjUte19QT1NfVVBE"
    "QVRFX0haX01BWH0+IC0gcG9zaXRpb24gc3luYyByYXRlJywKICAgICAgICAgICAgZid7X0FETUlO"
    "X1BSRUZJWH1pZGxlIDxzZWNvbmRzLCAwPW9mZj4nLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJF"
    "RklYfWtlZXBhbGl2ZSBvbnxvZmYnLAogICAgICAgICAgICBmJ3tfQURNSU5fUFJFRklYfXN0YXR1"
    "cycsCiAgICAgICAgICAgIGYne19BRE1JTl9QUkVGSVh9c2F2ZSAtIHdyaXRlIHRoZXNlIHNldHRp"
    "bmdzIHRvIENvbmZpZy5pbmknLAogICAgICAgIF0pCiAgICBpZiBjbWQgPT0gJ3dobyc6CiAgICAg"
    "ICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgX2ZtdFBsYXllcnMoc2VydmVyKSkKICAgIGlmIGNt"
    "ZCA9PSAnc3RhdHVzJzoKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbCiAgICAgICAg"
    "ICAgIGYncGxheWVycyB7bGVuKHNlcnZlci5zdGF0ZS5hY3RpdmVVc2Vycyl9LCAnCiAgICAgICAg"
    "ICAgIGYnaHoge19QT1NfVVBEQVRFX0hafSwgaWRsZSB7X0lETEVfVElNRU9VVH1zLCAnCiAgICAg"
    "ICAgICAgIGYna2VlcGFsaXZlIHsib24iIGlmIF9TRU5EX05PUFMgZWxzZSAib2ZmIn0nLAogICAg"
    "ICAgIF0pCiAgICBpZiBjbWQgPT0gJ3NheSc6CiAgICAgICAgaWYgbm90IGFyZzoKICAgICAgICAg"
    "ICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgWydzYXkgd2hhdD8nXSkKICAgICAgICBtc2cgPSBf"
    "ZW0oZicvYWRtaW4ge3Nhbml0aXplVGV4dChhcmcsIF9NQVhfQ0hBVF9URVhUKX0nKQogICAgICAg"
    "IHNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6bGlzdChzZXJ2ZXIuc3RhdGUuYWN0aXZlVXNlcnMu"
    "dmFsdWVzKCkpLCdtZXNzYWdlJzptc2d9KQogICAgICAgIHJldHVybiBOb25lICN0aGUgYW5ub3Vu"
    "Y2VtZW50IGl0c2VsZiBpcyB0aGUgYWRtaW4ncyBjb25maXJtYXRpb24KICAgIGlmIGNtZCA9PSAn"
    "a2ljayc6CiAgICAgICAgaWYgbm90IGFyZzoKICAgICAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5"
    "KHVzciwgWydraWNrIHdobz8nXSkKICAgICAgICBpZiBhcmcuY2FzZWZvbGQoKSA9PSB3aG8uY2Fz"
    "ZWZvbGQoKToKICAgICAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgWydraWNraW5nIHlv"
    "dXJzZWxmIGlzIG5vdCBhIHBsYW4nXSkKICAgICAgICBvayA9IHNlcnZlci5raWNrUGxheWVyKGFy"
    "ZywgZidLaWNrZWQgYnkge3dob30nKQogICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFtm"
    "J2tpY2tlZCB7YXJnfScgaWYgb2sgZWxzZSBmJ3thcmd9IGlzIG5vdCBvbmxpbmUnXSkKICAgIGlm"
    "IGNtZCA9PSAnbW90ZCc6CiAgICAgICAgaWYgbm90IGFyZzoKICAgICAgICAgICAgcmV0dXJuIF9h"
    "ZG1pblJlcGx5KHVzciwgWydtb3RkIG5lZWRzIHNvbWUgdGV4dCddKQogICAgICAgIERFRkFVTFRf"
    "TU9URCA9IGFyZwogICAgICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFsnbW90ZCBzZXQgKHNo"
    "b3duIGF0IHRoZSBuZXh0IGxvZ2luKSddKQogICAgaWYgY21kID09ICdoeic6CiAgICAgICAgdHJ5"
    "OgogICAgICAgICAgICBoeiA9IGZsb2F0KGFyZykKICAgICAgICBleGNlcHQgVmFsdWVFcnJvcjoK"
    "ICAgICAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5KHVzciwgWydoeiBuZWVkcyBhIG51bWJlcidd"
    "KQogICAgICAgICNDbGFtcGVkIGV4YWN0bHkgYXMgYXBwbHlDb25maWcoKSBkb2VzIC0gb25lIHJ1"
    "bGUsIG9uZSBwbGFjZSB0byBjaGFuZ2UuCiAgICAgICAgX1BPU19VUERBVEVfSFogPSBtaW4obWF4"
    "KGh6LCAwLjUpLCBfUE9TX1VQREFURV9IWl9NQVgpCiAgICAgICAgcmV0dXJuIF9hZG1pblJlcGx5"
    "KHVzciwgW2YncG9zaXRpb24gc3luYyBub3cge19QT1NfVVBEQVRFX0hafS9zJ10pCiAgICBpZiBj"
    "bWQgPT0gJ2lkbGUnOgogICAgICAgIHRyeToKICAgICAgICAgICAgX0lETEVfVElNRU9VVCA9IG1h"
    "eCgwLCBpbnQoYXJnKSkKICAgICAgICBleGNlcHQgVmFsdWVFcnJvcjoKICAgICAgICAgICAgcmV0"
    "dXJuIF9hZG1pblJlcGx5KHVzciwgWydpZGxlIG5lZWRzIGEgd2hvbGUgbnVtYmVyIG9mIHNlY29u"
    "ZHMnXSkKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbZidpZGxlIHRpbWVvdXQgbm93"
    "IHtfSURMRV9USU1FT1VUfXMnXSkKICAgIGlmIGNtZCA9PSAna2VlcGFsaXZlJzoKICAgICAgICBp"
    "ZiBhcmcubG93ZXIoKSBub3QgaW4gKCdvbicsICdvZmYnKToKICAgICAgICAgICAgcmV0dXJuIF9h"
    "ZG1pblJlcGx5KHVzciwgWydrZWVwYWxpdmUgb258b2ZmJ10pCiAgICAgICAgX1NFTkRfTk9QUyA9"
    "IGFyZy5sb3dlcigpID09ICdvbicKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbZidr"
    "ZWVwYWxpdmUgeyJvbiIgaWYgX1NFTkRfTk9QUyBlbHNlICJvZmYifSddKQogICAgaWYgY21kID09"
    "ICdzYXZlJzoKICAgICAgICAjRXZlcnl0aGluZyBhYm92ZSBjaGFuZ2VzIHRoZSBsaXZlIHNlcnZl"
    "ciBvbmx5LiBUaGlzIGlzIHRoZSBvbmUgY29tbWFuZAogICAgICAgICN0aGF0IHRvdWNoZXMgdGhl"
    "IGZpbGUsIHNvIGEgc2Vzc2lvbiBvZiBleHBlcmltZW50cyBjYW5ub3QgYmUgbWFkZQogICAgICAg"
    "ICNwZXJtYW5lbnQgYnkgYWNjaWRlbnQuCiAgICAgICAgY2ZnID0gbG9hZENvbmZpZygpCiAgICAg"
    "ICAgc2VjID0gY2ZnWydzZXJ2ZXInXQogICAgICAgIHNlY1snTU9URCddID0gX2VzY2FwZU1PVEQo"
    "REVGQVVMVF9NT1REKQogICAgICAgIHNlY1snUG9zaXRpb25VcGRhdGVIeiddID0gc3RyKF9QT1Nf"
    "VVBEQVRFX0haKQogICAgICAgIHNlY1snSWRsZVRpbWVvdXQnXSA9IHN0cihfSURMRV9USU1FT1VU"
    "KQogICAgICAgIHNlY1snS2VlcGFsaXZlJ10gPSBzdHIoX1NFTkRfTk9QUykKICAgICAgICBzYXZl"
    "Q29uZmlnKGNmZykKICAgICAgICByZXR1cm4gX2FkbWluUmVwbHkodXNyLCBbJ3NhdmVkIHRvIENv"
    "bmZpZy5pbmknXSkKICAgIHJldHVybiBfYWRtaW5SZXBseSh1c3IsIFtmJ3Vua25vd24gY29tbWFu"
    "ZCB7Y21kIXJ9IC0gdHJ5IHtfQURNSU5fUFJFRklYfWhlbHAnXSkKZGVmIF9nZXRndWlsZHJhbmtw"
    "b2ludHMobWQsdXNyLHJlcyk6CiAgICAoYSxiLGMsZCkgPSBfZ3JwKCkKICAgIHJldHVybiBfZW0o"
    "ZicvZ2V0Z3VpbGRyYW5rcG9pbnRzICJ7YX0iICJ7Yn0iICJ7Y30iICJ7ZH0iJykKCiMjIEdVSUxE"
    "UwojR3VpbGQgY3JlYXRpb24gZGlkIG5vdGhpbmcgYXQgYWxsIGJlZm9yZSB0aGlzOiB0aGVyZSB3"
    "YXMgbm8gL2NyZWF0ZWd1aWxkIChvcgojYW55dGhpbmcgZWxzZSBndWlsZC1yZWxhdGVkKSBpbiBf"
    "Q09NTUFORFMsIHNvIHRoZSBjbGllbnQncyByZXF1ZXN0IGZlbGwKI3Rocm91Z2ggdG8gdGhlICJV"
    "bmtub3duIENvbW1hbmQiIGJyYW5jaCBvZiBDb21tYW5kUGFyc2VyLnBhcnNlIGFuZCB3YXMKI2Ry"
    "b3BwZWQuIFRoZSBjbGllbnQgZ290IG5vIHJlcGx5LCBubyBlcnJvciwgYW5kIG5vIGd1aWxkLgoj"
    "Tk9URSBPTiBDT01NQU5EIE5BTUVTOiB0aGUgZXhhY3Qgd2lyZSBuYW1lcyB0aGUgcmV0YWlsIGNs"
    "aWVudCB1c2VzIGZvciB0aGUKI2d1aWxkIFVJIGFyZSBub3QgZG9jdW1lbnRlZCBhbnl3aGVyZSB3"
    "ZSBoYXZlLiBUaGUgaGFuZGxlcnMgYmVsb3cgYXJlCiNyZWdpc3RlcmVkIHVuZGVyIGV2ZXJ5IHNw"
    "ZWxsaW5nIHRoYXQgZml0cyB0aGlzIHByb3RvY29sJ3MgY29udmVudGlvbnMsIGFsbAojcm91dGVk"
    "IHRvIHRoZSBzYW1lIGltcGxlbWVudGF0aW9uLCBzbyB3aGljaGV2ZXIgb25lIHRoZSBjbGllbnQg"
    "YWN0dWFsbHkKI3NlbmRzIGlzIHNlcnZlZC4gcGFyc2UoKSBub3cgbG9ncyB0aGUgcmF3IHRleHQg"
    "b2YgYW55dGhpbmcgc3RpbGwgdW5tYXRjaGVkLAojd2hpY2ggaXMgaG93IHRvIGNvbmZpcm0vdHJp"
    "bSB0aGlzIGxpc3QgZnJvbSBhIHJlYWwgc2Vzc2lvbidzIGxvZy4KZGVmIF90ZXN0Y3JlYXRlZ3Vp"
    "bGQobWQsdXNyLHJlcyk6CiAgICAjQ29uZmlybWVkIGZyb20gYSBsaXZlIGNsaWVudCBjYXB0dXJl"
    "OiBvcGVuaW5nIHRoZSBndWlsZCBzY3JlZW4gc2VuZHMKICAgICMvZ3VpbGRzbGFkZGVyLCBhbmQg"
    "dHlwaW5nIGEgbmFtZSBhbmQgcHJlc3NpbmcgY3JlYXRlIHNlbmRzCiAgICAjL3Rlc3RjcmVhdGVn"
    "dWlsZCAiPG5hbWU+Ii4gVGhlIGNsaWVudCB0aGVuIHdhaXRzIGZvciB0aGUgc2VydmVyIHRvIHNh"
    "eQogICAgI3doZXRoZXIgdGhhdCBuYW1lIGNhbiBiZSB1c2VkIC0gd2l0aCBubyBhbnN3ZXIgaXQg"
    "d2FpdHMgZm9yZXZlciwgd2hpY2ggaXMKICAgICN3aGF0IHRoZSAiZ3VpbGQgY3JlYXRpb24gaGFu"
    "Z3MiIHJlcG9ydCB3YXMuIEV2ZXJ5IGd1aWxkIGNvbW1hbmQgbmFtZQogICAgI2d1ZXNzZWQgYmVm"
    "b3JlIHRoaXMgY2FwdHVyZSAoIC9jcmVhdGVndWlsZCwgL2pvaW5ndWlsZCwgLi4uICkgd2FzIHdy"
    "b25nOwogICAgI3RoaXMgb25lIGNvbWVzIGZyb20gdGhlIHdpcmUuCiAgICBuYW1lID0gc2FuaXRp"
    "emVUZXh0KHJlc1sxXSkuc3RyaXAoKQogICAgZnJlZSA9IDEgaWYgR0RILmd1aWxkTmFtZUZyZWUo"
    "bmFtZSkgZWxzZSAwCiAgICBwcmludChmJ1tMb2JieV0ge3Vzci51c2VyLm5hbWV9IGNoZWNrZWQg"
    "Z3VpbGQgbmFtZSAie25hbWV9IjogJwogICAgICAgICAgZid7ImF2YWlsYWJsZSIgaWYgZnJlZSBl"
    "bHNlICJyZWplY3RlZCJ9JykKICAgICNFY2hvLXBsdXMtZmxhZywgdGhlIHNhbWUgc2hhcGUgdGhl"
    "IGNsaWVudCBhbHJlYWR5IGFjY2VwdHMgZnJvbQogICAgIy9yZXF1ZXN0am9pbmdhbWVjaGFubmVs"
    "ICgiMSIgZ28gYWhlYWQgLyAiMCIgbm8pLgogICAgcmV0dXJuIF9lbShmJy90ZXN0Y3JlYXRlZ3Vp"
    "bGQgIntuYW1lfSIgIntmcmVlfSInKQpkZWYgX2d1aWxkc2xhZGRlcihtZCx1c3IscmVzKToKICAg"
    "ICNTZW50IHdoZW4gdGhlIGd1aWxkIHNjcmVlbiBvcGVucy4gVGhlIGxheW91dCBvZiBhbiBpbmRp"
    "dmlkdWFsIGxhZGRlcgogICAgI2VudHJ5IGlzIG5vdCBrbm93biwgYW5kIHRoaXMgY2xpZW50IGlz"
    "IGZyYWdpbGUgZW5vdWdoIHRoYXQgaW52ZW50aW5nIG9uZQogICAgI3Jpc2tzIHRha2luZyBpdCBk"
    "b3duIC0gc28gdGhlIGFuc3dlciBpcyBhbiBob25lc3QgZW1wdHkgbGFkZGVyLCB3aGljaCBpcwog"
    "ICAgI2Fsc28gdGhlIHRydXRoZnVsIG9uZSB1bnRpbCBndWlsZHMgY2FuIGFjdHVhbGx5IGJlIGNy"
    "ZWF0ZWQuIFRoZSBjb3VudAogICAgI2NvbWVzIGxhc3QsIG1hdGNoaW5nIC9qb2luZ2FtZWNoYW5u"
    "ZWwncyBlY2hvLXBsdXMtY291bnQgcmVwbHkuCiAgICBwYWdlID0gc2FuaXRpemVUZXh0KHJlc1sx"
    "XSkgaWYgbGVuKHJlcykgPiAxIGVsc2UgJzEnCiAgICByZXR1cm4gX2VtKGYnL2d1aWxkc2xhZGRl"
    "ciAie3BhZ2V9IiAiMCInKQpkZWYgX2xhZGRlcihtZCx1c3IscmVzKToKICAgICNTZWVuIG9uY2Ug"
    "b24gdGhlIHdpcmUsIHJpZ2h0IGFmdGVyIGEgc3VjY2Vzc2Z1bCAvam9pbmd1aWxkLCB3aXRoIG5v"
    "CiAgICAjYXJndW1lbnRzIGNhcHR1cmVkIC0gcHJvYmFibHkgYSBzZXJ2ZXItd2lkZSBsZWFkZXJi"
    "b2FyZCByYXRoZXIgdGhhbiBhCiAgICAjZ3VpbGQgb25lLiBJdHMgcmVwbHkgc2hhcGUgaXMgbm90"
    "IGtub3duLiBFdmVyeSBvdGhlciBjb21tYW5kIGluIHRoaXMKICAgICNmaWxlIHRoYXQgcmVhY2hl"
    "ZCB0aGlzIHN0YXRlIHdhcyBhbnN3ZXJlZCBieSBtYXRjaGluZyBhIHNoYXBlIHRoZSBjbGllbnQK"
    "ICAgICNoYWQgYWxyZWFkeSBiZWVuIHNlZW4gYWNjZXB0aW5nIGVsc2V3aGVyZSAoZWNobytmbGFn"
    "LCBlY2hvK2NvdW50KTsgdGhlcmUKICAgICNpcyBubyBzdWNoIHByZWNlZGVudCBmb3IgdGhpcyBv"
    "bmUuIEd1ZXNzaW5nIGEgZmllbGQgbGF5b3V0IHJpc2tzIGZlZWRpbmcKICAgICN0aGlzIGNsaWVu"
    "dCBkYXRhIGl0IGRvZXMgbm90IGV4cGVjdCwgYW5kIGl0IGhhcyBhbHJlYWR5IHNob3duIGl0c2Vs"
    "ZgogICAgI3dpbGxpbmcgdG8gY3Jhc2ggb24gYmFkIGlucHV0IHJhdGhlciB0aGFuIHJlamVjdCBp"
    "dCBncmFjZWZ1bGx5IC0gYSB3b3JzZQogICAgI291dGNvbWUgdGhhbiBhIFVJIGVsZW1lbnQgdGhh"
    "dCBzdGF5cyBlbXB0eS4gUmVnaXN0ZXJlZCBzbyBpdCBzdG9wcwogICAgI3Nob3dpbmcgdXAgYXMg"
    "YW4gdW5rbm93biBjb21tYW5kOyBkZWxpYmVyYXRlbHkgYW5zd2VyZWQgd2l0aCBub3RoaW5nCiAg"
    "ICAjdW50aWwgYSBjYXB0dXJlIHNob3dzIHdoYXQgcmVwbHkgaXQgYWN0dWFsbHkgd2FpdHMgZm9y"
    "LgogICAgcHJpbnQoZidbTG9iYnldIHt1c3IudXNlci5uYW1lfSBzZW50IC9sYWRkZXIge3Jlc1sx"
    "Ol0hcn0gLSBub3QgYW5zd2VyZWQsICcKICAgICAgICAgZidzaGFwZSB1bmtub3duIChzZWUgY29t"
    "bWVudCBhYm92ZSBfbGFkZGVyKScpCiAgICByZXR1cm4gTm9uZQpkZWYgX2pvaW5ndWlsZChtZCx1"
    "c3IscmVzKToKICAgICNDYXB0dXJlZCBmcm9tIHRoZSByZXRhaWwgY2xpZW50OiBhZnRlciAvdGVz"
    "dGNyZWF0ZWd1aWxkIGFuc3dlcnMgdGhhdCBhCiAgICAjbmFtZSBpcyBmcmVlLCB0aGUgY2xpZW50"
    "IGNyZWF0ZXMgdGhlIGd1aWxkIGJ5IHNlbmRpbmcKICAgICMvam9pbmd1aWxkICI8bmFtZT4iICIx"
    "IiAiMSIuIFNvIHRoaXMgb25lIGNvbW1hbmQgY292ZXJzIGJvdGggY3JlYXRpbmcgYW5kCiAgICAj"
    "am9pbmluZywgYW5kIHdoaWNoIGl0IGlzIGZvbGxvd3MgZnJvbSB3aGV0aGVyIHRoZSBndWlsZCBh"
    "bHJlYWR5IGV4aXN0cyAtCiAgICAjdGhlIHRyYWlsaW5nIGZsYWdzIGFyZSBub3QgbmVlZGVkIHRv"
    "IHRlbGwgdGhlbSBhcGFydC4gQW5zd2VyaW5nIG5vdGhpbmcKICAgICNoZXJlIGlzIHdoYXQgbGVm"
    "dCB0aGUgZ3VpbGQgZGlhbG9nIHNwaW5uaW5nLgogICAgbmFtZSA9IHNhbml0aXplVGV4dChyZXNb"
    "MV0pLnN0cmlwKCkKICAgIGlmIEdESC5ndWlsZEV4aXN0cyhuYW1lKToKICAgICAgICBlcnIgPSBH"
    "REguam9pbkd1aWxkKG5hbWUsIHVzci51c2VyLm5hbWUpCiAgICAgICAgYWN0aW9uID0gJ2pvaW5l"
    "ZCcKICAgIGVsc2U6CiAgICAgICAgZXJyID0gR0RILmNyZWF0ZUd1aWxkKG5hbWUsIHVzci51c2Vy"
    "Lm5hbWUpICN2YWxpZGF0ZXMgdGhlIG5hbWUgaXRzZWxmCiAgICAgICAgYWN0aW9uID0gJ2ZvdW5k"
    "ZWQnCiAgICBpZiBlcnI6CiAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciB7ZXJyfSAie25hbWV9"
    "IicpCiAgICAjQ2Fub25pY2FsIHNwZWxsaW5nIGZyb20gdGhlIGRhdGFiYXNlLCB3aGljaCBtYXkg"
    "ZGlmZmVyIGluIGNhc2UgZnJvbSB3aGF0CiAgICAjd2FzIHR5cGVkLgogICAgbmFtZSA9IEdESC5n"
    "ZXRHdWlsZE5hbWUodXNyLnVzZXIubmFtZSkgb3IgbmFtZQogICAgdXNyLnVzZXIuZ3VpbGQgPSBz"
    "YW5pdGl6ZVRleHQobmFtZSkKICAgIHByaW50KGYnW0xvYmJ5XSB7dXNyLnVzZXIubmFtZX0ge2Fj"
    "dGlvbn0gZ3VpbGQgIntuYW1lfSInKQogICAgI1JlLWFubm91bmNlIHRoZSBwbGF5ZXIgdG8gdGhl"
    "aXIgdG93biBzbyB0aGUgb3RoZXJzIHBpY2sgdXAgdGhlIG5ldyB0YWcKICAgICN3aXRob3V0IHJl"
    "bG9nZ2luZy4gVGhpcyByZXVzZXMgJGdhbWVjaGFubmVsdXNlciAtIGEgbWVzc2FnZSBmb3JtYXQg"
    "dGhlCiAgICAjY2xpZW50IGRlbW9uc3RyYWJseSBhY2NlcHRzIC0gcmF0aGVyIHRoYW4gaW52ZW50"
    "aW5nIGEgZ3VpbGQtc3BlY2lmaWMgb25lLgogICAgY2hubCA9IHVzci51c2VyLmdhbWVjaGFubmVs"
    "CiAgICBpZiBjaG5sOgogICAgICAgIG1kLmFkZCh7J3RhcmdldCc6X3dvVXNlcihjaG5sLnVzZXJs"
    "aXN0LCB1c3IpLAogICAgICAgICAgICAgICAgJ21lc3NhZ2UnOnVzci51c2VyLmdldEdDVW1zZygp"
    "fSkKICAgICNFY2hvIHBsdXMgbWVtYmVyIGNvdW50LCB0aGUgc2hhcGUgL2pvaW5nYW1lY2hhbm5l"
    "bCBhbHJlYWR5IHJlcGxpZXMgd2l0aC4KICAgIHJldHVybiBfZW0oZicvam9pbmd1aWxkICJ7bmFt"
    "ZX0iICJ7bGVuKEdESC5nZXRHdWlsZE1lbWJlcnMobmFtZSkpfSInKQojVGhlIHJvb20gbmFtZSBp"
    "cyB0eXBlZCBieSBhIHBsYXllciBhbmQgaXMgdGhlbiBicm9hZGNhc3QgdG8gZXZlcnlvbmUgYnJv"
    "d3NpbmcKI3RoZSB0b3duIGluc2lkZSBhIHF1b3RlZCAkZ2FtZSBmaWVsZC4gSXQgd2FzIHBhc3Nl"
    "ZCB0aHJvdWdoIHVudG91Y2hlZDogYSAnIicgaW4KI2l0IGZvcmdlZCBwcm90b2NvbCBmaWVsZHMg"
    "Zm9yIGV2ZXJ5IG90aGVyIGNsaWVudCwgYW5kIGl0cyBsZW5ndGggd2FzIHVuYm91bmRlZC4KI0Jv"
    "dGggaGFuZGxlcnMgbXVzdCBmb2xkIGl0IGlkZW50aWNhbGx5IC0gdGhlIG5hbWUgaXMgYWxzbyB0"
    "aGUgZGljdGlvbmFyeSBrZXkKI3RoZSBjcmVhdGUgcmVxdWVzdCBpcyBsYXRlciBtYXRjaGVkIGFn"
    "YWluc3QsIHNvIGFueSBkaWZmZXJlbmNlIGJldHdlZW4gdGhlbQojd291bGQgdHVybiBhIGxlZ2l0"
    "aW1hdGUgY3JlYXRpb24gaW50byAiZ2FtZU5hbWVUYWtlbiIuCmRlZiBfZ2FtZU5hbWUocmF3KToK"
    "ICAgIHJldHVybiBzYW5pdGl6ZVRleHQocmF3LCBfTUFYX0dBTUVOQU1FKQpkZWYgX3JlcXVlc3Rj"
    "cmVhdGVnYW1lKG1kLHVzcixyZXMpOgogICAgaWYgbm90IHVzci51c2VyLmdhbWVjaGFubmVsOgog"
    "ICAgICAgIHJldHVybiBOb25lICNub3QgaW4gYSBnYW1lIGNoYW5uZWwgLSB1c2VkIHRvIHJhaXNl"
    "IEF0dHJpYnV0ZUVycm9yIG9uCiAgICAgICAgICAgICAgICAgICAgI05vbmUgYW5kIGtpbGwgdGhl"
    "IGNvbm5lY3Rpb24ncyBoYW5kbGVyIHRocmVhZAogICAgcmV0dXJuIHVzci51c2VyLmdhbWVjaGFu"
    "bmVsLnJlcXVlc3RDcmVhdGVHYW1lKHVzciwgX2dhbWVOYW1lKHJlc1sxXSkpCmRlZiBfY3JlYXRl"
    "R2FtZShtZCx1c3IscmVzKToKICAgIGlmIG5vdCB1c3IudXNlci5nYW1lY2hhbm5lbDoKICAgICAg"
    "ICByZXR1cm4gTm9uZSAjc2VlIF9yZXF1ZXN0Y3JlYXRlZ2FtZQogICAgcmV0dXJuIHVzci51c2Vy"
    "LmdhbWVjaGFubmVsLmNyZWF0ZUdhbWUoX2dhbWVOYW1lKHJlc1sxXSksIHVzciwgcmVzWzJdLCBy"
    "ZXNbM10sIHJlc1s0XSwgcmVzWzVdLCByZXNbNl0sIHJlc1s3XSwgcmVzWzhdLCByZXNbOV0pCmRl"
    "ZiBfc3RvcGdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiB1c3IudXNlci5nYW1lOgogICAgICAgIHJl"
    "dHVybiB1c3IudXNlci5nYW1lLnJlbW92ZSh1c3IpCiAgICAjcHJpbnQoJ1VzZXIgaXMgbm90IGlu"
    "IGEgZ2FtZScpCiAgICByZXR1cm4gTm9uZQpkZWYgX3N0YXJ0aW5nZ2FtZShtZCx1c3IscmVzKToK"
    "ICAgIGlmIHVzci51c2VyLmdhbWU6CiAgICAgICAgcmV0dXJuIHVzci51c2VyLmdhbWUuc3RhcnRH"
    "YW1lKHVzcikKICAgIHJldHVybiBOb25lICNUT0RPIHdoYXQgZG9lcyB0aGlzIGV2ZW4gZG8/CmRl"
    "ZiBfc3RhcnRnYW1lKG1kLHVzcixyZXMpOgogICAgI1RPRE8gaGFuZGxlIHByb3Blcmx5CiAgICBp"
    "ZiB1c3IudXNlci5nYW1lOgogICAgICAgIHBhc3MKICAgIHJldHVybiBOb25lCmRlZiBfZ2FtZWNv"
    "bW1hbmR0b3VzZXIobWQsdXNyLHJlcyk6CiAgICBkYXQgPSBfUmVhZEJsb2IodXNyLCByZXNbMl0p"
    "CiAgICB0Y29uID0gdXNyLnNlcnZlci5nZXRQbGF5ZXIocmVzWzFdKQogICAgI0FsbG93IGNvbW1h"
    "bmRzIHRvIGFueSBjb25uZWN0ZWQgcGxheWVyLCByZWdhcmRsZXNzIG9mIHN0YXRlLCB0byBzdXBw"
    "b3J0IG1vZGRlZCB1c2VzCiAgICBpZiBub3QgdGNvbjoKICAgICAgICAjcHJpbnQoJ1BsYXllcjon"
    "LHJlc1sxXSwnZG9lcyBub3QgZXhpc3Q/JykKICAgICAgICByZXR1cm4gTm9uZQogICAgI1RPRE8g"
    "Y29uc2lkZXIgb3B0aW1pc2luZyB0aGlzIGNvbW1hbmQgaW4gcGFydGljdWxhcgogICAgZnVsbXNn"
    "ID0gX2VtKGYnL2dhbWVjb21tYW5kdG91c2VyICJ7dXNyLnVzZXIubmFtZX0iICJ7bGVuKGRhdCl9"
    "IicpK2RhdAogICAgI1N0cmFpZ2h0IG9udG8gdGhlIHJlY2lwaWVudCdzIG93biBvdXRib3VuZCBx"
    "dWV1ZSBpbnN0ZWFkIG9mIHZpYSB0aGUKICAgICNzZXJ2ZXItd2lkZSBNZXNzYWdlRGlzdHJpYnV0"
    "b3IuIFRoaXMgaXMgdGhlIGNvbW1hbmQgdGhhdCBjYXJyaWVzIHRoZQogICAgI2FjdHVhbCBpbi1n"
    "YW1lIHRyYWZmaWMgYmV0d2VlbiB0d28gcGxheWVycywgaXQgYWx3YXlzIGhhcyBleGFjdGx5IG9u"
    "ZQogICAgI3JlY2lwaWVudCwgYW5kIHNlbmQoKSBpcyBqdXN0IGEgcXVldWUgcHV0IC0gc28gdGhl"
    "IGRpc3RyaWJ1dG9yIGhvcCBib3VnaHQKICAgICNub3RoaW5nIGJ1dCBsYXRlbmN5LiBXb3JzZSwg"
    "dGhhdCBzaW5nbGUgZGlzdHJpYnV0b3IgdGhyZWFkIGlzIHNoYXJlZCBieQogICAgI2V2ZXJ5IGNv"
    "bm5lY3Rpb24gb24gdGhlIHNlcnZlcjogb25lIHNsb3cgZmFuLW91dCAoYSBwb3NpdGlvbiBicm9h"
    "ZGNhc3QgdG8KICAgICNhIGZ1bGwgdG93biwgYSBoZXJvZGF0YSBibG9iKSBxdWV1ZWQgYWhlYWQg"
    "b2YgYSBnYW1lIGNvbW1hbmQgZGVsYXllZCBpdAogICAgI2ZvciBldmVyeW9uZS4gRGlyZWN0IGhh"
    "bmQtb2ZmIHJlbW92ZXMgYm90aCB0aGUgZXh0cmEgdGhyZWFkIHdha2UtdXAgYW5kCiAgICAjdGhh"
    "dCBoZWFkLW9mLWxpbmUgYmxvY2tpbmcsIGFuZCByZWxheSBvcmRlciBiZXR3ZWVuIGFueSBnaXZl"
    "biBwYWlyIG9mCiAgICAjcGxheWVycyBpcyBzdGlsbCBwcmVzZXJ2ZWQgYmVjYXVzZSB0aGV5IGFs"
    "bCB0YWtlIHRoaXMgc2FtZSBwYXRoLgogICAgdGNvbi5zZW5kKGZ1bG1zZykKICAgIHJldHVybiBO"
    "b25lCmRlZiBfam9pbmdhbWUobWQsdXNyLHJlcyk6CiAgICBpZiBub3QgdXNyLnVzZXIuZ2FtZWNo"
    "YW5uZWw6CiAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciB1bmtub3duR2FtZSAie3Jlc1sxXX0i"
    "JykgI25vdCBpbiBhIGdhbWUgY2hhbm5lbAogICAgZ20gPSB1c3IudXNlci5nYW1lY2hhbm5lbC5n"
    "YW1lcy5nZXQoX2dhbWVOYW1lKHJlc1sxXSksTm9uZSkKICAgIGlmIGdtID09IE5vbmU6CiAgICAg"
    "ICAgI0Fuc3dlciwgZG9uJ3QgaWdub3JlOiB0aGUgY2xpZW50IGlzIHNpdHRpbmcgb24gYSAiY29u"
    "bmVjdGluZyIgZGlhbG9nCiAgICAgICAgI3RoYXQgb25seSBhIHJlcGx5IGRpc21pc3Nlcy4gSGFw"
    "cGVucyB3aGVuZXZlciB0aGUgcm9vbSBpcyB0b3JuIGRvd24KICAgICAgICAjYmV0d2VlbiB0aGUg"
    "cGxheWVyIHNlZWluZyBpdCBpbiB0aGUgbGlzdCBhbmQgY2xpY2tpbmcgaXQuCiAgICAgICAgcmV0"
    "dXJuIF9lbShmJy9lcnJvciB1bmtub3duR2FtZSAie3Jlc1sxXX0iJykKICAgICNUaGUgcGFzc3dv"
    "cmQgYXJndW1lbnQgaXMgYWJzZW50IHdoZW4gdGhlIHJvb20gaGFzIG5vbmUgLSBzZWUgdGhlIGFy"
    "aXR5CiAgICAjbm90ZSBvbiBfQ09NTUFORFMuCiAgICByZXR1cm4gZ20uYWRkVXNlcih1c3IsIHJl"
    "c1syXSBpZiBsZW4ocmVzKT4yIGVsc2UgJycpCmRlZiBfd2hvaXMobWQsdXNyLHJlcyk6CiAgICBp"
    "ZiBsZW4ocmVzKTwyOgogICAgICAgIHJldHVybiBOb25lCiAgICB0YXJnZXQgPSByZXNbMV0KICAg"
    "IGluZm8gPSBHREguZ2V0V2hvaXModGFyZ2V0KQogICAgaWYgaW5mbyBpcyBOb25lOgogICAgICAg"
    "IHJldHVybiBOb25lICN1bmtub3duIHVzZXIKICAgIHRjb24gPSB1c3Iuc2VydmVyLmdldFBsYXll"
    "cih0YXJnZXQpCiAgICB0b3duID0gdGNvbi51c2VyLmdhbWVjaGFubmVsLm5hbWUgaWYgKHRjb24g"
    "YW5kIHRjb24udXNlci5nYW1lY2hhbm5lbCkgZWxzZSAnJwogICAgY2hhdGNoYW5uZWwgPSAnJwog"
    "ICAgaWYgdGNvbiBhbmQgdGNvbi51c2VyLmNoYXRjaGFubmVsOgogICAgICAgICNUaGUgdGFyZ2V0"
    "J3MgY2hhdCBjaGFubmVsIGlzIGEgcGxhaW4gbGlzdCwgc28gaXQgaXMgaWRlbnRpZmllZCBieQog"
    "ICAgICAgICNzZWFyY2hpbmcgZm9yIHRoZSBvYmplY3QuIFN0b3AgYXQgdGhlIGZpcnN0IG1hdGNo"
    "IGluc3RlYWQgb2Ygd2Fsa2luZwogICAgICAgICNldmVyeSBjaGFubmVsIG9mIGV2ZXJ5IHRvd24g"
    "YWZ0ZXJ3YXJkcyAtIGFuZCB0YWtlIHRoZSBuYW1lIGZyb20gdGhlCiAgICAgICAgI3Rvd24gdGhl"
    "IHBsYXllciBpcyBhY3R1YWxseSBpbiwgd2hpY2ggdGhlIHVuYnJva2VuIGxvb3AgY291bGQgb3Zl"
    "cndyaXRlCiAgICAgICAgI3dpdGggYSBsYXRlciB0b3duJ3MgaWRlbnRpY2FsbHktbmFtZWQgY2hh"
    "bm5lbC4KICAgICAgICBmb3IgY2huIGluIGxpc3QodXNyLnNlcnZlci5zdGF0ZS5nYW1lQ2hhbm5l"
    "bHMudmFsdWVzKCkpOgogICAgICAgICAgICBmb3IgY25hbWUsIHVsaXN0IGluIGxpc3QoY2huLmNo"
    "YXRDaGFubmVscy5pdGVtcygpKToKICAgICAgICAgICAgICAgIGlmIHVsaXN0IGlzIHRjb24udXNl"
    "ci5jaGF0Y2hhbm5lbDoKICAgICAgICAgICAgICAgICAgICBjaGF0Y2hhbm5lbCA9IGNuYW1lCiAg"
    "ICAgICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgaWYgY2hhdGNoYW5uZWw6CiAgICAg"
    "ICAgICAgICAgICBicmVhawogICAgZ3VpbGQgPSBzYW5pdGl6ZVRleHQoR0RILmdldEd1aWxkTmFt"
    "ZSh0YXJnZXQpKQogICAgI0NhcHBlZCBhZ2FpbiBvbiB0aGUgd2F5IG91dCwgbm90IG9ubHkgb24g"
    "dGhlIHdheSBpbjogcm93cyB3cml0dGVuIGJlZm9yZQogICAgIy91cGRhdGUgd2FzIGJvdW5kZWQg"
    "YXJlIHN0aWxsIGluIHRoZSBkYXRhYmFzZSwgYW5kIHRoaXMgaXMgdGhlIG1lc3NhZ2UgdGhhdAog"
    "ICAgI2hhbmRzIHRoZW0gdG8gYSAqZGlmZmVyZW50KiBwbGF5ZXIncyBjbGllbnQuCiAgICByZXR1"
    "cm4gX2VtKAogICAgICAgIGYnL3dob2lzICJ7dGFyZ2V0fSIgIntndWlsZH0iICJ7c2FuaXRpemVU"
    "ZXh0KHRvd24pfSIgIntzYW5pdGl6ZVRleHQoY2hhdGNoYW5uZWwpfSIgJwogICAgICAgIGYnIntz"
    "YW5pdGl6ZVRleHQoaW5mb1siZW1haWwiXSwgX01BWF9XSE9JU19GSUVMRCl9IiAnCiAgICAgICAg"
    "Zicie3Nhbml0aXplVGV4dChpbmZvWyJsb2NhdGlvbiJdLCBfTUFYX1dIT0lTX0ZJRUxEKX0iICcK"
    "ICAgICAgICBmJ3tpbmZvWyJhZ2UiXX0ge2luZm9bImdlbmRlciJdfSAie3Nhbml0aXplVGV4dChp"
    "bmZvWyJkZXNjcmlwdGlvbiJdLCBfTUFYX0RFU0NSSVBUSU9OKX0iJwogICAgKQpkZWYgX3VwZGF0"
    "ZShtZCx1c3IscmVzKToKICAgICMvdXBkYXRlICJuYW1lIiAiZW1haWwiICJsb2NhdGlvbiIgImFn"
    "ZSIgImdlbmRlciIgImRlc2NyaXB0aW9uIgogICAgaWYgbGVuKHJlcyk8NjoKICAgICAgICByZXR1"
    "cm4gTm9uZQogICAgaWYgcmVzWzFdICE9IHVzci51c2VyLm5hbWU6CiAgICAgICAgcmV0dXJuIE5v"
    "bmUgI2NhbiBvbmx5IHVwZGF0ZSBvd24gd2hvaXMgaW5mbwogICAgZW1haWwgPSBzYW5pdGl6ZVRl"
    "eHQocmVzWzJdLCBfTUFYX1dIT0lTX0ZJRUxEKQogICAgbG9jYXRpb24gPSBzYW5pdGl6ZVRleHQo"
    "cmVzWzNdLCBfTUFYX1dIT0lTX0ZJRUxEKQogICAgYWdlID0gcmVzWzRdCiAgICBnZW5kZXIgPSBy"
    "ZXNbNV0KICAgIGRlc2NyaXB0aW9uID0gc2FuaXRpemVUZXh0KHJlc1s2XSwgX01BWF9ERVNDUklQ"
    "VElPTikgaWYgbGVuKHJlcyk+NiBlbHNlICcnCiAgICBHREgudXBkYXRlV2hvaXModXNyLnVzZXIu"
    "bmFtZSwgZW1haWwsIGxvY2F0aW9uLCBhZ2UsIGdlbmRlciwgZGVzY3JpcHRpb24pCiAgICByZXR1"
    "cm4gTm9uZSAjc2VydmVyIHNlbmRzIG5vIHJlc3BvbnNlLCBwZXIgcHJvdG9jb2wgZG9jCgpfUkVf"
    "Q01EID0gcmUuY29tcGlsZShyJyg/OiIoW14iXSopIil8KFteXHNdKyknKQojY29tbWFuZCAtPiAo"
    "aGFuZGxlciwgbWluaW11bSBhcmd1bWVudCBjb3VudCAqZXhjbHVkaW5nKiB0aGUgY29tbWFuZCB3"
    "b3JkKS4KI1RoZSBjb3VudCBpcyBlbmZvcmNlZCBvbmNlLCBjZW50cmFsbHksIGluIHBhcnNlKCk6"
    "IGV2ZXJ5IGhhbmRsZXIgaW5kZXhlcyBpbnRvCiNyZXNbXSBwb3NpdGlvbmFsbHksIHNvIGEgY2xp"
    "ZW50IHNlbmRpbmcgYSBjb21tYW5kIHdpdGggZmV3ZXIgYXJndW1lbnRzIHRoYW4KI2V4cGVjdGVk"
    "IHVzZWQgdG8gcmFpc2UgSW5kZXhFcnJvciBhbmQgdGVhciBkb3duIGl0cyBvd24gY29ubmVjdGlv"
    "biB0aHJlYWQuCiNEZWNsYXJpbmcgdGhlIGFyaXR5IGhlcmUga2VlcHMgdGhhdCBjaGVjayBpbiBv"
    "bmUgcGxhY2UgaW5zdGVhZCBvZiByZXBlYXRpbmcgYQojbGVuKHJlcykgZ3VhcmQgYXQgdGhlIHRv"
    "cCBvZiBmaWZ0ZWVuIGhhbmRsZXJzLgpfQ09NTUFORFMgPSB7CiAgICAnL25vcCc6ICAgICAgICAg"
    "ICAgICAgICAgICAoX25vcCwgMCksCiAgICAnL2xlYXZlZ2FtZWNoYW5uZWwnOiAgICAgICAoX2xl"
    "YXZlZ2FtZWNoYW5uZWwsIDApLAogICAgJy9yZXF1ZXN0am9pbmdhbWVjaGFubmVsJzogKF9yZXF1"
    "ZXN0am9pbmdhbWVjaGFubmVsLCAxKSwKICAgICNBcml0eSAxLCBub3QgMjogdGhlIHBvc2l0aW9u"
    "IGFyZ3VtZW50IGlzIG9wdGlvbmFsICh0aGUgY2xpZW50IG9taXRzIGl0CiAgICAjd2hlbiBpdCBo"
    "YXMgbm8gbGFzdC1rbm93biBwb3NpdGlvbiB5ZXQsIGUuZy4gdGhlIHZlcnkgZmlyc3QgdG93biBl"
    "bnRyeQogICAgI2FmdGVyIGxvZ2luKS4gUmVxdWlyaW5nIGl0IG1hZGUgcGFyc2UoKSBkcm9wIHRo"
    "ZSBjb21tYW5kIHNpbGVudGx5LCB3aGljaAogICAgI3RoZSBjbGllbnQgZXhwZXJpZW5jZXMgYXMg"
    "YSB0b3duIGl0IGNhbiBuZXZlciBmaW5pc2ggbG9hZGluZy4KICAgICcvam9pbmdhbWVjaGFubmVs"
    "JzogICAgICAgIChfam9pbmdhbWVjaGFubmVsLCAxKSwKICAgICcvdXBkaGVyb3Bvcyc6ICAgICAg"
    "ICAgICAgIChfdXBkaGVyb3BvcywgMSksCiAgICAnL3NlbmQnOiAgICAgICAgICAgICAgICAgICAo"
    "X3NlbmQsIDEpLAogICAgJy9nZXRndWlsZHJhbmtwb2ludHMnOiAgICAgKF9nZXRndWlsZHJhbmtw"
    "b2ludHMsIDApLAogICAgJy9yZXF1ZXN0Y3JlYXRlZ2FtZSc6ICAgICAgKF9yZXF1ZXN0Y3JlYXRl"
    "Z2FtZSwgMSksCiAgICAnL2NyZWF0ZWdhbWUnOiAgICAgICAgICAgICAoX2NyZWF0ZUdhbWUsIDkp"
    "LAogICAgJy9zdG9wZ2FtZSc6ICAgICAgICAgICAgICAgKF9zdG9wZ2FtZSwgMCksCiAgICAnL2xl"
    "YXZlZ2FtZSc6ICAgICAgICAgICAgICAoX3N0b3BnYW1lLCAwKSwjVE9ETyBmaXggZm9yIG11bHRp"
    "cGxlIHVzZXJzPwogICAgJy9zdGFydGluZ2dhbWUnOiAgICAgICAgICAgKF9zdGFydGluZ2dhbWUs"
    "IDApLAogICAgJy9zdGFydGdhbWUnOiAgICAgICAgICAgICAgKF9zdGFydGdhbWUsIDApLAogICAg"
    "Jy9nZXRwbGF5ZXJkYXRhJzogICAgICAgICAgKF9nZXRwbGF5ZXJkYXRhLCAyKSwKICAgICcvc2V0"
    "cGxheWVyZGF0YSc6ICAgICAgICAgIChfc2V0cGxheWVyZGF0YSwgMyksCiAgICAnL3NldHVzZXJo"
    "ZXJvZGF0YSc6ICAgICAgICAoX3NldHVzZXJoZXJvZGF0YSwgMiksCiAgICAnL2dhbWVjb21tYW5k"
    "dG91c2VyJzogICAgICAoX2dhbWVjb21tYW5kdG91c2VyLCAyKSwjVE9ETyBjb25zaWRlciBvcHRp"
    "bWlzaW5nCiAgICAjQXJpdHkgMTogdGhlIHBhc3N3b3JkIGFyZ3VtZW50IGlzIGFic2VudCBmb3Ig"
    "YSByb29tIHRoYXQgaGFzIG5vbmUsIGFuZAogICAgI2Ryb3BwaW5nIHRoZSBjb21tYW5kIGxlZnQg"
    "dGhlIGpvaW5pbmcgcGxheWVyIG9uICJjb25uZWN0aW5nIiBmb3JldmVyLgogICAgJy9qb2luZ2Ft"
    "ZSc6ICAgICAgICAgICAgICAgKF9qb2luZ2FtZSwgMSksCiAgICAnL3dob2lzJzogICAgICAgICAg"
    "ICAgICAgICAoX3dob2lzLCAxKSwKICAgICcvdXBkYXRlJzogICAgICAgICAgICAgICAgIChfdXBk"
    "YXRlLCA1KSwKICAgICNBcml0aWVzIGJlbG93IGFyZSB0aGUgY2xpZW50J3Mgb3duLCBmcm9tIGl0"
    "cyBmb3JtYXQgdGFibGUgLSBzZWUgdGhlIGJsb2NrCiAgICAjb2YgaGFuZGxlcnMgYWJvdmUuIC9t"
    "c2cncyBsYXlvdXQgaXMgbm90IGluIHRoYXQgdGFibGUgKHRoZSBjbGllbnQgYnVpbGRzIGl0CiAg"
    "ICAjYnkgY29uY2F0ZW5hdGlvbiwgbGlrZSAvc2VuZCksIHNvIDIgaXMgdGhlIHNtYWxsZXN0IHNh"
    "bmUgcmVxdWlyZW1lbnQuCiAgICAnL2dhbWVjaGFubmVsc2xpc3QnOiAgICAgICAoX2dhbWVjaGFu"
    "bmVsc2xpc3QsIDApLAogICAgJy9qb2luY2hhdGNoYW5uZWwnOiAgICAgICAgKF9qb2luY2hhdGNo"
    "YW5uZWwsIDEpLAogICAgJy9tc2cnOiAgICAgICAgICAgICAgICAgICAgKF9tc2csIDIpLAogICAg"
    "Jy9zZXRnYW1lcGFyYW1zJzogICAgICAgICAgKF9zZXRnYW1lcGFyYW1zLCAyKSwKICAgICcvbmV3"
    "Z2FtZWhvc3QnOiAgICAgICAgICAgIChfbmV3Z2FtZWhvc3QsIDEpLAogICAgI0d1aWxkcy4gRXZl"
    "cnkgbmFtZSBoZXJlIGhhcyBiZWVuIHNlZW4gb24gdGhlIHdpcmUgZnJvbSB0aGUgcmV0YWlsIGNs"
    "aWVudC4KICAgICNUaGUgYmF0Y2ggb2YgZ3Vlc3NlZCBzcGVsbGluZ3MgdGhhdCB1c2VkIHRvIHNp"
    "dCBhbG9uZ3NpZGUgdGhlbQogICAgIygvY3JlYXRlZ3VpbGQsIC9yZXF1ZXN0Y3JlYXRlZ3VpbGQs"
    "IC9jcmVhdGd1aWxkLCAvZ3VpbGRjcmVhdGUsCiAgICAjL3JlcXVlc3Rqb2luZ3VpbGQsIC9xdWl0"
    "Z3VpbGQsIC9nZXRndWlsZGluZm8pIGlzIGdvbmU6IHRoZSBjYXB0dXJlIHNob3dlZAogICAgI3Ro"
    "ZSBjbGllbnQgc2VuZHMgbm9uZSBvZiB0aGVtLCBhbmQgdGhhdCAvam9pbmd1aWxkIGlzIHdoYXQg"
    "Y3JlYXRlcyBhCiAgICAjZ3VpbGQuIExlYXZpbmcgYSBndWlsZCBoYXMgbm90IGJlZW4gb2JzZXJ2"
    "ZWQgeWV0LCBzbyBubyBoYW5kbGVyIGlzCiAgICAjcmVnaXN0ZXJlZCBmb3IgaXQgLSB0aGUgcmVh"
    "bCBuYW1lIHdpbGwgc2hvdyB1cCBpbiB0aGUgbG9nIGFzIGFuIHVua25vd24KICAgICNjb21tYW5k"
    "IHRoZSBmaXJzdCB0aW1lIHNvbWVib2R5IHRyaWVzLgogICAgJy9ndWlsZHNsYWRkZXInOiAgICAg"
    "ICAgICAgKF9ndWlsZHNsYWRkZXIsIDEpLAogICAgJy90ZXN0Y3JlYXRlZ3VpbGQnOiAgICAgICAg"
    "KF90ZXN0Y3JlYXRlZ3VpbGQsIDEpLAogICAgJy9qb2luZ3VpbGQnOiAgICAgICAgICAgICAgKF9q"
    "b2luZ3VpbGQsIDEpLAogICAgJy9sYWRkZXInOiAgICAgICAgICAgICAgICAgKF9sYWRkZXIsIDAp"
    "LAp9CmNsYXNzIENvbW1hbmRQYXJzZXIoKToKICAgIGRlZiBfX2luaXRfXyhzZWxmLCBtc2dlcik6"
    "CiAgICAgICAgc2VsZi5jb21tYW5kbGlzdCA9IF9DT01NQU5EUwogICAgICAgIHNlbGYubWQgPSBt"
    "c2dlcgoKICAgIGRlZiBwYXJzZShzZWxmLCBkYXRhLCBvcmlnaW4pOgogICAgICAgICNwcmludChm"
    "J1Rlc3QgUGFyc2luZyB7bGVuKGRhdGEpfToge2J5dGVzKGRhdGEsICdhc2NpaScpfScpCiAgICAg"
    "ICAgcmVzID0gbGlzdCggKGl0bVswXStpdG1bMV0gZm9yIGl0bSBpbiBfUkVfQ01ELmZpbmRhbGwo"
    "ZGF0YSkpICkKICAgICAgICAjcHJpbnQoJ1JlczonLCByZXMpCiAgICAgICAgaWYgbm90IHJlczoK"
    "ICAgICAgICAgICAgI1dhcyBhIHNpbGVudCBkcm9wLiBJZiBhIGZlYXR1cmUgZG9lcyBub3RoaW5n"
    "IGFuZCB0aGUgbG9nIHNob3dzIG5vCiAgICAgICAgICAgICNjb21tYW5kIGZvciBpdCBhdCBhbGws"
    "IHRoaXMgaXMgb25lIG9mIHRoZSB0d28gcGxhY2VzIGl0IGNvdWxkCiAgICAgICAgICAgICNoYXZl"
    "IGRpc2FwcGVhcmVkIGludG8gLSBzbyBzYXkgc28gcmF0aGVyIHRoYW4gbGVhdmUgYSBibGluZCBz"
    "cG90LgogICAgICAgICAgICBpZiBfREVCVUdfTE9HX0NPTU1BTkRTIGFuZCBkYXRhOgogICAgICAg"
    "ICAgICAgICAgd2hvID0gb3JpZ2luLnVzZXIubmFtZSBpZiBvcmlnaW4udXNlciBlbHNlICc/Jwog"
    "ICAgICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSAtPiAoVU5QQVJTRUFCTEUpIHtkYXRh"
    "IXJ9JykKICAgICAgICAgICAgcmV0dXJuIE5vbmUKICAgICAgICB3aG8gPSBvcmlnaW4udXNlci5u"
    "YW1lIGlmIG9yaWdpbi51c2VyIGVsc2UgJz8nCiAgICAgICAgbG91ZCA9IF9ERUJVR19MT0dfQ09N"
    "TUFORFMgYW5kIChfREVCVUdfTE9HX1ZFUkJPU0Ugb3IgcmVzWzBdIG5vdCBpbiBfUVVJRVRfQ09N"
    "TUFORFMpCiAgICAgICAgaWYgbG91ZDoKICAgICAgICAgICAgcHJpbnQoZidbY21kXSB7d2hvfSAt"
    "PiB7ZGF0YX0nKQogICAgICAgIGVudHJ5ID0gc2VsZi5jb21tYW5kbGlzdC5nZXQocmVzWzBdKQog"
    "ICAgICAgIGlmIGVudHJ5IGlzIE5vbmU6CiAgICAgICAgICAgICNMb2cgdGhlIHJhdyBsaW5lLCBu"
    "b3QganVzdCB0aGUgdG9rZW5pc2VkIGxpc3QuIEFuIHVuaW1wbGVtZW50ZWQKICAgICAgICAgICAg"
    "I2NvbW1hbmQgaXMgZXhhY3RseSB0aGUgc2l0dWF0aW9uIHdoZXJlIHRoZSBhcmd1bWVudCBsYXlv"
    "dXQgaXMKICAgICAgICAgICAgI3doYXQgd2UgbmVlZCB0byBzZWUsIGFuZCByZS1xdW90aW5nIHRo"
    "ZSBzcGxpdCB0b2tlbnMgbG9zZXMgaXQuCiAgICAgICAgICAgIHByaW50KGYnKioqIFVOS05PV04g"
    "Q09NTUFORCBmcm9tIHt3aG99OiB7ZGF0YSFyfScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAg"
    "ICAgICAgaGFuZGxlciwgbWluYXJncyA9IGVudHJ5CiAgICAgICAgaWYgbGVuKHJlcykgLSAxIDwg"
    "bWluYXJnczoKICAgICAgICAgICAgcHJpbnQoZicqKiogTUFMRk9STUVEIENPTU1BTkQgZnJvbSB7"
    "d2hvfTogJwogICAgICAgICAgICAgICAgICBmJ3tyZXNbMF19IG5lZWRzIHttaW5hcmdzfSBhcmd1"
    "bWVudChzKSwgZ290IHtsZW4ocmVzKS0xfScpCiAgICAgICAgICAgIHJldHVybiBOb25lCiAgICAg"
    "ICAgI3ByaW50KGYnUGFyc2VkIENvbW1hbmQgRnJvbSB7b3JpZ2luLnVzZXIubmFtZX06JywgcmVz"
    "KQogICAgICAgIG91dCA9IGhhbmRsZXIoc2VsZi5tZCwgb3JpZ2luLCByZXMpCiAgICAgICAgaWYg"
    "bG91ZDoKICAgICAgICAgICAgIyIobm8gZGlyZWN0IHJlcGx5KSIgaXMgdGhlIHNpZ25hdHVyZSBv"
    "ZiBldmVyeSBoYW5nIHJlcG9ydGVkIHNvCiAgICAgICAgICAgICNmYXI6IHRoZSBjbGllbnQgd2Fp"
    "dHMgb24gYW4gYW5zd2VyIHRoYXQgdGhpcyBzZXJ2ZXIgbmV2ZXIgc2VuZHMuCiAgICAgICAgICAg"
    "ICNTb21lIGNvbW1hbmRzIGxlZ2l0aW1hdGVseSBhbnN3ZXIgd2l0aCBub3RoaW5nLCBzbyB0aGlz"
    "IGlzIGEgbGVhZCwKICAgICAgICAgICAgI25vdCBhIHZlcmRpY3QgLSBidXQgaXQgaXMgdGhlIGZp"
    "cnN0IHRoaW5nIHRvIGxvb2sgYXQuCiAgICAgICAgICAgIGlmIG91dDoKICAgICAgICAgICAgICAg"
    "IGhlYWQgPSBvdXQuc3BsaXQoX04pWzBdLmRlY29kZShfV0lSRV9FTkMsICdyZXBsYWNlJykKICAg"
    "ICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gPC0ge2hlYWR9JykKICAgICAgICAgICAg"
    "ZWxzZToKICAgICAgICAgICAgICAgIHByaW50KGYnW2NtZF0ge3dob30gPC0gKG5vIGRpcmVjdCBy"
    "ZXBseSknKQogICAgICAgIHJldHVybiBvdXQKCiN0aHJlYWQgdG8gc2VuZCBtZXNzYWdlcyBhY3Jv"
    "c3MgYWxsIGNvbm5lY3RlZCBjbGllbnRzCiNfX0VYQU1QTEVfTUVTU0FHRV9fID0gewojICAgICd0"
    "YXJnZXQnOlsndXNlcmxpc3QnXSwKIyAgICAnbWVzc2FnZSc6Yicvd2hhdGV2ZXJcMCcrYidibG9i"
    "JwojfQpjbGFzcyBNZXNzYWdlRGlzdHJpYnV0b3IoKToKICAgIF9FTkRJVEVNID0gWydTVE9QJ10K"
    "ICAgIGRlZiBfX2luaXRfXyhzZWxmLCBzZXJ2ZXIpOgogICAgICAgIHNlbGYuX2NRdWV1ZSA9IFNp"
    "bXBsZVF1ZXVlKCkKICAgICAgICBzZWxmLnNlcnZlciA9IHNlcnZlcgogICAgZGVmIHNlcnZlX2Zv"
    "cmV2ZXIoc2VsZik6CiAgICAgICAgd2hpbGUgVHJ1ZTogI1RPRE8gcG9zc2libGUgY2hlY2sgc2Vs"
    "Zi5zZXJ2ZXIuX2lzX2Nsb3NpbmcKICAgICAgICAgICAgdHJ5OgogICAgICAgICAgICAgICAgY29t"
    "bWFuZCA9IHNlbGYuX2NRdWV1ZS5nZXQoKQogICAgICAgICAgICAgICAgI3ByaW50KCdNRDonLCBj"
    "b21tYW5kLCBzZWxmLnNlcnZlci5faXNfY2xvc2luZykKICAgICAgICAgICAgICAgIGlmIGNvbW1h"
    "bmQgPT0gc2VsZi5fRU5ESVRFTToKICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAg"
    "ICAgICAgdWwgPSBjb21tYW5kLmdldCgndGFyZ2V0JyxbXSkKICAgICAgICAgICAgICAgIG1zZyA9"
    "IGNvbW1hbmQuZ2V0KCdtZXNzYWdlJykKICAgICAgICAgICAgICAgIGlmIG1zZzoKICAgICAgICAg"
    "ICAgICAgICAgICBmb3IgdXNyIGluIHVsOgogICAgICAgICAgICAgICAgICAgICAgICB1c3Iuc2Vu"
    "ZChtc2cpCiAgICAgICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgICAgICBwcmlu"
    "dCgnW0xvYmJ5XSBEaXN0cmlidXRvciBlcnJvcjpcbicgKyB0cmFjZWJhY2suZm9ybWF0X2V4Yygp"
    "KQogICAgZGVmIGFkZChzZWxmLCBwcm9wcyk6CiAgICAgICAgI1NuYXBzaG90IHRoZSB0YXJnZXQg"
    "bGlzdCBIRVJFLCBpbiB0aGUgY2FsbGluZyB0aHJlYWQuIENhbGxlcnMgaGFuZCB1cwogICAgICAg"
    "ICNsaXZlIGNvbnRhaW5lcnMgKEdhbWVDaGFubmVsLnVzZXJsaXN0LCBzdGF0ZS5hY3RpdmVVc2Vy"
    "cy52YWx1ZXMoKSwgLi4uKQogICAgICAgICN0aGF0IG90aGVyIGhhbmRsZXIgdGhyZWFkcyBhcHBl"
    "bmQgdG8vcmVtb3ZlIGZyb20gY29udGludW91c2x5OyB0aGUKICAgICAgICAjZGlzdHJpYnV0b3Ig"
    "dGhyZWFkIGl0ZXJhdGVkIHRoZW0gbGF0ZXIgYW5kIGhpdCAnbGlzdCBjaGFuZ2VkIHNpemUKICAg"
    "ICAgICAjZHVyaW5nIGl0ZXJhdGlvbicsIHdoaWNoIHRoZSBleGNlcHQgYWJvdmUgc3dhbGxvd2Vk"
    "IC0gc2lsZW50bHkKICAgICAgICAjZHJvcHBpbmcgdGhlIGVudGlyZSBicm9hZGNhc3QuIHVwZGF0"
    "ZVBvcygpIGRvZXMgdGhpcyBvbmNlIGEgc2Vjb25kIGZvcgogICAgICAgICNldmVyeSBjaGFubmVs"
    "LCBzbyB0aGlzIHdhcyB0aGUgaG90IHBhdGggZm9yIHRoZSByYWNlLgogICAgICAgIGlmIGlzaW5z"
    "dGFuY2UocHJvcHMsIGRpY3QpOgogICAgICAgICAgICBwcm9wcyA9IGRpY3QocHJvcHMpCiAgICAg"
    "ICAgICAgIHByb3BzWyd0YXJnZXQnXSA9IGxpc3QocHJvcHMuZ2V0KCd0YXJnZXQnKSBvciAoKSkK"
    "ICAgICAgICBzZWxmLl9jUXVldWUucHV0KHByb3BzKQogICAgZGVmIGVuZChzZWxmKToKICAgICAg"
    "ICBzZWxmLmFkZChzZWxmLl9FTkRJVEVNKQogICAgCmRlZiBfY2xhbXBJbnQocmF3LCBkZWZhdWx0"
    "LCBsbywgaGkpOgogICAgI0V2ZXJ5IG51bWVyaWMgZmllbGQgb2YgL2NyZWF0ZWdhbWUgYXJyaXZl"
    "cyBhcyB0ZXh0IHN0cmFpZ2h0IG9mZiB0aGUgd2lyZS4KICAgICNpbnQoKSBvbiBpdCB1c2VkIHRv"
    "IHJhaXNlIFZhbHVlRXJyb3IgZm9yIGFueXRoaW5nIG5vbi1udW1lcmljLCBhbmQgdGhhdAogICAg"
    "I2V4Y2VwdGlvbiBsZWZ0IHRoZSBoYW5kbGVyLCB0b3JlIGRvd24gdGhlIGhvc3QncyBjb25uZWN0"
    "aW9uIHRocmVhZCBhbmQKICAgICNsb2dnZWQgYSB0cmFjZWJhY2sgLSBvbmUgbWFsZm9ybWVkIHJv"
    "b20gcmVxdWVzdCBkaXNjb25uZWN0ZWQgdGhlIHBsYXllcgogICAgI21ha2luZyBpdC4gVGhlIHJh"
    "bmdlIGNoZWNrIGlzIHRoZSBzYW1lIHJlYXNvbmluZyBhcHBsaWVkIHRvIHZhbHVlcyB0aGF0IGRv"
    "CiAgICAjcGFyc2U6IG1heHBsYXllcnMgY2FtZSBmcm9tIHRoZSBjbGllbnQgdG9vLCBzbyBhIHJv"
    "b20gY291bGQgYWR2ZXJ0aXNlCiAgICAjaXRzZWxmIGFzIGhvbGRpbmcgdHdvIGJpbGxpb24gcGVv"
    "cGxlLgogICAgdHJ5OgogICAgICAgIHZhbCA9IGludChyYXcpCiAgICBleGNlcHQgKFR5cGVFcnJv"
    "ciwgVmFsdWVFcnJvcik6CiAgICAgICAgcmV0dXJuIGRlZmF1bHQKICAgIHJldHVybiBtaW4obWF4"
    "KHZhbCwgbG8pLCBoaSkKY2xhc3MgR2FtZUVudHJ5KCk6CiAgICBkZWYgX19pbml0X18oc2VsZiwg"
    "cGFyZW50LCBuYW1lLCBob3N0LCBwYXN3LCBtYXBwLCBtYXB0LCBucGosIHVuMSwgc3RhdHVzLCBt"
    "YXhwbGF5ZXJzLCB1cmwpOgogICAgICAgIGlmIGhvc3QudXNlci5nYW1lOgogICAgICAgICAgICBo"
    "b3N0LnVzZXIuZ2FtZS5yZW1vdmUoaG9zdCkKICAgICAgICBzZWxmLnBhcmVudCA9IHBhcmVudCAj"
    "IEdhbWVjaGFubmVsCiAgICAgICAgc2VsZi5nbmFtZSA9IG5hbWUgIwogICAgICAgIHNlbGYuaG9z"
    "dCA9IGhvc3QgIyBDb25uZWN0aW9uIE9iamVjdAogICAgICAgIHNlbGYucGFzc3dvcmQgPSBwYXN3"
    "ICMgJycgb3IgJ3Bhc3N3b3JkJwogICAgICAgIHNlbGYubWFwUGFyID0gbWFwcCAjICJOZXRfTV8w"
    "MSBudWxsIDAgMSIKICAgICAgICBzZWxmLm1hcFRyYW5zbGF0ZSA9IG1hcHQgIyAidHJhbnNsYXRl"
    "TmV0X01fMDEiCiAgICAgICAgc2VsZi5ucGogPSBfY2xhbXBJbnQobnBqLCAwLCAwLCAxKSAjICJl"
    "bmFibGUgbmV3IHBsYXllciB0byBqb2luIChib29sKSIKICAgICAgICBzZWxmLnVuMSA9IF9jbGFt"
    "cEludCh1bjEsIDAsIDAsIF8zMmJpdCkgIyAwIFRPRE8gZmlndXJlIG91dCBpZiBtZWFucyAiZ3Vp"
    "bGQgZ2FtZSIKICAgICAgICBzZWxmLnN0YXR1cyA9IF9jbGFtcEludChzdGF0dXMsIDAsIDAsIDEp"
    "ICMgY2hhbmdlcyB0byAxIHdoZW4gc3RhcnRlZCwgb25seSByZWxldmFudCB3aGVuIG5waiB0cnVl"
    "CiAgICAgICAgc2VsZi5tYXhwbGF5ZXJzID0gX2NsYW1wSW50KG1heHBsYXllcnMsIDgsIDEsIEdh"
    "bWVDaGFubmVsLm1heHVzZXIpICMgOCAjbWF4IHVzZXJzPwogICAgICAgICN4LWRpcmVjdHBsYXkg"
    "dXJsLCB3aXRoIHRoZSBob3N0J3MgYWR2ZXJ0aXNlZCBhZGRyZXNzIHJlcGxhY2VkIGJ5IHRoZQog"
    "ICAgICAgICNhZGRyZXNzIHRoaXMgc2VydmVyIHNlZXMgaXQgY29ubmVjdCBmcm9tIC0gc2VlIHJl"
    "d3JpdGVHYW1lSG9zdCgpLgogICAgICAgIHBlZXIgPSBob3N0LmNsaWVudF9hZGRyZXNzWzBdIGlm"
    "IGhvc3QuY2xpZW50X2FkZHJlc3MgZWxzZSAnJwogICAgICAgICNLZXB0IGFzIHRoZXkgYXJyaXZl"
    "ZDogdGhlIGFkZHJlc3MgYSBqb2luZXIgaXMgZ2l2ZW4gaXMgcGlja2VkIHdoZW4KICAgICAgICAj"
    "dGhhdCBqb2luZXIgYXNrcywgZnJvbSBib3RoIGVuZHMgYXQgb25jZSAtIHNlZSBfdXJsRm9yKCku"
    "IHNlbGYudXJsIGlzCiAgICAgICAgI3RoZSByb29tJ3Mgb3duIGJlc3QgYW5zd2VyIHdpdGggbm9i"
    "b2R5IHRvIGFpbSBpdCBhdCwgdXNlZCBhcyB0aGUKICAgICAgICAjZmFsbGJhY2sgYW5kIGFzIHRo"
    "ZSB0aGluZyB0aGUgbG9nIGNhbiBzaG93IGF0IGNyZWF0aW9uIHRpbWUuCiAgICAgICAgc2VsZi5o"
    "b3N0UGVlciA9IHBlZXIKICAgICAgICBzZWxmLnJhd1VybCA9IHVybAogICAgICAgIChzZWxmLnVy"
    "bCwgbm90ZSkgPSByZXdyaXRlR2FtZUhvc3QodXJsLCBwZWVyKQogICAgICAgIHByaW50KGYnW0xv"
    "YmJ5XSBSb29tICJ7bmFtZX0iIGJ5IHtob3N0LnVzZXIubmFtZX06IHtub3RlfScpCiAgICAgICAg"
    "cHJpbnQoZidbTG9iYnldICAgdXJsIGFkdmVydGlzZWQgdG8gam9pbmVyczoge3NlbGYudXJsfScp"
    "CiAgICAgICAgc2VsZi51c2VybGlzdCA9IFtob3N0LF0KICAgICAgICBzZWxmLnBhcmVudC5nYW1l"
    "c1tzZWxmLmduYW1lXSA9IHNlbGYKICAgICAgICBzZWxmLmhvc3QudXNlci5nYW1lID0gc2VsZgog"
    "ICAgICAgICNBZHZlcnRpc2Ugb24gY3JlYXRpb24KICAgICAgICBtc2cgPSBzZWxmLmdldEdhbWVT"
    "dHJpbmcoKQogICAgICAgIHRnID0gc2VsZi5wYXJlbnQudXNlcmxpc3QKICAgICAgICBzZWxmLnBh"
    "cmVudC5zZXJ2ZXIuZGlzdC5hZGQoeyd0YXJnZXQnOnRnLCdtZXNzYWdlJzptc2d9KQogICAgZGVm"
    "IF9hdWRpZW5jZShzZWxmKToKICAgICAgICAjV2hvIG5lZWRzIHRvIGhlYXIgYWJvdXQgdGhpcyBy"
    "b29tIGNoYW5naW5nOiBldmVyeW9uZSBicm93c2luZyB0aGUKICAgICAgICAjdG93biwgcGx1cyBl"
    "dmVyeW9uZSBhbHJlYWR5IGluc2lkZSB0aGUgcm9vbS4gT25jZSBhIGdhbWUgc3RhcnRzIGl0cwog"
    "ICAgICAgICNwbGF5ZXJzIGFyZSB0YWtlbiBvZmYgdGhlIHRvd24gcm9zdGVyIChzZWUgc3RhcnRH"
    "YW1lKSwgc28gdGhlIHRvd24KICAgICAgICAjbGlzdCBhbG9uZSBubyBsb25nZXIgcmVhY2hlcyB0"
    "aGVtIC0gYW5kIHRoZSBob3N0LCB3aG8gaXMgYWx3YXlzCiAgICAgICAgI2luLWdhbWUsIGlzIGV4"
    "YWN0bHkgd2hvIG5lZWRzIHRvIGtub3cgdGhhdCBzb21lYm9keSBqb2luZWQuCiAgICAgICAgc2Vl"
    "biA9IGxpc3Qoc2VsZi5wYXJlbnQudXNlcmxpc3QpCiAgICAgICAgZm9yIGMgaW4gc2VsZi51c2Vy"
    "bGlzdDoKICAgICAgICAgICAgaWYgYyBub3QgaW4gc2VlbjoKICAgICAgICAgICAgICAgIHNlZW4u"
    "YXBwZW5kKGMpCiAgICAgICAgcmV0dXJuIHNlZW4KICAgIGRlZiBfdXJsRm9yKHNlbGYsIHVzcik6"
    "CiAgICAgICAgI1RoZSBhZGRyZXNzIG9mIHRoZSBob3N0IHRoYXQgVEhJUyBqb2luZXIgc2hvdWxk"
    "IGJlIHNlbnQgdG8uIEJvdGggZW5kcwogICAgICAgICNhcmUga25vd24gaGVyZSBhbmQgb25seSBo"
    "ZXJlOiB3aGVyZSB0aGUgaG9zdCBjb25uZWN0ZWQgZnJvbSwgYW5kIGJvdGgKICAgICAgICAjd2hl"
    "cmUgdGhlIGpvaW5lciBjb25uZWN0ZWQgZnJvbSBhbmQgd2hpY2ggb2Ygb3VyIG93biBhZGRyZXNz"
    "ZXMgdGhleQogICAgICAgICNyZWFjaGVkIHVzIGF0IC0gc2VlIHBpY2tKb2luQWRkcmVzcy4KICAg"
    "ICAgICBwZWVyID0gdXNyLmNsaWVudF9hZGRyZXNzWzBdIGlmIHVzci5jbGllbnRfYWRkcmVzcyBl"
    "bHNlICcnCiAgICAgICAgbG9jYWwgPSAnJwogICAgICAgIHRyeToKICAgICAgICAgICAgbG9jYWwg"
    "PSB1c3IucmVxdWVzdC5nZXRzb2NrbmFtZSgpWzBdCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAg"
    "ICAgICAgICAgIHBhc3MgI3NvY2tldCBhbHJlYWR5IGdvbmU7IHRoZSByb29tJ3Mgb3duIHVybCBp"
    "cyB0aGUgZmFsbGJhY2sKICAgICAgICAodXJsLCBub3RlKSA9IHJld3JpdGVHYW1lSG9zdEZvckpv"
    "aW5lcihzZWxmLnJhd1VybCwgc2VsZi5ob3N0UGVlciwgcGVlciwgbG9jYWwpCiAgICAgICAgaWYg"
    "bm90IHVybDoKICAgICAgICAgICAgcmV0dXJuIHNlbGYudXJsCiAgICAgICAgcHJpbnQoZidbTG9i"
    "YnldIHt1c3IudXNlci5uYW1lfSBqb2lucyAie3NlbGYuZ25hbWV9Ijoge25vdGV9JykKICAgICAg"
    "ICByZXR1cm4gdXJsCiAgICBkZWYgYWRkVXNlcihzZWxmLCB1c3IsIHBhc3cpOgogICAgICAgICNF"
    "dmVyeSByZWplY3Rpb24gYmVsb3cgaGFzIHRvIGFuc3dlciB0aGUgY2xpZW50IHdpdGggKnNvbWV0"
    "aGluZyouIFRoZQogICAgICAgICNjbGllbnQgc2hvd3MgImNvbm5lY3RpbmcuLi4iIGZyb20gdGhl"
    "IG1vbWVudCBpdCBzZW5kcyAvam9pbmdhbWUgdW50aWwKICAgICAgICAjdGhlIHNlcnZlciBhbnN3"
    "ZXJzLCBhbmQgaXQgaGFzIG5vIHRpbWVvdXQgb2YgaXRzIG93bjogcmV0dXJuaW5nIE5vbmUKICAg"
    "ICAgICAjbGVmdCB0aGUgcGxheWVyIHN0YXJpbmcgYXQgdGhhdCBkaWFsb2cgdW50aWwgdGhleSBr"
    "aWxsZWQgdGhlIGdhbWUuCiAgICAgICAgaWYgdXNyIGluIHNlbGYudXNlcmxpc3Q6CiAgICAgICAg"
    "ICAgICNBbHJlYWR5IGluIChkdXBsaWNhdGUgL2pvaW5nYW1lLCBlLmcuIHRoZSBwbGF5ZXIgZG91"
    "YmxlLWNsaWNrZWQKICAgICAgICAgICAgI3RoZSByb29tKS4gUmUtYW5zd2VyIGluc3RlYWQgb2Yg"
    "YXBwZW5kaW5nIHRoZW0gYSBzZWNvbmQgdGltZS4KICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9q"
    "b2luZ2FtZSAie3NlbGYuZ25hbWV9IiAie3NlbGYuX3VybEZvcih1c3IpfSIgIntzZWxmLnN0YXR1"
    "c30iJykKICAgICAgICBpZiBsZW4oc2VsZi51c2VybGlzdCk+PXNlbGYubWF4cGxheWVyczoKICAg"
    "ICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJvciBnYW1lRnVsbCAie3NlbGYuZ25hbWV9IicpCiAg"
    "ICAgICAgaWYgc2VsZi5zdGF0dXMgYW5kIG5vdCBzZWxmLm5wajoKICAgICAgICAgICAgcmV0dXJu"
    "IF9lbShmJy9lcnJvciBnYW1lQWxyZWFkeVN0YXJ0ZWQgIntzZWxmLmduYW1lfSInKQogICAgICAg"
    "IGlmIHNlbGYucGFzc3dvcmQgIT0gcGFzdzoKICAgICAgICAgICAgcmV0dXJuIF9lbShmJy9lcnJv"
    "ciBiYWRHYW1lUGFzc3dvcmQgIntzZWxmLmduYW1lfSInKQogICAgICAgIGlmIHVzci51c2VyLmdh"
    "bWUgaXMgbm90IE5vbmU6CiAgICAgICAgICAgIHVzci51c2VyLmdhbWUucmVtb3ZlKHVzcikgI2xl"
    "YXZlIHRoZSBwcmV2aW91cyByb29tIGNsZWFubHkgZmlyc3QKICAgICAgICBzZWxmLnVzZXJsaXN0"
    "LmFwcGVuZCh1c3IpCiAgICAgICAgdXNyLnVzZXIuZ2FtZSA9IHNlbGYKICAgICAgICByZXQgPSBf"
    "ZW0oZickZ2FtZXVzZXIgIntzZWxmLmduYW1lfSIgInt1c3IudXNlci5uYW1lfSIgIiIgIjEwMCIg"
    "IjAiJykKICAgICAgICAjVW5jb25kaXRpb25hbGx5LCB0byBldmVyeW9uZSBpbiB0aGUgdG93bi4g"
    "VGhpcyB1c2VkIHRvIGJlIHNlbnQgb25seQogICAgICAgICN3aGVuIG5waiAoIm5ldyBwbGF5ZXJz"
    "IG1heSBqb2luIGEgcnVubmluZyBnYW1lIikgd2FzIHNldCAtIGJ1dCBucGoKICAgICAgICAjc2F5"
    "cyBub3RoaW5nIGFib3V0IHdobyBzaG91bGQgaGVhciBhYm91dCBhIGpvaW4sIGl0IG9ubHkgY29u"
    "dHJvbHMKICAgICAgICAjd2hldGhlciBhICpzdGFydGVkKiBnYW1lIHN0YXlzIGxpc3RlZC4gRm9y"
    "IGFuIG9yZGluYXJ5IHJvb20sIHdoaWNoIGlzCiAgICAgICAgI2NyZWF0ZWQgd2l0aCBucGo9MCBh"
    "bmQgam9pbmVkIGJlZm9yZSBpdCBzdGFydHMsIG5vYm9keSB3YXMgZXZlciB0b2xkOgogICAgICAg"
    "ICN0aGUgaG9zdCdzIGxvYmJ5IG5ldmVyIGxpc3RlZCB0aGUgYXJyaXZpbmcgcGxheWVyLCBzbyB0"
    "aGUgaG9zdCBoYWQKICAgICAgICAjbm9ib2R5IHRvIHN0YXJ0IHRoZSBnYW1lIHdpdGgsIGFuZCB0"
    "aGUgam9pbmVyIHNhdCBpbiAiY29ubmVjdGluZyIKICAgICAgICAjZm9yZXZlciB3YWl0aW5nIGZv"
    "ciBhIHN0YXJ0IHRoYXQgY291bGQgbm90IGNvbWUuCiAgICAgICAgdXNyLnNlcnZlci5kaXN0LmFk"
    "ZCh7J3RhcmdldCc6c2VsZi5fYXVkaWVuY2UoKSwnbWVzc2FnZSc6cmV0fSkKICAgICAgICByZXR1"
    "cm4gX2VtKGYnL2pvaW5nYW1lICJ7c2VsZi5nbmFtZX0iICJ7c2VsZi5fdXJsRm9yKHVzcil9IiAi"
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
    "aXN0LnJlbW92ZShjb24pCiAgICAgICAgaWYgY29uLnVzZXIgaXMgTm9uZToKICAgICAgICAgICAg"
    "I0Nvbm5lY3Rpb24gYWxyZWFkeSB0b3JuIGRvd24gKGl0cyBoYW5kbGVyIHJhbiBmaW5pc2goKSB3"
    "aGlsZSB0aGlzCiAgICAgICAgICAgICNyZW1vdmFsIHdhcyBvbiBpdHMgd2F5IHRocm91Z2ggYW5v"
    "dGhlciB0aHJlYWQpLiBOb3RoaW5nIGxlZnQgdG8KICAgICAgICAgICAgI2Fubm91bmNlIGFib3V0"
    "IGl0LCBidXQgdGhlIHJvb20gaXRzZWxmIHN0aWxsIGhhcyB0byBiZSB0aWRpZWQgdXAKICAgICAg"
    "ICAgICAgI2JlbG93LCBzbyBkb24ndCByZXR1cm4gZWFybHkuCiAgICAgICAgICAgIGxlYXZlbXNn"
    "ID0gYicnCiAgICAgICAgZWxzZToKICAgICAgICAgICAgbGVhdmVtc2cgPSBfZW0oZicmZ2FtZXVz"
    "ZXIgIntjb24udXNlci5uYW1lfSInKQogICAgICAgICAgICBjb24udXNlci5nYW1lID0gTm9uZQog"
    "ICAgICAgIGlmIGNvbiBpcyBzZWxmLmhvc3Q6CiAgICAgICAgICAgICNUaGUgaG9zdCAqaXMqIHRo"
    "ZSBnYW1lIHNlc3Npb246IHRoZSBjby1vcCB3b3JsZCBydW5zIG9uIHRoZWlyCiAgICAgICAgICAg"
    "ICNtYWNoaW5lIGFuZCB0aGUgcm9vbSdzIERpcmVjdFBsYXkgdXJsIHBvaW50cyBhdCBpdC4gT25j"
    "ZSB0aGV5IGFyZQogICAgICAgICAgICAjZ29uZSB0aGUgcm9vbSBjYW5ub3QgYmUgam9pbmVkIGJ5"
    "IGFueWJvZHksIGJ1dCBpdCB1c2VkIHRvIHN0YXkKICAgICAgICAgICAgI2xpc3RlZCAtIHNvIHRo"
    "ZSBuZXh0IHBsYXllciB0byBjbGljayBpdCBnb3QgYSB1cmwgdG8gYSBnYW1lIHRoYXQKICAgICAg"
    "ICAgICAgI25vIGxvbmdlciBleGlzdGVkIGFuZCBzYXQgb24gImNvbm5lY3RpbmciIHVudGlsIHRo"
    "ZXkgZ2F2ZSB1cC4KICAgICAgICAgICAgI1RoaXMgaXMgd2hhdCBhIGhvc3QgY3Jhc2ggbGVhdmVz"
    "IGJlaGluZC4KICAgICAgICAgICAgd2hvID0gY29uLnVzZXIubmFtZSBpZiBjb24udXNlciBlbHNl"
    "ICc/JwogICAgICAgICAgICBwcmludChmJ1tMb2JieV0gSG9zdCB7d2hvfSBsZWZ0IHJvb20gIntz"
    "ZWxmLmduYW1lfSIsIGNsb3NpbmcgaXQnKQogICAgICAgICAgICBpZiBsZWF2ZW1zZzoKICAgICAg"
    "ICAgICAgICAgIHNlbGYucGFyZW50LnNlcnZlci5kaXN0LmFkZCh7J3RhcmdldCc6dGcsJ21lc3Nh"
    "Z2UnOmxlYXZlbXNnfSkKICAgICAgICAgICAgc2VsZi5kZXN0cm95KCkKICAgICAgICAgICAgcmV0"
    "dXJuCiAgICAgICAgI2lmIDAgdXNlcnMgbGVmdCwgcmVtb3ZlIGdhbWUKICAgICAgICBpZiBsZW4o"
    "c2VsZi51c2VybGlzdCk9PTA6CiAgICAgICAgICAgIGxlYXZlbXNnID0gX2VtKGYnJmdhbWUgIntz"
    "ZWxmLmduYW1lfSInKQogICAgICAgICAgICAjT25seSBpZiB0aGlzIGVudHJ5IGlzIHN0aWxsIHRo"
    "ZSBvbmUgcmVnaXN0ZXJlZCB1bmRlciB0aGF0IG5hbWUuIEEKICAgICAgICAgICAgI3Jvb20gd2hv"
    "c2UgaG9zdCByZWNvbm5lY3RzIGFuZCByZS1ob3N0cyBpcyByZXBsYWNlZCBieSBhICpuZXcqCiAg"
    "ICAgICAgICAgICNHYW1lRW50cnkgd2l0aCB0aGUgc2FtZSBuYW1lIChzZWUgX2lzU3RhbGVHYW1l"
    "KTsgdGhlIG9sZCBvbmUncwogICAgICAgICAgICAjbGFzdCBwbGF5ZXIgbGVhdmluZyB0aGVuIGRl"
    "bGV0ZWQgdGhlIGxpdmUgcm9vbSBvdXQgb2YgdGhlIGNoYW5uZWwgLQogICAgICAgICAgICAjb3Ig"
    "cmFpc2VkIEtleUVycm9yIGlmIGl0IGhhZCBhbHJlYWR5IGdvbmUsIGluc2lkZSB0aGUgZGlzY29u"
    "bmVjdAogICAgICAgICAgICAjcGF0aCwgd2hpY2ggYWJvcnRzIHRoZSByZXN0IG9mIHRoYXQgcGxh"
    "eWVyJ3MgY2xlYW51cC4KICAgICAgICAgICAgaWYgc2VsZi5wYXJlbnQuZ2FtZXMuZ2V0KHNlbGYu"
    "Z25hbWUpIGlzIHNlbGY6CiAgICAgICAgICAgICAgICBkZWwgc2VsZi5wYXJlbnQuZ2FtZXNbc2Vs"
    "Zi5nbmFtZV0KICAgICAgICBpZiBsZWF2ZW1zZzoKICAgICAgICAgICAgc2VsZi5wYXJlbnQuc2Vy"
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
    "CiAgICAgICAgI0Z1bGwgZm91ci1maWVsZCBmb3JtIChuYW1lLCBndWlsZCwgZmxhZ3MsIGd1aWQp"
    "LCB3aGljaCBpcyB3aGF0IHRoZQogICAgICAgICNjbGllbnQgaXMgZG9jdW1lbnRlZCB0byBzZW5k"
    "IGFuZCB3aGF0IGdldENDVW1zZygpIGV4aXN0cyB0byBidWlsZCAtCiAgICAgICAgI3NlZSB0aGUg"
    "Y2FwdHVyZSBub3RlZCBuZXh0IHRvIGl0LiBCb3RoIGFubm91bmNlbWVudHMgaGVyZSB1c2VkIHRv"
    "IGVtaXQKICAgICAgICAjYSBvbmUtZmllbGQgJyRjaGF0Y2hhbm5lbHVzZXIgIm5hbWUiJyBpbnN0"
    "ZWFkLCBzbyB0aGUgZ3VpbGQgY29sdW1uIHdhcwogICAgICAgICNhbHdheXMgYmxhbmsgaW4gY2hh"
    "dCBubyBtYXR0ZXIgd2hhdCBndWlsZCBhIHBsYXllciB3YXMgaW4sIGFuZCB0aGUKICAgICAgICAj"
    "Y2xpZW50IGhhZCB0byBmaWxsIHRocmVlIGZpZWxkcyBpdCB3YXMgbmV2ZXIgZ2l2ZW4uIFRoZSAk"
    "Z2FtZWNoYW5uZWx1c2VyCiAgICAgICAgI3BhdGggbmV4dCBkb29yIGhhcyBhbHdheXMgc2VudCBp"
    "dHMgZnVsbCBmb3JtOyB0aGVzZSB0d28gd2VyZSB0aGUKICAgICAgICAjc3RyYWdnbGVycy4KICAg"
    "ICAgICBjb24uc2VydmVyLmRpc3QuYWRkKHsKICAgICAgICAgICAgJ3RhcmdldCc6bGlzdChzZWxm"
    "LmNoYXRDaGFubmVsc1tuYW1dKSwKICAgICAgICAgICAgJ21lc3NhZ2UnOmNvbi51c2VyLmdldEND"
    "VW1zZygpfSkKICAgICAgICBzZWxmLmNoYXRDaGFubmVsc1tuYW1dLmFwcGVuZChjb24pCiAgICAg"
    "ICAgY29uLnVzZXIuY2hhdGNoYW5uZWwgPSBzZWxmLmNoYXRDaGFubmVsc1tuYW1dCiAgICAgICAg"
    "dWwgPSAxI2xlbihjb24udXNlci5jaGF0Y2hhbm5lbCkKICAgICAgICByZXRtc2cgPSBfZW0oZicv"
    "am9pbmNoYXRjaGFubmVsICJ7bmFtfSIgIiIgInt1bH0iJykKICAgICAgICAjZW51bWVyYXRlIG90"
    "aGVyIGNoYXQgdXNlcnM/CiAgICAgICAgY2h1bmtzID0gW10KICAgICAgICBmb3IgdWNvbiBpbiBs"
    "aXN0KGNvbi51c2VyLmNoYXRjaGFubmVsKToKICAgICAgICAgICAgaWYgdWNvbiAhPSBjb246CiAg"
    "ICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKHVjb24udXNlci5nZXRDQ1Vtc2coKSkKICAgICAg"
    "ICByZXRtc2crPWInJy5qb2luKGNodW5rcykKICAgICAgICByZXR1cm4gcmV0bXNnCiAgICBkZWYg"
    "ZW51bUNoYXRzKHNlbGYpOgogICAgICAgIGNodW5rcyA9IFtdCiAgICAgICAgZm9yIGNoYXROYW1l"
    "IGluIGxpc3Qoc2VsZi5jaGF0Q2hhbm5lbHMpOgogICAgICAgICAgICB1bGwgPSBsZW4oc2VsZi5j"
    "aGF0Q2hhbm5lbHNbY2hhdE5hbWVdKSNUT0RPIGltcHJvdmUKICAgICAgICAgICAgY2h1bmtzLmFw"
    "cGVuZCh3aXJlX2VuY29kZShmJyRjaGF0Y2hhbm5lbCAie2NoYXROYW1lfSIgIiIgInt1bGx9Iicp"
    "KQogICAgICAgIGlmIG5vdCBjaHVua3M6CiAgICAgICAgICAgIHJldHVybiBiJycgI25ldmVyIGEg"
    "bG9uZSB0ZXJtaW5hdG9yOiB0aGF0IGlzIGFuIGVtcHR5IGNvbW1hbmQgbGluZQogICAgICAgIHJl"
    "dHVybiBfTi5qb2luKGNodW5rcykrX04KICAgIGRlZiBlbnVtR2FtZXMoc2VsZik6CiAgICAgICAg"
    "Y2h1bmtzID0gW10KICAgICAgICBmb3IgZ25hbWUgaW4gc2VsZi5nYW1lczoKICAgICAgICAgICAg"
    "Z2FtZXN0ciA9IHNlbGYuZ2FtZXNbZ25hbWVdLmdldEdhbWVTdHJpbmcoKQogICAgICAgICAgICBp"
    "ZiBnYW1lc3RyOgogICAgICAgICAgICAgICAgY2h1bmtzLmFwcGVuZChnYW1lc3RyKQogICAgICAg"
    "IHJldHVybiBiJycuam9pbihjaHVua3MpCiAgICBkZWYgdXBkYXRlUG9zKHNlbGYsIG1kKToKICAg"
    "ICAgICBpZiBub3Qgc2VsZi5kaXJ0eToKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgI0NsZWFy"
    "ZWQgQkVGT1JFIHRoZSBzY2FuLCBub3QgYWZ0ZXIuIEEgL3VwZGhlcm9wb3MgdGhhdCBhcnJpdmVk"
    "IHdoaWxlCiAgICAgICAgI3RoZSBsb29wIGJlbG93IHdhcyBydW5uaW5nIHVzZWQgdG8gc2V0IGRp"
    "cnR5PVRydWUgYW5kIHRoZW4gaGF2ZSBpdAogICAgICAgICNpbW1lZGlhdGVseSBjbGVhcmVkIGFn"
    "YWluLCBzbyB0aGF0IHBsYXllcidzIG1vdmUgd2FzIG5vdCBicm9hZGNhc3QKICAgICAgICAjdW50"
    "aWwgc29tZWJvZHkgZWxzZSBoYXBwZW5lZCB0byBtb3ZlLiBDbGVhcmluZyBmaXJzdCBtZWFucyB0"
    "aGUgd29yc3QKICAgICAgICAjY2FzZSBpcyBvbmUgcmVkdW5kYW50IHBhc3MsIG5vdCBhIHNpbGVu"
    "dGx5IGRyb3BwZWQgcG9zaXRpb24uCiAgICAgICAgc2VsZi5kaXJ0eSA9IEZhbHNlCiAgICAgICAg"
    "I1NuYXBzaG90OiBwbGF5ZXJzIGpvaW4gYW5kIGxlYXZlIHRoZSB0b3duIHdoaWxlIHRoaXMgaXRl"
    "cmF0ZXMuCiAgICAgICAgdGcgPSBsaXN0KHNlbGYudXNlcmxpc3QpCiAgICAgICAgbW92ZXJzID0g"
    "W10KICAgICAgICBmb3IgdWNvbiBpbiB0ZzoKICAgICAgICAgICAgaWYgbm90IHVjb24udXNlci5w"
    "b3NjaGFuZ2VkOgogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgdWNvbi51c2Vy"
    "LnBvc2NoYW5nZWQgPSBGYWxzZQogICAgICAgICAgICBpZiBub3QgdWNvbi51c2VyLmhlcm9kYXRh"
    "OgogICAgICAgICAgICAgICAgI0EgcGxheWVyIGlzIG9ubHkgYW5ub3VuY2VkIHRvIHRoZSBvdGhl"
    "cnMgYnkgJGdhbWVjaGFubmVsdXNlciwKICAgICAgICAgICAgICAgICNhbmQgZ2V0R0NVbXNnKCkg"
    "ZW1pdHMgbm90aGluZyBhdCBhbGwgdW50aWwgdGhlaXIgaGVyb2RhdGEgaGFzCiAgICAgICAgICAg"
    "ICAgICAjYXJyaXZlZC4gQnJvYWRjYXN0aW5nIGEgcG9zaXRpb24gZm9yIGEgaGVybyBpZCBub2Jv"
    "ZHkgaGFzCiAgICAgICAgICAgICAgICAjYmVlbiB0b2xkIGFib3V0IGhhbmRzIGV2ZXJ5IGNsaWVu"
    "dCBhbiB1cGRhdGUgZm9yIGEgcGxheWVyIGl0CiAgICAgICAgICAgICAgICAjZG9lcyBub3Qga25v"
    "dyBleGlzdHMuIFdhaXQgdW50aWwgdGhleSBhcmUgYSByZWFsLCBhbm5vdW5jZWQKICAgICAgICAg"
    "ICAgICAgICNwbGF5ZXIuCiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBtb3Zl"
    "cnMuYXBwZW5kKCh1Y29uLCBmJ3t1Y29uLnVzZXIud2lyZUlkKCl9I3t1Y29uLnVzZXIucG9zZGF0"
    "YX0nKSkKICAgICAgICBpZiBub3QgbW92ZXJzOgogICAgICAgICAgICAjRXZlcnlvbmUgd2hvIHdh"
    "cyBkaXJ0eSBoYXMgc2luY2UgbGVmdCB0aGUgdG93bi4gU2VuZGluZyB0aGUKICAgICAgICAgICAg"
    "I2FyZ3VtZW50LWxlc3MgJy91cGRoZXJvcG9zICcgdGhhdCB0aGlzIHVzZWQgdG8gcHJvZHVjZSBq"
    "dXN0IGhhbmRzCiAgICAgICAgICAgICN0aGUgY2xpZW50IGFuIGVtcHR5IGNvbW1hbmQgdG8gcGFy"
    "c2UuCiAgICAgICAgICAgIHJldHVybgogICAgICAgICNOb2JvZHkgaXMgdG9sZCB0aGVpciBvd24g"
    "cG9zaXRpb24uIFRoZSBjbGllbnQgaXMgdGhlIGF1dGhvcml0eSBvbgogICAgICAgICN3aGVyZSBp"
    "dHMgb3duIGhlcm8gaXMgLSBpdCBpcyB3aGF0IHNlbnQgdGhlIGNvb3JkaW5hdGVzIGluIHRoZSBm"
    "aXJzdAogICAgICAgICNwbGFjZSAtIHNvIGVjaG9pbmcgdGhlbSBiYWNrIGEgZnJhY3Rpb24gb2Yg"
    "YSBzZWNvbmQgbGF0ZXIgaXMgYXQgYmVzdAogICAgICAgICNyZWR1bmRhbnQgYW5kIGF0IHdvcnN0"
    "IGEgaGl0Y2gsIGFzIHRoZSBoZXJvIGlzIG51ZGdlZCBiYWNrIHRvIHdoZXJlCiAgICAgICAgI2l0"
    "IHN0b29kIHdoZW4gdGhlIHBhY2tldCBsZWZ0LiBFdmVyeSBvdGhlciBicm9hZGNhc3QgaW4gdGhp"
    "cyBmaWxlCiAgICAgICAgI2FscmVhZHkgZXhjbHVkZXMgdGhlIG9yaWdpbmF0b3IgKHNlZSBfd29V"
    "c2VyKTsgcG9zaXRpb25zIHdlcmUgdGhlCiAgICAgICAgI2V4Y2VwdGlvbi4gQ29zdHMgb25lIG1l"
    "c3NhZ2UgYnVpbHQgcGVyIG1vdmluZyBwbGF5ZXIsIGFuZCBub3Qgb25lCiAgICAgICAgI2V4dHJh"
    "IGJ5dGUgb24gdGhlIHdpcmU6IHRoZSBkaXN0cmlidXRvciBhbHJlYWR5IHdyaXRlcyB0byBlYWNo"
    "CiAgICAgICAgI3JlY2lwaWVudCBzZXBhcmF0ZWx5LgogICAgICAgIG1vdmVkID0gc2V0KHUgZm9y"
    "ICh1LCBfKSBpbiBtb3ZlcnMpCiAgICAgICAgd2F0Y2hlcnMgPSBbYyBmb3IgYyBpbiB0ZyBpZiBj"
    "IG5vdCBpbiBtb3ZlZF0KICAgICAgICBpZiB3YXRjaGVyczoKICAgICAgICAgICAgZm9yIG1zZyBp"
    "biBzZWxmLl9wb3NNZXNzYWdlcyhbY2ggZm9yIChfLCBjaCkgaW4gbW92ZXJzXSk6CiAgICAgICAg"
    "ICAgICAgICBtZC5hZGQoeyd0YXJnZXQnOndhdGNoZXJzLCdtZXNzYWdlJzptc2d9KQogICAgICAg"
    "IGZvciAodWNvbiwgXykgaW4gbW92ZXJzOgogICAgICAgICAgICBvdGhlcnMgPSBbY2ggZm9yICh1"
    "LCBjaCkgaW4gbW92ZXJzIGlmIHUgaXMgbm90IHVjb25dCiAgICAgICAgICAgIGlmIG5vdCBvdGhl"
    "cnM6CiAgICAgICAgICAgICAgICBjb250aW51ZSAjb25seSBtb3ZlciBpbiB0aGUgdG93biwgbm90"
    "aGluZyB0byB0ZWxsIHRoZW0KICAgICAgICAgICAgZm9yIG1zZyBpbiBzZWxmLl9wb3NNZXNzYWdl"
    "cyhvdGhlcnMpOgogICAgICAgICAgICAgICAgbWQuYWRkKHsndGFyZ2V0JzoodWNvbiwgKSwnbWVz"
    "c2FnZSc6bXNnfSkKICAgIGRlZiBfcG9zTWVzc2FnZXMoc2VsZiwgY2h1bmtzKToKICAgICAgICAj"
    "U3BsaXQgaW50byBzZXZlcmFsIGNvbW1hbmRzIHJhdGhlciB0aGFuIG9uZSBhcmJpdHJhcmlseSBs"
    "b25nIGxpbmUuCiAgICAgICAgIy91cGRoZXJvcG9zIGlzIHRoZSBvbmx5IG1lc3NhZ2Ugd2hvc2Ug"
    "bGVuZ3RoIGdyb3dzIHdpdGggdGhlIG51bWJlciBvZgogICAgICAgICNwbGF5ZXJzIC0gYSBidXN5"
    "IHRvd24gd291bGQgcHV0IGZpZnR5ICJpZCN4I3kiIGdyb3VwcyBvbiBhIHNpbmdsZQogICAgICAg"
    "ICNsaW5lLiBUaGUgcmV0YWlsIGNsaWVudCBpcyBhIDIwMDggMzItYml0IGJpbmFyeSBhbmQgaXRz"
    "IGxvYmJ5IHBhcnNlcgogICAgICAgICNjYW4gYmUgYXNzdW1lZCB0byB1c2UgZml4ZWQtc2l6ZSBi"
    "dWZmZXJzOyBoYW5kaW5nIGl0IGEgbGluZSBsb25nZXIKICAgICAgICAjdGhhbiBpdCBleHBlY3Rz"
    "IGlzIHRoZSBjbGFzc2ljIHdheSB0byBjb3JydXB0IGl0cyBoZWFwIGFuZCB0YWtlIGl0CiAgICAg"
    "ICAgI2Rvd24gd2l0aCBhbiBhY2Nlc3MgdmlvbGF0aW9uIHNvbWV3aGVyZSBlbHNlIGVudGlyZWx5"
    "LiBTZXZlcmFsIHNob3J0CiAgICAgICAgI2NvbW1hbmRzIGFyZSBlcXVpdmFsZW50IGZvciB0aGUg"
    "Y2xpZW50IGFuZCBjb3N0IG9uZSBleHRyYSBoZWFkZXIKICAgICAgICAjZWFjaC4KICAgICAgICBi"
    "YXRjaGVzID0gW10KICAgICAgICBjdXIgPSBbXQogICAgICAgIHByZWZpeCA9IGxlbignL3VwZGhl"
    "cm9wb3MgJykKICAgICAgICBjdXJsZW4gPSBwcmVmaXggI3RoZSBjb21tYW5kIHdvcmQgY291bnRz"
    "IHRvd2FyZHMgdGhlIGxpbmUsIGl0IHdhcyBub3QKICAgICAgICAgICAgICAgICAgICAgICAgI2Jl"
    "aW5nIGNvdW50ZWQsIHNvIGEgZnVsbCBiYXRjaCBvdmVyc2hvdCB0aGUgY2FwIGJ5IDEyCiAgICAg"
    "ICAgZm9yIGNoIGluIGNodW5rczoKICAgICAgICAgICAgaWYgY3VyIGFuZCBjdXJsZW4gKyBsZW4o"
    "Y2gpICsgMSA+IF9NQVhfV0lSRV9MSU5FOgogICAgICAgICAgICAgICAgYmF0Y2hlcy5hcHBlbmQo"
    "Y3VyKQogICAgICAgICAgICAgICAgY3VyID0gW10KICAgICAgICAgICAgICAgIGN1cmxlbiA9IHBy"
    "ZWZpeAogICAgICAgICAgICBjdXIuYXBwZW5kKGNoKQogICAgICAgICAgICBjdXJsZW4gKz0gbGVu"
    "KGNoKSArIDEKICAgICAgICBpZiBjdXI6CiAgICAgICAgICAgIGJhdGNoZXMuYXBwZW5kKGN1cikK"
    "ICAgICAgICByZXR1cm4gW19lbSgnL3VwZGhlcm9wb3MgJyArICcgJy5qb2luKGIpKSBmb3IgYiBp"
    "biBiYXRjaGVzXQogICAgZGVmIGRlYnVnX2Fycl9nYW1lcyhzZWxmKToKICAgICAgICBhY3REaWN0"
    "ID0gW10KICAgICAgICBmb3IgZ24sIGcgaW4gbGlzdChzZWxmLmdhbWVzLml0ZW1zKCkpOgogICAg"
    "ICAgICAgICBhY3REaWN0LmFwcGVuZChnLmRlYnVnX2RpY3QoKSkKICAgICAgICByZXR1cm4gYWN0"
    "RGljdAogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAgICAgcmV0dXJuIHsKICAgICAgICAg"
    "ICAgJ3VzZXJzJzp0dXBsZShbYy51c2VyLm5hbWUgZm9yIGMgaW4gc2VsZi51c2VybGlzdF0pLAog"
    "ICAgICAgICAgICAnbWF4VXNlcnMnOnNlbGYubWF4dXNlciwKICAgICAgICAgICAgJ2dhbWVzJzp0"
    "dXBsZShbZ24gZm9yIGduIGluIHNlbGYuZ2FtZXNdKQogICAgICAgIH0KCl9NQVBOQU1FUyA9IFsn"
    "TmV0X1RfMDEnLCdOZXRfVF8wMicsJ05ldF9UXzAzJywnTmV0X1RfMDQnXSAjVE9ETyB1c2UgQ0ZH"
    "IG9iamVjdApjbGFzcyBHYW1lU3RhdGUoKToKICAgICNUT0RPIGF1dG8gZ3Jvd2FibGUgY2hhbm5l"
    "bHMsIFttYXBuYW1lXQogICAgI1RPRE8gYXZhaWxhYmxlIGluZGV4ZXMsIFttYXBuYW1lXQogICAg"
    "ZGVmIF9faW5pdF9fKHNlbGYsIHNlcnZlcik6CiAgICAgICAgI2luc3RhbmNlIGF0dHJpYnV0ZXMs"
    "IG5vdCBjbGFzcyBhdHRyaWJ1dGVzOiB0aGVzZSBtdXN0IE5PVCBiZSBzaGFyZWQKICAgICAgICAj"
    "YmV0d2VlbiBzZXBhcmF0ZSBDb3JlU2VydmVyIGluc3RhbmNlcyAoZS5nLiBzdG9wL3N0YXJ0IGZy"
    "b20gYSBHVUkKICAgICAgICAjd2l0aGluIHRoZSBzYW1lIHByb2Nlc3MpIG9yIGxlZnRvdmVyIHBs"
    "YXllcnMvY2hhbm5lbHMgZnJvbSBhCiAgICAgICAgI3ByZXZpb3VzIHJ1biB3b3VsZCBsZWFrIGlu"
    "dG8gdGhlIG5ldyBvbmUuCiAgICAgICAgc2VsZi5hY3RpdmVVc2VycyA9IHt9ICNUT0RPIHRyYWNr"
    "IHVzZXIgaGlzdG9yeT8gb3B0aW9uYWxseQogICAgICAgIHNlbGYuZ2FtZUNoYW5uZWxzID0ge30g"
    "I2NoYW5uZWxbXSwga2V5ZWQgYnkgbWFwbmFtZQogICAgICAgIHNlbGYuc2VydmVyPXNlcnZlcgog"
    "ICAgICAgIHNlbGYudXNlckxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgZm9yIG5hbWUg"
    "aW4gX01BUE5BTUVTOgogICAgICAgICAgICBmb3IgaSBpbiByYW5nZSgxKTogI1RPRE8gY29uZmln"
    "dXJlYWJsZSB1cCB0byAyMD8KICAgICAgICAgICAgICAgIGNobk5hbWUgPSBfZ2NobmwobmFtZSwg"
    "MStpKQogICAgICAgICAgICAgICAgc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0gPSBHYW1lQ2hh"
    "bm5lbChzZWxmLnNlcnZlciwgY2huTmFtZSkgI1RPRE8gMSBhbmQgZ3Jvdz8KICAgIGRlZiBjbGFp"
    "bVVzZXIoc2VsZiwgbmFtZSwgY29uKToKICAgICAgICAjUHVibGlzaCBjb24gYXMgVEhFIGxpdmUg"
    "c2Vzc2lvbiBmb3IgbmFtZSwgYXRvbWljYWxseS4gVGhlIG9sZCBjb2RlCiAgICAgICAgI2NoZWNr"
    "ZWQgZ2V0UGxheWVyKCkgZHVyaW5nIGxvZ2luIGFuZCB0aGVuIGluc2VydGVkIGludG8gYWN0aXZl"
    "VXNlcnMKICAgICAgICAjbXVjaCBsYXRlciwgaW4gX2xvYmJ5SGFuZGxlOyB0d28gY29ubmVjdGlv"
    "bnMgbG9nZ2luZyBpbiBhcyB0aGUgc2FtZQogICAgICAgICNhY2NvdW50IGF0IG9uY2UgYm90aCBw"
    "YXNzZWQgdGhlIGNoZWNrLCBhbmQgdGhlIHNlY29uZCBvbmUncyBpbnNlcnQKICAgICAgICAjb3Zl"
    "cndyb3RlIHRoZSBmaXJzdC4gVGhlIGxvc2VyIHRoZW4gZGVsZXRlZCB0aGUgd2lubmVyJ3MgZW50"
    "cnkgd2hlbiBpdAogICAgICAgICNkaXNjb25uZWN0ZWQsIGxlYXZpbmcgYSBjb25uZWN0ZWQgcGxh"
    "eWVyIGludmlzaWJsZSB0byB0aGUgc2VydmVyIChubwogICAgICAgICNraWNrLCBubyB3aG9pcywg"
    "bm8gbWVzc2FnZXMpLgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAgICAgICAgaWYg"
    "bmFtZSBpbiBzZWxmLmFjdGl2ZVVzZXJzOgogICAgICAgICAgICAgICAgcmV0dXJuIEZhbHNlCiAg"
    "ICAgICAgICAgIHNlbGYuYWN0aXZlVXNlcnNbbmFtZV0gPSBjb24KICAgICAgICAgICAgcmV0dXJu"
    "IFRydWUKICAgIGRlZiByZWxlYXNlVXNlcihzZWxmLCBuYW1lLCBjb24pOgogICAgICAgICNvbmx5"
    "IGNsZWFyIHRoZSBzbG90IGlmIHdlIHN0aWxsIG93biBpdCwgbmV2ZXIgc29tZW9uZSBlbHNlJ3Mg"
    "c2Vzc2lvbgogICAgICAgIHdpdGggc2VsZi51c2VyTG9jazoKICAgICAgICAgICAgaWYgc2VsZi5h"
    "Y3RpdmVVc2Vycy5nZXQobmFtZSkgaXMgY29uOgogICAgICAgICAgICAgICAgZGVsIHNlbGYuYWN0"
    "aXZlVXNlcnNbbmFtZV0KICAgIGRlZiBlbnVtZXJhdGVHQyhzZWxmKToKICAgICAgICBjaG5zID0g"
    "W10KICAgICAgICBmb3IgY2huTmFtZSBpbiBsaXN0KHNlbGYuZ2FtZUNoYW5uZWxzKToKICAgICAg"
    "ICAgICAgY2huID0gc2VsZi5nYW1lQ2hhbm5lbHNbY2huTmFtZV0KICAgICAgICAgICAgY2hucy5h"
    "cHBlbmQod2lyZV9lbmNvZGUoZickZ2FtZWNoYW5uZWwgIntjaG5OYW1lfSIgIntsZW4oY2huLnVz"
    "ZXJsaXN0KX0iICJ7Y2huLm1heHVzZXJ9IiAiMCIgIjAiJykpICNUT0RPIEF2YWlsYWJsZSAtIEFs"
    "bAogICAgICAgIGlmIG5vdCBjaG5zOgogICAgICAgICAgICByZXR1cm4gYicnICNzZWUgZW51bUNo"
    "YXRzCiAgICAgICAgcmV0dXJuIF9OLmpvaW4oY2hucykrX04KICAgIGRlZiB1cGRhdGVQb3Moc2Vs"
    "Zik6CiAgICAgICAgbWQgPSBzZWxmLnNlcnZlci5kaXN0CiAgICAgICAgZm9yIGNobiBpbiBsaXN0"
    "KHNlbGYuZ2FtZUNoYW5uZWxzLnZhbHVlcygpKToKICAgICAgICAgICAgY2huLnVwZGF0ZVBvcyht"
    "ZCkKI2hhbmRsZXMgaW50ZXJhY3Rpb25zIGJldHdlZW4gYWxsIGVsZW1lbnRzCmNsYXNzIENvcmVT"
    "ZXJ2ZXIoc29ja2V0c2VydmVyLlRocmVhZGluZ1RDUFNlcnZlcik6CiAgICBhbGxvd19yZXVzZV9h"
    "ZGRyZXNzID0gVHJ1ZSAjIFRPRE8gY2hlY2sgaWYgaW1wcm92ZXMgcmVzdGFydCB0aW1lcyB3aXRo"
    "b3V0IG90aGVyIGlzc3VlcwogICAgZGFlbW9uX3RocmVhZHMgPSBUcnVlCiAgICBibG9ja19vbl9j"
    "bG9zZSA9IEZhbHNlCiAgICBfaXNfY2xvc2luZyA9IEZhbHNlCiAgICBkZWYgX19pbml0X18oc2Vs"
    "Zik6CiAgICAgICAgI1RPRE8gZ2V0IHZhbHVlcyBmcm9tIGNmZwogICAgICAgICNhZGRyZXNzID0g"
    "J2xvY2FsaG9zdCcKICAgICAgICBhZGRyZXNzID0gJycKICAgICAgICBwb3J0ID0gX1RXX0xPQkJZ"
    "X1BPUlQKICAgICAgICBwcmludChmJ0luaXRpYWxpemluZyBzZXJ2ZXIgZm9yIHBvcnQge3BvcnR9"
    "JykKICAgICAgICBzdXBlcigpLl9faW5pdF9fKChhZGRyZXNzLCBwb3J0KSwgQ29ubmVjdGlvbkhh"
    "bmRsZXIpCiAgICAgICAgc2VsZi5kaXN0ID0gTWVzc2FnZURpc3RyaWJ1dG9yKHNlbGYpCiAgICAg"
    "ICAgc2VsZi5jb21wYXJzID0gQ29tbWFuZFBhcnNlcihzZWxmLmRpc3QpCiAgICAgICAgc2VsZi5z"
    "dGF0ZSA9IEdhbWVTdGF0ZShzZWxmKQogICAgICAgIHNlbGYuc3RhcnRUaW1lID0gZGF0ZXRpbWUu"
    "ZGF0ZXRpbWUubm93KCkKICAgICAgICBzZWxmLnNlcnZpY2VfdGljayA9IDAKICAgICAgICBzZWxm"
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
    "cnZhbHMKICAgICAgICAjUmVhZCBsaXZlLCBub3QgZnJvbSB0aGUgY29weSB0YWtlbiB3aGVuIHRo"
    "aXMgc2VydmVyIG9iamVjdCB3YXMgYnVpbHQuCiAgICAgICAgI0V2ZXJ5IG90aGVyIHN5bmNocm9u"
    "aXNhdGlvbiBzZXR0aW5nIHRha2VzIGVmZmVjdCBvbiBhIHJ1bm5pbmcgc2VydmVyIC0KICAgICAg"
    "ICAjYXBwbHlDb25maWcoKSB3cml0ZXMgdGhlIG1vZHVsZSBnbG9iYWxzIGFuZCB0aGUgbG9vcHMg"
    "cmUtcmVhZCB0aGVtIC0KICAgICAgICAjd2hpY2ggbWFkZSB0aGlzIHRoZSBvbmUgc3dpdGNoIGlu"
    "IHRoYXQgZ3JvdXAgdGhhdCBzaWxlbnRseSBkaWQgbm90aGluZwogICAgICAgICN1bnRpbCB0aGUg"
    "bmV4dCByZXN0YXJ0LCB3aGlsZSB0aGUgR1VJIHNhaWQgb3RoZXJ3aXNlLgogICAgICAgIGlmIF9T"
    "RU5EX05PUFMgYW5kIChzZWxmLnNlcnZpY2VfdGljayUzKT09MDoKICAgICAgICAgICAgc2VsZi5k"
    "aXN0LmFkZCh7J3RhcmdldCc6c2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy52YWx1ZXMoKSwnbWVzc2Fn"
    "ZSc6X2VtKCcvbm9wJyl9KQogICAgICAgICAgICAjc2VuZCAnL25vcCcgdG8gYWxsIGV2ZXJ5IDMg"
    "c2VjIG9wdGlvbmFsbHkKICAgICAgICAjc2VydmljZSB0aWNrIDMgZGF5IHJlc2V0IGludGVydmFs"
    "IFRPRE8gdGVzdCBhbGlnbm1lbnQgd2l0aCBvdGhlciBmYWN0b3JzCiAgICAgICAgc2VsZi5zZXJ2"
    "aWNlX3RpY2sgPSAoc2VsZi5zZXJ2aWNlX3RpY2srMSklKDYwKjYwKjI0KjMpCiAgICAgICAgc3Vw"
    "ZXIoKS5zZXJ2aWNlX2FjdGlvbnMoKQogICAgZGVmIHNlcnZlX2ZvcmV2ZXIoc2VsZik6CiAgICAg"
    "ICAgZGlzdFRocmVhZCA9IHRocmVhZGluZy5UaHJlYWQodGFyZ2V0PXNlbGYuZGlzdC5zZXJ2ZV9m"
    "b3JldmVyKQogICAgICAgIGRpc3RUaHJlYWQuc3RhcnQoKQogICAgICAgIHNlbGYuX3Bvc1N0b3Au"
    "Y2xlYXIoKQogICAgICAgIHNlbGYuX3Bvc1RocmVhZCA9IHRocmVhZGluZy5UaHJlYWQodGFyZ2V0"
    "PXNlbGYuX3Bvc0xvb3AsIGRhZW1vbj1UcnVlKQogICAgICAgIHNlbGYuX3Bvc1RocmVhZC5zdGFy"
    "dCgpCiAgICAgICAgI3BvbGxfaW50ZXJ2YWwgaXMgbm93IG9ubHkgdGhlIGFjY2VwdCBsb29wJ3Mg"
    "c2h1dGRvd24gcmVzcG9uc2l2ZW5lc3MgLQogICAgICAgICNwb3NpdGlvbiBicm9hZGNhc3RzIG5v"
    "IGxvbmdlciByaWRlIG9uIGl0CiAgICAgICAgc3VwZXIoKS5zZXJ2ZV9mb3JldmVyKDEpCiAgICAg"
    "ICAgc2VsZi5fcG9zU3RvcC5zZXQoKQogICAgICAgIGlmIHNlbGYuX3Bvc1RocmVhZDoKICAgICAg"
    "ICAgICAgc2VsZi5fcG9zVGhyZWFkLmpvaW4odGltZW91dD0yLjApCiAgICAgICAgICAgIHNlbGYu"
    "X3Bvc1RocmVhZCA9IE5vbmUKICAgICAgICBzZWxmLmRpc3QuZW5kKCkjaW4gY2FzZSBpdCBoYXNu"
    "J3QgYWxyZWFkeQogICAgICAgIGRpc3RUaHJlYWQuam9pbigpCiAgICBkZWYgaGFuZGxlX3NpZ25h"
    "bChzZWxmLCB0aW1lb3V0KToKICAgICAgICBkZWYgaGFuZGxlcihzaWdudW0sIF8pOgogICAgICAg"
    "ICAgICBkZWFkbGluZSA9IHRpbWUubW9ub3RvbmljKCkgKyB0aW1lb3V0CiAgICAgICAgICAgIHNp"
    "Z25hbWUgPSBzaWduYWwuU2lnbmFscyhzaWdudW0pLm5hbWUKICAgICAgICAgICAgc2VsZi5faXNf"
    "Y2xvc2luZyA9IFRydWUgI1RPRE8gcHJvcGVybHkgZW5kIGNvbm5lY3Rpb25zIGFmdGVyIGEgZGVs"
    "YXkKICAgICAgICAgICAgcHJpbnQoZidDbG9zaW5nIGluIHt0aW1lb3V0fScpCiAgICAgICAgICAg"
    "ICN3aGlsZSAoY3VycmVudF90aW1lIDo9IHRpbWUubW9ub3RvbmljKCkpIDwgZGVhZGxpbmU6CiAg"
    "ICAgICAgICAgICMgICAgZGVsdGEgPSBpbnQoZGVhZGxpbmUgLSBjdXJyZW50X3RpbWUpCiAgICAg"
    "ICAgICAgICAgICAjVE9ETyBzaWduYWwgdG8gcGxheWVycyB0aGF0IGNvbm5lY3Rpb24gaXMgc2h1"
    "dHRpbmcgZG93bgogICAgICAgICAgICAgICAgIy0gc2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy52YWx1"
    "ZXMoKQogICAgICAgICAgICAgICAgIy0gZicvYWRtaW4gU2VydmVyIGNsb3NpbmcgaW4ge2RlbHRh"
    "fScuZW5jb2RlKCdhc2NpaScpK19OCiAgICAgICAgICAgICAgICAjTE9HIENMT1NFCiAgICAgICAg"
    "ICAgICAgICAjVE9ETyBiZXR0ZXIgc2h1dGRvd24gaGFuZGxpbmcKICAgICAgICAgICAgIyAgICB0"
    "aW1lLnNsZWVwKDEpCiAgICAgICAgICAgIHRpbWUuc2xlZXAodGltZW91dCkjYWx0IHdoaWxlIG90"
    "aGVyIHN0dWZmIGlzIG9uZ29pbmcKICAgICAgICAgICAgc2VsZi5fQmFzZVNlcnZlcl9fc2h1dGRv"
    "d25fcmVxdWVzdCA9IFRydWUKICAgICAgICAgICAgI3NlbGYuc2h1dGRvd24oKSAjb25seSBpZiBz"
    "ZXJ2ZV9mb3JldmVyIGlzIGluIGEgZGlmZmVyZW50IHRocmVhZAogICAgICAgICAgICAjc2VsZi5z"
    "ZXJ2ZXJfY2xvc2UoKSAjb25seSBuZWVkZWQgaWYgbm90IHVzaW5nIGEgd2l0aCBzdGF0ZW1lbnQK"
    "ICAgICAgICByZXR1cm4gaGFuZGxlcgogICAgZGVmIHJlZ2lzdGVyQ29ubmVjdGlvbihzZWxmLCBj"
    "b24pOgogICAgICAgIHdpdGggc2VsZi5fY29ubkxvY2s6CiAgICAgICAgICAgIHNlbGYuX2Nvbm5z"
    "LmFkZChjb24pCiAgICBkZWYgdW5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZiwgY29uKToKICAgICAg"
    "ICB3aXRoIHNlbGYuX2Nvbm5Mb2NrOgogICAgICAgICAgICBzZWxmLl9jb25ucy5kaXNjYXJkKGNv"
    "bikKICAgIGRlZiBjbG9zZUNvbm5lY3Rpb25zKHNlbGYpOgogICAgICAgICNEcm9wIGV2ZXJ5IGNs"
    "aWVudC4gU2h1dHRpbmcgdGhlIHNvY2tldCBkb3duIHVuYmxvY2tzIHdoaWNoZXZlcgogICAgICAg"
    "ICNzZWxlY3QoKS9yZWN2KCkgdGhhdCBjb25uZWN0aW9uJ3MgdGhyZWFkIGlzIHNpdHRpbmcgaW4s"
    "IHNvIGl0IHJ1bnMKICAgICAgICAjaXRzIG5vcm1hbCBjbGVhbnVwIHBhdGggYW5kIGV4aXRzIGlu"
    "c3RlYWQgb2YgbGluZ2VyaW5nLgogICAgICAgIHdpdGggc2VsZi5fY29ubkxvY2s6CiAgICAgICAg"
    "ICAgIGNvbm5zID0gbGlzdChzZWxmLl9jb25ucykKICAgICAgICBmb3IgY29uIGluIGNvbm5zOgog"
    "ICAgICAgICAgICBjb24uZHJvcCgpCiAgICAgICAgcmV0dXJuIGxlbihjb25ucykKICAgIGRlZiBz"
    "aHV0ZG93bihzZWxmKToKICAgICAgICAjU3RvcHBpbmcgdGhlIHNlcnZlciBtZWFucyBzdG9wcGlu"
    "ZyBpdDogZmxhZyBpdCBmaXJzdCBzbyB0aGUgcmVhZAogICAgICAgICNsb29wcyBiYWlsIG91dCBy"
    "YXRoZXIgdGhhbiBzZXJ2aW5nIGFub3RoZXIgY29tbWFuZCwgdGhlbiBzdG9wIHRoZQogICAgICAg"
    "ICNhY2NlcHQgbG9vcCwgdGhlbiBldmljdCBldmVyeW9uZSBzdGlsbCBjb25uZWN0ZWQuCiAgICAg"
    "ICAgc2VsZi5faXNfY2xvc2luZyA9IFRydWUKICAgICAgICBzdXBlcigpLnNodXRkb3duKCkKICAg"
    "ICAgICBuID0gc2VsZi5jbG9zZUNvbm5lY3Rpb25zKCkKICAgICAgICBpZiBuOgogICAgICAgICAg"
    "ICBwcmludChmJ1tMb2JieV0gQ2xvc2VkIHtufSBjbGllbnQgY29ubmVjdGlvbihzKSBvbiBzaHV0"
    "ZG93bicpCiAgICBkZWYgZ2V0UGxheWVyKHNlbGYsIHVzZXJuYW1lKToKICAgICAgICByZXR1cm4g"
    "c2VsZi5zdGF0ZS5hY3RpdmVVc2Vycy5nZXQodXNlcm5hbWUpCiAgICBkZWYga2lja1BsYXllcihz"
    "ZWxmLCB1c2VybmFtZSwgcmVhc29uPSdLaWNrZWQgYnkgYWRtaW4nKToKICAgICAgICAjQWRtaW4t"
    "cGFuZWwgYWN0aW9uOiBmb3JjaWJseSBkaXNjb25uZWN0IGEgY29ubmVjdGVkIHBsYXllci4gU2Vu"
    "ZHMgYQogICAgICAgICNiZXN0LWVmZm9ydCAvYWRtaW4gbm90aWNlIGZpcnN0IChjbGllbnQgc2hv"
    "d3MgaXQgbGlrZSBhbnkgb3RoZXIKICAgICAgICAjc2VydmVyIGFkbWluIG1lc3NhZ2UpLCB0aGVu"
    "IHNodXRzIGRvd24gdGhlIHNvY2tldCBzbyB0aGUgcGxheWVyJ3MKICAgICAgICAjaGFuZGxlciB0"
    "aHJlYWQgdW5ibG9ja3MgZnJvbSBpdHMgcmVjdigpIGFuZCBydW5zIGl0cyBub3JtYWwKICAgICAg"
    "ICAjZGlzY29ubmVjdC9jbGVhbnVwIHBhdGguCiAgICAgICAgY29uID0gc2VsZi5nZXRQbGF5ZXIo"
    "dXNlcm5hbWUpCiAgICAgICAgaWYgY29uIGlzIE5vbmU6CiAgICAgICAgICAgIHJldHVybiBGYWxz"
    "ZQogICAgICAgICNRdWV1ZWQsIG5vdCB3cml0dGVuIGlubGluZS4gc2VuZFJhdygpIHRha2VzIHRo"
    "YXQgY29ubmVjdGlvbidzIHNlbmQKICAgICAgICAjbG9jaywgYW5kIGl0cyB3cml0ZXIgdGhyZWFk"
    "IGhvbGRzIHRoYXQgbG9jayBmb3IgdGhlIHdob2xlIG9mIGEKICAgICAgICAjYmxvY2tpbmcgc2Vu"
    "ZGFsbCgpIC0gc28ga2lja2luZyBhIHBsYXllciB3aG9zZSBsaW5rIGhhZCBzdGFsbGVkIGJsb2Nr"
    "ZWQKICAgICAgICAjd2hvZXZlciBjYWxsZWQgdGhpcyB1bnRpbCB0aGUgc3RhbGxlZCBjbGllbnQg"
    "d2VudCBhd2F5LCBhbmQgdGhlIGNhbGxlcgogICAgICAgICNoZXJlIGlzIHRoZSBHVUkgdGhyZWFk"
    "LiBUaGUgYWRtaW4gcGFuZWwgZnJvemUgb24gZXhhY3RseSB0aGUgcGxheWVyIGl0CiAgICAgICAg"
    "I3dhcyB0cnlpbmcgdG8gZ2V0IHJpZCBvZi4gQSBxdWV1ZSBwdXQgY2Fubm90IGJsb2NrLgogICAg"
    "ICAgIHRyeToKICAgICAgICAgICAgY29uLnNlbmQoX2VtKGYnL2FkbWluIHtyZWFzb259JykpCiAg"
    "ICAgICAgZXhjZXB0IEV4Y2VwdGlvbjoKICAgICAgICAgICAgcGFzcyAjYmVzdCBlZmZvcnQsIGNv"
    "bm5lY3Rpb24gbWF5IGFscmVhZHkgYmUgb24gaXRzIHdheSBvdXQKICAgICAgICBjb24uZmx1c2hQ"
    "ZW5kaW5nKDAuMykgI2JvdW5kZWQ6IGdpdmUgdGhlIG5vdGljZSBhIGNoYW5jZSB0byBnbyBvdXQK"
    "ICAgICAgICBjb24uZHJvcCgpCiAgICAgICAgcmV0dXJuIFRydWUKICAgIGRlZiBkZWxldGVBY2Nv"
    "dW50KHNlbGYsIHVzZXJuYW1lKToKICAgICAgICAjQWRtaW4tcGFuZWwgYWN0aW9uOiBwZXJtYW5l"
    "bnRseSBkZWxldGVzIGEgY2hhcmFjdGVyL2FjY291bnQuCiAgICAgICAgI0tpY2tzIGZpcnN0IChu"
    "by1vcCBpZiBhbHJlYWR5IG9mZmxpbmUpIHNvIGEgY29ubmVjdGVkIGNsaWVudCBuZXZlcgogICAg"
    "ICAgICNrZWVwcyBwbGF5aW5nIG9uIGFuIGFjY291bnQgdGhhdCBoYXMganVzdCB2YW5pc2hlZCBm"
    "cm9tIHRoZSBEQi4KICAgICAgICBzZWxmLmtpY2tQbGF5ZXIodXNlcm5hbWUsIHJlYXNvbj0nQWNj"
    "b3VudCBkZWxldGVkIGJ5IGFkbWluJykKICAgICAgICByZXR1cm4gR0RILmRlbGV0ZUFjY291bnQo"
    "dXNlcm5hbWUpCiNGYWlsZWQtbG9naW4gdGhyb3R0bGUsIHBlciBzb3VyY2UgSVAuCiNUd28gcmVh"
    "c29ucyB0aGlzIGlzIG5vdCBvcHRpb25hbCBvbiBhIHNlcnZlciByZWFjaGFibGUgZnJvbSB0aGUg"
    "aW50ZXJuZXQ6CiNhIHBhc3N3b3JkIGd1ZXNzIGlzIGNoZWFwIGZvciB0aGUgYXR0YWNrZXIgYnV0"
    "IGNvc3RzICp1cyogYSAxMDBrLWl0ZXJhdGlvbgojUEJLREYyICh0ZW5zIG9mIG1zIG9mIENQVSBl"
    "YWNoKSwgc28gYW4gdW50aHJvdHRsZWQgbG9naW4gZW5kcG9pbnQgaXMgYm90aCBhCiNicnV0ZS1m"
    "b3JjZSBvcmFjbGUgYW5kIGEgQ1BVIGFtcGxpZmllciAtIGEgaGFuZGZ1bCBvZiBjb25uZWN0aW9u"
    "cyBjYW4gcGluCiNldmVyeSBjb3JlLiBTdWNjZXNzZnVsIGxvZ2lucyBjbGVhciB0aGUgY291bnRl"
    "ciwgc28gYSBwbGF5ZXIgZnVtYmxpbmcgdGhlaXIKI3Bhc3N3b3JkIGEgZmV3IHRpbWVzIGlzIG5l"
    "dmVyIGxvY2tlZCBvdXQgZm9yIGxvbmcuCl9MT0dJTl9GQUlMX0xJTUlUID0gNiAgICAgICNmYWls"
    "dXJlcyBhbGxvd2VkIGluc2lkZSB0aGUgd2luZG93IGJlZm9yZSBkZWxheWluZwpfTE9HSU5fRkFJ"
    "TF9XSU5ET1cgPSAzMDAgICAjc2Vjb25kcyBhIGZhaWx1cmUgaXMgcmVtZW1iZXJlZApfTE9HSU5f"
    "RkFJTF9ERUxBWSA9IDIuMCAgICAjc2Vjb25kcyB0byBzdGFsbCBlYWNoIGF0dGVtcHQgb25jZSBv"
    "dmVyIHRoZSBsaW1pdApjbGFzcyBMb2dpblRocm90dGxlKCk6CiAgICBkZWYgX19pbml0X18oc2Vs"
    "Zik6CiAgICAgICAgc2VsZi5sb2NrID0gdGhyZWFkaW5nLkxvY2soKQogICAgICAgIHNlbGYuZmFp"
    "bHMgPSB7fSAjaXAgLT4gW3RpbWVzdGFtcHNdCiAgICBkZWYgX3BydW5lKHNlbGYsIGlwLCBub3cp"
    "OgogICAgICAgIHJlY2VudCA9IFt0IGZvciB0IGluIHNlbGYuZmFpbHMuZ2V0KGlwLCAoKSkgaWYg"
    "bm93IC0gdCA8IF9MT0dJTl9GQUlMX1dJTkRPV10KICAgICAgICBpZiByZWNlbnQ6CiAgICAgICAg"
    "ICAgIHNlbGYuZmFpbHNbaXBdID0gcmVjZW50CiAgICAgICAgZWxzZToKICAgICAgICAgICAgc2Vs"
    "Zi5mYWlscy5wb3AoaXAsIE5vbmUpCiAgICAgICAgcmV0dXJuIHJlY2VudAogICAgZGVmIGRlbGF5"
    "Rm9yKHNlbGYsIGlwKToKICAgICAgICBub3cgPSB0aW1lLm1vbm90b25pYygpCiAgICAgICAgd2l0"
    "aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHJlY2VudCA9IHNlbGYuX3BydW5lKGlwLCBub3cpCiAg"
    "ICAgICAgcmV0dXJuIF9MT0dJTl9GQUlMX0RFTEFZIGlmIGxlbihyZWNlbnQpID49IF9MT0dJTl9G"
    "QUlMX0xJTUlUIGVsc2UgMC4wCiAgICBkZWYgcmVjb3JkRmFpbHVyZShzZWxmLCBpcCk6CiAgICAg"
    "ICAgbm93ID0gdGltZS5tb25vdG9uaWMoKQogICAgICAgIHdpdGggc2VsZi5sb2NrOgogICAgICAg"
    "ICAgICByZWNlbnQgPSBzZWxmLl9wcnVuZShpcCwgbm93KQogICAgICAgICAgICByZWNlbnQuYXBw"
    "ZW5kKG5vdykKICAgICAgICAgICAgc2VsZi5mYWlsc1tpcF0gPSByZWNlbnQKICAgICAgICAgICAg"
    "cmV0dXJuIGxlbihyZWNlbnQpCiAgICBkZWYgcmVjb3JkU3VjY2VzcyhzZWxmLCBpcCk6CiAgICAg"
    "ICAgd2l0aCBzZWxmLmxvY2s6CiAgICAgICAgICAgIHNlbGYuZmFpbHMucG9wKGlwLCBOb25lKQpM"
    "T0dJTl9USFJPVFRMRSA9IExvZ2luVGhyb3R0bGUoKQoKX0xPR0lOX0VSUk9SUyA9IHsKICAgIDE6"
    "ICdJbnZhbGlkIHVzZXJuYW1lIG9yIHBhc3N3b3JkJywKICAgIDI6ICdBY2NvdW50IGFscmVhZHkg"
    "bG9nZ2VkIGluJywKICAgIDM6ICdQYXNzd29yZCByZXF1aXJlZCcsCiAgICA0OiAnVXNlcm5hbWUg"
    "cmVxdWlyZWQnLAogICAgI0FjY291bnRzIGFyZSB0aWVkIHRvIHRoZSBzZXJpYWwgdGhlIGNsaWVu"
    "dCBoYW5kc2hha2VzIHdpdGgsIHNvIGEKICAgICNyZWluc3RhbGxlZCBvciByZS1rZXllZCBnYW1l"
    "IGNhbm5vdCByZWFjaCBhbiBleGlzdGluZyBhY2NvdW50IG5vIG1hdHRlcgogICAgI3doYXQgcGFz"
    "c3dvcmQgaXQgdHlwZXMuIFNheSB0aGF0LCByYXRoZXIgdGhhbiBibGFtaW5nIHRoZSBuYW1lLgog"
    "ICAgNTogJ1RoaXMgbmFtZSBiZWxvbmdzIHRvIGFuIGFjY291bnQgcmVnaXN0ZXJlZCB3aXRoIGEg"
    "ZGlmZmVyZW50IGdhbWUgc2VyaWFsJywKfQpfUkVHSVNURVJfRVJST1JTID0gewogICAgMTogJ0Fj"
    "Y291bnQgYWxyZWFkeSBsb2dnZWQgaW4nLAogICAgMjogJ1VzZXJuYW1lIHVuYXZhaWxhYmxlIG9y"
    "IGludmFsaWQnLAp9CiNDZWlsaW5nIG9uIGhvdyBtdWNoIHVuc2VudCBkYXRhIG1heSBwaWxlIHVw"
    "IGZvciBhIHNpbmdsZSBjbGllbnQuIFRoZSB3cml0ZXIKI3RocmVhZCBibG9ja3MgaW5zaWRlIHNl"
    "bmRhbGwoKSBmb3IgZXhhY3RseSBhcyBsb25nIGFzIGEgY2xpZW50IHJlZnVzZXMgdG8gcmVhZCwK"
    "I2FuZCBhIGZyb3plbiBnYW1lIGRvZXMgcHJlY2lzZWx5IHRoYXQgLSB3aGlsZSBhbHNvIHNlbmRp"
    "bmcgbm90aGluZywgc28gbm90aGluZwojZWxzZSBub3RpY2VzIGl0IHVudGlsIGEgZnVsbCBpZGxl"
    "IHRpbWVvdXQgaGFzIHBhc3NlZC4gRm9yIHRob3NlIG1pbnV0ZXMgZXZlcnkKI3Bvc2l0aW9uIGJy"
    "b2FkY2FzdCwgZXZlcnkgY2hhdCBsaW5lIGFuZCBldmVyeSByZWxheWVkIGdhbWUgY29tbWFuZCBm"
    "b3IgdGhhdAojcGxheWVyIGtlcHQgYmVpbmcgYXBwZW5kZWQgdG8gYW4gdW5ib3VuZGVkIHF1ZXVl"
    "LiBCb3VuZGluZyBpdCB0dXJucyAidGhlIHNlcnZlcgojcXVpZXRseSBncm93cyBvbiBiZWhhbGYg"
    "b2YgYSBjbGllbnQgdGhhdCBpcyBhbHJlYWR5IGdvbmUiIGludG8gYSBjbGVhbiBkcm9wCiN3aXRo"
    "IGEgbGluZSBpbiB0aGUgbG9nLiBTaXplZCBmYXIgYWJvdmUgYW55IGxlZ2l0aW1hdGUgYnVyc3Q6"
    "IHRoZSBsYXJnZXN0CiNzaW5nbGUgdGhpbmcgdGhhdCBnb2VzIG91dCBpcyBhIGhlcm9kYXRhIGJs"
    "b2IsIGFuZCBhIHdob2xlIHRvd24gb2YgdGhlbSBkb2VzCiNub3QgY29tZSBjbG9zZS4KX01BWF9T"
    "RU5EX0JBQ0tMT0cgPSA0ICogMTAyNCAqIDEwMjQKI2hhbmRsZXMgaW5kaXZpZHVhbCBjb25uZWN0"
    "aW9ucwpjbGFzcyBDb25uZWN0aW9uSGFuZGxlcihzb2NrZXRzZXJ2ZXIuQmFzZVJlcXVlc3RIYW5k"
    "bGVyKToKICAgICNkZWZhdWx0IHByb3BlcnRpZXM6CiAgICAjIC0gcmVxdWVzdDogc29ja2V0IHRv"
    "IGRlc3RpbmF0aW9uCiAgICAjIC0gY2xpZW50X2FkZHJlc3MKICAgICMgLSBzZXJ2ZXI6IENvcmVT"
    "ZXJ2ZXIKICAgIF9TVE9QV1JJVEVSID0gb2JqZWN0KCkKICAgIGRlZiBzZXR1cChzZWxmKToKICAg"
    "ICAgICBzZWxmLl9zUXVldWUgPSBTaW1wbGVRdWV1ZSgpCiAgICAgICAgI0J5dGVzIHF1ZXVlZCBi"
    "dXQgbm90IHlldCBoYW5kZWQgdG8gc2VuZGFsbCgpLCBhbmQgdGhlIGZsYWcgdGhhdCBzYXlzCiAg"
    "ICAgICAgI3RoaXMgY29ubmVjdGlvbiBoYXMgYWxyZWFkeSBiZWVuIGdpdmVuIHVwIG9uIGZvciBl"
    "eGNlZWRpbmcgdGhlIGNhcC4KICAgICAgICBzZWxmLl9xQnl0ZXMgPSAwCiAgICAgICAgc2VsZi5f"
    "cUxvY2sgPSB0aHJlYWRpbmcuTG9jaygpCiAgICAgICAgc2VsZi5fb3ZlcmZsb3dlZCA9IEZhbHNl"
    "CiAgICAgICAgc2VsZi51c2VyID0gTm9uZQogICAgICAgIHNlbGYuZ3VpZCA9IE5vbmUKICAgICAg"
    "ICBzZWxmLmRhdGEgPSBiJycKICAgICAgICBzZWxmLlNLID0gYnl0ZWFycmF5KHN0cnVjdC5wYWNr"
    "KCc8SUknLCAweEE2QUUxRjlCLCAweDQzOERGRjQwKSkKICAgICAgICAjU2VyaWFsaXNlcyB0aGUg"
    "cmF3IHNvY2tldCB3cml0ZXMuIFRocmVlIHRocmVhZHMgY2FuIHdhbnQgdG8gd3JpdGUgdG8KICAg"
    "ICAgICAjb25lIGNsaWVudDogdGhpcyBjb25uZWN0aW9uJ3Mgb3duIHJlYWQgbG9vcCAoZHVyaW5n"
    "IHRoZSBoYW5kc2hha2UpLAogICAgICAgICNpdHMgd3JpdGVyIHRocmVhZCwgYW5kIHRoZSBHVUkg"
    "dGhyZWFkIHZpYSBraWNrUGxheWVyKCkuIFdpdGhvdXQgdGhlCiAgICAgICAgI2xvY2sgdHdvIHNl"
    "bmRhbGwoKSBjYWxscyBjYW4gaW50ZXJsZWF2ZSBhbmQgc3BsaXQgYSBwYWNrZXQgZG93biB0aGUK"
    "ICAgICAgICAjbWlkZGxlLCB3aGljaCB0aGUgY2xpZW50IHNlZXMgYXMgcHJvdG9jb2wgZ2FyYmFn"
    "ZS4KICAgICAgICBzZWxmLl9zZW5kTG9jayA9IHRocmVhZGluZy5Mb2NrKCkKICAgICAgICBzZWxm"
    "Ll93cml0ZXIgPSBOb25lCiAgICAgICAgc2VsZi5fd3JpdGVyRGVhZCA9IHRocmVhZGluZy5FdmVu"
    "dCgpCiAgICAgICAgI1NldCB3aGVuIHRoaXMgY29ubmVjdGlvbiBoYXMgYmVlbiBnaXZlbiB1cCBv"
    "biBmcm9tICpvdXRzaWRlKiBpdHMgb3duCiAgICAgICAgI2hhbmRsZXIgdGhyZWFkIC0gYW4gYWRt"
    "aW4ga2ljaywgb3IgdGhlIHNlbmQtYmFja2xvZyBjYXAuIFNodXR0aW5nIHRoZQogICAgICAgICNz"
    "b2NrZXQgZG93biBpcyBzdXBwb3NlZCB0byB3YWtlIHRoYXQgdGhyZWFkIG9uIGl0cyBvd24sIGFu"
    "ZCBub3JtYWxseQogICAgICAgICNkb2VzOyB0aGlzIG1ha2VzIGl0IGNlcnRhaW4gcmF0aGVyIHRo"
    "YW4gZGVwZW5kZW50IG9uIHRoZSBzb2NrZXQKICAgICAgICAjcmVwb3J0aW5nIHRoZSBzaHV0ZG93"
    "biBwcm9tcHRseS4gQSBraWNrIHRoYXQgaXMgbm90IG5vdGljZWQgbGVhdmVzIHRoZQogICAgICAg"
    "ICNhY2NvdW50IGNsYWltZWQsIGFuZCB0aGUgcGxheWVyIGNhbm5vdCBnZXQgYmFjayBpbiB1bnRp"
    "bCB0aGUgaWRsZQogICAgICAgICN0aW1lb3V0IGV4cGlyZXMgLSB0aGUgZXhhY3QgZmFpbHVyZSBh"
    "IGtpY2sgaXMgbWVhbnQgdG8gcmVzb2x2ZS4KICAgICAgICBzZWxmLl9kcm9wcGVkID0gdGhyZWFk"
    "aW5nLkV2ZW50KCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3RvbmljKCkKICAg"
    "ICAgICBzZWxmLnNlcnZlci5yZWdpc3RlckNvbm5lY3Rpb24oc2VsZikKICAgICAgICB0cnk6CiAg"
    "ICAgICAgICAgICNOYWdsZSBiYXRjaGVzIHNtYWxsIHdyaXRlcyBieSBob2xkaW5nIHRoZW0gZm9y"
    "IHVwIHRvIH40MG1zIHdhaXRpbmcKICAgICAgICAgICAgI2ZvciBtb3JlIGRhdGEuIEV2ZXJ5IG1l"
    "c3NhZ2UgdGhpcyBzZXJ2ZXIgc2VuZHMgaXMgc21hbGwgYW5kCiAgICAgICAgICAgICNsYXRlbmN5"
    "LXNlbnNpdGl2ZSAtIGNoYXQsIHBvc2l0aW9uIHVwZGF0ZXMgYW5kIGFib3ZlIGFsbCB0aGUKICAg"
    "ICAgICAgICAgIy9nYW1lY29tbWFuZHRvdXNlciByZWxheSB0aGF0IGNhcnJpZXMgdGhlIGFjdHVh"
    "bCBpbi1nYW1lIGNvLW9wCiAgICAgICAgICAgICN0cmFmZmljIGJldHdlZW4gdHdvIHBsYXllcnMg"
    "LSBzbyB0aGUgZGVsYXkgaXMgcHVyZSBhZGRlZCBsYWcuCiAgICAgICAgICAgIHNlbGYucmVxdWVz"
    "dC5zZXRzb2Nrb3B0KHNvY2tldC5JUFBST1RPX1RDUCwgc29ja2V0LlRDUF9OT0RFTEFZLCAxKQog"
    "ICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAgICAgICAgICBwYXNzICNub3QgZmF0YWwsIGp1c3Qg"
    "c2xvd2VyCiAgICAgICAgdHJ5OgogICAgICAgICAgICAjQXNrIHRoZSBPUyB0byBwcm9iZSBhbiBp"
    "ZGxlIGNvbm5lY3Rpb24uIFdoZW4gYSBwbGF5ZXIncyBnYW1lCiAgICAgICAgICAgICNjcmFzaGVz"
    "IG91dHJpZ2h0IHRoZSBzb2NrZXQgaXMgdXN1YWxseSByZXNldCBhbmQgd2UgZmluZCBvdXQgYXQK"
    "ICAgICAgICAgICAgI29uY2UsIGJ1dCBhIG1hY2hpbmUgdGhhdCBmcmVlemVzLCBzbGVlcHMgb3Ig"
    "bG9zZXMgaXRzIGxpbmsgc2VuZHMKICAgICAgICAgICAgI25vdGhpbmcgYXQgYWxsOiB3aXRob3V0"
    "IHByb2JlcyB0aGF0IGNvbm5lY3Rpb24gc2l0cyB0aGVyZSBob2xkaW5nCiAgICAgICAgICAgICN0"
    "aGUgYWNjb3VudCAoIkFjY291bnQgYWxyZWFkeSBsb2dnZWQgaW4iKSBhbmQgaXRzIHJvb20gdW50"
    "aWwgdGhlCiAgICAgICAgICAgICNpZGxlIHRpbWVvdXQgZXhwaXJlcyBtaW51dGVzIGxhdGVyLiBQ"
    "cm9iZSBhZnRlciAzMHMgaWRsZSwgdGhlbgogICAgICAgICAgICAjZXZlcnkgNXMuCiAgICAgICAg"
    "ICAgIHNlbGYucmVxdWVzdC5zZXRzb2Nrb3B0KHNvY2tldC5TT0xfU09DS0VULCBzb2NrZXQuU09f"
    "S0VFUEFMSVZFLCAxKQogICAgICAgICAgICBpZiBoYXNhdHRyKHNlbGYucmVxdWVzdCwgJ2lvY3Rs"
    "JykgYW5kIGhhc2F0dHIoc29ja2V0LCAnU0lPX0tFRVBBTElWRV9WQUxTJyk6CiAgICAgICAgICAg"
    "ICAgICBzZWxmLnJlcXVlc3QuaW9jdGwoc29ja2V0LlNJT19LRUVQQUxJVkVfVkFMUywgKDEsIDMw"
    "MDAwLCA1MDAwKSkgI1dpbmRvd3MKICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgIGZv"
    "ciAob3B0LCB2YWwpIGluICgoJ1RDUF9LRUVQSURMRScsIDMwKSwgKCdUQ1BfS0VFUElOVFZMJywg"
    "NSksCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgKCdUQ1BfS0VFUENOVCcsIDQp"
    "KToKICAgICAgICAgICAgICAgICAgICBpZiBoYXNhdHRyKHNvY2tldCwgb3B0KToKICAgICAgICAg"
    "ICAgICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHNvY2tvcHQoc29ja2V0LklQUFJPVE9fVENQ"
    "LCBnZXRhdHRyKHNvY2tldCwgb3B0KSwgdmFsKQogICAgICAgIGV4Y2VwdCBPU0Vycm9yOgogICAg"
    "ICAgICAgICBwYXNzICNrZWVwYWxpdmUgaXMgYW4gb3B0aW1pc2F0aW9uLCBub3QgYSByZXF1aXJl"
    "bWVudAogICAgZGVmIHNlbmRSYXcoc2VsZiwgbXNnKToKICAgICAgICAjVGhlIHNpbmdsZSBmdW5u"
    "ZWwgZm9yIGV2ZXJ5IGJ5dGUgbGVhdmluZyB0aGUgc2VydmVyIG9uIHRoaXMgc29ja2V0LgogICAg"
    "ICAgIHdpdGggc2VsZi5fc2VuZExvY2s6CiAgICAgICAgICAgIHNlbGYucmVxdWVzdC5zZW5kYWxs"
    "KG1zZykKICAgIGRlZiBzZW5kKHNlbGYsIG1zZyk6CiAgICAgICAgI05vcm1hbCBwYXRoIG9uY2Ug"
    "dGhlIGNvbm5lY3Rpb24gaXMgbGl2ZTogaGFuZCBvZmYgdG8gdGhlIHdyaXRlciB0aHJlYWQKICAg"
    "ICAgICAjc28gdGhlIGNhbGxlciAoYSBjb21tYW5kIGhhbmRsZXIsIG9yIHRoZSBkaXN0cmlidXRv"
    "cidzIGZhbi1vdXQpIG5ldmVyCiAgICAgICAgI2Jsb2NrcyBvbiBhIHNsb3cgb3Igc3RhbGxlZCBj"
    "bGllbnQuCiAgICAgICAgaWYgbm90IG1zZzoKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgd2l0"
    "aCBzZWxmLl9xTG9jazoKICAgICAgICAgICAgaWYgc2VsZi5fb3ZlcmZsb3dlZDoKICAgICAgICAg"
    "ICAgICAgIHJldHVybiAjYWxyZWFkeSBiZWluZyB0b3JuIGRvd24sIHN0b3AgYWNjb3VudGluZyBm"
    "b3IgaXQKICAgICAgICAgICAgc2VsZi5fcUJ5dGVzICs9IGxlbihtc2cpCiAgICAgICAgICAgIG92"
    "ZXIgPSBzZWxmLl9xQnl0ZXMgPiBfTUFYX1NFTkRfQkFDS0xPRwogICAgICAgICAgICBzZWxmLl9v"
    "dmVyZmxvd2VkID0gb3ZlcgogICAgICAgIGlmIG92ZXI6CiAgICAgICAgICAgICNTZWUgX01BWF9T"
    "RU5EX0JBQ0tMT0cuIFNodXR0aW5nIHRoZSBzb2NrZXQgZG93biBpcyB3aGF0IHRlbGxzIHRoZQog"
    "ICAgICAgICAgICAjcmVhZCBsb29wIHRvIHJ1biB0aGlzIGNvbm5lY3Rpb24ncyBub3JtYWwgY2xl"
    "YW51cCBwYXRoLgogICAgICAgICAgICB3aG8gPSBzZWxmLnVzZXIubmFtZSBpZiBzZWxmLnVzZXIg"
    "ZWxzZSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSB7"
    "d2hvfTogb3ZlciB7X01BWF9TRU5EX0JBQ0tMT0d9IGJ5dGVzIHF1ZXVlZCB1bnJlYWQsIGRyb3Bw"
    "aW5nJykKICAgICAgICAgICAgc2VsZi5kcm9wKCkKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAg"
    "c2VsZi5fc1F1ZXVlLnB1dChtc2cpCiAgICBkZWYgZHJvcChzZWxmKToKICAgICAgICAjRW5kIHRo"
    "aXMgY29ubmVjdGlvbiBmcm9tIGFub3RoZXIgdGhyZWFkLiBGbGFnZ2luZyBpdCBmaXJzdCBtZWFu"
    "cyB0aGUKICAgICAgICAjcmVhZCBsb29wIGJhaWxzIG91dCBhdCBpdHMgbmV4dCBwYXNzIG5vIG1h"
    "dHRlciB3aGF0IHRoZSBzb2NrZXQgZG9lczsKICAgICAgICAjdGhlIHNodXRkb3duIGlzIHdoYXQg"
    "d2FrZXMgaXQgZnJvbSBzZWxlY3QoKSBzdHJhaWdodCBhd2F5LiBJdHMgb3duCiAgICAgICAgI2hh"
    "bmRsZXIgdGhyZWFkIHN0aWxsIHJ1bnMgdGhlIG5vcm1hbCBmaW5pc2goKS9jbGVhbnVwIHBhdGgs"
    "IHNvIHRoZQogICAgICAgICNhY2NvdW50IGlzIHJlbGVhc2VkIGFuZCB0aGUgdG93biByb3N0ZXIg"
    "dGlkaWVkIGV4YWN0bHkgYXMgb24gYW55IG90aGVyCiAgICAgICAgI2Rpc2Nvbm5lY3QuIE5ldmVy"
    "IGNsb3NlKCkgaGVyZSAtIHNlZSBjbG9zZUNvbm5lY3Rpb25zKCkuCiAgICAgICAgc2VsZi5fZHJv"
    "cHBlZC5zZXQoKQogICAgICAgIHRyeToKICAgICAgICAgICAgc2VsZi5yZXF1ZXN0LnNodXRkb3du"
    "KHNvY2tldC5TSFVUX1JEV1IpCiAgICAgICAgZXhjZXB0IE9TRXJyb3I6CiAgICAgICAgICAgIHBh"
    "c3MgI2FscmVhZHkgZ29uZSwgb3IgbmV2ZXIgZnVsbHkgY29ubmVjdGVkCiAgICBkZWYgZmx1c2hQ"
    "ZW5kaW5nKHNlbGYsIHRpbWVvdXQpOgogICAgICAgICNCZXN0LWVmZm9ydCwgc3RyaWN0bHkgYm91"
    "bmRlZCB3YWl0IGZvciB0aGUgb3V0Ym91bmQgcXVldWUgdG8gZHJhaW4uCiAgICAgICAgI0ZvciBj"
    "YWxsZXJzIHRoYXQgd2FudCBhIGxhc3QgbWVzc2FnZSB0byBoYXZlIGxlZnQgYmVmb3JlIHRoZSBz"
    "b2NrZXQKICAgICAgICAjZ29lcyBkb3duICh0aGUgYWRtaW4ga2ljaykgd2l0aG91dCBpbmhlcml0"
    "aW5nIGEgc3RhbGxlZCBwZWVyJ3Mgc3RhbGwuCiAgICAgICAgZGVhZGxpbmUgPSB0aW1lLm1vbm90"
    "b25pYygpICsgdGltZW91dAogICAgICAgIHdoaWxlIG5vdCBzZWxmLl9zUXVldWUuZW1wdHkoKSBh"
    "bmQgdGltZS5tb25vdG9uaWMoKSA8IGRlYWRsaW5lOgogICAgICAgICAgICB0aW1lLnNsZWVwKDAu"
    "MDIpCiAgICBkZWYgX3dyaXRlckxvb3Aoc2VsZik6CiAgICAgICAgI0Jsb2NrcyBvbiB0aGUgcXVl"
    "dWUgaW5zdGVhZCBvZiBiZWluZyBwb2xsZWQuIFByZXZpb3VzbHkgdGhlIHJlYWQgbG9vcAogICAg"
    "ICAgICNkcmFpbmVkIHRoaXMgcXVldWUgaXRzZWxmIGJldHdlZW4gcmVjdigpIHRpbWVvdXRzLCBz"
    "byBhbnl0aGluZyBxdWV1ZWQKICAgICAgICAjanVzdCBhZnRlciB0aGUgdGhyZWFkIHdlbnQgYmFj"
    "ayBpbnRvIHJlY3YoKSB3YWl0ZWQgb3V0IHRoZSBmdWxsCiAgICAgICAgI3RpbWVvdXQgLSB1cCB0"
    "byAxMDBtcyBvZiBsYXRlbmN5IGFkZGVkIHRvIGV2ZXJ5IHJlbGF5ZWQgZ2FtZSBjb21tYW5kLAog"
    "ICAgICAgICNvbiB0b3Agb2YgZXZlcnkgaWRsZSBjb25uZWN0aW9uIHdha2luZyAxMCB0aW1lcyBh"
    "IHNlY29uZCB0byBjaGVjay4KICAgICAgICB0cnk6CiAgICAgICAgICAgIHdoaWxlIFRydWU6CiAg"
    "ICAgICAgICAgICAgICBtc2cgPSBzZWxmLl9zUXVldWUuZ2V0KCkKICAgICAgICAgICAgICAgIGlm"
    "IG1zZyBpcyBzZWxmLl9TVE9QV1JJVEVSOgogICAgICAgICAgICAgICAgICAgIGJyZWFrCiAgICAg"
    "ICAgICAgICAgICAjQ29hbGVzY2Ugd2hhdGV2ZXIgZWxzZSBwaWxlZCB1cCBiZWhpbmQgaXQgaW50"
    "byBhIHNpbmdsZSB3cml0ZS4KICAgICAgICAgICAgICAgICNQb3NpdGlvbiBicm9hZGNhc3RzIGFu"
    "ZCBnYW1lIGNvbW1hbmRzIG9mdGVuIGFycml2ZSBpbiBidXJzdHMuCiAgICAgICAgICAgICAgICBj"
    "aHVua3MgPSBbbXNnXQogICAgICAgICAgICAgICAgc3RvcHBpbmcgPSBGYWxzZQogICAgICAgICAg"
    "ICAgICAgd2hpbGUgVHJ1ZToKICAgICAgICAgICAgICAgICAgICB0cnk6CiAgICAgICAgICAgICAg"
    "ICAgICAgICAgIG54dCA9IHNlbGYuX3NRdWV1ZS5nZXRfbm93YWl0KCkKICAgICAgICAgICAgICAg"
    "ICAgICBleGNlcHQgRXhjZXB0aW9uOgogICAgICAgICAgICAgICAgICAgICAgICBicmVhawogICAg"
    "ICAgICAgICAgICAgICAgIGlmIG54dCBpcyBzZWxmLl9TVE9QV1JJVEVSOgogICAgICAgICAgICAg"
    "ICAgICAgICAgICBzdG9wcGluZyA9IFRydWUKICAgICAgICAgICAgICAgICAgICAgICAgYnJlYWsK"
    "ICAgICAgICAgICAgICAgICAgICBjaHVua3MuYXBwZW5kKG54dCkKICAgICAgICAgICAgICAgIHBh"
    "eWxvYWQgPSBiJycuam9pbihjaHVua3MpCiAgICAgICAgICAgICAgICAjUmVsZWFzZWQgYmVmb3Jl"
    "IHRoZSB3cml0ZSwgbm90IGFmdGVyOiB0aGUgYmFja2xvZyBleGlzdHMgdG8KICAgICAgICAgICAg"
    "ICAgICNkZXNjcmliZSB3aGF0IGlzIHN0aWxsIHdhaXRpbmcgZm9yIHRoZSBzb2NrZXQsIGFuZCB0"
    "aGVzZSBieXRlcwogICAgICAgICAgICAgICAgI2FyZSBvbiB0aGVpciB3YXkgb3V0LiBDb3VudGlu"
    "ZyB0aGVtIGFzIHBlbmRpbmcgZm9yIHRoZSB3aG9sZQogICAgICAgICAgICAgICAgI2R1cmF0aW9u"
    "IG9mIGEgc2xvdyBzZW5kYWxsKCkgd291bGQgbWFrZSBhIG1lcmVseSBzbG93IGxpbmsgbG9vawog"
    "ICAgICAgICAgICAgICAgI2xpa2UgdGhlIHdlZGdlZCBjbGllbnQgdGhlIGNhcCBpcyB0aGVyZSB0"
    "byBjYXRjaC4KICAgICAgICAgICAgICAgIHdpdGggc2VsZi5fcUxvY2s6CiAgICAgICAgICAgICAg"
    "ICAgICAgc2VsZi5fcUJ5dGVzIC09IGxlbihwYXlsb2FkKQogICAgICAgICAgICAgICAgc2VsZi5z"
    "ZW5kUmF3KHBheWxvYWQpCiAgICAgICAgICAgICAgICBpZiBzdG9wcGluZzoKICAgICAgICAgICAg"
    "ICAgICAgICByZXR1cm4KICAgICAgICBleGNlcHQgKENvbm5lY3Rpb25SZXNldEVycm9yLCBDb25u"
    "ZWN0aW9uQWJvcnRlZEVycm9yLCBCcm9rZW5QaXBlRXJyb3IsIE9TRXJyb3IpOgogICAgICAgICAg"
    "ICBwYXNzICNwZWVyIGlzIGdvbmU7IHRoZSByZWFkIGxvb3Agbm90aWNlcyBhbmQgcnVucyB0aGUg"
    "Y2xlYW51cAogICAgICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgICAgIHByaW50KCdbTG9i"
    "YnldIFdyaXRlciBlcnJvcjpcbicgKyB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgICAgIGZp"
    "bmFsbHk6CiAgICAgICAgICAgIHNlbGYuX3dyaXRlckRlYWQuc2V0KCkKICAgIGRlZiBfc3RhcnRX"
    "cml0ZXIoc2VsZik6CiAgICAgICAgc2VsZi5fd3JpdGVyID0gdGhyZWFkaW5nLlRocmVhZCh0YXJn"
    "ZXQ9c2VsZi5fd3JpdGVyTG9vcCwgZGFlbW9uPVRydWUpCiAgICAgICAgc2VsZi5fd3JpdGVyLnN0"
    "YXJ0KCkKICAgIGRlZiBfc3RvcFdyaXRlcihzZWxmKToKICAgICAgICBpZiBzZWxmLl93cml0ZXIg"
    "aXMgTm9uZToKICAgICAgICAgICAgcmV0dXJuCiAgICAgICAgc2VsZi5fc1F1ZXVlLnB1dChzZWxm"
    "Ll9TVE9QV1JJVEVSKQogICAgICAgIHNlbGYuX3dyaXRlci5qb2luKHRpbWVvdXQ9Mi4wKQogICAg"
    "ICAgIHNlbGYuX3dyaXRlciA9IE5vbmUKICAgIGRlZiBfY2xhaW1TZXNzaW9uKHNlbGYpOgogICAg"
    "ICAgICNUYWtlIG93bmVyc2hpcCBvZiB0aGUgdXNlcm5hbWUgc2xvdCBiZWZvcmUgdGVsbGluZyB0"
    "aGUgY2xpZW50IGl0IGlzCiAgICAgICAgI2xvZ2dlZCBpbi4gUmV0dXJucyBGYWxzZSBpZiBhbm90"
    "aGVyIGNvbm5lY3Rpb24gZ290IHRoZXJlIGZpcnN0LgogICAgICAgIGlmIHNlbGYuc2VydmVyLnN0"
    "YXRlLmNsYWltVXNlcihzZWxmLnVzZXIubmFtZSwgc2VsZik6CiAgICAgICAgICAgIHJldHVybiBU"
    "cnVlCiAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5zZXJ2ZXIpICNyZWxlYXNlcyB0"
    "aGUgaWRudW0gd2UganVzdCBhbGxvY2F0ZWQKICAgICAgICBzZWxmLnVzZXIgPSBOb25lCiAgICAg"
    "ICAgcmV0dXJuIEZhbHNlCiAgICBkZWYgYXR0ZW1wdExvZ2luKHNlbGYsIHVzZXJuYW1lLCBwYXNz"
    "d29yZCk6CiAgICAgICAgaWYgbGVuKHVzZXJuYW1lKTwxOgogICAgICAgICAgICByZXR1cm4gNCAj"
    "Tm8gVXNlcm5hbWUsIGxpa2VseSBmcmVzaCBsb2dpbgogICAgICAgICAgICAjVE9ETyBjaGVjayBp"
    "ZiBzZXJpYWwgZXhpc3RzIGFuZCByZXR1cm4gdXNlcm5hbWUgcHJvcGVybHkKICAgICAgICBpZiBs"
    "ZW4ocGFzc3dvcmQpPDE6CiAgICAgICAgICAgIHJldHVybiAzICNQYXNzd29yZCB0b28gc2hvcnQK"
    "ICAgICAgICAjVGVzdCBpZiBwbGF5ZXIgYWxyZWFkeSBsb2dnZWQgaW4gKGZhc3QgcGF0aDsgdGhl"
    "IGF1dGhvcml0YXRpdmUsCiAgICAgICAgI3JhY2UtZnJlZSBjaGVjayBpcyB0aGUgY2xhaW1Vc2Vy"
    "KCkgYmVsb3cpCiAgICAgICAgaWYgc2VsZi5zZXJ2ZXIuZ2V0UGxheWVyKHVzZXJuYW1lKToKICAg"
    "ICAgICAgICAgcmV0dXJuIDIgI1RPRE8gUExBWUVSIExPR0dFRCBJTiBFUlJPUgogICAgICAgICNw"
    "bGF5ZXIgbm90IGN1cnJlbnRseSBsb2dnZWQgaW4sIGF0dGVtcHQgdG8gbG9naW4gdmlhIGRhdGEg"
    "aGFuZGxlcgogICAgICAgIHNlbGYudXNlciA9IEdESC5sb2dpblBsYXllcih1c2VybmFtZSwgc2Vs"
    "ZiwgcGFzc3dvcmQpCiAgICAgICAgaWYgc2VsZi51c2VyOgogICAgICAgICAgICByZXR1cm4gMCBp"
    "ZiBzZWxmLl9jbGFpbVNlc3Npb24oKSBlbHNlIDIKICAgICAgICByZXR1cm4gMSAjVE9ETyBHZXQg"
    "ZnJvbSBHREgubG9naW5QbGF5ZXIsIHBhc3MgdXNlciBvYmplY3QgYWxvbmc/CiAgICBkZWYgYXR0"
    "ZW1wdFJlZ2lzdGVyKHNlbGYsIHVzZXJuYW1lLCBwYXNzd29yZCwgZW1haWwsIGxvY2F0aW9uLCBh"
    "Z2UsIGdlbmRlciwgZGVzY3JpcHRpb24pOgogICAgICAgICNUZXN0IGlmIHBsYXllciBhbHJlYWR5"
    "IGxvZ2dlZCBpbgogICAgICAgIGlmIHNlbGYuc2VydmVyLmdldFBsYXllcih1c2VybmFtZSk6CiAg"
    "ICAgICAgICAgIHJldHVybiAxICNUT0RPIFBMQVlFUiBMT0dHRUQgSU4gRVJST1IKICAgICAgICBz"
    "ZWxmLnVzZXIgPSBHREgucmVnaXN0ZXJQbGF5ZXIodXNlcm5hbWUsIHNlbGYsIHBhc3N3b3JkLCBl"
    "bWFpbCwgbG9jYXRpb24sIGFnZSwgZ2VuZGVyLCBkZXNjcmlwdGlvbikKICAgICAgICBpZiBzZWxm"
    "LnVzZXI6CiAgICAgICAgICAgIHJldHVybiAwIGlmIHNlbGYuX2NsYWltU2Vzc2lvbigpIGVsc2Ug"
    "MQogICAgICAgIHJldHVybiAyICNUT0RPIGdldCBlcnJvciBmcm9tIEdESAogICAgZGVmIGhhbmRs"
    "ZShzZWxmKToKICAgICAgICB0cnk6ICNJbnRlcmNlcHQgYW5kIHByaW50IGVycm9ycyBmb3IgZGVi"
    "dWdnaW5nCiAgICAgICAgICAgIHNlbGYuX2hhbmRsZSgpCiAgICAgICAgICAgICNUT0RPIGxvb3Ag"
    "bG9iYnkgaGFuZGxlIGJldHRlciB0byBoYW5kbGUgZXhjZXB0aW9ucyBncmFjZWZ1bGx5CiAgICAg"
    "ICAgICAgIHNlbGYuX2xvYmJ5SGFuZGxlKCkKICAgICAgICBleGNlcHQgUHJvdG9jb2xFcnJvciBh"
    "cyBlOgogICAgICAgICAgICAjbWFsZm9ybWVkL292ZXJzaXplZCBpbnB1dCAtIHRoZSBjbGllbnQn"
    "cyBmYXVsdCwgbm90IG91cnMuIERyb3AgdGhlCiAgICAgICAgICAgICNjb25uZWN0aW9uIHdpdGgg"
    "b25lIGxpbmUgaW5zdGVhZCBvZiBhIHRyYWNlYmFjay4KICAgICAgICAgICAgd2hvID0gc2VsZi51"
    "c2VyLm5hbWUgaWYgc2VsZi51c2VyIGVsc2Ugc2VsZi5jbGllbnRfYWRkcmVzc1swXQogICAgICAg"
    "ICAgICBwcmludChmJ1tMb2JieV0gUHJvdG9jb2wgZXJyb3IgZnJvbSB7d2hvfToge2V9JykKICAg"
    "ICAgICBleGNlcHQgKHpsaWIuZXJyb3IsIHN0cnVjdC5lcnJvciwgVW5pY29kZURlY29kZUVycm9y"
    "KSBhcyBlOgogICAgICAgICAgICAjdHJ1bmNhdGVkL2dhcmJhZ2UgcGFja2V0OiBwYXJzZURzdHIg"
    "YW5kIHN0cnVjdC51bnBhY2sgYm90aCByYWlzZSBvbgogICAgICAgICAgICAjc2hvcnQgcmVhZHMs"
    "IGFuZCAuZGVjb2RlKCkgb24gbm9uLWFzY2lpIGp1bmsuIFNhbWUgY2F0ZWdvcnkuCiAgICAgICAg"
    "ICAgIHByaW50KGYnW0xvYmJ5XSBNYWxmb3JtZWQgcGFja2V0IGZyb20ge3NlbGYuY2xpZW50X2Fk"
    "ZHJlc3NbMF19OiAnCiAgICAgICAgICAgICAgICAgIGYne3R5cGUoZSkuX19uYW1lX199OiB7ZX0n"
    "KQogICAgICAgIGV4Y2VwdCAoQ29ubmVjdGlvblJlc2V0RXJyb3IsIENvbm5lY3Rpb25BYm9ydGVk"
    "RXJyb3IsIE9TRXJyb3IpIGFzIGU6CiAgICAgICAgICAgICMgZXhwZWN0ZWQgZm9ybSBvZiBkaXNj"
    "b25uZWN0aW9uIChpbmNsdWRpbmcgYSBmb3JjZWQgYWRtaW4ga2ljayksCiAgICAgICAgICAgICMg"
    "YnV0IGxlYXZlIGEgb25lLWxpbmUgYnJlYWRjcnVtYiByYXRoZXIgdGhhbiBzdGF5aW5nIGZ1bGx5"
    "IHNpbGVudAogICAgICAgICAgICBpZiBzZWxmLnVzZXI6CiAgICAgICAgICAgICAgICBwcmludChm"
    "J1tMb2JieV0gQ29ubmVjdGlvbiBjbG9zZWQgZm9yIHtzZWxmLnVzZXIubmFtZX06IHtlfScpCiAg"
    "ICAgICAgZXhjZXB0IEV4Y2VwdGlvbjojIGFzIGU6CiAgICAgICAgICAgIHByaW50KHRyYWNlYmFj"
    "ay5mb3JtYXRfZXhjKCkpCiAgICAgICAgICAgIGlmIHNlbGYudXNlcjoKICAgICAgICAgICAgICAg"
    "IHByaW50KGYnVXNlcjoge3NlbGYudXNlci5uYW1lfScpCiAgICAgICAgICAgICNyYWlzZSBlCiAg"
    "ICBkZWYgX2xvYmJ5SGFuZGxlKHNlbGYpOgogICAgICAgICNhY3RpdmVVc2Vyc1suLi5dID0gc2Vs"
    "ZiB1c2VkIHRvIGhhcHBlbiBoZXJlOyBpdCBub3cgaGFwcGVucyB1bmRlciBhCiAgICAgICAgI2xv"
    "Y2sgaW5zaWRlIGF0dGVtcHRMb2dpbi9hdHRlbXB0UmVnaXN0ZXIsIGJlZm9yZSB0aGUgd2VsY29t"
    "ZSBwYWNrZXQKICAgICAgICAjZ29lcyBvdXQsIHNvIHR3byBsb2dpbnMgZm9yIG9uZSBhY2NvdW50"
    "IGNhbid0IGJvdGggc3VjY2VlZC4KICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFt"
    "ZX0gQ29ubmVjdGVkJykKICAgICAgICAjRnJvbSBoZXJlIG9uIG5vdGhpbmcgd3JpdGVzIHRvIHRo"
    "ZSBzb2NrZXQgaW5saW5lOiB0aGUgd3JpdGVyIHRocmVhZAogICAgICAgICNvd25zIHRoZSBvdXRi"
    "b3VuZCBkaXJlY3Rpb24gYW5kIHRoaXMgbG9vcCBvbmx5IHJlYWRzLgogICAgICAgIHNlbGYuX3N0"
    "YXJ0V3JpdGVyKCkKICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUubW9ub3RvbmljKCkKICAg"
    "ICAgICAjVGhlIHNvY2tldCBzdGF5cyBpbiBibG9ja2luZyBtb2RlIGZvciBpdHMgd2hvbGUgbGlm"
    "ZSBmcm9tIGhlcmUgb24sIGFuZAogICAgICAgICNyZWFkaW5lc3MgaXMgd2FpdGVkIGZvciB3aXRo"
    "IHNlbGVjdCgpIGluc3RlYWQgb2YgYSBzb2NrZXQgdGltZW91dC4KICAgICAgICAjVGhpcyBpcyBu"
    "b3QgYSBzdHlsZSBwcmVmZXJlbmNlIC0gYSBzb2NrZXQgdGltZW91dCBpcyBhIHByb3BlcnR5IG9m"
    "IHRoZQogICAgICAgICMqc29ja2V0Kiwgbm90IG9mIHRoZSBjYWxsLCBzbyB0aGUgc2V0dGltZW91"
    "dChfUkVBRF9USU1FT1VUKSB0aGlzIGxvb3AKICAgICAgICAjdXNlZCB0byBkbyBvbiBldmVyeSBw"
    "YXNzIGFsc28gYXJtZWQgYSAxcyB0aW1lb3V0IG9uIHRoZSB3cml0ZXIKICAgICAgICAjdGhyZWFk"
    "J3MgY29uY3VycmVudCBzZW5kYWxsKCkuIEEgY2xpZW50IHdob3NlIHJlY2VpdmUgd2luZG93IHdh"
    "cyBmdWxsCiAgICAgICAgI2ZvciBhIHNlY29uZCAoZXhhY3RseSB0aGUgY2FzZSBkdXJpbmcgYSBi"
    "dXN5IGNvLW9wIHNlc3Npb24pIG1hZGUgdGhhdAogICAgICAgICNzZW5kYWxsKCkgcmFpc2UgVGlt"
    "ZW91dEVycm9yICphZnRlciBoYXZpbmcgYWxyZWFkeSB3cml0dGVuIHBhcnQgb2YgdGhlCiAgICAg"
    "ICAgI3BhY2tldCo6IHRoZSB3cml0ZXIgdGhyZWFkIGRpZWQsIGFuZCB3aGF0ZXZlciB0aGUgY2xp"
    "ZW50IGhhZCByZWNlaXZlZAogICAgICAgICN3YXMgaGFsZiBhIG1lc3NhZ2UsIHNvIGl0cyBjb21t"
    "YW5kIHN0cmVhbSB3YXMgZGVzeW5jaHJvbmlzZWQgZnJvbQogICAgICAgICN0aGF0IHBvaW50IG9u"
    "LiBzZWxlY3QoKSBsZWF2ZXMgdGhlIHNvY2tldCBibG9ja2luZywgc28gd3JpdGVzIGFyZQogICAg"
    "ICAgICNuZXZlciBpbnRlcnJ1cHRlZCwgd2hpbGUgcmVhZHMgc3RpbGwgd2FrZSB1cCByZWd1bGFy"
    "bHkgZW5vdWdoIHRvCiAgICAgICAgI25vdGljZSBzaHV0ZG93biBhbmQgdGhlIGlkbGUgZGVhZGxp"
    "bmUuCiAgICAgICAgc2VsZi5yZXF1ZXN0LnNldHRpbWVvdXQoTm9uZSkKICAgICAgICB3aGlsZSBU"
    "cnVlOgogICAgICAgICAgICBpZiBzZWxmLl9kcm9wcGVkLmlzX3NldCgpOgogICAgICAgICAgICAg"
    "ICAgYnJlYWsgI2tpY2tlZCwgb3IgZHJvcHBlZCBmb3IgYW4gdW5yZWFkIHNlbmQgYmFja2xvZwog"
    "ICAgICAgICAgICBpZiBzZWxmLl93cml0ZXJEZWFkLmlzX3NldCgpOgogICAgICAgICAgICAgICAg"
    "YnJlYWsgI3BlZXIgd2VudCBhd2F5IHdoaWxlIHdlIHdlcmUgc2VuZGluZwogICAgICAgICAgICBp"
    "ZiBzZWxmLnNlcnZlci5faXNfY2xvc2luZzoKICAgICAgICAgICAgICAgIGJyZWFrICNzZXJ2ZXIg"
    "aXMgc3RvcHBpbmcgLSBjaGVja2VkIGhlcmUsIG5vdCBvbmx5IG9uIGFuIGlkbGUKICAgICAgICAg"
    "ICAgICAgICAgICAgICN0aW1lb3V0LCBzbyBhIGNsaWVudCB0aGF0IGtlZXBzIHRhbGtpbmcgY2Fu"
    "bm90IGtlZXAgaXRzCiAgICAgICAgICAgICAgICAgICAgICAjaGFuZGxlciB0aHJlYWQgKGFuZCBp"
    "dHMgbG9nIHNwYW0pIGFsaXZlIHBhc3Qgc2h1dGRvd24KICAgICAgICAgICAgdHJ5OgogICAgICAg"
    "ICAgICAgICAgcmVhZHksIF8sIF8gPSBzZWxlY3Quc2VsZWN0KFtzZWxmLnJlcXVlc3RdLCBbXSwg"
    "W10sIF9SRUFEX1RJTUVPVVQpCiAgICAgICAgICAgIGV4Y2VwdCAoT1NFcnJvciwgVmFsdWVFcnJv"
    "cik6CiAgICAgICAgICAgICAgICBicmVhayAjc29ja2V0IGNsb3NlZCB1bmRlciB1cyAoYWRtaW4g"
    "a2ljayAvIHNodXRkb3duKQogICAgICAgICAgICBpZiBub3QgcmVhZHk6CiAgICAgICAgICAgICAg"
    "ICBpZiBzZWxmLnNlcnZlci5faXNfY2xvc2luZzoKICAgICAgICAgICAgICAgICAgICBicmVhayAj"
    "U2VydmVyIFNodXR0aW5nIGRvd24KICAgICAgICAgICAgICAgIGlmIF9JRExFX1RJTUVPVVQgYW5k"
    "ICh0aW1lLm1vbm90b25pYygpIC0gc2VsZi5fbGFzdFJlY3YpID4gX0lETEVfVElNRU9VVDoKICAg"
    "ICAgICAgICAgICAgICAgICAjSGFsZi1vcGVuIGNvbm5lY3Rpb246IHRoZSBwZWVyIGlzIHVucmVh"
    "Y2hhYmxlIGJ1dCBuZXZlcgogICAgICAgICAgICAgICAgICAgICNzZW50IGEgRklOL1JTVCwgc28g"
    "cmVjdigpIGJsb2NrcyBmb3JldmVyIGFuZCB0aGUgYWNjb3VudAogICAgICAgICAgICAgICAgICAg"
    "ICNzdGF5cyBjbGFpbWVkLiBSZWFwIGl0IHNvIHRoZSBwbGF5ZXIgY2FuIGxvZyBiYWNrIGluLgog"
    "ICAgICAgICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSB7c2VsZi51c2VyLm5hbWV9IGlkbGUg"
    "Zm9yIHtfSURMRV9USU1FT1VUfXMsIGRyb3BwaW5nJykKICAgICAgICAgICAgICAgICAgICBicmVh"
    "awogICAgICAgICAgICAgICAgY29udGludWUKICAgICAgICAgICAgcm1zZyA9IHNlbGYucmVxdWVz"
    "dC5yZWN2KFJFQ1ZfQlVGX0xFTikgI1RPRE8gbG9nIG5ldHdvcmsgYnl0ZXJhdGUKICAgICAgICAg"
    "ICAgaWYgbm90IHJtc2c6CiAgICAgICAgICAgICAgICBicmVhayAjRGlzY29ubmVjdGVkCiAgICAg"
    "ICAgICAgIHNlbGYuZGF0YSs9cm1zZwogICAgICAgICAgICBzZWxmLl9sYXN0UmVjdiA9IHRpbWUu"
    "bW9ub3RvbmljKCkKICAgICAgICAgICAgd2hpbGUgc2VsZi5kYXRhOgogICAgICAgICAgICAgICAg"
    "dHJ5OgogICAgICAgICAgICAgICAgICAgIGNtZF9sID0gc2VsZi5kYXRhLmluZGV4KDApCiAgICAg"
    "ICAgICAgICAgICBleGNlcHQgVmFsdWVFcnJvcjoKICAgICAgICAgICAgICAgICAgICAjcHJpbnQo"
    "J2NtZCBkZWNvZGUgZXJyb3I6XG4nLCB0cmFjZWJhY2suZm9ybWF0X2V4YygpKQogICAgICAgICAg"
    "ICAgICAgICAgIGJyZWFrOyNNYXkgcmVxdWlyZSBtb3JlIGRhdGEKICAgICAgICAgICAgICAgIGNt"
    "ZCA9IHdpcmVfZGVjb2RlKHNlbGYuZGF0YVswOmNtZF9sXSkKICAgICAgICAgICAgICAgIHNlbGYu"
    "ZGF0YSA9IHNlbGYuZGF0YVtjbWRfbCsxOl0KICAgICAgICAgICAgICAgIHJlc3BvbnNlID0gc2Vs"
    "Zi5zZXJ2ZXIuY29tcGFycy5wYXJzZShjbWQsIHNlbGYpCiAgICAgICAgICAgICAgICBpZiByZXNw"
    "b25zZToKICAgICAgICAgICAgICAgICAgICAjUXVldWVkIHJhdGhlciB0aGFuIHNlbnQgaW5saW5l"
    "LCBzbyB0aGlzIGNvbm5lY3Rpb24gaGFzIGEKICAgICAgICAgICAgICAgICAgICAjc2luZ2xlIG9y"
    "ZGVyZWQgb3V0Ym91bmQgc3RyZWFtLiBTZW5kaW5nIGhlcmUgZGlyZWN0bHkKICAgICAgICAgICAg"
    "ICAgICAgICAjd291bGQgcmFjZSB0aGUgd3JpdGVyIHRocmVhZCBhbmQgY291bGQgbGFuZCBpbiB0"
    "aGUgbWlkZGxlCiAgICAgICAgICAgICAgICAgICAgI29mIGEgYnJvYWRjYXN0IGl0IGlzIGFscmVh"
    "ZHkgd3JpdGluZy4KICAgICAgICAgICAgICAgICAgICBzZWxmLnNlbmQocmVzcG9uc2UpCiAgICAg"
    "ICAgICAgICAgICAjTG9vc2UgYmxvYnMgc2hvdWxkIG5vdCBoYXBwZW4gYW55bW9yZSBob3BlZnVs"
    "bHkKICAgICAgICAgICAgICAgICNUT0RPIGZpeCB1bmNvbXByZXNzZWQgZGF0YSBibG9icz8KICAg"
    "ICAgICAgICAgICAgICNUT0RPIHNraXAgMSBieXRlIG9ubHkgd2hlbiBkZWNvZGUgZXJyb3I/CiAg"
    "ICAgICAgICAgICAgICBpZiAobGVuKHNlbGYuZGF0YSk+MiBhbmQKICAgICAgICAgICAgICAgICAg"
    "ICAgICAgc2VsZi5kYXRhWzBdPT0weDc4IGFuZAogICAgICAgICAgICAgICAgICAgICAgICBzZWxm"
    "LmRhdGFbMV09PTB4OWMpOgogICAgICAgICAgICAgICAgICAgICNMb29zZSB1bmhhbmRsZWQgYmxv"
    "YiBhZnRlciBjb21tYW5kCiAgICAgICAgICAgICAgICAgICAgYmxvYiwgc2VsZi5kYXRhID0gcF9n"
    "ZXRCbG9iKHNlbGYuZGF0YSwgc2VsZi5yZXF1ZXN0KQogICAgICAgICAgICAgICAgICAgICNUaGUg"
    "b3RoZXIgYmxpbmQgc3BvdDogYW55dGhpbmcgdGhlIGNsaWVudCBzZW5kcyBhcyBhCiAgICAgICAg"
    "ICAgICAgICAgICAgI2NvbXByZXNzZWQgYmxvYiByYXRoZXIgdGhhbiBhIHRleHQgY29tbWFuZCB3"
    "YXMgcmVhZCBhbmQKICAgICAgICAgICAgICAgICAgICAjdGhyb3duIGF3YXkgd2l0aG91dCBhIHRy"
    "YWNlLgogICAgICAgICAgICAgICAgICAgIGlmIF9ERUJVR19MT0dfQ09NTUFORFM6CiAgICAgICAg"
    "ICAgICAgICAgICAgICAgIHdobyA9IHNlbGYudXNlci5uYW1lIGlmIHNlbGYudXNlciBlbHNlICc/"
    "JwogICAgICAgICAgICAgICAgICAgICAgICBwcmludChmJ1tjbWRdIHt3aG99IC0+IChVTkhBTkRM"
    "RUQgQkxPQiBhZnRlciB7Y21kIXJ9KSAnCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYn"
    "e2xlbihibG9iKX0gYnl0ZXMnKQogICAgZGVmIF9yZWN2TW9yZShzZWxmKToKICAgICAgICBjaHVu"
    "ayA9IHNlbGYucmVxdWVzdC5yZWN2KFJFQ1ZfQlVGX0xFTikKICAgICAgICBpZiBub3QgY2h1bms6"
    "CiAgICAgICAgICAgICNwZWVyIGRpc2Nvbm5lY3RlZCBkdXJpbmcgaGFuZHNoYWtlL2xvZ2luLCBz"
    "dG9wIHRoZSBidXN5LWxvb3AKICAgICAgICAgICAgcmFpc2UgQ29ubmVjdGlvblJlc2V0RXJyb3Io"
    "J2Rpc2Nvbm5lY3RlZCBkdXJpbmcgbG9naW4nKQogICAgICAgIHNlbGYuZGF0YSArPSBjaHVuawog"
    "ICAgZGVmIF9oYW5kbGUoc2VsZik6CiAgICAgICAgI1RPRE8gbG9nIGxvZ2luIGF0dGVtcHRzPwog"
    "ICAgICAgIHBlZXJfaXAgPSBzZWxmLmNsaWVudF9hZGRyZXNzWzBdCiAgICAgICAgcHJpbnQoJ0Nv"
    "bm5lY3Rpb24gYXR0ZW1wdCBmcm9tOicsIHBlZXJfaXApCiAgICAgICAgTElTID0gMiAjbG9naW4g"
    "c3RhdGUgI1RPRE8gY29uc2lkZXIgbG9uZyB0aW1lb3V0cz8KICAgICAgICB3aGlsZSBMSVM6CiAg"
    "ICAgICAgICAgIHdoaWxlIGxlbihzZWxmLmRhdGEpPDQ6CiAgICAgICAgICAgICAgICBzZWxmLl9y"
    "ZWN2TW9yZSgpCiAgICAgICAgICAgIHBhY2tfbGVuID0gc3RydWN0LnVucGFjaygnPEknLHNlbGYu"
    "ZGF0YVswOjRdKVswXQogICAgICAgICAgICBpZiBwYWNrX2xlbiA8IDQgb3IgcGFja19sZW4gPiBf"
    "TUFYX0hBTkRTSEFLRToKICAgICAgICAgICAgICAgICN1bnZhbGlkYXRlZCwgdGhpcyBpcyBhIHBy"
    "ZS1hdXRoZW50aWNhdGlvbiBtZW1vcnkgYm9tYjogYW4KICAgICAgICAgICAgICAgICN1bmF1dGhl"
    "bnRpY2F0ZWQgcGVlciBhbm5vdW5jZXMgYSA0R0IgcGFja2V0IGFuZCB0aGUgbG9vcCBiZWxvdwog"
    "ICAgICAgICAgICAgICAgI2J1ZmZlcnMgdW50aWwgdGhlIHByb2Nlc3MgZGllcwogICAgICAgICAg"
    "ICAgICAgcmFpc2UgUHJvdG9jb2xFcnJvcihmJ2hhbmRzaGFrZSBwYWNrZXQgbGVuZ3RoIHtwYWNr"
    "X2xlbn0gb3V0IG9mIHJhbmdlJykKICAgICAgICAgICAgd2hpbGUobGVuKHNlbGYuZGF0YSk8cGFj"
    "a19sZW4pOgogICAgICAgICAgICAgICAgc2VsZi5fcmVjdk1vcmUoKQogICAgICAgICAgICAjc2xp"
    "Y2UgdG8gcGFja19sZW4gKG5vdCB0byB0aGUgZW5kIG9mIHRoZSBidWZmZXIpOiBhbnl0aGluZyBw"
    "YXN0CiAgICAgICAgICAgICN0aGlzIHBhY2tldCBiZWxvbmdzIHRvIHRoZSBuZXh0IG9uZS4gQm91"
    "bmRlZCBkZWNvbXByZXNzLCBiZWNhdXNlIGEKICAgICAgICAgICAgIzY0ayBoYW5kc2hha2Ugb2Yg"
    "Y29tcHJlc3NlZCB6ZXJvZXMgZXhwYW5kcyB0byBodW5kcmVkcyBvZiBNQi4KICAgICAgICAgICAg"
    "cmVzID0gX2RlY29tcHJlc3NfYm91bmRlZChzZWxmLmRhdGFbNDpwYWNrX2xlbl0sIF9NQVhfSEFO"
    "RFNIQUtFX0lORkxBVEVEKQogICAgICAgICAgICBzZWxmLmRhdGEgPSBzZWxmLmRhdGFbcGFja19s"
    "ZW46XQogICAgICAgICAgICBpZiBMSVMgPT0gMjoKICAgICAgICAgICAgICAgIGdhbWV2ZXJzaW9u"
    "ID0gcmVzWzA6MTZdICNUT0RPIG5vdGUgZ2FtZSB2ZXJzaW9uICh1bnZlcmlmaWVkKSBwZXIgdXNl"
    "cgogICAgICAgICAgICAgICAgbGFuZ25hbWUsIG9mZiA9IHBhcnNlRHN0cihyZXMsIDE2KQogICAg"
    "ICAgICAgICAgICAgI1RPRE8gY29uc2lkZXIgVFdTRSBpbmRpY2F0b3IgdG8gY3JlYXRlIHNlY3Vy"
    "ZSBjb25uZWN0aW9uPwogICAgICAgICAgICAgICAgI1RPRE8gY2hlY2sgaWYgdmFuaWxsYSBzZXJ2"
    "ZXIgaWdub3JlcyBleHRyYSBkYXRhIGluIGhhbmRzaGFrZSBwcm9jZXNzCiAgICAgICAgICAgICAg"
    "ICBSSyA9IHJlc1tvZmYrODpvZmYrMTZdCiAgICAgICAgICAgICAgICBmb3IgaSBpbiByYW5nZShs"
    "ZW4oUkspKToKICAgICAgICAgICAgICAgICAgICBzZWxmLlNLW2ldXj1SS1tpXQogICAgICAgICAg"
    "ICAgICAgI3dhcyBoYXJkY29kZWQgJ1RXMUNTJyB3aXRoIGEgIlNFUlZFUiBOQU1FIGNmZ1RPRE8i"
    "IG5vdGU6IHRoZQogICAgICAgICAgICAgICAgI25hbWUgY29uZmlndXJlZCBpbiBDb25maWcuaW5p"
    "L3RoZSBHVUkgcmVhY2hlZCB0aGUgd2VsY29tZQogICAgICAgICAgICAgICAgI3BhY2tldCBidXQg"
    "bmV2ZXIgdGhpcyBvbmUsIHNvIHRoZSBwcmUtbG9naW4gaGFuZHNoYWtlIGFsd2F5cwogICAgICAg"
    "ICAgICAgICAgI2Fubm91bmNlZCB0aGUgcGxhY2Vob2xkZXIuCiAgICAgICAgICAgICAgICBzZWxm"
    "LnNlbmRSYXcoX3NlcnZlcl9pbmZvX3BhY2tldChzYW5pdGl6ZVRleHQoREVGQVVMVF9USVRMRSkp"
    "KQogICAgICAgICAgICAgICAgI1RPRE8gVFcxQ1MgaW5kaWNhdG9yIGZvciBUV1NFIGNsaWVudCB0"
    "byBjcmVhdGUgc2VjdXJlIGNvbm5lY3Rpb24gb3IgcHJlLWhhc2ggcGFzc3dvcmQ/CiAgICAgICAg"
    "ICAgICAgICBMSVMgPSAxIAogICAgICAgICAgICAgICAgc2VsZi5TSyA9IGJ5dGVzKHNlbGYuU0sp"
    "CiAgICAgICAgICAgIGVsaWYgTElTID09IDE6CiAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0g"
    "LTEKICAgICAgICAgICAgICAgICNTdGFsbCByZXBlYXQgb2ZmZW5kZXJzIGJlZm9yZSBkb2luZyBh"
    "bnkgUEJLREYyIHdvcmsgZm9yIHRoZW0uCiAgICAgICAgICAgICAgICAjU2xlZXBpbmcgaW4gdGhp"
    "cyBoYW5kbGVyIHRocmVhZCBpcyB0aGUgcG9pbnQ6IGl0IGNvc3RzIHVzCiAgICAgICAgICAgICAg"
    "ICAjbm90aGluZyBhbmQgcmF0ZS1saW1pdHMgdGhhdCBjb25uZWN0aW9uLgogICAgICAgICAgICAg"
    "ICAgZGVsYXkgPSBMT0dJTl9USFJPVFRMRS5kZWxheUZvcihwZWVyX2lwKQogICAgICAgICAgICAg"
    "ICAgaWYgZGVsYXk6CiAgICAgICAgICAgICAgICAgICAgdGltZS5zbGVlcChkZWxheSkKICAgICAg"
    "ICAgICAgICAgIHVzZXJuYW1lLCBvZmYgPSBwYXJzZURzdHIocmVzLCAwKQogICAgICAgICAgICAg"
    "ICAgcGFzc3dvcmQsIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAgICAgICAgICAgICNU"
    "T0RPIFRXU0UgbW9kIGZvciBoaWdoZXIgbG9naW4gc2VjdXJpdHkKICAgICAgICAgICAgICAgICMt"
    "ZW5jcnlwdGVkIGNvbm5lY3Rpb24gdG8gcHJldmVudCByZXBsYXkgYXR0YWNrcwogICAgICAgICAg"
    "ICAgICAgIy1wcmVoYXNoIHBhc3N3b3JkIHdpdGggc2VyaWFsPywgY2hlY2sgaWYgcmVjb3Zlcnkg"
    "cG9zc2libGUuCiAgICAgICAgICAgICAgICBzZWxmLmd1aWQgPSBieXRlcyhyZXNbb2ZmOm9mZisx"
    "Nl0pCiAgICAgICAgICAgICAgICAjcHJpbnQoJ2d1aWQgYnl0ZTonLCBzZWxmLmd1aWRbMV0pCiAg"
    "ICAgICAgICAgICAgICAjc2VsZi5ndWlkID0gYnl0ZWFycmF5KHJlc1tvZmY6b2ZmKzE2XSkKICAg"
    "ICAgICAgICAgICAgICNzZWxmLmd1aWRbMV1ePTB4MTYgI0RPIE5PVCBwZXJmb3JtIHNlcnZlcnNp"
    "ZGUKICAgICAgICAgICAgICAgICNzZWxmLmd1aWQgPSBieXRlcyhzZWxmLmd1aWQpCiAgICAgICAg"
    "ICAgICAgICBvZmYrPTE2CiAgICAgICAgICAgICAgICBpc3JlZyA9IHN0cnVjdC51bnBhY2soJzxJ"
    "JyxyZXNbb2ZmOm9mZis0XSlbMF0KICAgICAgICAgICAgICAgIG9mZis9NAogICAgICAgICAgICAg"
    "ICAgdmlhUmVnaXN0ZXIgPSBib29sKGlzcmVnKQogICAgICAgICAgICAgICAgaWYgaXNyZWc6CiAg"
    "ICAgICAgICAgICAgICAgICAgZW1haWwsIG9mZiA9IHBhcnNlRHN0cihyZXMsIG9mZikKICAgICAg"
    "ICAgICAgICAgICAgICBsb2NhdGlvbiwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAgICAg"
    "ICAgICAgICAgICAgIGFnZSA9IHJlc1tvZmZdCiAgICAgICAgICAgICAgICAgICAgZ2VuZGVyID0g"
    "cmVzW29mZisxXQogICAgICAgICAgICAgICAgICAgIG9mZis9MiAjYWdlLCBnZW5kZXIKICAgICAg"
    "ICAgICAgICAgICAgICBkZXNjcmlwdGlvbiwgb2ZmID0gcGFyc2VEc3RyKHJlcywgb2ZmKQogICAg"
    "ICAgICAgICAgICAgICAgIGxvZ2luRXJyb3IgPSBzZWxmLmF0dGVtcHRSZWdpc3Rlcih1c2VybmFt"
    "ZSwgcGFzc3dvcmQsIGVtYWlsLCBsb2NhdGlvbiwgYWdlLCBnZW5kZXIsIGRlc2NyaXB0aW9uKQog"
    "ICAgICAgICAgICAgICAgZWxzZToKICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9yID0gc2Vs"
    "Zi5hdHRlbXB0TG9naW4odXNlcm5hbWUsIHBhc3N3b3JkKQogICAgICAgICAgICAgICAgICAgIGlm"
    "IGxvZ2luRXJyb3IgPT0gMSBhbmQgX0FVVE9fUkVHSVNURVI6CiAgICAgICAgICAgICAgICAgICAg"
    "ICAgIHZpYVJlZ2lzdGVyID0gVHJ1ZQogICAgICAgICAgICAgICAgICAgICAgICBsb2dpbkVycm9y"
    "ID0gc2VsZi5hdHRlbXB0UmVnaXN0ZXIodXNlcm5hbWUsIHBhc3N3b3JkLCAiIiwgIiIsIDEsIDAs"
    "ICIiKQogICAgICAgICAgICAgICAgICAgICAgICBpZiBsb2dpbkVycm9yIGFuZCBHREgubmFtZVRh"
    "a2VuKHVzZXJuYW1lKToKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNUaGUgYWNjb3VudCBl"
    "eGlzdHMsIHNvIHRoaXMgd2FzIG5ldmVyIGEKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNy"
    "ZWdpc3RyYXRpb246IHRoZSBsb2dpbiBiZWZvcmUgaXQgZmFpbGVkIG9uIHRoZQogICAgICAgICAg"
    "ICAgICAgICAgICAgICAgICAgI3Bhc3N3b3JkIG9yIC0gZmFyIG1vcmUgb2Z0ZW4gLSBvbiB0aGUg"
    "c2VyaWFsLAogICAgICAgICAgICAgICAgICAgICAgICAgICAgI2JlY2F1c2UgYWNjb3VudHMgYXJl"
    "IGJvdW5kIHRvIHRoZSBrZXkgdGhlIGNsaWVudAogICAgICAgICAgICAgICAgICAgICAgICAgICAg"
    "I2hhbmRzaGFrZXMgd2l0aCAoc2VlIGxvZ2luUGxheWVyJ3Mgc3RyaWN0IGxvb2t1cCkuCiAgICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAjRmFsbGluZyB0aHJvdWdoIHRvIHRoZSByZWdpc3RyYXRp"
    "b24gd29yZGluZyB0b2xkIGEKICAgICAgICAgICAgICAgICAgICAgICAgICAgICNwbGF5ZXIgd2hv"
    "IGhhZCByZWluc3RhbGxlZCB0aGUgZ2FtZSB0aGF0IHRoZWlyCiAgICAgICAgICAgICAgICAgICAg"
    "ICAgICAgICAjKnVzZXJuYW1lKiB3YXMgaW52YWxpZCwgd2hpY2ggc2VudCB0aGVtIG9mZgogICAg"
    "ICAgICAgICAgICAgICAgICAgICAgICAgI2ludmVudGluZyBuZXcgbmFtZXMgdGhhdCBjb3VsZCBu"
    "ZXZlciB3b3JrLgogICAgICAgICAgICAgICAgICAgICAgICAgICAgdmlhUmVnaXN0ZXIgPSBGYWxz"
    "ZQogICAgICAgICAgICAgICAgICAgICAgICAgICAgbG9naW5FcnJvciA9IDUKICAgICAgICAgICAg"
    "ICAgIGlmIGxvZ2luRXJyb3IgPT0gMDoKICAgICAgICAgICAgICAgICAgICBMT0dJTl9USFJPVFRM"
    "RS5yZWNvcmRTdWNjZXNzKHBlZXJfaXApCiAgICAgICAgICAgICAgICAgICAgI1RPRE8gYmV0dGVy"
    "IGhhbmRsaW5nIG9mIFRJVExFIEFORCBNT1RECiAgICAgICAgICAgICAgICAgICAgc2VsZi5zZW5k"
    "UmF3KF9zZXJ2ZXJfd2VsY29tZV9wYWNrZXQoYnl0ZXMoc2VsZi5TSyksIERFRkFVTFRfVElUTEUs"
    "IERFRkFVTFRfTU9URCkpCiAgICAgICAgICAgICAgICAgICAgTElTID0gMAogICAgICAgICAgICAg"
    "ICAgZWxzZTogI2Vycm9yIGJhc2VkIG9uIGxvZ2luRXJyb3IgbnVtYmVyCiAgICAgICAgICAgICAg"
    "ICAgICAgY291bnQgPSBMT0dJTl9USFJPVFRMRS5yZWNvcmRGYWlsdXJlKHBlZXJfaXApCiAgICAg"
    "ICAgICAgICAgICAgICAgaWYgY291bnQgPT0gX0xPR0lOX0ZBSUxfTElNSVQ6CiAgICAgICAgICAg"
    "ICAgICAgICAgICAgIHByaW50KGYnW0xvYmJ5XSBUaHJvdHRsaW5nIGxvZ2lucyBmcm9tIHtwZWVy"
    "X2lwfSAnCiAgICAgICAgICAgICAgICAgICAgICAgICAgICAgIGYnKHtjb3VudH0gZmFpbHVyZXMg"
    "aW4ge19MT0dJTl9GQUlMX1dJTkRPV31zKScpCiAgICAgICAgICAgICAgICAgICAgZXJybXNncyA9"
    "IF9SRUdJU1RFUl9FUlJPUlMgaWYgdmlhUmVnaXN0ZXIgZWxzZSBfTE9HSU5fRVJST1JTCiAgICAg"
    "ICAgICAgICAgICAgICAgc2VsZi5zZW5kUmF3KF9pbml0X2Vycm9yKGVycm1zZ3MuZ2V0KGxvZ2lu"
    "RXJyb3IsICdMb2dpbiBmYWlsZWQnKSkpCiAgICBkZWYgZmluaXNoKHNlbGYpOgogICAgICAgIHNl"
    "bGYuc2VydmVyLnVucmVnaXN0ZXJDb25uZWN0aW9uKHNlbGYpCiAgICAgICAgI1N0b3AgdGhlIHdy"
    "aXRlciBmaXJzdDogaXQgaG9sZHMgdGhpcyBzb2NrZXQgYW5kIHdvdWxkIG90aGVyd2lzZSBrZWVw"
    "CiAgICAgICAgI3dyaXRpbmcgb24gYmVoYWxmIG9mIGEgcGxheWVyIHdobyBoYXMgYWxyZWFkeSBs"
    "ZWZ0IGV2ZXJ5IGNoYW5uZWwuCiAgICAgICAgc2VsZi5fc3RvcFdyaXRlcigpCiAgICAgICAgaWYg"
    "c2VsZi51c2VyOgogICAgICAgICAgICBwcmludChmJ1VzZXI6IHtzZWxmLnVzZXIubmFtZX0gRGlz"
    "Y29ubmVjdGVkJykKICAgICAgICAgICAgc2VsZi51c2VyLmRpc2Nvbm5lY3Qoc2VsZi5zZXJ2ZXIp"
    "CiAgICAgICAgI2NsZWFudXAgdXNlciBkYXRhCiAgICAgICAgI1RPRE8gY2hlY2sgaWYgdHJpZ2dl"
    "cmVkIG9uIGNyYXNoZWQgY29ubmVjdGlvbgogICAgZGVmIGRlYnVnX2RpY3Qoc2VsZik6CiAgICAg"
    "ICAgaWYgc2VsZi51c2VyIGlzIE5vbmU6CiAgICAgICAgICAgICNQb2xsZWQgYnkgdGhlIGNvbnRy"
    "b2wgcGFuZWwgb25jZSBhIHNlY29uZCB3aGlsZSBwbGF5ZXJzIGNvbm5lY3QgYW5kCiAgICAgICAg"
    "ICAgICNkaXNjb25uZWN0OyBhIGNvbm5lY3Rpb24gY2F1Z2h0IGJldHdlZW4gdGhlIHR3byB1c2Vk"
    "IHRvIHJhaXNlIGhlcmUKICAgICAgICAgICAgI2FuZCBjb3N0IHRoZSBwYW5lbCBpdHMgd2hvbGUg"
    "cGxheWVyIHRhYmxlIGZvciB0aGF0IHRpY2suCiAgICAgICAgICAgIHJldHVybiB7J2dhbWUnOicn"
    "LCAndG93bic6JycsICdwb3MnOicnLCAnaWQnOjAsICdsb2dpblRpbWUnOicnfQogICAgICAgIHJl"
    "dHVybiB7CiAgICAgICAgICAgICNUT0RPIElQIGZvciBlbGV2YXRlZCBhdXRob3JpdHkKICAgICAg"
    "ICAgICAgIyduYW1lJzpzZWxmLnVzZXIubmFtZSwKICAgICAgICAgICAgJ2dhbWUnOnNlbGYudXNl"
    "ci5nYW1lLmduYW1lIGlmIHNlbGYudXNlci5nYW1lIGVsc2UgJycsCiAgICAgICAgICAgICd0b3du"
    "JzpzZWxmLnVzZXIuZ2FtZWNoYW5uZWwubmFtZSBpZiBzZWxmLnVzZXIuZ2FtZWNoYW5uZWwgZWxz"
    "ZSAnJywKICAgICAgICAgICAgJ3Bvcyc6c2VsZi51c2VyLnBvc2RhdGEgaWYgc2VsZi51c2VyLnBv"
    "c2RhdGEgZWxzZSAnJywKICAgICAgICAgICAgJ2lkJzpzZWxmLnVzZXIuaWRudW0sCiAgICAgICAg"
    "ICAgICdsb2dpblRpbWUnOmpzb25UaW1lKHNlbGYudXNlci5sb2dpblRpbWUpCiAgICAgICAgfSNU"
    "T0RPIGVsZXZhdGVkIGF1dGhvcml0eSB2ZXJzaW9uCgpkZWYgY21kX2RlZmF1bHQoKTojYXJncyk6"
    "CiAgICAjcHJpbnQoYXJncykKICAgICNfcmVhZGNvbmZpZygpCiAgICBzZXJ2ZXIgPSBDb3JlU2Vy"
    "dmVyKCkKICAgIHdpdGggc2VydmVyOgogICAgICAgIHRzdCA9IHNpZ25hbC5zaWduYWwoc2lnbmFs"
    "LlNJR0lOVCwgc2VydmVyLmhhbmRsZV9zaWduYWwodGltZW91dD0yKSkKICAgICAgICAjcHJpbnQo"
    "J0Fzc2lnbmVkIFNpZ25hbD8nLCB0c3QpCiAgICAgICAgI3NpZ25hbC5zaWduYWwoc2lnbmFsLlNJ"
    "R1RFUk0sIHNlcnZlci5oYW5kbGVfc2lnbmFsKHRpbWVvdXQ9MSkpCiAgICAgICAgc2VydmVyLnNl"
    "cnZlX2ZvcmV2ZXIoKQoKI3NjcmlwdCBsYXVuY2hlZCwgY2hlY2sgYXJndW1lbnRzIGFuZCBjb25m"
    "aWcuIHNldHVwIHZhcmlvdXMgb2JqZWN0cwppZiBfX25hbWVfXyA9PSAnX19tYWluX18nOgogICAg"
    "cHJpbnQoJ0luaXRpYWxpemluZyBTZXJ2ZXInKQogICAgY21kX2RlZmF1bHQoKQo="
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
